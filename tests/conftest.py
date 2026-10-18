"""Settings for every test."""

import os

import pytest

# The tab strip's underline slides for 0.3 s on each activation, and the pilot waits for it;
# tests check where it lands, not the slide. Set before Textual is imported, which reads it then.
os.environ.setdefault("TEXTUAL_ANIMATIONS", "none")


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """Twice as many workers as CPUs for ``-n auto``, unless
    ``PYTEST_XDIST_AUTO_NUM_WORKERS`` sets the count; ``-n logical`` keeps xdist's count."""
    # Textual's pilot sleeps at least 20 ms after every key and pause until the app goes
    # idle, so a Textual test spends about half its time waiting; a second worker per CPU
    # computes meanwhile.
    if config.option.numprocesses == "logical":
        return None
    if os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
        return None
    return 2 * (os.process_cpu_count() or 1)
