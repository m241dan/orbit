"""The empty temporary directories agents whose role works in none work in: made in the system's
temporary directory, off the event loop, and removed after their run."""

import asyncio
import contextlib
import shutil
import tempfile

from orbit.agent.application.ports import TempDirFailed
from orbit.agent.external.shielding import shielded

PREFIX = "orbit-agent-"
"""What the names of the temporary directories start with."""


class SystemTempDirs:
    """Makes empty directories in the system's temporary directory and removes them."""

    async def make(self) -> str:
        making = asyncio.ensure_future(asyncio.to_thread(_make_temp_dir))
        try:
            return await shielded(making)
        except asyncio.CancelledError:
            with contextlib.suppress(TempDirFailed):  # It failed to make one: nothing to remove.
                await asyncio.to_thread(shutil.rmtree, making.result(), ignore_errors=True)
            raise

    async def remove(self, path: str) -> None:
        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


def _make_temp_dir() -> str:
    """A new, empty directory in the system's temporary directory.

    Raises :class:`TempDirFailed` when it can't be made.
    """
    try:
        return tempfile.mkdtemp(prefix=PREFIX)
    except OSError as error:
        raise TempDirFailed(f"Couldn't make a temporary directory: {error}") from error
