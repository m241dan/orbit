"""The runtimes agents run on, by name, and the launchers a workspace's roles need: one for each
runtime an enabled role runs on, once its executable is found on PATH."""

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import typer

from orbit.agent import (
    CLAUDE,
    KIRO,
    AgentLauncher,
    ClaudeCliRunner,
    KiroCliRunner,
    Role,
    WorkspaceUnavailable,
    known_runtimes,
    knows_runtime,
    not_on_path,
    runtime_roles,
)
from orbit.telemetry import AgentCollector

_NO_COLLECTOR = AgentCollector()


@dataclass(frozen=True)
class Runtime:
    """A runtime agents run on: the ``executable`` it needs on PATH, what to do to ``install``
    it, and what makes its ``launcher``, whose agents send their own telemetry to a collector."""

    executable: str
    install: str
    launcher: Callable[[AgentCollector], AgentLauncher]


def _claude(collector: AgentCollector) -> AgentLauncher:
    """Claude Code's launcher, whose agents send their own telemetry to ``collector``."""
    return ClaudeCliRunner(endpoint=collector.endpoint, protocol=collector.protocol)


def _kiro(collector: AgentCollector) -> AgentLauncher:
    """Kiro's launcher, having swept away the agent configs crashed runs left behind; Kiro's
    agents send no telemetry of their own to ``collector``."""
    runner = KiroCliRunner()
    runner.sweep(datetime.now(UTC))
    return runner


RUNTIMES: dict[str, Runtime] = {
    CLAUDE: Runtime("claude", "Install Claude Code", _claude),
    KIRO: Runtime("kiro-cli", "Install the Kiro CLI and log in", _kiro),
}
"""The runtimes this version of orbit knows, by name."""


def build_runtimes(
    roles: Mapping[str, Role], collector: AgentCollector = _NO_COLLECTOR
) -> dict[str, AgentLauncher]:
    """The launchers of the runtimes the enabled ``roles`` run on, by name, in order, whose
    agents send their own telemetry to ``collector``: a runtime no enabled role runs on is
    neither looked for nor built.

    Raises :class:`WorkspaceUnavailable`, naming the roles, the runtime and what to install, if
    a runtime's executable isn't on PATH.
    """
    used = runtime_roles(roles)
    for name, users in used.items():
        _found(name, users)
    return {name: RUNTIMES[name].launcher(collector) for name in used}


def _found(name: str, users: tuple[str, ...]) -> None:
    """Raise :class:`WorkspaceUnavailable` unless the executable of the runtime ``name``, which
    the roles ``users`` run on, is on PATH."""
    runtime = RUNTIMES[name]
    if shutil.which(runtime.executable) is None:
        raise WorkspaceUnavailable(not_on_path(name, users, runtime.executable, runtime.install))


def given_runtime(runtime: str | None) -> str | None:
    """The runtime ``--runtime`` gives, ``runtime``, if any.

    Exits with status 2 if this version of orbit doesn't know it, naming those it does.
    """
    if runtime is not None and not knows_runtime(runtime):
        typer.echo(f"--runtime {runtime}: {known_runtimes()}", err=True)
        raise typer.Exit(2)
    return runtime
