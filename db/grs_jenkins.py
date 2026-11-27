#!/usr/bin/env python
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
  iterations JOB NUM                  per-case "iter N/M" verdict from the console: pass count,
                                      failed iteration, last started, COMPLETE/FAILED/INCOMPLETE.
  cmpfail JOB NUM,NUM,... [PAT...]    compare >=2 builds' failures: result + every FAILED
                                      test id + counts of fixed evidence patterns (+ extras).
                                      NUM may be comma-separated (e.g. 138,139) to do several in one call.
  srlog JOB NUM[,NUM...] [PAT]  DETACHED lanes (local1/local2): archived
                                results/selfrunner_<N>.sentinel.json + .log, grepped.
                                Use this instead of `console` for those lanes — their
                                consoleText never contains the pytest verdict.
  artifacts JOB NUM [DEST]      list (no DEST) or download every archived artifact
  config-grep PATTERN JOB...    match count of PATTERN in each JOB's config.xml, one call
  releases [PREFIX]            list artifactory client-builds/release trains; with a prefix
                                (e.g. release-138) verify it exists (exit 1 = N/A)
  backup JOB NUM [PATTERN]      extract build's backup_nsc_*.zip (nested zip of
                                client logs/config) into C:/tmp, grep PATTERN
                                (or list files). Cached — delete the dir to redo.
  monitor JOB NUM [IVL] [PAT]   poll until finished, then print checkpoints
  running                       every executor busy right now (cross-job VM safety)
  health                        Built-In Node memory/swap/disk + concurrent-build count —
                                 run BEFORE blaming groovy/tenant/VM when a build looks stuck
  trigger JOB k=v k=v ...       safety-check + crumb + POST buildWithParameters
                                 (HOSTNAME must be in memory/reference_vm_ip_map.md's
                                 known set, or REFUSED -- only the user's explicit
                                 same-turn _allow_vm=1 clears an unlisted VM)
  retrigger JOB SRC k=v...      copy SRC build's params, apply k=v overrides, trigger
                                (one short call — no need to re-type all params)
  replay JOB NUM [SCRIPT_FILE]  pipeline Replay: re-run NUM with same params (SCRIPT_FILE
                                overrides the Groovy; omit to replay the original)
  abort JOB NUM                 stop a build
  pause JOB                     disable a job (isDisabled flag only, no config.xml/groovy edit)
                                 -- blocks cron + chained + manual triggers; prints before/after state
  resume JOB                    re-enable a job paused by `pause`
  create NEW SRC                clone Jenkins job NEW from SRC's config.xml (same-as)
                                (NEW/SRC = alias or DEV/job/NAME; NEW must not exist)
  node-create NAME [remoteFS] [label]   create a new inbound-agent node (DumbSlave/
                                JNLPLauncher/Always) -- workaround for a node stuck
                                offline with a controller-side ghost channel that
                                survives an agent restart + VM reboot (needs
                                Agent/Create, distinct from Agent/Disconnect)
  node-secret NAME              print NAME's inbound-agent connection secret
                                (parsed from jenkins-agent.jnlp; needed to launch
                                the agent.jar on the target VM after node-create)
  node-disconnect NAME [MSG]    POST doDisconnect for NAME (needs Agent/Disconnect)
  sync-pipeline-script JOB GROOVY_FILE   replace a job's WHOLE cached <script>
                                 with GROOVY_FILE's content (full-file sync,
                                 unlike deploy-groovy's marker-anchored insert).
                                 Resolves JOB via _job() so a
                                 _NEW_BASE_ALIAS_OVERRIDE entry is honored.

JOB accepts an alias (see ALIASES) or a full path like DEV/job/GRS-AUSTIN.

Env overrides: JENKINS_BASE, JENKINS_USER, JENKINS_PASS.
"""

import os as _nw_os
import sys as _nw_sys

_nw_sys.path.insert(0, _nw_os.path.dirname(_nw_os.path.abspath(__file__)))
import nowindow  # noqa: E402,F401  (pythonw: no console windows from child processes)

import base64
import datetime
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

# Two-server split (2026-09-08 migration): the 7 SystemTest cron lanes moved to
# a dedicated server; every other job (PDV/GRS-WIN-DEV/PROD/AUSTIN-general/etc)
# stays put. JENKINS_BASE, if set, is a HARD override and forces every request
# (old callers/recipes that export it keep working unchanged). Otherwise each
# request auto-routes by job path via `_base_for()` -- callers no longer need
# to remember which server a given lane lives on.
_FORCE_BASE = os.getenv("JENKINS_BASE")
_OLD_BASE = "http://10.136.208.148:8080"
_NEW_BASE = os.getenv("JENKINS_BASE_SYSTEMTEST", "http://10.136.208.217:8080")
BASE = _FORCE_BASE or _OLD_BASE  # legacy fallback constant; prefer _base_for(path)
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
    "mac2-ssh": "DEV/job/GRS-SYSTEMTEST-MAC2-SSH",
    "andy": "DEV/job/GRS-ANDY",
}

# SystemTest lanes on _NEW_BASE (217). git44 migrated 7 lanes on 2026-09-08,
# MAC2-SSH added 2026-09-30. Keep in sync with ALIASES above -- this is the
# ONLY place that needs updating if more lanes move.
_MIGRATED_JOB_PATHS = (
    "DEV/job/GRS-SYSTEMTEST-REG",  # matches both REG and REG-02 (substring)
    "DEV/job/GRS-SYSTEMTEST-LOCAL1",
    "DEV/job/GRS-SYSTEMTEST-LOCAL2",
    "DEV/job/GRS-SYSTEMTEST-MAC1",
    "DEV/job/GRS-SYSTEMTEST-MAC2",
    "DEV/job/GRS-SYSTEMTEST-MAC2-SSH",
    "DEV/job/GRS-AUSTIN",
    "DEV/job/GRS-ANDY",  # cloned from GRS-AUSTIN 2026-09-29, same server
)


def _base_for(path_or_jobpath):
    """Resolve which Jenkins server a request belongs on. JENKINS_BASE (if
    set) always wins. Otherwise: does this path/job-path mention one of the
    7 migrated SystemTest lanes? -> _NEW_BASE (217). Everything else
    (PDV/GRS-WIN-DEV/PROD/computer-wide endpoints with no job in the path) ->
    _OLD_BASE (148), which is still the correct default for those."""
    if _FORCE_BASE:
        return _FORCE_BASE
    for jp in _MIGRATED_JOB_PATHS:
        if jp in path_or_jobpath:
            return _NEW_BASE
    return _OLD_BASE


def _all_bases():
    """Every distinct server this tool talks to, for cross-server sweeps
    (e.g. `running`'s VM safety check must see busy executors on BOTH)."""
    return [_FORCE_BASE] if _FORCE_BASE else [_OLD_BASE, _NEW_BASE]


# git44's pre-created new-server job configs are byte-identical to the old
# ones EXCEPT this one name (verified live 2026-09-08): the old server calls
# it GRS-SYSTEMTEST-REG-02, the new server GRS-SYSTEMTEST-REG2 (no hyphen).
# Every other migrated lane's name matches exactly across both servers.
_NEW_BASE_ALIAS_OVERRIDE = {
    "reg2": "DEV/job/GRS-SYSTEMTEST-REG2",
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


def _known_job_alias(job):
    """Map a hand-typed job name to its alias key, or None. Accepts the bare job
    name ('GRS-SYSTEMTEST-MAC2-SSH') and the folder form ('DEV/GRS-SYSTEMTEST-MAC2-SSH'),
    which are not valid Jenkins paths and used to end in a bare HTTP 404."""
    last = job.strip("/").split("/")[-1].lower()
    for key, path in ALIASES.items():
        if path.rsplit("/", 1)[-1].lower() == last:
            return key
    return None


def _job(job):
    if job not in ALIASES and not job.startswith("job/") and "/job/" not in job:
        job = _known_job_alias(job) or job
    p = ALIASES.get(job, job)
    p = p if p.startswith("job/") else "job/" + p
    # If this alias auto-routes to _NEW_BASE and that server names the job
    # differently, swap in the new-server path (see _NEW_BASE_ALIAS_OVERRIDE).
    if _base_for(p) == _NEW_BASE and job in _NEW_BASE_ALIAS_OVERRIDE:
        p = _NEW_BASE_ALIAS_OVERRIDE[job]
        p = p if p.startswith("job/") else "job/" + p
    return p


def _get(path, server=None):
    if "?" in path:
        p, _, q = path.partition("?")
        path = p + "?" + urllib.parse.quote(q, safe="=&")
    eff_base = server or _base_for(path)
    req = urllib.request.Request(f"{eff_base}/{path}")
    req.add_header("Authorization", f"Basic {AUTH}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# Public alias for cross-module import (e.g. db.py's backfill-bitness): every
# in-file call site keeps using `_get` (the underscore is correct for THIS
# module's own internal use), but a leading underscore signals "not for
# import" to any OTHER module -- name the public boundary explicitly instead
# of reaching into a private symbol (owner, 2026-09-13).
get_json = _get


_BROAD_AUTH = base64.b64encode(b"nsclientadmin:nsclientadmin").decode()


def _get_broad(path):
    """Same as `_get` but authenticates as `nsclientadmin:nsclientadmin` -- the
    documented broader (computer/node-view) credential (see jenkins.md
    memory). The module-default `USER`/`PASS` account can read job/build data
    fine but has no permission for raw node `monitorData` (memory/disk/swap):
    Jenkins doesn't error, it just returns `monitorData: {}`, which silently
    looked like "no data available" until traced while building `health`
    (2026-09-07). Use this for any Computer-API call that needs monitorData.
    """
    if "?" in path:
        p, _, q = path.partition("?")
        path = p + "?" + urllib.parse.quote(q, safe="=&")
    eff_base = _base_for(path)
    req = urllib.request.Request(f"{eff_base}/{path}")
    req.add_header("Authorization", f"Basic {_BROAD_AUTH}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _get_raw(path, server=None):
    """Raw bytes GET (e.g. a job's config.xml) — not JSON."""
    if "?" in path:
        p, _, q = path.partition("?")
        path = p + "?" + urllib.parse.quote(q, safe="=&")
    eff_base = server or _base_for(path)
    req = urllib.request.Request(f"{eff_base}/{path}")
    req.add_header("Authorization", f"Basic {AUTH}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _head_size(path):
    """Content-Length of one artifact, or None. HEAD only — never downloads.

    Jenkins' api/json lists artifact NAMES but not sizes, which is why "how big
    is this build's bundle?" used to be an ad-hoc curl.
    """
    eff_base = _base_for(path)
    req = urllib.request.Request(f"{eff_base}/{path}", method="HEAD")
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


# Public alias, same reasoning as get_json above.
params_from_actions = _params


def _job_param_names(jp):
    """The job's DECLARED parameter names (schema), not a past build's values.

    trigger's k=v pairs went straight to buildWithParameters with no check
    against this -- LOCAL2 build 60 (git44, 2026-09-09) passed `tenant=1457`
    instead of the real param name `tenant_info`; Jenkins silently ignored the
    unknown key and ran the job's DEFAULT tenant (1119) for 6 minutes before
    anyone noticed. Returns None if the job has no ParametersDefinitionProperty
    at all (caller must then skip the check, not treat it as an empty set).
    """
    d = _get(f"{jp}/api/json?tree=property[parameterDefinitions[name]]")
    for prop in d.get("property", []) or []:
        defs = prop.get("parameterDefinitions")
        if defs is not None:
            return {p["name"] for p in defs}
    return None


# ---------------------------------------------------------------------------
# Chapter lock -- campaign-scoped tenant pin (owner 2026-09-09)
#
# _job_param_names catches a MISSPELLED param name (tenant= instead of
# tenant_info=). It cannot catch a correctly-spelled tenant_info that carries
# the WRONG tenant number -- e.g. a stale --tenant 1334 pasted from an old
# command during the 1457 (fed) campaign. A "chapter" pins the one tenant ID
# every `trigger` call must use for the campaign's duration; `chapter set`
# is a deliberate one-time act (like coord.py claim), not something inferred.
# ---------------------------------------------------------------------------
CHAPTER_STATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "active_chapter.json"
)


def _load_chapters():
    """Return the list of active chapter entries, migrating the legacy
    single-dict-on-disk format (pre-2026-09-11, no --job scoping) transparently."""
    if not os.path.exists(CHAPTER_STATE):
        return []
    with open(CHAPTER_STATE, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "chapters" in data:
        return data["chapters"]
    if isinstance(data, dict):  # legacy: one global (job=None) chapter
        data.setdefault("job", None)
        return [data]
    return []


def _save_chapters(chapters):
    os.makedirs(os.path.dirname(CHAPTER_STATE), exist_ok=True)
    with open(CHAPTER_STATE, "w", encoding="utf-8") as f:
        json.dump({"chapters": chapters}, f, indent=2)


def _active_chapter(job=None):
    """The chapter that applies to `job` (alias string) right now: a chapter
    scoped to exactly this job wins; otherwise fall back to the global
    (job=None) chapter, if any. Two lanes can legitimately run two different
    tenants at once (e.g. REG on 1457 while REG2 runs 1347) -- a single global
    lock can't express that, so `chapter set --job JOB` scopes one to a lane
    without affecting any other lane's trigger calls."""
    chapters = _load_chapters()
    for ch in chapters:
        if ch.get("job") == job:
            return ch
    for ch in chapters:
        if ch.get("job") is None:
            return ch
    return None


def cmd_chapter(*args):
    """Campaign-scoped tenant lock for `trigger`.

        chapter set NAME --tenant N [--env FILE] [--job JOB]   arm a lock
        chapter show                                            list active locks
        chapter clear [--job JOB | --all]                       drop one/all

    While armed, `trigger`'s tenant_info=... must carry --tenant N (the
    locked one) or it's REFUSED before touching the queue -- catches a
    correctly-SPELLED but wrong-VALUE tenant that _job_param_names cannot see
    (see the module comment above). `--env FILE` is optional context recorded
    for the record only; --tenant is the ONLY value trigger. enforces.

    Without `--job`, the lock is global (applies to every job's trigger call,
    the original 2026-09-09 behavior) and there can be only one -- `set`
    without `--job` replaces the existing global chapter. With `--job JOB`,
    the lock only applies to that job alias and coexists with any other
    job-scoped or global chapter (added 2026-09-11 for the REG/REG2 tenant
    1457/1347 split-lane rearrangement).
    """
    if not args:
        print(
            "usage: chapter set NAME --tenant N [--env FILE] [--job JOB] | show | clear [--job JOB | --all]"
        )
        return 1
    action, rest = args[0], list(args[1:])
    if action == "show":
        chapters = _load_chapters()
        print(json.dumps(chapters, indent=2) if chapters else "no active chapter")
        return 0
    if action == "clear":
        job = None
        clear_all = "--all" in rest
        if "--job" in rest:
            i = rest.index("--job")
            if i + 1 < len(rest):
                job = rest[i + 1]
        chapters = _load_chapters()
        if clear_all:
            _save_chapters([])
            print(f"cleared all {len(chapters)} chapter(s)")
            return 0
        remaining = [c for c in chapters if c.get("job") != job]
        removed = len(chapters) - len(remaining)
        _save_chapters(remaining)
        print(f"cleared {removed} chapter(s) matching job={job!r}")
        return 0
    if action == "set":
        if not rest:
            print("usage: chapter set NAME --tenant N [--env FILE] [--job JOB]")
            return 1
        name, kv, i = rest[0], {}, 1
        while i < len(rest):
            if rest[i] in ("--tenant", "--env", "--job") and i + 1 < len(rest):
                key = {"--tenant": "tenant", "--env": "env_file", "--job": "job"}[
                    rest[i]
                ]
                kv[key] = rest[i + 1]
                i += 2
            else:
                i += 1
        if "tenant" not in kv:
            print("REFUSE: chapter set needs --tenant N")
            return 1
        kv.setdefault("job", None)
        state = {
            "name": name,
            "set_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **kv,
        }
        chapters = [c for c in _load_chapters() if c.get("job") != state["job"]]
        chapters.append(state)
        _save_chapters(chapters)
        print(f"chapter armed: {state}")
        return 0
    print(f"unknown chapter action: {action!r} (set|show|clear)")
    return 1


def _crumb(opener, server=None):
    eff_base = server or BASE
    req = urllib.request.Request(f"{eff_base}/crumbIssuer/api/json")
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
        try:
            xml = _get_raw(f"{_job(j)}/config.xml").decode("utf-8", "replace")
        except Exception as e:  # a job name that does not exist under DEV must not kill the whole sweep
            print(f"\n{j} — not readable ({type(e).__name__}); skipped")
            continue
        lines = [ln.strip() for ln in xml.splitlines() if re.search(pattern, ln)]
        uniq = list(dict.fromkeys(lines))
        print(f"\n{j} — {len(lines)} match(es), {len(uniq)} distinct")
        for ln in uniq[:5]:
            print(f"  {ln[:160]}")


def _refuse_flag_dest(cmd, dest):
    """A destination that starts with "--" is a mistyped option, not a path. 2026-11-17: `artifacts reg2 450 --list`
    created <repo>/--list/ because the positional dest took the flag as a directory name."""
    if dest and str(dest).startswith("--"):
        print(
            f"{cmd}: '{dest}' looks like an option, not a destination directory; "
            f"this command takes positional arguments only (see its docstring). Nothing was created.",
            file=sys.stderr,
        )
        sys.exit(2)


def cmd_clone_configs(dest, *jobs):
    """Write each job's live config.xml into dest/ as <job>.live.xml, one file
    per job, in a SINGLE invocation (so it runs under the allowlisted prefix and
    never re-prompts). Defaults to the 4 SystemTest jobs when none are named.
      clone-configs C:/git/claude-resource/jenkins_groovy_TRUE
      clone-configs <dir> reg reg2 austin localtest win-dev
    dest is created if missing. Prints the byte/line count per job."""
    _refuse_flag_dest("clone-configs", dest)
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
    # Cache key MUST include the server: 148 and 217 both number builds from 1,
    # so "job+build number" alone collides across the two-server split (found
    # 2026-09-08 -- served an 08-26 148 build's console as if it were 217's
    # fresh build #3, a stale-data false finding). Tag with the base's last
    # dotted octet (148/217) -- short, human-legible, stable per server.
    _srv_tag = _base_for(_job(job)).rsplit(".", 1)[-1].split(":")[0]
    path = _tmp_path(f"build_{_job(job).split('/')[-1]}_{n}_srv{_srv_tag}.txt")
    partial = path + ".partial"  # marker: the cached file was fetched mid-build
    building = _get(f"{_job(job)}/{n}/api/json?tree=building").get("building", False)
    stale_partial = os.path.exists(partial)
    if force or building or stale_partial or not os.path.exists(path):
        req = urllib.request.Request(
            f"{_base_for(_job(job))}/{_job(job)}/{n}/consoleText"
        )
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


CONSOLE_LINE_MAX = 300


def _cut_line(ln, limit=CONSOLE_LINE_MAX):
    return ln if len(ln) <= limit else f"{ln[:limit]} ...[+{len(ln) - limit} chars, see the saved file]"


_VERDICT_SUMMARY = re.compile(r"^=+ .*\b(passed|failed|skipped|error|errors|deselected)\b.* in [\d.]+s")


def cmd_verdict(job, num):
    """The lines that settle one build, in one call: `verdict <job> <build>`.

    Prints the result, the head line of the checkout, the client version(s), pytest's summary line, every FAILED/ERROR
    node and the first assertion (`E ` line) and skip reason, each in FULL (no cut): the pieces a triage note needs, so a
    conclusion is never written without the build's own lines. A self-runner lane (LOCAL2, MAC2) keeps pytest's output in
    the archived `results/selfrunner_<n>.log`, not in the console: when the console has no pytest summary this reads that
    artifact instead and says so. Nothing is written to the build or to a VM."""
    jp = _job(job)
    meta = _get(f"{jp}/{num}/api/json?tree=result,building,timestamp,fullDisplayName")
    print(f"== {meta.get('fullDisplayName')}  result={meta.get('result')} building={meta.get('building')} "
          f"started={_fmt_ts(meta.get('timestamp', 0))}")
    console = _console_text(job, num)
    lines = console
    source = "console"
    if not any(_VERDICT_SUMMARY.match(ln) for ln in lines):
        arts = _get(f"{jp}/{num}/api/json?tree=artifacts[relativePath]").get("artifacts", [])
        pick = [a["relativePath"] for a in arts if re.search(r"selfrunner_\d+\.log$", a["relativePath"])]
        if pick:
            data = _get_raw(f"{jp}/{num}/artifact/{urllib.parse.quote(pick[0])}")
            lines = data.decode("utf-8", errors="replace").splitlines()
            source = f"artifact {pick[0]}"
    print(f"-- pytest lines read from: {source}")
    head = next((ln for ln in console if re.match(r"^[0-9a-f]{7,10} \S", ln)), "(no head line)")

    def show(label, rows, limit=3):
        for r in rows[:limit]:
            print(f"{label}: {r.strip()}")
        if not rows:
            print(f"{label}: (none)")

    show("head", [head], 1)
    versions = list(dict.fromkeys(ln.strip() for ln in lines if "Client version::" in ln))
    show("client version", versions, 4)
    show("summary", [ln for ln in lines if _VERDICT_SUMMARY.match(ln)][-1:], 1)
    show("failed", [ln for ln in lines if re.match(r"^(FAILED|ERROR) ", ln)], 6)
    show("assertion", [ln for ln in lines if re.match(r"^E\s+\S", ln)], 2)
    show("skip", [ln for ln in lines if "Skipped:" in ln or "pytest.skip" in ln], 2)
    crit = [ln for ln in lines if "CRITICAL" in ln]
    print(f"critical lines: {len(crit)}" + (f" -- first: {crit[0].strip()}" if crit else ""))


def cmd_iterations(job, num):
    """Per-case iteration verdict of one build, from its console: `iterations <job> <build>`.

    Reads the "iter N/M PASS|FAIL" and "scenario iter N/M start" markers the SystemTest cases log
    (the pytest-captured copy repeats the start lines, so each N is counted once). Prints, per case id,
    how many iterations passed, which failed, the highest one started, and a one-line verdict.
    SUCCESS from Jenkins can hide a short run; this is the line to quote as "N/M".
    """
    txt = _console_text(job, num)
    done = {}  # case -> {n: verdict}
    started = {}  # case -> highest n started
    total = {}
    rx = re.compile(r"\[([A-Z0-9-]+)\] =+ (?:scenario )?iter (\d+)/(\d+) (PASS|FAIL|start)\b")
    for ln in txt:
        m = rx.search(ln)
        if not m:
            continue
        case, n, tot, what = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        total[case] = tot
        if what == "start":
            started[case] = max(started.get(case, 0), n)
        else:
            done.setdefault(case, {})[n] = what
    if not total:
        print("no 'iter N/M' markers in this console (case without per-iteration logging, or still installing)")
        return
    for case in sorted(total):
        d = done.get(case, {})
        passed = sorted(n for n, v in d.items() if v == "PASS")
        failed = sorted(n for n, v in d.items() if v == "FAIL")
        tot = total[case]
        if len(passed) == tot and not failed:
            verdict = f"COMPLETE {tot}/{tot}"
        elif failed:
            verdict = f"FAILED at iteration {failed[0]}"
        elif started.get(case, 0) > (max(passed) if passed else 0):
            verdict = f"STOPPED in iteration {started[case]} (started, no PASS line; failed or still running)"
        else:
            verdict = "INCOMPLETE (no FAIL line; run ended or is still going)"
        print(
            f"{case}: pass={len(passed)}/{tot} fail={failed or 'none'} "
            f"last_started={started.get(case, 0)} -> {verdict}"
        )


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
        # A matching line can be a whole config JSON dump (20 KB seen on REG 221); cut it so
        # one monitor/console call never floods the chat. The saved file keeps full lines.
        if pattern:
            hits = [ln for ln in txt if re.search(pattern, ln)]
            print("\n".join(_cut_line(ln) for ln in hits[-40:]))
        else:
            print("\n".join(_cut_line(ln) for ln in txt[-25:]))


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
            print(
                f"  residual processes: {[f'{name}(pid={pid})' for name, pid in procs]}"
            )


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

    _srv_tag = _base_for(_job(job)).rsplit(".", 1)[-1].split(":")[0]
    state_path = _tmp_path(
        f"pollstate_{_job(job).split('/')[-1]}_{num}_srv{_srv_tag}.json"
    )
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
        f"{_base_for(_job(job))}/{_job(job)}/{num}/logText/progressiveText?start={start}"
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


def _run_ref_running(ref):
    """True while the build named 'job#num' (a config_guard run_ref) is still building."""
    try:
        j, n = ref.split("#")
        return bool(_get(f"{_job(j)}/{n}/api/json?tree=building").get("building"))
    except Exception:
        return False


def cmd_monitor(job, num, ivl="90", pattern=None):
    ivl = int(ivl)
    for _ in range(400):
        d = _get(f"{_job(job)}/{num}/api/json?tree=building,result")
        if not d.get("building"):
            print(f"===== #{num} FINISHED result={d.get('result')} =====")
            cmd_console(job, num, pattern or DEFAULT_PAT)
            try:  # config journal: close/restore whatever this build left (pass, fail or abort)
                import config_guard

                if job in ("local1", "local2"):
                    time.sleep(90)  # a self-runner's pytest can outlive the Jenkins build
                config_guard.settle(f"{job}#{num}")
            except ImportError:
                pass
            if job in ("reg", "reg2"):
                # REG/REG-02 post-stage lock release runs on the controller where
                # `sshpass` is missing (RCA 2026-10-15), so it leaves the shared-VM
                # lock behind and the NEXT build waits/times out. Clear it here,
                # only if THIS build is the (finished) holder.
                info, err = _vmlock_read()
                if info and info.get("BUILD") == str(num):
                    cmd_vmlock("release")
            return
        time.sleep(ivl)
    print(f"#{num} still building after long poll — stopping")


def cmd_running():
    # VM safety is cross-server since the 2026-09-08 SystemTest migration: a VM
    # busy under a lane on _NEW_BASE would look "idle" if this only ever asked
    # _OLD_BASE. Sweep every distinct server (just the one, if JENKINS_BASE
    # forces it) and merge.
    busy = False
    for srv in _all_bases():
        try:
            d = _get("computer/api/json?depth=2", server=srv)
        except Exception as ex:
            print(f"[{srv}] computer/api/json failed: {ex}")
            continue
        for c in d.get("computer", []):
            for e in c.get("executors", []) + c.get("oneOffExecutors", []):
                ce = e.get("currentExecutable") or {}
                if not ce.get("url"):
                    continue
                busy = True
                try:
                    rel = ce["url"].split(f"{srv}/")[-1]
                    j = _get(
                        rel
                        + "api/json?tree=fullDisplayName,actions[parameters[name,value]]",
                        server=srv,
                    )
                    host = _params(j.get("actions")).get("HOSTNAME", "?")
                    print(
                        f"BUSY [{srv}] {c.get('displayName')}: {j.get('fullDisplayName')} HOSTNAME={host}"
                    )
                except Exception as ex:
                    print(
                        f"BUSY [{srv}] {c.get('displayName')}: {ce.get('url')} ({ex})"
                    )
    if not busy:
        print("all executors idle")


def cmd_health():
    """Jenkins Built-In Node health: memory/swap/disk + concurrent-build count there.

    Written 2026-09-07 after GRS-AUSTIN build 262 hung with durable-task
    'process apparently never started' / 'wrapper script does not seem to be
    touching the log file' -- traced to the Built-In Node running ~10
    concurrent docker-based pipelines against 5.4GB free memory and ZERO
    swap. Run this BEFORE blaming groovy/tenant/VM when a build looks stuck
    with no real console progress past its first couple of `sh` steps --
    that symptom is host resource contention, not a script/config bug.
    """
    d = _get_broad("computer/(built-in)/api/json?tree=monitorData[*[*]]&depth=2")
    md = d.get("monitorData", {})
    swap = md.get("hudson.node_monitors.SwapSpaceMonitor", {}) or {}
    disk = md.get("hudson.node_monitors.DiskSpaceMonitor", {}) or {}

    avail_mem = swap.get("availablePhysicalMemory")
    total_mem = swap.get("totalPhysicalMemory")
    avail_swap = swap.get("availableSwapSpace")
    total_swap = swap.get("totalSwapSpace")
    disk_free = disk.get("size")
    disk_total = disk.get("totalSize")

    status = "OK"
    lines = []

    def _bump(new):
        nonlocal status
        order = {"OK": 0, "WARN": 1, "FAIL": 2}
        if order[new] > order[status]:
            status = new

    if total_mem:
        pct = avail_mem / total_mem * 100
        lines.append(
            f"memory: {avail_mem / 1e9:.1f}GB free / {total_mem / 1e9:.1f}GB total ({pct:.0f}% free)"
        )
        if pct < 10:
            _bump("FAIL")
            lines.append("  [FAIL] under 10% free memory")
        elif pct < 20:
            _bump("WARN")
            lines.append("  [WARN] under 20% free memory")
    else:
        lines.append("memory: unavailable from monitorData")

    if total_swap == 0:
        lines.append("swap: NONE configured -- no buffer if memory spikes")
        _bump("WARN")
    elif total_swap:
        pct_swap = avail_swap / total_swap * 100
        lines.append(
            f"swap: {avail_swap / 1e9:.1f}GB free / {total_swap / 1e9:.1f}GB total ({pct_swap:.0f}% free)"
        )

    if disk_total:
        pct_disk = disk_free / disk_total * 100
        lines.append(
            f"disk (/var/jenkins_home): {disk_free / 1e9:.1f}GB free / {disk_total / 1e9:.1f}GB total ({pct_disk:.0f}% free)"
        )
        if pct_disk < 10:
            _bump("FAIL")
            lines.append("  [FAIL] under 10% free disk")
    else:
        lines.append("disk: unavailable from monitorData")

    cd = _get("computer/api/json?depth=2")
    built_in_busy = total_busy = 0
    for c in cd.get("computer", []):
        for e in c.get("executors", []) + c.get("oneOffExecutors", []):
            if (e.get("currentExecutable") or {}).get("url"):
                total_busy += 1
                if c.get("displayName") == "Built-In Node":
                    built_in_busy += 1
    lines.append(
        f"concurrent builds on Built-In Node: {built_in_busy} (all nodes: {total_busy})"
    )
    if built_in_busy >= 8:
        _bump("WARN")
        lines.append(
            f"  [WARN] {built_in_busy} concurrent jobs sharing one physical node's "
            "memory/CPU/durable-task IO"
        )

    print(f"[{status}] Jenkins Built-In Node health")
    for line in lines:
        print(f"  {line}")
    if status != "OK":
        print(
            "  -> a build stuck with no console progress under this condition is host "
            "resource contention (durable-task starved), NOT a groovy/tenant/VM bug -- "
            "don't chase the wrong layer. Wait for load to drop, then retry."
        )
    return {"OK": 0, "WARN": 1, "FAIL": 2}[status]


_NODE_TREE = (
    "displayName,offline,temporarilyOffline,offlineCauseReason,numExecutors,executors[currentExecutable[url]]"
)


def _node_servers(name):
    """[(base, api json)] for EVERY Jenkins server that has a computer called NAME (empty when none answers).

    A node name can exist on both servers: 148 keeps a same-named, long-offline copy of systest-mac-tar1 while the real one
    is on 217. `node` and `node-disconnect` used to ask only the default server (148 when the path names no job), so a node
    that was online on 217 read "Connection was broken" for hours and a disconnect hit the wrong controller."""
    found = []
    for base in _all_bases():
        try:
            found.append((base, _get(f"computer/{name}/api/json?tree={_NODE_TREE}", server=base)))
        except Exception:  # noqa: BLE001 - a server without this node answers 404; that is information, not an error
            continue
    return found


def cmd_node(name):
    """One node's online/offline status + executors (pre-trigger agent check), on EVERY server that has that name."""
    found = _node_servers(name)
    if not found:
        print(f"{name}: no such node on {', '.join(_all_bases())}")
        return
    for base, d in found:
        busy = [
            e.get("currentExecutable", {}).get("url")
            for e in d.get("executors", [])
            if e.get("currentExecutable")
        ]
        print(
            f"[{base.rsplit('//', 1)[-1]}] {d.get('displayName')}: offline={d.get('offline')} "
            f"temporarilyOffline={d.get('temporarilyOffline')} reason={d.get('offlineCauseReason') or '-'} busy={len(busy)}"
        )
    if len(found) > 1:
        print(
            f"WARNING: '{name}' exists on {len(found)} servers; the readings above can differ. A lane's node is the one on "
            "the server that hosts the lane's job (SystemTest lanes: 217)."
        )
    if name.startswith("systest-mac") and any(d.get("offline") for _b, d in found):
        # the cause and the fix of an offline mac lane node are written down; point at them where the symptom shows
        print(
            "KNOWN ISSUE: an offline mac lane node (\"Connection was broken\") has a recipe: "
            "C:/git/claude-resource/troubleshooting/mac2_halfopen/README.md -- read it before restarting anything."
        )


def _is_local_lane(jp):
    """LOCAL1/LOCAL2 run pytest ON their VM (no HOSTNAME param, no SSH from the runner)."""
    return jp.endswith(("GRS-SYSTEMTEST-LOCAL1", "GRS-SYSTEMTEST-LOCAL2"))


def _local_lane_ip(jp):
    """The VM ip of a LOCAL lane from the one VM map (memory/reference_vm_ip_map.md), never a copy of the number here.
    LOCAL2 = the live row that says LOCAL2; LOCAL1 = the live row that says LOCALTEST (its old name). None when the map
    has no such live row, which keeps the old `vm-resolve` REFUSE instead of probing a guessed machine."""
    key = "LOCAL2" if jp.endswith("LOCAL2") else "LOCALTEST"
    try:
        text = open(_VM_MAP_MD, encoding="utf-8").read()
    except OSError:
        return None
    for row in text.splitlines():
        if row.startswith("|") and "✅" in row and key in row.upper() and "~~" not in row.split("|")[2]:
            m = re.search(r"\b(10\.\d+\.\d+\.\d+)\b", row)
            if m:
                return m.group(1)
    return None


# The mac lanes run pytest ON the Mac guest / host (a self-runner agent, no HOSTNAME param, no SSH from the runner), so the
# Windows VM-side probes of grs_preflight (firewall, services, msi cache) do not apply and the old gate could only say
# `vm-resolve REFUSE` (git22 reproduced it 2026-10-29: every mac trigger carried _force_preflight=1, i.e. no check at all).
# The check that DOES apply is: is the lane's Jenkins node online. A wrong name here fails loudly (the node lookup 404s -> REFUSE).
_MAC_LANE_NODE = {
    "GRS-SYSTEMTEST-MAC2-SSH": "systest-mac-host1",  # guest .3, agent on the Mac host
    "GRS-SYSTEMTEST-MAC2": "systest-mac-tar1",  # guest .2, agent inside the guest
}


def _mac_lane_node(jp):
    for suffix, node in sorted(_MAC_LANE_NODE.items(), key=lambda kv: -len(kv[0])):
        if jp.endswith(suffix):
            return node
    return None


def _mac_node_row(node):
    """One preflight row for a mac lane: the lane's node must exist and be online."""
    try:
        d = _get(f"computer/{node}/api/json?tree=offline,temporarilyOffline,offlineCauseReason", server=_NEW_BASE)
    except Exception as e:  # noqa: BLE001
        return ("vm-resolve", "REFUSE", f"mac lane node {node} cannot be read on {_NEW_BASE}: {type(e).__name__}: {e}")
    if d.get("offline"):
        return ("vm-resolve", "REFUSE", f"mac lane node {node} is OFFLINE: {d.get('offlineCauseReason') or 'no reason given'}")
    return (
        "vm-resolve",
        "PASS",
        f"mac lane: pytest runs on the VM, node {node} is online (the Windows VM probes do not apply to a mac lane)",
    )


# Where the dc of the CURRENT command came from. Set by cmd_trigger/cmd_retrigger from the local-only
# kv `_dc_given=peer|owner`; never POSTed. Owner rule 2026-11-04: the dc is NEVER read out of the env
# json -- it comes from dc-pick, or from the same lane's previous build when the tenant matches, or
# from the person who asked for the run (then _dc_given names them).
DC_SRC = [""]
DC_CASE = [""]  # the case= this trigger serves, so the dc-source guard can verify against tenant_map


def _dc_from_tenant_info(tenant_info):
    """(tenant, dc) out of a tenant_info string, either one None when absent."""
    ti = tenant_info or ""
    tn = re.search(r"--tenant\s+(\S+)", ti)
    dc = re.search(r"--dc=(\S+)", ti)
    return (tn.group(1) if tn else None), (dc.group(1) if dc else None)


def _dc_source_guard(merged, base, job=None, case=None):
    """REFUSE text when this command's dc was not justified, else ''.

    The env json's `tenants` map is tenant-id -> tenant NAME (helper_fixtures.py:79) and says nothing
    about dcs, so it is not consulted here at all. Allowed sources, in the owner's order:
      1. the person who asked for the run named the dc  -> `_dc_given=peer` (or `owner`);
      2. dc-pick (the caller passed the dc dc-pick printed -- indistinguishable here, and dc-pick is
         what fills it in for a mapped case);
      3. the SAME lane's previous build with the SAME tenant (the habitual dc for that lane).
    Anything else is refused, because a dc invented from a lookup table is how a run lands on the
    wrong config group (2026-11-04: sys1119 was "corrected" to an env-json key).
    """
    tenant, dc = _dc_from_tenant_info(merged.get("tenant_info"))
    if not dc:
        return ""
    # HARD: a tenant NAME is not a dc. The env json's `tenants` keys are tenant names
    # (nsclientauto9, nsclientauto11, deepthi, ...). If the value given as --dc is one of those keys and
    # its tenant id matches, it is a name, not a config group -- refuse it whatever the source says.
    # Owner 2026-11-04: "no such an thing, it is not adc, never use it."
    envf = re.search(r"--custom-env-file=(\S+)", merged.get("tenant_info", "") or "")
    if envf:
        env = _grs_env_json(envf.group(1)) or {}
        tmap = env.get("tenants") or {}
        if dc in tmap and str(tmap.get(dc)) == str(tenant):
            return (
                f"REFUSE: dc={dc} is the TENANT NAME in {envf.group(1)} (tenants[{dc!r}] == {tenant}), "
                "not a dc. A dc is a config group on the tenant. Use dc-pick, or the same lane's previous "
                "dc when the tenant is the same, or the dc the requester named."
            )
    src = (DC_SRC[0] or "").strip().lower()
    if src in ("peer", "owner"):
        return ""
    if src == "dcpick":
        # dc-pick is the documented default source. Rather than trusting the word "dcpick", verify the
        # pair really is a combo tenant_map offers for THIS case and THIS lane -- that is what dc-pick reads.
        try:
            m = json.load(open(_TENANT_MAP, encoding="utf-8"))
            lane = (job or "").split("/")[-1].replace("GRS-SYSTEMTEST-", "").lower()
            combos = m.get("cases", {}).get((case or "").upper(), {}).get("combos") or []
            if any(str(c.get("tenant")) == str(tenant) and c.get("dc") == dc
                   and any(str(l).lower() == lane for l in (c.get("lanes") or [])) for c in combos):
                return ""
            return (f"REFUSE: _dc_given=dcpick but tenant_map has no combo {tenant}/{dc} for case {case} on "
                    f"lane {lane or job!r}; run `grs_jenkins.py dc-pick {case} {lane}` and use what it prints.")
        except Exception as e:
            return f"REFUSE: cannot verify _dc_given=dcpick against tenant_map ({type(e).__name__}: {e})."
    prev_tenant, prev_dc = _dc_from_tenant_info((base or {}).get("tenant_info"))
    if prev_dc and prev_dc == dc and prev_tenant == tenant:
        return ""
    return (
        f"REFUSE: dc={dc} has no allowed source. The dc is NEVER derived from the env json (its "
        f"'tenants' map is tenant-id -> tenant name only). Use dc-pick's dc; or, when the tenant is the "
        f"same ({tenant}), reuse this lane's previous dc ({prev_dc or 'none recorded'}); or, if the person "
        f"who asked for the run named {dc} themselves, pass _dc_given=peer (or =owner)."
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
    if _is_local_lane(jp):
        # no HOSTNAME to scrape on these lanes: take the VM from the map so the VM-side rows run instead of a permanent
        # vm-resolve REFUSE that teaches everyone to pass _force_preflight=1
        return pf, jp, base, merged, overrides, _local_lane_ip(jp)
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
            req = urllib.request.Request(
                f"{_base_for(jp)}/{jp}/{d['number']}/consoleText"
            )
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


def _grs_env_json(rel_path):
    """Load a GRS test_environment json from THIS agent's own clone only.

    NEVER read from other agents' clones (C:/git, C:/git22, etc.) -- each
    agent is responsible for keeping their own clone up to date. Reading
    from other clones violates agent isolation and causes wrong results
    when clones are at different commits.

    Uses the caller's cwd to locate the GRS clone root via git rev-parse.

    Returns the json dict, or None if the file cannot be read.
    """
    try:
        # Find THIS agent's GRS clone root from current working directory
        # (callers always run grs_jenkins.py from inside their GRS clone)
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
        )
        if result.returncode != 0:
            return None

        grs_root = result.stdout.strip()
        env_path = os.path.join(grs_root, rel_path)

        with open(env_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
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
        # The entry may live only on the branch this build will check out (2026-11-04: git's EPAIS
        # request runs branch klin/epais-env-1136, whose env file adds tenant 1136 while main has no
        # entry, so the on-disk copy under-reports). Read the SAME file from that branch instead of
        # calling the tenant unresolvable -- local git only, still no webapi.
        br = merged.get("branch") or merged.get("GIT_BRANCH") or ""
        rel = envf.group(1)
        if br:
            try:
                import subprocess as _sp

                root = _sp.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
                # the local branch may not exist (only origin/<br>), and a fresh fetch may not have run
                benv = None
                for ref in (br, f"origin/{br}", f"remotes/origin/{br}"):
                    raw = _sp.run(["git", "-C", root, "show", f"{ref}:{rel}"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
                    if raw.returncode == 0 and raw.stdout.strip():
                        try:
                            benv = json.loads(raw.stdout)
                            break
                        except ValueError:
                            continue
                if benv is not None:
                    name = next((k for k, v in (benv.get("tenants") or {}).items() if v == tid.group(1)), None)
                    if name:
                        env = benv  # the branch's file is the one the run will use
            except Exception:
                name = None
    if not name:
        return None, None
    suffix = env.get("alias_env") or env.get("env")
    # _tenant_webui prepends the test_environment dir itself — pass basename only
    return (
        gc._tenant_webui(f"{name}.{suffix}", os.path.basename(envf.group(1))),
        f"{name}.{suffix}",
    )


_VM_MAP_MD = os.path.join(
    # realpath, not abspath: this file is normally accessed through the
    # ~/grs_tools junction (SYNC.md convention) which maps ONE level (grs_tools
    # itself) into claude-resource, but does NOT make `dirname(dirname(...))`
    # land back in the real repo root -- abspath stays on the junction path
    # (C:\Users\<user>\), so the computed memory/ dir silently didn't exist and
    # every vm-allowlist check REFUSED with "unreadable" (found 2026-09-09
    # chasing an unrelated chapter-lock self-test). realpath resolves the
    # junction first, landing in the real claude-resource/ root.
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
    "memory",
    "reference_vm_ip_map.md",
)


def _vm_allowlist():
    """Every currently-active VM name from reference_vm_ip_map.md's table --
    single source, never a second hardcoded/duplicated list (ledger 41 family).
    A row wrapped in ~~strikethrough~~ or marked DECOMMISSIONED is excluded.
    Returns None if the file can't be read -- caller must treat that as REFUSE,
    never as "no list, so anything goes".
    """
    try:
        text = open(_VM_MAP_MD, encoding="utf-8").read()
    except OSError:
        return None
    names = set()
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) < 2:
            continue
        cell = cells[1].strip()
        m = re.search(r"`([^`]+)`", cell)
        if not m or cell.startswith("~~") or "DECOMMISSIONED" in line:
            continue
        names.add(m.group(1).upper())
    return names


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
        # The entry may exist only on the branch this build checks out (2026-11-04: git's EPAIS run uses
        # branch klin/epais-env-1136, whose env file adds tenant 1136 while main has no entry). Read the
        # same file from that branch instead of calling the tenant unresolvable -- local git, no webapi.
        br = merged.get("branch") or merged.get("GIT_BRANCH") or ""
        if br:
            import subprocess as _sp

            root = _sp.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
            for ref in (br, f"origin/{br}", f"remotes/origin/{br}"):
                raw = _sp.run(["git", "-C", root, "show", f"{ref}:{envf.group(1)}"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
                if raw.returncode == 0 and raw.stdout.strip():
                    try:
                        benv = json.loads(raw.stdout)
                    except ValueError:
                        continue
                    name = next((k for k, v in (benv.get("tenants") or {}).items() if v == tid.group(1)), None)
                    if name:
                        env = benv
                        break
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
    dc_refuse = _dc_source_guard(merged, base, job=job, case=(DC_CASE[0] or None))
    if dc_refuse:
        rows = [("dc-source", "REFUSE", dc_refuse)]
        return rows, ip, merged, overrides
    rows = []
    # 1. params (free, local)
    if not skip_params:
        rows.append(
            pf.ch_params_complete(merged, overrides, valid_names=_job_param_names(jp))
        )
    # 2. job-busy — a busy lane short-circuits BEFORE any VM contact
    d = _get(f"{jp}/lastBuild/api/json?tree=number,building")
    if d.get("building"):
        rows.append(("job-busy", "REFUSE", f"{jp} #{d.get('number')} still building"))
        return rows, ip, merged, overrides
    rows.append(("job-busy", "PASS", "no build running on this job"))
    # 2.5 VM allowlist — HARD gate (owner 2026-09-09, AUSTIN incident: triggered
    # on W11-25H2-PDV-3, a VM lifted wholesale from an unrelated PROD job's last
    # build params, never cross-checked against the known/approved VM set — see
    # `feedback_verify_vm_before_touch.md`). Cheap + local; runs before any
    # network contact with an unlisted box. Only `_allow_vm=1` (explicit,
    # same-turn user instruction — see cmd_trigger) clears this row; deliberately
    # NOT `_force_preflight`, so bypassing an unrelated REFUSE elsewhere (e.g.
    # env-json) can never also smuggle through an unauthorized VM.
    host = merged.get("HOSTNAME")
    if host:
        allow = _vm_allowlist()
        if allow is None:
            rows.append(
                ("vm-allowlist", "REFUSE", f"{_VM_MAP_MD} unreadable — cannot verify")
            )
            return rows, ip, merged, overrides
        if host.upper() not in allow:
            rows.append(
                (
                    "vm-allowlist",
                    "REFUSE",
                    f"HOSTNAME={host} is not in the known/approved VM set "
                    f"({sorted(allow)}) — only the user may authorize an unlisted "
                    "VM this turn (_allow_vm=1)",
                )
            )
            return rows, ip, merged, overrides
        rows.append(("vm-allowlist", "PASS", f"{host} is a known/approved VM"))
    # 3. env json (local file) — tenant/dc resolves or we say so; no webapi
    host_tenant = _pf_tenant_host(merged)
    # The env json's "tenants" map is tenant-id -> tenant NAME (see _pf_tenant_host and
    # helper_fixtures.py:79). It is NOT a list of valid dc names, so this row must not read as if the
    # dc had been checked: on 2026-11-04 I "corrected" a dc that was not a key in that map and told a
    # peer it would have failed this check. It would not have -- the dc never enters this function.
    dc_given = re.search(r"--dc=(\S+)", merged.get("tenant_info", "") or "")
    dc_note = (f"; dc={dc_given.group(1)} (dc is NOT validated by the env json -- it is a config group on "
               "the tenant, proved only by the run)") if dc_given else ""
    if host_tenant:
        rows.append(("env-json", "PASS", f"tenant resolves to {host_tenant}{dc_note}"))
    else:
        rows.append(
            ("env-json", "REFUSE", "tenant/dc not resolvable from the env json")
        )
    # 4. VM probe — only now, one shot, zero reconnect, known-bad short-circuit
    _mac_node = _mac_lane_node(jp)
    if _mac_node:
        rows.append(_mac_node_row(_mac_node))
        return rows, ip, merged, overrides
    if not ip:
        rows.append(
            (
                "vm-resolve",
                "REFUSE",
                "cannot resolve target VM ip from last build console",
            )
        )
        return rows, ip, merged, overrides
    # 2026-11-26: a known-bad entry is a RECORD of a past failure, not a live measurement. It used to short-circuit the
    # probe with no expiry, so at 06:45 a 02:04:19 verdict was served as "SSH is down" for a box that had come back --
    # I repeated it to the owner as current and sent git44 to fix a machine that only needed to finish booting. Now the
    # entry is honoured only inside KNOWN_BAD_TTL_S; outside it the live probe runs and a success clears the record.
    bad = pf.bad_find(ip)
    if bad:
        _bad_age = _ts_age_s(bad.get("ts"))
        if _bad_age is not None and _bad_age <= KNOWN_BAD_TTL_S:
            rows.append(
                (
                    "vm-known-bad",
                    "REFUSE",
                    f"{ip} failed a probe at {bad.get('ts')} (age {_fmt_age(_bad_age)}); not re-probed because the "
                    f"record is inside the {KNOWN_BAD_TTL_S // 60} min window: {bad.get('failed')} "
                    "— run `vm-check` to measure now, or `recover` to fix+clear",
                )
            )
            return rows, ip, merged, overrides
        rows.append(
            (
                "vm-known-bad",
                "WARN",
                f"{ip} carries a known-bad record from {bad.get('ts')} (age {_fmt_age(_bad_age)}), older than the "
                f"{KNOWN_BAD_TTL_S // 60} min window: probing now, and the record is cleared if the probe passes",
            )
        )
    import time as _t

    _t0 = _t.time()
    vm_rows = pf.ch_vm_all(ip, want_tenant_host=host_tenant, local_lane=_is_local_lane(jp))
    print(f"[preflight] vm probe (one round trip) took {_t.time()-_t0:.1f}s")
    rows.extend(vm_rows)
    # Informational only (never a REFUSE: an upgrade case legitimately starts from an older client): say when the client on
    # the VM is from another release than the run asks for, because the harness must uninstall it first and that step failed
    # with msiexec 1603 on a left-over 139 client (LOCAL1 build 109, 2026-10-28).
    _want = re.search(r"--current_release=(?:release|develop)-(\d+)", str(merged.get("release_info", "")))
    for _i, (_n, _s, _d) in enumerate(list(rows)):
        _v = re.search(r"installed (\d+)\.", _d) if _n == "vm-client-version" else None
        if _v and _want and _v.group(1) != _want.group(1):
            rows[_i] = (
                _n,
                _s,
                f"{_d} but the run asks for release {_want.group(1)}: WARN, the harness must uninstall it first; "
                "if that fails (msiexec 1603) run force_uninstall_nsclient/run_remote.py on the VM before the run",
            )
    if any(s == "REFUSE" for _n, s, _d in vm_rows):
        pf.bad_mark(ip, vm_rows, job=jp)
    elif bad and pf.bad_clear(ip):
        rows.append(("vm-known-bad", "INFO", f"{ip}: the stale known-bad record is cleared, the probe passes"))
    return rows, ip, merged, overrides


KNOWN_BAD_TTL_S = 600  # 10 min: a known-bad record older than this is re-measured, never quoted as current
_VM_CHECK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'vm_checks')
# git44, 2026-11-26: connect to these on every attempt next to SSH, so 'Windows up, sshd not yet' can be told apart
# from 'dead or off the network'. Ports 135/445/5985 are RPC/SMB/WinRM -- open means the Windows side is alive.
_VM_SIDE_PORTS = (135, 445, 5985)


def _ts_age_s(ts):
    """Seconds since a "%Y-%m-%d %H:%M:%S" stamp, or None when it cannot be parsed."""
    import datetime as _dt
    try:
        return (_dt.datetime.now() - _dt.datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S")).total_seconds()
    except Exception:
        return None


def _fmt_age(sec):
    if sec is None:
        return "unknown"
    sec = int(sec)
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def _port_open(ip, port, timeout=2.0):
    import socket
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def _vm_side_ports(ip):
    return {p: _port_open(ip, p) for p in _VM_SIDE_PORTS}


def _vm_check_path(ip):
    os.makedirs(_VM_CHECK_DIR, exist_ok=True)
    return os.path.join(_VM_CHECK_DIR, f"{ip}.json")


def _vm_check_load(ip):
    try:
        with open(_vm_check_path(ip), encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def _vm_probe(ip, body='hostname'):
    """One SSH probe through the WSL layer. -> (ok, text). No retries: unreachable IS the answer."""
    import grs_preflight as _pf
    try:
        out = (_pf.vm_ps(ip, body, max_attempts=1) or '').strip()
    except Exception as e:  # noqa: BLE001 - the error text is the evidence
        return False, f"{type(e).__name__}: {e}"
    if not out:
        return False, "probe returned nothing (connection refused, or the command produced no output)"
    return True, out


# Windows lanes and the box each one runs on (VM map, memory/reference_vm_ip_map.md). mac lanes have no SSH from the
# runner: their reachability is the Jenkins node, which vm-check reports and does not require an ip for.
_LANE_VM_IP = {
    'local2': '10.136.219.35',   # W11-26H1-AUSTIN-SYS-04
    'local1': '10.136.213.96',   # S2025-AUSTIN-SYS-03 (localtest)
    'reg': '10.136.125.181',     # S2025-AUSTIN-SYS-02
    'reg2': '10.136.217.108',    # W11-26H1-AUSTIN-SYS-07
}
_LANE_NODE = {
    'local2': 'systest-local2',
    'local1': 'systest-localtest',
    'reg': 'systest-reg',
    'reg2': 'systest-reg2',
    'mac2': 'systest-mac-tar1',
    'mac2-ssh': 'systest-mac-host1',
}


def _lane_vm_ip(lane):
    return _LANE_VM_IP.get(str(lane).strip().lower(), '')


def _node_line(node_name):
    """One line per server that has this node: online/offline + busy count."""
    found = _node_servers(node_name)
    if not found:
        return f'{node_name}: no such node'
    out = []
    for base, d in found:
        busy = sum(1 for e in d.get('executors', []) if e.get('currentExecutable'))
        out.append(f"{base.split('//')[-1].split(':')[0]} offline={d.get('offline')} busy={busy}")
    return f"{node_name}: " + "; ".join(out)


def _node_for_lane(lane):
    return _LANE_NODE.get(str(lane).strip().lower(), '')

def cmd_vm_check(*args):
    """vm-check <lane|ip> [--note TEXT]  -- the ONE reachability measurement a run may rely on.

    Probes SSH (hostname), the Windows-side ports 135/445/5985, and the client state; reads the node and the
    known-bad record WITH ITS AGE; writes logs/vm_checks/<ip>.json so a trigger can cite `# VM-CHECK: <ip>@<HH:MM>`.
    Exit 0 pass / 2 fail / 3 pending.

    Three rules, all paid for on 2026-11-26: a known-bad record older than KNOWN_BAD_TTL_S is never quoted as
    current; one failed probe is NOT a verdict (the second failure at least 2 min after the first is); and a refused
    22 with 135/445/5985 open means 'booting or sshd down', not 'dead'.
    """
    if not args:
        print(cmd_vm_check.__doc__)
        return 2
    target = args[0]
    note = ''
    if '--note' in args:
        i = args.index('--note')
        note = args[i + 1] if len(args) > i + 1 else ''
    ip = target if re.fullmatch(r'\d+\.\d+\.\d+\.\d+', target) else _lane_vm_ip(target)
    if not ip:
        print(f"vm-check: cannot resolve a VM ip for {target!r}")
        return 2
    lane = target if ip != target else ''
    import time as _t
    now = _t.strftime('%Y-%m-%d %H:%M:%S')
    prev = _vm_check_load(ip) or {}
    ports = _vm_side_ports(ip)
    ok, text = _vm_probe(ip)
    client = {}
    if ok:
        c_ok, c_text = _vm_probe(ip, "(Get-Process stAgentUI -ErrorAction SilentlyContinue | Measure-Object).Count")
        client['stAgentUI'] = c_text.strip() if c_ok else f'unreadable ({c_text})'
        c_ok2, c_text2 = _vm_probe(ip, "& 'C:/Program Files/Netskope/STAgent/nsdiag.exe' -f | Select-String 'Client version|Tunnel status'")
        if c_ok2:
            client['nsdiag'] = ' | '.join(x.strip() for x in c_text2.splitlines() if x.strip())[:160]
    windows_up = any(ports.values())
    if ok:
        shape = 'ssh up'
    elif windows_up:
        shape = 'WINDOWS UP, SSH DOWN (booting or sshd not running) -- not a dead box'
    else:
        shape = 'ALL PORTS CLOSED (dead, off the network, or still powering on)'
    bad = None
    try:
        import grs_preflight as _pf2
        bad = _pf2.bad_find(ip)
    except Exception:
        bad = None
    bad_age = _ts_age_s(bad.get('ts')) if bad else None
    bad_state = 'none'
    if bad:
        inside = bad_age is not None and bad_age <= KNOWN_BAD_TTL_S
        bad_state = f"{bad.get('ts')} age {_fmt_age(bad_age)}" + (' (inside the window)' if inside else ' (stale, re-measured now)')
    node = ''
    if lane:
        try:
            node = _node_line(_node_for_lane(lane))
        except Exception as e:  # noqa: BLE001
            node = f'unreadable ({e})'
    fails_in_a_row = 0 if ok else 1
    if not ok and prev.get('ip') == ip and prev.get('verdict') in ('pending', 'fail'):
        p_age = _ts_age_s(prev.get('ts'))
        if p_age is not None and p_age >= 120:
            fails_in_a_row = 2
    verdict = 'pass' if ok else ('fail' if fails_in_a_row >= 2 else 'pending')
    rec = {'ip': ip, 'lane': lane, 'ts': now, 'verdict': verdict, 'probe_ok': ok, 'probe_text': text,
           'ports': ports, 'shape': shape, 'client': client, 'node': node, 'known_bad': bad_state,
           'note': note, 'prev_ts': prev.get('ts', ''), 'fails_in_a_row': fails_in_a_row}
    with open(_vm_check_path(ip), 'w', encoding='utf-8') as fh:
        json.dump(rec, fh, indent=1)
    port_txt = ' '.join(f"{p}={'open' if v else 'closed'}" for p, v in ports.items())
    print(f"VM-CHECK {ip}{' (' + lane + ')' if lane else ''}  {now}  {verdict.upper()}")
    print(f"  ssh(22): {'ok' if ok else 'FAILED'} -- {text[:140]}")
    print(f"  windows: {port_txt}  -> {shape}")
    if client:
        print(f"  client: stAgentUI count={client.get('stAgentUI')}" + (f" | {client['nsdiag']}" if client.get('nsdiag') else ''))
    print(f"  node: {node or '-'}")
    print(f"  known-bad: {bad_state}")
    if verdict == 'pending':
        print('  NEXT: one failed probe is not a verdict. Re-run vm-check after 2 minutes; the second failure is what')
        print('        pauses the lane and escalates it instead of triggering.')
    elif verdict == 'fail':
        print('  NEXT: PAUSED. Do not trigger, do not recover, do not _force_preflight. Send git44 the three facts')
        print('        (probe + ports + timestamps, node state, last known client state); git only when git44 has no')
        print('        live waiter (coord.py waiter-alive --agent git44), and copy git44 on that message.')
    else:
        print(f"  NEXT: cite this in the trigger as  # VM-CHECK: {ip}@" + now[11:16])
    return 0 if verdict == 'pass' else (2 if verdict == 'fail' else 3)

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


_VMLOCK_IP = "10.136.125.181"  # SYS-02, shared by REG + REG-02 (jenkins_vm.lock)


def _vmlock_ssh(cmd):
    """Run one command on SYS-02 (WSL layer). paramiko directly: importing
    grs_ssh rewraps sys.stdout and closes ours."""
    import paramiko

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        _VMLOCK_IP,
        username=os.environ.get("GRS_SSH_USER", "nsadmin"),
        password=os.environ.get("GRS_SSH_PASS") or os.environ.get("GRS_SSH_USER", "nsadmin"),
        timeout=15,
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        _i, o, _e = c.exec_command(cmd, timeout=30)
        return o.read().decode("utf-8", errors="replace")
    finally:
        c.close()


def _vmlock_read():
    """-> (info dict or None, error str). Reads /mnt/c/jenkins_vm.lock/info.txt."""
    try:
        out = _vmlock_ssh(
            "cat /mnt/c/jenkins_vm.lock/info.txt 2>/dev/null || echo __NOLOCK__"
        )
    except Exception as e:
        return None, f"cannot reach {_VMLOCK_IP}: {e}"
    if "__NOLOCK__" in out:
        return None, ""
    info = dict(
        ln.split("=", 1) for ln in out.splitlines() if "=" in ln and not ln.startswith("[")
    )
    return info, ""


def cmd_vmlock(action="status", *rest):
    """vm-lock status|release [vm-ip] -- the per-VM jenkins_vm.lock (default SYS-02; pass
    the REG-02 / other REG VM ip to inspect or release that VM's lock).

    status : who holds jenkins_vm.lock and whether that build is still running.
    release: remove the lock ONLY when its holder build has finished (the
             controller's own release step can fail, e.g. `sshpass` missing, and
             leave a stale lock that makes every later REG/REG-02 build wait or
             time out). Refuses if the holder is still building."""
    global _VMLOCK_IP
    if rest:
        _VMLOCK_IP = rest[0]
    info, err = _vmlock_read()
    if err:
        print(f"REFUSE: {err}")
        return 1
    if not info:
        print(f"vm-lock: free (no jenkins_vm.lock on {_VMLOCK_IP})")
        return 0
    jobp = info.get("JOB", "")
    build = info.get("BUILD", "")
    alias = next((a for a, p in ALIASES.items() if p.replace("/job/", "/") == jobp), None)
    if not alias and jobp:  # alias table can lag the real job name (REG2 vs REG-02)
        alias = jobp.replace("/", "/job/")
    holder = "unknown"
    if alias and build.isdigit():
        d = _get(f"{_job(alias)}/{build}/api/json?tree=building,result")
        holder = "RUNNING" if d.get("building") else f"finished ({d.get('result')})"
    print(f"vm-lock: held by {jobp} #{build} -> {holder}  ({info.get('URL', '')})")
    if action != "release":
        return 0
    if holder != "finished" and not holder.startswith("finished"):
        print(f"REFUSE: holder is {holder}; only a finished build's lock is stale")
        return 1
    _vmlock_ssh("rm -rf /mnt/c/jenkins_vm.lock")
    out = _vmlock_ssh("ls -d /mnt/c/jenkins_vm.lock 2>&1")
    print("vm-lock: released" if "No such file" in out else f"vm-lock: NOT released: {out}")
    return 0 if "No such file" in out else 1


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


# Owner hard rule, 2026-10-14 (memory/feedback_systest_approved_tenants.md):
# new SystemTest triggers only use these tenants -- never 1118/1334. Re-check
# that memory file if this list might be stale; don't just trust this const.
# 1457 dropped for NEW triggers 2026-10-15 (owner: "focus on 1331 and 1340 ...
# 1457 enough"); builds already running on 1457 are left alone.
_APPROVED_TENANTS = {"1340", "1331", "115615", "1136", "1457"}  # 1457 (fed, nscauto7) added 2026-11-11 on the owner's sentence: release-144 must also be tested on 1457, 30-50% of the runs  # 115615 (nscsystest1) added by the owner 2026-10-28: "appove the two"; 1136 (epdlp-tenant00, QA) added 2026-11-09 on the owner's sentence naming it for the EPAIS 10603 runs on both OS: "we need policy deploy and get result in win mac of 10603 , from tenant 1340 and 1136" (relayed verbatim by git, message 7ea2b0ea)


def cmd_approved_tenants():
    """Print the tenants a NEW SystemTest trigger may use. This constant is the source of truth: a memory index line or
    a doc that lists a different set is stale (2026-10-24: an index line still listed 1457 and I repeated it)."""
    print("approved tenants for new triggers: " + " ".join(sorted(_APPROVED_TENANTS)) + "  (source: grs_jenkins.py _APPROVED_TENANTS)")


def _lane_platform(lane_or_job):
    """'mac' for the MAC2 lanes/jobs, else 'win'."""
    return "mac" if "mac" in (lane_or_job or "").lower() else "win"


def _dc_platform(m, tenant, dc):
    """Platform of a dc: the registry entry in tenant_map.json when there is one, else the naming contract (a `mac` suffix)."""
    reg = (m.get("dcs", {}).get(f"{tenant}/{dc}") or {}).get("platform")
    return reg or ("mac" if str(dc).endswith("mac") else "win")



READONLY_PARALLEL_AFTER_MIN = 3      # owner 2026-11-11: a read-only case may overlap the previous run, but only 3 min after that run's install is done
INSTALL_FALLBACK_MIN = 10            # when no install marker is readable in the running console, a 10-minute-old build counts as installed
_INSTALL_MARKERS = re.compile("verify_installation.*All checks passed|Starting fresh installation", re.I)
_CONSOLE_STAMP = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def _install_done_minutes(job, build):
    """Minutes since the running build's install finished, or None when no marker is readable.

    The owner allowed read-only cases to overlap a running one only after that run's installation is
    done for 3 minutes (2026-11-11). Jenkins exposes no install event, so the console is the evidence:
    the last install marker line's own timestamp is the anchor. A build with no readable marker falls
    back to its elapsed time (INSTALL_FALLBACK_MIN)."""
    try:
        lines = _console_text(job, build)
    except Exception:  # noqa: BLE001 - an unreadable console must not grant the overlap
        return None
    anchor = None
    for ln in lines:
        if _INSTALL_MARKERS.search(ln):
            m = _CONSOLE_STAMP.search(ln)
            if m:
                anchor = m.group(1)
    if not anchor:
        return None
    try:
        when = datetime.datetime.strptime(anchor, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (datetime.datetime.now() - when).total_seconds() / 60.0


def _dc_platform_error(m, tenant, dc, lane_or_job, test_cases=""):
    """'' when the dc fits the lane and the test filter, else the reason.

    Owner 2026-10-27: a new tenant arrived with a mac-only dc while the work (CERT-04 / CERT-03) is Windows-only, and
    nothing refused it, so a mac probe was built on it. The check lives in the tool so it cannot be skipped."""
    dcp, lanep = _dc_platform(m, tenant, dc), _lane_platform(lane_or_job)
    if dcp in ("both", "any"):  # a dc the owner registered for both platforms fits either lane
        dcp = lanep
    if dcp != lanep:
        return (
            f"dc {dc} on tenant {tenant} is a {dcp} dc but {lane_or_job} is a {lanep} lane. "
            "A Windows case needs a Windows dc, a mac case a `mac` dc; tell the owner the dc does not fit instead of building on it."
        )
    win_only = re.search(r"test_\w+?_win\b", test_cases or "")
    if win_only and lanep == "mac":
        return f"{win_only.group(0)} is a Windows-only case but {lane_or_job} is a mac lane."
    return ""


_TASKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tasks")
_OWNER_APPROVED_RULE_FROM = "2026-10-27"


def _task_case_error(task_id, case_id):
    """'' when the kanban task `task_id` exists, is open, and covers `case_id`; else the reason.

    Owner 2026-10-27: a mac stress case was picked as a "probe" for a tenant that was created for the Windows CERT-04
    task. Nothing tied the run to the task's goal. Now every SystemTest trigger names the task it serves, and the case
    must be one the task's title, next step or study/poc/design documents name."""
    if not task_id:
        return "every SystemTest trigger names the kanban task it serves: pass task=<id> (for example task=git-0001)"
    try:
        text = open(os.path.join(_TASKS_DIR, f"{task_id}.md"), encoding="utf-8").read()
    except OSError:
        return f"task {task_id} does not exist in claude-resource/tasks"

    def field(k):
        m = re.search(rf"^{k}:(.*)$", text, re.M)
        return m.group(1).strip() if m else ""

    if field("stage") == "closure":
        return f"task {task_id} is closed; a trigger must serve an open task"
    # Owner 2026-11-11 ("ok do both"): a case named anywhere in the task counts -- title, next step, or the
    # event lines a note writes -- so a multi-run task does not have to be re-edited before every single run.
    blob = field("title") + " " + field("next") + " " + text[:20000]
    root = os.path.dirname(os.path.abspath(_TASKS_DIR))
    for k in ("study_doc", "poc_doc", "design_doc"):
        p = field(k)
        if p:
            try:
                blob += " " + open(p if os.path.isabs(p) else os.path.join(root, p), encoding="utf-8").read()[:200000]
            except OSError:
                pass
    if not case_id:
        return "cannot tell which case this trigger runs; use case=<ID> so it can be matched to the task"
    if not re.search(rf"(?<![A-Za-z0-9]){re.escape(case_id)}(?![0-9])", blob, re.I):
        return (
            f"task {task_id} ({field('title')[:70]}) does not name case {case_id}. A trigger runs only the cases of the "
            "task it serves; if this case belongs to the task, name it in the task's study/poc/design document first."
        )
    return ""


def _task_stage(task_id):
    """Stage of a kanban task, '' when unreadable."""
    try:
        text = open(os.path.join(_TASKS_DIR, f"{task_id}.md"), encoding="utf-8").read()
    except OSError:
        return ""
    m = re.search(r"^stage:(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def _case_of_filter(m, test_cases):
    """Case id whose test_filter appears in a hand-assembled -k filter, else ''."""
    for cid, c in m.get("cases", {}).items():
        tf = c.get("test_filter", "")
        if tf and tf in (test_cases or ""):
            return cid
    return ""


def _owner_approved_ok(text):
    """(ok, why) for a tenant_map combo's owner_approved. Entries dated before the rule are legacy and accepted; newer
    ones must quote, in single quotes, words the owner typed in THIS Claude session (a pi session cannot verify them)."""
    d = re.search(r"\d{4}-\d{2}-\d{2}", text or "")
    if d and d.group(0) < _OWNER_APPROVED_RULE_FROM:
        return True, "legacy"
    quotes = re.findall(r"'([^']{12,})'", text or "")
    if not quotes:
        return False, "owner_approved must quote the owner's own words in single quotes"
    try:
        import task as _task
    except Exception as e:  # noqa: BLE001
        return False, f"cannot import task.py to verify the words ({e})"
    for q in quotes:
        said = _task._owner_said(" ".join(q.split()))
        if said:
            return True, "verified"
        if said is None:
            return False, "no readable Claude transcript here, so the owner's words cannot be verified"
    return False, "the quoted words are not in any message the owner typed in this session"


def _evidence_test_name(flt, lane):
    """The full test function name a lane runs. The map filter names a case without its platform suffix, but every
    SystemTest entry point ends in `_win` or `_mac`, and the DB keeps history under the full name (2026-10-28: a mac
    case looked up by the bare name found only the Windows passes and reported 'no DB history')."""
    m = re.search(r"test_\w+", flt or "")
    if not m:
        return flt
    name = m.group(0)
    return name if name.endswith(("_win", "_mac")) else name + ("_mac" if _lane_platform(lane) == "mac" else "_win")


def _db_history(test):
    """query-history JSON for one test name; None on any failure (the evidence is advisory, never fatal)."""
    try:
        db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_test_db", "db.py")
        out = subprocess.run(
            [sys.executable, db, "query-history", test, "--verdict", "any", "--top", "30", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        return json.loads(out.stdout)
    except Exception:
        return None


def _lane_key(lane):
    k = (lane or "").lower().replace("-", "")
    return {"reg2": "reg02"}.get(k, k)


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))


def _rank_by_evidence(flt, lane, combos):
    """Order the approved combos by their newest PASS on this lane (owner + git22 2026-10-28), newest date first; on the
    same day the higher client version wins (a tie-breaker, never the main key). Combos with no PASS on the lane keep their
    map order after the ranked ones. Any DB failure leaves the map order untouched."""
    if len(combos) < 2:
        return combos
    h = _db_history(_evidence_test_name(flt, lane))
    if not h:
        return combos
    want, best = _lane_key(lane), {}
    for ln in h.get("lanes", []):
        if _lane_key(ln.get("lane")) != want:
            continue
        for e in ln.get("entries", []):
            if e.get("verdict") != "PASS":
                continue
            k = (str(e.get("tenant")), e.get("dc"))
            cur = (str(e.get("started_at", ""))[:10], _ver_tuple(e.get("product_version")))
            if k not in best or cur > best[k]:
                best[k] = cur
    ranked = sorted(combos, key=lambda c: best.get((c["tenant"], c["dc"]), ("", ())), reverse=True)
    if [id(c) for c in ranked] != [id(c) for c in combos]:
        first = ranked[0]
        print(
            f"[rank] {first['tenant']}/{first['dc']} first: newest PASS on {lane} "
            f"{best.get((first['tenant'], first['dc']), ('?',))[0]} (map order overridden by evidence)",
            file=sys.stderr,
        )
    return ranked


def _combo_pass_verdict(flt, tenant, dc, lane):
    """Human verdict for one declared combo, from the results DB.

    'PASS <build> (<date>, client <ver>)' when this exact (tenant, dc) has a PASS on
    this lane; 'NO PASS RECORD on <lane>' when it has none; 'DB unreachable' when the
    history cannot be read (never fatal). Added 2026-10-29 so a trigger decision shows
    which pairs are proven instead of relying on the map's free-text note."""
    try:
        h = _db_history(_evidence_test_name(flt, lane))
    except Exception:
        return "no DB history readable (harvest gap) - map note is the only claim"
    if not h:
        # db.py exits 2 with "matches nothing among known test_result.test_name values"
        # when the results DB never ingested this test (harvest gap), which is NOT the
        # same as "the pair failed". Say which one it is.
        return "no DB history for this test (harvest gap) - map note is the only claim"
    want = _lane_key(lane)
    best = None
    for ln in h.get("lanes", []):
        if _lane_key(ln.get("lane")) != want:
            continue
        for e in ln.get("entries", []):
            if e.get("verdict") != "PASS":
                continue
            if str(e.get("tenant")) != str(tenant) or e.get("dc") != dc:
                continue
            cur = (str(e.get("started_at", "")), _ver_tuple(e.get("product_version")))
            if best is None or cur > best[0]:
                best = (cur, e)
    if best is None:
        return f"NO PASS RECORD on {lane} (treat as unverified)"
    e = best[1]
    return f"PASS build {e.get('build')} ({str(e.get('started_at', ''))[:10]}, client {e.get('product_version')})"


def _combo_gate(case, lane, tenant_info):
    """REFUSE text when a hand-written tenant_info is not a DECLARED combo for (case, lane).

    Why (git22, 2026-10-29): I triggered STRESS-06 with --dc=systeststatic1340 copied
    from a neighbour build's params, while the case's 1340 dc is push1340 -- a
    config-push case touching the wrong tenant config. Be strict: whenever case= AND
    tenant_info are both given, the (lane, tenant, dc) triple must exist as a combo in
    tenant_map, else the trigger is refused with the declared pairs.

    Returns "" when the triple is declared (or cannot be checked), else the REFUSE text.
    """
    try:
        m = json.load(open(_TENANT_MAP, encoding="utf-8"))
    except Exception:
        return ""  # unreadable map: never block on our own parse failure
    entry = (m.get("cases") or {}).get(str(case).upper())
    if not entry:
        return ""
    # a combo marked "disabled" is not a declared combo: dc-pick already honours the flag, the gate must too
    # (git22-0033, 2026-10-29: STEER-06 ran on the disabled 1340/pushdse1340 because this gate ignored it)
    lane_combos = [c for c in entry.get("combos", []) if lane in c.get("lanes", []) and not c.get("disabled")]
    if not lane_combos:
        return (
            f"case {case} has no combo for lane {lane!r} (declared lanes: "
            f"{[c.get('lanes') for c in entry.get('combos', [])]}). Refusing: the lane cannot run this case."
        )
    tt = re.search(r"--tenant\s+(\S+)", tenant_info or "")
    dd = re.search(r"--dc=(\S+)", tenant_info or "")
    if not tt or not dd:
        return ""
    tenant, dc = tt.group(1), dd.group(1)
    same_tenant = [c for c in lane_combos if str(c.get("tenant")) == tenant]
    if not same_tenant:
        return (
            f"case {case} on lane {lane} has no combo for tenant {tenant} "
            f"(declared tenant/dc pairs: {[(c.get('tenant'), c.get('dc')) for c in lane_combos]}). "
            f"Refusing: pick a declared tenant, or add the combo to tenant_map first."
        )
    dcs = sorted({c.get("dc") for c in same_tenant})
    if dc not in dcs:
        return (
            f"case {case} on lane {lane} tenant {tenant}: dc must be one of {dcs}, got {dc}. "
            f"Use the tenant_info that `dc-pick {case} {lane} --tenant {tenant}` prints."
        )
    return ""



# Owner 2026-11-05: "remember dc-pick and this api can help you find good dc, to use."
# The report server renders one table row per lane with the tenant and the DC of the last PASS/FAIL runs,
# which is the only machine-readable source of "which dc actually passed this test". Used by
# `pass-report` and by dc-pick's evidence.
PASS_REPORT = "http://10.136.218.74:8080/query"


def pass_report_rows(test_name, verdict="pass", timeout=30):
    """[{lane, release, verdict, run, iters, tenant, dc, bitness, when, duration}] from the report server."""
    import html as _html
    import urllib.parse as _up
    import urllib.request as _ur

    url = f"{PASS_REPORT}?" + _up.urlencode({"test": test_name, **({"verdict": verdict} if verdict else {})})
    with _ur.urlopen(url, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
        cells = [_html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if len(cells) < 10 or cells[0] == "Lane":
            continue
        out.append({"lane": cells[0], "release": cells[1], "verdict": cells[2], "run": cells[3], "iters": cells[4],
                    "tenant": cells[5], "dc": cells[6], "bitness": cells[7], "when": cells[8], "duration": cells[9]})
    return out


def cmd_pass_report(test_name, *kv):
    """pass-report TEST [verdict=pass|fail] -- per-lane latest runs with tenant+dc, from the report server."""
    d = dict(item.partition("=")[::2] for item in kv)
    verdict = d.get("verdict", "pass")
    rows = pass_report_rows(test_name, verdict)
    if not rows:
        print(f"no rows for {test_name} (verdict={verdict})")
        return 1
    print(f"{test_name}  verdict={verdict}  ({len(rows)} lane rows)")
    for r in rows:
        if r["verdict"] in ("-", ""):
            print(f"  {r['lane']:9} no {verdict.upper()} record")
        else:
            print(f"  {r['lane']:9} {r['verdict']:5} run {r['run']:>6} iters {r['iters']:>5} "
                  f"tenant {r['tenant']:>6} dc {r['dc']:22} {r['when']}  {r['duration']}")
    return 0

def _unknown_test_tokens(test_cases, test_suites="", branch=""):
    """test_* names in a -k filter that match NO `def test_*` of the SystemTest suite in the current GRS clone.

    2026-10-29 (git22-0033): STRESS-11 mac was triggered twice with `-k test_stress_11_handle_limit_enforcement`, a name
    that exists nowhere; both builds died at pytest configure, and config-guard printed no line at all because no case
    matched the name. [] when it cannot tell (not a SystemTest filter, a non-main branch whose tests this clone may not
    have, no repo found).
    """
    toks = re.findall(r"\btest_[A-Za-z0-9_]+", test_cases or "")
    if not toks or (test_suites and "SystemTest" not in test_suites) or (branch and branch != "main"):
        return []
    try:
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=20).stdout.strip()
        base = os.path.join(root, "golden_regression", "tests", "SystemTest")
        names = set()
        for dp, _dirs, files in os.walk(base):
            for f in files:
                if f.endswith(".py"):
                    with open(os.path.join(dp, f), encoding="utf-8", errors="replace") as fh:
                        names.update(re.findall(r"def (test_[A-Za-z0-9_]+)", fh.read()))
    except Exception:
        return []
    if not names:
        return []
    return [t for t in toks if not any(t in n for n in names)]


def _wait_hint(lane, build, started_ms):
    """How to be told when the blocking build ends, and how old it is (a build 'running' for hours may be queued for an
    offline agent: MAC2 270 sat 114 min 'building' without starting any step)."""
    age = ""
    try:
        age = f" (started {int((time.time() * 1000 - float(started_ms)) / 60000)} min ago; if it is old, check the console: it may be queued for an offline agent)"
    except (TypeError, ValueError):
        pass
    return f"{lane}#{build}{age}. To be told when it ends: python C:/git/claude-resource/grs_tools/coord.py wait-build {lane} {build}"


_FED_REQUEST = [""]


def _dc_pick(case, lane, tenant=""):
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
    # 1457 (fed) is owner-only: a fed combo is never a candidate unless the caller brings the owner's fed request with
    # `--fed-request "<his words>"`. Owner 2026-11-17, verbatim: "for 1457 fed, only owner approve , dc-pick can pick only
    # with fed request via owner". Everything else (stg / qa tenants) stays pickable.
    if str(tenant) != "1457" and not _FED_REQUEST[0]:
        combos = [c for c in combos if str(c.get("tenant")) != "1457"]
        if not combos:
            if tenant:
                return (
                    None,
                    f"case {case} on lane {lane!r} has no combo for tenant {tenant} — the only combo(s) there are fed (1457), "
                    "and fed needs the owner's request; pick another lane or ask him for the fed run",
                )
            return (
                None,
                f"case {case} on lane {lane!r}: only 1457 (fed) combos exist and fed needs the owner's request — pass "
                '`--fed-request "<his words>"` if he asked for a fed run, otherwise there is no pick',
            )
    if not combos:
        return (
            None,
            f"case {case} has no combo for lane {lane!r} (map says: {[c.get('lanes') for c in entry['combos']]}). "
            f"Fix: add a combo that lists {lane!r} under cases.{case.upper()}.combos in tenant_map.json (a minimal text edit, "
            f'for example {{"tenant": "1340", "dc": "<dc>", "lanes": ["{lane}"], "family": "<family>", "note": "unverified"}}), '
            f"then run dc-pick again. For a tenant and dc with real runs use: dc-pick {case.upper()} {lane} --history",
        )
    for c in combos:
        # A case may have a Windows and a mac case name (EPAIS-04: ..._win on Windows, ..._closed_mac on the mac
        # module). The combo's own test_filter wins over the case-level one, so the platform check reads the
        # name that combo actually runs (2026-11-10: dc-pick refused a mac combo because the case-level name
        # ended in _win).
        err = _dc_platform_error(m, c["tenant"], c["dc"], lane, c.get("test_filter") or entry.get("test_filter", ""))
        if err:
            return None, f"case {case}: map error, {err}"
    # Owner hard rule 2026-10-14: never trigger on a disallowed tenant, even
    # if it's the map's only/first-listed combo. Prefer an approved-tenant
    # combo over combos[0] when one exists; if NONE of this case's combos are
    # on an approved tenant, REFUSE rather than silently using the old one or
    # guessing a new tenant that was never actually verified for this case.
    # A combo may carry "owner_approved": "<owner's words/date>" in tenant_map.json
    # to allow ONE specific tenant/dc outside the standing list (owner 2026-10-17:
    # OVLP-02 on 1457/systest1457cloud) without re-opening the tenant for every case.
    oa_rejected = []

    def _allowed(c):
        if c.get("disabled"):
            return False
        if c["tenant"] in _APPROVED_TENANTS:
            return True
        if c.get("owner_approved"):
            ok, why = _owner_approved_ok(c["owner_approved"])
            if not ok:
                oa_rejected.append(f"{c['tenant']}/{c['dc']}: {why}")
            return ok
        return False

    approved_combos = [c for c in combos if _allowed(c)]

    # Owner rule 2026-11-01 (via git, verbatim: "make sure dc-pick and skill witll choose 1331 ,not 1340, htis one is with
    # failure."): a combo may carry "explicit_only": true, meaning the automatic pick (map order or evidence ranking) must
    # never choose it -- it stays reachable only when the caller names its tenant explicitly (dc-pick --tenant, or an explicit
    # tenant_info on the trigger), which is how the failing STRESS-08 combo 1340/push1340 stays available for investigation.
    _explicit_tenant = str(tenant or "")
    _auto = [
        c
        for c in approved_combos
        if not c.get("explicit_only") or (_explicit_tenant and str(c.get("tenant")) == _explicit_tenant)
    ]
    if _auto and len(_auto) != len(approved_combos):
        _held = [f"{c['tenant']}/{c['dc']}" for c in approved_combos if c not in _auto]
        print(
            f"[pick] held back (explicit_only, pass --tenant <T> to use): {', '.join(_held)}",
            file=sys.stderr,
        )
        approved_combos = _auto
    if not approved_combos:
        disallowed = sorted({c["tenant"] for c in combos})
        return None, (
            f"case {case} has no combo on an approved tenant ({sorted(_APPROVED_TENANTS)}) "
            f"— map only lists tenant(s) {disallowed}, which are disallowed "
            "(memory/feedback_systest_approved_tenants.md). STOP and ask the owner which "
            "approved tenant/dc to use for this case; do not substitute one unverified."
            + (f" owner_approved rejected: {'; '.join(oa_rejected)}." if oa_rejected else "")
        )
    if tenant:
        # --tenant must MEAN something (git22, 2026-10-29): the chapter fixes the
        # tenant, so resolve inside it and rank by that tenant's own PASS evidence.
        # Before this, `dc-pick STEER-01 reg2 --tenant 1340` printed the 1331 pick
        # and only the two --dc values in the map kept me honest.
        only = [c for c in approved_combos if str(c.get("tenant")) == str(tenant)]
        if not only:
            return (
                None,
                f"case {case} on lane {lane} has no approved combo for tenant {tenant} "
                f"(declared: {[(c['tenant'], c['dc']) for c in approved_combos]}). "
                f"Refusing: add the combo to tenant_map first, or pick a declared tenant.",
            )
        approved_combos = only
    _ranked_for_note = _rank_by_evidence(entry.get("test_filter", ""), lane, approved_combos)
    combos = _ranked_for_note
    # family conflict: any running SystemTest build on the same tenant+family?
    # Walk the approved combos in map order and take the first one with no
    # same-tenant+same-family conflict (2026-10-15: only combos[0] was checked,
    # so a free second approved tenant was never reached).
    running = []  # (job, tenant, dc)
    try:
        # mac lanes included (2026-10-15): a mac dc "<name>mac" shares the SAME
        # steering config as the Windows dc "<name>" on the same tenant (cloud1340 /
        # cloud1340mac both bind 'systestcloud'); mac STRESS runs flipped its traffic
        # mode under a Windows STEER-01 precondition (REG 170/173 incident).
        for j in ("reg", "reg2", "local1", "local2", "mac2", "mac2-ssh"):
            d = _get(
                f"{_job(j)}/lastBuild/api/json?tree=number,building,timestamp,actions[parameters[name,value]]"
            )
            if not d.get("building"):
                continue
            # every action, not actions[0]: a build started from the Jenkins UI lists its CauseAction first, so
            # reading only the first one saw no tenant/dc and the conflict check passed (LOCAL2 build 502 vs
            # STRESS-13 on the same systeststatic-win config, 2026-10-20)
            ti = " ".join(
                str(p.get("value", ""))
                for a in (d.get("actions") or [])
                if isinstance(a, dict)
                for p in (a.get("parameters") or [])
                if isinstance(p, dict)
            )
            m_t = re.search(r"--tenant\s+(\S+)", ti)
            m_d = re.search(r"--dc=(\S+)", ti)
            if m_t and m_d:
                running.append((j, m_t.group(1), m_d.group(1), d.get("number"), d.get("timestamp")))
    except Exception:
        running = []  # conflict check best-effort; the map choice stands

    def _conflict(cb):
        for j, rt, rd, bn, bts in running:
            if rt != cb["tenant"]:
                continue
            # Fact first: tenant_map.json "dcs" records the config each dc binds
            # (owner-confirmed). Two builds conflict iff they bind the SAME config;
            # different configs never conflict, whatever the dc names look like.
            # A dc with no tenant_map entry still IS a config group: two runs on the same
            # tenant+dc share it, so the pair itself is the key.
            cand_cfg = (m.get("dcs", {}).get(f"{cb['tenant']}/{cb['dc']}") or {}).get("config") \
                or f"dc:{cb['tenant']}/{cb['dc']}"
            run_cfg = (m.get("dcs", {}).get(f"{rt}/{rd}") or {}).get("config") \
                or f"dc:{rt}/{rd}"
            if cand_cfg and run_cfg:
                if cand_cfg == run_cfg:
                    # Owner 2026-11-11: a READ-ONLY case (tenant_map "pushes": false) may overlap a run
                    # on the same config, but only once that run's install has been done for 3 minutes.
                    # A writing case is never allowed to overlap: the config is the thing being changed.
                    if not entry.get("pushes"):
                        done = _install_done_minutes(j, bn)
                        if done is not None and done >= READONLY_PARALLEL_AFTER_MIN:
                            print(
                                f"[config] read-only case: overlapping {j} build {bn} on config {run_cfg!r} "
                                f"is allowed (its install finished {done:.1f} min ago, "
                                f"threshold {READONLY_PARALLEL_AFTER_MIN} min)",
                                file=sys.stderr,
                            )
                            continue
                        waited = (f"its install finished {done:.1f} min ago" if done is not None
                                  else "no install marker in its console yet")
                        return (
                            f"config conflict (read-only case, overlap not yet allowed): {j} build {bn} is "
                            f"running tenant {rt} dc {rd} on the same config {run_cfg!r} — {waited}, "
                            f"the owner allows the overlap only {READONLY_PARALLEL_AFTER_MIN} min after that "
                            f"install. {_wait_hint(j, bn, bts)}"
                        )
                    return (
                        f"config conflict: {j} build {bn} is running tenant {rt} dc {rd} "
                        f"bound to the same config {run_cfg!r} as {cb['dc']} — wait or pick another dc. {_wait_hint(j, bn, bts)}"
                    )
                continue
            # Fallback when either dc has no recorded config: the old name heuristic
            # (a mac dc "<name>mac" is assumed to share the Windows dc's config).
            _rd = rd[:-3] if rd.endswith("mac") else rd
            dc_fam = next(
                (f for f, dcs in m["families"].items() if rd in dcs or _rd in dcs), None
            )
            if dc_fam == cb["family"]:
                return (
                    f"family conflict: {j} build {bn} is running tenant {rt} dc {rd} "
                    f"(same {cb['family']} family) — wait or pick another family. {_wait_hint(j, bn, bts)}"
                )
        return None

    c, first_err = None, None
    for cb in combos:
        err = _conflict(cb)
        if not err and _ranked_for_note and _ranked_for_note[0] is not cb:
            print(
                f"[pick] {cb['tenant']}/{cb['dc']} chosen; the evidence-ranked combo "
                f"{_ranked_for_note[0]['tenant']}/{_ranked_for_note[0]['dc']} was passed over",
                file=sys.stderr,
            )
        if err is None:
            c = cb
            break
        first_err = first_err or err
    if c is None:
        return None, first_err
    bitness = c.get("bitness", "64")  # default 64-bit; 32 only when the map says so
    # -k is a substring match: "test_x" also selects its "test_x_mac" sibling, which then
    # skips on Windows and breaks "one case per run" (REG2 230, LOCAL1 62). Exclude mac
    # unless the filter itself names a mac case.
    _flt = c.get("test_filter") or entry["test_filter"]  # a combo may name its own case (the mac sibling)
    # Every SystemTest entry point now carries its platform in the name (`*_win` / `*_mac`, owner 2026-10-23), so
    # the platform filter is positive: `<id> and win` on a Windows lane, `<id> and mac` on a mac lane. The old
    # unconditional `and not mac` picked the opposite test on mac2 (STRESS-03 builds 250-252, 2026-10-23).
    if "mac" not in _flt and "win" not in _flt:
        _flt = f"{_flt} and {'mac' if lane.startswith('mac') else 'win'}"
    params = {
        "test_cases": f'-k "{_flt}"',
        "tenant_info": (
            f"--custom-env-file=golden_regression/test_environment/{c.get('env', 'boomskope_nonprod_stg.json')} "
            f"--tenant {c['tenant']} --dc={c['dc']} --is_64_bit={'true' if bitness == '64' else 'false'}"
        ),
        "timeout": str(entry.get("default_timeout_h", 2)),
    }
    if entry.get("fixed_iterations"):
        params["iterations"] = str(entry["fixed_iterations"])
    return params, None


_SYSTEMTEST_SUITE = "golden_regression/tests/SystemTest"


def _adapt_picked_for_job(picked, valid_for_job):
    """Fit the dc-pick params to what THIS job declares (pure, no network).

    tenant_map gives the timeout in hours as `timeout` and no test_suites. Two jobs differ: MAC2 / MAC2-SSH have
    no `timeout`, only `timeout_min` (minutes), and a `test_suites` whose default is features/collect_log. The old
    trigger silently dropped the unknown `timeout` (build 251 died at the 60 minute job default after its test had
    passed) and left test_suites at the default (build 250 selected no test at all, 2026-10-23).
    `valid_for_job` is the declared name set, or None when unknown: then nothing is changed."""
    picked = dict(picked)
    if valid_for_job is None:
        return picked
    if "timeout" in picked and "timeout" not in valid_for_job and "timeout_min" in valid_for_job:
        picked["timeout_min"] = str(int(float(picked.pop("timeout")) * 60))
    if "test_suites" in valid_for_job:
        picked.setdefault("test_suites", _SYSTEMTEST_SUITE)
    return picked


def _dc_history(test, lane="", tenant="", entry_known=False):
    """DB-history ranking of tenant/dc for a test (merged from the retired dc_resolve.py).
    Used by dc-pick for a test name that is not in tenant_map.json, or with --history.
    Never asks the owner for a dc; returns 0 when a PICK exists, 2 when only a tenant is needed."""
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_test_db", "db.py")
    out = subprocess.run(
        [sys.executable, db, "query-history", test, "--verdict", "any", "--top", "30", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        h = json.loads(out.stdout)
    except ValueError:
        print((out.stdout or out.stderr)[-1500:])
        return 1
    lane_norm = {"reg2": "reg02"}.get(lane.lower().replace("-", ""), lane.lower().replace("-", ""))
    # A Windows lane never takes a mac dc and a mac lane never takes a Windows dc (mac dcs end with "mac", see
    # feedback_mac_dc_needs_own_suffix). Without this, dc-pick DEM-01 local2 answered from MAC history (sys1340mac).
    want_mac = lane_norm.startswith("mac") if lane_norm else None
    stats = {}
    for ln in h.get("lanes", []):
        for e in ln.get("entries", []):
            t, dc = str(e.get("tenant")), e.get("dc")
            if not dc or t == "None":
                continue
            st = stats.setdefault((t, dc), {"pass": 0, "fail": 0, "lanes": set(), "last": ""})
            st["pass" if e["verdict"] == "PASS" else "fail"] += 1
            st["lanes"].add(ln["lane"].lower().replace("-", ""))
            st["last"] = max(st["last"], e.get("started_at", ""))
    cand = sorted(
        (
            (st["pass"], st["last"], t, dc, st)
            for (t, dc), st in stats.items()
            if t in _APPROVED_TENANTS
            and (not tenant or t == tenant)
            and (want_mac is None or str(dc).endswith("mac") == want_mac)
        ),
        key=lambda c: c[:4],
        reverse=True,
    )
    print(f"history: {h.get('test')}   approved tenants: {sorted(_APPROVED_TENANTS)}")
    for (t, dc), st in sorted(stats.items()):
        mark = "" if t in _APPROVED_TENANTS else "  [not approved]"
        print(f"  {t}/{dc}: pass {st['pass']} / fail {st['fail']} {sorted(st['lanes'])}{mark}")
    if not cand:
        print(
            "NO history on an approved tenant"
            + (f" {tenant}" if tenant else "")
            + ("" if want_mac is None else f" for a {'mac' if want_mac else 'Windows'} lane ({lane}); other-platform history is ignored")
        )
        print("ASK OWNER: tenant only (1340 or 1331). Never ask for the dc.")
        m = json.load(open(_TENANT_MAP, encoding="utf-8"))
        known = {c["dc"] for cs in m["cases"].values() for c in cs["combos"]} | {
            k.split("/", 1)[1] for k in m.get("dcs", {})
        }
        for (t0, dc0), s0 in sorted(stats.items(), key=lambda kv: -kv[1]["pass"]):
            if not s0["pass"]:
                continue
            kind = next((k for k in ("cloud", "static", "up", "cc", "failclose", "push") if k in dc0), "")
            for tt in sorted({tenant} if tenant else _APPROVED_TENANTS):
                for dc1 in sorted(k for k in known if tt in k and (k.endswith("mac") if want_mac else not k.endswith("mac"))):
                    if (kind and kind in dc1) or (not kind and "static" not in dc1 and "cloud" not in dc1):
                        print(
                            f"DERIVED: tenant={tt} dc={dc1} (same kind '{kind or 'plain'}' as {t0}/{dc0}, pass={s0['pass']})"
                        )
        return 2
    t, dc, st = cand[0][2], cand[0][3], cand[0][4]
    print(f"PICK: tenant={t} dc={dc} pass={st['pass']} fail={st['fail']}")
    if lane_norm and lane_norm not in st["lanes"]:
        print(f"WARN: no history for lane {lane} on this combo (ran on {sorted(st['lanes'])}); say so when confirming")
    if "cloud" in dc:
        print(f"NOTE: {dc} is a cloud-steering dc. Only a cloud-steering case belongs on it; say so in the confirm.")
    if st["pass"] == 0:
        print("WARN: this combo has NO PASS in history (only failures) - tell the owner before triggering")
    if not entry_known:
        print("NEXT: confirm with owner -> add the case to tenant_map.json (honest note) -> dc-pick <CASE> <lane> -> trigger")
    return 0



def _combo_evidence(flt, tenant, dc, lane=""):
    """One-line DB evidence for a (case, tenant, dc) combo -- appended to dc-pick
    output so the dispatch decision sees the LATEST measured result (retro
    2026-10-28, option C: decision input = live DB, map notes stay human).
    Looked up by the full test name of the lane's platform, shows the client version of the last run and flags
    evidence older than 30 days. Non-fatal: prints nothing on any DB error (dc-pick must never fail here)."""
    try:
        h = _db_history(_evidence_test_name(flt, lane))
        if not h:
            return
        recs = []
        for ln in h.get("lanes", []):
            for e in ln.get("entries", []):
                if str(e.get("tenant")) == str(tenant) and e.get("dc") == dc:
                    recs.append((str(e.get("started_at", "")), ln["lane"], e))
        if not recs:
            print(f"[evidence] {tenant}/{dc}: no DB history on this combo (map note is the only claim)")
            return
        recs.sort(reverse=True)
        last_at, last_lane, last = recs[0]
        recent = [e for _, _, e in recs[:5]]
        np = sum(1 for e in recent if e["verdict"] == "PASS")
        try:
            age = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(last_at)).days
        except Exception:
            age = None
        stale = f"  (STALE {age}d: older than 30 days)" if age is not None and age > 30 else ""
        print(
            f"[evidence] {tenant}/{dc}: last build {last.get('build')} ({last_at[:10]}, {last_lane}, client "
            f"{last.get('product_version') or '?'}) {last['verdict']}{stale} | last {len(recent)}: {np} PASS / "
            f"{len(recent) - np} not-PASS"
        )
        passes = [r for r in recs if r[2].get("verdict") == "PASS"]
        if passes:
            note = _code_changed_since(_evidence_test_name(flt, lane), passes[0][0])
            if note:
                print(note)
    except Exception:
        pass  # evidence is advisory only


def _grs_checkout():
    """A GRS checkout to read git history from: the current directory, else the usual per-agent clones."""
    for p in (os.getcwd(), r"C:\git\nsclient_golden_regression_suite", r"C:\git22\nsclient_golden_regression_suite"):
        if os.path.isdir(os.path.join(p, "golden_regression", "tests", "SystemTest")):
            return p
    return ""


def _code_changed_since(test, since_iso):
    """'' or a one-line [code] note: did the case's file or the SystemTest conftest.py change on origin/main after the
    combo's last PASS? A pass older than the code says nothing about the code now (STEER-06 mac after PR 575 and 590). Only
    those two files are watched on purpose: a wider list of shared modules would warn forever. Uses origin/main as of the
    last fetch."""
    repo = _grs_checkout()
    if not repo:
        return ""

    def git(*a):
        r = subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        return r.stdout.strip()

    hit = git("grep", "-l", f"def {test}(", "origin/main", "--", "golden_regression/tests/SystemTest").splitlines()
    files = [h.split(":", 1)[1] for h in hit if ":" in h][:1]
    if not files:
        return ""
    paths = files + ["golden_regression/tests/SystemTest/conftest.py"]
    log = git("log", "origin/main", f"--since={since_iso}", "--format=%h %s", "--", *paths).splitlines()
    day = since_iso[:10]
    if not log:
        return f"[code] {files[0].rsplit('/', 1)[-1]} and conftest.py unchanged since the last PASS ({day})"
    return f"[code] CHANGED since the last PASS ({day}): {len(log)} commit(s) on {files[0].rsplit('/', 1)[-1]} or conftest.py, newest {log[0][:90]}"


def _trigger_line(case, lane):
    """The whole ready-to-run trigger line for a lane, with the parameter names THIS job declares (REG2 wants `branch` and a
    HOSTNAME, MAC2-SSH wants GIT_BRANCH; both were refused once on 2026-10-28). Advisory: '' when Jenkins cannot answer."""
    try:
        names = _job_param_names(_job(lane)) or set()
    except Exception:
        return ""
    branch = "GIT_BRANCH" if "GIT_BRANCH" in names else "branch"
    # owner 2026-11-26: "remove all release info in dc-pick. dc has nothin todo with version." The line used to echo a
    # current_release read (which fell back to a hardcoded release-144 when logs/current_release.json was absent, and it
    # was absent), so every dc-pick advertised a stale version and a copied trigger installed it -- build 632 ran the
    # whole STRESS-05 50-iteration sweep on 144 instead of develop-145 that way. The version now comes only from
    # chapter_versions.json (chapter runs) or the newest train (everything else); never from this hint.
    parts = [
        f"python C:/git/claude-resource/grs_tools/grs_jenkins.py trigger {lane} case={case.upper()} task=<TASK-ID> iterations=1",
        f"{branch}=<BRANCH>",
    ]
    if "HOSTNAME" in names:
        parts.append("HOSTNAME=<VM from memory/reference_vm_ip_map.md>")
    # No `_force_preflight=1` for the LOCAL lanes any more (2026-10-29): preflight resolves their VM from the VM map and
    # checks the repo dir and .env keys; the flag would skip exactly those rows.
    parts.append("wake=<YOUR-AGENT-ID>")
    return " ".join(parts)


def cmd_dcpick(*args):
    """dc-pick <CASE|test-name> <lane> [--tenant T] [--history]
    CASE in tenant_map.json -> the resolved trigger params (the only source for a trigger).
    --history, or a name that is not a mapped CASE -> also/instead the DB-history ranking
    (what actually ran / passed). One tool: dc_resolve.py was merged in here."""
    pos = [a for a in args if not a.startswith("--")]
    flags = [a for a in args if a.startswith("--")]
    tenant = ""
    if "--tenant" in args:
        k = args.index("--tenant")
        tenant = args[k + 1] if k + 1 < len(args) else ""
        pos = [a for a in pos if a != tenant]
    # A fed (1457) pick needs the owner's own request; the words are the authorisation and they are audited.
    if "--fed-request" in args:
        k = args.index("--fed-request")
        _FED_REQUEST[0] = args[k + 1] if k + 1 < len(args) else ""
        pos = [a for a in pos if a != _FED_REQUEST[0]]
    if not pos:
        print("usage: dc-pick <CASE|test-name> [<lane>] [--tenant T] [--history]")
        return 2
    case, lane = pos[0], (pos[1] if len(pos) > 1 else "")
    m = json.load(open(_TENANT_MAP, encoding="utf-8"))
    mapped = case.upper() in m.get("cases", {})
    rc = 0
    if mapped and lane:
        params, err = _dc_pick(case, lane, tenant)
        if err:
            print(f"REFUSE: {err}")
            rc = 1
        else:
            for k, v in params.items():
                print(f"{k}={v}")
            _ti = params.get("tenant_info", "")
            _tm = re.search(r"--tenant\s+(\d+)\s+--dc=(\S+)", _ti)
            if _tm:
                _combo_evidence(
                    m["cases"][case.upper()].get("test_filter", case), _tm.group(1), _tm.group(2), lane
                )
            _case_entry = m["cases"].get(case.upper(), {})
            _flt = _case_entry.get("test_filter", case)
            for _c in _case_entry.get("combos", []):
                if lane not in _c.get("lanes", []):
                    continue
                if tenant and str(_c.get("tenant")) != str(tenant):
                    continue
                if _c.get("disabled"):
                    print(f"[combo] {_c['tenant']}/{_c['dc']}: disabled in the map")
                    continue
                _v = _combo_pass_verdict(_flt, _c["tenant"], _c["dc"], lane)
                _envf = _c.get("env", "boomskope_nonprod_stg.json")
                _ready = (
                    f"--custom-env-file=golden_regression/test_environment/{_envf} --tenant {_c['tenant']} "
                    f"--dc={_c['dc']} --is_64_bit=true"
                )
                print(f"[combo] {_c['tenant']}/{_c['dc']}: {_v}")
                # Every option comes ready to paste, so picking another tenant is one copy instead of a hand-assembled
                # string. Reason: a handover on 2026-11-26 quoted LOCAL2's combo (115615/systeststatic115615) for the
                # LOCAL1 box that is actually on 1331/systest1331, and only the picked combo had a usable line.
                print(f'        tenant_info="{_ready}"')
            # What the BOX runs right now, read from the box itself. The pick above is what the map and the evidence
            # prefer; the two are often different (the harness reinstalls for the pick). Reporting the pick as "the
            # box's tenant" is the mistake made in the SYS-03 handover on 2026-11-26 -- the box was on 1331 while the
            # handover quoted 115615 from LOCAL2's combo. One SSH round trip; --no-probe skips it.
            _box_ip = _LANE_VM_IP.get(lane.lower(), "")
            print(f"[box ] {_box_ip or '?'} ({lane}) -- current state read from the box:")
            if _box_ip and "--no-probe" not in flags:
                _b_ok, _b_txt = _vm_probe(
                    _box_ip,
                    "& 'C:/Program Files/Netskope/STAgent/nsdiag.exe' -f | Select-String 'Email|Client version|Tunnel status'",
                )
                _tag = re.search(r"\+([A-Za-z0-9_.-]+)@", _b_txt) if _b_ok else None
                _ver = re.search(r"Client version::\s*([0-9.]+)", _b_txt) if _b_ok else None
                _tun = re.search(r"Tunnel status::\s*(\S+)", _b_txt) if _b_ok else None
                if _tag:
                    print(
                        f"[box ]   tag={_tag.group(1)}"
                        + (f" client={_ver.group(1)}" if _ver else "")
                        + (f" tunnel={_tun.group(1)}" if _tun else "")
                    )
                    _picked = re.search(r"--dc=(\S+)", params.get("tenant_info", ""))
                    if _picked and _picked.group(1) != _tag.group(1):
                        print(
                            f"[box ]   WARN: the pick above is {_picked.group(1)}, the box is on {_tag.group(1)}. The "
                            "harness uninstalls and reinstalls the client for the pick (~6 min). Until that run happens, "
                            f"report this box as {_tag.group(1)} -- never as the picked dc."
                        )
                else:
                    print(f"[box ]   could not read the activation tag ({_b_txt[:90]})")
            else:
                print("[box ]   not probed (--no-probe): do NOT quote a tenant for this box from the pick above")
            _tl = _trigger_line(case, lane)
            if _tl:
                print(f"TRIGGER: {_tl}")
    if not mapped or "--history" in flags:
        test = m["cases"][case.upper()]["test_filter"] if mapped else case
        if not mapped:
            print(f"{case!r} is not a CASE in tenant_map.json; resolving from DB history")
        rc = _dc_history(test, lane, tenant, entry_known=mapped) or rc
    return rc


# VMs that run a self-runner agent (LOCAL1 = S2025-AUSTIN-SYS-03, LOCAL2 = W11-26H1-AUSTIN-SYS-04); the table in
# memory/reference_vm_ip_map.md (the lane-to-VM table) is the source; update both together.
_AGENT_VMS = {"S2025-AUSTIN-SYS-03", "W11-26H1-AUSTIN-SYS-04"}


def _job_param_default(jp, name):
    """The job's DECLARED default value for parameter `name` (what Jenkins uses when a trigger omits it), else None."""
    d = _get(f"{jp}/api/json?tree=property[parameterDefinitions[name,defaultParameterValue[value]]]")
    for prop in d.get("property", []) or []:
        for p in prop.get("parameterDefinitions") or []:
            if p.get("name") == name:
                return (p.get("defaultParameterValue") or {}).get("value")
    return None


def _agent_vm_granted(vm_name):
    """True when .coord/lane_grants.json holds a live, owner-worded grant for the lane that VM backs.

    Lane -> VM comes from grs_tools/lane_vms.json (git's file, read only here); grants come from
    lane_hold.py's grant subcommand. Unreadable files mean NO exception."""
    import time as _t
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        lv = json.load(open(os.path.join(root, "grs_tools", "lane_vms.json"), encoding="utf-8"))
        lane = next((v.get("lane") for v in lv.get("vms", [])
                     if str(v.get("name", "")).upper() == vm_name.upper()), None)
        if not lane:
            return False
        grants = json.load(open(os.path.join(root, ".coord", "lane_grants.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return False
    g = grants.get(lane) or {}
    return bool(g.get("to")) and float(g.get("until") or 0) > _t.time()


def _hostname_must_be_explicit(jp, kv_d):
    """REFUSE text when the job takes a HOSTNAME param and this trigger does not pass one, else None.

    RCA 2026-10-24 (REG FC-03 build with 'Target VM AUSTIN-FED-SYSTEST not found in OpenStack'): `trigger` POSTs only
    the k=v pairs it was given, so an omitted HOSTNAME silently becomes the JOB's declared default, which on REG is a
    VM that no longer exists. Preflight did not see it: it validates `merged` = the LAST build's params + the
    overrides, i.e. the last build's HOSTNAME (a listed VM), not the value Jenkins would actually run with, and the
    trigger path skips the params-explicit channel. Validated != sent. The fix is to make the VM an explicit,
    always-checked input for every job that has the param."""
    names = _job_param_names(jp)
    if not names or "HOSTNAME" not in names:
        return None
    given = (kv_d.get("HOSTNAME") or "").strip()
    if given:
        # An SSH-mode job (REG, REG-02, AUSTIN) must never drive a VM that hosts a LOCAL1 / LOCAL2 agent: the
        # allowlist accepts those names (they are active VMs) but they belong to the self-runner lanes.
        if given.upper() in _AGENT_VMS:
            # Owner-granted exception (2026-11-04): .coord/lane_grants.json records a lane VM handed to an
            # agent WITH the owner's verbatim words. When that grant is live for this VM, the lane owner is
            # driving it deliberately (e.g. it must read a client log on that VM afterwards), so an SSH job
            # is allowed to target it. No grant file, no exception; the refusal below stays the default.
            if _agent_vm_granted(given):
                print(f"NOTICE: HOSTNAME={given} hosts a LOCAL1/LOCAL2 agent but is granted by the owner "
                      f"(.coord/lane_grants.json); allowing {jp} to drive it")
            else:
                return (
                    f"REFUSE: HOSTNAME={given} hosts a LOCAL1 / LOCAL2 Jenkins agent (see the lane table in "
                    f"memory/reference_vm_ip_map.md); {jp} drives its VM over SSH. Use S2025-AUSTIN-SYS-02 or "
                    "W11-26H1-AUSTIN-SYS-07."
                )
        return None
    default = _job_param_default(jp, "HOSTNAME")
    return (
        f"REFUSE: this trigger does not pass HOSTNAME, so Jenkins would run {jp} on the job default "
        f"{default!r}, not on the VM your last build used. Pass HOSTNAME=<VM> explicitly "
        "(the active VMs are the table in memory/reference_vm_ip_map.md; REG and REG-02 share S2025-AUSTIN-SYS-02)."
    )



def cmd_wake(job, num, agent, sender, task_id="-"):
    """Wait (no model, no tokens) until build `num` of `job` ends, then drop ONE message per agent in the inbox of each, signed `sender`.
    `agent` may be comma-separated (wake local1 112 git22,git44 git22): one watcher per recipient.

    Why (2026-10-28): a pi agent does not come back by itself after a long wait; it acts only on an owner ping or an
    inbox arrival, so the evidence of a finished build was read ten hours late. `trigger ... wake=<agent>` arms this."""
    agents = [x.strip() for x in agent.split(",") if x.strip()]
    for ag in agents:
        _wake_one(job, num, ag, sender, task_id)
    return 0


def _wake_run_line(jp, num):
    """What the build ran and for whom: the case filter, the branch and the agent named in the branch token, read from the build's own
    parameters. Without it a wake-up 'sent for git22' hid that the run was another agent's (2026-11-10: a mac EPAIS-05 run of git read as
    git22's own task). Empty string when the parameters cannot be read; the wake-up is never held back by this."""
    try:
        acts = _get(f"{jp}/{num}/api/json?tree=actions[parameters[name,value]]").get("actions") or []
        par = {p.get("name"): str(p.get("value")) for a in acts for p in (a.get("parameters") or []) if p.get("name")}
    except Exception:  # noqa: BLE001
        return ""
    cases = par.get("test_cases") or par.get("TEST_CASES") or ""
    branch = par.get("GIT_BRANCH") or par.get("branch") or ""
    title = par.get("test_title") or ""
    who = re.match(r"acheng/(git\d*)-", branch)
    parts = [f"case filter: {cases}" if cases else "", f"branch: {branch}" if branch else "", f"title: {title}" if title else "",
             f"branch token agent: {who.group(1)}" if who else ""]
    line = "; ".join(p for p in parts if p)
    return f"Run: {line}. " if line else ""


def _wake_delivery_step(task_id, sender):
    """Who the result must reach: the requester first when it is not the agent that armed the watcher."""
    requester = _task_requester(task_id)
    if requester and requester != sender:
        return (
            f"one coord message to {requester} with the build number, result and the lines (their run), "
            f"and one to {sender}"
        )
    return f"one coord message to {sender} with the build number, result and the lines"


def _task_requester(task_id):
    """The agent that asked for this task, read from the card's `approved_by: ... request from <agent>`.

    A wake-up is armed by the agent that TRIGGERED the run, which is not always the agent that ASKED for it. On
    2026-11-15 that difference cost git33 three hours: his two validation runs finished, I reported both into my own
    task note, and step 4 of the wake-up told me to message the sender -- myself -- so he never got either result.
    """
    if not task_id or task_id == "-":
        return None
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tasks", f"{task_id}.md"),
                  encoding="utf-8") as fh:
            head = fh.read(2000)
    except OSError:
        return None
    m = re.search(r"request from\s+([a-z]+\d*)", head)
    return m.group(1) if m else None


def _wake_one(job, num, agent, sender, task_id="-"):
    jp = _job(job)
    end, d = time.time() + 6 * 3600, {}
    while time.time() < end:
        try:
            d = _get(f"{jp}/{num}/api/json?tree=building,result,description")
        except Exception:  # Jenkins hiccup: keep waiting
            time.sleep(60)
            continue
        if not d.get("building") and d.get("result"):
            break
        time.sleep(30)
    result = d.get("result") or "NOT-FINISHED-IN-6H"
    desc = " ".join((d.get("description") or "").split())[:200]
    text = (
        f"WAKE-UP (build watcher, sent for {sender}): build {num} on {jp} has ended, result={result}. {desc} "
        f"{_wake_run_line(jp, num)}Task: {task_id}. The build is finished, so do not run monitor. Steps (this is the only copy of them): "
        f"1) python C:/git/claude-resource/grs_tools/grs_jenkins.py console {job} {num} "
        f"\"\\[[A-Z]+-[0-9]+\\]|short test summary|FAILED|iteration [0-9]+/[0-9]+|Client version\" and quote the lines verbatim; "
        f"2) the backup command with the filter written in your task (grs_jenkins.py backup {job} {num} \"<filter>\"); "
        f"3) one note on {task_id}, then task.py move {task_id} closure --force -m \"ran; evidence in notes\"; "
        f"4) {_wake_delivery_step(task_id, sender)} No conclusions."
    )
    path = _tmp_path(f"wake_{num}_{agent}.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")
    coord = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coord.py")
    r = subprocess.run([sys.executable, coord, "msg", agent, "--text-file", path, "--from", sender], capture_output=True, text=True)
    print((r.stdout + r.stderr).strip())
    _notify_waiters(job, num, result, desc)


def _notify_waiters(job, num, result, desc=""):
    """Tell every agent registered with coord.py wait-build for this build (green or red); once per entry."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import coord

        n = coord.waiters_notify(job, num, result, desc)
        if n:
            print(f"[waiters] {n} registered waiter(s) of {job}#{num} notified")
    except Exception as e:  # noqa: BLE001
        print(f"[waiters] notify failed: {e}")


def cmd_waitnotify(job, num):
    """Watcher armed by coord.py wait-build: poll until the build ends, then notify the registered waiters."""
    jp = _job(job)
    end, d = time.time() + 12 * 3600, {}
    while time.time() < end:
        try:
            d = _get(f"{jp}/{num}/api/json?tree=building,result,description")
        except Exception:  # noqa: BLE001  Jenkins hiccup: keep waiting
            time.sleep(60)
            continue
        if not d.get("building") and d.get("result"):
            break
        time.sleep(30)
    _notify_waiters(job, num, d.get("result") or "NOT-FINISHED-IN-12H", " ".join((d.get("description") or "").split())[:200])
    return 0


def _spawn_wake(job, num, agent, task_id):
    """Start the detached watcher for `trigger ... wake=<agent>`. The sender is whoever is triggering: pi's inbox accepts
    only git, git33, git44 and owner, so an agent cannot wake itself, and the owner agent triggers instead."""
    try:
        import coord

        sender = coord._infer_agent()
    except Exception:  # noqa: BLE001
        sender = ""
    if not sender or sender == "unknown":
        print("[wake] cannot tell who is triggering, so no wake-up was armed")
        return
    # sender == agent is a self-wake: since 2026-10-28 the pi inbox extension accepts its own agent id (git22 commit
    # 4a99145b), and only git22 triggers tests, so the watcher must be allowed to wake the agent that started it.
    log = open(_tmp_path(f"wake_{num}_{agent}.log"), "ab")
    flags = (0x00000008 | 0x00000200) if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "wake", job, str(num), agent, sender, task_id],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True,
    )
    print(f"[wake] armed: {agent} gets one inbox message from {sender} when build {num} ends (log {log.name})")


_SYSTEMTEST_LANE = re.compile(r"GRS-SYSTEMTEST-[A-Z0-9]+", re.IGNORECASE)


def _param_set_refuse(jp, kv_names):
    """REFUSE text when a hand-typed trigger on a non-SystemTest job drops a declared param, else ''.

    RCA 2026-11-12 (GRS-AUSTIN 87): I typed the params of an unfamiliar job by hand and left out
    OS_CLOUD_PROJECT, so the pipeline's `openstack server list` had no project to look the VM up in
    and the build failed in 0 minutes with "Target VM ... not found in OpenStack". Every param was
    valid, and `ch_params_complete` only demands iterations/tenant_info/release_info/branch, so
    nothing refused it. The SystemTest lanes are exempt: their param set is routine here, and a
    missing param there fails loudly in the pytest stage instead of silently scoping a VM lookup.
    """
    if _SYSTEMTEST_LANE.search(jp or ""):
        return ""
    try:
        declared = _job_param_names(jp)
    except Exception:  # noqa: BLE001 - an unreadable job must not block a legitimate run
        return ""
    if not declared:
        return ""
    missing = sorted(n for n in declared if n not in kv_names)
    if not missing:
        return ""
    return (
        f"REFUSE: {jp} declares {len(declared)} parameter(s) and this hand-typed trigger states "
        f"{len(kv_names)}; missing: {missing}. A forgotten param on a job we do not run daily is how "
        "GRS-AUSTIN 87 died in 0 minutes ('Target VM ... not found in OpenStack' -- OS_CLOUD_PROJECT "
        "was missing, so the VM lookup had no project). Use: grs_jenkins.py retrigger <job> <last good "
        "build> k=v... -- that posts the whole inherited set and only overrides what must change."
    )


def cmd_trigger(job, *kv):
    jp = _job(job)
    # case=X auto-resolves test_cases + tenant_info + timeout from the tenant
    # map (hard rule owner 2026-08-17: never hand-assemble). Explicit
    # tenant_info/test_cases in kv still win (power-user override).
    kv_d = dict(item.partition("=")[::2] for item in kv)
    case = kv_d.pop("case", None)
    # Only overwrite when this command carries the marker: cmd_retrigger sets DC_SRC and then calls
    # through here, so an unconditional assignment would wipe the value it just set (found live 2026-11-04).
    DC_CASE[0] = case or ""
    if "_dc_given" in kv_d:
        DC_SRC[0] = kv_d.pop("_dc_given")  # peer|owner: who named the dc; local only, never POSTed
    task_id = kv_d.pop("task", None)  # which kanban task this run serves; checked below, never sent to Jenkins
    wake_agent = kv_d.pop("wake", None)  # agent(s) to wake with one inbox message each when the build ends; never sent to Jenkins.
    # comma-separated list (wake=git22,git44) so every stakeholder gets woken -- owner rule 2026-10-28:
    # the assigning agent must not have to poll for results of runs the trigger already started.
    if task_id is not None or wake_agent is not None:
        kv = tuple(f"{k}={v}" for k, v in kv_d.items())
    if case:
        # NOTE: no current_release check on purpose. On a stg tenant an omitted
        # --current_release is expected to install the gate release (143); that
        # is the job's own default (owner 2026-10-15), not an error to refuse.
        # 2026-10-29 RCA (git22-0033, REG): this block used to run only when the caller passed NO tenant_info, so
        # `case=STRESS-06 ... tenant_info=...` skipped the whole resolution, test_cases was inherited from the last build
        # (stress_01) and the wrong test ran. The case is resolved every time; an explicit k=v still wins below.
        explicit_tenant_info = kv_d.get("tenant_info")
        # 2026-10-29 RCA (git22-0033): STRESS-05 on 115615/systeststatic115615 was triggered while REG ran the same static-win
        # config. dc-pick REFUSED it, but a hand-passed tenant_info took the "not resolvable, use the caller's" branch below
        # and the trigger went through. Resolve against the caller's tenant, and never let a conflict refusal be bypassed.
        _ex_t = re.search(r"--tenant\s+(\S+)", explicit_tenant_info or "")
        # 2026-11-01 RCA (git, REG build 272): `dc-pick STRESS-08 reg --tenant 1340` was run and proposed, then the trigger
        # went out with no tenant_info; the trigger's own dc-pick ranked 1331/systeststatic1331 first (newest PASS) and the
        # run landed on a tenant nobody chose. With more than one approved combo the ranking must never decide: the caller
        # passes the tenant_info it picked.
        if explicit_tenant_info is None:
            try:
                _m = json.load(open(_TENANT_MAP, encoding="utf-8"))
                _opts = [
                    f"{c['tenant']}/{c['dc']}"
                    for c in (_m.get("cases", {}).get(case.upper(), {}).get("combos") or [])
                    if job in c.get("lanes", []) and not c.get("disabled")
                ]
            except Exception:
                _opts = []
            if len(_opts) > 1:
                print(
                    f"REFUSE: {case} has {len(_opts)} approved combos on {job} ({', '.join(_opts)}); pass "
                    f'"tenant_info=..." from `dc-pick {case} {job} --tenant <T>` so the run uses the tenant you chose, '
                    "not the ranking's first pick (REG build 272)"
                )
                return 1
        picked, err = _dc_pick(case, job, _ex_t.group(1) if _ex_t else "")
        if err:
            if explicit_tenant_info is None or "conflict" in err:
                print(f"REFUSE: {err}")
                return 1
            # 2026-10-29 (git22-0033): STRESS-08 win on 115615 got through this branch -- dc-pick failed with "no approved combo
            # for tenant 115615", the text has no "conflict", and the declared-combo gate below was only reached when dc-pick
            # SUCCEEDED with a different pick. A caller's tenant_info must be a declared combo whether or not dc-pick resolved.
            _cg0 = _combo_gate(case, job, explicit_tenant_info)
            if _cg0:
                print(f"[dc-pick] REFUSE {case}: {err[:160]}")
                print(f"REFUSE: {_cg0}")
                return 1
            print(f"[dc-pick] {case}: not resolvable ({err[:160]}); using the caller's tenant_info, test_cases must be explicit")
            picked = {}
        elif explicit_tenant_info is not None and picked.get("tenant_info") and picked["tenant_info"] != explicit_tenant_info:
            _cg = _combo_gate(case, job, explicit_tenant_info)
            if _cg:
                print(
                    f"[dc-pick] REFUSE {case}: your tenant_info differs from the map's pick. "
                    f"map: {picked['tenant_info']} | yours: {explicit_tenant_info}"
                )
                print(f"REFUSE: {_cg}")
                return 1
            print(
                f"[dc-pick] WARN {case}: your tenant_info differs from the map's pick. map: {picked['tenant_info']} | "
                f"yours: {explicit_tenant_info}. Yours is used; a config-push case on the wrong dc fails or damages the tenant."
            )
        # Not every job accepts every key _dc_pick can fill in (e.g. LOCAL1/
        # LOCAL2 have no `timeout` param, only REG/REG2 do) -- drop whichever
        # auto-filled keys this job doesn't declare, SILENTLY, rather than
        # letting the param-name gate below reject the whole trigger over a
        # key the tool itself generated, not one the caller mistyped.
        valid_for_job = _job_param_names(jp)
        picked = _adapt_picked_for_job(picked, valid_for_job)
        if valid_for_job is not None:
            dropped = {k: v for k, v in picked.items() if k not in valid_for_job}
            if dropped:
                print(f"[dc-pick] {job} has no param for {sorted(dropped)} -- dropped")
            picked = {k: v for k, v in picked.items() if k in valid_for_job}
        for k, v in picked.items():
            kv_d.setdefault(k, v)
        if "test_cases" not in kv_d:
            print(
                f"REFUSE: case={case} did not resolve test_cases and none was given. Without it the run would inherit the "
                "LAST build's test_cases and run a different test. Pass test_cases=... explicitly, or fix tenant_map.json."
            )
            return 1
        # self-runner lanes pick the test from the CASE choice param; CUSTOM
        # makes them honour test_cases (the one-case -k filter built above).
        if valid_for_job is not None and "CASE" in valid_for_job:
            kv_d.setdefault("CASE", "CUSTOM")
        kv = tuple(f"{k}={v}" for k, v in kv_d.items())
        # Echo what will actually be POSTed (explicit k=v from the caller already won above), so a wrong
        # test_cases / test_suites / timeout is visible at trigger time, not 50 minutes later.
        shown = {k: kv_d.get(k) for k in ("test_cases", "tenant_info", "test_suites", "timeout", "timeout_min")}
        print(f"[dc-pick] {case}: " + " | ".join(f"{k}={v}" for k, v in shown.items() if v))
    # Owner 2026-11-05: "this is final run for explicit_proxy. no more" -- the suite is closed after the one run
    # that was already queued. A refusal in the tool, because a rule that lives in memory gets forgotten.
    if "explicit_proxy" in (kv_d.get("test_suites") or "") and not kv_d.get("_owner_said"):
        print("REFUSE: explicit_proxy is closed for this chapter (the owner said: this is final run for explicit_proxy. no more). "
              "No rerun, no retry, no second filter. If he re-authorises it in words, pass _owner_said=\"<his words>\".")
        return 1
    kv_d.pop("_owner_said", None)
    _unknown = _unknown_test_tokens(kv_d.get("test_cases", ""), kv_d.get("test_suites", ""), kv_d.get("GIT_BRANCH") or kv_d.get("branch") or "")
    if _unknown:
        print(
            f"REFUSE: test_cases names {_unknown}, but no test function in golden_regression/tests/SystemTest contains that "
            "name, so the build would die at collection (or run nothing) and no case would match for config-guard. "
            "Use case=<CASE_ID> so the filter comes from tenant_map.json."
        )
        return 1
    # bitness alignment (REG 329 RCA): --build_url pinning a 64-bit MSI while
    # tenant_info lacks --is_64_bit=true -> install gate fails on path mismatch.
    # When case= resolved the bitness, the build_url's MSI name must agree.
    _ti = kv_d.get("tenant_info", "")
    _ri = kv_d.get("release_info", "")
    # HARD RULE (owner 2026-10-16, extended 2026-10-29): --build_url accepts
    # FEATURE builds and EHF builds only. A develop / regular release client
    # must be selected with --current_release=develop-NNN / release-NNN, never
    # by pinning an Artifactory URL. EHF builds live under
    # artifactory-rd/.../release/<ver>/<build>/ with an explicit EHF marker in
    # the artifact path -- owner 2026-10-29: "ehf is thing to test. we use
    # build_url, and you can see in url this is not a feature build.
    # build_url does not allow develop build and release build, accept EHF
    # build or feature build."
    _bu = re.search(r"--build_url=(\S+)", _ri)
    if _bu:
        url = _bu.group(1)
        is_feature = bool(re.search(r"feature", url, re.I))
        is_ehf = bool(re.search(r"EHF", url, re.I)) or "/client-builds/release/" in url.replace("\\", "/")
        # owner 2026-10-29: a version like X.0.0 (142.0.0, 143.0.0, ...) is a
        # MONTHLY RELEASE build -- build_url is refused for it; it must be
        # selected with --current_release=release-<X>. Non .0.0 versions under
        # the EHF layout (143.0.2/2839) are EHF builds and stay allowed.
        _ver = re.search(r"/client-builds/release/(\d+\.\d+\.\d+)/", url.replace("\\", "/"))
        if _ver and re.fullmatch(r"\d+\.0\.0", _ver.group(1)):
            print(
                f"REFUSE: --build_url pins {_ver.group(1)} -- a X.0.0 monthly release "
                f"build. Monthly releases must use "
                f"release_info=--current_release=release-{_ver.group(1).split('.')[0]}; "
                f"build_url accepts feature and EHF (non-X.0.0) builds only."
            )
            return 1
        if not (is_feature or is_ehf):
            print(
                f"REFUSE: --build_url={url} is neither a feature build nor an EHF build. "
                "build_url accepts feature and EHF builds only; use "
                "release_info=--current_release=develop-NNN / release-NNN for "
                "develop and regular release builds."
            )
            return 1
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
    _tdc, _ttn = re.search(r"--dc=(\S+)", _ti), re.search(r"--tenant\s+(\S+)", _ti)
    if "systemtest" in jp.lower():
        try:
            _tm = json.load(open(_TENANT_MAP, encoding="utf-8"))
        except Exception:
            _tm = {}
        if _tdc and _ttn:
            _dcerr = _dc_platform_error(_tm, _ttn.group(1), _tdc.group(1), jp, kv_d.get("test_cases", ""))
            if _dcerr:
                print(f"REFUSE: {_dcerr}")
                return 1
        _terr = _task_case_error(task_id, (case or "").upper() or _case_of_filter(_tm, kv_d.get("test_cases", "")))
        if _terr:
            print(f"REFUSE: {_terr}")
            return 1
        # A run for a task that is still in study, poc or design is a POC (owner 2026-10-27: a POC pass is NOT the task
        # passing). Label it so the Jenkins title, the report and any PR checklist show it; task.py's validate gate also
        # ignores builds started before the task entered validate.
        _stage = _task_stage(task_id)
        if _stage in ("study", "poc", "design"):
            _tt = kv_d.get("test_title", "")
            if not _tt.upper().startswith("POC"):
                kv_d["test_title"] = ("POC: " + _tt).strip()
                kv = tuple(f"{k}={v}" for k, v in kv_d.items())
            print(f"[poc] task {task_id} is at {_stage}: this run is labelled POC. Its result is evidence for the study, not task validation.")
    # Preflight BEFORE anything else (owner 2026-08-16): the check lives INSIDE
    # the action — no harness, no memory, no discipline required. Any REFUSE
    # row blocks the POST. Escape hatch: _force_preflight=1 (stripped, never
    # becomes a Jenkins param). plan_trigger_preflight.md.
    refuse = _hostname_must_be_explicit(jp, kv_d)
    if refuse:
        print(refuse)
        return 1
    force = kv_d.pop("_force_preflight", None)
    # Separate, narrower override for the vm-allowlist channel only (owner
    # 2026-09-09 hard rule: "we can only use listing vm to trigger, unless i
    # told you to"). Deliberately NOT folded into `_force_preflight` — that flag
    # is reached for to clear an unrelated REFUSE (env-json, vm-processes, ...)
    # and must never incidentally also authorize an unlisted VM. Only ever pass
    # this when the user has said so in the SAME turn.
    allow_vm = kv_d.pop("_allow_vm", None)
    # `vm=<ip>` names the target VM for the preflight channels on lanes whose params carry
    # no HOSTNAME (LOCAL1/LOCAL2 — see _pf_setup). Stripped here so it never reaches the POST.
    vm_ip = kv_d.pop("vm", None)
    if force or allow_vm or vm_ip:
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
    vm_refuses = [r for r in refuses if r[0] == "vm-allowlist"]
    other_refuses = [r for r in refuses if r[0] != "vm-allowlist"]
    if vm_refuses and not allow_vm:
        print(f"trigger REFUSED by preflight (vm={ip}):")
        for name, sev, detail in rows:
            print(f"  [{sev:6}] {name}: {detail}")
        print("only the user can authorize this — pass _allow_vm=1 if they just did")
        return 1
    if other_refuses and not force:
        print(f"trigger REFUSED by preflight (vm={ip}):")
        for name, sev, detail in rows:
            print(f"  [{sev:6}] {name}: {detail}")
        print(
            "fix with: grs_jenkins.py recover <job> [k=v...]  — or _force_preflight=1 to override"
        )
        return 1
    if allow_vm and vm_refuses:
        print(
            f"⚠️  vm-allowlist REFUSE overridden by _allow_vm ({len(vm_refuses)} row(s)):"
        )
        for name, _sev, detail in vm_refuses:
            print(f"  [FORCED] {name}: {detail}")
    if force and other_refuses:
        print(
            f"⚠️  preflight REFUSE overridden by _force_preflight ({len(other_refuses)} row(s)):"
        )
        for name, _sev, detail in other_refuses:
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
    # Param-name gate (LOCAL2 build 60 RCA, see _job_param_names docstring):
    # an unknown key means Jenkins would silently fall back to the job's
    # default for that param -- e.g. `tenant=1457` typo'd for `tenant_info`
    # ran the WRONG tenant for 6 minutes before anyone noticed. Refuse before
    # touching the queue at all. None = job has no declared params -> skip.
    valid_names = _job_param_names(jp)
    if valid_names is not None:
        sent_names = {item.partition("=")[0] for item in kv}
        unknown = sent_names - valid_names
        if unknown:
            print(f"REFUSE: unknown param name(s) for {job}: {sorted(unknown)}")
            print(f"  valid names: {sorted(valid_names)}")
            return 1
    # DEM release check (owner 2026-10-28, "just ensure version >= 143"): a DEM case needs a release number >= 143,
    # release-N or develop-N; the DEM agent ships from 143 on. A build_url pins a build whose version is not readable
    # here, so it passes. (History: build 65 fell back to a stock develop-143 that had no DEM agent yet.)
    _tc = kv_d.get("test_cases", "")
    if re.search(r"dem", _tc, re.IGNORECASE) and "build_url" not in _ri:
        _rel = re.search(r"--current_release=(?:release|develop)-(\d+)", _ri)
        if not _rel or int(_rel.group(1)) < 143:
            print(
                "REFUSE: test_cases targets a DEM case but release_info does not carry "
                "--current_release=release-N or develop-N with N >= 143 (the DEM agent ships "
                "from 143 on). Pass release_info=--current_release=release-144."
            )
            return 1
    # Chapter lock (see cmd_chapter docstring): a correctly-spelled tenant_info
    # can still carry the WRONG tenant number -- the param-name gate above
    # cannot see that. Only checked when tenant_info is actually part of THIS
    # trigger call; jobs/calls with no tenant_info are unaffected.
    chapter = _active_chapter(job)
    if chapter and "tenant_info" in kv_d:
        want = str(chapter["tenant"])
        m = re.search(r"--tenant\s+(\S+)", kv_d["tenant_info"])
        got = m.group(1) if m else None
        if got != want:
            print(
                f"REFUSE: chapter '{chapter['name']}' locks tenant={want}, "
                f"this trigger's tenant_info has tenant={got!r}"
            )
            print("  chapter clear  — if this tenant change is deliberate")
            return 1
    # Allow queueing builds (2026-09-10: MAC2 batch trigger needs queue support)
    d = _get(f"{jp}/api/json?tree=builds[building],inQueue")
    building_or_queued = any(b["building"] for b in d.get("builds", [])) or d.get(
        "inQueue"
    )
    if building_or_queued:
        print(
            f"⚠️  Job has builds running or queued — this build will be queued by Jenkins"
        )
    # Config journal (owner design 2026-10-17): a case that really PUSHES tenant config
    # first recovers any open journal for that config (an earlier abort), then records the
    # config's current rows. monitor settles it when the build ends. See config_guard.py.
    guard_entry = None
    try:
        import config_guard

        _tgt = config_guard.target(kv_d.get("test_cases", ""), kv_d.get("tenant_info", ""))
        if _tgt:
            _ok, _msg, guard_entry = config_guard.prepare(
                _tgt, lane=job, is_running=_run_ref_running, lane_busy=bool(building_or_queued)
            )
            print(_msg)
            if not _ok:
                return 1
    except ImportError:
        pass
    payload = []
    for item in kv:
        k, _, v = item.partition("=")
        payload.append((k, v))
    jbase = _base_for(jp)
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op, server=jbase)
    req = urllib.request.Request(
        f"{jbase}/{jp}/buildWithParameters",
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
            d = _get(
                f"queue/item/{qid}/api/json?tree=executable[number],why", server=jbase
            )
        except Exception:
            break
        ex = d.get("executable")
        if ex and ex.get("number"):
            print(
                f"build={ex['number']}  (poll: status {_job(job).split('/')[-1]} {ex['number']})"
            )
            if guard_entry:
                config_guard.attach(guard_entry, f"{job}#{ex['number']}")
            for _wa in [a.strip() for a in (wake_agent or "").split(",") if a.strip()]:
                _spawn_wake(job, ex["number"], _wa, task_id or "-")
            return
        time.sleep(3)
    print(f"build=?  (queue {qid} not yet assigned; re-check with: builds {job} 1)")


def cmd_retrigger(job, src, *kv):
    """Copy params from SRC build, apply overrides, then trigger. Reduces the
    per-op token count to `retrigger JOB SRC branch=... [more]=...` so one
    approval covers both the param-copy and the POST — the flat `trigger`
    form needs 8+ k=v pairs when re-running an UPGRADE/STRESS suite."""
    _kv_names = {item.partition("=")[0] for item in kv}
    _jp_for_check = _job(job)
    _refuse = _param_set_refuse(_jp_for_check, _kv_names)
    if _refuse:
        print(_refuse)
        return

    jp = _job(job)
    d = _get(f"{jp}/{src}/api/json?tree=actions[parameters[name,value]]")
    base = _params(d.get("actions", []))
    overrides = {}
    for item in kv:
        k, _, v = item.partition("=")
        overrides[k] = v
    DC_SRC[0] = overrides.pop("_dc_given", "")  # peer|owner: local only, never POSTed
    merged = dict(base)
    merged.update(overrides)
    # retrigger-inheritance guard (builds 307/110/111 all ran the WRONG branch):
    # the load-bearing params must be EXPLICIT in this retrigger, never inherited.
    # valid_names scopes the requirement to params THIS job actually has (RCA
    # 2026-09-17: GRS-AUSTIN has no `iterations` param, so requiring it here
    # unconditionally refused every AUSTIN retrigger with no way to comply).
    import grs_preflight as _pf

    valid_names = _job_param_names(jp)
    _n, sev, detail = _pf.ch_params_complete(merged, overrides, valid_names=valid_names)
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
        req = urllib.request.Request(f"{_base_for(jp)}/{jp}/{num}/replay/")
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
    jbase = _base_for(jp)
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op, server=jbase)
    data = urllib.parse.urlencode(
        {
            "mainScript": script,
            ".crumb": crumb["crumb"],
            "json": json.dumps({"mainScript": script}),
            "Submit": "Run",
        }
    ).encode()
    req = urllib.request.Request(
        f"{jbase}/{jp}/{num}/replay/run", data=data, method="POST"
    )
    req.add_header("Authorization", f"Basic {AUTH}")
    req.add_header(crumb["crumbRequestField"], crumb["crumb"])
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    r = op.open(req, timeout=20)
    src = "custom script" if script_file else "original script"
    print(f"replayed {jp}/{num} with {src}: status={r.status} url={r.url}")


ABORT_AUDIT = os.path.join(
    # realpath -- see _VM_MAP_MD's comment for why abspath breaks here.
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
    "abort_audit.log",
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
    jbase = _base_for(_job(job))
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op, server=jbase)
    req = urllib.request.Request(
        f"{jbase}/{_job(job)}/doDelete", data=b"", method="POST"
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
    jbase = _base_for(_job(job))
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op, server=jbase)
    endpoints = {"stop": "stop", "term": "term", "kill": "kill"}
    ep = endpoints.get(mode, "stop")
    req = urllib.request.Request(
        f"{jbase}/{_job(job)}/{num}/{ep}", data=b"", method="POST"
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
    _abort_release_vmlock(job, num)


def _abort_release_vmlock(job, num):
    """Best effort: free the per-VM jenkins_vm.lock the aborted build held.

    A kill/term never reaches the pipeline's own unlock step, so the next build on that VM waits in
    queue for up to its whole budget (RCA 2026-11-12, GRS-AUSTIN 89/90 -> 91). The release goes
    through cmd_vmlock, which refuses while the holder build is still running."""
    global _VMLOCK_IP
    ip = ""
    try:
        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        req = urllib.request.Request(f"{_base_for(_job(job))}/{_job(job)}/{num}/consoleText")
        req.add_header("Authorization", f"Basic {AUTH}")
        text = op.open(req, timeout=30).read().decode("utf-8", "replace")
        for line in text.splitlines():
            m = re.search(r"Target VM found:\s*(10\.\d+\.\d+\.\d+)", line)
            if m:
                ip = m.group(1)
    except Exception as exc:  # noqa: BLE001 - a failed lookup must not hide the abort itself
        print(f"vm-lock: could not read the build console to find its VM ({exc.__class__.__name__})")
        return
    if not ip:
        return
    _VMLOCK_IP = ip
    print(f"vm-lock: releasing the lock this build held on {ip} (the abort skips the pipeline's unlock)")
    try:
        cmd_vmlock("release", ip)
    except Exception as exc:  # noqa: BLE001
        print(f"vm-lock: release failed ({exc.__class__.__name__}); run: grs_jenkins.py vm-lock release {ip}")


def cmd_pause(job):
    """Disable a job (Jenkins' own enable/disable toggle, NOT config.xml/groovy
    edit -- just the isDisabled flag) so no new build can start, whether from
    a cron trigger, a chained `build job:` step, or a manual click. Reports
    the PRE-pause state first so the operator has a record to resume from
    later (cmd_resume just flips it back)."""
    jp = _job(job)
    jbase = _base_for(jp)
    before = _get(f"{jp}/api/json?tree=disabled", server=jbase)
    print(f"before: disabled={before.get('disabled')}")
    if before.get("disabled"):
        print(f"{jp} is already disabled -- no-op")
        return
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op, server=jbase)
    req = urllib.request.Request(f"{jbase}/{jp}/disable", data=b"", method="POST")
    req.add_header("Authorization", f"Basic {AUTH}")
    req.add_header(crumb["crumbRequestField"], crumb["crumb"])
    try:
        status = op.open(req, timeout=15).status
        print("disable status:", status)
    except urllib.error.HTTPError as e:
        print(f"disable HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
        return
    after = _get(f"{jp}/api/json?tree=disabled", server=jbase)
    print(f"after: disabled={after.get('disabled')}")
    with open(ABORT_AUDIT, "a", encoding="utf-8") as f:
        f.write(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {jp} | mode=disable | before=False after={after.get('disabled')}\n"
        )
    print(f"audit -> {ABORT_AUDIT}")
    print(f"resume later with: grs_jenkins.py resume {job}")


def cmd_resume(job):
    """Re-enable a job paused by cmd_pause (flips isDisabled back to False)."""
    jp = _job(job)
    jbase = _base_for(jp)
    before = _get(f"{jp}/api/json?tree=disabled", server=jbase)
    print(f"before: disabled={before.get('disabled')}")
    if not before.get("disabled"):
        print(f"{jp} is already enabled -- no-op")
        return
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op, server=jbase)
    req = urllib.request.Request(f"{jbase}/{jp}/enable", data=b"", method="POST")
    req.add_header("Authorization", f"Basic {AUTH}")
    req.add_header(crumb["crumbRequestField"], crumb["crumb"])
    try:
        status = op.open(req, timeout=15).status
        print("enable status:", status)
    except urllib.error.HTTPError as e:
        print(f"enable HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
        return
    after = _get(f"{jp}/api/json?tree=disabled", server=jbase)
    print(f"after: disabled={after.get('disabled')}")
    with open(ABORT_AUDIT, "a", encoding="utf-8") as f:
        f.write(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {jp} | mode=enable | before=True after={after.get('disabled')}\n"
        )
    print(f"audit -> {ABORT_AUDIT}")


def cmd_sync_pipeline_script(job, groovy_file):
    """Replace a job's cached CpsFlowDefinition <script> wholesale with the
    content of a local groovy file (full-file sync, unlike deploy-groovy's
    marker-anchored insert). Preserves every other part of config.xml
    (parameters, properties, sandbox flag). Uses _job() for alias resolution
    (so a _NEW_BASE_ALIAS_OVERRIDE entry like reg2 -> GRS-SYSTEMTEST-REG2
    is honored -- a prior one-off script that indexed ALIASES directly
    missed this and silently 404'd on reg2).
        sync-pipeline-script mac2-ssh C:/git/claude-resource/jenkins/DEV/GRS-SYSTEMTEST-MAC2-SSH.groovy
    """
    with open(groovy_file, "r", encoding="utf-8") as f:
        new_groovy = f.read()

    job_path = _job(job)
    jbase = _base_for(job_path)
    cfg = _get_raw(f"{job_path}/config.xml", server=jbase).decode("utf-8")
    print(f"[sync-pipeline-script] {job} -> {job_path} @ {jbase} ({len(cfg)} bytes)")

    pattern = re.compile(
        r'(<definition\s+class="org\.jenkinsci\.plugins\.workflow\.cps\.CpsFlowDefinition"[^>]*>\s*<script>)'
        r"([\s\S]*?)"
        r"(</script>)",
        re.DOTALL,
    )
    m = pattern.search(cfg)
    if not m:
        print("[error] could not find CpsFlowDefinition <script> block in config.xml")
        return 1
    old_script = (
        m.group(2).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    )
    print(f"  old script: {len(old_script)} chars; new script: {len(new_groovy)} chars")

    new_script_escaped = (
        new_groovy.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    new_cfg = cfg[: m.start(2)] + new_script_escaped + cfg[m.end(2) :]
    if new_cfg.lstrip().startswith("<?xml") and 'version="1.1"' in new_cfg[:60]:
        new_cfg = new_cfg.replace('version="1.1"', 'version="1.0"', 1)

    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op, server=jbase)
    post_data = new_cfg.encode("utf-8")
    req = urllib.request.Request(
        f"{jbase}/{job_path}/config.xml", data=post_data, method="POST"
    )
    req.add_header("Authorization", f"Basic {AUTH}")
    req.add_header(crumb["crumbRequestField"], crumb["crumb"])
    req.add_header("Content-Type", "application/xml;charset=UTF-8")
    try:
        with op.open(req, timeout=30) as r:
            print(f"[ok] POST config.xml -> HTTP {r.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[error] HTTP {e.code}: {body[:500]}")
        return 2

    verify_cfg = _get_raw(f"{job_path}/config.xml", server=jbase).decode("utf-8")
    m2 = pattern.search(verify_cfg)
    verify_script = (
        m2.group(2).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        if m2
        else ""
    )
    if verify_script == new_groovy:
        print(
            f"[ok] verify: live script now matches repo file ({len(verify_script)} chars)"
        )
        return 0
    print(
        f"[warn] verify: live script differs (verify={len(verify_script)} repo={len(new_groovy)})"
    )
    return 1


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

    jbase = _base_for(job_path)
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op, server=jbase)
    req = urllib.request.Request(
        f"{jbase}/{job_path}/config.xml",
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
    verify_cfg = _get_raw(f"{job_path}/config.xml", server=jbase).decode("latin-1")
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
    jbase = _base_for(new_path)
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op, server=jbase)
    url = (
        f"{jbase}/{folder_path}/createItem?name={urllib.parse.quote(new_name, safe='')}"
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


def cmd_node_create(name, remote_fs=None, label=None):
    """Create a brand-new inbound-agent node NAME (hudson.slaves.DumbSlave,
    JNLPLauncher, retention=Always). Use this to work around a node stuck
    offline with a controller-side ghost channel ("Connection was broken"
    that survives an agent restart AND a VM reboot) -- a fresh node name has
    no prior channel for the controller to hold onto. Needs Agent/Create
    (distinct from Agent/Disconnect, which can be denied even when this
    works). remote_fs defaults to C:/jenkins-agent (matches the existing
    LOCAL1/LOCAL2 agent layout); label defaults to NAME itself so a job can
    target it with `node(NAME)` without a separate label edit.

    RCA 2026-10-14: LOCAL1 (systest-localtest) and LOCAL2 (systest-local2)
    both went offline mid-session with offlineCauseReason "Connection was
    broken". Restarting the agent.jar process, rebooting the underlying VM,
    and relaunching with -webSocket (a different transport) all failed to
    clear it -- the jar cache re-synced fine each time, proving the AGENT
    side was never the problem. doDisconnect/toggleOffline/script console
    all 403'd for both available credentials (acheng, nsclientadmin --
    whoAmI showed authorities=["authenticated"] for both, i.e. neither has
    any admin role). A new node name is the one remaining self-serviceable
    option that doesn't need Agent/Disconnect."""
    remote_fs = remote_fs or "C:/jenkins-agent"
    label = label or name
    jbase = _base_for("")
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op, server=jbase)
    node_json = {
        "name": name,
        "nodeDescription": f"created by grs_jenkins.py node-create ({time.strftime('%Y-%m-%d')})",
        "numExecutors": "1",
        "remoteFS": remote_fs,
        "labelString": label,
        "mode": "EXCLUSIVE",
        "retentionStrategy": {
            "stapler-class": "hudson.slaves.RetentionStrategy$Always"
        },
        "launcher": {
            "stapler-class": "hudson.slaves.JNLPLauncher",
            "workDirSettings": {
                "disabled": False,
                "workDirPath": "",
                "internalDir": "remoting",
                "failIfWorkDirIsMissing": False,
            },
        },
        "nodeProperties": {"stapler-class-bag": "true"},
        "crumb": crumb["crumb"],
    }
    qs = urllib.parse.urlencode(
        {"name": name, "type": "hudson.slaves.DumbSlave", "json": json.dumps(node_json)}
    )
    url = f"{jbase}/computer/doCreateItem?{qs}"
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("Authorization", f"Basic {AUTH}")
    req.add_header(crumb["crumbRequestField"], crumb["crumb"])
    try:
        r = op.open(req, timeout=30)
        print(f"created node {name!r}: status={r.status}")
        print("next: grs_jenkins.py node-secret", name)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:800]
        sys.exit(f"node-create FAILED: HTTP {e.code} {e.reason}\n{body}")


def cmd_node_secret(name):
    """Print the inbound-agent connection secret for node NAME, parsed from
    its slave-agent.jnlp. Needed right after node-create to actually launch
    the agent on the target VM (-secret HEX_SECRET)."""
    jbase = _base_for("")
    req = urllib.request.Request(f"{jbase}/computer/{name}/jenkins-agent.jnlp")
    req.add_header("Authorization", f"Basic {AUTH}")
    try:
        body = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        sys.exit(
            f"node-secret FAILED: HTTP {e.code} {e.reason} -- try node-secret-legacy"
        )
    m = re.search(r"<argument>([0-9a-f]{40,})</argument>", body)
    if not m:
        sys.exit(f"node-secret: no secret found in response:\n{body[:500]}")
    print(m.group(1))


def cmd_node_disconnect(name, message=None):
    """POST computer/<name>/doDisconnect. Needs Agent/Disconnect -- kept here
    (not just a one-off Bash call) so the permission check itself is a
    reusable, auditable path instead of an inline script every time.

    The name can exist on both servers (see _node_servers): with no JENKINS_BASE set and the name on more than one, this
    REFUSES instead of picking the default server, because a disconnect aimed at the wrong controller does nothing to the
    node you meant. Set JENKINS_BASE to the server that hosts the lane (SystemTest lanes: http://10.136.208.217:8080)."""
    jbase = _base_for("")
    if not _FORCE_BASE:
        servers = [b for b, _d in _node_servers(name)]
        if len(servers) > 1:
            sys.exit(
                f"node-disconnect REFUSED: '{name}' exists on {', '.join(servers)}. Set JENKINS_BASE=<one of them> so the "
                "disconnect goes to the controller you mean."
            )
        if len(servers) == 1:
            jbase = servers[0]
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op, server=jbase)
    data = urllib.parse.urlencode(
        {"offlineMessage": message or "grs_jenkins.py node-disconnect"}
    ).encode()
    req = urllib.request.Request(
        f"{jbase}/computer/{name}/doDisconnect", data=data, method="POST"
    )
    req.add_header("Authorization", f"Basic {AUTH}")
    req.add_header(crumb["crumbRequestField"], crumb["crumb"])
    try:
        r = op.open(req, timeout=15)
        print(f"disconnect {name!r}: status={r.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        sys.exit(f"node-disconnect FAILED: HTTP {e.code} {e.reason}\n{body}")


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
            # No selfrunner artifact: LOCAL1 and MAC2-SSH stream pytest output into
            # the console (only LOCAL2/MAC2 detach with a self-runner), so the
            # evidence is in consoleText (2026-10-28 gap: this fallback was missing
            # and the CERT-04 build 103 report silently never came back).
            text = _get_raw(f"{jp}/{n}/consoleText").decode("utf-8", errors="replace")
            lines = text.splitlines()
            hits = [ln for ln in lines if re.search(pattern or SRLOG_VERDICT, ln)]
            print(f"-- console fallback ({len(lines)} lines, {len(hits)} match)")
            print("\n".join(ln[:240] for ln in (hits or lines)[-25:]))
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
    _refuse_flag_dest("artifacts", dest)
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


def cmd_artifactory(url, dest=None):
    """Download one file from Artifactory (any https URL) into C:/tmp and print its sha256.

    artifactory <url> [dest]

    Why this lives here: the raw-curl rule sends every artifact download through the allowlisted CLI, and this file is the
    only one. Jenkins artifacts have their own subcommands; a generic-playground package (a DEM agent installer, for example)
    is not archived by any build, so there was no path at all before this.
    """
    import hashlib

    if not str(url).lower().startswith("https://"):
        sys.exit("artifactory needs an https URL")
    _refuse_flag_dest("artifactory", dest)
    name = dest or os.path.join("C:/tmp", url.rstrip("/").split("/")[-1])
    req = urllib.request.Request(url, headers={"User-Agent": "grs_jenkins.py"})
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=600) as resp, open(name, "wb") as fh:
            status = getattr(resp, "status", 200)
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                digest.update(chunk)
                total += len(chunk)
    except urllib.error.HTTPError as exc:
        sys.exit(f"artifactory HTTP {exc.code} for {url}")
    print(f"http={status} bytes={total} sha256={digest.hexdigest()}")
    print(f"saved: {name}")


def cmd_releases(prefix=None, train="release"):
    """List the release/develop train folders on artifactory, or verify one release exists.

    releases                # every train in the release listing (135.1.27, 138.1.18, ...)
    releases release-138    # verdict + the matching train(s); exit 0 = present, 1 = N/A
    releases develop-144 develop   # same check against the DEVELOP listing (2026-11-24: develop-144 had gone)

    Why (2026-11-24): cmd_artifactory only DOWNLOADS a file, so "is release-N still on artifactory" had no
    allowlisted path — the release-132 N/A discovery (GRS-ANDY 67/68 RCA) was done with an inline urllib
    one-off, which is exactly the raw-curl sidestep the owner forbade. Fix the gap in the tool, not around it.
    """
    train = (train or "release").strip().lower()
    if train not in ("release", "develop"):
        sys.exit(f"releases: train must be 'release' or 'develop', got {train!r}")
    url = f"https://artifactory-rd.netskope.io/artifactory/client-builds/{train}/"
    req = urllib.request.Request(url, headers={"User-Agent": "grs_jenkins.py"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        sys.exit(f"releases: artifactory HTTP {exc.code} for {url}")
    names = []
    for href in re.findall(r'href="([^"]+)"', html):
        n = href.rstrip("/").split("/")[-1]
        if n and n[0].isdigit() and "." in n:
            names.append(n)
    names.sort(key=lambda v: [int(x) if x.isdigit() else 0 for x in v.split(".")])
    if prefix and str(prefix).split("-", 1)[-1].count(".") >= 2:
        # A full version like release-144.0.0 / develop-145.0.0: list the BUILD folders inside it, which is what a
        # 4-part --current_release (release-144.0.0.2880) needs in order to exist (2026-11-24).
        ver = str(prefix).split("-", 1)[-1]
        burl = f"https://artifactory-rd.netskope.io/artifactory/client-builds/{train}/{ver}/"
        try:
            with urllib.request.urlopen(urllib.request.Request(burl, headers={"User-Agent": "grs_jenkins.py"}), timeout=60) as resp:
                bhtml = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            sys.exit(f"releases: artifactory HTTP {exc.code} for {burl}")
        builds = sorted(
            n for n in (h.rstrip("/").split("/")[-1] for h in re.findall(r'href="([^"]+)"', bhtml))
            if n and n[0].isdigit() and n.isdigit()
        )
        print(f"{train}/{ver}: {len(builds)} build folder(s): {', '.join(builds) if builds else 'none'}")
        return
    if prefix:
        p = str(prefix).split("-", 1)[-1] if "-" in str(prefix) else str(prefix)
        hits = [n for n in names if n.split(".")[0] == p.split(".")[0]]
        if hits:
            print(f"release-{p}: PRESENT — train(s): {', '.join(hits)}")
        else:
            print(f"{train}-{p}: N/A — not in the listing ({len(names)} train(s): {', '.join(names)})")
            sys.exit(1)
        return
    print(f"{len(names)} {train} train(s) on artifactory client-builds/{train}/:")
    for n in names:
        print(f"  {n}")


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
    # With a pattern the output is a per-file count table plus the first few lines of each file, because one broad
    # pattern over a client log bundle returns hundreds of near-identical lines (453 for one build on 2026-10-24).
    # GRS_BACKUP_ALL=1 restores the old flat listing (200 lines).
    show_all = os.environ.get("GRS_BACKUP_ALL") == "1"
    per_file_cap = 5
    matches, per_file = [], []
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
                hits = [f"{full}:{i}: {line[:240]}" for i, line in enumerate(text.splitlines(), 1) if re.search(pattern, line)]
                if hits:
                    per_file.append((full, len(hits)))
                    matches.extend(hits if show_all else hits[:per_file_cap])
            else:
                matches.append(full)
    total = sum(n for _f, n in per_file) if pattern else len(matches)
    print(f"[{outdir}] {total} {'match' if pattern else 'file'}(es)")
    if pattern and not show_all:
        for f, n in per_file:
            print(f"  {n:>5}  {os.path.relpath(f, outdir)}")
        print(f"(first {per_file_cap} lines per file below; GRS_BACKUP_ALL=1 for the flat listing)")
    print("\n".join(matches[:200]))


def cmd_rename(job, new_name):
    """Rename a Jenkins job via /doRename (keeps build history — Jenkins moves
    the whole job dir). NEW must not already exist. Job ref = alias or
    DEV/job/NAME; new_name = the plain new job name (no path).
        rename localtest GRS-SYSTEMTEST-LOCAL1
    """
    import http.cookiejar

    jp = _job(job)
    jbase = _base_for(jp)
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = _crumb(op, server=jbase)
    url = f"{jbase}/{jp}/doRename?newName={urllib.parse.quote(new_name, safe='')}"
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("Authorization", f"Basic {AUTH}")
    req.add_header(crumb["crumbRequestField"], crumb["crumb"])
    try:
        r = op.open(req, timeout=30)
        print(f"renamed {jp} -> {new_name}: status={r.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        sys.exit(f"rename FAILED: HTTP {e.code} {e.reason}\n{body}")


def cmd_diagnose(job, num, extra=None):
    """The three reads a build's death needs before anyone blames a component (WI-0165).

        diagnose mac2 349 [EXTRA_PATTERN]

    1 CANCEL/TIMEOUT (did the budget kill it, "Timeout has been exceeded"), 2 LAST TIMESTAMPED LINE (how far it actually
    got), 3 CONTROL (do the same ERROR strings appear in a SUCCESS build of the same case -- shared lines are not a cause).
    Implemented in diagnose_build.py so this file keeps one entry point.
    """
    import diagnose_build  # noqa: PLC0415
    return diagnose_build.cmd_diagnose(job, num, extra)


CMDS = {
    "jobs": cmd_jobs,
    "diagnose": cmd_diagnose,
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
    "iterations": cmd_iterations,
    "verdict": cmd_verdict,
    "monitor": cmd_monitor,
    "running": cmd_running,
    "health": cmd_health,
    "trigger": cmd_trigger,
    "retrigger": cmd_retrigger,
    "replay": cmd_replay,
    "abort": cmd_abort,
    "pass-report": cmd_pass_report,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "artifacts": cmd_artifacts,
    "artifactory": cmd_artifactory,
    "releases": cmd_releases,
    "backup": cmd_backup,
    "srlog": cmd_srlog,
    "poll": cmd_poll,
    "node": cmd_node,
    "node-create": cmd_node_create,
    "node-secret": cmd_node_secret,
    "node-disconnect": cmd_node_disconnect,
    "preflight": cmd_preflight,
    "vm-check": cmd_vm_check,
    "dc-pick": cmd_dcpick,
    "approved-tenants": cmd_approved_tenants,
    "wake": cmd_wake,
    "waitnotify": cmd_waitnotify,
    "recover": cmd_recover,
    "vm-lock": cmd_vmlock,
    "create": cmd_create,
    "rename": cmd_rename,
    "delete": cmd_delete,
    "clone-configs": cmd_clone_configs,
    "deploy-groovy": cmd_deploy_groovy,
    "sync-pipeline-script": cmd_sync_pipeline_script,
    "chapter": cmd_chapter,
}


def main(argv):
    if not argv or argv[0] not in CMDS:
        print(__doc__)
        print("aliases:", ", ".join(f"{k}={v}" for k, v in ALIASES.items()))
        return 2
    if any(a in ("-h", "--help") for a in argv[1:]):  # WI-0079: -h after a command is help, not a job name or a pattern
        print(CMDS[argv[0]].__doc__ or __doc__)
        return 0
    try:
        CMDS[argv[0]](*argv[1:])
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        print(f"HTTP 404 for {e.url}\nunknown job or build number. Use an alias or a full path 'DEV/job/<NAME>'.")
        print("aliases:", ", ".join(f"{k}={v}" for k, v in ALIASES.items()))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
