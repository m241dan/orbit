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

CLIENT_CERT_REQUIRED = (
    "A client showed no certificate. Under tls = 'mtls' each client needs a certificate"
    " {client_ca} signed: make it one and pass it to orbit chat with --cert and --key."
)
"""A client refused under mutual TLS for showing no certificate, ``client_ca`` the CA that
signs them."""

CLIENT_CERT_UNTRUSTED = (
    "A client's certificate isn't signed by the client CA. Under tls = 'mtls' clients need a"
    " certificate {client_ca} signed: sign the client's with it, or set client_ca to the CA"
    " that signed it."
)
"""A client refused under mutual TLS for a certificate ``client_ca`` didn't sign."""

REVOKED = (
    "The client's certificate is revoked. Its fingerprint {fingerprint} is in [server] revoked:"
    " give the client a new certificate, or remove the fingerprint to admit it again."
)
"""The body of the HTTP 403 a client whose certificate, of ``fingerprint``, is revoked gets."""

LOCAL_NO_CA = (
    "Local discovery won't work without a CA. orbit chat on this machine can't verify {cert}"
    " without the CA that signed it: set ca in [server] to its file, or join with orbit chat"
    " --connect and --ca."
)
"""The warning for a server serving TLS with ``cert`` that names neither ``ca`` nor
``client_ca``, so frontends here can't find it on their own."""

LOCAL_NO_HOSTNAME = (
    "Local discovery won't work with this certificate. {cert} names none of {names}, by which"
    " orbit chat on this machine would connect: add one as a subject alternative name, or join"
    " with orbit chat --connect by a name it gives."
)
"""The warning for a server certificate, ``cert``, naming none of ``names``, the names this
machine reaches the server by."""

ALL = (
    MISSING_FILE,
    NOT_LOOPBACK,
    UNREADABLE,
    KEY_READABLE,
    KEY_MISMATCH,
    EXPIRED,
    NOT_YET_VALID,
    EXPIRING,
    CLIENT_CERT_REQUIRED,
    CLIENT_CERT_UNTRUSTED,
    REVOKED,
    LOCAL_NO_CA,
    LOCAL_NO_HOSTNAME,
)
"""Every message, for README.md's table to list."""
