"""The transport's ``Authorization: Bearer`` header, written and read, and the loopback and
file-permission rules both sides share."""

import pytest

from orbit.protocol import bearer_header, bearer_token
from orbit.protocol.transport import is_loopback, too_open


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


@pytest.mark.parametrize("host", ["127.0.0.1", "127.1.2.3", "::1", "localhost", "LocalHost"])
def test_localhost_and_loopback_addresses_are_loopback(host: str) -> None:
    assert is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.2", "::", "2001:db8::1", "example.com", ""])
def test_other_hosts_are_not_loopback(host: str) -> None:
    assert not is_loopback(host)


@pytest.mark.parametrize("mode", [0o100600, 0o600, 0o400, 0o700, 0o000])
def test_a_mode_for_the_owner_alone_is_not_too_open(mode: int) -> None:
    assert not too_open(mode)


@pytest.mark.parametrize("mode", [0o100640, 0o604, 0o620, 0o602, 0o610, 0o601, 0o644, 0o777])
def test_a_mode_letting_group_or_others_in_is_too_open(mode: int) -> None:
    assert too_open(mode)
