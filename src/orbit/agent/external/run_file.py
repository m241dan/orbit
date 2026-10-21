"""Where a running ``orbit serve`` says it is: ``run/server.json`` in the state directory,
holding its URL and process id, and over TLS how a frontend on this machine reaches it, readable
by its owner alone, while it listens. There's one per state directory, as one ``orbit serve``
serves every workspace registered there."""

import json
from pathlib import Path

from orbit.agent.domain.server import LocalTls, RunningServer, parse_running
from orbit.agent.external.private_files import write_private
from orbit.agent.external.processes import process_alive

RUN = "run"
SERVER_FILE = "server.json"


class RunFile:
    """The run file in the ``state`` directory."""

    def __init__(self, state: Path) -> None:
        self.path = state / RUN / SERVER_FILE

    def write(self, url: str, pid: int, tls: LocalTls | None = None) -> None:
        """Say the server at ``url``, in process ``pid``, is listening, reached from this
        machine as ``tls`` says, if given; raises ``OSError`` when it can't."""
        server = RunningServer(url=url, pid=pid, tls=tls)
        write_private(self.path, json.dumps(server.model_dump(exclude_none=True)) + "\n")

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


def running_server(state: Path) -> RunningServer | None:
    """The ``orbit serve`` running on the ``state`` directory, as its run file there says;
    ``None`` if there's no file, or the process it names is gone."""
    server = RunFile(state).read()
    if server is None or not process_alive(server.pid):
        return None
    return server
