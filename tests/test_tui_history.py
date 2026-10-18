"""The TUI paging the conversation back through the open session's history."""

from textual.pilot import Pilot
from textual.widgets import Static

from orbit.protocol import (
    PLANNER,
    PLANNER_LANE,
    WORK_LANE,
    AgentStarted,
    AssistantMessage,
    ConversationReset,
    Event,
    EventEnvelope,
    History,
    HistoryPage,
    Notice,
    SessionSummary,
    Settings,
    Snapshot,
    TaskSpec,
    TaskStarted,
    TaskState,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    Usage,
)
from orbit.tui import OrbitApp
from orbit.tui.tasks import TaskList
from orbit.tui.transcript import Transcript
from orbit.tui.transcript_log import TranscriptLog
from orbit.tui.view.transcript import activity_entry
from tests.test_transcript import visible_log
from tests.test_tui import FakeSession, feed_events, tab_strip


def logged(first: int, *events: Event) -> tuple[EventEnvelope, ...]:
    """``events`` as the session ``s1`` logged them, numbered from ``first``."""
    return tuple(
        EventEnvelope(seq=seq, session="s1", payload=event)
        for seq, event in enumerate(events, start=first)
    )


def notices(*messages: str) -> tuple[Notice, ...]:
    return tuple(Notice(message=message) for message in messages)


def shown(message: str) -> str:
    """How the conversation shows a notice of ``message``."""
    return activity_entry(Notice(message=message))[0]


def snapshot(*transcript: EventEnvelope, session: str = "s1") -> Snapshot:
    return Snapshot(
        session=SessionSummary(id=session, name="auth rework"),
        settings=Settings(),
        model_choices=(),
        busy={WORK_LANE: False, PLANNER_LANE: False},
        tasks=(TaskState(id="1", task=TaskSpec(prompt="Add a flag.", role="coder")),),
        steps=(),
        review=None,
        proposals=(),
        agents=(),
        transcript=transcript,
    )


def page(
    *events: EventEnvelope, more: bool = False, session: str = "s1", trimmed: bool = False
) -> HistoryPage:
    return HistoryPage(session=session, events=events, more=more, trimmed=trimmed)


def texts(app: OrbitApp, key: str | None = None) -> list[str]:
    """The texts of the entries under the tab keyed ``key``; None is All."""
    return [entry.text for entry in app.query_one(Transcript).entries(key)]


def cursor_text(app: OrbitApp) -> str | None:
    """The text of the entry the shown log's cursor is on, if it has one."""
    log = visible_log(app)
    index = log.inspected
    return None if index is None else log.entries[index].text


def histories(session: FakeSession) -> list[History]:
    return [command for command in session.commands if isinstance(command, History)]


def loading(app: OrbitApp) -> bool:
    return app.query_one(Transcript).loading_earlier


async def at_the_top(pilot: Pilot[None], entries: int) -> None:
    """Activate the conversation and move its cursor up to its first entry."""
    await pilot.press("k", "enter", *["k"] * (entries - 1))


async def test_moving_up_past_the_top_shows_the_earlier_page_above_keeping_the_cursor() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, snapshot(*logged(501, *notices("c", "d"))))
        await at_the_top(pilot, 2)
        assert cursor_text(app) == shown("c") and histories(session) == []

        await pilot.press("k")
        assert histories(session) == [History(session="s1", before_seq=501)]
        assert loading(app) and cursor_text(app) == shown("c")
        await pilot.press("k")
        assert len(histories(session)) == 1  # One request at a time.

        await feed_events(session, pilot, page(*logged(1, *notices("a", "b")), more=True))
        assert texts(app) == [shown(m) for m in "abcd"]
        assert cursor_text(app) == shown("c") and not loading(app)
        await pilot.press("k")
        assert cursor_text(app) == shown("b") and len(histories(session)) == 1


async def test_with_no_more_earlier_events_moving_up_past_the_top_asks_for_nothing() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, snapshot(*logged(501, *notices("c"))))
        await at_the_top(pilot, 1)
        await pilot.press("k")
        await feed_events(session, pilot, page(*logged(1, *notices("a")), more=False))
        await pilot.press("k", "k")
        assert cursor_text(app) == shown("a")
        assert histories(session) == [History(session="s1", before_seq=501)]
        assert not loading(app)


async def test_the_next_page_is_asked_for_before_the_oldest_shown() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, snapshot(*logged(501, *notices("c"))))
        await at_the_top(pilot, 1)
        await pilot.press("k")
        await feed_events(session, pilot, page(*logged(301, *notices("b")), more=True))
        await pilot.press("k", "k")
        assert [history.before_seq for history in histories(session)] == [501, 301]


async def test_an_empty_transcript_has_no_history_to_ask_for() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, snapshot(), Notice(message="live"))
        await at_the_top(pilot, 1)
        await pilot.press("k")
        assert histories(session) == [] and not loading(app)


async def test_a_page_not_asked_for_is_ignored() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, snapshot(*logged(501, *notices("c"))))
        await feed_events(session, pilot, page(*logged(1, *notices("a"))))
        assert texts(app) == [shown("c")]


async def test_a_snapshot_forgets_the_page_loading() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, snapshot(*logged(501, *notices("c"))))
        await at_the_top(pilot, 1)
        await pilot.press("k")
        other = snapshot(*logged(601, *notices("x")), session="s2")
        await feed_events(session, pilot, other, page(*logged(1, *notices("a"))))
        assert texts(app) == [shown("x")] and not loading(app)


def trimmed_shown(app: OrbitApp) -> bool:
    return app.query_one(Transcript).trimmed_earlier


async def test_the_last_page_of_a_trimmed_log_shows_older_events_were_removed() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, snapshot(*logged(501, *notices("c"))))
        await at_the_top(pilot, 1)
        await pilot.press("k")
        assert not trimmed_shown(app)
        await feed_events(session, pilot, page(*logged(400, *notices("b")), trimmed=True))
        assert trimmed_shown(app) and texts(app) == [shown("b"), shown("c")]
        marker = app.query_one("#trimmed-earlier", Static)
        assert str(marker.render()) == "Older events were removed (session_log_mb)"
        await pilot.press("k", "k")
        assert len(histories(session)) == 1


async def test_a_snapshot_hides_the_removed_events_line() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, snapshot(*logged(501, *notices("c"))))
        await at_the_top(pilot, 1)
        await pilot.press("k")
        await feed_events(session, pilot, page(trimmed=True))
        assert trimmed_shown(app)
        await feed_events(session, pilot, snapshot(*logged(601, *notices("x"))))
        assert not trimmed_shown(app)


async def test_a_request_that_fails_to_send_can_be_made_again() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, snapshot(*logged(501, *notices("c"))))
        await at_the_top(pilot, 1)
        session.send_error = RuntimeError("gone")
        await pilot.press("k")
        assert not loading(app)
        assert "Couldn't send the command: gone" in texts(app)
        session.send_error = None
        await pilot.press("k")
        assert len(histories(session)) == 2 and loading(app)


EARLIER = (
    TaskStarted(task_id="1"),
    AgentStarted(agent_id="agent-0", role="coder", task_id="1"),
    AgentStarted(agent_id="agent-9", role="docs", task_id="1"),
    TurnStarted(turn_id="t0", agent_id="agent-0"),
    TurnFinished(turn_id="t0", agent_id="agent-0", reason="completed", usage=Usage(cost_usd=5)),
    ConversationReset(agent=PLANNER),
    TurnStarted(turn_id="t1", agent_id="agent-0"),
    # Its result is lost: the page ends with the call still running.
    ToolCallStarted(turn_id="t1", agent_id="agent-0", call_id="x", name="Read", arguments={}),
)
LIVE_TURN = logged(
    501,
    AgentStarted(agent_id="agent-1", role="coder", task_id="1"),
    TurnStarted(turn_id="t2", agent_id="agent-1"),
    AssistantMessage(turn_id="t2", agent_id="agent-1", text="Later."),
)


async def paged_back(session: FakeSession, pilot: Pilot[None]) -> None:
    """Show a snapshot of a coder's turn in progress, then page back to ``EARLIER``."""
    await feed_events(session, pilot, snapshot(*LIVE_TURN))
    await at_the_top(pilot, 2)
    await pilot.press("k")
    await feed_events(session, pilot, page(*logged(1, *EARLIER)))


async def test_earlier_events_leave_the_tabs_and_usage_as_they_are() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, snapshot(*LIVE_TURN))
        subtitle, strip = app.query_one(TaskList).border_subtitle, tab_strip(app)
        await paged_back(session, pilot)

        assert tab_strip(app) == strip
        assert app.query_one(TaskList).border_subtitle == subtitle
        assert app.query_one(Transcript).entries(PLANNER)
        assert cursor_text(app) == "▶ agent-1 (coder) started"


async def test_earlier_events_go_under_their_pinned_tabs_their_calls_ended() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await paged_back(session, pilot)

        coder = app.query_one(Transcript).entries("coder")
        assert coder[-1].text == "Later."
        [call] = [entry.call for entry in coder if entry.call is not None]
        assert call.status != "running"
        assert not any(log.running for log in app.query(TranscriptLog))


async def test_the_turn_running_carries_on_after_earlier_events() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await paged_back(session, pilot)
        finished = TurnFinished(turn_id="t2", agent_id="agent-1", reason="completed")
        await feed_events(session, pilot, finished)

        later = app.query_one(Transcript).entries("coder")[-2]
        assert later.text == "Later." and later.turn is None  # No longer thinking.
