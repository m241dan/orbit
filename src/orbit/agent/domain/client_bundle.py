"""What ``orbit certs client --bundle`` puts in a client's bundle: the CA, the client's
certificate and key, and the ``[remotes.<name>]`` lines that connect with them once unpacked on
the client."""

from dataclasses import dataclass

import tomlkit

from orbit.protocol.transport import netloc

BUNDLE_HOME = "~/.config/orbit/certs"
"""Where on the client machine a bundle is unpacked, in a directory named for its client."""

REMOTE_FILE = "remote.toml"
"""The file in a bundle holding the ``[remotes.<name>]`` lines."""

_REMOTE_NOTE = (
    "# Add these lines to ~/.config/orbit/config.toml, then connect with orbit chat --remote"
    " {name}.\n"
)


@dataclass(frozen=True)
class BundledFile:
    """A file in a client's bundle: its ``name``, its ``text``, and whether it's ``private``,
    readable by its owner alone."""

    name: str
    text: str
    private: bool = False


def bundle_directory(name: str) -> str:
    """The directory on the client machine the bundle of the client ``name`` is unpacked in."""
    return f"{BUNDLE_HOME}/{name}"


def bundle_url(server_name: str, port: int) -> str:
    """The URL of the server named ``server_name`` serving TLS on ``port``."""
    return f"wss://{netloc(server_name, port)}"


def client_bundle(
    name: str, ca: str, certificate: str, key: str, url: str
) -> tuple[BundledFile, ...]:
    """The files in the bundle of the client ``name``: the CA certificate ``ca``, the client's
    ``certificate`` and its private ``key``, in PEM, then :data:`REMOTE_FILE`, the
    ``[remotes.<name>]`` lines that connect to ``url`` with them where they're unpacked."""
    files = (
        BundledFile("ca.pem", ca),
        BundledFile(f"{name}.pem", certificate),
        BundledFile(f"{name}-key.pem", key, private=True),
    )
    directory = bundle_directory(name)
    remote = {"url": url} | {
        setting: f"{directory}/{file.name}"
        for setting, file in zip(("ca", "cert", "key"), files, strict=True)
    }
    lines = tomlkit.dumps({"remotes": {name: remote}})
    return (*files, BundledFile(REMOTE_FILE, _REMOTE_NOTE.format(name=name) + lines))
