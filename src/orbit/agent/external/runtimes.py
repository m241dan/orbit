"""What each runtime this version of orbit knows can do and the models it takes, as its launcher
declares them and the package's ``runtimes.toml`` and the user config give its aliases: what the
roles are held to as they're loaded."""

from pathlib import Path

from orbit.agent.domain.runtimes import (
    CLAUDE,
    KIRO,
    Capabilities,
    FullIdRule,
    RuntimeRules,
    load_model_tables,
    model_aliases,
)
from orbit.agent.external.built_in_roles import ROLE_DIRECTORY
from orbit.agent.external.claude_cli import ClaudeCliRunner
from orbit.agent.external.kiro_cli import KiroCliRunner
from orbit.agent.external.toml_files import read_toml
from orbit.agent.external.user_config import read_user_models

RUNTIME_CAPABILITIES: dict[str, Capabilities] = {
    CLAUDE: ClaudeCliRunner.capabilities,
    KIRO: KiroCliRunner.capabilities,
}
"""What each runtime this version of orbit knows can do, by name, in order."""

FULL_MODEL_IDS: dict[str, FullIdRule] = {
    CLAUDE: ClaudeCliRunner.full_model_ids,
    KIRO: KiroCliRunner.full_model_ids,
}
"""How each runtime this version of orbit knows tells a full model id, by name, in order."""

RUNTIMES_FILE = ROLE_DIRECTORY / "runtimes.toml"
"""The aliases of the models each runtime takes, shipped with orbit beside the built-in
roles."""


def read_package_models() -> dict[str, dict[str, str]]:
    """The aliases the package's ``runtimes.toml`` gives each runtime, by name, each with the id
    it resolves to, in order.

    Raises :class:`ConfigError`, naming the file, if it can't be read or is invalid.
    """
    return read_toml(RUNTIMES_FILE, load_model_tables)


def read_runtime_rules(config_dir: Path) -> RuntimeRules:
    """What each runtime this version of orbit knows can do, and the models it takes: the
    package's aliases, with those the user config in ``config_dir`` gives added or in their
    place, and its full ids.

    Raises :class:`ConfigError`, naming the file, if the package's ``runtimes.toml`` or the
    user config can't be read or is invalid, as the user config is when it gives aliases to a
    runtime this version of orbit doesn't know.
    """
    models = model_aliases(read_package_models(), read_user_models(config_dir), FULL_MODEL_IDS)
    return RuntimeRules(dict(RUNTIME_CAPABILITIES), models)
