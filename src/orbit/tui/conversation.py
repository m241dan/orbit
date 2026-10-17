"""Turns backend events into the conversation's entries."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import assert_never

from orbit.protocol import (
    AgentExited,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    ErrorOccurred,
    Notice,
    ProgressNoted,
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
from orbit.tui.entries import Entry
from orbit.tui.panels import VerificationPanel
from orbit.tui.tabs import ConversationTabs
from orbit.tui.tasks import TaskList
from orbit.tui.transcript import Transcript
from orbit.tui.view import (
    AgentNote,
    EntryKind,
    TaskProgress,
    Turns,
    activity_entry,
    agent_exited,
    agent_started,
    error_entry,
    exit_rule,
    output_entry,
    speaker,
    task_note,
    turn_finished,
    turn_output,
    turn_rule_entry,
    turn_started,
)

ConversationEvent = (
    TurnStarted
    | TurnFinished
    | ErrorOccurred
    | AssistantMessage
    | ToolCallStarted
    | ToolCallFinished
)
"""Events of an agent's turns: their start and end, errors, messages and tool calls."""

FeedEvent = ConversationEvent | TaskProgress | AgentNote | AgentRequest | AgentExited
"""Events the conversation shows as entries."""


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

    def render(self, event: FeedEvent) -> None:
        """Show ``event`` in the conversation."""
        match event:
            case (
                TurnStarted()
                | TurnFinished()
                | ErrorOccurred()
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
            ):
                self._render_activity(event)
            case _:
                assert_never(event)

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
            case ErrorOccurred():
                self.append(*error_entry(event), event.agent_id)
            case AssistantMessage() | ToolCallStarted() | ToolCallFinished():
                self._output(event)
            case _:
                assert_never(event)

    def _render_progress(self, event: TaskProgress) -> None:
        """Note a task run's progress in the transcript: under its role's tab, and All,
        marking that tab unread when it isn't being shown."""
        task_list = self.task_list
        state = task_list.find(event.task_id)
        number = None if state is None else task_list.numbers[task_list.position(event.task_id)]
        (text, kind), key = task_note(event, state, number, self.verification.steps)
        self.tabs.mark_unread(key)
        self.transcript.add_entry(Entry(text, kind), key)

    def _render_activity(self, event: AgentNote | AgentRequest | AgentExited) -> None:
        """Show an agent's activity, a notice or the startup check's result; an agent's exit
        ends its run in one rule, and a request to orbit is an entry its reply updates."""
        if isinstance(event, AgentExited):
            self._agent_exited(event)
        elif isinstance(event, AgentRequest):
            self._add_tool_call(event)
        elif isinstance(event, AgentReply):
            self._replied(event)
        else:
            if isinstance(event, AgentStarted):
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
        if event.usage is not None:
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
