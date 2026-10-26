"""The state models commands and events share, and the base every message is built on."""

from typing import Any, Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationInfo, model_validator

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
"""How much the agent may use tools.

- ``off``: no tools at all.
- ``restricted``: anything that would need approval is refused; read-only actions still work.
- ``full``: every tool call is approved automatically.
"""


Effort = Literal["low", "medium", "high", "xhigh", "max"]
"""How hard the agent thinks, from ``low`` to ``max``: the levels the Claude CLI offers."""


class Settings(_Message):
    """Per-session agent configuration. ``model=None`` means the backend's default, and
    ``effort=None`` the CLI's."""

    model: str | None = None
    tool_access: ToolAccess = "restricted"
    effort: Effort | None = None


class TaskSpec(_Message):
    """One queued prompt, run in its own fresh context with its own settings.

    ``role`` names the kind of agent that takes it, such as ``coder`` or ``reviewer``.
    """

    prompt: str = Field(min_length=1)
    settings: Settings = Settings()
    role: str = Field(default=CODER, min_length=1)


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


class TaskState(_Message):
    """One entry of the task list. ``id`` stays the same while the list changes around it.

    ``fixes`` is the id of the task this one was added to fix, if any; it comes right
    after that task. ``attempt`` counts the fixes asked of the agent working on it, out
    of ``max_attempts``; both are 0 until verification first fails. A task a review
    suggested belongs to that review's ``batch``, at review ``round`` (0 otherwise).
    ``usage`` adds up what every turn run for the task cost. A task that ended badly says why
    in ``reason``, one line such as ``Unit tests failed``, with the full text in
    ``reason_detail``; both are empty otherwise. ``proposed_by`` is the conversational role
    that proposed a draft, such as ``planner``; none for every other task.
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


class ProposedStep(_Message):
    """A verification step an agent proposed: its ``name`` and ``command``."""

    name: str
    command: tuple[str, ...]


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
