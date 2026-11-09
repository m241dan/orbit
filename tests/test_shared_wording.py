"""The wording the backend and the frontends share: words joined as `a, b and c`, and what
releasing merge holds leaves undone."""

import pytest

from orbit.agent.domain.merge_holds import word_released
from orbit.tui.view.confirm import release_message
from orbit.wording import join_in_words, word_undone, word_unfixed, word_unpassed


@pytest.mark.parametrize(
    ("words", "joined"),
    [
        ((), ""),
        (("a",), "a"),
        (("a", "b"), "a and b"),
        (("a", "b", "c"), "a, b and c"),
        (("a", "b", "c", "d"), "a, b, c and d"),
    ],
)
def test_join_in_words_joins_the_last_with_and_and_the_rest_with_commas(
    words: tuple[str, ...], joined: str
) -> None:
    assert join_in_words(words) == joined


@pytest.mark.parametrize(
    ("sets", "worded"),
    [
        (("stress",), "stress hasn't passed"),
        (("stress", "soak"), "stress and soak haven't passed"),
        (("stress", "soak", "fuzz"), "stress, soak and fuzz haven't passed"),
    ],
)
def test_word_unpassed_agrees_with_one_set_or_several(sets: tuple[str, ...], worded: str) -> None:
    assert word_unpassed(sets) == worded


@pytest.mark.parametrize(
    ("lenses", "worded"),
    [
        (("bugs-reviewer",), "the finding from bugs-reviewer isn't fixed"),
        (("a", "b"), "the findings from a and b aren't fixed"),
        (("a", "b", "c"), "the findings from a, b and c aren't fixed"),
    ],
)
def test_word_unfixed_agrees_with_one_lens_or_several(lenses: tuple[str, ...], worded: str) -> None:
    assert word_unfixed(lenses) == worded


@pytest.mark.parametrize(
    ("sets", "lenses", "worded"),
    [
        (("stress",), (), "stress hasn't passed"),
        (("stress", "soak"), (), "stress and soak haven't passed"),
        ((), ("bugs-reviewer",), "the finding from bugs-reviewer isn't fixed"),
        ((), ("a", "b", "c"), "the findings from a, b and c aren't fixed"),
        (
            ("stress",),
            ("bugs-reviewer",),
            "stress hasn't passed and the finding from bugs-reviewer isn't fixed",
        ),
        (
            ("stress", "soak"),
            ("a", "b", "c"),
            "stress and soak haven't passed and the findings from a, b and c aren't fixed",
        ),
    ],
)
def test_word_undone_joins_the_unpassed_sets_and_the_unfixed_findings_with_and(
    sets: tuple[str, ...], lenses: tuple[str, ...], worded: str
) -> None:
    assert word_undone(sets, lenses) == worded


@pytest.mark.parametrize(
    ("sets", "lenses"),
    [
        (("stress",), ()),
        (("stress", "soak"), ()),
        ((), ("bugs-reviewer",)),
        (("stress", "soak"), ("a", "b", "c")),
    ],
)
def test_the_confirmation_and_the_notice_word_a_release_the_same(
    sets: tuple[str, ...], lenses: tuple[str, ...]
) -> None:
    undone = word_undone(sets, lenses)
    assert release_message(sets, lenses) == f"{undone}."
    assert word_released(sets, lenses) == f"Merges resumed by hand; {undone}."
