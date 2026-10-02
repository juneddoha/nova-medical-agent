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
    forced_diagnosis: bool = False


class Agent:
    def __init__(self, adapter, max_turns=60):
        if type(max_turns) is not int or not 1 <= max_turns <= 60:
            raise ValueError("max_turns must be between 1 and 60")
        self.adapter = adapter
        self.max_turns = max_turns

    def run(self, case_id, demographics, chief_complaint, observe, trajectory):
        state = PatientState(case_id, dict(demographics), chief_complaint,
                             max_turns=self.max_turns)
        duplicate_actions = format_errors = execution_failures = 0
        forced_diagnosis = False
        started = perf_counter()

        for turn in range(1, self.max_turns + 1):
            state.turn = turn
            error = None
            error_detail = None
            decision = None
            try:
                decision = self.adapter.next_action(state)
            except InvalidDecision as exc:
                format_errors += 1
                error = "invalid_decision"
                error_detail = exc.detail
            except Exception as exc:
                execution_failures += 1
                error = "adapter_error: " + type(exc).__name__
                error_detail = {"code": "adapter_exception", "field": None,
                                "message": "모델 호출에 실패했습니다. 연결 상태와 호출 함수를 확인하세요."}

            if error is None:
                try:
                    if not isinstance(decision, Decision):
                        raise InvalidDecision("Adapter는 Decision 객체를 반환해야 합니다.", "invalid_type", "response")
                    decision.validate()
                except (AttributeError, TypeError, ValueError) as exc:
                    format_errors += 1
                    error = "invalid_decision"
                    error_detail = exc.detail if isinstance(exc, InvalidDecision) else {
                        "code": "invalid_fields", "field": "response", "message": "결정 필드의 자료형을 확인하세요."}

            if error:
                state.last_error = error
                state.last_error_detail = error_detail
            if error and turn < self.max_turns:
                trajectory.write({"case_id": case_id, "turn": turn, "action": None,
                                  "error": error, "error_detail": error_detail, "rationale": ""})
                continue

            if turn == self.max_turns and (error or decision.action.type != ActionType.DIAGNOSE):
                # ponytail: emergency fallback; clinical stopping policy needs validated data.
                diagnosis = state.differential[0].name if state.differential else "미확정"
                error = error or "final_turn_requires_diagnosis"
                error_detail = error_detail or {"code": "final_turn_requires_diagnosis", "field": "action.type",
                                                "message": "마지막 턴에는 DIAGNOSE가 필요합니다."}
                forced_diagnosis = True
                decision = Decision(Action(ActionType.DIAGNOSE, diagnosis),
                                    "최종 응답 실패에 따른 강제 종료", state.differential)

            action = decision.action
            if action.type != ActionType.DIAGNOSE and state.has_action(action):
                duplicate_actions += 1
                state.last_error = "duplicate_action"
                state.last_error_detail = {"code": "duplicate_action", "field": "action",
                                           "message": "이미 성공한 행동입니다. 다른 행동을 선택하세요."}
                trajectory.write({"case_id": case_id, "turn": turn, "action": action.to_dict(),
                                  "error": "duplicate_action", "rationale": decision.rationale,
                                  "error_detail": state.last_error_detail,
                                  "differential": [item.name for item in state.differential]})
                continue

            state.last_error = error
            state.last_error_detail = error_detail
            state.differential = list(decision.differential)
            if action.type == ActionType.DIAGNOSE:
                state.actions_taken.append(action)
                state.final_diagnosis = action.content.strip()
                trajectory.write({"case_id": case_id, "turn": turn, "action": action.to_dict(),
                                  "observation": None, "rationale": decision.rationale,
                                  "differential": [item.name for item in state.differential],
                                  "differential_details": [asdict(item) for item in state.differential],
                                  "error": error, "error_detail": error_detail,
                                  "forced_diagnosis": forced_diagnosis})
                break

            try:
                observation = observe(action, turn)
                state.add_observation(observation, action, turn)
                state.actions_taken.append(action)
            except Exception as exc:
                execution_failures += 1
                error = "environment_error: " + type(exc).__name__
                error_detail = {"code": "environment_exception", "field": None,
                                "message": "환경 호출 또는 관찰 검증에 실패했습니다. 응답 값·상태·출처를 확인하세요."}
                state.last_error = error
                state.last_error_detail = error_detail
                observation = None
            trajectory.write({"case_id": case_id, "turn": turn, "action": action.to_dict(),
                              "observation": observation.to_dict() if observation else None,
                              "rationale": decision.rationale,
                              "differential": [item.name for item in state.differential],
                              "differential_details": [asdict(item) for item in state.differential],
                              "error": error, "error_detail": error_detail})

        return RunResult(state, duplicate_actions, format_errors, execution_failures,
                         (perf_counter() - started) * 1000, forced_diagnosis)
