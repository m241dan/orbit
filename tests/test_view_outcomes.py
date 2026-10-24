from dataclasses import replace

from orbit.tui.view.outcomes import Awaited, Outcomes, awaiting, settled


def noted(log: list[str], text: str) -> Awaited:
    """Awaiting an outcome by adding ``text`` and how it went to ``log``."""
    return Awaited(
        accepted=lambda: log.append(f"{text} accepted"),
        refused=lambda: log.append(f"{text} refused"),
    )


def test_a_command_awaited_is_settled_by_its_id() -> None:
    log: list[str] = []
    outcomes = awaiting(Outcomes(), "c1", "history", noted(log, "page"))
    outcomes, awaited = settled(outcomes, "c1")
    assert awaited is not None
    awaited.refused()
    assert log == ["page refused"]
    assert settled(outcomes, "c1") == (outcomes, None)


def test_an_outcome_awaited_by_nothing_settles_nothing() -> None:
    outcomes = awaiting(Outcomes(), "c1", "history", Awaited())
    assert settled(outcomes, "other") == (outcomes, None)


def superseding(log: list[str], text: str) -> Awaited:
    """Awaiting an outcome as :func:`noted` does, in place of earlier ones of its kind."""
    return replace(noted(log, text), supersedes=True)


def test_a_later_superseding_command_of_the_same_kind_takes_the_earlier_ones_place() -> None:
    log: list[str] = []
    outcomes = awaiting(Outcomes(), "c1", "history", superseding(log, "first"))
    outcomes = awaiting(outcomes, "c2", "history", superseding(log, "second"))
    assert settled(outcomes, "c1")[1] is None
    _, awaited = settled(outcomes, "c2")
    assert awaited is not None
    awaited.accepted()
    assert log == ["second accepted"]


def test_commands_of_the_same_kind_are_each_awaited_unless_superseding() -> None:
    log: list[str] = []
    outcomes = awaiting(Outcomes(), "c1", "decide_proposal", noted(log, "first"))
    outcomes = awaiting(outcomes, "c2", "decide_proposal", noted(log, "second"))
    outcomes, first = settled(outcomes, "c1")
    _, second = settled(outcomes, "c2")
    assert first is not None and second is not None
    first.refused()
    second.accepted()
    assert log == ["first refused", "second accepted"]


def test_a_superseding_command_leaves_the_others_of_its_kind_awaited() -> None:
    outcomes = awaiting(Outcomes(), "c1", "list_directory", Awaited())
    outcomes = awaiting(outcomes, "c2", "list_directory", Awaited(supersedes=True))
    outcomes = awaiting(outcomes, "c3", "list_directory", Awaited(supersedes=True))
    assert settled(outcomes, "c1")[1] is not None
    assert settled(outcomes, "c2")[1] is None


def test_commands_of_other_kinds_are_awaited_side_by_side() -> None:
    outcomes = awaiting(Outcomes(), "c1", "history", Awaited(supersedes=True))
    outcomes = awaiting(outcomes, "c2", "send_message", Awaited(supersedes=True))
    assert settled(outcomes, "c1")[1] is not None
    assert settled(outcomes, "c2")[1] is not None


def test_by_default_an_outcome_does_nothing_and_nothing_takes_why() -> None:
    awaited = Awaited()
    awaited.accepted()
    awaited.refused()
    awaited.code("invalid")
    assert awaited.why is None
