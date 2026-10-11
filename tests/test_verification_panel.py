from textual.widgets import Static

from orbit.protocol import (
    RunTasks,
    TaskFinished,
    TaskInserted,
    TaskSpec,
    TaskStarted,
    VerificationConfigured,
    VerificationFinished,
    VerificationStarted,
)
from orbit.tui import OrbitApp
from orbit.tui.tasks import TaskList
from orbit.tui.widgets import Entry, PromptInput, VerificationPanel
from tests.test_task_list import feed, rows, run_two_tasks
from tests.test_tui import FakeSession

FIX = TaskSpec(prompt="Fix the failed verification step: Unit tests\nFAILED test_x")


def steps(app: OrbitApp) -> str:
    return str(app.query_one(VerificationPanel).query_one(Static).render())


def first_lines(app: OrbitApp) -> list[str]:
    return [r.split("\n")[0] for r in rows(app)]


def transcript(app: OrbitApp) -> list[str]:
    return [str(e.render()) for e in app.query(Entry)]


async def test_panel_says_so_when_there_are_no_steps() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test():
        assert app.query_one(VerificationPanel).border_title == "Verification steps"
        assert steps(app) == "No verification steps"


async def test_panel_lists_steps_and_follows_their_progress() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(session, pilot, VerificationConfigured(steps=("Unit tests", "Lint")))
        assert steps(app) == "○ Unit tests\n○ Lint"

        await run_two_tasks(session, pilot)
        await feed(session, pilot, TaskStarted(index=0), VerificationStarted(index=0, step=0))
        assert steps(app) == "▶ Unit tests\n○ Lint"

        await feed(
            session,
            pilot,
            VerificationFinished(index=0, step=0, passed=True),
            VerificationStarted(index=0, step=1),
            VerificationFinished(index=0, step=1, passed=False),
        )
        assert steps(app) == "✓ Unit tests\n✗ Lint"
        assert transcript(app)[-1] == "Verification failed: Lint"
        assert app.query(Entry).last().has_class("error")

        # Each task starts with a clean slate.
        await feed(
            session,
            pilot,
            TaskFinished(index=0, reason="completed", verified=False),
            TaskStarted(index=1),
        )
        assert steps(app) == "○ Unit tests\n○ Lint"


async def test_cancelling_during_verification_marks_the_step_cancelled() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(session, pilot, VerificationConfigured(steps=("Unit tests", "Lint")))
        await run_two_tasks(session, pilot)
        await feed(
            session,
            pilot,
            TaskStarted(index=0),
            VerificationStarted(index=0, step=0),
            TaskFinished(index=0, reason="cancelled"),
        )
        assert steps(app) == "■ Unit tests\n○ Lint"
        assert not app.query_one(PromptInput).disabled


async def test_inserted_fix_task_runs_after_the_failed_task() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await run_two_tasks(session, pilot)
        await feed(
            session,
            pilot,
            TaskStarted(index=0),
            TaskInserted(index=1, task=FIX),
            TaskFinished(index=0, reason="completed", verified=False),
        )
        # The fix shows by its first line, and the failed task is marked unverified.
        assert first_lines(app) == [
            "⚠ one",
            "○ Fix the failed verification step: Unit tests",
            "○ two",
            "+ new task",
        ]
        tasks = app.query_one(TaskList)
        assert tasks.tasks[1] == FIX
        # The highlight stays on the row it was on.
        assert tasks.highlighted == 3

        await feed(session, pilot, TaskStarted(index=1))
        assert first_lines(app)[1] == "▶ Fix the failed verification step: Unit tests"
        assert transcript(app)[-1] == f"Task 2: {FIX.prompt}"

        await feed(
            session,
            pilot,
            TaskFinished(index=1, reason="completed", verified=True),
            TaskStarted(index=2),
        )
        assert first_lines(app)[2] == "▶ two"
        assert app.query_one(PromptInput).disabled

        await feed(session, pilot, TaskFinished(index=2, reason="error"))
        assert [line[0] for line in first_lines(app)] == ["⚠", "✓", "✗", "+"]
        assert not app.query_one(PromptInput).disabled

        # Running again skips the completed task and the one a fix was queued for.
        await pilot.press("ctrl+r")
        assert session.commands[-1] == RunTasks(tasks=(TaskSpec(prompt="two"),))


async def test_insert_before_the_highlight_keeps_it_in_place() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await run_two_tasks(session, pilot)
        await pilot.press("enter", "k", "k", "escape")
        tasks = app.query_one(TaskList)
        assert tasks.highlighted == 0

        await feed(session, pilot, TaskStarted(index=0), TaskInserted(index=1, task=FIX))
        assert tasks.highlighted == 0
