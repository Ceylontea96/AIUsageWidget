"""Read local activity only; token counts never determine plan quota."""
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG = logging.getLogger('ai_usage.activity')
FAST_INTERVAL = 2.0
INACTIVITY_TIMEOUT = 12.0
# The bar follows a turn from its start to its end. Codex writes both, and a
# session runs one turn at a time, so a new start replaces an unfinished one.
END_GRACE = 0.5
SCAN_INTERVAL = 0.25
DISCOVERY_INTERVAL = 1.0
STALE_TIMEOUT = 600.0
BACKSCAN_LIMIT = 1 << 20
_LIFECYCLE = {
    'task_started': 'start', 'turn_started': 'start',
    'task_complete': 'end', 'turn_complete': 'end', 'turn_aborted': 'end',
}
# Unescaped quotes cannot occur inside a JSON string, so these bytes only match
# a real event, never a message that happens to mention one.
_MARKERS = tuple(f'"payload":{{"type":"{kind}"'.encode() for kind in _LIFECYCLE)

# Per-file state: [offset, partial line, token total, inode, turn, grace until, last append]
OFFSET, PARTIAL, TOTAL, INODE, TURN, GRACE_UNTIL, LAST_APPEND = range(7)
ACTIVE, GRACE = 'active', 'grace'


def _last_lifecycle(path, end):
    """The last start/end event before byte `end`, reading back at most 1 MiB."""
    buffer, start = b'', end
    with path.open('rb') as stream:
        while start > 0 and end - start < BACKSCAN_LIMIT:
            previous, start = start, max(0, start - 65536, end - BACKSCAN_LIMIT)
            stream.seek(start)
            # Growing one buffer keeps a marker that straddles two chunks findable.
            buffer = stream.read(previous - start) + buffer
            at, marker = max((buffer.rfind(m), m) for m in _MARKERS)
            if at >= 0:
                return _LIFECYCLE[marker.split(b'"')[-2].decode()]
    return None


class CodexActivityMonitor:
    def __init__(self, home=None):
        self.root = Path(home or os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'sessions'
        self.files = {}
        self.last_activity_time = float('-inf')
        self.last_scan = float('-inf')
        self.last_discovery = float('-inf')
        self.initialized = False
        self.was_fast = False
        self.was_visual = False

    def fast(self, now):
        return now - self.last_activity_time < INACTIVITY_TIMEOUT

    def visual_active(self, now):
        """A turn is running in any session file, subagents included."""
        for state in self.files.values():
            if state[TURN] == ACTIVE and now - state[LAST_APPEND] >= STALE_TIMEOUT:
                state[TURN] = None
                LOG.debug('[Codex] turn without an end expired')
            elif state[TURN] == GRACE and now >= state[GRACE_UNTIL]:
                state[TURN] = None
        return any(state[TURN] in (ACTIVE, GRACE) for state in self.files.values())

    @staticmethod
    def _turn_event(state, lifecycle, now):
        if lifecycle == 'start':
            state[TURN] = ACTIVE
        elif state[TURN] == ACTIVE:
            state[TURN] = GRACE
            state[GRACE_UNTIL] = now + END_GRACE

    def _restore(self, path, state, stat, seen, now):
        """Whether a turn is still running in a file first read now.

        Only a start with appends within the stale window counts; an old
        orphaned start stays idle. Files quiet for longer are not read back.
        """
        quiet = max(0.0, time.time() - stat.st_mtime)
        if quiet >= STALE_TIMEOUT:
            return
        lifecycle = seen
        if lifecycle is None and stat.st_size > 65536:
            try:
                lifecycle = _last_lifecycle(path, max(0, stat.st_size - 65536))
            except OSError:
                lifecycle = None
        if lifecycle == 'start':
            state[TURN] = ACTIVE
            state[LAST_APPEND] = now - quiet
            LOG.debug('[Codex] joined a running turn')

    def poll(self, now):
        if now - self.last_scan < SCAN_INTERVAL:
            return False, False
        self.last_scan = now
        activity = quota = False
        try:
            paths = set(self.files)
            # Tracked files are read every scan; looking for new ones costs two
            # directory listings, so that happens once a second.
            if now - self.last_discovery >= DISCOVERY_INTERVAL:
                self.last_discovery = now
                today = datetime.now().date()
                for day in (today, today - timedelta(days=1)):
                    paths.update((self.root / day.strftime('%Y/%m/%d')).glob('*.jsonl'))
            # Bound discovery and reads; never scan the full session archive.
            ranked = []
            for path in paths:
                try:
                    ranked.append((path.stat().st_mtime, path))
                except OSError:
                    pass
            selected = [p for _, p in sorted(ranked, reverse=True)[:64]]
            self.files = {p: v for p, v in self.files.items() if p in selected}
            for path in selected:
                try:
                    stat = path.stat()
                    state = self.files.get(path)
                    seed = state is None or stat.st_size < state[0] or stat.st_ino != state[3]
                    if seed:
                        state = [max(0, stat.st_size - 65536), b'', None, stat.st_ino, None, float('-inf'), now]
                        self.files[path] = state
                    with path.open('rb') as stream:
                        stream.seek(state[0])
                        data = stream.read(65536)
                    state[0] += len(data)
                    if data and not seed:
                        state[LAST_APPEND] = now
                    seen = None
                    lines = (state[1] + data).split(b'\n')
                    state[1] = lines.pop()
                    if len(state[1]) > 65536:
                        state[1] = b''
                    for line in lines:
                        try:
                            event = json.loads(line)
                            when = datetime.fromisoformat(event.get('timestamp', '').replace('Z', '+00:00'))
                            fresh = 0 <= (datetime.now(timezone.utc) - when).total_seconds() <= INACTIVITY_TIMEOUT
                            payload = event.get('payload') or {}
                            if not isinstance(payload, dict) or event.get('type') != 'event_msg':
                                continue
                            kind = payload.get('type')
                            lifecycle = _LIFECYCLE.get(kind)
                            if lifecycle:
                                # Lines already in a file when it is first read only
                                # tell where it stands; later lines move the turn.
                                if seed:
                                    seen = lifecycle
                                else:
                                    self._turn_event(state, lifecycle, now)
                            info = payload.get('info') or {}
                            total = (info.get('total_token_usage') or {}).get('total_tokens')
                            valid = isinstance(total, (int, float)) and not isinstance(total, bool) and math.isfinite(total) and total >= 0
                            increased = valid and (state[2] is None and total > 0 or state[2] is not None and total > state[2])
                            previous = state[2]
                            if valid:
                                state[2] = total
                            live = self.initialized and fresh
                            if live and ((kind == 'token_count' and increased) or kind == 'task_complete'):
                                activity = True
                                LOG.debug('[Codex] activity detected; token total: %s -> %s', previous, total)
                            if live and (kind == 'token_count' and isinstance(payload.get('rate_limits'), dict) or kind == 'codex.rate_limits'):
                                quota = True
                        except (ValueError, TypeError, AttributeError, OverflowError):
                            continue
                    if seed:
                        self._restore(path, state, stat, seen, now)
                except OSError:
                    LOG.debug('[Codex] session read unavailable')
        except OSError:
            LOG.debug('[Codex] activity source unavailable')
        self.initialized = True
        if activity:
            self.last_activity_time = now
        fast = self.fast(now)
        if fast != self.was_fast:
            if fast:
                LOG.debug('[Codex] active=True')
            else:
                LOG.debug('[Codex] inactivity %.1fs', now - self.last_activity_time)
                LOG.debug('[Codex] active=False')
            LOG.debug('[Usage] GPT mode %s -> %s', 'FAST' if self.was_fast else 'NORMAL', 'FAST' if fast else 'NORMAL')
        self.was_fast = fast
        visual = self.visual_active(now)
        if visual != self.was_visual:
            LOG.debug('[Codex] turn %s', 'RUNNING' if visual else 'IDLE')
            self.was_visual = visual
        return activity, quota
