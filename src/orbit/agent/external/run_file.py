"""Where a running ``orbit serve`` says it is: ``run/<workspace id>.json`` in the state
directory, holding its URL and process id, readable by its owner alone, while it listens."""

import json
from pathlib import Path

from orbit.agent.external.private_files import write_private

RUN = "run"


class RunFile:
    """The run file of the workspace ``workspace_id`` in the ``state`` directory."""

    def __init__(self, state: Path, workspace_id: str) -> None:
        self.path = state / RUN / f"{workspace_id}.json"

    def write(self, url: str, pid: int) -> None:
        """Say the server at ``url``, in process ``pid``, is listening; raises ``OSError`` when
        it can't."""
        write_private(self.path, json.dumps({"url": url, "pid": pid}) + "\n")

    def remove(self) -> None:
        """Say the server is gone, if the file is there."""
        self.path.unlink(missing_ok=True)
