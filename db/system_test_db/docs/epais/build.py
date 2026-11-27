"""Build the multi-page EPAIS review pack served by report_server.py at /doc/epais/<page>.

    python build.py        read source.html (the cards of the single-page draft) plus the two pages written here
                           (communicate), write one <page>.html per tab next to this file.

source.html is split at the lines that start a card (<div class="card"); every card goes to the page named in PAGE_OF
by the first words of its <h2>. The CSS is taken from source.html so the pack keeps the look of the SystemTest report.
"""

import html
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "source.html"), encoding="utf-8").read()
CSS = re.search(r"<style>(.*?)</style>", SRC, re.S).group(1)

# (slug, tab label, page title, one line for the page head)
PAGES = [
    ("overview", "Overview", "EPAIS: from document to a result of record", "The whole pipeline and the numbers, on one page."),
    ("study", "1 Study", "Study: the document versus what the product does", "Facts are tagged; only observed facts may carry a pass criterion."),
    ("poc", "2 Assumption & POC", "Assumption to POC", "A guess becomes a fact by one small, planned run."),
    ("generate", "3 Generate", "Generate system tests from the study", "A fresh workbook, evidence on every pass criterion."),
    ("implement", "4 Implement", "Implement and check", "Branch, module, helper, gate, plain push."),
    ("dispatch", "5 Dispatch", "Dispatch: who does what", "One case per run; one reporter."),
    ("communicate", "6 Communicate", "How we work together: ai_chat with klin, the bus and the kanban", "Humans and agents on different machines share one board."),
    ("status", "7 Status", "Where we are", "Evidence of record, per case."),
    ("lessons", "8 Lessons", "What went wrong and what we changed", "An honest list with cost and change."),
    ("grs", "Part 2 &middot; GRS", "How GRS tests the client", "From a trigger to a stored result, and why it is quick to learn."),
    ("recipe", "Part 2 &middot; New feature", "Pick up a new feature quickly (EPAIS, DEM agent)", "Six steps and a side-by-side."),
]
PAGE_OF = [
    ("hero", "overview"), ("The whole pipeline", "overview"), ("Flow 1", "study"), ("Flow 2", "poc"), ("Flow 3", "generate"),
    ("Flow 4", "implement"), ("Flow 5", "dispatch"), ("Where we are", "status"), ("What went wrong", "lessons"),
    ("Flow 6", "grs"), ("Good points", "grs"), ("Recipe", "recipe"),
]

COMMUNICATE = """
<div class="card">
<h2>Why a board</h2>
<p>The Windows owner (klin) works on another machine without our repository and answers in his own time. Agents on our side talk over a local bus that he cannot see. So the work that needs a human answer goes on <b>ai_chat</b>, a small message board on the report server: <a href="/ai_chat/1" target="_blank">/ai_chat/1</a>.</p>
<div class="callout warn">A real gap it exposed: when we asked for a review of the 15 new cases, klin answered "I cannot get the file; claude-resource does not exist on this machine". We pasted the 15 rows on the board and, from this pack on, shared documents are served from the report server (<code>/doc/epais</code>).</div>
</div>

<div class="card">
<h2>One review round, as it really happened (2026-11-09)</h2>
<div class="flow">
<div class="blk"><div class="n">17:59</div><h4>git44 posts</h4><p>Board message 113: the new SYSTEM write verb, the branch, the unit-test result, three questions for the Windows owner.</p></div>
<div class="blk"><div class="n">18:07</div><h4>klin reviews</h4><p>Message 114: ran the guard himself and found four path forms that bypass it, an undo record written too late, a nesting trap, a supervisor race.</p></div>
<div class="blk"><div class="n">18:2x</div><h4>git forwards</h4><p>The four points go to git44 over the bus, verbatim, with unit tests required.</p></div>
<div class="blk"><div class="n">18:3x</div><h4>git replies</h4><p>Message 117: what was forwarded, the file request answered, a question about a process that never ran in build 352.</p></div>
<div class="blk ok"><div class="n">next</div><h4>fix lands</h4><p>git44 pushes the head; git22 runs it on a lane; the result comes back to the same thread.</p></div>
</div>
<p class="foot">The review changed the code before any live run: a deleting path guard that could be bypassed would have been found only by an accident on a VM.</p>
</div>

<div class="card">
<h2>Which channel for what</h2>
<table><thead><tr><th>Channel</th><th>Who</th><th>Used for</th><th>Rule</th></tr></thead><tbody>
<tr><td>ai_chat board</td><td>owner, klin, git, git44, git22</td><td>Reviews, questions, status for a human reader; shared files pasted when no repository is available</td><td>Messages in Traditional Chinese; one thread per topic; read receipt by <code>reader=</code></td></tr>
<tr><td>Coord bus</td><td>agents only</td><td>Task handover, build wake-ups, review findings between agents</td><td>A message with a question is an open request until answered; a waiter wakes an idle agent</td></tr>
<tr><td>Kanban</td><td>agents, owner reads</td><td>One task per work item and its stage gate</td><td>A task cannot leave study with a failing checklist</td></tr>
<tr><td>Pending-work ledger</td><td>each agent</td><td>Promises: what is waited on, ETA, a poll that says when it happened</td><td>A turn cannot end with an open item that has no registered wait</td></tr>
<tr><td>Hooks</td><td>every agent</td><td>Mechanical rules (one case per run, no force push, plan before a VM write)</td><td>A refusal is quoted verbatim in the report</td></tr></tbody></table>
</div>

<div class="card">
<h2>The board itself</h2>
<p>Open the live board: <a href="/ai_chat/1" target="_blank">/ai_chat/1</a> (thread with klin) or the list of threads <a href="/ai_chat" target="_blank">/ai_chat</a>. The working language with klin is Traditional Chinese, so the live messages are in that language; this pack shows them translated.</p>
<table><thead><tr><th>Board message</th><th>From</th><th>Content (translated)</th></tr></thead><tbody>
<tr><td>113</td><td>git44</td><td>Please review the SYSTEM write verb as the Windows owner. Branch, what changed (write with sha256 proof, two-layer path guard, single backup, always-restore context manager), unit tests on a fake runner, and three questions: is the guard strict enough, should restart use a SYSTEM task, does the policy-load order match your POC.</td></tr>
<tr><td>114</td><td>klin</td><td>Direction is right. One hole: <code>policy\\.</code>, <code>epais\\.\\policy</code>, <code>config\\.</code> and a trailing dot or space pass the guard and resolve to the whole folder, so a remove could delete it (I ran the check myself). The undo record is written after the write, so a failed write leaves a replaced file and a stray backup. Restore is runner-wide, so nesting restores too early. Keep restart on the SSH account, but handle the supervisor that restarts the daemon after 2 to 4 seconds.</td></tr>
<tr><td>115 to 117</td><td>git</td><td>The 15 candidate rows (EPAIS-31..45) pasted for review, and the reply: the four points were forwarded to the author, 1136 Windows runs go through the runner lane, and a question about a process that never ran in build 352.</td></tr></tbody></table>
<p class="foot">The board updates in the browser by polling for messages newer than the last one shown, so a long thread never reloads.</p>
</div>
"""

def split_cards():
    """Return {page: html} from the cards of source.html."""
    lines = SRC.split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.startswith('<div class="card')]
    out = {}
    for n, i in enumerate(starts):
        j = starts[n + 1] if n + 1 < len(starts) else len(lines)
        seg = [ln for ln in lines[i:j] if not ln.startswith('<div class="part"')]
        text = "\n".join(seg)
        if n == len(starts) - 1:  # the last card carries the page and body closers
            text = text.rsplit("</div>\n</body>", 1)[0] if "</div>\n</body>" in text else text
        h2 = re.search(r"<h2>(.*?)</h2>", text)
        key = "hero" if 'class="card hero"' in lines[i] else (html.unescape(h2.group(1)) if h2 else "")
        page = next((p for prefix, p in PAGE_OF if key.startswith(prefix)), None)
        if page is None:
            raise SystemExit(f"card with no page: {key!r}")
        out.setdefault(page, []).append(text)
    return out


def tabs(active):
    items = "".join(
        f'<a href="/doc/epais/{slug}" class="{"on" if slug == active else ""}">{label}</a>' for slug, label, _t, _s in PAGES
    )
    return items


def page_html(slug, body):
    idx = [p[0] for p in PAGES].index(slug)
    _s, _l, title, sub = PAGES[idx]
    prev_l = f'<a href="/doc/epais/{PAGES[idx-1][0]}">&lsaquo; {PAGES[idx-1][1]}</a>' if idx else "<span></span>"
    next_l = f'<a href="/doc/epais/{PAGES[idx+1][0]}">{PAGES[idx+1][1]} &rsaquo;</a>' if idx + 1 < len(PAGES) else "<span></span>"
    head = "" if slug == "overview" else f'<div class="card hero"><h1>{title}</h1><p class="muted">{sub}</p></div>'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>EPAIS review pack - {html.unescape(PAGES[idx][1])}</title>
<style>{CSS}
.tabs{{display:flex;gap:6px;flex-wrap:wrap;padding:10px 28px;background:var(--bg2);border-bottom:1px solid var(--border);position:sticky;top:56px;z-index:90}}
.tabs a{{font-size:13px;padding:4px 12px;border-radius:999px;border:1px solid var(--border);color:var(--muted);text-decoration:none;font-weight:600}}
.tabs a:hover{{color:var(--cyan);border-color:var(--border2)}}
.tabs a.on{{color:#05081a;background:var(--cyan);border-color:var(--cyan)}}
.pager{{display:flex;justify-content:space-between;margin-top:8px}}
.pager a{{color:var(--cyan);text-decoration:none;font-weight:600}}
a{{color:var(--cyan)}}
</style></head><body>
<div class="topbar"><span class="logo">GRS</span><span class="tag">EPAIS review pack</span>
<div class="nav"><a href="/">Reports</a><a href="/ai_chat">AI chat</a></div></div>
<div class="tabs">{tabs(slug)}</div>
<div class="page">
{head}
{body}
<div class="pager">{prev_l}{next_l}</div>
</div></body></html>
"""


def main():
    cards = split_cards()
    cards["communicate"] = [COMMUNICATE]
    for slug, _l, _t, _s in PAGES:
        if slug not in cards:
            raise SystemExit(f"page without content: {slug}")
        with open(os.path.join(HERE, f"{slug}.html"), "w", encoding="utf-8") as fh:
            fh.write(page_html(slug, "\n".join(cards[slug])))
        print("wrote", slug)


if __name__ == "__main__":
    main()
