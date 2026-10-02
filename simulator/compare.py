"""Compare local baseline summaries; never use this report inside inference."""

import argparse
import json
import math
import sys
from pathlib import Path


COUNTS = ("ask_count", "exam_count", "test_count", "duplicate_actions",
          "format_errors", "execution_failures")
METRICS = (
    ("accuracy", "정확도", True),
    ("mean_turns", "평균 턴", False),
    ("mean_actions", "평균 성공 정보 획득 행동", False),
    ("error_turn_rate", "오류 턴 비율", True),
    ("format_error_rate", "형식 오류/턴", True),
    ("execution_failure_rate", "실행 실패/턴", True),
    ("duplicate_turn_rate", "중복 행동/턴", True),
    ("forced_diagnosis_rate", "강제 종료 증례 비율", True),
    ("mean_elapsed_ms", "평균 실행 시간(ms)", False),
)


def load_summary(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("mode") == "manual":
        raise ValueError("기본 실행의 -summary.json 파일이 필요합니다. 수동 실행·JSONL은 비교 대상이 아닙니다.")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases에 하나 이상의 평가 결과가 필요합니다.")
    if "case_count" in data and (type(data["case_count"]) is not int or data["case_count"] != len(cases)):
        raise ValueError("case_count와 실제 증례 수가 다릅니다.")
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str) or not case["case_id"].strip():
            raise ValueError("증례마다 유효한 case_id가 필요합니다.")
        if case["case_id"] in ids:
            raise ValueError("중복 case_id가 있습니다.")
        ids.add(case["case_id"])
        if type(case.get("correct")) is not bool:
            raise ValueError("correct는 참/거짓이어야 합니다.")
        if not isinstance(case.get("diagnosis"), str):
            raise ValueError("diagnosis는 문자열이어야 합니다.")
        if type(case.get("turns")) is not int or case["turns"] < 1:
            raise ValueError("turns는 양의 정수여야 합니다.")
        for key in COUNTS:
            if type(case.get(key)) is not int or not 0 <= case[key] <= case["turns"]:
                raise ValueError(key + "는 턴 수 이하의 음이 아닌 정수여야 합니다.")
        errors = sum(case[key] for key in COUNTS[3:])
        if errors > case["turns"]:
            raise ValueError("오류 수의 합이 턴 수를 초과합니다.")
        elapsed = case.get("elapsed_ms")
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("elapsed_ms는 유한한 음이 아닌 수여야 합니다.")
        if "forced_diagnosis" in case and type(case["forced_diagnosis"]) is not bool:
            raise ValueError("forced_diagnosis는 참/거짓이어야 합니다.")
    return data


def summarize(data):
    cases = data["cases"]
    count = len(cases)
    turns = sum(case["turns"] for case in cases)
    totals = {key: sum(case[key] for case in cases) for key in COUNTS}
    return {
        "case_count": count,
        "accuracy": sum(case["correct"] for case in cases) / count,
        "mean_turns": turns / count,
        "mean_actions": sum(totals[key] for key in COUNTS[:3]) / count,
        "error_turn_rate": sum(totals[key] for key in COUNTS[3:]) / turns,
        "format_error_rate": totals["format_errors"] / turns,
        "execution_failure_rate": totals["execution_failures"] / turns,
        "duplicate_turn_rate": totals["duplicate_actions"] / turns,
        "forced_diagnosis_rate": (sum(case["forced_diagnosis"] for case in cases) / count
                                  if all("forced_diagnosis" in case for case in cases) else None),
        "mean_elapsed_ms": sum(case["elapsed_ms"] for case in cases) / count,
        "failure_cases": [
            {"case_id": case["case_id"], "diagnosis": case["diagnosis"], "correct": case["correct"],
             "errors": {key: case[key] for key in COUNTS[3:]},
             "forced_diagnosis": case.get("forced_diagnosis")}
            for case in cases if not case["correct"] or any(case[key] for key in COUNTS[3:])
            or case.get("forced_diagnosis", False)
        ],
    }


def compare(before_path, after_path):
    before, after = load_summary(before_path), load_summary(after_path)
    if {c["case_id"] for c in before["cases"]} != {c["case_id"] for c in after["cases"]}:
        raise ValueError("증례 ID 구성이 다릅니다. 같은 증례를 실행한 결과를 비교하세요.")
    warnings = []
    if before.get("cases_sha256") and after.get("cases_sha256"):
        if before["cases_sha256"] != after["cases_sha256"]:
            raise ValueError("증례 파일 내용이 다릅니다. 같은 파일로 다시 실행하세요.")
    else:
        warnings.append("이전 형식 파일에는 증례 해시가 없어 ID 일치만 확인했습니다. 증례 내용은 직접 확인하세요.")
    left, right = summarize(before), summarize(after)
    if left["forced_diagnosis_rate"] is None or right["forced_diagnosis_rate"] is None:
        warnings.append("강제 종료 기록이 없는 결과는 미상으로 표시합니다.")
    return {
        "before_file": str(before_path), "after_file": str(after_path), "warnings": warnings,
        "before": left, "after": right,
        "delta": {key: right[key] - left[key] if left[key] is not None and right[key] is not None else None
                  for key, _, _ in METRICS},
    }


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="같은 증례의 로컬 기본 실행 결과 비교")
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--json", action="store_true", help="분석 결과를 JSON으로 출력")
    args = parser.parse_args()
    try:
        report = compare(args.before, args.after)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return
    print("증례 {}개 비교 | 변화 = 이후 - 이전 | 비율 변화는 퍼센트포인트(pp)".format(report["before"]["case_count"]))
    for warning in report["warnings"]:
        print("참고:", warning)
    print("지표 | 이전 | 이후 | 변화")
    for key, label, percent in METRICS:
        values = []
        for column in ("before", "after", "delta"):
            value = report[column][key]
            if value is None:
                values.append("미상")
            elif percent:
                values.append(format(value * 100, "+.2f" if column == "delta" else ".2f") +
                              ("pp" if column == "delta" else "%"))
            else:
                values.append(format(value, "+.3f" if column == "delta" else ".3f"))
        print(label + " | " + " | ".join(values))
    for column, label in (("before", "이전"), ("after", "이후")):
        print(label + " 실패·오류·강제 종료 증례:")
        print(json.dumps(report[column]["failure_cases"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
