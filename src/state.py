from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from src.actions import Action, ActionType

VALID_STATUSES = {"positive", "negative", "unknown", "contradictory"}


@dataclass(frozen=True)
class Observation:
    value: str
    status: str
    source_turn: int
    source_action: Action

    def to_dict(self):
        result = asdict(self)
        result["source_action"] = self.source_action.to_dict()
        return result


@dataclass(frozen=True)
class DifferentialCandidate:
    name: str
    supporting: List[str] = field(default_factory=list)
    opposing: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    risk: str = "unknown"


@dataclass
class PatientState:
    case_id: str
    demographics: Dict[str, str]
    chief_complaint: str
    observations: List[Observation] = field(default_factory=list)
    differential: List[DifferentialCandidate] = field(default_factory=list)
    actions_taken: List[Action] = field(default_factory=list)
    turn: int = 0
    final_diagnosis: Optional[str] = None
    max_turns: int = 50
    last_error: Optional[str] = None
    last_error_detail: Optional[dict] = None
    stage: str = "preliminary"
    vital_signs: dict = field(default_factory=dict)
    attempt: int = 0
    must_diagnose: bool = False
    remaining_seconds: Optional[float] = 1200.0
    soap: Optional[dict] = None

    def add_observation(self, observation, action, turn):
        if not isinstance(observation, Observation):
            raise ValueError("Environment must return Observation")
        if not isinstance(observation.value, str) or not observation.value.strip():
            raise ValueError("Observation value must be nonempty text")
        if not isinstance(observation.status, str) or observation.status not in VALID_STATUSES:
            raise ValueError("Invalid observation status")
        if (type(observation.source_turn) is not int or observation.source_turn != turn
                or not isinstance(observation.source_action, Action)
                or observation.source_action.key() != action.key()):
            raise ValueError("Observation source does not match the requested action/turn")
        self.observations.append(observation)

    def has_action(self, action):
        return any(previous.key() == action.key() for previous in self.actions_taken)

    def to_dict(self):
        return {
            "case_id": self.case_id,
            "demographics": self.demographics,
            "chief_complaint": self.chief_complaint,
            "observations": [item.to_dict() for item in self.observations],
            "differential": [asdict(item) for item in self.differential],
            "actions_taken": [item.to_dict() for item in self.actions_taken],
            "turn": self.turn,
            "final_diagnosis": self.final_diagnosis,
            "max_turns": self.max_turns,
            "stage": self.stage,
            "vital_signs": self.vital_signs,
            "attempt": self.attempt,
            "must_diagnose": self.must_diagnose,
            "remaining_seconds": self.remaining_seconds,
            "soap": self.soap,
            "last_error": self.last_error,
            "last_error_detail": self.last_error_detail,
        }

    def make_soap(self, diagnosis, rationale, plan):
        """Build S/O from received data only; A/P remain unverified model judgments."""
        subjective = ["주호소: " + self.chief_complaint, "초기 인적 정보: " + str(self.demographics)]
        objective = ["초기 활력징후: " + (str(self.vital_signs) if self.vital_signs else "미제공")]
        for item in self.observations:
            target = subjective if item.source_action.type == ActionType.SAY else objective
            target.append("턴 {} | {} | {} | {}".format(
                item.source_turn, item.source_action.content, item.status, item.value))
        subjective.append("위 기록 외 병력·약물·알레르기·가족력·사회력·계통 문진은 확인 여부를 추정하지 않음.")
        return {"S": "\n".join(subjective), "O": "\n".join(objective),
                "A": {"primary_diagnosis": diagnosis, "rationale": rationale,
                      "differential": [asdict(item) for item in self.differential]},
                "P": plan}
