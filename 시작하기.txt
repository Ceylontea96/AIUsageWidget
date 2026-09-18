AI Usage 위젯
=============
Windows에서 GPT·Cursor·Claude 잔여 사용량을 보는 작은 위젯입니다.

이 위젯은 OpenAI(ChatGPT·Codex)·Cursor·Anthropic과 제휴되지 않은 비공식 도구입니다. 공식 앱이 아니며, 사용량 조회는 언제든 실패하거나 바뀔 수 있습니다. 소스 라이선스는 LICENSE(MIT)입니다.

실행 (원클릭)
1) zip을 폴더로 압축 해제합니다. zip 안에서 바로 실행하면 안 됩니다. 폴더 이름과 위치는 자유입니다.
2) AI Usage.exe 를 더블클릭합니다. 안 되면 같은 폴더의 start_usage_widget.vbs, 문제확인.bat 또는 diagnose.bat 을 실행하고 나오는 글을 보내세요.
3) 처음에는 푼 폴더에서 실행하세요. 한 번 실행한 뒤에는 AI Usage.exe 만 바탕화면 등으로 복사해도 됩니다. 원래 폴더는 지우면 안 됩니다.
4) 첫 실행 때 바탕화면 바로가기를 고를 수 있고, 위젯 우클릭 → 바탕화면 바로가기 생성에서도 만들 수 있습니다.
5) 이미 있는 Python 3.9 이상(명령 프롬프트에서 python --version)을 먼저 씁니다. Microsoft Store로 깐 것도 포함합니다.
6) 없거나, 3.9 미만이거나, 위젯 화면(Tk)을 쓸 수 없을 때만 위젯용 Python을 추가로 설치합니다. 이미 있는 Python을 업그레이드하지는 않습니다.
7) 표시할 서비스(GPT / Cursor / Claude)를 고릅니다. GPT·Cursor는 로그인이 없으면 설치·로그인 창을 띄울 수 있습니다. Claude는 연동을 켠 뒤 대화형 Claude Code가 필요합니다.

로그인
위젯은 ChatGPT 데스크톱 앱과 연동되지 않습니다.
- Codex: Codex CLI 설치 후 `codex login` (위젯에서 버튼으로 가능)
- Cursor: Cursor 앱 설치 후 앱에서 로그인 (위젯에서 버튼으로 가능)
- Claude: 우클릭 → 표시할 서비스에서 Claude 연동을 켭니다. Claude.ai 구독과 지원되는 Claude Code가 필요합니다. `claude -p`만으로는 추적되지 않으며 Additional/Billing은 아직 없습니다.

조작
- 제목 줄을 드래그하면 이동합니다.
- F5: 새로고침
- Ctrl+M 또는 접기 아이콘: 한 줄 / 상세
- 우클릭 또는 Ctrl++ / Ctrl+- / Ctrl+0: 위젯 크기
- 우클릭: 표시할 서비스, 항상 위, Windows 시작 시 실행, 바탕화면 바로가기 생성, 알림, 업데이트

업데이트
새 버전이 있으면 제목 옆에 초록 "↑ 업데이트 x.x.x" 버튼이 나타납니다. 한 줄 모드에서는 제목 자리에 "↑ 새 버전"으로 보입니다. 누르면 바뀐 점을 짧게 보여 준 뒤 파일을 받고 위젯이 다시 시작됩니다. 우클릭 메뉴에서도 받을 수 있습니다.

알림이 안 보이면 register_notifications.ps1 을 우클릭 → PowerShell로 실행하세요.

이 폴더에는 계정 정보가 없습니다. 로그인과 위젯 설정은 각 PC에 따로 저장됩니다.
자세한 표시 기준은 MANUAL.txt 를 보세요.
