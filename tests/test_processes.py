import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orbit.agent.external import processes
from orbit.agent.external.processes import process_alive, run_process, spawned
from tests.process_checks import gone


async def test_returns_exit_status_and_combined_output(tmp_path: Path) -> None:
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    returncode, output = await run_process((sys.executable, "-c", code), tmp_path)
    assert returncode == 3
    assert sorted(output.decode().split()) == ["err", "out"]


async def test_stderr_can_be_discarded(tmp_path: Path) -> None:
    code = "import sys; print('out'); print('err', file=sys.stderr)"
    returncode, output = await run_process(
        (sys.executable, "-c", code), tmp_path, stderr=asyncio.subprocess.DEVNULL
    )
    assert returncode == 0
    assert output.decode().split() == ["out"]


async def test_runs_in_cwd_without_stdin(tmp_path: Path) -> None:
    code = "import os, sys; print(os.getcwd()); print(repr(sys.stdin.read()))"
    _, output = await run_process((sys.executable, "-c", code), tmp_path)
    assert output.decode().split() == [str(tmp_path.resolve()), "''"]


async def test_runs_in_the_environment_given(tmp_path: Path) -> None:
    code = "import os; print(os.environ.get('ORBIT_TEST_VAR'))"
    _, output = await run_process(
        (sys.executable, "-c", code), tmp_path, env={**os.environ, "ORBIT_TEST_VAR": "set"}
    )
    assert output.decode().strip() == "set"


async def test_missing_program_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await run_process(("orbit-no-such-command",), tmp_path)


async def test_cancelling_kills_the_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    code = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    task = asyncio.create_task(run_process((sys.executable, "-c", code), tmp_path))
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
    assert task.cancelled()
    with pytest.raises(ProcessLookupError):  # Killed and reaped: the pid is gone.
        os.kill(pid, 0)


async def recorded_line(path: Path) -> str:
    """The line written to ``path``, once it's whole."""
    while not (path.exists() and path.read_text().endswith("\n")):
        await asyncio.sleep(0.01)
    return path.read_text()


async def cancelled_grandchild(tmp_path: Path, script: str) -> int:
    """The pid of the grandchild ``script`` records in ``$PID_FILE``, once ``run_process``
    running it under ``sh`` has been cancelled."""
    pid_file = tmp_path / "pid"
    env = {**os.environ, "PID_FILE": str(pid_file)}
    task = asyncio.create_task(run_process(("sh", "-c", script), tmp_path, env=env))
    await recorded_line(pid_file)
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
    assert task.cancelled()
    return int(pid_file.read_text())


async def test_cancelling_stops_what_the_child_started(tmp_path: Path) -> None:
    grandchild = await cancelled_grandchild(tmp_path, 'sleep 30 & echo $! > "$PID_FILE"; wait')
    assert await gone(grandchild)


async def test_cancelling_stops_what_the_child_started_after_the_child_exits(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "pid"
    env = {**os.environ, "PID_FILE": str(pid_file)}
    # sh exits at once, but the sleep it backgrounds holds its stdout open.
    script = 'sleep 30 & echo $$ $! > "$PID_FILE"'
    task = asyncio.create_task(run_process(("sh", "-c", script), tmp_path, env=env))
    child, grandchild = map(int, (await recorded_line(pid_file)).split())
    assert await gone(child)
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
    assert task.cancelled()
    assert await gone(grandchild)


async def test_a_failing_block_stops_the_group(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        async with spawned(("sleep", "30"), cwd=tmp_path) as process:
            raise ValueError
    assert process.returncode is not None


async def test_a_block_leaving_the_program_running_stops_it(tmp_path: Path) -> None:
    async with spawned(("sleep", "30"), cwd=tmp_path) as process:
        pass
    assert process.returncode is not None


async def test_cancelling_lets_the_child_exit_cleanly_on_sigterm(tmp_path: Path) -> None:
    script = 'trap "echo stopped > stopped; exit 0" TERM; sleep 30 & echo $! > "$PID_FILE"; wait'
    grandchild = await cancelled_grandchild(tmp_path, script)
    assert (tmp_path / "stopped").read_text() == "stopped\n"
    assert await gone(grandchild)


async def test_a_group_ignoring_sigterm_is_killed_after_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processes, "STOP_GRACE_SECONDS", 0.1)
    script = 'trap "" TERM; sleep 30 & echo $! > "$PID_FILE"; wait'
    grandchild = await cancelled_grandchild(tmp_path, script)
    assert await gone(grandchild)


def gated_start(monkeypatch: pytest.MonkeyPatch, gate: asyncio.Event, *, fails: bool) -> None:
    """Make starting a program hold until ``gate`` is set, as a busy loop can, after the
    program is running; then fail with ``FileNotFoundError`` if ``fails``."""
    start = asyncio.create_subprocess_exec

    async def gated(*command: str, **options: Any) -> asyncio.subprocess.Process:
        process = await start(*command, **options)
        await gate.wait()
        if fails:
            raise FileNotFoundError(command[0])
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", gated)


async def test_cancelling_while_starting_stops_what_the_child_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()
    gated_start(monkeypatch, gate, fails=False)
    pid_file = tmp_path / "pid"
    env = {**os.environ, "PID_FILE": str(pid_file)}
    script = 'sleep 30 & echo $! > "$PID_FILE"; wait'
    task = asyncio.create_task(run_process(("sh", "-c", script), tmp_path, env=env))
    grandchild = int(await recorded_line(pid_file))
    task.cancel()
    await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
    assert task.cancelled()
    assert await gone(grandchild)


async def test_cancelling_a_start_that_then_fails_stays_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()
    gated_start(monkeypatch, gate, fails=True)
    task = asyncio.create_task(run_process(("true",), tmp_path))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
    assert task.cancelled()


def test_this_process_is_alive() -> None:
    assert process_alive(os.getpid())


def test_a_process_that_has_exited_is_not_alive() -> None:
    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()
    assert not process_alive(process.pid)


def test_another_users_process_is_not_taken_as_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(pid: int, signal: int) -> None:
        raise PermissionError(pid)

    monkeypatch.setattr(os, "kill", refused)
    assert not process_alive(1)


def test_a_process_id_too_large_for_one_is_not_alive() -> None:
    assert not process_alive(2**64)
