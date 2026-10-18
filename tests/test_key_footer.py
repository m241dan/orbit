from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Label
from textual.widgets._footer import FooterKey

from orbit.tui.widgets import KeyFooter


class Plain(Widget, can_focus=True):
    pass


class Saving(Widget, can_focus=True):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("ctrl+s", "save", "Save")]


class Hiding(Widget, can_focus=True):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("x", "remove", show=False)]


class Paging(Widget, can_focus=True):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("n", "next", "Next"),
        Binding("p", "previous", "Previous"),
    ]


class Backwards(Widget, can_focus=True):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("p", "previous", "Previous"),
        Binding("n", "next", "Next"),
    ]


MOVES = Binding.Group("Move")


class Grouped(Widget, can_focus=True):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "up", "Up", group=MOVES),
        Binding("down", "down", "Down", group=MOVES),
    ]


class FooterApp(App[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+d", "danger", "Danger"),
    ]

    def __init__(self, compact: bool = False) -> None:
        super().__init__()
        self.compact = compact
        self.locked = False

    def compose(self) -> ComposeResult:
        yield Plain(id="first")
        yield Plain(id="second")
        yield Saving(id="saving")
        yield Hiding(id="hiding")
        yield Paging(id="paging")
        yield Backwards(id="backwards")
        yield Grouped(id="grouped")
        yield KeyFooter(compact=self.compact)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Locked, the danger key shows disabled.
        return None if action == "danger" and self.locked else True


class QuietApp(FooterApp):
    """Without the command palette, so the footer changes its keys one by one."""

    ENABLE_COMMAND_PALETTE = False


def keys(app: FooterApp) -> list[FooterKey]:
    return list(app.query(FooterKey))


def descriptions(app: FooterApp) -> list[str]:
    return [key.description for key in keys(app)]


async def focus(pilot: Pilot[None], widget_id: str) -> None:
    pilot.app.query_one(f"#{widget_id}").focus()
    await pilot.pause()


async def test_the_footer_shows_the_active_keys() -> None:
    app = FooterApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert descriptions(app) == ["Quit", "Danger", "palette"]


async def test_the_footer_keeps_its_keys_when_focus_brings_the_same_ones() -> None:
    app = FooterApp()
    async with app.run_test() as pilot:
        app.query_one("#first").focus()
        await pilot.pause()
        before = keys(app)

        app.query_one("#second").focus()
        await pilot.pause()

        assert keys(app) == before


async def test_the_footer_redraws_when_focus_brings_other_keys() -> None:
    app = FooterApp()
    async with app.run_test() as pilot:
        app.query_one("#saving").focus()
        await pilot.pause()

        assert descriptions(app) == ["Save", "Quit", "Danger", "palette"]


async def test_the_footer_keeps_its_keys_when_focus_brings_only_hidden_ones() -> None:
    app = FooterApp()
    async with app.run_test() as pilot:
        app.query_one("#first").focus()
        await pilot.pause()
        before = keys(app)

        app.query_one("#hiding").focus()
        await pilot.pause()

        assert keys(app) == before


async def test_without_the_palette_a_key_that_comes_is_mounted_in_its_place() -> None:
    app = QuietApp()
    async with app.run_test() as pilot:
        await focus(pilot, "first")
        quit_key, danger_key = keys(app)

        await focus(pilot, "saving")

        assert descriptions(app) == ["Save", "Quit", "Danger"]
        assert keys(app)[1:] == [quit_key, danger_key]


async def test_without_the_palette_a_key_that_goes_is_removed_alone() -> None:
    app = QuietApp()
    async with app.run_test() as pilot:
        await focus(pilot, "saving")
        _, quit_key, danger_key = keys(app)

        await focus(pilot, "first")

        assert keys(app) == [quit_key, danger_key]


async def test_a_key_that_changes_state_is_replaced_alone() -> None:
    app = QuietApp()
    async with app.run_test() as pilot:
        await focus(pilot, "first")
        quit_key, danger_key = keys(app)

        app.locked = True
        app.refresh_bindings()
        await pilot.pause()

        assert descriptions(app) == ["Quit", "Danger"]
        assert keys(app)[0] is quit_key
        assert keys(app)[1] is not danger_key
        assert keys(app)[1].has_class("-disabled")


async def test_a_key_mounted_alone_is_styled_like_the_footers_own() -> None:
    for compact in (False, True):
        app = QuietApp(compact=compact)
        async with app.run_test() as pilot:
            await focus(pilot, "saving")

            save_key, quit_key, _ = keys(app)
            assert save_key.classes == quit_key.classes
            assert save_key.has_class("-compact") is compact
            # The same width for the same width of key and description.
            assert save_key.region.width == quit_key.region.width


async def test_keys_that_change_order_are_rebuilt_in_their_new_order() -> None:
    app = QuietApp()
    async with app.run_test() as pilot:
        await focus(pilot, "paging")
        assert descriptions(app) == ["Next", "Previous", "Quit", "Danger"]

        await focus(pilot, "backwards")

        assert descriptions(app) == ["Previous", "Next", "Quit", "Danger"]


async def test_grouped_keys_are_shown_under_their_group() -> None:
    app = QuietApp()
    async with app.run_test() as pilot:
        await focus(pilot, "first")
        await focus(pilot, "grouped")

        assert descriptions(app) == ["", "", "Quit", "Danger"]
        assert [label.content for label in app.query(Label)] == ["Move"]

        await focus(pilot, "saving")

        assert descriptions(app) == ["Save", "Quit", "Danger"]
