import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cursor_activity as ca


class CursorActivityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def transcript(self, name, events=()):
        path = self.root / name / 'agent-transcripts' / 'conversation' / 'events.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(event) + '\n' for event in events), encoding='utf-8')
        return path

    @staticmethod
    def append(path, event, newline=True):
        with path.open('a', encoding='utf-8') as stream:
            if isinstance(event, str):
                stream.write(event)
            else:
                stream.write(json.dumps(event))
            if newline:
                stream.write('\n')

    def test_tail_initializes_active_and_ended_transcripts(self):
        self.transcript('active', [{'role': 'user'}, {'role': 'assistant'}])
        self.transcript('ended', [{'role': 'user'}, {'type': 'turn_ended', 'status': 'completed'}])
        monitor = ca.CursorActivityMonitor(self.root)
        monitor.poll(100.0)
        states = {path.parts[-4]: state.status for path, state in monitor.files.items()}
        self.assertEqual(states['active'], ca.ACTIVE)
        self.assertEqual(states['ended'], ca.ENDED)
        self.assertTrue(monitor.visual_active(100.0))

    def test_main_end_does_not_hide_active_subagent(self):
        main = self.transcript('main', [{'role': 'user'}])
        subagent = self.transcript('subagent', [{'role': 'user'}])
        monitor = ca.CursorActivityMonitor(self.root)
        monitor.poll(10.0)
        self.append(main, {'type': 'turn_ended'})
        self.assertTrue(monitor.poll(10.8))
        self.assertTrue(monitor.visual_active(12.0))
        self.append(subagent, {'type': 'turn_ended'})
        self.assertTrue(monitor.poll(12.1))
        self.assertTrue(monitor.visual_active(12.1))
        self.assertFalse(monitor.visual_active(13.0))

    def test_late_assistant_event_does_not_revive_ended_turn(self):
        path = self.transcript('late', [{'role': 'user'}])
        monitor = ca.CursorActivityMonitor(self.root)
        monitor.poll(20.0)
        self.append(path, {'type': 'turn_ended'})
        monitor.poll(20.8)
        self.assertFalse(monitor.visual_active(21.6))
        self.append(path, {'role': 'assistant'})
        self.assertFalse(monitor.poll(22.4))
        self.assertFalse(monitor.visual_active(22.4))

    def test_unknown_json_requests_once_per_cooldown(self):
        path = self.transcript('unknown')
        monitor = ca.CursorActivityMonitor(self.root)
        monitor.poll(0.0)
        self.append(path, {'type': 'future_event'})
        self.assertTrue(monitor.poll(0.8))
        self.append(path, {'type': 'future_event'})
        self.assertFalse(monitor.poll(1.6))
        self.append(path, {'type': 'another_future_event'})
        self.assertFalse(monitor.poll(2.4))
        self.append(path, {'type': 'future_event'})
        self.assertTrue(monitor.poll(61.0))
        self.assertFalse(monitor.visual_active(61.0))

    def test_work_before_known_start_refreshes_without_visual_activation(self):
        path = self.transcript('midturn')
        monitor = ca.CursorActivityMonitor(self.root)
        monitor.poll(0.0)
        self.append(path, {'role': 'assistant'})
        self.assertTrue(monitor.poll(0.8))
        self.assertFalse(monitor.visual_active(0.8))
        self.append(path, {'role': 'user'})
        self.assertTrue(monitor.poll(1.6))
        self.assertTrue(monitor.visual_active(1.6))

    def test_oversize_malformed_line_cannot_activate_from_its_suffix(self):
        path = self.transcript('oversize')
        monitor = ca.CursorActivityMonitor(self.root)
        monitor.poll(0)
        self.append(path, 'x' * (ca.READ_LIMIT * 2), newline=False)
        monitor.poll(1)
        monitor.poll(2)
        self.append(path, {'role': 'user'})
        self.assertFalse(monitor.poll(3))
        self.assertFalse(monitor.visual_active(3))
        self.append(path, {'role': 'user'})
        self.assertTrue(monitor.poll(4))
        self.assertTrue(monitor.visual_active(4))

    def test_malformed_and_partial_lines_do_not_create_false_activity(self):
        path = self.transcript('partial')
        monitor = ca.CursorActivityMonitor(self.root)
        monitor.poll(0.0)
        self.append(path, '{broken')
        self.assertFalse(monitor.poll(0.8))
        self.append(path, '{"role":"us', newline=False)
        self.assertFalse(monitor.poll(1.6))
        self.append(path, 'er","message":"x"}\n', newline=False)
        self.assertTrue(monitor.poll(2.4))
        self.assertTrue(monitor.visual_active(2.4))

    def test_new_transcript_in_old_project_is_found_on_the_next_scan(self):
        (self.root / 'old-project').mkdir()
        monitor = ca.CursorActivityMonitor(self.root)
        monitor.poll(0.0)
        self.transcript('old-project', [{'role': 'user'}])
        # Found through the folder times, not the twelve-second rescan.
        self.assertTrue(monitor.poll(ca.SCAN_INTERVAL))
        self.assertTrue(monitor.visual_active(ca.SCAN_INTERVAL))

    def test_new_project_and_new_conversation_are_found_quickly(self):
        monitor = ca.CursorActivityMonitor(self.root)
        monitor.poll(0.0)
        self.transcript('brand-new', [{'role': 'user'}])
        self.assertTrue(monitor.poll(1.0))
        first = self.transcript('project', [{'role': 'user'}, {'type': 'turn_ended'}])
        monitor.poll(2.0)
        second = first.parent.parent / 'second' / 'events.jsonl'
        second.parent.mkdir()
        second.write_text(json.dumps({'role': 'user'}) + '\n', encoding='utf-8')
        self.assertTrue(monitor.poll(3.0))
        self.assertIn(second, monitor.files)

    def test_a_new_subagent_of_a_recent_conversation_is_found_quickly(self):
        main = self.transcript('project', [{'role': 'user'}])
        monitor = ca.CursorActivityMonitor(self.root)
        monitor.poll(0.0)
        subagent = main.parent / 'subagents' / 'helper.jsonl'
        subagent.parent.mkdir()
        subagent.write_text(json.dumps({'role': 'user'}) + '\n', encoding='utf-8')
        monitor.poll(1.0)
        self.assertIn(subagent, monitor.files)

    def test_unchanged_folders_do_not_trigger_a_full_rescan(self):
        self.transcript('project', [{'role': 'user'}])
        monitor = ca.CursorActivityMonitor(self.root)
        monitor.poll(0.0)
        with patch.object(monitor, '_candidate_files', wraps=monitor._candidate_files) as rescan:
            for step in range(1, 10):
                monitor.poll(step * ca.SCAN_INTERVAL)
        self.assertEqual(rescan.call_count, 0)

    def test_active_transcript_is_pinned_over_tracking_limit(self):
        active = self.transcript('active', [{'role': 'user'}])
        monitor = ca.CursorActivityMonitor(self.root)
        with patch.object(ca, 'MAX_TRACKED', 2):
            monitor.poll(0.0)
            for index in range(4):
                self.transcript(f'new-{index}', [{'type': 'turn_ended'}])
            monitor.poll(ca.DISCOVERY_INTERVAL + 0.1)
        self.assertIn(active, monitor.files)
        self.assertTrue(monitor.visual_active(ca.DISCOVERY_INTERVAL + 0.1))

    def test_missing_turn_end_expires_through_watchdog(self):
        self.transcript('stale', [{'role': 'user'}])
        monitor = ca.CursorActivityMonitor(self.root)
        with patch.object(ca, 'STALE_TIMEOUT', 2.0):
            monitor.poll(30.0)
            self.assertTrue(monitor.visual_active(31.9))
            self.assertFalse(monitor.visual_active(32.1))

    def test_truncate_resets_previous_turn_state(self):
        path = self.transcript('truncate', [{'role': 'user'}])
        monitor = ca.CursorActivityMonitor(self.root)
        monitor.poll(40.0)
        self.assertTrue(monitor.visual_active(40.0))
        path.write_text('', encoding='utf-8')
        monitor.poll(40.8)
        self.assertFalse(monitor.visual_active(40.8))
        self.append(path, {'role': 'user'})
        self.assertTrue(monitor.poll(41.6))
        self.assertTrue(monitor.visual_active(41.6))


class BackgroundActivityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'project/agent-transcripts/main/events.jsonl'
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"role":"user"}\n', encoding='utf-8')

    def monitor(self, factory=None):
        monitor = ca.BackgroundCursorActivityMonitor(self.root, monitor_factory=factory)
        def stop():
            monitor.close()
            if monitor._thread:
                monitor._thread.join(3)
                self.assertFalse(monitor._thread.is_alive())
        self.addCleanup(stop)
        return monitor

    def drain(self, monitor, now):
        hit = monitor.poll(now)
        deadline = time.monotonic() + 3
        while monitor._busy and time.monotonic() < deadline:
            time.sleep(.001)
            hit |= monitor.poll(now)
        self.assertFalse(monitor._busy, 'background scan did not finish')
        return hit

    def test_real_transcript_start_end_and_subagent_reach_the_ui(self):
        monitor = self.monitor()
        self.assertFalse(self.drain(monitor, 0))
        self.assertTrue(monitor.visual_active(0))
        CursorActivityTests.append(self.path, {'type': 'turn_ended'})
        self.assertTrue(self.drain(monitor, 1))
        self.assertTrue(monitor.visual_active(1))
        self.assertFalse(monitor.visual_active(2))
        subagent = self.path.parent / 'subagents/helper.jsonl'
        subagent.parent.mkdir()
        subagent.write_text('{"role":"user"}\n', encoding='utf-8')
        self.assertTrue(self.drain(monitor, 2))
        self.assertTrue(monitor.visual_active(2))
        subagent.write_text('', encoding='utf-8')
        self.drain(monitor, 3)
        self.assertFalse(monitor.visual_active(3))

    def test_slow_scan_does_not_block_poll_or_overlap_another_scan(self):
        started, release = threading.Event(), threading.Event()
        worker_ids = []
        owner = self
        class Slow(ca.CursorActivityMonitor):
            def _candidate_files(self):
                worker_ids.append(threading.get_ident())
                started.set()
                if not release.wait(3):
                    raise RuntimeError('test worker was not released')
                return super()._candidate_files()
        monitor = self.monitor(lambda: Slow(owner.root))
        self.addCleanup(release.set)
        monitor.poll(0)
        self.assertTrue(started.wait(1))
        for now in (1, 2, 3):
            self.assertFalse(monitor.poll(now))
            self.assertTrue(monitor._busy)
        self.assertEqual(len(worker_ids), 1)
        self.assertNotEqual(worker_ids[0], threading.get_ident())
        release.set()
        self.drain(monitor, 0)
        self.assertTrue(monitor.visual_active(0))

    def test_pause_discards_in_flight_results_and_reseeds_on_resume(self):
        started, release = threading.Event(), threading.Event()
        owner = self
        class Delayed(ca.CursorActivityMonitor):
            def poll(self, now):
                result = super().poll(now)
                started.set()
                if not release.wait(3):
                    raise RuntimeError('test worker was not released')
                return result
        monitor = self.monitor(lambda: Delayed(owner.root))
        self.addCleanup(release.set)
        monitor.poll(0)
        self.assertTrue(started.wait(1))
        monitor.pause()
        CursorActivityTests.append(self.path, {'type': 'turn_ended'})
        release.set()
        self.assertFalse(self.drain(monitor, 1))
        self.assertFalse(monitor.visual_active(1))
        CursorActivityTests.append(self.path, {'role': 'user'})
        self.assertTrue(self.drain(monitor, 2))
        self.assertTrue(monitor.visual_active(2))

    def test_deadlines_expire_even_without_another_completed_scan(self):
        monitor = self.monitor()
        self.drain(monitor, 0)
        self.assertTrue(monitor.visual_active(0))
        self.assertFalse(monitor.visual_active(ca.STALE_TIMEOUT + 1))
        self.assertFalse(monitor.fast(ca.STALE_TIMEOUT + 1))

    def test_failed_worker_scan_is_retried(self):
        inner = ca.CursorActivityMonitor(self.root)
        monitor = self.monitor(lambda: inner)
        with patch.object(inner, 'poll', side_effect=OSError('temporarily unavailable')), \
             self.assertLogs(ca.LOG, level='ERROR'):
            self.assertFalse(self.drain(monitor, 0))
        self.drain(monitor, 1)
        self.assertTrue(monitor.visual_active(1))

    def test_thread_start_failure_can_be_retried(self):
        monitor = self.monitor()
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('no thread available')):
            with self.assertRaises(RuntimeError):
                monitor.poll(0)
        self.drain(monitor, 1)
        self.assertTrue(monitor.visual_active(1))

    def test_close_returns_while_scan_is_blocked_and_worker_exits_afterwards(self):
        started, release = threading.Event(), threading.Event()
        owner = self
        class Slow(ca.CursorActivityMonitor):
            def poll(self, now):
                started.set()
                if not release.wait(3):
                    raise RuntimeError('test worker was not released')
                return super().poll(now)
        monitor = self.monitor(lambda: Slow(owner.root))
        self.addCleanup(release.set)
        monitor.poll(0)
        self.assertTrue(started.wait(1))
        monitor.close()
        self.assertTrue(monitor._thread.is_alive())
        self.assertFalse(monitor.poll(1))
        release.set()
        monitor._thread.join(2)
        self.assertFalse(monitor._thread.is_alive())


if __name__ == '__main__':
    unittest.main(verbosity=2)
