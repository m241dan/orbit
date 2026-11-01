"""Proposed checks and the user's decisions on them."""

import shlex
from collections.abc import Awaitable, Callable
from typing import Protocol

from orbit.protocol import (
    TASK_CHECKS,
    AgentExited,
    ChecksProposed,
    DecideProposal,
    Event,
    ProposalAction,
    ProposalDecided,
    ProposedTrigger,
)
from orbit.tui.view.common import EntryKind

PROPOSAL_KEYS_HINT = "a accept · r revise · x end"
FEEDBACK_HINT = "enter send · esc back"
FEEDBACK_PLACEHOLDER = "What should change?"

COMMITS_EVENT = "commits"
MANUAL_EVENT = "manual"
EVENT_PHRASES = {
    "session_started": "when orbit opens",
    "task_landed": "when a task lands",
    "task_finished": "when a task finishes",
    "startup_check_failed": "when the startup check fails",
    MANUAL_EVENT: "by hand",
}
"""When a proposed trigger on each event, by name, fires, in words; ``commits`` counts."""

DECISION_NOTES: dict[ProposalAction, str] = {
    "accept": "proposal accepted",
    "revise": "changes asked for: ",
    "end": "task ended",
}


def proposal_title(agent_id: str) -> str:
    """The proposal modal's border title, naming the agent ``agent_id`` that proposed."""
    return f"Proposed checks · {agent_id}"


def proposal_text(event: ChecksProposed) -> str:
    """The proposed setup, then each step in order, and that they all passed. With steps in
    named sets, the steps come as the task checks, then each set, each step keeping its number,
    each group saying when it runs and each set whether it holds merges."""
    setup = event.setup
    lines = [f"Setup: {'none' if setup is None else shlex.join(setup.command)}"]
    if setup is not None and setup.watch:
        lines.append(f"  reruns when these change: {', '.join(setup.watch)}")
    if any(step.set is not None for step in event.steps):
        lines += _grouped_steps(event)
    else:
        lines += ["", "Steps:", *_step_lines(event, None)]
    lines += ["", "✅ All passed on the current code."]
    return "\n".join(lines)


def _grouped_steps(event: ChecksProposed) -> list[str]:
    """``event``'s task checks, then each named set, in the order its steps first appear, each
    under a line saying when it runs, and for a set, whether it holds merges."""
    task_checks = ", ".join(["every task", *_runs(event, TASK_CHECKS)])
    lines = ["", f"Task checks: {task_checks}", *(_step_lines(event, None) or ["  none"])]
    return lines + [line for name in _set_names(event) for line in _set_lines(event, name)]


def _set_names(event: ChecksProposed) -> tuple[str, ...]:
    """The names of the sets ``event``'s steps are in, each once, in the order they first
    appear."""
    return tuple(dict.fromkeys(name for name in (s.set for s in event.steps) if name is not None))


def _set_lines(event: ChecksProposed, name: str) -> list[str]:
    """The set ``name`` of ``event``'s, under a line saying when it runs and whether it holds
    merges, after a blank line."""
    effect = "holds merges" if event.sets.get(name, False) else "reports"
    runs = ", ".join([*(_runs(event, name) or ["no trigger"]), effect])
    return ["", f"Set {name}: {runs}", *_step_lines(event, name)]


def _step_lines(event: ChecksProposed, name: str | None) -> list[str]:
    """The line of each of ``event``'s steps in the set ``name``, or in none, with its number
    among all the steps."""
    return [
        f"  {i}. {s.name}: {shlex.join(s.command)}"
        for i, s in enumerate(event.steps, 1)
        if s.set == name
    ]


def _runs(event: ChecksProposed, checks: str) -> list[str]:
    """When each of ``event``'s triggers that runs ``checks`` fires, in order."""
    return [_fires(trigger) for trigger in event.triggers.values() if trigger.checks == checks]


def _fires(trigger: ProposedTrigger) -> str:
    """When ``trigger`` fires, as in ``every 10 commits`` or ``by hand``, and that it's off if
    it isn't enabled."""
    when = _event_phrase(trigger)
    if trigger.manual and trigger.on != MANUAL_EVENT:
        when += " or by hand"
    return when if trigger.enabled else f"{when} (off)"


def _event_phrase(trigger: ProposedTrigger) -> str:
    """When ``trigger``'s event happens, in words; ``on`` and its name for one this doesn't
    know."""
    if trigger.on == COMMITS_EVENT:
        every = trigger.every or 1
        return "every commit" if every == 1 else f"every {every} commits"
    return EVENT_PHRASES.get(trigger.on, f"on {trigger.on}")


def revision(proposal: ChecksProposed, feedback: str) -> DecideProposal | None:
    """The decision asking for the changes ``feedback`` says to ``proposal``; none while it's
    blank."""
    text = feedback.strip()
    if not text:
        return None
    return DecideProposal(task_id=proposal.task_id, action="revise", feedback=text)


def decision_entry(agent: str, decision: DecideProposal | ProposalDecided) -> tuple[str, EntryKind]:
    """The user's ``decision`` on ``agent``'s proposal, as a transcript entry."""
    note = DECISION_NOTES[decision.action]
    if decision.action == "revise":
        note += decision.feedback
    return f"orbit → {agent}: {note}", "agent"


Decided = Callable[[DecideProposal | None], Awaitable[None]]
"""Takes the user's decision on the proposal shown, or none if they closed it undecided."""
Decide = Callable[[ChecksProposed, DecideProposal], Awaitable[object]]
"""Takes the user's decision on a proposal."""


class ProposalsView(Protocol):
    """Where a :class:`ProposalQueue` shows a proposal, one at a time."""

    @property
    def shown(self) -> ChecksProposed | None:
        """The proposal shown, if any."""
        ...

    def open(self, proposal: ChecksProposed, decided: Decided) -> None:
        """Show ``proposal``, calling ``decided`` once the user decides or closes it."""

    def close(self) -> None:
        """Close the proposal shown, if any, calling nothing."""


class ProposalQueue:
    """Shows the proposals waiting on the user one at a time, each once, in ``view``, and
    closes the one shown when it's no longer waiting; ``decide`` takes the user's decision on
    a proposal."""

    def __init__(self, view: ProposalsView, decide: Decide) -> None:
        self._view = view
        self._decide = decide
        self._waiting: list[ChecksProposed] = []  # Those to show after the one shown, in order.
        self._exited: set[str] = set()  # The agents that exited, whose proposals can't be decided.

    def show(self, event: ChecksProposed) -> None:
        """Ask the user about ``event``'s proposal, or once the one shown is done with, unless
        it's the one shown or waiting already. Its agent no longer counts as exited, as agent
        ids restart with orbit."""
        self._exited.discard(event.agent_id)
        if self._view.shown is not None:
            self._wait(event)
            return  # A snapshot showed it before its event arrived, or another is shown.

        async def decided(decision: DecideProposal | None) -> None:
            self._show_next()
            if decision is not None:
                await self._decide(event, decision)

        self._view.open(event, decided)

    def follow(self, proposals: tuple[ChecksProposed, ...]) -> None:
        """Close the proposal shown unless it's among ``proposals``, still waiting, and wait on
        the rest of them; with none shown, show the first. The agents that exited are
        forgotten, as ``proposals`` are authoritative and agent ids restart with orbit."""
        self._exited = set()
        shown = self._view.shown
        if shown is not None and shown not in proposals:
            self._view.close()
        self._waiting = []
        for proposal in proposals:
            self._wait(proposal)
        if self._view.shown is None:
            self._show_next()

    def withdraw(self, event: Event) -> None:
        """Drop the proposals of ``event``'s agent, if it's an agent exiting, closing the one
        shown and showing the next."""
        if not isinstance(event, AgentExited):
            return
        self._exited.add(event.agent_id)
        self._waiting = [p for p in self._waiting if p.agent_id != event.agent_id]
        shown = self._view.shown
        if shown is not None and shown.agent_id == event.agent_id:
            self._view.close()
            self._show_next()

    def reopen(self, proposal: ChecksProposed) -> None:
        """Ask the user about ``proposal`` again, its decision undone: now, or next once the one
        shown is done with; not once its agent has exited."""
        if proposal.agent_id in self._exited:
            return
        self._wait_first(proposal)
        if self._view.shown is None:
            self._show_next()

    def _wait_first(self, proposal: ChecksProposed) -> None:
        """Show ``proposal`` before those waiting, unless it's shown."""
        if self._view.shown != proposal:
            self._waiting = [proposal, *(p for p in self._waiting if p != proposal)]

    def _wait(self, proposal: ChecksProposed) -> None:
        """Show ``proposal`` after those waiting, unless it's shown or waiting already."""
        if self._view.shown != proposal and proposal not in self._waiting:
            self._waiting.append(proposal)

    def _show_next(self) -> None:
        """Show the first proposal waiting, if any."""
        if self._waiting:
            self.show(self._waiting.pop(0))
