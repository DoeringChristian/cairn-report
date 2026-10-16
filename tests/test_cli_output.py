"""What the command line prints stays short: at a terminal width of 80 no line runs past 100 columns, except a line
whose only long part is one path or URL (which is never broken)."""

import os
import re
import socket
import subprocess
import sys


def cairn(folder, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("CAIRN_")}
    env.update(COLUMNS="80", NO_COLOR="1", HOME=str(folder.parent))
    return subprocess.run(
        [sys.executable, "-m", "cairn", *args],
        cwd=folder,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]  # closed again: nothing answers there


LONG = re.compile(r"(?:[a-z]+://|[~/.]?[\w.-]*/)\S{20,}")  # a URL or a path


def too_long(line: str) -> bool:
    if len(line) <= 100:
        return False
    return len(LONG.sub("", line)) > 100 - 20 or len(LONG.findall(line)) != 1


def test_commands_print_short_lines(tmp_path):
    folder = tmp_path / "project"
    (folder / "data").mkdir(parents=True)
    (folder / "data" / "x.json").write_text('{"a": 1}')
    outputs = []
    for args in (
        ("init",),
        ("publish", "data", "datasets/x"),
        ("show", "datasets/x"),
        ("status",),
        ("versions", "datasets/x"),
        ("artifacts",),
        ("show", "nope"),
        ("remote", "set", f"http://127.0.0.1:{free_port()}/project"),
        ("publish", "data", "datasets/y"),
        ("status",),
        ("sync",),
    ):
        result = cairn(folder, *args)
        outputs.append((args, result.stdout + result.stderr))
    text = "\n".join(f"$ cairn {' '.join(args)}\n{out}" for args, out in outputs)
    assert "published datasets/x@v1 (" in text and "cairn: no artifact nope" in text, text
    assert "remote  http://127.0.0.1:" in text and "pending datasets/y (" in text, text
    assert "Traceback" not in text, text
    long = [line for line in text.splitlines() if too_long(line)]
    assert not long, "\n".join(long)
