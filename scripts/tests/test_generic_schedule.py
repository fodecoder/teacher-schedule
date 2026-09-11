"""Genericity checks: a school with a completely different daily rhythm.

These build a minimal config for a school that runs a single uninterrupted
block every day (e.g. 8:00-13:30, no afternoon, no mensa, no special teacher
roles) and check that ``data.load_config`` accepts it and that the derived
grid behaves correctly, without touching the hard-coded example school at
all. This is the concrete case from the project brief: "se il mio orario
fosse 8-13:30 invece di 8:10-12:40, con vincoli diversi, deve poterlo usare".
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import data

_EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config.example.yaml"


@pytest.fixture(autouse=True)
def _restore_example_config():
    """Every test here calls ``data.load_config`` directly with its own
    throwaway file, which overwrites the module-global state (``data.DAYS``
    & co.) for the whole process. Reload the shared example config afterward
    so other test modules are unaffected regardless of execution order."""
    yield
    data.load_config(_EXAMPLE_CONFIG)


_MINIMAL_CONFIG = """
schedule:
  days: ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì"]
  slots:
    - {id: m1, kind: teaching, label: "8:00-9:00"}
    - {id: m2, kind: teaching, label: "9:00-10:00"}
    - {id: ricreazione, kind: interval, label: "10:00-10:15"}
    - {id: m3, kind: teaching, label: "10:15-11:15"}
    - {id: m4, kind: teaching, label: "11:15-12:15"}
    - {id: m5, kind: teaching, label: "12:15-13:30"}
  consecutive_pairs: [[m1, m2], [m3, m4]]

classes: ["A", "B"]
teachers: ["Rossi", "Bianchi"]

courses:
  - {teacher: "Rossi", classes: ["A"], subject: "Italiano", hours: 5}
  - {teacher: "Bianchi", classes: ["B"], subject: "Italiano", hours: 5}

constraint_params:
  target_hours: 25

weights:
  monte_ore_target: 100
  uscita_anticipata: 0
  p1_p2_stessa_docente: 0
  intervallo_con_s2_o_s3: 0
  mensa_con_s4_o_p1: 0
  materie_2h_giorni_diversi: 0
  un_solo_pomeriggio: 0
  buchi_orari: 0
  max_2h_giorno_stessa_materia: 0
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "generic.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_single_block_schedule_with_no_optional_roles_loads(
    tmp_path: Path,
) -> None:
    """No extended_days, no mensa, no special teacher roles: everything
    optional must gracefully default away instead of raising."""
    config = data.load_config(_write(tmp_path, _MINIMAL_CONFIG))

    assert config.days == (
        "Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì",
    )
    assert config.extended_days == ()
    assert config.afternoon_slots == ()
    assert config.lunch_slot is None
    assert config.interval_slot == "ricreazione"
    assert config.morning_slots == ("m1", "m2", "m3", "m4", "m5")
    assert config.early_exit_teacher is None
    assert config.no_afternoon_teacher is None
    assert config.teaching_only_teacher is None
    assert config.reinforcement_teacher is None
    assert config.reinforcement_hours == {}

    for day in config.days:
        assert data.teaching_slots(day) == (
            "m1", "m2", "m3", "m4", "m5",
        )
    assert data.WEEKLY_TEACHING_SLOTS == 5 * 5


def test_extended_only_slot_requires_extended_days(tmp_path: Path) -> None:
    broken = _MINIMAL_CONFIG.replace(
        '- {id: m5, kind: teaching, label: "12:15-13:30"}',
        '- {id: m5, kind: teaching, label: "12:15-13:30", extended_only: true}',
    )
    with pytest.raises(data.ConfigError):
        data.load_config(_write(tmp_path, broken))


def test_early_exit_role_requires_extended_days(tmp_path: Path) -> None:
    broken = _MINIMAL_CONFIG + textwrap.dedent(
        """
        teacher_roles:
          early_exit:
            teacher: "Rossi"
        """
    )
    with pytest.raises(data.ConfigError):
        data.load_config(_write(tmp_path, broken))


_TWO_LUNCH_SLOTS_CONFIG = """
schedule:
  days: ["Lunedì", "Martedì"]
  extended_days: ["Martedì"]
  slots:
    - {id: m1, kind: teaching, label: "8:00-9:00"}
    - {id: p1, kind: teaching, label: "14:00-15:00", extended_only: true}
    - {id: mensa1, kind: lunch, extended_only: true}
    - {id: mensa2, kind: lunch, extended_only: true}

classes: ["A"]
teachers: ["Rossi"]
courses:
  - {teacher: "Rossi", classes: ["A"], subject: "Italiano", hours: 1}
constraint_params:
  target_hours: 2
weights:
  monte_ore_target: 1
  uscita_anticipata: 0
  p1_p2_stessa_docente: 0
  intervallo_con_s2_o_s3: 0
  mensa_con_s4_o_p1: 0
  materie_2h_giorni_diversi: 0
  un_solo_pomeriggio: 0
  buchi_orari: 0
  max_2h_giorno_stessa_materia: 0
"""


def test_two_lunch_slots_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(data.ConfigError):
        data.load_config(_write(tmp_path, _TWO_LUNCH_SLOTS_CONFIG))
