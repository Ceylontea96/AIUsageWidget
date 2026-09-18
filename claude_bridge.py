"""Official Claude Code statusLine bridge: whitelist cache, no credentials."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SOURCE = "claude_statusline"
WINDOW_KEYS = ("five_hour", "seven_day")
KNOWN_WINDOWS = {
    "five_hour": {
        "display_name": "5시간",
        "window_seconds": 18000.0,
        "window_label": "5시간",
        "category": "session",
    },
    "seven_day": {
        "display_name": "주간",
        "window_seconds": 604800.0,
        "window_label": "주간",
        "category": "weekly",
    },
}
# statusLine rate_limits shipped in Claude Code 2.1.80+. Not a pin to 2.1.276.
MIN_CLAUDE_VERSION = (2, 1, 80)
# quota_observed_at older than this is statusLine-source stale, not REST lag.
STALE_AFTER_SECONDS = 15 * 60
# bridge_seen_at older than this means the Claude TUI session is inactive.
SESSION_INACTIVE_AFTER_SECONDS = 20.0
# Widget rereads local cache; this is not a network poll interval.
CACHE_READ_INTERVAL = 2.0
ALLOWED_CACHE_KEYS = {
    "source",
    "schema_version",
    "session_key",
    "claude_code_version",
    "bridge_seen_at",
    "quota_observed_at",
    "five_hour",
    "seven_day",
    "last_transcript_mtime",
    "last_window_fingerprint",
}
ALLOWED_WINDOW_KEYS = {"used_percent", "remaining_percent", "resets_at"}
FORBIDDEN_SUBSTRINGS = (
    "session_id",
    "transcript_path",
    "transcript",
    "prompt",
    "cwd",
    "project",
    "repo",
    "worktree",
    "email",
    "account",
    "credential",
    "token",
    "cookie",
    "authorization",
    "oauth",
    "model",
    "cost",
)


def app_root() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "AiUsageWidget"


def claude_dir() -> Path:
    override = os.environ.get("AIUSAGE_CLAUDE_DIR")
    if override:
        return Path(override)
    return app_root() / "claude"


def sessions_dir() -> Path:
    return claude_dir() / "sessions"


def salt_path() -> Path:
    return claude_dir() / "session.salt"


def integration_path() -> Path:
    return claude_dir() / "integration.json"


def _to_float(value: Any) -> float | None:
    if value is None or value is False:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def parse_unix_seconds(value: Any) -> float | None:
    number = _to_float(value)
    if number is None:
        return None
    if number > 10_000_000_000:
        number /= 1000.0
    if number <= 0:
        return None
    return number


def remaining_from_used(used: float | None) -> float | None:
    if used is None:
        return None
    return max(0.0, min(100.0, 100.0 - float(used)))


def parse_used_percentage(value: Any) -> float | None:
    number = _to_float(value)
    if number is None:
        return None
    if number < 0 or number > 100:
        return max(0.0, min(100.0, number))
    return number


def parse_claude_version(text: str) -> tuple[int, int, int] | None:
    digits = []
    current = ""
    for char in str(text or ""):
        if char.isdigit():
            current += char
        elif current:
            digits.append(int(current))
            current = ""
            if len(digits) == 3:
                break
    if current and len(digits) < 3:
        digits.append(int(current))
    if len(digits) < 3:
        return None
    return digits[0], digits[1], digits[2]


def version_supported(version: tuple[int, int, int] | None) -> bool:
    return version is not None and version >= MIN_CLAUDE_VERSION


def window_view(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    used = parse_used_percentage(raw.get("used_percentage"))
    if used is None:
        used = parse_used_percentage(raw.get("used_percent"))
    reset = parse_unix_seconds(raw.get("resets_at"))
    if used is None and reset is None:
        return None
    remaining = remaining_from_used(used)
    view = {}
    if used is not None:
        view["used_percent"] = used
        view["remaining_percent"] = remaining
    if reset is not None:
        view["resets_at"] = reset
    return view or None


def extract_whitelist(data: Any) -> dict[str, Any]:
    payload = data if isinstance(data, dict) else {}
    rate_limits = payload.get("rate_limits")
    windows = {}
    if isinstance(rate_limits, dict):
        for key in WINDOW_KEYS:
            parsed = window_view(rate_limits.get(key))
            if parsed is not None:
                windows[key] = parsed
    version = payload.get("version")
    return {
        "claude_code_version": str(version) if version not in (None, "") else "",
        "windows": windows,
        "has_rate_limits": isinstance(rate_limits, dict),
    }


def window_fingerprint(windows: dict[str, Any]) -> str:
    parts = []
    for key in WINDOW_KEYS:
        item = windows.get(key)
        if not isinstance(item, dict):
            parts.append(f"{key}:missing")
            continue
        used = item.get("used_percent")
        reset = item.get("resets_at")
        parts.append(f"{key}:{used}:{reset}")
    return "|".join(parts)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def sanitize_cache(payload: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        name = str(key)
        if name not in ALLOWED_CACHE_KEYS:
            continue
        if name in WINDOW_KEYS:
            if not isinstance(value, dict):
                continue
            window = {
                field: value[field]
                for field in ALLOWED_WINDOW_KEYS
                if field in value and _to_float(value[field]) is not None
            }
            if window:
                clean[name] = window
            continue
        clean[name] = value
    return clean


_SALT_MEMO: dict[str, bytes] = {}


def ensure_salt() -> bytes:
    path = salt_path()
    key = str(path)
    cached = _SALT_MEMO.get(key)
    if cached:
        return cached
    try:
        if path.is_file() and path.stat().st_size >= 16:
            secret = path.read_bytes()
            _SALT_MEMO[key] = secret
            return secret
    except OSError:
        pass
    secret = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        handle = os.open(path, flags, 0o600)
        try:
            os.write(handle, secret)
            os.fsync(handle)
        finally:
            os.close(handle)
    except FileExistsError:
        secret = path.read_bytes()
    except OSError:
        try:
            if path.is_file() and path.stat().st_size >= 16:
                secret = path.read_bytes()
        except OSError:
            pass
    _SALT_MEMO[key] = secret
    return secret


def _nums_eq(left: Any, right: Any) -> bool:
    first = _to_float(left)
    second = _to_float(right)
    if first is None and second is None:
        return True
    if first is None or second is None:
        return False
    return abs(first - second) <= 1e-6


def opaque_session_key(session_id: str = "", transcript_path: str = "") -> str:
    material = str(session_id or "").strip() or str(transcript_path or "").strip() or "unknown"
    digest = hmac.new(ensure_salt(), material.encode("utf-8", "replace"), hashlib.sha256)
    return digest.hexdigest()


def transcript_mtime(path_text: str) -> float | None:
    text = str(path_text or "").strip()
    if not text:
        return None
    try:
        stat = Path(text).stat()
    except OSError:
        return None
    return float(stat.st_mtime)


def load_session(session_key: str) -> dict[str, Any] | None:
    path = sessions_dir() / f"{session_key}.json"
    data = _load_json(path)
    return sanitize_cache(data) if data else None


def list_session_caches(now: float | None = None) -> list[dict[str, Any]]:
    folder = sessions_dir()
    if not folder.is_dir():
        return []
    items = []
    for path in folder.glob("*.json"):
        if path.name.startswith("."):
            continue
        data = _load_json(path)
        if not data:
            continue
        try:
            items.append(sanitize_cache(data))
        except Exception:
            continue
    return items


def session_inactive(cache: dict[str, Any], now: float) -> bool:
    seen = _to_float(cache.get("bridge_seen_at"))
    if seen is None:
        return True
    return now - seen > SESSION_INACTIVE_AFTER_SECONDS


def quota_stale(cache: dict[str, Any], now: float) -> bool:
    observed = _to_float(cache.get("quota_observed_at"))
    if observed is None:
        return True
    return now - observed > STALE_AFTER_SECONDS


def window_expired(window: dict[str, Any] | None, now: float) -> bool:
    if not isinstance(window, dict):
        return False
    reset = parse_unix_seconds(window.get("resets_at"))
    return reset is not None and now >= reset


def select_session_cache(caches: list[dict[str, Any]], now: float | None = None) -> dict[str, Any] | None:
    current = time.time() if now is None else float(now)
    ranked = []
    for cache in caches:
        if not isinstance(cache, dict):
            continue
        observed = _to_float(cache.get("quota_observed_at")) or 0.0
        seen = _to_float(cache.get("bridge_seen_at")) or 0.0
        stale = quota_stale(cache, current)
        ranked.append((stale, -observed, -seen, cache))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    fresh = [item for item in ranked if not item[0]]
    chosen = (fresh or ranked)[0][3]
    return chosen


def _should_refresh_quota(
    previous: dict[str, Any] | None,
    windows: dict[str, Any],
    mtime: float | None,
) -> bool:
    if not windows:
        return False
    current_fp = window_fingerprint(windows)
    if previous is None or _to_float(previous.get("quota_observed_at")) is None:
        return True
    previous_fp = str(previous.get("last_window_fingerprint") or "")
    if not previous_fp:
        previous_fp = window_fingerprint({
            key: previous.get(key)
            for key in WINDOW_KEYS
            if isinstance(previous.get(key), dict)
        })
    previous_mtime = _to_float(previous.get("last_transcript_mtime"))
    same_quota = bool(previous_fp) and previous_fp == current_fp
    if same_quota:
        return (
            mtime is not None
            and previous_mtime is not None
            and mtime > previous_mtime + 1e-6
        )
    prev_windows = {key: previous.get(key) for key in WINDOW_KEYS}
    for key in WINDOW_KEYS:
        before = prev_windows.get(key) if isinstance(prev_windows.get(key), dict) else None
        after = windows.get(key)
        if not isinstance(after, dict):
            continue
        if before is None:
            return True
        if not _nums_eq(before.get("used_percent"), after.get("used_percent")):
            return True
        if not _nums_eq(before.get("resets_at"), after.get("resets_at")):
            return True
    return False


def build_session_cache(
    *,
    session_key: str,
    whitelist: dict[str, Any],
    previous: dict[str, Any] | None,
    transcript_mtime_value: float | None,
    now: float,
) -> dict[str, Any]:
    windows = dict(whitelist.get("windows") or {})
    refresh = _should_refresh_quota(previous, windows, transcript_mtime_value)
    observed = now if refresh else (_to_float((previous or {}).get("quota_observed_at")) or None)
    payload = {
        "source": SOURCE,
        "schema_version": SCHEMA_VERSION,
        "session_key": session_key,
        "claude_code_version": str(whitelist.get("claude_code_version") or (previous or {}).get("claude_code_version") or ""),
        "bridge_seen_at": now,
        "quota_observed_at": observed,
        "last_window_fingerprint": window_fingerprint(windows),
    }
    if transcript_mtime_value is not None:
        payload["last_transcript_mtime"] = transcript_mtime_value
    elif previous and _to_float(previous.get("last_transcript_mtime")) is not None:
        payload["last_transcript_mtime"] = previous.get("last_transcript_mtime")
    for key in WINDOW_KEYS:
        if key in windows:
            payload[key] = windows[key]
    if previous:
        merged = dict(previous)
        merged.update(payload)
        if not windows:
            for key in WINDOW_KEYS:
                merged.pop(key, None)
        else:
            for key in WINDOW_KEYS:
                if key not in windows:
                    merged.pop(key, None)
        payload = merged
    return sanitize_cache(payload)


def write_session_cache(session_key: str, payload: dict[str, Any]) -> None:
    path = sessions_dir() / f"{session_key}.json"
    atomic_write_json(path, sanitize_cache(payload))


def ingest_statusline(raw_text: str, now: float | None = None) -> dict[str, Any] | None:
    current = time.time() if now is None else float(now)
    try:
        data = json.loads(raw_text) if str(raw_text).strip() else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    session_id = str(data.get("session_id") or data.get("sessionId") or "")
    transcript_path = ""
    for key in ("transcript_path", "transcriptPath"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            transcript_path = value
            break
        if isinstance(data.get("session"), dict):
            nested = data["session"].get(key)
            if isinstance(nested, str) and nested.strip():
                transcript_path = nested
                break
    mtime = transcript_mtime(transcript_path)
    session_key = opaque_session_key(session_id, transcript_path)
    whitelist = extract_whitelist(data)
    previous = load_session(session_key)
    payload = build_session_cache(
        session_key=session_key,
        whitelist=whitelist,
        previous=previous,
        transcript_mtime_value=mtime,
        now=current,
    )
    try:
        write_session_cache(session_key, payload)
    except OSError:
        return payload
    return payload


def format_status_line(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    parts = []
    five = payload.get("five_hour") if isinstance(payload.get("five_hour"), dict) else None
    week = payload.get("seven_day") if isinstance(payload.get("seven_day"), dict) else None
    if five and five.get("remaining_percent") is not None:
        parts.append(f"5h {float(five['remaining_percent']):.0f}%")
    if week and week.get("remaining_percent") is not None:
        parts.append(f"7d {float(week['remaining_percent']):.0f}%")
    return " · ".join(parts)


def load_original_command() -> str:
    data = _load_json(integration_path()) or {}
    original = data.get("original_statusline")
    if isinstance(original, dict):
        command = original.get("command")
        if isinstance(command, str) and command.strip():
            return command.strip()
    command = data.get("original_command")
    if isinstance(command, str) and command.strip():
        return command.strip()
    return ""


def _forward_original(command: str, raw: bytes) -> int:
    try:
        completed = subprocess.run(
            command,
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
            check=False,
        )
    except OSError:
        return 0
    try:
        sys.stdout.buffer.write(completed.stdout or b"")
        sys.stderr.buffer.write(completed.stderr or b"")
    except OSError:
        pass
    return int(completed.returncode or 0)


def main(argv: list[str] | None = None) -> int:
    raw = b""
    try:
        raw = sys.stdin.buffer.read()
    except OSError:
        raw = b""
    payload = None
    try:
        text = raw.decode("utf-8", "replace")
        payload = ingest_statusline(text)
    except Exception:
        payload = None
    command = ""
    try:
        command = load_original_command()
    except Exception:
        command = ""
    if command:
        try:
            return _forward_original(command, raw)
        except Exception:
            return 0
    line = format_status_line(payload)
    if line:
        try:
            sys.stdout.write(line)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
