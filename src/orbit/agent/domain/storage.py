"""How much of a session's events the store keeps: the user's storage settings, and the long text
fields cut from the copies it stores."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orbit.agent.domain.config_errors import ConfigError, validation_problems
from orbit.protocol import Event


class StorageSettings(BaseModel):
    """The user config's ``[storage]``: ``tool_output_kb``, the longest text field a stored event
    keeps whole, and ``session_log_mb``, the most of a session's log kept; 0 means unlimited."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    tool_output_kb: int = Field(default=64, ge=0)
    session_log_mb: int = Field(default=200, ge=0)

    @property
    def tool_output_bytes(self) -> int:
        return self.tool_output_kb * 1024

    @property
    def session_log_bytes(self) -> int:
        return self.session_log_mb * 1024 * 1024


def trim_target(limit_bytes: int) -> int:
    """The size, in bytes, a session's log over ``limit_bytes`` is trimmed to be under: 90% of
    it, so that a log at its limit isn't trimmed again with every event."""
    return limit_bytes * 9 // 10


class _UserConfig(BaseModel):
    """The parts of the user config orbit reads; the rest is left for later orbits."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    storage: StorageSettings = StorageSettings()


def load_storage(data: dict[str, Any]) -> StorageSettings:
    """The storage settings in ``data``, a user config file's contents, over the defaults; its
    tables but ``[storage]`` are ignored.

    Raises :class:`ConfigError` naming the key if ``[storage]`` is invalid.
    """
    try:
        return _UserConfig.model_validate(data).storage
    except ValidationError as exc:
        raise ConfigError(validation_problems(exc)) from None


def cut_fields(event: Event, limit_bytes: int) -> Event:
    """``event`` with every string in it longer than ``limit_bytes`` (UTF-8), however deeply
    nested, cut to its first and last half of the limit around a marker naming the size cut;
    ``event`` itself if ``limit_bytes`` is 0."""
    if limit_bytes == 0:
        return event
    cut: Event = _cut(event, limit_bytes)
    return cut


def _cut(value: Any, limit: int) -> Any:
    """``value`` with the strings in it cut as :func:`cut_fields` says."""
    if isinstance(value, str):
        return _cut_text(value, limit)
    if isinstance(value, BaseModel):
        fields = {name: _cut(getattr(value, name), limit) for name in type(value).model_fields}
        return value.model_copy(update=fields)
    return _cut_items(value, limit)


def _cut_items(value: Any, limit: int) -> Any:
    """``value``, if it's a dict, list or tuple, with the strings in its items cut as
    :func:`cut_fields` says; anything else as it is."""
    if isinstance(value, dict):
        return {key: _cut(item, limit) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_cut(item, limit) for item in value)
    return value


def _cut_text(text: str, limit: int) -> str:
    """``text`` whole if it's no longer than ``limit`` bytes, else its first and last half of
    the limit around a marker naming how much was cut."""
    encoded = text.encode()
    if len(encoded) <= limit:
        return text
    half = limit // 2
    # A character split at either end is dropped rather than garbled.
    head = encoded[:half].decode(errors="ignore")
    tail = encoded[len(encoded) - half :].decode(errors="ignore")
    return f"{head}\n… {_size(len(encoded) - 2 * half)} cut by orbit's tool_output_kb …\n{tail}"


def _size(count: int) -> str:
    """``count`` bytes, readably."""
    if count < 1024:
        return f"{count} bytes"
    if count < 1024 * 1024:
        return f"{round(count / 1024)} KB"
    return f"{round(count / (1024 * 1024))} MB"
