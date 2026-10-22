"""Who owns a repository: the first orbit to reach it, which runs its work, as its workspace
lock says; any other orbit on it is read-only."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class WorkspaceOwner(BaseModel):
    """The orbit owning a repository, as its lock says: its process ``pid`` and, for an
    ``orbit serve``, the ``url`` it listens at; neither when the lock can't be read."""

    model_config = ConfigDict(frozen=True, strict=True)

    pid: int | None = Field(default=None, gt=0)
    url: str | None = None

    @property
    def described(self) -> str:
        """The owner as the user is told of it."""
        if self.url is not None:
            return f"an orbit serve at {self.url}"
        if self.pid is not None:
            return f"an orbit chat, pid {self.pid}"
        return "another orbit"


def owner_text(owner: WorkspaceOwner) -> str:
    """What the lock of the repository ``owner`` owns holds: its JSON."""
    return owner.model_dump_json(exclude_none=True) + "\n"


def parse_owner(text: str) -> WorkspaceOwner:
    """The owner a lock's ``text`` names; one naming nobody when it's unreadable, as while the
    owner is still writing it."""
    try:
        return WorkspaceOwner.model_validate_json(text)
    except ValidationError:
        return WorkspaceOwner()


def read_only_note(owner: WorkspaceOwner) -> str:
    """The note a read-only orbit opens with, naming the ``owner``."""
    return (
        f"Read-only: {owner.described} owns this repository. Tasks you run are queued until this"
        " orbit owns it."
    )


def queued_dropped_note(count: int) -> str:
    """The note saying a switch of sessions dropped the ``count`` commands queued while
    read-only, which have to be asked for again."""
    if count == 1:
        return "Switched sessions: 1 queued command was dropped. Ask for it again in this session."
    return (
        f"Switched sessions: {count} queued commands were dropped. Ask for them again in this"
        " session."
    )
