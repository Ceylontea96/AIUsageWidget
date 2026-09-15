"""Read local Cursor agent transcripts only; contents never determine plan quota."""
import json
import time
from pathlib import Path

from codex_activity import INACTIVITY_TIMEOUT, LOG

SCAN_INTERVAL = 0.75
MAX_PROJECTS = 8
MAX_CONVOS = 16
MAX_FILES = 64
READ_LIMIT = 65536


class CursorActivityMonitor:
    def __init__(self, home=None):
        self.root = Path(home) if home else Path.home() / '.cursor' / 'projects'
        self.files = {}
        self.last_activity_time = float('-inf')
        self.last_scan = float('-inf')
        self.initialized = False
        self.was_fast = False
        self._missing_logged = False

    def fast(self, now):
        return now - self.last_activity_time < INACTIVITY_TIMEOUT

    def _rel(self, path):
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.name

    def _discover(self):
        ranked = []
        try:
            if not self.root.is_dir():
                if not self._missing_logged:
                    LOG.debug('[CursorActivity] transcript unavailable')
                    self._missing_logged = True
                return []
            self._missing_logged = False
            for child in self.root.iterdir():
                folder = child / 'agent-transcripts'
                try:
                    if folder.is_dir():
                        ranked.append((folder.stat().st_mtime, folder))
                except OSError:
                    continue
        except OSError:
            LOG.debug('[CursorActivity] transcript unavailable')
            return []
        found = {}
        for _, folder in sorted(ranked, reverse=True)[:MAX_PROJECTS]:
            convos = []
            try:
                for convo in folder.iterdir():
                    try:
                        if convo.is_dir():
                            convos.append((convo.stat().st_mtime, convo))
                    except OSError:
                        continue
            except OSError:
                continue
            for _, convo in sorted(convos, reverse=True)[:MAX_CONVOS]:
                try:
                    for path in convo.rglob('*.jsonl'):
                        try:
                            found[path] = path.stat().st_mtime
                        except OSError:
                            continue
                except OSError:
                    continue
        for path in list(self.files):
            try:
                found.setdefault(path, path.stat().st_mtime)
            except OSError:
                self.files.pop(path, None)
        return [path for _, path in sorted(((mtime, path) for path, mtime in found.items()), reverse=True)[:MAX_FILES]]

    def poll(self, now):
        if now - self.last_scan < SCAN_INTERVAL:
            return False
        self.last_scan = now
        activity = new_turn = ended = False
        added = 0
        try:
            selected = self._discover()
            keep = {}
            for path in selected:
                try:
                    stat = path.stat()
                    state = self.files.get(path)
                    seed = state is None or stat.st_size < state[0] or stat.st_ino != state[2]
                    if seed:
                        fresh = 0 <= (time.time() - stat.st_mtime) <= INACTIVITY_TIMEOUT
                        if self.initialized and (state is None or fresh):
                            LOG.debug('[CursorActivity] transcript discovered: %s', self._rel(path))
                        if not self.initialized or not fresh:
                            state = [stat.st_size, b'', stat.st_ino]
                        else:
                            state = [0, b'', stat.st_ino]
                    with path.open('rb') as stream:
                        stream.seek(state[0])
                        data = stream.read(READ_LIMIT)
                    state[0] += len(data)
                    if self.initialized and data:
                        activity = True
                        added += len(data)
                    lines = (state[1] + data).split(b'\n')
                    state[1] = lines.pop()
                    if len(state[1]) > READ_LIMIT:
                        state[1] = b''
                    if self.initialized:
                        for line in lines:
                            try:
                                event = json.loads(line)
                            except (ValueError, TypeError):
                                continue
                            if not isinstance(event, dict):
                                continue
                            kind = event.get('type')
                            if kind == 'turn_ended':
                                ended = True
                            elif event.get('role') == 'user':
                                new_turn = True
                    keep[path] = state
                except OSError:
                    LOG.debug('[CursorActivity] transcript read unavailable')
            self.files = keep
        except OSError:
            LOG.debug('[CursorActivity] transcript unavailable')
        self.initialized = True
        if activity:
            self.last_activity_time = now
            LOG.debug('[CursorActivity] transcript append +%s bytes', added)
            if new_turn:
                LOG.debug('[CursorActivity] new turn detected')
            if ended:
                LOG.debug('[CursorActivity] turn ended')
        fast = self.fast(now)
        if fast != self.was_fast:
            if fast:
                LOG.debug('[CursorActivity] ACTIVE')
                LOG.debug('[Usage] Cursor NORMAL -> FAST')
            else:
                LOG.debug('[CursorActivity] inactivity=%.1fs', now - self.last_activity_time)
                LOG.debug('[CursorActivity] NORMAL')
                LOG.debug('[Usage] Cursor FAST -> NORMAL')
        self.was_fast = fast
        return activity
