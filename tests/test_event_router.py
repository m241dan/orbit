"""The router taking each event to the widgets and hosts that show it, in order."""

from unittest.mock import Mock

from orbit.protocol import (
    STORAGE_FAILED,
    Catalog,
    CommandRefused,
    ErrorOccurred,
    Event,
    SessionStateChanged,
)
from orbit.tui.event_router import AppHooks, EventRouter, Handlers, Panes
from tests.tui_fakes import SNAPSHOT

# What every event reaches first, and what an event outside the conversation reaches last.
TRACKED = ["agents.track", "tabs.track", "transcript.track", "proposals.withdraw"]


def routed(event: Event) -> list[str]:
    """The names of the methods ``event`` calls on the router's widgets, hosts and hooks, in
    the order it calls them."""
    calls = Mock()
    panes = Panes(
        calls.transcript,
        calls.tabs,
        calls.prompt,
        calls.viewing,
        calls.task_list,
        calls.verification,
        calls.usage,
        calls.agents,
    )
    handlers = Handlers(
        calls.feed,
        calls.pager,
        calls.desk,
        calls.proposals,
        calls.pickers,
        calls.catalog,
        calls.sessions,
        calls.workspaces,
        calls.opening,
        calls.halt,
        calls.takeover,
    )
    hooks = AppHooks(calls.label_triggers, calls.leave_closed_detail, calls.unfocus_task_list)
    EventRouter(panes, handlers, hooks).render(event)
    return [str(name) for name, _, _ in calls.mock_calls]


def test_a_snapshot_shows_its_tasks_before_its_transcript_and_its_usage_after() -> None:
    assert routed(SNAPSHOT) == [
        "agents.track",
        "tabs.track",
        "proposals.withdraw",
        "pickers.show_choices",
        "prompt.show_settings",
        "prompt.show_busy",
        "tabs.set_planner_busy",
        "task_list.show",
        "agents.number_tasks",
        "verification.show",
        "agents.show_review",
        "proposals.follow",
        "feed.replace",
        "leave_closed_detail",
        "transcript.track",
        "pager.follow",
        "transcript.show_session",
        "unfocus_task_list",
        "task_list.let_go",
        "task_list.reset_usage",
        "sessions.view",
        "viewing.show",
        "task_list.show_waiting",
        "prompt.show_read_only",
        "takeover.follow",
        "halt.lifted",
    ]


def test_a_catalog_labels_the_triggers_then_follows_workspaces_and_sessions() -> None:
    catalog = Catalog(workspaces=(), agents=(), triggers=(), lanes=())

    assert routed(catalog) == [
        *TRACKED,
        "label_triggers",
        "catalog.follow",
        "workspaces.follow",
        "sessions.follow",
        "transcript.show_session",
        "viewing.show",
        "task_list.show_waiting",
        "opening.catalog_shown",
    ]


def test_a_sessions_state_reaches_the_catalog_then_the_pickers_and_the_viewing_mark() -> None:
    state = SessionStateChanged(session="s1", active=True, state="running", viewers=1)

    assert routed(state) == [
        *TRACKED,
        "catalog.follow_state",
        "sessions.follow_state",
        "workspaces.follow",
        "viewing.show",
        "task_list.show_waiting",
    ]


def test_a_storage_error_shows_in_the_feed_then_halts_then_reaches_the_workspaces() -> None:
    error = ErrorOccurred(code=STORAGE_FAILED, message="The disk is full.")

    assert routed(error) == [*TRACKED, "feed.render", "halt.halted", "workspaces.follow_error"]


def test_a_command_refused_for_storage_is_answered_then_halts() -> None:
    refusal = CommandRefused(command_id="c", code=STORAGE_FAILED, message="The disk is full.")

    assert routed(refusal) == [*TRACKED, "desk.answer", "halt.halted"]


def test_a_refusal_or_an_error_for_another_reason_halts_nothing() -> None:
    refusal = CommandRefused(command_id="c", code="busy", message="Busy")
    error = ErrorOccurred(code="agent_failed", message="x")

    assert routed(refusal) == [*TRACKED, "desk.answer"]
    assert routed(error) == [*TRACKED, "feed.render", "workspaces.follow_error"]
