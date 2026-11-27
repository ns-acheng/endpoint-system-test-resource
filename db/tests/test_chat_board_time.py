"""The ai_chat board shows every time in GMT+8, whatever the machine that renders it is set to."""

import calendar
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "system_test_db"))

import chat_board as cb  # noqa: E402


def test_when_is_gmt_plus_8():
    assert cb._when(0) == "1970-01-01 08:00"


def test_a_utc_evening_lands_on_the_next_day_in_gmt_plus_8():
    assert cb._when(calendar.timegm((2026, 11, 7, 16, 30, 0))) == "2026-11-08 00:30"
    assert cb._when(calendar.timegm((2026, 11, 8, 2, 5, 0))) == "2026-11-08 10:05"


def test_when_never_asks_for_the_local_timezone(monkeypatch):
    # the old code called time.localtime(); a machine in another zone showed another time for the same message
    def boom(*_a):
        raise AssertionError("localtime must not be used")

    monkeypatch.setattr(time, "localtime", boom)
    assert cb._when(86400) == "1970-01-02 08:00"
