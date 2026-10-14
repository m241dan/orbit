"""The review lane: reviews of landed work, run one at a time beside the work lane.

Each review runs in a read-only snapshot of the commit it reviews. Its suggestions become tasks
at the end of the queue, which the work lane then runs; :class:`ReviewPlanner` decides when a
review is due and stops reviews generating work forever.
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from orbit.agent.application.agent_run import AgentDesk, AgentRun
from orbit.agent.application.ports import CoderTree, Reply
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.reviews import ReviewJob, ReviewPlanner, review_prompt
from orbit.protocol import Notice, Settings, TasksChanged, TaskSpec, TaskStatus

REVIEW_ROLES = frozenset({"reviewer", "super-reviewer"})


class ReviewLane:
    """Queues and runs reviews of ``board``'s tasks through ``desk``'s agents.

    ``wake_work`` asks the work lane to run the suggestions a review queued.
    """

    def __init__(
        self,
        desk: AgentDesk,
        board: TaskBoard,
        planner: ReviewPlanner,
        wake_work: Callable[[], Awaitable[None]],
    ) -> None:
        assert desk.agents.tree is not None  # Reviews read commits.
        self._desk = desk
        self._tree: CoderTree = desk.agents.tree
        self._board = board
        self.planner = planner
        self._wake_work = wake_work
        self._publish = desk.publish
        self._jobs: dict[str, tuple[str, str]] = {}  # Review task id -> its batch and commit.
        self._running: asyncio.Task[None] | None = None
        self._paused_noted = False

    async def landed(self, task_id: str, commit: str) -> None:
        """Task ``task_id`` landed ``commit``."""
        await self._queue(self.planner.landed(self._board.get(task_id), commit))

    async def ended(self, task_id: str) -> None:
        """Task ``task_id`` finished without landing anything."""
        await self._queue(self.planner.ended(self._board.get(task_id)))

    def joined(self, task_id: str) -> None:
        """Task ``task_id`` was added to fix another; if that one is in a batch, so is it."""
        batch = self._board.get(task_id).batch
        if batch is not None and batch in self.planner.open_batches:
            self.planner.added(batch, task_id)

    async def suggest(self, task_id: str, title: str, prompt: str) -> Reply:
        """Review task ``task_id`` suggests a task; queue it unless it's refused."""
        job = self._jobs.get(task_id)
        if job is None:
            return Reply("Only reviews orbit queued itself can suggest tasks.", is_error=True)
        batch = job[0]
        refusal = self.planner.refusal(batch, title, self._board.tasks)
        if refusal is not None:
            return Reply(refusal, is_error=True)
        coder = self._desk.agents.roles["coder"]
        task = TaskSpec(
            prompt=f"{title.strip()}\n\n{prompt.strip()}",
            settings=Settings(model=coder.model, tool_access=coder.tool_access),
        )
        new_id = self._board.add_suggestion(task, batch, self.planner.round_of(batch))
        self.planner.added(batch, new_id)
        await self._publish(TasksChanged(tasks=self._board.tasks))
        return Reply(f"Queued as task {new_id}.")

    def kick(self) -> None:
        """Run the reviews waiting, if they aren't being run already."""
        if self._running is None or self._running.done():
            self._running = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self._running is not None and not self._running.done():
            self._running.cancel()
            with suppress(asyncio.CancelledError):
                await self._running

    async def _queue(self, job: ReviewJob | None) -> None:
        await self._note_pause()
        if job is None:
            return
        diff = await self._tree.show(job.commits)
        batch = self.planner.open_batch(job)
        reviewer = self._desk.agents.roles["reviewer"]
        task = TaskSpec(
            prompt=review_prompt(job, diff, self.planner.cap),
            role="reviewer",
            settings=Settings(model=reviewer.model, tool_access=reviewer.tool_access),
        )
        task_id = self._board.add_review(task)
        self._jobs[task_id] = (batch, job.commits[-1])
        await self._publish(TasksChanged(tasks=self._board.tasks))
        self.kick()

    async def _note_pause(self) -> None:
        if self.planner.paused and not self._paused_noted:
            message = (
                f"Review paused after {self.planner.rounds} round(s) of review-suggested work;"
                " it resumes when one of your tasks lands."
            )
            await self._publish(Notice(message=message))
        self._paused_noted = self.planner.paused

    async def _run(self) -> None:
        tried: set[str] = set()
        while (task_id := self._board.next_runnable(skip=tried, roles=REVIEW_ROLES)) is not None:
            tried.add(task_id)
            await self._review(task_id)

    async def _review(self, task_id: str) -> None:
        batch, commit = self._jobs.get(task_id, (None, None))
        self._board.set_status(task_id, "running")
        await self._publish(TasksChanged(tasks=self._board.tasks))
        root = await self._tree.snapshot(commit) if commit else "."
        status: TaskStatus = "cancelled"
        try:
            status = (await AgentRun(task_id, self._board, None, self._desk).run(None, root)).status
        finally:
            if commit:
                await self._tree.drop(root)
            self._board.finish(task_id, status)
            await self._publish(TasksChanged(tasks=self._board.tasks))
        if batch is not None:
            self.planner.close_if_empty(batch)
            if batch in self.planner.open_batches:
                await self._wake_work()
