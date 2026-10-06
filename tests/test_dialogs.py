"""The widget's own dialogs: themed controls, the services dialog and the help window."""
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import usage_widget as u
import widget_dialogs as dialogs
import widget_raster as raster
from tests.tk_support import destroy_root


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(destroy_root, self.root)
        self.m = u.Metrics()

    def test_a_check_follows_its_variable_both_ways(self):
        chosen = tk.BooleanVar(self.root, value=False)
        check = dialogs.ThemedCheck(self.root, 'GPT', chosen, self.m)
        check.invoke()
        self.assertTrue(chosen.get())
        chosen.set(False)            # set elsewhere: the box redraws
        self.assertEqual(check._image.width(), self.m.p(18))
        check.event_generate('<space>')
        check.destroy()
        chosen.set(True)             # no redraw on a destroyed box
        self.assertTrue(chosen.get())

    def test_a_check_is_wide_enough_for_its_whole_label(self):
        check = dialogs.ThemedCheck(self.root, 'Cursor', tk.BooleanVar(self.root), self.m)
        font = tk.font.Font(root=self.root, font=self.m.font(u.FONT_SERVICE))
        self.assertGreater(int(check.cget('width')), self.m.p(18) + self.m.p(10) + font.measure('Cursor'))

    def test_a_button_runs_its_command_and_takes_new_labels(self):
        ran = []
        button = dialogs.ThemedButton(self.root, '로그인', lambda: ran.append(1), self.m)
        narrow = int(button.cget('width'))
        button.invoke()
        button.set_label('설치하고 로그인', lambda: ran.append(2))
        button.invoke()
        self.assertEqual(ran, [1, 2])
        self.assertGreater(int(button.cget('width')), narrow)

    def test_the_checkbox_is_ticked_only_when_on(self):
        size, bg = 18, (0x0F, 0x10, 0x13)
        draw = lambda on: raster.read_png_rgba(raster.checkbox_png(size, on, '#10A37F', '#4A5060', '#FFFFFF', '#0F1013'))[2]
        on, off = draw(True), draw(False)
        pixel = lambda rows, x, y: tuple(rows[y][4 * x:4 * x + 3])
        # Inside the box, clear of the tick: filled when on, empty when off.
        self.assertEqual(pixel(on, 9, 3), (0x10, 0xA3, 0x7F))
        self.assertEqual(pixel(off, 9, 3), bg)
        # The tick's corner (0.43, 0.68 of the box) is drawn only when on.
        self.assertEqual(pixel(on, 7, 12), (0xFF, 0xFF, 0xFF))
        self.assertEqual(pixel(off, 7, 12), bg)
        # Rounded: the very corner is background either way.
        self.assertEqual(pixel(on, 0, 0), bg)


class HelpWindowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for guard in (patch.object(u, 'SETTINGS_PATH', Path(directory.name) / 'settings.json'),
                      patch.object(u, 'CACHE_PATH', Path(directory.name) / 'cache.json')):
            guard.start()
            self.addCleanup(guard.stop)
        self.w = u.UsageWidget(preview=True)
        self.w.root.withdraw()
        self.addCleanup(self.w.close)

    def open_help(self):
        shown = {}

        def look(window, other=None):
            shown['texts'] = [child.cget('text') for child in descendants(window) if isinstance(child, tk.Label)]
            shown['swatches'] = [child for child in descendants(window)
                                 if isinstance(child, tk.Canvas) and hasattr(child, '_image')
                                 and not isinstance(child, dialogs.ThemedButton)]
            shown['height'] = window.winfo_reqheight()
            window.destroy()

        with patch.object(tk.Toplevel, 'wait_window', look):
            self.w.help()
        return shown

    def test_help_shows_the_colour_legend_and_every_section(self):
        legend, sections, footer = u.help_content()
        shown = self.open_help()
        self.assertEqual(len(shown['swatches']), len(legend))
        for _, _, name, rule in legend:
            self.assertIn(name, shown['texts'])
            self.assertIn(rule, shown['texts'])
        for title, rows in sections:
            self.assertIn(title, shown['texts'])
        self.assertIn(footer[0], shown['texts'])
        self.assertFalse(self.w._overlay, 'the help gives the topmost hold back when it closes')

    def test_help_text_carries_the_same_content(self):
        legend, sections, footer = u.help_content()
        text = self.w.help_text()
        for line in footer:
            self.assertIn(line, text)
        for _, _, name, rule in legend:
            self.assertIn(f'{name} · {rule}', text)

    def test_help_fits_a_full_hd_screen_at_normal_size(self):
        with patch.object(u, 'work_area', return_value=(0, 0, 1920, 1040)):
            self.assertLessEqual(self.open_help()['height'], 1040 - 40)


if __name__ == '__main__':
    unittest.main()
