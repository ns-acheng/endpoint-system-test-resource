#!/usr/bin/env python
"""report_servers_status.py -- one screen for THE report server: the results-DB VM.

    python report_servers_status.py            print the table; exit 1 when anything is wrong (usable from a scheduler)

  http://10.136.218.74:8080    the results-DB VM (systemd grs-report, beside PostgreSQL). The ONLY report server.

Checks: /healthz (200 + latency), a page timing each, the service state, and whether the clone of this repo on the VM is at the
sha of origin/main (update_report_vm.py moves it). Ssh account: REPORT_VM_USER / REPORT_VM_PASS (see update_report_vm.py).
"""

import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_report_vm as uv  # noqa: E402

NEW = f"http://{uv.HOST}:{uv.PORT}"
PAGES = ("/report/143", "/report/144", "/report/145", "/query")


def get(base, path, timeout=60):
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, body, time.perf_counter() - t
    except Exception as e:  # noqa: BLE001
        return type(e).__name__, "", time.perf_counter() - t


def main():
    bad = []
    print(f"{'server':<5} {'healthz':<8} {'ms':>5}  " + "  ".join(f"{p[:22]:<22}" for p in PAGES))
    code, _body, dt = get(NEW, "/healthz", 10)
    row = [f"{'NEW':<5} {str(code):<8} {dt * 1000:>5.0f} "]
    if code != 200:
        bad.append(f"healthz {code}")
    for p in PAGES:
        c2, _, d2 = get(NEW, p)
        row.append(f" {str(c2) + ' ' + format(d2 * 1000, '.0f') + ' ms':<22}")
        if c2 != 200:
            bad.append(f"{p} -> {c2}")
    print(" ".join(row))
    try:
        c = uv.connect()
        try:
            state = uv.run(c, "systemctl is-active grs-report")[1]
            vm, origin = uv.shas(c)
        finally:
            c.close()
        print(f"VM service grs-report: {state}; VM clone at {vm or '(none)'}, origin/main at {origin or '(unreadable)'}")
        if state != "active":
            bad.append(f"VM service {state}")
        if not vm or vm != origin:
            bad.append("VM clone is not at origin/main: run update_report_vm.py")
    except Exception as e:  # noqa: BLE001
        print("VM check failed:", type(e).__name__, e)
        bad.append("VM ssh check failed")
    print("STATUS:", "OK" if not bad else "PROBLEM: " + "; ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
