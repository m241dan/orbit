"""ctrl+e: queue a configurator task at the front of the list and run it straight away."""

import asyncio

from orbit.agent import NO_STEPS_NOTE, AgentSession
from orbit.agent.application.session import CONFIGURING_NOTE
from orbit.agent.domain.roles import Setup, configure_prompt
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import (
    AddTask,
    BusyChanged,
    ConfigureChecks,
    ErrorOccurred,
    Event,
    Notice,
    RunTasks,
    TaskSpec,
    TurnEndReason,
    command_adapter,
)
from orbit.tui import OrbitApp
from orbit.tui.tasks import TaskList
from tests.agent_fakes import STEP, Call, FakeTree, Steps, board, session_for, verified
from tests.test_tui import FakeSession, shown

CODE = TaskSpec(prompt="Add a flag.")
SETUP = Setup(command=("uv", "sync"), watch=("uv.lock",))


class Configurator:
    """A configurator that fetches its task, records it, and stops without proposing."""

    def __init__(self) -> None:
        self.tasks: list[str] = []

    async def __call__(self, call: Call) -> TurnEndReason:
        self.tasks.append((await call("get_task")).text)
        return "completed"


async def claims_done(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("complete_task", summary="Added the flag")
    return "completed"


async def until_idle(session: AgentSession) -> list[Event]:
    events: list[Event] = []
    async for event in session.events():
        events.append(event)
        if event == BusyChanged(busy=False):
            return events
    raise AssertionError("unreachable")


def test_the_startup_note_says_how_to_set_steps_up() -> None:
    assert NO_STEPS_NOTE == "No verification steps configured. Press ctrl+e to set them up."


def test_the_prompt_says_nothing_is_configured_yet() -> None:
    prompt = configure_prompt((), None)
    assert "propose_checks" in prompt
    assert "Nothing is configured yet." in prompt


def test_the_prompt_lists_the_current_steps_and_setup() -> None:
    steps = (VerificationStep("Lint", ("ruff", "check")), VerificationStep("Tests", ("pytest",)))
    prompt = configure_prompt(steps, SETUP)
    assert "Configured now" in prompt
    assert "Steps:\n- Lint: ruff check\n- Tests: pytest" in prompt
    assert "Setup: uv sync (again when uv.lock changes)" in prompt


def test_the_prompt_lists_a_setup_without_steps() -> None:
    prompt = configure_prompt((), Setup(command=("make",)))
    assert "Steps:" not in prompt
    assert prompt.endswith("Setup: make")


async def test_it_queues_a_configurator_first_and_runs_it_straight_away() -> None:
    configurator, tree = Configurator(), FakeTree()
    tree.use_setup(SETUP)
    session, _, launcher = session_for(
        configurator, claims_done, verification=verified(Steps(True)), tree=tree
    )
    await session.send(AddTask(task=CODE))
    await session.send(ConfigureChecks())
    events = await asyncio.wait_for(until_idle(session), 5)
    tasks = board(events)
    assert [t.task.role for t in tasks] == ["configurator", "coder"]
    assert [t.status for t in tasks] == ["unverified", "completed"]
    assert [launch.agent.role for launch in launcher.launches] == ["configurator", "coder"]
    assert tasks[0].task.prompt == configure_prompt((STEP,), SETUP)
    assert tasks[0].task.settings.tool_access == "restricted"
    assert "- Unit tests: pytest" in configurator.tasks[0]
    assert "Setup: uv sync" in configurator.tasks[0]


async def test_with_nothing_configured_the_prompt_says_so() -> None:
    configurator = Configurator()
    session, _, _ = session_for(configurator)
    await session.send(ConfigureChecks())
    events = await asyncio.wait_for(until_idle(session), 5)
    assert board(events)[0].task.prompt == configure_prompt((), None)
    assert "Nothing is configured yet." in configurator.tasks[0]


async def test_a_second_request_is_refused_while_one_is_queued_or_running() -> None:
    session, _, _ = session_for(Configurator())
    await session.send(ConfigureChecks())
    await session.send(ConfigureChecks())
    events = await asyncio.wait_for(until_idle(session), 5)
    assert Notice(message=CONFIGURING_NOTE) in events
    assert CONFIGURING_NOTE == "A configurator task is already queued or running."
    assert [t.task.role for t in board(events)] == ["configurator"]


async def test_it_can_be_asked_for_again_to_reconfigure() -> None:
    session, _, launcher = session_for(Configurator(), Configurator())
    await session.send(ConfigureChecks())
    await asyncio.wait_for(until_idle(session), 5)
    await session.send(ConfigureChecks())
    events = await asyncio.wait_for(until_idle(session), 5)
    assert [t.task.role for t in board(events)] == ["configurator", "configurator"]
    assert len(launcher.launches) == 2


async def test_while_tasks_run_it_goes_next() -> None:
    session: AgentSession | None = None

    async def asks_to_configure(call: Call) -> TurnEndReason:
        assert session is not None
        await session.send(ConfigureChecks())
        return await claims_done(call)

    configurator = Configurator()
    session, _, launcher = session_for(asks_to_configure, configurator, claims_done)
    await session.send(AddTask(task=CODE))
    await session.send(AddTask(task=CODE))
    await session.send(RunTasks())
    events = await asyncio.wait_for(until_idle(session), 5)
    assert [launch.agent.role for launch in launcher.launches] == ["coder", "configurator", "coder"]
    assert [t.task.role for t in board(events)] == ["configurator", "coder", "coder"]
    assert not any(isinstance(e, ErrorOccurred) for e in events)


async def test_without_agents_it_is_refused() -> None:
    session = AgentSession()
    await session.send(ConfigureChecks())
    async for event in session.events():
        if isinstance(event, ErrorOccurred):
            assert event.message == "Tasks need agents, and none are set up."
            return


async def test_ctrl_e_asks_for_a_configurator() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+e")
        assert session.commands == [ConfigureChecks()]


def test_configure_checks_round_trips() -> None:
    assert command_adapter.validate_json(ConfigureChecks().model_dump_json()) == ConfigureChecks()


async def test_in_the_app_ctrl_e_runs_a_configurator_and_refuses_a_second() -> None:
    gate = asyncio.Event()
    configurator = Configurator()

    async def waits(call: Call) -> TurnEndReason:
        await gate.wait()
        return await configurator(call)

    session, _, launcher = session_for(waits)
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+e")
        while not launcher.launches:
            await pilot.pause()
        await pilot.press("ctrl+e")
        while CONFIGURING_NOTE not in "\n".join(shown(app)):
            await pilot.pause()
        gate.set()
        while not configurator.tasks:
            await pilot.pause()
        tasks = app.query_one(TaskList).tasks
        assert [t.task.role for t in tasks] == ["configurator"]
        assert len(launcher.launches) == 1
