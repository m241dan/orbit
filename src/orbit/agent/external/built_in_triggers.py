"""The triggers shipped with orbit, described by the ``triggers.toml`` in ``triggers/`` and told the
templates beside it that it names."""

from importlib.resources import files
from importlib.resources.abc import Traversable

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.roles import names_file
from orbit.agent.domain.triggers import Trigger, check_triggers, load_built_in_triggers
from orbit.agent.external.role_instructions import resolve_instructions
from orbit.agent.external.toml_files import read_toml
from orbit.protocol import BUILT_IN_ROLE_NAMES

TRIGGER_DIRECTORY = files(__package__) / "triggers"
"""The shipped triggers' directory in the package: their ``triggers.toml`` and templates."""

TRIGGERS_FILE = "triggers.toml"
"""The file in :data:`TRIGGER_DIRECTORY` that describes the shipped triggers."""


def read_built_in_triggers(directory: Traversable = TRIGGER_DIRECTORY) -> dict[str, Trigger]:
    """The triggers ``directory``'s ``triggers.toml`` describes, in order, each prompt naming a
    Markdown file there being that file's text, checked against the built-in roles (see
    :func:`~orbit.agent.domain.triggers.check_triggers`).

    Raises :class:`ConfigError`, naming the file, if ``triggers.toml`` or a template it names
    can't be read or is invalid.
    """
    path = directory / TRIGGERS_FILE
    triggers = read_toml(path, load_built_in_triggers)
    resolved = {name: _with_template(trigger, directory) for name, trigger in triggers.items()}
    try:
        check_triggers(resolved, BUILT_IN_ROLE_NAMES)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    return resolved


def read_template_files(directory: Traversable = TRIGGER_DIRECTORY) -> dict[str, str]:
    """The template file each shipped trigger's prompt is in, by trigger, as ``directory``'s
    ``triggers.toml`` names it, for those whose prompt names one.

    Raises :class:`ConfigError`, naming the file, if ``triggers.toml`` can't be read or is
    invalid.
    """
    triggers = read_toml(directory / TRIGGERS_FILE, load_built_in_triggers)
    return {
        name: trigger.prompt
        for name, trigger in triggers.items()
        if trigger.prompt is not None and names_file(trigger.prompt)
    }


def _with_template(trigger: Trigger, directory: Traversable) -> Trigger:
    """``trigger`` with its prompt, if it names a Markdown file in ``directory``, as that file's
    text.

    Raises :class:`ConfigError`, naming the file, if it can't be read.
    """
    if trigger.prompt is None:
        return trigger
    return trigger.model_copy(update={"prompt": resolve_instructions(trigger.prompt, directory)})
