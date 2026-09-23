"""Ask the installed Codex for plan usage over its own app-server protocol.

The widget never reads Codex's login tokens. Codex signs in by itself and
answers `account/rateLimits/read`, the call its own apps use. One app-server
is kept alive for the life of the widget because starting Codex costs one to
three seconds while a warm read costs well under one.

Only `initialize`, `initialized` and `account/rateLimits/read` are ever sent.
The protocol also has `account/rateLimitResetCredit/consume`, which spends a
reset credit; nothing here can send it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

START_TIMEOUT = 20.0
READ_TIMEOUT = 12.0
READ_METHOD = "account/rateLimits/read"
# The only requests this client can send.
ALLOWED_METHODS = frozenset({"initialize", READ_METHOD})

# npm installs a script shim that starts node, which starts the native binary.
# Running that binary directly keeps the server a single process that ends
# with the widget.
_NATIVE_PATTERNS = (
    "node_modules/@openai/codex/node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe",
    "node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe",
)


class CodexAppServerError(RuntimeError):
    """A read that did not produce rate limits. The message is user-facing."""


def resolve_codex_executable() -> Path | None:
    found = shutil.which("codex")
    if not found:
        return None
    path = Path(found)
    if path.suffix.lower() == ".exe":
        return path
    for pattern in _NATIVE_PATTERNS:
        hits = sorted(path.parent.glob(pattern))
        if hits:
            return hits[0]
    return path


def _kill_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        # A shim may sit between us and codex.exe; take the whole tree down.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _user_message(error: Any) -> str:
    text = str((error or {}).get("message") if isinstance(error, dict) else error or "").lower()
    if any(word in text for word in ("auth", "login", "logged", "unauthorized", "401", "token")):
        return "GPT 사용량 조회를 위해 Codex CLI에 로그인한 뒤 새로고침하세요."
    return "Codex에서 사용량을 받지 못했습니다. 자동 재시도합니다."


class CodexAppServer:
    """One lazily started `codex app-server`, one request at a time."""

    def __init__(
        self,
        executable: Path | str | None = None,
        *,
        client_version: str = "",
        start_timeout: float = START_TIMEOUT,
        read_timeout: float = READ_TIMEOUT,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self._executable = Path(executable) if executable else None
        self._client_version = client_version or "unknown"
        self.start_timeout = start_timeout
        self.read_timeout = read_timeout
        self._popen = popen
        self._lock = threading.Lock()
        self._state = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._pending: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        self.starts = 0

    # -- public -----------------------------------------------------------
    def read_rate_limits(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_started()
            result = self._request(READ_METHOD, None, self.read_timeout)
        if not isinstance(result, dict):
            raise CodexAppServerError("Codex에서 사용량을 받지 못했습니다. 자동 재시도합니다.")
        return result

    def restart(self) -> None:
        """Drop the server; the next read starts a fresh one."""
        self._stop()

    def close(self) -> None:
        self._stop(graceful=True)

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    # -- internals --------------------------------------------------------
    def _ensure_started(self) -> None:
        if self.running:
            return
        self._stop()
        executable = self._executable or resolve_codex_executable()
        if executable is None:
            raise CodexAppServerError("Codex CLI를 찾지 못했습니다. 설치하고 로그인한 뒤 새로고침하세요.")
        try:
            process = self._popen(
                [str(executable), "app-server"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise CodexAppServerError("Codex CLI를 실행하지 못했습니다. 설치 상태를 확인하세요.") from exc
        with self._state:
            self._process = process
            self._pending = {}
        self.starts += 1
        threading.Thread(target=self._read_loop, args=(process,), daemon=True,
                         name="codex-app-server-reader").start()
        try:
            self._request(
                "initialize",
                {"clientInfo": {"name": "ai_usage_widget", "title": "AI Usage",
                                "version": self._client_version}},
                self.start_timeout,
            )
            self._send({"method": "initialized"})
        except CodexAppServerError:
            self._stop()
            raise

    def _send(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if "id" in message and method not in ALLOWED_METHODS:
            raise CodexAppServerError("허용되지 않은 Codex 요청입니다.")
        process = self._process
        if process is None or process.stdin is None:
            raise CodexAppServerError("Codex에서 사용량을 받지 못했습니다. 자동 재시도합니다.")
        try:
            process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            self._stop()
            raise CodexAppServerError("Codex 연결이 끊겼습니다. 자동 재시도합니다.") from exc

    def _request(self, method: str, params: Any, timeout: float) -> Any:
        waiter = {"event": threading.Event(), "message": None}
        with self._state:
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = waiter
        self._send({"id": request_id, "method": method, "params": params})
        if not waiter["event"].wait(timeout):
            # A stuck server is not reused: the next read starts a fresh one.
            self._stop()
            raise CodexAppServerError("Codex 응답이 늦어 다시 시도합니다.")
        with self._state:
            self._pending.pop(request_id, None)
        message = waiter["message"]
        if not isinstance(message, dict):
            # Woken by end of output: the server is gone even if Windows has
            # not reaped it yet, so never write to it again.
            self._stop()
            raise CodexAppServerError("Codex 연결이 끊겼습니다. 자동 재시도합니다.")
        if "error" in message:
            raise CodexAppServerError(_user_message(message.get("error")))
        return message.get("result")

    def _read_loop(self, process: subprocess.Popen) -> None:
        stream = process.stdout
        try:
            for raw in iter(stream.readline, b""):
                try:
                    message = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(message, dict) or "id" not in message or "method" in message:
                    # Notifications and server-side requests are not answers.
                    continue
                with self._state:
                    waiter = self._pending.get(message.get("id"))
                if waiter is not None:
                    waiter["message"] = message
                    waiter["event"].set()
        except (OSError, ValueError):
            pass
        finally:
            # EOF: wake every waiter so nobody blocks on a dead server.
            with self._state:
                if self._process is process:
                    for waiter in self._pending.values():
                        waiter["event"].set()

    def _stop(self, graceful: bool = False) -> None:
        with self._state:
            process, self._process = self._process, None
            pending, self._pending = self._pending, {}
        for waiter in pending.values():
            waiter["event"].set()
        if process is None:
            return
        if graceful and process.poll() is None:
            try:
                process.stdin.close()   # the server exits on end of input
                process.wait(timeout=3)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        _kill_tree(process)
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
