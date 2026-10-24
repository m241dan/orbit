"""The verification steps, without the panel showing them: their lines, how long a running one
has run, when they're redrawn and laid out again, and when they tick."""

from orbit.protocol import StepState, StepStatus
from orbit.tui.view.common import spinner_frame
from orbit.tui.view.panels import VerificationSteps, step_lines
from tests.view_fakes import StepsView


class Steps:
    """Verification steps in a view, timed by a clock a test sets in ``now``."""

    def __init__(self, width: int | None = 40, now: float = 0.0) -> None:
        self.view = StepsView(width)
        self.now = now
        self.steps = VerificationSteps(self.view, lambda: self.now)

    @property
    def shown(self) -> str:
        return self.view.texts[-1]


def report(*statuses: StepStatus) -> tuple[StepState, ...]:
    names = ("Unit tests", "Lint", "Types", "Docs", "Build", "Size")
    return tuple(StepState(name=n, status=s) for n, s in zip(names, statuses, strict=False))


def test_with_no_steps_ticking_draws_nothing_new() -> None:
    steps = Steps()
    steps.steps.tick()
    assert steps.view.texts == []


def test_the_steps_reported_show_with_their_status() -> None:
    steps = Steps()
    steps.steps.show(report("pending", "running", "passed", "failed", "cancelled", "skipped"))
    assert steps.shown == "⏳ Unit tests\n◜  Lint · 0s\n✅ Types\n❌ Docs\n🛑 Build\n⏩ Size"


def test_a_running_step_shows_how_long_it_has_been_running() -> None:
    steps = Steps(now=100.0)
    steps.steps.show(report("running", "pending"))
    steps.now = 112.625  # frame 901: the arc moves 8 frames a second, 6 to a turn
    steps.steps.tick()
    assert steps.shown == "◠  Unit tests · 12s\n⏳ Lint"

    # Another report while the same step runs keeps its clock going.
    steps.steps.show(report("running", "pending"))
    assert steps.shown.startswith("◠  Unit tests · 12s")

    # The next step starts its own clock.
    steps.steps.show(report("passed", "running"))
    steps.now = 114.75  # frame 918, back to the start of the arc
    steps.steps.tick()
    assert steps.shown == "✅ Unit tests\n◜  Lint · 2s"

    # With nothing running, ticking changes nothing.
    steps.steps.show(report("passed", "passed"))
    drawn = len(steps.view.texts)
    steps.steps.tick()
    assert steps.shown == "✅ Unit tests\n✅ Lint"
    assert len(steps.view.texts) == drawn


def test_the_startup_check_shows_under_its_heading_until_a_task_takes_over() -> None:
    steps = Steps()
    steps.steps.show(report("passed", "running"), "Startup check")
    assert steps.shown == "Startup check\n✅ Unit tests\n◜  Lint · 0s"
    assert (steps.steps.steps, steps.steps.title) == (report("passed", "running"), "Startup check")
    # A task's verification reports without a title, and the heading goes.
    steps.steps.show(report("pending", "pending"))
    assert steps.shown == "⏳ Unit tests\n⏳ Lint"
    assert steps.steps.title == ""


def test_the_steps_are_redrawn_only_when_their_text_changes() -> None:
    steps = Steps(now=100.0)
    steps.steps.show(report("passed", "passed"))
    drawn = len(steps.view.texts)
    for _ in range(3):
        steps.now += 1 / 8
        steps.steps.tick()
    assert len(steps.view.texts) == drawn  # Nothing runs, so the ticks have nothing to redraw.
    steps.steps.show(report("passed", "running"))
    steps.now += 1 / 8
    steps.steps.tick()
    assert len(steps.view.texts) == drawn + 2  # The report, then the spinner's next frame.


def test_a_spinner_frame_redraws_the_steps_without_a_layout() -> None:
    steps = Steps(now=100.0)
    steps.steps.show(report("running", "pending"))
    steps.now += 1 / 8
    steps.steps.tick()
    assert steps.view.layouts[-1] is False


def test_a_change_in_the_number_of_lines_lays_the_steps_out() -> None:
    steps = Steps()
    steps.steps.show(report("running", "pending"))
    steps.steps.show(report("running", "pending"), "Startup check")
    assert steps.view.layouts == [True, True]


def test_a_line_that_wraps_as_its_time_grows_lays_the_steps_out() -> None:
    width = 40
    steps = Steps(width)
    # The running line around an empty name, so with this name it fills the width exactly at
    # 9s and wraps at 10s, when the time gains a digit.
    fixed = len(step_lines((StepState(name="", status="running"),), spinner_frame(9.0), 9)[0])
    name = "x" * (width - fixed)
    steps.steps.show((StepState(name=name, status="running"),))
    steps.now = 9.0
    steps.steps.tick()
    assert steps.view.layouts[-1] is False
    steps.now = 10.0
    steps.steps.tick()
    assert steps.view.layouts[-1] is True


def test_a_report_with_longer_names_that_wrap_lays_the_steps_out() -> None:
    steps = Steps(width=30)
    steps.steps.show(report("passed", "pending"))
    long = (
        StepState(name="Unit tests", status="passed"),
        StepState(name="A step whose name is far too long to fit", status="pending"),
    )
    steps.steps.show(long)
    assert steps.view.layouts == [True, True]


def test_without_a_width_yet_every_change_lays_the_steps_out() -> None:
    steps = Steps(width=None, now=100.0)
    steps.steps.show(report("running", "pending"))
    steps.now += 1 / 8
    steps.steps.tick()
    # The spinner's frame too, as its wrapping is unknown.
    assert steps.view.layouts == [True, True]


def test_the_steps_tick_only_while_one_runs() -> None:
    steps = Steps()
    assert not steps.view.ticking
    steps.steps.show(report("pending", "pending"))
    assert not steps.view.ticking
    steps.steps.show(report("running", "pending"))
    assert steps.view.ticking
    steps.steps.show(report("passed", "running"))
    assert steps.view.ticking
    steps.steps.show(report("passed", "passed"))
    assert not steps.view.ticking
