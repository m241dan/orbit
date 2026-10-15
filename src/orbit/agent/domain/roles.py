"""Roles agents work in, orbit's limits, its verification steps and worktree setup: built-in
defaults a project file can change.

The built-in roles' instructions aren't kept here: whoever builds the roles passes them in.

A role says what an agent is told about its job, which of orbit's tools it may call, whether its
claims of being done are verified, and how long it may take. Its model and tool access are the
defaults for tasks orbit queues in that role; a task the user queues keeps its own settings.
"""

import shlex
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.reviews import ReviewLimits
from orbit.agent.domain.verification import MAX_FIX_ATTEMPTS, VerificationStep
from orbit.protocol import Settings, TaskSpec, ToolAccess

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

CODER = "coder"
REVIEWER = "reviewer"
SUPER_REVIEWER = "super-reviewer"
PLANNER = "planner"
CONFIGURATOR = "configurator"
"""The built-in roles' names."""

REVIEW_ROLES = frozenset({REVIEWER, SUPER_REVIEWER})
"""The roles that review work rather than do it."""


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

    @property
    def settings(self) -> Settings:
        """The settings of a task orbit queues in this role."""
        return Settings(model=self.model, tool_access=self.tool_access)


def role_task(role_name: str, role: Role, prompt: str) -> TaskSpec:
    """A task orbit queues for ``role`` (named ``role_name``), with its settings."""
    return TaskSpec(prompt=prompt, settings=role.settings, role=role_name)


def suggested_task(coder: Role, title: str, prompt: str) -> TaskSpec:
    """A coder's task an agent suggested or proposed, as ``title`` and ``prompt``."""
    return role_task(CODER, coder, f"{title.strip()}\n\n{prompt.strip()}")


_BUILT_IN_FIELDS: dict[str, dict[str, Any]] = {
    CODER: {
        "tool_access": "full",
        "tools": ("get_task", "complete_task", "run_check", "report_progress", "give_up"),
    },
    REVIEWER: {
        "tools": ("get_task", "complete_task", "report_progress", "give_up", "suggest_task"),
        "verify": False,
    },
    SUPER_REVIEWER: {
        "model": "opus",
        "tools": ("get_task", "complete_task", "report_progress", "give_up", "suggest_task"),
        "verify": False,
    },
    PLANNER: {
        "tools": ("propose_task", "report_progress"),
        "verify": False,
    },
    CONFIGURATOR: {
        "tools": ("get_task", "propose_checks", "report_progress", "give_up"),
        "verify": False,
    },
}
"""Each built-in role's fields, all but its instructions."""

BUILT_IN_ROLE_NAMES = tuple(_BUILT_IN_FIELDS)


def built_in_roles(instructions: Mapping[str, str]) -> dict[str, Role]:
    """The built-in roles, each told ``instructions[its name]``.

    Raises :class:`ConfigError`, naming them, if any have no instructions.
    """
    missing = [name for name in BUILT_IN_ROLE_NAMES if not instructions.get(name, "").strip()]
    if missing:
        raise ConfigError(f"no instructions for the built-in roles {missing}")
    return {
        name: Role(instructions=instructions[name], **fields)
        for name, fields in _BUILT_IN_FIELDS.items()
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
        raise ConfigError(_describe(exc)) from None


_LIMITS = Limits()
_REVIEW_LIMITS = ReviewLimits()


class OrbitConfig(_Config):
    """Orbit's limits and roles.

    ``fix_attempts``: in-session fixes, asked of the same agent before its task falls back.
    ``max_checks``: verification steps an agent may run itself before claiming done.
    ``fallback_fixes``: fresh-agent fix tasks one task's chain may queue for each failing step.
    ``review_rounds``: rounds of review-suggested work after which review pauses.
    ``super_review_every``: commits between super reviews.
    ``review_suggestions`` / ``super_review_suggestions``: tasks one review may suggest.
    ``verification``: how tasks are verified.
    ``setup``: how the coder's worktree is set up; ``None`` sets nothing up.
    """

    fix_attempts: int = Field(default=_LIMITS.max_attempts, ge=1)
    max_checks: int = Field(default=_LIMITS.max_checks, ge=1)
    fallback_fixes: int = Field(default=MAX_FIX_ATTEMPTS, ge=1)
    review_rounds: int = Field(default=_REVIEW_LIMITS.rounds, ge=1)
    super_review_every: int = Field(default=_REVIEW_LIMITS.super_every, ge=1)
    review_suggestions: int = Field(default=_REVIEW_LIMITS.suggestions, ge=1)
    super_review_suggestions: int = Field(default=_REVIEW_LIMITS.super_suggestions, ge=1)
    roles: dict[str, Role]
    verification: VerificationConfig = VerificationConfig()
    setup: Setup | None = None

    @property
    def limits(self) -> Limits:
        return Limits(max_attempts=self.fix_attempts, max_checks=self.max_checks)

    @property
    def review_limits(self) -> ReviewLimits:
        return ReviewLimits(
            rounds=self.review_rounds,
            suggestions=self.review_suggestions,
            super_every=self.super_review_every,
            super_suggestions=self.super_review_suggestions,
        )


def load_config(data: dict[str, Any], built_in: Mapping[str, Role]) -> OrbitConfig:
    """The configuration ``data`` (a project file's contents) describes over the defaults, whose
    roles are ``built_in``.

    A role it names that's built in keeps the fields it doesn't set; any other role is new.
    Raises :class:`ConfigError` if it's invalid.
    """
    data = dict(data)
    roles = data.get("roles", {})
    if isinstance(roles, dict):
        data["roles"] = _with_built_in_roles(roles, built_in)
    try:
        return OrbitConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_describe(exc)) from None


def _with_built_in_roles(roles: dict[str, Any], built_in: Mapping[str, Role]) -> dict[str, Any]:
    merged: dict[str, Any] = {name: role.model_dump() for name, role in built_in.items()}
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
