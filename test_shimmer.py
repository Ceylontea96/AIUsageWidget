import unittest
from dataclasses import replace
from unittest.mock import patch

import usage_widget as u
from providers import ProviderSnapshot, QuotaBar, snapshot_to_dict, snapshot_from_dict


def snapshot(first=60, second=70, **kwargs):
    return ProviderSnapshot('chatgpt', 'Codex', 'Plus', True, min(first, second), '',
                            bars=[QuotaBar('5시간', first, 100-first, '', '09/14', 'window-a'),
                                  QuotaBar('주간', second, 100-second, '', '09/20', 'window-b')], **kwargs)


class UsageChangeTests(unittest.TestCase):
    def test_only_changed_row_including_sub_display_precision(self):
        self.assertEqual(u.usage_changes(snapshot(), snapshot(59.999)), ({0, 1}, {0}))
        self.assertEqual(u.usage_changes(snapshot(), snapshot()), ({0, 1}, set()))

    def test_initial_error_recovery_stale_and_plan_do_not_trigger(self):
        for previous in (None, snapshot(stale=True), replace(snapshot(), ok=False), replace(snapshot(), plan='Pro')):
            self.assertEqual(u.usage_changes(previous, snapshot(50)), (set(), set()))
        for current in (snapshot(50, stale=True), replace(snapshot(50), ok=False)):
            self.assertEqual(u.usage_changes(snapshot(), current), (set(), set()))

    def test_reset_limit_change_and_increase_establish_baseline(self):
        for field in ('reset_text', 'usage_scope'):
            current = snapshot(50)
            setattr(current.bars[0], field, 'new-period-or-limit')
            self.assertEqual(u.usage_changes(snapshot(), current), ({1}, set()))
        self.assertEqual(u.usage_changes(snapshot(), snapshot(80)), ({1}, set()))

    def test_used_percent_still_detects_after_remaining_is_clipped(self):
        previous, current = snapshot(0), snapshot(0)
        previous.bars[0].used_percent = 105
        current.bars[0].used_percent = 106
        self.assertEqual(u.usage_changes(previous, current)[1], {0})

    def test_scope_survives_worker_and_cache_serialization(self):
        sample = snapshot()
        self.assertEqual(snapshot_from_dict(snapshot_to_dict(sample)).bars, sample.bars)
        legacy = snapshot_to_dict(sample)
        for bar in legacy['bars']:
            del bar['usage_scope']
        self.assertEqual(snapshot_from_dict(legacy).bars[0].usage_scope, '')

    def test_missing_and_invalid_values_do_not_look_like_usage(self):
        for value in (None, float('nan'), float('inf')):
            current = snapshot()
            current.bars[0].used_percent = current.bars[0].remaining_percent = value
            self.assertEqual(u.usage_changes(snapshot(), current), ({1}, set()))


class ShimmerRasterTests(unittest.TestCase):
    def pixels(self, fill_width, phase=None, samples=1):
        return u.progress_bar_rgba(100, 8, 4, fill_width, '#262A36', '#4FE0B0', '#1A1D26',
                                   shimmer=phase, samples=samples)[2]

    def test_light_changes_fill_only_and_keeps_track_clipping(self):
        base = self.pixels(60)
        lit = self.pixels(60, 0.45)
        self.assertNotEqual(base, lit)
        for original, animated in zip(base, lit):
            self.assertEqual(original[60 * 4:], animated[60 * 4:])
            self.assertEqual(original[3::4], animated[3::4])
            self.assertTrue(all(a >= b for a, b in zip(animated, original)))

    def test_empty_and_rest_phases_match_static_bar(self):
        for width in (0, 1, 60, 100):
            for phase in (0, 0.15, 0.75, 0.99):
                self.assertEqual(self.pixels(width), self.pixels(width, phase))
        self.assertEqual(self.pixels(0), self.pixels(0, 0.45))

    def test_supersampling_keeps_shimmer(self):
        self.assertNotEqual(self.pixels(60, samples=2), self.pixels(60, 0.45, samples=2))


class ShimmerUiTests(unittest.TestCase):
    def setUp(self):
        self.root = u.tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()

    def pump(self, control, now, active=None):
        with patch.object(control, 'winfo_ismapped', return_value=True), patch.object(u.time, 'monotonic', return_value=now):
            if active is not None:
                control.set_activity(active)
            if control._shimmer_after is not None:
                control.after_cancel(control._shimmer_after)
                control._shimmer_after = None
            control._shimmer_tick()

    def weekly_card(self):
        card = u.Card(self.root, 'chatgpt')
        card.render(replace(snapshot(), bars=[QuotaBar('주간', 60, 40, '', '')]))
        return card

    def test_card_updates_only_bar_images_and_stops_timer(self):
        card = u.Card(self.root, 'chatgpt')
        card.render(ProviderSnapshot('chatgpt', 'Codex', 'Plus', True, 60, '',
                                    bars=[QuotaBar('5시간', 60, 40, '', '')]))
        items = card.rows.find_all()
        photos = list(card._bar_photos)
        ring = card._ring_photo
        self.pump(card, 2.25, True)
        self.pump(card, 2.65, True)
        self.assertEqual(items, card.rows.find_all())
        self.assertIs(photos[0], card._bar_photos[0])
        self.assertIsNot(ring, card._ring_photo)
        self.assertEqual(card._shown_pcts, [60])
        self.assertTrue(card._active)
        self.assertIsNotNone(card._shimmer_after)
        card._stop_shimmer()
        self.assertIsNone(card._shimmer_after)
        self.assertFalse(card._active)
        card._snap.stale = True
        self.assertFalse(card._shimmer_ready())

    def test_chip_preserves_label_and_stops_when_hidden(self):
        chip = u.Chip(self.root)
        chip.configure(text='Codex 60%', bg=u.CODEX, percent=60)
        label = chip.find_withtag('label')
        with patch.object(chip, 'winfo_ismapped', return_value=True):
            chip.set_activity(True)
            chip._paint_shimmer()
        self.assertEqual(label, chip.find_withtag('label'))
        self.assertEqual(chip.itemcget(label[0], 'text'), 'Codex 60%')
        chip._shimmer_tick()
        self.assertIsNone(chip._shimmer_after)
        self.assertFalse(chip._active)
        chip.configure(bg=u.CHIP_STALE)
        self.assertFalse(chip._shimmer_ready())

    def test_quota_change_does_not_drive_activity(self):
        card = self.weekly_card()
        with patch.object(card, 'winfo_ismapped', return_value=True):
            card.render(replace(snapshot(59.999), bars=[QuotaBar('주간', 59.999, 40.001, '', '')]))
        self.assertFalse(card._active)
        self.assertEqual(card._emphasis, 0.0)
        self.assertIsNone(card._shimmer_after)

    def test_continued_activity_holds_max_thickness(self):
        card = self.weekly_card()
        heights = []
        for now, active in ((0, True), (0.4, True), (4, True), (6, True), (8, True), (11, True), (15, True)):
            self.pump(card, now, active)
            heights.append(card._bar_height_for(0))
        peak = card.metrics.bar_h + card.metrics.p(4)
        self.assertEqual(heights[0], card.metrics.bar_h)
        self.assertEqual(heights[1], peak)
        self.assertTrue(all(height == peak for height in heights[1:]))
        self.assertIsNotNone(card._shimmer_phase_for(0))

    def test_short_activity_shrinks_only_after_inactive(self):
        card = self.weekly_card()
        self.pump(card, 0, True)
        self.pump(card, 0.4, True)
        peak = card._bar_height_for(0)
        self.pump(card, 0.4, False)
        self.assertEqual(card._bar_height_for(0), peak)
        self.pump(card, 0.4 + u.SHIMMER_SHRINK_S / 2, False)
        self.assertLess(card._bar_height_for(0), peak)
        self.assertGreater(card._bar_height_for(0), card.metrics.bar_h)
        self.pump(card, 0.4 + u.SHIMMER_SHRINK_S, False)
        self.assertEqual(card._bar_height_for(0), card.metrics.bar_h)
        self.assertFalse(card._active)

    def test_active_bar_thickens_smoothly_then_returns_to_base_height(self):
        card = self.weekly_card()
        heights = []
        offsets = []
        for now, active in ((10.0, True), (10.2, True), (10.4, True), (15.0, True), (15.4, False), (15.85, False)):
            self.pump(card, now, active)
            heights.append(card._bar_height_for(0))
            offsets.append(card.rows.coords('bar_0')[1])
        self.assertEqual(heights[0], card.metrics.bar_h)
        self.assertGreater(heights[1], heights[0])
        self.assertEqual(heights[2], card.metrics.bar_h + card.metrics.p(4))
        self.assertEqual(heights[3], heights[2])
        self.assertLess(heights[4], heights[3])
        self.assertEqual(heights[5], card.metrics.bar_h)
        self.assertEqual(len(set(offsets)), 1)

    def test_fractional_thickness_changes_pixels_without_moving_image(self):
        frames = []
        for height in (8.0, 8.1, 8.2, 8.3):
            w, h, rows = u.progress_bar_rgba(40, 14, height / 2, 30, u.TRACK, u.CODEX, u.CARD, shape_height=height)
            self.assertEqual((w, h), (40, 14))
            self.assertEqual(rows, list(reversed(rows)))
            frames.append(b''.join(rows))
        self.assertEqual(len(set(frames)), 4)

    def test_error_and_recovery_cancel_and_do_not_replay(self):
        card = u.Card(self.root, 'chatgpt')
        card.render(snapshot())
        self.pump(card, 1, True)
        self.assertTrue(card._active)
        card.render(snapshot(59.9, stale=True))
        self.pump(card, 1.1, True)
        self.assertFalse(card._active)
        self.assertIsNone(card._shimmer_after)
        card.render(snapshot(59.8))
        self.assertFalse(card._active)
        self.pump(card, 2, True)
        self.assertTrue(card._active)

    def test_hidden_usage_is_not_replayed_on_show(self):
        chip = u.Chip(self.root)
        chip.configure(bg=u.CODEX, percent=60)
        chip.set_activity(True)
        self.assertFalse(chip._active)
        with patch.object(chip, 'winfo_ismapped', return_value=True):
            chip.set_activity(True)
            self.assertTrue(chip._active)
            chip._stop_shimmer()
            self.assertFalse(chip._active)

    def advance_length(self, control, now):
        if control._anim_after is not None:
            control.after_cancel(control._anim_after)
        with patch.object(u.time, 'monotonic', return_value=now):
            control._anim_tick()

    def test_small_changes_tween_in_both_directions_with_shimmer(self):
        for start, end in ((60, 59.9), (59.9, 60)):
            card = u.Card(self.root, 'chatgpt')
            chip = u.Chip(self.root)
            with patch.object(u.time, 'monotonic', return_value=10), patch.object(card, 'winfo_ismapped', return_value=True):
                card.render(snapshot(start))
                chip.configure(bg=u.CODEX, percent=start)
                card.render(snapshot(end))
                chip.configure(percent=end)
            self.assertEqual(card._shown_pcts[0], start)
            self.assertEqual(chip.percent, start)
            items = card.rows.find_all()
            label = chip.find_withtag('label')
            for control in (card, chip):
                self.advance_length(control, 10.25)
            for shown in (card._shown_pcts[0], chip.percent):
                self.assertGreater(shown, min(start, end))
                self.assertLess(shown, max(start, end))
            self.assertEqual(card.rows.find_all(), items)
            self.assertEqual(chip.find_withtag('label'), label)
            self.assertAlmostEqual(chip.fill_width, u.chip_fill_width(chip.metrics.chip_w, chip.percent))
            # Light painting must not overwrite the intermediate length.
            intermediate = list(card._shown_pcts)
            card._paint_shimmer()
            self.assertEqual(card._shown_pcts, intermediate)
            for control in (card, chip):
                self.advance_length(control, 12.1)
                self.assertIsNone(control._anim_after)
            self.assertEqual(card._shown_pcts[0], end)
            self.assertEqual(chip.percent, end)
            card.destroy()
            chip.destroy()

    def test_repeated_target_does_not_restart_or_finish_length_early(self):
        card = u.Card(self.root, 'chatgpt')
        chip = u.Chip(self.root)
        with patch.object(u.time, 'monotonic', return_value=10):
            card.render(snapshot(60))
            card.render(snapshot(59.9))
            chip.configure(percent=60)
            chip.configure(percent=59.9)
        for control in (card, chip):
            self.advance_length(control, 10.25)
        with patch.object(u.time, 'monotonic', return_value=10.3):
            card.render(replace(snapshot(59.9), footer='metadata changed'))
            chip.configure(percent=59.9)
        self.assertEqual(card._anim_t0, 10.25)
        self.assertEqual(chip._anim_t0, 10.25)
        self.assertGreater(card._shown_pcts[0], 59.9)
        self.assertGreater(chip.percent, 59.9)

    def test_new_target_samples_current_motion_before_reversing(self):
        card = u.Card(self.root, 'chatgpt')
        chip = u.Chip(self.root)
        with patch.object(u.time, 'monotonic', return_value=10):
            card.render(snapshot(80))
            card.render(snapshot(40))
            chip.configure(percent=80)
            chip.configure(percent=40)
        for control in (card, chip):
            self.advance_length(control, 10.2)
        expected = card._shown_pcts[0]
        timer_ids = (card._anim_after, chip._anim_after)
        with patch.object(u.time, 'monotonic', return_value=10.3), patch.object(card, 'after_cancel', side_effect=AssertionError('restart')), patch.object(chip, 'after_cancel', side_effect=AssertionError('restart')):
            for target in (52, 51, 90):
                card.render(snapshot(target))
                chip.configure(percent=target)
        self.assertEqual((card._anim_after, chip._anim_after), timer_ids)
        self.assertAlmostEqual(card._shown_pcts[0], expected)
        self.assertAlmostEqual(chip.percent, expected)
        self.assertEqual(card._anim_t0, 10.2)
        self.assertEqual(chip._anim_t0, 10.2)
        for control in (card, chip):
            self.advance_length(control, 10.4)
        self.assertGreater(card._shown_pcts[0], expected)
        self.assertGreater(chip.percent, expected)



if __name__ == '__main__':
    unittest.main()
