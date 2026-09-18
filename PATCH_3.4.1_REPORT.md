# [3.4.1 Critical Fixes]

상태: COMPLETED — 구현 및 회귀 검증 완료, 미배포.

## 변경 파일

| 구분 | 파일 | 내용 |
| --- | --- | --- |
| Fix 1/2 | usage_widget.py | 신규 CLI 수신과 표시 분리, freshness/관측 시각 기반 source 선택 |
| Fix 3 | claude_integration.py | 손상·읽기 실패 settings fail-closed, metadata 선저장 |
| Fix 4 | providers.py | 명시적 percent 및 스케일 검증, semantic kind 분류 |
| Fix 5 | publish_update.ps1, release_policy.ps1 | 빌드 전 원격 버전 검증, create-only 배포 |
| 테스트 | test_claude.py, test_claude_stability.py, test_release_policy.py | 기존 fixture 명시화 및 신규 회귀 39개 |
| 버전 | updater.py | APP_VERSION 3.4.1 |
| 문서 | README.md, README.txt, MANUAL.txt, 사용법.txt, CHANGELOG.md | source 정책·persisted whitelist·미배포 패치 기록 |
| 보고 | PATCH_3.4.1_REPORT.md | 구현 및 검증 결과 |

## Fix 1 — CLI freshness

- 원인: 공통 accept 경로가 기존 CLI snapshot을 표시할 때마다 claude_cli_at을 갱신했습니다.
- 수정: worker 완료 경로만 is_new=True를 전달합니다. 성공한 새 CLI 응답에만 시각을 기록하며, 일반 accept/render/fallback 재사용은 갱신하지 않습니다.
- regression: 2초 간격으로 1,200초까지 재표시, t+300/600/1200 stale 확인, 실패 시 시각 불변, 실제 tick/worker 완료 경로 확인.

## Fix 2 — stale statusLine fallback

- 원인: ok=True만으로 CLI fallback을 억제하여 stale cache도 fresh처럼 취급했습니다.
- 수정: statusLine이 ok이고 stale이 아닐 때만 CLI 조회를 억제합니다. stale/missing이면 기존 60초 due에 따라 worker 조회를 허용합니다.
- source selection: fresh 우선 → quota_observed_at/fetched_at 최신 순 → 동률이면 statusLine 우선. 마지막으로 읽은 소스라는 이유로 선택하지 않습니다.
- CLI 실패 시 cached CLI는 stale 처리합니다. fresh statusLine은 유지하며, statusLine도 없으면 더 최신 historical 관측값을 stale로 보존합니다.
- regression: stale/missing fallback 시작, fresh 조회 억제, 간격 내 중복 억제, 양방향 관측 시각 비교, 실패 후 stale 보존.

## Fix 3 — settings fail-closed

- parse failure: JSON 손상, 비객체 JSON, 인코딩 오류, 읽기 권한 오류는 안전한 안내와 함께 중단합니다. FileNotFoundError일 때만 새 settings 생성을 허용합니다.
- original preservation: 손상된 원본 바이트 불변 및 기존 theme/permissions/hooks/MCP 등 비대상 필드 보존을 검증했습니다.
- backup ordering: 기존 statusLine 복구 metadata를 atomic하게 먼저 저장한 뒤 settings를 atomic replacement로 변경합니다. metadata 저장 실패 시 settings 쓰기를 시작하지 않습니다.
- settings 쓰기 실패 시 원본 바이트를 유지하고 복구 metadata는 남기되 installed=False로 표시합니다.
- 정상 설정/파일 없음/손상·읽기 실패/backup 실패/실제 os.replace 실패를 임시 디렉터리에서 검증했습니다.

## Fix 4 — CLI parser

- percent/utilization: 0..100 명시적 percent를 사용합니다. utilization은 명시적 utilization_scale(percent/fraction) 또는 percent와의 교차 검증이 가능한 경우만 사용합니다.
- kind classification: limits[].kind 및 row의 semantic kind가 raw key보다 우선하며 label로 분류하지 않습니다. five_hour/seven_day만 현재 표시 대상으로 사용합니다.
- ambiguity handling: utilization만 있고 스케일을 확인할 수 없으면 unavailable입니다. percent/utilization 불일치, 범위 밖·비유한·비정상 수치, 모르는 scale도 unavailable입니다. unknown kind는 강제 매핑하지 않고 건너뜁니다.
- regression: int/float/0/1/100, fraction-like 값, 일치·불일치, 명시적 두 스케일, 알려진 두 kind 및 unknown/malformed kind, 중복 충돌, 매우 큰 수치.
- 검증 한계: fixture는 synthetic이며 실계정 CLI 응답을 새로 수집하지 않았습니다. 명시적 percent/scale이 없는 CLI 버전은 보수적으로 unavailable이 될 수 있습니다. 이는 추측 표시를 금지한 안전 정책의 결과입니다.

## Fix 5 — publish protection

- equal version: remote latest.json 또는 GitHub release 목록에 동일·상위 버전이 있으면 거부합니다. 조회 실패/잘못된 원격 version도 fail-closed입니다.
- higher version: 높은 patch/minor/major 허용을 PowerShell에서 검증했습니다.
- 검사 시점: launcher 빌드 및 ZIP/latest 파일 생성 전입니다.
- overwrite: release upload --clobber/edit 경로를 제거했습니다. release create만 사용하므로 검사 이후 같은 tag가 생성되어도 기존 release를 덮어쓰지 않습니다.
- 검증은 원격 응답을 mock한 실제 PowerShell 함수 실행입니다. 실제 publish는 실행하지 않았습니다.

## Tests

- total: 244
- PASS: 244
- failures: 0
- errors: 0
- skipped: 0
- 기존 regression: 205개, 신규: 39개.
- 최종 실행: unittest discover -p test_*.py, Python -B, 임시 Claude settings/cache, Claude 실행 파일 탐색·버전 조회 mock 적용. 실행 시간 75.220초.
- 최초 sandbox 실행은 기존 UI 초기화의 Cursor 파일 존재 확인 권한 오류와 후속 Tk 오류가 있었습니다. 실행 권한을 조정한 최종 전체 suite에서는 오류가 없습니다.
- 추가 확인: git diff --check 및 두 PowerShell 파일 syntax parse PASS.

## Regression

- GPT: Hero/Compact, quota 및 인증 mock 기반 기존 테스트 PASS. GPT parser/UI 정책 변경 없음.
- Cursor: quota/billing, activity, UI 기존 테스트 PASS. 사용자-facing 의미 변경 없음.
- Claude: provider/bridge/integration/UI 46개 + 신규 안정화 모듈 26개 PASS.
- Additional: 기존 7개 PASS, 구현 변경 없음.
- Polling: 기존 scheduling/worker 및 Retry-After/FAST 관련 테스트 PASS. GPT/Cursor policy 변경 없음.
- Serialization/cache: 기존 architecture suite의 legacy cache 및 worker serialization 호환성 테스트 PASS. schema 변경 없음.

## Security

- credential access: 새로운 credential 접근 없음. 테스트에서 실제 Claude CLI 사용량 요청 및 원격 배포를 실행하지 않았습니다. 기존 UI 테스트의 로그인 파일 존재 확인은 수행되었습니다.
- settings preservation: 실제 사용자 Claude settings 및 설치 wrapper는 테스트 대상에서 제외하고 임시 경로만 사용했습니다. 손상 및 write failure 시 원본 보존을 확인했습니다.

## Version

- target: 3.4.1
- 공개 3.4.0 artifact, latest.json 및 AI Usage.exe 변경 없음.
- EXE 재빌드, commit/tag/push, GitHub release 생성·수정 없음.
- CHANGELOG의 3.4.1은 Unreleased로 기록했습니다.

## 발견 이슈 및 남은 backlog

- 추가로 발견한 missing statusLine + 실패한 CLI의 historical source 역행은 Fix 2 범위 내에서 수정·검증했습니다.
- synthetic fixture 검증과 실제 배포 artifact/실계정 검증은 구분해야 합니다. 후자는 이번에 수행하지 않았습니다.
- 기존 backlog 유지: wrapper 재설치 idempotency, CLI version memoize/UI blocking, Cursor Secondary 색상, GPT main 수집/stable ID, Hero selector, scoped/main contract, bars[:8], Additional stale, bar_color, active_interval, cache schema 재설계, representative_blocked. 모두 미착수.

배포 권장: YES — 필수 5건의 코드·회귀 검증 기준 충족. 공개 배포는 사용자 승인 후 별도 수행하며, 이번 작업에서는 실행하지 않았습니다.

## 독립 검증 (리뷰어, 실측)

위 구현을 인수 검증하면서 **배포 차단급 결함 1건을 발견해 수정**했습니다.

### Fix 4 회귀: 실제 CLI 응답을 파서가 거부

- 증상: 실제 `get_usage` 응답으로 `fetch_claude_cli()` 실행 시 `ok=False`, `사용량 없음`. 3.4.0에서 고친 Claude 카드가 다시 빈 상태가 됩니다.
- 원인 두 가지.
  1. `limits` 배열은 body 최상위가 아니라 `rate_limits` **안**에 있습니다. `body.get("limits")`는 항상 None이었습니다.
  2. 서버의 `kind` 어휘는 `session` / `weekly_all`이며 `five_hour` / `seven_day`가 아닙니다. 따라서 kind 매칭이 전부 실패했습니다.
  결과적으로 검증 가능한 `percent` 행을 찾지 못하고, `rate_limits.five_hour`는 bare `utilization`만 있어 "추측 금지" 규칙에 따라 unavailable이 됐습니다. 안전 규칙은 옳았고 배선이 틀렸습니다.
- 수정: `limits`를 `rate_limits` 안에서도 찾고, `CLAUDE_CLI_KINDS`로 semantic kind를 window에 매핑합니다(`session`→five_hour, `weekly_all`→seven_day). `weekly_scoped`는 모델·surface 범위이므로 의도적으로 제외합니다. percent 필수·교차 검증·스케일 거부 규칙은 그대로 유지합니다.
- 부수 결함: 첫 수정에서 `kind`가 dict인 malformed 행에 unhashable 예외가 발생했고, 기존 회귀 테스트가 이를 잡아 타입 가드를 복원했습니다.

### 실계정 실측

- 실제 CLI 조회 1회: `ok=True`, 5시간 31% 남음(리셋 18:20), 주간 91% 남음(리셋 9/25 14:00), 소요 5.2초.
- 응답에서 `rate_limits.five_hour.utilization`(69)과 `rate_limits.limits[kind=session].percent`(69)가 일치해 교차 검증이 실제로 동작함을 확인했습니다.
- 실제 응답 구조를 `test_claude_stability.ClaudeCliLiveShapeTests` 4개로 고정했습니다(수치·리셋 시각만 보존, 식별 정보 없음). 이전 보고서가 인정한 fixture 공백을 메웁니다.

### 보고된 4개 시나리오 재현 결과

| 시나리오 | 3.4.0 | 3.4.1 |
| --- | --- | --- |
| 서버 응답 없이 2초 cadence 재표시 | stale 영구 False | 300초 경계에서 stale 전환, 관측 시각 고정 |
| stale statusLine 캐시 존재 | CLI 조회 영구 차단 | 조회 허용, 표시값은 stale 유지 |
| 손상된 settings.json에 설치 | 설정 전체 소실 | 설치 거부, 원본 바이트 보존 |
| 동일 버전 재배포 | `--clobber` 허용 | 3.4.0/3.3.0 모두 거부, 3.4.1만 허용 |

### 최종 상태

- 전체 suite 248 PASS / 0 실패 / 0 오류 / 0 skip (신규 live-shape 4개 포함).
- 배포 가드는 실제 원격(latest.json + GitHub releases)에 대해 음성·양성 검사 모두 확인했습니다.
- 남은 backlog는 위 목록 그대로이며 미착수입니다. `accept(is_new=True)` 경로가 `fetch_claude()`를 한 번 더 호출하므로 60초 주기에 `claude --version` 비용이 한 번 더 듭니다 — 기존 "CLI version memoize/UI blocking" backlog와 같은 항목입니다.
