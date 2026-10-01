"""Win32 calls about windows and processes, with no Tk state of their own.

Which monitor a point is on, how the widget's windows and menus stack above
other windows, and finding, raising or ending another copy of the widget.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from tkinter import TclError


class _MonitorInfo(ctypes.Structure):
    # Defined once, at import. ctypes keeps every type ctypes.POINTER makes
    # until the process ends, so a class defined inside _monitor_rects was a
    # new class each call and every lookup leaked about 6 KB.
    _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT), ('work', wintypes.RECT),
                ('flags', wintypes.DWORD)]


def _monitor_rects(x, y):
    try:
        api = ctypes.windll.user32
        api.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        api.MonitorFromPoint.restype = wintypes.HANDLE
        api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
        info = _MonitorInfo(); info.size = ctypes.sizeof(info)
        monitor = api.MonitorFromPoint(wintypes.POINT(x, y), 2)
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
    except (AttributeError, OSError, ValueError, TclError):
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
    """Raise this process's dialogs/menus above the widget without dropping the widget.

    EnumWindows lists windows top first; they are raised bottom first so a
    dialog and a menu keep their order instead of trading places each pass.
    """
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
                found.append(handle)
        except (AttributeError, OSError, OverflowError, TypeError, ValueError):
            pass
        return True

    found = []
    try:
        user32.EnumWindows(callback, 0)
        for handle in reversed(found):
            lift(handle)
        dialog = user32.FindWindowW('#32770', None)
        if dialog:
            other = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(dialog, ctypes.byref(other))
            if other.value == pid:
                lift(dialog)
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        pass


def _window_rect(user32, hwnd):
    rect = (ctypes.c_long * 4)()
    if not user32.GetWindowRect(ctypes.c_void_p(hwnd), rect):
        return None
    return tuple(rect)


def _menus_on_top(user32, ours):
    """Whether no window that overlaps one of our menus is stacked above it.

    Menu drop shadows sit between the menus and are ignored, as are windows
    that do not overlap any menu, such as the 1x1 helper windows other
    programs keep at the very top.
    """
    rects = {int(h): _window_rect(user32, h) for h in ours}
    below = set(rects)
    buf = ctypes.create_unicode_buffer(64)
    hwnd = user32.GetTopWindow(None)
    for _ in range(256):
        if not hwnd or not below:
            break
        handle = int(hwnd)
        if handle in below:
            below.discard(handle)
        elif user32.IsWindowVisible(ctypes.c_void_p(handle)):
            user32.GetClassNameW(ctypes.c_void_p(handle), buf, 64)
            if buf.value != 'SysShadow':
                box = _window_rect(user32, handle)
                for h in below:
                    menu = rects[h]
                    if box is None or menu is None or (
                            box[0] < menu[2] and menu[0] < box[2] and box[1] < menu[3] and menu[1] < box[3]):
                        return False
        hwnd = user32.GetWindow(ctypes.c_void_p(handle), 2)  # GW_HWNDNEXT
    return not below


def lift_menu_windows(extra_hwnd=0):
    """Keep native/Tk popup menus in the TOPMOST band; do not touch the widget.

    This runs every 50 ms while a menu is held open. Raising every menu each
    time made the main menu briefly cover the 6 px where an open submenu
    overlaps it, so the strip flickered even though the order ended the same.
    Menus already on top are now left alone. Otherwise only the top menu is
    raised and each lower one is slotted directly under the one above it, so
    no menu ever passes over another.
    """
    try:
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):
        return
    flags = 0x0013  # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE

    def place(hwnd, after):
        if not hwnd:
            return
        try:
            user32.SetWindowPos(ctypes.c_void_p(int(hwnd)), ctypes.c_void_p(after), 0, 0, 0, 0, flags)
        except (AttributeError, OSError, OverflowError, TypeError, ValueError):
            pass

    try:
        user32.FindWindowExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.FindWindowExW.restype = ctypes.c_void_p
        user32.GetTopWindow.argtypes = [ctypes.c_void_p]
        user32.GetTopWindow.restype = ctypes.c_void_p
        user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetWindow.restype = ctypes.c_void_p
        # A submenu is its own #32768 window. Windows lists them top first.
        ours, hwnd = [], None
        for _ in range(8):
            hwnd = user32.FindWindowExW(None, hwnd, '#32768', None)
            if not hwnd:
                break
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
            if pid.value == os.getpid():
                ours.append(hwnd)
        if ours and _menus_on_top(user32, ours):
            return
        place(extra_hwnd, -1)
        if ours:
            place(ours[0], -1)  # HWND_TOPMOST
            for above, below in zip(ours, ours[1:]):
                place(below, above)
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        pass


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
