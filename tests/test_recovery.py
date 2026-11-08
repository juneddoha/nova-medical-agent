"""Offline recovery checks, including the lengths observed in the Luna run."""
import json
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import Mock

import run
import record
from nova_sdk import NovaError

SESSION = {'initial': {'opening': '모의 첫 진술', 'vital_signs': ''}, 'turns_remaining': 50}
LONG_GUIDANCE = ['구급대 올 때까지 남편 곁에 계시고 약은 더 드시지 마세요.',
                 '구급대 올 때까지 남편 곁에 계시고 약 더 먹지 마세요.',
                 '구급대 올 때까지 남편 곁에 계시고 약은 더 드시지 마세요.']
FORMAT_FAILURES = ['가' * 31] * 3  # 마침표 정리로 해결할 수 없는 형식 오류 묶음


def completion(value):
    return {'choices': [{'message': {'content': json.dumps(value, ensure_ascii=False)}}]}


def setup():
    calls, clock = Mock(), Mock()
    calls.need.return_value = 0
    calls.record_need.return_value = 100
    clock.left.return_value = 1800
    return calls, clock


class InterviewRecoveryTest(unittest.TestCase):
    def test_unformattable_failure_can_recover_without_spending_a_turn(self):
        calls, clock = setup()
        calls.chat.side_effect = [*[completion({'action': 'SAY', 'content': text}) for text in FORMAT_FAILURES],
                                  completion({'action': 'SAY', 'content': '약을 줄여 본 적이 있나요?'}),
                                  completion({'action': 'DONE'})]
        calls.say.return_value = {'turn': 1, 'content': '모의 답변', 'turns_remaining': 49}
        events = []
        run.interview(calls, SESSION, events, clock)
        self.assertEqual(calls.chat.call_count, 5)
        self.assertEqual(calls.say.call_count, 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['turn'], 1)
        recovery_context = calls.chat.call_args_list[3].args[0][1]['content']
        self.assertIn(FORMAT_FAILURES[0], recovery_context)
        self.assertIn('미전송', recovery_context)

    def test_permanent_format_failure_is_bounded_and_never_sent(self):
        calls, clock = setup()
        calls.chat.return_value = completion({'action': 'SAY', 'content': '가' * 31})
        events = []
        run.interview(calls, SESSION, events, clock)
        self.assertEqual(calls.chat.call_count, 9)
        calls.say.assert_not_called()
        calls.exam.assert_not_called()
        self.assertEqual(events, [])

    def test_non_string_content_is_not_sent_as_python_text(self):
        calls, _ = setup()
        calls.chat.side_effect = [completion({'action': 'SAY', 'content': None}),
                                  completion({'action': 'SAY', 'content': ['잘못된 형식']}),
                                  completion({'action': 'DONE'})]
        self.assertEqual(run.next_step(calls, SESSION, [], 10, lambda: 60)[0], 'DONE')
        self.assertEqual(calls.chat.call_count, 3)

    def test_format_recovery_budget_is_shared_across_successful_turns(self):
        calls, clock = setup()
        block = [*[completion({'action': 'SAY', 'content': text}) for text in FORMAT_FAILURES],
                 completion({'action': 'SAY', 'content': '복용 기간은 얼마나 되나요?'})]
        calls.chat.side_effect = block * 3
        calls.say.side_effect = lambda *a, **kw: {'turn': calls.say.call_count,
            'content': '모의 답변', 'turns_remaining': 50 - calls.say.call_count}
        events = []
        run.interview(calls, SESSION, events, clock)
        self.assertEqual(calls.chat.call_count, 11)
        self.assertEqual(calls.say.call_count, 2)
        self.assertEqual([e['turn'] for e in events], [1, 2])

    def test_expired_budget_after_retries_does_not_start_recovery(self):
        calls, _ = setup()
        calls.chat.return_value = completion({'action': 'SAY', 'content': '가' * 31})
        budget = Mock(side_effect=[60, 60, 60, 0])
        self.assertIsNone(run.next_step(calls, SESSION, [], 10, budget))
        self.assertEqual(calls.chat.call_count, 3)


class ExplanationRecoveryTest(unittest.TestCase):
    def test_saved_explanation_lengths_do_not_skip_later_treatment_and_warning(self):
        calls, clock = setup()
        calls.chat.side_effect = [completion({'say': text}) for text in (
            '가능성이 높은 진단을 안내합니다', '확인한 소견이 그 근거입니다',
            '약 조절은 나중에 하고, 응급실서 호흡·의식을 확인할게요.',
            '검사로 약물 영향을 확인하고, 감량은 회복 후 상의해요.',
            '치료는 의료진과 상의해요', '의식이 흐려지면 즉시 119에 연락하세요')]
        calls.say.side_effect = lambda *a, **kw: {'turn': 35 + calls.say.call_count,
            'content': '치료가 걱정돼요', 'turns_remaining': 15 - calls.say.call_count}
        events = [{'turn': 35, 'action': 'SAY', 'content': '문진', 'reply': '모의 답변'}]
        run.explain_to_patient(calls, SESSION, events, clock)
        self.assertEqual((calls.chat.call_count, calls.say.call_count), (6, 5))
        self.assertEqual(events[-1]['turn'], 40)
        self.assertEqual(events[-1]['content'], '의식이 흐려지면 즉시 119에 연락하세요')
        self.assertNotIn('전달하지 못했던 안내 목적', calls.chat.call_args_list[4].args[0][1]['content'])
        self.assertIn('치료가 걱정돼요', calls.chat.call_args_list[4].args[0][1]['content'])
        self.assertTrue(all(len(e['content']) <= 30 for e in events))

    def test_no_generation_budget_sends_one_fallback_without_model_call(self):
        calls, clock = setup()
        clock.left.return_value = 150
        calls.need.side_effect = lambda chats=0, says=0: chats * 60 + says * 5
        calls.say.return_value = {'turn': 1, 'content': '네', 'turns_remaining': 49}
        events = []
        run.explain_to_patient(calls, SESSION, events, clock)
        calls.chat.assert_not_called()
        self.assertEqual(calls.say.call_count, 1)
        self.assertEqual(len(events), 1)


class DeliveryContractTest(unittest.TestCase):
    def observed_client(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/observed_say_delivery.json').read_text(encoding='utf-8'))
        client, clock = Mock(), Mock()
        client.timeout = 240
        client.say.return_value = fixture['action_response']
        client.session.return_value = fixture['session_response']
        clock.left.return_value = 1800
        return fixture, client, clock, run.Calls(client, clock)

    def test_saved_api_shape_uses_history_for_next_context_and_record(self):
        fixture, client, clock, calls = self.observed_client()
        request = fixture['action_request']
        self.assertNotIn('effective_content', fixture['action_response'])
        client.chat.side_effect = [completion({'action': 'SAY', 'content': request['content']}),
                                   completion({'action': 'DONE'})]
        # 새 요청 ID를 사용하면서 관측된 실제 응답 형태를 재현한다.
        def session():
            state = deepcopy(fixture['session_response'])
            state['events'][0]['payload']['request_id'] = client.say.call_args.kwargs['request_id']
            return state
        client.session.side_effect = session
        events = []
        run.interview(calls, SESSION, events, clock)
        expected = fixture['session_response']['events'][0]['effective_content']
        self.assertEqual(events[0]['content'], expected)
        self.assertEqual(events[0]['requested_content'], request['content'])
        self.assertTrue(events[0]['delivery_verified'])
        self.assertIn(expected, client.chat.call_args_list[1].args[0][1]['content'])
        self.assertNotIn('자주 깨나요', client.chat.call_args_list[1].args[0][1]['content'])
        self.assertNotIn('자주 깨나요', record.dialogue(SESSION['initial'], events))
        client.say.assert_called_once()
        client.session.assert_called_once()

    def test_failed_or_mismatched_history_does_not_replay_successful_say(self):
        for kind in ('timeout', 'wrong_request', 'wrong_turn', 'malformed', 'no_time'):
            with self.subTest(kind=kind):
                fixture, client, clock, calls = self.observed_client()
                request = fixture['action_request']
                if kind == 'timeout':
                    client.session.side_effect = NovaError('API_UNAVAILABLE')
                elif kind == 'wrong_request':
                    client.session.return_value = {'events': []}
                elif kind == 'wrong_turn':
                    client.session.return_value = deepcopy(fixture['session_response'])
                    client.session.return_value['events'][0]['turn'] = 999
                elif kind == 'malformed':
                    client.session.return_value = {'events': [None, {'action': 'SAY', 'payload': []}]}
                else:
                    clock.left.return_value = calls.record_need() + run.MIN_TIMEOUT
                reply = calls.say(request['content'], request_id=request['request_id'])
                self.assertEqual(reply, fixture['action_response'])
                self.assertNotIn('effective_content', reply)
                client.say.assert_called_once()
                self.assertLessEqual(client.session.call_count, 1)
                if kind == 'no_time':
                    client.session.assert_not_called()

    def test_unknown_delivery_is_not_claimed_as_exact_education(self):
        events = [{'turn': 1, 'action': 'SAY', 'content': '검사를 하고 내일 오세요',
                   'reply': '네', 'explain': True, 'delivery_verified': False}]
        self.assertEqual(record.education_text(events), '')
        self.assertIn('실제 전달문 미확인', record.dialogue(SESSION['initial'], events))


class DeliveredGuidanceTest(unittest.TestCase):
    def test_interview_guidance_is_recorded_but_questions_and_refusals_are_not(self):
        calls, clock = setup()
        guidance, question = '지금 119에 연락하세요', '복용 중인 약이 있나요?'
        calls.chat.side_effect = [completion({'action': 'SAY', 'content': guidance, 'purpose': 'guidance'}),
                                  completion({'action': 'SAY', 'content': question, 'purpose': 'question'}),
                                  completion({'action': 'DONE'})]
        calls.say.side_effect = lambda text, **kw: {'turn': calls.say.call_count, 'content': '모의 답변',
            'turns_remaining': 50 - calls.say.call_count, 'effective_content': text}
        events = []
        run.interview(calls, SESSION, events, clock)
        events.append({'turn': 3, 'action': 'SAY', 'content': '거절된 안내',
                       'purpose': 'guidance', 'refused': 'ERROR'})
        self.assertEqual(events[0]['purpose'], 'guidance')
        self.assertEqual(record.education_text(events), '(턴 1) 지금 119에 연락하세요')
        minimal, _ = record.minimal(SESSION, events)
        self.assertEqual(minimal['P']['education'], record.education_text(events))

    def test_shortening_keeps_guidance_purpose_when_retry_omits_it(self):
        calls, _ = setup()
        calls.chat.side_effect = [completion({'action': 'SAY', 'content': LONG_GUIDANCE[0], 'purpose': 'guidance'}),
                                  completion({'action': 'SAY', 'content': '약을 더 드시지 마세요'})]
        self.assertEqual(run.next_step(calls, SESSION, [], 10, lambda: 60),
                         ('SAY', '약을 더 드시지 마세요', 'guidance'))


class RejectedActionTest(unittest.TestCase):
    def test_unavailable_action_does_not_invent_a_consumed_turn(self):
        for phase in (run.interview, run.explain_to_patient):
            with self.subTest(phase=phase.__name__):
                calls, clock = setup()
                calls.chat.return_value = completion({'action': 'SAY', 'content': '모의 질문', 'say': '모의 안내'})
                calls.say.side_effect = NovaError('ACTION_NOT_AVAILABLE', 422)
                events = []
                phase(calls, SESSION, events, clock)
                calls.say.assert_called_once()
                self.assertEqual(events, [])


class ConsultationContractTest(unittest.TestCase):
    def test_recovery_delivery_history_and_final_record_work_together_offline(self):
        client = Mock()
        client.timeout = 240
        history = []
        client.session.side_effect = lambda: {**SESSION, 'events': deepcopy(history)}

        def say(text, *, request_id):
            reply = {'turn': len(history) + 1, 'status': 'OK', 'content': '모의 환자 답변',
                     'turns_remaining': 49 - len(history)}
            history.append({'action': 'SAY', 'turn': reply['turn'], 'effective_content': text,
                            'payload': {'request_id': request_id, 'content': text}, 'response': reply})
            return reply  # 실제 API처럼 effective_content는 행동 응답에 없다.

        client.say.side_effect = say
        client.chat.side_effect = [
            *[completion({'action': 'SAY', 'content': text}) for text in FORMAT_FAILURES],
            completion({'action': 'SAY', 'content': '보호자와 함께 기다리세요', 'purpose': 'guidance'}),
            completion({'action': 'DONE'}),
            completion({'diagnosis': '모의 진단', 'education': '실제로 말하지 않은 안내'}),
            *[completion({'say': '가' * 31}) for _ in range(2)],
            *[completion({'say': text}) for text in ('검사는 원인을 확인합니다', '치료 계획을 함께 정해요')],
            completion({'ros_cough': {'mark': ''}}),
            completion({'say': '의식이 흐려지면 119에 연락하세요'}),
            completion({'ros_cough': {'mark': ''}}),
            completion({'hpi': '모의 환자 진술로 기록함'})]
        client.diagnose.return_value = {'status': 'completed'}
        self.assertTrue(run.consult(client, None))
        client.diagnose.assert_called_once()
        submitted = client.diagnose.call_args.kwargs
        self.assertEqual(submitted['diagnosis'], '모의 진단')
        self.assertIn('모의 진단 가능성이 있어요', submitted['note']['P']['education'])
        plan_calls = [call for call in client.chat.call_args_list if 'A(평가)' in call.args[0][0]['content']]
        self.assertEqual(len(plan_calls), 1)
        self.assertIn('보호자와 함께 기다리세요', submitted['note']['P']['education'])
        self.assertIn('의식이 흐려지면 119에 연락하세요', submitted['note']['P']['education'])
        self.assertNotIn('실제로 말하지 않은 안내', submitted['note']['P']['education'])
        self.assertEqual(client.say.call_count, 5)
        self.assertEqual(client.session.call_count, 6)
        self.assertTrue(all(len(call.args[0]) <= 30 for call in client.say.call_args_list))


if __name__ == '__main__':
    unittest.main()
