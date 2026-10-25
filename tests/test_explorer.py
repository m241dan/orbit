"""The explorer over the server's directories, without the screen showing it: listing, moving,
adding and opening workspaces, completing paths, and what it makes of each answer."""

from collections.abc import Sequence

from orbit.protocol import (
    AddWorkspace,
    DirectoryEntry,
    DirectoryListing,
    ListDirectory,
)
from orbit.tui.view.explorer import Explorer
from tests.desk_fakes import Backend

REPO_A = DirectoryEntry(name="a", is_repository=True, hidden=False)
SERVED_C = DirectoryEntry(name="c", is_repository=True, workspace="B", hidden=False)
PLAIN = DirectoryEntry(name="docs", is_repository=False, hidden=False)
LINK = DirectoryEntry(name="link", is_repository=False, hidden=False, target="/opt/x")
DOT = DirectoryEntry(name=".cache", is_repository=False, hidden=True)
SRV = DirectoryListing(
    path="/srv", parent="/", entries=(REPO_A, SERVED_C, DOT, PLAIN, LINK), total=5
)
SRV_A = DirectoryListing(path="/srv/a", parent="/srv", entries=(PLAIN,), total=1)
OPT_X = DirectoryListing(path="/opt/x", parent="/opt", entries=(), total=0)
SRV_ROWS = ["⎇ a", "⎇ c (served)", "docs", "link → /opt/x"]
ORBIT = DirectoryEntry(name="orbit", is_repository=True, hidden=False)
ORCHARD = DirectoryEntry(name="orchard", is_repository=False, hidden=False)
SRC = DirectoryListing(path="/src", parent="/", entries=(ORBIT, ORCHARD, DOT), total=3)
PROJECTS = DirectoryEntry(name="projects", is_repository=False, hidden=False)
WORK = DirectoryListing(path="/work", parent="/", entries=(PROJECTS,), total=1)
ROOT = DirectoryListing(
    path="/", entries=(DirectoryEntry(name="srv", is_repository=False, hidden=False),), total=1
)
REAL = DirectoryListing(path="/opt/real", parent="/opt", entries=(PROJECTS,), total=1)
DENIED = "Permission denied: /srv/a"


class View:
    """What the explorer shows, as it shows it, and what the user has done in it: the row
    highlighted and the text in the field, set as a test types."""

    def __init__(self) -> None:
        self.is_attached = True
        self.is_active = True
        self.highlighted: int | None = None
        self.field = ""
        self.title = ""
        self.more: str | None = None
        self.keys: list[str] = []
        self.rows: list[str] = []
        self.note: str | None = None
        self.field_note = ""
        self.candidates: tuple[str, ...] = ()
        self.dismissed: list[str | AddWorkspace | None] = []

    def show_heading(self, title: str, more: str | None) -> None:
        self.title, self.more = title, more

    def show_rows(self, keys: Sequence[str], labels: Sequence[str], highlight: int | None) -> None:
        self.keys, self.rows, self.highlighted = list(keys), list(labels), highlight

    def show_note(self, note: str | None) -> None:
        self.note = note

    def set_field(self, text: str) -> None:
        self.field = text

    def show_field_note(self, note: str) -> None:
        self.field_note = note

    def show_candidates(self, candidates: Sequence[str]) -> None:
        self.candidates = tuple(candidates)

    def dismiss(self, result: str | AddWorkspace | None) -> None:
        self.dismissed.append(result)


async def opened(*listings: DirectoryListing) -> tuple[Explorer, View, Backend]:
    """An explorer started in a view, sent ``listings`` by the server."""
    view, backend = View(), Backend()
    explorer = Explorer(view, backend.desk.send)
    await explorer.start()
    for listing in listings:
        explorer.show_listing(listing)
    return explorer, view, backend


async def test_starting_lists_the_starting_directory_with_marks_hiding_hidden_ones() -> None:
    _, view, backend = await opened(SRV)
    assert backend.commands == [ListDirectory(path=None)]
    assert (view.title, view.rows, view.highlighted, view.more) == ("/srv", SRV_ROWS, 0, None)
    assert view.keys == ["/srv/a", "/srv/c", "/srv/docs", "/srv/link"]


async def test_hidden_directories_show_and_hide_again_keeping_the_highlight() -> None:
    explorer, view, backend = await opened(SRV)
    view.highlighted = 2
    explorer.toggle_hidden()
    assert view.rows == ["⎇ a", "⎇ c (served)", ".cache", "docs", "link → /opt/x"]
    assert view.highlighted == 3
    view.highlighted = 2
    explorer.toggle_hidden()
    assert (view.rows, view.highlighted) == (SRV_ROWS, 2)
    assert backend.commands == [ListDirectory(path=None)]


async def test_showing_hidden_ones_of_nothing_shown_highlights_the_first() -> None:
    explorer, view, _ = await opened(DirectoryListing(path="/srv", entries=(DOT,), total=1))
    assert view.highlighted is None
    explorer.toggle_hidden()
    assert view.highlighted == 0


async def test_into_lists_the_directory_highlighted_ignoring_a_late_listing() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.into()
    assert backend.commands[-1] == ListDirectory(path="/srv/a")
    explorer.show_listing(SRV)
    assert view.rows == SRV_ROWS
    explorer.show_listing(SRV_A)
    assert (view.title, view.rows) == ("/srv/a", ["docs"])
    backend.accept()
    assert view.rows == ["docs"]


async def test_up_lists_the_parent_and_nothing_at_the_top() -> None:
    explorer, _, backend = await opened(SRV_A)
    await explorer.up()
    assert backend.commands[-1] == ListDirectory(path="/srv")
    explorer.show_listing(DirectoryListing(path="/srv", entries=(), total=0))
    await explorer.up()
    await explorer.into()
    await explorer.add()
    assert backend.commands[-1] == ListDirectory(path="/srv")


async def test_keys_before_a_listing_send_nothing() -> None:
    explorer, view, backend = await opened()
    await explorer.up()
    await explorer.into()
    await explorer.add()
    explorer.toggle_hidden()
    assert backend.commands == [ListDirectory(path=None)]
    assert view.title == ""


async def test_choosing_lists_a_directory_and_a_symlinks_listing_shows_once_accepted() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.choose(3)
    assert backend.commands[-1] == ListDirectory(path="/srv/link")
    explorer.show_listing(OPT_X)
    assert view.rows == SRV_ROWS
    backend.accept()
    assert (view.title, view.rows) == ("/opt/x", [])
    explorer.show_listing(SRV)
    assert view.rows == []


async def test_choosing_a_served_directory_opens_its_workspace() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.choose(1)
    assert view.dismissed == ["B"]
    assert backend.commands == [ListDirectory(path=None)]


async def test_adding_the_directory_highlighted_closes_with_it_once_accepted() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.add()
    assert backend.commands[-1] == AddWorkspace(path="/srv/a")
    assert view.dismissed == []
    backend.accept()
    assert view.dismissed == [AddWorkspace(path="/srv/a")]


async def test_adding_a_symlink_adds_its_target() -> None:
    explorer, view, backend = await opened(SRV)
    view.highlighted = 3
    await explorer.add()
    assert backend.commands[-1] == AddWorkspace(path="/opt/x")
    backend.accept()
    assert view.dismissed == [AddWorkspace(path="/opt/x")]


async def test_an_add_accepted_under_another_screen_closes_once_back_on_top() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.add()
    view.is_active = False
    backend.accept()
    explorer.resumed()
    assert view.dismissed == []
    view.is_active = True
    explorer.resumed()
    explorer.resumed()
    assert view.dismissed == [AddWorkspace(path="/srv/a")]


async def test_a_refused_listing_shows_why_under_the_listing_it_had() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.into()
    backend.refuse(DENIED)
    assert (view.note, view.rows) == (DENIED, SRV_ROWS)
    explorer.show_listing(SRV_A)
    assert view.rows == SRV_ROWS
    await explorer.into()
    assert view.note is None


async def test_a_refused_add_shows_why_and_keeps_a_listing_asked_for() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.into()
    await explorer.add()
    backend.refuse("/srv/docs is outside the browsable root /srv/a.")
    assert view.note == "/srv/docs is outside the browsable root /srv/a."
    explorer.show_listing(SRV_A)
    assert view.rows == ["docs"]


async def test_a_truncated_listing_says_how_many_are_left_out() -> None:
    truncated = DirectoryListing(path="/srv", entries=(PLAIN,), truncated=True, total=2400)
    explorer, view, _ = await opened(truncated)
    assert view.more == "… 2399 more not shown: type the path in the field"
    await explorer.into()
    explorer.show_listing(DirectoryListing(path="/srv/docs", entries=(), total=0))
    assert view.more is None


async def test_an_add_answered_once_the_explorer_closed_changes_nothing() -> None:
    for refused in (None, DENIED):
        explorer, view, backend = await opened(SRV)
        await explorer.add()
        view.is_attached = False
        if refused is None:
            backend.accept()
        else:
            backend.refuse(refused)
        assert (view.dismissed, view.note, backend.shown) == ([], None, [])


async def test_a_held_listing_accepted_once_the_explorer_closed_shows_nothing() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.choose(3)
    explorer.show_listing(OPT_X)
    view.is_attached = False
    backend.accept()
    assert (view.title, view.rows) == ("/srv", SRV_ROWS)


async def completing(explorer: Explorer, view: View, typed: str) -> None:
    """Type ``typed`` in the field and complete it."""
    view.field = typed
    await explorer.complete()


async def test_completing_one_match_from_the_listing_adds_a_slash() -> None:
    explorer, view, backend = await opened(SRC)
    await completing(explorer, view, "/src/orb")
    assert (view.field, view.candidates) == ("/src/orbit/", ())
    assert backend.commands == [ListDirectory(path=None)]


async def test_completing_several_matches_takes_their_prefix_and_shows_them() -> None:
    explorer, view, _ = await opened(SRC)
    await completing(explorer, view, "/src/or")
    assert (view.field, view.candidates) == ("/src/or", ("orbit", "orchard"))
    await completing(explorer, view, "/src/orc")
    assert (view.field, view.candidates) == ("/src/orchard/", ())


async def test_completing_with_no_match_changes_nothing() -> None:
    explorer, view, _ = await opened(SRC)
    await completing(explorer, view, "/src/x")
    assert (view.field, view.candidates) == ("/src/x", ())


async def test_hidden_directories_complete_only_while_shown() -> None:
    explorer, view, _ = await opened(SRC)
    await completing(explorer, view, "/src/.c")
    assert view.field == "/src/.c"
    explorer.toggle_hidden()
    await explorer.complete()
    assert view.field == "/src/.cache/"


async def test_completing_elsewhere_lists_the_parent_alone() -> None:
    explorer, view, backend = await opened(SRC)
    await completing(explorer, view, "/work/pro")
    assert backend.commands[-1] == ListDirectory(path="/work")
    explorer.show_listing(WORK)
    assert view.field == "/work/pro"
    backend.accept()
    assert view.field == "/work/projects/"
    assert (view.title, view.rows) == ("/src", ["⎇ orbit", "orchard"])
    explorer.show_listing(WORK)
    assert view.rows == ["⎇ orbit", "orchard"]
    assert view.field == "/work/projects/"


async def test_a_completion_listing_once_typed_on_completes_nothing() -> None:
    explorer, view, backend = await opened(SRC)
    await completing(explorer, view, "/work/pro")
    view.field = "/srv/a"
    explorer.show_listing(WORK)
    backend.accept()
    assert view.field == "/srv/a"


async def test_completing_without_a_path_does_nothing() -> None:
    explorer, view, backend = await opened(SRC)
    await completing(explorer, view, "src")
    assert view.field == "src"
    assert backend.commands == [ListDirectory(path=None)]


async def test_a_completion_listing_of_the_directory_asked_for_is_also_shown() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.into()
    await completing(explorer, view, "/srv/a/d")
    explorer.show_listing(SRV_A)
    assert view.rows == ["docs"]
    backend.accept()
    assert (view.field, view.rows) == ("/srv/a/docs/", ["docs"])


async def test_a_completion_listing_at_its_resolved_path_completes_the_path_typed() -> None:
    explorer, view, backend = await opened(SRC)
    await completing(explorer, view, "/link/pro")
    assert backend.commands[-1] == ListDirectory(path="/link")
    explorer.show_listing(REAL)
    backend.accept()
    assert view.field == "/link/projects/"
    assert (view.title, view.rows) == ("/src", ["⎇ orbit", "orchard"])


async def test_a_completion_listing_while_a_listing_is_awaited_is_never_shown() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.into()
    listed = len(backend.sent) - 1
    await completing(explorer, view, "/link/pro")
    explorer.show_listing(REAL)
    backend.accept(listed)
    assert view.rows == SRV_ROWS
    backend.accept()
    assert (view.field, view.rows) == ("/link/projects/", SRV_ROWS)
    await explorer.into()
    view.highlighted = 3
    await explorer.choose(3)
    explorer.show_listing(OPT_X)
    backend.accept()
    assert view.title == "/opt/x"


async def test_the_listing_asked_for_while_completing_elsewhere_is_shown_alone() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.into()
    await completing(explorer, view, "/link/d")
    explorer.show_listing(SRV_A)
    assert view.rows == ["docs"]
    backend.accept()
    assert view.field == "/link/d"


async def test_an_earlier_completions_outcome_once_another_is_asked_for_completes_nothing() -> None:
    explorer, view, backend = await opened(SRC)
    await completing(explorer, view, "/work/pro")
    earliest = len(backend.sent) - 1
    await explorer.complete()
    await explorer.complete()
    backend.accept(earliest)
    assert view.field == "/work/pro"
    backend.refuse("Permission denied: /work", index=earliest + 1)
    explorer.show_listing(WORK)
    backend.accept()
    assert view.field == "/work/projects/"


async def test_a_completion_answered_once_the_explorer_closed_changes_nothing() -> None:
    for refused in (None, "Permission denied: /work"):
        explorer, view, backend = await opened(SRC)
        await completing(explorer, view, "/work/pro")
        view.is_attached = False
        if refused is None:
            backend.accept()
        else:
            backend.refuse(refused)
        assert (view.field, view.field_note, view.candidates, backend.shown) == (
            "/work/pro",
            "",
            (),
            [],
        )


async def test_a_completion_listing_accepted_once_the_explorer_closed_changes_nothing() -> None:
    explorer, view, backend = await opened(SRC)
    await completing(explorer, view, "/work/pro")
    explorer.show_listing(WORK)
    view.is_attached = False
    backend.accept()
    assert (view.field, view.candidates, backend.shown) == ("/work/pro", (), [])


async def test_a_refused_completion_listing_shows_in_the_fields_hint() -> None:
    explorer, view, backend = await opened(SRC)
    await completing(explorer, view, "/root/x")
    backend.refuse("Permission denied: /root")
    assert (view.field_note, view.note) == ("Permission denied: /root", None)


async def test_a_refused_completions_hint_is_gone_after_the_next_completion() -> None:
    explorer, view, backend = await opened(SRC)
    await completing(explorer, view, "/root/x")
    backend.refuse("Permission denied: /root")
    await completing(explorer, view, "/src/orb")
    assert (view.field, view.field_note) == ("/src/orbit/", "")


async def shown_candidates() -> tuple[Explorer, View, Backend]:
    """An explorer on ``/src`` completing ``/src/or``, showing its candidates."""
    explorer, view, backend = await opened(SRC)
    await completing(explorer, view, "/src/or")
    assert view.candidates == ("orbit", "orchard")
    return explorer, view, backend


async def test_candidates_are_hidden_after_a_completion_with_no_match() -> None:
    explorer, view, _ = await shown_candidates()
    await completing(explorer, view, "/src/x")
    assert view.candidates == ()


async def test_candidates_are_hidden_after_a_refused_completion() -> None:
    explorer, view, backend = await shown_candidates()
    await completing(explorer, view, "/work/x")
    backend.refuse("Permission denied: /work")
    assert view.candidates == ()


async def test_candidates_are_hidden_once_typed_on_into_another_directory() -> None:
    explorer, view, backend = await shown_candidates()
    await completing(explorer, view, "/work/pro")
    view.field = "/srv/a"
    explorer.show_listing(WORK)
    backend.accept()
    assert view.candidates == ()


async def test_candidates_are_hidden_after_submitting_the_field() -> None:
    explorer, view, _ = await shown_candidates()
    await explorer.submit(view.field)
    assert view.candidates == ()


async def test_a_refused_completion_leaves_nothing_to_complete_from() -> None:
    explorer, view, backend = await opened(SRC)
    await completing(explorer, view, "/work/pro")
    backend.refuse("Permission denied: /work")
    await explorer.into()
    explorer.show_listing(WORK)
    backend.accept()
    assert (view.field, view.title) == ("/work/pro", "/work")


async def test_submitting_a_path_adds_it_closing_with_it_once_accepted() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.submit(" /srv/repo ")
    assert backend.commands[-1] == AddWorkspace(path="/srv/repo")
    backend.accept()
    assert view.dismissed == [AddWorkspace(path="/srv/repo")]


async def test_submitting_a_url_adds_it_as_one() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.submit("https://github.com/o/r")
    assert backend.commands[-1] == AddWorkspace(url="https://github.com/o/r")
    backend.accept()
    assert view.dismissed == [AddWorkspace(url="https://github.com/o/r")]


async def test_a_refused_add_from_the_field_shows_in_its_hint_until_the_next() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.submit("/etc")
    backend.refuse("/etc is outside the browsable root /srv.")
    assert (view.field_note, view.note) == ("/etc is outside the browsable root /srv.", None)
    await explorer.submit("/etc")
    assert view.field_note == ""


async def test_submitting_an_empty_field_adds_nothing() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.submit("  ")
    assert backend.commands == [ListDirectory(path=None)]
    assert view.dismissed == []


async def test_a_field_add_answered_once_the_explorer_closed_changes_nothing() -> None:
    for refused in (None, DENIED):
        explorer, view, backend = await opened(SRV)
        await explorer.submit("/srv/repo")
        view.is_attached = False
        if refused is None:
            backend.accept()
        else:
            backend.refuse(refused)
        assert (view.dismissed, view.field_note, backend.shown) == ([], "", [])


async def test_an_earlier_listings_outcome_once_another_is_asked_for_changes_nothing() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.into()
    earlier = len(backend.sent) - 1
    await explorer.up()
    assert backend.commands[-1] == ListDirectory(path="/")
    explorer.show_listing(SRV_A)
    backend.accept(earlier)
    assert view.rows == SRV_ROWS
    explorer.show_listing(ROOT)
    backend.accept()
    assert (view.title, view.rows) == ("/", ["srv"])


async def test_an_earlier_listings_refusal_once_another_is_asked_for_shows_nothing() -> None:
    explorer, view, backend = await opened(SRV)
    await explorer.into()
    earlier = len(backend.sent) - 1
    await explorer.up()
    backend.refuse(DENIED, index=earlier)
    assert view.note is None
    explorer.show_listing(ROOT)
    assert (view.title, view.rows) == ("/", ["srv"])
