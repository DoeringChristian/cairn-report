"""Run bookkeeping for Hydra projects: run.json, the per-name `latest` link, and study completion.

Every run directory gets `run.json`, its single completion record: `running` before experiment
code, then `completed` or `failed` (with the error). Only a completed single run advances
outputs/<run-name>/latest; a multirun study advances it once, from `StudyCompletion`, after every
trial completed, so parallel trials never race on it. Needs the `hydra` extra.

A project's experiments/run.py is then only:

    @hydra.main(version_base=None, config_path="../configs", config_name="config")
    def main(cfg: DictConfig) -> None:
        run_experiment(cfg, Experiment)

with the callbacks in configs/config.yaml:

    hydra:
      callbacks:
        source_snapshot:
          _target_: cairn.runs.snapshot.SourceSnapshotCallback
        study_completion:            # multirun only: completes the study, moves latest
          _target_: cairn.runs.hydra.StudyCompletion

`cairn finalize-study outputs/<run-name>/<timestamp>` completes a study whose launcher
died (for example a killed Slurm launcher) by the same rule, once every trial job has ended.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUN_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def code_revision(root: Path) -> str:
    """The git commit of the project at `root`, or "unversioned"."""
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unversioned"


def validate_run_name(name: str) -> str:
    if not RUN_NAME.fullmatch(name):
        raise ValueError(f"run.name {name!r} must be a short lowercase slug ({RUN_NAME.pattern})")
    return name


def write_status(run_dir: Path, record: dict) -> None:
    """Write run.json atomically, so readers never see a partial record."""
    temporary = Path(run_dir) / f".run.json.{uuid.uuid4().hex}"
    temporary.write_text(json.dumps(record, indent=2) + "\n")
    os.replace(temporary, Path(run_dir) / "run.json")


def update_latest(run_dir: Path) -> Path:
    """Point <run-name>/latest at a completed run directory with a relative symlink."""
    run_dir = Path(run_dir).resolve(strict=True)
    status = json.loads((run_dir / "run.json").read_text()).get("status")
    if status != "completed":
        raise RuntimeError(f"{run_dir} is {status!r}; only completed runs become latest")
    link = run_dir.parent / "latest"
    if link.exists() and not link.is_symlink():
        raise RuntimeError(f"{link} exists and is not a symlink; resolve the conflict by hand")
    temporary = run_dir.parent / f".latest-{uuid.uuid4().hex}"
    os.symlink(run_dir.name, temporary)
    if temporary.resolve() != run_dir:
        temporary.unlink()
        raise RuntimeError(f"temporary link does not resolve to {run_dir}")
    os.replace(temporary, link)
    return link


def run_experiment(cfg: Any, base: type | None = None, require_source_snapshot: bool = True) -> None:
    """The body of a project's Hydra entry point: record the run, check the source snapshot,
    instantiate `cfg.experiment` (an instance of `base`, when given), call `run(output_dir)`, and
    record how it ended. A completed single run becomes `latest`.

    Without a snapshot from `cairn.runs.snapshot.SourceSnapshotCallback` the run fails, unless
    `require_source_snapshot=False`."""
    from hydra.core.hydra_config import HydraConfig
    from hydra.types import RunMode
    from hydra.utils import get_original_cwd, instantiate

    from .snapshot import require_snapshot

    hydra_cfg = HydraConfig.get()
    output_dir = Path(hydra_cfg.runtime.output_dir).resolve()
    record = {
        "status": "running",
        "name": validate_run_name(str(cfg.run.name)),
        "description": str(cfg.run.get("description", "")),
        "started": now(),
        "code_revision": code_revision(Path(get_original_cwd())),
    }
    write_status(output_dir, record)
    try:
        if require_source_snapshot:
            require_snapshot(output_dir)  # Hydra reports callback errors only as warnings
        experiment = instantiate(cfg.experiment, _convert_="all")
        if base is not None and not isinstance(experiment, base):
            raise TypeError(f"{type(experiment).__name__} does not implement {base.__name__}")
        experiment.run(output_dir)
    except BaseException as error:
        write_status(output_dir, {**record, "status": "failed", "ended": now(),
                                  "error": f"{type(error).__name__}: {error}",
                                  "traceback": traceback.format_exc()})
        raise
    write_status(output_dir, {**record, "status": "completed", "ended": now()})
    if hydra_cfg.mode == RunMode.RUN:
        update_latest(output_dir)


def _trial_status(folder: Path) -> dict:
    status_file = folder / "run.json"
    return json.loads(status_file.read_text()) if status_file.is_file() else {}


def complete_study(study: Path, name: str, trials: list[str], started: str | None, revision: str,
                   **extra: Any) -> bool:
    """Write a study's run.json (completed only when every trial completed) and move latest."""
    records = {trial: _trial_status(study / trial) for trial in trials}
    rows = [{"trial": trial, "status": records[trial].get("status", "missing")} for trial in trials]
    complete = bool(rows) and all(row["status"] == "completed" for row in rows)
    study.mkdir(parents=True, exist_ok=True)
    write_status(study, {"status": "completed" if complete else "failed", "kind": "study",
                         "name": validate_run_name(name), "started": started, "ended": now(),
                         "code_revision": revision, "trials": rows, **extra})
    if complete:
        update_latest(study)
    return complete


def _numbered(study: Path) -> list[str]:
    return sorted((p.name for p in study.iterdir() if p.is_dir() and p.name.isdigit()), key=int) \
        if study.is_dir() else []


try:
    from hydra.experimental.callback import Callback
except ImportError:  # the module stays importable without Hydra; the callback needs it
    Callback = object  # type: ignore[assignment,misc]


class StudyCompletion(Callback):
    """Mark a multirun study completed only when every trial completed, then move latest."""

    def on_multirun_start(self, config: Any, **kwargs) -> None:
        # Hydra caches ${now:...}, so this resolves to the same directory the launcher uses.
        self.study = Path(config.hydra.sweep.dir).resolve()
        self.started = now()
        self.revision = code_revision(Path.cwd())

    def on_multirun_end(self, config: Any, **kwargs) -> None:
        study = getattr(self, "study", Path(config.hydra.sweep.dir).resolve())
        complete_study(study, str(config.run.name), _numbered(study), getattr(self, "started", None),
                       getattr(self, "revision", None) or code_revision(Path.cwd()))


def finalize_study(study: Path) -> bool:
    """Complete a study whose launcher died, by StudyCompletion's rule, after every trial ended.

    Trials are the numbered folders plus every job submitit recorded (a job that never wrote its
    folder counts as missing). Refuses while a trial is still `running`.
    """
    import yaml

    study = Path(study).resolve(strict=True)
    if (study / "run.json").exists():
        raise SystemExit(f"{study}/run.json already exists; nothing to finalize")
    present = set(_numbered(study))
    submitted = {p.name.rsplit("_", 1)[1] for p in (study / ".submitit").glob("*_*")
                 if p.name.rsplit("_", 1)[1].isdigit()}
    trials = sorted(present | submitted, key=int)
    if not trials:
        raise SystemExit(f"{study} has no trials")
    records = [_trial_status(study / trial) for trial in trials]
    running = [trial for trial, record in zip(trials, records) if record.get("status") == "running"]
    if running:
        raise SystemExit(f"trial(s) {', '.join(running)} still 'running'; confirm with squeue/sacct "
                         "that their jobs ended first")
    config_file = study / min(present, key=int) / ".hydra" / "config.yaml" if present else None
    config = yaml.safe_load(config_file.read_text()) if config_file and config_file.is_file() else {}
    name = str(((config or {}).get("run") or {}).get("name") or study.parent.name)
    started = min((r["started"] for r in records if r.get("started")), default=None)
    revision = next((r["code_revision"] for r in records if r.get("code_revision")), "unversioned")
    return complete_study(study, name, trials, started, revision, finalized_by="cairn finalize-study")
