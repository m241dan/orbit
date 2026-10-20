"""Turns backend events into the conversation's entries."""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TypeGuard, assert_never, get_args

from orbit.protocol import (
    PLANNER,
    AgentExited,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    CommandRefused,
    ConversationReset,
    ErrorOccurred,
    Event,
    LogGap,
    MessageSent,
    Notice,
    ProgressNoted,
    ProposalDecided,
    RunningAgent,
    StartupChecked,
    TaskGaveUp,
    TaskMerged,
    TaskStarted,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)
from orbit.tui.panels import VerificationPanel
from orbit.tui.tabs import ConversationTabs
from orbit.tui.tasks import TaskList
from orbit.tui.transcript import Transcript
from orbit.tui.view.common import EntryKind
from orbit.tui.view.log import Entry
from orbit.tui.view.progress import TaskProgress, task_note
from orbit.tui.view.proposals import decision_entry
from orbit.tui.view.rules import exit_rule, planner_reset_entry, turn_rule_entry
from orbit.tui.view.tabs import stopped_callers
from orbit.tui.view.transcript import (
    AgentNote,
    activity_entry,
    error_agent,
    error_entry,
    output_entry,
    speaker,
)
from orbit.tui.view.turns import (
    Turns,
    agent_exited,
    agent_started,
    turn_finished,
    turn_output,
    turn_started,
)

ConversationEvent = (
    TurnStarted
    | TurnFinished
    | ErrorOccurred
    | CommandRefused
    | AssistantMessage
    | ToolCallStarted
    | ToolCallFinished
)
"""Events of an agent's turns: their start and end, errors, messages and tool calls; and
refused commands, shown as errors."""

UserEvent = MessageSent | ProposalDecided
"""What the user said: their messages to agents and their decisions on proposals."""

FeedEvent = ConversationEvent | TaskProgress | AgentNote | AgentRequest | AgentExited | UserEvent
"""Events the conversation shows as entries."""

_FEED_EVENTS: tuple[type[FeedEvent], ...] = get_args(FeedEvent)
"""The classes in ``FeedEvent``, for ``isinstance``."""


def _is_feed_event(event: Event) -> TypeGuard[FeedEvent]:
    """Whether the conversation shows ``event`` as entries."""
    return isinstance(event, _FEED_EVENTS)


@dataclass(eq=False)
class ConversationFeed:
    """Shows events as entries in ``transcript``, under ``tabs``' tabs, timing turns and agents'
    runs by ``clock``."""

    transcript: Transcript
    tabs: ConversationTabs
    task_list: TaskList
    verification: VerificationPanel
    clock: Callable[[], float]
    # The running turns and agents, timed by ``clock``.
    _turns: Turns = field(default_factory=Turns, init=False)
    # Whether the events shown came before those shown already.
    _earlier: bool = field(default=False, init=False)

    def render(self, event: FeedEvent) -> None:
        """Show ``event`` in the conversation."""
        match event:
            case (
                TurnStarted()
                | TurnFinished()
                | ErrorOccurred()
                | CommandRefused()
                | AssistantMessage()
                | ToolCallStarted()
                | ToolCallFinished()
            ):
                self._render_conversation(event)
            case (
                TaskStarted()
                | VerificationStarted()
                | VerificationSkipped()
                | VerificationFinished()
                | TaskMerged()
            ):
                self._render_progress(event)
            case (
                AgentStarted()
                | TaskGaveUp()
                | AgentExited()
                | AgentRequest()
                | AgentReply()
                | ProgressNoted()
                | Notice()
                | StartupChecked()
                | LogGap()
            ):
                self._render_activity(event)
            case MessageSent() | ProposalDecided():
                self._render_user(event)
            case _:
                assert_never(event)

    def replace(self, events: Iterable[Event], running: Sequence[RunningAgent]) -> None:
        """Show ``events`` in place of the conversation and its running turns, as they were
        shown when they happened, with tabs for the agents ``running``, whose entries go under
        their pinned roles' tabs even when ``events`` don't hold their start. The calls and
        requests of agents not ``running``, and of runs a later start under the same id
        ended, show as having no result."""
        self.transcript.clear()
        self.tabs.reset(running)
        self._turns = Turns()
        logged = tuple(events)
        for event in logged:
            self.tabs.track(event)
            self._show_logged(event)
        # The log may end with agents that never exited, or start after some still running.
        self.tabs.follow(running)
        for agent in stopped_callers(logged, running):
            self.transcript.end_tool_calls(agent, requests=True)

    def prepend(self, events: Iterable[Event]) -> None:
        """Show ``events``, which came before every event shown, above them, as they were
        shown when they happened, leaving the tabs, the running turns and the usage counted
        as they are."""
        live, self._turns = self._turns, Turns()
        self._earlier = True
        self.transcript.prepend(lambda: self._show_earlier(events))
        self._turns, self._earlier = live, False

    def _show_earlier(self, events: Iterable[Event]) -> None:
        """Show ``events``, earlier ones, placing their agents in the pinned roles' tabs."""
        for event in events:
            self.tabs.learn_roles(event)
            self._show_logged(event)

    def _show_logged(self, event: Event) -> None:
        """Show ``event``, read from the session's log, if the conversation shows it."""
        if isinstance(event, ConversationReset):
            self.append(*planner_reset_entry(), PLANNER)
        elif _is_feed_event(event):
            self.render(event)

    def append(self, text: str, kind: EntryKind, agent: str | None = None) -> None:
        """Add an entry of ``text`` by ``agent``, if any, under its tab and All."""
        # Entries are kept by tab: a pinned role's agents' under its tab.
        entry = Entry(text, kind, agent, speaker=self._speaker(agent))
        self.transcript.add_entry(entry, self.tabs.key_of(agent))

    def _render_conversation(self, event: ConversationEvent) -> None:
        match event:
            case TurnStarted():
                self._turn_started(event)
            case TurnFinished():
                self._turn_finished(event)
            case ErrorOccurred() | CommandRefused():
                self.append(*error_entry(event), error_agent(event))
            case AssistantMessage() | ToolCallStarted() | ToolCallFinished():
                self._output(event)
            case _:
                assert_never(event)

    def _render_user(self, event: UserEvent) -> None:
        """Show the user's message as a prompt to its agent, or their decision on a proposal
        as orbit's note to the agent that proposed it."""
        if isinstance(event, MessageSent):
            self.append(event.text, "prompt", event.agent)
        else:
            self.append(*decision_entry(event.agent_id, event), event.agent_id)

    def _render_progress(self, event: TaskProgress) -> None:
        """Note a task run's progress in the transcript: under its role's tab, and All,
        marking that tab unread when it isn't being shown, unless it came before what is."""
        state = self.task_list.find(event.task_id)
        number = self.task_list.numbers_by_id.get(event.task_id)
        (text, kind), key = task_note(event, state, number, self.verification.steps)
        if not self._earlier:
            self.tabs.mark_unread(key)
        self.transcript.add_entry(Entry(text, kind), key)

    def _render_activity(self, event: AgentNote | AgentRequest | AgentExited) -> None:
        """Show an agent's activity, a notice or the startup check's result; an agent's start
        ends the calls and requests of any earlier run under its id, its exit ends its run in
        one rule, and a request to orbit is an entry its reply updates."""
        if isinstance(event, AgentExited):
            self._agent_exited(event)
        elif isinstance(event, AgentRequest):
            self._add_tool_call(event)
        elif isinstance(event, AgentReply):
            self._replied(event)
        else:
            if isinstance(event, AgentStarted):
                # Ids start again when orbit restarts: an earlier run under this one is over.
                self.transcript.end_tool_calls(event.agent_id, requests=True)
                self._turns = agent_started(self._turns, event, self.clock())
            self.append(*activity_entry(event), getattr(event, "agent_id", None))

    def _replied(self, event: AgentReply) -> None:
        """Show orbit's reply on the request it answers, or as an entry of its own when no
        request shown is the one."""
        if not self.transcript.finish_tool_call(event):
            self.append(*activity_entry(event), event.agent_id)

    def _agent_exited(self, event: AgentExited) -> None:
        """Add or update the rule ending ``event``'s agent's run, under All and its tab, which
        is known by its id even once the tab has closed."""
        agent = event.agent_id
        self.transcript.end_tool_calls(agent, requests=True)
        self._turns, exited = agent_exited(self._turns, event, self.clock())
        for turn, answer in exited.answers:
            self.transcript.promote(agent, turn, answer)
        rule = exit_rule(event, exited.drawn, exited.elapsed, self._speaker(agent))
        if rule is None:
            return
        if rule.replaces:
            self.transcript.replace_rule(rule.text, rule.kind, self.tabs.key_of(agent), agent)
        else:
            self.append(rule.text, rule.kind, agent)

    def _turn_started(self, event: TurnStarted) -> None:
        # The prompt follows BusyChanged, which also covers task runs.
        self._turns = turn_started(self._turns, event, self.clock())

    def _turn_finished(self, event: TurnFinished) -> None:
        if event.usage is not None and not self._earlier:
            self.task_list.count(event.usage)
        self.transcript.end_tool_calls(event.agent_id)
        self._turns, finished = turn_finished(self._turns, event, self.clock())
        self.transcript.promote(event.agent_id, event.turn_id, finished.answer)
        self.append(
            *turn_rule_entry(event, finished.elapsed, self._speaker(event.agent_id)),
            event.agent_id,
        )

    def _speaker(self, agent: str | None) -> str | None:
        """Who ``agent``'s entries are spoken by: the agent, but not the planner."""
        return speaker(agent, self.tabs.roles)

    def _think(self, event: AssistantMessage) -> None:
        """Show ``event``'s message dim, as thinking out loud until its turn ends."""
        agent = event.agent_id
        self._turns = turn_output(self._turns, event)
        who = self._speaker(agent)
        thinking = Entry(event.text, "assistant", agent, event.turn_id, speaker=who)
        self.transcript.add_entry(thinking, self.tabs.key_of(agent))

    def _output(self, event: AssistantMessage | ToolCallStarted | ToolCallFinished) -> None:
        """Show a piece of a turn's output: a message dim until its turn ends, a tool call's
        start as a step in its turn and an entry its result updates."""
        if isinstance(event, AssistantMessage):
            self._think(event)
        elif isinstance(event, ToolCallStarted):
            self._turns = turn_output(self._turns, event)
            self._add_tool_call(event)
        elif not self.transcript.finish_tool_call(event):
            self.append(*output_entry(event), event.agent_id)

    def _add_tool_call(self, event: ToolCallStarted | AgentRequest) -> None:
        """Add the call or request to orbit ``event`` starts as a running entry."""
        agent = event.agent_id
        self.transcript.add_tool_call(event, self.tabs.key_of(agent), self._speaker(agent))
