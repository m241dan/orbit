"""Warnings logged while the TUI runs show as transcript notes, once per kind."""

import asyncio
import logging

from orbit.tui import OrbitApp
from tests.tui_fakes import FakeSession, entries


def warn_twice() -> None:
    """Log the same telemetry warning twice, as the backend's thread would."""
    for count in (1, 2):
        logging.getLogger("orbit.telemetry.otel").warning("Dropped %d spans.", count)


def warn_differently() -> None:
    """Log the same telemetry warning twice, then another, as the backend's thread would."""
    warn_twice()
    logging.getLogger("opentelemetry.sdk").error("Export failed.")


async def test_a_warning_from_another_thread_shows_once_as_a_note() -> None:
    app = OrbitApp(FakeSession())
    with app.warnings.capturing():
        async with app.run_test() as pilot:
            await asyncio.to_thread(warn_twice)
            await pilot.pause()

            assert entries(app) == [("⚠ Telemetry: Dropped 1 spans.", "verify")]
    assert app.warnings.lines() == ["Dropped 1 spans.", "Dropped 2 spans."]


async def test_a_different_warning_from_another_thread_adds_its_own_note() -> None:
    app = OrbitApp(FakeSession())
    with app.warnings.capturing():
        async with app.run_test() as pilot:
            await asyncio.to_thread(warn_differently)
            await pilot.pause()

            assert entries(app) == [
                ("⚠ Telemetry: Dropped 1 spans.", "verify"),
                ("⚠ Telemetry: Export failed.", "verify"),
            ]
    assert app.warnings.lines() == ["Dropped 1 spans.", "Dropped 2 spans.", "Export failed."]
