# Store server benchmark

`bench/bench_store.py` starts `cairn server` as a separate process on a fresh store in a temporary folder and
drives it over HTTP (see the script's docstring for each scenario). Run it with

    taskpolicy -b python bench/bench_store.py --json out.json

Machine: Apple M5 Pro (6 performance + 12 efficiency cores, 18 in all), macOS 26.6, APFS on the internal
SSD, Python 3.12.14. Everything ran under `taskpolicy -b` (background QoS: macOS schedules it mostly on the
efficiency cores and throttles its disk I/O), so these are background-priority numbers, lower than the
machine can do in the foreground; the machine was in use at the same time. Defaults: 200 publishes of 3
files x 1.2 KB from 32 clients in 4 processes, a 256 MiB (268 MB) random file, 1000 random 64 KiB ranges
and the metadata queries from 4 clients, 2000 artifacts x 3 versions (the last version of each consumes
another artifact, so lineage has consumers to find). MB is 10^6 bytes.

## Baseline: `http.server` (commit a9292bc)

| scenario | count | rate | p50 ms | p95 ms | notes |
|---|---:|---:|---:|---:|---|
| meta/artifacts-cold | 1 | 1.0 req/s | 962.76 | 962.76 | first request after start |
| publish/distinct | 200 | 201.0 pub/s | 120.54 | 240.33 | 32 clients, 4 procs, 3 files x 1 KB, 5 requests each |
| publish/same | 200 | 58.4 pub/s | 514.49 | 711.94 | 32 clients, 4 procs, 3 files x 1 KB, 5 requests each |
| upload | 1 | 233.9 MB/s | 1147.61 | 1147.61 | 268 MB |
| pull/cold | 1 | 1195.5 MB/s | 224.53 | 224.53 | 268 MB, median of 1 |
| pull/warm | 3 | 1024.1 MB/s | 262.12 | 265.07 | 268 MB, median of 3 |
| pull/client | 1 | 581.4 MB/s | 461.68 | 461.68 | RemoteStore.blob_file: download + verify + place |
| range | 1000 | 2349.3 req/s | 1.60 | 2.60 | 64 KB, 4 clients |
| meta/projects | 500 | 1.0 req/s | 3855.95 | 4272.02 | 4 clients, 0 KB answer |
| meta/artifacts | 40 | 1.1 req/s | 3732.85 | 4014.51 | 4 clients, 383 KB answer |
| meta/summary | 500 | 1166.5 req/s | 3.18 | 5.36 | 4 clients, 1 KB answer |
| meta/versions | 500 | 2925.7 req/s | 1.19 | 2.41 | 4 clients, 1 KB answer |
| meta/lineage | 500 | 185.2 req/s | 20.93 | 31.40 | 4 clients, 0 KB answer |
| meta/resolve | 500 | 2000.4 req/s | 1.76 | 3.34 | 4 clients, 0 KB answer |

What stands out: the project list and the artifact listing re-read and re-parse every artifact's
`versions.jsonl` and `aliases.jsonl` on every request (about 4 s each for 2000 artifacts, and they
serialize on the GIL); publishes to one artifact serialize on the store's single lock and each re-reads the
artifact's whole history; lineage scans `consumers.jsonl` per request. Downloads are already near
memory speed through the page cache.

The raw rows are in `baseline.json`.
