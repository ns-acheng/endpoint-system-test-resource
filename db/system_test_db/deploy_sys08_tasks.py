"""Deploy the three results-DB scheduled tasks (Backup, Harvest, Health) to SYS-08 (plan: task_docs/db_harvest_move/PLAN.md).

    python deploy_sys08_tasks.py [--dry-run] [--no-conf] [--store] [--host 10.136.212.15] [--expect-host AUSTIN-SYS-08]

Copies, over ssh/sftp, only into C:\\grs_tasks\\ on SYS-08 (WSL path /mnt/c/grs_tasks):
  grs_tools\\...            the files of the three tasks (FILES) and the ingest closure (heartbeat.INGEST_FILES)
  snapshot\\golden_regression\\...   a read-only snapshot of three GRS files, so ingest.py needs no GRS clone
  launch.py, store_sync.py   the SYS-08 launcher and the store mirror (system_test_db/sys08/)
  MANIFEST.txt               sha256 of every file and the claude-resource sha
  .grs_db.conf               unless --no-conf: a copy of ~/.grs_db.conf (the content is never printed)
  systest_db_store\\...      with --store: the current cold store (one-time seed)
claude-resource is NOT cloned there and nothing is pushed from there. The box is refused unless its computer name
matches --expect-host. Nothing is deleted.
"""

import argparse
import hashlib
import io
import os
import posixpath
import subprocess
import sys

import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
CR_ROOT = os.path.dirname(os.path.dirname(HERE))  # claude-resource
GRS_ROOT = os.environ.get("GRS_REPO_ROOT_SRC", r"C:\git\nsclient_golden_regression_suite")
BASE = "/mnt/c/grs_tasks"
USER = os.environ.get("GRS_SSH_USER", "nsadmin")
PASSWORD = os.environ.get("GRS_SSH_PASS") or USER
PS = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

sys.path.insert(0, HERE)
import heartbeat  # noqa: E402  (INGEST_FILES is the one list of ingest files)

FILES = ["grs_tools/" + f for f in heartbeat.INGEST_FILES] + [
    "grs_tools/nowindow.py",
    "grs_tools/system_test_db/schema.sql",
    "grs_tools/system_test_db/harvest.py",
    "grs_tools/systest_db_backup_daily.py",
    "grs_tools/systest_db_harvest_daily.py",
    "grs_tools/db_health.py",
]
SYS08 = {"launch.py": "launch.py", "store_sync.py": "store_sync.py"}  # file in sys08/ -> name under BASE
GRS_SNAPSHOT = [
    "golden_regression/data/global_data.py",
    "golden_regression/tests/SystemTest/test_data.py",
    "golden_regression/workflows/system_test/health_gate.py",
]
EMPTY_INITS = [
    "golden_regression/__init__.py",
    "golden_regression/tests/__init__.py",
    "golden_regression/tests/SystemTest/__init__.py",
]


def _git_sha(root):
    p = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True)
    return p.stdout.strip() or "unknown"


def _mkdirs(sftp, remote_dir):
    cur = ""
    for part in remote_dir.strip("/").split("/"):
        cur += "/" + part
        try:
            sftp.stat(cur)
        except IOError:
            sftp.mkdir(cur)


def _put(sftp, data, remote):
    _mkdirs(sftp, posixpath.dirname(remote))
    sftp.putfo(io.BytesIO(data), remote)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="10.136.212.15")
    ap.add_argument("--expect-host", default="AUSTIN-SYS-08")
    ap.add_argument("--no-conf", action="store_true")
    ap.add_argument("--store", action="store_true", help="also seed the cold store (one time)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    files = []  # (remote, bytes)
    for rel in FILES:
        with open(os.path.join(CR_ROOT, rel), "rb") as fh:
            files.append((posixpath.join(BASE, rel), fh.read()))
    for src, dst in SYS08.items():
        with open(os.path.join(HERE, "sys08", src), "rb") as fh:
            files.append((posixpath.join(BASE, dst), fh.read()))
    for rel in GRS_SNAPSHOT:
        with open(os.path.join(GRS_ROOT, rel), "rb") as fh:
            files.append((posixpath.join(BASE, "snapshot", rel), fh.read()))
    for rel in EMPTY_INITS:
        files.append((posixpath.join(BASE, "snapshot", rel), b""))
    if a.store:
        store = os.path.join(CR_ROOT, "systest_db_store")
        for d, _dirs, names in os.walk(store):
            for n in names:
                local = os.path.join(d, n)
                with open(local, "rb") as fh:
                    rel = os.path.relpath(local, CR_ROOT).replace("\\", "/")
                    files.append((posixpath.join(BASE, rel), fh.read()))
    lines = [f"claude-resource {_git_sha(CR_ROOT)}", f"grs (read only) {_git_sha(GRS_ROOT)}"]
    lines += [f"{hashlib.sha256(d).hexdigest()}  {r[len(BASE) + 1:]}" for r, d in files if "systest_db_store" not in r]
    manifest = ("\n".join(lines) + "\n").encode()

    print(f"[plan] {len(files)} files -> {a.host}:{BASE} (store seed: {a.store}, conf: {not a.no_conf})")
    if a.dry_run:
        print(manifest.decode())
        return 0
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(a.host, username=USER, password=PASSWORD, timeout=30)
    try:
        _, out, _ = c.exec_command(f"{PS} -NoProfile -Command '$env:COMPUTERNAME'")
        host = out.read().decode().strip()
        if host != a.expect_host:
            print(f"REFUSE: {a.host} answers computer name {host!r}, expected {a.expect_host!r}; nothing touched")
            return 3
        sftp = c.open_sftp()
        for i, (remote, data) in enumerate(files, 1):
            _put(sftp, data, remote)
            if i % 1000 == 0:
                print(f"[deploy] {i}/{len(files)}")
        _put(sftp, manifest, posixpath.join(BASE, "MANIFEST.txt"))
        if not a.no_conf:
            src = os.path.join(os.path.expanduser("~"), ".grs_db.conf")
            with open(src, "rb") as fh:
                _put(sftp, fh.read(), posixpath.join(BASE, ".grs_db.conf"))
            print("[deploy] .grs_db.conf written (content not printed)")
        sftp.close()
        print(f"[deploy] done on {host}: {len(files)} files; see {BASE}/MANIFEST.txt")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
