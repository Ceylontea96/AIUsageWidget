"""Exercise Windows launch failures using isolated scripts and directories."""
import json
import os
from pathlib import Path
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
