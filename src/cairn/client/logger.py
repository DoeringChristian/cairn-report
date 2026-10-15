"""The run logger: write run evidence to fixed locations with its code origin.

`Writer` is the API a `cairn.Draft` and a `cairn.Run` share (`add`, `add_figure`, ...), so a job writes straight
into the artifact version its folder becomes. For a plain folder use `RunLogger(folder)` (also importable as
`cairn.runs.logger.RunLogger`): the same writer, plus the names of earlier releases (`scalar`, `figure`, ...) as
deprecated aliases for one release. It only writes files: no tracking, no store. This module defines the file
format for both sides: the report builder reads it with the `read_*` functions of cairn/formats.py. Every call
writes one well-defined file. The two indexes are append-only JSON Lines, so nothing already written is ever
rewritten:

    <folder>/
      metrics.jsonl        one entry per add({...}): the dict as given, plus time and origin
      artifacts.jsonl      one entry per written file: name, kind, path, time, origin, producer, metadata
      figures/<name>.png   Matplotlib figures (optional .svg), figures/<name>.plotly.json
      images/<name>.png    arrays or existing image files
      videos/<name>.mp4    frames or existing video files
      tables/<name>.csv    tidy tables
      data/<name>.json     structured results
      text/<name>.txt      human-readable text
      files/<name>         any other file or folder: models, archives (add_file, add_dir)

Usage:

    log = RunLogger(output_dir)                 # or a draft, or the run (cairn.init)
    log.add({"loss": loss, "psnr": psnr, "step": step})   # one entry, any keys
    log.add_table("trials", rows)
    log.add_figure("error_histogram", error_histogram, {"step": step}, errors)  # calls the plot function
    log.add_plotly("running_rmse", spec)
    log.add_file("checkpoints/best.pt")         # files/best.pt
    log.add_text("solver", solver_log)

Every `add_*` takes an optional `metadata` dict for that item, kept with its entry in artifacts.jsonl.
Read them back with `read_scalars(folder)` (latest value per key), `read_series(folder, key)`, and
`read_artifacts(folder)` (latest entry per path); a truncated last line from a crash is ignored.
"""

from __future__ import annotations

import csv
import inspect
import io
import json
import math
import os
import re
import shutil
import sys
import warnings
from datetime import datetime, timezone
from numbers import Number
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..formats import ARTIFACTS, METRICS, fold_artifacts, fold_scalars, read_artifacts, read_jsonl, read_scalars  # noqa: F401

NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,95}")
THIS_FILE = Path(__file__).resolve()
_RESOLVED: dict[str, Path] = {}  # code file name -> resolved path, for origin lookups
_OWN_FILES: set[Path] = {THIS_FILE}  # modules whose frames are never an origin (the drafts and runs modules add theirs)


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
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _finite(value: Any, key: str, nonfinite: dict) -> Any:
    """`value` with every non-finite float replaced by None (noted in `nonfinite` under its key path)."""
    if isinstance(value, float) and not math.isfinite(value):
        nonfinite[key] = repr(value)
        return None
    if isinstance(value, dict):
        return {name: _finite(item, f"{key}.{name}", nonfinite) for name, item in value.items()}
    if isinstance(value, list):
        return [_finite(item, f"{key}.{index}", nonfinite) for index, item in enumerate(value)]
    return value


def _metadata(metadata: Mapping | None, what: str) -> dict | None:
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        raise TypeError(f"{what}: metadata must be a dict, got {type(metadata).__name__}")
    nonfinite: dict = {}
    clean = {str(key): _finite(_plain(value), str(key), nonfinite) for key, value in metadata.items()}
    json.dumps(clean, allow_nan=False)
    return clean


class Writer:
    """Writes run evidence into `root` (see the module doc). `add` appends one metrics entry; every `add_*`
    writes one file (or folder) and indexes it in artifacts.jsonl with its optional `metadata`."""

    def __init__(self, output_dir: Path | str):
        self.root = Path(output_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.project = _project_root(self.root)

    def close(self) -> None:
        """Nothing is buffered; kept so existing `with`-less callers keep working."""

    # ---------------------------------------------------------------- entries

    def add(self, entry: Mapping[str, Any]) -> None:
        """Append one entry to metrics.jsonl: the dict as given (any keys; `step` is just a key), plus `time` and
        the code origin. A non-finite number is stored as null, its key listed in `nonfinite`."""
        if not isinstance(entry, Mapping) or not entry:
            raise TypeError(f"add() takes a non-empty dict, got {type(entry).__name__}")
        nonfinite: dict = {}
        record = {}
        for key, value in entry.items():
            if not isinstance(key, str) or not key:
                raise TypeError(f"add(): keys must be non-empty strings, got {key!r}")
            if key in ("origin", "nonfinite"):
                raise ValueError(f"add(): {key!r} is written by the logger")
            record[key] = _finite(_plain(value), key, nonfinite)
        record.setdefault("time", _now())
        record["origin"] = self._origin()
        if nonfinite:
            record["nonfinite"] = nonfinite
        _append(self.root / METRICS, record)

    # ------------------------------------------------------- tables and data

    def add_table(self, name: str, rows: Iterable[Mapping[str, Any]], metadata: Mapping | None = None) -> Path:
        """Write a tidy table (tables/<name>.csv); every row must have the same columns."""
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
        return self._write(name, "table", f"tables/{name}.csv", buffer.getvalue().encode(), metadata=metadata)

    def add_json(self, name: str, value: Any, metadata: Mapping | None = None) -> Path:
        """Write a structured result (data/<name>.json)."""
        data = json.dumps(_plain(value), indent=2, allow_nan=False) + "\n"
        return self._write(name, "data", f"data/{name}.json", data.encode(), metadata=metadata)

    def add_text(self, name: str, text: str, metadata: Mapping | None = None) -> Path:
        """Write human-readable text (text/<name>.txt); keep scientific values in entries, tables, or data."""
        return self._write(name, "text", f"text/{name}.txt", str(text).encode("utf-8"), metadata=metadata)

    # ---------------------------------------------------------------- figures

    def add_figure(self, name: str, figure: Any, metadata: Mapping | None = None, *args, svg: bool = False,
                   dpi: int = 160, **kwargs) -> Path:
        """Save a Matplotlib figure (figures/<name>.png, optional .svg) and close it.

        Pass the plotting function itself (`add_figure("hist", error_histogram, None, errors)`): it is called with
        the remaining arguments, and where the figure was drawn is recorded as well as where it was logged."""
        return self._figure(name, figure, metadata, args, kwargs, svg, dpi)

    def _figure(self, name, figure, metadata, args, kwargs, svg, dpi) -> Path:
        producer = None
        if callable(figure) and not hasattr(figure, "savefig"):
            producer = self._code_location(figure)
            figure = figure(*args, **kwargs)
        if not hasattr(figure, "savefig"):
            raise TypeError(f"figure {name!r} is not a Matplotlib figure")
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", dpi=dpi, bbox_inches="tight")
        path = self._write(name, "figure", f"figures/{name}.png", buffer.getvalue(), producer=producer, metadata=metadata)
        if svg:
            vector = io.BytesIO()
            figure.savefig(vector, format="svg", bbox_inches="tight")
            self._write(f"{name}.svg", "figure", f"figures/{name}.svg", vector.getvalue(), producer=producer, metadata=metadata)
        import matplotlib.pyplot as plt

        plt.close(figure)
        return path

    def add_plotly(self, name: str, spec: Any, metadata: Mapping | None = None) -> Path:
        """Save a Plotly figure (object or {data, layout, config} dict) as figures/<name>.plotly.json."""
        spec = spec.to_plotly_json() if hasattr(spec, "to_plotly_json") else spec
        if not isinstance(spec, Mapping) or "data" not in spec:
            raise TypeError(f"plotly figure {name!r} needs a 'data' entry")
        spec = {key: _plain(spec[key]) for key in ("data", "layout", "config") if key in spec}
        data = json.dumps(spec, allow_nan=False) + "\n"
        return self._write(name, "plotly", f"figures/{name}.plotly.json", data.encode(), metadata=metadata)

    def add_image(self, name: str, image: Any, metadata: Mapping | None = None) -> Path:
        """Save an image array (H×W or H×W×C) or copy an existing image file (images/<name>.*)."""
        if isinstance(image, (str, Path)):
            source = Path(image)
            return self._write(name, "image", f"images/{name}{source.suffix.lower()}", source.read_bytes(), metadata=metadata)
        import matplotlib.pyplot as plt

        buffer = io.BytesIO()
        plt.imsave(buffer, image, format="png")
        return self._write(name, "image", f"images/{name}.png", buffer.getvalue(), metadata=metadata)

    def add_video(self, name: str, video: Any, metadata: Mapping | None = None, fps: float = 30) -> Path:
        """Copy a video file (MP4, WebM, MOV) or encode frames (T×H×W or T×H×W×C; uint8, or floats in [0, 1]) as an
        H.264 MP4 that browsers play and seek (videos/<name>.mp4; frames need `imageio[ffmpeg]`)."""
        if isinstance(video, (str, Path)):
            source = Path(video)
            return self._write(name, "video", f"videos/{name}{source.suffix.lower()}", source.read_bytes(), metadata=metadata)
        try:
            import imageio.v3 as iio
        except ImportError as error:
            raise ImportError("add_video with frames needs imageio and imageio-ffmpeg "
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
        return self._write(name, "video", f"videos/{name}.mp4", data, metadata=metadata)

    # ------------------------------------------------------- files and folders

    def add_file(self, path: Path | str | bytes, name: str | None = None, metadata: Mapping | None = None) -> Path:
        """Copy any other file (a model, an archive) to files/<name>; `name` defaults to the file's name, and gets
        the file's suffix when it has none. Bytes need a `name` with a suffix (`add_file(data, "model.pt")`)."""
        if isinstance(path, (bytes, bytearray, memoryview)):
            if not name or "." not in name:
                raise ValueError("add_file with bytes needs a name with a suffix, e.g. name='model.pt'")
            data, target = bytes(path), name
        else:
            source = Path(path)
            if not source.is_file():
                raise ValueError(f"add_file: {source} is not a file (add_dir copies a folder)")
            suffix = "".join(source.suffixes)
            target = name or source.name
            if suffix and "." not in target:
                target += suffix
            data = source.read_bytes()
        stem = target.split(".", 1)[0] or target
        return self._write(stem, "file", f"files/{target}", data, metadata=metadata, check=target)

    def add_dir(self, path: Path | str, name: str | None = None, metadata: Mapping | None = None) -> Path:
        """Copy a folder (a checkpoint directory, a dataset shard) to files/<name>/ (`name` defaults to its name)."""
        source = Path(path)
        if not source.is_dir():
            raise ValueError(f"add_dir: {source} is not a folder")
        name = name or source.name
        self._check(name)
        relative = f"files/{name}"
        target = self.root / relative
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, symlinks=True)
        entry = {"name": name, "kind": "dir", "path": relative, "time": _now(), "origin": self._origin()}
        clean = _metadata(metadata, f"dir {name!r}")
        if clean is not None:
            entry["metadata"] = clean
        _append(self.root / ARTIFACTS, entry)
        return target

    # ---------------------------------------------------------------- helpers

    def _check(self, name: str) -> None:
        if not NAME.fullmatch(name):
            raise ValueError(
                f"log name {name!r} must match {NAME.pattern} (lowercase, no slashes)"
            )

    def _write(self, name: str, kind: str, relative: str, data: bytes, producer: dict | None = None,
               metadata: Mapping | None = None, check: str | None = None) -> Path:
        self._check(check or name)
        entry = {"name": name, "kind": kind, "path": relative, "time": _now(), "origin": self._origin()}
        if producer:
            entry["producer"] = producer
        clean = _metadata(metadata, f"{kind} {name!r}")
        if clean is not None:
            entry["metadata"] = clean
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
            if _RESOLVED[name] not in _OWN_FILES:
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


class RunLogger(Writer):
    """The writer on a plain folder. The method names of earlier releases still work for one release, with a
    DeprecationWarning, and write where they did (`text` into logs/, `artifact` into artifacts/)."""

    def _deprecated(self, old: str, new: str) -> None:
        warnings.warn(f"RunLogger.{old}() is deprecated; use {new}", DeprecationWarning, stacklevel=3)

    def scalar(self, name: str, value: Number, step: int | None = None) -> None:
        self._deprecated("scalar", "add({name: value, 'step': step})")
        self._check(name)
        number = _plain(value)
        if isinstance(number, bool) or not isinstance(number, Number):
            raise TypeError(f"scalar {name!r} must be a number, got {type(value).__name__}")
        self.add({name: number} if step is None else {name: number, "step": step})

    def scalars(self, values: Mapping[str, Number], step: int | None = None) -> None:
        self._deprecated("scalars", "add({...})")
        for name, value in values.items():
            self._check(name)
            number = _plain(value)
            if isinstance(number, bool) or not isinstance(number, Number):
                raise TypeError(f"scalar {name!r} must be a number, got {type(value).__name__}")
        entry = {name: _plain(value) for name, value in values.items()}
        self.add(entry if step is None else {**entry, "step": step})

    def table(self, name: str, rows: Iterable[Mapping[str, Any]]) -> Path:
        self._deprecated("table", "add_table")
        return self.add_table(name, rows)

    def json(self, name: str, value: Any) -> Path:
        self._deprecated("json", "add_json")
        return self.add_json(name, value)

    def figure(self, name: str, figure: Any, *args, svg: bool = False, dpi: int = 160, **kwargs) -> Path:
        self._deprecated("figure", "add_figure(name, figure, metadata, *args)")
        return self._figure(name, figure, None, args, kwargs, svg, dpi)

    def plotly(self, name: str, figure: Any) -> Path:
        self._deprecated("plotly", "add_plotly")
        return self.add_plotly(name, figure)

    def image(self, name: str, image: Any) -> Path:
        self._deprecated("image", "add_image")
        return self.add_image(name, image)

    def video(self, name: str, video: Any, fps: float = 30) -> Path:
        self._deprecated("video", "add_video")
        return self.add_video(name, video, fps=fps)

    def artifact(self, name: str, source: Path | str | bytes, suffix: str | None = None) -> Path:
        self._deprecated("artifact", "add_file")
        if isinstance(source, (str, Path)):
            source = Path(source)
            if not source.is_file():
                raise ValueError(f"artifact {name!r}: {source} is not a file (archive directories first)")
            data, suffix = source.read_bytes(), suffix or "".join(source.suffixes)
        else:
            data = bytes(source)
            if not suffix:
                raise ValueError(f"artifact {name!r} from bytes needs a suffix, e.g. suffix='.pt'")
        return self._write(name, "artifact", f"artifacts/{name}{suffix}", data)

    def text(self, name: str, content: str) -> Path:
        self._deprecated("text", "add_text")
        return self._write(name, "log", f"logs/{name}.txt", content.encode("utf-8"))
