"""Decision interface. The mock is only for local plumbing checks."""

from dataclasses import dataclass, field
from typing import List, Protocol

from src.actions import Action, ActionType, normalize
from src.state import DifferentialCandidate, PatientState


@dataclass(frozen=True)
class Decision:
    action: Action
    rationale: str
    differential: List[DifferentialCandidate] = field(default_factory=list)


class LLMAdapter(Protocol):
    def next_action(self, state: PatientState) -> Decision:
        """Use only information already present in state."""


class MockLLMAdapter:
    """Fixed toy decisions; no model call or medical reasoning occurs."""

    _PLANS = {
        "복통": [
            (ActionType.ASK, "통증이 시작된 위치는 어디인가요?"),
            (ActionType.EXAM, "복부 압통"),
            (ActionType.TEST, "혈액검사"),
            (ActionType.DIAGNOSE, "급성 충수염"),
        ],
        "기침": [
            (ActionType.ASK, "발열이 있나요?"),
            (ActionType.EXAM, "폐 청진"),
            (ActionType.TEST, "독감 신속항원검사"),
            (ActionType.DIAGNOSE, "인플루엔자"),
        ],
    }
    _CANDIDATES = {
        "복통": ["급성 충수염", "위장염", "요로결석"],
        "기침": ["인플루엔자", "상기도 감염", "폐렴"],
    }

    def next_action(self, state):
        complaint = normalize(state.chief_complaint)
        plan = self._PLANS.get(complaint)
        if plan is None:
            return Decision(Action(ActionType.DIAGNOSE, "미확정"), "Mock plan 없음")
        completed = {action.key() for action in state.actions_taken}
        for action_type, content in plan:
            action = Action(action_type, content)
            if action.key() not in completed:
                candidates = [
                    DifferentialCandidate(name=name, missing=["추가 정보 확인 필요"])
                    for name in self._CANDIDATES[complaint]
                ]
                return Decision(action, "고정된 로컬 예제 순서", candidates)
        return Decision(Action(ActionType.DIAGNOSE, plan[-1][1]), "예제 종료")
