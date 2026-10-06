import json
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from itertools import count
from pathlib import Path
from threading import current_thread
from unittest.mock import patch

from run import clear_interactive_screen
from src.agent import Agent
from src.llm import StructuredLLMAdapter
from src.state import Observation


ROOT = Path(__file__).resolve().parents[1]


class ConsoleTest(unittest.TestCase):
    def run_console(self, directory, text, *extra):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(ROOT / "run.py"),
             "--interactive", "--output-dir", directory, *extra],
            input=text, capture_output=True, encoding="utf-8", timeout=10, cwd=ROOT,
        )

    def test_interactive_input_validation_and_observation_round_trip(self):
        ask = json.dumps({"action": {"type": "SAY", "content": "언제 시작됐나요?"},
                          "rationale": "시점 확인", "differential": []}, ensure_ascii=False, indent=2)
        diagnose = json.dumps({"action": {"type": "DIAGNOSE", "content": "미확정"},
                               "rationale": "테스트 종료", "differential": [], "plan": "추가 평가 계획"}, ensure_ascii=False)
        inputs = "\n".join(["배가 아파요", "24", "bad JSON", "", ask, "",
                            "어제부터 아팠어요", "positive", diagnose, "", ""])
        with tempfile.TemporaryDirectory() as directory:
            process = self.run_console(directory, inputs)
            self.assertEqual(process.returncode, 0, process.stderr)
            records = [json.loads(line) for line in next(Path(directory).glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
            summary = json.loads(next(Path(directory).glob("*-summary.json")).read_text(encoding="utf-8"))
        self.assertEqual(records[0]["error"], "invalid_decision")
        self.assertEqual(records[0]["error_detail"]["code"], "invalid_json")
        self.assertIn("수정 안내: response - JSON 문법 오류", process.stdout)
        self.assertEqual(records[1]["observation"]["value"], "어제부터 아팠어요")
        self.assertEqual(summary["mode"], "manual")
        self.assertEqual(summary["state"]["chief_complaint"], "배가 아파요")
        self.assertEqual(summary["state"]["final_diagnosis"], "미확정")
        self.assertEqual(summary["format_errors"], 1)
        self.assertFalse(summary["forced_diagnosis"])
        self.assertEqual(summary["state"]["observations"][0]["value"], "어제부터 아팠어요")
        self.assertNotIn("[SYSTEM 프롬프트]", process.stdout)
        self.assertNotIn("[USER 프롬프트]", process.stdout)
        self.assertNotIn('"observations":', process.stdout)

    def test_eof_or_quit_stops_without_fabricating_diagnosis(self):
        for ending in ("", "/quit\n"):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as directory:
                process = self.run_console(directory, "테스트 증상\n\n" + ending)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertIn("중단했습니다", process.stdout)
                self.assertEqual(list(Path(directory).glob("*-summary.json")), [])
                self.assertEqual(next(Path(directory).glob("*.jsonl")).read_text(encoding="utf-8"), "")

    def test_blank_input_and_saved_history_do_not_end_or_contaminate_new_session(self):
        diagnose = json.dumps({"action": {"type": "DIAGNOSE", "content": "사용자 입력 진단"},
                               "rationale": "직접 종료", "differential": [], "plan": "테스트 계획"})
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "old.jsonl").write_text("OLD_RUN_SECRET", encoding="utf-8")
            process = self.run_console(directory, "새 증상\n25\n\n\n" + diagnose + "\n\n")
            summary = json.loads(next(Path(directory).glob("*-summary.json")).read_text(encoding="utf-8"))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn("OLD_RUN_SECRET", process.stdout + json.dumps(summary))
        self.assertEqual(summary["format_errors"], 0)
        self.assertEqual(summary["state"]["attempt"], 1)
        self.assertEqual(summary["state"]["final_diagnosis"], "사용자 입력 진단")
        self.assertFalse(summary["forced_diagnosis"])

    def test_full_prompts_are_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            process = self.run_console(directory, "현재 증상\n25\n/quit\n", "--show-prompts")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("[SYSTEM 프롬프트]", process.stdout)
        self.assertIn("[USER 프롬프트]", process.stdout)
        self.assertIn('"observations": []', process.stdout)

    def test_manual_wait_ignores_clock_and_attempt_limit_but_keeps_turn_limit(self):
        owner = current_thread()
        messages_seen = []

        def response(kind):
            return json.dumps({"action": {"type": kind, "content": "사용자 입력"},
                               "rationale": "검증", "differential": [],
                               "plan": "테스트 계획" if kind == "DIAGNOSE" else ""})

        replies = iter([response("SAY"), "bad JSON", response("EXAM"), response("DIAGNOSE")])

        def complete(messages):
            self.assertIs(current_thread(), owner)
            messages_seen.append(json.loads(messages[1]["content"]))
            return next(replies)

        observations = []

        def observe(action, turn):
            self.assertIs(current_thread(), owner)
            observations.append(turn)
            return Observation("입력된 관찰", "unknown", turn, action)

        class Log:
            def write(self, record):
                pass

        with patch("src.agent.perf_counter", side_effect=count(0, 5000)), patch("src.agent.bounded_call") as bounded:
            result = Agent(StructuredLLMAdapter(complete), max_turns=1, max_attempts=1,
                           manual_input=True).run("manual", {}, "검증", observe, Log())
            bounded.assert_not_called()
        self.assertEqual(observations, [1])
        self.assertEqual(result.state.turn, 1)
        self.assertEqual(result.state.attempt, 4)
        self.assertEqual(result.format_errors, 2)
        self.assertTrue(all(p["remaining_seconds"] is None for p in messages_seen))
        self.assertEqual(result.state.final_diagnosis, "사용자 입력")
        self.assertFalse(result.forced_diagnosis)

    def test_interactive_start_clears_terminal_scrollback_only_for_tty(self):
        for is_terminal in (True, False):
            with self.subTest(is_terminal=is_terminal):
                output = StringIO()
                with patch("run.sys.stdout", output), patch.object(output, "isatty", return_value=is_terminal):
                    clear_interactive_screen()
                self.assertEqual(output.getvalue(), "\033[2J\033[3J\033[H" if is_terminal else "")


if __name__ == "__main__":
    unittest.main()
