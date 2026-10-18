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
Push = Callable[[ProposalScreen, Decided], object]
Decide = Callable[[ChecksProposed, DecideProposal], Awaitable[object]]


class ProposalHost:
    """Shows each proposal waiting on the user once, with ``push``, and closes it when it's
    no longer waiting; ``decide`` takes the user's decision on a proposal."""

    def __init__(self, push: Push, decide: Decide) -> None:
        self._push = push
        self._decide = decide
        self.shown: ProposalScreen | None = None  # The proposal being shown, if any.

    def show(self, event: ChecksProposed) -> None:
        """Ask the user about ``event``'s proposal, unless it's the one shown."""
        if self.shown is not None and self.shown.proposal == event:
            return  # A snapshot showed it before its event arrived.
        screen = self.shown = ProposalScreen(event)

        async def decided(decision: DecideProposal | None) -> None:
            if self.shown is screen:  # Not one shown since.
                self.shown = None
            if decision is not None:
                await self._decide(event, decision)

        self._push(screen, decided)

    def follow(self, proposals: tuple[ChecksProposed, ...]) -> None:
        """Close the proposal shown unless it's among ``proposals``, still waiting; then, with
        none shown, show the first of them."""
        screen = self.shown
        if screen is not None and screen.proposal not in proposals:
            screen.dismiss(None)
            screen = self.shown = None
        if screen is None and proposals:
            self.show(proposals[0])

    def withdraw(self, event: Event) -> None:
        """Close the proposal shown, if ``event`` is its agent exiting."""
        screen = self.shown
        if isinstance(event, AgentExited) and screen and screen.proposal.agent_id == event.agent_id:
            screen.dismiss(None)
