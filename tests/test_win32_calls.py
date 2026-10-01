"""Win32 calls the widget makes every two seconds must leave nothing behind.

ctypes keeps every type that ctypes.POINTER makes until the process ends
(Python 3.13 and older), and with it the structure the pointer points to. A
structure class defined inside a function is a new class on every call, so
each call left its class and pointer type behind: about 17 KB for every
environment check, close to 1 MB a minute in a running widget.
"""
import ctypes
import gc
import unittest

import runtime
import win32_windows


def structures_of(module):
    gc.collect()
    return sum(1 for o in gc.get_objects()
               if isinstance(o, type) and issubclass(o, ctypes.Structure) and o.__module__ == module.__name__)


class NoNewTypesTests(unittest.TestCase):
    def assert_no_new_structures(self, module, call):
        call()  # whatever is made once, on first use, is made now
        before = structures_of(module)
        for _ in range(20):
            call()
        self.assertEqual(structures_of(module), before)

    def test_monitor_lookup(self):
        self.assert_no_new_structures(win32_windows, lambda: win32_windows.monitor_area(0, 0))

    def test_session_lock_check(self):
        self.assert_no_new_structures(runtime, runtime.session_locked)


if __name__ == '__main__':
    unittest.main()
