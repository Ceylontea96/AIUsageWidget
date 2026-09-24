"""Claude Code activity from session state only. Offline, with fake processes."""
import builtins
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import claude_activity as ca

WIDGET = 100


class FakeProcesses:
    def __init__(self):
        self.times = {WIDGET: 1000}
        self.tree = {}

    def add(self, pid, created, parent=None):
        self.times[pid] = created
        if parent is not None:
            self.tree[pid] = parent

    def created(self, pid):
        return self.times.get(pid)

    def parents(self):
        return dict(self.tree)


class MonitorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.procs = FakeProcesses()
        self.agent_calls = 0
        self.agents_answer = []
        sleep = patch.object(ca.time, 'sleep')
        sleep.start()
        self.addCleanup(sleep.stop)
        self.now = 0.0

    def agents(self):
        self.agent_calls += 1
        answer = self.agents_answer
        return answer() if callable(answer) else answer

    def monitor(self):
        return ca.ClaudeActivityMonitor(self.root, self.procs, self.agents, own=(WIDGET, 1000),
                                        spawn=lambda job: job())

    def session(self, pid, status='idle', created=5000, parent=None, **extra):
        self.procs.add(pid, created, parent)
        body = {'pid': pid, 'sessionId': f's{pid}', 'status': status, 'kind': 'interactive',
                'version': '2.1.280', 'procStart': str(created), 'entrypoint': 'claude-desktop'}
        body.update(extra)
        (self.root / f'{pid}.json').write_text(json.dumps(body), encoding='utf-8')

    def tick(self, monitor, step=1.0):
        self.now += step
        monitor.poll(self.now)
        return monitor.visual_active(self.now)


class FastPathTests(MonitorCase):
    def test_busy_is_active_and_idle_ends_after_the_grace(self):
        m = self.monitor()
        self.session(200, 'busy')
        self.assertTrue(self.tick(m))
        self.session(200, 'idle')
        self.assertTrue(m.visual_active(self.now + ca.END_GRACE / 2), 'the grace keeps it on briefly')
        self.assertFalse(self.tick(m, ca.END_GRACE + 0.05))

    def test_waiting_and_finished_states_are_not_activity(self):
        m = self.monitor()
        for status, state in (('waiting', None), ('idle', 'blocked'), ('idle', 'done'), ('idle', 'failed')):
            with self.subTest(status=status, state=state):
                self.session(200, status, **({'state': state} if state else {}))
                self.assertFalse(self.tick(m, 2.0))

    def test_a_background_session_still_working_is_activity(self):
        m = self.monitor()
        self.session(200, 'idle', state='working', kind='background')
        self.assertTrue(self.tick(m))

    def test_any_one_busy_session_makes_claude_active(self):
        m = self.monitor()
        self.session(200, 'idle')
        self.session(201, 'busy', created=5001)
        self.session(202, 'idle', created=5002)
        self.assertTrue(self.tick(m))

    def test_only_digit_json_files_are_opened(self):
        self.session(200, 'busy')
        (self.root / '200.8fbd19ac.key').write_text('{"peerToken":"secret"}', encoding='utf-8')
        (self.root / 'notes.json').write_text('{}', encoding='utf-8')
        (self.root / '200.json.tmp').write_text('{}', encoding='utf-8')
        opened = []
        real_open = builtins.open

        def spy(path, *args, **kwargs):
            opened.append(Path(path).name)
            return real_open(path, *args, **kwargs)

        m = self.monitor()
        with patch('builtins.open', spy):
            self.assertTrue(self.tick(m))
        self.assertEqual(set(opened), {'200.json'})

    def test_a_file_whose_pid_disagrees_with_its_name_is_ignored(self):
        m = self.monitor()
        self.procs.add(300, 5000)
        (self.root / '300.json').write_text(json.dumps({'pid': 301, 'status': 'busy'}), encoding='utf-8')
        self.assertFalse(self.tick(m))

    def test_dead_or_reused_processes_are_ignored(self):
        m = self.monitor()
        self.session(200, 'busy')
        del self.procs.times[200]
        self.assertFalse(self.tick(m), 'a leftover file from a crashed session')
        self.session(201, 'busy', created=5000)
        self.procs.times[201] = 9999
        self.assertFalse(self.tick(m), 'the PID now belongs to a later process')


class SelfExclusionTests(MonitorCase):
    def test_the_widgets_own_claude_runs_never_count(self):
        m = self.monitor()
        # widget -> poll worker -> claude.cmd shell -> claude
        self.procs.add(110, 2000, parent=WIDGET)
        self.procs.add(120, 3000, parent=110)
        self.session(130, 'busy', created=4000, parent=120, entrypoint='sdk-cli')
        self.assertFalse(self.tick(m))

    def test_ancestry_is_judged_once_per_session(self):
        m = self.monitor()
        snapshots = []
        real = self.procs.parents
        self.procs.parents = lambda: snapshots.append(1) or real()
        self.session(200, 'busy')
        for _ in range(5):
            self.tick(m)
        self.assertEqual(len(snapshots), 1)
        self.session(201, 'busy', created=5001)
        self.tick(m)
        self.assertEqual(len(snapshots), 2, 'a new session is judged, the old one is remembered')

    def test_sdk_cli_alone_is_not_a_reason_to_exclude(self):
        # Remote Control sessions are sdk-cli too; only ancestry decides.
        m = self.monitor()
        self.session(200, 'busy', entrypoint='sdk-cli', parent=50)
        self.procs.add(50, 10)
        self.assertTrue(self.tick(m))

    def test_a_reused_parent_pid_does_not_make_a_session_ours(self):
        m = self.monitor()
        # The session's recorded parent PID now belongs to a process that
        # started after it, so the chain is broken rather than followed.
        self.procs.add(110, 9000, parent=WIDGET)
        self.session(200, 'busy', created=5000, parent=110)
        self.assertTrue(self.tick(m))

    def test_a_widget_pid_with_another_start_time_is_not_the_widget(self):
        m = ca.ClaudeActivityMonitor(self.root, self.procs, self.agents, own=(WIDGET, 777),
                                     spawn=lambda job: job())
        self.session(200, 'busy', created=5000, parent=WIDGET)
        self.assertTrue(self.tick(m))


class ValidationTests(MonitorCase):
    def test_each_version_is_checked_once_when_a_session_first_appears(self):
        m = self.monitor()
        self.assertFalse(self.tick(m))
        self.assertEqual(self.agent_calls, 0, 'nothing to compare yet')
        self.session(200, 'busy')
        self.agents_answer = [{'pid': 200, 'kind': 'interactive', 'status': 'busy'}]
        self.tick(m)
        self.tick(m)
        self.assertEqual(self.agent_calls, 1)
        self.assertEqual(m.validated_version, '2.1.280')
        self.session(200, 'busy', version='2.2.0')
        self.tick(m)
        self.tick(m)
        self.assertEqual(self.agent_calls, 2)
        self.assertEqual(m.mode, ca.FAST)

    def test_a_check_that_finishes_between_scans_runs_once(self):
        # The check runs on a thread; its result must land before the next
        # scan decides whether the version still needs checking.
        queued = []
        m = ca.ClaudeActivityMonitor(self.root, self.procs, self.agents, own=(WIDGET, 1000),
                                     spawn=queued.append)
        self.session(200, 'busy')
        self.agents_answer = [{'pid': 200, 'status': 'busy'}]
        self.tick(m)
        queued.pop()()
        for _ in range(4):
            self.tick(m)
        self.assertEqual(self.agent_calls, 1)
        self.assertEqual(queued, [])

    def test_a_status_race_is_retried_before_it_counts(self):
        m = self.monitor()
        self.session(200, 'busy')
        answers = iter([[{'pid': 200, 'status': 'idle'}], [{'pid': 200, 'status': 'busy'}]])
        self.agents_answer = lambda: next(answers)
        self.tick(m)
        self.tick(m)
        self.assertEqual(m.mode, ca.FAST)
        self.assertEqual(m.validated_version, '2.1.280')

    def test_a_lasting_disagreement_hands_over_to_agents(self):
        m = self.monitor()
        self.session(200, 'busy')
        self.agents_answer = [{'pid': 200, 'status': 'idle'}]
        self.tick(m)
        self.tick(m)
        self.assertEqual(m.mode, ca.AGENTS)

    def test_a_session_missing_from_agents_is_inconclusive_not_broken(self):
        m = self.monitor()
        self.session(200, 'busy')
        self.agents_answer = []
        self.tick(m)
        self.tick(m)
        self.assertEqual(m.mode, ca.FAST)
        self.assertEqual(m.validated_version, '2.1.280')

    def test_a_claude_without_agents_keeps_the_readable_files(self):
        m = self.monitor()
        self.session(200, 'busy')
        self.agents_answer = None
        self.assertTrue(self.tick(m))
        self.tick(m)
        self.assertEqual(self.agent_calls, 1)
        self.assertEqual(m.mode, ca.FAST)

    def test_files_that_stop_parsing_switch_to_agents(self):
        m = self.monitor()
        self.procs.add(200, 5000)
        (self.root / '200.json').write_text('{"pid": 200, "status": "busy", "extra": ', encoding='utf-8')
        self.agents_answer = [{'pid': 200, 'status': 'busy'}]
        for _ in range(ca.PARSE_FAILURES_BEFORE_CHECK + 1):
            self.tick(m)
        self.assertEqual(m.mode, ca.AGENTS)

    def test_one_unreadable_read_is_a_file_being_written(self):
        m = self.monitor()
        self.procs.add(200, 5000)
        (self.root / '200.json').write_text('{"pid": 2', encoding='utf-8')
        self.tick(m)
        self.session(200, 'busy')
        self.tick(m)
        self.assertEqual(m.mode, ca.FAST)

    def test_no_files_and_no_agents_means_no_activity_only(self):
        m = self.monitor()
        self.procs.add(200, 5000)
        (self.root / '200.json').write_text('not json', encoding='utf-8')
        self.agents_answer = None
        for _ in range(ca.PARSE_FAILURES_BEFORE_CHECK + 1):
            self.assertFalse(self.tick(m))
        self.assertEqual(m.mode, ca.UNAVAILABLE)


class AgentsModeTests(MonitorCase):
    def test_polls_every_five_seconds_and_skips_its_own_runs(self):
        m = self.monitor()
        m.mode = ca.AGENTS
        self.procs.add(110, 2000, parent=WIDGET)
        self.agents_answer = [{'pid': 110, 'status': 'busy'}]
        self.tick(m)
        self.assertFalse(self.tick(m))
        self.agents_answer = [{'pid': 110, 'status': 'busy'}, {'state': 'working', 'kind': 'background'}]
        for _ in range(6):
            self.tick(m)
        self.assertTrue(self.tick(m))
        self.assertLessEqual(self.agent_calls, 3, 'about one call per five seconds')


if __name__ == '__main__':
    unittest.main()
