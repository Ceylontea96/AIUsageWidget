"""Compare idle Codex log monitoring with a Git baseline using synthetic files.

No account logs are read. Poll timestamps advance by 0.25 s without sleeping;
elapsed time measures just monitor execution, not 25 seconds of real app use.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import types
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', default='HEAD')
    parser.add_argument('--samples', type=int, default=5)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be positive')
    baseline = subprocess.check_output(['git', '-c', f'safe.directory={PROJECT.as_posix()}',
        'show', f'{args.baseline_ref}:codex_activity.py'], cwd=PROJECT)
    sources = {'before': baseline, 'after': (PROJECT / 'codex_activity.py').read_bytes()}
    modules = {}
    for name, source in sources.items():
        module = types.ModuleType('activity_' + name)
        exec(compile(source, name + '/codex_activity.py', 'exec'), module.__dict__)
        modules[name] = module
    destination = PROJECT / 'dist' / ('codex-activity-benchmark-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    home = destination / 'fixture'
    day = home / 'sessions' / datetime.now().strftime('%Y/%m/%d')
    day.mkdir(parents=True)
    line = json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(),
                       'type': 'event_msg', 'payload': {'type': 'item_completed'}}) + '\n'
    for i in range(64):
        (day / f'{i:02}.jsonl').write_text(line, encoding='utf-8')
    results = []
    for index in range(args.samples):
        order = ('before', 'after') if index % 2 == 0 else ('after', 'before')
        for name in order:
            monitor = modules[name].CodexActivityMonitor(home)
            monitor.poll(0)
            started = time.perf_counter()
            for scan in range(1, 101):
                monitor.poll(scan * .25)
            elapsed = (time.perf_counter() - started) * 1000
            results.append({'variant': name, 'pair': index, 'ms': elapsed})
    # Count I/O separately so the spy does not distort the elapsed-time samples.
    opens = {}
    original_open = Path.open
    for name in modules:
        monitor = modules[name].CodexActivityMonitor(home)
        monitor.poll(0)
        count = [0]
        def counted(path, *values, **keywords):
            count[0] += 1
            return original_open(path, *values, **keywords)
        with patch.object(Path, 'open', counted):
            for scan in range(1, 101):
                monitor.poll(scan * .25)
        opens[name] = count[0]
    payload = {'baseline_ref': args.baseline_ref, 'python': sys.version,
               'platform': platform.platform(), 'files': 64, 'polls': 100,
               'source_sha256': {name: hashlib.sha256(source).hexdigest() for name, source in sources.items()},
               'file_opens': opens, 'samples': results}
    (destination / 'results.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    for name in modules:
        values = [r['ms'] for r in results if r['variant'] == name]
        print(f'{name}: {opens[name]} opens; median {statistics.median(values):.1f} ms per 100 polls')
    print(destination / 'results.json')


if __name__ == '__main__':
    main()
