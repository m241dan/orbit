"""The conversational roles the prompt talks to, without the widgets: which one it talks to on
the tab shown, and each one's lane and settings."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from orbit.protocol import PLANNER, PLANNER_LANE, AgentEntry, BusyLane, Settings


@dataclass(frozen=True)
class Seat:
    """A conversational role the prompt can talk to, by ``name``, shown as ``label``, answering
    in ``lane``."""

    name: str
    label: str
    lane: BusyLane


PLANNER_SEAT = Seat(PLANNER, "Planner", PLANNER_LANE)
"""The planner's seat until a catalog lists it, as the backend always has one."""


def listed_seats(agents: Iterable[AgentEntry]) -> tuple[Seat, ...]:
    """The seats of the conversational roles among ``agents``, in their order but the planner's
    first, which is there even when they don't list it."""
    listed = _conversational(agents)
    planner = next((seat for seat in listed if seat.name == PLANNER), PLANNER_SEAT)
    return (planner, *(seat for seat in listed if seat.name != PLANNER))


def _conversational(agents: Iterable[AgentEntry]) -> list[Seat]:
    """The seats of the conversational roles among ``agents``, in their order."""
    return [Seat(agent.name, agent.label, agent.lane) for agent in agents if agent.conversational]


class SeatsPrompt(Protocol):
    """The prompt, as the seats mark whom it talks to."""

    def show_target(self, role: str, label: str) -> None: ...

    def show_busy(self, busy: bool) -> None: ...

    def show_settings(self, settings: Settings) -> None: ...


class Seats:
    """The conversational roles, shown on ``prompt``: those the latest catalog lists, each one's
    settings as last reported, and the one the prompt talks to, that of the tab shown, or the
    planner on any other tab; ``busy`` says whether a lane is busy."""

    def __init__(self, prompt: SeatsPrompt, busy: Callable[[BusyLane], bool]) -> None:
        self._prompt = prompt
        self._busy = busy
        self.listed: tuple[Seat, ...] = (PLANNER_SEAT,)
        self._settings: dict[str, Settings] = {}
        self._shown: str | None = None  # The key of the tab shown.

    @property
    def target(self) -> Seat:
        """The seat the prompt talks to: the shown tab's role's, if it's conversational, else
        the planner's."""
        return next((seat for seat in self.listed if seat.name == self._shown), self.listed[0])

    @property
    def lanes(self) -> dict[str, BusyLane]:
        """The lane each conversational role answers in, by role."""
        return {seat.name: seat.lane for seat in self.listed}

    @property
    def settings(self) -> Settings | None:
        """The settings of the role the prompt talks to, as :meth:`settings_of` has them."""
        return self.settings_of(self.target.name)

    def settings_of(self, role: str) -> Settings | None:
        """The settings of the conversational ``role`` as last reported: the planner's until it
        has some, and none before any were."""
        return self._settings.get(role, self._settings.get(PLANNER))

    def follow_catalog(self, agents: Iterable[AgentEntry]) -> None:
        """Talk to the conversational roles among the catalog's ``agents``."""
        self.listed = listed_seats(agents)
        self._show()

    def show_tab(self, key: str | None) -> None:
        """Talk to the role of the tab keyed ``key``, now shown, if it's conversational, else
        to the planner."""
        self._shown = key
        self._show()

    def replace_settings(self, settings: Mapping[str, Settings]) -> None:
        """Have the roles' settings be those in ``settings``, by role, as a snapshot reports
        them."""
        self._settings = dict(settings)
        self._show()

    def follow_settings(self, role: str, settings: Settings) -> None:
        """Note the conversational ``role``'s ``settings``, as reported."""
        self._settings[role] = settings
        self._show_settings()

    def follow_busy(self, lane: BusyLane, busy: bool) -> None:
        """Mark the prompt ``busy`` while ``lane`` is, if it's the lane of the role it talks
        to."""
        if lane == self.target.lane:
            self._prompt.show_busy(busy)

    def _show(self) -> None:
        """Mark the prompt with the role it talks to, whether that's busy, and its settings."""
        target = self.target
        self._prompt.show_target(target.name, target.label)
        self._prompt.show_busy(self._busy(target.lane))
        self._show_settings()

    def _show_settings(self) -> None:
        """Show the settings of the role the prompt talks to under it, once there are any."""
        settings = self.settings
        if settings is not None:
            self._prompt.show_settings(settings)
