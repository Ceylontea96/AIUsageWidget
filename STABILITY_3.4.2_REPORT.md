# [3.4.2 Stability Cleanup]

상태: **COMPLETED**

Version: **3.4.2**

구현과 regression test만 수행했습니다. 공개 배포, 기존 release artifact 덮어쓰기,
실행 중 위젯 재시작, 3.5.0 구조 작업은 수행하지 않았습니다.

## 변경 파일

- `claude_integration.py`: 설치 소유권/최초 원본 보존, 복구·충돌 방어, 버전 캐시와 비동기 확인, 소유 파일 cleanup
- `claude_bridge.py`: schema 검증, 입력 숫자 검증, 개인정보 whitelist 정규화, mtime 방어, 자기 포워딩 차단
- `providers.py`: CLI 버전 sanity와 실제 응답 schema 판별, UI의 캐시 조회 경로에서 버전 subprocess 제거
- `usage_widget.py`: Cursor 보조 막대 색상, 대표 수치/Compact 독립성, visible-state repaint signature, 활성 조회 주기 설정 연결
- `updater.py`: 로컬 소스 버전 3.4.2
- `publish_update.ps1`: 필수 회귀 조건인 동일·하위 버전 게시 차단, overwrite 경로 제거, 기존 runtime 모듈 및 정확한 안내 문서 포함
- `test_claude.py`, `test_claude_stability.py`, `test_ui.py`, `test_widget.py`, `test_publish.py`: 회귀 테스트
- `CLAUDE_INTEGRATION.md`, `MANUAL.txt`, `CHANGELOG.md`: 저장 schema·정책·사용자 안내와 버전 기록
- `STABILITY_3.4.2_REPORT.md`: 이번 보고서

이번 작업 이전부터 수정되어 있던 파일은 되돌리지 않았습니다. 작업 시작 당시 소스 비교용
복사본은 git에서 제외된 `dist/stability-342-baseline`에 보관했습니다. 기존 `latest.json`은
작업 시작 당시와 byte 단위로 동일하며 새 배포 zip/피드는 생성하지 않았습니다.

## Claude integration

- repeated install: PASS. 최초 원본 statusLine을 유지하며 중복 backup chain을 만들지 않음.
- wrapper recovery: PASS. wrapper 파일만 없어졌을 때 정상 metadata로 재설치 가능.
- self-forward prevention: PASS. 정확한 wrapper 경로를 검사하고 자식 프로세스 재진입도 차단.
- uninstall restore: PASS. 최초 원본 및 나머지 사용자 설정 보존. settings.json 파일 자체를 삭제하지 않음.
- conflict protection: PASS. 사용자 command/옵션 변경 시 덮어쓰기·자동 원복 차단.
- metadata 누락/손상: 원본을 추정하지 않고 안전하게 중단. 사용자 설정 확인 후 복구가 필요함.

## Claude version / feature

- version cache: 실행 파일 절대 경로, mtime_ns, size 기준. 성공 1시간, 실패 30초. path/metadata 변경, setup, 명시적 refresh 시 재확인.
- subprocess hot path: PASS. 2초 캐시 조회/render에서 버전 subprocess 없음. 상태 표시의 최초 검사는 백그라운드 스레드, CLI 조회는 별도 워커에서 실행.
- feature detection: 버전은 sanity filter로만 사용. 실제 statusLine/CLI 입력의 알려진 quota schema와 유효 숫자가 확인될 때만 사용량 표시. 알 수 없는 고버전 schema는 unavailable.
- 제한: 사용자가 직접 누르는 연동 설치의 최초 버전 검사는 동기식이며 최대 2초. 주기적인 UI 갱신에는 해당하지 않음.

## Claude cache

- schema validation: source/schema_version/session_key 및 호출·관측 시각 검증. 각 창의 사용률·잔여율·리셋 시각 검증.
- corrupt isolation: PASS. 손상 JSON/UTF-8, 잘못된 세션, 잘못된 창을 분리하고 정상 세션은 계속 선택.
- invalid numeric handling: bool/문자열 숫자/NaN/Inf/범위 밖 percent/잘못된 timestamp 거부. 사용률·잔여율 합계 불일치도 해당 창 unavailable.
- unknown field: 입력은 허용하되 판단과 저장에서 제외. 선택 필드 누락은 정상 허용.

## Claude cleanup/privacy

- bridge cleanup: wrapper, metadata, salt, bridge.log, hash session cache 및 정해진 atomic-write 임시 파일만 best-effort 정리. 다른 이름의 사용자 파일과 하위 디렉터리는 유지.
- persisted whitelist: 최상위 10개와 quota 필드 3개를 CLAUDE_INTEGRATION.md에 명시하고 테스트로 대조.
- sensitive persistence: raw session ID, transcript 경로/내용, prompt, project/repo, account/email, token/cookie/Authorization/credential은 세션 캐시와 bridge 로그에 저장하지 않음.
- 구분: integration.json의 최초 statusLine 명령·실행 경로는 원복에 필요한 사용자 설정 백업. 사용량 캐시 whitelist와 별도로 문서화.

## Freshness

- transcript mtime policy: 유지하되 API 완료 증거가 아닌 보조 heuristic으로 명시. transcript 내용은 읽지 않음.
- false-fresh mitigation: 같은 quota이면 최근 statusLine 관측과 활성 세션, 직전 호출 이후의 정상 범위 mtime 증가를 함께 요구. 비활성·과거·비정상 미래 mtime으로 재스탬프하지 않음.
- 3.4.1 freshness regression: PASS. CLI 반복 표시의 관측 시각 유지/5분 경과 stale 처리, stale statusLine의 CLI fallback 허용.
- 한계: 활성 세션의 파일 수정도 API 응답 완료를 완전히 증명하지는 않음. freshness 전체 재설계는 제외 범위.

## Cursor

- Secondary severity: 정상/주의/임박/critical 및 stale 상태에 따라 해당 보조 막대 자체 색상 표시.
- Hero/Compact independence: PASS. Other Models 3%여도 Cursor Models 80%인 대표 숫자와 Compact 상태는 유지.
- 기존 카드 전체 경고 배지는 totalPercentUsed를 사용하는 현재 정책 유지. 그 값이 실제 배지에 영향을 주므로 repaint key에는 파생 표시값을 포함. 이 정책의 전면 정리는 3.5.0 범위.

## UI performance

- repaint signature: 실제 표시 필드, 파생 경고, reset epoch, 표시되는 Additional 행으로 제한. raw/internal diagnostics와 canonical 중복 필드 전체 직렬화 제외.
- unnecessary repaint: PASS. internal-only/관측 시각 변경은 Card/Additional 전체 repaint 없음. 최신 snapshot 참조는 갱신.
- visible changes: 대표/보조 잔여, stale, labels, reset, Additional, 실제 배지 상태 변화는 repaint 유지. activity 효과는 기존 별도 animation 경로 유지.

## Cleanup

- removed dead code: production 미사용 bar_color()와 이를 직접 부르던 테스트, 미사용 FORBIDDEN_SUBSTRINGS 상수 제거. 실제 renderer의 색상 테스트로 대체.
- removed dead settings: 없음.
- retained items + reason: active_interval은 기존 polling policy의 설정으로 유지하고 실제 FAST 요청/예약 두 경로에서 사용하도록 연결. 기본 2초 동작 유지.
- 색상/polling/canonical architecture 재설계 없음.

## Tests

- total: **159** (기존 111개에서 회귀 검사 48개 추가)
- PASS: **159**
- failures: **0**
- Python 3.10: `python -B -m unittest discover` — 159 PASS, 12.002초
- Python 3.14: 실제 위젯의 Python 실행 파일로 `-B -m unittest discover` — 159 PASS, 10.940초
- `git diff --check`: PASS (기존 CRLF 변환 안내만 출력)
- 실제 Claude CLI smoke: ok=True, feature_available=True, 5시간·주간 한도 2개 확인. 프롬프트 전송 없음.
- Python 3.14의 샌드박스 실행에서는 Tk 로드에 실패하여 UI fixture 47개가 시작하지 못함. 사용자 권한 환경으로 재실행해 전체 PASS 확인. 제품 코드 회귀로 분류하지 않음.
- 배포 방어 테스트는 임시 폴더와 가짜 gh/피드로만 수행. 네트워크 게시·실제 빌드 없음.

## Regression

- GPT: PASS — 기존 대표 한도, 주간 경고/소진 및 제한 안내 유지.
- Cursor: PASS — 대표 수치, 보조 색상과 Compact 독립성.
- Claude: PASS — 설치/복구/해제, schema/feature, 버전 memoization, 실조회.
- Activity: PASS — 감지·coalescing·시각 효과 경로 유지.
- Polling: PASS — 정상/오류/소진 지연과 FAST scheduling.
- Additional: PASS — 기존 표시 경로 유지, 실제 값 변경 시 갱신.
- Updater: PASS — 기존 검사와 동일·하위 버전/원격 실패 차단.
- 3.4.1 critical fixes: **5/5 PASS** — CLI freshness 재스탬프 금지, stale statusLine fallback, 손상 settings fail-closed, percent/utilization 안전 처리, same-version publish 차단.

## 발견 이슈

- 작업 시작 소스의 publish_update.ps1에는 CHANGELOG의 설명과 달리 --clobber 경로가 남아 있었음. 필수 회귀 조건을 충족하도록 동일·하위 버전 및 원격 확인 실패를 빌드 전에 차단하고 overwrite 경로 제거.
- 배포 목록에서 기존 Claude/Additional/polling runtime 모듈이 누락되어 있었음. 해당 모듈과 privacy 문서를 포함하고 오래된 다른 txt가 현재 안내 문서로 잘못 선택되지 않게 수정.
- 손상·누락된 원본 backup에서 원본 명령을 자동 복구하는 것은 불가능하므로 안전 중단을 유지. 이는 의도된 복구 정책.

## 3.5.0으로 넘길 backlog

- GPT 가변 main quota, stable quota_id, reset_at canonical 전달
- Hero selector 단일화, display string identity 제거, canonical UI read path와 legacy adapter 정리
- global/scoped 계약, alert 개수 절단 제거, representative blocked 정책 정리
- Additional stale/layout, Billing consumption path 정리
- identity fallback 개선 및 architecture intent regression

이번 버전에서는 위 항목을 구현하지 않았습니다.

Release blocker: **없음 — 구현/회귀 검증 범위 기준**

배포 권장: **YES — 사용자 승인 대기**

종료 조건에 따라 공개 배포는 수행하지 않고 여기서 보고합니다.
