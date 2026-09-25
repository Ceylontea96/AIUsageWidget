"""Exercise release guards in an isolated fixture: no network, builds or publish."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


GIT_ENV = {'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
           'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}


def git(root, *args):
    return subprocess.run(['git', '-c', 'safe.directory=*', *args], cwd=root,
                          env={**os.environ, **GIT_ENV}, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def publish_clone(root, origin):
    """Make `root` a clean clone whose HEAD is exactly origin/main."""
    subprocess.run(['git', 'init', '--bare', '-q', '-b', 'main', str(origin)],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', 'fixture')
    git(root, 'remote', 'add', 'origin', str(origin))
    git(root, 'push', '-q', '-u', 'origin', 'main')


@unittest.skipUnless(os.name == 'nt', 'PowerShell release script is Windows-only')
class PublishGuardTests(unittest.TestCase):
    def guarded_attempt(self, version, published, code=0, feed_version=None, after_commit=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'work'
            root.mkdir()
            shutil.copyfile('publish_update.ps1', root / 'publish_update.ps1')
            (root / 'updater.py').write_text("APP_VERSION = '" + version + "'\n")
            (root / 'feed_url.txt').write_text('https://example.invalid/latest.json' if feed_version else '')
            (root / 'build_launcher.ps1').write_text("throw 'BUILD_REACHED'\n")
            (root / 'gh.cmd').write_text('@echo off\n' + ''.join('echo ' + tag + '\n' for tag in published) + 'exit /b ' + str(code) + '\n')
            env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']}
            entry=root / 'test_entry.ps1'
            stub = ("function Invoke-RestMethod { [CmdletBinding()] param([string]$Uri) "
                    "[pscustomobject]@{version='" + feed_version + "'} }\n") if feed_version else ''
            entry.write_text(stub + '& "$PSScriptRoot/publish_update.ps1" -GitHub\n')
            publish_clone(root, Path(directory) / 'origin.git')
            if after_commit:
                after_commit(root)
            result = subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass',
                '-File',str(entry)],cwd=root,env=env,
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=15)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse((root / 'latest.json').exists())
            self.assertFalse((root / 'dist').exists())
            return result.stdout.decode('utf-8','replace')

    def test_same_version_never_reaches_build(self):
        output=self.guarded_attempt('3.4.2',['v3.4.2','v3.4.1'])
        self.assertIn('Refusing same/older',output)
        self.assertNotIn('BUILD_REACHED',output)

    def test_older_version_never_reaches_build(self):
        output=self.guarded_attempt('3.4.2',['v3.5.0'])
        self.assertIn('Refusing same/older',output)
        self.assertNotIn('BUILD_REACHED',output)

    def test_remote_failure_is_fail_closed(self):
        output=self.guarded_attempt('3.4.2',[],1)
        self.assertIn('Cannot verify published',output)
        self.assertNotIn('BUILD_REACHED',output)

    def test_new_version_can_reach_build(self):
        output=self.guarded_attempt('3.4.2',['v3.4.1'])
        self.assertIn('BUILD_REACHED',output)

    def test_latest_feed_also_blocks_same_version(self):
        output=self.guarded_attempt('3.4.2',['v3.4.1'],feed_version='3.4.2')
        self.assertIn('Refusing same/older',output)
        self.assertNotIn('BUILD_REACHED',output)

    def test_uncommitted_change_never_reaches_build(self):
        def edit(root):
            (root / 'updater.py').write_text("APP_VERSION = '3.4.2'\n# local edit\n")
        output = self.guarded_attempt('3.4.2', ['v3.4.1'], after_commit=edit)
        self.assertIn('Uncommitted changes', output)
        self.assertNotIn('BUILD_REACHED', output)

    def test_unpushed_commit_never_reaches_build(self):
        def commit_locally(root):
            (root / 'CHANGELOG.md').write_text('local only\n')
            git(root, 'add', 'CHANGELOG.md')
            git(root, 'commit', '-q', '-m', 'not pushed')
        output = self.guarded_attempt('3.4.2', ['v3.4.1'], after_commit=commit_locally)
        self.assertIn('is not origin/main', output)
        self.assertNotIn('BUILD_REACHED', output)

    def test_untracked_packaged_file_never_reaches_build(self):
        def add_module(root):
            (root / 'providers.py').write_text('# never committed\n')
        output = self.guarded_attempt('3.4.2', ['v3.4.1'], after_commit=add_module)
        self.assertIn('would ship without being committed', output)
        self.assertIn('providers.py', output)
        self.assertNotIn('BUILD_REACHED', output)

    def test_ignored_packaged_file_never_reaches_build(self):
        def hide_module(root):
            (root / '.gitignore').write_text('providers.py\n')
            git(root, 'add', '.gitignore')
            git(root, 'commit', '-q', '-m', 'ignore it')
            git(root, 'push', '-q')
            (root / 'providers.py').write_text('# ignored, still packaged\n')
        output = self.guarded_attempt('3.4.2', ['v3.4.1'], after_commit=hide_module)
        self.assertIn('would ship without being committed', output)
        self.assertNotIn('BUILD_REACHED', output)

    def test_untracked_files_outside_the_archive_are_allowed(self):
        def scratch(root):
            (root / 'design_reference').mkdir()
            (root / 'design_reference' / 'mock.html').write_text('<p>scratch</p>\n')
            # A nested .bat must not match the top-level *.bat package rule.
            (root / 'old_stage').mkdir()
            (root / 'old_stage' / 'old.bat').write_text('rem previous stage\n')
        output = self.guarded_attempt('3.4.2', ['v3.4.1'], after_commit=scratch)
        self.assertIn('BUILD_REACHED', output)

    def test_release_is_tagged_on_the_checked_commit(self):
        text = Path('publish_update.ps1').read_text(encoding='utf-8')
        create = [line for line in text.splitlines() if '& $gh release create' in line]
        self.assertEqual(len(create), 1)
        self.assertIn('--target $sourceCommit', create[0])
        # The guard runs before any build step can touch tracked files.
        self.assertLess(text.index('Assert-PublishSourceCommitted -Project'),
                        text.index("build_launcher.ps1')"))

    def test_no_overwrite_path_and_runtime_modules_packaged(self):
        text=Path('publish_update.ps1').read_text(encoding='utf-8')
        self.assertNotIn('--clobber',text)
        self.assertNotIn('release edit',text)
        self.assertNotIn('release upload',text)
        for filename in ('claude_integration.py','claude_bridge.py','additional_ui.py','polling.py'):
            self.assertIn("'"+filename+"'",text)


if __name__ == '__main__':
    unittest.main()


class PackagedModuleTests(unittest.TestCase):
    """The zip must carry every module the widget imports at runtime.

    A module that exists locally but is left out of publish_update.ps1's copy
    list produces a zip that only fails once a user unpacks and runs it.
    """

    def runtime_modules(self):
        import ast
        project = Path('.')
        local = {path.stem for path in project.glob('*.py')}
        seen, queue = set(), ['usage_widget', 'poll_worker']
        while queue:
            name = queue.pop()
            if name in seen or name not in local:
                continue
            seen.add(name)
            tree = ast.parse((project / f'{name}.py').read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    queue.extend(alias.name.split('.')[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    queue.append(node.module.split('.')[0])
        return seen

    def package_files(self):
        script = Path('publish_update.ps1').read_text(encoding='utf-8')
        copy = re.search(r'\$copy\s*=\s*@\((.*?)\)', script, re.S)
        self.assertIsNotNone(copy, 'publish_update.ps1 must declare its copy list')
        return re.findall(r"'([^']+)'", copy.group(1))

    def test_every_runtime_module_is_packaged(self):
        files = self.package_files()
        missing = sorted(name for name in self.runtime_modules()
                         if f'{name}.py' not in files)
        self.assertEqual(missing, [], f'not in publish_update.ps1 copy list: {missing}')

    def test_packaged_python_modules_import_without_the_working_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            for filename in self.package_files():
                if filename.endswith('.py'):
                    shutil.copyfile(filename, Path(directory) / filename)
            result = subprocess.run(
                [sys.executable, '-B', '-I', '-c',
                 'import sys; sys.path.insert(0, sys.argv[1]); import usage_widget, poll_worker', directory],
                cwd=directory, capture_output=True, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
