"""Pin report.split_platform — no server, no DB (same convention as
test_report_server_code_check.py).

Why this exists: a platform-only view (/report/release-142-mac) is a
query-time filter on run.platform, not a second DB release row (see
report.py::split_platform docstring). The parser is the one place that can
silently swallow a real release label (e.g. treating "-final" as a platform
suffix) or fail to strip a real one, so its branches are pinned here.
"""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "system_test_db"))
)

import report  # noqa: E402


class TestSplitPlatform(unittest.TestCase):
    def test_mac_suffix_is_stripped(self):
        self.assertEqual(
            report.split_platform("release-141-mac"), ("release-141", "mac")
        )

    def test_windows_and_linux_suffixes_too(self):
        self.assertEqual(
            report.split_platform("release-142-windows"), ("release-142", "windows")
        )
        self.assertEqual(
            report.split_platform("release-142-linux"), ("release-142", "linux")
        )

    def test_no_suffix_is_unchanged(self):
        self.assertEqual(report.split_platform("release-142"), ("release-142", None))

    def test_unrelated_suffix_is_not_mistaken_for_a_platform(self):
        """release-141-final is a real, separate DB release label — must not be
        parsed as "release-141" + platform "final"."""
        self.assertEqual(
            report.split_platform("release-141-final"), ("release-141-final", None)
        )

    def test_case_insensitive_suffix(self):
        self.assertEqual(
            report.split_platform("release-141-MAC"), ("release-141", "mac")
        )

    def test_empty_and_none_are_safe(self):
        self.assertEqual(report.split_platform(""), ("", None))
        self.assertEqual(report.split_platform(None), ("", None))


class TestShortRelease(unittest.TestCase):
    def test_strips_the_release_prefix(self):
        self.assertEqual(report.short_release("release-142"), "142")

    def test_final_suffix_stays_intact(self):
        """release-141-final is a real, separate DB row -- only the leading
        "release-" prefix is stripped, not any suffix."""
        self.assertEqual(report.short_release("release-141-final"), "141-final")

    def test_no_prefix_is_unchanged(self):
        self.assertEqual(report.short_release("142"), "142")

    def test_empty_and_none_are_safe(self):
        self.assertEqual(report.short_release(""), "")
        self.assertEqual(report.short_release(None), "")


if __name__ == "__main__":
    unittest.main()
