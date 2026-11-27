"""Mirror the cold store (systest_db_store) to the DB host, a second machine, without GitHub.

Incremental: a file is copied when it is missing on the host or its size differs. Nothing is deleted there. Prints one
line and appends it to <BASE>\\store_sync.log. Never raises: a failed sync must not fail the harvest that ran before it.
"""

import os
import posixpath
import time

import paramiko

BASE = os.path.dirname(os.path.abspath(__file__))
STORE = os.environ.get("GRS_DB_STORE") or os.path.join(BASE, "systest_db_store")
HOST = os.environ.get("GRS_STORE_SYNC_HOST", "10.136.218.74")
USER = os.environ.get("GRS_SSH_USER", "nsadmin")  # lab VM account, same variables as deploy_ingest_host.py
PASSWORD = os.environ.get("GRS_SSH_PASS") or USER
REMOTE = "/home/nsadmin/grs_store/systest_db_store"
LOG = os.path.join(BASE, "store_sync.log")


def _mkdirs(sftp, path):
    cur = ""
    for part in path.strip("/").split("/"):
        cur += "/" + part
        try:
            sftp.stat(cur)
        except IOError:
            sftp.mkdir(cur)


def sync():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASSWORD, timeout=30)
    copied = seen = 0
    try:
        sftp = c.open_sftp()
        for d, _dirs, files in os.walk(STORE):
            rel = os.path.relpath(d, STORE).replace("\\", "/")
            rdir = REMOTE if rel == "." else posixpath.join(REMOTE, rel)
            _mkdirs(sftp, rdir)
            have = {a.filename: a.st_size for a in sftp.listdir_attr(rdir)}
            for f in files:
                seen += 1
                local = os.path.join(d, f)
                if have.get(f) != os.path.getsize(local):
                    sftp.put(local, posixpath.join(rdir, f))
                    copied += 1
        sftp.close()
    finally:
        c.close()
    return seen, copied


def main():
    t0 = time.time()
    try:
        seen, copied = sync()
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} store-sync ok host={HOST} files={seen} copied={copied} {time.time() - t0:.0f}s"
    except Exception as e:  # noqa: BLE001 - never fail the harvest
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} store-sync FAILED host={HOST} {type(e).__name__}: {str(e)[:160]}"
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if os.sys.stdout is not None:
        print(line)
    return line


if __name__ == "__main__":
    main()
