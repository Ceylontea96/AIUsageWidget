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


class InPlaceLayoutTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
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
            self.addCleanup(guard.stop)
        self.w = u.UsageWidget(preview=True)
        self.addCleanup(self.w.close)
        for key in u.FETCHERS:
            self.w.enabled[key].set(True)
            self.w.snapshots[key] = snapshot(key)
            self.w.render(key)
        self.w.root.update_idletasks()

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


if __name__ == '__main__':
    unittest.main()
