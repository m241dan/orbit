"""The settings of the session open: those given at startup, those a session opened keeps as
given, and the models they may choose from."""

from collections.abc import Collection, Sequence

from orbit.agent.domain.sessions import resumed_settings
from orbit.protocol import Settings, SettingsChanged


class SessionSettings:
    """The settings of the session open, at first ``given``; a session opened has its saved
    settings, but for those ``pinned``, by field name, which keep the given ones. Their models
    are chosen from ``model_choices``."""

    def __init__(
        self,
        given: Settings,
        pinned: Collection[str] = (),
        model_choices: Sequence[str] = (),
    ) -> None:
        self._given = given
        self._pinned = tuple(pinned)
        self._model_choices = tuple(model_choices)
        self.current = given

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

    def update(self, settings: Settings) -> None:
        """Have the session use ``settings`` from now on."""
        self.current = settings

    def resume(self, saved: Settings) -> None:
        """Have the session use the settings ``saved`` with it, but for those pinned, which
        are as given."""
        self.current = resumed_settings(saved, self._given, self._pinned)

    def changed(self) -> SettingsChanged:
        """The event saying the settings are now the current ones."""
        return SettingsChanged(settings=self.current, model_choices=self._model_choices)
