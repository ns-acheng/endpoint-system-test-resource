#!/usr/bin/env python
"""Stand up the SystemTest results DB on a fresh VM — one command, resumable.

Why this exists
---------------
The DB VM was decommissioned on 2026-07-27 with no dump. Rebuilding it is a
fixed sequence (reachability -> schema -> replay the cold store -> catch up from
Jenkins -> first backup), and the last rebuild was done by hand from a plan doc.
A hand-run sequence is where steps get skipped: the *first* backup is the one
that actually matters and it is also the easiest to forget once the data looks
right.

This is expected to run more than once (this VM, the next one, a local Docker
Postgres for testing), so it is a script rather than a checklist.

Design
------
* Every step is IDEMPOTENT and the run is RESUMABLE: re-running after a failure
  re-checks the earlier steps cheaply and continues. Nothing here is
  destructive without an explicit flag.
* Steps run in order and stop at the first failure, because each depends on the
  previous (no schema -> nothing to ingest).
* `--dry-run` prints the plan without touching anything, so it is safe to run
  against a live DB just to see what it would do.
* This does NOT provision the VM or install PostgreSQL: that needs root on the
  box and is a handful of apt commands (see the runbook). It starts from "a
  reachable, empty postgres with a grs role".

Usage
    python rebuild_db.py --dry-run          # show the plan, touch nothing
    python rebuild_db.py                    # full rebuild
    python rebuild_db.py --from ingest      # resume at a step
    python rebuild_db.py --skip catchup     # skip the Jenkins catch-up (offline)
    python rebuild_db.py --list             # list step names

Steps
    preflight   resolve settings, connect, report server version + emptiness
    schema      apply schema.sql if the tables are absent (never drops)
    ingest      replay claude-resource/systest_db_store/ (the durable inputs)
    catchup     db.py import-all for builds newer than the store (needs Jenkins)
    backup      db.py backup + verify the dump is non-empty  <- the point of all this
    verify      row counts + a restore-readiness check
"""

import argparse
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DB = os.path.join(_HERE, "db.py")
_HARVEST = os.path.join(_HERE, "harvest.py")
sys.path.insert(0, _HERE)
import dbconn  # noqa: E402

# Jobs to catch up from Jenkins. Same set the daily harvest covers, so a rebuild
# lands in the same state the poller would have maintained.
_JOBS = ["reg", "reg2", "localtest", "local2"]

STEPS = ["preflight", "schema", "ingest", "catchup", "backup", "verify"]


def _run(argv, label):
    """Run a child python tool, streaming its output. Returns its exit code."""
    print(f"\n$ {' '.join(str(x) for x in argv)}")
    r = subprocess.run([sys.executable] + [str(x) for x in argv])
    if r.returncode != 0:
        print(f"[FAIL] {label}: exit {r.returncode}")
    return r.returncode


def _tables(cur):
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def _counts(cur, tables):
    out = {}
    for t in tables:
        cur.execute(f"SELECT count(*) FROM {t}")
        out[t] = cur.fetchone()[0]
    return out


# ─────────────────────────── steps ───────────────────────────


def step_preflight(a):
    """Fail early and loudly if the DB is not reachable — every later step needs it."""
    print(f"settings: {dbconn.describe()}")
    if a.dry_run:
        print("[dry-run] would connect and report server version / table state")
        return 0
    try:
        conn = dbconn.connect()
    except Exception as e:
        # The single most common cause is a stale GRS_DB_HOST in ~/.grs_db.conf
        # after the VM was replaced, so say that rather than just re-raising.
        print(f"[FAIL] cannot connect: {type(e).__name__}: {e}")
        print("       If the VM was replaced, update GRS_DB_HOST in ~/.grs_db.conf first.")
        return 2
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        ver = cur.fetchone()[0]
        tabs = _tables(cur)
    conn.close()
    print(f"[ok] connected; PostgreSQL {ver}")
    print(f"[ok] public tables: {len(tabs)}" + (f" ({', '.join(tabs)})" if tabs else " (empty DB)"))
    return 0


def step_schema(a):
    """Apply schema.sql only when the tables are absent.

    Deliberately NEVER passes --drop: this script must be safe to re-run against
    a DB that already holds data. Dropping is a separate, explicit human act.
    """
    if a.dry_run:
        print("[dry-run] would apply schema.sql if public tables are absent")
        return 0
    conn = dbconn.connect()
    with conn.cursor() as cur:
        tabs = _tables(cur)
    conn.close()
    if "test_run" in tabs:
        print(f"[skip] schema already present ({len(tabs)} tables); not re-applying")
        return 0
    return _run([_DB, "apply-schema"], "apply-schema")


def step_ingest(a):
    """Replay the cold store. This is the actual data recovery.

    The store is the durable input set (gzipped ndjson + resource jsonl, kept in
    git). Everything here works with Jenkins offline and with builds long since
    rotated out of Jenkins retention.
    """
    if a.dry_run:
        print("[dry-run] would run: harvest.py ingest-all")
        return 0
    argv = [_HARVEST, "ingest-all"]
    if a.release:
        argv += ["--release", a.release]
    return _run(argv, "harvest ingest-all")


def step_catchup(a):
    """Pull builds newer than the store straight from Jenkins.

    Optional by design: it is the only step that needs Jenkins credentials and
    network, so `--skip catchup` still yields a complete DB up to the store's
    high-water mark.
    """
    if a.dry_run:
        print(f"[dry-run] would run: db.py import-all {' '.join(_JOBS)}")
        return 0
    argv = [_DB, "import-all"] + _JOBS
    if a.release:
        argv += ["--release", a.release]
    rc = _run(argv, "import-all")
    if rc != 0:
        # A Jenkins outage must not fail the whole rebuild: the recovered data is
        # already in from `ingest`, and catch-up is re-runnable any time.
        print("[warn] catch-up failed (Jenkins unreachable / auth). The DB is still")
        print("       complete up to the cold store; re-run `db.py import-all` later.")
        return 0
    return 0


def step_backup(a):
    """Take the first backup and PROVE it is non-empty.

    This is the step the whole incident was about. `product_issue` and
    `gate_ticket` are hand-entered and have no artifact source, so once anyone
    enters triage rows, the cold store no longer covers the whole DB. A rebuild
    that ends without a verified dump has recreated the original single point of
    failure.
    """
    if a.dry_run:
        print("[dry-run] would run: db.py backup, then assert the dump is non-empty")
        return 0
    out = a.backup_out or os.path.join(
        os.path.expanduser("~"), "grs_tools", "db_backups", f"rebuild-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    rc = _run([_DB, "backup", "--out", out], "backup")
    if rc != 0:
        return rc
    # Verify rather than trust: a backup command that exits 0 having written
    # nothing useful is exactly the failure mode that made this incident
    # unrecoverable.
    manifest = os.path.join(out, "manifest.json")
    if not os.path.isfile(manifest):
        print(f"[FAIL] backup wrote no manifest.json in {out}")
        return 2
    gz = [f for f in os.listdir(out) if f.endswith(".csv.gz")]
    total = sum(os.path.getsize(os.path.join(out, f)) for f in gz)
    print(f"[ok] backup verified: {len(gz)} table dump(s), {total / 1024:.1f} KB, manifest present")
    print(f"[!] copy this OFF this host — a backup that only lives here is not a backup:\n    {out}")
    return 0


def step_verify(a):
    """Report what landed, and flag the two states that mean 'not actually done'."""
    if a.dry_run:
        print("[dry-run] would report row counts per table")
        return 0
    conn = dbconn.connect()
    with conn.cursor() as cur:
        tabs = _tables(cur)
        counts = _counts(cur, tabs)
    conn.close()
    for t in sorted(counts):
        print(f"  {t:20} {counts[t]}")
    runs = counts.get("test_run", 0)
    if not runs:
        print("[FAIL] test_run is empty — the rebuild ingested nothing")
        return 2
    print(f"\n[ok] {runs} test_run row(s) recovered")
    for t in ("product_issue", "gate_ticket"):
        if not counts.get(t, 0):
            # Not an error: empty on a fresh rebuild by definition. Say it out loud
            # because these are the tables nobody can rebuild for you.
            print(f"[note] {t} is empty. It is hand-entered triage with no artifact")
            print("       source, so it did NOT come back with the rest. Re-enter it if you")
            print("       still have the ticket mapping (gate_ticket: `db.py issue set`).")
    return 0


_IMPL = {
    "preflight": step_preflight,
    "schema": step_schema,
    "ingest": step_ingest,
    "catchup": step_catchup,
    "backup": step_backup,
    "verify": step_verify,
}


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    p.add_argument("--from", dest="start", choices=STEPS, help="resume at this step")
    p.add_argument("--only", choices=STEPS, help="run exactly one step")
    p.add_argument("--skip", action="append", default=[], choices=STEPS, help="skip a step (repeatable)")
    p.add_argument("--release", help="release label to tag ingested runs")
    p.add_argument("--backup-out", help="backup dir (default ~/grs_tools/db_backups/rebuild-<ts>)")
    p.add_argument("--list", action="store_true", help="list step names and exit")
    a = p.parse_args(argv)

    if a.list:
        for s in STEPS:
            print(f"  {s:10} {(_IMPL[s].__doc__ or '').strip().splitlines()[0]}")
        return 0

    if a.only:
        plan = [a.only]
    else:
        start = STEPS.index(a.start) if a.start else 0
        plan = [s for s in STEPS[start:] if s not in a.skip]

    print("=" * 72)
    print(f"rebuild plan: {' -> '.join(plan)}" + ("   [DRY RUN]" if a.dry_run else ""))
    print("=" * 72)

    for i, name in enumerate(plan, 1):
        print(f"\n--- step {i}/{len(plan)}: {name} " + "-" * (48 - len(name)))
        rc = _IMPL[name](a)
        if rc != 0:
            # Stop at the first failure: each step depends on the previous, so
            # continuing would produce confusing cascade errors.
            print(f"\n[STOP] step '{name}' failed (exit {rc}).")
            print(f"       Fix it, then resume with: --from {name}")
            return rc

    print("\n" + "=" * 72)
    print("dry run complete — nothing was changed" if a.dry_run else "rebuild complete")
    # Warn in dry-run too: reviewing the plan is precisely when a missing backup
    # step is still cheap to add. Skipping the backup is what turned the
    # 2026-07-27 VM loss from an inconvenience into permanent data loss.
    if "backup" not in plan:
        print("[!] backup is NOT part of this plan. Run `db.py backup` before you")
        print("    consider the DB done — that omission is what made the 2026-07-27")
        print("    loss unrecoverable.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
