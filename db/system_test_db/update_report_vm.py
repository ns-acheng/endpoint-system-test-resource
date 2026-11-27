#!/usr/bin/env python
"""update_report_vm.py -- put the code of this repo on the report server: git pull on the results-DB VM, then restart.

    python update_report_vm.py            pull on the VM, point the service at the clone, restart, check /healthz
    python update_report_vm.py --check    read only: the sha the VM runs and the sha of origin/main

The report server (systemd service `grs-report`, host u2404-kenmin, 10.136.218.74) runs from a CLONE of this repo at
/home/nsadmin/grs_report_repo, so a change is on the live page when it is on origin/main and this script has run. Nothing is
copied from a workstation. Credentials are NOT in this file: the ssh account comes from REPORT_VM_USER / REPORT_VM_PASS
(default account nsadmin; the lab convention is that the password is the account name), the database settings stay in
/home/nsadmin/grs_report/grs_db.conf on the VM (outside this repo, never committed), the ai_chat data in /home/nsadmin/ai_chat_data.
Rollback: the old copy stays in /home/nsadmin/grs_report and the first unit file is saved as /home/nsadmin/grs-report.service.bak.
"""

import io
import os
import sys
import time

import paramiko

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HOST = os.environ.get("REPORT_VM_HOST", "10.136.218.74")
USER = os.environ.get("REPORT_VM_USER", "nsadmin")
PW = os.environ.get("REPORT_VM_PASS") or USER
HOSTNAME = "u2404-kenmin"
REPO_URL = "https://github.com/ns-acheng/endpoint-system-test-resource.git"
CLONE = "/home/nsadmin/grs_report_repo"
OLD_HOME = "/home/nsadmin/grs_report"  # holds grs_db.conf and lib/ (psycopg2); kept as the rollback copy of the code
CHAT_DATA = "/home/nsadmin/ai_chat_data"
PORT = int(os.environ.get("REPORT_PORT", "8080"))

UNIT = f"""[Unit]
Description=GRS SystemTest report server (next to the results DB), runs from a clone of endpoint-system-test-resource
After=network.target postgresql.service

[Service]
User={USER}
Environment=GRS_DB_CONF={OLD_HOME}/grs_db.conf
Environment=PYTHONPATH={OLD_HOME}/lib
Environment=AI_CHAT_DIR={CHAT_DATA}
WorkingDirectory={CLONE}/db/system_test_db
ExecStart=/usr/bin/python3 {CLONE}/db/system_test_db/report_server.py --host 0.0.0.0 --port {PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PW, timeout=20)
    return c


def run(c, cmd, timeout=300):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")
    return o.channel.recv_exit_status(), out.strip()


def shas(c):
    """(sha the VM clone is at or '', sha of origin/main as the VM sees it or '')."""
    vm = run(c, f"git -C {CLONE} rev-parse --short HEAD 2>/dev/null")[1]
    origin = run(c, f"timeout 30 git ls-remote {REPO_URL} refs/heads/main 2>/dev/null | cut -c1-7")[1]
    return vm, origin


def main(argv):
    if argv and argv[0] not in ("--check", "-h", "--help"):
        print(__doc__)
        return 2
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    c = connect()
    try:
        assert run(c, "hostname")[1] == HOSTNAME, "unexpected VM identity"
        if argv and argv[0] == "--check":
            vm, origin = shas(c)
            print(f"VM clone at {vm or '(none)'}, origin/main at {origin or '(unreadable)'}: {'in sync' if vm and vm == origin else 'NOT in sync'}")
            return 0 if vm and vm == origin else 1
        if run(c, f"test -d {CLONE}/.git && echo yes || echo no")[1] != "yes":
            code, out = run(c, f"git clone -q {REPO_URL} {CLONE} && echo cloned")
            print("clone:", out)
            if code != 0:
                return 1
        code, out = run(c, f"git -C {CLONE} pull --ff-only -q && git -C {CLONE} rev-parse --short HEAD")
        print("pull:", out)
        if code != 0:
            return 1
        run(c, f"test -e /home/nsadmin/grs-report.service.bak || cp /etc/systemd/system/grs-report.service /home/nsadmin/grs-report.service.bak")
        sftp = c.open_sftp()
        with sftp.open("/tmp/grs-report.service", "w") as f:
            f.write(UNIT)
        print("unit:", run(c, "sudo mv /tmp/grs-report.service /etc/systemd/system/grs-report.service && sudo systemctl daemon-reload && echo written")[1])
        run(c, "sudo systemctl enable grs-report >/dev/null 2>&1; sudo systemctl restart grs-report")
        time.sleep(5)
        print("service:", run(c, "systemctl is-active grs-report")[1])
        print("healthz:", run(c, f"curl -s -m 10 http://127.0.0.1:{PORT}/healthz")[1])
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
