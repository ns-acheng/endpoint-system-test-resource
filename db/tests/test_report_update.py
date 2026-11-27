"""report_update.py: a verdict cell is a ticket number or typed words, nothing an agent wrote."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "system_test_db")))
import report_update as ru  # noqa: E402


def test_a_ticket_number_is_the_cell():
    assert ru.cell_value("ENG-1455025", None) == "ENG-1455025"


@pytest.mark.parametrize("bad", ["ENG-1455025 CNSCom2::recvHandshakeReqfromClient", "crash", "ENG-", "eng-12", "ENG-12 and more"])
def test_text_that_is_not_a_ticket_number_is_refused(bad):
    with pytest.raises(ValueError):
        ru.cell_value(bad, None)


def test_typed_words_are_kept_exactly_and_must_be_one_line():
    assert ru.cell_value(None, "  duplicated ") == "duplicated"
    with pytest.raises(ValueError):
        ru.cell_value(None, "two\nlines")
    with pytest.raises(ValueError):
        ru.cell_value(None, "ab")


def test_both_or_neither_is_refused():
    with pytest.raises(ValueError):
        ru.cell_value("ENG-1", "words")
    with pytest.raises(ValueError):
        ru.cell_value(None, None)


def test_write_verdicts_keeps_the_other_entries(tmp_path):
    p = tmp_path / "v.json"
    p.write_text(json.dumps({"LOCAL1:184": "duplicated", "MAC2:361": "old"}), encoding="utf-8")
    keys = ru.write_verdicts(str(p), "MAC2", [361, 362], "ENG-1455025")
    assert keys == ["MAC2:361", "MAC2:362"]
    assert json.loads(p.read_text(encoding="utf-8")) == {"LOCAL1:184": "duplicated", "MAC2:361": "ENG-1455025", "MAC2:362": "ENG-1455025"}


def test_link_only_links_and_leaves_the_cell_alone(tmp_path, monkeypatch):
    p = tmp_path / "v.json"
    p.write_text(json.dumps({"MAC2:363": "ENG-1455025"}), encoding="utf-8")
    calls = []

    class Done:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(ru, "VERDICTS", str(p))
    monkeypatch.setattr(ru.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Done())
    assert ru.main(["set", "--lane", "MAC2", "--build", "363", "--ticket", "ENG-1457084", "--link-only"]) == 0
    assert json.loads(p.read_text(encoding="utf-8")) == {"MAC2:363": "ENG-1455025"}
    assert len(calls) == 1 and "ENG-1457084" in calls[0] and "363" in calls[0]
    assert ru.main(["set", "--lane", "MAC2", "--build", "363", "--words", "x y z", "--link-only"]) == 2  # no ticket, nothing linked


def test_crash_rows_reads_lane_build_and_verdict():
    page = (
        "<h2>Crash / BSOD dumps</h2><table><tr><th>gate</th><th>t</th><th>lane</th><th>build</th><th>dt</th><th>v</th><th>detail</th><th>verdict</th></tr>"
        "<tr><td>crash_dump</td><td>x</td><td>MAC2</td><td>361</td><td>d</td><td>145.0</td><td>detail</td><td>ENG-1455025</td></tr></table>"
    )
    assert ru.crash_rows(page) == [("MAC2", "361", "ENG-1455025")]
