"""The named sets' runs: each in the background, in a set-up snapshot of the base's latest
commit, shown in the verification panel under its set's name and as a checker; ``set_finished``
raised as each ends, a failure's notice while no trigger listens, a firing while one runs folded
into one more run, halts, cancelling, and their spans."""

import asyncio
from collections.abc import Mapping
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan

from orbit.agent.application.agent_trace import STEP_SPAN
from orbit.agent.application.ports import StepRunner, TreeFailed
from orbit.agent.application.set_runs import SetResult, SetRuns, SetSite, cancelled_note
from orbit.agent.application.set_trace import SET_SPAN, SetTracing
from orbit.agent.domain.checks import Setup
from orbit.agent.domain.triggers import SetOutcome
from orbit.agent.domain.verification import CheckSet, VerificationStep, set_failed_note
from orbit.protocol import (
    GIT_FAILED,
    AgentExited,
    AgentStarted,
    ErrorOccurred,
    Event,
    Notice,
    RunningAgent,
    StepState,
    StepStatus,
    VerificationChanged,
)
from tests.agent_fakes import FakeTree, Steps, settled
from tests.telemetry_helpers import Traced
from tests.waiting import eventually

MUTATION = VerificationStep("Mutation tests", ("mutmut", "run"), "slow")
SOAK = VerificationStep("Soak", ("soak",), "slow")
SLOW = CheckSet("slow", (MUTATION, SOAK))
FAST = CheckSet("fast", (VerificationStep("Smoke", ("smoke",), "fast"),))
SNAPSHOT = "/work/review-base"


class Runs:
    """Runs of ``sets`` in ``tree``, their steps played by ``steps``, recording what they
    publish and the ``set_finished`` they raise, halted while :attr:`halted` says so, a trigger
    listening while :attr:`listens` says so, traced by ``traced`` if given."""

    def __init__(
        self,
        steps: StepRunner,
        *sets: CheckSet,
        tree: FakeTree | None = None,
        traced: Traced | None = None,
    ) -> None:
        self.events: list[Event] = []
        self.heard: list[SetResult] = []
        self.finished: list[SetOutcome] = []
        self.halted = False
        self.listens = False
        self.tree = tree or FakeTree()
        site = SetSite(self.tree, steps, self.publish, lambda: self.halted, self.hear, self.finish)
        tracing = SetTracing() if traced is None else SetTracing(traced.telemetry, "ws")
        self.runs = SetRuns(sets or (SLOW,), site, tracing)

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    async def hear(self, result: SetResult) -> None:
        self.heard.append(result)

    async def finish(self, outcome: SetOutcome) -> bool:
        self.finished.append(outcome)
        return self.listens

    def exits(self) -> list[AgentExited]:
        return [event for event in self.events if isinstance(event, AgentExited)]

    def starts(self) -> list[AgentStarted]:
        return [event for event in self.events if isinstance(event, AgentStarted)]

    def notices(self) -> list[str]:
        return [event.message for event in self.events if isinstance(event, Notice)]

    async def ended(self, count: int = 1) -> None:
        """Return once ``count`` runs have ended."""
        await eventually(lambda: len(self.exits()) >= count)


def shown(*statuses: StepStatus, title: str = "slow", output: str = "") -> VerificationChanged:
    """The panel showing ``slow``'s steps at ``statuses``, under ``title``, with ``output``."""
    names = (MUTATION.name, SOAK.name)
    steps = tuple(StepState(name=n, status=s) for n, s in zip(names, statuses, strict=True))
    return VerificationChanged(steps=steps, title=title, output=output)


def gated(*results: bool) -> tuple[Steps, asyncio.Event]:
    """Steps playing ``results`` that wait at their gate, and the gate, closed until it's set."""
    steps = Steps(*results)
    gate = asyncio.Event()
    steps.gate = gate
    return steps, gate


# A run


async def test_a_run_shows_its_steps_under_its_name_in_a_set_up_snapshot_of_the_base() -> None:
    steps = Steps(True, True, True)
    tree = FakeTree()
    tree.setup = Setup(command=("uv", "sync"))
    runs = Runs(steps, tree=tree)

    runs.runs.run("slow", "slow")
    await runs.ended()

    # Never in the coder's tree: in a snapshot of the base's latest commit, set up first.
    assert (tree.snapshots, tree.prepared, tree.dropped) == (["base"], [], [SNAPSHOT])
    assert steps.runs == ["setup", "Mutation tests", "Soak"]
    assert steps.roots == [SNAPSHOT] * 3
    assert runs.events == [
        AgentStarted(agent_id="set-slow", role="checker", task_id="startup"),
        shown("pending", "pending"),
        shown("running", "pending"),
        shown("passed", "pending"),
        shown("passed", "running"),
        shown("passed", "passed"),
        AgentExited(agent_id="set-slow", reason="finished"),
    ]
    assert runs.runs.failures == {}


async def test_a_failing_reporting_set_adds_its_notice_and_keeps_its_failure() -> None:
    runs = Runs(Steps(False))
    runs.runs.run("slow", "slow")
    await runs.ended()

    assert runs.notices() == [
        "slow reported: Mutation tests failed. Its output ended:\n\n1 test failed"
    ]
    assert shown("failed", "pending") in runs.events
    assert runs.runs.failures == {"slow": (MUTATION, "1 test failed")}
    assert runs.exits() == [AgentExited(agent_id="set-slow", reason="finished")]


async def test_a_failing_run_shows_its_steps_trimmed_output_in_the_panel_before_its_notice() -> (
    None
):
    async def survived(step: VerificationStep, root: str) -> tuple[bool, str]:
        return False, "\n  3 mutants survived  \n"

    runs = Runs(survived)
    runs.runs.run("slow", "slow")
    await runs.ended()

    failed = shown("failed", "pending", output="3 mutants survived")
    assert runs.events[-3:] == [
        failed,
        Notice(message=set_failed_note("slow", MUTATION.name, "3 mutants survived")),
        AgentExited(agent_id="set-slow", reason="finished"),
    ]
    assert runs.runs.panel == failed


async def test_a_holding_sets_failing_run_shows_its_output_in_the_panel() -> None:
    stress = CheckSet("stress", (MUTATION, SOAK), "holds_merges")
    runs = Runs(Steps(False), stress)
    runs.runs.run("stress", "nightly")
    await runs.ended()

    panel = runs.runs.panel
    assert panel is not None and (panel.title, panel.output) == ("stress", "1 test failed")


async def test_a_run_after_a_failure_shows_no_output_and_a_passing_one_adds_none() -> None:
    runs = Runs(Steps(False, True, True))
    runs.runs.run("slow", "slow")
    await runs.ended()
    runs.runs.run("slow", "slow")
    await runs.ended(2)

    starts = [i for i, event in enumerate(runs.events) if isinstance(event, AgentStarted)]
    second = runs.events[starts[1] :]
    assert [event.output for event in second if isinstance(event, VerificationChanged)] == [""] * 5
    assert runs.runs.panel == shown("passed", "passed")


async def test_a_holding_sets_runs_are_heard_with_their_commit_and_trigger_and_no_notice() -> None:
    stress = CheckSet("stress", (MUTATION, SOAK), "holds_merges")
    runs = Runs(Steps(True, False, True, True), stress)
    runs.runs.run("stress", "nightly")
    await runs.ended()
    runs.runs.run("stress", "by-hand")
    await runs.ended(2)
    assert runs.notices() == []
    assert runs.heard == [
        SetResult(stress, "base", "nightly", (SOAK, "1 test failed")),
        SetResult(stress, "base", "by-hand", None),
    ]


async def test_a_reporting_sets_runs_are_not_heard() -> None:
    runs = Runs(Steps(False))
    runs.runs.run("slow", "slow")
    await runs.ended()
    assert runs.heard == []


async def test_a_holding_set_fired_again_while_it_runs_is_heard_from_the_last_trigger() -> None:
    steps, gate = gated(True, True, True, True)
    stress = CheckSet("stress", (MUTATION, SOAK), "holds_merges")
    runs = Runs(steps, stress)
    runs.runs.run("stress", "first")
    runs.runs.run("stress", "second")
    runs.runs.run("stress", "third")
    gate.set()
    await runs.ended(2)
    assert [result.trigger for result in runs.heard] == ["first", "third"]


# set_finished


async def test_a_passing_run_raises_set_finished_passed_on_its_commit() -> None:
    runs = Runs(Steps(True, True))
    runs.runs.run("slow", "slow")
    await runs.ended()
    assert runs.finished == [SetOutcome("slow", "base")]
    assert runs.finished[0].status == "passed"


async def test_a_failing_run_raises_set_finished_failed_with_its_step_and_output() -> None:
    runs = Runs(Steps(False))
    runs.runs.run("slow", "slow")
    await runs.ended()
    assert runs.finished == [SetOutcome("slow", "base", (MUTATION.name, "1 test failed"))]
    assert runs.finished[0].status == "failed"


async def test_a_failing_reporting_set_a_trigger_listens_for_gives_no_notice() -> None:
    runs = Runs(Steps(False))
    runs.listens = True
    runs.runs.run("slow", "slow")
    await runs.ended()
    assert runs.notices() == []
    assert runs.runs.failures == {"slow": (MUTATION, "1 test failed")}


async def test_a_holding_sets_failure_is_heard_and_raises_set_finished_too() -> None:
    stress = CheckSet("stress", (MUTATION, SOAK), "holds_merges")
    runs = Runs(Steps(False), stress)
    runs.runs.run("stress", "nightly")
    await runs.ended()
    assert runs.heard == [SetResult(stress, "base", "nightly", (MUTATION, "1 test failed"))]
    assert runs.finished == [SetOutcome("stress", "base", (MUTATION.name, "1 test failed"))]
    assert runs.notices() == []


async def test_a_site_raising_no_set_finished_has_no_trigger_listening() -> None:
    events: list[Event] = []

    async def publish(event: Event) -> None:
        events.append(event)

    site = SetSite(FakeTree(), Steps(False), publish, lambda: False)
    runs = SetRuns((SLOW,), site)
    runs.run("slow", "slow")
    await eventually(lambda: any(isinstance(event, AgentExited) for event in events))
    assert [event for event in events if isinstance(event, Notice)] == [
        Notice(message=set_failed_note("slow", MUTATION.name, "1 test failed"))
    ]


async def test_a_run_cancelled_or_one_git_cannot_start_raises_nothing() -> None:
    steps, _ = gated(True)
    runs = Runs(steps)
    runs.runs.run("slow", "slow")
    await eventually(lambda: steps.runs)
    await runs.runs.stop()
    runs.tree.failures["base_commit"] = TreeFailed("git rev-parse failed")
    runs.runs.run("slow", "slow")
    await runs.ended(2)
    assert runs.finished == []


async def test_a_failing_setup_fails_the_run_before_its_steps() -> None:
    steps = Steps(False)
    tree = FakeTree()
    tree.setup = Setup(command=("uv", "sync"))
    runs = Runs(steps, tree=tree)
    runs.runs.run("slow", "slow")
    await runs.ended()

    assert steps.runs == ["setup"]
    assert runs.notices() == ["slow reported: setup failed. Its output ended:\n\n1 test failed"]


async def test_a_base_git_cannot_find_is_reported() -> None:
    steps = Steps()
    tree = FakeTree()
    tree.failures["base_commit"] = TreeFailed("git rev-parse failed")
    runs = Runs(steps, tree=tree)
    runs.runs.run("slow", "slow")
    await runs.ended()

    assert runs.events[-2:] == [
        ErrorOccurred(code=GIT_FAILED, message="slow couldn't run: git rev-parse failed"),
        AgentExited(agent_id="set-slow", reason="crashed"),
    ]
    assert (steps.runs, tree.snapshots) == ([], [])


async def test_a_set_there_is_no_run_of_runs_nothing() -> None:
    runs = Runs(Steps())
    runs.runs.run("nightly", "nightly")
    await settled()
    assert runs.events == []


# Sets given anew


async def test_a_set_given_anew_runs_when_its_trigger_fires() -> None:
    runs = Runs(Steps(True))
    runs.runs.use_sets((FAST,))
    runs.runs.run("fast", "fast")
    await runs.ended()
    assert [start.agent_id for start in runs.starts()] == ["set-fast"]


async def test_a_set_left_out_when_sets_are_given_anew_keeps_its_run() -> None:
    steps = Steps(True, True)
    runs = Runs(steps)
    runs.runs.use_sets((FAST,))
    runs.runs.run("slow", "slow")
    await runs.ended()
    assert steps.runs == ["Mutation tests", "Soak"]


async def test_a_set_changed_while_it_runs_ends_as_it_started_and_runs_as_changed_next() -> None:
    steps, gate = gated(True, True, False)
    runs = Runs(steps)
    runs.runs.run("slow", "slow")
    await eventually(lambda: steps.runs)
    holding = CheckSet("slow", (MUTATION,), "holds_merges")
    runs.runs.use_sets((holding,))
    gate.set()
    await runs.ended()
    assert runs.heard == [] and runs.notices() == []

    runs.runs.run("slow", "nightly")
    await runs.ended(2)
    assert steps.runs == ["Mutation tests", "Soak", "Mutation tests"]
    assert runs.heard == [SetResult(holding, "base", "nightly", (MUTATION, "1 test failed"))]
    assert runs.runs.panel == VerificationChanged(
        steps=(StepState(name=MUTATION.name, status="failed"),),
        title="slow",
        output="1 test failed",
    )


# Firing again while it runs


async def test_firings_while_a_set_runs_fold_into_one_more_run_on_the_latest_commit() -> None:
    steps, gate = gated(True, True, True, True)
    runs = Runs(steps)
    runs.runs.run("slow", "slow")
    await eventually(lambda: steps.runs)
    runs.tree.base = "c2"
    runs.runs.run("slow", "slow")
    runs.runs.run("slow", "slow")
    gate.set()

    await runs.ended(2)
    await settled()
    assert runs.tree.snapshots == ["base", "c2"]
    assert len(runs.starts()) == 2
    assert steps.runs == ["Mutation tests", "Soak"] * 2


async def test_a_set_fired_after_its_run_ended_runs_again() -> None:
    runs = Runs(Steps(True, True, True, True))
    runs.runs.run("slow", "slow")
    await runs.ended()
    runs.runs.run("slow", "slow")
    await runs.ended(2)
    assert len(runs.starts()) == 2


# Halts, cancelling and closing


async def test_a_firing_while_halted_runs_once_the_halt_lifts() -> None:
    runs = Runs(Steps(True, True))
    runs.halted = True
    runs.runs.run("slow", "slow")
    runs.runs.restart()  # Still halted.
    await settled()
    assert runs.events == []

    runs.halted = False
    runs.runs.restart()
    await runs.ended()
    runs.runs.restart()  # Nothing more is due.
    await settled()
    assert len(runs.starts()) == 1


async def test_stopping_cancels_the_run_going_and_the_run_due_after_it() -> None:
    steps, _ = gated(True)
    runs = Runs(steps)
    runs.runs.run("slow", "slow")
    await eventually(lambda: steps.runs)
    runs.runs.run("slow", "slow")

    await runs.runs.stop()
    await settled()
    assert runs.events[-3:] == [
        shown("cancelled", "pending"),
        Notice(message=cancelled_note("slow")),
        AgentExited(agent_id="set-slow", reason="killed"),
    ]
    assert len(runs.starts()) == 1
    assert runs.tree.dropped == [SNAPSHOT]
    assert runs.runs.failures == {}


async def test_a_run_cancelled_after_a_failure_shows_no_output() -> None:
    steps = Steps(False, True)
    runs = Runs(steps)
    runs.runs.run("slow", "slow")
    await runs.ended()
    gate = asyncio.Event()
    steps.gate = gate
    runs.runs.run("slow", "slow")
    await eventually(lambda: len(steps.runs) == 2)

    await runs.runs.stop()
    assert runs.runs.panel == shown("cancelled", "pending")


async def test_cancelling_a_run_while_it_sets_up_leaves_its_steps_pending() -> None:
    steps, _ = gated(True)
    tree = FakeTree()
    tree.setup = Setup(command=("uv", "sync"))
    runs = Runs(steps, tree=tree)
    runs.runs.run("slow", "slow")
    await eventually(lambda: steps.runs)

    await runs.runs.stop()
    assert steps.runs == ["setup"]
    assert runs.events[-3:] == [
        shown("pending", "pending"),
        Notice(message=cancelled_note("slow")),
        AgentExited(agent_id="set-slow", reason="killed"),
    ]


async def test_stopped_runs_start_again_as_their_trigger_fires() -> None:
    runs = Runs(Steps(True, True))
    await runs.runs.stop()
    runs.runs.run("slow", "slow")
    await runs.ended()
    assert runs.exits() == [AgentExited(agent_id="set-slow", reason="finished")]


async def test_cancelling_its_agent_stops_the_run_and_the_run_due_after_it() -> None:
    steps, _ = gated(True)
    runs = Runs(steps, SLOW, FAST)
    runs.runs.run("slow", "slow")
    await eventually(lambda: steps.runs)
    runs.runs.run("slow", "slow")

    assert not runs.runs.cancel("set-fast")  # Not going.
    assert runs.runs.cancel("set-slow")
    await runs.ended()
    await settled()
    assert runs.exits() == [AgentExited(agent_id="set-slow", reason="killed")]
    assert not runs.runs.cancel("set-slow")  # No longer going.


async def test_closed_runs_start_no_more() -> None:
    runs = Runs(Steps(True, True))
    await runs.runs.close()
    runs.runs.run("slow", "slow")
    await settled()
    assert runs.events == []


# The panel and the agents


async def test_the_panel_shows_the_set_whose_run_showed_its_progress_last_until_released() -> None:
    steps, gate = gated(True, True, True)
    runs = Runs(steps, SLOW, FAST)
    assert runs.runs.panel is None
    runs.runs.run("slow", "slow")
    await eventually(lambda: steps.runs)
    assert runs.runs.panel == shown("running", "pending")

    runs.runs.run("fast", "fast")
    await eventually(lambda: len(steps.runs) == 2)
    panel = runs.runs.panel
    assert panel is not None and panel.title == "fast"
    assert runs.runs.agents == (
        RunningAgent(agent_id="set-slow", role="checker", task_id="startup"),
        RunningAgent(agent_id="set-fast", role="checker", task_id="startup"),
    )

    runs.runs.release_panel()
    assert runs.runs.panel is None
    gate.set()
    await runs.ended(2)
    assert runs.runs.agents == ()
    assert runs.runs.panel is None


async def test_a_released_run_publishes_no_more_progress_and_its_output_in_its_notice() -> None:
    steps, gate = gated(False)
    runs = Runs(steps)
    runs.runs.run("slow", "slow")
    await eventually(lambda: steps.runs)
    runs.runs.release_panel()
    published = len(runs.events)

    gate.set()
    await runs.ended()
    assert runs.events[published:] == [
        Notice(message=set_failed_note("slow", MUTATION.name, "1 test failed")),
        AgentExited(agent_id="set-slow", reason="finished"),
    ]
    assert runs.runs.panel is None
    assert runs.runs.failures.keys() == {"slow"}


async def test_a_run_failing_while_it_shows_keeps_its_output_in_its_notice_once_released() -> None:
    async def survived(step: VerificationStep, root: str) -> tuple[bool, str]:
        return False, "3 mutants survived"

    runs = Runs(survived)
    runs.runs.run("slow", "slow")
    await runs.ended()
    runs.runs.release_panel()

    assert runs.runs.panel is None
    assert runs.notices() == [
        "slow reported: Mutation tests failed. Its output ended:\n\n3 mutants survived"
    ]


async def test_a_run_starting_after_a_release_takes_the_panel_again() -> None:
    runs = Runs(Steps(True, True, True, True))
    runs.runs.run("slow", "slow")
    await runs.ended()
    runs.runs.release_panel()

    runs.runs.run("slow", "slow")
    await runs.ended(2)
    assert runs.runs.panel == shown("passed", "passed")
    assert runs.events[-2:] == [
        shown("passed", "passed"),
        AgentExited(agent_id="set-slow", reason="finished"),
    ]


# Spans


def attributes(span: ReadableSpan) -> Mapping[str, Any]:
    return dict(span.attributes or {})


def children(traced: Traced, parent: ReadableSpan) -> list[str]:
    """The names of the steps whose spans are children of ``parent``, in the order they
    started."""
    assert parent.context is not None
    steps = [
        span
        for span in traced.spans(STEP_SPAN)
        if span.parent is not None and span.parent.span_id == parent.context.span_id
    ]
    ordered = sorted(steps, key=lambda span: span.start_time or 0)
    return [str(attributes(span)["orbit.verify.step.name"]) for span in ordered]


async def test_a_run_is_a_root_span_naming_its_set_with_each_step_a_child() -> None:
    traced = Traced()
    runs = Runs(Steps(True, False), traced=traced)
    runs.runs.run("slow", "slow")
    await runs.ended()

    [span] = traced.spans(SET_SPAN)
    assert span.parent is None
    assert attributes(span) == {
        "orbit.workspace.id": "ws",
        "orbit.verify.set": "slow",
        "orbit.verify.result": "failed",
        "orbit.verify.failed_step": "Soak",
    }
    assert children(traced, span) == ["Mutation tests", "Soak"]


async def test_a_run_cancelled_is_traced_as_cancelled() -> None:
    traced = Traced()
    steps, _ = gated(True)
    runs = Runs(steps, traced=traced)
    runs.runs.run("slow", "slow")
    await eventually(lambda: steps.runs)
    await runs.runs.stop()

    [span] = traced.spans(SET_SPAN)
    assert attributes(span)["orbit.verify.result"] == "cancelled"
    assert "orbit.verify.failed_step" not in attributes(span)
