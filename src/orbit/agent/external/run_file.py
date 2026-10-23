"""Where a running ``orbit serve`` says it is: ``run/server.json`` in the state directory,
holding its URL and process id, and over TLS how a frontend on this machine reaches it, readable
by its owner alone, while it listens. There's one per state directory, as one ``orbit serve``
serves every workspace registered there, and the serve lock beside it, ``run/serve.lock``, keeps
it so: an exclusive ``flock`` the serve holds from its start, before it writes the run file, until
its process ends."""

import json
import os
from pathlib import Path

from orbit.agent.domain.server import LocalTls, RunningServer, parse_running
from orbit.agent.external.file_locks import take_flock
from orbit.agent.external.processes import process_alive
from orbit.agent.external.whole_files import write_private

RUN = "run"
SERVER_FILE = "server.json"
LOCK_FILE = "serve.lock"


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
        """Say the server in this process is gone, if the file names it, leaving one another
        serve wrote alone."""
        server = self.read()
        if server is not None and server.pid == os.getpid():
            self.path.unlink(missing_ok=True)


class ServeLock:
    """The serve lock in the ``state`` directory.

    Each stands for one ``orbit serve``: another ``ServeLock`` on the same directory, even in
    the same process, can't take it while this holds it.
    """

    def __init__(self, state: Path) -> None:
        self.path = state / RUN / LOCK_FILE
        self._fd: int | None = None

    def take(self) -> bool:
        """Whether this took the lock; ``False`` when another serve holds it.

        Raises ``OSError`` when the lock file can't be made or opened, or locked.
        """
        fd = take_flock(self.path, "", directory_mode=0o700)
        if fd is None:
            return False
        self._fd = fd
        return True

    def close(self) -> None:
        """Let go of the lock, if this holds it, leaving its file for the next serve."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def running_server(state: Path) -> RunningServer | None:
    """The ``orbit serve`` running on the ``state`` directory, as its run file there says;
    ``None`` if there's no file, or the process it names is gone."""
    server = RunFile(state).read()
    if server is None or not process_alive(server.pid):
        return None
    return server
