"""Settings for every test."""

import os
from pathlib import Path

import pytest

from orbit.agent.domain.usage import UsageFetch, UsageUnavailable
from orbit.agent.external.directories import CONFIG_DIR_VARIABLE, STATE_DIR_VARIABLE
from orbit.agent.external.usage_api import UsageApi

# The tab strip's underline slides for 0.3 s on each activation, and the pilot waits for it;
# tests check where it lands, not the slide. Set before Textual is imported, which reads it then.
os.environ.setdefault("TEXTUAL_ANIMATIONS", "none")

# Fixtures more than one test module uses.
pytest_plugins = ["tests.cli_fakes", "tests.git_repo", "tests.store_views"]


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


OFFLINE = "offline in tests"
"""Why the usage limits the CLI fetches in tests are unavailable."""


class OfflineUsageApi(UsageApi):
    """The usage API, never fetching: a real fetch would use the login of whoever runs the
    tests, and the network."""

    async def __call__(self) -> UsageFetch:
        return UsageUnavailable(OFFLINE)


@pytest.fixture(autouse=True)
def offline_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Have the CLI fetch its usage limits with :class:`OfflineUsageApi`."""
    monkeypatch.setattr("orbit.cli.backend.UsageApi", OfflineUsageApi)


@pytest.fixture(autouse=True)
def orbit_directories(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point orbit's state and config directories, and the XDG homes they fall back to, into
    ``tmp_path``, so no test reads or writes the real user's. A test can still set or unset
    each variable itself."""
    monkeypatch.setenv(STATE_DIR_VARIABLE, str(tmp_path / "state"))
    monkeypatch.setenv(CONFIG_DIR_VARIABLE, str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


@pytest.fixture
def state(tmp_path: Path) -> Path:
    """The state directory every test has, unless it sets its own."""
    return tmp_path / "state"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """The config directory every test has, unless it sets its own."""
    return tmp_path / "config"


@pytest.fixture
def no_git_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Git configured with no identity beyond a repository's own config."""
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.setenv("EMAIL", "")
    for name in ("AUTHOR", "COMMITTER"):
        for part in ("NAME", "EMAIL"):
            monkeypatch.delenv(f"GIT_{name}_{part}", raising=False)


SLOW_SSH = 'echo $$ > "$ORBIT_TEST_PIDFILE"\nexec sleep 30\n'
"""An SSH that notes its pid, then hangs, as one waiting on a slow server."""


@pytest.fixture
def slow_ssh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Have git's SSH hang; returns the file it notes its pid in."""
    script = tmp_path / "ssh.sh"
    script.write_text(SLOW_SSH)
    pidfile = tmp_path / "ssh.pid"
    monkeypatch.setenv("GIT_SSH_COMMAND", f"sh {script}")
    monkeypatch.setenv("ORBIT_TEST_PIDFILE", str(pidfile))
    return pidfile
