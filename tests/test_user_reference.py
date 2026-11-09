"""The user reference config: every setting the user's own ``config.toml`` can change, commented
out at its default with what it does, and examples of the tables it may add, generated from
orbit's models and committed to the package."""

import re
import runpy
import sys
import tomllib
from pathlib import Path

import pytest
from pydantic import BaseModel

from orbit.agent.domain.layers import RoleFields
from orbit.agent.domain.remotes import RemoteSettings
from orbit.agent.domain.server import ServerSettings
from orbit.agent.domain.storage import StorageSettings
from orbit.agent.external.reference_config import REFERENCE_WIDTH
from orbit.agent.external.user_config import (
    USER_CONFIG_FILE,
    USER_REFERENCE,
    read_remotes,
    read_server,
    read_storage,
    read_user_models,
    read_user_roles,
)
from orbit.agent.external.user_reference import render_user_reference
from orbit.protocol import BUILT_IN_ROLE_NAMES
from orbit.telemetry.settings import TelemetrySettings, load_telemetry

MODULE = "orbit.agent.external.user_reference"
KEY_LINE = re.compile(r"# (\[|[a-z_]+ = )")
NAMED_KEY = re.compile(r"# ([a-z_]+)(?: = |: )")
REFERENCE = render_user_reference()
TABLES: dict[str, type[BaseModel]] = {
    "storage": StorageSettings,
    "server": ServerSettings,
    "telemetry": TelemetrySettings,
    "roles": RoleFields,
}
EXAMPLES = (
    "# [roles.planner]",
    "# [roles.helper]",
    "# [runtimes.claude.models]",
    "# [remotes.work]",
    "# [mcp.github]",
)
HELPER_INSTRUCTIONS = "Help with whatever is asked."


def lines() -> list[str]:
    """The reference's lines."""
    return REFERENCE.splitlines()


def table(header: str) -> list[str]:
    """The lines of the block holding the commented-out ``header``."""
    return next(
        block.splitlines() for block in REFERENCE.split("\n\n") if header in block.splitlines()
    )


def uncommented(text: str) -> str:
    """``text`` with one leading ``# `` stripped from each key line: each table header, and
    each key with its value."""
    return "\n".join(
        line.removeprefix("# ") if KEY_LINE.match(line) else line for line in text.splitlines()
    )


def named(line: str) -> str | None:
    """The key ``line`` gives a value or says is unset, if it's one that does."""
    match = NAMED_KEY.match(line)
    return match[1] if match else None


def description(model: type[BaseModel], key: str) -> str:
    """``model``'s description of its field ``key``."""
    return model.model_fields[key].description or ""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A config directory whose ``config.toml`` is the reference with every key line
    uncommented, beside the instructions file its personal role names."""
    (tmp_path / "roles").mkdir()
    (tmp_path / "roles" / "helper.md").write_text(HELPER_INSTRUCTIONS, encoding="utf-8")
    (tmp_path / USER_CONFIG_FILE).write_text(uncommented(REFERENCE), encoding="utf-8")
    return tmp_path


def test_committed_reference_matches_the_models() -> None:
    assert USER_REFERENCE.read_text(encoding="utf-8") == REFERENCE, (
        "src/orbit/agent/external/reference/user-config.toml is stale: run"
        " `uv run python -m orbit.agent.external.user_reference`"
    )


def test_starts_by_saying_it_is_your_own_configuration() -> None:
    assert lines()[0] == (
        "# Your own orbit configuration, every setting commented out at its default."
    )
    assert lines()[1].startswith("# Uncomment the settings you change")


def test_everything_is_commented_out() -> None:
    assert tomllib.loads(REFERENCE) == {}
    assert all(line.startswith("# ") or not line for line in lines())


def test_lines_fit_in_the_reference_width() -> None:
    assert max(map(len, lines())) <= REFERENCE_WIDTH


@pytest.mark.parametrize("name", TABLES)
def test_each_table_lists_every_field_under_its_description(name: str) -> None:
    model = TABLES[name]
    block = table(f"# [{name}]")
    keys = [key for line in block if (key := named(line)) is not None]
    assert keys == list(model.model_fields)
    for key in keys:
        index = next(i for i, line in enumerate(block) if named(line) == key)
        assert block[index - 1] == f"# {description(model, key)}"


def test_settings_show_their_defaults_and_unset_ones_say_so() -> None:
    server = table("# [server]")
    port = server.index("# port = 7433")
    assert server[port - 1] == f"# {description(ServerSettings, 'port')}"
    assert "# clone_dir: unset" in server
    assert "# revoked = []" in server
    assert "# tool_output_kb = 64" in table("# [storage]")
    assert '# exporter = "none"' in table("# [telemetry]")
    assert table("# [roles]")[1:] == [
        line
        for key in RoleFields.model_fields
        for line in (f"# {description(RoleFields, key)}", f"# {key}: unset")
    ]


def test_the_examples_follow_the_tables_each_under_a_line_ending_for_example() -> None:
    headers = [line for line in lines() if line.startswith("# [")]
    assert headers == [*(f"# [{name}]" for name in TABLES), *EXAMPLES]
    for header in EXAMPLES:
        block = table(header)
        assert block[0].endswith("For example:")
        assert block[1] == header


def test_uncommented_tables_hold_the_defaults(config_dir: Path) -> None:
    assert read_storage(config_dir) == StorageSettings()
    assert read_server(config_dir) == ServerSettings()
    data = tomllib.loads((config_dir / USER_CONFIG_FILE).read_text(encoding="utf-8"))
    assert load_telemetry(data) == TelemetrySettings()


def test_uncommented_role_examples_set_the_planners_model_and_a_personal_role(
    config_dir: Path,
) -> None:
    roles = read_user_roles(config_dir, BUILT_IN_ROLE_NAMES)
    assert roles.every == RoleFields()
    assert roles.named == {"planner": RoleFields(model="opus")}
    helper = roles.personal["helper"]
    assert helper.instructions == HELPER_INSTRUCTIONS
    assert (helper.lane, helper.workspace, helper.merge, helper.verify) == (
        "helper",
        "snapshot",
        False,
        False,
    )
    assert roles.servers == {"github": {"command": ["/opt/bin/github-mcp", "stdio"]}}


def test_uncommented_alias_and_remote_examples_are_read(config_dir: Path) -> None:
    assert read_user_models(config_dir) == {"claude": {"fast": "claude-haiku-5-5"}}
    assert read_remotes(config_dir) == {
        "work": RemoteSettings(
            url="wss://orbit.example.com:7433", ca=str(config_dir / "certs" / "ca.pem")
        )
    }


def test_module_writes_the_reference_to_the_path_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "user-config.toml"
    monkeypatch.setattr("sys.argv", ["user_reference", str(path)])
    monkeypatch.delitem(sys.modules, MODULE)

    runpy.run_module(MODULE, run_name="__main__")

    assert path.read_text(encoding="utf-8") == REFERENCE


def test_module_writes_the_packages_copy_under_the_current_directory_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["user_reference"])
    monkeypatch.delitem(sys.modules, MODULE)

    runpy.run_module(MODULE, run_name="__main__")

    path = tmp_path / "src" / "orbit" / "agent" / "external" / "reference" / "user-config.toml"
    assert path.read_text(encoding="utf-8") == REFERENCE


def test_personal_role_instructions_are_described_as_a_file_in_the_config_directory() -> None:
    block = table("# [roles.helper]")
    above = block[block.index('# instructions = "roles/helper.md"') - 1]
    assert "config directory" in above
    assert ".orbit/" not in above
