"""Pin report_server._norm_platform / _query_filters -- no server, no DB (same
convention as test_report_platform_filter.py / test_report_server_code_check.py).

Why this exists: these are the new query-string parsers that let the filter-bar
dropdowns (?platform=&tenant=) win over the legacy suffix/path-segment URLs.
A silent parsing bug here either drops a filter the user picked, or -- worse --
lets a bad value reach the DB query as a false "no match" instead of a 400.
"""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "system_test_db"))
)

import report_server as rs  # noqa: E402


class TestNormPlatform(unittest.TestCase):
    def test_known_platform_accepted(self):
        self.assertEqual(rs._norm_platform("mac"), "mac")
        self.assertEqual(rs._norm_platform("windows"), "windows")

    def test_case_insensitive(self):
        self.assertEqual(rs._norm_platform("MAC"), "mac")

    def test_empty_or_missing_is_no_filter(self):
        self.assertIsNone(rs._norm_platform(""))
        self.assertIsNone(rs._norm_platform(None))

    def test_unknown_platform_is_invalid_not_silently_ignored(self):
        """False (not None) means "present but bad" -- the caller must 400,
        never treat a typo as if no filter was requested."""
        self.assertIs(rs._norm_platform("androids"), False)


class TestQueryFilters(unittest.TestCase):
    def test_no_query_string_is_no_filters(self):
        self.assertEqual(
            rs._query_filters("/report/release-142"), (None, None, None, None, None)
        )

    def test_platform_only(self):
        self.assertEqual(
            rs._query_filters("/report/release-142?platform=mac"),
            ("mac", None, None, None, None),
        )

    def test_tenant_only(self):
        self.assertEqual(
            rs._query_filters("/report/release-142?tenant=1457"),
            (None, "1457", None, None, None),
        )

    def test_both_together(self):
        self.assertEqual(
            rs._query_filters("/report/release-142?platform=mac&tenant=1457"),
            ("mac", "1457", None, None, None),
        )

    def test_stack_only(self):
        platform, tenant, stack, _version, err = rs._query_filters(
            "/report/release-142?stack=stg01"
        )
        self.assertIsNone(platform)
        self.assertIsNone(tenant)
        self.assertEqual(stack, "stg01")
        self.assertIsNone(err)

    def test_bad_platform_is_an_error_not_a_silent_no_op(self):
        platform, tenant, stack, _version, err = rs._query_filters(
            "/report/release-142?platform=bogus"
        )
        self.assertIsNone(platform)
        self.assertIsNone(tenant)
        self.assertIsNone(stack)
        self.assertEqual(err, "bad platform")

    def test_bad_tenant_is_an_error_not_a_silent_no_op(self):
        platform, tenant, stack, _version, err = rs._query_filters(
            "/report/release-142?tenant=..%2F.."
        )
        self.assertIsNone(platform)
        self.assertIsNone(tenant)
        self.assertIsNone(stack)
        self.assertEqual(err, "bad tenant")

    def test_version_ehf_and_exact(self):
        self.assertEqual(
            rs._query_filters("/report/release-143?version=ehf"),
            (None, None, None, "ehf", None),
        )
        self.assertEqual(
            rs._query_filters("/report/release-143?tenant=1340&version=143.0.2.2839"),
            (None, "1340", None, "143.0.2.2839", None),
        )

    def test_bad_version_is_an_error_not_a_silent_no_op(self):
        for bad in ("bogus", "143.0.2.2839%3Bdrop", "1.2.3.4.5"):
            self.assertEqual(
                rs._query_filters(f"/report/release-143?version={bad}"),
                (None, None, None, None, "bad version"),
            )


if __name__ == "__main__":
    unittest.main()
