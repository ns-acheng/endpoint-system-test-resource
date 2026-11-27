#!/usr/bin/env python
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
import testname  # noqa: E402
from epais_builds import EPAIS_144_BUILDS  # noqa: E402


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


def table(
    cols, data, cls="", highlight=None, link_col=None, col_widths=None, merge_cols=None, collapse_dash_from=None, raw_cols=None
):
    """highlight: optional fn(row_dict) -> css class for the <tr>.
    raw_cols: column indexes whose cell is trusted HTML written by the caller (a link), shown as is instead of escaped.
    link_col: column index holding a test_name -- rendered via test_link_html
    (plain escaped text when no TestRail case is known) instead of plain esc.
    col_widths: optional dict of {col_index: "css style string"} applied to
    <th> and <td> via inline style to override browser auto-layout.
    merge_cols: leading column indexes whose repeated values are shown once (rowspan). A run in column c lasts while
    columns 0..c are all equal to the row above, so a differing value is never hidden behind a merged cell.
    collapse_dash_from: when every cell from this column on is "-", show one "-" spanning them instead of a row of dashes.
    """
    cw = col_widths or {}
    merge = sorted(merge_cols or [])
    span = {}  # (row index, column) -> rows the cell covers; 0 = covered by the cell above
    for c in merge:
        i = 0
        while i < len(data):
            j = i + 1
            while j < len(data) and tuple(data[j][: c + 1]) == tuple(data[i][: c + 1]):
                j += 1
            span[(i, c)] = j - i
            for k in range(i + 1, j):
                span[(k, c)] = 0
            i = j

    def _style(i):
        return f' style="{cw[i]}"' if i in cw else ""

    th = "".join(f"<th{_style(i)}>{esc(c)}</th>" for i, c in enumerate(cols))
    trs = []
    for n, r in enumerate(data):
        rd = dict(zip(cols, r))
        rowcls = highlight(rd) if highlight else ""
        if merge and (n == 0 or r[0] != data[n - 1][0]):
            rowcls = (rowcls + " grp").strip()
        tds = ""
        collapse = collapse_dash_from is not None and all(str(x) == "-" for x in r[collapse_dash_from:])
        for i, v in enumerate(r):
            if collapse and i >= collapse_dash_from:
                if i == collapse_dash_from:
                    tds += f'<td colspan="{len(r) - collapse_dash_from}">-</td>'
                continue
            s = span.get((n, i))
            if s == 0:
                continue
            attr = f' rowspan="{s}" class="mrg"' if s and s > 1 else (' class="mrg"' if s else "")
            cell = test_link_html(v) if i == link_col else (v if i in (raw_cols or ()) else esc(v))
            tds += f"<td{_style(i)}{attr}>{cell}</td>"
        trs.append(f'<tr class="{rowcls}">{tds}</tr>')
    return f'<table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table>'


def monitored_card(roster, seen, platform=None):
    """The card that lists EVERY process the sampler is configured to monitor, also those with no data in this release, so that
    everybody can see what is monitored (the human, 2026-11-27: "show all proc name in report even they do not have any data").

    roster: the dict of monitored.load() (None when the file is missing); seen: {(process, platform): (runs, tests)} of this
    release; platform: "windows" / "mac" / None. A process with data that is not on the roster is listed too, so a roster that
    fell behind the sampler is visible."""
    if not roster:
        return (
            '<div class="card"><h2>Monitored processes</h2><p class="foot">The roster file monitored_processes.json is missing or '
            "unreadable on this server, so the list of monitored processes cannot be shown.</p></div>"
        )
    observe = set(roster.get("observe_only") or [])
    ceilings = roster.get("mem_ceiling_mb") or {}
    cols = ["process", "platform", "gate", "memory ceiling", "runs with data", "tests with data", "status"]
    data = []
    for pf in ("windows", "mac"):
        if platform and platform != pf:
            continue
        names = list(roster.get(pf) or [])
        for name in names:
            runs, tests = seen.get((name, pf), (0, 0))
            data.append(
                [
                    name,
                    pf,
                    "observe-only" if name in observe else "judged",
                    f"{ceilings[name]} MB (first value)" if name in ceilings else "default",
                    runs,
                    tests,
                    "monitored" if runs else "monitored, no data in this release",
                ]
            )
        for (name, spf), (runs, tests) in sorted(seen.items()):
            if spf == pf and name not in names:
                data.append([name, pf, "-", "-", runs, tests, "has data, NOT on the roster (the roster file is behind the sampler)"])
    src = roster.get("source") or {}
    where = f"{src.get('ref', '?')} {str(src.get('sha', ''))[:8]}, synced {src.get('synced', '?')}"
    return (
        '<div class="card"><h2>Monitored processes</h2>'
        '<p class="foot">Every process the sampler is configured to read during a test, whether this release has data for it or not. '
        "<em>judged</em> processes can fail a run; <em>observe-only</em> ones are recorded and shown but never fail it. "
        '"no data in this release" means no sampled test saw the process running: it was not started, or the build under test does '
        f"not carry it. Roster file: {esc(where)}.</p>" + table(cols, data) + "</div>"
    )


# Product version filter. "ehf" is a token, not a version: an EHF build is X.0.N.build with N > 0 (143.0.2.2839);
# monthly builds are X.0.0.build and feature builds X.100.100.build. Any other value is one exact version.
EHF_TOKEN = "ehf"
EHF_RE = r"^\d+\.0\.[1-9]\d*\.\d+$"
# "non-ehf" keeps the monthly builds (X.0.0.build), the feature builds (X.100.100.build) and builds with no version, and drops every
# patch build: an EHF (X.0.N.build, N > 0) and the patch lines X.M.N.build with M = 1..99 (140.1.0.2781, 140.1.4.2805). Those two
# 140.1 builds do not match EHF_RE, yet they are patches of 140 and their numbers pull the cross-version charts.
NONEHF_TOKEN = "non-ehf"
NONEHF_DROP_RE = r"^\d+\.(0\.[1-9]\d*|[1-9]\d?\.\d+)\.\d+$"
VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")


# Known, accepted reasons for a memory step, by the major version where it appears (shown next to the measured step).
# R142: the NPA feature was enabled in that release; it raises client memory and the increase is accepted.
# The one CPU gate the report shows (per core, 100 = one core): a run fails when a gated process stays at or above it for 60 s in a row.
# Older runs were gated at 140 or 240 per test; their worst_sustained_s is restated against this value (see the resource query):
# a run whose highest reading is under CPU_GATE has 0 s above it by definition.
CPU_GATE = 280

MEM_KNOWN_CAUSES = {142: "the NPA feature was enabled in this release; higher memory is expected"}


def is_ehf(version):
    return bool(re.match(EHF_RE, version or ""))


def _version_sort_key(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def filter_bar(
    release,
    platform,
    tenant,
    stack,
    platform_options,
    tenant_options,
    stack_options,
    version=None,
    version_options=(),
):
    """Platform / tenant / product version / stack <select> dropdowns, GET-submitted to
    /report/<release> (short_release'd -- see short_release) as soon as one changes
    (one inline onchange; Apply only inside <noscript>). Whatever
    a viewer picks becomes the canonical ?platform=&tenant=&stack= query string
    on the SHORT URL, so the legacy long/suffix/path-segment forms (still
    accepted on input) never reappear once someone touches a dropdown.

    Stack and tenant are mutually exclusive in practice (see build_report's
    stack_tenants comment), but both dropdowns stay visible and independently
    selectable -- picking a stack is how you SEE every tenant in it at once;
    picking a specific tenant afterwards narrows back down to one.
    """

    def _opt(value, label, current):
        selected = " selected" if (value or None) == (current or None) else ""
        return f'<option value="{esc(value)}"{selected}>{esc(label)}</option>'

    plat_opts = _opt("", "All platforms", platform) + "".join(
        _opt(p, p.capitalize(), platform) for p in platform_options
    )
    tenant_opts = _opt("", "All tenants", tenant) + "".join(
        _opt(t, tenant_label(t), tenant) for t in tenant_options
    )
    stack_opts = _opt("", "All stacks", stack) + "".join(
        _opt(s, s, stack) for s in stack_options
    )
    version_opts = (
        _opt("", "All versions", version)
        + _opt(EHF_TOKEN, "EHF only", version)
        + _opt(NONEHF_TOKEN, "Non-EHF (monthly + feature builds)", version)
        + "".join(
            _opt(v, f"{v} (EHF)" if is_ehf(v) else v, version)
            for v in version_options
        )
    )
    return (
        # Picking a value reloads the page at once (change bubbles to the form); the Apply button stays only
        # inside <noscript> for a browser without JS.
        f'<form class="filter-bar" method="get" action="/report/{esc(short_release(release))}" '
        'onchange="this.submit()">'
        f'<label>Platform <select name="platform">{plat_opts}</select></label>'
        f'<label>Tenant <select name="tenant">{tenant_opts}</select></label>'
        f'<label>Product version <select name="version">{version_opts}</select></label>'
        f'<label>Stack <select name="stack">{stack_opts}</select></label>'
        '<noscript><button type="submit">Apply</button></noscript>'
        "</form>"
    )


_PLATFORMS = ("mac", "windows", "linux")

# tenant_id -> (name, host, stack). Hand-maintained from the GRS repo's
# golden_regression/test_environment/*.json files (tenants dict + env field) --
# this process deliberately does NOT import the GRS repo (see build_report's
# comment on ver_prefix), so this is a small, explicit snapshot rather than a
# live lookup. Extend it the same way when a new tenant shows up in run.tenant:
# find its name in the matching env json's "tenants" dict, host = name + "." +
# that file's "env" value, stack = whichever _STACKS label owns that env file.
TENANT_DIRECTORY = {
    "1118": ("nsclientauto10", "qa.boomskope.com", "qa01"),
    "1119": ("nsclientauto11", "qa.boomskope.com", "qa01"),
    # EPAIS work: QA tenant 1136 delivers the EPAIS policy; stg tenant 2508 is nsclientauto12.
    "1136": ("epdlp-tenant00", "qa.boomskope.com", "qa01"),
    "2508": ("nsclientauto12", "stg.boomskope.com", "stg01"),
    "1329": ("nsclientauto1", "stg.boomskope.com", "stg01"),
    "1331": ("nsclientauto3", "stg.boomskope.com", "stg01"),
    "1334": ("nsclientauto4", "stg.boomskope.com", "stg01"),
    "1340": ("nsclientauto9", "stg.boomskope.com", "stg01"),
    "1347": ("karthik", "stg.boomskope.com", "stg01"),
    "1457": ("nscauto7", "fed.boomskope.com", "fed.boomskope"),
    "8243": ("nscauto5", "stg01-mplegacy.boomskope.com", "mplegacy"),
    # nscsystest tenants (owner 2026-10-28; added to the env json by PR 583). There is no nscsystest3.
    "115615": ("nscsystest1", "stg.boomskope.com", "stg01"),
    "115635": ("nscsystest2", "stg.boomskope.com", "stg01"),
    "115660": ("nscsystest4", "stg.boomskope.com", "stg01"),
    "115689": ("nscsystest5", "stg.boomskope.com", "stg01"),
    "115714": ("nscsystest6", "stg.boomskope.com", "stg01"),
    "115715": ("nscsystest7", "stg.boomskope.com", "stg01"),
}
_STACKS = ("qa01", "stg01", "fed.boomskope", "mplegacy")

# tenant_id -> set of enabled add-on features, per release. Hand-maintained
# (same caveat as TENANT_DIRECTORY -- this is a snapshot, not a live lookup
# against whatever system actually owns tenant feature flags). Absent tenant /
# absent feature both mean "disabled", not "unknown" -- a tenant not listed here
# has every feature red, same as a listed tenant missing one name from its set.
#
# Structure: release -> tenant_id -> set of features
# Special key "_release-143+" applies to release-143 and later (>= 143).
FEATURE_LIST = (
    "NPA", "EPDLP", "DEM", "BWAN",
    "EPAIS", "SHUTTLE",
    "AI-GW", "AI-DISC", "AI-NTEL", "AI-GRL", "AI-LOOP",
)
# AI feature display names for the badge labels (key = internal name, value = what the badge shows).
FEATURE_DISPLAY = {
    "AI-GW":   "AI Model Routing",       # aig_in_dp
    "AI-DISC": "AI Discovery",          # enableEndpointAiAssetDiscovery
    "AI-NTEL": "AI Network Telemetry",  # enableAiAssetNetworkTelemetry
    "AI-GRL":  "AI Guardrail",          # enableEndpointAiGuardRail
    "AI-LOOP": "AI Loopback Intercept", # AIInterceptLoopback
}
TENANT_FEATURES = {
    # release-140/141: AI features assumed same as release-143 (tenant-side settings; R142- backups 404 on Jenkins).
    "release-140": {
        "1118": {"NPA"},
        "1119": set(),
        "1136": {"EPAIS"},
        "1331": {"DEM", "EPDLP", "AI-NTEL", "AI-GRL", "AI-LOOP"},
        "1334": {"DEM"},
        "1340": {"NPA", "DEM", "EPDLP", "EPAIS", "BWAN", "AI-DISC", "AI-NTEL", "AI-GRL", "AI-LOOP"},
        "1347": {"NPA"},
    },
    "release-141": {
        "1118": {"NPA"},
        "1119": set(),
        "1331": {"DEM", "EPDLP", "AI-NTEL", "AI-GRL", "AI-LOOP"},
        "1334": {"DEM"},
        "1340": {"NPA", "DEM", "EPDLP", "EPAIS", "BWAN", "AI-DISC", "AI-NTEL", "AI-GRL", "AI-LOOP"},
        "1347": {"NPA"},
        "1457": {"NPA", "DEM", "EPDLP", "AI-DISC", "AI-NTEL", "AI-GRL", "AI-LOOP"},
    },
    "release-142": {
        "1118": {"NPA"},
        "1119": set(),
        "1136": {"EPAIS"},
        "1331": {"DEM", "EPDLP", "AI-NTEL", "AI-GRL", "AI-LOOP"},
        "1334": {"DEM"},
        "1340": {"NPA", "DEM", "EPDLP", "EPAIS", "BWAN", "AI-DISC", "AI-NTEL", "AI-GRL", "AI-LOOP"},
        "1347": {"NPA"},
        "115615": {"EPDLP", "EPAIS", "AI-DISC", "AI-NTEL", "AI-GRL", "AI-LOOP"},
        "1457": {"NPA", "DEM", "EPDLP", "AI-DISC", "AI-NTEL", "AI-GRL", "AI-LOOP"},
    },
    # AI-DISC/NTEL/GRL/LOOP verified enabled in release-143 from nsconfig (1340=REG2/304 2026-10-30).
    # aig_in_dp (AI-GW) was false in R143 for tenant 1340; it moves to _release-144+.
    "_release-143+": {
        "1136": {"EPAIS"},
        "1331": {"DEM", "EPDLP", "NPA", "AI-NTEL", "AI-GRL", "AI-LOOP"},
        "1334": {"DEM"},
        "1340": {"NPA", "DEM", "EPDLP", "EPAIS", "BWAN", "AI-DISC", "AI-NTEL", "AI-GRL", "AI-LOOP"},
        "1347": {"NPA"},
        "115615": {"EPDLP", "EPAIS", "AI-DISC", "AI-NTEL", "AI-GRL", "AI-LOOP"},
        "1457": {"NPA", "DEM", "EPDLP", "AI-DISC", "AI-NTEL", "AI-GRL", "AI-LOOP"},
    },
    # release-144: AI-GW (aig_in_dp) added, SHUTTLE (mac client module) added.
    # Sources: 1340=REG2/446, 1331=REG/348, 115615=REG/345, 1457=REG/347.
    # 1136 config is DPAPI-encrypted; AI-GW/SHUTTLE detected from mac process data.
    "_release-144+": {
        "1340": {"SHUTTLE", "AI-GW"},
        "1331": {"AI-GW"},
        "115615": {"AI-GW"},
        "1457": {"AI-GW"},
    },
}

# spec case-id ("STRESS-01") -> TestRail case URL. Hand-maintained snapshot
# (owner-provided), kept as DATA in testrail_links.json next to this file --
# NOT a Python literal -- so adding/editing a link is a JSON edit, picked up
# on the next request via the mtime-checked cache below. No process restart.
# Mac and Windows variants of the same test (test_stress_01_connection_storm
# vs _mac) share ONE TestRail case, so the lookup key is derived from the
# test_name's group+number prefix only; the _mac suffix (if any) is never
# part of the key.
_TESTRAIL_LINKS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testrail_links.json")
_testrail_links_cache = {"mtime": None, "data": {}}


def _load_testrail_links():
    """Return the case-id -> URL dict, reloading from disk only when the
    JSON file's mtime has changed since the last call (cheap: one stat()
    per call on the healthy path, a tiny JSON parse only after an edit).
    Missing/unparseable file -> {} + a logged warning, never a crash --
    a bad edit should cost "no links today", not take the report down."""
    try:
        mtime = os.path.getmtime(_TESTRAIL_LINKS_PATH)
    except OSError as e:
        if _testrail_links_cache["mtime"] is not None:
            print(f"[testrail_links] {_TESTRAIL_LINKS_PATH} vanished: {e}", file=sys.stderr)
        _testrail_links_cache["mtime"] = None
        _testrail_links_cache["data"] = {}
        return _testrail_links_cache["data"]
    if mtime != _testrail_links_cache["mtime"]:
        try:
            with open(_TESTRAIL_LINKS_PATH, encoding="utf-8") as fh:
                _testrail_links_cache["data"] = json.load(fh)
            _testrail_links_cache["mtime"] = mtime
        except (OSError, ValueError) as e:
            print(f"[testrail_links] failed to reload {_TESTRAIL_LINKS_PATH}: {e}", file=sys.stderr)
            # Keep serving the last-good data rather than going blank on a typo.
    return _testrail_links_cache["data"]


# Verdict column of the "Crash / BSOD dumps" table (owner 2026-11-21): who looked at a dump and what it is. Hand-maintained DATA in
# crash_verdicts.json next to this file, keyed "<lane>:<build>" (the lane as the table shows it, for example "LOCAL1:184"), so one
# JSON edit sets the verdict of every dump row of that build. A build with no entry shows a blank cell. Same reload rule as above:
# edited on disk, picked up on the next request, a bad edit keeps the last good data.
_CRASH_VERDICTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash_verdicts.json")
_crash_verdicts_cache = {"mtime": None, "data": {}}


def _load_crash_verdicts():
    try:
        mtime = os.path.getmtime(_CRASH_VERDICTS_PATH)
    except OSError:
        _crash_verdicts_cache["mtime"] = None
        _crash_verdicts_cache["data"] = {}
        return _crash_verdicts_cache["data"]
    if mtime != _crash_verdicts_cache["mtime"]:
        try:
            with open(_CRASH_VERDICTS_PATH, encoding="utf-8") as fh:
                _crash_verdicts_cache["data"] = json.load(fh)
            _crash_verdicts_cache["mtime"] = mtime
        except (OSError, ValueError) as e:
            print(f"[crash_verdicts] failed to reload {_CRASH_VERDICTS_PATH}: {e}", file=sys.stderr)
    return _crash_verdicts_cache["data"]


_DUMP_PATH_RE = re.compile(r"'[^']+\.(?:ips|dmp)'")


def dedup_dump_paths(rows, lane_i=2, build_i=3, detail_i=6):
    """The crash table lists the dump files a run saw. A crash report left by an earlier run of the same lane shows up again in the
    next run's bundle, so the same path is listed under several builds. Walk each lane from the oldest build to the newest, keep a
    path under the first build that lists it and drop it from the later ones (mac2 build 362 listed five paths of build 361).
    Returns new rows; the detail text outside the path list is kept, and a row with no new path says so."""
    out = [list(r) for r in rows]

    def build_no(i):
        b = str(out[i][build_i])
        return int(b) if b.isdigit() else 0

    seen = set()
    for i in sorted(range(len(out)), key=lambda i: (str(out[i][lane_i]), build_no(i))):
        detail = str(out[i][detail_i])
        found = list(_DUMP_PATH_RE.finditer(detail))
        if not found:
            continue
        lane = str(out[i][lane_i])
        kept = [m.group(0) for m in found if (lane, m.group(0)) not in seen]
        for m in found:
            seen.add((lane, m.group(0)))
        if len(kept) == len(found):
            continue
        start, end = found[0].start(), found[-1].end()
        text = detail[:start] + ", ".join(kept) + detail[end:]
        if not kept:
            text += " -- every path is already listed under an earlier build of this lane"
        out[i][detail_i] = text
    return out


_TESTNAME_CASE_RE = re.compile(r"^test_([a-z0-9]+)_(\d+)_")


def testrail_url_for_test(test_name):
    """'test_stress_01_connection_storm' or the '_mac' variant of the same
    case -> 'STRESS-01' -> testrail_links.json. Returns None (no link, not a
    broken one) for any test not in the hand-maintained snapshot above."""
    if not test_name:
        return None
    m = _TESTNAME_CASE_RE.match(test_name)
    if not m:
        return None
    key = f"{m.group(1).upper()}-{m.group(2)}"
    return _load_testrail_links().get(key)


def test_link_html(test_name, display=None):
    """Escaped test-name text, wrapped in an <a class="tname"> when a
    TestRail case is known. `.tname` CSS forces color:inherit and
    text-decoration:none so a linked case renders pixel-identical to an
    unlinked one -- the link is only discoverable by clicking or hovering,
    never by a color/underline change."""
    text = esc(display if display is not None else test_name)
    url = testrail_url_for_test(test_name)
    if not url:
        return text
    return (
        f'<a class="tname" href="{esc(url)}" target="_blank" rel="noopener">{text}</a>'
    )


# Process names (as seen in process_summary) that indicate a feature is enabled on the tenant.
# Checked against the last mac run so they cover mac-only features (SHUTTLE) and cross-platform
# ones (EPAIS); Windows-only features (NPA, EPDLP, BWAN, DEM) stay in TENANT_FEATURES for now.
_FEATURE_PROCESS_MAP = {
    "EPAIS": "MacOS/epais",
    "SHUTTLE": "/shuttle",
}


def _features_from_last_mac_run(cur, tid, release):
    """Return the set of features whose processes were alive in the last mac run of this tenant+release.
    Returns an empty set when no mac run exists."""
    cur.execute(
        """
        SELECT ps.process_name
        FROM process_summary ps
        JOIN test_result tres ON ps.test_result_id = tres.id
        JOIN test_run tr ON tres.run_id = tr.id
        WHERE tr.tenant = %s
          AND tr.release_id = (SELECT id FROM release WHERE label = %s)
          AND tr.platform = 'mac'
          AND ps.alive_pct > 0
          AND tr.id = (
            SELECT id FROM test_run
            WHERE tenant = %s
              AND release_id = (SELECT id FROM release WHERE label = %s)
              AND platform = 'mac'
            ORDER BY started_at DESC
            LIMIT 1
          )
        """,
        (str(tid), release, str(tid), release),
    )
    rows = {row[0] for row in cur.fetchall()}
    found = set()
    for feature, proc_prefix in _FEATURE_PROCESS_MAP.items():
        if any(r.startswith(proc_prefix) for r in rows):
            found.add(feature)
    return found


def feature_badges_html(tid, release, cur=None):
    """One .badge pill per FEATURE_LIST entry -- same pass/fail/warn pill used
    for 'NN test FAIL' / 'NN gate hits' elsewhere on this page, not a bespoke
    dot+label style.

    Static features (NPA, DEM, EPDLP, BWAN) come from TENANT_FEATURES.
    Dynamic features (EPAIS, SHUTTLE) are detected from the last mac run's
    process_summary when a DB cursor is supplied; falls back to TENANT_FEATURES."""
    match = re.search(r"release-(\d+)", release or "")
    rel_num = int(match.group(1)) if match else 0

    # Static lookup for NPA/DEM/EPDLP/BWAN: exact match first, then _release-143+ for 143+
    if release in TENANT_FEATURES:
        tenant_map = TENANT_FEATURES[release]
    elif rel_num >= 143:
        tenant_map = TENANT_FEATURES.get("_release-143+", {})
        if rel_num >= 144:
            extra = TENANT_FEATURES.get("_release-144+", {})
            tenant_map = {t: tenant_map.get(t, set()) | extra.get(t, set()) for t in {*tenant_map, *extra}}
    else:
        tenant_map = {}

    enabled = set(tenant_map.get(str(tid), set()))

    # Dynamic detection from mac process data overrides static entries for EPAIS and SHUTTLE
    if cur is not None:
        try:
            mac_features = _features_from_last_mac_run(cur, tid, release)
            # Only override the dynamically-detected features; keep static for the rest
            for f in _FEATURE_PROCESS_MAP:
                enabled.discard(f)
            enabled |= mac_features
        except Exception:
            pass  # DB error: fall back to static data already in enabled

    n_on = sum(1 for f in FEATURE_LIST if f in enabled)
    n_total = len(FEATURE_LIST)
    pill_cls = "feat-pill-all" if n_on == n_total else ("feat-pill-part" if n_on > 0 else "feat-pill-none")

    # Build grouped panel rows
    FEAT_GROUPS = [
        ("Core",     ["NPA", "EPDLP", "DEM", "BWAN"]),
        ("Endpoint", ["EPAIS", "SHUTTLE"]),
        ("AI",       ["AI-GW", "AI-DISC", "AI-NTEL", "AI-GRL", "AI-LOOP"]),
    ]
    rows = []
    for grp_name, members in FEAT_GROUPS:
        tags = "".join(
            f'<span class="ftag ftag-{"on" if m in enabled else "off"}">'
            f'<span class="fdot"></span>{esc(FEATURE_DISPLAY.get(m, m))}</span>'
            for m in members
        )
        rows.append(f'<div class="fgrp"><span class="fgrp-name">{esc(grp_name)}</span><div class="ftag-row">{tags}</div></div>')
    panel_body = "".join(rows)

    # Always-visible panel — same layout as before (vertical ftag columns), no click required.
    return f'<div class="fpanel-always">{panel_body}</div>'


VERSIONS_SHOWN = 11


def versions_header_html(version_rows):
    """Header 'Product version(s)' cell: the first VERSIONS_SHOWN inline, then a '+N more' link that opens a
    <dialog> listing every version with its builds and pass/fail. No link when everything fits."""
    if not version_rows:
        return "n/a"
    shown = ", ".join(esc(v[0]) for v in version_rows[:VERSIONS_SHOWN])
    hidden = len(version_rows) - VERSIONS_SHOWN
    if hidden <= 0:
        return shown
    trs = "".join(
        f"<tr><td>{esc(v)}</td><td>{esc(n)}</td><td>{esc(ok)} / {esc(bad)}</td></tr>" for v, n, ok, bad in version_rows
    )
    # Non-modal dialog placed under the link (showModal() would centre it in the window, and a bare dialog with the
    # page's reset margin lands at 0,0). Outside click and Esc close it.
    opener = (
        "var d=document.getElementById('verpanel'),r=this.getBoundingClientRect();"
        "if(d.open){d.close();return false}"
        "d.show();d.style.position='absolute';d.style.margin='0';d.style.right='auto';"
        "d.style.top=(r.bottom+window.scrollY+6)+'px';"
        "d.style.left=Math.max(8,Math.min(r.left+window.scrollX,window.scrollX+document.documentElement.clientWidth-d.offsetWidth-8))+'px';"
        "return false"
    )
    closer = (
        "document.addEventListener('click',function(e){var d=document.getElementById('verpanel');"
        "if(d&&d.open&&!d.contains(e.target)&&!e.target.closest('a.more'))d.close()});"
        "document.addEventListener('keydown',function(e){var d=document.getElementById('verpanel');"
        "if(e.key==='Escape'&&d&&d.open)d.close()})"
    )
    return (
        f'{shown} <a class="badge warn guide more" href="#" onclick="{opener}">+{hidden} more &rarr;</a>'
        '<dialog id="verpanel">'
        f'<div class="dh"><b>All product versions ({len(version_rows)})</b>'
        '<button type="button" aria-label="close" onclick="this.closest(\'dialog\').close()">&times;</button></div>'
        '<div class="dl"><table><thead><tr><th>Version</th><th>Runs</th><th>Pass / Fail</th></tr></thead>'
        f"<tbody>{trs}</tbody></table></div></dialog>"
        f"<script>{closer}</script>"
    )


def numbers_help_html(n_runs, run_clean, run_unstable, run_fail, run_gate, n_dump, pass_rate, is_final):
    """The 'How to read the numbers' pill: same look and behaviour as the '+N more' version link, opening a floating panel that
    explains every Summary number with this report's own values."""
    opener = (
        "var d=document.getElementById('numpanel'),r=this.getBoundingClientRect();"
        "if(d.open){d.close();return false}"
        "d.show();d.style.position='absolute';d.style.margin='0';d.style.right='auto';"
        "d.style.top=(r.bottom+window.scrollY+6)+'px';"
        "d.style.left=Math.max(8,Math.min(r.left+window.scrollX,window.scrollX+document.documentElement.clientWidth-d.offsetWidth-8))+'px';"
        "return false"
    )
    closer = (
        "document.addEventListener('click',function(e){var d=document.getElementById('numpanel');"
        "if(d&&d.open&&!d.contains(e.target)&&!e.target.closest('a.more'))d.close()});"
        "document.addEventListener('keydown',function(e){var d=document.getElementById('numpanel');"
        "if(e.key==='Escape'&&d&&d.open)d.close()})"
    )
    final_note = (
        "<p>This is a <b>-final</b> snapshot. Every PASS of the source release is kept. Only failing results are left out, each with a "
        "stated reason (setup or tooling noise, or the same test passed later on the same lane).</p>"
        if is_final
        else ""
    )
    body = (
        "<p>Every number in the Summary counts <b>Jenkins runs</b>. One run is one Jenkins build of one lane, and it gets exactly one result:</p>"
        "<table><thead><tr><th>Result</th><th>Meaning</th><th>Runs</th></tr></thead><tbody>"
        f"<tr><td>FAIL</td><td>at least one test in the run failed</td><td>{esc(run_fail)}</td></tr>"
        "<tr><td>Unstable</td><td>no test failed, but a health gate fired (crash dump, handle or memory growth), so Jenkins marks the build UNSTABLE</td>"
        f"<td>{esc(run_unstable)}</td></tr>"
        f"<tr><td>Clean PASS</td><td>no failure and no gate hit</td><td>{esc(run_clean)}</td></tr>"
        f"<tr><td><b>Total</b></td><td>{esc(run_clean)} + {esc(run_unstable)} + {esc(run_fail)}</td><td><b>{esc(n_runs)}</b></td></tr></tbody></table>"
        f"<p><b>Pass rate</b> = clean PASS runs / runs = {esc(run_clean)} / {esc(n_runs)} = {esc(pass_rate)}%.</p>"
        f"<p><b>Runs with a gate hit</b> ({esc(run_gate)}) = every Unstable run plus the FAIL runs that also fired a gate.</p>"
        f"<p><b>Dump files</b> ({esc(n_dump)}) = distinct dump files named by the gate; one test can account for several.</p>"
        "<p><b>Distinct test cases</b> counts different test names, not runs.</p>" + final_note
    )
    return (
        f'<a class="badge warn guide more" href="#" onclick="{opener}">How to read the numbers &rarr;</a>'
        '<dialog id="numpanel"><div class="dh"><b>How to read the numbers</b>'
        "<button type=\"button\" aria-label=\"close\" onclick=\"this.closest('dialog').close()\">&times;</button></div>"
        f'<div class="dl">{body}</div></dialog><script>{closer}</script>'
    )


def tenant_label(tid):
    """'1347' ->'1347 karthik.stg.boomskope.com'; unknown tenant -> just the
    bare id (never hide a real tenant because the directory hasn't caught up)."""
    if not tid:
        return tid
    ent = TENANT_DIRECTORY.get(str(tid))
    return f"{tid} {ent[0]}.{ent[1]}" if ent else str(tid)


def tenants_in_stack(stack):
    """All known tenant ids whose TENANT_DIRECTORY stack matches. Returns []
    (not None) for an unknown stack -- callers treat that as '(no match)', not
    '(no filter)'."""
    return [tid for tid, (_, _, s) in TENANT_DIRECTORY.items() if s == stack]


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


def _build_report_raw(release, top, tenant=None, platform=None, stack=None, version=None):
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
    stack = stack or None
    # version: None = all, "ehf" = every EHF build, anything else = that exact product_version
    version = version or None
    ehf_re = EHF_RE if version == EHF_TOKEN else None
    nonehf_re = NONEHF_DROP_RE if version == NONEHF_TOKEN else None
    exact_version = None if version in (None, EHF_TOKEN, NONEHF_TOKEN) else version
    # A stack filter means "every tenant in this stack", so it REPLACES a
    # single-tenant filter rather than combining with it (an AND of the two
    # would almost always match nothing, since tenant already implies exactly
    # one stack). tenants_in_stack([]) for an unrecognized stack deliberately
    # matches zero rows (= ANY('{}') is empty), not "no filter" -- the caller
    # asked for a stack we don't know about, not for everything.
    stack_tenants = tenants_in_stack(stack) if stack else None
    if stack_tenants is not None:
        tenant = None
    _m = re.search(r"(\d+)", release or "")
    _major = int(_m.group(1)) if _m else None
    p = {
        "rel": release,
        "ver_prefix": f"{_major}.%" if _major is not None else "%",
        "report_major": str(_major) if _major is not None else None,
        "platform": platform,
        "tenant": tenant,
        "stack_tenants": stack_tenants,
        "version": exact_version,
        "ehf_re": ehf_re,
        "nonehf_re": nonehf_re,
    }
    # FINAL VIEW: a "-final" release is a curated cut of its version (release-143-final holds 258 of release-143's 404
    # runs). The cross-version trends do not look at the page's release label, so on a -final page they showed ALL runs next
    # to a header counting only the final cut. In the final view only runs that sit in a "-final" release count: a major that
    # HAS one uses only its own release-<N>-final runs, and a major without a final cut (R140) is left out of the trends. The
    # old rule let such a major keep all its runs, which put the 140.1.x patch builds on the 144-final charts.
    _final_view = (release or "").endswith("-final")
    cur.execute(
        "SELECT DISTINCT substring(label from '^release-(\\d+)-final$') FROM release WHERE label ~ '^release-\\d+-final$'"
    )
    p["final_view"] = _final_view
    p["final_majors"] = sorted(int(r[0]) for r in cur.fetchall() if r[0])

    def _final_guard(run="run", rel="r"):
        major_sql = f"(substring({run}.product_version from '^(\\d+)'))"
        return (
            " AND (NOT %(final_view)s"
            f" OR {rel}.label = 'release-' || {major_sql} || '-final'"
            # a build whose own major has no final cut (a feature build, 211.x) still counts when it sits in some final release
            f" OR (NOT ({major_sql}::int = ANY(%(final_majors)s::int[])) AND {rel}.label ~ '^release-\\d+-final$'))"
        )

    _FINAL_GUARD = _final_guard()
    if ehf_re and _major is not None:
        # EHF view: the cross-version trends compare client MAJORS, and "ehf" matches the EHF builds of every major, so the
        # 143 page also plotted an R140 EHF point (140.0.2.2758). An EHF page belongs to one release: its trend charts keep
        # only this release's major (the 143 page: 143.0.N.x; a future 144 page: 144.0.N.x), i.e. one point.
        _FINAL_GUARD += f" AND (substring(run.product_version from '^(\\d+)'))::int = {int(_major)}"

    # Optional tenant/stack scope (e.g. one tenant's, or one stack's, SystemTest
    # history): appended to every guard below so a single param threads through
    # all 16 query sites instead of touching each one. %(tenant)s IS NULL /
    # %(stack_tenants)s IS NULL short-circuits each OR to a no-op when that
    # filter is not set — same trick _VER_GUARD already uses for ver_prefix,
    # not a new pattern.
    _TENANT_GUARD = (
        " AND (%(tenant)s IS NULL OR run.tenant=%(tenant)s)"
        " AND (%(stack_tenants)s IS NULL OR run.tenant = ANY(%(stack_tenants)s))"
        " AND (%(version)s IS NULL OR run.product_version = %(version)s)"
        " AND (%(ehf_re)s IS NULL OR run.product_version ~ %(ehf_re)s)"
        " AND (%(nonehf_re)s IS NULL OR run.product_version IS NULL OR run.product_version !~ %(nonehf_re)s)"
    )
    # A feature build (major >= 200, e.g. 211.x) is shown on the page of the release it was filed under: "we do not exclude feature
    # build result" (2026-11-03). Its synthetic major never matches the page's own major, so it is let through explicitly.
    _VER_GUARD = (
        "AND (run.product_version IS NULL OR run.product_version LIKE %(ver_prefix)s"
        " OR (substring(run.product_version from '^(\\d+)'))::int >= 200"
        # The EPAIS builds named in epais_builds.EPAIS_144_BUILDS (142.100.100.10608 ...) are shown on the R144 page whatever their
        # version string starts with: the prefix test above hid all 114 of their results. Only the listed builds, not every feature build.
        # 142.100.100.10500 is the same special case the cross-version charts already treat as R144 (_SPECIAL_144_BUILDS below); leaving
        # it out of this page counted its tickets (ENG-1403860) but not its run, so the Runs number and the Tickets table disagreed.
        " OR run.product_version IN ("
        + ", ".join(f"'{b}'" for b in dict.fromkeys(("142.100.100.10500",) + tuple(EPAIS_144_BUILDS)))
        + "))"
        + _TENANT_GUARD
    )
    if platform:
        _VER_GUARD += " AND run.platform=%(platform)s"
    _PLAT_ONLY = ("AND run.platform=%(platform)s" if platform else "") + _TENANT_GUARD
    # An orphan gate_ticket (no matching test_run -- the build died before
    # pytest ever ran) has no run.platform to check against, so a platform
    # filter falls back to the only signal an orphan carries: its own
    # jenkins_job name. Every SystemTest lane is either MAC1/MAC2 (mac) or
    # everything else (windows) -- confirmed against test_run's distinct
    # (platform, jenkins_job) pairs, there is no other lane naming today.
    # Without this, an orphan ticket from a Windows-only lane (e.g. LOCAL2)
    # bled into every platform filter, including mac.
    if platform == "mac":
        _ORPHAN_PLAT = " AND gt.jenkins_job ~* 'MAC'"
    elif platform:
        _ORPHAN_PLAT = " AND gt.jenkins_job !~* 'MAC'"
    else:
        _ORPHAN_PLAT = ""
    RUN_J = "test_run run JOIN release r ON r.id=run.release_id"
    TR_J = "test_result tr JOIN test_run run ON run.id=tr.run_id JOIN release r ON r.id=run.release_id"

    # A platform-mismatched sibling wrapper (e.g. a _mac test collected on a
    # windows run, or vice versa) never executed for real — it must not count
    # as a run, inflate any table, or show up anywhere per-test-case. This
    # was originally guarded by "verdict <> SKIP" alone (the wrapper is
    # supposed to always record SKIP), but a real ingest bug (build 181,
    # 2026-09-02, test_stress_11_dns_tcp_large_packet_mac on REG-02/windows)
    # recorded FAIL instead of SKIP for the mismatched sibling, leaking a
    # bogus mac row into every windows-filtered table. Guard on the actual
    # invariant instead: test_name's own _mac suffix must agree with
    # run.platform, independent of whatever verdict ingest happened to
    # record. n_runs counts SESSIONS (there is no `tr` alias in RUN_J's FROM
    # clause, and a session itself has no test_name/verdict), so it keeps the
    # pre-guard version; every per-test-case query below (all of which DO
    # have `tr` in scope) gets the full clause because they read `_VER_GUARD`
    # after this line.
    _RUN_GUARD = _VER_GUARD
    _VER_GUARD = (
        _RUN_GUARD
        + " AND tr.verdict <> 'SKIP'"
        + " AND "
        + testname.platform_guard_sql("tr.test_name", "run.platform")
    )

    def count_where(frm, extra="", guard=None):
        return scalar(
            cur,
            f"SELECT count(*) FROM {frm} WHERE r.label=%(rel)s {guard or _VER_GUARD} {extra}",
            p,
        )

    # Dump count = DISTINCT file paths named in a real gate_violation (the health
    # gate actually flagged it), NOT test_result.has_dump and NOT a raw count of
    # `dump` table rows. Both of those over-count: has_dump/the dump table record
    # every dump the out-of-band scan ever SAW, including a stale file left on
    # disk from an earlier build that a later build's scan re-detects (same
    # file_path, same captured_at) without it being a new incident, and dumps
    # the gate itself never considered violation-worthy (e.g. a background
    # stAgentSvc.dmp with no fresh BugCheck behind it). The gate_violation
    # detail text ("BSOD dumps present: [...]" / "crash dumps present: [...]")
    # is the one signal that only fires for a dump the gate treated as real.
    # RCA (142-final, 2026-09-30): 091526-15750-01.dmp/091526-16203-01.dmp were
    # correctly flagged once (build 204); has_dump additionally marked build
    # 208's re-detection of the SAME two files as a second "has dump" row, and
    # the `dump` table separately held a stAgentSvc.dmp/crash pair the gate
    # never violated on -- neither belongs in the count.
    _dump_cols, _dump_rows = rows(
        cur,
        f"""SELECT tr.test_name, run.product_version, gv.detail
            FROM gate_violation gv
            JOIN test_result tr ON tr.id=gv.test_result_id
            JOIN test_run run ON run.id=tr.run_id
            JOIN release r ON r.id=run.release_id
            WHERE r.label=%(rel)s {_VER_GUARD}
              AND gv.violation_type IN ('bsod_dump','crash_dump')
              AND position('__FAKE__' in COALESCE(gv.detail, '')) = 0""",
        p,
    )
    _dump_files_all = set()
    _dump_files_by_test = {}
    _dump_files_by_version = {}
    for _test_name, _version, _detail in _dump_rows:
        _paths = re.findall(r"'([^']+)'", _detail or "")
        _dump_files_all.update(_paths)
        _dump_files_by_test.setdefault(_test_name, set()).update(_paths)
        _dump_files_by_version.setdefault(_version, set()).update(_paths)
    n_dump = len(_dump_files_all)

    # Header "Product version(s)": one row per version with its builds and verdict counts, ascending.
    # The first VERSIONS_SHOWN go inline; the rest sit behind a "+N more" panel that lists them all.
    _, _vrun_rows = rows(
        cur,
        f"SELECT run.product_version, count(DISTINCT run.id) FROM {RUN_J} "
        f"WHERE r.label=%(rel)s AND run.product_version IS NOT NULL {_PLAT_ONLY} GROUP BY run.product_version",
        p,
    )
    _, _vres_rows = rows(
        cur,
        f"SELECT run.product_version, "
        f"count(*) FILTER (WHERE tr.verdict='PASS'), count(*) FILTER (WHERE tr.verdict='FAIL') "
        f"FROM {TR_J} WHERE r.label=%(rel)s AND run.product_version IS NOT NULL {_PLAT_ONLY} "
        f"AND tr.verdict <> 'SKIP' AND {testname.platform_guard_sql('tr.test_name', 'run.platform')} "
        f"GROUP BY run.product_version",
        p,
    )
    _vres = {r[0]: (r[1], r[2]) for r in _vres_rows}
    version_rows = [
        (r[0], r[1], *_vres.get(r[0], (0, 0))) for r in sorted(_vrun_rows, key=lambda r: _version_sort_key(r[0]))
    ]
    _, _tenants_seen_rows = rows(
        cur,
        f"SELECT DISTINCT run.tenant FROM {RUN_J} "
        f"WHERE r.label=%(rel)s AND run.tenant IS NOT NULL {_PLAT_ONLY} ORDER BY run.tenant",
        p,
    )
    # Each "id name.host" entry wrapped in its own nowrap span so a line break
    # can only happen at the ", " between entries, never inside one (splitting
    # "1331" from "nsclientauto3.stg.boomskope.com" mid-entry is illegible).
    tenants_html = (
        ", ".join(
            f'<span style="white-space:nowrap">{esc(tenant_label(r[0]))}</span>'
            for r in _tenants_seen_rows
        )
        or "n/a"
    )
    # Dropdown option lists for the filter bar. Deliberately UNGUARDED by the
    # current platform/tenant/stack selection (plain "WHERE r.label=%(rel)s") --
    # each dropdown must keep offering every value ever seen on this release, or
    # picking one filter would make the OTHER dropdowns' options vanish/collapse
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
    # Stack dropdown: the fixed known-stack list, not derived from the DB (a
    # stack groups tenants that may span releases/platforms this specific
    # release never touched -- offering only "stacks seen on THIS release"
    # would make the dropdown vary release to release for no reason a viewer
    # can see).
    stack_options = list(_STACKS)
    # Version dropdown: every product_version this release's runs ever had (unguarded, like the other dropdowns), highest first.
    _, _ver_rows = rows(
        cur,
        f"SELECT DISTINCT run.product_version FROM {RUN_J} "
        "WHERE r.label=%(rel)s AND run.product_version IS NOT NULL AND run.product_version LIKE %(ver_prefix)s",
        p,
    )
    version_options = sorted((r[0] for r in _ver_rows), key=_version_sort_key, reverse=True)

    # The Summary counts JENKINS RUNS only: one run, one verdict, one base for every number. A run is FAIL when any test in it
    # failed, else Unstable when a health gate fired (crash dump, handle or memory growth: Jenkins marks the build UNSTABLE),
    # else a clean PASS. clean + unstable + fail = runs, and Pass rate = clean / runs.
    _, _rv = rows(
        cur,
        f"""SELECT count(*) AS runs,
                   count(*) FILTER (WHERE s.f > 0) AS fail_runs,
                   count(*) FILTER (WHERE s.f = 0 AND s.g > 0) AS unstable_runs,
                   count(*) FILTER (WHERE s.f = 0 AND s.g = 0) AS clean_runs,
                   count(*) FILTER (WHERE s.g > 0) AS gate_runs
            FROM (SELECT run.id,
                         count(*) FILTER (WHERE tr.verdict='FAIL') AS f,
                         count(*) FILTER (WHERE tr.gate_violated) AS g
                    FROM {TR_J} WHERE r.label=%(rel)s {_VER_GUARD} GROUP BY run.id) s""",
        p,
    )
    n_runs, run_fail, run_unstable, run_clean, run_gate = (int(x or 0) for x in _rv[0])
    pass_rate = round(run_clean * 100.0 / (n_runs or 1))

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
               0 AS dumps
        FROM test_result tr JOIN test_run run ON run.id=tr.run_id JOIN release r ON r.id=run.release_id
        WHERE r.label=%(rel)s {_VER_GUARD} GROUP BY tr.test_name ORDER BY tr.test_name""".format(
            _VER_GUARD=_VER_GUARD
        ),
        p,
    )
    # dumps column patched from the gate_violation-derived per-test file sets
    # computed above -- see n_dump's comment for why (has_dump/`dump` over-count).
    per_test = [
        row[:-1] + (len(_dump_files_by_test.get(row[0], ())),) for row in per_test
    ]
    # Meta-grid "Test duration" -- sum of total_dur_s (index 5: test, runs,
    # pass, fail, avg_dur_s, total_dur_s, ...) across every test case, i.e.
    # the total wall-clock time this release's test bodies actually ran for.
    _total_test_duration_s = sum(
        float(row[5]) for row in per_test if row[5] is not None
    )
    if _total_test_duration_s >= 3600:
        test_duration = (
            f"{int(_total_test_duration_s // 3600)}h "
            f"{int((_total_test_duration_s % 3600) // 60)}m"
        )
    elif _total_test_duration_s >= 60:
        test_duration = (
            f"{int(_total_test_duration_s // 60)}m {int(_total_test_duration_s % 60)}s"
        )
    else:
        test_duration = f"{_total_test_duration_s:g}s"

    # 2. health-gate breakdown — test name, when, which build/version, observed
    #    value, threshold, detail. (ticket moved to the dedicated Tickets table,
    #    2026-09-08 -- a ticket links to a real build+version, and this table's
    #    grain (per gate_violation) let a bare test_name match leak the SAME
    #    ticket into every release sharing that test name.)
    c2, gate_bd = rows(
        cur,
        """
        SELECT gv.violation_type AS gate,
               tr.test_name AS triggered_by,
               regexp_replace(run.jenkins_job, '^.*GRS-SYSTEMTEST-', '') AS lane,
               run.jenkins_build AS build,
               COALESCE(to_char(COALESCE(tr.started_at, run.started_at),
                                'YYYY-MM-DD HH24:MI'), '-') AS datetime,
               COALESCE(run.product_version, '-') AS version,
               COALESCE(gv.process_name, '-') AS process,
               COALESCE(gv.observed::text, '-') AS observed,
               COALESCE(gv.threshold::text, '-') AS threshold,
               -- a dump violation keeps its full text: the file paths ARE the evidence (3 paths read 371 characters)
               COALESCE(CASE WHEN gv.violation_type IN ('bsod_dump', 'crash_dump') THEN gv.detail
                             ELSE left(gv.detail, 120) END, '-') AS detail
        FROM gate_violation gv
        JOIN test_result tr ON tr.id=gv.test_result_id
        JOIN test_run run ON run.id=tr.run_id
        JOIN release r ON r.id=run.release_id
        WHERE r.label=%(rel)s {_VER_GUARD}
          -- a dump planted by grs_tools/fake_crash_dump.py (name carries __FAKE__) only checks the crash scan; it is
          -- test tooling and never belongs in a report, even if its build is ingested again
          AND position('__FAKE__' in COALESCE(gv.detail, '')) = 0
        ORDER BY gv.violation_type, run.jenkins_build DESC""".format(
            _VER_GUARD=_VER_GUARD
        ),
        p,
    )
    # 2a. closest to each limit: a release with few or no violation rows must say how far its numbers stayed from the limits,
    # otherwise "no row" reads as "not measured". Limits come from the same Configuration the ingest gates with (no copied
    # constants); when the GRS checkout cannot be loaded the maxima are shown without a limit.
    try:
        import ingest as _ingest

        _lim = {
            "growth": getattr(_ingest.CFG, "MEM_GROWTH_MAX_MB", None),
            "ceiling": getattr(_ingest.CFG, "MEM_ABS_CEILING_MB", None),
            "handles": getattr(_ingest.CFG, "HEALTH_MAX_HANDLES", None),
        }
        _observe_only = tuple(getattr(_ingest._health_gate, "OBSERVE_ONLY_PROCESSES", ()))
    except (Exception, SystemExit):  # noqa: BLE001 - the report must render without the GRS checkout
        _lim, _observe_only = {}, ()
    # The handle limit of a release is the one it was gated with (gate_history.json), not whatever the current code says: a
    # newer, stricter gate must not change an older release's page. Works without the GRS checkout (the report VM has none).
    try:
        from gate_history import handles_gate_for_major

        _hist_handles = handles_gate_for_major(_major)
        if _hist_handles is not None:
            _lim["handles"] = _hist_handles
    except Exception:  # noqa: BLE001 - history is a refinement, never a reason to fail the page
        pass
    _, _near_rows = rows(
        cur,
        f"""
        SELECT max(ps.mem_growth_mb), max(ps.mem_max_mb), max(ps.handles_max), max(ps.cpu_max_pct), count(*)
        FROM process_summary ps
        JOIN test_result tr ON tr.id=ps.test_result_id
        JOIN test_run run ON run.id=tr.run_id
        JOIN release r ON r.id=run.release_id
        WHERE r.label=%(rel)s {_VER_GUARD}
          AND ps.alive_pct >= 50
          AND NOT (ps.process_name = ANY(%(observe_only)s))""",
        {**p, "observe_only": list(_observe_only)},
    )
    _near = _near_rows[0] if _near_rows and _near_rows[0][4] else None

    def _vs(value, limit, unit):
        if value is None:
            return "no data"
        tail = f" (limit {limit:g})" if limit is not None else ""
        return f"{float(value):g}{unit}{tail}"

    gate_near = (
        "Highest value in this release against each limit (gated processes alive at least 50% of a run): "
        f"memory growth {_vs(_near[0], _lim.get('growth'), ' MB')}, memory peak {_vs(_near[1], _lim.get('ceiling'), ' MB')}, "
        f"handles {_vs(_near[2], _lim.get('handles'), '')}, CPU peak {_vs(_near[3], CPU_GATE, '')} "
        "(a CPU run fails only after 60 s in a row). A violation row appears only when one of these crosses its limit; "
        "a dump always makes a row. Limits are those of this release."
        if _near
        else ""
    )

    # 2b. tickets — dedicated table (2026-09-08, redesign). A ticket links to a
    # REAL build+version, never a bare test name: LEFT JOIN test_run so a build
    # with real data reads version/bitness/iter/gate live, but a build that
    # died before pytest ever ran (docker image missing, git checkout timeout,
    # ...) -- no test_run row at all -- still shows up, using the version/
    # bitness `db.py issue set --version/--bitness` stored on the ticket
    # itself. Scope: a real run must belong to THIS release (r.label); an
    # orphan (no test_run) is scoped by its own stored version against this
    # release's major-version prefix -- the only signal available for it.
    c2b, tickets = rows(
        cur,
        """
        SELECT gt.ticket_id AS ticket,
               COALESCE(run.product_version, gt.product_version, '-') AS version,
               -- category and summary are per-ticket facts (db.py issue info writes every row of the ticket); the
               -- fallback to any other row of the same ticket keeps a row added later from showing '-'.
               COALESCE(gt.category, (SELECT max(g2.category) FROM gate_ticket g2
                                       WHERE g2.ticket_id = gt.ticket_id AND g2.category IS NOT NULL), '-') AS category,
               COALESCE(gt.summary, (SELECT max(g2.summary) FROM gate_ticket g2
                                      WHERE g2.ticket_id = gt.ticket_id AND g2.summary IS NOT NULL), '-') AS summary,
               -- A ticket stored without --test (test_name NULL) used to print "(whole build)" even when the
               -- build has a lane, a number and named gate hits. Name the cases the build's gate hits (else its
               -- FAIL cases) belong to; "(whole build)" stays only for a build with neither.
               COALESCE(gt.test_name,
                        (SELECT CASE WHEN count(*) > 3
                                     THEN array_to_string((array_agg(x.t ORDER BY x.t))[1:3], ', ') || ' (+' || (count(*) - 3) || ' more)'
                                     ELSE string_agg(x.t, ', ' ORDER BY x.t) END
                           FROM (SELECT DISTINCT tr.test_name AS t FROM test_result tr
                                   JOIN gate_violation gv ON gv.test_result_id = tr.id
                                  WHERE tr.run_id = run.id) x),
                        (SELECT CASE WHEN count(*) > 3
                                     THEN array_to_string((array_agg(x.t ORDER BY x.t))[1:3], ', ') || ' (+' || (count(*) - 3) || ' more)'
                                     ELSE string_agg(x.t, ', ' ORDER BY x.t) END
                           FROM (SELECT DISTINCT tr.test_name AS t FROM test_result tr
                                  WHERE tr.run_id = run.id AND tr.verdict = 'FAIL') x),
                        '-') AS test,
               regexp_replace(gt.jenkins_job, '^.*GRS-SYSTEMTEST-', '') AS lane,
               CASE WHEN gt.jenkins_build = 0 THEN '-' ELSE gt.jenkins_build::text END AS build,
               CASE WHEN gt.jenkins_build = 0 THEN '-' ELSE COALESCE((SELECT count(it.id) FROM iteration_result it
                          JOIN test_result tr ON tr.id = it.test_result_id
                         WHERE tr.run_id = run.id
                           AND (gt.test_name IS NULL OR tr.test_name = gt.test_name)), 0)::text END AS iter
        FROM gate_ticket gt
        -- clone-release can leave the same (job, build) pointing at >1 test_run row
        -- (e.g. a "-final" snapshot has its own copy, with a HIGHER id than the
        -- source release's original row). Picking a single global MIN(id) always
        -- resolved to the ORIGINAL release's row, so r.label below could never equal
        -- a "-final" rel and the ticket silently dropped out of every -final report
        -- (found 2026-11-18: "144-final ticket become fewer"). Fix: prefer the
        -- test_run that actually belongs to THIS report's release; only fall back to
        -- the global MIN(id) when no run exists under the current release at all
        -- (a truly orphaned ticket with no run anywhere in this release).
        LEFT JOIN test_run run
               ON run.id = COALESCE(
                    (SELECT MIN(tr2.id) FROM test_run tr2 JOIN release r2 ON r2.id = tr2.release_id
                      WHERE tr2.jenkins_job = gt.jenkins_job AND tr2.jenkins_build = gt.jenkins_build
                        AND r2.label = %(rel)s),
                    (SELECT MIN(tr2.id) FROM test_run tr2
                      WHERE tr2.jenkins_job = gt.jenkins_job
                        AND tr2.jenkins_build = gt.jenkins_build)
                  )
        LEFT JOIN release r ON r.id = run.release_id
        WHERE CASE
                WHEN gt.report_release IS NOT NULL THEN gt.report_release = %(report_major)s
                WHEN run.id IS NOT NULL THEN r.label = %(rel)s {_PLAT_ONLY}
                ELSE gt.product_version LIKE %(ver_prefix)s{_ORPHAN_PLAT}
              END
          AND (%(version)s IS NULL OR COALESCE(run.product_version, gt.product_version) = %(version)s)
          AND (%(ehf_re)s IS NULL OR COALESCE(run.product_version, gt.product_version) ~ %(ehf_re)s)
          AND (%(nonehf_re)s IS NULL OR COALESCE(run.product_version, gt.product_version) IS NULL
               OR COALESCE(run.product_version, gt.product_version) !~ %(nonehf_re)s)
        ORDER BY NULLIF(regexp_replace(gt.ticket_id, '\\D', '', 'g'), '')::bigint DESC NULLS LAST,
                 gt.ticket_id, COALESCE(run.product_version, gt.product_version), gt.jenkins_build, gt.test_name""".format(
            _PLAT_ONLY=_PLAT_ONLY, _ORPHAN_PLAT=_ORPHAN_PLAT
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
               round(max(CASE WHEN ps.cpu_threshold >= %(cpu_gate)s THEN ps.cpu_sustained_s
                              WHEN ps.cpu_max_pct < %(cpu_gate)s THEN 0
                              ELSE ps.cpu_sustained_s END),0) AS worst_sustained_s,
               %(cpu_gate)s AS cpu_gate,
               round(avg(ps.mem_growth_mb),1) AS avg_mem_growth,
               round(max(ps.mem_growth_mb),1) AS worst_mem_growth,
               max(ps.handles_max) AS worst_handles,
               round(avg(th.tpeak),1) AS avg_threads_peak,
               max(th.tpeak) AS worst_threads
        FROM process_summary ps JOIN test_result tr ON tr.id=ps.test_result_id
        JOIN test_run run ON run.id=tr.run_id JOIN release r ON r.id=run.release_id
        LEFT JOIN (SELECT rs.test_result_id, rs.process_name, max(rs.threads) AS tpeak
                     FROM resource_sample rs WHERE rs.alive AND rs.threads > 0
                    GROUP BY rs.test_result_id, rs.process_name) th
               ON th.test_result_id = ps.test_result_id AND th.process_name = ps.process_name
        WHERE r.label=%(rel)s {_VER_GUARD} GROUP BY ps.process_name ORDER BY worst_cpu_peak DESC""".format(
            _VER_GUARD=_VER_GUARD
        ),
        {**p, "cpu_gate": CPU_GATE},
    )

    # 3a. the roster of monitored processes with the number of runs that saw each one alive in this release (0 = no data)
    _, _seen_rows = rows(
        cur,
        f"""
        SELECT ps.process_name, run.platform, count(DISTINCT run.id), count(DISTINCT tr.test_name)
        FROM process_summary ps JOIN test_result tr ON tr.id=ps.test_result_id
        JOIN test_run run ON run.id=tr.run_id JOIN release r ON r.id=run.release_id
        WHERE r.label=%(rel)s {_VER_GUARD} AND ps.alive_pct > 0
        GROUP BY ps.process_name, run.platform""",
        p,
    )
    import monitored  # noqa: E402  (a file of this folder; sys.path already holds it)

    monitored_html = monitored_card(monitored.load(), {(r[0], r[1]): (r[2], r[3]) for r in _seen_rows}, platform)

    # 3b. resource peaks per process ACROSS versions (140 up through the
    # current release's major), feeding the trend line chart below the
    # per-process table. Deliberately NOT scoped by r.label/release: this
    # spans every release row in the DB whose product_version's major falls
    # in [140, _major] -- "which release a build's row got filed under" and
    # "which client major version it actually ran" are different axes (same
    # distinction _VER_GUARD's own ver_prefix comment makes), and this chart
    # is about the latter, growing one more version per report as later
    # releases land (R140 alone on release-140, +141 on release-141-final,
    # +142 on release-142-final, ...).
    # SPECIAL RULE, in the words it was given: "This is a specail rule." The EPAIS client builds 142.100.100.10500 and
    # 211.100.100.10556 are listed as R144 in the cross-version CPU and memory charts, whatever their own version string
    # says. One expression, used by every query of this section, so the CPU chart, the mac chart, the memory charts and
    # the step notes agree.
    _SPECIAL_144_BUILDS = tuple(dict.fromkeys(("142.100.100.10500", "211.100.100.10556") + EPAIS_144_BUILDS))
    _MAJOR_SQL = (
        "CASE WHEN run.product_version IN ("
        + ", ".join(f"'{b}'" for b in _SPECIAL_144_BUILDS)
        + ") THEN 144 ELSE (substring(run.product_version from '^(\\d+)'))::int END"
    )
    _SPECIAL_NOTE = (
        "Special rule: the EPAIS client builds "
        + " and ".join(_SPECIAL_144_BUILDS)
        + " are counted as R144 here, whatever their own version string says."
    )
    # WI-0079: the list above was typed from two version strings and missed a third EPAIS build (10575 drew an R142 point). The page
    # now says which EPAIS feature builds of the same families (142.100.100.*, 211.100.100.*) carry the process and are not in it.
    _gap_cols, _gap_rows = rows(
        cur,
        r"""SELECT DISTINCT run.product_version
        FROM process_summary ps
        JOIN test_result tr ON tr.id=ps.test_result_id
        JOIN test_run run ON run.id=tr.run_id
        WHERE ps.process_name ILIKE %(pat)s AND run.product_version ~ '^(142|211)\.100\.100\.'""",
        {"pat": "%epais%"},
    )
    _special_gap = sorted(v for (v,) in _gap_rows if v not in _SPECIAL_144_BUILDS)
    if _special_gap:
        _SPECIAL_NOTE += " WARNING: other EPAIS feature builds are not in this rule: " + ", ".join(_special_gap) + "."

    c3v, res_by_version = rows(
        cur,
        f"""
        SELECT {_MAJOR_SQL} AS major,
               ps.process_name AS process,
               round(avg(ps.cpu_max_pct),1) AS avg_cpu_peak,
               round(max(ps.cpu_max_pct),1) AS worst_cpu_peak
        FROM process_summary ps
        JOIN test_result tr ON tr.id=ps.test_result_id
        JOIN test_run run ON run.id=tr.run_id
        JOIN release r ON r.id=run.release_id
        WHERE run.product_version ~ '^\\d+'
          AND {_MAJOR_SQL}
              BETWEEN 140 AND %(major_ceiling)s
          {_TENANT_GUARD}
          {_FINAL_GUARD}
          AND run.platform='windows'
          -- a process never seen alive has a 0.0 CPU reading that means "not measured", the same rule the memory
          -- charts and the mac query use; without it a CPU point shows where the memory chart has none
          AND ps.alive_pct > 0
        GROUP BY major, ps.process_name
        ORDER BY major, ps.process_name
        """,
        {**p, "major_ceiling": _major if _major is not None else 9999},
    )

    # 3c. same trend for mac (MAC2 / MAC2-SSH). Kept in its own chart because
    # the mac process set (Netskope Client, nsAuxiliarySvc, ...) shares nothing
    # with the Windows one, and mac resource_samples are only ingested from R143
    # on (owner 2026-10-15: "start from 143"), so the x-axis floor is 143, not 140.
    _MAC_FIRST_MAJOR = 143
    c3m, res_by_version_mac = rows(
        cur,
        f"""
        SELECT {_MAJOR_SQL} AS major,
               ps.process_name AS process,
               round(avg(ps.cpu_max_pct),1) AS avg_cpu_peak,
               round(max(ps.cpu_max_pct),1) AS worst_cpu_peak
        FROM process_summary ps
        JOIN test_result tr ON tr.id=ps.test_result_id
        JOIN test_run run ON run.id=tr.run_id
        JOIN release r ON r.id=run.release_id
        WHERE run.product_version ~ '^\\d+'
          AND {_MAJOR_SQL}
              BETWEEN {_MAC_FIRST_MAJOR} AND %(major_ceiling)s
          {_TENANT_GUARD}
          {_FINAL_GUARD}
          AND run.platform='mac'
          -- mac sampler caveats (git44 RCA 2026-10-15): a process never seen
          -- alive is "not measured" (root-owned procs have cmdline=None for
          -- the SSH user), and runs with <=3 ticks only hold psutil's
          -- always-0.0 first reading. Neither is evidence of idle CPU.
          AND ps.alive_pct > 0
          AND ps.sample_count >= 5
        GROUP BY major, ps.process_name
        ORDER BY major, ps.process_name
        """,
        {**p, "major_ceiling": _major if _major is not None else 9999},
    )

    # 3d. MEMORY across versions, same shape as the CPU trend above. Windows memory is Private Bytes (GetProcessMemoryInfo
    # PrivateUsage, unchanged since the sampler was written); mac memory is RSS, so the two never share a chart. A row
    # with mem_max 0 or a process never seen alive is "not measured", not "uses no memory", and is left out.
    # Averages mix different test sets per version (R140: 15 tests, R142: 36, R143: 46), so the table prints the test
    # count next to every average and the notes below re-check any big step on the tests both versions ran.
    _MEM_SQL = f"""
        SELECT {_MAJOR_SQL} AS major,
               ps.process_name AS process,
               round(avg(ps.mem_baseline_mb),1) AS avg_baseline,
               round(avg(ps.mem_max_mb),1) AS avg_peak,
               round(max(ps.mem_max_mb),1) AS worst_peak,
               count(*) AS samples,
               count(DISTINCT tr.test_name) AS tests,
               count(DISTINCT run.id) AS runs
        FROM process_summary ps
        JOIN test_result tr ON tr.id=ps.test_result_id
        JOIN test_run run ON run.id=tr.run_id
        JOIN release r ON r.id=run.release_id
        WHERE run.product_version ~ '^\\d+'
          AND {_MAJOR_SQL}
              BETWEEN %(first_major)s AND %(major_ceiling)s
          {_TENANT_GUARD}
          {_FINAL_GUARD}
          AND run.platform=%(mem_platform)s
          AND ps.alive_pct > 0
          AND ps.mem_max_mb > 0
          {{extra}}
        GROUP BY major, ps.process_name
        ORDER BY ps.process_name, major
        """
    _ceiling = _major if _major is not None else 9999
    c3w, mem_by_version = rows(
        cur,
        _MEM_SQL.format(extra=""),
        {**p, "first_major": 140, "major_ceiling": _ceiling, "mem_platform": "windows"},
    )
    c3x, mem_by_version_mac = rows(
        cur,
        _MEM_SQL.format(extra="AND ps.sample_count >= 5"),
        {**p, "first_major": _MAC_FIRST_MAJOR, "major_ceiling": _ceiling, "mem_platform": "mac"},
    )

    # A big step between two neighbouring versions of one process gets a note, with the same comparison restricted to the
    # tests BOTH versions ran, so a change of test mix cannot pass for a product change.
    def _mem_step_notes(platform_name, mem_rows):
        base = {(r[0], r[1]): r for r in mem_rows if r[2] is not None}
        notes = []
        for (major, proc), r in sorted(base.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            prev = base.get((major - 1, proc))
            if not prev or prev[2] is None or float(prev[2]) <= 0:
                continue
            a, b = float(prev[2]), float(r[2])
            if b < a * 1.5 or b - a < 10:
                continue
            _, same = rows(
                cur,
                f"""
                SELECT {_MAJOR_SQL} AS major,
                       round(avg(ps.mem_baseline_mb),1), count(*), count(DISTINCT tr.test_name)
                FROM process_summary ps
                JOIN test_result tr ON tr.id=ps.test_result_id
                JOIN test_run run ON run.id=tr.run_id
                JOIN release r ON r.id=run.release_id
                WHERE run.platform=%(mem_platform)s AND ps.process_name=%(proc)s
                  AND ps.alive_pct > 0 AND ps.mem_max_mb > 0
                  AND run.product_version ~ '^\\d+'
                  AND {_MAJOR_SQL} IN (%(v1)s, %(v2)s)
                  {_TENANT_GUARD}
                  {_FINAL_GUARD}
                  AND tr.test_name IN (
                    SELECT tr2.test_name
                    FROM process_summary ps2
                    JOIN test_result tr2 ON tr2.id=ps2.test_result_id
                    JOIN test_run run2 ON run2.id=tr2.run_id
                    JOIN release r2 ON r2.id=run2.release_id
                    WHERE run2.platform=%(mem_platform)s AND ps2.process_name=%(proc)s
                      AND ps2.alive_pct > 0 AND ps2.mem_max_mb > 0
                      AND run2.product_version ~ '^\\d+'
                      AND {_MAJOR_SQL.replace('run.', 'run2.')} IN (%(v1)s, %(v2)s)
                      {_final_guard("run2", "r2")}
                    GROUP BY tr2.test_name
                    HAVING count(DISTINCT {_MAJOR_SQL.replace('run.', 'run2.')}) = 2)
                GROUP BY major ORDER BY major
                """,
                {**p, "mem_platform": platform_name, "proc": proc, "v1": major - 1, "v2": major},
            )
            text = f"{proc}: average baseline {a:g} MB in R{major - 1}, {b:g} MB in R{major}"
            if len(same) == 2:
                text += (
                    f". On the tests both versions ran: {same[0][1]} MB (R{same[0][0]}, {same[0][2]} samples) and "
                    f"{same[1][1]} MB (R{same[1][0]}, {same[1][2]} samples)"
                )
            if platform_name == "windows" and major in MEM_KNOWN_CAUSES:
                text += f". Known cause: {MEM_KNOWN_CAUSES[major]}"
            notes.append(text)
        return notes

    mem_notes_win = _mem_step_notes("windows", mem_by_version)
    mem_notes_mac = _mem_step_notes("mac", mem_by_version_mac)

    # Split processes into "heavy" (stAgentSvc etc., tens-to-hundreds % CPU) vs
    # "light" (svcmon/UI, low single-digit %) groups so the light ones aren't
    # visually flattened to a flat line by the heavy ones sharing one y-axis.
    _HEAVY_CPU_THRESHOLD = 30.0
    _version_processes_all = sorted({r[1] for r in res_by_version if r[1] is not None})
    _process_peak = {
        proc: max(
            [float(r[2]) for r in res_by_version if r[1] == proc and r[2] is not None]
            + [float(r[3]) for r in res_by_version if r[1] == proc and r[3] is not None]
            or [0.0]
        )
        for proc in _version_processes_all
    }
    _heavy_processes = sorted(
        p for p in _version_processes_all if _process_peak[p] >= _HEAVY_CPU_THRESHOLD
    )
    _light_processes = sorted(
        p for p in _version_processes_all if _process_peak[p] < _HEAVY_CPU_THRESHOLD
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
               0 AS dumps
        FROM test_run run
        JOIN release r ON r.id=run.release_id
        LEFT JOIN test_result tr ON tr.run_id=run.id
        LEFT JOIN gate_violation gv ON gv.test_result_id=tr.id
        WHERE r.label=%(rel)s AND run.product_version IS NOT NULL {_VER_GUARD}
        GROUP BY run.product_version
        ORDER BY version ASC""".format(_VER_GUARD=_VER_GUARD),
        p,
    )
    # dumps column patched from the gate_violation-derived per-version file sets
    # (see n_dump's comment above).
    stressed = [
        row[:-1] + (len(_dump_files_by_version.get(row[0], ())),) for row in stressed
    ]
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
    # 4d-2. tests that have results in this release but NO iteration rows at all
    # (DEM-05 uninstall, DEM-10 reboot, FC-03 reboot, skipped mac twins ...). The
    # checkpoint query above is built from iteration_result, so these used to vanish
    # from the table even though the DB holds their runs (git44 2026-10-16: "DEM cases
    # are in the DB but lost in the report"). Listed last, flagged "no iteration
    # recorded", with the runs / latest verdict so a FAIL is not silently hidden.
    _, iter_ckpt_none = rows(
        cur,
        """
        SELECT tr.test_name AS test,
               count(*) AS runs,
               count(*) FILTER (WHERE tr.verdict='PASS') AS n_pass,
               count(*) FILTER (WHERE tr.verdict='FAIL') AS n_fail,
               (array_agg(tr.verdict ORDER BY run.started_at DESC NULLS LAST, tr.id DESC))[1] AS latest
        FROM test_result tr
        JOIN test_run run ON run.id=tr.run_id
        JOIN release r ON r.id=run.release_id
        WHERE r.label=%(rel)s {_VER_GUARD}
          AND NOT EXISTS (SELECT 1 FROM iteration_result it
                          JOIN test_result t2 ON t2.id=it.test_result_id
                          JOIN test_run r2 ON r2.id=t2.run_id
                          WHERE t2.test_name=tr.test_name AND r2.release_id=run.release_id)
        GROUP BY tr.test_name
        ORDER BY tr.test_name""".format(_VER_GUARD=_VER_GUARD),
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
        # Two sortable headers (owner 2026-10-16): "Test case" sorts by name (click
        # again to reverse); "Deepest iteration reached" restores the default order
        # (deepest first, ties by name). Plain client-side sort, see sortCkpt().
        sort_link = {
            "Test case": "sortCkpt('test')",
            "Deepest iteration reached": "sortCkpt('deepest')",
        }
        th = "".join(
            (
                f'<th><a href="#" class="sortlink" onclick="{sort_link[c]};return false;" '
                f'title="click to sort">{esc(c)} &#x21C5;</a></th>'
                if c in sort_link
                else f"<th>{esc(c)}</th>"
            )
            for c in c4d_cols
        )
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
                f'<tr class="{rowcls}" data-test="{esc(test)}" data-deepest="{esc(max_iter)}">'
                f"<td>{test_link_html(test)}</td><td>{esc(iter1)}</td>"
                f"<td>{esc(iter10)}</td><td>{deepest_html}</td><td>{esc(max_pass)}</td></tr>"
            )
        for test, runs, n_pass, n_fail, latest in iter_ckpt_none:
            # No iteration rows exist for this case (older run, or a case that does not
            # iterate): count each RUN as one iteration using its verdict so pass/fail is
            # complete (owner 2026-10-16: "add iter 1/1 record or something"). Derived at
            # render time only -- nothing is written to the DB. Marked with * + footnote.
            dot_cls = "p" if latest == "PASS" else ("f" if latest == "FAIL" else "")
            dot = f'<span class="dot {dot_cls}"></span> ' if dot_cls else ""
            rowcls = "warn" if latest == "FAIL" else ""
            max_pass = 1 if n_pass else "-"
            trs.append(
                f'<tr class="{rowcls}" data-test="{esc(test)}" data-deepest="1">'
                f"<td>{test_link_html(test)}</td><td>{esc(n_pass)}/{esc(runs)} *</td>"
                f"<td>(undone &mdash; max iter reached is 1)</td>"
                f"<td>{dot}iter 1: {esc(latest)} *</td><td>{esc(max_pass)}</td></tr>"
            )
        foot = (
            '<p class="foot">* no iteration rows were recorded for this case (older run, '
            "or a case that does not iterate): each run is counted as one iteration "
            "using its own PASS/FAIL verdict.</p>"
            if iter_ckpt_none
            else ""
        )
        return (
            f'<table id="ckptTable"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table>{foot}'
        )

    iter_ckpt_html = (
        _ckpt_table_html(iter_ckpt) if (iter_ckpt or iter_ckpt_none) else ""
    )

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
            f'<div class="seqcase"><div class="seqtitle">{test_link_html(tc, short)}</div>{inner}</div>'
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
                f'<span class="lname" title="{esc(tip)}">{test_link_html(tname)}</span>'
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

    # Split gate_bd into crash rows (bsod/crash dump) and non-crash rows.
    # c2 = [gate, triggered_by, lane, build, datetime, version, process, observed, threshold, detail]
    _CRASH_TYPES = {"bsod_dump", "crash_dump"}
    _CRASH_IDX   = [0, 1, 2, 3, 4, 5, 9]   # gate, triggered_by, lane, build, datetime, version, detail
    _NCRASH_IDX  = [0, 1, 2, 3, 4, 5, 6, 7, 8]  # gate, triggered_by, lane, build, datetime, version, process, observed, threshold
    _verdicts = _load_crash_verdicts()
    crash_cols  = [c2[i] for i in _CRASH_IDX] + ["verdict"]
    ncrash_cols = [c2[i] for i in _NCRASH_IDX]
    # the verdict is the last (right-most) column; blank unless crash_verdicts.json names "<lane>:<build>"
    crash_bd  = [[r[i] for i in _CRASH_IDX] + [_verdicts.get(f"{r[2]}:{r[3]}", "")]
                 for r in gate_bd if r[0] in _CRASH_TYPES]
    # the same dump path under two builds of one lane is listed once, under the first build (detail is column 6 here)
    _dd = dedup_dump_paths(crash_bd, lane_i=2, build_i=3, detail_i=6)
    crash_bd = _dd
    ncrash_bd = [[r[i] for i in _NCRASH_IDX] for r in gate_bd if r[0] not in _CRASH_TYPES]
    # triggered_by is column 1 in both projected tables; detail is column 6 in crash table.
    _cw_crash  = {1: "word-break:break-all", 6: "min-width:260px;word-break:break-word", 7: "min-width:90px"}
    _cw_ncrash = {1: "word-break:break-all"}

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
  timestamp of its own); <em>version</em> = the client version installed in that build.
  Filed tickets for gate hits show in the <strong>Tickets</strong> table above (per case,
  not per row here).
</div>"""

    # Cross-version CPU trend cards. Windows card: every platform view except
    # mac-only. Mac card: every view except windows-only, and only when mac
    # samples exist. One row (avg + worst side by side), no heavy/light split:
    # the mac set is 4 processes and only "Netskope Client" ever exceeds a few %.
    _GUIDE_LINK = (
        '<a class="badge warn guide" href="/guide/resources" target="_blank" rel="noopener">'
        "&#9432; How to read these numbers &mdash; guide &rarr;</a>"
    )
    _final_note = (
        " <strong>Final view:</strong> a version that has a -final release ("
        + ", ".join(f"R{m}" for m in p["final_majors"])
        + ") uses only that release's runs; a version with no final cut is not shown."
        if p["final_view"]
        else ""
    )
    ver_win_card = (
        ""
        if platform == "mac"
        else f"""<div class="card"><h2>Resource peaks per process across versions</h2>
  <div>{_GUIDE_LINK}</div>
  <div class="cpu-explain">
    One line per process. X-axis = client major version, starting at R140 and
    growing by one point per report as later releases land (this page shows
    every major version from 140 up to {esc(_major if _major is not None else '?')}).
    Same cpu_max_pct metric as the table above, just tracked release over
    release instead of only this release's own peak. Windows runs only.{_final_note}
    <br><strong>What a point is:</strong> the worst single 10 s CPU reading (100 = one core) of that process in any run of
    that version that this report counts. It is a record of a peak. It does not mean CPU usage got worse from one
    version to the next, and it did not last: one 10 s window is a spike, and a run fails only when the process stays at
    or above {CPU_GATE} for 60 s in a row. Every version also runs a different set of tests, which is why the
    per-version average line was removed (an average of peaks follows the test mix). Read a point as "has been this high". {esc(_SPECIAL_NOTE)}
    The yellow dashed line is the {CPU_GATE} gate. It is drawn on the heavy chart only; the light processes peak under {int(_HEAVY_CPU_THRESHOLD)},
    where a line at {CPU_GATE} would flatten their lines.
  </div>
  <div class="chart-row">
    <div>
      <h3>Heavy processes (peak &ge; {esc(int(_HEAVY_CPU_THRESHOLD))}%)</h3>
      <div class="chart-wrap"><canvas id="cpuVersionWorstHeavyChart"></canvas></div>
    </div>
    <div>
      <h3>Light processes (peak &lt; {esc(int(_HEAVY_CPU_THRESHOLD))}%)</h3>
      <div class="chart-wrap"><canvas id="cpuVersionWorstLightChart"></canvas></div>
    </div>
  </div>
</div>"""
    )
    ver_mac_card = (
        f"""<div class="card"><h2>Resource peaks per process across versions — mac</h2>
  <div>{_GUIDE_LINK}</div>
  <div class="cpu-explain">
    MAC2 / MAC2-SSH runs only. X-axis = client major version, starting at
    R{_MAC_FIRST_MAJOR} (the first release with mac process samples) up to
    {esc(_major if _major is not None else '?')}. Same cpu_max_pct metric
    (100 = one core). A single row: the mac set is only a handful of processes.{_final_note}
    Each point is the worst single 10 s CPU reading of that process in any mac run of that version that this report
    counts: a record of a peak, not CPU usage getting worse, and not a sustained load. The per-version average line was
    removed (it follows the test mix).
  </div>
  <div class="chart-row one">
    <div class="chart-wrap"><canvas id="cpuVersionWorstMacChart"></canvas></div>
  </div>
</div>"""
        if platform != "windows" and res_by_version_mac
        else ""
    )

    _HEAVY_MEM_MB = 20.0

    def _mem_table(mem_rows):
        cols = ["Process", "Version", "Avg baseline MB", "Avg peak MB", "Worst peak MB", "Samples", "Tests", "Runs"]
        data = [(r[1], f"R{r[0]}", r[2], r[3], r[4], r[5], r[6], r[7]) for r in mem_rows]
        return table(cols, data)

    def _mem_notes_html(notes):
        if not notes:
            return ""
        items = "".join(f"<li>{esc(n)}</li>" for n in notes)
        return f'<div class="cpu-explain"><strong>Large steps between neighbouring versions:</strong><ul>{items}</ul></div>'

    mem_win_card = (
        ""
        if platform == "mac" or not mem_by_version
        else f"""<div class="card"><h2>Memory per process across versions</h2>
  <div>{_GUIDE_LINK}</div>
  <div class="cpu-explain">
    X-axis = client major version, R140 up to {esc(_major if _major is not None else '?')}. Metric = Private Bytes in MB,
    from the same per-test process summaries as the CPU charts (peak = the highest value of one test, baseline = its
    start). Each point averages every test that ran on that version, and the test set differs per version, so read the
    Tests and Samples columns before comparing. Windows runs only.{_final_note} {esc(_SPECIAL_NOTE)}
  </div>
  {_mem_notes_html(mem_notes_win)}
  <h3>Heavy processes (peak &ge; {esc(int(_HEAVY_MEM_MB))} MB)</h3>
  <div class="chart-row">
    <div class="chart-wrap"><canvas id="memVersionAvgHeavyChart"></canvas></div>
    <div class="chart-wrap"><canvas id="memVersionWorstHeavyChart"></canvas></div>
  </div>
  <h3>Light processes (peak &lt; {esc(int(_HEAVY_MEM_MB))} MB)</h3>
  <div class="chart-row">
    <div class="chart-wrap"><canvas id="memVersionAvgLightChart"></canvas></div>
    <div class="chart-wrap"><canvas id="memVersionWorstLightChart"></canvas></div>
  </div>
  {_mem_table(mem_by_version)}
</div>"""
    )
    mem_mac_card = (
        f"""<div class="card"><h2>Memory per process across versions — mac</h2>
  <div>{_GUIDE_LINK}</div>
  <div class="cpu-explain">
    MAC2 / MAC2-SSH runs only, from R{_MAC_FIRST_MAJOR} (the first release with mac process samples). Metric = RSS in
    MB, which is not the Windows Private Bytes, so never compare a mac number with a Windows one. Processes never seen
    alive, and runs with fewer than 5 samples, are left out as not measured.{_final_note}
  </div>
  {_mem_notes_html(mem_notes_mac)}
  <div class="chart-row">
    <div class="chart-wrap"><canvas id="memVersionAvgMacChart"></canvas></div>
    <div class="chart-wrap"><canvas id="memVersionWorstMacChart"></canvas></div>
  </div>
  {_mem_table(mem_by_version_mac)}
</div>"""
        if platform != "windows" and mem_by_version_mac
        else ""
    )

    body = f"""
<div class="card hero">
  <div class="header-row"><h1>SystemTest Report — {esc(short_release(release))}</h1></div>
  {filter_bar(release, platform, tenant, stack, platform_options, tenant_options, stack_options, version, version_options)}
  <div class="header-row">
  {f'<span class="badge warn" title="This view is filtered to one platform — other platforms runs are excluded.">Platform: {esc(platform)} only</span>' if platform else ''}
  {f'<span class="badge warn" title="This view is filtered to one tenant — other tenants runs are excluded.">Tenant: {esc(tenant_label(tenant))}</span>' if tenant else ''}
  {f'<span class="badge warn" title="This view is filtered to every tenant in one stack.">Stack: {esc(stack)}</span>' if stack else ''}
  <span class="badge {'fail' if run_fail else 'pass'}">{esc(run_fail)} runs FAIL</span>
  <span class="badge {'warn' if run_unstable else 'pass'}">{esc(run_unstable)} runs Unstable</span>
  <span class="badge {'fail' if n_dump else 'pass'}">{esc(n_dump)} dump files</span></div>
  <dl class="meta-grid{' has-feat' if tenant else ''}">
    <div><dt>Tenant(s)</dt><dd>{tenants_html}</dd></div>
    <div><dt>Product version(s)</dt><dd class="versions">{versions_header_html(version_rows)}</dd></div>
    <div><dt>Runs</dt><dd class="big">{esc(n_runs)}</dd></div>
    <div><dt>Test duration</dt><dd class="big">{esc(test_duration)}</dd></div>
    {f'<div class="feat"><dt>Features enabled</dt><dd>{feature_badges_html(tenant, release, cur=cur)}</dd></div>' if tenant else ''}
  </dl>
</div>

<div class="card"><h2>Summary</h2><div class="kpi-row">
  {kpi("Pass rate", f"{pass_rate}%", f"{run_clean}/{n_runs} runs clean")}
  {kpi("Runs FAIL", run_fail)}
  {kpi("Runs Unstable", run_unstable)}
  {kpi("Runs with a gate hit", run_gate)}
  {kpi("Dump files", n_dump)}
  {kpi("Distinct test cases", len(per_test))}
</div>
<div style="margin:8px 0 0">{numbers_help_html(n_runs, run_clean, run_unstable, run_fail, run_gate, n_dump, pass_rate, str(release).endswith("-final"))}</div>
</div>

<div class="card"><h2>Tickets</h2>
  <p class="foot">{'No tickets filed for this release.' if not tickets else ''}</p>
  {table(c2b, tickets, link_col=4, merge_cols=[0, 1, 2, 3], collapse_dash_from=4) if tickets else ''}
</div>

{latest_html}


<div class="card"><h2>Health-gate breakdown</h2>
  {gate_legend}
  <p class="foot" style="margin-top:10px">{gate_note}</p>
  <p class="foot">{esc(gate_near)}</p>
  {'<h3 style="margin:16px 0 6px;font-size:13px;color:var(--muted)">Crash / BSOD dumps</h3>' + table(crash_cols, crash_bd, link_col=1, col_widths=_cw_crash) if crash_bd else ''}
  {'<h3 style="margin:16px 0 6px;font-size:13px;color:var(--muted)">Resource gate violations</h3>' + table(ncrash_cols, ncrash_bd, link_col=1, col_widths=_cw_ncrash) if ncrash_bd else ''}
</div>

<div class="card"><h2>Resource peaks per process</h2>
  <div>{_GUIDE_LINK}</div>
  <div class="cpu-explain">
    <strong>How CPU peak is calculated:</strong>
    The sampler polls each process's CPU% every few seconds throughout the test run.
    <em>cpu_max_pct</em> is the highest single-sample reading observed.
    <strong>100 = one full CPU core</strong> (per-core scale, not percentage of total system CPU).
    So a value of 190 means the process was using ~1.9 CPU cores at its peak —
    on a 4-core VM that is ~47% of total system CPU, but reported as 190 here.
    <em>avg_cpu_peak</em> averages this peak across all runs; <em>worst_cpu_peak</em> is the single highest ever seen.
    <em>worst_sustained_s</em> = how many seconds the process was continuously above the gate threshold.
    <em>avg_threads_peak</em> / <em>worst_threads</em> = the highest thread count of each test (samples of a process that was not alive are skipped),
    averaged across the tests and the single highest ever seen; empty when the release has no thread samples.
    <em>cpu_gate</em> = {CPU_GATE}, the same for every process: a run fails when a gated process stays at or above it for 60 s in a row.
    <strong>Yellow dashed line in the chart</strong> = that gate. A bar above the line is not a failure by itself (the 60 s in a row is);
    demAgentSvc.exe is observe-only: it is measured against the line but never fails a run. Runs made before the gate was unified were gated
    at 140 or 240 per test; <em>worst_sustained_s</em> here is restated against {CPU_GATE} (a run whose highest reading is under {CPU_GATE} has 0 s above it).
  </div>
  {table(c3, res)}
  <div class="chart-wrap"><canvas id="cpuChart"></canvas></div>
</div>

{monitored_html}

{ver_win_card}
{mem_win_card}

{ver_mac_card}
{mem_mac_card}

<div class="card"><h2>Iteration checkpoints (iter 1 / iter 10 / deepest)</h2>
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

{seq_html}

<div class="card"><h2>Release summary — {esc(short_release(release))}</h2>
  <div class="summary-tabs">

    <div class="sumblock">
      <h3>Pass rate per test case</h3>
      <p class="foot">Pass% across all runs for this release, sorted lowest first (shows problem areas).</p>
      {table(c4a, pass_trend, link_col=0)}
    </div>

    <div class="sumblock">
      <h3>Version health overview</h3>
      <p class="foot">One row per product version: builds run, pass/fail counts,
        health-gate violations, crash dumps. Sorted by version ascending.</p>
      {table(c4b, stressed) if stressed else '<p class="foot">No build data.</p>'}
    </div>

  </div>
</div>

<div class="card"><h2>Per test case</h2>
  <p class="foot">Sorted alphabetically. Runs, duration, pass/fail, and whether a health gate or dump fired.</p>
  <p class="legend">
    <span class="sw warn"></span> at least one run FAILED &nbsp;&nbsp;
    <span class="sw gatewarn"></span> PASSED but a health gate fired &nbsp;&nbsp;
    <span class="sw none"></span> all runs clean
  </p>
  {table(c1, per_test_disp, highlight=test_hl, link_col=0)}
</div>

"""

    # CPU chart data. A process with only-NULL cpu peaks (no living samples)
    # yields NULL from avg()/max() -> coerce to 0.0 so the chart never crashes.
    def _f(v):
        return float(v) if v is not None else 0.0

    labels = [r[0] for r in res]
    avg_cpu = [_f(r[1]) for r in res]
    worst_cpu = [_f(r[2]) for r in res]

    # Cross-version trend: one Chart.js line dataset per process, x-axis =
    # sorted distinct major versions seen (140, 141, 142, ...). A gap (process
    # not yet observed at an earlier version) stays `null`, NOT 0.0 -- a 0
    # would falsely plot "measured and idle" where the truth is "didn't run
    # yet"; spanGaps draws the line straight through the null instead of
    # dropping to zero.
    _version_labels = sorted({r[0] for r in res_by_version if r[0] is not None})
    _worst_by_key = {
        (r[0], r[1]): float(r[3]) for r in res_by_version if r[3] is not None
    }
    _palette = [
        "#6ea8fe",
        "#f97316",
        "#22c55e",
        "#eab308",
        "#ec4899",
        "#8b5cf6",
        "#14b8a6",
        "#ef4444",
        "#84cc16",
        "#06b6d4",
    ]

    def _point_radius(data):
        """A series with a single measured version has no line to draw: show it as a large dot, or it is invisible."""
        return 7 if sum(v is not None for v in data) == 1 else 3

    def _version_datasets(by_key, processes):
        return [
            {
                "label": proc,
                "data": [by_key.get((v, proc)) for v in _version_labels],
                "borderColor": _palette[i % len(_palette)],
                "backgroundColor": _palette[i % len(_palette)],
                "pointRadius": _point_radius([by_key.get((v, proc)) for v in _version_labels]),
                "spanGaps": True,
                "tension": 0.25,
            }
            for i, proc in enumerate(processes)
        ]

    # _heavy_processes / _light_processes computed earlier (right after
    # res_by_version query) so the threshold value is available to the HTML
    # f-string above; reused here for the actual per-group dataset split.
    version_labels = [f"R{v}" for v in _version_labels]
    version_worst_heavy_datasets = _version_datasets(_worst_by_key, _heavy_processes)
    version_worst_light_datasets = _version_datasets(_worst_by_key, _light_processes)

    # mac trend: own version axis (R143+) and one dataset list per metric.
    _mac_versions = sorted({r[0] for r in res_by_version_mac if r[0] is not None})
    _mac_procs = sorted({r[1] for r in res_by_version_mac if r[1] is not None})
    _mac_worst = {(r[0], r[1]): float(r[3]) for r in res_by_version_mac if r[3] is not None}

    def _mac_datasets(by_key):
        return [
            {
                "label": proc,
                "data": [by_key.get((v, proc)) for v in _mac_versions],
                "borderColor": _palette[i % len(_palette)],
                "backgroundColor": _palette[i % len(_palette)],
                "pointRadius": _point_radius([by_key.get((v, proc)) for v in _mac_versions]),
                "spanGaps": True,
                "tension": 0.25,
            }
            for i, proc in enumerate(_mac_procs)
        ]

    mac_version_labels = [f"R{v}" for v in _mac_versions]
    mac_worst_datasets = _mac_datasets(_mac_worst)

    # memory trend datasets: one line per process; a missing version stays null (not measured), never 0
    def _mem_datasets(mem_rows, versions, by_idx, procs):
        by_key = {(r[0], r[1]): float(r[by_idx]) for r in mem_rows if r[by_idx] is not None}
        return [
            {
                "label": proc,
                "data": [by_key.get((v, proc)) for v in versions],
                "borderColor": _palette[i % len(_palette)],
                "backgroundColor": _palette[i % len(_palette)],
                "pointRadius": _point_radius([by_key.get((v, proc)) for v in versions]),
                "spanGaps": True,
                "tension": 0.25,
            }
            for i, proc in enumerate(procs)
        ]

    _mem_versions = sorted({r[0] for r in mem_by_version})
    _mem_peak_by_proc = {}
    for r in mem_by_version:
        for idx in (3, 4):
            if r[idx] is not None:
                _mem_peak_by_proc[r[1]] = max(_mem_peak_by_proc.get(r[1], 0.0), float(r[idx]))
    _mem_heavy = sorted(q for q, v in _mem_peak_by_proc.items() if v >= _HEAVY_MEM_MB)
    _mem_light = sorted(q for q, v in _mem_peak_by_proc.items() if v < _HEAVY_MEM_MB)
    _mem_mac_versions = sorted({r[0] for r in mem_by_version_mac})
    _mem_mac_procs = sorted({r[1] for r in mem_by_version_mac})

    # Embed chart data as real JSON, not Python list repr — decouples the JS from
    # Python's str() format (e.g. None vs null, quote style) and is injection-safe.
    return HTML_TEMPLATE.format(
        release=esc(short_release(release)),
        body=body,
        labels=json.dumps(labels),
        avg_cpu=json.dumps(avg_cpu),
        worst_cpu=json.dumps(worst_cpu),
        gate_value=CPU_GATE,
        version_labels=json.dumps(version_labels),
        version_worst_heavy_datasets=json.dumps(version_worst_heavy_datasets),
        version_worst_light_datasets=json.dumps(version_worst_light_datasets),
        mac_version_labels=json.dumps(mac_version_labels),
        mac_worst_datasets=json.dumps(mac_worst_datasets),
        mem_version_labels=json.dumps([f"R{v}" for v in _mem_versions]),
        mem_avg_heavy_datasets=json.dumps(_mem_datasets(mem_by_version, _mem_versions, 3, _mem_heavy)),
        mem_worst_heavy_datasets=json.dumps(_mem_datasets(mem_by_version, _mem_versions, 4, _mem_heavy)),
        mem_avg_light_datasets=json.dumps(_mem_datasets(mem_by_version, _mem_versions, 3, _mem_light)),
        mem_worst_light_datasets=json.dumps(_mem_datasets(mem_by_version, _mem_versions, 4, _mem_light)),
        mem_mac_version_labels=json.dumps([f"R{v}" for v in _mem_mac_versions]),
        mem_mac_avg_datasets=json.dumps(_mem_datasets(mem_by_version_mac, _mem_mac_versions, 3, _mem_mac_procs)),
        mem_mac_worst_datasets=json.dumps(_mem_datasets(mem_by_version_mac, _mem_mac_versions, 4, _mem_mac_procs)),
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
.topbar .nav-links {{ display:flex; align-items:center; gap:8px; margin-left:16px; }}
.topbar .nav-links a {{
  font-size:13px;
  padding:3px 11px;
  border-radius:999px;
  border:1px solid var(--border);
  color:var(--muted);
  text-decoration:none;
  font-weight:600;
}}
.topbar .nav-links a:hover {{ color:var(--cyan); border-color:var(--border2); background:rgba(0,199,230,0.08); }}
.topbar .nav-links a.api {{ margin-left:8px; color:var(--cyan); border-color:var(--border2); }}

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
a.badge.guide {{ display:inline-block; margin:0 0 12px; text-decoration:none; cursor:pointer; }}
a.badge.guide:hover {{ background:var(--warn); color:#05081a; }}

/* ── meta grid ── */
/* Explicit columns, no auto-fit, so no cell can overlap its neighbour:
   Tenant(s) | Product version(s) | Runs | Test duration | [Features enabled, one tenant only]. */
.meta-grid {{
  display:grid;
  /* Columns hug their content (Tenant capped at 320px, versions at 450px); the spare width is split into equal
     gaps between the 4 (all tenants) or 5 (one tenant) columns by space-between, so no column balloons. */
  grid-template-columns:fit-content(320px) fit-content(450px) max-content max-content;
  justify-content:space-between;
  gap:6px 28px;
  align-items:start;
}}
.meta-grid.has-feat {{ grid-template-columns:fit-content(320px) fit-content(240px) max-content max-content max-content; }}
.meta-grid dt {{ color:var(--muted); font-size:13px; text-transform:uppercase; letter-spacing:.5px; margin:0 0 4px; }}
.meta-grid dd {{ margin:0; font-family:'JetBrains Mono',Consolas,monospace; font-size:14px; color:var(--fg); }}
.meta-grid dd.big {{ font-size:22px; font-weight:600; }}
.meta-grid dd.versions {{ line-height:1.7; }}
/* "+N more": the same pill as the "How to read these numbers -- guide" link (a.badge.guide), minus its bottom margin. */
.meta-grid a.badge.more {{ margin:0 0 0 4px; white-space:nowrap; }}
.kpi-row + div a.badge.more {{ margin:0; white-space:nowrap; }}
dialog#numpanel p {{ margin:8px 0; font-size:13px; line-height:1.5; }}
dialog#numpanel td:nth-child(2) {{ font-family:inherit; padding-right:10px; }}
/* Features: always-visible panel, narrow, KPI-tile background, original vertical layout. */
.meta-grid .feat dd {{ position:relative; }}
.fpanel-always {{
  display:inline-block;
  width:max-content;
  max-width:200px;
  padding:10px 14px;
  background:var(--card2);
  border:1px solid var(--border);
  border-radius:var(--radius);
  position:relative;
}}
.fpanel-always::before {{
  content:""; display:block; position:absolute; top:0; left:0; right:0;
  height:2px; background:var(--grad); border-radius:var(--radius) var(--radius) 0 0;
}}
.fgrp {{ margin-bottom:9px; }}
.fgrp:last-child {{ margin-bottom:0; }}
.fgrp-name {{ display:block; font-size:10px; font-weight:700; letter-spacing:.8px; color:#4b5563;
              text-transform:uppercase; margin-bottom:5px; }}
.ftag-row {{ display:flex; flex-wrap:wrap; gap:4px; }}
.ftag {{ display:inline-flex; align-items:center; gap:4px; padding:2px 7px; border-radius:4px;
         font-size:12px; font-weight:500; font-family:'JetBrains Mono',monospace; }}
.ftag-on  {{ background:#052e16; color:#86efac; }}
.ftag-off {{ background:#111827; color:#374151; }}
.fdot {{ width:5px; height:5px; border-radius:50%; flex-shrink:0; }}
.ftag-on  .fdot {{ background:#22c55e; }}
.ftag-off .fdot {{ background:#374151; }}
@media (max-width:1100px) {{
  .meta-grid, .meta-grid.has-feat {{ grid-template-columns:1fr 1fr; justify-content:normal; }}
  .meta-grid .feat {{ grid-column:1 / -1; }}
  .meta-grid .feat dd {{ flex-direction:row; flex-wrap:wrap; }}
}}
/* Open panel: the AI chat git44 look -- purple #a78bfa left bar and a faint purple tint (card colour + 10% purple). */
dialog#verpanel {{ background:#20243c; color:var(--fg); border:1px solid var(--border); border-left:4px solid #a78bfa; border-radius:0 10px 10px 0; padding:0; width:min(560px,92vw); z-index:1000; box-shadow:0 12px 32px rgba(0,0,0,.55); }}
dialog#numpanel {{ background:#20243c; color:var(--fg); border:1px solid var(--border); border-left:4px solid #a78bfa; border-radius:0 10px 10px 0; padding:0; width:min(560px,92vw); z-index:1000; box-shadow:0 12px 32px rgba(0,0,0,.55); }}
dialog#verpanel::backdrop {{ background:rgba(2,5,16,.6); }}
dialog#numpanel::backdrop {{ background:rgba(2,5,16,.6); }}
dialog#verpanel .dh {{ display:flex; justify-content:space-between; align-items:center; padding:14px 18px; border-bottom:1px solid var(--border); }}
dialog#numpanel .dh {{ display:flex; justify-content:space-between; align-items:center; padding:14px 18px; border-bottom:1px solid var(--border); }}
dialog#verpanel .dh button {{ background:none; border:0; color:var(--muted); font-size:20px; cursor:pointer; }}
dialog#numpanel .dh button {{ background:none; border:0; color:var(--muted); font-size:20px; cursor:pointer; }}
dialog#verpanel .dl {{ max-height:60vh; overflow:auto; padding:6px 18px 14px; }}
dialog#numpanel .dl {{ max-height:60vh; overflow:auto; padding:6px 18px 14px; }}
dialog#verpanel table {{ width:100%; border-collapse:collapse; font-family:'JetBrains Mono',Consolas,monospace; font-size:13px; }}
dialog#numpanel table {{ width:100%; border-collapse:collapse; font-family:'JetBrains Mono',Consolas,monospace; font-size:13px; }}
dialog#verpanel th {{ color:var(--muted); text-align:left; font-weight:500; padding:8px 0; position:sticky; top:0; background:#20243c; }}
dialog#numpanel th {{ color:var(--muted); text-align:left; font-weight:500; padding:8px 0; position:sticky; top:0; background:#20243c; }}
dialog#verpanel .dh b {{ color:#a78bfa; }}
dialog#numpanel .dh b {{ color:#a78bfa; }}
dialog#verpanel td {{ padding:5px 0; border-top:1px solid var(--border); }}
dialog#numpanel td {{ padding:5px 0; border-top:1px solid var(--border); }}

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
td.mrg {{ vertical-align:top; background:rgba(255,255,255,0.025); border-right:1px solid var(--border); }}
tr.grp td {{ border-top:2px solid var(--border); }}
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
  display:inline-block; width:10px; height:10px; border-radius:50%;
  vertical-align:middle; margin-right:4px;
}}
.legend .sw.warn {{ background:var(--fail); opacity:.7; }}
.legend .sw.gatewarn {{ background:var(--warn); opacity:.7; }}
.legend .sw.none {{ background:var(--muted); opacity:.4; }}

/* ── chart ── */
.chart-wrap {{ position:relative; height:260px; margin-top:20px; }}
.chart-row {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
.chart-row.one {{ grid-template-columns:1fr; }}
@media(max-width:900px) {{ .chart-row {{ grid-template-columns:1fr; }} }}

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
a.sortlink {{ color:inherit; text-decoration:none; border-bottom:1px dashed currentColor; cursor:pointer; }}
a.sortlink:hover {{ opacity:.75; }}

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
a.tname {{ color:inherit; text-decoration:none; }}
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
.cpu-explain a {{ color:var(--cyan); font-weight:600; }}

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
  <div class="nav-links">
    <a href="/report/140" target="_blank" rel="noopener">140</a>
    <a href="/report/141" target="_blank" rel="noopener">141</a>
    <a href="/report/142" target="_blank" rel="noopener">142</a>
    <a href="/report/143" target="_blank" rel="noopener">143</a>
    <a href="/report/144" target="_blank" rel="noopener">144</a>
    <a href="/report/145" target="_blank" rel="noopener">145</a>
    <a class="api" href="/query" target="_blank" rel="noopener">Query API</a>
    <a class="api" href="/ai_chat" target="_blank" rel="noopener">AI chat</a>
    <a class="api" href="/doc" target="_blank" rel="noopener">Documents</a>
  </div>
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
       backgroundColor:'rgba(239,68,68,0.7)', borderColor:'#ef4444', borderWidth:1, borderRadius:4 }},
    {{ type:'line', label:'gate {gate_value} (fails only if CPU stays above it for 60 s)', data:[],
       borderColor:'#f59e0b', backgroundColor:'#f59e0b', borderWidth:2, borderDash:[8,5], pointRadius:0 }}
  ]}},
  options:{{
    responsive:true, maintainAspectRatio:false,
    plugins:{{
      legend:{{ position:'bottom', labels:{{ usePointStyle:true, pointStyle:'circle', boxWidth:10, boxHeight:10, padding:16 }} }},
      title:{{ display:true, text:'CPU peak per process (100 = 1 core)', color:'#94a3b8', font:{{size:12}} }}
    }},
    scales:{{
      x:{{ grid:{{ color:'rgba(255,255,255,0.04)' }}, ticks:{{ color:'#94a3b8' }} }},
      y:{{ beginAtZero:true, suggestedMax:350, grid:{{ color:'rgba(255,255,255,0.04)' }}, ticks:{{ color:'#94a3b8' }} }}
    }}
  }},
  plugins:[{{
    id:'gateLine',
    afterDatasetsDraw(chart) {{
      const a = chart.chartArea, y = chart.scales.y.getPixelForValue({gate_value}), c = chart.ctx;
      c.save(); c.setLineDash([8,5]); c.strokeStyle = '#f59e0b'; c.lineWidth = 2;
      c.beginPath(); c.moveTo(a.left, y); c.lineTo(a.right, y); c.stroke(); c.restore();
    }}
  }}]
}});

function makeVersionChart(canvasId, datasets, titleText, labels, gate) {{
  const el = document.getElementById(canvasId);
  if (!el) return;  // card hidden for this platform view
  // gate (optional): draw the yellow dashed CPU gate across the plot and keep the axis at least 70 above it, like the per-process chart
  const plugins = gate ? [{{
    id:'gateLine',
    afterDatasetsDraw(chart) {{
      const a = chart.chartArea, y = chart.scales.y.getPixelForValue(gate), c = chart.ctx;
      c.save(); c.setLineDash([8,5]); c.strokeStyle = '#f59e0b'; c.lineWidth = 2;
      c.beginPath(); c.moveTo(a.left, y); c.lineTo(a.right, y); c.stroke(); c.restore();
    }}
  }}] : [];
  if (gate) datasets = datasets.concat([{{ label:'gate ' + gate + ' (fails only after 60 s in a row)', data:[], borderColor:'#f59e0b',
                                          backgroundColor:'#f59e0b', borderWidth:2, borderDash:[8,5], pointRadius:0 }}]);
  new Chart(el, {{
    type:'line',
    data:{{ labels:labels || {version_labels}, datasets:datasets }},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:{{
        legend:{{ position:'bottom', labels:{{ usePointStyle:true, pointStyle:'circle', boxWidth:10, boxHeight:10, padding:16 }} }},
        title:{{ display:true, text:titleText, color:'#94a3b8', font:{{size:12}} }}
      }},
      scales:{{
        x:{{ offset:true, grid:{{ color:'rgba(255,255,255,0.04)' }}, ticks:{{ color:'#94a3b8' }} }},
        y:{{ beginAtZero:true, suggestedMax: gate ? gate + 70 : undefined, grid:{{ color:'rgba(255,255,255,0.04)' }}, ticks:{{ color:'#94a3b8' }} }}
      }}
    }},
    plugins:plugins
  }});
}}
makeVersionChart('cpuVersionWorstHeavyChart', {version_worst_heavy_datasets}, 'worst CPU peak — heavy processes, by version (100 = 1 core)', undefined, {gate_value});
makeVersionChart('cpuVersionWorstLightChart', {version_worst_light_datasets}, 'worst CPU peak — light processes, by version (100 = 1 core)');
makeVersionChart('cpuVersionWorstMacChart', {mac_worst_datasets}, 'mac: worst CPU peak by version (100 = 1 core)', {mac_version_labels});
makeVersionChart('memVersionAvgHeavyChart', {mem_avg_heavy_datasets}, 'avg memory peak — heavy processes, by version (MB)', {mem_version_labels});
makeVersionChart('memVersionWorstHeavyChart', {mem_worst_heavy_datasets}, 'worst memory peak — heavy processes, by version (MB)', {mem_version_labels});
makeVersionChart('memVersionAvgLightChart', {mem_avg_light_datasets}, 'avg memory peak — light processes, by version (MB)', {mem_version_labels});
makeVersionChart('memVersionWorstLightChart', {mem_worst_light_datasets}, 'worst memory peak — light processes, by version (MB)', {mem_version_labels});
makeVersionChart('memVersionAvgMacChart', {mem_mac_avg_datasets}, 'mac: avg memory peak by version (MB, RSS)', {mem_mac_version_labels});
makeVersionChart('memVersionWorstMacChart', {mem_mac_worst_datasets}, 'mac: worst memory peak by version (MB, RSS)', {mem_mac_version_labels});

// Iteration checkpoints table: 'test' = by test case name (click again to reverse),
// 'deepest' = the default order (deepest iteration first, ties by name).
function sortCkpt(key) {{
  const t = document.getElementById('ckptTable');
  if (!t) return;
  const tb = t.tBodies[0];
  const rows = Array.from(tb.rows);
  const byName = (a, b) => a.dataset.test.localeCompare(b.dataset.test);
  if (key === 'test') {{
    const dir = t.dataset.testDir === 'asc' ? 'desc' : 'asc';
    t.dataset.testDir = dir;
    rows.sort((a, b) => dir === 'asc' ? byName(a, b) : byName(b, a));
  }} else {{
    t.dataset.testDir = '';
    rows.sort((a, b) => (parseInt(b.dataset.deepest, 10) - parseInt(a.dataset.deepest, 10)) || byName(a, b));
  }}
  rows.forEach(r => tb.appendChild(r));
}}
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


# What the page shows for a process whose sampler name carries command-line text. The DB, the gates and the matching keep the
# full sampler name; only the page shows the executable name (owner 2026-11-13: process name only, no parameters).
PROCESS_DISPLAY_NAMES = {
    "MacOS/epais --config-dir": "epais",
    "/shuttle --bootstrap-config": "shuttle",
}


def show_process_names(html_text):
    """Replace each sampler name in PROCESS_DISPLAY_NAMES by its display name in the finished page (charts, tables, text)."""
    for raw, shown in PROCESS_DISPLAY_NAMES.items():
        html_text = html_text.replace(raw, shown)
    return html_text


def build_report(release, top, tenant=None, platform=None, stack=None, version=None):
    return show_process_names(_build_report_raw(release, top, tenant, platform, stack, version))


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
    ap.add_argument(
        "--stack",
        default=None,
        choices=_STACKS,
        help="filter to every known tenant in one stack (overrides --tenant)",
    )
    ap.add_argument(
        "--version",
        default=None,
        help="product version filter: 'ehf' for every EHF build, 'non-ehf' to drop every patch build (EHF and the X.M.N lines such as "
        "140.1.4.2805), or one exact version such as 143.0.2.2839",
    )
    a = ap.parse_args(argv)
    if a.version and a.version not in (EHF_TOKEN, NONEHF_TOKEN) and not VERSION_RE.match(a.version):
        ap.error("--version must be 'ehf', 'non-ehf' or a version like 143.0.2.2839")

    html_out = build_report(a.release, a.top, a.tenant, a.platform, a.stack, a.version)

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
