"""Time grid (invariant) and runtime loader for the school-specific config.

This module has two parts:

* The **time grid** -- days, slot ids, slot times, which slots are teaching
  slots on which day.  It is the same for any primary school that runs the
  "4 short mornings + 2 long days with mensa" schedule, so it is hard-coded
  here and never read from the config.

* The **loader** ``load_config`` -- everything that changes from one school or
  school year to the next (teachers, classes, fixed expert hours, teaching
  loads, reinforcement hours, per-role constraints, objective weights) lives in
  an external YAML file and is read at runtime.  ``load_config`` validates that
  file and publishes its contents as module attributes (``data.CLASSES``,
  ``data.COURSES``, ``data.WEIGHTS``, ...) so the rest of the code base can keep
  importing ``data`` and reading plain names.

Nothing in this module names a real person or a real school: the sample file
``config.example.yaml`` carries anonymised data with the same shape.

Durations are stored internally in *half hours* (integers) so that the 0.5h
interval credit never introduces floating point rounding inside the CP-SAT
model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Tuple

import yaml

# --------------------------------------------------------------------------
# Time grid -- invariant, not read from the config
# --------------------------------------------------------------------------

DAYS: Tuple[str, ...] = (
    "Lunedì",
    "Martedì",
    "Mercoledì",
    "Giovedì",
    "Venerdì",
)

#: Days that also have mensa, p1 and p2.
AFTERNOON_DAYS: Tuple[str, ...] = ("Martedì", "Giovedì")

MORNING_SLOTS: Tuple[str, ...] = ("s1", "s2", "s3", "s4")
AFTERNOON_SLOTS: Tuple[str, ...] = ("p1", "p2")

INTERVAL_SLOT = "intervallo"
LUNCH_SLOT = "mensa"

#: Slot descriptors reproduced in the ``meta.slots`` section of the output.
SLOT_METADATA: Tuple[Dict[str, object], ...] = (
    {"id": "s1", "label": "8:10-9:10", "start": "08:10", "end": "09:10"},
    {"id": "s2", "label": "9:10-10:10", "start": "09:10", "end": "10:10"},
    {
        "id": INTERVAL_SLOT,
        "label": "10:10-10:40",
        "start": "10:10",
        "end": "10:40",
    },
    {"id": "s3", "label": "10:40-11:40", "start": "10:40", "end": "11:40"},
    {"id": "s4", "label": "11:40-12:40", "start": "11:40", "end": "12:40"},
    {
        "id": LUNCH_SLOT,
        "label": "12:40-13:45",
        "start": "12:40",
        "end": "13:45",
        "days": list(AFTERNOON_DAYS),
    },
    {
        "id": "p1",
        "label": "13:45-14:45",
        "start": "13:45",
        "end": "14:45",
        "days": list(AFTERNOON_DAYS),
    },
    {
        "id": "p2",
        "label": "14:45-15:45",
        "start": "14:45",
        "end": "15:45",
        "days": list(AFTERNOON_DAYS),
    },
)

#: Display order used when printing / serialising a full day.
FULL_SLOT_ORDER: Tuple[str, ...] = (
    "s1",
    "s2",
    INTERVAL_SLOT,
    "s3",
    "s4",
    LUNCH_SLOT,
    "p1",
    "p2",
)


def teaching_slots(day: str) -> Tuple[str, ...]:
    """Return the teaching slots available on ``day``.

    Monday, Wednesday and Friday stop after s4; Tuesday and Thursday add the
    two afternoon slots.
    """
    if day not in DAYS:
        raise ValueError(f"giorno sconosciuto: {day!r}")
    if day in AFTERNOON_DAYS:
        return MORNING_SLOTS + AFTERNOON_SLOTS
    return MORNING_SLOTS


#: Slot pairs that are truly back-to-back: s2/s3 is broken by the interval and
#: s4/p1 is broken by mensa, so neither may host a 2-hour subject.
CONSECUTIVE_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("s1", "s2"),
    ("s3", "s4"),
    ("p1", "p2"),
)

#: Total teaching slots per class over the week: 4*3 + 6*2 = 24.
WEEKLY_TEACHING_SLOTS = sum(len(teaching_slots(day)) for day in DAYS)


# --------------------------------------------------------------------------
# Dataclasses for the school-specific input
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FixedExpertHour:
    """One immovable lesson taught by an external expert.

    ``shared_with`` lists the *other* classes attending the very same lesson
    (co-teaching); it is informative for the output only, because each class
    already has its own row here.
    """

    class_: str
    day: str
    slot: str
    subject: str
    shared_with: Tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class Course:
    """A teacher/subject workload to be scheduled.

    ``classes`` holds more than one class only for co-teaching: a lesson taught
    to two classes simultaneously counts as a single teaching hour, so it must
    be one decision variable per slot, never two.
    """

    teacher: str
    classes: Tuple[str, ...]
    subject: str
    hours: int

    @property
    def is_co_taught(self) -> bool:
        return len(self.classes) > 1

    @property
    def label(self) -> str:
        return f"{self.teacher}/{'+'.join(self.classes)}/{self.subject}"


# --------------------------------------------------------------------------
# Model metadata -- describes the HARD groups, not the school
# --------------------------------------------------------------------------

#: Human readable descriptions of the relaxable HARD groups, used both by the
#: unsat-core report and by the independent validator.  Roles ("early exit",
#: "teaching only", ...) are assigned to concrete teachers in the config.
HARD_CONSTRAINT_LABELS: Dict[str, str] = {
    "H1": "Ore fisse esperti bloccate",
    "H2": "Copertura completa degli slot di ogni classe",
    "H3": "Nessuna sovrapposizione di un docente su due classi",
    "H4": "Monte ore esatto di ogni corso",
    "H5": "Materie da 2h in slot consecutivi",
    "H6": (
        "Docente 'uscita anticipata': un solo pomeriggio e niente s4 nel "
        "giorno corto tra martedì e giovedì"
    ),
    "H7": "Docente 'niente pomeriggio': nessun p1/p2 nel giorno indicato",
    "H8": "Mensa: turno unico condiviso da 2 docenti",
    "H9": "Motoria in co-docenza su due classi, 1h sola",
    "H10": "Docente 'potenziamento': ore di potenziamento per classe",
    "H11": "Intervallo: max 1 docente per classe e 1 classe per docente",
    "H12": (
        "Docente 'solo didattica': 0h di assistenza, monte ore pinnato al "
        "target"
    ),
}


# --------------------------------------------------------------------------
# Runtime configuration
# --------------------------------------------------------------------------


class ConfigError(ValueError):
    """Raised when the YAML config is missing keys or self-inconsistent."""


@dataclass(frozen=True)
class Config:
    """Every school/year specific value, already validated and normalised."""

    classes: Tuple[str, ...]
    teachers: Tuple[str, ...]
    expert_label: str
    expert_fixed: Tuple[FixedExpertHour, ...]
    expert_covers_interval: FrozenSet[Tuple[str, str]]
    courses: Tuple[Course, ...]

    reinforcement_teacher: str
    reinforcement_subject: str
    reinforcement_hours: Dict[str, int]

    two_hour_subjects: FrozenSet[str]
    spread_classes: Tuple[str, ...]

    target_half_hours: int
    interval_credit_half_hours: int
    lunch_credit_half_hours: int
    lunch_supervisors_per_day: int
    daily_subject_soft_cap: int

    weights: Dict[str, int]

    early_exit_teacher: str
    early_exit_weighted_days: Tuple[str, ...]
    no_afternoon_teacher: str
    no_afternoon_day: str
    teaching_only_teacher: str


#: Objective weight keys the model expects; the config must provide all of them.
_REQUIRED_WEIGHT_KEYS: Tuple[str, ...] = (
    "monte_ore_target",
    "uscita_anticipata",
    "p1_p2_stessa_docente",
    "intervallo_con_s2_o_s3",
    "mensa_con_s4_o_p1",
    "materie_2h_giorni_diversi",
    "un_solo_pomeriggio",
    "buchi_orari",
    "max_2h_giorno_stessa_materia",
)

#: Module attributes published by :func:`load_config`.  Kept explicit so a stale
#: import fails loudly instead of reading a value from a previous load.
_PUBLISHED_ATTRS: Tuple[str, ...] = (
    "CONFIG",
    "CLASSES",
    "TEACHERS",
    "EXPERT_LABEL",
    "EXPERT_FIXED",
    "EXPERT_COVERS_INTERVAL",
    "COURSES",
    "REINFORCEMENT_TEACHER",
    "REINFORCEMENT_SUBJECT",
    "REINFORCEMENT_HOURS",
    "TWO_HOUR_SUBJECTS",
    "SPREAD_CLASSES",
    "TARGET_HALF_HOURS",
    "INTERVAL_CREDIT_HALF_HOURS",
    "LUNCH_CREDIT_HALF_HOURS",
    "LUNCH_SUPERVISORS_PER_DAY",
    "DAILY_SUBJECT_SOFT_CAP",
    "WEIGHTS",
    "EARLY_EXIT_TEACHER",
    "EARLY_EXIT_WEIGHTED_DAYS",
    "NO_AFTERNOON_TEACHER",
    "NO_AFTERNOON_DAY",
    "TEACHING_ONLY_TEACHER",
)


def _require(mapping: dict, key: str, where: str) -> object:
    if key not in mapping:
        raise ConfigError(f"{where}: chiave obbligatoria mancante: {key!r}")
    return mapping[key]


def _as_half_hours(hours: float, where: str) -> int:
    doubled = hours * 2
    if abs(doubled - round(doubled)) > 1e-9:
        raise ConfigError(
            f"{where}: {hours}h non è un multiplo di mezz'ora"
        )
    return int(round(doubled))


def _check_slot(day: str, slot: str, where: str) -> None:
    if day not in DAYS:
        raise ConfigError(f"{where}: giorno sconosciuto {day!r}")
    if slot not in teaching_slots(day):
        raise ConfigError(
            f"{where}: lo slot {slot!r} non esiste di {day}"
        )


def _parse_courses(
    raw: object, classes: FrozenSet[str], teachers: FrozenSet[str]
) -> Tuple[Course, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("courses: atteso un elenco non vuoto")
    courses: List[Course] = []
    for i, item in enumerate(raw):
        where = f"courses[{i}]"
        teacher = _require(item, "teacher", where)
        subject = _require(item, "subject", where)
        hours = _require(item, "hours", where)
        klass = _require(item, "classes", where)
        if teacher not in teachers:
            raise ConfigError(f"{where}: docente sconosciuto {teacher!r}")
        if not isinstance(klass, list) or not klass:
            raise ConfigError(f"{where}: 'classes' deve essere un elenco")
        for c in klass:
            if c not in classes:
                raise ConfigError(f"{where}: classe sconosciuta {c!r}")
        if not isinstance(hours, int) or hours < 1:
            raise ConfigError(f"{where}: 'hours' deve essere un intero >= 1")
        courses.append(
            Course(str(teacher), tuple(klass), str(subject), int(hours))
        )
    return tuple(courses)


def _parse_expert_fixed(
    raw: object, classes: FrozenSet[str]
) -> Tuple[FixedExpertHour, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("expert_fixed: atteso un elenco")
    seen: set = set()
    hours: List[FixedExpertHour] = []
    for i, item in enumerate(raw):
        where = f"expert_fixed[{i}]"
        class_ = str(_require(item, "class", where))
        day = str(_require(item, "day", where))
        slot = str(_require(item, "slot", where))
        subject = str(_require(item, "subject", where))
        shared = item.get("shared_with", []) or []
        if class_ not in classes:
            raise ConfigError(f"{where}: classe sconosciuta {class_!r}")
        _check_slot(day, slot, where)
        key = (class_, day, slot)
        if key in seen:
            raise ConfigError(f"{where}: slot {key} già assegnato a un esperto")
        seen.add(key)
        for c in shared:
            if c not in classes:
                raise ConfigError(f"{where}: classe sconosciuta in shared_with: {c!r}")
        hours.append(
            FixedExpertHour(class_, day, slot, subject, tuple(shared))
        )
    return tuple(hours)


def load_config(path: "str | Path") -> Config:
    """Read, validate and publish the school-specific config from ``path``.

    On success every name in ``_PUBLISHED_ATTRS`` becomes a module attribute
    and the :class:`Config` is returned.  Raises :class:`ConfigError` (a
    ``ValueError``) with a specific message on any missing key or broken
    cross-reference; raises ``FileNotFoundError`` if ``path`` does not exist.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"config non trovata: {path}. Copia config.example.yaml in "
            f"config.yaml e inserisci i dati reali."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: il file YAML deve avere un mapping in cima")

    classes = tuple(_require(raw, "classes", "root"))
    if len(set(classes)) != len(classes) or not classes:
        raise ConfigError("classes: elenco vuoto o con duplicati")
    teachers = tuple(_require(raw, "teachers", "root"))
    if len(set(teachers)) != len(teachers) or not teachers:
        raise ConfigError("teachers: elenco vuoto o con duplicati")
    class_set = frozenset(classes)
    teacher_set = frozenset(teachers)

    expert_label = str(raw.get("expert_label", "Esperto"))

    expert_fixed = _parse_expert_fixed(
        raw.get("expert_fixed"), class_set
    )

    covers_raw = raw.get("expert_covers_interval", []) or []
    expert_covers_interval = set()
    for i, item in enumerate(covers_raw):
        where = f"expert_covers_interval[{i}]"
        c = str(_require(item, "class", where))
        d = str(_require(item, "day", where))
        if c not in class_set:
            raise ConfigError(f"{where}: classe sconosciuta {c!r}")
        if d not in DAYS:
            raise ConfigError(f"{where}: giorno sconosciuto {d!r}")
        expert_covers_interval.add((c, d))

    courses = _parse_courses(
        _require(raw, "courses", "root"), class_set, teacher_set
    )

    reinf = _require(raw, "reinforcement", "root")
    reinf_teacher = str(_require(reinf, "teacher", "reinforcement"))
    reinf_subject = str(_require(reinf, "subject", "reinforcement"))
    reinf_hours_raw = _require(reinf, "hours", "reinforcement")
    if reinf_teacher not in teacher_set:
        raise ConfigError(f"reinforcement: docente sconosciuto {reinf_teacher!r}")
    reinf_hours: Dict[str, int] = {}
    for c, h in dict(reinf_hours_raw).items():
        if c not in class_set:
            raise ConfigError(f"reinforcement.hours: classe sconosciuta {c!r}")
        if not isinstance(h, int) or h < 0:
            raise ConfigError(f"reinforcement.hours[{c}]: intero >= 0 atteso")
        reinf_hours[str(c)] = int(h)

    params = _require(raw, "constraint_params", "root")
    two_hour_subjects = frozenset(
        _require(params, "two_hour_subjects", "constraint_params")
    )
    spread_classes = tuple(
        _require(params, "spread_classes", "constraint_params")
    )
    for c in spread_classes:
        if c not in class_set:
            raise ConfigError(f"constraint_params.spread_classes: classe sconosciuta {c!r}")
    target_half = _as_half_hours(
        float(_require(params, "target_hours", "constraint_params")),
        "constraint_params.target_hours",
    )
    interval_credit = _as_half_hours(
        float(_require(params, "interval_credit_hours", "constraint_params")),
        "constraint_params.interval_credit_hours",
    )
    lunch_credit = _as_half_hours(
        float(_require(params, "lunch_credit_hours", "constraint_params")),
        "constraint_params.lunch_credit_hours",
    )
    lunch_supervisors = int(
        _require(params, "lunch_supervisors_per_day", "constraint_params")
    )
    daily_cap = int(
        _require(params, "daily_subject_soft_cap", "constraint_params")
    )

    weights_raw = _require(raw, "weights", "root")
    weights: Dict[str, int] = {}
    for key in _REQUIRED_WEIGHT_KEYS:
        value = _require(weights_raw, key, "weights")
        if not isinstance(value, int) or value < 0:
            raise ConfigError(f"weights[{key}]: intero >= 0 atteso")
        weights[key] = value

    roles = _require(raw, "teacher_roles", "root")

    early = _require(roles, "early_exit", "teacher_roles")
    early_teacher = str(_require(early, "teacher", "teacher_roles.early_exit"))
    early_days = tuple(early.get("weighted_no_s4_days", []) or [])

    no_pm = _require(roles, "no_afternoon", "teacher_roles")
    no_pm_teacher = str(_require(no_pm, "teacher", "teacher_roles.no_afternoon"))
    no_pm_day = str(_require(no_pm, "day", "teacher_roles.no_afternoon"))

    teach_only = _require(roles, "teaching_only", "teacher_roles")
    teach_only_teacher = str(
        _require(teach_only, "teacher", "teacher_roles.teaching_only")
    )

    for role_name, tname in (
        ("early_exit", early_teacher),
        ("no_afternoon", no_pm_teacher),
        ("teaching_only", teach_only_teacher),
    ):
        if tname not in teacher_set:
            raise ConfigError(
                f"teacher_roles.{role_name}: docente sconosciuto {tname!r}"
            )
    for d in early_days:
        if d not in DAYS:
            raise ConfigError(
                f"teacher_roles.early_exit.weighted_no_s4_days: giorno sconosciuto {d!r}"
            )
    if no_pm_day not in DAYS:
        raise ConfigError(
            f"teacher_roles.no_afternoon.day: giorno sconosciuto {no_pm_day!r}"
        )

    config = Config(
        classes=classes,
        teachers=teachers,
        expert_label=expert_label,
        expert_fixed=expert_fixed,
        expert_covers_interval=frozenset(expert_covers_interval),
        courses=courses,
        reinforcement_teacher=reinf_teacher,
        reinforcement_subject=reinf_subject,
        reinforcement_hours=reinf_hours,
        two_hour_subjects=two_hour_subjects,
        spread_classes=spread_classes,
        target_half_hours=target_half,
        interval_credit_half_hours=interval_credit,
        lunch_credit_half_hours=lunch_credit,
        lunch_supervisors_per_day=lunch_supervisors,
        daily_subject_soft_cap=daily_cap,
        weights=weights,
        early_exit_teacher=early_teacher,
        early_exit_weighted_days=early_days,
        no_afternoon_teacher=no_pm_teacher,
        no_afternoon_day=no_pm_day,
        teaching_only_teacher=teach_only_teacher,
    )
    _publish(config)
    return config


def _publish(config: Config) -> None:
    """Expose ``config`` as flat module attributes for the rest of the code."""
    g = globals()
    g["CONFIG"] = config
    g["CLASSES"] = config.classes
    g["TEACHERS"] = config.teachers
    g["EXPERT_LABEL"] = config.expert_label
    g["EXPERT_FIXED"] = config.expert_fixed
    g["EXPERT_COVERS_INTERVAL"] = config.expert_covers_interval
    g["COURSES"] = config.courses
    g["REINFORCEMENT_TEACHER"] = config.reinforcement_teacher
    g["REINFORCEMENT_SUBJECT"] = config.reinforcement_subject
    g["REINFORCEMENT_HOURS"] = config.reinforcement_hours
    g["TWO_HOUR_SUBJECTS"] = config.two_hour_subjects
    g["SPREAD_CLASSES"] = config.spread_classes
    g["TARGET_HALF_HOURS"] = config.target_half_hours
    g["INTERVAL_CREDIT_HALF_HOURS"] = config.interval_credit_half_hours
    g["LUNCH_CREDIT_HALF_HOURS"] = config.lunch_credit_half_hours
    g["LUNCH_SUPERVISORS_PER_DAY"] = config.lunch_supervisors_per_day
    g["DAILY_SUBJECT_SOFT_CAP"] = config.daily_subject_soft_cap
    g["WEIGHTS"] = config.weights
    g["EARLY_EXIT_TEACHER"] = config.early_exit_teacher
    g["EARLY_EXIT_WEIGHTED_DAYS"] = config.early_exit_weighted_days
    g["NO_AFTERNOON_TEACHER"] = config.no_afternoon_teacher
    g["NO_AFTERNOON_DAY"] = config.no_afternoon_day
    g["TEACHING_ONLY_TEACHER"] = config.teaching_only_teacher
