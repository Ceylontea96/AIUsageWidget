"""One Tk timer for every animated bar, chip and ring in a window.

A client reports the frame interval it needs: FAST (60 fps) while a value or
the bar thickness is moving, SLOW (30 fps) while only the light sweeps, None
when it is idle. Each client keeps a deadline that advances by whole
intervals from the previous deadline, not from when the last frame finished.
A frame that starts late therefore skips the deadlines it missed instead of
pushing every later frame back; animation positions come from
time.monotonic(), so skipping never slows an animation down.

Lateness in the stats is measured from the deadline, so it includes the
Windows timer tick described at EARLY.
"""
from __future__ import annotations

import time
from collections import deque
from tkinter import TclError

FAST = 1 / 60
SLOW = 1 / 30
# Windows wakes Tk timers on its ~15.6 ms system tick, so after(17) waits
# about 31 ms. Timers are armed this much before a deadline, and a client
# within this much of its deadline is drawn now. Positions come from
# time.monotonic(), so a frame drawn a few ms early shows the right moment.
EARLY = 0.008


def frame_class(interval):
    return '60fps' if interval <= FAST + 1e-9 else '30fps'


class FrameStats:
    """Per frame class: frames drawn, deadlines missed, render and lateness."""

    def __init__(self, keep=4000):
        self._keep = keep
        self.classes = {}

    def record(self, interval, lateness, render, missed):
        entry = self.classes.get(frame_class(interval))
        if entry is None:
            entry = {'frames': 0, 'missed': 0, 'render': deque(maxlen=self._keep),
                     'lateness': deque(maxlen=self._keep)}
            self.classes[frame_class(interval)] = entry
        entry['frames'] += 1
        entry['missed'] += missed
        entry['render'].append(render)
        entry['lateness'].append(max(0.0, lateness))

    def summary(self):
        def p95(values):
            ordered = sorted(values)
            return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))] if ordered else 0.0

        return {
            name: {
                'frames': entry['frames'],
                'deadline_missed': entry['missed'],
                'render_ms_mean': round(1000 * sum(entry['render']) / max(1, len(entry['render'])), 2),
                'render_ms_p95': round(1000 * p95(entry['render']), 2),
                'lateness_ms_p95': round(1000 * p95(entry['lateness']), 2),
            }
            for name, entry in sorted(self.classes.items())
        }


class FrameClock:
    """Clients implement `_frame_interval()` and `_frame(now)`."""

    def __init__(self, widget):
        self._widget = widget
        self._due = {}
        self._after = None
        self._armed_for = None
        self.stats = FrameStats()

    def scheduled(self, client):
        return client in self._due

    def wake(self, client):
        """Start frames for a client, or bring its next frame forward."""
        interval = client._frame_interval()
        if interval is None:
            return
        due = time.monotonic() + interval
        if client in self._due and self._due[client] <= due:
            return
        self._due[client] = due
        self._arm()

    def release(self, client):
        if self._due.pop(client, None) is not None and not self._due:
            self._cancel()

    def _cancel(self):
        after, self._after, self._armed_for = self._after, None, None
        if after is not None:
            try:
                self._widget.after_cancel(after)
            except TclError:
                pass

    def _arm(self):
        if not self._due:
            self._cancel()
            return
        due = min(self._due.values())
        if self._after is not None and self._armed_for is not None and self._armed_for <= due:
            return
        self._cancel()
        delay = max(1, int((due - time.monotonic() - EARLY) * 1000))
        try:
            self._after = self._widget.after(delay, self._tick)
            self._armed_for = due
        except TclError:
            self._after = self._armed_for = None

    def _tick(self):
        self._after = self._armed_for = None
        try:
            self._run_due(time.monotonic())
        finally:
            # Even if one client raised, the others keep their frames.
            self._arm()

    def _run_due(self, now):
        for client, due in sorted(self._due.items(), key=lambda item: item[1]):
            if due > now + EARLY or client not in self._due:
                continue
            interval = client._frame_interval()
            if interval is None:
                del self._due[client]
                continue
            started = time.perf_counter()
            try:
                client._frame(now)
            except TclError:
                # The widget is being destroyed.
                self._due.pop(client, None)
                continue
            except Exception:
                self._due.pop(client, None)
                raise
            render = time.perf_counter() - started
            following = client._frame_interval()
            if following is None:
                self._due.pop(client, None)
                self.stats.record(interval, now - due, render, 0)
                continue
            next_due = due + following
            finished = time.monotonic()
            missed = 0
            if next_due <= finished:
                missed = int((finished - next_due) // following) + 1
                next_due += missed * following
            self._due[client] = next_due
            self.stats.record(interval, now - due, render, missed)


def clock_for(widget):
    """The one clock of the widget's Tk root."""
    root = widget._root()
    clock = getattr(root, '_usage_frame_clock', None)
    if clock is None:
        clock = FrameClock(root)
        root._usage_frame_clock = clock
    return clock
