"""Bounded polling, file watching and transition-only notifications."""
from __future__ import annotations

import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from providers import snapshot_from_dict
from quota_policy import limiting_quota as canonical_limiting_quota


def auth_paths():
    codex = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'auth.json'
    db = Path(os.environ.get('APPDATA', '')) / 'Cursor/User/globalStorage/state.vscdb'
    # WAL/SHM change constantly while Cursor is open; watching them retriggered
    # a full usage refetch about every 10s and made the widget flash.
    return {'chatgpt': [codex], 'cursor': [db]}


def login_present(key, paths=None):
    if key == 'claude':
        from claude_integration import is_installed
        return is_installed()
    return any(path.is_file() for path in (paths or auth_paths()).get(key, []))


def _first_file(paths):
    for path in paths:
        if path and Path(path).is_file():
            return Path(path)
    return None


def codex_cli_path():
    # The same lookup the GPT read uses, so the button and the read agree.
    from codex_app_server import find_codex
    return find_codex()


def cursor_app_path():
    found = shutil.which('cursor') or shutil.which('Cursor.exe')
    if found:
        return Path(found)
    local = Path(os.environ.get('LOCALAPPDATA', ''))
    return _first_file((
        local / 'Programs' / 'cursor' / 'Cursor.exe',
        local / 'cursor' / 'Cursor.exe',
        Path(os.environ.get('PROGRAMFILES', '')) / 'Cursor' / 'Cursor.exe',
    ))


def login_status(key, paths=None):
    if key == 'claude':
        from claude_integration import integration_label
        return integration_label()
    if key == 'chatgpt' and not codex_cli_path():
        return 'Codex CLI 없음 · ChatGPT 앱만으로는 안 됨'
    if login_present(key, paths):
        return '로그인 감지됨'
    if key == 'chatgpt':
        return 'CLI 있음 · 이 PC에서 로그인 없음'
    if cursor_app_path():
        return '앱 있음 · 이 PC에서 로그인 없음'
    return 'Cursor 앱 없음'


def prepare_action(key):
    if key == 'claude':
        from claude_integration import conflict_state, is_installed
        if conflict_state() == 'conflict':
            return '충돌 확인', 'claude-conflict'
        if is_installed():
            return '연동 해제', 'claude-uninstall'
        return 'Claude 연동', 'claude-setup'
    if key == 'chatgpt':
        # GPT is read through the Codex CLI, so a login left behind by the
        # desktop app alone is not enough to skip installing it.
        if codex_cli_path():
            return '로그인', 'codex-login'
        return '설치하고 로그인', 'codex-install'
    if login_present(key) or cursor_app_path():
        return 'Cursor 열기', 'cursor-open'
    return '설치하고 열기', 'cursor-install'


def tool_setup_command(action, codex=False, cursor=False):
    script = Path(__file__).resolve().parent / 'setup_login.ps1'
    command = [
        'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', str(script), '-Action', action,
    ]
    if codex:
        command.append('-Codex')
    if cursor:
        command.append('-Cursor')
    return command


def start_tool_setup(action, *, codex=False, cursor=False):
    kwargs = {}
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NEW_CONSOLE
    subprocess.Popen(tool_setup_command(action, codex=codex, cursor=cursor), **kwargs)


def fingerprint(paths):
    result = []
    for path in paths:
        try:
            stat = path.stat()
            result.append((stat.st_mtime_ns, stat.st_size, stat.st_ino))
        except OSError:
            result.append(None)
    return tuple(result)


class AuthWatcher:
    def __init__(self, paths=None):
        self.paths = paths or auth_paths()
        self.previous = {key: fingerprint(value) for key, value in self.paths.items()}
        self.last_trigger = dict.fromkeys(self.paths, float('-inf'))

    def changed(self, now):
        changed = []
        for key, paths in self.paths.items():
            current = fingerprint(paths)
            if current != self.previous[key]:
                # Cursor writes non-auth state to its WAL too. Coalesce activity.
                if now - self.last_trigger[key] >= 10:
                    changed.append(key)
                    self.last_trigger[key] = now
                    self.previous[key] = current
        return changed


class AlertGate:
    def __init__(self, state=None):
        if not isinstance(state, dict):
            state = {}
        self.state = {k: v for k, v in (state or {}).items() if isinstance(v, int) and 0 <= v <= 2}

    def observe(self, key, snap):
        """Alert on the whole provider, judged by every global main quota.

        Scoped quota is deliberately absent: one exhausted feature must not
        announce that the provider itself is finished.
        """
        if not snap.ok or snap.stale:
            return None
        quota = canonical_limiting_quota(snap)
        if quota is None:
            return None
        remaining = float(quota.remaining_percent)
        severity = 2 if snap.blocked or remaining <= 0 else 1 if remaining <= 10 else 0
        previous = self.state.get(key, 0)
        # Rearm only after clear recovery, avoiding repeated 10% boundary noise.
        if remaining > 12 and not snap.blocked:
            self.state[key] = 0
            return None
        if severity > previous:
            self.state[key] = severity
            return severity
        return None


def limiting_quota(snap):
    """Legacy tuple view of the canonical alert target."""
    quota = canonical_limiting_quota(snap)
    if quota is None:
        return snap.hero_percent, snap.hero_caption
    return quota.remaining_percent, quota.display_name


@dataclass
class Slot:
    generation: int
    started: float
    process: subprocess.Popen
    expired: bool = False


class _ThreadHandle:
    """What a slot needs from a worker process, for a job run on a thread."""

    def __init__(self, interrupt):
        self.returncode = None
        self._interrupt = interrupt

    def poll(self):
        return self.returncode

    def kill(self):
        if self.returncode is None:
            self._interrupt()


class CodexJob:
    """GPT usage from the widget's own Codex app-server, run on a thread.

    Codex answers with its own sign-in, so no token is read here. The server
    stays up between polls; a cancelled or stuck read restarts it.
    """

    def __init__(self, server):
        self.server = server

    def run(self):
        from providers import error_snapshot, fetch_chatgpt, snapshot_to_dict
        try:
            snap = fetch_chatgpt(self.server.read_rate_limits)
        except Exception as exc:
            message = str(exc) if isinstance(exc, RuntimeError) else '로그인 상태와 연결을 확인하세요.'
            snap = error_snapshot('chatgpt', 'chatgpt', message, '', getattr(exc, 'retry_after', ''))
        # The same JSON contract a worker process prints, so poll() is shared.
        return json.loads(json.dumps(snapshot_to_dict(snap), ensure_ascii=True, allow_nan=False))

    def interrupt(self):
        self.server.restart()

    def reset(self):
        self.server.restart()

    def close(self):
        self.server.close()


def worker_python():
    exe = Path(sys.executable)
    if exe.name.lower() == 'pythonw.exe':
        exe = exe.with_name('python.exe')
    return exe


class WorkerJob:
    """One provider read through a poll_worker that stays up between polls.

    The provider's login is loaded and kept inside that worker process, never
    in the widget. A fast poll every two seconds then costs one request, not
    a new Python process, a database open and a TLS handshake each time. A
    cancelled, stuck or dead worker is replaced on the next read.
    """

    LINE_LIMIT = 1024 * 1024

    def __init__(self, key, worker=None, timeout=20.0, popen=subprocess.Popen):
        self.key = key
        self.worker = Path(worker or Path(__file__).with_name('poll_worker.py'))
        self.timeout = timeout
        self._popen = popen
        self._lock = threading.Lock()
        self._state = threading.Lock()
        self._process = None
        self._lines = None
        self.starts = 0

    def run(self):
        with self._lock:
            process, lines = self._ensure_started()
            try:
                process.stdin.write((self.key + '\n').encode('ascii'))
                process.stdin.flush()
            except (OSError, ValueError):
                self._stop(process)
                return None
            try:
                line = lines.get(timeout=self.timeout)
            except queue.Empty:
                line = None
            if line is None:
                # Ended, killed by a cancel, or silent too long: start afresh.
                self._stop(process)
                return None
        payload = json.loads(line.decode('utf-8'))
        return payload if isinstance(payload, dict) and payload else None

    def interrupt(self):
        self._stop()

    def reset(self):
        self._stop()

    def close(self):
        self._stop(graceful=True)

    @property
    def running(self):
        process = self._process
        return process is not None and process.poll() is None

    def _ensure_started(self):
        with self._state:
            process, lines = self._process, self._lines
        if process is not None and process.poll() is None:
            return process, lines
        self._stop(process)
        process = self._popen(
            [str(worker_python()), '-B', str(self.worker), '--serve'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        lines = queue.Queue()
        with self._state:
            self._process, self._lines = process, lines
        self.starts += 1
        threading.Thread(target=self._read, args=(process, lines), daemon=True,
                         name='quota-worker-' + self.key).start()
        return process, lines

    def _read(self, process, lines):
        try:
            for raw in iter(lambda: process.stdout.readline(self.LINE_LIMIT + 1), b''):
                if len(raw) > self.LINE_LIMIT:
                    break
                lines.put(raw)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    def _stop(self, process=None, graceful=False):
        with self._state:
            if process is None or process is self._process:
                process, self._process, self._lines = self._process, None, None
        if process is None:
            return
        if graceful and process.poll() is None:
            try:
                process.stdin.close()   # the worker exits at end of input
                process.wait(timeout=2)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass


class PollRunner:
    """At most one job per provider. UI deadlines reject late results.

    Providers run in a short-lived worker process by default. A provider
    listed in `inprocess` runs on a thread here instead (Codex's app-server,
    or a long-lived worker), with the same slot, deadline and result rules.
    """
    def __init__(self, timeout=15, worker=None, inprocess=None):
        self.timeout = timeout
        self.worker = Path(worker or Path(__file__).with_name('poll_worker.py'))
        self.inprocess = dict(inprocess or {})
        self.slots = {}
        self.results = queue.Queue()
        self.generation = 0

    def start(self, key, now):
        if key in self.slots:
            return False
        job = self.inprocess.get(key)
        if job is not None:
            return self._start_inprocess(key, job, now)
        args = [str(worker_python()), '-B', str(self.worker), key]
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        self.generation += 1
        slot = Slot(self.generation, now, process)
        self.slots[key] = slot

        def collect():
            payload = None
            try:
                stdout, _ = process.communicate(timeout=self.timeout)
                if process.returncode == 0 and len(stdout) <= 1024 * 1024:
                    payload = json.loads(stdout.decode('utf-8'))
            except (subprocess.TimeoutExpired, OSError, ValueError, UnicodeError):
                try:
                    process.kill()
                    process.communicate(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            finally:
                self.results.put((key, slot.generation, payload))

        threading.Thread(target=collect, daemon=True, name='quota-reader-' + key).start()
        return True

    def _start_inprocess(self, key, job, now):
        handle = _ThreadHandle(job.interrupt)
        self.generation += 1
        slot = Slot(self.generation, now, handle)
        self.slots[key] = slot

        def run():
            payload = None
            try:
                payload = job.run()
            except Exception:
                payload = None
            finally:
                # Mark done before queueing, so poll() may release the slot.
                handle.returncode = 0
                self.results.put((key, slot.generation, payload))

        threading.Thread(target=run, daemon=True, name='quota-reader-' + key).start()
        return True

    def reset(self, key):
        """Forget a provider's long-lived state, e.g. after its login changed."""
        job = self.inprocess.get(key)
        if job is not None:
            job.reset()

    def cancel(self, key):
        slot = self.slots.get(key)
        if slot:
            slot.expired = True
            try:
                slot.process.kill()
            except OSError:
                pass

    def poll(self, now):
        events = []
        for key, slot in self.slots.items():
            if not slot.expired and now - slot.started >= self.timeout:
                self.cancel(key)
                events.append((key, None, f'조회 시간이 {self.timeout:g}초를 초과했습니다. 자동 재시도합니다.'))
        while True:
            try:
                key, generation, payload = self.results.get_nowait()
            except queue.Empty:
                break
            slot = self.slots.get(key)
            if not slot or generation != slot.generation:
                continue
            # Do not release a slot while its OS process is still alive.
            if slot.process.poll() is None:
                self.results.put((key, generation, payload))
                break
            del self.slots[key]
            if slot.expired:
                continue
            try:
                if not isinstance(payload, dict) or payload.get('key') != key:
                    raise ValueError('invalid worker response')
                snap = snapshot_from_dict(payload)
                snap.stale = False
                events.append((key, snap, ''))
            except (ValueError, TypeError, AttributeError):
                events.append((key, None, '조회에 실패했습니다. 자동 재시도합니다.'))
        return events

    def close(self):
        for key in list(self.slots):
            self.cancel(key)
        for job in self.inprocess.values():
            try:
                job.close()
            except Exception:
                pass


def session_locked():
    """None means unknown; never infer unlock from a failed API call."""
    from ctypes import wintypes as wt

    class Level1(ctypes.Structure):
        _fields_ = [('session', wt.DWORD), ('state', ctypes.c_int), ('flags', wt.LONG),
                    ('station', wt.WCHAR * 33), ('user', wt.WCHAR * 21), ('domain', wt.WCHAR * 18),
                    ('times', ctypes.c_longlong * 5), ('counters', wt.DWORD * 6)]

    class Info(ctypes.Structure):
        _fields_ = [('level', wt.DWORD), ('data', Level1)]

    try:
        api = ctypes.WinDLL('wtsapi32', use_last_error=True)
        api.WTSQuerySessionInformationW.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.DWORD)]
        api.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        pointer = ctypes.c_void_p()
        size = wt.DWORD()
        if not api.WTSQuerySessionInformationW(None, 0xFFFFFFFF, 25, ctypes.byref(pointer), ctypes.byref(size)):
            return None
        try:
            if size.value < ctypes.sizeof(Info):
                return None
            info = ctypes.cast(pointer, ctypes.POINTER(Info)).contents
            if info.level != 1:
                return None
            if info.data.state != 0:  # disconnected or inactive session
                return True
            return {0: True, 1: False}.get(info.data.flags)
        finally:
            api.WTSFreeMemory(pointer)
    except (AttributeError, OSError):
        return None


class ToastSender:
    def __init__(self):
        self.queue = queue.Queue(maxsize=8)
        self.results = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name='quota-toasts').start()

    def send(self, key, title, body):
        try:
            self.queue.put_nowait({'key': key, 'title': title, 'body': body})
        except queue.Full:
            self.results.put('알림 대기열이 가득 찼습니다.')

    def _run(self):
        powershell = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        script = Path(__file__).with_name('toast.ps1')
        while True:
            payload = self.queue.get()
            try:
                result = subprocess.run(
                    [str(powershell), '-NoProfile', '-NonInteractive', '-File', str(script)],
                    input=json.dumps(payload, ensure_ascii=True).encode('ascii'),
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
                self.results.put('' if result.returncode == 0 else 'Windows 알림 설정을 확인하세요.')
            except (OSError, subprocess.TimeoutExpired):
                self.results.put('Windows 알림을 보내지 못했습니다.')
