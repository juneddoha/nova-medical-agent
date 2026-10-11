import json
import tempfile
import unittest
from pathlib import Path

from simulator.environment import LocalEnvironment, load_cases
from simulator.evaluator import evaluate
from src.actions import Action, ActionType
from src.agent import Agent
from src.llm import Decision, MockLLMAdapter
from src.trajectory import TrajectoryWriter


CASES = Path(__file__).resolve().parents[1] / "simulator" / "sample_cases.json"


class RepeatingAdapter:
    def next_action(self, state):
        return Decision(Action(ActionType.SAY, " 같은   질문? "), "중복 감지 확인")


class InvalidAdapter:
    def next_action(self, state):
        return None


class BaselineTest(unittest.TestCase):
    def test_response_aliases_preserve_requested_source_and_unknown_questions(self):
        environment = LocalEnvironment(load_cases(CASES)[0])
        original = environment.observe(Action(ActionType.SAY, "통증이 시작된 위치는 어디인가요?"), 1)
        action = Action(ActionType.SAY, "  처음  어디가 아팠나요? ")
        alias = environment.observe(action, 2)
        self.assertEqual((alias.value, alias.status), (original.value, original.status))
        self.assertEqual(alias.source_action, action)
        self.assertEqual(alias.source_turn, 2)
        for action in [Action(ActionType.SAY, "처음 어디가 아프지 않았나요?"),
                       Action(ActionType.EXAM, "처음 어디가 아팠나요?")]:
            self.assertEqual(environment.observe(action, 3).status, "unknown")

    def test_response_aliases_reject_invalid_values_and_conflicting_mappings(self):
        for aliases in ("별칭", [""], [None], ["처음?", " 처음? "],
                        ["통증이 시작된 위치는 어디인가요?"]):
            with self.subTest(aliases=aliases), tempfile.TemporaryDirectory() as directory:
                data = json.loads(CASES.read_text(encoding="utf-8"))
                data[0]["responses"][0]["aliases"] = aliases
                path = Path(directory) / "cases.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_cases(path)
        with tempfile.TemporaryDirectory() as directory:
            data = json.loads(CASES.read_text(encoding="utf-8"))
            data[0]["responses"].append({"type": "SAY", "content": "처음 어디가 아팠나요?",
                                          "value": "상충하는 결과", "status": "negative"})
            path = Path(directory) / "cases.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cases(path)

    def test_two_cases_finish_without_state_leakage(self):
        cases = load_cases(CASES)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trajectory.jsonl"
            with TrajectoryWriter(path) as writer:
                results = [
                    Agent(MockLLMAdapter()).run(
                        case.case_id, case.demographics, case.chief_complaint,
                        LocalEnvironment(case).observe, writer,
                    )
                    for case in cases
                ]
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(records), 6)
        self.assertEqual([result.state.turn for result in results], [2, 2])
        self.assertTrue(all(evaluate(case, result)["correct"] for case, result in zip(cases, results)))
        self.assertEqual(results[0].state.case_id, "toy-abdominal-pain")
        self.assertEqual(results[1].state.case_id, "toy-cough")
        self.assertNotEqual(results[0].state.observations[0].value, results[1].state.observations[0].value)
        self.assertEqual({record["case_id"] for record in records}, {case.case_id for case in cases})
        self.assertTrue(all(record["turn"] <= 50 for record in records))
        self.assertTrue(all(record["action"]["type"] == "DIAGNOSE" for record in (records[2], records[5])))

    def test_repeated_action_is_not_sent_to_environment(self):
        case = load_cases(CASES)[0]
        calls = []

        def observe(action, turn):
            calls.append((action, turn))
            return LocalEnvironment(case).observe(action, turn)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trajectory.jsonl"
            with TrajectoryWriter(path) as writer:
                result = Agent(RepeatingAdapter(), max_turns=4, max_attempts=4).run(
                    case.case_id, case.demographics, case.chief_complaint, observe, writer,
                )
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(calls), 1)
        self.assertEqual(result.duplicate_actions, 2)
        self.assertEqual(result.state.turn, 1)
        self.assertEqual(result.state.final_diagnosis, "미확정")
        self.assertEqual([record.get("error") for record in records[1:3]], ["duplicate_action"] * 2)
        self.assertEqual(records[-1]["action"]["type"], "DIAGNOSE")

    def test_invalid_model_output_is_logged_and_ends_with_diagnosis(self):
        case = load_cases(CASES)[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trajectory.jsonl"
            with TrajectoryWriter(path) as writer:
                result = Agent(InvalidAdapter(), max_turns=3, max_attempts=3).run(
                    case.case_id, case.demographics, case.chief_complaint,
                    LocalEnvironment(case).observe, writer,
                )
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result.format_errors, 3)
        self.assertEqual(result.state.turn, 0)
        self.assertEqual([record["error"] for record in records[:2]], ["invalid_decision"] * 2)
        self.assertEqual(records[-1]["action"]["type"], "DIAGNOSE")


if __name__ == "__main__":
    unittest.main()
