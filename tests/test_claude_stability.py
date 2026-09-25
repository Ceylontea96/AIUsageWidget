import copy
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import claude_bridge as bridge
import claude_integration as integration
import providers


def cache(key='a' * 64, now=1000):
    return {
        'schema_version': 1, 'source': bridge.SOURCE, 'session_key': key,
        'claude_code_version': '2.1.275', 'bridge_seen_at': now, 'quota_observed_at': now,
        'five_hour': {'used_percent': 20, 'remaining_percent': 80, 'resets_at': 2000},
        'seven_day': {'used_percent': 30, 'remaining_percent': 70, 'resets_at': 8000},
    }


class IntegrationStabilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for p in (patch.dict(os.environ, AIUSAGE_CLAUDE_DIR=str(self.root / 'bridge'),
                             AIUSAGE_CLAUDE_SETTINGS=str(self.root / 'settings.json')),
                  patch.object(integration, 'claude_ready', return_value=(True, 'ok')),
                  patch.object(integration, 'claude_version_text', return_value='2.1.275'),
                  patch.object(integration, 'resolve_claude_executable', return_value=Path('claude.exe'))):
            p.start()
            self.addCleanup(p.stop)
        self.original = {'type': 'command', 'command': 'echo user-status', 'padding': 3}

    def settings(self, original=True):
        data = {'theme': 'dark'}
        if original:
            data['statusLine'] = self.original
        integration._write_user_settings(data)

    def test_first_install_without_statusline(self):
        self.settings(False)
        meta = integration.install_statusline()
        self.assertFalse(meta['had_original'])
        self.assertTrue(integration.is_installed())
        self.assertEqual(integration.uninstall_statusline(), 'removed')
        self.assertEqual(integration.read_user_settings(), {'theme': 'dark'})

    def install_with_python(self, python):
        with patch.object(integration, '_python_executable', return_value=python):
            return integration.install_statusline()

    def test_statusline_moves_to_this_python_after_the_old_one_is_removed(self):
        self.settings()
        self.install_with_python(self.root / 'Python312' / 'python.exe')
        self.assertTrue(integration.repair_statusline_command())
        statusline = integration.current_statusline()
        self.assertEqual(statusline['command'], integration.wrapper_command())
        self.assertEqual(integration.conflict_state(), 'installed')
        # The original backup survives, so uninstalling still restores it.
        self.assertEqual(integration.uninstall_statusline(), 'restored')
        self.assertEqual(integration.current_statusline(), self.original)

    def test_statusline_with_a_working_python_is_left_alone(self):
        self.settings()
        python = self.root / 'Python312' / 'python.exe'
        python.parent.mkdir()
        python.write_bytes(b'MZ')
        meta = self.install_with_python(python)
        self.assertFalse(integration.repair_statusline_command())
        self.assertEqual(integration.current_statusline()['command'], meta['command'])

    def test_statusline_the_user_changed_is_never_repaired(self):
        self.settings()
        self.install_with_python(self.root / 'Python312' / 'python.exe')
        edited = dict(integration.current_statusline(), refreshInterval=9)
        data = integration.read_user_settings()
        data['statusLine'] = edited
        integration._write_user_settings(data)
        self.assertFalse(integration.repair_statusline_command())
        self.assertEqual(integration.current_statusline(), edited)

    def test_repeated_install_preserves_first_original(self):
        self.settings()
        first = integration.install_statusline()
        second = integration.install_statusline()
        self.assertEqual(first, second)
        self.assertEqual(second['original_statusline'], self.original)
        self.assertEqual(integration.uninstall_statusline(), 'restored')
        self.assertEqual(integration.read_user_settings(), {'theme': 'dark', 'statusLine': self.original})

    def test_missing_wrapper_recovers_original(self):
        self.settings()
        integration.install_statusline()
        integration.wrapper_script_path().unlink()
        self.assertEqual(integration.conflict_state(), 'recovery')
        integration.install_statusline()
        self.assertTrue(integration.wrapper_script_path().is_file())
        self.assertEqual(bridge.load_original_command(), self.original['command'])
        self.assertEqual(integration.uninstall_statusline(), 'restored')

    def test_missing_or_corrupt_metadata_is_fail_closed(self):
        self.settings()
        integration.install_statusline()
        original_settings = integration.user_claude_settings_path().read_bytes()
        for damaged in (None, b'{', b'{}'):
            with self.subTest(damaged=damaged):
                if damaged is None:
                    bridge.integration_path().unlink(missing_ok=True)
                else:
                    bridge.integration_path().write_bytes(damaged)
                with self.assertRaisesRegex(RuntimeError, '복구'):
                    integration.install_statusline()
                self.assertEqual(integration.user_claude_settings_path().read_bytes(), original_settings)
                self.assertEqual(integration.uninstall_statusline(), 'conflict')

    def test_conflict_preserves_user_change_and_backup(self):
        self.settings()
        integration.install_statusline()
        before = bridge.integration_path().read_bytes()
        changed = {'statusLine': {'type': 'command', 'command': 'echo changed'}}
        integration._write_user_settings(changed)
        with self.assertRaisesRegex(RuntimeError, '변경'):
            integration.install_statusline()
        self.assertEqual(integration.uninstall_statusline(), 'conflict')
        self.assertEqual(bridge.integration_path().read_bytes(), before)
        self.assertEqual(integration.read_user_settings(), changed)

    def test_corrupt_settings_does_not_touch_any_files(self):
        integration.user_claude_settings_path().write_bytes(b'{bad')
        with self.assertRaises(RuntimeError):
            integration.install_statusline()
        self.assertFalse(bridge.integration_path().exists())
        self.assertFalse(integration.wrapper_script_path().exists())
        self.assertEqual(integration.user_claude_settings_path().read_bytes(), b'{bad')

    def test_changed_wrapper_options_are_also_protected(self):
        self.settings()
        integration.install_statusline()
        settings=integration.read_user_settings()
        settings['statusLine']['padding']=9
        integration._write_user_settings(settings)
        with self.assertRaisesRegex(RuntimeError, '변경'):
            integration.install_statusline()
        self.assertEqual(integration.read_user_settings(),settings)
        self.assertEqual(integration.uninstall_statusline(),'conflict')

    def test_failed_settings_write_keeps_recovery_backup(self):
        self.settings()
        with patch.object(integration, '_write_user_settings', side_effect=OSError('denied')):
            with self.assertRaises(OSError):
                integration.install_statusline()
        self.assertEqual(integration.load_integration()['original_statusline'], self.original)
        self.assertEqual(integration.current_statusline(), self.original)

    def test_self_forwarding_is_blocked_even_with_poisoned_backup(self):
        self.settings()
        meta = integration.install_statusline()
        meta['had_original'] = True
        meta['original_statusline'] = integration.installed_statusline_object()
        bridge.atomic_write_json(bridge.integration_path(), meta)
        self.assertEqual(bridge.load_original_command(), '')
        with patch.object(bridge.subprocess, 'run') as run:
            bridge._forward_original(integration.wrapper_command(), b'{}')
        run.assert_not_called()
        with self.assertRaises(RuntimeError):
            integration.install_statusline()

    def test_indirect_forwarding_cannot_recurse(self):
        with patch.dict(os.environ, AIUSAGE_CLAUDE_FORWARDING='1'), patch.object(bridge.subprocess, 'run') as run:
            bridge._forward_original('echo user-status', b'{}')
        run.assert_not_called()

    def test_cleanup_only_owned_artifacts(self):
        self.settings()
        integration.install_statusline()
        bridge.write_session_cache('a' * 64, cache())
        owned = [bridge.bridge_log_path(), bridge.salt_path(),
                 bridge.claude_dir() / '.integration.json.123.tmp',
                 bridge.sessions_dir() / ('.' + 'a' * 64 + '.json.123.tmp')]
        for path in owned:
            path.write_text('test')
        user_files = [bridge.claude_dir() / 'user.txt', bridge.sessions_dir() / 'user.json',
                      bridge.claude_dir() / '.user.123.tmp']
        for path in user_files:
            path.write_text('keep')
        self.assertEqual(integration.uninstall_statusline(), 'restored')
        for path in owned + [integration.wrapper_script_path(), bridge.integration_path(), bridge.sessions_dir() / ('a' * 64 + '.json')]:
            self.assertFalse(path.exists(), str(path))
        for path in user_files:
            self.assertEqual(path.read_text(), 'keep')

    def test_cleanup_failure_does_not_block_settings_restore(self):
        self.settings()
        integration.install_statusline()
        with patch.object(Path, 'unlink', side_effect=PermissionError('locked')):
            self.assertEqual(integration.uninstall_statusline(), 'restored')
        self.assertEqual(integration.current_statusline(), self.original)


class VersionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.exe = Path(temp.name) / 'claude.exe'
        self.exe.write_bytes(b'v1')
        integration.invalidate_version_cache()
        p = patch.object(integration, 'resolve_claude_executable', return_value=self.exe)
        p.start()
        self.addCleanup(p.stop)
        self.reply = SimpleNamespace(returncode=0, stdout='2.1.275 (Claude Code)', stderr='')

    def test_first_lookup_once_and_repeated_lookups_cached(self):
        with patch.object(integration.subprocess, 'run', return_value=self.reply) as run:
            for _ in range(10):
                self.assertTrue(integration.claude_ready()[0])
            self.assertEqual(run.call_count, 1)

    def test_executable_metadata_change_rechecks(self):
        with patch.object(integration.subprocess, 'run', return_value=self.reply) as run:
            integration.claude_version_text()
            self.exe.write_bytes(b'v2 changed')
            integration.claude_version_text()
            self.assertEqual(run.call_count, 2)

    def test_path_change_rechecks(self):
        other = self.exe.with_name('other.exe')
        other.write_bytes(b'v2')
        with patch.object(integration.subprocess, 'run', return_value=self.reply) as run:
            integration.claude_version_text()
            with patch.object(integration, 'resolve_claude_executable', return_value=other):
                integration.claude_version_text()
            self.assertEqual(run.call_count, 2)

    def test_failure_short_ttl_and_forced_refresh(self):
        with patch.object(integration.subprocess, 'run', side_effect=OSError('bad')) as run, patch.object(integration.time, 'monotonic', return_value=10):
            self.assertEqual(integration.claude_version_text(), '')
            self.assertEqual(integration.claude_version_text(), '')
            self.assertEqual(run.call_count, 1)
        with patch.object(integration.subprocess, 'run', return_value=self.reply) as run, patch.object(integration.time, 'monotonic', return_value=41):
            self.assertTrue(integration.claude_version_text())
            integration.claude_version_text(force=True)
            self.assertEqual(run.call_count, 2)

    def test_success_ttl_and_explicit_invalidation(self):
        with patch.object(integration.subprocess,'run',return_value=self.reply) as run:
            with patch.object(integration.time,'monotonic',return_value=10):
                integration.claude_version_text()
            with patch.object(integration.time,'monotonic',return_value=3611):
                integration.claude_version_text()
                self.assertEqual(run.call_count,2)
                integration.invalidate_version_cache()
                integration.claude_version_text()
                self.assertEqual(run.call_count,3)

    def test_ui_background_detection_never_runs_subprocess_on_caller(self):
        called = threading.Event()
        release = threading.Event()
        threads = []
        def run(*args, **kwargs):
            threads.append(threading.get_ident())
            called.set()
            release.wait(2)
            return self.reply
        with patch.object(integration.subprocess, 'run', side_effect=run):
            try:
                self.assertEqual(integration.claude_version_text(background=True), '')
                self.assertTrue(called.wait(2))
                for _ in range(5):
                    integration.claude_version_text(background=True)
                self.assertEqual(len(threads), 1)
                self.assertNotEqual(threads[0], threading.get_ident())
            finally:
                release.set()
                # Finish this one probe before the mock and temporary exe go away.
                for thread in threading.enumerate():
                    if thread.ident in threads:
                        thread.join(2)


class CacheTests(unittest.TestCase):
    def test_valid_unknown_and_optional_fields(self):
        data = cache()
        data['future_field'] = {'anything': True}
        data['five_hour']['future_field'] = 'allowed'
        clean = bridge.validate_session_cache(data, 1000)
        self.assertEqual(clean['five_hour'], cache()['five_hour'])
        self.assertNotIn('future_field', clean)
        self.assertNotIn('last_transcript_mtime', clean)

    def test_invalid_top_level_fields(self):
        for field, values in {
            'schema_version': [None, True, '1', 2], 'source': [None, 'other'],
            'session_key': [None, '', '../bad', True],
            'bridge_seen_at': [None, True, '1000', -1, float('nan'), float('inf'), 5000],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    data = cache(); data[field] = value
                    self.assertIsNone(bridge.validate_session_cache(data, 1000))

    def test_invalid_observed_time_makes_quota_unavailable(self):
        for value in (None, True, '1000', 0, -1, float('nan'), float('inf'), 1001):
            data = cache(); data['quota_observed_at'] = value
            clean = bridge.validate_session_cache(data, 1000)
            self.assertNotIn('five_hour', clean)
            self.assertIsNone(bridge.select_session_cache([clean], 1000))

    def test_invalid_quota_isolated_from_other_window(self):
        for field in ('used_percent', 'remaining_percent', 'resets_at'):
            values = [None, True, '20', float('nan'), float('inf'), -1]
            values.append(101 if field != 'resets_at' else bridge.MAX_TIMESTAMP + 1)
            for value in values:
                with self.subTest(field=field, value=value):
                    data = cache(); data['five_hour'][field] = value
                    clean = bridge.validate_session_cache(data, 1000)
                    self.assertNotIn('five_hour', clean)
                    self.assertIn('seven_day', clean)

    def test_inconsistent_percent_is_unavailable(self):
        data = cache(); data['five_hour']['remaining_percent'] = 90
        self.assertNotIn('five_hour', bridge.validate_session_cache(data, 1000))

    def test_corrupt_files_do_not_hide_valid_session(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, AIUSAGE_CLAUDE_DIR=directory):
            bridge.write_session_cache('a' * 64, cache())
            for key, content in [('b', b'{'), ('c', b'\xff'), ('d', b'null')]:
                (bridge.sessions_dir() / (key * 64 + '.json')).write_bytes(content)
            chosen = bridge.select_session_cache(bridge.list_session_caches(1000), 1000)
            self.assertEqual(chosen['session_key'], 'a' * 64)

    def test_newer_invalid_session_cannot_replace_valid_one(self):
        newer = cache('b' * 64, 1010)
        newer['five_hour']['used_percent'] = False
        newer['seven_day']['remaining_percent'] = 101
        chosen = bridge.select_session_cache([newer, cache()], 1010)
        self.assertEqual(chosen['session_key'], 'a' * 64)

    def test_version_alone_does_not_confirm_feature(self):
        data = cache(); data['claude_code_version'] = '99.1.1'
        data.pop('five_hour'); data.pop('seven_day')
        self.assertIsNone(bridge.select_session_cache([data], 1000))
        self.assertEqual(bridge.extract_whitelist({'version': '99.1.1', 'rate_limits': {'new_schema': 42}})['windows'], {})

    def test_two_second_provider_path_has_no_subprocess(self):
        with patch.object(integration, 'is_installed', return_value=True), patch.object(bridge, 'list_session_caches', return_value=[cache()]), patch.object(subprocess, 'run') as run:
            for _ in range(10):
                self.assertTrue(providers.fetch_claude(1000).ok)
            run.assert_not_called()

    def test_persisted_whitelist_removes_sensitive_unknowns(self):
        data = cache()
        for key in ('session_id','transcript_path','prompt','cwd','project','repo','email','token','cookie','Authorization','credential'):
            data[key] = 'DO_NOT_STORE'
        data['last_transcript_mtime'] = 999
        data['last_window_fingerprint'] = 'DO_NOT_STORE'
        data['claude_code_version'] = '2.1.275 DO_NOT_STORE'
        clean = bridge.sanitize_cache(data)
        self.assertEqual(set(clean), bridge.ALLOWED_CACHE_KEYS)
        self.assertNotIn('DO_NOT_STORE', json.dumps(clean))
        for key in bridge.WINDOW_KEYS:
            self.assertEqual(set(clean[key]), bridge.ALLOWED_WINDOW_KEYS)
        document = Path('CLAUDE_INTEGRATION.md').read_text(encoding='utf-8')
        for key in bridge.ALLOWED_CACHE_KEYS | bridge.ALLOWED_WINDOW_KEYS:
            self.assertIn('`' + key + '`', document)

    def test_mtime_is_heuristic_not_idle_or_future_freshness(self):
        previous = cache(); previous['last_transcript_mtime'] = 999
        windows = {key: previous[key] for key in bridge.WINDOW_KEYS}
        whitelist = {'claude_code_version':'2.1.275', 'windows':windows}
        for now, mtime, expected in ((1005,999,1000), (1005,1003,1005), (2000,1999,1000), (1005,5000,1000)):
            with self.subTest(now=now, mtime=mtime):
                built = bridge.build_session_cache(session_key='a'*64, whitelist=whitelist,
                    previous=previous, transcript_mtime_value=mtime, now=now)
                self.assertEqual(built['quota_observed_at'], expected)
        changed = copy.deepcopy(whitelist)
        changed['windows']['five_hour'].update(used_percent=21,remaining_percent=79)
        built = bridge.build_session_cache(session_key='a'*64, whitelist=changed,
            previous=previous, transcript_mtime_value=None, now=1005)
        self.assertEqual(built['quota_observed_at'],1005)

    def test_statusline_ingest_never_reads_transcript_contents(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, AIUSAGE_CLAUDE_DIR=directory):
            transcript = Path(directory) / 'private.txt'
            transcript.write_text('DO_NOT_STORE')
            real_read = Path.read_text
            def guarded(path, *args, **kwargs):
                self.assertNotEqual(path, transcript)
                return real_read(path, *args, **kwargs)
            payload = {'version':'2.1.275', 'session_id':'private-session', 'transcript_path':str(transcript),
                       'rate_limits':{'five_hour':{'used_percentage':20,'resets_at':2000}}}
            with patch.object(Path, 'read_text', guarded):
                result = bridge.ingest_statusline(json.dumps(payload), now=1000)
            self.assertEqual(result['five_hour']['remaining_percent'],80)
            self.assertNotIn('private',json.dumps(result))


class CliFeatureTests(unittest.TestCase):
    def response(self, body):
        return json.dumps({'type':'control_response','response':{'subtype':'success','request_id':'usage','response':body}})

    def test_unknown_feature_is_unavailable(self):
        for body in ({}, {'rate_limits': []}, {'rate_limits_available':'yes'}, {'rate_limits':{'unknown':{'percent':20}}}):
            self.assertFalse(providers.claude_usage_from_control_output(self.response(body),1000).ok)

    def test_supported_cli_schema_and_percent_scale(self):
        body = {'rate_limits':{'limits':[{'kind':'session','percent':20,'utilization':0.2,'resets_at':'2030-01-01T00:00:00Z'}]}}
        snap=providers.claude_usage_from_control_output(self.response(body),1000)
        self.assertTrue(snap.ok)
        self.assertEqual(snap.hero_percent,80)
        self.assertEqual(snap.plan,'-')
        self.assertTrue(snap.internal['feature_available'])

    def test_subscription_type_is_the_plan_label(self):
        limits = {'limits':[{'kind':'session','percent':20,'resets_at':'2030-01-01T00:00:00Z'}]}
        cases = (
            ({'subscription_type':'pro','rate_limits':limits}, 'Pro'),
            ({'subscriptionType':'max','rate_limits':limits}, 'Max'),
            ({'subscription_type':'team','rate_limits':limits}, 'Team'),
            ({'subscription_type':'enterprise','rate_limits':limits}, 'Enterprise'),
            ({'subscription_type':'max','rate_limit_tier':'default_claude_max_20x','rate_limits':limits}, 'Max 20x'),
            ({'subscription_type':'max','rateLimitTier':'default_claude_max_5x','rate_limits':limits}, 'Max 5x'),
            ({'subscription_type':'not-a-plan','rate_limits':limits}, '-'),
        )
        for body, plan in cases:
            with self.subTest(plan=plan):
                snap = providers.claude_usage_from_control_output(self.response(body), 1000)
                self.assertEqual(snap.plan, plan)

    def test_statusline_keeps_a_plan_learned_from_the_cli(self):
        import usage_widget as widget
        from providers import ProviderSnapshot
        status = ProviderSnapshot('claude','Claude','-',True,80,'',fetched_at=200,
            internal={'source':'claude_statusline','quota_observed_at':200})
        cli = ProviderSnapshot('claude','Claude','Pro',True,80,'',fetched_at=100,
            internal={'source':'claude_cli','quota_observed_at':100})
        state = SimpleNamespace(snapshots={}, claude_cli_snapshot=cli, claude_cli_at=100, claude_cli_error=None)
        shown = widget.UsageWidget._claude_display_snapshot(state, status, 120)
        self.assertEqual(shown.internal['source'], 'claude_statusline')
        self.assertEqual(shown.plan, 'Pro')

    def test_claude_ai_usage_shape_from_newer_cli(self):
        # Claude Code 2.1.280's get_usage, trimmed: window objects, utilization in percent.
        window = {'limit_dollars':None,'used_dollars':None,'remaining_dollars':None,'locked_reason':None}
        body = {'subscription_type':'pro','rate_limits_available':True,'rate_limits':{
            'five_hour':{'utilization':13,'resets_at':'2030-01-01T00:00:00+00:00',**window},
            'seven_day':{'utilization':76,'resets_at':'2030-01-05T00:00:00+00:00',**window},
            'seven_day_opus':None,
            'nimbus_quill':{'utilization':0,'resets_at':None,**window},
            'extra_usage':{'is_enabled':False,'utilization':None},
            'seven_day_breakdown':{'rows':[{'key':'claude_code','percent':99}]},
        }}
        snap=providers.claude_usage_from_control_output(self.response(body),1000)
        self.assertTrue(snap.ok)
        self.assertEqual(snap.plan,'Pro')
        self.assertEqual(sorted(i.remaining_percent for i in snap.main_limits),[24,87])
        # Outside that shape a bare utilization still says nothing about its scale.
        for row in ({'utilization':13}, {'utilization':13,'kind':'five_hour','resets_at':'2030-01-01T00:00:00Z'}):
            body={'rate_limits':{'five_hour':row}}
            self.assertFalse(providers.claude_usage_from_control_output(self.response(body),1000).ok)
        body={'rate_limits':{'five_hour':{'utilization':130,'resets_at':'2030-01-01T00:00:00Z'}}}
        self.assertFalse(providers.claude_usage_from_control_output(self.response(body),1000).ok)

    def test_ambiguous_or_invalid_cli_percent_is_unavailable(self):
        for row in ({'utilization':0.2}, {'percent':20,'utilization':0.8}, {'percent':True},
                    {'percent':float('nan')}, {'percent':101}, {'percent':20,'utilization_scale':'unknown','utilization':0.2}):
            body={'rate_limits':{'limits':[{'kind':'session', **row}]}}
            self.assertFalse(providers.claude_usage_from_control_output(self.response(body),1000).ok)


if __name__ == '__main__':
    unittest.main()
