from src.actions import ActionType, normalize


def evaluate(case, result):
    state = result.state
    counts = {action_type.value: 0 for action_type in ActionType}
    for action in state.actions_taken:
        counts[action.type.value] += 1
    information_attempts = counts["SAY"] + counts["EXAM"] + counts["TEST"] + result.duplicate_actions
    return {
        "case_id": case.case_id,
        "diagnosis": state.final_diagnosis,
        "correct": normalize(state.final_diagnosis or "") == normalize(case.diagnosis),
        "turns": state.turn,
        "say_count": counts["SAY"],
        "attempts": state.attempt,
        "soap": state.soap,
        "competition_model_calls": result.competition_model_calls,
        "model_call_requirement_met": result.competition_model_calls > 0,
        "official_submission_sent": False,
        "exam_count": counts["EXAM"],
        "test_count": counts["TEST"],
        "duplicate_actions": result.duplicate_actions,
        "duplicate_rate": result.duplicate_actions / information_attempts if information_attempts else 0.0,
        "format_errors": result.format_errors,
        "execution_failures": result.execution_failures,
        "forced_diagnosis": result.forced_diagnosis,
        "elapsed_ms": round(result.elapsed_ms, 3),
        "safety_assessed": False,
    }
