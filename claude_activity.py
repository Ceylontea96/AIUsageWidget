"""Tell whether Claude Code is working right now, from session state only.

Claude Code keeps one small file per live session, `<config>/sessions/<pid>.json`,
with a `status` of busy, waiting or idle. It is read every 0.25 s. That file is
not a documented interface, so `claude agents --json`, the supported way to read
session state from outside Claude Code, checks it: once for each Claude Code
version, when the first real session of that version shows up, and again if
the files stop parsing. If the files cannot be trusted the monitor polls
`claude agents --json` every 5 s instead. If neither works, Claude simply shows
no activity; quota polling is unaffected.

Only `<digits>.json` files are opened, and only their state fields are read.
The `.key` files beside them and every conversation transcript stay closed.

The widget's own `claude` runs (usage polls, version checks, logins, and the
check above) are Claude Code sessions too. They are recognised as descendants
of the widget process, matched by PID and creation time so a reused PID cannot
pass, and never count. `entrypoint` is not used for this: it follows the
environment a process inherits, so it cannot tell who started a session.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from codex_activity import LOG

SCAN_INTERVAL = 0.25
# Two scans must agree a session stopped before the bar drops, so a scheduling
# hiccup cannot flicker it.
END_GRACE = 0.5
AGENTS_INTERVAL = 5.0
AGENTS_TIMEOUT = 5.0
PARSE_FAILURES_BEFORE_CHECK = 3
MAX_SESSION_BYTES = 16384
MAX_ANCESTRY = 16

STATUSES = frozenset({'busy', 'waiting', 'idle'})
STATES = frozenset({'working', 'blocked', 'done', 'failed', 'stopped'})
_SESSION_FILE = re.compile(r'^(\d{1,10})\.json$')

FAST = 'fast'
AGENTS = 'agents'
UNAVAILABLE = 'unavailable'


def claude_config_dir() -> Path:
    override = os.environ.get('CLAUDE_CONFIG_DIR')
    return Path(override) if override else Path.home() / '.claude'


class WindowsProcesses:
    """Creation time and parent of a process, or None once it is gone."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes, self._wt = ctypes, wintypes
        self._k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self._k32.OpenProcess.restype = wintypes.HANDLE
        self._k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self._k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]

    def created(self, pid):
        """FILETIME of the process start, the same number Claude Code stores as procStart."""
        ct, wt = self._ctypes, self._wt
        handle = self._k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wt.FILETIME() for _ in range(4)]
            if not self._k32.GetProcessTimes(handle, *(ct.byref(t) for t in times)):
                return None
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            self._k32.CloseHandle(handle)

    def parents(self):
        """pid -> parent pid for every running process, from one snapshot."""
        ct, wt = self._ctypes, self._wt

        class Entry(ct.Structure):
            _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                        ('th32DefaultHeapID', ct.c_size_t), ('th32ModuleID', wt.DWORD),
                        ('cntThreads', wt.DWORD), ('th32ParentProcessID', wt.DWORD),
                        ('pcPriClassBase', ct.c_long), ('dwFlags', wt.DWORD), ('szExeFile', ct.c_wchar * 260)]

        snapshot = self._k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == wt.HANDLE(-1).value:
            return {}
        result = {}
        try:
            entry = Entry()
            entry.dwSize = ct.sizeof(Entry)
            ok = self._k32.Process32FirstW(snapshot, ct.byref(entry))
            while ok:
                result[entry.th32ProcessID] = entry.th32ParentProcessID
                ok = self._k32.Process32NextW(snapshot, ct.byref(entry))
        finally:
            self._k32.CloseHandle(snapshot)
        return result


def run_agents_json():
    """`claude agents --json` as a list, or None when it is missing or fails."""
    from claude_integration import resolve_claude_executable
    exe = resolve_claude_executable()
    if exe is None:
        return None
    try:
        done = subprocess.run(
            [str(exe), 'agents', '--json'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=AGENTS_TIMEOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        data = json.loads(done.stdout.decode('utf-8', 'replace')) if done.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return data if isinstance(data, list) else None


def _active(entry):
    return entry.get('status') == 'busy' or entry.get('state') == 'working'


def _session_shape_ok(entry):
    """The fields this monitor relies on, with the types it expects."""
    status, state = entry.get('status'), entry.get('state')
    return (isinstance(entry.get('pid'), int)
            and (status is None or status in STATUSES)
            and (state is None or state in STATES)
            and (status is not None or state is not None))


class ClaudeActivityMonitor:
    def __init__(self, root=None, processes=None, agents=None, own=None, spawn=None):
        self.root = Path(root) if root else claude_config_dir() / 'sessions'
        if processes is None and sys.platform == 'win32':
            processes = WindowsProcesses()
        self.processes = processes
        self.agents = agents or run_agents_json
        pid = os.getpid() if own is None else own[0]
        self.own = (pid, own[1] if own is not None else (processes.created(pid) if processes else None))
        self.spawn = spawn or (lambda job: threading.Thread(target=job, daemon=True, name='claude-agents').start())
        self.mode = FAST if processes is not None else UNAVAILABLE
        self.last_scan = float('-inf')
        self.last_active = float('-inf')
        self.validated_version = None
        self.parse_failures = {}
        self._own_cache = {}
        self._lock = threading.Lock()
        self._busy = False
        self._agents_result = None
        self._agents_at = float('-inf')
        self._agents_active = False
        self.was_visual = False

    # -- state -----------------------------------------------------------
    def visual_active(self, now):
        return now - self.last_active < END_GRACE

    def poll(self, now):
        if self.mode == UNAVAILABLE or now - self.last_scan < SCAN_INTERVAL:
            return
        self.last_scan = now
        if self.mode == FAST:
            active = self._scan_files(now)
        else:
            active = self._poll_agents(now)
        if active:
            self.last_active = now
        visual = self.visual_active(now)
        if visual != self.was_visual:
            LOG.debug('[Claude] activity %s (%s)', 'ACTIVE' if visual else 'IDLE', self.mode)
            self.was_visual = visual

    # -- fast path -------------------------------------------------------
    def _read_sessions(self):
        """(sessions, broken) for every `<digits>.json`; nothing else is opened."""
        sessions, broken = [], []
        try:
            names = [entry.name for entry in os.scandir(self.root) if entry.is_file()]
        except OSError:
            return sessions, broken
        for name in names:
            match = _SESSION_FILE.match(name)
            if not match:
                continue
            try:
                with open(self.root / name, 'rb') as handle:
                    entry = json.loads(handle.read(MAX_SESSION_BYTES).decode('utf-8'))
            except (OSError, ValueError):
                broken.append(name)
                continue
            if not isinstance(entry, dict) or not _session_shape_ok(entry) or entry['pid'] != int(match.group(1)):
                broken.append(name)
                continue
            sessions.append({key: entry.get(key) for key in ('pid', 'status', 'state', 'kind', 'version', 'procStart')})
        return sessions, broken

    def _alive(self, session):
        """The session's own process, checked by creation time so a reused PID fails."""
        created = self.processes.created(session['pid'])
        if created is None:
            return False
        stamp = session.get('procStart')
        return stamp is None or str(stamp) == str(created)

    def _is_own(self, pid, parents):
        """True when `pid` descends from the widget process."""
        own_pid, own_created = self.own
        child_created = self.processes.created(pid)
        for _ in range(MAX_ANCESTRY):
            if pid == own_pid:
                return own_created is None or child_created == own_created
            parent = parents.get(pid)
            if not parent or parent == pid:
                return False
            parent_created = self.processes.created(parent)
            # A parent that started after its child is a reused PID, not the real parent.
            if parent_created is None or child_created is None or parent_created > child_created:
                return False
            pid, child_created = parent, parent_created
        return False

    def _scan_files(self, now):
        # Apply a finished check first, or its version still looks unchecked
        # and a second identical check would start.
        self._take_check_result()
        sessions, broken = self._read_sessions()
        for name in list(self.parse_failures):
            if name not in broken:
                del self.parse_failures[name]
        for name in broken:
            # One unreadable read is a file being rewritten; a run of them is a changed format.
            self.parse_failures[name] = self.parse_failures.get(name, 0) + 1
        if any(count >= PARSE_FAILURES_BEFORE_CHECK for count in self.parse_failures.values()):
            LOG.debug('[Claude] session files no longer parse; checking with agents --json')
            self.parse_failures.clear()
            self._start_check(None, reason='parse')
        live = [s for s in sessions if self._alive(s)]
        # Who started a process never changes while it lives, so each session is
        # judged once; the process snapshot is only taken for a new one.
        keys = {(s['pid'], str(s.get('procStart'))) for s in live}
        self._own_cache = {k: v for k, v in self._own_cache.items() if k in keys}
        fresh = [s for s in live if (s['pid'], str(s.get('procStart'))) not in self._own_cache]
        if fresh:
            parents = self.processes.parents()
            for s in fresh:
                self._own_cache[(s['pid'], str(s.get('procStart')))] = self._is_own(s['pid'], parents)
        mine = [s for s in live if not self._own_cache[(s['pid'], str(s.get('procStart')))]]
        if mine:
            version = mine[0].get('version')
            if version != self.validated_version:
                self._start_check(mine, reason='version')
        return any(_active(s) for s in mine)

    # -- agents --json ----------------------------------------------------
    def _start_check(self, sessions, reason):
        with self._lock:
            if self._busy:
                return
            self._busy = True
        version = sessions[0].get('version') if sessions else None

        def job():
            result = self.agents()
            outcome = self._compare(sessions, result)
            if outcome == 'retry':
                time.sleep(1.0)
                fresh, _ = self._read_sessions()
                outcome = self._compare(fresh or sessions, self.agents())
            with self._lock:
                self._agents_result = (reason, version, outcome)
                self._busy = False

        self.spawn(job)

    @staticmethod
    def _compare(sessions, agents):
        """ok / inconclusive / mismatch / retry / unsupported, by shape rather than value.

        The two reads are a few hundred ms apart, so a status may legitimately
        change between them; a disagreement earns one retry before it counts.
        """
        if agents is None:
            return 'unsupported'
        if not sessions:
            return 'inconclusive'
        listed = {a.get('pid'): a for a in agents if isinstance(a, dict)}
        shared = [s for s in sessions if s['pid'] in listed]
        if not shared:
            # Not listing a session is the command's choice, not a broken file.
            return 'inconclusive'
        for session in shared:
            if not _session_shape_ok(listed[session['pid']]):
                return 'mismatch'
        for session in shared:
            if listed[session['pid']].get('status') != session.get('status'):
                return 'retry'
        return 'ok'

    def _take_check_result(self):
        with self._lock:
            result, self._agents_result = self._agents_result, None
        if result is None:
            return
        reason, version, outcome = result
        LOG.debug('[Claude] agents --json check (%s): %s', reason, outcome)
        if reason == 'version' and outcome in ('ok', 'inconclusive', 'unsupported'):
            # Readable files with nothing to compare against are still trusted,
            # and asking again every scan would only spawn more processes.
            self.validated_version = version
        elif outcome in ('mismatch', 'retry') or (reason == 'parse' and outcome != 'ok'):
            self.mode = UNAVAILABLE if outcome == 'unsupported' else AGENTS
            LOG.debug('[Claude] session files not trusted; mode %s', self.mode)

    def _poll_agents(self, now):
        with self._lock:
            result = self._agents_result
            if result is not None and result[0] == 'poll':
                self._agents_result = None
                self._agents_active = result[2]
            due = not self._busy and now - self._agents_at >= AGENTS_INTERVAL
            if due:
                self._busy = True
                self._agents_at = now
        if due:
            def job():
                agents = self.agents()
                active = False
                if agents is not None and self.processes is not None:
                    parents = self.processes.parents()
                    active = any(_active(a) for a in agents if isinstance(a, dict)
                                 and not (isinstance(a.get('pid'), int) and self._is_own(a['pid'], parents)))
                with self._lock:
                    self._agents_result = ('poll', None, active)
                    self._busy = False
                if agents is None:
                    self.mode = UNAVAILABLE
            self.spawn(job)
        return self._agents_active
