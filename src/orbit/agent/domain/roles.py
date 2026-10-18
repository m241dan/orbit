"""Roles agents work in: built-in defaults a project file can change, and the tasks orbit
queues in them.

The built-in roles' instructions aren't kept here: whoever builds the roles passes them in.

A role says what an agent is told about its job, which of orbit's tools it may call, whether its
claims of being done are verified, and how long it may take. Its model and tool access are the
defaults for tasks orbit queues in that role; a task the user queues keeps its own settings.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orbit.agent.domain.config_errors import ConfigError
from orbit.protocol import (
    BUILT_IN_ROLE_NAMES,
    CODER,
    CONFIGURATOR,
    PLANNER,
    REVIEWER,
    SUPER_REVIEWER,
    Settings,
    TaskSpec,
    ToolAccess,
)

GET_TASK = "get_task"
COMPLETE_TASK = "complete_task"
RUN_CHECK = "run_check"
REPORT_PROGRESS = "report_progress"
GIVE_UP = "give_up"
SUGGEST_TASK = "suggest_task"
PROPOSE_TASK = "propose_task"
PROPOSE_CHECKS = "propose_checks"

ORBIT_TOOLS = (
    GET_TASK,
    COMPLETE_TASK,
    RUN_CHECK,
    REPORT_PROGRESS,
    GIVE_UP,
    SUGGEST_TASK,
    PROPOSE_TASK,
    PROPOSE_CHECKS,
)
"""Every tool orbit's MCP server offers agents."""

REVIEW_ROLES = frozenset({REVIEWER, SUPER_REVIEWER})
"""The roles that review work rather than do it."""


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Role(_Config):
    instructions: str = Field(min_length=1)
    model: str | None = None
    tool_access: ToolAccess = "restricted"
    tools: tuple[str, ...] = (GET_TASK, COMPLETE_TASK, REPORT_PROGRESS, GIVE_UP)
    verify: bool = True
    timeout_minutes: float | None = Field(default=None, gt=0)

    @field_validator("tools")
    @classmethod
    def _known_tools(cls, tools: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [tool for tool in tools if tool not in ORBIT_TOOLS]
        if unknown:
            raise ValueError(f"unknown tools {unknown}; orbit's tools are {list(ORBIT_TOOLS)}")
        if GET_TASK not in tools and PROPOSE_TASK not in tools:
            raise ValueError(
                "an agent needs get_task to fetch its task, or propose_task to plan in conversation"
            )
        return tools

    @property
    def runs_checks(self) -> bool:
        """Whether an agent in this role can run verification steps itself."""
        return RUN_CHECK in self.tools

    @property
    def proposes_checks(self) -> bool:
        """Whether an agent in this role proposes verification steps rather than being
        verified."""
        return PROPOSE_CHECKS in self.tools

    @property
    def timeout_seconds(self) -> float | None:
        """How long an agent in this role may take, in seconds; none for no limit."""
        return None if self.timeout_minutes is None else self.timeout_minutes * 60

    @property
    def settings(self) -> Settings:
        """The settings of a task orbit queues in this role."""
        return Settings(model=self.model, tool_access=self.tool_access)


def out_of_time(who: str, minutes: float) -> str:
    """The error saying ``who`` ran out of their role's ``minutes``."""
    return f"{who} ran out of time ({minutes:g} minutes)."


def role_task(role_name: str, role: Role, prompt: str) -> TaskSpec:
    """A task orbit queues for ``role`` (named ``role_name``), with its settings."""
    return TaskSpec(prompt=prompt, settings=role.settings, role=role_name)


def suggested_task(coder: Role, title: str, prompt: str) -> TaskSpec:
    """A coder's task an agent suggested or proposed, as ``title`` and ``prompt``."""
    title, prompt = title.strip(), prompt.strip()
    return role_task(CODER, coder, f"{title}\n\n{prompt}" if prompt else title)


def task_suggestion(arguments: Mapping[str, Any]) -> tuple[str, str] | str:
    """The title, on one line, and prompt of the task an agent suggested or proposed with
    ``arguments``; or why it's refused."""
    title = " ".join(str(arguments.get("title", "")).split())
    if not title:
        return "A task needs a title."
    return title, str(arguments.get("prompt", "")).strip()


_BUILT_IN_FIELDS: dict[str, dict[str, Any]] = {
    CODER: {
        "tool_access": "full",
        "tools": (GET_TASK, COMPLETE_TASK, RUN_CHECK, REPORT_PROGRESS, GIVE_UP),
    },
    REVIEWER: {
        "tools": (GET_TASK, COMPLETE_TASK, REPORT_PROGRESS, GIVE_UP, SUGGEST_TASK),
        "verify": False,
    },
    SUPER_REVIEWER: {
        "model": "opus",
        "tools": (GET_TASK, COMPLETE_TASK, REPORT_PROGRESS, GIVE_UP, SUGGEST_TASK),
        "verify": False,
    },
    PLANNER: {
        "tools": (PROPOSE_TASK, REPORT_PROGRESS),
        "verify": False,
    },
    CONFIGURATOR: {
        "tools": (GET_TASK, PROPOSE_CHECKS, REPORT_PROGRESS, GIVE_UP),
        "verify": False,
    },
}
"""Each built-in role's fields, all but its instructions."""


def built_in_roles(instructions: Mapping[str, str]) -> dict[str, Role]:
    """The built-in roles, each told ``instructions[its name]``.

    Raises :class:`ConfigError`, naming them, if any have no instructions.
    """
    missing = [name for name in BUILT_IN_ROLE_NAMES if not instructions.get(name, "").strip()]
    if missing:
        raise ConfigError(f"no instructions for the built-in roles {missing}")
    return {
        name: Role(instructions=instructions[name], **_BUILT_IN_FIELDS[name])
        for name in BUILT_IN_ROLE_NAMES
    }
