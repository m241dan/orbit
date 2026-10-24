"""Moving records with the moves kept in the store: each move kept so a task that still names
the old path follows it, the store's failures reported or halting the session."""

from collections.abc import Collection

from orbit.agent.application.record_moves import MoveHooks, RecordMoves
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.records import moved_note, planner_note
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.protocol import INTERNAL, ErrorOccurred, Event, Notice, TaskSpec
from tests.agent_fakes import FakeHalt, FakeStore

OLD, NEW = "docs/00024-feature-metrics.md", "docs/00025-feature-metrics.md"
ELSEWHERE = "docs/00031-feature-metrics.md"
LATER = TaskSpec(prompt=f"Export metrics\n\nPlan: {OLD}")
TAKEN = ("00023-bugfix-pause.md", "00024-bugfix-review-loop.md")


def listing(path: str) -> Collection[str]:
    return TAKEN if path == "/tree/docs" else ()


class Moves:
    """Record moves over a board holding :data:`LATER`, with what they publish, tell the planner
    and halt for."""

    def __init__(self, store: FakeStore | None, names: bool = True) -> None:
        self.events: list[Event] = []
        self.notes: list[str] = []
        self.halt = FakeHalt()
        self.tasks = Tasks(TaskBoard(), self._publish)
        self.task_id = self.tasks.board.add(LATER)
        hooks = MoveHooks(listing if names else None, self._publish, self.notes.append)
        self.moves = RecordMoves(self.tasks, store, self.halt, hooks)

    async def _publish(self, event: Event) -> None:
        self.events.append(event)

    async def move(self) -> str:
        """Move the records the task names; its prompt then."""
        await self.moves.move(self.task_id, "/tree")
        return self.tasks.board.get(self.task_id).task.prompt


async def test_without_listing_files_records_keep_their_numbers() -> None:
    moves = Moves(FakeStore(), names=False)
    assert await moves.move() == LATER.prompt
    assert moves.events == []


async def test_a_move_is_kept_in_the_store_and_told_to_the_user_and_the_planner() -> None:
    store = FakeStore()
    moves = Moves(store)
    assert await moves.move() == LATER.prompt.replace(OLD, NEW)
    assert store.moves == {OLD: NEW}
    assert Notice(message=moved_note(OLD, NEW)) in moves.events
    assert moves.notes == [planner_note(OLD, NEW)]


async def test_a_task_naming_a_path_the_store_kept_a_move_from_follows_it() -> None:
    store = FakeStore()
    store.moves = {OLD: ELSEWHERE}
    assert await Moves(store).move() == LATER.prompt.replace(OLD, ELSEWHERE)


async def test_moves_that_cant_be_read_are_reported_once_and_records_still_move() -> None:
    store = FakeStore()
    store.moves_load_failure = "disk gone"
    moves = Moves(store)
    assert await moves.move() == LATER.prompt.replace(OLD, NEW)
    moves.tasks.board.add(TaskSpec(prompt=f"Again\n\nPlan: {OLD}"))
    moves.task_id = moves.tasks.board.tasks[-1].id
    await moves.move()
    errors = [event for event in moves.events if isinstance(event, ErrorOccurred)]
    assert errors == [ErrorOccurred(code=INTERNAL, message="disk gone")]


async def test_a_move_that_cant_be_kept_halts_and_is_kept_on_retrying() -> None:
    store = FakeStore()
    store.moves_failure = "disk full"
    moves = Moves(store)
    await moves.move()
    assert moves.halt.failures == [(FailedWrite.RECORD_MOVES, "disk full")]
    assert str(await moves.moves.retry()) == "disk full"
    store.moves_failure = None
    assert await moves.moves.retry() is None
    assert store.moves == {OLD: NEW}
    assert await moves.moves.retry() is None


async def test_a_move_made_while_halted_waits_for_the_retry() -> None:
    store = FakeStore()
    moves = Moves(store)
    moves.halt.failures.append((FailedWrite.EVENT_LOG, "disk full"))
    await moves.move()
    assert store.moves == {}
    assert await moves.moves.retry() is None
    assert store.moves == {OLD: NEW}


async def test_without_a_store_records_move_and_nothing_is_kept() -> None:
    moves = Moves(None)
    assert await moves.move() == LATER.prompt.replace(OLD, NEW)
    assert await moves.moves.retry() is None
