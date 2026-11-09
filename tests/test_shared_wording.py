"""The wording the backend and the frontends share: words joined as `a, b and c`."""

import pytest

from orbit.wording import join_in_words


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
