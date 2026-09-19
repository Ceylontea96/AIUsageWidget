"""Exercise release guards in an isolated fixture: no network, builds or publish."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == 'nt', 'PowerShell release script is Windows-only')
class PublishGuardTests(unittest.TestCase):
    def guarded_attempt(self, version, published, code=0, feed_version=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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

    def test_every_runtime_module_is_packaged(self):
        script = Path('publish_update.ps1').read_text(encoding='utf-8')
        missing = sorted(name for name in self.runtime_modules()
                         if f"'{name}.py'" not in script)
        self.assertEqual(missing, [], f'not in publish_update.ps1 copy list: {missing}')
