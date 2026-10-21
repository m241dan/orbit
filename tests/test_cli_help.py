"""The CLI's help: every command, and each command's options, listed."""

import pytest
from typer.testing import CliRunner

from orbit.cli import app

runner = CliRunner()

_CHAT = ("--workspace", "--new", "--name", "--connect", "--token-file", "--local")

_BACKEND = (
    "--model",
    "--tools",
    "--verify",
    "--worktree",
    "--review",
    "--usage",
    "--profile",
    "--state-dir",
    "--config-dir",
)


@pytest.mark.parametrize(
    ("args", "listed"),
    [
        ([], ("chat", "serve", "version", "db", "token")),
        (
            ["chat"],
            (*_BACKEND, *_CHAT),
        ),
        (["serve"], (*_BACKEND, "--add", "--host", "--port")),
        (["db"], ("backup",)),
        (["db", "backup"], ("PATH", "--force", "--state-dir")),
        (["token"], ("rotate",)),
        (["token", "rotate"], ("--config-dir",)),
    ],
)
def test_help_lists_the_commands_and_options(args: list[str], listed: tuple[str, ...]) -> None:
    result = runner.invoke(app, [*args, "--help"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    for name in listed:
        assert name in result.output


def test_serve_help_leaves_out_the_flags_only_chat_takes() -> None:
    result = runner.invoke(app, ["serve", "--help"], env={"COLUMNS": "200"})

    for name in _CHAT:
        assert name not in result.output
