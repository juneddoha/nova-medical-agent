import json
from dataclasses import dataclass
from pathlib import Path

from src.actions import Action, ActionType
from src.state import Observation, VALID_STATUSES


@dataclass(frozen=True)
class Case:
    case_id: str
    demographics: dict
    chief_complaint: str
    responses: dict
    diagnosis: str


def load_cases(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = []
    ids = set()
    for item in raw:
        case_id = item["case_id"]
        if case_id in ids:
            raise ValueError("Duplicate case_id: " + case_id)
        ids.add(case_id)
        responses = {}
        for response in item["responses"]:
            action = Action(ActionType(response["type"]), response["content"])
            key = action.key()
            if action.type == ActionType.DIAGNOSE or key in responses:
                raise ValueError("Invalid or duplicate response in " + case_id)
            if response["status"] not in VALID_STATUSES:
                raise ValueError("Invalid observation status in " + case_id)
            responses[key] = {
                "value": response["value"],
                "status": response["status"],
            }
        cases.append(Case(
            case_id=case_id,
            demographics=item["initial"]["demographics"],
            chief_complaint=item["initial"]["chief_complaint"],
            responses=responses,
            diagnosis=item["diagnosis"],
        ))
    return cases


class LocalEnvironment:
    def __init__(self, case):
        self._responses = case.responses

    def observe(self, action, turn):
        action.validate()
        if action.type == ActionType.DIAGNOSE:
            raise ValueError("DIAGNOSE has no observation")
        response = self._responses.get(action.key(), {
            "value": "이 행동에 대한 정보가 정의되지 않음",
            "status": "unknown",
        })
        return Observation(
            value=response["value"],
            status=response["status"],
            source_turn=turn,
            source_action=action,
        )
