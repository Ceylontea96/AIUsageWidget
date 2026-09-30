"""Run the test suite in parallel worker processes.

    py -3 -B -m tests.parallel                 # quick suite on several cores
    py -3 -B -m tests.parallel --full          # plus integration tests (before a release)
    py -3 -B -m tests.parallel -j 4 tests.test_ui tests.test_layout

Most of a serial run is Tk starting and stopping, about 1.3 s for every test
that builds the widget on the author's PC, and that spreads over processes.
Each worker starts once and imports the test modules once, then takes batches
of one class at a time from a shared queue, longest first by the times
recorded in the previous run (tests/.durations.json, not committed).

Integration tests (tests/support.py) start PowerShell and time out when the
machine is saturated, so with --full they get a phase of their own after the
other tests, on --integration-jobs workers.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DURATIONS = Path(__file__).resolve().parent / '.durations.json'
BATCH_SECONDS = 8.0     # aim for batches about this long once times are known
BATCH_TESTS = 8         # without recorded times, at most this many tests per batch
UNKNOWN_SECONDS = 1.0   # assumed time of a test never timed before


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def discover(names):
    loader = unittest.TestLoader()
    if names:
        suite = loader.loadTestsFromNames(names)
    else:
        suite = loader.discover(str(ROOT / 'tests'), pattern='test_*.py', top_level_dir=str(ROOT))
    runnable, broken = [], []
    for test in flatten(suite):
        # A module that fails to import becomes a _FailedTest; run it here.
        (broken if type(test).__module__ == 'unittest.loader' else runnable).append(test)
    return runnable, broken


def load_durations():
    try:
        data = json.loads(DURATIONS.read_text(encoding='utf-8'))
        return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def batches(test_ids, durations):
    """Batches of one class each, longest expected first."""
    by_class = defaultdict(list)
    for test_id in test_ids:
        by_class[test_id.rsplit('.', 1)[0]].append(test_id)
    out = []
    for ids in by_class.values():
        batch, spent = [], 0.0
        for test_id in ids:
            cost = durations.get(test_id, UNKNOWN_SECONDS)
            full = len(batch) >= BATCH_TESTS if not durations else spent + cost > BATCH_SECONDS
            if batch and full:
                out.append((spent, batch))
                batch, spent = [], 0.0
            batch.append(test_id)
            spent += cost
        if batch:
            out.append((spent, batch))
    out.sort(key=lambda item: -item[0])
    return [ids for _, ids in out]


# -- worker ------------------------------------------------------------------
class TimedResult(unittest.TextTestResult):
    times = {}

    def startTest(self, test):
        self._started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        TimedResult.times[test.id()] = time.perf_counter() - self._started


def run_ids(ids):
    stream = io.StringIO()
    TimedResult.times = {}
    try:
        suite = unittest.TestLoader().loadTestsFromNames(ids)
        result = unittest.TextTestRunner(stream=stream, verbosity=0, resultclass=TimedResult).run(suite)
        return {
            'ran': result.testsRun,
            'failures': [(t.id(), text) for t, text in result.failures],
            'errors': [(t.id(), text) for t, text in result.errors],
            'skipped': len(result.skipped),
            'unexpected': [t.id() for t in result.unexpectedSuccesses],
            'times': dict(TimedResult.times),
        }
    except Exception:
        return {'ran': 0, 'failures': [], 'skipped': 0, 'unexpected': [], 'times': {},
                'errors': [(', '.join(ids), traceback.format_exc())]}


def serve():
    """Worker: one JSON list of test ids per input line, one result per output line."""
    channel = sys.stdout
    sys.stdout = io.StringIO()  # a test printing must not break the protocol
    for line in sys.stdin:
        report = run_ids(json.loads(line))
        sys.stdout = io.StringIO()
        channel.write(json.dumps(report) + '\n')
        channel.flush()


# -- parent ------------------------------------------------------------------
def empty_report(ids, text):
    return {'ran': 0, 'failures': [], 'skipped': 0, 'unexpected': [], 'times': {},
            'errors': [(', '.join(ids), text)]}


def start_worker(env):
    command = [sys.executable, '-B', '-W', 'ignore::ResourceWarning', '-m', 'tests.parallel', '--serve']
    # stderr goes to a file: a pipe nobody reads would stall a chatty worker.
    errors = tempfile.TemporaryFile()
    worker = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=errors, text=True, encoding='utf-8', errors='replace', bufsize=1,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    worker.error_file = errors
    return worker


def error_text(worker):
    try:
        worker.error_file.seek(0)
        return worker.error_file.read().decode('utf-8', 'replace')[-4000:]
    except (OSError, ValueError):
        return ''


def drive(work, reports, env, lock, progress):
    """One worker process: take batches until the queue is empty."""
    worker = start_worker(env)
    try:
        while True:
            try:
                ids = work.get_nowait()
            except queue.Empty:
                return
            try:
                worker.stdin.write(json.dumps(ids) + '\n')
                worker.stdin.flush()
                line = worker.stdout.readline()
                report = json.loads(line) if line else None
            except (OSError, ValueError):
                report = None
            if report is None:
                # The worker died mid-batch (a crash inside Tk, say): report it, start another.
                worker.kill()
                worker.wait()
                report = empty_report(ids, 'worker exited without a result\n' + error_text(worker))
                worker.error_file.close()
                worker = start_worker(env)
            with lock:
                reports.append(report)
                progress(report)
    finally:
        try:
            worker.stdin.close()
            worker.wait(10)
        except (OSError, subprocess.TimeoutExpired):
            worker.kill()
        worker.error_file.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m tests.parallel')
    parser.add_argument('names', nargs='*', help='test modules, classes or methods (default: all)')
    # Measured on an 18-thread laptop: 8 workers 89 s, 12 workers 79 s, 16 workers 91 s.
    parser.add_argument('-j', '--jobs', type=int, default=max(2, min(12, (os.cpu_count() or 2) * 2 // 3)))
    parser.add_argument('--full', action='store_true', help='also run the integration tests')
    parser.add_argument('--integration-jobs', type=int, default=2,
                        help='workers for the integration phase of --full (default 2)')
    parser.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.serve:
        serve()
        return 0

    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
    if args.full:
        env['AIUSAGE_FULL_TESTS'] = '1'
        os.environ['AIUSAGE_FULL_TESTS'] = '1'
    started = time.perf_counter()
    runnable, broken = discover(args.names)
    durations = load_durations()
    heavy = [test.id() for test in runnable if getattr(type(test), 'integration', False)]
    light = [test.id() for test in runnable if not getattr(type(test), 'integration', False)]
    phases = [(batches(light, durations), args.jobs)]
    if args.full and heavy:
        phases.append((batches(heavy, durations), args.integration_jobs))
    else:
        phases[0][0].extend(batches(heavy, durations))  # skipped at once
    reports, lock = [], threading.Lock()

    def progress(report):
        mark = 'F' if report['failures'] or report['errors'] else '.'
        print(mark * max(1, report['ran']), end='', flush=True)

    if broken:
        result = unittest.TestResult()
        unittest.TestSuite(broken).run(result)
        reports.append({'ran': result.testsRun, 'failures': [], 'skipped': 0, 'unexpected': [], 'times': {},
                        'errors': [(t.id(), text) for t, text in result.errors]})
    summary = []
    for plan, wanted in phases:
        work = queue.Queue()
        for ids in plan:
            work.put(ids)
        jobs = max(1, min(wanted, len(plan) or 1))
        threads = [threading.Thread(target=drive, args=(work, reports, env, lock, progress), daemon=True)
                   for _ in range(jobs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        summary.append(f'{len(plan)} batches on {jobs} workers')
    elapsed = time.perf_counter() - started
    print()
    totals = {'ran': 0, 'skipped': 0, 'failures': [], 'errors': [], 'unexpected': []}
    times = dict(durations)
    for report in reports:
        for key in ('failures', 'errors', 'unexpected'):
            totals[key] += report[key]
        totals['ran'] += report['ran']
        totals['skipped'] += report['skipped']
        times.update(report['times'])
    try:
        DURATIONS.write_text(json.dumps(times, indent=0, sort_keys=True), encoding='utf-8')
    except OSError:
        pass
    for kind in ('failures', 'errors'):
        for test_id, text in sorted(totals[kind]):
            print('=' * 70)
            print(f'{"FAIL" if kind == "failures" else "ERROR"}: {test_id}')
            print('-' * 70)
            print(text)
    print('-' * 70)
    print(f'Ran {totals["ran"]} tests in {elapsed:.1f}s ({", then ".join(summary)})')
    problems = len(totals['failures']) + len(totals['errors']) + len(totals['unexpected'])
    detail = ', '.join(f'{n}={v}' for n, v in (
        ('failures', len(totals['failures'])), ('errors', len(totals['errors'])),
        ('skipped', totals['skipped']), ('unexpected successes', len(totals['unexpected']))) if v)
    print(('FAILED' if problems else 'OK') + (f' ({detail})' if detail else ''))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
