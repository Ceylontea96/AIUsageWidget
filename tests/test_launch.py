"""Exercise Windows launch failures using isolated scripts and directories."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import usage_widget as u


PROJECT = Path(__file__).resolve().parents[1]


def powershell(script, *args, cwd=None, timeout=30):
    return subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
         'Bypass', '-File', str(script), *map(str, args)],
        cwd=cwd, capture_output=True, timeout=timeout,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )


@unittest.skipUnless(os.name == 'nt', 'Windows launcher integration')
class ShortcutTests(unittest.TestCase):
    def test_missing_exe_error_is_utf8_korean(self):
        with tempfile.TemporaryDirectory() as directory:
            result = powershell(PROJECT / 'create_shortcut.ps1', '-Root', directory)
        self.assertEqual(result.returncode, 1)
        text = result.stderr.decode('utf-8')
        self.assertIn('AI Usage.exe를 찾지 못했습니다.', text)
        self.assertNotIn('CategoryInfo', text)

    def test_system_failure_is_utf8_and_ui_keeps_a_short_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / u.LAUNCHER_EXE).write_bytes(b'MZ')
            desktop = root / 'not-a-directory'
            desktop.write_text('occupied')
            result = powershell(PROJECT / 'create_shortcut.ps1', '-Root', root,
                                '-Desktop', desktop)
            self.assertEqual(result.returncode, 1)
            detail = result.stderr.decode('utf-8')
            self.assertTrue(detail.strip())
            self.assertNotIn('CategoryInfo', detail)
            with patch.object(u, 'APP_DIR', root), patch.object(u, 'save_install_root') as save:
                with self.assertRaisesRegex(RuntimeError, '^바탕화면 바로가기를 만들지 못했습니다') as error:
                    u.create_desktop_shortcut(root, desktop)
                save.assert_not_called()
            self.assertNotIn('CategoryInfo', str(error.exception))
            self.assertIn('shortcut failed:', (root / 'launch.log').read_text(encoding='utf-8'))
            self.assertEqual(desktop.read_text(), 'occupied')
            with patch.object(u, 'log_launch', side_effect=OSError('log unavailable')):
                with self.assertRaisesRegex(RuntimeError, '^바탕화면 바로가기를 만들지 못했습니다'):
                    u.create_desktop_shortcut(root, desktop)


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell Python selection')
class PythonSelectionTests(unittest.TestCase):
    def select(self, minimum, missing_tk=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if missing_tk:
                (root / 'tkinter.py').write_text("raise ImportError('no tkinter')\n")
            script = root / 'select.ps1'
            script.write_text(r'''
param($Source, $Candidate, $Minimum)
$ErrorActionPreference = 'Stop'
$MinPython = [version]$Minimum
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$null)
$wanted = @('Invoke-PythonText', 'Get-PythonArgs', 'Test-ReadyPython', 'Get-PyLauncherPython', 'Get-ReadyPython')
foreach ($node in $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $false)) {
    if ($node.Name -in $wanted) { Invoke-Expression $node.Extent.Text }
}
# Keep the real validator/process execution. Stub only environment discovery
# and py.exe's initial response, so unsupported candidates must be rejected.
$invoke = ${function:Invoke-PythonText}
function Invoke-PythonText {
    param($Exe, $Arguments)
    if ($Arguments[0] -eq '-3') { return $Candidate }
    & $invoke -Exe $Exe -Arguments $Arguments
}
function Refresh-Path {}
function Get-Command { param($Name, $CommandType, [switch]$All, $ErrorAction)
    [pscustomobject]@{Source=$Candidate}
}
function Get-InstalledPython { return $null }
function Get-RawPythonHits { return @() }
function Write-LaunchLog { param($Message) }
$chosen = Get-ReadyPython
@{selected=($chosen -eq $Candidate)} | ConvertTo-Json -Compress
''', encoding='utf-8-sig')
            result = powershell(script, PROJECT / 'setup_and_run.ps1', sys.executable,
                                minimum, cwd=root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['selected']

    def test_py_candidate_obeys_minimum_and_tk_requirement(self):
        self.assertTrue(self.select('3.9'))
        self.assertFalse(self.select('99.0'))
        self.assertFalse(self.select('3.9', missing_tk=True))


@unittest.skipUnless(os.name == 'nt', 'Compiled Windows launcher')
class LauncherTests(unittest.TestCase):
    def test_cached_runtime_validation_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "한글 folder's widget"
            root.mkdir()
            probe = root / 'host-check.vbs'
            probe.write_text('WScript.Quit 0\n')
            host_ready = subprocess.run(['cscript.exe', '//Nologo', '//T:5', str(probe)],
                capture_output=True, timeout=10).returncode == 0
            script = root / 'runtime-test.ps1'
            script.write_text(r'''
param($Launcher, $SetupSource, $VbsSource, [switch]$SkipVbs)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$Here = $PSScriptRoot
$Widget = Join-Path $Here 'usage_widget.py'
$env:APPDATA = Join-Path $Here 'appdata'
$appDir = Join-Path $env:APPDATA 'AiUsageWidget'
New-Item -ItemType Directory -Path $appDir -Force | Out-Null
$cache = Join-Path $appDir 'runtime-v1.txt'
$fakeDir = Join-Path $Here 'Python runtime'
New-Item -ItemType Directory -Path $fakeDir | Out-Null
$source = Join-Path $fakeDir 'fake.cs'
@'
using System;
using System.IO;
using System.Diagnostics;
using System.Threading;
class Fake {
    static int Main(string[] args) {
        string root = Environment.CurrentDirectory;
        File.AppendAllText(Path.Combine(root, "invocations.txt"), "called\n");
        File.WriteAllLines(Path.Combine(root, "arguments.txt"), args);
        string mode = File.ReadAllText(Path.Combine(root, "mode.txt")).Trim();
        if (mode == "wait") {
            File.WriteAllText(Path.Combine(root, "child.pid"), Process.GetCurrentProcess().Id.ToString());
            Thread.Sleep(10000);
        }
        return mode == "fail" ? 7 : 0;
    }
}
'@ | Set-Content -LiteralPath $source
$pythonw = Join-Path $fakeDir 'pythonw.exe'
$python = Join-Path $fakeDir 'python.exe'
& "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe "/out:$pythonw" $source
if ($LASTEXITCODE -ne 0) { throw 'fake runtime compile failed' }
Copy-Item -LiteralPath $pythonw -Destination $python
[IO.File]::WriteAllText($Widget, '# fixture')
[IO.File]::WriteAllText((Join-Path $Here 'mode.txt'), 'ok')
$setup = Join-Path $Here 'setup_and_run.ps1'
[IO.File]::WriteAllText($setup, '[IO.File]::AppendAllText((Join-Path $PSScriptRoot "fallback.txt"), "fallback`n")')
$ast = [System.Management.Automation.Language.Parser]::ParseFile($SetupSource, [ref]$null, [ref]$null)
foreach ($node in $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $false)) {
    if ($node.Name -in @('Save-RuntimeCache', 'Write-LaunchLog')) { Invoke-Expression $node.Extent.Text }
}
$type = ([Reflection.Assembly]::LoadFile($Launcher)).GetType('Program')
$flags = [Reflection.BindingFlags]'NonPublic,Static'
$fast = $type.GetMethod('TryStartCached', $flags)
$start = $type.GetMethod('StartWidget', $flags)
function Cached { $fast.Invoke($null, @([string]$Here, [string]$appDir)) }
function Save { Save-RuntimeCache $python $pythonw 424242 }
$missing = Cached
Save
$notReadySkipped = -not (Test-Path -LiteralPath $cache)
[IO.File]::WriteAllLines((Join-Path $appDir 'widget.instance'), @('424242', '0'))
Save
$noWindowSkipped = -not (Test-Path -LiteralPath $cache)
[IO.File]::WriteAllLines((Join-Path $appDir 'widget.instance'), @('424243', '123'))
Save
$wrongChildSkipped = -not (Test-Path -LiteralPath $cache)
[IO.File]::WriteAllLines((Join-Path $appDir 'widget.instance'), @('424242', '123'))
Save
$valid = Cached
if (-not $valid) {
    throw ((Get-Content -LiteralPath $cache -Raw -ErrorAction SilentlyContinue) +
           (Get-Content -LiteralPath (Join-Path $appDir 'launch.log') -Raw -ErrorAction SilentlyContinue))
}
$arguments = [IO.File]::ReadAllLines((Join-Path $Here 'arguments.txt'))
[IO.File]::Delete((Join-Path $Here 'invocations.txt'))
$rejected = @()
foreach ($file in @($Widget, $setup, $python, $pythonw)) {
    Save
    [IO.File]::SetLastWriteTimeUtc($file, [IO.File]::GetLastWriteTimeUtc($file).AddSeconds(5))
    $rejected += -not (Cached)
}
Save
$lines = [IO.File]::ReadAllLines($cache)
$lines[1] = Join-Path $Here 'old-folder'
[IO.File]::WriteAllLines($cache, $lines)
$moved = -not (Cached)
[IO.File]::WriteAllText($cache, 'invalid cache')
$malformed = -not (Cached)
Save
[IO.File]::Delete($python)
$deleted = -not (Cached)
Copy-Item -LiteralPath $pythonw -Destination $python
$noInvalidInvocation = -not (Test-Path -LiteralPath (Join-Path $Here 'invocations.txt'))
Save
[IO.File]::WriteAllText((Join-Path $Here 'mode.txt'), 'wait')
try {
    $running = Cached
    $childId = [int][IO.File]::ReadAllText((Join-Path $Here 'child.pid'))
    $stillRunning = $null -ne (Get-Process -Id $childId -ErrorAction SilentlyContinue)
} finally {
    if ($childId) { Stop-Process -Id $childId -ErrorAction SilentlyContinue }
}
[IO.File]::WriteAllText((Join-Path $Here 'mode.txt'), 'fail')
$null = $start.Invoke($null, @([string]$Here, [string]$appDir))
$fallback = Join-Path $Here 'fallback.txt'
for ($i = 0; $i -lt 100 -and -not (Test-Path -LiteralPath $fallback); $i++) { Start-Sleep -Milliseconds 50 }
$recovered = (Test-Path -LiteralPath $fallback) -and -not (Test-Path -LiteralPath $cache)
$fallbackCount = [IO.File]::ReadAllLines($fallback).Length
[IO.File]::Delete($fallback)
[IO.File]::WriteAllText((Join-Path $Here 'mode.txt'), 'ok')
Save
$null = $start.Invoke($null, @([string]$Here, [string]$appDir))
Start-Sleep -Milliseconds 200
$zeroNoFallback = -not (Test-Path -LiteralPath $fallback)
# A missing cache uses setup directly too.
[IO.File]::Delete($cache)
$null = $start.Invoke($null, @([string]$Here, [string]$appDir))
for ($i = 0; $i -lt 100 -and -not (Test-Path -LiteralPath $fallback); $i++) { Start-Sleep -Milliseconds 50 }
$uncachedFallback = Test-Path -LiteralPath $fallback
[IO.File]::Delete($cache)
New-Item -ItemType Directory -Path $cache | Out-Null
Save
$writeFailureLogged = [IO.File]::ReadAllText((Join-Path $appDir 'launch.log')).Contains('runtime cache unavailable:')
# Exercise the actual logon VBS with a harmless compiled launcher.
if (-not $SkipVbs) {
$vbs = Join-Path $Here 'start_usage_widget.vbs'
Copy-Item -LiteralPath $VbsSource -Destination $vbs
$entryExe = Join-Path $Here 'AI Usage.exe'
Copy-Item -LiteralPath $pythonw -Destination $entryExe
$argumentFile = Join-Path $Here 'arguments.txt'
[IO.File]::Delete($argumentFile)
$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
$child = Start-Process -FilePath $wscript -ArgumentList ('//B //T:5 "' + $vbs + '"') -WindowStyle Hidden -Wait -PassThru
for ($i = 0; $i -lt 100 -and -not (Test-Path -LiteralPath $argumentFile); $i++) { Start-Sleep -Milliseconds 50 }
$vbsUsesStartup = [IO.File]::ReadAllText($argumentFile).Trim() -eq '--startup'
$vbsFallback = @()
[IO.File]::Delete($fallback)
[IO.File]::Delete($entryExe)
$child = Start-Process -FilePath $wscript -ArgumentList ('//B //T:5 "' + $vbs + '"') -WindowStyle Hidden -Wait -PassThru
for ($i = 0; $i -lt 100 -and -not (Test-Path -LiteralPath $fallback); $i++) { Start-Sleep -Milliseconds 50 }
$vbsFallback = Test-Path -LiteralPath $fallback
}
@{missing=$missing;valid=$valid;arguments=$arguments;rejected=$rejected;moved=$moved;
  notReadySkipped=$notReadySkipped;noWindowSkipped=$noWindowSkipped;wrongChildSkipped=$wrongChildSkipped;
  malformed=$malformed;deleted=$deleted;noInvalidInvocation=$noInvalidInvocation;
  running=$running;stillRunning=$stillRunning;recovered=$recovered;fallbackCount=$fallbackCount;
  zeroNoFallback=$zeroNoFallback;uncachedFallback=$uncachedFallback;writeFailureLogged=$writeFailureLogged;
  vbsUsesStartup=$vbsUsesStartup;vbsFallback=$vbsFallback} | ConvertTo-Json -Compress
''', encoding='utf-8-sig')
            result = powershell(script, PROJECT / 'AI Usage.exe', PROJECT / 'setup_and_run.ps1',
                                PROJECT / 'start_usage_widget.vbs', *([] if host_ready else ['-SkipVbs']))
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertFalse(data['missing'])
            self.assertEqual(data['arguments'], ['-B', str(root / 'usage_widget.py')])
            self.assertEqual(data['rejected'], [True] * 4)
            self.assertEqual(data['fallbackCount'], 1)
            if host_ready:
                self.assertTrue(data['vbsFallback'])
                self.assertTrue(data['vbsUsesStartup'])
            for key in ('valid', 'notReadySkipped', 'noWindowSkipped', 'wrongChildSkipped',
                        'moved', 'malformed', 'deleted', 'noInvalidInvocation',
                        'running', 'stillRunning', 'recovered', 'zeroNoFallback',
                        'uncachedFallback', 'writeFailureLogged'):
                self.assertTrue(data[key], key)

    def test_shortcut_failures_are_nonfatal_and_timeout_stops_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'check.ps1'
            script.write_text(r'''
param($Launcher)
$ErrorActionPreference = 'Stop'
$assembly = [Reflection.Assembly]::LoadFile($Launcher)
$method = $assembly.GetType('Program').GetMethod('TryCreateShortcut', [Reflection.BindingFlags]'NonPublic,Static')
function Attempt($Name, $Code) {
    $root = Join-Path $PSScriptRoot $Name
    New-Item -ItemType Directory $root | Out-Null
    if ($Code) { [IO.File]::WriteAllText((Join-Path $root 'create_shortcut.ps1'), $Code) }
    $success = $method.Invoke($null, @([string]$root, [string]$root))
    $log = Join-Path $root 'launch.log'
    @{success=$success; logged=(Test-Path $log)}
}
$missing = Attempt 'missing' ''
$failure = Attempt 'failure' 'exit 1'
$success = Attempt 'success' 'exit 0'
$timeout = Attempt 'timeout' '$PID | Set-Content (Join-Path $PSScriptRoot "child.pid"); Start-Sleep -Seconds 60'
$childId = [int](Get-Content (Join-Path $PSScriptRoot 'timeout/child.pid'))
$child = Get-Process -Id $childId -ErrorAction SilentlyContinue
if ($child) { $null = $child.WaitForExit(3000) }
$stopped = $null -eq (Get-Process -Id $childId -ErrorAction SilentlyContinue)
if (-not $stopped) { Stop-Process -Id $childId }
@{missing=$missing; failure=$failure; success=$success; timeout=$timeout; stopped=$stopped} | ConvertTo-Json -Compress
''', encoding='utf-8-sig')
            result = powershell(script, PROJECT / 'AI Usage.exe')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        for kind in ('missing', 'failure', 'timeout'):
            self.assertEqual(data[kind], {'success': False, 'logged': True}, kind)
        self.assertEqual(data['success'], {'success': True, 'logged': False})
        self.assertTrue(data['stopped'])


@unittest.skipUnless(os.name == 'nt', 'Windows Script Host')
class LogonScriptLogTests(unittest.TestCase):
    def test_logon_script_logs_utf8_safe_ascii(self):
        # launch.log is read as UTF-8; the VBS text stream writes the ANSI code
        # page, so a localized date or a Korean folder name must not reach it raw.
        directory = Path(tempfile.mkdtemp())
        # What the script starts may still hold the folder for a moment.
        self.addCleanup(shutil.rmtree, directory, True)
        root = directory / '한글 위젯'
        root.mkdir()
        (root / 'start_usage_widget.vbs').write_bytes((PROJECT / 'start_usage_widget.vbs').read_bytes())
        # A harmless stand-in for the launcher, so the script takes its exe branch.
        (root / 'AI Usage.exe').write_bytes(Path(os.environ['WINDIR'], 'System32', 'whoami.exe').read_bytes())
        appdata = directory / 'appdata'
        appdata.mkdir()  # %APPDATA% always exists; the script creates only its own folder
        env = dict(os.environ, APPDATA=str(appdata))
        done = subprocess.run(['cscript.exe', '//Nologo', '//T:10', str(root / 'start_usage_widget.vbs')],
                              env=env, capture_output=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        text = (appdata / 'AiUsageWidget' / 'launch.log').read_bytes().decode('ascii')
        self.assertRegex(text, r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d vbs start ')
        # The Korean folder name, escaped rather than written in the ANSI code page.
        self.assertIn(r'\uD55C\uAE00 \uC704\uC82F', text)
