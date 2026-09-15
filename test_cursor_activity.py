import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_activity import CursorActivityMonitor


class CursorActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.convo = self.root / 'proj' / 'agent-transcripts' / 'conv-1'
        self.convo.mkdir(parents=True)
        self.path = self.convo / 'conv-1.jsonl'
        self.sub = self.convo / 'subagents' / 'sub-1.jsonl'
        self.monitor = CursorActivityMonitor(self.root)

    def write(self, path, payload, mode='ab'):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode) as stream:
            stream.write(payload if isinstance(payload, bytes) else (payload + '\n').encode())

    def user(self):
        return json.dumps({'role': 'user', 'message': {'content': [{'type': 'text'}]}}) + '\n'

    def assistant(self):
        return json.dumps({'role': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Shell'}]}}) + '\n'

    def ended(self, status='success'):
        return json.dumps({'type': 'turn_ended', 'status': status}) + '\n'

    def test_seed_then_append_enters_fast_and_times_out(self):
        self.write(self.path, self.user())
        self.assertFalse(self.monitor.poll(0))
        self.assertFalse(self.monitor.fast(0))
        self.write(self.path, self.assistant())
        self.assertTrue(self.monitor.poll(1))
        self.assertTrue(self.monitor.fast(1))
        self.assertTrue(self.monitor.fast(12.9))
        self.assertFalse(self.monitor.poll(14))
        self.assertFalse(self.monitor.fast(14))

    def test_same_size_does_not_extend_but_timeout_does(self):
        self.write(self.path, self.user())
        self.monitor.poll(0)
        self.write(self.path, self.assistant())
        self.assertTrue(self.monitor.poll(1))
        self.assertFalse(self.monitor.poll(2))
        self.assertTrue(self.monitor.fast(12))
        self.assertFalse(self.monitor.fast(13.1))

    def test_turn_ended_does_not_clear_active_immediately(self):
        self.write(self.path, self.user())
        self.monitor.poll(0)
        self.write(self.path, self.assistant() + self.ended())
        self.assertTrue(self.monitor.poll(1))
        self.assertTrue(self.monitor.fast(8))
        self.assertFalse(self.monitor.poll(14))
        self.assertFalse(self.monitor.fast(14))

    def test_subagent_keeps_active_while_main_is_quiet(self):
        self.write(self.path, self.user())
        self.write(self.sub, self.user())
        self.monitor.poll(0)
        self.write(self.sub, self.assistant())
        self.assertTrue(self.monitor.poll(1))
        self.assertTrue(self.monitor.fast(1))
        self.assertFalse(self.monitor.poll(2))
        self.assertTrue(self.monitor.fast(12))

    def test_new_file_after_init_is_activity(self):
        self.write(self.path, self.user())
        self.monitor.poll(0)
        other = self.root / 'proj' / 'agent-transcripts' / 'conv-2' / 'conv-2.jsonl'
        self.write(other, self.user() + self.assistant(), 'wb')
        self.assertTrue(self.monitor.poll(1))
        self.assertTrue(self.monitor.fast(1))

    def test_missing_root_is_nonfatal(self):
        monitor = CursorActivityMonitor(self.root / 'missing')
        self.assertFalse(monitor.poll(0))
        self.assertFalse(monitor.poll(1))
        self.assertFalse(monitor.fast(100))

    def test_truncation_recovers(self):
        self.write(self.path, self.user() * 20)
        self.monitor.poll(0)
        self.write(self.path, self.assistant(), 'wb')
        self.assertTrue(self.monitor.poll(1))

    def test_malformed_line_still_counts_as_append(self):
        self.write(self.path, self.user())
        self.monitor.poll(0)
        self.write(self.path, b'not-json\n')
        self.assertTrue(self.monitor.poll(1))
        self.assertTrue(self.monitor.fast(1))
