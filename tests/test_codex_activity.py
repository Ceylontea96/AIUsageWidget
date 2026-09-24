import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from codex_activity import END_GRACE, CodexActivityMonitor


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.monitor = CodexActivityMonitor(self.temp.name)
        self.path = Path(self.temp.name) / 'sessions' / datetime.now().strftime('%Y/%m/%d') / 'session.jsonl'
        self.path.parent.mkdir(parents=True)

    def event(self, total, kind='token_count'):
        return json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(), 'type':'event_msg', 'payload':{'type':kind, 'info':{'total_token_usage':{'total_tokens':total}}, 'rate_limits':{'primary':{'used_percent':39}}}}).encode() + b'\n'

    def append(self, total):
        with self.path.open('ab') as f:
            f.write(self.event(total))

    def test_small_activity_same_quota_continued_activity_and_timeout(self):
        self.append(100)
        self.assertEqual(self.monitor.poll(0), (False, False))
        self.append(101)
        self.assertEqual(self.monitor.poll(1), (True, True))
        self.assertTrue(self.monitor.fast(12))
        self.append(102)
        self.assertTrue(self.monitor.poll(12)[0])
        self.assertTrue(self.monitor.fast(23))
        self.assertEqual(self.monitor.poll(24), (False, False))
        self.assertFalse(self.monitor.fast(24))

    def test_duplicate_total_does_not_extend_fast_mode(self):
        self.append(100)
        self.monitor.poll(0)
        self.append(101)
        self.monitor.poll(1)
        self.append(101)
        self.assertEqual(self.monitor.poll(12), (False, True))
        self.assertFalse(self.monitor.fast(13))

    def test_partial_line_and_malformed_line(self):
        self.monitor.poll(0)
        record = self.event(1)
        self.path.write_bytes(b'bad json\n' + record[:40])
        self.assertFalse(self.monitor.poll(1)[0])
        with self.path.open('ab') as f:
            f.write(record[40:])
        self.assertTrue(self.monitor.poll(2)[0])

    def test_missing_or_denied_source_is_nonfatal(self):
        self.assertEqual(self.monitor.poll(0), (False, False))
        with patch.object(Path, 'glob', side_effect=PermissionError):
            self.assertEqual(self.monitor.poll(1), (False, False))
        self.assertFalse(self.monitor.fast(100))

    def test_turn_complete_is_activity_without_quota_conversion(self):
        self.monitor.poll(0)
        self.path.write_bytes(self.event(None, 'task_complete'))
        self.assertTrue(self.monitor.poll(1)[0])
        self.assertIsNone(self.monitor.files[self.path][2])

    def test_truncation_recovers(self):
        self.append(100000)
        self.monitor.poll(0)
        self.path.write_bytes(self.event(1))
        self.assertTrue(self.monitor.poll(1)[0])


class TurnTests(unittest.TestCase):
    """The bar follows each session file's turn: start to end, not token totals."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.monitor = CodexActivityMonitor(self.temp.name)
        self.day = Path(self.temp.name) / 'sessions' / datetime.now().strftime('%Y/%m/%d')
        self.day.mkdir(parents=True)
        self.path = self.day / 'rollout-main.jsonl'

    @staticmethod
    def line(kind, **payload):
        record = {'timestamp': datetime.now(timezone.utc).isoformat(), 'type': 'event_msg',
                  'payload': {'type': kind, **payload}}
        return json.dumps(record, separators=(',', ':')).encode() + b'\n'

    def write(self, *kinds, path=None):
        with (path or self.path).open('ab') as f:
            for kind in kinds:
                f.write(self.line(kind))

    def test_start_turns_the_bar_on_before_any_tokens(self):
        self.monitor.poll(0)
        self.write('task_started')
        self.monitor.poll(1)
        self.assertTrue(self.monitor.visual_active(1))
        self.assertFalse(self.monitor.fast(1), 'quota polling still waits for a token change')

    def test_complete_or_abort_ends_after_the_short_grace(self):
        self.monitor.poll(0)
        now = 0
        for end in ('task_complete', 'turn_complete', 'turn_aborted'):
            with self.subTest(end=end):
                self.write('task_started')
                now += 2
                self.monitor.poll(now)
                self.assertTrue(self.monitor.visual_active(now))
                self.write(end)
                now += 2
                self.monitor.poll(now)
                self.assertTrue(self.monitor.visual_active(now + END_GRACE / 2))
                self.assertFalse(self.monitor.visual_active(now + END_GRACE + 0.05))

    def test_turn_names_are_accepted_too(self):
        self.monitor.poll(0)
        self.write('turn_started')
        self.monitor.poll(1)
        self.assertTrue(self.monitor.visual_active(1))

    def test_a_new_start_replaces_an_unfinished_turn(self):
        self.monitor.poll(0)
        self.write('task_started')          # its end never arrives
        self.monitor.poll(1)
        self.write('task_started', 'task_complete')
        self.monitor.poll(2)
        self.assertFalse(self.monitor.visual_active(3), 'no leftover from the orphaned start')

    def test_subagent_files_keep_their_own_turn(self):
        subagent = self.day / 'rollout-guardian.jsonl'
        self.monitor.poll(0)
        self.write('task_started')
        self.write('task_started', path=subagent)
        self.monitor.poll(1)
        self.write('task_complete')
        self.monitor.poll(2)
        self.assertTrue(self.monitor.visual_active(3), 'the subagent is still working')
        self.write('task_complete', path=subagent)
        self.monitor.poll(4)
        self.assertFalse(self.monitor.visual_active(5))

    def test_a_turn_without_an_end_expires_after_ten_quiet_minutes(self):
        self.monitor.poll(0)
        self.write('task_started')
        self.monitor.poll(1)
        self.assertTrue(self.monitor.visual_active(600))
        self.assertFalse(self.monitor.visual_active(602))

    def test_joining_a_running_turn_at_startup(self):
        self.write('task_started', 'item_completed')
        self.monitor.poll(0)
        self.assertTrue(self.monitor.visual_active(0))

    def test_an_old_orphaned_start_is_not_revived(self):
        import os, time
        self.write('task_started')
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        self.monitor.poll(0)
        self.assertFalse(self.monitor.visual_active(0))

    def test_a_start_far_back_in_a_long_turn_is_found(self):
        # The start sits before the last 64 KiB, behind a long run of other events.
        filler = self.line('item_completed', text='x' * 2000) * 200
        with self.path.open('ab') as f:
            f.write(self.line('task_complete') + self.line('task_started') + filler)
        self.monitor.poll(0)
        self.assertTrue(self.monitor.visual_active(0))

    def test_a_mentioned_event_name_is_not_an_event(self):
        # A message quoting the marker is JSON-escaped, so it cannot match.
        filler = self.line('item_completed', text='x' * 2000) * 200
        quote = self.line('agent_message', message='"payload":{"type":"task_started"')
        with self.path.open('ab') as f:
            f.write(self.line('task_complete') + quote + filler)
        self.monitor.poll(0)
        self.assertFalse(self.monitor.visual_active(0))


class CadenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.monitor = CodexActivityMonitor(self.temp.name)
        self.day = Path(self.temp.name) / 'sessions' / datetime.now().strftime('%Y/%m/%d')
        self.day.mkdir(parents=True)

    def test_known_files_every_quarter_second_new_files_every_second(self):
        path = self.day / 'known.jsonl'
        path.write_bytes(TurnTests.line('item_completed'))
        self.monitor.poll(0.0)
        with path.open('ab') as f:
            f.write(TurnTests.line('task_started'))
        self.monitor.poll(0.25)
        self.assertTrue(self.monitor.visual_active(0.25), 'an append is read on the next quarter second')
        fresh = self.day / 'fresh.jsonl'
        fresh.write_bytes(TurnTests.line('task_started'))
        self.monitor.poll(0.5)
        self.assertNotIn(fresh, self.monitor.files, 'listing folders waits for the one-second beat')
        self.monitor.poll(1.0)
        self.assertIn(fresh, self.monitor.files)
