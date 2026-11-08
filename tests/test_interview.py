"""Offline contract checks; scripted replies cannot measure interview quality."""
import json
import unittest
from unittest.mock import Mock

import run


SESSION = {'initial': {'opening': '테스트용 첫 진술', 'vital_signs': '미제공'},
           'turns_remaining': 50}


def completion(action, content=''):
    return {'choices': [{'message': {'content': json.dumps(
        {'action': action, 'content': content}, ensure_ascii=False)}}]}


class InterviewTest(unittest.TestCase):
    def test_action_format_retry_keeps_call_budget_and_transcript(self):
        for turns_left in (35, 10, 1):
            with self.subTest(turns_left=turns_left):
                calls = Mock()
                calls.chat.side_effect = [completion('SAY', '가' * 31),
                                          completion('SAY', '다시 구체적으로 말씀해 주세요')]
                transcript = ['의사: 복용 중인 약이 있나요?', '환자: 이름은 모르겠어요.']
                result = run.next_step(calls, SESSION, transcript, turns_left, lambda: 60)
                self.assertEqual(result, ('SAY', '다시 구체적으로 말씀해 주세요', 'question'))
                self.assertEqual(calls.chat.call_count, 2)
                for call in calls.chat.call_args_list:
                    self.assertEqual(call.kwargs['max_tokens'], 2048)
                    self.assertEqual(call.kwargs['reasoning_effort'], 'medium')
                    for line in transcript:
                        self.assertIn(line, call.args[0][1]['content'])

    def test_done_finishes_without_an_extra_model_call(self):
        calls, clock = Mock(), Mock()
        calls.need.return_value = 0
        clock.left.return_value = 1800
        calls.chat.return_value = completion('DONE')
        events = []
        run.interview(calls, SESSION, events, clock)
        self.assertEqual(calls.chat.call_count, 1)
        calls.say.assert_not_called()
        calls.exam.assert_not_called()
        self.assertEqual(events, [])

    def test_interview_still_reserves_five_of_forty_turns(self):
        calls, clock = Mock(), Mock()
        calls.need.return_value = 0
        clock.left.return_value = 1800
        calls.chat.side_effect = lambda *a, **kw: completion('SAY', f'질문 {calls.chat.call_count}')
        calls.say.side_effect = lambda *a, **kw: {
            'content': '테스트 답변', 'turn': calls.say.call_count,
            'turns_remaining': 50 - calls.say.call_count}
        events = []
        run.interview(calls, SESSION, events, clock)
        self.assertEqual((run.MAX_TURNS, run.EXPLAIN_TURNS), (40, 5))
        self.assertEqual((len(events), calls.chat.call_count, calls.say.call_count), (35, 35, 35))
        self.assertEqual(events[-1]['turn'], 35)

    def test_expired_budget_does_not_call_model(self):
        calls = Mock()
        self.assertIsNone(run.next_step(calls, SESSION, [], 10, lambda: 0))
        calls.chat.assert_not_called()


if __name__ == '__main__':
    unittest.main()
