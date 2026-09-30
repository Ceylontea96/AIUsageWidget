"""The card ring's one-off moments: quota just used, a reset, turning red."""
import math
import unittest
from unittest.mock import patch

import usage_widget as u
import widget_raster as raster
from providers import ProviderSnapshot, QuotaItem
from tests.tk_support import destroy_root

CARD, TRACK = '#101316', '#202327'


def reference_ring_png(size, percent, color, thickness, background, track):
    """The ring as it was drawn before the moments existed."""
    radius, half = size*35/84, thickness/2
    fraction = max(0,min(100,percent))/100
    end = fraction*math.tau
    ex,ey=math.sin(end)*radius,-math.cos(end)*radius
    bg,track,fg=raster.hex_rgb(background),raster.hex_rgb(track),raster.hex_rgb(color)
    track_palette=[bytes([round(b+(t-b)*i/255) for b,t in zip(bg,track)]+[255]) for i in range(256)]
    fill_palette=[bytes([round(b+(f-b)*i/255) for b,f in zip(bg,fg)]+[255]) for i in range(256)]
    rows=[bytearray(bytes(bg)+b'\xff')*size for _ in range(size)]
    for y,x,distance,angle,dx,dy in raster.ring_geometry(size):
        cov=max(0,min(255,int((half+.5-distance)*255)))
        if not cov:
            continue
        if fraction>=1 or 0<fraction and angle<=end:
            pixel=fill_palette[cov]
        else:
            cap=min(math.hypot(dx,dy+radius),math.hypot(dx-ex,dy-ey)) if fraction>0 else size
            arc=max(0,min(255,int((half+.5-cap)*255)))
            if arc:
                base=track_palette[cov]
                pixel=bytes([round(base[i]+(fg[i]-base[i])*arc/255) for i in range(3)]+[255])
            else:
                pixel=track_palette[cov]
        rows[y][x:x+4]=pixel
    return raster.png_rgba(size,size,rows)


def at_percent(data, percent, size=84, margin=0):
    """The pixel on the ring's centre line at `percent` of the way round."""
    _, _, rows = raster.read_png_rgba(data)
    angle = percent / 100 * math.tau
    center = size / 2 + margin
    x = int(center + math.sin(angle) * size * 35 / 84)
    y = int(center - math.cos(angle) * size * 35 / 84)
    return tuple(rows[y][x * 4:x * 4 + 4])


class RingRasterTests(unittest.TestCase):
    def test_a_plain_ring_is_drawn_exactly_as_before(self):
        for size in (84, 105):
            for percent in (0, 0.5, 42, 99.5, 100):
                for thickness in (7, 9, 10):
                    args = (size, percent, '#C96442', thickness, CARD, TRACK)
                    with self.subTest(args=args):
                        self.assertEqual(raster.ring_png(*args), reference_ring_png(*args))

    def test_the_trail_lies_between_the_ring_and_where_it_was(self):
        data = raster.ring_png(84, 50, '#C96442', 7, CARD, TRACK, ghost=70, ghost_color='#E4B1A0')
        self.assertEqual(at_percent(data, 30)[:3], raster.hex_rgb('#C96442'))
        self.assertEqual(at_percent(data, 60)[:3], raster.hex_rgb('#E4B1A0'))
        self.assertEqual(at_percent(data, 85)[:3], raster.hex_rgb(TRACK))

    def test_a_ripple_spreads_into_a_see_through_margin(self):
        plain = raster.read_png_rgba(raster.ring_png(84, 100, '#C96442', 7, CARD, TRACK))[2]
        width, height, rows = raster.read_png_rgba(
            raster.ring_png(84, 100, '#C96442', 7, CARD, TRACK, ripple=(46.0, 0.6), ripple_color='#C96442', margin=8))
        self.assertEqual((width, height), (100, 100))
        # The ring itself is unchanged inside the margin.
        self.assertEqual(rows[8 + 42][8 * 4:(8 + 84) * 4], plain[42])
        # Straight above the centre, 46 px out: the circle, half see-through.
        pixel = tuple(rows[50 - 46][50 * 4:50 * 4 + 4])
        self.assertEqual(pixel[:3], raster.hex_rgb('#C96442'))
        self.assertTrue(0 < pixel[3] < 255)
        # The corner of the margin stays fully transparent.
        self.assertEqual(rows[0][3], 0)


def quota(raw, name, remaining, window):
    return QuotaItem(f'chatgpt:main:{raw}', 'chatgpt', 'main', name, raw_identifier=raw,
                     window_seconds=window, window_label=name, used_percent=100 - remaining,
                     remaining_percent=remaining, scope='global')


def snap(five, week=90, stale=False, windows=('primary_window', 'secondary_window')):
    """GPT with a 5-hour hero window; the ring always shows it."""
    limits = {'primary_window': quota('primary_window', '5시간', five, 18000.0),
              'secondary_window': quota('secondary_window', '주간', week, 604800.0)}
    return ProviderSnapshot('chatgpt', 'Codex', 'Plus', True, five, '', stale=stale,
                            main_limits=[limits[key] for key in windows])


class RingMomentTests(unittest.TestCase):
    def setUp(self):
        self.root = u.tk.Tk()
        self.root.withdraw()
        self.addCleanup(destroy_root, self.root)
        self.m = u.Metrics()

    def show(self, card, snapshot, now):
        with patch.object(u.time, 'monotonic', return_value=now):
            card.render(snapshot)

    def frame(self, card, now):
        with patch.object(card, 'winfo_ismapped', return_value=True), \
                patch.object(u.time, 'monotonic', return_value=now):
            card._frame(now)

    def frames(self, card, start, end):
        steps = int(round((end - start) * 60))
        for step in range(steps + 1):
            self.frame(card, start + step / 60)

    def card(self, first):
        card = u.Card(self.root, 'chatgpt')
        self.show(card, first, 10.0)
        return card

    def test_quota_just_used_leaves_a_trail_that_shrinks_away(self):
        card = self.card(snap(80))
        self.show(card, snap(72), 11.0)
        self.assertEqual(card._ghost, 80)
        self.frames(card, 11.0, 11.5)
        self.assertEqual(card._ghost, 80, 'the used part holds before it shrinks')
        self.assertEqual(card._ring_signature[4], 80.0)
        self.frames(card, 11.6, 13.0)
        self.assertIsNone(card._ghost)
        self.assertIsNone(card._ring_signature[4])
        self.assertIsNone(card._frame_interval())

    def test_a_second_use_while_the_trail_runs_keeps_its_top(self):
        card = self.card(snap(80))
        self.show(card, snap(72), 11.0)
        self.frames(card, 11.0, 11.2)
        self.show(card, snap(64), 11.3)
        self.assertEqual(card._ghost, 80)

    def test_a_reset_refills_slowly_flashes_and_spreads_a_circle(self):
        card = self.card(snap(20))
        self.show(card, snap(100), 11.0)
        self.assertIsNotNone(card._refill_t0)
        self.frame(card, 11.0)
        base = u.raster.blend(u.ACCENTS['chatgpt'], '#FFFFFF', 0)
        self.assertNotEqual(card._ring_signature[2], base, 'the ring is lit as the reset starts')
        self.frames(card, 11.0, 11.3)
        # At a bar's pace the ring would be past 90% by now.
        self.assertLess(card._shown_pcts[0], 85)
        self.frame(card, 11.5)
        margin = self.m.p(u.RING_MARGIN)
        self.assertEqual(card._ring_signature[7], margin)
        self.assertEqual(card._ring_photo.width(), self.m.p(84) + 2 * margin)
        self.assertEqual(card.rows.coords('ring'), [self.m.p(16) - margin, self.m.p(54) - margin])
        self.frames(card, 11.5, 12.5)
        self.assertIsNone(card._refill_t0)
        self.assertEqual(card._ring_photo.width(), self.m.p(84))
        self.assertEqual(card.rows.coords('ring'), [self.m.p(16), self.m.p(54)])

    def test_turning_red_beats_three_times_once(self):
        card = self.card(snap(25))
        self.show(card, snap(18), 11.0)
        self.assertIsNotNone(card._pulse_t0)
        self.frame(card, 11.0 + u.PULSE_BEAT_S / 2)
        self.assertEqual(card._ring_signature[3], self.m.p(7) + self.m.p(3), 'thickest mid-beat')
        self.frames(card, 11.2, 11.0 + u.PULSE_BEATS * u.PULSE_BEAT_S + 0.1)
        self.assertIsNone(card._pulse_t0)
        self.assertEqual(card._ring_signature[3], self.m.p(7))
        self.show(card, snap(15), 13.0)
        self.assertIsNone(card._pulse_t0, 'still red: no second beat')
        self.show(card, snap(4), 14.0)
        self.assertIsNotNone(card._pulse_t0, 'nearly out is another step down')

    def test_a_small_rise_is_not_a_reset(self):
        card = self.card(snap(40))
        self.show(card, snap(40 + u.REFILL_JUMP - 1), 11.0)
        self.assertIsNone(card._refill_t0)
        self.assertIsNone(card._ghost)

    def test_no_moment_without_something_to_compare(self):
        first = u.Card(self.root, 'chatgpt')
        self.show(first, snap(80), 10.0)
        self.assertFalse(first._moving(), 'the first paint')
        stale = self.card(snap(80, stale=True))
        self.show(stale, snap(40), 11.0)
        self.assertFalse(stale._moving(), 'after data that was already stale')
        switched = self.card(snap(80, windows=('primary_window',)))
        self.show(switched, snap(80, week=40, windows=('secondary_window',)), 11.0)
        self.assertFalse(switched._moving(), 'the ring now shows another quota')


if __name__ == '__main__':
    unittest.main()
