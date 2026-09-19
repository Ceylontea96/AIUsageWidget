# Claude integration — 3.4.2

이 문서는 내부 검증과 개인정보 저장 범위를 설명합니다. 사용자 안내는 MANUAL.txt를 참고하세요.

## 설정과 복구

설치는 사용자가 연동을 선택했을 때만 Claude settings.json의 statusLine을 변경합니다.
명령의 정확한 wrapper 경로와 statusLine fingerprint로 소유권을 판단합니다.
integration.json에는 최초 사용자 statusLine, 설치한 명령과 fingerprint, 설치 여부,
Claude 실행 파일 경로와 버전을 보관합니다. 원래 명령은 복구에 필요한 사용자 설정이며
경로를 포함할 수 있습니다. 이 복구 메타데이터는 아래 사용량 캐시와 별개입니다.

반복 설치 및 wrapper 파일만 사라진 경우 기존 원본 백업을 유지합니다.
설정이 우리 wrapper인데 백업이 없거나 손상된 경우에는 원본을 추측하지 않고 설치/해제를
중단합니다. 사용자가 statusLine을 변경한 경우에도 자동 덮어쓰기/원복하지 않습니다.
settings.json을 읽지 못하면 어떤 설정도 변경하지 않습니다.
직접·간접 자기 포워딩은 경로 검사와 자식 프로세스 재진입 표시로 차단합니다.

## 버전과 기능 확인

실행 파일은 PATH, 표준 CLI 설치 위치, 일반/Store Desktop의 CLI 위치에서 찾습니다.
버전은 실행 파일의 절대 경로·mtime_ns·size별로 캐시합니다. 성공은 1시간, 실패는
30초 동안 재사용합니다. 경로/메타데이터 변경, 연동 설치, 명시적 새로고침은 재검사합니다.
상태 표시의 버전 검사는 백그라운드 스레드에서, CLI 사용량 검사는 별도 워커에서 실행합니다.
사용자가 직접 누른 연동 설치의 최초 버전 검사는 동기식이며 최대 2초입니다.
2초 캐시 갱신과 render에서는 버전 subprocess를 실행하지 않습니다.

버전은 최소 지원 버전의 sanity filter이며 기능 지원의 증거가 아닙니다.
statusLine은 실제 입력에서 version과 알려진 rate_limits 창의 숫자/리셋 schema를 검증합니다.
CLI는 실제 get_usage control_response와 session/weekly_all 등의 알려진 kind,
명시적 percent 또는 검증 가능한 utilization scale을 검사합니다.
새로운 고버전이라도 해당 schema가 없거나 모호하면 unavailable로 처리합니다.
네트워크 endpoint를 추가하거나 credential을 직접 읽지 않습니다.

## 사용량 캐시의 정확한 persisted whitelist

세션 파일: `%APPDATA%/AiUsageWidget/claude/sessions/<session_key>.json`.
알 수 없는 입력 필드는 허용하되 저장하거나 quota 판단에 사용하지 않습니다.

최상위 필드는 다음 10개만 허용합니다(값이 없으면 일부는 생략).

- `source`: claude_statusline
- `schema_version`: 정수 1
- `session_key`: 기기별 salt로 만든 64자리 소문자 16진수 HMAC
- `claude_code_version`: 정규화된 숫자 버전
- `bridge_seen_at`: statusLine 호출 시각
- `quota_observed_at`: quota 관측 시각
- `five_hour`: 5시간 창
- `seven_day`: 주간 창
- `last_transcript_mtime`: transcript 파일 stat의 수정 시각만 저장
- `last_window_fingerprint`: 알려진 quota의 사용률/리셋으로 재계산한 비교 문자열

각 창에는 아래 세 필드만 저장합니다.

- `used_percent`: 사용률 0–100
- `remaining_percent`: 잔여율 0–100, 사용률과 합계 100
- `resets_at`: 리셋 Unix 초

숫자는 int/float만 허용하며 bool, 문자열 숫자, NaN/Inf는 거부합니다.
timestamp는 양수이고 10000년 이전이어야 합니다. bridge_seen_at의 미래 허용 오차는
60초이며 quota_observed_at은 bridge_seen_at보다 나중일 수 없습니다.
필수 세션 식별 정보가 없거나 틀리면 해당 세션을 무시합니다. quota 관측 시각이 없거나
유효하지 않으면 창을 사용할 수 없습니다. 필수 quota 필드 하나가 잘못되면 그 창만 무시합니다.
손상 JSON/UTF-8과 잘못된 세션은 정상 세션 선택을 막지 않습니다.

raw session_id, transcript_path, transcript 내용, prompt, cwd, project, repo,
account/email, token, cookie, Authorization, credential은 사용량 캐시/bridge 로그에 저장하지 않습니다.
transcript는 stat만 하며 내용을 열어 읽지 않습니다. 원본 statusLine으로 stdin을 전달하는 경우
기존 사용자 명령이 처리하는 입력은 이 위젯의 저장 범위와 별개입니다.

## Freshness

quota 변경 fingerprint는 새로운 관측 신호입니다. 사용률이 같아도 활성 세션에서
직전 statusLine 호출 이후 transcript mtime이 증가했다면 보조 신호로 사용합니다.
mtime은 API 응답 완료를 보장하지 않는 heuristic입니다. 비활성 세션의 파일 변경,
과거 변경 시각, 비정상 미래 mtime으로 같은 quota를 새 데이터로 되살리지 않습니다.
mtime 신호는 직전 호출 이후 20초 이내의 활성 세션과 유효한 quota 입력에만 결합합니다.
동일 quota의 반복 호출만으로 관측 시각을 바꾸지 않습니다.

서로 다른 세션의 창/관측 시각은 혼합하지 않습니다. statusLine이 없거나 오래되면
CLI fallback을 허용합니다. CLI 응답을 다시 그려도 최초 관측 시각을 유지하며
새 응답 없이 5분이 지나면 이전 데이터로 표시합니다.

## 로그와 정리

bridge.log는 호출 시각, rate_limits 유무, 알려진 창 이름과 정규화된 버전만 기록합니다.
최대 약 64 KiB로 제한합니다. session.salt는 무작위 기기별 해시 키입니다.
uninstall은 사용자 settings.json의 나머지 키와 원본 statusLine을 보존한 뒤
statusline_bridge.py, integration.json, session.salt, bridge.log를 정리합니다.
sessions 안의 64자리 hash.json 및 그 atomic-write 임시 파일,
wrapper/integration.json의 PID가 붙은 임시 파일만 삭제합니다. 다른 이름의 파일과
하위 디렉터리는 그대로 둡니다. 정리 실패는 이미 완료된 원본 복구를 취소하지 않습니다.
Claude 자체 로그, 프로젝트, transcript, credential, 사용자 settings.json 파일은 삭제하지 않습니다.

## 제외 범위

canonical 모델 재설계, identity fallback 전면 개선, Additional/Billing 확장,
새 endpoint/Provider는 이 패치에 포함하지 않습니다.
