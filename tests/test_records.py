from orbit.agent.domain.records import (
    clashing_records,
    moved_records,
    named_records,
    planner_note,
    record_directories,
    renumbered,
)

CREATE = "Create `docs/00024-feature-metrics.md` with exactly the text below."
DOCS = ("00023-bugfix-pause.md", "00024-bugfix-review-loop.md", "protocol.md")


def test_the_directories_a_prompt_names_records_in() -> None:
    prompt = "Plan: docs/00003-feature-a.md\nSee notes/00001-bugfix-b.md and 00002-refactor-c.md."
    assert record_directories(prompt) == ("docs", "notes", "")


def test_only_five_digit_numbered_markdown_files_are_records() -> None:
    prompt = "See docs/protocol.md, docs/0001-a.md, docs/000012-b.md and docs/00012-c.txt."
    assert record_directories(prompt) == ()


def test_a_path_inside_an_absolute_or_longer_path_is_not_a_record_of_its_own() -> None:
    assert record_directories("At /home/me/docs/00001-feature-a.md and x.00002-b.md") == ()


def test_a_record_whose_number_another_has_moves_one_above_the_highest() -> None:
    moves = moved_records(CREATE, {"docs": DOCS})
    assert moves == {"docs/00024-feature-metrics.md": "docs/00025-feature-metrics.md"}


def test_a_record_already_written_stays() -> None:
    listing = (*DOCS, "00024-feature-metrics.md")
    assert moved_records(CREATE, {"docs": listing}) == {}


def test_a_record_whose_number_is_free_stays_even_below_the_highest() -> None:
    prompt = "Plan: docs/00022-feature-spans.md"
    assert moved_records(prompt, {"docs": DOCS}) == {}


def test_a_directory_with_nothing_listed_moves_nothing() -> None:
    assert moved_records(CREATE, {}) == {}


def test_records_moved_in_one_directory_take_successive_numbers() -> None:
    prompt = f"{CREATE}\nThen docs/00023-bugfix-limits.md, and again docs/00024-feature-metrics.md."
    assert moved_records(prompt, {"docs": DOCS}) == {
        "docs/00024-feature-metrics.md": "docs/00025-feature-metrics.md",
        "docs/00023-bugfix-limits.md": "docs/00026-bugfix-limits.md",
    }


def test_records_at_the_top_level_move_among_the_top_level_files() -> None:
    prompt = "Create 00001-feature-a.md"
    assert moved_records(prompt, {"": ("00001-feature-b.md",)}) == {
        "00001-feature-a.md": "00002-feature-a.md"
    }


def test_renumbered_replaces_every_mention_of_each_moved_record() -> None:
    moves = {"docs/00024-feature-metrics.md": "docs/00025-feature-metrics.md"}
    text = f"{CREATE}\nPlan: docs/00024-feature-metrics.md\nAlso docs/00024-bugfix-review-loop.md"
    assert renumbered(text, moves) == (
        "Create `docs/00025-feature-metrics.md` with exactly the text below.\n"
        "Plan: docs/00025-feature-metrics.md\nAlso docs/00024-bugfix-review-loop.md"
    )


def test_renumbered_leaves_a_longer_path_that_ends_the_same() -> None:
    moves = {"docs/00024-feature-metrics.md": "docs/00025-feature-metrics.md"}
    text = "old/docs/00024-feature-metrics.md and mydocs/00024-feature-metrics.md"
    assert renumbered(text, moves) == text


def test_a_record_already_written_under_another_number_is_named_by_that_one() -> None:
    listing = (*DOCS, "00025-feature-metrics.md")
    assert moved_records(CREATE, {"docs": listing}) == {
        "docs/00024-feature-metrics.md": "docs/00025-feature-metrics.md"
    }


def test_the_planner_is_told_to_name_a_moved_record_by_its_new_path() -> None:
    assert planner_note("docs/00024-a.md", "docs/00025-a.md") == (
        "Another record took the number of `docs/00024-a.md`, so orbit renamed it "
        "`docs/00025-a.md` in the tasks still to come. Name it `docs/00025-a.md` from now on."
    )


STALE = "Plan: docs/00024-feature-metrics.md"


def test_the_records_tasks_name_in_the_order_named() -> None:
    texts = ["Plan: docs/00025-feature-a.md and 00001-b.md", "None here", "docs/00026-feature-c.md"]
    assert named_records(texts) == (
        "docs/00025-feature-a.md",
        "00001-b.md",
        "docs/00026-feature-c.md",
    )


def test_a_record_not_yet_written_is_named_as_another_task_names_it_since_it_moved() -> None:
    named = ("docs/00024-feature-metrics.md", "docs/00030-feature-metrics.md")
    assert moved_records(STALE, {"docs": DOCS}, named) == {
        "docs/00024-feature-metrics.md": "docs/00030-feature-metrics.md"
    }


def test_the_last_task_naming_the_record_elsewhere_wins() -> None:
    named = ("docs/00026-feature-metrics.md", "docs/00027-feature-metrics.md")
    assert (
        moved_records(STALE, {"docs": DOCS}, named)["docs/00024-feature-metrics.md"]
        == "docs/00027-feature-metrics.md"
    )


def test_a_name_whose_number_another_record_has_too_is_passed_over() -> None:
    named = ("docs/00023-feature-metrics.md", "docs/00024-feature-metrics.md")
    assert moved_records(STALE, {"docs": DOCS}, named) == {
        "docs/00024-feature-metrics.md": "docs/00025-feature-metrics.md"
    }


def test_another_record_or_directory_named_by_tasks_is_passed_over() -> None:
    named = ("docs/00030-feature-other.md", "notes/00030-feature-metrics.md")
    assert moved_records(STALE, {"docs": DOCS}, named) == {
        "docs/00024-feature-metrics.md": "docs/00025-feature-metrics.md"
    }


def test_a_record_written_under_another_number_wins_over_what_tasks_name() -> None:
    listing = (*DOCS, "00025-feature-metrics.md")
    named = ("docs/00030-feature-metrics.md",)
    assert moved_records(STALE, {"docs": listing}, named) == {
        "docs/00024-feature-metrics.md": "docs/00025-feature-metrics.md"
    }


LOGGED = {"docs/00024-feature-metrics.md": "docs/00031-feature-metrics.md"}


def test_a_record_not_yet_written_follows_the_move_kept_for_it() -> None:
    assert moved_records(STALE, {"docs": DOCS}, logged=LOGGED) == LOGGED


def test_a_record_follows_each_move_kept_after_the_first() -> None:
    logged = LOGGED | {"docs/00031-feature-metrics.md": "docs/00032-feature-metrics.md"}
    assert moved_records(STALE, {"docs": DOCS}, logged=logged) == {
        "docs/00024-feature-metrics.md": "docs/00032-feature-metrics.md"
    }


def test_moves_kept_in_a_loop_stop_before_coming_back_around() -> None:
    logged = LOGGED | {"docs/00031-feature-metrics.md": "docs/00024-feature-metrics.md"}
    assert moved_records(STALE, {"docs": DOCS}, logged=logged) == LOGGED


def test_a_move_kept_to_a_number_another_record_has_is_passed_over() -> None:
    logged = {"docs/00024-feature-metrics.md": "docs/00023-feature-metrics.md"}
    assert moved_records(STALE, {"docs": DOCS}, logged=logged) == {
        "docs/00024-feature-metrics.md": "docs/00025-feature-metrics.md"
    }


def test_a_move_kept_wins_over_what_tasks_name_and_a_written_record_over_both() -> None:
    named = ("docs/00030-feature-metrics.md",)
    assert moved_records(STALE, {"docs": DOCS}, named, LOGGED) == LOGGED
    listing = (*DOCS, "00025-feature-metrics.md")
    assert moved_records(STALE, {"docs": listing}, named, LOGGED) == {
        "docs/00024-feature-metrics.md": "docs/00025-feature-metrics.md"
    }


def test_a_record_clashes_only_while_unwritten_under_a_number_another_has() -> None:
    assert clashing_records(CREATE, {"docs": DOCS})
    assert not clashing_records(CREATE, {"docs": (*DOCS, "00024-feature-metrics.md")})
    assert not clashing_records("Plan: docs/00022-feature-spans.md", {"docs": DOCS})
    assert not clashing_records(CREATE, {})
