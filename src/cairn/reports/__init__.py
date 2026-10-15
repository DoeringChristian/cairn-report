"""Reports: Markdown to static pages (build.py), their local preview (serve.py), one self-contained page
(share.py), and publishing a report as an artifact (publishing.py)."""


from pathlib import Path as _Path


def _config(project):
    from ..config import load_config

    return load_config(project)


def build_all(slug: str | None = None, *, project: "str | _Path | None" = None):
    """Build every report of the project (or one, by slug); returns the builds (see `build.Build`)."""
    from .build import build_all as _build_all

    return _build_all(_config(project), slug)


def publish(report: "str | _Path", *, project: "str | _Path | None" = None, quiet: bool = True) -> int | None:
    """Build a report and store it as the next version of the artifact `reports/<slug>`, with its
    pinned inputs. `report` is a slug (`"descent-study"`) or the report folder. Returns the version
    number (the latest one when nothing changed), or None when it is queued for an unreachable server."""
    from .publishing import publish_report

    config = _config(project)
    folder = _Path(report)
    if not folder.is_dir() or not (folder / "report.md").is_file():
        folder = config.reports_dir / str(report)
    if not (folder / "report.md").is_file():
        raise FileNotFoundError(f"no report {report!r}: expected {folder / 'report.md'}")
    return publish_report(config, folder, quiet=quiet)
