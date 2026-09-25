import inspect
import tempfile
import unittest
from tests.tk_support import destroy_root
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import additional_ui as a
import providers as p
import usage_widget as u


COLORS = {
    'bg': u.CARD, 'muted': u.MUTED, 'text': u.TEXT, 'warn': u.WARN, 'danger': u.DANGER,
    'track': u.TRACK, 'dim': u.DIM, 'font_meta': u.FONT_META, 'font_row': u.FONT_ROW,
    'font_value': u.FONT_VALUE,
}


def sample_groups(source='chatgpt'):
    spark = p.LimitGroup(
        f'{source}:additional:spark', source, 'GPT-X Spark',
        raw_identifier='spark', limits=[
            p.QuotaItem(
                f'{source}:additional:spark:window:18000', source, 'additional', '5시간',
                raw_identifier='window:18000', window_seconds=18000, remaining_percent=70,
                reset_at=1_900_000_000, scope='scoped',
            ),
            p.QuotaItem(
                f'{source}:additional:spark:window:604800', source, 'additional', '주간',
                raw_identifier='window:604800', window_seconds=604800, remaining_percent=12,
                reset_at=1_900_086_400, scope='scoped',
            ),
        ],
    )
    compact = p.LimitGroup(
        f'{source}:additional:mini', source, '5시간',
        raw_identifier='mini', limits=[
            p.QuotaItem(
                f'{source}:additional:mini:window:18000', source, 'additional', '5시간',
                raw_identifier='window:18000', window_seconds=18000, remaining_percent=55,
                scope='scoped',
            ),
        ],
    )
    return [spark, compact]


def capture(block):
    header = []
    for item in block.header.find_all():
        if block.header.type(item) == 'text':
            header.append(block.header.itemcget(item, 'text'))
    body = []
    fills = []
    for item in block.body.find_all():
        kind = block.body.type(item)
        tags = block.body.gettags(item)
        if kind == 'text':
            body.append((tags[0] if tags else '', block.body.itemcget(item, 'text'), block.body.itemcget(item, 'fill')))
        elif kind == 'rectangle':
            fills.append((tags[0] if tags else '', block.body.itemcget(item, 'fill')))
    scroll = str(block.body.cget('scrollregion') or '').split()
    return {
        'count': a.additional_count(block.groups),
        'header': header,
        'body': body,
        'fills': fills,
        'height': block.height,
        'body_h': int(str(block.body.cget('height'))),
        'scroll': tuple(float(value) for value in scroll),
        'expanded': block.expanded,
    }


class AdditionalRendererTests(unittest.TestCase):
    def setUp(self):
        self.root = u.tk.Tk()
        self.root.withdraw()
        self.metrics = u.Metrics(1.0)

    def tearDown(self):
        destroy_root(self.root)

    def _render(self, groups, source, expanded=True, max_body=240, metrics=None):
        tagged = [
            replace(
                group,
                source=source,
                group_id=group.group_id.replace(group.source, source, 1),
                limits=[replace(item, source=source) for item in group.limits],
            )
            for group in groups
        ]
        block = a.AdditionalBlock(self.root, metrics or self.metrics)
        block.render(tagged, expanded=expanded, max_body=max_body, colors=COLORS)
        self.root.update_idletasks()
        return block, capture(block)

    def test_generic_rules_match_across_providers(self):
        groups = sample_groups('chatgpt')
        _, chatgpt = self._render(groups, 'chatgpt')
        _, synthetic = self._render(groups, 'synthetic_provider')
        self.assertEqual(chatgpt, synthetic)
        self.assertEqual(chatgpt['count'], 2)
        self.assertEqual(chatgpt['header'], ['추가 한도 2개 ▲'])
        labels = [text for _, text, _ in chatgpt['body']]
        self.assertIn('GPT-X Spark', labels)
        self.assertIn('5시간', labels)
        self.assertIn('주간', labels)
        self.assertIn('70%', labels)
        self.assertIn('12%', labels)
        self.assertIn('55%', labels)
        self.assertTrue(any(tag == 'reset' for tag, _, _ in chatgpt['body']))
        warn_row = next(color for tag, text, color in chatgpt['body'] if text == '주간')
        self.assertEqual(warn_row, u.DANGER)

    def test_single_window_hides_redundant_heading(self):
        rows = a.layout_additional(sample_groups()[1:])
        self.assertEqual([row.kind for row in rows], ['window'])
        self.assertEqual(rows[0].text, '5시간')

    def test_expand_collapse_and_scroll_height(self):
        groups = sample_groups()
        collapsed, closed = self._render(groups, 'chatgpt', expanded=False, max_body=40)
        self.assertFalse(closed['expanded'])
        self.assertEqual(closed['body_h'], 1)
        self.assertEqual(closed['header'], ['추가 한도 2개 ▾'])
        expanded, opened = self._render(groups, 'synthetic_provider', expanded=True, max_body=40)
        self.assertTrue(opened['expanded'])
        self.assertLessEqual(opened['body_h'], 40)
        self.assertGreater(opened['scroll'][3], opened['body_h'])
        expanded._wheel(type('E', (), {'delta': -120})())
        self.assertGreaterEqual(expanded.height, closed['height'])

    def test_dpi_scale_grows_content_height(self):
        rows = a.layout_additional(sample_groups())
        compact = a.additional_content_height(rows, u.Metrics(1.0).p)
        scaled = a.additional_content_height(rows, u.Metrics(1.5).p)
        self.assertGreater(scaled, compact)

    def test_renderer_has_no_provider_key_branches(self):
        source = inspect.getsource(a)
        self.assertNotIn("'chatgpt'", source)
        self.assertNotIn('"chatgpt"', source)
        self.assertNotIn("'cursor'", source)
        self.assertNotIn('"cursor"', source)
        self.assertNotIn('synthetic_provider', source)
        self.assertNotIn('additional_rate_limits', source)
        self.assertNotIn('metered_feature', source)

    def test_scoped_warning_does_not_change_provider_severity(self):
        groups = sample_groups()
        snap = p.ProviderSnapshot(
            'chatgpt', 'GPT', 'Plus', True, 90, '5시간 기준 잔여',
            bars=[p.QuotaBar('5시간', 90, 10, ''), p.QuotaBar('주간', 80, 20, '')],
            additional_groups=groups,
        )
        # A scoped warning reaches neither the representative channel nor
        # the risk channel; it is only visible inside the Additional block.
        self.assertEqual(u.representative_state(snap), 'ok')
        self.assertEqual(u.service_state(snap), 'ok')
        self.assertEqual(u.representative_percent(snap), 90)
        self.assertTrue(any(row.warn for row in a.layout_additional(groups)))


class AdditionalWidgetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name)
        self.patches = [
            patch.object(u, 'SETTINGS_PATH', path / 'settings.json'),
            patch.object(u, 'CACHE_PATH', path / 'cache.json'),
        ]
        for item in self.patches:
            item.start()
        self._card_animate = u.Card.animate
        self._chip_animate = u.Chip.animate
        u.Card.animate = False
        u.Chip.animate = False
        self.w = u.UsageWidget(preview=True)
        self.w.root.withdraw()

    def tearDown(self):
        self.w.close()
        u.Card.animate = self._card_animate
        u.Chip.animate = self._chip_animate
        for item in self.patches:
            item.stop()
        self.directory.cleanup()

    def test_accordion_one_additional_open_and_cursor_hides_total(self):
        gpt = p.ProviderSnapshot(
            'chatgpt', 'GPT', 'Plus', True, 90, '',
            bars=[p.QuotaBar('5시간', 90, 10, '')],
            additional_groups=sample_groups('chatgpt'),
        )
        cursor = p.ProviderSnapshot(
            'cursor', 'Cursor', 'Pro', True, 90, 'Cursor Models 기준 잔여',
            bars=[p.QuotaBar('Cursor Models', 90, 10, ''), p.QuotaBar('Other Models', 60, 40, '')],
            additional_groups=sample_groups('cursor'),
            internal={'totalPercentUsed': 99.0},
        )
        self.w.snapshots['chatgpt'] = gpt
        self.w.snapshots['cursor'] = cursor
        self.w.render('chatgpt')
        self.w.render('cursor')
        self.w.toggle_additional('chatgpt')
        self.assertTrue(self.w.cards['chatgpt'].additional.expanded)
        self.assertFalse(self.w.cards['cursor'].additional.expanded)
        self.w.toggle_additional('cursor')
        self.assertFalse(self.w.cards['chatgpt'].additional.expanded)
        self.assertTrue(self.w.cards['cursor'].additional.expanded)
        texts = [
            self.w.cards['cursor'].rows.itemcget(item, 'text')
            for item in self.w.cards['cursor'].rows.find_all()
            if self.w.cards['cursor'].rows.type(item) == 'text'
        ]
        self.assertIn('Cursor Models · 남은 사용량', texts)
        self.assertIn('Other Models', texts)
        self.assertNotIn('1%', texts)
        self.assertNotIn('99%', texts)
        self.assertEqual(self.w.mini_values['cursor'].cget('text'), 'Cursor 90%')


if __name__ == '__main__':
    unittest.main(verbosity=2)
