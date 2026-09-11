# Changelog

이 파일은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식을 따르며, 버전은 [Semantic Versioning](https://semver.org/lang/ko/)을 따릅니다.

## [Unreleased]

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

[Unreleased]: https://github.com/Ceylontea96/AIUsageWidget/compare/v3.2.0...HEAD
[3.2.0]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.2.0
[3.1.0]: https://github.com/Ceylontea96/AIUsageWidget/releases/tag/v3.1.0
