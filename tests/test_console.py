import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ConsoleTest(unittest.TestCase):
    def run_console(self, directory, text):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(ROOT / "run.py"),
             "--interactive", "--output-dir", directory],
            input=text, capture_output=True, encoding="utf-8", timeout=10, cwd=ROOT,
        )

    def test_interactive_input_validation_and_observation_round_trip(self):
        ask = json.dumps({"action": {"type": "ASK", "content": "언제 시작됐나요?"},
                          "rationale": "시점 확인", "differential": []}, ensure_ascii=False, indent=2)
        diagnose = json.dumps({"action": {"type": "DIAGNOSE", "content": "미확정"},
                               "rationale": "테스트 종료", "differential": []}, ensure_ascii=False)
        inputs = "\n".join(["배가 아파요", "24", "bad JSON", "", ask, "",
                            "어제부터 아팠어요", "positive", diagnose, "", ""])
        with tempfile.TemporaryDirectory() as directory:
            process = self.run_console(directory, inputs)
            self.assertEqual(process.returncode, 0, process.stderr)
            records = [json.loads(line) for line in next(Path(directory).glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
            summary = json.loads(next(Path(directory).glob("*-summary.json")).read_text(encoding="utf-8"))
        self.assertEqual(records[0]["error"], "invalid_decision")
        self.assertEqual(records[1]["observation"]["value"], "어제부터 아팠어요")
        self.assertEqual(summary["mode"], "manual")
        self.assertEqual(summary["state"]["chief_complaint"], "배가 아파요")
        self.assertEqual(summary["state"]["final_diagnosis"], "미확정")
        self.assertEqual(summary["format_errors"], 1)
        self.assertIn("어제부터 아팠어요", process.stdout.split("[USER 프롬프트 / 턴 3]")[1])

    def test_eof_or_quit_stops_without_fabricating_diagnosis(self):
        for ending in ("", "/quit\n"):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as directory:
                process = self.run_console(directory, "테스트 증상\n\n" + ending)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertIn("중단했습니다", process.stdout)
                self.assertEqual(list(Path(directory).glob("*-summary.json")), [])
                self.assertEqual(next(Path(directory).glob("*.jsonl")).read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
