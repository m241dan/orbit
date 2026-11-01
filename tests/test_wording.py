"""The backend's wording: a noun made plural unless its count is one, alone or after the count."""

import pytest

from orbit.agent.domain.wording import counted, noun


@pytest.mark.parametrize(("count", "word"), [(0, "commits"), (1, "commit"), (2, "commits")])
def test_noun_is_singular_only_for_one(count: int, word: str) -> None:
    assert noun(count, "commit") == word


@pytest.mark.parametrize(("count", "word"), [(0, "stashes"), (1, "stash"), (2, "stashes")])
def test_noun_takes_an_irregular_plural(count: int, word: str) -> None:
    assert noun(count, "stash", "stashes") == word


@pytest.mark.parametrize(("count", "text"), [(0, "0 commits"), (1, "1 commit"), (2, "2 commits")])
def test_counted_puts_the_count_before_a_noun_singular_only_for_one(count: int, text: str) -> None:
    assert counted(count, "commit") == text


@pytest.mark.parametrize(("count", "text"), [(0, "0 stashes"), (1, "1 stash"), (2, "2 stashes")])
def test_counted_takes_an_irregular_plural(count: int, text: str) -> None:
    assert counted(count, "stash", "stashes") == text
