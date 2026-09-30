"""The release version guard, taken from publish_update.ps1 itself.

The guard used to be tested through a separate copy (release_policy.ps1) that
publishing never ran. The cases now run against the function the release
script actually defines, all in one PowerShell start; the end-to-end paths
(remote feed, gh listing, build order) are covered by tests/test_publish.py.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import integration

PROJECT = Path(__file__).resolve().parent.parent
PUBLISH = PROJECT / 'publish_update.ps1'

CASES = {
    'equal version': ('3.4.1', ['v3.4.1'], False),
    'lower version': ('3.4.0', ['3.4.1'], False),
    'higher patch': ('3.4.2', ['3.4.1'], True),
    'higher minor': ('3.5.0', ['3.4.1'], True),
    'higher major': ('4.0.0', ['3.4.1'], True),
    'compared as numbers, not text': ('3.10.0', ['3.9.9'], True),
    'a longer patch is not newer by text': ('3.9.10', ['3.10.0'], False),
    'newer than every release': ('3.11.12', ['v3.11.11', 'v3.11.10'], True),
    'equal to one of several releases': ('3.11.11', ['v3.11.10', 'v3.11.11'], False),
    'target that is not x.y.z': ('3.4', [], False),
    'published version that cannot be read': ('3.4.2', ['latest'], False),
}

SCRIPT = r'''
param($Publish, $CasesJson)
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Publish, [ref]$null, [ref]$null)
$guard = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                        $n.Name -eq 'Assert-NewReleaseVersion' }, $true) | Select-Object -First 1
if (-not $guard) { throw 'Assert-NewReleaseVersion not found in publish_update.ps1' }
Invoke-Expression $guard.Extent.Text
$results = @{}
foreach ($case in (ConvertFrom-Json $CasesJson)) {
    try {
        Assert-NewReleaseVersion -Version $case.version -PublishedVersions @($case.published)
        $results[$case.name] = 'accept'
    } catch {
        $results[$case.name] = 'reject'
    }
}
$results | ConvertTo-Json -Compress
'''


@integration('runs the release script')
class VersionGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shell = shutil.which('powershell') or shutil.which('pwsh')
        if not shell:
            raise unittest.SkipTest('PowerShell is required for publish script tests')
        cases = [{'name': name, 'version': version, 'published': published}
                 for name, (version, published, _) in CASES.items()]
        # Bypass keeps the guard testable where the default execution policy
        # would refuse to run a local script.
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / 'version-guard.ps1'
            script.write_text(SCRIPT, encoding='utf-8-sig')
            done = subprocess.run(
                [shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script),
                 str(PUBLISH), json.dumps(cases)],
                capture_output=True, encoding='utf-8', errors='replace', timeout=120,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if done.returncode != 0:
            raise AssertionError(done.stdout + done.stderr)
        cls.results = json.loads(done.stdout.strip().splitlines()[-1])

    def test_every_case(self):
        for name, (_, _, accepted) in CASES.items():
            with self.subTest(name):
                self.assertEqual(self.results.get(name), 'accept' if accepted else 'reject')


NOTES_SCRIPT = r'''
param($Publish, $SectionsJson)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Publish, [ref]$null, [ref]$null)
$notes = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                        $n.Name -eq 'Get-LatestNotes' }, $true) | Select-Object -First 1
if (-not $notes) { throw 'Get-LatestNotes not found in publish_update.ps1' }
Invoke-Expression $notes.Extent.Text
$results = @{}
foreach ($case in (ConvertFrom-Json $SectionsJson)) {
    $results[$case.name] = Get-LatestNotes -Section $case.section -Version '9.9.9'
}
$results | ConvertTo-Json -Compress
'''

# Sections as the release script reads them from a CRLF checkout.
SECTIONS = {
    'first sentences of the first three items': (
        '### Changed\r\n'
        '- 빛이 더 부드럽게 흐릅니다. 초당 30번 그리던 것을 60번 그립니다.\r\n'
        '- 3.11.10에서 약 1.4ms 걸리던 확인을 줄였습니다. 세션 파일 3개 기준입니다.\r\n'
        '\r\n### Fixed\r\n'
        '- `claude.cmd` 조회가 끝나지 않던 문제를 고쳤습니다. node까지 종료합니다.\r\n'
        '- 네 번째 항목은 나가지 않습니다.\r\n',
        '빛이 더 부드럽게 흐릅니다. / 3.11.10에서 약 1.4ms 걸리던 확인을 줄였습니다. / '
        '`claude.cmd` 조회가 끝나지 않던 문제를 고쳤습니다.',
    ),
    'an item without a period stays whole': ('- 마침표 없는 안내\r\n', '마침표 없는 안내'),
    'no items': ('### Changed\r\n', 'AI Usage 9.9.9'),
}


@integration('runs the release script')
class LatestNotesTests(unittest.TestCase):
    """The update dialog's lines come from Get-LatestNotes in publish_update.ps1."""

    @classmethod
    def setUpClass(cls):
        shell = shutil.which('powershell') or shutil.which('pwsh')
        if not shell:
            raise unittest.SkipTest('PowerShell is required for publish script tests')
        cases = [{'name': name, 'section': section} for name, (section, _) in SECTIONS.items()]
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / 'latest-notes.ps1'
            script.write_text(NOTES_SCRIPT, encoding='utf-8-sig')
            done = subprocess.run(
                [shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script),
                 str(PUBLISH), json.dumps(cases)],
                capture_output=True, encoding='utf-8', errors='replace', timeout=120,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if done.returncode != 0:
            raise AssertionError(done.stdout + done.stderr)
        cls.results = json.loads(done.stdout.strip().splitlines()[-1])

    def test_every_section(self):
        for name, (_, expected) in SECTIONS.items():
            with self.subTest(name):
                self.assertEqual(self.results.get(name), expected)


class PublishOrderTests(unittest.TestCase):
    def test_publish_guard_precedes_build_and_has_no_overwrite_path(self):
        # The version is verified before anything is built, and there is no
        # path that overwrites a release.
        script = PUBLISH.read_text(encoding='utf-8-sig')
        # Anchor on the build call itself; the file name also appears in the
        # list of packaged sources that the source guard checks.
        build = script.index("& (Join-Path $project 'build_launcher.ps1')")
        self.assertLess(script.index('Assert-NewReleaseVersion -Version'), build)
        self.assertLess(script.index('Assert-PublishSourceCommitted -Project'), build)
        self.assertNotIn('--clobber', script)
        self.assertNotIn('release upload', script)
        self.assertNotIn('release edit', script)
        self.assertIn('release create', script)
        self.assertIn('already exists; artifacts will not be overwritten', script)


if __name__ == '__main__':
    unittest.main()
