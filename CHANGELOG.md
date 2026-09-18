# Changelog

이 파일은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식을 따르며, 버전은 [Semantic Versioning](https://semver.org/lang/ko/)을 따릅니다.

## [Unreleased]

## [3.3.0] - 2026-09-18

### Added
- GPT 추가 한도를 접어서 보고, 그룹 단위로 표시합니다
- Claude Pro 사용량을 대화형 Claude Code와 연동해 표시합니다
- 사용량 상태와 한도 표시 구조를 서비스 공통 형태로 정리합니다

### Changed
- Cursor 대표 잔여를 Cursor Models, 보조 막대를 Other Models로 표시합니다
- mapping implementation confirmed via same-response displayMessage/percent correlation; direct Dashboard sanity check pending
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

[Unreleased]: https://github.com/Ceylontea96/AIUsageWidget/compare/v3.3.0...HEAD
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
