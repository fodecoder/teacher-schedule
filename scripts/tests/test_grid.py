"""Invariants of the time grid loaded from config.example.yaml (no solver).

The grid is config-driven (see ``data._parse_schedule``), so these tests need
the ``config`` fixture to have run ``data.load_config`` first — unlike the
old hard-coded grid, ``data.DAYS`` & co. do not exist before that."""

from __future__ import annotations

import pytest

import data

pytestmark = pytest.mark.usefixtures("config")


def test_weekly_teaching_slots_is_24() -> None:
    assert data.WEEKLY_TEACHING_SLOTS == 24


def test_short_days_have_four_slots_long_days_six() -> None:
    for day in data.DAYS:
        slots = data.teaching_slots(day)
        expected = 6 if day in data.AFTERNOON_DAYS else 4
        assert len(slots) == expected, day
        assert len(set(slots)) == len(slots)


def test_unknown_day_rejected() -> None:
    try:
        data.teaching_slots("Sabato")
    except ValueError:
        pass
    else:  # pragma: no cover - the call must raise
        raise AssertionError("teaching_slots ha accettato un giorno inesistente")


def test_consecutive_pairs_are_real_slots() -> None:
    valid = set(data.MORNING_SLOTS) | set(data.AFTERNOON_SLOTS)
    for first, second in data.CONSECUTIVE_PAIRS:
        assert first in valid and second in valid


def test_full_slot_order_covers_every_slot_once() -> None:
    expected = (
        set(data.MORNING_SLOTS)
        | set(data.AFTERNOON_SLOTS)
        | {data.INTERVAL_SLOT, data.LUNCH_SLOT}
    )
    assert set(data.FULL_SLOT_ORDER) == expected
    assert len(data.FULL_SLOT_ORDER) == len(expected)
