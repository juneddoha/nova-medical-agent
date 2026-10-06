from copy import deepcopy
from dataclasses import asdict, dataclass
from itertools import count
from math import isfinite
from time import perf_counter

from src.actions import Action, ActionType, ExamRejected
from src.connection import CallDeadlineExceeded, bounded_call
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
    competition_model_calls: int = 0


class Agent:
    def __init__(self, adapter, max_turns=50, *, stage="preliminary", max_attempts=200,
                 time_limit_seconds=1200, submission_reserve_seconds=5, manual_input=False):
        if type(max_turns) is not int or not 1 <= max_turns <= 50:
            raise ValueError("max_turns must be between 1 and 50")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 200:
            raise ValueError("max_attempts must be between 1 and 200")
        if stage not in ("preliminary", "final"):
            raise ValueError("Unknown competition stage")
        if (type(time_limit_seconds) not in (int, float) or not isfinite(time_limit_seconds)
                or not 0 < time_limit_seconds <= 1200):
            raise ValueError("time_limit_seconds must be in (0, 1200]")
        if (type(submission_reserve_seconds) not in (int, float) or not isfinite(submission_reserve_seconds)
                or not 0 <= submission_reserve_seconds < time_limit_seconds):
            raise ValueError("Invalid submission reserve")
        self.adapter = adapter
        self.max_turns = max_turns
        self.stage = stage
        self.max_attempts = max_attempts
        self.time_limit_seconds = time_limit_seconds
        self.submission_reserve_seconds = submission_reserve_seconds
        self.manual_input = manual_input

    def run(self, case_id, demographics, chief_complaint, observe, trajectory, *, vital_signs=None):
        state = PatientState(case_id, dict(demographics), chief_complaint,
                             max_turns=self.max_turns, stage=self.stage,
                             vital_signs=dict(vital_signs or {}))
        duplicate_actions = format_errors = execution_failures = 0
        forced_diagnosis = False
        started = perf_counter()
        deadline = float("inf") if self.manual_input else started + self.time_limit_seconds - self.submission_reserve_seconds
        initial_calls = getattr(self.adapter, "successful_calls", 0)

        def record(action=None, observation=None, decision=None):
            trajectory.write({"case_id": case_id, "turn": state.turn, "attempt": state.attempt,
                              "action": action.to_dict() if action else None,
                              "observation": observation.to_dict() if observation else None,
                              "rationale": decision.rationale if decision else "",
                              "differential": [item.name for item in state.differential],
                              "differential_details": [asdict(item) for item in state.differential],
                              "error": state.last_error, "error_detail": state.last_error_detail,
                              "forced_diagnosis": forced_diagnosis,
                              "soap": state.soap})

        def error(code, message, detail=None):
            state.last_error = code
            state.last_error_detail = detail or {"code": code, "field": None, "message": message}

        def finish(decision):
            state.differential = list(decision.differential)
            state.final_diagnosis = decision.action.content.strip()
            state.soap = state.make_soap(state.final_diagnosis, decision.rationale, decision.plan)
            state.actions_taken.append(decision.action)
            record(decision.action, decision=decision)

        attempts = count(1) if self.manual_input else range(1, self.max_attempts + 1)
        for attempt in attempts:
            state.attempt = attempt
            state.remaining_seconds = (None if self.manual_input else
                                       max(0, started + self.time_limit_seconds - perf_counter()))
            state.must_diagnose = state.turn >= self.max_turns or (not self.manual_input and attempt == self.max_attempts)
            if perf_counter() >= deadline:
                error("time_budget_exhausted", "기록 마감 시간을 확보하기 위해 종료합니다.")
                break
            decision = None
            try:
                # A late callback cannot mutate the live patient state after the deadline.
                snapshot = deepcopy(state)
                decision = (self.adapter.next_action(snapshot) if self.manual_input else
                            bounded_call(lambda: self.adapter.next_action(snapshot),
                                         min(60, deadline - perf_counter())))
                if not isinstance(decision, Decision):
                    raise InvalidDecision("Adapter는 Decision 객체를 반환해야 합니다.", "invalid_type", "response")
                decision.validate()
                try:
                    decision.action.validate_for_stage(self.stage)
                except ValueError as exc:
                    raise InvalidDecision(str(exc), "action_rule_violation", "action") from None
            except CallDeadlineExceeded:
                execution_failures += 1
                if hasattr(self.adapter, "disabled"):
                    self.adapter.disabled = True
                error("call_deadline_exceeded", "모델 응답 대기 기한을 초과했습니다.")
                break
            except InvalidDecision as exc:
                format_errors += 1
                error("invalid_decision", "", exc.detail)
                if state.must_diagnose and not self.manual_input:
                    break
                record()
                continue
            except Exception as exc:
                execution_failures += 1
                error("adapter_error: " + type(exc).__name__, "모델 호출에 실패했습니다. 연결 설정을 확인하세요.")
                if not self.manual_input and (state.must_diagnose or getattr(self.adapter, "disabled", False)):
                    break
                record()
                continue

            if perf_counter() >= deadline:
                error("time_budget_exhausted", "기록 마감 시간을 확보하기 위해 종료합니다.")
                break
            if state.must_diagnose and decision.action.type != ActionType.DIAGNOSE:
                error("final_turn_requires_diagnosis", "사용 가능한 진료 턴 또는 판단 시도를 소진했습니다.")
                if self.manual_input:
                    format_errors += 1
                    record(decision=decision)
                    continue
                break
            action = decision.action
            if action.type != ActionType.DIAGNOSE and state.has_action(action):
                duplicate_actions += 1
                error("duplicate_action", "이미 성공한 행동입니다. 다른 행동을 선택하세요.")
                record(action, decision=decision)
                continue
            state.last_error = state.last_error_detail = None
            if action.type == ActionType.DIAGNOSE:
                finish(decision)
                break

            observation = None
            next_turn = state.turn + 1
            try:
                observation = (observe(action, next_turn) if self.manual_input else
                               bounded_call(lambda: observe(action, next_turn), deadline - perf_counter()))
                state.add_observation(observation, action, next_turn)
            except ExamRejected:
                if action.type == ActionType.EXAM:
                    error("exam_rejected", "진찰 동작 하나를 문장으로 다시 요청하세요. 턴은 차감하지 않았습니다.")
                else:
                    state.turn = next_turn
                    execution_failures += 1
                    error("environment_exception", "EXAM 외 행동에 잘못된 거절 응답이 도착했습니다.")
                record(action, decision=decision)
                continue
            except CallDeadlineExceeded:
                state.turn = next_turn
                execution_failures += 1
                error("call_deadline_exceeded", "환경 응답 대기 기한을 초과했습니다.")
                break
            except Exception as exc:
                # Without an explicit rejection, conservatively count the attempted external action.
                state.turn = next_turn
                execution_failures += 1
                error("environment_error: " + type(exc).__name__, "환경 응답 값·상태·출처를 확인하세요.")
                record(action, decision=decision)
                continue
            state.turn = next_turn
            state.actions_taken.append(action)
            state.differential = list(decision.differential)
            record(action, observation, decision)

        if state.final_diagnosis is None:
            forced_diagnosis = True
            # Never turn an unvalidated candidate into a confirmed diagnosis during fallback.
            finish(Decision(Action(ActionType.DIAGNOSE, "미확정"),
                            "실행 제한 또는 응답 오류로 평가를 완료하지 못함", state.differential,
                            "자동 진료가 완료되지 않았습니다. 추가 평가가 필요하며 치료·처치·추적·교육 계획은 미수립입니다."))

        model_calls = (getattr(self.adapter, "successful_calls", 0) - initial_calls
                       if getattr(self.adapter, "competition", False) else 0)
        return RunResult(state, duplicate_actions, format_errors, execution_failures,
                         (perf_counter() - started) * 1000, forced_diagnosis, model_calls)
