#!/usr/bin/env python
"""report_update.py -- the one way to change what the report page says about a run: a ticket link and the verdict cell.

    python report_update.py set  --lane MAC2 --build 361 362 363 --ticket ENG-1455025
    python report_update.py set  --lane LOCAL1 --build 184 --words "duplicated"
    python report_update.py live --release 145

The rule behind it, as typed in the chat on 2026-11-27: "to add verdict for the kt number or owner;s word, no angent's word allowed".

set   links the runs to the ticket (db.py issue set) and writes the verdict cell of every crash row of those builds into
      crash_verdicts.json ("<lane>:<build>"). The cell is a TICKET NUMBER, or the exact words of the person who asked, given
      with --words and copied unchanged. Nothing else is accepted: no description, no function name, no count, no judgement
      of an agent. The tool refuses a --ticket that is not a ticket number and a text that spans lines. It does not commit,
      push or deploy.
live  reads the live report page (after the 30 s page cache) and prints the verdict cell of every row of the crash table:
      a change is done when that cell is read here, not when the file is pushed.
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
VERDICTS = os.path.join(HERE, "crash_verdicts.json")
DB = os.path.join(HERE, "db.py")
TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
LIVE = os.environ.get("REPORT_URL", "http://10.136.218.74:8080")


def cell_value(ticket, words):
    """The text of the verdict cell: a ticket number, or the requester's words verbatim. Raises ValueError for anything else."""
    if bool(ticket) == bool(words):
        raise ValueError("give exactly one of --ticket (a ticket number) or --words (the exact words that were typed to you)")
    if ticket:
        if not TICKET_RE.match(ticket):
            raise ValueError(f"'{ticket}' is not a ticket number like ENG-1455025; a verdict cell holds a ticket number or typed words only")
        return ticket
    text = words.strip()
    if len(text) < 3 or "\n" in text:
        raise ValueError("--words must be one line of the exact typed words, 3 characters or more")
    return text


def write_verdicts(path, lane, builds, value):
    """Set '<lane>:<build>' for every build; keeps the other entries and their order. Returns the keys written."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    keys = [f"{lane}:{b}" for b in builds]
    for k in keys:
        data[k] = value
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return keys


def crash_rows(page):
    """[(lane, build, verdict cell)] of the 'Crash / BSOD dumps' table of a report page."""
    i = page.find("Crash / BSOD dumps")
    seg = page[i:] if i >= 0 else ""
    tb = seg[: seg.find("</table>")]
    out = []
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tb, re.S):
        c = [html.unescape(re.sub(r"<[^>]+>", "", x)).strip() for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
        if len(c) >= 7 and c[0].lower() != "gate":
            out.append((c[2], c[3], c[-1]))
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="link the runs to a ticket and write the verdict cell")
    s.add_argument("--lane", required=True, help="the lane as the crash table shows it: MAC2, LOCAL1, LOCAL2, REG ...")
    s.add_argument("--build", required=True, nargs="+", type=int)
    s.add_argument("--ticket", help="ticket number, for example ENG-1455025")
    s.add_argument("--words", help="the exact typed words for the cell (only when no ticket number was given)")
    s.add_argument("--no-link", action="store_true", help="write the cell only, do not touch the database link")
    s.add_argument("--link-only", action="store_true", help="link the runs to the ticket only; the verdict cell is not written (a second ticket on a run)")
    lv = sub.add_parser("live", help="read the verdict cells of the live page")
    lv.add_argument("--release", required=True, help="bare release number, for example 145")
    lv.add_argument("--wait", type=int, default=31, help="seconds to wait first for the 30 s page cache (default 31)")
    a = ap.parse_args(argv)
    if a.cmd == "set":
        try:
            value = cell_value(a.ticket, a.words)
        except ValueError as e:
            print("REFUSED:", e)
            return 2
        if a.ticket and not a.no_link:
            for b in a.build:
                r = subprocess.run([sys.executable, DB, "issue", "set", a.ticket, "--build", str(b), "--job", a.lane], capture_output=True, text=True)
                print(f"link {a.lane}:{b} -> {a.ticket}: exit {r.returncode}")
                if r.returncode != 0:
                    print((r.stdout + r.stderr).strip()[-300:])
                    return 1
        if a.link_only:
            if not a.ticket or a.no_link:
                print("REFUSED: --link-only needs --ticket and cannot be combined with --no-link")
                return 2
            print("verdict cell not written (--link-only); the link shows in the Tickets table and the gate rows of the run")
            return 0
        print("cells written:", ", ".join(f"{k} = {value!r}" for k in write_verdicts(VERDICTS, a.lane, a.build, value)))
        print("next: commit and push crash_verdicts.json, run update_report_vm.py, then report_update.py live")
        return 0
    time.sleep(max(a.wait, 0))
    page = urllib.request.urlopen(f"{LIVE}/report/{a.release}", timeout=120).read().decode("utf-8", "replace")
    rows = crash_rows(page)
    for lane, build, cell in rows:
        print(f"{lane} {build} | verdict cell: {cell!r}")
    print(f"{len(rows)} row(s) read from {LIVE}/report/{a.release}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
