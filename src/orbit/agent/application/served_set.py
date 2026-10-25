"""The set of workspaces a control plane serves, as frontends add them, by their paths or as
clones of repositories, and remove them, purging them if asked."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import PurePath

from orbit.agent.application.browsing import Browsing
from orbit.agent.application.clones import Clones
from orbit.agent.application.coder_slots import CoderSlots
from orbit.agent.application.connection import SessionConnection
from orbit.agent.application.failures import log_unexpected
from orbit.agent.application.purge import purge
from orbit.agent.application.workspace_host import WorkspaceHost
from orbit.agent.application.workspace_source import WorkspaceSource, WorkspaceUnavailable
from orbit.agent.domain.clones import added_url, local_clone_source
from orbit.agent.domain.purging import Purged, owned_refusal
from orbit.agent.domain.refusals import STOPPING, Refused, failed_to
from orbit.agent.domain.workspaces import (
    RegisteredWorkspace,
    check_addable,
    check_directory,
    check_removable,
    served_workspace,
)
from orbit.protocol import INVALID, AddWorkspace, RemoveWorkspace


class _ServedPurging:
    """Purging the workspace a control plane serves with ``host``, which ``workspaces`` holds
    the lock of, having had it ``stop``, which stops serving it, before deleting it, as
    :func:`purge` does; refused as ``busy`` while another orbit owns its repository, and, with
    the code ``workspaces`` gives, if what it would lose can't be found out or deleting it
    fails."""

    def __init__(
        self, host: WorkspaceHost, workspaces: WorkspaceSource, stop: Callable[[], Awaitable[None]]
    ) -> None:
        self._host = host
        self._workspaces = workspaces
        self._stop = stop
        self._name = host.display_name

    async def find(self, key: str) -> RegisteredWorkspace:
        """The workspace served with the host, whose id is ``key``."""
        return RegisteredWorkspace(key, self._name, served=True, managed=self._host.managed)

    @asynccontextmanager
    async def held(self, workspace: RegisteredWorkspace) -> AsyncIterator[str | None]:
        """Run the block, the lock being held already, giving why it can't be purged if another
        orbit owns its repository."""
        owner = self._host.owner
        yield None if owner is None else owned_refusal(self._name, owner)

    async def losses(self, workspace: RegisteredWorkspace) -> list[str]:
        """What purging ``workspace`` would lose, as ``workspaces`` says."""
        try:
            return await self._workspaces.losses(workspace.id)
        except WorkspaceUnavailable as exc:
            raise Refused(exc.code, f"Can't purge {self._name}: {exc}") from None

    async def stop(self, workspace: RegisteredWorkspace) -> None:
        """Stop serving ``workspace``."""
        await self._stop()

    async def delete(self, workspace: RegisteredWorkspace) -> Purged:
        """Have ``workspaces`` delete what orbit keeps of ``workspace``; refused, saying to
        finish with ``orbit db forget``, which no longer finds it served, if that fails."""
        try:
            return await self._workspaces.purge(workspace.id)
        except WorkspaceUnavailable as exc:
            unpurged = f"Removed {self._name}, but couldn't purge it: {exc}"
            finish = f"run orbit db forget {self._name} to finish."
            raise Refused(exc.code, f"{unpurged}; {finish}") from None


async def _flag_anyway(host: WorkspaceHost, workspaces: WorkspaceSource, *, served: bool) -> None:
    """Have ``workspaces`` flag ``host``'s workspace not served, or, if ``served``, served again
    as it was, keeping when it was added, as it cleans up after another failure, which it
    mustn't replace: its own failure is only logged."""
    try:
        if served:
            await workspaces.restore_served(host.workspace_id)
        else:
            await workspaces.set_served(host.workspace_id, False)
    except WorkspaceUnavailable as exc:
        flag = "served" if served else "not served"
        log_unexpected(failed_to(f"flag {host.display_name} {flag}", exc), exc)


@dataclass(frozen=True)
class ServedParts:
    """What a :class:`ServedSet` changes and tells: the ``hosts`` served, by workspace id, the
    ``workspaces`` source, the ``changing`` lock one change at a time holds, ``closing``, which
    says whether the control plane is closing, the ``coders``' slots, the ``clones`` under way,
    ``browsing``, if any, whose root confines the paths added, and the control plane's ``serve``,
    which wires a host in, ``begin``, which opens it and has it served, ``detach``, which has a
    connection attached to no workspace, ``changed``, which sends the catalog as what's served
    changed, and ``totals_changed``, which has the usage totals read afresh."""

    hosts: dict[str, WorkspaceHost]
    workspaces: WorkspaceSource
    changing: asyncio.Lock
    closing: Callable[[], bool]
    coders: CoderSlots
    clones: Clones
    browsing: Browsing | None
    serve: Callable[[WorkspaceHost], None]
    begin: Callable[[WorkspaceHost], Awaitable[None]]
    detach: Callable[[SessionConnection], None]
    changed: Callable[[SessionConnection | None], Awaitable[None]]
    totals_changed: Callable[[], None]


class ServedSet:
    """Adds and removes the workspaces a control plane serves, from ``parts``: ``AddWorkspace``
    adds a workspace, starting its host, and ``RemoveWorkspace`` removes one, closing its host
    and, if asked, purging its records, each followed by the catalog; one at a time, as each
    changes what's served across its awaits. An ``AddWorkspace`` with a URL is accepted at once,
    the repository cloned in the background by the source, and the clone then added as a path
    is, as a managed one."""

    def __init__(self, parts: ServedParts) -> None:
        self._parts = parts
        self._hosts = parts.hosts
        self._workspaces = parts.workspaces

    async def change(
        self, command: AddWorkspace | RemoveWorkspace, connection: SessionConnection
    ) -> None:
        """Add or remove the workspace ``command`` names, as ``connection`` asks, once no other
        change is under way, or start cloning the repository it names; refused once closing, or
        for a local repository outside the browsable root."""
        if isinstance(command, RemoveWorkspace):
            async with self._locked():
                await self._remove(command, connection)
            return
        url = added_url(command.path, command.url)
        if url is not None:
            await self._confine_source(url)
            self._clone(url)
            return
        assert command.path is not None  # added_url refuses an AddWorkspace with neither.
        async with self._locked():
            await self._add(command.path, connection)

    async def _confine(self, path: str, managed: bool) -> None:
        """Refuse adding the workspace at ``path`` as ``invalid`` when it's outside the
        browsable root, if there is one, with its symlinks followed, unless it's ``managed``, a
        clone orbit made in its clone directory."""
        if not managed and self._parts.browsing is not None:
            await self._parts.browsing.confined(path)

    async def _confine_source(self, url: str) -> None:
        """Refuse cloning ``url`` as ``invalid`` when it's a path on the server's host, or a
        ``file://`` URL to one, outside the browsable root, if there is one, with its symlinks
        followed."""
        path = local_clone_source(url)
        if path is not None and self._parts.browsing is not None:
            await self._parts.browsing.confined(path)

    @asynccontextmanager
    async def _locked(self) -> AsyncIterator[None]:
        """Run the block once no other change is under way; refused once closing."""
        async with self._parts.changing:
            self._check_open()
            yield

    def _check_open(self) -> None:
        """Refuse a change as ``invalid`` once closing."""
        if self._parts.closing():
            raise Refused(INVALID, STOPPING)

    def _clone(self, url: str) -> None:
        """Have the source clone the repository at ``url`` in the background, then serve the
        clone, managed; refused once closing, or as ``busy`` while ``url`` is being cloned
        already."""
        self._check_open()

        async def serve(path: str) -> str:
            async with self._locked():
                return await self._add(path, None, managed=True)

        self._parts.clones.start(url, self._workspaces, serve)

    async def _add(
        self, path: str, connection: SessionConnection | None, managed: bool = False
    ) -> str:
        """The id of the workspace at ``path``, served from the source, as a clone orbit made
        if ``managed``, unless it's served already: start its host, opening its most recently
        active session and its background work, and send the catalog to ``connection``, if one
        asked, and every listening connection, or let it go again if that fails or is
        cancelled, raising what failed even if clearing its flag fails; refused unless ``path``
        is the absolute path to a directory, within the browsable root, as its work tree's top
        level must be, unless it's ``managed``, or with the code it gives if it can't be served."""
        workspaces = self._workspaces
        check_addable(path, absolute=PurePath(path).is_absolute())
        await self._confine(path, managed)
        check_directory(path, await workspaces.kind(path))
        root = await workspaces.top_level(path)
        await self._confine(root, managed)  # Its work tree may reach above the browsable root.
        served = self._served_at(root)
        if served is not None:
            return served
        try:
            host = await workspaces.open(root, managed=managed)
        except WorkspaceUnavailable as exc:
            raise Refused(exc.code, f"Can't add {root}: {exc}") from None
        try:
            await self._start(host, connection)
        except BaseException:
            try:
                await _flag_anyway(host, workspaces, served=False)
            finally:
                await self._let_go(host)
            raise
        return host.workspace_id

    def _served_at(self, root: str) -> str | None:
        """The id of the workspace served at ``root``, if any."""
        return next((host.workspace_id for host in self._hosts.values() if host.path == root), None)

    async def _start(self, host: WorkspaceHost, connection: SessionConnection | None) -> None:
        """Serve ``host``, as the control plane begins one, and send the catalog to
        ``connection``, if given, and every listening connection. Its events are stamped and
        delivered as it opens its most recently active session, but no ``Attach`` reaches it
        till it's open."""
        self._parts.serve(host)
        await self._parts.begin(host)
        await self._parts.changed(connection)

    async def _remove(self, command: RemoveWorkspace, connection: SessionConnection) -> None:
        """Stop serving the workspace ``command`` names: have the source stop flagging it
        served, then close its host, having ``connection``, if it's attached there, attached to
        none, if ``command`` says to purge it, delete its records, as :func:`purge` does, while
        its lock is still held, and have the source let it go, and send the catalog to
        ``connection`` and every listening connection, even if closing the host or purging
        fails, then, once a purge has run, even if it failed, have the usage totals read
        afresh; refused for a workspace not served, as ``busy`` while it runs work or another
        connection views one of its sessions or, when purging, another orbit owns its
        repository, as ``invalid`` when purging it would lose work, or with the code the source
        gives, still served, if its flag can't be cleared."""
        host = self._hosts[served_workspace(command.workspace, tuple(self._hosts))]
        self._check_removable(host)
        try:
            async with AsyncExitStack() as stack:
                stop = partial(self._stop, host, connection, stack)
                if command.purge:
                    await purge(_ServedPurging(host, self._workspaces, stop), host.workspace_id)
                else:
                    await stop()
        finally:
            if command.purge:  # Some of its records may be gone even if purging failed.
                self._parts.totals_changed()

    async def _stop(
        self, host: WorkspaceHost, connection: SessionConnection, stack: AsyncExitStack
    ) -> None:
        """Stop serving ``host``'s workspace: have the source stop flagging it served, as
        :meth:`_unflag` does, have ``connection``, if it's attached there, attached to none, and
        close ``host``, its place in line for a coder, if any, going at once; once ``stack``
        closes, even if closing ``host`` fails, have the source let it go and send the catalog."""
        await self._unflag(host)
        stack.push_async_callback(self._parts.changed, connection)
        stack.push_async_callback(self._workspaces.close, host.workspace_id)
        if connection.workspace == host.workspace_id:
            self._parts.detach(connection)
        await self._close_host(host)

    @staticmethod
    def _check_removable(host: WorkspaceHost) -> None:
        """Refuse removing ``host``'s workspace as ``busy`` while it runs work or another
        connection views one of its sessions."""
        viewed = host.connections.others_view()
        check_removable(host.display_name, host.activity, viewed)

    async def _unflag(self, host: WorkspaceHost) -> None:
        """Have the source stop flagging ``host``'s workspace served, restoring the flag and
        refusing as :meth:`_check_removable` does if it got busy meanwhile, even if restoring it
        fails; refused with the code the source gives if that fails."""
        try:
            await self._workspaces.set_served(host.workspace_id, False)
        except WorkspaceUnavailable as exc:
            raise Refused(
                exc.code, f"Can't remove {host.display_name}, which is still served: {exc}"
            ) from None
        try:
            self._check_removable(host)
        except Refused:
            await _flag_anyway(host, self._workspaces, served=True)
            raise

    async def _let_go(self, host: WorkspaceHost) -> None:
        """Stop serving ``host``'s workspace: close it, as :meth:`_close_host` does, then have
        the source let it go, even if closing ``host`` fails."""
        try:
            await self._close_host(host)
        finally:
            await self._workspaces.close(host.workspace_id)

    async def _close_host(self, host: WorkspaceHost) -> None:
        """Close ``host``, no longer served; its place in line for a coder, if any, goes at
        once."""
        self._hosts.pop(host.workspace_id, None)
        self._parts.coders.drop(host.workspace_id)
        await host.close()
