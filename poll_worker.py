"""Short-lived read-only worker. Never receives or emits credential material."""
import json
import sys
import time
import providers

from providers import fetch_chatgpt, fetch_cursor, error_snapshot, snapshot_to_dict

if __name__ == '__main__':
    key = sys.argv[1]
    if key not in ('chatgpt', 'cursor'):
        raise SystemExit(2)
    if key == 'cursor' and len(sys.argv) > 2:
        providers._PLAN_MEM.update(name=sys.argv[2], until=time.time()+1800)
    try:
        snap = (fetch_chatgpt if key == 'chatgpt' else fetch_cursor)()
    except Exception as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else '로그인 상태와 연결을 확인하세요.'
        snap = error_snapshot(key, key, message, '')
    sys.stdout.buffer.write(json.dumps(snapshot_to_dict(snap), ensure_ascii=True, allow_nan=False).encode('ascii'))
