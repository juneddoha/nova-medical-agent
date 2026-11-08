"""한글로 말한 수를 읽는다. "마흔다섯 살" → 45, "넉 달" → 4, "이십 점 오" → 20.5.

환자는 수를 한글로 말하고 기록은 아라비아 숫자로 적는다. 기록의 숫자가 들은 값인지 견줄 때 쓴다.
확실한 것만 읽는다. 한 글자 수(이, 네, 한 등)는 낱말 처음에 있고 바로 뒤에 세는 말이 올 때만 센다.
"두통", "인터넷", "회사 일"을 수로 읽지 않으려는 것이다. 그래서 "반 알", "이삼일" 같은 말은 놓친다.
"""
import re

HANGUL = '가-힣'
SINO = {'영': 0, '공': 0, '일': 1, '이': 2, '삼': 3, '사': 4, '오': 5, '육': 6, '륙': 6, '칠': 7, '팔': 8, '구': 9}
UNIT = {'십': 10, '백': 100, '천': 1000}
DIGIT = '[일이삼사오육륙칠팔구]'
SINO_FORM = re.compile(f'(?:{DIGIT}?천)?(?:{DIGIT}?백)?(?:{DIGIT}?십)?{DIGIT}?')
TENS = {'열': 10, '스물': 20, '스무': 20, '서른': 30, '마흔': 40, '쉰': 50, '예순': 60, '일흔': 70, '여든': 80, '아흔': 90}
ONES = {'하나': 1, '한': 1, '둘': 2, '두': 2, '셋': 3, '세': 3, '석': 3, '넷': 4, '네': 4, '넉': 4,
        '다섯': 5, '여섯': 6, '일곱': 7, '여덟': 8, '아홉': 9}
ROUGH = {'한두': (1, 2), '두세': (2, 3), '두서너': (2, 3, 4), '서너': (3, 4), '네다섯': (4, 5), '너댓': (4, 5),
         '대여섯': (5, 6), '예닐곱': (6, 7), '일고여덟': (7, 8)}
DAYS = {'하루': 1, '이틀': 2, '사흘': 3, '나흘': 4, '닷새': 5, '엿새': 6, '열흘': 10, '보름': 15}
NEEDS_COUNTER = {'한', '두', '세', '석', '네', '넉'}  # 조사나 대답과 글자가 같다


def alternation(words):
    return '|'.join(sorted(words, key=len, reverse=True))


SINO_COUNTER = alternation(['년', '개월', '주일', '주', '일', '시간', '분', '초', '회', '번', '세', '살', '키로그람', '키로그램',
                            '킬로그램', '키로', '킬로', 'kg', '센티미터', '센티', 'cm', '미리', '밀리그램', '밀리', 'mg', '알',
                            '정', '도', '점', '갑', '잔', '병', '명', '배', '차례', '층', '퍼센트', '%', '월', '대', '학년',
                            '미터', '리터', '개'])
# 한 글자 수 뒤에서는 조사와 겹치는 세는 말(도, 점, 대 등)을 빼고 본다. "팔도", "이도 저도"를 거르려는 것이다
ONE_SYLLABLE_COUNTER = alternation(['년', '개월', '주일', '주', '일', '시간', '분', '초', '회', '번', '살', '키로그람', '키로그램',
                                    '킬로그램', '키로', '킬로', 'kg', '센티미터', '센티', 'cm', '밀리그램', 'mg', '알', '갑',
                                    '잔', '병', '명', '차례', '층', '퍼센트', '%', '월', '학년', '미터', '리터'])
NATIVE_COUNTER = alternation(['살', '개월', '개', '번', '알', '잔', '달', '시간', '시', '명', '분', '차례', '갑', '병', '주',
                              '해', '끼', '배', '가지', '군데', '정', '봉지', '캔', '컵', '그릇', '층', '쪽', '장', '대', '포'])
# 세는 말 뒤는 낱말이 끝나거나 조사가 와야 한다. "이 병원", "네 알겠습니다"를 거르려는 것이다
AFTER = f'(?![{HANGUL}])|(?=[은는이가을를에의도만쯤씩요로와과]|부터|까지|정도|동안|마다|밖에|째|였|입니다|예요)'
WORD_START = f'(?<![{HANGUL}])'
NOT_NUMBERS = re.compile('이번|이 ?정도|이 ?분|이 ?때|이 ?일|일도|일정|구분|사정|사회|육회|이 ?주제')
DECIMAL = re.compile(f'{WORD_START}([영공]|[일이삼사오육륙칠팔구십백천]+)\\s?점\\s?([영공일이삼사오육륙칠팔구]+)')
SINO_RANGE = re.compile(f'{WORD_START}([일이삼사오육륙칠팔구십백천]+)\\s?에서\\s?[일이삼사오육륙칠팔구십백천]+\\s?(?:{SINO_COUNTER})')
SINO_RUN = re.compile(f'{WORD_START}([일이삼사오육륙칠팔구십백천]+)')
NATIVE = re.compile(WORD_START + '(?:(?P<rough>' + alternation(ROUGH) + ')|(?P<day>' + alternation(DAYS) + ')'
                    '|(?P<tens>' + alternation(TENS) + r')(?:\s?(?P<tens_ones>' + alternation(ONES) + '))?'
                    '|(?P<ones>' + alternation(ONES) + '))')
DIGITS = re.compile(r'\d+(?:\.\d+)?')


def sino_value(run):
    total = current = 0
    for ch in run:
        if ch in SINO:
            current = SINO[ch]
        else:
            total += (current or 1) * UNIT[ch]
            current = 0
    return total + current


def plain(number):
    text = str(number)
    return text.rstrip('0').rstrip('.') if '.' in text else text


def counted(text, position, counter):
    return re.match(f'\\s?(?:{counter})(?:{AFTER})', text[position:])


def ends_word(text, position):
    return re.match(AFTER, text[position:]) is not None


def find(text):
    """[(수, 시작, 끝)]. 수는 '62', '13.5' 같은 문자열이다. "십일"처럼 두 가지로 읽히는 말은 둘 다 낸다."""
    found = [(plain(m.group()), m.start(), m.end()) for m in DIGITS.finditer(text)]
    text = NOT_NUMBERS.sub(lambda m: ' ' * len(m.group()), text)
    for match in DECIMAL.finditer(text):
        whole, fraction = match.groups()
        if whole in '영공' or SINO_FORM.fullmatch(whole):
            found.append((plain(f"{sino_value(whole)}.{''.join(str(SINO[ch]) for ch in fraction)}"), match.start(), match.end()))
            text = text[:match.start()] + ' ' * (match.end() - match.start()) + text[match.end():]  # 정수 조각으로 다시 읽지 않게
    for match in SINO_RANGE.finditer(text):
        if SINO_FORM.fullmatch(match.group(1)):
            found.append((str(sino_value(match.group(1))), match.start(1), match.end(1)))  # "이에서 삼회"의 앞쪽
    for match in SINO_RUN.finditer(text):
        run, end = match.group(1), match.end(1)
        if len(run) >= 2 and run.endswith('일') and SINO_FORM.fullmatch(run[:-1]) and ends_word(text, end):
            found.append((str(sino_value(run[:-1])), match.start(1), end))  # "십일 전"은 10일일 수도 11일일 수도 있다
        if not SINO_FORM.fullmatch(run):
            continue
        if len(run) == 1:
            sure = counted(text, end, ONE_SYLLABLE_COUNTER)
        else:
            sure = ends_word(text, end) or counted(text, end, SINO_COUNTER)
        if sure:
            found.append((str(sino_value(run)), match.start(1), end))
    for match in NATIVE.finditer(text):
        end = match.end()
        has_counter = counted(text, end, NATIVE_COUNTER)
        if match.group('rough'):
            if has_counter:
                found += [(str(n), match.start(), end) for n in ROUGH[match.group('rough')]]
        elif match.group('day'):
            found.append((str(DAYS[match.group('day')]), match.start(), end))
        elif match.group('tens'):
            ones = match.group('tens_ones')
            if ones and match.group('tens') == '열' and text[match.start('tens_ones') - 1] == ' ':
                continue  # "열 한 번도 안 났어요"는 열한 번이 아니라 발열 이야기다
            if has_counter or (ones and ends_word(text, end)):  # "열", "쉰"만 있으면 세는 말이 있어야 한다
                found.append((str(TENS[match.group('tens')] + ONES.get(ones, 0)), match.start(), end))
        elif has_counter or (match.group('ones') not in NEEDS_COUNTER and ends_word(text, end)):
            found.append((str(ONES[match.group('ones')]), match.start(), end))
    return found


def numbers(text):
    return {value for value, _, _ in find(text)}


def age(opening):
    """시작 정보에서 환자 나이. "마흔다섯 살", "45세"처럼 처음 나오는 나이를 읽고, 못 읽으면 None."""
    for value, _, end in sorted(find(opening), key=lambda item: item[1]):
        if re.match(r'\s?(?:살|세)', opening[end:]):
            return value
    return None
