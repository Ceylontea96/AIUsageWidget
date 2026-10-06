"""Small, read-only quota monitor. All Tk calls stay on the main thread."""
from __future__ import annotations

import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import queue
import subprocess
import threading
import time
import traceback
import tkinter as tk
import webbrowser
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tkinter import messagebox, font as tkfont

from additional_ui import additional_count, expanded_body_budget
import widget_raster as raster
from codex_activity import CodexActivityMonitor, LOG
from claude_activity import UNAVAILABLE as CLAUDE_ACTIVITY_UNAVAILABLE, ClaudeActivityMonitor
from cursor_activity import BackgroundCursorActivityMonitor
from providers import (
    claude_plan_label,
    error_snapshot,
    fetch_claude,
    snapshot_from_dict,
    snapshot_to_dict,
)
from quota_policy import global_main_limits, main_remaining_percents, representative_percent
from codex_app_server import CodexAppServer
from runtime import AlertGate, AuthWatcher, CodexJob, PollRunner, ToastSender, WorkerJob, limiting_quota, login_present, login_status, prepare_action, session_locked, start_tool_setup
from updater import APP_VERSION, CHECK_EVERY, LAUNCHER_EXE, download_and_stage, fetch_latest, is_git_checkout, load_feed_url, start_apply, update_confirm_text
from win32_windows import (
    clamp_position, dwm_round_corners, keep_topmost_style, lift_menu_windows, lift_owned_popups, monitor_area,
    monitor_dpi, process_alive, set_over_taskbar, show_window, terminate_pid, widget_windows, windows_for_pid,
    work_area,
)
from win32_curtain import Curtain
from widget_dialogs import PRIMARY, ThemedButton, ThemedCheck, dialog_label, dialog_rule, styled_window, swatch
from widget_theme import *  # noqa: F401,F403  colours, fonts and sizes
from widget_text import *  # noqa: F401,F403  what a snapshot reads as
from widget_cards import *  # noqa: F401,F403  cards, chips and their animation

APP_DIR = Path(os.environ.get('APPDATA', str(Path.home()))) / 'AiUsageWidget'
SETTINGS_PATH = APP_DIR / 'settings.json'
CACHE_PATH = APP_DIR / 'last_snapshot.json'
# Files earlier versions kept in APP_DIR that nothing reads any more.
# reset_credits.json: GPT reset-credit expiry cache, unused since 3.7.0.
OBSOLETE_FILES = ('reset_credits.json',)
ALERT_PATH = APP_DIR / 'alerts.json'
INSTALL_PATH = APP_DIR / 'install.json'
APP_ROOT = Path(__file__).resolve().parent
SHORTCUT_NAME = 'AI Usage.lnk'


def lock_path():
    return APP_DIR / 'widget.lock'


def instance_path():
    return APP_DIR / 'widget.instance'


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
        try:
            log_launch('shortcut failed: ' + detail)
        except OSError:
            pass
        raise RuntimeError('바탕화면 바로가기를 만들지 못했습니다. 바탕화면 폴더의 위치와 쓰기 권한을 확인하세요.')
    save_install_root(root, shortcut_asked=True)
    return desktop / SHORTCUT_NAME


LEGEND_RING_NOTE = ('한 줄 칩은 카드와 같은 색입니다. 칩 둘레가 노랑이나 빨강이면 크게 보이는 한도 말고 '
                    '다른 한도(예: 주간)가 낮다는 뜻입니다.')


def help_content():
    """(legend, sections, footer) of the help window and help_text().

    legend: (chip fill, ring colour or None, name, rule) for each colour.
    sections: (title, [(key or '', text)]); a key sets the row out as a table.
    footer: version first, then the notice lines.
    """
    legend = [
        (CHIP_OK['chatgpt'], None, '여유', '남은 양 50% 이상 · 서비스 색'),
        (CHIP_WARN, None, '주의', '50% 미만'),
        (CHIP_DANGER, None, '임박', '20% 미만'),
        (CHIP_DANGER, DANGER, '곧 한도', '5% 미만, 또는 사용이 제한됨'),
        (CHIP_STALE, None, '이전 값', '최근 확인이 실패해 마지막 값을 보여 줌'),
    ]
    sections = [
        ('서비스', [
            ('', 'GPT · 실제 한도 기간으로 구분해, 5시간 한도가 있으면 그것을, 없으면 주간·기타 한도를 크게 '
                 '보여 줍니다. ChatGPT 데스크톱 앱이 아니라 Codex CLI 로그인이 필요합니다.'),
            ('', 'Cursor · Cursor Models를 대표 잔여로, Other Models를 보조 막대로 보여 줍니다. 막대 아래는 '
                 '청구 주기 초기화입니다.'),
            ('', 'Claude · Claude.ai 구독과 지원되는 Claude Code가 필요합니다. 대화형 세션의 5시간·주간 한도만 '
                 '보여 주며 Additional/Billing은 없습니다. claude -p는 추적되지 않습니다.'),
            ('', '우클릭 → 서비스·로그인 관리에서 서비스를 고르고 로그인합니다. 기본 포함량 소진과 전체 한도 '
                 '소진은 다를 수 있습니다.'),
        ]),
        ('알림과 조회', [
            ('', '남은 양이 10% 이하가 되거나 소진되면 한 번 알립니다. 12%를 넘게 회복하면 다시 알릴 수 있습니다. '
                 '알림을 켜면 예시 알림이 한 번 뜹니다.'),
            ('', '카드마다 마지막 확인 시각이 보입니다. 조회가 실패하면 이전 값과 원인이 남고, 다시 확인·로그인 '
                 '안내 버튼이 나타납니다.'),
            ('', '로그인 파일이 바뀌면 자동으로 다시 조회합니다(제한 시간 15초). 화면이 잠긴 동안은 조회를 '
                 '멈추고, 잠금이 풀리면 바로 조회합니다.'),
        ]),
        ('조작', [
            ('F5', '새로고침'),
            ('Ctrl+M · 제목 더블클릭', '한 줄 / 상세 전환'),
            ('Ctrl++ · Ctrl+- · Ctrl+0', '크게 · 작게 · 기본 크기'),
            ('제목 드래그', '위젯 옮기기'),
            ('서비스 이름 · Enter/Space', '카드 접기·펼치기'),
            ('↗', '사용량 페이지 열기'),
            ('휠 · PageUp/PageDown', '긴 본문 스크롤'),
            ('우클릭', '설정 메뉴'),
        ]),
        ('설치와 업데이트', [
            ('', '실행 파일은 zip을 푼 폴더의 AI Usage.exe입니다. 한 번 실행한 뒤에는 실행 파일만 옮겨도 됩니다. '
                 '우클릭 → 바탕화면 바로가기 만들기로 바로가기를 만들 수 있습니다.'),
            ('', '새 버전이 있으면 제목 옆에 초록 ↑ 업데이트 버튼이 나타납니다.'),
        ]),
    ]
    footer = [
        f'현재 버전 {APP_VERSION}',
        '이 위젯은 OpenAI(ChatGPT)·Cursor·Anthropic과 제휴되지 않은 비공식 도구입니다.',
        '사용량 조회는 언제든 실패하거나 바뀔 수 있습니다.',
        '계정 로그인은 각 서비스에서 하세요. 위젯은 읽기만 합니다.',
    ]
    return legend, sections, footer


# What each service needs, under its name in the services dialog.
SERVICE_NEEDS = {
    'chatgpt': 'Codex CLI 로그인이 필요합니다. ChatGPT 데스크톱 앱만으로는 읽을 수 없습니다.',
    'cursor': 'Cursor 앱에 로그인되어 있어야 합니다.',
    'claude': '대화형 Claude Code 연동이 필요합니다. ~/.claude/settings.json은 연동 버튼을 눌렀을 때만 바뀝니다.',
}


def login_guidance(key, snap=None):
    """Action behind the card's login-help button."""
    if key == 'claude' and failure_cause(snap) == '재로그인 필요':
        return 'Claude 로그인', 'claude-login'
    return prepare_action(key)


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
# Opening: the window fades in over ENTRANCE_FADE_S with its rings empty, then
# after ENTRANCE_SETTLE_S the cards fill (widget_cards.ENTRANCE_S), each
# starting ENTRANCE_STAGGER_S after the last.
ENTRANCE_FADE_S = 0.22
ENTRANCE_SETTLE_S = 0.05
ENTRANCE_STAGGER_S = 0.09
LOG_LIMIT = 256 * 1024
CALLBACK_ERROR_REPEAT = 60.0
# statusLine only runs in terminal Claude Code. When it is silent the widget
# asks the CLI itself; that costs a process, not tokens, so keep it infrequent.
CLAUDE_CLI_INTERVAL = 60.0
# Each usage query launches Claude Code: several seconds of CPU and ~0.5 GB for
# a moment. While Claude has not been used on this PC for CLAUDE_RECENT_USE,
# the quota only moves through other devices, so it is asked every 5 minutes.
CLAUDE_CLI_IDLE_INTERVAL = 300.0
CLAUDE_RECENT_USE = 600.0
# A CLI value turns grey only after two idle queries were missed. It used to
# equal the idle interval, so each 3-7 s query greyed the card before it came
# back. A failed query still marks the value stale at once.
CLAUDE_CLI_STALE = 2 * CLAUDE_CLI_IDLE_INTERVAL + 60.0


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


def next_fast_due(started, now, interval):
    """When the next fast poll may start; `interval` is the provider's active_interval."""
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


def remove_obsolete_files(folder):
    """Delete files earlier versions left in the data folder; never anything else."""
    for name in OBSOLETE_FILES:
        try:
            (Path(folder) / name).unlink(missing_ok=True)
        except OSError:
            pass


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
    """The logon entry: the widget's launcher validates cached Python or finds it.

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
    """A line icon 16 px at scale 1: the 16 px master there, else the 32 px one shrunk.

    The 16 px image alone stayed 16 px when the widget grew, smaller and smaller
    beside its text.
    """
    size = max(8, int(round(16 * scale)))
    base, large = ICON_DIR / f'{name}.png', ICON_DIR / f'{name}@2x.png'
    try:
        if size == 16 or not large.is_file():
            return tk.PhotoImage(data=base.read_bytes(), format='png') if base.is_file() else None
        data = large.read_bytes()
        if size < 32:
            # Above 32 px (twice the size and more) the 32 px image is shown as it is.
            data = raster.png_rgba(*raster.shrink_rgba(*raster.read_png_rgba(data), size, size))
        return tk.PhotoImage(data=data, format='png')
    except (OSError, ValueError, tk.TclError):
        return None


PILL_PULSE_STEPS = 24


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
    # It pulses this many times when it appears, then rests at the pulse's
    # middle shade. Pulsing until the update was installed redrew it 16 times
    # a second for as long as that took, sometimes days.
    _PULSE_BEATS = 3

    def __init__(self, parent, command, metrics=None, tip=None):
        self.metrics = metrics or Metrics()
        super().__init__(parent, width=1, height=1, bg=BG, bd=0, highlightthickness=0)
        self.command = command
        self.tip, self.tip_text = tip, ''
        self.text, self.ready, self.hover, self.width_px = '', False, False, 0
        self._pulse_after = None
        self._pulse_phase = 0.0
        self._pulse_left = None   # steps of the pulse still to show
        self._resting = False
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
        if ready and not (self.ready and self.text):
            # Appearing, or ready again after installing failed: pulse anew.
            self._resting, self._pulse_left = False, None
        self.text, self.ready, self.width_px, self.tip_text = text, ready, max(1, int(width)), hint
        self.configure(width=self.width_px, height=self.metrics.pill_h, cursor='hand2' if ready else 'arrow')
        self._sync_pulse(0.0)
        self._redraw()
        if self.hover:
            self._tip_hover(True)
        return self.width_px

    def hide(self):
        self._stop_pulse()
        self._resting, self._pulse_left = False, None
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
        if self.ready and self.animate and not self.hover and self.text and not self._resting:
            if self._pulse_after is None:
                self._pulse_phase = phase
                if self._pulse_left is None:
                    self._pulse_left = self._PULSE_BEATS * self._PULSE_PERIOD // self._PULSE_MS
                self._schedule_pulse()
        else:
            self._stop_pulse()

    def _pulse_amount(self):
        # Stepped, so the pulse reuses a handful of cached images.
        return round(0.5 * (1.0 + math.sin(self._pulse_phase)) * PILL_PULSE_STEPS) / PILL_PULSE_STEPS

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
        self._pulse_left -= 1
        # Once its beats are shown it stops where it passes the resting shade,
        # so the last frame and the rest look the same.
        if self._pulse_left <= 0 and self._pulse_amount() == 0.5:
            self._resting = True
        try:
            self._redraw()
        except tk.TclError:
            return
        if not self._resting:
            self._schedule_pulse()

    def _redraw(self):
        self.delete('all')
        if not self.text:
            return
        w, h = self.width_px, self.metrics.pill_h
        if self.ready:
            if self.hover:
                amount = 1.0
            elif self.animate:
                amount = 0.5 if self._resting else self._pulse_amount()
            else:
                amount = 0.28
            fill = raster.blend(CODEX, raster.lighten(CODEX), amount)
            fg = BG
        else:
            # The chips' empty track: CARD is too close to the header to read as a button.
            fill, fg = CHIP_TRACK, MUTED
        # Drawn as an anti-aliased image like the chips: Tk's canvas ovals
        # left the round ends visibly stepped.
        self._photo = tk.PhotoImage(data=raster.pill_png(w, h, fill, BG), format='png')
        self.create_image(0, 0, image=self._photo, anchor='nw')
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self.metrics.font(FONT_PILL))


class UsageWidget:
    def __init__(self, preview=False):
        self.preview = preview
        self.settings = read_json(SETTINGS_PATH)
        self.scale = clamp_scale(self.settings.get('scale', DEFAULT_SCALE))
        # The monitor's DPI joins the user's scale (see Metrics). Preview and
        # tests draw at 96 so their sizes do not depend on the screen.
        self.dpi = 96 if preview else monitor_dpi(*self._saved_corner())
        self.metrics = Metrics(self.scale, self.dpi)
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
        # The widget opens by fading in while its cards fill (see _enter);
        # preview and tests show it at once.
        self.entrance = not preview
        if self.entrance:
            self.root.attributes('-alpha', 0.0)
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
        self.cursor_activity = BackgroundCursorActivityMonitor()
        self.claude_activity = ClaudeActivityMonitor()
        self.request_started = dict.fromkeys(FETCHERS, float('-inf'))
        self.poll_pending = dict.fromkeys(FETCHERS, False)
        self._ui_active = dict.fromkeys(FETCHERS, False)
        if not preview:
            remove_obsolete_files(APP_DIR)
        if not preview and not LOG.handlers:
            try:
                APP_DIR.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(APP_DIR / 'activity-debug.log', maxBytes=262144, backupCount=4, encoding='utf-8')
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
        self.claude_cli_started = float('-inf')
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
        # Whether Windows rounds the window itself (Windows 11); None until asked.
        self._dwm_corners = None
        # The still copy held over the widget while a card folds (see _held_still):
        # the row its new space opens at while it is up, else None.
        self._curtain = Curtain(BG)
        self._held_split = None
        self._topmost_held = False
        self._footer_state = None
        self.update_info = None
        self.update_queue = queue.Queue()
        self.last_update_check = float('-inf')
        self._update_busy = False
        self._overlay = 0
        self._menu_held = False
        # How many times the menu was posted; a release only counts for the latest.
        self._menu_post = 0
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
        if not preview:
            self.root.after(1500, self.cards[FETCHERS[0]].warm_turns)
        if self.entrance:
            self.root.after(0, self._enter)
            # Whatever happens to the entrance, the widget never stays see-through.
            self.root.after(3000, self._show_fully)
        self.tick()
        self._activity_tick()

    def _saved_corner(self):
        try:
            return int(self.settings.get('x', 40)), int(self.settings.get('y', 80))
        except (ValueError, TypeError):
            return 40, 80

    def _load_icons(self):
        self._icons_scale = self.metrics.scale
        for name in ('refresh', 'minus', 'close', 'expand', 'plus'):
            image = load_icon(name, self.metrics.scale)
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
        button = IconButton(parent, image, command, hover_bg=hover_bg, size=self.metrics.icon, tip=self.tip, hint=hint)
        button.icon_name = name
        return button

    def build(self):
        m = self.metrics
        self.shell = tk.Frame(self.root,bg=BG,width=m.window_w,highlightbackground=HAIR,highlightthickness=1)
        self.shell.pack()
        self.shell.pack_propagate(False)
        self.header = tk.Frame(self.shell,bg=BG,height=m.header_h)
        self.title = tk.Label(self.header,text='AI Usage',bg=BG,fg=TEXT,font=m.font(FONT_TITLE),bd=0,padx=0,pady=0)
        self.updated_label = tk.Label(self.header,text='',bg=BG,fg=DIM,bd=0)
        self.live_dot = tk.Canvas(self.header,width=6,height=6,bg=BG,bd=0,highlightthickness=0)
        self._header_text = None
        self._header_dot = None
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
        if self._held_split is not None:
            # Restacking the widget under its still copy let the copy drop
            # behind it for a frame; _held_still restacks once it is gone.
            self._topmost_held = True
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

    def _release_menu(self, post=None):
        """Clear the held state however the menu was dismissed.

        tk_popup only returns once the popup has gone, so reaching here means
        the menu is closed: by a command, a click outside, Escape, another
        window taking focus, or shutdown. The state must never survive it.

        post: which posting this release is for. A right-click while the menu
        is open closes it and posts it again at once, and the first posting's
        release, queued with after(0), then ran inside the second one: the
        widget took the hold for over and rose above the open menu, again
        every 2 s. A release for an older posting is ignored.
        """
        if post is not None and post != self._menu_post:
            return
        if self._menu_held:
            self._menu_held = False
            if not self._overlay and not self.closing:
                self.apply_topmost()

    def help_text(self):
        """The help as plain text: the same content the help window lays out."""
        legend, sections, footer = help_content()
        lines = [footer[0], '', *footer[1:], '', '표시 기준']
        lines += [f'{name} · {rule}' for _, _, name, rule in legend]
        for title, rows in sections:
            lines += ['', title]
            lines += [f'{key} · {text}' if key else text for key, text in rows]
        return '\n'.join(lines)

    def help(self):
        """The help window: colour legend, services, alerts, keys, install, in the widget's style.

        It replaced a Windows message box of 25 lines of plain text, whose
        colour rules had no colours and which kept one size at any scale.
        """
        m = self.metrics
        self.push_overlay()
        try:
            window = styled_window(self.root, 'AI Usage 도움말')
            pad, wrap = m.p(20), 520
            legend, sections, footer = help_content()
            view = tk.Canvas(window, bg=BG, bd=0, highlightthickness=0)
            body = tk.Frame(view, bg=BG)
            view.create_window(0, 0, window=body, anchor='nw')
            dialog_label(body, 'AI Usage 도움말', m, FONT_SERVICE, anchor='w', padx=pad, pady=(m.p(18), m.p(2)))
            dialog_label(body, footer[0], m, FONT_META, MUTED, anchor='w', padx=pad, pady=(0, m.p(6)))
            dialog_label(body, ' '.join(footer[1:]), m, FONT_META, MUTED, wrap=wrap, anchor='w', padx=pad,
                         pady=(0, m.p(14)))

            def heading(text):
                dialog_rule(body, m, padx=pad)
                dialog_label(body, text, m, FONT_TITLE, anchor='w', padx=pad, pady=(m.p(12), m.p(6)))

            heading('표시 기준')
            grid = tk.Frame(body, bg=BG)
            grid.pack(anchor='w', padx=pad)
            for row, (fill, ring, name, rule) in enumerate(legend):
                swatch(grid, fill, m, ring).grid(row=row, column=0, sticky='w', pady=m.p(3))
                dialog_label(grid, name, m, FONT_VALUE).grid(row=row, column=1, sticky='w', padx=(m.p(10), m.p(12)))
                dialog_label(grid, rule, m, FONT_SUB, STATUS_FG).grid(row=row, column=2, sticky='w')
            dialog_label(body, LEGEND_RING_NOTE, m, FONT_META, MUTED, wrap=wrap, anchor='w', padx=pad,
                         pady=(m.p(8), m.p(14)))
            for title, rows in sections:
                heading(title)
                table = tk.Frame(body, bg=BG)
                table.pack(anchor='w', padx=pad, pady=(0, m.p(12)))
                for row, (key, text) in enumerate(rows):
                    if key:
                        dialog_label(table, key, m, FONT_VALUE).grid(row=row, column=0, sticky='nw',
                                                                     padx=(0, m.p(14)), pady=m.p(3))
                        dialog_label(table, text, m, FONT_SUB, STATUS_FG, wrap=330).grid(
                            row=row, column=1, sticky='nw', pady=m.p(3))
                    else:
                        dialog_label(table, text, m, FONT_SUB, STATUS_FG, wrap=wrap).grid(
                            row=row, column=0, columnspan=2, sticky='w', pady=m.p(3))
            dialog_rule(body, m, padx=pad)
            bar = tk.Frame(window, bg=BG)
            close = ThemedButton(bar, '닫기', window.destroy, m, PRIMARY)
            close.pack(side='right')
            window.bind('<Escape>', lambda e: window.destroy())
            body.update_idletasks()
            try:
                work = work_area(self.root.winfo_rootx(), self.root.winfo_rooty())
                room = work[3] - work[1] - m.p(120)
            except tk.TclError:
                room = body.winfo_reqheight()
            height = min(body.winfo_reqheight(), max(m.p(240), room))
            view.configure(width=body.winfo_reqwidth(), height=height,
                           scrollregion=(0, 0, body.winfo_reqwidth(), body.winfo_reqheight()))
            view.pack(fill='both')
            bar.pack(fill='x', padx=pad, pady=(m.p(10), m.p(18)))
            if body.winfo_reqheight() > height:
                # Taller than the screen allows: the wheel scrolls it.
                window.bind('<MouseWheel>', lambda e: view.yview_scroll(-1 if e.delta > 0 else 1, 'units'))
                view.configure(yscrollincrement=m.p(24))
            try:
                ref_x, ref_y = self.root.winfo_rootx(), self.root.winfo_rooty()
            except tk.TclError:
                ref_x, ref_y = 0, 0
            place_on_screen_center(window, ref_x, ref_y)
            window.deiconify()
            close.focus_set()
            window.grab_set()
            window.wait_window()
        finally:
            self.pop_overlay()

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
        m = self.metrics
        self.push_overlay()
        dialog = styled_window(self.root, '서비스·로그인 관리')
        pad = m.p(20)
        chosen = {key: tk.BooleanVar(value=self.enabled[key].get()) for key in FETCHERS}
        dialog_label(dialog, '서비스·로그인 관리', m, FONT_SERVICE, anchor='w', padx=pad, pady=(m.p(18), m.p(4)))
        dialog_label(dialog, '이 PC에서 볼 서비스를 고르세요. 위젯은 사용량을 읽기만 합니다.', m, FONT_SUB, MUTED,
              wrap=360, anchor='w', padx=pad, pady=(0, m.p(14)))
        notes, dots, actions = {}, {}, {}
        for key in FETCHERS:
            dialog_rule(dialog, m, padx=pad)
            block = tk.Frame(dialog, bg=BG)
            block.pack(fill='x', padx=pad, pady=(m.p(12), m.p(12)))
            row = tk.Frame(block, bg=BG)
            row.pack(fill='x')
            ThemedCheck(row, TITLES[key], chosen[key], m).pack(side='left')
            state = tk.Frame(block, bg=BG)
            state.pack(anchor='w', padx=(m.p(28), 0), pady=(m.p(2), 0))
            dot = tk.Canvas(state, width=m.p(6), height=m.p(6), bg=BG, bd=0, highlightthickness=0)
            dot.pack(side='left', padx=(0, m.p(6)))
            note = dialog_label(state, '', m, FONT_META, MUTED, wrap=300)
            note.pack(side='left')
            notes[key], dots[key] = note, dot
            dialog_label(block, SERVICE_NEEDS[key], m, FONT_META, DIM, wrap=312, anchor='w',
                  padx=(m.p(28), 0), pady=(m.p(4), 0))
            controls = tk.Frame(block, bg=BG)
            controls.pack(anchor='w', padx=(m.p(28), 0), pady=(m.p(8), 0))
            text, action = prepare_action(key)
            button = ThemedButton(controls, text, lambda a=action: self._service_setup_action(a), m)
            button.pack(side='left')
            actions[key] = button
            if key == 'claude':
                def claude_login():
                    # The login turns Claude on; keep the dialog from turning it back off.
                    chosen['claude'].set(True)
                    self._claude_integration_action('claude-login')
                ThemedButton(controls, 'Claude 로그인', claude_login, m).pack(side='left', padx=(m.p(6), 0))
        dialog_rule(dialog, m, padx=pad)
        buttons = tk.Frame(dialog, bg=BG)
        buttons.pack(fill='x', padx=pad, pady=(m.p(16), m.p(18)))
        first_run = should_setup(self.settings, self.preview)
        alive = {'on': True}

        def refresh_status():
            # Every 2 s while open, so a login done from here shows up here.
            if not alive['on']:
                return
            for key in FETCHERS:
                status = login_status(key)
                notes[key].configure(text=status)
                ready = login_present(key) if key != 'claude' else status.startswith('연동 설정됨')
                d = m.p(6)
                dots[key].delete('all')
                dots[key].create_oval(0, 0, d, d, fill='#22C55E' if ready else WARN, outline='')
                text, action = prepare_action(key)
                actions[key].set_label(text, lambda a=action: self._service_setup_action(a))
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
                self.set_provider_enabled(key, chosen[key].get())
            self.persist()
            self.apply_mode()
            alive['on'] = False
            dialog.destroy()
            finish_prepare()

        def cancel():
            alive['on'] = False
            dialog.destroy()

        ThemedButton(buttons, '확인', commit, m, PRIMARY).pack(side='right')
        if not first_run:
            ThemedButton(buttons, '취소', cancel, m).pack(side='right', padx=(0, m.p(8)))
            dialog.bind('<Escape>', lambda e: cancel())
        dialog.protocol('WM_DELETE_WINDOW', commit if first_run else cancel)
        dialog.bind('<Destroy>', lambda e: alive.update(on=False) if e.widget is dialog else None)
        refresh_status()
        try:
            ref_x, ref_y = self.root.winfo_rootx(), self.root.winfo_rooty()
        except tk.TclError:
            ref_x, ref_y = 0, 0
        place_on_screen_center(dialog, ref_x, ref_y)
        dialog.deiconify()
        try:
            dialog.grab_set()
            dialog.wait_window()
        finally:
            self.pop_overlay()

    def apply_metrics(self):
        m = self.metrics
        if m.scale != self._icons_scale:
            self._load_icons()
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
                btn.image = self.icons.get(btn.icon_name, btn.image)
                btn.set_size(m.icon)
            else:
                btn.configure(font=fallback_font)
            btn.place(x=m.window_w-m.p(90) + m.p(28) * index, y=m.p(8), width=m.icon, height=m.icon)
        # The compact row places the title itself; apply_mode owns that slot.
        for index, btn in enumerate(self.mini_buttons):
            if isinstance(btn, IconButton):
                btn.image = self.icons.get(btn.icon_name, btn.image)
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
        self._rescale()
        self.persist()

    def _follow_dpi(self):
        """Redraw at the DPI of the monitor the widget is on now; True if it changed.

        Asked at the top-left corner, the monitor relayout keeps the whole
        window on, so growing on a larger monitor cannot carry it back.
        """
        if self.preview:
            return False
        try:
            dpi = monitor_dpi(self.root.winfo_x() + 1, self.root.winfo_y() + 1)
        except tk.TclError:
            return False
        if dpi == self.dpi:
            return False
        LOG.debug('[UI] monitor DPI %s -> %s', self.dpi, dpi)
        self.dpi = dpi
        self._rescale()
        return True

    def _rescale(self):
        """Draw everything again at the user's scale on the current monitor."""
        self.metrics = Metrics(self.scale, self.dpi)
        self.apply_metrics()
        self._layout = None
        self._region_h = None
        self._footer_state = None
        for key in FETCHERS:
            if key in self.snapshots:
                self.cards[key].last_signature = None
                self.render(key)
        self.apply_mode()
        if not self.preview:
            self.root.after(500, self.cards[FETCHERS[0]].warm_turns)
        self.set_footer('', MUTED, CODEX)
        self.place(self.root.winfo_x(), self.root.winfo_y())

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
        with self._held_still(key):
            self.additional_open = None if self.additional_open == key else key
            self.relayout()

    def _enter(self):
        """Open the widget: it fades in with its rings empty, then the cards fill one after another.

        Each card first draws the ring images its fill will show (about 85 ms a
        card, while the window is still see-through). The fill waits for the
        fade: cards filling while the window was see-through dropped frames
        (gaps of 35-40 ms), where once it was opaque they stayed under 20 ms.
        """
        if self.closing:
            return
        try:
            cards = [] if self.compact else [self.cards[k] for k in FETCHERS if self.enabled[k].get()]
            for card in cards:
                card.warm_entrance()
            started = 0
            for card in cards:
                if card.enter(delay=ENTRANCE_FADE_S + ENTRANCE_SETTLE_S + started * ENTRANCE_STAGGER_S):
                    started += 1
        except tk.TclError:
            pass
        self._entrance_t0 = time.monotonic()
        self._fade_in()

    def _fade_in(self):
        if self.closing:
            return
        elapsed = time.monotonic() - self._entrance_t0
        try:
            if elapsed < ENTRANCE_FADE_S:
                self.root.attributes('-alpha', (elapsed / ENTRANCE_FADE_S) ** 0.6)
                self.root.after(16, self._fade_in)
            else:
                # Opaque again, before ENTRANCE_SETTLE_S is up and the first
                # card moves. Tk keeps the window layered (at alpha 255) from
                # here on, which measured no costlier than a plain window.
                self.root.attributes('-alpha', 1.0)
        except tk.TclError:
            pass

    def _show_fully(self):
        if not self.closing:
            try:
                if float(self.root.attributes('-alpha')) < 1.0:
                    self.root.attributes('-alpha', 1.0)
            except (tk.TclError, ValueError):
                pass

    def toggle_card(self, key):
        card = self.cards[key]
        with self._held_still(key):
            card.set_collapsed(not card.collapsed, animate=True)
            self.collapsed[key] = card.collapsed
            self.tip.hide()
            self.relayout()
        self.persist()

    @contextmanager
    def _held_still(self, key):
        """Keep the widget's picture still while the block changes the card key, then show the new one.

        Tk moves and paints the parts one at a time and Windows showed each as
        it landed: for 40-100 ms after a fold, cards drawn over each other, one
        card twice, or the desktop through a gap. A copy of the picture covers
        the widget instead, until everything is painted. Growing, the copy opens
        the new space below the card at once (see apply_mode), so the change
        that follows only fills it in.
        """
        card = self.cards[key]
        held = scrolled = False
        if not self.preview and not self.compact:
            try:
                if self.root.winfo_viewable():
                    self._held_split = card.winfo_rooty() + card.winfo_height() - self.root.winfo_rooty()
                    scrolled = bool(self.body_scroll.winfo_ismapped())
                    held = self._curtain.cover(self._widget_hwnd())
            except tk.TclError:
                pass
        if not held:
            self._held_split = None
        try:
            yield
            if held:
                scrolled = scrolled or bool(self.body_scroll.winfo_ismapped())
                self._paint_now(None if scrolled else key)
        finally:
            if held:
                self._held_split = None
                self._curtain.uncover()
                if self._topmost_held:
                    self._topmost_held = False
                    self.apply_topmost()

    def _paint_now(self, key=None):
        """Paint the card key and everything below it now, rather than when Windows asks.

        Cards above it did not move unless the body scrolled; key None paints all.
        """
        keys = [k for k in FETCHERS if self.enabled[k].get()]
        start = keys.index(key) if key in keys else 0
        widgets = [self.shell, self.body_view, self.body]
        pending = [self.cards[k] for k in keys[start:]] + [self.footer]
        while pending:
            widget = pending.pop()
            widgets.append(widget)
            pending.extend(widget.winfo_children())
        for widget in widgets:
            if widget.winfo_ismapped():
                widget.event_generate('<Expose>', x=0, y=0, width=widget.winfo_width(),
                                      height=widget.winfo_height())
        self.root.update_idletasks()

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
        self.apply_mode((x, y, work, monitor))
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

    def apply_mode(self, place=None):
        """Lay the window out for its mode, cards and size.

        place: (x, y, work, monitor) relayout keeps the window on screen with.
        """
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
        rebuild = self._layout is None or self._layout[:2] != layout[:2] or self._layout[4] != layout[4]
        self._layout = layout
        if rebuild:
            for item in (self.header,self.body_view,self.body_scroll,self.footer,self.mini):
                item.place_forget()
            for key,card in self.cards.items():
                card.pack_forget()
                if key in visible:
                    card.pack(fill='x',pady=(0,0))
        elif body_h <= view_h:
            # Only heights changed (a card folded, a row came or went), so
            # everything is moved in place below. Taking it all down and up
            # again made Tk repaint the whole window, which showed as a blink.
            self.body_scroll.place_forget()
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
        size = f'{m.window_w}x{height}'
        if self._held_split is not None:
            # Growing under the still copy: open the new space in the copy first,
            # where the window ends up (relayout keeps it on screen, which can
            # move it up), and move the window there in the step it grows.
            top = clamp_position(place[0], place[1], m.window_w, height, *place[2:]) if place else None
            if self._curtain.extend(height, self._held_split, top) and top:
                size += geometry_at(*top)
        self.root.geometry(size)
        # Refresh before the newly visible detail widgets are painted.
        self._refresh_visible_clocks()
        self.root.update_idletasks()
        if not self.preview and self._dwm_corners is None:
            self._dwm_corners = dwm_round_corners(self._widget_hwnd(), HAIR)
        if not self.preview and not self._dwm_corners and height != self._region_h:
            # Without Windows 11's own rounding, a region cuts the corners:
            # stepped, with the border line missing along the curve.
            # Region coordinates include the whole frameless window.
            try:
                gdi = ctypes.windll.gdi32
                gdi.CreateRoundRectRgn.restype = ctypes.c_void_p
                hwnd = ctypes.c_void_p(int(self.root.wm_frame(),16))
                region = gdi.CreateRoundRectRgn(0,0,m.window_w+1,height+1,m.p(28),m.p(28))
                # Under the still copy everything is painted before it goes
                # (_held_still); repainting the whole window again is waste.
                if ctypes.windll.user32.SetWindowRgn(hwnd,ctypes.c_void_p(region),self._held_split is None):
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
        self._menu_post += 1
        post = self._menu_post
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
                self.root.after(0, self._release_menu, post)
            except tk.TclError:
                self._release_menu(post)

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
        self._follow_dpi()
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
        restored_any = False
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
                restored_any = True
                self.render(key, relayout=False)
            except (ValueError, TypeError, AttributeError, OverflowError):
                continue
        if restored_any:
            # All restored cards must have their final heights before laying
            # out the window; intermediate layouts force redundant Tk paints.
            self.relayout()

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
                if math.isfinite(previous):
                    self._log_interval(key, now - previous)
        except OSError:
            self.accept(key, error_snapshot(key, TITLES[key], '조회 프로세스를 시작하지 못했습니다.', URLS[key]))

    def _log_interval(self, key, interval):
        """Write the poll interval when the cadence changes, e.g. 30 s -> 2 s."""
        logged = self.__dict__.setdefault('_logged_interval', {})
        previous = logged.get(key)
        if previous is None or abs(interval - previous) > max(1.0, previous * 0.25):
            LOG.debug('[Usage] %s request interval=%.2fs', TITLES[key], interval)
            logged[key] = interval

    def _log_quota(self, key, snap):
        line = [(item.quota_id, item.used_percent, item.remaining_percent,
                 round(item.remaining_percent) if item.remaining_percent is not None else None)
                for item in global_main_limits(snap)]
        logged = self.__dict__.setdefault('_logged_quota', {})
        if line != logged.get(key):
            logged[key] = line
            LOG.debug('[Usage] quota raw/display remaining: %s', line)

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
            # A due time of 0 is a refresh someone asked for; it skips the idle gap.
            forced = self.claude_cli_due == 0.0
            spaced = now - self.claude_cli_started >= self._claude_cli_interval(now)
            if now >= self.claude_cli_due and (forced or spaced):
                self.claude_cli_due = now + CLAUDE_CLI_INTERVAL
                self.claude_cli_started = now
                try:
                    if self.runner.start(key, now):
                        self.request_started[key] = now
                        LOG.debug('[Usage] Claude usage query started')
                except OSError:
                    LOG.debug('[Usage] Claude usage query could not start')
        self.accept(key, self._claude_display_snapshot(snap, now))

    def _claude_cli_interval(self, now):
        """1 minute while Claude was used here recently, else 5 minutes.

        Without activity detection nothing says Claude is idle, so the query
        keeps its 1-minute pace.
        """
        monitor = getattr(self, 'claude_activity', None)
        last = getattr(monitor, 'last_active', None)
        mode = getattr(monitor, 'mode', None)
        if not isinstance(last, (int, float)) or mode == CLAUDE_ACTIVITY_UNAVAILABLE:
            return CLAUDE_CLI_INTERVAL
        return CLAUDE_CLI_INTERVAL if now - last < CLAUDE_RECENT_USE else CLAUDE_CLI_IDLE_INTERVAL

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

    def set_provider_enabled(self, key, enabled):
        was = self.enabled[key].get()
        self.enabled[key].set(enabled)
        if enabled and was:
            return
        if not enabled:
            self.runner.cancel(key)
            # Stop persistent readers as well as any request in flight.
            self.runner.reset(key)
            if key == 'chatgpt':
                self.codex_activity.pause()
            elif key == 'cursor':
                self.cursor_activity.pause()
        self.due[key] = 0
        self.failures[key] = 0
        if key == 'claude':
            self.claude_cli_due = 0.0
        if enabled and key in self.snapshots:
            self.snapshots[key] = replace(self.snapshots[key], stale=True)
            self.render(key)

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
                # Moved to another monitor by Windows (a display unplugged, or
                # its scaling changed in Settings): draw at its DPI.
                self._follow_dpi()
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
            self._log_quota(key, snap)
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
        # A locked session shows nothing; a bar sweeping there only costs CPU.
        shown = not self.locked
        gpt_active = (shown and not self.preview and self.enabled['chatgpt'].get()
                      and self.codex_activity.visual_active(now))
        cursor_active = shown and self.enabled['cursor'].get() and self.cursor_activity.visual_active(now)
        claude_active = shown and self.enabled['claude'].get() and self.claude_activity.visual_active(now)
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

    def render(self, key, *, relayout=True):
        if not self.enabled[key].get():
            self.mini_values[key].configure(text=TITLES[key] + ' 꺼짐', fg=MUTED, bg=CHIP_STALE, percent=0, animate=False)
            if relayout:
                self.apply_mode()
            return
        snap = self.snapshots[key]
        # Claude's cache is read every 2 s, mostly with nothing new. Laying the
        # window out again then cost 1-4 ms each time and changed nothing.
        if self.cards[key].render(snap) and relayout:
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
        label = '' if self.update_info or self._update_busy else '· '+text
        if label != self._header_text:
            self.updated_label.configure(text=label)
            self._header_text = label
        color=MUTED if age is None or age>60 or any(s.stale for s in snaps) else '#22C55E'
        if any(not s.ok for s in snaps): color=DANGER
        d=self.metrics.p(6)
        if (color, d) != self._header_dot:
            self.live_dot.delete('all')
            self.live_dot.create_oval(0,0,d,d,fill=color,outline='')
            self._header_dot = (color, d)

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
                now = time.monotonic()
                if self.locked:
                    # Nobody sees a locked session: no scans, and the light
                    # stops sweeping. Unlocked, the next beat picks up again.
                    self._sync_activity_ui(now)
                else:
                    self._poll_activity(now)
        finally:
            # One failed beat must not end activity tracking for the session.
            if not self.closing:
                self.activity_timer = self.root.after(ACTIVITY_TICK_MS, self._activity_tick)

    def _poll_activity(self, now):
        """Read the activity monitors, then move quota polling and the bars."""
        was_fast = self.codex_activity.was_fast
        was_cursor = self.cursor_activity.was_fast
        if self.enabled['chatgpt'].get():
            activity, quota_event = self.codex_activity.poll(now)
        else:
            self.codex_activity.pause()
            activity = quota_event = False
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
                from polling import policy_for
                started = self.request_started.get('cursor', float('-inf'))
                self.due['cursor'] = min(self.due['cursor'],
                                         next_fast_due(started, now, policy_for('cursor').active_interval))
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

    def _refresh_visible_clocks(self):
        if self.compact:
            return
        now = time.time()
        for key, card in self.cards.items():
            if self.enabled[key].get():
                card.refresh_clock(now)
        self.refresh_design_status()

    def _tick_once(self):
        """One pass of the widget clock. Returns the delay to the next pass."""
        self.drain_update_queue()
        if self.closing:
            # Installing an update closed the widget from inside the queue.
            return None
        self._refresh_visible_clocks()
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
                    from polling import policy_for
                    started = self.request_started.get(key, float('-inf'))
                    self.due[key] = min(self.due[key], next_fast_due(started, now, policy_for(key).active_interval))
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
        heat = (any(until > now for key, until in self.usage_until.items() if self.enabled[key].get())
                or self.enabled['chatgpt'].get() and self.codex_activity.fast(now)
                or self.enabled['cursor'].get() and self.cursor_activity.fast(now))
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
        if is_git_checkout(APP_ROOT):
            self.notify(messagebox.showinfo, '업데이트',
                        f'새 버전 {self.update_info.get("version", "")}이 있습니다.\n\n'
                        '이 위젯 폴더는 git 작업 폴더라 위젯이 파일을 덮어쓰지 않습니다.\n'
                        'git pull 로 받은 뒤 위젯을 다시 시작하세요.', parent=self.root)
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
        self.cursor_activity.close()
        if self.timer:
            self.root.after_cancel(self.timer)
        if self.activity_timer:
            self.root.after_cancel(self.activity_timer)
        # All callbacks belong to this application's Tk interpreter, including
        # short-lived menu/tooltip callbacks that do not retain their IDs.
        for callback in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.tk.call('after', 'cancel', callback)
        self._curtain.uncover()
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
