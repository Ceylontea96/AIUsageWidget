"""Read-only quota worker. Never receives or emits credential material.

`poll_worker.py <key>` answers once and exits. `poll_worker.py --serve` stays
up and answers one key per input line with one JSON line, until its input
closes; a provider's login then stays in this process between reads instead
of being loaded again for every poll.
"""
import json
import sys

from providers import fetch_claude_cli, fetch_cursor, error_snapshot, snapshot_to_dict

# GPT is read on a thread in the widget through its Codex app-server client.
FETCHERS = {'cursor': fetch_cursor, 'claude': fetch_claude_cli}


def answer(key):
    try:
        snap = FETCHERS[key]()
    except Exception as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else '로그인 상태와 연결을 확인하세요.'
        snap = error_snapshot(key, key, message, '', getattr(exc, 'retry_after', ''))
    return json.dumps(snapshot_to_dict(snap), ensure_ascii=True, allow_nan=False).encode('ascii')


def serve(stdin, stdout):
    for raw in iter(stdin.readline, b''):
        key = raw.decode('ascii', 'replace').strip()
        if key not in FETCHERS:
            stdout.write(b'{}\n')
        else:
            stdout.write(answer(key) + b'\n')
        stdout.flush()


if __name__ == '__main__':
    if sys.argv[1:] == ['--serve']:
        serve(sys.stdin.buffer, sys.stdout.buffer)
        raise SystemExit(0)
    key = sys.argv[1] if len(sys.argv) > 1 else ''
    if key not in FETCHERS:
        raise SystemExit(2)
    sys.stdout.buffer.write(answer(key))
