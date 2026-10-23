"""The OpeningHost opening the app on its first catalog: attaching, then sending the opening
command once, or opening a picker."""

from orbit.protocol import (
    Attach,
    Catalog,
    Command,
    Notice,
    RunTasks,
    SessionSummary,
    WorkspaceEntry,
)
from orbit.tui.opening import Hosts, Opening, OpeningHost
from orbit.tui.sessions import StartupPick
from orbit.tui.view.opening import lists_catalog

ATTACH = Attach(workspace="w", session="now")
OPEN_WITH = RunTasks()
CATALOG = Catalog(
    workspaces=(
        WorkspaceEntry(id="w", label="project", sessions=(SessionSummary(id="now", name="today"),)),
    ),
    agents=(),
    triggers=(),
    lanes=(),
)
NOTICE = Notice(message="hello")
OPENING = Opening(command=OPEN_WITH)


class Sent:
    """Records the commands sent; those in ``failing`` fail to send."""

    def __init__(self) -> None:
        self.commands: list[Command] = []
        self.failing: set[str] = set()

    async def __call__(self, command: Command) -> bool:
        self.commands.append(command)
        return command.type not in self.failing


class FakeSessions:
    """A sessions host whose workspaces' active session is ``attach``, which attaching sends
    with ``send``, recording the workspaces it joins and the pickers it opens."""

    def __init__(self, send: Sent, attach: Attach | None = ATTACH) -> None:
        self.attach = attach
        self.send = send
        self.joined: list[str] = []
        self.opened: list[tuple[StartupPick | None, str | None]] = []

    async def attach_active(self) -> bool:
        return self.attach is not None and await self.send(self.attach)

    async def join(self, workspace: str) -> bool:
        self.joined.append(workspace)
        return await self.attach_active()

    def open(self, startup: StartupPick | None = None, workspace: str | None = None) -> None:
        self.opened.append((startup, workspace))


class FakeWorkspaces:
    """A workspaces host recording the pickers it opens."""

    def __init__(self) -> None:
        self.opened: list[bool] = []

    def open(self, startup: bool = False) -> None:
        self.opened.append(startup)


def host(
    opening: Opening = OPENING,
    linked: bool = False,
    attach: Attach | None = ATTACH,
) -> tuple[OpeningHost, Sent, FakeSessions, FakeWorkspaces]:
    sent, workspaces = Sent(), FakeWorkspaces()
    sessions = FakeSessions(sent, attach)
    return (
        OpeningHost(opening, linked, sent, Hosts(sessions, workspaces)),
        sent,
        sessions,
        workspaces,
    )


def test_a_batch_lists_a_catalog_only_if_it_holds_one() -> None:
    assert lists_catalog([NOTICE, CATALOG])
    assert not lists_catalog([NOTICE])


async def test_the_first_catalog_attaches_then_sends_the_opening_command() -> None:
    opener, sent, _, _ = host()
    await opener.listed([NOTICE])
    assert sent.commands == []
    await opener.listed([CATALOG])
    assert sent.commands == [ATTACH, OPEN_WITH]


async def test_a_later_catalog_changes_nothing() -> None:
    opener, sent, sessions, workspaces = host()
    await opener.listed([CATALOG])
    await opener.listed([CATALOG])
    assert sent.commands == [ATTACH, OPEN_WITH]
    assert sessions.opened == [] and workspaces.opened == []


async def test_no_opening_command_is_sent_if_the_attach_fails_to_send() -> None:
    opener, sent, _, _ = host()
    sent.failing.add(ATTACH.type)
    await opener.listed([CATALOG])
    assert sent.commands == [ATTACH]


async def test_nothing_is_sent_without_an_active_session_to_attach_to() -> None:
    opener, sent, _, _ = host(attach=None)
    await opener.listed([CATALOG])
    assert sent.commands == []


async def test_in_process_the_first_catalog_attaches_without_joining_a_workspace() -> None:
    opener, _, sessions, _ = host()
    await opener.listed([CATALOG])
    assert sessions.joined == []


async def test_without_an_opening_command_only_the_attach_is_sent() -> None:
    opener, sent, _, _ = host(Opening())
    await opener.listed([CATALOG])
    assert sent.commands == [ATTACH]


async def test_over_a_link_the_first_catalog_opens_only_the_workspace_picker() -> None:
    opener, sent, sessions, workspaces = host(Opening(command=OPEN_WITH, pick=True), linked=True)
    await opener.listed([CATALOG])
    opener.catalog_shown()
    assert workspaces.opened == [True]
    assert sent.commands == [] and sessions.opened == []


async def test_the_workspace_picked_at_startup_is_attached_to_then_opened_with_once() -> None:
    opener, sent, sessions, _ = host(linked=True)
    await opener.open_workspace("w", startup=True)
    await opener.open_workspace("w", startup=True)
    assert sessions.joined == ["w", "w"]
    assert sent.commands == [ATTACH, OPEN_WITH, ATTACH]


async def test_a_failed_attach_to_the_workspace_picked_sends_no_opening_command() -> None:
    opener, sent, _, _ = host(linked=True)
    sent.failing.add(ATTACH.type)
    await opener.open_workspace("w", startup=True)
    assert sent.commands == [ATTACH]


async def test_a_workspace_picked_at_startup_with_no_session_sends_nothing() -> None:
    opener, sent, _, _ = host(linked=True, attach=None)
    await opener.open_workspace("w", startup=True)
    assert sent.commands == []


async def test_a_workspace_picked_at_startup_when_picking_offers_to_resume() -> None:
    opener, sent, sessions, _ = host(Opening(pick=True), linked=True)
    await opener.open_workspace("w", startup=True)
    assert sessions.opened == [("resume", "w")]
    assert sent.commands == []


async def test_a_workspace_picked_later_opens_its_session_picker() -> None:
    opener, sent, sessions, _ = host(linked=True)
    await opener.open_workspace("w", startup=False)
    assert sessions.opened == [(None, "w")]
    assert sent.commands == []


def test_in_process_the_first_catalog_shown_opens_the_session_picker_once() -> None:
    opener, _, sessions, _ = host(Opening(pick=True))
    opener.catalog_shown()
    opener.catalog_shown()
    assert sessions.opened == [("pick", None)]


def test_without_picking_a_catalog_shown_opens_no_picker() -> None:
    opener, _, sessions, _ = host()
    opener.catalog_shown()
    assert sessions.opened == []
