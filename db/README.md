---
nplan: null
type: sop
categories:
  - SOP / Workflow
  - Results DB
related_nplans: []
status: active
source: frontmatter
---

# SystemTest Results DB — Onboarding

What the results DB is, how data gets into it, and how to read it back out.
This page is for anyone new to the DB/reporting side of SystemTest, not just
AI agents — plain English, no assumed session history.

## 1. Architecture

```
Jenkins (SystemTest builds)
    |  each build's artifacts already contain iterations_<run_id>.ndjson
    |  + resource_samples/<test>.jsonl
    v
ingest pipeline (poll every few minutes, pull new/finished builds, parse +
                  load into Postgres)
    v
Postgres (schema.sql in this folder) --- report.py / query.py read it back
```

- **DB**: PostgreSQL, one `grs_results` database. Schema: [`system_test_db/schema.sql`](system_test_db/schema.sql)
  (grain: release -> test_run -> test_result -> iteration_result /
  process_summary / gate_violation).
- **Ingest is idempotent**: `test_run.run_id` is UNIQUE, so importing the same
  build twice is a safe no-op (skipped, not duplicated). This is why there
  are currently **two independent pollers** feeding the same DB — a primary
  (5-minute cadence, on Austin's workstation via Windows Task Scheduler) and
  a backup (11-minute cadence, on VM `SYS-07` via WSL cron, added 2026-09-06
  for redundancy against a single-machine scheduler failure). Neither needs
  to know the other exists.
- **Ingest code lives in the PRIVATE `claude-resource` repo**
  (`grs_tools/system_test_db/{db,ingest,harvest}.py` + the Task-Scheduler /
  cron wiring), not here. Reason: it imports the GRS test suite's own
  `health_gate` reduction (so the DB's pass/fail summary matches exactly what
  the gate saw at test time), and that dependency chain, plus the DB/Jenkins
  credential resolution, belong with the rest of the private ops tooling. If
  you need to add a build source, fix an ingest bug, or run a manual
  re-ingest, you need access to that repo — ask Austin.

## 2. What's mirrored here vs. what stays private

| Here (`endpoint-system-test-resource/db/`) | Private (`claude-resource/grs_tools/`) |
|---|---|
| `schema.sql` — DDL, read-only reference | `system_test_db/{db,ingest,harvest}.py` — the actual ingest/import code |
| `report.py`, `query.py`, `report_server.py`, `dbconn.py`, `grs_jenkins.py` — read-only reporting/query API | `systest_db_poll.py` + `.grs_db.conf` + Task Scheduler / cron wiring — the scheduling + credentials |

Every file here has a `MIRROR NOTICE` header: it's a point-in-time copy, not
the canonical source. If you need to change ingest/report/query logic, change
it in `claude-resource` first, then re-sync a copy here — don't edit the copy
directly, it'll just drift and confuse the next reader.

**Why the split**: this repo is public. The reporting/query code has no
credentials in it (see below) and is safe to publish. The ingest side pulls
in the GRS test framework's `health_gate` module and needs Jenkins/DB
credentials to resolve — that coupling and the credential-resolution logic
stay in the private repo.

## 3. Getting your own DB access

**Don't ask to share Austin's `~/.grs_db.conf`.** Get your own Postgres
login on the DB host (ask Austin to create one — see `dbconn.py`'s
docstring for the exact settings: host/port/db name/user/password, resolved
from env vars or a local `~/.grs_db.conf` file, never hard-coded). Once you
have credentials:

```bash
# ~/.grs_db.conf (chmod 600; never commit this anywhere)
GRS_DB_HOST=<db host>
GRS_DB_PORT=5432
GRS_DB_NAME=grs_results
GRS_DB_USER=<your own user>
GRS_DB_PASSWORD=<your own password>
```

Read-only queries don't need Jenkins credentials at all — those are only
resolved by `query.py`'s job-alias lookup and by the (private, not mirrored
here) ingest side.

## 4. Reading data back out

Requires `psycopg2` (`pip install psycopg2-binary`, or on Debian/Ubuntu
`apt-get install python3-psycopg2` — no need for the GRS test framework or
any of its other dependencies; this code path was specifically decoupled
from that in Sept 2026).

**One-shot HTML report** (a release/tenant/platform-scoped dashboard —
pass/fail per case, resource peaks, iteration verdict sequences):
```bash
cd system_test_db
python report.py --release release-141 --top 10 [--tenant 1457] [--platform mac]
# writes ./systest_report/release-141/systest_report_r141_<timestamp>.html
```

**Live query server** (`/report/<release>[/<tenant>]` and
`/query?test=<name>` endpoints — the checkbox-driven "did test X recently
pass on every lane" view):
```bash
cd system_test_db
python report_server.py --host 0.0.0.0 --port 8765
# then browse http://localhost:8765/report/release-141
#              http://localhost:8765/query?test=test_stress_05_crash_under_load
```
See [`report_server_setup.md`](system_test_db/report_server_setup.md) for
running it as a standing service. **Caveat**: its `ensure`/auto-restart
subcommand hard-codes `C:\Python311\python.exe` (Austin's own machine's
interpreter path) — fine for ad-hoc use, but fix that path first if you want
the self-relaunch watchdog to work on a different machine.

## 5. Health / troubleshooting

- A poller's own health state (consecutive-failure count, last error) is
  written to a sentinel file next to its log — private-repo side only, ask
  Austin if you suspect ingest has stalled (report/query on your end will
  just show data that's a bit stale, not an error).
- If a query returns nothing for a build you know ran: check the build
  actually finished (running builds are never imported) and that it's not
  older than the ingest tool's dedupe/retention window — again, a
  private-repo-side question.
