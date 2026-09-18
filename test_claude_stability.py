"""Offline 3.4.1 regressions: no real settings, credentials, or CLI requests."""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import claude_bridge as bridge
import claude_integration as integ
import providers as p
import usage_widget as u


def control(body, now=1000):
    return p.claude_usage_from_control_output(json.dumps({
        "type": "control_response",
        "response": {"subtype": "success", "response": body},
    }), now)


def snapshot(observed=1000, source="claude_cli", stale=False):
    snap = control({"limits": [{"kind": "five_hour", "percent": 25}]}, observed)
    return replace(snap, stale=stale, internal={"source": source, "quota_observed_at": observed})


class ClaudeFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.w = u.UsageWidget.__new__(u.UsageWidget)
        self.w.claude_cli_snapshot = None
        self.w.claude_cli_at = float("-inf")
        self.w.claude_cli_due = 0
        self.w.snapshots = {}
        self.w.failures = {"claude": 0}
        self.w.request_started = {}
        self.w.due = {}
        self.w.preview = True
        self.w.render = Mock()
        self.w._sync_activity_ui = Mock()
        self.w.runner = Mock()
        self.missing = p.error_snapshot("claude", "Claude", "missing", "")
        fetch = patch.object(u, "fetch_claude", return_value=self.missing)
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)
        clock = patch.object(u.time, "monotonic", return_value=100)
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    def receive(self, snap):
        self.w.accept("claude", snap, is_new=True)

    def test_repeated_display_expires_without_restamping(self):
        self.receive(snapshot())
        for elapsed in range(0, 1201, 2):
            self.clock.return_value = 100 + elapsed
            self.w.start_claude_job()
            self.assertEqual(self.w.claude_cli_at, 100)
            self.assertEqual(self.w.snapshots["claude"].stale, elapsed >= u.CLAUDE_CLI_STALE)
        self.assertEqual(self.w.snapshots["claude"].hero_percent, 75)

    def test_common_accept_and_render_do_not_record_a_cli_response(self):
        self.w.accept("claude", snapshot())
        self.w.render("claude")
        self.assertEqual(self.w.claude_cli_at, float("-inf"))
        self.assertIsNone(self.w.claude_cli_snapshot)

    def test_only_successful_new_response_advances_freshness(self):
        self.receive(snapshot())
        self.clock.return_value = 200
        self.receive(self.missing)
        self.assertEqual(self.w.claude_cli_at, 100)
        self.assertTrue(self.w.snapshots["claude"].stale)
        self.clock.return_value = 300
        self.receive(snapshot(1200))
        self.assertEqual(self.w.claude_cli_at, 300)
        self.assertFalse(self.w.snapshots["claude"].stale)

    def test_stale_statusline_starts_cli_when_due(self):
        self.fetch.return_value = snapshot(100, "claude_statusline", True)
        self.w.start_claude_job()
        self.w.runner.start.assert_called_once_with("claude", 100)
        self.assertTrue(self.w.snapshots["claude"].stale)
        self.w.start_claude_job()
        self.w.runner.start.assert_called_once()

    def test_fresh_statusline_suppresses_cli(self):
        self.fetch.return_value = snapshot(100, "claude_statusline")
        self.w.start_claude_job()
        self.w.runner.start.assert_not_called()
        self.assertFalse(self.w.snapshots["claude"].stale)

    def test_cli_failure_preserves_stale_statusline(self):
        self.fetch.return_value = snapshot(100, "claude_statusline", True)
        self.w.start_claude_job()
        self.receive(self.missing)
        self.assertTrue(self.w.snapshots["claude"].stale)
        self.assertEqual(self.w.snapshots["claude"].hero_percent, 75)
        self.assertEqual(self.w.claude_cli_at, float("-inf"))

    def test_cli_failure_does_not_replace_fresh_statusline(self):
        self.fetch.return_value = snapshot(100, "claude_statusline")
        self.receive(self.missing)
        self.assertFalse(self.w.snapshots["claude"].stale)
        self.assertEqual(self.w.snapshots["claude"].internal["source"], "claude_statusline")

    def test_fresh_source_wins_over_stale_and_tie_prefers_statusline(self):
        self.receive(snapshot(100))
        self.fetch.return_value = snapshot(200, "claude_statusline", True)
        self.w.start_claude_job()
        self.assertEqual(self.w.snapshots["claude"].internal["source"], "claude_cli")
        self.fetch.return_value = snapshot(100, "claude_statusline")
        self.w.start_claude_job()
        self.assertEqual(self.w.snapshots["claude"].internal["source"], "claude_statusline")

    def test_observation_time_not_last_writer_selects_source(self):
        self.fetch.return_value = snapshot(200, "claude_statusline")
        self.receive(snapshot(100))
        self.assertEqual(self.w.snapshots["claude"].fetched_at, 200)
        self.receive(snapshot(300))
        self.w.start_claude_job()  # Older statusLine is read last.
        self.assertEqual(self.w.snapshots["claude"].fetched_at, 300)

    def test_expired_cli_cannot_hide_fresh_statusline(self):
        self.receive(snapshot(200))
        self.clock.return_value = 700
        self.fetch.return_value = snapshot(100, "claude_statusline")
        self.w.start_claude_job()
        self.assertEqual(self.w.snapshots["claude"].internal["source"], "claude_statusline")
        self.assertEqual(self.w.claude_cli_at, 100)

    def test_missing_statusline_retains_newer_historical_observation(self):
        self.receive(snapshot(100))
        self.fetch.return_value = snapshot(200, "claude_statusline")
        self.w.start_claude_job()
        self.clock.return_value = 700
        self.fetch.return_value = self.missing
        self.receive(self.missing)
        self.assertTrue(self.w.snapshots["claude"].stale)
        self.assertEqual(self.w.snapshots["claude"].fetched_at, 200)
        self.w.start_claude_job()
        self.assertEqual(self.w.snapshots["claude"].fetched_at, 200)


class ClaudeCliParserSafetyTests(unittest.TestCase):
    def test_explicit_percent_int_float_and_boundaries(self):
        for used in (0, 1, 25, 25.5, 100, 0.25):
            with self.subTest(used=used):
                snap = control({"limits": [{"kind": "five_hour", "percent": used}]})
                self.assertTrue(snap.ok)
                self.assertEqual(snap.hero_percent, 100 - used)

    def test_ambiguous_utilization_is_unavailable_even_with_reset(self):
        for used in (0, 0.25, 1, 25, 100):
            with self.subTest(used=used):
                snap = control({"rate_limits": {"five_hour": {
                    "utilization": used, "resets_at": "2099-01-01T00:00:00Z",
                }}})
                self.assertFalse(snap.ok)
                self.assertIsNone(snap.hero_percent)

    def test_consistent_percent_and_utilization_are_cross_checked(self):
        for utilization in (25, 0.25):
            snap = control({"limits": [{"kind": "five_hour", "percent": 25}],
                            "rate_limits": {"five_hour": {"utilization": utilization}}})
            self.assertEqual(snap.hero_percent, 75)

    def test_scale_mismatch_never_silently_displays_wrong_percentage(self):
        for row in (
            {"percent": 25, "utilization": 0.5},
            {"percent": 25, "utilization": 0.25, "utilization_scale": "percent"},
            {"percent": 25, "utilization": 25, "utilization_scale": "fraction"},
        ):
            with self.subTest(row=row):
                snap = control({"limits": [dict(row, kind="five_hour")]})
                self.assertFalse(snap.ok)
                self.assertIsNone(snap.hero_percent)

    def test_explicit_scale_supported_without_size_heuristic(self):
        for utilization, scale, expected in ((0.25, "fraction", 75), (0.25, "percent", 99.75),
                                             (1, "fraction", 0), (1, "percent", 99)):
            snap = control({"limits": [{"kind": "five_hour", "utilization": utilization,
                                        "utilization_scale": scale}]})
            self.assertEqual(snap.hero_percent, expected)

    def test_semantic_kinds_override_labels_and_raw_keys(self):
        snap = control({"limits": [{"kind": "seven_day", "label": "5h", "percent": 10}],
                        "rate_limits": {"seven_day": {"kind": "five_hour", "percent": 25}}})
        self.assertEqual([item.raw_identifier for item in snap.main_limits], ["five_hour", "seven_day"])
        self.assertEqual([item.remaining_percent for item in snap.main_limits], [75, 90])

    def test_unknown_or_malformed_kind_is_not_mapped_by_label_or_key(self):
        for kind in ("unknown", None, [], {}):
            with self.subTest(kind=kind):
                snap = control({"limits": [{"kind": kind, "label": "five_hour", "percent": 25}],
                                "rate_limits": {"five_hour": {"kind": kind, "percent": 25}}})
                self.assertFalse(snap.ok)

    def test_invalid_numbers_and_unknown_scale_are_unavailable(self):
        for row in ({"percent": True}, {"percent": "25"}, {"percent": -1}, {"percent": 101},
                    {"percent": float("nan")}, {"percent": float("inf")}, {"percent": 10 ** 1000},
                    {"utilization": 0.25, "utilization_scale": "unknown"}):
            with self.subTest(row=row):
                self.assertFalse(control({"limits": [dict(row, kind="five_hour")]}).ok)

    def test_conflicting_duplicate_percent_is_unavailable(self):
        snap = control({"limits": [{"kind": "five_hour", "percent": 25}],
                        "rate_limits": {"five_hour": {"percent": 50}}})
        self.assertFalse(snap.ok)


class ClaudeSettingsSafetyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Path(tmp.name) / "settings.json"
        self.cache = Path(tmp.name) / "cache"
        env = patch.dict(os.environ, {"AIUSAGE_CLAUDE_DIR": str(self.cache),
                                    "AIUSAGE_CLAUDE_SETTINGS": str(self.settings)})
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("claude_ready", (True, "ok")), ("claude_version_text", "2.1.276"),
                            ("resolve_claude_executable", None)):
            mock = patch.object(integ, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_missing_settings_can_be_created(self):
        integ.install_statusline()
        self.assertIn("statusLine", integ.read_user_settings())

    def test_existing_settings_preserve_other_fields(self):
        before = {"theme": "dark", "permissions": {"allow": ["Read"]}, "hooks": {}, "mcpServers": {}}
        self.settings.write_text(json.dumps(before), encoding="utf-8")
        integ.install_statusline()
        after = integ.read_user_settings()
        after.pop("statusLine")
        self.assertEqual(after, before)

    def test_corrupt_nonobject_and_invalid_encoding_settings_fail_closed(self):
        for raw in (b'{"theme": "dark",', b"[]", b"null", b"\xff\xfe\x00"):
            with self.subTest(raw=raw):
                self.settings.write_bytes(raw)
                with self.assertRaisesRegex(RuntimeError, "settings.json"):
                    integ.install_statusline()
                self.assertEqual(self.settings.read_bytes(), raw)
                self.assertFalse(bridge.integration_path().exists())
                self.assertFalse(integ.wrapper_script_path().exists())
                self.assertEqual(integ.conflict_state(), "unreadable")

    def test_unreadable_settings_are_not_treated_as_missing(self):
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(RuntimeError, "settings.json"):
                integ.install_statusline()
        self.assertFalse(bridge.integration_path().exists())

    def test_backup_failure_prevents_settings_write(self):
        original = b'{ "theme": "dark" }\r\n'
        self.settings.write_bytes(original)
        with patch.object(integ, "atomic_write_json", side_effect=OSError("backup failed")), \
                patch.object(integ, "_write_user_settings") as write:
            with self.assertRaises(OSError):
                integ.install_statusline()
            write.assert_not_called()
        self.assertEqual(self.settings.read_bytes(), original)

    def test_atomic_settings_write_failure_keeps_bytes_and_prepared_backup(self):
        original = b'{ "theme": "dark", "statusLine": {"type":"command","command":"echo original"} }\r\n'
        self.settings.write_bytes(original)
        real_replace = os.replace

        def fail_settings(src, dst):
            if Path(dst) == self.settings:
                meta = integ.load_integration()
                self.assertEqual(meta["original_statusline"]["command"], "echo original")
                raise OSError("simulated atomic replacement failure")
            return real_replace(src, dst)

        with patch.object(bridge.os, "replace", side_effect=fail_settings):
            with self.assertRaises(OSError):
                integ.install_statusline()
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertFalse(integ.is_installed())
        self.assertEqual(integ.load_integration()["original_statusline"]["command"], "echo original")


# Shape captured from a real `get_usage` control response (Claude Code 2.1.276,
# Pro). Only quota numbers and reset stamps are kept; nothing identifying.
LIVE_RATE_LIMITS = {
    "five_hour": {
        "utilization": 69,
        "resets_at": "2026-09-18T09:20:00.538822+00:00",
        "limit_dollars": None, "used_dollars": None,
        "remaining_dollars": None, "locked_reason": None,
    },
    "seven_day": {
        "utilization": 9,
        "resets_at": "2026-09-25T05:00:00.538843+00:00",
        "limit_dollars": None, "used_dollars": None,
        "remaining_dollars": None, "locked_reason": None,
    },
    "seven_day_opus": None,
    "limits": [
        {"kind": "session", "group": "session", "percent": 69,
         "resets_at": "2026-09-18T09:20:00.538822+00:00", "severity": "normal",
         "scope": None, "is_active": True},
        {"kind": "weekly_all", "group": "weekly", "percent": 9,
         "resets_at": "2026-09-25T05:00:00.538843+00:00", "severity": "normal",
         "scope": None, "is_active": False},
    ],
}


class ClaudeCliLiveShapeTests(unittest.TestCase):
    """The shipped parser must read the shape the CLI actually sends."""

    def test_live_response_shape_is_readable(self):
        snap = control({
            "subscription_type": "pro",
            "rate_limits_available": True,
            "rate_limits": LIVE_RATE_LIMITS,
        })
        self.assertTrue(snap.ok, snap.error)
        self.assertEqual(snap.hero_percent, 31.0)
        self.assertEqual(
            [(item.raw_identifier, item.remaining_percent) for item in snap.main_limits],
            [("five_hour", 31.0), ("seven_day", 91.0)],
        )
        self.assertTrue(all(item.reset_at for item in snap.main_limits))

    def test_kind_rows_live_inside_rate_limits(self):
        # `limits` is nested, not a body-level key; both placements must work.
        nested = p.claude_windows_from_rate_limits(LIVE_RATE_LIMITS)
        top_level = p.claude_windows_from_rate_limits(
            {k: v for k, v in LIVE_RATE_LIMITS.items() if k != "limits"},
            LIVE_RATE_LIMITS["limits"],
        )
        self.assertEqual(nested, top_level)
        self.assertEqual(sorted(nested), ["five_hour", "seven_day"])

    def test_scoped_weekly_rows_are_not_global_windows(self):
        rate_limits = {"limits": [
            {"kind": "weekly_scoped", "group": "weekly", "percent": 0,
             "scope": {"model": {"display_name": "Opus"}}},
        ]}
        self.assertEqual(p.claude_windows_from_rate_limits(rate_limits), {})

    def test_live_shape_still_rejects_a_scale_mismatch(self):
        broken = dict(LIVE_RATE_LIMITS)
        broken["five_hour"] = dict(LIVE_RATE_LIMITS["five_hour"], utilization=0.42)
        windows = p.claude_windows_from_rate_limits(broken)
        self.assertNotIn("five_hour", windows)
        self.assertIn("seven_day", windows)


if __name__ == "__main__":
    unittest.main()
