"""Gate history: which health gate was in force when (data in gate_history.json).

  handles_gate_for_major(144) -> 1500      handles_gate_for_major(145) -> 1300
  page_html()                              the /doc/gates page of the report server

The report uses handles_gate_for_major() so a page for release N shows the handle limit that release was gated with,
whatever the current code says. Pure Python and JSON: it works on the report VM, which has no GRS checkout. The page uses
only the report's own look (card, table, cpu-explain); it adds no style of its own.
"""

import html
import json
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate_history.json")


def _load():
    with open(_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def handles_gate_for_major(major):
    """The handle FAIL limit for a client major version, or None when major is unknown."""
    if major is None:
        return None
    gate = next(g for g in _load()["gates"] if g["id"] == "handles-fail")
    limit = None
    for p in gate["periods"]:
        if p["from_major"] <= int(major):
            limit = p["limit_value"]
    return limit


def page_html():
    """Body of the gate-history page: one card with one table per gate, oldest period first."""
    import report  # the report module owns the look (table / card); imported here so the data helper above works alone

    esc = html.escape
    parts = [
        '<div class="card"><h2>Gate history</h2><div class="cpu-explain">Which gate was in force when. A release is judged '
        "by the gate of its own time, so a newer gate never changes the result of an older release. Dates are commit dates "
        'on the test-code main branch; a "planned" row is decided but not in the code yet. '
        '<a href="/doc">All documents</a></div></div>'
    ]
    for g in _load()["gates"]:
        rows = []
        for p in g["periods"]:
            end = p.get("to") or ("" if p.get("status") == "planned" else "now")
            span = p.get("from", "") + (f" to {end}" if end else " on")
            rows.append([span, p["limit"], p.get("status", ""), p.get("source", "")])
        parts.append(
            f'<div class="card"><h2>{esc(g["name"])}</h2><div class="cpu-explain">{esc(g["rule"])}</div>'
            + report.table(
                ["Period", "Limit", "Status", "Source"],
                rows,
                highlight=lambda rd: "warn" if rd.get("Status") == "planned" else "",
            )
            + "</div>"
        )
    return "".join(parts)


if __name__ == "__main__":
    assert handles_gate_for_major(140) == 1500 and handles_gate_for_major(144) == 1500
    assert handles_gate_for_major(145) == 1300 and handles_gate_for_major(150) == 1300
    assert handles_gate_for_major(None) is None
    print("gate_history OK:", {m: handles_gate_for_major(m) for m in (140, 144, 145)})
