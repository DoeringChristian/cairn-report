"""MLflow as a logging backend, and MLflow runs as report evidence.

Enable with `[logger] backends = ["mlflow"]` (or `["local", "mlflow"]`) and an
MLflow tracking server in `[logger.mlflow] tracking_uri` or MLFLOW_TRACKING_URI.
Credentials stay in MLflow's own environment variables (MLFLOW_TRACKING_TOKEN,
MLFLOW_TRACKING_USERNAME / MLFLOW_TRACKING_PASSWORD), never in the config.

Writing: one MLflow run per Hydra run directory, tagged `cairn.run_dir`
(outputs/<name>/<timestamp>) and `cairn.run_name`. Scalars become MLflow
metrics, logged files become artifacts at the same relative paths, and the
Hydra config becomes parameters. When the run finishes, the run's
metrics.jsonl / artifacts.jsonl indexes, `.hydra/`, `provenance/`, and
`run.json` are uploaded too, so an MLflow run has exactly the layout of a local
run directory. The MLflow status follows run.json (completed -> FINISHED,
failed -> FAILED), which stays the single completion contract.

Serving: with `[logger.mlflow] serve = true`, the project has its own tracking
server (data in `store`, default mlflow/ in the project, on `port`, default
5000): `cairn-report mlflow` runs it for experiments, and `cairn-report serve`
starts it next to the reports (or reuses it when it is already running), so
report links into the MLflow UI work. It needs the full `mlflow` package
(`cairn-report[mlflow-server]`).

Reading: `snapshot()` downloads a finished MLflow run into the project (by
default generated/mlflow/<run-id>/) so reports read and hash it like a local
run. Runs not written by cairn-report work too: their metric history becomes
metrics.jsonl and their MLflow status becomes run.json.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from .logger import ARTIFACTS, METRICS

STATUS = {"completed": "FINISHED", "failed": "FAILED"}
_open: dict[Path, "MLflowBackend"] = {}
_lock = threading.RLock()  # finish() runs inside attach() for finished trials


LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def tracking_uri(settings: dict) -> str:
    """The configured tracking server; a managed (`serve = true`) server is http://127.0.0.1:<port>."""
    if settings.get("serve"):
        managed = f"http://127.0.0.1:{int(settings.get('port', 5000))}"
        given = settings.get("tracking_uri")
        if given and urlparse(given).hostname not in LOCAL_HOSTS:
            raise SystemExit(f"[logger.mlflow] serve = true runs a local server; tracking_uri {given!r} is not local")
        return given or managed
    uri = settings.get("tracking_uri") or os.environ.get("MLFLOW_TRACKING_URI")
    if not uri:
        raise SystemExit("no MLflow tracking server: set [logger.mlflow] tracking_uri, serve = true, or MLFLOW_TRACKING_URI")
    return uri


def _client(settings: dict):
    try:
        from mlflow.tracking import MlflowClient
    except ImportError:
        raise SystemExit("the mlflow backend needs MLflow: install cairn-report[mlflow] (or mlflow-skinny)") from None
    uri = tracking_uri(settings)
    return MlflowClient(tracking_uri=uri), uri


def _healthy(uri: str) -> bool:
    try:
        with urlopen(f"{uri.rstrip('/')}/health", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def start_server(settings: dict, project: Path, host: str = "127.0.0.1", wait: float = 120) -> subprocess.Popen | None:
    """Start the project's managed tracking server, or None if one already answers on its port."""
    uri = tracking_uri(settings)
    if _healthy(uri):
        return None
    port = urlparse(uri).port or 5000
    store = (project / settings.get("store", "mlflow")).resolve()
    store.mkdir(parents=True, exist_ok=True)
    executable = shutil.which("mlflow") or str(Path(sys.executable).with_name("mlflow"))
    log = (store / "server.log").open("ab")
    process = subprocess.Popen(
        [executable, "server", "--host", host, "--port", str(port),
         "--backend-store-uri", f"sqlite:///{store / 'mlflow.db'}", "--artifacts-destination", str(store / "artifacts")],
        stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "MLFLOW_DISABLE_AGENT_HINT": "1"},
        start_new_session=True,  # its own process group, so stopping it also stops its workers
    )
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if _healthy(uri):
            return process
        if process.poll() is not None:
            tail = (store / "server.log").read_text(errors="replace").strip().splitlines()[-3:]
            raise SystemExit("the MLflow server did not start (it needs the full mlflow package: "
                             "install cairn-report[mlflow-server]):\n  " + "\n  ".join(tail))
        time.sleep(0.5)
    stop_server(process)
    raise SystemExit(f"the MLflow server did not answer on {uri} within {wait:.0f} s; see {store / 'server.log'}")


def stop_server(process: subprocess.Popen | None) -> None:
    """Stop the server and every worker it started."""
    if process is None:
        return
    import signal

    for sig, wait in ((signal.SIGTERM, 20), (signal.SIGKILL, 5)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def stop_on_sigterm() -> None:
    """Treat SIGTERM like Ctrl-C, so `finally` blocks stop the servers this process started."""
    import signal

    def interrupt(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)


def _experiment(client, name: str) -> str:
    experiment = client.get_experiment_by_name(name)
    if experiment is not None:
        return experiment.experiment_id
    try:
        return client.create_experiment(name)
    except Exception:  # created concurrently by another trial
        return client.get_experiment_by_name(name).experiment_id


def _flatten(value, prefix: str = "") -> dict[str, str]:
    if isinstance(value, dict):
        flat = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}{key}."))
        return flat
    return {prefix[:-1]: json.dumps(value) if isinstance(value, (list, tuple)) else str(value)}


def _relative(path: Path, project: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return path.as_posix()


class MLflowBackend:
    name = "mlflow"
    flush_seconds = 60

    @classmethod
    def attach(cls, root: Path, project: Path, settings: dict, required: bool = False) -> "MLflowBackend":
        """The backend of this run directory, shared by every RunLogger created for it."""
        with _lock:
            for other in list(_open.values()):  # a new trial in the same process: close finished ones
                if other.root != root and other.status() is not None:
                    other.finish()
            if root not in _open:
                _open[root] = cls(root, project, settings)
            return _open[root]

    def __init__(self, root: Path, project: Path, settings: dict):
        from mlflow.entities import Param

        self.root, self.project = root, project
        self.client, self.uri = _client(settings)
        if settings.get("serve") and not _healthy(self.uri):
            raise SystemExit(f"the project's MLflow server is not running on {self.uri}; "
                             "start it with `cairn-report mlflow` (or `cairn-report serve`)")
        self.experiment = settings.get("experiment") or project.name
        experiment_id = _experiment(self.client, self.experiment)
        run_dir = _relative(root, project)
        parts = run_dir.split("/")
        run_name = parts[1] if len(parts) >= 3 and parts[0] == "outputs" else root.parent.name
        existing = self.client.search_runs(
            [experiment_id], filter_string=f"tags.`cairn.run_dir` = '{run_dir}' and attributes.status = 'RUNNING'",
            max_results=1,
        )
        if existing:  # the same run directory continues (another logger, or a resumed process)
            self.run_id = existing[0].info.run_id
        else:
            tags = {"cairn.run_dir": run_dir, "cairn.run_name": run_name, "cairn.project": project.name}
            self.run_id = self.client.create_run(experiment_id, tags=tags, run_name="/".join(parts[1:]) or run_name).info.run_id
            config = root / ".hydra" / "config.yaml"
            if config.is_file():
                import yaml

                params = [Param(key[:250], value[:6000]) for key, value in _flatten(yaml.safe_load(config.read_text()) or {}).items() if key]
                for start in range(0, len(params), 100):
                    self.client.log_batch(self.run_id, params=params[start:start + 100])
        self.metrics: list[dict] = []
        self.artifacts: list[dict] = []
        self.last_flush = time.monotonic()
        self.finished = False
        atexit.register(self.finish)

    # ------------------------------------------------------------------ logging

    def scalar(self, event: dict) -> None:
        value = event["value"] if event["value"] is not None else float("nan")
        self.client.log_metric(self.run_id, event["name"], value, step=event["step"] or 0)
        self.metrics.append(event)
        self._maybe_flush()

    def file(self, relative: str, data: bytes, entry: dict) -> None:
        folder, name = os.path.split(relative)
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / name
            local.write_bytes(data)
            self.client.log_artifact(self.run_id, str(local), folder or None)
        self.artifacts.append(entry)
        self._maybe_flush()

    def _maybe_flush(self) -> None:
        if time.monotonic() - self.last_flush > self.flush_seconds:
            self.flush()

    def flush(self) -> None:
        """Upload the two indexes, so the MLflow run reads like a run directory even mid-run."""
        for name, events in ((METRICS, self.metrics), (ARTIFACTS, self.artifacts)):
            if events:
                self.client.log_text(self.run_id, "".join(json.dumps(event) + "\n" for event in events), name)
        self.last_flush = time.monotonic()

    def status(self) -> str | None:
        """The run's terminal status from run.json, or None while it is still running."""
        try:
            status = json.loads((self.root / "run.json").read_text()).get("status")
        except (OSError, ValueError):
            return None
        return status if status in ("completed", "failed", "interrupted") else None

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        try:
            self.flush()
            for folder in (".hydra", "provenance"):
                if (self.root / folder).is_dir():
                    self.client.log_artifacts(self.run_id, str(self.root / folder), folder)
            if (self.root / "run.json").is_file():
                self.client.log_artifact(self.run_id, str(self.root / "run.json"))
            status = self.status()
            self.client.set_terminated(self.run_id, STATUS.get(status, "KILLED") if status else "FINISHED")
        finally:
            with _lock:
                _open.pop(self.root, None)


# ----------------------------------------------------------------- reading


def find_run(settings: dict, project: Path, run_id: str | None = None, run_dir: str | None = None,
             name: str | None = None):
    """An MLflow run by id, by its run directory tag, or the newest finished run of a run name."""
    client, uri = _client(settings)
    if run_id:
        return client, uri, client.get_run(run_id)
    experiment = client.get_experiment_by_name(settings.get("experiment") or project.name)
    if experiment is None:
        raise ValueError(f"no MLflow experiment {settings.get('experiment') or project.name!r} at {uri}")
    if run_dir:
        query = f"tags.`cairn.run_dir` = '{run_dir}'"
    elif name:
        query = f"tags.`cairn.run_name` = '{name}' and attributes.status = 'FINISHED'"
    else:
        raise ValueError("find_run needs run_id, run_dir, or name")
    runs = client.search_runs([experiment.experiment_id], filter_string=query, order_by=["attributes.start_time DESC"], max_results=1)
    if not runs:
        raise ValueError(f"no MLflow run matches {query} in experiment {experiment.name!r} at {uri}")
    return client, uri, runs[0]


def snapshot(settings: dict, project: Path, cache: Path, **query) -> tuple[Path, dict]:
    """Download an MLflow run into cache/<run-id>/ (reused while the run is unchanged)."""
    import mlflow.artifacts

    client, uri, run = find_run(settings, project, **query)
    info = run.info
    record = {"run_id": info.run_id, "tracking_uri": uri, "experiment_id": info.experiment_id,
              "status": info.status, "end_time": info.end_time, "run_dir": run.data.tags.get("cairn.run_dir")}
    target = cache / info.run_id
    marker = target / "mlflow-run.json"
    if marker.is_file() and json.loads(marker.read_text()) == record and info.status != "RUNNING":
        return target, record
    cache.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=cache, prefix=f".{info.run_id}."))
    try:
        mlflow.artifacts.download_artifacts(run_id=info.run_id, dst_path=str(staging), tracking_uri=uri)
        if not (staging / METRICS).is_file():  # not written by cairn-report: rebuild the index from MLflow
            with (staging / METRICS).open("w") as handle:
                for key in sorted(run.data.metrics):
                    for metric in client.get_metric_history(info.run_id, key):
                        handle.write(json.dumps({"name": key, "value": metric.value, "step": metric.step,
                                                 "time": metric.timestamp, "origin": {}}) + "\n")
        if not (staging / "run.json").is_file():
            status = {"FINISHED": "completed", "FAILED": "failed", "KILLED": "interrupted"}.get(info.status, "running")
            (staging / "run.json").write_text(json.dumps({"status": status, "source": "mlflow"}) + "\n")
        (staging / "mlflow-run.json").write_text(json.dumps(record) + "\n")
        if target.exists():
            shutil.rmtree(target)
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return target, record
