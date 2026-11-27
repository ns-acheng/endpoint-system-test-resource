"""Tests for report_server's stale-rendering-code check — no server, no DB.

Why this exists: the report server renders in-process, so editing report.py does
nothing until a restart. On 2026-08-14 the new gate-breakdown columns were live
in the CLI HTML and absent from the dashboard for that reason, and nothing in
`status` could say so — the owner had to notice a missing column. The check that
replaced that eyeball is `_code_verdict`, so its three branches are pinned here.
"""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "system_test_db"))
)

import report_server as rs  # noqa: E402


class TestCodeVerdict(unittest.TestCase):
    def test_no_code_field_is_unknown_not_healthy(self):
        """A server started before the check must not read as 'current'."""
        running, stale = rs._code_verdict("ok", 1_700_000_000)
        self.assertIsNone(running)
        self.assertIsNone(stale)

    def test_disk_newer_than_process_is_stale(self):
        running, stale = rs._code_verdict("ok code=1700000000", 1_700_000_600)
        self.assertEqual(running, 1_700_000_000)
        self.assertTrue(stale)

    def test_same_mtime_is_current(self):
        _, stale = rs._code_verdict("ok code=1700000000", 1_700_000_000)
        self.assertFalse(stale)

    def test_sub_second_rounding_is_not_stale(self):
        """mtime is published as whole seconds; 0.4s of rounding is not a restart."""
        _, stale = rs._code_verdict("ok code=1700000000", 1_700_000_000.4)
        self.assertFalse(stale)

    def test_code_mtime_covers_report_module(self):
        """The snapshot must track report.py — that is the file being edited."""
        here = os.path.join(os.path.dirname(rs.__file__))
        self.assertGreaterEqual(
            rs._code_mtime(), os.path.getmtime(os.path.join(here, "report.py"))
        )


if __name__ == "__main__":
    unittest.main()
