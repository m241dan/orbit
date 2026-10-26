"""Consultations held past their asker's hourly limit, and the user's decisions on them."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from orbit.protocol import ApprovalDecided, ApprovalDecision, ApprovalRequested, DecideApproval

APPROVAL_TITLE = "Consultation past the hourly limit"

APPROVAL_CHOICES: tuple[tuple[str, ApprovalDecision, str], ...] = (
    ("a", "allow", "Allow"),
    ("h", "allow_hour", "Allow for the rest of the hour"),
    ("d", "deny", "Deny"),
)
"""Each choice the approval modal offers: its key, the decision and its label."""


def approval_text(request: ApprovalRequested) -> str:
    """What the approval modal asks about ``request``, then its choices, each by its key."""
    asked = f"{request.asked} time" if request.asked == 1 else f"{request.asked} times"
    question = (
        f"The {request.asker} wants to ask the {request.consultant}: '{request.title}'. "
        f"It has asked {asked} this hour."
    )
    choices = [f"{key}  {label}" for key, _, label in APPROVAL_CHOICES]
    return "\n".join([question, "", *choices])


Decided = Callable[[ApprovalDecision | None], Awaitable[None]]
"""Takes the user's decision on the consultation shown, or none if it closed undecided."""
Decide = Callable[[DecideApproval], Awaitable[object]]
"""Sends the user's decision on a consultation held."""


class ApprovalsView(Protocol):
    """Where an :class:`ApprovalQueue` asks about a consultation, one at a time."""

    @property
    def shown(self) -> ApprovalRequested | None:
        """What the modal shown asks, if one is."""
        ...

    def open(self, request: ApprovalRequested, decided: Decided) -> None:
        """Ask ``request``, calling ``decided`` once the user decides or it closes."""

    def close(self) -> None:
        """Close the modal shown, if any, calling nothing."""


class ApprovalQueue:
    """Asks the user about the consultations held one at a time, each once, in ``view``, and
    closes the one shown once it's settled, by this frontend or another; ``decide`` sends the
    user's decision."""

    def __init__(self, view: ApprovalsView, decide: Decide) -> None:
        self._view = view
        self._decide = decide
        self._waiting: list[ApprovalRequested] = []  # Those to ask after the one shown.

    def show(self, request: ApprovalRequested) -> None:
        """Ask ``request`` now, or once the one shown is done with, unless it's asked or waiting
        already."""
        if self._view.shown is not None:
            if self._view.shown != request and request not in self._waiting:
                self._waiting.append(request)
            return

        async def decided(decision: ApprovalDecision | None) -> None:
            self._show_next()
            if decision is not None:
                command = DecideApproval(consultation=request.consultation, decision=decision)
                await self._decide(command)

        self._view.open(request, decided)

    def settle(self, event: ApprovalDecided) -> None:
        """Stop asking about the consultation ``event`` settled, closing it if it's shown."""
        number = event.consultation
        self._waiting = [request for request in self._waiting if request.consultation != number]
        shown = self._view.shown
        if shown is not None and shown.consultation == number:
            self._view.close()
            self._show_next()

    def follow(self, requests: tuple[ApprovalRequested, ...]) -> None:
        """Ask about ``requests``, those held now, alone: close the one shown unless it's among
        them, and wait on the rest; with none shown, ask the first."""
        shown = self._view.shown
        if shown is not None and shown not in requests:
            self._view.close()
        self._waiting = []
        for request in requests:
            self.show(request)

    def _show_next(self) -> None:
        """Ask the first consultation waiting, if any."""
        if self._waiting:
            self.show(self._waiting.pop(0))
