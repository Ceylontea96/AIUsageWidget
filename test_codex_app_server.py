"""GPT usage through Codex's own app-server: adapter, client and runner. Offline."""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_app_server as cas
import providers as p
import usage_widget as u
from runtime import CodexJob, PollRunner

# A real `account/rateLimits/read` answer (Codex CLI 0.153.4, Plus), with the
# account id and credit id removed.
LIVE_RESULT = {
    "rateLimits": {
        "limitId": "codex", "limitName": None,
        "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1790155938},
        "secondary": {"usedPercent": 15, "windowDurationMins": 10080, "resetsAt": 1790661170},
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "individualLimit": None, "spendControlReached": False,
        "planType": "plus", "rateLimitReachedType": None,
    },
    "rateLimitsByLimitId": {"codex": {
        "limitId": "codex", "limitName": None,
        "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1790155938},
        "secondary": {"usedPercent": 15, "windowDurationMins": 10080, "resetsAt": 1790661170},
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "individualLimit": None, "spendControlReached": False,
        "planType": "plus", "rateLimitReachedType": None,
    }},
    "rateLimitResetCredits": {"availableCount": 1, "credits": [{
        "id": "credit-id", "resetType": "codexRateLimits", "status": "available",
        "grantedAt": 1790107136, "expiresAt": 1792699136,
        "title": "Full reset (Weekly + 5 hr)",
        "description": "Thanks for using Codex! You've been granted one free rate limit reset.",
    }]},
    "accountId": "account-id", "rateLimitUpsell": None,
}
NOW = 1790130000.0

FAKE_SERVER = r'''
import json, sys, time
mode, log = sys.argv[1], sys.argv[2]
result = json.loads(sys.argv[3])
def note(method):
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(method + "\n")
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    note(method)
    if method == "initialize":
        reply = {"id": message["id"], "result": {"userAgent": "fake"}}
    elif method == "account/rateLimits/read":
        if mode == "hang":
            time.sleep(60)
        if mode == "exit":
            sys.exit(3)
        if mode == "auth":
            reply = {"id": message["id"], "error": {"code": -32000, "message": "not logged in"}}
        else:
            reply = {"id": message["id"], "result": result}
    else:
        continue
    print(json.dumps({"method": "remoteControl/status/changed", "params": {}}), flush=True)
    print(json.dumps(reply), flush=True)
'''


class AdapterTests(unittest.TestCase):
    def snapshot(self, result):
        return p.chatgpt_snapshot(p.usage_body_from_app_server(result), NOW)

    def test_live_answer_matches_the_card_it_replaced(self):
        snap = self.snapshot(LIVE_RESULT)
        self.assertTrue(snap.ok)
        self.assertEqual(snap.plan, "ChatGPT Plus")
        self.assertEqual([(i.quota_id, i.display_name, i.remaining_percent, i.reset_at) for i in snap.main_limits], [
            ("chatgpt:main:primary_window", "5시간", 100.0, 1790155938),
            ("chatgpt:main:secondary_window", "주간", 85.0, 1790661170),
        ])
        self.assertEqual(snap.hero_caption, "5시간 기준 잔여")
        self.assertFalse(snap.blocked)
        self.assertEqual(snap.additional_groups, [])
        item = u.billing_entry(snap, "reset_credits")
        self.assertEqual((item.raw_value, item.metadata["nearest_expires_at"]), (1, 1792699136))
        self.assertIsNone(u.billing_entry(snap, "credits"))

    def test_other_limit_ids_become_scoped_groups(self):
        result = json.loads(json.dumps(LIVE_RESULT))
        result["rateLimitsByLimitId"]["codex_spark"] = {
            "limitId": "codex_spark", "limitName": "GPT-5.3-Codex-Spark",
            "primary": {"usedPercent": 40, "windowDurationMins": 300, "resetsAt": 1790155000},
            "secondary": {"usedPercent": 5, "windowDurationMins": 10080, "resetsAt": 1790661000},
        }
        snap = self.snapshot(result)
        self.assertEqual(len(snap.main_limits), 2)
        self.assertEqual(len(snap.additional_groups), 1)
        group = snap.additional_groups[0]
        self.assertIn("codex_spark", group.group_id)
        self.assertEqual(group.display_name, "GPT-5.3-Codex-Spark")
        self.assertEqual(sorted(i.remaining_percent for i in group.limits), [60.0, 95.0])
        self.assertTrue(all(i.scope == "scoped" for i in group.limits))

    def test_reached_limit_blocks(self):
        result = json.loads(json.dumps(LIVE_RESULT))
        result["rateLimits"]["rateLimitReachedType"] = "rate_limit_reached"
        snap = self.snapshot(result)
        self.assertTrue(snap.blocked)

    def test_credits_balance_is_carried(self):
        result = json.loads(json.dumps(LIVE_RESULT))
        result["rateLimits"]["credits"] = {"hasCredits": True, "unlimited": False, "balance": "12"}
        self.assertEqual(u.billing_value(self.snapshot(result), "credits"), "12")

    def test_unusable_answers_raise_the_user_message(self):
        for result in (None, {}, {"rateLimits": "x"}, {"rateLimits": {"primary": None}}):
            with self.subTest(result=result):
                with self.assertRaisesRegex(RuntimeError, "한도 정보"):
                    self.snapshot(result)

    def test_identifiers_and_titles_are_dropped(self):
        text = json.dumps(p.usage_body_from_app_server(LIVE_RESULT), ensure_ascii=False)
        for secret in ("credit-id", "account-id", "Full reset", "Thanks for using"):
            self.assertNotIn(secret, text)


class FakeServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.script = root / "fake_app_server.py"
        self.script.write_text(FAKE_SERVER, encoding="utf-8")
        self.log = root / "methods.log"
        self.modes = []
        self.processes = []

    def popen(self, args, **kwargs):
        self.assertEqual(args[1:], ["app-server"])
        mode = self.modes.pop(0) if self.modes else "ok"
        process = subprocess.Popen(
            [sys.executable, str(self.script), mode, str(self.log), json.dumps(LIVE_RESULT)], **kwargs)
        self.processes.append(process)
        return process

    def server(self, *modes, read_timeout=5.0):
        self.modes = list(modes)
        server = cas.CodexAppServer("fake-codex", client_version="test", popen=self.popen,
                                    read_timeout=read_timeout, start_timeout=10.0)
        self.addCleanup(server.close)
        return server

    def methods(self):
        return self.log.read_text(encoding="utf-8").split() if self.log.exists() else []


class ClientTests(FakeServerCase):
    def test_one_server_answers_repeated_reads(self):
        server = self.server()
        for _ in range(3):
            self.assertEqual(server.read_rate_limits()["rateLimits"]["planType"], "plus")
        self.assertEqual(server.starts, 1)
        self.assertEqual(self.methods(), ["initialize", "initialized"] + ["account/rateLimits/read"] * 3)

    def test_signed_out_codex_asks_for_login(self):
        server = self.server("auth")
        with self.assertRaisesRegex(cas.CodexAppServerError, "Codex CLI에 로그인"):
            server.read_rate_limits()

    def test_a_stuck_server_is_replaced(self):
        server = self.server("hang", "ok", read_timeout=0.8)
        started = time.monotonic()
        with self.assertRaises(cas.CodexAppServerError):
            server.read_rate_limits()
        self.assertLess(time.monotonic() - started, 5)
        self.processes[0].wait(timeout=5)
        self.assertIsNotNone(self.processes[0].poll())
        self.assertEqual(server.read_rate_limits()["rateLimits"]["limitId"], "codex")
        self.assertEqual(server.starts, 2)

    def test_a_dead_server_fails_fast_and_restarts(self):
        server = self.server("exit", "ok", read_timeout=30)
        started = time.monotonic()
        with self.assertRaises(cas.CodexAppServerError):
            server.read_rate_limits()
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(server.read_rate_limits()["rateLimits"]["planType"], "plus")
        self.assertEqual(server.starts, 2)

    def test_close_ends_the_process(self):
        server = self.server()
        server.read_rate_limits()
        server.close()
        self.processes[0].wait(timeout=5)
        self.assertIsNotNone(self.processes[0].poll())
        self.assertFalse(server.running)

    def test_the_spending_method_cannot_be_sent(self):
        self.assertEqual(cas.ALLOWED_METHODS, {"initialize", "account/rateLimits/read"})
        server = self.server()
        server.read_rate_limits()
        with self.assertRaises(cas.CodexAppServerError):
            server._send({"id": 99, "method": "account/rateLimitResetCredit/consume", "params": {}})
        self.assertNotIn("account/rateLimitResetCredit/consume", self.methods())

    def test_missing_codex_is_a_clear_error(self):
        server = cas.CodexAppServer(client_version="test")
        with patch.object(cas, "resolve_codex_executable", return_value=None):
            with self.assertRaisesRegex(cas.CodexAppServerError, "Codex CLI를 찾지"):
                server.read_rate_limits()


class ResolveTests(unittest.TestCase):
    def test_native_binary_behind_the_npm_shim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = root / "codex.CMD"
            shim.write_text("@echo off\n", encoding="utf-8")
            native = root / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
            native.parent.mkdir(parents=True)
            native.write_bytes(b"MZ")
            with patch.object(cas.shutil, "which", return_value=str(shim)):
                self.assertEqual(cas.resolve_codex_executable(), native)

    def test_shim_or_exe_or_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            shim = Path(directory) / "codex.cmd"
            shim.write_text("@echo off\n", encoding="utf-8")
            with patch.object(cas.shutil, "which", return_value=str(shim)):
                self.assertEqual(cas.resolve_codex_executable(), shim)
        with patch.object(cas.shutil, "which", return_value=r"C:\tools\codex.exe"):
            self.assertEqual(cas.resolve_codex_executable(), Path(r"C:\tools\codex.exe"))
        with patch.object(cas.shutil, "which", return_value=None):
            self.assertIsNone(cas.resolve_codex_executable())


class FakeJob:
    def __init__(self, payload=None, block=None):
        self.payload = payload
        self.block = block
        self.calls = []

    def run(self):
        self.calls.append("run")
        if self.block is not None:
            self.block.wait(5)
        return self.payload

    def interrupt(self):
        self.calls.append("interrupt")
        if self.block is not None:
            self.block.set()

    def reset(self):
        self.calls.append("reset")

    def close(self):
        self.calls.append("close")


def ok_payload():
    snap = p.chatgpt_snapshot(p.usage_body_from_app_server(LIVE_RESULT), NOW)
    return json.loads(json.dumps(p.snapshot_to_dict(snap)))


class RunnerTests(unittest.TestCase):
    def drain(self, runner, deadline=5.0):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            events = runner.poll(time.monotonic())
            if events:
                return events
            time.sleep(0.02)
        return []

    def test_thread_job_result_flows_like_a_worker(self):
        runner = PollRunner(inprocess={"chatgpt": FakeJob(ok_payload())})
        self.assertTrue(runner.start("chatgpt", time.monotonic()))
        (key, snap, error), = self.drain(runner)
        self.assertEqual((key, error, snap.ok, snap.hero_percent), ("chatgpt", "", True, 100.0))
        self.assertNotIn("chatgpt", runner.slots)

    def test_one_job_per_provider_until_done(self):
        gate = threading.Event()
        runner = PollRunner(inprocess={"chatgpt": FakeJob(ok_payload(), gate)})
        self.assertTrue(runner.start("chatgpt", time.monotonic()))
        self.assertFalse(runner.start("chatgpt", time.monotonic()))
        gate.set()
        self.assertTrue(self.drain(runner))
        self.assertTrue(runner.start("chatgpt", time.monotonic()))

    def test_deadline_interrupts_the_job(self):
        gate = threading.Event()
        job = FakeJob(ok_payload(), gate)
        runner = PollRunner(timeout=0.2, inprocess={"chatgpt": job})
        runner.start("chatgpt", time.monotonic())
        time.sleep(0.3)
        events = runner.poll(time.monotonic())
        self.assertEqual(events[0][0], "chatgpt")
        self.assertIsNone(events[0][1])
        self.assertIn("interrupt", job.calls)

    def test_reset_and_close_reach_the_job(self):
        job = FakeJob(ok_payload())
        runner = PollRunner(inprocess={"chatgpt": job})
        runner.reset("chatgpt")
        runner.reset("cursor")
        runner.close()
        self.assertEqual(job.calls, ["reset", "close"])

    def test_codex_job_turns_failures_into_an_error_snapshot(self):
        class Broken:
            def read_rate_limits(self):
                raise cas.CodexAppServerError("GPT 사용량 조회를 위해 Codex CLI에 로그인한 뒤 새로고침하세요.")
        payload = CodexJob(Broken()).run()
        snap = p.snapshot_from_dict(payload)
        self.assertEqual(snap.key, "chatgpt")
        self.assertFalse(snap.ok)
        self.assertIn("로그인", snap.error)


class WidgetWiringTests(unittest.TestCase):
    def test_widget_reads_gpt_through_codex_and_starts_nothing_until_asked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(u, "SETTINGS_PATH", root / "s.json"), patch.object(u, "CACHE_PATH", root / "c.json"):
                animate = u.UpdatePill.animate
                u.UpdatePill.animate = False
                try:
                    w = u.UsageWidget(preview=True)
                    w.root.withdraw()
                    job = w.runner.inprocess["chatgpt"]
                    self.assertIsInstance(job, CodexJob)
                    self.assertFalse(job.server.running)
                    w.close()
                    self.assertFalse(job.server.running)
                finally:
                    u.UpdatePill.animate = animate

    def test_worker_no_longer_offers_gpt(self):
        import poll_worker
        self.assertNotIn("chatgpt", poll_worker.FETCHERS)


if __name__ == "__main__":
    unittest.main()
