"""The modal asking whether a consultation past its asker's hourly limit may run."""

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import ModalScreen
from textual.widgets import Static

from orbit.protocol import ApprovalDecision, ApprovalRequested
from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.approvals import APPROVAL_CHOICES, APPROVAL_TITLE, Decided, approval_text


class ApprovalScreen(ModalScreen[ApprovalDecision]):
    """Asks about the consultation ``request`` holds and dismisses with the user's decision, by
    each choice's key. Escape doesn't answer, as the consultation waits for one."""

    AUTO_FOCUS = None
    DEFAULT_CSS = """
    ApprovalScreen { align: center middle; }
    ApprovalScreen > Static {
        width: 70;
        max-width: 90%;
        height: auto;
        border: round $accent;
        padding: 0 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding(key, f"decide('{decision}')", show=False) for key, decision, _ in APPROVAL_CHOICES
    ]

    def __init__(self, request: ApprovalRequested) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        body = Static(Text(approval_text(self.request)), id="approval-body")
        body.border_title = APPROVAL_TITLE
        yield body

    def action_decide(self, decision: ApprovalDecision) -> None:
        self.dismiss(decision)


class ApprovalHost:
    """Asks about consultations with ``push``, one at a time, as an :class:`ApprovalQueue`
    says."""

    def __init__(self, push: Push[ApprovalScreen, ApprovalDecision]) -> None:
        self._slot = ModalSlot[ApprovalScreen, ApprovalDecision](push)

    @property
    def shown(self) -> ApprovalRequested | None:
        screen = self._slot.showing
        return None if screen is None else screen.request

    def open(self, request: ApprovalRequested, decided: Decided) -> None:
        self._slot.open(ApprovalScreen(request), decided)

    def close(self) -> None:
        self._slot.close(None)
