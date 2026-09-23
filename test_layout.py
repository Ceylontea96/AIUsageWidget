"""Card collapse and bounded desktop viewport regressions, without account access."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import usage_widget as u
from providers import ProviderSnapshot, QuotaItem


def snapshot(key):
    return ProviderSnapshot(key, u.TITLES[key], 'Plus', True, None, '', main_limits=[
        QuotaItem(key + ':main:primary', key, 'main', '5시간',
                  raw_identifier='autoPercentUsed' if key == 'cursor' else 'five_hour',
                  remaining_percent=91, window_seconds=18000, reset_at=2000000000,
                  scope='global'),
        QuotaItem(key + ':main:week', key, 'main', '주간',
                  raw_identifier='weekly', remaining_percent=4,
                  window_seconds=604800, reset_at=2000400000, scope='global')])


class LayoutTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        for target, value in (
            ('SETTINGS_PATH', base / 'settings.json'),
            ('CACHE_PATH', base / 'cache.json'),
        ):
            guard = patch.object(u, target, value)
            guard.start()
            self.addCleanup(guard.stop)
        for guard in (
            patch.object(u, 'login_present', return_value=False),
            patch('claude_integration.ensure_bridge_copy'),
            patch.object(u.UsageWidget, '_sync_claude_menu'),
            patch.object(u, 'work_area', return_value=(0, 0, 800, 600)),
            patch.object(u, 'monitor_area', return_value=(0, 0, 800, 600)),
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

    def test_height_is_bounded_and_footer_stays_outside_scroll_area(self):
        w = self.w
        for scale in (.75, 1, 1.5):
            with self.subTest(scale=scale):
                w.set_scale(scale)
                w.root.update_idletasks()
                self.assertLessEqual(int(w.shell.cget('height')), 600)
                viewport = w.body_view.place_info()
                footer = w.footer.place_info()
                self.assertLessEqual(int(viewport['y']) + int(viewport['height']), int(footer['y']))
                if scale >= 1:
                    self.assertEqual(w.body_scroll.winfo_manager(), 'place')
                    w.body_view.yview_moveto(1)
                    self.assertGreater(w.body_view.yview()[0], 0)
                    self.assertAlmostEqual(w.body_view.yview()[1], 1, places=2)

    def test_collapse_keeps_quota_and_warning_and_shrinks_window(self):
        w = self.w
        for key in u.FETCHERS:
            w.toggle_card(key)
        w.root.update_idletasks()
        self.assertEqual(w.body_scroll.winfo_manager(), '')
        self.assertLess(int(w.shell.cget('height')), 400)
        self.assertEqual(w.body_view.yview()[0], 0)
        card = w.cards['chatgpt']
        self.assertIn('91%', card.rows.itemcget('collapsed_summary', 'text'))
        self.assertEqual(card.rows.itemcget('severity', 'text'), '곧 한도')
        self.assertTrue(w.enabled['chatgpt'].get())
        changed = replace(w.snapshots['chatgpt'], main_limits=[
            replace(w.snapshots['chatgpt'].main_limits[0], remaining_percent=85),
            w.snapshots['chatgpt'].main_limits[1]])
        w.snapshots['chatgpt'] = changed
        w.render('chatgpt')
        self.assertIn('85%', card.rows.itemcget('collapsed_summary', 'text'))
        w.toggle_card('chatgpt')
        self.assertTrue(card.rows.find_withtag('ring'))
        self.assertFalse(card.rows.find_withtag('collapsed_summary'))

    def test_compact_round_trip_and_monitor_resize_preserve_collapse(self):
        w = self.w
        w.toggle_card('cursor')
        w.toggle()
        self.assertEqual(int(w.shell.cget('height')), w.metrics.compact_h)
        self.assertEqual(w.body_view.winfo_manager(), '')
        w.toggle()
        self.assertTrue(w.cards['cursor'].collapsed)
        with patch.object(u, 'work_area', return_value=(0, 0, 1920, 1080)):
            w.relayout()
            self.assertEqual(w.body_scroll.winfo_manager(), '')
        w.relayout()
        self.assertLessEqual(int(w.shell.cget('height')), 600)

    def test_collapse_is_saved_and_restored(self):
        w = self.w
        w.toggle_card('cursor')
        w.preview = False
        try:
            w.persist()
        finally:
            w.preview = True
        self.assertEqual(u.read_json(u.SETTINGS_PATH)['collapsed'],
                         {'chatgpt': False, 'cursor': True, 'claude': False})
        w.close()
        restored = u.UsageWidget(preview=True)
        try:
            restored.snapshots['cursor'] = snapshot('cursor')
            restored.enabled['cursor'].set(True)
            restored.render('cursor')
            self.assertTrue(restored.cards['cursor'].collapsed)
            self.assertTrue(restored.cards['cursor'].rows.find_withtag('collapsed_summary'))
        finally:
            restored.close()

    def test_header_toggles_without_opening_browser(self):
        card = self.w.cards['chatgpt']
        with patch.object(u.webbrowser, 'open') as browser:
            card._clicked(SimpleNamespace(y=20))
            self.assertTrue(card.collapsed)
            browser.assert_not_called()
            card._toggle_card()
            self.assertFalse(card.collapsed)

    def test_wheel_is_scoped_and_does_not_double_scroll_additional(self):
        w = self.w
        with patch.object(w.body_view, 'yview_scroll') as scroll:
            self.assertEqual(w._scroll_body(SimpleNamespace(widget=w.cards['chatgpt'].rows, delta=-120)), 'break')
            scroll.assert_called_once_with(1, 'units')
            scroll.reset_mock()
            w._scroll_body(SimpleNamespace(widget=w.cards['chatgpt'].additional.body, delta=-120))
            w._scroll_body(SimpleNamespace(widget=w.footer, delta=-120))
            scroll.assert_not_called()


if __name__ == '__main__':
    unittest.main()
