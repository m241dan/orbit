"""How the app opens: once the first catalog lists the workspaces, attaching to one and
sending the command to open with, or opening a picker."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from orbit.protocol import Catalog, Command, Event
from orbit.tui.view.session_picker import StartupPick


def lists_catalog(batch: Sequence[Event]) -> bool:
    """Whether ``batch`` holds a catalog."""
    return any(isinstance(event, Catalog) for event in batch)


@dataclass(frozen=True)
class Opening:
    """How the app opens: on the workspace at the ``repository`` it runs in, if there's one,
    which a linked app's workspace picker preselects or offers to add, sending ``command``, if
    given, once attached, and with the session picker if ``pick``."""

    command: Command | None = None
    pick: bool = False
    repository: str | None = None


Send = Callable[[Command], Awaitable[bool]]
"""Sends a command to the backend, returning whether it was sent."""


class Sessions(Protocol):
    """The sessions of the workspace the app is on, as :class:`SessionsHost` keeps them."""

    async def attach_active(self) -> bool: ...

    async def join(self, workspace: str) -> bool: ...

    def open(self, startup: StartupPick | None = None, workspace: str | None = None) -> None: ...


class Workspaces(Protocol):
    """The workspaces served, as :class:`WorkspacesHost` keeps them."""

    def open(self, startup: bool = False) -> None: ...


@dataclass(frozen=True)
class Hosts:
    """The app's hosts of its ``sessions`` and its ``workspaces``."""

    sessions: Sessions
    workspaces: Workspaces


class OpeningHost:
    """Opens the app as its ``opening`` says, once: over a link, if ``linked``, the first
    catalog opens the picker of the ``hosts``' workspaces; otherwise the app attaches to the
    active session of the workspace their sessions open on, then sends the opening's command,
    with ``send``, or, if the opening says to pick, opens the session picker."""

    def __init__(self, opening: Opening, linked: bool, send: Send, hosts: Hosts) -> None:
        self._command = opening.command
        self._pick = opening.pick
        self._linked = linked
        self._send = send
        self._sessions = hosts.sessions
        self._workspaces = hosts.workspaces
        self._attaching = True  # Until the first catalog has the app attach.

    async def listed(self, batch: Sequence[Event]) -> None:
        """Once ``batch`` has the first catalog, open the workspace picker over a link; else
        attach to the active session of the workspace opened on, then send the command to open
        with, if there's one, once that's sent."""
        if not self._first_listed(batch):
            return
        if self._linked:
            self._workspaces.open(startup=True)
            return
        await self._open_on(self._sessions.attach_active())

    def _first_listed(self, batch: Sequence[Event]) -> bool:
        """Whether ``batch`` has the first catalog, after which the app no longer waits for
        one."""
        if not self._attaching or not lists_catalog(batch):
            return False
        self._attaching = False
        return True

    async def open_workspace(self, workspace: str, startup: bool) -> None:
        """Open the session picker of the ``workspace`` picked; at ``startup``, unless the app
        picks a session, attach to its active session and send the command to open with."""
        if startup and not self._pick:
            await self._open_on(self._sessions.join(workspace))
            return
        self._sessions.open("resume" if startup else None, workspace)

    def catalog_shown(self) -> None:
        """In process, open the session picker on the first catalog shown, if the app picks
        one."""
        if self._pick and not self._linked:
            self._pick = False
            self._sessions.open("pick")

    async def _open_on(self, attaching: Awaitable[bool]) -> None:
        """Await ``attaching`` to the active session of a workspace, then, once that's sent,
        send the command to open with."""
        if await attaching:
            await self._send_opening()

    async def _send_opening(self) -> None:
        """Send the command to open with, if there's one, only once."""
        if self._command is not None:
            command, self._command = self._command, None
            await self._send(command)
