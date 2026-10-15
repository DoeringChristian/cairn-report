"""Run bookkeeping for Hydra projects: run.json, drafts as run folders (or the per-name `latest` link), and study
completion.

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

Drafts as run folders. Importing this module registers the OmegaConf resolver `cairn.draft`, so a config can
write into a draft (cairn/client/drafts.py) instead of a runs/ folder:

    hydra:
      run: {dir: "${cairn.draft:descent}"}                              # one run: one draft
      sweep: {dir: "${cairn.draft:descent-sweep}", subdir: "${hydra.job.num}"}   # trials are its subfolders

The resolver is cached (OmegaConf `use_cache`), so every resolution in a job, and every trial of a sweep
(Hydra copies the launcher's resolver cache into each trial's config), gets the same folder. In a draft,
`run_experiment` records the job's pulls in the run folder, stores the resolved job config and its overrides
as `metadata["config"]` and `metadata["overrides"]`, and, for a single run, publishes the draft when the
experiment completes (a failed run keeps it, see `cairn drafts`) instead of moving `latest`; run.json is
written as always. `StudyCompletion` (and `cairn finalize-study`) publishes a sweep draft once every trial
completed, with the base config and the sweep's overrides as metadata, and keeps it otherwise. The experiment
gets its draft from `cairn.current_draft()` (a view on its trial folder in a sweep); `run(output_dir)` is unchanged.
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
RESOLVER = "cairn.draft"


def draft_folder(name: str) -> str:
    """The `${cairn.draft:<name>}` resolver: a new draft of artifact `name`, as its folder (cached per config)."""
    from ..client.drafts import Draft

    return str(Draft(str(name), activate=False).folder)


def register_resolver() -> None:
    """Register `cairn.draft` with OmegaConf (done on import; nothing happens without OmegaConf)."""
    try:
        from omegaconf import OmegaConf
    except ImportError:
        return
    if not OmegaConf.has_resolver(RESOLVER):
        OmegaConf.register_new_resolver(RESOLVER, draft_folder, use_cache=True)


register_resolver()


def _plain(config: Any) -> Any:
    """A config as plain JSON values (resolved); None when it cannot be resolved or serialized."""
    try:
        from omegaconf import OmegaConf

        value = OmegaConf.to_container(config, resolve=True) if OmegaConf.is_config(config) else config
        json.dumps(value)
        return value
    except Exception:  # an interpolation that fails, a value JSON cannot hold: no config metadata
        return None


def _config_metadata(config: Any, overrides) -> dict:
    found = {}
    plain = _plain(config)
    if plain is not None:
        found["config"] = plain
    if overrides is not None:
        found["overrides"] = [str(item) for item in overrides]
    return found


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

    from ..client.drafts import find as find_draft

    hydra_cfg = HydraConfig.get()
    output_dir = Path(hydra_cfg.runtime.output_dir).resolve()
    draft = find_draft(output_dir)
    single = draft is not None and not draft.trial and hydra_cfg.mode == RunMode.RUN
    if draft is not None:
        if single:
            draft.set_metadata(**_config_metadata(cfg, list(hydra_cfg.overrides.task)))
        draft.activate()  # the job's pulls are recorded in its folder (a trial's own subfolder in a sweep)
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
        if draft is not None:
            draft.fail(error) if single else draft.deactivate()
        raise
    write_status(output_dir, {**record, "status": "completed", "ended": now()})
    if single:
        draft.publish()
    elif draft is not None:
        draft.deactivate()
    elif hydra_cfg.mode == RunMode.RUN:
        update_latest(output_dir)


def _trial_status(folder: Path) -> dict:
    status_file = folder / "run.json"
    return json.loads(status_file.read_text()) if status_file.is_file() else {}


def complete_study(study: Path, name: str, trials: list[str], started: str | None, revision: str,
                   metadata: dict | None = None, **extra: Any) -> bool:
    """Write a study's run.json (completed only when every trial completed) and move latest; a sweep draft is
    published instead (with `metadata` added), or kept when a trial did not complete."""
    from ..client.drafts import find as find_draft

    records = {trial: _trial_status(study / trial) for trial in trials}
    rows = [{"trial": trial, "status": records[trial].get("status", "missing")} for trial in trials]
    complete = bool(rows) and all(row["status"] == "completed" for row in rows)
    study.mkdir(parents=True, exist_ok=True)
    write_status(study, {"status": "completed" if complete else "failed", "kind": "study",
                         "name": validate_run_name(name), "started": started, "ended": now(),
                         "code_revision": revision, "trials": rows, **extra})
    draft = find_draft(study)
    if draft is not None and not draft.trial:
        if metadata:
            draft.set_metadata(**metadata)
        if complete:
            draft.publish()
        else:
            failed = [row["trial"] for row in rows if row["status"] != "completed"]
            draft.fail(f"trial(s) {', '.join(failed) or '-'} did not complete")
    elif complete:
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
                       getattr(self, "revision", None) or code_revision(Path.cwd()), metadata=_study_metadata(config))


def _study_metadata(config: Any) -> dict:
    """A sweep's base config (without the hydra node) and its overrides (the sweep), for a sweep draft."""
    try:
        from omegaconf import OmegaConf

        base = OmegaConf.masked_copy(config, [key for key in config if key != "hydra"])
        overrides = list(config.hydra.overrides.task)
    except Exception:
        return {}
    return _config_metadata(base, overrides)


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
    metadata = {}
    sweep = study / "multirun.yaml"  # the launcher's config: the base config and the sweep's overrides
    if sweep.is_file():
        try:
            from omegaconf import OmegaConf

            metadata = _study_metadata(OmegaConf.load(sweep))
        except ImportError:
            pass
    return complete_study(study, name, trials, started, revision, metadata=metadata, finalized_by="cairn finalize-study")
