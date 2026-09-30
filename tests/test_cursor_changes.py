"""Cursor's folder checks run only after something under the projects tree changed."""
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cursor_activity as ca

WINDOWS = sys.platform == 'win32'


def transcript(root, project, conversation='conversation', events=({'role': 'user'},)):
    path = Path(root) / project / 'agent-transcripts' / conversation / 'events.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(event) + '\n' for event in events), encoding='utf-8')
    return path


class TempRoot(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)


@unittest.skipUnless(WINDOWS, 'Windows change notifications')
class NameChangesTests(TempRoot):
    def gate(self):
        gate = ca.NameChanges(self.root)
        self.addCleanup(gate.close)
        self.assertTrue(gate.changed(), 'the first look always happens')
        self.assertTrue(gate.watching)
        return gate

    def test_a_quiet_tree_reports_no_change(self):
        (self.root / 'project' / 'agent-transcripts').mkdir(parents=True)
        gate = self.gate()
        for _ in range(3):
            self.assertFalse(gate.changed())

    def test_entries_created_removed_or_renamed_anywhere_are_reported(self):
        deep = self.root / 'project' / 'agent-transcripts' / 'conversation'
        deep.mkdir(parents=True)
        gate = self.gate()
        steps = (
            lambda: (deep / 'subagents').mkdir(),
            lambda: (deep / 'events.jsonl').write_text('{}\n', encoding='utf-8'),
            lambda: (deep / 'events.jsonl').rename(deep / 'renamed.jsonl'),
            lambda: (deep / 'renamed.jsonl').unlink(),
            lambda: (self.root / 'new-project').mkdir(),
        )
        for step in steps:
            step()
            self.assertTrue(gate.changed())
            self.assertFalse(gate.changed(), 'reported once, then quiet again')

    def test_appends_are_not_reported_and_are_left_to_the_file_checks(self):
        path = transcript(self.root, 'project')
        gate = self.gate()
        with path.open('a', encoding='utf-8') as stream:
            stream.write('{"role":"assistant"}\n')
        self.assertFalse(gate.changed())

    def test_a_removed_root_drops_the_handle_and_a_new_one_is_made(self):
        (self.root / 'project').mkdir()
        gate = self.gate()
        shutil.rmtree(self.root)
        self.assertTrue(gate.changed())
        self.assertFalse(gate.watching, 'a handle on a removed folder is dropped')
        self.assertTrue(gate.changed(), 'no folder, no handle: always look')
        self.root.mkdir()
        self.assertTrue(gate.changed())
        self.assertTrue(gate.watching)
        self.assertFalse(gate.changed())


class FakeKernel32:
    """Stands in for the Windows calls so failures can be forced anywhere."""
    invalid_handle = -1

    def __init__(self):
        self.made = self.closed = 0
        self.wait_result = 0x102
        self.rearm = True

    def FindFirstChangeNotificationW(self, path, subtree, flags):
        self.made += 1
        return 1000 + self.made

    def WaitForSingleObject(self, handle, timeout):
        return self.wait_result

    def FindNextChangeNotification(self, handle):
        return self.rearm

    def FindCloseChangeNotification(self, handle):
        self.closed += 1
        return True


class HandleFailureTests(TempRoot):
    def gate(self):
        gate = ca.NameChanges(self.root)
        gate._k32 = fake = FakeKernel32()
        return gate, fake

    def test_a_failed_wait_falls_back_and_makes_a_new_handle(self):
        gate, fake = self.gate()
        self.assertTrue(gate.changed())
        self.assertFalse(gate.changed())
        fake.wait_result = 0xFFFFFFFF  # WAIT_FAILED
        self.assertTrue(gate.changed())
        self.assertEqual((fake.closed, gate.watching), (1, False))
        fake.wait_result = 0x102
        self.assertTrue(gate.changed())
        self.assertEqual(fake.made, 2)
        self.assertFalse(gate.changed())

    def test_a_handle_that_cannot_be_rearmed_is_dropped(self):
        gate, fake = self.gate()
        gate.changed()
        fake.wait_result, fake.rearm = 0x0, False
        self.assertTrue(gate.changed())
        self.assertEqual((fake.closed, gate.watching), (1, False))

    def test_without_the_windows_api_every_call_says_changed(self):
        gate = ca.NameChanges(self.root)
        gate._k32 = None
        self.assertTrue(all(gate.changed() for _ in range(3)))


class MonitorGateTests(TempRoot):
    def monitor(self):
        monitor = ca.CursorActivityMonitor(self.root)
        self.addCleanup(monitor.close)
        return monitor

    def quiet_polls(self, monitor, start, count):
        # Scans between the twelve-second full rescans.
        return [start + step * ca.SCAN_INTERVAL for step in range(1, count + 1)]

    @unittest.skipUnless(WINDOWS, 'Windows change notifications')
    def test_an_unchanged_tree_skips_the_folder_checks(self):
        transcript(self.root, 'project')
        for index in range(5):
            (self.root / f'no-agent-{index}').mkdir()
        monitor = self.monitor()
        monitor.poll(0.0)
        with patch.object(monitor, '_folder_times', wraps=monitor._folder_times) as checks:
            for now in self.quiet_polls(monitor, 0.0, 10):
                monitor.poll(now)
        self.assertEqual(checks.call_count, 0)

    def test_a_new_conversation_brings_the_folder_checks_back(self):
        transcript(self.root, 'project')
        monitor = self.monitor()
        monitor.poll(0.0)
        monitor.poll(ca.SCAN_INTERVAL)
        second = transcript(self.root, 'project', 'second')
        with patch.object(monitor, '_folder_times', wraps=monitor._folder_times) as checks:
            self.assertTrue(monitor.poll(2 * ca.SCAN_INTERVAL))
        self.assertGreaterEqual(checks.call_count, 1)
        self.assertIn(second, monitor.files)
        self.assertTrue(monitor.visual_active(2 * ca.SCAN_INTERVAL))

    def test_without_a_handle_the_folders_are_checked_every_scan(self):
        transcript(self.root, 'project')
        monitor = self.monitor()
        monitor._changes._k32 = None
        monitor.poll(0.0)
        with patch.object(monitor, '_folder_times', wraps=monitor._folder_times) as checks:
            for now in self.quiet_polls(monitor, 0.0, 4):
                monitor.poll(now)
        self.assertEqual(checks.call_count, 4)

    def test_the_projects_folder_can_vanish_and_come_back(self):
        transcript(self.root, 'project')
        monitor = self.monitor()
        monitor.poll(0.0)
        shutil.rmtree(self.root)
        monitor.poll(1.0)
        monitor.poll(2.0)
        path = transcript(self.root, 'again')
        for now in (3.0, 4.0):
            monitor.poll(now)
        self.assertIn(path, monitor.files)
        self.assertTrue(monitor.visual_active(4.0))


class BackgroundCloseTests(TempRoot):
    def test_replaced_and_finished_monitors_release_their_handles(self):
        transcript(self.root, 'project')
        closed = []

        class Counting(ca.CursorActivityMonitor):
            def close(self):
                closed.append(self)
                super().close()

        made = []
        background = ca.BackgroundCursorActivityMonitor(
            self.root, monitor_factory=lambda: made.append(Counting(self.root)) or made[-1])

        def drain(now):
            background.poll(now)
            deadline = time.monotonic() + 3
            while background._busy and time.monotonic() < deadline:
                time.sleep(.001)
                background.poll(now)
            self.assertFalse(background._busy)

        drain(0)
        background.pause()
        drain(1)
        self.assertEqual(closed, made[:1], 'the replaced monitor released its handle')
        background.close()
        background._thread.join(3)
        self.assertFalse(background._thread.is_alive())
        self.assertEqual(closed, made, 'the last monitor is released when the worker ends')


if __name__ == '__main__':
    unittest.main()
