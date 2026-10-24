"""The picker of a workspace's saved sessions, without the screen showing it: which rows it lists,
what picking one sends, naming a new session, and renaming or deleting saved ones."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Literal, Protocol

from orbit.protocol import (
    ActivateSession,
    Attach,
    DeleteSession,
    NewSession,
    RenameSession,
    SessionSummary,
)
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.sessions import (
    DELETE_TITLE,
    DELETE_YES,
    NEW_SESSION_LABEL,
    SESSIONS_HINTS,
    PickerRow,
    Viewing,
    can_delete,
    can_open,
    delete_message,
    name_hint,
    picked_commands,
    picker_rows,
    session_label,
    starting_commands,
)
from orbit.tui.view.typing_list import Label, TypingList, TypingListView

SessionChoice = Attach | ActivateSession | NewSession
"""What the user chose: a saved session to view, and perhaps activate, or a new one to start."""

StartupPick = Literal["resume", "pick"]
"""How the picker opens at startup: offering to resume the session viewed, or only to pick one."""

SessionChange = RenameSession | DeleteSession
"""A change to a saved session, made while the list stays open."""

Manage = Callable[[SessionChange, Awaited], Awaitable[object]]
"""Sends a change to a saved session to the backend, awaiting its outcome as told; the backend
lists the sessions again after one it accepts. Whatever it returns is ignored."""


class SessionPickerView(TypingListView[tuple[SessionChoice, ...]], Protocol):
    """What the session picker shows, what it asks the user, and where it sends changes."""

    async def manage(self, change: SessionChange, awaited: Awaited) -> object:
        """Send ``change`` to a saved session to the backend, awaiting its outcome as told."""
        ...

    def confirm(
        self, title: str, message: str, yes: str, then: Callable[[], Awaitable[object]]
    ) -> None:
        """Ask ``title`` with ``message``, ``yes`` naming what confirms, and await ``then`` only
        if the user confirms."""
        ...


class SessionPicker(TypingList[tuple[SessionChoice, ...]]):
    """Lists ``sessions`` of the workspace ``viewing`` names before ``New session``: the one it
    has attached first (as ``Resume`` if ``startup`` is ``resume`` or it hasn't joined the
    workspace, and not at all if ``startup`` is ``pick``), then the others; closes with the
    commands the user's choice sends, attaching to the workspace if it hasn't joined it.

    Picking a session that can't be opened does nothing; the attached session closes with none,
    keeping it. Picking ``New session`` asks for a name, which submitting confirms. Renaming asks
    for the highlighted session's new name the same way, and deleting deletes it once the user
    confirms; either is sent through the view, and a refusal shows why under the list, or in the
    name box's hint while it's shown, until the next change is sent; :meth:`refused` shows
    there why a pick was.
    """

    HINTS = SESSIONS_HINTS

    def __init__(
        self,
        view: SessionPickerView,
        sessions: Sequence[SessionSummary],
        viewing: Viewing,
        startup: StartupPick | None = None,
    ) -> None:
        super().__init__(view, (), startup is not None)
        self._picker_view = view
        self.workspace = viewing.workspace
        self.joined = viewing.joined
        self.sessions = tuple(sessions)
        self.attached = viewing.attached
        self.resume = startup == "resume"
        self._renaming: SessionSummary | None = None  # The session being renamed, if any.

    @property
    def rows(self) -> tuple[SessionSummary, ...]:
        """The sessions listed, in order, before ``New session``."""
        return tuple(row.summary for row in self._picker_rows)

    @property
    def _picker_rows(self) -> tuple[PickerRow, ...]:
        """The rows listed, in order, before ``New session``."""
        viewing = Viewing(self.workspace, self.attached, self.joined)
        return picker_rows(self.sessions, viewing, startup=self.startup, resume=self.resume)

    @property
    def keys(self) -> tuple[str | None, ...]:
        """The rows' session ids, then None for ``New session``."""
        return (*(row.id for row in self.rows), None)

    @property
    def labels(self) -> list[Label]:
        """The rows' labels, then ``New session``'s."""
        rows = [
            session_label(row.summary, attached=row.attached, resume=row.resume)
            for row in self._picker_rows
        ]
        return [*rows, NEW_SESSION_LABEL]

    def show(self, sessions: Sequence[SessionSummary], attached: str | None) -> None:
        """List ``sessions``, with the one ``attached`` first, in place of those listed, keeping
        the highlight on the session it was on, or where it was if that one's gone."""
        if not self._view.is_mounted:  # Listed again before it's on screen: it lists them then.
            self.sessions, self.attached = tuple(sessions), attached
            return
        shown = self.keys
        self.sessions, self.attached = tuple(sessions), attached
        self._relist(shown, self.keys, self.labels)

    def refused(self, why: str) -> None:
        """Show ``why`` a pick was refused under the list, as soon as it's on screen."""
        if self._view.is_mounted:
            self._refused(why)
        else:
            self._view.show_note(why)

    @property
    def _highlighted(self) -> SessionSummary | None:
        """The session highlighted; none on ``New session``."""
        rows = self.rows
        index = self._view.highlighted or 0
        return rows[index] if index < len(rows) else None

    def choose(self, index: int) -> None:
        """Pick row ``index``: name a new session on ``New session``, or else close with the
        commands opening the session there, unless it can't be opened."""
        rows = self.rows
        if index == len(rows):
            self._naming(True)
            return
        summary = rows[index]
        if can_open(summary):
            attached = self.attached if self.joined else None
            self._view.dismiss(picked_commands(summary, self.sessions, attached, self.workspace))

    async def named(self, text: str) -> None:
        """Take the name submitted, ``text``: start a new session with it, empty leaving the name
        to the backend, or rename the session being renamed, an empty name keeping its own."""
        name = text.strip()
        if self._renaming is None:
            viewing = Viewing(self.workspace, self.attached, self.joined)
            self._view.dismiss(starting_commands(name or None, viewing))
            return
        renaming = self._renaming
        self._naming(False)
        if name:
            await self._change(RenameSession(session_id=renaming.id, name=name))

    def rename(self) -> None:
        """Ask for the highlighted session's new name, starting from its name."""
        summary = self._highlighted
        if summary is not None:
            self._naming(True, summary)

    async def delete(self) -> None:
        """Delete the highlighted session once the user confirms; one that can't be deleted is
        sent without asking, for the backend to refuse, which shows why here."""
        summary = self._highlighted
        if summary is None:
            return
        deleting = DeleteSession(session_id=summary.id)
        if not can_delete(summary):
            await self._change(deleting)
            return
        message = delete_message(summary)
        self._picker_view.confirm(DELETE_TITLE, message, DELETE_YES, lambda: self._change(deleting))

    async def _change(self, change: SessionChange) -> None:
        """Send ``change``, showing why it's refused here, in place of any refusal shown."""
        self._view.show_note(None)
        await self._picker_view.manage(change, Awaited(why=self._refused))

    def _naming(self, on: bool, renaming: SessionSummary | None = None) -> None:
        """Show the box for the name of a new session, or the new name of the session
        ``renaming``, and type in it; or go back to the list."""
        self._renaming = renaming
        if not on:
            self._type(None)
            return
        self._type(name_hint(renaming=renaming is not None), renaming.name if renaming else "")
