"""Roles agents work in, orbit's limits, its verification steps and worktree setup: built-in
defaults a project file can change.

A role says what an agent is told about its job, which of orbit's tools it may call, whether its
claims of being done are verified, and how long it may take. Its model and tool access are the
defaults for tasks orbit queues in that role; a task the user queues keeps its own settings.
"""

import shlex
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import ToolAccess

ORBIT_TOOLS = (
    "get_task",
    "complete_task",
    "run_check",
    "report_progress",
    "give_up",
    "suggest_task",
    "propose_task",
    "propose_checks",
)
"""Every tool orbit's MCP server offers agents."""


class ConfigError(ValueError):
    """The configuration is invalid; the message says where and why, on one line per problem."""


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Role(_Config):
    instructions: str = Field(min_length=1)
    model: str | None = None
    tool_access: ToolAccess = "restricted"
    tools: tuple[str, ...] = ("get_task", "complete_task", "report_progress", "give_up")
    verify: bool = True
    timeout_minutes: float | None = Field(default=None, gt=0)

    @field_validator("tools")
    @classmethod
    def _known_tools(cls, tools: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [tool for tool in tools if tool not in ORBIT_TOOLS]
        if unknown:
            raise ValueError(f"unknown tools {unknown}; orbit's tools are {list(ORBIT_TOOLS)}")
        if "get_task" not in tools and "propose_task" not in tools:
            raise ValueError(
                "an agent needs get_task to fetch its task, or propose_task to plan in conversation"
            )
        return tools


BUILT_IN_ROLES: dict[str, Role] = {
    "coder": Role(
        instructions=(
            "You are orbit's coder. Make the change your task asks for in the working directory,"
            " keeping the project's checks passing. You may run a single check with run_check"
            " before claiming you're done. Don't commit: orbit commits your work once it's"
            " verified."
        ),
        tool_access="full",
        tools=("get_task", "complete_task", "run_check", "report_progress", "give_up"),
    ),
    "reviewer": Role(
        instructions=(
            "You are orbit's reviewer. Review the change your task names: look for bugs, missing"
            " tests and unclear code. Don't edit files. Suggest fixes worth making with"
            " suggest_task, then call complete_task with your findings."
        ),
        tools=("get_task", "complete_task", "report_progress", "give_up", "suggest_task"),
        verify=False,
    ),
    "super-reviewer": Role(
        instructions=(
            "You are orbit's super reviewer. Review the whole codebase, not only recent changes:"
            " look for duplication, structure that no longer fits, and gaps in tests. Don't edit"
            " files. Suggest improvements worth making with suggest_task, then call complete_task"
            " with your findings."
        ),
        model="opus",
        tools=("get_task", "complete_task", "report_progress", "give_up", "suggest_task"),
        verify=False,
    ),
    "planner": Role(
        instructions=(
            "You are orbit's planner, in conversation with the user. Help them break their goal"
            " into small tasks a coder can do one at a time, in order, each with a clear way to"
            " tell it's done. Propose each with propose_task (a short title, and a prompt saying"
            " what to change and how to check it); they appear as drafts the user accepts or"
            " drops. Ask when something is unclear. Don't edit files."
        ),
        tools=("propose_task", "report_progress"),
        verify=False,
    ),
    "configurator": Role(
        instructions=(
            "You are orbit's configurator. Work out how this project checks its code: inspect"
            " the repository (CI workflows, .pre-commit-config.yaml, package.json scripts,"
            " pyproject.toml, the Makefile and so on) to find the checks it already uses."
            " Propose them with propose_checks, quick ones first, plus a setup command that"
            " makes a fresh checkout ready and the files whose changes should rerun it. Orbit"
            " runs your proposal, then shows it to the user. Don't edit files. When orbit"
            " reports a failure or the user asks for changes, revise and propose again."
        ),
        tools=("get_task", "propose_checks", "report_progress", "give_up"),
        verify=False,
    ),
}


class VerificationConfig(_Config):
    """The steps that verify a task, run in order; with none, nothing is verified."""

    steps: tuple[VerificationStep, ...] = ()

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

    command: tuple[str, ...] = Field(min_length=1)
    watch: tuple[str, ...] = ()

    @field_validator("watch")
    @classmethod
    def _relative_paths(cls, watch: tuple[str, ...]) -> tuple[str, ...]:
        for path in watch:
            if not path or path.startswith("/") or ".." in path.split("/"):
                raise ValueError(f"{path!r} isn't a relative path inside the project")
        return watch


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
        raise ConfigError(_describe(exc)) from None


class OrbitConfig(_Config):
    """Orbit's limits and roles.

    ``fix_attempts``: fixes asked of one agent before a fresh one takes over.
    ``review_rounds``: rounds of review-suggested work after which review pauses.
    ``super_review_every``: commits between super reviews.
    ``review_suggestions`` / ``super_review_suggestions``: tasks one review may suggest.
    ``verification``: how tasks are verified.
    ``setup``: how the coder's worktree is set up; ``None`` sets nothing up.
    """

    fix_attempts: int = Field(default=5, ge=1)
    review_rounds: int = Field(default=3, ge=1)
    super_review_every: int = Field(default=10, ge=1)
    review_suggestions: int = Field(default=5, ge=1)
    super_review_suggestions: int = Field(default=10, ge=1)
    roles: dict[str, Role] = Field(default_factory=lambda: dict(BUILT_IN_ROLES))
    verification: VerificationConfig = VerificationConfig()
    setup: Setup | None = None


def load_config(data: dict[str, Any]) -> OrbitConfig:
    """The configuration ``data`` (a project file's contents) describes over the defaults.

    A role it names that's built in keeps the fields it doesn't set; any other role is new.
    Raises :class:`ConfigError` if it's invalid.
    """
    data = dict(data)
    if isinstance(data.get("roles"), dict):
        data["roles"] = _with_built_in_roles(data["roles"])
    try:
        return OrbitConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_describe(exc)) from None


def _with_built_in_roles(roles: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {name: role.model_dump() for name, role in BUILT_IN_ROLES.items()}
    for name, fields in roles.items():
        base = merged.get(name, {})
        merged[name] = {**base, **fields} if isinstance(fields, dict) else fields
    return merged


def _describe(error: ValidationError) -> str:
    problems = []
    for problem in error.errors():
        where = ".".join(str(part) for part in problem["loc"])
        problems.append(f"{where}: {problem['msg']}")
    return "\n".join(problems)
