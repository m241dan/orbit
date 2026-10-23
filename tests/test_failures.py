"""Calls to the store whose failures are told apart from other errors."""

import logging

import pytest

from orbit.agent.application.failures import ReportedOnce, outcome, unexpected
from orbit.agent.application.ports import ReadFailed, StoreFailed
from orbit.protocol import SAVE_FAILED, ErrorOccurred, Event
from tests.agent_fakes import FakeStore


class Buggy(FakeStore):
    """A store whose saves hit a bug rather than a storage problem."""

    async def save_review_count(self, count: int) -> None:
        raise RuntimeError("a bug")


class Unlistable(FakeStore):
    """A store whose review count can't be read."""

    async def review_count(self) -> int:
        raise ReadFailed("Couldn't read the review count")


async def test_a_store_failure_is_the_outcome() -> None:
    store = FakeStore()
    store.count_failure = "disk full"

    result = await outcome(store.save_review_count(1))

    assert isinstance(result, StoreFailed) and str(result) == "disk full"


async def test_a_read_failure_is_the_outcome() -> None:
    result = await outcome(Unlistable().review_count())

    assert isinstance(result, ReadFailed)


async def test_an_error_that_isnt_the_stores_escapes_the_outcome() -> None:
    with pytest.raises(RuntimeError, match="a bug"):
        await outcome(Buggy().save_review_count(1))


async def test_an_error_that_isnt_the_stores_escapes_unreported() -> None:
    published: list[Event] = []

    async def publish(event: Event) -> None:
        published.append(event)

    with pytest.raises(RuntimeError, match="a bug"):
        await ReportedOnce(publish).attempt(Buggy().save_review_count(1))
    assert published == []


async def test_a_store_failure_is_reported_as_a_failed_save() -> None:
    published: list[Event] = []

    async def publish(event: Event) -> None:
        published.append(event)

    assert await ReportedOnce(publish).value(Unlistable().review_count()) is None
    assert published == [ErrorOccurred(code=SAVE_FAILED, message="Couldn't read the review count")]


def raised() -> ValueError:
    """An error with a traceback, as one caught is."""
    try:
        raise ValueError("a bug")
    except ValueError as exc:
        return exc


def test_an_unexpected_error_returns_the_message() -> None:
    assert unexpected("Orbit failed to handle X: a bug", raised()) == (
        "Orbit failed to handle X: a bug"
    )


def test_an_unexpected_error_is_logged_with_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exc = raised()
    with caplog.at_level(logging.ERROR):
        unexpected("Orbit failed to handle X: 100% a bug", exc)

    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Orbit failed to handle X: 100% a bug"
    assert record.exc_info is not None and record.exc_info[1] is exc
    assert "Traceback" in caplog.text
