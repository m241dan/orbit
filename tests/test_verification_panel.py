from textual.widgets import Static

from orbit.protocol import (
    StartupChecked,
    StepState,
    StepStatus,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)
from orbit.tui import OrbitApp
from orbit.tui.widgets import Entry, VerificationPanel
from tests.test_task_list import feed
from tests.test_tui import FakeSession


def steps(app: OrbitApp) -> str:
    return str(app.query_one(VerificationPanel).query_one(Static).render())


def report(*statuses: StepStatus) -> VerificationChanged:
    names = ("Unit tests", "Lint", "Types", "Docs", "Build", "Size")
    return VerificationChanged(
        steps=tuple(StepState(name=n, status=s) for n, s in zip(names, statuses, strict=False))
    )


async def test_panel_says_so_when_there_are_no_steps() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test():
        assert app.query_one(VerificationPanel).border_title == "Verification steps"
        assert steps(app) == "No verification steps"
        # The spinner's redraws keep saying so.
        app.query_one(VerificationPanel).tick()
        assert steps(app) == "No verification steps"


async def test_panel_shows_the_steps_the_backend_reports() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        app.query_one(VerificationPanel).clock = lambda: 0.0
        await feed(
            session,
            pilot,
            report("pending", "running", "passed", "failed", "cancelled", "skipped"),
        )
        assert steps(app) == "⏳ Unit tests\n◜  Lint · 0s\n✅ Types\n❌ Docs\n🛑 Build\n⏩ Size"


async def test_running_step_shows_how_long_it_has_been_running() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        panel = app.query_one(VerificationPanel)
        now = [100.0]
        panel.clock = lambda: now[0]
        await feed(session, pilot, report("running", "pending"))
        now[0] = 112.625  # frame 901: the arc moves 8 frames a second, 6 to a turn
        panel.tick()
        assert steps(app) == "◠  Unit tests · 12s\n⏳ Lint"

        # Another report while the same step runs keeps its clock going.
        await feed(session, pilot, report("running", "pending"))
        assert steps(app).startswith("◠  Unit tests · 12s")

        # The next step starts its own clock.
        await feed(session, pilot, report("passed", "running"))
        now[0] = 114.75  # frame 918, back to the start of the arc
        panel.tick()
        assert steps(app) == "✅ Unit tests\n◜  Lint · 2s"

        # With nothing running, ticking changes nothing.
        await feed(session, pilot, report("passed", "passed"))
        panel.tick()
        assert steps(app) == "✅ Unit tests\n✅ Lint"


async def test_verification_progress_is_reported_in_the_transcript() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(
            session,
            pilot,
            report("pending", "pending"),
            VerificationStarted(task_id="1", step=0),
            VerificationFinished(task_id="1", step=0, passed=True),
            VerificationStarted(task_id="1", step=1),
            VerificationFinished(task_id="1", step=1, passed=False),
        )
        entries = [(str(e.render()), e.classes) for e in app.query(Entry)]
        assert entries == [
            ("🔍 Verifying: Unit tests…", frozenset({"verify"})),
            ("✅ Verification passed: Unit tests", frozenset({"verify"})),
            ("🔍 Verifying: Lint…", frozenset({"verify"})),
            ("❌ Verification failed: Lint", frozenset({"error"})),
        ]


async def test_skipped_verification_says_why_in_the_transcript() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(
            session,
            pilot,
            VerificationSkipped(task_id="1", reason="tools_off"),
            VerificationSkipped(task_id="2", reason="no_changes"),
        )
        entries = [(str(e.render()), e.classes) for e in app.query(Entry)]
        assert entries == [
            ("⏩ Verification skipped: tools were off", frozenset({"verify"})),
            ("⏩ Verification skipped: no files changed", frozenset({"verify"})),
        ]


async def test_the_startup_check_shows_under_its_heading_until_a_task_takes_over() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        app.query_one(VerificationPanel).clock = lambda: 0.0
        startup = report("passed", "running")
        await feed(session, pilot, startup.model_copy(update={"title": "Startup check"}))
        assert steps(app) == "Startup check\n✅ Unit tests\n◜  Lint · 0s"
        # A task's verification reports without a title, and the heading goes.
        await feed(session, pilot, report("pending", "pending"))
        assert steps(app) == "⏳ Unit tests\n⏳ Lint"


async def test_the_startup_checks_result_is_noted_in_the_transcript() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    output = "\n".join(f"line {n}" for n in range(1, 31))
    async with app.run_test() as pilot:
        await feed(
            session,
            pilot,
            StartupChecked(passed=True),
            StartupChecked(passed=False, step="Lint", output=output),
        )
        passed, failed = [(str(e.render()), e.classes) for e in app.query(Entry)]
        assert passed == ("✅ Startup check passed", frozenset({"verify"}))
        text, kind = failed
        assert kind == frozenset({"error"})
        tail = "\n".join(f"line {n}" for n in range(11, 31))
        assert text == (
            f"❌ Startup check failed: Lint\n\n{tail}\n\n"
            "📝 Added a draft task: Fix failing check: Lint. If the steps themselves are wrong,"
            " press ctrl+e to reconfigure them."
        )
