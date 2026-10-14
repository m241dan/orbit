"""Roles agents work in, and orbit's limits: built-in defaults a project file can change.

A role says what an agent is told about its job, which of orbit's tools it may call, whether its
claims of being done are verified, and how long it may take. Its model and tool access are the
defaults for tasks orbit queues in that role; a task the user queues keeps its own settings.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from orbit.protocol import ToolAccess

ORBIT_TOOLS = ("get_task", "complete_task", "run_check", "report_progress", "give_up")
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
        if "get_task" not in tools:
            raise ValueError("an agent needs get_task to fetch its task")
        return tools


BUILT_IN_ROLES: dict[str, Role] = {
    "coder": Role(
        instructions=(
            "You are orbit's coder. Make the change your task asks for in the working directory,"
            " keeping the project's checks passing. You may run a single check with run_check"
            " before claiming you're done."
        ),
        tool_access="full",
        tools=("get_task", "complete_task", "run_check", "report_progress", "give_up"),
    ),
    "reviewer": Role(
        instructions=(
            "You are orbit's reviewer. Review the change your task names: look for bugs, missing"
            " tests and unclear code. Don't edit files. Call complete_task with your findings."
        ),
        verify=False,
    ),
    "super-reviewer": Role(
        instructions=(
            "You are orbit's super reviewer. Review the whole codebase, not only recent changes:"
            " look for duplication, structure that no longer fits, and gaps in tests. Don't edit"
            " files. Call complete_task with your findings."
        ),
        model="opus",
        verify=False,
    ),
    "planner": Role(
        instructions=(
            "You are orbit's planner. Help break the user's goal into small tasks a coder can do"
            " one at a time, each with a clear way to tell it's done. Don't edit files."
        ),
        verify=False,
    ),
}


class OrbitConfig(_Config):
    """Orbit's limits and roles.

    ``fix_attempts``: fixes asked of one agent before a fresh one takes over.
    ``review_rounds``: rounds of review-suggested work after which review pauses.
    ``super_review_every``: commits between super reviews.
    ``review_suggestions`` / ``super_review_suggestions``: tasks one review may suggest.
    """

    fix_attempts: int = Field(default=5, ge=1)
    review_rounds: int = Field(default=3, ge=1)
    super_review_every: int = Field(default=10, ge=1)
    review_suggestions: int = Field(default=5, ge=1)
    super_review_suggestions: int = Field(default=10, ge=1)
    roles: dict[str, Role] = Field(default_factory=lambda: dict(BUILT_IN_ROLES))


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
