"""The roster of monitored processes: extracted from the suite's constants, shown in the report with or without data."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "system_test_db")))
import monitored  # noqa: E402
import report  # noqa: E402

WORKFLOW = '''
EPAIS_MAC_PROCESS = "MacOS/epais --config-dir"
DEFAULT_PROCESSES = ["stAgentSvc.exe", "epais.exe", "shuttle.exe"]
DEFAULT_PROCESSES_MAC = ["Netskope Client", EPAIS_MAC_PROCESS, "keystone"]
'''
HEALTH = '''
EPAIS_MAC_PROCESS = "MacOS/epais --config-dir"
OBSERVE_ONLY_PROCESSES = ("epais.exe", EPAIS_MAC_PROCESS, "keystone-auth")
PROCESS_MEM_CEILING_MB = {"shuttle.exe": 1024, "keystone": 1024}
'''


def test_extract_reads_lists_and_resolves_names():
    r = monitored.extract(WORKFLOW, HEALTH)
    assert r["windows"] == ["stAgentSvc.exe", "epais.exe", "shuttle.exe"]
    assert r["mac"] == ["Netskope Client", "MacOS/epais --config-dir", "keystone"]
    assert r["observe_only"] == ["epais.exe", "MacOS/epais --config-dir", "keystone-auth"]
    assert r["mem_ceiling_mb"] == {"shuttle.exe": 1024, "keystone": 1024}


def test_extract_refuses_a_constant_that_is_not_a_plain_list():
    with pytest.raises(ValueError):
        monitored.extract("DEFAULT_PROCESSES = make_list()\nDEFAULT_PROCESSES_MAC = []\n", HEALTH)
    with pytest.raises(ValueError):
        monitored.extract("DEFAULT_PROCESSES = [UNKNOWN_NAME]\nDEFAULT_PROCESSES_MAC = []\n", HEALTH)


def test_the_roster_file_of_the_repo_loads_and_names_the_shuttle_processes():
    r = monitored.load()
    assert r and "shuttle.exe" in r["windows"] and "keystone" in r["mac"]
    assert set(r["observe_only"]) <= set(r["windows"]) | set(r["mac"])


def test_a_missing_roster_file_is_none(tmp_path):
    assert monitored.load(str(tmp_path / "nope.json")) is None
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert monitored.load(str(tmp_path / "bad.json")) is None


ROSTER = {
    "windows": ["stAgentSvc.exe", "shuttle.exe", "sovin.exe"],
    "mac": ["Netskope Client", "keystone"],
    "observe_only": ["sovin.exe"],
    "mem_ceiling_mb": {"shuttle.exe": 1024},
    "source": {"ref": "origin/main", "sha": "a" * 40, "synced": "2026-11-27"},
}


def test_every_process_of_the_roster_is_listed_also_without_data():
    html = report.monitored_card(ROSTER, {("stAgentSvc.exe", "windows"): (12, 30)})
    for name in ("stAgentSvc.exe", "shuttle.exe", "sovin.exe", "Netskope Client", "keystone"):
        assert name in html, name
    assert html.count("monitored, no data in this release") == 4  # shuttle, sovin, Netskope Client, keystone
    assert "observe-only" in html and "1024 MB (first value)" in html


def test_a_process_with_data_shows_its_counts_and_the_plain_status():
    html = report.monitored_card(ROSTER, {("shuttle.exe", "windows"): (3, 9)})
    assert "<td>3</td><td>9</td><td>monitored</td>" in html


def test_a_process_with_data_that_is_not_on_the_roster_is_flagged():
    html = report.monitored_card(ROSTER, {("newproc.exe", "windows"): (2, 4)})
    assert "newproc.exe" in html and "NOT on the roster" in html


def test_the_platform_filter_keeps_only_that_platform():
    html = report.monitored_card(ROSTER, {}, platform="mac")
    assert "keystone" in html and "shuttle.exe" not in html


def test_a_missing_roster_says_so_instead_of_an_empty_card():
    assert "missing or unreadable" in report.monitored_card(None, {})


def test_the_source_of_the_roster_is_shown():
    assert "origin/main aaaaaaaa, synced 2026-11-27" in report.monitored_card(ROSTER, {})
