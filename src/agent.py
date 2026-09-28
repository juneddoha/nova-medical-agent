from dataclasses import asdict, dataclass
from time import perf_counter

from src.actions import Action, ActionType
from src.llm import Decision, InvalidDecision
from src.state import PatientState


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
        state = PatientState(case_id, dict(demographics), chief_complaint,
                             max_turns=self.max_turns)
        duplicate_actions = format_errors = execution_failures = 0
        started = perf_counter()

        for turn in range(1, self.max_turns + 1):
            state.turn = turn
            error = None
            decision = None
            try:
                decision = self.adapter.next_action(state)
            except InvalidDecision:
                format_errors += 1
                error = "invalid_decision"
            except Exception as exc:
                execution_failures += 1
                error = "adapter_error: " + type(exc).__name__

            if error is None:
                try:
                    if not isinstance(decision, Decision):
                        raise InvalidDecision("Adapter must return Decision")
                    decision.validate()
                except (AttributeError, TypeError, ValueError):
                    format_errors += 1
                    error = "invalid_decision"

            if error:
                state.last_error = error
            if error and turn < self.max_turns:
                trajectory.write({"case_id": case_id, "turn": turn, "action": None,
                                  "error": error, "rationale": ""})
                continue

            if turn == self.max_turns and (error or decision.action.type != ActionType.DIAGNOSE):
                # ponytail: emergency fallback; clinical stopping policy needs validated data.
                diagnosis = state.differential[0].name if state.differential else "미확정"
                error = error or "final_turn_requires_diagnosis"
                decision = Decision(Action(ActionType.DIAGNOSE, diagnosis),
                                    "최종 응답 실패에 따른 강제 종료", state.differential)

            action = decision.action
            if action.type != ActionType.DIAGNOSE and state.has_action(action):
                duplicate_actions += 1
                state.last_error = "duplicate_action"
                trajectory.write({"case_id": case_id, "turn": turn, "action": action.to_dict(),
                                  "error": "duplicate_action", "rationale": decision.rationale,
                                  "differential": [item.name for item in state.differential]})
                continue

            state.last_error = error
            state.differential = list(decision.differential)
            if action.type == ActionType.DIAGNOSE:
                state.actions_taken.append(action)
                state.final_diagnosis = action.content.strip()
                trajectory.write({"case_id": case_id, "turn": turn, "action": action.to_dict(),
                                  "observation": None, "rationale": decision.rationale,
                                  "differential": [item.name for item in state.differential],
                                  "differential_details": [asdict(item) for item in state.differential],
                                  "error": error})
                break

            try:
                observation = observe(action, turn)
                state.add_observation(observation, action, turn)
                state.actions_taken.append(action)
            except Exception as exc:
                execution_failures += 1
                error = "environment_error: " + type(exc).__name__
                state.last_error = error
                observation = None
            trajectory.write({"case_id": case_id, "turn": turn, "action": action.to_dict(),
                              "observation": observation.to_dict() if observation else None,
                              "rationale": decision.rationale,
                              "differential": [item.name for item in state.differential],
                              "differential_details": [asdict(item) for item in state.differential],
                              "error": error})

        return RunResult(state, duplicate_actions, format_errors, execution_failures,
                         (perf_counter() - started) * 1000)
