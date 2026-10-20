"""The transport's ``Authorization: Bearer`` header, written and read."""

import pytest

from orbit.protocol import bearer_header, bearer_token


def test_bearer_token_reads_back_the_header_bearer_header_writes() -> None:
    assert bearer_token({k.lower(): v for k, v in bearer_header("abc").items()}) == "abc"


@pytest.mark.parametrize(
    ("header", "token"),
    [
        (None, ""),
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER abc", "abc"),
        ("Bearer  abc ", "abc"),
        ("Basic abc", ""),
        ("Bearer", ""),
        ("", ""),
    ],
)
def test_bearer_token_reads_only_bearer_authorization(header: str | None, token: str) -> None:
    headers = {} if header is None else {"authorization": header}
    assert bearer_token(headers) == token


def test_bearer_token_is_empty_without_headers() -> None:
    assert bearer_token(None) == ""
