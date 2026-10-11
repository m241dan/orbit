from textual.widgets import Static

from orbit.protocol import StepState, VerificationChanged, VerificationFinished
from orbit.tui import OrbitApp
from orbit.tui.widgets import Entry, VerificationPanel
from tests.test_task_list import feed
from tests.test_tui import FakeSession


def steps(app: OrbitApp) -> str:
    return str(app.query_one(VerificationPanel).query_one(Static).render())


async def test_panel_says_so_when_there_are_no_steps() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test():
        assert app.query_one(VerificationPanel).border_title == "Verification steps"
        assert steps(app) == "No verification steps"


async def test_panel_shows_the_steps_the_backend_reports() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        names = ("Unit tests", "Lint", "Types", "Docs", "Build")
        statuses = ("pending", "running", "passed", "failed", "cancelled")
        report = VerificationChanged(
            steps=tuple(
                StepState(name=n, status=s)  # type: ignore[arg-type]
                for n, s in zip(names, statuses, strict=True)
            )
        )
        await feed(session, pilot, report)
        assert steps(app) == "○ Unit tests\n▶ Lint\n✓ Types\n✗ Docs\n■ Build"


async def test_a_failed_step_is_reported_in_the_transcript() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        report = VerificationChanged(steps=(StepState(name="Unit tests"), StepState(name="Lint")))
        await feed(
            session,
            pilot,
            report,
            VerificationFinished(task_id="1", step=0, passed=True),
            VerificationFinished(task_id="1", step=1, passed=False),
        )
        [entry] = app.query(Entry)
        assert str(entry.render()) == "Verification failed: Lint"
        assert entry.has_class("error")
