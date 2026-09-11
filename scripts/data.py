"""Runtime loader for the school-specific config, including the time grid.

Everything that changes from one school (or school year) to the next --
teachers, classes, the time grid itself (days, slots, which days run long),
fixed expert hours, teaching loads, reinforcement hours, per-role constraints,
objective weights -- lives in an external YAML file and is read at runtime by
``load_config``. It validates that file and publishes its contents as module
attributes (``data.CLASSES``, ``data.COURSES``, ``data.DAYS``, ``data.WEIGHTS``,
...) so the rest of the code base can keep importing ``data`` and reading
plain names. Nothing in this module is called before ``load_config`` has run;
every module attribute that depends on the config raises a clear
``RuntimeError`` if used earlier (see ``teaching_slots`` below), instead of
silently reading a stale value.

The time grid supports exactly two kinds of day: "base" days (a single block
of teaching slots) and "extended" days (the same block, plus an interval,
optionally a lunch break, and further teaching slots in the afternoon). This
covers both a school that runs uniform short days (e.g. every day 8:00-13:30,
no ``extended_days`` at all) and one that mixes short and long days (e.g. four
mornings 8:10-12:40 plus two long days with mensa). Everything about the grid
-- day names, slot ids/times/labels, which slots only exist on extended days,
which slot pairs are truly back-to-back -- is read from the ``schedule``
section of the YAML config; nothing about it is hard-coded here.

The three special teacher roles (``early_exit``, ``no_afternoon``,
``teaching_only``) and the reinforcement assignment are all optional in the
config: a school that has no equivalent of one of these simply omits it, and
the corresponding HARD/MEDIUM rule is not added to the model at all (see
``model.py``).

Nothing in this module names a real person or a real school: the sample file
``config.example.yaml`` carries anonymised data with the same shape.

Durations are stored internally in *half hours* (integers) so that the 0.5h
interval credit never introduces floating point rounding inside the CP-SAT
model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

import yaml

logger = logging.getLogger("orario.data")

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
#: unsat-core report and by the independent validator. Roles ("early exit",
#: "teaching only", ...) are assigned to concrete teachers in the config, and
#: the groups tied to an optional role are simply never added to the model
#: when that role is not configured (see ``model.py``).
HARD_CONSTRAINT_LABELS: Dict[str, str] = {
    "H1": "Ore fisse esperti bloccate",
    "H2": "Copertura completa degli slot di ogni classe",
    "H3": "Nessuna sovrapposizione di un docente su due classi",
    "H4": "Monte ore esatto di ogni corso",
    "H5": "Materie da 2h in slot consecutivi",
    "H6": (
        "Docente 'uscita anticipata' (opzionale): un solo giorno esteso e "
        "niente ultimo slot del mattino nel giorno esteso corto"
    ),
    "H7": "Docente 'niente pomeriggio' (opzionale): nessuno slot esteso nel giorno indicato",
    "H8": "Mensa (opzionale): turno unico condiviso da N docenti",
    "H9": "Co-docenza in un'unica lezione condivisa",
    "H10": "Docente 'potenziamento' (opzionale): ore di potenziamento per classe",
    "H11": "Intervallo (opzionale): max 1 docente per classe e 1 classe per docente",
    "H12": (
        "Docente 'solo didattica' (opzionale): 0h di assistenza, monte ore "
        "pinnato al target"
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

    # -- time grid --
    days: Tuple[str, ...]
    extended_days: Tuple[str, ...]
    slot_metadata: Tuple[Dict[str, object], ...]
    full_slot_order: Tuple[str, ...]
    slot_kind: Dict[str, str]
    morning_slots: Tuple[str, ...]
    afternoon_slots: Tuple[str, ...]
    interval_slot: Optional[str]
    lunch_slot: Optional[str]
    consecutive_pairs: Tuple[Tuple[str, str], ...]
    teaching_slots_fn: Callable[[str], Tuple[str, ...]]
    weekly_teaching_slots: int

    # -- people --
    classes: Tuple[str, ...]
    teachers: Tuple[str, ...]
    expert_label: str
    expert_fixed: Tuple[FixedExpertHour, ...]
    expert_covers_interval: FrozenSet[Tuple[str, str]]
    courses: Tuple[Course, ...]

    reinforcement_teacher: Optional[str]
    reinforcement_subject: Optional[str]
    reinforcement_hours: Dict[str, int]

    two_hour_subjects: FrozenSet[str]
    spread_classes: Tuple[str, ...]

    target_half_hours: int
    interval_credit_half_hours: int
    lunch_credit_half_hours: int
    lunch_supervisors_per_day: int
    daily_subject_soft_cap: Optional[int]

    weights: Dict[str, int]

    early_exit_teacher: Optional[str]
    early_exit_weighted_days: Tuple[str, ...]
    no_afternoon_teacher: Optional[str]
    no_afternoon_day: Optional[str]
    teaching_only_teacher: Optional[str]


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

#: Module attributes published by :func:`load_config`. Kept explicit so a stale
#: import fails loudly instead of reading a value from a previous load.
_PUBLISHED_ATTRS: Tuple[str, ...] = (
    "CONFIG",
    "DAYS",
    "AFTERNOON_DAYS",
    "SLOT_METADATA",
    "FULL_SLOT_ORDER",
    "SLOT_KIND",
    "MORNING_SLOTS",
    "AFTERNOON_SLOTS",
    "INTERVAL_SLOT",
    "LUNCH_SLOT",
    "CONSECUTIVE_PAIRS",
    "WEEKLY_TEACHING_SLOTS",
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


def teaching_slots(day: str) -> Tuple[str, ...]:
    """Return the teaching slots available on ``day``.

    This is a placeholder: :func:`load_config` overwrites this module
    attribute with a closure over the loaded schedule. Calling it before any
    config has been loaded is a programming error, so it fails loudly instead
    of silently returning an empty/hard-coded grid.
    """
    raise RuntimeError(
        "data.teaching_slots() usato prima di data.load_config(): la griglia "
        "oraria non e' ancora stata caricata da un file di configurazione."
    )


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------


def _require(mapping: dict, key: str, where: str) -> object:
    if not isinstance(mapping, dict) or key not in mapping:
        raise ConfigError(f"{where}: chiave obbligatoria mancante: {key!r}")
    return mapping[key]


def _as_half_hours(hours: float, where: str) -> int:
    doubled = hours * 2
    if abs(doubled - round(doubled)) > 1e-9:
        raise ConfigError(
            f"{where}: {hours}h non è un multiplo di mezz'ora"
        )
    return int(round(doubled))


@dataclass(frozen=True)
class _Schedule:
    days: Tuple[str, ...]
    extended_days: Tuple[str, ...]
    slot_metadata: Tuple[Dict[str, object], ...]
    full_slot_order: Tuple[str, ...]
    slot_kind: Dict[str, str]
    morning_slots: Tuple[str, ...]
    afternoon_slots: Tuple[str, ...]
    interval_slot: Optional[str]
    lunch_slot: Optional[str]
    consecutive_pairs: Tuple[Tuple[str, str], ...]
    teaching_slots_fn: Callable[[str], Tuple[str, ...]]
    weekly_teaching_slots: int


def _parse_schedule(raw: dict) -> _Schedule:
    """Parse the ``schedule`` section: days, slots, consecutive pairs.

    This is the only place that used to be a hard-coded "time grid" constant;
    it is now entirely config-driven so a school with a different daily
    rhythm (different hours, no afternoon at all, more than two extended
    days, ...) only has to edit the YAML, never this module.
    """
    sched = _require(raw, "schedule", "root")

    days = tuple(_require(sched, "days", "schedule"))
    if not days or len(set(days)) != len(days):
        raise ConfigError("schedule.days: elenco vuoto o con duplicati")

    extended_days = tuple(sched.get("extended_days", []) or [])
    for day in extended_days:
        if day not in days:
            raise ConfigError(
                f"schedule.extended_days: giorno sconosciuto {day!r}"
            )
    if len(set(extended_days)) != len(extended_days):
        raise ConfigError("schedule.extended_days: elenco con duplicati")

    slots_raw = _require(sched, "slots", "schedule")
    if not isinstance(slots_raw, list) or not slots_raw:
        raise ConfigError("schedule.slots: atteso un elenco non vuoto")

    valid_kinds = {"teaching", "interval", "lunch"}
    slot_ids: List[str] = []
    slot_metadata: List[Dict[str, object]] = []
    slot_kind: Dict[str, str] = {}
    morning_slots: List[str] = []
    afternoon_slots: List[str] = []
    interval_slot: Optional[str] = None
    lunch_slot: Optional[str] = None

    for i, item in enumerate(slots_raw):
        where = f"schedule.slots[{i}]"
        slot_id = str(_require(item, "id", where))
        kind = str(_require(item, "kind", where))
        if kind not in valid_kinds:
            raise ConfigError(
                f"{where}: kind sconosciuto {kind!r} (atteso uno tra "
                f"{sorted(valid_kinds)})"
            )
        if slot_id in slot_ids:
            raise ConfigError(f"{where}: id slot duplicato {slot_id!r}")
        extended_only = bool(item.get("extended_only", False))
        if extended_only and not extended_days:
            raise ConfigError(
                f"{where}: extended_only=true ma schedule.extended_days è "
                "vuoto"
            )

        entry: Dict[str, object] = {"id": slot_id, "kind": kind}
        for optional_key in ("label", "start", "end"):
            if optional_key in item:
                entry[optional_key] = item[optional_key]
        if extended_only:
            entry["days"] = list(extended_days)

        slot_ids.append(slot_id)
        slot_metadata.append(entry)
        slot_kind[slot_id] = kind

        if kind == "teaching":
            (afternoon_slots if extended_only else morning_slots).append(
                slot_id
            )
        elif kind == "interval":
            if interval_slot is not None:
                raise ConfigError(
                    f"{where}: più di uno slot con kind 'interval' "
                    "(era già definito da un altro slot)"
                )
            interval_slot = slot_id
        elif kind == "lunch":
            if lunch_slot is not None:
                raise ConfigError(
                    f"{where}: più di uno slot con kind 'lunch' "
                    "(era già definito da un altro slot)"
                )
            if not extended_only:
                raise ConfigError(
                    f"{where}: lo slot 'lunch' deve avere extended_only=true"
                )
            lunch_slot = slot_id

    if not morning_slots:
        raise ConfigError(
            "schedule.slots: nessuno slot 'teaching' non esteso — ogni "
            "giorno deve avere almeno una lezione"
        )
    if lunch_slot is not None and not afternoon_slots:
        raise ConfigError(
            "schedule.slots: e' definito uno slot 'lunch' ma nessuno slot "
            "'teaching' con extended_only=true dopo di esso"
        )

    morning_slots_t = tuple(morning_slots)
    afternoon_slots_t = tuple(afternoon_slots)
    extended_days_set = frozenset(extended_days)

    def teaching_slots_for(day: str) -> Tuple[str, ...]:
        if day not in days:
            raise ValueError(f"giorno sconosciuto: {day!r}")
        if day in extended_days_set:
            return morning_slots_t + afternoon_slots_t
        return morning_slots_t

    consecutive_raw = sched.get("consecutive_pairs", []) or []
    consecutive_pairs: List[Tuple[str, str]] = []
    valid_slot_set = set(slot_ids)
    for i, pair in enumerate(consecutive_raw):
        where = f"schedule.consecutive_pairs[{i}]"
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ConfigError(f"{where}: atteso [slot_a, slot_b]")
        a, b = str(pair[0]), str(pair[1])
        if a not in valid_slot_set or b not in valid_slot_set:
            raise ConfigError(f"{where}: slot sconosciuto in {(a, b)!r}")
        consecutive_pairs.append((a, b))

    weekly_teaching_slots = sum(len(teaching_slots_for(d)) for d in days)

    return _Schedule(
        days=days,
        extended_days=extended_days,
        slot_metadata=tuple(slot_metadata),
        full_slot_order=tuple(slot_ids),
        slot_kind=slot_kind,
        morning_slots=morning_slots_t,
        afternoon_slots=afternoon_slots_t,
        interval_slot=interval_slot,
        lunch_slot=lunch_slot,
        consecutive_pairs=tuple(consecutive_pairs),
        teaching_slots_fn=teaching_slots_for,
        weekly_teaching_slots=weekly_teaching_slots,
    )


def _check_slot(day: str, slot: str, where: str, schedule: _Schedule) -> None:
    if day not in schedule.days:
        raise ConfigError(f"{where}: giorno sconosciuto {day!r}")
    if slot not in schedule.teaching_slots_fn(day):
        raise ConfigError(f"{where}: lo slot {slot!r} non esiste di {day}")


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
    raw: object, classes: FrozenSet[str], schedule: _Schedule
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
        _check_slot(day, slot, where, schedule)
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
    and the :class:`Config` is returned. Raises :class:`ConfigError` (a
    ``ValueError``) with a specific message on any missing key or broken
    cross-reference; raises ``FileNotFoundError`` if ``path`` does not exist.
    """
    path = Path(path)
    logger.info("Lettura configurazione da %s", path)
    if not path.is_file():
        raise FileNotFoundError(
            f"config non trovata: {path}. Copia config.example.yaml in "
            f"config.yaml e inserisci i dati reali."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: il file YAML deve avere un mapping in cima")

    schedule = _parse_schedule(raw)
    logger.debug(
        "Griglia oraria: %d giorni (%d estesi), %d slot didattici/settimana",
        len(schedule.days),
        len(schedule.extended_days),
        schedule.weekly_teaching_slots,
    )

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
        raw.get("expert_fixed"), class_set, schedule
    )

    covers_raw = raw.get("expert_covers_interval", []) or []
    expert_covers_interval = set()
    for i, item in enumerate(covers_raw):
        where = f"expert_covers_interval[{i}]"
        if schedule.interval_slot is None:
            raise ConfigError(
                f"{where}: definito ma schedule non ha nessuno slot "
                "'interval'"
            )
        c = str(_require(item, "class", where))
        d = str(_require(item, "day", where))
        if c not in class_set:
            raise ConfigError(f"{where}: classe sconosciuta {c!r}")
        if d not in schedule.days:
            raise ConfigError(f"{where}: giorno sconosciuto {d!r}")
        expert_covers_interval.add((c, d))

    courses = _parse_courses(
        _require(raw, "courses", "root"), class_set, teacher_set
    )

    reinf_raw = raw.get("reinforcement")
    if reinf_raw is None:
        reinf_teacher: Optional[str] = None
        reinf_subject: Optional[str] = None
        reinf_hours: Dict[str, int] = {}
    else:
        reinf_teacher = str(_require(reinf_raw, "teacher", "reinforcement"))
        reinf_subject = str(_require(reinf_raw, "subject", "reinforcement"))
        reinf_hours_raw = _require(reinf_raw, "hours", "reinforcement")
        if reinf_teacher not in teacher_set:
            raise ConfigError(
                f"reinforcement: docente sconosciuto {reinf_teacher!r}"
            )
        reinf_hours = {}
        for c, h in dict(reinf_hours_raw).items():
            if c not in class_set:
                raise ConfigError(f"reinforcement.hours: classe sconosciuta {c!r}")
            if not isinstance(h, int) or h < 0:
                raise ConfigError(f"reinforcement.hours[{c}]: intero >= 0 atteso")
            reinf_hours[str(c)] = int(h)

    params = raw.get("constraint_params", {}) or {}
    two_hour_subjects = frozenset(params.get("two_hour_subjects", []) or [])
    spread_classes = tuple(params.get("spread_classes", []) or [])
    for c in spread_classes:
        if c not in class_set:
            raise ConfigError(f"constraint_params.spread_classes: classe sconosciuta {c!r}")
    target_half = _as_half_hours(
        float(_require(params, "target_hours", "constraint_params")),
        "constraint_params.target_hours",
    )
    interval_credit = _as_half_hours(
        float(params.get("interval_credit_hours", 0) or 0),
        "constraint_params.interval_credit_hours",
    )
    lunch_credit = _as_half_hours(
        float(params.get("lunch_credit_hours", 0) or 0),
        "constraint_params.lunch_credit_hours",
    )
    lunch_supervisors = int(params.get("lunch_supervisors_per_day", 0) or 0)
    if schedule.lunch_slot is not None and lunch_supervisors < 1:
        raise ConfigError(
            "constraint_params.lunch_supervisors_per_day: deve essere >= 1 "
            "quando schedule definisce uno slot 'lunch'"
        )
    daily_cap_raw = params.get("daily_subject_soft_cap")
    daily_cap = int(daily_cap_raw) if daily_cap_raw is not None else None

    weights_raw = _require(raw, "weights", "root")
    weights: Dict[str, int] = {}
    for key in _REQUIRED_WEIGHT_KEYS:
        value = _require(weights_raw, key, "weights")
        if not isinstance(value, int) or value < 0:
            raise ConfigError(f"weights[{key}]: intero >= 0 atteso")
        weights[key] = value

    roles = raw.get("teacher_roles", {}) or {}

    early = roles.get("early_exit")
    if early is None:
        early_teacher: Optional[str] = None
        early_days: Tuple[str, ...] = ()
    else:
        early_teacher = str(_require(early, "teacher", "teacher_roles.early_exit"))
        early_days = tuple(early.get("weighted_no_s4_days", []) or [])
        if not schedule.extended_days:
            raise ConfigError(
                "teacher_roles.early_exit: configurato ma schedule.extended_days "
                "è vuoto — il ruolo non ha senso senza giorni estesi"
            )

    no_pm = roles.get("no_afternoon")
    if no_pm is None:
        no_pm_teacher: Optional[str] = None
        no_pm_day: Optional[str] = None
    else:
        no_pm_teacher = str(_require(no_pm, "teacher", "teacher_roles.no_afternoon"))
        no_pm_day = str(_require(no_pm, "day", "teacher_roles.no_afternoon"))
        if not schedule.afternoon_slots:
            raise ConfigError(
                "teacher_roles.no_afternoon: configurato ma schedule non ha "
                "slot didattici extended_only"
            )

    teach_only = roles.get("teaching_only")
    if teach_only is None:
        teach_only_teacher: Optional[str] = None
    else:
        teach_only_teacher = str(
            _require(teach_only, "teacher", "teacher_roles.teaching_only")
        )

    for role_name, tname in (
        ("early_exit", early_teacher),
        ("no_afternoon", no_pm_teacher),
        ("teaching_only", teach_only_teacher),
    ):
        if tname is not None and tname not in teacher_set:
            raise ConfigError(
                f"teacher_roles.{role_name}: docente sconosciuto {tname!r}"
            )
    for d in early_days:
        if d not in schedule.days:
            raise ConfigError(
                f"teacher_roles.early_exit.weighted_no_s4_days: giorno "
                f"sconosciuto {d!r}"
            )
    if no_pm_day is not None and no_pm_day not in schedule.days:
        raise ConfigError(
            f"teacher_roles.no_afternoon.day: giorno sconosciuto {no_pm_day!r}"
        )

    config = Config(
        days=schedule.days,
        extended_days=schedule.extended_days,
        slot_metadata=schedule.slot_metadata,
        full_slot_order=schedule.full_slot_order,
        slot_kind=schedule.slot_kind,
        morning_slots=schedule.morning_slots,
        afternoon_slots=schedule.afternoon_slots,
        interval_slot=schedule.interval_slot,
        lunch_slot=schedule.lunch_slot,
        consecutive_pairs=schedule.consecutive_pairs,
        teaching_slots_fn=schedule.teaching_slots_fn,
        weekly_teaching_slots=schedule.weekly_teaching_slots,
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
    logger.info(
        "Configurazione valida: %d classi, %d docenti, %d corsi, %d ore "
        "esperti fisse",
        len(config.classes),
        len(config.teachers),
        len(config.courses),
        len(config.expert_fixed),
    )
    for role_name, tname in (
        ("early_exit", early_teacher),
        ("no_afternoon", no_pm_teacher),
        ("teaching_only", teach_only_teacher),
    ):
        if tname is None:
            logger.debug("Ruolo opzionale '%s' non configurato: nessun vincolo aggiunto", role_name)
    if reinf_teacher is None:
        logger.debug("Potenziamento non configurato: nessun vincolo H10 aggiunto")
    return config


def _publish(config: Config) -> None:
    """Expose ``config`` as flat module attributes for the rest of the code."""
    g = globals()
    g["CONFIG"] = config
    g["DAYS"] = config.days
    g["AFTERNOON_DAYS"] = config.extended_days
    g["SLOT_METADATA"] = config.slot_metadata
    g["FULL_SLOT_ORDER"] = config.full_slot_order
    g["SLOT_KIND"] = config.slot_kind
    g["MORNING_SLOTS"] = config.morning_slots
    g["AFTERNOON_SLOTS"] = config.afternoon_slots
    g["INTERVAL_SLOT"] = config.interval_slot
    g["LUNCH_SLOT"] = config.lunch_slot
    g["CONSECUTIVE_PAIRS"] = config.consecutive_pairs
    g["WEEKLY_TEACHING_SLOTS"] = config.weekly_teaching_slots
    g["teaching_slots"] = config.teaching_slots_fn
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
