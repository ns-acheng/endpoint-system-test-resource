"""Pin report.filter_bar -- no server, no DB (same convention as
test_report_platform_filter.py).

Why this exists: the two dropdowns are the only UI surface for the
platform/tenant filters (report_platform_tenant_filter plan). A regression
here either loses the current selection (dropdown resets to "All" every
render) or breaks the GET action, and both are silent -- the page still
renders, just filters wrong.
"""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "system_test_db"))
)

import report  # noqa: E402


class TestFilterBar(unittest.TestCase):
    def test_no_filter_selects_the_all_options(self):
        html = report.filter_bar(
            "release-142", None, None, None, ["mac", "windows"], ["1457"], []
        )
        self.assertIn('<option value="" selected>All platforms</option>', html)
        self.assertIn('<option value="" selected>All tenants</option>', html)

    def test_current_platform_is_marked_selected(self):
        html = report.filter_bar(
            "release-142", "mac", None, None, ["mac", "windows"], [], []
        )
        self.assertIn('<option value="mac" selected>Mac</option>', html)
        self.assertIn('<option value="windows">Windows</option>', html)
        # "All platforms" must NOT be selected once a real platform is chosen.
        self.assertIn('<option value="">All platforms</option>', html)

    def test_current_tenant_is_marked_selected(self):
        html = report.filter_bar(
            "release-142", None, "1457", None, [], ["1329", "1457"], []
        )
        # tenant_label() enriches with the tenant's hostname -- match on the
        # tenant id + selected-marker prefix, not the full label text.
        self.assertIn('<option value="1457" selected>1457 ', html)
        self.assertIn('<option value="1329">1329 ', html)

    def test_action_targets_the_short_release_no_suffix_no_tenant_segment(self):
        """Whatever gets submitted must normalize to the query-string form on
        the SHORT release -- not the long "release-142" form, and not a stale
        suffix/path segment (owner 2026-08-27: two URL forms for one report
        is the confusing part, not the short form)."""
        html = report.filter_bar(
            "release-142", "mac", "1457", None, ["mac"], ["1457"], []
        )
        self.assertIn('action="/report/142"', html)
        self.assertNotIn("release-142", html)

    def test_empty_option_lists_still_render_the_all_options(self):
        """A release with zero runs so far (e.g. release-142-mac before its
        first mac build lands) must not blow up building an empty dropdown."""
        html = report.filter_bar("release-142", None, None, None, [], [], [])
        self.assertIn("All platforms", html)
        self.assertIn("All tenants", html)


if __name__ == "__main__":
    unittest.main()
