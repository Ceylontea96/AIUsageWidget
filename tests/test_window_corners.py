"""The widget's rounded corners: Windows 11 draws them, Windows 10 gets a region."""
import ctypes
import sys
import tkinter as tk
import unittest
from unittest.mock import patch

import win32_windows
from tests.tk_support import destroy_root


class DwmCornerTests(unittest.TestCase):
    def calls(self, results):
        """Patch DwmSetWindowAttribute; returns the (attribute, value) pairs it was given."""
        seen = []

        def fake(hwnd, attribute, value, size):
            seen.append((attribute, value._obj.value))   # value is a ctypes.byref()
            return results.pop(0)

        return seen, patch.object(ctypes.windll.dwmapi, 'DwmSetWindowAttribute', side_effect=fake)

    def test_rounds_the_window_and_colours_its_border(self):
        seen, fake = self.calls([0, 0])
        with fake:
            self.assertTrue(win32_windows.dwm_round_corners(1234, '#202327'))
        self.assertEqual(seen, [(win32_windows.DWMWA_WINDOW_CORNER_PREFERENCE, win32_windows.DWMWCP_ROUND),
                                (win32_windows.DWMWA_BORDER_COLOR, 0x00272320)])   # 0x00BBGGRR

    def test_where_windows_cannot_round_it_says_so(self):
        # Windows 10 answers E_INVALIDARG for the corner attribute.
        seen, fake = self.calls([-2147024809])
        with fake:
            self.assertFalse(win32_windows.dwm_round_corners(1234, '#202327'))
        self.assertEqual(len(seen), 1)

    @unittest.skipUnless(sys.getwindowsversion().build >= 22000, 'Windows 11 only')
    def test_windows_11_rounds_a_frameless_tk_window(self):
        root = tk.Tk()
        self.addCleanup(destroy_root, root)
        root.overrideredirect(True)
        root.geometry('120x80+-30000+-30000')
        root.update()
        self.assertTrue(win32_windows.dwm_round_corners(int(root.wm_frame(), 16), '#202327'))


if __name__ == '__main__':
    unittest.main()
