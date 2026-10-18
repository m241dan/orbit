"""ctrl+g and ctrl+e: the keys that fire triggers, labelled in the footer from the catalog."""

from textual.pilot import Pilot
from textual.widgets._footer import FooterKey

from orbit.protocol import (
    CONFIGURE_CHECKS,
    SUPER_REVIEW,
    Catalog,
    FireTrigger,
    TriggerEntry,
)
from orbit.tui import OrbitApp
from orbit.tui.view import TRIGGER_KEYS, trigger_key_labels
from orbit.tui.widgets import ConfirmScreen
from tests.test_tui import FakeSession, feed_events

SUPER = TriggerEntry(name=SUPER_REVIEW, label="Super review", manual=True)
CONFIGURE = TriggerEntry(name=CONFIGURE_CHECKS, label="Set up checks", manual=True)


def catalog_of(*triggers: TriggerEntry) -> Catalog:
    return Catalog(workspaces=(), agents=(), triggers=triggers, lanes=())


def footer_labels(app: OrbitApp) -> list[str]:
    return [key.description for key in app.query(FooterKey)]


async def settled(pilot: Pilot[None]) -> None:
    """Let the footer redraw after the bindings change."""
    await pilot.pause()
    await pilot.pause()


def test_each_key_takes_its_triggers_label() -> None:
    renamed = SUPER.model_copy(update={"label": "Whole review"})
    labels = trigger_key_labels((renamed, CONFIGURE))
    assert labels == {"ctrl+g": "Whole review", "ctrl+e": "Set up checks"}


def test_a_key_whose_trigger_is_missing_has_no_label() -> None:
    assert trigger_key_labels((SUPER,)) == {"ctrl+g": "Super review", "ctrl+e": None}


def test_the_keys_fire_the_built_in_triggers() -> None:
    assert dict(TRIGGER_KEYS) == {"ctrl+g": SUPER_REVIEW, "ctrl+e": CONFIGURE_CHECKS}


async def test_before_any_catalog_the_keys_show_their_own_labels() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(300, 30)) as pilot:
        await settled(pilot)
        labels = footer_labels(app)
        assert "Super review" in labels and "Set up checks" in labels


async def test_a_catalog_relabels_a_trigger_key() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    renamed = SUPER.model_copy(update={"label": "Whole review"})
    async with app.run_test(size=(300, 30)) as pilot:
        await feed_events(session, pilot, catalog_of(renamed, CONFIGURE))
        await settled(pilot)
        labels = footer_labels(app)
        assert "Whole review" in labels and "Super review" not in labels
        await pilot.press("ctrl+g")
        assert session.commands == [FireTrigger(trigger=SUPER_REVIEW)]


async def test_a_catalog_without_a_trigger_hides_its_key_and_the_key_does_nothing() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(300, 30)) as pilot:
        await feed_events(session, pilot, catalog_of(SUPER))
        await settled(pilot)
        labels = footer_labels(app)
        assert "Super review" in labels and "Set up checks" not in labels
        await pilot.press("ctrl+e")
        assert not isinstance(app.screen, ConfirmScreen)
        assert session.commands == []


async def test_a_trigger_back_in_a_later_catalog_shows_its_key_again() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(300, 30)) as pilot:
        await feed_events(session, pilot, catalog_of(SUPER))
        await feed_events(session, pilot, catalog_of(SUPER, CONFIGURE))
        await settled(pilot)
        assert "Set up checks" in footer_labels(app)
        await pilot.press("ctrl+e")
        assert isinstance(app.screen, ConfirmScreen)
