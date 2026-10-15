"""Hydra jobs as Cairn runs: `run_experiment`, the `${cairn.run:}` and `${cairn.draft:NAME}` resolvers, and study
completion. Needs the `hydra` extra.

A project's experiments/run.py is only:

    @hydra.main(version_base=None, config_path="../configs", config_name="config")
    def main(cfg: DictConfig) -> None:
        run_experiment(cfg, Experiment)

with, in configs/config.yaml:

    hydra:
      run: {dir: "${cairn.run:}"}                                   # the job's folder is its run's folder
      sweep: {dir: "${cairn.run:}", subdir: "${hydra.job.num}"}     # a sweep: one run per trial, plus a summary run
      callbacks:
        source_snapshot:
          _target_: cairn.runs.snapshot.SourceSnapshotCallback
        study_completion:            # multirun only: publishes the sweep's summary run
          _target_: cairn.runs.hydra.StudyCompletion

Every job is a run (cairn/client/runs.py): `run_experiment` calls `cairn.init(name=cfg.run.name, config=<the resolved
job config>, metadata={"overrides": [...]})`, calls `experiment.run(output_dir)`, and finishes the run when the
experiment returns (failed, with the traceback, when it raises). Importing this module registers the resolvers:

- `${cairn.run:}` reserves a run folder (`.cairn/drafts/runs/<run.name>/<uid>/`) for `hydra.run.dir`, so Hydra's job
  folder IS the run's folder: `.hydra/`, `provenance/`, the job's outputs, and the run's logs, system metrics, and
  metrics.jsonl are one version of `runs/<run.name>`. Such a job writes no run.json: the version's status is the
  record. In `hydra.sweep.dir` the folder is the sweep's: each trial (`<sweep>/<n>`) is a run of its own, a version
  of `runs/<run.name>` labelled `<sweep label>/<n>` with `metadata.sweep` = {uid, label, num}, published when the
  trial ends (its folder then leaves the sweep's, and the trial is listed in the sweep's `trials.jsonl`).
  `StudyCompletion` (and `cairn finalize-study`) then publishes the sweep folder (multirun.yaml, trials.jsonl) as a
  run of its own, `metadata.kind = "run"` with `metadata.sweep.trials`, whose inputs are the trial runs; it is kept
  (not published) when a trial did not complete.
- `${cairn.draft:NAME}` keeps working: the job folder is an output draft of `NAME` (a sweep's trials are its
  subfolders), and the job's run gets a folder of its own. A single job's draft is published when the experiment
  completes, with the job's run as its input; a sweep draft by `StudyCompletion`, with the trial runs as inputs.

Both resolvers are cached (OmegaConf `use_cache`), so every resolution in a job, and every trial of a sweep (Hydra
copies the launcher's resolver cache into each trial's config), gets the same folder.

A job whose folder is neither (`outputs/<run.name>/<time>`) is a run too, with a folder of its own; its job folder
gets `run.json`, its completion record (`running`, then `completed` or `failed` with the error, and `run`: the run's
reference), and only a completed single job advances `outputs/<run.name>/latest`; a multirun study advances it once,
from `StudyCompletion`, after every trial completed. `cairn finalize-study <study>` completes a study whose launcher
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
RESOLVER = "cairn.draft"
RUN_RESOLVER = "cairn.run"
TRIALS = "trials.jsonl"


def draft_folder(name: str) -> str:
    """The `${cairn.draft:<name>}` resolver: a new draft of artifact `name`, as its folder (cached per config)."""
    from ..client.drafts import Draft

    return str(Draft(str(name), activate=False).folder)


def run_folder(name: str | None = None) -> str:
    """The `${cairn.run:}` resolver: a reserved folder for a run (cached per config). The run takes its name from
    `run.name` when the job starts it; `${cairn.run:NAME}` names it here already (the folder's place only)."""
    from ..client.drafts import reserve
    from ..client.runs import RUNS, _slug, default_name

    folder, _ = reserve(f"{RUNS}/{_slug(name or default_name())}", metadata={"kind": "run"}, kind="run")
    return str(folder)


def register_resolver() -> None:
    """Register `cairn.draft` and `cairn.run` with OmegaConf (done on import; nothing happens without OmegaConf)."""
    try:
        from omegaconf import OmegaConf
    except ImportError:
        return
    if not OmegaConf.has_resolver(RESOLVER):
        OmegaConf.register_new_resolver(RESOLVER, draft_folder, use_cache=True)
    if not OmegaConf.has_resolver(RUN_RESOLVER):
        OmegaConf.register_new_resolver(RUN_RESOLVER, run_folder, use_cache=True)


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


def _job_run(cfg: Any, output_dir: Path, overrides: list[str], multirun: bool, num: Any):
    """Start the job's run: in the job folder when `${cairn.run:}` reserved it (a sweep's trial in its subfolder),
    else in a folder of its own. Returns (run, kind): kind "run" (the job folder is the run's), "trial" (a trial of
    a `${cairn.run:}` sweep), or None."""
    from ..client import runs
    from ..client.drafts import find as find_draft

    name = validate_run_name(str(cfg.run.name))
    config = _plain(cfg)
    metadata = {"overrides": overrides}
    if cfg.run.get("description"):
        metadata["description"] = str(cfg.run.get("description"))
    found = find_draft(output_dir)
    if found is not None and found.record.get("kind") == "run":
        if not found.trial:
            return runs.init(name, config=config, metadata=metadata, _adopt=found, code=False), "run"
        sweep = {"uid": found.uid, "label": found.label, "num": int(num) if str(num).isdigit() else num, "ref": found.ref}
        label = f"{found.label}/{num}"
        return runs.init(name, config=config, metadata={**metadata, "sweep": sweep}, label=label, _folder=output_dir,
                         code=False), "trial"
    return runs.init(name, config=config, metadata=metadata, code=not (output_dir / "provenance").exists()), None


def run_experiment(cfg: Any, base: type | None = None, require_source_snapshot: bool = True) -> None:
    """The body of a project's Hydra entry point: start the job's run, check the source snapshot, instantiate
    `cfg.experiment` (an instance of `base`, when given), call `run(output_dir)`, and finish the run (see the module
    doc for where its folder is, and what happens to a `${cairn.draft:...}` or plain job folder).

    Without a snapshot from `cairn.runs.snapshot.SourceSnapshotCallback` the run fails, unless
    `require_source_snapshot=False`."""
    from hydra.core.hydra_config import HydraConfig
    from hydra.types import RunMode
    from hydra.utils import get_original_cwd, instantiate

    from ..client.drafts import find as find_draft
    from .snapshot import require_snapshot

    hydra_cfg = HydraConfig.get()
    output_dir = Path(hydra_cfg.runtime.output_dir).resolve()
    multirun = hydra_cfg.mode == RunMode.MULTIRUN
    overrides = [str(item) for item in hydra_cfg.overrides.task]
    run, kind = _job_run(cfg, output_dir, overrides, multirun, hydra_cfg.job.get("num"))
    draft = None if kind else find_draft(output_dir)
    single = draft is not None and not draft.trial and not multirun
    if draft is not None:
        if single:
            draft.set_metadata(**_config_metadata(cfg, overrides))
        draft.activate()  # an output of the job's run (a trial's view in a sweep)
    record = None
    if kind is None:  # the job folder is not the run's: run.json records the job there
        record = {
            "status": "running",
            "name": validate_run_name(str(cfg.run.name)),
            "description": str(cfg.run.get("description", "")),
            "started": now(),
            "code_revision": code_revision(Path(get_original_cwd())),
            "run": run.ref,
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
        if record is not None:
            write_status(output_dir, {**record, "status": "failed", "ended": now(),
                                      "error": f"{type(error).__name__}: {error}",
                                      "traceback": traceback.format_exc()})
        if draft is not None:
            draft.fail(error) if single else draft.deactivate()
        _end_trial(run, kind, output_dir, error)
        raise
    if record is not None:
        write_status(output_dir, {**record, "status": "completed", "ended": now()})
    if single:
        draft.publish()
    elif draft is not None:
        draft.deactivate()
    _end_trial(run, kind, output_dir, None)
    if kind is None and draft is None and not multirun:
        update_latest(output_dir)


def _end_trial(run, kind, output_dir: Path, error) -> None:
    """Finish the job's run; a `${cairn.run:}` trial is then listed in its sweep's trials.jsonl."""
    try:
        version = run.finish(error)
    finally:
        if kind == "trial":
            sweep = (run.record.get("metadata") or {}).get("sweep") or {}
            line = {"num": sweep.get("num"), "uid": run.uid, "label": run.label, "ref": version.ref if version is not None else run.ref,
                    "status": "failed" if error is not None or version is None else "completed", "time": now()}
            with (output_dir.parent / TRIALS).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line) + "\n")


def _trial_status(folder: Path) -> dict:
    status_file = folder / "run.json"
    return json.loads(status_file.read_text()) if status_file.is_file() else {}


def _trial_runs(study: Path) -> dict[str, dict]:
    """The trials a `${cairn.run:}` sweep listed in trials.jsonl, by number (the last line of each wins)."""
    from ..formats import read_jsonl

    return {str(line.get("num")): line for line in read_jsonl(study / TRIALS) if line.get("num") is not None}


def complete_study(study: Path, name: str, trials: list[str], started: str | None, revision: str,
                   metadata: dict | None = None, **extra: Any) -> bool:
    """Complete a study (only when every trial completed). A `${cairn.run:}` sweep publishes its folder as the summary
    run, whose inputs are the trial runs; a sweep draft is published (its inputs: the trials' runs), with `metadata`
    added; a plain study gets run.json and moves latest. Kept (not published) when a trial did not complete."""
    from ..client.drafts import find as find_draft

    draft = find_draft(study)
    if draft is not None and not draft.trial and draft.record.get("kind") == "run":
        return _complete_run_sweep(draft, study, name, trials, metadata or {}, extra)
    records = {trial: _trial_status(study / trial) for trial in trials}
    rows = [{"trial": trial, "status": records[trial].get("status", "missing")} for trial in trials]
    complete = bool(rows) and all(row["status"] == "completed" for row in rows)
    study.mkdir(parents=True, exist_ok=True)
    write_status(study, {"status": "completed" if complete else "failed", "kind": "study",
                         "name": validate_run_name(name), "started": started, "ended": now(),
                         "code_revision": revision, "trials": rows, **extra})
    if draft is not None and not draft.trial:
        if metadata:
            draft.set_metadata(**metadata)
        runs = [records[trial]["run"] for trial in trials if records[trial].get("run")]
        if runs:
            draft.record["inputs"] = runs
            draft._save()
        if complete:
            draft.publish()
        else:
            failed = [row["trial"] for row in rows if row["status"] != "completed"]
            draft.fail(f"trial(s) {', '.join(failed) or '-'} did not complete")
    elif complete:
        update_latest(study)
    return complete


def _complete_run_sweep(reserved, study: Path, name: str, trials: list[str], metadata: dict, extra: dict) -> bool:
    """Publish a `${cairn.run:}` sweep's folder as a run of `runs/<name>` whose inputs are its trial runs."""
    from ..client.runs import Run

    listed = _trial_runs(study)
    numbers = sorted(set(trials) | set(listed), key=lambda item: (not item.isdigit(), int(item) if item.isdigit() else 0, item))
    rows = [{"trial": number, "status": listed.get(number, {}).get("status", "missing"), "ref": listed.get(number, {}).get("ref")}
            for number in numbers]
    complete = bool(rows) and all(row["status"] == "completed" for row in rows)
    inputs = [row["ref"] for row in rows if row["status"] == "completed" and row["ref"]]
    summary = {**metadata, "sweep": {"trials": rows, **{key: value for key, value in extra.items() if value is not None}}}
    run = Run(validate_run_name(name), adopt=reserved, metadata=summary, inputs=inputs)
    if complete:
        run.publish()
    else:
        failed = [row["trial"] for row in rows if row["status"] != "completed"]
        run.fail(f"trial(s) {', '.join(failed) or '-'} did not complete")
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
    from ..client.drafts import find as find_draft

    reserved = find_draft(study)
    if reserved is not None and not reserved.trial and reserved.record.get("kind") == "run":
        return _finalize_run_sweep(reserved, study)
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


def _finalize_run_sweep(reserved, study: Path) -> bool:
    """`finalize_study` for a `${cairn.run:}` sweep: its trials are the numbered folders still there (trials that did
    not end well, or still run), the jobs submitit recorded, and the trials.jsonl lines."""
    import yaml

    from ..store.cache import read_json

    present = set(_numbered(study))
    submitted = {p.name.rsplit("_", 1)[1] for p in (study / ".submitit").glob("*_*") if p.name.rsplit("_", 1)[1].isdigit()}
    listed = _trial_runs(study)
    trials = sorted(present | submitted | set(listed), key=int)
    if not trials:
        raise SystemExit(f"{study} has no trials")
    running = [trial for trial in trials if trial not in listed and (read_json(study / f"{trial}.draft.json") or {}).get("state") == "open"]
    if running:
        raise SystemExit(f"trial(s) {', '.join(running)} still running; confirm with squeue/sacct that their jobs ended first")
    metadata, name = {}, None
    sweep = study / "multirun.yaml"
    if sweep.is_file():
        try:
            from omegaconf import OmegaConf

            loaded = OmegaConf.load(sweep)
            metadata = _study_metadata(loaded)
            name = str(loaded.run.name)
        except Exception:
            pass
    if name is None:
        config = yaml.safe_load(sweep.read_text()) if sweep.is_file() else {}
        name = str(((config or {}).get("run") or {}).get("name") or reserved.name.split("/", 1)[-1])
    return _complete_run_sweep(reserved, study, name, trials, metadata, {"finalized_by": "cairn finalize-study"})
