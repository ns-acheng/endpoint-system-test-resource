#!/usr/bin/env python
"""MIRROR NOTICE (added for endpoint-system-test-resource): this is a point-in-time copy for onboarding/public reference.
Canonical source (private repo): claude-resource/grs_tools/system_test_db/report.py -- do not edit this copy directly; changes land upstream first, then get re-synced here.
"""

"""Generate an HTML report from the SystemTest results DB (demo / visualization).

Reads the Postgres DB populated by the sibling ingest tool and renders a single
self-contained HTML file: no server, no build step, embeds Chart.js from a CDN.
Connection settings resolve via dbconn.py (env vars or ~/.grs_db.conf) — the same
way as db.py; no credentials live here.

This is a DEMO/visualization of the data we collect, NOT a CI gate. It answers,
per release (default release-140):
  - per test case: run count, avg/total duration, pass/fail, gate/dump flags
  - health-gate breakdown (crash / BSOD / CPU / mem / handles / reboot)
  - resource peaks per process (CPU per-core, mem growth, handles) + baselines
  - iteration verdict sequences (red/green) for the most-run cases
  - failure reasons (grouped) and any dumps
Large tables are capped (top-N) with a note; nothing is silently truncated.

Usage:
    python report.py [--release release-140] [--out report.html] [--top 10] [--tenant 1457] [--platform mac]
"""

import argparse
import html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dbconn  # noqa: E402


def connect():
    return dbconn.connect()


# ─────────────────────────── query helpers ───────────────────────────
def rows(cur, sql, params=None):
    cur.execute(sql, params or {})
    cols = [d[0] for d in cur.description]
    return cols, cur.fetchall()


def scalar(cur, sql, params=None):
    cur.execute(sql, params or {})
    r = cur.fetchone()
    return r[0] if r else None


# ─────────────────────────── HTML building ───────────────────────────
def esc(v):
    return html.escape("" if v is None else str(v))


def kpi(label, value, sub=""):
    sub_html = f'<div class="delta">{esc(sub)}</div>' if sub else ""
    return f'<div class="kpi"><div class="label">{esc(label)}</div><div class="value">{esc(value)}</div>{sub_html}</div>'


def table(cols, data, cls="", highlight=None):
    """highlight: optional fn(row_dict) -> css class for the <tr>."""
    th = "".join(f"<th>{esc(c)}</th>" for c in cols)
    trs = []
    for r in data:
        rd = dict(zip(cols, r))
        rowcls = highlight(rd) if highlight else ""
        tds = "".join(f"<td>{esc(v)}</td>" for v in r)
        trs.append(f'<tr class="{rowcls}">{tds}</tr>')
    return f'<table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table>'


def filter_bar(release, platform, tenant, platform_options, tenant_options):
    """Two <select> dropdowns (platform, tenant) + an Apply button, GET-submitted
    to /report/<release> (short_release'd -- see short_release). Zero JS: same
    host-page convention as query.py's checkbox <form>. Whatever a viewer picks
    becomes the canonical ?platform=&tenant= query string on the SHORT URL, so
    the legacy long/suffix/path-segment forms (still accepted on input) never
    reappear once someone touches a dropdown.
    """

    def _opt(value, label, current):
        selected = " selected" if (value or None) == (current or None) else ""
        return f'<option value="{esc(value)}"{selected}>{esc(label)}</option>'

    plat_opts = _opt("", "All platforms", platform) + "".join(
        _opt(p, p.capitalize(), platform) for p in platform_options
    )
    tenant_opts = _opt("", "All tenants", tenant) + "".join(
        _opt(t, t, tenant) for t in tenant_options
    )
    return (
        f'<form class="filter-bar" method="get" action="/report/{esc(short_release(release))}">'
        f'<label>Platform <select name="platform">{plat_opts}</select></label>'
        f'<label>Tenant <select name="tenant">{tenant_opts}</select></label>'
        '<button type="submit">Apply</button>'
        "</form>"
    )


_PLATFORMS = ("mac", "windows", "linux")


def split_platform(release):
    """ "release-141-mac" -> ("release-141", "mac"); no suffix -> (release, None).

    A platform-only view is a query-time filter on the existing run.platform
    column (populated at ingest, see ingest.py:612), not a second physical
    release row — the "-final" rows are a different, temporal axis, not a
    precedent to copy here (that would force every mac run to pick between
    release-142 and release-142-mac at ingest time).
    """
    s = release or ""
    for plat in _PLATFORMS:
        if s.lower().endswith(f"-{plat}"):
            return s[: -(len(plat) + 1)], plat
    return s, None


def short_release(label):
    """ "release-142" -> "142"; "release-141-final" -> "141-final" (only the
    leading "release-" prefix is stripped, any suffix like -final/-mac stays
    intact). Used everywhere the UI DISPLAYS or LINKS to a release, so a page
    never shows the redundant long form (owner 2026-08-27: two different-
    looking URLs for the same report is what confuses people, not the short
    form itself). _norm_release still accepts the long form on input for
    backward compat with old bookmarks -- this only changes what we generate.
    """
    s = label or ""
    return s[len("release-") :] if s.startswith("release-") else s


def build_report(release, top, tenant=None, platform=None):
    conn = connect()
    cur = conn.cursor()

    # header stats. Shared join skeleton keeps the count queries short + readable.
    # ver_prefix: derived from release label (release-140 -> '140.%') so builds
    # whose product_version belongs to a different major are excluded from every
    # query in this report. NULL product_version rows (infra-cancel, no client
    # installed) are kept (they carry no version at all, not a wrong one).
    # Regex-extract the leading digits (same rule as ingest._release_ordinal,
    # duplicated rather than imported: report_server.py imports this module at
    # its OWN top level to stay a light live-serving process, and ingest.py's
    # module level pulls in the full GRS repo incl. FTP_PASSWORD-gated code —
    # NOT a dependency this report can carry). A naive `label.split("-")[-1]`
    # reads "int" out of "release-141-int" and silently guards out every real
    # row (found while building the release-141-int publish snapshot, 2026-08-21).
    # explicit platform= wins (a caller that already resolved query-string vs
    # legacy suffix, e.g. report_server); a bare "-mac" suffix on release is
    # still honored when nothing explicit was passed, so the CLI's
    # --release release-142-mac usage keeps working unchanged.
    release, suffix_platform = split_platform(release)
    platform = platform or suffix_platform
    tenant = tenant or None  # "" from a URL query counts as "no filter"
    _m = re.search(r"(\d+)", release or "")
    _major = int(_m.group(1)) if _m else None
    p = {
        "rel": release,
        "ver_prefix": f"{_major}.%" if _major is not None else "%",
        "platform": platform,
        "tenant": tenant,
    }
    # Optional tenant scope (e.g. one tenant's SystemTest history): appended to
    # every guard below so a single param threads through all 16 query sites
    # instead of touching each one. %(tenant)s IS NULL short-circuits the OR
    # to a no-op when tenant is None — same trick _VER_GUARD already uses for
    # ver_prefix, not a new pattern.
    _TENANT_GUARD = " AND (%(tenant)s IS NULL OR run.tenant=%(tenant)s)"
    _VER_GUARD = (
        "AND (run.product_version IS NULL OR run.product_version LIKE %(ver_prefix)s)"
        + _TENANT_GUARD
    )
    if platform:
        _VER_GUARD += " AND run.platform=%(platform)s"
    _PLAT_ONLY = ("AND run.platform=%(platform)s" if platform else "") + _TENANT_GUARD
    RUN_J = "test_run run JOIN release r ON r.id=run.release_id"
    TR_J = "test_result tr JOIN test_run run ON run.id=tr.run_id JOIN release r ON r.id=run.release_id"

    # A SKIP row (the platform-mismatched sibling wrapper, e.g. the mac test on
    # a windows run) never executed — it must not count as a run, inflate any
    # table, or show up anywhere per-test-case. n_runs counts SESSIONS (there
    # is no `tr` alias in RUN_J's FROM clause, and a session itself has no
    # verdict), so it keeps the pre-skip guard; every per-test-case query below
    # (all of which DO have `tr` in scope) gets the skip clause automatically
    # because they read `_VER_GUARD` after this line.
    _RUN_GUARD = _VER_GUARD
    _VER_GUARD = _RUN_GUARD + " AND tr.verdict <> 'SKIP'"

    def count_where(frm, extra="", guard=None):
        return scalar(
            cur,
            f"SELECT count(*) FROM {frm} WHERE r.label=%(rel)s {guard or _VER_GUARD} {extra}",
            p,
        )

    n_runs = count_where(RUN_J, guard=_RUN_GUARD)
    n_tests = count_where(TR_J)
    n_fail = count_where(TR_J, "AND tr.verdict='FAIL'")
    n_gate = count_where(TR_J, "AND tr.gate_violated")
    n_dump = count_where(TR_J, "AND tr.has_dump")
    versions = (
        scalar(
            cur,
            f"SELECT string_agg(DISTINCT run.product_version, ', ') FROM {RUN_J} "
            f"WHERE r.label=%(rel)s AND run.product_version IS NOT NULL {_PLAT_ONLY}",
            p,
        )
        or "n/a"
    )
    tenants = (
        scalar(
            cur,
            f"SELECT string_agg(DISTINCT run.tenant, ', ') FROM {RUN_J} "
            f"WHERE r.label=%(rel)s AND run.tenant IS NOT NULL {_PLAT_ONLY}",
            p,
        )
        or "n/a"
    )
    # Dropdown option lists for the filter bar. Deliberately UNGUARDED by the
    # current platform/tenant selection (plain "WHERE r.label=%(rel)s") -- each
    # dropdown must keep offering every value ever seen on this release, or
    # picking one filter would make the OTHER dropdown's options vanish/collapse
    # to a single entry the moment it's applied.
    _, _plat_rows = rows(
        cur,
        f"SELECT DISTINCT run.platform FROM {RUN_J} "
        "WHERE r.label=%(rel)s AND run.platform IS NOT NULL ORDER BY run.platform",
        p,
    )
    platform_options = [r[0] for r in _plat_rows]
    _, _tenant_rows = rows(
        cur,
        f"SELECT DISTINCT run.tenant FROM {RUN_J} "
        "WHERE r.label=%(rel)s AND run.tenant IS NOT NULL ORDER BY run.tenant",
        p,
    )
    tenant_options = [r[0] for r in _tenant_rows]
    n_samples = (
        scalar(
            cur,
            "SELECT count(*) FROM resource_sample rs "
            "JOIN test_result tr ON tr.id=rs.test_result_id "
            "JOIN test_run run ON run.id=tr.run_id "
            f"JOIN release r ON r.id=run.release_id WHERE r.label=%(rel)s {_VER_GUARD}",
            p,
        )
        or 0
    )

    total = n_tests or 1
    pass_rate = round((n_tests - n_fail) * 100.0 / total)

    # 1. per-test aggregate — sorted by test name (alphabetical, not by run count)
    c1, per_test = rows(
        cur,
        """
        SELECT tr.test_name AS test,
               count(*) AS runs,
               count(*) FILTER (WHERE tr.verdict='PASS') AS pass,
               count(*) FILTER (WHERE tr.verdict='FAIL') AS fail,
               round(avg(tr.duration_s),1) AS avg_dur_s,
               round(sum(tr.duration_s),0) AS total_dur_s,
               sum(tr.iterations_total) AS iters,
               count(*) FILTER (WHERE tr.gate_violated) AS gate_hits,
               count(*) FILTER (WHERE tr.has_dump) AS dumps
        FROM test_result tr JOIN test_run run ON run.id=tr.run_id JOIN release r ON r.id=run.release_id
        WHERE r.label=%(rel)s {_VER_GUARD} GROUP BY tr.test_name ORDER BY tr.test_name""".format(
            _VER_GUARD=_VER_GUARD
        ),
        p,
    )

    # 2. health-gate breakdown — test name, when, which build/version, observed
    #    value, threshold, ticket, detail.
    #    datetime: the TEST's start (falls back to the run's) — a gate_violation
    #    carries no timestamp of its own, and the test start is what lines a row up
    #    with the client debug log.
    #    ticket: aggregated in a sub-select, NOT a join — a build with two tickets
    #    would otherwise duplicate the breakdown row and inflate the gate count.
    #    Written only by `db.py issue set` (gate_ticket is keyed by job+build+test).
    c2, gate_bd = rows(
        cur,
        """
        SELECT gv.violation_type AS gate,
               tr.test_name AS triggered_by,
               run.jenkins_build AS build,
               COALESCE(to_char(COALESCE(tr.started_at, run.started_at),
                                'YYYY-MM-DD HH24:MI'), '-') AS datetime,
               COALESCE(run.product_version, '-') AS version,
               COALESCE((SELECT string_agg(DISTINCT gt.ticket_id, ', ')
                           FROM gate_ticket gt
                          WHERE gt.jenkins_job = run.jenkins_job
                            AND gt.jenkins_build = run.jenkins_build
                            AND (gt.test_name IS NULL OR gt.test_name = tr.test_name)),
                        '-') AS ticket,
               COALESCE(gv.process_name, '-') AS process,
               COALESCE(gv.observed::text, '-') AS observed,
               COALESCE(gv.threshold::text, '-') AS threshold,
               COALESCE(left(gv.detail, 120), '-') AS detail
        FROM gate_violation gv
        JOIN test_result tr ON tr.id=gv.test_result_id
        JOIN test_run run ON run.id=tr.run_id
        JOIN release r ON r.id=run.release_id
        WHERE r.label=%(rel)s {_VER_GUARD}
        ORDER BY gv.violation_type, run.jenkins_build DESC""".format(
            _VER_GUARD=_VER_GUARD
        ),
        p,
    )

    # 3. resource peaks per process
    c3, res = rows(
        cur,
        """
        SELECT ps.process_name AS process,
               round(avg(ps.cpu_max_pct),1) AS avg_cpu_peak,
               round(max(ps.cpu_max_pct),1) AS worst_cpu_peak,
               round(max(ps.cpu_sustained_s),0) AS worst_sustained_s,
               max(ps.cpu_threshold) AS cpu_gate,
               round(avg(ps.mem_growth_mb),1) AS avg_mem_growth,
               round(max(ps.mem_growth_mb),1) AS worst_mem_growth,
               max(ps.handles_max) AS worst_handles
        FROM process_summary ps JOIN test_result tr ON tr.id=ps.test_result_id
        JOIN test_run run ON run.id=tr.run_id JOIN release r ON r.id=run.release_id
        WHERE r.label=%(rel)s {_VER_GUARD} GROUP BY ps.process_name ORDER BY worst_cpu_peak DESC""".format(
            _VER_GUARD=_VER_GUARD
        ),
        p,
    )

    # 4. release summary stats (replaces Failures section)
    # 4a. pass-rate trend per test (pass% across all runs)
    c4a, pass_trend = rows(
        cur,
        """
        SELECT tr.test_name AS test,
               count(*) AS total_runs,
               round(100.0 * count(*) FILTER (WHERE tr.verdict='PASS') / count(*), 1) AS pass_rate_pct,
               sum(tr.iterations_total) AS total_iters,
               round(avg(tr.iterations_total),1) AS avg_iters_per_run,
               round(avg(tr.duration_s)/60.0, 1) AS avg_dur_min
        FROM test_result tr JOIN test_run run ON run.id=tr.run_id JOIN release r ON r.id=run.release_id
        WHERE r.label=%(rel)s {_VER_GUARD} GROUP BY tr.test_name ORDER BY pass_rate_pct ASC, total_runs DESC""".format(
            _VER_GUARD=_VER_GUARD
        ),
        p,
    )
    # 4b. version health overview — aggregate by product_version
    c4b, stressed = rows(
        cur,
        """
        SELECT run.product_version AS version,
               count(DISTINCT run.id) AS builds,
               sum(run.n_pass) AS total_pass,
               sum(run.n_fail) AS total_fail,
               count(DISTINCT gv.id) AS gate_hits,
               count(DISTINCT d.id) AS dumps
        FROM test_run run
        JOIN release r ON r.id=run.release_id
        LEFT JOIN test_result tr ON tr.run_id=run.id
        LEFT JOIN gate_violation gv ON gv.test_result_id=tr.id
        LEFT JOIN dump d ON d.test_result_id=tr.id
        WHERE r.label=%(rel)s AND run.product_version IS NOT NULL {_VER_GUARD}
        GROUP BY run.product_version
        ORDER BY version ASC""".format(_VER_GUARD=_VER_GUARD),
        p,
    )
    # 4c. iteration efficiency: total iters run, iters passed, pass rate
    c4c, iter_eff = rows(
        cur,
        """
        SELECT tr.test_name AS test,
               count(it.id) AS total_iters,
               count(it.id) FILTER (WHERE it.status='PASS') AS iters_pass,
               count(it.id) FILTER (WHERE it.status='FAIL') AS iters_fail,
               round(100.0 * count(it.id) FILTER (WHERE it.status='PASS') / NULLIF(count(it.id),0), 1) AS iter_pass_pct,
               round(avg(it.duration_s),1) AS avg_iter_s
        FROM iteration_result it
        JOIN test_result tr ON tr.id=it.test_result_id
        JOIN test_run run ON run.id=tr.run_id
        JOIN release r ON r.id=run.release_id
        WHERE r.label=%(rel)s {_VER_GUARD}
        GROUP BY tr.test_name ORDER BY total_iters DESC""".format(
            _VER_GUARD=_VER_GUARD
        ),
        p,
    )

    # 4d. iteration checkpoints — iter1 / iter10 (pass/total across ALL runs of
    # this release), the DEEPEST iteration ever reached + its verdict, and the
    # deepest iteration that was still a clean PASS. The last two can differ:
    # test_stress_08 reached iter 19 but iter 19 FAILED, its last clean pass was
    # iter 18 -- a single "max iter" column would hide that it got one rep PAST
    # its last good run before breaking. iter10 is "(undone)" wording, not a
    # blank/dash, for cases that have never run 10 reps yet (owner request
    # 2026-08-18: don't let "no data" read as a silent FAIL).
    c4d, iter_ckpt = rows(
        cur,
        """
        WITH deepest AS (
            -- Multiple runs commonly TIE on the deepest iter_index (e.g. 7 runs that
            -- each only ever reached iter 1) -- DISTINCT ON with no secondary key would
            -- pick an arbitrary one of those tied rows' status, which is exactly the
            -- ambiguity this column exists to resolve. Break ties by the most RECENT
            -- run (run.started_at, then tr.id) so "status at the deepest point" means
            -- the latest evidence, not whichever row the scan happened to see first.
            SELECT DISTINCT ON (tr.test_name)
                   tr.test_name AS test, it.iter_index AS max_iter, it.status AS status_at_max
            FROM iteration_result it
            JOIN test_result tr ON tr.id=it.test_result_id
            JOIN test_run run ON run.id=tr.run_id
            JOIN release r ON r.id=run.release_id
            WHERE r.label=%(rel)s {_VER_GUARD}
            ORDER BY tr.test_name, it.iter_index DESC, run.started_at DESC NULLS LAST, tr.id DESC
        ), agg AS (
            SELECT tr.test_name AS test,
                   count(*) FILTER (WHERE it.iter_index=1) AS n1,
                   count(*) FILTER (WHERE it.iter_index=1 AND it.status='PASS') AS n1_pass,
                   count(*) FILTER (WHERE it.iter_index=10) AS n10,
                   count(*) FILTER (WHERE it.iter_index=10 AND it.status='PASS') AS n10_pass,
                   max(it.iter_index) FILTER (WHERE it.status='PASS') AS max_pass_iter
            FROM iteration_result it
            JOIN test_result tr ON tr.id=it.test_result_id
            JOIN test_run run ON run.id=tr.run_id
            JOIN release r ON r.id=run.release_id
            WHERE r.label=%(rel)s {_VER_GUARD}
            GROUP BY tr.test_name
        )
        SELECT d.test, agg.n1, agg.n1_pass, agg.n10, agg.n10_pass,
               d.max_iter, d.status_at_max, agg.max_pass_iter
        FROM deepest d JOIN agg ON agg.test = d.test
        ORDER BY d.max_iter DESC, d.test""".format(_VER_GUARD=_VER_GUARD),
        p,
    )
    c4d_cols = [
        "Test case",
        "iter 1 (pass/total)",
        "iter 10 (pass/total)",
        "Deepest iteration reached",
        "Max PASS iteration",
    ]

    def _ckpt_table_html(data):
        # Custom render (not the shared table() helper, which escapes every cell):
        # the deepest-iteration column needs a raw <span class="dot p|f"> like the
        # iteration-sequence strips use, so a FAIL at the deepest point reads at a
        # glance the same way red/green already reads everywhere else in this report.
        th = "".join(f"<th>{esc(c)}</th>" for c in c4d_cols)
        trs = []
        for test, n1, n1p, n10, n10p, max_iter, status_max, max_pass in data:
            iter1 = f"{n1p}/{n1}"
            iter10 = (
                f"{n10p}/{n10}" if n10 else f"(undone — max iter reached is {max_iter})"
            )
            dot_cls = "p" if status_max == "PASS" else "f"
            deepest_html = (
                f'<span class="dot {dot_cls}" title="iter {esc(max_iter)}: {esc(status_max)}"></span> '
                f"iter {esc(max_iter)}: {esc(status_max)}"
            )
            rowcls = "warn" if status_max == "FAIL" else ""
            trs.append(
                f'<tr class="{rowcls}"><td>{esc(test)}</td><td>{esc(iter1)}</td>'
                f"<td>{esc(iter10)}</td><td>{deepest_html}</td><td>{esc(max_pass)}</td></tr>"
            )
        return (
            f'<table><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table>'
        )

    iter_ckpt_html = _ckpt_table_html(iter_ckpt) if iter_ckpt else ""

    # 5. iteration sequence — top 3 most-run cases, 3 in a row
    top_cases = [r[0] for r in sorted(per_test, key=lambda x: -(x[1] or 0))[:3]]
    seq_blocks = []
    for tc in top_cases:
        _, seq = rows(
            cur,
            """
            SELECT run.jenkins_build AS build, it.iter_index AS iter, it.status,
                   round(it.duration_s,1) AS dur_s, COALESCE(left(it.fail_reason,80),'') AS reason
            FROM iteration_result it JOIN test_result tr ON tr.id=it.test_result_id
            JOIN test_run run ON run.id=tr.run_id JOIN release r ON r.id=run.release_id
            WHERE r.label=%(rel)s AND tr.test_name=%(tc)s {_VER_GUARD}
            ORDER BY run.started_at, it.iter_index LIMIT 100""".format(
                _VER_GUARD=_VER_GUARD
            ),
            {**p, "tc": tc},
        )
        by_build = {}
        for b, i, st, d, rsn in seq:
            by_build.setdefault(b, []).append((st, rsn))
        strip = []
        for b, pairs in by_build.items():
            dots = "".join(
                '<span class="dot {cls}" title="build #{b} iter {i}: {s}{rsn}"></span>'.format(
                    cls="p" if s == "PASS" else "f",
                    b=b,
                    i=j + 1,
                    s=s,
                    rsn=(" — " + html.escape(r)) if r else "",
                )
                for j, (s, r) in enumerate(pairs)
            )
            total = len(pairs)
            npas = sum(1 for s, _ in pairs if s == "PASS")
            strip.append(
                f'<div class="seqrow">'
                f'<span class="seqbuild" title="build #{b}">#{b}</span>'
                f'<div class="seqdots">{dots}</div>'
                f'<span class="seqsummary">{npas}/{total}</span>'
                f"</div>"
            )
        short = tc.replace("test_", "").replace("_", " ")
        no_data = '<p class="foot">No iteration data.</p>'
        inner = "".join(strip) if strip else no_data
        seq_blocks.append(
            f'<div class="seqcase"><div class="seqtitle">{esc(short)}</div>{inner}</div>'
        )
    seq_html = ""
    if seq_blocks:
        grid = "".join(seq_blocks)
        seq_html = (
            '<div class="card"><h2>Iteration sequences — top 3 most-run cases</h2>'
            '<p class="foot">Each dot = one --iterations rep (green=PASS, red=FAIL). '
            "Hover for build/iter detail. Number after dots = pass/total for that build.</p>"
            f'<div class="seqgrid">{grid}</div></div>'
        )

    # 6. latest execution result per test case — one column per suite.
    # Group by test_name prefix (test_stress_* -> STRESS, test_power_* -> POWER)
    # rather than the suite column, so test_power_04 gets its own column instead
    # of being mixed into STRESS (the suite field mislabels it).
    c6, latest = rows(
        cur,
        """
        SELECT DISTINCT ON (tr.test_name)
               tr.test_name AS test,
               tr.verdict   AS verdict,
               run.jenkins_build AS build,
               run.started_at    AS started
        FROM test_result tr JOIN test_run run ON run.id=tr.run_id
        JOIN release r ON r.id=run.release_id
        WHERE r.label=%(rel)s {_VER_GUARD}
        ORDER BY tr.test_name, run.started_at DESC NULLS LAST, tr.id DESC""".format(
            _VER_GUARD=_VER_GUARD
        ),
        p,
    )

    def _suite_from_name(n):
        parts = n.split("_")
        return parts[1].upper() if len(parts) >= 2 and parts[0] == "test" else "OTHER"

    suites = {}
    for tname, verdict, build, started in latest:
        suites.setdefault(_suite_from_name(tname), []).append(
            (tname, verdict, build, started)
        )
    total_n = len(latest)
    total_p = sum(1 for _, v, _, _ in latest if v == "PASS")
    total_f = total_n - total_p
    suite_cols = []
    for suite in sorted(suites):
        items = suites[suite]
        np_ = sum(1 for _, v, _, _ in items if v == "PASS")
        nf = len(items) - np_
        rows_html = []
        for tname, verdict, build, started in sorted(items, key=lambda x: x[0]):
            cls = "p" if verdict == "PASS" else "f"
            bld = f"#{build}" if build else ""
            s_str = started.strftime("%Y-%m-%d %H:%M") if started else ""
            tip = f"build {bld} {verdict} {s_str}".strip()
            rows_html.append(
                f'<div class="latest-row">'
                f'<span class="lname" title="{esc(tip)}">{esc(tname)}</span>'
                f'<span class="ldot {cls}" title="{esc(tip)}"></span>'
                f"</div>"
            )
        suite_cols.append(
            f'<div class="latest-col">'
            f"<h3>{esc(suite)}</h3>"
            f'<div class="latest-meta">{np_} pass / {nf} fail &middot; {len(items)} cases</div>'
            f'{"".join(rows_html)}'
            f"</div>"
        )
    latest_html = (
        '<div class="card"><h2>Latest execution result</h2>'
        '<p class="foot">Most recent run per test case '
        "(green=PASS, red=FAIL). One column per suite. "
        f"{total_p} green / {total_f} red across {total_n} cases.</p>"
        f'<div class="latest-grid">{"".join(suite_cols)}</div></div>'
    )

    conn.close()

    # ---- assemble ----
    def dur_fmt(cols, data):
        # humanize *_dur_s columns
        out = []
        for r in data:
            rd = list(r)
            for idx, cname in enumerate(cols):
                if cname and cname.endswith("_dur_s") and rd[idx] is not None:
                    s = float(rd[idx])
                    rd[idx] = (
                        f"{int(s // 60)}m {int(s % 60)}s" if s >= 60 else f"{s:g}s"
                    )
            out.append(rd)
        return out

    per_test_disp = dur_fmt(c1, per_test)

    def test_hl(rd):
        if rd.get("fail", 0) and int(rd["fail"]) > 0:
            return "warn"
        if rd.get("gate_hits", 0) and int(rd["gate_hits"]) > 0:
            return "gatewarn"
        return ""

    gate_note = "No health-gate violations recorded." if not gate_bd else ""

    gate_legend = """
<div class="gate-legend">
  <strong>What is a health gate?</strong>
  Each SystemTest run continuously monitors the client while the test scenario runs.
  A <em>gate violation</em> means a monitored metric crossed a hard threshold —
  the test might still PASS (scenario logic succeeded) but the system was under abnormal stress.
  Gate types:
  <ul>
    <li><strong>cpu_sustained</strong> — process held CPU above threshold for too long
      (e.g. stAgentSvc &gt;120% for &gt;30 s). Indicates a storm or runaway loop.</li>
    <li><strong>crash_dump</strong> — a .dmp file found after the test; process crashed
      (access violation, stack overflow, etc.).</li>
    <li><strong>reboot</strong> — VM rebooted unexpectedly mid-test (BSOD or watchdog).
      Observed = actual boots; threshold = allowed maximum.</li>
    <li><strong>mem_growth / mem_ceiling</strong> — process RSS grew beyond the allowed MB,
      or exceeded an absolute ceiling.</li>
    <li><strong>handles</strong> — handle count grew past the ceiling, suggesting a handle leak.</li>
  </ul>
  <em>Observed</em> = measured value; <em>Threshold</em> = the limit that was crossed.
  <em>datetime</em> = when the test that hit the gate started (a violation carries no
  timestamp of its own); <em>version</em> = the client version installed in that build;
  <em>ticket</em> = the bug this row was filed as (<code>-</code> = not triaged yet),
  entered with <code>db.py issue set &lt;TICKET&gt; --build N</code>.
</div>"""

    body = f"""
<div class="card hero">
  <div class="header-row"><h1>SystemTest Report — {esc(short_release(release))}</h1></div>
  {filter_bar(release, platform, tenant, platform_options, tenant_options)}
  <div class="header-row">
  {f'<span class="badge warn" title="This view is filtered to one platform — other platforms runs are excluded.">Platform: {esc(platform)} only</span>' if platform else ''}
  {f'<span class="badge warn" title="This view is filtered to one tenant — other tenants runs are excluded.">Tenant: {esc(tenant)}</span>' if tenant else ''}
  <span class="badge {'fail' if n_fail else 'pass'}">{esc(n_fail)} test FAIL</span>
  <span class="badge {'warn' if n_gate else 'pass'}">{esc(n_gate)} gate hits</span>
  <span class="badge {'fail' if n_dump else 'pass'}">{esc(n_dump)} dumps</span></div>
  <dl class="meta-grid">
    <div><dt>Product version(s)</dt><dd>{esc(versions)}</dd></div>
    <div><dt>Tenant(s)</dt><dd>{esc(tenants)}</dd></div>
    <div><dt>Runs (builds)</dt><dd>{esc(n_runs)}</dd></div>
    <div><dt>Test executions</dt><dd>{esc(n_tests)}</dd></div>
    <div><dt>Raw resource samples</dt><dd>{esc(n_samples)}</dd></div>
  </dl>
</div>

<div class="card"><h2>Summary</h2><div class="kpi-row">
  {kpi("Pass rate", f"{pass_rate}%", f"{n_tests-n_fail}/{n_tests} executions")}
  {kpi("Test FAIL", n_fail)}
  {kpi("Health-gate hits", n_gate)}
  {kpi("Crash/BSOD dumps", n_dump)}
  {kpi("Distinct test cases", len(per_test))}
</div></div>

{latest_html}

<div class="card"><h2>Per test case</h2>
  <p class="foot">Sorted alphabetically. Runs, duration, pass/fail, and whether a health gate or dump fired.</p>
  <p class="legend">
    <span class="sw warn"></span> at least one run FAILED &nbsp;&nbsp;
    <span class="sw gatewarn"></span> PASSED but a health gate fired &nbsp;&nbsp;
    <span class="sw none"></span> all runs clean
  </p>
  {table(c1, per_test_disp, highlight=test_hl)}
</div>

<div class="card"><h2>Health-gate breakdown</h2>
  {gate_legend}
  <p class="foot" style="margin-top:10px">{gate_note}</p>
  {table(c2, gate_bd) if gate_bd else ''}
</div>

<div class="card"><h2>Resource peaks per process</h2>
  <div class="cpu-explain">
    <strong>How CPU peak is calculated:</strong>
    The sampler polls each process's CPU% every few seconds throughout the test run.
    <em>cpu_max_pct</em> is the highest single-sample reading observed.
    <strong>100 = one full CPU core</strong> (per-core scale, not percentage of total system CPU).
    So a value of 190 means the process was using ~1.9 CPU cores at its peak —
    on a 4-core VM that is ~47% of total system CPU, but reported as 190 here.
    <em>avg_cpu_peak</em> averages this peak across all runs; <em>worst_cpu_peak</em> is the single highest ever seen.
    <em>worst_sustained_s</em> = how many seconds the process was continuously above the gate threshold.
    <em>cpu_gate</em> = the threshold that triggers a gate violation if sustained for too long.
  </div>
  {table(c3, res)}
  <div class="chart-wrap"><canvas id="cpuChart"></canvas></div>
</div>

{seq_html}

<div class="card"><h2>Release summary — {esc(short_release(release))}</h2>
  <div class="summary-tabs">

    <div class="sumblock">
      <h3>Pass rate per test case</h3>
      <p class="foot">Pass% across all runs for this release, sorted lowest first (shows problem areas).</p>
      {table(c4a, pass_trend)}
    </div>

    <div class="sumblock">
      <h3>Version health overview</h3>
      <p class="foot">One row per product version: builds run, pass/fail counts,
        health-gate violations, crash dumps. Sorted by version ascending.</p>
      {table(c4b, stressed) if stressed else '<p class="foot">No build data.</p>'}
    </div>

    <div class="sumblock">
      <h3>Iteration efficiency</h3>
      <p class="foot">Total iterations actually executed per test case, with per-iteration pass rate.
        A test_result with 20 iterations contributes 20 rows here.
        avg_iter_s = average time per single iteration.
      </p>
      {table(c4c, iter_eff) if iter_eff else '<p class="foot">No iteration data.</p>'}
    </div>

    <div class="sumblock">
      <h3>Iteration checkpoints (iter 1 / iter 10 / deepest)</h3>
      <p class="foot">Per test case, aggregated across every run of this release. iter 1 / iter 10
        = pass/total count of runs that reached that rep. "(undone)" means this case has never
        run that many reps yet — not a FAIL, just not attempted. Deepest iteration reached shows
        the furthest rep ever hit and its verdict; Max PASS iteration is the deepest rep that was
        still clean. When they differ, the case got past its last good rep before breaking.</p>
      <p class="legend">
        <span class="dot p"></span> deepest reach was a PASS &nbsp;&nbsp;
        <span class="dot f"></span> deepest reach was a FAIL (went past its last clean iteration)
      </p>
      {iter_ckpt_html if iter_ckpt_html else '<p class="foot">No iteration data.</p>'}
    </div>

  </div>
</div>

"""

    # CPU chart data. A process with only-NULL cpu peaks (no living samples)
    # yields NULL from avg()/max() -> coerce to 0.0 so the chart never crashes.
    def _f(v):
        return float(v) if v is not None else 0.0

    labels = [r[0] for r in res]
    avg_cpu = [_f(r[1]) for r in res]
    worst_cpu = [_f(r[2]) for r in res]

    # Embed chart data as real JSON, not Python list repr — decouples the JS from
    # Python's str() format (e.g. None vs null, quote style) and is injection-safe.
    return HTML_TEMPLATE.format(
        release=esc(short_release(release)),
        body=body,
        labels=json.dumps(labels),
        avg_cpu=json.dumps(avg_cpu),
        worst_cpu=json.dumps(worst_cpu),
    )


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SystemTest Report — {release}</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {{
  --bg:          #05081a;
  --bg2:         #0a1628;
  --card:        #111827;
  --card2:       #1a2235;
  --border:      rgba(255,255,255,0.08);
  --border2:     rgba(0,199,230,0.2);
  --fg:          #f0f4ff;
  --muted:       #94a3b8;
  --accent:      #0052cc;
  --cyan:        #00c7e6;
  --grad:        linear-gradient(135deg,#0052cc 0%,#00c7e6 100%);
  --pass:        #10b981;
  --fail:        #ef4444;
  --warn:        #f59e0b;
  --pass-bg:     rgba(16,185,129,0.12);
  --fail-bg:     rgba(239,68,68,0.12);
  --warn-bg:     rgba(245,158,11,0.12);
  --pass-border: rgba(16,185,129,0.3);
  --fail-border: rgba(239,68,68,0.3);
  --warn-border: rgba(245,158,11,0.3);
  --radius:      12px;
  --radius-sm:   6px;
  --shadow:      0 4px 24px rgba(0,0,0,0.4);
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{
  font-family:'Inter','Helvetica Neue',sans-serif;
  background:var(--bg);
  color:var(--fg);
  font-size:16px;
  line-height:1.6;
  padding:0;
}}

/* ── top nav bar ── */
.topbar {{
  background:rgba(5,8,26,0.95);
  backdrop-filter:blur(12px);
  border-bottom:1px solid var(--border);
  padding:0 32px;
  height:56px;
  display:flex;
  align-items:center;
  gap:12px;
  position:sticky;
  top:0;
  z-index:100;
}}
.topbar .logo {{
  font-size:17px;
  font-weight:700;
  background:var(--grad);
  -webkit-background-clip:text;
  -webkit-text-fill-color:transparent;
  letter-spacing:.3px;
}}
.topbar .release-tag {{
  font-size:13px;
  padding:2px 10px;
  border-radius:999px;
  background:rgba(0,199,230,0.1);
  border:1px solid var(--border2);
  color:var(--cyan);
  font-weight:600;
  letter-spacing:.5px;
}}

/* ── page wrapper ── */
.page {{ max-width:1600px; margin:0 auto; padding:24px 28px 48px; }}

/* ── cards ── */
.card {{
  background:var(--card);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:20px 24px;
  margin-bottom:20px;
  box-shadow:var(--shadow);
}}
.card.hero {{
  background:linear-gradient(135deg,#0a1628 0%,#0d1f3c 100%);
  border-color:var(--border2);
}}

/* ── headings ── */
h1 {{ font-size:28px; font-weight:700; color:var(--fg); }}
h2 {{
  font-size:17px;
  font-weight:600;
  text-transform:uppercase;
  letter-spacing:.8px;
  color:var(--cyan);
  margin-bottom:14px;
  padding-bottom:8px;
  border-bottom:1px solid var(--border);
}}
h3 {{ font-size:16px; font-weight:600; color:var(--fg); margin-bottom:8px; }}

/* ── header row ── */
.header-row {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:16px; }}

/* ── filter bar (platform / tenant dropdowns) ── */
.filter-bar {{ display:flex; align-items:center; gap:14px; flex-wrap:wrap; margin-bottom:16px; }}
.filter-bar label {{ display:flex; align-items:center; gap:6px; font-size:13px; color:var(--muted); }}
.filter-bar select {{
  background:var(--card2); color:var(--fg); border:1px solid var(--border);
  border-radius:var(--radius-sm); padding:6px 10px; font-size:14px;
  font-family:'JetBrains Mono',Consolas,monospace;
}}
.filter-bar button {{
  background:var(--grad); color:#fff; border:none; border-radius:var(--radius-sm);
  padding:7px 18px; font-size:14px; font-weight:600; cursor:pointer;
}}
.badge {{
  padding:3px 12px;
  border-radius:999px;
  font-weight:600;
  font-size:13px;
  letter-spacing:.3px;
}}
.badge.pass {{ background:var(--pass-bg); color:var(--pass); border:1px solid var(--pass-border); }}
.badge.fail {{ background:var(--fail-bg); color:var(--fail); border:1px solid var(--fail-border); }}
.badge.warn {{ background:var(--warn-bg); color:var(--warn); border:1px solid var(--warn-border); }}

/* ── meta grid ── */
.meta-grid {{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
  gap:4px 24px;
}}
.meta-grid dt {{ color:var(--muted); font-size:13px; text-transform:uppercase; letter-spacing:.5px; }}
.meta-grid dd {{ font-family:'JetBrains Mono',Consolas,monospace; font-size:14px; color:var(--fg); }}

/* ── KPI row ── */
.kpi-row {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; }}
.kpi {{
  padding:14px 16px;
  background:var(--card2);
  border:1px solid var(--border);
  border-radius:var(--radius);
  position:relative;
  overflow:hidden;
}}
.kpi::before {{
  content:'';
  position:absolute;
  top:0; left:0; right:0;
  height:2px;
  background:var(--grad);
}}
.kpi .label {{
  font-size:12px;
  color:var(--muted);
  text-transform:uppercase;
  letter-spacing:.7px;
  font-weight:600;
}}
.kpi .value {{
  font-size:32px;
  font-weight:700;
  margin-top:6px;
  font-family:'JetBrains Mono',Consolas,monospace;
  background:var(--grad);
  -webkit-background-clip:text;
  -webkit-text-fill-color:transparent;
}}
.kpi .delta {{ font-size:13px; color:var(--muted); margin-top:2px; }}

/* ── tables ── */
table {{ width:100%; border-collapse:collapse; font-size:15px; table-layout:auto; }}
thead tr {{ background:rgba(255,255,255,0.03); }}
th {{
  text-align:left;
  padding:9px 14px;
  color:var(--muted);
  font-size:13px;
  text-transform:uppercase;
  letter-spacing:.7px;
  font-weight:600;
  border-bottom:1px solid var(--border);
  white-space:nowrap;
}}
td {{
  padding:8px 14px;
  border-bottom:1px solid rgba(255,255,255,0.04);
  font-family:'JetBrains Mono',Consolas,monospace;
  word-break:break-word;
  overflow-wrap:anywhere;
}}
tbody tr:hover {{ background:rgba(255,255,255,0.03); }}
tr.warn {{ background:var(--fail-bg) !important; }}
tr.warn td {{ border-bottom-color:var(--fail-border); }}
tr.gatewarn {{ background:var(--warn-bg) !important; }}
tr.gatewarn td {{ border-bottom-color:var(--warn-border); }}
/* Prevent long text cells (e.g. gate detail, test name) from blowing the table */
td:last-child {{ max-width:480px; }}
td:first-child {{ min-width:80px; }}

/* ── footnotes / legend ── */
.foot {{ color:var(--muted); font-size:14px; margin-top:8px; }}
.legend {{ font-size:14px; color:var(--muted); margin:6px 0 12px; display:flex; gap:16px; flex-wrap:wrap; }}
.legend .sw {{
  display:inline-block; width:10px; height:10px; border-radius:2px;
  vertical-align:middle; margin-right:4px;
}}
.legend .sw.warn {{ background:var(--fail); opacity:.7; }}
.legend .sw.gatewarn {{ background:var(--warn); opacity:.7; }}
.legend .sw.none {{ background:var(--muted); opacity:.4; }}

/* ── chart ── */
.chart-wrap {{ position:relative; height:260px; margin-top:20px; }}

/* ── iteration sequences ── */
.seqgrid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px; }}
@media(max-width:900px) {{ .seqgrid {{ grid-template-columns:1fr; }} }}
.seqcase {{
  background:var(--card2);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:14px 16px;
}}
.seqtitle {{
  font-weight:600;
  font-size:13px;
  text-transform:uppercase;
  letter-spacing:.5px;
  color:var(--cyan);
  margin-bottom:10px;
}}
.seqrow {{
  display:grid;
  grid-template-columns:46px 1fr 52px;
  align-items:start;
  gap:4px;
  margin:3px 0;
}}
.seqbuild {{
  font-family:'JetBrains Mono',Consolas,monospace;
  font-size:12px; color:var(--muted); padding-top:1px;
}}
.seqdots {{ display:flex; flex-wrap:wrap; gap:3px; }}
.seqsummary {{
  font-family:'JetBrains Mono',Consolas,monospace;
  font-size:12px; color:var(--muted);
  text-align:right; padding-top:1px; white-space:nowrap;
}}
.dot {{ width:10px; height:10px; border-radius:2px; display:inline-block; }}
.dot.p {{ background:var(--pass); }}
.dot.f {{ background:var(--fail); }}

/* ── latest execution result ── */
/* Columns size to their content (widest test name) instead of fixed thirds, so
   long names like test_stress_07_failclose_traffic_enforcement never wrap.
   flex-wrap flows overflow columns to the next row on a narrow viewport. */
.latest-grid {{ display:flex; flex-wrap:wrap; gap:16px; align-items:flex-start; }}
.latest-col {{
  background:var(--card2);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:14px 16px;
  flex:1 1 auto;
  min-width:max-content;
}}
.latest-col h3 {{
  color:var(--cyan);
  font-size:14px;
  text-transform:uppercase;
  letter-spacing:.8px;
  margin-bottom:4px;
  padding-bottom:8px;
  border-bottom:1px solid var(--border);
}}
.latest-meta {{ font-size:12px; color:var(--muted); margin-bottom:10px; }}
.latest-row {{
  display:flex;
  justify-content:space-between;
  align-items:center;
  padding:7px 0;
  border-bottom:1px solid rgba(255,255,255,0.04);
  font-family:'JetBrains Mono',Consolas,monospace;
  font-size:14px;
}}
.latest-row:last-child {{ border-bottom:none; }}
.latest-row .lname {{ color:var(--fg); white-space:nowrap; }}
.latest-row .ldot {{ width:12px; height:12px; border-radius:50%; flex-shrink:0; margin-left:16px; }}
.latest-row .ldot.p {{ background:var(--pass); box-shadow:0 0 6px rgba(16,185,129,0.4); }}
.latest-row .ldot.f {{ background:var(--fail); box-shadow:0 0 6px rgba(239,68,68,0.4); }}

/* ── info boxes ── */
.gate-legend {{
  background:rgba(0,199,230,0.05);
  border:1px solid var(--border2);
  border-radius:var(--radius-sm);
  padding:12px 16px;
  font-size:15px;
  margin-bottom:14px;
  color:var(--fg);
}}
.gate-legend ul {{ margin:8px 0 0 20px; padding:0; }}
.gate-legend li {{ margin:4px 0; color:var(--muted); }}
.gate-legend li strong {{ color:var(--fg); }}
.cpu-explain {{
  background:rgba(0,199,230,0.05);
  border:1px solid var(--border2);
  border-radius:var(--radius-sm);
  padding:12px 16px;
  font-size:15px;
  margin-bottom:16px;
  color:var(--muted);
  line-height:1.7;
}}
.cpu-explain strong {{ color:var(--fg); }}
.cpu-explain em {{ color:var(--cyan); font-style:normal; }}

/* ── summary section ── */
.summary-tabs {{ display:flex; flex-direction:column; gap:20px; }}
.sumblock {{
  background:var(--card2);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:16px 18px;
}}
.sumblock h3 {{ color:var(--fg); }}

/* ── footer ── */
.page-footer {{
  text-align:center;
  color:var(--muted);
  font-size:13px;
  padding:16px 0 8px;
  border-top:1px solid var(--border);
  margin-top:8px;
}}
.page-footer span {{
  background:var(--grad);
  -webkit-background-clip:text;
  -webkit-text-fill-color:transparent;
  font-weight:600;
}}
</style></head>
<body>
<nav class="topbar">
  <div class="logo">Netskope SystemTest</div>
  <div class="release-tag">{release}</div>
  <span class="badge warn" title="Client versions with major>=200 (feature builds) are not ingested by design.">feature builds excluded</span>
</nav>
<div class="page">
{body}
<div class="page-footer">
  Generated by <span>report.py</span> from the SystemTest results DB
  &middot; visualization only, not a CI gate.
</div>
</div>
<script>
Chart.defaults.color = '#94a3b8';
Chart.defaults.borderColor = 'rgba(255,255,255,0.06)';
new Chart(document.getElementById('cpuChart'), {{
  type:'bar',
  data:{{ labels:{labels}, datasets:[
    {{ label:'avg CPU peak', data:{avg_cpu},
       backgroundColor:'rgba(0,82,204,0.7)', borderColor:'#0052cc', borderWidth:1, borderRadius:4 }},
    {{ label:'worst CPU peak', data:{worst_cpu},
       backgroundColor:'rgba(239,68,68,0.7)', borderColor:'#ef4444', borderWidth:1, borderRadius:4 }}
  ]}},
  options:{{
    responsive:true, maintainAspectRatio:false,
    plugins:{{
      legend:{{ position:'bottom', labels:{{ boxWidth:12, padding:16 }} }},
      title:{{ display:true, text:'CPU peak per process (100 = 1 core)', color:'#94a3b8', font:{{size:12}} }}
    }},
    scales:{{
      x:{{ grid:{{ color:'rgba(255,255,255,0.04)' }}, ticks:{{ color:'#94a3b8' }} }},
      y:{{ beginAtZero:true, grid:{{ color:'rgba(255,255,255,0.04)' }}, ticks:{{ color:'#94a3b8' }} }}
    }}
  }}
}});
</script>
</body></html>
"""

# Lightweight page shell (topbar/CSS/footer, no Chart.js) for pages that have no
# chart to draw, e.g. query.py's /query. Sliced from HTML_TEMPLATE instead of a
# second copy of the CSS block, so a style change to the main report reaches
# every page built on this shell too. Still a plain .format(release=, body=)
# template (same {{ / }} escaping as HTML_TEMPLATE).
_page_head, _, _ = HTML_TEMPLATE.partition("<script>\nChart.defaults")
PAGE_TEMPLATE = _page_head + "</body></html>"
assert (
    "{body}" in PAGE_TEMPLATE and "{release}" in PAGE_TEMPLATE
), "HTML_TEMPLATE shape changed"


def main(argv):
    ap = argparse.ArgumentParser(
        description="Generate an HTML report from the SystemTest results DB."
    )
    ap.add_argument(
        "--release", default="release-140", help="release label (default release-140)"
    )
    ap.add_argument(
        "--out",
        default=None,
        help="output HTML path (default: claude-resource/systest_report/systest_report_<release>_<timestamp>.html)",
    )
    ap.add_argument(
        "--top", type=int, default=10, help="cap for large tables (default 10)"
    )
    ap.add_argument(
        "--tenant", default=None, help="filter to one tenant (run.tenant), e.g. 1457"
    )
    ap.add_argument(
        "--platform",
        default=None,
        choices=_PLATFORMS,
        help="filter to one platform (run.platform); a -mac/-windows/-linux "
        "suffix on --release does the same thing",
    )
    a = ap.parse_args(argv)

    html_out = build_report(a.release, a.top, a.tenant, a.platform)

    from datetime import datetime

    _REPORT_DIR = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "systest_report",
    )
    _REPORT_DIR = os.path.normpath(_REPORT_DIR)
    if a.out:
        out = a.out
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        short = a.release.replace("release-", "r")
        _subdir = os.path.join(_REPORT_DIR, a.release)
        os.makedirs(_subdir, exist_ok=True)
        out = os.path.join(_subdir, f"systest_report_{short}_{ts}.html")

    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(html_out)
    print(f"[ok] wrote {out} ({len(html_out)} bytes) for {a.release}")
    return 0


if __name__ == "__main__":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
