"""The snapshot of the open session, sent after Welcome and after every switch."""

import asyncio
from dataclasses import replace
from typing import Any

from orbit.agent import Verification, VerificationStep, WorkspaceHost
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.ports import Reply
from orbit.agent.application.startup_check import AGENT_ID as STARTUP_AGENT
from orbit.agent.application.startup_check import ROLE as STARTUP_ROLE
from orbit.agent.application.startup_check import TITLE as STARTUP_TITLE
from orbit.agent.application.startup_check import CheckSite, StartupCheck
from orbit.agent.domain.runtimes import CLAUDE
from orbit.agent.domain.usage import UsageReport
from orbit.protocol import (
    CONFIGURATOR,
    PLANNER_LANE,
    STARTUP_TASK,
    WORK_LANE,
    AddTask,
    AgentExited,
    AgentReply,
    AgentStarted,
    Catalog,
    ChecksProposed,
    DecideProposal,
    Event,
    NewSession,
    ReviewStatus,
    RunningAgent,
    RunTasks,
    Settings,
    Snapshot,
    StepState,
    TaskSpec,
    Usage,
    Welcome,
)
from tests.agent_fakes import (
    CLAUDE_MODELS,
    LIMITS,
    STEP,
    FakeTree,
    Source,
    Steps,
    desk_for,
    events_until,
    fetch_usage_unseen,
    greet,
    of_type,
    put_on_board,
    send,
    session_for,
    snapshot_of,
    verified,
)
from tests.connection_helpers import published
from tests.proposal_fakes import CONFIGURE, LINT, proposes
from tests.waiting import eventually


def last_of[E](kind: type[E], events: list[Event]) -> E:
    return [e for e in events if isinstance(e, kind)][-1]


TASKS = (TaskSpec(prompt="Add a flag."), TaskSpec(prompt="Document it."))


async def opening(session: WorkspaceHost, count: int) -> list[Event]:
    """The first ``count`` events once ``session`` is greeted, its Welcome first."""
    await greet(session)
    return await published(session, count)


async def test_welcome_is_followed_by_a_snapshot_of_the_open_session() -> None:
    session, _, _ = session_for(verification=verified(Steps()))
    put_on_board(session, *TASKS)
    welcome, catalog, snapshot = await opening(session, 3)
    await session.close()

    assert isinstance(welcome, Welcome)
    assert isinstance(catalog, Catalog)
    assert isinstance(snapshot, Snapshot)
    assert [t.task for t in snapshot.tasks] == list(TASKS)
    assert snapshot.steps == (StepState(name=STEP.name),)
    assert snapshot.steps_title == ""
    assert snapshot.busy == {
        WORK_LANE: False,
        PLANNER_LANE: False,
        "experimenter": False,
        "review": False,
    }
    assert snapshot.session == session.saved_session.summary(snapshot.tasks, Usage(), None)
    assert (snapshot.settings, snapshot.model_choices) == (Settings(), ())
    assert (snapshot.review, snapshot.proposals, snapshot.agents) == (None, (), ())


async def test_a_session_without_verification_or_agents_snapshots_none() -> None:
    session = WorkspaceHost(settings=Settings(model="opus"))
    *_, snapshot = await opening(session, 3)
    await session.close()

    assert isinstance(snapshot, Snapshot)
    assert (snapshot.steps, snapshot.agents) == ((), ())
    assert (snapshot.settings, snapshot.model_choices, snapshot.model_ids) == (
        Settings(model="opus"),
        (),
        {},
    )


async def test_the_snapshot_offers_the_planners_runtimes_models_with_their_ids() -> None:
    models = replace(CLAUDE_MODELS, aliases={**CLAUDE_MODELS.aliases, "fast": "claude-haiku-5-5"})
    session, _, _ = session_for(models={CLAUDE: models})
    *_, snapshot = await opening(session, 3)
    await session.close()

    assert isinstance(snapshot, Snapshot)
    assert snapshot.model_choices == ("fable", "opus", "sonnet", "haiku", "fast")
    assert snapshot.model_ids == {**CLAUDE_MODELS.aliases, "fast": "claude-haiku-5-5"}


async def test_with_reviews_on_the_snapshot_has_the_review_status() -> None:
    session, _, _ = session_for(tree=FakeTree(), review=True)
    *_, snapshot = await opening(session, 3)
    await session.close()

    assert isinstance(snapshot, Snapshot)
    assert snapshot.review == ReviewStatus(paused=False, open_batches=0)


async def test_usage_fetched_before_attaching_comes_after_the_snapshot() -> None:
    session, _, _ = session_for(usage=Source(UsageReport(LIMITS)))
    await fetch_usage_unseen(session)
    events = await opening(session, 4)
    await session.close()

    kinds = [type(e).__name__ for e in events]
    assert kinds == ["Welcome", "Catalog", "Snapshot", "UsageChanged"]


async def test_with_no_usage_fetched_the_catalog_is_followed_by_the_snapshot() -> None:
    session, _, _ = session_for(usage=Source(UsageReport(LIMITS)))
    events = await opening(session, 3)
    await session.close()

    assert [type(e).__name__ for e in events] == ["Welcome", "Catalog", "Snapshot"]


async def proposal_waiting(session: WorkspaceHost) -> ChecksProposed:
    """Run a configurator task in ``session`` up to the proposal it waits on."""
    await send(session, AddTask(task=CONFIGURE))
    await send(session, RunTasks())
    events = await events_until(session, of_type(ChecksProposed))
    proposal = events[-1]
    assert isinstance(proposal, ChecksProposed)
    return proposal


def proposing() -> WorkspaceHost:
    """A session whose configurator proposes a lint step for the frontend to decide on."""
    replies: list[str] = []
    session, _, _ = session_for(
        proposes({"steps": [LINT]}, replies=replies),
        verification=verified(Steps(True, True)),
        tree=FakeTree(),
    )
    return session


async def test_a_waiting_proposal_is_in_the_snapshot_until_decided() -> None:
    session = proposing()
    proposal = await proposal_waiting(session)
    waiting = (await snapshot_of(session)).proposals
    await send(session, DecideProposal(task_id=proposal.task_id, action="accept"))
    decided = (await snapshot_of(session)).proposals
    await session.close()

    assert waiting == (proposal,)
    assert decided == ()


async def test_an_unanswered_call_is_an_open_request_of_its_agent_until_replied_to() -> None:
    session = proposing()
    proposal = await proposal_waiting(session)
    [waiting] = (await snapshot_of(session)).agents
    await send(session, DecideProposal(task_id=proposal.task_id, action="accept"))
    await events_until(session, of_type(AgentReply))
    agents = (await snapshot_of(session)).agents
    await session.close()

    [request] = waiting.open_requests
    assert (waiting.agent_id, waiting.role, waiting.task_id) == ("agent-1", CONFIGURATOR, "1")
    assert request.tool == "propose_checks"
    assert all(not agent.open_requests for agent in agents)


async def test_a_new_session_is_snapshot_empty_after_the_switch() -> None:
    session, _, _ = session_for()
    await send(session, AddTask(task=TASKS[0]))
    await send(session, NewSession(name="other"))
    await events_until(session, of_type(Snapshot))  # The one after the Welcome.
    events = await events_until(session, of_type(Snapshot))
    await session.close()

    snapshot = last_of(Snapshot, events)
    assert snapshot.tasks == ()
    assert snapshot.session.name == "other" and snapshot.session.current


async def test_while_the_startup_check_runs_it_is_a_running_agent_with_the_panel() -> None:
    steps = Steps(True)
    steps.gate = asyncio.Event()
    session, _, _ = session_for(verification=Verification((STEP,), steps), check_at_startup=True)
    await greet(session)
    await eventually(lambda: steps.runs, poll=0)
    snapshot = await snapshot_of(session)
    steps.gate.set()
    await session.close()

    startup = RunningAgent(agent_id=STARTUP_AGENT, role=STARTUP_ROLE, task_id=STARTUP_TASK)
    assert snapshot.agents == (startup,)
    assert snapshot.steps == (StepState(name=STEP.name, status="running"),)
    assert snapshot.steps_title == STARTUP_TITLE


class Seen:
    """What ``desk`` lists as running each time it publishes, by the kind of event."""

    def __init__(self) -> None:
        self.desk: AgentDesk | None = None
        self.running: list[tuple[str, tuple[RunningAgent, ...]]] = []

    async def publish(self, event: Event) -> None:
        assert self.desk is not None
        self.running.append((type(event).__name__, self.desk.running))

    def at(self, kind: str) -> tuple[RunningAgent, ...]:
        """What was running as the last event of ``kind`` was published."""
        return [running for name, running in self.running if name == kind][-1]


class Answers:
    """A run that answers every call, noting what its desk lists as running meanwhile."""

    def __init__(self, desk: AgentDesk) -> None:
        self.desk = desk
        self.running: tuple[RunningAgent, ...] = ()

    async def handle(self, tool: str, arguments: dict[str, Any]) -> Reply:
        self.running = self.desk.running
        return Reply("ok")


def watched_desk() -> tuple[AgentDesk, Seen]:
    seen = Seen()
    seen.desk = desk_for(seen.publish)
    return seen.desk, seen


async def test_an_agent_is_running_only_once_its_start_is_published() -> None:
    desk, _ = watched_desk()
    agent = desk.open(Answers(desk), "coder", "1")
    before = desk.running
    await desk.publish(AgentStarted(agent_id=agent.id, role="coder", task_id="1"))

    assert before == ()
    assert desk.running == (RunningAgent(agent_id=agent.id, role="coder", task_id="1"),)


async def test_an_agent_is_not_running_once_its_exit_is_published() -> None:
    desk, seen = watched_desk()
    agent = desk.open(Answers(desk), "coder", "1")
    await desk.publish(AgentStarted(agent_id=agent.id, role="coder", task_id="1"))
    await desk.publish(AgentExited(agent_id=agent.id, reason="finished"))

    assert seen.at("AgentExited") == ()
    assert desk.running == ()


async def test_a_closed_agent_is_not_running() -> None:
    desk, _ = watched_desk()
    agent = desk.open(Answers(desk), "coder", "1")
    await desk.publish(AgentStarted(agent_id=agent.id, role="coder", task_id="1"))
    desk.close(agent)

    assert desk.running == ()


async def test_a_call_is_an_open_request_only_once_its_request_is_published() -> None:
    desk, seen = watched_desk()
    run = Answers(desk)
    agent = desk.open(run, "coder", "1")
    await desk.publish(AgentStarted(agent_id=agent.id, role="coder", task_id="1"))
    await desk.handle("token-1", "get_task", {})

    [requesting] = seen.at("AgentRequest")
    [answering] = run.running
    [replying] = seen.at("AgentReply")
    assert requesting.open_requests == replying.open_requests == ()
    assert [request.tool for request in answering.open_requests] == ["get_task"]


async def test_the_startup_check_is_an_agent_only_between_its_start_and_exit() -> None:
    shown: list[tuple[str, RunningAgent | None]] = []

    async def publish(event: Event) -> None:
        shown.append((type(event).__name__, check.agent))

    async def add_fix(step: VerificationStep, output: str) -> None:
        pass

    check = StartupCheck(Verification((STEP,), Steps(True)), CheckSite(None, "."), publish, add_fix)
    check.start()
    await eventually(lambda: not check.running, poll=0)
    await check.close()

    agent = RunningAgent(agent_id=STARTUP_AGENT, role=STARTUP_ROLE, task_id=STARTUP_TASK)
    kinds = dict(shown)
    assert (kinds["AgentStarted"], kinds["StartupChecked"]) == (None, agent)
    assert kinds["AgentExited"] is None and check.agent is None
