"""The control-plane stream: the connections watching it are told every agent running across
the workspaces served and the usage totals, as they start watching and as those change; the
others are told neither."""

import asyncio
import itertools
from datetime import UTC, datetime

from orbit.agent import Agents, ControlPlane, WorkspaceHost
from orbit.agent.application.connection import SessionConnection
from orbit.agent.application.ports import ReadFailed
from orbit.agent.domain.usage_totals import UsageTotalsData, usage_totals
from orbit.protocol import (
    CONTROL_PLANE_STREAM,
    INTERNAL,
    UNKNOWN,
    AddTask,
    AgentExited,
    AgentRef,
    AgentsChanged,
    AgentStarted,
    CommandAccepted,
    CommandRefused,
    ErrorOccurred,
    Event,
    EventEnvelope,
    RunTasks,
    TaskSpec,
    Usage,
    UsageTotal,
    UsageTotals,
    Watch,
    WorkspaceUsage,
)
from tests.agent_fakes import (
    ROLES,
    FakeHost,
    FakeLedger,
    ScriptedLauncher,
    board_of,
    claims_done,
    settled,
)
from tests.connection_helpers import joined, say, taken
from tests.plane_fakes import idle, payloads, welcomed

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
"""The time the control plane's clock tells."""

MIDNIGHT = datetime(2026, 10, 22, tzinfo=UTC)
"""When the day :data:`NOW` falls in began."""

TURN = Usage(input_tokens=10, output_tokens=20, cost_usd=0.5, turns=1, duration_ms=900)
COUNTED = Usage(input_tokens=10, output_tokens=20, cost_usd=0.5)
"""What a turn costs, and what of it the totals count."""

NO_TOTALS = UsageTotals(workspaces=(), total=UsageTotal(today=Usage(), lifetime=Usage()))
WATCH = Watch(streams=(CONTROL_PLANE_STREAM,))


class TotalingLedger(FakeLedger):
    """A ledger in memory adding up its rows, every one today's, noting each day's start it's
    asked the totals from in ``asked``; while ``gate`` is set, adding up waits for it, and while
    ``unreadable`` is, it fails with it."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[datetime] = []
        self.gate: asyncio.Event | None = None
        self.reading = asyncio.Event()  # Set while adding up waits for the gate.
        self.unreadable: str | None = None

    async def totals(self, today_start: datetime) -> UsageTotalsData:
        self.asked.append(today_start)
        if self.gate is not None:
            self.reading.set()
            await self.gate.wait()
        if self.unreadable is not None:
            raise ReadFailed(self.unreadable)
        today: dict[str, Usage] = {}
        for row in self.rows:
            today[row.workspace_id] = today.get(row.workspace_id, Usage()) + row.usage
        recorded = {(row.workspace_id, row.session_id): row.usage for row in self.rows}
        return usage_totals(today, recorded, {})


def workspace(name: str, ledger: FakeLedger | None = None, *usages: Usage) -> WorkspaceHost:
    """The host of the workspace ``name``, whose tasks' agents claim them done, each turn
    costing the next of ``usages``, recorded in ``ledger``."""
    agent_host = FakeHost()
    launcher = ScriptedLauncher(agent_host, *(claims_done for _ in range(3)))
    launcher.usages = [(usage,) for usage in usages]
    tokens = (f"{name}-token-{n}" for n in itertools.count())
    agents = Agents(agent_host, launcher, lambda: next(tokens), roles=ROLES, exit_grace=0.5)
    return WorkspaceHost(
        agents=agents,
        workspace=f"/src/{name}",
        workspace_id=name,
        new_id=lambda: f"{name}-1",
        ledger=ledger,
    )


def plane_of(*hosts: WorkspaceHost, ledger: FakeLedger | None = None) -> ControlPlane:
    return ControlPlane(hosts, ledger=ledger, clock=lambda: NOW)


async def run_task(connection: SessionConnection) -> None:
    """Have ``connection``'s workspace run a task to its end."""
    await say(connection, "add", AddTask(task=TaskSpec(prompt="Add a flag.")))
    await say(connection, "run", RunTasks())
    await asyncio.wait_for(idle(connection), 5)
    await settled()


def told[T](connection: SessionConnection, kind: type[T]) -> list[T]:
    """The events of type ``kind`` sent to ``connection`` since last taken."""
    return [event for event in payloads(taken(connection)) if isinstance(event, kind)]


def stream_envelopes(envelopes: list[EventEnvelope]) -> list[EventEnvelope]:
    return [e for e in envelopes if isinstance(e.payload, AgentsChanged | UsageTotals)]


async def test_watching_tells_the_agents_running_and_the_totals_at_once() -> None:
    plane = plane_of(workspace("a"))
    connection = await welcomed(plane)

    await say(connection, "watch", WATCH)

    assert payloads(taken(connection)) == [
        AgentsChanged(agents=()),
        NO_TOTALS,
        CommandAccepted(command_id="watch"),
    ]
    await plane.close()


async def test_the_streams_events_belong_to_no_workspace_and_no_session() -> None:
    a = workspace("a")
    plane = plane_of(a)
    connection = await joined(plane, host=a)
    taken(connection)

    await say(connection, "watch", WATCH)

    envelopes = stream_envelopes(taken(connection))
    assert [(e.workspace, e.session) for e in envelopes] == [(None, None), (None, None)]
    await plane.close()


async def test_a_coder_starting_in_another_workspace_is_told_to_watchers_alone() -> None:
    a, b = workspace("a"), workspace("b")
    plane = plane_of(a, b)
    watcher, other, in_b = (
        await welcomed(plane),
        await joined(plane, host=a),
        await joined(plane, host=b),
    )
    await say(watcher, "watch", WATCH)
    taken(watcher)
    taken(other)

    await run_task(in_b)

    started, exited = told(watcher, AgentsChanged)
    (coder,) = started.agents
    assert (coder.workspace, coder.session, coder.role) == ("b", "b-1", "coder")
    assert coder.task_id == board_of(b).tasks[0].id
    assert exited == AgentsChanged(agents=())
    assert told(other, AgentsChanged) == []
    await plane.close()


async def test_an_attached_watcher_is_told_the_agents_of_another_workspace() -> None:
    a, b = workspace("a"), workspace("b")
    plane = plane_of(a, b)
    watcher, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await say(watcher, "watch", WATCH)
    taken(watcher)

    await run_task(in_b)

    assert [len(event.agents) for event in told(watcher, AgentsChanged)] == [1, 0]
    await plane.close()


async def test_watchers_are_told_the_agents_already_running_as_they_start_watching() -> None:
    a = workspace("a")
    plane = plane_of(a)
    in_a = await joined(plane, host=a)
    await a._events.put(AgentStarted(agent_id="agent-7", role="planner", task_id="plan"))
    watcher = await welcomed(plane)

    await say(watcher, "watch", WATCH)

    planner = AgentRef(
        workspace="a", session="a-1", agent_id="agent-7", role="planner", task_id="plan"
    )
    assert told(watcher, AgentsChanged) == [AgentsChanged(agents=(planner,))]
    taken(in_a)
    await plane.close()


async def test_an_exit_of_an_agent_never_started_tells_nothing() -> None:
    a = workspace("a")
    plane = plane_of(a)
    watcher = await welcomed(plane)
    await say(watcher, "watch", WATCH)
    taken(watcher)

    await a._events.put(AgentExited(agent_id="agent-9", reason="finished"))

    assert told(watcher, AgentsChanged) == []
    await plane.close()


async def test_a_finished_turn_tells_watchers_the_totals_with_it_counted() -> None:
    ledger = TotalingLedger()
    a, b = workspace("a", ledger, TURN), workspace("b", ledger)
    plane = plane_of(a, b, ledger=ledger)
    watcher, other, in_a = (
        await welcomed(plane),
        await joined(plane, host=b),
        await joined(plane, host=a),
    )
    await say(watcher, "watch", WATCH)
    taken(watcher)
    taken(other)

    await run_task(in_a)

    spent = UsageTotal(today=COUNTED, lifetime=COUNTED)
    assert told(watcher, UsageTotals) == [
        UsageTotals(
            workspaces=(WorkspaceUsage(workspace="a", today=COUNTED, lifetime=COUNTED),),
            total=spent,
        )
    ]
    assert told(other, UsageTotals) == []
    assert set(ledger.asked) == {MIDNIGHT}
    await plane.close()


async def test_totals_are_not_read_while_no_one_watches() -> None:
    ledger = TotalingLedger()
    a = workspace("a", ledger, TURN)
    plane = plane_of(a, ledger=ledger)

    await run_task(await joined(plane, host=a))

    assert len(ledger.rows) == 1
    assert ledger.asked == []
    await plane.close()


async def test_totals_that_cant_be_read_are_told_as_an_internal_error() -> None:
    ledger = TotalingLedger()
    ledger.unreadable = "Couldn't add up the usage in orbit.db: disk I/O error"
    plane = plane_of(workspace("a"), ledger=ledger)
    watcher = await welcomed(plane)

    await say(watcher, "watch", WATCH)

    assert payloads(taken(watcher))[1:] == [
        ErrorOccurred(code=INTERNAL, message=ledger.unreadable),
        CommandAccepted(command_id="watch"),
    ]
    await plane.close()


async def test_a_turn_recorded_while_the_totals_are_read_has_them_read_again() -> None:
    ledger = TotalingLedger()
    a = workspace("a", ledger)
    plane = plane_of(a, ledger=ledger)
    watcher = await welcomed(plane)
    ledger.gate = asyncio.Event()
    watching = asyncio.create_task(say(watcher, "watch", WATCH))
    await ledger.reading.wait()

    a.usage_recorded()
    ledger.gate.set()
    await watching
    await settled()

    assert len(told(watcher, UsageTotals)) == 2
    assert len(ledger.asked) == 2
    await plane.close()


async def test_watching_again_tells_nothing_more() -> None:
    plane = plane_of(workspace("a"))
    watcher = await welcomed(plane)
    await say(watcher, "watch", WATCH)
    taken(watcher)

    await say(watcher, "again", WATCH)

    assert payloads(taken(watcher)) == [CommandAccepted(command_id="again")]
    await plane.close()


async def test_watching_nothing_stops_the_stream() -> None:
    ledger = TotalingLedger()
    a = workspace("a", ledger, TURN)
    plane = plane_of(a, ledger=ledger)
    watcher, in_a = await welcomed(plane), await joined(plane, host=a)
    await say(watcher, "watch", WATCH)
    await say(watcher, "unwatch", Watch(streams=()))
    taken(watcher)

    await run_task(in_a)

    events: list[Event] = payloads(taken(watcher))
    assert [e for e in events if isinstance(e, AgentsChanged | UsageTotals)] == []
    assert len(ledger.asked) == 1
    await plane.close()


async def test_an_unknown_stream_is_refused_naming_the_streams_there_are() -> None:
    plane = plane_of(workspace("a"))
    watcher = await welcomed(plane)
    await say(watcher, "watch", WATCH)
    taken(watcher)

    await say(watcher, "nope", Watch(streams=(CONTROL_PLANE_STREAM, "nope")))

    assert payloads(taken(watcher)) == [
        CommandRefused(
            command_id="nope",
            code=UNKNOWN,
            message="No stream 'nope'; the stream is control_plane.",
        )
    ]
    assert watcher.streams == {CONTROL_PLANE_STREAM}
    await plane.close()
