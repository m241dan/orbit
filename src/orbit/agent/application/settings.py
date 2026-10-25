"""The settings of the session open, by conversational role: those given at startup, those a
session opened keeps as given, and the models they may choose from."""

from collections.abc import Collection, Mapping, Sequence

from orbit.agent.domain.sessions import resumed_settings
from orbit.protocol import PLANNER, Settings, SettingsChanged


class SessionSettings:
    """The settings of the session open, by conversational role, each at first ``given``; a
    session opened has its saved settings, but for those ``pinned``, by field name, which keep
    the given ones. Their models are chosen from ``model_choices``. The planner's are the
    session's current ones."""

    def __init__(
        self,
        given: Settings,
        pinned: Collection[str] = (),
        model_choices: Sequence[str] = (),
    ) -> None:
        self._given = given
        self._pinned = tuple(pinned)
        self._model_choices = tuple(model_choices)
        self._roles = {PLANNER: given}

    @property
    def given(self) -> Settings:
        """The settings given at startup."""
        return self._given

    @property
    def pinned(self) -> tuple[str, ...]:
        """The settings, by field name, that a session opened keeps as given."""
        return self._pinned

    @property
    def model_choices(self) -> tuple[str, ...]:
        """The models the settings may choose from."""
        return self._model_choices

    @property
    def current(self) -> Settings:
        """The planner's settings, which the session shows."""
        return self.of(PLANNER)

    @property
    def roles(self) -> dict[str, Settings]:
        """The settings of each role that has some, by role, to save."""
        return dict(self._roles)

    def of(self, role: str) -> Settings:
        """The settings of the conversational role ``role``: those given, until it has some."""
        return self._roles.get(role, self._given)

    def update(self, settings: Settings, role: str = PLANNER) -> None:
        """Have the conversational role ``role`` use ``settings`` from now on."""
        self._roles[role] = settings

    def resume(self, saved: Mapping[str, Settings]) -> None:
        """Have each role use the settings ``saved`` with the session for it, but for those
        pinned, which are as given; the planner has the given ones if none were saved for it."""
        resumed = {
            role: resumed_settings(s, self._given, self._pinned) for role, s in saved.items()
        }
        self._roles = {PLANNER: self._given, **resumed}

    def changed(self, role: str = PLANNER) -> SettingsChanged:
        """The event saying the conversational role ``role``'s settings are now its current
        ones."""
        choices = self._model_choices
        return SettingsChanged(role=role, settings=self.of(role), model_choices=choices)
