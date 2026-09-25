import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from providers import ProviderSnapshot, QuotaItem, error_snapshot, snapshot_to_dict
from runtime import AlertGate, AuthWatcher, PollRunner, WorkerJob, login_present, login_status, prepare_action, tool_setup_command


def quota(raw_id, remaining, window=18000.0, name='5시간'):
    return QuotaItem(f'chatgpt:main:{raw_id}','chatgpt','main',name,raw_identifier=raw_id,
                     window_seconds=window,window_label=name,used_percent=100-remaining,
                     remaining_percent=remaining,scope='global')


def snap(value, **kwargs):
    kwargs.setdefault('main_limits',[quota('primary_window',value)])
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
        # A second global quota at zero must still alert, whatever the hero says.
        value=snap(80,main_limits=[quota('autoPercentUsed',80,None,'Cursor Models'),
                                   quota('apiPercentUsed',0,None,'Other Models')])
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
            with patch('runtime.login_present', side_effect=lambda key, paths=None: key=='chatgpt'), patch('runtime.codex_cli_path', return_value=Path(directory)/'codex.exe'), patch('runtime.cursor_app_path', return_value=None):
                self.assertEqual(login_status('chatgpt'), '로그인 감지됨')
                self.assertEqual(login_status('cursor'), 'Cursor 앱 없음')
                self.assertEqual(prepare_action('chatgpt'), ('로그인', 'codex-login'))
                self.assertEqual(prepare_action('cursor'), ('설치하고 열기', 'cursor-install'))
            # A login the desktop app left behind is not a CLI to read through.
            with patch('runtime.login_present', return_value=True), patch('runtime.codex_cli_path', return_value=None):
                self.assertEqual(login_status('chatgpt'), 'Codex CLI 없음 · ChatGPT 앱만으로는 안 됨')
                self.assertEqual(prepare_action('chatgpt'), ('설치하고 로그인', 'codex-install'))
            with patch('runtime.login_present', side_effect=lambda key, paths=None: key=='chatgpt'), patch('runtime.codex_cli_path', return_value=None), patch('runtime.cursor_app_path', return_value=None):
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


SERVE = '''
import sys
count = 0
for raw in iter(sys.stdin.buffer.readline, b''):
    count += 1
    key = raw.decode().strip()
    if key == 'hang':
        import time; time.sleep(30)
    sys.stdout.buffer.write(PAYLOAD.replace(b'"N"', str(count).encode()) + b'\\n')
    sys.stdout.buffer.flush()
'''


class WorkerJobTests(unittest.TestCase):
    def worker(self, directory, payload):
        path = Path(directory) / 'worker.py'
        path.write_text('PAYLOAD = ' + repr(payload) + '\n' + SERVE, encoding='utf-8')
        return path

    def test_one_worker_answers_every_read(self):
        with tempfile.TemporaryDirectory() as directory:
            job = WorkerJob('cursor', worker=self.worker(directory, b'{"key": "cursor", "count": "N"}'), timeout=10)
            try:
                self.assertEqual(job.run(), {'key': 'cursor', 'count': 1})
                self.assertEqual(job.run(), {'key': 'cursor', 'count': 2})
                self.assertEqual(job.starts, 1)
            finally:
                job.close()
            self.assertFalse(job.running)

    def test_interrupt_ends_a_stuck_read_and_the_next_read_starts_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            job = WorkerJob('hang', worker=self.worker(directory, b'{"key": "hang"}'), timeout=20)
            results = []
            thread = threading.Thread(target=lambda: results.append(job.run()))
            try:
                thread.start()
                for _ in range(100):
                    if job.running:
                        break
                    time.sleep(.02)
                started = time.monotonic()
                job.interrupt()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results, [None])
                self.assertLess(time.monotonic() - started, 5)
                job.key = 'cursor'
                self.assertEqual(job.run(), {'key': 'hang'})
                self.assertEqual(job.starts, 2)
            finally:
                job.close()

    def test_runner_delivers_worker_snapshots(self):
        payload = json.dumps(snapshot_to_dict(error_snapshot('cursor', 'Cursor', 'offline', ''))).encode()
        with tempfile.TemporaryDirectory() as directory:
            job = WorkerJob('cursor', worker=self.worker(directory, payload), timeout=10)
            runner = PollRunner(timeout=10, inprocess={'cursor': job})
            try:
                self.assertTrue(runner.start('cursor', time.monotonic()))
                events = []
                for _ in range(250):
                    events += runner.poll(time.monotonic())
                    if events:
                        break
                    time.sleep(.02)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0][1].error, 'offline')
                self.assertFalse(runner.slots)
            finally:
                runner.close()
            self.assertFalse(job.running)

    def test_serve_answers_one_line_per_key(self):
        import io
        import poll_worker
        stdout = io.BytesIO()
        with patch.dict(poll_worker.FETCHERS, {'cursor': lambda: error_snapshot('cursor', 'Cursor', 'x', '')}):
            poll_worker.serve(io.BytesIO(b'cursor\nnope\ncursor\n'), stdout)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0])['key'], 'cursor')
        self.assertEqual(json.loads(lines[1]), {})


if __name__=='__main__':unittest.main(verbosity=2)
