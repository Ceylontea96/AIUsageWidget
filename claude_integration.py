"""Opt-in Claude Code statusLine integration. Never touches credentials."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from claude_bridge import (
    MIN_CLAUDE_VERSION,
    atomic_write_json,
    claude_dir,
    command_references_wrapper,
    integration_path,
    parse_claude_version,
    sessions_dir,
    version_supported,
)

WRAPPER_NAME = "statusline_bridge.py"
VERSION_TTL = 3600.0
VERSION_FAILURE_TTL = 30.0
_VERSION_CACHE = {}
_VERSION_PENDING = set()
_VERSION_LOCK = threading.Lock()


def user_claude_settings_path() -> Path:
    override = os.environ.get("AIUSAGE_CLAUDE_SETTINGS")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "settings.json"


def wrapper_script_path() -> Path:
    return claude_dir() / WRAPPER_NAME


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _python_executable() -> Path:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        alt = exe.with_name("python.exe")
        if alt.is_file():
            return alt
    return exe


def wrapper_command() -> str:
    python = _python_executable()
    script = wrapper_script_path()
    return f'"{python}" -B "{script}"'


def statusline_fingerprint(statusline: Any) -> str:
    encoded = json.dumps(statusline, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def installed_statusline_object() -> dict[str, Any]:
    return {
        "type": "command",
        "command": wrapper_command(),
        "refreshInterval": 5,
    }


def read_user_settings() -> dict[str, Any]:
    path = user_claude_settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Settings must be an object")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError) as exc:
        raise RuntimeError(
            "Claude settings.json을 읽을 수 없어 연동 설정을 변경하지 않았습니다. "
            "파일을 확인한 뒤 다시 시도하세요."
        ) from exc


def current_statusline() -> Any:
    settings = read_user_settings()
    return settings.get("statusLine")


def load_integration() -> dict[str, Any]:
    data = _read_json(integration_path())
    return data if data else {}


def is_installed() -> bool:
    meta = load_integration()
    return bool(meta.get("installed")) and wrapper_script_path().is_file()


def conflict_state() -> str:
    """installed | missing | conflict | absent | unmanaged | unreadable."""
    meta = load_integration()
    try:
        current = current_statusline()
    except RuntimeError:
        return "unreadable"
    if not meta.get("installed"):
        if owned_statusline(current, meta):
            return "recovery"
        return "absent" if current in (None, {}) else "unmanaged"
    expected = meta.get("fingerprint")
    actual = statusline_fingerprint(current) if current not in (None, {}) else ""
    if not current:
        return "missing"
    if not expected or actual != expected:
        return "conflict"
    return "installed" if wrapper_script_path().is_file() else "recovery"


def owned_statusline(statusline: Any, meta: dict[str, Any]) -> bool:
    if not isinstance(statusline, dict) or statusline.get("type") != "command":
        return False
    command = statusline.get("command")
    return command_references_wrapper(command) or bool(
        meta.get("fingerprint") == statusline_fingerprint(statusline)
        and command == meta.get("command") and command_references_wrapper(meta.get("command"))
    )


def valid_backup(meta: dict[str, Any]) -> bool:
    if type(meta.get("schema_version")) is not int or meta["schema_version"] != 1:
        return False
    if type(meta.get("had_original")) is not bool:
        return False
    original = meta.get("original_statusline")
    if not meta["had_original"]:
        return original is None
    return (isinstance(original, dict) and original.get("type") == "command"
            and isinstance(original.get("command"), str) and bool(original["command"].strip())
            and not command_references_wrapper(original["command"]))


def resolve_claude_executable() -> Path | None:
    found = shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe")
    if found:
        return Path(found)
    # Explorer may still have the PATH from before CLI installation. Store
    # Desktop installations also redirect Roaming into their package cache.
    candidates = [Path.home() / ".local" / "bin" / "claude.exe"]
    roaming = os.environ.get("APPDATA")
    local = os.environ.get("LOCALAPPDATA")
    roots = []
    if roaming:
        candidates.append(Path(roaming) / "npm" / "claude.cmd")
        roots.append(Path(roaming) / "Claude" / "claude-code")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if local:
        roots.extend(
            package / "LocalCache" / "Roaming" / "Claude" / "claude-code"
            for package in (Path(local) / "Packages").glob("Claude_*")
        )
    builds = [exe for root in roots for exe in root.glob("*/claude.exe") if exe.is_file()]
    return max(builds, key=lambda exe: parse_claude_version(exe.parent.name) or (0, 0, 0), default=None)


def start_claude_login() -> None:
    exe = resolve_claude_executable()
    if exe is None:
        raise RuntimeError("Claude Code를 찾지 못했습니다. 설치 후 다시 시도하세요.")
    # The official CLI opens the account authorization page in the browser.
    subprocess.Popen(
        [str(exe), "auth", "login"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def invalidate_version_cache() -> None:
    with _VERSION_LOCK:
        _VERSION_CACHE.clear()


def claude_version_text(executable: Path | None = None, *, force=False, background=False) -> str:
    exe = executable or resolve_claude_executable()
    if exe is None:
        return ""
    try:
        stat = exe.stat()
        key = (str(exe.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (str(exe), None, None)
    now = time.monotonic()
    with _VERSION_LOCK:
        cached = _VERSION_CACHE.get(key)
        if not force and cached and now < cached[0]:
            return cached[1]
        if background:
            if key not in _VERSION_PENDING:
                _VERSION_PENDING.add(key)
                threading.Thread(target=_cache_version, args=(exe, key), daemon=True).start()
            return ""
    return _cache_version(exe, key)


def _cache_version(exe, key) -> str:
    text = ""
    try:
        completed = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode == 0:
            text = ((completed.stdout or "") + " " + (completed.stderr or "")).strip()
            if parse_claude_version(text) is None:
                text = ""
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        with _VERSION_LOCK:
            ttl = VERSION_TTL if text else VERSION_FAILURE_TTL
            _VERSION_CACHE[key] = (time.monotonic() + ttl, text)
            _VERSION_PENDING.discard(key)
    return text


def claude_ready(*, executable=None, force=False, background=False) -> tuple[bool, str]:
    """Version sanity only; actual quota schema is checked at ingestion."""
    exe = executable or resolve_claude_executable()
    if exe is None:
        return False, "PATH에서 claude를 찾지 못했습니다."
    text = claude_version_text(exe, force=force, background=background)
    if background and not text:
        return False, "Claude Code 버전 확인 중 · 사용량 기능은 응답 수신 후 확인합니다."
    version = parse_claude_version(text)
    if not version_supported(version):
        needed = ".".join(str(part) for part in MIN_CLAUDE_VERSION)
        found = ".".join(str(part) for part in version) if version else "알 수 없음"
        return False, f"Claude Code {needed} 이상이 필요합니다. 현재 {found}."
    return True, f"{exe} ({'.'.join(str(part) for part in version)})"


def _copy_bridge_script() -> None:
    source = Path(__file__).with_name("claude_bridge.py")
    dest = wrapper_script_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(source.read_bytes())
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _write_user_settings(settings: dict[str, Any]) -> None:
    path = user_claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, settings)


def install_statusline() -> dict[str, Any]:
    # Parse settings before any mutation, including wrapper/backup changes.
    settings = read_user_settings()
    previous = load_integration()
    current = settings.get("statusLine")
    owned = owned_statusline(current, previous)
    if owned and not valid_backup(previous):
        raise RuntimeError("Claude 연동 복구 정보가 없거나 손상되었습니다. 원본 statusLine을 확인해 복구한 뒤 다시 연동하세요.")
    if (previous.get("installed") and current not in (None, {})
            and (not owned or previous.get("fingerprint") != statusline_fingerprint(current))):
        raise RuntimeError("사용자가 statusLine을 변경했습니다. 기존 설정과 원본 백업을 보존했습니다.")
    ready, detail = claude_ready(force=True)
    if not ready:
        raise RuntimeError(detail)
    if (owned or previous.get("installed")) and valid_backup(previous):
        original = previous.get("original_statusline")
    else:
        original = current
    had_original = original not in (None, {})
    if had_original and not valid_backup({"schema_version": 1, "had_original": True, "original_statusline": original}):
        raise RuntimeError("기존 statusLine 형식을 확인할 수 없어 설정을 변경하지 않았습니다.")
    original_object = dict(original) if isinstance(original, dict) else original
    _copy_bridge_script()
    installed_object = installed_statusline_object()
    settings["statusLine"] = installed_object
    meta = {
        "schema_version": 1,
        "installed": True,
        "had_original": had_original,
        "original_statusline": original_object if had_original else None,
        "fingerprint": statusline_fingerprint(installed_object),
        "command": installed_object["command"],
        "claude_executable": str(resolve_claude_executable() or ""),
        "claude_version": claude_version_text(),
    }
    # Persist the original statusLine before the atomic settings replacement.
    # Even an interrupted/failed settings write must leave recovery metadata.
    atomic_write_json(integration_path(), meta)
    try:
        _write_user_settings(settings)
    except OSError:
        meta["installed"] = False
        atomic_write_json(integration_path(), meta)
        raise
    return meta


def uninstall_statusline() -> str:
    """Returns restored | removed | conflict | absent."""
    state = conflict_state()
    if state in {"conflict", "unreadable"}:
        return "conflict"
    meta = load_integration()
    if owned_statusline(current_statusline(), meta) and not valid_backup(meta):
        return "conflict"
    if not meta.get("installed") and state in {"absent", "unmanaged"}:
        _cleanup_files()
        return "absent"
    settings = read_user_settings()
    if state == "missing" or not settings.get("statusLine"):
        settings.pop("statusLine", None)
        if settings:
            _write_user_settings(settings)
        elif user_claude_settings_path().is_file():
            _write_user_settings(settings)
        _cleanup_files()
        return "removed"
    if meta.get("had_original") and isinstance(meta.get("original_statusline"), dict):
        settings["statusLine"] = meta["original_statusline"]
        _write_user_settings(settings)
        _cleanup_files()
        return "restored"
    settings.pop("statusLine", None)
    if settings:
        _write_user_settings(settings)
    else:
        _write_user_settings(settings)
    _cleanup_files()
    return "removed"


def _cleanup_files() -> None:
    import re
    folder = claude_dir()
    owned = [wrapper_script_path(), integration_path(), folder / "session.salt", folder / "bridge.log"]
    owned.extend(path for path in folder.glob(".*.tmp")
                 if re.fullmatch(r"\.(integration\.json|statusline_bridge\.py)\.\d+\.tmp", path.name))
    for path in owned:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    sessions = sessions_dir()
    if sessions.is_dir():
        for item in sessions.glob("*"):
            if not re.fullmatch(r"[0-9a-f]{64}\.json|\.[0-9a-f]{64}\.json\.\d+\.tmp", item.name):
                continue
            try:
                item.unlink()
            except OSError:
                pass
        try:
            sessions.rmdir()
        except OSError:
            pass


def ensure_bridge_copy() -> None:
    if not is_installed():
        return
    try:
        _copy_bridge_script()
    except OSError:
        pass


def integration_label() -> str:
    ready, detail = claude_ready(background=True)
    state = conflict_state()
    if not ready:
        return detail
    labels = {
        "installed": "연동 설정됨 · 사용량 기능은 응답 수신 후 확인",
        "recovery": "연동 파일 복구 필요 · 원본 백업 확인 후 재연동",
        "missing": "연동 설정이 사라졌습니다",
        "conflict": "사용자가 statusLine을 변경함 · 자동 원복 안 함",
        "unmanaged": "기존 statusLine 있음 · 연동 시 tee wrapper 사용",
        "absent": "연동 안 됨",
        "unreadable": "Claude settings.json을 읽을 수 없습니다 · 파일 확인 필요",
    }
    return labels.get(state, state)
