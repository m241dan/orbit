"""The commands that make the certificates orbit serves TLS and mutual TLS with."""

import os
import socket
from collections.abc import Sequence
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
    fingerprint,
    issue_client,
    issue_server,
    make_ca,
    read_certificate,
    resolve_config_dir,
    server_names,
)
from orbit.cli.errors import _exit_on
from orbit.cli.options import ConfigDirOption

CA_EXISTS = (
    "A CA already exists in {directory}; a new one would invalidate every client certificate it"
    " signed. Use orbit certs server or orbit certs client, or --force to replace it."
)
CLIENT_EXISTS = (
    "A client certificate named {name} exists; use --force to issue a new one (the old one stays"
    " valid until you add its fingerprint to [server] revoked)."
)
REVOKE_OLD = "Add {fingerprint} to [server] revoked to stop the old certificate working."

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
CertificateDaysOption = Annotated[
    int,
    typer.Option("--days", min=1, max=CA_DAYS, help="How many days the certificate is valid for."),
]
CaKeyOption = Annotated[
    Path | None,
    typer.Option(
        "--ca-key",
        help="The CA's key, if it isn't ca-key.pem in certs in your config directory, such as"
        " on a drive kept offline.",
        show_default=False,
    ),
]
ClientNameArgument = Annotated[
    str,
    typer.Argument(
        metavar="NAME",
        help="The client's name: its certificate's CN, which orbit serve's connect lines show,"
        " and the name of its files.",
        show_default=False,
    ),
]
ReissueOption = Annotated[
    bool,
    typer.Option(
        "--force",
        help="Issue a new certificate for a client that has one, setting the old pair aside in"
        " certs/old-<time>; the old one stays valid until you revoke it.",
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
    _say_moved(old)
    typer.echo(_init_note(certs, config / USER_CONFIG_FILE, server_names(names)))


def server(
    names: NamesArgument = None,
    key_type: KeyTypeOption = "ed25519",
    days: CertificateDaysOption = CERTIFICATE_DAYS,
    ca_key: CaKeyOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Renew the server certificate: set the one in certs in your config directory aside and
    write a new one the CA signs, which orbit serve uses once restarted."""
    certs = CertsDir(resolve_config_dir(config_dir, os.environ))
    names = names or [socket.gethostname()]
    with _exit_on(CertificateError):
        ca, signer = certs.issuer(ca_key)
        certificate, key = issue_server(ca, signer, names, key_type, days)
        old = _set_aside(certs, (certs.server, certs.server_key))
        certs.write_key(certs.server_key, key)
        certs.write(certs.server, certificate)
    _say_moved(old)
    typer.echo(_server_note(certs, server_names(names)))


def client(
    name: ClientNameArgument,
    key_type: KeyTypeOption = "ed25519",
    days: CertificateDaysOption = CERTIFICATE_DAYS,
    ca_key: CaKeyOption = None,
    force: ReissueOption = False,
    config_dir: ConfigDirOption = None,
) -> None:
    """Issue a certificate the CA signs for the client NAME, in certs/clients in your config
    directory, and print its fingerprint."""
    # #lizard forgives(parameter_count): Typer takes each of the command's options as a parameter.
    certs = CertsDir(resolve_config_dir(config_dir, os.environ))
    with _exit_on(CertificateError):
        pair = (certs.client(name), certs.client_key(name))
        if pair[0].exists() and not force:
            typer.echo(CLIENT_EXISTS.format(name=name), err=True)
            raise typer.Exit(2)
        ca, signer = certs.issuer(ca_key)
        certificate, key = issue_client(ca, signer, name, key_type, days)
        replaced = read_certificate(pair[0]) if pair[0].exists() else None
        old = _set_aside(certs, pair)
        certs.write_key(pair[1], key)
        certs.write(pair[0], certificate)
    _say_moved(old)
    typer.echo(_client_note(certs, name, fingerprint(certificate)))
    if replaced is not None:
        typer.echo("\n" + REVOKE_OLD.format(fingerprint=fingerprint(replaced)))


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
    old = _set_aside(certs, _init_files(certs))
    certs.write_key(certs.ca_key, ca_key)
    certs.write(certs.ca, ca)
    certs.write_key(certs.server_key, server_key)
    certs.write(certs.server, server)
    return old


def _set_aside(certs: CertsDir, paths: Sequence[Path]) -> Path | None:
    """The directory in ``certs`` the ones of ``paths`` that exist are set aside in, if any do.

    Raises :class:`CertificateError` if they can't be set aside.
    """
    there = [path for path in paths if path.exists()]
    return certs.set_aside(there) if there else None


def _say_moved(old: Path | None) -> None:
    """Say the old files were moved to ``old``, if they were moved."""
    if old is not None:
        typer.echo(f"Moved the old files to {old}.")


def _listed(names: Sequence[str]) -> str:
    """``names``, two or more, as a list in prose."""
    *rest, last = names
    return f"{', '.join(rest)} and {last}"


def _init_files(certs: CertsDir) -> tuple[Path, ...]:
    """The files in ``certs`` that init writes."""
    return (certs.ca, certs.ca_key, certs.server, certs.server_key)


def _init_note(certs: CertsDir, config: Path, names: tuple[str, ...]) -> str:
    """What's said once ``certs`` holds a new CA and a server certificate for ``names``: the
    files, and the lines of ``config`` that serve mutual TLS with them."""
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
        f"Wrote a CA and a server certificate for {_listed(names)}:{files}\n\n"
        f"To serve mutual TLS with them, put these lines in {config}, in its [server] table if"
        f" it has one:\n\n{table}\n"
        f'For TLS without client certificates, use tls = "tls", with {ca} in place of client_ca.'
    )


def _server_note(certs: CertsDir, names: tuple[str, ...]) -> str:
    """What's said once ``certs`` holds a new server certificate for ``names``."""
    return (
        f"Wrote a server certificate for {_listed(names)}:\n"
        f"  {certs.server}\n  {certs.server_key}\n\n"
        "Restart orbit serve for it to use them."
    )


def _client_note(certs: CertsDir, name: str, shown: str) -> str:
    """What's said once ``certs`` holds a new certificate for the client ``name``, with the
    fingerprint ``shown``."""
    return (
        f"Wrote a certificate for the client {name}, with the SHA-256 fingerprint {shown}:\n"
        f"  {certs.client(name)}\n  {certs.client_key(name)}\n\n"
        f"Copy them and {certs.ca} to the client machine over a trusted channel, such as scp,"
        " for orbit chat's --cert, --key and --ca."
    )
