"""Gradient descent on a quadratic: the experiment of the example project.

    python descent.py            # writes runs/descent/<timestamp>/ and points runs/descent/latest at it

It pulls the problem from the artifact `datasets/quadratic`, so the run folder records the exact
version it read (`.cairn-inputs.jsonl`), and `cairn publish runs/descent/latest descent` carries
that along as the run's input. A plain script on purpose: Cairn needs no framework.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

import cairn  # noqa: E402
from cairn.runs.logger import RunLogger  # noqa: E402


def trajectory(points: np.ndarray, A: np.ndarray):
    """The iterates on the loss contours."""
    fig, ax = plt.subplots(figsize=(6, 4))
    u, v = np.meshgrid(np.linspace(-3, 3, 200), np.linspace(-2, 2, 200))
    ax.contour(u, v, 0.5 * (A[0, 0] * u**2 + A[1, 1] * v**2), levels=20, cmap="Greys", linewidths=0.6)
    ax.plot(points[:, 0], points[:, 1], "o-", ms=3, color="#176b5b")
    ax.set(xlabel="$x_1$", ylabel="$x_2$", title="Gradient descent on a quadratic")
    return fig


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    run = Path("runs/descent") / stamp
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"status": "running", "name": "descent", "started": stamp}))
    os.environ["CAIRN_INPUTS"] = str(run / ".cairn-inputs.jsonl")  # record what this run reads in its own folder

    problem = json.loads((cairn.pull("datasets/quadratic@latest") / "problem.json").read_text())
    A, x, rate = np.array(problem["A"]), np.array(problem["x0"]), problem["learning_rate"]

    log, points = RunLogger(run), [x.copy()]
    for step in range(problem["steps"]):
        x = x - rate * A @ x
        points.append(x.copy())
        log.scalars({"loss": float(0.5 * x @ A @ x)}, step=step)
    log.json("final", {"loss": float(0.5 * x @ A @ x), "steps": problem["steps"], "learning_rate": rate})
    log.figure("trajectory", trajectory, np.array(points), A)
    log.close()

    (run / "run.json").write_text(json.dumps({"status": "completed", "name": "descent", "started": stamp}))
    latest = run.parent / "latest"
    latest.unlink(missing_ok=True)
    latest.symlink_to(stamp)
    print(run)


if __name__ == "__main__":
    main()
