"""MIRROR NOTICE (added for endpoint-system-test-resource): this is a point-in-time copy for onboarding/public reference.
Canonical source (private repo): claude-resource/grs_tools/system_test_db/dbconn.py -- do not edit this copy directly; changes land upstream first, then get re-synced here.
"""

"""Connection settings for the SystemTest results DB.

Credentials NEVER live in the repo. Resolution order (first hit wins):

    1. env vars                GRS_DB_HOST / GRS_DB_PORT / GRS_DB_NAME /
                               GRS_DB_USER / GRS_DB_PASSWORD
    2. a local conf file       ~/.grs_db.conf   (key=value lines, gitignored by
                               virtue of living outside the repo). Override its
                               path with env GRS_DB_CONF.

Non-secret defaults (port/name/user) are applied only if nothing else supplies
them. HOST and PASSWORD have NO default — both must come from env or the conf
file, or connect() raises. So a box with just those two set (the rest defaulted)
works out of the box.
"""

import os

_DEFAULTS = {
    "GRS_DB_HOST": None,
    "GRS_DB_PORT": "5432",
    "GRS_DB_NAME": "grs_results",
    "GRS_DB_USER": "grs",
    "GRS_DB_PASSWORD": None,
}


def _conf_path():
    return os.environ.get("GRS_DB_CONF", os.path.join(os.path.expanduser("~"), ".grs_db.conf"))


def _load_conf_file():
    path = _conf_path()
    out = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def settings():
    """Return the resolved connection dict (env > conf file > non-secret default)."""
    conf = _load_conf_file()
    resolved = {}
    for key, default in _DEFAULTS.items():
        resolved[key] = os.environ.get(key) or conf.get(key) or default
    return resolved


def jenkins():
    """Resolve Jenkins base/user/password the same way (env > conf file). NO
    credential defaults live in the repo — base URL may default (not secret),
    user/password must come from env or ~/.grs_db.conf. Keys:
        JENKINS_BASE (default http://10.136.208.148:8080) / JENKINS_USER / JENKINS_PASS
    """
    conf = _load_conf_file()

    def _pick(key, default=None):
        return os.environ.get(key) or conf.get(key) or default

    base = _pick("JENKINS_BASE", "http://10.136.208.148:8080")
    user = _pick("JENKINS_USER")
    pw = _pick("JENKINS_PASS")
    if not user or not pw:
        raise SystemExit(
            "[jenkins] missing JENKINS_USER / JENKINS_PASS. Set them via env or "
            f"{_conf_path()} (no credential default is baked into the repo)."
        )
    return base, user, pw


def connect():
    """Open a psycopg2 connection from the resolved settings."""
    import psycopg2

    s = settings()
    missing = [k for k in ("GRS_DB_HOST", "GRS_DB_PASSWORD") if not s.get(k)]
    if missing:
        raise SystemExit(
            f"[db] missing connection settings: {', '.join(missing)}.\n"
            f"     set them via env or {_conf_path()} (see dbconn.py docstring)."
        )
    return psycopg2.connect(
        host=s["GRS_DB_HOST"],
        port=int(s["GRS_DB_PORT"]),
        dbname=s["GRS_DB_NAME"],
        user=s["GRS_DB_USER"],
        password=s["GRS_DB_PASSWORD"],
        connect_timeout=10,
    )


def describe():
    """Human-readable settings summary WITHOUT leaking the password."""
    s = settings()
    pw = s.get("GRS_DB_PASSWORD")
    return (
        f"host={s.get('GRS_DB_HOST')} port={s.get('GRS_DB_PORT')} "
        f"db={s.get('GRS_DB_NAME')} user={s.get('GRS_DB_USER')} "
        f"password={'set' if pw else 'MISSING'}  (conf: {_conf_path()})"
    )
