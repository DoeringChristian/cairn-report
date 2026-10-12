---
title: Lattice reconstruction project
subtitle: Project overview · template example
status: Feasibility in progress
---

<!--
The overview (super-report) is the project's entry point. It has exactly three
sections, in this order: Introduction, Status, Research directions. It does not
follow the paper structure of the direction reports. Comments like this one are
guidance for the author and are removed when the report is built.
-->

## Introduction

<!--
Introduction: state the general target the whole project pursues, the one
problem that every research direction tries to solve. Explain why it matters,
what success would look like (the criteria or measures the directions are
judged by), and the constraints shared by all directions (data, budget, scope).
This is the jumping-off point for every direction: each direction report's own
Introduction builds on this text and links back to it instead of restating it.
Keep it short and stable; it changes only when the project's goal changes.
-->

The project aims to reconstruct lattice structure robustly from noisy
measurements: recovered geometry must stay accurate near boundaries without
introducing temporal artifacts. Every research direction proposes one way to
get closer to that target and is judged by the same criteria, the boundary
residual against a data-only reference and the absence of visible flicker.

## Status

<!--
Status: a status report across all directions, written for someone deciding
what to do next. Say where the project stands as a whole: which directions are
promising, stalled, contradicted, or finished, where they agree or conflict, and
what the next decision is (use a `decision` or `question` callout). Then list
proposed directions that do not have a report yet: for each, the idea, why it
might reach the target, and what would justify starting it. Cite direction
findings with `report:` links instead of repeating their evidence. Keep numbers
in the direction reports; this section summarizes and decides. Any result you
do state here (a rate, a breaking point, a comparison) comes from a {{ }} value
computed from run files or becomes a link to the finding; never type it.
-->

One direction is under way. [Local fitting](report:local-fitting) lowers the
boundary residual
([main finding](report:local-fitting#finding-local-fitting-improves-edge-alignment)),
but a temporal artifact is not yet calibrated, so no direction is ready for
tuning.

!!! decision "Next decision"
    Calibrate the temporal artifact score before choosing between directions or
    starting any parameter study.

Proposed directions:

- **Spike-robust preprocessing**: remove impulsive outliers before fitting; worth
  starting if the edge cases left by local fitting turn out to be outlier-driven.
- **Learned boundary prior**: replace the hand-set smoothness weight; worth
  starting only once a calibrated artifact score exists.

## Research directions

<!--
Research directions: the fence below lists every direction report with the
`summary` from its front matter, its status, and its finding counts. Leave the
fence empty to list all directions, or name slugs, one per line, to choose and
order them. Do not write direction summaries here; keep each one current in its
own report.
-->

```reports
```
