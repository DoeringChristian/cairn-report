# Example: dataset → run → report

A small, complete Cairn project: a dataset artifact, an experiment that pulls it, and a report
that reads the experiment's results, each published as an artifact with its lineage recorded.
No server is needed; everything lives in a local store under `.cairn/`.

```text
descent/
├── cairn.toml                     the project ([store] project; a local store by default)
├── data/quadratic/problem.json    the dataset (a matrix, a start point, a step size)
├── descent.py                     the experiment: pulls the dataset, writes into a draft that becomes descent@v<n>
├── reports/descent-study/report.md   the report: declares its inputs, reads only them
└── run_example.py                 every step below, in Python
```

Run it with Cairn installed (`pip install "cairn[ui] @ git+https://github.com/DoeringChristian/cairn-report.git"`
plus matplotlib):

```bash
python run_example.py      # dataset, run, report, then the run's lineage
cairn ui                   # projects, versions, files, the lineage graph, the report
```

`run_example.py` is the whole flow in Python:

```python
store = cairn.connect()                                       # the store cairn.toml names: .cairn/store
project = store.project()                                     # descent-example
project.artifact("datasets/quadratic").publish("data/quadratic")   # datasets/quadratic@v1
descent.main()                                                # descent@v1, with datasets/quadratic@v1 as its input
cairn.reports.publish("descent-study")                        # reports/descent-study@v1, pinned to both
latest = project.artifact("descent").latest
latest.inputs, latest.consumers                               # the lineage, one level up and down
```

and `descent.py` writes its results straight into a draft, a fresh folder in `.cairn/drafts/` that
is also the run logger; leaving the block publishes it (and removes the folder), with the dataset
version it pulled recorded as its input:

```python
with cairn.draft("descent") as run:
    problem = json.loads((cairn.pull("datasets/quadratic@latest") / "problem.json").read_text())
    ...
    run.scalars({"loss": loss}, step=step)
    run.json("final", {...})
    run.figure("trajectory", trajectory, points, A)
```

The same steps from the command line: `cairn publish data/quadratic datasets/quadratic`,
`python descent.py`, `cairn publish reports/descent-study`, `cairn show descent@latest`.

Things to try:

- **A new dataset version.** Change `learning_rate` in `data/quadratic/problem.json` to 0.08 and run
  `python run_example.py` again. The dataset becomes v2, the run v2 with v2 as its input, and the
  UI's lineage graph shows both chains.
- **An alias.** `cairn alias descent@v1 best`, then declare `descent: descent@best` in the
  report: the report pins whichever version `best` points to at build time.
- **A failed run.** Raise an exception inside the `with` block of `descent.py`: nothing is published,
  and the folder stays for inspection (`cairn drafts` lists it; `cairn drafts discard <uid>`).
- **Strictness.** Add `open("data/quadratic/problem.json")` to a `python exec` block of the report:
  the build fails, because the report did not declare that file as an input.
- **A server.** `cairn server ~/cairn-store` in another terminal (it prints a token and starts the
  UI), then add `url = "http://127.0.0.1:8800"` under `[store]` here and run again.
