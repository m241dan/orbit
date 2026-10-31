"""What orbit's runners of agent CLIs share: the CLI session each agent's turns resume, starting
a turn's CLI, and following what it prints until it exits, a CLI exiting without finishing the
turn having crashed. How each CLI's output reads is its own runner's."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass

from orbit.agent.external.processes import spawned, start_failure
from orbit.protocol import Event, TurnFinished

# Tool results (e.g. whole files) arrive as single JSON lines; allow large ones.
LINE_LIMIT = 64 * 1024 * 1024
# The variables that send headers with OTLP, which may hold the key to orbit's collector; an
# agent, which can run any command, could leak it.
OTLP_HEADER_VARIABLES = (
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
)


class Conversations:
    """The CLI session each agent's turns resume, by the agent's id."""

    def __init__(self) -> None:
        self._conversations: dict[str, str] = {}  # Agent id -> its CLI session's id.

    def conversation(self, agent_id: str) -> str | None:
        """The id of the CLI session agent ``agent_id``'s turns resume."""
        return self._conversations.get(agent_id)

    def set_conversation(self, agent_id: str, handle: str | None) -> None:
        """Have agent ``agent_id``'s next turn resume the CLI session ``handle``, or start a new
        one when it's ``None``."""
        if handle is None:
            self._conversations.pop(agent_id, None)
        else:
            self._conversations[agent_id] = handle

    def remember(self, agent_id: str, session_id: str | None) -> None:
        """Note the CLI session ``session_id`` a turn ran in as agent ``agent_id``'s, if the turn
        reported one."""
        if session_id:
            self._conversations[agent_id] = session_id


@dataclass(frozen=True)
class CliCommand:
    """How a turn's CLI runs: as ``args``, with the environment ``env``, in ``cwd``.
    ``missing_hint`` follows the message saying the program is missing."""

    args: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str
    missing_hint: str


async def started(
    stack: contextlib.AsyncExitStack, command: CliCommand, stdin: int
) -> asyncio.subprocess.Process:
    """``command``'s CLI, running with ``stdin`` (such as ``asyncio.subprocess.PIPE``) as its
    stdin and its stdout and stderr piped, stopped with whatever it started when ``stack``
    closes.

    Raises RuntimeError saying why when it can't start.
    """
    try:
        return await stack.enter_async_context(
            spawned(
                command.args,
                env=command.env,
                cwd=command.cwd,
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=LINE_LIMIT,
            )
        )
    except OSError as error:
        hint = command.missing_hint
        raise RuntimeError(
            start_failure(command.args, command.cwd, error, missing_hint=hint)
        ) from None


async def followed(
    process: asyncio.subprocess.Process,
    events: Callable[[asyncio.StreamReader], AsyncIterator[Event]],
    crash: Callable[[int, str], Exception],
) -> AsyncIterator[Event]:
    """The events ``events`` reads from the CLI ``process``'s stdout, until it exits; it was
    started with its stdout and stderr piped. Its stderr is read meanwhile, and no longer once
    this ends, however it ends.

    Raises ``crash`` of its exit status and its stderr if it exits without an event finishing
    the turn.
    """
    stdout, stderr_pipe = _pipes(process)
    stderr = asyncio.create_task(stderr_pipe.read())
    try:
        finished = False
        async for event in events(stdout):
            finished = finished or isinstance(event, TurnFinished)
            yield event
        # Let the CLI exit on its own once it's done, so it can save its session.
        status = await process.wait()
        if not finished:
            raise crash(status, (await stderr).decode(errors="replace").strip())
    finally:
        stderr.cancel()


def _pipes(
    process: asyncio.subprocess.Process,
) -> tuple[asyncio.StreamReader, asyncio.StreamReader]:
    """The process's stdout and stderr; it was started with both piped."""
    assert process.stdout and process.stderr
    return process.stdout, process.stderr


class Crashed(RuntimeError):
    """The CLI ran but exited without finishing the turn."""


def crashed(program: str, status: int, stderr: str) -> Crashed:
    """The error for the CLI ``program`` exiting with ``status`` and ``stderr`` without finishing
    the turn: its stderr, or its status when it printed none."""
    return Crashed(stderr or f"{program} exited with status {status}")
