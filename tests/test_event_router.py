"""The router taking each event to the widgets and hosts that show it, in order."""

from datetime import UTC, datetime
from unittest.mock import Mock

from orbit.protocol import (
    CODER,
    PLANNER,
    REVIEWER,
    STORAGE_FAILED,
    AgentEntry,
    AgentsChanged,
    ApprovalDecided,
    ApprovalRequested,
    Catalog,
    CommandRefused,
    ConsultationAnswered,
    ConsultationAsked,
    DirectoryListing,
    ErrorOccurred,
    Event,
    OwnershipDeclined,
    OwnershipOffered,
    OwnershipTaken,
    ReportShown,
    ReportsListed,
    ReportSummary,
    SessionStateChanged,
    StepState,
    ToolCallDecided,
    ToolCallHeld,
    TriggerEntry,
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
    the order it calls them, but the usage corner, which every event reaches."""
    calls = calls_of(event).mock_calls
    skipped = {"navigation.catalog.agents", "navigation.catalog.mapped_skills", "corner.track"}
    return [str(name) for name, _, _ in calls if name not in skipped]


def calls_of(event: Event) -> Mock:
    """What ``event`` calls on the router's widgets, hosts and hooks, recorded."""
    calls = Mock()
    calls.navigation.catalog.agents.return_value = ()
    calls.navigation.catalog.mapped_skills.return_value = ()
    router_of(calls).render(event)
    return calls


def router_of(calls: Mock) -> EventRouter:
    """A router whose widgets, hosts and hooks are ``calls``' children."""
    panes = Panes(
        calls.transcript,
        calls.tabs,
        calls.prompt,
        calls.viewing,
        calls.paused,
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
        calls.approvals,
        calls.pickers,
        calls.navigation,
        calls.halt,
        calls.takeover,
        calls.reports,
        calls.corner,
    )
    hooks = AppHooks(
        calls.label_triggers,
        calls.leave_closed_detail,
        calls.unfocus_task_list,
        calls.refresh_keys,
    )
    return EventRouter(panes, handlers, hooks)


def test_the_usage_corner_follows_an_event_once_the_feed_has_counted_it() -> None:
    event = ErrorOccurred(code="x", message="boom")
    calls = calls_of(event)
    names = [str(name) for name, _, _ in calls.mock_calls]

    assert names.index("feed.render") < names.index("corner.track")
    calls.corner.track.assert_called_once_with(event)


def test_a_snapshot_shows_its_tasks_before_its_transcript_and_its_usage_after() -> None:
    assert routed(SNAPSHOT) == [
        "agents.track",
        "tabs.track",
        "proposals.withdraw",
        "pickers.show_choices",
        "prompt.show_target",
        "prompt.show_busy",
        "prompt.show_settings",
        "tabs.set_busy",
        "tabs.set_busy",
        "prompt.show_busy",
        "task_list.show",
        "agents.number_tasks",
        "task_list.show_merge_holds",
        "refresh_keys",
        "verification.show",
        "agents.show_review",
        "proposals.follow",
        "approvals.follow",
        "feed.replace",
        "leave_closed_detail",
        "transcript.track",
        "pager.follow",
        "transcript.show_session",
        "unfocus_task_list",
        "task_list.widen",
        "task_list.let_go",
        "task_list.reset_usage",
        "navigation.view",
        "viewing.show",
        "paused.show",
        "task_list.show_waiting",
        "prompt.show_read_only",
        "takeover.follow",
        "halt.lifted",
    ]


def test_a_catalog_labels_triggers_pins_conversations_tags_lanes_then_follows_workspaces() -> None:
    catalog = Catalog(workspaces=(), agents=(), triggers=(), lanes=())

    assert routed(catalog) == [
        *TRACKED,
        "label_triggers",
        "tabs.group",
        "navigation.follow_catalog",
        "transcript.show_session",
        "viewing.show",
        "paused.show",
        "task_list.show_waiting",
    ]


def test_a_catalogs_triggers_group_the_tabs() -> None:
    triggers = (TriggerEntry(name="bugs-review", label="b", manual=False, tab="review"),)
    catalog = Catalog(workspaces=(), agents=(), triggers=triggers, lanes=())
    calls_of(catalog).tabs.group.assert_called_once_with(triggers)


def test_a_sessions_state_reaches_the_catalog_then_the_pickers_and_the_prompt_lines() -> None:
    state = SessionStateChanged(session="s1", active=True, state="running", viewers=1)

    assert routed(state) == [
        *TRACKED,
        "navigation.follow_state",
        "viewing.show",
        "paused.show",
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


def test_consultations_reach_the_feed() -> None:
    asked = ConsultationAsked(
        consultation=1, asker="planner", consultant="experimenter", title="Check X", position=1
    )
    answered = ConsultationAnswered(
        consultation=1, asker="planner", consultant="experimenter", title="Check X", reply="3"
    )

    assert routed(asked) == [*TRACKED, "feed.render"]
    assert routed(answered) == [*TRACKED, "feed.render"]


CONSULTATION = ApprovalRequested(
    consultation=7, asker=PLANNER, consultant="experimenter", title="Check X", asked=6
)
CALL = ToolCallHeld(
    call=3,
    agent_id="agent-1",
    role="coder",
    server="github",
    tool="github__create_issue",
    arguments={"title": "Flaky test"},
)


def test_consultations_and_calls_held_and_settled_reach_the_approvals() -> None:
    assert routed(CONSULTATION) == [*TRACKED, "approvals.show"]
    assert routed(CALL) == [*TRACKED, "approvals.show"]
    assert routed(ApprovalDecided(consultation=7, decision="deny")) == [
        *TRACKED,
        "approvals.settle",
    ]
    assert routed(ToolCallDecided(call=3, decision="withdrawn")) == [*TRACKED, "approvals.settle"]


def test_a_snapshot_asks_about_the_consultations_then_the_calls_held() -> None:
    snapshot = SNAPSHOT.model_copy(update={"approvals": (CONSULTATION,), "tool_calls": (CALL,)})
    calls_of(snapshot).approvals.follow.assert_called_once_with((CONSULTATION, CALL))


def test_reports_reach_the_reports_list() -> None:
    when = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    listed = ReportsListed(
        reports=(ReportSummary(id=1, title="Timings", author="planner", time=when),)
    )
    shown = ReportShown(id=1, title="Timings", body="3s", author="planner", time=when)

    assert routed(listed) == [*TRACKED, "reports.follow"]
    assert routed(shown) == [*TRACKED, "reports.follow"]


def test_verification_steps_reach_their_panel_with_what_they_check() -> None:
    steps = (StepState(name="Lint", status="running"),)
    calls = calls_of(VerificationChanged(steps=steps, title="mutation", output="3 survived"))

    calls.verification.show.assert_called_once_with(steps, "mutation", "3 survived")


def test_a_catalog_changing_the_mapped_skills_shows_them_on_the_tasks_and_one_alike_doesnt() -> (
    None
):
    calls = Mock()
    calls.navigation.catalog.agents.return_value = ()
    calls.navigation.catalog.mapped_skills.return_value = ("python", "layers")
    router = router_of(calls)
    catalog = Catalog(workspaces=(), agents=(), triggers=(), lanes=())
    router.render(catalog)
    router.render(catalog)
    calls.task_list.show_mapped_skills.assert_called_once_with(("python", "layers"))


def test_a_catalog_changing_the_roles_tags_the_tasks_with_them_and_one_alike_doesnt() -> None:
    calls = Mock()
    calls.navigation.catalog.agents.return_value = (
        AgentEntry(name=CODER, label="Coder", conversational=False, skills=("code-style",)),
        AgentEntry(
            name=REVIEWER,
            label="Reviewer",
            conversational=False,
            lane="review",
            usage_known=False,
            skills=("security", "python"),
        ),
    )
    calls.navigation.catalog.mapped_skills.return_value = ()
    router = router_of(calls)
    catalog = Catalog(workspaces=(), agents=(), triggers=(), lanes=())
    router.render(catalog)
    router.render(catalog)
    calls.task_list.show_role_skills.assert_called_once_with(
        {CODER: ("code-style",), REVIEWER: ("security", "python")}
    )
    calls.task_list.show_lanes.assert_called_once_with({CODER: "work", REVIEWER: "review"})
    calls.task_list.show_usage_unknown.assert_called_once_with(frozenset({REVIEWER}))
