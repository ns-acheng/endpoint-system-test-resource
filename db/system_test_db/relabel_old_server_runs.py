#!/usr/bin/env python
"""relabel_old_server_runs.py -- give the runs of the OLD Jenkins server their own run_id so build numbers never collide.

    python relabel_old_server_runs.py --out C:/tmp/relabel_plan.csv            dry run: write the plan, change nothing
    python relabel_old_server_runs.py --apply                                   apply the plan (parks first, then renames)

Why (2026-10-29): SystemTest lanes were copied from Jenkins 10.136.208.148 (old) to 10.136.208.217 (new) on 2026-09-08. The new jobs
have the same names but their build counters started at 1, so `jenkins-<job>-<N>` named two different runs.

Which server did a run come from? (a date cutoff is wrong: MAC2 new #1/#3/#4 started 2026-09-07 19:36-23:00, the old #62 at 23:00)
  1. a job name that exists on one server only decides (LOCALTEST, REG-02, MAC1 = old; REG2, MAC2-SSH = new);
  2. otherwise the run's (build number, started_at) is matched, within 300 s, against the build lists of BOTH servers
     (api allBuilds): a match on exactly one server decides;
  3. no match (the controller keeps only the newest ~100 builds): started before WINDOW_START = old, at or after WINDOW_END = new;
  4. inside the overlap window the number sequence decides: each server numbers upward in time, so a run belongs to the server
     whose confirmed runs before and after it bracket its number (LOCAL2 #152-158 on 09-07 continue the old counter that ends at
     159; #22-27 on 09-08 continue the new one). Fits both or neither -> `review`: listed in the plan, never changed.

Naming: new-server run -> plain `jenkins-<job>-<N>`, jenkins_build = N; old-server run -> `jenkins-<job>-o<N>`, jenkins_build NULL.
Several rows of ONE execution (re-ingests: `-orig<date>`, `-int`, ...) are told apart by started_at (within 300 s = same
execution); the earliest ingested row keeps the clean name, the others get `-dup<k>`. Nothing is deleted and no test_result row
moves (they hang on test_run.id). Take `db.py backup` before --apply.
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import dbconn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grs_jenkins as gj  # noqa: E402

TZ8 = timezone(timedelta(hours=8))
WINDOW_START = datetime(2026, 9, 6, tzinfo=TZ8)  # before: certainly old (git44: new jobs created 2026-09-08)
WINDOW_END = datetime(2026, 9, 9, tzinfo=TZ8)  # from here: certainly new (old server has no build after 2026-09-07)
SAME_RUN_S = 300
ONLY_OLD = {"DEV/GRS-SYSTEMTEST-LOCALTEST", "DEV/GRS-SYSTEMTEST-REG-02", "DEV/GRS-SYSTEMTEST-MAC1"}
ONLY_NEW = {"DEV/GRS-SYSTEMTEST-REG2", "DEV/GRS-SYSTEMTEST-MAC2-SSH"}
REST_RE = re.compile(r"^(?P<n>\d+)(?P<tail>(?:-orig\d{8})?(?:-int)?(?:-\d+)?)$")


def build_lists(job):
    """{'old': {number: epoch_seconds}, 'new': {...}} for a jenkins_job like DEV/GRS-SYSTEMTEST-REG (empty when a server lacks it)."""
    path = "job/" + job.replace("/", "/job/")
    out = {}
    for era, base in (("old", gj._OLD_BASE), ("new", gj._NEW_BASE)):
        try:
            d = gj._get(f"{path}/api/json?tree=allBuilds[number,timestamp]", server=base)
            out[era] = {b["number"]: b["timestamp"] / 1000.0 for b in d.get("allBuilds", [])}
        except Exception:  # noqa: BLE001 - job missing on that server
            out[era] = {}
    return out


def era_of(job, n, started, lists):
    if job in ONLY_OLD:
        return "old", "job name exists on the old server only"
    if job in ONLY_NEW:
        return "new", "job name exists on the new server only"
    ts = started.timestamp()
    m_old = n in lists["old"] and abs(lists["old"][n] - ts) <= SAME_RUN_S
    m_new = n in lists["new"] and abs(lists["new"][n] - ts) <= SAME_RUN_S
    if m_old and not m_new:
        return "old", "matches the old server's build list"
    if m_new and not m_old:
        return "new", "matches the new server's build list"
    if m_old and m_new:
        return "review", "matches both servers"
    if started < WINDOW_START:
        return "old", "no list match, started before the overlap window"
    if started >= WINDOW_END:
        return "new", "no list match, started after the overlap window"
    return "review", "no list match and inside the overlap window"


def plan(cur, job_like):
    cur.execute(
        "SELECT id, run_id, jenkins_job, jenkins_build, started_at, product_version, tenant, dc, ingested_at, build_url "
        "FROM test_run WHERE jenkins_job ILIKE %s ORDER BY jenkins_job, started_at, ingested_at, id",
        (job_like,),
    )
    rows = cur.fetchall()
    lists_by_job = {}
    groups = defaultdict(list)  # (job, n, prefix, era) -> [rows]
    out, skipped = [], []
    parsed = []  # (row, n, prefix) of every recognised row
    for r in rows:
        # the job name comes from the row itself: REG-02 ends in digits that are NOT the build number
        prefix = "jenkins-" + r[2].replace("/", "-") + "-"
        m = REST_RE.match(r[1][len(prefix):]) if r[1].startswith(prefix) else None
        if not m:
            skipped.append(r)
            continue
        parsed.append((r, int(m.group("n")), prefix))
    verdict = {}  # row id -> (era, why)
    for r, n, prefix in parsed:
        if r[2] not in lists_by_job:
            lists_by_job[r[2]] = build_lists(r[2]) if r[2] not in ONLY_OLD | ONLY_NEW else {"old": {}, "new": {}}
        verdict[r[0]] = era_of(r[2], n, r[4], lists_by_job[r[2]])
    # window rows: decide by number-sequence continuity against the confirmed runs of the same job
    for job in {r[2] for r, _n, _p in parsed}:
        conf = {"old": [], "new": []}
        for r, n, _p in parsed:
            if r[2] == job and verdict[r[0]][0] in conf:
                conf[verdict[r[0]][0]].append((r[4], n))
        for r, n, _p in parsed:
            if r[2] != job or verdict[r[0]][0] != "review":
                continue
            fit = {}
            for era, seq in conf.items():
                lo = max((x for t, x in seq if t <= r[4]), default=0)
                hi = min((x for t, x in seq if t >= r[4]), default=10**9)
                fit[era] = lo <= n <= hi
            if fit["old"] != fit["new"]:
                era = "old" if fit["old"] else "new"
                verdict[r[0]] = (era, "overlap window, decided by number-sequence continuity")
    for r, n, prefix in parsed:
        era, why = verdict[r[0]]
        if era == "review":
            out.append(row_dict(r, n, "review", r[1], r[3], why))
            continue
        groups[(r[2], n, prefix, era)].append((r, why))
    for (job, n, prefix, era), grp in groups.items():
        grp.sort(key=lambda x: x[0][4])
        execs = []
        for item in grp:
            if execs and abs((item[0][4] - execs[-1][0][0][4]).total_seconds()) <= SAME_RUN_S:
                execs[-1].append(item)
            else:
                execs.append([item])
        for ei, ex in enumerate(execs):
            ex.sort(key=lambda x: (x[0][8], x[0][0]))  # earliest ingested first
            base = f"{prefix}o{n}" if era == "old" else f"{prefix}{n}"
            if len(execs) > 1:
                base = f"{base}-x{ei + 1}"  # two DIFFERENT executions under one number in one era: needs a human look
            for k, (r, why) in enumerate(ex):
                new_id = base if k == 0 else f"{base}-dup{k}"
                d = row_dict(r, n, era, new_id, "" if era == "old" else n, why)
                d["multi_exec"] = len(execs) > 1
                out.append(d)
    return out, skipped


def row_dict(r, n, era, new_id, new_build, why):
    return {
        "id": r[0], "old_run_id": r[1], "old_build": r[3], "started_at": r[4].astimezone(TZ8).strftime("%Y-%m-%d %H:%M"),
        "version": r[5], "tenant": r[6], "dc": r[7], "era": era, "new_run_id": new_id, "new_build": new_build,
        "multi_exec": False, "why": why, "build_url": r[9] or "",
    }


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", default="%GRS-SYSTEMTEST-%", help="ILIKE pattern on jenkins_job")
    ap.add_argument("--out", help="write the plan as CSV")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    conn = dbconn.connect()
    cur = conn.cursor()
    out, skipped = plan(cur, a.job)
    review = [p for p in out if p["era"] == "review"]
    changes = [p for p in out if p["era"] != "review" and (p["new_run_id"] != p["old_run_id"] or str(p["new_build"]) != str(p["old_build"] or ""))]
    ids = [p["new_run_id"] for p in out if p["era"] != "review"]
    clash = {i for i in ids if ids.count(i) > 1}
    print(f"rows considered {len(out)}, to change {len(changes)}, review (not changed) {len(review)}, skipped (id not recognised) {len(skipped)}")
    for era in ("old", "new"):
        e = [p for p in out if p["era"] == era]
        print(f"  era {era}: {len(e)} rows, {len([p for p in e if p in changes])} change, {len([p for p in e if p['multi_exec']])} in multi-execution groups")
    whys = defaultdict(int)
    for p in out:
        whys[(p["era"], p["why"])] += 1
    for (era, why), c in sorted(whys.items()):
        print(f"    {era:<6} {c:>5}  {why}")
    print(f"  duplicate target run_ids inside the plan: {len(clash)}")
    if a.out:
        with open(a.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
            w.writeheader()
            w.writerows([p for p in out if p in changes or p["era"] == "review"])
        print(f"plan written to {a.out}")
    if not a.apply:
        return 0
    if clash or review:
        print(f"REFUSED: {len(clash)} duplicate target ids, {len(review)} rows still in review; resolve them first")
        return 1
    # two passes so a rename never meets a name that another row is about to free: park every changing row first
    for p in changes:
        cur.execute("UPDATE test_run SET run_id = %s WHERE id = %s", (f"__relabel_{p['id']}", p["id"]))
    for p in changes:
        cur.execute(
            "UPDATE test_run SET run_id = %s, jenkins_build = %s WHERE id = %s",
            (p["new_run_id"], p["new_build"] if p["new_build"] != "" else None, p["id"]),
        )
    conn.commit()
    print(f"relabelled {len(changes)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
