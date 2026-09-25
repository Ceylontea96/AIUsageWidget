"""Small, read-only quota monitor. All Tk calls stay on the main thread."""
from __future__ import annotations

from functools import lru_cache
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import queue
import re
import struct
import subprocess
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

from additional_ui import AdditionalBlock, additional_count, expanded_body_budget, layout_additional
from bar_raster import cover_round_rect as _cover_round_rect, progress_rgba, ringed_progress_rgba
from frame_clock import FAST as FRAME_FAST, SLOW as FRAME_SLOW, clock_for
from codex_activity import CodexActivityMonitor, FAST_INTERVAL, LOG
from claude_activity import ClaudeActivityMonitor
from cursor_activity import CursorActivityMonitor
from providers import (
    claude_plan_label,
    dollars,
    error_snapshot,
    fetch_claude,
    fmt_local,
    snapshot_from_dict,
    snapshot_to_dict,
    to_float,
)
from quota_policy import (
    FIVE_HOURS,
    FIVE_HOUR_TOLERANCE,
    global_main_limits,
    group_stale,
    main_remaining_percents,
    remaining_band,
    representative_blocked,
    representative_percent,
    select_hero,
    service_remaining,
    window_matches,
)
from codex_app_server import CodexAppServer
from runtime import AlertGate, AuthWatcher, CodexJob, PollRunner, ToastSender, WorkerJob, limiting_quota, login_present, login_status, prepare_action, session_locked, start_tool_setup
from updater import APP_VERSION, CHECK_EVERY, LAUNCHER_EXE, download_and_stage, fetch_latest, load_feed_url, start_apply, update_confirm_text

APP_DIR = Path(os.environ.get('APPDATA', str(Path.home()))) / 'AiUsageWidget'
SETTINGS_PATH = APP_DIR / 'settings.json'
CACHE_PATH = APP_DIR / 'last_snapshot.json'
ALERT_PATH = APP_DIR / 'alerts.json'
INSTALL_PATH = APP_DIR / 'install.json'
ICON_DIR = Path(__file__).resolve().parent / 'assets' / 'icons'
FONT_DIR = Path(__file__).resolve().parent / 'assets' / 'fonts'
SHORTCUT_NAME = 'AI Usage.lnk'
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


def lock_path():
    return APP_DIR / 'widget.lock'


def instance_path():
    return APP_DIR / 'widget.instance'

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


def widget_root():
    return Path(__file__).resolve().parent


def is_widget_root(path):
    path = Path(path)
    return (path / 'usage_widget.py').is_file() and (path / 'setup_and_run.ps1').is_file()


def read_install_root(path=None):
    data = read_json(path or INSTALL_PATH)
    raw = str(data.get('root') or '')
    if not raw:
        return None
    root = Path(raw)
    if root.is_dir() and is_widget_root(root):
        return root.resolve()
    return None


def save_install_root(root=None, shortcut_asked=None):
    root = Path(root or widget_root()).resolve()
    if not is_widget_root(root):
        raise RuntimeError('위젯 폴더가 아닙니다.')
    current = read_json(INSTALL_PATH)
    current['root'] = str(root)
    if shortcut_asked is not None:
        current['shortcut_asked'] = bool(shortcut_asked)
    save_json(INSTALL_PATH, current)
    return root


def desktop_dir():
    buf = ctypes.create_unicode_buffer(260)
    try:
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0 and buf.value:
            return Path(buf.value)
    except (AttributeError, OSError):
        pass
    return Path(os.environ.get('USERPROFILE', str(Path.home()))) / 'Desktop'


def create_desktop_shortcut(root=None, desktop=None):
    root = Path(root or widget_root()).resolve()
    exe = root / LAUNCHER_EXE
    if not exe.is_file():
        raise RuntimeError('AI Usage.exe를 찾지 못했습니다. zip을 폴더로 푼 뒤 다시 시도하세요.')
    script = widget_root() / 'create_shortcut.ps1'
    if not script.is_file():
        raise RuntimeError('create_shortcut.ps1을 찾지 못했습니다.')
    desktop = Path(desktop) if desktop else desktop_dir()
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    completed = subprocess.run(
        [
            'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', str(script),
            '-Root', str(root),
            '-Desktop', str(desktop),
        ],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        creationflags=flags,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or '').strip()
        raise RuntimeError(detail or '바탕화면 바로가기를 만들지 못했습니다.')
    save_install_root(root, shortcut_asked=True)
    return desktop / SHORTCUT_NAME


def _state_for(snap, remaining, blocked):
    if snap.stale:
        return 'stale'
    if not snap.ok or blocked:
        return 'danger'
    band = remaining_band(remaining)
    # The chip has no separate critical fill; under 5% stays on the danger colour.
    if band in ('critical', 'danger'):
        return 'danger'
    if band == 'warn':
        return 'warn'
    return 'ok'


def service_state(snap):
    """Risk channel: the most limiting quota, which may not be the hero.

    Badges, the alert strip and the footer read this, so a quota the hero does
    not speak for can still raise a warning.
    """
    return _state_for(snap, service_remaining(snap), representative_blocked(snap))


def representative_state(snap):
    """Representative channel: the same quota the big number comes from.

    The compact chip uses the same 50 / 20 / 5 bands as the ring, so a
    percentage cannot be calm in one place and cautious in the other.
    """
    return _state_for(snap, representative_percent(snap), representative_blocked(snap))


def color_for(snap):
    state = representative_state(snap)
    if state == 'stale':
        return STALE_HERO
    if state == 'warn':
        return WARN
    if state == 'danger':
        return DANGER
    return ACCENTS.get(getattr(snap, 'key', ''), CODEX)


def chip_style(key, snap):
    if snap is None:
        return CHIP_STALE, CHIP_FG
    state = representative_state(snap)
    if state == 'stale':
        return CHIP_STALE, CHIP_FG
    if state == 'danger':
        return CHIP_DANGER, CHIP_FG
    if state == 'warn':
        return CHIP_WARN, CHIP_FG
    return CHIP_OK[key], CHIP_FG


def compact_warning(snap):
    """Flag a low secondary global quota without changing the hero's meaning."""
    if snap is None or not snap.ok or snap.stale:
        return None
    hero = select_hero(snap)
    candidates = [item for item in global_main_limits(snap)
                  if (hero is None or item.quota_id != hero.quota_id)
                  and remaining_band(item.remaining_percent) in ('warn', 'danger', 'critical')]
    return min(candidates, key=lambda item: item.remaining_percent) if candidates else None


def _checked_phrase(stamp, now, *, confirmed):
    """Relative time for one service. Empty when this snapshot was never stamped."""
    try:
        stamp = float(stamp or 0)
    except (TypeError, ValueError):
        return ''
    if not math.isfinite(stamp) or stamp <= 0:
        return ''
    age = max(0, int(now - stamp))
    if age < 5:
        head = '방금'
    elif age < 60:
        head = f'{age}초 전'
    elif age < 3600:
        head = f'{age // 60}분 전'
    elif age < 86400:
        head = f'{age // 3600}시간 전'
    else:
        head = f'{age // 86400}일 전'
    return f'{head} 확인' if confirmed else f'{head} 시도'


def failure_cause(snap):
    """Short reason for a failed check. Login failures share one phrase."""
    if snap is None:
        return ''
    internal = getattr(snap, 'internal', None) or {}
    text = str(getattr(snap, 'error', '') or '').strip()
    if internal.get('requires_login'):
        return '재로그인 필요'
    if not text:
        return ''
    if any(phrase in text for phrase in ('가져오는 중', '기다리는 중', '조회 중')):
        return '확인 중'
    folded = text.casefold()
    if any(token in folded for token in ('로그인', 'login', 'sign in', 'signin', '인증', '토큰', 'token')):
        return '재로그인 필요'
    if '초과' in text or 'timeout' in folded:
        return '응답 시간 초과'
    if '연결' in text or 'offline' in folded or 'connection' in folded:
        return '연결 확인 필요'
    if '형식' in text:
        return '응답 형식 확인 필요'
    sentence = text.split('\n', 1)[0].strip()
    for sep in ('。', '. '):
        sentence = sentence.split(sep, 1)[0]
    sentence = sentence.strip(' .')
    if len(sentence) > 24:
        return '조회 실패'
    return sentence


def service_status_text(snap, now=None):
    """Per-service check age, and the cached-value cause when a later check failed."""
    if snap is None:
        return ''
    now = time.time() if now is None else now
    age = _checked_phrase(getattr(snap, 'fetched_at', 0), now, confirmed=bool(snap.ok))
    if not snap.ok:
        return age
    if snap.stale:
        cause = failure_cause(snap)
        detail = f'이전 값 · {cause}' if cause else '이전 값'
        return f'{age} · {detail}' if age else detail
    return age


def service_needs_actions(snap):
    """Retry and login guidance belong on a real failure, not a check still in flight."""
    if snap is None or failure_cause(snap) == '확인 중':
        return False
    if not snap.ok:
        return True
    return bool(snap.stale and getattr(snap, 'error', ''))


def header_freshness(snaps, now=None):
    """Header age follows the oldest confirmation, so one fresh service cannot refresh the rest."""
    now = time.time() if now is None else now
    snaps = list(snaps or ())
    stamps = []
    for snap in snaps:
        if not (snap.ok or snap.stale):
            continue
        try:
            stamp = float(snap.fetched_at or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(stamp) and stamp > 0:
            stamps.append(stamp)
    if not stamps:
        return ('확인 실패', None) if any(not snap.ok for snap in snaps) else ('갱신 대기', None)
    oldest, newest = min(stamps), max(stamps)
    age = max(0, int(now - oldest))
    newest_age = max(0, int(now - newest))
    if len(stamps) > 1 and newest - oldest >= 30 and newest_age < 60:
        return '서비스별 확인', age
    return _checked_phrase(oldest, now, confirmed=True) or '갱신 대기', age


def login_guidance(key, snap=None):
    """Action behind the card's login-help button."""
    if key == 'claude' and failure_cause(snap) == '재로그인 필요':
        return 'Claude 로그인', 'claude-login'
    return prepare_action(key)


def compact_tooltip(snap):
    if snap is None:
        return '사용량 확인 중'
    lines = [TITLES.get(snap.key, snap.title)]
    status = service_status_text(snap)
    if status:
        lines.append(status)
    if snap.error and snap.error not in (status or ''):
        lines.append(snap.error)
    if snap.blocked and not snap.stale:
        lines.append('현재 사용 제한')
    warning = compact_warning(snap)
    if warning is not None:
        lines.append(f'주의: {warning.display_name} 잔여 {warning.remaining_percent:.0f}%')
    for item in global_main_limits(snap):
        value = '확인 중' if item.remaining_percent is None else f'잔여 {item.remaining_percent:.0f}%'
        lines.append(f'{item.display_name} · {value}')
    return '\n'.join(lines)


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
SHIMMER_GROW_S = 0.4
SHIMMER_SHRINK_S = 0.85
SHIMMER_SWEEP_S = 1.2
SHIMMER_GLOW = 0.65


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


def reset_stamp(text):
    if not text:
        return ''
    found = re.search(r'(\d{1,2}:\d{2})', text)
    return f'{found.group(1)} 리셋' if found else ''


def dated_reset_stamp(text):
    clean = str(text or '').replace(' 초기화', '').replace(' 재설정', '').replace(' 리셋', '').strip()
    return f'{clean} 리셋' if clean else ''


def billing_entry(snap, kind):
    for item in getattr(snap, 'billing', None) or []:
        if item.kind == kind:
            return item
    return None


def billing_value(snap, kind):
    item = billing_entry(snap, kind)
    return None if item is None else item.raw_value


def reset_credit(snap, now=None):
    item = billing_entry(snap, 'reset_credits')
    available = to_float(item.raw_value) if item is not None else None
    if not available:
        return ''
    text = f'리셋권 {available:.0f}'
    # With several credits the provider already picked the nearest expiry.
    expiry = to_float((item.metadata or {}).get('nearest_expires_at'))
    current = time.time() if now is None else now
    if expiry is not None and expiry > current:
        text += f' · {fmt_local(expiry, "reset")} 만료'
    return text


def included_amount(snap):
    limit = to_float(billing_value(snap, 'limit'))
    if limit is None:
        return ''
    included = to_float(billing_value(snap, 'includedSpend')) or 0.0
    return f'{dollars(included)} / {dollars(limit)}'


def bonus_line(snap):
    bonus = to_float(billing_value(snap, 'bonusSpend'))
    return f'보너스 {dollars(bonus)}' if bonus else ''


def main_limits(snap):
    """The card's rows: global main quota, in canonical order."""
    return global_main_limits(snap) if snap is not None and snap.ok else []


def hero_index(snap):
    """Position of the hero within main_limits(snap), for painting only."""
    hero = select_hero(snap) if snap is not None and snap.ok else None
    if hero is None:
        return None
    for index, item in enumerate(main_limits(snap)):
        if item is hero:
            return index
    return None


def status_percent(snap):
    """Service warnings can differ from the window chosen for the large number."""
    return service_remaining(snap)


def quota_alert_copy(key, severity, remaining, label):
    provider = {'chatgpt': 'ChatGPT', 'claude': 'Claude'}.get(key, TITLES.get(key, key))
    quota = quota_window_title(label)
    status = '소진' if remaining <= 0 else '제한' if severity == 2 else '임박'
    return f'{provider} {quota} {status}', f'{quota} · 잔여 {remaining:.0f}%'


def quota_window_title(label):
    label = str(label or '').strip()
    return f'{label} 한도' if label else '사용량 한도'


def quota_row_title(item):
    """Time-boxed quota reads as a limit; a named pool keeps its own name.

    The distinction is the measured window, not the provider or the label.
    """
    name = str(getattr(item, 'display_name', '') or getattr(item, 'window_label', '') or '')
    if getattr(item, 'window_seconds', None) is None:
        return name or '남은 사용량'
    return quota_window_title(name)


def quota_extras(snap):
    parts = []
    credits = to_float(billing_value(snap, 'credits'))
    if credits is not None:
        parts.append(f'크레딧 {credits:g}')
    reset = reset_credit(snap)
    if reset:
        parts.append(reset)
    return ' · '.join(parts)


# How many secondary quota rows the card draws. This is a display budget so a
# provider that starts reporting many windows cannot push the card off screen;
# risk detection and alerts still read every one of them.
MAX_SECONDARY_ROWS = 6
# Compact row budget. The window controls own a reserved strip on the right
# that provider chips may never enter, so the row gives up the title before it
# gives up a provider's name or percentage.
COMPACT_TITLE_STEPS = (('AI Usage', 76), ('AI', 34), ('', 12))
COMPACT_CHIP_GAPS = (8, 6, 4)
COMPACT_CHIP_PAD = 4
COMPACT_CONTROLS_GAP = 6


def compact_row_layout(*, count, chip_width, controls_left, scale_px,
                       title_steps=COMPACT_TITLE_STEPS, gaps=COMPACT_CHIP_GAPS):
    """Fit `count` chips between the title and the reserved controls strip.

    Returns (title, start_x, chip_width, gap). The title shrinks first
    ("AI Usage" then "AI" then nothing) and the gap only afterwards, so a chip
    keeps its full provider name and percentage for as long as possible.
    """
    if count <= 0:
        return title_steps[0][0], scale_px(title_steps[0][1]), chip_width, scale_px(gaps[0])
    limit = controls_left - scale_px(COMPACT_CONTROLS_GAP)
    for title, start in title_steps:
        origin = scale_px(start)
        for gap in gaps:
            step = scale_px(gap)
            if origin + count * chip_width + step * (count - 1) <= limit:
                return title, origin, chip_width, step
    # Nothing fits at the preferred width. Keep the tightest arrangement and
    # narrow the chips rather than letting them cross into the controls.
    title, origin = title_steps[-1][0], scale_px(title_steps[-1][1])
    step = scale_px(gaps[-1])
    room = limit - origin - step * (count - 1)
    return title, origin, max(1, room // count), step


ACTIVE_HOLD = 60
ACTIVITY_TICK_MS = 250
LOG_LIMIT = 256 * 1024
CALLBACK_ERROR_REPEAT = 60.0
# statusLine only runs in terminal Claude Code. When it is silent the widget
# asks the CLI itself; that costs a process, not tokens, so keep it infrequent.
CLAUDE_CLI_INTERVAL = 60.0
CLAUDE_CLI_STALE = 300.0


def remaining_marks(snap):
    """Every global main quota, so risk never misses a later one."""
    if not snap or not snap.ok:
        return []
    return [bar_display_percent(value) for value in main_remaining_percents(snap)]


def usage_dropped(previous, current):
    before = remaining_marks(previous)
    after = remaining_marks(current)
    if not before or len(before) != len(after):
        return False
    return any(old - new > 0.25 for old, new in zip(before, after))


def next_fast_due(started, now, interval=FAST_INTERVAL):
    return max(now, started + interval)


def next_interval(snap, failures=0, active=False):
    from polling import next_poll_delay, policy_for
    policy = policy_for(getattr(snap, 'key', '') if snap else '')
    return next_poll_delay(
        policy,
        ok=bool(snap and snap.ok),
        blocked=bool(snap and getattr(snap, 'blocked', False)),
        hero_percent=representative_percent(snap),
        main_remaining=remaining_marks(snap) if snap else [],
        failures=int(failures or 0),
        active=bool(active),
        retry_after=getattr(snap, 'retry_after', '') if snap else '',
    )


def should_setup(settings, preview=False):
    if preview:
        return False
    if settings.get('setup_done'):
        return False
    if settings.get('version') and isinstance(settings.get('enabled'), dict):
        return False
    return True


def update_menu_label(version):
    """The one update entry: check while nothing is waiting, install once something is."""
    return f'업데이트 {version} 설치...' if version else f'업데이트 확인 ({APP_VERSION})...'


def default_enabled(settings, preview=False, present=None):
    saved = settings.get('enabled')
    if isinstance(saved, dict) and not should_setup(settings, preview):
        return {key: bool(saved.get(key, key != 'claude')) for key in FETCHERS}
    present = present if present is not None else {key: login_present(key) for key in FETCHERS}
    if any(present.get(key) for key in NETWORK_FETCHERS):
        return {key: bool(present.get(key)) if key != 'claude' else False for key in FETCHERS}
    return {key: key != 'claude' for key in FETCHERS}


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
        user32.FindWindowExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.FindWindowExW.restype = ctypes.c_void_p
        # A submenu is its own #32768 window, so every one of ours is raised.
        hwnd = None
        for _ in range(8):
            hwnd = user32.FindWindowExW(None, hwnd, '#32768', None)
            if not hwnd:
                return
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
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


def center_box(width, height, ref_x=0, ref_y=0):
    """Top-left that centers a box on the monitor work area containing (ref_x, ref_y)."""
    width = max(1, int(width))
    height = max(1, int(height))
    left, top, right, bottom = work_area(int(ref_x), int(ref_y))
    x = left + max(0, (right - left - width) // 2)
    y = top + max(0, (bottom - top - height) // 2)
    return x, y


def place_on_screen_center(win, ref_x=None, ref_y=None):
    """Move a Toplevel to the monitor center. ref_* only picks the monitor, not an offset."""
    try:
        win.update_idletasks()
        width = max(win.winfo_reqwidth(), win.winfo_width(), 1)
        height = max(win.winfo_reqheight(), win.winfo_height(), 1)
        if ref_x is None:
            ref_x = win.master.winfo_rootx() if win.master else 0
        if ref_y is None:
            ref_y = win.master.winfo_rooty() if win.master else 0
        x, y = center_box(width, height, ref_x, ref_y)
        win.geometry(geometry_at(x, y))
    except (tk.TclError, OSError, TypeError, ValueError):
        pass


def startup_path():
    return Path(os.environ.get('APPDATA', '')) / 'Microsoft/Windows/Start Menu/Programs/Startup/AIUsageWidget.vbs'


def startup_script(root=None):
    """The logon entry: the widget's own launcher, which finds Python each time.

    Naming pythonw.exe here broke Windows startup silently whenever Python was
    upgraded or reinstalled somewhere else.
    """
    root = Path(root or widget_root()).resolve()
    launcher = root / 'start_usage_widget.vbs'
    if not launcher.is_file():
        raise RuntimeError('start_usage_widget.vbs를 찾을 수 없습니다.')
    # Plain \n: writing in text mode turns each into \r\n on Windows.
    return ('Set sh = CreateObject("Wscript.Shell")\n'
            f'sh.CurrentDirectory = "{root}"\n'
            f'sh.Run "wscript.exe ""{launcher}""", 0, False\n')


def set_startup(enabled, root=None):
    path = startup_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    value = startup_script(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-16')


def sync_startup(root=None):
    """Rewrite an existing logon entry that points somewhere else, such as an
    older pythonw.exe or a folder the widget has moved out of."""
    path = startup_path()
    if not path.is_file():
        return False
    try:
        wanted = startup_script(root)
        if path.read_text(encoding='utf-16') == wanted:
            return False
    except (OSError, UnicodeError, RuntimeError):
        return False
    try:
        path.write_text(wanted, encoding='utf-16')
    except OSError:
        return False
    return True


def load_icon(name, scale):
    # The whole design uses 1x pixel dimensions; the supplied icons are 16x16.
    path = ICON_DIR / f'{name}.png'
    return tk.PhotoImage(data=path.read_bytes(), format='png') if path.is_file() else None


# Provider key -> (icon asset stem, image height at scale 1). The heights make
# the marks look the same size: the OpenAI Blossom file keeps its clear space
# around a mark about half as tall, the Cursor cube has none. The renderer
# stays generic: a provider without an entry simply draws no icon.
SERVICE_ICONS = {'chatgpt': ('service_gpt', 33), 'cursor': ('service_cursor', 17)}


def read_png_rgba(data):
    """Decode an 8-bit RGBA, non-interlaced PNG, the only kind the icon masters are."""
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG')
    pos, idat, header = 8, [], None
    while pos < len(data):
        size, tag = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + size]
        if tag == b'IHDR':
            header = struct.unpack('>IIBBBBB', body)
        elif tag == b'IDAT':
            idat.append(body)
        pos += 12 + size
    if header is None or header[2:4] != (8, 6) or header[6] != 0:
        raise ValueError('unsupported PNG')
    width, height = header[:2]
    raw, stride, rows, prev, at = zlib.decompress(b''.join(idat)), width * 4, [], bytearray(width * 4), 0
    for _ in range(height):
        kind, line = raw[at], bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            left = line[i - 4] if i >= 4 else 0
            up, corner = prev[i], (prev[i - 4] if i >= 4 else 0)
            if kind == 1:
                line[i] = (line[i] + left) & 255
            elif kind == 2:
                line[i] = (line[i] + up) & 255
            elif kind == 3:
                line[i] = (line[i] + (left + up) // 2) & 255
            elif kind == 4:
                guess = left + up - corner
                pa, pb, pc = abs(guess - left), abs(guess - up), abs(guess - corner)
                line[i] = (line[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else corner)) & 255
        rows.append(line)
        prev = line
    return width, height, rows


def shrink_rgba(width, height, rows, target_w, target_h):
    """Area-average to a smaller size. Alpha is premultiplied so edges do not darken."""
    def weights(source, target):
        scale = source / target
        spans = []
        for t in range(target):
            start, end = t * scale, (t + 1) * scale
            spans.append([(s, min(end, s + 1) - max(start, s)) for s in range(int(start), min(source, math.ceil(end)))])
        return spans, scale
    cols, sx = weights(width, target_w)
    lines, sy = weights(height, target_h)
    wide = []
    for row in rows:
        out = []
        for span in cols:
            r = g = b = a = 0.0
            for s, w in span:
                alpha = row[s * 4 + 3] * w
                r += row[s * 4] * alpha
                g += row[s * 4 + 1] * alpha
                b += row[s * 4 + 2] * alpha
                a += alpha
            out.append((r, g, b, a))
        wide.append(out)
    result = []
    for span in lines:
        row = bytearray()
        for x in range(target_w):
            r = g = b = a = 0.0
            for s, w in span:
                pr, pg, pb, pa = wide[s][x]
                r += pr * w
                g += pg * w
                b += pb * w
                a += pa * w
            if a <= 0:
                row += b'\x00\x00\x00\x00'
                continue
            row += bytes((min(255, int(r / a + .5)), min(255, int(g / a + .5)), min(255, int(b / a + .5)),
                          min(255, int(a / (sx * sy) + .5))))
        result.append(row)
    return target_w, target_h, result


@lru_cache(maxsize=16)
def service_icon_png(name, height):
    """The master shrunk to `height`, keeping its proportions. Cached per size."""
    width, source_h, rows = read_png_rgba((ICON_DIR / f'{name}.png').read_bytes())
    height = max(1, min(source_h, int(height)))
    target_w = max(1, int(round(width * height / source_h)))
    return _png_rgba(*shrink_rgba(width, source_h, rows, target_w, height), level=6)


@lru_cache(maxsize=16)
def chevron_png(size, down, color, stroke):
    """A two-stroke chevron with soft edges, like the header's line icons.

    Tk canvas lines have no anti-aliasing on Windows, so it is rasterised here
    with 4x4 samples per pixel.
    """
    s = float(size)
    if down:
        points = ((.22*s, .38*s), (.5*s, .66*s), (.78*s, .38*s))
    else:
        points = ((.38*s, .22*s), (.66*s, .5*s), (.38*s, .78*s))
    half = stroke / 2.0

    def near(x, y):
        for (ax, ay), (bx, by) in zip(points, points[1:]):
            dx, dy = bx - ax, by - ay
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
            if math.hypot(x - ax - t * dx, y - ay - t * dy) <= half:
                return True
        return False

    r, g, b = _hex_rgb(color)
    rows = []
    for py in range(size):
        row = bytearray()
        for px_ in range(size):
            hits = sum(near(px_ + (i + .5) / 4, py + (j + .5) / 4) for i in range(4) for j in range(4))
            row += bytes((r, g, b, int(round(255 * hits / 16))))
        rows.append(row)
    return _png_rgba(size, size, rows, level=6)


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


# Brighter than the 11px meta gray so a check time and an error stay readable.
STATUS_FG = blend(MUTED, TEXT, 0.55)


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


def progress_bar_rgba(width, height, radius, fill_width, track, fill, background, samples=1, shimmer=None, shape_height=None):
    """Track + clipped fill as opaque RGBA rows. Fill cannot paint outside the track.

    Every pixel is computed on its own. Frames are drawn by
    bar_raster.progress_rgba, which must return the same bytes.
    """
    samples = max(1, int(samples))
    width = max(1, int(round(width)))
    height = max(1, int(round(height)))
    shape_height = float(height) if shape_height is None else max(0.0, min(height, float(shape_height)))
    fill_width = max(0.0, min(float(width), float(fill_width)))
    if samples > 1:
        src_w, src_h, rows = progress_bar_rgba(
            width * samples, height * samples, radius * samples, fill_width * samples,
            track, fill, background, samples=1, shimmer=shimmer, shape_height=shape_height * samples)
        return _box_downsample(rows, samples, width, height)
    tr, tg, tb = _hex_rgb(track)
    fr, fg, fb = _hex_rgb(fill)
    br, bg_, bb = _hex_rgb(background)
    colors = [(fr, fg, fb)] * width
    if shimmer is not None and fill_width > 0 and 0.15 < shimmer < 0.75:
        t = (shimmer - 0.15) / 0.60
        t = t * t * (3.0 - 2.0 * t)
        pulse = math.sin(math.pi * t) * 0.12
        fr += (255 - fr) * pulse
        fg += (255 - fg) * pulse
        fb += (255 - fb) * pulse
        colors = [(fr, fg, fb)] * width
        band = max(height * 1.5, fill_width * 0.20)
        center = -band + (fill_width + 2.0 * band) * t
        for x in range(min(width, int(math.ceil(fill_width)))):
            weight = max(0.0, 1.0 - abs(x + 0.5 - center) / band)
            glow = weight * weight * (3.0 - 2.0 * weight) * SHIMMER_GLOW
            colors[x] = (fr + (255 - fr) * glow, fg + (255 - fg) * glow, fb + (255 - fb) * glow)
    rows = []
    for y in range(height):
        py = y + 0.5 - (height - shape_height) / 2
        row = bytearray()
        for x in range(width):
            px = x + 0.5
            fr, fg, fb = colors[x]
            track_a = _cover_round_rect(px, py, width, shape_height, radius)
            fill_a = _cover_round_rect(px, py, fill_width, shape_height, radius) if fill_width > 0 else 0.0
            fill_a = min(fill_a, track_a)
            r = fr * fill_a + tr * (track_a - fill_a) + br * (1.0 - track_a)
            g = fg * fill_a + tg * (track_a - fill_a) + bg_ * (1.0 - track_a)
            b = fb * fill_a + tb * (track_a - fill_a) + bb * (1.0 - track_a)
            row.extend((int(r + 0.5), int(g + 0.5), int(b + 0.5), 255))
        rows.append(row)
    return width, height, rows


def _png_rgba(width, height, rows, level=9):
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    raw = b''.join(b'\x00' + bytes(row) for row in rows)
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw, level)) + chunk(b'IEND', b'')


def progress_bar_png(width, height, radius, fill_width, track, fill, background, samples=1, shimmer=None, shape_height=None):
    w, h, rows = progress_bar_rgba(width, height, radius, fill_width, track, fill, background,
                                   samples=samples, shimmer=shimmer, shape_height=shape_height)
    return _png_rgba(w, h, rows)


def progress_photo(width, height, radius, fill_width, track, fill, background, samples=1, shimmer=None, shape_height=None):
    if samples != 1:
        return tk.PhotoImage(data=progress_bar_png(width, height, radius, fill_width, track, fill, background,
                                                   samples=samples, shimmer=shimmer, shape_height=shape_height), format='png')
    w, h, rows = progress_rgba(width, height, radius, fill_width, track, fill, background,
                               shimmer=shimmer, shape_height=shape_height, glow=SHIMMER_GLOW)
    # A frame lives for one paint; fast compression beats a smaller file.
    return tk.PhotoImage(data=_png_rgba(w, h, rows, level=1), format='png')


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


def notify_user(title, text, icon=0x10):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x00040000 | icon)
    except (AttributeError, OSError):
        pass


def rotate_log(path, limit=LOG_LIMIT):
    """Keep one previous file, so a log that is only ever appended to stays small."""
    try:
        if path.stat().st_size > limit:
            os.replace(path, path.with_name(path.name + '.1'))
    except OSError:
        pass


def log_launch(message):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    path = APP_DIR / 'launch.log'
    rotate_log(path)
    line = time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + message + '\n'
    with path.open('a', encoding='utf-8') as log:
        log.write(line)


class CallbackErrors:
    """Tk callback exceptions, which pythonw would otherwise drop unseen.

    The same failure is written at most once a minute, with the number of
    repeats skipped in between, so an error on every tick cannot fill the disk.
    """

    def __init__(self, path=None, repeat=CALLBACK_ERROR_REPEAT, clock=time.monotonic):
        self.path = path
        self.repeat = repeat
        self.clock = clock
        self.seen = {}

    @staticmethod
    def signature(exc, tb):
        frames = traceback.extract_tb(tb)
        where = (frames[-1].filename, frames[-1].lineno) if frames else ('', 0)
        return getattr(exc, '__name__', str(exc)), where

    def report(self, exc, value, tb):
        key = self.signature(exc, tb)
        now = self.clock()
        entry = self.seen.get(key)
        if entry is not None and now - entry[0] < self.repeat:
            entry[1] += 1
            return False
        skipped = entry[1] if entry is not None else 0
        if len(self.seen) >= 64:
            self.seen.clear()
        self.seen[key] = [now, 0]
        header = time.strftime('%Y-%m-%d %H:%M:%S')
        if skipped:
            header += f' (+{skipped} repeats)'
        text = ''.join(traceback.format_exception(exc, value, tb))
        path = self.path or APP_DIR / 'runtime-error.log'
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            rotate_log(path)
            with path.open('a', encoding='utf-8') as log:
                log.write(header + '\n' + text + '\n')
        except OSError:
            pass
        return True


def record_crash():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    text = time.strftime('%Y-%m-%d %H:%M:%S') + '\n' + traceback.format_exc()
    (APP_DIR / 'error.log').write_text(text, encoding='utf-8')
    log_launch('crash')
    return text


def read_lock(path=None):
    path = path or instance_path()
    raw = path.read_text(encoding='ascii', errors='replace').strip().splitlines()
    pid = int(raw[0]) if raw else 0
    hwnd = int(raw[1]) if len(raw) >= 2 else 0
    return pid, hwnd


def write_instance(pid, hwnd=0):
    path = instance_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(f'{int(pid)}\n{int(hwnd)}\n', encoding='ascii')
    os.replace(tmp, path)


def process_alive(pid):
    if pid <= 0:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def terminate_pid(pid):
    if pid <= 0:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)
    if not handle:
        return False
    try:
        return bool(ctypes.windll.kernel32.TerminateProcess(handle, 1))
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def show_window(hwnd):
    user32 = ctypes.windll.user32
    handle = ctypes.c_void_p(int(hwnd))
    if not hwnd or not user32.IsWindow(handle):
        return False
    user32.ShowWindow(handle, 9)
    user32.ShowWindow(handle, 5)
    user32.SetWindowPos(handle, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0013)
    user32.BringWindowToTop(handle)
    foreground = user32.GetForegroundWindow()
    this_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    other_tid = user32.GetWindowThreadProcessId(foreground, None)
    attached = False
    if other_tid and other_tid != this_tid:
        attached = bool(user32.AttachThreadInput(this_tid, other_tid, True))
    user32.SetForegroundWindow(handle)
    if attached:
        user32.AttachThreadInput(this_tid, other_tid, False)
    return True


def window_title(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(ctypes.c_void_p(int(hwnd)), buf, 512)
    return buf.value


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


def widget_windows():
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        title = window_title(hwnd)
        if title == 'AI Usage' or title.startswith('AI Usage —'):
            found.append(int(hwnd))
        return True

    ctypes.windll.user32.EnumWindows(callback, 0)
    return found


def activate_existing():
    pid = hwnd = 0
    try:
        pid, hwnd = read_lock()
    except (OSError, ValueError, IndexError):
        pass
    if show_window(hwnd):
        return True
    if pid:
        for other in windows_for_pid(pid):
            if show_window(other):
                return True
    for other in widget_windows():
        if show_window(other):
            return True
    return False


def clear_stale_lock():
    pid = 0
    try:
        pid, _ = read_lock()
    except (OSError, ValueError, IndexError):
        pid = 0
    if process_alive(pid):
        return False
    removed = False
    for path in (lock_path(), instance_path()):
        try:
            path.unlink()
            removed = True
        except OSError:
            pass
    return removed


def recover_busy_lock():
    for _ in range(5):
        if activate_existing():
            return 'activated'
        time.sleep(0.15)
    pid = 0
    try:
        pid, _ = read_lock()
    except (OSError, ValueError, IndexError):
        pid = 0
    if pid and process_alive(pid):
        log_launch(f'replacing hung instance {pid}')
        terminate_pid(pid)
        for _ in range(10):
            if not process_alive(pid):
                break
            time.sleep(0.1)
    for path in (lock_path(), instance_path()):
        try:
            path.unlink()
        except OSError:
            pass
    return 'cleared'


class Instance:
    """Single-instance lock. Window identity lives in an unlocked sidecar file."""
    def __init__(self):
        self.handle = None

    def claim(self, retry=True):
        import msvcrt
        APP_DIR.mkdir(parents=True, exist_ok=True)
        path = lock_path()
        f = open(path, 'a+', encoding='ascii')
        if path.stat().st_size == 0:
            f.write('0'); f.flush()
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            f.close()
            log_launch('lock busy')
            recovered = recover_busy_lock()
            log_launch('lock recover ' + recovered)
            if recovered == 'activated':
                return False
            if retry:
                return self.claim(False)
            return False
        self.handle = f
        write_instance(os.getpid(), 0)
        return True

    def identify(self, hwnd):
        write_instance(os.getpid(), hwnd)

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


class BarShimmer:
    """Thickness and sweep follow activity state, not a fixed hold timer.

    Frames come from the window's FrameClock: 60 fps while the thickness or a
    length is moving, 30 fps while only the light sweeps. Every position is
    computed from time.monotonic(), so a skipped frame costs smoothness only.
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
        self._desired_active = bool(active)
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

    def _shimmer_live(self):
        try:
            return bool(self.winfo_ismapped()) and self.animate and self._shimmer_ready()
        except tk.TclError:
            return False

    def _frame_interval(self):
        if self._tweening():
            return FRAME_FAST
        if not self._shimmer_live():
            return None
        if self._emphasis != (1.0 if self._active else 0.0):
            return FRAME_FAST
        if self._active or self._sweep_t0 is not None:
            return FRAME_SLOW
        return None

    def _frame(self, now):
        """One frame: move the length, then the light, then draw once."""
        tweening = self._tweening()
        if tweening:
            self._advance_tween(now)
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
        return 0.15 + 0.60 * progress

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

    def _shimmer_tick(self):
        """Draw one frame now. The clock calls _frame itself."""
        self._frame(time.monotonic())
        self._sync_frames()


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
            if do_anim and self._seeded:
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

    def _anim_tick(self):
        """Move the length to now and draw. The clock calls _frame itself."""
        if not self._tweening():
            return
        self._advance_tween(time.monotonic())
        try:
            if self.winfo_exists():
                self._paint_shimmer()
        except tk.TclError:
            return
        self._sync_frames()

    def observe_usage(self, snap):
        self._usage_snapshot = snap
        self.tip_text = compact_tooltip(snap)
        warning = compact_warning(snap)
        color = None if warning is None else (WARN if remaining_band(warning.remaining_percent) == 'warn' else DANGER)
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
        if not self._warning_color:
            self.fill_width = chip_fill_width(width, self.percent)
            # Fill is clipped to the track so the leading cap cannot bulge outside.
            return progress_photo(width, height, shape_height / 2, self.fill_width, CHIP_TRACK, self.fill, BG,
                                  shimmer=shimmer, shape_height=shape_height)
        ring_w = self.metrics.p(2, 1)
        self.fill_width = chip_fill_width(max(1, width - 2 * ring_w), self.percent)
        w, h, rows = ringed_progress_rgba(width, height, shape_height, ring_w, self._warning_color,
                                          self.fill_width, CHIP_TRACK, self.fill, BG,
                                          shimmer=shimmer, glow=SHIMMER_GLOW)
        return tk.PhotoImage(data=_png_rgba(w, h, rows, level=1), format='png')

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


@lru_cache(maxsize=12)
def ring_geometry(size):
    radius, center = size * 35 / 84, size / 2
    points = []
    for y in range(size):
        for x in range(size):
            dx,dy=x+.5-center,y+.5-center
            distance=abs(math.hypot(dx,dy)-radius)
            if distance < size * .075:
                points.append((y,x*4,distance,(math.atan2(dy,dx)+math.pi/2)%math.tau,dx,dy))
    return points


def ring_photo(size, percent, color, thickness, background=CARD):
    return tk.PhotoImage(data=ring_png(max(1, int(size)), percent, color, thickness, background), format='png')


@lru_cache(maxsize=48)
def ring_png(size, percent, color, thickness, background=CARD):
    """A drawn ring costs several milliseconds; the same ring is reused."""
    radius, half = size*35/84, thickness/2
    fraction = max(0,min(100,percent))/100
    end = fraction*math.tau
    ex,ey=math.sin(end)*radius,-math.cos(end)*radius
    bg,track,fg=_hex_rgb(background),_hex_rgb(TRACK),_hex_rgb(color)
    track_palette=[bytes([round(b+(t-b)*i/255) for b,t in zip(bg,track)]+[255]) for i in range(256)]
    fill_palette=[bytes([round(b+(f-b)*i/255) for b,f in zip(bg,fg)]+[255]) for i in range(256)]
    rows=[bytearray(bytes(bg)+b'\xff')*size for _ in range(size)]
    for y,x,distance,angle,dx,dy in ring_geometry(size):
        cov=max(0,min(255,int((half+.5-distance)*255)))
        if not cov:
            continue
        if fraction>=1 or 0<fraction and angle<=end:
            pixel=fill_palette[cov]
        else:
            cap=min(math.hypot(dx,dy+radius),math.hypot(dx-ex,dy-ey)) if fraction>0 else size
            arc=max(0,min(255,int((half+.5-cap)*255)))
            if arc:
                base=track_palette[cov]
                pixel=bytes([round(base[i]+(fg[i]-base[i])*arc/255) for i in range(3)]+[255])
            else:
                pixel=track_palette[cov]
        rows[y][x:x+4]=pixel
    return _png_rgba(size,size,rows)


class Card(BarShimmer, tk.Frame):
    """Explicit pixel layout matching the supplied 334px-wide card references."""
    animate = True

    def __init__(self,parent,key,metrics=None,on_additional=None,on_toggle=None,on_retry=None,on_login=None):
        self.metrics = metrics or Metrics()
        self.collapsed = False
        self.on_toggle = on_toggle
        self.on_retry = on_retry
        self.on_login = on_login
        self._actions = []
        m = self.metrics
        super().__init__(parent,width=m.card_w,height=m.p(120),bg=BG)
        self.key, self.height, self.last_signature = key,m.p(120),None
        self.hero_height = m.p(120)
        self._service_icon = load_service_icon(key, m.scale)
        self._bar_photos = []
        self._bar_origins = []
        self._ring_photo = None
        self._hero_shown = self._hero_target = 0.0
        self._clock_second = None
        self._shown_pcts = []
        self._anim_to = []
        self._anim_t0 = None
        self._snap = None
        self._additional_expanded = False
        self._additional_max_body = 0
        self.rows = tk.Canvas(self,width=m.card_w,height=self.height,bg=BG,bd=0,highlightthickness=0,cursor='hand2')
        self.rows.pack()
        self.rows.bind('<Button-1>', self._clicked)
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

    def _clicked(self, event):
        x = getattr(event, 'x', None)
        action = '' if x is None else self._action_at(x, event.y)
        if action == 'retry' and self.on_retry:
            self.on_retry()
            return
        if action == 'login' and self.on_login:
            self.on_login()
            return
        if event.y < self.metrics.p(54) and self.on_toggle:
            self._toggle_card()
        else:
            webbrowser.open(URLS.get(self.key, ''))

    def _toggle_card(self, event=None):
        if self.on_toggle:
            self.on_toggle()
        return 'break'

    def set_collapsed(self, collapsed):
        if self.collapsed == bool(collapsed):
            return
        self.collapsed = bool(collapsed)
        self.last_signature = None
        if self._snap is not None:
            self.render(self._snap)

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
            return
        self.last_signature = signature
        targets = [bar_display_percent(item.remaining_percent) for item in limits]
        self._hero_target = bar_display_percent(representative_percent(snap))
        if self.animate and snap.ok and self._shown_pcts and len(self._shown_pcts) == len(targets):
            self._anim_to = targets
            if self._anim_t0 is None:
                self._anim_t0 = time.monotonic()
            self._arm_anim()
        else:
            self._cancel_anim()
            self._shown_pcts = targets
            self._hero_shown = self._hero_target
        self._paint(snap, self._shown_pcts)

    def _arm_anim(self):
        self._sync_frames()

    def _cancel_anim(self, event=None):
        self._anim_to = []
        self._anim_t0 = None
        self._sync_frames()

    def _tweening(self):
        return bool(self._anim_to) and self._anim_t0 is not None

    def _advance_tween(self, now):
        dt = now - self._anim_t0
        self._anim_t0 = now
        self._shown_pcts = [follow_bar(a, b, dt) for a, b in zip(self._shown_pcts, self._anim_to)]
        self._hero_shown = follow_bar(self._hero_shown, self._hero_target, dt)
        if self._shown_pcts == self._anim_to and self._hero_shown == self._hero_target:
            self._anim_to = []
            self._anim_t0 = None

    def _anim_tick(self):
        """Move the lengths to now and draw. The clock calls _frame itself."""
        if not self._tweening():
            return
        self._advance_tween(time.monotonic())
        if self._snap is not None:
            try:
                if self.winfo_exists():
                    self._paint_shimmer()
            except tk.TclError:
                return
        self._sync_frames()

    def _shimmer_ready(self):
        return (not self.collapsed and self._snap is not None and self._snap.ok and not self._snap.stale
                and any(percent > 0 for percent in self._shown_pcts))

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
        thickness = round((m.p(7) + m.p(2) * emphasis) * 2) / 2
        color = blend(color, '#FFFFFF', .24 * emphasis)
        ring_pct = 0 if actual is None else round(shown * 2) / 2
        signature = (m.p(84), ring_pct, color, thickness)
        if getattr(self,'_ring_signature',None) != signature:
            self._ring_photo = ring_photo(*signature)
            self._ring_signature = signature
        self.rows.itemconfigure('ring', image=self._ring_photo)
        self.rows.itemconfigure('hero',text='—' if actual is None else f'{shown:.0f}%',fill=color)

    def _paint_shimmer(self):
        if self._snap is None or not self.rows.find_withtag('ring'):
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
                color = blend(CARD,color,.7)
            height = self._bar_height_for(index)
            raster = m.bar_h + m.p(4) + 2
            photo = progress_photo(m.card_w-m.p(32),raster,height/2,
                chip_fill_width(m.card_w-m.p(32),shown),TRACK,color,CARD,
                shimmer=self._shimmer_phase_for(index),shape_height=height)
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

    def refresh_clock(self, now=None):
        now = time.time() if now is None else now
        if self._snap is None or int(now) == self._clock_second:
            return
        self._clock_second = int(now)
        if self.rows.find_withtag('service_status'):
            self.rows.itemconfigure('service_status', text=service_status_text(self._snap, now))
        if self.collapsed:
            return
        # Countdown granularity follows the hero's measured window, not its label.
        hero = select_hero(self._snap) if self._snap.ok else None
        prefer_days = hero is None or not window_matches(hero, FIVE_HOURS, FIVE_HOUR_TOLERANCE)
        self.rows.itemconfigure('countdown',text=reset_countdown(self._reset_epoch,now,prefer_days))
        for canvas_id, reset_at in self._secondary_clocks.values():
            days = max(0,int((reset_at-now)//86400))
            self.rows.itemconfigure(canvas_id,text=f'{days}일 남음' if days else reset_countdown(reset_at,now))

    def _paint(self,snap,percents):
        m, c = self.metrics, self.rows
        c.delete('all')
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
        text(46,18,TITLES[self.key],FONT_SERVICE)
        if self.on_toggle:
            # Same colour as the header's line icons, centred on the service icon row.
            self._chevron = tk.PhotoImage(data=chevron_png(m.p(11, 8), not self.collapsed, ICON, max(1.5, 1.6*m.scale)),
                                          format='png')
            c.create_image(m.p(8), m.p(27), image=self._chevron, tags='collapse_toggle')
        plan = '' if snap.plan=='-' else snap.plan.upper()
        font = tkfont.Font(root=c,font=m.font(FONT_PLAN))
        plan_w = font.measure(plan)+m.p(14) if plan else 0
        right = m.card_w-m.p(16)
        if plan:
            round_rect(c,right-plan_w,m.p(16),right,m.p(38),m.p(4),TRACK)
            c.create_text(right-plan_w/2,m.p(27),text=plan,font=m.font(FONT_PLAN),fill=MUTED)
        badge_w = tkfont.Font(root=c,font=m.font(FONT_BADGE)).measure(label)+m.p(25)
        right -= plan_w+m.p(8)
        round_rect(c,right-badge_w,m.p(16),right,m.p(38),m.p(11),blend(CARD,state,.15))
        c.create_oval(right-badge_w+m.p(8),m.p(25),right-badge_w+m.p(13),m.p(30),fill=state,outline='')
        c.create_text(right-badge_w+m.p(18),m.p(27),text=label,anchor='w',font=m.font(FONT_BADGE),fill=blend(state,TEXT,.4),tags='severity')
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
            raster = m.bar_h+m.p(4)+2
            self._bar_origins[index] = (m.p(16),bar_y)
            c.create_image(m.p(16),bar_y-(raster-m.bar_h)/2,anchor='nw',tags='bar_'+str(index))
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
        self._clock_second=None
        self.refresh_clock()


class UsageWidget:
    def __init__(self, preview=False):
        self.preview = preview
        self.settings = read_json(SETTINGS_PATH)
        self.scale = clamp_scale(self.settings.get('scale', DEFAULT_SCALE))
        self.metrics = Metrics(self.scale)
        self.root = tk.Tk()
        if not preview:
            self.callback_errors = CallbackErrors()
            self.root.report_callback_exception = self.callback_errors.report
        self.root.title('AI Usage' if not preview else 'AI Usage — Preview')
        self.root.configure(bg=BG)
        app_icon = ICON_DIR / 'app.ico'
        if app_icon.is_file():
            self.root.iconbitmap(default=str(app_icon))
        self.root.resizable(False, False)
        self.root.overrideredirect(not preview)
        self.topmost = tk.BooleanVar(value=bool(self.settings.get('topmost', True)))
        self.root.attributes('-topmost', self.topmost.get())
        self.compact = bool(self.settings.get('compact', False))
        saved_collapsed = self.settings.get('collapsed', {})
        self.collapsed = saved_collapsed if isinstance(saved_collapsed, dict) else {}
        self.closing = False
        # GPT asks the widget's own Codex app-server; no token is read here.
        self.runner = PollRunner(inprocess={
            'chatgpt': CodexJob(CodexAppServer(client_version=APP_VERSION)),
            # Cursor's login stays inside this worker, which lives across polls.
            'cursor': WorkerJob('cursor'),
        })
        self.watcher = AuthWatcher()
        self.codex_activity = CodexActivityMonitor()
        self.cursor_activity = CursorActivityMonitor()
        self.claude_activity = ClaudeActivityMonitor()
        self.request_started = dict.fromkeys(FETCHERS, float('-inf'))
        self.poll_pending = dict.fromkeys(FETCHERS, False)
        self._ui_active = dict.fromkeys(FETCHERS, False)
        if not preview and not LOG.handlers:
            try:
                APP_DIR.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(APP_DIR / 'activity-debug.log', maxBytes=262144, backupCount=1, encoding='utf-8')
                handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
                LOG.addHandler(handler)
                LOG.setLevel(logging.DEBUG)
            except OSError:
                pass
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
        self.claude_cli_snapshot = None
        self.claude_cli_error = None
        self.claude_cli_at = float('-inf')
        self.claude_cli_due = 0.0
        self.additional_open = None
        self.failures = dict.fromkeys(FETCHERS, 0)
        self.due = dict.fromkeys(FETCHERS, 0.0)
        self.usage_until = dict.fromkeys(FETCHERS, 0.0)
        self.last_save = 0
        self.cache_signature = ''
        self.timer = None
        self.activity_timer = None
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
        if not preview:
            try:
                save_install_root()
            except (OSError, RuntimeError):
                pass
            sync_startup()
        if should_setup(self.settings, self.preview):
            self.pick_services()
        try:
            from claude_integration import ensure_bridge_copy
            ensure_bridge_copy()
        except Exception:
            pass
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
        self._activity_tick()

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
                              font=(FACE, max(8, int(round(11 * self.metrics.scale)))), cursor='hand2')
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
        self.updated_label = tk.Label(self.header,text='',bg=BG,fg=DIM,bd=0)
        self.live_dot = tk.Canvas(self.header,width=6,height=6,bg=BG,bd=0,highlightthickness=0)
        self.update_pill = UpdatePill(self.header, self.install_update, m, tip=self.tip)
        self.header_buttons = []
        for name,callback in (('refresh',self.refresh),('minus',self.toggle),('close',self.close)):
            self.header_buttons.append(self.icon_button(self.header,name,callback,CLOSE_HOVER if name == 'close' else HOVER))
        self.body_view = tk.Canvas(self.shell,bg=BG,bd=0,highlightthickness=0,
                                   yscrollincrement=m.p(24),takefocus=True)
        self.body = tk.Frame(self.body_view,bg=BG,padx=0)
        self._body_window = self.body_view.create_window(0,0,window=self.body,anchor='nw',width=m.card_w)
        self.body_scroll = tk.Scrollbar(self.shell,orient='vertical',command=self.body_view.yview)
        self.body_view.configure(yscrollcommand=self.body_scroll.set)
        self.root.bind_all('<MouseWheel>', self._scroll_body)
        self.root.bind_all('<Next>', lambda e: self._scroll_page(1, e))
        self.root.bind_all('<Prior>', lambda e: self._scroll_page(-1, e))
        self.cards = {k: Card(
            self.body, k, m,
            on_additional=lambda key=k: self.toggle_additional(key),
            on_toggle=lambda key=k: self.toggle_card(key),
            on_retry=lambda key=k: self.retry_provider(key),
            on_login=lambda key=k: self.show_login_help(key),
        ) for k in FETCHERS}
        for key, card in self.cards.items():
            card.set_collapsed(self.collapsed.get(key, False))
        self.footer = tk.Frame(self.shell,bg=BG,height=m.footer_h)
        tk.Frame(self.footer,bg=HAIR,height=1).place(x=0,y=0,relwidth=1,height=1)
        self.footer_dot = tk.Canvas(self.footer,width=m.p(6),height=m.p(6),bg=BG,highlightthickness=0,bd=0)
        self.footer_text = tk.Label(self.footer,bg=BG,fg=MUTED,font=m.font(FONT_FOOT),bd=0,padx=0,pady=0)
        self.footer_sep = tk.Label(self.footer,text='·',bg=BG,fg=DIM,font=m.font(FONT_FOOT),bd=0,padx=0,pady=0)
        self.footer_hint = tk.Label(self.footer,text='F5 새로고침',bg=BG,fg=DIM,font=m.font(FONT_FOOT),bd=0,padx=0,pady=0)
        self.footer_hint.bind('<Button-1>',lambda e:self.refresh())
        self.footer_hint.configure(cursor='hand2')
        self.footer_text.bind('<Enter>', lambda e: self.tip.show(self.footer_text, self._poll_hint()))
        self.footer_text.bind('<Leave>', lambda e: self.tip.hide())
        self.status = self.footer_text
        self.mini = tk.Frame(self.shell,bg=BG,height=max(1, m.compact_h-2))
        self.mini_title = tk.Label(self.mini,text='AI Usage',bg=BG,fg=TEXT,font=m.font(FONT_TITLE),bd=0,padx=0,pady=0)
        self.mini_pill = UpdatePill(self.mini, self.install_update, m, tip=self.tip)
        self.mini_values = {}
        for key in FETCHERS:
            chip = Chip(self.mini, m)
            self.mini_values[key] = chip
            self.bind_drag(chip)
            chip.bind('<Enter>', lambda e, c=chip: self.tip.schedule(c, c.tip_text))
            chip.bind('<Leave>', lambda e: self.tip.hide())
            chip.bind('<Unmap>', lambda e: self.tip.hide(), add='+')
        self.mini_buttons = []
        for name,callback in (('refresh',self.refresh),('expand',self.toggle),('close',self.close)):
            self.mini_buttons.append(self.icon_button(self.mini,name,callback,CLOSE_HOVER if name == 'close' else HOVER))
        self.apply_metrics()
        for widget in (self.title,self.header,self.updated_label,self.mini_title,self.mini):
            self.bind_drag(widget)
        style = dict(tearoff=False, bg=CARD, fg=TEXT, activebackground=HAIR, activeforeground=TEXT, disabledforeground=DIM, selectcolor='#FFFFFF')
        # Only what has no button or always-visible control lives here; refresh,
        # compact mode and the update arrow are already on the widget itself.
        self.menu = tk.Menu(self.root, **style)
        size = tk.Menu(self.menu, **style)
        size.add_command(label='더 크게', accelerator='Ctrl++', command=lambda: self.nudge_scale(1))
        size.add_command(label='더 작게', accelerator='Ctrl+-', command=lambda: self.nudge_scale(-1))
        size.add_command(label='기본 크기', accelerator='Ctrl+0', command=lambda: self.set_scale(DEFAULT_SCALE))
        self.menu.add_cascade(label='크기', menu=size)
        self.menu.add_separator()
        self.menu.add_command(label='서비스·로그인 관리...', command=self.pick_services)
        self.usage_pages = tk.Menu(self.menu, **style)
        self.menu.add_cascade(label='사용량 페이지 열기', menu=self.usage_pages)
        self._fill_usage_pages()
        self.menu.add_separator()
        self.menu.add_checkbutton(label='항상 위', variable=self.topmost, command=self.set_topmost)
        self.startup = tk.BooleanVar(value=startup_path().exists())
        self.menu.add_checkbutton(label='Windows 시작 시 실행', variable=self.startup, command=self.toggle_startup)
        self.menu.add_checkbutton(label='한도 임박·소진 알림', variable=self.notifications, command=self.toggle_notifications)
        self.menu.add_separator()
        self.menu.add_command(label=update_menu_label(None), command=self.check_update_now)
        self._update_menu = self.menu.index('end')
        self.menu.add_command(label='도움말 · 표시 기준...', command=self.help)
        self.menu.add_command(label='바탕화면 바로가기 만들기', command=self.make_desktop_shortcut)
        self.menu.add_separator()
        self.menu.add_command(label='종료', command=self.close)
        self.menu.bind('<Map>', lambda e: self._on_menu_map())

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
        if self._menu_held:
            # A context menu is posted. Re-stacking the widget here is what
            # used to drop the menu behind it, so keep only the topmost style
            # bit and put the menu back on top instead.
            keep_topmost_style(self._widget_hwnd(), True)
            self._raise_open_menus()
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
        if kwargs.get('parent') is self.root:
            kwargs['parent'] = self.screen_center_owner()
        self.push_overlay()
        try:
            return fn(*args, **kwargs)
        finally:
            self.pop_overlay()

    def screen_center_owner(self):
        """Hidden owner so Windows message boxes center on the monitor, not the widget."""
        owner = getattr(self, '_center_owner', None)
        try:
            alive = owner is not None and owner.winfo_exists()
        except tk.TclError:
            alive = False
        if not alive:
            owner = tk.Toplevel(self.root)
            owner.withdraw()
            owner.overrideredirect(True)
            try:
                owner.attributes('-topmost', True)
            except tk.TclError:
                pass
            self._center_owner = owner
        try:
            ref_x, ref_y = self.root.winfo_rootx(), self.root.winfo_rooty()
        except tk.TclError:
            ref_x, ref_y = 0, 0
        x, y = center_box(1, 1, ref_x, ref_y)
        try:
            owner.geometry(f'1x1{geometry_at(x, y)}')
            owner.update_idletasks()
        except tk.TclError:
            pass
        return owner

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
        # winfo_ismapped() is always false for a Tk menu on Windows, where the
        # popup is a native window, so the id is passed whenever the menu is
        # held open rather than only when Tk claims it is mapped.
        extra = 0
        if self._menu_held:
            try:
                extra = int(self.menu.winfo_id())
            except (TypeError, ValueError, tk.TclError):
                extra = 0
        lift_menu_windows(extra)

    def _lift_menu(self):
        if not self._menu_held or self.closing:
            return
        self._raise_open_menus()

    def _on_menu_map(self):
        self._lift_menu()

    def _fill_usage_pages(self):
        pages = self.usage_pages
        pages.delete(0, 'end')
        shown = [key for key in FETCHERS if self.enabled[key].get()]
        for key in shown:
            pages.add_command(label=TITLES[key], command=lambda k=key: webbrowser.open(URLS[k]))
        if not shown:
            pages.add_command(label='켜 둔 서비스 없음', state='disabled')

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
        """Clear the held state however the menu was dismissed.

        tk_popup only returns once the popup has gone, so reaching here means
        the menu is closed: by a command, a click outside, Escape, another
        window taking focus, or shutdown. The state must never survive it.
        """
        if self._menu_held:
            self._menu_held = False
            if not self._overlay and not self.closing:
                self.apply_topmost()

    def help_text(self):
        return (
            f'현재 버전 {APP_VERSION}\n\n'
            '이 위젯은 OpenAI(ChatGPT)·Cursor·Anthropic과 제휴되지 않은 비공식 도구입니다.\n'
            '사용량 조회는 언제든 실패하거나 바뀔 수 있습니다.\n\n'
            'GPT: 실제 한도 기간으로 구분하며, 5시간이 있으면 우선 표시하고 없으면 주간·기타 한도를 표시합니다.\n'
            'Cursor: Cursor Models를 대표 잔여로 표시하고 Other Models를 보조 바로 표시합니다. 막대 아래는 청구 주기 초기화입니다.\n'
            'Claude: Claude.ai 구독과 지원되는 Claude Code가 필요합니다. 대화형 세션의 5시간·주간 한도만 표시하며 Additional/Billing은 없습니다. 연동은 우클릭 → 서비스·로그인 관리에서 켭니다. claude -p는 추적되지 않습니다.\n'
            '기본 포함량 소진과 전체 한도 소진은 다를 수 있습니다.\n\n'
            '한 줄 칩은 카드와 같은 색입니다. 50% 미만은 주의, 20% 미만은 임박, 5% 미만은 곧 한도입니다.\n'
            '우클릭 → 서비스·로그인 관리에서 GPT / Cursor / Claude를 고르고 로그인합니다.\n'
            '계정 로그인은 각 서비스에서 하세요. 위젯은 읽기만 합니다.\n'
            'GPT 사용량은 ChatGPT 데스크톱 앱이 아니라 Codex CLI 로그인이 필요합니다.\n\n'
            '실행은 zip 푼 폴더의 AI Usage.exe 입니다. 한 번 실행한 뒤에는 실행 파일만 옮겨도 됩니다.\n'
            '우클릭 → 바탕화면 바로가기 만들기로 바로가기를 만들 수 있습니다.\n\n'
            'F5 새로고침 · Ctrl+M 한 줄 모드\n'
            '서비스 제목 클릭 또는 카드에서 Enter/Space: 개별 접기·펼치기\n'
            '긴 본문: 마우스 휠 · 스크롤바 · PageUp/PageDown\n'
            '카드마다 마지막 확인 시각이 표시됩니다. 조회가 실패하면 이전 값과 원인이 남고, 다시 확인·로그인 안내가 나타납니다.\n'
            'Ctrl++ / Ctrl+- 크기 조절 · Ctrl+0 기본 크기\n'
            '제목 드래그로 이동 · 제목 더블클릭으로 한 줄/상세 전환 · 우클릭으로 설정\n\n'
            '10% 이하·소진 시 한 번 알림 (12% 초과 회복 시 재설정). 알림을 켜면 예시 알림이 한 번 뜹니다.\n'
            '로그인 파일 변경 자동 감지 · 조회 제한 15초\n'
            '잠금 중 조회 중지 · 해제 시 즉시 조회\n'
            '새 버전이 있으면 제목 옆에 초록 ↑ 업데이트 버튼이 나타납니다.'
        )

    def help(self):
        self.notify(messagebox.showinfo, 'AI Usage', self.help_text(), parent=self.root)

    def _service_setup_action(self, action):
        if str(action).startswith('claude'):
            self._claude_integration_action(action)
            return
        start_tool_setup(action)

    def _claude_integration_action(self, action):
        from claude_integration import install_statusline, uninstall_statusline
        try:
            if action == 'claude-login':
                from claude_integration import start_claude_login
                start_claude_login()
                self.enabled['claude'].set(True)
                self.claude_cli_due = 0.0
                self.due['claude'] = 0
                self.persist()
                self.apply_mode()
                return
            if action == 'claude-conflict':
                self.notify(
                    messagebox.showinfo,
                    'Claude 연동',
                    'Claude Code statusLine이 설치 이후 변경되어 자동으로 되돌리지 않습니다.\n'
                    '원본을 복구하려면 사용자가 직접 ~/.claude/settings.json의 statusLine을 확인하세요.',
                    parent=self.root,
                )
                return
            if action == 'claude-uninstall':
                if not self.notify(messagebox.askyesno, 'Claude 연동 해제',
                                   'statusLine wrapper를 제거하고 가능한 경우 원본을 복구할까요?',
                                   parent=self.root):
                    return
                result = uninstall_statusline()
                messages = {
                    'restored': '원본 statusLine을 복구했습니다.',
                    'removed': 'Claude statusLine 연동을 제거했습니다.',
                    'conflict': '사용자가 statusLine을 바꿔 자동 원복하지 않았습니다.',
                    'absent': '제거할 연동이 없습니다.',
                }
                self.notify(messagebox.showinfo, 'Claude 연동', messages.get(result, result), parent=self.root)
                return
            if not self.notify(
                messagebox.askyesno,
                'Claude 연동',
                '대화형 Claude Code의 공식 statusLine으로 5시간·주간 한도를 읽습니다.\n'
                '기존 statusLine이 있으면 덮어쓰지 않고 tee wrapper로 전달합니다.\n'
                '지금 연동할까요?',
                parent=self.root,
            ):
                return
            install_statusline()
            self.enabled['claude'].set(True)
            self.due['claude'] = 0
            self.persist()
            self.apply_mode()
            self.notify(
                messagebox.showinfo,
                'Claude 연동',
                '연동했습니다. 대화형 Claude Code 세션을 열면 사용량이 나타납니다.',
                parent=self.root,
            )
        except Exception as exc:
            self.notify(messagebox.showerror, 'Claude 연동', str(exc) or '연동에 실패했습니다.', parent=self.root)

    def make_desktop_shortcut(self):
        try:
            path = create_desktop_shortcut()
        except Exception as exc:
            self.notify(messagebox.showerror, 'AI Usage', str(exc) or '바탕화면 바로가기를 만들지 못했습니다.', parent=self.root)
            return
        self.notify(messagebox.showinfo, 'AI Usage', '바탕화면에 바로가기를 만들었습니다.\n' + str(path), parent=self.root)

    def pick_services(self):
        self.push_overlay()
        dialog = tk.Toplevel(self.root)
        dialog.title('서비스·로그인 관리')
        dialog.configure(bg=BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.attributes('-topmost', True)
        chosen = {key: tk.BooleanVar(value=self.enabled[key].get()) for key in FETCHERS}
        tk.Label(dialog, text='이 PC에서 볼 서비스를 고르세요.', bg=BG, fg=TEXT, font=FONT_TITLE).pack(anchor='w', padx=16, pady=(14, 6))
        tk.Label(
            dialog,
            text='GPT는 Codex CLI, Cursor는 Cursor 앱 로그인이 필요합니다. Claude는 대화형 Claude Code 연동이 필요합니다.',
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
            controls = tk.Frame(block, bg=BG)
            controls.pack(anchor='w', pady=(4, 0))
            button = tk.Button(
                controls, text=label, bg=CARD, fg=TEXT, bd=0, padx=10, pady=3, cursor='hand2',
                command=lambda a=action: self._service_setup_action(a),
            )
            button.pack(side='left')
            actions[key] = button
            if key == 'claude':
                def claude_login():
                    # The login turns Claude on; keep the dialog from turning it back off.
                    chosen['claude'].set(True)
                    self._claude_integration_action('claude-login')
                tk.Button(
                    controls, text='Claude 로그인', bg=CARD, fg=TEXT, bd=0, padx=10, pady=3, cursor='hand2',
                    command=claude_login,
                ).pack(side='left', padx=(6, 0))
        tk.Label(
            dialog,
            text='Claude 연동은 ~/.claude/settings.json을 자동으로 바꾸지 않습니다. 연동 버튼을 눌렀을 때만 statusLine wrapper를 설치합니다.',
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
                actions[key].configure(text=label, command=lambda a=action: self._service_setup_action(a))
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
                lines.append('GPT: ChatGPT 데스크톱이 아니라 Codex CLI가 필요합니다.')
            if need_cursor:
                lines.append('Cursor: Cursor 앱에서 로그인해야 합니다.')
            if self.notify(messagebox.askyesno, '로그인 준비', '\n'.join(lines), parent=self.root):
                start_tool_setup('prepare', codex=need_codex, cursor=need_cursor)

        def commit():
            if not any(item.get() for item in chosen.values()):
                messagebox.showinfo('서비스·로그인 관리', '하나 이상 선택하세요.', parent=dialog)
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
                    # A service that is off keeps no reader process running.
                    self.runner.reset(key)
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
        try:
            ref_x, ref_y = self.root.winfo_rootx(), self.root.winfo_rooty()
        except tk.TclError:
            ref_x, ref_y = 0, 0
        place_on_screen_center(dialog, ref_x, ref_y)
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
        self.body.configure(padx=0)
        self.title.place(x=m.p(28), y=0, height=m.header_h)
        self.live_dot.place(x=m.p(14),y=m.p(16),width=m.p(6),height=m.p(6))
        self.updated_label.configure(font=m.font((FACE,-10)))
        self.updated_label.place(x=m.p(94),y=0,height=m.header_h)
        fallback_font = (FACE, max(8, int(round(11 * m.scale))))
        for index, btn in enumerate(self.header_buttons):
            if isinstance(btn, IconButton):
                btn.set_size(m.icon)
            else:
                btn.configure(font=fallback_font)
            btn.place(x=m.window_w-m.p(90) + m.p(28) * index, y=m.p(8), width=m.icon, height=m.icon)
        # The compact row places the title itself; apply_mode owns that slot.
        for index, btn in enumerate(self.mini_buttons):
            if isinstance(btn, IconButton):
                btn.set_size(m.icon)
            else:
                btn.configure(font=fallback_font)
            btn.place(x=m.window_w-m.p(90) + m.p(28) * index, y=m.p(9), width=m.icon, height=m.icon)
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

    def toggle_additional(self, key):
        self.additional_open = None if self.additional_open == key else key
        self.relayout()

    def toggle_card(self, key):
        card = self.cards[key]
        card.set_collapsed(not card.collapsed)
        self.collapsed[key] = card.collapsed
        self.tip.hide()
        self.relayout()
        self.persist()

    def _scroll_page(self, direction, event=None):
        if not self.compact and (event is None or event.widget.winfo_toplevel() is self.root):
            self.body_view.yview_scroll(direction, 'pages')
            return 'break'

    def _scroll_body(self, event):
        if self.compact or not event.delta:
            return
        widget = event.widget
        if any(widget is card.additional.body for card in self.cards.values()):
            return
        while widget is not None:
            if widget is self.body_view:
                self.body_view.yview_scroll(-max(1,abs(event.delta)//120) if event.delta > 0
                                           else max(1,abs(event.delta)//120), 'units')
                self.tip.hide()
                return 'break'
            widget = getattr(widget, 'master', None)

    def relayout(self, x=None, y=None):
        try:
            if x is None:
                x, y = int(self.root.winfo_x()), int(self.root.winfo_y())
        except (tk.TclError, ValueError, TypeError):
            x, y = 40, 80
        work = work_area(x, y)
        self._work_height = work[3] - work[1]
        monitor = monitor_area(x, y)
        visible = [k for k in FETCHERS if self.enabled[k].get()]
        m = self.metrics
        used = 2 + m.header_h + m.footer_h
        for key in visible:
            card = self.cards[key]
            used += getattr(card, 'hero_height', card.height)
            snap = self.snapshots.get(key)
            groups = getattr(snap, 'additional_groups', []) if snap and getattr(snap, 'ok', False) else []
            if additional_count(groups) and not card.collapsed:
                used += m.p(28)
            used += m.card_gap
        remaining = expanded_body_budget(work[3] - work[1], used, m.p)
        for key in FETCHERS:
            self.cards[key].set_additional_layout(self.additional_open == key, remaining)
        self.apply_mode()
        height = int(self.shell.cget('height'))
        x, y = clamp_position(x, y, m.window_w, height, work, monitor)
        self.root.geometry(geometry_at(x, y))
        self.apply_topmost()

    def compact_row(self, visible=None):
        """Title, chip origin, chip width and gap for the compact row."""
        m = self.metrics
        if visible is None:
            visible = [k for k in FETCHERS if self.enabled[k].get()]
        needed = m.chip_w
        try:
            font = tkfont.Font(root=self.root, font=m.font(FONT_CHIP))
            for key in visible:
                # 100% is the widest value a chip ever shows.
                needed = max(needed, font.measure(f'{TITLES[key]} 100%') + 2 * m.p(COMPACT_CHIP_PAD))
        except tk.TclError:
            pass
        controls_left = m.window_w - m.p(90)
        return compact_row_layout(count=len(visible), chip_width=needed,
                                  controls_left=controls_left, scale_px=m.p)

    def apply_mode(self):
        m = self.metrics
        visible = [k for k in FETCHERS if self.enabled[k].get()]
        for key,card in self.cards.items():
            if key not in visible:
                self.mini_values[key].configure(text=TITLES[key]+' 꺼짐',fg=CHIP_FG,bg=CHIP_STALE,percent=0,animate=False)
        body_h = sum(self.cards[k].height for k in visible)+m.card_gap*max(0,len(visible)-1)
        work = work_area(self.root.winfo_x(), self.root.winfo_y())
        work_h = getattr(self, '_work_height', work[3]-work[1])
        chrome_h = 2+m.header_h+m.footer_h
        view_h = min(body_h, max(1, work_h-chrome_h-m.p(12)))
        height = m.compact_h if self.compact else chrome_h+view_h
        layout = (self.compact, tuple(visible), height, tuple(self.cards[k].height for k in visible), m.scale)
        if layout == self._layout:
            return
        self._layout = layout
        for item in (self.header,self.body_view,self.body_scroll,self.footer,self.mini):
            item.place_forget()
        for key,card in self.cards.items():
            card.pack_forget()
            if key in visible:
                card.pack(fill='x',pady=(0,0))
        shown = 0
        mini_h = max(1, m.compact_h - 2)
        title, origin, chip_w, gap = self.compact_row(visible)
        self._compact_row = (title, origin, chip_w, gap)
        for key in FETCHERS:
            chip = self.mini_values[key]
            if key in visible:
                chip.set_width(chip_w)
                chip.place(x=origin+(chip_w+gap)*shown,
                           y=(mini_h-m.chip_canvas_h)//2,
                           width=chip_w,height=m.chip_canvas_h)
                shown += 1
            else:
                # A disabled provider gives its space back to the others.
                chip.place_forget()
        self.mini_title.configure(text=title)
        if title:
            self.mini_title.place(x=m.p(12), y=0, height=mini_h)
        else:
            self.mini_title.place_forget()
        self.shell.configure(width=m.window_w,height=height)
        self.body_view.itemconfigure(self._body_window,width=m.card_w)
        self.body_view.configure(scrollregion=(0,0,m.card_w,body_h),yscrollincrement=m.p(24))
        if body_h <= view_h:
            self.body_view.yview_moveto(0)
        if self.compact:
            self.mini.place(x=1,y=1,width=m.window_w-2,height=max(1, m.compact_h-2),bordermode='outside')
        else:
            self.header.place(x=1,y=1,width=m.window_w-2,height=m.header_h,bordermode='outside')
            self.body_view.place(x=1,y=1+m.header_h,width=m.window_w-2,height=view_h,bordermode='outside')
            if body_h > view_h:
                self.body_scroll.place(x=m.window_w-1-m.p(8),y=1+m.header_h,
                                       width=m.p(8),height=view_h)
                self.body_scroll.lift()
            self.footer.place(x=1,y=height-m.footer_h-1,width=m.window_w-2,height=m.footer_h,bordermode='outside')
        self.root.geometry(f'{m.window_w}x{height}')
        self.root.update_idletasks()
        if not self.preview and height != self._region_h:
            # Region coordinates include the whole frameless window.
            try:
                gdi = ctypes.windll.gdi32
                gdi.CreateRoundRectRgn.restype = ctypes.c_void_p
                hwnd = ctypes.c_void_p(int(self.root.wm_frame(),16))
                region = gdi.CreateRoundRectRgn(0,0,m.window_w+1,height+1,m.p(28),m.p(28))
                if ctypes.windll.user32.SetWindowRgn(hwnd,ctypes.c_void_p(region),True):
                    self._region_h = height
                else:
                    gdi.DeleteObject(ctypes.c_void_p(region))
            except (AttributeError,OSError,ValueError):
                pass
        self.set_update_chrome()

    def toggle(self):
        self.compact = not self.compact
        self.relayout()
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
        self._fill_usage_pages()
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
        self.relayout(self.root.winfo_x(), self.root.winfo_y())
        self.persist()

    def place(self, x, y):
        self.relayout(x, y)

    def persist(self):
        if self.preview:
            return
        try:
            save_json(SETTINGS_PATH, {
                'x': self.root.winfo_x(),
                'y': self.root.winfo_y(),
                'compact': self.compact,
                'collapsed': {k: card.collapsed for k, card in self.cards.items()},
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
                restored = representative_percent(snap)
                if snap.key != key or restored is None or not 0 <= restored <= 100:
                    continue
                if cache.get('version') != 3:
                    continue
                self.snapshots[key] = snap
                self.render(key)
            except (ValueError, TypeError, AttributeError, OverflowError):
                continue

    def refresh(self):
        from claude_integration import invalidate_version_cache, claude_ready
        invalidate_version_cache()
        if self.enabled['claude'].get():
            claude_ready(background=True)
        for key in FETCHERS:
            self.due[key] = 0
        self.claude_cli_due = 0.0
        self.set_footer('새로고침 요청됨', MUTED, CODEX)

    def start_job(self, key):
        if key == 'claude':
            self.start_claude_job()
            return
        try:
            now = time.monotonic()
            previous = self.request_started.get(key, float('-inf'))
            if self.runner.start(key, now):
                self.request_started[key] = now
                self.poll_pending[key] = False
                LOG.debug('[Usage] %s request started', TITLES[key])
                if math.isfinite(previous):
                    LOG.debug('[Usage] %s request interval=%.2fs', TITLES[key], now - previous)
        except OSError:
            self.accept(key, error_snapshot(key, TITLES[key], '조회 프로세스를 시작하지 못했습니다.', URLS[key]))

    def start_claude_job(self):
        """statusLine cache first; fall back to asking Claude Code directly."""
        key = 'claude'
        snap = fetch_claude()
        now = time.monotonic()
        known_plan = claude_plan_label(snap.plan) or claude_plan_label(
            getattr(self.claude_cli_snapshot, "plan", "")
        )
        # A fresh statusLine has quota but not the subscription, so ask once until the plan is known.
        if snap.ok and not snap.stale and known_plan:
            self.claude_cli_due = max(self.claude_cli_due, now + CLAUDE_CLI_INTERVAL)
        else:
            if now >= self.claude_cli_due:
                self.claude_cli_due = now + CLAUDE_CLI_INTERVAL
                try:
                    if self.runner.start(key, now):
                        self.request_started[key] = now
                        LOG.debug('[Usage] Claude usage query started')
                except OSError:
                    LOG.debug('[Usage] Claude usage query could not start')
        self.accept(key, self._claude_display_snapshot(snap, now))

    def _claude_display_snapshot(self, statusline, now):
        """Prefer fresh observations, then the newest; statusLine wins ties."""
        candidates = [statusline] if statusline.ok else []
        previous = self.snapshots.get('claude')
        if not statusline.ok and previous is not None and previous.ok:
            # A missing cache is not a new observation of the previous value.
            if (previous.internal or {}).get('source') == 'claude_statusline':
                candidates.append(replace(previous, stale=True))
        fallback = self.claude_cli_snapshot
        if fallback is not None and fallback.ok:
            candidates.append(replace(
                fallback,
                stale=fallback.stale or now - self.claude_cli_at >= CLAUDE_CLI_STALE,
            ))

        def rank(snap):
            meta = snap.internal or {}
            observed = meta.get('quota_observed_at', snap.fetched_at)
            try:
                observed = float(observed)
            except (TypeError, ValueError):
                observed = 0.0
            if not math.isfinite(observed):
                observed = 0.0
            return (not snap.stale, observed, meta.get('source') == 'claude_statusline')

        chosen = max(candidates, key=rank) if candidates else (self.claude_cli_error or statusline)
        if chosen is None:
            return statusline
        label = ""
        for snap in (chosen, *candidates):
            label = claude_plan_label(getattr(snap, "plan", ""))
            if label:
                break
        if label and chosen.plan != label:
            chosen = replace(chosen, plan=label)
        return chosen

    def _request_fast_poll(self, key, now):
        from polling import policy_for
        if key in self.runner.slots:
            self.poll_pending[key] = True
            return
        started = self.request_started.get(key, float('-inf'))
        self.due[key] = min(self.due[key], next_fast_due(started, now, policy_for(key).active_interval))

    def toggle_provider(self, key):
        self.runner.cancel(key)
        self.due[key] = 0
        self.failures[key] = 0
        if key == 'claude':
            self.claude_cli_due = 0.0
        if self.enabled[key].get() and key in self.snapshots:
            self.snapshots[key] = replace(self.snapshots[key], stale=True)
            self.render(key)
        self.apply_mode()
        self.persist()

    def retry_provider(self, key):
        if key not in FETCHERS or not self.enabled[key].get() or self.closing:
            return
        self.failures[key] = 0
        self.runner.cancel(key)
        self.due[key] = 0
        if key == 'claude':
            self.claude_cli_due = 0.0
        if self.locked or self.preview:
            return
        self.start_job(key)

    def show_login_help(self, key):
        snap = self.snapshots.get(key)
        label, action = login_guidance(key, snap)
        lines = [f'{TITLES.get(key, key)} · {login_status(key)}']
        if snap is not None and snap.error:
            lines.append(snap.error)
        lines.append('')
        lines.append(f'{label} 작업을 진행할까요?')
        if self.notify(messagebox.askyesno, '로그인 안내', '\n'.join(lines), parent=self.root):
            self._service_setup_action(action)

    def toggle_notifications(self):
        self.persist()
        # Turning alerts on shows one right away, so a blocked Windows
        # notification setting is found now rather than at 10%.
        if self.notifications.get() and self.toast and not self.locked:
            self.toast.send('test', 'AI Usage 알림 켜짐', '남은 양이 10% 이하가 되거나 소진되면 이렇게 알려 드립니다.')

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
                    self.runner.cancel(key)
                    # A new login needs a fresh Codex app-server or Cursor worker.
                    self.runner.reset(key)
                    self.due[key] = 0

    def accept(self, key, snap, *, is_new=False):
        now = time.monotonic()
        if key == 'claude' and is_new:
            # Retain actionable worker errors between the 2-second cache reads.
            self.claude_cli_error = None if snap.ok else snap
            # Only an actual successful worker response advances CLI freshness.
            if snap.ok and not snap.stale and (snap.internal or {}).get('source') == 'claude_cli':
                self.claude_cli_snapshot = snap
                self.claude_cli_at = now
            elif self.claude_cli_snapshot is not None:
                self.claude_cli_snapshot = replace(self.claude_cli_snapshot, stale=True)
            selected = self._claude_display_snapshot(fetch_claude(), now)
            if selected.ok:
                snap = selected
        if key == 'claude' or snap.ok:
            self.failures[key] = 0
        else:
            self.failures[key] += 1
        previous = self.snapshots.get(key)
        if not snap.ok and previous and previous.ok:
            snap = replace(
                previous,
                stale=True,
                error=snap.error,
                retry_after=getattr(snap, 'retry_after', ''),
            )
        until = self.usage_until
        if key not in ('chatgpt', 'claude') and snap.ok and usage_dropped(previous, snap):
            until[key] = now + ACTIVE_HOLD
        self.snapshots[key] = snap
        if key == 'claude':
            from claude_bridge import CACHE_READ_INTERVAL
            self.due[key] = now + CACHE_READ_INTERVAL
        elif key == 'chatgpt':
            fast = self.codex_activity.fast(now) and not self.failures[key]
            self._schedule_poll(key, snap, now, active=fast)
            LOG.debug('[Usage] quota raw/display remaining: %s', [(item.quota_id, item.used_percent, item.remaining_percent, round(item.remaining_percent) if item.remaining_percent is not None else None) for item in global_main_limits(snap)])
        else:
            fast = (self.cursor_activity.fast(now) or until.get(key, 0) > now) and not self.failures[key]
            self._schedule_poll(key, snap, now, active=fast)
        self.render(key)
        self._sync_activity_ui(now)
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
                    remaining, label = limiting_quota(snap)
                    title, body = quota_alert_copy(key, severity, remaining, label)
                    self.toast.send(key, title, body)

    def _schedule_poll(self, key, snap, now, *, active):
        if self.failures[key]:
            self.due[key] = now + next_interval(snap, self.failures[key], False)
            return
        if active:
            from polling import policy_for
            started = self.request_started.get(key, float('-inf'))
            self.due[key] = next_fast_due(started, now, policy_for(key).active_interval)
            return
        self.due[key] = now + next_interval(snap, 0, False)

    def _sync_activity_ui(self, now=None):
        cards, chips = self.cards, self.mini_values
        if not cards:
            return
        now = time.monotonic() if now is None else now
        gpt_active = (not self.preview and self.enabled['chatgpt'].get()
                      and self.codex_activity.visual_active(now))
        cursor_active = self.enabled['cursor'].get() and self.cursor_activity.visual_active(now)
        claude_active = self.enabled['claude'].get() and self.claude_activity.visual_active(now)
        states = {'chatgpt': gpt_active, 'cursor': cursor_active, 'claude': claude_active}
        ui_active = self._ui_active
        for key, active in states.items():
            if ui_active.get(key) != active:
                ui_active[key] = active
                name = TITLES[key]
                if active:
                    LOG.debug('[UI] %s bar ACTIVE', name)
                    LOG.debug('[UI] %s shimmer ACTIVE', name)
                else:
                    LOG.debug('[UI] %s bar NORMAL', name)
                    LOG.debug('[UI] %s shimmer STOP', name)
            if key in cards:
                cards[key].set_activity(active)
            if chips and key in chips:
                chips[key].set_activity(active)

    def save_cache(self):
        payload = {k: snapshot_to_dict(v) for k, v in self.snapshots.items() if v.ok and not v.stale}
        for k, v in self.snapshots.items():
            if v.ok and k not in payload:
                payload[k] = snapshot_to_dict(v)
        signature = json.dumps({k: {a: b for a, b in v.items() if a != 'fetched_at'} for k, v in payload.items()}, sort_keys=True)
        if signature == self.cache_signature and time.monotonic() - self.last_save < 300:
            return
        try:
            save_json(CACHE_PATH, dict(payload, version=3))
            self.cache_signature = signature
            self.last_save = time.monotonic()
        except (OSError, ValueError):
            pass

    def render(self, key):
        if not self.enabled[key].get():
            self.mini_values[key].configure(text=TITLES[key] + ' 꺼짐', fg=MUTED, bg=CHIP_STALE, percent=0, animate=False)
            self.apply_mode()
            return
        snap = self.snapshots[key]
        self.cards[key].render(snap)
        self.relayout()
        hero = representative_percent(snap)
        value = '—' if hero is None else f'{hero:.0f}%'
        fill, fg = chip_style(key, snap)
        pct = 0 if hero is None else hero
        self.mini_values[key].configure(text=f'{TITLES[key]} {value}', fg=fg, bg=fill, percent=pct)
        self.mini_values[key].observe_usage(snap)

    def _poll_hint(self):
        now = time.monotonic()
        return '\n'.join(f'{TITLES[k]} · 다음 조회 {max(0,int(self.due[k]-now))}초 후' for k in FETCHERS if self.enabled[k].get())

    def refresh_design_status(self):
        snaps=[s for k,s in self.snapshots.items() if self.enabled[k].get()]
        text, age = header_freshness(snaps)
        self.updated_label.configure(text='' if self.update_info or self._update_busy else '· '+text)
        color=MUTED if age is None or age>60 or any(s.stale for s in snaps) else '#22C55E'
        if any(not s.ok for s in snaps): color=DANGER
        self.live_dot.delete('all')
        d=self.metrics.p(6)
        self.live_dot.create_oval(0,0,d,d,fill=color,outline='')

    def set_footer(self, text, fg, dot):
        if not text or text == '자동 감지':
            text,dot = '자동 감지 중','#22C55E'
        state = (text, dot)
        if state == self._footer_state:
            return
        self._footer_state = state
        self.footer_text.configure(text=text,fg=MUTED)
        m = self.metrics
        self.footer_sep.place_forget()
        self.footer_dot.delete('all')
        d = m.p(6)
        self.footer_dot.create_oval(0,0,d,d,fill=dot,outline='')

    def _activity_tick(self):
        # Activity has its own quarter-second beat so a turn's start and end
        # reach the bars without waiting for the one-second idle tick. Each
        # monitor keeps its own interval: Claude and Codex 0.25 s, Cursor 0.75 s.
        if self.closing:
            return
        try:
            if not self.preview:
                self._poll_activity(time.monotonic())
        finally:
            # One failed beat must not end activity tracking for the session.
            if not self.closing:
                self.activity_timer = self.root.after(ACTIVITY_TICK_MS, self._activity_tick)

    def _poll_activity(self, now):
        """Read the activity monitors, then move quota polling and the bars."""
        was_fast = self.codex_activity.was_fast
        was_cursor = self.cursor_activity.was_fast
        activity, quota_event = self.codex_activity.poll(now)
        cursor_hit = self.cursor_activity.poll(now) if self.enabled['cursor'].get() else False
        if self.enabled['claude'].get():
            self.claude_activity.poll(now)
        if not self.locked and self.enabled['chatgpt'].get():
            fast = self.codex_activity.fast(now)
            if (activity or quota_event) and not self.failures['chatgpt']:
                self._request_fast_poll('chatgpt', now)
            if was_fast and not fast and not self.failures['chatgpt']:
                self.due['chatgpt'] = now + next_interval(self.snapshots.get('chatgpt'), active=False)
        if not self.locked and self.enabled['cursor'].get() and not self.failures['cursor']:
            cursor_fast = self.cursor_activity.fast(now) or self.usage_until.get('cursor', 0) > now
            if cursor_hit:
                self._request_fast_poll('cursor', now)
            elif cursor_fast:
                started = self.request_started.get('cursor', float('-inf'))
                self.due['cursor'] = min(self.due['cursor'], next_fast_due(started, now))
            if was_cursor and not cursor_fast and not cursor_hit:
                self.due['cursor'] = now + next_interval(self.snapshots.get('cursor'), active=False)
        self._sync_activity_ui(now)

    def tick(self):
        if self.closing:
            return
        delay = 1000
        try:
            delay = self._tick_once()
        finally:
            # Scheduled even when this pass raised: a single bad snapshot or
            # Tk error must not freeze polling, the clock and the footer.
            if not self.closing:
                self.timer = self.root.after(delay or 1000, self.tick)

    def _tick_once(self):
        """One pass of the widget clock. Returns the delay to the next pass."""
        self.drain_update_queue()
        if self.closing:
            # Installing an update closed the widget from inside the queue.
            return None
        for card in self.cards.values():
            card.refresh_clock()
        self.refresh_design_status()
        now = time.monotonic()
        self.environment(now)
        if not self.preview:
            self._poll_activity(now)
        completed = []
        for key, snap, error in self.runner.poll(now):
            completed.append(key)
            if not self.locked and self.enabled[key].get():
                self.accept(key, snap or error_snapshot(key, TITLES[key], error, URLS[key]), is_new=True)
        for key in completed:
            if self.poll_pending.get(key):
                self.poll_pending[key] = False
                if not self.locked and self.enabled[key].get() and not self.failures[key]:
                    started = self.request_started.get(key, float('-inf'))
                    self.due[key] = min(self.due[key], next_fast_due(started, now))
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
        elif any(service_state(s) == 'danger' for s in enabled_snaps):
            self.set_footer('사용량 소진', MUTED, DANGER)
        elif any(s.stale for s in enabled_snaps):
            self.set_footer('일부 데이터 이전 기준', MUTED, STALE_STRIP)
        else:
            self.set_footer('자동 감지', MUTED, CODEX)
        heat = (any(until > now for until in self.usage_until.values())
                or self.codex_activity.fast(now) or self.cursor_activity.fast(now))
        return 200 if active or heat else 1000

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
            x = m.p(96)
            width = self.update_pill.show(header_labels, ready, m.p(270) - m.p(10) - x, hint)
            self.update_pill.place(x=x, y=(m.header_h - m.pill_h) // 2, width=width, height=m.pill_h)
        else:
            self.update_pill.hide()
        # Compact row: the pill takes the title slot so it never collides with chips or buttons.
        mini_h = max(1, m.compact_h - 2)
        title, origin, _, _ = getattr(self, '_compact_row', None) or self.compact_row()
        if pending and self.compact and origin > m.p(12):
            x = m.p(12)
            width = self.mini_pill.show(mini_labels, ready, origin - m.p(6) - x, hint)
            self.mini_title.place_forget()
            self.mini_pill.place(x=x, y=(mini_h - m.pill_h) // 2, width=width, height=m.pill_h)
        else:
            self.mini_pill.hide()
            self.mini_title.configure(text=title)
            if title:
                self.mini_title.place(x=m.p(12), y=0, height=mini_h)
            else:
                self.mini_title.place_forget()
        try:
            self.menu.entryconfig(
                self._update_menu,
                label=update_menu_label(version if ready else None),
                command=self.install_update if ready else self.check_update_now,
            )
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
                    if self.closing:
                        return
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
        digest = self.update_info.get('sha256', '')

        def work():
            try:
                source = download_and_stage(url, sha256=digest)
                self.update_queue.put(('downloaded', source))
            except RuntimeError as exc:
                # Our own checks explain themselves: size, checksum, contents.
                self.update_queue.put(('failed', str(exc)))
            except Exception:
                self.update_queue.put(('failed', '업데이트를 받지 못했습니다. 인터넷 연결을 확인하세요.'))

        threading.Thread(target=work, daemon=True, name='update-download').start()

    def _finish_update(self, source):
        try:
            start_apply(source)
        except (OSError, RuntimeError):
            # Nothing was replaced yet, so the running version simply stays.
            self._update_busy = False
            self.set_update_chrome()
            self.notify(messagebox.showinfo, '업데이트', '업데이트를 적용하지 못했습니다. 잠시 뒤 다시 시도하세요.', parent=self.root)
            return
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
        if self.activity_timer:
            self.root.after_cancel(self.activity_timer)
        # All callbacks belong to this application's Tk interpreter, including
        # short-lived menu/tooltip callbacks that do not retain their IDs.
        for callback in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.tk.call('after', 'cancel', callback)
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
        hwnd = int(app.root.winfo_id())
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
