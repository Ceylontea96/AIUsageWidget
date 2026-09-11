import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from providers import ProviderSnapshot, QuotaBar
from runtime import AlertGate, AuthWatcher, PollRunner, login_present, login_status, prepare_action, tool_setup_command


def snap(value, **kwargs):
    return ProviderSnapshot('chatgpt','Codex','Plus',True,value,'',**kwargs)


class RuntimeTests(unittest.TestCase):
    def test_alert_transitions_and_hysteresis(self):
        gate=AlertGate()
        self.assertIsNone(gate.observe('chatgpt',snap(50)))
        self.assertEqual(gate.observe('chatgpt',snap(10)),1)
        for value in (9,11,10,8):self.assertIsNone(gate.observe('chatgpt',snap(value)))
        self.assertEqual(gate.observe('chatgpt',snap(0)),2)
        self.assertIsNone(gate.observe('chatgpt',snap(0)))
        gate.observe('chatgpt',snap(13))
        self.assertEqual(gate.observe('chatgpt',snap(9)),1)

    def test_no_stale_or_failed_alerts(self):
        gate=AlertGate()
        self.assertIsNone(gate.observe('chatgpt',snap(0,stale=True)))
        self.assertIsNone(gate.observe('chatgpt',replace(snap(0),ok=False)))
        self.assertEqual(gate.state,{})

    def test_restart_does_not_repeat(self):
        gate=AlertGate({'chatgpt':2})
        self.assertIsNone(gate.observe('chatgpt',snap(0)))

    def test_server_restriction_alerts_even_with_remaining(self):
        self.assertEqual(AlertGate().observe('chatgpt',snap(80,blocked=True)),2)

    def test_independent_providers(self):
        gate=AlertGate()
        self.assertEqual(gate.observe('chatgpt',snap(5)),1)
        self.assertEqual(gate.observe('cursor',snap(5)),1)

    def test_cursor_api_limit_even_when_total_is_high(self):
        value=snap(80)
        value.bars=[QuotaBar('API 사용량',0,100,'')]
        gate=AlertGate()
        self.assertEqual(gate.observe('cursor',value),2)
        self.assertIsNone(gate.observe('cursor',value))

    def test_auth_creation_change_delete_and_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'auth.json';wal=Path(directory)/'db-wal'
            watcher=AuthWatcher({'chatgpt':[file],'cursor':[wal]})
            self.assertEqual(watcher.changed(0),[])
            file.write_text('new');self.assertEqual(watcher.changed(1),['chatgpt'])
            file.write_text('changed');self.assertEqual(watcher.changed(2),[])
            self.assertEqual(watcher.changed(11),['chatgpt'])
            wal.write_text('wal');self.assertEqual(watcher.changed(12),['cursor'])
            file.unlink();self.assertEqual(watcher.changed(22),['chatgpt'])

    def test_login_present_checks_files(self):
        with tempfile.TemporaryDirectory() as directory:
            missing=Path(directory)/'none.json'
            found=Path(directory)/'auth.json';found.write_text('{}')
            self.assertFalse(login_present('chatgpt',{'chatgpt':[missing]}))
            self.assertTrue(login_present('chatgpt',{'chatgpt':[found]}))

    def test_login_status_and_prepare_buttons(self):
        with tempfile.TemporaryDirectory() as directory:
            auth=Path(directory)/'auth.json';auth.write_text('{}')
            with patch('runtime.login_present', side_effect=lambda key, paths=None: key=='chatgpt'), patch('runtime.codex_cli_path', return_value=None), patch('runtime.cursor_app_path', return_value=None):
                self.assertEqual(login_status('chatgpt'), '로그인 감지됨')
                self.assertEqual(login_status('cursor'), 'Cursor 앱 없음')
                self.assertEqual(prepare_action('chatgpt'), ('로그인', 'codex-login'))
                self.assertEqual(prepare_action('cursor'), ('설치하고 열기', 'cursor-install'))
            with patch('runtime.login_present', return_value=False), patch('runtime.codex_cli_path', return_value=None), patch('runtime.cursor_app_path', return_value=Path(directory)/'Cursor.exe'):
                self.assertEqual(login_status('chatgpt'), 'Codex CLI 없음 · ChatGPT 앱만으로는 안 됨')
                self.assertEqual(prepare_action('cursor'), ('Cursor 열기', 'cursor-open'))

    def test_tool_setup_command_flags(self):
        command=tool_setup_command('prepare', codex=True, cursor=True)
        self.assertEqual(command[0], 'powershell.exe')
        self.assertIn('-Action', command)
        self.assertIn('prepare', command)
        self.assertIn('-Codex', command)
        self.assertIn('-Cursor', command)
        self.assertTrue(command[command.index('-File')+1].endswith('setup_login.ps1'))

    def test_timeout_kills_process_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=Path(directory)/'worker.py'
            worker.write_text('import time; time.sleep(30)')
            runner=PollRunner(timeout=.2,worker=worker)
            try:
                self.assertTrue(runner.start('chatgpt',time.monotonic()))
                child=runner.slots['chatgpt'].process
                self.assertFalse(runner.start('chatgpt',time.monotonic()))
                events=[]
                for _ in range(60):
                    time.sleep(.02);events+=runner.poll(time.monotonic())
                    if not runner.slots:break
                self.assertFalse(runner.slots)
                self.assertIsNotNone(child.poll())
                self.assertEqual(len(events),1)
                self.assertIsNone(events[0][1])
                self.assertTrue(runner.start('chatgpt',time.monotonic()))
            finally:
                runner.close()
                for slot in runner.slots.values():slot.process.wait(timeout=3)

    def test_cancel_discards_result(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=Path(directory)/'worker.py';worker.write_text('import time; time.sleep(30)')
            runner=PollRunner(timeout=1,worker=worker)
            runner.start('chatgpt',time.monotonic());runner.cancel('chatgpt')
            for _ in range(60):
                time.sleep(.02)
                self.assertEqual(runner.poll(time.monotonic()),[])
                if not runner.slots:break
            self.assertFalse(runner.slots)


if __name__=='__main__':unittest.main(verbosity=2)
