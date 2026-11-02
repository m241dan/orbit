"""Merge holds in the TUI: the task list's title naming the sets holding merges, the rows of work
waiting for them, and ctrl+f, shown only while merges are held, asking before it sends
``ReleaseHold``."""

from textual.widgets import Static

from orbit.protocol import MergeHold, ReleaseHold, SessionStateChanged
from orbit.tui import OrbitApp
from orbit.tui.view.confirm import RELEASE_TITLE, RELEASE_YES, release_message
from orbit.tui.widgets import ConfirmScreen
from tests.frontend_fakes import Shell
from tests.tui_fakes import SNAPSHOT, FakeSession, feed_events

STRESS = MergeHold(set="stress", commit="1a2b3c4", reason="Stress tests failed")
DONE = SNAPSHOT.tasks[1].model_copy(update={"status": "completed"})
HELD = SNAPSHOT.model_copy(
    update={
        "tasks": (SNAPSHOT.tasks[0], DONE),
        "merge_holds": (STRESS,),
        "waiting_to_merge": ("2",),
    }
)


def state(session: str = "s1", holds: tuple[MergeHold, ...] = ()) -> SessionStateChanged:
    """The state of ``session``, idle and open, with ``holds`` on merges and no work waiting."""
    return SessionStateChanged(
        session=session, active=True, state="idle", viewers=1, merge_holds=holds
    )


def test_the_release_wording() -> None:
    assert RELEASE_TITLE == "Release the merge hold?"
    assert RELEASE_YES == "release"
    assert release_message(("stress",)) == "stress hasn't passed."
    assert release_message(("stress", "soak")) == "stress, soak haven't passed."
    assert release_message((), ("bugs-reviewer",)) == "the finding from bugs-reviewer isn't fixed."
    assert release_message(("stress",), ("bugs-reviewer", "security-reviewer")) == (
        "stress hasn't passed and the findings from bugs-reviewer, security-reviewer aren't fixed."
    )


async def test_a_snapshot_holding_merges_titles_the_list_and_marks_the_work_waiting() -> None:
    shell = Shell()
    await shell.feed(HELD)
    assert shell.task_view.title == "Task list · merges held: stress failed"
    assert shell.task_view.rows[1].endswith(" · waiting to merge")
    assert not shell.task_view.rows[0].endswith("waiting to merge")
    assert shell.frontend.merges_held and shell.app.keys_refreshed == 1


async def test_the_session_viewed_no_longer_held_titles_the_list_as_before() -> None:
    shell = Shell()
    await shell.feed(HELD)
    await shell.feed(state("s2"))  # Another session's state leaves the holds shown.
    assert shell.frontend.merges_held
    await shell.feed(state())
    assert shell.task_view.title == "Task list"
    assert not shell.task_view.rows[1].endswith("waiting to merge")
    assert not shell.frontend.merges_held and shell.app.keys_refreshed == 2


async def test_a_state_with_the_same_holds_leaves_the_keys_alone() -> None:
    shell = Shell()
    await shell.feed(HELD, state(holds=(STRESS,)))
    assert shell.app.keys_refreshed == 1


async def test_releasing_asks_naming_the_sets_then_sends_release_hold() -> None:
    shell = Shell()
    await shell.feed(HELD)
    shell.frontend.release_hold()
    title, message, yes, then = shell.app.questions[-1]
    assert (title, message, yes) == ("Release the merge hold?", "stress hasn't passed.", "release")
    assert shell.wire.changes == []
    await then()
    assert shell.wire.changes == [ReleaseHold()]


async def test_releasing_a_findings_hold_asks_naming_the_lens_that_raised_it() -> None:
    shell = Shell()
    finding = MergeHold(
        set="bugs-reviewer", commit="c1", reason="blocking finding", finding="Fix the crash"
    )
    await shell.feed(HELD.model_copy(update={"merge_holds": (finding,)}))
    assert shell.task_view.title == "Task list · merges held: blocking finding"
    shell.frontend.release_hold()
    _, message, _, _ = shell.app.questions[-1]
    assert message == "the finding from bugs-reviewer isn't fixed."


async def test_releasing_with_merges_not_held_asks_nothing() -> None:
    shell = Shell()
    await shell.feed(SNAPSHOT)
    shell.frontend.release_hold()
    assert shell.app.questions == [] and shell.wire.changes == []


async def test_ctrl_f_is_hidden_and_does_nothing_while_merges_are_not_held() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT)
        assert not app.check_action("release_hold", ())
        await pilot.press("ctrl+f")
        assert len(app.screen_stack) == 1
    assert session.commands == []


async def test_ctrl_f_then_y_sends_release_hold_after_asking() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HELD)
        assert app.check_action("release_hold", ())
        await pilot.press("ctrl+f")
        assert isinstance(app.screen, ConfirmScreen)
        body = app.screen.query_one("#confirm-message", Static)
        assert body.border_title == "Release the merge hold?"
        assert body.border_subtitle == "y release · n/esc cancel"
        assert str(body.render()) == "stress hasn't passed."
        assert not app.check_action("release_hold", ())  # Not over its own modal.
        await pilot.press("y")
        assert len(app.screen_stack) == 1
    assert session.commands == [ReleaseHold()]


async def test_ctrl_f_then_n_or_escape_sends_nothing() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HELD)
        for key in ("n", "escape"):
            await pilot.press("ctrl+f", key)
            assert len(app.screen_stack) == 1
    assert session.commands == []
