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

import fnmatch
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from importlib import metadata
from pathlib import Path

from hydra.core.hydra_config import HydraConfig
from hydra.experimental.callback import Callback
from hydra.utils import get_original_cwd

SOURCE_SUFFIXES = {
    # code
    ".bash",
    ".c",
    ".cc",
    ".cjs",
    ".cpp",
    ".css",
    ".cu",
    ".cuh",
    ".frag",
    ".glsl",
    ".h",
    ".hpp",
    ".htm",
    ".html",
    ".jl",
    ".js",
    ".jsx",
    ".metal",
    ".mjs",
    ".py",
    ".pyi",
    ".r",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".ts",
    ".tsx",
    ".vert",
    ".zsh",
    # configuration and documentation
    ".cfg",
    ".cmake",
    ".ini",
    ".json",
    ".md",
    ".rst",
    ".tex",
    ".bib",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SOURCE_NAMES = {
    "makefile",
    "dockerfile",
    "cmakelists.txt",
    ".gitignore",
    ".gitattributes",
}
DENIED_PARTS = {
    ".git",
    ".pixi",
    ".venv",
    "__pycache__",
    "generated",
    "mlruns",
    "node_modules",
    "outputs",
    "runs",  # run folders (outputs_dir), which are evidence, not source
    "site-packages",
    "venv",
    ".ssh",
    ".aws",
    ".gnupg",
    ".azure",
    ".kube",
}
# Credential-like files never enter a snapshot, whatever their suffix.
DENIED_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    "credentials",
    "credentials.*",
    "*credentials*.json",
    "secret",
    "secret.*",
    "secrets",
    "secrets.*",
    "*.secret",
    "*.secrets",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "service-account*.json",
    "*-service-account.json",
    "*.token",
    "token.json",
    "auth.json",
    ".htpasswd",
)
SAFE_ENVIRONMENT = (
    "CUDA_VISIBLE_DEVICES",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "PYTHONHASHSEED",
    "SLURM_JOB_ID",
    "SLURM_ARRAY_TASK_ID",
)


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _denied(relative: Path) -> bool:
    name = relative.name.lower()
    return any(part in DENIED_PARTS for part in relative.parts) or any(
        fnmatch.fnmatch(name, pattern) for pattern in DENIED_PATTERNS
    )


def _source_type(relative: Path) -> bool:
    return (
        relative.suffix.lower() in SOURCE_SUFFIXES
        or relative.name.lower() in SOURCE_NAMES
    )


def _select(paths, root: Path, max_file_bytes: int, skipped: list[dict]) -> list[Path]:
    """Eligible source files. Oversized source fails; other exclusions are recorded."""
    selected = []
    for path in paths:
        relative = path.relative_to(root)
        if (
            path.is_symlink()
            or not path.is_file()
            or _denied(relative)
            or not _source_type(relative)
        ):
            continue
        size = path.stat().st_size
        if size > max_file_bytes:
            raise RuntimeError(
                f"source file {relative} is {size} bytes; per-file limit is {max_file_bytes}. "
                "Raise max_file_bytes or add a deny rule for it"
            )
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            skipped.append({"path": str(relative), "reason": "not UTF-8 text"})
            continue
        selected.append(path)
    return sorted(selected)


def _working_files(root: Path) -> list[Path]:
    try:
        names = _git(
            root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
        ).split(b"\0")
        return [root / os.fsdecode(name) for name in names if name]
    except (FileNotFoundError, subprocess.CalledProcessError):
        return list(root.rglob("*"))


def _check_total(total: int, max_total_bytes: int, what: str) -> None:
    if total > max_total_bytes:
        raise RuntimeError(f"{what} is {total} bytes; total limit is {max_total_bytes}")


def create_snapshot(
    root: Path, destination: Path, mode: str, max_file_bytes: int, max_total_bytes: int
) -> None:
    """Write a bounded source snapshot into `destination` (absent or empty)."""
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise RuntimeError(f"snapshot destination is not empty: {destination}")
    skipped: list[dict] = []
    files = _select(_working_files(root), root, max_file_bytes, skipped)
    working_total = sum(path.stat().st_size for path in files)
    manifest: dict[str, object] = {
        "mode": mode,
        "source_root": str(root),
        "working_files": [str(path.relative_to(root)) for path in files],
        "working_total_bytes": working_total,
    }

    if mode == "tree":
        _check_total(working_total, max_total_bytes, "eligible working source")
        for path in files:
            target = destination / "tree" / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    elif mode == "git":
        try:
            commit = _git(root, "rev-parse", "HEAD").decode().strip()
            tracked = {
                os.fsdecode(name)
                for name in _git(
                    root, "ls-tree", "-r", "--name-only", "-z", commit
                ).split(b"\0")
                if name
            }
        except (FileNotFoundError, subprocess.CalledProcessError) as error:
            raise RuntimeError(
                "git mode requires a repository with a commit"
            ) from error

        committed: list[tuple[str, bytes]] = []
        for relative in sorted(tracked):
            if _denied(Path(relative)) or not _source_type(Path(relative)):
                continue
            payload = _git(root, "show", f"{commit}:{relative}")
            if len(payload) > max_file_bytes:
                raise RuntimeError(
                    f"committed source file {relative} is {len(payload)} bytes; per-file limit is {max_file_bytes}"
                )
            try:
                payload.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append({"path": relative, "reason": "not UTF-8 text"})
                continue
            committed.append((relative, payload))
        untracked = [
            path for path in files if str(path.relative_to(root)) not in tracked
        ]
        relevant = sorted(
            {relative for relative, _ in committed}
            | {str(p.relative_to(root)) for p in files}
        )
        patch = (
            _git(root, "diff", "--no-ext-diff", "--no-color", "HEAD", "--", *relevant)
            if relevant
            else b""
        )
        committed_total = sum(len(payload) for _, payload in committed)
        untracked_total = sum(path.stat().st_size for path in untracked)
        # Each byte is stored once: committed tree, untracked copies, and the patch.
        _check_total(
            committed_total + untracked_total + len(patch),
            max_total_bytes,
            "git source snapshot",
        )

        with tarfile.open(destination / "git-tree.tar.gz", "w:gz") as archive:
            for relative, payload in committed:
                info = tarfile.TarInfo(relative)
                info.size = len(payload)
                info.mtime = 0
                archive.addfile(info, io.BytesIO(payload))
        (destination / "working-tree.patch").write_bytes(patch)
        for path in untracked:
            target = destination / "untracked" / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        manifest.update(
            commit=commit,
            committed_total_bytes=committed_total,
            untracked_total_bytes=untracked_total,
            patch_bytes=len(patch),
        )
    else:
        raise ValueError("source snapshot mode must be 'git' or 'tree'")

    manifest["skipped"] = [
        dict(entry)
        for entry in sorted({tuple(sorted(item.items())) for item in skipped})
    ]
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


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
    packages = sorted(
        {
            f"{dist.metadata['Name']}=={dist.version}"
            for dist in metadata.distributions()
            if dist.metadata["Name"]
        },
        key=str.lower,
    )
    (target / "pip-freeze.txt").write_text("\n".join(packages) + "\n", encoding="utf-8")
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
