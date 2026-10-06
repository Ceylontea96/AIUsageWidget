"""Folding a card: the chevron turns, and the window moves things in place instead of rebuilding."""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import usage_widget as u
import widget_raster as raster
from providers import ProviderSnapshot, QuotaItem
from tests.tk_support import destroy_root


def snapshot(key='chatgpt', first=60, second=70):
    def q(raw, name, remaining, window):
        return QuotaItem(f'{key}:main:{raw}', key, 'main', name, raw_identifier=raw,
                         window_seconds=window, window_label=name, used_percent=100 - remaining,
                         remaining_percent=remaining, reset_at=2000000000, scope='global')
    return ProviderSnapshot(key, u.TITLES[key], 'Plus', True, first, '',
                            main_limits=[q('primary_window', '5시간', first, 18000.0),
                                         q('secondary_window', '주간', second, 604800.0)])


def at(now, call, *args, **kwargs):
    with patch.object(u.time, 'monotonic', return_value=now):
        return call(*args, **kwargs)


class ChevronTests(unittest.TestCase):
    def test_turns_between_its_two_points(self):
        right, down = raster.chevron_png(11, False, u.ICON, 1.6), raster.chevron_png(11, True, u.ICON, 1.6)
        self.assertEqual(raster.chevron_png(11, 0.0, u.ICON, 1.6), right)
        self.assertEqual(raster.chevron_png(11, 1.0, u.ICON, 1.6), down)
        half = raster.chevron_png(11, 0.5, u.ICON, 1.6)
        self.assertNotIn(half, (right, down))
        # Turned, not redrawn smaller: about as much ink as either end.
        ink = lambda png: sum(sum(row[3::4]) for row in raster.read_png_rgba(png)[2])
        self.assertAlmostEqual(ink(half), ink(down), delta=0.15 * ink(down))


class CardTurnTests(unittest.TestCase):
    def setUp(self):
        self.root = u.tk.Tk()
        self.root.withdraw()
        self.addCleanup(destroy_root, self.root)
        self.card = u.Card(self.root, 'chatgpt', on_toggle=lambda: None)
        at(5.0, self.card.render, snapshot())

    def turns(self, *times):
        result = []
        for now in times:
            at(now, self.card._frame, now)
            result.append(self.card._chevron_turn)
        return result

    def test_rows_change_at_once_while_the_chevron_turns(self):
        card = self.card
        open_h = card.height
        self.assertEqual(card._chevron_turn, 1.0)
        at(10.0, card.set_collapsed, True, animate=True)
        self.assertLess(card.height, open_h)
        self.assertTrue(card.rows.find_withtag('collapsed_summary'))
        turns = self.turns(10.02, 10.06, 10.1, 10.14, 10.25)
        # The first frame starts the clock, so painting the rows eats none of the turn.
        self.assertEqual(turns[0], 1.0)
        self.assertEqual(turns[-1], 0.0)
        self.assertTrue(any(0 < turn < 1 for turn in turns), turns)
        self.assertTrue(all(a >= b for a, b in zip(turns, turns[1:])), turns)
        for turn in turns:
            self.assertEqual(turn * u.CHEVRON_TURN_STEPS, round(turn * u.CHEVRON_TURN_STEPS))
        self.assertIsNone(card._frame_interval())

    def test_a_click_mid_turn_turns_back_from_where_it_is(self):
        card = self.card
        at(10.0, card.set_collapsed, True, animate=True)
        mid = self.turns(10.02, 10.08)[-1]
        self.assertTrue(0 < mid < 1, mid)
        at(10.08, card.set_collapsed, False, animate=True)
        turns = self.turns(10.1, 10.14, 10.2, 10.35)
        self.assertEqual(turns[0], mid)
        self.assertTrue(all(a <= b for a, b in zip(turns, turns[1:])), turns)
        self.assertEqual(turns[-1], 1.0)

    def test_without_animation_the_chevron_points_at_once(self):
        self.card.set_collapsed(True)
        self.assertEqual(self.card._chevron_turn, 0.0)
        self.assertIsNone(self.card._turn_t0)


def open_widget(test):
    """A preview widget showing all three cards, closed when the test ends."""
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    base = Path(directory.name)
    for guard in (
        patch.object(u, 'SETTINGS_PATH', base / 'settings.json'),
        patch.object(u, 'CACHE_PATH', base / 'cache.json'),
        patch.object(u, 'login_present', return_value=False),
        patch('claude_integration.ensure_bridge_copy'),
        patch.object(u, 'work_area', return_value=(0, 0, 1920, 1080)),
        patch.object(u, 'monitor_area', return_value=(0, 0, 1920, 1080)),
    ):
        guard.start()
        test.addCleanup(guard.stop)
    w = u.UsageWidget(preview=True)
    test.addCleanup(w.close)
    for key in u.FETCHERS:
        w.enabled[key].set(True)
        w.snapshots[key] = snapshot(key)
        w.render(key)
    w.root.update_idletasks()
    return w


class InPlaceLayoutTests(unittest.TestCase):
    def setUp(self):
        self.w = open_widget(self)

    def taken_down(self, stack):
        w = self.w
        return [stack.enter_context(patch.object(item, name, wraps=getattr(item, name)))
                for item in (*w.cards.values(), w.header, w.body_view, w.footer)
                for name in ('pack_forget', 'place_forget')]

    def test_folding_a_card_moves_everything_in_place(self):
        # Taking the cards, header and footer down and up again made Tk repaint
        # the whole window: a blink each time a card folded or a row came or went.
        w = self.w
        before = int(w.shell.cget('height'))
        with ExitStack() as stack:
            calls = self.taken_down(stack)
            w.toggle_card('chatgpt')
        self.assertFalse([call for call in calls if call.called])
        height = int(w.shell.cget('height'))
        self.assertLess(height, before)
        self.assertEqual(int(w.footer.place_info()['y']), height - w.metrics.footer_h - 1)
        self.assertTrue(w.cards['chatgpt'].rows.find_withtag('collapsed_summary'))

    def test_a_new_mode_still_lays_out_from_scratch(self):
        w = self.w
        with ExitStack() as stack:
            calls = self.taken_down(stack)
            w.toggle()
        self.assertTrue([call for call in calls if call.called])
        self.assertEqual(int(w.shell.cget('height')), w.metrics.compact_h)


class FakeCurtain:
    def __init__(self, events, works=True):
        self.events, self.works = events, works

    def cover(self, owner):
        self.events.append(('cover',))
        return self.works

    def extend(self, height, split, top=None):
        self.events.append(('extend', height, split, top))
        return True

    def uncover(self):
        self.events.append(('uncover',))


class HeldStillTests(unittest.TestCase):
    """A fold shows as one change: a still copy covers the widget until it is painted."""

    def setUp(self):
        w = self.w = open_widget(self)
        w.root.update()                    # mapped, so the parts can be painted
        w.preview = False
        # Windows 10's way: a region rounds the window. Windows 11's is tested below.
        w._dwm_corners = False
        self.events = []
        w._curtain = FakeCurtain(self.events)
        paint = w._paint_now

        def painted(key=None):
            self.events.append(('paint', key))
            paint(key)

        w._paint_now = painted
        region = lambda hwnd, rgn, redraw: self.events.append(('region', bool(redraw))) or 1
        for guard in (
            patch.object(w.root, 'winfo_viewable', return_value=True),
            patch.object(w.root, 'attributes'),
            patch.object(u, 'set_over_taskbar', side_effect=lambda *a: self.events.append(('restack',))),
            patch.object(u, 'lift_tip_window'),
            patch.object(u.ctypes.windll.user32, 'SetWindowRgn', side_effect=region),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def names(self):
        return [event[0] for event in self.events if event[0] != 'extend']

    def test_a_fold_is_painted_in_full_before_the_copy_goes(self):
        self.w.toggle_card('chatgpt')
        self.assertTrue(self.w.cards['chatgpt'].collapsed)
        self.assertEqual(self.names(), ['cover', 'region', 'paint', 'uncover', 'restack'])
        # Painted in full under the copy, so the new outline needs no repaint of its own.
        self.assertIn(('region', False), self.events)
        self.assertIn(('paint', 'chatgpt'), self.events)
        self.assertIsNone(self.w._held_split)

    def test_unfolding_opens_the_space_below_the_card_first(self):
        w = self.w
        card = w.cards['cursor']
        w.toggle_card('cursor')
        split = card.winfo_rooty() + card.winfo_height() - w.root.winfo_rooty()
        self.events.clear()
        w.toggle_card('cursor')
        self.assertFalse(card.collapsed)
        grow = [event[:3] for event in self.events if event[0] == 'extend']
        self.assertEqual(grow, [('extend', int(w.shell.cget('height')), split)])
        self.assertLess(self.events.index(next(e for e in self.events if e[0] == 'extend')),
                        self.events.index(('paint', 'cursor')))

    def test_growing_off_the_bottom_moves_the_copy_and_the_window_up_together(self):
        w = self.w
        w.toggle_card('cursor')
        folded = int(w.shell.cget('height'))
        self.events.clear()
        # Sitting on the bottom of the screen; taller, relayout lifts it to stay on screen.
        with patch.object(w.root, 'winfo_x', return_value=100), \
                patch.object(w.root, 'winfo_y', return_value=1080 - folded), \
                patch.object(w.root, 'geometry', wraps=w.root.geometry) as geometry:
            w.toggle_card('cursor')
        height = int(w.shell.cget('height'))
        top = (100, 1080 - height)
        grow = [event for event in self.events if event[0] == 'extend']
        self.assertEqual(grow[0][3], top)
        # The window took its new size and place in one step, not grown and then lifted.
        self.assertIn(f'{w.metrics.window_w}x{height}+{top[0]}+{top[1]}',
                      [call.args[0] for call in geometry.call_args_list if call.args])

    def test_where_windows_rounds_the_window_no_region_is_set(self):
        self.w._dwm_corners = True
        self.w.toggle_card('chatgpt')
        self.w.toggle_card('chatgpt')
        self.assertNotIn('region', self.names())
        self.assertEqual(self.names()[:3], ['cover', 'paint', 'uncover'])

    def test_without_a_copy_the_card_folds_as_before(self):
        self.w._curtain.works = False
        self.w.toggle_card('chatgpt')
        self.assertTrue(self.w.cards['chatgpt'].collapsed)
        self.assertEqual(self.names(), ['cover', 'region', 'restack'])
        self.assertIn(('region', True), self.events)

    def test_an_error_mid_fold_still_takes_the_copy_away(self):
        with patch.object(self.w, 'relayout', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.w.toggle_card('chatgpt')
        self.assertEqual(self.names()[-1], 'uncover')
        self.assertIsNone(self.w._held_split)

    def test_only_the_card_and_what_moved_below_it_repaint(self):
        w = self.w
        exposed = []
        w.root.bind_all('<Expose>', lambda event: exposed.append(event.widget), add='+')
        w._paint_now('cursor')
        for widget in (w.cards['cursor'].rows, w.cards['claude'].rows, w.footer, w.body):
            self.assertIn(widget, exposed)
        for widget in (w.cards['chatgpt'].rows, w.header):
            self.assertNotIn(widget, exposed)

    def test_extra_rows_opening_are_held_still_too(self):
        self.w.toggle_additional('claude')
        self.assertEqual(self.names()[0], 'cover')
        self.assertEqual(self.names()[-2:], ['uncover', 'restack'])

    def test_the_preview_never_covers(self):
        self.w.preview = True
        self.w.toggle_card('chatgpt')
        self.assertNotIn(('cover',), self.events)


if __name__ == '__main__':
    unittest.main()
