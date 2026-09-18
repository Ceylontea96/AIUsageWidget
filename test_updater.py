import tempfile
import unittest
import zipfile
from pathlib import Path

import updater as u


class UpdaterTests(unittest.TestCase):
    def test_check_every_is_thirty_minutes(self):
        self.assertEqual(u.CHECK_EVERY, 30 * 60)

    def test_version_compare(self):
        self.assertTrue(u.is_newer('3.1.1', '3.1.0'))
        self.assertFalse(u.is_newer('3.1.0', '3.1.0'))
        self.assertFalse(u.is_newer('3.0.9', '3.1.0'))
        self.assertEqual(u.parse_version('3.1'), (3, 1, 0))
        self.assertTrue(u.is_newer('3.3.1', '3.3.0'))
        self.assertTrue(u.is_newer('3.3.0', '3.2.27'))
        self.assertFalse(u.is_newer('3.3.0', '3.3.0'))
        self.assertTrue(u.is_newer('3.3.1', '3.3.0'))
        self.assertTrue(u.is_newer('3.4.0', '3.3.0'))
        self.assertTrue(u.is_newer('4.0.0', '3.3.9'))
        self.assertTrue(u.is_newer('3.10.0', '3.9.9'))
        # Numeric dotted compare only: prerelease suffixes are not SemVer-aware.
        self.assertEqual(u.parse_version('3.3.0-rc.1'), (3, 3, 0))
        self.assertFalse(u.is_newer('3.3.0', '3.3.0-rc.1'))
        self.assertFalse(u.is_newer('3.3.0-rc.1', '3.3.0'))

    def test_publish_script_includes_new_modules(self):
        script = Path(__file__).with_name('publish_update.ps1').read_text(encoding='utf-8')
        for name in ('polling.py', 'additional_ui.py', 'claude_bridge.py', 'claude_integration.py'):
            self.assertIn("'" + name + "'", script)

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


if __name__ == '__main__':
    unittest.main()
