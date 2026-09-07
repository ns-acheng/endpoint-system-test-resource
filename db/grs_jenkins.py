#!/usr/bin/env python
"""MIRROR NOTICE (added for endpoint-system-test-resource): this is a point-in-time copy for onboarding/public reference.
Canonical source (private repo): claude-resource/grs_tools/grs_jenkins.py -- do not edit this copy directly; changes land upstream first, then get re-synced here.
"""

"""Multi-purpose GRS Jenkins CLI — one allowlisted command for every Jenkins op.

Allowlist `python C:/Users/acheng/grs_tools/grs_jenkins.py` ONCE and all
subcommands below run without repeated approval prompts.

Subcommands:
  jobs [FOLDER]                 list jobs under a folder (default DEV)
  builds JOB [N]                last N builds: result/building/ts + key params (default 6)
  params JOB NUM                EVERY param of one build, exact names (branch param name
                                differs per lane — never guess it for `retrigger`)
  status JOB NUM                one build: result/building/duration
  poll JOB NUM [fresh]          ONE short line: pass/fail/undone/% for a running build
                                (`fresh` = drop cached state, rescan the whole log)
                                (incremental — cheap to call repeatedly)
  console JOB NUM[,NUM...] [PATTERN]  download consoleText once to /tmp, grep PATTERN (or tail).
  cmpfail JOB NUM,NUM,... [PAT...]    compare >=2 builds' failures: result + every FAILED
                                      test id + counts of fixed evidence patterns (+ extras).
                                      NUM may be comma-separated (e.g. 138,139) to do several in one call.
  srlog JOB NUM[,NUM...] [PAT]  DETACHED lanes (local1/local2): archived
                                results/selfrunner_<N>.sentinel.json + .log, grepped.
                                Use this instead of `console` for those lanes — their
                                consoleText never contains the pytest verdict.
  artifacts JOB NUM [DEST]      list (no DEST) or download every archived artifact
  config-grep PATTERN JOB...    match count of PATTERN in each JOB's config.xml, one call
  backup JOB NUM [PATTERN]      extract build's backup_nsc_*.zip (nested zip of
                                client logs/config) into C:/tmp, grep PATTERN
                                (or list files). Cached — delete the dir to redo.
  monitor JOB NUM [IVL] [PAT]   poll until finished, then print checkpoints
  running                       every executor busy right now (cross-job VM safety)
  trigger JOB k=v k=v ...       safety-check + crumb + POST buildWithParameters
  retrigger JOB SRC k=v...      copy SRC build's params, apply k=v overrides, trigger
                                (one short call — no need to re-type all params)
  replay JOB NUM [SCRIPT_FILE]  pipeline Replay: re-run NUM with same params (SCRIPT_FILE
                                overrides the Groovy; omit to replay the original)
  abort JOB NUM                 stop a build
  create NEW SRC                clone Jenkins job NEW from SRC's config.xml (same-as)
                                (NEW/SRC = alias or DEV/job/NAME; NEW must not exist)

JOB accepts an alias (see ALIASES) or a full path like DEV/job/GRS-AUSTIN.

Env overrides: JENKINS_BASE, JENKINS_USER, JENKINS_PASS.
"""

import base64
import http.cookiejar
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Console is cp1252; wrap stdout so repo content (CJK, arrows) never crashes the
# tool. Guarded because this module is now IMPORTED for its ALIASES table (single
# source, RCA 2026-08-03): an importer that already rewrapped stdout has detached
# the original buffer, so an unconditional rewrap here raised
# "ValueError: I/O operation on closed file" and broke the importer at load time.
if __name__ == "__main__" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = os.getenv("JENKINS_BASE", "http://10.136.208.148:8080")
USER = os.getenv("JENKINS_USER", "acheng")
PASS = os.getenv("JENKINS_PASS", "dlptest111???")
AUTH = base64.b64encode(f"{USER}:{PASS}".encode()).decode()

ALIASES = {
    "austin": "DEV/job/GRS-AUSTIN",
    "reg": "DEV/job/GRS-SYSTEMTEST-REG",
    "reg2": "DEV/job/GRS-SYSTEMTEST-REG-02",
    "localtest": "DEV/job/GRS-SYSTEMTEST-LOCAL1",  # renamed from GRS-SYSTEMTEST-LOCALTEST (2026-08-01)
    "local1": "DEV/job/GRS-SYSTEMTEST-LOCAL1",
    # LOCAL2 = detached self-runner variant of LOCALTEST (fault-ledger 31).
    # Must stay in sync with db.py's _JOB_ALIASES: harvest.py shells out to
    # THIS tool to download, so an alias known only to db.py fails at download.
    "local2": "DEV/job/GRS-SYSTEMTEST-LOCAL2",
    "win-dev": "DEV/job/GRS-WIN-DEV",
    "win-austin": "DEV/job/GRS-WIN-DEV-AUSTIN",
    "linux-dev": "DEV/job/GRS-LINUX-DEV",
    "mac-dev": "DEV/job/GRS-MAC-DEV",
    "mac1": "DEV/job/GRS-SYSTEMTEST-MAC1",
    "mac2": "DEV/job/GRS-SYSTEMTEST-MAC2",
}
PARAM_KEYS = [
    "branch",
    "test_cases",
    "test_suites",
    "tenant_info",
    "HOSTNAME",
    "release_info",
    "current_release",
    "previous_release",
    "iterations",
]
DEFAULT_PAT = (
    r"short test summary|PASSED|FAILED|ERROR|AssertionError|Timeout|"
    r"\[UPGRADE|\[STRESS|\[IPC|\[NET|\[STEER|step-[0-9]|pre-[0-9]"
)


# A bare "/tmp/..." string means TWO different directories depending on who
# opens it: native Windows Python's open() resolves a leading "/" against the
# CURRENT DRIVE (-> C:\tmp\...), while Git-Bash/MSYS's /tmp is its own mount
# (a different folder). This tool runs under native Windows Python and used to
# print "/tmp/build_...txt" as if it were that posix path -- every console/
# srlog/pollstate cache file silently landed under C:\tmp while a caller
# grepping bash's /tmp found nothing and nothing existed there (build 354 RCA,
# 2026-08-23). _tmp_path always resolves + prints the real, unambiguous path.
TMPDIR = os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "tmp")
os.makedirs(TMPDIR, exist_ok=True)


def _tmp_path(name):
    return os.path.join(TMPDIR, name)


def _job(job):
    p = ALIASES.get(job, job)
    return p if p.startswith("job/") else "job/" + p


def _get(path):
    if "?" in path:
        base, _, q = path.partition("?")
        path = base + "?" + urllib.parse.quote(q, safe="=&")
    req = urllib.request.Request(f"{BASE}/{path}")
    req.add_header("Authorization", f"Basic {AUTH}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _get_raw(path):
    """Raw bytes GET (e.g. a job's config.xml) — not JSON."""
    if "?" in path:
        base, _, q = path.partition("?")
        path = base + "?" + urllib.parse.quote(q, safe="=&")
    req = urllib.request.Request(f"{BASE}/{path}")
    req.add_header("Authorization", f"Basic {AUTH}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _head_size(path):
    """Content-Length of one artifact, or None. HEAD only — never downloads.

    Jenkins' api/json lists artifact NAMES but not sizes, which is why "how big
    is this build's bundle?" used to be an ad-hoc curl.
    """
    req = urllib.request.Request(f"{BASE}/{path}", method="HEAD")
    req.add_header("Authorization", f"Basic {AUTH}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            n = r.headers.get("Content-Length")
            return int(n) if n and n.isdigit() else None
    except (urllib.error.URLError, OSError):
        return None


def _params(actions):
    out = {}
    for a in actions or []:
        for p in a.get("parameters", []) or []:
            out[p["name"]] = p.get("value")
    return out


def _crumb(opener):
    req = urllib.request.Request(f"{BASE}/crumbIssuer/api/json")
    req.add_header("Authorization", f"Basic {AUTH}")
    return json.loads(opener.open(req, timeout=15).read())


def _fmt_ts(ms):
    return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000)) if ms else "?"


def _elapsed_min(ms):
    return int((time.time() - ms / 1000) / 60) if ms else "?"


def _now():
    return time.strftime("%m-%d %H:%M:%S")


def cmd_jobs(folder="DEV"):
    d = _get(f"job/{folder}/api/json" if folder else "api/json")
    for j in d.get("jobs", []):
        print(f"{j.get('color','?'):8} {j['name']}")


def cmd_config(job, what=None):
    """Dump a job's config.xml, or just its build-step commands.
      config localtest              -> full config.xml
      config localtest builders     -> only the <command> / <script> build-step bodies
    Use to inspect a freestyle job's batch step (the pipeline `<script>` alone,
    via grs_groovy_lint show, misses freestyle <builders> command bodies)."""
    xml = _get_raw(f"{_job(job)}/config.xml").decode("utf-8", "replace")
    if what == "builders":
        # Freestyle: <hudson.tasks.BatchFile><command>..</command> / Shell <command>;
        # pipeline: <script>. Print each body with its tag for context.
        for tag in ("command", "script"):
            for m in re.finditer(rf"<{tag}>(.*?)</{tag}>", xml, re.S):
                body = (
                    m.group(1)
                    .replace("&lt;", "<")
                    .replace("&gt;", ">")
                    .replace("&quot;", '"')
                    .replace("&#39;", "'")
                    .replace("&amp;", "&")
                )
                print(f"===== <{tag}> ({len(body)} chars) =====")
                print(body)
    else:
        print(xml)


def cmd_config_grep(pattern, *jobs):
    """Match count + distinct matching lines (first 5) of PATTERN in each JOB's
    live config.xml, one call for all of them (no bash for-loop needed — see
    top-rule: extend the tool, don't wrap it).
        config-grep ndjson local1 local2 reg reg2 mac1 mac2
    """
    if not jobs:
        print("usage: config-grep PATTERN job1 [job2 ...]")
        return 2
    for j in jobs:
        xml = _get_raw(f"{_job(j)}/config.xml").decode("utf-8", "replace")
        lines = [ln.strip() for ln in xml.splitlines() if re.search(pattern, ln)]
        uniq = list(dict.fromkeys(lines))
        print(f"\n{j} — {len(lines)} match(es), {len(uniq)} distinct")
        for ln in uniq[:5]:
            print(f"  {ln[:160]}")


def cmd_clone_configs(dest, *jobs):
    """Write each job's live config.xml into dest/ as <job>.live.xml, one file
    per job, in a SINGLE invocation (so it runs under the allowlisted prefix and
    never re-prompts). Defaults to the 4 SystemTest jobs when none are named.
      clone-configs C:/git/claude-resource/jenkins_groovy_TRUE
      clone-configs <dir> reg reg2 austin localtest win-dev
    dest is created if missing. Prints the byte/line count per job."""
    jobs = jobs or ("reg", "reg2", "austin", "localtest")
    os.makedirs(dest, exist_ok=True)
    for j in jobs:
        xml = _get_raw(f"{_job(j)}/config.xml").decode("utf-8", "replace")
        out = os.path.join(dest, f"{j}.live.xml")
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(xml)
        print(f"{j:<10} -> {out}  ({len(xml)} bytes, {xml.count(chr(10)) + 1} lines)")


def cmd_builds(job, n="6"):
    # builtOn = the AGENT the build actually ran on. Never infer the machine from
    # an artifact filename: every SystemTest VM reports the same Windows
    # COMPUTERNAME (W11-26H1-AUSTIN, none were renamed to their inventory names),
    # so a zip called backup_nsc_W11-26H1-AUSTIN_<n>.zip says nothing about WHICH
    # VM produced it. This field is the answer; that filename is not.
    d = _get(
        f"{_job(job)}/api/json?tree=builds[number,building,result,timestamp,builtOn,"
        f"actions[parameters[name,value]]]"
    )
    print(f"now: {_now()}")
    for b in d.get("builds", [])[: int(n)]:
        p = _params(b.get("actions"))
        elapsed = (
            f" elapsed={_elapsed_min(b.get('timestamp'))}min" if b["building"] else ""
        )
        print(
            f"#{b['number']:<4} {_fmt_ts(b.get('timestamp'))} "
            f"building={b['building']} result={b['result']} "
            f"node={b.get('builtOn') or '?'}{elapsed}"
        )
        line = "  " + " ".join(f"{k}={p[k]}" for k in PARAM_KEYS if p.get(k))
        if line.strip():
            print(line)


def cmd_params(job, num):
    # Every param of ONE build, exact names included. `builds` only prints the
    # PARAM_KEYS subset, which hides the branch parameter — and its name is NOT
    # the same across lanes (LOCAL1 vs LOCAL2 disagree). Overriding a guessed
    # name on `retrigger` silently keeps the SOURCE build's branch, i.e. runs
    # somebody else's code and calls it your verification.
    d = _get(f"{_job(job)}/{num}/api/json?tree=actions[parameters[name,value]]")
    p = _params(d.get("actions"))
    if not p:
        sys.exit(f"no parameters found on {job} #{num}")
    for k in sorted(p):
        print(f"{k}={p[k]}")


def cmd_findruns(pattern, jobs=None, n="12"):
    """Which builds ran something matching PATTERN, in which job, with what result?

    The question this answers — "do we still have a PASSING run of this suite to
    compare against?" — came up during a failure triage and the only way to ask
    it was a bash `for` loop piping `builds` into grep, i.e. a tool gap (the
    owner rejected the loop, correctly). PATTERN is matched case-insensitively
    against every build parameter value, so it takes a suite path
    (`category_bypass`), a tenant (`1338`), a branch, or a hostname.

    Results are grouped per job and marked so the eye lands on the green ones:
    a triage needs the last SUCCESS, not the newest build.
    """
    job_list = (
        [j.strip() for j in jobs.split(",") if j.strip()]
        if jobs
        else [
            "win-dev",
            "win-austin",
            "linux-dev",
            "mac-dev",
            "mac1",
            "austin",
            "reg",
            "reg2",
            "local1",
            "local2",
            "DEV/job/GRS-WIN-DEV-JITHAN",
            "PROD/job/GRS-WIN",
            "PROD/job/GRS-LINUX",
            "PROD/job/GRS-MAC",
        ]
    )
    pat = str(pattern).lower()
    print(
        f"now: {_now()}   pattern={pattern!r} over {len(job_list)} job(s), last {n} builds each"
    )
    total = greens = 0
    for j in job_list:
        try:
            d = _get(
                f"{_job(j)}/api/json?tree=builds[number,building,result,timestamp,"
                f"actions[parameters[name,value]]]"
            )
        except Exception as e:
            # A job that 404s / is unreachable must be NAMED, not skipped in
            # silence: "no hits" and "never looked" are different answers.
            print(f"{j:<28} [skip] {type(e).__name__}: {e}")
            continue
        hits = []
        for b in d.get("builds", [])[: int(n)]:
            p = _params(b.get("actions"))
            if any(pat in str(v).lower() for v in p.values()):
                hits.append((b, p))
        if not hits:
            continue
        print(f"\n{j}")
        for b, p in hits:
            total += 1
            res = b.get("result") or ("BUILDING" if b.get("building") else "?")
            if res == "SUCCESS":
                greens += 1
            mark = " <== GREEN" if res == "SUCCESS" else ""
            print(f"  #{b['number']:<5} {_fmt_ts(b.get('timestamp'))} {res:<9}{mark}")
            line = "        " + " ".join(f"{k}={p[k]}" for k in PARAM_KEYS if p.get(k))
            if line.strip():
                print(line)
    print(f"\n{total} matching build(s), {greens} SUCCESS")
    if total and not greens:
        print(
            "NOTE: no SUCCESS in the scanned window — widen with a bigger N before "
            "concluding this never passed (a 0-hit window is not an absence proof)."
        )


def cmd_status(job, num):
    # num may be comma-separated (e.g. "111,26,181") so several builds report in
    # ONE invocation (one approval) instead of one call each.
    print(f"now: {_now()}")
    for n in [x.strip() for x in str(num).split(",") if x.strip()]:
        d = _get(
            f"{_job(job)}/{n}/api/json?tree=number,building,result,duration,timestamp"
        )
        dur = (
            _elapsed_min(d.get("timestamp"))
            if d.get("building")
            else int(d.get("duration", 0) / 60000)
        )
        print(
            f"#{d.get('number')} building={d.get('building')} result={d.get('result')} "
            f"start={_fmt_ts(d.get('timestamp'))} {'elapsed' if d.get('building') else 'dur'}={dur}min"
        )


def cmd_desc(job, num):
    """Print a build's description (the SystemTest summary line) + flag whether it
    carries the canonical `iter N/M` token. `desc reg 95` — build 95 is the golden
    reference format: `<title> | 0v 1x | iter 1/1 | <version> | <duration>`.
    num may be comma-separated to compare several builds in one call."""
    print(f"now: {_now()}")
    for n in [x.strip() for x in str(num).split(",") if x.strip()]:
        d = _get(f"{_job(job)}/{n}/api/json?tree=description,result")
        desc = d.get("description") or "(no description)"
        has_iter = bool(re.search(r"iter\s+\d+/\d+", desc))
        flag = "OK iter" if has_iter else "!! NO iter token"
        print(f"#{n} [{d.get('result')}] [{flag}]  {desc}")


def cmd_status_multi(*specs):
    """Poll builds ACROSS several jobs in one call: `status-multi reg:111 reg2:26 austin:181`.
    Each spec is JOB:BUILD (build may itself be comma-separated). One approval covers
    all of them — use when several verification builds run in parallel on different jobs.
    """
    print(f"now: {_now()}")
    for spec in specs:
        job, _, num = spec.partition(":")
        if not num:
            print(f"[skip] bad spec {spec!r} (want JOB:BUILD)")
            continue
        for n in [x.strip() for x in num.split(",") if x.strip()]:
            try:
                d = _get(
                    f"{_job(job)}/{n}/api/json?tree=number,building,result,duration,timestamp"
                )
                dur = (
                    _elapsed_min(d.get("timestamp"))
                    if d.get("building")
                    else int(d.get("duration", 0) / 60000)
                )
                print(
                    f"{job:10} #{d.get('number')} building={d.get('building')} result={d.get('result')} "
                    f"{'elapsed' if d.get('building') else 'dur'}={dur}min"
                )
            except Exception as e:
                print(f"{job:10} #{n} [error] {e}")


def _console_text(job, n, force=False):
    """Fetch (or serve from cache) one build's consoleText as a line list.

    Shared by cmd_console and cmd_cmpfail so both honour the SAME staleness
    rule: only cache a FINISHED build (a still-building log grows, so a cached
    partial would go stale); a partial cached WHILE building is re-fetched once
    the build finishes. Prints the same "[saved ...]" line cmd_console always
    printed, so callers see identical cache-status feedback either way.
    """
    path = _tmp_path(f"build_{_job(job).split('/')[-1]}_{n}.txt")
    partial = path + ".partial"  # marker: the cached file was fetched mid-build
    building = _get(f"{_job(job)}/{n}/api/json?tree=building").get("building", False)
    stale_partial = os.path.exists(partial)
    if force or building or stale_partial or not os.path.exists(path):
        req = urllib.request.Request(f"{BASE}/{_job(job)}/{n}/consoleText")
        req.add_header("Authorization", f"Basic {AUTH}")
        with urllib.request.urlopen(req, timeout=60) as r:
            open(path, "wb").write(r.read())
        # Mark the cache partial iff still building; clear the marker once
        # this fetch captured a finished build.
        if building:
            open(partial, "w").close()
        elif os.path.exists(partial):
            os.remove(partial)
    state = "building" if building else "final"
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    print(f"[saved {path}] ({state}, {len(lines)} lines)")
    return lines


def cmd_console(job, num, pattern=None):
    # num may be a comma-separated list (e.g. "138,139") so several builds are
    # fetched + grepped in ONE invocation instead of one call (one approval) each.
    nums = [n.strip() for n in str(num).split(",") if n.strip()]
    for n in nums:
        if len(nums) > 1:
            print(f"\n===== {_job(job).split('/')[-1]} build {n} =====")
        force = pattern == "fresh"
        txt = _console_text(job, n, force=force)
        if force:
            pattern = None
        if pattern:
            hits = [ln for ln in txt if re.search(pattern, ln)]
            print("\n".join(hits[-40:]))
        else:
            print("\n".join(txt[-25:]))


# Evidence signatures worth counting on every cmpfail row by default — each one
# earned its place investigating a real multi-build comparison (SystemTest
# nsdiag-zombie-vs-uninstall race, 2026-09-01): a raw fail count alone can't
# tell "same root cause every time" from "coincidentally the same test name".
_CMPFAIL_DEFAULT_PATTERNS = (
    r"Timeout \(",  # ResilientSSHExecutor's own timeout-retry log line
    r"still active after uninstall",  # leftover stAgentSvc/nsdiag/stadrv process
    r"Residual paths",
    r"AssertionError:",
)


def cmd_cmpfail(job, num, *extra_patterns):
    """Compare N builds' failures side by side: result, every FAILED test node
    id (from pytest's short test summary), and counts of a fixed evidence-
    pattern set (+ any extra regex args) — so a recurring root cause across
    builds is visible without re-grepping each console by hand.

        cmpfail reg 365,366                       # default evidence patterns
        cmpfail "PROD/job/GRS-WIN" 16010,16015,16026,16030 "pid=\\d+"

    num is comma-separated (required, unlike single-build commands) since the
    whole point is >=2 builds in one call.
    """
    nums = [n.strip() for n in str(num).split(",") if n.strip()]
    patterns = list(_CMPFAIL_DEFAULT_PATTERNS) + list(extra_patterns)
    print(f"now: {_now()}")
    for n in nums:
        d = _get(f"{_job(job)}/{n}/api/json?tree=number,building,result")
        txt = _console_text(job, n)
        text = "\n".join(txt)
        failed = sorted(set(re.findall(r"^FAILED ([\w./:]+) ", text, re.MULTILINE)))
        print(f"\n#{n} result={d.get('result')} building={d.get('building')}")
        print(f"  FAILED: {failed if failed else '(none matched)'}")
        for pat in patterns:
            n_hits = len(re.findall(pat, text))
            print(f"  {pat!r}: {n_hits}")
        # Dedicated extraction, not just a count: WHICH process/pid was flagged
        # still-running matters as much as whether one was — a count alone
        # can't tell "always nsdiag" from "different process each time" (the
        # difference between one root cause and several).
        procs = sorted(set(re.findall(r"process:(\w+)=running\(pid=(\d+)\)", text)))
        if procs:
            print(f"  residual processes: {[f'{name}(pid={pid})' for name, pid in procs]}")


# The pipeline's per-test verdict line carries a decoration that DIFFERS BY LANE:
# REG/AUSTIN print "TEST #1 END - ✓ PASS", LOCAL1/LOCAL2 the ASCII fallback
# "TEST #1 END - [OK] PASS" (and ✗ / [XX] for FAIL). The original pattern matched
# exactly one character there, so every LOCAL* build reported "pass=0 fail=0
# undone=1" while its console plainly said "1 passed" — found on LOCAL1 build 244,
# where a green verification run looked like a build with no test data.
_PASS_RE = re.compile(r"TEST #\d+ END -.*?\bPASS\b")
_FAIL_RE = re.compile(r"TEST #\d+ END -.*?\bFAIL\b")
# pytest's own tail, e.g. "===== 1 passed, 83 deselected, 7 warnings in 2436.70s ====="
_SUMMARY_LINE_RE = re.compile(r"(?m)^=+ .*\bin \d+\.\d+s.*$")
_SUMMARY_ITEM_RE = re.compile(
    r"(\d+) (passed|failed|errors?|xpassed|xfailed|skipped)\b"
)


def _scan_counts(text):
    """(total, passed, failed, authoritative).

    authoritative=True means the numbers are absolute totals read from pytest's
    own summary line, so the caller must REPLACE its accumulator instead of adding
    (the summary appears once, at the end).
    """
    m = re.search(r"collected (\d+) items(?: / \d+ deselected / (\d+) selected)?", text)
    total = int(m.group(2) or m.group(1)) if m else None
    passed = len(_PASS_RE.findall(text))
    failed = len(_FAIL_RE.findall(text))
    if passed or failed:
        return total, passed, failed, False
    # No pipeline markers in this text. Rather than report a finished green build
    # as "no test data", read pytest's summary — it is authoritative and lane
    # independent, so it also survives the next decoration change.
    lines = _SUMMARY_LINE_RE.findall(text)
    if lines:
        counts = {k: int(n) for n, k in _SUMMARY_ITEM_RE.findall(lines[-1])}
        p = counts.get("passed", 0) + counts.get("xpassed", 0)
        f = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
        if p or f:
            return total, p, f, True
    return total, passed, failed, False


def cmd_poll(job, num, fresh=None):
    """Short pass/fail/undone/% status for ONE running (or just-finished) build.
    Incremental while BUILDING (progressiveText, cheap on multi-MB logs) — a marker
    split across two fetch boundaries can undercount mid-flight, so once the build
    is FINISHED this re-scans the whole log once for an authoritative final count.

    `poll JOB NUM fresh` drops the cached state and rescans from byte 0. Needed
    whenever the counting itself changed: a state file written by the old scanner
    keeps replaying its wrong numbers forever, because a finished build is never
    re-read."""
    d = _get(f"{_job(job)}/{num}/api/json?tree=building,result,timestamp,duration")
    building, result = d.get("building"), d.get("result")
    elapsed = (
        f"elapsed={_elapsed_min(d.get('timestamp'))}min"
        if building
        else f"dur={int(d.get('duration', 0) / 60000)}min"
    )

    state_path = _tmp_path(f"pollstate_{_job(job).split('/')[-1]}_{num}.json")
    state = {"offset": 0, "total": None, "passed": 0, "failed": 0, "final": False}
    if str(fresh).lower() in ("fresh", "--fresh", "reset"):
        try:
            os.remove(state_path)
        except OSError:
            pass
    elif os.path.exists(state_path):
        state.update(json.load(open(state_path)))

    start = 0 if (not building and not state["final"]) else state["offset"]
    req = urllib.request.Request(
        f"{BASE}/{_job(job)}/{num}/logText/progressiveText?start={start}"
    )
    req.add_header("Authorization", f"Basic {AUTH}")
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8", errors="replace")
        state["offset"] = int(r.headers.get("X-Text-Size", state["offset"]))

    total, passed, failed, authoritative = _scan_counts(text)
    if start == 0 or authoritative:
        # authoritative numbers are absolute totals — replace, never add.
        if total is not None:
            state["total"] = total
        state["passed"], state["failed"] = passed, failed
    else:
        if total is not None:
            state["total"] = total
        state["passed"] += passed
        state["failed"] += failed
    state["final"] = not building

    json.dump(state, open(state_path, "w"))

    total, p, f = state["total"], state["passed"], state["failed"]
    if total:
        undone, pct = total - p - f, round((p + f) / total * 100)
        summary = f"pass={p} fail={f} undone={undone} ({pct}%)"
    else:
        summary = "collecting..." if building else "no test data"
    stat = "BUILDING" if building else (result or "?")
    print(f"[{_now()}] #{num} {stat} {elapsed} — {summary}")


def cmd_monitor(job, num, ivl="90", pattern=None):
    ivl = int(ivl)
    for _ in range(400):
        d = _get(f"{_job(job)}/{num}/api/json?tree=building,result")
        if not d.get("building"):
            print(f"===== #{num} FINISHED result={d.get('result')} =====")
            cmd_console(job, num, pattern or DEFAULT_PAT)
            return
        time.sleep(ivl)
    print(f"#{num} still building after long poll — stopping")


def cmd_running():
    d = _get("computer/api/json?depth=2")
    busy = False
    for c in d.get("computer", []):
        for e in c.get("executors", []) + c.get("oneOffExecutors", []):
            ce = e.get("currentExecutable") or {}
            if not ce.get("url"):
                continue
            busy = True
            try:
                rel = ce["url"].split(f"{BASE}/")[-1]
                j = _get(
                    rel
                    + "api/json?tree=fullDisplayName,actions[parameters[name,value]]"
                )
                host = _params(j.get("actions")).get("HOSTNAME", "?")
                print(
                    f"BUSY {c.get('displayName')}: {j.get('fullDisplayName')} HOSTNAME={host}"
                )
            except Exception as ex:
                print(f"BUSY {c.get('displayName')}: {ce.get('url')} ({ex})")
    if not busy:
        print("all executors idle")


def cmd_node(name):
    """One node's online/offline status + executors (pre-trigger agent check)."""
    d = _get(
        f"computer/{name}/api/json?tree=displayName,offline,temporarilyOffline,offlineCauseReason,numExecutors,executors[currentExecutable[url]]"
    )
    busy = [
        e.get("currentExecutable", {}).get("url")
        for e in d.get("executors", [])
        if e.get("currentExecutable")
    ]
    print(
        f"{d.get('displayName')}: offline={d.get('offline')} temporarilyOffline={d.get('temporarilyOffline')}"
        f" reason={d.get('offlineCauseReason') or '-'} busy={len(busy)}"
    )


def _pf_setup(job, kv, vm_ip=None):
    """Shared preflight/recover setup: merged params (last build + k=v overrides),
    target VM ip (explicit `vm=` or resolved from the last build's console), tenant host, dc.

    `vm=<ip>` exists because the console-scrape below needs a HOSTNAME param, and the
    localTest lanes (LOCAL1/LOCAL2) do not have one — they run ON the VM, so their console
    never prints "Target VM found:". Result: `ip` stayed None there forever, every VM-side
    channel (firewall / processes / client / msi cache) was SKIPPED, and the gate emitted a
    permanent `vm-resolve REFUSE`. A gate that can never pass on a lane teaches that lane's
    users to reach for `_force_preflight=1`, which skips the real checks instead of running
    them — strictly worse than having no gate. Passing the ip explicitly makes the channels
    actually run. `vm` is stripped by the callers, so it never becomes a Jenkins param.
    """
    import grs_preflight as pf  # sibling module, channel logic lives there

    jp = _job(job)
    d = _get(f"{jp}/lastBuild/api/json?tree=number,actions[parameters[name,value]]")
    base = _params(d.get("actions", []))
    overrides = dict(item.partition("=")[::2] for item in kv)
    # Defensive: also strip it here, so a caller that forgets cannot POST `vm` as a param.
    overrides.pop("vm", None)
    merged = dict(base)
    merged.update(overrides)
    ip = vm_ip
    if ip:
        return pf, jp, base, merged, overrides, ip
    host = merged.get("HOSTNAME")
    # BUG FOUND 2026-09-01 (OVLP-01 build 368 misfire): the old code trusted
    # lastBuild's "Target VM found: <ip>" unconditionally, with NO check that
    # lastBuild actually targeted the SAME host `host` we're resolving for.
    # A trigger call overriding HOSTNAME to a DIFFERENT VM than whatever
    # someone else's job most recently ran (any agent can trigger this shared
    # job in between) silently inherited THAT VM's ip — e.g. a REG6 build sat
    # as lastBuild while this call was targeting SYS-07, so the "vm-processes"
    # channel checked REG6's leftover sampler and REFUSED a trigger that was
    # never going near REG6. Guard: only trust lastBuild's resolved ip when
    # lastBuild's OWN HOSTNAME param matches `host` — otherwise fall through
    # to ip=None, which is the ALREADY-SAFE degraded path (VM-side channels
    # skip rather than check the wrong machine; see the LOCAL1/LOCAL2 case
    # this function's docstring already documents).
    if host and d.get("number") and base.get("HOSTNAME") == host:
        try:
            req = urllib.request.Request(f"{BASE}/{jp}/{d['number']}/consoleText")
            req.add_header("Authorization", f"Basic {AUTH}")
            with urllib.request.urlopen(req, timeout=30) as r:
                txt = r.read().decode("utf-8", "replace")
            m = re.search(r"Target VM found:\s*([0-9.]+)", txt) or re.search(
                rf"nsadmin@([0-9.]+).*?{re.escape(host)}", txt
            )
            if m:
                ip = m.group(1)
        except Exception:
            ip = None
    return pf, jp, base, merged, overrides, ip


# Every agent's GRS clone is a separate checkout that can be behind main
# (e.g. C:/git22 missing a tenant recently added to staging.json on
# C:/git's main) -- try each known root and keep the first one that actually
# resolves the tenant, instead of hardcoding a single agent's path. Bug found
# 2026-08-25: tenant 1457 (added to C:/git's staging.json) wasn't in C:/git22's
# stale copy, so every LOCAL1 preflight for that tenant REFUSED with
# "tenant/dc not resolvable" even though the tenant genuinely exists.
_GRS_CLONE_ROOTS = ["C:/git", "C:/git22", "C:/git33", "C:/git44"]


def _grs_env_json(rel_path):
    """Load a GRS test_environment json, trying every known clone root until
    one has the file (never assume a single clone is current)."""
    for root in _GRS_CLONE_ROOTS:
        try:
            return json.load(
                open(
                    f"{root}/nsclient_golden_regression_suite/{rel_path}",
                    encoding="utf-8",
                )
            )
        except OSError:
            continue
    return None


def _pf_tenant_webui(merged):
    """LEGACY — only recover's client-config-failclose fix path calls this
    (needs a real webapi session). preflight itself never does (owner rule:
    no tenant API in preflight). Tenant hostname from --tenant id via the
    env json's reverse map."""
    import grs_preflight as pf

    ti = merged.get("tenant_info", "")
    tid = re.search(r"--tenant\s+(\S+)", ti)
    if not tid:
        return None, None
    envf = re.search(r"--custom-env-file=(\S+)", ti)
    gc = pf._grs_client()
    env = _grs_env_json(envf.group(1)) if envf else None
    if not env:
        return None, None
    name = next(
        (k for k, v in env.get("tenants", {}).items() if v == tid.group(1)), None
    )
    if not name:
        return None, None
    suffix = env.get("alias_env") or env.get("env")
    # _tenant_webui prepends the test_environment dir itself — pass basename only
    return (
        gc._tenant_webui(f"{name}.{suffix}", os.path.basename(envf.group(1))),
        f"{name}.{suffix}",
    )


def _pf_tenant_host(merged):
    """Tenant hostname from --tenant id via the env json's reverse map.
    LOCAL FILE ONLY — no webapi (owner 2026-08-17 hard rule: preflight never
    touches the tenant API; login waves and latency are not our problem here)."""
    ti = merged.get("tenant_info", "")
    tid = re.search(r"--tenant\s+(\S+)", ti)
    envf = re.search(r"--custom-env-file=(\S+)", ti)
    if not tid or not envf:
        return None
    env = _grs_env_json(envf.group(1))
    if not env:
        return None
    name = next(
        (k for k, v in env.get("tenants", {}).items() if v == tid.group(1)), None
    )
    if not name:
        return None
    suffix = env.get("alias_env") or env.get("env")
    return f"{name}.{suffix}"


def _pf_run_all(job, kv, skip_params=False, vm_ip=None):
    """Run preflight channels in cheap+decisive order; a REFUSE short-circuits.
    Design: claude-resource/plan_preflight_fast_lane.md (owner 2026-08-17).
    preflight's only job: find a safe lane FAST. Never fixes, never touches
    the tenant API, never probes a VM while the lane is busy, never retries
    a dead probe."""
    pf, jp, base, merged, overrides, ip = _pf_setup(job, kv, vm_ip)
    rows = []
    # 1. params (free, local)
    if not skip_params:
        rows.append(pf.ch_params_complete(merged, overrides))
    # 2. job-busy — a busy lane short-circuits BEFORE any VM contact
    d = _get(f"{jp}/lastBuild/api/json?tree=number,building")
    if d.get("building"):
        rows.append(("job-busy", "REFUSE", f"{jp} #{d.get('number')} still building"))
        return rows, ip, merged, overrides
    rows.append(("job-busy", "PASS", "no build running on this job"))
    # 3. env json (local file) — tenant/dc resolves or we say so; no webapi
    host_tenant = _pf_tenant_host(merged)
    if host_tenant:
        rows.append(("env-json", "PASS", f"tenant resolves to {host_tenant}"))
    else:
        rows.append(
            ("env-json", "REFUSE", "tenant/dc not resolvable from the env json")
        )
    # 4. VM probe — only now, one shot, zero reconnect, known-bad short-circuit
    if not ip:
        rows.append(
            (
                "vm-resolve",
                "REFUSE",
                "cannot resolve target VM ip from last build console",
            )
        )
        return rows, ip, merged, overrides
    bad = pf.bad_find(ip)
    if bad:
        rows.append(
            (
                "vm-known-bad",
                "REFUSE",
                f"{ip} on the known-bad list since {bad.get('ts')}: {bad.get('failed')} "
                "— run `recover` to fix+clear, or pick another candidate",
            )
        )
        return rows, ip, merged, overrides
    import time as _t

    _t0 = _t.time()
    vm_rows = pf.ch_vm_all(ip, want_tenant_host=host_tenant)
    print(f"[preflight] vm probe (one round trip) took {_t.time()-_t0:.1f}s")
    rows.extend(vm_rows)
    if any(s == "REFUSE" for _n, s, _d in vm_rows):
        pf.bad_mark(ip, vm_rows, job=jp)
    return rows, ip, merged, overrides


def cmd_preflight(job, *kv):
    """Read-only all-channel residue check before a trigger (plan_trigger_preflight.md).
    REFUSE on any row = do not trigger until it's cleaned (see `recover`).

    Accepts `vm=<ip>` for lanes with no HOSTNAME param (LOCAL1/LOCAL2), same as `trigger`.
    """
    kv_d = dict(item.partition("=")[::2] for item in kv)
    vm_ip = kv_d.pop("vm", None)
    if vm_ip:
        kv = tuple(f"{k}={v}" for k, v in kv_d.items())
    rows, ip, merged, _ov = _pf_run_all(job, kv, vm_ip=vm_ip)
    print(f"preflight {_job(job)} vm={ip}:")
    worst = 0
    for name, sev, detail in rows:
        print(f"  [{sev:6}] {name}: {detail}")
        if sev == "REFUSE":
            worst = 1
    print("PREFLIGHT", "REFUSE" if worst else "OK")
    return worst


def cmd_recover(job, *kv):
    """Fix what preflight finds (VM firewall/processes/wrong-tenant client+cache,
    failClose flag), every fix read-back, then re-run preflight as proof.
    Never touches the DSE type or gmail — those are owner's calls."""
    import grs_preflight as pf

    # `vm=<ip>` for lanes with no HOSTNAME param (LOCAL1/LOCAL2) — see _pf_setup. Without it
    # recover could never fix anything on those lanes either: every fix below is gated on `ip`.
    kv_d = dict(item.partition("=")[::2] for item in kv)
    vm_ip = kv_d.pop("vm", None)
    if vm_ip:
        kv = tuple(f"{k}={v}" for k, v in kv_d.items())
    rows, ip, merged, _ov = _pf_run_all(job, kv, vm_ip=vm_ip)
    # A known-bad entry is a RECORD of past failures, not a live diagnosis —
    # left unhandled it made recover itself useless (LOCAL2 2026-08-18: a stale-MSI
    # flag refused the trigger AND recover). Expand it into fresh channel rows so
    # the fix branches below act on CURRENT state, and clear the entry once fixed.
    known_bad = ip and any(r[0] == "vm-known-bad" for r in rows)
    if known_bad:
        rows = [r for r in rows if r[0] != "vm-known-bad"]
        for chname in pf.VM_CHANNEL_FUNCS:
            rows.append(getattr(pf, chname)(ip))
    for name, sev, detail in rows:
        if sev != "REFUSE":
            continue
        if name == "vm-firewall" and ip:
            pf.vm_ps(
                ip,
                'Get-NetFirewallRule -DisplayName "BLOCK_*" | Remove-NetFirewallRule -Confirm:$false',
            )
            print(
                f"[recover] vm-firewall: deleted; read-back -> {pf.ch_vm_firewall(ip)[1]}"
            )
        elif name == "vm-processes" and ip:
            evidence = pf.vm_ps(
                ip,
                "Get-CimInstance Win32_Process | Where-Object {$_.Name -match 'python|pytest|curl'} "
                "| ForEach-Object { $_.ProcessId.ToString() + ' ' + $_.Name + ' ' + $_.CommandLine }",
            )
            print(f"[recover] vm-processes evidence BEFORE kill:\n{evidence}")
            pf.vm_ps(
                ip,
                "Get-Process python*,pytest*,curl* -ErrorAction SilentlyContinue | Stop-Process -Force -Confirm:$false",
            )
            print(
                f"[recover] vm-processes: killed; read-back -> {pf.ch_vm_processes(ip)[1]}"
            )
        elif name == "vm-client" and ip:
            pf.vm_ps(
                ip,
                '$u = Get-ItemProperty "HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*",'
                '"HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*" -ErrorAction SilentlyContinue '
                '| Where-Object {$_.DisplayName -like "*Netskope*"} | Select-Object -ExpandProperty PSChildName; '
                'if ($u) { Start-Process msiexec.exe -ArgumentList "/x","$u","/qn","UNINSTALLPASSWORD=netSk0pe","/norestart" -Wait | Out-Null }; '
                "Remove-Item -Recurse -Force C:\\nsclient_download -ErrorAction SilentlyContinue",
            )
            print(
                f"[recover] vm-client: uninstalled + cache wiped; read-back -> {pf.ch_vm_client(ip)[1]} / {pf.ch_vm_msi_cache(ip)[1]}"
            )
        elif name == "vm-msi-cache" and ip:
            pf.vm_ps(
                ip,
                "Remove-Item -Recurse -Force C:\\nsclient_download -ErrorAction SilentlyContinue",
            )
            print(
                f"[recover] vm-msi-cache: wiped; read-back -> {pf.ch_vm_msi_cache(ip)[1]}"
            )
        elif name == "client-config-failclose":
            webui, _ = _pf_tenant_webui(merged)
            dc = re.search(r"--dc=(\S+)", merged.get("tenant_info", "") or "")
            if webui and dc:
                from webapi.settings.security_cloud_platform.netskope_client.client_configuration import (
                    ClientConfiguration,
                )

                ClientConfiguration(webui).update_client_config(
                    search_config=dc.group(1), failClose="0"
                )
                print(
                    f"[recover] failClose -> 0; read-back -> {pf.ch_client_config_failclose(webui, dc.group(1))[1]}"
                )
        else:
            print(
                f"[recover] {name}: not auto-fixable — needs owner/manual action: {detail}"
            )
    if known_bad:
        print(
            f"[recover] known-bad entry for {ip}: {'cleared' if pf.bad_clear(ip) else 'not found'}"
        )
    print("--- re-run preflight after recover ---")
    # Re-append vm=: it was stripped out of kv above, and without it this proof-of-fix
    # re-run resolves no ip and reports `vm-resolve REFUSE` — i.e. it would hide the very
    # read-back it exists to show.
    return cmd_preflight(job, *(kv + ((f"vm={vm_ip}",) if vm_ip else ())))


_TENANT_MAP = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tenant_map.json"
)


def _dc_pick(case, lane):
    """Resolve case -> test filter + tenant/dc from the machine-readable tenant
    map (tenant_map.json). NEVER from memory, NEVER from a previous build unless
    it passed. Returns (params_dict, error_str)."""
    try:
        m = json.load(open(_TENANT_MAP, encoding="utf-8"))
    except Exception as e:
        return None, f"tenant_map.json unreadable: {e}"
    entry = m.get("cases", {}).get(case.upper())
    if not entry:
        return None, f"case {case!r} not in tenant map — STOP, ask owner (never guess)"
    combos = [c for c in entry["combos"] if lane in c.get("lanes", [])]
    if not combos:
        return (
            None,
            f"case {case} has no combo for lane {lane!r} (map says: {[c.get('lanes') for c in entry['combos']]})",
        )
    # family conflict: any running SystemTest build on the same tenant+family?
    fam = combos[0]["family"]
    tid = combos[0]["tenant"]
    try:
        for j in ("reg", "reg2", "local1", "local2"):
            d = _get(
                f"{_job(j)}/lastBuild/api/json?tree=number,building,actions[parameters[name,value]]"
            )
            if not d.get("building"):
                continue
            ti = " ".join(
                str(p.get("value", ""))
                for p in (d.get("actions") or [{}])[0].get("parameters", []) or []
                if isinstance(p, dict)
            )
            m_t = re.search(r"--tenant\s+(\S+)", ti)
            m_d = re.search(r"--dc=(\S+)", ti)
            if m_t and m_t.group(1) == tid and m_d:
                dc_fam = next(
                    (f for f, dcs in m["families"].items() if m_d.group(1) in dcs), None
                )
                if dc_fam == fam:
                    return None, (
                        f"family conflict: {j} build is running tenant {tid} dc {m_d.group(1)} "
                        f"(same {fam} family) — wait or pick another family"
                    )
    except Exception:
        pass  # conflict check best-effort; the map choice stands
    c = combos[0]
    bitness = c.get("bitness", "64")  # default 64-bit; 32 only when the map says so
    params = {
        "test_cases": f"-k {entry['test_filter']}",
        "tenant_info": (
            f"--custom-env-file=golden_regression/test_environment/boomskope_nonprod_stg.json "
            f"--tenant {c['tenant']} --dc={c['dc']} --is_64_bit={'true' if bitness == '64' else 'false'}"
        ),
        "timeout": str(entry.get("default_timeout_h", 2)),
    }
    if entry.get("fixed_iterations"):
        params["iterations"] = str(entry["fixed_iterations"])
    return params, None


def cmd_dcpick(*args):
    """dc-pick <CASE> <lane> — print the resolved params (never hand-assembled)."""
    if len(args) < 2:
        print("usage: dc-pick <CASE> <lane>")
        return 2
    params, err = _dc_pick(args[0], args[1])
    if err:
        print(f"REFUSE: {err}")
        return 1
    for k, v in params.items():
        print(f"{k}={v}")
    return 0


def cmd_trigger(job, *kv):
    jp = _job(job)
    # case=X auto-resolves test_cases + tenant_info + timeout from the tenant
    # map (hard rule owner 2026-08-17: never hand-assemble). Explicit
    # tenant_info/test_cases in kv still win (power-user override).
    kv_d = dict(item.partition("=")[::2] for item in kv)
    case = kv_d.pop("case", None)
    if case and "tenant_info" not in kv_d:
        picked, err = _dc_pick(case, job)
        if err:
            print(f"REFUSE: {err}")
            return 1
        for k, v in picked.items():
            kv_d.setdefault(k, v)
        kv = tuple(f"{k}={v}" for k, v in kv_d.items())
        print(
            f"[dc-pick] {case}: {picked.get('test_cases')} | {picked.get('tenant_info')}"
        )
    # bitness alignment (REG 329 RCA): --build_url pinning a 64-bit MSI while
    # tenant_info lacks --is_64_bit=true -> install gate fails on path mismatch.
    # When case= resolved the bitness, the build_url's MSI name must agree.
    _ti = kv_d.get("tenant_info", "")
    _ri = kv_d.get("release_info", "")
    _m64 = re.search(r"--is_64_bit=(\S+)", _ti)
    _msi = re.search(r"build_url=\S*/(STAgent(?:64)?\.msi)", _ri)
    if _m64 and _msi:
        want64 = _m64.group(1).lower() == "true"
        got64 = _msi.group(1) == "STAgent64.msi"
        if want64 != got64:
            print(
                f"REFUSE: bitness mismatch — is_64_bit={_m64.group(1)} but build_url "
                f"pins {_msi.group(1)} ({'64' if got64 else '32'}-bit). Fix one of them."
            )
            return 1
    # Preflight BEFORE anything else (owner 2026-08-16): the check lives INSIDE
    # the action — no harness, no memory, no discipline required. Any REFUSE
    # row blocks the POST. Escape hatch: _force_preflight=1 (stripped, never
    # becomes a Jenkins param). plan_trigger_preflight.md.
    force = kv_d.pop("_force_preflight", None)
    # `vm=<ip>` names the target VM for the preflight channels on lanes whose params carry
    # no HOSTNAME (LOCAL1/LOCAL2 — see _pf_setup). Stripped here so it never reaches the POST.
    vm_ip = kv_d.pop("vm", None)
    if force or vm_ip:
        kv = tuple(f"{k}={v}" for k, v in kv_d.items())
    try:
        rows, ip, _merged, _ov = _pf_run_all(job, kv, skip_params=True, vm_ip=vm_ip)
    except Exception as e:
        rows, ip = [
            (
                "preflight-itself",
                "REFUSE",
                f"preflight failed to complete: {type(e).__name__}: {e}",
            )
        ], None
    refuses = [r for r in rows if r[1] == "REFUSE"]
    if refuses and not force:
        print(f"trigger REFUSED by preflight (vm={ip}):")
        for name, sev, detail in rows:
            print(f"  [{sev:6}] {name}: {detail}")
        print(
            "fix with: grs_jenkins.py recover <job> [k=v...]  — or _force_preflight=1 to override"
        )
        return 1
    if force and refuses:
        print(
            f"⚠️  preflight REFUSE overridden by _force_preflight ({len(refuses)} row(s)):"
        )
        for name, _sev, detail in refuses:
            print(f"  [FORCED] {name}: {detail}")
    # Already-merged branch guard (git22 IDP/UPN RCA 2026-07-30): triggering a
    # build on a branch whose content is fully merged into origin/main wastes a
    # VM and tests the wrong thing. Check only acheng/* branches; refuse early.
    # The branch param is NOT named the same across lanes: REG/REG-02 use `branch`, LOCAL1/
    # LOCAL2 use `GIT_BRANCH`. Reading only `branch` made this guard silently INERT on the
    # localTest lanes — the exact "same thing, two names" defect that already cost a full
    # build round (memory: LOCAL1/LOCAL2 param-name mismatch). Check both.
    br = kv_d.get("branch") or kv_d.get("GIT_BRANCH") or ""
    if br.startswith("acheng/"):
        import subprocess as _sp

        repo = r"C:\git22\nsclient_golden_regression_suite"
        _sp.run(
            ["git", "fetch", "origin", "-q"], cwd=repo, capture_output=True, timeout=60
        )
        r1 = _sp.run(
            ["git", "ls-remote", "--exit-code", "origin", br],
            cwd=repo,
            capture_output=True,
            timeout=30,
        )
        if r1.returncode == 0:
            r2 = _sp.run(
                ["git", "merge-base", "--is-ancestor", f"origin/{br}", "origin/main"],
                cwd=repo,
                capture_output=True,
                timeout=30,
            )
            if r2.returncode == 0:
                print(
                    f"REFUSED: branch '{br}' is fully merged into origin/main — it is DONE."
                )
                print(
                    "Delete it; test main or a new branch. (guard: merged-branch RCA)"
                )
                return 1
    d = _get(f"{jp}/api/json?tree=builds[building],inQueue")
    if any(b["building"] for b in d.get("builds", [])) or d.get("inQueue"):
        print("BLOCKED: a build is running or queued for this job")
        return
    payload = []
    for item in kv:
        k, _, v = item.partition("=")
        payload.append((k, v))
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op)
    req = urllib.request.Request(
        f"{BASE}/{jp}/buildWithParameters",
        data=urllib.parse.urlencode(payload).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Basic {AUTH}")
    req.add_header(crumb["crumbRequestField"], crumb["crumb"])
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    r = op.open(req, timeout=20)
    loc = r.getheader("Location")
    print(f"triggered {jp} status={r.status} queue={loc}")
    # Resolve the queue item -> build number so the caller never needs a second
    # (approval-gated) step to find out which build is theirs. Poll briefly: the
    # executable appears once the queue item leaves the wait state.
    qid = None
    if loc:
        m = re.search(r"/queue/item/(\d+)/", loc)
        qid = m.group(1) if m else None
    if not qid:
        return
    for _ in range(20):
        try:
            d = _get(f"queue/item/{qid}/api/json?tree=executable[number],why")
        except Exception:
            break
        ex = d.get("executable")
        if ex and ex.get("number"):
            print(
                f"build={ex['number']}  (poll: status {_job(job).split('/')[-1]} {ex['number']})"
            )
            return
        time.sleep(3)
    print(f"build=?  (queue {qid} not yet assigned; re-check with: builds {job} 1)")


def cmd_retrigger(job, src, *kv):
    """Copy params from SRC build, apply overrides, then trigger. Reduces the
    per-op token count to `retrigger JOB SRC branch=... [more]=...` so one
    approval covers both the param-copy and the POST — the flat `trigger`
    form needs 8+ k=v pairs when re-running an UPGRADE/STRESS suite."""
    jp = _job(job)
    d = _get(f"{jp}/{src}/api/json?tree=actions[parameters[name,value]]")
    base = _params(d.get("actions", []))
    overrides = {}
    for item in kv:
        k, _, v = item.partition("=")
        overrides[k] = v
    merged = dict(base)
    merged.update(overrides)
    # retrigger-inheritance guard (builds 307/110/111 all ran the WRONG branch):
    # the load-bearing params must be EXPLICIT in this retrigger, never inherited.
    import grs_preflight as _pf

    _n, sev, detail = _pf.ch_params_complete(merged, overrides)
    if sev == "REFUSE" and "_force_preflight=1" not in kv:
        print(f"retrigger REFUSED: {detail}")
        print(
            "pass them explicitly, e.g. retrigger reg 123 branch=main iterations=2 tenant_info=... release_info=..."
        )
        return
    # Show what will be posted so the caller sees the effective set
    print(f"retrigger {jp} (based on #{src} + {len(overrides)} override(s)):")
    for k in sorted(merged):
        star = " *" if k in overrides else ""
        print(f"  {k}={merged[k]}{star}")
    cmd_trigger(job, *(f"{k}={v}" for k, v in merged.items()))


def cmd_replay(job, num, script_file=None):
    """Pipeline Replay: re-run <num> with the same params. If script_file is
    given, its contents override the Groovy (used to test pipeline changes
    without editing the live job config). Otherwise the original build's
    script is re-used verbatim."""
    jp = _job(job)
    # Fetch original script when caller didn't supply one — Jenkins Replay
    # form requires mainScript field even when replaying unchanged.
    if script_file:
        with open(script_file, "r", encoding="utf-8") as f:
            script = f.read()
    else:
        req = urllib.request.Request(f"{BASE}/{jp}/{num}/replay/")
        req.add_header("Authorization", f"Basic {AUTH}")
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        m = re.search(
            r'<textarea[^>]*name="_\.mainScript"[^>]*>(.*?)</textarea>', html, re.S
        )
        if not m:
            print("could not extract mainScript from replay page")
            return
        script = m.group(1)
        # HTML-unescape common entities
        script = (
            script.replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&amp;", "&")
        )
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op)
    data = urllib.parse.urlencode(
        {
            "mainScript": script,
            ".crumb": crumb["crumb"],
            "json": json.dumps({"mainScript": script}),
            "Submit": "Run",
        }
    ).encode()
    req = urllib.request.Request(
        f"{BASE}/{jp}/{num}/replay/run", data=data, method="POST"
    )
    req.add_header("Authorization", f"Basic {AUTH}")
    req.add_header(crumb["crumbRequestField"], crumb["crumb"])
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    r = op.open(req, timeout=20)
    src = "custom script" if script_file else "original script"
    print(f"replayed {jp}/{num} with {src}: status={r.status} url={r.url}")


ABORT_AUDIT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "abort_audit.log"
)


def _abort_gate(argv_tail):
    """Owner-approval gate for abort/term/kill (Rule 25, .pi/error_log.md;
    fault-ledger 37 — an unapproved abort killed a good build on a misread).
    argv_tail = the raw args AFTER `abort JOB NUM [mode]`; must contain
    --owner-token "<the owner's exact approval words from chat>". The token is
    appended to abort_audit.log (token + build + ts) so the owner can audit
    whether any abort was self-approved. No token = REFUSED, no exceptions."""
    token = None
    for i, a in enumerate(argv_tail):
        if a == "--owner-token" and i + 1 < len(argv_tail):
            token = argv_tail[i + 1]
    if not token or not token.strip():
        print(
            'REFUSED: abort requires --owner-token "<owner\'s approval words>".\n'
            "Rule 25: never abort anything without explicit owner approval.\n"
            "Ask the owner; pass their approval words verbatim; it lands in abort_audit.log."
        )
        sys.exit(1)
    return token.strip()


def cmd_delete(job, *rest):
    """Delete a JOB (not a build) via POST doDelete. GATED like abort: requires
    --owner-token \"<owner's approval words>\"; lands in abort_audit.log.
    Deleting a job destroys its build history — use only when the owner has
    confirmed the job is orphaned/unwanted."""
    token = _abort_gate(list(rest))
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op)
    req = urllib.request.Request(
        f"{BASE}/{_job(job)}/doDelete", data=b"", method="POST"
    )
    req.add_header("Authorization", f"Basic {AUTH}")
    req.add_header(crumb["crumbRequestField"], crumb["crumb"])
    try:
        status = op.open(req, timeout=15).status
        print("doDelete status:", status)
    except urllib.error.HTTPError as e:
        status = f"HTTP {e.code}"
        print(f"doDelete HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}")
    with open(ABORT_AUDIT, "a", encoding="utf-8") as f:
        f.write(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {_job(job)} | mode=doDelete | "
            f"status={status} | owner-token={token!r}\n"
        )


def cmd_abort(job, num, mode="stop", *rest):
    """Abort a build. mode=stop (default, graceful), term (harder — interrupts the
    flow), or kill (hardest — kills the CpsFlowExecution outright). Use term/kill
    when a pipeline hangs in a post step and plain `stop` is ignored (soft abort
    can't interrupt a native step that swallows the interrupt).
    GATED: requires --owner-token \"<owner's approval words>\" (see _abort_gate)."""
    # main() dispatches positionally: mode may swallow '--owner-token' when the
    # caller puts flags right after NUM — scan mode+rest together for the flag.
    tail = ([mode] if mode else []) + list(rest)
    token = _abort_gate(tail)
    if mode not in ("stop", "term", "kill"):
        mode = "stop"
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op)
    endpoints = {"stop": "stop", "term": "term", "kill": "kill"}
    ep = endpoints.get(mode, "stop")
    req = urllib.request.Request(
        f"{BASE}/{_job(job)}/{num}/{ep}", data=b"", method="POST"
    )
    req.add_header("Authorization", f"Basic {AUTH}")
    req.add_header(crumb["crumbRequestField"], crumb["crumb"])
    status = None
    try:
        status = op.open(req, timeout=15).status
        print(f"{ep} status:", status)
    except urllib.error.HTTPError as e:
        status = f"HTTP {e.code}"
        print(f"{ep} HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}")
    with open(ABORT_AUDIT, "a", encoding="utf-8") as f:
        f.write(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {_job(job)}/{num} | mode={ep} | "
            f"status={status} | owner-token={token!r}\n"
        )
    print(f"audit -> {ABORT_AUDIT}")


def cmd_deploy_groovy(job, *args):
    """Deploy a marker-anchored insert into a Jenkins pipeline job's config.xml.

    Usage: deploy-groovy JOB ANCHOR INSERT_FILE [--marker MARKER]

    Safety checks (fault-ledger 27):
      1. Fetch config.xml as latin-1 (preserve bytes exactly)
      2. If MARKER already in config -> SKIP (block already deployed, no duplicate)
      3. Insert INSERT_FILE content before ANCHOR
      4. POST back as latin-1
      5. READ BACK the config.xml and verify MARKER is present + ANCHOR still present
      6. Warn if no compile-verification build is triggered (user should trigger one)
    """
    if len(args) < 2:
        print("usage: deploy-groovy JOB ANCHOR INSERT_FILE [--marker MARKER]")
        return 1
    anchor = args[0]
    insert_file = args[1]
    marker = None
    if "--marker" in args:
        marker = args[args.index("--marker") + 1]

    job_path = _job(job)
    print(f"[deploy-groovy] fetching {job_path}/config.xml ...")
    cfg = _get_raw(f"{job_path}/config.xml").decode("latin-1")
    print(f"  got {len(cfg)} bytes")

    # Check 1: marker already present? -> skip (no duplicate)
    if marker and marker in cfg:
        print(f"  MARKER '{marker}' already present -> SKIP (avoid duplicate)")
        return 0

    # Check 2: anchor present?
    if anchor not in cfg:
        print(f"  ERROR: anchor not found in config.xml")
        return 1

    # Read insert content
    with open(insert_file, "r", encoding="utf-8") as f:
        insert = f.read()

    # Insert before anchor
    idx = cfg.find(anchor)
    new_cfg = cfg[:idx] + insert + cfg[idx:]
    print(f"  inserting {len(insert)} bytes before anchor")

    # POST back as latin-1
    import http.cookiejar

    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op)
    req = urllib.request.Request(
        f"{BASE}/{job_path}/config.xml",
        method="POST",
        data=new_cfg.encode("latin-1"),
    )
    req.add_header("Authorization", f"Basic {AUTH}")
    req.add_header("Content-Type", "application/xml")
    req.add_header(crumb["crumbRequestField"], crumb["crumb"])
    resp = op.open(req, timeout=30)
    print(f"  POST HTTP {resp.status}")

    # READ BACK + verify (fault-ledger 27 rule 1)
    print(f"  reading back config.xml to verify...")
    verify_cfg = _get_raw(f"{job_path}/config.xml").decode("latin-1")
    if marker:
        if marker not in verify_cfg:
            print(f"  ERROR: marker '{marker}' NOT found in readback!")
            return 1
        print(f"  marker present in readback: OK")
    if anchor not in verify_cfg:
        print(f"  ERROR: anchor missing in readback!")
        return 1
    print(f"  anchor present in readback: OK")

    # Check for duplicates (fault-ledger 27 rule 2)
    if marker:
        count = verify_cfg.count(marker)
        if count > 1:
            print(
                f"  WARNING: marker appears {count} times (duplicate!) — manual cleanup needed"
            )
            return 1
        print(f"  marker count = {count}: OK (no duplicate)")

    print(f"  deploy OK — trigger a build on {job} to verify groovy compiles")
    return 0


def cmd_create(new, src):
    """Create a Jenkins job NEW by copying SRC's config.xml (same-as clone).
    Both args accept an alias or a full path like DEV/job/NAME. NEW must not
    already exist (Jenkins returns 400 'already exists' if it does). Uses the
    write-preferred credential (JENKINS_USER/PASS) + a crumb. The folder is
    derived from NEW's path so the clone lands in the same folder.
        create reg2 reg             # clone DEV/job/GRS-SYSTEMTEST-REG -> ...-REG-02
        create DEV/job/X DEV/job/Y  # explicit paths
    """
    new_path = _job(new)
    if "/job/" not in new_path:
        sys.exit(
            f"create: cannot derive folder from '{new_path}' — use an alias or a DEV/job/NAME path"
        )
    folder_path, new_name = new_path.rsplit("/job/", 1)
    src_path = _job(src)
    print(f"[create] fetching {src_path}/config.xml ...")
    cfg = _get_raw(f"{src_path}/config.xml")
    if not cfg:
        sys.exit(f"create: empty config.xml from {src_path} — aborting")
    print(
        f"[create] got config.xml ({len(cfg)} bytes); POSTing createItem name={new_name} under {folder_path}"
    )
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op)
    url = (
        f"{BASE}/{folder_path}/createItem?name={urllib.parse.quote(new_name, safe='')}"
    )
    req = urllib.request.Request(url, data=cfg, method="POST")
    req.add_header("Authorization", f"Basic {AUTH}")
    req.add_header(crumb["crumbRequestField"], crumb["crumb"])
    req.add_header("Content-Type", "application/xml")
    try:
        r = op.open(req, timeout=30)
        print(f"created {new_path} from {src_path}: status={r.status} url={r.url}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        sys.exit(f"create FAILED: HTTP {e.code} {e.reason}\n{body}")


SRLOG_VERDICT = (
    r"PASSED|FAILED|ERROR|SKIPPED|Skipping|=+ .*(passed|failed|error|skipped)|"
    r"AssertionError|Traceback|pytest.skip|INTERNALERROR"
)


def cmd_srlog(job, num, pattern=None):
    """Self-runner verdict for a DETACHED lane (LOCAL1/LOCAL2): sentinel + pytest log.

        srlog local2 134,135              # compare two builds
        srlog local2 135 "baseline|bypass"

    Those lanes run pytest via a scheduled task on the VM, so consoleText holds
    only pipeline noise (build 135: 128 lines, none of them the failure) — the
    verdict lives in the ARCHIVED results/selfrunner_<N>.log + .sentinel.json.
    Fetching them from Jenkins keeps RCA off the VM: no SSH, works after the VM
    is reimaged or the workspace is wiped by the next run.

    pattern defaults to the verdict/traceback markers; "fresh" forces a re-fetch.
    """
    jp = _job(job)
    short = jp.split("/")[-1]
    for n in [x.strip() for x in str(num).split(",") if x.strip()]:
        print(f"\n===== {short} build {n} =====")
        d = _get(f"{jp}/{n}/api/json?tree=artifacts[relativePath],result,description")
        print(f"result={d.get('result')} desc={(d.get('description') or '').strip()}")
        rels = [a["relativePath"] for a in d.get("artifacts", [])]
        want = [r for r in rels if "selfrunner" in r]
        if not want:
            print(
                f"no selfrunner artifact (archived: {rels or 'none'}) — not a detached lane?"
            )
            continue
        for rel in sorted(want):  # .json sorts before .log: sentinel first
            path = _tmp_path(f"{short}_{n}_{rel.rsplit('/', 1)[-1]}")
            if pattern == "fresh" or not os.path.exists(path):
                open(path, "wb").write(
                    _get_raw(f"{jp}/{n}/artifact/{urllib.parse.quote(rel)}")
                )
            lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
            if rel.endswith(".json"):
                print(f"-- {rel} {' '.join(ln.strip() for ln in lines)[:400]}")
                continue
            hits = [ln for ln in lines if re.search(pattern or SRLOG_VERDICT, ln)]
            print(f"-- {rel} ({len(lines)} lines, {len(hits)} match) [{path}]")
            print("\n".join(ln[:240] for ln in (hits or lines)[-25:]))


def cmd_artifacts(job, num, dest=None):
    """List (dest omitted) or download (dest given) a build's archived artifacts.

    List:      artifacts reg 84
    Download:  artifacts reg 84 C:/tmp/reg84   (downloads ALL artifacts, preserving
               relativePath under dest; prints each saved path + size)
    """
    jp = _job(job)
    d = _get(f"{jp}/{num}/api/json?tree=artifacts[relativePath,fileName]")
    arts = d.get("artifacts", [])
    if not arts:
        print(f"no archived artifacts on {jp} #{num}")
        return
    if not dest:
        # Sizes come from a HEAD per artifact (never a download): Jenkins'
        # api/json lists names but no sizes, and "what does keeping this build
        # cost / which file is the whale?" is a storage-retention question that
        # was otherwise an ad-hoc curl.
        rows, total, unknown = [], 0, 0
        for a in arts:
            rel = a["relativePath"]
            size = _head_size(f"{jp}/{num}/artifact/{urllib.parse.quote(rel)}")
            if size is None:
                unknown += 1
            else:
                total += size
            rows.append((size, rel))
        for size, rel in sorted(rows, key=lambda r: -(r[0] or 0)):
            print(
                f"  {'     ?' if size is None else f'{size / 1048576:6.1f}'} MB  {rel}"
            )
        print(
            f"\n{len(arts)} artifact(s), total {total / 1048576:.1f} MB"
            + (f" ({unknown} size unknown)" if unknown else "")
            + ". Pass a dest dir to download."
        )
        return
    os.makedirs(dest, exist_ok=True)
    for a in arts:
        rel = a["relativePath"]
        data = _get_raw(f"{jp}/{num}/artifact/{urllib.parse.quote(rel)}")
        out = os.path.join(dest, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(data)
        print(f"saved {out}  ({len(data)} bytes)")
    print(f"\n{len(arts)} artifact(s) -> {dest}")


def cmd_backup(job, num, pattern=None):
    """Extract a build's backup_nsc_*.zip (client logs/config bundle) and grep it.

        backup local2 142                       # list every file inside
        backup local2 142 "msi_install|1603"    # grep across all extracted text

    The archive is a NESTED zip (outer backup_nsc_<host>_<n>.zip contains an
    inner zip with Windows backslash paths) — this unwraps both layers once
    into C:/tmp/<job>_<n>_backup/ and reuses that cache on repeat calls
    (delete the dir to force a re-extract). Replaces the hand-rolled
    curl+unzip recipe in CLAUDE.md's "Step 3/4" — use this instead.
    """
    import zipfile

    jp = _job(job)
    short = jp.split("/")[-1]
    outdir = _tmp_path(f"{short}_{num}_backup")
    if not os.path.isdir(outdir):
        d = _get(f"{jp}/{num}/api/json?tree=artifacts[relativePath]")
        rels = [a["relativePath"] for a in d.get("artifacts", [])]
        want = [r for r in rels if "backup_nsc" in r and r.endswith(".zip")]
        if not want:
            print(f"no backup_nsc_*.zip artifact (archived: {rels or 'none'})")
            return
        outer = _get_raw(f"{jp}/{num}/artifact/{urllib.parse.quote(want[0])}")
        os.makedirs(outdir, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(outer)) as zf:
            zf.extractall(outdir)
        # unwrap any inner zip(s) found inside the outer extract, one level
        for root, _dirs, files in list(os.walk(outdir)):
            for fn in files:
                if fn.lower().endswith(".zip"):
                    inner_path = os.path.join(root, fn)
                    with zipfile.ZipFile(inner_path) as zf:
                        zf.extractall(root)
    matches = []
    for root, _dirs, files in os.walk(outdir):
        for fn in files:
            if fn.lower().endswith(".zip"):
                continue
            full = os.path.join(root, fn)
            if pattern:
                try:
                    text = open(full, encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if re.search(pattern, line):
                        matches.append(f"{full}:{i}: {line[:240]}")
            else:
                matches.append(full)
    print(f"[{outdir}] {len(matches)} {'match' if pattern else 'file'}(es)")
    print("\n".join(matches[:200]))


def cmd_rename(job, new_name):
    """Rename a Jenkins job via /doRename (keeps build history — Jenkins moves
    the whole job dir). NEW must not already exist. Job ref = alias or
    DEV/job/NAME; new_name = the plain new job name (no path).
        rename localtest GRS-SYSTEMTEST-LOCAL1
    """
    import http.cookiejar

    jp = _job(job)
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op)
    url = f"{BASE}/{jp}/doRename?newName={urllib.parse.quote(new_name, safe='')}"
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("Authorization", f"Basic {AUTH}")
    req.add_header(crumb["crumbRequestField"], crumb["crumb"])
    try:
        r = op.open(req, timeout=30)
        print(f"renamed {jp} -> {new_name}: status={r.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        sys.exit(f"rename FAILED: HTTP {e.code} {e.reason}\n{body}")


CMDS = {
    "jobs": cmd_jobs,
    "builds": cmd_builds,
    "params": cmd_params,
    "findruns": cmd_findruns,
    "status": cmd_status,
    "config": cmd_config,
    "config-grep": cmd_config_grep,
    "status-multi": cmd_status_multi,
    "desc": cmd_desc,
    "console": cmd_console,
    "cmpfail": cmd_cmpfail,
    "monitor": cmd_monitor,
    "running": cmd_running,
    "trigger": cmd_trigger,
    "retrigger": cmd_retrigger,
    "replay": cmd_replay,
    "abort": cmd_abort,
    "artifacts": cmd_artifacts,
    "backup": cmd_backup,
    "srlog": cmd_srlog,
    "poll": cmd_poll,
    "node": cmd_node,
    "preflight": cmd_preflight,
    "dc-pick": cmd_dcpick,
    "recover": cmd_recover,
    "create": cmd_create,
    "rename": cmd_rename,
    "delete": cmd_delete,
    "clone-configs": cmd_clone_configs,
    "deploy-groovy": cmd_deploy_groovy,
}


def main(argv):
    if not argv or argv[0] not in CMDS:
        print(__doc__)
        print("aliases:", ", ".join(f"{k}={v}" for k, v in ALIASES.items()))
        return 2
    CMDS[argv[0]](*argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
