# AGENTS.md

AI Usage 위젯(Windows, Python/Tk) 저장소에서 일하는 모든 AI 에이전트가 따르는 규칙입니다. Claude Code는 `CLAUDE.md`를 거쳐 이 파일을 읽습니다.

## 시작하기 전에

- 사용자는 이 작업 폴더를 여러 에이전트와 함께 씁니다. 작업을 시작할 때마다 `git fetch`, `git status`, `origin/main`과의 차이, 최신 GitHub 릴리스(태그가 가리키는 커밋과 `updater.py`의 `APP_VERSION`)를 확인합니다. 내가 만들지 않은 커밋 안 된 변경이 있으면 커밋하거나 지우거나 그 위에 작업하기 전에 사용자에게 알리고 묻습니다.
- 폴더 소유자가 `BUILTIN\Administrators`라서 git은 명령마다 `git -c safe.directory='*' ...`로 실행합니다. 전역 git 설정은 바꾸지 않습니다. `gh`에는 `--repo Ceylontea96/AIUsageWidget`을 붙입니다.
- 사용자의 위젯이 이 폴더에서 실행 중일 수 있습니다(`pythonw usage_widget.py`). 위젯 본체는 시작할 때 읽은 코드로 계속 돌지만, Claude 사용량 조회는 매번 새 `poll_worker.py` 프로세스가 `providers.py` 등을 디스크에서 다시 읽습니다. 작업 도중에도 모듈 사이의 함수 이름과 인자를 깨뜨리지 않고, 끝나면 위젯을 다시 시작해야 반영된다고 사용자에게 알립니다.

## 사용자와 소통

- 사용자에게 보이는 글(진행 설명, 질문, 보고)은 모두 한국어로 씁니다.
- 코드, 코드 주석, 커밋 메시지는 영어로 씁니다. `CHANGELOG.md`, `README.md`, `MANUAL.txt`, 이 파일 같은 문서는 한국어로 씁니다.

## 테스트

- 빠른 전체 테스트: `py -3 -B -m tests.parallel` (약 1분)
- 릴리스 전: `py -3 -B -m tests.parallel --full`. PowerShell, 런처, git 복제처럼 실제 프로세스를 띄우는 통합 테스트까지 돌립니다.
- 프로세스를 띄우는 느린 테스트에는 `tests/support.py`의 `@integration(...)`을 붙여 빠른 실행에서 빠지게 합니다.

## 변경 기록과 커밋

- 사용자에게 보이는 변경은 그 커밋에서 `CHANGELOG.md`의 `## [Unreleased]` 아래(`### Changed`, `### Fixed` 등)에 한국어로 적습니다. 무엇이 왜 바뀌었는지 쓰고, 성능 변경에는 잰 숫자를 넣습니다.
- 커밋 제목은 `fix:`, `perf:`, `refactor:`, `test:`, `chore:`, `docs:` 접두어를 붙인 영어이고, 본문에 이유와 측정값을 씁니다.
- 일회용 측정 스크립트는 저장소에 넣지 않습니다. 숫자는 커밋 메시지와 CHANGELOG에 남깁니다.

## 성능과 화면 품질

- 위젯은 늘 켜져 있으므로 상시 비용(타이머, 파일 감시, 애니메이션)을 가볍게 유지합니다.
- 애니메이션과 UI 품질은 떨어뜨리지 않습니다. 그리기를 건드리는 성능 변경에는 화면이 같게(또는 더 좋게) 보인다는 근거(픽셀·위치 오차, 프레임 간격)를 함께 제시합니다.
- 사용자 PC의 CPU는 성능 코어와 효율 코어가 섞여 있어, 같은 작업도 CPU 시간이 두 배까지 달라집니다. 성능 비교는 측정 프로세스를 한 코어에 고정(`SetProcessAffinityMask`)하고, 바꾸기 전과 후를 번갈아 여러 번 재서 중앙값으로 합니다.

## 계정 정보

- GPT는 Codex의 공식 로컬 인터페이스(`codex app-server`)로, Claude는 statusLine과 Claude Code CLI로만 읽습니다. 두 서비스의 로그인 토큰은 읽지 않습니다.
- Cursor는 액세스 토큰만 읽기 전용으로 씁니다. 토큰을 갱신하거나 Cursor의 로그인 정보를 바꾸지 않습니다.
- 설정, 캐시, 로그는 `%APPDATA%\AiUsageWidget`에 있고 저장소에는 없습니다.

## 배포 파일

- 위젯이 불러오는 모든 모듈은 `publish_update.ps1`의 `$copy` 목록에 있어야 합니다. `tests/test_publish.py`가 확인합니다.
- `claude_bridge.py`는 Claude 설정 폴더에 `statusline_bridge.py`로 복사되어 혼자 실행됩니다. 다른 위젯 모듈을 import하면 안 됩니다.
- git 작업 폴더에서는 위젯의 업데이트 버튼이 파일을 바꾸지 않습니다. 이 폴더는 git으로 갱신합니다.

## 릴리스

사용자가 요청할 때만 합니다.

1. `updater.py`의 `APP_VERSION`과 `MANUAL.txt` 첫 줄의 버전을 올리고, `CHANGELOG.md`의 `[Unreleased]` 항목을 `## [X.Y.Z] - YYYY-MM-DD` 아래로 옮깁니다. 커밋 제목은 `Release X.Y.Z with ...`입니다.
2. `py -3 -B -m tests.parallel --full`이 통과해야 합니다.
3. `origin/main`에 push한 뒤 `powershell -NoProfile -ExecutionPolicy Bypass -File publish_update.ps1 -GitHub`를 실행합니다. 이 스크립트는 커밋이나 push가 안 된 상태, 같거나 낮은 버전, 이미 있는 태그를 거부합니다. 통과하면 검사한 커밋에 태그를 붙이고 zip과 `latest.json`(SHA-256 포함)을 올립니다. 커밋되지 않은 작업 폴더에서는 배포하지 않습니다.
