import unittest
from datetime import datetime
from dataclasses import replace
import usage_widget as u
from providers import ProviderSnapshot, QuotaBar

class RefinedTests(unittest.TestCase):
    def test_reset_year_boundary_and_expiration(self):
        ref=datetime(2026,12,31,12).timestamp()
        end=u.reset_epoch('1월 1일 09:00',ref)
        self.assertEqual(datetime.fromtimestamp(end).year,2027)
        self.assertEqual(u.reset_countdown(end,end+1),'곧')
        self.assertIsNone(u.reset_epoch('unknown',ref))
        self.assertIsNone(u.reset_epoch('2월 31일 09:00',ref))
    def test_countdown_and_severity_boundaries(self):
        self.assertEqual(u.reset_countdown(3600,0),'1시간 0분')
        self.assertEqual(u.reset_countdown(65,0),'1분 05초')
        self.assertEqual(u.reset_countdown(86400*13,0,True),'13일 후')
        for value,expected in [(50,'ok'),(49.9,'warn'),(20,'warn'),(19.9,'danger'),(5,'danger'),(4.9,'critical')]:
            self.assertEqual(u.design_severity(value)[0],expected)
        self.assertEqual(u.design_severity(90,blocked=True)[0],'critical')
        self.assertEqual(u.design_severity(1,stale=True)[0],'stale')
    def test_hero_and_badge_use_five_hour_while_weekly_warns_independently(self):
        root=u.tk.Tk(); root.withdraw()
        try:
            card=u.Card(root,'chatgpt')
            snap=ProviderSnapshot('chatgpt','GPT','Plus',True,4,'',bars=[QuotaBar('5시간',91,9,''),QuotaBar('주간',4,96,'')])
            card.render(snap)
            self.assertEqual(card.rows.itemcget('hero','text'),'91%')
            self.assertIn('5시간 한도 · 남은 사용량',[card.rows.itemcget(i,'text') for i in card.rows.find_all() if card.rows.type(i)=='text'])
            self.assertEqual(card.rows.itemcget('severity','text'),'여유')
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
        finally: root.destroy()

    def test_representative_state_ignores_secondary_quota(self):
        cases = [
            (100, 13, 'ok', u.CHIP_OK['chatgpt']),
            (5, 80, 'danger', u.CHIP_DANGER),
            (100, 0, 'ok', u.CHIP_OK['chatgpt']),
            (0, 80, 'danger', u.CHIP_DANGER),
        ]
        for hero, secondary, expected, compact_color in cases:
            with self.subTest(hero=hero, secondary=secondary):
                snap=ProviderSnapshot('chatgpt','GPT','Plus',True,hero,'',
                    bars=[QuotaBar('5시간',hero,100-hero,''),QuotaBar('주간',secondary,100-secondary,'')],
                    blocked=secondary == 0 or hero == 0)
                self.assertEqual(u.representative_percent(snap),hero)
                self.assertEqual(u.visual_state(snap),expected)
                self.assertEqual(u.chip_style('chatgpt',snap)[0],compact_color)

    def test_secondary_alert_names_quota_without_changing_hero_state(self):
        snap=ProviderSnapshot('chatgpt','GPT','Plus',True,100,'5시간 기준 잔여',
            bars=[QuotaBar('5시간',100,0,''),QuotaBar('주간',0,100,'')],blocked=True)
        self.assertEqual(u.visual_state(snap),'ok')
        self.assertEqual(u.quota_alert_copy('chatgpt',2,0,'주간'),
                         ('ChatGPT 주간 한도 소진','주간 한도 · 잔여 0%'))
    def test_cursor_bonus_is_spend_not_remaining(self):
        root=u.tk.Tk(); root.withdraw()
        try:
            card=u.Card(root,'cursor')
            card.render(ProviderSnapshot('cursor','Cursor','Pro',True,27,'',
                bars=[QuotaBar('Cursor Models',72,28,'','9월 28일 11:27'),QuotaBar('Other Models',82,18,'','9월 28일 11:27')],
                info_rows=[], footer='9월 28일 11:27 초기화 · 보너스 $340.87'))
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
            card.destroy(); root.destroy()
