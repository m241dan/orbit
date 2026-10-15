import asyncio
import os
import sys
from pathlib import Path

import pytest

from orbit.agent.external.processes import run_process


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
