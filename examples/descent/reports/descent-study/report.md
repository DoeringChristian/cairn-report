---
title: Descent study
status: active
summary: Gradient descent reaches a loss of {{ f"{final['loss']:.2e}" }} on the quadratic from the dataset.
inputs:
  problem: datasets/quadratic@latest
  descent: descent@latest
  run: runs/descent@latest
---

```python exec hide
problem = read_json(inputs.problem / "problem.json")
final = read_json(inputs.descent / "data" / "final.json")
loss = read_series(inputs.run, "loss")
```

## Problem

The run minimizes $f(x) = \tfrac12 x^\top A x$ for a diagonal $A$ with entries {{ problem['A'][0][0] }}
and {{ problem['A'][1][1] }}, starting at $x_0$ = ({{ problem['x0'][0] }}, {{ problem['x0'][1] }}),
with step size {{ final['learning_rate'] }} for {{ final['steps'] }} steps. The problem comes from
the dataset artifact listed under Data above, the result from the result artifact, and the loss curve from
the run that made it; all three are pinned.

## Result

```python exec
metric("Final loss", final["loss"], "lower is better; after the last step", fmt=".2e")
metric("Steps", final["steps"], "full gradient steps")
```

```python exec hide
import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(7, 3.2), num="loss-curve")
ax.semilogy([row["step"] for row in loss], [row["value"] for row in loss], color="#176b5b")
ax.set(xlabel="step", ylabel="loss")
caption("The loss at every step, read from the run's metrics.jsonl.")
```

![Trajectory](input:descent/figures/trajectory.png "The iterates on the loss contours, as logged by the result.")
