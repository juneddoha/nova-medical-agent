# 회귀 입력의 출처와 범위

이 폴더의 `live_e27_records.json`, `live_e33_records.json`, `live_e35_records.json`, `live_e39_records.json`, `live_e46_c01.json`, `live_e47_c01.json`, `observed_say_delivery.json`은 2026-10-18에 실행한 **공개 가상 환자 샘플**에서 관찰한 동작을 로컬 회귀 검사로 재현하는 입력입니다. 실환자 의무기록이나 비공개 평가 증례가 아닙니다.

숫자·단위·표식·질문과 답변·전달문 불일치를 재현하는 데 필요한 입력만 저장합니다. 게시용 사본의 실제 요청·이벤트 UUID는 동일 관계를 유지하는 가상 UUID로 교체했습니다. API 키·실제 세션 토큰·개인 PC 경로·연락처는 포함하지 않습니다.

활성 `run.py`·`doctor.py`·`explain.py`·`record.py`는 이 폴더를 읽지 않습니다. 공개 기대 진단이나 다른 진료의 정보를 활성 Doctor 입력에 주입하는 자료로 사용하지 않습니다. 별도 라이선스가 확인되지 않은 대회 자료의 제3자 재배포 권한을 주장하지 않습니다.
