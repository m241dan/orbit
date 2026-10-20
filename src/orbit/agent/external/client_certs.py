"""Client certificates under mutual TLS: each WebSocket's certificate passed to the app in its
ASGI scope, as the TLS extension's ``client_cert_chain``, and each handshake refused for want of
a trusted one told to the user, as the TLS layer refuses it before any WebSocket opens."""

import ssl
from collections.abc import Callable
from typing import cast

from cryptography import x509
from starlette.types import Scope
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
from websockets.http11 import Request

from orbit.agent.external import tls_messages

# OpenSSL's verification errors for a certificate whose chain ends at no trusted CA.
_UNTRUSTED = frozenset({2, 18, 19, 20, 21})
# Its error for a client that showed no certificate.
_NO_CERTIFICATE = "PEER_DID_NOT_RETURN_A_CERTIFICATE"


class CertifiedWebSocketProtocol(WebSocketsSansIOProtocol):
    """uvicorn's WebSocket protocol, with the client's certificate, if it showed one, in each
    scope it gives the app."""

    def handle_connect(self, event: Request) -> None:
        super().handle_connect(event)
        scope = getattr(self, "scope", None)  # None for an upgrade that was refused.
        der = self._peer_certificate()
        if scope is not None and der is not None:
            chain = [ssl.DER_cert_to_PEM_cert(der)]
            scope.setdefault("extensions", {})["tls"] = {"client_cert_chain": chain}

    def _peer_certificate(self) -> bytes | None:
        """The certificate the client showed, in DER; none if it showed none."""
        ssl_object = self.transport.get_extra_info("ssl_object")
        if ssl_object is None:
            return None
        return cast(bytes | None, ssl_object.getpeercert(binary_form=True))


def client_certificate(scope: Scope) -> bytes | None:
    """The client certificate, in DER, that the WebSocket of ``scope`` showed; none if it
    showed none."""
    chain = scope.get("extensions", {}).get("tls", {}).get("client_cert_chain") or []
    return ssl.PEM_cert_to_DER_cert(chain[0]) if chain else None


def certificate_subject(der: bytes) -> str:
    """The subject of the certificate ``der``, in DER, as RFC 4514 writes it."""
    return x509.load_der_x509_certificate(der).subject.rfc4514_string()


def refused_client(error: ssl.SSLError, client_ca: str) -> str | None:
    """What to tell the user about a handshake that failed with ``error`` for want of a client
    certificate ``client_ca`` signed; none if it failed for another reason."""
    if isinstance(error, ssl.SSLCertVerificationError) and error.verify_code in _UNTRUSTED:
        return tls_messages.CLIENT_CERT_UNTRUSTED.format(client_ca=client_ca)
    if error.reason == _NO_CERTIFICATE:
        return tls_messages.CLIENT_CERT_REQUIRED.format(client_ca=client_ca)
    return None


def reporting_handshakes(say: Callable[[str], None], client_ca: str) -> type[ssl.SSLObject]:
    """The TLS connection class whose handshakes ``say`` why a client was refused for want of
    a certificate ``client_ca`` signed, which the TLS layer otherwise drops unseen."""

    class Reporting(ssl.SSLObject):
        def do_handshake(self) -> None:
            try:
                super().do_handshake()
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                raise  # Not failed: waiting for more of the handshake.
            except ssl.SSLError as error:
                message = refused_client(error, client_ca)
                if message is not None:
                    say(message)
                raise

    return Reporting
