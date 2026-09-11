"""Small, read-only quota monitor. All Tk calls stay on the main thread."""
from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import re
import struct
import sys
import threading
import time
import traceback
import tkinter as tk
import webbrowser
import zlib
from dataclasses import replace
from pathlib import Path
from tkinter import messagebox, font as tkfont

from providers import error_snapshot, snapshot_from_dict, snapshot_to_dict
from runtime import AlertGate, AuthWatcher, PollRunner, ToastSender, limiting_quota, login_present, login_status, prepare_action, session_locked, start_tool_setup
from updater import APP_VERSION, CHECK_EVERY, download_and_stage, fetch_latest, load_feed_url, start_apply, update_confirm_text

APP_DIR = Path(os.environ.get('APPDATA', str(Path.home()))) / 'AiUsageWidget'
SETTINGS_PATH = APP_DIR / 'settings.json'
CACHE_PATH = APP_DIR / 'last_snapshot.json'
ALERT_PATH = APP_DIR / 'alerts.json'
ICON_DIR = Path(__file__).resolve().parent / 'assets' / 'icons'

# Variant A: pixel dimensions from the supplied tokens.json.
TOKENS = {'width': 360,
 'radius': {'window': 12, 'card': 12, 'pill': 999, 'bar': 4},
 'strip': {'width': 3, 'inset_top': 12, 'inset_bottom': 12},
 'header_h': 40,
 'compact_h': 44,
 'footer_h': 28,
 'gap': {'cards': 10, 'rows': 12, 'label_bar': 6, 'window_pad_x': 12, 'window_pad_y': 10},
 'card': {'pad_x': 16, 'pad_y': 14, 'hairline': 1},
 'bar': {'height': 8, 'track_radius': 4},
 'chip': {'height': 24,
          'radius': 999,
          'pad_x': 10,
          'gap_between': 6,
          'font_size': 12,
          'font_weight': 'semibold',
          'fg_on_fill': '#F2FFFB',
          'fg_on_fill_stale': '#8A92A6'},
 'fonts': {'family_latin': 'Segoe UI Semibold',
           'family_hangul': 'Malgun Gothic',
           'css_stack': '"Segoe UI Semibold", "Malgun Gothic", "맑은 고딕", sans-serif',
           'size': {'title': 13,
                    'hero_num': 44,
                    'hero_sub': 12,
                    'row_label': 12,
                    'row_value': 12,
                    'caption': 11,
                    'plan': 11,
                    'pill': 11,
                    'footer': 11,
                    'chip': 12},
           'weight_note': 'Segoe UI Semibold for latin/digits; Malgun Gothic auto-fallback for '
                          'hangul'},
 'color': {'bg_window': '#12141A',
           'bg_card': '#1A1D26',
           'hairline': '#262A36',
           'track': '#262A36',
           'fg': '#E6E8EE',
           'fg_muted': '#8A92A6',
           'fg_dim': '#5B6478',
           'codex': '#4FE0B0',
           'cursor': '#B39AF7',
           'warn': '#F6B44A',
           'danger': '#F26D6D',
           'stale_strip': '#4A5060',
           'stale_hero': '#8A92A6',
           'icon': '#B4BAC8',
           'icon_hover': '#E6E8EE',
           'close_hover_bg': '#3A2020',
           'chip_codex_fill': '#1F6B5A',
           'chip_cursor_fill': '#4E3F7A',
           'chip_warn_fill': '#C48A22',
           'chip_danger_fill': '#B44545',
           'chip_stale_fill': '#3A4252'},
 'thresholds': {'warn_pct_at_or_below': 30, 'danger_pct_at_or_below': 15},
 'rules': {'hero_shows': 'remaining_percent',
           'bar_color_is_per_row': True,
           'stale_grays_strip_and_hero_only': True,
           'reset_caption_only_on_codex_5h_bar': True,
           'reset_caption_format': 'HH:MM 재설정',
           'compact_pill_format': '{service} {pct}%',
           'chip_fills_are_own_palette': 'do not reuse detail bar hex (#4FE0B0/#B39AF7) on chip '
                                         'fills; white text needs darker fill',
           'card_dot_no_halo': True,
           'title_is_one_line': True,
           'no_badges': ['이전', '제한']},
 'labels': {'title': 'AI Usage',
            'codex': 'Codex',
            'cursor': 'Cursor',
            'codex_hero_sub': '5시간 기준 잔여',
            'cursor_hero_sub': '전체 잔여',
            'codex_row_5h': '5시간',
            'codex_row_weekly': '주간',
            'codex_row_extra': '추가',
            'codex_extra_value': '리셋권 1',
            'cursor_row_own': '자사 모델',
            'cursor_row_api': 'API 사용량',
            'cursor_footer_line': '기본 포함량',
            'footer_ok': '자동 감지',
            'footer_empty': '사용량 소진',
            'footer_stale': '일부 데이터 이전 기준',
            'refresh_hint': 'F5 새로고침'}}
BG, CARD, HAIR, TRACK = (TOKENS['color'][k] for k in ('bg_window','bg_card','hairline','track'))
TEXT, MUTED, DIM = (TOKENS['color'][k] for k in ('fg','fg_muted','fg_dim'))
CODEX, CURSOR = TOKENS['color']['codex'], TOKENS['color']['cursor']
WARN, DANGER = TOKENS['color']['warn'], TOKENS['color']['danger']
STALE_STRIP, STALE_HERO = TOKENS['color']['stale_strip'], TOKENS['color']['stale_hero']
ICON, HOVER, CLOSE_HOVER = TOKENS['color']['icon'], '#20232D', TOKENS['color']['close_hover_bg']
CHIP_CODEX, CHIP_CURSOR = TOKENS['color']['chip_codex_fill'], TOKENS['color']['chip_cursor_fill']
CHIP_WARN, CHIP_DANGER, CHIP_STALE = (TOKENS['color'][k] for k in ('chip_warn_fill','chip_danger_fill','chip_stale_fill'))
CHIP_FG, CHIP_TRACK = '#F2FFFB', '#2A3142'
GREEN, AMBER, RED, LINE = CODEX, WARN, DANGER, HAIR
ACCENTS = {'chatgpt': CODEX, 'cursor': CURSOR}
CHIP_OK = {'chatgpt': CHIP_CODEX, 'cursor': CHIP_CURSOR}
TITLES = {'chatgpt': 'Codex', 'cursor': 'Cursor'}
ICON_HINTS = {
    'refresh': '새로고침 (F5)',
    'minus': '한 줄로 접기',
    'expand': '상세로 펼치기',
    'close': '종료',
}
HERO_SUB = {'chatgpt': '5시간 기준 잔여', 'cursor': '전체 잔여'}
URLS = {'chatgpt': 'https://chatgpt.com/codex/settings/usage', 'cursor': 'https://cursor.com/dashboard/usage'}
FETCHERS = ('chatgpt', 'cursor')
WARN_AT, DANGER_AT = 30, 15
WINDOW_W, HEADER_H, COMPACT_H, FOOTER_H = (TOKENS[k] for k in ('width','header_h','compact_h','footer_h'))
BAR_H, STRIP_W, CHIP_H = TOKENS['bar']['height'], TOKENS['strip']['width'], TOKENS['chip']['height']
CARD_W, CARD_RADIUS, CARD_GAP = WINDOW_W - 26, TOKENS['radius']['card'], TOKENS['gap']['cards']
# Negative Tk font sizes are pixels, avoiding point/DPI-driven layout inflation.
FONT_TITLE = ('Segoe UI Semibold', -13)
FONT_SERVICE = ('Segoe UI Semibold', -14)
FONT_HERO = ('Segoe UI Semibold', -44)
FONT_SUB = ('Malgun Gothic', -12)
FONT_PLAN = ('Segoe UI Semibold', -11)
FONT_ROW = ('Malgun Gothic', -12)
FONT_VALUE = ('Segoe UI Semibold', -12)
FONT_META = ('Malgun Gothic', -11)
FONT_CHIP = ('Segoe UI Semibold', -12)
FONT_PILL = ('Malgun Gothic', -11, 'bold')
FONT_FOOT = ('Malgun Gothic', -11)
FONT_BADGE = ('Malgun Gothic', -11)
SCALE_MIN, SCALE_MAX, SCALE_STEP, DEFAULT_SCALE = 0.75, 1.5, 0.15, 1.0


def clamp_scale(value, default=DEFAULT_SCALE):
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(scale):
        return default
    return max(SCALE_MIN, min(SCALE_MAX, round(scale, 2)))


def px(value, scale, minimum=0):
    return max(minimum, int(round(float(value) * float(scale))))


def scaled_font(font, scale):
    family, size = font[0], font[1]
    sign = -1 if size < 0 else 1
    return (family, sign * max(1, int(round(abs(size) * float(scale))))) + tuple(font[2:])


def step_scale(scale, steps=1):
    return clamp_scale(clamp_scale(scale) + SCALE_STEP * int(steps))


class Metrics:
    """Variant A token sizes multiplied by the user scale."""

    def __init__(self, scale=DEFAULT_SCALE):
        self.scale = clamp_scale(scale)

    def p(self, value, minimum=0):
        return px(value, self.scale, minimum)

    def font(self, spec):
        return scaled_font(spec, self.scale)

    @property
    def window_w(self):
        return self.p(WINDOW_W, 1)

    @property
    def header_h(self):
        return self.p(HEADER_H, 1)

    @property
    def compact_h(self):
        return self.p(COMPACT_H, 1)

    @property
    def footer_h(self):
        return self.p(FOOTER_H, 1)

    @property
    def bar_h(self):
        return self.p(BAR_H, 1)

    @property
    def strip_w(self):
        return self.p(STRIP_W, 1)

    @property
    def chip_h(self):
        return self.p(CHIP_H, 1)

    @property
    def chip_w(self):
        return self.p(82, 1)

    @property
    def pill_h(self):
        return self.p(22, 1)

    @property
    def card_w(self):
        return self.p(CARD_W, 1)

    @property
    def card_radius(self):
        return self.p(CARD_RADIUS, 1)

    @property
    def card_gap(self):
        return self.p(CARD_GAP, 1)

    @property
    def icon(self):
        return self.p(24, 1)


def read_json(path):
    try:
        if path.stat().st_size > 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(tmp, path)


def visual_state(snap):
    if snap.stale:
        return 'stale'
    if not snap.ok or snap.blocked or (snap.hero_percent is not None and snap.hero_percent <= DANGER_AT):
        return 'danger'
    if snap.hero_percent is not None and snap.hero_percent <= WARN_AT:
        return 'warn'
    return 'ok'


def color_for(snap):
    state = visual_state(snap)
    if state == 'stale':
        return STALE_HERO
    if state == 'warn':
        return WARN
    if state == 'danger':
        return DANGER
    return ACCENTS.get(getattr(snap, 'key', ''), CODEX)


def strip_color(key, snap):
    state = visual_state(snap)
    if state == 'stale':
        return STALE_STRIP
    if state == 'warn':
        return WARN
    if state == 'danger':
        return DANGER
    return ACCENTS[key]


def bar_color(key, remaining, stale):
    if remaining is None:
        return TRACK
    if stale:
        return ACCENTS[key]
    if remaining <= DANGER_AT:
        return DANGER
    if remaining <= WARN_AT:
        return WARN
    return ACCENTS[key]


def chip_style(key, snap):
    if snap is None:
        return CHIP_STALE, CHIP_FG
    state = visual_state(snap)
    if state == 'stale':
        return CHIP_STALE, CHIP_FG
    if state == 'danger':
        return CHIP_DANGER, CHIP_FG
    if state == 'warn':
        return CHIP_WARN, CHIP_FG
    return CHIP_OK[key], CHIP_FG


def chip_fill_width(total, percent):
    if percent is None:
        return 0.0
    try:
        value = float(percent)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(float(total), float(total) * max(0.0, min(100.0, value)) / 100.0))


def reset_stamp(text):
    if not text:
        return ''
    found = re.search(r'(\d{1,2}:\d{2})', text)
    return f'{found.group(1)} 재설정' if found else ''


def reset_credit(snap):
    for row in snap.info_rows:
        if row.label == '추가':
            for part in row.value.replace(',', '·').split('·'):
                part = part.strip()
                if part.startswith('리셋권'):
                    return part
    return ''


def included_amount(snap):
    for row in snap.info_rows:
        if row.label == '기본 포함량':
            return row.value
    return ''


def bonus_line(snap):
    for part in (snap.footer or '').split('·'):
        part = part.strip()
        if part.startswith('보너스'):
            return part
    return ''


def next_interval(snap, failures=0):
    if failures:
        return min(900, 30 * (2 ** min(failures - 1, 5)))
    if not snap or not snap.ok:
        return 60
    if snap.blocked or snap.hero_percent == 0:
        return 300
    if snap.hero_percent is not None and snap.hero_percent <= 10:
        return 20
    if snap.hero_percent is not None and snap.hero_percent <= 35:
        return 30
    return 60


def should_setup(settings, preview=False):
    if preview:
        return False
    if settings.get('setup_done'):
        return False
    if settings.get('version') and isinstance(settings.get('enabled'), dict):
        return False
    return True


def default_enabled(settings, preview=False, present=None):
    saved = settings.get('enabled')
    if isinstance(saved, dict) and not should_setup(settings, preview):
        return {key: bool(saved.get(key, True)) for key in FETCHERS}
    present = present if present is not None else {key: login_present(key) for key in FETCHERS}
    if any(present.values()):
        return {key: bool(present.get(key)) for key in FETCHERS}
    return {key: True for key in FETCHERS}


def _monitor_rects(x, y):
    try:
        from ctypes import wintypes as wt
        class Info(ctypes.Structure):
            _fields_ = [('size', wt.DWORD), ('monitor', wt.RECT), ('work', wt.RECT), ('flags', wt.DWORD)]
        api = ctypes.windll.user32
        api.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
        api.MonitorFromPoint.restype = wt.HANDLE
        api.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(Info)]
        info = Info(); info.size = ctypes.sizeof(info)
        monitor = api.MonitorFromPoint(wt.POINT(x, y), 2)
        if api.GetMonitorInfoW(monitor, ctypes.byref(info)):
            work = info.work
            full = info.monitor
            return (
                (work.left, work.top, work.right, work.bottom),
                (full.left, full.top, full.right, full.bottom),
            )
    except (AttributeError, OSError):
        pass
    fallback = (0, 0, 1920, 1080)
    return fallback, fallback


def work_area(x, y):
    return _monitor_rects(x, y)[0]


def monitor_area(x, y):
    return _monitor_rects(x, y)[1]


def clamp_position(x, y, w, h, work, monitor):
    ml, mt, mr, mb = monitor
    wl, wt, wr, wb = work
    x = min(max(int(x), ml), max(ml, mr - w))
    y = min(max(int(y), mt), max(mt, mb - h))
    for edge in (wl + 8, wr - w - 8, ml + 8, mr - w - 8):
        if abs(x - edge) < 18:
            x = edge
    for edge in (wt + 8, mt + 8, wb - h, mb - h):
        if abs(y - edge) < 18:
            y = edge
    return x, y


def set_over_taskbar(root, on=True):
    try:
        hwnd = ctypes.c_void_p(int(root.wm_frame(), 16))
        insert = ctypes.c_void_p(-1 if on else -2)
        ctypes.windll.user32.SetWindowPos(hwnd, insert, 0, 0, 0, 0, 0x0013)
    except (AttributeError, OSError, ValueError, tk.TclError):
        pass


def keep_topmost_style(hwnd, on=True):
    """Keep WS_EX_TOPMOST without restacking above an already-open menu/dialog."""
    handle = int(hwnd or 0)
    if not handle:
        return
    try:
        user32 = ctypes.windll.user32
        getter = getattr(user32, 'GetWindowLongPtrW', user32.GetWindowLongW)
        setter = getattr(user32, 'SetWindowLongPtrW', user32.SetWindowLongW)
        style = int(getter(ctypes.c_void_p(handle), -20) or 0)
        flag = 0x00000008
        style = (style | flag) if on else (style & ~flag)
        setter(ctypes.c_void_p(handle), -20, style)
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        pass


def raise_over_taskbar(root):
    set_over_taskbar(root, True)


def lift_owned_popups(owner_hwnd=0):
    """Raise this process's dialogs/menus above the widget without dropping the widget."""
    try:
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):
        return
    insert = ctypes.c_void_p(-1)
    owner = int(owner_hwnd or 0)
    pid = os.getpid()
    buf = ctypes.create_unicode_buffer(256)

    def lift(hwnd):
        handle = int(hwnd or 0)
        if not handle or handle == owner:
            return
        try:
            user32.SetWindowPos(ctypes.c_void_p(handle), insert, 0, 0, 0, 0, 0x0013)
        except (AttributeError, OSError, OverflowError, TypeError, ValueError):
            pass

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        handle = int(hwnd or 0)
        if not handle or handle == owner:
            return True
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            other = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(other))
            if other.value != pid:
                return True
            owned = int(user32.GetWindow(hwnd, 4) or 0)
            user32.GetClassNameW(hwnd, buf, 256)
            if owned == owner or buf.value in ('#32770', '#32768', 'TkTopLevel'):
                lift(handle)
        except (AttributeError, OSError, OverflowError, TypeError, ValueError):
            pass
        return True

    try:
        user32.EnumWindows(callback, 0)
        dialog = user32.FindWindowW('#32770', None)
        if dialog:
            other = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(dialog, ctypes.byref(other))
            if other.value == pid:
                lift(dialog)
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        pass


def lift_menu_windows(extra_hwnd=0):
    """Raise native/Tk popup menus into the TOPMOST band; do not touch the widget."""
    try:
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):
        return
    insert = ctypes.c_void_p(-1)

    def lift(hwnd):
        if not hwnd:
            return
        try:
            user32.SetWindowPos(ctypes.c_void_p(int(hwnd)), insert, 0, 0, 0, 0, 0x0013)
        except (AttributeError, OSError, OverflowError, TypeError, ValueError):
            pass

    try:
        lift(extra_hwnd)
        hwnd = user32.FindWindowW('#32768', None)
        if not hwnd:
            return
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            lift(hwnd)
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        pass


def lift_tip_window(win):
    """Raise a mapped Tip Toplevel into the TOPMOST band without activating it."""
    if win is None:
        return
    try:
        if not win.winfo_exists() or not win.winfo_ismapped():
            return
    except (AttributeError, tk.TclError):
        return
    set_over_taskbar(win, True)


def geometry_at(x, y):
    return f'+{int(x)}+{int(y)}'


def startup_path():
    return Path(os.environ.get('APPDATA', '')) / 'Microsoft/Windows/Start Menu/Programs/Startup/AIUsageWidget.vbs'


def set_startup(enabled):
    path = startup_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    exe = Path(sys.executable).with_name('pythonw.exe')
    if not exe.is_file():
        raise RuntimeError('pythonw.exe를 찾을 수 없습니다.')
    script = Path(__file__).resolve()
    value = ('Set sh = CreateObject("Wscript.Shell")\r\n'
             f'sh.CurrentDirectory = "{script.parent}"\r\n'
             f'sh.Run """{exe}"" ""{script}""", 0, False\r\n')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-16')


def load_icon(name, scale):
    # The whole design uses 1x pixel dimensions; the supplied icons are 16x16.
    path = ICON_DIR / f'{name}.png'
    return tk.PhotoImage(data=path.read_bytes(), format='png') if path.is_file() else None


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


def _hex_rgb(value):
    value = value.lstrip('#')
    return tuple(int(value[i:i+2], 16) for i in (0, 2, 4))


def lighten(color, amount=0.18):
    r, g, b = _hex_rgb(color)
    mix = lambda c: min(255, int(round(c + (255 - c) * amount)))
    return '#%02X%02X%02X' % (mix(r), mix(g), mix(b))


def blend(a, b, t):
    t = max(0.0, min(1.0, float(t)))
    ar, ag, ab = _hex_rgb(a)
    br, bg, bb = _hex_rgb(b)
    mix = lambda x, y: int(round(x + (y - x) * t))
    return '#%02X%02X%02X' % (mix(ar, br), mix(ag, bg), mix(ab, bb))


def _cover_round_rect(px, py, width, height, radius):
    if width <= 0 or height <= 0:
        return 0.0
    radius = max(0.0, min(float(radius), width / 2.0, height / 2.0))
    dx = abs(px - width / 2.0) - (width / 2.0 - radius)
    dy = abs(py - height / 2.0) - (height / 2.0 - radius)
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0)) + min(max(dx, dy), 0.0) - radius
    return max(0.0, min(1.0, 0.5 - outside))


def _box_downsample(rows, samples, dst_w, dst_h):
    n = samples * samples
    out = []
    for y in range(dst_h):
        row = bytearray()
        for x in range(dst_w):
            rs = gs = bs = 0
            for dy in range(samples):
                src = rows[y * samples + dy]
                for dx in range(samples):
                    i = (x * samples + dx) * 4
                    rs += src[i]; gs += src[i+1]; bs += src[i+2]
            row.extend((rs // n, gs // n, bs // n, 255))
        out.append(row)
    return dst_w, dst_h, out


def progress_bar_rgba(width, height, radius, fill_width, track, fill, background, samples=1):
    """Track + clipped fill as opaque RGBA rows. Fill cannot paint outside the track."""
    samples = max(1, int(samples))
    width = max(1, int(round(width)))
    height = max(1, int(round(height)))
    fill_width = max(0.0, min(float(width), float(fill_width)))
    if samples > 1:
        src_w, src_h, rows = progress_bar_rgba(
            width * samples, height * samples, radius * samples, fill_width * samples,
            track, fill, background, samples=1)
        return _box_downsample(rows, samples, width, height)
    tr, tg, tb = _hex_rgb(track)
    fr, fg, fb = _hex_rgb(fill)
    br, bg_, bb = _hex_rgb(background)
    rows = []
    for y in range(height):
        py = y + 0.5
        row = bytearray()
        for x in range(width):
            px = x + 0.5
            track_a = _cover_round_rect(px, py, width, height, radius)
            fill_a = _cover_round_rect(px, py, fill_width, height, radius) if fill_width > 0 else 0.0
            fill_a = min(fill_a, track_a)
            r = fr * fill_a + tr * (track_a - fill_a) + br * (1.0 - track_a)
            g = fg * fill_a + tg * (track_a - fill_a) + bg_ * (1.0 - track_a)
            b = fb * fill_a + tb * (track_a - fill_a) + bb * (1.0 - track_a)
            row.extend((int(r + 0.5), int(g + 0.5), int(b + 0.5), 255))
        rows.append(row)
    return width, height, rows


def _png_rgba(width, height, rows):
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    raw = b''.join(b'\x00' + bytes(row) for row in rows)
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b'')


def progress_bar_png(width, height, radius, fill_width, track, fill, background, samples=1):
    w, h, rows = progress_bar_rgba(width, height, radius, fill_width, track, fill, background, samples=samples)
    return _png_rgba(w, h, rows)


def progress_photo(width, height, radius, fill_width, track, fill, background, samples=1):
    return tk.PhotoImage(data=progress_bar_png(width, height, radius, fill_width, track, fill, background, samples=samples), format='png')


def round_photo(width, height, radius, fill, background, pad=1):
    w, h, rows = padded_stadium_rgba(width, height, radius, fill, background, pad=pad, samples=4)
    return tk.PhotoImage(data=_png_rgba(w, h, rows), format='png'), pad


def padded_stadium_rgba(width, height, radius, fill, background, pad=1, samples=4):
    """Stadium with 1px AA padding so 3px caps can round without looking square-cut."""
    width = max(1, int(round(width)))
    height = max(1, int(round(height)))
    pad = max(0, int(pad))
    samples = max(1, int(samples))
    img_w, img_h = width + pad * 2, height + pad * 2
    sw, sh = img_w * samples, img_h * samples
    fr, fg, fb = _hex_rgb(fill)
    br, bg_, bb = _hex_rgb(background)
    ox, oy = pad * samples, pad * samples
    rw, rh, rr = width * samples, height * samples, radius * samples
    src = []
    for y in range(sh):
        py = y + 0.5
        row = bytearray()
        for x in range(sw):
            px = x + 0.5
            a = _cover_round_rect(px - ox, py - oy, rw, rh, rr)
            row.extend((
                int(fr * a + br * (1.0 - a) + 0.5),
                int(fg * a + bg_ * (1.0 - a) + 0.5),
                int(fb * a + bb * (1.0 - a) + 0.5),
                255,
            ))
        src.append(row)
    return _box_downsample(src, samples, img_w, img_h)


def baseline_text(canvas, x, y, text, font, fill, right=False, tags=()):
    if not hasattr(canvas, '_font_cache'):
        canvas._font_cache = {}
    if font not in canvas._font_cache:
        canvas._font_cache[font] = tkfont.Font(root=canvas, font=font)
    face = canvas._font_cache[font]
    return canvas.create_text(x, y+face.metrics('descent'), text=text, font=face,
                              fill=fill, anchor='se' if right else 'sw', tags=tags)


def notify_user(title, text, icon=0x10):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x00040000 | icon)
    except (AttributeError, OSError):
        pass


def log_launch(message):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    line = time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + message + '\n'
    with (APP_DIR / 'launch.log').open('a', encoding='utf-8') as log:
        log.write(line)


def record_crash():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    text = time.strftime('%Y-%m-%d %H:%M:%S') + '\n' + traceback.format_exc()
    (APP_DIR / 'error.log').write_text(text, encoding='utf-8')
    log_launch('crash')
    return text


def read_lock(path=None):
    path = path or (APP_DIR / 'widget.lock')
    raw = path.read_text(encoding='ascii', errors='replace').strip().splitlines()
    pid = int(raw[0]) if raw else 0
    hwnd = int(raw[1]) if len(raw) >= 2 else 0
    return pid, hwnd


def process_alive(pid):
    if pid <= 0:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def show_window(hwnd):
    user32 = ctypes.windll.user32
    handle = ctypes.c_void_p(int(hwnd))
    if not hwnd or not user32.IsWindow(handle):
        return False
    user32.ShowWindow(handle, 9)
    user32.ShowWindow(handle, 5)
    user32.SetWindowPos(handle, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0013)
    user32.SetForegroundWindow(handle)
    return True


def windows_for_pid(pid):
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        other = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(other))
        if other.value == pid:
            found.append(int(hwnd))
        return True

    ctypes.windll.user32.EnumWindows(callback, 0)
    return found


def activate_existing():
    try:
        pid, hwnd = read_lock()
    except (OSError, ValueError, IndexError):
        return False
    if show_window(hwnd):
        return True
    for other in windows_for_pid(pid):
        if show_window(other):
            return True
    return False


def clear_stale_lock():
    try:
        pid, _ = read_lock()
    except (OSError, ValueError, IndexError):
        pid = 0
    if process_alive(pid):
        return False
    try:
        (APP_DIR / 'widget.lock').unlink()
        return True
    except OSError:
        return False


class Instance:
    """Windows byte lock compatible with v1, held until process shutdown."""
    def __init__(self):
        self.handle = None

    def claim(self, retry=True):
        import msvcrt
        APP_DIR.mkdir(parents=True, exist_ok=True)
        path = APP_DIR / 'widget.lock'
        f = open(path, 'a+', encoding='ascii')
        if path.stat().st_size == 0:
            f.write('0'); f.flush()
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            f.close()
            log_launch('lock busy')
            if activate_existing():
                log_launch('activated existing window')
                return False
            if retry and clear_stale_lock():
                log_launch('cleared stale lock')
                return self.claim(False)
            notify_user(
                'AI Usage',
                'widget.lock 때문에 시작하지 못했습니다.\n\n'
                '작업 관리자 세부 정보에서 pythonw.exe와 python.exe를 종료하고,\n'
                '%APPDATA%\\AiUsageWidget\\widget.lock 을 지운 뒤 다시 실행하세요.',
                0x40,
            )
            return False
        self.handle = f
        return True

    def identify(self, hwnd):
        self.handle.seek(0); self.handle.truncate()
        self.handle.write(f'{os.getpid()}\n{hwnd}\n'); self.handle.flush()

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None


class Tip:
    """Delayed hover label that stays above the always-on-top widget."""
    def __init__(self, root, metrics):
        self.root = root
        self.metrics = metrics
        self.delay = 400
        self.after = None
        self._keep = None
        self.win = None
        self._widget = None
        self._text = ''

    def schedule(self, widget, text):
        self.cancel()
        self._destroy()
        self._widget, self._text = widget, text
        if not text:
            return
        try:
            self.after = self.root.after(self.delay, self._fire)
        except tk.TclError:
            pass

    def hide(self):
        self.cancel()
        self._destroy()

    def cancel(self):
        if self.after is not None:
            try:
                self.root.after_cancel(self.after)
            except tk.TclError:
                pass
            self.after = None

    def _fire(self):
        self.after = None
        widget, text = self._widget, self._text
        try:
            if not self.root.winfo_exists() or not widget.winfo_exists():
                return
        except tk.TclError:
            return
        self.show(widget, text)

    def show(self, widget, text):
        self._destroy()
        if not text:
            return
        try:
            wx, wy = widget.winfo_rootx(), widget.winfo_rooty()
            ww, wh = widget.winfo_width(), widget.winfo_height()
        except tk.TclError:
            return
        m = self.metrics() if callable(self.metrics) else self.metrics
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        try:
            win.attributes('-topmost', True)
        except tk.TclError:
            pass
        label = tk.Label(
            win, text=text, bg=CARD, fg=TEXT, font=m.font(FONT_FOOT),
            padx=m.p(8), pady=m.p(4), bd=0, highlightthickness=1, highlightbackground=HAIR,
        )
        label.pack()
        win.update_idletasks()
        gap = m.p(6)
        tw, th = win.winfo_reqwidth(), win.winfo_reqheight()
        x, y = wx, wy + wh + gap
        try:
            area = monitor_area(wx + ww // 2, wy + wh // 2)
        except (tk.TclError, OSError, ValueError):
            area = None
        if area:
            left, top, right, bottom = area
            if y + th > bottom:
                y = wy - th - gap
            x = min(max(x, left), max(left, right - tw))
            y = min(max(y, top), max(top, bottom - th))
        win.geometry(f'+{int(x)}+{int(y)}')
        try:
            win.deiconify()
            win.update_idletasks()
        except tk.TclError:
            try:
                win.destroy()
            except tk.TclError:
                pass
            return
        set_over_taskbar(win, True)
        self.win = win
        self._arm_keep()

    def _arm_keep(self):
        self._cancel_keep()
        def pulse():
            self._keep = None
            win = self.win
            if win is None:
                return
            try:
                if not win.winfo_exists() or not win.winfo_ismapped():
                    return
            except tk.TclError:
                return
            set_over_taskbar(win, True)
            try:
                self._keep = self.root.after(80, pulse)
            except tk.TclError:
                pass
        try:
            self._keep = self.root.after(80, pulse)
        except tk.TclError:
            pass

    def _cancel_keep(self):
        if self._keep is not None:
            try:
                self.root.after_cancel(self._keep)
            except tk.TclError:
                pass
            self._keep = None

    def _destroy(self):
        self._cancel_keep()
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None


class IconButton(tk.Canvas):
    def __init__(self, parent, image, command, hover_bg=HOVER, size=24, tip=None, hint=''):
        super().__init__(parent,width=size,height=size,bg=BG,bd=0,highlightthickness=0,
                         cursor='hand2',takefocus=True)
        self.image, self.command, self.hover, self.size = image, command, hover_bg, size
        self.tip, self.tip_text = tip, hint
        self.bind('<Button-1>', self._click)
        self.bind('<Return>',lambda e:command())
        self.bind('<space>',lambda e:command())
        self.bind('<Enter>', self._enter)
        self.bind('<Leave>', self._leave)
        self.paint(False)

    def _click(self, event=None):
        if self.tip:
            self.tip.hide()
        self.command()

    def _enter(self, event=None):
        self.paint(True)
        if self.tip and self.tip_text:
            self.tip.schedule(self, self.tip_text)

    def _leave(self, event=None):
        self.paint(False)
        if self.tip:
            self.tip.hide()

    def set_size(self, size):
        self.size = max(1, int(size))
        self.configure(width=self.size, height=self.size)
        self.paint(False)

    def paint(self, hover):
        self.delete('all')
        size = self.size
        if hover:
            round_rect(self,0,0,size,size,max(2, int(round(size * 0.25))),self.hover)
        self.create_image(size/2,size/2,image=self.image)


class UpdatePill(tk.Canvas):
    """Filled call-to-action shown only while an update is pending or installing."""
    animate = True
    _PULSE_MS = 50
    _PULSE_PERIOD = 1200

    def __init__(self, parent, command, metrics=None, tip=None):
        self.metrics = metrics or Metrics()
        super().__init__(parent, width=1, height=1, bg=BG, bd=0, highlightthickness=0)
        self.command = command
        self.tip, self.tip_text = tip, ''
        self.text, self.ready, self.hover, self.width_px = '', False, False, 0
        self._pulse_after = None
        self._pulse_phase = 0.0
        self.bind('<Button-1>', self._click)
        self.bind('<Enter>', lambda e: self._set_hover(True))
        self.bind('<Leave>', lambda e: self._set_hover(False))
        self.bind('<Destroy>', self._stop_pulse)

    def set_metrics(self, metrics):
        self.metrics = metrics
        self._redraw()

    def show(self, candidates, ready, max_width, hint=''):
        """Pick the longest label that fits; returns the pill width in pixels."""
        font = tkfont.Font(root=self, font=self.metrics.font(FONT_PILL))
        pad = self.metrics.p(9)
        text, width = candidates[-1], 0
        for option in candidates:
            width = font.measure(option) + pad * 2
            if width <= max_width:
                text = option
                break
        else:
            width = min(font.measure(text) + pad * 2, max_width)
        self.text, self.ready, self.width_px, self.tip_text = text, ready, max(1, int(width)), hint
        self.configure(width=self.width_px, height=self.metrics.pill_h, cursor='hand2' if ready else 'arrow')
        self._sync_pulse(0.0)
        self._redraw()
        if self.hover:
            self._tip_hover(True)
        return self.width_px

    def hide(self):
        self._stop_pulse()
        if self.tip and (self.hover or getattr(self.tip, '_widget', None) is self):
            self.tip.hide()
        self.text, self.ready, self.hover, self.tip_text = '', False, False, ''
        self.place_forget()

    def _click(self, event=None):
        if self.tip:
            self.tip.hide()
        if self.ready:
            self.command()

    def _set_hover(self, on):
        if on == self.hover:
            return
        self.hover = on
        if on:
            self._stop_pulse()
        else:
            self._sync_pulse(math.pi / 2)
        self._tip_hover(on)
        self._redraw()

    def _tip_hover(self, on):
        if not self.tip:
            return
        if on and self.tip_text:
            self.tip.schedule(self, self.tip_text)
        else:
            self.tip.hide()

    def _sync_pulse(self, phase=0.0):
        if self.ready and self.animate and not self.hover and self.text:
            if self._pulse_after is None:
                self._pulse_phase = phase
                self._schedule_pulse()
        else:
            self._stop_pulse()

    def _schedule_pulse(self):
        try:
            self._pulse_after = self.after(self._PULSE_MS, self._pulse_tick)
        except tk.TclError:
            self._pulse_after = None

    def _stop_pulse(self, event=None):
        aid = self._pulse_after
        self._pulse_after = None
        if aid is not None:
            try:
                self.after_cancel(aid)
            except tk.TclError:
                pass

    def _pulse_tick(self):
        self._pulse_after = None
        if not self.animate or not self.ready or self.hover or not self.text:
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._pulse_phase += 2 * math.pi * (self._PULSE_MS / self._PULSE_PERIOD)
        try:
            self._redraw()
        except tk.TclError:
            return
        self._schedule_pulse()

    def _redraw(self):
        self.delete('all')
        if not self.text:
            return
        w, h = self.width_px, self.metrics.pill_h
        if self.ready:
            peak = lighten(CODEX)
            if self.hover:
                amount = 1.0
            elif self.animate:
                amount = 0.5 * (1.0 + math.sin(self._pulse_phase))
            else:
                amount = 0.28
            fill = blend(CODEX, peak, amount)
            glow = blend(peak, lighten(CODEX, 0.42), amount)
            round_rect(self, 0, 0, w, h, h / 2, glow)
            inset = max(1.0, min(h / 6.0, float(self.metrics.p(1))))
            round_rect(self, inset, inset, w - inset, h - inset, max(0.0, (h - 2 * inset) / 2), fill)
            fg = BG
        else:
            round_rect(self, 0, 0, w, h, h / 2, CARD)
            fg = MUTED
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self.metrics.font(FONT_PILL))


class Chip(tk.Canvas):
    """One progress pill: proportional fill and an independent text overlay."""
    def __init__(self, parent, metrics=None):
        self.metrics = metrics or Metrics()
        super().__init__(parent,width=self.metrics.chip_w,height=self.metrics.chip_h,highlightthickness=0,bd=0,bg=BG)
        self.text, self.fill, self.fg, self.percent = '—', CHIP_STALE, CHIP_FG, 0.0
        self._photo = None
        self._redraw()

    def set_metrics(self, metrics):
        self.metrics = metrics
        self.configure(width=metrics.chip_w, height=metrics.chip_h)
        self._redraw()

    def cget(self,key):
        if key == 'text':
            return self.text
        return super().cget(key)

    def configure(self,text=None,fg=None,bg=None,percent=None,**kwargs):
        changed = False
        if text is not None and text != self.text:
            self.text = text
            changed = True
        if bg is not None and bg != self.fill:
            self.fill = bg
            changed = True
        if percent is not None:
            value = chip_fill_width(100, percent)
            if value != self.percent:
                self.percent = value
                changed = True
        self.fg = CHIP_FG
        if kwargs:
            super().configure(**kwargs)
        if changed:
            self._redraw()

    def _redraw(self):
        self.delete('all')
        width, height = self.metrics.chip_w, self.metrics.chip_h
        self.fill_width = chip_fill_width(width,self.percent)
        # Fill is clipped to the track so the leading cap cannot bulge outside.
        self._photo = progress_photo(width, height, height / 2, self.fill_width, CHIP_TRACK, self.fill, BG)
        self.create_image(0, 0, image=self._photo, anchor='nw', tags='track')
        self.create_text(width/2,height/2,text=self.text,fill=CHIP_FG,font=self.metrics.font(FONT_CHIP),tags='label')


class Card(tk.Frame):
    """Explicit pixel layout matching the supplied 334px-wide card references."""
    def __init__(self,parent,key,metrics=None):
        self.metrics = metrics or Metrics()
        m = self.metrics
        super().__init__(parent,width=m.card_w,height=m.p(120),bg=BG)
        self.key, self.height, self.last_signature = key,m.p(120),None
        self._bar_photos = []
        self.rows = tk.Canvas(self,width=m.card_w,height=self.height,bg=BG,bd=0,highlightthickness=0,cursor='hand2')
        self.rows.pack()
        self.rows.bind('<Button-1>',lambda e:webbrowser.open(URLS[key]))

    def set_metrics(self, metrics):
        if self.metrics.scale != metrics.scale:
            self.last_signature = None
        self.metrics = metrics

    def render(self,snap):
        visual = snapshot_to_dict(snap)
        visual.pop('fetched_at',None)
        signature = json.dumps(visual,sort_keys=True)
        if signature == self.last_signature:
            return
        self.last_signature = signature
        m = self.metrics
        positions = []
        label_y = m.p(108)
        for bar in snap.bars if snap.ok else []:
            reset = reset_stamp(bar.reset_text) if self.key == 'chatgpt' and bar.label == '5시간' else ''
            positions.append((bar,label_y,reset))
            label_y += m.p(42) + (m.p(20) if reset else 0)
        last_bottom = positions[-1][1]+m.p(18) if positions else m.p(92)
        amount = included_amount(snap) if self.key == 'cursor' and snap.ok else ''
        extra = bonus_line(snap) if self.key == 'cursor' and snap.ok else ''
        bill_y = last_bottom+m.p(27)
        bonus_y = bill_y+m.p(30) if amount else last_bottom+m.p(27)
        self.height = (bonus_y+m.p(19) if extra else bill_y+m.p(19) if amount else last_bottom+m.p(16)) if snap.ok else m.p(138)
        state = strip_color(self.key,snap)
        strip_h = max(1, self.height - m.p(24))
        photos = [round_photo(m.strip_w, strip_h, m.strip_w / 2, state, CARD)]
        track_w = m.card_w - m.p(36)
        for bar,_,_ in positions:
            fill_w = chip_fill_width(track_w, bar.remaining_percent)
            photos.append(progress_photo(track_w, m.bar_h, 4 * m.scale, fill_w, TRACK,
                                         bar_color(self.key, bar.remaining_percent, snap.stale), CARD))
        c = self.rows
        c.configure(width=m.card_w,height=self.height)
        self.configure(width=m.card_w,height=self.height)
        c.delete('all')
        strip, strip_pad = photos[0]
        round_rect(c,0,0,m.card_w,self.height,m.card_radius,HAIR)
        round_rect(c,1,1,m.card_w-1,self.height-1,max(1, m.card_radius-1),CARD)
        c.create_image(1 - strip_pad, m.p(12) - strip_pad, image=strip, anchor='nw', tags='strip')
        c.create_oval(m.p(21),m.p(21),m.p(29),m.p(29),fill=state,outline='')
        baseline_text(c,m.p(36),m.p(30),TITLES[self.key],m.font(FONT_SERVICE),TEXT)
        credit = reset_credit(snap) if self.key == 'chatgpt' and snap.ok else ''
        plan_right = m.card_w-m.p(16)
        if credit:
            font = tkfont.Font(root=c,font=m.font(FONT_BADGE))
            pill_w = font.measure(credit)+m.p(16)
            round_rect(c,plan_right-pill_w,m.p(16),plan_right,m.p(34),m.p(9),'#295346')
            round_rect(c,plan_right-pill_w+1,m.p(17),plan_right-1,m.p(33),m.p(8),'#20302F')
            c.create_text(plan_right-pill_w/2,m.p(25),text=credit,font=m.font(FONT_BADGE),fill=CODEX)
            plan_right -= pill_w+m.p(8)
        baseline_text(c,plan_right,m.p(30),'' if snap.plan == '-' else snap.plan,m.font(FONT_PLAN),MUTED,right=True)
        value = '—' if snap.hero_percent is None else f'{snap.hero_percent:.0f}%'
        hero_id = baseline_text(c,m.p(20),m.p(81),value,m.font(FONT_HERO),color_for(snap),tags='hero')
        hero_box = c.bbox(hero_id)
        subtitle = HERO_SUB[self.key] if snap.ok else '조회 실패'
        baseline_text(c,hero_box[2]+m.p(8),m.p(80),subtitle,m.font(FONT_SUB),MUTED)
        for index,(bar,y,reset) in enumerate(positions):
            baseline_text(c,m.p(20),y,bar.label,m.font(FONT_ROW),MUTED)
            value = '—' if bar.remaining_percent is None else f'{bar.remaining_percent:.0f}%'
            baseline_text(c,m.card_w-m.p(16),y,value,m.font(FONT_VALUE),TEXT,right=True)
            c.create_image(m.p(20), y+m.p(10), image=photos[index+1], anchor='nw')
            if reset:
                baseline_text(c,m.p(20),y+m.p(35),reset,m.font(FONT_META),DIM)
        self._bar_photos = [photos[0][0], *photos[1:]]
        if amount:
            baseline_text(c,m.p(20),bill_y,'기본 포함량',m.font(FONT_ROW),MUTED)
            baseline_text(c,m.card_w-m.p(16),bill_y,amount,m.font(FONT_VALUE),TEXT,right=True)
        if extra:
            baseline_text(c,m.p(20),bonus_y,extra,m.font(FONT_META),DIM)
        if not snap.ok:
            c.create_text(m.p(20),m.p(99),text=snap.error or '조회 실패',font=m.font(FONT_META),fill=DANGER,anchor='nw',width=m.card_w-m.p(36))


class UsageWidget:
    def __init__(self, preview=False):
        self.preview = preview
        self.settings = read_json(SETTINGS_PATH)
        self.scale = clamp_scale(self.settings.get('scale', DEFAULT_SCALE))
        self.metrics = Metrics(self.scale)
        self.root = tk.Tk()
        self.root.title('AI Usage' if not preview else 'AI Usage — Preview')
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.overrideredirect(not preview)
        self.topmost = tk.BooleanVar(value=bool(self.settings.get('topmost', True)))
        self.root.attributes('-topmost', self.topmost.get())
        self.compact = bool(self.settings.get('compact', False))
        self.closing = False
        self.runner = PollRunner()
        self.watcher = AuthWatcher()
        self.alerts = AlertGate(read_json(ALERT_PATH))
        self.notifications = tk.BooleanVar(value=bool(self.settings.get('notifications', True)))
        enabled = default_enabled(self.settings, self.preview)
        self.enabled = {k: tk.BooleanVar(value=enabled[k]) for k in FETCHERS}
        self.toast = None if preview else ToastSender()
        self.toast_error = ''
        self.locked = False if preview else session_locked() is not False
        self.last_environment = float('-inf')
        self.last_auth_scan = float('-inf')
        self.dragging = False
        self.last_area = None
        self.snapshots = {}
        self.failures = dict.fromkeys(FETCHERS, 0)
        self.due = dict.fromkeys(FETCHERS, 0.0)
        self.last_save = 0
        self.cache_signature = ''
        self.timer = None
        self.icons = {}
        self._layout = None
        self._region_h = None
        self._footer_state = None
        self.update_info = None
        self.update_queue = queue.Queue()
        self.last_update_check = float('-inf')
        self._update_busy = False
        self._overlay = 0
        self._menu_held = False
        self.tip = Tip(self.root, lambda: self.metrics)
        self._load_icons()
        self.build()
        if should_setup(self.settings, self.preview):
            self.pick_services()
        self.apply_mode()
        self.load_cache()
        self.root.update_idletasks()
        try:
            x, y = int(self.settings.get('x', 40)), int(self.settings.get('y', 80))
        except (ValueError, TypeError):
            x, y = 40, 80
        self.place(x, y)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind_all('<F5>', lambda e: self.refresh())
        self.root.bind_all('<Control-m>', lambda e: self.toggle())
        self.root.bind_all('<Control-equal>', self._scale_up)
        self.root.bind_all('<Control-plus>', self._scale_up)
        self.root.bind_all('<Control-KP_Add>', self._scale_up)
        self.root.bind_all('<Control-minus>', self._scale_down)
        self.root.bind_all('<Control-KP_Subtract>', self._scale_down)
        self.root.bind_all('<Control-0>', self._scale_reset)
        self.root.bind_all('<Control-KP_0>', self._scale_reset)
        self.root.bind_all('<Button-3>', self.popup)
        self.tick()

    def _load_icons(self):
        try:
            scale = float(self.root.winfo_fpixels('1i')) / 96.0
        except tk.TclError:
            scale = 1.0
        for name in ('refresh', 'minus', 'close', 'expand', 'plus'):
            image = load_icon(name, scale)
            if image is not None:
                self.icons[name] = image

    def icon_button(self, parent, name, command, hover_bg=HOVER):
        hint = ICON_HINTS.get(name, '')
        image = self.icons.get(name)
        if image is None:
            fallback = {'refresh': '↻', 'minus': '−', 'close': '×', 'expand': '＋', 'plus': '＋'}
            button = tk.Label(parent, text=fallback.get(name, '·'), bg=parent.cget('bg'), fg=MUTED,
                              font=('Segoe UI', max(8, int(round(11 * self.metrics.scale)))), cursor='hand2')
            button.tip_text = hint
            button.bind('<Button-1>', lambda e: (self.tip.hide(), command()))
            if hint:
                button.bind('<Enter>', lambda e, b=button, t=hint: self.tip.schedule(b, t), add='+')
                button.bind('<Leave>', lambda e: self.tip.hide(), add='+')
            return button
        return IconButton(parent, image, command, hover_bg=hover_bg, size=self.metrics.icon, tip=self.tip, hint=hint)

    def build(self):
        m = self.metrics
        self.shell = tk.Frame(self.root,bg=BG,width=m.window_w,highlightbackground=HAIR,highlightthickness=1)
        self.shell.pack()
        self.shell.pack_propagate(False)
        self.header = tk.Frame(self.shell,bg=BG,height=m.header_h)
        self.title = tk.Label(self.header,text='AI Usage',bg=BG,fg=TEXT,font=m.font(FONT_TITLE),bd=0,padx=0,pady=0)
        self.update_pill = UpdatePill(self.header, self.install_update, m, tip=self.tip)
        self.header_buttons = []
        for name,callback in (('refresh',self.refresh),('minus',self.toggle),('close',self.close)):
            self.header_buttons.append(self.icon_button(self.header,name,callback,CLOSE_HOVER if name == 'close' else HOVER))
        self.body = tk.Frame(self.shell,bg=BG,padx=m.p(12))
        self.cards = {k:Card(self.body,k,m) for k in FETCHERS}
        self.footer = tk.Frame(self.shell,bg=BG,height=m.footer_h)
        tk.Frame(self.footer,bg=HAIR,height=1).place(x=0,y=0,relwidth=1,height=1)
        self.footer_dot = tk.Canvas(self.footer,width=m.p(6),height=m.p(6),bg=BG,highlightthickness=0,bd=0)
        self.footer_text = tk.Label(self.footer,bg=BG,fg=MUTED,font=m.font(FONT_FOOT),bd=0,padx=0,pady=0)
        self.footer_sep = tk.Label(self.footer,text='·',bg=BG,fg=DIM,font=m.font(FONT_FOOT),bd=0,padx=0,pady=0)
        self.footer_hint = tk.Label(self.footer,text='F5 새로고침',bg=BG,fg=DIM,font=m.font(FONT_FOOT),bd=0,padx=0,pady=0)
        self.status = self.footer_text
        self.mini = tk.Frame(self.shell,bg=BG,height=max(1, m.compact_h-2))
        self.mini_title = tk.Label(self.mini,text='AI Usage',bg=BG,fg=TEXT,font=m.font(FONT_TITLE),bd=0,padx=0,pady=0)
        self.mini_pill = UpdatePill(self.mini, self.install_update, m, tip=self.tip)
        self.mini_values = {}
        for key in FETCHERS:
            chip = Chip(self.mini, m)
            self.mini_values[key] = chip
            self.bind_drag(chip)
        self.mini_buttons = []
        for name,callback in (('refresh',self.refresh),('expand',self.toggle),('close',self.close)):
            self.mini_buttons.append(self.icon_button(self.mini,name,callback,CLOSE_HOVER if name == 'close' else HOVER))
        self.apply_metrics()
        for widget in (self.title,self.header,self.mini_title,self.mini):
            self.bind_drag(widget)
        self.menu = tk.Menu(self.root, tearoff=False, bg=CARD, fg=TEXT, activebackground=HAIR, activeforeground=TEXT, disabledforeground=DIM, selectcolor='#FFFFFF')
        self.menu.add_command(label='새로고침    F5', command=self.refresh)
        self.menu.add_command(label='한 줄 / 상세    Ctrl+M', command=self.toggle)
        self.menu.add_separator()
        self.menu.add_command(label='더 크게    Ctrl++', command=lambda: self.nudge_scale(1))
        self.menu.add_command(label='더 작게    Ctrl+-', command=lambda: self.nudge_scale(-1))
        self.menu.add_command(label='기본 크기    Ctrl+0', command=lambda: self.set_scale(DEFAULT_SCALE))
        self.menu.add_separator()
        self.menu.add_checkbutton(label='항상 위', variable=self.topmost, command=self.set_topmost)
        self.startup = tk.BooleanVar(value=startup_path().exists())
        self.menu.add_checkbutton(label='Windows 시작 시 실행', variable=self.startup, command=self.toggle_startup)
        self.menu.add_checkbutton(label='한도 임박·소진 알림', variable=self.notifications, command=self.persist)
        self.menu.add_command(label='알림 테스트', command=self.test_toast)
        self.menu.add_separator()
        self.menu.add_command(label='업데이트', command=self.install_update, state='disabled')
        self._update_menu = self.menu.index('end')
        self.menu.add_command(label='업데이트 확인', command=self.check_update_now)
        self.menu.add_separator()
        self.menu.add_command(label='표시할 서비스·로그인...', command=self.pick_services)
        for key in FETCHERS:
            self.menu.add_checkbutton(label=TITLES[key] + ' 조회', variable=self.enabled[key], command=lambda k=key: self.toggle_provider(k))
        self.menu.add_separator()
        for key in FETCHERS:
            self.menu.add_command(label=TITLES[key] + ' 사용량 페이지', command=lambda k=key: webbrowser.open(URLS[k]))
        self.menu.add_command(label='표시 기준 / 도움말', command=self.help)
        self.menu.add_separator()
        self.menu.add_command(label=f'버전 {APP_VERSION}', state='disabled')
        self.menu.add_command(label='종료', command=self.close)
        self.menu.bind('<Map>', lambda e: self._lift_menu())

    def apply_topmost(self):
        if self.preview:
            return
        want = bool(self.topmost.get())
        owner = self._widget_hwnd()
        if self._overlay or self._menu_held:
            if want:
                keep_topmost_style(owner, True)
            if self._overlay:
                lift_owned_popups(owner)
            else:
                self._raise_open_menus()
            return
        try:
            self.root.attributes('-topmost', want)
        except tk.TclError:
            return
        set_over_taskbar(self.root, want)
        if want:
            lift_tip_window(self.tip.win)

    def _widget_hwnd(self):
        try:
            return int(self.root.wm_frame(), 16)
        except (AttributeError, TypeError, ValueError, tk.TclError):
            return 0

    def _keep_widget_topmost(self):
        if self.preview or not self.topmost.get():
            return
        try:
            self.root.attributes('-topmost', True)
        except tk.TclError:
            return
        set_over_taskbar(self.root, True)
        keep_topmost_style(self._widget_hwnd(), True)

    def push_overlay(self):
        self._overlay += 1
        try:
            self.tip.hide()
        except (AttributeError, tk.TclError):
            pass
        if self._overlay == 1:
            self._keep_widget_topmost()
            self._arm_overlay_raise()

    def pop_overlay(self):
        self._overlay = max(0, self._overlay - 1)
        if self._overlay == 0 and not self.closing:
            self.apply_topmost()

    def notify(self, fn, *args, **kwargs):
        self.push_overlay()
        try:
            return fn(*args, **kwargs)
        finally:
            self.pop_overlay()

    def _arm_overlay_raise(self):
        hwnd = self._widget_hwnd()
        want = bool(self.topmost.get())

        def lift():
            while self._overlay and not self.closing:
                if hwnd and want:
                    keep_topmost_style(hwnd, True)
                lift_owned_popups(hwnd)
                time.sleep(0.05)

        threading.Thread(target=lift, daemon=True, name='overlay-z').start()

    def _raise_open_menus(self):
        extra = 0
        try:
            if self.menu.winfo_ismapped():
                extra = int(self.menu.winfo_id())
        except (TypeError, ValueError, tk.TclError):
            extra = 0
        lift_menu_windows(extra)

    def _lift_menu(self):
        if not self._menu_held or self.closing:
            return
        self._raise_open_menus()

    def _arm_menu_raise(self):
        hwnd = self._widget_hwnd()
        if not self._overlay and self.topmost.get():
            try:
                self.root.attributes('-topmost', True)
            except tk.TclError:
                pass
            set_over_taskbar(self.root, True)
            keep_topmost_style(hwnd, True)
        self._raise_open_menus()
        def pulse():
            if not self._menu_held or self.closing:
                return
            self._raise_open_menus()
            try:
                self.root.after(50, pulse)
            except tk.TclError:
                pass
        try:
            self.root.after(50, pulse)
        except tk.TclError:
            pass
        want = bool(self.topmost.get())
        def lift():
            while self._menu_held and not self.closing:
                if hwnd and want:
                    keep_topmost_style(hwnd, True)
                lift_menu_windows()
                time.sleep(0.05)
        threading.Thread(target=lift, daemon=True, name='menu-z').start()

    def _release_menu(self):
        try:
            if self.menu.winfo_ismapped():
                self.root.after(50, self._release_menu)
                return
        except tk.TclError:
            pass
        if self._menu_held:
            self._menu_held = False
            if not self._overlay and not self.closing:
                self.apply_topmost()

    def help_text(self):
        return (
            f'현재 버전 {APP_VERSION}\n\n'
            '이 위젯은 OpenAI(ChatGPT·Codex)·Cursor와 제휴되지 않은 비공식 도구입니다.\n'
            '사용량 조회는 언제든 실패하거나 바뀔 수 있습니다.\n\n'
            'Codex: 5시간·주간 중 더 적게 남은 한도입니다.\n'
            'Cursor: 전체 잔여와 자사 모델·API 잔여를 구분합니다.\n'
            '기본 포함량 소진과 전체 한도 소진은 다를 수 있습니다.\n\n'
            '한 줄 칩 색이 임박·소진·이전 데이터를 나타냅니다.\n'
            '우클릭 → 표시할 서비스·로그인에서 Codex / Cursor를 고릅니다.\n'
            '계정 로그인은 각 서비스에서 하세요. 위젯은 읽기만 합니다.\n'
            'Codex는 ChatGPT 데스크톱 앱이 아니라 Codex CLI 로그인이 필요합니다.\n\n'
            'F5 새로고침 · Ctrl+M 한 줄 모드\n'
            'Ctrl++ / Ctrl+- 크기 조절 · Ctrl+0 기본 크기\n'
            '제목 드래그로 이동 · 우클릭으로 설정\n\n'
            '10% 이하·소진 시 한 번 알림 (12% 초과 회복 시 재설정)\n'
            '로그인 파일 변경 자동 감지 · 조회 제한 15초\n'
            '잠금 중 조회 중지 · 해제 시 즉시 조회\n'
            '새 버전이 있으면 제목 옆에 초록 ↑ 업데이트 버튼이 나타납니다.'
        )

    def help(self):
        self.notify(messagebox.showinfo, 'AI Usage', self.help_text(), parent=self.root)

    def pick_services(self):
        self.push_overlay()
        dialog = tk.Toplevel(self.root)
        dialog.title('표시할 서비스')
        dialog.configure(bg=BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.attributes('-topmost', True)
        chosen = {key: tk.BooleanVar(value=self.enabled[key].get()) for key in FETCHERS}
        tk.Label(dialog, text='이 PC에서 볼 서비스를 고르세요.', bg=BG, fg=TEXT, font=FONT_TITLE).pack(anchor='w', padx=16, pady=(14, 6))
        tk.Label(
            dialog,
            text='ChatGPT 데스크톱 앱만 있으면 Codex는 연동되지 않습니다. Codex CLI와 Cursor 앱 로그인이 필요합니다.',
            bg=BG, fg=MUTED, font=FONT_FOOT, wraplength=340, justify='left',
        ).pack(anchor='w', padx=16)
        notes = {}
        actions = {}
        for key in FETCHERS:
            block = tk.Frame(dialog, bg=BG)
            block.pack(fill='x', padx=16, pady=6)
            row = tk.Frame(block, bg=BG)
            row.pack(fill='x')
            tk.Checkbutton(
                row, text=TITLES[key], variable=chosen[key], bg=BG, fg=TEXT, selectcolor=CARD,
                activebackground=BG, activeforeground=TEXT, font=FONT_SERVICE, highlightthickness=0, bd=0,
            ).pack(side='left')
            note = tk.Label(row, text=login_status(key), bg=BG, fg=DIM, font=FONT_META)
            note.pack(side='left', padx=8)
            notes[key] = note
            label, action = prepare_action(key)
            button = tk.Button(
                block, text=label, bg=CARD, fg=TEXT, bd=0, padx=10, pady=3, cursor='hand2',
                command=lambda a=action: start_tool_setup(a),
            )
            button.pack(anchor='w', pady=(4, 0))
            actions[key] = button
        tk.Label(
            dialog,
            text='Claude · Gemini · ChatGPT 웹 구독은 아직 로컬 잔여량 조회를 지원하지 않습니다.',
            bg=BG, fg=DIM, font=FONT_META, wraplength=340, justify='left',
        ).pack(anchor='w', padx=16, pady=(4, 4))
        buttons = tk.Frame(dialog, bg=BG)
        buttons.pack(fill='x', padx=16, pady=(8, 14))
        first_run = should_setup(self.settings, self.preview)
        alive = {'on': True}

        def refresh_status():
            if not alive['on']:
                return
            for key in FETCHERS:
                notes[key].configure(text=login_status(key))
                label, action = prepare_action(key)
                actions[key].configure(text=label, command=lambda a=action: start_tool_setup(a))
        try:
            dialog.after(2000, refresh_status)
        except tk.TclError:
            alive['on'] = False

        def finish_prepare():
            need_codex = chosen['chatgpt'].get() and not login_present('chatgpt')
            need_cursor = chosen['cursor'].get() and not login_present('cursor')
            if not (need_codex or need_cursor):
                return
            lines = ['선택한 서비스의 로그인이 없습니다. 지금 설치하고 로그인할까요?']
            if need_codex:
                lines.append('Codex: ChatGPT 데스크톱이 아니라 Codex CLI가 필요합니다.')
            if need_cursor:
                lines.append('Cursor: Cursor 앱에서 로그인해야 합니다.')
            if self.notify(messagebox.askyesno, '로그인 준비', '\n'.join(lines), parent=self.root):
                start_tool_setup('prepare', codex=need_codex, cursor=need_cursor)

        def commit():
            if not any(item.get() for item in chosen.values()):
                messagebox.showinfo('표시할 서비스', '하나 이상 선택하세요.', parent=dialog)
                return
            for key in FETCHERS:
                was = self.enabled[key].get()
                now = chosen[key].get()
                self.enabled[key].set(now)
                if now and not was:
                    self.due[key] = 0
                    self.failures[key] = 0
                if not now:
                    self.runner.cancel(key)
                    self.due[key] = 0
            self.persist()
            self.apply_mode()
            alive['on'] = False
            dialog.destroy()
            finish_prepare()

        def cancel():
            alive['on'] = False
            dialog.destroy()

        tk.Button(buttons, text='확인', command=commit, bg=CARD, fg=TEXT, bd=0, padx=12, pady=4, cursor='hand2').pack(side='right')
        if not first_run:
            tk.Button(buttons, text='취소', command=cancel, bg=BG, fg=MUTED, bd=0, padx=12, pady=4, cursor='hand2').pack(side='right', padx=(0, 8))
        dialog.protocol('WM_DELETE_WINDOW', commit if first_run else cancel)
        dialog.bind('<Destroy>', lambda e: alive.update(on=False) if e.widget is dialog else None)
        dialog.update_idletasks()
        dialog.geometry(f'+{self.root.winfo_rootx() + 24}+{self.root.winfo_rooty() + 48}')
        refresh_status()
        try:
            dialog.grab_set()
            dialog.wait_window()
        finally:
            self.pop_overlay()

    def apply_metrics(self):
        m = self.metrics
        self.title.configure(font=m.font(FONT_TITLE))
        self.update_pill.set_metrics(m)
        self.mini_title.configure(font=m.font(FONT_TITLE))
        self.mini_pill.set_metrics(m)
        self.footer_text.configure(font=m.font(FONT_FOOT))
        self.footer_sep.configure(font=m.font(FONT_FOOT))
        self.footer_hint.configure(font=m.font(FONT_FOOT))
        self.header.configure(height=m.header_h)
        self.footer.configure(height=m.footer_h)
        self.mini.configure(height=max(1, m.compact_h - 2))
        self.body.configure(padx=m.p(12))
        self.title.place(x=m.p(12), y=0, height=m.header_h)
        fallback_font = ('Segoe UI', max(8, int(round(11 * m.scale))))
        for index, btn in enumerate(self.header_buttons):
            if isinstance(btn, IconButton):
                btn.set_size(m.icon)
            else:
                btn.configure(font=fallback_font)
            btn.place(x=m.p(270) + m.p(26) * index, y=m.p(8), width=m.icon, height=m.icon)
        self.mini_title.place(x=m.p(12), y=0, height=max(1, m.compact_h - 2))
        for index, btn in enumerate(self.mini_buttons):
            if isinstance(btn, IconButton):
                btn.set_size(m.icon)
            else:
                btn.configure(font=fallback_font)
            btn.place(x=m.p(270) + m.p(26) * index, y=m.p(9), width=m.icon, height=m.icon)
        self.footer_dot.configure(width=m.p(6), height=m.p(6))
        self.footer_dot.place(x=m.p(14), y=m.p(12))
        self.footer_text.place(x=m.p(30), y=m.p(1), height=m.p(27))
        self.footer_hint.place(x=m.window_w - m.p(16), y=m.p(1), height=m.p(27), anchor='ne')
        for chip in self.mini_values.values():
            chip.set_metrics(m)
        for card in self.cards.values():
            card.set_metrics(m)
            if card.last_signature is None:
                card.height = m.p(120)
                card.configure(width=m.card_w, height=card.height)
                card.rows.configure(width=m.card_w, height=card.height)

    def set_scale(self, scale):
        scale = clamp_scale(scale)
        if scale == self.scale:
            return
        self.scale = scale
        self.metrics = Metrics(scale)
        self.apply_metrics()
        self._layout = None
        self._region_h = None
        self._footer_state = None
        for key in FETCHERS:
            if key in self.snapshots:
                self.cards[key].last_signature = None
                self.render(key)
        self.apply_mode()
        self.set_footer('', MUTED, CODEX)
        self.place(self.root.winfo_x(), self.root.winfo_y())
        self.persist()

    def nudge_scale(self, steps):
        self.set_scale(step_scale(self.scale, steps))

    def _scale_up(self, event=None):
        self.nudge_scale(1)
        return 'break'

    def _scale_down(self, event=None):
        self.nudge_scale(-1)
        return 'break'

    def _scale_reset(self, event=None):
        self.set_scale(DEFAULT_SCALE)
        return 'break'

    def apply_mode(self):
        m = self.metrics
        visible = [k for k in FETCHERS if self.enabled[k].get()]
        for key,card in self.cards.items():
            if key not in visible:
                self.mini_values[key].configure(text=TITLES[key]+' 꺼짐',fg=CHIP_FG,bg=CHIP_STALE,percent=0)
        body_h = m.p(6)+sum(self.cards[k].height for k in visible)+m.card_gap*max(0,len(visible)-1)+m.p(10)
        height = m.compact_h if self.compact else 2+m.header_h+body_h+m.footer_h
        layout = (self.compact, tuple(visible), height, tuple(self.cards[k].height for k in visible), m.scale)
        if layout == self._layout:
            return
        self._layout = layout
        for item in (self.header,self.body,self.footer,self.mini):
            item.place_forget()
        for key,card in self.cards.items():
            card.pack_forget()
            if key in visible:
                index = visible.index(key)
                card.pack(fill='x',pady=(m.p(6) if index == 0 else m.card_gap,0))
        shown = 0
        for key in FETCHERS:
            chip = self.mini_values[key]
            if key in visible:
                chip.place(x=m.p(76)+m.p(88)*shown,y=m.p(9),width=m.chip_w,height=m.chip_h)
                shown += 1
            else:
                chip.place_forget()
        self.shell.configure(width=m.window_w,height=height)
        if self.compact:
            self.mini.place(x=1,y=1,width=m.window_w-2,height=max(1, m.compact_h-2),bordermode='outside')
        else:
            self.header.place(x=1,y=1,width=m.window_w-2,height=m.header_h,bordermode='outside')
            self.body.place(x=1,y=1+m.header_h,width=m.window_w-2,height=body_h,bordermode='outside')
            self.footer.place(x=1,y=height-m.footer_h-1,width=m.window_w-2,height=m.footer_h,bordermode='outside')
        self.root.geometry(f'{m.window_w}x{height}')
        self.root.update_idletasks()
        if not self.preview and height != self._region_h:
            # Region coordinates include the whole frameless window.
            try:
                gdi = ctypes.windll.gdi32
                gdi.CreateRoundRectRgn.restype = ctypes.c_void_p
                hwnd = ctypes.c_void_p(int(self.root.wm_frame(),16))
                region = gdi.CreateRoundRectRgn(0,0,m.window_w+1,height+1,m.p(24),m.p(24))
                if ctypes.windll.user32.SetWindowRgn(hwnd,ctypes.c_void_p(region),True):
                    self._region_h = height
                else:
                    gdi.DeleteObject(ctypes.c_void_p(region))
            except (AttributeError,OSError,ValueError):
                pass
        self.set_update_chrome()

    def toggle(self):
        self.compact = not self.compact
        self.apply_mode()
        self.place(self.root.winfo_x(), self.root.winfo_y())
        self.persist()

    def set_topmost(self):
        self.apply_topmost()
        self.persist()

    def toggle_startup(self):
        if self.preview:
            self.startup.set(not self.startup.get())
            return
        try:
            set_startup(self.startup.get())
        except (OSError, RuntimeError) as exc:
            self.startup.set(not self.startup.get())
            self.notify(messagebox.showerror, '시작 설정', str(exc), parent=self.root)

    def popup(self, event):
        self.tip.hide()
        if not self._menu_held:
            self._menu_held = True
            self._arm_menu_raise()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.menu.grab_release()
            except tk.TclError:
                pass
            try:
                self.root.after(0, self._release_menu)
            except tk.TclError:
                self._release_menu()

    def bind_drag(self, widget):
        widget.bind('<ButtonPress-1>', self.start_drag)
        widget.bind('<B1-Motion>', self.drag)
        widget.bind('<ButtonRelease-1>', self.end_drag)
        widget.bind('<Double-Button-1>', lambda e: self.toggle())

    def start_drag(self, e):
        self.tip.hide()
        self.dragging = True
        self.drag_offset = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag(self, e):
        self.root.geometry(geometry_at(e.x_root - self.drag_offset[0], e.y_root - self.drag_offset[1]))

    def end_drag(self, e):
        self.dragging = False
        self.place(self.root.winfo_x(), self.root.winfo_y())
        self.persist()

    def place(self, x, y):
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        cx, cy = x + w // 2, y + h // 2
        x, y = clamp_position(x, y, w, h, work_area(cx, cy), monitor_area(cx, cy))
        self.root.geometry(geometry_at(x, y))
        self.apply_topmost()

    def persist(self):
        if self.preview:
            return
        try:
            save_json(SETTINGS_PATH, {
                'x': self.root.winfo_x(),
                'y': self.root.winfo_y(),
                'compact': self.compact,
                'topmost': self.topmost.get(),
                'scale': self.scale,
                'version': 3,
                'setup_done': True,
                'notifications': self.notifications.get(),
                'enabled': {k: v.get() for k, v in self.enabled.items()},
            })
        except OSError:
            pass

    def load_cache(self):
        cache = read_json(CACHE_PATH)
        for key in FETCHERS:
            data = cache.get(key)
            try:
                if not isinstance(data, dict) or not data.get('ok'):
                    continue
                snap = snapshot_from_dict(data)
                if snap.key != key or snap.hero_percent is None or not 0 <= snap.hero_percent <= 100:
                    continue
                if cache.get('version') != 2:
                    continue
                self.snapshots[key] = snap
                self.render(key)
            except (ValueError, TypeError, AttributeError, OverflowError):
                continue

    def refresh(self):
        for key in FETCHERS:
            self.due[key] = 0
        self.set_footer('새로고침 요청됨', MUTED, CODEX)

    def start_job(self, key):
        try:
            self.runner.start(key, time.monotonic())
        except OSError:
            self.accept(key, error_snapshot(key, TITLES[key], '조회 프로세스를 시작하지 못했습니다.', URLS[key]))

    def toggle_provider(self, key):
        self.runner.cancel(key)
        self.due[key] = 0
        self.failures[key] = 0
        if self.enabled[key].get() and key in self.snapshots:
            self.snapshots[key] = replace(self.snapshots[key], stale=True)
            self.render(key)
        self.apply_mode()
        self.persist()

    def test_toast(self):
        if self.toast and not self.locked:
            self.toast.send('test', 'AI Usage 알림 테스트', '한도 임박·소진 알림이 이곳에 표시됩니다.')

    def environment(self, now):
        if self.preview:
            return
        if now - self.last_environment >= 2:
            self.last_environment = now
            state = session_locked()
            if state is not None and state != self.locked:
                self.locked = state
                for key in FETCHERS:
                    self.runner.cancel(key)
                    self.due[key] = 0
                if not state:
                    for key, snap in list(self.snapshots.items()):
                        self.snapshots[key] = replace(snap, stale=True)
                        self.render(key)
            if not self.locked and not self.dragging:
                x, y = self.root.winfo_x(), self.root.winfo_y()
                w, h = self.root.winfo_width(), self.root.winfo_height()
                area = monitor_area(x + w // 2, y + h // 2)
                left, top, right, bottom = area
                if area != self.last_area or x < left or y < top or x + w > right or y + h > bottom:
                    self.place(x, y)
                    self.last_area = area
                    self.persist()
                elif self.topmost.get():
                    self.apply_topmost()
        if not self.locked and now - self.last_auth_scan >= 3:
            self.last_auth_scan = now
            for key in self.watcher.changed(now):
                if self.enabled[key].get():
                    if key == 'cursor':
                        self.runner.plan_cache = None
                    self.runner.cancel(key)
                    self.due[key] = 0

    def accept(self, key, snap):
        if snap.ok:
            self.failures[key] = 0
        else:
            self.failures[key] += 1
        previous = self.snapshots.get(key)
        if not snap.ok and previous and previous.ok:
            snap = replace(previous, stale=True, error=snap.error)
        self.snapshots[key] = snap
        self.due[key] = time.monotonic() + next_interval(snap, self.failures[key])
        self.render(key)
        if not self.preview:
            self.save_cache()
            if self.notifications.get() and not self.locked and self.enabled[key].get():
                before = dict(self.alerts.state)
                severity = self.alerts.observe(key, snap)
                if self.alerts.state != before:
                    try:
                        save_json(ALERT_PATH, self.alerts.state)
                    except OSError:
                        self.toast_error = '알림 상태 저장 실패'
                if severity:
                    title = TITLES[key] + (' 한도 소진·제한' if severity == 2 else ' 한도 임박')
                    remaining, label = limiting_quota(snap)
                    self.toast.send(key, title, f'{label} · 잔여 {remaining:.0f}%')

    def save_cache(self):
        payload = {k: snapshot_to_dict(v) for k, v in self.snapshots.items() if v.ok and not v.stale}
        for k, v in self.snapshots.items():
            if v.ok and k not in payload:
                payload[k] = snapshot_to_dict(v)
        signature = json.dumps({k: {a: b for a, b in v.items() if a != 'fetched_at'} for k, v in payload.items()}, sort_keys=True)
        if signature == self.cache_signature and time.monotonic() - self.last_save < 300:
            return
        try:
            save_json(CACHE_PATH, dict(payload, version=2))
            self.cache_signature = signature
            self.last_save = time.monotonic()
        except (OSError, ValueError):
            pass

    def render(self, key):
        if not self.enabled[key].get():
            self.mini_values[key].configure(text=TITLES[key] + ' 꺼짐', fg=MUTED, bg=CHIP_STALE, percent=0)
            self.apply_mode()
            return
        snap = self.snapshots[key]
        self.cards[key].render(snap)
        self.apply_mode()
        value = '—' if snap.hero_percent is None else f'{snap.hero_percent:.0f}%'
        fill, fg = chip_style(key, snap)
        pct = 0 if snap.hero_percent is None else snap.hero_percent
        self.mini_values[key].configure(text=f'{TITLES[key]} {value}', fg=fg, bg=fill, percent=pct)

    def set_footer(self, text, fg, dot):
        snaps = [self.snapshots[k] for k in FETCHERS if self.enabled[k].get() and k in self.snapshots]
        if any(not s.stale and visual_state(s) == 'danger' for s in snaps):
            text,dot = '사용량 소진',DANGER
        elif any(s.stale for s in snaps):
            text,dot = '일부 데이터 이전 기준',STALE_STRIP
        else:
            text,dot = '자동 감지',CODEX
        state = (text, dot)
        if state == self._footer_state:
            return
        self._footer_state = state
        self.footer_text.configure(text=text,fg=MUTED)
        m = self.metrics
        width = tkfont.Font(root=self.root,font=m.font(FONT_FOOT)).measure(text)
        self.footer_sep.place(x=m.p(30)+width+m.p(10),y=m.p(1),height=m.p(27))
        self.footer_dot.delete('all')
        d = m.p(6)
        self.footer_dot.create_oval(0,0,d,d,fill=dot,outline='')

    def tick(self):
        if self.closing:
            return
        self.drain_update_queue()
        now = time.monotonic()
        self.environment(now)
        for key, snap, error in self.runner.poll(now):
            if not self.locked and self.enabled[key].get():
                self.accept(key, snap or error_snapshot(key, TITLES[key], error, URLS[key]))
        if not self.preview and not self.locked:
            for key in FETCHERS:
                if self.enabled[key].get() and key not in self.runner.slots and now >= self.due[key]:
                    self.start_job(key)
            self.check_update()
        if self.toast:
            try:
                while True:
                    self.toast_error = self.toast.results.get_nowait()
            except queue.Empty:
                pass
        active = [k for k, s in self.runner.slots.items() if not s.expired]
        failed = [TITLES[k] for k, v in self.failures.items() if v and self.enabled[k].get()]
        enabled_snaps = [self.snapshots[k] for k in FETCHERS if self.enabled[k].get() and k in self.snapshots]
        if self.locked:
            self.set_footer('잠금 중 · 조회 일시 중지', MUTED, STALE_STRIP)
        elif active:
            self.set_footer('갱신 중 · ' + ', '.join(TITLES[k] for k in active), MUTED, CODEX)
        elif self.toast_error:
            self.set_footer(self.toast_error, WARN, WARN)
        elif failed:
            self.set_footer(', '.join(failed) + ' 연결 확인 중 · 자동 재시도', WARN, WARN)
        elif not any(v.get() for v in self.enabled.values()):
            self.set_footer('조회 꺼짐 · 우클릭으로 켜기', MUTED, STALE_STRIP)
        elif self.preview:
            self.set_footer('미리보기 · 실제 계정 조회 없음', MUTED, CODEX)
        elif any(visual_state(s) == 'danger' for s in enabled_snaps):
            self.set_footer('사용량 소진', MUTED, DANGER)
        elif any(s.stale for s in enabled_snaps):
            self.set_footer('일부 데이터 이전 기준', MUTED, STALE_STRIP)
        else:
            self.set_footer('자동 감지', MUTED, CODEX)
        self.timer = self.root.after(200 if active else 1000, self.tick)

    def set_update_chrome(self):
        m = self.metrics
        ready = bool(self.update_info) and not self._update_busy
        pending = ready or self._update_busy
        version = self.update_info['version'] if self.update_info else ''
        if self._update_busy:
            header_labels = ['설치 중...', '설치 중']
            mini_labels = ['설치 중', '...']
            hint = '설치 중...'
        else:
            header_labels = [f'↑ 업데이트 {version}', f'↑ 새 버전 {version}', '↑ 업데이트', '↑ 새 버전', '↑']
            mini_labels = ['↑ 새 버전', '새 버전', f'↑ {version}', '↑']
            hint = f'업데이트 {version}' if ready else ''
        # Detail header: a filled pill right after the title, hidden when nothing is pending.
        if pending and not self.compact:
            x = m.p(84)
            width = self.update_pill.show(header_labels, ready, m.p(270) - m.p(10) - x, hint)
            self.update_pill.place(x=x, y=(m.header_h - m.pill_h) // 2, width=width, height=m.pill_h)
        else:
            self.update_pill.hide()
        # Compact row: the pill takes the title slot so it never collides with chips or buttons.
        mini_h = max(1, m.compact_h - 2)
        if pending and self.compact:
            x = m.p(12)
            width = self.mini_pill.show(mini_labels, ready, m.p(76) - m.p(6) - x, hint)
            self.mini_title.place_forget()
            self.mini_pill.place(x=x, y=(mini_h - m.pill_h) // 2, width=width, height=m.pill_h)
        else:
            self.mini_pill.hide()
            self.mini_title.place(x=m.p(12), y=0, height=mini_h)
        label = f"업데이트 {version}" if ready else '업데이트'
        try:
            self.menu.entryconfig(self._update_menu, label=label, state=('normal' if ready else 'disabled'))
        except tk.TclError:
            pass

    def check_update(self, force=False, notify=False):
        if self.preview:
            return
        now = time.monotonic()
        if not force and now - self.last_update_check < CHECK_EVERY:
            return
        self.last_update_check = now
        feed = load_feed_url()
        if not feed:
            if notify:
                self.update_queue.put(('checked', None, True, False))
            return

        def work():
            info = fetch_latest(feed) if feed else None
            self.update_queue.put(('checked', info, notify, bool(feed)))

        threading.Thread(target=work, daemon=True, name='update-check').start()

    def check_update_now(self):
        self.check_update(force=True, notify=True)

    def drain_update_queue(self):
        try:
            while True:
                item = self.update_queue.get_nowait()
                kind = item[0]
                if kind == 'checked':
                    _, info, notify, had_feed = item
                    self.update_info = info
                    self.set_update_chrome()
                    if notify:
                        if not had_feed:
                            self.notify(messagebox.showinfo, '업데이트', '배포 주소가 없습니다.\nfeed_url.txt에 latest.json 공개 주소를 넣으면 친구가 업데이트를 받을 수 있습니다.', parent=self.root)
                        elif info:
                            self.notify(messagebox.showinfo, '업데이트', f"{info['version']} 을 받을 수 있습니다.", parent=self.root)
                        else:
                            self.notify(messagebox.showinfo, '업데이트', f'이미 최신입니다. ({APP_VERSION})', parent=self.root)
                elif kind == 'downloaded':
                    self._finish_update(item[1])
                elif kind == 'failed':
                    self._update_busy = False
                    self.set_update_chrome()
                    self.notify(messagebox.showinfo, '업데이트', item[1], parent=self.root)
        except queue.Empty:
            pass

    def install_update(self):
        if self.preview or not self.update_info or self._update_busy:
            return
        if not self.notify(messagebox.askyesno, '업데이트', update_confirm_text(self.update_info), parent=self.root):
            return
        self._update_busy = True
        self.set_update_chrome()
        url = self.update_info['zip']

        def work():
            try:
                source = download_and_stage(url)
                self.update_queue.put(('downloaded', source))
            except Exception:
                self.update_queue.put(('failed', '업데이트를 받지 못했습니다. 인터넷 연결을 확인하세요.'))

        threading.Thread(target=work, daemon=True, name='update-download').start()

    def _finish_update(self, source):
        start_apply(source)
        self.close()

    def close(self):
        if self.closing:
            return
        self.persist()
        self.closing = True
        self.tip.hide()
        try:
            self.update_pill.hide()
            self.mini_pill.hide()
        except tk.TclError:
            pass
        self.runner.close()
        if self.timer:
            self.root.after_cancel(self.timer)
        self.root.destroy()


def main():
    log_launch('start ' + APP_VERSION)
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    instance = Instance()
    if not instance.claim():
        return
    try:
        app = UsageWidget()
        app.root.update_idletasks()
        hwnd = int(app.root.wm_frame(), 16)
        instance.identify(hwnd)
        try:
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except (AttributeError, OSError):
            pass
        app.root.mainloop()
    finally:
        instance.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        record_crash()
        notify_user('AI Usage', '위젯을 시작하지 못했습니다.\n\n%APPDATA%\\AiUsageWidget\\error.log 를 확인하세요.')
        raise
