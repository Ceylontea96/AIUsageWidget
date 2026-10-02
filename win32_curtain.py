"""A still copy of a window's picture, held over it while the window rearranges.

Tk moves and paints a window's parts one at a time, and Windows composes the
screen every few milliseconds (every 6.9 ms at 144 Hz), so each part showed
as it landed: when a card folded, for 40-100 ms the widget showed cards drawn
over each other, one card twice, or the desktop through a gap. A copy of the
old picture, held over the widget until the new one is fully painted, turns
that into one change.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('width', ctypes.c_long), ('height', ctypes.c_long),
                ('planes', wintypes.WORD), ('bit_count', wintypes.WORD), ('compression', wintypes.DWORD),
                ('size_image', wintypes.DWORD), ('x_ppm', ctypes.c_long), ('y_ppm', ctypes.c_long),
                ('colors_used', wintypes.DWORD), ('colors_important', wintypes.DWORD)]


class _Blend(ctypes.Structure):
    _fields_ = [('op', ctypes.c_ubyte), ('flags', ctypes.c_ubyte), ('alpha', ctypes.c_ubyte),
                ('format', ctypes.c_ubyte)]


def _api():
    """user32, gdi32 and dwmapi with argument types, apart from the ctypes.windll ones other modules use."""
    user32, gdi32, dwmapi = ctypes.WinDLL('user32'), ctypes.WinDLL('gdi32'), ctypes.WinDLL('dwmapi')
    handle, hwnd, hdc = wintypes.HANDLE, wintypes.HWND, wintypes.HDC
    user32.CreateWindowExW.restype = hwnd
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, hwnd, handle,
                                       handle, ctypes.c_void_p]
    user32.DestroyWindow.argtypes = [hwnd]
    user32.GetWindowRect.argtypes = [hwnd, ctypes.POINTER(wintypes.RECT)]
    user32.GetDC.restype = hdc
    user32.GetDC.argtypes = [hwnd]
    user32.ReleaseDC.argtypes = [hwnd, hdc]
    user32.FillRect.argtypes = [hdc, ctypes.POINTER(wintypes.RECT), handle]
    user32.SetWindowRgn.argtypes = [hwnd, handle, wintypes.BOOL]
    user32.SetWindowPos.argtypes = [hwnd, hwnd, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.UINT]
    user32.UpdateLayeredWindow.argtypes = [hwnd, hdc, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
                                           hdc, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
                                           ctypes.POINTER(_Blend), wintypes.DWORD]
    gdi32.CreateCompatibleDC.restype = hdc
    gdi32.CreateCompatibleDC.argtypes = [hdc]
    gdi32.CreateDIBSection.restype = handle
    gdi32.CreateDIBSection.argtypes = [hdc, ctypes.POINTER(_BitmapInfoHeader), wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), handle, wintypes.DWORD]
    gdi32.SelectObject.restype = handle
    gdi32.SelectObject.argtypes = [hdc, handle]
    gdi32.DeleteObject.argtypes = [handle]
    gdi32.DeleteDC.argtypes = [hdc]
    gdi32.BitBlt.argtypes = [hdc, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, hdc, ctypes.c_int,
                             ctypes.c_int, wintypes.DWORD]
    gdi32.CreateSolidBrush.restype = handle
    gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    gdi32.CreateRoundRectRgn.restype = handle
    gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
    gdi32.GdiFlush.argtypes = []
    dwmapi.DwmSetWindowAttribute.argtypes = [hwnd, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    return user32, gdi32, dwmapi


try:
    _user32, _gdi32, _dwmapi = _api()
except (AttributeError, OSError):
    _user32 = _gdi32 = _dwmapi = None

# Layered (its picture comes from UpdateLayeredWindow), clicks pass through,
# no taskbar button, never activated, topmost.
_EX_STYLE = 0x00080000 | 0x00000020 | 0x00000080 | 0x08000000 | 0x00000008
_WS_POPUP = 0x80000000
_SRCCOPY = 0x00CC0020
_ULW_OPAQUE = 0x4
_SHOW = 0x0001 | 0x0002 | 0x0010 | 0x0040   # no size, no move, no activate, show
_DWMWA_TRANSITIONS_FORCEDISABLED = 3


class Curtain:
    """Covers one window with its own picture: cover(), extend() while it grows, uncover()."""

    def __init__(self, background):
        """background: '#RRGGBB', the colour of the space extend() opens."""
        value = int(background.lstrip('#'), 16)
        self._background = (value & 0xFF) << 16 | (value & 0xFF00) | (value >> 16 & 0xFF)   # 0x00BBGGRR
        self.hwnd = None
        self._dc = self._bitmap = None
        self.size = (0, 0)

    @property
    def shown(self):
        return self.hwnd is not None

    def cover(self, owner, corner):
        """Show the owner's picture as it is now, over it, with corners rounded like the owner's.

        False if any step failed; nothing is left on screen then.
        """
        self.uncover()
        if _user32 is None or not owner:
            return False
        try:
            rect = wintypes.RECT()
            if not _user32.GetWindowRect(owner, ctypes.byref(rect)):
                return False
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width <= 0 or height <= 0 or not self._new_picture(width, height):
                return False
            source = _user32.GetDC(owner)
            copied = bool(source) and _gdi32.BitBlt(self._dc, 0, 0, width, height, source, 0, 0, _SRCCOPY)
            if source:
                _user32.ReleaseDC(owner, source)
            # A new window each time: a hidden one kept between folds would still
            # hold an old picture for anything that showed it.
            self.hwnd = _user32.CreateWindowExW(_EX_STYLE, 'Static', None, _WS_POPUP, 0, 0, 0, 0, owner,
                                                None, None, None) or None
            if not copied or not self.hwnd:
                self.uncover()
                return False
            off = wintypes.BOOL(True)
            _dwmapi.DwmSetWindowAttribute(self.hwnd, _DWMWA_TRANSITIONS_FORCEDISABLED, ctypes.byref(off),
                                          ctypes.sizeof(off))
            if not self._show(corner, wintypes.POINT(rect.left, rect.top)):
                self.uncover()
                return False
            _user32.SetWindowPos(self.hwnd, wintypes.HWND(-1), 0, 0, 0, 0, _SHOW)
            return True
        except (ctypes.ArgumentError, OSError, TypeError, ValueError):
            self.uncover()
            return False

    def extend(self, height, split, corner, top=None):
        """Grow the copy to height by opening space at row split; what was below it moves down.

        top: the screen point to move it to in the same step, as the window will be.
        True if it grew.
        """
        width, old = self.size
        if not self.shown or height <= old:
            return False
        split = max(0, min(int(split), old))
        dc, bitmap = self._dc, self._bitmap
        self._dc = self._bitmap = None
        try:
            if not self._new_picture(width, height):
                return False
            brush = _gdi32.CreateSolidBrush(self._background)
            _user32.FillRect(self._dc, ctypes.byref(wintypes.RECT(0, split, width, split + height - old)), brush)
            _gdi32.DeleteObject(brush)
            _gdi32.BitBlt(self._dc, 0, 0, width, split, dc, 0, 0, _SRCCOPY)
            _gdi32.BitBlt(self._dc, 0, split + height - old, width, old - split, dc, 0, split, _SRCCOPY)
            return self._show(corner, wintypes.POINT(*top) if top else None)
        except (ctypes.ArgumentError, OSError, TypeError, ValueError):
            return False
        finally:
            _gdi32.DeleteDC(dc)
            _gdi32.DeleteObject(bitmap)

    def uncover(self):
        """Take the copy away, showing the owner as it is now."""
        if self.hwnd:
            _gdi32.GdiFlush()   # the owner's new picture is all drawn before the copy goes
            _user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self._dc:
            _gdi32.DeleteDC(self._dc)
        if self._bitmap:
            _gdi32.DeleteObject(self._bitmap)
        self._dc = self._bitmap = None
        self.size = (0, 0)

    def _new_picture(self, width, height):
        dc = _gdi32.CreateCompatibleDC(None)
        header = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bitmap = _gdi32.CreateDIBSection(dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0) if dc else None
        if not bitmap:
            if dc:
                _gdi32.DeleteDC(dc)
            return False
        _gdi32.SelectObject(dc, bitmap)
        self._dc, self._bitmap, self.size = dc, bitmap, (width, height)
        return True

    def _show(self, corner, where=None):
        width, height = self.size
        # The region first: growing, the window must not show its new rows unrounded, or cut off.
        region = _gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, corner, corner)
        if region and not _user32.SetWindowRgn(self.hwnd, region, False):
            _gdi32.DeleteObject(region)
        blend = _Blend(0, 0, 255, 0)
        return bool(_user32.UpdateLayeredWindow(
            self.hwnd, None, ctypes.byref(where) if where else None, ctypes.byref(wintypes.SIZE(width, height)),
            self._dc, ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), _ULW_OPAQUE))
