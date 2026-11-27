"""`db.py final <release>`: build the publish snapshot `release-<N>-final` for ANY release.

  python db.py final 143                 classify every non-PASS row, write keep_lists/release-143-final.json,
                                         print the counts a clone would give (dry run, writes no DB rows)
  python db.py final 143 --apply         clone release-143 into release-143-final with that keep list
  python db.py final 143 --apply --redo  drop release-143-final first (the source keeps receiving builds,
                                         so a snapshot is point-in-time; regenerate it after more runs)

Nothing here knows a lane, a job name or a build number: the rules (final_rules.json) match only the
failure text, `has_dump` and the gate_ticket table, and every job string comes from the DB rows, so a new
or renamed lane works unchanged. A row that no rule matches is filtered as ambiguous and listed, never
silently kept. Decisions a person makes live in the keep file's `overrides` section and are re-applied
on every re-run, so regenerating after new builds does not lose them.

The source release is never modified. Policy and rationale: runbook_release_final_snapshot.md.
"""

import argparse
import json
import os
import re
import sys

from version_rules import PATCH_LINE_RE

HERE = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(HERE, "final_rules.json")
KEEP_DIR = os.path.join(HERE, "keep_lists")
REPORT_BASE = "http://10.136.218.74:8080/report"

SQL_NONPASS = """
select tr.test_name, r.jenkins_job, r.jenkins_build, tr.verdict,
       coalesce(tr.iterations_passed, 0), tr.iterations_target,
       coalesce(tr.has_dump, false), coalesce(tr.fail_reason, ''), coalesce(tr.duration_s, 0), r.platform
from test_result tr
join test_run r on r.id = tr.run_id
join release rel on rel.id = r.release_id
where rel.label = %s and tr.verdict != 'PASS'
  and (r.product_version is null or r.product_version !~ %s)
order by tr.test_name, r.jenkins_job, r.jenkins_build
"""


def parse_release(text):
    """'143', 'release-143', '143-final', 'release-143-final' -> 143 (int)."""
    m = re.match(r"^(?:release-)?(\d+)(?:-final)?$", str(text).strip())
    if not m:
        raise SystemExit(f"cannot read a release number from {text!r} (want 143, release-143 or 143-final)")
    return int(m.group(1))


def load_rules(path=RULES_PATH):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rules = []
    for r in data["rules"]:
        rules.append({**r, "rx": re.compile(r["pattern"], re.DOTALL)})
    return rules


def fetch_rows(conn, label):
    with conn.cursor() as cur:
        cur.execute(SQL_NONPASS, (label, PATCH_LINE_RE))
        cols = ["test_name", "job", "build", "verdict", "passed", "target", "has_dump", "reason", "duration_s", "platform"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_pass_index(conn, label):
    """{(test_name, job): [builds with a PASS]} for the release."""
    idx = {}
    with conn.cursor() as cur:
        cur.execute(
            """select tr.test_name, r.jenkins_job, r.jenkins_build from test_result tr
               join test_run r on r.id = tr.run_id join release rel on rel.id = r.release_id
               where rel.label = %s and tr.verdict = 'PASS'
                 and (r.product_version is null or r.product_version !~ %s)""",
            (label, PATCH_LINE_RE),
        )
        for t, j, b in cur.fetchall():
            idx.setdefault((t, j), []).append(b)
    return idx


def fetch_tickets(conn):
    """{(job, build, test_name_or_None)} for every gate_ticket row (empty when the table is absent)."""
    try:
        with conn.cursor() as cur:
            cur.execute("select jenkins_job, jenkins_build, test_name from gate_ticket")
            return {(j, b, t) for j, b, t in cur.fetchall()}
    except Exception:
        conn.rollback()
        return set()


GRS_REPO = os.environ.get("GRS_REPO", "C:/git/nsclient_golden_regression_suite")


def fetch_never_passed(conn, label):
    """Test names that have at least one FAIL and not a single PASS in the release."""
    with conn.cursor() as cur:
        cur.execute(
            """select tr.test_name from test_result tr
               join test_run r on r.id = tr.run_id join release rel on rel.id = r.release_id
               where rel.label = %s and (r.product_version is null or r.product_version !~ %s) group by tr.test_name
               having count(*) filter (where tr.verdict = 'PASS') = 0 and count(*) filter (where tr.verdict = 'FAIL') > 0""",
            (label, PATCH_LINE_RE),
        )
        return {row[0] for row in cur.fetchall()}


def tests_on_main(repo=GRS_REPO):
    """Names of every test function on origin/main of the GRS repo, or None when git cannot answer (the guard is then off)."""
    import subprocess

    try:
        ls = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "origin/main", "--name-only"], capture_output=True, text=True, timeout=120)
        if ls.returncode != 0:
            return None
        names = set()
        for f in ls.stdout.split():
            if not (f.endswith(".py") and "/tests/" in f):
                continue
            src = subprocess.run(["git", "-C", repo, "show", f"origin/main:{f}"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60).stdout
            names.update(re.findall(r"^\s*def (test_\w+)", src, re.M))
        return names or None
    except Exception:
        return None


def classify_row(row, rules, tickets):
    """-> (action 'keep'|'filter', rule id, reason)."""
    if row["verdict"] == "SKIP":
        # Only failures may be filtered out; a skip is not a failure, so it stays in the snapshot.
        return "keep", "skip", "not a failure; kept as skipped"
    key = (row["job"], row["build"], row["test_name"])
    if row["has_dump"] or key in tickets or (row["job"], row["build"], None) in tickets:
        return "keep", "dump-or-ticket", "has a dump or a gate_ticket row (structured evidence)"
    for r in rules:
        if r["rx"].search(row["reason"]):
            return r["action"], r["id"], r["reason"]
    return "filter", "ambiguous", "no rule matched; filtered by the when-in-doubt policy"


def _excerpt(text, n=110):
    one = " ".join((text or "").split())
    return one if len(one) <= n else one[: n - 3] + "..."


def _keep_main_never_passed(decided, never_passed, on_main):
    """A test that is on origin/main and has no PASS anywhere in the release is a big issue, never filter noise: keep its
    latest FAIL row whatever rule matched. A test that is not on main is still in development and stays unlisted."""
    def _ran_on_its_platform(d):
        # a _win test that only ever ran on a mac lane (or the reverse) was sent to the wrong VM: that is no evidence about the test
        n, plat = d["test_name"], d.get("platform")
        return (plat == "windows") if n.endswith("_win") else (plat == "mac") if n.endswith("_mac") else True

    by_test = {}
    for d in decided:
        if d["test_name"] in never_passed and d["test_name"] in on_main and d["verdict"] == "FAIL" and _ran_on_its_platform(d):
            by_test.setdefault(d["test_name"], []).append(d)
    for rows in by_test.values():
        last = max(rows, key=lambda d: (d["build"] if d["build"] is not None else -1, d["job"]))
        last["action"], last["rule"] = "keep", "main-never-passed"
        last["why"] = "on origin/main and no PASS anywhere in this release: a test that exists but has never passed must be visible"


def _refine_ambiguous(decided, pass_index):
    """A failure no rule explains is not dropped silently: if the same test passed LATER on the same lane, the failure
    is superseded by that rerun (filtered, with the build that passed); otherwise it is kept as an unresolved failure."""
    for d in decided:
        if d["rule"] != "ambiguous" or d["action"] != "filter":
            continue
        # a PASS row can have no build number either (R140's old LOCALTEST runs): it can never count as "later"
        later = [
            b
            for b in pass_index.get((d["test_name"], d["job"]), [])
            if d["build"] is not None and b is not None and b > d["build"]
        ]
        if later:
            d["action"], d["rule"] = "filter", "superseded-by-rerun"
            d["why"] = f"failed, then the same test passed later on the same lane (build {min(later)})"
            d["superseded_by"] = min(later)
        else:
            d["action"], d["rule"] = "keep", "unresolved-failure"
            d["why"] = "no rule explains it and no later PASS on the same lane: kept as an unresolved failure"


def classify_all(rows, rules, tickets, overrides, never_passed=None, on_main=None, pass_index=None):
    forced_keep = {(o["job"], o.get("build"), o["test_name"]) for o in overrides.get("keep", [])}
    forced_filter = {(o["job"], o.get("build"), o["test_name"]) for o in overrides.get("filter", [])}
    decided = []
    for row in rows:
        key = (row["job"], row["build"], row["test_name"])
        if key in forced_keep:
            action, rid, why = "keep", "override", "kept by a person (overrides.keep)"
        elif key in forced_filter:
            action, rid, why = "filter", "override", "filtered by a person (overrides.filter)"
        else:
            action, rid, why = classify_row(row, rules, tickets)
        decided.append({**row, "action": action, "rule": rid, "why": why})
    if pass_index is not None:
        _refine_ambiguous([d for d in decided if (d["job"], d["build"], d["test_name"]) not in forced_filter], pass_index)
    if never_passed and on_main:
        _keep_main_never_passed([d for d in decided if (d["job"], d["build"], d["test_name"]) not in forced_filter], never_passed, on_main)

    # A NULL build cannot be keyed per row: one (job, test) key would keep every null-build row of that
    # job and test. Keep such a key only when every one of its non-PASS rows is keepable (or forced).
    groups = {}
    for d in decided:
        if d["build"] is None:
            groups.setdefault((d["job"], d["test_name"]), []).append(d)
    conflicts = []
    for (job, test), members in groups.items():
        keeps = [m for m in members if m["action"] == "keep"]
        if keeps and len(keeps) != len(members):
            forced = any((m["job"], None, m["test_name"]) in forced_keep for m in members)
            conflicts.append({"job": job, "test_name": test, "keepable": len(keeps), "rows": len(members), "forced": forced})
            if not forced:
                for m in keeps:
                    m["action"], m["rule"], m["why"] = "filter", "null-build-conflict", "null build shares its key with filtered rows"
    return decided, conflicts


def build_doc(release, decided, conflicts, overrides, tickets_n):
    keep, seen = [], set()
    for d in decided:
        if d["action"] != "keep":
            continue
        key = (d["job"], d["build"], d["test_name"])
        if key in seen:
            continue
        seen.add(key)
        keep.append(
            {
                "job": d["job"],
                "build": d["build"],
                "test_name": d["test_name"],
                "rule": d["rule"],
                "reason": f"{d['why']}: {_excerpt(d['reason'])}",
            }
        )
    by_rule = {}
    for d in decided:
        by_rule[(d["action"], d["rule"])] = by_rule.get((d["action"], d["rule"]), 0) + 1
    return {
        "source_release": f"release-{release}",
        "target_release": f"release-{release}-final",
        "generated_by": "db.py final (rules: final_rules.json)",
        "policy": "PASS rows are always kept. A non-PASS row is kept only with concrete product evidence "
        "(measured value, named list, dump, gate_ticket); setup, config-sync, probe, installer and by-design-wait "
        "noise is filtered; anything ambiguous is filtered. A NULL build keys every null-build row of its job+test.",
        "snapshot_note": "Point-in-time copy. Re-run `db.py final <N> --apply --redo` after more builds finish.",
        "counts": {"non_pass_rows": len(decided), "kept": len([d for d in decided if d["action"] == "keep"]), "gate_tickets_seen": tickets_n},
        "by_rule": {f"{a}:{r}": n for (a, r), n in sorted(by_rule.items())},
        "null_build_conflicts": conflicts,
        "overrides": overrides,
        "keep": keep,
        # Every failure that was left out of the snapshot, with the reason and the time it took, so no number is unexplained.
        "filtered": [
            {
                "job": d["job"],
                "build": d["build"],
                "test_name": d["test_name"],
                "verdict": d["verdict"],
                "rule": d["rule"],
                "reason": d["why"],
                "superseded_by_build": d.get("superseded_by"),
                "duration_s": round(float(d.get("duration_s") or 0), 1),
                "failure": _excerpt(d["reason"], 160),
            }
            for d in decided
            if d["action"] == "filter"
        ],
        "filtered_summary": _filtered_summary(decided),
        "ambiguous_for_review": [
            {"job": d["job"], "build": d["build"], "test_name": d["test_name"], "reason": _excerpt(d["reason"], 160)}
            for d in decided
            if d["rule"] == "unresolved-failure"
        ],
    }


def _filtered_summary(decided):
    out = {}
    for d in decided:
        if d["action"] != "filter":
            continue
        s = out.setdefault(d["rule"], {"results": 0, "hours": 0.0})
        s["results"] += 1
        s["hours"] += float(d.get("duration_s") or 0) / 3600.0
    return {k: {"results": v["results"], "hours": round(v["hours"], 1)} for k, v in sorted(out.items(), key=lambda kv: -kv[1]["results"])}


def _counts_after(conn, label):
    with conn.cursor() as cur:
        cur.execute(
            "select count(distinct r.id), count(*) filter (where tr.verdict='PASS'), count(*) "
            "from test_run r join release rel on rel.id=r.release_id left join test_result tr on tr.run_id=r.id "
            "where rel.label=%s",
            (label,),
        )
        return cur.fetchone()


def _pass_without_patch_lines(conn, label):
    """PASS result rows of the release whose run is not a patch line (140.1.4.2805): what a final cut must hold."""
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from test_result tr join test_run r on r.id=tr.run_id join release rel on rel.id=r.release_id "
            "where rel.label=%s and tr.verdict='PASS' and (r.product_version is null or r.product_version !~ %s)",
            (label, PATCH_LINE_RE),
        )
        return cur.fetchone()[0]


def main(argv, db):
    """`db` is the db module (cmd_clone_release / cmd_drop_release / dbconn)."""
    ap = argparse.ArgumentParser(prog="db.py final", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("release", help="143, release-143 or 143-final")
    ap.add_argument("--apply", action="store_true", help="clone with the keep file (default is classify + dry run)")
    ap.add_argument("--redo", action="store_true", help="with --apply: drop the existing -final release first")
    ap.add_argument("--keep-file", help="keep list to write/use (default keep_lists/release-<N>-final.json)")
    ap.add_argument("--reclassify", action="store_true", help="with --apply: rebuild the keep file from the rules first")
    a = ap.parse_args(argv)

    n = parse_release(a.release)
    src, tgt = f"release-{n}", f"release-{n}-final"
    keep_file = a.keep_file or os.path.join(KEEP_DIR, f"{tgt}.json")

    overrides = {"keep": [], "filter": []}
    if os.path.exists(keep_file):
        with open(keep_file, encoding="utf-8") as f:
            overrides = json.load(f).get("overrides") or overrides

    conn = db.dbconn.connect()
    try:
        if not a.apply or a.reclassify or not os.path.exists(keep_file):
            rows = fetch_rows(conn, src)
            if not rows and not _counts_after(conn, src)[0]:
                print(f"[error] release '{src}' has no runs in the DB")
                return 2
            tickets = fetch_tickets(conn)
            never_passed = fetch_never_passed(conn, src)
            on_main = tests_on_main()
            if on_main is None:
                print("[final] WARNING: could not read origin/main of the GRS repo; the main-never-passed guard is OFF")
            else:
                dev_only = sorted(t for t in never_passed if t not in on_main)
                if dev_only:
                    print(f"[final] not on main, never passed, not listed (still in development): {', '.join(dev_only)}")
            decided, conflicts = classify_all(rows, load_rules(), tickets, overrides, never_passed, on_main, fetch_pass_index(conn, src))
            doc = build_doc(n, decided, conflicts, overrides, len(tickets))
            os.makedirs(os.path.dirname(keep_file), exist_ok=True)
            with open(keep_file, "w", encoding="utf-8", newline="\n") as f:
                json.dump(doc, f, indent=1, ensure_ascii=False)
                f.write("\n")
            c = doc["counts"]
            print(f"[final] {src}: {c['non_pass_rows']} non-PASS rows -> {c['kept']} kept, {c['non_pass_rows'] - c['kept']} filtered")
            print(f"[final] wrote {keep_file}")
            for k, v in doc["by_rule"].items():
                print(f"    {v:4d}  {k}")
            if conflicts:
                print(f"[final] {len(conflicts)} null-build key(s) share a key with filtered rows (see null_build_conflicts); force with overrides.keep")
            if doc["ambiguous_for_review"]:
                print(f"[final] {len(doc['ambiguous_for_review'])} unresolved failure(s) kept; read them in ambiguous_for_review and add overrides.filter for any that are not product evidence")
            fs_ = doc["filtered_summary"]
            print(f"[final] filtered failures: {sum(v['results'] for v in fs_.values())} results, {sum(v['hours'] for v in fs_.values()):.1f} h")
        ns = argparse.Namespace(source=src, target=tgt, keep_file=keep_file, dry_run=not a.apply, label=tgt, yes=True)
        if a.apply and a.redo:
            with conn.cursor() as cur:
                cur.execute("select 1 from release where label=%s", (tgt,))
                exists = cur.fetchone() is not None
            if exists:
                rc = db.cmd_drop_release(ns)
                if rc not in (0, None):
                    return rc
        before = _counts_after(conn, src)
        rc = db.cmd_clone_release(ns)
        if rc not in (0, None):
            if _target_exists(conn, tgt) and not a.redo:
                print(f"[final] {tgt} already exists; to regenerate it: python db.py final {n} --apply --redo")
            return rc
        if not a.apply:
            print(f"[final] dry run only. Apply: python db.py final {n} --apply" + (" --redo" if _target_exists(conn, tgt) else ""))
            return 0
        after = _counts_after(conn, src)
        t = _counts_after(conn, tgt)
        src_pass = _pass_without_patch_lines(conn, src)  # a final cut leaves out patch-line runs, so they are not expected
        ok = before == after and t[1] == src_pass
        print(f"[final] verify: {src} unchanged={before == after} runs={after[0]}; {tgt} runs={t[0]} PASS={t[1]} (source PASS={src_pass}) -> {'OK' if ok else 'MISMATCH'}")
        print(f"[final] report: {REPORT_BASE}/{n}-final   (data refreshes within the 30 s cache)")
        return 0 if ok else 1
    finally:
        conn.close()


def _target_exists(conn, label):
    with conn.cursor() as cur:
        cur.execute("select count(*) from test_run r join release rel on rel.id=r.release_id where rel.label=%s", (label,))
        return cur.fetchone()[0] > 0
