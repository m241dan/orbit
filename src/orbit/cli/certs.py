"""The commands that make the certificates orbit serves TLS and mutual TLS with."""

import os
import socket
from pathlib import Path
from typing import Annotated

import tomlkit
import typer

from orbit.agent import (
    CA_DAYS,
    CERTIFICATE_DAYS,
    USER_CONFIG_FILE,
    CertificateError,
    CertsDir,
    KeyType,
    issue_server,
    make_ca,
    resolve_config_dir,
    server_names,
)
from orbit.cli.errors import _exit_on
from orbit.cli.options import ConfigDirOption

CA_EXISTS = (
    "A CA already exists in {directory}; a new one would invalidate every client certificate it"
    " signed. Use orbit certs server or orbit certs client, or --force to replace it."
)

NamesArgument = Annotated[
    list[str] | None,
    typer.Argument(
        metavar="NAMES...",
        help="The hostnames and IP addresses frontends reach the server by (default: this"
        " machine's hostname); localhost and 127.0.0.1 are added.",
        show_default=False,
    ),
]
KeyTypeOption = Annotated[
    KeyType,
    typer.Option(
        "--key-type",
        help="The keys to make: ed25519, or p256 (ECDSA) for a browser or load balancer in"
        " front that needs it.",
    ),
]
DaysOption = Annotated[
    int,
    typer.Option(
        "--days",
        min=1,
        max=CA_DAYS,
        help="How many days the server certificate is valid for; the CA is valid for 10 years.",
    ),
]
ForceOption = Annotated[
    bool,
    typer.Option(
        "--force",
        help="Replace the CA there is, and every certificate it signed, setting the old files"
        " aside in certs/old-<time>.",
    ),
]


def init(
    names: NamesArgument = None,
    key_type: KeyTypeOption = "ed25519",
    days: DaysOption = CERTIFICATE_DAYS,
    force: ForceOption = False,
    config_dir: ConfigDirOption = None,
) -> None:
    """Make a private CA and a server certificate it signs, in certs in your config directory,
    and print the config.toml lines that serve mutual TLS with them."""
    config = resolve_config_dir(config_dir, os.environ)
    certs = CertsDir(config)
    if certs.ca.exists() and not force:
        typer.echo(CA_EXISTS.format(directory=certs.path), err=True)
        raise typer.Exit(2)
    hostname = socket.gethostname()
    names = names or [hostname]
    with _exit_on(CertificateError):
        old = _initialized(certs, hostname, names, key_type, days)
    if old is not None:
        typer.echo(f"Moved the old files to {old}.")
    typer.echo(_init_note(certs, config / USER_CONFIG_FILE, server_names(names)))


def _initialized(
    certs: CertsDir, hostname: str, names: list[str], key_type: KeyType, days: int
) -> Path | None:
    """Write a CA of the machine ``hostname`` and a server certificate it signs for ``names``,
    valid for ``days``, with ``key_type`` keys, to ``certs``, setting aside the files of any
    there first; the directory they're set aside in, if any.

    Raises :class:`CertificateError` if a name is neither a hostname nor an IP address, or the
    files can't be set aside or written.
    """
    ca, ca_key = make_ca(hostname, key_type)
    server, server_key = issue_server(ca, ca_key, names, key_type, days)
    there = [path for path in _init_files(certs) if path.exists()]
    old = certs.set_aside(there) if there else None
    certs.write_key(certs.ca_key, ca_key)
    certs.write(certs.ca, ca)
    certs.write_key(certs.server_key, server_key)
    certs.write(certs.server, server)
    return old


def _init_files(certs: CertsDir) -> tuple[Path, ...]:
    """The files in ``certs`` that init writes."""
    return (certs.ca, certs.ca_key, certs.server, certs.server_key)


def _init_note(certs: CertsDir, config: Path, names: tuple[str, ...]) -> str:
    """What's said once ``certs`` holds a new CA and a server certificate for ``names``: the
    files, and the lines of ``config`` that serve mutual TLS with them."""
    *rest, last = names
    files = "".join(f"\n  {path}" for path in _init_files(certs))
    server = {
        "tls": "mtls",
        "cert": str(certs.server),
        "key": str(certs.server_key),
        "client_ca": str(certs.ca),
    }
    table = tomlkit.dumps({"server": server})
    ca = tomlkit.dumps({"ca": str(certs.ca)}).strip()
    return (
        f"Wrote a CA and a server certificate for {', '.join(rest)} and {last}:{files}\n\n"
        f"To serve mutual TLS with them, put these lines in {config}, in its [server] table if"
        f" it has one:\n\n{table}\n"
        f'For TLS without client certificates, use tls = "tls", with {ca} in place of client_ca.'
    )
