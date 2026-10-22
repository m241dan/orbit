"""Commands, which flow from the frontend to the backend.

They're a discriminated union on ``type``, so they can be serialized to JSON and sent across a
process boundary without changes.
"""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from orbit.protocol.models import Settings, TaskSpec, _Message
from orbit.protocol.names import WORK_LANE, BusyLane


class Hello(_Message):
    """Open the stream as ``client``, speaking ``protocol_version`` of the protocol; it must
    come before any other command. Answered with ``Welcome``, then a ``Catalog``, or refused;
    it attaches the connection to no session, which ``Attach`` does. ``since`` is the last
    ``seq`` the client saw, if any; once the connection attaches to a workspace's open session,
    that session's events after it are sent again in place of a ``Snapshot`` when they're all
    it missed."""

    type: Literal["hello"] = "hello"
    client: str
    protocol_version: int
    since: int | None = None


class SendMessage(_Message):
    """Send ``text`` to the conversational agent ``agent``, which answers in its own lane."""

    type: Literal["send_message"] = "send_message"
    agent: str = Field(min_length=1)
    text: str = Field(min_length=1)


class CancelTurn(_Message):
    """Stop the work in ``lane``: the planner's turn, or the task run."""

    type: Literal["cancel_turn"] = "cancel_turn"
    lane: BusyLane = WORK_LANE


class UpdateSettings(_Message):
    """Replace the session settings; they apply from the next turn."""

    type: Literal["update_settings"] = "update_settings"
    settings: Settings


class AddTask(_Message):
    """Append ``task`` to the backend's task list."""

    type: Literal["add_task"] = "add_task"
    task: TaskSpec


class EditTask(_Message):
    """Replace task ``task_id`` with ``task`` and make it pending again.

    Rejected while that task runs.
    """

    type: Literal["edit_task"] = "edit_task"
    task_id: str
    task: TaskSpec


class RunTasks(_Message):
    """Run the task list: every task that hasn't finished, in order, each in a new context.

    Tasks added while it runs join in. Rejected while a run is in progress (the planner's
    turns don't count); ``CancelTurn`` stops the task in progress and the run.
    """

    type: Literal["run_tasks"] = "run_tasks"


class AcceptTask(_Message):
    """Make draft task ``task_id`` (one the planner proposed) a real task, to be run."""

    type: Literal["accept_task"] = "accept_task"
    task_id: str


class RemoveTask(_Message):
    """Take task ``task_id`` off the list; rejected while it runs."""

    type: Literal["remove_task"] = "remove_task"
    task_id: str


class CancelAgent(_Message):
    """Stop agent ``agent_id``'s work: a review on its own, or the run (or planner turn) it's in."""

    type: Literal["cancel_agent"] = "cancel_agent"
    agent_id: str


class FireTrigger(_Message):
    """Fire the manual trigger named ``trigger`` with ``input`` as its template values."""

    type: Literal["fire_trigger"] = "fire_trigger"
    trigger: str = Field(min_length=1)
    input: dict[str, str] = Field(default={})


ProposalAction = Literal["accept", "revise", "end"]
"""What the user made of proposed checks: take them, ask for changes, or end the task."""


class DecideProposal(_Message):
    """The user's decision on the checks proposed for task ``task_id``; ``feedback`` says what
    to change when the ``action`` is ``revise``."""

    type: Literal["decide_proposal"] = "decide_proposal"
    task_id: str
    action: ProposalAction
    feedback: str = ""


class Attach(_Message):
    """View the session ``session`` of the workspace ``workspace``: the connection gets its
    ``Snapshot``, then its events from then on, in place of those of the session it viewed. A
    session other than the workspace's open one is a read-only view. A connection's first
    ``Attach`` after ``Hello`` is answered, for a ``Hello`` whose ``since`` it can catch up from
    on the workspace's open session, by that session's events after it in place of the
    ``Snapshot``. Refused with ``unknown`` for a workspace not served or a session there's none
    of."""

    type: Literal["attach"] = "attach"
    workspace: str
    session: str


class ActivateSession(_Message):
    """Make the saved session ``session`` the workspace's open one: its tasks, review state,
    planner conversation and settings replace the open session's, and nothing runs until asked.
    The connection views it, live; the others keep viewing what they viewed, the session left a
    read-only view. Refused with ``busy`` unless the open session is idle."""

    type: Literal["activate_session"] = "activate_session"
    session: str


class NewSession(_Message):
    """Make a new, empty session ``name``d so, or by the time it started if ``name`` is none, the
    open one, as ``ActivateSession`` does a saved one, and refused as it is."""

    type: Literal["new_session"] = "new_session"
    name: str | None = None


class RenameSession(_Message):
    """Name the session ``session_id`` ``name``; refused for an empty name, or a session that's
    unreadable or open in another orbit. A ``Catalog`` follows."""

    type: Literal["rename_session"] = "rename_session"
    session_id: str
    name: str


class DeleteSession(_Message):
    """Delete the saved session ``session_id``; refused for the open session, or one open in
    another orbit. A ``Catalog`` follows."""

    type: Literal["delete_session"] = "delete_session"
    session_id: str


class ResetConversation(_Message):
    """End the conversation of the agent ``agent``, so its next message starts a fresh one, and
    re-read its role from the project's config. Refused while the agent answers."""

    type: Literal["reset_conversation"] = "reset_conversation"
    agent: str = Field(min_length=1)


class History(_Message):
    """Ask for the events of the session ``session`` logged before ``before_seq``: the latest
    ``limit`` of them, at most 500, answered with ``HistoryPage``. Refused for a session that
    isn't this workspace's."""

    type: Literal["history"] = "history"
    session: str
    before_seq: int = Field(ge=1)
    limit: int = Field(default=200, ge=1, le=500)


class ResumeStorage(_Message):
    """Retry the writes to the store that halted the session, and lift the halt once they work,
    logging what was parked in the fallback file; refused with ``storage_failed`` while they
    still fail. Does nothing while the session isn't halted."""

    type: Literal["resume_storage"] = "resume_storage"


class TakeOwnership(_Message):
    """Answer ``OwnershipOffered``: with ``take``, this read-only orbit owns the repository from
    now on and runs the work it queued; else it lets the repository go again, stays read-only
    and offers it once more when it's next free. Refused with ``invalid`` when no offer is
    open."""

    type: Literal["take_ownership"] = "take_ownership"
    take: bool


class AddWorkspace(_Message):
    """Serve the workspace at ``path``, or a clone of the repository at ``url``; exactly one is
    given, else it's refused with ``invalid``. A control-plane command: its envelope's
    ``workspace`` is ``null``; a local orbit, which serves its own repository alone, refuses it
    with ``invalid``.

    ``path`` is an absolute path on the server's host to a directory: a repository's top level,
    which a path inside it stands for, or a plain directory. It's registered, or found by its
    path, opens its most recently active session, takes its lock and runs its startup check, and
    a ``Catalog`` listing it follows. Accepted, changing nothing, for a workspace already served;
    refused with ``invalid`` for a relative path or a file, and ``unknown`` for a path there's
    nothing at.

    ``url`` is an ``https://``, ``ssh://`` or ``file://`` URL, a scp-style ``user@host:path`` or
    an absolute path on the server's host; anything else is refused with ``invalid``, and a URL
    already being cloned with ``busy``. It's accepted at once and cloned in the background into
    the server's clone directory, with ``WorkspaceCloning`` events reporting git's progress; the
    clone is then served as one added by its path is, and a ``Catalog`` listing it follows, or
    a control-plane ``ErrorOccurred`` coded ``git_failed`` gives git's message."""

    type: Literal["add_workspace"] = "add_workspace"
    path: str | None = Field(default=None, min_length=1)
    url: str | None = Field(default=None, min_length=1)


class RemoveWorkspace(_Message):
    """Stop serving the workspace ``workspace``, by its id: its lock is let go, its sessions are
    kept for when it's added again, and a ``Catalog`` without it follows; the connection sending
    it, if attached there, is attached to no workspace again. A control-plane command: its
    envelope's ``workspace`` is ``null``. Refused with ``unknown`` for a workspace not served,
    ``busy`` while it runs work or another connection views one of its sessions, and
    ``invalid`` by a local orbit. With ``purge``, its records are deleted too: its sessions,
    events, logs, review count, session locks, fallback files and registry entry, and, for a
    clone orbit made, its directory; refused with ``invalid``, listing what would be lost and
    removing nothing, while that clone holds work its remote doesn't."""

    type: Literal["remove_workspace"] = "remove_workspace"
    workspace: str
    purge: bool = False


Command = Annotated[
    Hello
    | SendMessage
    | CancelTurn
    | UpdateSettings
    | AddTask
    | EditTask
    | RunTasks
    | FireTrigger
    | AcceptTask
    | RemoveTask
    | CancelAgent
    | DecideProposal
    | Attach
    | ActivateSession
    | NewSession
    | RenameSession
    | DeleteSession
    | ResetConversation
    | History
    | ResumeStorage
    | TakeOwnership
    | AddWorkspace
    | RemoveWorkspace,
    Field(discriminator="type"),
]


command_adapter: TypeAdapter[Command] = TypeAdapter(Command)
