import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import claude_bridge as bridge
import claude_integration as integ
import providers as p
import usage_widget as u
from providers import ProviderSnapshot, QuotaBar
from runtime import AlertGate, limiting_quota


def _stdin_payload(session="sess-a", transcript=None, five=1, week=0, five_reset=None, week_reset=None, extra=None):
    now = time.time()
    data = {
        "session_id": session,
        "version": "2.1.276",
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": five_reset or now + 10000},
            "seven_day": {"used_percentage": week, "resets_at": week_reset or now + 200000},
        },
    }
    if transcript:
        data["transcript_path"] = str(transcript)
    if extra:
        data.update(extra)
    return json.dumps(data)


class ClaudeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.sessions = self.dir / "sessions"
        self.env = patch.dict(os.environ, {"AIUSAGE_CLAUDE_DIR": str(self.dir)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_int_and_float_used_percentage(self):
        self.assertEqual(bridge.parse_used_percentage(0), 0.0)
        self.assertEqual(bridge.parse_used_percentage(1), 1.0)
        self.assertEqual(bridge.parse_used_percentage(23.5), 23.5)
        self.assertEqual(bridge.remaining_from_used(23.5), 76.5)

    def test_reset_parsing_unix_and_millis(self):
        self.assertEqual(bridge.parse_unix_seconds(1789723200), 1789723200.0)
        self.assertEqual(bridge.parse_unix_seconds(1789723200000), 1789723200.0)

    def test_timer_tick_does_not_refresh_quota_observed_at(self):
        payload = _stdin_payload()
        first = bridge.ingest_statusline(payload, now=1000)
        second = bridge.ingest_statusline(payload, now=1003)
        self.assertEqual(first["quota_observed_at"], 1000)
        self.assertEqual(second["quota_observed_at"], 1000)
        self.assertEqual(second["bridge_seen_at"], 1003)

    def test_salt_stays_stable_if_file_disappears(self):
        payload = _stdin_payload()
        first = bridge.ingest_statusline(payload, now=1000)
        salt = bridge.salt_path()
        self.assertTrue(salt.is_file())
        salt.unlink()
        second = bridge.ingest_statusline(payload, now=1003)
        self.assertEqual(first["session_key"], second["session_key"])
        self.assertEqual(second["quota_observed_at"], 1000)
        self.assertEqual(second["bridge_seen_at"], 1003)

    def test_transcript_mtime_refresh_updates_quota_observed_at(self):
        transcript = self.dir / "t.jsonl"
        transcript.write_text("one", encoding="utf-8")
        first_payload = _stdin_payload(transcript=transcript, five=1)
        first = bridge.ingest_statusline(first_payload, now=1000)
        os.utime(transcript, (2000, 2000))
        later_payload = _stdin_payload(transcript=transcript, five=2)
        second = bridge.ingest_statusline(later_payload, now=1010)
        self.assertEqual(first["quota_observed_at"], 1000)
        self.assertEqual(second["quota_observed_at"], 1010)
        self.assertEqual(second["five_hour"]["used_percent"], 2)
        third = bridge.ingest_statusline(later_payload, now=1013)
        self.assertEqual(third["quota_observed_at"], 1010)
        self.assertEqual(third["bridge_seen_at"], 1013)
        self.assertIn("last_transcript_mtime", third)

    def test_window_drop_does_not_count_as_server_fresh(self):
        payload = json.loads(_stdin_payload())
        bridge.ingest_statusline(json.dumps(payload), now=1000)
        payload["rate_limits"].pop("seven_day")
        after = bridge.ingest_statusline(json.dumps(payload), now=1012)
        self.assertEqual(after["quota_observed_at"], 1000)
        self.assertIn("five_hour", after)
        self.assertNotIn("seven_day", after)

    def test_missing_window_is_not_100_percent(self):
        cache = {"five_hour": {"used_percent": 1, "remaining_percent": 99, "resets_at": 9}}
        self.assertIsNone(cache.get("seven_day"))

    def test_expired_window_is_unconfirmed(self):
        item = p._claude_quota_item(
            "five_hour",
            {"used_percent": 10, "remaining_percent": 90, "resets_at": 50},
            now=100,
        )
        self.assertIsNone(item)

    def test_raw_session_id_and_transcript_path_are_not_stored(self):
        transcript = self.dir / "secret-path.jsonl"
        transcript.write_text("x", encoding="utf-8")
        session = "raw-session-id-should-not-leak"
        bridge.ingest_statusline(_stdin_payload(session=session, transcript=transcript), now=1)
        text = ""
        for path in self.sessions.glob("*.json"):
            text += path.read_text(encoding="utf-8")
            self.assertNotIn(session, path.name)
        self.assertNotIn(session, text)
        self.assertNotIn("secret-path", text)
        self.assertNotIn("transcript_path", text)
        self.assertNotIn("session_id", text)

    def test_atomic_write_ignores_temp_and_corrupt(self):
        (self.sessions).mkdir(parents=True, exist_ok=True)
        (self.sessions / ".foo.json.1.tmp").write_text("{", encoding="utf-8")
        (self.sessions / "broken.json").write_text("{not json", encoding="utf-8")
        good = {
            "source": "claude_statusline",
            "session_key": "abc",
            "quota_observed_at": 9,
            "bridge_seen_at": time.time(),
            "five_hour": {"used_percent": 1, "remaining_percent": 99, "resets_at": time.time() + 1000},
        }
        bridge.write_session_cache("abc", good)
        caches = bridge.list_session_caches()
        self.assertEqual(len(caches), 1)
        self.assertEqual(caches[0]["session_key"], "abc")

    def test_unknown_cache_field_does_not_break_read(self):
        self.sessions.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": "claude_statusline",
            "session_key": "x",
            "quota_observed_at": time.time(),
            "bridge_seen_at": time.time(),
            "future_field": {"nested": 1},
            "five_hour": {"used_percent": 3, "remaining_percent": 97, "resets_at": time.time() + 10},
        }
        (self.sessions / "x.json").write_text(json.dumps(payload), encoding="utf-8")
        caches = bridge.list_session_caches()
        self.assertEqual(len(caches), 1)
        self.assertNotIn("future_field", caches[0])
        self.assertEqual(caches[0]["five_hour"]["used_percent"], 3)

    def test_inactive_bridge_does_not_mark_fresh_quota_stale(self):
        now = time.time()
        cache = {
            "session_key": "alive-quota",
            "quota_observed_at": now - 30,
            "bridge_seen_at": now - 25,
            "five_hour": {"used_percent": 1, "remaining_percent": 99, "resets_at": now + 1000},
        }
        self.assertTrue(bridge.session_inactive(cache, now))
        self.assertFalse(bridge.quota_stale(cache, now))

    def test_timer_tick_session_does_not_beat_newer_inactive_quota(self):
        now = time.time()
        ticking = {
            "session_key": "tick",
            "quota_observed_at": now - 40,
            "bridge_seen_at": now,
            "five_hour": {"used_percent": 90, "remaining_percent": 10, "resets_at": now + 1000},
        }
        newer = {
            "session_key": "newer",
            "quota_observed_at": now - 5,
            "bridge_seen_at": now - 25,
            "five_hour": {"used_percent": 1, "remaining_percent": 99, "resets_at": now + 1000},
        }
        chosen = bridge.select_session_cache([ticking, newer], now)
        self.assertEqual(chosen["session_key"], "newer")

    def test_multi_session_prefers_quota_observed_at_not_last_writer(self):
        now = time.time()
        old = {
            "session_key": "old",
            "quota_observed_at": now - 30,
            "bridge_seen_at": now,
            "five_hour": {"used_percent": 90, "remaining_percent": 10, "resets_at": now + 1000},
        }
        fresh = {
            "session_key": "fresh",
            "quota_observed_at": now - 1,
            "bridge_seen_at": now - 5,
            "five_hour": {"used_percent": 1, "remaining_percent": 99, "resets_at": now + 1000},
            "seven_day": {"used_percent": 0, "remaining_percent": 100, "resets_at": now + 2000},
        }
        chosen = bridge.select_session_cache([old, fresh], now)
        self.assertEqual(chosen["session_key"], "fresh")
        self.assertEqual(chosen["five_hour"]["used_percent"], 1)

    def test_does_not_merge_windows_across_sessions(self):
        now = time.time()
        a = {
            "session_key": "a",
            "quota_observed_at": now - 8,
            "bridge_seen_at": now,
            "five_hour": {"used_percent": 1, "remaining_percent": 99, "resets_at": now + 1000},
        }
        b = {
            "session_key": "b",
            "quota_observed_at": now - 1,
            "bridge_seen_at": now,
            "seven_day": {"used_percent": 0, "remaining_percent": 100, "resets_at": now + 2000},
        }
        chosen = bridge.select_session_cache([a, b], now)
        self.assertEqual(chosen["session_key"], "b")
        self.assertNotIn("five_hour", chosen)

    def test_wrapper_forwards_original_stdin(self):
        command = f'"{__import__("sys").executable}" -c "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"'
        integ.atomic_write_json(bridge.integration_path(), {
            "installed": True,
            "original_statusline": {"type": "command", "command": command},
        })
        payload = _stdin_payload()

        class Stream:
            def __init__(self, data=b""):
                self.buffer = io.BytesIO(data)

        stdin = Stream(payload.encode("utf-8"))
        stdout = Stream()
        stderr = Stream()
        with patch.object(bridge.sys, "stdin", stdin), patch.object(bridge.sys, "stdout", stdout), patch.object(bridge.sys, "stderr", stderr):
            code = bridge.main([])
        self.assertEqual(code, 0)
        forwarded = stdout.buffer.getvalue().decode("utf-8")
        self.assertEqual(json.loads(forwarded)["version"], "2.1.276")
        caches = list(self.sessions.glob("*.json"))
        self.assertTrue(caches)
        stored = caches[0].read_text(encoding="utf-8")
        self.assertNotIn("session_id", stored)
        self.assertNotIn("sess-a", stored)


class ClaudeProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"AIUSAGE_CLAUDE_DIR": str(self.dir)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def _install(self):
        (self.dir / "statusline_bridge.py").write_text("#", encoding="utf-8")
        bridge.atomic_write_json(bridge.integration_path(), {"installed": True})

    def _write(self, key, payload):
        payload = dict(payload)
        payload.setdefault("source", "claude_statusline")
        payload.setdefault("session_key", key)
        payload.setdefault("bridge_seen_at", time.time())
        payload.setdefault("quota_observed_at", time.time())
        bridge.write_session_cache(key, payload)

    def test_five_hour_and_seven_day(self):
        self._install()
        now = time.time()
        self._write("s", {
            "quota_observed_at": now,
            "bridge_seen_at": now,
            "five_hour": {"used_percent": 1, "remaining_percent": 99, "resets_at": now + 1000},
            "seven_day": {"used_percent": 0, "remaining_percent": 100, "resets_at": now + 2000},
        })
        with patch.object(integ, "claude_ready", return_value=(True, "ok")):
            snap = p.fetch_claude(now)
        self.assertTrue(snap.ok)
        self.assertFalse(snap.stale)
        self.assertEqual(snap.hero_percent, 99)
        self.assertEqual([item.raw_identifier for item in snap.main_limits], ["five_hour", "seven_day"])
        self.assertEqual(snap.hero_caption, "5시간 기준 잔여")
        self.assertEqual(u.hero_bar_index(snap), 0)
        self.assertEqual(u.representative_percent(snap), 99)

    def test_five_hour_only_and_seven_day_only(self):
        self._install()
        now = time.time()
        self._write("a", {"five_hour": {"used_percent": 10, "remaining_percent": 90, "resets_at": now + 50}})
        with patch.object(integ, "claude_ready", return_value=(True, "ok")):
            snap = p.fetch_claude(now)
        self.assertEqual(snap.hero_percent, 90)
        self.assertEqual(snap.main_limits[0].raw_identifier, "five_hour")
        self._write("a", {"seven_day": {"used_percent": 40, "remaining_percent": 60, "resets_at": now + 50}})
        with patch.object(integ, "claude_ready", return_value=(True, "ok")):
            snap = p.fetch_claude(now)
        self.assertEqual(snap.hero_percent, 60)
        self.assertEqual(snap.main_limits[0].raw_identifier, "seven_day")
        self.assertEqual(snap.hero_caption, "주간 기준 잔여")

    def test_both_missing(self):
        self._install()
        self._write("empty", {"quota_observed_at": time.time(), "bridge_seen_at": time.time()})
        with patch.object(integ, "claude_ready", return_value=(True, "ok")):
            snap = p.fetch_claude()
        self.assertFalse(snap.ok)
        self.assertIsNone(snap.hero_percent)
        self.assertIn("사용량 창", snap.error)

    def test_stale_is_not_exhausted(self):
        self._install()
        now = time.time()
        self._write("s", {
            "quota_observed_at": now - bridge.STALE_AFTER_SECONDS - 10,
            "bridge_seen_at": now - 100,
            "five_hour": {"used_percent": 1, "remaining_percent": 99, "resets_at": now + 1000},
        })
        with patch.object(integ, "claude_ready", return_value=(True, "ok")):
            snap = p.fetch_claude(now)
        self.assertTrue(snap.ok)
        self.assertTrue(snap.stale)
        self.assertEqual(snap.hero_percent, 99)
        self.assertEqual(u.visual_state(snap), "stale")
        self.assertIsNone(AlertGate().observe("claude", snap))

    def test_inactive_session_keeps_fresh_quota(self):
        self._install()
        now = time.time()
        self._write("s", {
            "quota_observed_at": now - 30,
            "bridge_seen_at": now - 25,
            "five_hour": {"used_percent": 1, "remaining_percent": 99, "resets_at": now + 1000},
        })
        with patch.object(integ, "claude_ready", return_value=(True, "ok")):
            snap = p.fetch_claude(now)
        self.assertTrue(snap.ok)
        self.assertFalse(snap.stale)
        self.assertEqual(snap.hero_percent, 99)
        self.assertEqual(u.visual_state(snap), "ok")
        self.assertFalse(snap.footer)

    def test_weekly_warning_does_not_recolor_hero(self):
        snap = ProviderSnapshot(
            "claude", "Claude", "Claude", True, 99, "5시간 기준 잔여",
            bars=[QuotaBar("5시간", 99, 1, ""), QuotaBar("주간", 5, 95, "")],
        )
        self.assertEqual(u.representative_percent(snap), 99)
        self.assertEqual(u.visual_state(snap), "ok")
        self.assertEqual(u.chip_style("claude", snap)[0], u.CHIP_OK["claude"])
        self.assertEqual(limiting_quota(snap), (5, "주간"))
        self.assertEqual(
            u.quota_alert_copy("claude", 1, 5, "주간"),
            ("Claude 주간 한도 임박", "주간 한도 · 잔여 5%"),
        )

    def test_not_in_rest_polling_policies(self):
        from polling import POLICIES
        self.assertNotIn("claude", POLICIES)


class ClaudeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.settings = self.dir / "settings.json"
        env = patch.dict(os.environ, {
            "AIUSAGE_CLAUDE_DIR": str(self.dir / "cache"),
            "AIUSAGE_CLAUDE_SETTINGS": str(self.settings),
        })
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.ready = patch.object(integ, "claude_ready", return_value=(True, "ok"))
        self.ready.start()
        self.addCleanup(self.ready.stop)

    def test_install_without_existing_statusline(self):
        meta = integ.install_statusline()
        self.assertTrue(meta["installed"])
        self.assertFalse(meta["had_original"])
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["statusLine"]["type"], "command")
        self.assertIn("statusline_bridge.py", data["statusLine"]["command"])
        self.assertTrue((self.dir / "cache" / "statusline_bridge.py").is_file())

    def test_install_preserves_existing_statusline_in_backup(self):
        original = {"type": "command", "command": "jq .", "padding": 2, "future": True}
        self.settings.write_text(json.dumps({"statusLine": original, "other": 1}), encoding="utf-8")
        integ.install_statusline()
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertNotEqual(data["statusLine"]["command"], "jq .")
        self.assertEqual(data["other"], 1)
        backup = integ.load_integration()["original_statusline"]
        self.assertEqual(backup["command"], "jq .")
        self.assertEqual(backup["future"], True)
        self.assertEqual(integ.uninstall_statusline(), "restored")
        restored = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(restored["statusLine"], original)

    def test_uninstall_without_original_removes_statusline(self):
        integ.install_statusline()
        self.assertEqual(integ.uninstall_statusline(), "removed")
        data = json.loads(self.settings.read_text(encoding="utf-8")) if self.settings.is_file() else {}
        self.assertNotIn("statusLine", data)

    def test_user_modified_statusline_is_conflict(self):
        integ.install_statusline()
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "whoami"}}), encoding="utf-8")
        self.assertEqual(integ.conflict_state(), "conflict")
        self.assertEqual(integ.uninstall_statusline(), "conflict")
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["statusLine"]["command"], "whoami")


class ClaudeUiTests(unittest.TestCase):
    def test_card_hero_and_weekly_layout(self):
        root = u.tk.Tk()
        root.withdraw()
        try:
            card = u.Card(root, "claude")
            snap = ProviderSnapshot(
                "claude", "Claude", "Claude", True, 99, "",
                bars=[QuotaBar("5시간", 99, 1, "", "9월 18일 18:20"), QuotaBar("주간", 100, 0, "", "9월 25일 14:00")],
            )
            card.render(snap)
            self.assertEqual(card.rows.itemcget("hero", "text"), "99%")
            texts = [card.rows.itemcget(i, "text") for i in card.rows.find_all() if card.rows.type(i) == "text"]
            self.assertTrue(any("5시간 한도" in text for text in texts))
            self.assertTrue(any("주간 한도" in text for text in texts))
            self.assertEqual(card.rows.itemcget("severity", "text"), "여유")
            self.assertFalse(card.rows.find_withtag("bar_0"))
            self.assertTrue(card.rows.find_withtag("bar_1"))
            card.destroy()
        finally:
            root.destroy()

    def test_stale_card_keeps_numbers(self):
        root = u.tk.Tk()
        root.withdraw()
        try:
            card = u.Card(root, "claude")
            snap = ProviderSnapshot(
                "claude", "Claude", "Claude", True, 99, "",
                bars=[QuotaBar("5시간", 99, 1, "")],
                stale=True,
            )
            card.render(snap)
            self.assertEqual(card.rows.itemcget("hero", "text"), "99%")
            self.assertEqual(card.rows.itemcget("severity", "text"), "이전 데이터")
            card.destroy()
        finally:
            root.destroy()

    def test_unavailable_card(self):
        root = u.tk.Tk()
        root.withdraw()
        try:
            card = u.Card(root, "claude")
            snap = p.error_snapshot("claude", "Claude", "사용량 창을 기다리는 중", "")
            card.render(snap)
            texts = [card.rows.itemcget(i, "text") for i in card.rows.find_all() if card.rows.type(i) == "text"]
            self.assertTrue(any("사용량 창" in text for text in texts))
            card.destroy()
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
