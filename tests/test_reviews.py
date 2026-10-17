"""When reviews happen and what they may suggest: rounds, batches, the breaker and caps."""

from orbit.agent.domain.reviews import (
    DIFF_LIMIT,
    BatchSnapshot,
    PlannerSnapshot,
    ReviewJob,
    ReviewLimits,
    ReviewPlanner,
    review_prompt,
)
from orbit.protocol import TaskSpec, TaskState, TaskStatus


def state(
    task_id: str,
    status: TaskStatus = "completed",
    origin: str = "manual",
    batch: str | None = None,
    prompt: str = "x",
) -> TaskState:
    return TaskState(
        id=task_id,
        task=TaskSpec(prompt=prompt),
        status=status,
        origin=origin,  # type: ignore[arg-type]
        batch=batch,
    )


def planner(rounds: int = 3, cap: int = 5) -> ReviewPlanner:
    return ReviewPlanner(ReviewLimits(rounds=rounds, suggestions=cap))


def test_a_commit_from_the_users_task_is_reviewed_in_round_zero() -> None:
    reviews = planner()
    job = reviews.landed(state("1"), "abc123")
    assert job == ReviewJob(commits=("abc123",), round=0, batch=None)


def test_a_review_opens_a_batch_its_suggestions_join_one_round_on() -> None:
    reviews = planner()
    job = reviews.landed(state("1"), "abc123")
    assert job is not None
    batch = reviews.open_batch(job)
    assert reviews.round_of(batch) == 1


def filled(reviews: ReviewPlanner, *tasks: str) -> str:
    """A batch whose review suggested ``tasks`` and ended."""
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    for task in tasks:
        reviews.added(batch, task)
    assert reviews.review_ended(batch) is None
    return batch


def test_a_batch_is_reviewed_once_all_its_tasks_have_finished() -> None:
    reviews = planner()
    batch = filled(reviews, "2", "3")
    assert reviews.landed(state("2", origin="review", batch=batch), "c2") is None
    job = reviews.landed(state("3", origin="review", batch=batch), "c3")
    assert job == ReviewJob(commits=("c2", "c3"), round=1, batch=batch)


def test_batch_tasks_that_end_without_a_commit_still_finish_the_batch() -> None:
    reviews = planner()
    batch = filled(reviews, "2", "3")
    assert reviews.ended(state("2", status="gave_up", origin="review", batch=batch)) is None
    job = reviews.landed(state("3", origin="review", batch=batch), "c3")
    assert job == ReviewJob(commits=("c3",), round=1, batch=batch, missing=("2",))


def test_a_batch_whose_tasks_all_failed_is_not_reviewed() -> None:
    reviews = planner()
    batch = filled(reviews, "2")
    assert reviews.ended(state("2", status="error", origin="review", batch=batch)) is None
    assert not reviews.paused


def test_a_batch_with_no_suggestions_is_closed_without_a_review() -> None:
    reviews = planner()
    batch = filled(reviews)
    assert reviews.round_of(batch) == 1
    assert batch not in reviews.open_batches


def test_review_pauses_once_the_next_round_would_pass_the_limit() -> None:
    reviews = planner(rounds=2)
    first = filled(reviews, "2")
    job = reviews.landed(state("2", origin="review", batch=first), "c2")
    assert job is not None and job.round == 1
    second = reviews.open_batch(job)
    assert reviews.round_of(second) == 2
    reviews.added(second, "3")
    reviews.review_ended(second)
    assert reviews.landed(state("3", origin="review", batch=second), "c3") is None
    assert reviews.paused


def test_a_commit_from_the_users_task_resumes_review() -> None:
    reviews = planner(rounds=1)
    batch = filled(reviews, "2")
    reviews.landed(state("2", origin="review", batch=batch), "c2")
    assert reviews.paused
    job = reviews.landed(state("9", origin="planner"), "c9")
    assert job is not None and job.round == 0
    assert not reviews.paused


def test_suggestions_are_capped_per_review() -> None:
    reviews = planner(cap=2)
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    assert reviews.refusal(batch, "One", queued=()) is None
    reviews.added(batch, "2")
    assert reviews.refusal(batch, "Two", queued=()) is None
    reviews.added(batch, "3")
    refusal = reviews.refusal(batch, "Three", queued=())
    assert refusal is not None and "2" in refusal


def test_suggestions_already_queued_are_refused() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    queued = (state("5", status="pending", prompt="Add tests for parse\nmore"),)
    refusal = reviews.refusal(batch, "  add TESTS for parse ", queued=queued)
    assert refusal is not None and "already" in refusal
    finished = (state("5", status="completed", prompt="Add tests for parse"),)
    assert reviews.refusal(batch, "Add tests for parse", queued=finished) is None


def test_tasks_outside_any_batch_from_reviews_are_not_reviewed() -> None:
    reviews = planner()
    assert reviews.landed(state("4", origin="review"), "c4") is None
    assert reviews.ended(state("4", status="error")) is None


def test_a_batch_with_suggestions_stays_open_after_its_review() -> None:
    reviews = planner()
    batch = filled(reviews, "2")
    assert batch in reviews.open_batches


def test_a_task_whose_fix_carries_its_work_on_is_not_missing() -> None:
    reviews = planner()
    batch = filled(reviews, "2", "3")  # Task 3 fixes task 2.
    assert reviews.ended(state("2", status="unverified", origin="review", batch=batch)) is None
    job = reviews.landed(state("3", origin="review", batch=batch), "c3")
    assert job == ReviewJob(commits=("c3",), round=1, batch=batch)


def test_landing_after_its_batch_closed_is_not_reviewed_again() -> None:
    reviews = planner()
    batch = filled(reviews, "2")
    reviews.landed(state("2", origin="review", batch=batch), "c2")
    assert reviews.landed(state("2", origin="review", batch=batch), "c2") is None


def test_a_commit_review_prompt_is_its_title_its_limit_and_the_diff() -> None:
    prompt = review_prompt(ReviewJob(commits=("abc1234def",), round=0, batch=None), "+new", 5)
    assert prompt == "Review commit abc1234\n\nYou may make at most 5 suggestions.\n\n+new"


def test_a_batch_review_prompt_names_its_commits_and_what_did_not_land() -> None:
    job = ReviewJob(commits=("a" * 10, "b" * 10), round=1, batch="batch-1", missing=("7",))
    prompt = review_prompt(job, "diffs", 3)
    assert prompt == (
        "Review the 2 commits made for review batch-1\n\n"
        "Task 7 of that review ended without a commit.\n\n"
        "You may make at most 3 suggestions.\n\n"
        "diffs"
    )


def test_long_diffs_are_trimmed_from_the_end() -> None:
    prompt = review_prompt(
        ReviewJob(commits=("a",), round=0, batch=None), "d" * (DIFF_LIMIT + 50), 5
    )
    assert prompt.endswith("d" * DIFF_LIMIT + "\n… (diff trimmed)")


def test_a_super_review_is_due_every_so_many_commits() -> None:
    reviews = ReviewPlanner(
        ReviewLimits(rounds=3, suggestions=5, super_every=3, super_suggestions=10)
    )
    assert [reviews.count() for _ in range(7)] == [False, False, True, False, False, True, False]


def test_a_super_review_due_while_paused_waits_until_review_resumes() -> None:
    reviews = ReviewPlanner(
        ReviewLimits(rounds=1, suggestions=5, super_every=1, super_suggestions=10)
    )
    batch = filled(reviews, "2")
    reviews.landed(state("2", origin="review", batch=batch), "c2")
    assert reviews.paused
    assert not reviews.count()
    reviews.landed(state("9"), "c9")
    assert reviews.count()


def test_asking_for_a_super_review_restarts_the_count() -> None:
    reviews = ReviewPlanner(
        ReviewLimits(rounds=3, suggestions=5, super_every=2, super_suggestions=10)
    )
    assert not reviews.count()
    job = reviews.super_review("abc")
    assert job == ReviewJob(commits=("abc",), round=0, batch=None, whole=True)
    assert not reviews.count()
    assert reviews.count()


def test_a_super_reviews_batch_has_its_own_cap() -> None:
    reviews = ReviewPlanner(
        ReviewLimits(rounds=3, suggestions=1, super_every=10, super_suggestions=2)
    )
    batch = reviews.open_batch(reviews.super_review("abc"))
    assert reviews.round_of(batch) == 1
    for task_id in ("2", "3"):
        assert reviews.refusal(batch, f"Task {task_id}", queued=()) is None
        reviews.added(batch, task_id)
    refusal = reviews.refusal(batch, "Task 4", queued=())
    assert refusal is not None and "2 tasks" in refusal


def test_a_super_review_prompt_is_its_title_and_its_limit() -> None:
    job = ReviewJob(commits=("abc1234def",), round=0, batch=None, whole=True)
    prompt = review_prompt(job, "", 10)
    assert (
        prompt == "Super review of the codebase at abc1234\n\nYou may make at most 10 suggestions."
    )


def test_a_batch_is_not_reviewed_while_its_review_is_still_suggesting() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2")
    assert reviews.landed(state("2", origin="review", batch=batch), "c2") is None
    assert reviews.refusal(batch, "More", queued=()) is None
    reviews.added(batch, "3")
    assert reviews.landed(state("3", origin="review", batch=batch), "c3") is None
    job = reviews.review_ended(batch)
    assert job == ReviewJob(commits=("c2", "c3"), round=1, batch=batch)
    assert batch not in reviews.open_batches


def test_a_batch_still_working_when_its_review_ends_is_reviewed_later() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2")
    assert reviews.review_ended(batch) is None
    assert batch in reviews.open_batches
    job = reviews.landed(state("2", origin="review", batch=batch), "c2")
    assert job == ReviewJob(commits=("c2",), round=1, batch=batch)


def test_suggesting_into_a_closed_batch_is_refused() -> None:
    reviews = planner()
    batch = filled(reviews)
    refusal = reviews.refusal(batch, "Late", queued=())
    assert refusal is not None and "over" in refusal


def test_suggesting_into_a_batch_whose_review_ended_is_refused() -> None:
    reviews = planner()
    batch = filled(reviews, "2")
    assert batch in reviews.open_batches
    refusal = reviews.refusal(batch, "Late", queued=())
    assert refusal is not None and "review is over" in refusal


def test_a_batch_is_filling_only_while_its_review_runs() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2")
    assert reviews.filling(batch)
    reviews.review_ended(batch)
    assert not reviews.filling(batch)
    assert not reviews.filling("batch-9")


def test_removing_a_batchs_last_unfinished_task_has_the_batch_reviewed_without_it() -> None:
    reviews = planner()
    batch = filled(reviews, "2", "3")
    assert reviews.landed(state("2", origin="review", batch=batch), "c2") is None
    job = reviews.removed(state("3", status="pending", origin="review", batch=batch))
    assert job == ReviewJob(commits=("c2",), round=1, batch=batch)
    assert reviews.open_batches == ()


def test_after_a_removal_the_batch_is_reviewed_once_the_rest_finish() -> None:
    reviews = planner()
    batch = filled(reviews, "2", "3")
    assert reviews.removed(state("2", status="pending", origin="review", batch=batch)) is None
    job = reviews.landed(state("3", origin="review", batch=batch), "c3")
    assert job == ReviewJob(commits=("c3",), round=1, batch=batch)


def test_removing_a_batchs_only_task_closes_it_with_nothing_to_review() -> None:
    reviews = planner()
    batch = filled(reviews, "2")
    assert reviews.removed(state("2", status="pending", origin="review", batch=batch)) is None
    assert reviews.open_batches == ()
    assert not reviews.paused


def test_removing_a_task_of_a_batch_still_filling_leaves_it_open() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2")
    assert reviews.removed(state("2", status="pending", origin="review", batch=batch)) is None
    assert reviews.open_batches == (batch,)
    assert reviews.review_ended(batch) is None
    assert not reviews.open_batches


def test_removing_a_task_outside_any_open_batch_changes_nothing() -> None:
    reviews = planner()
    batch = filled(reviews, "2")
    assert reviews.removed(state("1")) is None
    assert reviews.removed(state("9", origin="review", batch="batch-7")) is None
    assert reviews.open_batches == (batch,)


def test_a_reopened_task_holds_its_batch_open_until_it_finishes_again() -> None:
    reviews = planner()
    batch = filled(reviews, "2", "3")
    assert reviews.ended(state("2", status="error", origin="review", batch=batch)) is None
    reviews.reopened(state("2", status="pending", origin="review", batch=batch))
    assert reviews.landed(state("3", origin="review", batch=batch), "c3") is None
    job = reviews.landed(state("2", origin="review", batch=batch), "c2")
    assert job == ReviewJob(commits=("c3", "c2"), round=1, batch=batch)


def test_a_reopened_task_that_had_landed_is_waited_for_again() -> None:
    reviews = planner()
    batch = filled(reviews, "2", "3")
    assert reviews.landed(state("2", origin="review", batch=batch), "c2") is None
    reviews.reopened(state("2", status="pending", origin="review", batch=batch))
    assert reviews.landed(state("3", origin="review", batch=batch), "c3") is None
    assert reviews.open_batches == (batch,)


def test_reopening_a_task_outside_any_open_batch_changes_nothing() -> None:
    reviews = planner()
    batch = filled(reviews, "2")
    reviews.reopened(state("1", status="pending"))
    reviews.reopened(state("9", status="pending", origin="review", batch="batch-7"))
    job = reviews.landed(state("2", origin="review", batch=batch), "c2")
    assert job == ReviewJob(commits=("c2",), round=1, batch=batch)


def busy() -> ReviewPlanner:
    """A planner part way through a session: paused, one batch closed, one waiting for a task
    and one still filling."""
    reviews = planner(rounds=1, cap=2)
    done = filled(reviews, "2")
    reviews.landed(state("2", origin="review", batch=done), "c2")
    waiting = filled(reviews, "3", "4")
    reviews.ended(state("3", status="gave_up", origin="review", batch=waiting))
    reviews.open_batch(ReviewJob(commits=("c5",), round=0, batch=None))
    return reviews


def restored(snapshot: PlannerSnapshot) -> ReviewPlanner:
    reviews = planner(rounds=1, cap=2)
    reviews.restore(PlannerSnapshot.model_validate_json(snapshot.model_dump_json()))
    return reviews


def test_a_snapshot_holds_the_planners_session_state() -> None:
    reviews = planner(rounds=1, cap=2)
    batch = filled(reviews, "2", "3")
    reviews.ended(state("2", status="gave_up", origin="review", batch=batch))
    reviews.landed(state("3", origin="review", batch=batch), "c3")
    reviews.open_batch(ReviewJob(commits=("c4",), round=0, batch=None))
    assert reviews.snapshot() == PlannerSnapshot(
        paused=True,
        next_batch=3,
        batches={"batch-2": BatchSnapshot(round=1, cap=2)},
        rounds={"batch-1": 1, "batch-2": 1},
    )


def test_a_snapshot_survives_json_and_restores_the_same_state() -> None:
    reviews = busy()
    assert restored(reviews.snapshot()).snapshot() == reviews.snapshot()


def test_a_restored_planner_numbers_batches_on_from_where_it_was() -> None:
    reviews = busy()
    again = restored(reviews.snapshot())
    job = ReviewJob(commits=("c6",), round=0, batch=None)
    assert again.open_batch(job) == reviews.open_batch(job) == "batch-4"


def test_a_restored_planner_refuses_and_reviews_as_before() -> None:
    reviews = busy()
    again = restored(reviews.snapshot())
    for each in (reviews, again):
        assert each.refusal("batch-3", "Add tests", ()) is None
        each.added("batch-3", "5")
        each.added("batch-3", "6")
        assert each.refusal("batch-3", "More", ()) is not None
        assert each.refusal("batch-1", "More", ()) is not None
        assert each.review_ended("batch-3") is None
        each.landed(state("5", origin="review", batch="batch-3"), "c5")
        assert each.round_of("batch-3") == 1
    assert again.landed(state("6", origin="review", batch="batch-3"), "c6") is None
    assert reviews.landed(state("6", origin="review", batch="batch-3"), "c6") is None
    assert again.snapshot() == reviews.snapshot()


def test_restoring_replaces_what_the_planner_had() -> None:
    reviews = busy()
    reviews.restore(PlannerSnapshot())
    assert reviews.snapshot() == PlannerSnapshot()
    assert not reviews.open_batches


def test_the_super_review_count_is_kept_apart_from_snapshots() -> None:
    reviews = ReviewPlanner(ReviewLimits(super_every=3))
    reviews.count()
    snapshot = reviews.snapshot()
    reviews.since_super = 2
    reviews.restore(snapshot)
    assert reviews.since_super == 2
    assert reviews.count()
    assert reviews.since_super == 0
