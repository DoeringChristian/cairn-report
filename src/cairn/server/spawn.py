"""The UI as a child process beside the store server: start, forward its output, stop.

The server never imports the UI: it runs `python -m cairn ui`, and the child exits when its parent is gone
(`PARENT_ENV` names the parent's pid; the child watches it).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
PARENT_ENV = "CAIRN_UI_PARENT"  # set for a UI the store server spawned: the pid it belongs to


def start_ui(host: str, port: int, ui_port: int, owner_token: str, no_auth: bool):
    """`cairn ui` as a child process beside this store (so page work never loads the store server).

    The child reaches the store at `host:port`; the owner token goes through its environment only (never its
    command line). Returns (process, the line it printed with its address), or (None, None) if it did not start.
    """
    where = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    url = f"http://[{where}]:{port}" if ":" in where else f"http://{where}:{port}"
    command = [sys.executable, "-m", "cairn", "ui", "--store", url, "--host", host, "--port", str(ui_port), "--allow-http", "--no-browser"]
    if no_auth:
        command.append("--no-auth")
    if host in LOCAL_HOSTS:
        command.append("--insecure-cookies")  # plain HTTP on this machine
    env = dict(os.environ)
    env.pop("CAIRN_STORE_TOKEN", None)
    if not no_auth:
        env["CAIRN_STORE_TOKEN"] = owner_token
    env[PARENT_ENV] = str(os.getpid())  # the UI exits when this process is gone, even after a SIGKILL
    child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, text=True)
    line = child.stdout.readline().rstrip()
    if not line:
        child.wait()
        return None, None
    return child, line


def forward(child) -> None:
    """Pass the UI's later output on, and say so if it dies while the store keeps running."""
    for line in child.stdout:
        print(line.rstrip(), flush=True)
    code = child.wait()
    if code not in (0, -signal.SIGTERM, -signal.SIGINT):
        print(f"warning: the UI stopped (exit code {code}); the store keeps running", file=sys.stderr, flush=True)


def stop(child) -> None:
    child.terminate()
    try:
        child.wait(5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
