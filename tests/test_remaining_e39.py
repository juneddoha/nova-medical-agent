"""Saved e39 failures and boundary cases. No clinical scoring or live calls."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

import explain
import record
import run


def completion(value):
    return {'choices': [{'message': {'content': json.dumps(value, ensure_ascii=False)}}]}


class ConditionalFollowupTest(unittest.TestCase):
    def test_after_discharge_condition_cannot_be_inverted_to_before_discharge(self):
        ask = Mock(side_effect=[json.dumps({'say': '퇴원 전에 1주 내 약물을 재평가해요'}),
                                json.dumps({'say': '퇴원 후 1주 내 약물을 재평가해요'})])
        self.assertEqual(explain.next_sentence(ask, '', explain.TOPICS[3],
            plan={'follow_up': '퇴원하면 1주 내 약물 재평가'}), '퇴원 후 1주 내 약물을 재평가해요')

    def test_outpatient_followup_after_emergency_sentence_is_not_lost(self):
        source = ('가장 가까운 일정은 지금 119 구급대에 의한 응급실 이송·평가임. '
                  '응급실 평가 결과에 따라 관찰·치료를 받고, 퇴원하면 1주 이내 외래에서 약물 사용과 감량 계획을 재평가함. '
                  '깨우기 어렵거나 호흡이 느려지면 즉시 119에 연락함.')
        ask = Mock(side_effect=[json.dumps({'say': '지금 응급실에서 약 용량을 확인하세요'}),
                                json.dumps({'say': '퇴원하면 1주 내 외래에서 감량을 상의해요'})])
        self.assertEqual(explain.next_sentence(ask, '', explain.TOPICS[3], plan={'follow_up': source}),
                         '퇴원하면 1주 내 외래에서 감량을 상의해요')

    def test_conditional_followup_cannot_become_unconditional(self):
        ask = Mock(side_effect=[json.dumps({'say': '1주 내 외래에서 약물을 재평가해요'}),
                                json.dumps({'say': '퇴원하면 1주 내 약물을 재평가해요'})])
        self.assertEqual(explain.next_sentence(ask, '', explain.TOPICS[3],
            plan={'follow_up': '퇴원하면 1주 내 외래에서 약물 재평가'}), '퇴원하면 1주 내 약물을 재평가해요')
        self.assertEqual(ask.call_count, 2)


class MeasurementProgressTest(unittest.TestCase):
    def test_unambiguous_numeric_units_without_a_label_are_not_marked_missing(self):
        for question, reply in [('혈압을 재겠습니다', '128/78 mmHg'),
                                ('호흡수를 세겠습니다', '18회/분'),
                                ('체중을 재겠습니다', '16.5kg'),
                                ('산소포화도를 측정하겠습니다', '98%')]:
            self.assertEqual(record.exam_feedback(question, reply), reply)

    def test_paraphrased_sitting_height_is_bounded_and_returned_height_is_not_sitting_height(self):
        first = {'turn': 23, 'action': 'EXAM', 'content': '앉은키를 재겠습니다',
                 'reply': '신장 105cm, 체중 16.5kg, 머리둘레 52cm'}
        second = first | {'turn': 30, 'content': '앉은키를 정확히 재겠습니다'}
        self.assertFalse(run.repeated_exam({}, second['content'], [first]))
        for text in ['앉은키를 앉은키 측정판으로 재겠습니다', '앉은키 측정판으로 앉은키를 재겠습니다']:
            self.assertTrue(run.repeated_exam({}, text, [first, second]))
        note, _ = record.minimal({'initial': {'opening': '키가 작음', 'vital_signs': ''}}, [first])
        self.assertIn('요청 측정치 미반환', note['O']['exam'])
        self.assertIn(first['reply'], note['O']['exam'])
        self.assertIn('요청 측정치 미반환', record.dialogue({'opening': '', 'vital_signs': ''}, [first]))

    def test_obtained_measurement_blocks_paraphrase_but_new_patient_fact_can_justify_repeat(self):
        first = {'turn': 1, 'action': 'EXAM', 'content': '산소포화도를 측정하겠습니다', 'reply': 'SpO2 98%'}
        text = '손가락에서 산소포화도를 재겠습니다'
        self.assertTrue(run.repeated_exam({}, text, [first]))
        history = [first, {'turn': 2, 'action': 'SAY', 'content': '상태 확인', 'reply': '새로 숨차요'}]
        self.assertFalse(run.repeated_exam({'repeat_turn': 2, 'repeat_quote': '새로 숨차요',
                                           'repeat_reason': '새 호흡 증상'}, text, history))
        self.assertTrue(run.repeated_exam({'repeat_turn': 2, 'repeat_quote': '아무 이상 없어요',
                                          'repeat_reason': '임의 사유'}, text, history))


class DatedNumericTest(unittest.TestCase):
    def test_all_saved_e39_summaries_survive_one_generation_without_erasing_fields(self):
        cases = json.loads((Path(__file__).parent / 'fixtures/live_e39_records.json').read_text(encoding='utf-8'))
        for case in cases:
            for story in case['stories']:
                with self.subTest(case=case['case'], hpi=story['hpi'][:50]):
                    expected = record.clean(story, record.STORY_KEYS)
                    self.assertEqual(record.unheard_numbers(expected, case['initial'], case['events']), [])
                    ask = Mock(return_value=json.dumps(story, ensure_ascii=False))
                    self.assertEqual(record.write_story(ask, case['initial'], case['events']), expected)
                    self.assertEqual(ask.call_count, 1)

    def test_daily_meal_denominator_is_not_an_unheard_illness_duration(self):
        for written in ['하루 세 끼와 간식', '하루 3끼와 간식', '하루 세 끼를 먹음']:
            story = dict.fromkeys(record.STORY_KEYS, '') | {'hpi': written}
            self.assertEqual(record.unheard_numbers(story, {'opening': '하루 세 끼 잘 먹어요'}, []), [])
        wrong = dict.fromkeys(record.STORY_KEYS, '') | {'hpi': '1일간 아팠음'}
        self.assertTrue(record.unheard_numbers(wrong, {'opening': '하루 세 끼 잘 먹어요'}, []))

    def test_derived_age_needs_birthdate_and_matching_explicit_measurement_date(self):
        events = [{'action': 'SAY', 'turn': 1, 'content': '생년월일은요?', 'reply': '2020년 5월 12일'},
                  {'action': 'SAY', 'turn': 2, 'content': '키를 잰 날짜는요?', 'reply': '2025년 10월'}]
        story = dict.fromkeys(record.STORY_KEYS, '') | {'hpi': '2025년 10월 5세 때 기록'}
        self.assertEqual(record.unheard_numbers(story, {'opening': '6세 남아'}, events), [])
        for text in ['2025년 10월 8세 때 기록', '2025년 10월 현재 5세', '5세 때 기록',
                     '2025년 10월 어머니 5세 때 기록']:
            self.assertTrue(record.unheard_numbers(story | {'hpi': text}, {'opening': '6세 남아'}, events))
        self.assertTrue(record.unheard_numbers(story, {'opening': '6세 남아'}, events[1:]))


class HandoffProgressTest(unittest.TestCase):
    def test_frequency_question_is_not_mistaken_for_duration(self):
        events = self.history() + [
            {'turn': 3, 'action': 'SAY', 'content': '처방 용량은요?', 'reply': '10mg'},
            {'turn': 4, 'action': 'SAY', 'content': '약 복용은 얼마나 자주 하세요?', 'reply': '매일요'}]
        calls = Mock(); calls.chat.return_value = completion({'action': 'DONE'})
        result = run.next_step(calls, {'initial': {'opening': '', 'vital_signs': ''}}, [], 10,
                               lambda: 60, events=events)
        self.assertIn('시작한 지', result[1])

    def test_unknown_handoff_answers_do_not_loop_or_prevent_finishing(self):
        events = self.history()
        calls = Mock(); calls.chat.return_value = completion({'action': 'DONE'})
        session = {'initial': {'opening': '약 복용', 'vital_signs': ''}}
        for n in range(3):
            step = run.next_step(calls, session, [], 10 - n, lambda: 60, events=events)
            self.assertEqual(step[0], 'SAY')
            events.append({'turn': 3 + n, 'action': 'SAY', 'content': step[1], 'reply': '잘 모르겠어요'})
        self.assertEqual(run.next_step(calls, session, [], 7, lambda: 60, events=events)[0], 'DONE')

    def test_medication_pack_and_emergency_transfer_are_distinct_actions(self):
        self.assertFalse(explain.repeated_guidance('약 포장을 구급대에 전달하세요', ['119에 연락하세요']))
        self.assertFalse(explain.repeated_guidance('구급대가 올 때까지 혼자 있지 마세요', ['119에 연락하세요']))

    def test_new_urgent_fact_allows_repeating_emergency_advice(self):
        history = self.history() + [{'turn': 3, 'action': 'SAY', 'content': '상태는요?', 'reply': '갑자기 숨이 안 쉬어져요'}]
        calls = Mock(); calls.chat.return_value = completion({'action': 'SAY', 'purpose': 'guidance',
            'content': '즉시 119에 연락하세요', 'urgent_reply': history[-1]['reply']})
        self.assertEqual(run.next_step(calls, {'initial': {'opening': '', 'vital_signs': ''}}, [], 10,
            lambda: 60, events=history)[1], '즉시 119에 연락하세요')

    def history(self):
        return [{'turn': 1, 'action': 'SAY', 'content': '복용량은요?', 'reply': '밤 40mg, 새벽 20mg'},
                {'turn': 2, 'action': 'SAY', 'content': '119에 연락하세요', 'reply': '네', 'purpose': 'guidance'}]

    def test_empty_done_after_medication_emergency_guidance_checks_missing_handoff_history(self):
        calls = Mock(); calls.chat.return_value = completion({'action': 'DONE', 'history_gap': ''})
        result = run.next_step(calls, {'initial': {'opening': '약 복용', 'vital_signs': ''}}, [], 10,
                               lambda: 60, events=self.history())
        self.assertEqual(result[0], 'SAY')
        self.assertIn('처방', result[1])
        self.assertEqual(calls.chat.call_count, 1)

    def test_cited_urgent_transfer_is_not_delayed_by_handoff_check(self):
        events = self.history() + [{'turn': 3, 'action': 'SAY', 'content': '상태는요?', 'reply': '지금 숨을 못 쉬겠어요'}]
        calls = Mock(); calls.chat.return_value = completion({'action': 'DONE',
            'urgent_transfer': {'turn': 3, 'quote': events[-1]['reply'], 'reason': '즉시 이송 우선'}})
        self.assertEqual(run.next_step(calls, {'initial': {'opening': '', 'vital_signs': ''}}, [], 10,
            lambda: 60, events=events)[0], 'DONE')

    def test_fabricated_urgent_quote_cannot_skip_handoff(self):
        calls = Mock(); calls.chat.return_value = completion({'action': 'DONE',
            'urgent_transfer': {'turn': 1, 'quote': '숨을 못 쉬어요', 'reason': '이송'}})
        self.assertEqual(run.next_step(calls, {'initial': {'opening': '', 'vital_signs': ''}}, [], 10,
            lambda: 60, events=self.history())[0], 'SAY')

    def test_semantically_repeated_emergency_guidance_is_replaced_before_sending(self):
        calls = Mock(); calls.chat.side_effect = [completion({'action': 'SAY', 'purpose': 'guidance',
            'content': '지금 119로 응급실에 가세요'}), completion({'action': 'SAY', 'content': '처방 용량은 얼마인가요?'})]
        result = run.next_step(calls, {'initial': {'opening': '', 'vital_signs': ''}}, [], 10,
                              lambda: 60, events=self.history())
        self.assertEqual(result[1], '처방 용량은 얼마인가요?')
        self.assertEqual(calls.chat.call_count, 2)


class PlanEvidenceTest(unittest.TestCase):
    def test_initial_patient_statement_is_a_valid_source_for_breathing_denial(self):
        ask = Mock(return_value=json.dumps({'diagnosis': '잠정 판단', 'evidence': '호흡곤란은 없음.'}))
        plan = record.prepare_plan(ask, {'initial': {'opening': '숨쉬는 건 괜찮고 호흡곤란은 없어요',
                                                     'vital_signs': ''}}, [])
        self.assertEqual(plan['evidence'], '호흡곤란은 없음.')

    def test_partial_sleepiness_answer_does_not_become_breathlessness_denial_in_A(self):
        events = [{'turn': 8, 'action': 'SAY', 'content': '지금 심하게 졸리거나 숨쉬기 힘드세요?',
                   'reply': '멍하지만 심하게 졸리진 않아요.'}]
        ask = Mock(return_value=json.dumps({'diagnosis': '잠정 판단',
            'evidence': '과량 복용 병력이 있음. 심한 졸림·호흡곤란은 부인함.'}))
        plan = record.prepare_plan(ask, {'initial': {'opening': '', 'vital_signs': ''}}, events)
        self.assertNotIn('호흡곤란은 부인', plan['evidence'])
        self.assertIn('호흡곤란', plan['evidence'])
        self.assertIn('미확인', plan['evidence'])
        self.assertIn('과량 복용 병력이 있음', plan['evidence'])
        self.assertEqual(ask.call_count, 1)

    def test_explicit_breathing_denial_is_kept_but_positive_answer_is_not_negative(self):
        for reply, accepted in [('아니요, 숨쉬는 건 괜찮아요', True), ('네, 숨이 차요', False),
                                ('숨이 차고 가슴 통증은 없어요', False)]:
            ask = Mock(return_value=json.dumps({'diagnosis': '잠정 판단', 'evidence': '호흡곤란은 없음.'}))
            events = [{'turn': 1, 'action': 'SAY', 'content': '숨쉬기 힘드세요?', 'reply': reply}]
            plan = record.prepare_plan(ask, {'initial': {'opening': '', 'vital_signs': ''}}, events)
            self.assertEqual('호흡곤란은 없음' in plan['evidence'], accepted)


class TreatmentActionTest(unittest.TestCase):
    def test_generic_deferral_is_rewritten_when_plan_has_a_concrete_support_option(self):
        ask = Mock(side_effect=[json.dumps({'say': '오늘 검사 후 체중 감소 원인 치료를 정하세요'}),
                                json.dumps({'say': '검사 후 증상 조절 약을 상의해요'})])
        text = explain.next_sentence(ask, '', explain.TOPICS[1], concern='체중이 줄어 걱정돼요',
            plan={'treatment': '검사 및 금기 확인 후 증상 조절 약물을 고려함. 안구 건조에는 인공눈물을 고려함.'})
        self.assertEqual(text, '검사 후 증상 조절 약을 상의해요')
        self.assertEqual(ask.call_count, 2)

    def test_missing_treatment_plan_does_not_force_an_invented_treatment(self):
        ask = Mock(return_value=json.dumps({'say': '검사 후 치료 방향을 정하겠습니다'}))
        self.assertEqual(explain.next_sentence(ask, '', explain.TOPICS[1], plan={}),
                         '검사 후 치료 방향을 정하겠습니다')


if __name__ == '__main__':
    unittest.main()
