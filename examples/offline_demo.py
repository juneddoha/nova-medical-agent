"""Deterministic demonstration; no server, keys, or real patient data."""
import json
from pathlib import Path
import sys
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import record
import run


def main():
    calls = Mock()
    calls.chat.return_value = {'choices': [{'message': {'content': json.dumps({
        'action': 'SAY', 'content': '언제부터 잠들기 어려우셨나요?',
    }, ensure_ascii=False)}}]}
    initial = {'opening': '잠들기 어려워요.', 'vital_signs': ''}
    action, content, _ = run.next_step(calls, {'initial': initial}, [], 10, lambda: 60)
    # A synthetic uncertain answer cannot support a negative symptom mark.
    events = [{'turn': 1, 'action': 'SAY', 'content': '기침이 있나요?', 'reply': '잘 모르겠어요'}]
    proposed = {'ros_cough': {'mark': '-', 'turn': 1, 'quote': '잘 모르겠어요'}}
    kept = record.checked_marks(proposed, initial, events)
    assert action == 'SAY' and content == '언제부터 잠들기 어려우셨나요?'
    assert 'ros_cough' not in kept
    calls.say.assert_not_called()
    calls.exam.assert_not_called()
    print(json.dumps({'mode': 'offline_mock', 'action': action, 'content': content,
                      'unsupported_negative_rejected': 'ros_cough' not in kept,
                      'doctor_mock_calls': calls.chat.call_count}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
