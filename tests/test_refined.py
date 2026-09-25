import unittest
from tests.tk_support import destroy_root
from datetime import datetime
from dataclasses import replace
import usage_widget as u
import quota_policy as qp
from providers import BillingItem, ProviderSnapshot, QuotaItem

FIVE_H = 18000.0
WEEK = 604800.0


def quota(raw_id, name, remaining, window=None, reset_at=None, source='chatgpt'):
    """Canonical quota fixture: meaning comes from fields, never from `name`."""
    return QuotaItem(f'{source}:main:{raw_id}', source, 'main', name, raw_identifier=raw_id,
                     window_seconds=window, window_label=name, used_percent=100 - remaining,
                     remaining_percent=remaining, reset_at=reset_at, scope='global')

class RefinedTests(unittest.TestCase):
    def test_reset_crosses_the_year_boundary_and_expires(self):
        # 3.5 carries reset_at as an epoch straight from the provider, so the
        # year is never inferred from a rendered caption. The countdown
        # behaviour that inference existed to serve still holds.
        end=datetime(2027,1,1,9).timestamp()
        self.assertEqual(u.reset_countdown(end,end+1),'곧')
        root=u.tk.Tk(); root.withdraw()
        try:
            card=u.Card(root,'chatgpt')
            card.render(ProviderSnapshot('chatgpt','GPT','Plus',True,None,'',
                main_limits=[quota('primary_window','5시간',80,FIVE_H,end)],
                fetched_at=datetime(2026,12,31,12).timestamp()))
            self.assertEqual(card._reset_epoch,end)
            card.refresh_clock(end+1)
            self.assertEqual(card.rows.itemcget('countdown','text'),'곧')
            card.destroy()
        finally: destroy_root(root)
    def test_secondary_countdowns_use_each_quotas_reset(self):
        now = datetime(2030, 1, 1, 12).timestamp()
        root = u.tk.Tk()
        root.withdraw()
        try:
            card = u.Card(root, 'chatgpt')
            card.render(ProviderSnapshot('chatgpt', 'GPT', 'Plus', True, None, '',
                main_limits=[
                    quota('primary', '5시간', 80, FIVE_H, now + 3600),
                    quota('weekly', '주간', 40, WEEK, now + 2 * 86400),
                    quota('monthly', '월간', 60, 30 * 86400, now + 10 * 86400),
                ]))
            card.refresh_clock(now)
            labels = card.rows.find_withtag('week_remaining')
            self.assertEqual([card.rows.itemcget(i, 'text') for i in labels],
                             ['2일 남음', '10일 남음'])
            card.refresh_clock(now + 2 * 86400 - 65)
            self.assertEqual([card.rows.itemcget(i, 'text') for i in labels],
                             ['1분 05초', '8일 남음'])
            card.refresh_clock(now + 2 * 86400 + 1)
            self.assertEqual([card.rows.itemcget(i, 'text') for i in labels],
                             ['곧', '7일 남음'])
        finally:
            destroy_root(root)

    def test_secondary_countdowns_follow_reorder_and_removal(self):
        now = datetime(2030, 1, 1, 12).timestamp()
        hero = quota('primary', '5시간', 80, FIVE_H, now + 3600)
        weekly = quota('weekly', '주간', 40, WEEK, now + 2 * 86400)
        monthly = quota('monthly', '월간', 60, 30 * 86400, now + 10 * 86400)
        unknown = quota('unknown', '기간 미상', 50)
        root = u.tk.Tk()
        root.withdraw()
        try:
            card = u.Card(root, 'chatgpt')
            for limits, expected in (
                ([hero, weekly, monthly], ['2일 남음', '10일 남음']),
                ([hero, monthly, weekly], ['10일 남음', '2일 남음']),
                ([hero, replace(weekly, reset_at=now + 3 * 86400), unknown], ['3일 남음']),
                ([hero, unknown], []),
            ):
                with self.subTest(expected=expected):
                    card.render(ProviderSnapshot('chatgpt', 'GPT', 'Plus', True, None, '',
                                                main_limits=limits))
                    card.refresh_clock(now)
                    self.assertEqual(
                        [card.rows.itemcget(i, 'text')
                         for i in card.rows.find_withtag('week_remaining')], expected)
        finally:
            destroy_root(root)

    def test_countdown_and_severity_boundaries(self):
        self.assertEqual(u.reset_countdown(3600,0),'1시간 0분')
        self.assertEqual(u.reset_countdown(65,0),'1분 05초')
        self.assertEqual(u.reset_countdown(86400*13,0,True),'13일 후')
        for value,expected in [(50,'ok'),(49.9,'warn'),(20,'warn'),(19.9,'danger'),(5,'danger'),(4.9,'critical')]:
            self.assertEqual(u.design_severity(value)[0],expected)
        self.assertEqual(u.design_severity(90,blocked=True)[0],'critical')
        self.assertEqual(u.design_severity(1,stale=True)[0],'stale')

    def test_compact_chip_uses_the_card_bands(self):
        from runtime import AlertGate
        cases = (
            (50, 'ok', u.CHIP_OK['chatgpt']),
            (49.9, 'warn', u.CHIP_WARN),
            (40, 'warn', u.CHIP_WARN),
            (20, 'warn', u.CHIP_WARN),
            (19.9, 'danger', u.CHIP_DANGER),
            (5, 'danger', u.CHIP_DANGER),
            (4.9, 'danger', u.CHIP_DANGER),
        )
        for value, state, chip in cases:
            with self.subTest(value=value):
                snap = ProviderSnapshot('chatgpt', 'GPT', 'Plus', True, None, '',
                    main_limits=[quota('primary_window', '5시간', value, FIVE_H)])
                card_band = u.design_severity(value)[0]
                self.assertEqual(card_band, 'critical' if value < 5 else state)
                self.assertEqual(u.representative_state(snap), state)
                self.assertEqual(u.chip_style('chatgpt', snap)[0], chip)
        calm = ProviderSnapshot('chatgpt', 'GPT', 'Plus', True, None, '',
            main_limits=[quota('primary_window', '5시간', 40, FIVE_H)])
        self.assertIsNone(AlertGate().observe('chatgpt', calm))
    def test_hero_and_badge_use_five_hour_while_weekly_warns_independently(self):
        root=u.tk.Tk(); root.withdraw()
        try:
            card=u.Card(root,'chatgpt')
            snap=ProviderSnapshot('chatgpt','GPT','Plus',True,None,'',
                main_limits=[quota('primary_window','5시간',91,FIVE_H),
                             quota('secondary_window','주간',4,WEEK)])
            card.render(snap)
            self.assertEqual(card.rows.itemcget('hero','text'),'91%')
            self.assertIn('5시간 한도 · 남은 사용량',[card.rows.itemcget(i,'text') for i in card.rows.find_all() if card.rows.type(i)=='text'])
            # The hero stays calm; the badge is the channel that reports the
            # weekly window, and it does so independently.
            self.assertEqual(u.representative_state(snap),'ok')
            self.assertEqual(card.rows.itemcget('severity','text'),'곧 한도')
            self.assertFalse(card.rows.find_withtag('bar_0'))
            self.assertTrue(card.rows.find_withtag('bar_1'))
            for scale in (.75,1,1.5):
                card.set_metrics(u.Metrics(scale));card.render(snap)
                for i in card.rows.find_all():
                    if card.rows.type(i)=='text':
                        box=card.rows.bbox(i)
                        self.assertGreaterEqual(box[0],0)
                        self.assertLessEqual(box[2],card.metrics.card_w)
            card.destroy()
        finally: destroy_root(root)

    def test_representative_state_ignores_a_secondary_quotas_percentage(self):
        # A low secondary window moves the risk channel, never the hero's own
        # number or colour. A provider-wide block is a different signal, and
        # 3.5 lets it through for every provider rather than for a hardcoded
        # list of provider names.
        cases = [
            (100, 13, False, 'ok', 'danger', u.CHIP_OK['chatgpt']),
            (100, 3, False, 'ok', 'danger', u.CHIP_OK['chatgpt']),
            (5, 80, False, 'danger', 'danger', u.CHIP_DANGER),
            (100, 0, True, 'danger', 'danger', u.CHIP_DANGER),
            (0, 80, True, 'danger', 'danger', u.CHIP_DANGER),
        ]
        for hero, secondary, blocked, representative, service, compact in cases:
            with self.subTest(hero=hero, secondary=secondary):
                snap=ProviderSnapshot('chatgpt','GPT','Plus',True,None,'',
                    main_limits=[quota('primary_window','5시간',hero,FIVE_H),
                                 quota('secondary_window','주간',secondary,WEEK)],
                    blocked=blocked)
                self.assertEqual(u.representative_percent(snap),hero)
                self.assertEqual(u.representative_state(snap),representative)
                self.assertEqual(u.service_state(snap),service)
                self.assertEqual(u.chip_style('chatgpt',snap)[0],compact)

    def test_compact_warning_is_secondary_global_and_fresh_only(self):
        hero = quota('primary_window', '5시간', 91, FIVE_H)
        weekly = quota('secondary_window', '주간', 4, WEEK)
        snap = ProviderSnapshot('chatgpt', 'GPT', 'Plus', True, None, '',
                                main_limits=[hero, weekly])
        self.assertEqual(u.compact_warning(snap).quota_id, weekly.quota_id)
        self.assertEqual(u.representative_percent(snap), 91)
        self.assertEqual(u.chip_style('chatgpt', snap)[0], u.CHIP_OK['chatgpt'])
        self.assertIn('주의: 주간 잔여 4%', u.compact_tooltip(snap))
        self.assertIn('5시간 · 잔여 91%', u.compact_tooltip(snap))
        for other in (replace(weekly, remaining_percent=50),
                      replace(weekly, remaining_percent=None),
                      replace(weekly, scope='model')):
            self.assertIsNone(u.compact_warning(replace(snap, main_limits=[hero, other])))
        self.assertIsNone(u.compact_warning(replace(snap, main_limits=[weekly])))
        self.assertIsNone(u.compact_warning(replace(snap, stale=True)))
        self.assertIsNone(u.compact_warning(replace(snap, ok=False)))
        self.assertIn('이전 값', u.compact_tooltip(replace(snap, stale=True)))

    def test_compact_warning_recovers_and_uses_all_secondary_limits(self):
        limits = [quota('primary_window', '5시간', 91, FIVE_H),
                  quota('weekly', '주간', 40, WEEK),
                  quota('monthly', '월간', 0, 30 * 86400)]
        snap = ProviderSnapshot('chatgpt', 'GPT', 'Plus', True, None, '', main_limits=limits)
        root = u.tk.Tk()
        root.withdraw()
        try:
            chip = u.Chip(root)
            chip.configure(text='GPT 91%', percent=91, bg=u.CHIP_OK['chatgpt'], animate=False)
            chip.observe_usage(snap)
            self.assertEqual(u.compact_warning(snap).display_name, '월간')
            self.assertEqual(chip._warning_color, u.DANGER)
            self.assertTrue(chip.find_withtag('quota_warning'))
            chip.observe_usage(replace(snap, main_limits=limits[:2]))
            self.assertEqual(chip._warning_color, u.WARN)
            self.assertIn('주간 잔여 40%', chip.tip_text)
            chip.observe_usage(replace(snap, main_limits=limits[:1]))
            self.assertFalse(chip.find_withtag('quota_warning'))
            self.assertEqual(chip.cget('text'), 'GPT 91%')
            self.assertEqual(chip.fill, u.CHIP_OK['chatgpt'])
            chip.observe_usage(snap)
            chip.observe_usage(replace(snap, stale=True))
            self.assertFalse(chip.find_withtag('quota_warning'))
        finally:
            destroy_root(root)

    def test_compact_warning_rings_the_chip_without_moving_the_label(self):
        root = u.tk.Tk()
        root.withdraw()
        try:
            for scale in (.75, 1, 1.15, 1.3, 1.5):
                m = u.Metrics(scale)
                font = u.tkfont.Font(root=root, font=m.font(u.FONT_CHIP))
                width = max(m.chip_w, max(font.measure(f'{name} 100%')
                            for name in u.TITLES.values()) + 2*m.p(u.COMPACT_CHIP_PAD))
                _, _, width, _ = u.compact_row_layout(count=3, chip_width=width,
                    controls_left=m.window_w-m.p(90), scale_px=m.p)
                for key, name in u.TITLES.items():
                    with self.subTest(scale=scale, key=key):
                        chip = u.Chip(root, m)
                        chip.set_width(width)
                        chip.configure(text=f'{name} 100%', animate=False)
                        snap = ProviderSnapshot(key, name, 'Pro', True, None, '', main_limits=[
                            quota('autoPercentUsed' if key == 'cursor' else 'five_hour', '5시간', 100, FIVE_H, source=key),
                            quota('weekly', '주간', 4, WEEK, source=key)])
                        before = chip.bbox('label')
                        chip.observe_usage(snap)
                        label = chip.bbox('label')
                        ring = chip.bbox('quota_warning')
                        self.assertIsNotNone(ring)
                        self.assertEqual(label, before)
                        self.assertGreaterEqual(label[0], 0)
                        self.assertLessEqual(label[2], width)
                        self.assertEqual((ring[0], ring[2]), (0, width))
                        chip.destroy()
        finally:
            destroy_root(root)

    def test_secondary_alert_names_the_quota_that_is_running_out(self):
        snap=ProviderSnapshot('chatgpt','GPT','Plus',True,None,'5시간 기준 잔여',
            main_limits=[quota('primary_window','5시간',100,FIVE_H),
                         quota('secondary_window','주간',2,WEEK)])
        # The hero is untouched while the alert points at the weekly window.
        self.assertEqual(u.representative_percent(snap),100)
        self.assertEqual(u.representative_state(snap),'ok')
        limiting=qp.limiting_quota(snap)
        self.assertEqual(limiting.raw_identifier,'secondary_window')
        self.assertEqual(u.quota_alert_copy('chatgpt',2,0,limiting.display_name),
                         ('ChatGPT 주간 한도 소진','주간 한도 · 잔여 0%'))
    def test_cursor_bonus_is_spend_not_remaining(self):
        root=u.tk.Tk(); root.withdraw()
        try:
            card=u.Card(root,'cursor')
            # Billing arrives as a BillingItem, never as a phrase parsed back
            # out of the footer, and it never becomes quota.
            card.render(ProviderSnapshot('cursor','Cursor','Pro',True,None,'',
                main_limits=[quota('autoPercentUsed','Cursor Models',72,reset_at=1790000000.0,source='cursor'),
                             quota('apiPercentUsed','Other Models',82,reset_at=1790000000.0,source='cursor')],
                billing=[BillingItem('cursor:billing:bonusSpend','cursor','bonusSpend',34087)],
                info_rows=[], footer=''))
            texts=[card.rows.itemcget(i,'text') for i in card.rows.find_all() if card.rows.type(i)=='text']
            self.assertIn('◇ 보너스 사용액',texts)
            self.assertIn('$340.87',texts)
            self.assertIn('Cursor Models · 남은 사용량',texts)
            self.assertFalse(card.rows.find_withtag('bar_0'))
            self.assertEqual(card.rows.itemcget('bar_value_1','text'),'82%')
            self.assertNotIn('28% 사용', texts)
            self.assertNotIn('18% 사용', texts)
            self.assertTrue(card.rows.find_withtag('ring'))
        finally:
            card.destroy(); destroy_root(root)
