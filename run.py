"""Run the local synthetic baseline: python run.py"""

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from simulator.environment import LocalEnvironment, load_cases
from simulator.evaluator import evaluate
from src.agent import Agent
from src.llm import MockLLMAdapter, StructuredLLMAdapter
from src.state import Observation, VALID_STATUSES
from src.trajectory import TrajectoryWriter


ROOT = Path(__file__).resolve().parent


def read_input(prompt):
    try:
        value = input(prompt)
    except EOFError:
        raise KeyboardInterrupt() from None
    if value.strip().lower() == "/quit":
        raise KeyboardInterrupt()
    return value


def interactive(output_dir):
    print("수동 연결 테스트입니다. 실제 LLM 호출이나 자동 진단은 수행하지 않습니다.")
    print("환자 정보 → 생성된 프롬프트 확인 → 모델 응답을 대신 입력 → 관찰 입력 순서입니다.")
    print("종료: /quit 또는 Ctrl+C")
    trajectory_path = None
    try:
        complaint = read_input("주증상/상황을 입력하세요: ").strip()
        while not complaint:
            complaint = read_input("주증상을 한 줄 이상 입력하세요: ").strip()
        age = read_input("나이 (생략 가능): ").strip()
        run_id = "manual-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
        trajectory_path = output_dir / (run_id + ".jsonl")
        summary_path = output_dir / (run_id + "-summary.json")
        first_prompt = True

        def complete(messages):
            nonlocal first_prompt
            if first_prompt:
                print("\n[SYSTEM 프롬프트]\n" + messages[0]["content"])
                first_prompt = False
            payload = json.loads(messages[1]["content"])
            print("\n[USER 프롬프트 / 턴 " + str(payload["turn"]) + "]")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            if payload["last_error"]:
                print("이전 오류:", payload["last_error"])
                if payload["last_error_detail"]:
                    detail = payload["last_error_detail"]
                    print("수정 안내:", detail["field"] or "response", "-", detail["message"])
            print("모델 응답 JSON을 직접 입력/붙여넣기하고, 빈 줄로 제출하세요.")
            print('예: {"action":{"type":"ASK","content":"언제 시작됐나요?"},'
                  '"rationale":"시작 시점 확인","differential":[]}')
            print("종료 진단은 type을 DIAGNOSE, content를 진단명으로 입력하세요.")
            lines = []
            while True:
                line = read_input("JSON> " if not lines else "... ")
                if not line.strip():
                    return "\n".join(lines)
                lines.append(line)

        def observe(action, turn):
            print("\n선택된 행동:", action.type.value, action.content)
            value = read_input("환자 답변/관찰 결과: ").strip()
            while not value:
                value = read_input("관찰 결과를 입력하세요: ").strip()
            status = read_input("상태 [positive/negative/unknown/contradictory] (기본 unknown): ").strip() or "unknown"
            while status not in VALID_STATUSES:
                status = read_input("상태를 다시 입력하세요: ").strip() or "unknown"
            return Observation(value, status, turn, action)

        with TrajectoryWriter(trajectory_path) as trajectory:
            result = Agent(StructuredLLMAdapter(complete)).run(
                run_id, {"age": age} if age else {}, complaint, observe, trajectory,
            )
        summary = {
            "mode": "manual", "state": result.state.to_dict(),
            "duplicate_actions": result.duplicate_actions,
            "format_errors": result.format_errors,
            "execution_failures": result.execution_failures,
            "forced_diagnosis": result.forced_diagnosis,
        }
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("\n수동 테스트 종료. 제출된 진단:", result.state.final_diagnosis)
        print("summary:", summary_path)
    except KeyboardInterrupt:
        print("\n수동 테스트를 중단했습니다.")
    if trajectory_path is not None:
        print("trajectory:", trajectory_path)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="N.O.V.A. local mock baseline")
    parser.add_argument("--cases", type=Path, default=ROOT / "simulator" / "sample_cases.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--case", help="Run one case_id instead of all cases")
    mode.add_argument("--interactive", action="store_true", help="Enter patient data and simulated model replies in the terminal (no LLM)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runs")
    args = parser.parse_args()

    if args.interactive:
        interactive(args.output_dir)
        return

    cases = load_cases(args.cases)
    if args.case:
        cases = [case for case in cases if case.case_id == args.case]
        if not cases:
            parser.error("Unknown case_id: " + args.case)

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    trajectory_path = args.output_dir / (run_id + ".jsonl")
    summary_path = args.output_dir / (run_id + "-summary.json")
    metrics = []

    with TrajectoryWriter(trajectory_path) as trajectory:
        for case in cases:
            environment = LocalEnvironment(case)
            result = Agent(MockLLMAdapter()).run(
                case.case_id,
                case.demographics,
                case.chief_complaint,
                environment.observe,
                trajectory,
            )
            metrics.append(evaluate(case, result))

    summary = {
        "mode": "baseline",
        "adapter": "MockLLMAdapter",
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "cases": metrics,
        "case_count": len(metrics),
        "accuracy": sum(item["correct"] for item in metrics) / len(metrics) if metrics else None,
        "mean_turns": sum(item["turns"] for item in metrics) / len(metrics) if metrics else None,
        "mean_elapsed_ms": sum(item["elapsed_ms"] for item in metrics) / len(metrics) if metrics else None,
        "max_elapsed_ms": max((item["elapsed_ms"] for item in metrics), default=None),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("trajectory:", trajectory_path)
    print("summary:", summary_path)


if __name__ == "__main__":
    main()
