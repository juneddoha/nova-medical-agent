"""Adversarial source/meaning regressions. No API or clinical score assertions."""
import json
import unittest
from unittest.mock import Mock

import record
import run
from nova_sdk import NovaError


INITIAL = {'opening': '', 'vital_signs': ''}


class PolarityTest(unittest.TestCase):
    def check(self, key, question, reply, mark):
        return record.checked_marks({key: {'mark': mark, 'turn': 1, 'quote': reply}}, INITIAL,
            [{'turn': 1, 'action': 'SAY', 'content': question, 'reply': reply}])

    def test_opposite_or_uncertain_marks_are_not_accepted(self):
        cases = [
            ('ros_cough', '기침이 있나요?', '기침은 조금 있어요', '-'),
            ('ros_cough', '기침이 있나요?', '기침은 없어요', '+'),
            ('ros_cough', '기침이 있나요?', '기침은 심하지 않아요', '-'),
            ('ros_cough', '기침이 있나요?', '기침이 없는 건 아니에요', '-'),
            ('ros_cough', '기침이 있나요?', '기침은 없지는 않아요', '-'),
            ('ros_cough', '기침이 있나요?', '기침은 잘 모르겠어요', '-'),
            ('ros_cough', '기침이 있나요?', '기침은 거의 없어요', '-'),
            ('ros_cough', '기침과 가래가 있나요?', '없어요', '-'),
            ('ros_cough', '기침은 없으시죠?', '네', '-'),
            ('pmh_dm', '당뇨가 있나요?', '아버지는 당뇨가 있어요', '+'),
            ('ros_cough', '기침과 가래는요?', '기침은 있고 가래는 없어요', '-'),
            ('ros_sputum', '기침과 가래는요?', '기침은 있고 가래는 없어요', '+'),
            ('ros_weakness', '기운이 없나요?', '기운이 없어요', '-'),
            ('ros_anorexia', '식욕은요?', '식욕이 없어요', '-'),
            ('sh_alcohol', '술은요?', '수술 받은 적은 없어요', '-'),
            ('ros_fever', '다른 증상은요?', '열흘 전부터 기침만 있어요', '+'),
            ('ros_cough', '기침은요?', '기침 없다고 의사가 말했어요', '-'),
            ('ros_cough', '기침은요?', '기침이 없을 리가 없어요', '-'),
            ('ros_cough', '기침은요?', '기침은 없어요?', '-'),
            ('fh_dm', '가족 당뇨가 있나요?', '저는 당뇨가 있어요', '+'),
            ('sh_smoking', '담배 피우세요?', '담배는 집에 있어요', '+'),
            ('sh_alcohol', '술 드세요?', '술은 집에 있어요', '+'),
            ('pmh_op', '수술 받으셨나요?', '수술 예정이 있어요', '+'),
        ]
        for key, question, reply, mark in cases:
            with self.subTest(reply=reply, key=key, mark=mark):
                self.assertEqual(self.check(key, question, reply, mark), {})

    def test_clear_targeted_assertions_survive(self):
        cases = [
            ('ros_cough', '기침이 있나요?', '기침은 조금 있어요', '+'),
            ('ros_cough', '기침이 있나요?', '가끔 기침이 나요', '+'),
            ('ros_cough', '기침이 있나요?', '없어요', '-'),
            ('pmh_allergy', '약 알레르기는요?', '없어요', '-'),
            ('ros_cough', '기침과 가래는요?', '기침은 있고 가래는 없어요', '+'),
            ('ros_sputum', '기침과 가래는요?', '기침은 있고 가래는 없어요', '-'),
            ('ros_weakness', '기운은요?', '기운이 없어요', '+'),
            ('ros_anorexia', '식욕은요?', '식욕이 없어요', '+'),
            ('ros_cough', '기침이 있나요?', '아니요', '-'),
            ('ros_cough', '기침이 있나요?', '네', '+'),
            ('sh_smoking', '담배 피우세요?', '담배 안 피워요', '-'),
        ]
        for key, question, reply, mark in cases:
            with self.subTest(reply=reply, key=key):
                self.assertEqual(self.check(key, question, reply, mark), {key: mark})


class NumericBindingTest(unittest.TestCase):
    def story(self, text):
        return dict.fromkeys(record.STORY_KEYS, '') | {'hpi': text}

    def events(self, reply, question='수치를 알려주세요'):
        return [{'turn': 1, 'action': 'SAY', 'content': question, 'reply': reply}]

    def test_known_values_cannot_be_rebound_or_borrowed_from_questions(self):
        cases = [
            ('키 170cm, 체중 60kg입니다', '키 60cm, 체중 170kg', '수치는요?'),
            ('처방은 1정, 실제는 6정 먹어요', '처방 6정, 실제 1정', '복용량은요?'),
            ('수축기 120mmHg, 이완기 80mmHg', '수축기 80mmHg, 이완기 120mmHg', '혈압은요?'),
            ('아니요, 모르겠어요', '3일 전부터 5mg 복용', '3일 전부터 5mg인가요?'),
            ('몸무게는 60kg입니다', '체중 60mg', '체중은요?'),
            ('졸피뎀 5mg, 멜라토닌 10mg', '졸피뎀 10mg, 멜라토닌 5mg', '약은요?'),
        ]
        for reply, written, question in cases:
            with self.subTest(written=written):
                self.assertTrue(record.unheard_numbers(self.story(written), INITIAL, self.events(reply, question)))

    def test_spacing_aliases_and_korean_numbers_keep_their_units(self):
        cases = [
            ('키는 백칠십 센티미터, 체중은 육십 킬로그램', '키 170 cm, 체중 60 kg'),
            ('2.50 mg을 2정 복용해요', '2.5mg 2정 복용'),
            ('한 알 먹었어요', '1정 복용'),
            ('몸무게 60kg, 키 170cm', '신장 170cm, 체중 60kg'),
            ('처방은 1정이고 실제는 6정입니다', '실제 6정, 처방 1정'),
            ('평소 5~6정 복용', '평소 5~6정 복용'),
            ('졸피뎀은 5mg, 멜라토닌은 10mg', '멜라토닌 10mg, 졸피뎀 5mg'),
            ('1,000mg 복용', '1000mg 복용'),
        ]
        for reply, written in cases:
            with self.subTest(reply=reply):
                self.assertEqual(record.unheard_numbers(self.story(written), INITIAL, self.events(reply)), [])

    def test_failed_numeric_repair_never_returns_a_still_invalid_story(self):
        bad = json.dumps(self.story('키 60cm, 체중 170kg'))
        ask = Mock(side_effect=[bad, bad])
        self.assertIsNone(record.write_story(ask, INITIAL, self.events('키 170cm, 체중 60kg')))

    def test_partial_repair_is_not_accepted(self):
        ask = Mock(side_effect=[json.dumps(self.story('키 99cm, 체중 88kg')),
                                json.dumps(self.story('키 170cm, 체중 88kg'))])
        self.assertIsNone(record.write_story(ask, INITIAL, self.events('키 170cm, 체중 60kg')))

    def test_objective_measurement_is_not_patient_report(self):
        events = [{'turn': 1, 'action': 'EXAM', 'content': '산소포화도 측정', 'reply': '98%'}]
        self.assertTrue(record.unheard_numbers(self.story('산소포화도 98%라고 함'), INITIAL, events))

    def test_question_units_do_not_supply_a_missing_patient_quantity(self):
        self.assertTrue(record.unheard_numbers(self.story('졸피뎀 5mg'), INITIAL,
            self.events('잘 모르겠어요', '졸피뎀 5mg인가요?')))

    def test_repair_timeout_falls_back_to_verbatim_sources(self):
        ask = Mock(return_value=json.dumps(self.story('키 60cm, 체중 170kg')))
        budgets = iter([True, False])
        self.assertIsNone(record.write_story(ask, INITIAL, self.events('키 170cm, 체중 60kg'),
                                           enough_time=lambda: next(budgets)))
        self.assertEqual(ask.call_count, 1)


class FinalExplanationTest(unittest.TestCase):
    def test_missing_server_remaining_preserves_the_five_reserved_slots(self):
        calls, clock = Mock(), Mock()
        calls.need.return_value = 0
        clock.left.return_value = 1800
        calls.chat.return_value = {'choices': [{'message': {'content': '{"action":"SAY","content":"질문입니다"}'}}]}
        calls.say.side_effect = lambda *a, **kw: {'turn': calls.say.call_count, 'content': '네'}
        events = []
        run.interview(calls, {'initial': INITIAL, 'turns_remaining': 50}, events, clock)
        self.assertEqual((len(events), events[-1]['turns_remaining']), (35, 15))

    def test_missing_remaining_does_not_hide_server_exhaustion(self):
        calls, clock = Mock(), Mock()
        calls.need.return_value = 0
        calls.record_need.return_value = 100
        clock.left.return_value = 1800
        calls.chat.return_value = {'choices': [{'message': {'content': '{"say":"안내입니다"}'}}]}
        calls.say.side_effect = lambda *a, **kw: {'turn': calls.say.call_count, 'content': '네'}
        events = []
        run.explain_to_patient(calls, {'initial': INITIAL, 'turns_remaining': 1}, events, clock)
        self.assertEqual(calls.say.call_count, 1)

    def test_missing_plan_does_not_produce_an_empty_diagnosis_sentence(self):
        calls, clock = Mock(), Mock()
        calls.need.return_value = 0
        calls.record_need.return_value = 100
        clock.left.return_value = 1800
        calls.chat.return_value = {'choices': [{'message': {'content': '{"say":"추가 확인이 필요해요"}'}}]}
        calls.say.return_value = {'turn': 1, 'content': '네', 'turns_remaining': 0}
        run.explain_to_patient(calls, {'initial': INITIAL}, [], clock,
                               plan={'diagnosis': ''}, topics=(run.explain.TOPICS[0],))
        self.assertEqual(calls.say.call_args.args[0], '추가 확인이 필요해요')

    def consult(self, changed_turn=2, *, remaining=50, final_refused=False, long_diagnosis=False):
        client = Mock(timeout=240)
        client.session.return_value = {'initial': INITIAL, 'turns_remaining': remaining}
        plans = []
        new_diagnosis = '새 판단' if not long_diagnosis else '매우 긴 상세 임상 진단명으로 서술한 새로운 잠정적 판단입니다'

        def chat(messages, **kwargs):
            system = messages[0]['content']
            user = messages[1]['content']
            if '외래 진료실' in system:
                value = {'action': 'DONE'}
            elif 'A(평가)' in system:
                value = {'diagnosis': new_diagnosis if '새로 실신했어요' in user else '이전 판단',
                         'follow_up': '오늘 재평가'}
                plans.append(value)
            elif '항목마다' in system:
                value = {'plan_update': {'turn': changed_turn, 'quote': '새로 실신했어요'}
                         if '새로 실신했어요' in user else None}
            elif 'S(환자에게' in system:
                value = {'hpi': '환자 진술 원문에 근거함'}
            elif '\n이번 안내 목적(참고): ' + run.explain.TOPICS[3] in user:
                value = {'say': '오늘 재진해 상태를 재평가할게요'}
            else:
                value = {'say': f'계획에 따른 안내 {client.chat.call_count}'}
            return {'choices': [{'message': {'content': json.dumps(value, ensure_ascii=False)}}]}

        def say(text, **kwargs):
            n = client.say.call_count
            if final_refused and n == 5:
                raise NovaError('SAY_TOO_LONG', 422)
            return {'turn': n, 'content': '새로 실신했어요' if n == changed_turn else '네',
                    'effective_content': text, 'turns_remaining': remaining - n}

        client.chat.side_effect, client.say.side_effect = chat, say
        client.diagnose.return_value = {'status': 'completed'}
        self.assertTrue(run.consult(client, None))
        return client, plans

    def test_revised_diagnosis_is_spoken_before_submission_within_five_slots(self):
        client, plans = self.consult()
        self.assertEqual(client.diagnose.call_args.kwargs['diagnosis'], '새 판단')
        self.assertIn('새 판단', client.say.call_args_list[-1].args[0])
        self.assertEqual(client.say.call_count, 5)
        self.assertEqual(len(plans), 2)

    def test_last_reply_cannot_silently_change_the_record_or_add_a_sixth_say(self):
        client, _ = self.consult(changed_turn=5)
        submitted = client.diagnose.call_args.kwargs
        self.assertEqual(submitted['diagnosis'], '새 판단')
        self.assertIn('추가 설명 미완료', submitted['note']['A']['evidence'])
        self.assertEqual(client.say.call_count, 5)
        self.assertNotIn('새 판단', submitted['note']['P']['education'])

    def test_server_exhaustion_does_not_restart_explanation(self):
        client, _ = self.consult(remaining=4)
        self.assertEqual(client.say.call_count, 4)
        self.assertIn('추가 설명 미완료', client.diagnose.call_args.kwargs['note']['A']['evidence'])

    def test_refused_revised_explanation_is_not_claimed_as_delivered(self):
        client, _ = self.consult(final_refused=True)
        submitted = client.diagnose.call_args.kwargs
        self.assertNotIn('새 판단', submitted['note']['P']['education'])
        self.assertIn('추가 설명 미완료', submitted['note']['A']['evidence'])

    def test_generic_model_sentence_cannot_claim_delivery_of_a_long_revised_diagnosis(self):
        client, _ = self.consult(long_diagnosis=True)
        submitted = client.diagnose.call_args.kwargs
        self.assertIn('추가 설명 미완료', submitted['note']['A']['evidence'])

    def test_final_new_fact_is_preserved_even_if_story_omits_it(self):
        client, _ = self.consult(changed_turn=5)
        self.assertIn('새로 실신했어요', client.diagnose.call_args.kwargs['note']['S']['hpi'])

    def test_fortieth_turn_stays_the_last_turn(self):
        calls, clock = Mock(), Mock()
        calls.need.return_value = 0
        calls.record_need.return_value = 100
        clock.left.return_value = 1800
        calls.chat.side_effect = lambda *a, **kw: {'choices': [{'message': {'content': json.dumps({'say': f'안내 {calls.chat.call_count}'})}}]}
        calls.say.side_effect = lambda *a, **kw: {'turn': 35 + calls.say.call_count,
            'content': '네', 'effective_content': a[0], 'turns_remaining': 15 - calls.say.call_count}
        events = [{'turn': 35, 'action': 'SAY', 'content': '문진 완료', 'reply': '네', 'turns_remaining': 15}]
        run.explain_to_patient(calls, {'initial': INITIAL}, events, clock)
        run.explain_to_patient(calls, {'initial': INITIAL}, events, clock)
        self.assertEqual((calls.say.call_count, events[-1]['turn']), (5, 40))

    def test_evidence_only_refresh_does_not_claim_a_changed_clinical_plan(self):
        events = [{'turn': 2, 'action': 'SAY', 'content': '안내', 'reply': '새 증상', 'explain': True}]
        ask = Mock(side_effect=[json.dumps({'plan_update': {'turn': 2, 'quote': '새 증상'}}),
                                json.dumps({'diagnosis': '유지 판단', 'evidence': '새 근거'}),
                                json.dumps({'hpi': '새 증상'})])
        ready = Mock()
        note, diagnosis = record.build(ask, {'initial': INITIAL}, events,
            prepared_plan={'diagnosis': '유지 판단'}, plan_turn=1, on_plan_ready=ready)
        self.assertEqual(diagnosis, '유지 판단')
        self.assertFalse(ready.call_args.args[1])
        self.assertNotIn('추가 설명 미완료', note['A']['evidence'])

    def test_failed_last_review_preserves_reply_and_does_not_claim_success(self):
        events = []
        def final_say(plan, changed):
            events.append({'turn': 1, 'action': 'SAY', 'content': '안내', 'reply': '새 증상', 'explain': True})
        ask = Mock(side_effect=[json.dumps({'plan_update': None}),
                                NovaError('MODEL_CALL_LIMIT', 429), json.dumps({'hpi': '요약'})])
        note, _ = record.build(ask, {'initial': INITIAL}, events,
            prepared_plan={'diagnosis': '기존 판단'}, on_plan_ready=final_say)
        self.assertIn('새 증상', note['S']['hpi'])
        self.assertIn('추가 설명 미완료', note['A']['evidence'])


if __name__ == '__main__':
    unittest.main()
