"""Open context menus keep their stacking order while they are held on top."""
import os
import unittest
from unittest.mock import patch

import usage_widget as u

MAIN, SUBMENU, OTHER_APP = 101, 102, 900


class FakeUser32:
    """Top-first window order; SetWindowPos(HWND_TOPMOST) moves a window to the front."""

    def __init__(self, order):
        self.order = list(order)
        self.owner = {MAIN: os.getpid(), SUBMENU: os.getpid(), OTHER_APP: 1}

        # A plain function, like a ctypes function, accepts argtypes/restype.
        def find(parent, after, cls, name):
            start = 0 if after is None else self.order.index(after) + 1
            return self.order[start] if start < len(self.order) else None
        self.FindWindowExW = find

    def GetWindowThreadProcessId(self, hwnd, ref):
        ref._obj.value = self.owner.get(int(hwnd.value or 0), 0)
        return 1

    def SetWindowPos(self, hwnd, insert, *rest):
        handle = int(hwnd.value or 0)
        if handle in self.order:
            self.order.remove(handle)
            self.order.insert(0, handle)
        return 1


class Windll:
    def __init__(self, user32):
        self.user32 = user32


class MenuStackingTests(unittest.TestCase):
    def raise_menus(self, user32, passes):
        with patch.object(u.ctypes, 'windll', Windll(user32)):
            for _ in range(passes):
                u.lift_menu_windows()

    def test_an_open_submenu_stays_over_the_main_menu(self):
        user32 = FakeUser32([SUBMENU, MAIN, OTHER_APP])
        seen = []
        for _ in range(6):
            self.raise_menus(user32, 1)
            seen.append(tuple(user32.order[:2]))
        self.assertEqual(set(seen), {(SUBMENU, MAIN)}, 'the two menus must not trade places')

    def test_menus_are_raised_above_other_windows(self):
        user32 = FakeUser32([OTHER_APP, SUBMENU, MAIN])
        self.raise_menus(user32, 1)
        self.assertEqual(user32.order, [SUBMENU, MAIN, OTHER_APP])

    def test_other_processes_menus_are_left_alone(self):
        user32 = FakeUser32([OTHER_APP, MAIN])
        user32.owner[OTHER_APP] = 1
        self.raise_menus(user32, 3)
        self.assertEqual(user32.order, [MAIN, OTHER_APP])


if __name__ == '__main__':
    unittest.main()
