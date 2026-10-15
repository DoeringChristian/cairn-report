"""The whole example in Python: dataset -> run -> result -> report, each published as an artifact, then the lineage.

    python run_example.py

Run it from anywhere with Cairn installed (with matplotlib); everything lands in .cairn/ next to this file.
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)  # the project root (cairn.toml): its repo (.cairn/), drafts, and reports are found from here

import cairn  # noqa: E402
import cairn.reports  # noqa: E402


def main() -> None:
    repo = cairn.connect()                          # the project's repo, .cairn/ (no remote: it numbers the versions)
    project = repo.project()                        # descent-example

    dataset = project.artifact("datasets/quadratic").publish("data/quadratic")
    print(f"published {dataset.ref}", flush=True)   # unchanged on a second run: the same version

    # the experiment is its own process, so it is its own run: it completes when the script exits
    subprocess.run([sys.executable, "descent.py"], check=True)

    number = cairn.reports.publish("descent-study")  # builds the report, stores it pinned to its inputs
    print(f"published {project.name}:reports/descent-study@v{number}")

    result = project.artifact("descent").latest
    run = result.run                                # the run the result came from
    print(f"\n{result.ref}")
    print(f"  run        {run.ref} ({run.status}, config {run.metadata['config']})")
    print(f"  data       {', '.join(v.ref for v in result.upstream()) or '-'}")
    print(f"  consumers  {', '.join(result.consumers) or '-'}")
    print(f"  run files  {', '.join(sorted({f.path.split('/')[0] for f in run.files}))}")
    print("\nExplore it: cairn ui   (then open the printed address)")


if __name__ == "__main__":
    main()
