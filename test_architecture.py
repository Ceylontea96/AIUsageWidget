from datetime import datetime, timezone
from email.utils import format_datetime
import io
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

import providers as p
import usage_widget as u
from polling import PollingPolicy, next_poll_delay, policy_for, retry_after_delay
from runtime import limiting_quota


class QuotaModelTests(unittest.TestCase):
    def test_legacy_snapshot_builds_canonical_main_limits(self):
        # Restoring a 3.4.x cache must produce identity that is deterministic,
        # stable across restarts and free of collisions. The exact id string is
        # an implementation detail; those four properties are the contract.
        def restore():
            return p.ProviderSnapshot(
                'chatgpt', 'GPT', 'Plus', True, 80, '',
                bars=[p.QuotaBar('5시간', 80, 20, '', '', '[null, 18000]'),
                      p.QuotaBar('5시간', 40, 60, '', '', '[null, 18000]')],
            )

        snap = restore()
        self.assertEqual(len(snap.main_limits), 2)
        first, second = snap.main_limits
        self.assertEqual(first.window_seconds, 18000)
        self.assertEqual(first.display_name, '5시간')
        # Two windows of the same length keep separate identities.
        self.assertNotEqual(first.quota_id, second.quota_id)
        # Identity never embeds the display name, and repeats exactly.
        self.assertNotIn('5시간', first.quota_id)
        self.assertEqual([item.quota_id for item in restore().main_limits],
                         [item.quota_id for item in snap.main_limits])

    def test_new_snapshot_builds_legacy_bars(self):
        item = p.QuotaItem(
            'chatgpt:main:window:18000', 'chatgpt', 'main', '5시간',
            raw_identifier='window:18000', window_seconds=18000,
            used_percent=25, remaining_percent=75,
        )
        snap = p.ProviderSnapshot('chatgpt', 'GPT', 'Plus', True, 75, '', main_limits=[item])
        self.assertEqual([(bar.label, bar.remaining_percent) for bar in snap.bars], [('5시간', 75)])

    def test_round_trip_preserves_groups_billing_and_unknown_window(self):
        unknown = p.QuotaItem(
            'chatgpt:additional:group-x:unknown-1', 'chatgpt', 'additional', '기간 미상',
            raw_identifier='opaque-window', remaining_percent=41, scope='model',
        )
        group = p.LimitGroup(
            'chatgpt:additional:group-x', 'chatgpt', '추가 한도',
            raw_identifier='group-x', limits=[unknown], scope='model',
        )
        billing = p.BillingItem(
            'cursor:billing:opaque', 'cursor', 'opaque', {'future': 1},
            metadata={'unknown': True},
        )
        snap = p.ProviderSnapshot(
            'chatgpt', 'GPT', 'Plus', True, 90, '',
            bars=[p.QuotaBar('5시간', 90, 10, '')],
            additional_groups=[group], billing=[billing],
        )
        restored = p.snapshot_from_dict(p.snapshot_to_dict(snap))
        self.assertEqual(restored.additional_groups, [group])
        self.assertEqual(restored.billing, [billing])
        self.assertIsNone(restored.additional_groups[0].limits[0].window_seconds)

    def test_nonfinite_generic_billing_is_safe_for_worker_json(self):
        snap = p.ProviderSnapshot(
            'cursor', 'Cursor', 'Pro', True, 50, '',
            billing=[p.BillingItem('b', 'cursor', 'future', float('nan'))],
        )
        payload = p.snapshot_to_dict(snap)
        self.assertIsNone(payload['billing'][0]['raw_value'])

    def test_old_cache_payload_remains_readable(self):
        restored = p.snapshot_from_dict({
            'key': 'chatgpt', 'title': 'GPT', 'plan': 'Plus', 'ok': True,
            'hero_percent': 63, 'hero_caption': '5시간 기준 잔여',
            'bars': [{'label': '5시간', 'remaining_percent': 63,
                      'used_percent': 37, 'detail': '잔여 63%'}],
        })
        self.assertEqual(restored.bars[0].remaining_percent, 63)
        self.assertEqual(restored.main_limits[0].remaining_percent, 63)
        self.assertTrue(restored.stale)

    def test_display_name_is_not_canonical_identity(self):
        first = p.QuotaItem('provider:main:raw-1', 'provider', 'main', 'Old', raw_identifier='raw-1')
        renamed = p.QuotaItem('provider:main:raw-1', 'provider', 'main', 'New', raw_identifier='raw-1')
        self.assertEqual(first.quota_id, renamed.quota_id)
        self.assertNotEqual(first.display_name, renamed.display_name)

    def test_limit_group_keeps_multiple_windows(self):
        group = p.LimitGroup(
            'chatgpt:additional:spark', 'chatgpt', 'GPT-X Spark',
            raw_identifier='spark', limits=[
                p.QuotaItem(
                    'chatgpt:additional:spark:window:18000', 'chatgpt', 'additional', '5시간',
                    raw_identifier='window:18000', window_seconds=18000, remaining_percent=70,
                    scope='model',
                ),
                p.QuotaItem(
                    'chatgpt:additional:spark:window:604800', 'chatgpt', 'additional', '주간',
                    raw_identifier='window:604800', window_seconds=604800, remaining_percent=52,
                    scope='model',
                ),
            ],
        )
        restored = p.snapshot_from_dict(p.snapshot_to_dict(p.ProviderSnapshot(
            'chatgpt', 'GPT', 'Plus', True, 80, '',
            bars=[p.QuotaBar('5시간', 80, 20, '')], additional_groups=[group],
        )))
        self.assertEqual(len(restored.additional_groups), 1)
        self.assertEqual(len(restored.additional_groups[0].limits), 2)
        self.assertEqual(
            [item.window_seconds for item in restored.additional_groups[0].limits],
            [18000, 604800],
        )
        self.assertEqual(len(restored.bars), 1)


class PollingPolicyTests(unittest.TestCase):
    def test_current_intervals_are_preserved(self):
        policy = PollingPolicy()
        base = dict(ok=True, blocked=False, hero_percent=80, main_remaining=[80])
        self.assertEqual(next_poll_delay(policy, **base), 30)
        self.assertEqual(next_poll_delay(policy, **{**base, 'main_remaining': [35]}), 20)
        self.assertEqual(next_poll_delay(policy, **base, active=True), 2)
        self.assertEqual(next_poll_delay(policy, **{**base, 'blocked': True}), 300)
        self.assertEqual([next_poll_delay(policy, **base, failures=n) for n in (1, 2, 3, 6)],
                         [30, 60, 120, 900])
        self.assertEqual(policy_for('chatgpt'), policy_for('cursor'))
        self.assertNotIn('claude', __import__('polling').POLICIES)

    def test_retry_after_integer_is_not_clamped(self):
        policy = PollingPolicy(max_backoff=900)
        delay = next_poll_delay(
            policy, ok=False, blocked=False, hero_percent=None, main_remaining=[],
            failures=1, retry_after='7200',
        )
        self.assertEqual(delay, 7200)

    def test_retry_after_http_date_is_respected(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()
        header = format_datetime(datetime.fromtimestamp(now + 123, timezone.utc), usegmt=True)
        self.assertEqual(retry_after_delay(header, now), 123)

    def test_invalid_negative_and_past_retry_after_fall_back(self):
        policy = PollingPolicy()
        args = dict(ok=False, blocked=False, hero_percent=None, main_remaining=[], failures=2)
        self.assertEqual(next_poll_delay(policy, **args, retry_after='invalid'), 60)
        self.assertEqual(next_poll_delay(policy, **args, retry_after='-1'), 60)
        past = format_datetime(datetime(2020, 1, 1, tzinfo=timezone.utc), usegmt=True)
        self.assertEqual(next_poll_delay(policy, **args, retry_after=past, now=2_000_000_000), 60)

    def test_additional_quota_does_not_change_global_polling(self):
        group = p.LimitGroup(
            'g', 'chatgpt', 'Scoped', limits=[
                p.QuotaItem('q', 'chatgpt', 'additional', 'Model', remaining_percent=0)
            ],
        )
        snap = p.ProviderSnapshot(
            'chatgpt', 'GPT', 'Plus', True, 80, '',
            bars=[p.QuotaBar('5시간', 80, 20, '')], additional_groups=[group],
        )
        self.assertEqual(u.next_interval(snap), 30)

    def test_widget_schedule_uses_unclamped_retry_after(self):
        widget = u.UsageWidget.__new__(u.UsageWidget)
        widget.failures = {'chatgpt': 1}
        widget.due = {}
        snap = p.error_snapshot('chatgpt', 'GPT', 'rate limited', '', '7200')
        widget._schedule_poll('chatgpt', snap, 100.0, active=False)
        self.assertEqual(widget.due['chatgpt'], 7300.0)

    def test_http_error_captures_retry_after_without_body_or_credentials(self):
        headers = Message()
        headers['Retry-After'] = '321'
        error = urllib.error.HTTPError(
            'https://example.invalid', 429, 'limited', headers, io.BytesIO(b''))
        with patch.object(p._OPENER, 'open', side_effect=error):
            status, payload = p.http_json('GET', 'https://example.invalid', {})
        self.assertEqual(status, 429)
        self.assertEqual(payload, {})
        self.assertEqual(payload.retry_after, '321')

    def test_additional_quota_does_not_change_limiting_quota(self):
        group = p.LimitGroup(
            'g', 'chatgpt', 'Scoped', limits=[
                p.QuotaItem('q', 'chatgpt', 'additional', 'Model', remaining_percent=0)
            ],
        )
        snap = p.ProviderSnapshot(
            'chatgpt', 'GPT', 'Plus', True, 80, '',
            bars=[p.QuotaBar('5시간', 80, 20, '')], additional_groups=[group],
        )
        self.assertEqual(limiting_quota(snap), (80, '5시간'))


class FakeCursorAuth:
    access_token = 'fake'
    plan = 'pro'

    def load(self):
        return None

    def ensure_fresh(self):
        return None


def _cursor_usage_payload():
    return p.JsonPayload({
        'planUsage': {
            'autoPercentUsed': 10.0,
            'apiPercentUsed': 40.0,
            'totalPercentUsed': 99.0,
            'includedSpend': 1.0,
            'limit': 2.0,
            'remaining': 3.0,
            'bonusSpend': 4.0,
            'remainingBonus': 5.0,
            'totalSpend': 6.0,
        },
        'billingCycleEnd': 1893456000,
        'displayMessage': "You've used 99% of your included total usage",
    })


class CursorMappingTests(unittest.TestCase):
    def _fetch(self, payload=None):
        usage = payload or _cursor_usage_payload()

        def fake_http(method, url, headers, body=None, timeout=8.0):
            if 'GetCurrentPeriodUsage' in url:
                return 200, usage
            return 200, p.JsonPayload({'planInfo': {'planName': 'Pro'}})

        with patch.object(p, 'CursorAuth', FakeCursorAuth), patch.object(p, 'http_json', fake_http):
            return p.fetch_cursor()

    def test_auto_and_api_map_to_named_pools(self):
        snap = self._fetch()
        self.assertEqual([item.display_name for item in snap.main_limits], ['Cursor Models', 'Other Models'])
        self.assertEqual([bar.label for bar in snap.bars], ['Cursor Models', 'Other Models'])
        self.assertEqual(snap.hero_percent, 90.0)
        self.assertEqual(snap.hero_caption, 'Cursor Models 기준 잔여')
        self.assertEqual(snap.bars[0].remaining_percent, 90.0)
        self.assertEqual(snap.bars[1].remaining_percent, 60.0)
        self.assertEqual(snap.internal.get('totalPercentUsed'), 99.0)

    def test_total_percent_is_internal_and_not_in_freeze_history(self):
        snap = self._fetch()
        texts = ' '.join(bar.label for bar in snap.bars)
        self.assertNotIn('totalPercentUsed', texts)
        self.assertNotIn('전체', snap.hero_caption)
        marks = u.remaining_marks(snap)
        self.assertNotIn(1.0, marks)
        self.assertNotIn(99.0, marks)
        self.assertEqual(u.hero_index(snap), 0)
        self.assertEqual(u.representative_percent(snap), 90.0)

    def test_billing_items_stay_generic(self):
        snap = self._fetch()
        kinds = [item.kind for item in snap.billing]
        for kind in ('includedSpend', 'limit', 'remaining', 'bonusSpend'):
            self.assertIn(kind, kinds)
        self.assertFalse(any('포함' in item.kind or '보너스' in item.kind for item in snap.billing))


class AdditionalParserTests(unittest.TestCase):
    def test_count_is_group_count_not_window_count(self):
        groups = p.additional_groups_from_payload('chatgpt', {
            'additional_rate_limits': [{
                'limit_id': 'spark',
                'display_name': 'GPT-X Spark',
                'rate_limit': {
                    'primary_window': {'used_percent': 20, 'limit_window_seconds': 18000, 'reset_after_seconds': 60},
                    'secondary_window': {'used_percent': 40, 'limit_window_seconds': 604800, 'reset_after_seconds': 3600},
                },
            }]
        })
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].limits), 2)
        self.assertEqual(groups[0].group_id, 'chatgpt:additional:spark')
        self.assertEqual([item.window_seconds for item in groups[0].limits], [18000, 604800])

    def test_unknown_fields_are_preserved(self):
        groups = p.additional_groups_from_payload('chatgpt', {
            'additional_rate_limits': [{
                'limit_id': 'future',
                'display_name': 'Future Model',
                'mystery_flag': True,
                'nested': {'keep': 1},
                'rate_limit': {
                    'primary_window': {
                        'used_percent': 12,
                        'limit_window_seconds': 900,
                        'future_token_unit': 'spark',
                    }
                },
            }]
        })
        self.assertEqual(groups[0].metadata['raw']['mystery_flag'], True)
        self.assertEqual(groups[0].metadata['raw']['nested'], {'keep': 1})
        self.assertEqual(groups[0].limits[0].metadata['raw']['future_token_unit'], 'spark')
        self.assertEqual(groups[0].limits[0].window_label, '15분')

    def test_non_list_payload_is_kept_generically(self):
        groups = p.additional_groups_from_payload('chatgpt', {
            'additional_rate_limits': {'unexpected': 'shape', 'used_percent': 7},
        })
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].category, 'unknown')
        self.assertEqual(groups[0].metadata['raw']['unexpected'], 'shape')

    def test_identity_is_stable_across_display_rename(self):
        body = {
            'additional_rate_limits': [{
                'limit_id': 'spark',
                'display_name': 'Old',
                'rate_limit': {'primary_window': {'used_percent': 1, 'limit_window_seconds': 18000}},
            }]
        }
        first = p.additional_groups_from_payload('chatgpt', body)[0]
        body['additional_rate_limits'][0]['display_name'] = 'New'
        second = p.additional_groups_from_payload('chatgpt', body)[0]
        self.assertEqual(first.group_id, second.group_id)
        self.assertEqual(first.limits[0].quota_id, second.limits[0].quota_id)
        self.assertNotEqual(first.display_name, second.display_name)


if __name__ == '__main__':
    unittest.main(verbosity=2)
