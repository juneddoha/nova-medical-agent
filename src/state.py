from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from src.actions import Action


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
        }
