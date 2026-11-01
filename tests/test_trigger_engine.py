"""The trigger engine: triggers listening for a task's landing or end queue their roles' tasks,
rendered from their templates, with their names as origins, unless a guard holds them back, and
the review-rounds guard reviews the review the review lane finds due; landings count toward the
commits triggers, and those that fire, or one fired by hand, review the whole codebase; opening a
workspace fires the session_started triggers, those running the checks running them, and a step
of the startup check failing the startup_check_failed ones."""

import pytest

from orbit.agent.application.ports import TreeFailed
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.triggers import ReviewRound, TriggerEngine
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.checks import Setup, configured_checks
from orbit.agent.domain.reviews import ReviewJob, commit_review, review_prompt, review_values
from orbit.agent.domain.roles import Role
from orbit.agent.domain.triggers import Trigger
from orbit.agent.domain.verification import VerificationStep, startup_fix_prompt
from orbit.protocol import (
    CODER,
    GIT_FAILED,
    REVIEWER,
    SUPER_REVIEW,
    SUPER_REVIEWER,
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
        self.checks_run = 0
        self.tree = tree or FakeTree()
        desk = desk_for(self.publish, tree=self.tree, triggers=triggers, roles=roles or ROLES)
        self.tasks = Tasks(TaskBoard(), self.publish)
        self.board = self.tasks.board
        self.engine = TriggerEngine(desk, self.tasks, self.kicked.append, self.run_checks)

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    def run_checks(self) -> None:
        self.checks_run += 1

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


# Landings counted toward the commits triggers, and firing by hand


SUPER = TRIGGERS[SUPER_REVIEW]
WHOLE = ReviewJob(commits=(COMMIT,), round=0, batch=None, whole=True)


def super_every(every: int) -> dict[str, Trigger]:
    """The shipped super-review trigger, due every ``every`` landings."""
    return {SUPER_REVIEW: SUPER.model_copy(update={"every": every})}


async def test_a_landing_counts_toward_the_commits_triggers_taking_it() -> None:
    engine = Engine(super_every(2))
    first = engine.engine.count(ended("1", origin="planner"), {})
    assert (first.counts, first.fires) == ({SUPER_REVIEW: 1}, [])
    second = engine.engine.count(ended("2"), first.counts)
    assert (second.counts, second.fires) == (
        {SUPER_REVIEW: 0},
        [(SUPER_REVIEW, SUPER.model_copy(update={"every": 2}))],
    )
    assert engine.board.tasks == ()  # Queuing is left to fire_counted.


async def test_a_due_super_review_waits_while_a_review_on_the_board_waits() -> None:
    engine = Engine(super_every(1))
    engine.board.add_triggered(TaskSpec(prompt="Review commit c0", role=REVIEWER), "review")
    counted = engine.engine.count(ended(), {})
    assert (counted.counts, counted.fires) == ({SUPER_REVIEW: 1}, [])


async def test_a_landing_counts_toward_no_trigger_whose_role_is_disabled() -> None:
    engine = Engine(super_every(1), roles=disabled(SUPER_REVIEWER))
    assert engine.engine.count(ended(), {}).counts == {}


async def test_a_trigger_fired_by_hand_is_found_by_its_name() -> None:
    engine = Engine({SUPER_REVIEW: SUPER})
    assert engine.engine.by_hand(SUPER_REVIEW) == [(SUPER_REVIEW, SUPER)]
    assert engine.engine.by_hand("nightly") == []


async def test_a_disabled_trigger_or_one_whose_role_is_disabled_does_not_fire_by_hand() -> None:
    off = Engine({SUPER_REVIEW: SUPER.model_copy(update={"enabled": False})})
    assert off.engine.by_hand(SUPER_REVIEW) == []
    unrolled = Engine({SUPER_REVIEW: SUPER}, roles=disabled(SUPER_REVIEWER))
    assert unrolled.engine.by_hand(SUPER_REVIEW) == []


async def test_counted_triggers_queue_their_tasks_reviewing_the_codebase_and_booked() -> None:
    booked: list[str] = []
    engine = Engine({SUPER_REVIEW: SUPER})
    engine.board.add(TaskSpec(prompt="Make it faster.", skills=("python",)))
    values = review_values(WHOLE, "", 10, ["Make it faster."])
    fired = [(SUPER_REVIEW, SUPER)]
    [task_id] = await engine.engine.fire_counted(fired, values, ReviewRound(WHOLE, booked.append))
    review = engine.board.get(task_id)
    assert (review.task.role, review.origin) == (SUPER_REVIEWER, SUPER_REVIEW)
    assert review.task.prompt == review_prompt(WHOLE, "", 10, ["Make it faster."])
    assert review.task.skills == ()  # It reviews no particular task.
    assert booked == [task_id]
    assert isinstance(engine.events[-1], TasksChanged)
    assert engine.kicked == []


async def test_counted_triggers_are_not_held_back_again_as_they_are_queued() -> None:
    engine = Engine({SUPER_REVIEW: SUPER})
    engine.board.add_triggered(TaskSpec(prompt="Review commit c0", role=REVIEWER), "review")
    values = review_values(WHOLE, "", 10, [])
    review = ReviewRound(WHOLE, lambda task_id: None)
    assert len(await engine.engine.fire_counted([(SUPER_REVIEW, SUPER)], values, review)) == 1


async def test_each_counted_trigger_renders_its_own_name_and_is_booked_apart() -> None:
    booked: list[str] = []
    other = SUPER.model_copy(update={"prompt": "{trigger} at {commit}"})
    engine = Engine({SUPER_REVIEW: SUPER, "drift": other})
    fired = [(SUPER_REVIEW, SUPER), ("drift", other)]
    values = review_values(WHOLE, "", 10, [])
    queued = await engine.engine.fire_counted(fired, values, ReviewRound(WHOLE, booked.append))
    assert [(origin, prompt.split("\n", 1)[0]) for _, origin, _, prompt in engine.shown()] == [
        (SUPER_REVIEW, "Super review of the codebase at c1abcde"),
        ("drift", "drift at c1abcde"),
    ]
    assert booked == list(queued)


# Firing as a workspace opens

LINT = VerificationStep("Lint", ("ruff", "check"))
SETUP = Setup(command=("uv", "sync"))
GATE = Trigger(on="session_started", checks="gate", when={"verification": "configured"})


def hello(**fields: object) -> Trigger:
    """A trigger queuing a coder's hello as a workspace opens with steps, with ``fields`` over
    that."""
    return Trigger.model_validate(
        {
            "on": "session_started",
            "role": CODER,
            "prompt": "Say hello",
            "when": {"verification": "configured"},
            **fields,
        }
    )


async def test_opening_with_steps_runs_the_checks_of_a_trigger_running_them_queuing_nothing() -> (
    None
):
    engine = Engine({"startup-check": GATE})
    assert await engine.engine.started((LINT,)) == ()
    assert engine.checks_run == 1
    assert engine.board.tasks == ()
    assert engine.events == []


@pytest.mark.parametrize(
    ("steps", "state"), [((LINT,), "configured"), ((), "missing"), (None, "off")]
)
async def test_opening_fires_the_triggers_taking_its_verification(
    steps: tuple[VerificationStep, ...] | None, state: str
) -> None:
    states = ("configured", "missing", "off")
    triggers = {name: hello(when={"verification": name}) for name in states}
    engine = Engine(triggers)
    queued = await engine.engine.started(steps)
    assert engine.shown() == [(CODER, state, "pending", "Say hello")]
    assert queued == (engine.board.tasks[0].id,)
    assert isinstance(engine.events[-1], TasksChanged)
    assert engine.kicked == []


async def test_opening_gives_the_checks_configured_now() -> None:
    tree = FakeTree()
    tree.setup = SETUP
    engine = Engine({"hello": hello(prompt="Hello. {checks}")}, tree)
    await engine.engine.started((LINT,))
    assert engine.shown()[0][3] == f"Hello. {configured_checks((LINT,), SETUP)}"


async def test_opening_with_verification_off_is_told_nothing_is_configured() -> None:
    engine = Engine({"hello": hello(prompt="{checks}", when={})})
    await engine.engine.started(None)
    assert engine.shown()[0][3] == "Nothing is configured yet."


async def test_the_triggers_on_opening_each_fire_in_their_order() -> None:
    engine = Engine({"hello": hello(), "startup-check": GATE, "bye": hello(prompt="Bye")})
    await engine.engine.started((LINT,))
    assert [prompt for _, _, _, prompt in engine.shown()] == ["Say hello", "Bye"]
    assert engine.checks_run == 1


async def test_a_guard_holds_back_a_trigger_running_the_checks() -> None:
    engine = Engine({"startup-check": GATE.model_copy(update={"guards": ("suggestions-pending",)})})
    engine.board.add_triggered(TaskSpec(prompt="Review commit c0", role=REVIEWER), "review")
    await engine.engine.started((LINT,))
    assert engine.checks_run == 0


async def test_a_disabled_trigger_running_the_checks_runs_nothing() -> None:
    engine = Engine({"startup-check": GATE.model_copy(update={"enabled": False})})
    await engine.engine.started((LINT,))
    assert engine.checks_run == 0


async def test_a_failing_step_adds_todays_startup_fix_as_a_draft_from_startup_fix() -> None:
    engine = Engine(TRIGGERS)
    queued = await engine.engine.check_failed(LINT, "E501 line too long\n")
    [draft] = engine.board.tasks
    assert queued == (draft.id,)
    assert (draft.task.role, draft.origin, draft.status) == (CODER, "startup-fix", "draft")
    assert draft.task.prompt == startup_fix_prompt(LINT, "E501 line too long\n")
    assert draft.task.settings == ROLES[CODER].settings
    assert draft.proposed_by is None
    assert isinstance(engine.events[-1], TasksChanged)


async def test_a_failing_step_adds_no_fix_while_the_coder_is_disabled() -> None:
    engine = Engine(TRIGGERS, roles=disabled(CODER))
    assert await engine.engine.check_failed(LINT, "1 test failed") == ()
    assert engine.board.tasks == ()
    assert engine.events == []


async def test_a_failing_step_fires_the_triggers_taking_its_step() -> None:
    triggers = {
        name.lower(): Trigger(
            on="startup_check_failed", role=CODER, prompt=f"{{step}} {name}", when={"step": name}
        )
        for name in ("Lint", "Tests")
    }
    engine = Engine(triggers)
    await engine.engine.check_failed(LINT, "")
    assert [(origin, prompt) for _, origin, _, prompt in engine.shown()] == [("lint", "Lint Lint")]


# Running


async def test_running_kicks_the_lane_of_each_role_of_the_tasks_still_on_the_board() -> None:
    engine = Engine({})
    first = engine.board.add(TaskSpec(prompt="a", role=REVIEWER))
    second = engine.board.add(TaskSpec(prompt="b", role=REVIEWER))
    gone = engine.board.add(TaskSpec(prompt="c", role=CODER))
    engine.board.remove(gone)
    engine.engine.run((first, second, gone))
    assert engine.kicked == [REVIEWER]
