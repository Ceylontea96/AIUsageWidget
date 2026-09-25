"""Regression tests for the canonical quota architecture (3.5.0).

These cover the contracts the rest of the app now depends on: stable identity,
one hero selector, the global/scoped split, variable quota counts, additional
groups, billing isolation and forward compatibility with limits nobody has
seen yet.
"""

import json
import time
import unittest
from unittest.mock import patch

import additional_ui
import providers as p
import quota_policy as qp
import usage_widget as u
from providers import BillingItem, LimitGroup, ProviderSnapshot, QuotaItem
from runtime import AlertGate


class FakeAuth:
    plan = 'pro'

    def load(self): pass
    def ensure_fresh(self): pass
    def access_token(self): return 'fake'
    def account_id(self): return ''


def gpt(body):
    return p.chatgpt_snapshot(body)


def window(used, seconds=None, **extra):
    raw = {'used_percent': used}
    if seconds is not None:
        raw['limit_window_seconds'] = seconds
    raw.update(extra)
    return raw


def gpt_rate(**rate):
    return gpt({'rate_limit': rate})


def cursor(plan_usage, **extra):
    usage = {'planUsage': plan_usage, 'billingCycleEnd': 1800000000}
    usage.update(extra)
    with patch.object(p, 'CursorAuth', FakeAuth), \
         patch.object(p, 'http_json', return_value=(200, usage)), \
         patch.object(p, '_cursor_plan_name', return_value='Pro'):
        return p.fetch_cursor()


def claude(*limits, now=1_900_000_000.0):
    body = {'rate_limits': {'limits': list(limits)}}
    line = json.dumps({'type': 'control_response',
                       'response': {'subtype': 'success', 'request_id': 'usage', 'response': body}})
    return p.claude_usage_from_control_output(line, now)


def claude_row(kind, percent, resets='2033-05-18T03:33:20Z'):
    return {'kind': kind, 'percent': percent, 'resets_at': resets}


def quota(raw_id, name, remaining, *, window_seconds=None, scope='global',
          category='main', source='chatgpt', reset_at=None):
    return QuotaItem(f'{source}:{category}:{raw_id}', source, category, name,
                     raw_identifier=raw_id, window_seconds=window_seconds,
                     window_label=name, used_percent=None if remaining is None else 100 - remaining,
                     remaining_percent=remaining, reset_at=reset_at, scope=scope)


FIVE_H = 18000.0
WEEK = 604800.0


class StableIdentityTests(unittest.TestCase):
    def test_display_name_change_keeps_quota_id(self):
        def snap(name):
            return gpt({'rate_limit': {'primary_window': window(10, FIVE_H)},
                        'additional_rate_limits': [{'limit_id': 'spark', 'display_name': name,
                                                    'rate_limit': {'primary_window': window(20, FIVE_H)}}]})
        before, after = snap('Spark'), snap('Spark Model')
        self.assertEqual([i.quota_id for i in before.main_limits],
                         [i.quota_id for i in after.main_limits])
        self.assertEqual(before.additional_groups[0].group_id, after.additional_groups[0].group_id)
        self.assertEqual([i.quota_id for i in before.additional_groups[0].limits],
                         [i.quota_id for i in after.additional_groups[0].limits])
        self.assertNotEqual(before.additional_groups[0].display_name,
                            after.additional_groups[0].display_name)

    def test_reset_change_keeps_quota_id(self):
        first = gpt_rate(primary_window=window(10, FIVE_H, reset_at=1800000000))
        second = gpt_rate(primary_window=window(40, FIVE_H, reset_at=1900000000))
        self.assertEqual([i.quota_id for i in first.main_limits],
                         [i.quota_id for i in second.main_limits])
        self.assertNotEqual(first.main_limits[0].reset_at, second.main_limits[0].reset_at)

    def test_same_duration_windows_keep_separate_ids(self):
        snap = gpt_rate(primary_window=window(10, FIVE_H), secondary_window=window(40, FIVE_H))
        ids = [item.quota_id for item in snap.main_limits]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 2)

    def test_repeated_raw_id_still_separates_quotas(self):
        snap = gpt_rate(primary_window=window(10, FIVE_H, limit_id='same'),
                        secondary_window=window(40, WEEK, limit_id='same'))
        ids = [item.quota_id for item in snap.main_limits]
        self.assertEqual(len(ids), len(set(ids)))

    def test_unknown_duration_id_is_stable_across_polls(self):
        polls = [gpt_rate(primary_window=window(used, None, reset_after_seconds=60))
                 for used in (10, 20, 30)]
        ids = {tuple(i.quota_id for i in snap.main_limits) for snap in polls}
        self.assertEqual(len(ids), 1)
        self.assertNotIn('None', ids.pop()[0])

    def test_identity_never_embeds_display_or_reset(self):
        snap = gpt_rate(primary_window=window(10, FIVE_H, reset_at=1800000000),
                        secondary_window=window(20, WEEK, reset_at=1900000000))
        for item in snap.main_limits:
            self.assertNotIn(item.display_name, item.quota_id)
            self.assertNotIn('1800000000', item.quota_id)
            self.assertNotIn('1900000000', item.quota_id)

    def test_cache_round_trip_preserves_identity(self):
        snap = gpt_rate(primary_window=window(10, FIVE_H), secondary_window=window(20, WEEK))
        restored = p.snapshot_from_dict(p.snapshot_to_dict(snap))
        self.assertEqual([i.quota_id for i in restored.main_limits],
                         [i.quota_id for i in snap.main_limits])
        self.assertEqual([i.window_seconds for i in restored.main_limits],
                         [i.window_seconds for i in snap.main_limits])
        self.assertEqual(qp.select_hero(restored).quota_id, qp.select_hero(snap).quota_id)

    def test_legacy_bar_cache_restores_without_merging_quotas(self):
        # A 3.4.x cache carried only bars. Restoring must not collapse two
        # windows into one identity, and must not invent a bogus percentage.
        legacy = {'key': 'chatgpt', 'title': 'GPT', 'plan': 'Plus', 'ok': True,
                  'hero_percent': 80, 'hero_caption': '', 'bars': [
                      {'label': '5시간', 'remaining_percent': 80, 'used_percent': 20,
                       'detail': '', 'usage_scope': json.dumps([1800000000, FIVE_H])},
                      {'label': '주간', 'remaining_percent': 40, 'used_percent': 60,
                       'detail': '', 'usage_scope': json.dumps([1900000000, WEEK])}]}
        restored = p.snapshot_from_dict(legacy)
        ids = [item.quota_id for item in restored.main_limits]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(qp.select_hero(restored).remaining_percent, 80)
        self.assertEqual(restored.main_limits[1].reset_at, 1900000000)
        # And the identity survives a restart, which re-reads the same cache.
        self.assertEqual([i.quota_id for i in p.snapshot_from_dict(legacy).main_limits], ids)


class CacheCompatibilityTests(unittest.TestCase):
    def v342_cache(self):
        """What a 3.4.2 install wrote: bars plus first-generation main_limits."""
        return {
            'key': 'chatgpt', 'title': 'GPT', 'plan': 'ChatGPT Plus', 'ok': True,
            'hero_percent': 80, 'hero_caption': '5시간 기준 잔여', 'stale': False,
            'bars': [
                {'label': '5시간', 'remaining_percent': 80, 'used_percent': 20,
                 'detail': '잔여 80%', 'reset_text': '9월 24일 12:00',
                 'usage_scope': json.dumps([1800000000, FIVE_H])},
                {'label': '주간', 'remaining_percent': 40, 'used_percent': 60,
                 'detail': '잔여 40%', 'usage_scope': json.dumps([1900000000, WEEK])}],
            'info_rows': [{'label': '5시간', 'value': '잔여 80%', 'emphasis': ''}],
            'main_limits': [
                {'quota_id': 'chatgpt:main:window:18000', 'source': 'chatgpt', 'category': 'main',
                 'display_name': '5시간', 'raw_identifier': 'window:18000',
                 'window_seconds': FIVE_H, 'window_label': '5시간', 'used_percent': 20,
                 'remaining_percent': 80, 'reset_at': 1800000000, 'scope': 'global'},
                {'quota_id': 'chatgpt:main:window:604800', 'source': 'chatgpt', 'category': 'main',
                 'display_name': '주간', 'raw_identifier': 'window:604800',
                 'window_seconds': WEEK, 'window_label': '주간', 'used_percent': 60,
                 'remaining_percent': 40, 'reset_at': 1900000000, 'scope': 'global'}],
            'additional_groups': [], 'billing': [], 'internal': {}}

    def test_a_342_cache_restores_without_losing_quota(self):
        snap = p.snapshot_from_dict(self.v342_cache())
        self.assertTrue(snap.stale)
        self.assertEqual(len(qp.global_main_limits(snap)), 2)
        self.assertEqual(u.representative_percent(snap), 80)
        self.assertEqual(u.remaining_marks(snap), [80.0, 40.0])
        self.assertEqual(qp.select_hero(snap).window_seconds, FIVE_H)
        self.assertEqual(qp.limiting_quota(snap).remaining_percent, 40)

    def test_migration_invents_neither_zero_nor_full(self):
        snap = p.snapshot_from_dict(self.v342_cache())
        for item in snap.main_limits:
            self.assertNotIn(item.remaining_percent, (0, 100))

    def test_new_cache_round_trips_every_canonical_section(self):
        source = gpt({'rate_limit': {'primary_window': window(10, FIVE_H),
                                     'secondary_window': window(20, WEEK)},
                      'credits': {'has_credits': True, 'balance': 4},
                      'additional_rate_limits': [{'limit_id': 'spark', 'display_name': 'Spark',
                                                  'rate_limit': {'primary_window': window(30, FIVE_H)}}]})
        restored = p.snapshot_from_dict(p.snapshot_to_dict(source))
        self.assertEqual([i.quota_id for i in restored.main_limits],
                         [i.quota_id for i in source.main_limits])
        self.assertEqual([g.group_id for g in restored.additional_groups],
                         [g.group_id for g in source.additional_groups])
        self.assertEqual([b.billing_id for b in restored.billing],
                         [b.billing_id for b in source.billing])
        self.assertEqual(u.included_amount(restored), u.included_amount(source))
        self.assertEqual(u.quota_extras(restored), u.quota_extras(source))

    def test_a_cache_with_many_quotas_is_not_truncated(self):
        rate = {f'window_{index}': window(index, 3600 * (index + 2)) for index in range(12)}
        source = gpt_rate(**rate)
        restored = p.snapshot_from_dict(p.snapshot_to_dict(source))
        self.assertEqual(len(restored.main_limits), 12)
        self.assertEqual(len(restored.bars), 12)


class HeroSelectionTests(unittest.TestCase):
    def test_gpt_prefers_five_hour_then_weekly(self):
        both = gpt_rate(primary_window=window(20, FIVE_H), secondary_window=window(90, WEEK))
        self.assertEqual(qp.select_hero(both).window_seconds, FIVE_H)
        weekly = gpt_rate(primary_window=window(30, WEEK))
        self.assertEqual(qp.select_hero(weekly).window_seconds, WEEK)

    def test_gpt_position_does_not_decide_meaning(self):
        reversed_snap = gpt_rate(primary_window=window(30, WEEK), secondary_window=window(20, FIVE_H))
        self.assertEqual(qp.select_hero(reversed_snap).window_seconds, FIVE_H)

    def test_unknown_only_falls_back_deterministically(self):
        snaps = [gpt_rate(primary_window=window(25, None, reset_after_seconds=60),
                          secondary_window=window(35, None, reset_after_seconds=90))
                 for _ in range(3)]
        heroes = {qp.select_hero(snap).quota_id for snap in snaps}
        self.assertEqual(len(heroes), 1)
        self.assertEqual({qp.select_hero(snap).remaining_percent for snap in snaps}, {75.0})

    def test_cursor_hero_is_the_model_pool_not_a_label(self):
        snap = cursor({'autoPercentUsed': 20, 'apiPercentUsed': 90})
        self.assertEqual(qp.select_hero(snap).raw_identifier, 'autoPercentUsed')
        only_other = cursor({'apiPercentUsed': 40})
        self.assertEqual(qp.select_hero(only_other).raw_identifier, 'apiPercentUsed')

    def test_claude_prefers_five_hour_then_seven_day(self):
        both = claude(claude_row('session', 20), claude_row('weekly_all', 90))
        self.assertEqual(qp.select_hero(both).raw_identifier, 'five_hour')
        weekly = claude(claude_row('weekly_all', 30))
        self.assertEqual(qp.select_hero(weekly).raw_identifier, 'seven_day')

    def test_one_selector_feeds_hero_and_compact(self):
        for snap in (gpt_rate(primary_window=window(0, FIVE_H), secondary_window=window(97, WEEK)),
                     cursor({'autoPercentUsed': 0, 'apiPercentUsed': 95}),
                     claude(claude_row('session', 0), claude_row('weekly_all', 97))):
            with self.subTest(key=snap.key):
                hero = qp.select_hero(snap)
                self.assertEqual(u.representative_percent(snap), hero.remaining_percent)
                self.assertEqual(u.representative_state(snap),
                                 u._state_for(snap, hero.remaining_percent, snap.blocked))

    def test_hero_number_and_colour_come_from_the_same_quota(self):
        # 5h full, weekly nearly gone: the big number stays calm, the risk
        # channel does not.
        snap = gpt_rate(primary_window=window(0, FIVE_H), secondary_window=window(97, WEEK))
        self.assertEqual(u.representative_percent(snap), 100)
        self.assertEqual(u.representative_state(snap), 'ok')
        self.assertEqual(u.service_state(snap), 'danger')
        self.assertEqual(qp.limiting_quota(snap).remaining_percent, 3)

    def test_hero_survives_a_renamed_window(self):
        with patch.object(p, '_duration_label', lambda *a, **k: 'Five Hours'):
            snap = gpt_rate(primary_window=window(20, FIVE_H), secondary_window=window(90, WEEK))
        self.assertEqual(qp.select_hero(snap).window_seconds, FIVE_H)


class ProviderContractTests(unittest.TestCase):
    def snapshots(self):
        return (gpt_rate(primary_window=window(20, FIVE_H), secondary_window=window(40, WEEK)),
                cursor({'autoPercentUsed': 20, 'apiPercentUsed': 40}),
                claude(claude_row('session', 20), claude_row('weekly_all', 40)))

    def test_every_provider_publishes_main_global_quota(self):
        # A provider that mislabels category or scope would silently vanish
        # from the card, the chip, alerts and polling all at once.
        for snap in self.snapshots():
            with self.subTest(key=snap.key):
                limits = qp.global_main_limits(snap)
                self.assertEqual(limits, snap.main_limits)
                self.assertEqual(len(limits), 2)
                self.assertTrue(all(item.quota_id and item.raw_identifier for item in limits))
                self.assertTrue(all(item.source for item in limits))

    def test_legacy_bars_mirror_the_canonical_limits(self):
        for snap in self.snapshots():
            with self.subTest(key=snap.key):
                self.assertEqual([bar.label for bar in snap.bars],
                                 [item.display_name for item in snap.main_limits])
                self.assertEqual([bar.remaining_percent for bar in snap.bars],
                                 [item.remaining_percent for item in snap.main_limits])

    def test_hero_percent_field_matches_the_selector(self):
        for snap in self.snapshots():
            with self.subTest(key=snap.key):
                self.assertEqual(snap.hero_percent, qp.select_hero(snap).remaining_percent)
                self.assertEqual(u.representative_percent(snap), snap.hero_percent)


class BlockedSemanticsTests(unittest.TestCase):
    def test_blocked_follows_account_wide_exhaustion_for_every_provider(self):
        cases = ((gpt_rate(primary_window=window(50, FIVE_H), secondary_window=window(100, WEEK)), True),
                 (gpt_rate(primary_window=window(50, FIVE_H), secondary_window=window(50, WEEK)), False),
                 (claude(claude_row('session', 50), claude_row('weekly_all', 100)), True),
                 (claude(claude_row('session', 50), claude_row('weekly_all', 50)), False))
        for snap, expected in cases:
            with self.subTest(key=snap.key, expected=expected):
                self.assertEqual(snap.blocked, expected)
                self.assertEqual(qp.representative_blocked(snap), expected)

    def test_a_server_side_limit_blocks_even_with_quota_left(self):
        snap = gpt_rate(primary_window=window(20, FIVE_H), limit_reached=True)
        self.assertTrue(snap.blocked)
        self.assertEqual(u.representative_percent(snap), 80)
        self.assertEqual(u.representative_state(snap), 'danger')

    def test_scoped_exhaustion_never_sets_blocked(self):
        snap = gpt({'rate_limit': {'primary_window': window(20, FIVE_H)},
                    'additional_rate_limits': [{'limit_id': 'g', 'rate_limit': {
                        'primary_window': window(100, FIVE_H)}}]})
        self.assertFalse(snap.blocked)
        self.assertEqual(u.service_state(snap), 'ok')
        self.assertEqual(snap.additional_groups[0].limits[0].remaining_percent, 0)


class ScopeContractTests(unittest.TestCase):
    def snapshot(self):
        group = LimitGroup('chatgpt:additional:code_review', 'chatgpt', 'Code Review',
                           raw_identifier='code_review',
                           limits=[quota('code_review:w', '5시간', 0, window_seconds=FIVE_H,
                                         scope='scoped', category='additional')])
        return ProviderSnapshot('chatgpt', 'GPT', 'Plus', True, None, '',
                                main_limits=[quota('primary_window', '5시간', 80, window_seconds=FIVE_H)],
                                additional_groups=[group])

    def test_scoped_exhaustion_never_speaks_for_the_provider(self):
        snap = self.snapshot()
        self.assertEqual(u.representative_percent(snap), 80)
        self.assertEqual(u.representative_state(snap), 'ok')
        self.assertEqual(u.service_state(snap), 'ok')
        self.assertFalse(qp.representative_blocked(snap))
        self.assertEqual(qp.limiting_quota(snap).remaining_percent, 80)
        self.assertIsNone(AlertGate().observe('chatgpt', snap))

    def test_scoped_quota_is_still_visible_for_its_own_warning(self):
        exhausted = [row for row in additional_ui.layout_additional(self.snapshot().additional_groups)
                     if row.kind == 'window' and row.percent == 0]
        self.assertEqual(len(exhausted), 1)
        self.assertTrue(exhausted[0].warn)
        self.assertEqual(exhausted[0].quota_id, self.snapshot().additional_groups[0].limits[0].quota_id)

    def test_scoped_quota_does_not_speed_up_polling(self):
        snap = self.snapshot()
        self.assertEqual(u.remaining_marks(snap), [80.0])
        self.assertEqual(u.next_interval(snap), 30)

    def test_a_scoped_main_limit_is_filtered_out_too(self):
        snap = ProviderSnapshot('chatgpt', 'GPT', 'Plus', True, None, '', main_limits=[
            quota('primary_window', '5시간', 80, window_seconds=FIVE_H),
            quota('scoped_window', '5시간', 0, window_seconds=FIVE_H, scope='scoped')])
        self.assertEqual([i.raw_identifier for i in qp.global_main_limits(snap)], ['primary_window'])
        self.assertEqual(u.remaining_marks(snap), [80.0])


class VariableMainLimitTests(unittest.TestCase):
    def rate_with(self, count):
        return {f'window_{index}': window(index, 3600 * (index + 2)) for index in range(count)}

    def test_every_main_quota_is_kept(self):
        for count in (0, 1, 2, 3, 10):
            with self.subTest(count=count):
                rate = self.rate_with(count)
                if not count:
                    with self.assertRaises(RuntimeError):
                        gpt_rate(**rate)
                    continue
                snap = gpt_rate(**rate)
                self.assertEqual(len(snap.main_limits), count)
                self.assertEqual(len(qp.global_main_limits(snap)), count)
                self.assertEqual(len(u.remaining_marks(snap)), count)

    def test_risk_reads_past_the_eighth_quota(self):
        rate = self.rate_with(10)
        rate['window_9'] = window(100, 36000)
        snap = gpt_rate(**rate)
        self.assertEqual(qp.limiting_quota(snap).remaining_percent, 0)
        self.assertEqual(AlertGate().observe('chatgpt', snap), 2)
        self.assertEqual(u.next_interval(snap), 300)

    def test_display_budget_does_not_shrink_risk_detection(self):
        rate = self.rate_with(20)
        rate['window_19'] = window(95, 36000)
        snap = gpt_rate(**rate)
        self.assertEqual(len(qp.global_main_limits(snap)), 20)
        self.assertEqual(len(u.remaining_marks(snap)), 20)
        self.assertEqual(qp.limiting_quota(snap).remaining_percent, 5)
        self.assertEqual(AlertGate().observe('chatgpt', snap), 1)
        self.assertLess(u.MAX_SECONDARY_ROWS, 20)

    def test_a_third_window_never_overwrites_the_second(self):
        snap = gpt_rate(primary_window=window(10, FIVE_H),
                        secondary_window=window(20, WEEK),
                        windows=[window(30, 86400)])
        self.assertEqual([i.window_seconds for i in snap.main_limits], [FIVE_H, WEEK, 86400])
        self.assertEqual([i.remaining_percent for i in snap.main_limits], [90, 80, 70])


class AdditionalGroupTests(unittest.TestCase):
    def groups(self, raw):
        return p.additional_groups_from_payload('chatgpt', {'additional_rate_limits': raw})

    def test_group_counts_match_the_payload(self):
        for count in (0, 1, 3):
            raw = [{'limit_id': f'g{index}', 'display_name': f'G{index}',
                    'rate_limit': {'primary_window': window(10, FIVE_H)}} for index in range(count)]
            self.assertEqual(len(self.groups(raw)), count)

    def test_two_windows_in_one_group_stay_separate(self):
        groups = self.groups([{'limit_id': 'spark', 'display_name': 'Spark', 'rate_limit': {
            'primary_window': window(10, FIVE_H), 'secondary_window': window(20, WEEK)}}])
        self.assertEqual(len(groups[0].limits), 2)
        self.assertEqual(len({item.quota_id for item in groups[0].limits}), 2)

    def test_unknown_window_is_preserved_not_guessed(self):
        groups = self.groups([{'limit_id': 'g', 'rate_limit': {'primary_window': window(10, 864000)}}])
        item = groups[0].limits[0]
        self.assertEqual(item.window_seconds, 864000)
        self.assertNotIn(item.display_name, ('주간', '5시간'))

    def test_additional_never_moves_the_hero_or_the_chip(self):
        base = ProviderSnapshot('chatgpt', 'GPT', 'Plus', True, None, '',
                                main_limits=[quota('primary_window', '5시간', 80, window_seconds=FIVE_H)])
        with_groups = ProviderSnapshot(
            'chatgpt', 'GPT', 'Plus', True, None, '',
            main_limits=[quota('primary_window', '5시간', 80, window_seconds=FIVE_H)],
            additional_groups=self.groups([{'limit_id': 'g', 'display_name': 'G', 'rate_limit': {
                'primary_window': window(100, FIVE_H)}}]))
        self.assertEqual(u.representative_percent(base), u.representative_percent(with_groups))
        self.assertEqual(u.service_state(base), u.service_state(with_groups))
        self.assertEqual(u.next_interval(base), u.next_interval(with_groups))

    def test_stale_group_is_marked_without_staling_the_provider(self):
        group = LimitGroup('g', 'chatgpt', 'Spark', metadata={'stale': True},
                           limits=[quota('g:w', '5시간', 50, window_seconds=FIVE_H,
                                         scope='scoped', category='additional')])
        snap = ProviderSnapshot('chatgpt', 'GPT', 'Plus', True, None, '',
                                main_limits=[quota('primary_window', '5시간', 80, window_seconds=FIVE_H)],
                                additional_groups=[group])
        self.assertTrue(qp.group_stale(group, snap))
        self.assertFalse(snap.stale)
        self.assertEqual(u.representative_state(snap), 'ok')
        rows = additional_ui.layout_additional([group], [True])
        self.assertTrue(all(row.stale for row in rows))

    def test_renderer_does_not_branch_on_provider(self):
        with open(additional_ui.__file__, encoding='utf-8') as handle:
            text = handle.read()
        for name in ('chatgpt', 'cursor', 'claude'):
            self.assertNotIn(f'"{name}"', text)
            self.assertNotIn(f"'{name}'", text)
        for field in ('autoPercentUsed', 'primary_window', 'rate_limit', 'additional_rate_limits'):
            self.assertNotIn(field, text)

    def test_expanded_body_is_clamped_to_the_work_area(self):
        budget = additional_ui.expanded_body_budget(600, 100, lambda value: value)
        self.assertEqual(budget, 200)
        self.assertEqual(additional_ui.expanded_body_budget(2000, 100, lambda value: value), 380)
        self.assertEqual(additional_ui.expanded_body_budget(200, 500, lambda value: value), 0)


class BillingSeparationTests(unittest.TestCase):
    def test_billing_does_not_touch_quota_state(self):
        limits = [quota('autoPercentUsed', 'Cursor Models', 60, source='cursor')]
        plain = ProviderSnapshot('cursor', 'Cursor', 'Pro', True, None, '', main_limits=limits)
        billed = ProviderSnapshot('cursor', 'Cursor', 'Pro', True, None, '', main_limits=list(limits),
                                  billing=[BillingItem('cursor:billing:remaining', 'cursor', 'remaining', 0),
                                           BillingItem('cursor:billing:limit', 'cursor', 'limit', 2000)])
        self.assertEqual(u.representative_percent(plain), u.representative_percent(billed))
        self.assertEqual(u.service_state(plain), u.service_state(billed))
        self.assertEqual(AlertGate().observe('cursor', plain), AlertGate().observe('cursor', billed))
        self.assertEqual(u.next_interval(plain), u.next_interval(billed))

    def test_cursor_billing_is_read_from_billing_items(self):
        snap = cursor({'autoPercentUsed': 10, 'includedSpend': 500, 'limit': 2000, 'bonusSpend': 300})
        self.assertEqual(u.included_amount(snap), '$5.00 / $20.00')
        self.assertEqual(u.bonus_line(snap), '보너스 $3.00')
        self.assertNotIn('remaining', [item.kind for item in snap.billing if item.raw_value is None])

    def test_cursor_total_stays_internal(self):
        snap = cursor({'autoPercentUsed': 10, 'apiPercentUsed': 20, 'totalPercentUsed': 95})
        self.assertEqual(snap.internal['totalPercentUsed'], 95)
        self.assertEqual([i.raw_identifier for i in qp.global_main_limits(snap)],
                         ['autoPercentUsed', 'apiPercentUsed'])
        self.assertEqual(u.representative_percent(snap), 90)
        self.assertEqual(u.remaining_marks(snap), [90.0, 80.0])
        self.assertNotIn('totalPercentUsed',
                         [item.kind for item in snap.billing] + [i.raw_identifier for i in snap.main_limits])


class CursorInternalOnlyTests(unittest.TestCase):
    """totalPercentUsed is reference data. It must have no user-visible effect."""

    def snapshot(self, auto, api, total):
        return cursor({'autoPercentUsed': 100 - auto, 'apiPercentUsed': 100 - api,
                       'totalPercentUsed': total})

    def user_facing(self, snap):
        return {
            'hero': u.representative_percent(snap),
            'hero_state': u.representative_state(snap),
            'service': u.service_remaining(snap),
            'service_state': u.service_state(snap),
            'blocked': qp.representative_blocked(snap),
            'limiting': qp.limiting_quota(snap).remaining_percent,
            'marks': u.remaining_marks(snap),
            'poll': u.next_interval(snap),
            'alert': AlertGate().observe('cursor', snap),
            'chip': u.chip_style('cursor', snap),
        }

    def test_case_a_a_full_total_does_not_manufacture_danger(self):
        snap = self.snapshot(80, 70, 100)
        self.assertEqual(u.representative_percent(snap), 80)
        self.assertEqual(u.representative_state(snap), 'ok')
        # The badge follows the canonical main quota, which is the 70% pool.
        self.assertEqual(u.service_remaining(snap), 70)
        self.assertEqual(u.service_state(snap), 'ok')
        self.assertFalse(qp.representative_blocked(snap))
        self.assertFalse(snap.blocked)
        self.assertIsNone(AlertGate().observe('cursor', snap))
        self.assertEqual(u.next_interval(snap), 30)

    def test_case_b_an_empty_total_does_not_mask_real_risk(self):
        snap = self.snapshot(3, 70, 0)
        self.assertEqual(u.representative_percent(snap), 3)
        self.assertEqual(u.representative_state(snap), 'danger')
        self.assertEqual(u.service_remaining(snap), 3)
        self.assertEqual(u.service_state(snap), 'danger')
        self.assertEqual(qp.limiting_quota(snap).raw_identifier, 'autoPercentUsed')
        self.assertEqual(AlertGate().observe('cursor', snap), 1)
        self.assertEqual(u.next_interval(snap), 20)

    def test_every_user_facing_value_ignores_total(self):
        for auto, api in ((80, 70), (3, 70), (100, 100), (0, 0)):
            baseline = self.user_facing(self.snapshot(auto, api, 0))
            for total in (25, 50, 99, 100):
                with self.subTest(auto=auto, api=api, total=total):
                    self.assertEqual(self.user_facing(self.snapshot(auto, api, total)), baseline)

    def test_total_does_not_change_the_visible_card_signature(self):
        import dataclasses
        base = self.snapshot(80, 70, 0)
        signatures = set()
        for total in (0, 40, 100, None):
            snap = dataclasses.replace(base, internal={'totalPercentUsed': total})
            signatures.add(json.dumps({
                'hero': u.representative_percent(snap),
                'service': u.service_remaining(snap),
                'limits': [[i.quota_id, i.remaining_percent] for i in u.main_limits(snap)],
                'extras': [u.reset_credit(snap), u.included_amount(snap), u.bonus_line(snap)],
            }, sort_keys=True))
        self.assertEqual(len(signatures), 1)

    def test_no_runtime_path_reads_total_outside_normalization(self):
        import inspect
        import additional_ui
        import polling
        import quota_policy
        import runtime
        for module in (quota_policy, runtime, polling, additional_ui, u):
            with self.subTest(module=module.__name__):
                self.assertNotIn('totalPercentUsed', inspect.getsource(module))
        source = inspect.getsource(p)
        # providers.py may read it exactly twice: once to parse, once to store.
        self.assertEqual(source.count('totalPercentUsed'), 2)


class ForwardCompatibilityTests(unittest.TestCase):
    def test_unknown_shapes_survive_without_a_guess(self):
        snap = gpt({'rate_limit': {'primary_window': window(10, FIVE_H),
                                   'future_window': window(20, 864000, tier='gold')},
                    'additional_rate_limits': [{'limit_id': 'future', 'category': 'experiment',
                                                'rate_limit': {'window': window(30)}}]})
        future = snap.main_limits[-1]
        self.assertEqual(future.window_seconds, 864000)
        self.assertEqual(future.metadata['raw']['tier'], 'gold')
        self.assertNotIn(future.display_name, ('주간', '5시간'))
        group = snap.additional_groups[0]
        self.assertEqual(group.category, 'experiment')
        self.assertIsNone(group.limits[0].window_seconds)

    def test_a_list_shaped_rate_limit_is_read_not_dropped(self):
        snap = gpt({'rate_limit': [window(10, FIVE_H), window(80, WEEK)]})
        self.assertEqual([i.remaining_percent for i in snap.main_limits], [90, 20])
        self.assertEqual(qp.select_hero(snap).window_seconds, FIVE_H)
        self.assertFalse(snap.blocked)

    def test_unknown_payload_shapes_do_not_crash(self):
        for raw in ({'additional_rate_limits': 'surprise'},
                    {'additional_rate_limits': [None, 7]},
                    {'additional_rate_limits': [{'rate_limit': []}]}):
            snap = gpt({'rate_limit': {'primary_window': window(10, FIVE_H)}, **raw})
            self.assertEqual(u.representative_percent(snap), 90)
            self.assertTrue(all(group.group_id for group in snap.additional_groups))

    def test_round_trip_keeps_unknown_metadata(self):
        snap = gpt({'rate_limit': {'primary_window': window(10, 999), 'x': window(20, None, note='hi')}})
        restored = p.snapshot_from_dict(p.snapshot_to_dict(snap))
        self.assertEqual([i.quota_id for i in restored.main_limits],
                         [i.quota_id for i in snap.main_limits])
        self.assertEqual(restored.main_limits[-1].metadata['raw']['note'], 'hi')

    def test_missing_percentages_never_become_zero_or_full(self):
        snap = ProviderSnapshot('chatgpt', 'GPT', 'Plus', True, None, '', main_limits=[
            quota('primary_window', '5시간', None, window_seconds=FIVE_H)])
        self.assertIsNone(u.representative_percent(snap))
        self.assertEqual(u.remaining_marks(snap), [])
        self.assertIsNone(qp.limiting_quota(snap))
        self.assertIsNone(AlertGate().observe('chatgpt', snap))


if __name__ == '__main__':
    unittest.main()
