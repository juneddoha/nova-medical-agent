"""Doctor 모델 호출과 다시 보내기. 일시 오류는 잠시 뒤 다시 보내고, 답에서 JSON 객체를 꺼낸다."""
import http.client
import json
import re
import time
import unicodedata
import uuid

from nova_sdk import NovaError

# Doctor 모델 추론 강도: 'low'(싸고 빠름, 빈 답 많음)·'medium'·'high'(비싸고 느림, max_tokens 넉넉히).
# 같은 요청 실험에서 medium을 명시하면 지정하지 않을 때보다 빈 답이 적었습니다.
REASONING_EFFORT = 'medium'
# 서버가 답을 버린 요청(요금·턴 없음). 같은 요청 번호로 다시 보내면 같은 오류가 돌아오니 새 번호로 보냅니다.
# PROVIDER_RESPONSE_INVALID: Doctor 모델의 빈 답. PATIENT_/EXAMINER_…: 가상 환자·진찰 판정 모델이 형식에 맞는 답을 못 냄.
NEW_ID_CODES = {'PROVIDER_RESPONSE_INVALID', 'PATIENT_RESPONSE_INVALID', 'EXAMINER_RESPONSE_INVALID'}
# 서버·모델·연결이 잠시 안 되거나 앞 요청이 아직 처리 중인 것. 진료 시간이 남아 있는 동안 같은 번호로 다시 보냅니다.
# (대회 문서: "잠시 뒤 같은 request_id로 다시 보내세요. 재시도하지 않고 멈추면 실행 실패로 기록됩니다.")
DOWN_CODES = {'PROVIDER_UNAVAILABLE', 'API_UNAVAILABLE', 'REQUEST_PENDING', 'LOCAL_GATEWAY_UNAVAILABLE'}
# 서버가 "지금은 안 된다"고 답한 것. Doctor 호출만, 같은 번호로 연달아 SAME_ID_LIMIT번 안 되면 새 번호로 바꿔 봅니다.
# SAY·EXAM·기록 제출은 번호를 바꾸지 않습니다(서버가 이미 처리했을 수 있어, 바꾸면 두 번 처리됩니다).
REFUSED_CODES = {'PROVIDER_UNAVAILABLE', 'LOCAL_GATEWAY_UNAVAILABLE'}
SAME_ID_LIMIT = 3
RETRY_CODES = NEW_ID_CODES | DOWN_CODES
WAITS = (2, 5, 10)                   # 답을 버린 요청: 세 번 더 보냅니다
DOWN_WAITS = (2, 5, 10, 20, 40, 60)  # 안 되는 서버: 점점 길게 기다리고, 그 뒤로는 60초마다 다시 보냅니다
DOWN_TRIES = 8                       # 남은 시간을 모를 때만 쓰는 횟수 한도


def action_text(value, limit=30):
    """단어·숫자는 그대로 두고 상한을 한 글자 넘긴 문장의 끝 마침표만 덜어낸다."""
    text = unicodedata.normalize('NFC', value).strip() if isinstance(value, str) else ''
    if len(text) == limit + 1 and text.endswith('.') and not text.endswith('..'):
        return text[:-1]
    return text


def parse_json(text):
    """모델 출력에서 첫 JSON 객체를 꺼냅니다(코드 블록이나 앞뒤 설명이 붙어도 됩니다)."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{', text or ''):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def new_id():
    return str(uuid.uuid4())


def failure(exc):
    """오류의 갈래. 'invalid': 서버가 답을 버림(새 번호로 다시), 'down': 잠시 안 됨(같은 번호로 다시), None: 다시 보내도 소용없음."""
    if exc.code in NEW_ID_CODES:
        return 'invalid'
    if exc.code in DOWN_CODES or (exc.status or 0) >= 500:  # 502·504처럼 중간 서버가 낸 오류도 잠시 안 되는 것입니다
        return 'down'
    return None


def attempt(call, *args, **kwargs):
    """한 번 부릅니다. SDK가 NovaError가 아닌 예외를 올리면(응답 본문을 읽다 끊김, 중간 서버가 낸 엉뚱한 꼴의 오류 본문)
    응답을 못 받은 것과 같게 다룹니다. ValueError는 우리가 보내려던 내용이 틀렸다는 뜻이라 그대로 올립니다."""
    try:
        return call(*args, **kwargs)
    except (OSError, http.client.HTTPException, AttributeError, TypeError, KeyError, IndexError) as exc:
        raise NovaError('API_UNAVAILABLE') from exc


def with_retry(call, *args, budget=None, renew_refused=False, **kwargs):
    """일시 오류는 잠시 뒤 다시 보냅니다. 실패한 요청은 턴을 쓰지 않습니다.
    request_id를 준 호출은 같은 번호로 다시 보냅니다. 응답을 못 받았어도 서버가 처리했을 수 있어서, 같은 번호여야
    한 번만 처리되고 끝난 답을 그대로 받습니다. 서버가 답을 버렸을 때만 새 번호로 바꿉니다.
    budget()은 이 일에 더 쓸 수 있는 초입니다. 0 이하가 되면 다시 보내지 않고 오류를 올립니다.
    서버가 잠시 안 되는 오류는 횟수가 아니라 budget으로 끊습니다(시간이 남았는데 포기하지 않게)."""
    invalid = down = refused = 0
    while True:
        try:
            return attempt(call, *args, **kwargs)
        except NovaError as exc:
            kind = failure(exc)
            left = float('inf') if budget is None else budget()
            if kind is None or left <= 0 or (kind == 'invalid' and invalid >= len(WAITS)) or (
                    kind == 'down' and budget is None and down >= DOWN_TRIES):
                raise
            if kind == 'invalid':
                wait, invalid, refused = WAITS[invalid], invalid + 1, 0
                if 'request_id' in kwargs:
                    kwargs['request_id'] = new_id()
            else:
                wait, down = DOWN_WAITS[min(down, len(DOWN_WAITS) - 1)], down + 1
                refused = refused + 1 if exc.code in REFUSED_CODES else 0
                if renew_refused and refused >= SAME_ID_LIMIT and 'request_id' in kwargs:
                    kwargs['request_id'], refused = new_id(), 0
            time.sleep(min(wait, left))
            if budget is not None and budget() <= 0:  # 기다리는 사이에 시간이 다 됐으면 더 보내지 않습니다
                raise


def ask(client, system, user, max_tokens, budget=None):
    # Doctor 모델은 추론 토큰도 max_tokens에 포함되므로 여유 있게 둡니다.
    completion = with_retry(client.chat, [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                            budget=budget, renew_refused=True, max_tokens=max_tokens, reasoning_effort=REASONING_EFFORT,
                            request_id=new_id())
    try:
        content = completion['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError):  # 답이 든 칸이 없으면 빈 답과 같게 다룹니다(부른 쪽이 다시 묻습니다)
        return ''
    return content if isinstance(content, str) else ''
