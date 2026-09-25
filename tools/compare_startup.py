"""Compare one working-tree source file with a Git version, alternating runs.

Only that file (usage_widget.py unless --file names another) differs between
packages; all other files and probes match. Run with the same Python that
setup_and_run.ps1 selects, as the desktop user.
"""
import argparse
from datetime import datetime
import hashlib
import json
import shutil
import statistics
import subprocess

from benchmark_startup import PROJECT, measure, prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', default='HEAD')
    parser.add_argument('--file', default='usage_widget.py',
                        help='the packaged source file to take from the baseline')
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--cache', choices=('both', 'empty', 'populated'), default='both')
    parser.add_argument('--routes', nargs='+', choices=('exe', 'vbs', 'powershell', 'python'),
                        default=['exe', 'python'])
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be at least 1')
    baseline = subprocess.check_output(['git', '-c', f'safe.directory={PROJECT.as_posix()}',
        'show', f'{args.baseline_ref}:{args.file}'], cwd=PROJECT)
    # The package runs usage_widget.py as widget_app.py behind the startup probe.
    packaged = 'widget_app.py' if args.file == 'usage_widget.py' else args.file
    destination = PROJECT / 'dist' / ('startup-comparison-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    after = prepare(destination / 'after')
    before = destination / 'before' / 'package'
    shutil.copytree(after, before)
    if not (before / packaged).is_file():
        parser.error(f'{args.file} is not part of the package')
    (before / packaged).write_bytes(baseline)
    packages = {'before': before, 'after': after}
    results = []
    scenarios = (False, True) if args.cache == 'both' else (args.cache == 'populated',)
    for cached in scenarios:
        for index in range(args.samples):
            for route in args.routes:
                order = ['before', 'after'] if index % 2 == 0 else ['after', 'before']
                for variant in order:
                    result = measure(packages[variant],
                        destination / f'{cached}-{index}-{route}-{variant}', route, cached)
                    result.update(variant=variant, pair=index)
                    results.append(result)
                    print(f'{variant} {route} cached={cached}: {result["total_ms"]:.1f} ms', flush=True)
    payload = {'baseline_ref': args.baseline_ref, 'file': args.file,
        'file_sha256': {key: hashlib.sha256((path / packaged).read_bytes()).hexdigest()
                        for key, path in packages.items()}, 'samples': results}
    (destination / 'results.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    interpreters = {row['executable'] for row in results}
    if len(interpreters) != 1:
        raise RuntimeError('Different interpreters used; results are not directly comparable')
    for cached in scenarios:
        for route in args.routes:
            for variant in packages:
                values = [r['total_ms'] for r in results
                          if (r['cached'], r['route'], r['variant']) == (cached, route, variant)]
                print(f'{variant} {route} cached={cached}: median {statistics.median(values):.1f} ms')
    print(destination / 'results.json')


if __name__ == '__main__':
    main()
