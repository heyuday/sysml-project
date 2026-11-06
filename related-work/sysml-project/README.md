# Workload-aware SQL result caching

The project implements the three research tracks in the attached plan. Python runs the broker and the replay harness. PostgreSQL is the authoritative store, and Redis holds serialized result values. The existing BIRD installation supplies agent traces for RQ1 only. The CH workload uses the upstream BenchBase PostgreSQL loader for TPC-C plus CH-benCHmark. No code runs a workload at import or installation time.

| Question | Input | Implemented comparisons |
|---|---|---|
| RQ1 | Controlled positive and negative SQL pairs on CH and separately reported BIRD agent SQL | N0 exact text, N1 lexical, N2 SQLGlot with PostgreSQL parse-tree guard, N3 model proposal with independent verification |
| RQ2A | CH reads and committed writes | I0 full flush, I1 table, I2 columns, I3 old/new row predicates, I4 lazy revision counters, pre/post execution oracle |
| RQ2B | Same calibrated CH trace and fixed I1 or I2 | R0 lazy or reconsider admission, R1 all, R2 frequency, R3 cost, R4 recency, R5 decay, R6 reuse before invalidation, R7 combined. Budgeted and unlimited refresh, five feature ablations |
| RQ3 | CH with fixed normalization and invalidation | E0 FIFO, E1 LRU, E2 LFU, E3 decayed LFU, E4 cost, E5 frequency × cost, E6 GDSF, E7 predicted reuse, E8 combined. Byte budgets are unlimited or 50%, 25%, 10%, 5%, 1% of one measured unlimited replay |

## Files

- `sysml/identity.py` defines cache eligibility, dependency extraction, identities and the N3 proof gate.
- `sysml/policies.py` implements all policies and persistent-within-run metadata, shared across concurrent clients.
- `sql/install-cdc.sql`, `sysml/db.py` and `sysml/live.py` implement transactional change capture, the broker and the Redis value store.
- `sysml/workloads.py` and `scripts/bird_capture.py` handle workload construction and observed tool calls.
- `sysml/evaluate.py`, `simulation.py`, `matrix.py` and `reporting.py` implement calibration, replay, validation and aggregation.
- `configs/design.json` lists workload factors, seeds and time partitions. Policy JSONs are editable examples. The sweep command varies policies for one supplied trace. Generate a separate trace for each workload factor combination you choose.
- `vendor/benchbase` is a local checkout of https://github.com/cmu-db/benchbase at `33c00473807ebd49304d114a6d769d2d2b2bbb34`. Its license and upstream source are included. The Python lockfile is `uv.lock`.

## Repository contents and external dependencies

This repository tracks the project code, configuration, research plan, and collected experiment evidence under `artifacts/`, including raw request logs, broker decisions, BIRD agent traces, and analysis sources. BenchBase, the local Python environment, credentials, and generated report exports are excluded from Git.

Restore the external BenchBase source at the recorded revision before generating CH workloads or building the database loader:

```powershell
git clone https://github.com/cmu-db/benchbase.git vendor/benchbase
git -C vendor/benchbase checkout 33c00473807ebd49304d114a6d769d2d2b2bbb34
uv sync
```

The current experiments use BenchBase's TPC-C/CH loader and CH query templates Q1, Q6, and Q14. Its bundled `data/tpch-sf0.01`, `data/tpch-sf0.1`, and `data/twitter` directories are unused by these experiments and have been removed from the local checkout. Cloning upstream restores those optional datasets too.

The evidence preserves the original paths and run settings. Windows launchers currently target `D:\Projects\sysml-project`; update those paths for another machine. BIRD-Interact and its separately obtained dataset/database installation are external dependencies. Some historical status files reflect intermediate stages; completed trial results and the final analysis evidence are the authoritative records.

The 5 November analysis source and reviewed RQ1 pair labels are in `artifacts/rq1-command-a-full-lite/analysis-20261105/`. The report generator additionally requires ReportLab and Windows Segoe UI fonts. Raw logs are deliberately retained for reanalysis; a fresh clone is larger than a code-only repository.

## Future setup and execution

These are instructions for a later, explicitly authorized run. Run from `D:\Projects\sysml-project`. PowerShell examples assume the installed project environment is active. Each result command creates a new output file and refuses to overwrite an earlier one.

```powershell
.\.venv\Scripts\Activate.ps1
sysml --help
```

Once per clean experiment environment, `.\scripts\prepare-ch.ps1` starts the project-specific Compose services, builds the BenchBase loader, loads a single warehouse, and installs CDC **after** the bulk load. The script refuses to initialize a populated template. It requires Docker Desktop, network access on first build, and sufficient disk space for database clones. The loader builds in a container, so a host JDK is unnecessary. PostgreSQL and Redis use loopback ports 55432 and 56379. BIRD uses different services and ports.

### Controlled CH operations and calibration

Keep the loaded `chbenchmark` database as a pristine template. Close template connections before cloning. Each calibration, oracle or live policy run with writes needs its own fresh clone of that template. Clone creation accepts a new name beginning with `sysml_` and never replaces an existing database.

```powershell
sysml generate artifacts/ch11.jsonl --n 1000 --seed 11 --clients 8 --write .05 --skew 1.1 --pattern shift --repetition high --locality hot --mix mixed
sysml clone-db sysml_cal11
$env:SYSML_DSN='postgresql://bench:localbench@127.0.0.1:55432/sysml_cal11'
sysml calibrate artifacts/ch11.jsonl artifacts/cal11.jsonl --profile-refresh --oracle artifacts/oracle11.jsonl
```

The generator fixes the arrival schedule, clients, bound parameters and requested write count at generation time. Its mix includes official BenchBase Q1, Q6 and Q14 from the pinned source and custom point, range, join, stock-update and order-line-update operations. These are labeled separately in every trace. This is a controlled CH-derived workload, not a score from the native full BenchBase transaction mix. The standard loader and native mixed workload remain available under `vendor/benchbase`. Transactions from an external native BenchBase client also go through the installed triggers, even if they bypass the broker.

Use `--write 0` for initial normalization and eviction comparisons. Use `--write_column referenced` or `unreferenced` to isolate column selection on stock, `--pattern burst` to cluster reads after hot writes, and `--mix disagreement` for cheap popular lookups versus expensive long-tail aggregates. Use `--variation` to vary SQL formatting. Record actual baseline cost distributions: the installed data and date filters determine what counts as expensive. Keep `source=controlled-ch` separate from BIRD.

Calibration executes original statements serially against one CH clone. It saves raw SQL identity, result type metadata in the digest, PostgreSQL client-observed execution time, serialized bytes, version, and committed changes. The oracle compares the entire pool of previously observed eligible raw reads immediately before and after each write. For I4, the report materializes the selected set by checking stored revisions. `--profile-refresh` additionally measures post-write values and costs needed to model eager refreshes. Both options can be expensive. Use a manageable correctness trace. Exclude this measurement work from policy DB-cost comparisons.

### RQ1 normalization and BIRD collection

```powershell
sysml pairs artifacts/equivalence-pairs.jsonl
sysml normalize artifacts/cal11.jsonl artifacts/cal11.jsonl.catalog.json artifacts/normal-ch.jsonl --controlled
sysml bird-import 'PATH-TO-A-REAL-BIRD-ADK-RESULT.json' artifacts/bird-observed.jsonl
```

The importer takes `results[].tool_trajectory`, preserves the SQL from `execute_sql` and `submit_sql`, and marks submissions separately. It never uses `sol_sql`. A task copy is a separate database state; each submission also starts a conservative new state scope. A standard ADK result lacks SQL arrival times and direct PostgreSQL costs. Use it for key counts, or collect timestamped traces for timing studies.

To collect future traces, use the existing BIRD Python environment and the existing agent and simulator. Start its database-environment service with the wrapper entry point below **in place of** its normal uvicorn database-environment command. Do not start two processes on port 6002. The other two BIRD services and PostgreSQL must be configured as in the original BIRD setup.

```powershell
$env:SYSML_BIRD_TRACE='D:\Projects\sysml-project\artifacts\bird-live.jsonl'
New-Item -ItemType Directory -Force 'D:\Projects\sysml-project\artifacts' | Out-Null
$env:PYTHONPATH='D:\Projects\sysml-project\scripts;D:\Projects\BIRD-Interact\BIRD-Interact-ADK'
Set-Location 'D:\Projects\BIRD-Interact\BIRD-Interact-ADK'
.\.venv\Scripts\python.exe -m uvicorn bird_capture:app --host 127.0.0.1 --port 6002
```

The wrapper observes HTTP tool calls and delegates them to the original service. It records successful reads, SQL submissions, schema calls, timestamps, response digests and endpoint latency. Endpoint latency and formatted BIRD response bytes are distinct from PostgreSQL execution cost and full SQL result size. Session resets and submissions separate state scopes. It does not provision snapshots automatically. Preserve the appropriate task database state if you want to validate post-submission reads. For first-phase reads known to use the unchanged base state, a matching read-only clone can be reused.

For static key analysis, export a catalog using `sysml catalog artifacts/alien-catalog.json` with `SYSML_DSN` pointing to the matching BIRD database. Run `normalize` with that catalog and `--scope <captured-scope>`. For timing, create a JSON dictionary mapping each captured scope to an **environment variable name** containing the corresponding snapshot DSN. Run `sysml bird-calibrate artifacts/bird-live.jsonl snapshot-map.json artifacts/bird-cal.jsonl`; this uses read-only transactions and emits a per-scope catalog for replay. The mapping is required because the ADK typically cleans up task copies. For multi-snapshot collision checks, pass `--snapshot-env SNAP_A --snapshot-env SNAP_B` to `normalize` with matching immutable snapshots. Evaluate controlled pairs on CH separately from agent traces.

N3 uses an explicitly configured OpenAI-compatible reasoning endpoint. Set `SYSML_N3_URL` to its base URL (including `/v1` if applicable), `SYSML_N3_MODEL` to its model identifier, and optionally `SYSML_N3_KEY`. Enable with `--policy N0 N1 N2 N3`. No endpoint or model is selected by default. N3 sends SQL text to the selected endpoint. A proposal can only merge keys if both PostgreSQL trees are identical after a narrow proof rule for reordering total built-in integer comparisons in a top-level AND. The proof preserves projections, names, casts, parameter positions and ordering. Unverified proposals fall back to N2. Finite snapshot matches never authorize a rewrite.

### RQ2 and RQ3 offline comparisons

```powershell
sysml simulate artifacts/cal11.jsonl artifacts/one-policy.jsonl --config configs/rq2b.json
sysml sweep artifacts/cal11.jsonl artifacts/rq2a11.jsonl --track rq2a --split tune --seed 11 --scenario artifacts/ch11.jsonl.scenario.json
sysml sweep artifacts/cal11.jsonl artifacts/rq2b11.jsonl --track rq2b --split tune --seed 11 --scenario artifacts/ch11.jsonl.scenario.json
sysml sweep artifacts/cal11.jsonl artifacts/rq311.jsonl --track rq3 --split tune --seed 11 --scenario artifacts/ch11.jsonl.scenario.json
```

Repeat with independently generated seeds. Freeze the selected `tau`, prediction horizon, weights, refresh threshold and admission settings in a policy JSON, then pass it via `--config` when evaluating `--split test`. The first 40% of operations form the tuning partition and the last 60% form the held-out partition. Both begin cold. The first 20% of each partition is reported separately. `sysml summarize artifacts/summary.jsonl result1.jsonl result2.jsonl ...` reports seed-level variability as a bootstrap interval for mean steady-state avoided read DB time, after refresh cost. Use different files per seed and policy family. Multiple runs of the same seed are not independent replicates.

Offline replay models demand fill completion, concurrent identical misses, a four-worker refresh queue, revision-checked publication and lost work. It uses the fixed recorded write order as logical commit order. DB costs come from the corresponding calibration. It reports modeled waits, hit/byte rates, normalizer overhead, conditional hit metrics, wasted refreshes, value bytes and estimated metadata bytes. Treat model estimates and live timings as separate outcomes. The simulator does not model CPU, database lock or connection-pool contention.

### Live validation and concurrency

For each policy, use a new database clone and a fresh broker process. Use the same input trace, machine and Redis configuration. The direct baseline passes through the same broker and CDC infrastructure with result caching disabled. In a clean shell, start the server with the clone DSN already set.

```powershell
sysml clone-db sysml_live11
$env:SYSML_DSN='postgresql://bench:localbench@127.0.0.1:55432/sysml_live11'
sysml serve --config configs/baseline.json --log artifacts/broker11.jsonl
# In a second shell:
sysml live artifacts/ch11.jsonl artifacts/live11.jsonl --clients 8
```

Use `configs/direct.json` on another fresh clone for the reference. Compare completed logs with `sysml compare-live artifacts/direct11.jsonl artifacts/live11.jsonl artifacts/live-comparison.jsonl`. The report separates client wait, DB time, queue delay fields, result sizes and refresh work. Different interleavings can legally change live results. To test freshness, use a separate run with `configs/live-audit.json`; every cache hit is reexecuted in the same PostgreSQL snapshot. Audit reads are separately counted and must be excluded from performance runs.

## Consistency contract and interpretation

One broker process owns the experiment's in-memory policy metadata. Multiple clients share it and its Redis value namespace. Starting another broker starts a cold independent trial. Redis eviction is disabled; the broker enforces the same serialized-byte budget across policies. Historical stats survive invalidation and eviction inside each trial. Restart persistence of cache metadata is outside this experimental design.

All writers participate in transactional row/statement triggers on the loaded public base tables. A common writer fence serializes the change clock and outbox commit order. This introduces real write overhead, held constant across policies. A background consumer processes the outbox. Each read takes a PostgreSQL repeatable-read snapshot and checks its watermark before serving any cached value. The snapshot is the read's consistency point. A lagging consumer is caught up synchronously. Fills coalesce per key, and demand and proactive results can only be published against the version they used. Unrelated writes can conservatively discard in-flight work. CDC capture-to-apply lag is recorded separately from audit failures.

The supported contract assumes a dedicated static schema, no DDL/role/config changes mid-trial, trusted built-in operators and functions, fixed DB identity, role, timezone and search path, and no disabled triggers or replication-role bypass. Cache eligibility is deliberately narrow. Views, row security, inheritance, unknown functions, locks, LIMIT and unsupported syntax bypass. I2 narrows columns only when single-table provenance is unambiguous. I3 has a sound integer comparison subset and checks both row images. I4 keeps integer equality bucket revisions and falls back to table revisions. It is a restricted variant inspired by https://arxiv.org/abs/2310.15360. Unsupported dependency refinements fall back conservatively.

Results preserve PostgreSQL field names, type OIDs, type modifiers, NULLs and raw text field bytes. Unordered results are compared as multisets with duplicates. Ordered results are compared as sequences; use a total ORDER BY when testing ordering, since ties may otherwise reorder. Result sizes are serialized JSON bytes including metadata, not Redis allocator bytes. Live stats report Redis value-key allocation separately; Python object-size estimates include entries, history, revision and reverse indexes. Transfer/query execution timings include the Python-to-PostgreSQL round trip.

## Verification boundary

On November 4, 2026, a bounded smoke test loaded a disposable CH warehouse, passed the four unit contracts, imported 101 SQL calls from the two bundled BIRD example files, and executed Q1, Q6 and Q14. All five invalidation policies detected the observed changed result. The offline harness exercised all refresh and eviction policies. The live broker served audited Redis hits, retained an unaffected row, refreshed invalidated results and returned the updated value to four concurrent readers after an external write. A separately synchronized two-write test confirmed that an older in-flight refresh was discarded. N3's HTTP interface and proof gate were checked with a stub. A live LLM, a new BIRD agent session and sustained load remain untested. The temporary smoke database and services were removed.
