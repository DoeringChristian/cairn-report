# Store server benchmark

`bench/bench_store.py` starts `cairn server` as a separate process on a fresh store in a temporary folder
and drives it over HTTP (each scenario is described in the script's docstring):

    taskpolicy -b python bench/bench_store.py --json out.json
    taskpolicy -b python bench/bench_store.py --server-src <worktree>/src     # an older server, same clients

**Machine:** Apple M5 Pro (6 performance + 12 efficiency cores, 18 in all), macOS 26.6, APFS on the internal
SSD, Python 3.12.14. **Everything ran under `taskpolicy -b`** (background QoS: macOS keeps the processes on
the efficiency cores and throttles their I/O) while the machine was in use, so absolute numbers are well
below what the machine does in the foreground, and single-sample rows (`count` 1) vary by about 10 %.
The client processes and the server share the machine.

**Workload (defaults):** 200 publishes of 3 files x 1.2 KB from 32 clients in 4 processes (each publish is
the client's full protocol: `blobs/missing`, 3 PUTs, `publish`); one 256 MiB (268 MB) random file; 1000
random 64 KiB ranges and the metadata queries from 4 clients; 2000 artifacts x 3 versions in one project,
the last version of each consuming another artifact (so lineage has consumers). Every client keeps one
keep-alive connection and sends bodies in 1 MB blocks. MB is 10^6 bytes. Cells: rate (p50 / p95 ms).

## Results

| scenario | baseline: http.server (a9292bc) | async + JSON Lines index (097df21) | async + SQLite (2459baa) | SQLite vs baseline |
|---|---:|---:|---:|---:|
| publish/distinct | 234 pub/s (115.8 / 192.0) | 295 pub/s (95.0 / 121.1) | 366 pub/s (68.2 / 118.7) | 1.56x |
| publish/same | 60 pub/s (524.5 / 692.4) | 274 pub/s (103.4 / 130.2) | 312 pub/s (78.2 / 131.1) | 5.19x |
| upload (268 MB) | 884 MB/s (303.7) | 928 MB/s (289.4) | 1168 MB/s (229.8) | 1.32x |
| pull/cold (first after restart) | 1051 MB/s (255.3) | 837 MB/s (320.6) | 707 MB/s (379.6) | 0.67x |
| pull/warm | 1094 MB/s (245.2 / 247.6) | 1068 MB/s (251.5 / 257.9) | 1045 MB/s (256.8 / 261.0) | 0.95x |
| pull/curl x1 | 1109 MB/s (242.0 / 243.9) | 1053 MB/s (255.0 / 257.7) | 1131 MB/s (237.4 / 252.5) | 1.02x |
| pull/curl x4 (aggregate) | 1168 MB/s (919.6 / 926.1) | 1396 MB/s (769.1 / 781.6) | 1380 MB/s (778.0 / 908.5) | 1.18x |
| pull/client (download + verify) | 568 MB/s (472.2) | 595 MB/s (450.9) | 515 MB/s (520.7) | 0.91x |
| range (64 KiB) | 2004 req/s (2.0 / 2.8) | 2757 req/s (1.3 / 2.1) | 2101 req/s (1.7 / 3.3) | 1.05x |
| meta/artifacts-cold (first request) | 0.9 req/s (1110.6) | 1.3 req/s (744.7) | 27 req/s (36.9) | 30x |
| meta/projects | 0.9 req/s (4219.7 / 4991.5) | 667 req/s (6.1 / 8.6) | 3292 req/s (1.0 / 2.1) | ~3600x |
| meta/artifacts (2000, 383 KB) | 0.9 req/s (4109.5 / 4517.2) | 554 req/s (6.9 / 9.0) | 1509 req/s (2.4 / 3.8) | ~1600x |
| meta/summary | 1437 req/s (2.6 / 4.3) | 3215 req/s (1.1 / 2.0) | 2102 req/s (1.7 / 3.5) | 1.46x |
| meta/versions | 2709 req/s (1.3 / 2.3) | 3653 req/s (1.0 / 1.7) | 2646 req/s (1.4 / 2.3) | 0.98x |
| meta/lineage | 198 req/s (19.5 / 30.9) | 2645 req/s (1.4 / 2.2) | 1316 req/s (2.7 / 5.0) | 6.6x |
| meta/resolve | 2331 req/s (1.5 / 2.9) | 3528 req/s (1.0 / 1.6) | 2185 req/s (1.6 / 3.0) | 0.94x |

The baseline column is the old server measured with the final bench script (`--server-src` on a worktree
of a9292bc: `baseline.json`; its large-file rows come from a second run of the same kind with `--only
large`, which added the curl rows and the server CPU). The middle column is the first rewrite (the async
server over the JSON Lines files with an in-memory index: `jsonl-index.json`), the right one the final
state (metadata in SQLite: `sqlite.json`). The first baseline, measured with the first version of the
script (8 KB upload blocks, client start-up inside the publish timing), is at the end.

Server CPU per GB sent (curl rows): 0.75 / 0.71 s/GB for the baseline, 0.81 / 0.81 s/GB for SQLite
(1 / 4 streams); server CPU per publish (bench-like run with 400 publishes, `ps` time): baseline 6.0 ms
(distinct) and 31.7 ms (same artifact: it re-read the artifact's whole history), SQLite 5.2-6.3 ms for both.

## What changed, and why the numbers move

- **Metadata queries (d)**: the old server re-read and re-parsed every artifact's `versions.jsonl` and
  `aliases.jsonl` for each project or artifact listing (about 4 s for 2000 artifacts) and scanned all of
  `consumers.jsonl` for lineage. Now the listing is one query over summary rows that every write keeps
  current in the same transaction, cached (with its serialized JSON) until the project's generation
  changes; the project list is one `GROUP BY`; lineage is an indexed lookup. Single-artifact reads
  (versions, resolve) were already cheap and stay where they were: they are bound by the per-request cost
  (HTTP parsing, one trip to a worker thread, a few indexed queries). The in-memory JSON Lines index
  answered those from memory and was faster there, but needed a change-hint file, periodic sweeps, and
  per-process staleness rules; the owner chose SQLite for its transactions and simpler consistency.
- **Publishes (a)**: to one artifact, the old server serialized on one global lock and re-read the
  artifact's whole history per request (60 pub/s, falling as the history grows); now a publish is one
  short transaction, so the same artifact reaches the distinct-artifact rate. Distinct artifacts gain
  1.56x and the p95 drops from 192 to 119 ms. What is left is per-request CPU in one Python process (the
  GIL): parsing, JSON, and the fsyncs of each blob and each commit.
- **Uploads**: streamed in 4 MB pieces, hashed and written in two worker threads at once while the next
  piece arrives (1.32x; a single sample).
- **Downloads (b)**: at background priority this machine moves about 1.1 GB/s per loopback stream
  whatever the server does: Python's client, curl, the old server, and the new one all land there. With
  four parallel downloads the new server does 1.18x the old one. On macOS `sendfile` was measured
  *slower* for parallel downloads of one blob (four curls: 1.0 GB/s with sendfile against 1.4 GB/s with
  1 MB pieces, repeated runs; one stream: 1.14 against 1.10 GB/s), so the server uses `sendfile` only on
  Linux, where it is zero-copy (`CAIRN_SENDFILE=1` or `0` overrides; the zero-copy path is tested on
  macOS too). The Linux numbers are not measured here.
- **Regression: pull/cold** is the first download after a restart. It is slower on both new servers in
  repeated runs (about 300-380 ms against 255-275 ms for 268 MB); warm downloads are not. The likely cause
  is first-use cost in the new process (the worker threads and their database connections, and fresh 1 MB
  buffers touching new memory pages); it is a one-off of some tens of milliseconds per server start.
- **Range requests (c)**: unchanged within noise; each is one small request and bound by the per-request
  cost. Small bodies (up to 1 MB) are read in the same worker-thread trip that resolves the file.

## First baseline (first version of the script)

Measured before the script excluded client process start-up from the publish timing and sent uploads in
1 MB blocks (http.client's default is 8 KB, which capped the upload row); kept for the record.

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
