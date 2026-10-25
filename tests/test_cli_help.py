"""The CLI's help: every command, and each command's options, listed."""

import pytest
from typer.testing import CliRunner

from orbit.cli import app

runner = CliRunner()

_CHAT = ("--workspace", "--new", "--name", "--connect", "--token-file", "--local")

_CONNECTION = ("--connect", "--remote", "--token-file", "--ca", "--cert", "--key")

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
        ([], ("chat", "serve", "status", "init", "config", "version", "db", "token", "certs")),
        (
            ["chat"],
            (*_BACKEND, *_CHAT),
        ),
        (["serve"], (*_BACKEND, "--add", "--host", "--port")),
        (["status"], (*_CONNECTION, "--json", "--state-dir", "--config-dir")),
        (["init"], ("--workspace", "-C", "--update", "--force", "--roles")),
        (["config"], ("show",)),
        (
            ["config", "show"],
            ("KEY", "--workspace", "-C", "--json", "--full", "--config-dir"),
        ),
        (["db"], ("backup",)),
        (["db", "backup"], ("PATH", "--force", "--state-dir")),
        (["token"], ("rotate",)),
        (["token", "rotate"], ("--config-dir",)),
        (["certs"], ("init", "server", "client", "list")),
        (["certs", "init"], ("NAMES", "--key-type", "--days", "--force", "--config-dir")),
        (["certs", "server"], ("NAMES", "--key-type", "--days", "--ca-key", "--config-dir")),
        (
            ["certs", "client"],
            (
                "NAME",
                "--key-type",
                "--days",
                "--ca-key",
                "--force",
                "--bundle",
                "--url",
                "--config-dir",
            ),
        ),
        (["certs", "list"], ("--config-dir",)),
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
