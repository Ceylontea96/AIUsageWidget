"""Animation cost: the cached bar renderer, the shared frame clock, the ring cache."""
import random
import unittest
from unittest.mock import patch

import bar_raster
import frame_clock as fc
import usage_widget as u
from providers import ProviderSnapshot, QuotaItem


def reference(*args, **kwargs):
    width, height, rows = u.progress_bar_rgba(*args, **kwargs)
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

    def test_photo_uses_the_cached_renderer(self):
        root = u.tk.Tk()
        root.withdraw()
        try:
            with patch.object(u, 'progress_bar_png', side_effect=AssertionError('slow path')):
                photo = u.progress_photo(80, 10, 4, 40, u.TRACK, u.CODEX, u.CARD, shimmer=0.4, shape_height=8)
            self.assertEqual((photo.width(), photo.height()), (80, 10))
        finally:
            root.destroy()


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
    def __init__(self, interval=fc.SLOW, cost=0.0, clock=None):
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
        self.fire(100 + fc.SLOW)
        self.assertEqual((len(a.frames), len(b.frames)), (1, 2))
        self.assertEqual(len(self.widget.pending), 1)

    def test_deadlines_follow_the_grid_and_late_frames_skip(self):
        slow = Client(fc.SLOW, clock=self.now)
        self.clock.wake(slow)
        start = 100 + fc.SLOW
        # 2.5 intervals late: draw once now, count the two skipped deadlines,
        # and keep the next deadline on the original 30 fps grid.
        self.fire(start + 2.5 * fc.SLOW)
        self.assertEqual(len(slow.frames), 1)
        self.assertAlmostEqual(self.clock._due[slow], start + 3 * fc.SLOW)
        stats = self.clock.stats.summary()['30fps']
        self.assertEqual((stats['frames'], stats['deadline_missed']), (1, 2))
        self.assertAlmostEqual(stats['lateness_ms_p95'], 2.5 * fc.SLOW * 1000, delta=0.01)

    def test_a_timer_waking_just_before_the_deadline_still_draws(self):
        client = Client(fc.SLOW, clock=self.now)
        self.clock.wake(client)
        self.fire(100 + fc.SLOW - fc.EARLY / 2)
        self.assertEqual(len(client.frames), 1)
        self.assertAlmostEqual(self.clock._due[client], 100 + 2 * fc.SLOW)
        self.assertEqual(self.clock.stats.summary()['30fps']['deadline_missed'], 0)

    def test_a_timer_waking_well_before_the_deadline_waits(self):
        client = Client(fc.SLOW, clock=self.now)
        self.clock.wake(client)
        self.fire(100 + fc.SLOW - 2 * fc.EARLY)
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
        self.fire(100 + fc.SLOW)
        self.assertEqual(client.frames, [])
        self.assertFalse(self.clock.scheduled(client))
        self.assertEqual(self.widget.pending, {})
        self.clock.wake(client)
        self.assertEqual(self.widget.pending, {})

    def test_a_faster_need_brings_the_next_frame_forward(self):
        client = Client(fc.SLOW, clock=self.now)
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
        good = Client(fc.SLOW, clock=self.now)

        class Broken(Client):
            def _frame(self, now):
                raise ValueError('boom')

        bad = Broken(fc.SLOW, clock=self.now)
        self.clock.wake(bad)
        self.clock.wake(good)
        with self.assertRaises(ValueError):
            self.fire(100 + fc.SLOW)
        self.assertFalse(self.clock.scheduled(bad))
        self.assertTrue(self.clock.scheduled(good))
        self.assertEqual(len(self.widget.pending), 1)

    def test_destroyed_widgets_are_dropped_quietly(self):
        class Gone(Client):
            def _frame(self, now):
                raise u.tk.TclError('invalid command name')

        gone = Gone(clock=self.now)
        self.clock.wake(gone)
        self.fire(100 + fc.SLOW)
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
        self.addCleanup(self.root.destroy)

    def frame(self, control, now, active=None):
        with patch.object(control, 'winfo_ismapped', return_value=True), \
                patch.object(u.time, 'monotonic', return_value=now):
            if active is not None:
                control.set_activity(active)
            control._frame(now)
            return control._frame_interval()

    def test_growth_runs_at_60_and_the_sweep_alone_at_30(self):
        card = u.Card(self.root, 'chatgpt')
        card.render(snapshot())
        self.assertEqual(self.frame(card, 10.0, True), fc.FAST)
        self.assertEqual(self.frame(card, 10.2, True), fc.FAST)
        self.assertEqual(self.frame(card, 10.4, True), fc.SLOW)
        self.assertEqual(self.frame(card, 12.0, True), fc.SLOW)
        self.assertEqual(self.frame(card, 12.1, False), fc.FAST)
        self.assertIsNone(self.frame(card, 13.5, False))

    def test_a_length_change_asks_for_60_until_it_lands(self):
        card = u.Card(self.root, 'chatgpt')
        with patch.object(u.time, 'monotonic', return_value=10):
            card.render(snapshot(60))
            card.render(snapshot(40))
        self.assertEqual(card._frame_interval(), fc.FAST)
        self.frame(card, 12.5)
        self.assertEqual(card._shown_pcts[0], 40)
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
        u.ring_png.cache_clear()
        signatures = set()
        for step in range(40):
            self.frame(card, 10 + step * 0.01, True)
            signatures.add(card._ring_signature)
        self.assertLessEqual(len(signatures), u.RING_EMPHASIS_STEPS + 1)
        before = u.ring_png.cache_info()
        card._ring_signature = None
        card._paint_ring()
        self.assertEqual(u.ring_png.cache_info().hits, before.hits + 1)


if __name__ == '__main__':
    unittest.main()
