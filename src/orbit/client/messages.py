"""What a frontend says when it can't connect to a control plane: each message's first sentence
says what's wrong, the rest names the URL or file and the fix. README.md's table of TLS and
connection errors lists each first sentence."""

UNKNOWN_CA = (
    "The server's certificate isn't signed by a trusted CA. {url} showed a certificate this"
    " machine doesn't trust: pass --ca with the CA file that signed it, or set the remote's ca."
)
"""A server at ``url`` whose certificate no CA trusted here signed."""

HOSTNAME_MISMATCH = (
    "The hostname doesn't match the server's certificate. The certificate {url} showed isn't"
    " for {host}: connect by a name the certificate gives, or reissue it with {host} as a"
    " subject alternative name."
)
"""A server at ``url`` whose certificate doesn't name ``host``, the host connected to."""

CERTIFICATE_INVALID = (
    "The server's certificate can't be used. {url} showed a certificate that failed"
    " verification ({reason}): renew it on the server, or check this machine's clock."
)
"""A server at ``url`` whose certificate is signed by a trusted CA but fails verification
otherwise, for ``reason``, such as having expired."""

NOT_TLS = (
    "The server isn't speaking TLS. {url} answered in plaintext: connect with ws:// to a server"
    " on loopback, or serve it with --tls tls."
)
"""A ``wss://`` URL, ``url``, to a server serving plaintext."""

SPEAKS_TLS = "The server is speaking TLS. {url} expects TLS: connect with wss:// rather than ws://."
"""A ``ws://`` URL, ``url``, to a server serving TLS."""

PLAINTEXT_NOT_LOOPBACK = (
    "Plaintext is refused beyond loopback. {url} would carry the token unencrypted to {host}:"
    " connect with wss:// to a server serving TLS, or through an SSH tunnel to a loopback"
    " address."
)
"""A ``ws://`` URL, ``url``, to ``host``, which isn't a loopback address."""

CA_UNREADABLE = (
    "The CA file can't be read. {path}: {reason}; check the path given to --ca, and that the"
    " file is a PEM certificate."
)
"""A CA file at ``path`` that can't be read, or holds no certificate, for ``reason``."""

TOKEN_REFUSED = (
    "The control plane refused the token. It came from {path}: give it the token orbit serve"
    " uses, copied there or named with --token-file."
)
"""A token, read from the file at ``path``, the control plane turned away."""

ALL = (
    UNKNOWN_CA,
    HOSTNAME_MISMATCH,
    CERTIFICATE_INVALID,
    NOT_TLS,
    SPEAKS_TLS,
    PLAINTEXT_NOT_LOOPBACK,
    CA_UNREADABLE,
    TOKEN_REFUSED,
)
"""Every message, for README.md's table to list."""
