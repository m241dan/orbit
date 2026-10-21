"""What a frontend says when it can't connect to a control plane: each message's first sentence
says what's wrong, the rest names the URL or file and the fix. README.md's table of TLS and
connection errors lists each first sentence."""

from orbit.protocol.transport import KEY_READABLE as KEY_READABLE

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

TOKEN_UNREADABLE = (
    "The token file can't be read. {path}: {reason}; check the path given to --token-file, or"
    " the remote's token_file, and that it holds the token orbit serve uses."
)
"""A token file at ``path`` that can't be read, for ``reason``."""

TOKEN_EMPTY = (
    "The token file is empty. {path} holds no token: copy into it the token orbit serve uses,"
    " or name another with --token-file."
)
"""A token file at ``path`` that holds no token."""

LOCAL_TOKEN_REFUSED = (
    "The orbit serve running here refused the token. It came from {path}: give it the token"
    " orbit serve uses, or pass --local to run one here."
)
"""A token, read from the file at ``path``, the control plane found on this machine turned
away."""

CLIENT_CERT_REQUIRED = (
    "The server requires a client certificate. {url} refused the connection, as a server under"
    " mutual TLS does for a client showing none: pass --cert and --key with a certificate its"
    " client CA signed."
)
"""A ``wss://`` server at ``url`` that refused a client showing no certificate, with HTTP 401
or by closing the connection before answering."""

CLIENT_CERT_REJECTED = (
    "The server rejected the client certificate. {url} closed the connection unanswered, as a"
    " server under mutual TLS does for a certificate its client CA didn't sign: pass --cert"
    " and --key with one signed by the CA in the server's client_ca."
)
"""A ``wss://`` server at ``url`` that closed the connection before answering a client showing
a certificate."""

REVOKED = (
    "The server has revoked the client certificate. {url} refused it with HTTP 403: ask its"
    " operator for a new certificate, or to remove its fingerprint from [server] revoked."
)
"""A server at ``url`` that turned the client certificate away as revoked."""

TOKEN_EXPECTED = (
    "The server wants a token, not a client certificate. {url} doesn't serve mutual TLS: pass"
    " --token-file rather than --cert and --key."
)
"""A server at ``url`` that turned away a client showing a certificate rather than a token."""

CLIENT_CERT_UNREADABLE = (
    "The client certificate can't be used. {cert} with the key {key}: {reason}; check the paths"
    " given to --cert and --key, and that the key is the certificate's."
)
"""A client certificate, ``cert``, and key, ``key``, that can't be read or don't match, for
``reason``."""

CERT_WITHOUT_KEY = (
    "A client certificate goes with its key. Only {given} was given: pass --cert and --key"
    " together."
)
"""A client certificate or key, the one ``given``, without the other."""

LOCAL_CLIENT_CERT_MISSING = (
    "Local discovery under mutual TLS needs a client certificate. {url} serves mutual TLS: set"
    " local_client_cert and local_client_key in [server] in {path} to a certificate its client"
    " CA signed and its key."
)
"""A control plane on this machine, at ``url``, serving mutual TLS, while the user config at
``path`` names no client certificate for frontends here to show it."""

ALL = (
    UNKNOWN_CA,
    HOSTNAME_MISMATCH,
    CERTIFICATE_INVALID,
    NOT_TLS,
    SPEAKS_TLS,
    PLAINTEXT_NOT_LOOPBACK,
    CA_UNREADABLE,
    TOKEN_UNREADABLE,
    TOKEN_EMPTY,
    TOKEN_REFUSED,
    LOCAL_TOKEN_REFUSED,
    CLIENT_CERT_REQUIRED,
    CLIENT_CERT_REJECTED,
    REVOKED,
    TOKEN_EXPECTED,
    KEY_READABLE,
    CLIENT_CERT_UNREADABLE,
    CERT_WITHOUT_KEY,
    LOCAL_CLIENT_CERT_MISSING,
)
"""Every message, for README.md's table to list."""
