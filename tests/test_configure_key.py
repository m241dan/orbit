"""ctrl+e: queue a configurator task at the front of the list and run it straight away."""

import asyncio

import pytest
from textual.widgets import Static

from orbit.agent import NO_STEPS_NOTE, AgentSession, Verification
from orbit.agent.application.work_turns import CONFIGURING_NOTE, NO_VERIFY_MESSAGE
from orbit.agent.domain.checks import Setup, configure_prompt
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import (
    CONFIGURE_CHECKS,
    INVALID,
    NOT_CONFIGURED,
    UNKNOWN,
    AddTask,
    CancelTurn,
    CommandRefused,
    ErrorOccurred,
    Event,
    FireTrigger,
    Notice,
    RunTasks,
    TasksChanged,
    TaskSpec,
    TurnEndReason,
    command_adapter,
)
from orbit.tui import OrbitApp
from orbit.tui.tasks import TaskList
from orbit.tui.view.confirm import CONFIGURE_MESSAGE, CONFIGURE_TITLE
from orbit.tui.widgets import ConfirmScreen, PickerScreen
from tests.agent_fakes import (
    STEP,
    Call,
    FakeTree,
    Steps,
    board,
    events_until,
    frontend,
    idle,
    of_type,
    refused,
    send,
    session_for,
    verified,
)
from tests.test_tui import FakeSession, shown

CODE = TaskSpec(prompt="Add a flag.")
SETUP = Setup(command=("uv", "sync"), watch=("uv.lock",))


def no_steps() -> Verification:
    """Verification turned on with no steps configured."""
    return Verification((), Steps())


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
    return await events_until(session, idle())


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
    await send(session, AddTask(task=CODE))
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    events = await until_idle(session)
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
    session, _, _ = session_for(configurator, verification=no_steps())
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    events = await until_idle(session)
    assert board(events)[0].task.prompt == configure_prompt((), None)
    assert "Nothing is configured yet." in configurator.tasks[0]


async def test_a_second_request_is_refused_while_one_is_queued_or_running() -> None:
    session, _, _ = session_for(Configurator(), verification=no_steps())
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    events = await until_idle(session)
    assert Notice(message=CONFIGURING_NOTE) in events
    assert CONFIGURING_NOTE == "A configurator task is already queued or running."
    assert [t.task.role for t in board(events)] == ["configurator"]


async def test_it_can_be_asked_for_again_to_reconfigure() -> None:
    session, _, launcher = session_for(Configurator(), Configurator(), verification=no_steps())
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    await until_idle(session)
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    events = await until_idle(session)
    assert [t.task.role for t in board(events)] == ["configurator", "configurator"]
    assert len(launcher.launches) == 2


async def test_while_tasks_run_it_goes_next() -> None:
    session: AgentSession | None = None

    async def asks_to_configure(call: Call) -> TurnEndReason:
        assert session is not None
        await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
        return await claims_done(call)

    configurator = Configurator()
    session, _, launcher = session_for(
        asks_to_configure, configurator, claims_done, verification=no_steps()
    )
    await send(session, AddTask(task=CODE))
    await send(session, AddTask(task=CODE))
    await send(session, RunTasks())
    events = await until_idle(session)
    assert [launch.agent.role for launch in launcher.launches] == ["coder", "configurator", "coder"]
    assert [t.task.role for t in board(events)] == ["configurator", "coder", "coder"]
    assert not any(isinstance(e, ErrorOccurred) for e in events)


async def test_after_ctrl_s_holds_the_work_it_still_runs_straight_away() -> None:
    fetched = asyncio.Event()

    async def waits_forever(call: Call) -> TurnEndReason:
        await call("get_task")
        fetched.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    configurator = Configurator()
    session, _, launcher = session_for(
        waits_forever, configurator, claims_done, verification=no_steps()
    )
    await send(session, AddTask(task=CODE))
    await send(session, RunTasks())
    await asyncio.wait_for(fetched.wait(), 5)
    await send(session, CancelTurn())
    await until_idle(session)
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    events = await until_idle(session)
    assert [launch.agent.role for launch in launcher.launches] == ["coder", "configurator", "coder"]
    assert len(configurator.tasks) == 1
    assert [t.task.role for t in board(events)] == ["configurator", "coder"]


async def test_without_agents_it_is_refused() -> None:
    session = AgentSession()
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    events = await until_refused(session)
    assert events[-1] == refused(NOT_CONFIGURED, "Tasks need agents, and none are set up.")


async def test_without_verification_it_is_refused_and_nothing_starts() -> None:
    session, _, launcher = session_for(Configurator())
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    events = await until_refused(session)
    assert events[-1] == refused(NOT_CONFIGURED, NO_VERIFY_MESSAGE)
    assert NO_VERIFY_MESSAGE == (
        "Setting up checks needs verification on; orbit was started with --no-verify."
    )
    assert not any(isinstance(e, TasksChanged) and e.tasks for e in events)
    assert launcher.launches == []


async def until_refused(session: AgentSession) -> list[Event]:
    """Events up to and including the first refusal; none of them an error."""
    events = await events_until(session, of_type(CommandRefused))
    assert not any(isinstance(e, ErrorOccurred) for e in events)
    return events


async def test_given_input_it_is_refused_and_nothing_is_queued() -> None:
    session, _, launcher = session_for(Configurator(), verification=no_steps())
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS, input={"x": "1"}))
    events = await until_refused(session)
    assert events[-1] == refused(INVALID, "The configure-checks trigger takes no input.")
    assert not any(isinstance(e, TasksChanged) and e.tasks for e in events)
    assert launcher.launches == []


async def test_an_unknown_trigger_is_refused_naming_the_triggers() -> None:
    session, _, launcher = session_for(Configurator(), verification=no_steps())
    await send(session, AddTask(task=CODE))
    await send(session, FireTrigger(trigger="nope"))
    events = await until_refused(session)
    message = "No trigger 'nope'; the triggers are super-review and configure-checks."
    assert events[-1] == refused(UNKNOWN, message)
    assert [len(e.tasks) for e in events if isinstance(e, TasksChanged)][-1] == 1
    assert launcher.launches == []


async def test_ctrl_e_asks_for_a_configurator() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+e")
        assert session.commands == []
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        assert session.commands == [FireTrigger(trigger=CONFIGURE_CHECKS)]
        assert not isinstance(app.screen, ConfirmScreen)


@pytest.mark.parametrize("key", ["n", "escape"])
async def test_declining_the_confirmation_sends_nothing(key: str) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+e", key)
        assert not isinstance(app.screen, ConfirmScreen)
        assert session.commands == []


@pytest.mark.parametrize("key", ["enter", "x"])
async def test_other_keys_leave_the_confirmation_open(key: str) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+e", key)
        assert isinstance(app.screen, ConfirmScreen)
        assert session.commands == []


async def test_ctrl_e_in_the_confirmation_does_not_stack_another() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+e", "ctrl+e")
        assert len(app.screen_stack) == 2
        await pilot.press("y")
        assert len(app.screen_stack) == 1
        assert session.commands == [FireTrigger(trigger=CONFIGURE_CHECKS)]


async def test_ctrl_e_over_another_modal_does_not_ask() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await app.push_screen(PickerScreen("Model", ["default"]))
        await pilot.press("ctrl+e")
        assert isinstance(app.screen, PickerScreen)
        assert not app.check_action("configure_checks", ())


async def test_the_confirmation_shows_its_question_and_keys() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+e")
        body = app.screen.query_one("#confirm-message", Static)
        assert body.border_title == CONFIGURE_TITLE
        assert body.border_subtitle == "y set up · n/esc cancel"
        assert str(body.render()) == CONFIGURE_MESSAGE


def test_the_confirmation_wording() -> None:
    assert CONFIGURE_TITLE == "Set up checks?"
    assert CONFIGURE_MESSAGE == (
        "A configurator task will go to the front of the task list and run now."
    )
    assert ConfirmScreen("Sure?", "Really.").yes == "confirm"


def test_the_configure_checks_trigger_round_trips() -> None:
    command = FireTrigger(trigger=CONFIGURE_CHECKS)
    assert command_adapter.validate_json(command.model_dump_json()) == command


async def test_in_the_app_ctrl_e_runs_a_configurator_and_refuses_a_second() -> None:
    gate = asyncio.Event()
    configurator = Configurator()

    async def waits(call: Call) -> TurnEndReason:
        await gate.wait()
        return await configurator(call)

    session, _, launcher = session_for(waits, verification=no_steps())
    app = OrbitApp(frontend(session))
    async with app.run_test() as pilot:
        await pilot.press("ctrl+e", "y")
        while not launcher.launches:
            await pilot.pause()
        await pilot.press("ctrl+e", "y")
        while CONFIGURING_NOTE not in "\n".join(shown(app)):
            await pilot.pause()
        gate.set()
        while not configurator.tasks:
            await pilot.pause()
        tasks = app.query_one(TaskList).tasks
        assert [t.task.role for t in tasks] == ["configurator"]
        assert len(launcher.launches) == 1
