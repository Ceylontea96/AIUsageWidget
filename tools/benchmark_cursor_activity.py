"""Measure caller blocking during Cursor discovery with synthetic transcripts.

The synchronous scanner is the same implementation used inside the worker.
No real Cursor projects or accounts are accessed.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from cursor_activity import CursorActivityMonitor, BackgroundCursorActivityMonitor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=5)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be positive')
    destination = PROJECT / 'dist' / ('cursor-activity-benchmark-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    root = destination / 'fixture'
    for project in range(20):
        for conversation in range(50):
            path = root / str(project) / 'agent-transcripts' / str(conversation) / 'events.jsonl'
            path.parent.mkdir(parents=True)
            path.write_text('{"type":"turn_ended"}\n', encoding='utf-8')
    samples = []
    for index in range(args.samples):
        for background in ((False, True) if index % 2 == 0 else (True, False)):
            monitor = (BackgroundCursorActivityMonitor if background else CursorActivityMonitor)(root)
            calls = []
            now = time.monotonic()
            started = time.perf_counter()
            try:
                while True:
                    call_start = time.perf_counter()
                    monitor.poll(now)
                    calls.append((time.perf_counter() - call_start) * 1000)
                    if not background or not monitor._busy:
                        break
                    if time.perf_counter() - started > 10:
                        raise TimeoutError('background scan did not complete')
                    time.sleep(.001)
                samples.append({'background': background, 'pair': index,
                    'first_call_ms': calls[0], 'max_call_ms': max(calls),
                    'caller_total_ms': sum(calls), 'ready_ms': (time.perf_counter() - started) * 1000})
            finally:
                if background:
                    monitor.close()
                    monitor._thread.join(3)
                    assert not monitor._thread.is_alive()
    payload = {'python': sys.version, 'platform': platform.platform(), 'projects': 20, 'files': 1000,
        'source_sha256': hashlib.sha256((PROJECT / 'cursor_activity.py').read_bytes()).hexdigest(),
        'samples': samples}
    (destination / 'results.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    for background in (False, True):
        rows = [r for r in samples if r['background'] == background]
        print('background=', background,
              'max caller block median(ms)=', round(statistics.median(r['max_call_ms'] for r in rows), 3),
              'ready median(ms)=', round(statistics.median(r['ready_ms'] for r in rows), 1))
    print(destination / 'results.json')


if __name__ == '__main__':
    main()
