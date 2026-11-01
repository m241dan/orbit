"""The named sets' runs: a set a trigger names with ``checks = "<set>"`` runs in the background,
in a read-only snapshot of the base's latest commit set up with the tree's setup, never in the
coder's tree (decision 13), so it never races with an agent there.

A run shows in the verification panel under its set's name, with the steps' states the startup
check shows, until a task's verification takes the panel over for the rest of that run, and in
the agents pane as a ``checker`` that can be cancelled. A trigger firing while its set runs
queues one more run, on the base's latest commit as it starts once that run ends; firings
meanwhile fold into it. As for the startup check, a halt cancels the runs, and the runs queued
with them, and a firing while halted runs once the halt lifts.

A run that passes adds nothing more; one that fails shows the failing step's output, trimmed,
in the panel, if it still has it, until the set's next run starts, and keeps its failure; one of
a reporting set adds a notice too, as :func:`~orbit.agent.domain.verification.set_failed_note`
words it, or, once a task's verification took the panel from the run,
:func:`~orbit.agent.domain.verification.set_failed_away_note`, quoting the output. How a run
of a set that holds merges came out, on which commit, is told to the site's ``heard``, which
holds merges while it fails. Each run is traced in an ``orbit.check_set`` span, as
:class:`SetTracing` says.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from orbit.agent.application.closing import close_all
from orbit.agent.application.lanes import Background
from orbit.agent.application.ports import CoderTree, Publish, StepRunner, TreeFailed
from orbit.agent.application.set_trace import SetTracing
from orbit.agent.application.snapshots import snapshot_of
from orbit.agent.application.startup_check import ROLE
from orbit.agent.application.verifier import Failure, first_failure, record_result
from orbit.agent.domain.checks import setup_step
from orbit.agent.domain.verification import (
    CheckSet,
    StepProgress,
    set_failed_away_note,
    set_failed_note,
    trim_output,
)
from orbit.protocol import (
    GIT_FAILED,
    STARTUP_TASK,
    AgentExited,
    AgentExitReason,
    AgentStarted,
    ErrorOccurred,
    Notice,
    RunningAgent,
    StepStatus,
    VerificationChanged,
)
from orbit.tracing import current_span

AGENT_PREFIX = "set-"
"""What the id of the agent a set's run shows as starts with, before the set's name."""


def set_agent(name: str) -> str:
    """The id of the agent a run of the set ``name`` shows as."""
    return f"{AGENT_PREFIX}{name}"


def cancelled_note(name: str) -> str:
    """The notice saying a run of the set ``name`` was cancelled."""
    return f"{name} cancelled."


@dataclass(frozen=True)
class SetResult:
    """How a run of ``check_set``, which the trigger named ``trigger`` fired, came out on
    ``commit``, the base's latest as it started: its ``failure``, if it failed."""

    check_set: CheckSet
    commit: str
    trigger: str
    failure: Failure | None


async def _unheard(result: SetResult) -> None:
    """Hear nothing of ``result``."""


@dataclass(frozen=True)
class SetSite:
    """Where and how the sets run: in snapshots of ``tree``'s base, each step through
    ``run_step``, publishing with ``publish``, while ``halted`` says the session isn't; ``heard``
    hears how each run of a set that holds merges came out."""

    tree: CoderTree
    run_step: StepRunner
    publish: Publish
    halted: Callable[[], bool]
    heard: Callable[[SetResult], Awaitable[None]] = _unheard


class SetRun:
    """Runs ``check_set``'s steps at ``site``, one run at a time, each traced with ``tracing``;
    ``shown`` hears the set's name each time a run shows its progress in the verification
    panel, which a run does from its start until :meth:`release_panel`. :attr:`failure` is the
    last run's failure, with its output, if it failed."""

    def __init__(
        self,
        check_set: CheckSet,
        site: SetSite,
        tracing: SetTracing,
        shown: Callable[[str], None],
    ) -> None:
        self._set = check_set
        self._site = site
        self._tracing = tracing
        self._shown = shown
        self._progress = StepProgress.of(step.name for step in check_set.steps)
        self._due = False  # Whether a run waits for the one running, or a halt, to end.
        self._announced = False  # Whether its start is published and its exit isn't.
        self._showing = False  # Whether the run going still shows in the panel.
        self._background = Background(f"{check_set.name} set", site.publish)
        self._background.ended = self.start_due
        self._trigger = ""  # The trigger that fired the run due or going, by name.
        self._output = ""  # The failing step's output, trimmed, once the run going failed.
        self.failure: Failure | None = None

    @property
    def agent_id(self) -> str:
        """The id of the agent its runs show as."""
        return set_agent(self._set.name)

    @property
    def running(self) -> bool:
        return self._background.running

    @property
    def agent(self) -> RunningAgent | None:
        """The run as a running agent, between publishing its start and its exit."""
        if not self._announced:
            return None
        return RunningAgent(agent_id=self.agent_id, role=ROLE, task_id=STARTUP_TASK)

    @property
    def panel(self) -> VerificationChanged:
        """What it shows in the verification panel: its steps' progress, under its name, with
        the failing step's output once a run failed."""
        return VerificationChanged(
            steps=self._progress.steps, title=self._set.name, output=self._output
        )

    def fire(self, trigger: str) -> None:
        """Run the set, as the trigger named ``trigger`` fired: now, unless a run is going or
        the session is halted; else once that run ends or the halt lifts, as the one run due,
        fired by the trigger that fired last."""
        self._due = True
        self._trigger = trigger
        self.start_due()

    def start_due(self) -> None:
        """Start the run due, if one is, unless one is going or the session is halted."""
        if not self._due or self._background.running or self._site.halted():
            return
        self._due = False
        self._background.start(self._run(self._trigger))

    def release_panel(self) -> None:
        """A task's verification takes the panel over: the run going, if any, shows no more of
        its progress there."""
        self._showing = False

    def cancel(self) -> None:
        """Cancel the run going, if any, and the run due after it."""
        self._due = False
        self._background.cancel()

    async def stop(self) -> None:
        """Cancel the run going, if any, and the run due after it, and wait for it to stop."""
        self._due = False
        await self._background.stop()

    async def close(self) -> None:
        """Stop the runs, as :meth:`stop` does, for good: none starts from now on."""
        self._due = False
        await self._background.close()

    async def _run(self, trigger: str) -> None:
        with self._tracing.run(self._set.name):
            await self._site.publish(
                AgentStarted(agent_id=self.agent_id, role=ROLE, task_id=STARTUP_TASK)
            )
            self._announced, self._showing = True, True
            self._progress, self._output = self._progress.reset(), ""
            await self._show()
            reason: AgentExitReason = "killed"
            try:
                reason = await self._check(trigger)
            finally:
                self._announced = False
                await self._site.publish(AgentExited(agent_id=self.agent_id, reason=reason))

    async def _check(self, trigger: str) -> AgentExitReason:
        """Run the steps in a snapshot and report how they came out, the run fired by the
        trigger named ``trigger``; returns how the run's agent exits."""
        try:
            commit, failure = await self._in_snapshot()
        except asyncio.CancelledError:
            await self._cancel_running()
            await self._site.publish(Notice(message=cancelled_note(self._set.name)))
            raise
        except TreeFailed as exc:  # Git couldn't find the base or make the snapshot.
            message = f"{self._set.name} couldn't run: {exc}"
            current_span().error(message, exc)
            await self._site.publish(ErrorOccurred(code=GIT_FAILED, message=message))
            return "crashed"
        await self._report(SetResult(self._set, commit, trigger, failure))
        return "finished"

    async def _in_snapshot(self) -> tuple[str, Failure | None]:
        """Run the steps in a snapshot of the base's latest commit; returns that commit and the
        first failure."""
        tree = self._site.tree
        commit = await tree.base_commit()
        async with snapshot_of(tree, commit, self._site.publish) as root:
            return commit, await self._verify(root)

    async def _verify(self, root: str) -> Failure | None:
        """Set up ``root`` with the tree's setup, if it has one, then run the steps there;
        returns the first failure."""
        runner = self._tracing.steps(self._site.run_step)
        setup = self._site.tree.setup
        if setup is not None:
            failure = await first_failure((setup_step(setup),), runner, root)
            if failure is not None:
                return failure
        return await first_failure(self._set.steps, runner, root, self._set_step)

    async def _report(self, result: SetResult) -> None:
        """Keep how the run came out, as ``result`` says, and tell of it: a failure's output in
        the panel, a reporting set's failure in a notice, a holding set's outcome to the site's
        ``heard``; then record it on the run's span."""
        failure = result.failure
        self.failure = failure
        if failure is not None:
            _, output = failure
            self._output = trim_output(output)
            await self._show()
        if self._set.effect == "holds_merges":
            await self._site.heard(result)
        elif failure is not None:
            await self._site.publish(Notice(message=self._failed_note(failure)))
        # Only once it's reported, so a run cancelled meanwhile names no failed step.
        record_result(failure)

    def _failed_note(self, failure: Failure) -> str:
        """The notice saying the run going failed as ``failure`` says: pointing at the panel
        while the run shows there, else quoting the failing output, as the panel shows a task's
        verification instead."""
        step, output = failure
        if self._showing:
            return set_failed_note(self._set.name, step.name)
        return set_failed_away_note(self._set.name, step.name, output)

    async def _cancel_running(self) -> None:
        """Mark the step running, if any, cancelled."""
        cancelled = self._progress.cancel_running()
        if cancelled != self._progress:
            self._progress = cancelled
            await self._show()

    async def _set_step(self, number: int, status: StepStatus) -> None:
        self._progress = self._progress.set(number, status)
        await self._show()

    async def _show(self) -> None:
        if not self._showing:
            return
        self._shown(self._set.name)
        await self._site.publish(self.panel)


_NO_TRACING = SetTracing()


class SetRuns:
    """The runs of the named ``sets`` at ``site``, a :class:`SetRun` each, traced with
    ``tracing``. The verification panel shows the set whose run showed its progress last, until
    a task's verification takes it over; only a run starting after that takes it back."""

    def __init__(
        self, sets: Sequence[CheckSet], site: SetSite, tracing: SetTracing = _NO_TRACING
    ) -> None:
        self._runs = {
            check_set.name: SetRun(check_set, site, tracing, self._show) for check_set in sets
        }
        self._shown: str | None = None  # The set whose run has the panel, if one has.

    def run(self, name: str, trigger: str) -> None:
        """Run the set ``name``, as the trigger named ``trigger`` fired, as :meth:`SetRun.fire`
        says; nothing if there's no such set."""
        run = self._runs.get(name)
        if run is not None:
            run.fire(trigger)

    def restart(self) -> None:
        """Start the runs due that a halt kept from starting, as it lifts."""
        for run in self._runs.values():
            run.start_due()

    def cancel(self, agent_id: str) -> bool:
        """Cancel the run going that shows as agent ``agent_id``, and the run due after it;
        returns whether one was going."""
        found = (run for run in self._runs.values() if run.running and run.agent_id == agent_id)
        run = next(found, None)
        if run is None:
            return False
        run.cancel()
        return True

    def release_panel(self) -> None:
        """A task's verification takes the panel over, until a run starts again: the runs going
        show no more of their progress there."""
        self._shown = None
        for run in self._runs.values():
            run.release_panel()

    @property
    def panel(self) -> VerificationChanged | None:
        """What the runs show in the verification panel; none while none has it."""
        return None if self._shown is None else self._runs[self._shown].panel

    @property
    def agents(self) -> tuple[RunningAgent, ...]:
        """The runs going, as running agents, in the sets' order."""
        return tuple(agent for run in self._runs.values() if (agent := run.agent) is not None)

    @property
    def failures(self) -> Mapping[str, Failure]:
        """The failure of each set whose last run failed, with its output, by name."""
        return {name: run.failure for name, run in self._runs.items() if run.failure is not None}

    async def stop(self) -> None:
        """Stop every run, as :meth:`SetRun.stop` does, and wait for them to stop."""
        await close_all([run.stop for run in self._runs.values()])

    async def close(self) -> None:
        """Stop every run for good, as :meth:`SetRun.close` does."""
        await close_all([run.close for run in self._runs.values()])

    def _show(self, name: str) -> None:
        self._shown = name
