"""Animation cost: the cached bar renderer, the shared frame clock, the ring cache."""
import random
import unittest
from tests.tk_support import destroy_root
from unittest.mock import patch

import bar_raster
import frame_clock as fc
import usage_widget as u
import widget_raster as raster
from providers import ProviderSnapshot, QuotaItem

# A client slower than FAST: the clock serves whatever interval it is asked for.
SLOW = 1 / 30


def reference(*args, **kwargs):
    width, height, rows = raster.progress_bar_rgba(*args, **kwargs)
    return width, height, [bytes(row) for row in rows]


def fast(*args, **kwargs):
    width, height, rows = bar_raster.progress_rgba(*args, glow=u.SHIMMER_GLOW, **kwargs)
    return width, height, [bytes(row) for row in rows]


class RasterIdentityTests(unittest.TestCase):
    """The cached renderer must draw exactly what every-pixel drawing does."""

    def test_boundary_lengths_thicknesses_and_phases(self):
        m = u.Metrics()
        shapes = (
            (m.card_w - m.p(32), m.bar_h + m.p(4) + 2, m.bar_h, m.bar_h + m.p(4)),
            (m.chip_w, m.chip_canvas_h, m.chip_h, m.chip_canvas_h),
        )
        for width, height, thin, thick in shapes:
            for percent in (0, 0.5, 50, 99.5, 100):
                for shape in (thin, (thin + thick) / 2, thick):
                    for phase in (None, 0.15, 0.3, 0.45, 0.74, 0.9):
                        args = (width, height, shape / 2, u.chip_fill_width(width, percent),
                                u.TRACK, u.CODEX, u.CARD)
                        with self.subTest(width=width, percent=percent, shape=shape, phase=phase):
                            self.assertEqual(fast(*args, shimmer=phase, shape_height=shape),
                                             reference(*args, shimmer=phase, shape_height=shape))

    def test_random_shapes_and_colours(self):
        rng = random.Random(20260923)
        for _ in range(250):
            width, height = rng.randint(1, 360), rng.randint(1, 24)
            args = (width, height, rng.uniform(0, 14), rng.choice([rng.uniform(0, width), rng.randint(0, width)]),
                    *('#%06x' % rng.randrange(1 << 24) for _ in range(3)))
            kwargs = {'shimmer': rng.choice([None, rng.random()]), 'shape_height': rng.uniform(0, height)}
            with self.subTest(args=args, kwargs=kwargs):
                self.assertEqual(fast(*args, **kwargs), reference(*args, **kwargs))

    def test_ringed_pill_keeps_round_ends(self):
        m = u.Metrics()
        width, height, ring_w = m.chip_w, m.chip_canvas_h, m.p(2, 1)
        bg, ring = (0x10, 0x20, 0x30), (0xF0, 0x40, 0x40)
        for shape in (m.chip_h, (m.chip_h + height) / 2, height):
            for percent in (0, 4, 87, 100):
                with self.subTest(shape=shape, percent=percent):
                    fill = u.chip_fill_width(width - 2 * ring_w, percent)
                    _, _, rows = bar_raster.ringed_progress_rgba(
                        width, height, shape, ring_w, '#F04040', fill, u.CHIP_TRACK, u.CODEX, '#102030')
                    _, _, outer = bar_raster.progress_rgba(
                        width, height, shape / 2, 0, '#F04040', '#F04040', '#102030', shape_height=shape)
                    for y in range(height):
                        for x in range(width):
                            outside = tuple(outer[y][x * 4:x * 4 + 3]) == bg
                            if outside:
                                # Nothing may show outside the ring, such as an inner square corner.
                                self.assertEqual(tuple(rows[y][x * 4:x * 4 + 3]), bg, (x, y))
                    # The ring shows on the top edge and at both round ends.
                    middle = int((height - shape) / 2) + 1
                    self.assertEqual(tuple(rows[middle][width // 2 * 4:width // 2 * 4 + 3]), ring)
                    self.assertEqual(tuple(rows[height // 2][4:7]), ring)
                    self.assertEqual(tuple(rows[height // 2][(width - 2) * 4:(width - 2) * 4 + 3]), ring)

    def test_photo_uses_the_cached_renderer(self):
        root = u.tk.Tk()
        root.withdraw()
        try:
            with patch.object(raster, 'progress_bar_png', side_effect=AssertionError('slow path')):
                photo = u.progress_photo(80, 10, 4, 40, u.TRACK, u.CODEX, u.CARD, shimmer=0.4, shape_height=8)
            self.assertEqual((photo.width(), photo.height()), (80, 10))
            with patch.object(raster, 'progress_bar_png', wraps=raster.progress_bar_png) as encode:
                photo = u.progress_photo(80, 10, 4, 40, u.TRACK, u.CODEX, u.CARD, samples=2)
                encode.assert_called_once()
            self.assertEqual((photo.width(), photo.height()), (80, 10))
        finally:
            destroy_root(root)


class FakeWidget:
    def __init__(self):
        self.pending = {}
        self.cancelled = []
        self.counter = 0

    def after(self, delay, callback):
        self.counter += 1
        self.pending[self.counter] = (delay, callback)
        return self.counter

    def after_cancel(self, ident):
        self.cancelled.append(ident)
        self.pending.pop(ident, None)


class Client:
    def __init__(self, interval=SLOW, cost=0.0, clock=None):
        self.interval = interval
        self.cost = cost
        self.clock = clock
        self.frames = []

    def _frame_interval(self):
        return self.interval

    def _frame(self, now):
        self.frames.append(now)
        self.clock[0] += self.cost


class FrameClockTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.patch = patch.object(fc.time, 'monotonic', side_effect=lambda: self.now[0])
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.widget = FakeWidget()
        self.clock = fc.FrameClock(self.widget)

    def fire(self, at):
        self.now[0] = at
        ident, (_, callback) = max(self.widget.pending.items())
        del self.widget.pending[ident]
        callback()

    def test_one_timer_serves_every_client(self):
        a, b = Client(clock=self.now), Client(fc.FAST, clock=self.now)
        self.clock.wake(a)
        self.clock.wake(b)
        self.assertEqual(len(self.widget.pending), 1)
        self.fire(100 + fc.FAST)
        self.assertEqual((len(a.frames), len(b.frames)), (0, 1))
        self.fire(100 + SLOW)
        self.assertEqual((len(a.frames), len(b.frames)), (1, 2))
        self.assertEqual(len(self.widget.pending), 1)

    def test_deadlines_follow_the_grid_and_late_frames_skip(self):
        slow = Client(SLOW, clock=self.now)
        self.clock.wake(slow)
        start = 100 + SLOW
        # 2.5 intervals late: draw once now, count the two skipped deadlines,
        # and keep the next deadline on the original 30 fps grid.
        self.fire(start + 2.5 * SLOW)
        self.assertEqual(len(slow.frames), 1)
        self.assertAlmostEqual(self.clock._due[slow], start + 3 * SLOW)
        stats = self.clock.stats.summary()['30fps']
        self.assertEqual((stats['frames'], stats['deadline_missed']), (1, 2))
        self.assertAlmostEqual(stats['lateness_ms_p95'], 2.5 * SLOW * 1000, delta=0.01)

    def test_a_timer_waking_just_before_the_deadline_still_draws(self):
        client = Client(SLOW, clock=self.now)
        self.clock.wake(client)
        self.fire(100 + SLOW - fc.EARLY / 2)
        self.assertEqual(len(client.frames), 1)
        self.assertAlmostEqual(self.clock._due[client], 100 + 2 * SLOW)
        self.assertEqual(self.clock.stats.summary()['30fps']['deadline_missed'], 0)

    def test_a_timer_waking_well_before_the_deadline_waits(self):
        client = Client(SLOW, clock=self.now)
        self.clock.wake(client)
        self.fire(100 + SLOW - 2 * fc.EARLY)
        self.assertEqual(client.frames, [])
        self.assertTrue(self.clock.scheduled(client))
        self.assertEqual(len(self.widget.pending), 1)

    def test_a_slow_frame_is_measured_against_the_next_deadline(self):
        heavy = Client(fc.FAST, cost=1.5 * fc.FAST, clock=self.now)
        self.clock.wake(heavy)
        self.fire(100 + fc.FAST)
        # The frame ran past the next deadline, so that one is skipped.
        self.assertAlmostEqual(self.clock._due[heavy], 100 + 3 * fc.FAST)
        self.assertEqual(self.clock.stats.summary()['60fps']['deadline_missed'], 1)

    def test_idle_clients_leave_and_the_timer_stops(self):
        client = Client(clock=self.now)
        self.clock.wake(client)
        client.interval = None
        self.fire(100 + SLOW)
        self.assertEqual(client.frames, [])
        self.assertFalse(self.clock.scheduled(client))
        self.assertEqual(self.widget.pending, {})
        self.clock.wake(client)
        self.assertEqual(self.widget.pending, {})

    def test_a_faster_need_brings_the_next_frame_forward(self):
        client = Client(SLOW, clock=self.now)
        self.clock.wake(client)
        client.interval = fc.FAST
        self.clock.wake(client)
        self.assertAlmostEqual(self.clock._due[client], 100 + fc.FAST)
        # Armed EARLY before the deadline, since Windows timers fire on a tick.
        self.assertEqual(self.widget.pending[max(self.widget.pending)][0], int((fc.FAST - fc.EARLY) * 1000))

    def test_release_cancels_the_last_timer(self):
        client = Client(clock=self.now)
        self.clock.wake(client)
        ident = max(self.widget.pending)
        self.clock.release(client)
        self.assertIn(ident, self.widget.cancelled)
        self.assertFalse(self.clock.scheduled(client))

    def test_a_failing_client_does_not_stop_the_others(self):
        good = Client(SLOW, clock=self.now)

        class Broken(Client):
            def _frame(self, now):
                raise ValueError('boom')

        bad = Broken(SLOW, clock=self.now)
        self.clock.wake(bad)
        self.clock.wake(good)
        with self.assertRaises(ValueError):
            self.fire(100 + SLOW)
        self.assertFalse(self.clock.scheduled(bad))
        self.assertTrue(self.clock.scheduled(good))
        self.assertEqual(len(self.widget.pending), 1)

    def test_destroyed_widgets_are_dropped_quietly(self):
        class Gone(Client):
            def _frame(self, now):
                raise u.tk.TclError('invalid command name')

        gone = Gone(clock=self.now)
        self.clock.wake(gone)
        self.fire(100 + SLOW)
        self.assertFalse(self.clock.scheduled(gone))


def snapshot(first=60, second=70):
    def q(raw, name, remaining, window):
        return QuotaItem(f'chatgpt:main:{raw}', 'chatgpt', 'main', name, raw_identifier=raw,
                         window_seconds=window, window_label=name, used_percent=100 - remaining,
                         remaining_percent=remaining, scope='global')
    return ProviderSnapshot('chatgpt', 'Codex', 'Plus', True, first, '',
                            main_limits=[q('primary_window', '5시간', first, 18000.0),
                                         q('secondary_window', '주간', second, 604800.0)])


class WidgetFrameTests(unittest.TestCase):
    def setUp(self):
        self.root = u.tk.Tk()
        self.root.withdraw()
        self.addCleanup(destroy_root, self.root)

    def frame(self, control, now, active=None):
        with patch.object(control, 'winfo_ismapped', return_value=True), \
                patch.object(u.time, 'monotonic', return_value=now):
            if active is not None:
                control.set_activity(active)
            control._frame(now)
            return control._frame_interval()

    def test_growth_and_the_sweep_both_run_at_60(self):
        card = u.Card(self.root, 'chatgpt')
        card.render(snapshot())
        self.assertEqual(self.frame(card, 10.0, True), fc.FAST)
        self.assertEqual(self.frame(card, 10.2, True), fc.FAST)
        self.assertEqual(self.frame(card, 10.4, True), fc.FAST)
        self.assertEqual(self.frame(card, 12.0, True), fc.FAST)
        self.assertEqual(self.frame(card, 12.1, False), fc.FAST)
        self.assertIsNone(self.frame(card, 13.5, False))

    def test_a_length_change_asks_for_60_until_it_lands(self):
        # A rise below a reset's size: no ring moment follows the length.
        card = u.Card(self.root, 'chatgpt')
        with patch.object(u.time, 'monotonic', return_value=10):
            card.render(snapshot(40))
            card.render(snapshot(50))
        self.assertEqual(card._frame_interval(), fc.FAST)
        self.frame(card, 12.5)
        self.assertEqual(card._shown_pcts[0], 50)
        self.assertIsNone(card._frame_interval())

    def test_cards_and_chips_share_one_clock(self):
        card, chip = u.Card(self.root, 'chatgpt'), u.Chip(self.root)
        self.assertIs(fc.clock_for(card), fc.clock_for(chip))
        self.assertIs(fc.clock_for(card), getattr(self.root, '_usage_frame_clock'))

    def test_one_frame_draws_once_even_while_both_move(self):
        card = u.Card(self.root, 'chatgpt')
        with patch.object(u.time, 'monotonic', return_value=10):
            card.render(snapshot(60))
            card.render(snapshot(40))
        with patch.object(card, '_paint_shimmer') as paint:
            self.frame(card, 10.1, True)
        paint.assert_called_once()

    def test_ring_growth_uses_a_few_cached_images(self):
        card = u.Card(self.root, 'chatgpt')
        card.render(snapshot())
        raster.ring_png.cache_clear()
        signatures = set()
        for step in range(40):
            self.frame(card, 10 + step * 0.01, True)
            signatures.add(card._ring_signature)
        self.assertLessEqual(len(signatures), u.RING_EMPHASIS_STEPS + 1)
        before = raster.ring_png.cache_info()
        card._ring_signature = None
        card._paint_ring()
        self.assertEqual(raster.ring_png.cache_info().hits, before.hits + 1)


class SteadySweepTests(unittest.TestCase):
    """Once the light sweeps at a steady thickness, a frame redraws only the bars."""

    def setUp(self):
        self.root = u.tk.Tk()
        self.root.withdraw()
        self.addCleanup(destroy_root, self.root)
        self.cache = u.FrameCache()
        cache = patch.object(u, 'SHIMMER_CACHE', self.cache)
        cache.start()
        self.addCleanup(cache.stop)

    def frame(self, control, now):
        with patch.object(control, 'winfo_ismapped', return_value=True), \
                patch.object(u.time, 'monotonic', return_value=now):
            control.set_activity(True)
            control._frame(now)

    def sweep(self, control, start):
        for step in range(73):
            self.frame(control, start + step / 60)

    def steady_card(self):
        card = u.Card(self.root, 'chatgpt')
        card.render(snapshot())
        for step in range(100):  # grown, and every length landed
            self.frame(card, 10 + step * 0.02)
        return card

    def test_a_steady_frame_leaves_the_ring_and_number_alone(self):
        card = self.steady_card()
        tags = []
        configure = card.rows.itemconfigure

        def spy(tag, *args, **kwargs):
            tags.append(tag)
            return configure(tag, *args, **kwargs)

        with patch.object(card.rows, 'itemconfigure', side_effect=spy):
            self.sweep(card, 20)
        # The hero quota is the ring; the weekly one is drawn as bar_1.
        self.assertIn('bar_1', tags)
        self.assertNotIn('ring', tags)
        self.assertNotIn('hero', tags)

    def test_a_repaint_shows_the_ring_and_number_again(self):
        card = self.steady_card()
        card._paint(card._snap, card._shown_pcts)
        self.assertTrue(card.rows.itemcget('ring', 'image'))
        self.assertEqual(card.rows.itemcget('hero', 'text'), '60%')

    def test_the_next_sweep_draws_no_new_frame(self):
        card = self.steady_card()
        self.sweep(card, 20)
        with patch.object(u, 'progress_png', side_effect=AssertionError('drawn again')):
            self.sweep(card, 20 + u.SHIMMER_SWEEP_S)

    def test_a_steady_chip_reuses_frames_with_or_without_its_warning_ring(self):
        for warning in (None, u.WARN):
            with self.subTest(warning=warning):
                chip = u.Chip(self.root)
                chip.configure(text='GPT 60%', percent=60, bg=u.CHIP_CODEX)
                chip._warning_color = warning
                for step in range(100):
                    self.frame(chip, 10 + step * 0.02)
                self.sweep(chip, 20)
                with patch.object(u, 'progress_png', side_effect=AssertionError('drawn again')), \
                        patch.object(u, 'ringed_progress_rgba', side_effect=AssertionError('drawn again')):
                    self.sweep(chip, 20 + u.SHIMMER_SWEEP_S)

    def test_frames_while_the_bar_grows_are_not_cached(self):
        card = u.Card(self.root, 'chatgpt')
        card.render(snapshot())
        before = len(self.cache)
        for step in range(10):  # half of the 0.4 s growth
            self.frame(card, 10 + step * 0.02)
        self.assertIsNone(card._frame_cache())
        self.assertEqual(len(self.cache), before)

    def test_the_light_lands_on_fixed_positions(self):
        card = self.steady_card()
        for now in (20.0, 20.013, 20.3337, 20.9, 21.19):
            with patch.object(u.time, 'monotonic', return_value=now):
                phase = card._shimmer_phase_for(0)
            steps = (phase - 0.15) / 0.60 * u.SHIMMER_PHASES
            self.assertAlmostEqual(steps, round(steps), places=6)

    def test_the_cache_keeps_the_most_recent_frames(self):
        cache = u.FrameCache(limit=3)
        for key in 'abcd':
            cache.get(key, key.encode)
        self.assertEqual(len(cache), 3)
        drawn = []
        cache.get('b', lambda: drawn.append('b') or b'b')
        cache.get('a', lambda: drawn.append('a') or b'a')
        self.assertEqual(drawn, ['a'])

    def test_bars_that_look_alike_get_their_own_images(self):
        # One shared image would carry one bar's light onto the other.
        args = (80, 10, 4, 40, u.TRACK, u.CODEX, u.CARD)
        first = u.progress_photo(*args, shimmer=0.4, shape_height=8, cache=self.cache)
        second = u.progress_photo(*args, shimmer=0.4, shape_height=8, cache=self.cache)
        self.assertIsNot(first, second)
        self.assertEqual(len(self.cache), 1)


if __name__ == '__main__':
    unittest.main()
