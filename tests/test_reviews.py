"""When reviews happen and what they may suggest: rounds, batches, the breaker and caps."""

from orbit.agent.domain.reviews import DIFF_LIMIT, ReviewJob, ReviewPlanner, review_prompt
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
    return ReviewPlanner(rounds=rounds, cap=cap)


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


def test_a_batch_is_reviewed_once_all_its_tasks_have_finished() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2")
    reviews.added(batch, "3")
    assert reviews.landed(state("2", origin="review", batch=batch), "c2") is None
    job = reviews.landed(state("3", origin="review", batch=batch), "c3")
    assert job == ReviewJob(commits=("c2", "c3"), round=1, batch=batch)


def test_batch_tasks_that_end_without_a_commit_still_finish_the_batch() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2")
    reviews.added(batch, "3")
    assert reviews.ended(state("2", status="gave_up", origin="review", batch=batch)) is None
    job = reviews.landed(state("3", origin="review", batch=batch), "c3")
    assert job == ReviewJob(commits=("c3",), round=1, batch=batch, missing=("2",))


def test_a_batch_whose_tasks_all_failed_is_not_reviewed() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2")
    assert reviews.ended(state("2", status="error", origin="review", batch=batch)) is None
    assert not reviews.paused


def test_a_batch_with_no_suggestions_is_closed_without_a_review() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.close_if_empty(batch)
    assert reviews.round_of(batch) == 1
    assert batch not in reviews.open_batches


def test_review_pauses_once_the_next_round_would_pass_the_limit() -> None:
    reviews = planner(rounds=2)
    first = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(first, "2")
    job = reviews.landed(state("2", origin="review", batch=first), "c2")
    assert job is not None and job.round == 1
    second = reviews.open_batch(job)
    assert reviews.round_of(second) == 2
    reviews.added(second, "3")
    assert reviews.landed(state("3", origin="review", batch=second), "c3") is None
    assert reviews.paused


def test_a_commit_from_the_users_task_resumes_review() -> None:
    reviews = planner(rounds=1)
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2")
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
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2")
    reviews.close_if_empty(batch)
    assert batch in reviews.open_batches


def test_a_task_whose_fix_carries_its_work_on_is_not_missing() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2")
    reviews.added(batch, "3")  # The fix of task 2.
    assert reviews.ended(state("2", status="unverified", origin="review", batch=batch)) is None
    job = reviews.landed(state("3", origin="review", batch=batch), "c3")
    assert job == ReviewJob(commits=("c3",), round=1, batch=batch)


def test_landing_after_its_batch_closed_is_not_reviewed_again() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2")
    reviews.landed(state("2", origin="review", batch=batch), "c2")
    assert reviews.landed(state("2", origin="review", batch=batch), "c2") is None


def test_a_commit_review_prompt_asks_for_suggestions_and_includes_the_diff() -> None:
    prompt = review_prompt(ReviewJob(commits=("abc1234def",), round=0, batch=None), "+new", 5)
    assert prompt.startswith("Review commit abc1234\n")
    assert "suggest_task" in prompt and "at most 5" in prompt and "complete_task" in prompt
    assert prompt.endswith("+new")


def test_a_batch_review_prompt_names_its_commits_and_what_did_not_land() -> None:
    job = ReviewJob(commits=("a" * 10, "b" * 10), round=1, batch="batch-1", missing=("7",))
    prompt = review_prompt(job, "diffs", 3)
    assert prompt.startswith("Review the 2 commits made for review batch-1\n")
    assert "task 7" in prompt


def test_long_diffs_are_trimmed_from_the_end() -> None:
    prompt = review_prompt(
        ReviewJob(commits=("a",), round=0, batch=None), "d" * (DIFF_LIMIT + 50), 5
    )
    assert prompt.endswith("d" * DIFF_LIMIT + "\n… (diff trimmed)")
