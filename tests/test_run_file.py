"""The run file saying where a running ``orbit serve`` is."""

import json
import stat
from pathlib import Path

from orbit.agent.external.run_file import RunFile


def test_the_run_file_holds_the_url_and_process_id_owner_only(tmp_path: Path) -> None:
    run_file = RunFile(tmp_path, "workspace-1")
    run_file.write("ws://127.0.0.1:4567/", 42)
    assert run_file.path == tmp_path / "run" / "workspace-1.json"
    assert json.loads(run_file.path.read_text()) == {"url": "ws://127.0.0.1:4567/", "pid": 42}
    assert stat.S_IMODE(run_file.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(run_file.path.parent.stat().st_mode) == 0o700


def test_removing_the_run_file_leaves_none(tmp_path: Path) -> None:
    run_file = RunFile(tmp_path, "workspace-1")
    run_file.write("ws://127.0.0.1:4567/", 42)
    run_file.remove()
    run_file.remove()
    assert not run_file.path.exists()
