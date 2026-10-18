import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orbit.agent.external.processes import process_alive, run_process


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
