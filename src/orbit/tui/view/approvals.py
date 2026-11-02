"""What waits for the user's approval, and their decisions on it: consultations held past their
asker's hourly limit, and agents' calls to MCP tools whose servers hold them."""

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from rich.cells import cell_len

from orbit.protocol import (
    ApprovalDecided,
    ApprovalDecision,
    ApprovalRequested,
    DecideApproval,
    DecideToolCall,
    ToolCallAnswer,
    ToolCallDecided,
    ToolCallHeld,
)
from orbit.tui.view.common import clipped, counted

APPROVAL_TITLE = "Consultation past the hourly limit"
TOOL_CALL_TITLE = "Tool call waiting for approval"

APPROVAL_CHOICES: tuple[tuple[str, ApprovalDecision, str], ...] = (
    ("a", "allow", "Allow"),
    ("h", "allow_hour", "Allow for the rest of the hour"),
    ("d", "deny", "Deny"),
)
"""Each choice the approval modal offers about a consultation: its key, the decision and its
label."""

TOOL_CALL_CHOICES: tuple[tuple[str, ApprovalDecision, str], ...] = (
    ("a", "allow", "Allow"),
    ("d", "deny", "Deny"),
)
"""Each choice the approval modal offers about an agent's call: its key, the decision and its
label."""

ARGUMENT_LIMIT = 60
"""The most cells an argument of a held call is shown in."""

type HeldRequest = ApprovalRequested | ToolCallHeld
"""What the user is asked about: a consultation, or an agent's call, held for approval."""

type HeldDecision = ApprovalDecided | ToolCallDecided
"""The settling of what the user was asked about."""

type HeldKey = tuple[str, int]
"""What a request held, or its settling, is told apart by: its kind and its number."""


def approval_text(request: ApprovalRequested) -> str:
    """What the approval modal asks about ``request``, then its choices, each by its key."""
    question = (
        f"The {request.asker} wants to ask the {request.consultant}: '{request.title}'. "
        f"It has asked {counted(request.asked, 'time')} this hour."
    )
    return _asked(question, APPROVAL_CHOICES)


def tool_call_text(request: ToolCallHeld) -> str:
    """What the approval modal asks about the call ``request`` holds, its long arguments cut
    short, then its choices, each by its key."""
    arguments = shown_arguments(request.arguments)
    question = f"The {request.role} wants to call {request.tool} with {arguments}."
    return _asked(question, TOOL_CALL_CHOICES)


def shown_arguments(arguments: Mapping[str, Any]) -> str:
    """``arguments`` as JSON, each value whose text is longer than :data:`ARGUMENT_LIMIT` cells
    cut short, as text."""
    return json.dumps({key: _shown(value) for key, value in arguments.items()}, ensure_ascii=False)


def _shown(value: Any) -> Any:
    """``value``, or its text cut to :data:`ARGUMENT_LIMIT` cells if it's longer."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return value if cell_len(text) <= ARGUMENT_LIMIT else clipped(text, ARGUMENT_LIMIT)


def _asked(question: str, choices: tuple[tuple[str, ApprovalDecision, str], ...]) -> str:
    """``question``, then ``choices``, each by its key."""
    return "\n".join([question, "", *(f"{key}  {label}" for key, _, label in choices)])


def held_title(request: HeldRequest) -> str:
    """The title of the modal asking about ``request``."""
    return TOOL_CALL_TITLE if isinstance(request, ToolCallHeld) else APPROVAL_TITLE


def held_text(request: HeldRequest) -> str:
    """What the modal asks about ``request``, then its choices."""
    if isinstance(request, ToolCallHeld):
        return tool_call_text(request)
    return approval_text(request)


def held_choices(request: HeldRequest) -> tuple[tuple[str, ApprovalDecision, str], ...]:
    """The choices the modal offers about ``request``."""
    return TOOL_CALL_CHOICES if isinstance(request, ToolCallHeld) else APPROVAL_CHOICES


def held_key(request: HeldRequest) -> HeldKey:
    """What tells ``request`` apart from the others held."""
    if isinstance(request, ToolCallHeld):
        return ("tool_call", request.call)
    return ("consultation", request.consultation)


def decided_key(event: HeldDecision) -> HeldKey:
    """What tells the request ``event`` settles apart from the others held."""
    if isinstance(event, ToolCallDecided):
        return ("tool_call", event.call)
    return ("consultation", event.consultation)


def decision_command(
    request: HeldRequest, decision: ApprovalDecision
) -> DecideApproval | DecideToolCall:
    """The command sending the user's ``decision`` on ``request``; a call is allowed only by
    ``allow``."""
    if isinstance(request, ToolCallHeld):
        answer: ToolCallAnswer = "allow" if decision == "allow" else "deny"
        return DecideToolCall(call=request.call, decision=answer)
    return DecideApproval(consultation=request.consultation, decision=decision)


Decided = Callable[[ApprovalDecision | None], Awaitable[None]]
"""Takes the user's decision on the request shown, or none if it closed undecided."""
Decide = Callable[[DecideApproval | DecideToolCall], Awaitable[object]]
"""Sends the user's decision on a request held."""


class ApprovalsView(Protocol):
    """Where an :class:`ApprovalQueue` asks about a request held, one at a time."""

    @property
    def shown(self) -> HeldRequest | None:
        """What the modal shown asks, if one is."""
        ...

    def open(self, request: HeldRequest, decided: Decided) -> None:
        """Ask ``request``, calling ``decided`` once the user decides or it closes."""

    def close(self) -> None:
        """Close the modal shown, if any, calling nothing."""


class ApprovalQueue:
    """Asks the user about the consultations and the agents' calls held one at a time, each
    once, in ``view``, and closes the one shown once it's settled, by this frontend or another;
    ``decide`` sends the user's decision."""

    def __init__(self, view: ApprovalsView, decide: Decide) -> None:
        self._view = view
        self._decide = decide
        self._waiting: list[HeldRequest] = []  # Those to ask after the one shown.

    def show(self, request: HeldRequest) -> None:
        """Ask ``request`` now, or once the one shown is done with, unless it's asked or waiting
        already."""
        if self._view.shown is not None:
            if self._view.shown != request and request not in self._waiting:
                self._waiting.append(request)
            return

        async def decided(decision: ApprovalDecision | None) -> None:
            self._show_next()
            if decision is not None:
                await self._decide(decision_command(request, decision))

        self._view.open(request, decided)

    def settle(self, event: HeldDecision) -> None:
        """Stop asking about the request ``event`` settled, closing it if it's shown."""
        key = decided_key(event)
        self._waiting = [request for request in self._waiting if held_key(request) != key]
        shown = self._view.shown
        if shown is not None and held_key(shown) == key:
            self._view.close()
            self._show_next()

    def follow(self, requests: tuple[HeldRequest, ...]) -> None:
        """Ask about ``requests``, those held now, alone: close the one shown unless it's among
        them, and wait on the rest; with none shown, ask the first."""
        shown = self._view.shown
        if shown is not None and shown not in requests:
            self._view.close()
        self._waiting = []
        for request in requests:
            self.show(request)

    def _show_next(self) -> None:
        """Ask the first request waiting, if any."""
        if self._waiting:
            self.show(self._waiting.pop(0))
