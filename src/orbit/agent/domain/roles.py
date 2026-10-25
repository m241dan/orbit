"""Roles agents work in: built-in defaults a project file can change, and the tasks orbit
queues in them.

The built-in roles aren't kept here: whoever builds them passes in the contents of their
``roles.toml`` and the instructions it names.

A role says what an agent is told about its job, which of orbit's tools it may call, whether its
claims of being done are verified, and how long it may take. Its model and tool access are the
defaults for tasks orbit queues in that role; a task the user queues keeps its own settings.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from orbit.agent.domain.config_errors import ConfigError, validation_problems
from orbit.protocol import (
    AGENT_FAILED,
    BUILT_IN_ROLE_NAMES,
    CODER,
    CONFIGURATOR,
    TIMED_OUT,
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

READ_ONLY_TOOLS = ("Read", "Glob", "Grep", "WebSearch")
"""What a restricted agent may use besides orbit's tools: reading and searching its tree and the
web, never running code or writing files. WebFetch is read-only too, but restricting the CLI
disables it unless the built-in tool set is replaced to name it."""

WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")
"""The tools a restricted agent is denied, which write files."""

CODER_ROLES = frozenset({CODER, CONFIGURATOR})
"""The roles whose tasks take one of the coders' slots a control plane shares across its
workspaces."""


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


@dataclass(frozen=True)
class ToolRules:
    """The tools an agent may use, and those it's denied, by the names agents see them by."""

    allowed: tuple[str, ...]
    denied: tuple[str, ...]


def orbit_tool_name(tool: str) -> str:
    """The name an agent sees orbit's ``tool`` by."""
    return f"mcp__orbit__{tool}"


def tool_rules(tool_access: ToolAccess, tools: Sequence[str]) -> ToolRules:
    """The tools an agent with ``tool_access`` may use: orbit's ``tools``, and with restricted
    access the read-only ones; and those it's denied: orbit's others, and with restricted access
    the ones that write files."""
    allowed = tuple(orbit_tool_name(tool) for tool in tools)
    denied = tuple(orbit_tool_name(tool) for tool in ORBIT_TOOLS if tool not in tools)
    if tool_access == "restricted":
        return ToolRules(allowed + READ_ONLY_TOOLS, denied + WRITE_TOOLS)
    return ToolRules(allowed, denied)


def out_of_time(who: str, minutes: float) -> str:
    """The error saying ``who`` ran out of their role's ``minutes``."""
    return f"{who} ran out of time ({minutes:g} minutes)."


def timeout_ending(
    expired: bool, minutes: float | None, who: str, exc: TimeoutError
) -> tuple[str, str]:
    """The error code and message for ``who``'s work that raised ``exc``: they ran out of their
    role's ``minutes`` only if its deadline ``expired``, as the work can raise a TimeoutError of
    its own."""
    if minutes is None or not expired:
        return AGENT_FAILED, str(exc)
    return TIMED_OUT, out_of_time(who, minutes)


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


class _BuiltInRoles(_Config):
    roles: dict[str, Role]


def load_built_in_roles(data: dict[str, Any]) -> dict[str, Role]:
    """The built-in roles ``data`` (``roles.toml``'s contents) describes, in order, each told the
    name of its Markdown file.

    Raises :class:`ConfigError`, naming the role and field, if a role is invalid, or naming the
    roles, if any built-in ones have no table or a table isn't for a built-in role.
    """
    try:
        roles = _BuiltInRoles.model_validate(data).roles
    except ValidationError as exc:
        raise ConfigError(validation_problems(exc)) from None
    if missing := set(BUILT_IN_ROLE_NAMES) - set(roles):
        raise ConfigError(f"roles: no table for the built-in roles {sorted(missing)}")
    if unknown := set(roles) - set(BUILT_IN_ROLE_NAMES):
        raise ConfigError(f"roles: {sorted(unknown)} aren't built-in roles")
    return {name: roles[name] for name in BUILT_IN_ROLE_NAMES}


def built_in_roles(roles: Mapping[str, Role], instructions: Mapping[str, str]) -> dict[str, Role]:
    """The built-in ``roles``, each told ``instructions[its name]`` instead.

    Raises :class:`ConfigError`, naming them, if any have no instructions.
    """
    missing = [name for name in BUILT_IN_ROLE_NAMES if not instructions.get(name, "").strip()]
    if missing:
        raise ConfigError(f"no instructions for the built-in roles {missing}")
    return {
        name: roles[name].model_copy(update={"instructions": instructions[name]})
        for name in BUILT_IN_ROLE_NAMES
    }
