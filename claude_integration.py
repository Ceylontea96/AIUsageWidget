"""Opt-in Claude Code statusLine integration. Never touches credentials."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from claude_bridge import (
    MIN_CLAUDE_VERSION,
    atomic_write_json,
    claude_dir,
    integration_path,
    parse_claude_version,
    sessions_dir,
    version_supported,
)

WRAPPER_NAME = "statusline_bridge.py"


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
    if not path.is_file():
        return {}
    data = _read_json(path)
    return data if data is not None else {}


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
    """none | installed | missing | conflict | absent."""
    meta = load_integration()
    current = current_statusline()
    if not meta.get("installed"):
        return "absent" if current in (None, {}) else "unmanaged"
    expected = meta.get("fingerprint")
    actual = statusline_fingerprint(current) if current not in (None, {}) else ""
    if not current:
        return "missing"
    if expected and actual != expected:
        return "conflict"
    return "installed"


def resolve_claude_executable() -> Path | None:
    found = shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe")
    if found:
        return Path(found)
    return None


def claude_version_text(executable: Path | None = None) -> str:
    exe = executable or resolve_claude_executable()
    if exe is None:
        return ""
    try:
        completed = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return ((completed.stdout or "") + " " + (completed.stderr or "")).strip()


def claude_ready() -> tuple[bool, str]:
    exe = resolve_claude_executable()
    if exe is None:
        return False, "PATH에서 claude를 찾지 못했습니다."
    text = claude_version_text(exe)
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
    dest.write_bytes(source.read_bytes())


def _write_user_settings(settings: dict[str, Any]) -> None:
    path = user_claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, settings)


def install_statusline() -> dict[str, Any]:
    ready, detail = claude_ready()
    if not ready:
        raise RuntimeError(detail)
    settings = read_user_settings()
    original = settings.get("statusLine")
    had_original = original not in (None, {})
    original_object = dict(original) if isinstance(original, dict) else original
    _copy_bridge_script()
    installed_object = installed_statusline_object()
    settings["statusLine"] = installed_object
    _write_user_settings(settings)
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
    atomic_write_json(integration_path(), meta)
    return meta


def uninstall_statusline() -> str:
    """Returns restored | removed | conflict | absent."""
    state = conflict_state()
    if state == "conflict":
        return "conflict"
    meta = load_integration()
    if not meta.get("installed") and state in {"absent", "unmanaged"}:
        _cleanup_files()
        return "absent"
    settings = read_user_settings()
    if state == "missing" or not settings.get("statusLine"):
        settings.pop("statusLine", None)
        if settings:
            _write_user_settings(settings)
        elif user_claude_settings_path().is_file() and settings == {}:
            try:
                user_claude_settings_path().unlink()
            except OSError:
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
        path = user_claude_settings_path()
        try:
            if path.is_file() and _read_json(path) == {"statusLine": settings.get("statusLine")}:
                path.unlink()
            else:
                _write_user_settings(settings)
        except OSError:
            _write_user_settings(settings)
    _cleanup_files()
    return "removed"


def _cleanup_files() -> None:
    folder = claude_dir()
    for path in (wrapper_script_path(), integration_path(), folder / "session.salt"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    sessions = sessions_dir()
    if sessions.is_dir():
        for item in sessions.glob("*"):
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
    ready, detail = claude_ready()
    state = conflict_state()
    if not ready:
        return detail
    labels = {
        "installed": "연동됨 · 대화형 Claude Code 사용량",
        "missing": "연동 설정이 사라졌습니다",
        "conflict": "사용자가 statusLine을 변경함 · 자동 원복 안 함",
        "unmanaged": "기존 statusLine 있음 · 연동 시 tee wrapper 사용",
        "absent": "연동 안 됨",
    }
    return labels.get(state, state)
