"""The whole example in Python: dataset -> run -> report, each published as an artifact, then the lineage.

    python run_example.py

Run it from anywhere with Cairn installed (with matplotlib); everything lands in .cairn/ next to this file.
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)  # the project root (cairn.toml): the store, drafts, and reports are found from here
sys.path.insert(0, str(HERE))

import cairn  # noqa: E402
import cairn.reports  # noqa: E402

import descent  # noqa: E402


def main() -> None:
    store = cairn.connect()                         # the store [store] in cairn.toml names: a local one in .cairn/store
    project = store.project()                       # descent-example

    dataset = project.artifact("datasets/quadratic").publish("data/quadratic")
    print(f"published {dataset.ref}")               # unchanged on a second run: the same version

    run = descent.main()                            # pulls the dataset, writes into a draft, publishes descent@v<n>

    number = cairn.reports.publish("descent-study")  # builds the report, stores it pinned to both inputs
    print(f"published {project.name}:reports/descent-study@v{number}")

    latest = project.artifact("descent").latest
    print(f"\n{latest.ref} ({run.label})")
    print(f"  inputs     {', '.join(latest.inputs) or '-'}")
    print(f"  consumers  {', '.join(latest.consumers) or '-'}")
    print("\nExplore it: cairn ui   (then open the printed address)")


if __name__ == "__main__":
    main()
