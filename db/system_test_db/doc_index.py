"""The /doc index: one page that links every document the report server serves.

  doc_index_html(docs_dir)   body of the page (packs found under docs/, the gate history, the guides, the query page)

A pack is a folder docs/<name>/ of HTML pages; its overview.html is the entry. New packs and new pages show up on their own:
the listing is read from disk on each request, nothing is registered by hand. The page uses only the report's own look
(card, table, cpu-explain); it adds no style of its own.
"""

import html
import os
import re

_NAME = re.compile(r"[a-z0-9_-]+")
_TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)

# pages the server builds itself (not files under docs/): (url, label, one line)
FIXED = (
    ("/doc/gates", "Gate history", "which health gate and which dump trigger was in force when, per release"),
    ("/guide/resources", "Resource guide (English)", "how to read the CPU, memory and handle numbers"),
    ("/guide/resources/zh", "Resource guide (zhTW)", "same guide in Traditional Chinese"),
    ("/query", "Query", "latest PASS results by test case, across all lanes"),
    ("/", "Release reports", "the live report of every release, with its -final cut"),
)


def _title(path, fallback):
    try:
        with open(path, encoding="utf-8") as fh:
            head = fh.read(4000)
    except OSError:
        return fallback
    m = _TITLE.search(head)
    # the <title> is already HTML-escaped in the page; undo that once so it is escaped exactly once on the way out
    text = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip() if m else ""
    return text or fallback


def _packs(docs_dir):
    """[(pack, [(page, title)])] for every folder under docs_dir that has an overview.html."""
    out = []
    if not os.path.isdir(docs_dir):
        return out
    for pack in sorted(os.listdir(docs_dir)):
        folder = os.path.join(docs_dir, pack)
        if not (_NAME.fullmatch(pack) and os.path.isfile(os.path.join(folder, "overview.html"))):
            continue
        pages = []
        for fn in sorted(os.listdir(folder)):
            stem, ext = os.path.splitext(fn)
            if ext == ".html" and _NAME.fullmatch(stem):
                pages.append((stem, _title(os.path.join(folder, fn), stem)))
        out.append((pack, pages))
    return out


def _link(url, label):
    return f'<a class="tname" href="{html.escape(url)}">{html.escape(label)}</a>'


def doc_index_html(docs_dir):
    import report  # the report module owns the look (table / card); imported here so this file stays importable alone

    esc = html.escape
    parts = [
        '<div class="card"><h2>Documents</h2><div class="cpu-explain">Every document and tool page of this server. '
        "Packs are read from the docs folder on each request, so a new pack or page appears here without any edit.</div></div>",
        '<div class="card"><h2>Pages</h2>'
        + report.table(
            ["Page", "What it shows", "Address"],
            [[_link(u, label), line, u] for u, label, line in FIXED],
            raw_cols={0},
        )
        + "</div>",
    ]
    for pack, pages in _packs(docs_dir):
        overview_title = next((t for p, t in pages if p == "overview"), pack)
        rows = [[_link(f"/doc/{pack}/{p}", t), f"/doc/{pack}/{p}"] for p, t in pages if p != "overview"]
        parts.append(
            f'<div class="card"><h2>{esc(overview_title)}</h2>'
            f'<p class="foot">{_link("/doc/" + pack, "Overview")} &middot; {len(pages)} page(s)</p>'
            + report.table(["Page", "Address"], rows, raw_cols={0})
            + "</div>"
        )
    return "".join(parts)


if __name__ == "__main__":
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
    body = doc_index_html(here)
    packs = _packs(here)
    assert packs and all(p for p, _ in packs), "no doc packs found"
    assert "<ul" not in body and "<style" not in body, "the index must use the report look only"
    print("doc_index OK:", {p: len(pg) for p, pg in packs}, "| body bytes:", len(body))
