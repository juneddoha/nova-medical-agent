import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event

from simulator.compare import compare
from simulator.environment import LocalEnvironment, load_cases
from src.actions import Action, ActionType, ExamRejected
from src.agent import Agent
from src.connection import ConnectionSettings, ModelReply, PreparedAPIAdapter
from src.llm import Decision, InvalidDecision, MockLLMAdapter, StructuredLLMAdapter, parse_decision
from src.state import Observation, PatientState


ROOT = Path(__file__).resolve().parents[1]


class MemoryLog:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)


def reply(kind="DIAGNOSE", content="미확정"):
    return json.dumps({"action": {"type": kind, "content": content}, "rationale": "연결 검증",
                       "differential": [], "plan": "추가 평가 계획" if kind == "DIAGNOSE" else ""})


class OTTest(unittest.TestCase):
    def test_fifty_actions_then_zero_turn_diagnosis_uses_latest_observation(self):
        seen = []

        def complete(messages):
            state = json.loads(messages[1]["content"])
            seen.append(state)
            return reply() if state["final_turn"] else reply("EXAM", "진찰 " + str(state["turn"]))

        log = MemoryLog()
        result = Agent(StructuredLLMAdapter(complete)).run(
            "fifty", {}, "검증", lambda action, turn: Observation("결과", "unknown", turn, action), log)
        self.assertEqual(result.state.turn, 50)
        self.assertEqual(result.state.attempt, 51)
        self.assertEqual(len(result.state.observations), 50)
        self.assertEqual(seen[-1]["observations"][-1]["source_turn"], 50)
        self.assertEqual(log.records[-1]["turn"], 50)
        self.assertFalse(result.forced_diagnosis)

    def test_action_rules_reject_before_environment_and_allow_final_test(self):
        for action in (Action(ActionType.SAY, "가" * 31), Action(ActionType.SAY, "언제? 어디?"),
                       Action(ActionType.EXAM, "폐 청진 및 복부 촉진"), Action(ActionType.TEST, "검사")):
            with self.subTest(action=action):
                sequence = iter([Decision(action, "검증"), parse_decision(reply())])

                class Adapter:
                    def next_action(self, state):
                        return next(sequence)

                calls = []
                result = Agent(Adapter()).run("rules", {}, "검증", lambda *args: calls.append(args), MemoryLog())
                self.assertEqual(calls, [])
                self.assertEqual(result.format_errors, 1)
                self.assertEqual(result.state.turn, 0)
        Action(ActionType.SAY, "가" * 30).validate_for_stage("preliminary")
        case = load_cases(ROOT / "simulator/sample_cases.json")[0]
        result = Agent(MockLLMAdapter(), stage="final").run(
            case.case_id, case.demographics, case.chief_complaint, LocalEnvironment(case).observe, MemoryLog())
        self.assertEqual(result.state.turn, 3)
        self.assertEqual(sum(a.type == ActionType.TEST for a in result.state.actions_taken), 1)

    def test_rejected_exam_does_not_cost_turn_and_can_be_retried(self):
        calls = []

        def complete(messages):
            state = json.loads(messages[1]["content"])
            return reply() if state["observations"] else reply("EXAM", "진찰 요청")

        def observe(action, turn):
            calls.append(turn)
            if len(calls) == 1:
                raise ExamRejected("server rejection")
            return Observation("소견", "negative", turn, action)

        log = MemoryLog()
        result = Agent(StructuredLLMAdapter(complete)).run("reject", {}, "검증", observe, log)
        self.assertEqual(calls, [1, 1])
        self.assertEqual(result.state.turn, 1)
        self.assertEqual(log.records[0]["error"], "exam_rejected")
        self.assertEqual(result.execution_failures, 0)

    def test_deadline_returns_without_waiting_for_callback_or_accepting_late_state(self):
        release, finished = Event(), Event()

        class BlockingAdapter:
            def next_action(self, state):
                release.wait(2)
                state.chief_complaint = "late mutation"
                finished.set()
                return parse_decision(reply())

        try:
            result = Agent(BlockingAdapter(), time_limit_seconds=0.1, submission_reserve_seconds=0.02).run(
                "timeout", {}, "original", None, MemoryLog())
            self.assertTrue(result.forced_diagnosis)
            self.assertEqual(result.state.last_error, "call_deadline_exceeded")
            self.assertLess(result.elapsed_ms, 1500)
            self.assertFalse(finished.is_set())
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(result.state.chief_complaint, "original")

    def test_soap_contains_received_evidence_and_diagnosis_requires_plan(self):
        case = load_cases(ROOT / "simulator/sample_cases.json")[0]
        result = Agent(MockLLMAdapter()).run(case.case_id, case.demographics, case.chief_complaint,
                                            LocalEnvironment(case).observe, MemoryLog(),
                                            vital_signs={"pulse": "80"})
        soap = result.state.soap
        self.assertEqual(set(soap), {"S", "O", "A", "P"})
        self.assertIn("배꼽 주변", soap["S"])
        self.assertIn("턴 2", soap["O"])
        self.assertIn("80", soap["O"])
        self.assertNotIn("백혈구", soap["O"])
        self.assertEqual(soap["A"]["primary_diagnosis"], result.state.final_diagnosis)
        self.assertEqual(result.competition_model_calls, 0)
        raw = json.loads(reply())
        del raw["plan"]
        with self.assertRaises(InvalidDecision):
            parse_decision(json.dumps(raw))

    def test_transport_contract_usage_and_per_case_call_counts(self):
        requests = []

        def complete(messages, **kwargs):
            requests.append((messages, kwargs))
            return ModelReply(reply(), 10, 20)

        adapter = PreparedAPIAdapter(complete, lambda messages, model: 10)
        agent = Agent(adapter)
        for case_id in ("one", "two"):
            result = agent.run(case_id, {}, "검증", None, MemoryLog())
            self.assertEqual(result.competition_model_calls, 0)
        self.assertEqual(adapter.usage()["calls"], 2)
        self.assertEqual(adapter.input_tokens, 20)
        self.assertEqual(adapter.output_tokens, 40)
        self.assertEqual(requests[0][1]["model"], "openai/gpt-oss-20b")
        self.assertLessEqual(requests[0][1]["timeout_seconds"], 60)
        self.assertEqual(json.loads(requests[1][0][1]["content"])["observations"], [])
        self.assertNotIn("private", repr(ConnectionSettings("private-url", "private-key")))

    def test_budget_exhaustion_and_unknown_usage_stop_future_requests(self):
        for calls, tokens_in, tokens_out in [(200, 0, 0), (0, 500000, 0), (0, 0, 100000)]:
            with self.subTest(calls=calls, tokens_in=tokens_in, tokens_out=tokens_out):
                invoked = []
                adapter = PreparedAPIAdapter(lambda *args, **kwargs: invoked.append(1), lambda *args: 1)
                adapter.calls, adapter.input_tokens, adapter.output_tokens = calls, tokens_in, tokens_out
                with self.assertRaises(RuntimeError):
                    adapter.next_action(PatientState("one", {}, "검증"))
                self.assertEqual(invoked, [])
        adapter = PreparedAPIAdapter(lambda *args, **kwargs: ModelReply(reply(), 1, -1), lambda *args: 1)
        with self.assertRaises(ValueError):
            adapter.next_action(PatientState("one", {}, "검증"))
        with self.assertRaises(RuntimeError):
            adapter.next_action(PatientState("two", {}, "검증"))
        self.assertEqual(adapter.calls, 1)

    def test_connection_check_is_explicitly_offline(self):
        process = subprocess.run([sys.executable, str(ROOT / "run.py"), "--check-connection"],
                                 capture_output=True, encoding="utf-8", timeout=10, cwd=ROOT)
        self.assertEqual(process.returncode, 0, process.stderr)
        data = json.loads(process.stdout)
        self.assertEqual(data["network_requests"], 0)
        self.assertFalse(data["real_api_verified"])
        self.assertTrue(data["official_transport_implemented"])
        self.assertFalse(data["stub_usage"]["competition_transport"])

    def test_compare_handles_zero_clinical_turns_and_refuses_mixed_turn_definitions(self):
        data = {"schema_version": 2, "stage": "preliminary", "mode": "baseline", "cases_sha256": "same",
                "cases": [{"case_id": "zero", "diagnosis": "미확정", "correct": False,
                           "turns": 0, "attempts": 3, "say_count": 0, "exam_count": 0, "test_count": 0,
                           "duplicate_actions": 0, "format_errors": 3, "execution_failures": 0,
                           "forced_diagnosis": True, "elapsed_ms": 1}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            report = compare(path, path)
            self.assertEqual(report["after"]["mean_turns"], 0)
            self.assertEqual(report["after"]["error_turn_rate"], 1)
            other = Path(directory) / "other.json"
            data["stage"] = "final"
            other.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                compare(path, other)


if __name__ == "__main__":
    unittest.main()
