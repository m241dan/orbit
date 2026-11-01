"""The checks a project is verified and set up with: its verification steps, the task checks
among them and the named sets the others are in, the setup that makes a fresh worktree ready, and
the checks an agent proposes, with the triggers that would run them."""

import shlex
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from orbit.agent.domain.config_errors import ConfigError, validation_problems
from orbit.agent.domain.triggers import ChecksTrigger, Trigger, TriggerName, check_triggers
from orbit.agent.domain.verification import CheckSet, SetEffect, SetName, VerificationStep


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


def check_sets(verification: VerificationConfig) -> None:
    """Raise :class:`ConfigError` naming each of ``verification``'s sets' tables for a set no
    step is in, on a line of its own, if any, each at ``verification.sets.<name>``."""
    problems = [
        f'verification.sets.{name}: no step is in it; a step joins it with set = "{name}".'
        for name in verification.sets
        if name not in verification.set_names
    ]
    if problems:
        raise ConfigError("\n".join(problems))


def setup_step(setup: Setup) -> VerificationStep:
    """``setup``'s command, as a step run before the others."""
    return VerificationStep("setup", setup.command)


class ChecksProposal(VerificationConfig):
    """Verification steps, at least one, a setup, the named sets' tables, each given as a table
    or as whether it holds merges, and the ``triggers`` that run the checks, by name, as an agent
    proposes them."""

    steps: tuple[VerificationStep, ...] = Field(min_length=1)
    setup: Setup | None = None
    triggers: dict[TriggerName, ChecksTrigger] = Field(default_factory=dict)

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
    def trigger_settings(self) -> dict[str, Trigger]:
        """The ``triggers``, by name, as the config would read them."""
        return {name: trigger.as_trigger() for name, trigger in self.triggers.items()}


def describe_checks(steps: Sequence[VerificationStep], setup: Setup | None) -> str:
    """``steps``, one per line, then ``setup``; each part only if there is one."""
    parts = []
    if steps:
        lines = [f"- {step.name}: {shlex.join(step.command)}" for step in steps]
        parts.append("Steps:\n" + "\n".join(lines))
    if setup is not None:
        line = f"Setup: {shlex.join(setup.command)}"
        if setup.watch:
            line += f" (again when {', '.join(setup.watch)} changes)"
        parts.append(line)
    return "\n\n".join(parts)


def configure_prompt(steps: Sequence[VerificationStep], setup: Setup | None) -> str:
    """The task for a configurator asked for from the frontend, told what's configured now."""
    prompt = (
        "Set up this project's verification steps: work out the checks it uses and propose"
        " them with propose_checks."
    )
    return f"{prompt} {configured_checks(steps, setup)}"


def configured_checks(steps: Sequence[VerificationStep], setup: Setup | None) -> str:
    """What a configurator is told is configured now, the ``steps`` and ``setup`` (see
    :func:`describe_checks`), or that nothing is: the ``{checks}`` a trigger's template uses."""
    current = describe_checks(steps, setup)
    if not current:
        return "Nothing is configured yet."
    return "Configured now (build on these):\n\n" + current


def parse_proposal(data: dict[str, Any]) -> ChecksProposal:
    """The proposal ``data`` (an agent's arguments) describes; raises :class:`ConfigError` if
    it's invalid, as the config would be: a set's table for a set no step is in, or a trigger
    whose ``checks`` name neither the task checks nor a set a step is in, among the rest."""
    try:
        proposal = ChecksProposal.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(validation_problems(exc)) from None
    check_sets(proposal)
    check_triggers(proposal.trigger_settings, (), proposal.set_names)
    return proposal
