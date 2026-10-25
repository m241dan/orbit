"""The settings of the session open, by conversational role: those given at startup, those a
session opened keeps as given, and the models they may choose from."""

from collections.abc import Callable, Collection, Mapping

from orbit.agent.domain.runtimes import ModelAliases
from orbit.agent.domain.sessions import resumed_settings
from orbit.protocol import PLANNER, Settings, SettingsChanged

ModelsOf = Callable[[str], ModelAliases | None]
"""The models a conversational role's settings may choose from, by its name: those its runtime
takes, if known."""


def _no_models(role: str) -> ModelAliases | None:
    """No models, for any role."""
    return None


class SessionSettings:
    """The settings of the session open, by conversational role, each at first ``given``; a
    session opened has its saved settings, but for those ``pinned``, by field name, which keep
    the given ones. Each role's models are chosen from those its runtime takes, as ``models``
    says. The planner's are the session's current ones."""

    def __init__(
        self, given: Settings, pinned: Collection[str] = (), models: ModelsOf = _no_models
    ) -> None:
        self._given = given
        self._pinned = tuple(pinned)
        self._models = models
        self._roles = {PLANNER: given}

    @property
    def given(self) -> Settings:
        """The settings given at startup."""
        return self._given

    @property
    def pinned(self) -> tuple[str, ...]:
        """The settings, by field name, that a session opened keeps as given."""
        return self._pinned

    def choices(self, role: str = PLANNER) -> tuple[tuple[str, ...], dict[str, str]]:
        """The models role ``role``'s settings may choose from, its runtime's aliases, in order,
        and the id each resolves to, by alias; none if they aren't known."""
        models = self._models(role)
        if models is None:
            return (), {}
        return tuple(models.aliases), dict(models.aliases)

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
        ones, with the models they may choose from and the id each resolves to."""
        choices, ids = self.choices(role)
        return SettingsChanged(
            role=role, settings=self.of(role), model_choices=choices, model_ids=ids
        )
