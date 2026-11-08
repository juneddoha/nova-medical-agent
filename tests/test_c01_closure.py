"""Offline termination-path checks, not diagnostic accuracy or clinical scoring."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

import run


def event(question, answer, turn=1, **extra):
    return dict(action='SAY', content=question, reply=answer, turn=turn, **extra)


def history():
    cases = json.loads(Path('tests/fixtures/live_e39_records.json').read_text(encoding='utf-8'))
    events = [dict(e) for e in cases[0]['events'] if e['turn'] <= 15]
    for e in events:
        if e['turn'] in (9, 10, 11, 13, 15):
            e['purpose'] = 'guidance'
    return events


def choose(events, output=None, turns=15, budget=60):
    calls = Mock()
    calls.chat.return_value = {'choices': [{'message': {'content': json.dumps(
        output or {'action': 'DONE'}, ensure_ascii=False)}}]}
    result = run.next_step(calls, {'initial': {'opening': '', 'vital_signs': ''}}, [], turns,
                           lambda: budget, events=events)
    return result, calls


class SedativeClosureTest(unittest.TestCase):
    def test_saved_c01_interview_with_repeated_done_collects_all_missing_handoff_facts(self):
        saved = history()
        answers = {'breathing': '아니요', 'strength': '10mg', 'today': '60mg',
                   'prescription': '10mg', 'duration': '7년', 'control': '줄이려다 여러 번 실패했어요',
                   'withdrawal': '거르면 손이 떨리고 식은땀이 나요', 'function': '낮에 졸려 일을 못 해요'}
        calls, clock = Mock(), Mock(); clock.left.return_value = 1800; calls.need.return_value = 0
        def chat(*args, **kwargs):
            index = calls.chat.call_count - 1
            output = ({'action': 'SAY', 'content': saved[index]['content'],
                       'purpose': saved[index].get('purpose', 'question')} if index < len(saved)
                      else {'action': 'DONE'})
            return {'choices': [{'message': {'content': json.dumps(output, ensure_ascii=False)}}]}
        def say(content, **kwargs):
            index = calls.say.call_count - 1
            if index < len(saved):
                reply = saved[index]['reply']
            else:
                key = next(key for key, _, _, question in run.SEDATIVE_HANDOFF if content.endswith(question))
                reply = answers[key]
            return {'effective_content': content, 'content': reply, 'turn': index + 1,
                    'turns_remaining': 49 - index}
        calls.chat.side_effect = chat; calls.say.side_effect = say
        session = {'initial': {'opening': '불면으로 수면제 처방을 원함', 'vital_signs': ''}, 'turns_remaining': 50}
        events = []
        run.interview(calls, session, events, clock)
        self.assertEqual(calls.say.call_count, len(saved) + 8)
        self.assertEqual(calls.chat.call_count, calls.say.call_count + 1)
        self.assertTrue(all(v['status'] == 'answered' for v in run.sedative_handoff_coverage(events).values()))
        self.assertEqual([e['content'] for e in events[:15]], [e['content'] for e in saved])
        self.assertLessEqual(len(events), run.MAX_TURNS - run.EXPLAIN_TURNS)
        self.assertEqual(len({c.kwargs['request_id'] for c in calls.say.call_args_list}), len(events))
        calls.exam.assert_not_called()

    def test_saved_partial_answer_leaves_breathing_unconfirmed(self):
        state = run.sedative_handoff_coverage(history())
        self.assertEqual(state['breathing']['status'], 'missing')
        self.assertEqual(state['duration']['status'], 'missing')  # insomnia != medication duration
        self.assertEqual(state['today']['status'], 'missing')  # two tablets != today's total mg
        self.assertEqual(choose(history())[0][1], '다시, 지금 숨쉬기 힘드신가요?')

    def test_done_cannot_finish_with_only_three_old_handoff_questions(self):
        events = history() + [event('지금 숨쉬기 힘드신가요?', '아니요', 16),
            event('한 알의 함량은 몇 mg인가요?', '10mg', 17),
            event('오늘 총 몇 mg을 복용하셨나요?', '60mg', 18),
            event('처방받은 1회 복용량은 얼마인가요?', '10mg', 19),
            event('약 복용을 시작한 지 얼마나 됐나요?', '7년', 20),
            event('약을 줄이거나 거르면 어떤 증상이 생기나요?', '거르면 손이 떨려요', 21)]
        state = run.sedative_handoff_coverage(events)
        self.assertEqual(state['withdrawal']['status'], 'answered')
        self.assertEqual(state['control']['status'], 'missing')
        self.assertIn('실패', choose(events)[0][1])

    def test_boolean_answers_are_answers_but_generic_ack_is_not_a_dose(self):
        events = history() + [event('지금 숨쉬기 힘드신가요?', '아니요', 16),
                             event('한 알의 함량은 몇 mg인가요?', '네', 17)]
        state = run.sedative_handoff_coverage(events)
        self.assertEqual(state['breathing']['status'], 'answered')
        self.assertEqual(state['strength']['status'], 'missing')
        self.assertIn('다시', choose(events)[0][1])

    def test_unknown_declined_and_repeated_ambiguity_finish_without_fabricating_answers(self):
        for reply in ('모르겠어요', '답하고 싶지 않아요', '한 번에 하나씩 물어봐 주세요.', ''):
            with self.subTest(reply=reply):
                events = history()
                for n in range(25):
                    result, calls = choose(events)
                    if result[0] == 'DONE':
                        break
                    self.assertEqual(result[0], 'SAY')
                    self.assertLessEqual(len(result[1]), 30)
                    self.assertEqual(calls.chat.call_count, 1)
                    events.append(event(result[1], reply, 16 + n))
                else:
                    self.fail('termination repair loop')
                state = run.sedative_handoff_coverage(events)
                self.assertNotIn('answered', [v['status'] for v in state.values()])

    def test_each_background_slot_has_a_bounded_retry_and_unknown_is_preserved(self):
        base = history() + [event('지금 숨쉬기 힘드신가요?', '아니요', 16)]
        for answer in ('모르겠어요', '답변을 거부합니다', '매일요', '', run.record.NO_ANSWER):
            events = list(base)
            for n in range(20):
                result, _ = choose(events)
                if result[0] == 'DONE':
                    break
                self.assertLessEqual(len(result[1]), 30)
                events.append(event(result[1], answer, 17 + n))
            else:
                self.fail('unbounded background slot attempts')
            state = run.sedative_handoff_coverage(events)
            self.assertTrue(all(v['status'] != 'answered' for k, v in state.items() if k != 'breathing'))

    def test_other_field_numbers_cannot_complete_a_missing_quantity(self):
        events = history() + [event('약에 대해 더 알려주세요',
            '처방은 모르겠고 실제로 60mg 먹어요. 복용 기간도 모르겠어요.', 16)]
        state = run.sedative_handoff_coverage(events)
        self.assertNotEqual(state['prescription']['status'], 'answered')

    def test_off_topic_numbers_and_acknowledgments_do_not_complete_a_slot(self):
        for key, question, answer in (
                ('duration', '약 복용 기간은요?', '불면은 7년 됐어요'),
                ('prescription', '처방 용량은요?', '실제로는 60mg 먹어요'),
                ('today', '오늘 총 몇 mg을 복용하셨나요?', '평소에는 60mg 먹어요'),
                ('strength', '한 알의 함량은 몇 mg인가요?', '총 60mg 먹었어요'),
                ('withdrawal', '약을 거르면 어떤 증상이 생기나요?', '네')):
            with self.subTest(key=key):
                state = run.sedative_handoff_coverage(history() + [event(question, answer, 16)])
                self.assertNotEqual(state[key]['status'], 'answered')

    def test_later_uncertainty_invalidates_previously_reported_quantity(self):
        events = history() + [event('처방 용량은요?', '10mg', 16),
                             event('처방 용량을 다시 확인할게요', '아까 답은 잘 모르겠어요', 17)]
        self.assertEqual(run.sedative_handoff_coverage(events)['prescription']['status'], 'unavailable')

    def test_supplied_prescription_is_kept_when_another_field_is_unknown(self):
        events = history() + [event('약에 대해 더 알려주세요', '처방은 10mg이고 복용 기간은 모르겠어요.', 16)]
        state = run.sedative_handoff_coverage(events)
        self.assertEqual(state['prescription']['status'], 'answered')
        self.assertEqual(state['duration']['status'], 'unavailable')

    def test_model_cannot_cite_a_fabricated_emergency_to_skip_coverage(self):
        output = {'action': 'DONE', 'urgent_transfer': {'turn': 8, 'quote': '숨을 못 쉬어요', 'reason': '이송'}}
        self.assertEqual(choose(history(), output)[0][0], 'EXAM')

    def test_model_question_and_exam_are_not_overwritten_by_the_done_guard(self):
        for output in ({'action': 'SAY', 'content': '마지막 복용 시각은 언제인가요?'},
                       {'action': 'EXAM', 'content': '지남력을 확인하겠습니다'}):
            result, calls = choose(history(), output)
            self.assertEqual(result[:2], (output['action'], output['content']))
            self.assertEqual(calls.chat.call_count, 1)

    def test_arrived_transport_or_request_to_stop_is_not_delayed_for_coverage(self):
        for answer in ('구급대가 도착했어요', '지금 이송 중이에요', '질문은 그만해 주세요',
                       '더 묻지 마세요'):
            events = history() + [event('지금 상태는요?', answer, 16)]
            self.assertEqual(choose(events)[0][0], 'DONE', answer)

    def test_focused_answers_finish_without_changing_a_diagnosis(self):
        answers = {'breathing': '아니요', 'strength': '십 밀리그램', 'today': '육십 밀리그램',
                   'prescription': '10mg', 'duration': '칠 년', 'control': '여러 번 줄이려다 실패했어요',
                   'withdrawal': '거르면 손 떨리고 식은땀 나요', 'function': '낮에 졸려 일을 못 해요'}
        events = history()
        for key, answer in answers.items():
            result, _ = choose(events)
            self.assertEqual(result[1], run.sedative_handoff_coverage(events)[key]['question'])
            events.append(event(result[1], answer, len(events) + 1))
        self.assertEqual(choose(events)[0][0], 'DONE')
        state = run.sedative_handoff_coverage(events)
        self.assertTrue(all(v['status'] == 'answered' for v in state.values()))
        self.assertTrue(all(v['sources'] for v in state.values()))

    def test_unsolicited_explicit_information_is_not_asked_again(self):
        events = history() + [event('약에 대해 더 말씀해주세요',
            '처방은 10mg이고 복용 기간은 7년이에요. 줄이려다 실패했어요. 거르면 손이 떨려요.', 16)]
        state = run.sedative_handoff_coverage(events)
        for key in ('prescription', 'duration', 'control', 'withdrawal'):
            self.assertEqual(state[key]['status'], 'answered', key)

    def test_frequency_and_partial_compound_answers_are_not_completed_history(self):
        events = history() + [event('복용 기간과 처방 용량은요?', '10mg', 16),
                             event('약 복용 기간은요?', '매일요', 17)]
        state = run.sedative_handoff_coverage(events)
        self.assertNotEqual(state['duration']['status'], 'answered')
        self.assertNotEqual(state['prescription']['status'], 'answered')

    def test_refused_or_unverified_delivery_does_not_supply_evidence(self):
        for extra in ({'refused': 'ERROR'}, {'delivery_verified': False}):
            events = history() + [event('약 복용 기간은요?', '7년', 16, **extra)]
            self.assertNotEqual(run.sedative_handoff_coverage(events)['duration']['status'], 'answered')

    def test_emergency_guidance_and_cited_immediate_transfer_are_not_delayed(self):
        events = history() + [event('지금 상태는요?', '숨을 못 쉬겠어요', 16)]
        for output in ({'action': 'SAY', 'content': '즉시 119에 연락하세요', 'purpose': 'guidance',
                        'urgent_reply': events[-1]['reply']},
                       {'action': 'DONE', 'urgent_transfer': {'turn': 16,
                        'quote': events[-1]['reply'], 'reason': '즉시 이송 우선'}}):
            result, _ = choose(events, output)
            self.assertEqual(result[0], output['action'])

    def test_no_emergency_guidance_does_not_trigger_a_mandatory_checklist(self):
        self.assertEqual(choose(history()[:8])[0][0], 'DONE')

    def test_exhausted_time_or_turn_budget_does_not_force_questions(self):
        self.assertIsNone(choose(history(), budget=0)[0])
        calls, clock = Mock(), Mock()
        clock.left.return_value = 1800; calls.need.return_value = 0
        session = {'initial': {'opening': '', 'vital_signs': ''}, 'turns_remaining': 0}
        run.interview(calls, session, history(), clock)
        calls.chat.assert_not_called()
        calls.say.assert_not_called()

    def test_positive_or_uncertain_breathing_does_not_force_background_history(self):
        for answer in ('네, 숨차고 힘들어요', '모르겠어요', '아니요, 숨은 차요'):
            events = history() + [event('지금 숨쉬기 힘드신가요?', answer, 16)]
            self.assertEqual(choose(events)[0][0], 'DONE')

    def test_memory_loss_answer_is_not_mistaken_for_failure_to_recall_history(self):
        state = run.sedative_handoff_coverage(history() + [
            event('복용 후 기억이 끊긴 적 있나요?', '먹은 뒤 기억이 안 나요', 16)])
        self.assertEqual(state['function']['status'], 'answered')

    def test_emergency_override_wins_even_if_model_also_supplies_a_history_gap(self):
        events = history() + [event('지금 상태는요?', '숨을 못 쉬겠어요', 16)]
        output = {'action': 'DONE', 'history_gap': '약은 언제부터 드셨나요?',
                  'urgent_transfer': {'turn': 16, 'quote': events[-1]['reply'], 'reason': '즉시 이송'}}
        self.assertEqual(choose(events, output)[0][0], 'DONE')

    def test_non_sedative_cases_keep_baseline_decisions_at_every_prefix(self):
        cases = json.loads(Path('tests/fixtures/live_e39_records.json').read_text(encoding='utf-8'))
        for case in cases[1:]:
            for n in range(len(case['events']) + 1):
                events = case['events'][:n]
                self.assertIsNone(run.sedative_handoff_coverage(events))
                for output in ({'action': 'DONE'}, {'action': 'SAY', 'content': '상태는 어떤가요?'}):
                    call = Mock(); call.chat.return_value = {'choices': [{'message': {'content': json.dumps(output)}}]}
                    session = {'initial': case['initial']}
                    args = (call, session, [], 10, lambda: 60)
                    expected = (('DONE', '', '') if output['action'] == 'DONE'
                                else ('SAY', output['content'], 'question'))
                    self.assertEqual(run.next_step(*args, events=events), expected)


if __name__ == '__main__':
    unittest.main()
