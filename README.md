# N.O.V.A. Medical Agent — local baseline

N.O.V.A. 2026 대화형 의료 진단 AI 에이전트 대회용 **로컬 실행 뼈대**입니다. 현재 포함된 두 증례와 판단 규칙은 코드 연결을 검증하기 위한 장난감 예제입니다. 실제 의료 진단 성능이나 공식 대회 점수를 뜻하지 않습니다.

## 실행

Python 3.8 이상에서 프로젝트 루트로 이동해 실행합니다. 외부 패키지 설치나 API 키는 필요하지 않습니다.

```powershell
python run.py
python run.py --case toy-abdominal-pain
python -m unittest discover -s tests -v
```

각 실행은 `runs/`에 새로운 JSONL trajectory와 JSON 평가 요약을 생성합니다. 출력 경로는 `--output-dir`로 바꿀 수 있습니다. `runs/`는 Git 추적에서 제외됩니다.

## 파일별 역할과 실행 흐름

| 파일 | 역할 |
|---|---|
| `run.py` | 증례를 읽고 새 Agent와 상태를 증례마다 만들며, 결과를 기록·평가하는 시작점 |
| `src/actions.py` | `ASK`, `EXAM`, `TEST`, `DIAGNOSE` 타입, 내용 검증, 동일 행동 판별 키 |
| `src/state.py` | 환자 정보, 출처가 있는 관찰, 감별진단, 수행한 행동을 보관하는 `PatientState` |
| `src/llm.py` | `LLMAdapter.next_action(state)` 연결 규격과 고정된 로컬 `MockLLMAdapter` |
| `src/agent.py` | 최대 60턴 반복, 중복·형식 검사, 관찰 반영, 최종 진단 제출 |
| `src/trajectory.py` | 턴마다 행동·관찰·감별진단·오류를 JSONL로 저장 |
| `simulator/environment.py` | 행동에 해당하는 사전 작성 응답만 반환하는 로컬 환자 환경 |
| `simulator/evaluator.py` | 정답 여부, 턴·행동 수, 중복·형식 오류·실행 실패·시간 계산 |
| `simulator/sample_cases.json` | 코드 검증용 합성 증례 두 개 |
| `assets/source_manifest.json` | 포함된 자료의 출처·라이선스 기록 |

`run.py` → 증례의 초기 정보로 `PatientState` 생성 → adapter가 다음 `Action` 선택 → Agent가 검사 → 환경이 해당 관찰만 반환 → 상태와 trajectory 갱신 → `DIAGNOSE` 후 평가 순서로 실행됩니다. Agent에는 증례 정답이나 아직 요청하지 않은 응답이 전달되지 않습니다. 증례마다 새 환경과 상태를 만들어 정보가 섞이지 않습니다.

`Observation.status`는 `positive`, `negative`, `unknown`, `contradictory` 중 하나입니다. 정보가 정의되지 않은 행동은 `unknown`으로 처리하므로, 묻지 않은 사실을 음성 소견으로 간주하지 않습니다. 중복 행동은 공백·대소문자를 정규화한 **동일 타입·동일 문구**일 때 탐지합니다. 의미가 같지만 표현이 다른 질문은 아직 구분하지 못합니다.

## 현재 제한

- `MockLLMAdapter`는 증상 이름별 고정 순서를 반환합니다. 실제 LLM 호출이 없으므로 대회 제출 요건인 증례별 고정 LLM 호출을 충족하지 않습니다.
- 참가자 가이드의 API 형식이 공개되면 `src/llm.py`에 대회용 adapter를 추가하고 `run.py`의 로컬 입출력을 제출 인터페이스에 맞춰 연결해야 합니다. 현재 구조를 공식 API 형식으로 가정하지 마세요.
- 정확도와 행동 횟수는 내부 대리 지표입니다. 공식 세부 평가식, 임상 안전성 채점, 시간 제한은 아직 이 코드에 반영되지 않았습니다. `safety_assessed: false`는 안전성 평가가 없다는 뜻입니다.
- 합성 증례 두 개의 정답률은 파이프라인 동작 확인용입니다. 의학적 타당성이나 대회 성능을 보여주지 않습니다.
- 외부 네트워크 호출, RAG, 파인튜닝 자료, 실제 환자 정보는 포함하지 않습니다.

공식 규정과 가이드: [진행 및 평가](https://nova.snubhai.org/evaluation/), [참가 규칙](https://nova.snubhai.org/rules/), [FAQ](https://nova.snubhai.org/faq/).
