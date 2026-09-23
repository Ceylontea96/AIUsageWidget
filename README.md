# AI Usage

Windows에서 GPT·Cursor·Claude 잔여 사용량을 보는 작은 위젯입니다.

[![version](https://img.shields.io/github/v/release/Ceylontea96/AIUsageWidget?label=version)](https://github.com/Ceylontea96/AIUsageWidget/releases/latest)

이 위젯은 OpenAI(ChatGPT·Codex)·Cursor·Anthropic과 제휴되지 않은 비공식 도구입니다. 공식 앱이 아니며, 사용량 조회는 언제든 실패하거나 바뀔 수 있습니다. 소스 라이선스는 [LICENSE](LICENSE)(MIT)입니다.

현재 버전은 [Releases](https://github.com/Ceylontea96/AIUsageWidget/releases/latest)의 최신 태그입니다.

## 실행

1. [Releases](https://github.com/Ceylontea96/AIUsageWidget/releases/latest)에서 `AIUsageWidget.zip`을 받습니다.
2. 압축을 풀고 `AI Usage.exe`를 실행합니다.
3. 한 번 실행한 뒤에는 실행 파일만 다른 곳으로 복사해도 됩니다. 원래 폴더는 그대로 두세요.
4. Python이 없으면 처음 한 번 설치를 시도합니다.
5. GPT는 Codex CLI 로그인, Cursor는 Cursor 앱 로그인, Claude는 Claude Code 연동이 필요합니다.

## 사용량을 읽는 방법

- GPT는 설치된 Codex CLI에 공식 로컬 인터페이스(`codex app-server`)로 사용량을 물어봅니다. 위젯은 Codex 로그인 토큰을 읽지 않습니다.
- Claude는 공식 statusLine 값과 Claude Code 자신에게 보내는 사용량 조회만 씁니다. 계정 토큰에 접근하지 않습니다.
- Cursor는 공개된 사용량 API가 없어, Cursor 앱이 쓰는 내부 사용량 조회를 Cursor 로그인 정보로 호출합니다. Cursor 약관상 회색지대일 수 있으니 판단해서 사용하세요. 우클릭에서 Cursor 조회를 끌 수 있습니다.

새 버전이 올라가면 위젯의 **업데이트** 버튼이 켜집니다.

## 패치노트

- [Releases](https://github.com/Ceylontea96/AIUsageWidget/releases)
- [CHANGELOG.md](CHANGELOG.md)

설정과 로그인은 `%APPDATA%\AiUsageWidget`에 저장되며 저장소에는 없습니다.
