"""Tests for db.py's gate-ticket plumbing — no live DB.

Two things are worth pinning without a database:

1. `_migrate_blocks()` — the marker mechanism that lets `migrate` create a whole
   TABLE on a populated DB by reading schema.sql instead of carrying a second
   copy of the DDL (fault ledger 41: a copied constant rots). If the markers or
   the parser break, `apply-schema` on a fresh DB still works, so nothing else
   would notice — only the populated live DB silently misses the table.
2. `issue set/rm` argument validation, which must fail BEFORE touching the DB.
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest

_DBDIR = os.path.join(os.path.dirname(__file__), "..", "system_test_db")
sys.path.insert(0, os.path.abspath(_DBDIR))

import db  # noqa: E402

_PY = sys.executable
_DB = os.path.join(_DBDIR, "db.py")


class TestMigrateBlocks(unittest.TestCase):
    def test_gate_ticket_block_present(self):
        blocks = dict(db._migrate_blocks())
        self.assertIn("gate_ticket", blocks)
        sql = blocks["gate_ticket"]
        self.assertIn("CREATE TABLE IF NOT EXISTS gate_ticket", sql)
        # Both partial unique indexes: a plain UNIQUE would let the whole-build
        # form (test_name IS NULL) be inserted twice on PG 14.
        self.assertIn("uq_gt_case", sql)
        self.assertIn("uq_gt_build", sql)

    def test_every_block_statement_is_idempotent(self):
        """`migrate` runs these against a POPULATED DB, possibly repeatedly.

        Strip `-- line comments` BEFORE splitting on ';' -- a bare split found
        a false positive (2026-10-07): an existing comment line ("...resolvable
        test_run; --version/--bitness override...") has a semicolon INSIDE the
        comment text, which split one real CREATE TABLE statement into two
        fragments and failed the back half for "missing IF NOT EXISTS" even
        though the actual SQL statement is fine. Schema comments never contain
        a string literal with a semicolon, so this simple per-line strip is safe
        here (not a general SQL comment stripper).
        """
        for name, sql in db._migrate_blocks():
            no_comments = "\n".join(
                re.sub(r"--.*$", "", line) for line in sql.splitlines()
            )
            for stmt in [s.strip() for s in no_comments.split(";") if s.strip()]:
                head = " ".join(stmt.split())[:80]
                self.assertIn(
                    "IF NOT EXISTS",
                    stmt.upper(),
                    f"migrate-block {name!r} has a non-idempotent statement: {head}",
                )

    def test_unterminated_block_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "schema.sql")
            with open(path, "w", encoding="utf-8") as f:
                f.write("-- migrate-block: oops\nCREATE TABLE x (id int);\n")
            orig = db._SCHEMA
            db._SCHEMA = path
            try:
                with self.assertRaises(ValueError):
                    db._migrate_blocks()
            finally:
                db._SCHEMA = orig


class TestIssueArgs(unittest.TestCase):
    """`issue set` must reject missing args without a DB connection.

    Run through the real CLI with no DB config so a regression that moved the
    check after `dbconn.connect()` would surface as a connection error instead of
    the usage message.
    """

    def _run(self, *args):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        # Point the conf resolver at an empty dir so no real credentials load.
        env["GRS_DB_HOST"] = ""
        env["USERPROFILE"] = env["HOME"] = tempfile.gettempdir()
        r = subprocess.run(
            [_PY, _DB, "issue", *args], capture_output=True, text=True, env=env
        )
        return r.returncode, r.stdout + r.stderr

    def test_set_without_build_is_usage_error(self):
        rc, out = self._run("set", "ENG-1180143")
        self.assertEqual(rc, 2)
        self.assertIn("--build", out)

    def test_set_without_ticket_is_usage_error(self):
        rc, out = self._run("set", "--build", "87")
        self.assertEqual(rc, 2)
        self.assertIn("<TICKET>", out)


if __name__ == "__main__":
    unittest.main()
