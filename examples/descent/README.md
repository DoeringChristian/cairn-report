# Example: dataset → run → result → report

A small, complete Cairn project: a dataset artifact, an experiment that uses it, and a report that
reads the experiment's result and its loss curve, each published as an artifact with its lineage
recorded. No server is needed; everything lives in a local store under `.cairn/`.

```text
descent/
├── cairn.toml                     the project ([store] project; a local store by default)
├── data/quadratic/problem.json    the dataset (a matrix and a start point)
├── descent.py                     the experiment: a run of runs/descent that publishes the result `descent`
├── reports/descent-study/report.md   the report: declares its inputs, reads only them
└── run_example.py                 every step below, in Python
```

Run it with Cairn installed (`pip install "cairn[ui] @ git+https://github.com/DoeringChristian/cairn-report.git"`
plus matplotlib):

```bash
python run_example.py      # dataset, run, result, report, then the result's lineage
cairn ui                   # projects, runs (with their charts and logs), versions, files, the lineage graph, the report
```

`descent.py` is a run: `cairn.init` makes the process a version of `runs/descent` (running until the
script exits), `cairn.use` records the dataset's exact version as the run's input, `run.add` logs the
loss curve into the run, and the result goes into a draft, which becomes `descent@v<n>` with the run
as its one input:

```python
run = cairn.init(name="descent", config=config)            # runs/descent@v<n>
data = cairn.use(config["dataset"])                         # datasets/quadratic@v1, an input of the run
problem = json.loads((data.path / "problem.json").read_text())
for step in range(config["steps"]):
    ...
    run.add({"loss": loss, "step": step})                   # the run's metrics.jsonl
with cairn.draft("descent") as result:                      # the result artifact, an output of the run
    result.add_json("final", {...}, {"dataset": data.ref})
    result.add_figure("trajectory", trajectory, {"steps": ...}, points, A)
```

The run also records its config, its output (`logs/stdout.txt`), the code (`code/`), the packages
(`environment/`), and the machine (`system/`). The report declares the dataset, the result, and the run
(`runs/descent@latest`, whose `metrics.jsonl` it reads with `read_series`).

`run_example.py` is the whole flow; it runs `descent.py` as its own process, so the run completes when
that script exits:

```python
project.artifact("datasets/quadratic").publish("data/quadratic")   # datasets/quadratic@v1
subprocess.run([sys.executable, "descent.py"], check=True)          # runs/descent@v1 and descent@v1
cairn.reports.publish("descent-study")                              # reports/descent-study@v1, pinned to all three
result = project.artifact("descent").latest
result.run, result.upstream(), result.consumers                     # its run, the data behind it, the report
```

The same steps from the command line: `cairn publish data/quadratic datasets/quadratic`,
`python descent.py`, `cairn publish reports/descent-study`, `cairn show descent@latest` (it prints the
run and the data behind it), `cairn show runs/descent@latest`.

Things to try:

- **Another config.** `python descent.py --learning-rate 0.08`: a new run (`runs/descent@v2`, its config
  in its metadata) and a new result with it as input. The UI's lineage graph shows both; "Data only"
  hides the runs.
- **A new dataset version.** Change `x0` in `data/quadratic/problem.json` and run `python run_example.py`
  again: the dataset becomes v2, and the next run uses it.
- **An alias.** `cairn alias descent@v1 best`, then declare `descent: descent@best` in the report: the
  report pins whichever version `best` points to at build time.
- **A failed run.** Raise an exception in `descent.py` after the loop: the run is marked failed (its
  traceback in `logs/error.txt`), and its folder stays for inspection (`cairn drafts`).
- **Strictness.** Add `open("data/quadratic/problem.json")` to a `python exec` block of the report:
  the build fails, because the report did not declare that file as an input.
- **A server.** `cairn server ~/cairn-store` in another terminal (it prints a token and starts the
  UI), then add `url = "http://127.0.0.1:8800"` under `[store]` here and run again: the run is synced
  while it runs, so the UI shows it live.
