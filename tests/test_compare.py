import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from simulator.compare import compare, load_summary


ROOT = Path(__file__).resolve().parents[1]


def sample():
    return {
        "mode": "baseline", "cases_sha256": "same-data", "case_count": 2,
        "cases": [
            {"case_id": "a", "diagnosis": "후보 A", "correct": True, "turns": 4,
             "ask_count": 1, "exam_count": 1, "test_count": 1, "duplicate_actions": 0,
             "format_errors": 0, "execution_failures": 0, "elapsed_ms": 10, "forced_diagnosis": False},
            {"case_id": "b", "diagnosis": "미확정", "correct": False, "turns": 4,
             "ask_count": 1, "exam_count": 0, "test_count": 0, "duplicate_actions": 1,
             "format_errors": 1, "execution_failures": 0, "elapsed_ms": 30, "forced_diagnosis": True},
        ],
    }


class CompareTest(unittest.TestCase):
    def write_summary(self, directory, name, data):
        path = Path(directory) / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_metrics_deltas_and_failures_match_hand_calculation(self):
        before = sample()
        after = copy.deepcopy(before)
        after["cases"][1].update(correct=True, turns=2, diagnosis="후보 B", duplicate_actions=0,
                                 format_errors=0, forced_diagnosis=False, elapsed_ms=10)
        after["cases"].reverse()  # Alignment uses IDs, not row order.
        with tempfile.TemporaryDirectory() as directory:
            report = compare(self.write_summary(directory, "before.json", before),
                             self.write_summary(directory, "after.json", after))
        self.assertEqual(report["before"]["accuracy"], 0.5)
        self.assertEqual(report["before"]["error_turn_rate"], 2 / 8)
        self.assertEqual(report["before"]["mean_actions"], 2)
        self.assertEqual(report["delta"]["accuracy"], 0.5)
        self.assertEqual(report["delta"]["mean_turns"], -1)
        self.assertEqual(report["delta"]["forced_diagnosis_rate"], -0.5)
        self.assertEqual(report["delta"]["mean_elapsed_ms"], -10)
        self.assertEqual([c["case_id"] for c in report["before"]["failure_cases"]], ["b"])
        self.assertEqual(report["after"]["failure_cases"], [])

    def test_legacy_missing_fields_remain_unknown(self):
        before, after = sample(), sample()
        del before["cases_sha256"]
        for case in before["cases"]:
            del case["forced_diagnosis"]
        with tempfile.TemporaryDirectory() as directory:
            report = compare(self.write_summary(directory, "before.json", before),
                             self.write_summary(directory, "after.json", after))
        self.assertIsNone(report["before"]["forced_diagnosis_rate"])
        self.assertIsNone(report["delta"]["forced_diagnosis_rate"])
        self.assertEqual(len(report["warnings"]), 2)

    def test_mismatched_cases_or_source_data_are_rejected(self):
        for change in ("case_id", "hash"):
            before, after = sample(), sample()
            if change == "case_id":
                after["cases"][0]["case_id"] = "other"
            else:
                after["cases_sha256"] = "changed-data"
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                left = self.write_summary(directory, "before.json", before)
                right = self.write_summary(directory, "after.json", after)
                with self.assertRaises(ValueError):
                    compare(left, right)

    def test_invalid_summaries_are_rejected(self):
        invalid = [{"mode": "manual", "state": {}}, {"cases": []}, []]
        for field, value in [("correct", 1), ("turns", 0), ("format_errors", -1),
                             ("execution_failures", 99), ("elapsed_ms", float("nan")),
                             ("forced_diagnosis", "false")]:
            data = sample()
            data["cases"][0][field] = value
            invalid.append(data)
        data = sample()
        data["cases"][1]["case_id"] = "a"
        invalid.append(data)
        for data in invalid:
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                path = self.write_summary(directory, "invalid.json", data)
                with self.assertRaises(ValueError):
                    load_summary(path)

    def test_cli_prints_percent_point_deltas_and_machine_readable_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_summary(directory, "summary.json", sample())
            command = [sys.executable, "-X", "utf8", "-m", "simulator.compare", str(path), str(path)]
            human = subprocess.run(command, capture_output=True, encoding="utf-8", cwd=ROOT, timeout=10)
            machine = subprocess.run(command + ["--json"], capture_output=True, encoding="utf-8", cwd=ROOT, timeout=10)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("+0.00pp", human.stdout)
        self.assertIn("강제 종료 증례 비율", human.stdout)
        self.assertEqual(machine.returncode, 0, machine.stderr)
        self.assertEqual(json.loads(machine.stdout)["delta"]["accuracy"], 0)

    def test_actual_baseline_output_can_be_compared(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "run.py"),
                                      "--output-dir", directory], capture_output=True, encoding="utf-8", timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            path = next(Path(directory).glob("*-summary.json"))
            report = compare(path, path)
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["after"]["accuracy"], 1)
        self.assertEqual(report["after"]["forced_diagnosis_rate"], 0)
        self.assertEqual(report["after"]["error_turn_rate"], 0)


if __name__ == "__main__":
    unittest.main()
