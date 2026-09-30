import ctypes
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cli_process
from tests.support import integration


class FakeProcess:
    """A Popen whose first communicate() times out, as a hung shim's does."""

    def __init__(self, hang_first=True, drain_hangs=False):
        self.pid = 4242
        self.returncode = None
        self.calls = []
        self.hang_first = hang_first
        self.drain_hangs = drain_hangs

    def communicate(self, input=None, timeout=None):
        self.calls.append((input, timeout))
        if len(self.calls) == 1 and self.hang_first:
            raise subprocess.TimeoutExpired('claude', timeout)
        if len(self.calls) > 1 and self.drain_hangs:
            raise subprocess.TimeoutExpired('claude', timeout)
        self.returncode = 0
        return b'{"ok": true}', b'note'

    def poll(self):
        return self.returncode


class RunTests(unittest.TestCase):
    def test_output_comes_back_as_bytes(self):
        process = FakeProcess(hang_first=False)
        with patch.object(cli_process.subprocess, 'Popen', return_value=process) as popen:
            done = cli_process.run(['claude', '-p'], input=b'x\n', timeout=12, cwd='folder')
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b'{"ok": true}', None))
        self.assertEqual(process.calls, [(b'x\n', 12)])
        self.assertEqual(popen.call_args.kwargs['stdin'], subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs['cwd'], 'folder')

    def test_stderr_only_when_asked(self):
        with patch.object(cli_process.subprocess, 'Popen', return_value=FakeProcess(hang_first=False)) as popen:
            done = cli_process.run(['claude', '--version'], timeout=2, stderr=True)
        self.assertEqual(done.stderr, b'note')
        self.assertEqual(popen.call_args.kwargs['stderr'], subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_a_timeout_ends_the_whole_tree_and_bounds_the_wait(self):
        process = FakeProcess()
        with patch.object(cli_process.subprocess, 'Popen', return_value=process), \
                patch.object(cli_process, 'kill_tree') as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                cli_process.run(['claude.cmd'], timeout=1)
        kill.assert_called_once_with(process)
        # subprocess.run() waits for the pipe with no limit here; this must not.
        self.assertEqual(process.calls[1], (None, cli_process.DRAIN_TIMEOUT))

    def test_a_pipe_that_stays_open_still_gives_up(self):
        process = FakeProcess(drain_hangs=True)
        with patch.object(cli_process.subprocess, 'Popen', return_value=process), \
                patch.object(cli_process, 'kill_tree'):
            with self.assertRaises(subprocess.TimeoutExpired):
                cli_process.run(['claude.cmd'], timeout=1)
        self.assertEqual(len(process.calls), 2)

    def test_kill_tree_leaves_a_finished_process_alone(self):
        process = FakeProcess()
        process.returncode = 0
        with patch.object(cli_process.subprocess, 'run') as run:
            cli_process.kill_tree(process)
        run.assert_not_called()

    def test_kill_tree_uses_taskkill_on_the_tree(self):
        process = FakeProcess()
        process.kill = lambda: None
        process.wait = lambda timeout=None: 0
        with patch.object(cli_process.subprocess, 'run') as run:
            cli_process.kill_tree(process)
        self.assertEqual(run.call_args.args[0], ['taskkill', '/PID', '4242', '/T', '/F'])


def _alive(pid):
    kernel = ctypes.WinDLL('kernel32')
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(ctypes.c_void_p(handle), 0) != 0
    finally:
        kernel.CloseHandle(ctypes.c_void_p(handle))


@unittest.skipUnless(sys.platform == 'win32', 'Windows shims')
@integration('starts cmd.exe and Python')
class ShimTimeoutTests(unittest.TestCase):
    def test_a_hung_grandchild_no_longer_holds_the_caller(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            pid_file = folder / 'grandchild.pid'
            sleeper = folder / 'sleeper.py'
            sleeper.write_text(
                'import os, sys, time\n'
                'open(sys.argv[1], "w").write(str(os.getpid()))\n'
                'time.sleep(30)\n', encoding='utf-8')
            # Like npm's claude.cmd: cmd.exe starts a child that inherits the pipe.
            shim = folder / 'tool.cmd'
            shim.write_text(f'@echo off\r\n"{sys.executable}" -B "{sleeper}" "{pid_file}"\r\n', encoding='ascii')
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                cli_process.run([str(shim)], input=b'request\n', timeout=3)
            # subprocess.run() would have waited out the 30 s sleep here.
            self.assertLess(time.monotonic() - started, 12)
            self.assertTrue(pid_file.exists(), 'the grandchild never started; nothing was tested')
            pid = int(pid_file.read_text())
            deadline = time.monotonic() + 5
            while _alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(_alive(pid), 'the grandchild outlived the timeout')


if __name__ == '__main__':
    unittest.main()
