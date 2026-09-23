"""Run the local synthetic baseline: python run.py"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from simulator.environment import LocalEnvironment, load_cases
from simulator.evaluator import evaluate
from src.agent import Agent
from src.llm import MockLLMAdapter
from src.trajectory import TrajectoryWriter


ROOT = Path(__file__).resolve().parent


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="N.O.V.A. local mock baseline")
    parser.add_argument("--cases", type=Path, default=ROOT / "simulator" / "sample_cases.json")
    parser.add_argument("--case", help="Run one case_id instead of all cases")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runs")
    args = parser.parse_args()

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
        "adapter": "MockLLMAdapter",
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
