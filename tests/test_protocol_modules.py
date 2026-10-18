from types import ModuleType
from typing import Any, get_args

import orbit.protocol
from orbit.protocol import Command, Event, commands, events
from orbit.protocol.models import _Message


def _messages(module: ModuleType) -> set[type[_Message]]:
    """The message classes ``module`` defines that have a ``type`` field."""
    return {
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, _Message)
        and value.__module__ == module.__name__
        and "type" in value.model_fields
    }


def _members(union: Any) -> set[type[_Message]]:
    """The message classes in the discriminated ``union``."""
    return set(get_args(get_args(union)[0]))


def test_every_public_name_resolves() -> None:
    for name in orbit.protocol.__all__:
        assert hasattr(orbit.protocol, name), name


def test_command_covers_every_command_class() -> None:
    assert _members(Command) == _messages(commands)


def test_event_covers_every_event_class() -> None:
    assert _members(Event) == _messages(events)
