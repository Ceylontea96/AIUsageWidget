# Changelog

이 파일은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식을 따르며, 버전은 [Semantic Versioning](https://semver.org/lang/ko/)을 따릅니다.

## [Unreleased]

## [3.6.0] - 2026-09-23

### Added
- GPT 리셋권 옆에 만료 시각을 표시합니다. 여러 개면 가장 먼저 만료되는 것을 보여 줍니다.

### Fixed
- GPT 크레딧 잔액만 바뀌었을 때 카드가 다시 그려지지 않아 이전 잔액이 남던 문제를 고쳤습니다.

## [3.5.3] - 2026-09-22

### Changed
- 한 줄 칩 색을 카드와 같은 기준으로 맞췄습니다. 50% 미만은 주의, 20% 미만은 임박, 5% 미만은 곧 한도입니다. Windows 알림은 10% 이하에서만 옵니다.
- Claude 카드에 제품 이름 대신 실제 요금제(Pro, Max, Team, Enterprise)를 표시합니다.

## [3.5.2] - 2026-09-20

### Fixed
- Cursor의 다음 리셋이 "20703385일 후"처럼 잘못 표시되던 문제를 수정했습니다. 이제 실제 청구 주기 날짜와 남은 기간을 보여줍니다.
- AI Usage.exe와 바탕화면 바로가기에 앱 아이콘이 표시되지 않던 문제를 수정했습니다.

### Notes
- 이전 버전에서 저장된 Cursor 리셋 값도 실행 즉시 바로잡혀 표시됩니다.

## [3.5.1] - 2026-09-20

### Fixed
- 세 가지 서비스를 한 줄 모드에서 함께 볼 때 마지막 칩이 창 버튼과 겹치던 문제를 수정했습니다.
- 한 줄 모드에서 공간이 모자라면 제목을 먼저 줄이고, 서비스 이름과 남은 비율은 끝까지 그대로 보여줍니다.
- 우클릭 메뉴가 잠시 후 위젯 뒤로 숨던 문제를 수정했습니다. 메뉴가 열려 있는 동안에는 항상 위 설정이 메뉴를 가리지 않습니다.

### Notes
- 표시 동작만 바뀌었고 사용량 계산과 경고 기준은 3.5.0과 같습니다.

## [3.5.0] - 2026-09-20

### Changed
- 여러 사용량 한도를 더 안정적으로 처리하도록 내부 구조를 정리했습니다.
- GPT 다중 quota 처리를 개선해 한도가 두 개를 넘어도 모두 표시·경고 대상이 됩니다.
- 사용량 대표값과 경고 표시가 항상 같은 한도를 기준으로 하도록 일관성을 맞췄습니다.
- Cursor 상태 배지가 전체 합산값 대신 실제 모델 한도를 기준으로 표시됩니다.
- 추가 한도(Additional)의 상태·갱신 시점 표시를 개선했습니다.
- 향후 Provider 한도 확장을 위한 호환성을 높였습니다.

### Fixed
- Claude 카드에서 한도 행이 표시되지 않을 수 있던 문제를 수정했습니다.
- 한도 이름이 바뀌어도 저장된 상태가 유지됩니다.

### Notes
- 기존 설정과 캐시는 그대로 사용할 수 있습니다.
- 3.4.2의 안정화 수정도 함께 포함됩니다.

## [3.4.2] - 2026-09-19

### Fixed
- Claude 연동 복구 및 안정성 개선
- Claude 상태 조회 시 UI 응답성 개선
- 손상된 캐시 처리 안정성 개선
- Cursor Other Models 상태 색상 표시 개선
- 불필요한 UI 갱신 감소

### Notes
- 구현 및 검증 단계이며 공개 배포 전입니다.

### 기존 로컬 수정 포함
- GPT 주간 한도 소진·서버 사용 제한을 경고와 안내에 반영하고, Cursor 경고 배지는 전체 잔여량을 기준으로 판단합니다. 대표 숫자의 한도 선택은 유지합니다.
- 기본 서비스 선택과 화면 잠금 테스트에 Claude를 반영했습니다.
- Microsoft Store Claude의 패키지 경로와 PATH에 없는 표준 CLI 설치 경로를 감지합니다. 여러 버전은 숫자로 비교해 최신 버전을 선택합니다.
- Claude Code 미로그인을 구독 한도 미지원과 구분하고, 조회 오류가 캐시 갱신 때 사라지지 않도록 수정했습니다. 우클릭 메뉴에서 Claude 로그인을 열 수 있습니다.

## [3.4.1] - 2026-09-18

### Fixed
- Claude CLI 기존 값의 재표시가 관측 시각을 갱신하지 않으며, 오래된 statusLine도 CLI fallback 조회를 허용합니다.
- 손상되거나 읽을 수 없는 Claude settings.json은 변경하지 않습니다. 설정 변경 전에 원본 statusLine 복구 metadata를 저장합니다.
- Claude CLI 한도는 semantic kind와 검증 가능한 percent/scale로 해석하며, 모호하거나 불일치하는 수치는 사용 불가로 처리합니다.
- Claude CLI 응답의 실제 구조(rate_limits 안의 kind 행, session/weekly_all)를 읽도록 맞춰 사용량이 다시 표시됩니다.
- 배포 전 원격 latest와 release 버전을 검사하고 동일·하위 버전 재게시 및 기존 artifact 덮어쓰기를 차단합니다.

## [3.4.0] - 2026-09-18

### Fixed
- 데스크톱 앱 세션만 쓰면 Claude 사용량이 비어 있던 문제를 고쳤습니다
- 우클릭 메뉴에 Claude 연동 진입점을 추가해 기존 버전에서 업데이트한 사용자도 찾기 쉽게 했습니다

### Added
- statusLine 값이 없으면 60초마다 Claude Code에 사용량만 직접 물어봅니다. 프롬프트를 보내지 않아 사용량을 소모하지 않습니다
- statusLine 호출 기록을 claude/bridge.log에 남겨 연동 동작 여부를 확인할 수 있습니다 (식별자·경로·대화 내용은 기록하지 않음)

### Changed
- Claude 카드 구성을 statusLine·직접 조회 두 경로가 공유해 대표 한도와 문구가 같게 나옵니다
- 직접 조회는 기존 워커 프로세스에서 실행되어 다른 서비스 조회나 화면을 막지 않습니다
- 사용 설명서에 Claude 조회 경로와 갱신 주기를 반영했습니다

## [3.3.0] - 2026-09-18

### Added
- GPT 추가 한도를 접어서 보고, 그룹 단위로 표시합니다
- Claude Pro 사용량을 대화형 Claude Code와 연동해 표시합니다
- 사용량 상태와 한도 표시 구조를 서비스 공통 형태로 정리합니다

### Changed
- Cursor 대표 잔여를 Cursor Models, 보조 막대를 Other Models로 표시합니다
- 조회 간격과 오류 재시도(Retry-After)를 더 안정적으로 처리합니다

### Notes
- Claude는 Claude Code와 Claude.ai 구독이 필요하며, 대화형 Claude Code 세션 기준입니다
- `claude -p`만으로는 실시간 추적되지 않으며 Claude Additional/Billing은 아직 없습니다
- Cursor Billing 문구의 사용자-facing 의미는 별도 검증 전까지 새로 확정하지 않습니다

## [3.2.27] - 2026-09-18

### Fixed
- Hero quota가 100%인 동안 보조 quota 경고가 Compact 색상과 Hero 상태를 덮어쓰지 않도록 한 3.2.26 hotfix를 새 버전으로 재배포

## [3.2.26] - 2026-09-18

### Fixed
- Compact 숫자·색상과 Hero 배지·상태를 대표 Hero quota 기준으로 통일하고, 보조 quota 경고에는 해당 기간을 명시
- 초기 Cursor transcript 스캔이 종료된 과거 이벤트로 FAST fallback을 시작하지 않도록 수정
- 4시간·6일·8일 한도를 5시간·주간으로 잘못 분류하지 않도록 기간 판별 허용 범위를 축소
- 주간 잔여량의 경고와 소진 배지, Cursor 전체 잔여량 기반 상태 표시를 보완하고 이전 quota 캐시를 무효화
- 긴 JSON 행의 잘린 조각 오인식과 FAST 유지 중 불필요한 후속 조회 예약을 수정
- 재탐색 전에 추적 파일의 새 이벤트를 처리하고 숨김/복귀 시 shimmer 진행 위치를 보존
- 종료 후 남은 Tk 타이머를 취소하고 현재 디자인에 맞춰 회귀 테스트를 갱신

### Changed
- Cursor 활동 감지를 transcript별 `user`/`assistant`/`turn_ended` 상태 머신으로 변경하고, 종료 grace와 10분 watchdog 추가
- Cursor quota 감소의 60초 FAST fallback을 API polling에만 적용해 shimmer·굵기 효과와 분리
- 추적 파일은 0.75초마다 확인하고 전체 프로젝트를 12초마다 metadata-only 재탐색하며 ACTIVE transcript는 퇴출하지 않도록 변경
- 위젯 Unmap 시 shimmer 상태를 보존하고, Compact chip에 고정 Canvas 내부 24→28px ACTIVE 굵기 효과 추가
- GPT quota window를 `primary/secondary` 위치가 아니라 `limit_window_seconds` 기간으로 분류하도록 변경
- 5시간 window가 없으면 주간·기타 기간을 원형 게이지로 표시하고 라벨을 `남은 사용량`으로 명확화
- 원형 게이지·Compact 값·리셋 시간이 같은 대표 window를 사용하도록 통일

### Added
- Cursor 동시 main/subagent, 늦은 이벤트, unknown cooldown, partial JSON, 재탐색, stale timeout 회귀 테스트
- 주간 단독, window 순서 반전, 알 수 없는 기간, 리셋 연결에 대한 GPT quota 회귀 테스트

## [3.2.25] - 2026-09-15

### Added
- Cursor Agent transcript 활동으로 FAST 조회·바 굵기·shimmer를 시작하고, 마지막 활동 12초 후 일반 모드로 복귀

### Changed
- GPT 원형 라벨을 `5시간 창 · 남음`에서 `5시간 한도 · 남음`으로 바꿔 주간 한도와 표현을 맞춤

## [3.2.24] - 2026-09-15

### Changed
- Refined v2 디자인 적용: 380px 창, 원형 잔여량, 상태 배지, 리셋 카운트다운, 서비스별 색상
- GPT 5시간 잔여량은 원형, 주간 한도는 보조 바로 표시; 기존 리셋권·크레딧 유지
- Cursor 보조 바도 잔여량으로 표시하고 보너스 사용액을 별도 강조
- 기존 10px 바·연속 추종 길이 애니메이션 유지
- 사용 중 바 굵기와 shimmer를 고정 6초 사이클이 아니라 Codex/Cursor activity 상태에 연결
- GPT FAST 조회를 요청 시작 시각 기준 2초 간격으로 맞추고, Cursor 즉시 재조회(ACTIVE_POLL=0)를 2초로 제한

## [3.2.23] - 2026-09-15

### Changed
- 공통 Progress Bar 높이를 8px에서 10px로 확대하고 모서리 반경을 5px로 조정
- 상세 바와 축약 칩의 값 변화를 연속 지수 추종으로 변경 (speed 8.0, 16ms, snap 0.01%p)
- 이동 중 새 목표가 들어와도 현재 표시값과 기존 타이머 유지

## [3.2.22] - 2026-09-15

### Added
- GPT 주간 사용량 바 아래 재설정 날짜와 시간 표시
- GPT와 Cursor 서비스 아이콘 및 고해상도 자산 추가
- Codex 로컬 토큰 활동으로 빠른 조회 시작, 마지막 활동 12초 후 일반 조회로 복귀
- 활동 감지 디버그 로그와 오류 시 정기 조회 폴백 추가

### Changed
- Codex 서비스 표시명을 GPT로 변경
- 사용 감지 강조 효과를 확대 0.6초·유지 4.4초·복귀 1초로 연장
- 바 길이 변화를 1~3초로 연장하고 소수점 두께 렌더링으로 확대·축소 계단 현상 완화
- 사용 감지 효과 갱신 간격을 16ms로 단축하고 바 이미지 위치 고정
- 토큰 활동과 플랜 사용량을 분리하고 기존 HTTP quota·credit 표시 유지
- 위젯 글자를 Pretendard로 통일. 번들 파일이 없으면 기존 Segoe UI / 맑은 고딕을 씀

## [3.2.21] - 2026-09-15

### Added
- 사용량 증가가 감지된 막대에 한 번 지나가는 빛 효과 추가

### Changed
- 잔여량이 작게 변하거나 연속으로 갱신되어도 막대 길이가 현재 위치에서 부드럽게 이어지도록 개선

## [3.2.20] - 2026-09-14

### Changed
- 실행 파일과 위젯 창에 AI 원형 게이지 아이콘 적용

## [3.2.19] - 2026-09-14

### Changed
- 잔여량이 줄면 1분 동안 이전 조회가 끝나는 즉시 다시 조회

## [3.2.18] - 2026-09-14

### Changed
- 도움말·로그인·업데이트 확인 같은 별도 창을 위젯 옆이 아니라 화면 중앙에 표시

## [3.2.17] - 2026-09-14

### Added
- Cursor 상세 카드에 청구 주기 초기화 시각을 표시

### Changed
- 잔여량이 변하면 막대와 한 줄 칩이 최대 2초에 걸쳐 따라감
- 우클릭 메뉴의 바탕화면 바로가기를 버전 위로 옮기고 `바탕화면 바로가기 생성`으로 바꿈

## [3.2.16] - 2026-09-13

### Added
- `AI Usage.exe` 실행 파일. 폴더에서 한 번 실행하면 경로를 기억해 실행 파일만 옮겨도 됨
- 첫 실행과 우클릭 메뉴에서 바탕화면 바로가기를 만들 수 있음

### Fixed
- 이미 실행 중이면 작업 관리자 안내 대신 기존 창을 앞으로 가져옴. 멈춘 잠금은 자동으로 해제함

## [3.2.15] - 2026-09-12

### Changed
- 사용량 조회 간격을 정상 30초, 잔여 35% 이하 20초로 줄임. 소진·실패 백오프는 그대로

## [3.2.14] - 2026-09-11

### Fixed
- 한 줄 모드에서 업데이트 확인 창을 열어도 위젯이 작업 표시줄 뒤로 가지 않음
- 우클릭 메뉴가 위젯과 앞뒤가 바뀌며 깜빡이던 문제

## [3.2.13] - 2026-09-11

### Added
- 새로고침·한 줄로 접기·상세로 펼치기·종료 아이콘에 마우스를 올리면 안내가 뜸
- 새 버전이 있으면 업데이트 배지가 천천히 밝아졌다 어두워지고, 올리면 설치 안내가 뜸
- MIT 라이선스와 비공식 안내

### Changed
- 실행 후에도 30분마다 새 버전을 확인함

### Fixed
- 한 줄 모드에서 우클릭해도 위젯이 작업 표시줄 뒤로 가지 않음
- 안내 말풍선이 위젯 뒤에 가려지던 문제

## [3.2.12] - 2026-09-11

### Added
- 업데이트 전에 새 버전과 짧은 변경 요약을 보여 줌

### Fixed
- 한 줄 모드에서 작업 표시줄 위에 두고 우클릭하면 위젯이 표시줄 뒤로 가던 문제

## [3.2.11] - 2026-09-11

### Notes
- 업데이트 배지 확인용. 3.2.10에서 초록 `↑ 업데이트 3.2.11`이 보여야 함

## [3.2.10] - 2026-09-11

### Changed
- 새 버전이 있으면 제목 옆에 초록 `↑ 업데이트 x.x.x` 배지가 나타남. 없을 때는 숨김
- 우클릭 메뉴 체크표시를 흰색으로 바꿔 어두운 배경에서 보이게 함

## [3.2.9] - 2026-09-11

### Fixed
- 폴더 이름에 공백·괄호·한글이 있어도 위젯이 실행됨. `AIUsageWidget (1)` 그대로 써도 됨

## [3.2.8] - 2026-09-11

### Fixed
- 설치 폴더의 pythonw.exe를 PATH 없이 바로 찾아, 3.13이 있는데도 설치 창이 뜨던 문제

## [3.2.7] - 2026-09-11

### Fixed
- `py -3`으로 이미 있는 Python 3.13+Tk를 먼저 찾아, 있는 PC에 3.12를 또 깔지 않음
- 이미 Python이 있을 때 위젯용 3.12를 PATH 맨 앞에 넣지 않음

### Notes
- 3.13이 있는데도 설치 창이 뜨면 이 버전 zip을 다시 받아 diagnose.bat을 실행하세요.

## [3.2.6] - 2026-09-11

### Fixed
- 폴더 이름에 괄호가 있으면 Python 설치 단계가 바로 실패하던 문제

### Notes
- `AIUsageWidget (1)`처럼 받은 폴더는 이름을 `AIUsageWidget`으로 바꾸는 것이 안전합니다.

## [3.2.5] - 2026-09-11

### Fixed
- diagnose.bat이 UTF-8이라 명령이 깨지던 문제. 영문 ASCII로 저장

### Notes
- 안 되면 새 zip의 diagnose.bat을 다시 실행하세요.

## [3.2.4] - 2026-09-11

### Fixed
- 프로세스가 이미 꺼진 뒤 남은 widget.lock이 재실행을 막던 문제
- 실행 직후 launch.log를 남기고, 안 될 때는 문제확인.bat으로 원인을 볼 수 있게 함

### Notes
- 위젯이 안 뜨면 zip을 폴더로 푼 뒤 문제확인.bat을 실행하고 창 내용을 보내세요.

## [3.2.3] - 2026-09-11

### Fixed
- 실행 시 검은 명령 창만 보이던 문제. 위젯은 창 없이 시작하고, Python 설치는 안내 창으로 알림
- 이미 떠 있는 숨은 위젯이 있으면 앞으로 가져오고, 창이 없으면 잠금을 해제한 뒤 다시 시작
- 실행할 때마다 `%APPDATA%\\AiUsageWidget\\launch.log`에 기록을 남김

### Notes
- 검은 창만 깜빡이면 작업 관리자에서 pythonw.exe를 종료하고 widget.lock을 지운 뒤 start_usage_widget.vbs를 다시 실행하세요.

## [3.2.2] - 2026-09-11

### Fixed
- 실행 실패 시 숨기지 않고 오류 창을 띄움. zip 안에서 바로 실행하면 bat이 안내함
- 위젯이 바로 꺼지면 error.log를 남기고 알림
- 이미 실행 중이면 기존 창을 앞으로 가져옴

### Notes
- 실행이 안 되는 친구는 이 zip을 새로 받으세요. 이미 3.2.1이 켜져 있으면 업데이트 버튼으로 받을 수 있습니다.

## [3.2.1] - 2026-09-11

### Added
- GitHub 저장소 README에 최신 릴리스 버전 배지
- 우클릭 메뉴·도움말에 현재 위젯 버전

### Fixed
- 우클릭 메뉴와 대화상자가 항상 위 위젯 뒤로 가던 문제

### Notes
- 3.2.0에서 업데이트 버튼을 눌러 받을 수 있습니다.

## [3.2.0] - 2026-09-11

### Added
- 위젯 크기 조절: 우클릭 메뉴와 `Ctrl++` / `Ctrl+-` / `Ctrl+0` (0.75~1.5배)
- GitHub Releases용 패치노트(`CHANGELOG.md`)와 배포 스크립트의 릴리스 노트 생성

### Notes
- 3.1.0 zip을 받은 친구는 이 버전부터 크기 조절을 쓸 수 있습니다. 업데이트 버튼이 켜집니다.

## [3.1.0] - 2026-09-11

첫 공개 릴리스. 친구가 zip만 받아 실행하고, 이후 업데이트는 위젯 버튼으로 받습니다.

### Added
- `start_usage_widget.vbs` 원클릭 실행. Python이 없거나 3.9 미만이거나 Tk를 쓸 수 없으면 설치를 시도함
- Codex / Cursor 잔여량 위젯, 한 줄·상세 모드, 한도 알림
- 서비스 선택 창에서 Codex CLI·Cursor 앱 설치/로그인 준비
- 새 버전이 있을 때만 켜지는 업데이트 버튼
- 작업 표시줄 바로 위·표시줄 위로 배치 가능

### Changed
- 이미 있는 쓸 수 있는 Python은 재설치하지 않음. Microsoft Store 바로가기도 실제 설치본을 찾아 사용
- Codex는 ChatGPT 데스크톱 앱이 아니라 Codex CLI 로그인을 사용한다고 안내

### Notes
- 로그인·설정은 `%APPDATA%\AiUsageWidget`에만 저장됨
- ChatGPT Plus 계정은 API에 주간 창이 없으면 주간 값이 `—`로 보일 수 있음

[Unreleased]: https://github.com/Ceylontea96/AIUsageWidget/compare/v3.3.1...HEAD
[3.3.1]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.3.1
[3.3.0]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.3.0
[3.2.27]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.27
[3.2.26]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.26
[3.2.25]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.25
[3.2.24]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.24
[3.2.23]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.23
[3.2.22]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.22
[3.2.21]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.21
[3.2.20]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.20
[3.2.19]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.19
[3.2.18]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.18
[3.2.17]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.17
[3.2.16]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.16
[3.2.15]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.15
[3.2.14]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.14
[3.2.13]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.13
[3.2.12]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.12
[3.2.11]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.11
[3.2.10]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.10
[3.2.9]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.9
[3.2.8]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.8
[3.2.7]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.7
[3.2.6]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.6
[3.2.5]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.5
[3.2.4]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.4
[3.2.3]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.3
[3.2.2]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.2
[3.2.1]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.1
[3.2.0]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.0
[3.1.0]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.1.0
