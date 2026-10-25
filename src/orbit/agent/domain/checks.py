"""The checks a project is verified and set up with: its verification steps, the setup that
makes a fresh worktree ready, and the checks an agent proposes."""

import shlex
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from orbit.agent.domain.config_errors import ConfigError, validation_problems
from orbit.agent.domain.verification import VerificationStep


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class VerificationConfig(_Config):
    """The steps that verify a task, run in order; with none, nothing is verified."""

    steps: tuple[VerificationStep, ...] = Field(
        default=(),
        description="The steps that verify a task, run in order; with none, nothing is verified.",
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


def setup_step(setup: Setup) -> VerificationStep:
    """``setup``'s command, as a step run before the others."""
    return VerificationStep("setup", setup.command)


class ChecksProposal(VerificationConfig):
    """Verification steps, at least one, and a setup, as an agent proposes them."""

    steps: tuple[VerificationStep, ...] = Field(min_length=1)
    setup: Setup | None = None


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
    current = describe_checks(steps, setup)
    if not current:
        return prompt + " Nothing is configured yet."
    return prompt + " Configured now (build on these):\n\n" + current


def parse_proposal(data: dict[str, Any]) -> ChecksProposal:
    """The proposal ``data`` (an agent's arguments) describes; raises :class:`ConfigError` if
    it's invalid."""
    try:
        return ChecksProposal.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(validation_problems(exc)) from None
