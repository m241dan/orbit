"""The state models commands and events share, and the base every message is built on."""

from collections.abc import Mapping
from typing import Any, Literal, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_serializer,
    model_validator,
)

from orbit.protocol.names import CODER, MANUAL_ORIGIN, PauseReason, TaskOrigin


class WireTolerant(BaseModel):
    """A model that refuses fields it doesn't declare, except when validated with the ``wire``
    context set, as decoding does: then it drops them, so a newer peer's additions don't
    break it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_unknown_on_wire(cls, data: Any, info: ValidationInfo) -> Any:
        if not isinstance(data, dict) or not (info.context or {}).get("wire"):
            return data
        return {name: value for name, value in data.items() if name in cls.model_fields}


def message_types(union: Any) -> frozenset[str]:
    """The ``type`` names of the messages in the discriminated ``union``."""
    return frozenset(member.model_fields["type"].default for member in get_args(get_args(union)[0]))


def names_unknown_type(data: Any, known: frozenset[str]) -> bool:
    """Whether the envelope ``data``'s payload names a ``type`` that isn't one of the ``known``
    ones."""
    payload = data.get("payload") if isinstance(data, dict) else None
    kind = payload.get("type") if isinstance(payload, dict) else None
    return isinstance(kind, str) and kind not in known


def drop_unknown_types_on_wire(envelopes: Any, known: frozenset[str], info: ValidationInfo) -> Any:
    """``envelopes`` without those of types not ``known``, when validated with the ``wire``
    context set; otherwise ``envelopes`` unchanged, so validation refuses them."""
    if not isinstance(envelopes, list | tuple) or not (info.context or {}).get("wire"):
        return envelopes
    return [envelope for envelope in envelopes if not names_unknown_type(envelope, known)]


def oldest_seq_if_dropped(
    envelopes: Any, known: frozenset[str], info: ValidationInfo
) -> int | None:
    """The lowest ``seq`` among ``envelopes``, counting those of types not ``known``, when
    validated with the ``wire`` context set and some of them are of such types, to be dropped;
    otherwise none."""
    if not isinstance(envelopes, list | tuple) or not (info.context or {}).get("wire"):
        return None
    if not any(names_unknown_type(envelope, known) for envelope in envelopes):
        return None
    return min(_readable_seqs(envelopes), default=None)


def _readable_seqs(envelopes: list[Any] | tuple[Any, ...]) -> list[int]:
    """The integer ``seq`` values of those ``envelopes`` that are objects holding one."""
    seqs = [envelope.get("seq") for envelope in envelopes if isinstance(envelope, dict)]
    return [seq for seq in seqs if type(seq) is int]


class _Message(WireTolerant):
    pass


ToolAccess = Literal["off", "restricted", "full"]
"""How much the agent may use tools, as older peers say it; a :data:`WorkspaceLevel` and web
access say it now, and messages carry both, mapped as :data:`LEVEL_OF_TOOL_ACCESS` and
:data:`TOOL_ACCESS_OF_LEVEL` say.

- ``off``: no tools at all.
- ``restricted``: anything that would need approval is refused; read-only actions still work.
- ``full``: every tool call is approved automatically.
"""

WorkspaceLevel = Literal["none", "read", "write", "shell"]
"""What an agent may do in its workspace, each level allowing what the one before it does.

- ``none``: nothing; it has orbit's tools only.
- ``read``: read and search the workspace.
- ``write``: also edit files, but run no commands.
- ``shell``: also run commands.

Web access is apart from it, so an agent at any level may have it or not.
"""

LEVEL_OF_TOOL_ACCESS: Mapping[ToolAccess, tuple[WorkspaceLevel, bool]] = {
    "off": ("none", False),
    "restricted": ("read", True),
    "full": ("shell", True),
}
"""The workspace level and web access an older peer's tool access means."""

TOOL_ACCESS_OF_LEVEL: Mapping[WorkspaceLevel, ToolAccess] = {
    "none": "off",
    "read": "restricted",
    "write": "full",
    "shell": "full",
}
"""The tool access an older peer knows each workspace level by."""


def levels_of_tool_access(access: object) -> tuple[WorkspaceLevel, bool] | None:
    """The workspace level and web access ``access`` means, if it's tool access; none
    otherwise."""
    return next((levels for known, levels in LEVEL_OF_TOOL_ACCESS.items() if known == access), None)


def tool_access_of_level(level: object) -> ToolAccess | None:
    """The tool access an older peer knows ``level`` by, if it's a workspace level; none
    otherwise."""
    return next((access for known, access in TOOL_ACCESS_OF_LEVEL.items() if known == level), None)


def agreeing_tool_access(data: Any) -> Any:
    """``data``, a message holding tool access, a workspace level and web access, as given, with
    them agreeing: a workspace level given sets the tool access, and tool access given alone,
    as an older peer gives it, sets the workspace level and, unless it's given, web access.
    Values that aren't among them are left for validation to refuse."""
    if not isinstance(data, dict):
        return data
    if "workspace_level" in data:
        access = tool_access_of_level(data["workspace_level"])
        return data if access is None else data | {"tool_access": access}
    levels = levels_of_tool_access(data.get("tool_access"))
    if levels is None:
        return data
    level, web = levels
    return {"workspace_level": level, "web": web} | data


Effort = Literal["low", "medium", "high", "xhigh", "max"]
"""How hard the agent thinks, from ``low`` to ``max``: the levels the Claude CLI offers."""


class Settings(_Message):
    """Per-session agent configuration. ``model=None`` means the backend's default, and
    ``effort=None`` the CLI's. The agent works in its workspace at ``workspace_level``, with
    web access if ``web``; ``tool_access`` says as much to older peers, and agrees with them
    (see :func:`agreeing_tool_access`)."""

    model: str | None = None
    tool_access: ToolAccess = "restricted"
    effort: Effort | None = None
    workspace_level: WorkspaceLevel = "read"
    web: bool = True

    @model_validator(mode="before")
    @classmethod
    def _agree_on_tool_access(cls, data: Any) -> Any:
        return agreeing_tool_access(data)

    @field_serializer("tool_access")
    def _tool_access_of_level(self, _: ToolAccess) -> ToolAccess:
        return TOOL_ACCESS_OF_LEVEL[self.workspace_level]

    def updated(self, changes: Mapping[str, Any]) -> Settings:
        """These settings with ``changes``, by field name, the tool access agreeing with the
        workspace level."""
        return Settings.model_validate(self.model_dump(exclude={"tool_access"}) | dict(changes))


class TaskSpec(_Message):
    """One queued prompt, run in its own fresh context with its own settings.

    ``role`` names the kind of agent that takes it, such as ``coder`` or ``reviewer``.
    ``skills`` are the skills its agent gets beyond its role's, by name; a task naming none gets
    the project's mapped skills instead.
    """

    prompt: str = Field(min_length=1)
    settings: Settings = Settings()
    role: str = Field(default=CODER, min_length=1)
    skills: tuple[str, ...] = ()


class Usage(_Message):
    """What turns cost: tokens of each kind, dollars, credits, model round trips and time taken.

    A runtime that charges in credits, as Kiro does, reports them apart from dollars, rather than
    at a rate. Usages add up, so a task's total is the sum of its turns'.
    """

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    credits: float = Field(default=0.0, ge=0)
    turns: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)

    @property
    def tokens(self) -> int:
        """Every token, of every kind."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    @property
    def read_tokens(self) -> int:
        """The tokens read: the input, with what was cached and what was read from the cache."""
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    def __add__(self, other: Usage) -> Usage:
        mine, theirs = self.model_dump(), other.model_dump()
        return Usage(**{name: mine[name] + theirs[name] for name in mine})


# State the backend owns and reports whole, so the frontend only renders it.

TaskStatus = Literal[
    "draft",
    "pending",
    "running",
    "verifying",
    "completed",
    "unverified",
    "gave_up",
    "cancelled",
    "error",
]
"""Where a task stands.

``verifying``: its turn completed and the verification steps are checking it.
``unverified``: its turn completed but a verification step failed.
``gave_up``: the agent reported it couldn't do the task.
``draft``: the planner proposed it; it waits to be accepted.
"""

ACTIVE_STATUSES: frozenset[TaskStatus] = frozenset({"running", "verifying"})
"""Statuses of a task an agent is working on or orbit is verifying."""

QueuedAs = Literal["task", "review"]
"""What orbit queued a task as: a ``review`` it booked into its review lane, reviewing work it
picked (the commits that landed, or the codebase at one) and suggesting into a batch of its own,
whatever the task's role; or a ``task``, every other, a review the user or the planner added, or
one a trigger queued without booking it, included."""


class TaskState(_Message):
    """One entry of the task list. ``id`` stays the same while the list changes around it.

    ``fixes`` is the id of the task this one was added to fix, if any; it comes right
    after that task. ``attempt`` counts the fixes asked of the agent working on it, out
    of ``max_attempts``; both are 0 until verification first fails. A task a review
    suggested belongs to that review's ``batch``, at review ``round`` (0 otherwise).
    ``usage`` adds up what every turn run for the task cost. A task that ended badly says why
    in ``reason``, one line such as ``Unit tests failed``, with the full text in
    ``reason_detail``; both are empty otherwise. ``proposed_by`` is the conversational role
    that proposed a draft, such as ``planner``; none for every other task. ``edited`` says
    that role edited the draft since proposing it. ``queued_as`` says whether orbit queued it as
    a ``review`` of its own, booked into its review lane, which can't be edited (see
    :data:`QueuedAs`).
    """

    id: str
    task: TaskSpec
    status: TaskStatus = "pending"
    fixes: str | None = None
    origin: TaskOrigin = MANUAL_ORIGIN
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=0, ge=0)
    batch: str | None = None
    round: int = Field(default=0, ge=0)
    usage: Usage = Usage()
    reason: str = ""
    reason_detail: str = ""
    proposed_by: str | None = None
    edited: bool = False
    queued_as: QueuedAs = "task"


SessionState = Literal["running", "idle"]
"""Whether a session is ``running`` a task run, a review or a planner turn, or ``idle``, running
none."""


ActivityKind = Literal["task_run", "waiting_for_coder", "review", "planner", "startup_check"]
"""What's in progress in a session: a ``task_run``, its task ``waiting_for_coder`` while every
coder's slot across the workspaces served is taken, a ``review``, a ``planner`` turn or the
``startup_check``."""


class Activity(_Message):
    """Something in progress in a session, of ``kind``; ``task_id`` is the task a task run runs
    or the task whose work a review reviews, and none otherwise, such as for a super review;
    ``position`` is where a task run ``waiting_for_coder`` waits in line, 1 next, and ``busy``
    how many coders run meanwhile, taking the slots it waits for; both are none otherwise."""

    kind: ActivityKind
    task_id: str | None = None
    position: int | None = Field(default=None, ge=1)
    busy: int | None = Field(default=None, ge=0)


class SessionSummary(_Message):
    """A saved session as the list of sessions shows it: how many of its tasks stand at each
    status in ``counts``, and what they cost together in ``usage``. ``current`` marks the
    session open here, ``locked`` one open in another orbit, and ``unreadable`` says why a
    session can't be opened, if it can't. In a ``Catalog``, ``active`` marks the workspace's
    open session, ``state`` says whether it's running, ``viewers`` counts the connections
    viewing it, ``activity`` says what's in progress in it, and, while its run waits to start
    again by itself, ``paused_until`` says when it does, ``paused_for`` why and
    ``paused_runtime`` on which runtime."""

    id: str
    name: str
    last_active: AwareDatetime | None = None
    counts: dict[TaskStatus, int] = {}
    usage: Usage = Usage()
    current: bool = False
    locked: bool = False
    unreadable: str | None = None
    active: bool = False
    state: SessionState = "idle"
    viewers: int = Field(default=0, ge=0)
    activity: tuple[Activity, ...] = ()
    paused_until: AwareDatetime | None = None
    paused_for: PauseReason | None = None
    paused_runtime: str | None = None


class SessionRef(_Message):
    """A session, by its ``id`` and its ``name``."""

    id: str
    name: str


StepStatus = Literal["pending", "running", "passed", "failed", "cancelled", "skipped"]


class StepState(_Message):
    """One verification step and how it fared after the current task."""

    name: str
    status: StepStatus = "pending"


class MergeHold(_Message):
    """A hold on merges into the user's branch: a named set's, the ``set`` that failed, the
    ``commit`` it failed on and why (``reason``), such as ``Stress tests failed``; or a blocking
    finding's, the lens role that raised it as ``set``, the ``commit`` reviewed, ``reason``
    ``blocking finding``, and the title of the task it was filed as, ``finding``, which a set's
    hold has none of."""

    set: str
    commit: str
    reason: str
    finding: str | None = None


class ProposedStep(_Message):
    """A verification step an agent proposed: its ``name`` and ``command``, and the named ``set``
    it's in; in none, it's one of the task checks."""

    name: str
    command: tuple[str, ...]
    set: str | None = None


class ProposedTrigger(_Message):
    """A trigger an agent proposed to run ``checks``, ``task-checks`` or a named set: the event
    it fires ``on``, how many landings it fires after on ``commits``, ``every``, whether the
    user can fire it, ``manual``, under its ``label``, and whether it's ``enabled``."""

    on: str
    checks: str
    every: int | None = None
    manual: bool = False
    label: str | None = None
    enabled: bool = True


ReportStatus = Literal["failed", "any"]
"""Which runs of its set a proposed report takes: those that failed, or any, passed or failed."""


class ProposedReport(_Message):
    """A report an agent proposed, a trigger queuing a task for the lens ``role`` when a run of
    the named ``set`` finishes with the ``status`` it takes, with the inline ``prompt``, which
    may use ``{set}``, ``{status}``, ``{commit}`` and ``{output}``."""

    set: str
    role: str
    status: ReportStatus = "failed"
    prompt: str


class ProposedSetup(_Message):
    """The setup an agent proposed: its ``command``, and the files whose changes rerun it."""

    command: tuple[str, ...]
    watch: tuple[str, ...] = ()


class UsageLimit(_Message):
    """How much of a subscription usage window, such as ``Session`` or ``Week``, is used, and
    when it resets (timezone-aware), if known."""

    label: str
    percent: float = Field(ge=0, le=100)
    resets_at: AwareDatetime | None = None
