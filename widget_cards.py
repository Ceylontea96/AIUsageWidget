"""The provider cards and compact chips, and the light, ring and chevron animation they draw."""
from __future__ import annotations

import json
import math
import time
import tkinter as tk
import webbrowser
import zlib
from collections import OrderedDict
from functools import lru_cache
from tkinter import font as tkfont

from additional_ui import AdditionalBlock, additional_count, layout_additional
from bar_raster import progress_rgba, ringed_progress_rgba
import widget_raster as raster
from frame_clock import FAST as FRAME_FAST, clock_for
from providers import fmt_local
from quota_policy import (
    FIVE_HOURS,
    FIVE_HOUR_TOLERANCE,
    group_stale,
    remaining_band,
    representative_blocked,
    representative_percent,
    select_hero,
    window_matches,
)
from widget_theme import *  # noqa: F401,F403  colours, fonts and sizes
from widget_text import *  # noqa: F401,F403  what a snapshot reads as


def chip_fill_width(total, percent):
    if percent is None:
        return 0.0
    try:
        value = float(percent)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(float(total), float(total) * max(0.0, min(100.0, value)) / 100.0))


BAR_ANIM_SPEED = 8.0
BAR_ANIM_SNAP = 0.01  # Percentage points.
# The ring's growth is drawn in this many cached steps.
RING_EMPHASIS_STEPS = 8
# Moments the ring marks once, then rests: quota just used, a limit coming
# back, turning red. Each is drawn in RING_FX_STEPS cached images.
RING_FX_STEPS = 10
GHOST_HOLD_S = 0.6       # the used part stays this long, then shrinks away
REFILL_JUMP = 15.0       # a rise of this many points or more is a reset
REFILL_S = 0.9           # the ring flashes and a circle spreads out from it
REFILL_PACE = 0.45       # a reset refills the ring at this pace of a bar's
RIPPLE_DELAY_S = 0.2
RING_MARGIN = 8          # transparent pixels around the ring while the circle spreads
PULSE_BEATS = 3
PULSE_BEAT_S = 0.4
RING_BANDS = ('ok', 'warn', 'danger', 'critical')
# Folding or unfolding a card turns its chevron a quarter over CHEVRON_TURN_S,
# drawn in CHEVRON_TURN_STEPS cached images.
CHEVRON_TURN_S = 0.2
CHEVRON_TURN_STEPS = 12
# Opening the widget: a card's ring, bars and number fill from zero over
# ENTRANCE_S, in steps of 1/ENTRANCE_FPS so the ring images are known before.
ENTRANCE_S = 0.6
ENTRANCE_FPS = 60
SHIMMER_GROW_S = 0.4
SHIMMER_SHRINK_S = 0.85
SHIMMER_SWEEP_S = 1.2
SHIMMER_GLOW = 0.65
# Light positions per sweep. At its fastest the light crosses a card bar at
# about 360 px/s, so neighbouring positions are under a pixel apart, and each
# sweep draws the same frames as the one before it.
SHIMMER_PHASES = 288
# Recent shimmer frames kept as PNG bytes, about 0.65 KB each; six bars
# sweeping at once use 1,734 of them.
SHIMMER_FRAMES = 2048


def ease_out(progress):
    """Ease-out cubic: quick to answer the click, gentle as it lands."""
    progress = max(0.0, min(1.0, progress))
    return 1 - (1 - progress) ** 3


def bar_display_percent(value):
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def follow_bar(current, target, dt):
    """Frame-rate-independent exponential following, in percentage points."""
    alpha = -math.expm1(-BAR_ANIM_SPEED * max(0.0, dt))
    value = current + (target - current) * alpha
    return target if abs(target - value) <= BAR_ANIM_SNAP else value


def follow_emphasis(current, target, dt, grow=SHIMMER_GROW_S, shrink=SHIMMER_SHRINK_S):
    """Move 0–1 thickness toward the activity target without restarting."""
    goal = 1.0 if target else 0.0
    if current < goal:
        value = min(goal, current + max(0.0, dt) / grow)
    elif current > goal:
        value = max(goal, current - max(0.0, dt) / shrink)
    else:
        return current
    return goal if abs(value - goal) <= 1e-9 else value


# Provider key -> (icon asset stem, image height at scale 1). The heights make
# the marks look the same size: the OpenAI Blossom file keeps its clear space
# around a mark about half as tall, the Cursor cube has none. The renderer
# stays generic: a provider without an entry simply draws no icon.
SERVICE_ICONS = {'chatgpt': ('service_gpt', 33), 'cursor': ('service_cursor', 17)}


@lru_cache(maxsize=16)
def service_icon_png(name, height):
    """The master shrunk to `height`, keeping its proportions. Cached per size."""
    width, source_h, rows = raster.read_png_rgba((ICON_DIR / f'{name}.png').read_bytes())
    height = max(1, min(source_h, int(height)))
    target_w = max(1, int(round(width * height / source_h)))
    return raster.png_rgba(*raster.shrink_rgba(width, source_h, rows, target_w, height), level=6)


def load_service_icon(key, scale):
    entry = SERVICE_ICONS.get(key)
    if not entry:
        return None
    name, height = entry
    try:
        return tk.PhotoImage(data=service_icon_png(name, px(height, scale, 1)), format='png')
    except (OSError, ValueError, zlib.error, tk.TclError):
        return None


def round_rect(canvas, x1, y1, x2, y2, radius, fill, tags=()):
    if x2 <= x1 or y2 <= y1:
        return
    radius = max(0, min(radius, (x2-x1)/2, (y2-y1)/2))
    options = dict(fill=fill, outline='', tags=tags)
    canvas.create_oval(x1,y1,x1+radius*2,y1+radius*2,**options)
    canvas.create_oval(x2-radius*2,y1,x2,y1+radius*2,**options)
    canvas.create_oval(x1,y2-radius*2,x1+radius*2,y2,**options)
    canvas.create_oval(x2-radius*2,y2-radius*2,x2,y2,**options)
    canvas.create_rectangle(x1+radius,y1,x2-radius,y2,**options)
    canvas.create_rectangle(x1,y1+radius,x2,y2-radius,**options)


class FrameCache:
    """Recent shimmer frames as PNG bytes, shared by every bar and chip.

    While the light sweeps at a steady thickness the same SHIMMER_PHASES
    frames come round every sweep, so after the first few sweeps a frame is
    only decoded, not drawn and compressed again. Bytes, not Tk images: 288
    images hold about 4 MB for each bar, and each bar makes its own image from
    the bytes, so two bars that look alike never share one.
    """

    def __init__(self, limit=SHIMMER_FRAMES):
        self.limit = limit
        self._frames = OrderedDict()

    def __len__(self):
        return len(self._frames)

    def get(self, key, draw):
        data = self._frames.get(key)
        if data is None:
            data = self._frames[key] = draw()
            if len(self._frames) > self.limit:
                self._frames.popitem(last=False)
        else:
            self._frames.move_to_end(key)
        return data


SHIMMER_CACHE = FrameCache()


def progress_png(width, height, radius, fill_width, track, fill, background, shimmer=None, shape_height=None):
    w, h, rows = progress_rgba(width, height, radius, fill_width, track, fill, background,
                               shimmer=shimmer, shape_height=shape_height, glow=SHIMMER_GLOW)
    # Fast compression: a frame is decoded at once, or kept small in the cache.
    return raster.png_rgba(w, h, rows, level=1)


def progress_photo(width, height, radius, fill_width, track, fill, background, samples=1, shimmer=None,
                   shape_height=None, cache=None):
    if samples != 1:
        return tk.PhotoImage(data=raster.progress_bar_png(width, height, radius, fill_width, track, fill, background,
                                                   samples=samples, shimmer=shimmer, shape_height=shape_height, glow=SHIMMER_GLOW), format='png')
    args = (width, height, radius, fill_width, track, fill, background, shimmer, shape_height)
    data = progress_png(*args) if cache is None else cache.get(('bar',) + args, lambda: progress_png(*args))
    return tk.PhotoImage(data=data, format='png')


class BarShimmer:
    """Thickness and sweep follow activity state, not a fixed hold timer.

    Frames come from the window's FrameClock at 60 fps while the thickness, a
    length or the light is moving. Every position is computed from
    time.monotonic(), so a skipped frame costs smoothness only. At a steady
    thickness and length the frames repeat every sweep and come from
    SHIMMER_CACHE instead of being drawn again.
    """

    def _init_shimmer(self):
        self._desired_active = False
        self._active = False
        self._emphasis = 0.0
        self._emphasis_t0 = None
        self._sweep_t0 = None
        self._paused_at = None
        self.bind('<Unmap>', self._pause_shimmer, add='+')
        self.bind('<Map>', self._resume_shimmer, add='+')
        self.bind('<Destroy>', self._destroy_shimmer, add='+')

    def set_activity(self, active):
        if not self.animate:
            return
        active = bool(active)
        if active == self._desired_active:
            # Idle widgets need no Tk checks; an existing frame handles motion.
            # If motion lost its scheduled frame, fall through and re-arm it.
            if not self._fx_needed() and not self._tweening():
                self._emphasis_t0 = None
                return
            if self._frame_scheduled:
                return
        self._desired_active = active
        self._active = self._desired_active
        try:
            if not self.winfo_ismapped():
                return
        except tk.TclError:
            return
        if self._active and self._sweep_t0 is None:
            self._sweep_t0 = time.monotonic()
        self._start_shimmer()

    def _fx_needed(self):
        return self._active or self._emphasis > 1e-4 or self._sweep_t0 is not None

    # -- frames -------------------------------------------------------------
    @property
    def _frame_scheduled(self):
        return clock_for(self).scheduled(self)

    def _tweening(self):
        return False

    def _advance_tween(self, now):
        pass

    def _moving(self):
        """A one-off effect is playing, such as a card ring's moments."""
        return False

    def _advance_effects(self, now):
        pass

    def _shimmer_live(self):
        try:
            return bool(self.winfo_ismapped()) and self.animate and self._shimmer_ready()
        except tk.TclError:
            return False

    def _frame_interval(self):
        if self._tweening() or self._moving():
            return FRAME_FAST
        if not self._shimmer_live():
            return None
        growing = self._emphasis != (1.0 if self._active else 0.0)
        if growing or self._active or self._sweep_t0 is not None:
            # The light too: at 30 fps it jumped up to 12 px a frame, at 60 6 px.
            return FRAME_FAST
        return None

    def _frame_cache(self):
        """SHIMMER_CACHE while frames come round again, else None.

        While the thickness or a length moves every frame is new; caching those
        would only push out the frames that repeat.
        """
        steady = self._emphasis == (1.0 if self._active else 0.0) and not self._tweening()
        return SHIMMER_CACHE if steady else None

    def _frame(self, now):
        """One frame: move the length, then the light, then draw once."""
        tweening = self._tweening()
        if tweening:
            self._advance_tween(now)
        self._advance_effects(now)
        live = self._shimmer_live()
        if live:
            self._advance_shimmer(now)
        elif self._fx_needed():
            self._hold_shimmer()
        if tweening or live:
            self._paint_shimmer()

    def _sync_frames(self):
        """Ask the clock for frames while anything moves, else leave it."""
        try:
            clock = clock_for(self)
        except tk.TclError:
            return
        if self._frame_interval() is None:
            # Frames stop here. Forget the last frame's time, or the first frame
            # of the next activity would count the whole quiet spell and jump
            # straight to full thickness instead of growing.
            self._emphasis_t0 = None
            clock.release(self)
        else:
            clock.wake(self)

    def _start_shimmer(self):
        self._sync_frames()

    def _hold_shimmer(self):
        self._emphasis_t0 = None
        if self._paused_at is None:
            self._paused_at = time.monotonic()

    def _pause_shimmer(self, event=None):
        if event is not None and event.widget is not self:
            return
        self._hold_shimmer()
        self._sync_frames()

    def _resume_shimmer(self, event=None):
        if event is not None and event.widget is not self:
            return
        self._active = self._desired_active
        if self._paused_at is not None:
            if self._sweep_t0 is not None:
                self._sweep_t0 += time.monotonic() - self._paused_at
            self._paused_at = None
        if self._active and self._sweep_t0 is None:
            self._sweep_t0 = time.monotonic()
        self._start_shimmer()

    def _destroy_shimmer(self, event=None):
        if event is not None and event.widget is not self:
            return
        self._desired_active = False
        self._active = False
        self._emphasis = 0.0
        self._emphasis_t0 = None
        self._sweep_t0 = None
        try:
            clock_for(self).release(self)
        except tk.TclError:
            pass

    def _shimmer_phase_for(self, index):
        if self._sweep_t0 is None or not self.animate or not self._shimmer_ready():
            return None
        progress = max(0.0, min(1.0, (time.monotonic() - self._sweep_t0) / SHIMMER_SWEEP_S))
        # On fixed positions, so every sweep draws the frames of the last one.
        return 0.15 + 0.60 * round(progress * SHIMMER_PHASES) / SHIMMER_PHASES

    def _advance_shimmer(self, now):
        dt = 0.0 if self._emphasis_t0 is None else now - self._emphasis_t0
        self._emphasis_t0 = now
        self._emphasis = follow_emphasis(self._emphasis, self._active, dt)
        if self._active:
            if self._sweep_t0 is None:
                self._sweep_t0 = now
            elapsed = now - self._sweep_t0
            if elapsed >= SHIMMER_SWEEP_S:
                self._sweep_t0 += SHIMMER_SWEEP_S * max(1, int(elapsed // SHIMMER_SWEEP_S))
        elif self._sweep_t0 is not None and now - self._sweep_t0 >= SHIMMER_SWEEP_S:
            self._sweep_t0 = None

class Chip(BarShimmer, tk.Canvas):
    """One progress pill: proportional fill and an independent text overlay."""
    animate = True

    def __init__(self, parent, metrics=None):
        self.metrics = metrics or Metrics()
        self._width = None
        super().__init__(parent,width=self.metrics.chip_w,height=self.metrics.chip_canvas_h,highlightthickness=0,bd=0,bg=BG)
        self.text, self.fill, self.fg, self.percent = '—', CHIP_STALE, CHIP_FG, 0.0
        self._photo = None
        self._seeded = False
        self._usage_snapshot = None
        self._warning_color = None
        self.tip_text = '사용량 확인 중'
        self._anim_to = self._anim_t0 = None
        self.bind('<Destroy>', self._cancel_anim)
        self._init_shimmer()
        self._redraw()

    @property
    def chip_width(self):
        """Laid-out width, which the compact row sizes to fit its text."""
        return self._width or self.metrics.chip_w

    def set_width(self, width):
        width = max(1, int(width or 0)) if width else None
        if width == self._width:
            return
        self._width = width
        self._redraw()

    def set_metrics(self, metrics):
        self.metrics = metrics
        self.configure(width=self.chip_width, height=metrics.chip_canvas_h)
        self._redraw()

    def cget(self,key):
        if key == 'text':
            return self.text
        return super().cget(key)

    def configure(self,text=None,fg=None,bg=None,percent=None,animate=None,**kwargs):
        changed = False
        if text is not None and text != self.text:
            self.text = text
            changed = True
        if bg is not None and bg != self.fill:
            self.fill = bg
            changed = True
        if percent is not None:
            value = chip_fill_width(100, percent)
            do_anim = self.animate if animate is None else animate
            if do_anim and self._seeded and value != self.percent:
                self._anim_to = value
                if self._anim_t0 is None:
                    self._anim_t0 = time.monotonic()
                self._arm_anim()
            else:
                self._cancel_anim()
                if value != self.percent:
                    self.percent = value
                    changed = True
                self._seeded = True
        self.fg = CHIP_FG
        if kwargs:
            super().configure(**kwargs)
        if changed:
            self._redraw()

    def _arm_anim(self):
        self._sync_frames()

    def _cancel_anim(self, event=None):
        self._anim_to = self._anim_t0 = None
        self._sync_frames()

    def _tweening(self):
        return self._anim_to is not None and self._anim_t0 is not None

    def _advance_tween(self, now):
        self.percent = follow_bar(self.percent, self._anim_to, now - self._anim_t0)
        self._anim_t0 = now
        if self.percent == self._anim_to:
            self._anim_to = self._anim_t0 = None

    def observe_usage(self, snap):
        self._usage_snapshot = snap
        self.tip_text = compact_tooltip(snap)
        color = compact_ring_color(snap)
        if color != self._warning_color:
            self._warning_color = color
            self._redraw()

    def _shimmer_ready(self):
        return self.fill != CHIP_STALE and self.percent > 0

    def _bar_photo(self):
        """The pill, ringed in the warning colour when a secondary quota is low.

        The ring takes no width: the compact row has no room for a marker
        beside the label, and one drawn over the corner pulled the label off
        centre. Ring and bar are one image so the ring's round ends stay clean.
        """
        width, height = self.chip_width, self.metrics.chip_canvas_h
        shape_height = self.metrics.chip_h + (height-self.metrics.chip_h) * self._emphasis
        shimmer = self._shimmer_phase_for(0)
        cache = self._frame_cache()
        if not self._warning_color:
            self.fill_width = chip_fill_width(width, self.percent)
            # Fill is clipped to the track so the leading cap cannot bulge outside.
            return progress_photo(width, height, shape_height / 2, self.fill_width, CHIP_TRACK, self.fill, BG,
                                  shimmer=shimmer, shape_height=shape_height, cache=cache)
        ring_w = self.metrics.p(2, 1)
        self.fill_width = chip_fill_width(max(1, width - 2 * ring_w), self.percent)
        args = (width, height, shape_height, ring_w, self._warning_color, self.fill_width, CHIP_TRACK, self.fill, BG)

        def draw():
            w, h, rows = ringed_progress_rgba(*args, shimmer=shimmer, glow=SHIMMER_GLOW)
            return raster.png_rgba(w, h, rows, level=1)

        data = draw() if cache is None else cache.get(('ringed',) + args + (shimmer,), draw)
        return tk.PhotoImage(data=data, format='png')

    def _paint_shimmer(self):
        self._photo = self._bar_photo()
        self.itemconfigure('track', image=self._photo)

    def _redraw(self):
        self.delete('all')
        width, height = self.chip_width, self.metrics.chip_canvas_h
        self._photo = self._bar_photo()
        tags = ('track', 'quota_warning') if self._warning_color else 'track'
        self.create_image(0, 0, image=self._photo, anchor='nw', tags=tags)
        self.create_text(width/2,height/2,text=self.text,fill=CHIP_FG,font=self.metrics.font(FONT_CHIP),tags='label')


def design_severity(value, stale=False, blocked=False):
    if stale or value is None:
        return 'stale', '이전 데이터' if stale else '확인 중', MUTED
    band = remaining_band(value)
    if blocked or band == 'critical':
        return 'critical', '한도 제한' if blocked else '곧 한도', '#DC2626'
    if band == 'danger':
        return 'danger', '임박', DANGER
    if band == 'warn':
        return 'warn', '주의', WARN
    return 'ok', '여유', None


def reset_countdown(reset, now=None, monthly=False):
    if reset is None:
        return '정보 없음'
    seconds = max(0, int(reset - (time.time() if now is None else now)))
    if seconds == 0:
        return '곧'
    days, hours = seconds // 86400, seconds // 3600
    if monthly and days:
        return f'{days}일 후'
    if hours:
        return f'{hours}시간 {seconds % 3600 // 60}분'
    return f'{seconds // 60}분 {seconds % 60:02d}초'


def ring_png_for(size, percent, color, thickness, background=CARD, ghost=None, ghost_color=None,
                 ripple=None, ripple_color=None, margin=0):
    """The ring's PNG, always asked for the same way so cached images are found."""
    return raster.ring_png(max(1, int(size)), percent, color, thickness, background, TRACK,
                           ghost=ghost, ghost_color=ghost_color, ripple=ripple,
                           ripple_color=ripple_color, margin=margin)


def ring_photo(size, percent, color, thickness, background=CARD, ghost=None, ghost_color=None,
               ripple=None, ripple_color=None, margin=0):
    return tk.PhotoImage(data=ring_png_for(size, percent, color, thickness, background, ghost, ghost_color,
                                           ripple, ripple_color, margin), format='png')


class Card(BarShimmer, tk.Frame):
    """Explicit pixel layout matching the supplied 334px-wide card references."""
    animate = True

    def __init__(self,parent,key,metrics=None,on_additional=None,on_toggle=None,on_retry=None,on_login=None):
        self.metrics = metrics or Metrics()
        self.collapsed = False
        self.on_toggle = on_toggle
        self.on_retry = on_retry
        self.on_login = on_login
        # The chevron's quarter turn (see CHEVRON_TURN_S): when it began, inf
        # until its first frame, and the turn it began from.
        self._turn_t0 = None
        self._turn_from = 1.0
        self._chevron_turn = None
        # The opening fill (see enter()): when it starts, and the bar and
        # number values it fills to.
        self._enter_t0 = None
        self._enter_to = []
        self._enter_hero = 0.0
        self._enter_step = None   # the step painted last, so a waiting card is not redrawn
        self._actions = []
        m = self.metrics
        super().__init__(parent,width=m.card_w,height=m.p(120),bg=BG)
        self.key, self.height, self.last_signature = key,m.p(120),None
        self.hero_height = m.p(120)
        self._service_icon = load_service_icon(key, m.scale)
        self._bar_photos = []
        self._bar_origins = []
        self._ring_photo = None
        # (image, number, colour) the ring and hero items show now
        self._ring_shown = None
        # Ring moments, see RING_FX_STEPS.
        self._ghost = None        # percent the trail of quota just used reaches
        self._ghost_hold = 0.0    # when that trail starts to shrink
        self._ghost_t0 = None
        self._refill_t0 = None
        self._pulse_t0 = None
        self._hero_shown = self._hero_target = 0.0
        self._clock_second = None
        self._shown_pcts = []
        self._anim_to = []
        self._anim_t0 = None
        self._snap = None
        self._additional_expanded = False
        self._additional_max_body = 0
        self.rows = tk.Canvas(self,width=m.card_w,height=self.height,bg=BG,bd=0,highlightthickness=0)
        self.rows.pack()
        self.rows.bind('<Button-1>', self._clicked)
        self.rows.bind('<Motion>', self._hovered)
        self.rows.bind('<Leave>', lambda e: self._hover_link(False))
        # (x1, y1, x2, y2) of the ↗ beside the title that opens the usage page.
        self._page_link = None
        self._link_lit = False
        self.rows.configure(takefocus=True)
        self.rows.bind('<Return>', self._toggle_card)
        self.rows.bind('<space>', self._toggle_card)
        self.additional = AdditionalBlock(self, m, on_toggle=on_additional, bg=CARD)
        self.bind('<Destroy>', self._cancel_anim)
        self._init_shimmer()

    def set_metrics(self, metrics):
        if self.metrics.scale != metrics.scale:
            self.last_signature = None
            self._service_icon = load_service_icon(self.key, metrics.scale)
        self.metrics = metrics
        self.additional.set_metrics(metrics)

    def _action_at(self, x, y):
        for x1, y1, x2, y2, kind in self._actions:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return kind
        return ''

    def _on_page_link(self, x, y):
        box = self._page_link
        return box is not None and x is not None and box[0] <= x <= box[2] and box[1] <= y <= box[3]

    def _hovered(self, event):
        """A hand only over what a click acts on: the title row, the ↗ and the buttons."""
        link = self._on_page_link(event.x, event.y)
        self._hover_link(link)
        acts = link or event.y < self.metrics.p(54) or bool(self._action_at(event.x, event.y))
        cursor = 'hand2' if acts else ''
        if self.rows.cget('cursor') != cursor:
            self.rows.configure(cursor=cursor)

    def _hover_link(self, on):
        if on != self._link_lit:
            self._link_lit = on
            self.rows.itemconfigure('page_link', fill=TEXT if on else MUTED)

    def _clicked(self, event):
        x = getattr(event, 'x', None)
        if self._on_page_link(x, event.y):
            webbrowser.open(URLS.get(self.key, ''))
            return
        action = '' if x is None else self._action_at(x, event.y)
        if action == 'retry' and self.on_retry:
            self.on_retry()
            return
        if action == 'login' and self.on_login:
            self.on_login()
            return
        if event.y < self.metrics.p(54) and self.on_toggle:
            self._toggle_card()
        # The rest of the card is for reading. A click there used to open the
        # usage page, which a stray click on the ring or a bar did unasked.

    def _toggle_card(self, event=None):
        if self.on_toggle:
            self.on_toggle()
        return 'break'

    def set_collapsed(self, collapsed, animate=False):
        """Fold or unfold the card. The rows change at once; animated, the
        chevron turns over CHEVRON_TURN_S, back from where it is if clicked
        again mid-turn."""
        if self.collapsed == bool(collapsed):
            return
        turn = self._turn_at(time.monotonic())
        self.collapsed = bool(collapsed)
        self.last_signature = None
        if animate and self.animate and self._snap is not None:
            # The clock starts on the first frame: painting the new rows takes
            # tens of ms, which would otherwise eat the start of the turn.
            self._turn_t0, self._turn_from = math.inf, turn
        else:
            self._turn_t0 = None
        if self._snap is not None:
            self.render(self._snap)
        self._sync_frames()

    def _turn_at(self, now):
        """The chevron's turn: 1 points down (open), 0 right (folded)."""
        target = 0.0 if self.collapsed else 1.0
        if self._turn_t0 is None:
            return target
        turn = self._turn_from + (target - self._turn_from) * ease_out((now - self._turn_t0) / CHEVRON_TURN_S)
        return round(turn * CHEVRON_TURN_STEPS) / CHEVRON_TURN_STEPS

    def _advance_turn(self, now):
        if self._turn_t0 == math.inf:
            self._turn_t0 = now
        elif now - self._turn_t0 >= CHEVRON_TURN_S:
            self._turn_t0 = None
        self._paint_chevron(now)

    def _chevron_png(self, turn):
        m = self.metrics
        return raster.chevron_png(m.p(11, 8), turn, ICON, max(1.5, 1.6*m.scale))

    def _paint_chevron(self, now=None):
        if not self.on_toggle or not self.rows.find_withtag('collapse_toggle'):
            return
        turn = self._turn_at(time.monotonic() if now is None else now)
        if turn == self._chevron_turn:
            return
        self._chevron = tk.PhotoImage(data=self._chevron_png(turn), format='png')
        self.rows.itemconfigure('collapse_toggle', image=self._chevron)
        self._chevron_turn = turn

    def warm_turns(self, step=0):
        """Draw the chevron's in-between turns one at a time while idle.

        Each takes about 3.5 ms; drawn during a first turn they cost it frames.
        """
        if step <= CHEVRON_TURN_STEPS and self.winfo_exists():
            self._chevron_png(step / CHEVRON_TURN_STEPS)
            self.after(40, self.warm_turns, step + 1)

    def set_additional_layout(self, expanded, max_body):
        if (self._additional_expanded, self._additional_max_body) == (bool(expanded), max(0, int(max_body or 0))):
            return
        self._additional_expanded = bool(expanded)
        self._additional_max_body = max(0, int(max_body or 0))
        self._sync_additional()

    def _sync_additional(self):
        groups = getattr(self._snap, 'additional_groups', []) if self._snap and self._snap.ok and not self.collapsed else []
        stale_flags = [group_stale(group, self._snap) for group in groups]
        colors = {
            'bg': CARD, 'muted': MUTED, 'text': TEXT, 'warn': WARN, 'danger': DANGER,
            'track': TRACK, 'dim': DIM, 'font_meta': FONT_META, 'font_row': FONT_ROW,
            'font_value': FONT_VALUE,
        }
        self.additional.render(
            groups,
            expanded=self._additional_expanded,
            max_body=self._additional_max_body,
            colors=colors,
            stale_flags=stale_flags,
        )
        if additional_count(groups):
            if not self.additional.winfo_ismapped():
                self.additional.pack(fill='x')
        else:
            self.additional.pack_forget()
        extra = self.additional.height if additional_count(groups) else 0
        self.height = getattr(self, 'hero_height', self.height) + extra
        self.configure(width=self.metrics.card_w, height=self.height)

    def render(self,snap):
        """Show snap; True when the card was repainted, so its size may have changed."""
        # Mid-entrance the ring shows a fraction of its value, which is no reading
        # to mark a change from: new data there is not a reset.
        entering = self._enter_t0 is not None
        before = (self._ring_reading(self._snap, self._hero_value()[0])
                  if self._snap is not None and not entering else None)
        visual = {field: getattr(snap, field) for field in (
            'key', 'title', 'plan', 'ok', 'hero_caption',
            'footer', 'error', 'dashboard_url', 'stale', 'blocked',
        )}
        limits = main_limits(snap)
        visual['limits'] = [
            [item.quota_id, item.display_name, item.remaining_percent, item.reset_at]
            for item in limits
        ]
        visual['hero'] = hero_index(snap)
        visual['hero_percent'] = representative_percent(snap)
        # Compare exactly what the card draws, so a credit balance or a
        # reset-credit expiry changing on its own still repaints.
        visual['extras'] = [quota_extras(snap), included_amount(snap), bonus_line(snap)]
        # Derived from canonical quota only. Nothing in snap.internal reaches
        # this signature, so reference figures cannot repaint or recolour the
        # card.
        visual['service_remaining'] = status_percent(snap)
        # fetched_at is intentionally absent: the clock rewrites "N초 전" in place.
        # A new stamp alone must not rebuild the card image.
        stale_flags = [group_stale(group, snap) for group in (snap.additional_groups or [])]
        visual['additional'] = [
            vars(row) for row in layout_additional(snap.additional_groups, stale_flags)
        ]
        signature = json.dumps(visual,sort_keys=True)
        self._snap = snap
        if signature == self.last_signature:
            return False
        self.last_signature = signature
        # New data ends the entrance; the usual tween goes on from where it got to.
        self._enter_t0 = None
        targets = [bar_display_percent(item.remaining_percent) for item in limits]
        self._hero_target = bar_display_percent(representative_percent(snap))
        if self.animate and snap.ok and self._shown_pcts and len(self._shown_pcts) == len(targets):
            self._anim_to = targets
            if self._anim_t0 is None:
                self._anim_t0 = time.monotonic()
            index = hero_index(snap)
            target = targets[index] if index is not None and index < len(targets) else self._hero_target
            self._ring_moment(before, self._ring_reading(snap, target))
            self._arm_anim()
        else:
            self._cancel_anim()
            self._shown_pcts = targets
            self._hero_shown = self._hero_target
        self._paint(snap, self._shown_pcts)
        return True

    def _arm_anim(self):
        self._sync_frames()

    def _cancel_anim(self, event=None):
        self._anim_to = []
        self._anim_t0 = None
        self._sync_frames()

    def _tweening(self):
        return (bool(self._anim_to) and self._anim_t0 is not None) or self._enter_t0 is not None

    def enter(self, delay=0.0):
        """Fill the ring, bars and number from zero to what the card shows, as the widget opens.

        Positions move in steps of 1/ENTRANCE_FPS over ENTRANCE_S, so the ring
        images are the ones warm_entrance() drew beforehand: drawn during the
        first frames instead, three cards at once overran the frame budget.
        """
        snap = self._snap
        if not self.animate or snap is None or not snap.ok or not self.rows.find_withtag('ring'):
            return False
        self._cancel_anim()
        self._enter_to, self._enter_hero = list(self._shown_pcts), self._hero_shown
        self._enter_t0 = time.monotonic() + delay
        self._shown_pcts = [0.0] * len(self._enter_to)
        self._hero_shown = 0.0
        self._enter_step = None
        self._paint_shimmer()
        self._enter_step = 0
        self._sync_frames()
        return True

    @property
    def entering(self):
        return self._enter_t0 is not None

    def _entrance_steps(self):
        return round(ENTRANCE_S * ENTRANCE_FPS)

    def warm_entrance(self):
        """Draw the ring images enter() will show; its frames then only look them up."""
        snap, m = self._snap, self.metrics
        if snap is None or not snap.ok:
            return
        hero, _, actual = self._hero_value()
        if actual is None:
            return
        # As _paint_ring draws it with no emphasis, beat or flash during the entrance.
        _, _, color = design_severity(actual, snap.stale or not snap.ok, representative_blocked(snap))
        color = raster.blend(color or ACCENTS[self.key], '#FFFFFF', 0.0)
        thickness = round(m.p(7) * 2) / 2
        steps = self._entrance_steps()
        for step in range(steps + 1):
            ring_png_for(m.p(84), round(hero * ease_out(step / steps) * 2) / 2, color, thickness)

    def _advance_entrance(self, now):
        steps = self._entrance_steps()
        step = min(steps, max(0, round((now - self._enter_t0) * ENTRANCE_FPS)))
        if step == self._enter_step:
            return
        self._enter_step = step
        eased = ease_out(step / steps)
        self._shown_pcts = [target * eased for target in self._enter_to]
        self._hero_shown = self._enter_hero * eased
        if step >= steps:
            self._enter_t0 = None

    def _advance_tween(self, now):
        if self._enter_t0 is not None:
            self._advance_entrance(now)
            return
        dt = now - self._anim_t0
        self._anim_t0 = now
        # After a reset the ring refills at a pace you can watch; bars keep theirs.
        refill = self._refill_t0 is not None
        hero = hero_index(self._snap) if refill else None
        self._shown_pcts = [follow_bar(a, b, dt * REFILL_PACE if index == hero else dt)
                            for index, (a, b) in enumerate(zip(self._shown_pcts, self._anim_to))]
        self._hero_shown = follow_bar(self._hero_shown, self._hero_target, dt * REFILL_PACE if refill else dt)
        if self._shown_pcts == self._anim_to and self._hero_shown == self._hero_target:
            self._anim_to = []
            self._anim_t0 = None

    # -- ring moments --------------------------------------------------------
    @staticmethod
    def _ring_reading(snap, shown):
        """(quota id, percent shown, severity band) of the ring for `snap`, or None when muted."""
        if snap is None or not snap.ok or snap.stale:
            return None
        index = hero_index(snap)
        limits = main_limits(snap)
        item = limits[index] if index is not None and index < len(limits) else None
        actual = item.remaining_percent if item is not None else representative_percent(snap)
        if actual is None:
            return None
        band = design_severity(actual, False, representative_blocked(snap))[0]
        return (item.quota_id if item is not None else None), shown, band

    def _ring_moment(self, before, after):
        """Start the trail of quota just used, the reset flash or the red pulse."""
        if not self.animate or before is None or after is None or before[0] != after[0]:
            # Nothing to compare, or the ring now shows another quota.
            return
        (_, was, was_band), (_, value, band) = before, after
        now = time.monotonic()
        if value < was - 0.25:
            self._ghost = max(self._ghost or 0.0, was)
            self._ghost_hold = now + GHOST_HOLD_S
            self._ghost_t0 = None
        elif value >= was + REFILL_JUMP:
            self._ghost = None
            self._refill_t0 = now
        if band in ('danger', 'critical') and RING_BANDS.index(band) > RING_BANDS.index(was_band):
            self._pulse_t0 = now

    def _moving(self):
        return (self._ghost is not None or self._refill_t0 is not None or self._pulse_t0 is not None
                or self._turn_t0 is not None)

    def _advance_effects(self, now):
        if self._turn_t0 is not None:
            self._advance_turn(now)
        if self._ghost is not None and now >= self._ghost_hold:
            dt = 0.0 if self._ghost_t0 is None else now - self._ghost_t0
            self._ghost_t0 = now
            shown = self._hero_value()[0]
            self._ghost = follow_bar(self._ghost, shown, dt)
            if self._ghost <= shown + 0.25:
                self._ghost = None
        if self._refill_t0 is not None and now - self._refill_t0 >= REFILL_S:
            self._refill_t0 = None
        if self._pulse_t0 is not None and now - self._pulse_t0 >= PULSE_BEATS * PULSE_BEAT_S:
            self._pulse_t0 = None

    def _pulse_beat(self, now):
        """0-1 swell of the ring's beat after it turns red."""
        if self._pulse_t0 is None:
            return 0.0
        beats = (now - self._pulse_t0) / PULSE_BEAT_S
        if not 0 <= beats < PULSE_BEATS:
            return 0.0
        return round(math.sin(math.pi * (beats % 1)) * RING_FX_STEPS) / RING_FX_STEPS

    def _refill_flash(self, now):
        """1-0 brightness of the ring as a reset refills it."""
        if self._refill_t0 is None:
            return 0.0
        left = max(0.0, min(1.0, 1 - (now - self._refill_t0) / REFILL_S))
        return round(left * RING_FX_STEPS) / RING_FX_STEPS

    def _ripple_for(self, now, size, thickness):
        """(radius, strength) of the circle spreading from the ring after a reset, or None."""
        if self._refill_t0 is None:
            return None
        progress = (now - self._refill_t0 - RIPPLE_DELAY_S) / (REFILL_S - RIPPLE_DELAY_S)
        if not 0 <= progress < 1:
            return None
        step = round(progress * RING_FX_STEPS) / RING_FX_STEPS
        start = size * 35 / 84 + thickness / 2 + 1
        end = size / 2 + self.metrics.p(RING_MARGIN) - 1.5
        return round(start + (end - start) * step, 1), round(0.75 * (1 - step), 2)

    def _shimmer_ready(self):
        # No light or thickening while the entrance fills the card: each would
        # need ring and bar images warm_entrance() has not drawn.
        return (self._enter_t0 is None and not self.collapsed and self._snap is not None and self._snap.ok
                and not self._snap.stale and any(percent > 0 for percent in self._shown_pcts))

    def _bar_height_for(self, index):
        base = self.metrics.bar_h
        if not self.animate or not self._shimmer_ready():
            return base
        return base + self.metrics.p(4) * self._emphasis

    def _hero_value(self):
        index = hero_index(self._snap)
        limits = main_limits(self._snap)
        if index is not None and index < len(self._shown_pcts):
            return self._shown_pcts[index], index, limits[index].remaining_percent
        return self._hero_shown, None, representative_percent(self._snap)

    def _paint_ring(self):
        snap, m = self._snap, self.metrics
        shown, _, actual = self._hero_value()
        # The ring's colour is read from the same quota as its number.
        _, _, color = design_severity(actual, snap.stale or not snap.ok, representative_blocked(snap))
        color = color or ACCENTS[self.key]
        emphasis = self._emphasis if self.animate and self._shimmer_ready() else 0.0
        emphasis = round(emphasis * RING_EMPHASIS_STEPS) / RING_EMPHASIS_STEPS
        now = time.monotonic()
        beat = self._pulse_beat(now)
        thickness = round((m.p(7) + m.p(2) * emphasis + m.p(3) * beat) * 2) / 2
        base = raster.blend(color, '#FFFFFF', .24 * emphasis)
        lit = .35 * max(beat, self._refill_flash(now))
        color = raster.blend(base, '#FFFFFF', lit) if lit else base
        ring_pct = 0 if actual is None else round(shown * 2) / 2
        size = m.p(84)
        ghost = None if self._ghost is None else round(self._ghost * 2) / 2
        ghost = ghost if ghost is not None and actual is not None and ghost > ring_pct else None
        # Paler than the ring even at the top of a beat or a flash (.35), so the two stay apart.
        ghost_color = raster.blend(base, '#FFFFFF', .6) if ghost is not None else None
        ripple = self._ripple_for(now, size, thickness)
        margin = m.p(RING_MARGIN) if ripple else 0
        signature = (size, ring_pct, color, thickness, ghost, ghost_color, ripple, margin)
        if getattr(self,'_ring_signature',None) != signature:
            self._ring_photo = ring_photo(size, ring_pct, color, thickness, ghost=ghost, ghost_color=ghost_color,
                                          ripple=ripple, ripple_color=base if ripple else None, margin=margin)
            self._ring_signature = signature
        hero = '—' if actual is None else f'{shown:.0f}%'
        # This runs every shimmer frame. Setting the same image or text again
        # still redrew the ring and the number, the larger part of a frame.
        shown_now = (self._ring_photo, hero, color)
        if shown_now == self._ring_shown:
            return
        self._ring_shown = shown_now
        self.rows.itemconfigure('ring', image=self._ring_photo)
        # A spreading circle needs a margin round the ring; the ring stays put.
        self.rows.coords('ring', m.p(16) - margin, m.p(54) - margin)
        self.rows.itemconfigure('hero',text=hero,fill=color)

    def _paint_shimmer(self):
        if self._snap is None or not self.rows.find_withtag('ring'):
            return
        if self._enter_t0 is not None and self._enter_step is not None and self._enter_t0 > time.monotonic():
            # Still waiting for its turn at zero; the frame before already shows that.
            return
        self._paint_ring()
        m = self.metrics
        limits = main_limits(self._snap)
        for index, origin in enumerate(self._bar_origins):
            if origin is None or index >= len(limits):
                continue
            shown = self._shown_pcts[index]
            _, _, color = design_severity(limits[index].remaining_percent, self._snap.stale)
            color = color or ACCENTS[self.key]
            if self._snap.stale:
                color = MUTED
            if self.key == 'cursor' and index > 0 and color == ACCENTS[self.key]:
                color = raster.blend(CARD,color,.7)
            height = self._bar_height_for(index)
            raster_height = m.bar_h + m.p(4) + 2
            photo = progress_photo(m.card_w-m.p(32),raster_height,height/2,
                chip_fill_width(m.card_w-m.p(32),shown),TRACK,color,CARD,
                shimmer=self._shimmer_phase_for(index),shape_height=height,cache=self._frame_cache())
            self.rows.itemconfigure('bar_'+str(index),image=photo)
            self._bar_photos[index+1] = photo

    def _place_service_status(self, canvas, y):
        """Draw this service's check time, cached-value cause, and failure actions."""
        snap = self._snap
        if snap is None:
            return y
        m = self.metrics
        label = service_status_text(snap)
        if label:
            color = WARN if (not snap.ok or snap.stale) else STATUS_FG
            item = canvas.create_text(
                m.p(16), m.p(y), text=label, anchor='nw',
                font=m.font(FONT_SUB), fill=color, tags='service_status',
                width=max(1, m.card_w - m.p(32)),
            )
            self._clock_text['service_status'] = label
            bbox = canvas.bbox(item)
            y = (bbox[3] / max(m.scale, 0.01) + 6) if bbox else y + 18
        if snap.ok and snap.stale and snap.error and snap.error not in (label or ''):
            item = canvas.create_text(
                m.p(16), m.p(y), text=snap.error, anchor='nw',
                font=m.font(FONT_SUB), fill=STATUS_FG, tags='error_text',
                width=max(1, m.card_w - m.p(32)),
            )
            bbox = canvas.bbox(item)
            y = (bbox[3] / max(m.scale, 0.01) + 6) if bbox else y + 18
        if service_needs_actions(snap):
            y = self._draw_actions(canvas, y)
        return y

    def _draw_actions(self, canvas, y):
        m = self.metrics
        font = tkfont.Font(root=canvas, font=m.font(FONT_BADGE))
        x = m.p(16)
        top = m.p(y)
        height = m.p(22)
        gap = m.p(8)
        for label, kind in (('다시 확인', 'retry'), ('로그인 안내', 'login')):
            width = font.measure(label) + m.p(16)
            round_rect(canvas, x, top, x + width, top + height, m.p(4), TRACK, tags=('action', kind))
            canvas.create_text(
                x + width / 2, top + height / 2, text=label, fill=TEXT,
                font=m.font(FONT_BADGE), tags=('action', kind),
            )
            self._actions.append((x, top, x + width, top + height, kind))
            x += width + gap
        return y + 30

    def _set_clock_text(self, item, text):
        if self._clock_text.get(item) != text:
            self.rows.itemconfigure(item, text=text)
            self._clock_text[item] = text

    def refresh_clock(self, now=None):
        now = time.time() if now is None else now
        if self._snap is None or int(now) == self._clock_second:
            return
        self._clock_second = int(now)
        if 'service_status' in self._clock_text:
            self._set_clock_text('service_status', service_status_text(self._snap, now))
        if self.collapsed:
            return
        # Countdown granularity follows the hero's measured window, not its label.
        hero = select_hero(self._snap) if self._snap.ok else None
        prefer_days = hero is None or not window_matches(hero, FIVE_HOURS, FIVE_HOUR_TOLERANCE)
        self._set_clock_text('countdown', reset_countdown(self._reset_epoch,now,prefer_days))
        for canvas_id, reset_at in self._secondary_clocks.values():
            days = max(0,int((reset_at-now)//86400))
            self._set_clock_text(canvas_id, f'{days}일 남음' if days else reset_countdown(reset_at,now))

    def _paint(self,snap,percents):
        m, c = self.metrics, self.rows
        c.delete('all')
        self._ring_shown = None  # new ring and hero items show nothing yet
        # Canvas IDs and displayed text belong to this paint, including collapsed cards.
        self._clock_text = {}
        self._clock_second = None
        self._actions = []
        limits = main_limits(snap)
        self._bar_origins = [None] * len(limits)
        self._bar_photos = [None] * (len(limits)+1)
        self._secondary_clocks = {}
        primary_index = hero_index(snap)
        primary = limits[primary_index] if primary_index is not None else None
        # The hero's reset comes from its own QuotaItem, never from re-reading
        # a rendered caption or a footer string.
        self._reset_epoch = primary.reset_at if primary is not None else None
        reset = fmt_local(self._reset_epoch, 'reset') if self._reset_epoch else ''
        hero_blocked = representative_blocked(snap)
        service_percent = status_percent(snap)
        _, label, state = design_severity(service_percent,snap.stale or not snap.ok,hero_blocked)
        if hero_blocked and snap.ok and not snap.stale and snap.hero_caption:
            # blocked is a provider-level semantic state, so its caption is
            # shown for whichever provider reports it.
            label = snap.hero_caption
        state = state or ACCENTS[self.key]
        def text(x,y,value,font=FONT_ROW,color=TEXT,anchor='nw',tags=()):
            return c.create_text(m.p(x),m.p(y),text=value,font=m.font(font),fill=color,anchor=anchor,tags=tags)
        # A flat, divided surface matches the reference; keep all data-driven rows.
        self.height = m.p(170)
        c.configure(bg=CARD)
        if self._service_icon is not None:
            c.create_image(m.p(27),m.p(27),image=self._service_icon,tags='service_icon')
        title = text(46,18,TITLES[self.key],FONT_SERVICE)
        # The usage page opens from here only, with room round the arrow to hit it.
        left = (c.bbox(title) or (0, 0, m.p(80), 0))[2] + m.p(5)
        link = c.create_text(left, m.p(27), text='↗', anchor='w', font=m.font(FONT_ROW),
                             fill=TEXT if self._link_lit else MUTED, tags='page_link')
        box = c.bbox(link)
        self._page_link = (box[0] - m.p(4), m.p(14), box[2] + m.p(4), m.p(40)) if box else None
        if self.on_toggle:
            # Same colour as the header's line icons, centred on the service icon row.
            c.create_image(m.p(8), m.p(27), tags='collapse_toggle')
            self._chevron_turn = None
            self._paint_chevron()
        plan = '' if snap.plan=='-' else snap.plan.upper()
        font = tkfont.Font(root=c,font=m.font(FONT_PLAN))
        plan_w = font.measure(plan)+m.p(14) if plan else 0
        right = m.card_w-m.p(16)
        if plan:
            round_rect(c,right-plan_w,m.p(16),right,m.p(38),m.p(4),TRACK)
            c.create_text(right-plan_w/2,m.p(27),text=plan,font=m.font(FONT_PLAN),fill=MUTED)
        badge_w = tkfont.Font(root=c,font=m.font(FONT_BADGE)).measure(label)+m.p(25)
        right -= plan_w+m.p(8)
        round_rect(c,right-badge_w,m.p(16),right,m.p(38),m.p(11),raster.blend(CARD,state,.15))
        c.create_oval(right-badge_w+m.p(8),m.p(25),right-badge_w+m.p(13),m.p(30),fill=state,outline='')
        c.create_text(right-badge_w+m.p(18),m.p(27),text=label,anchor='w',font=m.font(FONT_BADGE),fill=raster.blend(state,TEXT,.4),tags='severity')
        if not snap.stale and (hero_blocked or (service_percent is not None and service_percent<20)):
            c.create_rectangle(0,0,m.card_w,m.p(2),fill=state,outline='',tags='strip')
        if self.collapsed:
            value = representative_percent(snap)
            summary = '확인 중' if value is None else f'잔여 {value:.0f}%'
            name = quota_row_title(primary) if primary is not None else '사용량'
            text(16,54,f'{name} · {summary}',FONT_ROW,MUTED,tags='collapsed_summary')
            bottom = self._place_service_status(c, 72)
            self.hero_height = self.height = m.p(max(86, bottom + 14))
            c.configure(width=m.card_w,height=self.height)
            self._sync_additional()
            c.create_line(0,self.hero_height-1,m.card_w,self.hero_height-1,fill=HAIR)
            return
        c.create_image(m.p(16),m.p(54),anchor='nw',tags='ring')
        c.create_text(m.p(58),m.p(96),text='',font=m.font(FONT_HERO),tags='hero')
        hero_title = (quota_row_title(primary) + ' · 남은 사용량'
                      if primary is not None else '남은 사용량')
        text(114,64,hero_title,FONT_SERVICE)
        text(114,88,'다음 리셋',FONT_META,MUTED)
        text(172,85,'', (FACE_SEMI,-15),TEXT,tags='countdown')
        # A five-hour window shows a clock; anything longer shows a date.
        short_reset = primary is not None and window_matches(primary, FIVE_HOURS, FIVE_HOUR_TOLERANCE)
        text(114,112,reset_stamp(reset) if short_reset else dated_reset_stamp(reset),FONT_META,DIM)
        y = 156
        if no_five_hour_limit(snap):
            # Where the weekly row sits when the ring shows five hours.
            text(16,y,NO_FIVE_HOUR_NOTE,FONT_META,MUTED,tags='five_hour_note')
            y += 24
        drawn = 0
        hidden = 0
        for index,item in enumerate(limits):
            if index == primary_index:
                continue
            if drawn >= MAX_SECONDARY_ROWS:
                hidden += 1
                continue
            drawn += 1
            text(16,y,quota_row_title(item),FONT_ROW,MUTED)
            value = item.remaining_percent
            c.create_text(m.card_w-m.p(16),m.p(y),text='—' if value is None else f'{value:.0f}%',font=m.font(FONT_VALUE),fill=TEXT,anchor='ne',tags='bar_value_'+str(index))
            bar_y = m.p(y+23)
            raster_height = m.bar_h+m.p(4)+2
            self._bar_origins[index] = (m.p(16),bar_y)
            c.create_image(m.p(16),bar_y-(raster_height-m.bar_h)/2,anchor='nw',tags='bar_'+str(index))
            y += 42
            if item.window_seconds is not None and item.reset_at:
                stamp = fmt_local(item.reset_at, 'reset')
                text(16,y,item.display_name + ' 리셋 ' + dated_reset_stamp(stamp).removesuffix(' 리셋'),FONT_META,DIM)
                clock_id = c.create_text(m.card_w-m.p(16),m.p(y),text='',font=m.font(FONT_META),fill=DIM,anchor='ne',tags='week_remaining')
                self._secondary_clocks[item.quota_id] = (clock_id, item.reset_at)
                y += 22
        if hidden:
            text(16,y,f'그 외 한도 {hidden}개',FONT_META,MUTED)
            y += 24
        extras = quota_extras(snap) if snap.ok else ''
        if extras:
            text(16,y,extras,FONT_META,MUTED)
            y += 24
        amount = included_amount(snap) if snap.ok else ''
        bonus = bonus_line(snap) if snap.ok else ''
        if amount or bonus:
            c.create_line(m.p(16),m.p(y),m.card_w-m.p(16),m.p(y),fill=HAIR,dash=(3,3))
            y += 14
        if bonus:
            round_rect(c,m.p(16),m.p(y),m.card_w-m.p(16),m.p(y+38),m.p(8),'#2C271D')
            text(26,y+11,'◇ 보너스 사용액',FONT_ROW,'#FFD27A')
            c.create_text(m.card_w-m.p(26),m.p(y+11),text=bonus.removeprefix('보너스').strip(),anchor='ne',fill='#FFD27A',font=m.font(FONT_VALUE))
            y += 50
        if amount:
            text(16,y,'기본 포함량',FONT_ROW,MUTED)
            c.create_text(m.card_w-m.p(16),m.p(y),text=amount,anchor='ne',fill=TEXT,font=m.font(FONT_VALUE))
            y += 24
        if not snap.ok:
            error_id = c.create_text(
                m.p(16), m.p(150), text=snap.error or '조회 중', anchor='nw',
                font=m.font(FONT_SUB), fill=STATUS_FG, tags='error_text',
                width=m.card_w - m.p(32),
            )
            bbox = c.bbox(error_id)
            y = 178
            if bbox:
                y = max(y, bbox[3] / max(m.scale, 0.01) + 8)
        y = self._place_service_status(c, y)
        self.height=m.p(y+16)
        c.configure(width=m.card_w,height=self.height)
        self.hero_height = self.height
        self._sync_additional()
        c.create_line(0,self.hero_height-1,m.card_w,self.hero_height-1,fill=HAIR)
        self._paint_shimmer()
        self.refresh_clock()
