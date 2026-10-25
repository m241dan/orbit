"""Orbit's and OpenTelemetry's warnings are held back while the TUI runs, shown once per kind."""

import asyncio
import logging
import threading

import pytest

from orbit.agent.application.failures import log_unexpected, unexpected
from orbit.tui.warning_capture import WarningCapture


def test_capturing_takes_orbits_and_opentelemetrys_records_only_for_the_block() -> None:
    capture = WarningCapture()

    with capture.capturing():
        logging.getLogger("orbit.telemetry.otel").warning("dropped %d", 3)
        logging.getLogger("opentelemetry.sdk").error("export failed")
    logging.getLogger("orbit.agent").warning("after")

    assert capture.lines() == ["dropped 3", "export failed"]


def test_records_below_warning_arent_captured() -> None:
    capture = WarningCapture()
    logger = logging.getLogger("orbit.quiet")
    logger.setLevel(logging.INFO)

    with capture.capturing():
        logger.info("fine")
    logger.setLevel(logging.NOTSET)

    assert capture.lines() == []


def test_capturing_keeps_records_from_the_terminal(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without pytest's handlers, an uncaptured warning goes to stderr, as it would in a run.
    monkeypatch.setattr(logging.root, "handlers", [])

    with WarningCapture().capturing():
        logging.getLogger("orbit.agent").warning("held back")

    assert capsys.readouterr().err == ""


async def test_a_warning_from_another_thread_is_shown_once_on_the_attached_loop() -> None:
    capture = WarningCapture()
    shown: list[tuple[str, str, str]] = []
    capture.attach(lambda name, message: shown.append((name, message, thread())))

    def warn_twice() -> None:
        for count in (1, 2):
            logging.getLogger("orbit.telemetry.otel").warning("dropped %d", count)

    with capture.capturing():
        await asyncio.to_thread(warn_twice)
        await asyncio.sleep(0)

    assert shown == [("orbit.telemetry.otel", "dropped 1", "MainThread")]
    assert capture.lines() == ["dropped 1", "dropped 2"]


async def test_kinds_differ_by_logger_and_by_template() -> None:
    capture = WarningCapture()
    shown: list[tuple[str, str]] = []
    capture.attach(lambda name, message: shown.append((name, message)))

    with capture.capturing():
        logging.getLogger("orbit.a").warning("one")
        logging.getLogger("orbit.b").warning("one")
        logging.getLogger("orbit.a").warning("two")
        await asyncio.sleep(0)

    assert shown == [("orbit.a", "one"), ("orbit.b", "one"), ("orbit.a", "two")]


async def test_warnings_captured_before_attaching_are_shown_on_attaching() -> None:
    capture = WarningCapture()
    shown: list[tuple[str, str]] = []

    with capture.capturing():
        logging.getLogger("orbit.a").warning("early")
        logging.getLogger("orbit.a").warning("early")
        capture.attach(lambda name, message: shown.append((name, message)))
        await asyncio.sleep(0)

    assert shown == [("orbit.a", "early")]


async def test_a_detached_capture_shows_nothing_more_but_keeps_capturing() -> None:
    capture = WarningCapture()
    shown: list[tuple[str, str]] = []
    capture.attach(lambda name, message: shown.append((name, message)))
    capture.detach()

    with capture.capturing():
        logging.getLogger("orbit.a").warning("late")
        await asyncio.sleep(0)

    assert shown == []
    assert capture.lines() == ["late"]


def test_a_warning_after_the_attached_loop_closed_is_still_captured() -> None:
    capture = WarningCapture()
    shown: list[tuple[str, str]] = []

    async def attach() -> None:
        capture.attach(lambda name, message: shown.append((name, message)))

    asyncio.run(attach())
    with capture.capturing():
        logging.getLogger("orbit.a").warning("unseen")

    assert shown == []
    assert capture.lines() == ["unseen"]


async def test_an_unexpected_error_is_kept_with_its_traceback_but_not_shown() -> None:
    capture = WarningCapture()
    shown: list[tuple[str, str]] = []
    capture.attach(lambda name, message: shown.append((name, message)))

    with capture.capturing():
        unexpected("Orbit failed to handle X: a bug", raised())
        await asyncio.sleep(0)

    assert shown == []
    [line] = capture.lines()
    assert line.startswith("Orbit failed to handle X: a bug\nTraceback")
    assert line.endswith("ValueError: a bug")


async def test_an_unexpected_error_captured_before_attaching_isnt_shown_on_attaching() -> None:
    capture = WarningCapture()
    shown: list[tuple[str, str]] = []

    with capture.capturing():
        unexpected("Orbit failed to handle X: a bug", raised())
        capture.attach(lambda name, message: shown.append((name, message)))
        await asyncio.sleep(0)

    assert shown == []


async def test_a_warning_after_an_unexpected_error_is_still_shown_once() -> None:
    capture = WarningCapture()
    shown: list[tuple[str, str]] = []
    capture.attach(lambda name, message: shown.append((name, message)))

    with capture.capturing():
        unexpected("Orbit failed to handle X: a bug", raised())
        logging.getLogger("orbit.a").warning("one")
        logging.getLogger("orbit.a").warning("one")
        await asyncio.sleep(0)

    assert shown == [("orbit.a", "one")]
    assert len(capture.lines()) == 3


async def test_an_unexpected_error_only_logged_is_shown_once_and_kept_with_its_traceback() -> None:
    capture = WarningCapture()
    shown: list[tuple[str, str]] = []
    capture.attach(lambda name, message: shown.append((name, message)))

    with capture.capturing():
        log_unexpected("Orbit failed to flag X: a bug", raised())
        log_unexpected("Orbit failed to flag X: a bug", raised())
        await asyncio.sleep(0)

    assert shown == [("orbit.agent.application.failures", "Orbit failed to flag X: a bug")]
    first, second = capture.lines()
    assert first.startswith("Orbit failed to flag X: a bug\nTraceback")
    assert second.endswith("ValueError: a bug")


async def test_unexpected_errors_only_logged_are_each_shown_after_a_reported_one() -> None:
    capture = WarningCapture()
    shown: list[tuple[str, str]] = []
    capture.attach(lambda name, message: shown.append((name, message)))

    with capture.capturing():
        unexpected("Orbit failed to handle X: a bug", raised())
        log_unexpected("Orbit failed to flag X: a bug", raised())
        log_unexpected("Orbit failed to fetch the usage: a bug", raised())
        await asyncio.sleep(0)

    assert [message for _, message in shown] == [
        "Orbit failed to flag X: a bug",
        "Orbit failed to fetch the usage: a bug",
    ]


async def test_another_error_logged_with_its_traceback_is_still_shown() -> None:
    capture = WarningCapture()
    shown: list[tuple[str, str]] = []
    capture.attach(lambda name, message: shown.append((name, message)))

    with capture.capturing():
        logging.getLogger("opentelemetry.sdk").error("Export failed.", exc_info=raised())
        await asyncio.sleep(0)

    assert shown == [("opentelemetry.sdk", "Export failed.")]


def raised() -> ValueError:
    """An error with a traceback, as one caught is."""
    try:
        raise ValueError("a bug")
    except ValueError as exc:
        return exc


def thread() -> str:
    """The name of the thread running."""
    return threading.current_thread().name
