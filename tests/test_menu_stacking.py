"""Open context menus keep their stacking order, and are left alone once on top."""
import os
import unittest
from unittest.mock import patch

import usage_widget as u

MAIN, SUBMENU, OTHER_APP, WIDGET, SHADOW, HELPER = 101, 102, 900, 500, 103, 700
# left, top, right, bottom: the submenu overlaps the main menu by 6 px.
RECTS = {MAIN: (500, 400, 603, 460), SUBMENU: (597, 418, 665, 478), SHADOW: (597, 418, 670, 483),
         WIDGET: (8, 8, 700, 894), OTHER_APP: (0, 0, 2000, 2000), HELPER: (0, 0, 1, 1)}
CLASSES = {MAIN: '#32768', SUBMENU: '#32768', OTHER_APP: '#32768', SHADOW: 'SysShadow',
           WIDGET: 'TkTopLevel', HELPER: 'ThumbnailDeviceHelperWnd'}
TOPMOST = {-1, 2 ** 32 - 1, 2 ** 64 - 1}


class FakeUser32:
    """Top-first window order. SetWindowPos moves a window to the front
    (HWND_TOPMOST) or directly below the window given as insert-after."""

    def __init__(self, order, visible=None):
        self.order = list(order)
        self.visible = set(order) if visible is None else set(visible)
        self.owner = {MAIN: os.getpid(), SUBMENU: os.getpid(), OTHER_APP: 1, WIDGET: os.getpid(),
                      SHADOW: os.getpid(), HELPER: 2}
        self.menus = {MAIN, SUBMENU, OTHER_APP}
        self.calls = []
        self.states = []

        # Plain functions, like ctypes functions, accept argtypes/restype.
        def find(parent, after, cls, name):
            menus = [h for h in self.order if h in self.menus]
            start = 0 if after is None else menus.index(after) + 1
            return menus[start] if start < len(menus) else None

        def top(parent):
            return self.order[0] if self.order else None

        def next_window(hwnd, cmd):
            at = self.order.index(int(hwnd.value)) + 1
            return self.order[at] if at < len(self.order) else None

        self.FindWindowExW, self.GetTopWindow, self.GetWindow = find, top, next_window

    def GetClassNameW(self, hwnd, buf, size):
        buf.value = CLASSES.get(int(hwnd.value or 0), '')
        return len(buf.value)

    def GetWindowRect(self, hwnd, rect):
        box = RECTS.get(int(hwnd.value or 0))
        if box is None:
            return 0
        rect[:] = box
        return 1

    def IsWindowVisible(self, hwnd):
        return int(hwnd.value or 0) in self.visible

    def GetWindowThreadProcessId(self, hwnd, ref):
        ref._obj.value = self.owner.get(int(hwnd.value or 0), 0)
        return 1

    def SetWindowPos(self, hwnd, insert, *rest):
        handle, after = int(hwnd.value or 0), insert.value
        self.calls.append(handle)
        if handle not in self.order:
            return 1
        self.order.remove(handle)
        if after in TOPMOST or after is None:
            self.order.insert(0, handle)
        else:
            self.order.insert(self.order.index(after) + 1, handle)
        self.states.append(list(self.order))
        return 1


class Windll:
    def __init__(self, user32):
        self.user32 = user32


class MenuStackingTests(unittest.TestCase):
    def raise_menus(self, user32, passes=1, extra=0):
        with patch.object(u.ctypes, 'windll', Windll(user32)):
            for _ in range(passes):
                u.lift_menu_windows(extra)

    def test_menus_already_on_top_are_not_touched(self):
        # Touching them at all is what made the overlapping strip flicker.
        user32 = FakeUser32([SUBMENU, MAIN, WIDGET, OTHER_APP])
        self.raise_menus(user32, passes=20, extra=WIDGET)
        self.assertEqual(user32.calls, [])
        self.assertEqual(user32.order[:2], [SUBMENU, MAIN])

    def test_raising_never_passes_the_main_menu_over_the_submenu(self):
        user32 = FakeUser32([WIDGET, SUBMENU, MAIN, OTHER_APP])
        self.raise_menus(user32)
        self.assertEqual(user32.order[:3], [SUBMENU, MAIN, WIDGET])
        for state in user32.states:
            self.assertLess(state.index(SUBMENU), state.index(MAIN), state)

    def test_once_raised_later_passes_do_nothing(self):
        user32 = FakeUser32([WIDGET, SUBMENU, MAIN])
        self.raise_menus(user32)
        user32.calls.clear()
        self.raise_menus(user32, passes=10)
        self.assertEqual(user32.calls, [])

    def test_shadows_and_tiny_helper_windows_do_not_count(self):
        # The real stack while a submenu is open: a 1x1 helper window from
        # another program on top, and a drop shadow under each menu.
        user32 = FakeUser32([HELPER, SUBMENU, SHADOW, MAIN, WIDGET])
        self.raise_menus(user32, passes=20)
        self.assertEqual(user32.calls, [])

    def test_the_widget_over_a_menu_is_not_left_there(self):
        user32 = FakeUser32([WIDGET, HELPER, SUBMENU, SHADOW, MAIN])
        self.raise_menus(user32)
        self.assertLess(user32.order.index(MAIN), user32.order.index(WIDGET))
        self.assertLess(user32.order.index(SUBMENU), user32.order.index(MAIN))

    def test_hidden_windows_above_do_not_count(self):
        user32 = FakeUser32([WIDGET, SUBMENU, MAIN], visible=[SUBMENU, MAIN])
        self.raise_menus(user32, passes=5)
        self.assertEqual(user32.calls, [])

    def test_other_processes_menus_are_left_alone(self):
        user32 = FakeUser32([OTHER_APP, MAIN])
        self.raise_menus(user32, passes=3)
        self.assertEqual(user32.order, [MAIN, OTHER_APP])
        self.assertNotIn(OTHER_APP, user32.calls)


if __name__ == '__main__':
    unittest.main()
