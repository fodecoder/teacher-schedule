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

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ortools.sat.python import cp_model

import data

logger = logging.getLogger("orario.model")

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
        self,
        config: data.Config,
        optimize: bool = True,
        post_assumptions: bool = False,
    ) -> None:
        """Configure the build.

        ``optimize`` pins every HARD group to true and adds the weighted
        objective -- the normal solving mode.  With ``optimize=False`` the
        group literals stay free so a caller can either hand them to
        ``AddAssumptions`` (``post_assumptions=True``, for the unsat core) or
        pin only a subset of them (greedy relaxation).
        """
        self.config = config
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
            for hour in self.config.expert_fixed
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
        for index, course in enumerate(self.config.courses):
            for day in self.config.days:
                for slot in self.config.teaching_slots(day):
                    name = f"teach_{index}_{day}_{slot}"
                    self.teach[(index, day, slot)] = self.model.NewBoolVar(name)

        for class_ in self.config.reinforcement_hours:
            for day in self.config.days:
                for slot in self.config.teaching_slots(day):
                    name = f"pot_{class_}_{day}_{slot}"
                    self.reinforce[(class_, day, slot)] = self.model.NewBoolVar(
                        name
                    )

        # Interval-supervision duties only make sense if the schedule
        # actually has an "interval" slot (a school with no recess omits it
        # in the config, and the whole H11/assistance machinery disappears).
        if self.config.interval_slot is not None:
            for teacher in self.config.teachers:
                for class_ in self.config.classes:
                    for day in self.config.days:
                        if (class_, day) in self.config.expert_covers_interval:
                            continue
                        name = f"int_{teacher}_{class_}_{day}"
                        self.interval[(teacher, class_, day)] = (
                            self.model.NewBoolVar(name)
                        )

        # Likewise, lunch duties only exist if the schedule has a "lunch"
        # slot at all.
        if self.config.lunch_slot is not None:
            for teacher in self.config.teachers:
                for day in self.config.extended_days:
                    self.lunch[(teacher, day)] = self.model.NewBoolVar(
                        f"mensa_{teacher}_{day}"
                    )

        if self.config.early_exit_teacher is not None:
            for day in self.config.extended_days:
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
            for index, course in enumerate(self.config.courses)
            if course.teacher == teacher and (index, day, slot) in self.teach
        ]
        if teacher == self.config.reinforcement_teacher:
            literals.extend(
                self.reinforce[(class_, day, slot)]
                for class_ in self.config.reinforcement_hours
                if (class_, day, slot) in self.reinforce
            )
        return literals

    def _class_literals(
        self, class_: str, day: str, slot: str
    ) -> List[cp_model.IntVar]:
        """Decision variables that would occupy ``class_`` at that slot."""
        literals = [
            self.teach[(index, day, slot)]
            for index, course in enumerate(self.config.courses)
            if class_ in course.classes and (index, day, slot) in self.teach
        ]
        if (class_, day, slot) in self.reinforce:
            literals.append(self.reinforce[(class_, day, slot)])
        return literals

    def _create_occupancy_variables(self) -> None:
        """Per (class, day, slot, teacher) presence indicators."""
        for class_ in self.config.classes:
            for day in self.config.days:
                for slot in self.config.teaching_slots(day):
                    expert_subject = self._expert_subject(class_, day, slot)
                    self.occupancy[(class_, day, slot, self.config.expert_label)] = (
                        self._constant_bool(1 if expert_subject else 0)
                    )
                    for teacher in self.config.teachers:
                        literals = [
                            self.teach[(index, day, slot)]
                            for index, course in enumerate(self.config.courses)
                            if course.teacher == teacher
                            and class_ in course.classes
                            and (index, day, slot) in self.teach
                        ]
                        if (
                            teacher == self.config.reinforcement_teacher
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

        for teacher in self.config.teachers:
            for day in self.config.days:
                for slot in self.config.teaching_slots(day):
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
        for class_ in self.config.classes:
            for day in self.config.days:
                for slot in self.config.teaching_slots(day):
                    expert = 1 if self._expert_subject(class_, day, slot) else 0
                    literals = self._class_literals(class_, day, slot)
                    self._add(
                        "H2", self.model.Add(sum(literals) == 1 - expert)
                    )

    def _add_teacher_uniqueness(self) -> None:
        """H3: a teacher cannot be in two places at the same time."""
        for teacher in self.config.teachers:
            for day in self.config.days:
                for slot in self.config.teaching_slots(day):
                    literals = self._teaching_literals(teacher, day, slot)
                    if len(literals) > 1:
                        self._add("H3", self.model.Add(sum(literals) <= 1))

    def _add_course_hours(self) -> None:
        """H4: each course gets exactly its prescribed number of hours."""
        for index, course in enumerate(self.config.courses):
            literals = [
                var for key, var in self.teach.items() if key[0] == index
            ]
            self._add("H4", self.model.Add(sum(literals) == course.hours))

    def _add_two_hour_adjacency(self) -> None:
        """H5: 2-hour subjects sit on two truly consecutive slots."""
        for index, course in enumerate(self.config.courses):
            if course.hours != 2 or course.subject not in self.config.two_hour_subjects:
                continue
            pair_literals: List[cp_model.IntVar] = []
            for day in self.config.days:
                slots = self.config.teaching_slots(day)
                for first, second in self.config.consecutive_pairs:
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
        """H6 (optional): the early-exit teacher works a single extended day
        and leaves at the end of the morning block on the short extended day.

        Skipped entirely if ``teacher_roles.early_exit`` is not configured —
        a school with no equivalent of this role simply has no H6 rule."""
        teacher = self.config.early_exit_teacher
        if teacher is None:
            return
        last_morning_slot = self.config.morning_slots[-1]
        self._add(
            "H6", self.model.Add(sum(self.early_exit_afternoon.values()) == 1)
        )

        for day in self.config.extended_days:
            marker = self.early_exit_afternoon[day]
            # No last-morning-slot lesson and no mensa on the short day.
            for literal in self._teaching_literals(
                teacher, day, last_morning_slot
            ):
                self._add("H6", self.model.Add(literal <= marker))
            if (teacher, day) in self.lunch:
                self._add(
                    "H6",
                    self.model.Add(self.lunch[(teacher, day)] <= marker),
                )
            # Afternoon lessons only on the marked day, and that day must
            # really carry at least one of them.
            afternoon_literals: List[cp_model.IntVar] = []
            for slot in self.config.afternoon_slots:
                for literal in self._teaching_literals(teacher, day, slot):
                    self._add("H6", self.model.Add(literal <= marker))
                    afternoon_literals.append(literal)
            self._add(
                "H6",
                self.model.Add(sum(afternoon_literals) >= marker),
            )
            # The weighted-exit days would deserve the same ban, but it is
            # unsatisfiable there and is handled as a weighted goal instead; see
            # _add_early_exit_preference.

    def _add_no_afternoon_rule(self) -> None:
        """H7 (optional): the "no afternoon" teacher never works an extended
        slot on the configured day. Skipped if the role is not configured."""
        if self.config.no_afternoon_teacher is None:
            return
        for slot in self.config.afternoon_slots:
            for literal in self._teaching_literals(
                self.config.no_afternoon_teacher, self.config.no_afternoon_day, slot
            ):
                self._add("H7", self.model.Add(literal == 0))

    def _add_lunch_shift(self) -> None:
        """H8 (optional): one mensa shift per mensa day, shared by exactly N
        teachers. Skipped if the schedule has no 'lunch' slot."""
        if self.config.lunch_slot is None:
            return
        for day in self.config.extended_days:
            literals = [
                self.lunch[(teacher, day)] for teacher in self.config.teachers
            ]
            self._add(
                "H8",
                self.model.Add(
                    sum(literals) == self.config.lunch_supervisors_per_day
                ),
            )

    def _add_reinforcement_hours(self) -> None:
        """H10 (optional): the reinforcement teacher's per-class load, from
        the config. Skipped if no reinforcement teacher is configured."""
        if self.config.reinforcement_teacher is None:
            return
        for class_, hours in self.config.reinforcement_hours.items():
            literals = [
                var for key, var in self.reinforce.items() if key[0] == class_
            ]
            self._add("H10", self.model.Add(sum(literals) == hours))

    def _add_interval_capacity(self) -> None:
        """H11 (optional): at most one supervisor per class, one class per
        teacher. Skipped if the schedule has no 'interval' slot."""
        if self.config.interval_slot is None:
            return
        for class_ in self.config.classes:
            for day in self.config.days:
                literals = [
                    self.interval[(teacher, class_, day)]
                    for teacher in self.config.teachers
                    if (teacher, class_, day) in self.interval
                ]
                if literals:
                    self._add("H11", self.model.Add(sum(literals) <= 1))

        for teacher in self.config.teachers:
            for day in self.config.days:
                literals = [
                    self.interval[(teacher, class_, day)]
                    for class_ in self.config.classes
                    if (teacher, class_, day) in self.interval
                ]
                if literals:
                    self._add("H11", self.model.Add(sum(literals) <= 1))

    def _add_hour_accounting(self) -> None:
        """Weekly load per teacher, in half hours, plus the H12 rule for the
        "teaching only" teacher."""
        for teacher in self.config.teachers:
            teaching = [
                var
                for key, var in self.teach.items()
                if self.config.courses[key[0]].teacher == teacher
            ]
            if teacher == self.config.reinforcement_teacher:
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
                + self.config.interval_credit_half_hours * sum(intervals)
                + self.config.lunch_credit_half_hours * sum(lunches)
            )
            self.half_hours[teacher] = total

        # H12 (optional): the "teaching only" teacher has 0h of assistance
        # and is pinned exactly at the target load. Skipped if the role is
        # not configured.
        teaching_only = self.config.teaching_only_teacher
        if teaching_only is not None:
            assistance = [
                var
                for key, var in self.interval.items()
                if key[0] == teaching_only
            ] + [
                var for key, var in self.lunch.items() if key[0] == teaching_only
            ]
            self._add("H12", self.model.Add(sum(assistance) == 0))
            self._add(
                "H12",
                self.model.Add(
                    self.half_hours[teaching_only] == self.config.target_half_hours
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
        for teacher in self.config.teachers:
            if teacher == self.config.teaching_only_teacher:
                continue  # pinned by H12
            delta = self.model.NewIntVar(-200, 200, f"delta_{teacher}")
            self.model.Add(
                delta == self.half_hours[teacher] - self.config.target_half_hours
            )
            deviation = self.model.NewIntVar(0, 200, f"dev_{teacher}")
            self.model.AddAbsEquality(deviation, delta)
            target_hours = self.config.target_half_hours / 2
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
        """The early-exit teacher's departure on the weighted days.

        HARD in the source document, but unsatisfiable: those days carry no
        expert hours, so at the last morning slot every class needs a
        titolare and as many distinct teachers as there are classes (H2 +
        H3); some class can only be taught by two teachers, so barring the
        early-exit teacher from that slot leaves one class uncovered. Kept as
        a heavily weighted goal so the residual violations show up in the
        report instead of turning the whole model infeasible (see
        ``docs/DECISIONS.md``). Skipped if the role is not configured.
        """
        teacher = self.config.early_exit_teacher
        if teacher is None:
            return
        last_morning_slot = self.config.morning_slots[-1]
        for day in self.config.early_exit_weighted_days:
            literals = self._teaching_literals(teacher, day, last_morning_slot)
            if not literals:
                continue
            penalty = self.model.NewBoolVar(f"early_exit_{day}")
            self.model.AddMaxEquality(penalty, literals)
            self._register(
                "uscita_anticipata",
                "medium",
                penalty,
                lambda _value, d=day, slot=last_morning_slot: (
                    f"{teacher}: {slot} di {d}, non esce all'orario previsto "
                    "(nessun altro docente disponibile per una classe)"
                ),
                teacher=teacher,
            )

    def _add_afternoon_pairing(self) -> None:
        """MEDIUM: the two extended-day afternoon slots of a class taught by
        the same person. Only meaningful with exactly two afternoon slots
        (the usual "p1 e p2" shape); skipped otherwise."""
        if len(self.config.afternoon_slots) != 2:
            return
        slot_a, slot_b = self.config.afternoon_slots
        holders = list(self.config.teachers) + [self.config.expert_label]
        for class_ in self.config.classes:
            for day in self.config.extended_days:
                same_holder = [
                    self._bool_and(
                        f"same_{class_}_{day}_{holder}",
                        [
                            self.occupancy[(class_, day, slot_a, holder)],
                            self.occupancy[(class_, day, slot_b, holder)],
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
                        f"{d}: {slot_a} e {slot_b} della {c} assegnati a "
                        "docenti diversi"
                    ),
                    class_=class_,
                )

    def _interval_neighbor_slots(self) -> Tuple[str, ...]:
        """Teaching slots immediately before/after the interval slot.

        Generalises the original "s2 or s3" rule (the two teaching slots
        flanking the recess) to whatever slot ids the schedule actually
        uses."""
        if self.config.interval_slot is None:
            return ()
        order = self.config.full_slot_order
        idx = order.index(self.config.interval_slot)
        neighbors = []
        if idx > 0 and self.config.slot_kind.get(order[idx - 1]) == "teaching":
            neighbors.append(order[idx - 1])
        if idx < len(order) - 1 and self.config.slot_kind.get(order[idx + 1]) == "teaching":
            neighbors.append(order[idx + 1])
        return tuple(neighbors)

    def _add_interval_preference(self) -> None:
        """MEDIUM: interval supervised by whoever taught the slot right
        before or right after it in that class. Skipped if there is no
        interval slot."""
        if self.config.interval_slot is None:
            return
        neighbor_slots = self._interval_neighbor_slots()
        for (teacher, class_, day), duty in self.interval.items():
            entitled = self._bool_or(
                f"ent_{teacher}_{class_}_{day}",
                [
                    self.occupancy[(class_, day, slot, teacher)]
                    for slot in neighbor_slots
                    if (class_, day, slot, teacher) in self.occupancy
                ],
            )
            penalty = self.model.NewBoolVar(
                f"int_pen_{teacher}_{class_}_{day}"
            )
            self.model.AddBoolOr([duty.Not(), entitled, penalty])
            neighbor_label = " o ".join(neighbor_slots) or "nessuno slot adiacente"
            self._register(
                "intervallo_con_s2_o_s3",
                "medium",
                penalty,
                lambda _value, t=teacher, c=class_, d=day, nl=neighbor_label: (
                    f"{d}: {t} sorveglia l'intervallo della {c} senza avervi "
                    f"{nl}"
                ),
                class_=class_,
                teacher=teacher,
            )

    def _add_lunch_preference(self) -> None:
        """MEDIUM: mensa preferably to whoever teaches the slot right before
        or right after it that day. Skipped if there is no lunch slot."""
        if self.config.lunch_slot is None:
            return
        last_morning_slot = self.config.morning_slots[-1]
        first_afternoon_slot = (
            self.config.afternoon_slots[0] if self.config.afternoon_slots else None
        )
        for (teacher, day), duty in self.lunch.items():
            penalty = self.model.NewBoolVar(f"mensa_pen_{teacher}_{day}")
            candidates = [duty.Not(), self.busy[(teacher, day, last_morning_slot)]]
            if first_afternoon_slot is not None:
                candidates.append(self.busy[(teacher, day, first_afternoon_slot)])
            candidates.append(penalty)
            self.model.AddBoolOr(candidates)
            self._register(
                "mensa_con_s4_o_p1",
                "medium",
                penalty,
                lambda _value, t=teacher, d=day: (
                    f"{d}: {t} è di turno mensa pur non avendo lezione "
                    "immediatamente prima o dopo"
                ),
                teacher=teacher,
            )

    def _add_subject_spread(self) -> None:
        """SOFT: storia/scienze/geografia on three different days in 3ª-5ª."""
        subjects = ("Storia", "Scienze", "Geografia")
        for class_ in self.config.spread_classes:
            per_day: Dict[Tuple[str, str], cp_model.IntVar] = {}
            for subject in subjects:
                for day in self.config.days:
                    literals = [
                        self.teach[(index, day, slot)]
                        for index, course in enumerate(self.config.courses)
                        if course.subject == subject
                        and class_ in course.classes
                        for slot in self.config.teaching_slots(day)
                        if (index, day, slot) in self.teach
                    ]
                    per_day[(subject, day)] = self._bool_or(
                        f"sd_{class_}_{subject}_{day}", literals
                    )
            for first in range(len(subjects)):
                for second in range(first + 1, len(subjects)):
                    for day in self.config.days:
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
        for teacher in self.config.teachers:
            if teacher == self.config.early_exit_teacher:
                continue
            markers = [
                self._bool_or(
                    f"pom_{teacher}_{day}",
                    [
                        self.busy[(teacher, day, slot)]
                        for slot in self.config.afternoon_slots
                    ],
                )
                for day in self.config.extended_days
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
        for teacher in self.config.teachers:
            for day in self.config.days:
                slots = self.config.teaching_slots(day)
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
        """SOFT: no more than ``daily_subject_soft_cap`` hours of the same
        subject per class per day. Skipped if no cap is configured."""
        if self.config.daily_subject_soft_cap is None:
            return
        subjects = {course.subject for course in self.config.courses}
        if self.config.reinforcement_subject is not None:
            subjects.add(self.config.reinforcement_subject)
        subjects = sorted(subjects)
        for class_ in self.config.classes:
            for subject in subjects:
                for day in self.config.days:
                    literals: List[cp_model.IntVar] = []
                    for index, course in enumerate(self.config.courses):
                        if (
                            course.subject != subject
                            or class_ not in course.classes
                        ):
                            continue
                        literals.extend(
                            self.teach[(index, day, slot)]
                            for slot in self.config.teaching_slots(day)
                            if (index, day, slot) in self.teach
                        )
                    if subject == self.config.reinforcement_subject:
                        literals.extend(
                            self.reinforce[(class_, day, slot)]
                            for slot in self.config.teaching_slots(day)
                            if (class_, day, slot) in self.reinforce
                        )
                    fixed = sum(
                        1
                        for slot in self.config.teaching_slots(day)
                        if self._expert_subject(class_, day, slot) == subject
                    )
                    if not literals and fixed <= self.config.daily_subject_soft_cap:
                        continue
                    excess = self.model.NewIntVar(
                        0,
                        len(literals) + fixed,
                        f"cap_{class_}_{subject}_{day}",
                    )
                    self.model.Add(
                        excess
                        >= sum(literals) + fixed - self.config.daily_subject_soft_cap
                    )
                    self._register(
                        "max_2h_giorno_stessa_materia",
                        "soft",
                        excess,
                        lambda value, c=class_, s=subject, d=day: (
                            f"{c}: {value + self.config.daily_subject_soft_cap} ore di "
                            f"{s} di {d}"
                        ),
                        class_=class_,
                    )

    # -- assembly ---------------------------------------------------------

    def build(self) -> cp_model.CpModel:
        """Post every variable, constraint and (optionally) the objective."""
        logger.debug("Costruzione modello CP-SAT (optimize=%s)", self.optimize)
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

        # Every relaxable HARD group must have an assumption literal, even
        # one whose constraint methods were skipped because the matching
        # role is not configured (see the optional-role guards above):
        # diagnose_infeasibility()/_greedy_relaxation() index this dict by
        # every name in RELAXABLE_HARD_GROUPS unconditionally.
        for group in RELAXABLE_HARD_GROUPS:
            self._guard(group)

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
                self.config.weights[term.constraint] * term.var
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

        for hour in self.config.expert_fixed:
            lessons.append(
                Lesson(
                    classes=(hour.class_,),
                    day=hour.day,
                    slot=hour.slot,
                    subject=hour.subject,
                    teacher=self.config.expert_label,
                    activity_type=ACTIVITY_TEACHING,
                    shared_with=hour.shared_with,
                )
            )

        for (index, day, slot), var in self.teach.items():
            if solver.Value(var):
                course = self.config.courses[index]
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
                        subject=self.config.reinforcement_subject,
                        teacher=self.config.reinforcement_teacher,
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
                self.config.interval_credit_half_hours
                * sum(1 for duty in interval_duties if duty[0] == teacher)
                + self.config.lunch_credit_half_hours
                * sum(1 for duty in lunch_duties if duty[0] == teacher)
            )
            for teacher in self.config.teachers
        }
        teaching = {
            teacher: half_hours[teacher] - assistance[teacher]
            for teacher in self.config.teachers
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


def diagnose_infeasibility(config: data.Config, time_limit: float) -> List[str]:
    """Return the HARD groups that make the model infeasible.

    First tries CP-SAT's assumption-based unsat core; if the core comes back
    empty (it can, when the conflict is proved before assumptions are used),
    falls back to a greedy relaxation that drops one group at a time until the
    remaining model becomes satisfiable.
    """
    logger.info("Diagnosi infeasibility: ricerca unsat core (assumption-based)")
    builder = TimetableModelBuilder(config, optimize=False, post_assumptions=True)
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
        result = sorted(core, key=lambda key: int(key[1:]))
        logger.info("Unsat core trovato: %s", result)
        return result

    logger.warning(
        "Unsat core vuoto: passo a rilassamento greedy (piu' lento)"
    )
    return _greedy_relaxation(config, time_limit)


def _greedy_relaxation(config: data.Config, time_limit: float) -> List[str]:
    """Drop HARD groups one by one until the model becomes feasible."""
    dropped: List[str] = []
    for group in reversed(RELAXABLE_HARD_GROUPS):
        dropped.append(group)
        logger.debug("Rilassamento greedy: provo a rilasciare %s", dropped)
        builder = TimetableModelBuilder(config, optimize=False, post_assumptions=False)
        model = builder.build()
        for kept in RELAXABLE_HARD_GROUPS:
            model.Add(builder.assumptions[kept] == (0 if kept in dropped else 1))
        solver = build_solver(time_limit, log_progress=False)
        status = solver.Solve(model)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            result = sorted(dropped, key=lambda key: int(key[1:]))
            logger.info("Rilassamento greedy: feasible rilasciando %s", result)
            return result
    logger.error(
        "Rilassamento greedy: nessun sottoinsieme di gruppi rilasciati ha "
        "reso il modello feasible"
    )
    return list(RELAXABLE_HARD_GROUPS)
