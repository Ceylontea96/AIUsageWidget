"""Compare setup and cached-runtime startup using the same instrumented package.

The cache is produced by one real, isolated PowerShell startup before timing.
Run as the desktop user using the Python selected by setup_and_run.ps1.
"""
import argparse
from datetime import datetime
import hashlib
import json
import statistics

from benchmark_startup import PROJECT, measure, prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=5)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be at least 1')
    destination = PROJECT / 'dist' / ('launch-comparison-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    package = prepare(destination)
    seed = destination / 'bootstrap'
    measure(package, seed, 'powershell', cached=True)
    cache = seed / 'appdata/AiUsageWidget/runtime-v1.txt'
    assert cache.is_file(), 'setup did not confirm and cache the initialized widget'
    results = []
    for index in range(args.samples):
        for route in ('exe', 'vbs'):
            order = (False, True) if index % 2 == 0 else (True, False)
            for warm in order:
                result = measure(package, destination / f'{index}-{route}-{warm}', route,
                                 cached=True, runtime_cache=cache if warm else None)
                assert result['fast_path'] == warm, 'unexpected startup path'
                if warm:
                    assert 'ps_entry' not in result['events'], 'cached start fell back to setup'
                result.update(warm_runtime=warm, pair=index)
                results.append(result)
                print(f'{route} warm_runtime={warm}: {result["total_ms"]:.1f} ms', flush=True)
    assert len({r['executable'] for r in results}) == 1, 'different Python interpreters'
    inputs = ('usage_widget.py', 'setup_and_run.ps1', 'start_usage_widget.vbs',
              'launcher/AIUsageLauncher.cs')
    payload = {'source_sha256': {name: hashlib.sha256((PROJECT / name).read_bytes()).hexdigest()
                                 for name in inputs}, 'samples': results}
    (destination / 'results.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    for route in ('exe', 'vbs'):
        for warm in (False, True):
            values = [r['total_ms'] for r in results
                      if r['route'] == route and r['warm_runtime'] == warm]
            print(f'{route} warm_runtime={warm}: median {statistics.median(values):.1f} ms')
    print(destination / 'results.json')


if __name__ == '__main__':
    main()
