"""Opening the widget: it fades in while each card's ring, bars and number fill from zero."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import usage_widget as u
import widget_cards as cards
import widget_raster as raster
from providers import ProviderSnapshot, QuotaItem
from tests.tk_support import destroy_root


def snapshot(key='chatgpt', first=80, second=45, stale=False):
    def q(raw, name, remaining, window):
        return QuotaItem(f'{key}:main:{raw}', key, 'main', name, raw_identifier=raw,
                         window_seconds=window, window_label=name, used_percent=100 - remaining,
                         remaining_percent=remaining, reset_at=2000000000, scope='global')
    return ProviderSnapshot(key, u.TITLES[key], 'Plus', True, first, '', stale=stale,
                            main_limits=[q('primary_window', '5시간', first, 18000.0),
                                         q('secondary_window', '주간', second, 604800.0)])


def at(now, call, *args, **kwargs):
    with patch.object(u.time, 'monotonic', return_value=now):
        return call(*args, **kwargs)


class CardEntranceTests(unittest.TestCase):
    def setUp(self):
        self.root = u.tk.Tk()
        self.root.withdraw()
        self.addCleanup(destroy_root, self.root)
        self.card = u.Card(self.root, 'chatgpt')
        self.card.render(snapshot())

    def frames(self, *times):
        values = []
        for now in times:
            at(now, self.card._frame, now)
            values.append((list(self.card._shown_pcts), self.card._hero_shown))
        return values

    def test_fills_from_zero_and_lands_on_the_values(self):
        card = self.card
        targets, hero = list(card._shown_pcts), card._hero_shown
        self.assertTrue(at(10.0, card.enter))
        self.assertTrue(card.entering)
        self.assertEqual(card._shown_pcts, [0.0, 0.0])
        self.assertEqual(card.rows.itemcget('hero', 'text'), '0%')
        steps = [10.0 + k / cards.ENTRANCE_FPS for k in range(0, 40, 4)]
        values = self.frames(*steps)
        heroes = [h for _, h in values]
        self.assertEqual(heroes[0], 0.0)
        self.assertTrue(all(a < b for a, b in zip(heroes, heroes[1:-1])), heroes)
        # Ease-out: the first stretch covers more than the last.
        self.assertGreater(heroes[1] - heroes[0], heroes[-2] - heroes[-3])
        self.frames(10.0 + cards.ENTRANCE_S + 0.05)
        self.assertFalse(card.entering)
        self.assertEqual(card._shown_pcts, targets)
        self.assertEqual(card._hero_shown, hero)
        self.assertEqual(card.rows.itemcget('hero', 'text'), '80%')

    def test_a_later_card_waits_at_zero(self):
        at(10.0, self.card.enter, 0.18)
        values = self.frames(10.05, 10.17)
        self.assertEqual(values, [([0.0, 0.0], 0.0)] * 2)
        self.frames(10.3)
        self.assertGreater(self.card._hero_shown, 0.0)

    def test_after_warming_its_frames_draw_no_new_ring(self):
        card = self.card
        raster.ring_png.cache_clear()
        card.warm_entrance()
        warmed = raster.ring_png.cache_info().misses
        at(10.0, card.enter)
        self.frames(*[10.0 + k / cards.ENTRANCE_FPS for k in range(0, 40)])
        self.assertFalse(card.entering)
        self.assertEqual(raster.ring_png.cache_info().misses, warmed)

    def test_new_data_mid_entrance_moves_on_without_a_reset_flash(self):
        card = self.card
        at(10.0, card.enter)
        self.frames(10.0, 10.1)
        partial = card._hero_shown
        self.assertGreater(partial, 0.0)
        at(10.1, card.render, snapshot(first=78))
        self.assertFalse(card.entering)
        self.assertIsNone(card._refill_t0)
        self.assertEqual(card._hero_shown, partial)
        self.frames(10.2, 11.5)
        self.assertEqual(card._hero_shown, 78)

    def test_no_light_or_thickening_while_it_fills(self):
        card = self.card
        at(10.0, card.enter)
        with patch.object(card, 'winfo_ismapped', return_value=True):
            at(10.0, card.set_activity, True)
            self.frames(10.1, 10.3)
            self.assertFalse(card._shimmer_ready())
            self.assertEqual(card._emphasis, 0.0)
            self.frames(10.7, 10.9)
            self.assertTrue(card._shimmer_ready())
            self.assertGreater(card._emphasis, 0.0)

    def test_a_folded_card_has_nothing_to_fill(self):
        self.card.set_collapsed(True)
        self.assertFalse(self.card.enter())


class WidgetEntranceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        for guard in (
            patch.object(u, 'SETTINGS_PATH', base / 'settings.json'),
            patch.object(u, 'CACHE_PATH', base / 'cache.json'),
            patch.object(u, 'login_present', return_value=False),
            patch('claude_integration.ensure_bridge_copy'),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        self.w = u.UsageWidget(preview=True)
        self.addCleanup(self.w.close)
        for key in u.FETCHERS:
            self.w.enabled[key].set(True)
            self.w.snapshots[key] = replace(snapshot(key), stale=True)
            self.w.render(key)

    def alpha(self):
        return float(self.w.root.attributes('-alpha'))

    def test_preview_opens_at_once(self):
        self.assertFalse(self.w.entrance)
        self.assertEqual(self.alpha(), 1.0)

    def test_the_window_fades_in_first_then_the_cards_fill_in_turn(self):
        w = self.w
        at(10.0, w._enter)
        first = u.ENTRANCE_FADE_S + u.ENTRANCE_SETTLE_S
        starts = [w.cards[k]._enter_t0 for k in u.FETCHERS]
        self.assertEqual([round(s - 10.0, 3) for s in starts],
                         [round(first + i * u.ENTRANCE_STAGGER_S, 3) for i in range(3)])
        at(10.11, w._fade_in)
        self.assertTrue(0.0 < self.alpha() < 1.0)
        # Opaque, and so no longer a layered window, before any card moves.
        at(10.0 + u.ENTRANCE_FADE_S, w._fade_in)
        self.assertEqual(self.alpha(), 1.0)
        self.assertTrue(all(w.cards[k].entering for k in u.FETCHERS))
        self.assertEqual(w.cards['chatgpt']._shown_pcts, [0.0, 0.0])


if __name__ == '__main__':
    unittest.main()
