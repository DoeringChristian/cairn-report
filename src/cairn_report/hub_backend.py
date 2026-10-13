"""The hub as a logging backend: a run's files reach the hub while the run executes.

Enable with `[logger] backends = ["local", "hub"]` (or `["hub"]`) and a `[hub]`
section (url, project, token). The run directory stays where files are written
first; a background thread sends what changed to the hub every `[hub] interval`
seconds (default 30), shortly after each newly logged file, and once more when
the process exits, so the final run.json is included. A hub that cannot be
reached never stops the run: the thread warns and retries, and `cairn-report
push runs` can send anything left over later.
"""

from __future__ import annotations

import atexit
import json
import sys
import threading
import time
from pathlib import Path

_open: dict[Path, "HubBackend"] = {}
_lock = threading.RLock()
SETTLE = 2.0  # seconds to wait after a logged file, so a burst of files goes out as one push


class HubBackend:
    name = "hub"

    @classmethod
    def attach(cls, root: Path, project: Path) -> "HubBackend":
        """The backend of this run directory, shared by every RunLogger created for it."""
        with _lock:
            for other in list(_open.values()):  # a new trial in the same process: finish completed ones
                if other.root != root and other.terminal():
                    other.finish()
            if root not in _open:
                _open[root] = cls(root, project)
            return _open[root]

    def __init__(self, root: Path, project: Path):
        from .config import load_config
        from .push import RunPusher, connect, project_name

        config = load_config(project)
        outputs = config.outputs_dir.resolve()
        if outputs != root and outputs not in root.parents:
            raise ValueError(f"the hub backend sends runs under {outputs}; {root} is outside it")
        self.root = root
        self.pusher = RunPusher(connect(config), config, project_name(config), folder=root)
        self.interval = float(config.hub.get("interval", 30))
        self.wake, self.stopped = threading.Event(), threading.Event()
        self.failing = False
        self.thread = threading.Thread(target=self._loop, name=f"cairn-hub-sync:{root.name}", daemon=True)
        self.thread.start()
        atexit.register(self.finish)

    # -------------------------------------------------------------- logging

    def scalar(self, event: dict) -> None:
        pass  # metrics.jsonl is written by the local backend and sent with the next sync

    def file(self, relative: str, data: bytes, entry: dict) -> None:
        self.wake.set()

    # ------------------------------------------------------------- syncing

    def _sync(self) -> bool:
        try:
            self.pusher.once()
            if self.failing:
                print(f"hub sync of {self.root} works again", file=sys.stderr, flush=True)
            self.failing = False
            return True
        except Exception as error:  # the run directory has everything; never stop the run
            if not self.failing:
                print(f"warning: hub sync of {self.root} failed ({error}); retrying", file=sys.stderr, flush=True)
            self.failing = True
            return False

    def _loop(self) -> None:
        while not self.stopped.is_set():
            woken = self.wake.wait(self.interval)
            if self.stopped.is_set():
                break
            if woken:
                time.sleep(SETTLE)
            self.wake.clear()
            self._sync()

    def terminal(self) -> bool:
        try:
            return json.loads((self.root / "run.json").read_text()).get("status") in ("completed", "failed", "interrupted")
        except (OSError, ValueError):
            return False

    def finish(self) -> None:
        """Stop the background thread and send the final state (a few attempts)."""
        if self.stopped.is_set():
            return
        self.stopped.set()
        self.wake.set()
        self.thread.join(timeout=60)
        for attempt in range(3):
            if self._sync():
                break
            time.sleep(2 * (attempt + 1))
        with _lock:
            _open.pop(self.root, None)
