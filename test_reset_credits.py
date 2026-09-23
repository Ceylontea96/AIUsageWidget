"""GPT reset-credit expiry: parsing, cadence, transport and display. Offline."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import providers as p
import usage_widget as u

NOW = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc).timestamp()


def iso(days, hours=0):
    moment = datetime.fromtimestamp(NOW + days * 86400 + hours * 3600, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def credit(days, status="available", **extra):
    item = {"id": f"credit-{days}-{status}", "status": status, "expires_at": iso(days),
            "title": "Full reset (Weekly + 5 hr)", "reset_type": "codex_rate_limits"}
    item.update(extra)
    return item


class FakeAuth:
    def load(self): pass
    def ensure_fresh(self): pass
    def access_token(self): return "fake"
    def account_id(self): return ""


def usage_body(available):
    return {
        "rate_limit": {
            "primary_window": {"used_percent": 10, "limit_window_seconds": 18000, "reset_after_seconds": 3600},
            "secondary_window": {"used_percent": 20, "limit_window_seconds": 604800, "reset_after_seconds": 86400},
        },
        "rate_limit_reset_credits": {"available_count": available, "applicable_available_count": 0},
    }


class ResetCreditCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "reset_credits.json"
        env = patch.dict(os.environ, {"AIUSAGE_RESET_CREDITS_CACHE": str(self.cache)})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

    def serve(self, usage=None, credits=None, credits_status=200, fail=False):
        def respond(method, url, headers, body=None, timeout=None):
            self.calls.append((method, url))
            if url == p.RESET_CREDITS_URL:
                if fail:
                    raise RuntimeError("offline")
                return credits_status, {"available_count": len(credits or []), "credits": credits or []}
            return 200, usage
        return patch.object(p, "http_json", side_effect=respond)

    def credit_calls(self):
        return [call for call in self.calls if call[1] == p.RESET_CREDITS_URL]


class NearestExpiryTests(unittest.TestCase):
    def test_picks_the_nearest_of_several(self):
        body = {"credits": [credit(30), credit(3), credit(12)]}
        self.assertAlmostEqual(p.nearest_reset_credit_expiry(body, NOW), NOW + 3 * 86400, places=3)

    def test_only_usable_future_credits_count(self):
        body = {"credits": [
            credit(1, status="redeemed"),
            credit(2, status="expired"),
            credit(-1),
            {"status": "available", "expires_at": None},
            {"status": "available", "expires_at": "soon"},
            {"status": "available"},
            "not a credit",
            credit(9),
        ]}
        self.assertAlmostEqual(p.nearest_reset_credit_expiry(body, NOW), NOW + 9 * 86400, places=3)

    def test_nothing_usable_means_no_date(self):
        for body in ({"credits": []}, {"credits": None}, {}, None, {"credits": [credit(4, status="used")]}):
            with self.subTest(body=body):
                self.assertIsNone(p.nearest_reset_credit_expiry(body, NOW))

    def test_real_timestamp_format(self):
        body = {"credits": [{"status": "available", "expires_at": "2026-10-22T19:58:56.745296Z"}]}
        expected = datetime(2026, 10, 22, 19, 58, 56, 745296, timezone.utc).timestamp()
        self.assertAlmostEqual(p.nearest_reset_credit_expiry(body, NOW), expected, places=3)


class ExpiryCadenceTests(ResetCreditCase):
    def test_first_read_fetches_the_list_and_caches_no_identifiers(self):
        with self.serve(credits=[credit(5)]):
            expiry = p.reset_credit_expiry({}, 1, NOW)
        self.assertAlmostEqual(expiry, NOW + 5 * 86400, places=3)
        self.assertEqual(self.credit_calls(), [("GET", p.RESET_CREDITS_URL)])
        stored = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(set(stored), {"fetched_at", "available_count", "nearest_expires_at"})
        self.assertNotIn("credit-5", self.cache.read_text(encoding="utf-8"))

    def test_fresh_cache_with_same_count_skips_the_request(self):
        with self.serve(credits=[credit(5)]):
            p.reset_credit_expiry({}, 1, NOW)
            for step in range(1, 30):
                p.reset_credit_expiry({}, 1, NOW + step * 2)
        self.assertEqual(len(self.credit_calls()), 1)

    def test_count_change_refetches_at_once(self):
        with self.serve(credits=[credit(5)]):
            p.reset_credit_expiry({}, 1, NOW)
        with self.serve(credits=[credit(5), credit(2)]):
            expiry = p.reset_credit_expiry({}, 2, NOW + 10)
        self.assertEqual(len(self.credit_calls()), 2)
        self.assertAlmostEqual(expiry, NOW + 2 * 86400, places=3)

    def test_ttl_refetches(self):
        with self.serve(credits=[credit(5)]):
            p.reset_credit_expiry({}, 1, NOW)
            p.reset_credit_expiry({}, 1, NOW + p.RESET_CREDITS_TTL + 1)
        self.assertEqual(len(self.credit_calls()), 2)

    def test_a_passed_cached_date_refetches(self):
        self.cache.write_text(json.dumps({"fetched_at": NOW - 10, "available_count": 1,
                                          "nearest_expires_at": NOW - 1}), encoding="utf-8")
        with self.serve(credits=[credit(7)]):
            expiry = p.reset_credit_expiry({}, 1, NOW)
        self.assertEqual(len(self.credit_calls()), 1)
        self.assertAlmostEqual(expiry, NOW + 7 * 86400, places=3)

    def test_failure_keeps_a_known_future_date(self):
        self.cache.write_text(json.dumps({"fetched_at": NOW - p.RESET_CREDITS_TTL - 5, "available_count": 1,
                                          "nearest_expires_at": NOW + 3600}), encoding="utf-8")
        with self.serve(fail=True):
            self.assertEqual(p.reset_credit_expiry({}, 1, NOW), NOW + 3600)
        with self.serve(credits_status=503):
            self.assertEqual(p.reset_credit_expiry({}, 1, NOW), NOW + 3600)

    def test_failure_without_a_cache_just_hides_the_date(self):
        with self.serve(fail=True):
            self.assertIsNone(p.reset_credit_expiry({}, 1, NOW))

    def test_corrupt_cache_is_ignored(self):
        self.cache.write_text("{not json", encoding="utf-8")
        with self.serve(credits=[credit(4)]):
            self.assertAlmostEqual(p.reset_credit_expiry({}, 1, NOW), NOW + 4 * 86400, places=3)


class FetchChatgptTests(ResetCreditCase):
    def fetch(self, available, **serve):
        with patch.object(p, "ChatGptAuth", FakeAuth), patch.object(p.time, "time", return_value=NOW), \
             self.serve(usage=usage_body(available), **serve):
            return p.fetch_chatgpt()

    def test_no_credits_means_no_extra_request(self):
        snap = self.fetch(0, credits=[credit(5)])
        self.assertTrue(snap.ok)
        self.assertEqual(self.credit_calls(), [])
        self.assertIsNone(u.billing_entry(snap, "reset_credits"))

    def test_credits_carry_the_nearest_expiry(self):
        snap = self.fetch(2, credits=[credit(20), credit(6)])
        item = u.billing_entry(snap, "reset_credits")
        self.assertEqual(item.raw_value, 2)
        self.assertAlmostEqual(item.metadata["nearest_expires_at"], NOW + 6 * 86400, places=3)

    def test_list_failure_never_breaks_the_card(self):
        snap = self.fetch(1, fail=True)
        self.assertTrue(snap.ok)
        item = u.billing_entry(snap, "reset_credits")
        self.assertEqual(item.raw_value, 1)
        self.assertNotIn("nearest_expires_at", item.metadata)

    def test_expiry_survives_the_worker_json_round_trip(self):
        snap = self.fetch(1, credits=[credit(6)])
        wire = json.loads(json.dumps(p.snapshot_to_dict(snap), allow_nan=False))
        back = p.snapshot_from_dict(wire)
        self.assertAlmostEqual(u.billing_entry(back, "reset_credits").metadata["nearest_expires_at"],
                               NOW + 6 * 86400, places=3)

    def test_the_spending_endpoint_is_never_referenced(self):
        source = Path(p.__file__).read_text(encoding="utf-8")
        self.assertNotIn("rate-limit-reset-credits/consume", source)
        self.assertNotIn("/consume", p.RESET_CREDITS_URL)


class DisplayTests(unittest.TestCase):
    def snap(self, available, metadata=None):
        billing = [p.BillingItem("chatgpt:billing:reset_credits", "chatgpt", "reset_credits",
                                 available, metadata or {})] if available is not None else []
        return p.ProviderSnapshot("chatgpt", "GPT", "Plus", True, 80, "x", billing=billing)

    def test_count_and_nearest_expiry(self):
        expiry = NOW + 6 * 86400
        text = u.reset_credit(self.snap(2, {"nearest_expires_at": expiry}), now=NOW)
        self.assertEqual(text, f"리셋권 2 · {p.fmt_local(expiry, 'reset')} 만료")

    def test_a_passed_expiry_is_not_shown(self):
        self.assertEqual(u.reset_credit(self.snap(1, {"nearest_expires_at": NOW - 1}), now=NOW), "리셋권 1")

    def test_count_alone_without_a_date(self):
        self.assertEqual(u.reset_credit(self.snap(1), now=NOW), "리셋권 1")

    def test_no_credits_no_text(self):
        self.assertEqual(u.reset_credit(self.snap(0), now=NOW), "")
        self.assertEqual(u.reset_credit(self.snap(None), now=NOW), "")

    def test_extras_line_carries_the_date(self):
        expiry = NOW + 6 * 86400
        line = u.quota_extras(self.snap(1, {"nearest_expires_at": expiry}))
        self.assertIn("만료", line)
        self.assertIn(p.fmt_local(expiry, "reset"), line)


class CardRepaintTests(unittest.TestCase):
    """The card must redraw when only its extras line changes."""

    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        self.patches = [patch.object(u, "SETTINGS_PATH", directory / "settings.json"),
                        patch.object(u, "CACHE_PATH", directory / "cache.json")]
        for item in self.patches:
            item.start()
        self.animate = u.UpdatePill.animate
        u.UpdatePill.animate = False
        self.w = u.UsageWidget(preview=True)
        self.w.root.withdraw()

    def tearDown(self):
        self.w.close()
        u.UpdatePill.animate = self.animate
        for item in self.patches:
            item.stop()

    def drawn_extras(self):
        canvas = self.w.cards["chatgpt"].rows
        texts = [canvas.itemcget(i, "text") for i in canvas.find_all() if canvas.type(i) == "text"]
        return next((t for t in texts if "리셋권" in t or "크레딧" in t), "")

    def show(self, billing):
        bars = [p.QuotaBar("5시간", 80.0, 20.0, "")]
        snap = p.ProviderSnapshot("chatgpt", "GPT", "Plus", True, 80.0, "5시간 기준 잔여",
                                  bars=bars, billing=billing)
        self.w.snapshots["chatgpt"] = snap
        self.w.render("chatgpt")
        self.w.root.update_idletasks()
        return self.drawn_extras()

    def reset(self, available, expiry=None):
        return p.BillingItem("chatgpt:billing:reset_credits", "chatgpt", "reset_credits", available,
                             {"nearest_expires_at": expiry} if expiry else {})

    def test_credit_balance_change_alone_repaints(self):
        self.assertIn("크레딧 5", self.show([p.BillingItem("chatgpt:billing:credits", "chatgpt", "credits", 5),
                                               self.reset(1)]))
        self.assertIn("크레딧 9", self.show([p.BillingItem("chatgpt:billing:credits", "chatgpt", "credits", 9),
                                               self.reset(1)]))

    def test_new_expiry_alone_repaints_and_fits_the_card(self):
        import time as clock
        later = clock.time() + 20 * 86400
        self.assertEqual(self.show([self.reset(1)]), "리셋권 1")
        text = self.show([self.reset(1, later)])
        self.assertIn(p.fmt_local(later, "reset"), text)
        canvas = self.w.cards["chatgpt"].rows
        item = next(i for i in canvas.find_all() if canvas.type(i) == "text" and "리셋권" in canvas.itemcget(i, "text"))
        self.assertLessEqual(canvas.bbox(item)[2], int(canvas.cget("width")))


if __name__ == "__main__":
    unittest.main()
