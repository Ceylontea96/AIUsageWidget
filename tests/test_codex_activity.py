import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from codex_activity import CodexActivityMonitor


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
