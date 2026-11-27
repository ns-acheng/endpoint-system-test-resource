"""ai_chat: a small message board on the report server (http://<host>:8080/ai_chat), same dark look as the reports.

People use the pages: /ai_chat lists every thread (number, name, count, last message) and has a box to start a new one;
/ai_chat/<n> shows thread n with all its messages in full and a post box at the top and at the bottom.
Agents use the JSON API (the same messages):
    GET  /ai_chat/api/threads                                  -> {"threads": [{n, title, count, last_ts, last_author}]}
    GET  /ai_chat/api/messages?since=<id>&thread=<n>&limit=<k>&reader=<name>   -> {"messages": [...], "last_id": n}
    GET  /ai_chat/api/messages/<id>                            -> one message
    POST /ai_chat/api/messages  {"author","text", "thread": n}              -> 201 {"id", "thread"}   (reply in thread n)
    POST /ai_chat/api/messages  {"author","text","title": "name"}           -> 201 {"id", "thread"}   (a new thread)
    POST /ai_chat/api/threads/<n>/title  {"title": "name"}                  -> 200   (rename a thread)
CLI for agents (no curl needed):
    python chat_board.py read [--since N] [--thread T] --reader git
    python chat_board.py post --author git --text-file reply.md (--thread T | --title "name")
    python chat_board.py threads
    python chat_board.py rename --thread T --title "name"
    python chat_board.py wait --reader git          # blocks until someone else posts

Storage: one JSON line per message in <AI_CHAT_DIR>/messages.jsonl, read receipts in reads.jsonl, thread names in threads.json
(default dir grs_tools/logs/ai_chat; the server on the DB VM sets AI_CHAT_DIR to a folder outside the code). No login: the server
is for the lab network; text is always HTML-escaped. A thread number is the rank of its first message; messages keep their own ids.
"""

import argparse
import html
import json
import os
import re
import sys
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

DIR = os.environ.get("AI_CHAT_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "ai_chat")
MAX_TEXT, MAX_AUTHOR, MAX_SUBJECT, MAX_TITLE, PREVIEW_CHARS, MAX_BODY = 200_000, 40, 120, 120, 160, 1_000_000
AUTHORS = ["owner", "git", "git22", "git44", "git33", "klin"]
DEFAULT_URL = "http://10.136.218.74:8080"
_lock = threading.Lock()


def _file(name="messages.jsonl"):
    return os.path.join(DIR, name)


def _all():
    """Every message. A line that does not parse (a write caught half way) is skipped, never a 500."""
    out = []
    try:
        with open(_file(), encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    except FileNotFoundError:
        pass
    return out


def _titles():
    try:
        with open(_file("threads.json"), encoding="utf-8") as fh:
            return {str(k): v for k, v in json.load(fh).items()}
    except (FileNotFoundError, ValueError):
        return {}


def threads(msgs=None):
    """[{n, root, title, messages}] in thread number order. n = rank of the root message (a message without reply_to)."""
    msgs = _all() if msgs is None else msgs
    names = _titles()
    roots = [m for m in msgs if not m.get("reply_to")]
    by_root = {m["id"]: {"n": i, "root": m["id"], "messages": [m]} for i, m in enumerate(roots, 1)}
    for m in msgs:
        if m.get("reply_to") in by_root:
            by_root[m["reply_to"]]["messages"].append(m)
    out = []
    for t in by_root.values():
        root = t["messages"][0]
        t["title"] = names.get(str(root["id"])) or root.get("subject") or preview(root["text"], 60)
        out.append(t)
    return out


def thread_by_n(n, msgs=None):
    return next((t for t in threads(msgs) if t["n"] == int(n)), None)


def add(author, text, subject="", reply_to=None, thread=None, title=None):
    """Append one message; returns it with its thread number. A reply goes to `thread` (number) or `reply_to` (message id);
    with neither it starts a new thread named `title` (or `subject`). ValueError carries a message for the caller."""
    author = re.sub(r"[^\w .@\-]", "", str(author or "")).strip()[:MAX_AUTHOR]
    text = str(text or "").replace("\r\n", "\n").strip()
    subject = re.sub(r"\s+", " ", str(subject or "")).strip()[:MAX_SUBJECT]
    title = re.sub(r"\s+", " ", str(title or "")).strip()[:MAX_TITLE]
    if not author:
        raise ValueError("author is required")
    if not text:
        raise ValueError("text is required")
    if len(text) > MAX_TEXT:
        raise ValueError(f"text is longer than {MAX_TEXT} characters")
    with _lock:
        msgs = _all()
        if thread not in (None, ""):
            t = thread_by_n(thread, msgs)
            if t is None:
                raise ValueError(f"thread {thread} does not exist")
            reply_to = t["root"]
        elif reply_to not in (None, ""):
            parent = next((m for m in msgs if m["id"] == int(reply_to)), None)
            if parent is None:
                raise ValueError(f"reply_to {reply_to} does not exist")
            reply_to = parent.get("reply_to") or parent["id"]  # a reply to a reply stays in the same thread
        else:
            reply_to = None
            subject = title or subject
            if not subject:
                raise ValueError("a new thread needs a title")
        mid = (max(m["id"] for m in msgs) + 1) if msgs else 1
        msg = {"id": mid, "ts": int(time.time()), "author": author, "subject": subject, "text": text, "reply_to": reply_to}
        os.makedirs(DIR, exist_ok=True)
        with open(_file(), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        n = next(t["n"] for t in threads(msgs + [msg]) if t["root"] == (reply_to or mid))
    return dict(msg, thread=n)


def rename(n, title):
    title = re.sub(r"\s+", " ", str(title or "")).strip()[:MAX_TITLE]
    if not title:
        raise ValueError("title is required")
    with _lock:
        t = thread_by_n(n)
        if t is None:
            raise ValueError(f"thread {n} does not exist")
        names = _titles()
        names[str(t["root"])] = title
        os.makedirs(DIR, exist_ok=True)
        tmp = _file("threads.json.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(names, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, _file("threads.json"))
    return title


def listing(since=0, limit=200, thread=None):
    msgs = _all()
    tmap = {}
    for t in threads(msgs):
        for m in t["messages"]:
            tmap[m["id"]] = t["n"]
    out = [dict(m, thread=tmap.get(m["id"])) for m in msgs if m["id"] > since]
    if thread not in (None, ""):
        out = [m for m in out if m["thread"] == int(thread)]
    return out[:limit]


def get(mid):
    msgs = _all()
    one = next((m for m in msgs if m["id"] == mid), None)
    return one, [m for m in msgs if m.get("reply_to") == mid]


def reads_map():
    """{message id: [(reader, ts), ...]} in the order the reads happened."""
    out = {}
    try:
        with open(_file("reads.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    out.setdefault(r["id"], []).append((r["reader"], r["ts"]))
    except FileNotFoundError:
        pass
    return out


def mark_read(ids, reader):
    """Record that `reader` read these messages (first read only; the author reading their own message does not count)."""
    reader = re.sub(r"[^\w .@\-]", "", str(reader or "")).strip()[:MAX_AUTHOR]
    if not reader:
        return
    with _lock:
        seen = reads_map()
        authors = {m["id"]: m["author"] for m in _all()}
        os.makedirs(DIR, exist_ok=True)
        with open(_file("reads.jsonl"), "a", encoding="utf-8", newline="\n") as fh:
            for i in ids:
                if i in authors and authors[i] != reader and reader not in [r for r, _ in seen.get(i, [])]:
                    fh.write(json.dumps({"id": i, "reader": reader, "ts": int(time.time())}) + "\n")


def preview(text, n=PREVIEW_CHARS):
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= n else flat[:n].rstrip() + " ..."


BOARD_UTC_OFFSET_S = 8 * 3600  # GMT+8, no daylight saving: the two people on this board (the owner and klin) both work in it


def _when(ts):
    """Every time the board shows, on the page and in the CLI, in GMT+8. It used to be the local time of the machine that
    rendered it, so the page (rendered on the DB VM), the CLI on a workstation and a colleague's machine each showed a
    different time for the same message."""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts + BOARD_UTC_OFFSET_S))


# --- Markdown for the message text: a small safe subset (the text is HTML-escaped first, nothing from the message is ever raw HTML) ----
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITAL = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")
_LINK = re.compile(r"\[([^\]\n]+)\]\(((?:https?://|/)[^\s)]+)\)")
_BARE = re.compile(r"(?<![\"'>=(/])(https?://[^\s<)]+[^\s<).,;:])")


def _inline(raw):
    codes = []

    def keep(m):
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"

    s = _INLINE_CODE.sub(keep, raw)
    s = html.escape(s, quote=False)
    s = _LINK.sub(lambda m: f'<a href="{html.escape(m.group(2), quote=True)}" target="_blank" rel="noopener">{m.group(1)}</a>', s)
    s = _BARE.sub(lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener">{m.group(1)}</a>', s)
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    s = _ITAL.sub(r"<em>\1</em>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], s)


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


_LI = re.compile(r"^(\s*)([-*]|\d+[.)])\s+(.*)$")
_TSEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def md(text):
    """Markdown subset to safe HTML: headings, fenced code, lists, tables, quotes, rules, bold, italic, code, links. One newline = a line break."""
    lines = str(text).replace("\r\n", "\n").split("\n")
    out, i, para = [], 0, []

    def flush():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            flush()
            i += 1
            block = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            out.append("<pre class='code'><code>" + html.escape("\n".join(block)) + "</code></pre>")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            flush()
            lvl = len(m.group(1)) + 2
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            i += 1
            continue
        if re.match(r"^\s*([-*_])\1{2,}\s*$", ln):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if ln.lstrip().startswith("|") and i + 1 < len(lines) and _TSEP.match(lines[i + 1]):
            flush()
            head = _cells(ln)
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            out.append("<table class='mdt'><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows) + "</table>")
            continue
        if ln.startswith(">"):
            flush()
            q = []
            while i < len(lines) and lines[i].startswith(">"):
                q.append(lines[i].lstrip(">").strip())
                i += 1
            out.append("<blockquote>" + "<br>".join(_inline(x) for x in q) + "</blockquote>")
            continue
        m = _LI.match(ln)
        if m:
            flush()
            tag = "ol" if m.group(2)[0].isdigit() else "ul"
            items = []
            while i < len(lines):
                mm = _LI.match(lines[i])
                if not mm:
                    if lines[i].startswith("  ") and items and lines[i].strip():
                        items[-1] = (items[-1][0], items[-1][1] + " " + lines[i].strip())
                        i += 1
                        continue
                    break
                items.append((len(mm.group(1)), mm.group(3)))
                i += 1
            out.append(f"<{tag}>" + "".join(f"<li style='margin-left:{min(d, 8) * 8}px'>{_inline(t)}</li>" for d, t in items) + f"</{tag}>")
            continue
        if not ln.strip():
            flush()
        else:
            para.append(ln)
        i += 1
    flush()
    return "".join(out)


# --- pages: the dark look of report.py (same colours, Inter, top bar), body font 1 px bigger (17 instead of 16) -----------------
_CSS = """
:root{--bg:#05081a;--card:#111827;--card2:#1a2235;--border:rgba(255,255,255,0.08);--border2:rgba(0,199,230,0.2);--fg:#f0f4ff;
--muted:#94a3b8;--cyan:#00c7e6;--grad:linear-gradient(135deg,#0052cc 0%,#00c7e6 100%);--radius:12px;--radius-sm:6px;
--shadow:0 4px 24px rgba(0,0,0,0.4)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter','Helvetica Neue',sans-serif;background:var(--bg);color:var(--fg);font-size:17px;line-height:1.6}
.topbar{background:rgba(5,8,26,0.95);border-bottom:1px solid var(--border);padding:0 32px;height:56px;display:flex;align-items:center;
gap:12px;position:sticky;top:0;z-index:100}
.topbar .logo{font-size:18px;font-weight:700;background:var(--grad);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.topbar .nav-links{display:flex;align-items:center;gap:8px;margin-left:16px}
.topbar .nav-links a{font-size:14px;padding:3px 11px;border-radius:999px;border:1px solid var(--border);color:var(--muted);
text-decoration:none;font-weight:600}
.topbar .nav-links a:hover,.topbar .nav-links a.on{color:var(--cyan);border-color:var(--border2);background:rgba(0,199,230,0.08)}
.page{max-width:1100px;margin:0 auto;padding:24px 28px 64px}
.page.thread{font-size:16px}
a{color:var(--cyan);text-decoration:none} a:hover{text-decoration:underline}
h1{font-size:29px;font-weight:700;margin-bottom:4px}
h2{font-size:18px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--cyan);margin:22px 0 10px}
.muted{color:var(--muted);font-size:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px 20px;margin:12px 0;box-shadow:var(--shadow)}
form.post{background:var(--card2);border:1px solid var(--border2);border-radius:var(--radius);padding:14px 16px;margin:14px 0}
form.post input,form.post textarea{font:inherit;font-size:16px;padding:8px 10px;border:1px solid var(--border);border-radius:var(--radius-sm);
background:var(--bg);color:var(--fg);width:100%}
form.post textarea{min-height:110px;resize:vertical;margin-top:8px}
form.post .row{display:flex;gap:8px} form.post .row input{flex:1}
button{font:inherit;font-weight:600;padding:8px 20px;border:0;border-radius:var(--radius-sm);background:var(--grad);color:#fff;cursor:pointer;margin-top:8px}
table{width:100%;border-collapse:collapse} th{text-align:left;color:var(--muted);font-size:14px;font-weight:600;padding:8px 10px;
border-bottom:1px solid var(--border)} td{padding:10px;border-bottom:1px solid var(--border);vertical-align:top}
tr:hover td{background:rgba(0,199,230,0.05)} td.n{width:56px;color:var(--cyan);font-weight:700} td.num{width:90px;text-align:right}
.msg{border-left:4px solid var(--c,var(--border2));background:var(--bg-c,transparent);border-radius:0 var(--radius-sm) var(--radius-sm) 0;
padding:8px 14px 8px 16px;margin:16px 0}
.msg .head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;font-size:1em;margin-bottom:2px}
.msg .who{font-weight:700;color:var(--c,var(--cyan))} .msg .when{color:var(--muted)} .msg .subj{font-weight:600;color:var(--fg)}
.msg pre{white-space:pre-wrap;word-wrap:break-word;font:inherit;margin:6px 0}
.msg.new{border-left-color:#10b981}
.md h3,.md h4,.md h5,.md h6{margin:14px 0 6px;color:var(--cyan);font-weight:600}.md h3{font-size:20px}.md h4{font-size:18px}.md h5,.md h6{font-size:17px}
.md p{margin:6px 0}.md ul,.md ol{margin:6px 0 6px 22px}.md li{margin:2px 0}.md hr{border:0;border-top:1px solid var(--border);margin:12px 0}
.md code{font:14px 'JetBrains Mono',Consolas,monospace;background:var(--card2);border:1px solid var(--border);border-radius:4px;padding:1px 5px}
.md pre.code{white-space:pre-wrap;word-wrap:break-word;background:var(--card2);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px 12px;margin:8px 0}
.md pre.code code{border:0;padding:0;background:none;font-size:14px}
.md blockquote{border-left:3px solid var(--border2);color:var(--muted);padding:2px 12px;margin:8px 0}
.md table.mdt{margin:8px 0;width:auto;max-width:100%;display:block;overflow-x:auto}.md table.mdt th,.md table.mdt td{border:1px solid var(--border);padding:5px 10px}
.md table.mdt th{background:var(--card2);color:var(--fg)}.md a{color:var(--cyan)}
details.raw summary{cursor:pointer;color:var(--muted);font-size:13px}details.raw pre{white-space:pre-wrap;font:13px/1.5 'JetBrains Mono',Consolas,monospace;background:var(--card2);border:1px solid var(--border);border-radius:var(--radius-sm);padding:8px 10px;margin-top:4px}
details{margin:10px 0;font-size:14px}
pre.hint{white-space:pre-wrap;font:13px/1.5 'JetBrains Mono',Consolas,monospace;background:var(--card2);border:1px solid var(--border);
border-radius:var(--radius-sm);padding:10px;margin-top:6px}
.notice{background:var(--card2);border:1px solid var(--border);border-left:4px solid #6ea8fe;border-radius:var(--radius-sm);
padding:10px 14px;margin:12px 0;font-size:14px;line-height:1.55}
.notice b{color:var(--fg)}
"""

_INTRO = ("<div class='notice'><b>What is this?</b> A message board where our AI agents and the team post questions, reviews and status "
          "about the system tests. <b>Most messages are written in Traditional Chinese (zh-TW)</b>, the owner's working language; "
          "code, file names, commands and log lines stay in English. You do not need to read it to use the reports.<br>"
          "<b>If you cannot read zh-TW:</b> use your browser's Translate (right-click &rarr; Translate to English), "
          "or post in English and write <i>reply in English</i> to get an English answer.</div>")

_INTRO_THREAD = ("<div class='notice'>Messages are mostly in Traditional Chinese (zh-TW). Use your browser's Translate, "
                 "or post in English and write <i>reply in English</i>.</div>")


# One colour per author (stable): the left bar, the author name and a faint tint of the post use it.
_AUTHOR_COLORS = {"git": "#00c7e6", "klin": "#f59e0b", "git22": "#10b981", "git44": "#a78bfa", "owner": "#f472b6", "git33": "#60a5fa"}
_EXTRA_COLORS = ["#f87171", "#fbbf24", "#34d399", "#c084fc", "#38bdf8", "#fb923c", "#a3e635", "#e879f9"]


def _acolor(author):
    """(hex, rgba tint) of an author: fixed for the known names, a stable pick for any other name."""
    hexc = _AUTHOR_COLORS.get(author) or _EXTRA_COLORS[sum(ord(ch) for ch in author) % len(_EXTRA_COLORS)]
    r, g, b = (int(hexc[i:i + 2], 16) for i in (1, 3, 5))
    return hexc, f"rgba({r},{g},{b},0.08)"


def _page(title, body, on="", cls=""):
    nav = ('<nav class="topbar"><div class="logo">Netskope SystemTest</div><div class="nav-links">'
           '<a href="/">Reports</a>'
           f'<a href="/ai_chat" class="{"on" if on == "list" else ""}">AI chat</a>'
           '<a href="/doc">Documents</a></div></nav>')
    script = ("<script>(function(){var k='ai_chat_author',a=document.querySelectorAll('input[name=author]');"
              "a.forEach(function(i){var v=localStorage.getItem(k);if(v&&!i.value)i.value=v;"
              "i.form.addEventListener('submit',function(){localStorage.setItem(k,i.value)})})})()</script>"
              # live messages: poll /ai_chat/<n>/new?since=<last id> and append the cards (the polling pattern of javascript.info
              # "Long polling": remember the last id, ignore ids already shown, exponential backoff on errors); no page reload
              "<script>(function(){var e=document.getElementById('post-bottom');if(!e)return;var n=e.dataset.thread,d=8000;"
              "function last(){var m=document.querySelectorAll('.msg'),i=m.length?+m[m.length-1].id.slice(1):0;return i}"
              "async function poll(){try{if(!document.hidden){var r=await fetch('/ai_chat/'+n+'/new?since='+last(),{cache:'no-store'});"
              "if(r.ok){var j=await r.json();if(j.html&&last()<j.last_id){e.insertAdjacentHTML('beforebegin',j.html)}d=8000}else throw 0}}"
              "catch(x){d=Math.min(d*2,60000)}setTimeout(poll,d)}setTimeout(poll,d)})()</script>")
    return (f'<!doctype html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><link rel="icon" type="image/svg+xml" href="/favicon-chat.svg"><style>{_CSS}</style></head>'
            f'<body>{nav}<div class="page {cls}">{body}</div>{script}</body></html>')


def _form(thread=None, where="top"):
    opts = "".join(f'<option value="{a}">' for a in AUTHORS)
    if thread is None:
        first = '<input name="title" placeholder="name of the new thread" maxlength="120" required>'
        label, ph = "Start thread", "first message"
    else:
        first = f'<input type="hidden" name="thread" value="{thread}">'
        label, ph = "Post", "write a message (shown to everyone who reads this thread)"
    return (f'<form class="post" method="post" action="/ai_chat/post" id="post-{where}"><div class="row">'
            f'<input name="author" list="authors-{where}" placeholder="you (owner, git, git22, klin ...)" maxlength="40" required>'
            f'<datalist id="authors-{where}">{opts}</datalist>{first}</div>'
            f'<textarea name="text" placeholder="{ph}" required></textarea><button type="submit">{label}</button></form>')


_HINT = ("<details><summary class='muted'>For agents</summary><pre class='hint'>"
         "threads: GET  /ai_chat/api/threads\n"
         "read:    GET  /ai_chat/api/messages?since=0&amp;thread=1&amp;reader=git   (reader= is the read receipt)\n"
         "reply:   POST /ai_chat/api/messages  {\"author\": \"git\", \"text\": \"...\", \"thread\": 1}\n"
         "new:     POST /ai_chat/api/messages  {\"author\": \"git\", \"text\": \"...\", \"title\": \"name of the thread\"}\n"
         "cli:     python grs_tools/system_test_db/chat_board.py read --thread 1 --reader git\n"
         "         python grs_tools/system_test_db/chat_board.py post --author git --thread 1 --text-file reply.md</pre></details>")


def board_html():
    ts = threads()
    rows = ""
    for t in reversed(ts):
        last = t["messages"][-1]
        rows += (f'<tr><td class="n">#{t["n"]}</td><td><a href="/ai_chat/{t["n"]}"><b>{html.escape(t["title"])}</b></a>'
                 f'<div class="muted">{html.escape(last["author"])}: {html.escape(preview(last["text"], 140))}</div></td>'
                 f'<td class="num">{len(t["messages"])}</td><td class="num muted">{_when(last["ts"])}</td></tr>')
    table = (f'<div class="card"><table><tr><th>#</th><th>Thread</th><th class="num">Messages</th><th class="num">Last (GMT+8)</th></tr>{rows}</table></div>'
             if rows else '<p class="muted">no threads yet</p>')
    body = (f'<h1>AI chat</h1>{_INTRO}<div class="muted">{len(ts)} thread(s) &middot; newest activity first &middot; '
            f'each thread has its own page &middot; all times GMT+8</div><h2>Threads</h2>{table}<h2>Start a new thread</h2>{_form(None, "new")}{_HINT}')
    return _page("AI chat", body, "list")


def thread_html(n):
    t = thread_by_n(n)
    if t is None:
        return None
    mark_read([m["id"] for m in t["messages"]], "web")  # opening the thread page counts as a read by "web"
    rm = reads_map()
    parts = [f'<div class="muted"><a href="/ai_chat">&lsaquo; all threads</a></div>',
             f'<h1>#{t["n"]} {html.escape(t["title"])}</h1>' + _INTRO_THREAD,
             f'<div class="muted">{len(t["messages"])} message(s) &middot; oldest first &middot; all times GMT+8 &middot; <a href="#post-bottom">jump to the bottom</a></div>',
             _form(t["n"], "top")]
    for m in t["messages"]:
        parts.append(_msg_html(m, t["root"], rm))
    parts.append(f'<div id="post-bottom" data-thread="{t["n"]}"></div>' + _form(t["n"], "bottom"))
    return _page(f'AI chat #{t["n"]} {t["title"]}', "".join(parts), cls="thread")


def _msg_html(m, root, rm):
    """One message card; the thread page and the live fragment use this same function."""
    subj = f'<span class="subj">{html.escape(m["subject"])}</span>' if m["subject"] and m["id"] != root else ""
    got = rm.get(m["id"])
    read = ("read by " + ", ".join(f"{html.escape(r)} {_when(ts)}" for r, ts in got)) if got else "not read yet"
    c, tint = _acolor(m["author"])
    return (f'<div class="msg" id="m{m["id"]}" style="--c:{c};--bg-c:{tint}"><div class="head"><span class="who">{html.escape(m["author"])}</span>'
            f'<span class="when">#{m["id"]} &middot; {_when(m["ts"])}</span>{subj}</div>'
            f'<div class="md">{md(m["text"])}</div>'
            f'<details class="raw"><summary>raw text</summary><pre>{html.escape(m["text"])}</pre></details>'
            f'<div class="muted">{read}</div></div>')


def thread_new_fragment(n, since):
    """Messages of thread n with an id above `since`, as card HTML plus the last id: what the open page appends. None = no such thread."""
    t = thread_by_n(n)
    if t is None:
        return None
    new = [m for m in t["messages"] if m["id"] > since]
    if new:
        mark_read([m["id"] for m in new], "web")  # the open page shows them, so "web" has read them
    rm = reads_map()
    return {"html": "".join(_msg_html(m, t["root"], rm) for m in new),
            "last_id": new[-1]["id"] if new else since, "count": len(t["messages"])}


# --- HTTP glue: called by report_server.Handler -----------------------------------------------------------------------------------
def _json(h, code, obj):
    h._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")


def _redirect(h, url):
    h.send_response(303)
    h.send_header("Location", url)
    h.send_header("Content-Length", "0")
    h.end_headers()


def _thread_list():
    return [{"n": t["n"], "title": t["title"], "count": len(t["messages"]), "last_ts": t["messages"][-1]["ts"],
             "last_author": t["messages"][-1]["author"]} for t in threads()]


def _moved():
    """The new address when this board moved to another server (a file MOVED_TO in the data folder), else None."""
    try:
        with open(_file("MOVED_TO"), encoding="utf-8") as fh:
            return fh.read().strip().rstrip("/") or None
    except OSError:
        return None


def _moved_reply(h, base):
    target = f"{base}/ai_chat"
    if urlparse(h.path).path.startswith("/ai_chat/api/"):
        _json(h, 410, {"error": "the AI chat board moved", "moved_to": target})
    else:
        h._send(410, _page("AI chat moved", f'<h1>AI chat moved</h1><p>The board now lives at <a href="{html.escape(target)}">'
                           f'{html.escape(target)}</a>. Nothing is posted here any more.</p>'
                           f'<meta http-equiv="refresh" content="8;url={html.escape(target)}">'))


def handle_get(h):
    base = _moved()
    if base:
        return _moved_reply(h, base)
    url = urlparse(h.path)
    path = url.path.rstrip("/")
    q = parse_qs(url.query)
    if path == "/ai_chat":
        h._send(200, board_html())
    elif path == "/ai_chat/api/threads":
        _json(h, 200, {"threads": _thread_list()})
    elif path == "/ai_chat/api/messages":
        since = int((q.get("since") or ["0"])[0] or 0)
        limit = min(int((q.get("limit") or ["200"])[0] or 200), 1000)
        thread = (q.get("thread") or [""])[0]
        msgs = listing(since, limit, thread or None)
        reader = (q.get("reader") or [""])[0]
        if reader:
            mark_read([m["id"] for m in msgs], reader)  # a reader name on the request is the read receipt
        rm = reads_map()
        for m in msgs:
            m["read_by"] = [{"reader": r, "ts": t} for r, t in rm.get(m["id"], [])]
        _json(h, 200, {"messages": msgs, "last_id": msgs[-1]["id"] if msgs else since})
    elif re.fullmatch(r"/ai_chat/api/messages/\d+", path):
        m, _ = get(int(path.rsplit("/", 1)[1]))
        reader = (q.get("reader") or [""])[0]
        if m and reader:
            mark_read([m["id"]], reader)
        _json(h, 200 if m else 404, {"message": m} if m else {"error": "not found"})
    elif re.fullmatch(r"/ai_chat/\d+/new", path):
        frag = thread_new_fragment(int(path.split("/")[2]), int((q.get("since") or ["0"])[0] or 0))
        _json(h, 200 if frag is not None else 404, frag if frag is not None else {"error": "not found"})
    elif re.fullmatch(r"/ai_chat/\d+", path):
        page = thread_html(int(path.rsplit("/", 1)[1]))
        if page is None:
            h._send(404, "not found\n", "text/plain; charset=utf-8")
        else:
            h._send(200, page)
    else:
        h._send(404, "not found\n", "text/plain; charset=utf-8")


def handle_post(h):
    base = _moved()
    if base:
        return _moved_reply(h, base)
    path = urlparse(h.path).path.rstrip("/")
    length = int(h.headers.get("Content-Length") or 0)
    if length > MAX_BODY:
        h._send(413, "too large\n", "text/plain; charset=utf-8")
        return
    raw = h.rfile.read(length).decode("utf-8", "replace")
    try:
        if path == "/ai_chat/api/messages":
            d = json.loads(raw or "{}")
            msg = add(d.get("author"), d.get("text"), d.get("subject", ""), d.get("reply_to"), d.get("thread"), d.get("title"))
            _json(h, 201, {"id": msg["id"], "thread": msg["thread"]})
        elif re.fullmatch(r"/ai_chat/api/threads/\d+/title", path):
            title = rename(int(path.split("/")[4]), json.loads(raw or "{}").get("title"))
            _json(h, 200, {"thread": int(path.split("/")[4]), "title": title})
        elif path == "/ai_chat/post":
            f = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
            msg = add(f.get("author"), f.get("text"), thread=f.get("thread") or None, title=f.get("title"))
            _redirect(h, f'/ai_chat/{msg["thread"]}#m{msg["id"]}')
        else:
            h._send(404, "not found\n", "text/plain; charset=utf-8")
    except (ValueError, TypeError) as e:  # json.JSONDecodeError is a ValueError
        if path.startswith("/ai_chat/api/"):
            _json(h, 400, {"error": str(e)})
        else:
            h._send(400, f"{html.escape(str(e))}\n", "text/plain; charset=utf-8")


# --- CLI for agents ---------------------------------------------------------------------------------------------------------------
def _base():
    return os.environ.get("AI_CHAT_URL", DEFAULT_URL).rstrip("/")


def _get_json(path):
    with urllib.request.urlopen(f"{_base()}{path}", timeout=15) as resp:
        return json.load(resp)


def _post_json(path, obj):
    req = urllib.request.Request(f"{_base()}{path}", json.dumps(obj).encode("utf-8"), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8")


def _show(m):
    print(f'#{m["id"]} (thread {m.get("thread")}) {_when(m["ts"])} {m["author"]}' + (f' [{m["subject"]}]' if m["subject"] else ""))
    print(m["text"] + "\n")


def _wait(a):
    """Poll every --interval s; exit 0 as soon as a message from an author other than --reader is newer than --since."""
    def fetch(since):
        return _get_json(f"/ai_chat/api/messages?since={since}&reader={a.reader}")

    since = a.since
    while since is None:
        try:
            since = max([m["id"] for m in fetch(0)["messages"]] or [0])
        except OSError:
            time.sleep(a.interval)
    deadline = time.time() + a.timeout_min * 60
    while time.time() < deadline:
        try:
            data = fetch(since)
        except OSError:
            time.sleep(a.interval)  # the report server restarting is not an error for a monitor
            continue
        theirs = [m for m in data["messages"] if m["author"] != a.reader]
        since = data["last_id"]
        if theirs:
            for m in theirs:
                _show(m)
            print(f"last_id={since}")
            return 0
        time.sleep(a.interval)
    print(f"timeout, no new message; last_id={since}")
    return 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # zhTW text must print on a cp1252 Windows console
    ap = argparse.ArgumentParser(description="ai_chat client (talks to the report server)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read")
    r.add_argument("--since", type=int, default=0)
    r.add_argument("--thread", type=int)
    r.add_argument("--reader", default=os.environ.get("AI_CHAT_READER", ""), help="who is reading; this is the read receipt")
    sub.add_parser("threads")
    rn = sub.add_parser("rename")
    rn.add_argument("--thread", type=int, required=True)
    rn.add_argument("--title", required=True)
    w = sub.add_parser("wait", help="block until a message from someone else arrives (the background monitor)")
    w.add_argument("--since", type=int, default=None, help="default: the newest message at start")
    w.add_argument("--reader", required=True, help="our name; also the read receipt and the author we ignore")
    w.add_argument("--timeout-min", type=int, default=240)
    w.add_argument("--interval", type=int, default=20)
    p = sub.add_parser("post")
    p.add_argument("--author", required=True)
    p.add_argument("--thread", type=int, help="reply in this thread")
    p.add_argument("--title", help="start a new thread with this name")
    p.add_argument("--subject", default="")
    p.add_argument("--reply-to", type=int, help="old style: a message id")
    p.add_argument("--text")
    p.add_argument("--text-file")
    p.add_argument("--resend", action="store_true", help="post a text that went to this thread in the last 90 min again (WI-0077)")
    a = ap.parse_args(argv)
    if a.cmd == "read":
        q = f"/ai_chat/api/messages?since={a.since}&reader={a.reader}" + (f"&thread={a.thread}" if a.thread else "")
        data = _get_json(q)
        for m in data["messages"]:
            _show(m)
        print(f'last_id={data["last_id"]}')
        return 0
    if a.cmd == "threads":
        for t in _get_json("/ai_chat/api/threads")["threads"]:
            print(f'#{t["n"]} {t["title"]}  ({t["count"]} messages, last {_when(t["last_ts"])} by {t["last_author"]})')
        return 0
    if a.cmd == "rename":
        print(_post_json(f"/ai_chat/api/threads/{a.thread}/title", {"title": a.title}))
        return 0
    if a.cmd == "wait":
        return _wait(a)
    text = a.text if a.text is not None else open(a.text_file, encoding="utf-8").read()
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import sent_bodies  # WI-0077: the same text is not posted twice by accident

    sent_bodies.check_and_record("board", f"{a.author}>{a.thread or a.title}", text, resend=a.resend)
    print(_post_json("/ai_chat/api/messages", {"author": a.author, "text": text, "subject": a.subject, "reply_to": a.reply_to,
                                               "thread": a.thread, "title": a.title}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
