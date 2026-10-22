"""Browsing the directories on the server's host, from its starting directory, beside the
workspace added last, confined to its browsable root if it has one."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePath

from orbit.agent.application.ports import DirectoryLister
from orbit.agent.domain.listings import check_inside, check_listable, listing, unreadable
from orbit.agent.domain.refusals import Refused
from orbit.protocol import DirectoryListing


@dataclass(frozen=True)
class Browsing:
    """How frontends browse the server's host: read by ``lister``, starting at ``home`` unless
    confined to ``root``, the absolute directory, with its symlinks followed, that they may
    browse and add workspaces within."""

    lister: DirectoryLister
    home: str
    root: str | None = None

    async def listing(
        self, path: str | None, served: Mapping[str, str], latest: str | None = None
    ) -> DirectoryListing:
        """The listing of the directory at ``path``, with its symlinks followed, or of the
        starting directory if it's ``None``, its entries marked with the ids of the workspaces
        ``served`` at their paths; refused as ``invalid`` for a relative path, one outside the
        root, a file or one that can't be read, and as ``unknown`` when there's nothing
        there.

        The starting directory is the one holding ``latest``, the workspace added last, if it
        can be listed, else the root, if there is one, else home."""
        if path is not None:
            return await self._listed(path, served)
        if latest is not None:
            try:
                return await self._listed(str(PurePath(latest).parent), served)
            except Refused:
                pass  # Gone or outside the root, so start where there's no workspace.
        return await self._listed(self.root or self.home, served)

    async def _listed(self, path: str, served: Mapping[str, str]) -> DirectoryListing:
        """The listing of the directory at ``path``, as :meth:`listing` says."""
        check_listable(path, absolute=PurePath(path).is_absolute())
        resolved = await self.confined(path)
        try:
            found = await self.lister.directories(resolved)
        except OSError as exc:
            raise unreadable(path, exc) from None
        return listing(resolved, self._parent(resolved), found, served)

    async def confined(self, path: str) -> str:
        """``path``, absolute, with its symlinks followed; refused as ``invalid`` when that's
        outside the root, if there is one."""
        resolved = await self.lister.resolved(path)
        inside = self.root is None or PurePath(resolved).is_relative_to(self.root)
        check_inside(path, self.root, inside=inside)
        return resolved

    def _parent(self, path: str) -> str | None:
        """The directory above ``path``; ``None`` at the filesystem's root or the root."""
        pure = PurePath(path)
        if pure == pure.parent or path == self.root:
            return None
        return str(pure.parent)
