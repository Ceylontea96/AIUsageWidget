"""Tk idle events must not leak into the next test's interpreter."""
from pathlib import Path
import subprocess
import sys
import unittest


class TkLifecycleTests(unittest.TestCase):
    def test_destroyed_roots_leave_no_background_theme_errors(self):
        # Tcl reports these errors straight to stderr, outside Python's
        # redirect_stderr. Keep the old interpreters alive to reproduce the
        # ordering seen when test widgets retain their roots.
        code = '''
import tkinter as tk
from tests.tk_support import destroy_root
roots, idle = [], []
for index in range(3):
    root = tk.Tk()
    root.withdraw()
    roots.append(root)
    root.after_idle(lambda i=index: idle.append(i))
    destroy_root(root)
probe = tk.Tk()
probe.withdraw()
probe.update_idletasks()
destroy_root(probe)
assert idle == [0, 1, 2], idle
'''
        result = subprocess.run(
            [sys.executable, '-B', '-c', code],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b'')
