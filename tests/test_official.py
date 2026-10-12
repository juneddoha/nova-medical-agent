import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import run
import sample
from nova_sdk import Client, NovaError, empty_record
from src.actions import Action, ActionType
from src.official import RECORD_PROMPT, consult, parse_record


def step(kind, content):
    return json.dumps({"action": {"type": kind, "content": content}, "rationale": "관찰에 따른 판단",
                       "differential": [], "plan": "추가 평가 계획" if kind == "DIAGNOSE" else ""})


def record():
    note = empty_record()
    note["S"]["cc"] = "확인용 증상"
    note["O"]["exam"] = "초기 정보: 맥박 80회/분; 턴 2: 진찰 소견"
    note["P"]["follow_up"] = "추가 평가 계획"
    return json.dumps({"note": note, "diagnosis": "미확정"})


class HTTPReply:
    def __init__(self, payload, status=200):
        self.status = status
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self, limit):
        return self.payload[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Server:
    """Mock HTTP boundary; production Client serialization and validation still run."""
    def __init__(self, replies, turn=0, observations=None):
        self.replies = iter(replies)
        self.observations = iter(observations) if observations is not None else None
        self.turn = turn
        self.requests, self.cached = [], {}
        self.refuse_exam = False
        self.lose_action_reply = False
        self.provider_error = False
        self.receipt_status = "completed"
        self.deadline = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()

    def open(self, request):
        body = json.loads(request.data) if request.data else None
        path = request.full_url.split("http://sdk.test", 1)[-1]
        self.requests.append((path, body))
        if path == "/_nova/ready":
            return HTTPReply({"ready": True})
        if path == "/v1/sample/cases":
            return HTTPReply({"cases": [{"id": "case-id"}]})
        if path == "/v1/session":
            return HTTPReply({"session_id": "00000000-0000-0000-0000-000000000001", "status": "active",
                              "initial": {"opening": "확인용 환자 첫 진술", "vital_signs": "맥박 80회/분"},
                              "turns_remaining": 50 - self.turn, "deadline": self.deadline})
        request_id = body["request_id"]
        if request_id in self.cached:
            return HTTPReply(self.cached[request_id])
        if path == "/v1/chat/completions":
            if self.provider_error:
                self.provider_error = False
                return HTTPReply({"error": {"code": "PROVIDER_RESPONSE_INVALID"}}, 503)
            payload = {"choices": [{"message": {"content": next(self.replies)}}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
        elif body["action"] == "DIAGNOSE":
            payload = {"status": self.receipt_status, "record_digest": "test-digest", "turn": self.turn}
        else:
            if body["action"] == "EXAM" and self.refuse_exam:
                self.refuse_exam = False
                return HTTPReply({"error": {"code": "EXAM_NOT_SPECIFIC"}}, 400)
            self.turn += 1
            field = "content" if body["action"] == "SAY" else "result"
            value = next(self.observations) if self.observations is not None else "실제로 받은 원문"
            payload = {"status": "OK", field: value, "turn": self.turn,
                       "turns_remaining": 50 - self.turn}
            if self.lose_action_reply:
                self.lose_action_reply = False
                self.cached[request_id] = payload
                raise urllib.error.URLError("connection dropped after acceptance")
        self.cached[request_id] = payload
        return HTTPReply(payload)


class OfficialTest(unittest.TestCase):
    def execute(self, server, entry=consult):
        client = Client(api_base="http://sdk.test", token="SECRET_TEST_TOKEN")
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"NOVA_OUTPUT_DIR": directory}), \
                patch.object(client, "_open", side_effect=server.open), \
                patch("src.official.time.sleep"):
            result = entry(client, None)
            raw = "".join(p.read_text(encoding="utf-8") for p in Path(directory).glob("*.jsonl"))
        self.assertNotIn("SECRET_TEST_TOKEN", raw)
        return result, [json.loads(line) for line in raw.splitlines()]

    def test_real_sdk_serialization_full_consultation_and_receipt(self):
        server = Server([step("SAY", "언제 시작됐나요?"), step("EXAM", "복부 청진하겠습니다"),
                         step("DIAGNOSE", "미확정"), record()])
        result, logs = self.execute(server)
        actions = [b for p, b in server.requests if p == "/v1/session/actions"]
        self.assertEqual([a["action"] for a in actions], ["SAY", "EXAM", "DIAGNOSE"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(logs[-1]["event"], "submitted")
        self.assertEqual(logs[-1]["doctor_responses"], 4)
        self.assertEqual(logs[-1]["input_tokens"], 40)
        self.assertEqual(logs[-1]["output_tokens"], 80)
        note = actions[-1]["note"]
        self.assertEqual(set(note["A"]), {"evidence", "differential"})
        self.assertNotIn("diagnosis", note["A"])
        self.assertEqual(note["O"]["tests"], "")
        messages = [b["messages"] for p, b in server.requests if p == "/v1/chat/completions"]
        self.assertEqual(json.loads(messages[0][1]["content"])["vital_signs"], {"raw": "맥박 80회/분"})
        state = json.loads(messages[2][1]["content"])
        self.assertEqual([o["source_turn"] for o in state["observations"]], [1, 2])
        self.assertEqual(messages[-1][0]["content"], RECORD_PROMPT)

    def test_transport_loss_replays_same_action_id_without_double_turn(self):
        server = Server([step("SAY", "언제 시작됐나요?"), step("DIAGNOSE", "미확정"), record()])
        server.lose_action_reply = True
        self.execute(server)
        actions = [b for p, b in server.requests if b and b.get("action") == "SAY"]
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0], actions[1])
        self.assertEqual(server.turn, 1)

    def test_known_provider_failure_gets_new_id_and_exam_rejection_costs_no_turn(self):
        server = Server([step("EXAM", "복부 청진하겠습니다"), step("EXAM", "복부 청진하겠습니다"),
                         step("DIAGNOSE", "미확정"), record()])
        server.provider_error = server.refuse_exam = True
        _, logs = self.execute(server)
        chats = [b for p, b in server.requests if p == "/v1/chat/completions"]
        self.assertNotEqual(chats[0]["request_id"], chats[1]["request_id"])
        retry_state = json.loads(chats[2]["messages"][1]["content"])
        self.assertEqual(retry_state["turn"], 0)
        self.assertEqual(retry_state["last_error"], "EXAM_NOT_SPECIFIC")
        self.assertEqual(server.turn, 1)
        self.assertEqual(logs[-1]["turn"], 1)

    def test_all_fifty_turns_keep_last_observation_before_record(self):
        server = Server([step("SAY", "질문 {}".format(i)) for i in range(50)] + [record()])
        self.execute(server)
        chats = [b for p, b in server.requests if p == "/v1/chat/completions"]
        final_state = json.loads(chats[-1]["messages"][1]["content"])
        self.assertEqual(server.turn, 50)
        self.assertEqual(final_state["observations"][-1]["source_turn"], 50)
        self.assertEqual(len(chats), 51)

    def test_remaining_server_turns_and_near_deadline_go_directly_to_record(self):
        for turn, seconds in ((49, 1200), (0, 170)):
            server = Server(([step("EXAM", "복부 청진하겠습니다")] if turn else []) + [record()], turn=turn)
            server.deadline = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
            self.execute(server)
            self.assertEqual(server.turn, 50 if turn else 0)
            chats = [b for p, b in server.requests if p == "/v1/chat/completions"]
            self.assertEqual(len(chats), 2 if turn else 1)

    def test_malformed_record_falls_back_to_received_evidence(self):
        server = Server([step("SAY", "언제 시작됐나요?"), step("DIAGNOSE", "미확정"), "{}", "{}", "{}"])
        _, logs = self.execute(server)
        submitted = logs[-1]
        self.assertTrue(any(l["event"] == "record_fallback" for l in logs))
        self.assertIn("실제로 받은 원문", submitted["note"]["S"]["hpi"])
        self.assertEqual(submitted["note"]["O"]["tests"], "")
        self.assertEqual(submitted["record_status"], "fallback")

    def test_c10_missing_results_loop_moves_to_model_record(self):
        # Actual C10 turns 16-18: paraphrases bypassed exact action deduplication.
        questions = ["TSH 수치 알려주세요", "TSH와 free T4 수치를 알려주실 수 있나요?",
                     "TSH·free T4 결과 알려주세요."]
        answers = ["아니에요, 아직 없어요", "아직 없어요", "아직 없어요."]
        expected = json.loads(record())
        expected["diagnosis"] = "모델이 판단한 테스트용 진단"
        expected["note"]["A"]["evidence"] = "확보한 관찰에 대한 모델의 판단; 검사 미확인"
        expected["note"]["P"]["tests"] = "모델이 제안한 추가 검사 계획"
        server = Server([step("SAY", q) for q in questions] + [json.dumps(expected)], observations=answers)
        _, logs = self.execute(server)
        self.assertEqual(server.turn, 3)
        self.assertEqual(logs[-1]["diagnosis"], expected["diagnosis"])
        self.assertEqual(logs[-1]["note"], expected["note"])
        self.assertEqual(logs[-1]["stop_reason"], "unavailable_test_results")
        self.assertEqual(logs[-1]["record_status"], "model_generated")
        chats = [b for p, b in server.requests if p == "/v1/chat/completions"]
        payload = json.loads(chats[-1]["messages"][1]["content"])
        self.assertEqual([o["value"] for o in payload["observations"]], answers)
        self.assertEqual(payload["proposed_diagnosis"], "")
        self.assertNotIn("actions_taken", payload)

    def test_negative_symptoms_or_received_test_values_do_not_trigger_stall(self):
        scenarios = [(["발열이 있나요?", "기침이 있나요?", "두통이 있나요?"], ["없어요"] * 3),
                     (["검사 결과가 있나요?", "검사 수치 알려주세요", "검사 언제 받았나요?"],
                      ["없어요", "값은 10입니다", "어제 받았어요"])]
        for questions, answers in scenarios:
            with self.subTest(questions=questions):
                server = Server([step("SAY", q) for q in questions] +
                                [step("DIAGNOSE", "미확정"), record()], observations=answers)
                _, logs = self.execute(server)
                self.assertEqual(logs[-1]["stop_reason"], "model_diagnosis")

    def test_specific_validation_feedback_reaches_next_decision(self):
        server = Server([step("SAY", "가" * 31), "{", step("SAY", "언제 시작됐나요?"),
                         step("SAY", "언제 시작됐나요?"), step("DIAGNOSE", "미확정"), record()])
        _, logs = self.execute(server)
        errors = [l["detail"] for l in logs if l["event"] == "decision_error"]
        self.assertEqual([e["code"] for e in errors],
                         ["action_rule_violation", "invalid_json", "duplicate_action"])
        chats = [b for p, b in server.requests if p == "/v1/chat/completions"]
        for index, detail in zip((1, 2, 4), errors):
            self.assertEqual(json.loads(chats[index]["messages"][1]["content"])["last_error_detail"], detail)
        self.assertIn("30자", errors[0]["message"])
        self.assertEqual(chats[2]["max_tokens"], 8192)
        self.assertEqual(server.turn, 1)

    def test_empty_record_response_retries_with_more_tokens_and_preserves_diagnostics(self):
        server = Server([step("DIAGNOSE", "미확정"), "", record()])
        original_open = server.open

        def open_response(request):
            reply = original_open(request)
            payload = json.loads(reply.payload)
            if request.full_url.endswith("/chat/completions") and not payload["choices"][0]["message"]["content"]:
                payload["choices"][0]["finish_reason"] = "length"
                payload["choices"][0]["message"]["reasoning_content"] = "test reasoning"
                return HTTPReply(payload)
            return reply

        server.open = open_response
        _, logs = self.execute(server)
        chats = [b for p, b in server.requests if p == "/v1/chat/completions"]
        self.assertEqual([b["max_tokens"] for b in chats], [4096, 8192, 16384])
        self.assertNotEqual(chats[1]["request_id"], chats[2]["request_id"])
        self.assertEqual(logs[-1]["record_status"], "model_generated")
        self.assertEqual(logs[-1]["output_tokens"], 60)  # Empty replies also cost tokens.
        self.assertTrue(logs[-1]["usage_complete"])
        diagnostic = next(l for l in logs if l.get("finish_reason") == "length")
        self.assertEqual(diagnostic["content_chars"], 0)
        self.assertGreater(diagnostic["reasoning_chars"], 0)
        self.assertNotIn("test reasoning", json.dumps(logs))

    def test_persistent_empty_record_is_bounded_and_marked_fallback(self):
        server = Server([step("SAY", "언제 시작됐나요?"), step("DIAGNOSE", "미확정")] + [""] * 4)
        _, logs = self.execute(server)
        chats = [b for p, b in server.requests if p == "/v1/chat/completions"]
        self.assertEqual(len(chats), 6)
        self.assertEqual([b["max_tokens"] for b in chats[-4:]], [8192, 16384, 16384, 16384])
        self.assertEqual(logs[-1]["record_status"], "fallback")
        self.assertIn("실제로 받은 원문", logs[-1]["note"]["S"]["hpi"])

    def test_record_format_feedback_and_retry_preserve_evidence(self):
        server = Server([step("SAY", "언제 시작됐나요?"), step("DIAGNOSE", "미확정"), "{}", record()])
        _, logs = self.execute(server)
        chats = [b for p, b in server.requests if p == "/v1/chat/completions"]
        self.assertIn("Expected note and diagnosis", chats[-1]["messages"][0]["content"])
        self.assertEqual(chats[-1]["messages"][1], chats[-2]["messages"][1])
        self.assertEqual(logs[-1]["record_status"], "model_generated")

    def test_three_decision_errors_still_generate_record_without_inventing_diagnosis(self):
        server = Server([step("SAY", "언제 시작됐나요?"), "{}", "{}", "{}", record()])
        _, logs = self.execute(server)
        self.assertEqual(server.turn, 1)
        self.assertEqual(logs[-1]["stop_reason"], "decision_errors")
        self.assertEqual(logs[-1]["record_status"], "model_generated")

    def test_no_success_claim_without_completed_receipt(self):
        server = Server([step("DIAGNOSE", "미확정"), record()])
        server.receipt_status = "active"
        with self.assertRaisesRegex(NovaError, "SUBMISSION_NOT_CONFIRMED"):
            self.execute(server)

    def test_submission_read_timeout_replays_identical_request(self):
        server = Server([step("DIAGNOSE", "미확정"), record()])
        original_open = server.open
        lost = []

        class LostReply:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, limit):
                raise TimeoutError("response interrupted after server commit")

        def open_response(request):
            result = original_open(request)
            body = json.loads(request.data) if request.data else {}
            if body.get("action") == "DIAGNOSE" and not lost:
                lost.append(True)
                return LostReply()
            return result

        server.open = open_response
        result, logs = self.execute(server)
        submissions = [b for p, b in server.requests if b and b.get("action") == "DIAGNOSE"]
        self.assertEqual(len(submissions), 2)
        self.assertEqual(submissions[0], submissions[1])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(logs[-1]["event"], "submitted")

    def test_missing_usage_is_marked_unknown_without_losing_valid_answer(self):
        server = Server([step("DIAGNOSE", "미확정"), record()])
        original_open = server.open

        def open_response(request):
            result = original_open(request)
            if request.full_url.endswith("/chat/completions"):
                payload = json.loads(result.payload)
                payload["usage"] = None
                return HTTPReply(payload)
            return result

        server.open = open_response
        result, logs = self.execute(server)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(logs[-1]["usage_complete"])
        self.assertEqual(logs[-1]["doctor_responses"], 2)

    def test_invalid_action_response_finishes_with_only_prior_verified_evidence(self):
        server = Server([step("SAY", "언제 시작됐나요?"), step("EXAM", "복부 청진하겠습니다"), record()])
        original_open = server.open

        def open_response(request):
            result = original_open(request)
            body = json.loads(request.data) if request.data else {}
            if body.get("action") == "EXAM":
                return HTTPReply({"turn": 2, "turns_remaining": 48, "result": ""})
            return result

        server.open = open_response
        result, logs = self.execute(server)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any(l.get("code") == "INVALID_API_RESPONSE" for l in logs))
        chats = [b for p, b in server.requests if p == "/v1/chat/completions"]
        final_state = json.loads(chats[-1]["messages"][1]["content"])
        self.assertEqual(len(final_state["observations"]), 1)
        self.assertEqual(final_state["observations"][0]["value"], "실제로 받은 원문")

    def test_nfc_and_exam_limits_and_official_record_fields(self):
        Action(ActionType.SAY, "\u1100\u1161" * 30).validate_for_stage("preliminary")
        for kind in (ActionType.SAY, ActionType.EXAM, ActionType.TEST):
            with self.assertRaises(ValueError):
                Action(kind, "가" * 31).validate_for_stage("final")
        invalid = json.loads(record())
        invalid["note"]["A"]["diagnosis"] = "extra field"
        with self.assertRaises(ValueError):
            parse_record(json.dumps(invalid))
        invalid = json.loads(record())
        invalid["note"]["O"]["tests"] = "unperformed test"
        with self.assertRaises(ValueError):
            parse_record(json.dumps(invalid))

    def test_official_entry_uses_ready_handshake_and_temp_output(self):
        server = Server([step("DIAGNOSE", "미확정"), record()])

        def entry(client, prepared):
            with patch("nova_sdk.Client", return_value=client), \
                    patch("sys.argv", ["run.py"]), contextlib.redirect_stdout(io.StringIO()) as output, \
                    patch.dict(os.environ, {"NOVA_API_BASE": "http://sdk.test", "NOVA_RUN_TOKEN": "hidden"}):
                run.main()
                return json.loads(output.getvalue())

        result, _ = self.execute(server, entry)
        self.assertEqual([p for p, b in server.requests[:2]], ["/_nova/ready", "/v1/session"])
        self.assertEqual(result["status"], "completed")

    def test_sample_list_does_not_start_or_charge_a_case(self):
        server = Server([])
        client = Client(api_base="http://sdk.test", token="hidden", sample=True)
        with patch("sample.Client", return_value=client), patch.object(client, "_open", side_effect=server.open), \
                patch("sys.argv", ["sample.py", "--list"]), patch.dict(os.environ, {"NOVA_DEV_KEY": "hidden"}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            sample.main()
        self.assertEqual([p for p, b in server.requests], ["/v1/sample/cases"])
        self.assertEqual(json.loads(output.getvalue())["cases"][0]["id"], "case-id")


if __name__ == "__main__":
    unittest.main()
