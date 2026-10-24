"""The router taking each event to the widgets and hosts that show it, in order."""

from unittest.mock import Mock

from orbit.protocol import (
    STORAGE_FAILED,
    AgentsChanged,
    Catalog,
    CommandRefused,
    DirectoryListing,
    ErrorOccurred,
    Event,
    OwnershipDeclined,
    OwnershipOffered,
    OwnershipTaken,
    SessionStateChanged,
    StepState,
    Usage,
    UsageTotal,
    UsageTotals,
    VerificationChanged,
)
from orbit.tui.view.router import AppHooks, EventRouter, Handlers, Panes
from tests.tui_fakes import SNAPSHOT

# What every event reaches first, and what an event outside the conversation reaches last.
TRACKED = ["agents.track", "tabs.track", "transcript.track", "proposals.withdraw"]


def routed(event: Event) -> list[str]:
    """The names of the methods ``event`` calls on the router's widgets, hosts and hooks, in
    the order it calls them."""
    return [str(name) for name, _, _ in calls_of(event).mock_calls]


def calls_of(event: Event) -> Mock:
    """What ``event`` calls on the router's widgets, hosts and hooks, recorded."""
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
        calls.navigation,
        calls.halt,
        calls.takeover,
    )
    hooks = AppHooks(calls.label_triggers, calls.leave_closed_detail, calls.unfocus_task_list)
    EventRouter(panes, handlers, hooks).render(event)
    return calls


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
        "navigation.view",
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
        "navigation.follow_catalog",
        "transcript.show_session",
        "viewing.show",
        "task_list.show_waiting",
    ]


def test_a_sessions_state_reaches_the_catalog_then_the_pickers_and_the_viewing_mark() -> None:
    state = SessionStateChanged(session="s1", active=True, state="running", viewers=1)

    assert routed(state) == [
        *TRACKED,
        "navigation.follow_state",
        "viewing.show",
        "task_list.show_waiting",
    ]


def test_a_storage_error_shows_in_the_feed_then_halts_then_reaches_the_workspaces() -> None:
    error = ErrorOccurred(code=STORAGE_FAILED, message="The disk is full.")

    assert routed(error) == [*TRACKED, "feed.render", "halt.halted", "navigation.follow_error"]


def test_a_command_refused_for_storage_is_answered_then_halts() -> None:
    refusal = CommandRefused(command_id="c", code=STORAGE_FAILED, message="The disk is full.")

    assert routed(refusal) == [*TRACKED, "desk.answer", "halt.halted"]


def test_a_refusal_or_an_error_for_another_reason_halts_nothing() -> None:
    refusal = CommandRefused(command_id="c", code="busy", message="Busy")
    error = ErrorOccurred(code="agent_failed", message="x")

    assert routed(refusal) == [*TRACKED, "desk.answer"]
    assert routed(error) == [*TRACKED, "feed.render", "navigation.follow_error"]


def test_an_offer_asks_about_the_repository() -> None:
    assert routed(OwnershipOffered()) == [*TRACKED, "takeover.offered"]


def test_an_offer_settled_closes_it_and_owning_the_repository_clears_the_read_only_mark() -> None:
    assert routed(OwnershipDeclined()) == [*TRACKED, "takeover.settled"]
    assert routed(OwnershipTaken()) == [*TRACKED, "takeover.settled", "prompt.show_read_only"]


def test_the_streams_agents_and_listings_reach_the_navigation_and_its_totals_nothing() -> None:
    agents = AgentsChanged(agents=())
    listing = DirectoryListing(path="/srv", entries=(), total=0)
    totals = UsageTotals(workspaces=(), total=UsageTotal(today=Usage(), lifetime=Usage()))

    assert routed(agents) == [*TRACKED, "navigation.follow_agents"]
    assert routed(listing) == [*TRACKED, "navigation.follow_listing"]
    assert routed(totals) == TRACKED


def test_verification_steps_reach_their_panel_with_what_they_check() -> None:
    steps = (StepState(name="Lint", status="running"),)
    calls = calls_of(VerificationChanged(steps=steps, title="Startup check"))

    calls.verification.show.assert_called_once_with(steps, "Startup check")
