"""The verification steps panel: the app showing the steps reported and noting their
progress in the transcript, and the panel alone wrapping, laying out and ticking."""

import pytest
from textual.app import App, ComposeResult
from textual.pilot import Pilot
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
from orbit.tui.panels import VerificationPanel
from orbit.tui.view.common import spinner_frame
from orbit.tui.view.panels import step_lines
from tests.tui_fakes import FakeSession, entries, feed_events


def steps(app: OrbitApp) -> str:
    return str(app.query_one(VerificationPanel).query_one(Static).render())


def report(*statuses: StepStatus) -> VerificationChanged:
    names = ("Unit tests", "Lint", "Types", "Docs", "Build", "Size")
    return VerificationChanged(
        steps=tuple(StepState(name=n, status=s) for n, s in zip(names, statuses, strict=False))
    )


async def test_panel_shows_the_steps_the_backend_reports() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        app.query_one(VerificationPanel).clock = lambda: 0.0
        await feed_events(
            session,
            pilot,
            report("pending", "running", "passed", "failed", "cancelled", "skipped"),
        )
        assert steps(app) == "⏳ Unit tests\n◜  Lint · 0s\n✅ Types\n❌ Docs\n🛑 Build\n⏩ Size"


async def test_verification_progress_is_reported_in_the_transcript() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            report("pending", "pending"),
            VerificationStarted(task_id="1", step=0),
            VerificationFinished(task_id="1", step=0, passed=True),
            VerificationStarted(task_id="1", step=1),
            VerificationFinished(task_id="1", step=1, passed=False),
        )
        assert entries(app) == [
            ("🔍 Verifying: Unit tests…", "verify"),
            ("✅ Verification passed: Unit tests", "verify"),
            ("🔍 Verifying: Lint…", "verify"),
            ("❌ Verification failed: Lint", "error"),
        ]


async def test_skipped_verification_says_why_in_the_transcript() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            VerificationSkipped(task_id="1", reason="tools_off"),
            VerificationSkipped(task_id="2", reason="no_changes"),
        )
        assert entries(app) == [
            ("⏩ Verification skipped: tools were off", "verify"),
            ("⏩ Verification skipped: no files changed", "verify"),
        ]


async def test_the_startup_checks_result_is_noted_in_the_transcript() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    output = "\n".join(f"line {n}" for n in range(1, 31))
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            StartupChecked(passed=True),
            StartupChecked(passed=False, step="Lint", output=output),
        )
        passed, (text, kind) = entries(app)
        assert passed == ("✅ Startup check passed", "verify")
        assert kind == "error"
        tail = "\n".join(f"line {n}" for n in range(11, 31))
        assert text == (
            f"❌ Startup check failed: Lint\n\n{tail}\n\n"
            "📝 Added a draft task: Fix failing check: Lint. If the steps themselves are wrong,"
            " press ctrl+e to reconfigure them."
        )


def spy_layouts(panel: VerificationPanel, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    layouts: list[bool] = []

    def update(content: object, *, layout: bool = True) -> None:
        layouts.append(layout)

    monkeypatch.setattr(panel.query_one(Static), "update", update)
    return layouts


def running(name: str) -> VerificationChanged:
    return VerificationChanged(steps=(StepState(name=name, status="running"),))


class PanelApp(App[None]):
    """A bare app around a verification steps panel."""

    def compose(self) -> ComposeResult:
        yield VerificationPanel(id="verification-steps")

    @property
    def panel(self) -> VerificationPanel:
        return self.query_one(VerificationPanel)


def panel_text(app: PanelApp) -> str:
    return str(app.panel.query_one(Static).render())


async def show(pilot: Pilot[None], panel: VerificationPanel, event: VerificationChanged) -> None:
    """Have ``panel`` show the steps ``event`` reports, and let it redraw."""
    panel.show(event.steps, event.title)
    await pilot.pause()


async def test_panel_says_so_when_there_are_no_steps() -> None:
    app = PanelApp()
    async with app.run_test():
        assert app.panel.border_title == "Verification steps"
        assert panel_text(app) == "No verification steps"
        # The spinner's redraws keep saying so.
        app.panel.tick()
        assert panel_text(app) == "No verification steps"


async def test_a_line_that_wraps_as_its_time_grows_lays_the_panel_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PanelApp()
    async with app.run_test(size=(60, 24)) as pilot:
        panel = app.panel
        width = panel.query_one(Static).content_region.width
        # The running line around an empty name, so with this name it fills the width
        # exactly at 9s and wraps at 10s, when the time gains a digit.
        fixed = len(step_lines((StepState(name="", status="running"),), spinner_frame(9.0), 9)[0])
        name = "x" * (width - fixed)
        now = [0.0]
        panel.clock = lambda: now[0]
        await show(pilot, panel, running(name))
        # Records the panel's height for the line at 9s before the spy goes in.
        now[0] = 9.0
        panel.tick()
        layouts = spy_layouts(panel, monkeypatch)
        now[0] = 10.0
        panel.tick()
        assert layouts == [True]


async def test_a_spinner_frame_redraws_the_panel_without_a_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.panel
        now = [100.0]
        panel.clock = lambda: now[0]
        await show(pilot, panel, report("running", "pending"))
        layouts = spy_layouts(panel, monkeypatch)
        now[0] += 1 / 8
        panel.tick()
        assert layouts == [False]


async def test_a_panel_without_a_width_yet_lays_out_every_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.panel
        now = [100.0]
        panel.clock = lambda: now[0]
        panel.query_one(Static).styles.width = 0
        await pilot.pause()
        layouts = spy_layouts(panel, monkeypatch)
        await show(pilot, panel, report("running", "pending"))
        now[0] += 1 / 8
        panel.tick()
        assert layouts == [True, True]  # The spinner's frame too, as its wrapping is unknown.


async def test_panel_ticks_only_while_a_step_runs() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.panel
        assert not panel.ticking
        await show(pilot, panel, report("pending", "pending"))
        assert not panel.ticking
        await show(pilot, panel, report("running", "pending"))
        assert panel.ticking
        await show(pilot, panel, report("passed", "running"))
        assert panel.ticking
        await show(pilot, panel, report("passed", "passed"))
        assert not panel.ticking
