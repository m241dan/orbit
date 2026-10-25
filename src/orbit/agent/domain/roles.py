"""Roles agents work in: built-in defaults a project file can change, and the tasks orbit
queues in them.

The built-in roles aren't kept here: whoever builds them passes in the contents of their
``roles.toml`` and the instructions it names.

A role says what an agent is told about its job, which of orbit's tools it may call, whether its
claims of being done are verified, how long it may take and the runtime it runs on. Its model,
tool access and effort are the defaults for tasks orbit queues in that role; a task the user
queues keeps its own settings, but one without an effort runs with its role's. It also says
which lane runs it, where it works, whether you talk to it, whether its work merges and whether
it's enabled, which are checked here. The enabled task roles' lanes are the session's lanes, the
one whose roles merge running on the user's asking; the enabled conversational roles each have a
conversation of their own; where a role works decides whether its tasks take a coder's slot; the
runtimes the enabled roles run on are those orbit must start agents on, and must be able to do
what those roles ask; a role's model, and a task's, must be one its runtime takes; and the work
asked of a disabled role is refused.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self, get_args

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
from orbit.agent.domain.runtimes import (
    CLAUDE,
    UNCHECKED,
    Capabilities,
    ModelAliases,
    RuntimeRules,
    accepted,
    asks_beyond,
    known_models,
    knows_runtime,
    resolves,
    unknown_model,
    unknown_runtime,
)
from orbit.protocol import (
    AGENT_FAILED,
    BUILT_IN_ROLE_NAMES,
    CODER,
    INVALID,
    NOT_CONFIGURED,
    REVIEW_ROLES,
    REVIEWER,
    SUPER_REVIEWER,
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

TOOL_ACCESSES: tuple[ToolAccess, ...] = get_args(ToolAccess)
"""Every level of tool access, in order."""

EFFORTS: tuple[Effort, ...] = get_args(Effort)
"""Every level of effort, in order."""

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
        description='Where its agents work: "worktree", "snapshot", "checkout" or "none".',
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
        default=CLAUDE,
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
        default=24, gt=0, description="How long a consultation's copy is kept, in hours."
    )

    @field_validator("tools")
    @classmethod
    def _known_tools(cls, tools: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [tool for tool in tools if tool not in ORBIT_TOOLS]
        if unknown:
            raise ValueError(f"unknown tools {unknown}; orbit's tools are {list(ORBIT_TOOLS)}")
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
        "get_task left out of its tools": GET_TASK not in role.tools,
        "merge = false": not role.merge,
        "verify = false": not role.verify,
        'a workspace other than "worktree"': role.workspace != "worktree",
    }
    return _unmet("conversation = true needs", needs)


def _task_problems(role: Role) -> list[str]:
    """What a task ``role`` breaks: it needs a way to its work, and consults no one."""
    if role.conversation:
        return []
    fetches = GET_TASK in role.tools or PROPOSE_TASK in role.tools
    problems = (
        []
        if fetches
        else ["an agent needs get_task to fetch its task, or propose_task to plan in conversation"]
    )
    if role.consults:
        problems.append("consults needs conversation = true")
    return problems


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
    worktree = {
        name: role for name, role in roles.items() if role.enabled and role.workspace == "worktree"
    }
    return _lanes_apart('the enabled roles with workspace = "worktree"', worktree)


def review_lane_clashes(roles: Mapping[str, Role]) -> list[str]:
    """Why the enabled reviewer and super-reviewer among ``roles`` can't be where they are, if
    they're in different lanes, naming them by lane: orbit's reviews keep their rules only in the
    review lane, which runs one lane's roles."""
    reviewers = {
        name: role for name, role in roles.items() if role.enabled and name in REVIEW_ROLES
    }
    return _lanes_apart("the enabled reviewer and super-reviewer", reviewers)


def _lanes_apart(who: str, roles: Mapping[str, Role]) -> list[str]:
    """That ``who``, the ``roles``, must share one lane, if they're in more than one, naming
    them by lane."""
    lanes: dict[str, list[str]] = {}
    for name, role in roles.items():
        lanes.setdefault(role.lane, []).append(name)
    if len(lanes) < 2:
        return []
    named = " and ".join(f"lane {lane!r} has {names}" for lane, names in lanes.items())
    return [f"{who} must share one lane, but {named}"]


def role_clashes(roles: Mapping[str, Role]) -> list[str]:
    """How ``roles`` break the rules across roles: the conversational roles whose lanes task
    roles share (see :func:`lane_clashes`), the worktree's roles in more than one lane (see
    :func:`worktree_lane_clashes`), and the review roles in different lanes (see
    :func:`review_lane_clashes`)."""
    return (
        lane_clashes(roles)
        + worktree_lane_clashes(roles)
        + review_lane_clashes(roles)
        + consult_clashes(roles)
    )


def consult_clashes(roles: Mapping[str, Role]) -> list[str]:
    """Why the roles among ``roles`` can't consult whom they name, naming them: each must be
    another enabled conversational role, and none may consult round in a cycle."""
    clashes = [
        f"the role {name!r} can't consult {other!r}, which isn't another enabled"
        " conversational role"
        for name, role in roles.items()
        for other in role.consults
        if other == name or other not in conversational_roles(roles)
    ]
    return clashes + _consult_cycles(roles)


def _consult_cycles(roles: Mapping[str, Role]) -> list[str]:
    """A cycle among the ``roles``' consults, named role by role, if there is one."""
    for start in roles:
        cycle = _cycle_from(roles, [start])
        if cycle:
            return ["a consults cycle: " + " -> ".join(repr(name) for name in cycle)]
    return []


def _onward(roles: Mapping[str, Role], path: list[str], other: str) -> list[str]:
    """The cycle ``path`` carried on through ``other`` makes, unless ``other`` is already on
    it."""
    return [] if other in path else _cycle_from(roles, [*path, other])


def _cycle_from(roles: Mapping[str, Role], path: list[str]) -> list[str]:
    """The ``path`` of consults carried on until it returns to its start, if it can; nothing
    otherwise."""
    role = roles.get(path[-1])
    for other in role.consults if role else ():
        cycle = [*path, other] if other == path[0] else _onward(roles, path, other)
        if cycle:
            return cycle
    return []


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


def review_lane(roles: Mapping[str, Role]) -> str:
    """The lane that runs orbit's reviews among ``roles``: the enabled reviewer's, or the
    super-reviewer's while the reviewer is disabled, as enabled ones share a lane. It's the
    reviewer's if both are disabled, when none is queued."""
    enabled = (roles[name].lane for name in (REVIEWER, SUPER_REVIEWER) if roles[name].enabled)
    return next(enabled, roles[REVIEWER].lane)


def runtime_roles(roles: Mapping[str, Role]) -> dict[str, tuple[str, ...]]:
    """The runtimes the enabled roles among ``roles`` run on, in order, each with the names of
    those roles, in order: those orbit must be able to start agents on."""
    runtimes: dict[str, list[str]] = {}
    for name, role in roles.items():
        if role.enabled:
            runtimes.setdefault(role.runtime, []).append(name)
    return {runtime: tuple(names) for runtime, names in runtimes.items()}


def runtime_problems(roles: Mapping[str, Role], rules: RuntimeRules = UNCHECKED) -> list[str]:
    """Why each of ``roles`` is refused for its runtime, naming it and its runtime, in order: its
    runtime is one this version of orbit doesn't know, its model is one its runtime doesn't take,
    as ``rules`` say by runtime (see :func:`model_problems`), or it's enabled and asks for what
    its runtime can't do, as they say (see :func:`capability_problems`); a runtime they don't
    name isn't checked."""
    return [
        problem for name, role in roles.items() for problem in _role_problems(name, role, rules)
    ]


def _role_problems(name: str, role: Role, rules: RuntimeRules) -> list[str]:
    """Why role ``name``, ``role``, is refused for its runtime, as :func:`runtime_problems`
    says."""
    if not knows_runtime(role.runtime):
        return [unknown_runtime(f"roles.{name}.runtime", role.runtime)]
    problems = model_problems(name, role, rules.models.get(role.runtime))
    can = rules.capabilities.get(role.runtime)
    if role.enabled and can is not None:
        problems += capability_problems(name, role, can)
    return problems


def model_problems(name: str, role: Role, models: ModelAliases | None) -> list[str]:
    """Why role ``name``, ``role``, is refused for its model, naming it, the model and the
    aliases, if its runtime's ``models`` don't resolve it; nothing for no model, or no
    ``models`` to resolve it with."""
    if role.model is None or models is None or resolves(models, role.model):
        return []
    return [unknown_model(f"roles.{name}.model", role.model, models)]


def capability_problems(name: str, role: Role, capabilities: Capabilities) -> list[str]:
    """What role ``name``, ``role``, asks for that its runtime can't do, as its ``capabilities``
    say, in order, each naming the role, the runtime, the request and what the runtime does: a
    level of tool access or effort it doesn't accept, and a conversation it can't carry on."""
    asks = _level_ask("tool_access", role.tool_access, capabilities.tool_access, TOOL_ACCESSES)
    asks += _level_ask("effort", role.effort, capabilities.effort, EFFORTS)
    if role.conversation and not capabilities.resume:
        asks.append(("conversation = true", "doesn't support resume"))
    return [asks_beyond(name, role.runtime, request, supported) for request, supported in asks]


def _level_ask(
    setting: str, level: str | None, supported: frozenset[str], levels: Sequence[str]
) -> list[tuple[str, str]]:
    """``level`` of ``setting``, if it's asked for and isn't ``supported``, with what says which
    of the setting's ``levels`` are, in order; nothing otherwise."""
    if level is None or level in supported:
        return []
    shown = [each for each in levels if each in supported]
    return [(f'{setting} "{level}"', accepted(setting, shown))]


def check_runtimes(roles: Mapping[str, Role], rules: RuntimeRules = UNCHECKED) -> None:
    """Raise :class:`ConfigError` naming each of ``roles`` refused for its runtime, as
    :func:`runtime_problems` says with ``rules``, on a line of its own, if any."""
    problems = runtime_problems(roles, rules)
    if problems:
        raise ConfigError("\n".join(problems))


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


def check_model(name: str, model: str | None, models: ModelAliases | None) -> None:
    """Refuse ``model``, a task's or a conversation's in role ``name``, unless its runtime's
    ``models`` resolve it; no model, or no ``models`` to resolve it with, isn't refused.

    Raises :class:`Refused`, coded ``invalid``, naming the model and the aliases.
    """
    if model is None or models is None or resolves(models, model):
        return
    raise Refused(INVALID, f"{name}'s model \"{model}\" doesn't resolve: {known_models(models)}.")


def check_settings(name: str, runtime: str, settings: Settings, capabilities: Capabilities) -> None:
    """Refuse ``settings``, a task's or a conversation's in role ``name`` on ``runtime``, if
    they ask for a level of tool access or effort its ``capabilities`` don't accept; a level
    left unset isn't refused.

    Raises :class:`Refused`, coded ``invalid``, naming the role, the runtime, the request and
    what the runtime accepts.
    """
    asks = _level_ask("tool_access", settings.tool_access, capabilities.tool_access, TOOL_ACCESSES)
    asks += _level_ask("effort", settings.effort, capabilities.effort, EFFORTS)
    if asks:
        request, supported = asks[0]
        message = f"Role {name}'s runtime {runtime} {supported}, so it can't take {request}."
        raise Refused(INVALID, message)


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


def suggestion_refusal(roles: Mapping[str, Role]) -> str | None:
    """Why a task an agent suggests or proposes is refused, if it is: it's a coder's task, and
    nothing would run it while the coder is disabled among ``roles``."""
    return disabled_note(CODER) if CODER in disabled_roles(roles) else None


def task_suggestion(arguments: Mapping[str, Any]) -> tuple[str, str] | str:
    """The title, on one line, and prompt of the task an agent suggested or proposed with
    ``arguments``; or why it's refused."""
    title = " ".join(str(arguments.get("title", "")).split())
    if not title:
        return "A task needs a title."
    return title, str(arguments.get("prompt", "")).strip()


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
