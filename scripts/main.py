"""Entry point: load the config, build the CP-SAT model, solve it, validate,
write the JSON.

Usage (inside scripts/.venv):

    scripts/.venv/Scripts/python.exe scripts/main.py \\
        --config scripts/config.yaml [--time-limit 120]

Exit codes: 0 solved and validated, 1 infeasible, 2 solved but the independent
validation found a HARD violation.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

import data
import model as model_module
import output
from model import Lesson, Solution, TimetableModelBuilder

EXIT_OK = 0
EXIT_INFEASIBLE = 1
EXIT_INVALID = 2

SCRIPTS_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = SCRIPTS_DIR / "orario_output.json"
DEFAULT_CONFIG = SCRIPTS_DIR / "config.yaml"
EXPECTED_VENV = SCRIPTS_DIR / ".venv"


def _assert_project_venv() -> None:
    """Refuse to run outside scripts/.venv.

    The project pins every dependency in ``scripts/requirements.txt`` and must
    never fall back to a global interpreter, where versions can differ
    silently.  Compare the running ``sys.prefix`` with the expected venv path
    and abort loudly if they disagree.
    """
    running = Path(sys.prefix).resolve()
    expected = EXPECTED_VENV.resolve()
    if running != expected:
        raise SystemExit(
            "ERRORE: interprete Python fuori dal virtual env del progetto.\n"
            f"  atteso : {expected}\\Scripts\\python.exe\n"
            f"  in uso : {sys.executable}\n"
            f"           (sys.prefix = {running})\n"
            "Crea il venv e installa le dipendenze, poi rilancia:\n"
            f"  python -m venv {expected}\n"
            f"  {expected}\\Scripts\\python.exe -m pip install -r "
            f"{SCRIPTS_DIR / 'requirements.txt'}"
        )


# --------------------------------------------------------------------------
# Independent validation (does not trust the solver)
# --------------------------------------------------------------------------


def _teaching_lessons(solution: Solution) -> List[Lesson]:
    return [
        lesson
        for lesson in solution.lessons
        if lesson.teacher != data.EXPERT_LABEL
    ]


def _check_class_coverage(solution: Solution) -> List[Dict[str, str]]:
    occupancy: Counter = Counter()
    for lesson in solution.lessons:
        for class_ in lesson.classes:
            occupancy[(class_, lesson.day, lesson.slot)] += 1

    problems: List[Dict[str, str]] = []
    for class_ in data.CLASSES:
        for day in data.DAYS:
            for slot in data.teaching_slots(day):
                count = occupancy[(class_, day, slot)]
                if count != 1:
                    problems.append(
                        {
                            "constraint": "H2",
                            "detail": (
                                f"{class_} {day} {slot}: {count} lezioni "
                                "invece di 1"
                            ),
                        }
                    )
    return problems


def _check_teacher_uniqueness(solution: Solution) -> List[Dict[str, str]]:
    occupancy: Counter = Counter()
    for lesson in _teaching_lessons(solution):
        occupancy[(lesson.teacher, lesson.day, lesson.slot)] += 1
    return [
        {
            "constraint": "H3",
            "detail": f"{teacher} {day} {slot}: {count} lezioni in parallelo",
        }
        for (teacher, day, slot), count in sorted(occupancy.items())
        if count > 1
    ]


def _check_course_hours(solution: Solution) -> List[Dict[str, str]]:
    counts: Counter = Counter()
    for lesson in _teaching_lessons(solution):
        if lesson.activity_type == model_module.ACTIVITY_REINFORCEMENT:
            continue
        counts[(lesson.teacher, lesson.classes, lesson.subject)] += 1

    problems: List[Dict[str, str]] = []
    for course in data.COURSES:
        key = (course.teacher, course.classes, course.subject)
        found = counts.pop(key, 0)
        if found != course.hours:
            problems.append(
                {
                    "constraint": "H4",
                    "detail": (
                        f"{course.label}: {found}h assegnate invece di "
                        f"{course.hours}h"
                    ),
                }
            )
    for (teacher, classes, subject), found in sorted(counts.items()):
        problems.append(
            {
                "constraint": "H4",
                "detail": (
                    f"lezione non prevista: {teacher} {'+'.join(classes)} "
                    f"{subject} ({found}h)"
                ),
            }
        )

    reinforcement: Counter = Counter()
    for lesson in _teaching_lessons(solution):
        if lesson.activity_type == model_module.ACTIVITY_REINFORCEMENT:
            reinforcement[lesson.classes[0]] += 1
    for class_, hours in data.REINFORCEMENT_HOURS.items():
        found = reinforcement.pop(class_, 0)
        if found != hours:
            problems.append(
                {
                    "constraint": "H10",
                    "detail": (
                        f"potenziamento in {class_}: {found}h invece di "
                        f"{hours}h"
                    ),
                }
            )
    for class_, found in sorted(reinforcement.items()):
        problems.append(
            {
                "constraint": "H10",
                "detail": f"potenziamento non previsto in {class_}: {found}h",
            }
        )
    return problems


def _check_expert_hours(solution: Solution) -> List[Dict[str, str]]:
    scheduled = {
        (lesson.classes[0], lesson.day, lesson.slot, lesson.subject)
        for lesson in solution.lessons
        if lesson.teacher == data.EXPERT_LABEL
    }
    expected = {
        (hour.class_, hour.day, hour.slot, hour.subject)
        for hour in data.EXPERT_FIXED
    }
    problems = [
        {
            "constraint": "H1",
            "detail": f"ora esperto mancante: {class_} {day} {slot} {subject}",
        }
        for class_, day, slot, subject in sorted(expected - scheduled)
    ]
    problems.extend(
        {
            "constraint": "H1",
            "detail": f"ora esperto inattesa: {class_} {day} {slot} {subject}",
        }
        for class_, day, slot, subject in sorted(scheduled - expected)
    )

    blocked = {
        (hour.class_, hour.day, hour.slot) for hour in data.EXPERT_FIXED
    }
    for lesson in _teaching_lessons(solution):
        for class_ in lesson.classes:
            if (class_, lesson.day, lesson.slot) in blocked:
                problems.append(
                    {
                        "constraint": "H1",
                        "detail": (
                            f"{lesson.teacher} occupa l'ora esperto "
                            f"{class_} {lesson.day} {lesson.slot}"
                        ),
                    }
                )
    return problems


def _check_two_hour_adjacency(solution: Solution) -> List[Dict[str, str]]:
    placements: Dict[Tuple[str, Tuple[str, ...], str], List[Lesson]] = (
        defaultdict(list)
    )
    for lesson in _teaching_lessons(solution):
        placements[(lesson.teacher, lesson.classes, lesson.subject)].append(
            lesson
        )

    problems: List[Dict[str, str]] = []
    for course in data.COURSES:
        if course.hours != 2 or course.subject not in data.TWO_HOUR_SUBJECTS:
            continue
        lessons = placements.get(
            (course.teacher, course.classes, course.subject), []
        )
        if len(lessons) != 2:
            continue  # already reported by the hour check
        first, second = sorted(
            lessons, key=lambda item: data.FULL_SLOT_ORDER.index(item.slot)
        )
        pair = (first.slot, second.slot)
        if first.day != second.day or pair not in data.CONSECUTIVE_PAIRS:
            problems.append(
                {
                    "constraint": "H5",
                    "detail": (
                        f"{course.label}: {first.day} {first.slot} e "
                        f"{second.day} {second.slot} non sono consecutive"
                    ),
                }
            )
    return problems


def _check_early_exit(solution: Solution) -> List[Dict[str, str]]:
    teacher = data.EARLY_EXIT_TEACHER
    problems: List[Dict[str, str]] = []

    afternoons = {
        lesson.day
        for lesson in _teaching_lessons(solution)
        if lesson.teacher == teacher and lesson.slot in data.AFTERNOON_SLOTS
    }
    if len(afternoons) != 1:
        problems.append(
            {
                "constraint": "H6",
                "detail": (
                    f"{teacher}: {len(afternoons)} pomeriggi invece di 1"
                ),
            }
        )
    long_day = solution.early_exit_afternoon_day
    if afternoons and long_day not in afternoons:
        problems.append(
            {
                "constraint": "H6",
                "detail": (
                    f"{teacher}: giorno lungo dichiarato {long_day} ma le ore "
                    f"pomeridiane sono di {', '.join(sorted(afternoons))}"
                ),
            }
        )

    # The weighted-exit days (never afternoon days) are deliberately excluded
    # here: the 11:40 departure is unsatisfiable there and is tracked as a
    # weighted goal instead.
    short_days = {day for day in data.AFTERNOON_DAYS if day != long_day}
    for lesson in _teaching_lessons(solution):
        if (
            lesson.teacher == teacher
            and lesson.slot == "s4"
            and lesson.day in short_days
        ):
            problems.append(
                {
                    "constraint": "H6",
                    "detail": (
                        f"{teacher}: s4 di {lesson.day}, ma quel giorno esce "
                        "alle 11:40"
                    ),
                }
            )
    for duty_teacher, day in solution.lunch_duties:
        if duty_teacher == teacher and day != long_day:
            problems.append(
                {
                    "constraint": "H6",
                    "detail": (
                        f"{teacher}: turno mensa di {day}, giorno di uscita "
                        "alle 11:40"
                    ),
                }
            )
    return problems


def _check_no_afternoon(solution: Solution) -> List[Dict[str, str]]:
    teacher = data.NO_AFTERNOON_TEACHER
    day = data.NO_AFTERNOON_DAY
    return [
        {
            "constraint": "H7",
            "detail": (
                f"{teacher}: {lesson.slot} di {day} "
                f"({lesson.subject} in {'+'.join(lesson.classes)})"
            ),
        }
        for lesson in _teaching_lessons(solution)
        if lesson.teacher == teacher
        and lesson.day == day
        and lesson.slot in data.AFTERNOON_SLOTS
    ]


def _check_co_teaching(solution: Solution) -> List[Dict[str, str]]:
    """H9: every co-taught course keeps its classes on one shared lesson."""
    problems: List[Dict[str, str]] = []
    for course in data.COURSES:
        if not course.is_co_taught:
            continue
        matched = [
            lesson
            for lesson in _teaching_lessons(solution)
            if lesson.teacher == course.teacher
            and lesson.subject == course.subject
            and set(lesson.classes) == set(course.classes)
        ]
        if len(matched) != course.hours:
            problems.append(
                {
                    "constraint": "H9",
                    "detail": (
                        f"{course.label}: attese {course.hours}h in co-docenza "
                        f"sulle classi {'+'.join(course.classes)}, trovate "
                        f"{len(matched)}"
                    ),
                }
            )
    return problems


def _check_assistance(solution: Solution) -> List[Dict[str, str]]:
    problems: List[Dict[str, str]] = []

    per_day = Counter(day for _, day in solution.lunch_duties)
    for day in data.AFTERNOON_DAYS:
        if per_day[day] != data.LUNCH_SUPERVISORS_PER_DAY:
            problems.append(
                {
                    "constraint": "H8",
                    "detail": (
                        f"mensa di {day}: {per_day[day]} docenti invece di "
                        f"{data.LUNCH_SUPERVISORS_PER_DAY}"
                    ),
                }
            )

    per_class_day = Counter(
        (class_, day) for _, class_, day in solution.interval_duties
    )
    for (class_, day), count in sorted(per_class_day.items()):
        if count > 1:
            problems.append(
                {
                    "constraint": "H11",
                    "detail": (
                        f"intervallo {class_} {day}: {count} sorveglianti"
                    ),
                }
            )
    per_teacher_day = Counter(
        (teacher, day) for teacher, _, day in solution.interval_duties
    )
    for (teacher, day), count in sorted(per_teacher_day.items()):
        if count > 1:
            problems.append(
                {
                    "constraint": "H11",
                    "detail": (
                        f"{teacher} {day}: {count} intervalli nello stesso "
                        "giorno"
                    ),
                }
            )
    for _, class_, day in solution.interval_duties:
        if (class_, day) in data.EXPERT_COVERS_INTERVAL:
            problems.append(
                {
                    "constraint": "H11",
                    "detail": (
                        f"intervallo {class_} {day} già coperto dall'esperto"
                    ),
                }
            )

    teaching_only = data.TEACHING_ONLY_TEACHER
    assistance = output.hours_from_half(
        solution.assistance_half_hours[teaching_only]
    )
    total = output.hours_from_half(solution.half_hours[teaching_only])
    target = output.hours_from_half(data.TARGET_HALF_HOURS)
    if solution.assistance_half_hours[teaching_only] != 0:
        problems.append(
            {
                "constraint": "H12",
                "detail": (
                    f"{teaching_only}: {assistance}h di assistenza, deve "
                    "essere 0"
                ),
            }
        )
    if solution.half_hours[teaching_only] != data.TARGET_HALF_HOURS:
        problems.append(
            {
                "constraint": "H12",
                "detail": f"{teaching_only}: {total}h invece di {target}h",
            }
        )
    return problems


def _check_hour_accounting(solution: Solution) -> List[Dict[str, str]]:
    """Recompute every load from the extracted schedule, not from the model."""
    problems: List[Dict[str, str]] = []
    for teacher in data.TEACHERS:
        teaching = sum(
            1
            for lesson in _teaching_lessons(solution)
            if lesson.teacher == teacher
        )
        intervals = sum(
            1 for duty in solution.interval_duties if duty[0] == teacher
        )
        lunches = sum(1 for duty in solution.lunch_duties if duty[0] == teacher)
        recomputed = (
            2 * teaching
            + data.INTERVAL_CREDIT_HALF_HOURS * intervals
            + data.LUNCH_CREDIT_HALF_HOURS * lunches
        )
        if recomputed != solution.half_hours[teacher]:
            problems.append(
                {
                    "constraint": "monte_ore",
                    "detail": (
                        f"{teacher}: monte ore ricalcolato "
                        f"{output.hours_from_half(recomputed)}h contro "
                        f"{output.hours_from_half(solution.half_hours[teacher])}h "
                        "riportate dal modello"
                    ),
                }
            )
    return problems


def validate(solution: Solution) -> List[Dict[str, str]]:
    """Run every HARD check on the extracted solution."""
    problems: List[Dict[str, str]] = []
    problems.extend(_check_expert_hours(solution))
    problems.extend(_check_class_coverage(solution))
    problems.extend(_check_teacher_uniqueness(solution))
    problems.extend(_check_course_hours(solution))
    problems.extend(_check_two_hour_adjacency(solution))
    problems.extend(_check_early_exit(solution))
    problems.extend(_check_no_afternoon(solution))
    problems.extend(_check_co_teaching(solution))
    problems.extend(_check_assistance(solution))
    problems.extend(_check_hour_accounting(solution))
    return problems


# --------------------------------------------------------------------------
# Human readable report
# --------------------------------------------------------------------------

CELL_WIDTH = 20


def _cell(text: str) -> str:
    if len(text) > CELL_WIDTH:
        return text[: CELL_WIDTH - 1] + "…"
    return text.ljust(CELL_WIDTH)


def _print_grid(title: str, rows: Dict[str, Dict[str, str]]) -> None:
    print(f"\n{title}")
    header = "giorno".ljust(12) + "".join(
        _cell(slot) for slot in data.FULL_SLOT_ORDER
    )
    print(header)
    print("-" * len(header))
    for day in data.DAYS:
        line = day.ljust(12)
        for slot in data.FULL_SLOT_ORDER:
            line += _cell(rows.get(day, {}).get(slot, "—"))
        print(line.rstrip())


def print_report(solution: Solution, payload: Dict[str, object]) -> None:
    """Print the whole timetable and the constraint outcome to stdout."""
    print("=" * 78)
    print("ORARIO SCOLASTICO — risultato CP-SAT")
    print("=" * 78)
    print(f"Interprete   : {sys.executable}")
    print(f"Virtual env  : {sys.prefix}")
    print(f"Stato solver : {solution.status}")
    print(f"Obiettivo    : {solution.objective_value}")
    print(
        f"Giorno lungo (uscita anticipata): "
        f"{solution.early_exit_afternoon_day}"
    )

    by_class = payload["by_class"]
    supervisors = {
        (class_, day): teacher
        for teacher, class_, day in solution.interval_duties
    }
    for class_ in data.CLASSES:
        rows = {
            day: {
                slot: f"{cell['subject']} ({cell['teacher']})"
                for slot, cell in slots.items()
            }
            for day, slots in by_class[class_].items()
        }
        # by_class holds teaching slots only, like the schema; the interval
        # supervisor lives in by_teacher, so it is merged in just for reading.
        for day in data.DAYS:
            if (class_, day) in data.EXPERT_COVERS_INTERVAL:
                rows[day][data.INTERVAL_SLOT] = "sorv. Esperto"
            elif (class_, day) in supervisors:
                rows[day][data.INTERVAL_SLOT] = (
                    f"sorv. {supervisors[(class_, day)]}"
                )
        _print_grid(f"Classe {class_}", rows)

    by_teacher = payload["by_teacher"]
    for teacher in data.TEACHERS:
        entry = by_teacher[teacher]
        rows: Dict[str, Dict[str, str]] = {}
        for day, slots in entry["schedule"].items():
            rows[day] = {}
            for slot, cell in slots.items():
                if cell["activity_type"] == model_module.ACTIVITY_LUNCH:
                    label = f"mensa (+{cell['shared_with_teacher']})"
                elif cell["activity_type"] == model_module.ACTIVITY_INTERVAL:
                    label = f"interv. {cell['class']}"
                elif (
                    cell["activity_type"]
                    == model_module.ACTIVITY_REINFORCEMENT
                ):
                    label = f"potenz. {cell['class']}"
                else:
                    label = f"{cell['subject']} {cell['class']}"
                rows[day][slot] = label
        _print_grid(
            f"Docente {teacher} — {entry['total_hours']}h "
            f"(didattica {entry['teaching_hours']}h, "
            f"assistenza {entry['assistance_hours']}h)",
            rows,
        )

    print("\nMonte ore settimanale")
    print("-" * 60)
    target_hours = output.hours_from_half(data.TARGET_HALF_HOURS)
    for teacher in data.TEACHERS:
        entry = by_teacher[teacher]
        delta = entry["total_hours"] - target_hours
        marker = (
            "OK" if delta == 0 else f"{delta:+g}h rispetto a {target_hours:g}h"
        )
        print(
            f"{teacher:<10} totale {entry['total_hours']:>5}h  "
            f"didattica {entry['teaching_hours']:>5}h  "
            f"assistenza {entry['assistance_hours']:>4}h  {marker}"
        )

    report = payload["constraint_report"]
    for label, key in (
        ("HARD", "hard_violations"),
        ("MEDIUM", "medium_violations"),
        ("SOFT", "soft_violations"),
    ):
        entries = report[key]
        print(f"\nViolazioni {label}: {len(entries)}")
        for item in entries:
            print(f"  - [{item['constraint']}] {item['detail']}")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Risolve l'orario scolastico con OR-Tools CP-SAT."
    )
    parser.add_argument(
        "--time-limit",
        type=float,
        default=120.0,
        help="limite di tempo del solver in secondi (default: 120)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="percorso del file YAML di configurazione (default: "
        "scripts/config.yaml)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="percorso del JSON di output",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="stampa il log di ricerca del solver",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    _assert_project_venv()
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"Interprete : {sys.executable}")
    print(f"sys.prefix : {sys.prefix}")

    try:
        data.load_config(args.config)
    except FileNotFoundError as error:
        raise SystemExit(f"ERRORE: {error}")
    except data.ConfigError as error:
        raise SystemExit(f"ERRORE nella configurazione {args.config}: {error}")
    print(f"Config     : {args.config}")

    builder = TimetableModelBuilder(optimize=True)
    cp_sat_model = builder.build()
    solver = model_module.build_solver(args.time_limit, args.verbose)
    status = solver.Solve(cp_sat_model)
    status_name = solver.StatusName(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print(f"Nessuna soluzione: stato {status_name}.")
        print("Analisi dei vincoli HARD in conflitto in corso…")
        groups = model_module.diagnose_infeasibility(args.time_limit)
        for group in groups:
            print(
                f"  - {group}: "
                f"{data.HARD_CONSTRAINT_LABELS.get(group, 'sconosciuto')}"
            )
        payload = output.build_infeasible_output(status_name, groups)
        output.write_output(payload, args.output)
        print(f"\nReport scritto in {args.output}")
        return EXIT_INFEASIBLE

    solution = builder.extract(solver, status_name)
    hard_violations = validate(solution)
    payload = output.build_output(solution, hard_violations)
    output.write_output(payload, args.output)
    print_report(solution, payload)
    print(f"\nJSON scritto in {args.output}")

    if hard_violations:
        print(
            "\nATTENZIONE: la validazione indipendente ha trovato "
            f"{len(hard_violations)} violazioni HARD."
        )
        return EXIT_INVALID
    print("\nValidazione indipendente superata: nessun vincolo HARD violato.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
