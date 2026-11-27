#!/usr/bin/env python
"""repair_displaced_runs.py -- put the NEWER execution back on the plain run_id when an older one took it.

    python repair_displaced_runs.py --job REG            dry run: list the pairs, write nothing
    python repair_displaced_runs.py --job REG --apply    swap them, one transaction per pair

Why (2026-10-29): Jenkins reused build numbers after a counter reset. ingest.py [reclaim] renames the row that is already in
the DB when an incoming run has the same run_id but a different started_at. A stale August store slot replayed AFTER the
October run was ingested therefore pushed the October run out to `<run_id>-orig<yyyymmdd>` (jenkins_build NULL) and put the
August run on the plain run_id. Reports and tools that look a run up by job + build number then show August data.

A pair is repaired only when ALL hold: same job; O.run_id == P.run_id + '-orig' + yyyymmdd(O.started_at) (the name reclaim
gives); O.jenkins_build IS NULL; P.jenkins_build IS NOT NULL; O.started_at is more than 300 s AFTER P.started_at.
The swap: P becomes P.run_id + '-orig' + yyyymmdd(P.started_at) (a numeric suffix is added when that name exists),
jenkins_build NULL; O takes the plain run_id and P's jenkins_build / build_url (same job + number = same Jenkins URL).
test_result rows hang on test_run.id, so no result row moves. Nothing is deleted. Take `db.py backup` first.
"""

import argparse
import sys

import dbconn

TOL_S = 300

PAIRS_SQL = """
SELECT p.id, p.run_id, p.jenkins_build, p.started_at, p.product_version, p.build_url,
       o.id, o.run_id, o.started_at, o.product_version
FROM test_run p
JOIN test_run o
  ON o.jenkins_job = p.jenkins_job
 AND o.run_id = p.run_id || '-orig' || to_char(o.started_at, 'YYYYMMDD')
WHERE p.jenkins_build IS NOT NULL
  AND o.jenkins_build IS NULL
  AND p.jenkins_job ILIKE %s
  AND o.started_at > p.started_at + (%s || ' seconds')::interval
ORDER BY p.jenkins_build, p.id, o.started_at
"""


def free_name(cur, base):
    name, k = base, 1
    while True:
        cur.execute("SELECT 1 FROM test_run WHERE run_id = %s", (name,))
        if cur.fetchone() is None:
            return name
        k += 1
        name = f"{base}-{k}"


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", required=True, help="substring of jenkins_job, e.g. GRS-SYSTEMTEST-REG (not REG2: use the full name)")
    ap.add_argument("--apply", action="store_true", help="write; without it nothing is changed")
    a = ap.parse_args(argv)
    conn = dbconn.connect()
    cur = conn.cursor()
    cur.execute(PAIRS_SQL, (f"%{a.job}", TOL_S))
    rows = cur.fetchall()
    # one October row per plain row: if several qualify, the newest wins and the others stay renamed as they are
    best = {}
    for r in rows:
        if r[0] not in best or r[8] > best[r[0]][8]:
            best[r[0]] = r
    pairs = list(best.values())
    print(f"{len(pairs)} pair(s) for jenkins_job ILIKE '%{a.job}' ({'APPLY' if a.apply else 'dry run'})")
    for p_id, p_rid, build, p_start, p_ver, _url, o_id, o_rid, o_start, o_ver in pairs:
        print(f"  build {build}: plain id {p_id} {p_ver} started {p_start:%Y-%m-%d %H:%M}  <->  {o_rid} id {o_id} {o_ver} started {o_start:%Y-%m-%d %H:%M}")
    if not a.apply:
        return 0
    done = 0
    for p_id, p_rid, build, p_start, p_ver, url, o_id, o_rid, o_start, o_ver in pairs:
        try:
            cur.execute("SELECT to_char(started_at, 'YYYYMMDD') FROM test_run WHERE id = %s", (p_id,))
            day = cur.fetchone()[0]
            new_p = free_name(cur, f"{p_rid}-orig{day}")
            cur.execute("UPDATE test_run SET run_id = %s, jenkins_build = NULL, build_url = NULL WHERE id = %s", (new_p, p_id))
            cur.execute("UPDATE test_run SET run_id = %s, jenkins_build = %s, build_url = %s WHERE id = %s", (p_rid, build, url, o_id))
            conn.commit()
            done += 1
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            print(f"  build {build}: FAILED and rolled back: {type(e).__name__}: {e}")
    print(f"swapped {done}/{len(pairs)}")
    return 0 if done == len(pairs) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
