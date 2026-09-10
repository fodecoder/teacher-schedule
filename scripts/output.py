"""Serialisation of a solved timetable into docs/schema_output.json.

Keys are English, values (days, subjects, teacher and class names) stay
Italian, exactly as the schema prescribes.

Two notes on the shape produced here:

* ``by_class`` lists teaching slots only, like the schema example; interval and
  mensa duties belong to a teacher rather than to a class hour and are reported
  in ``by_teacher``, where each duty already carries its class.
* co-teaching is reported with ``shared_with`` on both sides: in ``by_class``
  it names the other classes attending, in ``by_teacher`` it names the other
  classes the teacher has in front of them during that same hour.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import data
import model as model_module
from model import Solution, Violation


def hours_from_half(half_hours: int) -> float:
    """Convert internal half hours to the hours used in the JSON."""
    return half_hours // 2 if half_hours % 2 == 0 else half_hours / 2


def _school_year(moment: datetime) -> str:
    """Return the school year label, e.g. ``2026-2027``.

    Italian school years start in September, so anything from September on
    belongs to ``year/year+1``.
    """
    if moment.month >= 9:
        return f"{moment.year}-{moment.year + 1}"
    return f"{moment.year - 1}-{moment.year}"


def _build_meta(
    solver_status: str, objective_value: Optional[int], moment: datetime
) -> Dict[str, object]:
    return {
        "school_year": _school_year(moment),
        "generated_at": moment.isoformat(timespec="seconds"),
        "solver_status": solver_status,
        "objective_value": objective_value,
        "days": list(data.DAYS),
        "slots": [dict(slot) for slot in data.SLOT_METADATA],
        "classes": list(data.CLASSES),
        "teachers": list(data.TEACHERS),
    }


def _build_by_class(solution: Solution) -> Dict[str, Dict[str, dict]]:
    by_class: Dict[str, Dict[str, dict]] = {
        class_: {day: {} for day in data.DAYS} for class_ in data.CLASSES
    }
    for lesson in solution.lessons:
        for class_ in lesson.classes:
            others = [
                other for other in lesson.classes if other != class_
            ] + [
                other
                for other in lesson.shared_with
                if other != class_ and other not in lesson.classes
            ]
            by_class[class_][lesson.day][lesson.slot] = {
                "subject": lesson.subject,
                "teacher": lesson.teacher,
                "shared_with": sorted(others),
            }
    # Keep the slots of each day in chronological order.
    for class_, days in by_class.items():
        for day, slots in days.items():
            by_class[class_][day] = {
                slot: slots[slot]
                for slot in data.FULL_SLOT_ORDER
                if slot in slots
            }
    return by_class


def _build_by_teacher(solution: Solution) -> Dict[str, dict]:
    schedules: Dict[str, Dict[str, dict]] = {
        teacher: {day: {} for day in data.DAYS} for teacher in data.TEACHERS
    }

    for lesson in solution.lessons:
        if lesson.teacher == data.EXPERT_LABEL:
            continue
        entry: Dict[str, object] = {
            "class": lesson.classes[0],
            "subject": lesson.subject,
            "activity_type": lesson.activity_type,
        }
        if len(lesson.classes) > 1:
            entry["shared_with"] = sorted(lesson.classes[1:])
        schedules[lesson.teacher][lesson.day][lesson.slot] = entry

    for teacher, class_, day in solution.interval_duties:
        schedules[teacher][day][data.INTERVAL_SLOT] = {
            "class": class_,
            "subject": None,
            "activity_type": model_module.ACTIVITY_INTERVAL,
        }

    for teacher, day in solution.lunch_duties:
        partners = sorted(
            other
            for other, other_day in solution.lunch_duties
            if other_day == day and other != teacher
        )
        schedules[teacher][day][data.LUNCH_SLOT] = {
            "class": None,
            "subject": None,
            "activity_type": model_module.ACTIVITY_LUNCH,
            "shared_with_teacher": partners[0] if partners else None,
        }

    by_teacher: Dict[str, dict] = {}
    for teacher in data.TEACHERS:
        ordered_days = {}
        for day in data.DAYS:
            slots = schedules[teacher][day]
            ordered_days[day] = {
                slot: slots[slot]
                for slot in data.FULL_SLOT_ORDER
                if slot in slots
            }
        by_teacher[teacher] = {
            "total_hours": hours_from_half(solution.half_hours[teacher]),
            "teaching_hours": hours_from_half(solution.teaching_half_hours[teacher]),
            "assistance_hours": hours_from_half(
                solution.assistance_half_hours[teacher]
            ),
            "schedule": ordered_days,
        }
    return by_teacher


def _serialise_violation(violation: Violation) -> Dict[str, object]:
    payload: Dict[str, object] = {"constraint": violation.constraint}
    if violation.class_ is not None:
        payload["class"] = violation.class_
    if violation.teacher is not None:
        payload["teacher"] = violation.teacher
    payload["detail"] = violation.detail
    return payload


def _build_constraint_report(
    violations: Sequence[Violation], hard_violations: Sequence[Dict[str, str]]
) -> Dict[str, List[dict]]:
    report: Dict[str, List[dict]] = {
        "hard_violations": [dict(item) for item in hard_violations],
        "medium_violations": [],
        "soft_violations": [],
    }
    for violation in violations:
        key = f"{violation.severity}_violations"
        if key not in report:
            raise ValueError(
                f"severità sconosciuta per il vincolo {violation.constraint!r}: "
                f"{violation.severity!r}"
            )
        report[key].append(_serialise_violation(violation))
    for key in ("medium_violations", "soft_violations"):
        report[key].sort(key=lambda item: (item["constraint"], item["detail"]))
    return report


def build_output(
    solution: Solution,
    hard_violations: Sequence[Dict[str, str]],
    moment: Optional[datetime] = None,
) -> Dict[str, object]:
    """Assemble the full JSON payload for a solved timetable."""
    moment = moment or datetime.now().astimezone()
    return {
        "meta": _build_meta(
            solution.status, solution.objective_value, moment
        ),
        "by_class": _build_by_class(solution),
        "by_teacher": _build_by_teacher(solution),
        "activity_types": list(model_module.ACTIVITY_TYPES),
        "constraint_report": _build_constraint_report(
            solution.violations, hard_violations
        ),
    }


def build_infeasible_output(
    solver_status: str,
    conflicting_groups: Sequence[str],
    moment: Optional[datetime] = None,
) -> Dict[str, object]:
    """Assemble the payload for a model that has no feasible solution.

    ``conflicting_groups`` are the HARD identifiers returned by the unsat-core
    analysis; they are reported as hard violations so the reader knows which
    combination of rules has to be renegotiated.
    """
    moment = moment or datetime.now().astimezone()
    hard_violations = [
        {
            "constraint": group,
            "detail": data.HARD_CONSTRAINT_LABELS.get(group, group),
        }
        for group in conflicting_groups
    ]
    return {
        "meta": _build_meta(solver_status, None, moment),
        "by_class": {},
        "by_teacher": {},
        "activity_types": list(model_module.ACTIVITY_TYPES),
        "constraint_report": {
            "hard_violations": hard_violations,
            "medium_violations": [],
            "soft_violations": [],
        },
    }


def write_output(payload: Dict[str, object], path: Path) -> None:
    """Write ``payload`` as UTF-8 JSON, creating the parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except OSError as error:
        raise OSError(
            f"impossibile scrivere il file di output {path}: {error}"
        ) from error
