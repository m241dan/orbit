"""Firing triggers: the events of a task's end raised, and each trigger listening for one queuing
its role's task on the board, its prompt rendered from its template, unless a guard holds it
back.

The ``review-rounds`` guard is today's review planner: the review lane finds the review due on a
landing, which the triggers it guards review, and books each review they queue into its batches.
A batch's review falls due as the batch is done rather than as one task lands, so it fires the
triggers ``review-rounds`` guards on ``task_landed`` with no task causing it, as 12d's
``batch_landed`` will.

The review lane counts each landing toward the ``commits`` triggers too, as their guards see the
board before the landing's own review is queued, and has those that fire, or one fired by hand,
review the whole codebase at the commit, each task booked into that review's batches.

The host raises ``session_started`` as it opens the workspace with its session, whose triggers
with ``checks = "task-checks"`` run the startup check, and the startup check raises
``startup_check_failed`` for the step that failed. ctrl+e fires ``configure-checks`` by hand, on
``manual``. A trigger with ``checks`` naming a set has it run, in the background, on whatever
event it fires, or by hand.

While verification is missing, every task a trigger queues waits for it to be set up, as the
session's gate says, with a notice naming its trigger, but a configurator's, which goes first.
"""

from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from functools import partial

from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lanes import OPEN_GATE, SessionGate
from orbit.agent.application.ports import TreeFailed
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.checks import configured_checks
from orbit.agent.domain.reviews import (
    ReviewJob,
    ReviewTemplate,
    commit_review,
    pending_titles,
    review_skills,
    review_values,
)
from orbit.agent.domain.roles import enabled_roles, role_task
from orbit.agent.domain.setup_first import waiting_for_setup, waiting_note
from orbit.agent.domain.triggers import (
    COMMITS,
    REVIEW_ROUNDS,
    SESSION_STARTED,
    STARTUP_CHECK_FAILED,
    TASK_FINISHED,
    TASK_LANDED,
    TRIGGER_VARIABLE,
    Counted,
    Trigger,
    TriggerEvent,
    count_landing,
    fires_by_hand,
    held_back,
    listening,
    render_template,
    verification_state,
)
from orbit.agent.domain.verification import CheckSet, VerificationStep, failure_report
from orbit.protocol import (
    CODER,
    GIT_FAILED,
    ErrorOccurred,
    Notice,
    QueuedAs,
    TaskSpec,
    TaskState,
)

Values = Callable[[], Awaitable[Mapping[str, str] | None]]
"""What gathers an event's template values, but the trigger's name, once a trigger may fire on
it; none if they can't be gathered, when none fires."""

Kick = Callable[[str], None]
"""What runs the tasks waiting in the lane of a role, by its name, if that lane starts by
itself."""

RunChecks = Callable[[str, str], None]
"""What runs the checks a trigger's ``checks`` names, by that name, as the trigger named second
fired: the task checks, as the startup check, for ``"task-checks"``, or else the named set, in
the background."""

REVIEW_SKIPPED = "Review skipped"
TRIGGERS_SKIPPED = "Triggers skipped"
"""What an error says was skipped when git can't show the diff of what landed: a review that
was due, or the other triggers on the landing."""


@dataclass(frozen=True)
class ReviewRound:
    """A review the ``review-rounds`` guard finds due: ``job``, and ``book``, which records the
    review task queued for it, by its id, with the template and values its prompt was rendered
    from, opening the batch its suggestions join."""

    job: ReviewJob
    book: Callable[[str, ReviewTemplate], None]


@dataclass(frozen=True)
class _Firing:
    """The triggers ``listening`` for an event, by name, in order, with what they render their
    tasks from: its ``values`` and the review due, ``review``, if any. On a landing ``counted``
    toward the ``commits`` triggers, or fired by hand, their guards were decided already, and
    each of them reviews ``review``; otherwise only those ``review-rounds`` guards do."""

    listening: list[tuple[str, Trigger]]
    values: Mapping[str, str]
    review: ReviewRound | None
    counted: bool = False

    def review_of(self, trigger: Trigger) -> ReviewRound | None:
        """The review ``trigger``'s task reviews, if any."""
        reviews = self.counted or REVIEW_ROUNDS in trigger.guards
        return self.review if reviews else None


def _no_kick(role: str) -> None:
    """Runs no lane."""


def _no_checks(checks: str, trigger: str) -> None:
    """Runs no checks."""


@dataclass(frozen=True)
class TriggerHooks:
    """What a :class:`TriggerEngine` acts through: ``kick``, which runs the tasks waiting in a
    role's lane if it starts by itself, ``run_checks``, which runs the checks a trigger with
    ``checks`` names, and the session's ``gate``, which says what verification is."""

    kick: Kick = _no_kick
    run_checks: RunChecks = _no_checks
    gate: SessionGate = OPEN_GATE


_NO_HOOKS = TriggerHooks()


async def _no_values() -> Mapping[str, str]:
    """The values of an event that gives none but the trigger's name."""
    return {}


async def _given(values: Mapping[str, str]) -> Mapping[str, str]:
    """``values``, an event's values, known as it's raised."""
    return values


class TriggerEngine:
    """Fires the triggers of ``desk``'s agents on events, as they stand as each is raised,
    queuing their tasks on ``tasks``' board, or for those with ``checks`` having the ``hooks``'
    ``run_checks`` run the checks they name, and runs those tasks when asked, with
    their ``kick``, each in its role's lane if that starts by itself. While their ``gate`` says
    verification is missing, each task it queues but a configurator's is noted as waiting.

    What raises an event runs the tasks it queued once it has done with them, as a lane runs the
    tasks it queues itself."""

    def __init__(self, desk: AgentDesk, tasks: Tasks, hooks: TriggerHooks = _NO_HOOKS) -> None:
        self._desk = desk
        self._tasks = tasks
        self._kick = hooks.kick
        self._run_checks = hooks.run_checks
        self._gate = hooks.gate

    async def started(
        self, steps: Sequence[VerificationStep] | None, sets: Sequence[CheckSet] = ()
    ) -> tuple[str, ...]:
        """Raise ``session_started``: orbit opened the workspace with its session, verifying
        tasks with ``steps``, the task checks, none while verification is off, which give its
        ``verification`` and, with the named ``sets`` and the tree's setup, its ``{checks}``.
        Those of its triggers with ``checks = "task-checks"`` run the steps, and those naming a
        set run it. Returns the ids of the tasks the others queued."""
        fields = {"verification": verification_state(steps)}
        event = TriggerEvent(SESSION_STARTED, fields)
        return await self.fire(event, partial(_given, self._checks(steps or (), sets)))

    async def fire_by_hand(
        self, name: str, steps: Sequence[VerificationStep], sets: Sequence[CheckSet] = ()
    ) -> tuple[str, ...]:
        """Fire the trigger ``name`` by hand, if it fires so, as :meth:`by_hand` says, whatever
        its ``when`` and guards: its task is queued, a configurator's first, rendered from the
        variables of ``manual``, its ``{checks}`` the task checks ``steps``, the named ``sets``
        and the tree's setup configured now, or the set it names runs. The board is published if
        a task was queued. Returns its id, for :meth:`run`; none if it doesn't fire or queues no
        task."""
        fired = self.by_hand(name)
        if not fired:
            return ()
        return await self._queue(_Firing(fired, self._checks(steps, sets), None, counted=True))

    def _checks(
        self, steps: Sequence[VerificationStep], sets: Sequence[CheckSet]
    ) -> Mapping[str, str]:
        """The ``{checks}`` of the task checks ``steps``, the named ``sets`` with the triggers
        that run them, and the tree's setup configured now, which a configurator is told."""
        tree = self._desk.agents.tree
        setup = None if tree is None else tree.setup
        return {"checks": configured_checks(steps, setup, sets, self._desk.agents.triggers)}

    async def check_failed(self, step: VerificationStep, output: str) -> tuple[str, ...]:
        """Raise ``startup_check_failed``: ``step`` of the startup check failed with ``output``,
        which its ``{output}`` reports with the command that failed. Returns the ids of the
        tasks queued."""
        values = {"step": step.name, "output": failure_report(step, output)}
        event = TriggerEvent(STARTUP_CHECK_FAILED, {"step": step.name})
        return await self.fire(event, partial(_given, values))

    async def landed(
        self, state: TaskState, commit: str, review: ReviewRound | None = None
    ) -> tuple[str, ...]:
        """Raise ``task_landed``: the task that ended as ``state`` landed ``commit``, whose own
        review, if due, is ``review``. Returns the ids of the tasks queued."""
        fields = {"role": state.task.role, "origin": state.origin}
        event = TriggerEvent(TASK_LANDED, fields, state)
        skipped = TRIGGERS_SKIPPED if review is None else REVIEW_SKIPPED
        job = commit_review(state.id, commit)
        values = partial(self._values, job, state.task.skills, skipped)
        return await self.fire(event, values, review)

    async def finished(self, state: TaskState) -> tuple[str, ...]:
        """Raise ``task_finished``: the task that was ``state`` ended as its status says.
        Returns the ids of the tasks queued."""
        fields = {"role": state.task.role, "origin": state.origin, "status": state.status}
        return await self.fire(TriggerEvent(TASK_FINISHED, fields, state), _no_values)

    async def reviewed(self, review: ReviewRound) -> tuple[str, ...]:
        """Fire the triggers ``review-rounds`` guards on ``task_landed`` for ``review``, of a
        batch that's done, which no one task caused. Returns the ids of the tasks queued."""
        firing = await self._batch_firing(review)
        return () if firing is None else await self._queue(firing)

    async def reviewed_quietly(self, review: ReviewRound) -> None:
        """Fire the triggers ``review-rounds`` guards for ``review`` as :meth:`reviewed` does,
        their tasks queued on the board without publishing it or running them."""
        firing = await self._batch_firing(review)
        if firing is not None:
            self._add(self._tasks.board, firing)

    async def fire(
        self, event: TriggerEvent, values: Values, review: ReviewRound | None = None
    ) -> tuple[str, ...]:
        """Fire the triggers listening for ``event``, with ``review`` the review due on it, if
        any, as :func:`~orbit.agent.domain.triggers.listening` says: in their order, each that
        no guard holds back queues its role's task, a draft if it says so, from it, rendered
        from the ``values`` gathered and its name, or runs the checks its ``checks`` names. The
        board is published if any task was queued. Returns their ids, for :meth:`run`."""
        firing = await self._firing(self._desk.agents.triggers, event, values, review)
        return () if firing is None else await self._queue(firing)

    def count(self, state: TaskState, counts: Mapping[str, int]) -> Counted:
        """Count the landing of the task that ended as ``state`` toward the ``commits``
        triggers, from ``counts``, the landings counted toward each since it last fired, by
        name, as :func:`~orbit.agent.domain.triggers.count_landing` says, their guards seeing
        the board as it stands; returns the counts after it and the triggers that fire, for
        :meth:`fire_counted`."""
        fields = {"role": state.task.role, "origin": state.origin}
        event = TriggerEvent(COMMITS, fields, state)
        roles = enabled_roles(self._desk.agents.roles)
        board = self._tasks.board.tasks
        return count_landing(self._desk.agents.triggers, event, roles, counts, board)

    def fire_checks(self, fired: Sequence[tuple[str, Trigger]]) -> list[tuple[str, Trigger]]:
        """Run the checks of each of the triggers ``fired``, by name, on a landing counted, that
        runs them, their guards decided already; returns the others, which queue tasks, in
        order, for :meth:`fire_counted`."""
        for name, trigger in fired:
            if trigger.checks is not None:
                self._run_checks(trigger.checks, name)
        return [(name, trigger) for name, trigger in fired if trigger.checks is None]

    def by_hand(self, name: str) -> list[tuple[str, Trigger]]:
        """The trigger ``name``, by name, if it fires when fired by hand, as
        :func:`~orbit.agent.domain.triggers.fires_by_hand` says, for :meth:`fire_counted`; none
        otherwise."""
        trigger = self._desk.agents.triggers.get(name)
        roles = enabled_roles(self._desk.agents.roles)
        return [] if trigger is None or not fires_by_hand(trigger, roles) else [(name, trigger)]

    async def fire_counted(
        self, fired: Sequence[tuple[str, Trigger]], values: Mapping[str, str], review: ReviewRound
    ) -> tuple[str, ...]:
        """Queue the tasks of the triggers ``fired``, by name, in order, on a landing counted
        or by hand, their guards decided already, each rendered from ``values`` and its name,
        reviewing ``review``, which books it. The board is published if any was queued.
        Returns their ids, for :meth:`run`."""
        return await self._queue(_Firing(list(fired), values, review, counted=True))

    def run(self, task_ids: Collection[str]) -> None:
        """Run the tasks ``task_ids`` that are still on the board, each in its role's lane if
        that starts by itself."""
        board = self._tasks.board
        found = (board.find(task_id) for task_id in task_ids)
        roles = dict.fromkeys(state.task.role for state in found if state is not None)
        for role in roles:
            self._kick(role)

    async def _batch_firing(self, review: ReviewRound) -> _Firing | None:
        """What the triggers ``review-rounds`` guards fire on for ``review``, the review of a
        batch's coder tasks, from what it reviews; none if none does."""
        job = review.job
        triggers = {
            name: trigger
            for name, trigger in self._desk.agents.triggers.items()
            if REVIEW_ROUNDS in trigger.guards
        }
        event = TriggerEvent(TASK_LANDED, {"role": CODER, "origin": job.origin})
        skills = review_skills(job, self._tasks.board.tasks)
        return await self._firing(
            triggers, event, partial(self._values, job, skills, REVIEW_SKIPPED), review
        )

    async def _firing(
        self,
        triggers: Mapping[str, Trigger],
        event: TriggerEvent,
        values: Values,
        review: ReviewRound | None,
    ) -> _Firing | None:
        """The ``triggers`` that may fire on ``event``, with ``review`` the review due, if any,
        and the ``values`` they render from, gathered only if one may; none if none may, or the
        values can't be gathered."""
        roles = enabled_roles(self._desk.agents.roles)
        found = listening(triggers, event, roles, None if review is None else review.job)
        given = await values() if found else None
        return None if given is None else _Firing(found, given, review)

    async def _queue(self, firing: _Firing) -> tuple[str, ...]:
        """Queue the tasks of the triggers ``firing`` that no guard holds back, publishing the
        board if any were, then noting each that waits for verification to be set up; returns
        their ids."""
        queued = self._add(self._tasks.board, firing)
        if queued:
            await self._tasks.publish()
            await self._note_waiting(queued)
        return queued

    async def _note_waiting(self, task_ids: Sequence[str]) -> None:
        """Say of each of the tasks ``task_ids``, just queued, that waits for verification to be
        set up, as the gate says it is, that it waits, naming the trigger that queued it, its
        origin."""
        board = self._tasks.board
        states = [board.get(task_id) for task_id in task_ids]
        waiting = waiting_for_setup(self._gate.verification(), states)
        for state in states:
            if state.id in waiting:
                await self._desk.publish(Notice(message=waiting_note(state.origin)))

    def _add(self, board: TaskBoard, firing: _Firing) -> tuple[str, ...]:
        """Add to ``board`` the task of each of the triggers ``firing`` that no guard holds
        back, in order, each seeing those before it, running the checks for each with
        ``checks``; returns their ids."""
        return tuple(
            task_id
            for name, trigger in firing.listening
            if (task_id := self._fire_one(board, name, trigger, firing)) is not None
        )

    def _fire_one(
        self, board: TaskBoard, name: str, trigger: Trigger, firing: _Firing
    ) -> str | None:
        """Fire ``trigger``, named ``name``, as ``firing`` says: with ``checks``, run them
        unless a guard holds it back, with ``board``'s tasks, if they're still to decide, adding
        none; otherwise add its task to ``board`` as :meth:`_add_one` does, returning its id if
        it's added."""
        if trigger.checks is None:
            return self._add_one(board, name, trigger, firing)
        if firing.counted or not held_back(trigger, None, board.tasks):
            self._run_checks(trigger.checks, name)
        return None

    def _add_one(
        self, board: TaskBoard, name: str, trigger: Trigger, firing: _Firing
    ) -> str | None:
        """Add to ``board`` the task ``trigger``, named ``name``, queues as it fires as
        ``firing`` says, unless a guard holds it back, if they're still to decide; returns its
        id if it's added. One reviewing the review due does so with the skills of what it
        reviews, whatever its role, and is queued as a review, booked into its batches with what
        its prompt was rendered from."""
        assert trigger.role is not None  # It listened without checks, so it queues a role's task,
        assert trigger.prompt is not None  # with a prompt.
        values = {**firing.values, TRIGGER_VARIABLE: name}
        prompt = render_template(trigger.prompt, values)
        if not firing.counted and held_back(trigger, prompt, board.tasks):
            return None
        review = firing.review_of(trigger)
        skills = () if review is None else review_skills(review.job, board.tasks)
        task = role_task(trigger.role, self._desk.agents.roles[trigger.role], prompt, skills)
        task_id = _added(board, task, name, trigger, "task" if review is None else "review")
        if review is not None:
            review.book(task_id, ReviewTemplate(trigger.prompt, values))
        return task_id

    async def _values(
        self, job: ReviewJob, skills: Sequence[str], skipped: str
    ) -> Mapping[str, str] | None:
        """The values of ``task_landed`` for ``job``'s commits, landed by the tasks it reviews,
        which name ``skills``, as its review's task renders from them; none, with an error
        published saying what's ``skipped``, if git can't show their diff."""
        tree = self._desk.agents.tree
        assert tree is not None  # Only work in a tree lands.
        try:
            diff = await tree.show(job.commits)
        except TreeFailed as exc:  # Git failed; the triggers are skipped rather than the run.
            await self._desk.publish(ErrorOccurred(code=GIT_FAILED, message=f"{skipped}: {exc}"))
            return None
        cap = self._desk.agents.review_limits.suggestions
        pending = pending_titles(self._tasks.board.tasks)
        landing = {"task": ", ".join(job.reviewed), "task_skills": ", ".join(skills)}
        return review_values(job, diff, cap, pending) | landing


def _added(
    board: TaskBoard, task: TaskSpec, name: str, trigger: Trigger, queued_as: QueuedAs
) -> str:
    """Add ``task``, which ``trigger``, named ``name``, queued as ``queued_as``, to ``board``:
    as a draft if it says so; returns its id."""
    if trigger.draft:
        return board.add_draft(task, None, name, queued_as)
    return board.add_triggered(task, name, queued_as)
