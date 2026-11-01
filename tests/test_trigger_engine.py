"""The trigger engine: triggers listening for a task's landing or end queue their roles' tasks,
rendered from their templates, with their names as origins, unless a guard holds them back, and
the review-rounds guard reviews the review the review lane finds due."""

from orbit.agent.application.ports import TreeFailed
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.triggers import ReviewRound, TriggerEngine
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.reviews import ReviewJob, commit_review, review_prompt
from orbit.agent.domain.roles import Role
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import (
    CODER,
    GIT_FAILED,
    REVIEWER,
    ErrorOccurred,
    Event,
    TasksChanged,
    TaskSpec,
    TaskState,
    TaskStatus,
)
from tests.agent_fakes import ROLES, TRIGGERS, FakeTree, desk_for

COMMIT = "c1abcdef99"


class Engine:
    """A trigger engine firing ``triggers`` over a board of its own, in ``tree``, with
    ``roles`` (:data:`ROLES` unless given), recording what it publishes and the roles whose
    lanes it kicks."""

    def __init__(
        self,
        triggers: dict[str, Trigger],
        tree: FakeTree | None = None,
        roles: dict[str, Role] | None = None,
    ) -> None:
        self.events: list[Event] = []
        self.kicked: list[str] = []
        self.tree = tree or FakeTree()
        desk = desk_for(self.publish, tree=self.tree, triggers=triggers, roles=roles or ROLES)
        self.tasks = Tasks(TaskBoard(), self.publish)
        self.board = self.tasks.board
        self.engine = TriggerEngine(desk, self.tasks, self.kicked.append)

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    def shown(self) -> list[tuple[str, str, str, str]]:
        """Each task on the board: its role, origin, status and prompt."""
        return [(t.task.role, t.origin, t.status, t.task.prompt) for t in self.board.tasks]

    def errors(self) -> list[ErrorOccurred]:
        return [event for event in self.events if isinstance(event, ErrorOccurred)]


def look(**fields: object) -> Trigger:
    """A trigger queuing a reviewer's look at each landed coder commit, with ``fields`` over
    that."""
    return Trigger.model_validate(
        {
            "on": "task_landed",
            "role": REVIEWER,
            "prompt": "Look at {commit}",
            "when": {"role": CODER},
            **fields,
        }
    )


def ended(
    task_id: str = "1",
    role: str = CODER,
    origin: str = "manual",
    status: TaskStatus = "completed",
    skills: tuple[str, ...] = (),
) -> TaskState:
    """A task as it ended: ``role``'s, queued by ``origin``, as ``status``, naming ``skills``."""
    task = TaskSpec(prompt=f"Task {task_id}", role=role, skills=skills)
    return TaskState(id=task_id, task=task, status=status, origin=origin)


def disabled(name: str) -> dict[str, Role]:
    """The built-in roles, ``name`` disabled."""
    return {**ROLES, name: ROLES[name].model_copy(update={"enabled": False})}


# Firing on a landing


async def test_a_landing_queues_the_task_of_a_trigger_listening_from_its_template() -> None:
    engine = Engine({"look": look()})
    queued = await engine.engine.landed(ended(), COMMIT)
    assert engine.shown() == [(REVIEWER, "look", "pending", "Look at c1abcde")]
    assert queued == (engine.board.tasks[0].id,)
    assert isinstance(engine.events[-1], TasksChanged)
    assert engine.kicked == []  # Running is left to what raised the event.


async def test_a_triggers_task_has_its_roles_settings_and_no_skills_of_its_own() -> None:
    engine = Engine({"look": look()})
    await engine.engine.landed(ended(skills=("python",)), COMMIT)
    [state] = engine.board.tasks
    assert state.task.settings == ROLES[REVIEWER].settings
    assert state.task.skills == ()


async def test_a_landing_no_trigger_takes_queues_nothing_and_shows_no_diff() -> None:
    engine = Engine({"look": look()})
    engine.tree.failures["show"] = TreeFailed("would be shown")
    assert await engine.engine.landed(ended(role=REVIEWER), COMMIT) == ()
    assert engine.board.tasks == ()
    assert engine.events == []


async def test_a_trigger_whose_role_is_disabled_never_fires() -> None:
    engine = Engine({"look": look()}, roles=disabled(REVIEWER))
    assert await engine.engine.landed(ended(), COMMIT) == ()
    assert engine.board.tasks == ()


async def test_two_triggers_on_a_landing_each_fire_in_their_order() -> None:
    first = look(prompt="First at {commit}")
    engine = Engine({"second": look(prompt="Second {trigger}"), "first": first})
    await engine.engine.landed(ended(), COMMIT)
    assert [(origin, prompt) for _, origin, _, prompt in engine.shown()] == [
        ("second", "Second second"),
        ("first", "First at c1abcde"),
    ]


async def test_a_landing_gives_its_commit_task_skills_and_its_reviews_values() -> None:
    template = (
        "{title}\n\n{task}: {task_skills}\n\n{cap}\n\n{queued}\n\n{missing}{landed_since}\n\n{diff}"
    )
    engine = Engine({"look": look(prompt=template)})
    engine.board.add(TaskSpec(prompt="Add docs\n\nMore."))
    await engine.engine.landed(ended("3", skills=("python", "layers")), COMMIT)
    assert engine.board.tasks[-1].task.prompt == (
        "Review commit c1abcde\n\n3: python, layers\n\n5\n\n"
        "Tasks already queued (don't suggest these):\n- Add docs\n\n"
        f"diff of {COMMIT}"
    )


async def test_a_diff_git_cannot_show_skips_the_triggers_on_the_landing() -> None:
    engine = Engine({"look": look()})
    engine.tree.failures["show"] = TreeFailed("git show failed: bad object")
    assert await engine.engine.landed(ended(), COMMIT) == ()
    assert engine.board.tasks == ()
    assert engine.errors() == [
        ErrorOccurred(code=GIT_FAILED, message="Triggers skipped: git show failed: bad object")
    ]


async def test_a_draft_trigger_adds_its_task_as_a_draft_from_it() -> None:
    engine = Engine({"look": look(draft=True)})
    await engine.engine.landed(ended(), COMMIT)
    [state] = engine.board.tasks
    assert (state.status, state.origin, state.proposed_by) == ("draft", "look", None)


# Guards


async def test_dedupe_skips_a_task_whose_first_line_a_waiting_task_has() -> None:
    engine = Engine({"look": look(prompt="Look at the code", guards=["dedupe"])})
    await engine.engine.landed(ended("1"), COMMIT)
    assert await engine.engine.landed(ended("2"), "c2") == ()
    assert len(engine.board.tasks) == 1
    engine.board.finish(engine.board.tasks[0].id, "completed")
    await engine.engine.landed(ended("3"), "c3")
    assert [status for _, _, status, _ in engine.shown()] == ["completed", "pending"]


async def test_not_queued_skips_while_a_task_of_its_role_waits() -> None:
    engine = Engine({"look": look(guards=["not-queued"])})
    engine.board.add(TaskSpec(prompt="Make it faster."))
    waiting = engine.board.add(TaskSpec(prompt="Review it.", role=REVIEWER))
    assert await engine.engine.landed(ended(), COMMIT) == ()
    engine.board.remove(waiting)
    assert len(await engine.engine.landed(ended(), COMMIT)) == 1


async def test_review_rounds_holds_its_trigger_back_without_a_review_due() -> None:
    engine = Engine({"review": TRIGGERS["review"], "look": look()})
    await engine.engine.landed(ended(), COMMIT)
    assert [origin for _, origin, _, _ in engine.shown()] == ["look"]


async def test_review_rounds_reviews_the_review_due_and_books_its_task() -> None:
    booked: list[str] = []
    engine = Engine({"review": TRIGGERS["review"]})
    engine.board.add(TaskSpec(prompt="Make it faster.", skills=("python",)))
    job = commit_review("1", COMMIT)
    [task_id] = await engine.engine.landed(ended(), COMMIT, ReviewRound(job, booked.append))
    review = engine.board.get(task_id)
    assert (review.task.role, review.origin) == (REVIEWER, "review")
    assert review.task.prompt == review_prompt(job, f"diff of {COMMIT}", 5, ["Make it faster."])
    assert review.task.skills == ("python",)  # Those of the task it reviews.
    assert booked == [task_id]


async def test_a_diff_git_cannot_show_skips_the_review_due() -> None:
    engine = Engine({"review": TRIGGERS["review"]})
    engine.tree.failures["show"] = TreeFailed("git show failed: bad object")
    review = ReviewRound(commit_review("1", COMMIT), lambda task_id: None)
    assert await engine.engine.landed(ended(), COMMIT, review) == ()
    assert [error.message for error in engine.errors()] == [
        "Review skipped: git show failed: bad object"
    ]


# Firing on a task's end


def tidy() -> Trigger:
    """A trigger queuing a coder's tidying up after each task that finishes."""
    return Trigger(on="task_finished", role=CODER, prompt="Tidy up after {trigger}")


async def test_a_task_finishing_queues_the_task_of_a_trigger_listening() -> None:
    engine = Engine({"tidy": tidy()})
    await engine.engine.finished(ended(status="gave_up"))
    assert engine.shown() == [(CODER, "tidy", "pending", "Tidy up after tidy")]


async def test_a_trigger_never_fires_on_the_end_of_a_task_it_queued() -> None:
    engine = Engine({"tidy": tidy()})
    assert await engine.engine.finished(ended(origin="tidy")) == ()
    assert engine.board.tasks == ()


async def test_a_trigger_takes_a_finished_task_by_its_status() -> None:
    engine = Engine({"tidy": tidy().model_copy(update={"when": {"status": ("gave_up",)}})})
    assert await engine.engine.finished(ended(status="completed")) == ()
    assert len(await engine.engine.finished(ended(status="gave_up"))) == 1


# A batch's review


BATCH = ReviewJob(commits=("c2", "c3"), round=1, batch="batch-1", reviewed=("2", "3"))


async def test_a_batchs_review_fires_only_the_triggers_review_rounds_guards() -> None:
    booked: list[str] = []
    triggers = {"review": TRIGGERS["review"], "look": look(when={})}
    engine = Engine(triggers)
    engine.board.add(TaskSpec(prompt="Add tests", skills=("python",)))
    [task_id] = await engine.engine.reviewed(ReviewRound(BATCH, booked.append))
    review = engine.board.get(task_id)
    assert review.origin == "review"
    assert review.task.prompt == review_prompt(BATCH, "diff of c2\ndiff of c3", 5, ["Add tests"])
    assert booked == [task_id]
    assert isinstance(engine.events[-1], TasksChanged)


async def test_a_batchs_review_with_the_review_trigger_disabled_queues_nothing() -> None:
    off = TRIGGERS["review"].model_copy(update={"enabled": False})
    engine = Engine({"review": off})
    assert await engine.engine.reviewed(ReviewRound(BATCH, lambda task_id: None)) == ()
    assert engine.board.tasks == ()


async def test_a_batchs_review_queued_quietly_is_not_published() -> None:
    engine = Engine({"review": TRIGGERS["review"]})
    await engine.engine.reviewed_quietly(ReviewRound(BATCH, lambda task_id: None))
    assert [role for role, _, _, _ in engine.shown()] == [REVIEWER]
    assert engine.events == []


async def test_a_batchs_review_queued_quietly_with_none_listening_adds_nothing() -> None:
    engine = Engine({})
    await engine.engine.reviewed_quietly(ReviewRound(BATCH, lambda task_id: None))
    assert engine.board.tasks == ()


# Running


async def test_running_kicks_the_lane_of_each_role_of_the_tasks_still_on_the_board() -> None:
    engine = Engine({})
    first = engine.board.add(TaskSpec(prompt="a", role=REVIEWER))
    second = engine.board.add(TaskSpec(prompt="b", role=REVIEWER))
    gone = engine.board.add(TaskSpec(prompt="c", role=CODER))
    engine.board.remove(gone)
    engine.engine.run((first, second, gone))
    assert engine.kicked == [REVIEWER]
