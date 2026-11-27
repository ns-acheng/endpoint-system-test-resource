"""Build the resource guide pages (English + zhTW) and the proposal figure.

    python build_guide.py

Writes resource_guide_en.html and resource_guide_zh.html next to this file; report_server.py serves them at
/guide/resources (English) and /guide/resources/zh (zhTW). Regenerate and commit the HTML after editing any file here
(tests/test_resource_guide_pages.py fails when the committed HTML is stale).
proposal_same_test_trend.svg is the "one test across versions" view kept for the board task about the per-version charts.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from content_en_a import part_a as en_a  # noqa: E402
from content_en_b import part_b as en_b  # noqa: E402
from content_zh_a import part_a as zh_a  # noqa: E402
from content_zh_b import part_b as zh_b  # noqa: E402
from guide_css import CSS  # noqa: E402
from guide_figs2 import fig_proposal  # noqa: E402

PAGES = {
    "en": dict(file="resource_guide_en.html", lang="en", title="Resource monitoring guide - SystemTest Report", tag="Resource guide",
               body=lambda: en_a() + en_b(), nav='<a class="cur" href="/guide/resources">English</a><a href="/guide/resources/zh">中文</a>',
               back="Back to report"),
    "zh": dict(file="resource_guide_zh.html", lang="zh-Hant", title="資源監控解說 - SystemTest Report", tag="資源監控解說",
               body=lambda: zh_a() + zh_b(), nav='<a href="/guide/resources">English</a><a class="cur" href="/guide/resources/zh">中文</a>',
               back="回到報表"),
}


def render(key):
    p = PAGES[key]
    return f"""<!DOCTYPE html>
<html lang="{p['lang']}"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{p['title']}</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>{CSS}</style></head><body>
<nav class="topbar"><span class="logo">Netskope SystemTest</span><span class="tag">{p['tag']}</span>
<span class="push"></span>{p['nav']}<a href="/" onclick="if(history.length&gt;1){{history.back();return false}}">{p['back']}</a></nav>
<div class="page">
{p['body']()}
</div></body></html>"""


def build(key):
    html = render(key)
    out = os.path.join(HERE, PAGES[key]["file"])
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    print("wrote", out, len(html), "bytes")


if __name__ == "__main__":
    for k in PAGES:
        build(k)
    svg = fig_proposal("en")
    cut = svg.index(">") + 1
    with open(os.path.join(HERE, "proposal_same_test_trend.svg"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n' + svg[:cut] + '<rect width="100%" height="100%" fill="#1a2235"/>' + svg[cut:])
    print("wrote proposal_same_test_trend.svg")
