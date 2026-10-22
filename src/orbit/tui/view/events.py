"""Which events are worth rendering."""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TypeIs, get_args

from orbit.protocol import (
    AgentExited,
    AgentRequest,
    AssistantMessage,
    BusyChanged,
    Catalog,
    ChecksProposed,
    CommandAccepted,
    CommandRefused,
    ConversationReset,
    ErrorOccurred,
    Event,
    HistoryPage,
    MessageSent,
    OwnershipDeclined,
    OwnershipOffered,
    OwnershipTaken,
    ProposalDecided,
    ReviewStatus,
    SessionStateChanged,
    SettingsChanged,
    Snapshot,
    TaskMerged,
    TasksChanged,
    TaskStarted,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    UsageChanged,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
    Welcome,
)
from orbit.tui.view.progress import TaskProgress
from orbit.tui.view.transcript import AgentNote

ProgressEvent = (
    VerificationChanged | ReviewStatus | ChecksProposed | UsageChanged | SessionStateChanged
)
"""Snapshots of progress: the verification steps', whether review is paused, proposed steps,
usage limits and a session's state."""

StateEvent = SettingsChanged | BusyChanged | TasksChanged | ProgressEvent
"""Snapshots of backend state, or a decision it waits for."""

TurnEvent = (
    TurnStarted
    | TurnFinished
    | ErrorOccurred
    | AssistantMessage
    | ToolCallStarted
    | ToolCallFinished
)
"""Events of an agent's turns: their start and end, errors, messages and tool calls."""

ActivityEvent = AgentNote | AgentRequest | AgentExited
"""Agents' activity, notices, the startup check's result and events lost when storage failed."""

FeedEvent = TurnEvent | TaskProgress | ActivityEvent
"""Events the conversation shows as they come."""

OwnershipEvent = OwnershipOffered | OwnershipTaken | OwnershipDeclined
"""The session's hold on the repository: offered, taken or declined."""

SessionEvent = ConversationReset | Snapshot | Catalog | HistoryPage | OwnershipEvent
"""Events about the session viewed: a reset conversation, a snapshot, a catalog, a page of
history or its hold on the repository."""

AnswerEvent = CommandAccepted | CommandRefused | Welcome | MessageSent | ProposalDecided
"""What the backend makes of a command: its outcome, or the message or decision it accepted."""

STATE_EVENTS: tuple[type[StateEvent], ...] = get_args(StateEvent)
TASK_PROGRESS_EVENTS: tuple[type[TaskProgress], ...] = get_args(TaskProgress)
ACTIVITY_EVENTS: tuple[type[ActivityEvent], ...] = get_args(ActivityEvent)
FEED_EVENTS: tuple[type[FeedEvent], ...] = get_args(FeedEvent)
OWNERSHIP_EVENTS: tuple[type[OwnershipEvent], ...] = get_args(OwnershipEvent)
SESSION_EVENTS: tuple[type[SessionEvent], ...] = get_args(SessionEvent)
ANSWER_EVENTS: tuple[type[AnswerEvent], ...] = get_args(AnswerEvent)
"""The classes in each grouping, for ``isinstance``."""


def in_group[T](event: object, group: tuple[type[T], ...]) -> TypeIs[T]:
    """Whether ``event`` is one of the classes in ``group``."""
    return isinstance(event, group)


# Snapshot events a later one of the same type replaces, unless one of these events, which
# read the state the snapshot sets, lies between them.
SUPERSEDED_UNLESS: Mapping[type[Event], tuple[type[Event], ...]] = MappingProxyType(
    {
        TasksChanged: (
            TaskStarted,
            VerificationStarted,
            VerificationSkipped,
            VerificationFinished,
            TaskMerged,
        ),
        VerificationChanged: (VerificationStarted, VerificationFinished),
        ReviewStatus: (),
        SettingsChanged: (),
    }
)


def coalesce(events: Sequence[Event]) -> list[Event]:
    """The ``events`` to render, in order: without the snapshots a later one replaces before
    anything reads them."""
    kept: list[Event] = []
    superseded: set[type[Event]] = set()
    for event in reversed(events):
        kind = type(event)
        if kind not in superseded:
            kept.append(event)
        if kind in SUPERSEDED_UNLESS:
            superseded.add(kind)
        superseded -= {s for s in superseded if kind in SUPERSEDED_UNLESS[s]}
    kept.reverse()
    return kept
