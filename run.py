"""팀원 v1b6 t40 r2 기반 예선 에이전트. 문진·설명·초진 기록 개선을 적용한 개발본입니다.
서버나 모델이 잠시 안 될 때 다시 보내고 기다리는 기존 복구 경로를 사용합니다.
다시 보내는 요청은 같은 요청 번호를 씁니다(2026-10-13 대회 문서의 재시도 규칙).
진료 시간(30분)은 시계 하나로 재고, 응답이 느린 날에는 설명과 기록에 쓸 시간을 먼저 떼어 둡니다. 기록은 시간이 끝날 때까지 냅니다.

흐름
1. 문진과 진찰: 지정 Doctor 모델(client.chat)이 매 턴 다음 행동 하나를 JSON으로 고릅니다.
   SAY = 환자에게 질문 한 마디, EXAM = 신체 진찰 동작 하나, DONE = 문진 종료.
   행동 결과(환자 대답, 진찰 소견)를 진료 기록에 붙여 다음 턴에 다시 보여 줍니다.
2. 설명: explain.py가 매번 최신 환자 반응을 읽고 다음 안내·확인 한 문장을 만들어 SAY로 말합니다.
3. 기록: record.py가 S·O·A·P 초진 기록과 주진단을 만들어 제출합니다.
   양식과 진찰 소견은 코드가 채우고, Doctor 모델에게는 대화에서 확인된 내용과 평가·계획을 묻습니다.
"""
import json
import re
import sys
import time
from datetime import datetime, timezone

import explain
import record
from doctor import action_text, ask, attempt, failure, new_id, parse_json, with_retry
from nova_sdk import NovaError, run_agent

MAX_TURNS = 40      # 대회 한도 50턴 중 이 판이 쓰는 턴 수
EXPLAIN_TURNS = 5   # 그중 끝에 설명에 쓰는 턴. 0이면 설명을 문진 흐름 안에서 모델이 알아서 합니다(시작 패키지 방식)
MAX_CHARS = 30      # SAY·EXAM 한 번에 30자(공백 포함, NFC) 이하
FORMAT_FAILURE_LIMIT = 3  # 문진 전체의 형식 복구 묶음 소진 한도(묶음당 최대 3호출)
STOP_CODES = {'TURN_LIMIT_REACHED', 'SESSION_EXPIRED', 'SESSION_CLOSED', 'RUN_NOT_ACTIVE'}
TURN_REJECT_CODES = {'EXAM_NOT_SPECIFIC', 'EXAM_NOT_RECOGNIZED', 'CONTENT_TOO_LONG', 'EMPTY_CONTENT'}

# 진료 시간. 정상 진료는 7분 안에 끝나므로 아래 숫자들은 서버나 모델이 느린 날에만 쓰입니다
SESSION_SECONDS = 1800  # 진료 한도 30분
START_SLACK = 30    # 시작 시각으로 잰 한도는 이만큼 앞당겨 잡습니다(초). 서버의 시계는 우리가 시작하기 조금 전에 돌기 시작합니다
RESERVE = 40        # 기록을 내는 데 남겨 두는 시간(초). 다른 호출은 이 시간을 건드리지 않습니다
MIN_TIMEOUT = 5     # 호출 하나의 대기 시간을 줄일 때의 아래 끝(초)
PACE = (10.0, 5.0)  # 아직 재지 못했을 때 호출 하나에 걸린다고 보는 시간(초): Doctor 모델, 환자와의 문답
SLACK = 1.5         # 앞으로 들 시간은 요즘 걸리는 시간의 이만큼으로 넉넉히 잡습니다
INTERVIEW_MARGIN = (300, 600)  # 문진을 멈추고 설명과 기록으로 넘어가는 때: 마감 5분 전에서 10분 전 사이(응답이 느릴수록 일찍)
RECORD_NEED = (50, 200)        # 설명을 하는 동안 기록용으로 떼어 두는 시간(초)의 아래 끝과 위 끝
LAST_RESORT = 300   # 기록이 접수되지 않은 채 마감이 이만큼 남으면 최소 기록도 번갈아 보냅니다(초)
REJECTED_STATUS = {400, 413, 422}    # 보낸 기록이 거절된 것(칸, 크기, 형식). 같은 기록을 다시 보내도 소용없습니다
NOT_REJECTED = {408, 409, 425, 429}  # 4xx 가운데 이것들은 잠시 안 되는 것입니다
FULL_STRIKES = 3    # 그 밖의 4xx는 온전한 기록에 세 번 이어지면 거절로 보고 최소 기록으로 바꿉니다(한 번은 중간 서버의 실수일 수 있습니다)
CLOSED_CODES = {'SESSION_CLOSED', 'SESSION_EXPIRED', 'RUN_NOT_ACTIVE'}  # 이미 접수됐거나 세션이 끝남. 더 보낼 수 없습니다
SUBMIT_WAITS = (1, 2, 5, 10)         # 기록 제출을 다시 보내는 간격(초). 마지막 값으로 계속 보냅니다

# 설명 단계가 따로 있으면 문진 흐름에서는 설명을 하지 않습니다
FLOW_END = ('관련 신체 진찰 → DONE.\n진단과 계획 설명은 진료 끝에 따로 하니 여기서는 하지 않습니다.' if EXPLAIN_TURNS
            else '관련 신체 진찰 → 환자에게 가능성 높은 진단과 다음 계획 설명 → DONE.')
STEP_PROMPT = f"""당신은 외래 진료실의 의사입니다. 가상 환자를 진료합니다.
매번 아래 JSON 객체 하나만 출력하세요.
{{"action": "SAY", "content": "환자에게 할 말", "purpose": "question"}}  질문 하나({MAX_CHARS}자 이내)
{{"action": "SAY", "content": "환자에게 할 말", "purpose": "guidance"}}  실제 안내·설명 한 마디({MAX_CHARS}자 이내)
{{"action": "EXAM", "content": "진찰 동작 하나"}}  예: "복부 압통 확인하겠습니다"({MAX_CHARS}자 이내)
{{"action": "DONE", "history_gap": "아직 물을 핵심 질문 하나 또는 빈 문자열"}}  진료를 마칠 때
진행: 주호소·현병력(시작·경과·양상·악화/완화·동반 증상·일상 기능) → 관련 과거력·이전 치료·복용 약·알레르기
→ 관련 가족력·사회력(생활·직업·흡연·음주)·계통 문진 → {FLOW_END}
초기 정보와 실제 답변을 보고 판단·계획에 필요한 미확인 항목 하나를 선택하세요. 한 가설이나 위험만 확인하느라 다른 영역의 중요한 병력·진찰을 빠뜨리지 마세요.
응급 위험은 먼저 대응하고 불안정한 환자의 이송·처치를 지연하지 마세요. 안정 상태에서는 남은 병력·진찰을 이어가며, 확인한 위험·조치는 새 단서 없이 반복하지 마세요.
질문은 한 번에 확인 대상 하나만 담으세요. 질문했어도 답이 모호하거나 일부만 답했으면 미확인입니다. 중요한 답만 구체화하고, 이미 답을 얻었거나 얻을 수 없는 정보는 반복 요구하지 마세요. 약은 처방과 실제 복용을 구분하세요.
현병력은 증상의 존재만 아니라 실제 양·빈도·시간, 변화 전후, 일상 기능, 이전 치료와 반응까지 확인하세요. 불면이면 실제 총 수면시간·잠드는 시간·수면 습관, 성장이나 체중 변화면 날짜별 측정·섭취·관련 가족력을 환자별로 선택하세요. 약물 위험 확인만으로 원래 주호소 문진을 끝내지 마세요.
진찰은 한 부위에서 시행할 방법 하나를 구체적으로 요청하세요. 서로 다른 관찰·움직임을 묶거나 여러 부위를 포괄적으로 살펴달라고 하지 마세요. 거절된 요청은 구체화하고, 요청한 소견이 없으면 미확인으로 두세요.
DONE 전에 관련 병력과 필요한 진찰 결과를 확보했는지 점검하세요. 남은 턴에 얻을 수 있는 중요한 정보가 있으면 history_gap에 그 질문 하나를 30자 이내로 쓰세요. 이미 답을 얻었거나 환자가 모르거나 답변을 거부한 항목, 관련 없는 항목은 넣지 마세요. 응급 이송·처치가 우선이면 문진을 지연하지 말고 history_gap은 비우세요. 다음 행동 하나만 출력하세요."""
STEP_PROMPT += ('\n응급 안내를 했다는 사실만으로 주호소·약물 병력이 완료된 것은 아닙니다. '
                '이송을 기다리며 대화가 가능한 경우 처방량·복용 기간·감량/누락 반응 등 인계에 필요한 미확인 정보만 짧게 확인하세요. '
                '즉시 이송·처치가 우선이라 더 묻지 않아야 하면 DONE에 urgent_transfer: '
                '{"turn": 근거 턴, "quote": 환자 답변 전체 원문, "reason": 즉시 종료 이유}를 적으세요. '
                '호흡과 의식 등 서로 다른 위험을 묶어 물어 일부만 답하면 나머지는 미확인입니다.')
STEP_PROMPT += '\n이미 한 안내를 새 상태 악화 때문에 다시 해야 할 때만 urgent_reply에 직전 환자 답변 전체 원문을 넣으세요.'


class ActionFormatError(ValueError):
    """환자에게 보내지 못한 출력. DONE이나 시간 소진과 구분한다."""


def prepare():
    return None


def clamp(value, low, high):
    return max(low, min(high, value))


def scrub(value):
    """서버에서 받은 글에 UTF-8로 못 바꾸는 글자(짝 없는 대리쌍)가 있으면 뺍니다. 그대로 두면 그 글이 든 요청을 보낼 수 없습니다."""
    if isinstance(value, str):
        return value.encode('utf-8', 'ignore').decode('utf-8')
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


class Clock:
    """진료에 남은 시간. 서버가 준 마감과 '시작하고 30분' 중 이른 쪽을 따릅니다.
    마감을 못 읽거나 말이 안 되면(이미 지났거나 30분보다 멀리 있음) 시작 시각만으로 잽니다.
    흐른 시간은 뒤로 가지 않는 시계(time.monotonic)로 잽니다. 컴퓨터 시계가 보정돼 뛰어도 흔들리지 않게 하려는 것입니다."""

    def __init__(self):
        self.end = time.monotonic() + SESSION_SECONDS - START_SLACK

    def sync(self, session):
        deadline = session.get('deadline') if isinstance(session, dict) else None
        try:
            if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
                end = float(deadline)
            else:
                moment = datetime.fromisoformat(deadline)
                if moment.tzinfo is None:  # 시간대가 없으면 UTC로 봅니다
                    moment = moment.replace(tzinfo=timezone.utc)
                end = moment.timestamp()
        except (TypeError, ValueError):
            return
        left = end - time.time()
        if 0 < left <= SESSION_SECONDS + START_SLACK:
            self.end = min(self.end, time.monotonic() + left)

    def left(self):
        return self.end - time.monotonic()


class Calls:
    """SDK client 앞에 서서 세 가지를 합니다.
    1. 호출 직전에 SDK의 대기 시간(client.timeout)을 남은 진료 시간에 맞춰 줄입니다. SDK 파일은 고치지 않고 값만 정합니다.
       시간이 넉넉하면 SDK가 정한 값 그대로입니다. (응답이 통째로 안 올 때의 보호입니다. 응답을 조금씩 흘려보내는 서버는 막지 못합니다.)
    2. 성공한 호출에 걸린 시간을 적어 둡니다. 느린 날에 설명과 기록에 쓸 시간을 떼어 두는 데 씁니다.
    3. 받은 글에서 UTF-8로 못 바꾸는 글자를 뺍니다."""

    def __init__(self, client, clock):
        self.client, self.clock = client, clock
        self.patience = getattr(client, 'timeout', None)
        self.took = {'chat': [], 'act': []}

    def call(self, kind, reserve, work):
        if self.patience is not None:
            self.client.timeout = max(MIN_TIMEOUT, min(self.patience, self.clock.left() - reserve))
        started = time.monotonic()
        result = scrub(work())
        if kind:
            self.took[kind] = self.took[kind][-4:] + [time.monotonic() - started]
        return result

    def pace(self):
        """요즘 호출 하나에 걸리는 시간(초): (Doctor 모델, 환자와의 문답). 최근 성공한 다섯 번의 가운데 값입니다."""
        return tuple(sorted(values)[len(values) // 2] if values else default
                     for values, default in zip((self.took['chat'], self.took['act']), PACE))

    def need(self, chats=0, says=0):
        """앞으로 Doctor 호출 chats번과 환자와의 문답 says번에 들 것으로 보는 시간(초)."""
        chat, say = self.pace()
        return SLACK * (chats * chat + says * say)

    def record_need(self):
        """기록을 쓰고 내는 데 떼어 둘 시간(초). Doctor 호출 세 번(계획, 표시, 서술)이고, 못해도 주진단을 정하는 한 번은 남깁니다."""
        return RESERVE + max(clamp(self.need(chats=3), *RECORD_NEED), self.need(chats=1))

    def session(self):
        return self.call(None, RESERVE, self.client.session)

    def say(self, content, *, request_id):
        def work():
            reply = self.client.say(content, request_id=request_id)
            reserve = self.record_need()
            if isinstance(reply.get('effective_content'), str) or self.clock.left() <= reserve + MIN_TIMEOUT:
                return reply
            # 실제 API는 전달문을 행동 응답 대신 세션 이벤트에 넣는다.
            # 조회 실패가 성공한 SAY의 재전송으로 이어지지 않게 여기서 처리한다.
            try:
                state = self.call(None, reserve, lambda: attempt(self.client.session))
            except NovaError:
                sys.stderr.write('[say] 전달문 조회 실패; 환자 답변은 보존\n')
                return reply
            history = state.get('events') if isinstance(state, dict) else None
            for event in reversed(history if isinstance(history, list) else []):
                if not isinstance(event, dict):
                    continue
                payload = event.get('payload')
                if (event.get('action') == 'SAY' and isinstance(payload, dict)
                        and payload.get('request_id') == request_id
                        and event.get('turn') == reply.get('turn')
                        and isinstance(event.get('effective_content'), str)):
                    return {**reply, 'effective_content': event['effective_content']}
            return reply

        return self.call('act', RESERVE, work)

    def exam(self, content, *, request_id):
        return self.call('act', RESERVE, lambda: self.client.exam(content, request_id=request_id))

    def chat(self, messages, **options):
        return self.call('chat', RESERVE, lambda: self.client.chat(messages, **options))

    def diagnose(self, *, note, diagnosis, request_id):
        return self.call(None, 0, lambda: self.client.diagnose(note=note, diagnosis=diagnosis, request_id=request_id))


def situation(session, transcript):
    initial = session['initial']
    return (f"시작 정보\n첫 진술: {initial['opening']}\n활력징후: {initial['vital_signs']}\n\n"
            '진료 기록\n' + ('\n'.join(transcript) or '(아직 없음)'))


def repeated_exam(step, content, events):
    """동일 진찰은 새 환자 진술에 근거한 재평가 사유가 있을 때만 허용한다."""
    key = lambda text: ''.join(text.split()).rstrip('.').casefold()
    target = record.measurement(content)
    previous = [event for event in events if event['action'] == 'EXAM'
                and (key(event['content']) == key(content)
                     or (target and record.measurement(event['content']) == target))]
    if not previous:
        return False
    last = previous[-1]
    reason = step.get('repeat_reason')
    source_turn = step.get('repeat_turn')
    quote = step.get('repeat_quote')
    justified = (
        isinstance(reason, str) and reason.strip() and isinstance(quote, str) and quote.strip()
        and any(event['action'] == 'SAY' and event.get('turn') == source_turn
                and event['turn'] > last['turn'] and event.get('reply') == quote for event in events))
    if key(last['content']) == key(content):
        return bool(last.get('refused')) or not justified
    if justified:
        return False
    # 필요한 수치가 오지 않았으면 표현을 구체화할 기회는 한 번만 준다.
    return len(previous) >= 2 or (not last.get('refused')
        and record.exam_feedback(last['content'], last.get('reply', '')) == last.get('reply', ''))


def compound_exam(content):
    """구체적 방법 없이 여러 대상을 묶은 확인·관찰은 보내기 전에 구체화한다."""
    if re.search(r'전신|전체|여러\s*부위', content) and re.search(r'외형|기형|특징적|이상\s*소견', content):
        return True
    return (bool(re.search(r'[가-힣]+(?:과|와)\s+[가-힣]+|\s및\s', content))
            and bool(re.search(r'확인|살펴|관찰', content))
            and not re.search(r'촉진|만져|청진|타진|측정|재어|신장계', content))


SEDATIVE_HANDOFF = (
    ('breathing', r'숨|호흡', r'호흡|숨쉬|숨이\s*차|숨차|숨을\s*못\s*쉬|숨은\s*(?:괜찮|차)', '지금 숨쉬기 힘드신가요?'),
    ('strength', r'(?:한|1)\s*(?:알|정).*(?:함량|mg|밀리)|정당|함량',
     r'(?:한|1)\s*(?:알|정).*(?:mg|밀리)|함량', '한 알의 함량은 몇 mg인가요?'),
    ('today', r'오늘.*(?:총|모두|합쳐|몇\s*(?:mg|알|정))',
     r'오늘.*(?:총|모두|합쳐)', '오늘 총 몇 mg을 복용하셨나요?'),
    ('prescription', r'처방.*(?:량|용량|몇|mg)|처방은', r'처방', '처방받은 1회 복용량은 얼마인가요?'),
    ('duration', r'(?:복용|드신|먹은).*(?:기간|언제부터|얼마나\s*오래|시작)|(?:언제부터|얼마나\s*오래).*(?:복용|드셨)',
     r'복용.*(?:기간|시작|년|개월)|(?:년|개월).*(?:복용|먹|드)', '약 복용을 시작한 지 얼마나 됐나요?'),
    ('control', r'(?:줄|감량|끊|중단).*(?:실패|시도|적|봤|보셨)',
     r'(?:줄|감량|끊).*(?:실패|못|성공|적.*없)', '약을 줄이려다 실패한 적이 있나요?'),
    ('withdrawal', r'거르|빠뜨|누락|늦어|(?:중단|감량|줄|끊).*증상',
     r'(?:거르|빠뜨|누락|늦).*(?:떨|땀|불안|없|경련|불면)|금단.*(?:있|없|겪)',
     '약을 거르면 어떤 증상이 생기나요?'),
    ('function', r'기억|낮.*졸|졸.*(?:생활|일)|일상|일.*지장',
     r'기억.*(?:끊|안\s*나|잃)|졸.*(?:일|생활)|일상.*지장',
     '복용 후 일상생활에 지장이 있나요?'),
)


def handoff_quantity(answer, units):
    return any(re.match(r'\s*(?:' + units + ')', answer[end:], re.I)
               for _, _, end in record.korean_numbers.find(answer))


def sedative_handoff_coverage(events):
    """수면제 인계의 답변 근거 장부. 상태는 진단 기준 충족/양성 판정이 아니다."""
    # ponytail: 명시적 표현과 단위만 읽는다. 자유 진술의 임상 의미 전체를 판정하지 않는다.
    replies = [e for e in record.answered(events, 'SAY') if not e.get('refused')]
    patient = '\n'.join(e['reply'] for e in replies)
    if not (re.search(r'수면제|졸피뎀|스틸녹스|진정제', patient)
            and any(handoff_quantity(e['reply'], r'mg|밀리그램|밀리|알|정') for e in replies)):
        return None
    coverage = {}
    for key, pattern, explicit, question in SEDATIVE_HANDOFF:
        attempts, sources, unavailable = 0, [], False
        for e in events:
            if e['action'] != 'SAY' or e.get('purpose') == 'guidance' or e.get('explain'):
                continue
            answer = e.get('reply', '').strip()
            topics = [name for name, match, _, _ in SEDATIVE_HANDOFF if re.search(match, e['content'], re.I)]
            if 'control' in topics and re.search(r'경련|금단|증상|떨|식은땀', e['content']) \
                    and not re.search(r'실패|시도', e['content']):
                topics.remove('control')
            # 졸림·호흡을 묶어 묻고 졸림만 부인한 답을 호흡 음성으로 채우지 않는다.
            focused = topics == [key] and not (key == 'breathing' and re.search(r'졸|멍|의식|반응', e['content']))
            asked = key in topics
            attempts += bool(asked)
            if e.get('refused') or e.get('delivery_verified') is False:
                continue
            stated = bool(re.search(explicit, answer, re.I))
            if not focused and not stated:
                continue
            parts = re.split(r'[,;\n]|(?<!\d)\.(?!\d)|이고|이며|지만|(?<=고)\s+(?=실제)', answer)
            value = next((part for part in parts if re.search(explicit, part, re.I)), answer if focused else '')
            if re.search(r'모르|답.*(?:싫|않|거부)|말.*(?:싫|않)', value):
                unavailable = True
                sources = []
                continue
            if not answer or answer == record.NO_ANSWER:
                continue
            if key in ('strength', 'today', 'prescription'):
                valid = handoff_quantity(value, r'mg|밀리그램|밀리' + (r'|알|정' if key == 'prescription' else ''))
                valid |= focused and bool(re.fullmatch(r'\d+(?:\.\d+)?(?:이요|요|입니다|이에요)?[.!\s]*', value))
            elif key == 'duration':
                valid = handoff_quantity(value, r'년|개월|달|주|일')
            else:
                valid = stated or (focused and bool(re.search(
                    (r'^아니' if key == 'withdrawal' else r'^(?:네|예|아니)')
                    + r'|없|괜찮|힘들|떨|땀|경련|불안|불면|실패|못|졸|지장', value)))
            # 다른 항목의 숫자를 현재 질문의 답으로 빌리지 않는다.
            if not stated and ((key == 'duration' and re.search(r'불면|수면\s*시간|잠들', value))
                    or (key == 'prescription' and re.search(r'실제|평소|오늘', value))
                    or (key == 'today' and re.search(r'평소|보통|매일', value))
                    or (key == 'strength' and re.search(r'총|합쳐|모두', value))):
                valid = False
            if valid:
                source = {'turn': e['turn'], 'quote': answer}
                if key == 'breathing':
                    # 음성 여부가 불명확하면 병력 보충을 강제하지 않는다. 안정 판정은 아니다.
                    source['clear_negative'] = bool(focused and re.fullmatch(
                        r'(?:(?:아니요|아뇨)[,\s]*)?(?:(?:숨쉬는\s*건|숨쉬기(?:는|가)?|호흡은)\s*)?'
                        r'(?:괜찮아요|문제\s*없어요|안\s*힘들어요|힘들지\s*않아요|없어요'
                        r'|호흡곤란(?:은|이)?\s*없어요|숨이\s*차지(?:는)?\s*않아요)?[.!\s]*', answer))
                sources.append(source)
        status = 'answered' if sources else ('unavailable' if unavailable else ('unresolved' if attempts >= 2 else 'missing'))
        coverage[key] = {'status': status, 'sources': sources, 'attempts': attempts,
                         'question': ('다시, ' if attempts and status == 'missing' else '') + question}
    return coverage


def immediate_handoff(step, replies):
    urgent = step.get('urgent_transfer')
    if isinstance(urgent, dict) and isinstance(urgent.get('reason'), str) and urgent['reason'].strip():
        source = next((e for e in replies if e['turn'] == urgent.get('turn')
                       and e['reply'] == urgent.get('quote') and not record.acknowledgment(e['reply'])), None)
        if source is None:
            return False
        if sedative_handoff_coverage(replies) is None:
            return True
        # 수면제 인계에서 이송 동의·용량·막연한 낮 졸림을 현재 불안정의 증거로 쓰지 않는다.
        for clause in re.split(r'[,.]|지만|(?<=고)\s*(?=숨|호흡|의식)', source['reply']):
            quote = re.sub(r'\s+', '', clause)
            if re.search(r'못쉬지(?:는)?않|(?:의식저하|호흡곤란|경련|실신).{0,5}(?:없|않)', quote):
                continue
            severe = re.search(r'숨.{0,8}못쉬|호흡.{0,8}(?:안돼|힘들|곤란)'
                               r'|의식.{0,8}(?:없|잃|저하)|(?:경련|실신)(?:했|해|중)'
                               r'|깨[우워].{0,12}(?:어렵|반응.{0,3}없)|반응.{0,5}없|심하게.{0,5}졸', quote)
            if severe:
                return True
            if re.search(SEDATIVE_HANDOFF[0][2] + r'|의식|경련|실신|쓰러|반응|깨우|깨워', quote) \
                    and not re.search(r'없|않|아니|괜찮|멍|낮에도|좀졸|약간졸', quote):
                return True
        return False
    return False


def handoff_stopped(replies):
    return bool(replies and re.search(
        r'(?:구급대|구급차)(?:가|는)?\s*(?:도착했|왔어요)|지금\s*(?:이송|출발)\s*중'
        r'|질문.{0,5}그만|더\s*묻지', replies[-1]['reply']))


def handoff_transfer(replies):
    # 한 문장에 동행 안내가 있어도 별도로 포함된 이송 지시를 잃지 않는다.
    return any(e.get('purpose') == 'guidance' and re.search(r'119|응급실|구급대', e['content'])
               for e in replies)


def sedative_termination_check(step, events):
    """부적절한 즉시 종료 인용이면 현재 상태를 먼저 진찰한다. 이송 안내는 유지한다."""
    if not isinstance(step.get('urgent_transfer'), dict) or sedative_handoff_coverage(events) is None:
        return None
    replies = [e for e in record.answered(events, 'SAY') if not e.get('refused')]
    if not handoff_transfer(replies):
        return None
    if handoff_stopped(replies) or immediate_handoff(step, replies):
        return 'DONE', '', ''
    attempted = [e for e in events if e['action'] == 'EXAM' and re.search(r'지남력|의식\s*(?:수준|상태)', e['content'])]
    if not attempted:
        return 'EXAM', '사람·장소·시간의 지남력을 확인하겠습니다', ''
    latest = attempted[-1]
    result = latest.get('reply', '')
    if latest.get('refused') or not (re.search(r'명료', result) and re.search(r'지남력\s*3\s*/\s*3', result)):
        return 'DONE', '', ''  # 정상 여부 미확인이면 배경 문진을 강제하지 않는다.
    return None  # 정상 지남력 하나로 안정 판정하지 않고 호흡 확인·인계 병력을 이어간다.


def handoff_gap(step, events):
    replies = [e for e in record.answered(events, 'SAY') if not e.get('refused')]
    if immediate_handoff(step, replies):
        return ''  # 실제 근거를 인용한 즉시 이송 판단을 질문 때문에 지연하지 않는다.
    coverage = sedative_handoff_coverage(events) if handoff_transfer(replies) else None
    if coverage is not None:
        if handoff_stopped(replies):
            return ''
        breathing = coverage['breathing']
        if breathing['status'] != 'missing' and not (
                breathing['sources'] and breathing['sources'][-1].get('clear_negative')):
            return ''
        return next((item['question'] for item in coverage.values() if item['status'] == 'missing'), '')
    if not (any(e.get('purpose') == 'guidance' and explain.guidance_key(e['content']) == '응급 이송' for e in replies)
            and any(re.search(r'\d+\s*(?:mg|정|알)', e['reply'], re.I) for e in replies)):
        return ''
    questions = '\n'.join(e['content'] for e in replies if e.get('purpose') != 'guidance'
                          and e['reply'].strip() not in ('', record.NO_ANSWER))
    for pattern, question in (
            (r'처방.*(?:량|용량|몇|mg)', '처방받은 1회 복용량은 얼마인가요?'),
            (r'(?:복용|드신|먹은).*(?:기간|언제부터|얼마나\s*오래|시작)|(?:언제부터|얼마나\s*오래).*(?:복용|드셨)', '약 복용을 시작한 지 얼마나 됐나요?'),
            (r'감량|거르|줄이|끊', '약을 줄이거나 거르면 어떤 증상이 생기나요?')):
        if not re.search(pattern, questions):
            return question
    return ''


def next_step(calls, session, transcript, turns_left, budget, events=()):
    """다음 행동 하나. 시간 소진은 None, 형식 재시도 소진은 ActionFormatError."""
    request = situation(session, transcript) + f'\n\n남은 턴: {turns_left}'
    if turns_left <= 2 and not EXPLAIN_TURNS:
        request += '\n이제 환자에게 가능성 높은 진단과 다음 계획을 쉬운 말로 설명하세요(SAY).'
    elif turns_left <= 10:
        request += ('\n마무리 점검: 현병력·관련 배경 정보·필요한 진찰 중 판단·계획을 바꿀 미확인 항목을 우선하세요. '
                    '이미 얻은 정보는 반복하지 말고, 중요한 누락이 없으면 DONE으로 마무리하세요.')
    request += '\n다음 행동:'
    original_request, repair = request, None
    for _ in range(3):
        if budget() <= 0:
            break
        step = parse_json(ask(calls, STEP_PROMPT, request, 2048, budget)) or {}
        action = str(step.get('action', '')).upper()
        raw = step.get('content')
        if repair is None and action == 'DONE':
            checked = sedative_termination_check(step, events)
            if checked is not None:
                return checked
        if repair is None and action == 'DONE' and sedative_handoff_coverage(events) is not None:
            replies = [e for e in record.answered(events, 'SAY') if not e.get('refused')]
            if immediate_handoff(step, replies) or handoff_stopped(replies):
                step['history_gap'] = ''
        if repair is None and action == 'DONE' and not action_text(step.get('history_gap'), MAX_CHARS):
            step['history_gap'] = handoff_gap(step, events)
        if repair is None and action == 'DONE' and action_text(step.get('history_gap'), MAX_CHARS):
            action, raw = 'SAY', step['history_gap']
            step['purpose'] = 'question'
            key = lambda value: re.sub(r'[\s.?!。？]', '', value)
            if any(e.get('action') == 'SAY' and not e.get('refused')
                   and e.get('reply', '').strip() not in ('', record.NO_ANSWER)
                   and key(e['content']) == key(raw) for e in events):
                request = (original_request + '\n그 누락 질문은 이미 답변을 받았습니다: ' + raw
                           + '\n모름·답변 거절도 같은 문장으로 반복하지 마세요. 다른 중요한 질문을 선택하거나 DONE으로 마무리하세요.')
                continue
        content = action_text(raw, MAX_CHARS)
        purpose = ('guidance' if step.get('purpose') == 'guidance' else 'question') if action == 'SAY' else ''
        replies = record.answered(events, 'SAY')
        new_urgent = bool(replies and step.get('urgent_reply') == replies[-1]['reply']
                          and not record.acknowledgment(replies[-1]['reply']))
        if action == 'SAY' and purpose == 'guidance' and not new_urgent and explain.repeated_guidance(content,
                [e['content'] for e in record.answered(events, 'SAY') if e.get('purpose') == 'guidance']):
            request = (original_request + '\n그 응급 안내는 이미 전달했습니다. '
                       '새 상태 악화가 있으면 확인하고 대응하되, 같은 안내를 반복하기보다 필요한 미확인 인계 병력이나 다른 조치를 선택하세요.')
            continue
        if action == 'EXAM' and compound_exam(content):
            request = (original_request + '\n직전 진찰은 서로 다른 대상·관찰을 묶었습니다: ' + content
                       + '\n한 부위의 구체적인 진찰 동작 하나로 다시 쓰세요. 같은 방법으로 얻는 여러 소견은 허용됩니다.')
            continue
        if action == 'EXAM' and content and repeated_exam(step, content, events):
            request = (original_request + '\n같은 진찰 요청은 이미 시도했습니다: ' + content
                       + '\n재전송하지 말고 다른 구체적 방법이나 중요한 미확인 정보를 선택하세요. '
                       '성공한 진찰의 재평가가 필요하면 새 환자 진술의 repeat_turn·repeat_quote와 repeat_reason을 JSON에 적으세요. '
                       '거절된 진찰은 같은 문장으로 다시 보내지 마세요.')
            continue
        if (repair is None or action == repair['action']) and (
                action == 'DONE' or (action in ('SAY', 'EXAM') and 0 < len(content) <= MAX_CHARS)):
            return action, content, repair['purpose'] if repair else purpose
        if repair is None and action in ('SAY', 'EXAM') and len(content) > MAX_CHARS:
            repair = {'action': action, 'content': content, 'purpose': purpose}
        if repair is not None:
            request = (original_request + '\n다음 원래 요청을 짧게 고쳐 쓰세요: '
                       + json.dumps(repair, ensure_ascii=False)
                       + '\n행동 종류와 확인하려는 정보를 유지하고 부가 표현을 덜어 25자 이내로 줄이세요. '
                       '다른 질문·진찰이나 DONE으로 바꾸지 마세요. JSON 객체 하나만 출력하세요.')
        else:
            request += f'\n(직전 출력은 형식에 맞지 않았습니다. {MAX_CHARS}자 이내의 JSON 하나만 출력하세요.)'
    if budget() <= 0:
        return None
    raise ActionFormatError(json.dumps(repair, ensure_ascii=False) if repair else '빈 답 또는 잘못된 JSON')


def interview(calls, session, events, clock):
    """문진과 진찰. 턴마다 한 일을 events에 바로 쌓아서, 도중에 멈춰도 그때까지 들은 것으로 기록을 씁니다."""
    transcript, turn = [], 0  # transcript는 다음 행동을 고를 때 모델에게 보여 주는 글입니다
    turns_left, refused = min(MAX_TURNS - EXPLAIN_TURNS, session['turns_remaining']), 0
    format_failures = 0

    def budget():  # 설명은 문장마다 Doctor 한 번. 기록 세 번과 함께 시간을 남깁니다.
        return clock.left() - clamp(RESERVE + calls.need(chats=EXPLAIN_TURNS + 3, says=EXPLAIN_TURNS), *INTERVIEW_MARGIN)

    while turns_left > 0 and budget() > 0 and refused < 3:
        try:
            step = next_step(calls, session, transcript, turns_left, budget, events=events)
        except ActionFormatError as exc:
            format_failures += 1
            transcript.append(f'(형식 오류로 미전송, 환자 답변 없음: {exc}. '
                              '이 내용은 시행한 행동이 아닙니다. 필요한 목적을 짧게 표현하거나 다른 중요한 미확인 정보를 확인하세요.)')
            if format_failures >= FORMAT_FAILURE_LIMIT:
                sys.stderr.write('[interview] 반복 형식 실패 한도 도달; 확보한 내용으로 설명·기록 진행\n')
                break
            continue
        except NovaError:
            break  # 호출 한도, 문진에 쓸 시간이 다 됨, 다시 보내도 소용없는 오류: 지금까지의 기록으로 마무리합니다.
        if step is None or step[0] == 'DONE' or budget() <= 0:
            break
        action, content, purpose = step
        requested_content = content
        try:
            if action == 'SAY':
                reply = with_retry(calls.say, content, budget=budget, request_id=new_id())
                if isinstance(reply.get('effective_content'), str):
                    content = reply['effective_content']
                answer = str(reply.get('content') or '')
                label = '의사' if isinstance(reply.get('effective_content'), str) else '의사 요청(실제 전달문 미확인)'
                transcript += [f'{label}: {content}', f"환자: {answer}"]
            else:
                reply = with_retry(calls.exam, content, budget=budget, request_id=new_id())
                answer = str(reply.get('result') or '')
                transcript.append(f"진찰({content}): {record.exam_feedback(content, answer)}")
        except NovaError as exc:
            if exc.code in STOP_CODES or failure(exc) or exc.code not in TURN_REJECT_CODES:
                break
            # EXAM_NOT_SPECIFIC(진찰을 뭉뚱그려 요청), EXAM_NOT_RECOGNIZED(신체진찰이 아닌 요청).
            # 잘못 보낸 요청이라 답은 없지만 1턴을 씁니다.
            refused += 1
            turns_left -= 1
            turn += 1
            remaining = events[-1].get('turns_remaining', session['turns_remaining']) if events else session['turns_remaining']
            events.append({'turn': turn, 'action': action, 'content': content, 'refused': exc.code,
                           'turns_remaining': max(0, remaining - 1)})
            hint = '신체진찰이 아닙니다. 진찰 동작 하나를 요청하세요' if exc.code == 'EXAM_NOT_RECOGNIZED' else '하나만 구체적으로 다시 요청하세요'
            transcript.append(f'(거절된 요청: {action} {content}; {exc.code}, 1턴 사용. {hint})')
            continue
        refused = 0
        turn = reply.get('turn', turn + 1)
        remaining = events[-1].get('turns_remaining', session['turns_remaining']) if events else session['turns_remaining']
        events.append({'turn': turn, 'action': action, 'content': content, 'reply': answer,
                       'requested_content': requested_content, 'purpose': purpose,
                       'turns_remaining': reply.get('turns_remaining', max(0, remaining - 1)),
                       'delivery_verified': isinstance(reply.get('effective_content'), str) if action == 'SAY' else None})
        turns_left = min(turns_left - 1, reply.get('turns_remaining', turns_left))


def explain_to_patient(calls, session, events, clock, plan=None, topics=None, revised=False):
    """진단과 계획을 환자에게 설명합니다. 문장마다 SAY 한 번이고, 기록을 쓸 시간이 모자라면 멈춥니다."""
    def saying():  # 설명 한 문장을 더 말할 수 있는 초. 그 문장을 말한 뒤에도 기록을 쓸 시간이 남아야 합니다
        return clock.left() - calls.record_need() - calls.need(says=1)

    def asking():  # 다음 문장 생성·전달 뒤에도 기록을 쓸 시간을 남깁니다.
        return clock.left() - calls.record_need() - calls.need(chats=1, says=1)

    if (not EXPLAIN_TURNS or saying() <= 0
            or (events and events[-1].get('turns_remaining', 1) <= 0)
            or (not events and session.get('turns_remaining', 1) <= 0)):
        return
    turn = events[-1]['turn'] if events else 0
    topics, missed = (explain.TOPICS[:EXPLAIN_TURNS] if topics is None else topics), []
    for index, topic in enumerate(topics):
        if turn >= MAX_TURNS or saying() <= 0:
            break
        talk = record.dialogue(record.start_information(session), events)
        if plan:
            talk += '\n\n설명과 최종 기록에 사용할 공통 판단·계획: ' + json.dumps(plan, ensure_ascii=False)
        if missed:
            talk += '\n\n앞서 생성 실패로 전달하지 못했던 안내 목적: ' + '; '.join(missed)
        replies = record.answered(events, 'SAY')
        diagnosis_topic = topic == explain.TOPICS[0]
        content = (explain.usable(f"{plan['diagnosis']} 가능성이 있어요")
                   if diagnosis_topic and plan and plan.get('diagnosis', '').strip() else None)
        content = content or explain.next_sentence(lambda system, user, max_tokens: ask(calls, system, user, max_tokens, asking),
                                        talk, topic, lambda: asking() > 0,
                                        patient_reply=replies[-1]['reply'] if replies else '',
                                        previous=[e['content'] for e in replies if e.get('explain') or e.get('purpose') == 'guidance'],
                                        plan=plan, concern=explain.patient_concern(record.start_information(session), events))
        if content is None and (topic == explain.TOPICS[3] or (asking() > 0 and index + 1 < len(topics))):
            missed.append(topic)
            sys.stderr.write(f'[explain] 안내 미전달(생성 실패): {topic}; 남은 안내 계속\n')
            continue
        fallback = content is None
        content = content or explain.FALLBACK
        if saying() <= 0:  # 모델 호출 중 시간이 소진될 수 있습니다.
            break
        requested_content = content
        try:
            reply = with_retry(calls.say, content, budget=saying, request_id=new_id())
        except NovaError as exc:
            if exc.code in STOP_CODES or failure(exc) or exc.code not in TURN_REJECT_CODES:
                break
            turn += 1
            remaining = events[-1].get('turns_remaining', 1) if events else session.get('turns_remaining', 1)
            events.append({'turn': turn, 'action': 'SAY', 'content': content, 'refused': exc.code, 'explain': True,
                           'turns_remaining': max(0, remaining - 1)})
            if fallback:
                break
            continue
        turn = reply.get('turn', turn + 1)
        if isinstance(reply.get('effective_content'), str):
            content = reply['effective_content']
        remaining = events[-1].get('turns_remaining', session.get('turns_remaining', 1)) if events else session.get('turns_remaining', 1)
        remaining = reply.get('turns_remaining', max(0, remaining - 1))
        events.append({'turn': turn, 'action': 'SAY', 'content': content, 'reply': str(reply.get('content') or ''),
                       'requested_content': requested_content, 'explain': True,
                       'turns_remaining': remaining,
                       'delivery_verified': isinstance(reply.get('effective_content'), str),
                       'plan_revision_delivered': revised and content == requested_content
                           and isinstance(reply.get('effective_content'), str) and not fallback
                           and bool(plan and plan['diagnosis'] in content)})
        if fallback or remaining <= 0:
            break


def submit(calls, clock, session, events, note, diagnosis):
    """기록을 냅니다. 못 내면 그 증례가 0점이라 진료 시간이 끝날 때까지 같은 요청 번호로 다시 보냅니다(두 번 내는 일이 없게).
    보낸 기록이 거절됐거나(크기, 형식) 마감이 LAST_RESORT초 남도록 접수가 안 되면 최소 기록도 번갈아 보냅니다.
    최소 기록은 무슨 답이 와도 시간이 끝날 때까지 보냅니다. 접수되면 True를 돌려주고, 예외를 밖으로 내지 않습니다."""
    try:
        full = {'note': note, 'diagnosis': diagnosis, 'request_id': new_id()}  # 거절되면 None으로 바꾸고 더 보내지 않습니다
        small, strikes, tried = None, 0, 0  # small: 최소 기록. 이것은 거절돼도 버리지 않습니다(더 보낼 것이 없습니다)
        while clock.left() > 0:
            if small is None and (full is None or clock.left() <= LAST_RESORT):
                minimal_note, undecided = record.minimal(session, events)
                small = {'note': minimal_note, 'diagnosis': undecided, 'request_id': new_id()}
            dropped = False
            for entry in (full, small):
                if entry is None:
                    continue
                try:
                    attempt(calls.diagnose, **entry)
                    return True
                except ValueError:  # SDK가 보내기 전에 막음: 칸이나 주진단이 형식에 안 맞습니다
                    code, firm = 'ValueError', True
                except NovaError as exc:
                    if exc.code in CLOSED_CODES:  # 응답만 못 받고 이미 접수된 경우도 여기로 옵니다
                        return False
                    if not 400 <= (exc.status or 0) < 500 or exc.status in NOT_REJECTED:
                        continue  # 잠시 안 되는 것: 다음 바퀴에 같은 번호로 다시 보냅니다
                    code, firm = exc.code, exc.status in REJECTED_STATUS
                if entry is full:
                    strikes += 1
                    if firm or strikes >= FULL_STRIKES:
                        sys.stderr.write(f'[consult] 제출 거절: {code}\n')
                        full, dropped = None, True
            if dropped and small is None:
                continue  # 온전한 기록이 거절됐으면 기다리지 않고 바로 최소 기록을 보냅니다
            time.sleep(max(0, min(SUBMIT_WAITS[min(tried, len(SUBMIT_WAITS) - 1)], clock.left())))
            tried += 1
    except Exception as exc:
        sys.stderr.write(f'[consult] 제출 중단: {type(exc).__name__}\n')
    return False


def consult(client, model):
    """어느 단계가 죽어도 기록은 냅니다. 미제출은 그 증례가 0점입니다."""
    clock = Clock()
    calls = Calls(client, clock)
    session, events = None, []

    def budget():  # 기록을 쓰는 데(세션을 받는 데) 더 쓸 수 있는 초
        return clock.left() - record.MIN_SECONDS

    try:
        session = with_retry(calls.session, budget=budget)  # 여기서 멈추면 Doctor 모델을 한 번도 못 부르고 끝납니다
        clock.sync(session)
        try:
            interview(calls, session, events, clock)
        except Exception as exc:
            sys.stderr.write(f'[consult] 문진 중단: {type(exc).__name__}\n')
        generate = lambda system, user, max_tokens: ask(calls, system, user, max_tokens, budget)
        plan, plan_turn = None, events[-1]['turn'] if events else 0
        try:
            plan = record.prepare_plan(generate, session, events, clock.left)
        except Exception as exc:
            sys.stderr.write(f'[consult] 설명 전 계획 생성 실패: {type(exc).__name__}\n')
        try:
            explain_to_patient(calls, session, events, clock, plan=plan, topics=explain.TOPICS[:EXPLAIN_TURNS - 1])
        except Exception as exc:
            sys.stderr.write(f'[consult] 설명 중단: {type(exc).__name__}\n')
        def finish_explanation(final_plan, changed):
            topic = explain.TOPICS[0] if not plan or (changed and final_plan['diagnosis'] != plan['diagnosis']) else explain.TOPICS[-1]
            explain_to_patient(calls, session, events, clock, plan=final_plan, topics=(topic,), revised=changed)

        note, diagnosis = record.build(generate, session, events, clock.left,
                                       prepared_plan=plan, plan_turn=plan_turn, on_plan_ready=finish_explanation)
    except Exception as exc:
        sys.stderr.write(f'[consult] 기록 작성 실패: {type(exc).__name__}\n')
        note, diagnosis = record.minimal(session, events)
    return submit(calls, clock, session, events, note, diagnosis)


if __name__ == '__main__':
    run_agent(prepare, consult)
