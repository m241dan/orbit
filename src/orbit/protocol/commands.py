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
    come before any other command. ``since`` is the last ``seq`` the client saw, if any; the
    open session's events after it are sent again in place of a ``Snapshot`` when they're all it
    missed. Answered with ``Welcome``, or refused."""

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


class ListSessions(_Message):
    """Ask for the saved sessions, answered with ``SessionsListed``."""

    type: Literal["list_sessions"] = "list_sessions"


class Attach(_Message):
    """View the session ``session``: the connection gets its ``Snapshot``, then its events from
    then on, in place of those of the session it viewed. A session other than the workspace's
    open one is a read-only view. Refused with ``unknown`` for a session there's none of."""

    type: Literal["attach"] = "attach"
    session: str


class OpenSession(_Message):
    """Retired, and always refused with ``invalid``: ``Attach`` views a session, and
    ``ActivateSession`` makes it the open one. Deprecated, to be removed at the next major
    ``PROTOCOL_VERSION``."""

    type: Literal["open_session"] = "open_session"
    session_id: str


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
    unreadable or open in another orbit. The sessions are listed again after."""

    type: Literal["rename_session"] = "rename_session"
    session_id: str
    name: str


class DeleteSession(_Message):
    """Delete the saved session ``session_id``; refused for the open session, or one open in
    another orbit. The sessions are listed again after."""

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
    | ListSessions
    | Attach
    | OpenSession
    | ActivateSession
    | NewSession
    | RenameSession
    | DeleteSession
    | ResetConversation
    | History
    | ResumeStorage
    | TakeOwnership,
    Field(discriminator="type"),
]


command_adapter: TypeAdapter[Command] = TypeAdapter(Command)
