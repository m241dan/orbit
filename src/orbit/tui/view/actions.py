"""The user's actions, as traced: each that sends commands is one ``orbit.tui.action`` span,
started as it sends the first, whose trace context every command it sends carries; the commands
the TUI sends on its own carry none."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from orbit.protocol import CommandEnvelope
from orbit.tracing import SpanHandle, Telemetry
from orbit.tui.view.commands import Send

ACTION_SPAN = "orbit.tui.action"
"""The span each user action sending commands is traced in."""

type ActionName = Literal[
    "run_tasks",
    "cancel_turn",
    "stop_planner",
    "reset_planner",
    "super_review",
    "configure_checks",
    "pick_model",
    "pick_tool_access",
    "pick_effort",
    "cancel_agent",
    "submit_prompt",
    "add_task",
    "edit_task",
    "accept_task",
    "remove_task",
    "decide_proposal",
    "pick_session",
    "rename_session",
    "delete_session",
    "activate_session",
    "pick_workspace",
    "add_workspace",
    "remove_workspace",
    "browse",
    "page_history",
    "resume_storage",
    "take_ownership",
]
"""What ``orbit.action`` names a user action: the TUI's own names, not its commands' types."""


class _Action:
    """The user's action ``name`` under way: its span, once it has sent a command, and whether
    it's over."""

    def __init__(self, name: ActionName) -> None:
        self.name = name
        self.span: SpanHandle | None = None
        self.over = False


_current: ContextVar[_Action | None] = ContextVar("orbit_tui_action", default=None)


@contextmanager
def action(name: ActionName) -> Iterator[None]:
    """Run the block as the user's action ``name``: the commands it sends are traced in one
    span, ended on leaving the block, an error escaping it recorded on it; a block sending none
    starts none."""
    current = _Action(name)
    token = _current.set(current)
    try:
        yield
    except Exception as error:
        if current.span is not None:
            current.span.error(str(error), error)
        raise
    finally:
        _current.reset(token)
        current.over = True
        if current.span is not None:
            current.span.end()


def carrying(send: Send, telemetry: Telemetry) -> Send:
    """``send``, each envelope carrying with ``telemetry`` the trace context of the user's
    action it's sent in, as :func:`carried` says."""

    async def traced(envelope: CommandEnvelope) -> None:
        await send(carried(envelope, telemetry))

    return traced


def carried(envelope: CommandEnvelope, telemetry: Telemetry) -> CommandEnvelope:
    """``envelope`` carrying with ``telemetry`` the trace context of the span of the user's
    action under way, started now if it's the first command the action sends; unchanged outside
    an action, or once the action that started what sends it is over."""
    current = _current.get()
    if current is None or current.over:
        return envelope
    if current.span is None:
        current.span = telemetry.start(ACTION_SPAN, {"orbit.action": current.name})
    with current.span.use():
        return telemetry.carry(envelope)
