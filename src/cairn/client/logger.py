"""The run logger: write run evidence to fixed locations with its code origin.

A `cairn.Draft` is a `RunLogger` on its own folder (`run.scalar(...)`, `run.figure(...)`, ...), so a run
logs straight into the artifact version it becomes. For a plain folder use `RunLogger(folder)`
(also importable as `cairn.runs.logger.RunLogger`, the same class). It only writes files: no tracking,
no store. This module defines the run file format for both sides: the
report builder reads runs with the same `read_*` functions. Every call writes one well-defined file. The two indexes are
append-only JSON Lines, so nothing already written is ever rewritten:

    <run>/
      metrics.jsonl        one event per logged scalar: name, value, step, time, origin
      artifacts.jsonl      one event per logged file: name, kind, path, time, origin, producer
      tables/<name>.csv    tidy tables
      data/<name>.json     structured results
      figures/<name>.png   Matplotlib figures (optional .svg)
      figures/<name>.plotly.json
      images/<name>.png    arrays or existing image files
      artifacts/<name>.*   any other evidence: models, animations, archives
      logs/<name>.txt      human-readable logs

The logger only writes these files. A draft publishes its folder when it closes (`cairn.draft(name,
watch=True)` keeps a running version synced meanwhile); a plain folder is published with `cairn publish`.

Read them back with `read_scalars(run_dir)` (latest value per name) and
`read_artifacts(run_dir)` (latest entry per path); a truncated last line from a
crash is ignored.

Usage:

    log = RunLogger(output_dir)
    log.scalar("rmse", rmse)                    # single value
    log.scalar("loss", loss, step=epoch)        # series
    log.table("trials", rows)
    log.figure("error_histogram", error_histogram, errors)  # calls the plot function
    log.plotly("running_rmse", spec)
    log.artifact("model", checkpoint_path)      # or bytes with suffix=".pt"
    log.text("solver", solver_log)
"""

from __future__ import annotations

import csv
import inspect
import io
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from numbers import Number
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..formats import ARTIFACTS, METRICS, fold_artifacts, fold_scalars, read_artifacts, read_jsonl, read_scalars  # noqa: F401

NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,95}")
THIS_FILE = Path(__file__).resolve()
_RESOLVED: dict[str, Path] = {}  # code file name -> resolved path, for origin lookups


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project_root(start: Path) -> Path:
    """The project root (same rule as the report builder), so origins stay valid in every run."""
    from ..config import find_project

    return find_project(start)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _append(path: Path, record: dict) -> None:
    """Append one JSON line with a single write, so earlier lines are never touched."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, allow_nan=False) + "\n")


def _plain(value: Any) -> Any:
    """Convert NumPy scalars and arrays to JSON-ready Python values."""
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


class RunLogger:
    def __init__(self, output_dir: Path | str):
        """One logger per run directory."""
        self.root = Path(output_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.project = _project_root(self.root)

    def close(self) -> None:
        """Nothing is buffered; kept so existing `with`-less callers keep working."""

    # ---------------------------------------------------------------- scalars

    def scalar(self, name: str, value: Number, step: int | None = None) -> None:
        """Append one scalar event. Without `step` it is a single value; with it, a series point."""
        self._check(name)
        number = _plain(value)
        if isinstance(number, bool) or not isinstance(number, Number):
            raise TypeError(
                f"scalar {name!r} must be a number, got {type(value).__name__}"
            )
        finite = math.isfinite(number)
        event = {
            "name": name,
            "value": number if finite else None,
            "step": step,
            "time": _now(),
            "origin": self._origin(),
        }
        if not finite:
            event["nonfinite"] = repr(float(number))
        _append(self.root / METRICS, event)

    def scalars(self, values: Mapping[str, Number], step: int | None = None) -> None:
        for name, value in values.items():
            self.scalar(name, value, step)

    # ------------------------------------------------------- tables and data

    def table(self, name: str, rows: Iterable[Mapping[str, Any]]) -> Path:
        """Write a tidy table; every row must have the same columns."""
        rows = [{key: _plain(item) for key, item in row.items()} for row in rows]
        if not rows:
            raise ValueError(f"table {name!r} is empty")
        columns = list(rows[0])
        if any(list(row) != columns for row in rows):
            raise ValueError(f"table {name!r} rows have different columns")
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return self._write(
            name, "table", f"tables/{name}.csv", buffer.getvalue().encode()
        )

    def json(self, name: str, value: Any) -> Path:
        """Write a structured result that is not a scalar series or table."""
        data = json.dumps(_plain(value), indent=2, allow_nan=False) + "\n"
        return self._write(name, "data", f"data/{name}.json", data.encode())

    # ---------------------------------------------------------------- figures

    def figure(
        self, name: str, figure: Any, *args, svg: bool = False, dpi: int = 160, **kwargs
    ) -> Path:
        """Save a Matplotlib figure and close it.

        Pass the plotting function itself (`log.figure("hist", error_histogram, errors)`)
        to record where the figure was drawn as well as where it was logged.
        """
        producer = None
        if callable(figure) and not hasattr(figure, "savefig"):
            producer = self._code_location(figure)
            figure = figure(*args, **kwargs)
        if not hasattr(figure, "savefig"):
            raise TypeError(f"figure {name!r} is not a Matplotlib figure")
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", dpi=dpi, bbox_inches="tight")
        path = self._write(
            name, "figure", f"figures/{name}.png", buffer.getvalue(), producer=producer
        )
        if svg:
            vector = io.BytesIO()
            figure.savefig(vector, format="svg", bbox_inches="tight")
            self._write(
                f"{name}.svg",
                "figure",
                f"figures/{name}.svg",
                vector.getvalue(),
                producer=producer,
            )
        import matplotlib.pyplot as plt

        plt.close(figure)
        return path

    def plotly(self, name: str, figure: Any) -> Path:
        """Save a Plotly figure (object or {data, layout, config} dict) as JSON."""
        spec = figure.to_plotly_json() if hasattr(figure, "to_plotly_json") else figure
        if not isinstance(spec, Mapping) or "data" not in spec:
            raise TypeError(f"plotly figure {name!r} needs a 'data' entry")
        spec = {
            key: _plain(spec[key])
            for key in ("data", "layout", "config")
            if key in spec
        }
        data = json.dumps(spec, allow_nan=False) + "\n"
        return self._write(name, "plotly", f"figures/{name}.plotly.json", data.encode())

    def image(self, name: str, image: Any) -> Path:
        """Save an image array (H×W or H×W×C) or copy an existing image file."""
        if isinstance(image, (str, Path)):
            source = Path(image)
            target = f"images/{name}{source.suffix.lower()}"
            return self._write(name, "image", target, source.read_bytes())
        import matplotlib.pyplot as plt

        buffer = io.BytesIO()
        plt.imsave(buffer, image, format="png")
        return self._write(name, "image", f"images/{name}.png", buffer.getvalue())

    def video(self, name: str, video: Any, fps: float = 30) -> Path:
        """Copy a video file (MP4, WebM, MOV) or encode frames (T×H×W or T×H×W×C; uint8, or floats in
        [0, 1]) as an H.264 MP4 that browsers play and seek (needs `imageio[ffmpeg]`)."""
        if isinstance(video, (str, Path)):
            source = Path(video)
            return self._write(name, "video", f"videos/{name}{source.suffix.lower()}", source.read_bytes())
        try:
            import imageio.v3 as iio
        except ImportError as error:
            raise ImportError("log.video with frames needs imageio and imageio-ffmpeg "
                              "(`pip install 'cairn[video]'`)") from error
        import numpy as np

        frames = np.asarray(video)
        if frames.dtype != np.uint8:
            frames = (np.clip(frames, 0, 1) * 255).round().astype(np.uint8)
        if frames.ndim == 3:
            frames = np.repeat(frames[..., None], 3, axis=-1)
        frames = frames[..., :3]
        # yuv420p (what every browser decodes) needs even sizes
        frames = np.pad(frames, ((0, 0), (0, frames.shape[1] % 2), (0, frames.shape[2] % 2), (0, 0)), mode="edge")
        data = iio.imwrite("<bytes>", frames, extension=".mp4", plugin="FFMPEG", fps=fps, codec="libx264",
                           pixelformat="yuv420p", macro_block_size=1,  # keep the frame size as given
                           output_params=["-movflags", "+faststart"])  # seekable before fully loaded
        return self._write(name, "video", f"videos/{name}.mp4", data)

    # ------------------------------------------------------- other evidence

    def artifact(
        self, name: str, source: Path | str | bytes, suffix: str | None = None
    ) -> Path:
        """Store any other evidence file (model, animation, archive) under artifacts/."""
        if isinstance(source, (str, Path)):
            source = Path(source)
            if not source.is_file():
                raise ValueError(
                    f"artifact {name!r}: {source} is not a file (archive directories first)"
                )
            data, suffix = source.read_bytes(), suffix or "".join(source.suffixes)
        else:
            data = bytes(source)
            if not suffix:
                raise ValueError(
                    f"artifact {name!r} from bytes needs a suffix, e.g. suffix='.pt'"
                )
        return self._write(name, "artifact", f"artifacts/{name}{suffix}", data)

    def text(self, name: str, content: str) -> Path:
        """Write a human-readable log; keep scientific values in scalars, tables, or data."""
        return self._write(name, "log", f"logs/{name}.txt", content.encode("utf-8"))

    # ---------------------------------------------------------------- helpers

    def _check(self, name: str) -> None:
        if not NAME.fullmatch(name):
            raise ValueError(
                f"log name {name!r} must match {NAME.pattern} (lowercase, no slashes)"
            )

    def _write(
        self,
        name: str,
        kind: str,
        relative: str,
        data: bytes,
        producer: dict | None = None,
    ) -> Path:
        self._check(name)
        entry = {"name": name, "kind": kind, "path": relative, "time": _now(), "origin": self._origin()}
        if producer:
            entry["producer"] = producer
        _atomic_write(self.root / relative, data)
        _append(self.root / ARTIFACTS, entry)
        return self.root / relative

    def _relative(self, file: str) -> str:
        path = _RESOLVED.get(file) or Path(file).resolve()
        try:
            return path.relative_to(self.project).as_posix()
        except ValueError:
            return path.as_posix()

    def _origin(self) -> dict:
        """The first frame outside this module: where the experiment called the logger.

        Walks frames directly (inspect.stack() would read source context for every frame on every call)."""
        frame = sys._getframe(1)
        while frame is not None:
            name = frame.f_code.co_filename
            if name not in _RESOLVED:
                _RESOLVED[name] = Path(name).resolve()
            if _RESOLVED[name] != THIS_FILE:
                return {"file": self._relative(name), "line": frame.f_lineno, "function": frame.f_code.co_name}
            frame = frame.f_back
        return {}

    def _code_location(self, function: Callable) -> dict:
        target = inspect.unwrap(function)
        try:
            _, line = inspect.getsourcelines(target)
            file = inspect.getsourcefile(target)
        except (OSError, TypeError):
            return {"function": getattr(target, "__qualname__", repr(target))}
        return {
            "file": self._relative(file),
            "line": line,
            "function": target.__qualname__,
        }
