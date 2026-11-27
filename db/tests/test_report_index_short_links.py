"""Pin report_server._index_html's link generation -- no server, no DB (pure
function: releases/platforms are passed in as plain lists).

Why this exists: the index used to link "release-142" verbatim. Owner
2026-08-27: showing two different-looking URLs for the same report (the long
DB label vs. the short number) is the confusing part, not the short form --
so every generated link must use short_release, while old long-form bookmarks
still resolve (that part is _norm_release's job, unchanged, pinned elsewhere).
"""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "system_test_db"))
)

import report_server as rs  # noqa: E402


class TestIndexShortLinks(unittest.TestCase):
    def test_release_link_is_short(self):
        html = rs._index_html(["release-142"], [])
        self.assertIn('href="/report/142"', html)
        self.assertNotIn("release-142", html)

    def test_platform_shortcut_link_is_short(self):
        html = rs._index_html(["release-142"], ["mac"])
        self.assertIn('href="/report/142-mac"', html)

    def test_final_release_link_keeps_its_suffix_but_drops_the_prefix(self):
        """-final is not a platform suffix (split_platform only knows mac/
        windows/linux) -- it gets platform shortcuts appended like any other
        release, just with the long "release-" prefix gone."""
        html = rs._index_html(["release-141-final"], ["mac"])
        self.assertIn('href="/report/141-final"', html)
        self.assertIn('href="/report/141-final-mac"', html)
        self.assertNotIn("release-141-final", html)

    def test_already_platform_suffixed_label_skips_the_double_shortcut(self):
        """A label that IS already a platform view (e.g. a hypothetical
        "release-142-mac" DB row) must not render "142-mac-mac"."""
        html = rs._index_html(["release-142-mac"], ["mac", "windows"])
        self.assertIn('href="/report/142-mac"', html)
        self.assertNotIn("142-mac-mac", html)
        self.assertNotIn("142-mac-windows", html)

    def test_no_releases_falls_back_to_the_empty_message(self):
        html = rs._index_html([], [])
        self.assertIn("no releases in the DB yet", html)


if __name__ == "__main__":
    unittest.main()
