"""Running a task through an agent: start it, answer its calls, verify its work, stop it.

The agent is a separate process that reaches orbit through the :class:`AgentHost`. Its calls
arrive at the :class:`~orbit.agent.application.desk.AgentDesk`, which passes each to the
:class:`AgentRun` for that agent.
Verification runs in the run's own task, not in a call's, so it carries on if the agent
disconnects while waiting for the result. So does trying a proposal of checks, and asking the
user what they make of it, which a :class:`ProposalTrial` does.
"""

import asyncio
from collections.abc import Awaitable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal

from orbit.agent.application.desk import AgentDesk, Tool, dispatch, report_progress
from orbit.agent.application.ports import Publish, Reply
from orbit.agent.application.proposal_trial import ProposalTrial
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.verifier import Failure, Verifier, first_failure
from orbit.agent.domain.agent_task import FINISHED, AgentTask, Phase
from orbit.agent.domain.agents import Agent, bootstrap_prompt
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.checks import ChecksProposal, parse_proposal
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.endings import Ending, bug, error, run_ending, unaccepted
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.roles import (
    COMPLETE_TASK,
    GET_TASK,
    GIVE_UP,
    PROPOSE_CHECKS,
    REPORT_PROGRESS,
    RUN_CHECK,
    SUGGEST_TASK,
    Role,
    out_of_time,
    task_suggestion,
)
from orbit.agent.domain.verification import (
    VerificationStep,
    check_reply,
    failure_reason,
    step_list,
)
from orbit.protocol import (
    AGENT_FAILED,
    GIT_FAILED,
    NOT_CONFIGURED,
    TIMED_OUT,
    AgentExited,
    AgentExitReason,
    AgentStarted,
    ErrorOccurred,
    Event,
    TaskGaveUp,
    TaskStatus,
    TurnFinished,
    Usage,
)

Wake = Literal["exited", "gave_up"]
"""Why the run loop wakes up, other than a claim that the task is done."""

STATUSES: dict[Phase, TaskStatus] = {
    "assigned": "error",
    "working": "error",
    "verifying": "error",
    "done": "completed",
    "fallback": "unverified",
    "gave_up": "gave_up",
    "ended": "cancelled",
}


@dataclass(frozen=True)
class Outcome:
    """How a task ended: its status, the failure to hand a fresh agent if it fell back, the
    agent's summaries of what it did, one per claim, the checks the user accepted from it, and
    why it ended badly, in a line (``reason``) and in full (``detail``)."""

    status: TaskStatus
    failure: Failure | None = None
    summaries: tuple[str, ...] = ()
    checks: ChecksProposal | None = None
    reason: str = ""
    detail: str = ""


def ended(status: TaskStatus, why: Ending) -> Outcome:
    """A task's ending as ``status``, with nothing to show for it, for the reason ``why``."""
    reason, detail = why
    return Outcome(status, reason=reason, detail=detail)


async def see_to_end(
    tasks: Tasks, task_id: str, default: Outcome, outcome: Awaitable[Outcome]
) -> Outcome:
    """The ``outcome`` task ``task_id`` of ``tasks`` reached, recorded as its end. It ends as
    ``default`` if ``outcome`` is cancelled, and in error, re-raised, if ``outcome`` breaks on a
    bug: a task left running could never be run or dropped."""
    reached = default
    try:
        reached = await outcome
    except Exception as exc:
        reached = ended("error", bug(exc))
        raise
    finally:
        await tasks.finish(task_id, reached.status, reached.reason, reached.detail)
    return reached


async def tree_failed(publish: Publish, exc: RuntimeError) -> Outcome:
    """A task's ending in error because git or its tree failed with ``exc``, published with
    ``publish``."""
    await publish(ErrorOccurred(code=GIT_FAILED, message=str(exc)))
    return ended("error", error(str(exc)))


@dataclass(frozen=True)
class _Proposal:
    """Checks the agent proposed, and the call waiting for orbit's answer."""

    checks: ChecksProposal
    answer: "asyncio.Future[Reply]"


def _answer(claim: "asyncio.Future[Reply] | None", reply: Reply) -> None:
    """Answer ``claim`` with ``reply``, unless there's no claim or it was answered already."""
    if claim is not None and not claim.done():
        claim.set_result(reply)


def _cancel(claim: "asyncio.Future[Reply] | None") -> None:
    """Answer ``claim``, if it's still waiting, with the reply that the task was cancelled."""
    _answer(claim, Reply("The task was cancelled.", is_error=True))


def _cancelling() -> bool:
    """Whether the task running now is being cancelled."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _fail(claim: "asyncio.Future[Reply] | None", exc: BaseException) -> None:
    """Answer ``claim``, if it's still waiting, with why orbit stopped checking: it was
    cancelled, or ``exc`` went wrong."""
    if isinstance(exc, asyncio.CancelledError):
        _cancel(claim)
    else:
        _answer(claim, Reply(f"Orbit couldn't check your work: {exc}", is_error=True))


class AgentRun:
    """One agent working on task ``task_id`` of ``tasks``, from launch until it exits."""

    def __init__(
        self, task_id: str, tasks: Tasks, verifier: Verifier | None, desk: AgentDesk
    ) -> None:
        self._task_id = task_id
        self._tasks = tasks
        self._board = tasks.board
        self._verifier = verifier
        self._desk = desk
        self._publish = self._noting
        self._error: str | None = None  # The first error published while the agent ran.
        self._given_up = ""  # Why the agent gave up, if it did.
        self._work = AgentTask(self._board.get(task_id).task, desk.agents.limits)
        self._role: Role | None = desk.agents.roles.get(self._work.task.role)
        self._stream_task: asyncio.Task[None] | None = None  # The agent's process, once launched.
        self._inbox: asyncio.Queue[asyncio.Future[Reply] | _Proposal | Wake] = asyncio.Queue()
        self._checking = asyncio.Lock()  # One verification step at a time.
        self._checks: set[asyncio.Task[tuple[bool, str]]] = set()  # The agent's, in flight.
        self._failure: Failure | None = None
        self._failed_state: object = None  # The tree when verification last failed.
        self._accepted: ChecksProposal | None = None
        # How the last proposal failed when tried; never a fix task's failure, so kept apart.
        self._proposal_failure: Failure | None = None
        self._agent: Agent | None = None
        self._root = desk.agents.root
        self._tools: dict[str, Tool] = {
            GET_TASK: self._get_task,
            COMPLETE_TASK: self._complete_task,
            RUN_CHECK: self._run_check,
            REPORT_PROGRESS: self._report_progress,
            GIVE_UP: self._give_up,
            SUGGEST_TASK: self._suggest_task,
            PROPOSE_CHECKS: self._propose_checks,
        }

    @property
    def agent(self) -> Agent:
        assert self._agent is not None  # Set when the run starts.
        return self._agent

    @property
    def role(self) -> Role:
        assert self._role is not None  # A run without its role ends before the agent starts.
        return self._role

    async def run(self, before: object, root: str) -> Outcome:
        """Launch the agent in ``root`` and see the task through; ``before`` is the tree's
        state there beforehand."""
        task = self._work.task
        if self._role is None:
            message = f"No role named {task.role!r}."
            await self._publish(ErrorOccurred(code=NOT_CONFIGURED, message=message))
            return ended("error", error(message))
        self._root = root
        self._agent = self._desk.open(self, task.role, self._task_id)
        try:
            await self._see_through(before)
        finally:
            self._desk.close(self.agent)  # Even if the agent never got as far as starting.
            self._drain()
            await self._end_checks()
        status = STATUSES[self._work.phase]
        reason, detail = self._why(status)
        summaries = tuple(self._work.summaries)
        return Outcome(status, self._failure, summaries, self._accepted, reason, detail)

    def _why(self, status: TaskStatus) -> Ending:
        """Why the task ended as ``status``: the step that failed, if it fell back for one, its
        proposal that was never accepted, if it fell back without one, or else how the run
        ended."""
        if status == "unverified" and self._failure is not None:
            return failure_reason(*self._failure)
        if status == "unverified":
            return unaccepted(self._proposal_failure)
        return run_ending(status, self._work.phase, self._given_up, self._error)

    async def _noting(self, event: Event) -> None:
        """Publish ``event``, keeping the first error's message to say why the run ended."""
        if isinstance(event, ErrorOccurred) and self._error is None:
            self._error = event.message
        await self._desk.publish(event)

    async def _see_through(self, before: object) -> None:
        """Launch the agent and serve it until the task is finished, then stop it."""
        stream = self._stream_task = await self._launch()
        try:
            await self._serve_in_time(before)
            if self._work.phase in FINISHED:
                await self._await_exit(stream)
        finally:
            await self._stop(stream)

    async def _launch(self) -> "asyncio.Task[None]":
        await self._desk.url()  # Started before the agent is announced.
        role = self._work.task.role
        await self._publish(AgentStarted(agent_id=self.agent.id, role=role, task_id=self._task_id))
        return asyncio.create_task(self._stream())

    async def _serve_in_time(self, before: object) -> None:
        """Serve the agent until it's done, or its role's time runs out."""
        minutes = self.role.timeout_minutes
        try:
            async with asyncio.timeout(self.role.timeout_seconds) as deadline:
                await self._serve(before)
        except TimeoutError:
            # Raised while serving, not the role's time running out.
            if minutes is None or not deadline.expired():
                raise
            message = out_of_time(self.agent.id, minutes)
            await self._publish(
                ErrorOccurred(agent_id=self.agent.id, code=TIMED_OUT, message=message)
            )

    async def handle(self, tool: str, arguments: dict[str, Any]) -> Reply:
        return await dispatch(self._work.task.role, self.role, self._tools, tool, arguments)

    # The agent's process

    async def _stream(self) -> None:
        """Publish what the agent does until its process ends, then wake the run loop."""
        reason: AgentExitReason = "crashed"
        try:
            reason = await self._follow()
        except asyncio.CancelledError:
            reason = "killed"
            raise
        except Exception as exc:
            await self._publish(
                ErrorOccurred(agent_id=self.agent.id, code=AGENT_FAILED, message=str(exc))
            )
        finally:
            await self._publish(AgentExited(agent_id=self.agent.id, reason=reason))
            self._inbox.put_nowait("exited")

    async def _follow(self) -> AgentExitReason:
        reason: AgentExitReason = "finished"
        launch = await self._desk.launch(
            self.agent, bootstrap_prompt(self.agent), self._work.task.settings, self._root
        )
        events = self._desk.agents.launcher.run_agent(launch)
        async for event in events:
            if isinstance(event, TurnFinished) and event.reason == "error":
                reason = "crashed"
            await self._publish(event)
            if isinstance(event, TurnFinished) and event.usage is not None:
                await self._count(event.usage)
        return reason

    async def _count(self, usage: Usage) -> None:
        """Add what one of the agent's turns cost to its task's total, unless the task was
        removed once it finished: its last turn's usage arrives after it's completed."""
        await self._tasks.change(
            lambda board: board.add_usage(self._task_id, usage), refused=self._uncounted
        )

    async def _uncounted(self, error: Refused) -> None:
        """Act on a turn's usage the board refused for a removed task: what the session spent
        still changed."""
        await self._tasks.changed_elsewhere()

    async def _await_exit(self, stream: "asyncio.Task[None]") -> None:
        """Give a finished agent ``exit_grace`` seconds to exit on its own."""
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(stream), self._desk.agents.exit_grace)

    async def _stop(self, stream: "asyncio.Task[None]") -> None:
        stream.cancel()
        with suppress(asyncio.CancelledError):
            await stream

    # The run loop

    async def _serve(self, before: object) -> None:
        """Handle claims until the work is finished or the agent has gone."""
        while self._work.phase not in FINISHED:
            item = await self._inbox.get()
            if item == "exited":
                await self._exited(before)
                return
            if isinstance(item, asyncio.Future):
                await self._verify(before, item)
            elif isinstance(item, _Proposal):
                await self._consider(item)

    async def _end_checks(self) -> None:
        """Stop the checks the agent's calls are still running: once the run is over, its tree
        is the next task's."""
        for check in self._checks:
            check.cancel()
        await asyncio.gather(*self._checks, return_exceptions=True)

    def _drain(self) -> None:
        """Answer the calls left in the inbox once the run loop has stopped taking them."""
        while not self._inbox.empty():
            item = self._inbox.get_nowait()
            if isinstance(item, asyncio.Future):
                _cancel(item)
            elif isinstance(item, _Proposal):
                _cancel(item.answer)

    async def _exited(self, before: object) -> None:
        if not self._work.exited():
            message = f"{self.agent.id} exited without calling get_task."
            await self._publish(
                ErrorOccurred(agent_id=self.agent.id, code=AGENT_FAILED, message=message)
            )
        elif await self._unchanged_since_failure():
            assert self._verifier is not None and self._failure is not None
            await self._verifier.skip_unchanged(self._task_id)
            self._work.failed(*self._failure)  # The agent is gone, so this falls back.
        elif self.role.proposes_checks:
            self._work.unproposed()  # Its work is a proposal, and none was accepted.
        else:
            await self._verify(before, None)

    async def _unchanged_since_failure(self) -> bool:
        """Whether the agent stopped after a failed verification without changing anything."""
        if self._failed_state is None:
            return False
        assert self._verifier is not None  # Only a verification failure records a state.
        return await self._verifier.fingerprint(self._root) == self._failed_state

    async def _verify(self, before: object, claim: "asyncio.Future[Reply] | None") -> None:
        """Check the work, then answer ``claim`` (if the agent made one) with the verdict."""
        try:
            failure = await self._check(before)
            reply = self._work.passed() if failure is None else await self._failed(failure)
        except BaseException as exc:
            _fail(claim, exc)
            raise
        _answer(claim, Reply(reply))

    async def _check(self, before: object) -> Failure | None:
        if self._verifier is None or not self.role.verify:
            return None
        async with self._checking:
            return await self._verifier.check(self._task_id, before, self._root)

    async def _failed(self, failure: Failure) -> str:
        reply = self._work.failed(*failure, can_check=self.role.runs_checks)
        self._failure = failure
        if self._work.phase == "working":
            assert self._verifier is not None  # Only verification fails.
            self._failed_state = await self._verifier.fingerprint(self._root)
            await self._tasks.change(self._retry)
        return reply

    def _retry(self, board: TaskBoard) -> None:
        """Have the task running again on ``board``, on its next attempt."""
        board.set_attempt(self._task_id, self._work.attempt, self._work.limits.max_attempts)
        board.set_status(self._task_id, "running")

    async def _consider(self, proposal: _Proposal) -> None:
        """Try ``proposal``, have the user decide on it if it works, and answer the agent."""
        try:
            verdict = await self._trial().decide(proposal.checks)
        except BaseException as exc:
            _fail(proposal.answer, exc)
            raise
        if verdict.accepted is not None:
            self._accepted = verdict.accepted
        self._proposal_failure = verdict.failure
        _answer(proposal.answer, Reply(verdict.reply))

    def _trial(self) -> ProposalTrial:
        review = self._desk.agents.proposal_review
        assert review is not None  # Proposals are refused without one.
        assert self._stream_task is not None  # Proposals come from a launched agent.
        ask = partial(review.review, self._task_id)
        return ProposalTrial(self._work, self._run_steps, ask, self._stream_task)

    async def _run_steps(self, steps: Sequence[VerificationStep]) -> Failure | None:
        """Run ``steps`` in the agent's tree until one fails; returns that failure."""
        assert self._verifier is not None  # Proposals are refused without one.
        async with self._checking:
            return await first_failure(steps, self._verifier.run_one, self._root)

    # Tools

    async def _get_task(self, arguments: dict[str, Any]) -> Reply:
        steps: list[str] = []
        if self._verifier is not None and self.role.verify:
            steps = [step.name for step in self._verifier.verification.steps]
        return Reply(self._work.get_task(steps, self.role.runs_checks))

    async def _complete_task(self, arguments: dict[str, Any]) -> Reply:
        claim = self._work.claim_done(str(arguments.get("summary", "")))
        if not claim.verify:
            return Reply(claim.reply or "", is_error=True)
        answer: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        self._inbox.put_nowait(answer)
        # Shielded: if the agent disconnects, the claim is still verified.
        return await asyncio.shield(answer)

    async def _run_check(self, arguments: dict[str, Any]) -> Reply:
        if self._verifier is None:
            return Reply("No verification steps are configured.", is_error=True)
        steps = {step.name: step for step in self._verifier.verification.steps}
        step = steps.get(str(arguments.get("step", "")))
        if step is None:
            names = step_list(tuple(steps))
            return Reply(f"No step by that name; the steps are: {names}.", is_error=True)
        refusal = self._work.request_check()
        if refusal is not None:
            return Reply(refusal, is_error=True)
        return await self._checked(step)

    async def _checked(self, step: VerificationStep) -> Reply:
        """The result of running ``step`` for the agent, in a task the run stops if it ends
        first."""
        check = asyncio.create_task(self._run_one(step))
        self._checks.add(check)
        try:
            passed, output = await check
        except asyncio.CancelledError:
            if _cancelling():  # The call was cancelled, not the run ended.
                raise
            return Reply("The run ended before the check finished.", is_error=True)
        finally:
            self._checks.discard(check)
        return Reply(check_reply(step, passed, output))

    async def _run_one(self, step: VerificationStep) -> tuple[bool, str]:
        assert self._verifier is not None  # Checks are refused without one.
        async with self._checking:
            return await self._verifier.run_one(step, self._root)

    async def _report_progress(self, arguments: dict[str, Any]) -> Reply:
        return await report_progress(self._publish, self.agent.id, self._task_id, arguments)

    async def _suggest_task(self, arguments: dict[str, Any]) -> Reply:
        if self._desk.suggest is None:
            return Reply("Orbit isn't taking suggestions.", is_error=True)
        suggestion = task_suggestion(arguments)
        if isinstance(suggestion, str):
            return Reply(suggestion, is_error=True)
        title, prompt = suggestion
        return await self._desk.suggest(self._task_id, title, prompt)

    async def _propose_checks(self, arguments: dict[str, Any]) -> Reply:
        if self._verifier is None or self._desk.agents.proposal_review is None:
            return Reply("Orbit isn't taking check proposals.", is_error=True)
        try:
            checks = parse_proposal(arguments)
        except ConfigError as exc:
            return Reply(f"Invalid proposal: {exc}", is_error=True)
        refusal = self._work.propose()
        if refusal is not None:
            return Reply(refusal, is_error=True)
        answer: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        self._inbox.put_nowait(_Proposal(checks, answer))
        # Shielded, like a claim: the user still decides if the agent disconnects.
        return await asyncio.shield(answer)

    async def _give_up(self, arguments: dict[str, Any]) -> Reply:
        reason = str(arguments.get("reason", ""))
        reply = self._work.give_up(reason)
        if self._work.phase != "gave_up":
            return Reply(reply, is_error=True)
        self._given_up = reason
        await self._publish(
            TaskGaveUp(agent_id=self.agent.id, task_id=self._task_id, reason=reason)
        )
        self._inbox.put_nowait("gave_up")
        return Reply(reply)
