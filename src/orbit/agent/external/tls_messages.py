"""What ``orbit serve`` says when its TLS settings or files can't be used: each message's first
sentence says what's wrong, the rest names the setting or file and the fix. README.md's table
of TLS and connection errors lists each first sentence."""

MISSING_FILE = (
    "The TLS mode is missing a file. tls = {mode!r} needs {key}: set {key} in [server] or pass"
    " --{flag}."
)
"""A mode without one of the files it needs, ``key``, given by ``--{flag}``."""

NOT_LOOPBACK = (
    "Plaintext is for loopback alone. orbit serve won't listen on {host} without TLS: pass"
    " --tls tls with --cert and --key, or set tls in [server]; or listen on 127.0.0.1 and reach"
    " it through an SSH tunnel."
)
"""A plaintext server asked to listen beyond loopback, on ``host``."""

MTLS_NOT_SUPPORTED = (
    "Mutual TLS isn't supported yet. Use --tls tls, or tls = 'tls' in [server], with the token"
    " for now."
)
"""The ``mtls`` mode, which a later release serves."""

UNREADABLE = "A TLS file can't be read. {path}: {reason}; check the path in [server] or the flag."
"""A certificate or key at ``path`` that can't be read, or isn't PEM, for ``reason``."""

KEY_READABLE = (
    "The private key can be read by others. {path} must be readable by you alone: run"
    " chmod 600 {path}."
)
"""A private key at ``path`` others can read."""

KEY_MISMATCH = (
    "The certificate and key don't match. {key} isn't the private key of {cert}: give the key"
    " the certificate was made for, or make a new certificate for the key."
)
"""A certificate, ``cert``, with another certificate's private key, ``key``."""

EXPIRED = (
    "The server certificate has expired. {cert} was valid from {not_before} to {not_after}:"
    " renew it and restart orbit serve."
)
"""A server certificate, ``cert``, past the end of its validity."""

NOT_YET_VALID = (
    "The server certificate isn't valid yet. {cert} is valid from {not_before} to {not_after}:"
    " check this machine's clock, or make a certificate valid from now."
)
"""A server certificate, ``cert``, before the start of its validity."""

EXPIRING = (
    "The server certificate expires soon. {cert} is valid until {not_after}: renew it and"
    " restart orbit serve before then."
)
"""The warning for a server certificate, ``cert``, that expires within 14 days."""

ALL = (
    MISSING_FILE,
    NOT_LOOPBACK,
    MTLS_NOT_SUPPORTED,
    UNREADABLE,
    KEY_READABLE,
    KEY_MISMATCH,
    EXPIRED,
    NOT_YET_VALID,
    EXPIRING,
)
"""Every message, for README.md's table to list."""
