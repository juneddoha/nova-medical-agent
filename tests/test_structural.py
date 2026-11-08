"""Observed failure regressions; no medical-quality or score assertions."""
import json
import unittest
from unittest.mock import Mock

import explain
import record
import run
from nova_sdk import NovaError


SESSION = {'initial': {'opening': '테스트 첫 진술', 'vital_signs': ''}, 'turns_remaining': 50}


def completion(value):
    return {'choices': [{'message': {'content': json.dumps(value, ensure_ascii=False)}}]}


class FormattingRegressionTest(unittest.TestCase):
    def test_observed_31_character_answer_keeps_words_without_regeneration(self):
        sentences = [
            '줄일 수 있지만 오늘은 과량 복용으로 응급실부터 가세요.',
            '갑자기 끊지 않도록 돕고, 응급실서 약 영향을 확인해요.',
            '응급실서 약 영향을 확인하고, 갑자기 끊지 않게 도와요.',
        ]
        for text in sentences:
            with self.subTest(text=text):
                self.assertEqual(len(text), 31)
                ask = Mock(side_effect=[json.dumps({'say': text}, ensure_ascii=False),
                                        json.dumps({'say': '의존이 의심돼요.'}, ensure_ascii=False)])
                sentence = explain.next_sentence(ask, '대화', '안내', patient_reply='줄일 수 있나요?')
                self.assertEqual(sentence, text[:-1])
                self.assertEqual(ask.call_count, 1)

    def test_formatting_does_not_cut_words_decimal_or_question_mark(self):
        self.assertEqual(explain.usable('용량은 1.5mg인가요?'), '용량은 1.5mg인가요?')
        self.assertIsNone(explain.usable('가' * 30 + '?'))
        self.assertIsNone(explain.usable('가' * 31 + '...'))


class ExamProgressTest(unittest.TestCase):
    def test_exact_repeat_is_not_sent_and_new_method_can_follow(self):
        calls, clock = Mock(), Mock()
        calls.need.return_value = 0
        clock.left.return_value = 1800
        original, revised = '산소포화도를 측정하겠습니다', '손가락 산소포화도를 측정하겠습니다'
        calls.chat.side_effect = [completion({'action': 'EXAM', 'content': text})
                                  for text in (original, original, revised)] + [completion({'action': 'DONE'})]
        calls.exam.side_effect = lambda *a, **kw: {'turn': calls.exam.call_count,
            'result': '요청과 다른 활력징후' if calls.exam.call_count == 1 else 'SpO2 98%',
            'turns_remaining': 50 - calls.exam.call_count}
        events = []
        run.interview(calls, SESSION, events, clock)
        self.assertEqual(calls.exam.call_count, 2)
        self.assertEqual([event['turn'] for event in events], [1, 2])
        self.assertEqual(events[-1]['content'], revised)

    def test_refused_exam_is_blocked_even_with_a_repeat_reason(self):
        history = [{'turn': 1, 'action': 'EXAM', 'content': '걸음걸이와 균형을 확인하겠습니다',
                    'refused': 'EXAM_NOT_SPECIFIC'},
                   {'turn': 2, 'action': 'SAY', 'content': '새 증상', 'reply': '새로 어지러워요'}]
        calls = Mock()
        calls.chat.return_value = completion({'action': 'EXAM', 'content': history[0]['content'],
            'repeat_reason': '새 증상', 'repeat_turn': 2, 'repeat_quote': '새로 어지러워요'})
        with self.assertRaises(run.ActionFormatError):
            run.next_step(calls, SESSION, [], 10, lambda: 60, events=history)
        self.assertEqual(calls.chat.call_count, 3)
        calls.exam.assert_not_called()

    def test_new_patient_change_can_justify_repeating_a_successful_exam(self):
        history = [{'turn': 1, 'action': 'EXAM', 'content': '호흡수를 세겠습니다', 'reply': '18회/분'},
                   {'turn': 2, 'action': 'SAY', 'content': '새 증상', 'reply': '새로 숨이 차요'}]
        calls = Mock()
        calls.chat.return_value = completion({'action': 'EXAM', 'content': history[0]['content'],
            'repeat_reason': '새 호흡 증상', 'repeat_turn': 2, 'repeat_quote': '새로 숨이 차요'})
        self.assertEqual(run.next_step(calls, SESSION, [], 10, lambda: 60, events=history)[0], 'EXAM')

    def test_invented_repeat_quote_cannot_spend_another_turn(self):
        history = [{'turn': 1, 'action': 'EXAM', 'content': '호흡수를 세겠습니다', 'reply': '18회/분'},
                   {'turn': 2, 'action': 'SAY', 'content': '새 증상', 'reply': '괜찮아요'}]
        calls = Mock()
        calls.chat.return_value = completion({'action': 'EXAM', 'content': history[0]['content'],
            'repeat_reason': '새 증상', 'repeat_turn': 2, 'repeat_quote': '새로 숨이 차요'})
        with self.assertRaises(run.ActionFormatError):
            run.next_step(calls, SESSION, [], 10, lambda: 60, events=history)


class NegativeEvidenceTest(unittest.TestCase):
    def test_general_denial_does_not_become_unasked_disease_negatives(self):
        reply = '아니요, 고혈압 말고는 없어요.'
        events = [{'turn': 22, 'action': 'SAY', 'content': '고혈압 외에 앓고 계신 병이 있나요?', 'reply': reply}]
        raw = {key: {'mark': '-', 'turn': 22, 'quote': reply}
               for key in ('pmh_dm', 'pmh_stroke', 'pmh_tbc', 'pmh_hepatitis', 'pmh_heart')}
        self.assertEqual(record.checked_marks(raw, SESSION['initial'], events), {})

    def test_targeted_denial_and_patient_named_condition_are_retained(self):
        events = [{'turn': 1, 'action': 'SAY', 'content': '약물 알레르기가 있나요?', 'reply': '없어요'},
                  {'turn': 2, 'action': 'SAY', 'content': '다른 증상은요?', 'reply': '기침은 없어요'}]
        raw = {'pmh_allergy': {'mark': '-', 'turn': 1, 'quote': '없어요'},
               'ros_cough': {'mark': '-', 'turn': 2, 'quote': '기침은 없어요'}}
        self.assertEqual(record.checked_marks(raw, SESSION['initial'], events),
                         {'pmh_allergy': '-', 'ros_cough': '-'})


class SharedPlanTest(unittest.TestCase):
    def test_full_consult_does_not_explain_a_different_independent_diagnosis(self):
        client = Mock()
        client.timeout = 240
        client.session.return_value = SESSION

        def chat(messages, **kwargs):
            system, user = (message['content'] for message in messages)
            if '외래 진료실' in system:
                value = {'action': 'DONE'}
            elif 'A(평가)' in system:
                value = {'diagnosis': '공통 판단', 'tests': '검사 목적'}
            elif '항목마다' in system:
                value = {'plan_update': None}
            elif 'S(환자에게' in system:
                value = {'hpi': '모의 병력'}
            else:
                value = {'say': '계획에 따른 안내입니다' if '공통 판단' in user else '다른 진단이 의심돼요'}
            return completion(value)

        client.chat.side_effect = chat
        client.say.side_effect = lambda text, **kw: {'turn': client.say.call_count,
            'content': '네', 'effective_content': text, 'turns_remaining': 50 - client.say.call_count}
        client.diagnose.return_value = {'status': 'completed'}
        self.assertTrue(run.consult(client, None))
        self.assertEqual(client.say.call_args_list[0].args[0], '공통 판단 가능성이 있어요')
        self.assertEqual(client.diagnose.call_args.kwargs['diagnosis'], '공통 판단')
        self.assertEqual(sum('A(평가)' in call.args[0][0]['content'] for call in client.chat.call_args_list), 1)

    def test_final_record_reuses_plan_without_independent_diagnosis_generation(self):
        plan = {'diagnosis': '공통 판단', 'tests': '검사 목적', 'follow_up': '내일 재평가'}
        ask = Mock(side_effect=[json.dumps({'plan_update': None}), json.dumps({'hpi': '모의 병력'})])
        note, diagnosis = record.build(ask, SESSION, [], prepared_plan=plan, plan_turn=0)
        self.assertEqual((diagnosis, note['P']['tests']), ('공통 판단', '검사 목적'))
        self.assertEqual(ask.call_count, 2)

    def test_first_explanation_names_shared_diagnosis_without_choosing_another(self):
        calls, clock = Mock(), Mock()
        calls.need.return_value = 0
        calls.record_need.return_value = 100
        clock.left.return_value = 1800
        calls.chat.side_effect = [completion({'say': text}) for text in (
            '수집한 병력을 근거로 판단했어요', '검사로 원인을 확인할게요',
            '내일 재평가를 받으세요', '심해지면 바로 응급실로 오세요')]
        calls.say.side_effect = lambda *a, **kw: {'turn': calls.say.call_count,
            'content': '네', 'turns_remaining': 50 - calls.say.call_count}
        plan = {'diagnosis': '공통 판단', 'tests': '검사 목적', 'follow_up': '내일 재평가'}
        run.explain_to_patient(calls, SESSION, [], clock, plan=plan)
        self.assertEqual(calls.say.call_args_list[0].args[0], '공통 판단 가능성이 있어요')
        self.assertEqual(calls.chat.call_count, 4)
        self.assertIn('검사 목적', calls.chat.call_args_list[0].args[0][1]['content'])

    def test_verified_new_patient_fact_can_change_plan(self):
        events = [{'turn': 2, 'action': 'SAY', 'content': '새 증상 확인',
                   'reply': '새로 숨이 차요', 'explain': True}]
        ask = Mock(side_effect=[json.dumps({'plan_update': {'turn': 2, 'quote': '새로 숨이 차요'}}),
                                json.dumps({'diagnosis': '새 사실에 따른 판단', 'evidence': '새 증상'}),
                                json.dumps({'hpi': '새 증상 진술'})])
        note, diagnosis = record.build(ask, SESSION, events,
                                      prepared_plan={'diagnosis': '이전 판단'}, plan_turn=1)
        self.assertEqual(diagnosis, '새 사실에 따른 판단')
        self.assertIn('새로 숨이 차요', note['A']['evidence'])
        self.assertEqual(ask.call_count, 3)

    def test_wrong_quote_does_not_trigger_plan_regeneration(self):
        events = [{'turn': 2, 'action': 'SAY', 'content': '안내', 'reply': '네', 'explain': True}]
        ask = Mock(side_effect=[json.dumps({'plan_update': {'turn': 2, 'quote': '없는 증상'}}),
                                json.dumps({'hpi': '모의 병력'})])
        _, diagnosis = record.build(ask, SESSION, events,
                                    prepared_plan={'diagnosis': '공통 판단'}, plan_turn=1)
        self.assertEqual(diagnosis, '공통 판단')
        self.assertEqual(ask.call_count, 2)

    def test_no_record_budget_keeps_prepared_plan_without_more_model_calls(self):
        ask = Mock()
        note, diagnosis = record.build(ask, SESSION, [], time_left=lambda: 0,
                                      prepared_plan={'diagnosis': '공통 판단', 'tests': '확인 계획'})
        ask.assert_not_called()
        self.assertEqual(diagnosis, '공통 판단')
        self.assertEqual(note['P']['tests'], '확인 계획')

    def test_failed_plan_update_records_uncertainty_and_preserves_new_reply(self):
        events = [{'turn': 2, 'action': 'SAY', 'content': '새 증상',
                   'reply': '새로 숨이 차요', 'explain': True}]
        ask = Mock(side_effect=[json.dumps({'plan_update': {'turn': 2, 'quote': '새로 숨이 차요'}}),
                                NovaError('MODEL_CALL_LIMIT', 429), json.dumps({'hpi': '새로 숨이 차다고 함'})])
        note, diagnosis = record.build(ask, SESSION, events,
                                      prepared_plan={'diagnosis': '기존 판단'}, plan_turn=1)
        self.assertEqual(diagnosis, '기존 판단')
        self.assertIn('재평가 미완료', note['A']['evidence'])
        self.assertIn('새로 숨이 차요', note['A']['evidence'])


if __name__ == '__main__':
    unittest.main()
