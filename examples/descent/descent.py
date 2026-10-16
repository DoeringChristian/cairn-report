"""Gradient descent on a quadratic: the experiment of the example project.

    python descent.py                      # a run of runs/descent, and its result, the artifact `descent`
    python descent.py --learning-rate 0.08

The process is a run (`cairn.init`): a version of `runs/descent` that records the config, what it used (the
dataset `datasets/quadratic`, at its exact version), the loss curve (`cairn.run.add`), its output, code, packages, and
the machine. The result goes into a draft, a fresh folder Cairn manages (in .cairn/drafts/), which becomes
`descent@v<n>` when the block ends, with the run as its input. The run completes when the script exits. A plain
script on purpose: Cairn needs no framework.
"""

import argparse
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


def main(config: dict) -> "cairn.Version":
    cairn.init(name="descent", config=config)                  # runs/descent@v<n>, running until the script exits
    data = cairn.use(config["dataset"])                         # an input of the run, at its exact version
    problem = json.loads((data.path / "problem.json").read_text())
    A, x, rate = np.array(problem["A"]), np.array(problem["x0"]), config["learning_rate"]
    points = [x.copy()]
    for step in range(config["steps"]):
        x = x - rate * A @ x
        points.append(x.copy())
        cairn.run.add({"loss": float(0.5 * x @ A @ x), "step": step})  # the loss curve, in the run's metrics.jsonl
    loss = float(0.5 * x @ A @ x)
    print(f"final loss {loss:.3e} after {config['steps']} steps")  # also in the run's logs/stdout.txt
    with cairn.draft("descent") as result:                      # the result artifact: an output of the run
        result.add_json("final", {"loss": loss, "steps": config["steps"], "learning_rate": rate}, {"dataset": data.ref})
        result.add_figure("trajectory", trajectory, {"steps": config["steps"]}, np.array(points), A)
    print(f"published {result.version.ref} from {data.ref}")    # leaving the block published it, removed its folder
    return result.version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", default="datasets/quadratic@latest")
    parser.add_argument("--learning-rate", type=float, default=0.15)
    parser.add_argument("--steps", type=int, default=60)
    args = parser.parse_args()
    main({"dataset": args.dataset, "learning_rate": args.learning_rate, "steps": args.steps})
