"""Gradient descent on a quadratic: the experiment of the example project.

    python descent.py            # publishes the run as the next version of the artifact `descent`

It pulls the problem from the artifact `datasets/quadratic` and writes its results into a draft: a fresh
folder Cairn manages (in .cairn/drafts/), which becomes `descent@v<n>` when the block ends, with the
exact dataset version it pulled as its input. The draft is also the run logger. A plain script on purpose:
Cairn needs no framework.
"""

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

import cairn  # noqa: E402


def trajectory(points: np.ndarray, A: np.ndarray):
    """The iterates on the loss contours."""
    fig, ax = plt.subplots(figsize=(6, 4))
    u, v = np.meshgrid(np.linspace(-3, 3, 200), np.linspace(-2, 2, 200))
    ax.contour(u, v, 0.5 * (A[0, 0] * u**2 + A[1, 1] * v**2), levels=20, cmap="Greys", linewidths=0.6)
    ax.plot(points[:, 0], points[:, 1], "o-", ms=3, color="#176b5b")
    ax.set(xlabel="$x_1$", ylabel="$x_2$", title="Gradient descent on a quadratic")
    return fig


def main() -> "cairn.Version":
    with cairn.draft("descent") as run:
        problem = json.loads((cairn.pull("datasets/quadratic@latest") / "problem.json").read_text())  # an input of the run
        A, x, rate = np.array(problem["A"]), np.array(problem["x0"]), problem["learning_rate"]
        run.set_metadata(config={"learning_rate": rate, "steps": problem["steps"]})
        points = [x.copy()]
        for step in range(problem["steps"]):
            x = x - rate * A @ x
            points.append(x.copy())
            run.scalars({"loss": float(0.5 * x @ A @ x)}, step=step)
        run.json("final", {"loss": float(0.5 * x @ A @ x), "steps": problem["steps"], "learning_rate": rate})
        run.figure("trajectory", trajectory, np.array(points), A)
    print(f"published {run.version.ref}")  # leaving the block published the draft and removed its folder
    return run.version


if __name__ == "__main__":
    main()
