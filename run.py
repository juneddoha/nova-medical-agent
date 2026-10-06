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
from src.connection import ConnectionSettings, ModelReply, PreparedAPIAdapter
from src.llm import MockLLMAdapter, StructuredLLMAdapter
from src.state import Observation, PatientState, VALID_STATUSES
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


def clear_interactive_screen():
    if sys.stdout.isatty():
        # Clear visible output and restored scrollback, not saved run files or shell command history.
        print("\033[2J\033[3J\033[H", end="", flush=True)


def interactive(output_dir, stage="preliminary", show_prompts=False):
    clear_interactive_screen()
    print("수동 연결 테스트입니다. 실제 LLM 호출이나 자동 진단은 수행하지 않습니다.")
    print("새 테스트를 시작합니다. 이전 실행 기록은 불러오지 않습니다.")
    print("입력 대기 시간 제한은 없습니다. 직접 DIAGNOSE를 입력해야 진단을 확정합니다.")
    print("전체 프롬프트 보기: --show-prompts")
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
            if first_prompt and show_prompts:
                print("\n[SYSTEM 프롬프트]\n" + messages[0]["content"])
                first_prompt = False
            payload = json.loads(messages[1]["content"])
            print("\n[판단 {} | 사용한 진료 턴 {}/{}]".format(
                payload["attempt"], payload["turn"], payload["max_turns"]))
            if show_prompts:
                print("[USER 프롬프트]")
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            if payload["final_turn"]:
                print("진료 50턴에 도달했습니다. 추가 행동은 할 수 없습니다. DIAGNOSE를 입력하거나 /quit으로 중단하세요.")
            if payload["last_error"]:
                print("이전 오류:", payload["last_error"])
                if payload["last_error_detail"]:
                    detail = payload["last_error_detail"]
                    print("수정 안내:", detail["field"] or "response", "-", detail["message"])
            print("모델 응답 JSON을 직접 입력/붙여넣기하고, 빈 줄로 제출하세요.")
            print('예: {"action":{"type":"SAY","content":"언제 시작됐나요?"},'
                  '"rationale":"시작 시점 확인","differential":[]}')
            print("종료 시 DIAGNOSE와 주진단 1개, plan(검사·치료·처치·추적·교육 계획)을 입력하세요.")
            lines = []
            while True:
                line = read_input("JSON> " if not lines else "... ")
                if not line.strip():
                    if lines:
                        return "\n".join(lines)
                    continue
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
            result = Agent(StructuredLLMAdapter(complete), stage=stage, manual_input=True).run(
                run_id, {"age": age} if age else {}, complaint, observe, trajectory,
            )
        summary = {
            "mode": "manual", "state": result.state.to_dict(),
            "duplicate_actions": result.duplicate_actions,
            "format_errors": result.format_errors,
            "execution_failures": result.execution_failures,
            "forced_diagnosis": result.forced_diagnosis,
            "competition_model_calls": result.competition_model_calls,
            "official_submission_sent": False,
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
    mode.add_argument("--check-connection", action="store_true", help="Check configuration and exercise an offline stub; no network requests")
    parser.add_argument("--stage", choices=("preliminary", "final"), default="preliminary")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runs")
    parser.add_argument("--show-prompts", action="store_true", help="Show full prompts in the manual interactive mode")
    args = parser.parse_args()

    if args.check_connection:
        settings = ConnectionSettings.from_env()
        raw = json.dumps({"action": {"type": "DIAGNOSE", "content": "미확정"},
                          "rationale": "오프라인 연결 점검", "differential": [],
                          "plan": "연결 점검용 응답이며 임상 계획이 아닙니다."})

        def stub(messages, **kwargs):
            return ModelReply(raw, 1, 1)

        adapter = PreparedAPIAdapter(stub, lambda messages, model: 1)
        decision = adapter.next_action(PatientState("connection-check", {}, "로컬 점검"))
        print(json.dumps({"mode": "offline_stub", "model": settings.model,
                          "endpoint_configured": bool(settings.endpoint),
                          "api_key_configured": bool(settings.api_key),
                          "official_transport_implemented": False,
                          "real_api_verified": False, "network_requests": 0,
                          "parsed_action": decision.action.type.value,
                          "stub_usage": adapter.usage()}, ensure_ascii=False, indent=2))
        return

    if args.interactive:
        interactive(args.output_dir, args.stage, args.show_prompts)
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
            result = Agent(MockLLMAdapter(), stage=args.stage).run(
                case.case_id,
                case.demographics,
                case.chief_complaint,
                environment.observe,
                trajectory,
                vital_signs=case.vital_signs,
            )
            metrics.append(evaluate(case, result))

    summary = {
        "mode": "baseline",
        "schema_version": 2,
        "stage": args.stage,
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
