from orbit.agent.domain.records import moved_records, record_directories, renumbered

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
