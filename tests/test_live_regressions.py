"""Saved public conversations are local regression data, never Doctor input assets."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

import record
import explain
import run


CASES = json.loads((Path(__file__).parent / 'fixtures/live_e27_records.json').read_text(encoding='utf-8'))
FOLLOWUP = json.loads((Path(__file__).parent / 'fixtures/live_e33_records.json').read_text(encoding='utf-8'))
FINAL_CASES = json.loads((Path(__file__).parent / 'fixtures/live_e35_records.json').read_text(encoding='utf-8'))


class LiveRecordRegressionTest(unittest.TestCase):
    def test_followup_live_summaries_keep_parent_units_time_frames_and_frequency(self):
        for case in FOLLOWUP:
            for index, raw in enumerate(case['stories']):
                with self.subTest(case=case['case'], attempt=index):
                    expected = record.clean(raw, record.STORY_KEYS)
                    self.assertEqual(record.unheard_numbers(expected, case['initial'], case['events']), [])

    def test_question_time_frame_requires_an_answer_and_cannot_supply_drug_quantity(self):
        initial = {'opening': ''}
        cases = [
            ('지난 24시간 총 몇 mg 드셨나요?', '총 60mg 먹었어요', '지난 24시간 60mg', False),
            ('지난 24시간 총 몇 mg 드셨나요?', '잘 모르겠어요', '지난 24시간 60mg', True),
            ('지난 3일간 5mg 드신 건가요?', '아니요', '3일간 5mg', True),
        ]
        for question, reply, text, bad in cases:
            with self.subTest(question=question, reply=reply):
                events = [{'turn': 1, 'action': 'SAY', 'content': question, 'reply': reply}]
                self.assertEqual(bool(record.unheard_numbers(dict.fromkeys(record.STORY_KEYS, '') | {'hpi': text}, initial, events)), bad)

    def test_followup_clear_marks_are_not_lost_in_mixed_answers(self):
        expected = {'C03': {'ros_weakness': '-'}, 'C10': {'sh_alcohol': '+', 'ros_fatigue': '+',
                    'ros_chest_pain': '-', 'ros_anorexia': '-'}}
        for case in FOLLOWUP:
            kept = record.checked_marks(case['proposed_marks'], case['initial'], case['events'])
            for key, value in expected.get(case['case'], {}).items():
                with self.subTest(case=case['case'], key=key):
                    self.assertEqual(kept.get(key), value)

    def test_saved_valid_summaries_keep_numbers_and_fields_without_regeneration(self):
        for case in CASES + FOLLOWUP + FINAL_CASES:
            for index, raw in enumerate(case['stories']):
                with self.subTest(case=case['case'], attempt=index):
                    expected = record.clean(raw, record.STORY_KEYS)
                    self.assertEqual(record.unheard_numbers(expected, case['initial'], case['events']), [])
                    ask = Mock(return_value=json.dumps(raw, ensure_ascii=False))
                    self.assertEqual(record.write_story(ask, case['initial'], case['events']), expected)
                    self.assertEqual(ask.call_count, 1)

    def test_clear_saved_marks_survive_while_unasked_disease_negatives_do_not(self):
        for case in CASES:
            with self.subTest(case=case['case']):
                self.assertEqual(record.checked_marks(case['proposed_marks'], case['initial'], case['events']),
                                 case['expected_valid_marks'])

    def test_full_record_keeps_medicine_family_and_social_summaries(self):
        for case in CASES + FOLLOWUP + FINAL_CASES:
            with self.subTest(case=case['case']):
                story = case['stories'][-1]
                ask = Mock(side_effect=[json.dumps(case['proposed_marks']), json.dumps(story)])
                note, diagnosis = record.build(ask, {'initial': case['initial']}, case['events'],
                                              prepared_plan={'diagnosis': '테스트 잠정 판단'}, plan_turn=40)
                self.assertEqual(ask.call_count, 2)
                self.assertEqual(note['S']['hpi'], story['hpi'])
                for key, field in [('medicine', 'pmh'), ('fh_others', 'fh'), ('sh_others', 'sh')]:
                    if story[key]:
                        self.assertIn(story[key], note['S'][field])

    def test_corrupt_field_cannot_erase_valid_family_or_medicine(self):
        story = dict.fromkeys(record.STORY_KEYS, '') | {
            'hpi': '키 99cm', 'medicine': '암로디핀 복용', 'fh_others': '아버지는 어릴 때 작았음'}
        initial = {'opening': '키 170cm', 'vital_signs': ''}
        ask = Mock(return_value=json.dumps(story))
        result = record.write_story(ask, initial, [])
        self.assertIsNotNone(result)
        self.assertEqual(result['medicine'], story['medicine'])
        self.assertEqual(result['fh_others'], story['fh_others'])
        self.assertNotIn('99cm', result['hpi'])
        self.assertIn('키 170cm', result['hpi'])

    def test_multiple_denials_keep_each_named_condition_and_opposite_marks_fail(self):
        for reply in ['기침은 없고 가래도 없어요', '기침은 있고 가래는 없어요']:
            events = [{'turn': 1, 'action': 'SAY', 'content': '기침과 가래는요?', 'reply': reply}]
            for key, mark in [('ros_cough', '+' if '있고' in reply else '-'), ('ros_sputum', '-')]:
                with self.subTest(reply=reply, key=key):
                    evidence = {'turn': 1, 'quote': reply}
                    self.assertEqual(record.checked_marks({key: evidence | {'mark': mark}}, {}, events), {key: mark})
                    self.assertEqual(record.checked_marks({key: evidence | {'mark': '-' if mark == '+' else '+'}}, {}, events), {})

    def test_normalized_numbers_accept_ranges_and_clock_but_not_rebound_values(self):
        cases = [
            ('밤 11시에 누워요', '23시에 취침', False),
            ('서너 알로 늘었어요', '3~4정 복용', False),
            ('엄마 165, 아빠 178cm', '어머니 178cm, 아버지 165cm', True),
            ('아침 5mg, 밤 10mg', '아침 10mg, 밤 5mg', True),
            ('처방 1정, 실제 6정', '처방 6정, 실제 1정', True),
            ('키 170cm, 체중 60kg', '키 60cm, 체중 170kg', True),
            ('원래 체중 56kg, 지금은 49kg', '현재 체중 56kg, 원래 49kg', True),
        ]
        for source, written, bad in cases:
            with self.subTest(source=source, written=written):
                story = dict.fromkeys(record.STORY_KEYS, '') | {'hpi': written}
                self.assertEqual(bool(record.unheard_numbers(story, {'opening': source}, [])), bad)

    def test_repeated_explanation_is_rewritten_within_existing_two_attempts(self):
        ask = Mock(side_effect=[json.dumps({'say': '원인을 확인할게요'}),
                                json.dumps({'say': '혈액검사로 원인을 좁힐게요'})])
        result = explain.next_sentence(ask, '대화', '필요 검사', previous=['원인을 확인할게요'])
        self.assertEqual(result, '혈액검사로 원인을 좁힐게요')
        self.assertEqual(ask.call_count, 2)

    def test_repeated_explanation_is_not_sent_after_bounded_attempts(self):
        ask = Mock(return_value=json.dumps({'say': '원인을 확인할게요'}))
        self.assertIsNone(explain.next_sentence(ask, '대화', '이유', previous=['원인을 확인할게요']))
        self.assertEqual(ask.call_count, 2)

    def test_observed_compound_exams_are_repaired_before_spending_a_turn(self):
        for content in ('얼굴과 사지를 살펴 기형 소견을 확인하겠습니다', '눈의 돌출과 안구운동을 확인하겠습니다'):
            with self.subTest(content=content):
                calls = Mock()
                calls.chat.side_effect = [{'choices': [{'message': {'content': json.dumps(value)}}]}
                    for value in ({'action': 'EXAM', 'content': content},
                                  {'action': 'EXAM', 'content': '안구 돌출을 관찰하겠습니다'})]
                selected = run.next_step(calls, {'initial': {'opening': '', 'vital_signs': ''}}, [], 10, lambda: 60)
                self.assertEqual(selected[:2], ('EXAM', '안구 돌출을 관찰하겠습니다'))
                self.assertEqual(calls.chat.call_count, 2)
                calls.exam.assert_not_called()

    def test_single_palpation_with_multiple_findings_is_allowed(self):
        text = '목을 만져 갑상샘 크기와 결절을 확인하겠습니다'
        calls = Mock()
        calls.chat.return_value = {'choices': [{'message': {'content': json.dumps({'action': 'EXAM', 'content': text})}}]}
        self.assertEqual(run.next_step(calls, {'initial': {'opening': '', 'vital_signs': ''}}, [], 10, lambda: 60)[1], text)
        self.assertEqual(calls.chat.call_count, 1)

    def test_saved_whole_body_generic_observation_is_repaired_before_sending(self):
        text = '전신 외형에서 특징적인 얼굴·신체 기형을 관찰하겠습니다'
        self.assertTrue(run.compound_exam(text))
        self.assertFalse(run.compound_exam('전신 피부의 발진 분포를 관찰하겠습니다'))

    def test_latest_clear_symptoms_and_explicit_plural_denials_keep_their_marks(self):
        expected = {'C03': {'ros_abdominal_pain': '-', 'ros_diarrhea': '-'},
                    'C10': {'ros_chest_pain': '-', 'ros_diarrhea': '+'}}
        for case in FINAL_CASES:
            kept = record.checked_marks(case['proposed_marks'], case['initial'], case['events'])
            for key, mark in expected.get(case['case'], {}).items():
                with self.subTest(case=case['case'], key=key):
                    self.assertEqual(kept.get(key), mark)
                    wrong = {key: case['proposed_marks'][key] | {'mark': '+' if mark == '-' else '-'}}
                    self.assertEqual(record.checked_marks(wrong, case['initial'], case['events']), {})


if __name__ == '__main__':
    unittest.main()
