# Example: dataset → run → result → report

A small, complete Cairn project: a dataset artifact, an experiment that uses it, and a report that
reads the experiment's result and its loss curve, each published as an artifact with its lineage
recorded. No server is needed; everything lives in the project's repo, `.cairn/`.

```text
descent/
├── cairn.toml                     the project ([repo] project; a repo without a remote)
├── data/quadratic/problem.json    the dataset (a matrix and a start point)
├── descent.py                     the experiment: a run of runs/descent that publishes the result `descent`
├── reports/descent-study/report.md   the report: declares its inputs, reads only them
```

Run it with Cairn installed (`pip install "cairn[ui] @ git+https://github.com/DoeringChristian/cairn.git"`
plus matplotlib):

```bash
cd examples/descent                                      # its own repo: examples/descent/.cairn/
cairn publish data/quadratic datasets/quadratic          # datasets/quadratic@v1
python descent.py                                        # runs/descent@v1 and its result descent@v1
cairn publish reports/descent-study                      # reports/descent-study@v1, pinned to its inputs
cairn show descent@latest                                # the result, its run, and the data behind it
cairn ui                                                 # projects, runs (charts, logs), versions, files, lineage, the report
```

Run these from this folder: Cairn uses the repo of the folder it runs in (the nearest `.cairn/` or
`cairn.toml` upward), so from elsewhere the commands would use another repo.

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

Things to try:

- **Another config.** `python descent.py --learning-rate 0.08`: a new run (`runs/descent@v2`, its config
  in its metadata) and a new result with it as input. The UI's lineage graph shows both; "Data only"
  hides the runs.
- **A new dataset version.** Change `x0` in `data/quadratic/problem.json` and publish it
  again (`cairn publish data/quadratic datasets/quadratic`): the dataset becomes v2, and the next
  `python descent.py` uses it.
- **An alias.** `cairn alias descent@v1 best`, then declare `descent: descent@best` in the report: the
  report pins whichever version `best` points to at build time.
- **A failed run.** Raise an exception in `descent.py` after the loop: the run is marked failed (its
  traceback in `logs/error.txt`), and its folder stays for inspection (`cairn drafts`).
- **Strictness.** Add `open("data/quadratic/problem.json")` to a `python exec` block of the report:
  the build fails, because the report did not declare that file as an input.
- **A remote.** `cairn server ~/cairn-remote` in another terminal (it prints a token and starts the
  UI), then add `remote = "http://127.0.0.1:8800"` under `[repo]` here, `export CAIRN_TOKEN=<token>`, and
  run again: the versions this repo numbered itself are sent first (the remote numbers them), and the run is
  synced while it runs, so the remote's UI shows it live. Stop the server and run again: everything still
  works, the new versions wait as pending (`cairn status`) and are sent by the next command once the server
  is back.
