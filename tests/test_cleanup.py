"""Leftovers from earlier versions and old local release copies are cleaned up."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import usage_widget as u
from tests.support import integration

PROJECT = Path(__file__).resolve().parent.parent


class ObsoleteFileTests(unittest.TestCase):
    def test_only_the_listed_leftovers_are_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('reset_credits.json', 'settings.json', 'last_snapshot.json', 'alerts.json'):
                (root / name).write_text('{}', encoding='utf-8')
            u.remove_obsolete_files(root)
            self.assertEqual(sorted(p.name for p in root.iterdir()),
                             ['alerts.json', 'last_snapshot.json', 'settings.json'])
            u.remove_obsolete_files(root)  # nothing left to remove is fine

    def test_a_missing_folder_is_fine(self):
        u.remove_obsolete_files(Path(tempfile.gettempdir()) / 'ai-usage-no-such-folder')


@integration('runs the release script')
@unittest.skipUnless(os.name == 'nt' and shutil.which('powershell'), 'PowerShell release script is Windows-only')
class ReleasePruneTests(unittest.TestCase):
    def prune_snippet(self):
        lines = (PROJECT / 'publish_update.ps1').read_text(encoding='utf-8-sig').splitlines()
        start = next(i for i, line in enumerate(lines) if "-Filter 'release-*'" in line)
        return '\n'.join(lines[start:start + 3])

    def test_only_the_three_newest_release_copies_stay(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            dist = project / 'dist'
            names = [f'release-3.11.{n}-x' for n in range(5)]
            for name in names:
                (dist / name).mkdir(parents=True)
                (dist / name / 'AIUsageWidget.zip').write_bytes(b'zip')
                time.sleep(0.05)
            (dist / 'notes.txt').write_text('kept', encoding='utf-8')
            script = f"$project = '{project}'\n{self.prune_snippet()}"
            subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                           check=True, capture_output=True, timeout=60)
            self.assertEqual(sorted(p.name for p in dist.iterdir()), ['notes.txt'] + names[2:])


if __name__ == '__main__':
    unittest.main()
