"""Regression tests for ingest build identity and the ingester heartbeat.

2026-10-16: REG builds 175-183 were skipped as "already in DB" for a day because July's
rows held the same build NUMBERS (the job counter restarted). A build number is not an
identity; number AND start time is. These tests pin that, the audit's collision
detection, and the heartbeat comparison that catches a silent / out-of-date ingester.
Pure functions only -- no DB, no Jenkins.
"""

import importlib
import os
import sys
import unittest

_DBDIR = os.path.join(os.path.dirname(__file__), "..", "system_test_db")
sys.path.insert(0, _DBDIR)
sys.path.insert(0, os.path.join(_DBDIR, ".."))
db = importlib.import_module("db")
heartbeat = importlib.import_module("heartbeat")

JULY = 1_785_000_000.0
OCT = JULY + 80 * 86400.0


class TestIsIngested(unittest.TestCase):
    def test_same_build_is_ingested(self):
        have = {175: [OCT + 120]}  # ndjson start differs from Jenkins by minutes
        self.assertTrue(db._is_ingested(have, {"number": 175, "ts": OCT}))

    def test_reused_number_is_not_ingested(self):
        """THE incident: new build 175 (Oct) vs an old row numbered 175 (July)."""
        have = {175: [JULY]}
        self.assertFalse(db._is_ingested(have, {"number": 175, "ts": OCT}))

    def test_unknown_number_is_not_ingested(self):
        self.assertFalse(db._is_ingested({}, {"number": 9, "ts": OCT}))

    def test_row_without_start_time_still_counts(self):
        """No clock to compare: trust the number (the pre-fix behaviour), never loop."""
        self.assertTrue(db._is_ingested({175: [None]}, {"number": 175, "ts": OCT}))

    def test_old_and_new_rows_both_present(self):
        have = {175: [JULY, OCT]}
        self.assertTrue(db._is_ingested(have, {"number": 175, "ts": OCT}))


class TestCollidedBuilds(unittest.TestCase):
    def test_flags_only_the_reused_number(self):
        times = {175: [JULY], 176: [OCT], 177: [JULY, OCT]}
        jenkins = {n: {"ts": OCT} for n in (175, 176, 177)}
        self.assertEqual(db._collided_builds(times, jenkins), {175})

    def test_build_jenkins_forgot_is_not_collided(self):
        self.assertEqual(db._collided_builds({175: [JULY]}, {}), set())


class TestHeartbeatProblems(unittest.TestCase):
    A = ("winbox", "aaaa111111", 120.0, 0, "ok")
    B = ("SYS-07", "aaaa111111", 300.0, 0, "ok")

    def test_healthy_pair(self):
        self.assertEqual(heartbeat.problems([self.A, self.B]), [])

    def test_version_skew_is_reported(self):
        skew = ("SYS-07", "bbbb222222", 300.0, 0, "ok")
        out = heartbeat.problems([self.A, skew])
        self.assertTrue(any("DIFFERENT code" in p for p in out), out)

    def test_silent_ingester_is_reported(self):
        silent = ("SYS-07", "aaaa111111", 3 * 3600.0, 0, "ok")
        out = heartbeat.problems([self.A, silent])
        self.assertTrue(any("silent" in p for p in out), out)
        self.assertTrue(any("redundancy" in p for p in out), out)

    def test_failed_poll_is_reported(self):
        bad = ("SYS-07", "aaaa111111", 300.0, 1, "boom")
        self.assertTrue(any("failed" in p for p in heartbeat.problems([self.A, bad])))

    def test_retired_host_does_not_alarm(self):
        old = ("old-vm", "cccc333333", 30 * 86400.0, 0, "ok")
        self.assertEqual(heartbeat.problems([self.A, self.B, old]), [])

    def test_no_heartbeats_at_all(self):
        self.assertTrue(heartbeat.problems([]))


if __name__ == "__main__":
    unittest.main()
