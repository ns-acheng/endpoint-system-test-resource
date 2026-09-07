<!-- MIRROR NOTICE: point-in-time copy for onboarding/public reference. Canonical source (private repo): claude-resource/grs_tools/system_test_db/report_server_setup.md -->

# SystemTest live report server — setup & rebuild

Serves the same HTML `report.py` writes to a file, but on demand over HTTP, so
the page always reflects the current DB. **Source + this doc live in
claude-resource** (`grs_tools/system_test_db/`), so a host rebuild is one `git
clone` + the steps below — not tribal knowledge.

| what | value |
|---|---|
| Server source | `grs_tools/system_test_db/report_server.py` (stdlib only, no new deps) |
| Runs on | the Windows report/orchestration box — currently `10.136.210.243` |
| URL | `http://10.136.210.243:8080/report/140` (also `/report/141`, `/` index, `/healthz`) |
| Data source | results DB `10.136.218.74` via `~/.grs_db.conf` (same as db.py) |
| Auto-start | Task Scheduler `GRS-SystemTestDB-ReportServer` (at boot) **+ `GRS-SystemTestDB-ReportServerWatch` (`ensure`, every 10 min)** |
| Firewall | inbound TCP 8080 allow rule `GRS-Report-8080` |
| Operate it | `report_server.py status` / `restart` / `ensure` / `install-task` — **not** hand-typed schtasks + netstat + curl |

## Operate it with the tool (start here)

```bash
python C:/git/claude-resource/grs_tools/system_test_db/report_server.py status
python C:/git/claude-resource/grs_tools/system_test_db/report_server.py restart      # start + WAIT for /healthz
python C:/git/claude-resource/grs_tools/system_test_db/report_server.py install-task # boot task + watchdog
```

`status` checks the boot task (state + decoded last result), the watchdog task,
port 8080 listening, `/healthz` on **both** localhost and the LAN address, the
firewall rule, and the DB — exit 0 only when it is really serving to the LAN.
Everything below is the underlying mechanism, kept for a host rebuild; you do not
need to type it during an incident.

**Why the watchdog exists (2026-07-31 → 2026-08-14):** the boot task is
`ONSTART` only. The server process was terminated on 2026-07-31 (`Last Result`
`1073807364`) and, with no re-trigger and nothing probing the port, the dashboard
was dark for two weeks while every other DB check reported healthy. `db_health.py`
now consumes `report_server._status()`, so a dead dashboard alarms with the rest.

The report host and the DB host are **different machines**; Jenkins
(`10.136.208.148`) is a **third**. Nothing here needs to run on Jenkins — the
server only needs to reach the DB and bind a port on the report host.

---

## Prerequisites (already true on the current box)

1. `git clone` claude-resource to `C:\git\claude-resource`.
2. `~/.grs_db.conf` present with `GRS_DB_HOST=10.136.218.74` + `GRS_DB_PASSWORD`
   (see `reference_grs_db_vm.md`). Verify: `python grs_tools/system_test_db/db.py ping`.
3. `pip install psycopg2-binary` (report.py's only real dep).

## Step 1 — smoke-test in the foreground

```bash
python C:/git/claude-resource/grs_tools/system_test_db/report_server.py --host 127.0.0.1 --port 8899
# in another shell:
curl http://127.0.0.1:8899/healthz         # -> ok
curl -s http://127.0.0.1:8899/report/140 | head -c 200
# Ctrl-C to stop.
```

## Step 2 — open the Windows firewall for 8080 (LAN access)

```cmd
netsh advfirewall firewall add rule name="GRS-Report-8080" dir=in action=allow protocol=TCP localport=8080
```
Remove later with:
```cmd
netsh advfirewall firewall delete rule name="GRS-Report-8080"
```

## Step 3 — auto-start at boot (Task Scheduler)

`report_server.py install-task` does both registrations for you (boot task +
watchdog) and warns if the firewall rule is missing — prefer it. The raw commands
below are what it runs, kept so the mechanism is auditable.

Mirror the three DB tasks (same `acheng` account, survives reboot). `pythonw.exe`
(no console window) + boot trigger:

```cmd
schtasks /Create /F /TN "GRS-SystemTestDB-ReportServer" /SC ONSTART /RU acheng /RL LIMITED /TR "C:\Python311\pythonw.exe C:\git\claude-resource\grs_tools\system_test_db\report_server.py --host 0.0.0.0 --port 8080"
schtasks /Run /TN "GRS-SystemTestDB-ReportServer"
```

Verify it is listening and serving:
```cmd
schtasks /Query /TN "GRS-SystemTestDB-ReportServer"
powershell -Command "(Invoke-WebRequest http://localhost:8080/healthz).Content"
```

Stop / restart:
```cmd
schtasks /End /TN "GRS-SystemTestDB-ReportServer"     REM stop (task stays registered)
schtasks /Run /TN "GRS-SystemTestDB-ReportServer"     REM start again
```

Fully remove:
```cmd
schtasks /Delete /F /TN "GRS-SystemTestDB-ReportServer"
netsh advfirewall firewall delete rule name="GRS-Report-8080"
```

---

## Rebuild on a fresh host (host died / replaced)

1. Point the box's `~/.grs_db.conf` at the live DB (`10.136.218.74`) — step
   Prereq 2.
2. `pip install psycopg2-binary`.
3. Firewall rule — Step 2.
4. Register + start the task — Step 3.
5. If the box's **IP changed**, tell consumers the new `http://<newIP>:8080/`;
   nothing in the server hard-codes an IP (it binds `0.0.0.0`), so no code edit.

That is the whole server: one Python file + these four commands. There is no
database on the report host and no persisted state — it is a stateless view over
the DB, so losing the host loses nothing but the process.

## How "live" works

The server renders on each request (30 s per-release cache). The poll task
(`GRS-SystemTestDB-Poll`, every 5 min) ingests new builds, so a page refresh
after a build lands shows it within one poll interval — no manual regen. The
file-writing CLI (`/grs-report` skill / `db.py report`) still exists for
committed snapshots and is unaffected.

## Notes

- **Scale:** a single render is ~50–160 ms (measured); the cache absorbs
  refreshes and a handful of concurrent viewers. `ThreadingHTTPServer` handles
  concurrent requests. This is sized for a team-internal dashboard, not public
  traffic — for many simultaneous users, front it with a real WSGI server.
- **No auth:** LAN-internal, read-only, non-secret aggregate data. Do not expose
  it outside the `10.136.0.0/16` network without adding auth.
- Task registrations do NOT sync via git (see `SYNC.md`) — that is why Steps 2-3
  are re-run per host.
