"""Measure isolated Windows startup routes without changing the installed widget.

Usage: python -B tools/benchmark_startup.py --samples 5
Results and the instrumented package are kept under ignored dist/.
Each process is fresh; these are warm filesystem measurements, not cold boots.
Run as the desktop user, with the same Python selected by setup_and_run.ps1.
Direct-Python results must not be compared across different interpreters.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def replace_once(text, old, new):
    assert text.count(old) == 1, f'Instrumentation point changed: {old!r}'
    return text.replace(old, new, 1)


def prepare(destination):
    package = destination / 'package'
    package.mkdir(parents=True)
    script = (PROJECT / 'publish_update.ps1').read_text(encoding='utf-8-sig')
    copy = re.search(r'\$copy\s*=\s*@\((.*?)\)', script, re.S)
    for name in re.findall(r"'([^']+)'", copy.group(1)):
        shutil.copy2(PROJECT / name, package / name)
    shutil.copytree(PROJECT / 'assets', package / 'assets')
    for name in ('README.txt', 'MANUAL.txt'):
        shutil.copy2(PROJECT / name, package / name)
    (package / 'usage_widget.py').rename(package / 'widget_app.py')
    shutil.copy2(Path(__file__).with_name('startup_probe.py'), package / 'usage_widget.py')

    source = (PROJECT / 'launcher/AIUsageLauncher.cs').read_text(encoding='utf-8')
    source = replace_once(source,
        'Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData)',
        'Environment.GetEnvironmentVariable("APPDATA")')
    source = replace_once(source, 'string exePath = Assembly.GetExecutingAssembly().Location;',
        '''File.WriteAllText(Path.Combine(Environment.GetEnvironmentVariable("AIUSAGE_BENCH_DIR"), "exe_entry.txt"),
                ((double)Stopwatch.GetTimestamp() / Stopwatch.Frequency).ToString(System.Globalization.CultureInfo.InvariantCulture));
            string exePath = Assembly.GetExecutingAssembly().Location;''')
    cs = destination / 'BenchmarkLauncher.cs'
    cs.write_text(source, encoding='utf-8')
    framework = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319'
    subprocess.run([str(framework / 'csc.exe'), '/nologo', '/target:winexe',
        '/platform:anycpu', '/optimize+', '/codepage:65001',
        f'/reference:{framework / "System.Windows.Forms.dll"}',
        f'/win32icon:{package / "assets/icons/app.ico"}',
        f'/out:{package / "AI Usage.exe"}', str(cs)], check=True, creationflags=HIDDEN)

    setup = (package / 'setup_and_run.ps1').read_text(encoding='utf-8-sig')
    setup = replace_once(setup, "$ErrorActionPreference = 'Stop'", '''$ErrorActionPreference = 'Stop'
function Mark-Benchmark([string]$Name) {
    $seconds = [double][Diagnostics.Stopwatch]::GetTimestamp() / [Diagnostics.Stopwatch]::Frequency
    $line = $Name + ' ' + $seconds.ToString('R', [Globalization.CultureInfo]::InvariantCulture) + "`n"
    [IO.File]::AppendAllText((Join-Path $env:AIUSAGE_BENCH_DIR 'powershell.txt'), $line)
}
[IO.File]::WriteAllText((Join-Path $env:AIUSAGE_BENCH_DIR 'powershell.pid'), [string]$PID)
Mark-Benchmark 'ps_entry'
''')
    replacements = (
        ('    $p = $null\n    try {', "    Mark-Benchmark 'validation_start'\n    $p = $null\n    try {"),
        ('        return $line.Trim()', "        Mark-Benchmark 'validation_end'\n        return $line.Trim()"),
        ('    Write-LaunchLog "setup start $Here"', "    Mark-Benchmark 'setup_start'\n    Write-LaunchLog \"setup start $Here\""),
        ('    Unblock-Here\n', "    Mark-Benchmark 'unblock_start'\n    Unblock-Here\n    Mark-Benchmark 'unblock_end'\n"),
        ('    $python = Get-ReadyPython\n', "    $python = Get-ReadyPython\n    Mark-Benchmark 'python_ready'\n"),
        ("        $p = Start-Process -FilePath $pythonw", "        Mark-Benchmark 'spawn_start'\n        $p = Start-Process -FilePath $pythonw"),
        ('    Write-LaunchLog "widget start', "    Mark-Benchmark 'spawn_end'\n    Write-LaunchLog \"widget start"),
        ('    Start-Sleep -Milliseconds 1200', "    Start-Sleep -Milliseconds 1200\n    Mark-Benchmark 'watch_end'"),
        ("function Show-Popup([string]$Message, [int]$Icon = 64) {", "function Show-Popup([string]$Message, [int]$Icon = 64) {\n    throw $Message"),
    )
    for before, after in replacements:
        setup = replace_once(setup, before, after)
    (package / 'setup_and_run.ps1').write_text(setup, encoding='utf-8-sig')
    return package


def initialize_appdata(base, package, cached):
    app = base / 'AiUsageWidget'
    app.mkdir(parents=True)
    (app / 'install.json').write_text(json.dumps({'root': str(package), 'shortcut_asked': True}))
    keys = ('chatgpt', 'cursor', 'claude')
    (app / 'settings.json').write_text(json.dumps({
        'setup_done': True, 'version': 3, 'enabled': dict.fromkeys(keys, True),
        'notifications': False, 'topmost': False, 'scale': 1.0,
    }))
    if cached:
        cache = {'version': 3}
        for key in keys:
            cache[key] = {'key': key, 'title': key, 'plan': 'Plus', 'ok': True,
                'hero_percent': 80, 'hero_caption': '', 'bars': [
                    {'label': '5시간', 'remaining_percent': 80, 'used_percent': 20, 'detail': ''},
                    {'label': '주간', 'remaining_percent': 60, 'used_percent': 40, 'detail': ''}],
                'fetched_at': time.time()}
        (app / 'last_snapshot.json').write_text(json.dumps(cache), encoding='utf-8')


def measure(package, run, route, cached, profile=False, runtime_cache=None):
    run.mkdir()
    env = dict(os.environ)
    env['AIUSAGE_BENCH_DIR'] = str(run)
    env['APPDATA'] = str(run / 'appdata')
    env['USERPROFILE'] = str(run / 'user')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['AIUSAGE_BENCH_EXPECT_CACHE'] = '3' if cached else '0'
    env.pop('AIUSAGE_BENCH_PROFILE', None)
    # LOCALAPPDATA is preserved because the real launcher searches it for Python.
    if profile:
        env['AIUSAGE_BENCH_PROFILE'] = '1'
    initialize_appdata(Path(env['APPDATA']), package, cached)
    if runtime_cache is not None:
        shutil.copy2(runtime_cache, Path(env['APPDATA']) / 'AiUsageWidget/runtime-v1.txt')
    system = Path(os.environ['SystemRoot']) / 'System32'
    routes = {
        'exe': [str(package / 'AI Usage.exe')],
        'vbs': [str(system / 'wscript.exe'), str(package / 'start_usage_widget.vbs')],
        'powershell': [str(system / 'WindowsPowerShell/v1.0/powershell.exe'), '-NoProfile',
                       '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File',
                       str(package / 'setup_and_run.ps1')],
        'python': [str(Path(sys.executable).with_name('pythonw.exe')), '-B', str(package / 'usage_widget.py')],
    }
    start = time.perf_counter()
    proc = subprocess.Popen(routes[route], cwd=package, env=env, creationflags=HIDDEN,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while time.perf_counter() - start < 30:
            if (run / 'error.txt').exists():
                raise RuntimeError((run / 'error.txt').read_text(encoding='utf-8'))
            psfile = run / 'powershell.txt'
            ps = psfile.read_text() if psfile.exists() else ''
            launch_log = Path(env['APPDATA']) / 'AiUsageWidget/launch.log'
            fast = launch_log.exists() and 'cached widget start ' in launch_log.read_text(encoding='utf-8-sig')
            if (run / 'result.json').exists() and (route == 'python' or 'watch_end' in ps or fast):
                break
            time.sleep(0.01)
        else:
            raise TimeoutError(f'No readiness marker: {run}')
        proc.wait(timeout=5)
        result = json.loads((run / 'result.json').read_text(encoding='utf-8'))
        events = result['events']
        events['request'] = start
        for line in ps.splitlines():
            name, value = line.split()
            events[name] = float(value)
        if (run / 'exe_entry.txt').exists():
            events['exe_entry'] = float((run / 'exe_entry.txt').read_text())
        result.update(route=route, cached=cached, profile=profile, fast_path=fast,
                      total_ms=(events['ready'] - start) * 1000)
        assert all(value >= start for value in events.values()), 'clock epochs differ'
        return result
    finally:
        if proc.poll() is None:
            proc.kill()
        # Only PIDs emitted by this isolated run; never search by process name.
        # Successful probes close themselves; timeout cleanup is scoped here.
        if not (run / 'result.json').exists():
            for filename in ('python.pid', 'powershell.pid'):
                path = run / filename
                if path.exists():
                    subprocess.run(['taskkill', '/PID', path.read_text().strip(), '/T', '/F'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=HIDDEN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--routes', nargs='+', choices=['exe', 'vbs', 'powershell', 'python'],
                        default=['exe', 'vbs', 'powershell', 'python'])
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be at least 1')
    destination = PROJECT / 'dist' / ('startup-benchmark-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    package = prepare(destination)
    results = []
    for cached in (False, True):
        for index in range(args.samples):
            # Alternate route order so one route does not always warm the others.
            routes = args.routes if index % 2 == 0 else list(reversed(args.routes))
            for route in routes:
                result = measure(package, destination / f'{cached}-{index}-{route}', route, cached)
                results.append(result)
                print(f'{route:10s} cache={cached} sample={index+1} {result["total_ms"]:.1f} ms', flush=True)
    profile_result = measure(package, destination / 'profile', 'python', True, profile=True)
    payload = {'platform': platform.platform(), 'samples': results,
               'source': subprocess.check_output(['git', '-c', f'safe.directory={PROJECT.as_posix()}',
                                                  'rev-parse', 'HEAD'], cwd=PROJECT, text=True).strip(),
               'profile_run': profile_result}
    (destination / 'results.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    interpreters = sorted({r['executable'] for r in results})
    print('Interpreters: ' + ', '.join(interpreters))
    if len(interpreters) != 1:
        print('WARNING: different interpreters were used; route totals are not directly comparable.')
    for cached in (False, True):
        for route in args.routes:
            values = [r['total_ms'] for r in results if r['route'] == route and r['cached'] == cached]
            print(f'{route:10s} cache={cached}: median {statistics.median(values):.1f} ms; range {min(values):.1f}..{max(values):.1f}')
    print(destination / 'results.json')


if __name__ == '__main__':
    main()
