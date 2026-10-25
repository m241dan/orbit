"""Roles agents work in: built-in defaults a project file can change, and the tasks orbit
queues in them.

The built-in roles aren't kept here: whoever builds them passes in the contents of their
``roles.toml`` and the instructions it names.

A role says what an agent is told about its job, which of orbit's tools it may call, whether its
claims of being done are verified, and how long it may take. Its model, tool access and effort
are the defaults for tasks orbit queues in that role; a task the user queues keeps its own
settings, but one without an effort runs with its role's. It also says which lane runs
it, where it works, whether you talk to it, whether its work merges and whether it's enabled,
which are checked here. The enabled task roles' lanes are the session's lanes, the one whose
roles merge running on the user's asking; the enabled conversational roles each have a
conversation of their own; where a role works decides whether its tasks take a coder's slot; and
the work asked of a disabled role is refused.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from orbit.agent.domain.config_errors import ConfigError, validation_problems
from orbit.agent.domain.refusals import Refused
from orbit.protocol import (
    AGENT_FAILED,
    BUILT_IN_ROLE_NAMES,
    CODER,
    NOT_CONFIGURED,
    TIMED_OUT,
    WORK_LANE,
    Effort,
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

Workspace = Literal["worktree", "snapshot", "checkout", "none"]
"""Where an agent in a role works: the coder's separate worktree, which can merge; a read-only
snapshot of the base commit; the user's checkout, read in place; or an empty temporary
directory."""

LANE_NAME = re.compile(r"[A-Za-z0-9_-]+")
"""What a lane's name is made of."""


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _can_merge(fields: dict[str, Any]) -> bool:
    """Whether a role with the validated ``fields`` can merge its work: it's verified, in a
    worktree. A role that leaves ``merge`` out merges if it can, so an unverified one still
    loads; a verified one that can't is refused, as its checked work would vanish."""
    verify: bool = fields["verify"]
    return verify and fields["workspace"] == "worktree"


class Role(_Config):
    instructions: str = Field(min_length=1)
    model: str | None = None
    tool_access: ToolAccess = "restricted"
    tools: tuple[str, ...] = (GET_TASK, COMPLETE_TASK, REPORT_PROGRESS, GIVE_UP)
    verify: bool = True
    timeout_minutes: float | None = Field(default=None, gt=0)
    lane: str = "work"
    workspace: Workspace = "worktree"
    conversation: bool = False
    merge: bool = Field(default_factory=_can_merge)
    enabled: bool = True
    effort: Effort | None = None

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

    @field_validator("lane")
    @classmethod
    def _plain_lane(cls, lane: str) -> str:
        if not LANE_NAME.fullmatch(lane):
            raise ValueError("a lane is a plain name: letters, digits, - and _")
        return lane

    @model_validator(mode="after")
    def _fields_agree(self) -> Self:
        problems = (
            _merge_problems(self)
            + _verify_problems(self)
            + _checkout_problems(self)
            + _conversation_problems(self)
        )
        if problems:
            raise PydanticCustomError("role_rules", "; ".join(problems))
        return self

    @property
    def takes_coder_slot(self) -> bool:
        """Whether a task in this role takes one of the coders' slots a control plane shares
        across its workspaces: it works in the coder's worktree."""
        return self.workspace == "worktree"

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
        return Settings(model=self.model, tool_access=self.tool_access, effort=self.effort)

    def launch_settings(self, settings: Settings) -> Settings:
        """``settings`` as an agent in this role runs with them: with the role's effort when they
        set none."""
        return settings.model_copy(update={"effort": settings.effort or self.effort})


def _unmet(rule: str, needs: Mapping[str, bool]) -> list[str]:
    """``rule`` followed by each of ``needs`` that isn't met."""
    return [f"{rule} {need}" for need, met in needs.items() if not met]


def _merge_problems(role: Role) -> list[str]:
    """What a merging ``role`` lacks to merge: a worktree to merge from, and verified work."""
    if not role.merge:
        return []
    needs = {'workspace = "worktree"': role.workspace == "worktree", "verify = true": role.verify}
    return _unmet("merge = true needs", needs)


def _verify_problems(role: Role) -> list[str]:
    """Why ``role``'s verified work would go nowhere, if it would: work checked but not merged
    vanishes once it's put aside."""
    if role.verify and not role.merge:
        return ["verify = true needs merge = true"]
    return []


def _checkout_problems(role: Role) -> list[str]:
    """Why ``role`` can't work in the user's checkout, if it can't: with full tool access it
    could edit the user's tree."""
    if role.workspace == "checkout" and role.tool_access == "full":
        return ['workspace = "checkout" can\'t have tool_access = "full"']
    return []


def _conversation_problems(role: Role) -> list[str]:
    """What a conversational ``role`` breaks: it plans rather than taking tasks, and leaves the
    worktree and merging to the roles that do."""
    if not role.conversation:
        return []
    needs = {
        "propose_task in its tools": PROPOSE_TASK in role.tools,
        "get_task left out of its tools": GET_TASK not in role.tools,
        "merge = false": not role.merge,
        "verify = false": not role.verify,
        'a workspace other than "worktree"': role.workspace != "worktree",
    }
    return _unmet("conversation = true needs", needs)


def lane_clashes(roles: Mapping[str, Role]) -> list[str]:
    """Each conversational role among ``roles`` whose lane a task role shares, naming them: a
    task in that lane would wait on the conversation, or the conversation on it."""
    clashes = []
    for name, role in roles.items():
        shared = _task_roles_in(roles, role.lane) if role.conversation else []
        if shared:
            clashes.append(
                f"the conversational role {name!r} can't share its lane {role.lane!r} with the"
                f" task roles {shared}"
            )
    return clashes


def worktree_lane_clashes(roles: Mapping[str, Role]) -> list[str]:
    """Why the enabled roles among ``roles`` that work in the worktree can't, if they're in more
    than one lane, naming them by lane: there's one worktree, and lanes run beside each other."""
    lanes: dict[str, list[str]] = {}
    for name, role in roles.items():
        if role.enabled and role.workspace == "worktree":
            lanes.setdefault(role.lane, []).append(name)
    if len(lanes) < 2:
        return []
    named = " and ".join(f"lane {lane!r} has {names}" for lane, names in lanes.items())
    return [f'the enabled roles with workspace = "worktree" must share one lane, but {named}']


def role_clashes(roles: Mapping[str, Role]) -> list[str]:
    """How ``roles`` break the rules across roles: the conversational roles whose lanes task
    roles share (see :func:`lane_clashes`), and the worktree's roles in more than one lane (see
    :func:`worktree_lane_clashes`)."""
    return lane_clashes(roles) + worktree_lane_clashes(roles)


def task_lanes(roles: Mapping[str, Role]) -> dict[str, frozenset[str]]:
    """The lanes the enabled task roles among ``roles`` run in, in order, each with the names of
    those roles: the roles that aren't conversational."""
    lanes: dict[str, set[str]] = {}
    for name, role in roles.items():
        if role.enabled and not role.conversation:
            lanes.setdefault(role.lane, set()).add(name)
    return {lane: frozenset(names) for lane, names in lanes.items()}


def conversational_roles(roles: Mapping[str, Role]) -> tuple[str, ...]:
    """The names of the enabled conversational roles among ``roles``, in order: those the user
    talks to, each with a conversation of its own."""
    return tuple(name for name, role in roles.items() if role.enabled and role.conversation)


def merging_lane(roles: Mapping[str, Role]) -> str:
    """The lane of the enabled roles among ``roles`` whose work merges, which runs only on the
    user's asking: there's one at most, as only roles in the worktree merge and they share a
    lane. It's the work lane if none merges."""
    return next((role.lane for role in roles.values() if role.enabled and role.merge), WORK_LANE)


def disabled_roles(roles: Mapping[str, Role]) -> frozenset[str]:
    """The names of the roles among ``roles`` that are disabled, whose work is refused."""
    return frozenset(name for name, role in roles.items() if not role.enabled)


def disabled_note(name: str) -> str:
    """Why the work asked of role ``name`` is refused: it's disabled."""
    return f"{name} is disabled in this project's configuration."


def check_enabled(roles: Mapping[str, Role], name: str) -> None:
    """Refuse the work asked of role ``name`` if it's disabled among ``roles``; a role they
    don't have isn't.

    Raises :class:`Refused`, coded ``not_configured``, naming the role.
    """
    if name in disabled_roles(roles):
        raise Refused(NOT_CONFIGURED, disabled_note(name))


def _task_roles_in(roles: Mapping[str, Role], lane: str) -> list[str]:
    """The roles among ``roles`` in ``lane`` that aren't conversational."""
    return [name for name, role in roles.items() if not role.conversation and role.lane == lane]


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
