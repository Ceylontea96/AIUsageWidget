"""Claude usage queries are spaced by activity, run from an empty folder,
and routine polls stay out of the log unless something changed."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import providers as p
import usage_widget as u
from providers import ProviderSnapshot, QuotaItem


def quota(remaining):
    return QuotaItem('chatgpt:main:primary_window', 'chatgpt', 'main', '5시간', raw_identifier='primary_window',
                     window_seconds=18000.0, used_percent=100 - remaining, remaining_percent=remaining, scope='global')


class WidgetCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name)
        for item in (patch.object(u, 'SETTINGS_PATH', path / 'settings.json'),
                     patch.object(u, 'CACHE_PATH', path / 'cache.json')):
            item.start()
            self.addCleanup(item.stop)
        animate = u.UpdatePill.animate
        u.UpdatePill.animate = False
        self.addCleanup(setattr, u.UpdatePill, 'animate', animate)
        self.w = u.UsageWidget(preview=True)
        self.w.root.withdraw()
        self.addCleanup(self.w.close)
        self.w.preview = False
        self.w.runner = Mock(slots={})
        self.w.claude_activity = Mock(last_active=float('-inf'), mode='fast')


class ClaudeQueryScheduleTests(WidgetCase):
    stale = ProviderSnapshot('claude', 'Claude', 'Claude', True, 80, '5시간 기준 잔여', stale=True)

    def job_at(self, now):
        self.w.runner.start.reset_mock()
        with patch.object(u, 'fetch_claude', return_value=self.stale), patch.object(u.time, 'monotonic', return_value=now):
            self.w.start_claude_job()
        return self.w.runner.start.called

    def test_idle_claude_is_asked_every_five_minutes(self):
        self.assertTrue(self.job_at(1000), 'the first query is not delayed')
        self.assertFalse(self.job_at(1000 + u.CLAUDE_CLI_INTERVAL + 1))
        self.assertFalse(self.job_at(1000 + u.CLAUDE_CLI_IDLE_INTERVAL - 1))
        self.assertTrue(self.job_at(1000 + u.CLAUDE_CLI_IDLE_INTERVAL))

    def test_recent_use_keeps_the_one_minute_pace(self):
        self.w.claude_activity.last_active = 990
        self.assertTrue(self.job_at(1000))
        self.assertTrue(self.job_at(1000 + u.CLAUDE_CLI_INTERVAL))

    def test_starting_to_use_claude_brings_the_query_forward(self):
        self.assertTrue(self.job_at(1000))
        self.assertFalse(self.job_at(1090))
        self.w.claude_activity.last_active = 1095
        self.assertTrue(self.job_at(1100), 'no waiting out the rest of the idle gap')

    def test_a_requested_refresh_skips_the_idle_gap(self):
        self.assertTrue(self.job_at(1000))
        self.w.claude_cli_due = 0.0
        self.assertTrue(self.job_at(1010))

    def test_without_activity_detection_the_pace_stays_one_minute(self):
        for monitor in (Mock(last_active=float('-inf'), mode=u.CLAUDE_ACTIVITY_UNAVAILABLE), Mock(spec=[]), None):
            with self.subTest(monitor=monitor):
                self.w.claude_activity = monitor
                self.assertEqual(self.w._claude_cli_interval(5000), u.CLAUDE_CLI_INTERVAL)


class ClaudeStaleTests(WidgetCase):
    def display_at(self, observed_at, now):
        cli = ProviderSnapshot('claude', 'Claude', 'Pro', True, 80, '5시간 기준 잔여',
                               internal={'source': 'claude_cli', 'quota_observed_at': 1000})
        self.w.claude_cli_snapshot = cli
        self.w.claude_cli_at = observed_at
        missing = p.error_snapshot('claude', 'Claude', 'waiting', '')
        return self.w._claude_display_snapshot(missing, now)

    def test_an_idle_query_that_takes_a_while_does_not_grey_the_card(self):
        # The next idle query starts 5 minutes after the last and needs a few seconds.
        self.assertFalse(self.display_at(1000, 1000 + u.CLAUDE_CLI_IDLE_INTERVAL + 10).stale)

    def test_two_missed_idle_queries_grey_the_card(self):
        self.assertTrue(self.display_at(1000, 1000 + 2 * u.CLAUDE_CLI_IDLE_INTERVAL + 61).stale)


class LogVolumeTests(WidgetCase):
    def test_unchanged_quota_is_logged_once(self):
        snap = ProviderSnapshot('chatgpt', 'GPT', 'Plus', True, 80, '', main_limits=[quota(80)])
        with self.assertLogs('ai_usage.activity', 'DEBUG') as logs:
            for _ in range(5):
                self.w._log_quota('chatgpt', snap)
            self.w._log_quota('chatgpt', ProviderSnapshot('chatgpt', 'GPT', 'Plus', True, 79, '', main_limits=[quota(79)]))
        self.assertEqual(sum('quota raw/display' in line for line in logs.output), 2)

    def test_poll_interval_is_logged_when_the_cadence_changes(self):
        with self.assertLogs('ai_usage.activity', 'DEBUG') as logs:
            for interval in (30.1, 30.9, 31.4, 2.0, 2.1, 1.9, 30.5):
                self.w._log_interval('chatgpt', interval)
        self.assertEqual(sum('request interval' in line for line in logs.output), 3)

    def test_log_keeps_several_files(self):
        source = Path(u.__file__).read_text(encoding='utf-8')
        self.assertIn("backupCount=4", source)


class QueryFolderTests(unittest.TestCase):
    def test_the_usage_query_runs_in_its_own_empty_folder(self):
        with tempfile.TemporaryDirectory() as base:
            calls = []

            def run(args, **kwargs):
                calls.append(kwargs.get('cwd'))
                return Mock(stdout=b'', returncode=0)

            with patch.dict(p.os.environ, {'APPDATA': base}), \
                    patch.object(p, '_claude_cli_executable', return_value=Path('claude.exe')), \
                    patch('claude_integration.claude_ready', return_value=(True, '')), \
                    patch('cli_process.run', side_effect=run):
                p.fetch_claude_cli()
            folder = Path(base) / 'AiUsageWidget' / 'claude-query'
            self.assertTrue(calls)
            self.assertTrue(all(Path(cwd) == folder for cwd in calls), calls)
            self.assertTrue(folder.is_dir())
            self.assertEqual(list(folder.iterdir()), [])

    def test_an_unusable_folder_falls_back_to_the_current_one(self):
        with patch.object(p.Path, 'mkdir', side_effect=OSError):
            self.assertIsNone(p._claude_query_dir())


if __name__ == '__main__':
    unittest.main()
