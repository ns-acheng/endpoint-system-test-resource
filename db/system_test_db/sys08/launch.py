"""Launcher of the DB scheduled tasks on SYS-08: sets the paths in THIS process only, then runs one task script.

    pythonw.exe C:\\grs_tasks\\launch.py <script.py> [args]

The environment variables are set here and not machine-wide, because the owner also runs GRS-AUSTIN on this box and
must not see GRS_* variables appear in his runs. Child processes (db.py, harvest.py) inherit them.

After the harvest driver this also mirrors the cold store to the DB host (store_sync.py), because SYS-08 has no push
credential for claude-resource and the store needs a copy on a second machine.
"""

import os
import runpy
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("GRS_DB_CONF", os.path.join(BASE, ".grs_db.conf"))
os.environ.setdefault("GRS_REPO_ROOT", os.path.join(BASE, "snapshot"))
os.environ.setdefault("GRS_DB_STORE", os.path.join(BASE, "systest_db_store"))
os.environ.setdefault("GRS_DB_HARVEST_NOPUSH", "1")
os.environ.setdefault("GRS_DB_BACKUP_DIR", os.path.join(BASE, "db_backups"))
os.environ.setdefault("GRS_DB_BACKUP_OFFBOX", os.path.join(BASE, "db_backups_offbox"))
sys.path.insert(0, os.path.join(BASE, "grs_tools"))
sys.path.insert(0, os.path.join(BASE, "grs_tools", "system_test_db"))


def main():
    script = sys.argv[1]
    sys.argv = [script] + sys.argv[2:]
    code = 0
    try:
        runpy.run_path(os.path.join(BASE, "grs_tools", script), run_name="__main__")
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    if script == "systest_db_harvest_daily.py":
        import store_sync

        store_sync.main()
    return code


if __name__ == "__main__":
    sys.exit(main())
