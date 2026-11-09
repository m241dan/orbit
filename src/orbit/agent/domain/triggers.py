"""Triggers, which turn an event into a task for a role or a run of checks, the task checks or a
named set: the events and guards orbit knows, a trigger's fields, the package's triggers merged
with a project's and checked against its roles and sets, the reports a configurator proposes
routing a set's runs to a lens role, the sets no trigger runs, which of them fire on an event
and which guards hold one back, which of them a landing's changed paths let fire, when one fires
in words, the cron expressions of the ``schedule`` triggers, the tab a task's agent shows
under, what a named set's run ending raises, and the rendering of a trigger's prompt template.

The package's triggers aren't kept here: whoever builds them passes in the contents of their
``triggers.toml`` and the templates it names. ``manual`` isn't raised yet, but a trigger fired by
hand fires whatever its event; a trigger that runs a named set fires on any event, and one that
runs the task checks only on ``session_started``, as the startup check.
"""

import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from pydantic_core import PydanticCustomError

from orbit.agent.domain.board import WAITING
from orbit.agent.domain.code_changes import changes_code
from orbit.agent.domain.config_errors import ConfigError, validation_problems
from orbit.agent.domain.cron import cron_problem
from orbit.agent.domain.layers import DEFAULT, PACKAGE, Layer
from orbit.agent.domain.path_globs import matchable, matches_path, unmatchable_glob
from orbit.agent.domain.reviews import ReviewJob, ReviewKind, drop_diff, hold_diff
from orbit.agent.domain.roles import PLAIN_NAME
from orbit.agent.domain.verification import VerificationStep, set_report
from orbit.protocol import TASK_CHECKS, ReportStatus, TaskState, is_orbit_review

EventName = Literal[
    "session_started",
    "task_landed",
    "task_finished",
    "commits",
    "startup_check_failed",
    "manual",
    "reviews_finished",
    "set_finished",
    "schedule",
]
"""What a trigger fires on: orbit opening a workspace with its session, a task's work landing, a
task finishing, landings counted up to a trigger's ``every``, a step of the startup check
failing, the user firing it, every finding role's task of a review having ended, a run of a
named set ending, or its ``cron`` expression coming due."""

GuardName = Literal[
    "review-rounds", "raised-finding", "suggestions-pending", "not-queued", "dedupe", "code-changed"
]
"""A named rule that may hold a trigger back."""

EVENT_NAMES: tuple[str, ...] = get_args(EventName)
"""The events' names, in order."""

GUARD_NAMES: tuple[str, ...] = get_args(GuardName)
"""The guards' names, in order."""

COMMITS = "commits"
"""The event that counts landings, the only one a trigger's ``every`` goes with."""

TASK_LANDED = "task_landed"
TASK_FINISHED = "task_finished"
"""The events of a task's end: its work landing, and its finishing, however it did."""

REVIEWS_FINISHED = "reviews_finished"
"""The event of a review's lenses having ended: every task of a finding role, one granted
``report_finding``, that one review queued."""

SET_FINISHED: Final = "set_finished"
"""The event of a named set's run ending, passed or failed; neither a run cancelled or one git
couldn't start, nor the task checks, raise it."""

SCHEDULE: Final = "schedule"
"""The event of a trigger's ``cron`` expression coming due while its workspace is open, or
having come due while it was closed, the only event ``cron`` goes with."""

SetStatus = Literal["passed", "failed"]
"""What ``set_finished``'s ``status`` field says: the set's run passed, or failed."""

SET_STATUSES: tuple[str, ...] = get_args(SetStatus)
"""The values of ``set_finished``'s ``status`` field, in order."""

SESSION_STARTED = "session_started"
STARTUP_CHECK_FAILED = "startup_check_failed"
"""The events of opening a workspace: orbit opening it with its session, the only event a
trigger that runs the task checks fires on, and a step of the startup check failing."""

GATE = "gate"
"""What a trigger's ``checks`` called the task checks before they had their name, read as
:data:`~orbit.protocol.TASK_CHECKS`."""

VerificationState = Literal["configured", "missing", "off"]
"""What ``session_started``'s ``verification`` field says: verification on with at least one
task check, on with none, or off."""

VERIFICATION_STATES: tuple[str, ...] = get_args(VerificationState)
"""The values of ``session_started``'s ``verification`` field, in order."""

REVIEW_ROUNDS = "review-rounds"
"""The guard that holds a trigger back unless today's review rounds and batches find a review
due, whose job its task reviews; the tasks of every trigger it guards are booked into that one
review."""

RAISED_FINDING = "raised-finding"
"""The guard that holds a trigger back on a batch's review unless its role raised a finding
filed, or merged, into one of the batch's tasks, so a batch of fixes is reviewed by the lenses
whose findings it fixes (decision 20). It holds nothing back on any other event."""

CODE_CHANGED = "code-changed"
"""The guard that holds a trigger on ``task_landed`` back when nothing its commits changed is
code, as :func:`~orbit.agent.domain.code_changes.changes_code` says; a trigger with ``paths`` is
decided by them alone."""

REVIEW_FIELD = "review"
"""The ``when`` field saying what a review reviews: on ``task_landed``, a landing's own commit
or a batch that's done."""

LandingReview = Literal["commit", "batch"]
"""What ``task_landed``'s ``review`` field says: the landing's own commit is reviewed, or a
batch that's done, which no one task caused."""

LANDING_REVIEWS: tuple[str, ...] = get_args(LandingReview)
"""The values of ``task_landed``'s ``review`` field, in order."""

TRIGGER_VARIABLE = "trigger"
"""The variable every event gives: the name of the trigger it fires."""

VARIABLE = re.compile(r"\{([a-z_]+)\}")
"""A variable in a template: its name, in braces."""

PARAGRAPH_BREAK = "\n\n"
"""What parts a template's paragraphs."""


@dataclass(frozen=True)
class Event:
    """What an event gives the triggers on it: the ``fields`` their ``when`` can match, and the
    ``variables`` their templates can use, in order."""

    fields: tuple[str, ...]
    variables: tuple[str, ...]


EVENTS: Mapping[str, Event] = {
    SESSION_STARTED: Event(("verification",), (TRIGGER_VARIABLE, "checks")),
    TASK_LANDED: Event(
        ("role", "origin", REVIEW_FIELD),
        (
            TRIGGER_VARIABLE,
            "commit",
            "task",
            "title",
            "diff",
            "missing",
            "cap",
            "queued",
            "landed_since",
            "task_skills",
        ),
    ),
    TASK_FINISHED: Event(("role", "origin", "status"), (TRIGGER_VARIABLE,)),
    COMMITS: Event(
        ("role", "origin"),
        (TRIGGER_VARIABLE, "commit", "cap", "queued", "since_commits", "changed_files", "sample"),
    ),
    STARTUP_CHECK_FAILED: Event(("step",), (TRIGGER_VARIABLE, "step", "output")),
    "manual": Event((), (TRIGGER_VARIABLE, "checks")),
    REVIEWS_FINISHED: Event(
        ("findings", "review"),
        (
            TRIGGER_VARIABLE,
            "title",
            "commit",
            "findings",
            "cap",
            "queued",
            "landed_since",
            "messages",
        ),
    ),
    SET_FINISHED: Event(("set", "status"), (TRIGGER_VARIABLE, "set", "status", "commit", "output")),
    SCHEDULE: Event((), (TRIGGER_VARIABLE,)),
}
"""Each event, by name, with what it gives. A trigger fired by hand gives the variables of its
event, as the super review does those of ``commits``."""

ROLE_FIELD = "role"
"""The ``when`` field naming the role of an event's task, whose values are the roles: a disabled
one among them."""

WHEN_VALUES: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    SESSION_STARTED: {"verification": VERIFICATION_STATES},
    TASK_LANDED: {REVIEW_FIELD: LANDING_REVIEWS},
    REVIEWS_FINISHED: {"findings": ("some", "none"), REVIEW_FIELD: get_args(ReviewKind)},
    SET_FINISHED: {"status": SET_STATUSES},
}
"""The values each event gives the ``when`` fields orbit fixes them for, by event and field
name. Besides these, :data:`ROLE_FIELD`'s are the roles, and ``origin``, ``task_finished``'s
``status``, ``step`` and ``set`` take any value: origins include trigger names, steps are the
project's own, and a set may be renamed, so a ``set`` no set has simply never matches."""


def _plain_trigger_name(name: str) -> str:
    """The trigger's ``name``, refused unless it's a plain name."""
    if not PLAIN_NAME.fullmatch(name):
        raise PydanticCustomError(
            "trigger_name", "a trigger's name is a plain name: letters, digits, - and _"
        )
    return name


TriggerName = Annotated[str, AfterValidator(_plain_trigger_name)]
"""A trigger's name, a plain name as a role's is, since its settings are keyed by it between
dots, as in ``triggers.bugs-review.role``."""


def _plain_tab(tab: str) -> str:
    """The trigger's ``tab``, refused unless it's a plain name."""
    if not PLAIN_NAME.fullmatch(tab):
        raise PydanticCustomError("trigger_tab", "a tab is a plain name: letters, digits, - and _")
    return tab


TabName = Annotated[str, AfterValidator(_plain_tab)]
"""The name of the tab a trigger's agents share, a plain name as a trigger's is."""


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Trigger(_Config):
    """A trigger, as a file sets it: what it fires ``on``, the ``role`` of the task it queues with
    its ``prompt``, or a list of roles, one task each, or the checks it runs, the task checks or a
    named set, as ``checks`` (``gate`` read as the task checks), the events it takes ``when`` they
    match, the ``guards`` that may hold it back, how many landings it fires after, ``every``,
    whether it's ``manual``, under its ``label``, whether its task is a ``draft``, whether it's
    ``enabled``, the ``tab`` its agents share, if any, the ``paths`` a landing must change for
    it to fire, if any, and, on ``schedule``, the ``cron`` expression saying when it comes
    due."""

    on: EventName = Field(
        description='The event it fires on, such as "task_landed", "commits" or "manual".'
    )
    role: str | tuple[str, ...] | None = Field(
        default=None,
        description="The role of the task it queues, or a list of roles, queuing one task each.",
    )
    checks: str | None = Field(
        default=None,
        description='Instead of a role, the checks it runs: "task-checks", or a named set.',
    )
    prompt: str | None = Field(
        default=None,
        description="The task's prompt: the text, or a Markdown file under .orbit/ it names.",
    )
    when: dict[str, str | tuple[str, ...]] = Field(
        default_factory=dict,
        description="The events it takes, by their fields, each a value or a list of values.",
    )
    guards: tuple[GuardName, ...] = Field(
        default=(),
        description=(
            "Guards: review-rounds, raised-finding, suggestions-pending, not-queued, dedupe or"
            " code-changed."
        ),
    )
    every: int | None = Field(
        default=None,
        gt=0,
        description='With on = "commits", how many matching landings it fires after.',
    )
    manual: bool = Field(default=False, description="Whether you can fire it yourself.")
    label: str | None = Field(
        default=None, description="What it's called where you fire it; a manual trigger needs one."
    )
    draft: bool = Field(
        default=False, description="Whether its task is added as a draft, for you to accept."
    )
    enabled: bool = Field(default=True, description="Whether it fires at all.")
    tab: TabName | None = Field(
        default=None,
        description=(
            "The tab its agents share, a plain name; without one, their role's or their own."
        ),
    )
    paths: tuple[str, ...] | None = Field(
        default=None,
        description='With on = "task_landed", globs of the paths a commit must change to fire.',
    )
    cron: str | None = Field(
        default=None,
        description='With on = "schedule", when it fires: five cron fields, in local time.',
    )

    @field_validator("on", mode="before")
    @classmethod
    def _known_event(cls, on: object) -> object:
        return _refuse_unknown_event(on)

    @field_validator("checks", mode="before")
    @classmethod
    def _gate_as_task_checks(cls, checks: object) -> object:
        return TASK_CHECKS if checks == GATE else checks

    @field_validator("guards", mode="before")
    @classmethod
    def _known_guards(cls, guards: object) -> object:
        unknown = [guard for guard in _strings(guards) if guard not in GUARD_NAMES]
        if unknown:
            raise PydanticCustomError(
                "trigger_guard",
                f"{unknown[0]} isn't a guard; the guards are {', '.join(GUARD_NAMES)}.",
            )
        return guards

    @property
    def roles(self) -> tuple[str, ...]:
        """The roles it queues a task for, in order: its ``role``, or each of its list; none if
        it runs checks."""
        if self.role is None:
            return ()
        return (self.role,) if isinstance(self.role, str) else self.role


class ChecksTrigger(_Config):
    """A trigger that runs ``checks``, the task checks or a named set, as an agent proposes it:
    the fields of a :class:`Trigger` such a trigger uses."""

    on: EventName
    checks: str
    every: int | None = Field(default=None, gt=0)
    manual: bool = False
    label: str | None = None
    enabled: bool = True

    @field_validator("on", mode="before")
    @classmethod
    def _known_event(cls, on: object) -> object:
        return _refuse_unknown_event(on)

    def as_trigger(self) -> Trigger:
        """The :class:`Trigger` it is, as the config would read it."""
        return Trigger.model_validate(self.model_dump())


class ReportTrigger(_Config):
    """A report, as an agent proposes it: a trigger queuing a task for the lens ``role`` when a
    run of the named ``set`` finishes, one that failed or, with ``status`` ``any``, any run,
    with the inline ``prompt``, a template of ``set_finished``'s variables."""

    set: str
    role: str
    status: ReportStatus = "failed"
    prompt: str = Field(min_length=1)

    def as_trigger(self) -> Trigger:
        """The ``set_finished`` :class:`Trigger` it is, as the config would read it."""
        status = {"status": "failed"} if self.status == "failed" else {}
        return Trigger(
            on=SET_FINISHED, role=self.role, prompt=self.prompt, when={"set": self.set, **status}
        )


def _refuse_unknown_event(on: object) -> object:
    """``on``, a trigger's event, refused if it's a name that isn't among :data:`EVENT_NAMES`."""
    if isinstance(on, str) and on not in EVENT_NAMES:
        raise PydanticCustomError(
            "trigger_event", f"{on} isn't an event; the events are {', '.join(EVENT_NAMES)}."
        )
    return on


def _strings(value: object) -> list[str]:
    """The strings in ``value``, if it's a list or tuple; none otherwise."""
    items = value if isinstance(value, list | tuple) else ()
    return [item for item in items if isinstance(item, str)]


class _BuiltInTriggers(_Config):
    triggers: dict[TriggerName, Trigger]


def load_built_in_triggers(data: dict[str, Any]) -> dict[str, Trigger]:
    """The package's triggers ``data`` (``triggers.toml``'s contents) describes, in order, each
    with its prompt as the file gives it.

    Raises :class:`ConfigError`, naming the trigger and field, if a trigger is invalid.
    """
    try:
        return _BuiltInTriggers.model_validate(data).triggers
    except ValidationError as exc:
        raise ConfigError(validation_problems(exc)) from None


@dataclass(frozen=True)
class MergedTriggers:
    """The triggers merged across the package and the project file but not yet validated: each
    trigger's ``tables`` of fields, by name, as validating a trigger takes them, and the
    ``layers`` each field came from, by trigger and field name."""

    tables: Any
    layers: dict[str, dict[str, Layer]]


def merge_triggers(package: Mapping[str, Trigger], project: Layer, given: object) -> MergedTriggers:
    """The ``package``'s triggers, then those the file in ``project`` adds in ``given``, its
    ``[triggers]`` table, each field the project's value if it sets it, else the package's, else
    the default. ``given``, or one of its triggers, as it is if it isn't a table, for validation
    to refuse."""
    if not isinstance(given, dict):
        return MergedTriggers(given, {})
    merged = {
        name: _merged(package.get(name), project, given.get(name, {}))
        for name in _merged_names(package, given)
    }
    return MergedTriggers(
        {name: table for name, (table, _) in merged.items()},
        {name: layers for name, (_, layers) in merged.items()},
    )


def _merged_names(package: Mapping[str, Trigger], given: Mapping[str, object]) -> list[str]:
    """The names of the triggers merged from the ``package`` and those ``given`` in the project
    file: the package's, then the project's new ones, each in its order."""
    return [*package, *(name for name in given if name not in package)]


def _merged(
    shipped: Trigger | None, project: Layer, given: object
) -> tuple[object, dict[str, Layer]]:
    """The fields of a trigger, the ``shipped`` one if any, with those ``given`` for it in the
    file in ``project`` over them, and the layer each came from; ``given`` as it is if it isn't
    a table."""
    if not isinstance(given, dict):
        return given, {}
    package = () if shipped is None else shipped.model_fields_set
    layers = dict.fromkeys(Trigger.model_fields, DEFAULT) | dict.fromkeys(package, PACKAGE)
    return _merged_fields(shipped, given), layers | dict.fromkeys(given, project)


def _merged_fields(shipped: Trigger | None, given: dict[str, Any]) -> dict[str, Any]:
    """The fields of a trigger, the ``shipped`` one's if any, with those ``given`` over them."""
    return ({} if shipped is None else shipped.model_dump()) | given


def merge_proposed(
    package: Mapping[str, Trigger], proposed: Mapping[str, ChecksTrigger]
) -> dict[str, Trigger]:
    """The ``proposed`` triggers, by name, as the config reads them once they're written: each
    with the fields it sets over those of the ``package``'s trigger of its name, if any, as
    :func:`merge_triggers` merges them."""
    return {
        name: Trigger.model_validate(
            _merged_fields(package.get(name), trigger.model_dump(exclude_defaults=True))
        )
        for name, trigger in proposed.items()
    }


def check_free_names(package: Mapping[str, Trigger], names: Iterable[str]) -> None:
    """Raise :class:`ConfigError` naming each of ``names``, proposed for triggers that run
    checks, that a trigger of the ``package`` with a role has, on a line of its own, if any, each
    at ``triggers.<name>.checks``: merged over that trigger, it would have a role and checks."""
    problems = [
        f"triggers.{name}.checks: {name} is taken by a built-in trigger, which queues"
        f" {_tasks_of(roles)}; give this trigger another name."
        for name in names
        if name in package and (roles := package[name].roles)
    ]
    if problems:
        raise ConfigError("\n".join(problems))


@dataclass(frozen=True)
class ReportTargets:
    """What a proposed report may name: the ``lenses``, the enabled roles granted
    ``report_finding``, it may queue a task for, the ``sets`` it may report on, and the names
    ``taken`` by other triggers, each with whose it is, such as ``a built-in trigger``."""

    lenses: Collection[str]
    sets: Collection[str]
    taken: Mapping[str, str]


def check_reports(reports: Mapping[str, ReportTrigger], targets: ReportTargets) -> None:
    """Raise :class:`ConfigError` naming each problem of each of ``reports``, on a line of its
    own, if any, each at ``reports.<name>``, or ``reports.<name>.<field>``: a name among the
    ``targets``' taken ones, a role not among their lenses, a set not among their sets, or a
    prompt using a variable ``set_finished`` doesn't give."""
    problems = [
        f"reports.{name}{field}: {why}"
        for name, report in reports.items()
        for field, why in _report_problems(name, report, targets)
    ]
    if problems:
        raise ConfigError("\n".join(problems))


def _report_problems(
    name: str, report: ReportTrigger, targets: ReportTargets
) -> list[tuple[str, str]]:
    """Each problem of ``report``, named ``name``, by the field it's in after a dot, or none
    for its name, as :func:`check_reports` finds them against the ``targets`` it may name."""
    prompt = _variable_problems(SET_FINISHED, report.prompt)
    return [
        *_report_name_problems(name, targets.taken),
        *_lens_problems(report.role, targets.lenses),
        *_report_set_problems(report.set, targets.sets),
        *((f".{field}", why) for field, why in prompt),
    ]


def _report_name_problems(name: str, taken: Mapping[str, str]) -> list[tuple[str, str]]:
    """Why a report's ``name`` is refused, if it is: it's among the ``taken`` names, as another
    trigger's."""
    owner = taken.get(name)
    if owner is None:
        return []
    return [("", f"{name} is taken by {owner}; give this report another name.")]


def _lens_problems(role: str, lenses: Collection[str]) -> list[tuple[str, str]]:
    """Why the ``role`` a report queues a task for is refused, if it is: it isn't among the
    ``lenses``, the enabled roles granted ``report_finding``."""
    if role in lenses:
        return []
    known = f"those are {', '.join(lenses)}" if lenses else "none is"
    return [(".role", f"{role} isn't an enabled role granted report_finding; {known}.")]


def _report_set_problems(name: str, sets: Collection[str]) -> list[tuple[str, str]]:
    """Why the set ``name`` a report names is refused, if it is: it isn't among ``sets``, those
    a step is in, in the proposal or the project."""
    if name in sets:
        return []
    known = f"the sets are {', '.join(sets)}" if sets else "no step is in a set"
    return [(".set", f"{name} isn't a set in the proposal or the project; {known}.")]


def _tasks_of(roles: Sequence[str]) -> str:
    """The tasks a trigger listing ``roles`` queues, in words: ``a coder's task``, or ``tasks
    for style-reviewer, drift-reviewer and scaling-reviewer``."""
    if len(roles) == 1:
        return f"a {roles[0]}'s task"
    return f"tasks for {', '.join(roles[:-1])} and {roles[-1]}"


def check_triggers(
    triggers: Mapping[str, Trigger], roles: Collection[str], sets: Collection[str] = ()
) -> None:
    """Raise :class:`ConfigError` naming each problem of each of ``triggers``, on a line of its
    own, if any, each at ``triggers.<name>.<field>``: a role that isn't among ``roles`` (a
    disabled one is), neither a role nor ``checks``, or both, ``checks`` naming neither the task
    checks nor one of ``sets``, those a step is in, a role without a prompt or ``checks`` with
    one, a ``when`` field or a template variable its event doesn't give, a ``when`` value its
    field never takes on its event, a role not among ``roles`` or a value :data:`WHEN_VALUES`
    doesn't give, ``review-rounds`` or ``code-changed`` on a trigger on another event than
    ``task_landed``,
    ``raised-finding`` without ``review-rounds`` and ``task_landed``, or on a trigger running
    checks, ``every`` on another event than ``commits``, ``paths`` on another event than
    ``task_landed``, or naming no glob, ``cron`` missing on ``schedule``, on another event, or
    not a five-field cron expression that comes due, and ``manual`` without a ``label``."""
    problems = [
        f"triggers.{name}.{field}: {why}"
        for name, trigger in triggers.items()
        for field, why in _problems(trigger, _Targets(roles, sets))
    ]
    if problems:
        raise ConfigError("\n".join(problems))


@dataclass(frozen=True)
class _Targets:
    """What a trigger may act on: the ``roles`` it may queue a task for, and the ``sets`` it may
    run, besides the task checks."""

    roles: Collection[str]
    sets: Collection[str]


def _problems(trigger: Trigger, targets: _Targets) -> list[tuple[str, str]]:
    """Each problem of ``trigger``, by the field it's in, as :func:`check_triggers` finds them
    against the ``targets`` it may act on."""
    return [
        *_target_problems(trigger, targets),
        *_prompt_problems(trigger),
        *_when_problems(trigger, targets.roles),
        *_guard_problems(trigger),
        *_raised_problems(trigger),
        *_every_problems(trigger),
        *_paths_problems(trigger),
        *_cron_problems(trigger),
        *_label_problems(trigger),
    ]


def _target_problems(trigger: Trigger, targets: _Targets) -> list[tuple[str, str]]:
    """What's wrong with what ``trigger`` does: it needs a role among the ``targets``' roles, or
    a list of at least one, each among them and listed once, or ``checks`` naming the task checks
    or one of their sets, and not both."""
    if trigger.role is None:
        return _checks_problems(trigger.checks, targets.sets)
    if trigger.checks is not None:
        return [("checks", "a trigger has a role or checks, not both.")]
    if not trigger.roles:
        return [("role", "a list of roles names at least one.")]
    return _role_list_problems(Counter(trigger.roles), targets.roles)


def _role_list_problems(listed: Counter[str], roles: Collection[str]) -> list[tuple[str, str]]:
    """What's wrong with the roles a trigger lists, each counted as often as ``listed`` says:
    each needs to be among ``roles``, and listed once. Each is named once per problem."""
    unknown = [("role", _not_a_role(role, roles)) for role in listed if role not in roles]
    repeated = [
        ("role", f"{role} is listed {_how_often(count)}.")
        for role, count in listed.items()
        if count > 1
    ]
    return unknown + repeated


def _how_often(count: int) -> str:
    """How often something is listed ``count`` times, in words."""
    return "twice" if count == 2 else f"{count} times"


def _checks_problems(checks: str | None, sets: Collection[str]) -> list[tuple[str, str]]:
    """What's wrong with the ``checks`` of a trigger without a role: it needs them, naming the
    task checks or one of ``sets``, those a step is in."""
    if checks is None:
        return [("role", f'a trigger needs a role, or checks = "{TASK_CHECKS}" or a set.')]
    if checks == TASK_CHECKS or checks in sets:
        return []
    known = f"the sets are {', '.join(sets)}" if sets else "no step is in a set"
    return [("checks", f'{checks} isn\'t "{TASK_CHECKS}" or a set a step is in; {known}.')]


def _not_a_role(name: str, roles: Collection[str]) -> str:
    """Why ``name``, given as a role, is refused: it isn't among ``roles``."""
    return f"{name} isn't a role; the roles are {', '.join(roles)}."


def _prompt_problems(trigger: Trigger) -> list[tuple[str, str]]:
    """What's wrong with ``trigger``'s prompt: a role's task needs one, a run of the checks takes
    none, and it can use only the variables its event gives."""
    if trigger.prompt is None:
        return [] if trigger.role is None else [("prompt", "a trigger with a role needs a prompt.")]
    if trigger.checks is not None and trigger.role is None:
        return [("prompt", "checks queues no task, so it takes no prompt.")]
    return _variable_problems(trigger.on, trigger.prompt)


def _variable_problems(on: str, prompt: str) -> list[tuple[str, str]]:
    """The variables ``prompt``, the template of a trigger on the event ``on``, uses that the
    event doesn't give, each once."""
    given = EVENTS[on].variables
    braced = ", ".join(f"{{{name}}}" for name in given)
    return [
        ("prompt", f"{{{name}}} isn't a variable of {on}; its variables are {braced}.")
        for name in template_variables(prompt)
        if name not in given
    ]


def _when_problems(trigger: Trigger, roles: Collection[str]) -> list[tuple[str, str]]:
    """What's wrong with ``trigger``'s ``when``, field by field: one its event doesn't have, or
    each value it never takes, a role not among ``roles`` among them."""
    fields = EVENTS[trigger.on].fields
    theirs = f"its fields are {', '.join(fields)}" if fields else "it has none"
    return [
        ("when", why)
        for field, wanted in trigger.when.items()
        for why in (
            _value_problems(field, _values(wanted), roles, WHEN_VALUES.get(trigger.on, {}))
            if field in fields
            else [f"{field} isn't a field of {trigger.on}; {theirs}."]
        )
    ]


def _value_problems(
    field: str,
    values: Sequence[str],
    roles: Collection[str],
    fixed: Mapping[str, tuple[str, ...]],
) -> list[str]:
    """Why each of ``values``, a ``when`` field ``field``'s, is refused, if it is: a role not
    among ``roles``, or a value of a field its event ``fixed``, as :data:`WHEN_VALUES` gives
    them, not among its values."""
    if field == ROLE_FIELD:
        return [_not_a_role(value, roles) for value in values if value not in roles]
    known = fixed.get(field)
    return [] if known is None else _not_values(field, values, known)


def _not_values(field: str, values: Sequence[str], known: Sequence[str]) -> list[str]:
    """Why each of ``values``, a ``when`` field ``field``'s, is refused, if it is: it isn't among
    the field's ``known`` values."""
    return [
        f"{value} isn't a value of {field}; its values are {', '.join(known)}."
        for value in values
        if value not in known
    ]


def _guard_problems(trigger: Trigger) -> list[tuple[str, str]]:
    """Why ``review-rounds`` or ``code-changed`` can't guard ``trigger``, if they do: the review
    lane books its task into the review it finds due, which only falls due on ``task_landed``,
    and only a landing's commits change paths. Any number of triggers may take
    ``review-rounds``, whatever their roles, each task they queue for one review booked into it
    (decision 8)."""
    if trigger.on == TASK_LANDED:
        return []
    return [
        ("guards", f'{guard} needs on = "{TASK_LANDED}".')
        for guard in (REVIEW_ROUNDS, CODE_CHANGED)
        if guard in trigger.guards
    ]


_NEEDS_ROUNDS = f'{RAISED_FINDING} needs {REVIEW_ROUNDS} and on = "{TASK_LANDED}".'
_NEEDS_ROLE = f"{RAISED_FINDING} needs a role; a trigger running a set has none."


def _raised_problems(trigger: Trigger) -> list[tuple[str, str]]:
    """Why ``raised-finding`` can't guard ``trigger``, if it does: it only narrows a batch's
    review, which fires only the triggers on ``task_landed`` that ``review-rounds`` guards, and
    only by the roles a trigger queues for, which one running a set has none of."""
    if RAISED_FINDING not in trigger.guards:
        return []
    rounds = [] if _hears_batches(trigger) else [_NEEDS_ROUNDS]
    role = [_NEEDS_ROLE] if trigger.role is None and trigger.checks is not None else []
    return [("guards", why) for why in (*rounds, *role)]


def _hears_batches(trigger: Trigger) -> bool:
    """Whether ``trigger`` can hear a batch's review: it's on ``task_landed`` and
    ``review-rounds`` guards it."""
    return trigger.on == TASK_LANDED and REVIEW_ROUNDS in trigger.guards


def _every_problems(trigger: Trigger) -> list[tuple[str, str]]:
    """Why ``trigger``'s ``every`` is refused, if it is: it counts landings, which only
    ``commits`` does."""
    if trigger.every is None or trigger.on == COMMITS:
        return []
    return [("every", f'every = {trigger.every} needs on = "{COMMITS}".')]


def _paths_problems(trigger: Trigger) -> list[tuple[str, str]]:
    """Why ``trigger``'s ``paths`` are refused, if they are: they're matched against what a
    commit changed, which only ``task_landed`` gives, and an empty list, or a glob no git path
    can match, would never match."""
    if trigger.paths is None:
        return []
    event = [] if trigger.on == TASK_LANDED else [("paths", f'paths needs on = "{TASK_LANDED}".')]
    empty = [] if trigger.paths else [("paths", "a list of paths names at least one glob.")]
    unmatchable = [
        ("paths", unmatchable_glob(glob)) for glob in trigger.paths if not matchable(glob)
    ]
    return [*event, *empty, *unmatchable]


def _cron_problems(trigger: Trigger) -> list[tuple[str, str]]:
    """Why ``trigger``'s ``cron`` is refused, or missing, if it is: a ``schedule`` trigger needs
    one, no other takes one, and it's a five-field cron expression that comes due."""
    if trigger.cron is None:
        return [("cron", "a schedule trigger needs cron.")] if trigger.on == SCHEDULE else []
    if trigger.on != SCHEDULE:
        return [("cron", f'cron needs on = "{SCHEDULE}".')]
    problem = cron_problem(trigger.cron)
    return [] if problem is None else [("cron", f'cron = "{trigger.cron}" {problem}')]


def scheduled_crons(triggers: Mapping[str, Trigger]) -> dict[str, str]:
    """The cron expressions of the enabled ``schedule`` triggers among ``triggers``, by name, in
    their order."""
    return {
        name: trigger.cron
        for name, trigger in triggers.items()
        if trigger.enabled and trigger.on == SCHEDULE and trigger.cron is not None
    }


def _label_problems(trigger: Trigger) -> list[tuple[str, str]]:
    """Why ``trigger`` lacks a label, if it does: a manual one needs it."""
    return (
        [("label", "manual = true needs a label.")] if trigger.manual and not trigger.label else []
    )


def note_unrun_sets(triggers: Mapping[str, Trigger], sets: Iterable[str]) -> tuple[str, ...]:
    """The notice of each of ``sets`` no enabled one of ``triggers`` runs, in order."""
    run = {trigger.checks for trigger in triggers.values() if trigger.enabled}
    return tuple(
        f"The set {name} has no trigger, so it never runs." for name in sets if name not in run
    )


FIRING_PHRASES: Mapping[str, str] = {
    SESSION_STARTED: "when orbit opens",
    TASK_LANDED: "when a task lands",
    TASK_FINISHED: "when a task finishes",
    STARTUP_CHECK_FAILED: "when the startup check fails",
    "manual": "by hand",
    REVIEWS_FINISHED: "when a review's lenses have finished",
    SET_FINISHED: "when a set finishes",
    SCHEDULE: "on schedule",
}
"""When a trigger on each event but ``commits``, which counts, fires, in words, by name; a
``schedule`` trigger's is followed by its cron expression."""


def describe_firing(trigger: Trigger) -> str:
    """When ``trigger`` fires, in words, as in ``every 10 commits`` or ``by hand``, and that
    it's off if it isn't enabled."""
    when = _describe_event(trigger)
    if trigger.manual and trigger.on != "manual":
        when += " or by hand"
    return when if trigger.enabled else f"{when} (off)"


def _describe_event(trigger: Trigger) -> str:
    """When ``trigger``'s event happens, in words."""
    if trigger.on == SCHEDULE:
        return f"{FIRING_PHRASES[SCHEDULE]} {trigger.cron}"
    if trigger.on != COMMITS:
        return FIRING_PHRASES[trigger.on]
    every = trigger.every or 1
    return "every commit" if every == 1 else f"every {every} commits"


def template_variables(template: str) -> tuple[str, ...]:
    """The variables ``template`` uses, each once, in the order they first appear."""
    return tuple(dict.fromkeys(VARIABLE.findall(template)))


def render_template(template: str, values: Mapping[str, str]) -> str:
    """``template`` with each variable replaced by its value among ``values``, paragraph by
    paragraph, and any paragraph left blank dropped: a list-like variable's value is its block,
    or nothing when the list is empty, so its paragraph goes with it. Values aren't rendered
    themselves, and braces around anything but a variable's name are kept as they are."""
    paragraphs = (
        VARIABLE.sub(lambda found: values[found[1]], paragraph)
        for paragraph in template.split(PARAGRAPH_BREAK)
    )
    return PARAGRAPH_BREAK.join(paragraph for paragraph in paragraphs if paragraph.strip())


def render_held_prompt(template: str, values: Mapping[str, str]) -> str:
    """``template`` rendered from ``values`` as a review task of commits is kept until it
    starts: ``{diff}`` quoting :data:`~orbit.agent.domain.reviews.DIFF_HELD`, whatever diff
    ``values`` give."""
    return render_template(template, hold_diff(drop_diff(values)))


@dataclass(frozen=True)
class TriggerEvent:
    """An event as it's raised: its ``name``, the ``fields`` a trigger's ``when`` matches, its
    ``cause``, the task that caused it, if one did, and the ``commits`` it's of, a landing's own
    or a batch's, whose changed paths a trigger's ``paths`` match."""

    name: str
    fields: Mapping[str, str]
    cause: TaskState | None = None
    commits: tuple[str, ...] = ()


def listening(
    triggers: Mapping[str, Trigger],
    event: TriggerEvent,
    roles: Collection[str],
    review: ReviewJob | None,
    raised: Collection[str] = (),
) -> list[tuple[str, Trigger]]:
    """The ``triggers`` that may fire on ``event``, by name, in their order: the enabled ones on
    it that queue a task for one of ``roles``, the enabled roles, by their role or one of their
    list, run a named set, or run the task checks, on ``session_started`` only, whose ``when``
    takes it and that didn't queue the task that caused it, if any, as its origin says (decision
    9). One guarded by ``review-rounds`` only listens while today's rounds and batches find a
    ``review`` due, and one guarded by ``raised-finding``, on a batch's review, only if one of
    its roles is among those that ``raised`` the batch's findings (see :func:`firing_roles` for
    the roles it then queues for)."""
    return [
        (name, trigger)
        for name, trigger in triggers.items()
        if _hears(name, trigger, event, roles)
        and (review is not None or REVIEW_ROUNDS not in trigger.guards)
        and _raised_in(trigger, event, raised)
    ]


def _raised_in(trigger: Trigger, event: TriggerEvent, raised: Collection[str]) -> bool:
    """Whether ``raised-finding`` lets ``trigger`` hear ``event``: it doesn't guard it, the
    event isn't a batch's review, or one of the trigger's roles is among those that ``raised``
    the batch's findings."""
    return not _narrowed(trigger, event) or _queues_for(trigger, raised)


def _narrowed(trigger: Trigger, event: TriggerEvent | None) -> bool:
    """Whether ``raised-finding`` narrows the roles ``trigger`` queues for on ``event``: it
    guards it, and the event is a batch's review."""
    batch = (
        event is not None
        and event.name == TASK_LANDED
        and event.fields.get(REVIEW_FIELD) == "batch"
    )
    return batch and RAISED_FINDING in trigger.guards


def needs_paths(found: Iterable[tuple[str, Trigger]]) -> bool:
    """Whether one of the triggers ``found`` listening, by name, is decided by the paths their
    event's commits changed, as :func:`reads_paths` says, so those paths are needed to decide
    whether it fires."""
    return any(reads_paths(trigger) for _, trigger in found)


def reads_paths(trigger: Trigger) -> bool:
    """Whether whether ``trigger`` fires on a landing depends on the paths its commits changed:
    it has ``paths``, or ``code-changed`` guards it."""
    return trigger.paths is not None or _code_guarded(trigger)


def _code_guarded(trigger: Trigger) -> bool:
    """Whether ``code-changed`` decides ``trigger``: it guards it, and it has no ``paths`` of
    its own, which would decide it alone."""
    return trigger.paths is None and CODE_CHANGED in trigger.guards


@dataclass(frozen=True)
class Sifted:
    """The triggers listening for a landing, by name, in order, sifted by the paths its commits
    changed: those that ``fire``, and those ``code-changed`` ``held`` back."""

    fire: list[tuple[str, Trigger]]
    held: list[tuple[str, Trigger]]


def sift_changes(
    found: Sequence[tuple[str, Trigger]], changed: Collection[str], mapped_paths: Sequence[str]
) -> Sifted:
    """The triggers ``found`` listening, by name, in order, sifted by the paths ``changed``:
    those their ``paths`` let fire, as :func:`within_paths` says, but those ``code-changed``
    holds back, without ``paths`` of their own, when none of the paths ``changed`` is code, with
    ``mapped_paths`` the paths of the project's ``[[skills]]``."""
    within = within_paths(found, changed)
    held = (
        []
        if changes_code(changed, mapped_paths)
        else [(name, trigger) for name, trigger in within if _code_guarded(trigger)]
    )
    return Sifted(fire=[heard for heard in within if heard not in held], held=held)


def note_skipped_reviews(
    held: Sequence[tuple[str, Trigger]],
    prompts: Sequence[str],
    roles: Collection[str],
    event: TriggerEvent | None = None,
    raised: Collection[str] = (),
) -> str | None:
    """What the conversation says of the triggers ``code-changed`` ``held`` back, by name, on a
    landing of the tasks with ``prompts``: the reviews of the roles each would have queued for
    on ``event``, as :func:`firing_roles` says with ``roles``, the enabled roles, and those that
    ``raised`` the batch's findings, each without its ``-reviewer`` suffix, of the first task,
    by its title, its prompt's first line as its row shows it, and how many more; none if they
    would queue for no role, or there's no task."""
    lenses = tuple(
        dict.fromkeys(
            role.removesuffix(LENS_SUFFIX)
            for _, trigger in held
            for role in firing_roles(trigger, roles, event, raised)
        )
    )
    if not lenses or not prompts:
        return None
    title = prompts[0].partition("\n")[0]
    more = f" and {len(prompts) - 1} more" if len(prompts) > 1 else ""
    return f'Skipped the {_joined(lenses)} reviews of "{title}"{more}: it changed no code.'


LENS_SUFFIX = "-reviewer"
"""What a lens role's name ends with, left out where the note of its skipped reviews names it."""


def _joined(names: Sequence[str]) -> str:
    """``names`` in words, as ``a, b and c``."""
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def within_paths(
    found: Sequence[tuple[str, Trigger]], changed: Collection[str]
) -> list[tuple[str, Trigger]]:
    """The triggers ``found`` listening, by name, in order, that the paths ``changed`` let fire:
    those without ``paths``, and those one of whose globs matches one of them."""
    return [
        (name, trigger)
        for name, trigger in found
        if trigger.paths is None or _changes_any(trigger.paths, changed)
    ]


def _changes_any(patterns: Sequence[str], changed: Collection[str]) -> bool:
    """Whether one of the paths ``changed`` matches one of the globs ``patterns``."""
    return any(matches_path(pattern, path) for pattern in patterns for path in changed)


def firing_roles(
    trigger: Trigger,
    roles: Collection[str],
    event: TriggerEvent | None = None,
    raised: Collection[str] = (),
) -> tuple[str, ...]:
    """The roles ``trigger`` queues a task for as it fires, in its order: those it lists among
    ``roles``, the enabled roles, and, on ``event``, if it's a batch's review that
    ``raised-finding`` guards it on, only those among the roles that ``raised`` the batch's
    findings, so each lens of a list reviews only the fixes of its own findings. Without an
    ``event``, as on a landing counted or by hand, none is narrowed."""
    narrowed = _narrowed(trigger, event)
    return tuple(
        role for role in trigger.roles if role in roles and (not narrowed or role in raised)
    )


def can_queue_reviews(triggers: Mapping[str, Trigger], roles: Collection[str]) -> bool:
    """Whether a review of commits can be queued: one of ``triggers`` that ``review-rounds``
    guards is enabled and queues a task for one of ``roles``, the enabled roles, or runs a
    named set."""
    return any(
        trigger.enabled and REVIEW_ROUNDS in trigger.guards and _acts(trigger, TASK_LANDED, roles)
        for trigger in triggers.values()
    )


def _hears(name: str, trigger: Trigger, event: TriggerEvent, roles: Collection[str]) -> bool:
    """Whether ``trigger``, named ``name``, hears ``event``, as :func:`listening` says, but for
    its guards."""
    return (
        trigger.enabled
        and trigger.on == event.name
        and _acts(trigger, event.name, roles)
        and takes(trigger.when, event.fields)
        and (event.cause is None or event.cause.origin != name)
    )


def takes(when: Mapping[str, str | tuple[str, ...]], fields: Mapping[str, str]) -> bool:
    """Whether a trigger's ``when`` takes an event with ``fields``: each field it names has its
    value, or one of its values."""
    return all(
        field in fields and fields[field] in _values(wanted) for field, wanted in when.items()
    )


def _acts(trigger: Trigger, event: str, roles: Collection[str]) -> bool:
    """Whether ``trigger`` does something on the event named ``event``: queues a task for one of
    ``roles``, runs a named set, on any event, or runs the task checks, only on
    ``session_started``, as the startup check."""
    if trigger.checks is None:
        return _queues_for(trigger, roles)
    return trigger.checks != TASK_CHECKS or event == SESSION_STARTED


def _queues_for(trigger: Trigger, roles: Collection[str]) -> bool:
    """Whether ``trigger`` queues a task for one of ``roles``: its role, or one of its list, is
    among them."""
    return any(role in roles for role in trigger.roles)


def _values(wanted: str | tuple[str, ...]) -> tuple[str, ...]:
    """The values ``wanted``, a ``when`` field's value or values, allows."""
    return (wanted,) if isinstance(wanted, str) else wanted


def held_back(trigger: Trigger, prompt: str | None, tasks: Sequence[TaskState]) -> bool:
    """Whether a guard of ``trigger`` holds back its task, rendered as ``prompt``, with ``tasks``
    on the board, of which only those still to run or running count, so a caller may pass just
    those: ``not-queued`` while a task of one of its roles is still to run or running,
    ``dedupe`` while such a task's prompt has the same first line, and ``suggestions-pending``
    while a task the reviews queued is: a task orbit queued as a review, booked into its review
    lane, whatever its role, or a task in a review's batch. ``review-rounds`` and
    ``raised-finding`` are decided as it listens, and ``code-changed`` with the paths a landing
    changed (see :func:`sift_changes`). Without a ``prompt``, as a landing is counted
    before there's one, ``dedupe`` holds nothing back."""
    waiting = [state for state in tasks if state.status in WAITING]
    return any(_holds(guard, trigger, prompt, waiting) for guard in trigger.guards)


def _holds(
    guard: GuardName, trigger: Trigger, prompt: str | None, waiting: Sequence[TaskState]
) -> bool:
    """Whether ``guard`` holds back ``trigger``'s task, ``prompt``, if there's one yet, with the
    tasks ``waiting``."""
    match guard:
        case "not-queued":
            return _queued(trigger.roles, waiting)
        case "dedupe":
            return prompt is not None and _repeats(prompt, waiting)
        case "suggestions-pending":
            return _reviews_queued(waiting)
        case _:
            return False


def _queued(roles: Collection[str], waiting: Sequence[TaskState]) -> bool:
    """Whether one of the tasks ``waiting`` is one of ``roles``'."""
    return any(state.task.role in roles for state in waiting)


def _reviews_queued(waiting: Sequence[TaskState]) -> bool:
    """Whether one of the tasks ``waiting`` was queued by the reviews: a task orbit queued as a
    review, booked into its review lane, whatever its role (see
    :func:`~orbit.protocol.is_orbit_review`), or a task in a review's batch, a suggestion or a
    fix of one. What the tasks hold says so, so a session read back from a save is judged
    alike."""
    return any(state.batch is not None or is_orbit_review(state) for state in waiting)


def _repeats(prompt: str, waiting: Sequence[TaskState]) -> bool:
    """Whether one of the tasks ``waiting`` has the first line of ``prompt``."""
    return any(_title(state.task.prompt) == _title(prompt) for state in waiting)


def _title(prompt: str) -> str:
    """The first line of ``prompt``, stripped: its task's title."""
    return prompt.strip().split("\n", 1)[0].strip()


@dataclass(frozen=True)
class Counted:
    """A landing counted toward the ``commits`` triggers: the landings counted toward each since
    it last fired, by name, after it, ``counts``, and the triggers it ``fires``, by name, in
    order."""

    counts: Mapping[str, int]
    fires: list[tuple[str, Trigger]]


def count_landing(
    triggers: Mapping[str, Trigger],
    event: TriggerEvent,
    roles: Collection[str],
    counts: Mapping[str, int],
    tasks: Sequence[TaskState],
) -> Counted:
    """Count ``event``, a landing on ``commits``, toward each of ``triggers`` that hears it, as
    :func:`listening` says with ``roles`` the enabled roles, from ``counts``, the landings
    counted toward each since it last fired, by name: its count goes up by one, and once that
    reaches its ``every`` (1 if it sets none) it's due, and fires unless a guard holds it back
    with ``tasks`` on the board, as :func:`held_back` says, its count starting again. One held
    back stays due, and fires on the first landing it hears once none does."""
    heard = listening(triggers, event, roles, None)
    raised = {**counts, **{name: counts.get(name, 0) + 1 for name, _ in heard}}
    fires = [
        (name, trigger)
        for name, trigger in heard
        if _due(trigger, raised[name]) and not held_back(trigger, None, tasks)
    ]
    return Counted(restart_counts(raised, (name for name, _ in fires)), fires)


def _due(trigger: Trigger, count: int) -> bool:
    """Whether ``trigger``, a ``commits`` one with ``count`` landings counted toward it since it
    last fired, is due: once the count reaches its ``every``, 1 if it sets none."""
    return count >= (trigger.every or 1)


def restart_counts(counts: Mapping[str, int], fired: Iterable[str]) -> dict[str, int]:
    """``counts``, the landings counted toward each ``commits`` trigger since it last fired, by
    name, with those of the triggers ``fired`` starting again."""
    return {**counts, **dict.fromkeys(fired, 0)}


def fires_by_hand(trigger: Trigger, roles: Collection[str]) -> bool:
    """Whether ``trigger`` fires when it's fired by hand, with ``roles`` the enabled roles: if
    it's enabled and queues a task for one of them or runs a named set, whatever its ``when``
    and guards. The task checks run only as the startup check, so not by hand."""
    acts = _queues_for(trigger, roles) or trigger.checks not in (None, TASK_CHECKS)
    return trigger.enabled and acts


def trigger_tab(triggers: Mapping[str, Trigger], origin: str, role: str) -> str | None:
    """The tab of the trigger among ``triggers`` named ``origin``, a task's origin, which the
    task's agent, in ``role``, shows under; none if no trigger has that name, it has no tab, or
    it queues no task for ``role``, as a merge pass's fix coder's."""
    trigger = triggers.get(origin)
    return None if trigger is None or role not in trigger.roles else trigger.tab


@dataclass(frozen=True)
class SetOutcome:
    """How a run of a named set came out: the set's ``name``, the ``commit`` it ran on, and, if
    it failed, the ``failed`` step's name with its output."""

    name: str
    commit: str
    failed: tuple[str, str] | None = None

    @property
    def status(self) -> SetStatus:
        """What ``set_finished``'s ``status`` field says of it."""
        return "passed" if self.failed is None else "failed"


def set_finished_event(outcome: SetOutcome) -> TriggerEvent:
    """``set_finished`` as ``outcome``, a named set's run ending, raises it: the set's name and
    how it came out, as its ``set`` and ``status``."""
    return TriggerEvent(SET_FINISHED, {"set": outcome.name, "status": outcome.status})


def set_finished_values(outcome: SetOutcome) -> dict[str, str]:
    """The variables of ``set_finished`` for ``outcome``, but ``{trigger}``, the caller's: the
    set's name, its status, the commit it ran on, abbreviated, and the failing step's name with
    its output, trimmed as the verification panel trims it, as ``{output}``, empty when it
    passed, so its paragraph drops."""
    output = "" if outcome.failed is None else set_report(*outcome.failed)
    return {
        "set": outcome.name,
        "status": outcome.status,
        "commit": outcome.commit[:7],
        "output": output,
    }


def verification_state(steps: Sequence[VerificationStep] | None) -> VerificationState:
    """What ``session_started``'s ``verification`` field says of the task checks, ``steps``,
    none while verification is off: ``configured`` with at least one, ``missing`` with none."""
    if steps is None:
        return "off"
    return "configured" if steps else "missing"
