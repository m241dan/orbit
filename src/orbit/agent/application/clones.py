"""The clones a control plane makes of the repositories frontends ask it to serve by URL, each in
the background, one at a time per URL."""

import asyncio
from collections.abc import Awaitable, Callable

from orbit.agent.application.ports import CloneFailed, Cloner
from orbit.agent.domain.refusals import Refused
from orbit.protocol import BUSY, GIT_FAILED, INTERNAL, ErrorOccurred, Event


class Clones:
    """The clones under way, by URL, each telling every connection of its progress and, if it
    fails, why, through ``tell``."""

    def __init__(self, tell: Callable[[Event], None]) -> None:
        self._tell = tell
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, url: str, cloner: Cloner, serve: Callable[[str], Awaitable[None]]) -> None:
        """Have ``cloner`` clone the repository at ``url`` in the background, then ``serve``
        the clone; refused as ``busy`` while ``url`` is being cloned already."""
        if url in self._tasks:
            raise Refused(BUSY, f"Can't clone {url}: it's being cloned already.")
        self._tasks[url] = asyncio.create_task(self._clone(url, cloner, serve))

    async def _clone(
        self, url: str, cloner: Cloner, serve: Callable[[str], Awaitable[None]]
    ) -> None:
        """Have ``cloner`` clone the repository at ``url``, then ``serve`` the clone, telling
        every connection if either fails: as ``git_failed`` if the clone does, with the code
        refusing to serve it gives, or as ``internal`` if orbit fails; ``url`` is free to clone
        again once it's done."""
        try:
            await serve(await cloner.clone(url, self._tell))
        except CloneFailed as exc:
            self._tell(ErrorOccurred(code=GIT_FAILED, message=str(exc)))
        except Refused as refusal:
            self._tell(ErrorOccurred(code=refusal.code, message=str(refusal)))
        except Exception as exc:  # A bug mustn't go unreported, as no command waits on it.
            message = f"Orbit failed to serve the clone of {url}: {exc}"
            self._tell(ErrorOccurred(code=INTERNAL, message=message))
        finally:
            # Freed here, not as the task ends a step later, so a frontend shown the clone's
            # catalog can clone the URL again at once.
            del self._tasks[url]

    async def close(self) -> None:
        """Stop every clone under way, each removing what it cloned, and wait until they have."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
