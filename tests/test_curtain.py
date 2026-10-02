"""The still copy held over the widget while a card folds (win32_curtain.Curtain)."""
import ctypes
import tkinter as tk
import unittest
from ctypes import wintypes

import win32_curtain
from tests.tk_support import destroy_root
from win32_curtain import Curtain

user32, gdi32 = ctypes.WinDLL('user32'), ctypes.WinDLL('gdi32')
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HANDLE]
gdi32.GetPixel.restype = wintypes.COLORREF
gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateSolidBrush.restype = wintypes.HANDLE
gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]

RED, BLUE = 0x0000FF, 0xFF0000   # COLORREF is 0x00BBGGRR


def rect_of(hwnd):
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


class CurtainTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.addCleanup(destroy_root, self.root)
        self.root.overrideredirect(True)
        # Far off screen: the tests never show anything.
        self.root.geometry('120x80+-30000+-30000')
        self.root.update()
        self.owner = int(self.root.wm_frame(), 16)
        self.curtain = Curtain('#102030')
        self.addCleanup(self.curtain.uncover)

    def paint(self, top, bottom, split):
        """Stand in for the owner's picture: top colour above row split, bottom colour below."""
        width, height = self.curtain.size
        for colour, rows in ((top, (0, split)), (bottom, (split, height))):
            brush = gdi32.CreateSolidBrush(colour)
            user32.FillRect(self.curtain._dc, ctypes.byref(wintypes.RECT(0, rows[0], width, rows[1])), brush)
            gdi32.DeleteObject(brush)

    def test_covers_the_window_with_a_window_that_lets_clicks_through(self):
        self.assertTrue(self.curtain.cover(self.owner, 8))
        hwnd = self.curtain.hwnd
        self.assertTrue(user32.IsWindowVisible(hwnd))
        self.assertEqual(rect_of(hwnd), rect_of(self.owner))
        self.assertEqual(user32.GetWindow(hwnd, 4), self.owner)   # owned: always stacked above it
        style = user32.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF
        for flag in (0x00080000, 0x00000020, 0x08000000, 0x00000080):   # layered, transparent, no activate, tool
            self.assertTrue(style & flag, hex(flag))
        self.curtain.uncover()
        self.assertIsNone(self.curtain.hwnd)
        self.assertFalse(user32.IsWindow(hwnd))   # nothing hidden is kept holding an old picture

    def test_growing_opens_space_at_the_split_and_moves_the_rest_down(self):
        self.curtain.cover(self.owner, 8)
        self.paint(RED, BLUE, 30)
        self.curtain.extend(120, 30, 8)
        self.assertEqual(self.curtain.size, (120, 120))
        left, top, right, bottom = rect_of(self.curtain.hwnd)
        self.assertEqual((right - left, bottom - top), (120, 120))
        pixel = lambda y: gdi32.GetPixel(self.curtain._dc, 60, y)
        self.assertEqual([pixel(y) for y in (0, 29)], [RED, RED])
        self.assertEqual([pixel(y) for y in (30, 69)], [0x302010] * 2)   # '#102030' as COLORREF
        self.assertEqual([pixel(y) for y in (70, 119)], [BLUE, BLUE])

    def test_never_shrinks(self):
        self.curtain.cover(self.owner, 8)
        self.curtain.extend(60, 10, 8)
        self.assertEqual(self.curtain.size, (120, 80))

    def test_without_a_window_to_cover_nothing_is_shown(self):
        self.assertFalse(self.curtain.cover(0, 8))
        self.assertIsNone(self.curtain.hwnd)
        self.curtain.extend(200, 10, 8)
        self.curtain.uncover()

    def test_without_win32_it_declines(self):
        saved = win32_curtain._user32
        win32_curtain._user32 = None
        try:
            self.assertFalse(self.curtain.cover(self.owner, 8))
        finally:
            win32_curtain._user32 = saved


if __name__ == '__main__':
    unittest.main()
