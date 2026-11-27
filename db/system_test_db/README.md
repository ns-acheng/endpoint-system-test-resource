# SystemTest Results DB

Store every SystemTest run's results + resource telemetry in PostgreSQL so we can
build reports and compare product versions over time. Nothing new is collected —
this tool **ingests the artifacts the suite already produces**.

## What goes in

| Artifact (already produced by the suite) | Feeds |
|------------------------------------------|-------|
| `iterations_<run_id>.ndjson` (run / test / iteration records) | `test_run`, `test_result`, `iteration_result` |
| `resource_samples/<test>.jsonl` or `<test>/resource_samples.jsonl` (per-sample per-process CPU/mem/handles + dump paths) | `process_summary`, `gate_violation`, `dump` |

The per-process health-gate summary is only *logged* by the suite at test time,
never persisted. The ingest recomputes it by importing the suite's own
`health_gate.summarize_samples` / `check_thresholds` / `max_sustained_cpu_s`, so
the stored numbers match what the gate actually saw (no logic drift).

## Schema (11 tables)

```
release ─< test_run ─< test_result ─< iteration_result   (per --iterations rep: PASS/FAIL + duration)
                                    ─< process_summary    (per-process peak / sustained / growth / handles)
                                    ─< resource_sample    (raw per-tick per-process cpu/mem/handles time-series)
                                    ─< gate_violation     (parsed: which gate failed the case)
                                    ─< dump               (crash / BSOD / live dump)
product_issue   (manual triage: ticket + summary, linked to the first sighting)
gate_ticket     (manual triage: ticket <-> ONE gate breakdown row; keyed by
                 job+build+test_name so it survives a rebuild — `db.py issue set`)
db_growth       (weekly size snapshot)
```

- **Dedupe key:** `test_run.run_id` (`<job>-<build>`) is UNIQUE. Re-ingesting a
  build is a no-op unless `--replace`. A Jenkins re-run gets a *different* build
  number → different `run_id` → a separate row (it is a separate execution, not a
  duplicate).
- **Release / version:** derived from the installed client version in the ndjson
  (`140.0.0.2739` → `release-140`), so each build is tagged by the version it
  actually ran. No release is filtered out — 132, 140, developer builds all land.

## Connection settings (credentials NEVER in the repo)

Resolved by `dbconn.py`, first hit wins:

1. env vars: `GRS_DB_HOST` `GRS_DB_PORT` `GRS_DB_NAME` `GRS_DB_USER` `GRS_DB_PASSWORD`
2. a local file `~/.grs_db.conf` (outside the repo), `key=value` per line:

   ```
   GRS_DB_HOST=<db-vm-ip>
   GRS_DB_PORT=5432
   GRS_DB_NAME=grs_results
   GRS_DB_USER=grs
   GRS_DB_PASSWORD=<password>
   ```

Non-secret defaults (port/name/user) apply if nothing supplies them; host and
password have no default. The DB VM's current values live in team memory, not here.

## CPU-data validity cutoff (PR 263)

PR 263 (merge `12a9a31f`, 2026-07-18 11:16:52 +0800) switched the CPU gate to
**per-core** and added the `cpu_pct_per_core` field. CPU metrics from builds that
ran *before* this use the old total-capacity unit and are **not comparable**.

- The cutoff is a **build start time**, not the presence of the field (the field
  appears on some earlier builds via branch churn, so it is not a reliable signal).
- `import-all` **by default imports only builds started at/after the cutoff**
  (time only — branch is not considered). `--all` overrides to import every
  finished build (pass/fail/iteration data is still correct; only the CPU numbers
  are on the old unit).
- The only unconditional safety rule: a **running** build (`building=true`) is
  never imported.

## CLI — `db.py`

All DB ops go through one command so connection settings resolve in one place.

```
python grs_tools/system_test_db/db.py <subcommand> ...
```

| Subcommand | Purpose |
|------------|---------|
| `conf` | show resolved connection settings (password masked) |
| `ping` | connect + list tables |
| `apply-schema [--drop]` | create the 10 tables (`--drop` = drop them first) |
| `inspect <build_dir>` | print a build's raw ndjson run/test fields — diagnose missing data, writes nothing |
| `ingest <build_dir> [--release R] [--replace] [--dry-run]` | parse one already-downloaded build → DB |
| `fetch <job> <build> [--release R] [--replace]` | download a Jenkins build's artifacts, unzip the backup, ingest — end to end |
| `import-all <job> [<job>...] [--release R] [--limit N] [--replace] [--all]` | fetch+ingest every CPU-valid finished build of each job (see cutoff above) |
| `candidates <job> [<job>...]` | read-only: list which builds are CPU-valid and what's excluded + why |
| `query <name> [--test T] [--release R]` | canned report queries (below) |
| `runtime [--test PAT] [--release 140,141] [--per-iter] [--status ...] [--store] [--overhead]` | **how long does a case take** — per release, iter1 vs all-iteration avg/median (below) |
| `report [--release R] [--out F.html] [--top N]` | generate a self-contained HTML report (demo/visualization) — see below |
| `growth [--record]` | DB size + row counts; `--record` writes a `db_growth` snapshot |

Job aliases: `reg` `reg2` `localtest` `austin` (or a full `DEV/job/<name>` path).

### Queries

| Name | Answers |
|------|---------|
| `breakthrough` | per test/process: avg + worst CPU peak, avg sustained seconds, avg mem growth, worst handles — the per-case baseline to compare future versions against |
| `sequence` | per-iteration PASS/FAIL + duration, in order (the red→green→red pattern; "passed through iter N, failed at N+1") |
| `dumps` | every crash/BSOD/live dump in a release + the test that produced it |
| `compare` | same test/process peak + sustained across releases |
| `versions` | which releases / product versions are in the DB (proves nothing is filtered) |

### "How long does case X take?" (`db.py runtime`)

The one command for every timing question — campaign planning, choosing a test
timeout, or "is 141 slower than 140". Reads the DB by default, the harvest cold
store with `--store` (so it still answers while the DB VM is down).

```bash
# owner's question: for 140 and 141, how long is one STRESS-03 iteration?
python grs_tools/system_test_db/db.py runtime --test stress03 --release 140,141

# every case on 141, one row per case x release
python grs_tools/system_test_db/db.py runtime --release 141

# include the runs that FAILED (how long before it gave up)
python grs_tools/system_test_db/db.py runtime --test stress03 --status all

# lane budget: bodies + install/fixture/collect overhead, off the cold store
python grs_tools/system_test_db/db.py runtime --store --overhead
```

What the output guarantees, and why each part exists:

- **`--test` is fuzzy** — `stress03`, `STRESS-03`, `stress_03` and the full
  pytest name all resolve; a miss **lists the real case names** instead of
  printing an empty table (an empty table reads as "never ran").
- **`--release` takes bare majors** (`140`, `140,141`, `140/141`).
- **Grouped by release, always.** Durations are not comparable across releases,
  so one pooled average silently mixes populations.
- **`iter1` is separate from the all-iteration aggregate.** Rep 1 pays
  cold-cache + first-config-push costs later reps do not — quote `iter1` for
  `--iterations 1`. Measured: IPC-01 on 140 = 10m59s at iter 1 vs ~9m for reps
  2-18.
- **`--per-iter`** breaks it down by iteration index (automatic when `--test`
  matches ONE case, which also prints every sample with its job + build number).
- **`avg` AND `med`.** STRESS-03 on 141 = avg 45m40s / med 59m45s over 3 samples
  (13m…64m) — the average alone would be a lie you could quote.
- **`n<5` cells are flagged** (`[n<5] ... direction only, do NOT quote a delta`),
  per the noise-floor rule.
- **`--status pass` is the default**: a FAIL body is truncated or sat in a
  timeout, so it is not a sample of how long the case takes. Cases with **no**
  qualifying iteration are **named**, never silently dropped.

### HTML report (`report.py` / `db.py report`)

A single self-contained HTML file (embeds Chart.js from a CDN — no server, no
build). DEMO / visualization of the collected data, **not a CI gate**. Per
release it renders: summary KPIs (pass rate, test FAIL, gate hits, dumps);
per-test-case table (runs, avg/total duration, pass/fail, gate/dump flags —
rows shaded **red** = a test-verdict FAIL, **yellow** = health-gate fired but
test passed); health-gate breakdown by type; per-process resource peaks (CPU
per-core, sustained, mem growth, handles) + a bar chart; the most-run case's
iteration red/green sequence; and the failure list (capped at `--top`, default
10). Large tables are capped with a note — nothing is silently dropped.

```bash
python grs_tools/system_test_db/db.py report --release release-140 --out report.html
# or standalone:  python grs_tools/system_test_db/report.py --release release-140
```

## Typical workflow

```bash
# one-time
python grs_tools/system_test_db/db.py apply-schema
python grs_tools/system_test_db/db.py ping

# see what's worth importing (read-only)
python grs_tools/system_test_db/db.py candidates reg reg2 localtest

# import all CPU-valid builds across the three SystemTest jobs
python grs_tools/system_test_db/db.py import-all reg reg2 localtest

# report
python grs_tools/system_test_db/db.py query breakthrough --test test_stress_26
python grs_tools/system_test_db/db.py query versions
python grs_tools/system_test_db/db.py report --release release-140 --out report.html

# weekly growth snapshot (cron target for phase B)
python grs_tools/system_test_db/db.py growth --record
```

External BI tools (Beekeeper, Grafana, …) connect straight to Postgres with the
same credentials and run the SQL behind these queries directly.

## Deployment note

Phase A (current): ingest runs on a workstation / Jenkins node against the remote
DB over the LAN. Phase B (planned): a scheduled `import-all` on a host near the DB
picks up new finished builds automatically. This tool is the same in both.
