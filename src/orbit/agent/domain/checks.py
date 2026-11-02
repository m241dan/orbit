"""The checks a project is verified and set up with: its verification steps, the task checks
among them and the named sets the others are in, the setup that makes a fresh worktree ready, and
the checks an agent proposes, with the triggers that would run them and the reports routing its
sets' runs to lens roles, and what a configurator is told is configured now."""

import shlex
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from orbit.agent.domain.config_errors import ConfigError, validation_problems
from orbit.agent.domain.roles import Role, finding_roles
from orbit.agent.domain.triggers import (
    SET_FINISHED,
    ChecksTrigger,
    ReportTargets,
    ReportTrigger,
    Trigger,
    TriggerName,
    check_free_names,
    check_reports,
    check_triggers,
    describe_firing,
    merge_proposed,
)
from orbit.agent.domain.verification import CheckSet, SetEffect, SetName, VerificationStep
from orbit.protocol import TASK_CHECKS


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class VerificationSet(_Config):
    """A named set of verification steps, as its ``[verification.sets.<name>]`` table sets it:
    whether its failure ``holds_merges``."""

    holds_merges: bool = Field(
        default=False,
        description=(
            "Whether its failure holds merges until a fix lands; otherwise it only reports."
        ),
    )


class VerificationConfig(_Config):
    """The verification ``steps``, in order: the task checks, which verify every task, and those
    in named sets, with the ``sets``' tables, by name."""

    steps: tuple[VerificationStep, ...] = Field(
        default=(),
        description=(
            "The verification steps, in order; the task checks, in no set, verify every task."
        ),
    )
    sets: dict[SetName, VerificationSet] = Field(
        default_factory=dict,
        description=(
            'The named sets steps are in, by name; a trigger runs one with checks = "<name>".'
        ),
    )

    @property
    def task_checks(self) -> tuple[VerificationStep, ...]:
        """The steps in no set, in order: those every task, and the startup check, runs."""
        return tuple(step for step in self.steps if step.set is None)

    @property
    def set_names(self) -> tuple[str, ...]:
        """The names of the sets the steps are in, each once, in the order they first appear."""
        return tuple(dict.fromkeys(step.set for step in self.steps if step.set is not None))

    @property
    def check_sets(self) -> tuple[CheckSet, ...]:
        """The named sets, in the order they first appear, each with its steps, in order, and
        what its failure does, as its table says: only report, without one."""
        return tuple(
            CheckSet(
                name,
                tuple(step for step in self.steps if step.set == name),
                _effect(self.sets.get(name)),
            )
            for name in self.set_names
        )

    @field_validator("steps")
    @classmethod
    def _complete_steps(cls, steps: tuple[VerificationStep, ...]) -> tuple[VerificationStep, ...]:
        numbers: dict[str, int] = {}
        for number, step in enumerate(steps, start=1):
            if not step.name.strip():
                raise ValueError(f"step {number} has no name")
            if not step.command:
                raise ValueError(f"step {number} has no command")
            if not step.command[0].strip():
                raise ValueError(f"step {number} has no program")
            if step.name in numbers:
                raise ValueError(f"step {number} has the same name as step {numbers[step.name]}")
            numbers[step.name] = number
        return steps


class Setup(_Config):
    """What makes a fresh worktree ready to work in: ``command`` runs there when it's first
    prepared, and again whenever a file in ``watch`` (paths relative to the tree) changes."""

    command: tuple[str, ...] = Field(
        min_length=1,
        description="The command run in a fresh worktree, and again when a watched file changes.",
    )
    watch: tuple[str, ...] = Field(
        default=(), description="Files, relative to the tree, whose changes run the command again."
    )

    @field_validator("watch")
    @classmethod
    def _relative_paths(cls, watch: tuple[str, ...]) -> tuple[str, ...]:
        for path in watch:
            if not path or path.startswith("/") or ".." in path.split("/"):
                raise ValueError(f"{path!r} isn't a relative path inside the project")
        return watch


def _effect(table: VerificationSet | None) -> SetEffect:
    """What the failure of a set with ``table``, if it has one, does."""
    return "holds_merges" if table is not None and table.holds_merges else "reports"


def check_sets(verification: VerificationConfig, kept: Collection[str] = ()) -> None:
    """Raise :class:`ConfigError` naming each of ``verification``'s sets' tables for a set no
    step is in, neither one of its own nor one of the ``kept`` sets a file keeps beside them, on
    a line of its own, if any, each at ``verification.sets.<name>``."""
    problems = [
        f'verification.sets.{name}: no step is in it; a step joins it with set = "{name}".'
        for name in verification.sets
        if name not in verification.set_names and name not in kept
    ]
    if problems:
        raise ConfigError("\n".join(problems))


def setup_step(setup: Setup) -> VerificationStep:
    """``setup``'s command, as a step run before the others."""
    return VerificationStep("setup", setup.command)


class ChecksProposal(VerificationConfig):
    """Verification steps, at least one, a setup, the named sets' tables, each given as a table
    or as whether it holds merges, the ``triggers`` that run the checks, by name, and the
    ``reports`` routing a set's runs to a lens role, by name, as an agent proposes them."""

    steps: tuple[VerificationStep, ...] = Field(min_length=1)
    setup: Setup | None = None
    triggers: dict[TriggerName, ChecksTrigger] = Field(default_factory=dict)
    reports: dict[TriggerName, ReportTrigger] = Field(default_factory=dict)

    @field_validator("sets", mode="before")
    @classmethod
    def _tables_of_holds(cls, sets: object) -> object:
        if not isinstance(sets, dict):
            return sets
        return {
            name: {"holds_merges": table} if isinstance(table, bool) else table
            for name, table in sets.items()
        }

    @property
    def set_effects(self) -> dict[SetName, SetEffect]:
        """What the failure of each set the proposal writes the table of does, by name: those
        its steps are in, in order, then those only the ``sets`` give a table, sets a file keeps
        as configured beside the proposal, whose tables it replaces."""
        effects = {check_set.name: check_set.effect for check_set in self.check_sets}
        return effects | {name: _effect(table) for name, table in self.sets.items()}

    @property
    def trigger_settings(self) -> dict[str, Trigger]:
        """The ``triggers``, by name, as the config would read them."""
        return {name: trigger.as_trigger() for name, trigger in self.triggers.items()}

    @property
    def report_settings(self) -> dict[str, Trigger]:
        """The ``reports``, by name, as the config would read them: ``set_finished`` triggers."""
        return {name: report.as_trigger() for name, report in self.reports.items()}


_NO_TRIGGERS: Mapping[str, Trigger] = MappingProxyType({})
_NO_ROLES: Mapping[str, Role] = MappingProxyType({})


def merge_proposed_sets(
    configured: Sequence[CheckSet], proposal: ChecksProposal
) -> tuple[CheckSet, ...]:
    """The named sets once ``proposal`` is written over the ``configured`` ones, as the config
    reads them then: its sets, each with its steps and what its failure does, then each of the
    ``configured`` sets none of its steps is in, as it was but for what its failure does, if
    ``proposal.sets`` says."""
    effects = proposal.set_effects
    kept = (
        replace(check_set, effect=effects.get(check_set.name, check_set.effect))
        for check_set in configured
        if check_set.name not in proposal.set_names
    )
    return (*proposal.check_sets, *kept)


def merge_proposed_triggers(
    configured: Mapping[str, Trigger], built_in: Mapping[str, Trigger], proposal: ChecksProposal
) -> dict[str, Trigger]:
    """The triggers once ``proposal`` is written over the ``configured`` ones, by name, as the
    config reads them then: each of the ``configured`` in its order, but that one of the
    proposal's of its name takes its place, then the proposal's new ones, each merged over the
    ``built_in`` trigger of its name, if any (see
    :func:`~orbit.agent.domain.triggers.merge_proposed`), then its reports, the ``set_finished``
    triggers they are."""
    proposed = merge_proposed(built_in, proposal.triggers)
    return {**configured, **proposed, **proposal.report_settings}


_EFFECT_PHRASES: Mapping[SetEffect, str] = {"holds_merges": "holds merges", "reports": "reports"}
"""What the failure of a set with each effect does, in words."""

_REPORT_PHRASES: Mapping[tuple[str, ...], str] = {
    ("failed",): "when it fails",
    ("passed",): "when it passes",
}
"""When a report taking each ``status``, alone, queues its lens's task, in words; a report taking
any other, or none, does when its set finishes."""


ReportScope = Literal["sets", "all"]
"""Which reports a description lists: ``sets``, only those on the sets it describes, or
``all``, also those on sets kept as configured, each under its set's name."""


def describe_checks(
    steps: Sequence[VerificationStep],
    setup: Setup | None,
    sets: Sequence[CheckSet] = (),
    triggers: Mapping[str, Trigger] = _NO_TRIGGERS,
    reports: ReportScope = "sets",
) -> str:
    """``steps``, the task checks, one per line, then ``setup``; each part only if there is
    one. With named ``sets``, the task checks come under a line saying when they run, then each
    set with its steps, under a line saying when those of ``triggers`` that run it fire and
    whether it holds merges, then the ``set_finished`` triggers reporting it to a lens, as the
    proposal window groups them. With ``reports`` ``all``, the triggers reporting a set none of
    ``sets`` is come before the setup, under a line naming their set, kept as configured."""
    groups = _describe_groups(steps, sets, triggers) if sets else _describe_steps(steps)
    kept = _describe_kept(sets, triggers) if reports == "all" else []
    return "\n\n".join([*groups, *kept, *_describe_setup(setup)])


def _describe_steps(steps: Sequence[VerificationStep]) -> list[str]:
    """``steps`` under ``Steps:``, if there are any."""
    return ["Steps:\n" + _step_lines(steps)] if steps else []


def _describe_groups(
    steps: Sequence[VerificationStep], sets: Sequence[CheckSet], triggers: Mapping[str, Trigger]
) -> list[str]:
    """The task checks ``steps``, then each of ``sets``, each under a line saying when
    ``triggers`` run it, and for a set, whether it holds merges."""
    task_checks = _group("Task checks", ["every task", *_runs(triggers, TASK_CHECKS)], steps)
    return [task_checks, *(_describe_set(check_set, triggers) for check_set in sets)]


def _describe_set(check_set: CheckSet, triggers: Mapping[str, Trigger]) -> str:
    """``check_set``'s steps under a line saying when ``triggers`` run it and whether it holds
    merges, then each of ``triggers`` reporting it to a lens, on a line of its own."""
    runs = _runs(triggers, check_set.name) or ["no trigger"]
    effect = _EFFECT_PHRASES[check_set.effect]
    group = _group(f"Set {check_set.name}", [*runs, effect], check_set.steps)
    return "\n".join([group, *_reports(triggers, check_set.name)])


def _describe_kept(sets: Sequence[CheckSet], triggers: Mapping[str, Trigger]) -> list[str]:
    """Each set that ``triggers`` report but none of ``sets`` is, in order, under a line saying
    it's kept, with the line of each of ``triggers`` reporting it."""
    named = {check_set.name for check_set in sets}
    kept = [name for name in _reported_sets(triggers) if name not in named]
    return ["\n".join([f"Set {name}: kept", *_reports(triggers, name)]) for name in kept]


def _reported_sets(triggers: Mapping[str, Trigger]) -> list[str]:
    """The sets ``triggers`` report to a lens, each once, in order."""
    named = (name for trigger in triggers.values() for name in _values(trigger, "set"))
    return list(dict.fromkeys(name for name in named if _reports_on_any(triggers, name)))


def _reports_on_any(triggers: Mapping[str, Trigger], name: str) -> bool:
    """Whether any of ``triggers`` reports set ``name`` to a lens."""
    return any(_reports_on(trigger, name) for trigger in triggers.values())


def _group(title: str, notes: Sequence[str], steps: Sequence[VerificationStep]) -> str:
    """``steps``, or ``none``, under ``title`` and its ``notes``."""
    return f"{title}: {', '.join(notes)}\n" + (_step_lines(steps) or "none")


def _runs(triggers: Mapping[str, Trigger], checks: str) -> list[str]:
    """When each of ``triggers`` that runs ``checks`` fires, in order, in words."""
    return [describe_firing(trigger) for trigger in triggers.values() if trigger.checks == checks]


def _reports(triggers: Mapping[str, Trigger], name: str) -> list[str]:
    """The line of each of ``triggers`` reporting set ``name`` to a lens, in order, as in
    ``Report mutation-report: mutation → tests-reviewer when it fails``."""
    return [
        _report_line(trigger_name, trigger, name)
        for trigger_name, trigger in triggers.items()
        if _reports_on(trigger, name)
    ]


def _reports_on(trigger: Trigger, name: str) -> bool:
    """Whether ``trigger`` queues a role's task when a run of set ``name`` finishes."""
    return trigger.on == SET_FINISHED and bool(trigger.roles) and name in _values(trigger, "set")


def _report_line(trigger_name: str, trigger: Trigger, name: str) -> str:
    """``trigger``'s line, named ``trigger_name``, reporting set ``name`` to its roles, and when,
    and that it's off if it isn't enabled."""
    phrase = _REPORT_PHRASES.get(_values(trigger, "status"), "when it finishes")
    off = "" if trigger.enabled else " (off)"
    return f"Report {trigger_name}: {name} → {', '.join(trigger.roles)} {phrase}{off}"


def _values(trigger: Trigger, field: str) -> tuple[str, ...]:
    """The values ``trigger``'s ``when`` field ``field`` takes, if it has one."""
    value = trigger.when.get(field, ())
    return (value,) if isinstance(value, str) else value


def _step_lines(steps: Sequence[VerificationStep]) -> str:
    """Each of ``steps``, with its command, on a line of its own."""
    return "\n".join(f"- {step.name}: {shlex.join(step.command)}" for step in steps)


def _describe_setup(setup: Setup | None) -> list[str]:
    """``setup``'s command and the files that run it again, if there is one."""
    if setup is None:
        return []
    watched = f" (again when {', '.join(setup.watch)} changes)" if setup.watch else ""
    return [f"Setup: {shlex.join(setup.command)}{watched}"]


def configure_prompt(
    steps: Sequence[VerificationStep],
    setup: Setup | None,
    sets: Sequence[CheckSet] = (),
    triggers: Mapping[str, Trigger] = _NO_TRIGGERS,
) -> str:
    """The task for a configurator asked for from the frontend, told what's configured now (see
    :func:`configured_checks`)."""
    prompt = (
        "Set up this project's verification steps: work out the checks it uses and propose"
        " them with propose_checks."
    )
    return f"{prompt} {configured_checks(steps, setup, sets, triggers)}"


def configured_checks(
    steps: Sequence[VerificationStep],
    setup: Setup | None,
    sets: Sequence[CheckSet] = (),
    triggers: Mapping[str, Trigger] = _NO_TRIGGERS,
) -> str:
    """What a configurator is told is configured now, the task checks ``steps``, the named
    ``sets`` with when ``triggers`` run them and those reporting them to lenses, and ``setup``
    (see :func:`describe_checks`), or that nothing is: the ``{checks}`` a trigger's template
    uses."""
    current = describe_checks(steps, setup, sets, triggers)
    if not current:
        return "Nothing is configured yet."
    return "Configured now (build on these):\n\n" + current


def find_kept(
    configured: Sequence[VerificationStep], proposal: ChecksProposal
) -> tuple[VerificationStep, ...]:
    """The ``configured`` steps, a file's, that stay as they are once ``proposal`` is written,
    being in a set none of its steps is in, in order."""
    proposed_sets = {None, *proposal.set_names}
    return tuple(step for step in configured if step.set not in proposed_sets)


def find_kept_sets(
    configured: Sequence[VerificationStep], proposal: ChecksProposal
) -> tuple[SetName, ...]:
    """The names of the sets the ``configured`` steps ``proposal`` keeps are in (see
    :func:`find_kept`), each once, in the order they first appear."""
    kept = find_kept(configured, proposal)
    return tuple(dict.fromkeys(step.set for step in kept if step.set is not None))


def find_clashes(
    configured: Sequence[VerificationStep], proposal: ChecksProposal
) -> tuple[VerificationStep, ...]:
    """The ``configured`` steps, a file's, that stay as they are once ``proposal`` is written
    (see :func:`find_kept`) and share a name with one of its steps: those the file couldn't load
    beside it."""
    proposed_names = {step.name for step in proposal.steps}
    return tuple(step for step in find_kept(configured, proposal) if step.name in proposed_names)


def check_clashes(configured: Sequence[VerificationStep], proposal: ChecksProposal) -> None:
    """Raise :class:`ConfigError` naming each of the ``configured`` steps ``proposal`` clashes
    with (see :func:`find_clashes`), and its set, on a line of its own, if any."""
    problems = [
        f'step "{step.name}" has the same name as a step in set {step.set}, which stays as'
        " configured since no proposed step is in that set; give it another name."
        for step in find_clashes(configured, proposal)
    ]
    if problems:
        raise ConfigError("\n".join(problems))


def parse_proposal(
    data: dict[str, Any],
    built_in: Mapping[str, Trigger] = _NO_TRIGGERS,
    configured: Sequence[VerificationStep] = (),
    roles: Mapping[str, Role] = _NO_ROLES,
) -> ChecksProposal:
    """The proposal ``data`` (an agent's arguments) describes; raises :class:`ConfigError` if
    it's invalid, as the config would be once it's written over the ``built_in`` triggers and
    the ``configured`` steps: a set's table for a set no step is in, a step named like one of
    the ``configured`` steps it leaves as they are (see :func:`find_clashes`), a trigger named
    like a built-in one with a role, or a trigger, merged over the built-in one of its name,
    whose ``checks`` name neither the task checks nor a set a step is in, among the rest. Its
    triggers, reports and sets' tables may name a set only the ``configured`` steps it keeps are
    in (see :func:`find_kept_sets`), as the file keeps that set. A report is refused when it's
    named like a built-in or proposed trigger, its role isn't one of the enabled ``roles``
    granted ``report_finding``, its set is neither proposed nor kept, or its prompt uses a
    variable ``set_finished`` doesn't give."""
    try:
        proposal = ChecksProposal.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(validation_problems(exc)) from None
    kept = find_kept_sets(configured, proposal)
    sets = (*proposal.set_names, *kept)
    check_sets(proposal, kept)
    check_clashes(configured, proposal)
    check_free_names(built_in, proposal.triggers)
    check_triggers(merge_proposed(built_in, proposal.triggers), (), sets)
    targets = ReportTargets(finding_roles(roles), sets, _taken(proposal, built_in))
    check_reports(proposal.reports, targets)
    return proposal


def _taken(proposal: ChecksProposal, built_in: Mapping[str, Trigger]) -> dict[str, str]:
    """The trigger names a report of ``proposal``'s can't take, each with whose it is: the
    ``built_in`` triggers' and ``proposal``'s own triggers'."""
    return dict.fromkeys(built_in, "a built-in trigger") | dict.fromkeys(
        proposal.triggers, "a proposed trigger"
    )
