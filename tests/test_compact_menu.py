"""Regressions for the two 3.5.1 UI fixes.

* the compact row must fit three provider chips without entering the window
  controls' reserved strip or clipping a provider's name, and
* an open context menu must stay above the widget no matter which periodic
  callback runs while it is posted.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tkinter.font as tkfont

import usage_widget as u
from providers import ProviderSnapshot, QuotaItem

FIVE_H = 18000.0
WEEK = 604800.0


def quota(raw_id, name, remaining, window, source='chatgpt'):
    return QuotaItem(f'{source}:main:{raw_id}', source, 'main', name, raw_identifier=raw_id,
                     window_seconds=window, window_label=name, used_percent=100 - remaining,
                     remaining_percent=remaining, scope='global')


def snapshot(key, remaining):
    return ProviderSnapshot(key, u.TITLES[key], 'Plus', True, None, '',
                            main_limits=[quota('primary_window', '5시간', remaining, FIVE_H, key)])


class CompactRowPolicyTests(unittest.TestCase):
    """The pure layout policy, with no Tk involved."""

    def layout(self, count, chip_width=84, controls_left=290):
        return u.compact_row_layout(count=count, chip_width=chip_width,
                                    controls_left=controls_left, scale_px=lambda v: v)

    def test_the_title_is_given_up_before_a_chip_is(self):
        titles = [self.layout(count)[0] for count in (1, 2, 3)]
        self.assertEqual(titles[0], 'AI Usage')
        # Whatever the row needs, a chip never gets narrower than asked for
        # while a title is still on screen to surrender.
        for count, title in zip((1, 2, 3), titles):
            with self.subTest(count=count):
                _, origin, width, gap = self.layout(count)
                self.assertEqual(width, 84)
                self.assertLessEqual(origin + count * width + gap * (count - 1), 290 - 6)

    def test_the_title_shrinks_in_order(self):
        steps = [step[0] for step in u.COMPACT_TITLE_STEPS]
        self.assertEqual(steps, ['AI Usage', 'AI', ''])
        # A row too tight for the full title falls back one step at a time.
        self.assertEqual(u.compact_row_layout(count=2, chip_width=84, controls_left=250,
                                              scale_px=lambda v: v)[0], 'AI')
        self.assertEqual(u.compact_row_layout(count=3, chip_width=84, controls_left=290,
                                              scale_px=lambda v: v)[0], '')

    def test_the_gap_only_shrinks_after_the_title_is_gone(self):
        # Wide enough for three chips at a tighter gap, but not at the widest.
        title, _, width, gap = u.compact_row_layout(count=3, chip_width=84, controls_left=283,
                                                    scale_px=lambda v: v)
        self.assertEqual(title, '')
        self.assertEqual(width, 84, 'the chip narrowed before the gap did')
        self.assertLess(gap, u.COMPACT_CHIP_GAPS[0])

    def test_an_impossible_row_narrows_chips_rather_than_invading_controls(self):
        title, origin, width, gap = u.compact_row_layout(count=3, chip_width=200,
                                                         controls_left=200, scale_px=lambda v: v)
        self.assertEqual(title, '')
        self.assertLess(width, 200)
        self.assertLessEqual(origin + 3 * width + gap * 2, 200)

    def test_no_providers_still_returns_a_usable_row(self):
        title, origin, width, gap = self.layout(0)
        self.assertEqual(title, 'AI Usage')
        self.assertGreater(origin, 0)
        self.assertGreater(width, 0)
        self.assertGreaterEqual(gap, 0)


class CompactWidgetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name)
        self.patches = [patch.object(u, 'SETTINGS_PATH', path / 'settings.json'),
                        patch.object(u, 'CACHE_PATH', path / 'cache.json')]
        for item in self.patches:
            item.start()
        self._pill_animate = u.UpdatePill.animate
        u.UpdatePill.animate = False
        self.w = u.UsageWidget(preview=True)
        self.w.root.withdraw()
        self.w.compact = True

    def tearDown(self):
        self.w.close()
        u.UpdatePill.animate = self._pill_animate
        for item in self.patches:
            item.stop()
        self.directory.cleanup()

    def show(self, keys):
        for key in u.FETCHERS:
            self.w.enabled[key].set(key in keys)
        self.w._layout = None
        self.w.apply_mode()
        return self.w._compact_row

    def controls_left(self):
        return self.w.metrics.window_w - self.w.metrics.p(90)

    def chip_text_width(self, key):
        font = tkfont.Font(root=self.w.root, font=self.w.metrics.font(u.FONT_CHIP))
        return font.measure(f'{u.TITLES[key]} 100%')

    def test_every_provider_count_clears_the_controls(self):
        for keys in (['chatgpt'], ['chatgpt', 'cursor'], ['chatgpt', 'cursor', 'claude'],
                     ['cursor'], ['claude'], ['cursor', 'claude']):
            with self.subTest(keys=keys):
                _, origin, width, gap = self.show(keys)
                right = origin + (width + gap) * (len(keys) - 1) + width
                self.assertLessEqual(right, self.controls_left(),
                                     f'{len(keys)} chips reach into the controls strip')
                self.assertGreaterEqual(origin, 0)

    def test_three_providers_keep_full_names_and_percentages(self):
        keys = ['chatgpt', 'cursor', 'claude']
        _, _, width, _ = self.show(keys)
        for key in keys:
            with self.subTest(key=key):
                self.assertGreaterEqual(width, self.chip_text_width(key),
                                        f'{u.TITLES[key]} 100% would not fit its chip')

    def test_chips_never_overlap_each_other(self):
        self.show(['chatgpt', 'cursor', 'claude'])
        places = []
        for key in u.FETCHERS:
            chip = self.w.mini_values[key]
            if chip.winfo_manager() == 'place':
                x = int(chip.place_info()['x'])
                places.append((x, x + chip.chip_width))
        places.sort()
        self.assertEqual(len(places), 3)
        for (_, end), (start, _) in zip(places, places[1:]):
            self.assertLessEqual(end, start)

    def test_a_disabled_provider_gives_its_space_back(self):
        _, origin_three, width_three, gap_three = self.show(['chatgpt', 'cursor', 'claude'])
        right_three = origin_three + (width_three + gap_three) * 2 + width_three
        _, origin_two, width_two, gap_two = self.show(['chatgpt', 'claude'])
        right_two = origin_two + width_two + gap_two + width_two
        self.assertLess(right_two, right_three)
        self.assertEqual(self.w.mini_values['cursor'].winfo_manager(), '')

    def test_the_window_width_never_moves_with_the_percentage(self):
        keys = ['chatgpt', 'cursor', 'claude']
        self.show(keys)
        widths = set()
        rows = set()
        for percent in (0, 9, 10, 42, 99, 100):
            for key in keys:
                self.w.snapshots[key] = snapshot(key, percent)
                self.w.render(key)
            widths.add(self.w.metrics.window_w)
            rows.add(self.w._compact_row)
            for key in keys:
                self.assertIn(f'{percent:.0f}%', self.w.mini_values[key].cget('text'))
        self.assertEqual(len(widths), 1)
        self.assertEqual(len(rows), 1, 'the row re-flowed as the percentage changed')

    def test_every_scale_fits_three_providers(self):
        for scale in (0.75, 1.0, 1.15, 1.3, 1.5):
            with self.subTest(scale=scale):
                self.w.set_scale(scale)
                _, origin, width, gap = self.show(['chatgpt', 'cursor', 'claude'])
                self.assertLessEqual(origin + (width + gap) * 2 + width, self.controls_left())
                for key in ('chatgpt', 'cursor', 'claude'):
                    self.assertGreaterEqual(width, self.chip_text_width(key))

    def test_the_update_pill_yields_its_slot_before_a_chip_does(self):
        # The pill lives in the title slot, so it follows the title's priority:
        # it shows while there is room and steps aside when there is not.
        self.w.update_info = {'version': '9.9.9', 'notes': ''}
        self.w._update_busy = False
        for keys, expected in ((['chatgpt', 'cursor'], True), (['chatgpt', 'cursor', 'claude'], False)):
            with self.subTest(keys=keys):
                _, origin, width, gap = self.show(keys)
                self.w.set_update_chrome()
                visible = self.w.mini_pill.winfo_manager() == 'place'
                self.assertEqual(visible, expected)
                if visible:
                    pill_right = int(self.w.mini_pill.place_info()['x']) + int(self.w.mini_pill.cget('width'))
                    self.assertLessEqual(pill_right, origin)
                right = origin + (width + gap) * (len(keys) - 1) + width
                self.assertLessEqual(right, self.controls_left())

    def test_the_title_is_hidden_rather_than_clipped(self):
        title, _, _, _ = self.show(['chatgpt', 'cursor', 'claude'])
        self.assertIn(title, ('AI Usage', 'AI', ''))
        if title:
            self.assertEqual(self.w.mini_title.cget('text'), title)
            self.assertEqual(self.w.mini_title.winfo_manager(), 'place')
        else:
            self.assertEqual(self.w.mini_title.winfo_manager(), '')


class ContextMenuZOrderTests(unittest.TestCase):
    """While the menu is posted nothing may re-stack the widget window.

    Setting the topmost attribute or calling SetWindowPos with HWND_TOPMOST
    both raise the widget above an already-open menu. Only the style bit,
    which does not restack, is allowed.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name)
        self.patches = [patch.object(u, 'SETTINGS_PATH', path / 'settings.json'),
                        patch.object(u, 'CACHE_PATH', path / 'cache.json')]
        for item in self.patches:
            item.start()
        self._pill_animate = u.UpdatePill.animate
        u.UpdatePill.animate = False
        self.w = u.UsageWidget(preview=True)
        self.w.root.withdraw()
        self.w.preview = False          # exercise the real topmost paths
        self.restacks = []
        self.raised = []
        self.spies = [
            patch.object(u, 'set_over_taskbar',
                         lambda root, on=True: self.restacks.append(f'set_over_taskbar({on})')),
            patch.object(u, 'keep_topmost_style', lambda hwnd, on=True: None),
            patch.object(u, 'lift_menu_windows', lambda extra=0: self.raised.append(extra)),
            patch.object(u, 'lift_owned_popups', lambda *a: None),
            patch.object(u, 'lift_tip_window', lambda *a: None),
        ]
        for spy in self.spies:
            spy.start()
        real = u.tk.Wm.attributes
        outer = self

        def attributes(inner, *args):
            if args and args[0] == '-topmost' and len(args) > 1 and args[1]:
                outer.restacks.append('attributes(-topmost)')
            return real(inner, *args)

        self.attr_patch = patch.object(u.tk.Wm, 'attributes', attributes)
        self.attr_patch.start()

    def tearDown(self):
        self.attr_patch.stop()
        for spy in self.spies:
            spy.stop()
        self.w.preview = True
        self.w.close()
        u.UpdatePill.animate = self._pill_animate
        for item in self.patches:
            item.stop()
        self.directory.cleanup()

    def callbacks(self):
        w = self.w
        for key in u.FETCHERS:
            w.enabled[key].set(True)
            w.snapshots[key] = snapshot(key, 70)
        return {
            'apply_topmost': w.apply_topmost,
            '_keep_widget_topmost': w._keep_widget_topmost,
            'overlay push/pop': lambda: (w.push_overlay(), w.pop_overlay()),
            'render': lambda: [w.render(key) for key in u.FETCHERS],
            'relayout': w.relayout,
            'apply_mode': lambda: (setattr(w, '_layout', None), w.apply_mode()),
            'refresh_clock': lambda: [c.refresh_clock(c._clock_second or 0) for c in w.cards.values()],
        }

    def test_no_callback_restacks_the_widget_while_the_menu_is_held(self):
        for compact in (True, False):
            self.w.compact = compact
            for name, callback in self.callbacks().items():
                with self.subTest(mode='compact' if compact else 'detail', callback=name):
                    self.w._menu_held = True
                    self.restacks.clear()
                    callback()
                    self.assertEqual(self.restacks, [],
                                     f'{name} re-stacked the widget over the open menu')
                    self.w._menu_held = False

    def test_the_same_callbacks_do_restack_once_the_menu_is_closed(self):
        # The guard must be about the menu, not a way to disable always-on-top.
        self.w._menu_held = False
        self.restacks.clear()
        self.w._keep_widget_topmost()
        self.assertTrue(self.restacks, 'always-on-top stopped being asserted')

    def test_holding_the_menu_keeps_re_raising_it(self):
        self.w._menu_held = True
        self.raised.clear()
        self.w.apply_topmost()
        self.w._keep_widget_topmost()
        self.assertEqual(len(self.raised), 2)
        # The menu's own window id is offered while it is held, because Tk
        # never reports a Windows popup menu as mapped.
        self.assertTrue(all(value for value in self.raised))
        self.w._menu_held = False

    def test_a_posted_menu_is_never_reported_as_mapped(self):
        # This is why the held flag, not winfo_ismapped(), drives the guard.
        self.assertFalse(self.w.menu.winfo_ismapped())

    def test_the_held_state_is_cleared_however_the_menu_closed(self):
        for reason in ('command', 'click outside', 'escape', 'focus loss'):
            with self.subTest(reason=reason):
                self.w._menu_held = True
                self.w._release_menu()
                self.assertFalse(self.w._menu_held)

    def test_closing_the_widget_does_not_leave_the_state_held(self):
        self.w._menu_held = True
        self.w.closing = True
        self.w._release_menu()
        self.assertFalse(self.w._menu_held)
        self.w.closing = False

    def test_popup_holds_and_then_releases_around_the_menu(self):
        posted = []
        with patch.object(self.w.menu, 'tk_popup',
                          side_effect=lambda *a: posted.append(self.w._menu_held)), \
             patch.object(self.w, '_arm_menu_raise'):
            self.w.popup(SimpleNamespace(x_root=10, y_root=10))
        self.assertEqual(posted, [True], 'the menu was posted without the hold in place')
        self.w.root.update()
        self.assertFalse(self.w._menu_held)

    def test_the_guard_applies_to_both_modes(self):
        for compact in (True, False):
            with self.subTest(mode='compact' if compact else 'detail'):
                self.w.compact = compact
                self.w._menu_held = True
                self.restacks.clear()
                self.w.relayout()
                self.w._keep_widget_topmost()
                self.assertEqual(self.restacks, [])
                self.w._menu_held = False


if __name__ == '__main__':
    unittest.main()
