from dataclasses import dataclass
from time import perf_counter

from src.actions import Action, ActionType
from src.llm import Decision
from src.state import DifferentialCandidate, Observation, PatientState


@dataclass
class RunResult:
    state: PatientState
    duplicate_actions: int
    format_errors: int
    execution_failures: int
    elapsed_ms: float


class Agent:
    def __init__(self, adapter, max_turns=60):
        if not 1 <= max_turns <= 60:
            raise ValueError("max_turns must be between 1 and 60")
        self.adapter = adapter
        self.max_turns = max_turns

    def run(self, case_id, demographics, chief_complaint, observe, trajectory):
        state = PatientState(case_id, dict(demographics), chief_complaint)
        duplicate_actions = format_errors = execution_failures = 0
        started = perf_counter()

        for turn in range(1, self.max_turns + 1):
            state.turn = turn
            error = None
            if turn == self.max_turns:
                diagnosis = state.differential[0].name if state.differential else "미확정"
                decision = Decision(Action(ActionType.DIAGNOSE, diagnosis), "턴 제한에 따른 종료", state.differential)
            else:
                try:
                    decision = self.adapter.next_action(state)
                except Exception as exc:
                    execution_failures += 1
                    error = "adapter_error: " + type(exc).__name__
                    diagnosis = state.differential[0].name if state.differential else "미확정"
                    decision = Decision(Action(ActionType.DIAGNOSE, diagnosis), "모델 실패 후 종료", state.differential)

            try:
                if not isinstance(decision, Decision):
                    raise ValueError("Adapter must return Decision")
                action = decision.action
                action.validate()
                if not isinstance(decision.rationale, str):
                    raise ValueError("Invalid rationale")
                if not isinstance(decision.differential, list) or any(
                    not isinstance(item, DifferentialCandidate) or not item.name.strip()
                    for item in decision.differential
                ):
                    raise ValueError("Invalid differential")
            except (AttributeError, TypeError, ValueError):
                format_errors += 1
                trajectory.write({"case_id": case_id, "turn": turn, "action": None,
                                  "error": "invalid_decision", "rationale": ""})
                continue

            state.differential = list(decision.differential)
            if action.type != ActionType.DIAGNOSE and state.has_action(action):
                duplicate_actions += 1
                trajectory.write({"case_id": case_id, "turn": turn, "action": action.to_dict(),
                                  "error": "duplicate_action", "rationale": decision.rationale,
                                  "differential": [item.name for item in state.differential]})
                continue

            state.actions_taken.append(action)
            if action.type == ActionType.DIAGNOSE:
                state.final_diagnosis = action.content.strip()
                trajectory.write({"case_id": case_id, "turn": turn, "action": action.to_dict(),
                                  "observation": None, "rationale": decision.rationale,
                                  "differential": [item.name for item in state.differential],
                                  "error": error})
                break

            try:
                observation = observe(action, turn)
                if not isinstance(observation, Observation):
                    raise ValueError("Environment must return Observation")
                state.observations.append(observation)
            except Exception as exc:
                execution_failures += 1
                error = "environment_error: " + type(exc).__name__
                observation = None
            trajectory.write({"case_id": case_id, "turn": turn, "action": action.to_dict(),
                              "observation": observation.to_dict() if observation else None,
                              "rationale": decision.rationale,
                              "differential": [item.name for item in state.differential],
                              "error": error})

        return RunResult(state, duplicate_actions, format_errors, execution_failures,
                         (perf_counter() - started) * 1000)
