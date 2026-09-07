#!/usr/bin/env python
"""MIRROR NOTICE (added for endpoint-system-test-resource): this is a point-in-time copy for onboarding/public reference.
Canonical source (private repo): claude-resource/grs_tools/system_test_db/report_server.py -- do not edit this copy directly; changes land upstream first, then get re-synced here.
"""

"""Live HTTP front-end for the SystemTest results report.

Wraps report.build_report() in a stdlib http.server so the same HTML the CLI
writes to a file is served on demand, always reflecting the current DB (the poll
task ingests new builds every 5 min, so the page is live with no extra work).

    GET  /                        index: links to every release found in the DB
    GET  /report/<rel>            the report for that release (140, r140, release-140)
    GET  /report/<rel>/<tenant>   same report, filtered to one tenant (run.tenant), e.g. 1457
    GET  /report/<rel>?platform=&tenant=   same filters via the page's own dropdowns
                                            (wins over the legacy suffix/path-segment forms
                                            above, which stay supported for old bookmarks)
    GET  /healthz                 "ok" + DB reachability (200 healthy / 503 if DB down)

Operating the server is part of the tool, not a shell recipe to remember:

    python report_server.py status        # task state + port + healthz + LAN + firewall + DB
    python report_server.py restart       # (re)start the boot task, wait until it serves
    python report_server.py ensure        # no-op when healthy, restart when not (watchdog body)
    python report_server.py install-task  # register the boot task AND the ensure watchdog

No new dependencies (stdlib only). Connection + release list come from the same
dbconn.py / DB as db.py and report.py — no credentials here. A short in-memory
cache (default 30 s per release) keeps a browser refresh or a couple of viewers
from re-querying every hit; the CLI report is unaffected.

Run:
    python report_server.py [--host 0.0.0.0] [--port 8080] [--cache 30]

Bind 0.0.0.0 to serve the LAN (open the Windows firewall for the port — see
report_server_setup.md); 127.0.0.1 for localhost-only. Setup + rebuild steps and
the auto-start task live in report_server_setup.md, kept beside this file so a
host rebuild is one document, not tribal knowledge.
"""

import argparse
import io
import os
import re
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dbconn  # noqa: E402
import query  # noqa: E402
import report  # noqa: E402

# release label -> (rendered_at_epoch, html). Trimmed implicitly: at most one
# entry per release, and releases are few.
_CACHE = {}
_CACHE_TTL = 30.0
_TOP = 10


def _code_mtime():
    """Newest mtime across the modules whose code this process has LOADED.

    report.py is imported once at start-up, so a long-lived server keeps serving
    the layout it booted with. Comparing this snapshot (published by /healthz)
    with the files on disk is what turns "the dashboard is missing my column"
    from a human observation into a `status` line.
    """
    newest = 0.0
    for mod in ("report.py", "report_server.py", "query.py"):
        try:
            newest = max(newest, os.path.getmtime(os.path.join(_HERE, mod)))
        except OSError:
            pass
    return newest


_HERE = os.path.dirname(os.path.abspath(__file__))
# Snapshot at import time: this is the code the process is actually running.
_CODE_MTIME = _code_mtime()


def _norm_release(arg):
    """Accept 140 / r140 / release-140 / release-140-int -> release-140[-int].
    `isalnum()` alone would reject any hyphenated suffix (e.g. the "-int"
    publish-filtered snapshot introduced 2026-08-21) and 400 a real release,
    so the token check is alnum-or-hyphen instead. Still rejects anything with
    spaces/slashes/quotes etc. (defends the DB query and the cache key)."""
    s = unquote(arg or "").strip().lower()
    if s.startswith("release-"):
        s = s[len("release-") :]
    elif s.startswith("r") and s[1:].isdigit():
        s = s[1:]
    if not s or not re.match(r"^[a-z0-9-]+$", s):
        return None
    return f"release-{s}"


def _norm_tenant(arg):
    """Validate the optional /report/<rel>/<tenant> path segment.

    run.tenant is free-text (ndjson tenant, e.g. "1457"); the query is already
    parameterized (no injection risk), this just keeps a junk path segment from
    reaching the DB as a real filter and rejects it as a 400 instead of quietly
    matching zero rows.
    """
    s = unquote(arg or "").strip()
    if not s or not re.match(r"^[A-Za-z0-9_.-]{1,64}$", s):
        return None
    return s


def _norm_platform(arg):
    """Validate an explicit ?platform= query value against report._PLATFORMS --
    same whitelist split_platform uses for the legacy -mac/-windows suffix, not
    a second list that could drift out of sync with it."""
    s = unquote(arg or "").strip().lower()
    if not s:
        return None
    return s if s in report._PLATFORMS else False  # False = present but invalid


def _query_filters(full_path):
    """?platform=&tenant= from the RAW request path (do_GET's local `path` var
    already had the query string stripped off before this is called -- same
    reason query.selected_from_query re-reads self.path instead of relying on
    that stripped copy). Returns (platform, tenant, error); error is a string
    when a value was present but failed validation, so the caller 400s instead
    of silently matching zero rows.
    """
    if "?" not in full_path:
        return None, None, None
    qs = parse_qs(full_path.split("?", 1)[1])
    platform = None
    if qs.get("platform"):
        platform = _norm_platform(qs["platform"][0])
        if platform is False:
            return None, None, "bad platform"
    tenant = None
    if qs.get("tenant"):
        tenant = _norm_tenant(qs["tenant"][0])
        if tenant is None:
            return None, None, "bad tenant"
    return platform, tenant, None


def _list_releases():
    """Release labels present in the DB, newest-ish first. Empty list on error
    (the index still renders, just without links)."""
    conn = dbconn.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT label FROM release ORDER BY label DESC")
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _list_platforms():
    """Distinct run.platform values ever ingested, e.g. ['mac','windows'].

    Used for the index's per-release platform shortcut links. Global (not
    per-release) on purpose: a platform link is offered even for a release
    with zero runs of it yet (owner 2026-08-24: release-142-mac should be
    ready to click before release-142 has any mac data), not only once data
    shows up — report.build_report renders an honest empty table either way.
    """
    conn = dbconn.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT platform FROM test_run WHERE platform IS NOT NULL ORDER BY platform"
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _render_cached(release, tenant=None, platform=None):
    key = (release, platform, tenant)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and (now - hit[0]) < _CACHE_TTL:
        return hit[1]
    html = report.build_report(release, _TOP, tenant, platform)
    _CACHE[key] = (now, html)
    return html


# Lucide "chart-column" (ISC licensed) — inline SVG, no external CDN, so the
# server stays self-contained. currentColor makes it inherit the text colour.
_ICON_PATHS = '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>'
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="#6ea8fe" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    f"{_ICON_PATHS}</svg>"
)
# data: URI so the tab icon needs no extra request; also served at /favicon.svg
# (report.py pages link there) and /favicon.ico (browsers probe it blindly).
_FAVICON_LINK = (
    '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,'
    + _FAVICON_SVG.replace("#", "%23")
    .replace('"', "'")
    .replace("<", "%3C")
    .replace(">", "%3E")
    + '">'
)
_TITLE_ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="30" height="30" viewBox="0 0 24 24" '
    'fill="none" stroke="#6ea8fe" stroke-width="2" stroke-linecap="round" '
    f'stroke-linejoin="round" style="vertical-align:-5px;margin-right:10px">{_ICON_PATHS}</svg>'
)

_INDEX_CSS = (
    "body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1420;"
    "color:#e6e9ef;margin:0;padding:40px}h1{font-weight:600}a{color:#6ea8fe;"
    "text-decoration:none;font-size:18px}li{margin:8px 0}.muted{color:#8b93a7;font-size:14px}"
    ".plat{font-size:13px;color:#8b93a7;margin-left:10px;border:1px solid #2a3142;"
    "border-radius:10px;padding:2px 8px}.plat:hover{color:#6ea8fe;border-color:#6ea8fe}"
)


def _index_html(releases, platforms):
    def _row(r):
        # Display + link with the short form everywhere (owner 2026-08-27:
        # showing "release-142" anywhere is what confuses people, not the
        # short form) -- _norm_release still accepts the long form on input,
        # this only changes what the index GENERATES.
        short = report.short_release(r)
        # Skip the shortcut on a release label that is ITSELF already a
        # platform view (none exist today, but a future DB write of e.g.
        # "release-142-mac" as a real label must not render "-mac-mac").
        if report.split_platform(r)[1]:
            return f'<li><a href="/report/{short}">{report.esc(short)}</a></li>'
        plats = "".join(
            f' <a class="plat" href="/report/{short}-{p}">{report.esc(p)} only</a>'
            for p in platforms
        )
        return f'<li><a href="/report/{short}">{report.esc(short)}</a>{plats}</li>'

    links = (
        "".join(_row(r) for r in releases)
        or '<li class="muted">no releases in the DB yet</li>'
    )
    return (
        f'<!doctype html><html lang="en"><head><meta charset="UTF-8">{_FAVICON_LINK}'
        f"<title>SystemTest reports</title><style>{_INDEX_CSS}</style></head><body>"
        f"<h1>{_TITLE_ICON}SystemTest results — live reports</h1>"
        f'<p class="muted">Rendered from the results DB on each request '
        f"(cache {int(_CACHE_TTL)}s). New builds appear within the poll interval.</p>"
        f'<p><a href="/query">Query latest PASS results by test case, across all 5 lanes &rarr;</a></p>'
        f"<ul>{links}</ul></body></html>"
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "GRSReport/1.0"

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/":
                try:
                    rels = _list_releases()
                except Exception:
                    rels = []
                try:
                    plats = _list_platforms()
                except Exception:
                    plats = []
                self._send(200, _index_html(rels, plats))
                return

            if path in ("/favicon.svg", "/favicon.ico"):
                self._send(200, _FAVICON_SVG, "image/svg+xml")
                return

            if path == "/healthz":
                try:
                    dbconn.connect().close()
                    # Report the report.py this PROCESS is running on. The server
                    # renders in-process, so an edit to report.py changes nothing
                    # here until a restart — 2026-08-14 the new gate-breakdown
                    # columns were live in the CLI HTML and invisible on the
                    # dashboard for exactly that reason. Publishing the loaded
                    # mtime lets `status` say "stale code" instead of a human
                    # having to notice a missing column.
                    self._send(
                        200, f"ok code={_CODE_MTIME:.0f}\n", "text/plain; charset=utf-8"
                    )
                except Exception as e:
                    self._send(
                        503, f"db-unreachable: {e}\n", "text/plain; charset=utf-8"
                    )
                return

            if path == "/query":
                try:
                    selected = query.selected_from_query(self.path)
                    self._send(200, query.build_query_page(selected))
                except Exception:
                    traceback.print_exc()
                    self._send(
                        500,
                        "query page failed (see server log)\n",
                        "text/plain; charset=utf-8",
                    )
                return

            if path.startswith("/report/"):
                segs = path[len("/report/") :].split("/", 1)
                release = _norm_release(segs[0])
                if not release:
                    self._send(400, "bad release\n", "text/plain; charset=utf-8")
                    return
                tenant = None
                if len(segs) > 1 and segs[1]:
                    tenant = _norm_tenant(segs[1])
                    if not tenant:
                        self._send(400, "bad tenant\n", "text/plain; charset=utf-8")
                        return
                # ?platform=&tenant= (the filter-bar dropdowns) win over the
                # legacy suffix/path-segment forms -- self.path still carries
                # the query string that `path` (above) already stripped off.
                platform = None
                q_platform, q_tenant, q_err = _query_filters(self.path)
                if q_err:
                    self._send(400, f"{q_err}\n", "text/plain; charset=utf-8")
                    return
                if q_platform:
                    platform = q_platform
                if q_tenant:
                    tenant = q_tenant
                try:
                    self._send(200, _render_cached(release, tenant, platform))
                except Exception:
                    # A bad release label reaches build_report and yields an
                    # empty-but-valid report, so an exception here is a real
                    # server/DB fault worth surfacing (and logging), not a 404.
                    traceback.print_exc()
                    self._send(
                        500,
                        "report generation failed (see server log)\n",
                        "text/plain; charset=utf-8",
                    )
                return

            self._send(404, "not found\n", "text/plain; charset=utf-8")
        except BrokenPipeError:
            pass  # client navigated away mid-response; nothing to do

    def log_message(self, fmt, *args):
        # One concise line to stdout (Task Scheduler / console); no client host spam.
        sys.stdout.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))


# --- operations: status / restart / ensure / install-task ------------------
# Every check the setup doc used to spell out as a cmd line lives here instead.
# A verification step that exists only as a remembered shell recipe is a step
# that gets skipped or run against the wrong host (ai_new_comer ledger 48) —
# and this server proved it: the process was terminated 2026-07-31 and, with an
# ONSTART-only trigger and nothing watching the port, the dashboard stayed dark
# for two weeks without a single alarm.
TASK_NAME = "GRS-SystemTestDB-ReportServer"
WATCH_TASK_NAME = "GRS-SystemTestDB-ReportServerWatch"
WATCH_EVERY_MIN = 10
FIREWALL_RULE = "GRS-Report-8080"
# Network the report consumers sit on (report_server_setup.md "do not expose
# outside 10.136.0.0/16"). Used to pick the right local address to advertise.
_CONSUMER_NET = "10.136."
_PYTHONW = r"C:\Python311\pythonw.exe"
_PYTHON = r"C:\Python311\python.exe"
# schtasks "Last Result" values seen in practice; anything else prints raw.
_TASK_RESULT_MEANING = {
    0: "ok",
    1: "generic failure",
    267009: "still running",
    267011: "never ran",
    267014: "terminated by user",
    1073807364: "terminated (process killed / host shut down)",
}


def _sh(cmd, timeout=30):
    """(rc, combined output). Never raises; a missing binary is rc=-1."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=timeout
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, f"{type(e).__name__}: {e}"


def _lan_ip():
    """This host's address as report consumers reach it ('' if undetermined).

    Prefer an address on the documented consumer network (10.136.0.0/16) over
    whatever the default route picks: measured on this box, the route-derived
    address is the Netskope virtual adapter (198.18.x) because steering owns the
    default route, and probing THAT proves nothing about LAN reachability.
    """
    cands = []
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = res[4][0]
            if ip not in cands:
                cands.append(ip)
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet sent; just resolves the interface
        routed = s.getsockname()[0]
    except OSError:
        routed = ""
    finally:
        s.close()
    if routed and routed not in cands:
        cands.append(routed)
    for ip in cands:
        if ip.startswith(_CONSUMER_NET):
            return ip
    return routed or (cands[0] if cands else "")


def _ps(script):
    """One PowerShell one-liner -> stdout lines. Arg-list call (no shell), so the
    `$_` pipeline variables need no bash escaping."""
    rc, out = _sh(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    return [ln.strip() for ln in out.splitlines() if ln.strip()] if rc == 0 else []


def _procs():
    """[(pid, started, cmdline)] for every running report_server.py process.

    `schtasks /End` ends the TASK; if the python child survives it keeps the port
    and the next `/Run` instance dies on bind — leaving the old code serving while
    every check says 200. That is why the restart path needs process identity, not
    just a healthy probe.
    """
    lines = _ps(
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -like '*report_server*' } | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.CreationDate.ToString('s'))|$($_.CommandLine)\" }"
    )
    out = []
    for ln in lines:
        pid, _, rest = ln.partition("|")
        started, _, cmd = rest.partition("|")
        if pid.isdigit():
            out.append((int(pid), started, cmd))
    return out


def _port_owner(port):
    """PID listening on `port`, or None."""
    for ln in _ps(
        f"Get-NetTCPConnection -LocalPort {port} -State Listen "
        "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess"
    ):
        if ln.isdigit():
            return int(ln)
    return None


def _code_verdict(healthz_body, disk_mtime):
    """(running_mtime, stale) parsed out of a /healthz body.

    stale is None when the body carries no `code=` field — i.e. the running
    server was started before this check existed, so the honest answer is
    UNKNOWN, not "fine" (a false "current" is how the missing-column report
    stayed invisible in the first place). 1s slack absorbs mtime rounding.

    Deliberately NOT folded into `serving`: a server on last week's code is
    still serving, and db_health (which imports _status) must not start alarming
    daily just because someone edited report.py.
    """
    m = re.search(r"code=(\d+)", healthz_body or "")
    if not m:
        return None, None
    running = float(m.group(1))
    return running, disk_mtime > running + 1


def _probe(url, timeout=5):
    """(http_code, first line of body). code None = no connection."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read(200).decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        return e.code, e.read(200).decode("utf-8", "replace").strip()
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"


def _port_listening(port):
    for fam, addr in ((socket.AF_INET, ("127.0.0.1", port)),):
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(2)
        try:
            return s.connect_ex(addr) == 0
        finally:
            s.close()
    return False


def _task_info(name):
    """Parsed `schtasks /Query /FO LIST /V` for one task, or None if absent."""
    rc, out = _sh(["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"])
    if rc != 0:
        return None
    info = {}
    for line in out.splitlines():
        k, sep, v = line.partition(":")
        if sep:
            info.setdefault(k.strip(), v.strip())
    return info or None


def _firewall_open(rule=FIREWALL_RULE):
    rc, out = _sh(["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule}"])
    return rc == 0 and "Enabled:" in out and "Yes" in out


def _db_ok():
    try:
        dbconn.connect().close()
        return True, ""
    except Exception as e:  # psycopg2 errors are not OSError
        return False, (
            str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        )


def _status(port):
    """Everything a human would check, as data. serving = the only verdict."""
    lan = _lan_ip()
    local_code, local_body = _probe(f"http://127.0.0.1:{port}/healthz")
    lan_code, lan_body = (None, "no LAN ip")
    if lan:
        lan_code, lan_body = _probe(f"http://{lan}:{port}/healthz")
    task = _task_info(TASK_NAME)
    watch = _task_info(WATCH_TASK_NAME)
    db_ok, db_err = _db_ok()
    disk_mtime = _code_mtime()
    running_mtime, stale = _code_verdict(local_body, disk_mtime)
    return {
        "port": port,
        "lan_ip": lan,
        "listening": _port_listening(port),
        "local_healthz": (local_code, local_body),
        "lan_healthz": (lan_code, lan_body),
        "task": task,
        "watch_task": watch,
        "firewall": _firewall_open(),
        "db_ok": db_ok,
        "db_err": db_err,
        "code_running_mtime": running_mtime,
        "code_disk_mtime": disk_mtime,
        "code_stale": stale,
        # LAN reachability is what consumers actually need; localhost alone is
        # not "serving" (a firewall/bind regression looks healthy from here).
        "serving": local_code == 200 and (lan_code == 200 if lan else True),
    }


def _print_status(st):
    def mark(ok):
        return "OK  " if ok else "FAIL"

    print(
        f"{mark(st['serving'])} report server on port {st['port']} (host {st['lan_ip'] or '?'})"
    )
    print(f"  listening 127.0.0.1:{st['port']} : {st['listening']}")
    print(
        f"  healthz local                : {st['local_healthz'][0]} {st['local_healthz'][1]}"
    )
    print(
        f"  healthz LAN                  : {st['lan_healthz'][0]} {st['lan_healthz'][1]}"
    )
    print(
        f"  firewall rule {FIREWALL_RULE}  : {'open' if st['firewall'] else 'MISSING/disabled'}"
    )
    print(
        f"  results DB                   : {'reachable' if st['db_ok'] else 'UNREACHABLE ' + st['db_err']}"
    )
    if st["code_stale"] is None:
        code = "unknown (running server predates the check — restart to enable it)"
    elif st["code_stale"]:
        age_min = (st["code_disk_mtime"] - st["code_running_mtime"]) / 60.0
        code = (
            f"STALE — report.py on disk is {age_min:.0f} min newer than the running "
            f"process; the dashboard shows the OLD layout. Fix: report_server.py restart"
        )
    else:
        code = "current (process loaded the report.py on disk)"
    print(f"  rendering code               : {code}")
    for label, name, info in (
        ("boot task", TASK_NAME, st["task"]),
        ("watchdog", WATCH_TASK_NAME, st["watch_task"]),
    ):
        if not info:
            print(f"  {label} {name}: NOT REGISTERED")
            continue
        raw = info.get("Last Result", "?")
        try:
            meaning = _TASK_RESULT_MEANING.get(int(raw), "unmapped code")
        except ValueError:
            meaning = "unparsed"
        print(
            f"  {label} {name}: status={info.get('Status', '?')} "
            f"last_run={info.get('Last Run Time', '?')} last_result={raw} ({meaning})"
        )
    if st["serving"]:
        print(f"  URL: http://{st['lan_ip']}:{st['port']}/  (e.g. /report/release-141)")


def cmd_status(port):
    st = _status(port)
    _print_status(st)
    return 0 if st["serving"] else 1


def _wait_serving(port, wait_s):
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if _probe(f"http://127.0.0.1:{port}/healthz")[0] == 200:
            return True
        time.sleep(1)
    return False


def cmd_restart(port, wait_s=30):
    """Start (or restart) the boot task and WAIT for it to actually serve.

    `schtasks /Run` returning 0 only means the scheduler accepted the request —
    it is not evidence the server bound the port (the push-Success-is-not-applied
    trap). Nothing is reported until /healthz answers.
    """
    if _task_info(TASK_NAME) is None:
        print(f"[restart] {TASK_NAME} not registered — registering it first")
        rc = cmd_install_task(port)
        if rc:
            return rc
    before = _port_owner(port)
    print(
        f"[restart] serving pid before: {before if before else '(nothing listening)'}"
    )
    _sh(["schtasks", "/End", "/TN", TASK_NAME])  # no-op when not running
    # `/End` ends the TASK; a surviving python child would keep the port and the
    # new instance would die on bind, leaving OLD code serving while every probe
    # says 200 (that is how a "restart OK" hid the old report layout on
    # 2026-08-14). Wait for the port to actually free, then kill only our own
    # leftover process if it is still holding it.
    for _ in range(int(wait_s)):
        if _port_owner(port) is None:
            break
        time.sleep(1)
    held = _port_owner(port)
    if held is not None:
        if any(pid == held for pid, _, _ in _procs()):
            print(
                f"[restart] pid {held} survived /End and still holds :{port} — killing it"
            )
            _sh(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"Stop-Process -Id {held} -Force",
                ]
            )
        else:
            print(
                f"[restart] FAIL — :{port} is held by pid {held}, which is NOT a "
                "report_server process. Refusing to kill someone else's process."
            )
            return 1
    rc, out = _sh(["schtasks", "/Run", "/TN", TASK_NAME])
    print(f"[restart] schtasks /Run rc={rc} {out.strip()[:120]}")
    if not _wait_serving(port, wait_s):
        print(f"[restart] FAIL — nothing serving on 127.0.0.1:{port} after {wait_s}s")
        _print_status(_status(port))
        return 1
    after = _port_owner(port)
    print(f"[restart] serving pid after:  {after if after else '(unknown)'}")
    if before is not None and after == before:
        print(
            "[restart] FAIL — same pid is serving: the old process never died, so "
            "any code change is NOT live."
        )
        _print_status(_status(port))
        return 1
    st = _status(port)
    _print_status(st)
    if st["code_stale"]:
        # Reached only if report.py was edited DURING the restart, or the task
        # launches a different copy of the tree than the one just edited.
        print(
            "[restart] FAIL — the fresh process is still rendering older code than "
            "the files on disk (check the task's script path)."
        )
        return 1
    return 0 if st["serving"] else 1


def cmd_ensure(port):
    """Watchdog body: silent when healthy, restart + report when not."""
    if _probe(f"http://127.0.0.1:{port}/healthz")[0] == 200:
        return 0
    print(f"[ensure] not serving on port {port} — restarting")
    return cmd_restart(port)


def cmd_install_task(port):
    """Register the ONSTART server task AND the every-N-min ensure watchdog.

    ONSTART alone is why the dashboard stayed dark: the process was terminated
    and nothing re-ran it short of a reboot. The watchdog is a no-op when the
    server answers, so it costs one HTTP probe per interval.
    """
    if os.name != "nt":
        print("[install-task] Windows-only (schtasks)")
        return 2
    me = os.path.abspath(__file__)
    rc1, out1 = _sh(
        [
            "schtasks",
            "/Create",
            "/F",
            "/TN",
            TASK_NAME,
            "/SC",
            "ONSTART",
            "/RU",
            os.environ.get("USERNAME", "acheng"),
            "/RL",
            "LIMITED",
            "/TR",
            f'"{_PYTHONW}" "{me}" --host 0.0.0.0 --port {port}',
        ]
    )
    print(f"[install-task] {TASK_NAME} rc={rc1} {out1.strip()[:120]}")
    rc2, out2 = _sh(
        [
            "schtasks",
            "/Create",
            "/F",
            "/TN",
            WATCH_TASK_NAME,
            "/SC",
            "MINUTE",
            "/MO",
            str(WATCH_EVERY_MIN),
            "/RU",
            os.environ.get("USERNAME", "acheng"),
            "/RL",
            "LIMITED",
            "/TR",
            f'"{_PYTHON}" "{me}" ensure --port {port}',
        ]
    )
    print(
        f"[install-task] {WATCH_TASK_NAME} (every {WATCH_EVERY_MIN}min) rc={rc2} {out2.strip()[:120]}"
    )
    if not _firewall_open():
        print(
            f"[install-task] firewall rule {FIREWALL_RULE} missing — see report_server_setup.md step 2"
        )
    return 0 if rc1 == 0 and rc2 == 0 else 1


def main(argv):
    ops = {
        "status": cmd_status,
        "restart": cmd_restart,
        "ensure": cmd_ensure,
        "install-task": cmd_install_task,
    }
    if argv and argv[0] in ops:
        op = argv[0]
        ap = argparse.ArgumentParser(prog=f"report_server.py {op}")
        ap.add_argument("--port", type=int, default=8080)
        a = ap.parse_args(argv[1:])
        return ops[op](a.port)

    ap = argparse.ArgumentParser(description="Serve SystemTest reports live over HTTP.")
    ap.add_argument(
        "--host",
        default="0.0.0.0",
        help="bind address (0.0.0.0 = LAN, 127.0.0.1 = localhost)",
    )
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument(
        "--cache",
        type=float,
        default=30.0,
        help="per-release cache TTL seconds (default 30)",
    )
    ap.add_argument(
        "--top",
        type=int,
        default=10,
        help="cap for large tables (passed to build_report)",
    )
    a = ap.parse_args(argv)

    global _CACHE_TTL, _TOP
    _CACHE_TTL = a.cache
    _TOP = a.top

    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    print(
        f"[report-server] serving on http://{a.host}:{a.port}  (cache {a.cache}s, top {a.top})"
    )
    print(f"[report-server]   index    http://{a.host}:{a.port}/")
    print(f"[report-server]   report   http://{a.host}:{a.port}/report/140")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[report-server] stopping")
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    # Under pythonw.exe (how the boot task runs it) there is no console, so
    # sys.stdout/stderr are None — wrapping .buffer would AttributeError and exit
    # the server before it ever binds. Only wrap a real stdout.
    if sys.stdout is not None:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    else:
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
