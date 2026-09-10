"""Data-integrity checks on the YAML config, without running the solver.

These run in milliseconds and are the first thing to trust when adapting
``config.yaml`` to a new school year: if the hours do not add up, a test here
fails long before the CP-SAT model is even built.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import data

pytestmark = pytest.mark.usefixtures("config")

WEEKLY = 24  # teaching slots per class, from the grid


def _titolari_hours(class_: str) -> int:
    return sum(c.hours for c in data.COURSES if class_ in c.classes)


def _expert_hours(class_: str) -> int:
    return sum(1 for e in data.EXPERT_FIXED if e.class_ == class_)


def test_every_class_has_24_teaching_slots() -> None:
    assert data.WEEKLY_TEACHING_SLOTS == WEEKLY


def test_per_class_hours_balance_to_24() -> None:
    for class_ in data.CLASSES:
        titolari = _titolari_hours(class_)
        expert = _expert_hours(class_)
        reinforcement = data.REINFORCEMENT_HOURS.get(class_, 0)
        total = titolari + expert + reinforcement
        assert total == WEEKLY, (
            f"{class_}: titolari {titolari} + esperti {expert} + "
            f"potenziamento {reinforcement} = {total}, atteso {WEEKLY}"
        )


def test_reinforcement_fills_exactly_the_leftover_slots() -> None:
    for class_, hours in data.REINFORCEMENT_HOURS.items():
        leftover = WEEKLY - _titolari_hours(class_) - _expert_hours(class_)
        assert hours == leftover, (
            f"potenziamento in {class_}: {hours}h contro {leftover}h liberi"
        )


def test_total_hours_across_all_classes() -> None:
    titolari = sum(_titolari_hours(c) for c in data.CLASSES)
    expert = sum(_expert_hours(c) for c in data.CLASSES)
    reinforcement = sum(data.REINFORCEMENT_HOURS.values())
    assert titolari + expert + reinforcement == WEEKLY * len(data.CLASSES)


def test_courses_reference_known_teachers_and_classes() -> None:
    for course in data.COURSES:
        assert course.teacher in data.TEACHERS, course.label
        for class_ in course.classes:
            assert class_ in data.CLASSES, course.label
        assert course.hours >= 1


def test_expert_hours_have_no_duplicate_slot() -> None:
    seen = set()
    for hour in data.EXPERT_FIXED:
        key = (hour.class_, hour.day, hour.slot)
        assert key not in seen, key
        seen.add(key)
        assert hour.slot in data.teaching_slots(hour.day)


def test_role_teachers_exist() -> None:
    for teacher in (
        data.EARLY_EXIT_TEACHER,
        data.NO_AFTERNOON_TEACHER,
        data.TEACHING_ONLY_TEACHER,
        data.REINFORCEMENT_TEACHER,
    ):
        assert teacher in data.TEACHERS


def test_teaching_only_teacher_is_already_at_target() -> None:
    teaching = sum(
        c.hours for c in data.COURSES if c.teacher == data.TEACHING_ONLY_TEACHER
    )
    assert teaching * 2 == data.TARGET_HALF_HOURS, (
        f"il docente 'solo didattica' ha {teaching}h di didattica, il target "
        f"è {data.TARGET_HALF_HOURS / 2:g}h: 0h di assistenza sarebbe "
        "infattibile"
    )


def test_spread_and_two_hour_subjects_are_consistent() -> None:
    for class_ in data.SPREAD_CLASSES:
        assert class_ in data.CLASSES
    # Ogni corso in una materia "da 2h" con esattamente 2 ore è ciò che il
    # vincolo di adiacenza (H5) governa; nessun corso di quelle materie deve
    # avere un monte ore che il modello non sa spezzare (1h o 2h).
    for course in data.COURSES:
        if course.subject in data.TWO_HOUR_SUBJECTS:
            assert course.hours in (1, 2), course.label


def test_weighted_exit_days_are_not_afternoon_days() -> None:
    for day in data.EARLY_EXIT_WEIGHTED_DAYS:
        assert day in data.DAYS
        assert day not in data.AFTERNOON_DAYS, (
            f"{day}: l'uscita anticipata pesata ha senso solo nei giorni "
            "senza pomeriggio"
        )


def test_load_config_rejects_unknown_teacher(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        textwrap.dedent(
            """
            classes: ["1ª"]
            teachers: ["Docente A"]
            expert_fixed: []
            courses:
              - {teacher: "Docente Z", classes: ["1ª"], subject: "X", hours: 1}
            reinforcement: {teacher: "Docente A", subject: "P", hours: {}}
            constraint_params:
              two_hour_subjects: []
              spread_classes: []
              target_hours: 22
              interval_credit_hours: 0.5
              lunch_credit_hours: 1
              lunch_supervisors_per_day: 2
              daily_subject_soft_cap: 2
            weights:
              monte_ore_target: 1
              uscita_anticipata: 1
              p1_p2_stessa_docente: 1
              intervallo_con_s2_o_s3: 1
              mensa_con_s4_o_p1: 1
              materie_2h_giorni_diversi: 1
              un_solo_pomeriggio: 1
              buchi_orari: 1
              max_2h_giorno_stessa_materia: 1
            teacher_roles:
              early_exit: {teacher: "Docente A"}
              no_afternoon: {teacher: "Docente A", day: "Giovedì"}
              teaching_only: {teacher: "Docente A"}
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(data.ConfigError):
        data.load_config(broken)


def test_load_config_rejects_missing_key(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text('classes: ["1ª"]\n', encoding="utf-8")
    with pytest.raises(data.ConfigError):
        data.load_config(broken)
