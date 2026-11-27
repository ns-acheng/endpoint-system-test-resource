"""Deploy the results-DB ingest poller to the DB server host (plan: task_docs/db_ingest_move/PLAN.md).

    python deploy_ingest_host.py [--host 10.136.218.74] [--expect-host u2404-kenmin] [--no-conf] [--dry-run]

Copies, over ssh/sftp, only into /home/nsadmin/grs_ingest/ on the DB host:
  grs_tools/...            the ingest closure of claude-resource (files listed in CLOSURE)
  snapshot/golden_regression/...   a read-only snapshot of three GRS files plus three empty __init__.py,
                                   so ingest.py can read health_gate and Configuration without a GRS clone
  MANIFEST.txt             sha256 of every file, the claude-resource sha and the GRS sha
and, unless --no-conf, ~/.grs_db.conf with GRS_DB_HOST=127.0.0.1 (chmod 600, the content is never printed).
GRS is only read. The box is refused unless its hostname matches --expect-host. Nothing is deleted.
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
GRS_ROOT = os.environ.get("GRS_REPO_ROOT", r"C:\git\nsclient_golden_regression_suite")
BASE = "/home/nsadmin/grs_ingest"
USER = os.environ.get("GRS_SSH_USER", "nsadmin")  # the lab VM account, same variables as grs_ssh.py
PASSWORD = os.environ.get("GRS_SSH_PASS") or USER  # lab VMs use the account name as the password

sys.path.insert(0, HERE)
import heartbeat  # noqa: E402  (INGEST_FILES is the one list of ingest files; never copied here)

CLOSURE = ["grs_tools/" + f for f in heartbeat.INGEST_FILES] + [  # claude-resource relative paths, kept under BASE
    "grs_tools/nowindow.py",
    "grs_tools/system_test_db/schema.sql",
]
EXTRA = [x for x in os.environ.get("GRS_INGEST_EXTRA", "").split(",") if x]  # more files found by a dry run
GRS_SNAPSHOT = [  # GRS relative paths, read only
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
    parts, cur = remote_dir.strip("/").split("/"), ""
    for part in parts:
        cur += "/" + part
        try:
            sftp.stat(cur)
        except IOError:
            sftp.mkdir(cur)


def _put_bytes(sftp, data, remote, mode=None):
    _mkdirs(sftp, posixpath.dirname(remote))
    sftp.putfo(io.BytesIO(data), remote)
    if mode:
        sftp.chmod(remote, mode)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="10.136.218.74")
    ap.add_argument("--expect-host", default="u2404-kenmin")
    ap.add_argument("--no-conf", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--if-stale",
        action="store_true",
        help="deploy only when a file hash differs from the MANIFEST.txt on the host (the sha header lines are ignored)",
    )
    a = ap.parse_args()

    files = []  # (remote, bytes)
    for rel in CLOSURE + EXTRA:
        with open(os.path.join(CR_ROOT, rel), "rb") as fh:
            files.append((posixpath.join(BASE, rel), fh.read()))
    for rel in GRS_SNAPSHOT:
        with open(os.path.join(GRS_ROOT, rel), "rb") as fh:
            files.append((posixpath.join(BASE, "snapshot", rel), fh.read()))
    for rel in EMPTY_INITS:
        files.append((posixpath.join(BASE, "snapshot", rel), b""))
    lines = [f"claude-resource {_git_sha(CR_ROOT)}", f"grs (read only) {_git_sha(GRS_ROOT)}"]
    lines += [f"{hashlib.sha256(d).hexdigest()}  {r[len(BASE) + 1:]}" for r, d in files]
    manifest = ("\n".join(lines) + "\n").encode()

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(a.host, username=USER, password=PASSWORD, timeout=30)
    try:
        _, out, _ = c.exec_command("hostname")
        host = out.read().decode().strip()
        if host != a.expect_host:
            print(f"REFUSE: {a.host} answers hostname {host!r}, expected {a.expect_host!r}; nothing touched")
            return 3
        if a.if_stale:
            try:
                sftp0 = c.open_sftp()
                with sftp0.open(posixpath.join(BASE, "MANIFEST.txt")) as fh:
                    remote_lines = fh.read().decode().splitlines()[2:]
                sftp0.close()
            except IOError:
                remote_lines = None
            if remote_lines == lines[2:]:
                print(f"[deploy] {a.host}: up to date ({len(files)} files match MANIFEST.txt), nothing copied")
                return 0
        print(f"[deploy] {a.host} hostname={host}; {len(files)} files -> {BASE}" + (" (dry run)" if a.dry_run else ""))
        if a.dry_run:
            print(manifest.decode())
            return 0
        sftp = c.open_sftp()
        for remote, data in files:
            _put_bytes(sftp, data, remote)
        _put_bytes(sftp, manifest, posixpath.join(BASE, "MANIFEST.txt"))
        if not a.no_conf:
            src = os.path.join(os.path.expanduser("~"), ".grs_db.conf")
            with open(src, encoding="utf-8") as fh:
                kept = [ln for ln in fh.read().splitlines() if not ln.strip().startswith("GRS_DB_HOST")]
            conf = ("\n".join(["GRS_DB_HOST=127.0.0.1"] + kept) + "\n").encode()
            _put_bytes(sftp, conf, "/home/nsadmin/.grs_db.conf", 0o600)
            print("[deploy] ~/.grs_db.conf written (GRS_DB_HOST=127.0.0.1, mode 600, content not printed)")
        sftp.close()
        print("[deploy] done; see MANIFEST.txt on the host")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
