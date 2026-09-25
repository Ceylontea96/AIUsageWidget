import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import updater as u


def zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buffer.getvalue()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class IsolatedTemp:
    """Point tempfile at a scratch folder so cleanup never touches the real one."""

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = tempfile.tempdir
        tempfile.tempdir = self.directory.name
        return Path(self.directory.name)

    def __exit__(self, *exc):
        tempfile.tempdir = self.previous
        self.directory.cleanup()


def write(root, files):
    for name, text in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')


class UpdaterTests(unittest.TestCase):
    def test_check_every_is_thirty_minutes(self):
        self.assertEqual(u.CHECK_EVERY, 30 * 60)

    def test_version_compare(self):
        self.assertTrue(u.is_newer('3.1.1', '3.1.0'))
        self.assertFalse(u.is_newer('3.1.0', '3.1.0'))
        self.assertFalse(u.is_newer('3.0.9', '3.1.0'))
        self.assertEqual(u.parse_version('3.1'), (3, 1, 0))

    def test_feed_url_ignores_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'feed_url.txt'
            path.write_text('# comment\nhttps://example.com/latest.json\n', encoding='utf-8')
            self.assertEqual(u.load_feed_url(directory), 'https://example.com/latest.json')
            path.write_text('# only comment\n', encoding='utf-8')
            self.assertEqual(u.load_feed_url(directory), '')

    def test_pending_update_requires_newer_https_zip(self):
        feed = 'https://example.com/latest.json'
        self.assertIsNone(u.pending_update({'version': '9.0.0'}))
        self.assertIsNone(u.pending_update({'version': '0.0.1', 'zip': 'https://example.com/a.zip'}, local='3.1.0'))
        got = u.pending_update({'version': '9.0.0'}, feed=feed)
        self.assertEqual(got['zip'], 'https://example.com/AIUsageWidget.zip')
        self.assertIsNone(u.pending_update({'version': '9.0.0', 'zip': 'http://example.com/a.zip'}))

    def test_safe_extract_skips_zip_slip(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'a.zip'
            dest = Path(directory) / 'out'
            with zipfile.ZipFile(archive, 'w') as zf:
                zf.writestr('usage_widget.py', 'ok')
                zf.writestr('../escape.py', 'bad')
            u.safe_extract(archive, dest)
            self.assertTrue((dest / 'usage_widget.py').is_file())
            self.assertFalse((Path(directory) / 'escape.py').is_file())

    def test_fetch_latest_rejects_non_https(self):
        self.assertIsNone(u.fetch_latest('http://example.com/latest.json'))

    def test_update_confirm_text_lists_short_notes(self):
        text = u.update_confirm_text({
            'version': '9.9.9',
            'notes': '체크표시를 흰색으로 바꿈 / 초록 `↑ 업데이트` 배지',
        })
        self.assertIn('새 버전 9.9.9', text)
        self.assertIn('· 체크표시를 흰색으로 바꿈', text)
        self.assertIn('· 초록 ↑ 업데이트 배지', text)
        self.assertTrue(text.endswith('이 파일을 받고 위젯을 다시 시작할까요?'))
        self.assertEqual(u.note_lines('a / b / c / d'), ['a', 'b', 'c'])
        self.assertNotIn('·', u.update_confirm_text({'version': '1.0.0'}))

    def test_launch_after_update_prefers_exe(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertEqual(u.launch_after_update(target).name, 'start_usage_widget.vbs')
            (target / u.LAUNCHER_EXE).write_bytes(b'MZ')
            self.assertEqual(u.launch_after_update(target).name, u.LAUNCHER_EXE)


class FeedTrustTests(unittest.TestCase):
    FEED = 'https://github.com/Owner/Repo/releases/latest/download/latest.json'

    def test_archive_must_come_from_the_feed_repository(self):
        same = 'https://github.com/owner/repo/releases/download/v9.0.0/AIUsageWidget.zip'
        other_repo = 'https://github.com/owner/fork/releases/download/v9.0.0/AIUsageWidget.zip'
        other_host = 'https://example.com/AIUsageWidget.zip'
        self.assertEqual(u.pending_update({'version': '9.0.0', 'zip': same}, feed=self.FEED)['zip'], same)
        self.assertIsNone(u.pending_update({'version': '9.0.0', 'zip': other_repo}, feed=self.FEED))
        self.assertIsNone(u.pending_update({'version': '9.0.0', 'zip': other_host}, feed=self.FEED))

    def test_checksum_is_passed_on_and_malformed_ones_rejected(self):
        digest = 'A' * 64
        got = u.pending_update({'version': '9.0.0', 'sha256': digest}, feed=self.FEED)
        self.assertEqual(got['sha256'], digest.lower())
        self.assertEqual(u.pending_update({'version': '9.0.0'}, feed=self.FEED)['sha256'], '')
        self.assertIsNone(u.pending_update({'version': '9.0.0', 'sha256': 'abc'}, feed=self.FEED))


class DownloadTests(unittest.TestCase):
    def download(self, payload, **kwargs):
        with patch.object(u, '_open', return_value=FakeResponse(payload)):
            return u.download_and_stage('https://example.com/AIUsageWidget.zip', **kwargs)

    def test_matching_checksum_is_staged(self):
        payload = zip_bytes({'usage_widget.py': 'ok'})
        with IsolatedTemp():
            staged = self.download(payload, sha256=hashlib.sha256(payload).hexdigest())
            self.assertEqual((staged / 'usage_widget.py').read_text(), 'ok')

    def test_wrong_checksum_is_refused_and_cleaned_up(self):
        payload = zip_bytes({'usage_widget.py': 'ok'})
        with IsolatedTemp() as temp:
            with self.assertRaisesRegex(RuntimeError, '배포된 파일과 다릅니다'):
                self.download(payload, sha256='0' * 64)
            self.assertEqual(list(temp.glob(u.STAGE_PREFIX + '*')), [])

    def test_broken_archive_is_explained(self):
        with IsolatedTemp():
            with self.assertRaisesRegex(RuntimeError, '손상'):
                self.download(b'not a zip')

    def test_unpacked_size_is_limited(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'big.zip'
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('usage_widget.py', 'x' * 5000)
            with self.assertRaisesRegex(RuntimeError, '너무 큽니다'):
                u.safe_extract(archive, Path(directory) / 'out', limit=1000)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.source, self.target, self.backup = (self.root / name for name in ('source', 'target', 'backup'))
        for path in (self.source, self.target, self.backup):
            path.mkdir()

    def tearDown(self):
        self._dir.cleanup()

    def test_overwrites_adds_and_removes_only_obsolete_package_files(self):
        write(self.target, {'usage_widget.py': 'old', 'gone.py': 'old', 'mine.txt': 'user file',
                            u.MANIFEST: json.dumps({'files': ['usage_widget.py', 'gone.py']})})
        write(self.source, {'usage_widget.py': 'new', 'assets/icons/new.png': 'png'})
        removed = u.apply_update(self.source, self.target, self.backup)
        self.assertEqual(removed, 1)
        self.assertEqual((self.target / 'usage_widget.py').read_text(), 'new')
        self.assertEqual((self.target / 'assets/icons/new.png').read_text(), 'png')
        self.assertFalse((self.target / 'gone.py').exists())
        # Never listed by a package, so never ours to remove.
        self.assertEqual((self.target / 'mine.txt').read_text(), 'user file')
        written = json.loads((self.target / u.MANIFEST).read_text())['files']
        self.assertIn('assets/icons/new.png', written)
        self.assertNotIn('gone.py', written)

    def test_the_package_manifest_is_used_but_never_drops_a_shipped_file(self):
        write(self.target, {'kept.py': 'old', u.MANIFEST: json.dumps({'files': ['kept.py']})})
        write(self.source, {'kept.py': 'new', u.MANIFEST: json.dumps({'files': []})})
        self.assertEqual(u.apply_update(self.source, self.target, self.backup), 0)
        self.assertEqual((self.target / 'kept.py').read_text(), 'new')

    def test_manifest_cannot_reach_outside_the_install(self):
        outside = self.root / 'outside.py'
        outside.write_text('keep')
        write(self.target, {u.MANIFEST: json.dumps({'files': ['../outside.py', str(outside)]})})
        write(self.source, {'usage_widget.py': 'new'})
        u.apply_update(self.source, self.target, self.backup)
        self.assertEqual(outside.read_text(), 'keep')

    def test_failure_restores_every_file_as_it_was(self):
        write(self.target, {'a.py': 'old a', 'gone.py': 'old',
                            u.MANIFEST: json.dumps({'files': ['a.py', 'gone.py']})})
        (self.target / 'z.py').mkdir()   # the last file cannot be written
        write(self.source, {'a.py': 'new a', 'b.py': 'new b', 'z.py': 'new z'})
        with self.assertRaises(OSError):
            u.apply_update(self.source, self.target, self.backup)
        self.assertEqual((self.target / 'a.py').read_text(), 'old a')
        self.assertEqual((self.target / 'gone.py').read_text(), 'old')
        self.assertFalse((self.target / 'b.py').exists())
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), sorted(['a.py', 'gone.py', 'z.py', u.MANIFEST]))

    def test_helper_retries_then_always_restarts_the_widget(self):
        write(self.source, {'usage_widget.py': 'new'})
        launched = []
        with IsolatedTemp() as temp, patch.object(u, 'apply_update', side_effect=PermissionError('locked')) as apply:
            (temp / (u.STAGE_PREFIX + 'old')).mkdir()
            applied = u.run_apply(self.target, self.source, 1, None, pause=0,
                                  wait=lambda pid: True, launch=launched.append)
            self.assertFalse(applied)
            self.assertEqual(apply.call_count, u.APPLY_ATTEMPTS)
            self.assertEqual(launched, [self.target])
            self.assertEqual(list(temp.iterdir()), [])

    def test_backup_is_kept_when_a_rollback_could_not_finish(self):
        write(self.source, {'usage_widget.py': 'new'})
        with IsolatedTemp() as temp, patch.object(u, 'apply_update', side_effect=u.RollbackIncomplete(['a.py'])):
            u.run_apply(self.target, self.source, 1, None, attempts=1, pause=0,
                        wait=lambda pid: True, launch=lambda target: None)
            self.assertEqual(len(list(temp.glob(u.BACKUP_PREFIX + '*'))), 1)

    def test_start_apply_runs_a_copy_outside_the_install(self):
        with IsolatedTemp() as temp, patch.object(u.subprocess, 'Popen') as popen:
            u.start_apply(self.source, self.target, log=self.root / 'update.log')
            command = popen.call_args.args[0]
            script = temp / u.APPLY_SCRIPT
            self.assertEqual(script.read_bytes(), Path(u.__file__).read_bytes())
            self.assertEqual(command[:4], [sys.executable, '-B', str(script), 'apply'])
            self.assertEqual(command[command.index('--wait-pid') + 1], str(os.getpid()))
            self.assertEqual(command[command.index('--target') + 1], str(self.target))

    def test_helper_script_applies_as_a_standalone_process(self):
        write(self.source, {'usage_widget.py': 'new'})
        write(self.target, {'usage_widget.py': 'old'})
        finished = subprocess.Popen([sys.executable, '-c', 'pass'])
        finished.wait()
        log = self.root / 'update.log'
        scratch = self.root / 'tmp'
        scratch.mkdir()
        env = dict(os.environ, TEMP=str(scratch), TMP=str(scratch))
        result = subprocess.run(
            [sys.executable, '-B', u.__file__, 'apply', '--target', str(self.target),
             '--source', str(self.source), '--wait-pid', str(finished.pid), '--log', str(log)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.target / 'usage_widget.py').read_text(), 'new')
        self.assertIn('applied', log.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
