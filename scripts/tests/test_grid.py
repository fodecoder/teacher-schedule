"""Invariants of the time grid loaded from config.example.yaml (no solver).

The grid is config-driven (see ``data._parse_schedule``): these tests read it
from the ``config`` fixture's :class:`data.Config` instance."""

from __future__ import annotations

import data


def test_weekly_teaching_slots_is_24(config: data.Config) -> None:
    assert config.weekly_teaching_slots == 24


def test_short_days_have_four_slots_long_days_six(config: data.Config) -> None:
    for day in config.days:
        slots = config.teaching_slots(day)
        expected = 6 if day in config.extended_days else 4
        assert len(slots) == expected, day
        assert len(set(slots)) == len(slots)


def test_unknown_day_rejected(config: data.Config) -> None:
    try:
        config.teaching_slots("Sabato")
    except ValueError:
        pass
    else:  # pragma: no cover - the call must raise
        raise AssertionError("teaching_slots ha accettato un giorno inesistente")


def test_consecutive_pairs_are_real_slots(config: data.Config) -> None:
    valid = set(config.morning_slots) | set(config.afternoon_slots)
    for first, second in config.consecutive_pairs:
        assert first in valid and second in valid


def test_full_slot_order_covers_every_slot_once(config: data.Config) -> None:
    expected = (
        set(config.morning_slots)
        | set(config.afternoon_slots)
        | {config.interval_slot, config.lunch_slot}
    )
    assert set(config.full_slot_order) == expected
    assert len(config.full_slot_order) == len(expected)
