"""The report tools: what agents whose role grants them save, list and read, per workspace."""

from collections.abc import Callable
from typing import Any

from orbit.agent.application.ports import Reply
from orbit.agent.application.reports import Reports
from orbit.agent.domain.reports import NO_ID, TOO_LARGE
from orbit.protocol import CODER, PLANNER, Event, SendMessage, TurnEndReason, TurnFinished
from tests.agent_fakes import (
    ROLES,
    START,
    Call,
    FakeStore,
    Script,
    Ticks,
    events_until,
    run,
    send,
    session_for,
)


def finished(agent_id: str) -> Callable[[Event], bool]:
    """Stops at agent ``agent_id``'s first TurnFinished."""
    return lambda event: isinstance(event, TurnFinished) and event.agent_id == agent_id


REPORTING_PLANNER = ROLES[PLANNER].model_copy(
    update={"tools": (*ROLES[PLANNER].tools, "save_report", "list_reports", "read_report")}
)
READING_CODER = ROLES[CODER].model_copy(
    update={"tools": (*ROLES[CODER].tools, "list_reports", "read_report")}
)
REPORTING = {**ROLES, PLANNER: REPORTING_PLANNER, CODER: READING_CODER}
"""The built-in roles, with a planner granted the report tools and a coder reading them."""


def calls(heard: list[Reply], *made: tuple[str, dict[str, Any]]) -> Script:
    """A turn making each call in ``made``, noting what it hears in ``heard``."""

    async def turn(call: Call) -> TurnEndReason:
        for tool, arguments in made:
            heard.append(await call(tool, **arguments))
        return "completed"

    return turn


async def planner_hears(store: FakeStore, *made: tuple[str, dict[str, Any]]) -> list[Reply]:
    """What a planner granted the report tools hears making the calls ``made`` in a session
    saved in ``store``."""
    heard: list[Reply] = []
    session, _, launcher = session_for(roles=REPORTING, store=store)
    launcher.planner_turns = [calls(heard, *made)]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, finished("agent-1"))
    await session.close()
    return heard


SAVE = ("save_report", {"title": "Timings", "body": "It took 3s."})


async def test_a_role_given_the_report_tools_saves_lists_and_reads_a_report() -> None:
    store = FakeStore()
    heard = await planner_hears(store, SAVE, ("list_reports", {}), ("read_report", {"id": 1}))
    [saved] = store.reports_kept.values()
    assert heard == [
        Reply("Saved report 1."),
        Reply(f"1. Timings (by the planner, {saved.at:%Y-%m-%d})"),
        Reply("# Timings\n\nIt took 3s."),
    ]
    assert (saved.author, saved.session_id) == (PLANNER, "session-1")


async def test_a_body_over_200_kb_is_refused_and_nothing_saved() -> None:
    store = FakeStore()
    heard = await planner_hears(store, ("save_report", {"title": "Big", "body": "x" * 300_000}))
    assert heard == [Reply(TOO_LARGE, is_error=True)] and store.reports_kept == {}


async def test_a_report_without_a_title_is_refused() -> None:
    heard = await planner_hears(FakeStore(), ("save_report", {"title": " ", "body": "b"}))
    assert heard == [Reply("A report needs a title.", is_error=True)]


async def test_reading_an_unknown_report_is_refused() -> None:
    heard = await planner_hears(FakeStore(), ("read_report", {"id": 4}))
    assert heard == [Reply("There's no report 4.", is_error=True)]


async def test_a_role_without_the_report_tools_is_refused_them() -> None:
    heard: list[Reply] = []
    session, _, launcher = session_for(store=FakeStore())
    launcher.planner_turns = [calls(heard, SAVE)]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, finished("agent-1"))
    await session.close()
    assert heard == [Reply("Your role (planner) can't use save_report.", is_error=True)]


async def test_without_a_store_no_role_has_the_report_tools() -> None:
    heard: list[Reply] = []
    session, _, launcher = session_for(roles=REPORTING)
    launcher.planner_turns = [calls(heard, ("list_reports", {}))]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, finished("agent-1"))
    await session.close()
    assert heard == [Reply("Your role (planner) can't use list_reports.", is_error=True)]


async def test_a_task_role_given_the_reading_tools_reads_the_reports() -> None:
    store = FakeStore()
    await Reports(store, lambda: "s0", Ticks()).tools(PLANNER)[SAVE[0]](SAVE[1])
    heard: list[Reply] = []

    async def reads(call: Call) -> TurnEndReason:
        await call("get_task")
        heard.append(await call("read_report", id="1"))
        heard.append(await call("save_report", title="Mine", body="b"))
        await call("complete_task", summary="done")
        return "completed"

    session, _, _ = session_for(reads, roles=REPORTING, store=store)
    await run(session)
    await session.close()
    assert heard == [
        Reply("# Timings\n\nIt took 3s."),
        Reply("Your role (coder) can't use save_report.", is_error=True),
    ]


async def test_reports_are_saved_at_the_clocks_time_in_the_session_open() -> None:
    store = FakeStore()
    tools = Reports(store, lambda: "s0", Ticks()).tools("experimenter")
    await tools["save_report"]({"title": "  Timings  ", "body": "3s"})
    [saved] = store.reports_kept.values()
    assert (saved.title, saved.author, saved.session_id, saved.at) == (
        "Timings",
        "experimenter",
        "s0",
        START,
    )


async def test_listing_no_reports_says_so() -> None:
    tools = Reports(FakeStore(), lambda: "s0", Ticks()).tools(PLANNER)
    assert await tools["list_reports"]({}) == Reply("There are no reports yet.")


async def test_reading_without_an_id_is_refused() -> None:
    tools = Reports(FakeStore(), lambda: "s0", Ticks()).tools(PLANNER)
    assert await tools["read_report"]({}) == Reply(NO_ID, is_error=True)
