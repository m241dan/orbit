"""Where a running ``orbit serve`` says it is: ``run/<workspace id>.json`` in the state
directory, holding its URL and process id, readable by its owner alone, while it listens."""

import json
from pathlib import Path

from orbit.agent.domain.server import RunningServer, parse_running
from orbit.agent.external.private_files import write_private
from orbit.agent.external.processes import process_alive
from orbit.agent.external.sqlite_store import find_workspace_id

RUN = "run"


class RunFile:
    """The run file of the workspace ``workspace_id`` in the ``state`` directory."""

    def __init__(self, state: Path, workspace_id: str) -> None:
        self.path = state / RUN / f"{workspace_id}.json"

    def write(self, url: str, pid: int) -> None:
        """Say the server at ``url``, in process ``pid``, is listening; raises ``OSError`` when
        it can't."""
        write_private(self.path, json.dumps({"url": url, "pid": pid}) + "\n")

    def read(self) -> RunningServer | None:
        """The server the file names; ``None`` if there's no file or it can't be read."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return parse_running(text)

    def remove(self) -> None:
        """Say the server is gone, if the file is there."""
        self.path.unlink(missing_ok=True)


def running_server(state: Path, workspace: str) -> RunningServer | None:
    """The ``orbit serve`` running for the workspace at ``workspace``, as its run file in the
    ``state`` directory says; ``None`` if there's no file, or the process it names is gone."""
    workspace_id = find_workspace_id(state, workspace)
    if workspace_id is None:
        return None
    server = RunFile(state, workspace_id).read()
    if server is None or not process_alive(server.pid):
        return None
    return server
