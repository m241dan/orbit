"""The workspace explorer's rows, marks, which listings it takes and how it completes paths."""

from orbit.protocol import DirectoryEntry, DirectoryListing
from orbit.tui.view.explorer import (
    Completion,
    added_path,
    answers,
    candidates_line,
    completed,
    completion_parent,
    entry_label,
    entry_path,
    explorer_title,
    more_note,
    shown_entries,
)

REPO = DirectoryEntry(name="orbit", is_repository=True, hidden=False)
SERVED = DirectoryEntry(name="served", is_repository=True, workspace="W", hidden=False)
LINK = DirectoryEntry(name="link", is_repository=False, hidden=False, target="../elsewhere")
DOT = DirectoryEntry(name=".cache", is_repository=False, hidden=True)


def listing(path: str, *entries: DirectoryEntry, total: int | None = None) -> DirectoryListing:
    count = len(entries) if total is None else total
    return DirectoryListing(path=path, entries=entries, truncated=total is not None, total=count)


def test_a_plain_directory_shows_its_name() -> None:
    assert entry_label(DirectoryEntry(name="docs", is_repository=False, hidden=False)) == "docs"


def test_a_repository_is_marked() -> None:
    assert entry_label(REPO) == "⎇ orbit"


def test_a_served_workspace_is_marked_served() -> None:
    assert entry_label(SERVED) == "⎇ served (served)"


def test_a_symlink_shows_its_target() -> None:
    assert entry_label(LINK) == "link → ../elsewhere"


def test_hidden_entries_are_left_out_unless_shown() -> None:
    entries = (REPO, DOT, LINK)
    assert shown_entries(entries, hidden=False) == (REPO, LINK)
    assert shown_entries(entries, hidden=True) == entries


def test_an_entrys_path_is_inside_the_listings() -> None:
    assert entry_path(listing("/srv"), REPO) == "/srv/orbit"
    assert entry_path(listing("/"), REPO) == "/orbit"


def test_a_directory_is_added_at_its_path_in_the_listing() -> None:
    assert added_path(listing("/srv"), REPO) == "/srv/orbit"


def test_a_symlink_is_added_at_its_absolute_target() -> None:
    link = DirectoryEntry(name="link", is_repository=False, hidden=False, target="/opt/x/")
    assert added_path(listing("/srv"), link) == "/opt/x"


def test_a_symlink_is_added_at_its_relative_target_from_the_listing() -> None:
    assert added_path(listing("/srv/sub"), LINK) == "/srv/elsewhere"


def test_a_truncated_listing_says_how_many_more_there_are() -> None:
    assert more_note(listing("/srv", REPO, LINK, total=2002)) == (
        "… 2000 more not shown: type the path in the field"
    )


def test_a_whole_listing_has_no_more_note() -> None:
    assert more_note(listing("/srv", REPO)) is None


def test_the_title_is_the_listings_path_or_empty_before_one() -> None:
    assert explorer_title(listing("/srv")) == "/srv"
    assert explorer_title(None) == ""


def test_any_listing_answers_the_starting_directory() -> None:
    assert answers(None, listing("/home/me"))


def test_a_listing_answers_the_path_asked_for_alone() -> None:
    assert answers("/srv/a", listing("/srv/a"))
    assert not answers("/srv/a", listing("/srv"))


def test_the_completion_parent_is_what_comes_before_the_last_slash() -> None:
    assert completion_parent("/src/orb") == "/src"
    assert completion_parent("/src/") == "/src"
    assert completion_parent("/orb") == "/"


def test_text_not_starting_at_the_root_has_no_completion_parent() -> None:
    assert completion_parent("") is None
    assert completion_parent("src/orb") is None
    assert completion_parent("https://github.com/o/r") is None


def test_one_match_completes_with_a_trailing_slash() -> None:
    assert completed("/src/orb", ["orbit", "docs"]) == Completion(text="/src/orbit/")
    assert completed("/orb", ["orbit"]) == Completion(text="/orbit/")


def test_several_matches_complete_their_common_prefix_with_candidates() -> None:
    assert completed("/src/o", ["orbit", "orchard", "docs"]) == Completion(
        text="/src/or", candidates=("orbit", "orchard")
    )


def test_an_empty_last_component_matches_every_name() -> None:
    assert completed("/src/", ["a", "b"]) == Completion(text="/src/", candidates=("a", "b"))


def test_no_match_completes_nothing() -> None:
    assert completed("/src/x", ["orbit"]) is None


def test_the_candidates_line_lists_the_names_apart() -> None:
    assert candidates_line(("orbit", "orchard")) == "orbit  orchard"
