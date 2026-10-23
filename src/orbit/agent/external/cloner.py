"""Cloning the repositories frontends ask a server to serve into its clone directory, with git
never prompting for credentials, and its progress reported as it clones."""

import asyncio
import codecs
import os
import re
import shutil
from collections import deque
from collections.abc import Callable
from pathlib import Path

from orbit.agent.application.ports import CloneFailed
from orbit.agent.domain.clones import clone_failure, clone_name, clone_names, clone_progress
from orbit.agent.external.git import GitUnavailable, git
from orbit.agent.external.processes import spawned
from orbit.protocol import WorkspaceCloning

# How much of what git says, besides its progress, is kept for the message if it fails.
_KEPT_LINES = 20
_CHUNK = 4096


class Cloner:
    """Clones repositories into ``clone_dir``, each in a directory of its own named for its URL,
    as the server's account, with its credential helpers and SSH agent, never prompting."""

    def __init__(self, clone_dir: Path) -> None:
        self.clone_dir = clone_dir

    async def clone(self, url: str, progress: Callable[[WorkspaceCloning], None]) -> str:
        """The path of a new clone of the repository at ``url``, in a directory named for it,
        numbered if the name is taken, telling ``progress`` of git's progress as it clones.
        Raises :class:`CloneFailed` with git's message if it fails, having removed the
        directory; cancelled, git and what it started are stopped and the directory removed
        before the cancellation ends."""
        directory = await asyncio.to_thread(self._claimed, url)
        try:
            await _cloned(url, directory, progress)
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)
            raise
        return str(directory)

    async def remove(self, path: str) -> None:
        """Remove the clone at ``path``, as far as it can, off the event loop."""
        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)

    def _claimed(self, url: str) -> Path:
        """A new, empty directory in the clone directory for the clone of ``url``, named for it
        or, if that's taken, numbered. Raises :class:`CloneFailed` if it can't be made."""
        try:
            self.clone_dir.mkdir(parents=True, exist_ok=True)
            names = clone_names(clone_name(url))
            while True:
                directory = self.clone_dir / next(names)
                try:
                    directory.mkdir()
                except FileExistsError:
                    continue
                return directory
        except OSError as exc:
            message = f"couldn't make a directory in {self.clone_dir}: {exc}"
            raise CloneFailed(f"Couldn't clone {url}: {message}") from None


async def _cloned(url: str, directory: Path, progress: Callable[[WorkspaceCloning], None]) -> None:
    """Clone the repository at ``url`` into ``directory``, which is empty, telling ``progress``
    of git's progress. Raises :class:`CloneFailed` if git fails or can't be started."""
    try:
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": await ssh_command(directory),
        }
        command = ("git", "clone", "--progress", "--", url, str(directory))
        async with spawned(
            command,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        ) as process:
            assert process.stderr is not None
            said = await _GitOutput(url, progress).read(process.stderr)
            status = await process.wait()
    except (OSError, GitUnavailable) as exc:
        raise CloneFailed(f"Couldn't clone {url}: git failed to start: {exc}") from None
    if status != 0:
        raise CloneFailed(clone_failure(url, said))


async def ssh_command(cwd: Path) -> str:
    """The command git run in ``cwd`` runs SSH with: the one configured, in ``GIT_SSH_COMMAND``
    or git's ``core.sshCommand``, else ``ssh``, never asking for a password or passphrase."""
    configured = os.environ.get("GIT_SSH_COMMAND")
    if configured is None:
        found = await git(cwd, "config", "--get", "core.sshCommand", stderr=False)
        configured = found.text if found.ok else "ssh"
    return f"{configured} -o BatchMode=yes"


class _GitOutput:
    """What git says as it clones the repository at ``url``: each change in its progress told
    to ``progress``, and its last other lines kept."""

    def __init__(self, url: str, progress: Callable[[WorkspaceCloning], None]) -> None:
        self._url = url
        self._progress = progress
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._partial = ""
        self._last: WorkspaceCloning | None = None
        self._said: deque[str] = deque(maxlen=_KEPT_LINES)

    async def read(self, stream: asyncio.StreamReader) -> list[str]:
        """Read ``stream``, git's stderr, to its end; returns git's last lines other than its
        progress. Git ends a progress line with a carriage return, so it's overwritten."""
        while chunk := await stream.read(_CHUNK):
            *lines, self._partial = re.split(r"[\r\n]", self._partial + self._decoder.decode(chunk))
            for line in lines:
                self._line(line)
        self._line(self._partial + self._decoder.decode(b"", final=True))
        return list(self._said)

    def _line(self, line: str) -> None:
        """Tell of the progress ``line`` reports if it changed, or else keep it."""
        noted = clone_progress(self._url, line)
        if noted is None:
            if line.strip():
                self._said.append(line.strip())
        elif noted != self._last:
            self._last = noted
            self._progress(noted)
