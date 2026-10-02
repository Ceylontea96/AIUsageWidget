"""Colours, fonts and sizes the widget is drawn with, from its design tokens.

No Tk here: fonts are registered with GDI and sizes are plain numbers, so the
cards, the window and the tests read the same values.
"""
from __future__ import annotations

import ctypes
import math
import sys
from pathlib import Path

import widget_raster as raster

ICON_DIR = Path(__file__).resolve().parent / 'assets' / 'icons'
FONT_DIR = Path(__file__).resolve().parent / 'assets' / 'fonts'
_FONTS_REGISTERED = None
PRETENDARD_FILES = (
    'Pretendard-Regular.ttf',
    'Pretendard-Medium.ttf',
    'Pretendard-SemiBold.ttf',
)


def register_bundled_fonts():
    """Load Pretendard for this process only. Returns True if UI can use it."""
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED is not None:
        return _FONTS_REGISTERED
    _FONTS_REGISTERED = False
    if sys.platform != 'win32':
        return False
    try:
        add = ctypes.windll.gdi32.AddFontResourceExW
    except AttributeError:
        return False
    add.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
    add.restype = ctypes.c_int
    loaded = 0
    for name in PRETENDARD_FILES:
        path = FONT_DIR / name
        try:
            if path.is_file() and add(str(path), 0x10, None):
                loaded += 1
        except (OSError, OverflowError, TypeError, ValueError):
            continue
    _FONTS_REGISTERED = loaded >= 2
    return _FONTS_REGISTERED


def ui_faces():
    if register_bundled_fonts():
        return 'Pretendard', 'Pretendard Medium', 'Pretendard SemiBold'
    return 'Malgun Gothic', 'Malgun Gothic', 'Segoe UI Semibold'


# Colours and base sizes the window is drawn from, all read just below. Font
# faces and sizes are the FONT_* values after them.
TOKENS = {'width': 380,
 'radius': {'card': 12},
 'strip': {'width': 3},
 'header_h': 38,
 'compact_h': 44,
 'footer_h': 36,
 'gap': {'cards': 0},
 'bar': {'height': 10},
 'chip': {'height': 24},
 'color': {'bg_window': '#0F1013',
           'bg_card': '#101316',
           'hairline': '#202327',
           'track': '#202327',
           'fg': '#E7E8EC',
           'fg_muted': '#8B8F99',
           'fg_dim': '#5B6069',
           'codex': '#10A37F',
           'cursor': '#A78BFA',
           'claude': '#C96442',
           'warn': '#F5B544',
           'danger': '#EF4444',
           'stale_strip': '#4A5060',
           'stale_hero': '#8B8F99',
           'icon': '#B4BAC8',
           'close_hover_bg': '#3A2020',
           'chip_codex_fill': '#1F6B5A',
           'chip_cursor_fill': '#5B4A9E',
           'chip_claude_fill': '#6B3A2A',
           'chip_warn_fill': '#C48A22',
           'chip_danger_fill': '#B44545',
           'chip_stale_fill': '#3A4252'}}
BG, CARD, HAIR, TRACK = (TOKENS['color'][k] for k in ('bg_window','bg_card','hairline','track'))
TEXT, MUTED, DIM = (TOKENS['color'][k] for k in ('fg','fg_muted','fg_dim'))
CODEX, CURSOR, CLAUDE = TOKENS['color']['codex'], TOKENS['color']['cursor'], TOKENS['color']['claude']
WARN, DANGER = TOKENS['color']['warn'], TOKENS['color']['danger']
STALE_STRIP, STALE_HERO = TOKENS['color']['stale_strip'], TOKENS['color']['stale_hero']
ICON, HOVER, CLOSE_HOVER = TOKENS['color']['icon'], '#20232D', TOKENS['color']['close_hover_bg']
CHIP_CODEX, CHIP_CURSOR, CHIP_CLAUDE = (
    TOKENS['color']['chip_codex_fill'],
    TOKENS['color']['chip_cursor_fill'],
    TOKENS['color']['chip_claude_fill'],
)
CHIP_WARN, CHIP_DANGER, CHIP_STALE = (TOKENS['color'][k] for k in ('chip_warn_fill','chip_danger_fill','chip_stale_fill'))
CHIP_FG, CHIP_TRACK = '#F2FFFB', '#2A3142'
ACCENTS = {'chatgpt': CODEX, 'cursor': CURSOR, 'claude': CLAUDE}
CHIP_OK = {'chatgpt': CHIP_CODEX, 'cursor': CHIP_CURSOR, 'claude': CHIP_CLAUDE}
TITLES = {'chatgpt': 'GPT', 'cursor': 'Cursor', 'claude': 'Claude'}
ICON_HINTS = {
    'refresh': '새로고침 (F5)',
    'minus': '한 줄로 접기',
    'expand': '상세로 펼치기',
    'close': '종료',
}
URLS = {
    'chatgpt': 'https://chatgpt.com/codex/settings/usage',
    'cursor': 'https://cursor.com/dashboard/usage',
    'claude': 'https://claude.ai/settings/usage',
}
FETCHERS = ('chatgpt', 'cursor', 'claude')
NETWORK_FETCHERS = ('chatgpt', 'cursor')
WINDOW_W, HEADER_H, COMPACT_H, FOOTER_H = (TOKENS[k] for k in ('width','header_h','compact_h','footer_h'))
BAR_H, STRIP_W, CHIP_H = TOKENS['bar']['height'], TOKENS['strip']['width'], TOKENS['chip']['height']
CARD_W, CARD_RADIUS, CARD_GAP = WINDOW_W - 2, TOKENS['radius']['card'], TOKENS['gap']['cards']
# Negative Tk font sizes are pixels, avoiding point/DPI-driven layout inflation.
FACE, FACE_MED, FACE_SEMI = ui_faces()
FONT_TITLE = (FACE_SEMI, -13)
FONT_SERVICE = (FACE_SEMI, -14)
FONT_HERO = (FACE_SEMI, -21)
FONT_SUB = (FACE_MED, -12)
FONT_PLAN = (FACE_SEMI, -11)
FONT_ROW = (FACE_MED, -12)
FONT_VALUE = (FACE_SEMI, -12)
FONT_META = (FACE, -11)
FONT_CHIP = (FACE_SEMI, -12)
FONT_PILL = (FACE_SEMI, -11)
FONT_FOOT = (FACE, -11)
FONT_BADGE = (FACE_MED, -11)
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
    def chip_canvas_h(self):
        return self.p(28, 1)

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


# Brighter than the 11px meta gray so a check time and an error stay readable.
STATUS_FG = raster.blend(MUTED, TEXT, 0.55)
