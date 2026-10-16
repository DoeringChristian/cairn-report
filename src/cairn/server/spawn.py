"""The UI as a child process beside the server: start, forward its output, stop.

The server never imports the UI: it runs `python -m cairn ui`, and the child exits when its parent is gone
(`PARENT_ENV` names the parent's pid; the child watches it).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

from ..hosts import LOCAL_HOSTS  # noqa: F401  (app imports it from here)

PARENT_ENV = "CAIRN_UI_PARENT"  # set for a UI the server spawned: the pid it belongs to


def start_ui(host: str, port: int, ui_port: int, owner_token: str, no_auth: bool):
    """`cairn ui` as a child process beside this server (so page work never loads the server).

    The child reaches the store at `host:port`; the owner token goes through its environment only (never its
    command line). Returns (process, the lines it printed at start: its address and login link), or (None, None) if it did not start.
    """
    where = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    url = f"http://[{where}]:{port}" if ":" in where else f"http://{where}:{port}"
    command = [sys.executable, "-m", "cairn", "ui", "--remote", url, "--host", host, "--port", str(ui_port), "--no-browser"]
    if no_auth:
        command.append("--no-auth")
    env = dict(os.environ)
    env.pop("CAIRN_TOKEN", None)
    if not no_auth:
        env["CAIRN_TOKEN"] = owner_token
    env[PARENT_ENV] = str(os.getpid())  # the UI exits when this process is gone, even after a SIGKILL
    child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, text=True)
    lines = []  # its startup lines, up to the login link (or the no-auth notice), so the server prints them in order
    for line in child.stdout:
        lines.append(line.rstrip())
        if line.startswith(("Log in:", "Authentication is off")):
            return child, lines
    child.wait()
    return None, None


def forward(child) -> None:
    """Pass the UI's later output on, and say so if it dies while the store keeps running."""
    for line in child.stdout:
        print(line.rstrip(), flush=True)
    code = child.wait()
    if code not in (0, -signal.SIGTERM, -signal.SIGINT):
        print(f"warning: the UI stopped (exit code {code}); the server keeps running", file=sys.stderr, flush=True)


def stop(child) -> None:
    child.terminate()
    try:
        child.wait(5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
