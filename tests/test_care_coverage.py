"""Offline flow checks. Scripted choices do not establish clinical quality."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import explain
import run


def completion(value):
    return {'choices': [{'message': {'content': json.dumps(value, ensure_ascii=False)}}]}


class HistoryCoverageTest(unittest.TestCase):
    def test_gap_cannot_replace_a_question_already_being_shortened(self):
        calls = Mock()
        calls.chat.side_effect = [completion({'action': 'SAY', 'content': '가' * 35}),
                                  completion({'action': 'DONE', 'history_gap': '다른 질문인가요?'}),
                                  completion({'action': 'SAY', 'content': '원래 목적의 짧은 질문'})]
        result = run.next_step(calls, {'initial': {'opening': '테스트', 'vital_signs': ''}}, [], 10, lambda: 60)
        self.assertEqual(result, ('SAY', '원래 목적의 짧은 질문', 'question'))
        self.assertEqual(calls.chat.call_count, 3)

    def test_identified_gap_already_answered_or_unavailable_is_not_asked_again(self):
        question = '하루 총 몇 시간 주무세요?'
        for answer in ('5시간이요', '잘 모르겠어요', '답하고 싶지 않아요'):
            with self.subTest(answer=answer):
                calls = Mock()
                calls.chat.side_effect = [completion({'action': 'DONE', 'history_gap': question}),
                                          completion({'action': 'DONE', 'history_gap': ''})]
                events = [{'turn': 1, 'action': 'SAY', 'content': question, 'reply': answer}]
                self.assertEqual(run.next_step(calls, {'initial': {'opening': '불면', 'vital_signs': ''}},
                    ['환자: ' + answer], 10, lambda: 60, events=events)[0], 'DONE')
                self.assertEqual(calls.chat.call_count, 2)
                calls.say.assert_not_called()

    def test_done_with_identified_core_gap_asks_it_without_extra_generation(self):
        for question in ('실제로 하루에 몇 시간 주무세요?', '잠드는 데 몇 분 걸리세요?',
                         '최근 키 기록은 언제 측정했나요?', '체중 변화 전후 식사량은 어떤가요?'):
            with self.subTest(question=question):
                calls = Mock()
                calls.chat.return_value = completion({'action': 'DONE', 'history_gap': question})
                result = run.next_step(calls, {'initial': {'opening': '테스트 진술', 'vital_signs': ''}}, [], 10, lambda: 60)
                self.assertEqual(result, ('SAY', question, 'question'))
                self.assertEqual(calls.chat.call_count, 1)

    def test_overlong_gap_is_repaired_without_becoming_done_or_exam(self):
        calls = Mock()
        question = '실제로 잠을 주무시는 시간을 모두 합하면 하루에 몇 시간인가요?'
        calls.chat.side_effect = [completion({'action': 'DONE', 'history_gap': question}),
                                  completion({'action': 'DONE'}),
                                  completion({'action': 'SAY', 'content': '하루 총 몇 시간 주무세요?'})]
        result = run.next_step(calls, {'initial': {'opening': '테스트 진술', 'vital_signs': ''}}, [], 2, lambda: 60)
        self.assertEqual(result, ('SAY', '하루 총 몇 시간 주무세요?', 'question'))
        self.assertEqual(calls.chat.call_count, 3)

    def test_emergency_guidance_is_not_replaced_by_history_gap(self):
        calls = Mock()
        calls.chat.return_value = completion({'action': 'SAY', 'purpose': 'guidance',
            'content': '즉시 응급 평가를 받으세요', 'history_gap': '하루 총 몇 시간 주무세요?'})
        self.assertEqual(run.next_step(calls, {'initial': {'opening': '테스트 진술', 'vital_signs': ''}}, [], 10, lambda: 60),
                         ('SAY', '즉시 응급 평가를 받으세요', 'guidance'))


class ConcernCoverageTest(unittest.TestCase):
    def test_patient_concern_survives_acknowledgement_but_not_doctor_or_exam_text(self):
        initial = {'opening': '약을 줄이면 잠을 못 잘까 걱정돼요'}
        events = [{'action': 'SAY', 'content': '검사가 무섭나요?', 'reply': '네, 치료가 무서워요'},
                  {'action': 'SAY', 'content': '설명', 'reply': '네'},
                  {'action': 'EXAM', 'content': '검사', 'reply': '불안 소견'},
                  {'action': 'SAY', 'content': '질문', 'refused': 'ERROR', 'reply': '더 걱정돼요'}]
        concern = explain.patient_concern(initial, events)
        self.assertIn(initial['opening'], concern)
        self.assertIn('네, 치료가 무서워요', concern)
        self.assertNotIn('불안 소견', concern)
        self.assertNotIn('더 걱정돼요', concern)
        self.assertEqual(explain.patient_concern(initial, []), initial['opening'])
        self.assertEqual(explain.patient_concern({}, [{'action': 'SAY', 'reply': '없어요'}]), '')

    def test_second_slot_connects_concern_to_treatment_from_shared_plan(self):
        calls, clock = Mock(), Mock()
        clock.left.return_value = 1800
        calls.need.return_value = 0
        calls.record_need.return_value = 100
        calls.chat.return_value = completion({'say': '의료진과 감량하며 수면치료를 받으세요'})
        calls.say.return_value = {'turn': 2, 'turns_remaining': 48, 'content': '네'}
        plan = {'diagnosis': '잠정 판단', 'treatment': '감독하 감량과 수면치료', 'evidence': '확인한 근거'}
        events = [{'turn': 1, 'action': 'SAY', 'content': '안내', 'reply': '네', 'explain': True}]
        run.explain_to_patient(calls, {'initial': {'opening': '잠 못 잘까 걱정돼요'}}, events, clock,
                               plan=plan, topics=(explain.TOPICS[1],))
        context = calls.chat.call_args.args[0][1]['content']
        self.assertIn('남아 있는 환자 걱정·질문: "잠 못 잘까 걱정돼요"', context)
        self.assertIn('이번 안내에 사용할 계획: "감독하 감량과 수면치료"', context)
        self.assertIn('치료', explain.TOPICS[1])
        self.assertEqual(calls.chat.call_count, 1)
        self.assertIn('감량', calls.say.call_args.args[0])

    def test_generic_worry_does_not_erase_specific_earlier_concern(self):
        concern = explain.patient_concern({'opening': '약 줄이면 잠 못 잘까 걱정돼요'},
            [{'action': 'SAY', 'reply': '많이 걱정되네요'}, {'action': 'SAY', 'reply': '네'}])
        self.assertIn('약 줄이면 잠 못 잘까 걱정돼요', concern)


class FollowupCoverageTest(unittest.TestCase):
    def test_exhausted_followup_does_not_add_a_generic_warning_before_final_warning(self):
        calls, clock = Mock(), Mock()
        clock.left.return_value = 1800
        calls.need.return_value = 0
        calls.record_need.return_value = 100
        calls.chat.side_effect = [completion({'say': text}) for text in (
            '치료 계획을 함께 상의하겠습니다', '검사로 원인을 확인할게요',
            '두통이면 바로 진료받으세요', '두통이면 즉시 진료받으세요',
            '두통·시야 변화 땐 바로 진료받으세요')]
        calls.say.side_effect = lambda text, **kw: {'turn': calls.say.call_count,
            'turns_remaining': 50 - calls.say.call_count, 'content': '네', 'effective_content': text}
        events = []
        plan = {'diagnosis': '잠정 판단', 'follow_up': '내일 결과 확인'}
        session = {'initial': {'opening': '테스트'}, 'turns_remaining': 50}
        run.explain_to_patient(calls, session, events, clock, plan=plan, topics=explain.TOPICS[:4])
        run.explain_to_patient(calls, session, events, clock, plan=plan, topics=explain.TOPICS[-1:])
        delivered = [c.args[0] for c in calls.say.call_args_list]
        self.assertNotIn(explain.FALLBACK, delivered)
        self.assertEqual(sum('두통' in text for text in delivered), 1)
        self.assertEqual(calls.chat.call_count, 5)

    def test_followup_time_without_purpose_is_repaired(self):
        ask = Mock(side_effect=[json.dumps({'say': '내일 재진하세요'}),
                                json.dumps({'say': '내일 재진해 검사 결과를 확인해요'})])
        self.assertEqual(explain.next_sentence(ask, '', explain.TOPICS[3],
            plan={'follow_up': '내일 결과 확인'}), '내일 재진해 검사 결과를 확인해요')
        self.assertEqual(ask.call_count, 2)

    def test_emergency_time_in_plan_cannot_be_used_as_routine_followup(self):
        source = '4–6개월 내 키를 재측정합니다. 두통이 생기면 당일 진료를 받습니다.'
        ask = Mock(side_effect=[json.dumps({'say': '당일 재진해 키를 확인해요'}),
                                json.dumps({'say': '4–6개월 내 재진해 키를 확인해요'})])
        self.assertEqual(explain.next_sentence(ask, '', explain.TOPICS[3], plan={'follow_up': source}),
                         '4–6개월 내 재진해 키를 확인해요')

    def test_unknown_schedule_is_not_replaced_by_an_invented_date(self):
        ask = Mock(side_effect=[json.dumps({'say': '내일 재진해 결과를 확인해요'}),
                                json.dumps({'say': '재진 일정은 검사 후 상의하겠습니다'})])
        self.assertEqual(explain.next_sentence(ask, '', explain.TOPICS[3],
            plan={'follow_up': '검사 후 재진 일정을 정함'}), '재진 일정은 검사 후 상의하겠습니다')

    def test_new_urgent_patient_fact_can_override_routine_slot_with_exact_source(self):
        reply = '지금 갑자기 숨이 차요'
        warning = '즉시 응급실로 가세요'
        ask = Mock(return_value=json.dumps({'say': warning, 'urgent_reply': reply}))
        self.assertEqual(explain.next_sentence(ask, '', explain.TOPICS[3], patient_reply=reply,
            plan={'follow_up': '내일 결과 확인'}), warning)
        self.assertEqual(ask.call_count, 1)

    def test_fabricated_or_empty_urgent_source_cannot_bypass_followup(self):
        for reply, quote in [('네', '네'), ('네', '갑자기 숨이 차요'), ('', '')]:
            with self.subTest(reply=reply, quote=quote):
                ask = Mock(side_effect=[json.dumps({'say': '즉시 응급실로 가세요', 'urgent_reply': quote}),
                                        json.dumps({'say': '내일 재진해 결과를 확인해요'})])
                self.assertEqual(explain.next_sentence(ask, '', explain.TOPICS[3], patient_reply=reply,
                    plan={'follow_up': '내일 결과 확인'}), '내일 재진해 결과를 확인해요')

    def test_saved_warning_cannot_replace_planned_followup(self):
        for source, warning, followup in (
            ('24–72시간 이내 재진해 약물과 금단 위험 재평가',
             '깨우기 어렵거나 숨이 느려지면 119에 연락하세요', '24–72시간 내 재진해 감량을 상의하세요'),
            ('4–6개월 이내 재진해 키 재측정',
             '두통·시야 변화는 당일 소아과, 심하면 응급실로 가세요', '4–6개월 내 재진해 키를 재겠습니다'),
            ('수일 내 검사 결과 확인을 위해 외래 재진',
             '흉통·실신, 안정 시 숨참은 즉시 응급실로 오세요', '수일 내 재진해 검사 결과를 확인해요')):
            with self.subTest(source=source):
                ask = Mock(side_effect=[json.dumps({'say': warning}), json.dumps({'say': followup})])
                result = explain.next_sentence(ask, '대화', explain.TOPICS[3], plan={'follow_up': source})
                self.assertEqual(result, followup)
                self.assertEqual(ask.call_count, 2)

    def test_retry_cannot_invent_a_different_followup_interval(self):
        ask = Mock(side_effect=[json.dumps({'say': '3일 뒤 재진해 키를 확인해요'}),
                                json.dumps({'say': '4~6개월 내 재진해 키를 확인해요'})])
        self.assertEqual(explain.next_sentence(ask, '', explain.TOPICS[3],
            plan={'follow_up': '4–6개월 내 키 재측정'}), '4~6개월 내 재진해 키를 확인해요')
        self.assertEqual(ask.call_count, 2)

    def test_repeated_warning_never_satisfies_followup_after_retry_exhaustion(self):
        ask = Mock(return_value=json.dumps({'say': '심해지면 바로 응급실로 오세요'}))
        self.assertIsNone(explain.next_sentence(ask, '', explain.TOPICS[3],
            plan={'follow_up': '내일 검사 결과 확인'}))
        self.assertEqual(ask.call_count, 2)

    def test_five_slots_deliver_treatment_tests_followup_and_one_warning(self):
        calls, clock = Mock(), Mock()
        clock.left.return_value = 1800
        calls.need.return_value = 0
        calls.record_need.return_value = 100
        sentences = ['의료진과 감량하며 수면치료를 받으세요', '검사로 약물 영향을 확인할게요',
                     '24–72시간 내 재진해 감량을 상의하세요', '숨이 느려지면 119에 연락하세요']
        calls.chat.side_effect = [completion({'say': text}) for text in sentences]
        calls.say.side_effect = lambda text, **kw: {'turn': 35 + calls.say.call_count,
            'turns_remaining': 15 - calls.say.call_count, 'content': '네', 'effective_content': text}
        events = [{'turn': 35, 'action': 'SAY', 'content': '문진', 'reply': '걱정돼요'}]
        plan = {'diagnosis': '잠정 판단', 'treatment': '감독하 감량과 수면치료',
                'tests': '검사로 약물 영향 확인', 'follow_up': '24–72시간 내 재진해 감량 상의'}
        run.explain_to_patient(calls, {'initial': {'opening': '걱정돼요'}, 'turns_remaining': 50},
                               events, clock, plan=plan)
        delivered = [c.args[0] for c in calls.say.call_args_list]
        self.assertEqual(delivered[1:], sentences)
        self.assertEqual((len(delivered), calls.chat.call_count, events[-1]['turn']), (5, 4, 40))
        self.assertEqual(sum('119' in text for text in delivered), 1)
        self.assertIn('재진', explain.TOPICS[3])
        self.assertNotIn('또는', explain.TOPICS[3])


class ThreeCaseFlowTest(unittest.TestCase):
    def test_saved_three_case_histories_reach_record_with_all_five_explanation_roles(self):
        cases = json.loads((Path(__file__).parent / 'fixtures/live_e35_records.json').read_text(encoding='utf-8'))
        examples = {
            'C01': ('수면제 의존 의심', '의료진과 감량하며 수면치료를 받으세요',
                    '검사로 약물 영향을 확인할게요', '24–72시간 내 재진해 감량을 상의하세요',
                    '숨이 느려지면 119에 연락하세요', 35),
            'C03': ('성장지연 의심', '성장기록을 보며 치료 필요성을 정할게요',
                    '골연령 검사로 성장 상태를 확인해요', '4–6개월 내 재진해 키를 확인해요',
                    '두통·시야 변화 땐 바로 진료받으세요', 31),
            'C10': ('갑상선기능항진증 의심', '검사 후 약물치료를 결정하겠습니다',
                    '혈액검사로 갑상선 기능을 확인할게요', '수일 내 재진해 검사 결과를 확인해요',
                    '안정 시 숨차면 즉시 응급실로 가세요', 35),
        }
        for case in cases:
            with self.subTest(case=case['case']):
                diagnosis, treatment, tests, followup, warning, last_turn = examples[case['case']]
                client = Mock(timeout=240)
                client.session.return_value = {'initial': case['initial'], 'turns_remaining': 50}
                seed = [dict(e, turns_remaining=50 - e['turn']) for e in case['events'] if e['turn'] <= last_turn]
                plan = {'diagnosis': diagnosis, 'treatment': treatment, 'tests': tests, 'follow_up': followup}
                sentences = dict(zip(explain.TOPICS, [diagnosis, treatment, tests, followup, warning]))

                def chat(messages, **kwargs):
                    system, user = (m['content'] for m in messages)
                    if system.startswith('진료 대화와 진찰 소견'):
                        value = plan
                    elif '항목마다' in system:
                        value = case['proposed_marks'] | {'plan_update': None}
                    elif 'S(환자에게' in system:
                        value = case['stories'][0]
                    else:
                        topic = next(topic for topic in explain.TOPICS
                                     if '\n이번 안내 목적(참고): ' + topic in user)
                        value = {'say': sentences[topic]}
                    return completion(value)

                client.chat.side_effect = chat
                client.say.side_effect = lambda text, **kw: {'turn': last_turn + client.say.call_count,
                    'turns_remaining': 50 - last_turn - client.say.call_count, 'content': '네',
                    'effective_content': text}
                client.diagnose.return_value = {'status': 'completed'}
                with patch.object(run, 'interview', side_effect=lambda calls, session, events, clock: events.extend(seed)):
                    self.assertTrue(run.consult(client, None))
                delivered = [c.args[0] for c in client.say.call_args_list]
                self.assertEqual(delivered[1:], [treatment, tests, followup, warning])
                self.assertEqual(len(delivered), 5)
                self.assertTrue(all(0 < len(text) <= 30 for text in delivered))
                submitted = client.diagnose.call_args.kwargs
                self.assertEqual(submitted['diagnosis'], diagnosis)
                self.assertEqual(submitted['note']['S']['hpi'], case['stories'][0]['hpi'])
                for text in delivered:
                    self.assertIn(text, submitted['note']['P']['education'])


if __name__ == '__main__':
    unittest.main()
