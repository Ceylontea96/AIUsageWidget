"""Run a command-line tool with a time limit that also ends what it started.

npm installs a tool as a script shim (claude.cmd, codex.cmd): cmd.exe starts
node, and node inherits the output pipe. On a timeout subprocess.run() kills
only cmd.exe and then waits, with no limit, for that pipe to close, so a hung
node kept the caller waiting and stayed behind once the caller gave up.
"""
from __future__ import annotations

import subprocess

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# After the tree is ended the pipe closes at once; this only bounds a failure.
DRAIN_TIMEOUT = 2.0


def kill_tree(process: subprocess.Popen) -> None:
    """End `process` and every process it started, then reap it."""
    if process.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass


def run(args, *, timeout: float, input: bytes | None = None, cwd=None,
        stderr: bool = False) -> subprocess.CompletedProcess:
    """Like subprocess.run() with captured bytes, safe for a shim.

    Raises subprocess.TimeoutExpired once `timeout` has passed, after the
    whole process tree has been ended. stderr is discarded unless asked for.
    """
    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if stderr else subprocess.DEVNULL,
        cwd=cwd, creationflags=NO_WINDOW,
    )
    try:
        out, err = process.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(process)
        try:
            process.communicate(timeout=DRAIN_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        raise
    return subprocess.CompletedProcess(args, process.returncode, out or b"", err if stderr else None)
