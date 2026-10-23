"""The clones a control plane makes of the repositories frontends ask it to serve by URL, each in
the background, one at a time per URL."""

import asyncio
from collections.abc import Awaitable, Callable

from orbit.agent.application.ports import CloneFailed, Cloner
from orbit.agent.domain.clones import clone_served, couldnt_clone
from orbit.agent.domain.refusals import Refused
from orbit.protocol import BUSY, GIT_FAILED, INTERNAL, ErrorOccurred, Event


class Clones:
    """The clones under way, by URL, each telling every connection of its progress and, once
    it's done, the workspace it's served as or why it failed, through ``tell``."""

    def __init__(self, tell: Callable[[Event], None]) -> None:
        self._tell = tell
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, url: str, cloner: Cloner, serve: Callable[[str], Awaitable[str]]) -> None:
        """Have ``cloner`` clone the repository at ``url`` in the background, then ``serve``
        the clone, which gives the workspace it's served as; refused as ``busy`` while ``url``
        is being cloned already."""
        if url in self._tasks:
            raise Refused(BUSY, f"Can't clone {url}: it's being cloned already.")
        self._tasks[url] = asyncio.create_task(self._clone(url, cloner, serve))

    async def _clone(
        self, url: str, cloner: Cloner, serve: Callable[[str], Awaitable[str]]
    ) -> None:
        """Have ``cloner`` clone the repository at ``url``, then ``serve`` the clone, telling
        every connection the workspace it's served as, or else an error naming ``url``, with a
        message starting ``Couldn't clone <url>: ``: as ``git_failed`` if the clone fails, with
        the code refusing to serve it gives, or as ``internal`` if orbit fails; ``url`` is free
        to clone again once it's done."""
        try:
            workspace = await _served(await cloner.clone(url, self._tell), cloner, serve)
        except CloneFailed as exc:  # Its message names the URL already.
            self._failed(url, GIT_FAILED, str(exc))
        except Refused as refusal:
            self._failed(url, refusal.code, couldnt_clone(url, str(refusal)))
        except Exception as exc:  # A bug mustn't go unreported, as no command waits on it.
            self._failed(
                url, INTERNAL, couldnt_clone(url, f"Orbit failed to serve the clone: {exc}")
            )
        else:
            self._tell(clone_served(url, workspace))
        finally:
            # Freed here, not as the task ends a step later, so a frontend shown the clone's
            # catalog can clone the URL again at once.
            del self._tasks[url]

    def _failed(self, url: str, code: str, message: str) -> None:
        """Tell every connection the clone of ``url`` failed, as ``code`` and ``message`` say."""
        self._tell(ErrorOccurred(code=code, message=message, url=url))

    async def close(self) -> None:
        """Stop every clone under way, each removing what it cloned, and wait until they have."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _served(path: str, cloner: Cloner, serve: Callable[[str], Awaitable[str]]) -> str:
    """The workspace ``serve`` serves the clone ``cloner`` made at ``path`` as, having ``cloner``
    remove it if that fails or is cancelled, once ``serve`` has let go of it, so a failed clone
    leaves nothing behind."""
    try:
        return await serve(path)
    except BaseException:
        await cloner.remove(path)
        raise
