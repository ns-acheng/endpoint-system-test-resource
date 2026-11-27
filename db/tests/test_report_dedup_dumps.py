"""report.py dedup_dump_paths: a dump path is listed under the first build that has it, not again under later builds of the lane."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "system_test_db")))
import report  # noqa: E402


def row(lane, build, *paths):
    detail = "crash dumps present: [" + ", ".join(f"'{p}'" for p in paths) + "]"
    return ["crash_dump", "t", lane, str(build), "d", "v", detail, ""]


def test_a_path_of_an_earlier_build_is_dropped_from_the_later_one():
    rows = [row("MAC2", 362, "/L/a.ips", "/L/b.ips", "/L/c.ips"), row("MAC2", 361, "/L/a.ips", "/L/b.ips")]
    out = report.dedup_dump_paths(rows)
    assert out[1][6] == "crash dumps present: ['/L/a.ips', '/L/b.ips']"  # the older build keeps its paths, row order unchanged
    assert out[0][6] == "crash dumps present: ['/L/c.ips']"


def test_a_row_with_no_new_path_says_so_and_other_lanes_are_independent():
    rows = [row("MAC2", 361, "/L/a.ips"), row("MAC2", 362, "/L/a.ips"), row("LOCAL1", 5, "/L/a.ips")]
    out = report.dedup_dump_paths(rows)
    assert out[1][6].endswith("every path is already listed under an earlier build of this lane")
    assert out[2][6] == "crash dumps present: ['/L/a.ips']"


def test_windows_paths_and_untouched_rows():
    p = "C:\\\\crash\\\\x.dmp"
    rows = [row("LOCAL2", 633, p), row("LOCAL2", 634, "C:\\\\crash\\\\y.dmp")]
    out = report.dedup_dump_paths(rows)
    assert out == rows
