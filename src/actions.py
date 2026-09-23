from dataclasses import dataclass
from enum import Enum


class ActionType(str, Enum):
    ASK = "ASK"
    EXAM = "EXAM"
    TEST = "TEST"
    DIAGNOSE = "DIAGNOSE"


def normalize(text):
    return " ".join(text.split()).casefold()


@dataclass(frozen=True)
class Action:
    type: ActionType
    content: str

    def validate(self):
        if not isinstance(self.type, ActionType):
            raise ValueError("Unknown action type")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("Action content must be nonempty text")

    def key(self):
        self.validate()
        return (self.type.value, normalize(self.content))

    def to_dict(self):
        return {"type": self.type.value, "content": self.content}
