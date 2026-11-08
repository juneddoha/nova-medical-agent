"""초진 기록을 만든다. 양식과 진찰 소견은 코드가 채우고, 모델에게는 대화에서 확인된 내용만 묻는다.

- O칸 진찰 소견: 받은 글자 그대로 옮기고 턴 번호를 붙인다. 모델을 거치지 않는다.
- S칸의 (+)/(-): 출처·항목·명확한 극성이 맞지 않으면 ( )로 비운다.
- 숫자: 환자 진술의 수치·단위·항목 조합을 검사하고 재작성도 실패하면 원문으로 복구한다.
- 어느 단계가 실패해도 기록은 낸다. 미제출은 그 증례가 0점이다.
"""
import json
import re
import sys
from datetime import date
from decimal import Decimal

import korean_numbers
from doctor import parse_json
from nova_sdk import RECORD_FIELDS, empty_record

NOTE_MAX_BYTES, DIAGNOSIS_MAX_BYTES = 100_000, 500
MIN_SECONDS = 50  # 남은 시간이 이보다 적으면 모델을 더 부르지 않고 지금까지 만든 것으로 낸다. 호출의 대기 시간은 run.py가 남은 시간에 맞춰 줄인다
NO_DIAGNOSIS = '진단 미정'
NO_ANSWER = '한 번에 하나씩 물어봐 주세요.'  # 한꺼번에 물으면 가상 환자가 하는 말(개발 가이드)

# (키, 모델에게 보여 줄 뜻, 시작 정보만으로 표시할 때 거기 있어야 하는 낱말)
SLOTS = [
    ('pmh_dm', '본인의 당뇨병', ('당뇨',)), ('pmh_htn', '본인의 고혈압', ('고혈압',)),
    ('pmh_stroke', '본인의 뇌졸중 과거력', ('뇌졸중', '중풍', '뇌경색', '뇌출혈')), ('pmh_tbc', '본인의 결핵', ('결핵',)),
    ('pmh_hepatitis', '본인의 간염', ('간염',)), ('pmh_heart', '본인의 심장병', ('심장',)),
    ('pmh_dyslipidemia', '본인의 고지혈증', ('고지혈', '콜레스테롤')), ('pmh_op', '수술 받은 적', ('수술',)),
    ('pmh_allergy', '약이나 음식 알레르기', ('알레르기', '알러지')),
    ('fh_dm', '가족 중 당뇨병', ('당뇨',)), ('fh_htn', '가족 중 고혈압', ('고혈압',)),
    ('fh_stroke', '가족 중 뇌졸중', ('뇌졸중', '중풍', '뇌경색', '뇌출혈')),
    ('sh_smoking', '흡연', ('담배', '흡연')), ('sh_alcohol', '음주', ('술', '음주')),
    ('ros_weakness', '전신 쇠약감, 기운 없음', ('기운', '쇠약', '무력', '힘이 빠', '힘이빠')), ('ros_fatigue', '피로', ('피로', '피곤', '지치', '지쳐')),
    ('ros_headache', '두통', ('두통', '머리')), ('ros_dizziness', '어지럼', ('어지',)),
    ('ros_chest_pain', '가슴 통증', ('가슴', '흉통')), ('ros_palpitation', '두근거림', ('두근',)),
    ('ros_fever', '발열', ('열',)), ('ros_chill', '오한', ('오한', '으슬')), ('ros_cough', '기침', ('기침',)),
    ('ros_sputum', '가래', ('가래',)), ('ros_rhinorrhea', '콧물', ('콧물',)),
    ('ros_abdominal_pain', '복통', ('복통', '배가', '배 아프', '배 아픔')), ('ros_melena', '검은 변', ('흑변', '검은')),
    ('ros_hematochezia', '혈변', ('혈변', '피가')), ('ros_hematemesis', '피를 토함', ('토혈', '피를 토')),
    ('ros_anorexia', '식욕 저하', ('식욕', '입맛')), ('ros_nausea', '메스꺼움', ('메스꺼', '구역', '울렁')),
    ('ros_vomiting', '구토', ('구토', '토하', '토했')), ('ros_diarrhea', '설사', ('설사', '묽')), ('ros_constipation', '변비', ('변비',)),
]
# 모범 초진기록의 양식. 한 줄에 (이름, 그 줄에 들어가는 키들)
LAYOUT = {
    'pmh': [('DM / HTN', ('pmh_dm', 'pmh_htn')), ('previous stroke', ('pmh_stroke',)),
            ('Tbc / hepatitis', ('pmh_tbc', 'pmh_hepatitis')), ('heart disease', ('pmh_heart',)),
            ('dyslipidemia', ('pmh_dyslipidemia',)), ('Op. Hx', ('pmh_op',)), ('Allergy', ('pmh_allergy',))],
    'fh': [('DM / HTN', ('fh_dm', 'fh_htn')), ('Stroke', ('fh_stroke',))],
    'sh': [('Smoking', ('sh_smoking',)), ('Alcohol', ('sh_alcohol',))],
    'ros': [('General weakness', ('ros_weakness',)), ('Fatigue', ('ros_fatigue',)), ('Headache', ('ros_headache',)),
            ('Dizziness', ('ros_dizziness',)), ('Chest pain', ('ros_chest_pain',)), ('Palpitation', ('ros_palpitation',)),
            ('Fever/Chill/Cough/Sputum/Rhinorrhea', ('ros_fever', 'ros_chill', 'ros_cough', 'ros_sputum', 'ros_rhinorrhea')),
            ('Abdominal pain', ('ros_abdominal_pain',)), ('Melena', ('ros_melena',)), ('Hematochezia', ('ros_hematochezia',)),
            ('Hematemesis', ('ros_hematemesis',)),
            ('Anorexia/Nausea/Vomiting/Diarrhea/Constipation',
             ('ros_anorexia', 'ros_nausea', 'ros_vomiting', 'ros_diarrhea', 'ros_constipation'))],
}

MARKS_PROMPT = """진료 대화를 읽고 아래 항목마다 환자에게서 확인된 것만 표시합니다.
환자가 있다고 했으면 "+", 없다고 했으면 "-"입니다. 의사가 묻지 않았고 환자도 말하지 않은 항목은 ""로 둡니다. 추측하지 않습니다.
정도가 약하다는 말은 부재가 아닙니다. '심하지 않다', 모른다는 답, 묶음 질문의 일부에만 한 답으로 나머지 항목을 음성 처리하지 않습니다. 모호하면 ""로 둡니다.
의사의 질문·설명·추측을 환자의 진술로 쓰지 않습니다. 문진과 설명 중 환자가 새로 답한 사실은 모두 사용할 수 있습니다.
항목마다 {"mark": "-", "turn": 6, "quote": "환자 답변 전체 원문"}처럼 씁니다. turn은 그 사실을 들은 턴 번호입니다.
quote는 해당 턴의 환자 답변 전체를 생략·수정 없이 옮깁니다. 시작 정보가 근거면 turn은 0이고 quote는 첫 진술 중 근거 문장을 그대로 옮깁니다.
JSON 객체 하나만 출력하세요. 키와 뜻:
""" + '\n'.join(f'{key}: {meaning}' for key, meaning, _ in SLOTS)

STORY_PROMPT = """진료 대화에서 환자가 말한 내용만으로 초진 기록의 S(환자에게 들은 내용)를 씁니다. 병원 초진 기록 말투로 간결하게 씁니다.
듣지 않은 내용, 추측, 의학 지식은 쓰지 않습니다. 숫자(나이, 기간, 횟수, 용량)는 환자가 말한 값 그대로 아라비아 숫자로 옮깁니다.
질문만 한 내용은 사실이 아닙니다. 정도가 약함과 증상 없음, 처방된 용법과 실제 복용을 구분하세요. 모호하거나 일부만 답한 항목은 미확인으로 두며 음성으로 바꾸지 마세요.
JSON 객체 하나만 출력하세요. 모든 값은 한국어 문자열(줄바꿈은 \\n)이고, 듣지 못한 칸은 빈 문자열입니다.
{"cc": "주호소 한 마디", "onset": "시작 시점",
 "hpi": "나이와 성별, 시작과 경과, 양상, 심해지거나 나아지는 요인, 동반 증상, 환자의 걱정. '~하여 내원함'으로 끝냄",
 "medicine": "복용 중인 약의 이름과 들은 용량·횟수", "pmh_others": "그 밖에 들은 과거력",
 "fh_others": "가족력으로 들은 내용", "sh_others": "직업, 음주량, 흡연량, 생활습관으로 들은 내용",
 "ros_others": "그 밖에 물어서 확인한 증상을 '증상(+)', '증상(-)'로 나열"}"""
STORY_KEYS = ('cc', 'onset', 'hpi', 'medicine', 'pmh_others', 'fh_others', 'sh_others', 'ros_others')

PLAN_PROMPT = """진료 대화와 진찰 소견을 근거로 초진 기록의 A(평가)와 P(계획)를 씁니다. 병원 초진 기록 말투로 간결하게 씁니다.
근거에는 대화와 진찰에서 확인한 사실만 씁니다. 검사는 아직 하지 않았으므로 결과가 있는 것처럼 쓰지 않고 계획으로만 씁니다.
주호소의 전체 경과·기능 변화·확인된 소견을 종합해 가장 잘 뒷받침되는 주진단 하나를 고르세요. 확인된 문제와 배제해야 할 위험을 구분하세요.
확진 여부와 가장 가능성 높은 원인 선택을 구분하세요. 검사가 남아 있어도 수집한 경과·가족력·진찰이 원인을 지지하면 그 원인을 '의심'인 잠정 주진단으로 선택하고, 확진에 필요한 검사는 P에 씁니다. 원인 간 우열을 뒷받침할 근거가 없을 때만 증상·증후군을 주진단으로 남깁니다.
위험해서 확인했다는 이유만으로 그 질환을 주진단으로 삼지 마세요. 응급 소견이 실제 있으면 그 근거에 따라 우선 대응하세요.
질문한 사실, 모호한 답, 확인되지 않은 정상 소견은 진단 근거가 아닙니다. 부족한 정보와 불확실성은 명시하세요.
환자에게 설명한 잠정 진단과 최종 판단이 달라지면 변경 근거를 evidence에 쓰세요. 단지 앞서 말했다는 이유로 진단을 유지하지 마세요.
추가 검사는 이 환자에서 필요한 것만 고르고 무엇을 확인하며 결과에 따라 다음 판단·처치가 어떻게 달라지는지 적으세요.
재진은 환자 상태에 맞는 구체적인 시점 또는 조건과 목적을 쓰세요. 즉시 진료가 필요한 경고 증상·행동·진료 장소를 일반 추적과 구분하세요.
follow_up에는 일반 재진·결과 확인 시점과 목적을 먼저 쓰고, 경고 증상은 뒤에 분리하세요. 검사 결과를 확인할 시점과 장기간 경과를 재측정할 시점이 다르면 둘을 구분하세요. 환자에게 전달할 가장 가까운 다음 일정을 명확히 하세요.
계획은 아직 시행·설명한 사실이 아닙니다. 미실시 검사 결과나 존재하지 않는 예약을 만들지 마세요.
JSON 객체 하나만 출력하세요. 모든 값은 한국어 문자열(줄바꿈은 \\n)입니다.
{"diagnosis": "가장 가능성 높은 주진단 하나", "evidence": "#1. 주진단\\n- 근거 : 확인한 사실과 해석",
 "differential": "r/o 진단명 : 이유 (한 줄에 하나)", "tests": "추가 검사·확인 목적·결과에 따른 다음 판단", "treatment": "치료 계획",
 "disposition": "귀가, 외래 추적, 의뢰, 응급 처치 중 결정과 이유", "follow_up": "재진 시점 또는 조건·목적, 경고 증상과 즉시 할 행동·진료 장소"}"""
PLAN_KEYS = ('evidence', 'differential', 'tests', 'treatment', 'disposition', 'follow_up')

def answered(events, action):
    return [e for e in events if e['action'] == action and 'reply' in e]


MEASUREMENTS = (
    ('앉은키', r'앉은\s*키|좌고', r'(?:앉은\s*키|좌고)\s*[:：]?\s*\d'),
    ('산소포화도', r'산소\s*포화도|spo2', r'(?:산소\s*포화도|spo2)\s*[:：]?\s*\d|^\s*\d+(?:\.\d+)?\s*%\s*$'),
    ('호흡수', r'호흡수', r'호흡(?:수)?\s*[:：]?\s*\d|^\s*\d+\s*회\s*/\s*분\s*$'),
    ('혈압', r'혈압', r'혈압\s*[:：]?\s*\d|^\s*\d+\s*/\s*\d+\s*(?:mmhg)?\s*$'),
    ('체중', r'체중|몸무게', r'(?:체중|몸무게)\s*[:：]?\s*\d|^\s*\d+(?:\.\d+)?\s*kg\s*$'),
)


def measurement(content):
    # ponytail: 정량 측정의 명시적 이름만 묶는다. 일반 진찰의 의미 동일성은 추론하지 않는다.
    return next((item for item in MEASUREMENTS if re.search(item[1], content, re.I)), None)


def exam_feedback(content, reply):
    target = measurement(content)
    if target and not re.search(target[2], reply, re.I):
        return '요청 측정치 미반환; 다른 제공 소견 원문: ' + reply
    return reply


def dialogue(initial, events):
    """모델에게 보여 줄 진료 기록. 턴 번호를 붙여 근거 턴을 댈 수 있게 한다."""
    lines = [f"시작 정보\n첫 진술: {initial['opening']}\n활력징후: {initial['vital_signs']}\n\n진료 대화"]
    for event in events:
        head = f"[턴 {event['turn']}]"
        if 'reply' not in event:
            lines.append(f"{head} 요청({event['content']})이 거절됨")
        elif event['action'] == 'SAY':
            label = '의사 요청(실제 전달문 미확인)' if event.get('delivery_verified') is False else '의사'
            lines.append(f"{head} {label}: {event['content']}\n{head} 환자: {event['reply']}")
        else:
            lines.append(f"{head} 진찰({event['content']}): {exam_feedback(event['content'], event['reply'])}")
    return '\n'.join(lines)


def exam_text(initial, events):
    vitals = ', '.join(re.sub(r'\s*:\s*', ' ', line.strip()) for line in initial['vital_signs'].splitlines() if line.strip())
    lines = [f'V/S : {vitals} (시작 정보)'] if vitals else []
    lines += [f"{e['content']} : {exam_feedback(e['content'], e['reply'])} (턴 {e['turn']})" for e in answered(events, 'EXAM')]
    return '\n'.join(lines)


def education_text(events):
    """문진 중 안내와 설명 단계의 성공 발언을 기록한다. 내용의 적절성·환자 이해는 별도다."""
    return '\n'.join(f"(턴 {e['turn']}) {e['content']}" for e in answered(events, 'SAY')
                     if (e.get('explain') or e.get('purpose') == 'guidance')
                     and e.get('delivery_verified') is not False and e['content'].strip())


def ask_json(ask, system, user, max_tokens, tries=2, enough_time=lambda: True):
    for _ in range(tries):
        if not enough_time():
            return None
        value = parse_json(ask(system, user, max_tokens))
        if value:
            return value
    return None


def slot_matches(words, text):
    matches = []
    for word in words:
        pattern = re.escape(word)
        if word in ('술', '열'):
            pattern = r'(?<![가-힣])' + pattern + r'(?=$|[^가-힣]|[은는이가을를도과만]|나요|감|나[요고])'
        matches.extend(re.finditer(pattern, text))
    return matches


def supported_mark(key, words, question, quote):
    """명확한 개별 진술만 표식으로 쓴다. 해석하기 어려운 문장은 원문에만 남긴다."""
    family = r'가족|아버지|어머니|부모|형제|자매|할머니|할아버지'
    if key.startswith('pmh_') and re.search(family, quote):
        return None
    if key.startswith('fh_') and not re.search(family, quote + question):
        return None
    if key.startswith('fh_') and re.search(r'저는|제가|본인|나만', quote):
        return None
    all_words = {word for _, _, names in SLOTS for word in names}
    target = slot_matches(words, quote)
    drinking_answer = (key == 'sh_alcohol' and re.search(r'술|음주', question)
                       and re.search(r'마셔|마시|음주', quote))
    pain_answer = (key == 'ros_chest_pain' and re.search(r'가슴|흉통', question)
                   and re.match(r'통증(?:(?:·|,|와|과|및)\s*[가-힣]+)*[은이가\s]*없', quote))
    if not target and (drinking_answer or pain_answer):
        clause = re.split(r'고\s+|지만\s+|,\s*', quote)[-1] if drinking_answer else quote
    elif target:
        start = min(m.start() for m in target)
        end = min((m.start() for m in slot_matches(all_words - set(words), quote)
                   if m.start() > start), default=len(quote))
        clause = quote[start:end]
        # 같은 항목의 여러 시점/반대 진술을 한 표식으로 압축하지 않는다.
        if len(target) > 1:
            return None
    else:
        asked = {names for _, _, names in SLOTS if slot_matches(names, question)}
        plural_denial = bool(re.fullmatch(r'(?:아니요[,\s]*)?(?:그런|말씀하신)\s*증상[은이는\s]*없(?:어요|습니다)[.!\s]*', quote))
        if ((len(asked) != 1 and not plural_denial) or not slot_matches(words, question)
                or slot_matches(all_words, quote)):
            return None
        clause = quote
        if re.fullmatch(r'\s*(?:아니요|아뇨|네|예)[.!\s]*', quote) and re.search(r'있나요|있어요', question):
            return '-' if re.search(r'아니요|아뇨', quote) else '+'
    if re.search(r'모르|기억|심하지|거의|같[아은]|같아요|가능|예정|계획|받을|없지|없는\s*(?:건|것)|아니지|잘\s*안|예전|전에는|없.*(?:아니|않|없)|없다고|없을\s*리|[?？]', clause):
        return None
    if key in ('sh_smoking', 'sh_alcohol'):
        behavior = r'피워|피우|흡연' if key == 'sh_smoking' else r'마셔|마시|음주|먹어'
        negative_behavior = r'안\s*(?:피워|피우)' if key == 'sh_smoking' else r'안\s*(?:마셔|마시|먹어)'
        if re.search(negative_behavior, clause):
            return '-'
        if (key == 'sh_alcohol' and korean_numbers.find(clause) and re.search(r'잔|병', clause)
                and not re.search(r'없|않|안\s|집|보관', clause)):
            return '+'  # 평소 음주량을 묻고 받은 수량 답변도 음주 진술이다.
        if not re.search(behavior, clause):
            return None
    absent = bool(re.search(r'없(?:어|습|다|었|는|고|음)|없$|(?:하진|하지|아프지|묽지|묽진)\s*않', clause))
    present = bool(re.search(r'있(?:어|습|다|었|는|고|음)|나(?:요|고)|앓|진단받|피워|마셔', clause))
    if key in ('ros_weakness', 'ros_anorexia'):
        # 기운/식욕의 부재는 증상의 존재다.
        if absent and not present:
            return '+'
        if key == 'ros_anorexia':
            if re.search(r'좋|정상|잘\s*먹|증가|늘|많이\s*먹', clause):
                return '-'
            if re.search(r'떨어|줄었|저하', clause):
                return '+'
        if key == 'ros_weakness' and re.search(r'힘.*빠|기운.*없', clause):
            return '+'
        if key == 'ros_weakness' and re.search(r'아니요.*(?:활발|잘\s*놀)', clause):
            return '-'
        return None
    if key == 'ros_fatigue' and re.search(r'아니요.*(?:활발|잘\s*놀)', clause):
        return '-'
    if key == 'ros_constipation' and re.search(r'아니요.*(?:편하|편하게|잘\s*봐)', clause):
        return '-'
    if not absent and not present:
        if key == 'ros_fatigue' and re.search(r'피곤|피로|지쳐|지치', clause):
            return '+'
        if key == 'ros_palpitation' and re.search(r'두근(?:거리|거려|대)', clause):
            return '+'
        if key == 'ros_diarrhea' and '묽' in clause:
            return '+'
        if (target and re.search(r'거나|와|과|도', quote) and re.search(r'(?:하진|하지)\s*않', quote)
                and not re.search(r'있|나[요고]|좋|심하지', quote)):
            return '-'
    if absent == present:
        return None
    return '-' if absent else '+'


def checked_marks(raw, initial, events):
    """출처·항목·명확한 극성이 모두 맞는 표식만 기록한다."""
    replies = {e['turn']: e['reply'].strip() for e in answered(events, 'SAY')
               if e['reply'].strip() and e['reply'].strip() != NO_ANSWER}
    marks = {}
    for key, _, words in SLOTS:
        item = raw.get(key)
        if not isinstance(item, dict) or str(item.get('mark')).strip() not in ('+', '-'):
            continue
        try:
            turn = int(item.get('turn'))
        except (TypeError, ValueError):
            continue
        quote = item.get('quote')
        if not isinstance(quote, str) or not quote.strip():
            continue
        quote = quote.strip()
        if (turn in replies and quote == replies[turn]) or (
                turn == 0 and quote in initial['opening'] and any(word in quote for word in words)):
            question = next((e['content'] for e in answered(events, 'SAY') if e['turn'] == turn), '')
            mark = str(item['mark']).strip()
            if supported_mark(key, words, question, quote) == mark:
                marks[key] = mark
    return marks


UNIT_ALIASES = {
    'cm': ('cm', '센티미터', '센티'), 'kg': ('kg', '킬로그램', '키로그램', '키로그람', '킬로', '키로'),
    'mg': ('mg', '밀리그램', '미리그램'), 'g': ('g', '그램'), 'ml': ('ml', '밀리리터'),
    'mmhg': ('mmhg',), 'mg/dl': ('mg/dl',), 'mmol/l': ('mmol/l',),
    '정': ('정', '알'), '세': ('세', '살'), '개월': ('개월', '달'), '년': ('년', '해'),
    '일': ('일',), '주': ('주일', '주'), '시간': ('시간',), '시': ('시',), '분': ('분',),
    '초': ('초',), '회': ('회', '번', '차례'), '도': ('도', '℃', '°c'), '%': ('%', '퍼센트'),
    '갑': ('갑',), '잔': ('잔',), '병': ('병',), '개': ('개',), 'bpm': ('bpm',), '월': ('월',), '끼': ('끼',),
}
UNIT_NAMES = {alias: unit for unit, aliases in UNIT_ALIASES.items() for alias in aliases}
QUANTITY_UNIT = re.compile(r'\s*(' + '|'.join(map(re.escape, sorted(UNIT_NAMES, key=len, reverse=True))) + ')', re.I)
LABEL_GROUPS = {
    '항목': {'신장': '키', '키': '키', '몸무게': '체중', '체중': '체중', '수축기': '수축기', '이완기': '이완기'},
    '용법': {'처방': '처방', '실제': '실제'},
    '사람': {'엄마': '어머니', '어머니': '어머니', '아빠': '아버지', '아버지': '아버지'},
    '시간대': {'오전': '아침', '아침': '아침', '저녁': '저녁', '밤': '밤', '새벽': '새벽'},
    '시점': {'현재': '현재', '지금': '현재', '원래': '이전', '이전': '이전', '전에는': '이전'},
}


def quantity_labels(prefix, unit):
    labels = {}
    for group, aliases in LABEL_GROUPS.items():
        tail = r'(?=$|[^가-힣]|[은는이가을를의])' if group == '항목' else ''
        found = [(m.start(), aliases[word]) for word in aliases
                 for m in re.finditer(r'(?<![가-힣])' + re.escape(word) + tail, prefix)]
        if found:
            labels[group] = max(found)[1]
    if unit in ('mg', 'g', 'ml', '정'):
        match = re.search(r'([가-힣A-Za-z][가-힣A-Za-z_-]+)\s*[:：]?\s*$', prefix)
        if match:
            name = re.sub(r'(?:은|는|을|를|의)$', '', match[1])
            modifiers = r'^(?:약|현재|최근|지금|오늘|어제|매일|하루|일일|총|회당|복용|용량|처방|실제|평소|밤|새벽|오전|오후|아침|저녁|사이|정도)'
            if not re.search(modifiers, name) and not re.search(r'(?:에|에서|으로|이고|하며|하고|부터|까지)$', name):
                labels['약물'] = name
    return frozenset(labels.items())


def numeric_facts(text, question=''):
    """값·단위와 명시적 구분만 비교한다. 조사·부사는 항목 이름이 아니다."""
    text = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', text)
    # 보호자가 '저는 165, 아빠는 178cm'로 답하면 두 부모 중 명시하지 않은 쪽이 화자다.
    people = LABEL_GROUPS['사람']
    asked_people = {value for word, value in people.items() if word in question}
    named_people = {value for word, value in people.items() if word in text}
    if len(asked_people) == 2 and len(named_people) == 1 and re.search(r'저는|제\s*키', text):
        speaker = (asked_people - named_people).pop()
        text = re.sub(r'저는', speaker + '는', text)
        text = re.sub(r'제\s*키', speaker + ' 키', text)
    facts = set()
    found = sorted(set(korean_numbers.find(text)), key=lambda item: (item[1], item[2]))
    for value, start, end in found:
        unit_match = QUANTITY_UNIT.match(text, end)
        unit = UNIT_NAMES[unit_match[1].lower()] if unit_match else ''
        if text[start:end] in korean_numbers.DAYS:
            unit = '일'
        following = next(((s, e) for _, s, e in found if s >= end), None)
        if following:
            next_start, next_end = following
            next_unit = QUANTITY_UNIT.match(text, next_end)
            if not next_unit:
                second = next(((s, e) for _, s, e in found if s >= next_end), None)
                if second and re.fullmatch(r'\s*(?:~|～|–|-|에서|부터)\s*', text[next_end:second[0]]):
                    next_unit = QUANTITY_UNIT.match(text, second[1])
            if not unit and next_unit and re.fullmatch(r'\s*(?:~|～|–|-|에서|부터)\s*', text[end:next_start]):
                unit = UNIT_NAMES[next_unit[1].lower()]
            if unit == '일' and value == '1' and next_unit and UNIT_NAMES[next_unit[1].lower()] in ('회', '끼', '정'):
                if re.fullmatch(r'\s*(?:일)?\s*', text[end:next_start]):
                    continue  # '하루 세 끼'의 하루는 기간이 아니라 빈도 분모다.
        previous = max((e for _, _, e in found if e <= start), default=0)
        prefix = text[previous:start]
        labels = dict(quantity_labels(prefix, unit))
        for group, aliases in LABEL_GROUPS.items():
            asked_labels = {value for word, value in aliases.items() if word in question}
            if group not in labels and len(asked_labels) == 1:
                labels[group] = asked_labels.pop()
        if not unit:
            candidates = {UNIT_NAMES[m[1].lower()] for _, s, e in found if s > start
                          for m in [QUANTITY_UNIT.match(text, e)] if m}
            asked = {m[0].lower() for m in re.finditer(r'(?<![A-Za-z])(cm|kg|mg|ml|mmhg)(?![A-Za-z])', question, re.I)}
            if ('cm' in candidates and re.search(r'키|신장', question)
                    and not re.search(r'체중|몸무게', question)):
                unit = 'cm'
            elif len(candidates) == 1 and ('사람' in labels or re.search(r'키|신장', question)):
                unit = candidates.pop()  # '엄마 165, 아빠 178cm'의 공통 단위
            elif len(asked) == 1:
                unit = asked.pop()
        # 약 이름은 수치 바로 앞에서 읽는다. 앞 문장의 약을 다음 수치에 전파하지 않는다.
        previous_unit = QUANTITY_UNIT.match(prefix) if previous else None
        if previous_unit:
            prefix = prefix[previous_unit.end():]
        labels = frozenset((k, v) for k, v in labels.items() if k != '약물') | frozenset(
            (k, v) for k, v in quantity_labels(prefix, unit) if k == '약물')
        if unit not in ('cm', 'kg', 'mmhg'):
            labels = frozenset((k, v) for k, v in labels if k != '항목')
        if unit == '시':
            labels = frozenset((k, v) for k, v in labels if k != '시간대')
            local_period = dict(quantity_labels(prefix, unit)).get('시간대')
            if local_period in ('밤', '저녁') and 1 <= float(value) < 12:
                facts.add((str(int(value) + 12), unit, labels))
        canonical = {'년': ('개월', 12), '주': ('일', 7), '시간': ('분', 60)}
        if unit in canonical:
            unit, factor = canonical[unit]
            value = korean_numbers.plain(format(Decimal(value) * factor, 'f'))
        facts.add((value, unit, labels))
    return facts


def dated_ages(sources):
    """환자 생년월일과 명시적 측정 연월로 확정 가능한 당시 나이만 계산한다."""
    pattern = r'(\d{4})\s*년\s*(\d{1,2})\s*월(?:\s*(\d{1,2})\s*일)?'
    births, measurements = set(), []
    for text, question in sources:
        birth = bool(re.search(r'생년월일', question + text)) and not re.search(r'어머니|아버지|엄마|아빠', question)
        for match in re.finditer(pattern, text):
            y, m, d = match.groups()
            try:
                parsed = date(int(y), int(m), int(d or 1))
            except ValueError:
                continue
            if birth and d:
                births.add(parsed)
            elif re.search(r'키|신장|체중|몸무게|측정|검진|잰', question):
                measurements.append((parsed, bool(d)))
    derived = []
    if len(births) == 1:
        born = births.pop()
        for measured, full_day in measurements:
            if measured.month == born.month and not full_day:
                continue  # 생일 전후가 불명확하면 나이를 확정하지 않는다.
            age = measured.year - born.year - ((measured.month, measured.day) < (born.month, born.day))
            if 0 <= age <= 120:
                derived.append((str(age), measured.year, measured.month))
    return derived


def unheard_numbers(story, initial, events):
    sources = [(initial['opening'], '')]
    sources += [(e['reply'], e['content']) for e in answered(events, 'SAY')]
    heard = set().union(*(numeric_facts(text, question) for text, question in sources))
    for text, question in sources:
        if (re.search(r'(?:지난|최근)\s*\d+\s*(?:시간|일|주|개월|년)', question)
                and re.search(r'몇|얼마', question) and korean_numbers.find(text)
                and not re.search(r'모르|기억.*안|아니|안\s*먹', text)):
            heard.update((v, u, frozenset()) for v, u, _ in numeric_facts(question)
                         if u in ('분', '일', '개월'))
    derived = dated_ages(sources)
    def dated_age(key, value, unit, labels):
        compact = re.sub(r'\s+', '', story[key])
        return (key == 'hpi' and unit == '세' and dict(labels).get('시점') != '현재'
                and dict(labels).get('사람') not in ('어머니', '아버지')
                and any(value == age and f'{year}년{month}월' in compact for age, year, month in derived))
    written = [(key, fact) for key in STORY_KEYS for fact in numeric_facts(story[key])]
    wrong = [(value, unit, labels) for key, (value, unit, labels) in written
             if not dated_age(key, value, unit, labels)
             if not any(value == v and unit == u and all(dict(item).get(k, label) == label for k, label in labels)
                        for v, u, item in heard)]
    return sorted({f'{value}{unit}' + (f'[{dict(labels)}]' if labels else '') for value, unit, labels in wrong})


def write_story(ask, initial, events, enough_time=lambda: True):
    """S 수치의 조합을 검사한다. 한 번의 재작성도 실패하면 원문 기록을 요청한다."""
    request = dialogue(initial, events)
    age = korean_numbers.age(initial['opening'])
    if age:
        request += f'\n\n시작 정보에서 읽은 환자 나이: {age}세'
    spoken = [f"[턴 {e['turn']}] 환자: {e['reply']}" for e in answered(events, 'SAY') if korean_numbers.find(e['reply'])]
    if spoken:
        request += '\n\n환자가 숫자를 말한 문장(기록에서 빠뜨리지 않습니다):\n' + '\n'.join(spoken)
    story = clean(ask_json(ask, STORY_PROMPT, request, 4096, enough_time=enough_time), STORY_KEYS)
    if story is None:
        return None
    wrong = unheard_numbers(story, initial, events)
    if wrong:
        retry = clean(ask_json(ask, STORY_PROMPT, request + '\n\n직전 답의 수치·단위·항목 조합 ' + ', '.join(wrong)
                               + '은(는) 근거와 일치하지 않습니다. 환자 진술의 조합을 보존해 다시 작성하세요.', 4096, tries=1,
                               enough_time=enough_time), STORY_KEYS)
        if retry is not None and not unheard_numbers(retry, initial, events):
            return retry
        # 한 칸의 오류가 정상 약물력·가족력까지 지우지 않게 칸별로 복구한다.
        merged, invalid = dict.fromkeys(STORY_KEYS, ''), []
        for key in STORY_KEYS:
            values = [(retry or {}).get(key, ''), story[key]]
            for value in values:
                single = dict.fromkeys(STORY_KEYS, '') | {key: value}
                if value and not unheard_numbers(single, initial, events):
                    merged[key] = value
                    break
            if not merged[key] and any(values):
                invalid.append(key)
        if not any(merged.values()):
            return None
        if invalid:
            raw = initial['opening'] + '\n' + '\n'.join(
                f"(턴 {e['turn']}) 환자: {e['reply']}" for e in answered(events, 'SAY'))
            merged['hpi'] += '\n수치 재작성 미확인 항목(' + ', '.join(invalid) + '), 환자 원문:\n' + raw
        return {key: value.strip() for key, value in merged.items()}
    return story


def clean(value, keys):
    """모델 답에서 필요한 칸만 문자열로 꺼낸다. 목록으로 답한 칸은 줄로 잇는다."""
    if not isinstance(value, dict):
        return None
    out = {}
    for key in keys:
        item = value.get(key) or ''
        out[key] = '\n'.join(map(str, item)) if isinstance(item, list) else str(item).strip()
    return out


def slot_lines(section, marks):
    return [f"{label} ({'/'.join(marks.get(key, ' ') for key in keys)})" for label, keys in LAYOUT[section]]


def with_others(lines, others):
    return '\n'.join(lines + ([f'Others : {others}'] if others else []))


def fit(text, limit):
    return text.encode('utf-8')[:limit].decode('utf-8', 'ignore')


def start_information(session):
    """시작 정보를 글자로 맞춘다. 값이 없거나 글자가 아니어도 기록 작성이 죽지 않게 한다."""
    initial = (session or {}).get('initial') or {}
    return {'opening': str(initial.get('opening') or ''), 'vital_signs': str(initial.get('vital_signs') or '')}


def minimal(session, events):
    """모델 없이, 어떤 입력에서도 낼 수 있는 기록. 다른 길이 다 막혔을 때 쓴다."""
    note = empty_record()
    try:
        initial = start_information(session)
        note['S']['cc'] = f"C.C : {initial['opening']}"
        note['S']['hpi'] = '\n'.join(f"의사: {e['content']} / 환자: {e['reply']}" for e in answered(events, 'SAY'))
        note['O']['exam'] = exam_text(initial, events)
        note['P']['education'] = education_text(events)
    except Exception as exc:
        sys.stderr.write(f'[record] 최소 기록도 실패: {type(exc).__name__}\n')
        note = empty_record()
    if not any(value.strip() for fields in note.values() for value in fields.values()):
        note['S']['cc'] = 'C.C : ( )'  # 칸이 전부 비면 서버가 기록을 받지 않는다
    return note, NO_DIAGNOSIS


def prepare_plan(ask, session, events, time_left=lambda: float('inf')):
    """기존 A/P 생성 호출을 설명 전에 수행해 설명·제출이 같은 판단을 사용한다."""
    if time_left() < MIN_SECONDS:
        return None
    raw = ask_json(ask, PLAN_PROMPT, dialogue(start_information(session), events), 4096,
                   enough_time=lambda: time_left() >= MIN_SECONDS)
    plan = clean(raw, PLAN_KEYS + ('diagnosis',))
    if plan:
        guard_breathing_denial(plan, events, start_information(session))
    return plan if plan and plan['diagnosis'].strip() else None


def guard_breathing_denial(plan, events, initial=None):
    """관측된 부분 답변의 A 음성 확장을 보류한다. 전체 A 의미 검증은 아니다."""
    breathing = r'호흡|숨쉬|숨이\s*차|숨차|숨찬'
    supported = False
    sources = answered(events, 'SAY') + [{'content': '', 'reply': (initial or {}).get('opening', '')}]
    for event in sources:
        question, reply = event['content'], event['reply']
        if re.search(breathing, reply):
            start = re.search(breathing, reply).start()
            clause = re.split(r'가슴|흉통|실신|졸림|졸리|의식', reply[start:], maxsplit=1)[0]
            supported |= bool(re.search(r'괜찮|없(?:어|습|음)|안\s*힘들', clause)
                              and not re.search(r'괜찮지|없지|모르', clause))
        elif (re.search(breathing, question) and not re.search(r'졸|의식|흉통|실신|기침|가슴', question)
              and re.fullmatch(r'(?:아니요[,\s]*)?(?:없어요|괜찮아요)[.!\s]*', reply)):
            supported = True
    if not supported:
        pattern = r'호흡곤란[^.。\n]{0,50}(?:부인(?:함|하)|없(?:음|습니다|다고|고)|확인되지\s*않)'
        for key in ('evidence', 'differential'):
            if not re.search(pattern, plan[key]):
                continue
            clauses = re.split(r'(?<=[.。])\s+|\n', plan[key])
            plan[key] = '\n'.join('호흡곤란의 음성 여부는 미확인임; 환자 원문 확인 필요.'
                                  if re.search(pattern, clause) else clause for clause in clauses)


def acknowledgment(reply):
    """새 사실이 없는 짧은 수긍만 허용 목록으로 판별한다."""
    return re.sub(r'[\s.!。]', '', reply).casefold() in ('', '네', '예', '넵', '알겠습니다', '네알겠습니다', '감사합니다')


def build(ask, session, events, time_left=lambda: float('inf'), prepared_plan=None, plan_turn=0,
          on_plan_ready=None):
    """(note, diagnosis). ask(system, user, max_tokens)는 Doctor 모델의 답 글을 돌려준다."""
    initial = start_information(session)

    def enough_time():
        return time_left() >= MIN_SECONDS

    def step(name, work):
        if not enough_time():
            return None
        try:
            return work()
        except Exception as exc:  # 한 단계가 죽어도 나머지로 기록을 낸다
            sys.stderr.write(f'[record] {name} 단계 실패: {type(exc).__name__}\n')
            return None

    talk = dialogue(initial, events)
    plan = clean(prepared_plan, PLAN_KEYS + ('diagnosis',))
    shared = bool(plan and plan['diagnosis'].strip())
    if not shared:
        plan_raw = step('plan', lambda: ask_json(ask, PLAN_PROMPT, talk, 4096, enough_time=enough_time)) or {}
        plan = clean(plan_raw, PLAN_KEYS + ('diagnosis',))
        shared = bool(plan and plan['diagnosis'].strip())
    guard_breathing_denial(plan, events, initial)
    def review(current, last_turn):
        marks_prompt, marks_talk = MARKS_PROMPT, dialogue(initial, events)
        if shared:
            marks_prompt += ('\n추가 키 plan_update: 기존 계획 이후 설명 중 새로 확인된 환자 사실이 판단·처치를 바꾸면 '
                             '{"turn": 번호, "quote": 환자 답변 전체 원문}, 아니면 null. 단순 동의·걱정은 새 임상 사실이 아닙니다.')
            marks_talk += f'\n\n기존 계획의 마지막 근거 턴: {last_turn}\n공통 계획: ' + json.dumps(current, ensure_ascii=False)
        raw = step('marks', lambda: ask_json(ask, marks_prompt, marks_talk, 4096, enough_time=enough_time))
        change = (raw or {}).get('plan_update')
        source = next((event for event in answered(events, 'SAY') if shared and isinstance(change, dict)
                       and event.get('explain') and event['turn'] > last_turn
                       and event['turn'] == change.get('turn') and event['reply'].strip()
                       and event['reply'] == change.get('quote')), None)
        changed = False
        if source:
            updated = step('plan-update', lambda: prepare_plan(ask, session, events, time_left))
            if updated:
                changed = any(updated[key] != current[key]
                              for key in ('diagnosis', 'tests', 'treatment', 'disposition', 'follow_up'))
                current = updated
            status = '추가 진술로 계획을 재평가함' if updated else '추가 진술에 대한 계획 재평가 미완료'
            current['evidence'] += f"\n- {status} (턴 {source['turn']}): {source['reply']}"
        return current, raw, changed

    plan, marks_raw, changed = review(plan, plan_turn)
    late = []
    if marks_raw is None and any(not acknowledgment(event['reply']) for event in answered(events, 'SAY')
                                 if event.get('explain') and event['turn'] > plan_turn):
        plan['evidence'] += '\n- 설명 중 추가 진술의 계획 영향 확인 미완료; 후속 확인 필요'
    if on_plan_ready:
        last_turn = events[-1]['turn'] if events else 0
        try:
            on_plan_ready(plan, changed)
        except Exception as exc:
            sys.stderr.write(f'[record] 마지막 설명 실패: {type(exc).__name__}\n')
        late = [event for event in answered(events, 'SAY') if event['turn'] > last_turn
                and not acknowledgment(event['reply'])]
        if late:
            plan, late_marks, late_changed = review(plan, last_turn)
            marks_raw = {**(marks_raw or {}), **(late_marks or {})}
            if late_changed or late_marks is None:
                plan['evidence'] += '\n- 마지막 환자 답변 후 변경 판단·계획의 추가 설명 미완료; 후속 확인 필요'
        if changed and not any(event.get('plan_revision_delivered') for event in events
                               if event['turn'] > last_turn):
            plan['evidence'] += '\n- 변경 판단·계획의 추가 설명 미완료; 후속 확인 필요'
    elif changed:
        plan['evidence'] += '\n- 변경 판단·계획의 추가 설명 미완료; 후속 확인 필요'
    marks = checked_marks(marks_raw or {}, initial, events)
    story = step('story', lambda: write_story(ask, initial, events, enough_time))

    note = empty_record()
    if story is None:  # 서술을 못 받았으면 들은 말을 그대로 싣는다
        note['S']['cc'] = f"C.C : {initial['opening']}"
        note['S']['hpi'] = '\n'.join(f"의사: {e['content']} / 환자: {e['reply']}" for e in answered(events, 'SAY'))
        story = dict.fromkeys(STORY_KEYS, '')
    else:
        note['S']['cc'] = f"C.C : {story['cc']}\nOnset : {story['onset'] or '( )'}"
        note['S']['hpi'] = story['hpi']
        for event in late:
            note['S']['hpi'] += f"\n마지막 설명 후 환자 원문 (턴 {event['turn']}): {event['reply']}"
    pmh = slot_lines('pmh', marks)
    pmh.insert(-1, f"Medicine history : {story['medicine'] or '( )'}")  # Allergy 앞
    note['S']['pmh'] = with_others(pmh, story['pmh_others'])
    note['S']['fh'] = with_others(slot_lines('fh', marks), story['fh_others'])
    note['S']['sh'] = with_others(slot_lines('sh', marks), story['sh_others'])
    note['S']['ros'] = with_others(slot_lines('ros', marks), story['ros_others'])
    note['O']['exam'] = exam_text(initial, events)
    note['A'].update(evidence=plan['evidence'], differential=plan['differential'])
    for field in RECORD_FIELDS['P']:
        note['P'][field] = education_text(events) if field == 'education' else plan[field]
    diagnosis = fit(' '.join(plan['diagnosis'].split()), DIAGNOSIS_MAX_BYTES) or NO_DIAGNOSIS
    size = sum(len(value.encode('utf-8')) for fields in note.values() for value in fields.values())
    if size > NOTE_MAX_BYTES - 5_000:  # 한도에 닿기 전에 가장 긴 칸인 현병력을 줄인다
        note['S']['hpi'] = fit(note['S']['hpi'], max(0, len(note['S']['hpi'].encode('utf-8')) - (size - NOTE_MAX_BYTES + 5_000)))
    return note, diagnosis
