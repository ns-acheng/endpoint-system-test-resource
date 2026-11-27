#!/usr/bin/env python
"""Ingest one SystemTest build's artifacts into the results DB.

Reads the artifacts the suite ALREADY produces (nothing new is collected) and
loads them into the Postgres schema (schema.sql):

    <build_dir>/
        iterations_<run_id>.ndjson        -> test_run / test_result / iteration_result
        resource_samples/<test>.jsonl     -> process_summary + gate_violation + dump
        resource_samples/<test>.csv        (ignored; jsonl is the source of truth)

The per-process health-gate summary is only LOGGED by the suite, never persisted,
so we recompute it here by importing the suite's own health_gate functions — this
keeps the reduction identical to what the gate saw at test time (no logic drift).

Connection settings resolve through dbconn.py (env or ~/.grs_db.conf — never
hard-coded here). Normally driven via db.py; runnable standalone too.

Idempotent: test_run.run_id is UNIQUE. Re-ingesting the same build is skipped
unless --replace is given (which deletes the run row and cascades, then reloads).

Usage:
    python ingest.py <build_dir> [--release release-140] [--replace] [--dry-run]
"""

import argparse
import ast
import glob
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone

from epais_builds import EPAIS_144_BUILDS

# Import the suite's own health-gate reduction so the DB summary matches the
# GRS repo root for importing the suite's health_gate (matches the gate
# verdict exactly). Resolve from env var GRS_REPO_ROOT, else search the
# standard co-located clone roots (git / git22 / git33), else fall back to
# the old relative path (two levels up) for backward compat when run from a
# GRS clone. The report subcommand does NOT need this; only ingest does.
_CLONE_CANDIDATES = [
    os.environ.get("GRS_REPO_ROOT"),
    r"C:\\git\\nsclient_golden_regression_suite",
    r"C:\\git22\\nsclient_golden_regression_suite",
    r"C:\\git33\\nsclient_golden_regression_suite",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
]
_REPO_ROOT = next(
    (
        c
        for c in _CLONE_CANDIDATES
        if c
        and os.path.isfile(
            os.path.join(
                c, "golden_regression", "workflows", "system_test", "health_gate.py"
            )
        )
    ),
    None,
)
if _REPO_ROOT and _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _load_health_gate(repo_root):
    """Load health_gate.py by FILE PATH, bypassing golden_regression.workflows'
    and golden_regression.workflows.system_test's own __init__.py.

    health_gate.py's own docstring says it is "Dependency-free (no SSH, no
    webui)" -- and it is, zero import statements. But a normal `from
    golden_regression.workflows.system_test.health_gate import ...` still has
    to execute golden_regression/workflows/__init__.py first (which eagerly
    imports all 22 workflow classes, several webapi/SSH-backed) and
    golden_regression/workflows/system_test/__init__.py (which imports the
    3000+-line SystemTestWorkflow). Confirmed live 2026-09-06 setting up a
    second (backup) ingester on SYS-07: reaching this one pure module through
    the normal import path forced installing the GRS repo's FULL
    requirements_linux_py3_11.txt (280+ packages -- selenium, playwright,
    numpy, pandas, kubernetes...) for zero functional benefit, since this
    module never touches any of them. Loading the file directly skips both
    __init__.py files entirely -- the DB tool now only needs a golden_regression
    checkout + psycopg2, not the whole test-automation framework."""
    path = os.path.join(
        repo_root, "golden_regression", "workflows", "system_test", "health_gate.py"
    )
    spec = importlib.util.spec_from_file_location("grs_health_gate_standalone", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


from golden_regression.tests.SystemTest.test_data import Configuration  # noqa: E402

if _REPO_ROOT:
    _health_gate = _load_health_gate(_REPO_ROOT)
    check_thresholds = _health_gate.check_thresholds
    max_sustained_cpu_s = _health_gate.max_sustained_cpu_s
    summarize_samples = _health_gate.summarize_samples
else:
    # No repo root resolved at all -- surface the same missing-dependency
    # error a direct import would have, rather than a confusing NoneType.
    from golden_regression.workflows.system_test.health_gate import (  # noqa: E402
        check_thresholds,
        max_sustained_cpu_s,
        summarize_samples,
    )

# GlobalData uses a singleton metaclass, so `Configuration` is already an
# instance (calling it raises "not callable"). Use it directly.
CFG = Configuration


def _scan_health_cpu_max_overrides(repo_root):
    """test_name -> N for every `@pytest.mark.health_cpu_max(N)` decorator in
    the suite. Report/ingest were recomputing cpu_sustained gate hits against
    the GLOBAL CFG.CPU_SUSTAIN_PCT default for every test, ignoring that
    stress_01/stress_02/etc. carry their own raised threshold (280) because
    they are intentionally CPU-heavy by design — that produced 9 false
    "gate_hits" on tenant 1457 (report /report/142?tenant=1457) for tests
    that actually PASSED their real, higher gate at run time. AST-parsed
    (not hand-copied) so this can never drift from the real decorators."""
    overrides = {}
    if not repo_root:
        return overrides
    pattern = os.path.join(
        repo_root, "golden_regression", "tests", "SystemTest", "test_*.py"
    )
    for path in glob.glob(pattern):
        try:
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith(
                "test_"
            ):
                continue
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "health_cpu_max"
                    and dec.args
                    and isinstance(dec.args[0], ast.Constant)
                ):
                    overrides[node.name] = dec.args[0].value
    return overrides


_HEALTH_CPU_MAX_OVERRIDES = _scan_health_cpu_max_overrides(_REPO_ROOT)

# ─────────────────────────── helpers ───────────────────────────


def _ts(epoch):
    """Unix epoch (seconds) -> aware UTC datetime, or None."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    except (ValueError, OSError, TypeError):
        return None


def _suite_of(test_name):
    """Derive the suite bucket from a test name (best-effort, telemetry only)."""
    n = (test_name or "").lower()
    for key in ("stress", "ipc", "power", "upgrade", "steer", "net"):
        if key in n:
            return key.upper()
    return None


def _release_ordinal(label):
    """'release-140' / 'R140' / '140' -> 140, else None."""
    m = re.search(r"(\d+)", label or "")
    return int(m.group(1)) if m else None


def _release_from_version(version):
    """Installed client version -> release label. '140.0.0.2739' -> 'release-140'.
    Returns None if the version doesn't start with a major number."""
    m = re.match(r"\s*(\d+)\b", version or "")
    return f"release-{m.group(1)}" if m else None


# Feature / early-stage builds carry a synthetic high major (e.g. 211.x.x.x,
# 200.x.x.x from client-feature-builds). A version whose major is >= this bound is a
# feature build. Since 2026-11-03 it is ingested and filed under the current release
# line (see insert_model); it used to be skipped, which hid its gate violations.
FEATURE_BUILD_MAJOR_MIN = 200

# TEMP/diagnostic test_case names that were never a spec case and have since
# been removed from the repo entirely -- a one-time `db.py purge-test-name
# --fix` is not durable because harvest runs HOURLY off Jenkins artifacts, and
# a build whose artifact still carries the old iterations ndjson (e.g. REG
# build 143, test_steer_06_poc_dns_push_query_3x) gets silently re-ingested on
# the very next pull. Filtering at ingest time is the only fix that survives
# the next harvest cycle. Add a name here, don't just delete the DB row.
EXCLUDED_TEST_NAMES = {"test_steer_06_poc_dns_push_query_3x"}


def _is_feature_build_version(version):
    """True if the installed version is a feature/early-stage build (major >= 200,
    e.g. 211.100.100.9740). None/unparseable -> False (don't skip on unknowns)."""
    m = re.match(r"\s*(\d+)\b", version or "")
    return bool(m) and int(m.group(1)) >= FEATURE_BUILD_MAJOR_MIN


# Feature builds numbered like a release (142.100.100.<build>) that are tested for the current line: every run on them is filed there,
# whatever the cases are (10603 was filed under release-142 for its stress and install runs while its EPAIS runs went to 144).

def _is_epais_feature_run(version, tests):
    """True for a run on one of the named EPAIS builds (epais_builds.EPAIS_144_BUILDS, 142.100.100.<build>): it belongs to the
    current line though its client is numbered like the previous release. Not every feature build is an EPAIS build, so a run
    whose cases are all EPAIS cases but whose build is not in the list is NOT moved (owner, 2026-11-11)."""
    return (version or "").strip() in EPAIS_144_BUILDS


def _current_line_label(conn):
    """The highest numbered release label in the DB ('release-144'), the line a feature build is tested for; None on an empty DB."""
    with conn.cursor() as cur:
        cur.execute("SELECT label FROM release WHERE label ~ '^release-[0-9]+$' ORDER BY ordinal DESC LIMIT 1")
        row = cur.fetchone()
    return row[0] if row else None


def is_all_skip(statuses):
    """True iff there is at least one case and EVERY one is SKIP (run measured nothing).

    Public because db.py's `gaps` labelling asks the same question about a stored
    run: a second copy of the predicate would drift (ledger 41) and the two tools
    would disagree about whether a build was skipped by design or is a real gap.
    """
    seen = list(statuses)
    return bool(seen) and all(s == "SKIP" for s in seen)


def _read_ndjson(path):
    """Yield parsed objects from an NDJSON file, skipping blank/garbled lines."""
    # errors="replace": test artifacts occasionally carry non-UTF8 bytes (log
    # fragments); a decode error must not abort the whole ingest — the bad line
    # just fails json.loads below and is skipped.
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# ─────────────────── gate-violation string parsing ───────────────────
# check_thresholds returns human-readable strings; parse them back to the
# structured gate_violation rows. Patterns mirror health_gate.py exactly.
_GV_PATTERNS = [
    ("crash_dump", re.compile(r"^crash dumps present: (?P<detail>.+)$")),
    ("bsod_dump", re.compile(r"^BSOD dumps present: (?P<detail>.+)$")),
    ("bsod_event", re.compile(r"^BSOD event: (?P<detail>.+)$")),
    (
        "reboot",
        re.compile(
            r"^unexpected reboots: boots=(?P<observed>\d+).*expected<=(?P<threshold>-?\d+)"
        ),
    ),
    (
        "cpu_sustained",
        re.compile(
            r"^(?P<proc>\S+) CPU sustained >= (?P<threshold>[\d.]+)% for (?P<observed>[\d.]+)s"
        ),
    ),
    (
        "mem_ceiling",
        re.compile(
            r"^(?P<proc>\S+) mem_max (?P<observed>[\d.]+)MB > ceiling (?P<threshold>[\d.]+)MB"
        ),
    ),
    (
        "mem_growth",
        re.compile(
            r"^(?P<proc>\S+) mem growth (?P<observed>[\d.]+)MB.*> (?P<threshold>[\d.]+)MB"
        ),
    ),
    (
        "handles",
        re.compile(
            r"^(?P<proc>\S+) handles_max (?P<observed>\d+) >= (?P<threshold>\d+)"
        ),
    ),
]


def _parse_violation(msg):
    """Map one violation string to (type, process, observed, threshold, detail)."""
    for vtype, pat in _GV_PATTERNS:
        m = pat.search(msg)
        if not m:
            continue
        gd = m.groupdict()
        obs = gd.get("observed")
        thr = gd.get("threshold")
        return {
            "violation_type": vtype,
            "process_name": gd.get("proc"),
            "observed": float(obs) if obs is not None else None,
            "threshold": float(thr) if thr is not None else None,
            "detail": msg,
        }
    return {
        "violation_type": "other",
        "process_name": None,
        "observed": None,
        "threshold": None,
        "detail": msg,
    }


# first violation type becomes test_result.failing_gate (single-label summary)
def _primary_gate(violations):
    return violations[0]["violation_type"] if violations else None


# ─────────────────── artifact -> in-memory model ───────────────────


class NoData(Exception):
    """A build directory has no ingestable test data (e.g. an ABORTED build with
    no iterations_*.ndjson). Callers catch this to skip the build gracefully —
    it is NOT a fatal error, so it must not be SystemExit (batch import relies on
    continuing past it)."""


def _run_build_number(path):
    """The build_number the 'run' record inside this ndjson claims, or None.

    Read from the record, NOT parsed out of the filename: the filename is only a
    convention, the record is the data we are about to insert.
    """
    for obj in _read_ndjson(path):
        if obj.get("record_type") == "run":
            return _int(obj.get("build_number"))
    return None


def _pick_ndjson(ndjson_files, expect_build=None):
    """Choose WHICH iterations_*.ndjson describes the build being ingested.

    A Jenkins backup zip regularly carries a PREVIOUS build's ndjson: when a run
    dies before writing its own iterations file, the archived workspace still
    holds the last successful one (the stale-artifact trap in
    memory/reference_diagnostic_red_herrings.md). So a build dir can legitimately
    contain several, and picking the wrong one attributes another build's results
    to this one.

    Order of preference:
      1. the file whose 'run' record build_number == expect_build (the caller
         knows which build it asked Jenkins for) — exact, not a heuristic;
      2. otherwise the HIGHEST build_number present. `sorted(...)[0]` used to be
         used here, which is lexicographic and therefore picks the LOWEST/oldest
         ('...-47' sorts before '...-9'), i.e. it preferred the stale copy.

    Returns (path, picked_build, is_exact_match).
    """
    numbered = [(p, _run_build_number(p)) for p in sorted(ndjson_files)]
    if expect_build is not None:
        for p, n in numbered:
            if n is not None and n == expect_build:
                return p, n, True
    with_num = [(p, n) for p, n in numbered if n is not None]
    if with_num:
        p, n = max(with_num, key=lambda pn: pn[1])
        return p, n, False
    return numbered[0][0], None, False


def load_build(build_dir, expect_build=None):
    """Parse one build directory into a dict model ready for DB insert.
    Raises NoData if the build has no ndjson / no run record (skippable).

    ``expect_build``: the Jenkins build number the caller fetched. When given and
    the directory holds only another build's ndjson, this raises NoData rather
    than silently ingesting the wrong build (see _pick_ndjson).
    """
    ndjson_files = glob.glob(os.path.join(build_dir, "iterations_*.ndjson"))
    if not ndjson_files:
        # also accept a bare iterations.ndjson or a nested system_test_artifacts/
        ndjson_files = glob.glob(
            os.path.join(build_dir, "**", "iterations_*.ndjson"), recursive=True
        )
    if not ndjson_files:
        raise NoData(f"no iterations_*.ndjson under {build_dir}")

    expect_build = _int(expect_build)
    ndjson_path, picked_build, exact = _pick_ndjson(ndjson_files, expect_build)
    if expect_build is not None and not exact:
        # Every ndjson here belongs to some OTHER build: this build produced none
        # of its own (died before writing one) and the zip carries a neighbour's.
        # Skippable, not fatal — but it must never be ingested as this build.
        raise NoData(
            f"build #{expect_build}: no ndjson with its own run record "
            f"(found build_number={picked_build!r} in {os.path.basename(ndjson_path)} "
            f"— stale artifact from an earlier build); nothing of this build's own to ingest"
        )
    if len(ndjson_files) > 1:
        print(
            f"[pick] {len(ndjson_files)} ndjson files in {build_dir}; "
            f"using {os.path.basename(ndjson_path)} (build_number={picked_build!r})"
        )

    run = None
    tests = {}  # test_case -> test record
    iters = {}  # test_case -> [iteration records]
    for obj in _read_ndjson(ndjson_path):
        rt = obj.get("record_type")
        if rt == "run":
            run = obj
        elif rt == "test":
            if obj["test_case"] in EXCLUDED_TEST_NAMES:
                continue
            tests[obj["test_case"]] = obj
        elif rt == "iteration":
            iters.setdefault(obj["test_case"], []).append(obj)

    if run is None:
        raise NoData(f"no 'run' record in {ndjson_path}")

    if not tests:
        raise NoData(
            f"every test_case in {ndjson_path} is in EXCLUDED_TEST_NAMES "
            f"-- nothing of this build's own to ingest"
        )

    session_start = run.get("started_at")
    model = {"run": run, "ndjson_path": ndjson_path, "tests": []}

    for test_case, trec in tests.items():
        it_list = sorted(iters.get(test_case, []), key=lambda x: x.get("iter_index", 0))
        samples = _load_samples(build_dir, test_case)
        summary = (
            summarize_samples(
                samples, baseline_n=getattr(CFG, "MEM_BASELINE_SAMPLES", 3)
            )
            if samples
            else None
        )

        proc_rows, gate_msgs, dumps = [], [], []
        if summary:
            interval = getattr(CFG, "SAMPLE_INTERVAL_S", 10)
            # Per-test @pytest.mark.health_cpu_max(N) override wins over the
            # global default (matches what conftest.py actually gated on at
            # test time -- see _scan_health_cpu_max_overrides above).
            cpu_overrides = {}
            sustain_pct = _HEALTH_CPU_MAX_OVERRIDES.get(
                test_case, getattr(CFG, "CPU_SUSTAIN_PCT", 120)
            )
            if test_case in _HEALTH_CPU_MAX_OVERRIDES:
                cpu_overrides["cpu_sustain_pct"] = sustain_pct
            for pname, ps in summary.get("processes", {}).items():
                proc_rows.append(
                    {
                        "process_name": pname,
                        "cpu_max_pct": ps.get("cpu_max"),
                        "cpu_sustained_s": max_sustained_cpu_s(
                            samples, pname, sustain_pct, interval
                        ),
                        "cpu_threshold": sustain_pct,
                        "mem_baseline_mb": ps.get("mem_baseline"),
                        "mem_max_mb": ps.get("mem_max"),
                        "mem_growth_mb": ps.get("mem_growth"),
                        "handles_max": ps.get("handles_max"),
                        "alive_pct": ps.get("alive_pct"),
                        "sample_count": summary.get("sample_count"),
                    }
                )
            # The handle limit a build is judged by depends on its client major (1500 up to R144, 1300 from R145), so
            # re-ingesting an old build can never apply the newer limit. A health_gate without client_major_of (an older
            # GRS checkout) or without the keyword keeps the old behaviour.
            gate_kwargs = {}
            _major_of = getattr(_health_gate, "client_major", None)
            if _major_of is not None:
                gate_kwargs["client_major"] = _major_of(trec.get("client_version") or run.get("client_version") or "")
            try:
                gate_msgs = check_thresholds(
                    summary,
                    samples,
                    CFG,
                    session_start_ts=session_start,
                    overrides=cpu_overrides,
                    **gate_kwargs,
                )
            except TypeError:  # an older health_gate.check_thresholds that does not know client_major
                gate_msgs = check_thresholds(
                    summary,
                    samples,
                    CFG,
                    session_start_ts=session_start,
                    overrides=cpu_overrides,
                )
            dumps = _extract_dumps(summary)

        violations = [_parse_violation(m) for m in gate_msgs]
        model["tests"].append(
            {
                "test": trec,
                "iterations": it_list,
                "process_summary": proc_rows,
                "raw_samples": _flatten_samples(samples),
                "violations": violations,
                "dumps": dumps,
                "gate_violated": bool(gate_msgs),
                "failing_gate": _primary_gate(violations),
                "has_dump": bool(dumps),
            }
        )
    return model


def _load_samples(build_dir, test_case):
    """Load the resource_samples jsonl for a test, tolerant of both artifact
    layouts seen in the wild:

        <build_dir>/**/<test_case>/resource_samples.jsonl   (backup zip layout —
            each test gets its own folder, fixed file name)
        <build_dir>/**/resource_samples/<test_case>.jsonl   (flat layout)

    Match the test folder / stem on a normalized (alnum-lowered) key so casing
    and pytest-node decoration don't defeat it."""
    want = re.sub(r"[^a-z0-9]", "", test_case.lower())
    # layout 1: a folder named after the test containing resource_samples.jsonl
    for path in glob.glob(
        os.path.join(build_dir, "**", "resource_samples.jsonl"), recursive=True
    ):
        folder = re.sub(
            r"[^a-z0-9]", "", os.path.basename(os.path.dirname(path)).lower()
        )
        if want and (want in folder or folder in want):
            return list(_read_ndjson(path))
    # layout 2: a resource_samples/ dir with per-test jsonl files
    for path in glob.glob(
        os.path.join(build_dir, "**", "resource_samples", "*.jsonl"), recursive=True
    ):
        stem = re.sub(
            r"[^a-z0-9]", "", os.path.splitext(os.path.basename(path))[0].lower()
        )
        if want and (want in stem or stem in want):
            return list(_read_ndjson(path))
    return []


def _flatten_samples(samples):
    """Flatten raw jsonl ticks into (process x tick) rows for resource_sample.

    Each jsonl line is one tick: {ts, boot_id, processes:{name:{cpu_pct,
    cpu_pct_per_core, mem_mb, handles, alive, ...}}}. elapsed_s is seconds since
    the FIRST tick's ts (test-relative), matching the single-session report's
    time axis. Returns [] for an empty series."""
    if not samples:
        return []
    t0 = next((s.get("ts") for s in samples if s.get("ts") is not None), None)
    rows = []
    for s in samples:
        ts = s.get("ts")
        elapsed = int(ts - t0) if (ts is not None and t0 is not None) else None
        boot = s.get("boot_id")
        sampled_at = _ts(ts)
        # Tick-level throughput + load phase (additive; may be absent on older
        # artifacts). Denormalized onto each process row for this tick.
        net_in = s.get("net_mbps_in")
        net_out = s.get("net_mbps_out")
        phase = s.get("load_phase")
        for pname, info in (s.get("processes") or {}).items():
            rows.append(
                {
                    "process_name": pname,
                    "elapsed_s": elapsed,
                    "sampled_at": sampled_at,
                    "boot_id": boot,
                    "cpu_pct": info.get("cpu_pct"),
                    "cpu_pct_per_core": info.get("cpu_pct_per_core"),
                    "mem_mb": info.get("mem_mb"),
                    "handles": info.get("handles"),
                    "alive": info.get("alive"),
                    "threads": info["threads"] if "threads" in info else info.get("num_threads"),
                    "net_mbps_in": net_in,
                    "net_mbps_out": net_out,
                    "load_phase": phase,
                }
            )
    return rows


def _extract_dumps(summary):
    """Flatten crash/bsod dumps from the summary into dump rows."""
    rows = []
    for p in summary.get("crash_dumps") or []:
        rows.append(
            {
                "dump_type": "crash",
                "file_path": p,
                "file_name": os.path.basename(p),
                "process_name": _proc_from_dump(p),
            }
        )
    for b in summary.get("bsod_dumps") or []:
        path = b["path"] if isinstance(b, dict) else b
        rows.append(
            {
                "dump_type": "bsod",
                "file_path": path,
                "file_name": os.path.basename(path),
                "size_bytes": b.get("size") if isinstance(b, dict) else None,
                "captured_at": _ts(b.get("mtime") if isinstance(b, dict) else None),
            }
        )
    for ev in summary.get("bsod_events") or []:
        rows.append(
            {
                "dump_type": "bsod_event",
                "event_id": ev.get("event_id"),
                "captured_at": _ts(ev.get("ts")),
                "note": ev.get("detail"),
            }
        )
    return rows


def _proc_from_dump(path):
    low = (path or "").lower()
    for p in ("stagentsvcmon", "stagentsvc", "stagentui"):
        if p in low:
            return p
    return None


# ─────────────────────────── DB insert ───────────────────────────


def _release_id(cur, label):
    if not label:
        return None
    cur.execute("SELECT id FROM release WHERE label=%s", (label,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO release (label, ordinal) VALUES (%s, %s) RETURNING id",
        (label, _release_ordinal(label)),
    )
    return cur.fetchone()[0]


def _old_server_twin(cur, run_id, started):
    """The run_id of the old-server row (`<job prefix>o<N>[-dupK][-xK]`) holding the same execution as `run_id`, or None.

    Old-server runs were relabelled on 2026-10-30 (relabel_old_server_runs.py): the same execution already sits under
    that label. Same build number and a start time within 300 s count as the same execution."""
    m = re.match(r"^(?P<p>.*-)(?P<n>\d+)$", run_id)
    if not (m and started):
        return None
    cur.execute(
        "SELECT run_id FROM test_run WHERE run_id ~ %s AND abs(extract(epoch FROM started_at - %s)) <= 300 LIMIT 1",
        ("^" + re.escape(m.group("p")) + "o" + m.group("n") + r"(-dup\d+)?(-x\d+)?$", started),
    )
    row = cur.fetchone()
    return row[0] if row else None


def insert_model(conn, model, release_label=None, replace=False, commit=True):
    from psycopg2.extras import execute_values

    run = model["run"]
    run_id = run["run_id"]

    # Feature-build gate (BEFORE any DB state change): a feature/early-stage build
    # (major >= 200, e.g. 211.100.100.9740 from a --build_url feature MSI) is not
    # part of the standard 1xx system-test line and must never enter the DB. Check
    # up front so we neither delete an existing row nor write a new one for it.
    product_version = next(
        (
            t["test"].get("client_version")
            for t in model["tests"]
            if t["test"].get("client_version")
        ),
        None,
    )
    if _is_feature_build_version(product_version):
        # Feature builds are kept and shown (rule from the chat, 2026-11-03: "we do not exclude feature build result. so they must
        # be showed"). Their synthetic major (211.x) has no release of its own, so the run is filed under the line being tested,
        # unless the caller named a release. Skipping them dropped the demAgentSvc crash of REG2 build 311 without a trace.
        if not release_label:
            release_label = _current_line_label(conn)
        print(
            f"[feature] run_id={run_id} feature build v={product_version}: ingested under release {release_label!r}"
        )

    elif _is_epais_feature_run(product_version, model["tests"]):
        # EPAIS feature builds are numbered like a release (142.100.100.10575, 142.100.100.10603) but are tested for the current line
        # (owner, 2026-11-09: "all epais start from 144"). A run whose cases are all EPAIS cases on such a client is filed under it.
        if not release_label:
            release_label = _current_line_label(conn)
        print(f"[epais] run_id={run_id} EPAIS feature build v={product_version}: ingested under release {release_label!r}")

    # All-skip gate (also BEFORE any DB state change): a run whose every case was
    # SKIPped measured nothing — REG build 358 (0 pass / 0 fail / 1 skip) still
    # FINISHED SUCCESS and landed in the DB, diluting every rollup. Predicate is
    # "every case is SKIP", not "no pass and no fail", so an all-ERROR run (a
    # status the schema reserves but the producer does not emit today) still
    # ingests — ERROR carries signal. 0-test runs keep the legitimate-empty path.
    tests = model["tests"]
    if is_all_skip(t["test"].get("test_status") for t in tests):
        print(f"[skip] run_id={run_id} all {len(tests)} case(s) SKIP — not ingested")
        return False

    with conn.cursor() as cur:
        # Replaying an old store slot must not insert a second row under the plain id of an old-server run.
        _o = _old_server_twin(cur, run_id, _ts(run.get("started_at")))
        if _o:
            print(f"[skip] run_id={run_id} already ingested as the old-server run {_o}")
            return False
        cur.execute(
            """
            SELECT t.id, t.started_at, (SELECT count(*) FROM test_result WHERE run_id=t.id)
            FROM test_run t WHERE t.run_id=%s
        """,
            (run_id,),
        )
        row = cur.fetchone()
        n_tests_in_model = len(model.get("tests", []))
        # partial write = test_run exists, 0 test_result rows, but ndjson has tests
        # legitimate empty = ndjson also has 0 tests (infra failure / build cancelled)
        existing = row if row and row[2] > 0 else None
        partial = row and row[2] == 0 and n_tests_in_model > 0

        # Jenkins build-number reuse (job counter reset -- confirmed live on
        # MAC2 2026-09: builds 56-62 collided with a completely different set
        # of runs 5 days later, silently dropped by the plain "already
        # ingested" skip below). run_id is derived only from job+build
        # (Jenkins' own BUILD_TAG convention), never from a timestamp, so the
        # SAME run_id string can legitimately belong to two unrelated
        # executions months apart if the job gets recreated/reset. Detect it
        # by comparing the incoming ndjson's real started_at against the
        # existing row's: a genuine re-ingest of the SAME run has an
        # identical (or near-identical) timestamp; a reused build number
        # differs by hours/days. Reclaim by renaming the OLD row out of the
        # way -- same manual fix applied to MAC2 56-62 -- instead of either
        # skipping (drops the real new run forever) or --replace (destroys
        # real historical data for a run that never actually re-ran).
        incoming_started = _ts(run.get("started_at"))
        if (
            existing
            and incoming_started
            and row[1]
            and abs((incoming_started - row[1]).total_seconds()) > 300
        ):
            cur.execute(
                """
                UPDATE test_run
                SET run_id = run_id || '-orig' || to_char(started_at, 'YYYYMMDD'),
                    jenkins_build = NULL, build_url = NULL
                WHERE id = %s
                """,
                (row[0],),
            )
            print(
                f"[reclaim] run_id={run_id} belongs to a different execution than "
                f"the existing row (existing started_at={row[1]}, incoming="
                f"{incoming_started}) -- Jenkins build-number reuse. Renamed the "
                f"old row out of the way instead of skipping/replacing."
            )
            existing = None
            partial = False
            row = None

        if existing:
            if not replace:
                print(
                    f"[skip] run_id={run_id} already ingested (use --replace to overwrite)"
                )
                return False
            cur.execute("DELETE FROM test_run WHERE id=%s", (row[0],))  # cascades
            print(f"[replace] deleted existing run_id={run_id}")
        elif partial:
            cur.execute("DELETE FROM test_run WHERE id=%s", (row[0],))  # cascades
            print(
                f"[repair] run_id={run_id} had test_run but 0 test_result rows — re-ingesting"
            )
        elif row and row[2] == 0 and n_tests_in_model == 0:
            # legitimate empty build already recorded — skip without alarm
            print(f"[skip] run_id={run_id} already ingested (0 tests, empty build)")
            return False

        # product_version already resolved above (feature-build gate). Same across a run.

        # Installed arch: recorded per TEST record (the client is not installed yet
        # when the run record is written), but one install serves the whole session,
        # so first non-null wins — same shape as product_version above. Stays NULL
        # for every build recorded before the field existed.
        installed_bitness = installed_bitness_of(model)

        # Release: explicit --release wins; otherwise derive from the installed
        # version's major (140.0.0.x -> release-140) so a batch import tags each
        # build by the version it ACTUALLY ran, not one blanket label.
        label = release_label or _release_from_version(product_version)
        rel_id = _release_id(cur, label)

        # session pass/fail/skip rollup from test rows
        n_pass = sum(
            1 for t in model["tests"] if t["test"].get("test_status") == "PASS"
        )
        n_fail = sum(
            1 for t in model["tests"] if t["test"].get("test_status") == "FAIL"
        )
        n_skip = sum(
            1 for t in model["tests"] if t["test"].get("test_status") == "SKIP"
        )

        job = run.get("job_name")  # e.g. 'DEV/GRS-SYSTEMTEST-REG' (already foldered)
        bnum = run.get("build_number")
        build_url = None
        if job and bnum:
            # job already carries the folder (DEV/...); Jenkins path needs /job/ between levels.
            jpath = "/job/".join(job.split("/"))
            base = os.environ.get("JENKINS_BASE", "http://10.136.208.148:8080").rstrip(
                "/"
            )
            build_url = f"{base}/job/{jpath}/{bnum}/"

        cur.execute(
            """INSERT INTO test_run
               (run_id, jenkins_job, jenkins_build, build_url, node_name, vm_ip,
                release_id, product_version, previous_release, platform, tenant, dc,
                is_64_bit_cli, is_64_bit_installed,
                iterations_cli, started_at, n_pass, n_fail, n_skip, raw_ndjson_path)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (
                run_id,
                job,
                _int(bnum),
                build_url,
                run.get("node_name"),
                run.get("vm_ip"),
                rel_id,
                product_version,
                run.get("previous_release"),
                run.get("test_platform"),
                run.get("tenant"),
                run.get("dc"),
                _bool(run.get("is_64_bit")),
                installed_bitness,
                run.get("iterations_cli"),
                _ts(run.get("started_at")),
                n_pass,
                n_fail,
                n_skip,
                model["ndjson_path"],
            ),
        )
        run_pk = cur.fetchone()[0]

        for t in model["tests"]:
            trec = t["test"]
            it_list = t["iterations"]
            duration = round(sum(i.get("duration_s") or 0 for i in it_list), 3) or None
            started = _ts(
                min(
                    (i.get("started_at") for i in it_list if i.get("started_at")),
                    default=None,
                )
            )
            # A test that died BEFORE its iteration loop has no iteration rows, so
            # its reason rides on the `test` record instead (recorder change,
            # 2026-08-23) -- without the fallback such rows land reason-less.
            first_fail_reason = next(
                (i.get("fail_reason") for i in it_list if i.get("status") == "FAIL"),
                None,
            ) or trec.get("fail_reason")

            cur.execute(
                """INSERT INTO test_result
                   (run_id, test_name, suite, verdict, started_at, duration_s,
                    iterations_target, iterations_total, iterations_passed,
                    iterations_failed, first_failed_iter, fail_reason, failing_gate,
                    has_dump, gate_violated)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id""",
                (
                    run_pk,
                    trec["test_case"],
                    _suite_of(trec["test_case"]),
                    trec.get("test_status", "UNKNOWN"),
                    started,
                    duration,
                    trec.get("iterations_target"),
                    trec.get("iterations_total"),
                    trec.get("iterations_passed"),
                    trec.get("iterations_failed"),
                    trec.get("first_failed_iter"),
                    first_fail_reason,
                    t["failing_gate"],
                    t["has_dump"],
                    t["gate_violated"],
                ),
            )
            tr_pk = cur.fetchone()[0]

            for it in it_list:
                cur.execute(
                    """INSERT INTO iteration_result
                       (test_result_id, iter_index, iter_total, status, duration_s, fail_reason)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (
                        tr_pk,
                        it.get("iter_index"),
                        it.get("iter_total"),
                        it.get("status"),
                        it.get("duration_s"),
                        it.get("fail_reason"),
                    ),
                )
            for ps in t["process_summary"]:
                cur.execute(
                    """INSERT INTO process_summary
                       (test_result_id, process_name, cpu_max_pct, cpu_sustained_s,
                        cpu_threshold, mem_baseline_mb, mem_max_mb, mem_growth_mb,
                        handles_max, alive_pct, sample_count)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        tr_pk,
                        ps["process_name"],
                        ps["cpu_max_pct"],
                        ps["cpu_sustained_s"],
                        ps["cpu_threshold"],
                        ps["mem_baseline_mb"],
                        ps["mem_max_mb"],
                        ps["mem_growth_mb"],
                        ps["handles_max"],
                        ps["alive_pct"],
                        ps["sample_count"],
                    ),
                )
            for v in t["violations"]:
                cur.execute(
                    """INSERT INTO gate_violation
                       (test_result_id, violation_type, process_name, observed, threshold, detail)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (
                        tr_pk,
                        v["violation_type"],
                        v["process_name"],
                        v["observed"],
                        v["threshold"],
                        v["detail"],
                    ),
                )
            for d in t["dumps"]:
                cur.execute(
                    """INSERT INTO dump
                       (test_result_id, dump_type, process_name, signal_tag, file_name,
                        file_path, size_bytes, event_id, captured_at, note)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        tr_pk,
                        d.get("dump_type"),
                        d.get("process_name"),
                        d.get("signal_tag"),
                        d.get("file_name"),
                        d.get("file_path"),
                        d.get("size_bytes"),
                        d.get("event_id"),
                        d.get("captured_at"),
                        d.get("note"),
                    ),
                )
            # raw per-sample time-series — bulk insert (can be thousands of rows)
            raw = t.get("raw_samples") or []
            if raw:
                execute_values(
                    cur,
                    """INSERT INTO resource_sample
                       (test_result_id, process_name, elapsed_s, sampled_at, boot_id,
                        cpu_pct, cpu_pct_per_core, mem_mb, handles, alive,
                        net_mbps_in, net_mbps_out, load_phase, threads)
                       VALUES %s""",
                    [
                        (
                            tr_pk,
                            r["process_name"],
                            r["elapsed_s"],
                            r["sampled_at"],
                            r["boot_id"],
                            r["cpu_pct"],
                            r["cpu_pct_per_core"],
                            r["mem_mb"],
                            r["handles"],
                            r["alive"],
                            r.get("net_mbps_in"),
                            r.get("net_mbps_out"),
                            r.get("load_phase"),
                            r.get("threads"),
                        )
                        for r in raw
                    ],
                    page_size=1000,
                )
    # commit=False is the `--check-insert` path: every statement really ran against
    # the live schema (so a column/placeholder mismatch surfaces) but nothing is
    # kept. A silent mismatch here would break the 5-min poller for every lane.
    if commit:
        conn.commit()
    else:
        conn.rollback()
        print(
            "[check-insert] all statements executed against the live schema, then ROLLED BACK"
        )
    return True


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _bool(v):
    """Coerce a recorded flag to bool, or None when it says nothing.

    The ndjson carries whatever pytest handed the recorder: a real bool for the
    install-path probe, but `--is_64_bit` arrives from the Jenkins param string
    as the TEXT "true"/"false". An unrecognised value must become NULL, never a
    silent False — "we don't know the arch" and "it was 32-bit" are different
    facts and this column exists to tell them apart."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y", "64", "x64"):
            return True
        if s in ("false", "0", "no", "n", "32", "x86"):
            return False
    return None


def installed_bitness_of(model):
    """Run-level installed arch: first non-null per-test reading, else None.

    Public (no underscore) because `db.py inspect` reports the same number — one
    definition, imported, so the diagnostic and the insert can never disagree.

    Mac is a known constant, not a measurement: the Netskope Mac client is
    64-bit only (no 32-bit macOS build exists), so a mac run always installs
    64-bit regardless of what the per-test install-path probe captured (or
    didn't -- that probe only exists since 2026-09-13; older mac runs fell
    back to the CLI --is_64_bit param, which is meaningless for mac and
    produced a bogus "32-bit?" in reports). Skip the probe/CLI-fallback
    entirely for mac."""
    if (model["run"].get("test_platform") or "").lower() == "mac":
        return True
    return next(
        (
            _bool(t["test"].get("installed_is_64bit"))
            for t in model["tests"]
            if t["test"].get("installed_is_64bit") is not None
        ),
        None,
    )


def _bitness_str(model):
    """`installed=64 cli=32` style line for logs; `?` where unknown."""

    def _s(v):
        return "?" if v is None else ("64" if v else "32")

    return f"installed={_s(installed_bitness_of(model))} cli={_s(_bool(model['run'].get('is_64_bit')))}"


# ─────────────────────────── CLI ───────────────────────────


def main(argv):
    ap = argparse.ArgumentParser(
        description="Ingest a SystemTest build into the results DB."
    )
    ap.add_argument(
        "build_dir", help="dir containing iterations_*.ndjson + resource_samples/"
    )
    ap.add_argument(
        "--release", help="release label e.g. release-140 (links the run to a release)"
    )
    ap.add_argument(
        "--replace", action="store_true", help="overwrite if this run_id exists"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="parse + print summary, do NOT connect to the DB",
    )
    ap.add_argument(
        "--check-insert",
        action="store_true",
        help="run the real INSERTs against the live schema inside a transaction, then ROLLBACK. "
        "Use after a schema/column change: --dry-run cannot catch a column/placeholder mismatch.",
    )
    ap.add_argument(
        "--expect-build",
        type=int,
        help="Jenkins build number this dir is supposed to be. When set, a dir holding only "
        "ANOTHER build's ndjson (stale artifact in the backup zip) is reported as no-data "
        "instead of being ingested under the wrong build.",
    )
    a = ap.parse_args(argv)

    try:
        model = load_build(a.build_dir, expect_build=a.expect_build)
    except NoData as e:
        print(f"[no-data] {e}")
        return 3
    run = model["run"]
    print(
        f"[parsed] run_id={run['run_id']}  job={run.get('job_name')}  "
        f"build={run.get('build_number')}  tenant={run.get('tenant')}  "
        f"dc={run.get('dc')}  bitness={_bitness_str(model)}  "
        f"tests={len(model['tests'])}"
    )
    for t in model["tests"]:
        trec = t["test"]
        print(
            f"  - {trec['test_case']:45} {trec.get('test_status'):5} "
            f"iters={trec.get('iterations_total')}  "
            f"procs={len(t['process_summary'])}  "
            f"gate={'VIOLATED('+str(t['failing_gate'])+')' if t['gate_violated'] else 'ok'}  "
            f"dumps={len(t['dumps'])}"
        )

    if a.dry_run:
        print("[dry-run] no DB writes")
        return 0

    import dbconn

    conn = dbconn.connect()
    try:
        wrote = insert_model(
            conn,
            model,
            release_label=a.release,
            replace=a.replace,
            commit=not a.check_insert,
        )
        if a.check_insert:
            print(
                "[check-insert] OK"
                if wrote
                else "[check-insert] gated before insert (nothing to check)"
            )
        else:
            print("[ok] ingested" if wrote else "[noop] nothing written")
        lost = False
        if not wrote and not a.check_insert and any(t.get("gate_violated") for t in model["tests"]):
            # Nothing was written for a build whose gate was violated. If the run is not in the DB already, the evidence is
            # lost (2026-11-03: the demAgentSvc crash of REG2 build 311 vanished behind a quiet "[noop]"): say so, non-zero.
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM test_run WHERE run_id=%s", (run["run_id"],))
                lost = cur.fetchone() is None
                if lost:
                    # The execution may already sit under its old-server label (skipped on purpose, evidence stored there).
                    lost = _old_server_twin(cur, run["run_id"], _ts(run.get("started_at"))) is None
            if lost:
                print(
                    f"[LOST-GATE] run_id={run['run_id']} had gate violations "
                    f"({'; '.join(str(t.get('failing_gate')) for t in model['tests'] if t.get('gate_violated'))}) and nothing "
                    "was stored for it; read the skip reason printed above"
                )
    finally:
        conn.close()
    # rc 0 = wrote a new run; rc 5 = no-op (run_id already in DB, skipped); rc 6 = nothing stored for a build with a gate violation.
    # import-all uses this to avoid counting a skip as an ingest.
    if lost:
        return 6
    return 0 if wrote else 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
