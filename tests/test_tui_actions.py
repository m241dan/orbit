"""The TUI's actions as traced: each user action that sends commands is one ``orbit.tui.action``
span named for it, whose trace context every command it sends carries; the commands the TUI
sends on its own, and actions sending nothing, start none."""

from collections.abc import Awaitable, Callable, Sequence

import pytest
from opentelemetry.sdk.trace import ReadableSpan

from orbit.protocol import (
    CommandEnvelope,
    DecideProposal,
    EventEnvelope,
    Notice,
    SessionRef,
    Settings,
    SettingsChanged,
    TasksChanged,
    TaskSpec,
)
from orbit.tracing import Telemetry
from orbit.tui import Opening, OrbitApp
from orbit.tui.view.actions import ACTION_SPAN
from orbit.tui.view.frontend import Frontend
from orbit.tui.view.workspaces import CANCEL_INDEX
from tests.frontend_fakes import Shell
from tests.protocol_examples import PROPOSAL
from tests.session_apps import CATALOG, CURRENT, OLD, SWITCH_TO_OLD
from tests.telemetry_helpers import Traced, traceparent_of
from tests.tui_fakes import FakeSession, changes, feed_events, opened, task

Act = Callable[[Telemetry], Awaitable[Shell]]
"""Does a user action on a frontend traced with the telemetry given; returns the frontend."""


def action_of(span: ReadableSpan) -> object:
    return (span.attributes or {}).get("orbit.action")


def carried(sent: Sequence[CommandEnvelope], span: ReadableSpan) -> list[str]:
    """The types of the commands among those ``sent`` whose envelopes carry ``span``'s
    context."""
    traceparent = traceparent_of(span)
    return [envelope.payload.type for envelope in sent if envelope.traceparent == traceparent]


# The app, as the user drives it.


async def test_ctrl_r_sends_run_tasks_carrying_the_run_tasks_action_span() -> None:
    traced, session = Traced(), FakeSession()
    app = OrbitApp(session, telemetry=traced.telemetry)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+r")
        await pilot.pause()
    [span] = traced.spans(ACTION_SPAN)
    assert action_of(span) == "run_tasks"
    assert [(e.payload.type, e.traceparent) for e in session.envelopes] == [
        ("run_tasks", traceparent_of(span))
    ]


async def test_submitting_a_prompt_is_one_submit_prompt_span() -> None:
    traced, session = Traced(), FakeSession()
    app = OrbitApp(session, telemetry=traced.telemetry)
    async with app.run_test() as pilot:
        await pilot.press("i", *"hi", "enter")
        await pilot.pause()
    [span] = traced.spans(ACTION_SPAN)
    assert action_of(span) == "submit_prompt"
    assert carried(session.envelopes, span) == ["send_message"]


async def test_picking_a_session_is_one_pick_session_span_both_its_commands_carry() -> None:
    traced, session = Traced(), FakeSession()
    app = OrbitApp(session, opening=Opening(pick=True), telemetry=traced.telemetry)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("j", "k", "enter")
        await pilot.pause()
        assert changes(session) == SWITCH_TO_OLD
    [span] = traced.spans(ACTION_SPAN)
    assert action_of(span) == "pick_session"
    assert carried(session.envelopes, span) == ["attach", "activate_session"]


async def test_toggling_the_sidebar_starts_no_span() -> None:
    traced = Traced()
    app = OrbitApp(FakeSession(), telemetry=traced.telemetry)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+b")
        await pilot.pause()
    assert traced.spans(ACTION_SPAN) == []


async def test_the_hello_and_the_opening_attach_carry_no_trace_context() -> None:
    traced, session = Traced(), FakeSession()
    app = OrbitApp(session, telemetry=traced.telemetry)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
    assert [(e.payload.type, e.traceparent) for e in session.sent] == [
        ("hello", None),
        ("attach", None),
    ]
    assert traced.spans(ACTION_SPAN) == []


async def test_without_telemetry_an_action_sends_its_command_with_no_trace_context() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+r")
        await pilot.pause()
    assert [(e.payload.type, e.traceparent, e.tracestate) for e in session.envelopes] == [
        ("run_tasks", None, None)
    ]


# Each action, on the frontend the app drives.


def calling(call: Callable[[Frontend], Awaitable[object]]) -> Act:
    """The action of ``call`` on the frontend."""

    async def act(telemetry: Telemetry) -> Shell:
        shell = Shell(telemetry=telemetry)
        await call(shell.frontend)
        return shell

    return act


async def _confirmed(shell: Shell) -> None:
    """Answer yes to the question asked last."""
    *_, then = shell.app.questions[-1]
    await then()


async def reset_planner(telemetry: Telemetry) -> Shell:
    shell = Shell(telemetry=telemetry)
    shell.frontend.reset_planner()
    await _confirmed(shell)
    return shell


async def configure_checks(telemetry: Telemetry) -> Shell:
    shell = Shell(telemetry=telemetry)
    shell.frontend.configure_checks()
    await _confirmed(shell)
    return shell


async def _settings_shown(telemetry: Telemetry) -> Shell:
    shell = Shell(telemetry=telemetry)
    await shell.feed(SettingsChanged(settings=Settings(), model_choices=("opus",)))
    return shell


async def pick_model(telemetry: Telemetry) -> Shell:
    shell = await _settings_shown(telemetry)
    shell.frontend.pick_model()
    await shell.pickers.pick(1)
    return shell


async def pick_tool_access(telemetry: Telemetry) -> Shell:
    shell = await _settings_shown(telemetry)
    shell.frontend.pick_tool_access()
    await shell.pickers.pick(2)
    return shell


async def submit_prompt(telemetry: Telemetry) -> Shell:
    shell = Shell(telemetry=telemetry)
    await shell.submit("Add a flag.")
    return shell


async def add_task(telemetry: Telemetry) -> Shell:
    shell = Shell(telemetry=telemetry)
    shell.frontend.edit_task(None)
    *_, saved = shell.app.editors[-1]
    await saved(TaskSpec(prompt="Add a flag."))
    return shell


async def edit_task(telemetry: Telemetry) -> Shell:
    shell = Shell(telemetry=telemetry)
    pending = task("1")
    await shell.feed(TasksChanged(tasks=(pending,)))
    shell.frontend.edit_task(pending)
    *_, saved = shell.app.editors[-1]
    await saved(TaskSpec(prompt="Add a flag."))
    return shell


async def unsaved_task(telemetry: Telemetry) -> Shell:
    shell = Shell(telemetry=telemetry)
    shell.frontend.edit_task(None)
    *_, saved = shell.app.editors[-1]
    await saved(None)
    return shell


async def decide_proposal(telemetry: Telemetry) -> Shell:
    shell = Shell(telemetry=telemetry)
    decision = DecideProposal(task_id=PROPOSAL.task_id, action="accept")
    await shell.frontend.decide(PROPOSAL, decision)
    return shell


async def page_history(telemetry: Telemetry) -> Shell:
    shell = Shell(telemetry=telemetry)
    logged = EventEnvelope(seq=501, session=OLD.id, payload=Notice(message="hi"))
    await shell.feed(opened(OLD).model_copy(update={"transcript": (logged,)}))
    await shell.frontend.page_back()
    return shell


async def _sessions_listed(telemetry: Telemetry) -> Shell:
    """A frontend in process on the workspace, with its session picker shown."""
    shell = Shell(telemetry=telemetry)
    await shell.feed(CATALOG)
    shell.frontend.list_sessions()
    await shell.settle()
    return shell


async def pick_session(telemetry: Telemetry) -> Shell:
    shell = await _sessions_listed(telemetry)
    picker = shell.modals.sessions
    assert picker is not None
    picker.choose(1)
    await shell.settle()
    return shell


async def rename_session(telemetry: Telemetry) -> Shell:
    shell = await _sessions_listed(telemetry)
    picker = shell.modals.sessions
    assert picker is not None
    picker.rename()
    await picker.named("flags")
    return shell


async def delete_session(telemetry: Telemetry) -> Shell:
    shell = await _sessions_listed(telemetry)
    picker, view = shell.modals.sessions, shell.modals.sessions_view
    assert picker is not None and view is not None
    view.highlighted = 1  # Not the active session, so deleting it asks first.
    await picker.delete()
    *_, then = shell.modals.confirms[-1]
    await then()
    return shell


async def delete_active_session(telemetry: Telemetry) -> Shell:
    shell = await _sessions_listed(telemetry)
    picker = shell.modals.sessions
    assert picker is not None
    await picker.delete()  # The active session: sent without asking, for the backend to refuse.
    return shell


async def activate_session(telemetry: Telemetry) -> Shell:
    shell = Shell(telemetry=telemetry)
    active = SessionRef(id=CURRENT.id, name=CURRENT.name)
    viewed = opened(OLD).model_copy(update={"active": False, "open_session": active})
    await shell.feed(CATALOG, viewed)
    shell.frontend.navigation.place.offer()
    await shell.modals.answer_offer(0)
    return shell


async def _workspaces_listed(telemetry: Telemetry) -> Shell:
    """A frontend linked to a control plane, with the workspace picker it opens on shown."""
    shell = Shell(linked=True, telemetry=telemetry)
    await shell.feed(CATALOG)
    return shell


async def pick_workspace(telemetry: Telemetry) -> Shell:
    shell = await _workspaces_listed(telemetry)
    picker = shell.modals.workspaces
    assert picker is not None
    await picker.choose(0)
    await shell.settle()
    return shell


async def add_workspace(telemetry: Telemetry) -> Shell:
    shell = await _workspaces_listed(telemetry)
    picker = shell.modals.workspaces
    assert picker is not None
    await picker.typed("/src/flags")
    return shell


async def remove_workspace(telemetry: Telemetry) -> Shell:
    shell = await _workspaces_listed(telemetry)
    picker = shell.modals.workspaces
    assert picker is not None
    picker.remove()
    _, answered = shell.modals.questions[-1]
    await answered(0)
    return shell


async def cancel_removing_a_workspace(telemetry: Telemetry) -> Shell:
    shell = await _workspaces_listed(telemetry)
    picker = shell.modals.workspaces
    assert picker is not None
    picker.remove()
    _, answered = shell.modals.questions[-1]
    await answered(CANCEL_INDEX)
    return shell


async def _exploring(telemetry: Telemetry) -> Shell:
    """A frontend linked to a control plane, with the workspace picker's explorer open."""
    shell = await _workspaces_listed(telemetry)
    picker = shell.modals.workspaces
    assert picker is not None
    await picker.choose(len(picker.rows) - 1)  # Add a path or URL…
    return shell


async def browse(telemetry: Telemetry) -> Shell:
    shell = await _exploring(telemetry)
    await shell.settle()  # The explorer lists where the server starts.
    return shell


async def complete_a_path(telemetry: Telemetry) -> Shell:
    shell = await _exploring(telemetry)
    explorer, view = shell.modals.explorer, shell.modals.explorer_view
    assert explorer is not None and view is not None
    view.field = "/src/fl"
    await explorer.complete()
    return shell


async def add_a_typed_path(telemetry: Telemetry) -> Shell:
    shell = await _exploring(telemetry)
    explorer = shell.modals.explorer
    assert explorer is not None
    await explorer.submit("/src/flags")
    return shell


async def resume_storage(telemetry: Telemetry) -> Shell:
    shell = Shell(telemetry=telemetry)
    await shell.parts.halt.retry()
    return shell


async def take_ownership(telemetry: Telemetry) -> Shell:
    shell = Shell(telemetry=telemetry)
    shell.parts.takeover.offered()
    await shell.offer_view.answer(True)
    return shell


ACTIONS: list[tuple[str, Act, list[str]]] = [
    ("run_tasks", calling(Frontend.run_tasks), ["run_tasks"]),
    ("cancel_turn", calling(Frontend.cancel_turn), ["cancel_turn"]),
    ("stop_planner", calling(Frontend.stop_planner), ["cancel_turn"]),
    ("reset_planner", reset_planner, ["reset_conversation"]),
    ("super_review", calling(Frontend.super_review), ["fire_trigger"]),
    ("configure_checks", configure_checks, ["fire_trigger"]),
    ("pick_model", pick_model, ["update_settings"]),
    ("pick_tool_access", pick_tool_access, ["update_settings"]),
    ("cancel_agent", calling(lambda frontend: frontend.cancel_agent("a1")), ["cancel_agent"]),
    ("submit_prompt", submit_prompt, ["send_message"]),
    ("add_task", add_task, ["add_task"]),
    ("edit_task", edit_task, ["edit_task"]),
    ("accept_task", calling(lambda frontend: frontend.accept_task("1")), ["accept_task"]),
    ("remove_task", calling(lambda frontend: frontend.remove_task("1")), ["remove_task"]),
    ("decide_proposal", decide_proposal, ["decide_proposal"]),
    ("pick_session", pick_session, ["attach", "activate_session"]),
    ("rename_session", rename_session, ["rename_session"]),
    ("delete_session", delete_session, ["delete_session"]),
    ("delete_session", delete_active_session, ["delete_session"]),
    ("activate_session", activate_session, ["activate_session"]),
    ("pick_workspace", pick_workspace, ["attach"]),
    ("add_workspace", add_workspace, ["add_workspace"]),
    ("add_workspace", add_a_typed_path, ["add_workspace"]),
    ("remove_workspace", remove_workspace, ["remove_workspace"]),
    ("browse", browse, ["list_directory"]),
    ("browse", complete_a_path, ["list_directory"]),
    ("page_history", page_history, ["history"]),
    ("resume_storage", resume_storage, ["resume_storage"]),
    ("take_ownership", take_ownership, ["take_ownership"]),
]
"""Each action, how it's done, and the types of the commands it sends."""


@pytest.mark.parametrize(("name", "act", "sent"), ACTIONS)
async def test_an_action_is_one_span_named_for_it_that_only_its_commands_carry(
    name: str, act: Act, sent: list[str]
) -> None:
    traced = Traced()
    shell = await act(traced.telemetry)
    [span] = traced.spans(ACTION_SPAN)
    assert action_of(span) == name
    assert carried(shell.wire.sent, span) == sent
    assert {e.traceparent for e in shell.wire.sent} <= {None, traceparent_of(span)}


@pytest.mark.parametrize("act", [unsaved_task, cancel_removing_a_workspace])
async def test_an_action_that_sends_nothing_starts_no_span(act: Act) -> None:
    traced = Traced()
    await act(traced.telemetry)
    assert traced.spans(ACTION_SPAN) == []
