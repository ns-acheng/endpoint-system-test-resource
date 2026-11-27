"""The resource guide pages are generated files: the committed HTML must equal what the sources render, the two languages must
link to each other, and the report must link to the guide and no longer carry the removed per-version average views.

Run: python grs_tools/tests/test_resource_guide_pages.py   (exit 1 on failure)
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUIDE = os.path.join(os.path.dirname(HERE), "system_test_db", "guide")
sys.path.insert(0, GUIDE)
import build_guide  # noqa: E402

REPORT_PY = os.path.join(os.path.dirname(HERE), "system_test_db", "report.py")


def _read(name):
    with open(os.path.join(GUIDE, name), encoding="utf-8") as fh:
        return fh.read()


def test_the_committed_pages_equal_what_the_sources_render():
    for key, page in build_guide.PAGES.items():
        assert _read(page["file"]) == build_guide.render(key), f"{page['file']} is stale: run guide/build_guide.py"


def test_the_two_languages_link_to_each_other():
    assert 'href="/guide/resources/zh"' in _read("resource_guide_en.html")
    assert 'href="/guide/resources"' in _read("resource_guide_zh.html")


def test_the_pages_carry_no_internal_task_ids_or_the_todo_section():
    for name in ("resource_guide_en.html", "resource_guide_zh.html"):
        text = _read(name)
        assert "git-00" not in text, name
        assert "圖表建議" not in text and "Chart proposals" not in text, name


def test_the_report_links_to_the_guide_and_dropped_the_average_views():
    with open(REPORT_PY, encoding="utf-8") as fh:
        src = fh.read()
    assert src.count("/guide/resources") >= 1
    for gone in ("cpuVersionAvgHeavyChart", "cpuVersionAvgLightChart", "cpuVersionAvgMacChart", "Avg growth MB"):
        assert gone not in src, gone


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                failures += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if failures else 0)
