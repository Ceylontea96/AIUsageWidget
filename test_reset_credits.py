"""GPT reset-credit expiry from Codex's rate-limit answer: parsing and display. Offline."""
import json
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


def app_server_result(available, credits=()):
    """Shaped like Codex's account/rateLimits/read answer."""
    return {
        "rateLimits": {
            "limitId": "codex", "planType": "plus", "rateLimitReachedType": None,
            "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": int(NOW) + 3600},
            "secondary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": int(NOW) + 86400},
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        },
        "rateLimitResetCredits": {"availableCount": available, "credits": list(credits)},
    }


def app_credit(days, status="available"):
    return {"id": f"credit-{days}-{status}", "resetType": "codexRateLimits", "status": status,
            "grantedAt": int(NOW) - 86400, "expiresAt": int(NOW + days * 86400),
            "title": "Full reset (Weekly + 5 hr)", "description": "granted"}


class NearestExpiryTests(unittest.TestCase):
    def test_picks_the_nearest_of_several(self):
        body = {"credits": [credit(30), credit(3), credit(12)]}
        self.assertAlmostEqual(p.nearest_reset_credit_expiry(body, NOW), NOW + 3 * 86400, places=3)

    def test_only_usable_future_credits_count(self):
        body = {"credits": [
            credit(1, status="redeemed"),
            credit(2, status="redeeming"),
            credit(-1),
            {"status": "available", "expires_at": None},
            {"status": "available", "expires_at": "soon"},
            {"status": "available"},
            "not a credit",
            credit(9),
        ]}
        self.assertAlmostEqual(p.nearest_reset_credit_expiry(body, NOW), NOW + 9 * 86400, places=3)

    def test_nothing_usable_means_no_date(self):
        for body in ({"credits": []}, {"credits": None}, {}, None, {"credits": [credit(4, status="redeemed")]}):
            with self.subTest(body=body):
                self.assertIsNone(p.nearest_reset_credit_expiry(body, NOW))


class ExpiryFormatTests(unittest.TestCase):
    def test_epoch_seconds_millis_and_iso_are_read_alike(self):
        expected = NOW + 5 * 86400
        for value in (int(expected), int(expected) * 1000, iso(5)):
            with self.subTest(value=value):
                body = {"credits": [{"status": "available", "expires_at": value}]}
                self.assertAlmostEqual(p.nearest_reset_credit_expiry(body, NOW), expected, delta=1)

    def test_booleans_are_not_timestamps(self):
        body = {"credits": [{"status": "available", "expires_at": True}]}
        self.assertIsNone(p.nearest_reset_credit_expiry(body, NOW))


class FetchChatgptTests(unittest.TestCase):
    def fetch(self, result):
        with patch.object(p.time, "time", return_value=NOW):
            return p.fetch_chatgpt(lambda: result)

    def test_no_credits_means_no_billing_item(self):
        snap = self.fetch(app_server_result(0))
        self.assertTrue(snap.ok)
        self.assertIsNone(u.billing_entry(snap, "reset_credits"))

    def test_credits_carry_the_nearest_expiry_from_the_same_answer(self):
        snap = self.fetch(app_server_result(2, [app_credit(20), app_credit(6), app_credit(1, "redeemed")]))
        item = u.billing_entry(snap, "reset_credits")
        self.assertEqual(item.raw_value, 2)
        self.assertAlmostEqual(item.metadata["nearest_expires_at"], NOW + 6 * 86400, delta=1)

    def test_missing_credit_list_keeps_the_count(self):
        snap = self.fetch(app_server_result(1))
        item = u.billing_entry(snap, "reset_credits")
        self.assertEqual(item.raw_value, 1)
        self.assertNotIn("nearest_expires_at", item.metadata)

    def test_expiry_survives_the_json_round_trip(self):
        snap = self.fetch(app_server_result(1, [app_credit(6)]))
        wire = json.loads(json.dumps(p.snapshot_to_dict(snap), allow_nan=False))
        back = p.snapshot_from_dict(wire)
        self.assertAlmostEqual(u.billing_entry(back, "reset_credits").metadata["nearest_expires_at"],
                               NOW + 6 * 86400, delta=1)

    def test_credit_ids_and_titles_never_reach_the_snapshot(self):
        snap = self.fetch(app_server_result(1, [app_credit(6)]))
        text = json.dumps(p.snapshot_to_dict(snap), ensure_ascii=False)
        self.assertNotIn("credit-6-available", text)
        self.assertNotIn("Full reset", text)


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
