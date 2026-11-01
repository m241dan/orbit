"""Triggers, which turn an event into a task for a role or a run of checks, the task checks or a
named set: the events and guards orbit knows, a trigger's fields, the package's triggers merged
with a project's and checked against its roles and sets, the sets no trigger runs, which of them
fire on an event and which guards hold one back, when one fires in words, and the rendering of
a trigger's prompt template.

The package's triggers aren't kept here: whoever builds them passes in the contents of their
``triggers.toml`` and the templates it names. ``manual`` isn't raised yet, but a trigger fired by
hand fires whatever its event; a trigger that runs a named set fires on any event, and one that
runs the task checks only on ``session_started``, as the startup check.
"""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, get_args

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
from orbit.agent.domain.config_errors import ConfigError, validation_problems
from orbit.agent.domain.layers import DEFAULT, PACKAGE, Layer
from orbit.agent.domain.reviews import ReviewJob
from orbit.agent.domain.roles import PLAIN_NAME
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import REVIEWER, TASK_CHECKS, TaskState, is_orbit_review

EventName = Literal[
    "session_started",
    "task_landed",
    "task_finished",
    "commits",
    "startup_check_failed",
    "manual",
]
"""What a trigger fires on: orbit opening a workspace with its session, a task's work landing, a
task finishing, landings counted up to a trigger's ``every``, a step of the startup check
failing, or the user firing it."""

GuardName = Literal["review-rounds", "suggestions-pending", "not-queued", "dedupe"]
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
due, whose job its task reviews."""

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
        ("role", "origin"),
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
}
"""Each event, by name, with what it gives. A trigger fired by hand gives the variables of its
event, as the super review does those of ``commits``."""

ROLE_FIELD = "role"
"""The ``when`` field naming the role of an event's task, whose values are the roles: a disabled
one among them."""

WHEN_VALUES: Mapping[str, tuple[str, ...]] = {"verification": VERIFICATION_STATES}
"""The values the events give each ``when`` field orbit fixes them for, by its name. Besides
these, :data:`ROLE_FIELD`'s are the roles, and ``origin``, ``status`` and ``step`` take any
value: origins include trigger names, and steps are the project's own."""


def _plain_trigger_name(name: str) -> str:
    """The trigger's ``name``, refused unless it's a plain name."""
    if not PLAIN_NAME.fullmatch(name):
        raise PydanticCustomError(
            "trigger_name", "a trigger's name is a plain name: letters, digits, - and _"
        )
    return name


TriggerName = Annotated[str, AfterValidator(_plain_trigger_name)]
"""A trigger's name, a plain name as a role's is, since its settings are keyed by it between
dots, as in ``triggers.review.role``."""


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Trigger(_Config):
    """A trigger, as a file sets it: what it fires ``on``, the ``role`` of the task it queues
    with its ``prompt``, or the checks it runs, the task checks or a named set, as ``checks``
    (``gate`` read as the task checks), the events it takes ``when`` they match, the ``guards``
    that may hold it back, how many landings it fires after, ``every``, whether it's ``manual``,
    under its ``label``, whether its task is a ``draft``, and whether it's ``enabled``."""

    on: EventName = Field(
        description='The event it fires on, such as "task_landed", "commits" or "manual".'
    )
    role: str | None = Field(default=None, description="The role of the task it queues.")
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
            "The named rules that may hold it back: review-rounds, suggestions-pending, not-queued"
            " or dedupe."
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
        f"triggers.{name}.checks: {name} is taken by a built-in trigger, which queues a"
        f" {role}'s task; give this trigger another name."
        for name in names
        if name in package and (role := package[name].role) is not None
    ]
    if problems:
        raise ConfigError("\n".join(problems))


def check_triggers(
    triggers: Mapping[str, Trigger], roles: Collection[str], sets: Collection[str] = ()
) -> None:
    """Raise :class:`ConfigError` naming each problem of each of ``triggers``, on a line of its
    own, if any, each at ``triggers.<name>.<field>``: a role that isn't among ``roles`` (a
    disabled one is), neither a role nor ``checks``, or both, ``checks`` naming neither the task
    checks nor one of ``sets``, those a step is in, a role without a prompt or ``checks`` with
    one, a ``when`` field or a template variable its event doesn't give, a ``when`` value its
    field never takes, a role not among ``roles`` or a value :data:`WHEN_VALUES` doesn't give,
    ``review-rounds`` on a trigger that isn't a ``reviewer``'s on ``task_landed`` or on an
    enabled one after the first, ``every`` on another event than ``commits``, and ``manual``
    without a ``label``."""
    owner = _review_rounds_owner(triggers)
    problems = [
        f"triggers.{name}.{field}: {why}"
        for name, trigger in triggers.items()
        for field, why in _problems(name, trigger, _Targets(roles, sets), owner)
    ]
    if problems:
        raise ConfigError("\n".join(problems))


@dataclass(frozen=True)
class _Targets:
    """What a trigger may act on: the ``roles`` it may queue a task for, and the ``sets`` it may
    run, besides the task checks."""

    roles: Collection[str]
    sets: Collection[str]


def _problems(
    name: str, trigger: Trigger, targets: _Targets, owner: str | None
) -> list[tuple[str, str]]:
    """Each problem of ``trigger``, named ``name``, by the field it's in, as
    :func:`check_triggers` finds them against the ``targets`` it may act on, with ``owner`` the
    trigger ``review-rounds`` may guard."""
    return [
        *_target_problems(trigger, targets),
        *_prompt_problems(trigger),
        *_when_problems(trigger, targets.roles),
        *_guard_problems(name, trigger, owner),
        *_every_problems(trigger),
        *_label_problems(trigger),
    ]


def _target_problems(trigger: Trigger, targets: _Targets) -> list[tuple[str, str]]:
    """What's wrong with what ``trigger`` does: it needs a role among the ``targets``' roles or
    ``checks`` naming the task checks or one of their sets, and not both."""
    if trigger.role is None:
        return _checks_problems(trigger.checks, targets.sets)
    if trigger.checks is not None:
        return [("checks", "a trigger has a role or checks, not both.")]
    if trigger.role not in targets.roles:
        return [("role", _not_a_role(trigger.role, targets.roles))]
    return []


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
            _value_problems(field, _values(wanted), roles)
            if field in fields
            else [f"{field} isn't a field of {trigger.on}; {theirs}."]
        )
    ]


def _value_problems(field: str, values: Sequence[str], roles: Collection[str]) -> list[str]:
    """Why each of ``values``, a ``when`` field ``field``'s, is refused, if it is: a role not
    among ``roles``, or a value of a field in :data:`WHEN_VALUES` not among its values."""
    if field == ROLE_FIELD:
        return [_not_a_role(value, roles) for value in values if value not in roles]
    known = WHEN_VALUES.get(field)
    return [] if known is None else _not_values(field, values, known)


def _not_values(field: str, values: Sequence[str], known: Sequence[str]) -> list[str]:
    """Why each of ``values``, a ``when`` field ``field``'s, is refused, if it is: it isn't among
    the field's ``known`` values."""
    return [
        f"{value} isn't a value of {field}; its values are {', '.join(known)}."
        for value in values
        if value not in known
    ]


def _review_rounds_owner(triggers: Mapping[str, Trigger]) -> str | None:
    """The name of the first enabled one of ``triggers`` that ``review-rounds`` guards, if any:
    the only one it may guard, since every trigger it guards would fire on the same review."""
    return next(
        (
            name
            for name, trigger in triggers.items()
            if trigger.enabled and REVIEW_ROUNDS in trigger.guards
        ),
        None,
    )


def _guard_problems(name: str, trigger: Trigger, owner: str | None) -> list[tuple[str, str]]:
    """Why ``review-rounds`` can't guard ``trigger``, named ``name``, if it does: the review lane
    books its task as the review it finds due, so it must queue a ``reviewer``'s task on
    ``task_landed``, the only event a review falls due on, and be ``owner``, the trigger it may
    guard, if it's enabled (decision 12)."""
    if REVIEW_ROUNDS not in trigger.guards:
        return []
    return [
        ("guards", why)
        for why, misused in (
            (f'{REVIEW_ROUNDS} needs on = "{TASK_LANDED}".', trigger.on != TASK_LANDED),
            (f'{REVIEW_ROUNDS} needs role = "{REVIEWER}".', trigger.role != REVIEWER),
            (
                f"{REVIEW_ROUNDS} guards only one enabled trigger, and {owner} already has it.",
                trigger.enabled and owner != name,
            ),
        )
        if misused
    ]


def _every_problems(trigger: Trigger) -> list[tuple[str, str]]:
    """Why ``trigger``'s ``every`` is refused, if it is: it counts landings, which only
    ``commits`` does."""
    if trigger.every is None or trigger.on == COMMITS:
        return []
    return [("every", f'every = {trigger.every} needs on = "{COMMITS}".')]


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
}
"""When a trigger on each event but ``commits``, which counts, fires, in words, by name."""


def describe_firing(trigger: Trigger) -> str:
    """When ``trigger`` fires, in words, as in ``every 10 commits`` or ``by hand``, and that
    it's off if it isn't enabled."""
    when = _describe_event(trigger)
    if trigger.manual and trigger.on != "manual":
        when += " or by hand"
    return when if trigger.enabled else f"{when} (off)"


def _describe_event(trigger: Trigger) -> str:
    """When ``trigger``'s event happens, in words."""
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


@dataclass(frozen=True)
class TriggerEvent:
    """An event as it's raised: its ``name``, the ``fields`` a trigger's ``when`` matches, and
    its ``cause``, the task that caused it, if one did."""

    name: str
    fields: Mapping[str, str]
    cause: TaskState | None = None


def listening(
    triggers: Mapping[str, Trigger],
    event: TriggerEvent,
    roles: Collection[str],
    review: ReviewJob | None,
) -> list[tuple[str, Trigger]]:
    """The ``triggers`` that may fire on ``event``, by name, in their order: the enabled ones on
    it that queue a task for one of ``roles``, the enabled roles, run a named set, or run the task
    checks, on ``session_started`` only, whose ``when`` takes it and that didn't queue the task
    that caused it, if any, as its origin says (decision 9). One guarded by ``review-rounds``
    only listens while today's rounds and batches find a ``review`` due."""
    return [
        (name, trigger)
        for name, trigger in triggers.items()
        if _hears(name, trigger, event, roles)
        and (review is not None or REVIEW_ROUNDS not in trigger.guards)
    ]


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
        return trigger.role in roles
    return trigger.checks != TASK_CHECKS or event == SESSION_STARTED


def _values(wanted: str | tuple[str, ...]) -> tuple[str, ...]:
    """The values ``wanted``, a ``when`` field's value or values, allows."""
    return (wanted,) if isinstance(wanted, str) else wanted


def held_back(trigger: Trigger, prompt: str | None, tasks: Sequence[TaskState]) -> bool:
    """Whether a guard of ``trigger`` holds back its task, rendered as ``prompt``, with ``tasks``
    on the board: ``not-queued`` while a task of its role is still to run or running,
    ``dedupe`` while such a task's prompt has the same first line, and ``suggestions-pending``
    while a task the reviews queued is: a task orbit queued as a review, booked into its review
    lane, whatever its role, or a task in a review's batch. ``review-rounds`` is decided as it
    listens. Without a ``prompt``, as a landing is counted before there's one, ``dedupe`` holds
    nothing back."""
    waiting = [state for state in tasks if state.status in WAITING]
    return any(_holds(guard, trigger, prompt, waiting) for guard in trigger.guards)


def _holds(
    guard: GuardName, trigger: Trigger, prompt: str | None, waiting: Sequence[TaskState]
) -> bool:
    """Whether ``guard`` holds back ``trigger``'s task, ``prompt``, if there's one yet, with the
    tasks ``waiting``."""
    match guard:
        case "not-queued":
            return _queued(trigger.role, waiting)
        case "dedupe":
            return prompt is not None and _repeats(prompt, waiting)
        case "suggestions-pending":
            return _reviews_queued(waiting)
        case _:
            return False


def _queued(role: str | None, waiting: Sequence[TaskState]) -> bool:
    """Whether one of the tasks ``waiting`` is ``role``'s."""
    return any(state.task.role == role for state in waiting)


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
    with ``tasks`` on the board, its count starting again. One held back stays due, and fires
    on the first landing it hears once none does."""
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
    acts = trigger.role in roles or trigger.checks not in (None, TASK_CHECKS)
    return trigger.enabled and acts


def verification_state(steps: Sequence[VerificationStep] | None) -> VerificationState:
    """What ``session_started``'s ``verification`` field says of the task checks, ``steps``,
    none while verification is off: ``configured`` with at least one, ``missing`` with none."""
    if steps is None:
        return "off"
    return "configured" if steps else "missing"
