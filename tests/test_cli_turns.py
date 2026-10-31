"""What the runners of agent CLIs share: starting a turn's CLI, following what it prints until it
exits, a CLI exiting without finishing the turn crashing it, and the conversation each agent
carries on."""

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orbit.agent.external.cli_turns import (
    CliCommand,
    Conversations,
    Crashed,
    crashed,
    followed,
    started,
)
from orbit.protocol import AssistantMessage, Event, TurnFinished

HINT = "Is the CLI installed?"
END = "end"
"""What the CLIs below print to finish the turn."""
READY = "import time; print('ready', flush=True); time.sleep(60)"
"""A CLI that says it's ready, then runs until stopped."""


def python(tmp_path: Path, code: str) -> CliCommand:
    """A CLI running the Python ``code`` in ``tmp_path``."""
    return CliCommand((sys.executable, "-c", code), dict(os.environ), str(tmp_path), HINT)


async def said(stdout: asyncio.StreamReader) -> AsyncIterator[Event]:
    """What a CLI prints on ``stdout``: each line a message, but END, which finishes the turn."""
    async for raw in stdout:
        text = raw.decode().strip()
        if text == END:
            yield TurnFinished(turn_id="t", reason="completed")
        else:
            yield AssistantMessage(turn_id="t", text=text)


def crash(status: int, stderr: str) -> Crashed:
    """The crash of a CLI named ``cli``."""
    return crashed("cli", status, stderr)


async def turn(command: CliCommand) -> list[Event]:
    """The events of a turn of ``command``'s CLI, run as the runners run theirs."""
    async with contextlib.AsyncExitStack() as stack:
        process = await started(stack, command, asyncio.subprocess.DEVNULL)
        return [event async for event in followed(process, said, crash)]


# Following a turn


async def test_a_cli_finishing_the_turn_gives_its_events_whatever_it_exits_with(
    tmp_path: Path,
) -> None:
    code = f"import sys; print('hi'); print({END!r}); sys.exit(1)"
    assert await turn(python(tmp_path, code)) == [
        AssistantMessage(turn_id="t", text="hi"),
        TurnFinished(turn_id="t", reason="completed"),
    ]


async def test_a_cli_exiting_without_finishing_the_turn_crashes_with_its_stderr(
    tmp_path: Path,
) -> None:
    code = "import sys; print('hi'); sys.stderr.write('Invalid API key\\n'); sys.exit(1)"
    with pytest.raises(Crashed, match=r"^Invalid API key$"):
        await turn(python(tmp_path, code))


async def test_a_cli_exiting_early_without_stderr_crashes_naming_its_status(
    tmp_path: Path,
) -> None:
    with pytest.raises(Crashed, match=r"^cli exited with status 3$"):
        await turn(python(tmp_path, "import sys; sys.exit(3)"))


async def test_a_crash_whose_stderr_is_not_utf8_reports_it_with_replacements(
    tmp_path: Path,
) -> None:
    code = "import sys; sys.stderr.buffer.write(b'Bad \\xff key\\n'); sys.exit(1)"
    with pytest.raises(Crashed, match=r"^Bad \ufffd key$"):
        await turn(python(tmp_path, code))


async def test_a_crash_is_what_the_runner_makes_of_its_status_and_stderr(tmp_path: Path) -> None:
    def unreachable(status: int, stderr: str) -> RuntimeError:
        return RuntimeError(f"status {status}, stderr {stderr!r}")

    code = "import sys; sys.stderr.write('  gone  \\n'); sys.exit(4)"
    async with contextlib.AsyncExitStack() as stack:
        process = await started(stack, python(tmp_path, code), asyncio.subprocess.DEVNULL)
        with pytest.raises(RuntimeError, match=r"^status 4, stderr 'gone'$"):
            [event async for event in followed(process, said, unreachable)]


# Starting a turn's CLI


async def test_a_missing_cli_fails_to_start_with_the_runners_hint(tmp_path: Path) -> None:
    missing = str(tmp_path / "no-such-cli")
    with pytest.raises(RuntimeError) as raised:
        await turn(CliCommand((missing,), {}, str(tmp_path), HINT))
    assert not isinstance(raised.value, Crashed)
    assert str(raised.value) == f"Could not start {missing!r}: no such program. {HINT}"


async def test_a_cli_with_no_directory_to_run_in_fails_to_start_saying_so(tmp_path: Path) -> None:
    gone = tmp_path / "gone"
    with pytest.raises(RuntimeError) as raised:
        await turn(python(gone, "pass"))
    assert str(raised.value) == (
        f"Could not start {sys.executable!r}: there's no directory {gone} to run it in."
    )


async def test_a_cli_started_with_its_stdin_piped_reads_what_its_sent(tmp_path: Path) -> None:
    code = f"import sys; print(sys.stdin.read()); print({END!r})"
    async with contextlib.AsyncExitStack() as stack:
        process = await started(stack, python(tmp_path, code), asyncio.subprocess.PIPE)
        assert process.stdin
        process.stdin.write(b"hello")
        process.stdin.close()
        events = [event async for event in followed(process, said, crash)]
    assert events[0] == AssistantMessage(turn_id="t", text="hello")


# Cancelling a turn


class Running:
    """A turn of the CLI ``command`` runs, noting its process and when it printed a line."""

    def __init__(self, command: CliCommand) -> None:
        self.command = command
        self.process: asyncio.subprocess.Process | None = None
        self.printed = asyncio.Event()

    async def run(self) -> None:
        """Run the turn until the CLI exits or the turn is cancelled."""
        async with contextlib.AsyncExitStack() as stack:
            self.process = await started(stack, self.command, asyncio.subprocess.DEVNULL)
            async for _ in followed(self.process, self._said, crash):
                pass

    async def _said(self, stdout: asyncio.StreamReader) -> AsyncIterator[Event]:
        """What the CLI prints on ``stdout``, as :func:`said` reads it, noting that it did."""
        async for event in said(stdout):
            self.printed.set()
            yield event


async def test_cancelling_a_turn_stops_its_cli_and_its_stderr_reader(tmp_path: Path) -> None:
    running = Running(python(tmp_path, READY))
    before = asyncio.all_tasks()
    task = asyncio.create_task(running.run())
    await asyncio.wait_for(running.printed.wait(), timeout=5)
    [stderr] = asyncio.all_tasks() - before - {task}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stderr.cancelled()
    assert running.process and running.process.returncode is not None


# Conversations


def test_each_agent_resumes_the_session_its_own_turns_reported() -> None:
    conversations = Conversations()
    conversations.remember("agent-1", "session-1")
    conversations.remember("agent-2", "session-2")
    assert conversations.conversation("agent-1") == "session-1"
    assert conversations.conversation("agent-2") == "session-2"
    assert conversations.conversation("agent-3") is None


def test_a_later_turns_session_replaces_the_earlier_ones() -> None:
    conversations = Conversations()
    conversations.remember("agent-1", "session-1")
    conversations.remember("agent-1", "session-2")
    assert conversations.conversation("agent-1") == "session-2"


def test_a_turn_reporting_no_session_leaves_the_conversation_as_it_was() -> None:
    conversations = Conversations()
    conversations.set_conversation("agent-1", "saved")
    conversations.remember("agent-1", None)
    assert conversations.conversation("agent-1") == "saved"


def test_a_conversation_set_for_an_agent_is_resumed_and_can_be_dropped() -> None:
    conversations = Conversations()
    conversations.set_conversation("agent-1", "saved")
    assert conversations.conversation("agent-1") == "saved"
    conversations.set_conversation("agent-1", None)
    conversations.set_conversation("agent-1", None)  # Dropping none is fine too.
    assert conversations.conversation("agent-1") is None
