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
import time
from dataclasses import dataclass
from pathlib import Path

from providers import snapshot_from_dict


def auth_paths():
    codex = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'auth.json'
    db = Path(os.environ.get('APPDATA', '')) / 'Cursor/User/globalStorage/state.vscdb'
    # WAL/SHM change constantly while Cursor is open; watching them retriggered
    # a full usage refetch about every 10s and made the widget flash.
    return {'chatgpt': [codex], 'cursor': [db]}


def login_present(key, paths=None):
    return any(path.is_file() for path in (paths or auth_paths()).get(key, []))


def _first_file(paths):
    for path in paths:
        if path and Path(path).is_file():
            return Path(path)
    return None


def codex_cli_path():
    found = shutil.which('codex') or shutil.which('codex.exe')
    if found:
        return Path(found)
    home = Path.home()
    local = Path(os.environ.get('LOCALAPPDATA', ''))
    return _first_file((
        home / '.codex' / 'bin' / 'codex.exe',
        home / '.local' / 'bin' / 'codex.exe',
        local / 'Programs' / 'codex' / 'codex.exe',
        Path(os.environ.get('APPDATA', '')) / 'npm' / 'codex.cmd',
    ))


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
    if login_present(key, paths):
        return '로그인 감지됨'
    if key == 'chatgpt':
        if codex_cli_path():
            return 'CLI 있음 · 이 PC에서 로그인 없음'
        return 'Codex CLI 없음 · ChatGPT 앱만으로는 안 됨'
    if cursor_app_path():
        return '앱 있음 · 이 PC에서 로그인 없음'
    return 'Cursor 앱 없음'


def prepare_action(key):
    if key == 'chatgpt':
        if login_present(key) or codex_cli_path():
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
        if not snap.ok or snap.stale or snap.hero_percent is None:
            return None
        remaining, _ = limiting_quota(snap)
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
    values = [(bar.remaining_percent, bar.label) for bar in snap.bars
              if bar.remaining_percent is not None]
    if not values and snap.hero_percent is not None:
        values.append((snap.hero_percent, snap.hero_caption))
    return min(values, key=lambda value: value[0])


@dataclass
class Slot:
    generation: int
    started: float
    process: subprocess.Popen
    expired: bool = False


class PollRunner:
    """At most one process per provider. UI deadlines reject late results."""
    def __init__(self, timeout=15, worker=None):
        self.timeout = timeout
        self.worker = Path(worker or Path(__file__).with_name('poll_worker.py'))
        self.slots = {}
        self.results = queue.Queue()
        self.generation = 0
        self.plan_cache = None

    def start(self, key, now):
        if key in self.slots:
            return False
        if self.plan_cache and now >= self.plan_cache[1]:
            self.plan_cache = None
        exe = Path(sys.executable)
        if exe.name.lower() == 'pythonw.exe':
            exe = exe.with_name('python.exe')
        args = [str(exe), '-B', str(self.worker), key]
        if key == 'cursor' and self.plan_cache:
            args.append(self.plan_cache[0])
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
                events.append((key, None, '조회 시간이 15초를 초과했습니다. 자동 재시도합니다.'))
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
                if key == 'cursor' and snap.ok and not self.plan_cache:
                    self.plan_cache = (snap.plan, now + 1800)
                events.append((key, snap, ''))
            except (ValueError, TypeError, AttributeError):
                events.append((key, None, '조회에 실패했습니다. 자동 재시도합니다.'))
        return events

    def close(self):
        for key in list(self.slots):
            self.cancel(key)


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
