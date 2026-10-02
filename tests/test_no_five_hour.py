"""GPT accounts that Codex reports without a five-hour limit say so, on the card and the chip."""
import unittest

import usage_widget as u
from providers import ProviderSnapshot, QuotaItem
from tests.tk_support import destroy_root


def quota(key, raw, name, remaining, window):
    return QuotaItem(f'{key}:main:{raw}', key, 'main', name, raw_identifier=raw,
                     window_seconds=window, window_label=name, used_percent=100 - remaining,
                     remaining_percent=remaining, reset_at=2000000000, scope='global')


def snapshot(key, *limits, ok=True):
    hero = limits[0].remaining_percent if limits else None
    return ProviderSnapshot(key, u.TITLES[key], 'Plus', ok, hero, '', main_limits=list(limits))


WEEKLY_ONLY = (quota('chatgpt', 'primary_window', '주간', 45, 604800.0),)
BOTH = (quota('chatgpt', 'primary_window', '5시간', 80, 18000.0),
        quota('chatgpt', 'secondary_window', '주간', 45, 604800.0))


class NoFiveHourTests(unittest.TestCase):
    def test_only_gpt_limits_without_a_five_hour_window_count(self):
        self.assertTrue(u.no_five_hour_limit(snapshot('chatgpt', *WEEKLY_ONLY)))
        self.assertFalse(u.no_five_hour_limit(snapshot('chatgpt', *BOTH)))
        # Nothing to judge from: a failed read, or no limits yet.
        self.assertFalse(u.no_five_hour_limit(snapshot('chatgpt', *WEEKLY_ONLY, ok=False)))
        self.assertFalse(u.no_five_hour_limit(snapshot('chatgpt')))
        self.assertFalse(u.no_five_hour_limit(None))
        # Other services have their own windows.
        cursor = quota('cursor', 'autoPercentUsed', 'Cursor Models', 90, 2592000.0)
        self.assertFalse(u.no_five_hour_limit(snapshot('cursor', cursor)))

    def test_the_card_says_it_where_the_weekly_row_would_be(self):
        root = u.tk.Tk()
        root.withdraw()
        self.addCleanup(destroy_root, root)
        card = u.Card(root, 'chatgpt')
        card.render(snapshot('chatgpt', *WEEKLY_ONLY))
        note = card.rows.find_withtag('five_hour_note')
        self.assertTrue(note)
        self.assertEqual(card.rows.itemcget(note[0], 'text'), u.NO_FIVE_HOUR_NOTE)
        self.assertEqual(card.rows.coords(note[0])[1], card.metrics.p(156))
        card.render(snapshot('chatgpt', *BOTH))
        self.assertFalse(card.rows.find_withtag('five_hour_note'))

    def test_the_compact_tooltip_says_it_too(self):
        self.assertIn(u.NO_FIVE_HOUR_NOTE, u.compact_tooltip(snapshot('chatgpt', *WEEKLY_ONLY)))
        self.assertNotIn(u.NO_FIVE_HOUR_NOTE, u.compact_tooltip(snapshot('chatgpt', *BOTH)))


if __name__ == '__main__':
    unittest.main()
