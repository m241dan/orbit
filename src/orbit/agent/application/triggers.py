"""Firing triggers: the events of a task's end raised, and each trigger listening for one queuing
its role's task on the board, or a task for each enabled role of its list, its prompt rendered
once from its template, unless a guard holds it back.

The ``review-rounds`` guard is today's review planner: the review lane finds the review due on a
landing, which the triggers it guards review, and books each task they queue into that review,
whose one batch its suggestions join. Once every finding role's task of a review has ended, the
review lane raises ``reviews_finished`` for it, each task a trigger on it queues booked into the
review too.
A batch's review falls due as the batch is done rather than as one task lands, so it fires the
triggers ``review-rounds`` guards on ``task_landed`` with no task causing it, its ``review``
field ``batch`` where a landing's is ``commit``. Of them, those ``raised-finding`` guards fire
only if their role raised one of the batch's findings, so a batch of fixes is reviewed narrowly
by the lenses whose findings it fixes; a trigger listing roles queues only for those of them
that raised one. A trigger with ``paths`` fires on a landing only if its commit, or one of the
batch's, changed a path one of them matches; those paths are asked of git only while such a
trigger listens, and if git can't list them it fires all the same, the failure on the span
current. One ``code-changed`` guards, without ``paths``, fires only if one of those paths is
code, and those it holds back are noted in the conversation, but as reviews are raised again
quietly.

The review lane counts each landing toward the ``commits`` triggers too, as their guards see the
board before the landing's own review is queued, and has those that fire, or one fired by hand,
review the whole codebase at the commit, each task booked into that review's batches.

The host raises ``session_started`` as it opens the workspace with its session, whose triggers
with ``checks = "task-checks"`` run the startup check, and the startup check raises
``startup_check_failed`` for the step that failed. ctrl+e fires ``configure-checks`` by hand, on
``manual``. The schedules raise ``schedule`` for the triggers whose cron expressions came due.
A trigger with ``checks`` naming a set has it run, in the background, on whatever event it
fires, or by hand. A named set's run ending raises ``set_finished``: each task of a
finding role its triggers queue is booked into one review of the set's report, as the review
lane asks, and any other role's is an ordinary task.

While verification is missing, every task a trigger queues waits for it to be set up, as the
session's gate says, with a notice naming its trigger, but a configurator's, which goes first.
"""

from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from typing import Literal

from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.diffs import show_diff
from orbit.agent.application.lanes import OPEN_GATE, SessionGate
from orbit.agent.application.ports import TreeFailed
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.checks import configured_checks
from orbit.agent.domain.reviews import (
    ReviewJob,
    ReviewTemplate,
    commit_review,
    drop_diff,
    hold_diff,
    holds_diff,
    pending_titles,
    quote_diff,
    quotes_diff,
    review_skills,
    review_values,
)
from orbit.agent.domain.roles import Role, enabled_roles, role_task
from orbit.agent.domain.setup_first import waiting_for_setup, waiting_note
from orbit.agent.domain.tools import REPORT_FINDING
from orbit.agent.domain.triggers import (
    COMMITS,
    REVIEW_FIELD,
    REVIEW_ROUNDS,
    SCHEDULE,
    SESSION_STARTED,
    STARTUP_CHECK_FAILED,
    TASK_FINISHED,
    TASK_LANDED,
    TRIGGER_VARIABLE,
    Counted,
    SetOutcome,
    Sifted,
    Trigger,
    TriggerEvent,
    count_landing,
    fires_by_hand,
    firing_roles,
    held_back,
    listening,
    needs_paths,
    note_skipped_reviews,
    reads_paths,
    render_held_prompt,
    render_template,
    set_finished_event,
    set_finished_values,
    sift_changes,
    verification_state,
)
from orbit.agent.domain.verification import CheckSet, VerificationStep, failure_report
from orbit.protocol import (
    CODER,
    Notice,
    QueuedAs,
    TaskSpec,
    TaskState,
)
from orbit.tracing import current_span

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

PATHS_FAILED_EVENT = "orbit.trigger.paths_failed"
"""The event on the span current when git can't list the paths a landing changed, for each
trigger with ``paths``, or that ``code-changed`` guards, that fires all the same."""


@dataclass(frozen=True)
class ReviewRound:
    """A review the ``review-rounds`` guard finds due, or whose lenses finished: ``job``, and
    ``book``, which records a task queued for it, by its id, with the template and values its
    prompt was rendered from, into the review, the first opening the batch its suggestions
    join. For a batch's review, ``raised`` are the roles that raised the batch's findings, which
    the ``raised-finding`` guard lets review it."""

    job: ReviewJob
    book: Callable[[str, ReviewTemplate], None]
    raised: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _Firing:
    """The triggers ``listening`` for an event, by name, in order, with what they render their
    tasks from: its ``values`` and the review due, ``review``, if any. On a landing ``counted``
    toward the ``commits`` triggers, or fired by hand, their guards were decided already, and
    each of them reviews ``review``; otherwise those ``review-rounds`` guards do, every one of
    them once a review's lenses finished, and, on a set's report, the tasks of finding roles, as
    ``books`` says. The ``event`` raised, if any, narrows the roles each queues for, as
    :func:`~orbit.agent.domain.triggers.firing_roles` says."""

    listening: list[tuple[str, Trigger]]
    values: Mapping[str, str]
    review: ReviewRound | None
    counted: bool = False
    books: Literal["guarded", "every", "lenses"] = "guarded"
    event: TriggerEvent | None = None

    def review_of(self, trigger: Trigger, role: Role) -> ReviewRound | None:
        """The review the task of ``role`` that ``trigger`` queues reviews, if any: every
        trigger's, on a landing counted, by hand, or once a review's lenses finished, a finding
        role's on a set's report, else only those ``review-rounds`` guards."""
        return self.review if self._books(trigger, role) else None

    def queues(
        self, trigger: Trigger, specs: Mapping[str, Role]
    ) -> list[tuple[str, ReviewRound | None]]:
        """The roles ``trigger`` queues a task for as it fires, in its order, among the enabled
        roles of ``specs``, as :func:`~orbit.agent.domain.triggers.firing_roles` says, each with
        the review its task reviews, if any, as :meth:`review_of` says. Both the diff read and
        the tasks queued ask this, so a task keeping its prompt never quotes
        :data:`~orbit.agent.domain.reviews.DIFF_HELD`."""
        raised = () if self.review is None else self.review.raised
        roles = firing_roles(trigger, enabled_roles(specs), self.event, raised)
        return [(role, self.review_of(trigger, specs[role])) for role in roles]

    def _books(self, trigger: Trigger, role: Role) -> bool:
        """Whether the task of ``role`` that ``trigger`` queues is booked into the review, as
        :meth:`review_of` says."""
        if self.books == "lenses":
            return REPORT_FINDING in role.granted_tools
        return self.books == "every" or self.counted or REVIEW_ROUNDS in trigger.guards


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
        fields = {"role": state.task.role, "origin": state.origin, REVIEW_FIELD: "commit"}
        event = TriggerEvent(TASK_LANDED, fields, state, (commit,))
        job = commit_review(state.id, commit)
        values = partial(self._values, job, state.task.skills)
        return await self.fire(event, values, review)

    async def finished(self, state: TaskState) -> tuple[str, ...]:
        """Raise ``task_finished``: the task that was ``state`` ended as its status says.
        Returns the ids of the tasks queued."""
        fields = {"role": state.task.role, "origin": state.origin, "status": state.status}
        return await self.fire(TriggerEvent(TASK_FINISHED, fields, state), _no_values)

    async def reviewed(self, review: ReviewRound) -> tuple[str, ...]:
        """Fire the triggers ``review-rounds`` guards on ``task_landed`` for ``review``, of a
        batch that's done, which no one task caused, as its ``review`` field says, but those
        ``raised-finding`` guards whose role raised none of its findings. Those ``code-changed``
        held back are noted in the conversation, naming the batch's reviewed tasks. Returns the
        ids of the tasks queued."""
        fired = await self._batch_firing(review)
        queued = () if fired.firing is None else await self._queue(fired.firing)
        board = self._tasks.board
        found = (board.find(task_id) for task_id in review.job.reviewed)
        prompts = [state.task.prompt for state in found if state is not None]
        await self._note_skipped(fired, prompts)
        return queued

    async def reviewed_quietly(self, review: ReviewRound) -> None:
        """Fire the triggers ``review-rounds`` guards for ``review`` as :meth:`reviewed` does,
        their tasks queued on the board without publishing it or running them."""
        firing = (await self._batch_firing(review)).firing
        if firing is not None:
            self._add(self._tasks.board, firing)

    async def fan_in(
        self, event: TriggerEvent, values: Values, review: ReviewRound
    ) -> tuple[str, ...]:
        """Raise ``event``, ``reviews_finished`` for ``review``, whose lenses have all ended:
        each trigger on it that no guard holds back queues its task, rendered from the
        ``values`` gathered and its name, booked into the review. The board is published if any
        task was queued. Returns their ids, for :meth:`run`."""
        firing = await self._fan_in_firing(event, values, review)
        return () if firing is None else await self._queue(firing)

    async def fan_in_quietly(
        self, event: TriggerEvent, values: Values, review: ReviewRound
    ) -> None:
        """Raise ``event`` for ``review`` as :meth:`fan_in` does, the tasks queued on the board
        without publishing it or running them."""
        firing = await self._fan_in_firing(event, values, review)
        if firing is not None:
            self._add(self._tasks.board, firing)

    async def _fan_in_firing(
        self, event: TriggerEvent, values: Values, review: ReviewRound
    ) -> _Firing | None:
        """What the triggers on ``event`` fire on for ``review``, each booking its task into it;
        none if none does."""
        firing = await self._firing(self._desk.agents.triggers, event, values, review)
        return None if firing is None else replace(firing, books="every")

    async def set_finished(
        self, outcome: SetOutcome, review: ReviewRound | None = None
    ) -> tuple[str, ...]:
        """Raise ``set_finished``: a named set's run ended as ``outcome`` says. Each trigger on
        it that no guard holds back queues its tasks, or runs its checks; those of finding roles
        are booked into ``review``, the review of the set's report, if there's one, and the
        others are ordinary tasks. The board is published if any task was queued. Returns their
        ids, for :meth:`run`."""
        event = set_finished_event(outcome)
        values = partial(_given, set_finished_values(outcome))
        firing = await self._firing(self._desk.agents.triggers, event, values, review)
        return () if firing is None else await self._queue(replace(firing, books="lenses"))

    async def scheduled(self, names: Collection[str]) -> tuple[str, ...]:
        """Raise ``schedule`` for the triggers ``names``, whose cron expressions came due: each
        of them that listens for it, as :func:`~orbit.agent.domain.triggers.listening` says, and
        that no guard holds back queues its tasks, rendered from its name, or runs the set it
        names. The board is published if any task was queued. Returns their ids, for
        :meth:`run`."""
        triggers = {
            name: trigger for name, trigger in self._desk.agents.triggers.items() if name in names
        }
        firing = await self._firing(triggers, TriggerEvent(SCHEDULE, {}), _no_values, None)
        return () if firing is None else await self._queue(firing)

    async def fire(
        self, event: TriggerEvent, values: Values, review: ReviewRound | None = None
    ) -> tuple[str, ...]:
        """Fire the triggers listening for ``event``, with ``review`` the review due on it, if
        any, as :func:`~orbit.agent.domain.triggers.listening` says: in their order, each that
        no guard holds back queues its role's task, a draft if it says so, from it, rendered
        from the ``values`` gathered and its name, or runs the checks its ``checks`` names. The
        board is published if any task was queued. Those ``code-changed`` held back on a landing
        that changed no code are noted in the conversation, naming the task that caused it.
        Returns their ids, for :meth:`run`."""
        fired = await self._sift(self._desk.agents.triggers, event, values, review)
        queued = () if fired.firing is None else await self._queue(fired.firing)
        prompts = () if event.cause is None else (event.cause.task.prompt,)
        await self._note_skipped(fired, prompts)
        return queued

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

    async def _batch_firing(self, review: ReviewRound) -> _Fired:
        """What the triggers ``review-rounds`` guards fire on for ``review``, the review of a
        batch's coder tasks, from what it reviews, and those ``code-changed`` held back."""
        job = review.job
        triggers = {
            name: trigger
            for name, trigger in self._desk.agents.triggers.items()
            if REVIEW_ROUNDS in trigger.guards
        }
        fields = {"role": CODER, "origin": job.origin, REVIEW_FIELD: "batch"}
        event = TriggerEvent(TASK_LANDED, fields, commits=job.commits)
        skills = review_skills(job, self._tasks.board.tasks)
        return await self._sift(triggers, event, partial(self._values, job, skills), review)

    async def _firing(
        self,
        triggers: Mapping[str, Trigger],
        event: TriggerEvent,
        values: Values,
        review: ReviewRound | None,
    ) -> _Firing | None:
        """What the ``triggers`` that may fire on ``event`` fire on, as :meth:`_sift` says; none
        if none may, or the values can't be gathered."""
        return (await self._sift(triggers, event, values, review)).firing

    async def _sift(
        self,
        triggers: Mapping[str, Trigger],
        event: TriggerEvent,
        values: Values,
        review: ReviewRound | None,
    ) -> _Fired:
        """The ``triggers`` that may fire on ``event``, with ``review`` the review due, if any,
        and the roles that raised its batch's findings, and that the paths its commits changed
        let fire, and the ``values`` they render from, gathered only if one may, with those
        ``code-changed`` held back, and, on a landing, the diff its tasks quote, as
        :meth:`_show_firing_diff` says."""
        roles = enabled_roles(self._desk.agents.roles)
        job, raised = (None, frozenset[str]()) if review is None else (review.job, review.raised)
        sifted = await self._within_paths(listening(triggers, event, roles, job, raised), event)
        given = await values() if sifted.fire else None
        firing = None if given is None else _Firing(sifted.fire, given, review, event=event)
        shown = await self._show_firing_diff(firing)
        return _Fired(shown, sifted.held, frozenset(roles), event, raised)

    async def _show_firing_diff(self, firing: _Firing | None) -> _Firing | None:
        """``firing``, if any, and if it's a landing's, with ``{diff}`` the diff of its commits,
        as git shows it, if a task it may queue keeps that in its prompt, as
        :meth:`_keeps_diff` says, else quoting :data:`~orbit.agent.domain.reviews.DIFF_HELD`,
        every task that quotes it holding it back for the review lane to read as it starts the
        task. If git can't show it, the tasks are queued all the same, quoting what says so.
        Any other firing is returned as it is."""
        if firing is None or firing.event is None or firing.event.name != TASK_LANDED:
            return firing
        if not self._keeps_diff(firing):
            return replace(firing, values=hold_diff(firing.values))
        tree = self._desk.agents.tree
        assert tree is not None  # Only work in a tree lands.
        diff = await show_diff(tree, firing.event.commits)
        return replace(firing, values=quote_diff(firing.values, diff))

    def _keeps_diff(self, firing: _Firing) -> bool:
        """Whether a task the triggers ``firing`` may queue quotes ``{diff}`` and keeps it in its
        prompt, holding none back, as :func:`_holds_diff` says."""
        specs = self._desk.agents.roles
        return any(
            not _holds_diff(review, role)
            for _, trigger in firing.listening
            if trigger.prompt is not None and quotes_diff(trigger.prompt)
            for role, review in firing.queues(trigger, specs)
        )

    async def _within_paths(self, found: list[tuple[str, Trigger]], event: TriggerEvent) -> Sifted:
        """The triggers ``found`` listening for ``event``, by name, in order, sifted by the
        paths its commits changed, as :func:`~orbit.agent.domain.triggers.sift_changes` says,
        with the paths of the project's ``[[skills]]``, those changed paths asked of git only
        if one of them has ``paths`` or ``code-changed`` guards it. If git can't list them,
        every one of them fires, the failure noted on the span current for each so decided."""
        if not needs_paths(found):
            return Sifted(found, [])
        try:
            changed = await self._changed_paths(event)
        except TreeFailed as exc:  # Git failed; reviews fire rather than be skipped unseen.
            _note_paths_failed(found, exc)
            return Sifted(found, [])
        mapped = [path for mapping in self._desk.agents.skill_mappings for path in mapping.paths]
        return sift_changes(found, changed, mapped)

    async def _changed_paths(self, event: TriggerEvent) -> list[str]:
        """The paths ``event``'s commits changed, as git lists them; raises
        :class:`~orbit.agent.application.ports.TreeFailed` if it can't."""
        tree = self._desk.agents.tree
        assert tree is not None  # Only work in a tree lands, and only a landing takes paths.
        return [
            path
            for commit in event.commits
            for path in await tree.changed_files(f"{commit}^", commit)
        ]

    async def _note_skipped(self, fired: _Fired, prompts: Sequence[str]) -> None:
        """Say in the conversation which reviews the triggers ``code-changed`` held back as
        ``fired`` says skipped of the tasks with ``prompts``, if it held back any that would
        have queued a task."""
        note = note_skipped_reviews(fired.held, prompts, fired.roles, fired.event, fired.raised)
        if note is not None:
            await self._desk.publish(Notice(message=note))

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
        """Add to ``board`` the tasks of each of the triggers ``firing`` that no guard holds
        back, in order, each seeing those before it, running the checks for each with
        ``checks``; returns their ids."""
        return tuple(
            task_id
            for name, trigger in firing.listening
            for task_id in self._fire_one(board, name, trigger, firing)
        )

    def _fire_one(
        self, board: TaskBoard, name: str, trigger: Trigger, firing: _Firing
    ) -> tuple[str, ...]:
        """Fire ``trigger``, named ``name``, as ``firing`` says: with ``checks``, run them
        unless a guard holds it back, with ``board``'s tasks, if they're still to decide, adding
        none; otherwise add its tasks to ``board`` as :meth:`_add_tasks` does, returning their
        ids."""
        if trigger.checks is None:
            return self._add_tasks(board, name, trigger, firing)
        if firing.counted or not held_back(trigger, None, board.tasks):
            self._run_checks(trigger.checks, name)
        return ()

    def _add_tasks(
        self, board: TaskBoard, name: str, trigger: Trigger, firing: _Firing
    ) -> tuple[str, ...]:
        """Add to ``board`` the tasks ``trigger``, named ``name``, queues as it fires as
        ``firing`` says, one for each of its roles that fires, from one rendering of its
        prompt, unless a guard holds it back, if they're still to decide, seeing the board as
        it was before any of them; returns their ids. Each reviewing the review due does so
        with the skills of what it reviews, whatever its role, and is queued as a review, booked
        into its batches with what its prompt was rendered from, but the diff it quotes."""
        assert trigger.prompt is not None  # It listened without checks, so it has a prompt.
        values = {**firing.values, TRIGGER_VARIABLE: name}
        prompt = render_template(trigger.prompt, values)
        if not firing.counted and held_back(trigger, prompt, board.tasks):
            return ()
        held = render_held_prompt(trigger.prompt, values)
        template = ReviewTemplate(trigger.prompt, drop_diff(values))
        queued = _Queued(name, trigger, prompt, held, template)
        pairs = firing.queues(trigger, self._desk.agents.roles)
        return tuple(self._add_one(board, queued, role, review) for role, review in pairs)

    def _add_one(
        self, board: TaskBoard, queued: _Queued, role: str, review: ReviewRound | None
    ) -> str:
        """Add to ``board`` ``role``'s task of the trigger ``queued`` says, reviewing
        ``review``, if any; returns its id. One reviewing the review due does so with the skills
        of what it reviews, and is queued as a review, booked into its batches with what its
        prompt was rendered from. One that holds its diff back, as :func:`_holds_diff` says,
        quotes :data:`~orbit.agent.domain.reviews.DIFF_HELD` in place of the diff, which the
        review lane reads from git as it starts it."""
        spec = self._desk.agents.roles[role]
        skills = () if review is None else review_skills(review.job, board.tasks)
        prompt = queued.held if _holds_diff(review, role) else queued.prompt
        task = role_task(role, spec, prompt, skills)
        queued_as: QueuedAs = "task" if review is None else "review"
        task_id = _added(board, task, queued.name, queued.trigger, queued_as)
        if review is not None:
            review.book(task_id, queued.template)
        return task_id

    async def _values(self, job: ReviewJob, skills: Sequence[str]) -> Mapping[str, str]:
        """The values of ``task_landed`` for ``job``'s commits, landed by the tasks it reviews,
        which name ``skills``, as its review's task renders from them, but ``{diff}``, which
        :meth:`_show_firing_diff` gives."""
        cap = self._desk.agents.review_limits.suggestions
        pending = pending_titles(self._tasks.board.tasks)
        landing = {"task": ", ".join(job.reviewed), "task_skills": ", ".join(skills)}
        return review_values(job, cap, pending) | landing


@dataclass(frozen=True)
class _Fired:
    """What an event fires: the ``firing``, if any trigger fires and its values were gathered,
    and the triggers ``code-changed`` ``held`` back, by name, in order, with what decides the
    roles each would have queued for: ``roles``, the enabled roles, the ``event`` and the roles
    that ``raised`` its batch's findings."""

    firing: _Firing | None
    held: list[tuple[str, Trigger]]
    roles: frozenset[str]
    event: TriggerEvent
    raised: frozenset[str]


@dataclass(frozen=True)
class _Queued:
    """What a trigger fires its tasks from: its ``name``, the ``trigger``, its ``prompt``, as
    rendered, the same ``held`` back, quoting :data:`~orbit.agent.domain.reviews.DIFF_HELD` in
    place of the diff, and the ``template`` it was rendered from, with its values but the
    diff."""

    name: str
    trigger: Trigger
    prompt: str
    held: str
    template: ReviewTemplate


def _holds_diff(review: ReviewRound | None, role: str) -> bool:
    """Whether ``role``'s task, booked into ``review``, if any, holds its diff back for the
    review lane to read as it starts it, as
    :func:`~orbit.agent.domain.reviews.holds_diff` says; an unbooked task holds back none."""
    return review is not None and holds_diff(role)


def _note_paths_failed(found: Sequence[tuple[str, Trigger]], error: TreeFailed) -> None:
    """Note on the span current, for each of the triggers ``found``, by name, that the paths a
    landing changed decide, that it fires though git failed, as ``error`` says, to list what
    changed."""
    span = current_span()
    for name, trigger in found:
        if reads_paths(trigger):
            span.event(PATHS_FAILED_EVENT, {"orbit.trigger": name, "error": str(error)})


def _added(
    board: TaskBoard, task: TaskSpec, name: str, trigger: Trigger, queued_as: QueuedAs
) -> str:
    """Add ``task``, which ``trigger``, named ``name``, queued as ``queued_as``, to ``board``:
    as a draft if it says so; returns its id."""
    if trigger.draft:
        return board.add_draft(task, None, name, queued_as)
    return board.add_triggered(task, name, queued_as)
