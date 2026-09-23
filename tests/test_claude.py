import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import claude_integration as integration
import providers
import usage_widget as widget


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for patcher in (
            patch.object(integration.shutil, 'which', return_value=None),
            patch.object(integration.Path, 'home', return_value=self.root / 'home'),
            patch.dict(os.environ, APPDATA=str(self.root / 'roaming'), LOCALAPPDATA=str(self.root / 'local')),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def executable(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_store_desktop_version_order_and_shared_resolver(self):
        base = 'local/Packages/Claude_test/LocalCache/Roaming/Claude/claude-code'
        self.executable(base + '/2.1.99/claude.exe')
        latest = self.executable(base + '/2.1.275/claude.exe')
        self.assertEqual(integration.resolve_claude_executable(), latest)
        self.assertEqual(providers._claude_cli_executable(), latest)

    def test_native_install_without_path_takes_priority(self):
        native = self.executable('home/.local/bin/claude.exe')
        self.executable('roaming/Claude/claude-code/2.1.275/claude.exe')
        self.assertEqual(integration.resolve_claude_executable(), native)

    def test_roaming_desktop_fallback(self):
        exe = self.executable('roaming/Claude/claude-code/2.1.275/claude.exe')
        self.assertEqual(integration.resolve_claude_executable(), exe)

    def test_path_keeps_priority(self):
        with patch.object(integration.shutil, 'which', return_value='custom/claude.exe'):
            self.assertEqual(integration.resolve_claude_executable(), Path('custom/claude.exe'))

    def test_missing_cli(self):
        self.assertIsNone(integration.resolve_claude_executable())

    def fetch_with_auth(self, logged_in):
        self.executable('home/.local/bin/claude.exe')
        body = {'type': 'control_response', 'response': {'subtype': 'success', 'response': {
            'rate_limits_available': False, 'rate_limits': None}}}
        results = [SimpleNamespace(stdout=json.dumps(body).encode()),
                   SimpleNamespace(stdout=json.dumps({'loggedIn': logged_in}).encode())]
        with patch.object(subprocess, 'run', side_effect=results), patch.object(integration, 'claude_ready', return_value=(True, 'test')):
            return providers.fetch_claude_cli()

    def test_signed_out_is_actionable(self):
        snap = self.fetch_with_auth(False)
        self.assertFalse(snap.ok)
        self.assertTrue(snap.internal['requires_login'])
        self.assertIn('Claude 로그인', snap.error)

    def test_signed_in_without_quota_is_not_mislabeled(self):
        snap = self.fetch_with_auth(True)
        self.assertNotIn('requires_login', snap.internal)
        self.assertIn('한도를 제공하지', snap.error)

    def test_worker_error_survives_cache_refresh(self):
        error = self.fetch_with_auth(False)
        state = SimpleNamespace(snapshots={}, claude_cli_snapshot=None, claude_cli_error=error)
        empty_cache = providers.error_snapshot('claude', 'Claude', 'waiting', '')
        self.assertIs(widget.UsageWidget._claude_display_snapshot(state, empty_cache, 100), error)

    def test_fresh_quota_replaces_error(self):
        state = SimpleNamespace(snapshots={}, claude_cli_snapshot=None, claude_cli_error=self.fetch_with_auth(False))
        quota = providers.ProviderSnapshot('claude', 'Claude', 'Claude', True, 75, '5시간 기준 잔여')
        self.assertIs(widget.UsageWidget._claude_display_snapshot(state, quota, 100), quota)

    def test_login_uses_discovered_cli_without_prompt(self):
        exe = self.executable('home/.local/bin/claude.exe')
        with patch.object(integration.subprocess, 'Popen') as launch:
            integration.start_claude_login()
        self.assertEqual(launch.call_args.args[0], [str(exe), 'auth', 'login'])


if __name__ == '__main__':
    unittest.main()
