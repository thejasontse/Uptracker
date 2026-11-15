"""Dataset retrieval executors.

Every dataset method runs a shell command body locally on the server host:
``bash`` / ``cmd`` / ``powershell``. Protocol probes are ordinary commands in
this model — ``ping -c1 host``, ``nc -z host 443``, ``curl -s https://…`` —
so no dedicated executors are needed. Credentials are injected into the child
environment (``SM_USER``, ``SM_SECRET``) and never written to disk or logs.

The executor returns the raw stdout, which the parser then mines for metric
values.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

# Methods that run a shell command body.
SHELL_METHODS = {"bash", "cmd", "powershell"}
ALL_METHODS = SHELL_METHODS

_SHELL_BIN = {
    "bash": ["bash", "-lc"],
    "cmd": ["cmd", "/c"],
    "powershell": ["powershell", "-NoProfile", "-Command"],
}


@dataclass
class RunResult:
    ok: bool
    raw: str
    error: str | None = None
    exit_code: int | None = None


def _env_with_credentials(secret: str | None, username: str | None) -> dict[str, str]:
    env = dict(os.environ)
    if username:
        env["SM_USER"] = username
    if secret:
        env["SM_SECRET"] = secret
    return env


def run_shell(method: str, script: str, timeout_s: int, secret: str | None = None,
              username: str | None = None) -> RunResult:
    argv = _SHELL_BIN.get(method)
    if not argv:
        return RunResult(False, "", f"unknown shell method: {method}")
    try:
        proc = subprocess.run(
            argv + [script],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=_env_with_credentials(secret, username),
        )
    except subprocess.TimeoutExpired:
        return RunResult(False, "", f"timeout after {timeout_s}s")
    except FileNotFoundError:
        return RunResult(False, "", f"interpreter for '{method}' not found on host")
    out = proc.stdout if proc.stdout else proc.stderr
    return RunResult(proc.returncode == 0, out, None if proc.returncode == 0 else f"exit {proc.returncode}", proc.returncode)


def run_dataset(method: str, script: str, timeout_s: int, secret: str | None = None,
                username: str | None = None) -> RunResult:
    """Dispatch to the right executor for *method*."""
    if method in SHELL_METHODS:
        return run_shell(method, script, timeout_s, secret, username)
    return RunResult(False, "", f"unsupported method: {method}")
