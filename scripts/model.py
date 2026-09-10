"""CP-SAT model for the primary-school timetable.

HARD requirements are posted as ``Add(...)`` constraints, grouped under the
identifiers H1..H12 of ``data.HARD_CONSTRAINT_LABELS``.  Every group can be
attached to an assumption literal so that an infeasible model yields an unsat
core naming the conflicting groups instead of a bare "INFEASIBLE".

MEDIUM and SOFT requirements are reified into penalty variables and summed,
weighted, into the objective; the same variables are read back after solving to
report exactly which of them ended up violated.

Two rules that the source document lists as HARD are weighted goals here,
because posting them as HARD makes the whole model infeasible.  Both are
reported in ``constraint_report`` whenever the solution has to break them; the
rationale and status of each derogation is recorded in ``docs/DECISIONS.md``.

* "exactly the target load per teacher": the assistance supply (mensa hours in
  total, plus at most one interval per teacher per day) cannot cover the hours
  the fixed teaching loads leave uncovered.  The rule stays HARD for the
  "teaching only" teacher, already at the target load with pure teaching (H12).
* "the early-exit teacher leaves at 11:40 on the weighted days": those days
  carry no expert hours, so at s4 every class needs a titolare and as many
  distinct teachers as there are classes; some class can only be taught by two
  teachers, so barring the early-exit teacher from s4 leaves one class
  uncovered.  The rule stays HARD for the short day between the two afternoon
  days, where it is satisfiable.

Which concrete teacher fills each role ("early exit", "no afternoon",
"teaching only", "reinforcement") is set in the YAML config, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ortools.sat.python import cp_model

import data

# Activity labels used in the output schema.
ACTIVITY_TEACHING = "didattica"
ACTIVITY_INTERVAL = "assistenza_intervallo"
ACTIVITY_LUNCH = "assistenza_mensa"
ACTIVITY_REINFORCEMENT = "potenziamento"

ACTIVITY_TYPES: Tuple[str, ...] = (
    ACTIVITY_TEACHING,
    ACTIVITY_INTERVAL,
    ACTIVITY_LUNCH,
    ACTIVITY_REINFORCEMENT,
)

#: HARD rules enforced by the shape of the model rather than by a constraint
#: that could be dropped: a co-taught lesson is a single decision variable
#: filling both classes, so it can never drift apart.
STRUCTURAL_HARD_GROUPS: Tuple[str, ...] = ("H9",)

#: Relaxable HARD groups, in the order they are reported.
RELAXABLE_HARD_GROUPS: Tuple[str, ...] = tuple(
    key
    for key in sorted(data.HARD_CONSTRAINT_LABELS, key=lambda k: int(k[1:]))
    if key not in STRUCTURAL_HARD_GROUPS
)


@dataclass(frozen=True)
class Lesson:
    """One scheduled hour in front of one or more classes.

    ``classes`` holds every class the *same decision variable* fills, i.e. a
    co-taught lesson.  ``shared_with`` carries the co-teaching that is already
    expressed as separate rows in the expert input, so that the output can
    report it without duplicating the lesson.
    """

    classes: Tuple[str, ...]
    day: str
    slot: str
    subject: str
    teacher: str
    activity_type: str
    shared_with: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Violation:
    """A MEDIUM or SOFT requirement that the solution does not satisfy."""

    severity: str
    constraint: str
    detail: str
    class_: Optional[str] = None
    teacher: Optional[str] = None


@dataclass
class Solution:
    """Everything ``output.py`` and the validator need, free of CP-SAT types."""

    status: str
    objective_value: int
    lessons: List[Lesson]
    interval_duties: List[Tuple[str, str, str]]  # (teacher, class, day)
    lunch_duties: List[Tuple[str, str]]  # (teacher, day)
    early_exit_afternoon_day: Optional[str]
    half_hours: Dict[str, int]
    teaching_half_hours: Dict[str, int]
    assistance_half_hours: Dict[str, int]
    violations: List[Violation]


@dataclass
class PenaltyTerm:
    """A reified MEDIUM/SOFT violation kept for both objective and reporting."""

    constraint: str
    severity: str
    var: cp_model.IntVar
    describe: Callable[[int], str]
    class_: Optional[str] = None
    teacher: Optional[str] = None


class TimetableModelBuilder:
    """Builds the CP-SAT model and extracts a :class:`Solution` from it."""

    def __init__(
        self, optimize: bool = True, post_assumptions: bool = False
    ) -> None:
        """Configure the build.

        ``optimize`` pins every HARD group to true and adds the weighted
        objective -- the normal solving mode.  With ``optimize=False`` the
        group literals stay free so a caller can either hand them to
        ``AddAssumptions`` (``post_assumptions=True``, for the unsat core) or
        pin only a subset of them (greedy relaxation).
        """
        self.optimize = optimize
        self.post_assumptions = post_assumptions
        self.model = cp_model.CpModel()

        self.teach: Dict[Tuple[int, str, str], cp_model.IntVar] = {}
        self.reinforce: Dict[Tuple[str, str, str], cp_model.IntVar] = {}
        self.interval: Dict[Tuple[str, str, str], cp_model.IntVar] = {}
        self.lunch: Dict[Tuple[str, str], cp_model.IntVar] = {}
        self.early_exit_afternoon: Dict[str, cp_model.IntVar] = {}

        self.busy: Dict[Tuple[str, str, str], cp_model.IntVar] = {}
        self.occupancy: Dict[Tuple[str, str, str, str], cp_model.IntVar] = {}
        self.half_hours: Dict[str, cp_model.IntVar] = {}

        self.penalties: List[PenaltyTerm] = []
        self.assumptions: Dict[str, cp_model.IntVar] = {}

        self._constants: Dict[int, cp_model.IntVar] = {}
        self._expert_at: Dict[Tuple[str, str, str], data.FixedExpertHour] = {
            (hour.class_, hour.day, hour.slot): hour
            for hour in data.EXPERT_FIXED
        }

    # -- small helpers ----------------------------------------------------

    def _constant_bool(self, value: int) -> cp_model.IntVar:
        """Return a cached BoolVar fixed to ``value`` (usable as a literal)."""
        if value not in self._constants:
            var = self.model.NewBoolVar(f"const_{value}")
            self.model.Add(var == value)
            self._constants[value] = var
        return self._constants[value]

    def _bool_or(
        self, name: str, literals: Sequence[cp_model.IntVar]
    ) -> cp_model.IntVar:
        if not literals:
            return self._constant_bool(0)
        var = self.model.NewBoolVar(name)
        self.model.AddMaxEquality(var, literals)
        return var

    def _bool_and(
        self, name: str, literals: Sequence[cp_model.IntVar]
    ) -> cp_model.IntVar:
        if not literals:
            return self._constant_bool(1)
        var = self.model.NewBoolVar(name)
        self.model.AddMinEquality(var, literals)
        return var

    def _guard(self, group: str) -> Optional[cp_model.IntVar]:
        """Return the enforcement literal of a HARD group, if any.

        In optimize mode the literal is pinned to true and the constraints
        behave as plain HARD rules; otherwise it stays free so the caller can
        assume or drop the whole group.
        """
        if group not in self.assumptions:
            literal = self.model.NewBoolVar(f"hard_{group}")
            self.assumptions[group] = literal
            if self.optimize:
                self.model.Add(literal == 1)
        return self.assumptions[group]

    def _add(self, group: str, constraint) -> None:
        """Attach ``constraint`` to the enforcement literal of ``group``."""
        constraint.OnlyEnforceIf(self._guard(group))

    def _expert_subject(self, class_: str, day: str, slot: str) -> Optional[str]:
        hour = self._expert_at.get((class_, day, slot))
        return hour.subject if hour is not None else None

    # -- variables --------------------------------------------------------

    def _create_variables(self) -> None:
        for index, course in enumerate(data.COURSES):
            for day in data.DAYS:
                for slot in data.teaching_slots(day):
                    name = f"teach_{index}_{day}_{slot}"
                    self.teach[(index, day, slot)] = self.model.NewBoolVar(name)

        for class_ in data.REINFORCEMENT_HOURS:
            for day in data.DAYS:
                for slot in data.teaching_slots(day):
                    name = f"pot_{class_}_{day}_{slot}"
                    self.reinforce[(class_, day, slot)] = self.model.NewBoolVar(
                        name
                    )

        for teacher in data.TEACHERS:
            for class_ in data.CLASSES:
                for day in data.DAYS:
                    if (class_, day) in data.EXPERT_COVERS_INTERVAL:
                        continue
                    name = f"int_{teacher}_{class_}_{day}"
                    self.interval[(teacher, class_, day)] = (
                        self.model.NewBoolVar(name)
                    )

        for teacher in data.TEACHERS:
            for day in data.AFTERNOON_DAYS:
                self.lunch[(teacher, day)] = self.model.NewBoolVar(
                    f"mensa_{teacher}_{day}"
                )

        for day in data.AFTERNOON_DAYS:
            self.early_exit_afternoon[day] = self.model.NewBoolVar(
                f"early_exit_pom_{day}"
            )

    def _teaching_literals(
        self, teacher: str, day: str, slot: str
    ) -> List[cp_model.IntVar]:
        """Distinct decision variables that keep ``teacher`` busy teaching.

        A co-taught lesson appears once, so summing this list counts it as a
        single hour even though it fills two classes.
        """
        literals = [
            self.teach[(index, day, slot)]
            for index, course in enumerate(data.COURSES)
            if course.teacher == teacher and (index, day, slot) in self.teach
        ]
        if teacher == data.REINFORCEMENT_TEACHER:
            literals.extend(
                self.reinforce[(class_, day, slot)]
                for class_ in data.REINFORCEMENT_HOURS
                if (class_, day, slot) in self.reinforce
            )
        return literals

    def _class_literals(
        self, class_: str, day: str, slot: str
    ) -> List[cp_model.IntVar]:
        """Decision variables that would occupy ``class_`` at that slot."""
        literals = [
            self.teach[(index, day, slot)]
            for index, course in enumerate(data.COURSES)
            if class_ in course.classes and (index, day, slot) in self.teach
        ]
        if (class_, day, slot) in self.reinforce:
            literals.append(self.reinforce[(class_, day, slot)])
        return literals

    def _create_occupancy_variables(self) -> None:
        """Per (class, day, slot, teacher) presence indicators."""
        for class_ in data.CLASSES:
            for day in data.DAYS:
                for slot in data.teaching_slots(day):
                    expert_subject = self._expert_subject(class_, day, slot)
                    self.occupancy[(class_, day, slot, data.EXPERT_LABEL)] = (
                        self._constant_bool(1 if expert_subject else 0)
                    )
                    for teacher in data.TEACHERS:
                        literals = [
                            self.teach[(index, day, slot)]
                            for index, course in enumerate(data.COURSES)
                            if course.teacher == teacher
                            and class_ in course.classes
                            and (index, day, slot) in self.teach
                        ]
                        if (
                            teacher == data.REINFORCEMENT_TEACHER
                            and (class_, day, slot) in self.reinforce
                        ):
                            literals.append(
                                self.reinforce[(class_, day, slot)]
                            )
                        self.occupancy[(class_, day, slot, teacher)] = (
                            self._bool_or(
                                f"occ_{class_}_{day}_{slot}_{teacher}", literals
                            )
                        )

        for teacher in data.TEACHERS:
            for day in data.DAYS:
                for slot in data.teaching_slots(day):
                    literals = self._teaching_literals(teacher, day, slot)
                    self.busy[(teacher, day, slot)] = self._bool_or(
                        f"busy_{teacher}_{day}_{slot}", literals
                    )

    # -- HARD constraints -------------------------------------------------

    def _add_expert_blocking(self) -> None:
        """H1: no titolare and no reinforcement on a fixed expert hour."""
        for (class_, day, slot) in self._expert_at:
            for literal in self._class_literals(class_, day, slot):
                self._add("H1", self.model.Add(literal == 0))

    def _add_class_coverage(self) -> None:
        """H2: every teaching slot of every class is filled exactly once."""
        for class_ in data.CLASSES:
            for day in data.DAYS:
                for slot in data.teaching_slots(day):
                    expert = 1 if self._expert_subject(class_, day, slot) else 0
                    literals = self._class_literals(class_, day, slot)
                    self._add(
                        "H2", self.model.Add(sum(literals) == 1 - expert)
                    )

    def _add_teacher_uniqueness(self) -> None:
        """H3: a teacher cannot be in two places at the same time."""
        for teacher in data.TEACHERS:
            for day in data.DAYS:
                for slot in data.teaching_slots(day):
                    literals = self._teaching_literals(teacher, day, slot)
                    if len(literals) > 1:
                        self._add("H3", self.model.Add(sum(literals) <= 1))

    def _add_course_hours(self) -> None:
        """H4: each course gets exactly its prescribed number of hours."""
        for index, course in enumerate(data.COURSES):
            literals = [
                var for key, var in self.teach.items() if key[0] == index
            ]
            self._add("H4", self.model.Add(sum(literals) == course.hours))

    def _add_two_hour_adjacency(self) -> None:
        """H5: 2-hour subjects sit on two truly consecutive slots."""
        for index, course in enumerate(data.COURSES):
            if course.hours != 2 or course.subject not in data.TWO_HOUR_SUBJECTS:
                continue
            pair_literals: List[cp_model.IntVar] = []
            for day in data.DAYS:
                slots = data.teaching_slots(day)
                for first, second in data.CONSECUTIVE_PAIRS:
                    if first not in slots or second not in slots:
                        continue
                    pair = self.model.NewBoolVar(
                        f"pair_{index}_{day}_{first}{second}"
                    )
                    self.model.AddImplication(
                        pair, self.teach[(index, day, first)]
                    )
                    self.model.AddImplication(
                        pair, self.teach[(index, day, second)]
                    )
                    pair_literals.append(pair)
            self._add("H5", self.model.Add(sum(pair_literals) == 1))

    def _add_early_exit_rules(self) -> None:
        """H6: the early-exit teacher works a single afternoon and leaves at
        11:40 (end of s3) on the short day between the two afternoon days."""
        teacher = data.EARLY_EXIT_TEACHER
        self._add(
            "H6", self.model.Add(sum(self.early_exit_afternoon.values()) == 1)
        )

        for day in data.AFTERNOON_DAYS:
            marker = self.early_exit_afternoon[day]
            # No s4 and no mensa on the short day.
            for literal in self._teaching_literals(teacher, day, "s4"):
                self._add("H6", self.model.Add(literal <= marker))
                self._add(
                    "H6", self.model.Add(self.lunch[(teacher, day)] <= marker)
                )
                # Afternoon lessons only on the marked day, and that day must
                # really carry at least one of them.
                afternoon_literals: List[cp_model.IntVar] = []
                for slot in data.AFTERNOON_SLOTS:
                    for literal in self._teaching_literals(teacher, day, slot):
                        self._add("H6", self.model.Add(literal <= marker))
                        afternoon_literals.append(literal)
                self._add(
                    "H6",
                    self.model.Add(sum(afternoon_literals) >= marker),
                )
            # The weighted-exit days would deserve the same s4 ban, but it is
            # unsatisfiable there and is handled as a weighted goal instead; see
            # _add_early_exit_preference.

    def _add_no_afternoon_rule(self) -> None:
        """H7: the "no afternoon" teacher never works p1/p2 on the configured
        day."""
        for slot in data.AFTERNOON_SLOTS:
            for literal in self._teaching_literals(
                data.NO_AFTERNOON_TEACHER, data.NO_AFTERNOON_DAY, slot
            ):
                self._add("H7", self.model.Add(literal == 0))

    def _add_lunch_shift(self) -> None:
        """H8: one mensa shift per mensa day, shared by exactly 2 teachers."""
        for day in data.AFTERNOON_DAYS:
            literals = [
                self.lunch[(teacher, day)] for teacher in data.TEACHERS
            ]
            self._add(
                "H8",
                self.model.Add(
                    sum(literals) == data.LUNCH_SUPERVISORS_PER_DAY
                ),
            )

    def _add_reinforcement_hours(self) -> None:
        """H10: the reinforcement teacher's per-class load, from the config."""
        for class_, hours in data.REINFORCEMENT_HOURS.items():
            literals = [
                var for key, var in self.reinforce.items() if key[0] == class_
            ]
            self._add("H10", self.model.Add(sum(literals) == hours))

    def _add_interval_capacity(self) -> None:
        """H11: at most one supervisor per class, one class per teacher."""
        for class_ in data.CLASSES:
            for day in data.DAYS:
                literals = [
                    self.interval[(teacher, class_, day)]
                    for teacher in data.TEACHERS
                    if (teacher, class_, day) in self.interval
                ]
                if literals:
                    self._add("H11", self.model.Add(sum(literals) <= 1))

        for teacher in data.TEACHERS:
            for day in data.DAYS:
                literals = [
                    self.interval[(teacher, class_, day)]
                    for class_ in data.CLASSES
                    if (teacher, class_, day) in self.interval
                ]
                if literals:
                    self._add("H11", self.model.Add(sum(literals) <= 1))

    def _add_hour_accounting(self) -> None:
        """Weekly load per teacher, in half hours, plus the H12 rule for the
        "teaching only" teacher."""
        for teacher in data.TEACHERS:
            teaching = [
                var
                for key, var in self.teach.items()
                if data.COURSES[key[0]].teacher == teacher
            ]
            if teacher == data.REINFORCEMENT_TEACHER:
                teaching.extend(self.reinforce.values())
            intervals = [
                var
                for key, var in self.interval.items()
                if key[0] == teacher
            ]
            lunches = [
                var for key, var in self.lunch.items() if key[0] == teacher
            ]
            total = self.model.NewIntVar(0, 200, f"halfhours_{teacher}")
            self.model.Add(
                total
                == 2 * sum(teaching)
                + data.INTERVAL_CREDIT_HALF_HOURS * sum(intervals)
                + data.LUNCH_CREDIT_HALF_HOURS * sum(lunches)
            )
            self.half_hours[teacher] = total

        teaching_only = data.TEACHING_ONLY_TEACHER
        assistance = [
            var for key, var in self.interval.items() if key[0] == teaching_only
        ] + [
            var for key, var in self.lunch.items() if key[0] == teaching_only
        ]
        self._add("H12", self.model.Add(sum(assistance) == 0))
        self._add(
            "H12",
            self.model.Add(
                self.half_hours[teaching_only] == data.TARGET_HALF_HOURS
            ),
        )

    # -- MEDIUM / SOFT penalties -----------------------------------------

    def _register(
        self,
        constraint: str,
        severity: str,
        var: cp_model.IntVar,
        describe: Callable[[int], str],
        class_: Optional[str] = None,
        teacher: Optional[str] = None,
    ) -> None:
        self.penalties.append(
            PenaltyTerm(constraint, severity, var, describe, class_, teacher)
        )

    def _add_weekly_load_goal(self) -> None:
        """Deviation from the contractual target load (soft, dominant weight).

        HARD in the source document, but a weighted goal here: the available
        assistance capacity is below the demand the fixed teaching loads
        create, so an exact-target equality for every teacher is infeasible
        (see ``docs/DECISIONS.md``).  The "teaching only" teacher is excluded,
        pinned exactly by H12."""
        for teacher in data.TEACHERS:
            if teacher == data.TEACHING_ONLY_TEACHER:
                continue  # pinned by H12
            delta = self.model.NewIntVar(-200, 200, f"delta_{teacher}")
            self.model.Add(
                delta == self.half_hours[teacher] - data.TARGET_HALF_HOURS
            )
            deviation = self.model.NewIntVar(0, 200, f"dev_{teacher}")
            self.model.AddAbsEquality(deviation, delta)
            target_hours = data.TARGET_HALF_HOURS / 2
            self._register(
                "monte_ore_target",
                "soft",
                deviation,
                lambda value, name=teacher, t=target_hours: (
                    f"{name} si discosta di {value / 2:g}h dal monte di {t:g}h"
                ),
                teacher=teacher,
            )

    def _add_early_exit_preference(self) -> None:
        """The early-exit teacher's 11:40 departure on the weighted days.

        HARD in the source document, but unsatisfiable: those days carry no
        expert hours, so at s4 every class needs a titolare and as many
        distinct teachers as there are classes (H2 + H3); some class can only
        be taught by two teachers, so barring the early-exit teacher from s4
        leaves one class uncovered.  Kept as a heavily weighted goal so the
        residual violations show up in the report instead of turning the whole
        model infeasible (see ``docs/DECISIONS.md``).
        """
        teacher = data.EARLY_EXIT_TEACHER
        for day in data.EARLY_EXIT_WEIGHTED_DAYS:
            literals = self._teaching_literals(teacher, day, "s4")
            if not literals:
                continue
            penalty = self.model.NewBoolVar(f"early_exit_{day}")
            self.model.AddMaxEquality(penalty, literals)
            self._register(
                "uscita_anticipata",
                "medium",
                penalty,
                lambda _value, d=day: (
                    f"{teacher}: s4 di {d}, non esce alle 11:40 "
                    "(nessun altro docente disponibile per una classe)"
                ),
                teacher=teacher,
            )

    def _add_afternoon_pairing(self) -> None:
        """MEDIUM: p1 and p2 of a class taught by the same person."""
        holders = list(data.TEACHERS) + [data.EXPERT_LABEL]
        for class_ in data.CLASSES:
            for day in data.AFTERNOON_DAYS:
                same_holder = [
                    self._bool_and(
                        f"same_{class_}_{day}_{holder}",
                        [
                            self.occupancy[(class_, day, "p1", holder)],
                            self.occupancy[(class_, day, "p2", holder)],
                        ],
                    )
                    for holder in holders
                ]
                matched = self._bool_or(
                    f"p1p2_match_{class_}_{day}", same_holder
                )
                penalty = self.model.NewBoolVar(f"p1p2_pen_{class_}_{day}")
                self.model.Add(matched + penalty == 1)
                self._register(
                    "p1_p2_stessa_docente",
                    "medium",
                    penalty,
                    lambda _value, c=class_, d=day: (
                        f"{d}: p1 e p2 della {c} assegnati a docenti diversi"
                    ),
                    class_=class_,
                )

    def _add_interval_preference(self) -> None:
        """MEDIUM: interval supervised by who taught s2 or s3 in that class."""
        for (teacher, class_, day), duty in self.interval.items():
            entitled = self._bool_or(
                f"ent_{teacher}_{class_}_{day}",
                [
                    self.occupancy[(class_, day, "s2", teacher)],
                    self.occupancy[(class_, day, "s3", teacher)],
                ],
            )
            penalty = self.model.NewBoolVar(
                f"int_pen_{teacher}_{class_}_{day}"
            )
            self.model.AddBoolOr([duty.Not(), entitled, penalty])
            self._register(
                "intervallo_con_s2_o_s3",
                "medium",
                penalty,
                lambda _value, t=teacher, c=class_, d=day: (
                    f"{d}: {t} sorveglia l'intervallo della {c} senza avervi "
                    "s2 o s3"
                ),
                class_=class_,
                teacher=teacher,
            )

    def _add_lunch_preference(self) -> None:
        """MEDIUM: mensa preferably to who has s4 or p1 that day."""
        for (teacher, day), duty in self.lunch.items():
            penalty = self.model.NewBoolVar(f"mensa_pen_{teacher}_{day}")
            self.model.AddBoolOr(
                [
                    duty.Not(),
                    self.busy[(teacher, day, "s4")],
                    self.busy[(teacher, day, "p1")],
                    penalty,
                ]
            )
            self._register(
                "mensa_con_s4_o_p1",
                "medium",
                penalty,
                lambda _value, t=teacher, d=day: (
                    f"{d}: {t} è di turno mensa pur non avendo s4 né p1"
                ),
                teacher=teacher,
            )

    def _add_subject_spread(self) -> None:
        """SOFT: storia/scienze/geografia on three different days in 3ª-5ª."""
        subjects = ("Storia", "Scienze", "Geografia")
        for class_ in data.SPREAD_CLASSES:
            per_day: Dict[Tuple[str, str], cp_model.IntVar] = {}
            for subject in subjects:
                for day in data.DAYS:
                    literals = [
                        self.teach[(index, day, slot)]
                        for index, course in enumerate(data.COURSES)
                        if course.subject == subject
                        and class_ in course.classes
                        for slot in data.teaching_slots(day)
                        if (index, day, slot) in self.teach
                    ]
                    per_day[(subject, day)] = self._bool_or(
                        f"sd_{class_}_{subject}_{day}", literals
                    )
            for first in range(len(subjects)):
                for second in range(first + 1, len(subjects)):
                    for day in data.DAYS:
                        clash = self._bool_and(
                            f"clash_{class_}_{day}_{first}{second}",
                            [
                                per_day[(subjects[first], day)],
                                per_day[(subjects[second], day)],
                            ],
                        )
                        self._register(
                            "materie_2h_giorni_diversi",
                            "soft",
                            clash,
                            lambda _value, c=class_, d=day, a=subjects[first],
                            b=subjects[second]: (
                                f"{c}: {a} e {b} cadono entrambe di {d}"
                            ),
                            class_=class_,
                        )

    def _add_single_afternoon_preference(self) -> None:
        """SOFT: at most one afternoon per teacher (HARD, via H6, for the
        early-exit teacher, who is skipped here)."""
        for teacher in data.TEACHERS:
            if teacher == data.EARLY_EXIT_TEACHER:
                continue
            markers = [
                self._bool_or(
                    f"pom_{teacher}_{day}",
                    [
                        self.busy[(teacher, day, slot)]
                        for slot in data.AFTERNOON_SLOTS
                    ],
                )
                for day in data.AFTERNOON_DAYS
            ]
            excess = self.model.NewIntVar(
                0, len(markers), f"pom_excess_{teacher}"
            )
            self.model.Add(excess >= sum(markers) - 1)
            self._register(
                "un_solo_pomeriggio",
                "soft",
                excess,
                lambda value, t=teacher: (
                    f"{t}: {value + 1} pomeriggi assegnati invece di 1"
                ),
                teacher=teacher,
            )

    def _add_gap_penalties(self) -> None:
        """SOFT: idle slots between two lessons of the same teacher."""
        for teacher in data.TEACHERS:
            for day in data.DAYS:
                slots = data.teaching_slots(day)
                gaps: List[cp_model.IntVar] = []
                for position, slot in enumerate(slots):
                    if position == 0 or position == len(slots) - 1:
                        continue
                    before = self._bool_or(
                        f"pre_{teacher}_{day}_{slot}",
                        [
                            self.busy[(teacher, day, other)]
                            for other in slots[:position]
                        ],
                    )
                    after = self._bool_or(
                        f"post_{teacher}_{day}_{slot}",
                        [
                            self.busy[(teacher, day, other)]
                            for other in slots[position + 1:]
                        ],
                    )
                    gap = self._bool_and(
                        f"gap_{teacher}_{day}_{slot}",
                        [
                            before,
                            after,
                            self.busy[(teacher, day, slot)].Not(),
                        ],
                    )
                    gaps.append(gap)
                if not gaps:
                    continue
                total = self.model.NewIntVar(
                    0, len(gaps), f"gaps_{teacher}_{day}"
                )
                self.model.Add(total == sum(gaps))
                self._register(
                    "buchi_orari",
                    "soft",
                    total,
                    lambda value, t=teacher, d=day: (
                        f"{t}: {value} ora/e buca/buche di {d}"
                    ),
                    teacher=teacher,
                )

    def _add_daily_subject_cap(self) -> None:
        """SOFT: no more than 2 hours of the same subject per class per day."""
        subjects = sorted(
            {course.subject for course in data.COURSES}
            | {data.REINFORCEMENT_SUBJECT}
        )
        for class_ in data.CLASSES:
            for subject in subjects:
                for day in data.DAYS:
                    literals: List[cp_model.IntVar] = []
                    for index, course in enumerate(data.COURSES):
                        if (
                            course.subject != subject
                            or class_ not in course.classes
                        ):
                            continue
                        literals.extend(
                            self.teach[(index, day, slot)]
                            for slot in data.teaching_slots(day)
                            if (index, day, slot) in self.teach
                        )
                    if subject == data.REINFORCEMENT_SUBJECT:
                        literals.extend(
                            self.reinforce[(class_, day, slot)]
                            for slot in data.teaching_slots(day)
                            if (class_, day, slot) in self.reinforce
                        )
                    fixed = sum(
                        1
                        for slot in data.teaching_slots(day)
                        if self._expert_subject(class_, day, slot) == subject
                    )
                    if not literals and fixed <= data.DAILY_SUBJECT_SOFT_CAP:
                        continue
                    excess = self.model.NewIntVar(
                        0,
                        len(literals) + fixed,
                        f"cap_{class_}_{subject}_{day}",
                    )
                    self.model.Add(
                        excess
                        >= sum(literals) + fixed - data.DAILY_SUBJECT_SOFT_CAP
                    )
                    self._register(
                        "max_2h_giorno_stessa_materia",
                        "soft",
                        excess,
                        lambda value, c=class_, s=subject, d=day: (
                            f"{c}: {value + data.DAILY_SUBJECT_SOFT_CAP} ore di "
                            f"{s} di {d}"
                        ),
                        class_=class_,
                    )

    # -- assembly ---------------------------------------------------------

    def build(self) -> cp_model.CpModel:
        """Post every variable, constraint and (optionally) the objective."""
        self._create_variables()
        self._create_occupancy_variables()

        self._add_expert_blocking()
        self._add_class_coverage()
        self._add_teacher_uniqueness()
        self._add_course_hours()
        self._add_two_hour_adjacency()
        self._add_early_exit_rules()
        self._add_no_afternoon_rule()
        self._add_lunch_shift()
        self._add_reinforcement_hours()
        self._add_interval_capacity()
        self._add_hour_accounting()

        if not self.optimize:
            # Feasibility pass: no objective, so CP-SAT can return an unsat
            # core over the HARD groups.
            if self.post_assumptions:
                self.model.AddAssumptions(
                    [
                        self.assumptions[group]
                        for group in RELAXABLE_HARD_GROUPS
                    ]
                )
            return self.model

        self._add_weekly_load_goal()
        self._add_early_exit_preference()
        self._add_afternoon_pairing()
        self._add_interval_preference()
        self._add_lunch_preference()
        self._add_subject_spread()
        self._add_single_afternoon_preference()
        self._add_gap_penalties()
        self._add_daily_subject_cap()

        self.model.Minimize(
            sum(
                data.WEIGHTS[term.constraint] * term.var
                for term in self.penalties
            )
        )
        return self.model

    # -- solution extraction ---------------------------------------------

    def extract(
        self, solver: cp_model.CpSolver, status_name: str
    ) -> Solution:
        """Turn solver values into a plain-Python :class:`Solution`."""
        lessons: List[Lesson] = []

        for hour in data.EXPERT_FIXED:
            lessons.append(
                Lesson(
                    classes=(hour.class_,),
                    day=hour.day,
                    slot=hour.slot,
                    subject=hour.subject,
                    teacher=data.EXPERT_LABEL,
                    activity_type=ACTIVITY_TEACHING,
                    shared_with=hour.shared_with,
                )
            )

        for (index, day, slot), var in self.teach.items():
            if solver.Value(var):
                course = data.COURSES[index]
                lessons.append(
                    Lesson(
                        classes=course.classes,
                        day=day,
                        slot=slot,
                        subject=course.subject,
                        teacher=course.teacher,
                        activity_type=ACTIVITY_TEACHING,
                    )
                )

        for (class_, day, slot), var in self.reinforce.items():
            if solver.Value(var):
                lessons.append(
                    Lesson(
                        classes=(class_,),
                        day=day,
                        slot=slot,
                        subject=data.REINFORCEMENT_SUBJECT,
                        teacher=data.REINFORCEMENT_TEACHER,
                        activity_type=ACTIVITY_REINFORCEMENT,
                    )
                )

        interval_duties = sorted(
            key for key, var in self.interval.items() if solver.Value(var)
        )
        lunch_duties = sorted(
            key for key, var in self.lunch.items() if solver.Value(var)
        )

        afternoon_day = next(
            (
                day
                for day, var in self.early_exit_afternoon.items()
                if solver.Value(var)
            ),
            None,
        )

        half_hours = {
            teacher: solver.Value(var)
            for teacher, var in self.half_hours.items()
        }
        assistance = {
            teacher: (
                data.INTERVAL_CREDIT_HALF_HOURS
                * sum(1 for duty in interval_duties if duty[0] == teacher)
                + data.LUNCH_CREDIT_HALF_HOURS
                * sum(1 for duty in lunch_duties if duty[0] == teacher)
            )
            for teacher in data.TEACHERS
        }
        teaching = {
            teacher: half_hours[teacher] - assistance[teacher]
            for teacher in data.TEACHERS
        }

        violations: List[Violation] = []
        for term in self.penalties:
            value = solver.Value(term.var)
            if value > 0:
                violations.append(
                    Violation(
                        severity=term.severity,
                        constraint=term.constraint,
                        detail=term.describe(value),
                        class_=term.class_,
                        teacher=term.teacher,
                    )
                )

        return Solution(
            status=status_name,
            objective_value=int(round(solver.ObjectiveValue())),
            lessons=lessons,
            interval_duties=interval_duties,
            lunch_duties=lunch_duties,
            early_exit_afternoon_day=afternoon_day,
            half_hours=half_hours,
            teaching_half_hours=teaching,
            assistance_half_hours=assistance,
            violations=violations,
        )


def build_solver(time_limit: float, log_progress: bool) -> cp_model.CpSolver:
    """Return a configured CP-SAT solver."""
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 8
    solver.parameters.log_search_progress = log_progress
    return solver


def diagnose_infeasibility(time_limit: float) -> List[str]:
    """Return the HARD groups that make the model infeasible.

    First tries CP-SAT's assumption-based unsat core; if the core comes back
    empty (it can, when the conflict is proved before assumptions are used),
    falls back to a greedy relaxation that drops one group at a time until the
    remaining model becomes satisfiable.
    """
    builder = TimetableModelBuilder(optimize=False, post_assumptions=True)
    model = builder.build()
    solver = build_solver(time_limit, log_progress=False)
    solver.Solve(model)

    core_indices = solver.SufficientAssumptionsForInfeasibility()
    literal_to_group = {
        builder.assumptions[group].Index(): group
        for group in RELAXABLE_HARD_GROUPS
    }
    core = [
        literal_to_group[index]
        for index in core_indices
        if index in literal_to_group
    ]
    if core:
        return sorted(core, key=lambda key: int(key[1:]))

    return _greedy_relaxation(time_limit)


def _greedy_relaxation(time_limit: float) -> List[str]:
    """Drop HARD groups one by one until the model becomes feasible."""
    dropped: List[str] = []
    for group in reversed(RELAXABLE_HARD_GROUPS):
        dropped.append(group)
        builder = TimetableModelBuilder(optimize=False, post_assumptions=False)
        model = builder.build()
        for kept in RELAXABLE_HARD_GROUPS:
            model.Add(builder.assumptions[kept] == (0 if kept in dropped else 1))
        solver = build_solver(time_limit, log_progress=False)
        status = solver.Solve(model)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return sorted(dropped, key=lambda key: int(key[1:]))
    return list(RELAXABLE_HARD_GROUPS)
