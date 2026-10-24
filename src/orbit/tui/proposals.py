"""The modal where the user decides on the checks an agent proposed."""

from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Input, Static

from orbit.protocol import ChecksProposed, DecideProposal, ProposalAction
from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.proposals import (
    FEEDBACK_HINT,
    FEEDBACK_PLACEHOLDER,
    PROPOSAL_KEYS_HINT,
    Decide,
    Decided,
    ProposalQueue,
    proposal_text,
    proposal_title,
    revision,
)
from orbit.tui.widgets import BoxedModal


class ProposalScreen(BoxedModal[DecideProposal | None]):
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
        decision = revision(self.proposal, event.value)
        if decision is not None:
            self.dismiss(decision)

    def _typing(self, on: bool) -> None:
        feedback = self.query_one(Input)
        feedback.display = on
        self.query_one(Vertical).border_subtitle = FEEDBACK_HINT if on else PROPOSAL_KEYS_HINT
        if on:
            feedback.focus()
        else:
            self.set_focus(None)


class ProposalHost:
    """Shows proposals with ``push``, one at a time, as its :class:`ProposalQueue` says;
    ``decide`` takes the user's decision on a proposal."""

    def __init__(self, push: Push[ProposalScreen, DecideProposal], decide: Decide) -> None:
        self._slot = ModalSlot[ProposalScreen, DecideProposal](push)
        self.queue = ProposalQueue(self, decide)

    @property
    def shown(self) -> ChecksProposed | None:
        screen = self._slot.showing
        return None if screen is None else screen.proposal

    def open(self, proposal: ChecksProposed, decided: Decided) -> None:
        self._slot.open(ProposalScreen(proposal), decided)

    def close(self) -> None:
        self._slot.close(None)
