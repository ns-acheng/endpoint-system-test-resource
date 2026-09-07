#!/usr/bin/env python
"""MIRROR NOTICE (added for endpoint-system-test-resource): this is a point-in-time copy for onboarding/public reference.
Canonical source (private repo): claude-resource/grs_tools/system_test_db/query.py -- do not edit this copy directly; changes land upstream first, then get re-synced here.
"""

"""SystemTest /query page: pick test case(s) by checkbox, see the most recent
PASS results for each of the 5 SystemTest lanes (REG / REG-02 / LOCAL1 / LOCAL2
/ MAC1).

Answers "did steer01 recently pass on every lane, and when/what tenant/dc/
bitness/duration" without hand SQL. Served by report_server.py at GET /query
(?test=<name>&test=<name2>... repeatable) — GET so the URL stays shareable,
same convention as /report/<release>/<tenant>.
"""

import math
import os
import sys
from urllib.parse import parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dbconn  # noqa: E402
import report  # noqa: E402

# Single source for job aliases, same import as db.py:444 (a hand-copied table
# here is exactly the class of bug that broke localtest/LOCAL1 for 2 days).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from grs_jenkins import ALIASES as _JOB_ALIASES  # noqa: E402

_TOP_N = 2


def _db_job(alias):
    """test_run.jenkins_job is stored WITHOUT the "/job/" segment
    (ingest.py: job_name is already foldered, e.g. 'DEV/GRS-SYSTEMTEST-REG'),
    so derive from grs_jenkins.ALIASES (single source, same pattern as
    db.py's _alias_map) instead of hand-typing job paths a second time."""
    path = _JOB_ALIASES.get(alias, "")
    return "DEV/" + path.split("/")[-1] if path else None


# Lane display order + which test_run.jenkins_job values count as that lane.
LANES = [
    ("REG", [_db_job("reg")]),
    ("REG-02", [_db_job("reg2")]),
    (
        "LOCAL1",
        [
            _db_job("local1"),
            # pre-rename literal (grs_jenkins.py ALIASES comment: renamed from
            # GRS-SYSTEMTEST-LOCALTEST 2026-08-01) — old DB rows still carry it.
            "DEV/GRS-SYSTEMTEST-LOCALTEST",
        ],
    ),
    ("LOCAL2", [_db_job("local2")]),
    ("MAC1", [_db_job("mac1")]),
]

_COLS = [
    "Lane",
    "Run #",
    "Pass iter",
    "Tenant",
    "DC",
    "Bitness",
    "Date/time",
    "Duration",
]


def _form_css(n):
    """n = number of checkboxes. grid-auto-flow:column + explicit row counts
    (one set per breakpoint's column count) makes the already-alphabetical
    list READ alphabetically top-to-bottom-then-next-column. Plain
    grid-template-columns (default row-major flow) would instead put items
    1,4,7.. in column 1 — sorted in the DB, but not sorted to the eye."""
    rows3 = math.ceil(n / 3) or 1
    rows2 = math.ceil(n / 2) or 1
    # max-content columns (not 1fr): each column sizes to its widest label, so
    # a long test name is never truncated/ellipsized. If that makes the grid
    # wider than the card, it scrolls horizontally instead of wrapping the
    # text — "no trim, no wrap, more rows is ok" (owner 2026-08-27).
    return (
        ".qform{margin-bottom:20px}"
        f".qlist{{display:grid;grid-auto-flow:column;"
        f"grid-template-rows:repeat({rows3}, auto);"
        "grid-template-columns:repeat(3, max-content);gap:4px 32px;"
        "max-height:420px;overflow:auto;padding:14px;background:var(--card2);"
        "border:1px solid var(--border);border-radius:var(--radius)}"
        "@media (max-width:1100px){.qlist{grid-template-columns:repeat(2, max-content);"
        f"grid-template-rows:repeat({rows2}, auto)}}}}"
        "@media (max-width:650px){.qlist{grid-template-columns:max-content;"
        f"grid-template-rows:repeat({n or 1}, auto)}}}}"
        ".qlist label{display:flex;align-items:center;gap:8px;font-size:13px;"
        "font-family:'JetBrains Mono',Consolas,monospace;padding:4px 6px;"
        "border-radius:6px;cursor:pointer;white-space:nowrap}"
        ".qlist label:hover{background:rgba(255,255,255,0.05)}"
        ".qlist input{flex-shrink:0}"
        ".qbtn{margin-top:14px;background:var(--grad);color:#fff;border:none;"
        "border-radius:var(--radius-sm);padding:10px 20px;font-size:14px;font-weight:600;"
        "cursor:pointer}"
    )


def list_test_names(cur):
    cur.execute("SELECT DISTINCT test_name FROM test_result ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def _fmt_bitness(installed, cli):
    if installed is True:
        return "64-bit"
    if installed is False:
        return "32-bit"
    if cli is True:
        return "64-bit?"
    if cli is False:
        return "32-bit?"
    return "-"


def _fmt_iter(passed, target, total):
    denom = target if target is not None else total
    if passed is None and denom is None:
        return "-"
    return (
        f"{passed if passed is not None else 0}/{denom if denom is not None else '?'}"
    )


def _fmt_dur(seconds):
    if seconds is None:
        return "-"
    s = int(seconds)
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def _latest_pass(cur, test_name, jobs):
    jobs = [j for j in jobs if j]
    if not jobs:
        return []
    # Ordered/displayed by test_run.started_at, not test_result.started_at: the
    # latter is never populated by ingest (verified live, 2026-08-27: 0/590 rows
    # have it) while the run's start time is always there and is a fine proxy
    # for "when this test result happened" (one run = one pytest session).
    #
    # run_id NOT LIKE '%-int': excludes curated publish-snapshot CLONE rows
    # (db.py:1764 copies a run into a "release-141-int" style release with
    # run_id + "-int", same jenkins_build/started_at) — without this filter
    # the same real execution shows up twice under an identical build number.
    cur.execute(
        """
        SELECT r.jenkins_build, tr.iterations_passed, tr.iterations_target,
               tr.iterations_total, r.tenant, r.dc, r.is_64_bit_installed,
               r.is_64_bit_cli, r.started_at, tr.duration_s
        FROM test_result tr
        JOIN test_run r ON tr.run_id = r.id
        WHERE tr.test_name = %s AND tr.verdict = 'PASS' AND r.jenkins_job = ANY(%s)
              AND r.run_id NOT LIKE '%%-int'
        ORDER BY r.started_at DESC NULLS LAST
        LIMIT %s
        """,
        (test_name, jobs, _TOP_N),
    )
    return cur.fetchall()


def _test_table(cur, test_name):
    data = []
    for lane, jobs in LANES:
        found = _latest_pass(cur, test_name, jobs)
        if not found:
            data.append((lane, "-", "-", "-", "-", "-", "no PASS record", "-"))
            continue
        for (
            build,
            passed,
            target,
            total,
            tenant,
            dc,
            inst64,
            cli64,
            started,
            dur,
        ) in found:
            data.append(
                (
                    lane,
                    build,
                    _fmt_iter(passed, target, total),
                    tenant or "-",
                    dc or "-",
                    _fmt_bitness(inst64, cli64),
                    started.strftime("%Y-%m-%d %H:%M") if started else "-",
                    _fmt_dur(dur),
                )
            )
    return f'<div class="card"><h2>{report.esc(test_name)}</h2>{report.table(_COLS, data)}</div>'


def build_query_page(selected):
    conn = dbconn.connect()
    try:
        cur = conn.cursor()
        all_tests = list_test_names(cur)
        checks = "".join(
            f'<label><input type="checkbox" name="test" value="{report.esc(t)}"'
            f'{" checked" if t in selected else ""}> {report.esc(t)}</label>'
            for t in all_tests
        )
        form = (
            '<div class="card qform"><h2>Pick test case(s)</h2>'
            '<form method="get" action="/query">'
            f'<div class="qlist">{checks}</div>'
            f'<button class="qbtn" type="submit">Show latest {_TOP_N} PASS per lane</button>'
            "</form></div>"
        )
        results = "".join(_test_table(cur, t) for t in selected if t in all_tests)
        if selected and not results:
            results = (
                '<div class="card"><p class="foot">'
                "No selected test matches a known test case.</p></div>"
            )
        body = f"<style>{_form_css(len(all_tests))}</style>{form}{results}"
    finally:
        conn.close()
    return report.PAGE_TEMPLATE.format(release="Query", body=body)


def selected_from_query(path):
    """path = '/query' or '/query?test=a&test=b'. Ordered, deduped selection."""
    if "?" not in path:
        return []
    qs = parse_qs(path.split("?", 1)[1])
    seen = []
    for t in qs.get("test", []):
        if t not in seen:
            seen.append(t)
    return seen
