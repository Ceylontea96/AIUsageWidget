"""The launcher's fast path gets its cache even when the widget registers late,
and a git checkout is never overwritten by the widget's own updater."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import updater
import usage_widget as u
from tests.support import integration

PROJECT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
param($SetupSource, $Here, $RegisterAfterMs, $SleeperSeconds)
$ErrorActionPreference = 'Stop'
$env:APPDATA = Join-Path $Here 'appdata'
$appDir = Join-Path $env:APPDATA 'AiUsageWidget'
New-Item -ItemType Directory -Path $appDir -Force | Out-Null
$Widget = Join-Path $Here 'usage_widget.py'
[IO.File]::WriteAllText($Widget, '# fixture')
[IO.File]::WriteAllText((Join-Path $Here 'setup_and_run.ps1'), '# fixture')
$runtime = Join-Path $Here 'runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$python = Join-Path $runtime 'python.exe'; $pythonw = Join-Path $runtime 'pythonw.exe'
Copy-Item "$env:WINDIR\System32\where.exe" $python; Copy-Item "$env:WINDIR\System32\where.exe" $pythonw
$ast = [System.Management.Automation.Language.Parser]::ParseFile($SetupSource, [ref]$null, [ref]$null)
foreach ($node in $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $false)) {
    if ($node.Name -in @('Save-RuntimeCache', 'Write-LaunchLog')) { Invoke-Expression $node.Extent.Text }
}
# Stands in for the widget process: alive for a while, registering late (or never).
$sleeper = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-Command', "Start-Sleep -Seconds $SleeperSeconds") -WindowStyle Hidden -PassThru
$instance = Join-Path $appDir 'widget.instance'
[IO.File]::WriteAllLines($instance, @('1', '999'))   # an older widget's registration
if ($RegisterAfterMs -ne 'never') {
    $write = "Start-Sleep -Milliseconds $RegisterAfterMs; [IO.File]::WriteAllLines('$instance', @('$($sleeper.Id)', '123'))"
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-Command', $write) -WindowStyle Hidden | Out-Null
}
$watch = [Diagnostics.Stopwatch]::StartNew()
Save-RuntimeCache $python $pythonw $sleeper.Id
$elapsed = $watch.Elapsed.TotalSeconds
Stop-Process -Id $sleeper.Id -ErrorAction SilentlyContinue
$cache = Join-Path $appDir 'runtime-v1.txt'
$log = Join-Path $appDir 'launch.log'
Write-Output ("cached=" + (Test-Path -LiteralPath $cache))
Write-Output ("elapsed=" + [math]::Round($elapsed, 2))
if (Test-Path -LiteralPath $log) { Write-Output ("log=" + [IO.File]::ReadAllText($log).Trim()) }
'''


@integration('runs the setup script')
@unittest.skipUnless(os.name == 'nt' and shutil.which('powershell'), 'Windows launcher integration')
class RuntimeCacheTests(unittest.TestCase):
    def run_save(self, register_after_ms, sleeper_seconds=20):
        with tempfile.TemporaryDirectory() as folder:
            here = Path(folder) / 'widget'
            here.mkdir()
            script = here / 'save-cache.ps1'
            script.write_text(SCRIPT, encoding='utf-8-sig')
            done = subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script),
                 str(PROJECT / 'setup_and_run.ps1'), str(here), str(register_after_ms), str(sleeper_seconds)],
                capture_output=True, timeout=90, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            out = done.stdout.decode('utf-8', 'replace')
            self.assertEqual(done.returncode, 0, out + done.stderr.decode('utf-8', 'replace'))
            return dict(line.split('=', 1) for line in out.splitlines() if '=' in line)

    def test_a_widget_that_registers_after_setup_checks_is_still_cached(self):
        # The widget registered 1.8 s after starting; setup used to look once at 1.2 s.
        result = self.run_save(register_after_ms=2500)
        self.assertEqual(result['cached'], 'True', result)
        self.assertGreaterEqual(float(result['elapsed']), 2.0)

    def test_a_widget_that_exits_without_registering_is_not_cached(self):
        result = self.run_save(register_after_ms='never', sleeper_seconds=1)
        self.assertEqual(result['cached'], 'False', result)
        self.assertLess(float(result['elapsed']), 10, 'the wait ends with the process')
        self.assertIn('runtime cache skipped', result.get('log', ''))


class GitCheckoutUpdateTests(unittest.TestCase):
    def test_git_checkouts_are_recognised(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertFalse(updater.is_git_checkout(root))
            (root / '.git').mkdir()
            self.assertTrue(updater.is_git_checkout(root))
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / '.git').write_text('gitdir: elsewhere', encoding='utf-8')
            self.assertTrue(updater.is_git_checkout(folder), 'a worktree has a .git file')

    def test_the_widget_never_updates_a_git_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            with patch.object(u, 'SETTINGS_PATH', path / 'settings.json'), patch.object(u, 'CACHE_PATH', path / 'cache.json'):
                animate = u.UpdatePill.animate
                u.UpdatePill.animate = False
                try:
                    w = u.UsageWidget(preview=True)
                    w.root.withdraw()
                    w.preview = False
                    w.update_info = {'version': '9.9.9', 'zip': 'https://example.invalid/a.zip', 'notes': ''}
                    (path / '.git').mkdir()
                    with patch.object(u, 'APP_ROOT', path), patch.object(w, 'notify') as notify, \
                            patch.object(u, 'download_and_stage') as download:
                        w.install_update()
                    self.assertIn('git pull', notify.call_args.args[2])
                    self.assertIn('9.9.9', notify.call_args.args[2])
                    download.assert_not_called()
                    self.assertFalse(w._update_busy)
                    w.close()
                finally:
                    u.UpdatePill.animate = animate


if __name__ == '__main__':
    unittest.main()
