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
from ortools.sat.python import cp_model

import data
import model

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
        assert config.teaching_slots(day) == (
            "m1", "m2", "m3", "m4", "m5",
        )
    assert config.weekly_teaching_slots == 5 * 5


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


_EARLY_EXIT_NO_OWN_COURSES_CONFIG = """
schedule:
  days: ["Lunedì", "Martedì"]
  extended_days: ["Martedì"]
  slots:
    - {id: m1, kind: teaching, label: "8:00-9:00"}
    - {id: m2, kind: teaching, label: "9:00-10:00"}
    - {id: p1, kind: teaching, label: "14:00-15:00", extended_only: true}

classes: ["A"]
teachers: ["Rossi", "Bianchi"]

courses:
  - {teacher: "Bianchi", classes: ["A"], subject: "Italiano", hours: 5}

constraint_params:
  target_hours: 5

teacher_roles:
  early_exit:
    teacher: "Rossi"

weights:
  monte_ore_target: 0
  uscita_anticipata: 0
  p1_p2_stessa_docente: 0
  intervallo_con_s2_o_s3: 0
  mensa_con_s4_o_p1: 0
  materie_2h_giorni_diversi: 0
  un_solo_pomeriggio: 0
  buchi_orari: 0
  max_2h_giorno_stessa_materia: 0
"""


def test_h6_binds_afternoon_even_with_no_last_morning_slot_literals(
    tmp_path: Path,
) -> None:
    """Regression test for the H6 indentation bug in
    ``_add_early_exit_rules``: the mensa/afternoon constraints must be
    posted once per day, not nested inside the loop over the early-exit
    teacher's last-morning-slot teaching literals.

    ``Rossi`` is the early_exit teacher but has no courses of their own
    (all teaching is done by ``Bianchi``), so
    ``_teaching_literals("Rossi", day, slot)`` is empty for every day and
    slot. With only one extended day, H6's ``sum(marker) == 1`` forces
    that day's marker to 1, which in turn (once the fix posts
    ``sum(afternoon_literals) >= marker`` at the day level, unconditional
    on the last-morning-slot loop) requires at least one afternoon
    teaching literal for Rossi on that day — impossible, since Rossi
    never teaches at all. The model must therefore be INFEASIBLE.

    Before the fix, the day-level mensa/afternoon block was nested inside
    the (here always empty) last-morning-slot loop and so never ran,
    leaving the marker disconnected from real afternoon lessons and the
    model spuriously FEASIBLE.
    """
    config = data.load_config(_write(tmp_path, _EARLY_EXIT_NO_OWN_COURSES_CONFIG))

    builder = model.TimetableModelBuilder(config, optimize=True)
    cp_sat_model = builder.build()
    solver = model.build_solver(time_limit=5.0, log_progress=False)
    status = solver.Solve(cp_sat_model)

    assert status == cp_model.INFEASIBLE
