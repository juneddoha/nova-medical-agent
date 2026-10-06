from dataclasses import dataclass
from enum import Enum


class ActionType(str, Enum):
    SAY = "SAY"
    EXAM = "EXAM"
    TEST = "TEST"
    DIAGNOSE = "DIAGNOSE"


class ExamRejected(ValueError):
    """The environment explicitly rejected an EXAM without charging a turn."""


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

    def validate_for_stage(self, stage):
        self.validate()
        if stage not in ("preliminary", "final"):
            raise ValueError("Unknown competition stage")
        if self.type == ActionType.TEST and stage == "preliminary":
            raise ValueError("예선에서는 TEST를 실행할 수 없습니다. 검사 계획은 DIAGNOSE의 plan에 기록하세요.")
        if self.type == ActionType.SAY:
            if len(self.content) > 30:
                raise ValueError("SAY는 공백·문장부호를 포함해 30자 이하여야 합니다.")
            # ponytail: obvious multiple questions only; semantic bundling needs model/server validation.
            if self.content.count("?") + self.content.count("？") > 1 or "\n" in self.content or "\r" in self.content:
                raise ValueError("SAY는 한 번에 하나의 질문 또는 설명만 전달하세요.")
        if self.type == ActionType.EXAM:
            # Obvious bundles only; clinical semantics remain the environment's responsibility.
            if any(separator in self.content for separator in ("\n", "\r", ";", " 그리고 ", " 및 ", "하고 ", " and ")):
                raise ValueError("EXAM은 진찰 동작 하나만 요청하세요.")

    def key(self):
        self.validate()
        return (self.type.value, normalize(self.content))

    def to_dict(self):
        return {"type": self.type.value, "content": self.content}
