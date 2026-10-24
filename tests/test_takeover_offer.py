"""Following the offer of a freed repository, without the screens showing it: when to ask,
what an answer sends, and asking again when it's refused."""

import pytest

from orbit.protocol import TakeOwnership
from orbit.tui.view.takeover import Answered, TakeoverOffer
from tests.desk_fakes import Backend


class View:
    """What the offer shows: the modals opened, by what takes each one's answer; a test answers
    the one open as the user would, with :meth:`answer`. ``closes`` counts the modals closed
    from the offer."""

    def __init__(self) -> None:
        self.modals: list[Answered] = []
        self.modal_open = False
        self.closes = 0

    def open_modal(self, answered: Answered) -> None:
        assert not self.modal_open
        self.modals.append(answered)
        self.modal_open = True

    def close_modal(self) -> None:
        if self.modal_open:
            self.closes += 1
            self.modal_open = False

    async def answer(self, take: bool | None) -> None:
        """Answer the modal open, taking the repository over or not, or closing it with none."""
        assert self.modal_open
        self.modal_open = False
        await self.modals[-1](take)


def offer() -> tuple[TakeoverOffer, View, Backend]:
    """An offer not yet made, shown in a view, answered through a backend."""
    view, backend = View(), Backend()
    return TakeoverOffer(view, backend.desk.send), view, backend


async def test_an_offer_asks_and_taking_over_sends_the_choice() -> None:
    takeover, view, backend = offer()
    takeover.offered()
    assert view.modal_open and backend.commands == []
    await view.answer(True)
    assert backend.commands == [TakeOwnership(take=True)]
    assert len(view.modals) == 1


@pytest.mark.parametrize("answer", [False, None])
async def test_staying_read_only_sends_the_choice(answer: bool | None) -> None:
    takeover, view, backend = offer()
    takeover.offered()
    await view.answer(answer)
    assert backend.commands == [TakeOwnership(take=False)]


async def test_following_a_snapshot_with_the_offer_open_asks() -> None:
    takeover, view, backend = offer()
    takeover.follow(True)
    assert view.modal_open and takeover.open
    await view.answer(True)
    assert backend.commands == [TakeOwnership(take=True)]


def test_an_offer_already_shown_isnt_asked_again() -> None:
    takeover, view, _ = offer()
    takeover.offered()
    takeover.follow(True)
    takeover.offered()
    assert len(view.modals) == 1


@pytest.mark.parametrize("from_snapshot", [False, True])
async def test_an_offer_settled_elsewhere_closes_the_modal_unanswered(from_snapshot: bool) -> None:
    takeover, view, backend = offer()
    takeover.follow(True)
    if from_snapshot:
        takeover.follow(False)
    else:
        takeover.settled()
    assert (view.modal_open, view.closes) == (False, 1)
    assert not takeover.open
    assert backend.commands == []

    takeover.offered()
    assert view.modal_open and len(view.modals) == 2


@pytest.mark.parametrize("take", [True, False])
async def test_a_refused_answer_asks_again(take: bool) -> None:
    takeover, view, backend = offer()
    takeover.offered()
    await view.answer(take)
    assert not view.modal_open
    backend.refuse("Not now.")
    assert view.modal_open and len(view.modals) == 2
    await view.answer(take)
    assert backend.commands == [TakeOwnership(take=take), TakeOwnership(take=take)]


async def test_an_answer_that_fails_to_send_asks_again() -> None:
    takeover, view, backend = offer()
    takeover.offered()
    backend.send_error = RuntimeError("gone")
    await view.answer(True)
    assert view.modal_open and len(view.modals) == 2


async def test_a_refusal_after_the_offer_settled_doesnt_ask_again() -> None:
    takeover, view, backend = offer()
    takeover.follow(True)
    await view.answer(True)
    takeover.settled()
    backend.refuse("Not now.")
    assert not view.modal_open and len(view.modals) == 1


async def test_an_accepted_answer_leaves_no_modal_open() -> None:
    takeover, view, backend = offer()
    takeover.offered()
    await view.answer(True)
    backend.accept()
    assert not view.modal_open and len(view.modals) == 1
    assert takeover.open  # Until the backend says it's settled.
