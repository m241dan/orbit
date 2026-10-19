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


def test_a_later_command_of_the_same_kind_takes_the_earlier_ones_place() -> None:
    log: list[str] = []
    outcomes = awaiting(Outcomes(), "c1", "history", noted(log, "first"))
    outcomes = awaiting(outcomes, "c2", "history", noted(log, "second"))
    assert settled(outcomes, "c1")[1] is None
    _, awaited = settled(outcomes, "c2")
    assert awaited is not None
    awaited.accepted()
    assert log == ["second accepted"]


def test_commands_of_other_kinds_are_awaited_side_by_side() -> None:
    outcomes = awaiting(Outcomes(), "c1", "history", Awaited())
    outcomes = awaiting(outcomes, "c2", "send_message", Awaited())
    assert settled(outcomes, "c1")[1] is not None
    assert settled(outcomes, "c2")[1] is not None


def test_by_default_an_outcome_does_nothing() -> None:
    awaited = Awaited()
    awaited.accepted()
    awaited.refused()
