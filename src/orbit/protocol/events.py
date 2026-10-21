"""Events, which flow from the backend to the frontend.

They're a discriminated union on ``type``, so they can be serialized to JSON and sent across a
process boundary without changes.
"""

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import AwareDatetime, Field, TypeAdapter

from orbit.protocol.commands import ProposalAction
from orbit.protocol.models import (
    Activity,
    ProposedSetup,
    ProposedStep,
    SessionRef,
    SessionState,
    SessionSummary,
    Settings,
    StepState,
    TaskState,
    Usage,
    UsageLimit,
    _Message,
)
from orbit.protocol.names import WORK_LANE, BusyLane

if TYPE_CHECKING:  # Envelopes carry events, so those events hold are resolved in envelope.py.
    from orbit.protocol.envelope import EventEnvelope

TurnEndReason = Literal["completed", "cancelled", "max_steps", "error"]


class SettingsChanged(_Message):
    """Current settings, sent on connect and after every change."""

    type: Literal["settings_changed"] = "settings_changed"
    settings: Settings
    model_choices: tuple[str, ...] = ()


# Turn events carry ``agent_id`` when an agent runs the turn, and none when orbit ends it itself.


class TurnStarted(_Message):
    type: Literal["turn_started"] = "turn_started"
    turn_id: str
    agent_id: str | None = None


class AssistantMessage(_Message):
    type: Literal["assistant_message"] = "assistant_message"
    turn_id: str
    agent_id: str | None = None
    text: str


class ToolCallStarted(_Message):
    type: Literal["tool_call_started"] = "tool_call_started"
    turn_id: str
    agent_id: str | None = None
    call_id: str
    name: str
    arguments: dict[str, Any]


class ToolCallFinished(_Message):
    type: Literal["tool_call_finished"] = "tool_call_finished"
    turn_id: str
    agent_id: str | None = None
    call_id: str
    output: str
    is_error: bool = False


class TurnFinished(_Message):
    """The turn ended for ``reason``; ``usage`` says what it cost, when the runner knows."""

    type: Literal["turn_finished"] = "turn_finished"
    turn_id: str
    agent_id: str | None = None
    reason: TurnEndReason
    usage: Usage | None = None


class BusyChanged(_Message):
    """Whether ``lane`` has work in progress; new work for that lane is rejected while busy."""

    type: Literal["busy_changed"] = "busy_changed"
    busy: bool
    lane: BusyLane = WORK_LANE


class TasksChanged(_Message):
    """The whole task list, sent after every change to it."""

    type: Literal["tasks_changed"] = "tasks_changed"
    tasks: tuple[TaskState, ...]


class TaskStarted(_Message):
    """Task ``task_id`` begins; its turn's events follow."""

    type: Literal["task_started"] = "task_started"
    task_id: str


class VerificationChanged(_Message):
    """The verification steps and their progress, sent on connect and after every change.

    Not sent when no steps are configured. ``title`` names what the steps are checking when
    it isn't the current task, such as the startup check; the panel shows it as a heading.
    """

    type: Literal["verification_changed"] = "verification_changed"
    steps: tuple[StepState, ...]
    title: str = ""


class StartupChecked(_Message):
    """The verification steps ran once on the base commit at startup. Unless they all
    ``passed``, ``step`` failed with ``output`` (its end), and a draft task to fix it was added."""

    type: Literal["startup_checked"] = "startup_checked"
    passed: bool
    step: str = ""
    output: str = ""


class VerificationStarted(_Message):
    """Step ``step`` (an index into the steps) begins checking task ``task_id``."""

    type: Literal["verification_started"] = "verification_started"
    task_id: str
    step: int


SkipReason = Literal["tools_off", "no_changes", "unchanged", "no_steps"]
"""Why a task wasn't verified: it ran with tools off, it changed no files, its agent stopped
without changing anything since its last failed verification, or no steps are configured."""


class VerificationSkipped(_Message):
    """Task ``task_id`` completed but has nothing to verify, so no step runs."""

    type: Literal["verification_skipped"] = "verification_skipped"
    task_id: str
    reason: SkipReason


class VerificationFinished(_Message):
    """Step ``step`` (an index into the steps) finished checking task ``task_id``."""

    type: Literal["verification_finished"] = "verification_finished"
    task_id: str
    step: int
    passed: bool


class ErrorOccurred(_Message):
    """An error with no command behind it, of the kind ``code`` names (one of the error codes,
    such as :data:`AGENT_FAILED`, or a refusal code where it fits), and ``message`` says what."""

    type: Literal["error_occurred"] = "error_occurred"
    turn_id: str | None = None
    agent_id: str | None = None
    code: str
    message: str


class CommandAccepted(_Message):
    """Command ``command_id`` was handled; the events its handling published came before."""

    type: Literal["command_accepted"] = "command_accepted"
    command_id: str


class Welcome(_Message):
    """``Hello`` ``command_id`` was received, in place of its ``CommandAccepted``: the backend,
    orbit ``server_version``, publishes its events from here on."""

    type: Literal["welcome"] = "welcome"
    command_id: str
    server_version: str


class CommandRefused(_Message):
    """Command ``command_id`` was refused, for the reason ``code`` names (one of the refusal
    codes, such as :data:`UNKNOWN`), and ``message`` says why; it changed nothing, unless the
    code is :data:`INTERNAL`, which means orbit failed partway through handling it, so it may
    have changed something."""

    type: Literal["command_refused"] = "command_refused"
    command_id: str
    code: str
    message: str


MergeResult = Literal["merged", "rebased", "conflict", "blocked", "detached"]
"""What came of merging a task's commit into the base branch.

``merged``: the base now has it. ``rebased``: the base had moved, so it was replayed on top and
must be verified again. ``conflict``: it couldn't be replayed. ``blocked``: the base's checkout
has changes in the way. ``detached``: there's no base branch to merge into.
"""


class TaskMerged(_Message):
    """Task ``task_id``'s work was committed as ``commit``, and this came of merging it;
    ``detail`` says why when it wasn't merged."""

    type: Literal["task_merged"] = "task_merged"
    task_id: str
    result: MergeResult
    detail: str = ""
    commit: str = ""


class ReviewStatus(_Message):
    """Whether review is paused, and how many review batches are still open; sent on changes."""

    type: Literal["review_status"] = "review_status"
    paused: bool
    open_batches: int


class Notice(_Message):
    """Something worth the user's attention that isn't an error."""

    type: Literal["notice"] = "notice"
    message: str


AgentExitReason = Literal["finished", "crashed", "killed"]
"""How an agent's process ended: on its own, with a failure, or stopped by orbit."""


class AgentStarted(_Message):
    """Agent ``agent_id`` was started in the ``role`` to work on task ``task_id``."""

    type: Literal["agent_started"] = "agent_started"
    agent_id: str
    role: str
    task_id: str


class AgentRequest(_Message):
    """Agent ``agent_id`` called one of orbit's tools; an ``AgentReply`` with the same
    ``request_id`` follows."""

    type: Literal["agent_request"] = "agent_request"
    agent_id: str
    request_id: str
    tool: str
    arguments: dict[str, Any]


class AgentReply(_Message):
    """Orbit's answer to request ``request_id``."""

    type: Literal["agent_reply"] = "agent_reply"
    agent_id: str
    request_id: str
    text: str
    is_error: bool = False


class ProgressNoted(_Message):
    """A short status note an agent reported while working on task ``task_id``."""

    type: Literal["progress_noted"] = "progress_noted"
    agent_id: str
    task_id: str
    note: str


class TaskGaveUp(_Message):
    """The agent working on task ``task_id`` reported it can't do it, and why."""

    type: Literal["task_gave_up"] = "task_gave_up"
    agent_id: str
    task_id: str
    reason: str


class AgentExited(_Message):
    """Agent ``agent_id``'s process ended."""

    type: Literal["agent_exited"] = "agent_exited"
    agent_id: str
    reason: AgentExitReason


class ChecksProposed(_Message):
    """Agent ``agent_id`` proposed these checks for task ``task_id``, and they all passed on
    the current code; the user decides on them with ``DecideProposal``."""

    type: Literal["checks_proposed"] = "checks_proposed"
    agent_id: str
    task_id: str
    steps: tuple[ProposedStep, ...]
    setup: ProposedSetup | None = None


class ProposalDecided(_Message):
    """The user's decision on agent ``agent_id``'s checks proposed for task ``task_id``, sent
    as command ``command_id``, was recorded: the ``action``, with the ``feedback`` saying what
    to change when it's ``revise``."""

    type: Literal["proposal_decided"] = "proposal_decided"
    command_id: str
    agent_id: str
    task_id: str
    action: ProposalAction
    feedback: str = ""


UsageStatus = Literal["ok", "rate_limited", "unavailable"]
"""Whether the latest usage fetch worked, was rate limited, or couldn't be made."""


class UsageChanged(_Message):
    """The subscription's usage limits, sent after every fetch.

    Unless the ``status`` is ``ok``, ``limits`` are the last good ones; ``detail`` says why
    usage is unavailable, and ``retry_at`` when a rate-limited fetch is tried again.
    """

    type: Literal["usage_changed"] = "usage_changed"
    limits: tuple[UsageLimit, ...] = ()
    status: UsageStatus = "ok"
    detail: str = ""
    retry_at: AwareDatetime | None = None


class MessageSent(_Message):
    """The user's message ``text`` to the agent ``agent``, sent as command ``command_id``, was
    accepted; the agent's turn on it follows."""

    type: Literal["message_sent"] = "message_sent"
    command_id: str
    agent: str
    text: str


class ConversationReset(_Message):
    """The conversation of the agent ``agent`` ended at the user's ``ResetConversation``; its
    next message starts a fresh one."""

    type: Literal["conversation_reset"] = "conversation_reset"
    agent: str


class OpenRequest(_Message):
    """A call ``request_id`` to orbit's ``tool`` that an agent made and orbit hasn't answered."""

    request_id: str
    tool: str


class RunningAgent(_Message):
    """Agent ``agent_id``, running in the ``role`` on task ``task_id``, with its
    ``open_requests``, oldest first."""

    agent_id: str
    role: str
    task_id: str
    open_requests: tuple[OpenRequest, ...] = ()


class Snapshot(_Message):
    """The session a connection views as it stands, sent in answer to ``Attach`` and after every
    switch; its envelope's ``seq`` is the point it's current to.

    It holds the ``session``'s summary, its ``settings`` and ``model_choices``, whether each
    lane is ``busy``, its ``tasks``, the verification ``steps`` and the ``steps_title`` they
    show under, the ``review`` status (none when reviews are off), the checks ``proposals``
    waiting for a decision, the ``agents`` running, the ``transcript``: the session's
    last logged events, at most 500, oldest first, in the envelopes they were sent in, and,
    while the session is halted because storage failed, ``storage_halt``: why, and how to
    resume; ``read_only`` says another orbit owns the repository, so the work this one is
    asked to run waits, and ``ownership_offered`` that the repository is offered, as
    ``OwnershipOffered`` said, until the offer is settled. ``active`` says whether the session
    is the workspace's open one, which runs work; one that isn't is a read-only view, from its
    saved record and its last logged events. ``open_session`` names the workspace's open
    session, and ``viewers`` counts the connections viewing this one.
    """

    type: Literal["snapshot"] = "snapshot"
    session: SessionSummary
    settings: Settings
    model_choices: tuple[str, ...]
    busy: dict[BusyLane, bool]
    tasks: tuple[TaskState, ...]
    steps: tuple[StepState, ...]
    steps_title: str = ""
    review: ReviewStatus | None
    proposals: tuple[ChecksProposed, ...]
    agents: tuple[RunningAgent, ...]
    transcript: tuple["EventEnvelope", ...] = ()
    storage_halt: str | None = None
    read_only: bool = False
    ownership_offered: bool = False
    active: bool = True
    open_session: SessionRef | None = None
    viewers: int = Field(default=0, ge=0)


class HistoryPage(_Message):
    """The events of the session ``session`` that ``History`` asked for, oldest first, in the
    envelopes they were sent in; ``more`` says whether older ones remain, and ``trimmed``
    whether older ones were removed from the log."""

    type: Literal["history_page"] = "history_page"
    session: str
    events: tuple["EventEnvelope", ...]
    more: bool
    trimmed: bool = False


class LogGap(_Message):
    """The session's events ``first_seq`` to ``last_seq`` were lost: a write to the store failed
    and they reached neither the log nor a fallback file."""

    type: Literal["log_gap"] = "log_gap"
    first_seq: int
    last_seq: int


class OwnershipOffered(_Message):
    """The repository this read-only orbit waits on is free, and this orbit holds it until the
    user answers with ``TakeOwnership``; sent once for each time it's found free."""

    type: Literal["ownership_offered"] = "ownership_offered"


class OwnershipTaken(_Message):
    """This orbit owns the repository now, so it's no longer read-only and runs its work; the
    offer is settled."""

    type: Literal["ownership_taken"] = "ownership_taken"


class OwnershipDeclined(_Message):
    """The offer of the repository was declined, so this orbit stays read-only and lets the
    repository go; the offer is settled."""

    type: Literal["ownership_declined"] = "ownership_declined"


class SessionStateChanged(_Message):
    """Session ``session`` is now ``active`` (the workspace's open session) or not, in
    ``state``, with ``viewers`` connections viewing it, and ``activity`` in progress in it; sent
    to every connection whenever one of them changes, and never logged."""

    type: Literal["session_state_changed"] = "session_state_changed"
    session: str
    active: bool
    state: SessionState
    viewers: int = Field(ge=0)
    activity: tuple[Activity, ...] = ()


def with_state(summary: SessionSummary, changed: SessionStateChanged) -> SessionSummary:
    """``summary``, as active, running, viewed and doing what ``changed`` says, if ``changed``
    is about its session."""
    if changed.session != summary.id:
        return summary
    update = {
        "active": changed.active,
        "state": changed.state,
        "viewers": changed.viewers,
        "activity": changed.activity,
    }
    return summary.model_copy(update=update)


class WorkspaceEntry(_Message):
    """Workspace ``id``, shown as ``label``, at ``path`` on the backend's host, if it has one,
    and its ``sessions`` as the list of sessions shows them."""

    id: str
    label: str
    path: str | None = None
    sessions: tuple[SessionSummary, ...]


class AgentEntry(_Message):
    """An agent role ``name``, shown as ``label``; ``conversational`` if the user can message
    it."""

    name: str
    label: str
    conversational: bool


class TriggerEntry(_Message):
    """A trigger ``name``, shown as ``label``; ``manual`` if the user fires it."""

    name: str
    label: str
    manual: bool


class Catalog(_Message):
    """What exists: the ``workspaces`` and their sessions, the ``agents``' roles, the
    ``triggers`` and the ``lanes``, and whether this orbit is ``read_only``, as another owns
    the repository; sent after ``Welcome`` and, to the connections attached, whenever the
    sessions change."""

    type: Literal["catalog"] = "catalog"
    workspaces: tuple[WorkspaceEntry, ...]
    agents: tuple[AgentEntry, ...]
    triggers: tuple[TriggerEntry, ...]
    lanes: tuple[str, ...]
    read_only: bool = False


Event = Annotated[
    SettingsChanged
    | TurnStarted
    | AssistantMessage
    | ToolCallStarted
    | ToolCallFinished
    | TurnFinished
    | BusyChanged
    | TasksChanged
    | TaskStarted
    | VerificationChanged
    | StartupChecked
    | VerificationStarted
    | VerificationSkipped
    | VerificationFinished
    | ErrorOccurred
    | AgentStarted
    | AgentRequest
    | AgentReply
    | ProgressNoted
    | TaskGaveUp
    | AgentExited
    | TaskMerged
    | Notice
    | ReviewStatus
    | ChecksProposed
    | ProposalDecided
    | UsageChanged
    | MessageSent
    | ConversationReset
    | LogGap
    | OwnershipOffered
    | OwnershipTaken
    | OwnershipDeclined
    | SessionStateChanged
    | Snapshot
    | HistoryPage
    | Catalog
    | Welcome
    | CommandAccepted
    | CommandRefused,
    Field(discriminator="type"),
]


event_adapter: TypeAdapter[Event] = TypeAdapter(Event)
