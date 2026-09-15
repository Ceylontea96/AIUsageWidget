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


class CodexActivityMonitor:
    def __init__(self, home=None):
        self.root = Path(home or os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'sessions'
        self.files = {}
        self.last_activity_time = float('-inf')
        self.last_scan = float('-inf')
        self.initialized = False
        self.was_fast = False

    def fast(self, now):
        return now - self.last_activity_time < INACTIVITY_TIMEOUT

    def poll(self, now):
        if now - self.last_scan < 1:
            return False, False
        self.last_scan = now
        activity = quota = False
        try:
            today = datetime.now().date()
            paths = set(self.files)
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
                        state = [max(0, stat.st_size - 65536), b'', None, stat.st_ino]
                        self.files[path] = state
                    with path.open('rb') as stream:
                        stream.seek(state[0])
                        data = stream.read(65536)
                    state[0] += len(data)
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
        return activity, quota
