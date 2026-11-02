"""Roles agents work in: built-in defaults a project file can change, and the tasks orbit
queues in them.

The built-in roles aren't kept here: whoever builds them passes in the contents of their
``roles.toml`` and the instructions it names.

A role says what an agent is told about its job, the skills it gets, which of orbit's tools it
may call, whether its claims of being done are verified, how long it may take and the runtime it
runs on; whether the skills it names exist is checked once they're read. Its model,
tool access and effort are the defaults for tasks orbit queues in that role; a task the user
queues keeps its own settings, but one without an effort runs with its role's. It also says
which lane runs it, where it works, whether you talk to it, whether its work merges and whether
it's enabled, which are checked here role by role. Where a role works decides whether its tasks
take a coder's slot, and the work asked of a disabled role is refused.
"""

import re
from collections.abc import Mapping
from datetime import timedelta
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
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
from orbit.agent.domain.tools import (
    COMPLETE_TASK,
    CONSULT,
    DRAFT_TOOLS,
    GET_TASK,
    GIVE_UP,
    ORBIT_TOOLS,
    PROPOSE_CHECKS,
    PROPOSE_TASK,
    READ_SKILL,
    REPORT_PROGRESS,
    RUN_CHECK,
)
from orbit.protocol import (
    BUILT_IN_ROLE_NAMES,
    DEFAULT_RUNTIME,
    NOT_CONFIGURED,
    Effort,
    Settings,
    TaskSpec,
    ToolAccess,
)

Workspace = Literal["worktree", "snapshot", "checkout", "none", "copy"]
"""Where an agent in a role works: the coder's separate worktree, which can merge; a read-only
snapshot of the base commit; the user's checkout, read in place; an empty temporary directory;
or a writable copy of the base commit, set up, which never merges."""

DEFAULT_COPY_TTL_HOURS = 24.0
"""How long a role's copy is kept since its conversation's last turn, in hours, unless the role
says otherwise."""

MAX_COPY_TTL_HOURS = float(timedelta.max // timedelta(hours=1))
"""The longest copy TTL a role may set, in hours: the whole hours a timedelta can hold."""

RETIRED_ROLES: Mapping[str, str] = {
    "super-reviewer": "super-reviewer is now style-reviewer, drift-reviewer and scaling-reviewer."
}
"""The built-in roles orbit no longer ships, by name, each with what a project file that sets
anything under its table is told, and the roles it became."""

PLAIN_NAME = re.compile(r"[A-Za-z0-9_-]+")
"""What a lane's or a role's name is made of."""


def _plain_role_name(name: str) -> str:
    """The role's ``name``, refused unless it's a plain name."""
    if not PLAIN_NAME.fullmatch(name):
        raise PydanticCustomError(
            "role_name", "a role's name is a plain name: letters, digits, - and _"
        )
    return name


RoleName = Annotated[str, AfterValidator(_plain_role_name)]
"""A role's name, a plain name as a lane's is, since a role's settings are keyed by it between
dots, as in ``roles.coder.model``."""


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _can_merge(fields: dict[str, Any]) -> bool:
    """Whether a role with the validated ``fields`` can merge its work: it's verified, in a
    worktree. A role that leaves ``merge`` out merges if it can, so an unverified one still
    loads; a verified one that can't is refused, as its checked work would vanish."""
    verify: bool = fields["verify"]
    return verify and fields["workspace"] == "worktree"


def _refuse_granted(tools: tuple[str, ...]) -> None:
    """Raises ``ValueError`` if ``tools`` names a tool orbit grants every role, or grants through
    another one."""
    if READ_SKILL in tools:
        raise ValueError(f"{READ_SKILL} comes with every role; leave it out of tools.")
    if CONSULT in tools:
        raise ValueError(f"{CONSULT} comes with consults, not tools")
    drafts = [tool for tool in tools if tool in DRAFT_TOOLS]
    if drafts:
        raise ValueError(f"{drafts[0]} comes with {PROPOSE_TASK}; list that instead.")


class Role(_Config):
    instructions: str = Field(
        min_length=1,
        description=(
            "What its agents are told about their job: the text, or a Markdown file under .orbit/"
            " it names."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "Its tasks' model, an alias such as"
            ' "opus" or a full id;'
            " unset, the user config's, else the CLI's."
        ),
    )
    tool_access: ToolAccess = Field(
        default="restricted",
        description=(
            "How its tasks may use the CLI's tools:"
            ' "off", "restricted" (read and search) or "full".'
        ),
    )
    tools: tuple[str, ...] = Field(
        default=(GET_TASK, COMPLETE_TASK, REPORT_PROGRESS, GIVE_UP),
        description="The orbit tools its agents may call.",
    )
    blocking_findings: bool = Field(
        default=False,
        description="Whether its agents may report a finding as blocking with report_finding.",
    )
    verify: bool = Field(
        default=True, description="Whether its agents' claims of being done are verified."
    )
    timeout_minutes: float | None = Field(
        default=None, gt=0, description="How long its agents may take, in minutes; unset, no limit."
    )
    lane: str = Field(
        default="work",
        description="The lane that runs its agents, a plain name; a lane runs one task at a time.",
    )
    workspace: Workspace = Field(
        default="worktree",
        description=(
            'Where its agents work: "worktree", "snapshot", "checkout", "none" or "copy".'
        ),
    )
    conversation: bool = Field(
        default=False,
        description="Whether you talk to it, as you do the planner, rather than it taking tasks.",
    )
    merge: bool = Field(
        default_factory=_can_merge, description="Whether its finished work is committed and merged."
    )
    enabled: bool = Field(default=True, description="Whether it runs at all.")
    effort: Effort | None = Field(
        default=None,
        description=(
            'Its effort: "low", "medium", "high", "xhigh" or "max";'
            " unset, the user config's, else the CLI's."
        ),
    )
    runtime: str = Field(
        default=DEFAULT_RUNTIME,
        description=(
            'The runtime its agents run on: "claude" or "kiro".'
            ' Left out, the user config\'s, else "claude".'
        ),
    )
    consults: tuple[str, ...] = Field(
        default=(),
        description="The other conversational roles a conversational role may consult, by name.",
    )
    consult_limit_per_hour: int = Field(
        default=6, ge=0, description="Consultations it may start in an hour; 0, no limit."
    )
    copy_ttl_hours: float = Field(
        default=DEFAULT_COPY_TTL_HOURS,
        gt=0,
        le=MAX_COPY_TTL_HOURS,
        allow_inf_nan=False,
        description="How long its copy is kept after its conversation's last turn, in hours.",
    )
    skills: tuple[str, ...] = Field(
        default=(),
        description=(
            "The skills its agents get, by name: orbit's own, or the project's in .orbit/skills/."
        ),
    )

    @field_validator("tools")
    @classmethod
    def _known_tools(cls, tools: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [tool for tool in tools if tool not in ORBIT_TOOLS]
        if unknown:
            raise ValueError(f"unknown tools {unknown}; orbit's tools are {list(ORBIT_TOOLS)}")
        _refuse_granted(tools)
        return tools

    @field_validator("lane")
    @classmethod
    def _plain_lane(cls, lane: str) -> str:
        if not PLAIN_NAME.fullmatch(lane):
            raise ValueError("a lane is a plain name: letters, digits, - and _")
        return lane

    @model_validator(mode="after")
    def _fields_agree(self) -> Self:
        problems = (
            _merge_problems(self)
            + _verify_problems(self)
            + _checkout_problems(self)
            + _conversation_problems(self)
            + _task_problems(self)
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
    def granted_tools(self) -> tuple[str, ...]:
        """The orbit tools its agents may call: its tools, the draft tools when it proposes
        tasks, ``consult`` when it consults anyone, and ``read_skill``, which every role has."""
        drafts = DRAFT_TOOLS if PROPOSE_TASK in self.tools else ()
        consult = (CONSULT,) if self.consults else ()
        return (*self.tools, *drafts, *consult, READ_SKILL)

    @property
    def runs_checks(self) -> bool:
        """Whether an agent in this role can run verification steps itself."""
        return RUN_CHECK in self.tools

    @property
    def proposes_tasks(self) -> bool:
        """Whether an agent in this role proposes tasks, so it's told the skills it can give
        them."""
        return PROPOSE_TASK in self.tools

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
        "get_task left out of its tools": GET_TASK not in role.tools,
        "merge = false": not role.merge,
        "verify = false": not role.verify,
        'a workspace other than "worktree"': role.workspace != "worktree",
    }
    return _unmet("conversation = true needs", needs)


def _task_problems(role: Role) -> list[str]:
    """What a task ``role`` breaks: it needs get_task to fetch its work, and consults no one."""
    if role.conversation:
        return []
    problems = (
        []
        if GET_TASK in role.tools
        else ["a task role needs get_task in its tools to fetch its task"]
    )
    if role.consults:
        problems.append("consults needs conversation = true")
    return problems


def disabled_roles(roles: Mapping[str, Role]) -> frozenset[str]:
    """The names of the roles among ``roles`` that are disabled, whose work is refused."""
    return frozenset(name for name, role in roles.items() if not role.enabled)


def enabled_roles(roles: Mapping[str, Role]) -> dict[str, Role]:
    """The roles among ``roles`` that aren't disabled, by name, in their order."""
    disabled = disabled_roles(roles)
    return {name: role for name, role in roles.items() if name not in disabled}


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


def role_task(role_name: str, role: Role, prompt: str, skills: tuple[str, ...] = ()) -> TaskSpec:
    """A task orbit queues for ``role`` (named ``role_name``), with its settings, naming the
    ``skills`` its agent gets beyond its role's, none unless given."""
    return TaskSpec(prompt=prompt, settings=role.settings, role=role_name, skills=skills)


def names_file(instructions: str) -> bool:
    """Whether a role's ``instructions``, as a file gives them, name the Markdown file whose
    text they are: they're one line ending in ``.md``."""
    return "\n" not in instructions and instructions.endswith(".md")


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
