"""The modal where the user decides on the checks an agent proposed."""

from collections.abc import Awaitable, Callable
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from orbit.protocol import AgentExited, ChecksProposed, DecideProposal, Event, ProposalAction
from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.proposals import (
    FEEDBACK_HINT,
    FEEDBACK_PLACEHOLDER,
    PROPOSAL_KEYS_HINT,
    proposal_text,
    proposal_title,
)


class ProposalScreen(ModalScreen[DecideProposal | None]):
    """Shows ``proposal`` and dismisses with the user's decision on it.

    ``a`` accepts and ``x`` ends the task; ``r`` opens a box for what to change, which
    enter sends and escape leaves. Dismissed with None when the proposal is withdrawn.
    """

    AUTO_FOCUS = None
    DEFAULT_CSS = """
    ProposalScreen { align: center middle; }
    ProposalScreen > Vertical {
        width: 80;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: round $accent;
        padding: 0 1;
    }
    ProposalScreen Input { border: round $secondary; }
    ProposalScreen Input:focus { border: heavy $accent; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("a", "decide('accept')", show=False),
        Binding("r", "revise", show=False),
        Binding("x", "decide('end')", show=False),
        Binding("escape", "back", show=False),
    ]

    def __init__(self, proposal: ChecksProposed) -> None:
        super().__init__()
        self.proposal = proposal

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = proposal_title(self.proposal.agent_id)
            box.border_subtitle = PROPOSAL_KEYS_HINT
            yield Static(Text(proposal_text(self.proposal)), id="proposal-body")
            feedback = Input(placeholder=FEEDBACK_PLACEHOLDER, id="proposal-feedback")
            feedback.display = False
            yield feedback

    def action_decide(self, action: ProposalAction) -> None:
        self.dismiss(DecideProposal(task_id=self.proposal.task_id, action=action))

    def action_revise(self) -> None:
        self._typing(True)

    def action_back(self) -> None:
        self._typing(False)

    @on(Input.Submitted, "#proposal-feedback")
    def _send(self, event: Input.Submitted) -> None:
        event.stop()
        feedback = event.value.strip()
        if feedback:
            task_id = self.proposal.task_id
            self.dismiss(DecideProposal(task_id=task_id, action="revise", feedback=feedback))

    def _typing(self, on: bool) -> None:
        feedback = self.query_one(Input)
        feedback.display = on
        self.query_one(Vertical).border_subtitle = FEEDBACK_HINT if on else PROPOSAL_KEYS_HINT
        if on:
            feedback.focus()
        else:
            self.set_focus(None)


Decided = Callable[[DecideProposal | None], Awaitable[None]]
Decide = Callable[[ChecksProposed, DecideProposal], Awaitable[object]]


class ProposalHost:
    """Shows the proposals waiting on the user one at a time, each once, with ``push``, and
    closes the one shown when it's no longer waiting; ``decide`` takes the user's decision on
    a proposal."""

    def __init__(self, push: Push[ProposalScreen, DecideProposal], decide: Decide) -> None:
        self._slot = ModalSlot[ProposalScreen, DecideProposal](push)
        self._decide = decide
        self._waiting: list[ChecksProposed] = []  # Those to show after the one shown, in order.
        self._exited: set[str] = set()  # The agents that exited, whose proposals can't be decided.

    @property
    def shown(self) -> ProposalScreen | None:
        """The proposal being shown, if any."""
        return self._slot.showing

    def show(self, event: ChecksProposed) -> None:
        """Ask the user about ``event``'s proposal, or once the one shown is done with, unless
        it's the one shown or waiting already."""
        if self.shown is not None:
            self._wait(event)
            return  # A snapshot showed it before its event arrived, or another is shown.

        async def decided(decision: DecideProposal | None) -> None:
            self._show_next()
            if decision is not None:
                await self._decide(event, decision)

        self._slot.open(ProposalScreen(event), decided)

    def follow(self, proposals: tuple[ChecksProposed, ...]) -> None:
        """Close the proposal shown unless it's among ``proposals``, still waiting, and wait on
        the rest of them; with none shown, show the first. The agents that exited are
        forgotten, as ``proposals`` are authoritative and agent ids restart with orbit."""
        self._exited = set()
        screen = self.shown
        if screen is not None and screen.proposal not in proposals:
            self._slot.close(None)
        self._waiting = []
        for proposal in proposals:
            self._wait(proposal)
        if self.shown is None:
            self._show_next()

    def withdraw(self, event: Event) -> None:
        """Drop the proposals of ``event``'s agent, if it's an agent exiting, closing the one
        shown and showing the next."""
        if not isinstance(event, AgentExited):
            return
        self._exited.add(event.agent_id)
        self._waiting = [p for p in self._waiting if p.agent_id != event.agent_id]
        screen = self.shown
        if screen is not None and screen.proposal.agent_id == event.agent_id:
            self._slot.close(None)
            self._show_next()

    def reopen(self, proposal: ChecksProposed) -> None:
        """Ask the user about ``proposal`` again, its decision undone: now, or next once the one
        shown is done with; not once its agent has exited."""
        if proposal.agent_id in self._exited:
            return
        self._wait_first(proposal)
        if self.shown is None:
            self._show_next()

    def _wait_first(self, proposal: ChecksProposed) -> None:
        """Show ``proposal`` before those waiting, unless it's shown."""
        if self.shown is None or self.shown.proposal != proposal:
            self._waiting = [proposal, *(p for p in self._waiting if p != proposal)]

    def _wait(self, proposal: ChecksProposed) -> None:
        """Show ``proposal`` after those waiting, unless it's shown or waiting already."""
        shown = self.shown is not None and self.shown.proposal == proposal
        if not shown and proposal not in self._waiting:
            self._waiting.append(proposal)

    def _show_next(self) -> None:
        """Show the first proposal waiting, if any."""
        if self._waiting:
            self.show(self._waiting.pop(0))
