"""직전 환자 반응을 포함한 대화로 다음 안내·확인 한 문장을 만든다.

설명은 기록에 적는 것만으로 안 되고 진료 중 SAY로 실제로 해야 한다. 한 번에 30자라 한 가지를 한 문장으로 나눠 말한다.
"""
import json
import re

from doctor import action_text, parse_json
from nova_sdk import NovaError
from record import acknowledgment

MAX_CHARS = 30
TOPICS = ('가장 가능성이 높은 진단을 쉬운 말로', '환자 걱정에 답하는 구체적 치료·지원 방법과 다음 행동 하나', '필요한 검사와 확인하려는 것',
          '일반 재진·검사 결과 확인 시점과 목적을 구체적으로(응급 경고와 분리)',
          '즉시 진료가 필요한 구체적 증상과 그때 할 행동·진료 장소')
EXPLAIN_PROMPT = (
    '진료를 마무리하며 다음에 할 말 한 문장을 씁니다. 대화와 진찰에서 확인된 내용만 근거로 합니다.\n'
    '진단은 주호소의 전체 경과와 확인된 소견에 근거해 설명하세요. 배제하려고 질문한 위험을 확진처럼 말하지 마세요.\n'
    '직전 환자의 질문·걱정·오해에 먼저 직접 답하세요. 단순 공감이나 진단·원인 확인을 반복하지 말고, 계획에 있는 구체적 치료·지원 방법 또는 다음 절차 하나로 답하세요. 가능한 경우 이번 안내 목적에 연결하세요.\n'
    '단순히 "걱정되시죠" 또는 "원인 확인이 필요해요"로 끝내지 마세요. 치료 안내에서는 현재 안전 조치와 이후 치료를 구별해 환자가 무엇을 할 수 있는지 답하세요. 계획에 없는 치료나 완치·안전 보장은 만들지 마세요.\n'
    '일반적인 "네"가 나와도 앞서 표현한 걱정이 해결됐다고 가정하지 마세요. 경고는 마지막 안내에 모으고, 같은 위험을 다른 표현으로 반복해 치료나 재진 안내를 밀어내지 마세요. 단, 새 응급 증상을 호소하면 즉시 대응하세요.\n'
    '검사는 확인 목적을, 재진은 상태에 맞는 시점이나 조건을 구체화하세요. 한 문장에는 지금 가장 필요한 안내를 담으세요.\n'
    f'25자 정도의 짧고 쉬운 존댓말로 쓰고 공백 포함 {MAX_CHARS}자를 넘지 마세요. 여러 질문을 묶지 마세요.\n'
    '직전 답변에 새로 발생한 응급 호소가 있어 이번 안내보다 즉시 대응해야 할 때만 urgent_reply에 그 환자 답변 전체를 그대로 넣으세요. 단순 수긍·걱정에는 넣지 마세요.\n'
    'JSON 객체 하나만 출력하세요.\n{"say": "다음에 할 말", "urgent_reply": null}')
# 생성 실패나 시간 부족 때 진료를 끝내기 전 전달할 일반 안내.
FALLBACK = '증상이 심해지면 바로 진료를 받으세요.'


def usable(sentence):
    if not isinstance(sentence, str):
        return None
    text = action_text(sentence, MAX_CHARS)
    return text if 0 < len(text) <= MAX_CHARS else None


def guidance_key(text):
    if re.search(r'약|포장|봉투', text) and re.search(r'챙|준비|가져|알려|전달', text):
        return '약물 정보 전달'
    if '운전' in text:
        return '운전 금지'
    if re.search(r'혼자|남편|보호자', text):
        return '동행·관찰 지원'
    if re.search(r'면|경우|땐|때', text) and re.search(r'호흡|숨|의식|깨우|반응|실신|흉통|두통|시야', text):
        return '응급 경고'
    if re.search(r'(?:호흡|의식).*(?:확인|평가|살피|관찰)', text):
        return '호흡·의식 평가'
    if re.search(r'응급실|119|구급대', text):
        return '응급 이송'
    return None


def repeated_guidance(text, previous):
    key = guidance_key(text)
    compact = lambda value: re.sub(r'[\s.!。]', '', value)
    return any(compact(text) == compact(old) or (key and key == guidance_key(old)) for old in previous)


def patient_concern(initial, events):
    """환자가 직접 표현한 최근 걱정·질문을 짧은 수긍 때문에 잃지 않는다."""
    replies = [str(initial.get('opening') or '')] + [e['reply'] for e in events
        if e.get('action') == 'SAY' and not e.get('refused') and isinstance(e.get('reply'), str)]
    return '\n'.join(dict.fromkeys(text for text in replies if re.search(r'걱정|불안|무섭|무서|두렵|두려|[?？]', text)))


def followup_times(text):
    """계획의 시점을 그대로 전달한다. 단위 변환·동의어 추론은 하지 않는다."""
    pattern = (r'(?:\d+(?:\s*[~–—−-]\s*\d+)?|한두|두세|서너|한|두|세|네)\s*(?:시간|개월|주일|주|일|달|년)'
               r'|오늘|당일|내일|모레|수일|며칠|이틀|사흘|이번\s*주|다음\s*주'
               r'|검사\s*결과(?:가)?\s*나오면|(?:퇴원|귀가)\s*(?:하면|한다면|직후|후|뒤|시)')
    return {'퇴원후' if re.match(r'퇴원|귀가', m.group()) else
            re.sub(r'\s+', '', re.sub('[–—−-]', '~', m.group())) for m in re.finditer(pattern, text)}


def follows_plan(text, source):
    after_discharge = r'(?:퇴원|귀가)\s*(?:하면|한다면|후|뒤|시)'
    if re.search(after_discharge, source) and not re.search(after_discharge, text):
        return False
    times, planned = followup_times(text), followup_times(source)
    if not planned:
        return not times and bool(re.search(r'시점|일정', text)) and bool(re.search(r'확인|상의|정하', text))
    return bool(times and times <= planned and re.search(r'재평가|재측정|확인|결과|측정|검토|재겠|상의|경과|치료', text))


def routine_followup(source):
    sentences = re.split(r'(?<=[.!。])\s+|\n', source.strip())
    for sentence in sentences:
        if (followup_times(sentence) and re.search(r'외래|재진|재평가|추적|결과.*확인|재측정', sentence)
                and not re.search(r'생기면|악화하면|나타나면|심해지면', sentence)):
            return sentence
    return sentences[0]


def next_sentence(ask, talk, topic, enough_time=lambda: True, patient_reply='', previous=(), plan=None, concern=''):
    """최신 대화로 한 문장을 생성한다. 형식 재시도는 한 번이며 실패는 None."""
    request = (talk + '\n\n직전 환자 답변(직접 대응할 대상): ' + json.dumps(patient_reply, ensure_ascii=False)
               + '\n이번 안내 목적(참고): ' + topic)
    if topic == TOPICS[1]:
        request += ('\n남아 있는 환자 걱정·질문: ' + json.dumps(concern, ensure_ascii=False)
                    + '\n이번 안내에 사용할 계획: ' + json.dumps((plan or {}).get('treatment', ''), ensure_ascii=False)
                    + '\n그 걱정에 치료·지원의 구체적 행동으로 답하세요. 진단이나 검사 필요성만 반복하지 마세요. '
                    '치료 계획이 없으면 모른다고 밝히고 확인할 다음 절차를 설명하세요.')
    followup = (plan or {}).get('follow_up', '') if topic == TOPICS[3] else ''
    if followup:
        followup = routine_followup(followup)
        request += ('\n이번 안내에 사용할 재진 계획: ' + json.dumps(followup, ensure_ascii=False)
                    + '\n일반 재진 또는 결과 확인의 시점과 목적을 먼저 말하세요. 계획에 있는 시점을 그대로 쓰고 '
                    '임의의 날짜·기간을 만들지 마세요. 시점이 미정이면 일정 확인이 필요하다고 말하세요. '
                    '경고 증상만 말하는 것은 재진 안내가 아닙니다.')
    for _ in range(2):
        if not enough_time():
            break
        try:
            value = parse_json(ask(EXPLAIN_PROMPT, request, 2048)) or {}
            raw = value.get('say')
        except NovaError:  # 모델 호출이 오류로 끝나도 어느 증례에나 맞는 설명은 한다
            break
        text = usable(raw)
        urgent = bool(patient_reply.strip() and value.get('urgent_reply') == patient_reply
                      and not acknowledgment(patient_reply))
        repeated = bool(text and not urgent and repeated_guidance(text, previous))
        wrong_followup = bool(text and followup and not urgent and not follows_plan(text, followup))
        actions = r'약|감량|식사|수면|상담|눈물|운동|금연|음주|전문의'
        generic_treatment = bool(text and not urgent and topic == TOPICS[1]
            and re.search(actions, (plan or {}).get('treatment', '')) and not re.search(actions, text)
            and re.search(r'(?:검사|결과).*(?:치료를?\s*(?:정|결정)|원인\s*확인)', text))
        if text and not repeated and not wrong_followup and not generic_treatment:
            return text
        request += ('\n직전 출력: ' + json.dumps(raw, ensure_ascii=False)
                    + ('\n이미 전달한 문장입니다. 새로운 근거나 구체적인 다음 절차로 답하세요.' if repeated else '')
                    + ('\n재진 시점·목적이 빠졌거나 계획의 시점과 다릅니다. 경고 대신 계획의 재진 안내를 쓰세요.' if wrong_followup else '')
                    + ('\n계획에 있는 구체적 치료·지원 행동 하나로 걱정에 답하세요. 검사 뒤 결정한다는 말만 반복하지 마세요.' if generic_treatment else '')
                    + '\n부가 표현을 덜고 핵심 의도를 유지하며 25자 이내 문자열 say 하나로 고쳐 쓰세요.')
    return None
