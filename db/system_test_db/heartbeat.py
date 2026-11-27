"""Ingester heartbeat: every poller records that it ran, with the code version.

WHY (2026-10-16): two pollers ingest into the one results DB (this Windows machine
and SYS-07 over WSL cron). Nothing compared them: SYS-07 kept a 9-06 clone while the
primary got fixes, and when REG builds 175-183 were skipped for days neither poller
said anything. A heartbeat row per (host) makes three things checkable from the DB
alone, with no access to either machine:

  - a poller went silent (redundancy is gone and nobody noticed),
  - the pollers run DIFFERENT code (a fix is live on one and not the other),
  - a poller's last poll failed.

The table is created with IF NOT EXISTS so a poller on a stale schema upgrades itself;
schema.sql carries the same DDL for rebuilds.
"""

import os
import socket
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import dbconn  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS ingester_heartbeat (
    host        TEXT PRIMARY KEY,
    code_sha    TEXT,
    last_poll   TIMESTAMPTZ NOT NULL,
    last_rc     INTEGER,
    summary     TEXT
)
"""

# The files that decide what gets ingested, relative to grs_tools. ONE list: code_sha hashes it and
# deploy_ingest_host.py ships it, so a host that has exactly these files compares equal to any other.
INGEST_FILES = [
    "system_test_db/db.py",
    "system_test_db/ingest.py",
    "system_test_db/dbconn.py",
    "system_test_db/testname.py",
    "system_test_db/epais_builds.py",
    "system_test_db/heartbeat.py",
    "systest_db_poll.py",
    "grs_jenkins.py",
]

# A poller runs every 5 / 11 minutes; silence for this long means it stopped.
SILENT_AFTER_S = 30 * 60
# A host unseen this long is retired, not "broken" (a decommissioned VM must not alarm forever).
RETIRED_AFTER_S = 14 * 86400


def code_sha():
    """Content hash of the ingest code this poller runs: system_test_db/*.py plus the
    poller and the Jenkins alias table.

    NOT the repo's git HEAD: claude-resource gets unrelated commits all day, so comparing
    HEADs would report 'version skew' constantly, and SYS-07 has no .git at all (it is
    deployed as a stripped copy). Hashing the files that decide what gets ingested means
    two hosts match exactly when they would ingest identically. Line endings are
    normalised: the Windows checkout is CRLF, the WSL copy LF."""
    import hashlib

    tools = os.path.dirname(_HERE)
    files = sorted(os.path.join(tools, *f.split("/")) for f in INGEST_FILES)
    h = hashlib.sha1()
    for p in files:
        try:
            with open(p, "rb") as f:
                data = f.read().replace(b"\r\n", b"\n")
        except OSError:
            continue
        h.update(os.path.basename(p).encode() + b"\0" + data + b"\0")
    return h.hexdigest()[:10]


def beat(rc, summary, host=None):
    """Upsert this poller's heartbeat. Never raises: a heartbeat must not fail a poll."""
    host = host or socket.gethostname()
    try:
        conn = dbconn.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(DDL)
                cur.execute(
                    "INSERT INTO ingester_heartbeat (host, code_sha, last_poll, last_rc, summary) "
                    "VALUES (%s, %s, now(), %s, %s) "
                    "ON CONFLICT (host) DO UPDATE SET code_sha=EXCLUDED.code_sha, "
                    "last_poll=EXCLUDED.last_poll, last_rc=EXCLUDED.last_rc, summary=EXCLUDED.summary",
                    (host, code_sha(), rc, (summary or "")[:500]),
                )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:  # noqa: BLE001 -- best effort by contract
        print(f"[heartbeat] not recorded: {e}", file=sys.stderr)
        return False


def rows():
    """[(host, code_sha, age_seconds, last_rc, summary)] newest first."""
    conn = dbconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            conn.commit()
            cur.execute(
                "SELECT host, code_sha, extract(epoch FROM now()-last_poll), last_rc, summary "
                "FROM ingester_heartbeat ORDER BY last_poll DESC"
            )
            return [(h, s, float(a), rc, sm) for h, s, a, rc, sm in cur.fetchall()]
    finally:
        conn.close()


def problems(heartbeats=None, now_ignored=None):
    """Human-readable problems; empty list means the ingesters look healthy."""
    hb = heartbeats if heartbeats is not None else rows()
    live = [r for r in hb if r[2] <= RETIRED_AFTER_S]
    out = []
    if not live:
        return ["no ingester heartbeat at all -- no poller has ever recorded one"]
    for host, sha, age, rc, summary in live:
        if age > SILENT_AFTER_S:
            out.append(
                f"ingester {host} silent for {age / 60:.0f} min (last rc={rc}, code {sha})"
            )
        elif rc not in (0, None):
            out.append(f"ingester {host} last poll failed rc={rc}: {summary}")
    fresh = [r for r in live if r[2] <= SILENT_AFTER_S]
    shas = {r[1] for r in fresh}
    if len(shas) > 1:
        out.append(
            "ingesters run DIFFERENT code: "
            + ", ".join(f"{r[0]}={r[1]}" for r in fresh)
        )
    if len(fresh) < 2 and len(live) >= 2:
        out.append("only one ingester is alive -- the redundancy is gone")
    return out


def main():
    hb = rows()
    for host, sha, age, rc, summary in hb:
        print(
            f"{host:28s} code={sha:12s} last_poll={age / 60:7.1f} min ago  rc={rc}  {summary}"
        )
    probs = problems(hb)
    print(
        "\n"
        + (
            "OK -- ingesters healthy"
            if not probs
            else "PROBLEMS:\n  " + "\n  ".join(probs)
        )
    )
    return 0 if not probs else 2


if __name__ == "__main__":
    sys.exit(main())
