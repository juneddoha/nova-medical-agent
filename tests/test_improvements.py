"""Mocked contracts only: these tests do not measure medical quality or scores."""
import json
import unittest
from unittest.mock import Mock

import run
import explain
import record
from nova_sdk import NovaError, RECORD_FIELDS

SESSION = {'initial': {'opening': '테스트 첫 진술', 'vital_signs': ''}, 'turns_remaining': 50}


def completion(value):
    return {'choices': [{'message': {'content': json.dumps(value, ensure_ascii=False)}}]}


class ActionRepairTest(unittest.TestCase):
    def test_overlong_question_stays_in_repair_context_and_cannot_become_exam(self):
        calls = Mock()
        original = '확인하려는 질문의 목적을 다른 내용으로 바꾸지 말고 알려 주세요?'
        self.assertGreater(len(original), 30)
        calls.chat.side_effect = [completion({'action': 'SAY', 'content': original}),
                                  completion({'action': 'EXAM', 'content': '복부를 청진하겠습니다'}),
                                  completion({'action': 'SAY', 'content': '원래 질문을 짧게 말씀해 주세요'})]
        self.assertEqual(run.next_step(calls, SESSION, [], 10, lambda: 60),
                         ('SAY', '원래 질문을 짧게 말씀해 주세요', 'question'))
        self.assertEqual(calls.chat.call_count, 3)
        for call in calls.chat.call_args_list[1:]:
            self.assertIn(original, call.args[0][1]['content'])

    def test_failed_repair_does_not_send_overlong_text_or_accept_done(self):
        calls = Mock()
        calls.chat.side_effect = [completion({'action': 'SAY', 'content': '가' * 31}),
                                  completion({'action': 'DONE'}), completion({'action': 'DONE'})]
        with self.assertRaises(run.ActionFormatError):
            run.next_step(calls, SESSION, [], 10, lambda: 60)
        self.assertEqual(calls.chat.call_count, 3)
        calls.say.assert_not_called()

    def test_rejected_exam_is_visible_to_next_action_selection(self):
        calls, clock = Mock(), Mock()
        calls.need.return_value = 0
        clock.left.return_value = 1800
        exam = '두 가지 진찰을 함께 하겠습니다'
        calls.chat.side_effect = [completion({'action': 'EXAM', 'content': exam}), completion({'action': 'DONE'})]
        calls.exam.side_effect = NovaError('EXAM_NOT_SPECIFIC', 422)
        events = []
        run.interview(calls, SESSION, events, clock)
        self.assertIn(exam, calls.chat.call_args_list[1].args[0][1]['content'])
        self.assertEqual(events[0]['refused'], 'EXAM_NOT_SPECIFIC')
        self.assertEqual(events[0]['turn'], 1)

    def test_only_effective_question_enters_next_context(self):
        calls, clock = Mock(), Mock()
        calls.need.return_value = 0
        clock.left.return_value = 1800
        calls.chat.side_effect = [completion({'action': 'SAY', 'content': '기침과 가래가 있나요?'}),
                                  completion({'action': 'DONE'})]
        calls.say.return_value = {'turn': 1, 'turns_remaining': 49,
                                  'effective_content': '기침이 있나요?', 'content': '없어요'}
        events = []
        run.interview(calls, SESSION, events, clock)
        context = calls.chat.call_args_list[1].args[0][1]['content']
        self.assertIn('기침이 있나요?', context)
        self.assertNotIn('가래', context)
        self.assertEqual(events[0]['requested_content'], '기침과 가래가 있나요?')
        self.assertEqual(events[0]['content'], '기침이 있나요?')


class ExplanationTest(unittest.TestCase):
    def calls_and_clock(self):
        calls, clock = Mock(), Mock()
        clock.left.return_value = 1800
        calls.need.return_value = 0
        calls.record_need.return_value = 100
        return calls, clock

    def test_next_sentence_gets_new_patient_reply_and_five_turn_limit(self):
        calls, clock = self.calls_and_clock()
        calls.chat.side_effect = [completion({'say': '어떤 점이 걱정되세요?'}),
                                  *[completion({'say': f'걱정에 대한 안내 {n}'}) for n in range(4)]]
        calls.say.side_effect = lambda *a, **kw: {'turn': 35 + calls.say.call_count,
            'content': '치료가 무서워요', 'turns_remaining': 15 - calls.say.call_count}
        events = [{'turn': 35, 'action': 'SAY', 'content': '문진 질문', 'reply': '문진 답변'}]
        run.explain_to_patient(calls, SESSION, events, clock)
        self.assertEqual((calls.chat.call_count, calls.say.call_count, events[-1]['turn']), (5, 5, 40))
        self.assertIn('치료가 무서워요', calls.chat.call_args_list[1].args[0][1]['content'])
        self.assertIn('직전 환자 답변(직접 대응할 대상): "치료가 무서워요"',
                      calls.chat.call_args_list[1].args[0][1]['content'])
        self.assertEqual(calls.say.call_args_list[0].args[0], '어떤 점이 걱정되세요?')
        self.assertTrue(all(e.get('explain') for e in events[1:]))

    def test_all_malformed_topics_get_bounded_retries_then_one_fallback(self):
        calls, clock = self.calls_and_clock()
        calls.chat.return_value = completion({'say': '가' * 31})
        calls.say.return_value = {'turn': 1, 'content': '네', 'turns_remaining': 49}
        events = []
        run.explain_to_patient(calls, SESSION, events, clock)
        self.assertEqual((calls.chat.call_count, calls.say.call_count), (10, 1))
        self.assertEqual(events[0]['content'], explain.FALLBACK)

    def test_record_reserve_and_server_turn_limit_are_respected(self):
        calls, clock = self.calls_and_clock()
        clock.left.return_value = 50
        run.explain_to_patient(calls, SESSION, [], clock)
        calls.chat.assert_not_called()
        calls.say.assert_not_called()
        clock.left.return_value = 1800
        calls.chat.return_value = completion({'say': '안내입니다'})
        calls.say.return_value = {'turn': 1, 'content': '네', 'turns_remaining': 0}
        run.explain_to_patient(calls, SESSION, [], clock)
        self.assertEqual(calls.say.call_count, 1)

    def test_explanation_records_effective_content(self):
        calls, clock = self.calls_and_clock()
        calls.chat.return_value = completion({'say': '검사를 하고 내일 다시 오세요'})
        calls.say.return_value = {'turn': 1, 'content': '네', 'turns_remaining': 0,
                                  'effective_content': '검사를 하세요'}
        events = []
        run.explain_to_patient(calls, SESSION, events, clock)
        self.assertEqual(events[0]['content'], '검사를 하세요')
        self.assertEqual(events[0]['requested_content'], '검사를 하고 내일 다시 오세요')

    def test_model_latency_cannot_use_record_reserve_for_say(self):
        calls, clock = self.calls_and_clock()

        def slow_completion(*args, **kwargs):
            clock.left.return_value = 50
            return completion({'say': '검사를 안내합니다'})

        calls.chat.side_effect = slow_completion
        events = []
        run.explain_to_patient(calls, SESSION, events, clock)
        self.assertEqual(calls.chat.call_count, 1)
        calls.say.assert_not_called()
        self.assertEqual(events, [])


class RecordEvidenceTest(unittest.TestCase):
    def test_marks_reject_missing_wrong_or_partial_patient_quote(self):
        events = [{'turn': 1, 'action': 'SAY', 'content': '기침이 없으시죠?', 'reply': '가끔 기침이 나요'},
                  {'turn': 2, 'action': 'SAY', 'content': '열과 오한이 있나요?', 'reply': record.NO_ANSWER},
                  {'turn': 3, 'action': 'EXAM', 'content': '진찰', 'reply': '정상'}]
        for turn, quote in [(1, None), (1, '없어요'), (1, '기침이 없으시죠?'),
                            (1, '기침이 나요'), (2, record.NO_ANSWER), (3, '정상'), (4, '없어요')]:
            with self.subTest(turn=turn, quote=quote):
                self.assertEqual(record.checked_marks({'ros_cough': {'mark': '-', 'turn': turn, 'quote': quote}},
                                                       SESSION['initial'], events), {})
        self.assertEqual(record.checked_marks({'ros_cough': {'mark': '+', 'turn': 1, 'quote': '가끔 기침이 나요'}},
                                               SESSION['initial'], events), {'ros_cough': '+'})

    def test_opening_and_new_explanation_answers_need_matching_source(self):
        initial = {'opening': '당뇨병이 있어요. 기침은 없어요.', 'vital_signs': ''}
        events = [{'turn': 36, 'action': 'SAY', 'content': '약 알레르기가 있나요?',
                   'reply': '약 알레르기는 없어요', 'explain': True}]
        raw = {'pmh_dm': {'mark': '+', 'turn': 0, 'quote': '당뇨병이 있어요.'},
               'ros_cough': {'mark': '-', 'turn': 0, 'quote': '당뇨병이 있어요.'},
               'pmh_allergy': {'mark': '-', 'turn': 36, 'quote': '약 알레르기는 없어요'}}
        self.assertEqual(record.checked_marks(raw, initial, events), {'pmh_dm': '+', 'pmh_allergy': '-'})

    def test_build_keeps_schema_and_only_delivered_education_without_extra_calls(self):
        events = [{'turn': 1, 'action': 'SAY', 'content': '기침이 있나요?', 'reply': '기침은 없어요'},
                  {'turn': 36, 'action': 'SAY', 'content': '어떤 점이 걱정되세요?', 'reply': '검사가 걱정돼요', 'explain': True},
                  {'turn': 37, 'action': 'SAY', 'content': '내일 다시 오세요', 'refused': 'ERROR', 'explain': True}]
        responses = [{'diagnosis': '모의 진단', 'education': '운전 금지를 설명함', 'tests': '검사 계획',
                      'follow_up': '내일 재진 계획'},
                     {'ros_cough': {'mark': '-', 'turn': 1, 'quote': '기침은 없어요'}},
                     {'hpi': '기침 부인함'}]
        ask = Mock(side_effect=[json.dumps(value, ensure_ascii=False) for value in responses])
        note, diagnosis = record.build(ask, SESSION, events)
        self.assertEqual(ask.call_count, 3)
        self.assertEqual(diagnosis, '모의 진단')
        self.assertEqual(note['P']['education'], '(턴 36) 어떤 점이 걱정되세요?')
        self.assertEqual(note['P']['follow_up'], '내일 재진 계획')
        self.assertEqual(note['P']['tests'], '검사 계획')
        self.assertEqual(set(note), set(RECORD_FIELDS))
        for section, fields in RECORD_FIELDS.items():
            self.assertEqual(set(note[section]), set(fields))
            self.assertTrue(all(isinstance(value, str) for value in note[section].values()))
        # 모델 호출이 불가능한 경로도 전달된 설명 원문을 보존한다.
        minimal, _ = record.minimal(SESSION, events)
        self.assertEqual(minimal['P']['education'], note['P']['education'])
        ask.reset_mock()
        budget_note, _ = record.build(ask, SESSION, events, time_left=lambda: 0)
        ask.assert_not_called()
        self.assertEqual(budget_note['P']['education'], note['P']['education'])
        self.assertEqual(record.education_text(events[:1]), '')


if __name__ == '__main__':
    unittest.main()
