#!/usr/bin/env python
"""GRS SystemTest results DB — one CLI for every DB operation.

All DB work goes through this tool so the connection settings resolve in ONE
place (dbconn.py: env or ~/.grs_db.conf, never hard-coded in the repo) and the
whole class of ops runs behind a single allowlisted prefix.

Subcommands
-----------
    db.py conf                       show resolved connection settings (password masked)
    db.py ping                       connect + SELECT 1 + list tables
    db.py apply-schema [--drop]      run schema.sql (--drop = DROP the 11 tables first)
    db.py ingest <build_dir> [--release R] [--replace] [--dry-run] [--check-insert]
                                     parse one build's artifacts -> DB (see ingest.py).
                                     --dry-run never touches the DB; --check-insert runs the
                                     real INSERTs against the live schema then ROLLBACKs
                                     (the only check that catches a column/placeholder
                                     mismatch after a schema change).
    db.py fetch <job> <build> [--release R] [--replace]
                                     download a Jenkins build's artifacts, unzip the
                                     backup, then ingest — end to end, no manual steps
    db.py candidates <job> [<job>...]
                                     read-only: per job, list builds whose CPU data
                                     is valid (finished + started at/after PR263;
                                     branch ignored) and show what's excluded + why.
    db.py import-all <job> [<job>...] [--release R] [--limit N] [--replace] [--all]
                                     fetch+ingest each job's CPU-valid finished builds
                                     (finished + started at/after PR263). --all imports
                                     every finished build incl. pre-PR263 (old CPU unit).
                                     NEVER touches a running build (building=true).
                                     Already-ingested builds are skipped (run_id key).
    db.py query <name> [args...]     canned report queries (see `db.py query` for list)
    db.py report [--release R] [--out F.html] [--top N]
                                     generate a self-contained HTML report (demo/visual)
    db.py fix-run <job> <build> [--version V] [--release R]
                                     correct one run's product_version and/or release label
                                     (e.g. a build that recorded a residual client version and
                                     got miscategorised). Prints before/after for audit.
    db.py final <N> [--apply [--redo]]
                                     the whole release-<N>-final flow for ANY release/lane set:
                                     rules-based keep list (final_rules.json) -> dry run -> clone
                                     -> verify. Overrides persist in keep_lists/release-<N>-final.json.
    db.py clone-release --source R --target R2 --keep-file F [--dry-run]
                                     copy release R into a NEW release R2, dropping non-PASS
                                     test_result rows not listed in F's 'keep' list (publish-
                                     ready snapshot). PASS rows always copied; empty-shell
                                     test_run rows (0 surviving results) are not copied.
    db.py rename-release --from L --to L2
                                     rename a release label in place (no data moves).
    db.py purge-feature [--fix]
                                     list (--fix DELETEs) feature-build runs (version
                                     major>=200, e.g. 211.x) that slipped into the DB
                                     before the ingest gate; DELETE cascades to children
    db.py check-partial [--fix] [--release R]
                                     find test_run rows with 0 test_result (partial/interrupted
                                     write that the old skip-guard silently ignored); --fix
                                     re-fetches + re-ingests each one automatically
    db.py runtime [--test PAT] [--release 140,141] [--job J] [--per-iter]
                  [--status pass|fail|all] [--store [DIR]] [--overhead]
                                     "how long does case X take?" — avg/median/min/max per
                                     RELEASE (durations are not comparable across releases),
                                     iter_index=1 reported separately from the all-iteration
                                     aggregate because rep 1 pays cold-cache + first
                                     config-push costs that later reps do not.
                                     --test is fuzzy ('stress03' / 'STRESS-03' / full pytest
                                     name) and a miss LISTS the real names. --release takes
                                     bare majors. --per-iter breaks the numbers down by
                                     iteration index (automatic for a single case, which also
                                     prints every sample with its job+build). Cells with n<5
                                     are flagged: direction only, never quote a delta.
                                     --store reads the harvest cold store instead of the DB,
                                     so it works while the DB is down. --overhead adds Jenkins
                                     wall-clock minus body time (install + fixtures +
                                     collect), computed from single-case iter=1 builds.
    db.py issue list [--release R] [--build N]
    db.py issue set <TICKET> --build N [--job J] [--test PAT] [--note S]
    db.py issue rm  <TICKET> --build N [--job J] [--test PAT]
                                     ticket id for a Health-gate breakdown row (the
                                     report's `ticket` column). `list` prints the same
                                     rows the report shows, with their ticket. `set`
                                     needs --job when one build number exists in two
                                     lanes (never guessed), and --test to tag ONE case
                                     instead of every gate row of that build. The link
                                     is keyed by job+build+test, so it survives a DB
                                     rebuild — but like product_issue it has no artifact
                                     source: only `db.py backup` can restore it.
    db.py growth [--record]          DB size + row counts; --record writes a db_growth row
    db.py backup [--out DIR]         dump every table to gzipped CSV + manifest.json
                                     (COPY-based, needs no pg_dump binary). MANDATORY
                                     because product_issue is hand-entered and has no
                                     artifact source — every other table can be rebuilt
                                     by `harvest.py ingest-all`, that one cannot.
    db.py restore <DIR> [--truncate] load a backup dir back in (parents first, id
                                     sequences re-pointed). Refuses a non-empty DB
                                     unless --truncate (destructive).

Examples
    python db.py ping
    python db.py fetch reg 84 --release release-140
    python db.py query breakthrough test_stress_26 --release release-140
    python db.py runtime --test stress03 --release 140,141
    python db.py runtime --store --overhead
    python db.py growth --record
"""

import os as _nw_os
import sys as _nw_sys

_nw_sys.path.insert(0, _nw_os.path.dirname(_nw_os.path.dirname(_nw_os.path.abspath(__file__))))
import nowindow  # noqa: E402,F401  (pythonw: no console windows from child processes)

import argparse
import os
import re
import subprocess
import sys

# Console is cp1252; force UTF-8 via reconfigure(), NOT a fresh TextIOWrapper.
# A new wrapper here would be left unreferenced the moment another module does
# the same to sys.stdout.buffer (grs_jenkins does, and `runtime --overhead`
# imports it) -> GC -> the real stdout buffer is closed under us -> "I/O
# operation on closed file" on the next print. reconfigure() mutates the
# existing object, which sys.__stdout__ keeps alive, so it composes safely.
# Same reasoning as the comment at the top of harvest.py.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCHEMA = os.path.join(_HERE, "schema.sql")
_TABLES = [
    "db_growth",
    "gate_ticket",
    "product_issue",
    "dump",
    "gate_violation",
    "resource_sample",
    "process_summary",
    "iteration_result",
    "test_result",
    "test_run",
    "release",
]

sys.path.insert(0, _HERE)
import dbconn  # noqa: E402
import testname  # noqa: E402  (OS suffix convention: test_x_win / _mac / _linux)

# ─────────────────────────── conf / ping ───────────────────────────


def cmd_conf(a):
    print(dbconn.describe())
    return 0


def cmd_ping(a):
    conn = dbconn.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"
        )
        tables = [r[0] for r in cur.fetchall()]
    conn.close()
    print(f"[ok] connected: {dbconn.describe()}")
    print(f"[ok] tables ({len(tables)}): {', '.join(tables) or '(none)'}")
    return 0


# ─────────────────────────── schema ───────────────────────────


def cmd_apply_schema(a):
    conn = dbconn.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        if a.drop:
            for t in _TABLES:  # drop in FK-safe order
                cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
            print(f"[drop] dropped {len(_TABLES)} tables")
        with open(_SCHEMA, encoding="utf-8") as f:
            cur.execute(f.read())
    conn.close()
    print(f"[ok] applied {_SCHEMA}")
    return 0


# Additive column migrations for an already-populated DB. Each entry is
# idempotent (ADD COLUMN IF NOT EXISTS). Append new additive columns here rather
# than DROP+re-apply (which would destroy existing data). Ordered oldest-first.
_MIGRATIONS = [
    ("resource_sample", "net_mbps_in", "NUMERIC"),
    ("resource_sample", "net_mbps_out", "NUMERIC"),
    ("resource_sample", "load_phase", "TEXT"),
    # Thread count per process and tick: Windows sampler key "threads", mac sampler key "num_threads".
    ("resource_sample", "threads", "INTEGER"),
    # Per-run bitness (tenant / dc / platform were in the original schema).
    # Two columns: CLI flag = expected arch, install path = ground truth.
    ("test_run", "is_64_bit_cli", "BOOLEAN"),
    ("test_run", "is_64_bit_installed", "BOOLEAN"),
    # A ticket links to a real build+version, not a bare test name -- needed
    # for the no-test_run (infra-level failure) case; see schema.sql.
    ("gate_ticket", "product_version", "TEXT"),
    ("gate_ticket", "is_64_bit", "BOOLEAN"),
    # Report-page override, decoupled from the real run/version (see schema.sql).
    ("gate_ticket", "report_release", "TEXT"),
    # Ticket-level category and title for the report's Tickets table (see schema.sql).
    ("gate_ticket", "category", "TEXT"),
    ("gate_ticket", "summary", "TEXT"),
]


def _migrate_blocks():
    """Whole-object migrations, read straight out of schema.sql.

    A new TABLE cannot be expressed as an ADD COLUMN, and duplicating its DDL
    here would be the copied-constant failure (fault ledger 41): the two copies
    drift and only one of them is the truth. So schema.sql marks the
    already-idempotent statements (every one CREATE ... IF NOT EXISTS) with

        -- migrate-block: <name>
        ...
        -- end migrate-block

    and this reads them back. schema.sql stays the single source of truth for
    both `apply-schema` (fresh DB) and `migrate` (populated DB).
    Returns [(name, sql), ...] in file order.
    """
    with open(_SCHEMA, encoding="utf-8") as f:
        lines = f.read().splitlines()
    blocks, name, buf = [], None, []
    for ln in lines:
        s = ln.strip()
        if s.startswith("-- migrate-block:"):
            name, buf = s.split(":", 1)[1].strip(), []
        elif s == "-- end migrate-block":
            if name is not None:
                blocks.append((name, "\n".join(buf)))
            name, buf = None, []
        elif name is not None:
            buf.append(ln)
    if name is not None:
        raise ValueError(
            f"schema.sql: migrate-block '{name}' has no '-- end migrate-block'"
        )
    return blocks


def cmd_migrate(a):
    """Apply additive migrations to the live DB without dropping data.
    Idempotent: safe to run repeatedly (ADD COLUMN / CREATE ... IF NOT EXISTS)."""
    conn = dbconn.connect()
    conn.autocommit = True
    applied = 0
    with conn.cursor() as cur:
        for table, col, coltype in _MIGRATIONS:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}")
            print(f"[migrate] {table}.{col} {coltype} — ensured")
            applied += 1
        for name, sql in _migrate_blocks():
            cur.execute(sql)
            print(f"[migrate] {name} (schema.sql block) — ensured")
            applied += 1
    conn.close()
    print(f"[ok] {applied} migration(s) ensured")
    return 0


# ─────────────────────────── ingest / fetch ───────────────────────────


def cmd_ingest(a):
    import ingest

    argv = [a.build_dir]
    if a.release:
        argv += ["--release", a.release]
    if a.replace:
        argv += ["--replace"]
    if a.dry_run:
        argv += ["--dry-run"]
    if getattr(a, "check_insert", False):
        argv += ["--check-insert"]
    return ingest.main(argv)


def cmd_report(a):
    import report

    argv = ["--release", a.release, "--top", str(a.top)]
    if a.out:
        argv += ["--out", a.out]
    return report.main(argv)


def cmd_inspect(a):
    """Diagnose a build's RAW ndjson: print the run record + each test record's
    key fields (client_version, iterations, status) exactly as parsed — no DB.
    Use to check WHY a field (e.g. product_version) is missing after ingest."""
    import ingest

    try:
        model = ingest.load_build(a.build_dir)
    except ingest.NoData as e:
        print(f"[no-data] {e}")
        return 3
    run = model["run"]
    print(f"ndjson: {model['ndjson_path']}")
    print("run record:")
    for k in (
        "run_id",
        "job_name",
        "build_number",
        "tenant",
        "dc",
        "test_platform",
        "is_64_bit",
        "previous_release",
        "iterations_cli",
        "started_at",
    ):
        print(f"  {k:18} = {run.get(k)!r}")
    # Installed arch comes from the TEST records, not the run record (the client
    # is not installed yet when the run line is written) — same resolver ingest
    # uses, so this line is what actually lands in test_run.is_64_bit_installed.
    print(f"  {'installed 64-bit':18} = {ingest.installed_bitness_of(model)!r}")
    derived = ingest._release_from_version(
        next(
            (
                t["test"].get("client_version")
                for t in model["tests"]
                if t["test"].get("client_version")
            ),
            None,
        )
    )
    print(f"  derived release   = {derived!r}")
    print("test records:")
    for t in model["tests"]:
        tr = t["test"]
        print(
            f"  {tr['test_case']:42} status={tr.get('test_status')} "
            f"client_version={tr.get('client_version')!r} "
            f"iters={tr.get('iterations_total')}"
        )
    return 0


def cmd_fetch(a):
    """Download a Jenkins build's artifacts via grs_jenkins.py, unzip the backup
    (resource_samples live inside it), colocate the ndjson, then ingest."""
    import shutil

    dest = a.dest or os.path.join(
        os.environ.get("TEMP", "/tmp"), f"grsdb_{a.job}_{a.build}"
    )
    # Clean up the work dir on EVERY exit path (failed download, bad zip, ingest
    # exception, disk full), not only after a clean ingest: the early returns used
    # to leave 100-300 MB per build in TEMP and filled the C: drive (2026-10-28,
    # 39 dirs / 5.3 GB). A user-supplied --dest is never deleted.
    try:
        return _cmd_fetch_body(a, dest)
    finally:
        if not a.dest:
            shutil.rmtree(dest, ignore_errors=True)


def _cmd_fetch_body(a, dest):
    import glob
    import shutil
    import zipfile

    # Start from a clean dir every time: a stale ndjson/unz from a prior fetch of
    # a DIFFERENT build (or a re-run) must never be picked up by the recursive
    # ndjson/backup globs below — that would ingest the wrong build's data.

    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)

    gj = os.path.join(os.path.expanduser("~"), "grs_tools", "grs_jenkins.py")
    if not os.path.isfile(gj):
        gj = "grs_jenkins.py"  # fall back to PATH
    print(f"[fetch] downloading {a.job} #{a.build} artifacts -> {dest}")
    r = subprocess.run(
        [sys.executable, gj, "artifacts", a.job, str(a.build), dest],
        capture_output=True,
        text=True,
    )
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        print(r.stderr)
        return 1

    # unzip every backup_nsc_*.zip in place (resource_samples/<test>/... inside).
    # Guard against path-traversal entries ("../x", absolute paths): only extract
    # members that resolve inside unz/. The zips come from our own Jenkins, but a
    # cheap containment check keeps a malformed/hostile archive from writing out.
    unz_dir = os.path.join(dest, "unz")
    unz_abs = os.path.abspath(unz_dir)
    for z in glob.glob(os.path.join(dest, "**", "backup_nsc_*.zip"), recursive=True):
        try:
            with zipfile.ZipFile(z) as zf:
                safe = []
                for m in zf.namelist():
                    target = os.path.abspath(os.path.join(unz_dir, m))
                    if target == unz_abs or target.startswith(unz_abs + os.sep):
                        safe.append(m)
                    else:
                        print(f"[warn] skipping unsafe zip entry: {m}")
                zf.extractall(unz_dir, members=safe)
            print(f"[fetch] unzipped {os.path.basename(z)}")
        except zipfile.BadZipFile:
            print(f"[warn] bad zip: {z}")

    # mac lanes (MAC2 / MAC2-SSH) archive a per-test results/<test>_<ts>.zip
    # (resource_samples.jsonl at the zip root) instead of backup_nsc_*.zip. Unzip
    # each into unz/<zip stem>/ so ingest._load_samples layout 1 (folder name
    # contains the test name) finds it; without this every mac build ingested
    # with procs=0 and the mac process charts had no data.
    for z in glob.glob(os.path.join(dest, "**", "*.zip"), recursive=True):
        if os.path.basename(z).startswith("backup_nsc_"):
            continue
        try:
            with zipfile.ZipFile(z) as zf:
                if "resource_samples.jsonl" not in zf.namelist():
                    continue
                sub = os.path.join(unz_dir, os.path.splitext(os.path.basename(z))[0])
                os.makedirs(sub, exist_ok=True)
                zf.extract("resource_samples.jsonl", sub)
            print(f"[fetch] unzipped resource_samples from {os.path.basename(z)}")
        except zipfile.BadZipFile:
            print(f"[warn] bad zip: {z}")

    # colocate the ndjson next to the unzipped samples so ingest sees one tree.
    # The ndjson may sit at the download root (REG/REG-02) or under results/
    # (LOCALTEST is a freestyle job that archives into results/), so search
    # recursively. The backup zip is unzipped into unz/ regardless of its depth.
    unz = os.path.join(dest, "unz")
    if os.path.isdir(unz):
        for nd in glob.glob(
            os.path.join(dest, "**", "iterations_*.ndjson"), recursive=True
        ):
            if os.path.commonpath(
                [os.path.abspath(nd), os.path.abspath(unz)]
            ) == os.path.abspath(unz):
                continue  # already inside unz (don't copy onto itself)
            shutil.copy(nd, unz)
        build_dir = unz
    else:
        build_dir = dest

    import ingest

    # --expect-build: we know exactly which build we downloaded, so tell ingest.
    # A backup zip whose only ndjson belongs to an EARLIER build (workspace reuse
    # after a run died pre-test) must be reported as no-data, never ingested as
    # this build's results.
    argv = [build_dir, "--expect-build", str(a.build)]
    if a.release:
        argv += ["--release", a.release]
    if a.replace:
        argv += ["--replace"]
    return ingest.main(argv)


# ── CPU-data validity cutoff ────────────────────────────────────────────────
# PR 263 (merge commit 12a9a31f, 2026-07-18 11:16:52 +0800) switched the CPU
# gate to per-core and added the cpu_pct_per_core field. CPU metrics from builds
# that ran BEFORE this are on the old total-capacity unit and are NOT comparable.
# Presence of the field is NOT a reliable signal (it appears on some earlier
# builds too via branch churn) — build START TIME vs the merge instant is the
# authoritative test. FILTER = finished AND started_at >= cutoff. Branch is NOT
# used (owner decision 2026-07-19): time alone decides validity.
PR263_MERGE_EPOCH = 1784344612  # 2026-07-18 11:16:52 +0800 in UTC epoch seconds
PR263 = "12a9a31f"

# Job aliases are OWNED BY grs_jenkins.py — imported, never copied.
#
# RCA 2026-08-03: this used to be a hand-maintained copy carrying a comment that
# said "must stay in sync with grs_jenkins.py". It did not stay in sync. The
# LOCALTEST job was renamed to LOCAL1 on 2026-08-01; grs_jenkins.py was updated,
# this copy was not, so every `_api` call for "localtest" hit a renamed path and
# Jenkins answered 404. That silently broke BOTH consumers for 2 days / 608
# consecutive poll failures: the poller (DB ingest — recoverable) and harvest.py
# (cold-store rescue — NOT recoverable past the job's 14-day artifact retention).
# A comment asking a human to mirror a constant is not a mechanism; importing is.
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from grs_jenkins import ALIASES as _JOB_ALIASES  # noqa: E402
    from grs_jenkins import _base_for as _gj_base_for  # noqa: E402
    from grs_jenkins import _NEW_BASE as _GJ_NEW_BASE  # noqa: E402
    from grs_jenkins import (
        _NEW_BASE_ALIAS_OVERRIDE as _GJ_NEW_BASE_OVERRIDE,
    )  # noqa: E402
    from grs_jenkins import get_json as _gj_get  # noqa: E402
    from grs_jenkins import params_from_actions as _gj_params  # noqa: E402
except ImportError:  # pragma: no cover - grs_jenkins should always be one dir up
    # Fail loud rather than resurrect a copy: a stale local table is the exact
    # defect this import exists to prevent.
    raise SystemExit(
        "db.py: cannot import ALIASES/_base_for from grs_tools/grs_jenkins.py — "
        "job routing lives THERE (single source). Fix the path; do not re-add a local copy."
    )


def _job_path(job):
    """Resolve `job` (alias key or raw substring) to its Jenkins job path.

    Dual-server routing (RCA 2026-09-09): this used to stop at ALIASES, which
    only knows the 148 (old-server) name. reg2's ACTUAL job on 217 (where
    grs_jenkins.py's own _base_for routes it) is named GRS-SYSTEMTEST-REG2,
    no hyphen -- an ALIASES-only lookup silently queried the disabled 148 job
    and 404'd, breaking the poller (507 consecutive failures) and harvest
    (cold-store rescue -- unrecoverable past retention) for every REG2 build
    since the 2026-09-08 migration. Apply the SAME override grs_jenkins._job()
    applies, imported not copied.
    """
    jp = _JOB_ALIASES.get(
        job, job if "/job/" in job or job.startswith("job/") else f"DEV/job/{job}"
    )
    if _gj_base_for(jp) == _GJ_NEW_BASE and job in _GJ_NEW_BASE_OVERRIDE:
        jp = _GJ_NEW_BASE_OVERRIDE[job]
    return jp if jp.startswith("job/") else "job/" + jp


def _api(url):
    import base64
    import json as _json
    import urllib.parse
    import urllib.request

    # Base URL is routed per-request (dual-server, see _job_path's docstring),
    # NEVER dbconn's static default -- that default is 148, permanently wrong
    # for any of the 6 migrated lanes. dbconn.jenkins() still owns credentials
    # (env / ~/.grs_db.conf), just not the base.
    base = _gj_base_for(url)
    _, user, pw = dbconn.jenkins()
    if "?" in url:
        head, _, q = url.partition("?")
        url = head + "?" + urllib.parse.quote(q, safe="=&")
    req = urllib.request.Request(f"{base}/{url}")
    req.add_header(
        "Authorization", f"Basic {base64.b64encode(f'{user}:{pw}'.encode()).decode()}"
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        return _json.loads(r.read())


def _list_builds(job):
    """Return [(number, building, result), ...] for a job, newest first."""
    data = _api(f"{_job_path(job)}/api/json?tree=builds[number,building,result]")
    return [
        (b["number"], b["building"], b.get("result")) for b in data.get("builds", [])
    ]


def _list_builds_full(job):
    """Return [{number, building, result, ts, branch, test_cases}] newest first.
    ts is epoch seconds (build start). Uses depth=2 to pull params without the
    bracketed-tree URL that 404s through some proxies."""
    data = _api(f"{_job_path(job)}/api/json?depth=2")
    out = []
    for b in data.get("builds", []):
        params = {
            p["name"]: p.get("value")
            for a in b.get("actions", [])
            for p in (a.get("parameters") or [])
            if p
        }
        out.append(
            {
                "number": b["number"],
                "building": b.get("building"),
                "result": b.get("result"),
                "ts": (b.get("timestamp") or 0) / 1000.0,
                "branch": params.get("branch", "?"),
                "test_cases": params.get("test_cases", ""),
            }
        )
    return out


def _cpu_valid(b):
    """A build's CPU data is trustworthy: finished AND started at/after PR263.
    Time is the sole criterion — branch is NOT used (owner decision 2026-07-19)."""
    return (not b["building"]) and b["ts"] >= PR263_MERGE_EPOCH


def cmd_candidates(a):
    """List, per job, the builds whose CPU data is valid (finished + started
    at/after PR263; branch ignored). Read-only. Use before import-all to see scope."""
    import time as _time

    print(f"CPU-valid cutoff = PR263 {PR263} merged 2026-07-18 11:16:52 +0800")
    print("rule: finished AND started_at >= cutoff  (time only; branch ignored)\n")
    grand = 0
    for job in a.jobs:
        builds = _list_builds_full(job)
        finished = [b for b in builds if not b["building"]]
        valid = [b for b in finished if _cpu_valid(b)]
        running = [b["number"] for b in builds if b["building"]]
        print(
            f"=== {job}: total={len(builds)} finished={len(finished)} "
            f"CPU-valid={len(valid)}"
            + (f"  (running, skipped: {running})" if running else "")
            + " ==="
        )
        for b in sorted(valid, key=lambda x: -x["number"]):
            t = _time.strftime("%m-%d %H:%M", _time.localtime(b["ts"]))
            print(
                f"  #{b['number']:<4} {str(b['result']):8} {t}  {b['test_cases'][:45]}"
            )
        # show what got excluded and why (transparency, per no-silent-caps rule)
        excluded = [b for b in finished if not _cpu_valid(b)]
        if excluded:
            print(
                f"  -- {len(excluded)} finished build(s) EXCLUDED (pre-PR263 CPU unit):"
            )
            for b in sorted(excluded, key=lambda x: -x["number"])[:100]:
                print(f"     #{b['number']:<4} {str(b['result']):8} (pre-PR263)")
        grand += len(valid)
        print()
    print(f"TOTAL CPU-valid builds across {len(a.jobs)} job(s): {grand}")
    return 0


def cmd_lanes(a):
    """Per lane: cold-store newest vs Jenkins newest — verdict IDLE or BEHIND(n).

    WHY THIS EXISTS (2026-08-12): db_health.py prints, per stale lane, "VERIFY:
    either that lane is genuinely idle, or harvest is silently missing it". It
    cannot answer that itself by design — it must not touch Jenkins, or it fails
    for the same reasons as the thing it watches. So the answer was left to a
    human, and a human (me) answered it by hand-piping `db.py candidates` through
    grep/head in front of the owner. A verification step that only exists as a
    remembered shell recipe is a step that gets skipped, mistyped, or performed on
    the wrong lane. This is that recipe, computed.

    Read-only: Jenkins + the local store. Never needs the DB, so it still works
    while PostgreSQL is down — which is exactly when the cold store matters most.
    """
    import time as _time

    jobs = list(a.jobs) if a.jobs else None
    if jobs is None:
        # Same lane list the daily harvest covers, imported rather than restated
        # (ledger 41: a copied constant rots — LOCALTEST->LOCAL1 cost 2 days).
        try:
            sys.path.insert(
                0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            import systest_db_harvest_daily as _daily

            jobs = list(_daily.JOBS)
        except Exception:
            jobs = ["reg", "reg2", "localtest", "local2"]

    store_root = os.environ.get("GRS_DB_STORE") or os.path.abspath(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "systest_db_store"
        )
    )

    print(f"[store] {store_root}")
    print(
        f"{'lane':<12} {'store':>7} {'jenkins':>8} {'running':>8}  verdict\n" + "-" * 62
    )
    behind_any = []
    for job in jobs:
        stored = []
        jd = os.path.join(store_root, job)
        if os.path.isdir(jd):
            stored = sorted(int(x) for x in os.listdir(jd) if x.isdigit())
        s_newest = stored[-1] if stored else None

        # Builds harvest already proved carry no iterations_*.ndjson. They can never
        # reach the DB, so counting them as a gap turns this verdict into noise
        # (it read "BEHIND 23" against a true gap of 6 before this was wired in).
        nodata = set()
        try:
            import json as _json

            with open(os.path.join(jd, "no_data.json"), encoding="utf-8") as f:
                nodata = {int(k) for k in _json.load(f)}
        except (OSError, ValueError, TypeError):
            pass

        try:
            builds = _list_builds_full(job)
        except Exception as e:  # a 404 here IS the renamed-job failure mode
            print(
                f"{job:<12} {str(s_newest):>7} {'ERROR':>8} {'-':>8}  JENKINS ERROR: {e}"
            )
            behind_any.append(job)
            continue

        finished = [b for b in builds if not b["building"]]
        running = [b["number"] for b in builds if b["building"]]
        j_newest = max((b["number"] for b in finished), default=None)

        # "Missing" counts only builds Jenkins still serves AND whose CPU data is
        # valid — the same rule harvest/import use, so this verdict cannot disagree
        # with what a pull would actually fetch.
        missing = sorted(
            (
                b["number"]
                for b in finished
                if _cpu_valid(b)
                and b["number"] not in stored
                and b["number"] not in nodata
            ),
            reverse=True,
        )
        if not missing:
            verdict = "IDLE — store is level with Jenkins"
            if nodata:
                verdict += f"  ({len(nodata)} build(s) have no ndjson — unharvestable)"
        else:
            newest_ts = max(
                (b["ts"] for b in finished if b["number"] in set(missing)), default=0
            )
            when = (
                _time.strftime("%m-%d %H:%M", _time.localtime(newest_ts))
                if newest_ts
                else "?"
            )
            verdict = f"BEHIND {len(missing)} — newest missing #{missing[0]} ({when})"
            behind_any.append(job)
        print(
            f"{job:<12} {str(s_newest):>7} {str(j_newest):>8} "
            f"{(str(running[0]) if running else '-'):>8}  {verdict}"
        )
        if missing:
            print(
                f"{'':<12} missing: {missing[:20]}{' ...' if len(missing) > 20 else ''}"
            )

    print()
    if behind_any:
        print(
            "NOT idle — harvest is genuinely behind on: " + " ".join(behind_any) + "\n"
            "  python grs_tools/system_test_db/harvest.py pull "
            + " ".join(behind_any)
            + "\n"
            "  then: python grs_tools/systest_db_harvest_daily.py   "
            "(refreshes the state db_health reads, and pushes the store)"
        )
    else:
        print(
            "every lane IDLE — a staleness alarm from db_health.py is a quiet lane,\n"
            "not a broken harvest. Nothing to do."
        )
    return 0


def _store_has_own_data(build_dir, number):
    """True iff this store directory holds a run of ITS OWN build, not a neighbour's.

    Layout is `<store>/<job>/<build>/iterations_<run_id>.ndjson.gz` and the run_id ends
    with the build number, so the filename alone settles it — no gunzip needed.

    Why it matters: a Jenkins backup zip routinely carries the PREVIOUS build's
    iterations file when a run dies before writing its own (workspace reuse, ledger 45).
    Such a directory exists in the store but has nothing of its own to ingest, and
    `harvest.py ingest-all` correctly skips it as already-ingested-under-another-run_id.
    Counting it as "store-only, recoverable" would tell the operator to run an ingest
    that provably does nothing — an alarm that cries wolf gets muted, which is the
    failure this whole toolchain exists to prevent.
    """
    import glob as _glob

    for path in _glob.glob(os.path.join(build_dir, "iterations_*.ndjson.gz")):
        stem = os.path.basename(path)[len("iterations_") : -len(".ndjson.gz")]
        tail = stem.rsplit("-", 1)[-1]
        if tail.isdigit() and int(tail) == number:
            return True
    return False


def _store_build_is_feature(build_dir):
    """True/False/None — is the stored run a feature build (client_version major >= 200)?

    ingest gates those BY DESIGN, so counting them as a recoverable gap would send the
    operator to an `ingest-all` that provably skips them (REG 148/149/150 are 211.x and
    were purged deliberately on 2026-07-24). Returns None when it cannot tell, so the
    caller can say "unknown" rather than mislabel.

    The major-version threshold is imported from ingest, never copied (ledger 41).
    """
    import glob as _glob
    import gzip as _gzip
    import re as _re

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from ingest import _is_feature_build_version
    except Exception:
        return None
    for path in _glob.glob(os.path.join(build_dir, "iterations_*.ndjson.gz")):
        try:
            with _gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                blob = f.read(400000)
        except OSError:
            continue
        versions = _re.findall(r'"client_version"\s*:\s*"([^"]+)"', blob)
        if any(_is_feature_build_version(v) for v in versions):
            return True
        if versions:
            return False
    return None


def _store_build_is_allskip(build_dir):
    """True/False/None — did the stored run SKIP every case?

    Same reason as _store_build_is_feature: ingest gates all-skip runs BY DESIGN
    (a run that measured nothing dilutes every rollup — REG 358), so labelling one
    "store-only, recoverable" points the operator at an `ingest-all` that provably
    skips it. Returns None when it cannot tell, so the caller says "unknown".

    Reads the WHOLE file, unlike the feature probe's 400KB peek: a truncated view
    whose visible statuses are all SKIP would mislabel a run with a later PASS.
    The predicate itself is imported from ingest, never copied (ledger 41).
    """
    import glob as _glob
    import gzip as _gzip
    import re as _re

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from ingest import is_all_skip
    except Exception:
        return None
    for path in _glob.glob(os.path.join(build_dir, "iterations_*.ndjson.gz")):
        statuses = []
        try:
            with _gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    statuses += _re.findall(r'"test_status"\s*:\s*"([^"]+)"', line)
        except OSError:
            continue
        if statuses:
            return is_all_skip(statuses)
    return None


def _store_build_is_all_excluded(build_dir):
    """True/False/None -- does the stored run hold NOTHING ingest would keep?

    ingest drops every `test` record whose test_case is in EXCLUDED_TEST_NAMES (the STEER-06 POC
    case) and raises NoData (rc=3) when no test record is left -- whether the file had only
    excluded cases or no test record at all (its message says "every test_case ... EXCLUDED" for
    both). Such a build is not recoverable, yet it looked like "store-only" forever (mac2-ssh 37,
    57, 124, 126, 153, reported by git44 2026-10-20) and kept the SYSTEST_DB_GAPS sentinel up.
    Returns None when it cannot tell (no run record / unreadable), so the caller says "unknown".

    The name set is imported from ingest, never copied (ledger 41), and the rule is the same one
    ingest applies: look at `test` records only.
    """
    import glob as _glob
    import gzip as _gzip
    import json as _json

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from ingest import EXCLUDED_TEST_NAMES
    except Exception:
        return None
    for path in _glob.glob(os.path.join(build_dir, "iterations_*.ndjson.gz")):
        names, has_run = set(), False
        try:
            with _gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"record_type"' not in line:
                        continue
                    try:
                        obj = _json.loads(line)
                    except ValueError:
                        continue
                    if obj.get("record_type") == "run":
                        has_run = True
                    elif obj.get("record_type") == "test":
                        names.add(obj.get("test_case"))
        except OSError:
            continue
        if has_run:
            # ingest raises NoData (rc=3) when no non-excluded `test` record is left; that covers
            # a file with ONLY excluded cases and a file with a run record and no test record at
            # all (mac2-ssh 37/57/124/126/153 are the latter: the run died before any case logged).
            return not (names - set(EXCLUDED_TEST_NAMES))
    return None


def _db_runs_by_job():
    """{jenkins_job: {build_number: started_at_epoch_or_None}} for every test_run row.

    Deliberately NOT filtered by lane: the DB keeps whatever job string was current when
    a run was ingested, so a RENAMED job leaves rows under the old name. Filtering by the
    lane's present-day path would silently under-count those and report perfectly-ingested
    builds as missing (ledger 41: LOCALTEST -> LOCAL1). cmd_gaps therefore claims rows by
    tail and reports any job string no lane claimed, so the next rename surfaces as
    "unclaimed DB rows" instead of a phantom gap.

    started_at comes along because it is the ONLY surviving clock for a build Jenkins has
    already deleted: build numbers rise with time, so a dated neighbour on each side brackets
    a vanished build's real date (see _bracket_ts).
    """
    conn = dbconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT jenkins_job, jenkins_build, started_at FROM test_run "
                "WHERE jenkins_build IS NOT NULL"
            )
            out = {}
            for job, build, started in cur.fetchall():
                ts = started.timestamp() if started is not None else None
                d = out.setdefault(job or "?", {})
                if d.get(build) is None:
                    d[build] = ts
            return out
    finally:
        conn.close()


def _db_build_times():
    """{jenkins_job: {build_number: [started_at_epoch, ...]}} -- EVERY row, not just the
    first. A build number alone is not an identity (a job whose counter restarts reuses
    numbers), so the audit needs all start times to tell "this build" from "an old build
    that happens to share its number"."""
    conn = dbconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT jenkins_job, jenkins_build, extract(epoch FROM started_at) "
                "FROM test_run WHERE jenkins_build IS NOT NULL"
            )
            out = {}
            for job, build, epoch in cur.fetchall():
                out.setdefault(job or "?", {}).setdefault(build, []).append(
                    float(epoch) if epoch is not None else None
                )
            return out
    finally:
        conn.close()


def _collided_builds(db_times, jenkins):
    """Build numbers Jenkins serves whose only DB rows belong to a DIFFERENT execution.

    `db_times` is {build: [start epoch, ...]} for one lane, `jenkins` is
    {build: {"ts": ...}}. A build is collided when the DB holds its number but no stored
    run starts within _SAME_BUILD_WINDOW_S of the Jenkins build -- the shape of REG
    175-183 (2026-10): July's rows kept the numbers, so the new builds read as
    "already in DB" and their results never arrived."""
    out = set()
    for n, times in db_times.items():
        b = jenkins.get(n)
        if not b or not b.get("ts"):
            continue  # Jenkins forgot it: the number is all we have, trust it
        known = [t for t in times if t]
        if known and not any(abs(t - b["ts"]) <= _SAME_BUILD_WINDOW_S for t in known):
            out.add(n)
    return out


def _store_build_ts(job_dir):
    """{build_number: build_ts} from each harvested build's harvest.json.

    build_ts is the BUILD's start time (copied from Jenkins at harvest time), not the
    harvest time — so it keeps dating a build long after Jenkins has dropped the record.
    """
    import glob as _glob
    import json as _json

    out = {}
    for hj in _glob.glob(os.path.join(job_dir, "*", "harvest.json")):
        try:
            with open(hj, encoding="utf-8") as f:
                d = _json.load(f)
            n, ts = int(d.get("build") or 0), float(d.get("build_ts") or 0)
        except (OSError, ValueError, TypeError):
            continue
        if n and ts:
            out[n] = ts
    return out


def _harvest_since(job_dir):
    """When the cold-store harvest first covered this lane (earliest harvested_at), or None.

    This is the boundary that makes "absent from the store" mean something. After it, harvest
    downloaded and inspected EVERY finished CPU-valid build on every run, so a build it did
    not store had nothing to store. Before it, absence proves nothing.

    Errs LATE by design: --replace rewrites harvest.json and moves harvested_at forward, so
    this can only ever under-claim coverage — pushing builds into `unknowable` rather than
    silently blessing them as `examined~`.
    """
    import glob as _glob
    import json as _json

    oldest = None
    for hj in _glob.glob(os.path.join(job_dir, "*", "harvest.json")):
        try:
            with open(hj, encoding="utf-8") as f:
                ts = float(_json.load(f).get("harvested_at") or 0)
        except (OSError, ValueError, TypeError):
            continue
        if ts and (oldest is None or ts < oldest):
            oldest = ts
    return oldest


def _bracket_ts(n, dated):
    """(lo_ts, hi_ts) bounding build n's start time, from its nearest DATED neighbours.

    Jenkins build numbers are monotonic in time, so a dated build below n started before it
    and a dated build above n started after it. That is the only way left to date a build
    whose Jenkins record is gone — and dating it is what separates "aged out before this
    pipeline existed" (not our loss) from "lost on our watch" (a real, permanent loss).
    Returns None on an open side (nothing dated below / above n).

    `dated` must be a sorted list of (number, ts).
    """
    import bisect

    nums = [x[0] for x in dated]
    i = bisect.bisect_left(nums, n)
    lo = dated[i - 1][1] if i > 0 else None
    hi = dated[i][1] if i < len(dated) else None
    return lo, hi


def cmd_gaps(a):
    """Per lane, classify EVERY build number in the known window across three sources.

    WHY THIS EXISTS (2026-08-13): a build was found missing from the results DB only
    AFTER Jenkins' retention had deleted it, so it is gone for good. Nothing was
    watching for that: `lanes` compares only the NEWEST build number, `verify` only
    checks that what IS in the store can be replayed, and the harvest staleness alarm
    fires on the age of the newest build. A hole in the MIDDLE — a finished build that
    was never harvested and never ingested — is invisible to all three until its
    artifacts age out (REG/REG-02 7 days, LOCAL* 14).

    Sources: Jenkins (what still exists), the cold store (what we rescued), the DB
    (what was ingested). Read-only. Degrades: if the DB is unreachable the store and
    Jenkins halves still report, which is the case where this matters most.

    Classes, per build number:
      in-db        already ingested — nothing to do
      store-only   in the cold store but NOT in the DB -> recoverable right now
      rescuable    still served by Jenkins, absent from store AND DB -> pull it before
                   retention deletes it. NOTE it MAY hold nothing: whether a build has an
                   iterations ndjson can only be learned by downloading it, which is what
                   `pull` does (2026-08-16: all three REG-02 builds flagged here on a 6.7d
                   "deadline" turned out to carry no ndjson at all). So this class means
                   "undetermined, and the clock is running", not "results at risk".
      no-data      harvest proved it carries no iterations ndjson -> nothing to ingest
      feature-gated / allskip-gated / excluded-gated
                   in the store, absent from the DB, and ingest refuses it BY DESIGN
                   (feature/early-stage major, every case SKIP, or only cases in
                   EXCLUDED_TEST_NAMES, ingest rc=3). NOT a gap: calling
                   these "recoverable" sends the operator to an ingest that does nothing.

    A build with no record in ANY source is DATED first, by bracketing it between its
    nearest dated neighbours (_bracket_ts), and only then classified:

      pre-cutoff~   ran entirely before the PR263 CPU cutoff -> excluded by design
      examined~     ran while the daily harvest was already covering this lane, so harvest
                    DID download and inspect it and chose not to store it => it carried no
                    iterations ndjson => nothing was ever there to lose
      unknowable    ran before harvest covered this lane; "absent from the store" proves
                    nothing about it, and Jenkins has since deleted the evidence

    Why the dating step exists (2026-08-16): before it, all of the above collapsed into one
    "unknown-gap: needs judgement" count, printed as `sorted(unknown)[:30]` — the OLDEST
    thirty. On REG that meant 84 numbers summarised by builds 42..71 from before the DB
    existed, so a build that fell through the cracks YESTERDAY was unreachable behind three
    dozen pre-history numbers that will never matter (same shape as ledger 45: sorting
    ascending and slicing shows the least relevant end).

    Why there is NO "LOST" class: nothing on disk can prove a loss for a build Jenkins has
    deleted. "No ndjson, nothing to lose" and "results lost" leave the identical trace — an
    absence. The first draft of this audit did label them LOST and got 68, which the harvest
    log immediately disproved (local2 #10/#11/#13/#16/#17 are in it, 2026-08-03, as
    "no iterations_*.ndjson — nothing to harvest"). An audit that over-counts losses gets
    muted, which is the failure this whole toolchain exists to prevent, so the undecidable
    case is NAMED undecidable. `examined~` is the one inference that is safe, and it is
    safe only because harvest inspects EVERY finished CPU-valid build on every run.
    """
    import time as _time

    jobs = list(a.jobs) if a.jobs else None
    retention = {}
    if jobs is None:
        try:
            sys.path.insert(
                0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            import systest_db_harvest_daily as _daily

            jobs = list(_daily.JOBS)
            retention = dict(_daily.RETENTION_DAYS)
        except Exception:
            jobs = ["reg", "reg2", "localtest", "local2"]
    if not retention:
        try:
            import systest_db_harvest_daily as _daily2

            retention = dict(_daily2.RETENTION_DAYS)
        except Exception:
            retention = {"reg": 7, "reg2": 7, "localtest": 14, "local2": 14}

    store_root = os.environ.get("GRS_DB_STORE") or os.path.abspath(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "systest_db_store"
        )
    )
    print(f"[store] {store_root}")
    now = _time.time()
    grand_rescuable, grand_store_only, grand_unknowable = [], [], []

    # Historical job names still present in the DB. A lane claims rows whose jenkins_job
    # tail matches its CURRENT path tail or any tail listed here. Sourced from the data,
    # not guessed — cmd_gaps prints unclaimed job strings so a new rename is visible.
    _HISTORICAL_TAILS = {
        "localtest": ("GRS-SYSTEMTEST-LOCALTEST",),
        "local1": ("GRS-SYSTEMTEST-LOCALTEST",),
        # Renamed at some point from "REG-02" (hyphenated) to "REG2" -- rows
        # ingested under the old job path are real, already-in-db data, not a
        # gap; without this the audit misreports them as "store-only" /
        # "unclaimed" (2026-09-16 RCA, confirmed live: all 10 flagged builds
        # already had real test_run rows under jenkins-DEV-GRS-SYSTEMTEST-REG-02-*).
        "reg2": ("GRS-SYSTEMTEST-REG-02",),
    }
    try:
        db_by_job = _db_runs_by_job()
        db_times_by_job = _db_build_times()
    except Exception as e:
        print(f"[db] UNREACHABLE ({e}) — store/Jenkins halves only")
        db_by_job = None
        db_times_by_job = {}
    claimed_job_strings = set()
    grand_collided, grand_success_nodata = [], []

    for job in jobs:
        jd = os.path.join(store_root, job)
        stored = set()
        if os.path.isdir(jd):
            stored = {int(x) for x in os.listdir(jd) if x.isdigit()}
        nodata = set()
        try:
            import json as _json

            with open(os.path.join(jd, "no_data.json"), encoding="utf-8") as f:
                nodata = {int(k) for k in _json.load(f)}
        except (OSError, ValueError, TypeError):
            pass

        in_db, db_srcs, db_ts = None, [], {}
        if db_by_job is not None:
            accept = (_job_path(job).rsplit("/", 1)[-1],) + _HISTORICAL_TAILS.get(
                job, ()
            )
            in_db = set()
            for db_job, builds in db_by_job.items():
                if db_job.rsplit("/", 1)[-1] in accept:
                    in_db |= set(builds)
                    db_ts.update({n: t for n, t in builds.items() if t})
                    db_srcs.append(f"{db_job}({len(builds)})")
                    claimed_job_strings.add(db_job)

        try:
            jenkins = {b["number"]: b for b in _list_builds_full(job)}
        except Exception as e:
            print(f"=== {job}: JENKINS ERROR ({e}) — cannot classify ===")
            continue

        # A DB row only counts as THIS build if it started when the Jenkins build did.
        collided = set()
        if in_db is not None:
            lane_times = {}
            for db_job, builds in db_times_by_job.items():
                if db_job.rsplit("/", 1)[-1] in accept:
                    for n, ts_list in builds.items():
                        lane_times.setdefault(n, []).extend(ts_list)
            collided = _collided_builds(lane_times, jenkins)
            in_db -= collided
            if collided:
                grand_collided.append((job, sorted(collided)))
        # SUCCESS builds that produced no iterations ndjson: pytest always writes one, so
        # this means the recorder or the artifact archive broke, not "nothing to ingest".
        # Only builds Jenkins still serves (the audit would otherwise nag about history).
        success_nodata = sorted(
            n
            for n in nodata
            if jenkins.get(n)
            and jenkins[n].get("result") == "SUCCESS"
            and _cpu_valid(jenkins[n])
        )
        if success_nodata:
            grand_success_nodata.append((job, success_nodata))

        # Every clock we still have for this lane, weakest source first so the most
        # authoritative (Jenkins' own build start) wins on collision.
        store_ts, harvest_since = _store_build_ts(jd), _harvest_since(jd)
        dated = {}
        dated.update(db_ts)
        dated.update(store_ts)
        dated.update({n: b["ts"] for n, b in jenkins.items() if b.get("ts")})
        dated_sorted = sorted(dated.items())

        universe = set(stored) | set(nodata) | set(jenkins) | set(in_db or ())
        if not universe:
            print(f"=== {job}: no data in any source ===")
            continue
        lo, hi = min(universe), max(universe)

        counts = {
            "in-db": 0,
            "store-only": 0,
            "feature-gated": 0,
            "allskip-gated": 0,
            "excluded-gated": 0,
            "stale-artifact": 0,
            "pre-cutoff": 0,
            "rescuable": 0,
            "no-data": 0,
            # gone from every source, then dated (see _bracket_ts)
            "pre-cutoff~": 0,
            "examined~": 0,
            "unknowable": 0,
        }
        rescuable, store_only, unknowable = [], [], []
        for n in range(lo, hi + 1):
            b = jenkins.get(n)
            if b and b.get("building"):
                continue  # unfinished — never ingested by design
            if in_db is not None and n in in_db:
                counts["in-db"] += 1
            elif n in stored:
                bdir = os.path.join(jd, str(n))
                if not _store_has_own_data(bdir, n):
                    counts["stale-artifact"] += 1
                elif _store_build_is_feature(bdir):
                    counts["feature-gated"] += 1
                elif _store_build_is_allskip(bdir):
                    counts["allskip-gated"] += 1
                elif _store_build_is_all_excluded(bdir):
                    counts["excluded-gated"] += 1
                else:
                    counts["store-only"] += 1
                    store_only.append(n)
            elif n in nodata:
                counts["no-data"] += 1
            elif b is not None and not _cpu_valid(b):
                # Finished but started before the PR263 CPU-unit cutoff: import-all and
                # harvest both exclude these by design, so they are not a gap.
                counts["pre-cutoff"] += 1
            elif b is not None:
                counts["rescuable"] += 1
                days_left = (retention.get(job, 7)) - (now - b["ts"]) / 86400.0
                rescuable.append((n, days_left))
            else:
                # No record anywhere. Date it before judging it: an undated gap is
                # unclassifiable, and a pile of unclassifiable numbers is exactly what
                # buried the decidable ones.
                b_lo, b_hi = _bracket_ts(n, dated_sorted)
                if b_hi is not None and b_hi < PR263_MERGE_EPOCH:
                    counts["pre-cutoff~"] += 1
                elif harvest_since and b_lo is not None and b_lo >= harvest_since:
                    # Harvest was covering this lane and inspects every finished build, so
                    # it downloaded this one and stored nothing => no iterations ndjson.
                    counts["examined~"] += 1
                else:
                    counts["unknowable"] += 1
                    unknowable.append((n, b_lo, b_hi))

        if a.build is not None:
            # Single-build mode: answer "why is build N missing?" for one number, per lane.
            n = a.build
            b = jenkins.get(n)
            if n < lo or n > hi:
                verdict = f"outside this lane's known window #{lo}..#{hi}"
            elif in_db is not None and n in in_db:
                verdict = "IN DB — nothing missing"
            elif n in stored and _store_has_own_data(os.path.join(jd, str(n)), n):
                # The two ingest gates make "in store, not in DB" mean DELIBERATE,
                # not recoverable — saying "recoverable" here sent the operator to an
                # ingest-all that provably does nothing (REG 358 all-skip, REG
                # 148/149/150 feature builds).
                bdir = os.path.join(jd, str(n))
                if _store_build_is_feature(bdir):
                    verdict = (
                        "feature build (early-stage major) — gated at ingest "
                        "BY DESIGN, not a gap"
                    )
                elif _store_build_is_allskip(bdir):
                    verdict = (
                        "every case SKIP (run measured nothing) — gated at ingest "
                        "BY DESIGN, not a gap"
                    )
                elif _store_build_is_all_excluded(bdir):
                    verdict = (
                        "only cases in ingest's EXCLUDED_TEST_NAMES (rc=3, nothing of its "
                        "own to ingest) — gated BY DESIGN, not a gap"
                    )
                else:
                    verdict = (
                        "in cold store, NOT in DB -> recoverable: harvest.py ingest-all"
                    )
            elif n in stored:
                verdict = (
                    "store dir holds a NEIGHBOUR's iterations file (stale Jenkins zip, "
                    "ledger 45) — this build produced no run of its own, nothing to ingest"
                )
            elif n in nodata:
                verdict = "no iterations ndjson (nothing to ingest, by design)"
            elif b is not None:
                left = (retention.get(job, 7)) - (now - b["ts"]) / 86400.0
                verdict = (
                    f"still in Jenkins (result={b.get('result')}), absent from store AND DB "
                    f"-> RESCUE within {left:.1f}d: harvest.py pull {job}"
                )
            else:
                b_lo, b_hi = _bracket_ts(n, dated_sorted)
                when = (
                    "ran between "
                    + (
                        _time.strftime("%Y-%m-%d %H:%M", _time.localtime(b_lo))
                        if b_lo
                        else "?"
                    )
                    + " and "
                    + (
                        _time.strftime("%Y-%m-%d %H:%M", _time.localtime(b_hi))
                        if b_hi
                        else "?"
                    )
                )
                if b_hi is not None and b_hi < PR263_MERGE_EPOCH:
                    era = "pre-PR263 CPU cutoff — excluded by design, NOT a loss"
                elif harvest_since and b_lo is not None and b_lo >= harvest_since:
                    era = (
                        "harvest was covering this lane and inspects every finished build, "
                        "so it saw this one and stored nothing => no iterations ndjson, "
                        "nothing was there to lose"
                    )
                else:
                    era = (
                        "ran before harvest covered this lane — UNKNOWABLE, and Jenkins has "
                        "deleted the evidence; do not call it a loss either way"
                    )
                verdict = f"GONE from Jenkins, absent from store AND DB; {when}; {era}"
            print(f"{job:<12} #{n}: {verdict}")
            continue

        print(
            f"=== {job}: window #{lo}..#{hi}  "
            + "  ".join(f"{k}={v}" for k, v in counts.items())
            + ("" if in_db is not None else "  (db unknown)")
            + " ==="
        )
        if db_srcs:
            print(f"  db rows claimed from: {', '.join(sorted(db_srcs))}")
        if rescuable:
            soon = sorted(rescuable, key=lambda t: t[1])
            print(
                "  RESCUABLE NOW (in Jenkins, not in store, not in DB) — "
                f"soonest to expire: "
                + ", ".join(f"#{n}({d:.1f}d left)" for n, d in soon[:10])
            )
            grand_rescuable.append(job)
        if store_only:
            print(f"  store-only (ingest recovers these): {sorted(store_only)[:20]}")
            grand_store_only.append(job)
        if unknowable:
            # NEWEST first: the recent end is the only end anyone can still act on.
            # Pre-history is counted in the header and deliberately not listed.
            newest = sorted(unknowable, key=lambda t: -t[0])
            since = (
                _time.strftime("%Y-%m-%d", _time.localtime(harvest_since))
                if harvest_since
                else "NEVER"
            )
            print(
                f"  unknowable ({len(unknowable)}) — ran before harvest covered this lane "
                f"(since {since}); newest first:"
            )
            for n, b_lo, b_hi in newest[:10]:
                span = (
                    (
                        _time.strftime("%m-%d %H:%M", _time.localtime(b_lo))
                        if b_lo
                        else "?"
                    )
                    + " .. "
                    + (
                        _time.strftime("%m-%d %H:%M", _time.localtime(b_hi))
                        if b_hi
                        else "?"
                    )
                )
                print(f"      #{n:<5} ran between {span}")
            if len(newest) > 10:
                print(f"      ... and {len(newest) - 10} older (use --build N for one)")
            grand_unknowable.append((job, len(unknowable)))

    print()
    if db_by_job is not None:
        unclaimed = sorted(set(db_by_job) - claimed_job_strings)
        if unclaimed:
            print(
                "UNCLAIMED DB job strings — rows no lane matched. Either a job was renamed\n"
                "(add its old tail to _HISTORICAL_TAILS) or a lane is missing from this audit;\n"
                "until then those builds cannot be reconciled: "
                + ", ".join(f"{j}({len(db_by_job[j])})" for j in unclaimed)
            )
    if grand_rescuable:
        print(
            "ACT NOW — these lanes have finished builds Jenkins still serves that are in\n"
            "neither the store nor the DB. Their contents are UNDETERMINED until pulled (many\n"
            "turn out to carry no iterations ndjson), and after retention that stays true\n"
            "forever, so pull settles it either way:\n"
            "  python grs_tools/system_test_db/harvest.py pull "
            + " ".join(grand_rescuable)
        )
    if grand_store_only:
        print(
            "Recoverable without a deadline (already rescued, just not ingested):\n"
            "  python grs_tools/system_test_db/harvest.py ingest-all"
        )
    if grand_unknowable:
        print(
            "UNKNOWABLE (not a loss claim) — these builds ran before harvest covered their\n"
            "lane and Jenkins has deleted them, so 'absent from the store' says nothing about\n"
            "whether they ever had results. Nothing to act on and nothing to recover; listed\n"
            "so the count is never mistaken for either zero or a loss: "
            + ", ".join(f"{j}={n}" for j, n in grand_unknowable)
            + "\n  python grs_tools/system_test_db/db.py gaps --build <N>   (dates one build)"
        )
    if grand_collided:
        print(
            "NUMBER COLLISION — Jenkins builds whose number is held in the DB by an OLDER run\n"
            "(counter restart). `import-all` now ingests these; if they stay listed the poller\n"
            "is running old code: "
            + ", ".join(f"{j}={n[:12]}" for j, n in grand_collided)
        )
    if grand_success_nodata:
        print(
            "SUCCESS WITHOUT DATA — finished SUCCESS builds with no iterations ndjson (the\n"
            "recorder or the artifact archive broke; not 'nothing to ingest'): "
            + ", ".join(f"{j}={n[:12]}" for j, n in grand_success_nodata)
        )
    actionable = bool(
        grand_rescuable or grand_store_only or grand_collided or grand_success_nodata
    )
    if not actionable:
        print(
            "no actionable gap: every finished build is either in the DB or accounted for."
        )
    # --check is for schedulers: non-zero means "an operator must look".
    return 2 if (getattr(a, "check", False) and actionable) else 0


def _ingested_builds(job):
    """Return {build number: [stored started_at epoch, ...]} already in the DB for
    this job. Matches on the stored jenkins_job tail (DB stores
    'DEV/GRS-SYSTEMTEST-REG'; alias 'reg' resolves to a path ending in that name).
    Used to skip BEFORE downloading, so a 5-minute poll doesn't re-fetch megabytes
    of already-ingested artifacts. The start time is kept because a build NUMBER
    alone is not an identity: when a job's counter restarts (REG after the Jenkins
    server move), new build 175 collides with July's build 175 and was skipped as
    "already in DB" forever, so R143 REG builds 175-183 never reached the DB."""
    tail = _job_path(job).rsplit("/", 1)[-1]  # e.g. 'GRS-SYSTEMTEST-REG'
    conn = dbconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT jenkins_build, extract(epoch FROM started_at) FROM test_run "
                "WHERE jenkins_job LIKE %s AND jenkins_build IS NOT NULL",
                (f"%{tail}",),
            )
            have = {}
            for num, epoch in cur.fetchall():
                have.setdefault(num, []).append(float(epoch) if epoch is not None else None)
            return have
    finally:
        conn.close()


# A stored run counts as THIS Jenkins build only if its start is within this window
# of the build's own timestamp (ndjson start vs Jenkins queue/start differ by minutes;
# a reused build number differs by weeks).
_SAME_BUILD_WINDOW_S = 6 * 3600


def _is_ingested(have, build):
    """True if the DB already holds this exact Jenkins build (number AND start time)."""
    for epoch in have.get(build["number"], []):
        if epoch is None or abs(epoch - build["ts"]) <= _SAME_BUILD_WINDOW_S:
            return True
    return False


def cmd_import_all(a):
    """Fetch+ingest builds of each job.

    Filters, in order:
      1. RUNNING builds (building=true) are NEVER imported — unfinished data.
      2. Builds already in the DB are skipped BEFORE download (run_id dedupe key),
         unless --replace — so a frequent poll is cheap (no re-download).
      3. By default only CPU-VALID builds (finished + started at/after PR263)
         are imported, since pre-PR263 CPU metrics use the old total-capacity
         unit and are not comparable. Pass --all to import every finished build.
    A finished build with no ndjson (infra died pre-test) just reports
    "no artifacts" and is counted as no-data — result state is NOT pre-filtered.
    """
    total_ok = total_running = total_pre = total_nores = total_dup = 0
    for job in a.jobs:
        builds = _list_builds_full(job)
        running = [b["number"] for b in builds if b["building"]]
        finished = [b for b in builds if not b["building"]]
        if running:
            print(
                f"\n=== {job}: {len(builds)} builds; skipping {len(running)} RUNNING: {running} ==="
            )
            total_running += len(running)
        else:
            print(f"\n=== {job}: {len(builds)} builds, {len(finished)} finished ===")

        if a.all:
            chosen = finished
        else:
            chosen = [b for b in finished if _cpu_valid(b)]
            pre = len(finished) - len(chosen)
            if pre:
                print(
                    f"[filter] {pre} pre-PR263 build(s) excluded (old CPU unit; --all to include)"
                )
                total_pre += pre

        # Skip already-ingested builds BEFORE downloading (unless --replace).
        if not a.replace:
            have = _ingested_builds(job)
            before = len(chosen)
            chosen = [b for b in chosen if not _is_ingested(have, b)]
            dup = before - len(chosen)
            if dup:
                print(f"[dedupe] {dup} build(s) already in DB, not re-downloading")
                total_dup += dup

        chosen = sorted(chosen, key=lambda x: -x["number"])
        if a.limit:
            chosen = chosen[: a.limit]

        for b in chosen:
            num, result = b["number"], b["result"]
            print(f"\n--- {job} #{num} (result={result}) ---")
            ns = argparse.Namespace(
                job=job, build=str(num), dest=None, release=a.release, replace=a.replace
            )
            try:
                rc = cmd_fetch(ns)
                if rc == 0:
                    total_ok += 1
                elif rc == 5:
                    # ingest ran but the run_id was already in the DB — e.g. a
                    # re-run/ABORTED build whose archived artifact carries an
                    # EARLIER build's ndjson (Jenkins workspace reuse). Not a new
                    # ingest; the run_id UNIQUE key correctly skipped the write.
                    total_dup += 1
                else:
                    total_nores += 1
            except Exception as e:  # keep going across the batch
                print(f"[warn] {job} #{num} failed: {e}")
                total_nores += 1
    print(
        f"\n[import-all] ingested={total_ok}  already-in-db={total_dup}  "
        f"pre-PR263-excluded={total_pre}  running-skipped={total_running}  "
        f"no-artifact/error={total_nores}"
    )
    return 0


# ─────────────────────────── fix-run ───────────────────────────


def cmd_fix_run(a):
    """Correct a single run's product_version and/or release label. Use when a
    build recorded the wrong version (e.g. a residual client read at setup) and
    the report miscategorised it. Matches the run by job alias + build number.

    The release label must already exist (created by a prior ingest). Prints the
    before/after row so the correction is auditable."""
    conn = dbconn.connect()
    tail = _job_path(a.job).rsplit("/", 1)[-1]  # e.g. GRS-SYSTEMTEST-LOCALTEST
    with conn.cursor() as cur:
        cur.execute(
            """SELECT run.id, run.run_id, run.product_version, r.label
               FROM test_run run LEFT JOIN release r ON r.id=run.release_id
               WHERE run.jenkins_job LIKE %s AND run.jenkins_build=%s""",
            (f"%{tail}", a.build),
        )
        row = cur.fetchone()
        if not row:
            print(f"[error] no run found for job~{tail} build={a.build}")
            conn.close()
            return 2
        run_pk, run_id, old_ver, old_label = row
        print(f"before: run_id={run_id}  version={old_ver}  release={old_label}")

        sets, params = [], []
        if a.version:
            sets.append("product_version=%s")
            params.append(a.version)
        if a.release:
            cur.execute("SELECT id FROM release WHERE label=%s", (a.release,))
            rel = cur.fetchone()
            if not rel:
                print(
                    f"[error] release label '{a.release}' does not exist (ingest a build for it first)"
                )
                conn.close()
                return 2
            sets.append("release_id=%s")
            params.append(rel[0])
        if not sets:
            print("[noop] nothing to change — pass --version and/or --release")
            conn.close()
            return 1
        params.append(run_pk)
        cur.execute(f"UPDATE test_run SET {', '.join(sets)} WHERE id=%s", params)
        conn.commit()

        cur.execute(
            """SELECT run.product_version, r.label
               FROM test_run run LEFT JOIN release r ON r.id=run.release_id
               WHERE run.id=%s""",
            (run_pk,),
        )
        new_ver, new_label = cur.fetchone()
        print(f"after:  run_id={run_id}  version={new_ver}  release={new_label}")
    conn.close()
    return 0


def cmd_reclaim_run(a):
    """Detach a stale test_run row from a build number Jenkins has since
    reused. run_id is derived only from job+build (Jenkins' own BUILD_TAG
    convention), never from a timestamp, so when a job's build counter gets
    reset (job recreated), the SAME run_id string can end up meaning two
    unrelated executions months apart — the older row keeps masquerading as
    "the" current build N, and `ingest.py` (2026-09-17 fix, same RCA) now
    auto-detects and self-heals this on future ingests, but an ALREADY
    stuck historical row (like MAC2 builds 56-62, confirmed via live Jenkins
    timestamps not matching the DB's stored started_at) needs this one-time
    manual detach. Renames run_id with an -orig<YYYYMMDD> suffix and nulls
    jenkins_build/build_url so the slot is free for a real re-ingest of the
    current build N. Matches by job alias + build number. Prints before/after
    for audit."""
    conn = dbconn.connect()
    tail = _job_path(a.job).rsplit("/", 1)[-1]
    with conn.cursor() as cur:
        cur.execute(
            """SELECT run.id, run.run_id, run.started_at
               FROM test_run run
               WHERE run.jenkins_job LIKE %s AND run.jenkins_build=%s""",
            (f"%{tail}", a.build),
        )
        row = cur.fetchone()
        if not row:
            print(f"[error] no run found for job~{tail} build={a.build}")
            conn.close()
            return 2
        run_pk, old_run_id, started_at = row
        print(
            f"before: run_id={old_run_id}  jenkins_build={a.build}  started_at={started_at}"
        )
        cur.execute(
            """UPDATE test_run
               SET run_id = run_id || '-orig' || to_char(started_at, 'YYYYMMDD'),
                   jenkins_build = NULL, build_url = NULL
               WHERE id=%s
               RETURNING run_id""",
            (run_pk,),
        )
        new_run_id = cur.fetchone()[0]
        conn.commit()
        print(f"after:  run_id={new_run_id}  jenkins_build=NULL  build_url=NULL")
    conn.close()
    return 0


# ─────────────────────────── growth ───────────────────────────


def cmd_check_partial(a):
    """Find test_run rows that have 0 test_result rows (partial / interrupted writes).
    These are orphan shells that the old skip-guard silently ignores — future fetches
    would report 'already ingested' but the run never actually lands in any report.
    With --fix, re-fetches + re-ingests each one (equivalent to fetch --replace)."""
    conn = dbconn.connect()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT run.run_id, run.jenkins_job, run.jenkins_build, run.started_at
            FROM test_run run
            WHERE (SELECT count(*) FROM test_result WHERE run_id=run.id) = 0
              AND (run.n_pass + run.n_fail + run.n_skip) > 0
            ORDER BY run.started_at DESC NULLS LAST
        """)
        rows_ = cur.fetchall()
    conn.close()

    if not rows_:
        print(
            "[ok] no partial writes found — every test_run has at least one test_result row"
        )
        return 0

    print(f"[warn] {len(rows_)} test_run row(s) with 0 test_result (partial write):")
    for run_id, job, build, started in rows_:
        print(f"  {run_id}  job={job}  build={build}  started={started}")

    if not a.fix:
        print("\nRe-run with --fix to repair (re-fetch + re-ingest each).")
        return 1

    # --fix: resolve job alias back to a short key for cmd_fetch
    _alias_map = {v.split("/")[-1]: k for k, v in _JOB_ALIASES.items()}
    repaired = 0
    for run_id, job, build, started in rows_:
        if build is None:
            print(f"[skip] {run_id} — no build number recorded, cannot re-fetch")
            continue
        job_tail = (job or "").split("/")[-1]
        job_key = _alias_map.get(job_tail, job_tail)
        print(f"\n--- repairing {run_id} ---")
        ns = argparse.Namespace(
            job=job_key, build=str(build), dest=None, release=a.release, replace=True
        )
        try:
            rc = cmd_fetch(ns)
            if rc == 0:
                repaired += 1
        except Exception as e:
            print(f"[warn] failed: {e}")
    print(f"\n[check-partial] repaired {repaired}/{len(rows_)}")
    return 0 if repaired == len(rows_) else 1


def cmd_purge_feature(a):
    """List (and with --fix, DELETE) test_run rows whose product_version is a
    feature/early-stage build (major >= 200, e.g. 211.x). These are not part of
    the standard 1xx system-test line; ingest now skips them, but any that landed
    before the gate was added should be purged. DELETE cascades to child rows."""
    import ingest as _ing

    conn = dbconn.connect()
    with conn.cursor() as cur:
        cur.execute("""SELECT run.id, run.run_id, run.jenkins_job, run.jenkins_build,
                      run.product_version, r.label
               FROM test_run run LEFT JOIN release r ON r.id=run.release_id
               ORDER BY run.product_version, run.jenkins_build""")
        rows_ = [
            row for row in cur.fetchall() if _ing._is_feature_build_version(row[4])
        ]
        # Feature builds are kept since 2026-11-03 ("we do not exclude feature build result"), so this command only lists them and
        # refuses --fix: deleting them again would put back the silent loss it was written against.
        if a.fix and rows_:
            print("[purge-feature] refused: feature builds are kept and shown in the release report; nothing was deleted")
            conn.close()
            return 1

        if not rows_:
            print(
                f"[ok] no feature builds (major>={_ing.FEATURE_BUILD_MAJOR_MIN}) in DB"
            )
            conn.close()
            return 0

        print(
            f"[warn] {len(rows_)} feature-build run(s) in DB (major>={_ing.FEATURE_BUILD_MAJOR_MIN}):"
        )
        for _id, run_id, job, build, ver, label in rows_:
            print(
                f"  {run_id}  job={job}  build={build}  version={ver}  release={label}"
            )

        if not a.fix:
            print(
                "\nRe-run with --fix to DELETE these rows (cascades to child tables)."
            )
            conn.close()
            return 1

        ids = [row[0] for row in rows_]
        cur.execute("DELETE FROM test_run WHERE id = ANY(%s)", (ids,))
        conn.commit()
        print(
            f"\n[purge-feature] deleted {cur.rowcount} test_run row(s) + cascaded children"
        )
    conn.close()
    return 0


def cmd_purge_test_name(a):
    """List (and with --fix, DELETE) every test_result row for one exact
    test_name -- for retiring a TEMP/diagnostic case's results once the real
    fix lands (e.g. a POC test that is removed from the repo but left its
    rows in the DB). Pure runs (every result in that test_run belongs to this
    test_name, e.g. a dedicated '-k test_name' build) are deleted whole via
    test_run (cascades, keeps run-level rollups consistent). Mixed runs keep
    the test_run row and only delete the matching test_result row(s), with
    the run's n_pass/n_fail/n_skip rollup decremented per deleted verdict so
    it does not go stale."""
    conn = dbconn.connect()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT tr.id, run.id, run.run_id, run.jenkins_job, run.jenkins_build,
                      run.product_version, tr.verdict,
                      (SELECT COUNT(*) FROM test_result WHERE run_id = run.id) AS total_in_run
               FROM test_result tr JOIN test_run run ON run.id = tr.run_id
               WHERE tr.test_name = %s
               ORDER BY run.jenkins_build""",
            (a.test_name,),
        )
        rows_ = cur.fetchall()

        if not rows_:
            print(f"[ok] no test_result rows for test_name={a.test_name!r}")
            conn.close()
            return 0

        print(f"[warn] {len(rows_)} test_result row(s) for test_name={a.test_name!r}:")
        pure_run_ids, mixed_rows = set(), []
        for result_id, run_pk, run_id, job, build, ver, verdict, total_in_run in rows_:
            kind = "pure run (whole run deleted)" if total_in_run == 1 else "mixed run (row-only delete)"
            print(f"  run_id={run_id}  job={job}  build={build}  version={ver}  verdict={verdict}  {kind}")
            if total_in_run == 1:
                pure_run_ids.add(run_pk)
            else:
                mixed_rows.append((result_id, run_pk, verdict))

        if not a.fix:
            print("\nRe-run with --fix to DELETE these rows.")
            conn.close()
            return 1

        if pure_run_ids:
            cur.execute("DELETE FROM test_run WHERE id = ANY(%s)", (list(pure_run_ids),))
            print(f"[purge-test-name] deleted {cur.rowcount} pure test_run row(s) + cascaded children")

        verdict_col = {"PASS": "n_pass", "FAIL": "n_fail", "SKIP": "n_skip"}
        for result_id, run_pk, verdict in mixed_rows:
            cur.execute("DELETE FROM test_result WHERE id = %s", (result_id,))
            col = verdict_col.get(verdict)
            if col:
                cur.execute(
                    f"UPDATE test_run SET {col} = GREATEST({col} - 1, 0) WHERE id = %s",
                    (run_pk,),
                )
        if mixed_rows:
            print(f"[purge-test-name] deleted {len(mixed_rows)} test_result row(s) from mixed runs, rollups decremented")

        conn.commit()
    conn.close()
    return 0


def cmd_canon_names(a):
    """List (and with --fix, UPDATE) the Windows test_result rows of one release whose test_name is still the
    legacy unsuffixed form, to the canonical test_x_win (testname.canonical_name). Only rows whose run.platform
    is windows are touched; mac/linux rows and names that already carry an OS suffix are left alone. Idempotent:
    a second run finds nothing. A DB rebuild from the cold store re-ingests the old names, so re-run it after one."""
    import testname

    conn = dbconn.connect()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT tr.id, tr.test_name, run.jenkins_job, run.jenkins_build,
                      EXISTS (SELECT 1 FROM test_result t2
                              WHERE t2.run_id = tr.run_id AND t2.test_name = tr.test_name || '_win')
               FROM test_result tr JOIN test_run run ON run.id = tr.run_id
               JOIN release rel ON rel.id = run.release_id
               WHERE rel.label = %s AND run.platform = 'windows'
                 AND tr.test_name !~ %s
               ORDER BY tr.test_name, run.jenkins_build""",
            (a.release, testname._SUFFIX_RE),
        )
        rows_ = cur.fetchall()
        if not rows_:
            print(f"[ok] {a.release}: every windows test_name is already canonical")
            conn.close()
            return 0
        clash = [r for r in rows_ if r[4]]
        names = sorted({r[1] for r in rows_})
        print(f"[warn] {a.release}: {len(rows_)} windows row(s), {len(names)} legacy name(s):")
        for n in names:
            print(f"  {n}  ->  {testname.canonical_name(n)}  ({sum(1 for r in rows_ if r[1] == n)} row(s))")
        if clash:
            print(f"[refuse] {len(clash)} row(s) would collide with an existing _win row in the same run; not touching")
            conn.close()
            return 2
        if not a.fix:
            print("\nRe-run with --fix to UPDATE these rows.")
            conn.close()
            return 1
        cur.execute(
            "UPDATE test_result SET test_name = test_name || '_win' WHERE id = ANY(%s)",
            ([r[0] for r in rows_],),
        )
        print(f"[canon-names] renamed {cur.rowcount} row(s) in {a.release}")
        # A gate_ticket is matched to its test_result by test_name; carry the ticket links of the same windows runs.
        cur.execute(
            """UPDATE gate_ticket gt SET test_name = gt.test_name || '_win'
                WHERE gt.test_name LIKE 'test\\_%%' AND gt.test_name !~ %s
                  AND EXISTS (SELECT 1 FROM test_run run JOIN release rel ON rel.id = run.release_id
                               WHERE run.jenkins_job = gt.jenkins_job AND run.jenkins_build = gt.jenkins_build
                                 AND rel.label = %s AND run.platform = 'windows')""",
            (testname._SUFFIX_RE, a.release),
        )
        print(f"[canon-names] renamed {cur.rowcount} ticket link(s) of the same runs")
        conn.commit()
    conn.close()
    return 0


def cmd_rename_test(a):
    """List (and with --fix, UPDATE) every test_result row named OLD to NEW, so results recorded under a temporary
    or POC name aggregate with the official case. Dry run by default. Refuses when OLD is NEW, when NEW has no
    row anywhere (a typo would invent a case) unless --allow-new, or when a run already holds a NEW row (the
    rename would put two rows of one case into one run). A cold-store rebuild re-ingests the old name, so re-run
    it after a rebuild, like canon-names."""
    if a.old == a.new:
        print("[fail] OLD and NEW are the same name")
        return 2
    conn = dbconn.connect()
    if a.tickets_only:
        # ticket links whose build has no stored run (it died before pytest) have no test_result row to rename
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ticket_id, jenkins_job, jenkins_build FROM gate_ticket WHERE test_name = %s", (a.old,)
            )
            links = cur.fetchall()
            print(f"{a.old}  ->  {a.new}   ({len(links)} ticket link(s))")
            _print_table(["ticket", "job", "build"], links)
            if not links:
                conn.close()
                return 2
            if not a.fix:
                print("\nRe-run with --fix to UPDATE these links.")
                conn.close()
                return 1
            cur.execute("UPDATE gate_ticket SET test_name = %s WHERE test_name = %s", (a.new, a.old))
            print(f"[rename-test] renamed {cur.rowcount} ticket link(s) to {a.new}")
            conn.commit()
        conn.close()
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """SELECT tr.id, run.jenkins_job, run.jenkins_build, tr.verdict,
                      EXISTS (SELECT 1 FROM test_result t2 WHERE t2.run_id = tr.run_id AND t2.test_name = %s)
                 FROM test_result tr JOIN test_run run ON run.id = tr.run_id
                WHERE tr.test_name = %s ORDER BY run.jenkins_job, run.jenkins_build""",
            (a.new, a.old),
        )
        rows_ = cur.fetchall()
        if not rows_:
            print(f"[fail] no test_result row is named {a.old!r}")
            conn.close()
            return 2
        cur.execute("SELECT count(*) FROM test_result WHERE test_name = %s", (a.new,))
        known = cur.fetchone()[0]
        print(f"{a.old}  ->  {a.new}   ({len(rows_)} row(s); {known} row(s) already carry the new name)")
        _print_table(["job", "build", "verdict"], [(r[1], r[2], r[3]) for r in rows_])
        if not known and not a.allow_new:
            print("[refuse] the new name has no row anywhere; check the spelling or pass --allow-new")
            conn.close()
            return 2
        clash = [r for r in rows_ if r[4]]
        if clash:
            print(f"[refuse] {len(clash)} run(s) already hold a {a.new!r} row; not touching")
            conn.close()
            return 2
        if not a.fix:
            print("\nRe-run with --fix to UPDATE these rows (run `db.py backup` first).")
            conn.close()
            return 1
        cur.execute("UPDATE test_result SET test_name = %s WHERE id = ANY(%s)", (a.new, [r[0] for r in rows_]))
        print(f"[rename-test] renamed {cur.rowcount} row(s) to {a.new}")
        # ticket links of the same runs follow the case (the report matches a ticket to its result by name)
        cur.execute(
            """UPDATE gate_ticket gt SET test_name = %s
                WHERE gt.test_name = %s AND EXISTS (SELECT 1 FROM test_result tr JOIN test_run run ON run.id = tr.run_id
                                                     WHERE tr.id = ANY(%s) AND run.jenkins_job = gt.jenkins_job
                                                       AND run.jenkins_build = gt.jenkins_build)""",
            (a.new, a.old, [r[0] for r in rows_]),
        )
        print(f"[rename-test] renamed {cur.rowcount} ticket link(s) of the same runs")
        conn.commit()
    conn.close()
    return 0


def cmd_purge_allskip(a):
    """List (and with --fix, DELETE) test_run rows where every case was SKIPped
    (n_skip>0, n_pass=0, n_fail=0 — the n_skip>0 half also excludes 0-test empty
    builds). Such a run measured nothing; ingest now gates them, but any that
    landed before the gate should be purged. --version filters by product_version
    prefix (e.g. 142). DELETE cascades to child rows."""
    conn = dbconn.connect()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT run.id, run.run_id, run.jenkins_job, run.jenkins_build,
                      run.product_version, r.label, run.n_skip
               FROM test_run run LEFT JOIN release r ON r.id=run.release_id
               WHERE run.n_skip > 0 AND run.n_pass = 0 AND run.n_fail = 0
                 AND (%s IS NULL OR run.product_version LIKE %s)
               ORDER BY run.product_version, run.jenkins_build""",
            (a.version, f"{a.version}.%" if a.version else None),
        )
        rows_ = cur.fetchall()
        scope = f" (version {a.version}.x)" if a.version else ""

        if not rows_:
            print(f"[ok] no all-skip runs in DB{scope}")
            conn.close()
            return 0

        print(f"[warn] {len(rows_)} all-skip run(s) in DB{scope}:")
        for _id, run_id, job, build, ver, label, n_skip in rows_:
            print(
                f"  {run_id}  job={job}  build={build}  version={ver}  "
                f"release={label}  skipped={n_skip}"
            )

        if not a.fix:
            print(
                "\nRe-run with --fix to DELETE these rows (cascades to child tables)."
            )
            conn.close()
            return 1

        cur.execute("DELETE FROM test_run WHERE id = ANY(%s)", ([r[0] for r in rows_],))
        conn.commit()
        print(
            f"\n[purge-allskip] deleted {cur.rowcount} test_run row(s) + cascaded children"
        )
    conn.close()
    return 0


# ─────────────────────────── clone-release ───────────────────────────

# One-time publish-ready snapshot: copy a release into a new label, dropping
# non-PASS test_result rows that are infra/harness noise (timeouts, backend-
# sync lag, precondition gates, test-code exceptions, transport errors,
# self-declared setup issues, or a signal an owner already ruled non-product).
# PASS rows are always copied — the goal is "keep as many real results as
# possible", not "keep only tickets". The decision is DATA (keep_lists/*.json),
# not a regex baked into this file, because the free-text fail_reason across
# 99 rows needed per-row human judgement (see keep_lists/release-141-int.json
# for the full reasoning trail) and a hardcoded pattern list would rot the
# next time a test's wording changes.

_CLONE_RUN_COLS = [
    "run_id",
    "jenkins_job",
    "jenkins_build",
    "build_url",
    "node_name",
    "vm_ip",
    "product_version",
    "previous_release",
    "platform",
    "tenant",
    "dc",
    "is_64_bit_cli",
    "is_64_bit_installed",
    "iterations_cli",
    "started_at",
    "finished_at",
    "raw_ndjson_path",
]  # release_id / n_pass / n_fail / n_skip handled separately (remapped / recomputed)

_CLONE_RESULT_COLS = [
    "test_name",
    "suite",
    "verdict",
    "started_at",
    "duration_s",
    "iterations_target",
    "iterations_total",
    "iterations_passed",
    "iterations_failed",
    "first_failed_iter",
    "fail_reason",
    "failing_gate",
    "has_dump",
    "gate_violated",
    "is_product_issue",
]  # run_id handled separately (remapped); iterations_undone is GENERATED

_CLONE_CHILD_TABLES = {
    "iteration_result": [
        "iter_index",
        "iter_total",
        "status",
        "duration_s",
        "fail_reason",
    ],
    "process_summary": [
        "process_name",
        "cpu_max_pct",
        "cpu_sustained_s",
        "cpu_threshold",
        "mem_baseline_mb",
        "mem_max_mb",
        "mem_growth_mb",
        "handles_max",
        "alive_pct",
        "sample_count",
    ],
    "gate_violation": [
        "violation_type",
        "process_name",
        "observed",
        "threshold",
        "detail",
    ],
    "dump": [
        "dump_type",
        "process_name",
        "signal_tag",
        "file_name",
        "file_path",
        "size_bytes",
        "event_id",
        "captured_at",
        "note",
    ],
    "resource_sample": [
        "process_name",
        "elapsed_s",
        "sampled_at",
        "boot_id",
        "cpu_pct",
        "cpu_pct_per_core",
        "mem_mb",
        "handles",
        "alive",
        "net_mbps_in",
        "net_mbps_out",
        "load_phase",
    ],
}


def _dictrows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _load_keep_set(path):
    import json

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # build may be null: some runs lost their build number at ingest (counter-restart collisions)
    return {
        (k["job"], None if k.get("build") is None else int(k["build"]), k["test_name"])
        for k in data["keep"]
    }


def cmd_clone_release(a):
    """Copy every row of --source into a NEW release --target. Non-PASS
    test_result rows are copied only if listed in --keep-file; PASS rows are
    always copied. A test_run left with zero surviving test_result is not
    copied (no empty shells). --source is never modified. Refuses if --target
    already has test_run rows (re-running would duplicate; drop it first if a
    redo is genuinely wanted)."""
    import ingest as _ing

    keep_set = _load_keep_set(a.keep_file)
    conn = dbconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM release WHERE label=%s", (a.source,))
            row = cur.fetchone()
            if not row:
                print(f"[error] source release '{a.source}' not found")
                return 2
            src_id = row[0]

            cur.execute("SELECT id FROM release WHERE label=%s", (a.target,))
            trow = cur.fetchone()
            if trow:
                cur.execute(
                    "SELECT count(*) FROM test_run WHERE release_id=%s", (trow[0],)
                )
                if cur.fetchone()[0] > 0:
                    print(
                        f"[error] target release '{a.target}' already has test_run rows — "
                        "drop it first if a redo is genuinely wanted"
                    )
                    return 2

            cur.execute(
                f"SELECT id, {', '.join(_CLONE_RUN_COLS)} FROM test_run "
                "WHERE release_id=%s ORDER BY id",
                (src_id,),
            )
            runs = _dictrows(cur)

            cur.execute(
                "SELECT tr.id AS result_id, tr.run_id AS run_pk, "
                "r.jenkins_job AS job, r.jenkins_build AS build, "
                + ", ".join("tr." + c for c in _CLONE_RESULT_COLS)
                + " FROM test_result tr JOIN test_run r ON r.id = tr.run_id "
                "WHERE r.release_id=%s ORDER BY tr.id",
                (src_id,),
            )
            results = _dictrows(cur)

            # A final cut never contains a patch line (140.1.0.2781, 140.1.4.2805): drop those runs and their results first.
            from version_rules import is_patch_line

            patch_run_pks = {r["id"] for r in runs if is_patch_line(r["product_version"])}
            patch_versions = sorted({r["product_version"] for r in runs if r["id"] in patch_run_pks})
            runs = [r for r in runs if r["id"] not in patch_run_pks]
            patch_results_n = sum(1 for res in results if res["run_pk"] in patch_run_pks)
            results = [res for res in results if res["run_pk"] not in patch_run_pks]
            if patch_run_pks:
                print(
                    f"[patch-lines] {len(patch_run_pks)} run(s) / {patch_results_n} result(s) of patch builds "
                    f"{', '.join(patch_versions)} are not part of a final cut"
                )

            kept = [
                res
                for res in results
                if res["verdict"] == "PASS"
                or (res["job"], res["build"], res["test_name"]) in keep_set
            ]
            filtered_n = len(results) - len(kept)
            surviving_run_pks = {res["run_pk"] for res in kept}
            runs_to_copy = [r for r in runs if r["id"] in surviving_run_pks]
            skipped_runs = len(runs) - len(runs_to_copy)

            if a.dry_run:
                print(f"[dry-run] {a.source} ({src_id}) -> {a.target}")
                print(
                    f"  test_run: {len(runs_to_copy)}/{len(runs)} would be copied "
                    f"({skipped_runs} empty-shell run(s) dropped)"
                )
                print(
                    f"  test_result: {len(kept)}/{len(results)} kept "
                    f"({filtered_n} non-PASS row(s) filtered by {a.keep_file})"
                )
                return 0

            tgt_id = _ing._release_id(cur, a.target)

            run_map = {}
            for r in runs_to_copy:
                cols = _CLONE_RUN_COLS + ["release_id", "n_pass", "n_fail", "n_skip"]
                vals = [
                    r["run_id"] + "-int" if c == "run_id" else r[c]
                    for c in _CLONE_RUN_COLS
                ]
                vals += [
                    tgt_id,
                    0,
                    0,
                    0,
                ]  # counts recomputed below, after results are copied
                cur.execute(
                    f"INSERT INTO test_run ({', '.join(cols)}) "
                    f"VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                    vals,
                )
                run_map[r["id"]] = cur.fetchone()[0]

            result_map = {}
            for res in kept:
                cols = ["run_id"] + _CLONE_RESULT_COLS
                vals = [run_map[res["run_pk"]]] + [res[c] for c in _CLONE_RESULT_COLS]
                cur.execute(
                    f"INSERT INTO test_result ({', '.join(cols)}) "
                    f"VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                    vals,
                )
                result_map[res["result_id"]] = cur.fetchone()[0]

            child_counts = {}
            for table, cols in _CLONE_CHILD_TABLES.items():
                total = 0
                for old_id, new_id in result_map.items():
                    cur.execute(
                        f"INSERT INTO {table} (test_result_id, {', '.join(cols)}) "
                        f"SELECT %s, {', '.join(cols)} FROM {table} WHERE test_result_id=%s",
                        (new_id, old_id),
                    )
                    total += cur.rowcount
                child_counts[table] = total

            for new_run_pk in run_map.values():
                cur.execute(
                    "UPDATE test_run SET "
                    "n_pass=(SELECT count(*) FROM test_result WHERE run_id=%s AND verdict='PASS'), "
                    "n_fail=(SELECT count(*) FROM test_result WHERE run_id=%s AND verdict='FAIL'), "
                    "n_skip=(SELECT count(*) FROM test_result WHERE run_id=%s "
                    "AND verdict NOT IN ('PASS','FAIL')) WHERE id=%s",
                    (new_run_pk, new_run_pk, new_run_pk, new_run_pk),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"[clone-release] {a.source} -> {a.target}")
    print(
        f"  test_run copied: {len(run_map)} (skipped {skipped_runs} empty-shell run(s))"
    )
    print(
        f"  test_result copied: {len(result_map)} ({filtered_n} non-PASS row(s) filtered)"
    )
    for table, n in child_counts.items():
        print(f"  {table}: {n}")
    return 0


def cmd_drop_release(a):
    """Delete every test_run row (and cascaded test_result/child rows) under
    --label, then the now-empty release row itself. Lets a curated snapshot
    (e.g. release-142-final) be regenerated after re-running clone-release —
    clone-release refuses a --target that still has test_run rows, so this is
    the required first step of a regen, not an alternative to it. Refuses
    without --yes (irreversible; no undo short of a DB backup restore)."""
    conn = dbconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM release WHERE label=%s", (a.label,))
            row = cur.fetchone()
            if not row:
                print(f"[error] release '{a.label}' not found")
                return 2
            rel_id = row[0]
            cur.execute("SELECT count(*) FROM test_run WHERE release_id=%s", (rel_id,))
            n_runs = cur.fetchone()[0]
            if not a.yes:
                print(
                    f"[drop-release] '{a.label}' has {n_runs} test_run row(s) "
                    f"(+ cascaded test_result/child rows). Re-run with --yes to delete."
                )
                return 1
            cur.execute("DELETE FROM test_run WHERE release_id=%s", (rel_id,))
            cur.execute("DELETE FROM release WHERE id=%s", (rel_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(
        f"[drop-release] '{a.label}' dropped ({n_runs} test_run row(s) + cascaded children)"
    )
    return 0


def cmd_drop_gate_violation(a):
    """Delete one gate_violation row by id -- for a Health-gate breakdown row
    whose evidence (the .dmp file it names) is confirmed gone (Jenkins build
    retention purged the build + its archived backup zip, and the VM has long
    since been reused). Does NOT touch the `dump` table (dump_type/file_path
    rows parsed at ingest) or test_result.has_dump/gate_violated -- those are
    a separate historical record of what the test observed, out of scope for
    "the report row that claims a dump is available to look at" (release-142-
    final gv id=93, REG2 build 200, 2026-09-29 -- confirmed via `grs_jenkins.py
    status reg2 200` returning HTTP 404 before this was added). Refuses
    without --yes (irreversible; no undo short of a DB backup restore)."""
    conn = dbconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT gv.violation_type, tr.test_name, run.jenkins_job,
                          run.jenkins_build, r.label, gv.detail
                     FROM gate_violation gv
                     JOIN test_result tr ON tr.id = gv.test_result_id
                     JOIN test_run run ON run.id = tr.run_id
                     JOIN release r ON r.id = run.release_id
                    WHERE gv.id = %s""",
                (a.id,),
            )
            row = cur.fetchone()
            if not row:
                print(f"[error] gate_violation id={a.id} not found")
                return 2
            gate, test, job, build, label, detail = row
            print(
                f"[drop-gate-violation] id={a.id}: {gate} on {test} "
                f"({job} build {build}, release {label})\n  detail: {detail}"
            )
            if not a.yes:
                print("Re-run with --yes to delete.")
                return 1
            cur.execute("DELETE FROM gate_violation WHERE id=%s", (a.id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"[drop-gate-violation] id={a.id} deleted")
    return 0


def cmd_rename_release(a):
    """Rename a release label in place. No test_run/test_result rows move —
    only the release row's label (and recomputed ordinal) change. Refuses if
    --to already exists (never silently merges two releases)."""
    import ingest as _ing

    conn = dbconn.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM release WHERE label=%s", (a.to,))
        if cur.fetchone():
            print(f"[error] target label '{a.to}' already exists")
            conn.close()
            return 2
        cur.execute(
            "UPDATE release SET label=%s, ordinal=%s WHERE label=%s RETURNING id",
            (a.to, _ing._release_ordinal(a.to), a.frm),
        )
        row = cur.fetchone()
        if not row:
            print(f"[error] source label '{a.frm}' not found")
            conn.close()
            return 2
        conn.commit()
    conn.close()
    print(f"[rename-release] '{a.frm}' -> '{a.to}'")
    return 0


# ─────────────────────────── runtime ───────────────────────────

# "How long does one iteration of each case take?" — the question asked before
# every campaign (how many cases fit in a lane, what timeout to set).
#
# iter_index = 1 is reported SEPARATELY from the all-iteration aggregate, and it
# is the number to quote for `--iterations 1`: the first iteration pays
# cold-cache and first-config-push costs that later reps in the same process do
# not, so an all-iteration median taken from 50-rep soaks understates a
# single-iteration run.
#
# Only PASS iterations count by DEFAULT. A FAIL aborts partway through the body
# (or sits in a retry/timeout that a PASS never pays), so its duration is not a
# sample of "how long the case takes" -- `--status all` / `--status fail` opt in
# when the question really is "how long before it gave up".
#
# Rows are grouped by RELEASE, always. Durations are not comparable across
# releases (product changes, tenant changes, lane changes), so a single pooled
# average silently mixes populations; the owner's question is literally
# "for 140/141, how long".
#
# Two sources, one output shape: the DB (default) and the harvest cold store
# (--store). The store path exists because the DB is derived data that can be
# down for days (it was decommissioned 2026-07-27) while the ndjson inputs are
# still on disk — a timing question should not have to wait for a VM.


def _fmt_dur(sec):
    if sec is None:
        return "-"
    m, s = divmod(int(round(float(sec))), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _norm_release(spec):
    """'140' / '141' / 'release-141' / '140,141' / '140/141' -> ['release-140', ...].

    The label in the DB is `release-140`; every human asks for `140`. Normalising
    in ONE place (via ingest's `_release_ordinal`, not a second regex here) means
    no caller has to know the stored format. Unrecognised tokens pass through
    unchanged so an odd label is still filterable.
    """
    if not spec:
        return None
    import ingest

    out = []
    for tok in str(spec).replace("/", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        n = ingest._release_ordinal(tok)
        out.append(f"release-{n}" if n is not None else tok)
    return out or None


def _norm_case(name):
    """Lower-case, alphanumerics only -- 'STRESS-03' and 'stress_03' collapse to
    the same key so a spec ID and a pytest name both match."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _one_case(names, pattern):
    """Candidates for a command that needs exactly ONE test: exact name wins, else the
    normalised substring. A short name such as 'test_stress_26' matches the same case on
    every OS ('test_x_win', 'test_x_mac', later 'test_x_linux') and, for history from before
    the _win rename, the unsuffixed 'test_x'. None of that is an ambiguity:
      - an unsuffixed legacy name and its _win twin are one test, the suffixed one is kept;
      - when several OS variants of one case hit, the default OS (Windows) wins (ask for
        another OS by its full name or with 'mac' / 'linux' in the pattern, which only that
        variant's name contains).
    Two genuinely different cases stay ambiguous. OS list: testname.OS_SUFFIX."""
    if pattern in names:
        return [pattern]
    pat = _norm_case(pattern)
    hit = [n for n in names if pat in _norm_case(n)]
    by_case = {}
    for n in hit:
        base, suffix = testname.split_os(n)
        by_case.setdefault(base, {}).setdefault(suffix, []).append(n)
    out = []
    for variants in by_case.values():
        if len(variants) > 1 and testname.DEFAULT_PLATFORM_SUFFIX in variants:
            variants = {testname.DEFAULT_PLATFORM_SUFFIX: variants[testname.DEFAULT_PLATFORM_SUFFIX]}
        for same_os in variants.values():
            # legacy 'test_x' and 'test_x_win': keep the one written under the current convention
            out.append(max(same_os, key=lambda n: testname.canonical_name(n) == n))
    return sorted(out)


def _resolve_cases(rows, pattern):
    """Fuzzy `--test`: 'stress03' -> ['test_stress_03_tunnel_flapping'].

    Exact name wins; otherwise normalised substring. Returns [] on no match so
    the caller can print what IS available -- an empty table would read as "that
    case never ran" (ledger 11: 0 rows means my filter missed, not absence).
    """
    names = sorted({r["case"] for r in rows})
    if not pattern:
        return names
    if pattern in names:
        return [pattern]
    pat = _norm_case(pattern)
    return [n for n in names if pat in _norm_case(n)]


def _runtime_rows_from_db(a):
    """One dict per iteration record: case/iter/dur/job/release/status/build.

    The `--test` filter is deliberately NOT in the SQL: resolving it in Python
    over every case lets a miss list the real names instead of printing an empty
    table. iteration_result is small (hundreds of rows), so the scan is free.
    """
    sql = """
        SELECT r.test_name, i.iter_index, i.duration_s, n.jenkins_job,
               COALESCE(rel.label, '(none)'), i.status, n.run_id
          FROM iteration_result i
          JOIN test_result r ON r.id = i.test_result_id
          JOIN test_run    n ON n.id = r.run_id
          LEFT JOIN release rel ON rel.id = n.release_id
         WHERE i.duration_s IS NOT NULL
           AND (%(releases)s IS NULL OR rel.label = ANY(%(releases)s))
           AND (%(job)s      IS NULL
                OR (%(canon)s IS NOT NULL AND n.jenkins_job = %(canon)s)
                OR (%(canon)s IS NULL AND n.jenkins_job LIKE '%%' || %(job)s || '%%'))
    """
    conn = dbconn.connect()
    with conn.cursor() as cur:
        cur.execute(sql, {"releases": _norm_release(a.release), "job": a.job, "canon": _canon_job(a.job)})
        rows = [
            {
                "case": r[0],
                "iter": r[1],
                "dur": float(r[2]),
                "job": r[3] or "?",
                "release": r[4],
                "status": (r[5] or "?").upper(),
                "build": str(r[6] or "?").rsplit("-", 1)[-1],
            }
            for r in cur.fetchall()
        ]
    conn.close()
    return rows


def _store_dir(a):
    if isinstance(a.store, str) and a.store:
        return os.path.abspath(a.store)
    env = os.environ.get("GRS_DB_STORE")
    if env:
        return os.path.abspath(env)
    return os.path.abspath(os.path.join(_HERE, "..", "..", "systest_db_store"))


def _iter_store_ndjson(store):
    """Yield (job_alias, build, [record dicts]) per stored build."""
    import glob
    import gzip
    import json

    pat = os.path.join(store, "**", "iterations_*.ndjson.gz")
    for p in sorted(glob.glob(pat, recursive=True)):
        build_dir = os.path.dirname(p)
        job = os.path.basename(os.path.dirname(build_dir))
        recs = []
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    continue  # a truncated tail line is not worth failing the report
        yield job, os.path.basename(build_dir), recs


def _runtime_rows_from_store(a, store):
    """Same row shape as the DB path, straight off the harvested ndjson."""
    import ingest

    releases = _norm_release(a.release)
    rows = []
    for job, build, recs in _iter_store_ndjson(store):
        if a.job and a.job not in job:
            continue
        # The store has no release column: derive it the way ingest does, from
        # the first non-null client_version among the run's `test` records
        # (ingest.py:511). Same definition, imported -- never a second copy.
        ver = next(
            (
                d.get("client_version")
                for d in recs
                if d.get("record_type") == "test" and d.get("client_version")
            ),
            None,
        )
        label = ingest._release_from_version(ver) or "(none)"
        if releases and label not in releases:
            continue
        for d in recs:
            if d.get("record_type") != "iteration" or d.get("duration_s") is None:
                continue
            rows.append(
                {
                    "case": d.get("test_case"),
                    "iter": d.get("iter_index"),
                    "dur": float(d["duration_s"]),
                    "job": job,
                    "release": label,
                    "status": str(d.get("status") or "?").upper(),
                    "build": str(build),
                }
            )
    return rows


def _stat(vals):
    """n / avg / median / min / max. `avg` because that is what gets asked for;
    `med` because with n=2 and a 20x spread the average is the wrong summary."""
    if not vals:
        return None
    import statistics as st

    return {
        "n": len(vals),
        "avg": st.fmean(vals),
        "med": st.median(vals),
        "min": min(vals),
        "max": max(vals),
    }


def _runtime_overhead(a, store, wanted=None):
    """Jenkins wall-clock minus iteration body, from single-case iter=1 builds.

    The recorder's duration_s covers only the iteration body; checkout,
    uninstall/install, check_client_installation, the tunnel-up wait and the
    artifact collect all sit outside that window. Without this, a per-case
    number badly underestimates how long a lane actually needs.
    """
    import statistics as st

    cands = []
    for job, build, recs in _iter_store_ndjson(store):
        if a.job and a.job not in job:
            continue
        iters = [
            d
            for d in recs
            if d.get("record_type") == "iteration" and d.get("duration_s") is not None
        ]
        cases = {d.get("test_case") for d in iters}
        # Exactly one case, exactly one iteration, and that iteration passed:
        # anything else mixes other cases' time into the wall-clock delta.
        if len(cases) != 1 or len(iters) != 1:
            continue
        it = iters[0]
        if it.get("iter_total") != 1 or it.get("status") != "PASS":
            continue
        case = cases.pop()
        if wanted and case not in wanted:
            continue
        cands.append((job, build, case, float(it["duration_s"])))

    if not cands:
        return None, []

    # Reuse grs_jenkins for auth + alias resolution rather than restating the
    # credentials or the job paths here (one definition, same as harvest.py).
    sys.path.insert(0, os.path.dirname(_HERE))
    try:
        import grs_jenkins
    except ImportError:
        print("[warn] --overhead needs grs_jenkins.py alongside grs_tools/; skipped")
        return None, []

    detail, overs = [], []
    for job, build, case, body in sorted(cands, key=lambda x: x[2]):
        try:
            info = grs_jenkins._get(f"{grs_jenkins._job(job)}/{build}/api/json")
            wall = (info.get("duration") or 0) / 1000.0
        except Exception as e:  # a dead build/job must not kill the whole report
            detail.append((case, job, build, body, None, None, f"{type(e).__name__}"))
            continue
        if wall <= 0:
            continue
        detail.append((case, job, build, body, wall, wall - body, ""))
        overs.append(wall - body)
    summary = (
        {"n": len(overs), "med": st.median(overs), "min": min(overs), "max": max(overs)}
        if overs
        else None
    )
    return summary, detail


def cmd_timeout(a):
    """Recommend a Jenkins timeout for case x N iterations from MEASURED
    per-iteration durations (never a hand-tuned formula). Output per case:
    iter1 median / steady-state median / need for N / suggested REG `timeout`
    (hours) / LOCAL formula (60+40N min) sufficiency. Thin history (<5 PASS
    samples) doubles the headroom and says so — a fresh case has no measured
    steady state to trust."""
    import statistics as st

    if a.store is not False and a.store is not None:
        rows = _runtime_rows_from_store(a, _store_dir(a))
    else:
        rows = _runtime_rows_from_db(a)
    if not rows:
        print("[!] no iteration records with a duration matched the selection")
        return 1
    cases = _resolve_cases(rows, a.test)
    if a.test and not cases:
        print(f"[!] --test {a.test!r} matched no case. available:")
        for n in sorted({r["case"] for r in rows}):
            print(f"      {n}")
        return 1
    rows = [r for r in rows if r["case"] in cases]
    want = (a.status or "pass").lower()
    rows = [r for r in rows if want == "all" or r["status"] == want.upper()]
    n_iter = max(1, a.iterations)
    print(
        f"iterations={n_iter}  install={a.install_min}min  headroom=x{a.headroom} (x2 if samples<5)\n"
    )
    print(
        f"{'case':52} {'iter1':>7} {'steady':>7} {'n':>3} {'need':>8} {'suggest':>8}  LOCAL 60+40N"
    )
    for case in sorted(cases):
        crows = [r for r in rows if r["case"] == case]
        if not crows:
            print(f"{case:52} {'(no PASS samples)':>26}")
            continue
        iter1 = [r["dur"] for r in crows if r["iter"] == 1]
        steady = [r["dur"] for r in crows if r["iter"] >= 2]
        m1 = st.median(iter1) if iter1 else None
        ms = st.median(steady) if steady else m1
        if m1 is None:
            m1 = ms
        if m1 is None or ms is None:
            print(f"{case:52} {'(no duration data)':>26}")
            continue
        head = a.headroom * (2 if len(crows) < 5 else 1)
        need_min = (a.install_min * 60 + m1 + (n_iter - 1) * ms) / 60.0
        sug_min = need_min * head
        reg_h = -(-sug_min // 60)  # ceil to hours for the REG timeout param
        local_formula = 60 + 40 * n_iter
        flag = (
            "ok"
            if local_formula >= sug_min
            else f"SHORT by {int(sug_min - local_formula)}min"
        )
        thin = " (thin: x2 headroom)" if len(crows) < 5 else ""
        print(
            f"{case:52} {_fmt_dur(m1):>7} {_fmt_dur(ms):>7} {len(crows):>3} "
            f"{int(need_min):>6}m {('%dh' % reg_h):>8}  {flag}{thin}"
        )
    return 0


def cmd_runtime(a):
    use_store = a.store is not False and a.store is not None
    if use_store:
        store = _store_dir(a)
        if not os.path.isdir(store):
            print(f"[store] {store} — DOES NOT EXIST")
            return 1
        rows = _runtime_rows_from_store(a, store)
        print(f"source: cold store {store}")
    else:
        store = None
        rows = _runtime_rows_from_db(a)
        print(f"source: DB {dbconn.describe()}")

    if not rows:
        print("[!] no iteration records with a duration matched --release/--job")
        return 1

    cases = _resolve_cases(rows, a.test)
    if a.test and not cases:
        # Never let a typo look like "this case has no history" (ledger 11).
        print(f"[!] --test {a.test!r} matched no case. available:")
        for n in sorted({r["case"] for r in rows}):
            print(f"      {n}")
        return 1
    if a.test:
        print(f"--test {a.test!r} -> {len(cases)} case(s): {', '.join(cases)}")
    rows = [r for r in rows if r["case"] in cases]

    want = (a.status or "pass").lower()
    kept = [r for r in rows if want == "all" or r["status"] == want.upper()]
    if not kept:
        seen = ", ".join(sorted({r["status"] for r in rows}))
        print(
            f"[!] no {want.upper()} iterations for that selection "
            f"(statuses present: {seen}) — try --status all"
        )
        return 1
    # A case whose every iteration was filtered out must be NAMED, not dropped:
    # "no PASS iteration on this release" is itself the answer to "how long does
    # it take" (it never finished), and a silent omission looks like no history.
    have = {r["case"] for r in kept}
    empty = [c for c in cases if c not in have]
    cases = [c for c in cases if c in have]
    releases = sorted({r["release"] for r in kept})
    print(
        f"iterations: {len(kept)} used, {len(rows) - len(kept)} excluded by "
        f"--status {want}   cases: {len(cases)}   releases: {', '.join(releases)}\n"
    )
    if empty:
        print(
            f"[no {want.upper()} sample] {len(empty)} case(s) never completed an "
            f"iteration in this selection: {', '.join(empty)}\n"
        )

    hdr = (
        f"{'test case':<40} {'release':<12} {'iter1 avg':>9} {'med':>8} {'n':>3} | "
        f"{'all avg':>8} {'med':>8} {'min':>8} {'max':>8} {'n':>4} | jobs"
    )
    print(hdr)
    print("-" * len(hdr))
    thin = []  # (case, release, n) where n < 5 -> the average is directional only
    starred = False
    for c in cases:
        for rel in sorted({r["release"] for r in kept if r["case"] == c}):
            rc = [r for r in kept if r["case"] == c and r["release"] == rel]
            first = _stat([r["dur"] for r in rc if r["iter"] == 1])
            allit = _stat([r["dur"] for r in rc])
            jobs = ",".join(sorted({r["job"] for r in rc}))
            shown = first or allit
            mark = "" if first else "*"
            starred = starred or not first
            if shown["n"] < 5:
                thin.append((c, rel, shown["n"]))
            print(
                f"{c:<40} {rel:<12} {_fmt_dur(shown['avg']) + mark:>9} "
                f"{_fmt_dur(shown['med']):>8} {shown['n']:>3} | "
                f"{_fmt_dur(allit['avg']):>8} {_fmt_dur(allit['med']):>8} "
                f"{_fmt_dur(allit['min']):>8} {_fmt_dur(allit['max']):>8} "
                f"{allit['n']:>4} | {jobs}"
            )
    if starred:
        print(
            "\n* no iter_index=1 sample in that cell; all-iteration stats shown instead"
        )
    if thin:
        # feedback_measure_noise_before_comparing: under n=5 the number is a
        # direction, not a magnitude -- say so here instead of hoping the reader
        # notices the n column.
        print(
            f"[n<5] {len(thin)} cell(s) have fewer than 5 samples "
            f"(direction only, do NOT quote a delta): "
            + ", ".join(f"{c.replace('test_', '')}/{rel}={n}" for c, rel, n in thin[:8])
            + (" ..." if len(thin) > 8 else "")
        )

    # Per-iteration breakdown: automatic for a single case (that IS the question
    # when you name one), opt-in otherwise so the all-cases table stays readable.
    if a.per_iter or len(cases) == 1:
        print(
            "\nper-iteration breakdown (iter_index = the rep inside one --iterations run):"
        )
        h2 = (
            f"  {'test case':<38} {'release':<12} {'iter':>4} {'n':>3} "
            f"{'avg':>8} {'med':>8} {'min':>8} {'max':>8}"
        )
        print(h2)
        print("  " + "-" * (len(h2) - 2))
        for c in cases:
            for rel in sorted({r["release"] for r in kept if r["case"] == c}):
                cell = [r for r in kept if r["case"] == c and r["release"] == rel]
                for idx in sorted(
                    {r["iter"] for r in cell}, key=lambda x: (x is None, x)
                ):
                    s = _stat([r["dur"] for r in cell if r["iter"] == idx])
                    print(
                        f"  {c:<38} {rel:<12} {str(idx):>4} {s['n']:>3} "
                        f"{_fmt_dur(s['avg']):>8} {_fmt_dur(s['med']):>8} "
                        f"{_fmt_dur(s['min']):>8} {_fmt_dur(s['max']):>8}"
                    )

    # One case selected -> show every sample. With a 20x spread (STRESS-03: 3m to
    # 64m) an average alone misleads; the build numbers are what you go read.
    if len(cases) == 1:
        cap = 40
        print(f"\nsamples ({len(kept)}):")
        for r in sorted(kept, key=lambda r: (r["release"], r["job"], r["build"]))[:cap]:
            print(
                f"  {r['release']:<12} {r['job']:<10} build {r['build']:>6} "
                f"iter {str(r['iter']):>3} {r['status']:<4} {_fmt_dur(r['dur']):>8}"
            )
        if len(kept) > cap:
            print(
                f"  ... {len(kept) - cap} more not shown (cap {cap}; narrow with --release/--job)"
            )

    pooled = []
    for c in cases:
        rc = [r for r in kept if r["case"] == c]
        first = _stat([r["dur"] for r in rc if r["iter"] == 1])
        pooled.append((first or _stat([r["dur"] for r in rc]))["med"])
    print(
        f"\nsum of per-case iter=1 medians (bodies only, releases pooled): "
        f"{_fmt_dur(sum(pooled))}"
    )
    print(
        "NOTE: excludes install + fixture setup + artifact collect — add the overhead below."
    )

    if a.overhead:
        if not use_store:
            print(
                "\n[warn] --overhead needs the cold store (build wall-clock is not in the DB); "
                "re-run with --store"
            )
            return 0
        summary, detail = _runtime_overhead(a, store, wanted=set(cases))
        print(f"\noverhead detail (single-case iter=1 builds, n={len(detail)}):")
        h2 = f"  {'case':<46} {'job':<10} {'build':>6} {'body':>8} {'wall':>8} {'overhead':>9}"
        print(h2)
        print("  " + "-" * (len(h2) - 2))
        for case, job, build, body, wall, ov, err in detail:
            if err:
                print(
                    f"  {case:<46} {job:<10} {build:>6} {_fmt_dur(body):>8}  api: {err}"
                )
            else:
                print(
                    f"  {case:<46} {job:<10} {build:>6} {_fmt_dur(body):>8} "
                    f"{_fmt_dur(wall):>8} {_fmt_dur(ov):>9}"
                )
        if summary:
            print(
                f"\noverhead (install + fixtures + collect): median {_fmt_dur(summary['med'])}  "
                f"min {_fmt_dur(summary['min'])}  max {_fmt_dur(summary['max'])}  n={summary['n']}"
            )
    return 0


def cmd_status(a):
    """One-shot DB + harvest health summary.

    Combines three operations that were previously run as a manual compound:
      db.py ping && db.py growth && db_health.py --verbose
    All three are fast (local reads / single SQL query). Exit 0 even when
    health alarms are present — this is a diagnostic view, not a gate.

    Output sections:
      [db]       connection state, table row counts
      [health]   harvest sentinel + staleness alarms from db_health.check()
    """
    import importlib.util

    # ── DB ping + row counts ───────────────────────────────────────────────
    try:
        conn = dbconn.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            size = cur.fetchone()[0]
            cur.execute("SELECT pg_size_pretty(%s::bigint)", (size,))
            pretty = cur.fetchone()[0]
            counts = {}
            for t in ("test_run", "test_result", "iteration_result", "process_summary"):
                cur.execute(f"SELECT count(*) FROM {t}")
                counts[t] = cur.fetchone()[0]
            # Per-run environment metadata coverage. These columns are never
            # gated on and do not show in the report — they are kept so a result
            # can be attributed to the tenant / dc / arch it actually ran on
            # (a case is green on one combo and red on another). Coverage is
            # reported because a field that silently stops being recorded is
            # invisible otherwise; expect bitness to be 0 for every run
            # predating the recorder field, and to grow from there.
            cur.execute("""SELECT count(*), count(tenant), count(dc), count(platform),
                          count(is_64_bit_cli), count(is_64_bit_installed)
                   FROM test_run""")
            meta = cur.fetchone()
        conn.close()
        print(f"[db]  connected  size={pretty}")
        for t, n in counts.items():
            print(f"      {t:20} {n:,}")
        total = meta[0] or 0
        print(f"[meta] per-run env coverage over {total:,} run(s):")
        for label, n in zip(
            ("tenant", "dc", "platform", "is_64_bit_cli", "is_64_bit_installed"),
            meta[1:],
        ):
            pct = f"{100.0 * n / total:.0f}%" if total else "n/a"
            print(f"      {label:20} {n:,} ({pct})")
    except Exception as e:
        print(f"[db]  UNREACHABLE — {e}")

    # ── harvest / cold-store health (reads local state files only) ─────────
    # GRS_DB_HEALTH_DIR: test-only override (test_db_status.py) so a stub
    # check() can be injected instead of the real one, which shells out to
    # schtasks/git and took 10-20s per test with no override (found
    # 2026-10-07) -- the test file already assumed this env var existed and
    # wrote a stub for it, but nothing here ever read it.
    _GRSTOOLS = os.environ.get("GRS_DB_HEALTH_DIR") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    health_path = os.path.join(_GRSTOOLS, "db_health.py")
    spec = importlib.util.spec_from_file_location("db_health", health_path)
    if spec:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        findings, info = mod.check()
        print(f"[health]  harvest / cold-store")
        for ln in info:
            print(f"  {ln}")
        if findings:
            print(f"  => {len(findings)} finding(s):")
            for f in findings:
                for part in f.splitlines():
                    print(f"    {part}")
        else:
            print("  => healthy")
    else:
        print("[health]  db_health.py not found next to db.py")
    return 0


def cmd_growth(a):
    conn = dbconn.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        size = cur.fetchone()[0]
        counts = {}
        for t in ("test_run", "test_result", "iteration_result", "process_summary"):
            cur.execute(f"SELECT count(*) FROM {t}")
            counts[t] = cur.fetchone()[0]
        cur.execute("SELECT pg_size_pretty(%s::bigint)", (size,))
        pretty = cur.fetchone()[0]
        print(f"db size: {pretty} ({size} bytes)")
        for t, c in counts.items():
            print(f"  {t:18} {c}")
        if a.record:
            cur.execute(
                """INSERT INTO db_growth
                   (db_size_bytes, n_runs, n_test_result, n_iteration, n_psummary)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    size,
                    counts["test_run"],
                    counts["test_result"],
                    counts["iteration_result"],
                    counts["process_summary"],
                ),
            )
            conn.commit()
            print("[ok] recorded db_growth row")
    conn.close()
    return 0


# ─────────────────────────── backup / restore ───────────────────────────

# Why this exists (2026-07-27): the DB VM was decommissioned with no dump ever
# taken. Nine of the ten tables are DERIVED — harvest.py keeps their inputs and
# `harvest.py ingest-all` replays them. `product_issue` is the exception: it is
# hand-entered triage (ticket <-> first-seen) with NO artifact source, so a lost
# DB loses it permanently. That single table is the reason a dump is mandatory.
#
# Implemented with COPY over the existing psycopg2 connection rather than
# pg_dump, so it runs from any box that can already reach the DB (the boxes that
# drive the scheduled jobs have psycopg2 but NOT the postgres client binaries).


def _table_columns(cur, table):
    """Real (non-generated) columns, in ordinal order.

    Generated columns (test_result.iterations_undone) must be excluded: COPY
    refuses to write them, and they are recomputed on insert anyway.
    """
    cur.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema='public' AND table_name=%s AND is_generated='NEVER'
           ORDER BY ordinal_position""",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def cmd_backup(a):
    import gzip
    import json
    import time

    out = a.out or os.path.join(
        os.path.expanduser("~"),
        "grs_tools",
        "db_backups",
        time.strftime("%Y%m%d-%H%M%S"),
    )
    os.makedirs(out, exist_ok=True)
    conn = dbconn.connect()
    manifest = {"created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "tables": {}}
    total = 0
    with conn.cursor() as cur:
        # parents first — the same order restore replays, so a half-written
        # backup is still restorable up to the table it stopped at
        for t in reversed(_TABLES):
            cols = _table_columns(cur, t)
            if not cols:
                print(f"[backup] {t:18} SKIP (table absent)")
                continue
            path = os.path.join(out, f"{t}.csv.gz")
            collist = ", ".join(f'"{c}"' for c in cols)
            with gzip.open(path, "wb", compresslevel=9) as f:
                cur.copy_expert(
                    f"COPY (SELECT {collist} FROM {t}) TO STDOUT WITH CSV", f
                )
            cur.execute(f"SELECT count(*) FROM {t}")
            rows = cur.fetchone()[0]
            size = os.path.getsize(path)
            total += size
            manifest["tables"][t] = {"rows": rows, "columns": cols, "bytes": size}
            print(f"[backup] {t:18} {rows:>7} rows  {size:>9} B")
    conn.close()
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"[ok] backup -> {out}  ({len(manifest['tables'])} tables, {total} bytes)")
    print(
        "[!] a backup that only ever sits on one host is not a backup — copy it off-box"
    )
    _prune_backups(os.path.dirname(os.path.abspath(out)))
    return 0


def _prune_backups(root, keep_daily=7, keep_days=3):
    """Retention for ~/grs_tools/db_backups, agreed by git, git22, git33 and git44 on 2026-11-18: keep rebuild-*, the
    newest keep_daily daily-* entries and every entry younger than keep_days; delete the rest. Only runs on the
    default backup root, never on a user-chosen --out directory."""
    import shutil
    import time

    default_root = os.path.abspath(
        os.path.join(os.path.expanduser("~"), "grs_tools", "db_backups")
    )
    if os.path.realpath(root) != os.path.realpath(default_root):
        return
    entries = [e for e in os.scandir(root) if e.is_dir()]
    daily = sorted((e for e in entries if e.name.startswith("daily-")), key=lambda e: e.name)
    newest_daily = {e.name for e in daily[-keep_daily:]}
    cutoff = time.time() - keep_days * 86400
    for e in entries:
        if e.name.startswith("rebuild-") or e.name in newest_daily:
            continue
        if e.stat().st_mtime >= cutoff:
            continue
        shutil.rmtree(e.path)
        print(f"[prune] removed old backup {e.name}")


def cmd_restore(a):
    import gzip
    import json

    src = a.dir
    mpath = os.path.join(src, "manifest.json")
    if not os.path.isfile(mpath):
        # ASCII only: SystemExit text goes to stderr, which (unlike stdout above)
        # is NOT re-wrapped as utf-8, so a non-ASCII dash lands as mojibake.
        raise SystemExit(
            f"[restore] no manifest.json in {src} - not a backup directory"
        )
    with open(mpath, encoding="utf-8") as f:
        manifest = json.load(f)
    print(f"[restore] backup created_at={manifest.get('created_at')}  from {src}")
    conn = dbconn.connect()
    with conn.cursor() as cur:
        if a.truncate:
            # one statement so CASCADE + the FK graph are resolved together
            cur.execute(f"TRUNCATE {', '.join(_TABLES)} CASCADE")
            print(f"[restore] truncated {len(_TABLES)} tables")
        else:
            for t in _TABLES:
                cur.execute(f"SELECT count(*) FROM {t}")
                n = cur.fetchone()[0]
                if n:
                    conn.rollback()
                    conn.close()
                    raise SystemExit(
                        f"[restore] {t} already has {n} rows. Restoring on top would "
                        f"duplicate rows or violate the run_id key. Re-run with --truncate "
                        f"to replace the contents (destructive), or restore into an empty DB."
                    )
        for t in reversed(_TABLES):  # parents before children
            info = manifest["tables"].get(t)
            path = os.path.join(src, f"{t}.csv.gz")
            if not info or not os.path.isfile(path):
                print(f"[restore] {t:18} SKIP (not in backup)")
                continue
            collist = ", ".join(f'"{c}"' for c in info["columns"])
            with gzip.open(path, "rb") as f:
                cur.copy_expert(f"COPY {t} ({collist}) FROM STDIN WITH CSV", f)
            cur.execute(f"SELECT count(*) FROM {t}")
            print(
                f"[restore] {t:18} {cur.fetchone()[0]:>7} rows (backup had {info['rows']})"
            )
        # COPY preserves the id values but leaves each sequence at its old value,
        # so the next INSERT would collide with a restored row. Re-point them.
        # is_called=false when the table is empty, so the sequence still hands out
        # 1 rather than skipping it.
        for t in _TABLES:
            cur.execute("SELECT pg_get_serial_sequence(%s,'id')", (t,))
            seq = cur.fetchone()[0]
            if not seq:  # table absent, or its id is not sequence-backed
                continue
            cur.execute(
                f"SELECT setval(%s, COALESCE(max(id),1), count(*) > 0) FROM {t}", (seq,)
            )
    conn.commit()
    conn.close()
    print("[ok] restore complete (id sequences re-pointed)")
    return 0


# ─────────────────── gate triage tickets (issue) ───────────────────
# The ticket column in the report's Health-gate breakdown is written ONLY from
# here — no hand-SQL (the mapping is hand-entered and unrecoverable, so it needs
# one auditable entry point). Grain = one breakdown row: job + build + test case.
# An ambiguous build is NEVER resolved silently (fault ledger 45): the candidates
# are printed and the command exits non-zero.

# One row per Health-gate breakdown row, with whatever ticket is attached. Same
# join + same ticket sub-select as report.py's table, so the CLI and the HTML can
# never disagree about which row a ticket lands on.
_GATE_ROWS_SQL = """
    SELECT run.jenkins_job          AS job,
           run.jenkins_build        AS build,
           COALESCE(rel.label,'-')  AS release,
           COALESCE(to_char(COALESCE(tr.started_at, run.started_at),
                            'YYYY-MM-DD HH24:MI'), '-') AS datetime,
           COALESCE(run.product_version,'-') AS version,
           tr.test_name             AS test,
           gv.violation_type        AS gate,
           COALESCE(gv.process_name,'-')     AS process,
           COALESCE((SELECT string_agg(DISTINCT gt.ticket_id, ', ')
                       FROM gate_ticket gt
                      WHERE gt.jenkins_job   = run.jenkins_job
                        AND gt.jenkins_build = run.jenkins_build
                        AND (gt.test_name IS NULL OR gt.test_name = tr.test_name)),
                    '-') AS ticket
      FROM gate_violation gv
      JOIN test_result tr  ON tr.id  = gv.test_result_id
      JOIN test_run    run ON run.id = tr.run_id
      LEFT JOIN release rel ON rel.id = run.release_id
     WHERE (%(releases)s IS NULL OR rel.label = ANY(%(releases)s))
       AND (%(build)s::int IS NULL OR run.jenkins_build = %(build)s::int)
     ORDER BY run.jenkins_build DESC, tr.test_name, gv.violation_type
"""


def _gate_rows(cur, releases=None, build=None):
    cur.execute(_GATE_ROWS_SQL, {"releases": releases, "build": build})
    return [d[0] for d in cur.description], cur.fetchall()


def _canon_job(job):
    """Canonical DB job name for a lane alias (reg, reg2, local1, ...; any case), else None.

    A lane alias names exactly one job. Matching it as a substring ("%reg%") also hits every job whose name merely
    contains it: REG matched REG2, so a ticket, a timeout median or an iteration-time sample for REG could land on
    REG2. Callers use this to match the alias EXACTLY and fall back to a substring only for free text."""
    if not job:
        return None
    jp = _JOB_ALIASES.get(job) or _JOB_ALIASES.get(str(job).lower())
    return jp.replace("/job/", "/") if jp else None


def _resolve_build(cur, build, job=None, release=None):
    """(job, build) -> exactly one lane, or None after printing the candidates.

    Build numbers are per-job, so `--build 87` alone can name a run in REG and
    another in LOCAL2. Returning the first match would attach the ticket to
    whichever lane sorted first — the ledger-45 shape — so the ambiguity is
    handed back to the caller instead.

    `job` may be an alias key ("reg2") or a raw substring ("REG-02") — an
    alias's canonical form ("REG-02") is NOT a substring of its own short key
    ("reg2" has no hyphen, so ILIKE '%reg2%' never matches "...REG-02"). Found
    2026-09-08: this silently sent a build WITH a real test_run down the
    orphan/no-test_run fallback path just because the caller passed the alias
    key. Try both: the literal string the caller gave, OR (if it resolves to a
    known alias) that alias's canonical job path.

    `release`: found 2026-10-07 — `clone-release` can leave the SAME
    (jenkins_job, jenkins_build) pointing at two release rows (e.g.
    "release-142" and its "-final" snapshot), so --job alone cannot
    disambiguate when both candidates share the identical job string. If
    given, narrows the candidate set by release label (via _norm_release)
    before falling back to the ambiguity error.
    """
    canon = _canon_job(job)
    cur.execute(
        """SELECT run.jenkins_job, run.jenkins_build, COALESCE(rel.label,'-'),
                  COALESCE(run.product_version,'-'), run.started_at
             FROM test_run run
             LEFT JOIN release rel ON rel.id = run.release_id
            WHERE run.jenkins_build = %(build)s::int
              AND (%(job)s IS NULL
                   OR run.jenkins_job ILIKE '%%' || %(job)s || '%%'
                   OR (%(canon)s IS NOT NULL AND run.jenkins_job = %(canon)s))
            ORDER BY run.jenkins_job""",
        {"build": build, "job": job, "canon": canon},
    )
    cands = cur.fetchall()
    if canon and len(cands) > 1:
        # An alias key names exactly one lane. The ILIKE branch above also drags in every lane whose name merely
        # contains the key ("reg" matches ...REG2 too), so REG could never be picked on its own (2026-10-22: the
        # same build number 232 exists on REG and REG2). Prefer the exact canonical job when it is among the hits.
        exact = [c for c in cands if c[0] == canon]
        if exact:
            cands = exact
    if not cands:
        # Fallback (2026-09-08): a build that died before pytest ever ran (docker
        # image missing, git checkout timeout, ...) produces NOTHING ingestable,
        # so it has no test_run row at all -- not just no gate row. That build is
        # exactly the kind an infra ticket needs linking to, so if --job names a
        # known alias, synthesize the (job, build) pair instead of refusing outright.
        # gate_ticket stores job/build as free text (no FK to test_run), so this is
        # safe to insert; the report/list side already handles a ticket with no
        # matching run (prints it under "orphans").
        if canon:
            print(
                f"[note] no test_run row for build {build} on {canon} (this build "
                "never produced ingestable data -- likely an infra-level failure). "
                "Linking by job alias instead."
            )
            return (canon, int(build), "-", "-", None)
        where = f" matching job ~ '{job}'" if job else ""
        print(f"[fail] no run with build {build}{where}")
        print("       `db.py issue list` shows every build that has a gate row")
        return None
    if len(cands) > 1 and release:
        wanted = set(_norm_release(release) or [])
        narrowed = [c for c in cands if c[2] in wanted]
        if narrowed:
            cands = narrowed
    if len(cands) > 1:
        print(
            f"[fail] build {build} is ambiguous across {len(cands)} lanes — pass --job"
            f"{'' if job else ' and/or --release'}:"
        )
        _print_table(["job", "build", "release", "version", "started_at"], cands)
        return None
    return cands[0]


def _resolve_gate_test(cur, job, build, pattern):
    """Fuzzy --test within one run. Returns (test_name, rows) or (None, None).

    Matches against the cases that actually HAVE a gate violation in this build,
    because that is the set the breakdown table can show. A miss lists them
    rather than silently tagging nothing (ledger 11).

    Fallback (2026-09-08): a build that never reached pytest at all (infra/
    build-level failure -- docker image missing, git checkout timeout, ...)
    has ZERO gate rows, so it could never be tagged with a specific test_name
    before this -- only the whole-build (test_name=NULL) link, which the new
    Iteration-efficiency ticket column (keyed by test_name) would never see.
    If the build has no gate row AT ALL for `pattern`, fall back to matching
    against every test_name the DB has ever seen (any release/lane) -- still
    refuses on a miss/ambiguous match, same anti-typo guarantee as before.
    """
    _, rows = _gate_rows(cur, build=build)
    rows = [r for r in rows if r[0] == job]
    names = sorted({r[5] for r in rows})
    if pattern is None:
        return None, rows  # whole build
    hit = _one_case(names, pattern)
    if len(hit) == 1:
        return hit[0], [r for r in rows if r[5] == hit[0]]
    if not names:
        cur.execute("SELECT DISTINCT test_name FROM test_result ORDER BY 1")
        all_names = [r[0] for r in cur.fetchall()]
        hit = _one_case(all_names, pattern)
        if len(hit) == 1:
            print(
                f"[note] build {build} has no gate violation for {hit[0]!r} -- "
                "linking by test_name anyway (known case, just not a gate row here)."
            )
            return hit[0], []
        if len(hit) > 1:
            print(
                f"[fail] --test {pattern!r} is ambiguous ({len(hit)} matches) among ALL known cases:"
            )
            for n in hit:
                print(f"         {n}")
            return None, None
    verb = "matches nothing" if not hit else f"is ambiguous ({len(hit)} matches)"
    print(f"[fail] --test {pattern!r} {verb} among the gate rows of build {build}:")
    for n in names:
        print(f"         {n}")
    if not names:
        print(
            "         (this build has no gate violation at all, and no known case matched either)"
        )
    return None, None


def cmd_backfill_bitness(a):
    """Backfill test_run.is_64_bit_cli from each build's OWN Jenkins params.

    The ndjson recorder never wrote `is_64_bit` into the run record until the
    2026-09-13 fix (iteration_recorder.py) -- every test_run row ingested
    before that fix has is_64_bit_cli=NULL. The CLI flag is still recoverable
    per-build from Jenkins' own build parameters (tenant_info's --is_64_bit),
    one read-only GET per build. `is_64_bit_installed` (the actually-probed
    arch) is NOT recoverable this way -- that probe simply never ran for old
    builds, the data does not exist anywhere, and this command does not try.

    Most builds outside Jenkins' own retention window (~7-14 days) will 404;
    those are counted as unreachable, not treated as an error.
    """
    conn = dbconn.connect()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id, jenkins_job, jenkins_build FROM test_run
                WHERE is_64_bit_cli IS NULL
                ORDER BY jenkins_build DESC""")
        pending = cur.fetchall()
        print(
            f"[backfill-bitness] {len(pending)} test_run row(s) missing is_64_bit_cli"
        )
        updated = unreachable = omitted = 0
        for tr_id, job, build in pending:
            if not job or not build:
                unreachable += 1
                continue
            jpath = "/job/".join(job.split("/"))
            try:
                d = _gj_get(
                    f"job/{jpath}/{build}/api/json?tree=actions[parameters[name,value]]"
                )
            except Exception:
                unreachable += 1
                continue
            tenant_info = _gj_params(d.get("actions")).get("tenant_info") or ""
            m = re.search(r"--is_64_bit[= ](\S+)", tenant_info)
            if not m:
                # Flag omitted = 32-bit convention (config_fixtures.py's is_64_bit
                # fixture: only true/1/yes count as 64-bit; omitted/false/etc -> 32-bit).
                is64 = False
                omitted += 1
            else:
                is64 = m.group(1).strip().lower() in (
                    "true",
                    "1",
                    "yes",
                    "y",
                    "64",
                    "x64",
                )
            if a.dry_run:
                print(f"  [dry-run] {job} #{build}: is_64_bit_cli -> {is64}")
                continue
            cur.execute(
                "UPDATE test_run SET is_64_bit_cli=%s WHERE id=%s", (is64, tr_id)
            )
            updated += 1
        if a.dry_run:
            print("[dry-run] no rows written")
        else:
            conn.commit()
        print(
            f"[backfill-bitness] updated={updated} unreachable(gone from Jenkins)={unreachable} "
            f"flag-omitted(32bit)={omitted}"
        )
        return 0
    finally:
        conn.close()


def cmd_fix_mac_bitness(a):
    """Set test_run.is_64_bit_installed=TRUE for every mac run that doesn't
    already say so.

    Mac is a known constant, not a measurement: the Netskope Mac client is
    64-bit only (no 32-bit macOS build exists). Runs ingested before the
    2026-09-13 install-path probe fix (or where the probe otherwise came back
    NULL) fell back to displaying the CLI --is_64_bit param, which is
    meaningless for mac and produced a bogus "32-bit?" in reports/query. This
    is the one-time backfill for existing rows; `ingest.py::installed_bitness_of`
    now sets this correctly for every future ingest, mac or not.
    """
    conn = dbconn.connect()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id, jenkins_job, jenkins_build, is_64_bit_installed
               FROM test_run
               WHERE platform = 'mac'
                 AND (is_64_bit_installed IS NULL OR is_64_bit_installed = FALSE)
               ORDER BY jenkins_build DESC""")
        pending = cur.fetchall()
        print(f"[fix-mac-bitness] {len(pending)} mac test_run row(s) need correction")
        if a.dry_run:
            for tr_id, job, build, old in pending:
                print(f"  [dry-run] {job} #{build}: is_64_bit_installed {old} -> True")
            print("[dry-run] no rows written")
            return 0
        cur.execute("""UPDATE test_run SET is_64_bit_installed = TRUE
               WHERE platform = 'mac'
                 AND (is_64_bit_installed IS NULL OR is_64_bit_installed = FALSE)""")
        n = cur.rowcount
        conn.commit()
        print(f"[fix-mac-bitness] updated={n}")
        return 0
    finally:
        conn.close()


def _issue_ext(a):
    """`issue ext`: a ticket of a release that never ran on our lanes (no build, no test_run). Stored with job '-' and build 0
    so the release's Tickets table shows it; unknown fields stay empty or '-'. --version is the only required fact."""
    if not (a.ticket and a.version):
        print("[fail] `issue ext` needs <TICKET> and --version")
        print("       e.g. db.py issue ext ENG-1409583 --version 144.0.0.7459 --test stress-05")
        return 2
    major = a.report_release or a.version.split(".")[0]
    test = a.test or None
    conn = dbconn.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, report_release FROM gate_ticket WHERE ticket_id=%s AND jenkins_job='-' AND jenkins_build=0""",
            (a.ticket,),
        )
        row = cur.fetchone()
        if row and row[1] != major:
            # uq_gt_build allows ONE no-build row per ticket: moving it would silently drop the ticket from release row[1]
            print(f"[fail] {a.ticket} already has its no-build row on release {row[1]}; the table holds one per ticket, so it cannot also sit on {major}")
            return 2
        # A ticket of a release that never ran on our lanes is a 64-bit client unless --bitness 32 says otherwise
        # (owner: every bitness shown in the Tickets table is 64-bit), so the table never prints "-" for it.
        is64 = a.bitness != "32"
        if row:
            # --test given -> replace the stored test name; omitted -> keep what is stored
            cur.execute(
                """UPDATE gate_ticket SET note=COALESCE(%s, note), product_version=%s, report_release=%s,
                          test_name=COALESCE(%s, test_name), is_64_bit=%s WHERE id=%s RETURNING test_name""",
                (a.note, a.version, major, test, is64, row[0]),
            )
            test = cur.fetchone()[0]  # what is stored now, so the line below does not claim "-" for a kept name
            verb = "updated"
        else:
            cur.execute(
                """INSERT INTO gate_ticket (ticket_id, jenkins_job, jenkins_build, test_name, note, product_version,
                                            is_64_bit, report_release)
                   VALUES (%s,'-',0,%s,%s,%s,%s,%s)""",
                (a.ticket, test, a.note, a.version, is64, major),
            )
            verb = "added"
        conn.commit()
        print(
            f"[ok] {verb} {a.ticket} (version {a.version}, {'64' if is64 else '32'}-bit, test {test or '-'}) "
            f"on the release {major} Tickets table, no build"
        )
        return 0
    finally:
        conn.close()


def _issue_info(a):
    """`issue info`: set the category and/or title of a ticket on every row it has (per-ticket facts for the report)."""
    if not a.ticket or not (a.category or a.summary):
        print("[fail] `issue info` needs <TICKET> and --category and/or --summary")
        print('       e.g. db.py issue info ENG-1409583 --category crash --summary "stAgentSvc crash stAgentSvc!nsssl::wait+0x3b9"')
        return 2
    conn = dbconn.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE gate_ticket SET category=COALESCE(%s, category), summary=COALESCE(%s, summary) WHERE ticket_id=%s",
            (a.category, a.summary, a.ticket),
        )
        n = cur.rowcount
        if n == 0:
            print(f"[fail] {a.ticket} has no row in gate_ticket; add it first with `issue set` or `issue ext`")
            return 2
        conn.commit()
        print(f"[ok] {a.ticket}: category={a.category or '(kept)'} summary={'set' if a.summary else '(kept)'} on {n} row(s)")
        return 0
    finally:
        conn.close()


def cmd_issue(a):
    if a.action == "ext":
        return _issue_ext(a)
    if a.action == "info":
        return _issue_info(a)
    # Validate BEFORE connecting: a missing --build would otherwise reach the DB
    # as NULL and come back "no run with build None", which reads like a data
    # problem instead of a usage error.
    if a.action in ("set", "rm"):
        # build 0 is the no-build row `issue ext` writes; only `rm` may name it
        missing = [
            n
            for n, v in (("<TICKET>", a.ticket), ("--build", a.build))
            if not v and not (n == "--build" and a.action == "rm" and a.build == 0)
        ]
        if missing:
            print(f"[fail] `issue {a.action}` needs {' and '.join(missing)}")
            print(
                f"       e.g. db.py issue {a.action} ENG-1180143 --build 87 --job REG-02"
            )
            return 2
    conn = dbconn.connect()
    try:
        cur = conn.cursor()
        if a.action == "list":
            cols, rows = _gate_rows(cur, _norm_release(a.release), a.build)
            _print_table(cols, rows)
            print(f"\n{len(rows)} gate row(s)")
            # A ticket attached to a build/case with no gate violation would be
            # invisible in the report; say so here rather than let it look applied.
            cur.execute("""SELECT gt.ticket_id, gt.jenkins_job, gt.jenkins_build,
                          COALESCE(gt.test_name,'(whole build)'), gt.note
                     FROM gate_ticket gt
                    WHERE gt.jenkins_job <> '-' AND NOT EXISTS (
                          SELECT 1 FROM gate_violation gv
                            JOIN test_result tr ON tr.id = gv.test_result_id
                            JOIN test_run   run ON run.id = tr.run_id
                           WHERE run.jenkins_job   = gt.jenkins_job
                             AND run.jenkins_build = gt.jenkins_build
                             AND (gt.test_name IS NULL OR gt.test_name = tr.test_name))
                    ORDER BY gt.jenkins_build DESC""")
            orphans = cur.fetchall()
            if orphans:
                print(
                    f"\n[warn] {len(orphans)} ticket(s) match NO gate row (not shown in the report):"
                )
                _print_table(["ticket", "job", "build", "test", "note"], orphans)
            return 0

        if a.action == "rm" and a.build == 0:
            cur.execute(
                """DELETE FROM gate_ticket WHERE ticket_id=%s AND jenkins_job='-' AND jenkins_build=0
                      AND test_name IS NOT DISTINCT FROM %s""",
                (a.ticket, a.test or None),
            )
            conn.commit()
            print(f"[ok] removed {cur.rowcount} no-build row(s) of {a.ticket}")
            return 0 if cur.rowcount else 2

        run = _resolve_build(cur, a.build, a.job, a.release)
        if run is None:
            return 2
        job, build = run[0], run[1]
        test, rows = _resolve_gate_test(cur, job, build, a.test)
        if rows is None:
            return 2

        if a.action == "set":
            # Without --test the link was stored with test_name NULL and the report printed "(whole build)"
            # next to a real lane and run. When the build's gate hits name exactly one case, store that case.
            if test is None and rows:
                one = {r[5] for r in rows}
                if len(one) == 1:
                    test = next(iter(one))
                    print(f"[note] the build's gate hits are all {test!r} -- storing that case, not a blank test name.")
            # A ticket links to a real build+version, not a bare test name
            # (owner, 2026-09-08) -- auto-fill from the matching test_run when
            # one exists; --version/--bitness always override (the only way to
            # set it when the build has no test_run at all).
            cur.execute(
                """SELECT product_version, is_64_bit_installed FROM test_run
                    WHERE jenkins_job=%s AND jenkins_build=%s""",
                (job, build),
            )
            run_row = cur.fetchone()
            version = a.version or (run_row[0] if run_row else None)
            bitness = (
                {"64": True, "32": False}[a.bitness]
                if a.bitness
                else (run_row[1] if run_row else None)
            )
            if not version:
                print(
                    "[warn] no --version given and no test_run to derive it from -- "
                    "this ticket will NOT be scoped to any release's report page "
                    "until you re-run with --version."
                )
            cur.execute(
                """SELECT id, note FROM gate_ticket
                    WHERE ticket_id=%s AND jenkins_job=%s AND jenkins_build=%s
                      AND test_name IS NOT DISTINCT FROM %s""",
                (a.ticket, job, build, test),
            )
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    """UPDATE gate_ticket
                          SET note=COALESCE(%s, note),
                              product_version=COALESCE(%s, product_version),
                              is_64_bit=COALESCE(%s, is_64_bit),
                              report_release=COALESCE(%s, report_release)
                        WHERE id=%s""",
                    (a.note, version, bitness, a.report_release, existing[0]),
                )
                print(
                    f"[ok] {a.ticket} already on {job} build {build}"
                    f"{'' if test is None else ' / ' + test} — note/version/bitness/report_release refreshed"
                )
            else:
                cur.execute(
                    """INSERT INTO gate_ticket
                           (ticket_id, jenkins_job, jenkins_build, test_name, note,
                            product_version, is_64_bit, report_release)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (a.ticket, job, build, test, a.note, version, bitness, a.report_release),
                )
                print(
                    f"[ok] {a.ticket} -> {job} build {build}"
                    f"{' / whole build' if test is None else ' / ' + test}"
                    f" (version={version or '-'}, bitness={'-' if bitness is None else ('64' if bitness else '32')}"
                    f", report_release={a.report_release or '(unset, normal behavior)'})"
                )
            # A ticket is scoped by the release its run is filed under and shows in the report's Tickets table; it needs no gate row.
            cur.execute(
                """SELECT r.label FROM test_run run JOIN release r ON r.id=run.release_id
                    WHERE run.jenkins_job=%s AND run.jenkins_build=%s ORDER BY run.id LIMIT 1""",
                (job, build),
            )
            filed = cur.fetchone()
            where = a.report_release or (filed[0] if filed else None)
            print(f"[info] the ticket shows in the Tickets table of {where}" if where else "[warn] no run is stored for this build: the ticket is scoped by its stored version")
            if rows:
                print("       and in the Health-gate breakdown rows it matches.")
        else:  # rm
            cur.execute(
                """DELETE FROM gate_ticket
                    WHERE ticket_id=%s AND jenkins_job=%s AND jenkins_build=%s
                      AND (%s::text IS NULL OR test_name = %s)""",
                (a.ticket, job, build, test, test),
            )
            print(
                f"[ok] removed {cur.rowcount} link(s) for {a.ticket} on {job} build {build}"
            )
        conn.commit()

        # Read the state back: an UPDATE/INSERT that "succeeded" is not evidence
        # that the report row now carries the ticket.
        cols, after = _gate_rows(cur, build=build)
        after = [r for r in after if r[0] == job]
        print("\nbreakdown row(s) for that build after the change:")
        _print_table(cols, after)
        return 0
    finally:
        conn.close()


# ─────────────────────────── query ───────────────────────────

_QUERIES = {
    # per-case breakthrough baseline: peak + sustained per process, one release
    "breakthrough": (
        """SELECT tr.test_name, ps.process_name,
                  round(avg(ps.cpu_max_pct),1)   AS avg_cpu_peak,
                  max(ps.cpu_max_pct)            AS worst_cpu_peak,
                  round(avg(ps.cpu_sustained_s),1) AS avg_sustained_s,
                  round(avg(ps.mem_growth_mb),1) AS avg_mem_growth,
                  max(ps.handles_max)            AS worst_handles,
                  count(*)                       AS runs
           FROM process_summary ps
           JOIN test_result tr ON tr.id = ps.test_result_id
           JOIN test_run run   ON run.id = tr.run_id
           LEFT JOIN release r ON r.id = run.release_id
           WHERE (%(test)s IS NULL OR tr.test_name = %(test)s)
             AND (%(release)s IS NULL OR r.label = %(release)s)
           GROUP BY tr.test_name, ps.process_name
           ORDER BY tr.test_name, ps.process_name""",
        ("test", "release"),
    ),
    # iteration verdict sequence per test (red/green pattern) for one release
    "sequence": (
        """SELECT tr.test_name, run.run_id, COALESCE(r.label,'(none)') AS release,
                  run.started_at, it.iter_index, it.status,
                  round(it.duration_s,1) AS dur_s
           FROM iteration_result it
           JOIN test_result tr ON tr.id = it.test_result_id
           JOIN test_run run   ON run.id = tr.run_id
           LEFT JOIN release r ON r.id = run.release_id
           WHERE (%(test)s IS NULL OR tr.test_name = %(test)s)
             AND (%(release)s IS NULL OR r.label = %(release)s)
           ORDER BY tr.test_name, run.started_at, it.iter_index""",
        ("test", "release"),
    ),
    # every dump found in a release + its test background
    "dumps": (
        """SELECT r.label, tr.test_name, d.dump_type, d.process_name,
                  d.file_name, d.event_id, d.captured_at
           FROM dump d
           JOIN test_result tr ON tr.id = d.test_result_id
           JOIN test_run run   ON run.id = tr.run_id
           LEFT JOIN release r ON r.id = run.release_id
           WHERE (%(release)s IS NULL OR r.label = %(release)s)
           ORDER BY d.captured_at""",
        ("release",),
    ),
    # one-shot mac health overview for a release: per mac case, how many results, how many tripped the
    # health gate, how many crash/bsod gate violations + dumps, and the WORST cpu (per-core %), memory
    # growth and handles over every process. Answers "any mac crash / leak?" without reading consoles.
    "mac-health": (
        """WITH base AS (
             SELECT tr.id, tr.test_name, tr.gate_violated, tr.has_dump
             FROM test_result tr
             JOIN test_run run ON run.id = tr.run_id
             LEFT JOIN release r ON r.id = run.release_id
             WHERE run.platform = 'mac'
               AND (%(release)s IS NULL OR r.label = %(release)s)
               AND run.run_id NOT LIKE '%%-int'
           ),
           ps AS (
             SELECT test_result_id, max(cpu_max_pct) AS cpu, max(mem_growth_mb) AS mem_g, max(handles_max) AS h
             FROM process_summary GROUP BY test_result_id
           ),
           gv AS (
             SELECT test_result_id,
                    count(*) FILTER (WHERE violation_type IN ('crash_dump','bsod_dump')) AS crash_viol
             FROM gate_violation GROUP BY test_result_id
           )
           SELECT b.test_name,
                  count(*)                                    AS results,
                  count(*) FILTER (WHERE b.gate_violated)     AS gate_hits,
                  COALESCE(sum(gv.crash_viol), 0)             AS crash_viol,
                  count(*) FILTER (WHERE b.has_dump)          AS has_dump,
                  round(max(ps.cpu)::numeric, 1)              AS worst_cpu_pct,
                  round(max(ps.mem_g)::numeric, 1)            AS worst_mem_growth_mb,
                  max(ps.h)                                   AS worst_handles
           FROM base b
           LEFT JOIN ps ON ps.test_result_id = b.id
           LEFT JOIN gv ON gv.test_result_id = b.id
           GROUP BY b.test_name
           ORDER BY gate_hits DESC, crash_viol DESC, b.test_name""",
        ("release",),
    ),
    # per RESULT memory growth of one case, every process, worst first: is a gate hit a one-off or the usual size? `compare`
    # averages (positive and negative growth cancel) and `mac-health` keeps only the maximum, so neither answers that.
    "proc-growth": (
        """SELECT run.jenkins_job, run.jenkins_build, r.label AS release, run.started_at::date AS day,
                  ps.process_name, ps.mem_baseline_mb, ps.mem_growth_mb, ps.mem_max_mb, ps.sample_count, tr.verdict
           FROM process_summary ps
           JOIN test_result tr ON tr.id = ps.test_result_id
           JOIN test_run run   ON run.id = tr.run_id
           LEFT JOIN release r ON r.id = run.release_id
           WHERE tr.test_name = %(test)s
             AND (%(release)s IS NULL OR r.label = %(release)s)
             AND run.run_id NOT LIKE '%%-int'
             AND run.run_id NOT LIKE '%%-dup%%'
           ORDER BY ps.mem_growth_mb DESC NULLS LAST
           LIMIT 40""",
        ("test", "release"),
    ),
    # the numbers behind the report's cross-version CPU / memory charts (windows, majors 140-144, the three charted
    # processes): one row per major x release label x product_version x process, so a low or a surprise point can be
    # traced to the exact builds, test count and days that make it. --release scopes it to one label (e.g. release-141-final).
    "proc-by-version": (
        """SELECT substring(run.product_version from '^(\\d+)')::int AS major,
                  COALESCE(r.label,'(none)') AS release, run.product_version, ps.process_name,
                  count(*) FILTER (WHERE ps.alive_pct > 0 AND ps.mem_max_mb > 0) AS samples,
                  count(*) AS rows_all, run.platform,
                  count(DISTINCT tr.test_name) AS tests, count(DISTINCT run.id) AS runs,
                  round(avg(ps.mem_baseline_mb) FILTER (WHERE ps.alive_pct > 0 AND ps.mem_max_mb > 0),1) AS avg_base_mb,
                  round(avg(ps.mem_max_mb) FILTER (WHERE ps.alive_pct > 0 AND ps.mem_max_mb > 0),1) AS avg_peak_mb,
                  round(avg(ps.cpu_max_pct) FILTER (WHERE ps.alive_pct > 0),1) AS avg_cpu_peak,
                  min(run.started_at)::date AS first_day, max(run.started_at)::date AS last_day
           FROM process_summary ps
           JOIN test_result tr ON tr.id = ps.test_result_id
           JOIN test_run run   ON run.id = tr.run_id
           LEFT JOIN release r ON r.id = run.release_id
           WHERE ps.process_name IN ('demAgentSvc.exe', 'stAgentSvc.exe', 'stAgentUI.exe')
             AND run.product_version ~ '^(14[0-4]\\.|211\\.100\\.100\\.10556$)'
             AND (%(release)s IS NULL OR r.label = %(release)s)
           GROUP BY major, r.label, run.product_version, ps.process_name, run.platform
           ORDER BY ps.process_name, major, r.label, run.product_version""",
        ("release",),
    ),
    # same charted numbers split by TENANT (windows, majors 140-144): the add-on features a tenant has (NPA, DEM, EPDLP...)
    # move client memory, so a version whose tests ran on other tenants than its neighbours shows a step that is not the build.
    "proc-by-tenant": (
        """SELECT substring(run.product_version from '^(\\d+)')::int AS major, run.tenant, ps.process_name,
                  count(*) AS samples, count(DISTINCT run.id) AS runs, count(DISTINCT tr.test_name) AS tests,
                  round(avg(ps.mem_baseline_mb),1) AS avg_base_mb, round(avg(ps.mem_max_mb),1) AS avg_peak_mb
           FROM process_summary ps
           JOIN test_result tr ON tr.id = ps.test_result_id
           JOIN test_run run   ON run.id = tr.run_id
           LEFT JOIN release r ON r.id = run.release_id
           WHERE run.platform = 'windows' AND ps.alive_pct > 0 AND ps.mem_max_mb > 0
             AND ps.process_name IN ('stAgentSvc.exe', 'stAgentUI.exe')
             AND run.product_version ~ '^14[0-4]\\.'
             AND run.product_version !~ '^142\\.100\\.100\\.'
             AND (%(release)s IS NULL OR r.label = %(release)s)
           GROUP BY major, run.tenant, ps.process_name
           ORDER BY ps.process_name, major, run.tenant""",
        ("release",),
    ),
    # the single rows behind each charted point: per major, the six highest memory peaks of stAgentUI / demAgentSvc, with
    # the build, test and tenant that produced them. A "worst peak" chart point is ONE row; this names it.
    "proc-top": (
        """SELECT * FROM (
             SELECT substring(run.product_version from '^(\\d+)')::int AS major, ps.process_name, run.jenkins_job,
                    run.jenkins_build, run.product_version, run.tenant, tr.test_name, ps.mem_baseline_mb, ps.mem_max_mb,
                    row_number() OVER (PARTITION BY substring(run.product_version from '^(\\d+)'), ps.process_name
                                       ORDER BY ps.mem_max_mb DESC) AS rk
             FROM process_summary ps
             JOIN test_result tr ON tr.id = ps.test_result_id
             JOIN test_run run   ON run.id = tr.run_id
             LEFT JOIN release r ON r.id = run.release_id
             WHERE run.platform = 'windows' AND ps.alive_pct > 0 AND ps.mem_max_mb > 0
               AND ps.process_name IN ('stAgentUI.exe', 'demAgentSvc.exe', 'stAgentSvc.exe')
               AND run.product_version ~ '^14[0-4]\\.'
               AND run.product_version !~ '^142\\.100\\.100\\.'
               AND (%(release)s IS NULL OR r.label = %(release)s)
           ) t WHERE rk <= 6 ORDER BY process_name, major, rk""",
        ("release",),
    ),
    # which Jenkins lane ran which tenant, per major (windows, stAgentSvc): shows WHY one tenant dominates a version's numbers.
    "proc-lane-tenant": (
        """SELECT substring(run.product_version from '^(\\d+)')::int AS major, run.jenkins_job, run.tenant,
                  count(DISTINCT run.id) AS runs, count(*) AS samples,
                  min(run.started_at)::date AS first_day, max(run.started_at)::date AS last_day,
                  round(avg(ps.mem_baseline_mb),1) AS avg_base_mb, round(avg(ps.mem_max_mb),1) AS avg_peak_mb
           FROM process_summary ps
           JOIN test_result tr ON tr.id = ps.test_result_id
           JOIN test_run run   ON run.id = tr.run_id
           LEFT JOIN release r ON r.id = run.release_id
           WHERE run.platform = 'windows' AND ps.alive_pct > 0 AND ps.mem_max_mb > 0
             AND ps.process_name = 'stAgentSvc.exe'
             AND run.product_version ~ '^14[0-4]\\.' AND run.product_version !~ '^142\\.100\\.100\\.'
             AND (%(release)s IS NULL OR r.label = %(release)s)
           GROUP BY major, run.jenkins_job, run.tenant
           ORDER BY major, run.tenant, run.jenkins_job""",
        ("release",),
    ),
    # one test case, every run of it (windows, stAgentSvc): build, version, lane, tenant, baseline, peak, growth. Use --test.
    "proc-test-detail": (
        """SELECT substring(run.product_version from '^(\\d+)')::int AS major, COALESCE(r.label,'(none)') AS release,
                  run.jenkins_job, run.jenkins_build, run.product_version, run.tenant, run.dc,
                  ps.mem_baseline_mb, ps.mem_max_mb, ps.mem_growth_mb, ps.sample_count, tr.verdict
           FROM process_summary ps
           JOIN test_result tr ON tr.id = ps.test_result_id
           JOIN test_run run   ON run.id = tr.run_id
           LEFT JOIN release r ON r.id = run.release_id
           WHERE tr.test_name = %(test)s AND run.platform = 'windows' AND ps.process_name = 'stAgentSvc.exe'
             AND ps.alive_pct > 0 AND ps.mem_max_mb > 0
             AND run.product_version ~ '^14[0-4]\\.' AND run.product_version !~ '^142\\.100\\.100\\.'
             AND (%(release)s IS NULL OR r.label = %(release)s)
           ORDER BY major, run.started_at""",
        ("test", "release"),
    ),
    # every test case x lane (windows, stAgentSvc, majors 143-144, private bytes): runs, the highest start (baseline) and peak, and
    # how many runs started / peaked at or above 110 and 200 MB. Sizes a memory gate: a case that already STARTS above a level
    # trips it on its first look, whatever it does. --release scopes it to one label.
    "proc-case-lane": (
        """SELECT tr.test_name, run.jenkins_job, count(*) AS runs,
                  round(max(ps.mem_baseline_mb),0) AS max_base_mb, round(max(ps.mem_max_mb),0) AS max_peak_mb,
                  count(*) FILTER (WHERE ps.mem_baseline_mb >= 110) AS base_ge_110,
                  count(*) FILTER (WHERE ps.mem_max_mb >= 110) AS peak_ge_110,
                  count(*) FILTER (WHERE ps.mem_baseline_mb >= 200) AS base_ge_200,
                  count(*) FILTER (WHERE ps.mem_max_mb >= 200) AS peak_ge_200
           FROM process_summary ps
           JOIN test_result tr ON tr.id = ps.test_result_id
           JOIN test_run run   ON run.id = tr.run_id
           LEFT JOIN release r ON r.id = run.release_id
           WHERE run.platform = 'windows' AND ps.process_name = 'stAgentSvc.exe'
             AND ps.alive_pct > 0 AND ps.mem_max_mb > 0
             AND run.product_version ~ '^14[34]\\.' AND run.product_version !~ '^142\\.100\\.100\\.'
             AND (%(release)s IS NULL OR r.label = %(release)s)
           GROUP BY tr.test_name, run.jenkins_job
           ORDER BY max_peak_mb DESC, tr.test_name""",
        ("release",),
    ),
    # what releases / product versions are in the DB (proves nothing is filtered)
    "versions": (
        """SELECT COALESCE(r.label,'(none)') AS release,
                  run.product_version,
                  count(DISTINCT run.id) AS runs,
                  count(DISTINCT tr.test_name) AS tests
           FROM test_run run
           LEFT JOIN release r  ON r.id = run.release_id
           LEFT JOIN test_result tr ON tr.run_id = run.id
           GROUP BY r.label, r.ordinal, run.product_version
           ORDER BY r.ordinal NULLS LAST, run.product_version""",
        (),
    ),
    # release compare: same test/process peak+sustained across releases
    "compare": (
        """SELECT tr.test_name, ps.process_name, r.label,
                  round(avg(ps.cpu_max_pct),1)     AS avg_cpu_peak,
                  round(avg(ps.cpu_sustained_s),1) AS avg_sustained_s,
                  round(avg(ps.mem_growth_mb),1)   AS avg_mem_growth
           FROM process_summary ps
           JOIN test_result tr ON tr.id = ps.test_result_id
           JOIN test_run run   ON run.id = tr.run_id
           JOIN release r      ON r.id = run.release_id
           WHERE (%(test)s IS NULL OR tr.test_name = %(test)s)
           GROUP BY tr.test_name, ps.process_name, r.label, r.ordinal
           ORDER BY tr.test_name, ps.process_name, r.ordinal""",
        ("test",),
    ),
    # cases with NO PASS as the latest attempt in ANY lane -- the real
    # "still broken" list for a wrap-up, as opposed to "latest per lane is
    # FAIL" (which double-counts a case that already passed on a different
    # lane). --release scopes it; --test is ignored (this is a release-wide
    # sweep, not a single-case lookup).
    "no-pass": (
        """WITH ranked AS (
             SELECT tr.test_name, run.jenkins_job, tr.verdict, run.jenkins_build,
                    run.started_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY tr.test_name, run.jenkins_job
                        ORDER BY run.started_at DESC
                    ) AS rn
             FROM test_result tr
             JOIN test_run run ON run.id = tr.run_id
             LEFT JOIN release r ON r.id = run.release_id
             WHERE (%(release)s IS NULL OR r.label = %(release)s)
               AND run.run_id NOT LIKE '%%-int'
           ),
           latest AS (SELECT * FROM ranked WHERE rn = 1)
           SELECT test_name,
                  string_agg(
                      regexp_replace(jenkins_job, '^.*GRS-SYSTEMTEST-', '') || ':'
                      || verdict || '#' || jenkins_build,
                      ', ' ORDER BY jenkins_job
                  ) AS latest_per_lane
           FROM latest
           GROUP BY test_name
           HAVING NOT bool_or(verdict = 'PASS')
           ORDER BY test_name""",
        ("release",),
    ),
}


def cmd_query(a):
    if not a.name or a.name not in _QUERIES:
        print("available queries:")
        for k in _QUERIES:
            print(f"  {k}")
        print("\nusage: db.py query <name> [--test NAME] [--release LABEL]")
        return 0 if a.name is None else 2
    sql, params = _QUERIES[a.name]
    # --test matches a case across the _win rename: 'test_x' and 'test_x_win' are one test (testname.py)
    sql = sql.replace(
        "tr.test_name = %(test)s",
        f"{testname.canon_sql('tr.test_name')} = {testname.canon_sql('%(test)s')}",
    )
    args = {"test": a.test, "release": a.release}
    conn = dbconn.connect()
    with conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    conn.close()
    print("NOTE: feature builds (major>=200) excluded by design.\n")
    _print_table(cols, rows)
    print(f"\n{len(rows)} row(s)")
    return 0


def cmd_query_history(a):
    """Pass/fail history for ONE test case across every SystemTest lane, with
    the tenant/dc/build/version each ran on -- the ground truth for "did this
    case ever pass, and where/on what dc" without hand SQL.

    Built for agent consumption: --json emits one object per lane with a
    build_url ready to hand straight to `grs_jenkins.py console/srlog` for
    the actual log. Reuses query.py's LANES (the same job-alias grouping the
    live /query dashboard uses) as the single source of truth -- no
    hand-copied job list to drift out of sync (see feedback_single_source_
    job_alias.md: a hand-copied alias table is exactly the class of bug that
    broke localtest/LOCAL1 for 2 days).
    """
    import json as json_mod

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import query as query_mod  # noqa: E402  (local import: only this command needs it)

    conn = dbconn.connect()
    cur = conn.cursor()
    all_tests = query_mod.list_test_names(cur)
    hit = _one_case(all_tests, a.test)
    if len(hit) != 1:
        verb = "matches nothing" if not hit else f"is ambiguous ({len(hit)} matches)"
        print(
            f"[fail] --test {a.test!r} {verb} among known test_result.test_name values:"
        )
        for n in hit or all_tests:
            print(f"         {n}")
        conn.close()
        return 2
    test_name = hit[0]

    verdicts = ["PASS", "FAIL"] if a.verdict == "any" else [a.verdict.upper()]
    result = {
        "test": test_name,
        "verdict_filter": a.verdict,
        "top_n": a.top,
        "lanes": [],
    }
    for lane, jobs in query_mod.LANES:
        jobs = [j for j in jobs if j]
        if not jobs:
            continue
        cur.execute(
            """
            SELECT r.jenkins_build, tr.verdict, r.tenant, r.dc, r.build_url,
                   r.started_at, tr.duration_s, r.is_64_bit_installed, r.is_64_bit_cli,
                   r.product_version
            FROM test_result tr JOIN test_run r ON r.id = tr.run_id
            WHERE """
            + testname.canon_sql("tr.test_name")
            + """ = %s AND tr.verdict = ANY(%s) AND r.jenkins_job = ANY(%s)
                  AND r.run_id NOT LIKE '%%-int'
            ORDER BY r.started_at DESC NULLS LAST
            LIMIT %s
            """,
            (testname.canonical_name(test_name), verdicts, jobs, a.top),
        )
        entries = []
        for (
            build,
            verdict,
            tenant,
            dc,
            build_url,
            started,
            dur,
            inst64,
            cli64,
            version,
        ) in cur.fetchall():
            entries.append(
                {
                    "build": build,
                    "verdict": verdict,
                    "tenant": tenant,
                    "dc": dc,
                    "build_url": build_url,
                    "started_at": started.isoformat() if started else None,
                    "duration_s": float(dur) if dur is not None else None,
                    "bitness": query_mod._fmt_bitness(inst64, cli64),
                    "product_version": version,
                }
            )
        result["lanes"].append({"lane": lane, "entries": entries})
    conn.close()

    if a.json:
        print(json_mod.dumps(result, indent=2))
        return 0

    print(f"test: {test_name}   verdict_filter: {a.verdict}   top_n: {a.top}\n")
    for lane_block in result["lanes"]:
        lane, entries = lane_block["lane"], lane_block["entries"]
        if not entries:
            print(
                f"{lane}: no {a.verdict.upper() if a.verdict != 'any' else 'PASS/FAIL'} record"
            )
            continue
        for e in entries:
            print(
                f"{lane}: {e['verdict']} build={e['build']} tenant={e['tenant']} dc={e['dc']} "
                f"bitness={e['bitness']} version={e['product_version']} "
                f"started_at={e['started_at']} duration_s={e['duration_s']}"
            )
            if e["build_url"]:
                print(f"    {e['build_url']}")
    return 0


def _print_table(cols, rows):
    widths = [len(c) for c in cols]
    srows = []
    for row in rows:
        sr = ["" if v is None else str(v) for v in row]
        srows.append(sr)
        widths = [max(w, len(v)) for w, v in zip(widths, sr)]
    line = "  ".join(c.ljust(w) for c, w in zip(cols, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for sr in srows:
        print("  ".join(v.ljust(w) for v, w in zip(sr, widths)))


# ─────────────────────────── CLI ───────────────────────────


def main(argv):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("conf").set_defaults(func=cmd_conf)
    sub.add_parser("ping").set_defaults(func=cmd_ping)

    ap = sub.add_parser("apply-schema")
    ap.add_argument(
        "--drop", action="store_true", help="DROP the tables first (destructive)"
    )
    ap.set_defaults(func=cmd_apply_schema)

    mg = sub.add_parser(
        "migrate",
        help="apply additive column migrations to the live DB (idempotent, non-destructive)",
    )
    mg.set_defaults(func=cmd_migrate)

    ig = sub.add_parser("ingest")
    ig.add_argument("build_dir")
    ig.add_argument("--release")
    ig.add_argument("--replace", action="store_true")
    ig.add_argument("--dry-run", action="store_true")
    ig.add_argument(
        "--check-insert",
        action="store_true",
        help="execute the real INSERTs against the live schema, then ROLLBACK (schema-change check)",
    )
    ig.set_defaults(func=cmd_ingest)

    ins = sub.add_parser("inspect")
    ins.add_argument(
        "build_dir", help="a downloaded build dir (with iterations_*.ndjson)"
    )
    ins.set_defaults(func=cmd_inspect)

    ft = sub.add_parser("fetch")
    ft.add_argument(
        "job", help="Jenkins job alias (reg/reg2/localtest/...) or full path"
    )
    ft.add_argument("build", help="build number")
    ft.add_argument("--dest", help="download dir (default: TEMP/grsdb_<job>_<build>)")
    ft.add_argument("--release")
    ft.add_argument("--replace", action="store_true")
    ft.set_defaults(func=cmd_fetch)

    ca = sub.add_parser("candidates")
    ca.add_argument(
        "jobs", nargs="+", help="job aliases (reg reg2 localtest) or full paths"
    )
    ca.set_defaults(func=cmd_candidates)

    gp = sub.add_parser(
        "gaps",
        help="per-build 3-way audit (Jenkins/store/DB): finds holes in the MIDDLE, "
        "which lanes/verify/staleness all miss until retention deletes them",
    )
    gp.add_argument(
        "jobs",
        nargs="*",
        help="job aliases (default: every lane the daily harvest covers)",
    )
    gp.add_argument(
        "--build",
        type=int,
        help="classify ONE build number in every lane instead of the full audit "
        "(answers 'why is build N missing?')",
    )
    gp.add_argument(
        "--check",
        action="store_true",
        help="exit 2 when anything needs an operator (rescuable, store-only, number "
        "collision, SUCCESS build without data) -- for the scheduled poller",
    )
    gp.set_defaults(func=cmd_gaps)

    ig = sub.add_parser(
        "ingesters",
        help="poller heartbeats: which ingesters are alive, which code version each "
        "runs; exit 2 on silence / version skew / lost redundancy",
    )
    ig.set_defaults(func=lambda a: __import__("heartbeat").main())

    ln = sub.add_parser(
        "lanes",
        help="per-lane cold-store vs Jenkins: IDLE or BEHIND (answers db_health's VERIFY)",
    )
    ln.add_argument(
        "jobs",
        nargs="*",
        help="job aliases (default: every lane the daily harvest covers)",
    )
    ln.set_defaults(func=cmd_lanes)

    ia = sub.add_parser("import-all")
    ia.add_argument(
        "jobs", nargs="+", help="job aliases (reg reg2 localtest) or full paths"
    )
    ia.add_argument("--release", help="release label to tag every ingested run")
    ia.add_argument(
        "--limit", type=int, help="only the newest N (CPU-valid) builds per job"
    )
    ia.add_argument(
        "--replace", action="store_true", help="re-ingest even if run_id exists"
    )
    ia.add_argument(
        "--all",
        action="store_true",
        help="import ALL finished builds, incl. pre-PR263 (old CPU unit)",
    )
    ia.set_defaults(func=cmd_import_all)

    fr = sub.add_parser(
        "fix-run", help="correct a single run's product_version and/or release label"
    )
    fr.add_argument(
        "job", help="Jenkins job alias (reg/reg2/localtest/...) or full path"
    )
    fr.add_argument("build", help="build number")
    fr.add_argument("--version", help="new product_version, e.g. 140.0.0.2750")
    fr.add_argument(
        "--release", help="new release label, e.g. release-140 (must already exist)"
    )
    fr.set_defaults(func=cmd_fix_run)

    rc = sub.add_parser(
        "reclaim-run",
        help="detach a stale run whose build number Jenkins has since reused",
    )
    rc.add_argument(
        "job", help="Jenkins job alias (reg/reg2/localtest/...) or full path"
    )
    rc.add_argument("build", help="build number")
    rc.set_defaults(func=cmd_reclaim_run)

    cp = sub.add_parser(
        "check-partial",
        help="find test_run rows with 0 test_result (partial write); --fix re-ingests them",
    )
    cp.add_argument(
        "--fix", action="store_true", help="re-fetch + re-ingest each partial run"
    )
    cp.add_argument("--release", help="release label to pass to re-ingest (optional)")
    cp.set_defaults(func=cmd_check_partial)

    pf = sub.add_parser(
        "purge-feature",
        help="list (--fix deletes) feature-build runs (version major>=200) that slipped into the DB",
    )
    pf.add_argument(
        "--fix",
        action="store_true",
        help="DELETE the feature-build runs (cascades to child rows)",
    )
    pf.set_defaults(func=cmd_purge_feature)

    pcn = sub.add_parser(
        "canon-names",
        help="list (--fix renames) legacy unsuffixed Windows test names of one release to test_x_win",
    )
    pcn.add_argument("--release", required=True, help="release label, e.g. release-144")
    pcn.add_argument("--fix", action="store_true", help="UPDATE the rows (default is a dry run)")
    pcn.set_defaults(func=cmd_canon_names)

    ptn = sub.add_parser(
        "purge-test-name",
        help="list (--fix deletes) every test_result row for one exact test_name (e.g. a removed TEMP/POC case)",
    )
    ptn.add_argument("test_name", help="exact test_result.test_name value, e.g. test_steer_06_poc_dns_push_query_3x")
    ptn.add_argument(
        "--fix",
        action="store_true",
        help="DELETE: whole test_run for pure runs, else just the matching test_result row (rollup decremented)",
    )
    ptn.set_defaults(func=cmd_purge_test_name)

    prt = sub.add_parser(
        "rename-test",
        help="list (--fix renames) every test_result row of one test_name to the official name, so results aggregate",
    )
    prt.add_argument("old", help="exact current test_name, e.g. a POC name")
    prt.add_argument("new", help="official test_name that already has rows in the DB")
    prt.add_argument("--allow-new", action="store_true", help="accept a NEW name that has no row yet")
    prt.add_argument(
        "--tickets-only", action="store_true", help="rename only the ticket links (gate_ticket) named OLD, no test_result row"
    )
    prt.add_argument("--fix", action="store_true", help="UPDATE the rows (default is a dry run)")
    prt.set_defaults(func=cmd_rename_test)

    pa = sub.add_parser(
        "purge-allskip",
        help="list (--fix deletes) runs where every case was SKIP (measured nothing)",
    )
    pa.add_argument(
        "--fix", action="store_true", help="DELETE the all-skip runs (cascades)"
    )
    pa.add_argument("--version", help="product_version major to limit to, e.g. 142")
    pa.set_defaults(func=cmd_purge_allskip)

    cr = sub.add_parser(
        "clone-release",
        help="copy a release into a new label, filtering non-PASS rows via a keep-list JSON",
    )
    cr.add_argument(
        "--source", required=True, help="existing release label, e.g. release-141"
    )
    cr.add_argument(
        "--target", required=True, help="new release label, e.g. release-141-int"
    )
    cr.add_argument(
        "--keep-file",
        required=True,
        help="JSON with a 'keep' list of {job,build,test_name} — see keep_lists/*.json",
    )
    cr.add_argument(
        "--dry-run", action="store_true", help="print counts, write nothing"
    )
    cr.set_defaults(func=cmd_clone_release)

    dr = sub.add_parser(
        "drop-release",
        help="delete a release's test_run (+ cascaded) rows so clone-release can regen it",
    )
    dr.add_argument(
        "--label", required=True, help="release label to delete, e.g. release-142-final"
    )
    dr.add_argument(
        "--yes",
        action="store_true",
        help="actually delete (required; refuses without it)",
    )
    dr.set_defaults(func=cmd_drop_release)

    dgv = sub.add_parser(
        "drop-gate-violation",
        help="delete one Health-gate breakdown row by gate_violation.id (e.g. its "
        "dump evidence is confirmed gone from Jenkins retention)",
    )
    dgv.add_argument("id", type=int, help="gate_violation.id (see `db.py issue list`)")
    dgv.add_argument(
        "--yes",
        action="store_true",
        help="actually delete (required; refuses without it)",
    )
    dgv.set_defaults(func=cmd_drop_gate_violation)

    rr = sub.add_parser(
        "rename-release",
        help="rename a release label in place (no data moves; e.g. fixing a snapshot's name)",
    )
    rr.add_argument("--from", dest="frm", required=True, help="existing release label")
    rr.add_argument(
        "--to", required=True, help="new release label (must not already exist)"
    )
    rr.set_defaults(func=cmd_rename_release)

    fn = sub.add_parser(
        "final",
        add_help=False,
        help="build the publish snapshot release-<N>-final for any release: classify, dry run, --apply [--redo]",
    )
    fn.add_argument("rest", nargs=argparse.REMAINDER)
    fn.set_defaults(
        func=lambda a: __import__("final_snapshot").main(a.rest, sys.modules[__name__])
    )

    bk = sub.add_parser(
        "backup",
        help="dump every table to gzipped CSV + manifest.json (no pg_dump needed)",
    )
    bk.add_argument(
        "--out", help="output directory (default ~/grs_tools/db_backups/<timestamp>)"
    )
    bk.set_defaults(func=cmd_backup)

    rs = sub.add_parser("restore", help="load a `backup` directory back into the DB")
    rs.add_argument("dir", help="backup directory (must contain manifest.json)")
    rs.add_argument(
        "--truncate",
        action="store_true",
        help="TRUNCATE all tables first (destructive); without it, restore refuses a non-empty DB",
    )
    rs.set_defaults(func=cmd_restore)

    rt = sub.add_parser(
        "runtime",
        help="how long a test case takes: per release, iter1 + per-iteration avg/median",
    )
    rt.add_argument(
        "--test",
        help="test case, fuzzy: 'stress03' / 'STRESS-03' / full pytest name "
        "(a miss lists the available names instead of printing nothing)",
    )
    rt.add_argument(
        "--release",
        help="release filter, bare majors accepted: 140 | 141 | 140,141 | release-141",
    )
    rt.add_argument(
        "--job", help="substring match on the job (reg / reg2 / localtest / local2)"
    )
    rt.add_argument(
        "--per-iter",
        action="store_true",
        help="per-iteration-index table (automatic when --test matches one case)",
    )
    rt.add_argument(
        "--status",
        choices=["pass", "fail", "all"],
        default="pass",
        help="which iterations to measure (default pass: a FAIL body is truncated "
        "or timed out, so it is not a sample of how long the case takes)",
    )
    # nargs="?" so both `--store` (default dir) and `--store D` work; default=False
    # distinguishes "flag absent" (use the DB) from "flag given with no value".
    rt.add_argument(
        "--store",
        nargs="?",
        const=True,
        default=False,
        help="read the harvest cold store instead of the DB (works while the DB is down)",
    )
    rt.add_argument(
        "--overhead",
        action="store_true",
        help="also show Jenkins wall-clock minus body time (install + fixtures + collect)",
    )
    rt.set_defaults(func=cmd_runtime)

    to = sub.add_parser(
        "timeout",
        help="data-driven timeout for a case x iterations: install overhead + "
        "iter1 + (N-1) x steady median, x headroom. Never hand-guess again "
        "(REG 315 died at iter 18/20 on the default 1h).",
    )
    to.add_argument("--test", help="fuzzy case filter (same rules as runtime)")
    to.add_argument(
        "--iterations", type=int, default=20, help="planned --iterations (default 20)"
    )
    to.add_argument("--release", help="release filter, bare majors accepted")
    to.add_argument("--job", help="substring match on the job")
    to.add_argument(
        "--status",
        choices=["pass", "fail", "all"],
        default="pass",
        help="measure PASS iterations only by default (a FAIL body is not a duration sample)",
    )
    to.add_argument(
        "--store",
        nargs="?",
        const=True,
        default=False,
        help="read the harvest cold store instead of the DB",
    )
    to.add_argument(
        "--install-min",
        type=float,
        default=8.0,
        help="install+fixture overhead minutes (default 8)",
    )
    to.add_argument(
        "--headroom",
        type=float,
        default=1.5,
        help="safety multiplier (default 1.5; thin history <5 samples gets x2 automatically)",
    )
    to.set_defaults(func=cmd_timeout)

    bb = sub.add_parser(
        "backfill-bitness",
        help="backfill test_run.is_64_bit_cli from each build's own Jenkins params "
        "(one-time, for rows ingested before the 2026-09-13 ndjson recorder fix)",
    )
    bb.add_argument(
        "--dry-run", action="store_true", help="print what would change, write nothing"
    )
    bb.set_defaults(func=cmd_backfill_bitness)

    fmb = sub.add_parser(
        "fix-mac-bitness",
        help="set is_64_bit_installed=TRUE for every mac run (mac is 64-bit only, not a measurement)",
    )
    fmb.add_argument(
        "--dry-run", action="store_true", help="print what would change, write nothing"
    )
    fmb.set_defaults(func=cmd_fix_mac_bitness)

    iss = sub.add_parser(
        "issue",
        help="attach a ticket id to a Health-gate breakdown row (report ticket column)",
    )
    iss.add_argument("action", choices=["list", "set", "rm", "ext", "info"])
    iss.add_argument("--category", help="info: ticket category shown in the report (crash / functional / resource leak / latency)")
    iss.add_argument("--summary", help="info: the ticket title shown in the report")
    iss.add_argument("ticket", nargs="?", help="ticket id, e.g. ENG-1180143 (set/rm)")
    iss.add_argument("--build", type=int, help="Jenkins build number")
    iss.add_argument(
        "--job",
        help="lane substring (reg/reg2/localtest/local2) — required when the same "
        "build number exists in more than one lane",
    )
    iss.add_argument(
        "--test",
        help="one case in that build, fuzzy ('stress02'); omit = every gate row of the build",
    )
    iss.add_argument("--note", help="free-text note stored with the link")
    iss.add_argument(
        "--release",
        help="list: release filter (140 / release-141). set/rm: disambiguate "
        "--build when the same (job, build) matches >1 release row (e.g. a "
        "clone-release '-final' snapshot) and --job alone can't tell them apart.",
    )
    iss.add_argument(
        "--version",
        help="set: product version this ticket happened on (e.g. 142.0.0.7276). "
        "Auto-filled from the build's test_run when one exists; REQUIRED to "
        "place the ticket on a release's report page when the build has no "
        "test_run at all (died before pytest ran).",
    )
    iss.add_argument(
        "--bitness",
        choices=["32", "64"],
        help="set: 32 or 64 -- same auto-fill/override rule as --version",
    )
    iss.add_argument(
        "--report-release",
        help="set: bare major version (e.g. 143) to force this ticket onto THAT "
        "release's report page regardless of which release the build's own "
        "test_run/product_version actually belongs to -- for a ticket discovered/"
        "triaged in a later period than the run it happened on. Omit for the "
        "normal (run-release / product_version-prefix) behavior.",
    )
    iss.set_defaults(func=cmd_issue)

    gr = sub.add_parser("growth")
    gr.add_argument(
        "--record", action="store_true", help="write a db_growth snapshot row"
    )
    gr.set_defaults(func=cmd_growth)

    st = sub.add_parser(
        "status",
        help="one-shot: DB ping + row counts + harvest health (replaces ping+growth+db_health compound)",
    )
    st.set_defaults(func=cmd_status)

    q = sub.add_parser("query")
    q.add_argument("name", nargs="?", help="query name (omit to list)")
    q.add_argument("--test", help="filter by test_name")
    q.add_argument("--release", help="filter by release label")
    q.set_defaults(func=cmd_query)

    qh = sub.add_parser(
        "query-history",
        help="pass/fail history for one test case across every lane, with tenant/dc/build_url (--json for agents)",
    )
    qh.add_argument(
        "test", help="fuzzy case filter, e.g. 'stress27' / full pytest name"
    )
    qh.add_argument(
        "--verdict",
        choices=["pass", "fail", "any"],
        default="pass",
        help="which verdict to show (default pass)",
    )
    qh.add_argument(
        "--top",
        type=int,
        default=2,
        help="how many recent records per lane (default 2)",
    )
    qh.add_argument("--json", action="store_true", help="machine-readable output")
    qh.set_defaults(func=cmd_query_history)

    rp = sub.add_parser("report")
    rp.add_argument(
        "--release", default="release-140", help="release label (default release-140)"
    )
    rp.add_argument(
        "--out",
        help="output HTML path (default claude-resource/systest_report/systest_report_<release>_<timestamp>.html)",
    )
    rp.add_argument(
        "--top", type=int, default=10, help="cap for large tables (default 10)"
    )
    rp.set_defaults(func=cmd_report)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
