"""The options the CLI's commands share."""

from pathlib import Path
from typing import Annotated

import typer

from orbit.agent import MODEL_ALIASES, TlsMode
from orbit.protocol import ToolAccess

# Options declared apart, so chat stays short.
WorkspaceOption = Annotated[
    Path,
    typer.Option(
        "-C",
        "--workspace",
        help="The project to work on (default: the current directory). Inside a git"
        " repository, its top level.",
    ),
]
AddOption = Annotated[
    list[Path] | None,
    typer.Option(
        "--add",
        help="Serve the project at this path from now on, here and in every later orbit serve"
        " (repeatable). Inside a git repository, its top level.",
    ),
]
NewOption = Annotated[
    bool,
    typer.Option(
        "--new", help="Start a new session rather than picking one of the saved sessions."
    ),
]
NameOption = Annotated[
    str | None, typer.Option(help="Start a new session with this name (implies --new).")
]
ProfileOption = Annotated[
    Path | None,
    typer.Option(
        help="Write a profile of each thread, and a log of every time the UI or backend was"
        " blocked for over 50 ms, to this directory.",
    ),
]

StateDirOption = Annotated[
    Path | None,
    typer.Option(
        "--state-dir",
        help="The directory orbit keeps its database of saved sessions, and their locks, in"
        " (default: $ORBIT_STATE_DIR, else $XDG_STATE_HOME/orbit, else ~/.local/state/orbit).",
    ),
]

ConfigDirOption = Annotated[
    Path | None,
    typer.Option(
        "--config-dir",
        help="The directory of your own config.toml, whose [storage] sets what orbit keeps of"
        " sessions, [server] how orbit serve listens and [remotes] the control planes orbit chat"
        " --remote joins, and of the token frontends show it"
        " (default: $ORBIT_CONFIG_DIR, else $XDG_CONFIG_HOME/orbit, else ~/.config/orbit).",
    ),
]


ModelOption = Annotated[
    str | None,
    typer.Option(
        help=f"The planner's model: an alias ({', '.join(MODEL_ALIASES)}) or full model name."
    ),
]
ToolsOption = Annotated[
    ToolAccess | None,
    typer.Option(
        help="Default tool access for new tasks: off (none), restricted (read-only; anything"
        " needing approval is refused; the default), full (auto-approve everything)."
    ),
]
VerifyOption = Annotated[
    bool,
    typer.Option(
        help="Run the verification steps in .orbit/config.toml once at startup, and after"
        " each task; a failure after a task goes back to the agent to fix."
    ),
]
WorktreeOption = Annotated[
    bool,
    typer.Option(
        help="Run tasks in a separate git worktree and merge their verified commits into"
        " the current branch (only inside a git repository).",
    ),
]
ReviewOption = Annotated[
    bool,
    typer.Option(
        help="Review each landed commit in the background and queue the reviewer's"
        " suggestions as tasks (needs the worktree).",
    ),
]
UsageOption = Annotated[
    bool,
    typer.Option(
        help="Show your Claude subscription's session and weekly usage limits, read with"
        " your Claude login's token from ~/.claude/.credentials.json.",
    ),
]


ConnectOption = Annotated[
    str | None,
    typer.Option(
        "--connect",
        metavar="URL",
        help="Connect to the control plane orbit serve runs at this wss:// URL (or ws:// on"
        " loopback) rather than run one here; the backend flags belong on orbit serve then.",
    ),
]
RemoteOption = Annotated[
    str | None,
    typer.Option(
        "--remote",
        metavar="NAME",
        help="Connect, as --connect does, to the control plane [remotes.NAME] in your"
        " config.toml names, with its ca and its token_file or cert and key; the flags given"
        " override them.",
    ),
]
CaOption = Annotated[
    Path | None,
    typer.Option(
        "--ca",
        metavar="PATH",
        help="The CA certificate that signed the server's certificate, to verify a wss://"
        " control plane by (default: the system's trusted CAs).",
    ),
]
ClientCertOption = Annotated[
    Path | None,
    typer.Option(
        "--cert",
        metavar="PATH",
        help="The client certificate, in PEM, to show a control plane serving mutual TLS"
        " rather than the token; with --key.",
    ),
]
ClientKeyOption = Annotated[
    Path | None,
    typer.Option(
        "--key",
        metavar="PATH",
        help="The client certificate's private key, in PEM and readable by you alone.",
    ),
]
TokenFileOption = Annotated[
    Path | None,
    typer.Option(
        "--token-file",
        help="The file holding the token to show the control plane connected to (default:"
        " token in the config directory).",
    ),
]
LocalOption = Annotated[
    bool,
    typer.Option(
        "--local",
        help="Run the control plane here, even when an orbit serve is running.",
    ),
]

# The chat flags that configure the control plane, which a connected chat doesn't run.
_SERVE_FLAGS = ("model", "tools", "verify", "worktree", "review", "usage", "profile")


HostOption = Annotated[
    str | None,
    typer.Option(
        help="The address to listen on (default: [server] host in your config.toml, else"
        " 127.0.0.1); beyond loopback only with --tls."
    ),
]
PortOption = Annotated[
    int | None,
    typer.Option(
        min=0,
        max=65535,
        help="The port to listen on (default: [server] port in your config.toml, else 7433);"
        " 0 lets the system choose one.",
    ),
]
TlsOption = Annotated[
    TlsMode | None,
    typer.Option(
        "--tls",
        help="How to serve: off (plaintext, on loopback alone), tls (wss:// with --cert and"
        " --key) or mtls (also requiring client certificates --client-ca signed) (default:"
        " [server] tls in your config.toml, else off).",
    ),
]
CertOption = Annotated[
    Path | None,
    typer.Option(
        "--cert",
        help="The server certificate, in PEM (default: [server] cert in your config.toml).",
    ),
]
KeyOption = Annotated[
    Path | None,
    typer.Option(
        "--key",
        help="The server certificate's private key, in PEM and readable by you alone (default:"
        " [server] key in your config.toml).",
    ),
]
ClientCaOption = Annotated[
    Path | None,
    typer.Option(
        "--client-ca",
        help="The CA that signs client certificates under mtls (default: [server] client_ca in"
        " your config.toml).",
    ),
]
