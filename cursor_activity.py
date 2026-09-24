"""Detect Cursor Agent turns without reading transcript content for quota values."""
import json
import time
from dataclasses import dataclass
from pathlib import Path

from codex_activity import INACTIVITY_TIMEOUT, LOG

SCAN_INTERVAL = 0.75
DISCOVERY_INTERVAL = 12.0
MAX_TRACKED = 128
READ_LIMIT = 65536
END_GRACE = 0.75
STALE_TIMEOUT = 600.0
UNKNOWN_COOLDOWN = 60.0

UNKNOWN = 'unknown'
ACTIVE = 'active'
GRACE = 'grace'
ENDED = 'ended'

_START_TYPES = {'turn_start', 'turn_started', 'user', 'user_message'}
_WORK_TYPES = {
    'assistant', 'assistant_message', 'tool', 'tool_call', 'tool_result',
    'subagent', 'subagent_start', 'subagent_message',
}
_WORK_ROLES = {'assistant', 'tool', 'subagent'}


@dataclass
class TranscriptState:
    offset: int = 0
    partial: bytes = b''
    inode: int = 0
    status: str = UNKNOWN
    last_meaningful_at: float = float('-inf')
    grace_until: float = float('-inf')
    last_unknown_refresh: float = float('-inf')
    last_mtime: float = 0.0
    skipping_line: bool = False


class CursorActivityMonitor:
    def __init__(self, home=None):
        self.root = Path(home) if home else Path.home() / '.cursor' / 'projects'
        self.files = {}
        self.last_activity_time = float('-inf')
        self.last_scan = float('-inf')
        self.last_discovery = float('-inf')
        self.initialized = False
        self.was_fast = False
        self.was_visual = False
        self._missing_logged = False
        self._root_mtime = None
        self._transcript_dirs = []
        self._folders = None

    @staticmethod
    def _classify(event):
        kind = str(event.get('type') or '').lower()
        role = str(event.get('role') or '').lower()
        if kind == 'turn_ended':
            return 'end'
        if role == 'user' or kind in _START_TYPES:
            return 'start'
        if role in _WORK_ROLES or kind in _WORK_TYPES:
            return 'work'
        return 'unknown'

    @staticmethod
    def _inode(stat):
        return int(getattr(stat, 'st_ino', 0) or 0)

    @staticmethod
    def _expire_state(state, now):
        if state.status == GRACE and now >= state.grace_until:
            state.status = ENDED
        elif state.status == ACTIVE and now - state.last_meaningful_at >= STALE_TIMEOUT:
            state.status = ENDED

    def _expire(self, now):
        for state in self.files.values():
            before = state.status
            self._expire_state(state, now)
            if before == ACTIVE and state.status == ENDED:
                LOG.debug('[CursorActivity] stale turn expired')

    def visual_active(self, now):
        self._expire(now)
        return any(state.status in (ACTIVE, GRACE) for state in self.files.values())

    def fast(self, now):
        return self.visual_active(now) or now - self.last_activity_time < INACTIVITY_TIMEOUT

    def _unknown_refresh(self, state, now, allow_refresh, label=None):
        if not allow_refresh or now - state.last_unknown_refresh < UNKNOWN_COOLDOWN:
            return False
        state.last_unknown_refresh = now
        LOG.debug('[CursorActivity] unclassified event refresh')
        return True

    def _apply_event(self, state, event, now, *, event_at=None, allow_refresh=True):
        event_at = now if event_at is None else event_at
        classification = self._classify(event)
        if classification == 'start':
            state.status = ACTIVE
            state.grace_until = float('-inf')
            state.last_meaningful_at = event_at
            if allow_refresh:
                self.last_activity_time = max(self.last_activity_time, event_at)
            return allow_refresh
        if classification == 'work':
            if state.status == ACTIVE:
                state.last_meaningful_at = event_at
                if allow_refresh:
                    self.last_activity_time = max(self.last_activity_time, event_at)
                return allow_refresh
            # The monitor can attach halfway through a turn. Until a start event
            # is known, request once but do not risk reviving an ended turn.
            if state.status == UNKNOWN:
                return self._unknown_refresh(state, now, allow_refresh, '<work-before-start>')
            return False
        if classification == 'end':
            if state.status in (GRACE, ENDED):
                return False
            state.last_meaningful_at = event_at
            if allow_refresh:
                self.last_activity_time = max(self.last_activity_time, event_at)
            if state.status == UNKNOWN:
                state.status = ENDED
                state.grace_until = float('-inf')
                return allow_refresh
            remaining = max(0.0, END_GRACE - max(0.0, now - event_at))
            if remaining:
                state.status = GRACE
                state.grace_until = now + remaining
            else:
                state.status = ENDED
                state.grace_until = float('-inf')
            return allow_refresh
        return self._unknown_refresh(state, now, allow_refresh, event.get('type') or '<none>')

    @staticmethod
    def _decode_lines(lines):
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(event, dict):
                yield event

    def _seed(self, path, stat, now, *, allow_refresh):
        start = max(0, stat.st_size - READ_LIMIT)
        try:
            with path.open('rb') as stream:
                prefix = b'\n'
                if start:
                    stream.seek(start - 1)
                    prefix = stream.read(1)
                stream.seek(start)
                data = stream.read(READ_LIMIT)
        except OSError:
            return None, False
        parts = data.split(b'\n')
        partial = parts.pop()
        if start and prefix != b'\n' and parts:
            parts.pop(0)
        state = TranscriptState(
            offset=start + len(data), partial=partial, inode=self._inode(stat),
            last_mtime=stat.st_mtime,
        )
        if start and prefix != b'\n' and b'\n' not in data:
            state.partial = b''
            state.skipping_line = True
        age = max(0.0, time.time() - stat.st_mtime)
        event_at = now - age
        refresh = False
        for event in self._decode_lines(parts):
            refresh |= self._apply_event(
                state, event, now, event_at=event_at, allow_refresh=allow_refresh)
        # Seeding reconstructs the latest durable lifecycle state; grace is only
        # for a turn_ended event observed live.
        if state.status == GRACE:
            state.status = ENDED
            state.grace_until = float('-inf')
        self._expire_state(state, now)
        return state, refresh

    def _candidate_files(self):
        found = {}
        if not self.root.is_dir():
            if not self._missing_logged:
                LOG.debug('[CursorActivity] transcript unavailable')
                self._missing_logged = True
            return found
        self._missing_logged = False
        try:
            projects = list(self.root.iterdir())
        except OSError:
            LOG.debug('[CursorActivity] transcript unavailable')
            return found
        for project in projects:
            folder = project / 'agent-transcripts'
            try:
                if not folder.is_dir():
                    continue
                for path in folder.rglob('*.jsonl'):
                    try:
                        found[path] = path.stat()
                    except OSError:
                        continue
            except OSError:
                continue
        return found

    def _discover(self, now):
        found = self._candidate_files()
        self._expire(now)
        pinned = {
            path for path, state in self.files.items()
            if state.status in (ACTIVE, GRACE)
        }
        ranked = sorted(found, key=lambda path: found[path].st_mtime, reverse=True)
        selected = set(pinned)
        for path in ranked:
            if path in selected:
                continue
            if len(selected) >= MAX_TRACKED:
                break
            selected.add(path)
        refresh = False
        keep = {}
        for path in selected:
            state = self.files.get(path)
            stat = found.get(path)
            if state is not None and stat is None:
                # A pinned but temporarily unavailable turn remains protected
                # until the watchdog expires.
                if state.status in (ACTIVE, GRACE):
                    keep[path] = state
                continue
            if stat is None:
                continue
            if state is None:
                fresh = 0 <= time.time() - stat.st_mtime < INACTIVITY_TIMEOUT
                state, hit = self._seed(path, stat, now, allow_refresh=self.initialized and fresh)
                refresh |= hit
            if state is not None:
                keep[path] = state
        self.files = keep
        return refresh

    def _read_appends(self, now):
        refresh = False
        remove = []
        for path, state in list(self.files.items()):
            try:
                stat = path.stat()
                replaced = stat.st_size < state.offset or self._inode(stat) != state.inode
                if replaced:
                    seeded, hit = self._seed(path, stat, now, allow_refresh=True)
                    if seeded is not None:
                        self.files[path] = seeded
                    refresh |= hit
                    continue
                if stat.st_size <= state.offset:
                    continue
                with path.open('rb') as stream:
                    stream.seek(state.offset)
                    data = stream.read(READ_LIMIT)
                state.offset += len(data)
                state.last_mtime = stat.st_mtime
                if state.skipping_line:
                    boundary = data.find(b'\n')
                    if boundary < 0:
                        continue
                    data = data[boundary + 1:]
                    state.skipping_line = False
                parts = (state.partial + data).split(b'\n')
                state.partial = parts.pop()
                if len(state.partial) > READ_LIMIT:
                    state.partial = b''
                    state.skipping_line = True
                for event in self._decode_lines(parts):
                    refresh |= self._apply_event(state, event, now)
            except OSError:
                if state.status not in (ACTIVE, GRACE):
                    remove.append(path)
        for path in remove:
            self.files.pop(path, None)
        return refresh

    def _log_transitions(self, now):
        visual = self.visual_active(now)
        fast = self.fast(now)
        if visual != self.was_visual:
            LOG.debug('[CursorActivity] visual=%s', visual)
        if fast != self.was_fast:
            LOG.debug('[Usage] Cursor mode %s -> %s',
                      'FAST' if self.was_fast else 'NORMAL',
                      'FAST' if fast else 'NORMAL')
        self.was_visual = visual
        self.was_fast = fast

    def _folder_times(self):
        """Change times of the folders a new transcript can appear in.

        A folder's time moves only when an entry is added or removed directly
        inside it, so three levels are watched: projects, each project's
        agent-transcripts, and the conversation folders (with their
        subagents) that are running or were written in the last ten minutes.
        Older conversations are left to the slow full rescan.
        """
        try:
            root_time = self.root.stat().st_mtime_ns
        except OSError:
            return None
        if root_time != self._root_mtime:
            self._root_mtime = root_time
            try:
                self._transcript_dirs = [project / 'agent-transcripts' for project in self.root.iterdir()]
            except OSError:
                self._transcript_dirs = []
        folders = [self.root, *self._transcript_dirs]
        recent = time.time() - STALE_TIMEOUT
        for path, state in self.files.items():
            if state.status in (ACTIVE, GRACE) or state.last_mtime >= recent:
                conversation = path.parent.parent if path.parent.name == 'subagents' else path.parent
                folders += [conversation, conversation / 'subagents']
        times = []
        for folder in dict.fromkeys(folders):
            try:
                times.append((folder, folder.stat().st_mtime_ns))
            except OSError:
                times.append((folder, None))
        return tuple(times)

    def poll(self, now):
        if now - self.last_scan < SCAN_INTERVAL:
            self._log_transitions(now)
            return False
        self.last_scan = now
        # Consume tracked appends before discovery can evict a newly-active file.
        refresh = self._read_appends(now)
        folders = self._folder_times()
        if now - self.last_discovery >= DISCOVERY_INTERVAL or folders != self._folders:
            self.last_discovery = now
            refresh |= self._discover(now)
            # Discovery may add conversations to watch, so take the times again.
            folders = self._folder_times()
        self._folders = folders
        self.initialized = True
        self._log_transitions(now)
        return refresh
