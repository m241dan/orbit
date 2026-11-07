"""When reviews happen and what they may suggest: rounds, batches, the breaker and caps."""

from collections.abc import Sequence
from dataclasses import fields

import pytest

from orbit.agent.domain.review_sample import Choose
from orbit.agent.domain.reviews import (
    DIFF_LIMIT,
    FILES_LIMIT,
    LANDED_LIMIT,
    PENDING_LIMIT,
    BatchSnapshot,
    Finding,
    FindingOutcome,
    LandedSince,
    LaneSnapshot,
    PlannerSnapshot,
    ReviewGroup,
    ReviewJob,
    ReviewLimits,
    ReviewPlanner,
    SuperReviewCount,
    _Batch,
    kept_bookings,
    kept_groups,
    pause_notice,
    pending_titles,
    raised_roles,
    review_prompt,
    review_skills,
    runnable_tasks,
    tell_landed_since,
)
from orbit.protocol import (
    CONFIGURATOR,
    REVIEW_ORIGIN,
    REVIEWER,
    STYLE_REVIEWER,
    SUPER_REVIEW,
    TaskSpec,
    TaskState,
    TaskStatus,
)


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
        origin=origin,
        batch=batch,
    )


def planner(rounds: int = 3, cap: int = 5) -> ReviewPlanner:
    return ReviewPlanner(ReviewLimits(rounds=rounds, suggestions=cap))


def test_a_commit_from_the_users_task_is_reviewed_in_round_zero() -> None:
    reviews = planner()
    job = reviews.landed(state("1"), "abc123")
    assert job == ReviewJob(commits=("abc123",), round=0, batch=None, reviewed=("1",))


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
    assert job == ReviewJob(commits=("c2", "c3"), round=1, batch=batch, reviewed=("2", "3"))


def test_batch_tasks_that_end_without_a_commit_still_finish_the_batch() -> None:
    reviews = planner()
    batch = filled(reviews, "2", "3")
    assert reviews.ended(state("2", status="gave_up", origin="review", batch=batch)) is None
    job = reviews.landed(state("3", origin="review", batch=batch), "c3")
    assert job == ReviewJob(commits=("c3",), round=1, batch=batch, missing=("2",), reviewed=("3",))


def test_a_batch_whose_tasks_all_failed_is_not_reviewed() -> None:
    reviews = planner()
    batch = filled(reviews, "2")
    assert reviews.ended(state("2", status="error", origin="review", batch=batch)) is None
    assert not reviews.paused


def test_a_batch_with_no_suggestions_is_closed_without_a_review() -> None:
    reviews = planner()
    batch = filled(reviews)
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
    assert reviews.refusal(batch, "One", tasks=()) is None
    reviews.added(batch, "2")
    assert reviews.refusal(batch, "Two", tasks=()) is None
    reviews.added(batch, "3")
    refusal = reviews.refusal(batch, "Three", tasks=())
    assert refusal is not None and "2" in refusal


def test_tasks_a_review_filed_into_batches_of_their_own_count_toward_its_cap() -> None:
    reviews = planner(cap=2)
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    assert reviews.refusal(batch, "One", tasks=(), elsewhere=1) is None
    reviews.added(batch, "2")
    refusal = reviews.refusal(batch, "Two", tasks=(), elsewhere=1)
    assert refusal is not None and "2" in refusal


def test_a_fix_batch_is_reviewed_once_its_task_lands_as_no_review_fills_it() -> None:
    reviews = planner()
    batch = reviews.open_fix_batch(2)
    reviews.added(batch, "4", "Fix the crash")
    assert reviews.round_of(batch) == 2 and not reviews.filling(batch)
    job = reviews.landed(state("4", origin="review", batch=batch), "c4")
    assert job == ReviewJob(commits=("c4",), round=2, batch=batch, reviewed=("4",))
    assert batch not in reviews.open_batches


def test_suggestions_already_queued_are_refused() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    queued = (state("5", status="pending", prompt="Add tests for parse\nmore"),)
    refusal = reviews.refusal(batch, "  add TESTS for parse ", tasks=queued)
    assert refusal == "Task 5 already covers that."


def test_a_suggestion_a_finished_task_covers_is_refused_with_its_id() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    finished = (state("5", status="completed", prompt="Add tests for parse"),)
    assert reviews.refusal(batch, "Add tests for parse", finished) == "Task 5 already covers that."


def test_a_suggestion_a_task_of_any_status_covers_is_refused() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    statuses: tuple[TaskStatus, ...] = (
        "draft",
        "running",
        "verifying",
        "unverified",
        "gave_up",
        "error",
        "cancelled",
    )
    for status in statuses:
        covered = (state("5", status=status, prompt="Add tests"),)
        assert reviews.refusal(batch, "Add tests", covered) == "Task 5 already covers that."


def test_a_reworded_suggestion_is_accepted() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2", "Add tests for parse")
    covered = (state("5", status="completed", prompt="Add tests for parse"),)
    assert reviews.refusal(batch, "Add parse tests", covered) is None


def test_a_suggestion_an_earlier_review_made_is_refused_once_its_task_is_gone() -> None:
    reviews = planner()
    first = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(first, "2", "Add tests for parse\nmore")
    assert reviews.removed(state("2", status="pending", origin="review", batch=first)) is None
    second = reviews.open_batch(ReviewJob(commits=("b",), round=0, batch=None))
    refusal = reviews.refusal(second, "  add TESTS for parse ", ())
    assert refusal == "An earlier review already suggested that."


def test_a_suggestion_still_on_the_board_is_refused_by_its_task() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2", "Add tests")
    queued = (state("2", status="pending", origin="review", batch=batch, prompt="Add tests"),)
    assert reviews.refusal(batch, "Add tests", queued) == "Task 2 already covers that."


def test_a_fix_joining_a_batch_is_not_remembered_as_a_suggestion() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "3")
    assert reviews.snapshot().suggested == frozenset()


def test_a_closed_batch_and_the_cap_are_refused_before_a_repeat() -> None:
    reviews = planner(cap=1)
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2", "Add tests")
    refusal = reviews.refusal(batch, "Add tests", ())
    assert refusal is not None and "1 tasks" in refusal
    assert reviews.review_ended(batch) is None
    refusal = reviews.refusal(batch, "Add tests", ())
    assert refusal is not None and "review is over" in refusal


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
    assert job == ReviewJob(commits=("c3",), round=1, batch=batch, reviewed=("3",))


def test_a_batch_review_names_the_tasks_that_landed_in_task_order() -> None:
    reviews = planner()
    batch = filled(reviews, "2", "3", "4")
    assert reviews.landed(state("4", origin="review", batch=batch), "c4") is None
    assert reviews.ended(state("3", status="unverified", origin="review", batch=batch)) is None
    job = reviews.landed(state("2", origin="review", batch=batch), "c2")
    assert job is not None and job.reviewed == ("2", "4")


def test_a_super_review_reviews_no_particular_task() -> None:
    assert planner().start_super_review("abc").reviewed == ()


def test_a_lane_snapshot_saved_before_jobs_named_their_tasks_still_loads() -> None:
    saved = {"planner": {}, "jobs": {"2": {"commits": ["c1"], "round": 0, "batch": None}}}
    snapshot = LaneSnapshot.model_validate(saved)
    assert snapshot.jobs["2"] == ReviewJob(commits=("c1",), round=0, batch=None)


def test_landing_after_its_batch_closed_is_not_reviewed_again() -> None:
    reviews = planner()
    batch = filled(reviews, "2")
    reviews.landed(state("2", origin="review", batch=batch), "c2")
    assert reviews.landed(state("2", origin="review", batch=batch), "c2") is None


def test_a_commit_review_prompt_is_its_title_its_limit_and_the_diff() -> None:
    prompt = review_prompt(ReviewJob(commits=("abc1234def",), round=0, batch=None), "+new", 5, ())
    assert prompt == "Review commit abc1234\n\nYou may make at most 5 suggestions.\n\n+new"


def test_a_batch_review_prompt_names_its_commits_and_what_did_not_land() -> None:
    job = ReviewJob(commits=("a" * 10, "b" * 10), round=1, batch="batch-1", missing=("7",))
    prompt = review_prompt(job, "diffs", 3, ())
    assert prompt == (
        "Review the 2 commits made for review batch-1\n\n"
        "Task 7 of that review ended without landing a commit.\n\n"
        "You may make at most 3 suggestions.\n\n"
        "diffs"
    )


def test_a_review_prompt_lists_the_tasks_queued_before_the_diff() -> None:
    job = ReviewJob(commits=("abc1234def",), round=0, batch=None)
    prompt = review_prompt(job, "+new", 5, ("Add tests", "Rename x"))
    assert prompt == (
        "Review commit abc1234\n\n"
        "You may make at most 5 suggestions.\n\n"
        "Tasks already queued (don't suggest these):\n- Add tests\n- Rename x\n\n"
        "+new"
    )


def test_a_review_prompt_names_only_so_many_queued_tasks_and_counts_the_rest() -> None:
    job = ReviewJob(commits=("abc1234def",), round=0, batch=None, whole=True)
    pending = tuple(f"Task {n}" for n in range(PENDING_LIMIT + 3))
    prompt = review_prompt(job, "", 10, pending)
    listed = prompt.split("Tasks already queued (don't suggest these):\n", 1)[1]
    assert listed == "\n".join([*(f"- {line}" for line in pending[:PENDING_LIMIT]), "… and 3 more"])
    assert f"- Task {PENDING_LIMIT}" not in prompt


def test_a_review_prompt_lists_exactly_the_limit_without_a_count() -> None:
    job = ReviewJob(commits=("a",), round=0, batch=None)
    prompt = review_prompt(job, "", 5, tuple(f"Task {n}" for n in range(PENDING_LIMIT)))
    assert f"- Task {PENDING_LIMIT - 1}\n\n" in prompt and "more" not in prompt


LANDED = "Landed since (check your findings against these):"


def test_a_review_prompt_lists_what_landed_since_oldest_first_before_the_diff() -> None:
    job = ReviewJob(commits=("abc1234def",), round=0, batch=None)
    landed = LandedSince(("Add B", "Add C"))
    prompt = review_prompt(job, "+new", 5, ("Add tests",), landed)
    assert prompt == (
        "Review commit abc1234\n\n"
        "You may make at most 5 suggestions.\n\n"
        "Tasks already queued (don't suggest these):\n- Add tests\n\n"
        f"{LANDED}\n- Add B\n- Add C\n\n"
        "+new"
    )


def test_a_review_prompt_with_nothing_landed_since_has_no_such_section() -> None:
    job = ReviewJob(commits=("abc1234def",), round=0, batch=None)
    assert "Landed since" not in review_prompt(job, "+new", 5, (), LandedSince())


def test_a_review_prompt_names_only_so_many_landed_commits_and_counts_the_rest() -> None:
    job = ReviewJob(commits=("a" * 10, "b" * 10), round=1, batch="batch-1")
    landed = tuple(f"Commit {n}" for n in range(LANDED_LIMIT + 5))
    prompt = review_prompt(job, "diffs", 5, (), LandedSince(landed))
    listed = prompt.split(f"{LANDED}\n", 1)[1]
    names = [f"- {line}" for line in landed[:LANDED_LIMIT]]
    assert listed == "\n".join([*names, "… and 5 more", "", "diffs"])
    assert LANDED_LIMIT == 40


def test_a_review_prompt_lists_no_files_the_commits_landed_since_changed() -> None:
    job = ReviewJob(commits=("abc1234def",), round=0, batch=None)
    prompt = review_prompt(job, "+new", 5, (), LandedSince(("Add B",), ("b.py",)))
    assert "b.py" not in prompt and "Files" not in prompt


VALUES = {"commit": "abc1234", "diff": "+new", "landed_since": ""}


def test_a_reviews_values_told_what_landed_list_it_oldest_first_and_keep_the_rest() -> None:
    told = tell_landed_since(VALUES, ("Add B", "Add C"))
    assert told == VALUES | {"landed_since": f"{LANDED}\n- Add B\n- Add C"}


def test_a_reviews_values_told_again_what_landed_list_only_the_latest() -> None:
    first = tell_landed_since(VALUES, ("Add B",))
    assert tell_landed_since(first, ("Add B", "Add C")) == tell_landed_since(
        VALUES, ("Add B", "Add C")
    )


def test_a_reviews_values_told_again_that_nothing_landed_list_nothing() -> None:
    first = tell_landed_since(VALUES, ("Add B",))
    assert tell_landed_since(first, ()) == VALUES


def test_a_reviews_values_told_that_too_many_landed_name_only_so_many() -> None:
    subjects = tuple(f"Commit {n}" for n in range(LANDED_LIMIT + 5))
    listed = tell_landed_since(VALUES, subjects)["landed_since"].split(f"{LANDED}\n", 1)[1]
    names = [f"- {line}" for line in subjects[:LANDED_LIMIT]]
    assert listed == "\n".join([*names, "… and 5 more"])


SINCE_SUPER = "Commits since the last super review:"
FILES = "Files they changed:"
SUPER = ReviewJob(commits=("abc1234def",), round=0, batch=None, whole=True)


def test_a_super_review_prompt_lists_the_commits_since_the_last_and_their_files_sorted() -> None:
    landed = LandedSince(("Add D", "Add E"), ("src/e.py", "README.md", "src/d.py"))
    prompt = review_prompt(SUPER, "", 10, ("Add tests",), landed)
    assert prompt == (
        "Super review of the codebase at abc1234\n\n"
        "You may make at most 10 suggestions.\n\n"
        "Tasks already queued (don't suggest these):\n- Add tests\n\n"
        f"{SINCE_SUPER}\n- Add D\n- Add E\n\n"
        f"{FILES}\n- README.md\n- src/d.py\n- src/e.py"
    )


def test_a_super_review_prompt_lists_every_commit_since_the_last() -> None:
    subjects = tuple(f"Commit {n}" for n in range(LANDED_LIMIT + 5))
    prompt = review_prompt(SUPER, "", 10, (), LandedSince(subjects))
    listed = prompt.split(f"{SINCE_SUPER}\n", 1)[1]
    assert listed == "\n".join(f"- {subject}" for subject in subjects)


def test_a_super_review_prompt_names_only_so_many_files_and_counts_the_rest() -> None:
    files = tuple(f"src/f{n:03}.py" for n in range(FILES_LIMIT + 20))
    prompt = review_prompt(SUPER, "", 10, (), LandedSince(("Add F",), files[::-1]))
    listed = prompt.split(f"{FILES}\n", 1)[1]
    names = [f"- {path}" for path in files[:FILES_LIMIT]]
    assert listed == "\n".join([*names, "… and 20 more"])
    assert FILES_LIMIT == 100


def test_a_super_review_prompt_with_no_commits_since_the_last_lists_neither() -> None:
    for landed in (LandedSince(), LandedSince((), ("b.py",))):
        prompt = review_prompt(SUPER, "", 10, (), landed)
        assert prompt == (
            "Super review of the codebase at abc1234\n\nYou may make at most 10 suggestions."
        )


def test_a_super_review_prompt_with_commits_that_changed_no_files_lists_no_files() -> None:
    prompt = review_prompt(SUPER, "", 10, (), LandedSince(("Empty commit",)))
    assert prompt.endswith(f"{SINCE_SUPER}\n- Empty commit") and FILES not in prompt


SAMPLED = "Sampled files (read these too):"


def test_a_super_review_prompt_lists_the_sampled_files_after_the_changed_ones() -> None:
    landed = LandedSince(("Add D",), ("src/d.py",), ("src/a.py", "src/b.cpp"))
    prompt = review_prompt(SUPER, "", 10, (), landed)
    assert prompt.endswith(
        f"{SINCE_SUPER}\n- Add D\n\n{FILES}\n- src/d.py\n\n{SAMPLED}\n- src/a.py\n- src/b.cpp"
    )


def test_the_first_super_review_prompt_lists_the_sampled_files_alone() -> None:
    prompt = review_prompt(SUPER, "", 10, (), LandedSince(sampled=("src/a.py",)))
    assert prompt == (
        "Super review of the codebase at abc1234\n\n"
        "You may make at most 10 suggestions.\n\n"
        f"{SAMPLED}\n- src/a.py"
    )


def test_a_super_review_prompt_with_nothing_sampled_has_no_such_section() -> None:
    prompt = review_prompt(SUPER, "", 10, (), LandedSince(("Add D",), ("src/d.py",)))
    assert SAMPLED not in prompt


def test_a_review_prompt_lists_no_sampled_files() -> None:
    job = ReviewJob(commits=("abc1234def",), round=0, batch=None)
    prompt = review_prompt(job, "+new", 5, (), LandedSince(sampled=("src/a.py",)))
    assert "src/a.py" not in prompt and SAMPLED not in prompt


def test_the_tasks_still_to_run_or_running_are_listed_by_their_first_lines() -> None:
    tasks = (
        state("1", status="completed", prompt="Done already"),
        state("2", status="pending", prompt="  Add tests \n\nPlease."),
        state("3", status="running", prompt="Rename x"),
        state("4", status="verifying", prompt="Tidy"),
        state("5", status="draft", prompt="A draft"),
        state("6", status="gave_up", prompt="Gave up"),
    )
    assert pending_titles(tasks) == ("Add tests", "Rename x", "Tidy")


def orbits(task_id: str, status: TaskStatus, role: str) -> TaskState:
    return TaskState(id=task_id, task=TaskSpec(prompt=f"A {role} task", role=role), status=status)


def test_the_tasks_orbit_queued_for_itself_are_not_listed() -> None:
    tasks = (
        orbits("1", "pending", REVIEWER),
        orbits("2", "running", STYLE_REVIEWER),
        orbits("3", "pending", CONFIGURATOR),
        state("4", status="pending", prompt="Add tests"),
        state("5", status="running", prompt="Rename x"),
        state("6", status="verifying", prompt="Tidy"),
    )
    assert pending_titles(tasks) == ("Add tests", "Rename x", "Tidy")


def test_long_diffs_are_trimmed_from_the_end() -> None:
    prompt = review_prompt(
        ReviewJob(commits=("a",), round=0, batch=None), "d" * (DIFF_LIMIT + 50), 5, ()
    )
    assert prompt.endswith("d" * DIFF_LIMIT + "\n… (diff trimmed)")


def test_a_super_review_is_of_the_whole_codebase_at_its_commit() -> None:
    job = planner().start_super_review("abc")
    assert job == ReviewJob(commits=("abc",), round=0, batch=None, whole=True)


def test_a_super_review_starts_from_the_commit_it_reviews_after_the_first() -> None:
    reviews = planner()
    assert reviews.last_super_commit is None
    reviews.start_super_review("abc")
    assert reviews.last_super_commit == "abc"
    reviews.start_super_review("def")
    assert reviews.super_count == SuperReviewCount(0, "def", 2)


def test_starting_a_super_review_leaves_the_count_to_its_trigger() -> None:
    reviews = planner()
    reviews.counts = {SUPER_REVIEW: 4, "nightly": 2}
    reviews.start_super_review("abc")
    assert reviews.counts == {SUPER_REVIEW: 4, "nightly": 2}


def test_the_super_review_count_is_the_super_review_triggers_and_that_ones_commit() -> None:
    reviews = planner()
    assert reviews.super_count == SuperReviewCount(0, None)
    reviews.counts = {SUPER_REVIEW: 1, "nightly": 5}
    assert reviews.super_count == SuperReviewCount(1, None)
    reviews.super_count = SuperReviewCount(2, "abc")
    assert (reviews.counts, reviews.last_super_commit) == ({SUPER_REVIEW: 2, "nightly": 5}, "abc")


def test_the_super_review_count_holds_the_super_reviews_and_the_files_each_last_sampled() -> None:
    reviews = planner()
    reviews.super_count = SuperReviewCount(1, "abc", 4, {"a.py": 2})
    assert (reviews.super_reviews, reviews.sampled) == (4, {"a.py": 2})
    assert reviews.super_count == SuperReviewCount(1, "abc", 4, {"a.py": 2})


def test_each_super_review_queued_counts_one_more_super_review() -> None:
    reviews = planner()
    assert reviews.super_reviews == 0
    reviews.start_super_review("abc")
    reviews.start_super_review("def")
    assert reviews.super_reviews == 2


ABC = ("a.py", "b.py", "c.py")
UNCOUNTED = SuperReviewCount()


def sampling(size: int, count: SuperReviewCount = UNCOUNTED) -> ReviewPlanner:
    """A planner whose super reviews sample ``size`` files, counting on from ``count``."""
    reviews = ReviewPlanner(ReviewLimits(super_sample=size))
    reviews.super_count = count
    return reviews


def sampled_by_next(
    reviews: ReviewPlanner, choose: Choose, tracked: Sequence[str] = ABC
) -> tuple[str, ...]:
    """What the next super review ``reviews`` queues samples of ``tracked``, with ``choose``."""
    reviews.start_super_review("abc")
    return reviews.sample(tracked, (), (), choose)


def first(paths: Sequence[str], size: int) -> list[str]:
    return list(paths[:size])


def last(paths: Sequence[str], size: int) -> list[str]:
    return list(paths[::-1][:size])


def test_the_second_super_review_samples_the_file_never_sampled_and_one_of_the_rest() -> None:
    reviews = sampling(2)
    assert sampled_by_next(reviews, first) == ("a.py", "b.py")
    assert reviews.sampled == {"a.py": 1, "b.py": 1}
    after = reviews.super_count
    assert sampled_by_next(reviews, first) == ("a.py", "c.py")
    reviews.super_count = after
    assert sampled_by_next(reviews, last) == ("b.py", "c.py")


def test_the_sample_rotates_through_every_file_before_any_comes_up_again() -> None:
    reviews = sampling(1, SuperReviewCount(super_reviews=3, sampled={"a.py": 1, "b.py": 3}))
    samples = [sampled_by_next(reviews, first) for _ in range(3)]
    assert samples == [("c.py",), ("a.py",), ("b.py",)]
    assert reviews.super_count == SuperReviewCount(0, "abc", 6, {"a.py": 5, "b.py": 6, "c.py": 4})


def test_a_file_no_longer_tracked_is_forgotten_when_the_next_super_review_samples() -> None:
    reviews = sampling(1, SuperReviewCount(super_reviews=1, sampled={"gone.py": 1, "a.py": 1}))
    assert sampled_by_next(reviews, first, ("a.py", "b.py")) == ("b.py",)
    assert reviews.sampled == {"a.py": 1, "b.py": 2}


def test_a_super_review_sampling_nothing_still_forgets_files_no_longer_tracked() -> None:
    reviews = sampling(0, SuperReviewCount(super_reviews=1, sampled={"gone.py": 1}))
    assert sampled_by_next(reviews, first) == ()
    assert reviews.super_count == SuperReviewCount(0, "abc", 2, {})


def test_the_sample_leaves_out_the_files_changed_and_those_outside_the_mapped_paths() -> None:
    reviews = sampling(10)
    reviews.start_super_review("abc")
    tracked = ("src/a.py", "src/b.py", "tools/c.py")
    assert reviews.sample(tracked, ["src"], ["src/a.py"], first) == ("src/b.py",)


def test_a_super_reviews_batch_has_its_own_cap() -> None:
    reviews = ReviewPlanner(ReviewLimits(rounds=3, suggestions=1, super_suggestions=2))
    batch = reviews.open_batch(reviews.start_super_review("abc"))
    assert reviews.round_of(batch) == 1
    for task_id in ("2", "3"):
        assert reviews.refusal(batch, f"Task {task_id}", tasks=()) is None
        reviews.added(batch, task_id)
    refusal = reviews.refusal(batch, "Task 4", tasks=())
    assert refusal is not None and "2 tasks" in refusal


def test_a_batchs_cap_is_the_one_it_opened_with_even_once_restored_under_other_limits() -> None:
    reviews = ReviewPlanner(ReviewLimits(suggestions=1, super_suggestions=2))
    batch = reviews.open_batch(ReviewJob(commits=("abc",), round=0, batch=None))
    whole = reviews.open_batch(reviews.start_super_review("abc"))
    again = ReviewPlanner(ReviewLimits(suggestions=4, super_suggestions=8))
    again.restore(reviews.snapshot())
    assert (again.cap_of(batch), again.cap_of(whole)) == (1, 2)


def test_a_super_review_prompt_is_its_title_and_its_limit() -> None:
    job = ReviewJob(commits=("abc1234def",), round=0, batch=None, whole=True)
    prompt = review_prompt(job, "", 10, ())
    assert (
        prompt == "Super review of the codebase at abc1234\n\nYou may make at most 10 suggestions."
    )


def test_a_batch_is_not_reviewed_while_its_review_is_still_suggesting() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2")
    assert reviews.landed(state("2", origin="review", batch=batch), "c2") is None
    assert reviews.refusal(batch, "More", tasks=()) is None
    reviews.added(batch, "3")
    assert reviews.landed(state("3", origin="review", batch=batch), "c3") is None
    job = reviews.review_ended(batch)
    assert job == ReviewJob(commits=("c2", "c3"), round=1, batch=batch, reviewed=("2", "3"))
    assert batch not in reviews.open_batches


def test_a_batch_still_working_when_its_review_ends_is_reviewed_later() -> None:
    reviews = planner()
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    reviews.added(batch, "2")
    assert reviews.review_ended(batch) is None
    assert batch in reviews.open_batches
    job = reviews.landed(state("2", origin="review", batch=batch), "c2")
    assert job == ReviewJob(commits=("c2",), round=1, batch=batch, reviewed=("2",))


def test_suggesting_into_a_closed_batch_is_refused() -> None:
    reviews = planner()
    batch = filled(reviews)
    refusal = reviews.refusal(batch, "Late", tasks=())
    assert refusal is not None and "over" in refusal


def test_suggesting_into_a_batch_whose_review_ended_is_refused() -> None:
    reviews = planner()
    batch = filled(reviews, "2")
    assert batch in reviews.open_batches
    refusal = reviews.refusal(batch, "Late", tasks=())
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
    assert job == ReviewJob(commits=("c2",), round=1, batch=batch, reviewed=("2",))
    assert reviews.open_batches == ()


def test_after_a_removal_the_batch_is_reviewed_once_the_rest_finish() -> None:
    reviews = planner()
    batch = filled(reviews, "2", "3")
    assert reviews.removed(state("2", status="pending", origin="review", batch=batch)) is None
    job = reviews.landed(state("3", origin="review", batch=batch), "c3")
    assert job == ReviewJob(commits=("c3",), round=1, batch=batch, reviewed=("3",))


def test_a_removed_task_that_ended_without_a_commit_is_not_reported_missing() -> None:
    reviews = planner()
    batch = filled(reviews, "2", "3")
    assert reviews.ended(state("2", status="gave_up", origin="review", batch=batch)) is None
    assert reviews.removed(state("2", status="gave_up", origin="review", batch=batch)) is None
    job = reviews.landed(state("3", origin="review", batch=batch), "c3")
    assert job == ReviewJob(commits=("c3",), round=1, batch=batch, reviewed=("3",))
    assert "ended without landing a commit" not in review_prompt(job, "", 5, ())


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
    assert job == ReviewJob(commits=("c3", "c2"), round=1, batch=batch, reviewed=("2", "3"))


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
    assert job == ReviewJob(commits=("c2",), round=1, batch=batch, reviewed=("2",))


def busy() -> ReviewPlanner:
    """A planner part way through a session: paused, one batch closed, one waiting for a task
    and one still filling, having remembered one suggestion."""
    reviews = planner(rounds=1, cap=2)
    done = filled(reviews, "2")
    reviews.landed(state("2", origin="review", batch=done), "c2")
    waiting = filled(reviews, "3", "4")
    reviews.ended(state("3", status="gave_up", origin="review", batch=waiting))
    reviews.open_batch(ReviewJob(commits=("c5",), round=0, batch=None))
    reviews.added("batch-3", "7", "Tidy imports")
    reviews.removed(state("7", status="pending", origin="review", batch="batch-3"))
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
    )


def test_a_snapshot_survives_json_and_restores_the_same_state() -> None:
    reviews = busy()
    assert reviews.snapshot().suggested == frozenset({"tidy imports"})
    assert restored(reviews.snapshot()).snapshot() == reviews.snapshot()


def test_a_restored_planner_remembers_the_suggestions_made_before() -> None:
    again = restored(busy().snapshot())
    refusal = again.refusal("batch-3", "TIDY imports", ())
    assert refusal == "An earlier review already suggested that."


def test_a_snapshot_saved_before_suggestions_were_remembered_still_loads() -> None:
    saved = {"paused": False, "next_batch": 2, "batches": {}}
    assert PlannerSnapshot.model_validate(saved).suggested == frozenset()


def test_a_batch_and_its_snapshot_have_the_same_fields() -> None:
    assert [f.name for f in fields(_Batch)] == list(BatchSnapshot.model_fields)


def test_a_batch_thawed_from_a_snapshot_freezes_back_to_it() -> None:
    snapshot = BatchSnapshot(
        round=2,
        cap=4,
        tasks=("5", "6"),
        finished=frozenset({"5"}),
        commits=("c5",),
        missing=("6",),
        landed=("5",),
        filling=False,
    )
    assert _Batch.thawed(snapshot).frozen() == snapshot


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
    batch = reviews.open_batch(ReviewJob(commits=("a",), round=0, batch=None))
    assert reviews.refusal(batch, "Tidy imports", ()) is None


def test_the_super_review_count_is_kept_apart_from_snapshots() -> None:
    reviews = planner()
    reviews.counts = {SUPER_REVIEW: 1}
    snapshot = reviews.snapshot()
    reviews.super_count = SuperReviewCount(2, "abc", 3, {"a.py": 1})
    reviews.restore(snapshot)
    assert reviews.super_count == SuperReviewCount(2, "abc", 3, {"a.py": 1})


def test_a_commit_review_is_from_review() -> None:
    job = ReviewJob(commits=("abc",), round=0, batch=None)
    assert job.origin == REVIEW_ORIGIN


def test_a_super_review_is_from_super_review() -> None:
    job = ReviewJob(commits=("abc",), round=0, batch=None, whole=True)
    assert job.origin == SUPER_REVIEW


def skilled(task_id: str, *skills: str) -> TaskState:
    """Task ``task_id``, which landed a commit, naming ``skills``."""
    return TaskState(id=task_id, task=TaskSpec(prompt="x", skills=skills), status="completed")


def test_a_review_names_the_skills_of_the_task_it_reviews() -> None:
    job = ReviewJob(commits=("c1",), round=0, batch=None, reviewed=("1",))
    tasks = [skilled("1", "python", "application")]
    assert review_skills(job, tasks) == ("python", "application")


def test_a_batch_review_names_its_tasks_skills_in_task_order_each_once() -> None:
    job = ReviewJob(commits=("c2", "c3"), round=1, batch="batch-1", reviewed=("2", "3"))
    tasks = [skilled("1", "rust"), skilled("2", "python"), skilled("3", "cpp", "python")]
    assert review_skills(job, tasks) == ("python", "cpp")


def test_a_reviewed_task_no_longer_on_the_board_adds_no_skills_to_its_review() -> None:
    job = ReviewJob(commits=("c2", "c3"), round=1, batch="batch-1", reviewed=("2", "3"))
    assert review_skills(job, [skilled("3", "cpp")]) == ("cpp",)


def test_a_review_whose_reviewed_tasks_are_all_gone_names_no_skills() -> None:
    job = ReviewJob(commits=("c1",), round=0, batch=None, reviewed=("1",))
    assert review_skills(job, [skilled("2", "python")]) == ()


def test_a_super_review_names_no_skills() -> None:
    job = ReviewJob(commits=("c1",), round=0, batch=None, whole=True)
    assert review_skills(job, [skilled("1", "python")]) == ()


def test_the_pause_notice_names_the_rounds_limit() -> None:
    assert pause_notice(3) == (
        "Review paused after 3 round(s) of review-suggested work;"
        " it resumes when one of your tasks lands."
    )


def reviewing_while(fires: list[bool]) -> ReviewPlanner:
    """A planner pausing after round 1, whose reviews of commits can be queued while the last of
    ``fires`` says a trigger guarded by ``review-rounds`` can fire."""
    return ReviewPlanner(ReviewLimits(rounds=1), lambda: fires[-1])


def test_while_no_review_trigger_can_fire_no_commit_is_reviewed() -> None:
    reviews = reviewing_while([False])
    assert reviews.landed(state("1"), "abc123") is None


def test_while_no_review_trigger_can_fire_a_finished_batch_closes_unreviewed_and_unpaused() -> None:
    reviews = reviewing_while([False])
    batch = filled(reviews, "2")
    assert reviews.landed(state("2", origin="review", batch=batch), "c2") is None
    assert reviews.open_batches == ()
    assert not reviews.paused


def test_commits_are_reviewed_again_once_a_review_trigger_can_fire_again() -> None:
    fires = [False]
    reviews = reviewing_while(fires)
    assert reviews.landed(state("4"), "abc123") is None
    fires.append(True)
    assert reviews.landed(state("5"), "def456") is not None


# Which lenses raised a batch's findings, and how long findings are kept

COMMIT_JOB = ReviewJob(commits=("c1",), round=0, batch=None, reviewed=("1",))


def finding(finding_id: str, role: str, task: str | None) -> Finding:
    """Finding ``finding_id``, which ``role`` raised, filed in ``task``, if any."""
    outcome: FindingOutcome = "dismissed" if task is None else "filed"
    return Finding(finding_id, role, "Title", "Body", "normal", outcome=outcome, task=task)


RAISED = ReviewGroup(
    COMMIT_JOB,
    findings=(
        finding("F1", "fit-reviewer", "5"),
        finding("F2", "bugs-reviewer", "5"),
        finding("F3", "logic-reviewer", None),
        finding("F4", "tests-reviewer", "6"),
    ),
)
"""A commit's review whose fit and bugs lenses' findings were filed into task 5, the tests
lens's into task 6, and whose logic lens's finding was dismissed."""


def batch_job(reviewed: tuple[str, ...], missing: tuple[str, ...] = ()) -> ReviewJob:
    """The review of batch-1, whose tasks ``reviewed`` landed and ``missing`` didn't."""
    return ReviewJob(("c2",), 1, "batch-1", missing, reviewed=reviewed)


def fixing(task_id: str, fixes: str) -> TaskState:
    """Task ``task_id``, a fix of task ``fixes``."""
    return state(task_id, batch="batch-1").model_copy(update={"fixes": fixes})


def test_the_roles_raising_a_batchs_findings_are_those_filed_or_merged_into_its_tasks() -> None:
    assert raised_roles(batch_job(("5",)), [RAISED], []) == {"fit-reviewer", "bugs-reviewer"}


def test_a_batch_task_that_landed_nothing_still_counts_its_findings() -> None:
    assert raised_roles(batch_job((), missing=("6",)), [RAISED], []) == {"tests-reviewer"}


def test_a_fix_in_the_batch_takes_the_findings_of_the_task_it_fixes_up_the_chain() -> None:
    tasks = [fixing("7", "6"), fixing("8", "7")]
    assert raised_roles(batch_job(("8",)), [RAISED], tasks) == {"tests-reviewer"}


def test_a_dismissed_finding_raises_nothing_in_any_batch() -> None:
    assert raised_roles(batch_job(("5", "6")), [RAISED], []) == {
        "fit-reviewer",
        "bugs-reviewer",
        "tests-reviewer",
    }


def test_a_commit_review_has_no_roles_raising_its_findings() -> None:
    assert raised_roles(COMMIT_JOB, [RAISED], []) == frozenset()


def test_a_group_is_kept_while_a_task_it_filed_a_finding_into_is_in_an_open_batch() -> None:
    groups = {"batch-1": RAISED}
    assert kept_groups(groups, ("batch-4",), {"5": "batch-4"}) == groups
    assert kept_groups(groups, ("batch-4",), {"5": "batch-1", "6": None}) == {}


def test_the_groups_kept_are_those_whose_batch_is_still_open() -> None:
    groups = {"batch-1": RAISED, "batch-2": ReviewGroup(COMMIT_JOB)}
    assert kept_groups(groups, ("batch-2", "batch-3"), {}) == {"batch-2": ReviewGroup(COMMIT_JOB)}


FIX_JOB = ReviewJob(commits=("c2",), round=1, batch="batch-3", reviewed=("5",))


@pytest.mark.parametrize(
    ("runnable", "live", "held", "kept"),
    [
        ((), (), (), False),  # Its batch closed and nothing refers to it.
        ((), ("batch-2",), (), True),  # Booked into a batch still open or kept.
        ((), (), ("batch-3",), True),  # Reviews a held fix's batch.
        (("6",), (), (), True),  # It could run, so its booking keeps it from running again.
    ],
)
def test_a_booking_is_kept_while_its_batch_lives_a_hold_needs_it_or_its_task_could_run(
    runnable: tuple[str, ...], live: tuple[str, ...], held: tuple[str, ...], kept: bool
) -> None:
    bookings = kept_bookings({"6": "batch-2"}, {"6": FIX_JOB}, runnable, live, held)
    assert bookings == (frozenset({"6"}) if kept else frozenset())


def test_the_tasks_that_could_run_are_those_not_done_for_good() -> None:
    statuses: tuple[TaskStatus, ...] = (
        "completed",
        "gave_up",
        "unverified",
        "cancelled",
        "error",
        "pending",
        "running",
        "verifying",
        "draft",
    )
    tasks = [state(str(i), status) for i, status in enumerate(statuses)]
    assert runnable_tasks(tasks) == {"3", "4", "5", "6", "7", "8"}
