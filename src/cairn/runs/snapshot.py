"""Source snapshots: a Hydra callback that records bounded source and runtime provenance per job.

Register it in a project's configs/config.yaml (needs the `hydra` extra):

    hydra:
      callbacks:
        source_snapshot:
          _target_: cairn.runs.snapshot.SourceSnapshotCallback

Hydra reports exceptions raised in callbacks as warnings and keeps running the
job, so the entry point must call `require_snapshot(output_dir)` before any
experiment code (`cairn.runs.hydra.run_experiment` does): a run without a
complete snapshot is a failed run.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from hydra.core.hydra_config import HydraConfig
from hydra.experimental.callback import Callback
from hydra.utils import get_original_cwd

from ..client.code import (DENIED_PARTS, DENIED_PATTERNS, SAFE_ENVIRONMENT, SOURCE_NAMES, SOURCE_SUFFIXES,  # noqa: F401
                           _denied, _git, _select, _source_type, _working_files, create_snapshot, packages)


def capture_environment(root: Path, destination: Path) -> None:
    target = destination / "environment"
    target.mkdir(parents=True, exist_ok=True)
    facts = {
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "selected_environment": {
            key: os.environ[key] for key in SAFE_ENVIRONMENT if key in os.environ
        },
    }
    (target / "runtime.json").write_text(
        json.dumps(facts, indent=2) + "\n", encoding="utf-8"
    )
    # Installed distributions in `pip freeze` format, read from package metadata so pip is not required.
    (target / "pip-freeze.txt").write_text("\n".join(packages()) + "\n", encoding="utf-8")
    lock = root / "pixi.lock"
    if lock.is_file():
        shutil.copy2(lock, target / "pixi.lock")
    try:
        (target / "git-status.txt").write_bytes(
            _git(root, "status", "--short", "--branch")
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass


def require_snapshot(output_dir: Path) -> Path:
    """Fail the job unless the callback wrote a complete snapshot (call from the entry point)."""
    manifest = Path(output_dir) / "provenance" / "manifest.json"
    if not manifest.is_file():
        raise RuntimeError(
            f"no source snapshot at {manifest.parent}; see the SourceSnapshotCallback warning above"
        )
    return manifest


class SourceSnapshotCallback(Callback):
    def __init__(
        self,
        mode: str = "git",
        source_root: str | None = None,
        capture_runtime: bool = True,
        max_file_bytes: int = 2_000_000,
        max_total_bytes: int = 50_000_000,
    ) -> None:
        self.mode = mode
        self.source_root = source_root
        self.capture_runtime = capture_runtime
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes

    def on_job_start(self, config, **kwargs) -> None:
        root = Path(self.source_root or get_original_cwd()).resolve()
        output = Path(HydraConfig.get().runtime.output_dir).resolve()
        destination = output / "provenance"
        if destination.exists():
            raise RuntimeError(
                f"refusing to replace existing provenance: {destination}"
            )
        temporary = Path(tempfile.mkdtemp(prefix=".provenance-", dir=output))
        try:
            create_snapshot(
                root, temporary, self.mode, self.max_file_bytes, self.max_total_bytes
            )
            if self.capture_runtime:
                capture_environment(root, temporary)
            temporary.replace(destination)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
