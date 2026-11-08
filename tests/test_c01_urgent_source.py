"""Actual e46 termination bypass and safety boundaries; no live calls."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
import run


def history():
    events = [dict(e) for e in json.loads(Path('tests/fixtures/live_e46_c01.json').read_text(encoding='utf-8'))]
    return events


def done(events, index=-1):
    e = events[index]
    return {'action': 'DONE', 'history_gap': '', 'urgent_transfer':
            {'turn': e['turn'], 'quote': e['reply'], 'reason': '즉시 응급 이송 우선'}}


def choose(events, value=None):
    calls = Mock()
    calls.chat.return_value = {'choices': [{'message': {'content': json.dumps(value or done(events), ensure_ascii=False)}}]}
    return run.next_step(calls, {'initial': {'opening': '불면', 'vital_signs': ''}}, [], 20,
                         lambda: 60, events=events)


class UrgentSourceTest(unittest.TestCase):
    def test_real_logistical_agreement_does_not_bypass_current_assessment(self):
        events = history()
        self.assertFalse(run.immediate_handoff(done(events), events))
        self.assertEqual(choose(events), ('EXAM', '사람·장소·시간의 지남력을 확인하겠습니다', ''))

    def test_mild_sleepiness_or_dose_is_not_an_immediate_termination_certificate(self):
        events = history()
        for index in (3, 4, 5):
            with self.subTest(index=index):
                self.assertFalse(run.immediate_handoff(done(events,index), events))
                self.assertEqual(choose(events, done(events,index))[0], 'EXAM')

    def test_severe_new_symptom_after_guidance_still_allows_immediate_transfer(self):
        for quote in ('지금 숨을 못 쉬겠어요', '깨워도 반응이 없어요', '갑자기 경련했어요'):
            events = history()+[dict(turn=8, action='SAY', content='119를 기다리세요',
                                    purpose='guidance', reply=quote)]
            self.assertTrue(run.immediate_handoff(done(events),events))
            self.assertEqual(choose(events)[0], 'DONE')

    def test_normal_orientation_then_breathing_check_then_background_history(self):
        events = history()+[dict(turn=8,action='EXAM',content='사람·장소·시간의 지남력을 확인하겠습니다',
                                reply='명료, 지남력 3/3점')]
        original = done(history())
        self.assertEqual(choose(events,original)[1], '지금 숨쉬기 힘드신가요?')
        events.append(dict(turn=9,action='SAY',content='지금 숨쉬기 힘드신가요?',reply='아니요, 괜찮아요'))
        self.assertEqual(choose(events,original)[0], 'SAY')
        self.assertNotIn('숨쉬',choose(events,original)[1])

    def test_abnormal_missing_or_refused_orientation_does_not_force_background(self):
        for extra in ({'reply':'지남력 저하'}, {'reply':'혈압 128/78'}, {'refused':'EXAM_NOT_RECOGNIZED'}):
            events=history()+[dict(turn=8,action='EXAM',content='사람·장소·시간의 지남력을 확인하겠습니다',**extra)]
            self.assertEqual(choose(events,done(history()))[0], 'DONE')

    def test_transport_arrival_and_patient_stop_take_precedence_over_assessment(self):
        for quote in ('구급대가 도착했어요','더 묻지 마세요'):
            events=history()+[dict(turn=8,action='SAY',content='상태는요?',reply=quote)]
            self.assertEqual(choose(events)[0],'DONE')

    def test_sleep_phrase_negative_symptoms_and_logistical_variants_are_not_urgent_evidence(self):
        for quote in ('약 안 먹으면 한숨도 못 자요', '119에 연락하고 남편과 기다려요',
                      '의식 저하는 없어요', '호흡곤란은 없어요', '숨을 못 쉬지는 않아요'):
            with self.subTest(quote=quote):
                events=history()+[dict(turn=8,action='SAY',content='상태는요?',reply=quote)]
                self.assertFalse(run.immediate_handoff(done(events),events))

    def test_negative_other_symptom_cannot_mask_a_current_severe_symptom(self):
        events=history()+[dict(turn=8,action='SAY',content='상태는요?',reply='경련은 없지만 숨을 못 쉬겠어요')]
        self.assertTrue(run.immediate_handoff(done(events),events))

    def test_insomnia_and_daytime_sleepiness_do_not_complete_breathing_and_function(self):
        state=run.sedative_handoff_coverage(history())
        self.assertEqual(state['breathing']['status'],'missing')
        self.assertEqual(state['function']['status'],'missing')

    def test_explicit_respiratory_denials_are_not_left_as_unknown_by_wording(self):
        for answer in ('아니요, 호흡곤란은 없어요', '숨이 차지는 않아요', '아니요, 숨쉬는 건 괜찮아요'):
            es=history()+[dict(turn=8,action='SAY',content='지금 숨쉬기 힘드신가요?',reply=answer)]
            self.assertTrue(run.sedative_handoff_coverage(es)['breathing']['sources'][-1]['clear_negative'])

    def test_actual_e47_denial_and_seizure_question_leave_reduction_failure_missing(self):
        es=json.loads(Path('tests/fixtures/live_e47_c01.json').read_text(encoding='utf-8'))
        state=run.sedative_handoff_coverage(es)
        self.assertTrue(state['breathing']['sources'][-1]['clear_negative'])
        self.assertEqual(state['control']['status'],'missing')

    def test_existing_orientation_in_consciousness_exam_is_not_repeated(self):
        es=history()+[dict(turn=8,action='EXAM',content='의식 수준을 확인하겠습니다',reply='명료, 지남력 3/3점')]
        self.assertEqual(choose(es,done(history()))[0],'SAY')

    def test_actual_e47_termination_now_selects_the_remaining_handoff_question(self):
        es=json.loads(Path('tests/fixtures/live_e47_c01.json').read_text(encoding='utf-8'))
        value={'action':'DONE','urgent_transfer':{'turn':11,'quote':es[10]['reply'],'reason':'즉시 이송'}}
        result=choose(es,value)
        self.assertEqual(result[0],'SAY')
        self.assertNotIn('지남력',result[1])

    def test_actual_e47_tail_can_collect_remaining_facts_within_existing_turn_limit(self):
        es=json.loads(Path('tests/fixtures/live_e47_c01.json').read_text(encoding='utf-8'))
        value={'action':'DONE','urgent_transfer':{'turn':11,'quote':es[10]['reply'],'reason':'즉시 이송'}}
        answers={'strength':'10mg','today':'20mg','control':'여러 번 줄이려다 실패했어요'}
        for n in range(6):
            action,text,_=choose(es,value)
            if action=='DONE':break
            self.assertEqual(action,'SAY')
            key=next(k for k,_,_,q in run.SEDATIVE_HANDOFF if text.endswith(q))
            es.append(dict(turn=30+n,action='SAY',content=text,reply=answers[key]))
        else:self.fail('incomplete tail')
        self.assertTrue(all(v['status']=='answered' for v in run.sedative_handoff_coverage(es).values()))
        self.assertLessEqual(len(es)+run.EXPLAIN_TURNS,run.MAX_TURNS)

    def test_repeated_logistical_done_cannot_finish_after_only_an_assessment(self):
        events=history()
        original=done(events)
        answers={'breathing':'아니요','strength':'10mg','today':'60mg','prescription':'10mg',
                 'duration':'7년','control':'줄이려다 여러 번 실패했어요',
                 'withdrawal':'거르면 손이 떨려요','function':'낮에 졸려 일을 못 해요'}
        for n in range(15):
            action,text,_=choose(events,original)
            if action=='DONE':break
            if action=='EXAM':reply='명료, 지남력 3/3점'
            else:
                key=next(k for k,_,_,q in run.SEDATIVE_HANDOFF if text.endswith(q))
                reply=answers[key]
            events.append(dict(turn=8+n,action=action,content=text,reply=reply))
        else:self.fail('loop')
        self.assertTrue(all(v['status']=='answered' for v in run.sedative_handoff_coverage(events).values()))


if __name__=='__main__': unittest.main()
