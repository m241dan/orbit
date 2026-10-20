# TLS, mTLS and named remotes requirements

The decisions and plan for phase 3c of milestone 3 (control-plane server and remote client) of the control-plane design: `orbit serve` can listen beyond loopback over TLS or mutual TLS, clients always verify it, and named remotes keep each control plane's address and credentials in the user config.

## Goal

A control plane can run on another machine and be reached safely: traffic is encrypted, the server is always verified, clients are identified by a token or a certificate, one lost machine can be revoked, and every misconfiguration says what's wrong and how to fix it.

## Modes

1. Three modes: plaintext `ws://`, TLS `wss://` with a server certificate, and mTLS requiring a client certificate signed by a CA the server trusts. Plaintext only on a loopback bind; any other bind without TLS is refused at startup. Orbit terminates TLS itself through uvicorn's options, inside `orbit.agent.external`; plaintext on loopback behind a reverse proxy or SSH tunnel stays possible.
2. `[server]` gains `tls` (`"off"` | `"tls"` | `"mtls"`, default `"off"`), `cert`, `key`, `client_ca` and `ca`; `--tls`, `--cert`, `--key`, `--client-ca` override them. A mode missing its files exits with status 2 naming the missing key. With TLS on, `host` may be any address. Over deriving the mode from the paths set: a missing `client_ca` would silently weaken mTLS; over flags only: long command lines under service managers.

## Identity

3. Under mTLS the client certificate is the identity: the handshake asks for one without requiring it and fails on one the CA didn't sign, the WebSocket route (and every route but the health checks of `docs/00012-feature-health-and-container.md`) refuses a connection that showed none with HTTP 401, so health checks need none, any token is ignored, and `orbit serve`'s connect line names the certificate's subject and SHA-256 fingerprint. With `tls` or `off`, phase 3a's bearer token is required. Over certificate and token: two secrets with no added identity; over certificate or token: a leaked token bypasses mTLS.
4. `[server] revoked` lists client certificate fingerprints, re-read for each new connection; a match is refused at the upgrade with HTTP 403 naming the fingerprint. Over a CRL file: CA tooling and a restart; over replacing the CA: drastic for one lost laptop.

## Clients

5. Clients trust a remote's `ca` file or `--ca` when given, otherwise the system trust store; the hostname is always checked; there is no insecure flag. Over a configured CA only: public certificates need their chain copied by hand; over the system store only: private CAs installed system-wide.
6. `orbit chat --connect wss://…` accepts `--ca`, `--cert`, `--key` beside `--token-file`; `[remotes.<name>]` holds `url`, `ca`, and `token_file` or `cert` and `key`; `orbit chat --remote <name>` uses them. An unknown name exits with status 2 listing the names configured; unknown keys in a remote exit with status 2 naming the file and key. Over TLS through remotes only: every test of a new server needs a config edit; over no client certificates on `--connect`: inconsistent.
7. Discovery on the same machine works in every mode: the `run/` file also records the CA to trust (`[server] ca`, or `client_ca`) and the hostname the certificate names; under mTLS, local frontends use `[server] local_client_cert`/`local_client_key`. Over a plaintext loopback listener beside TLS: a second door; over no discovery for TLS servers: the local default breaks once TLS is on.

## Keys and certificates

8. Private key files (`key`, `local_client_key`, a remote's `key`, `--key`) readable by others are refused with status 2 naming the file and `chmod 600 <path>`; certificates and CA files may be readable. Over warning: a readable key is a real exposure; over no check: least safe.
9. A server certificate that's expired or not yet valid stops `orbit serve` with status 2 naming its dates and the fix; one expiring within 14 days prints a warning at start. Over starting anyway: clients get cryptic handshake errors; over no warning: no notice before it fails.
10. A renewed server certificate takes effect when `orbit serve` restarts; clients reconnect on their own. Over reloading on file change or `SIGHUP`: orbit would build and swap its own TLS context, fragile across uvicorn versions.

## Errors and docs

11. Every TLS and connection error message is a constant: the server's in `orbit.agent.external.tls_messages`, the client's in `orbit.client.messages` (missing files for the mode, key permissions, certificate and key not matching, expired or not yet valid, plaintext beyond loopback, unknown CA, hostname mismatch, client certificate required, certificate rejected or revoked, refused token), each naming what's wrong and its fix. README has a table of each message's first words and its fix, and a test checks every message's first sentence appears in it. Over a hand-written table: it drifts; over README anchors in messages: links break silently.
12. README covers certificate setup and exchange heavily: the three modes and when to use each; openssl steps for a private CA, a server certificate (with the hostname as a SAN) and client certificates; which file goes on which machine and moving them safely; `[server]` and `[remotes]` with a complete two-machine example; the token path for TLS without mTLS; rotation (restart for the server certificate) and revocation with `revoked`; the error table. `orbit certs init` replaces the openssl steps in a later milestone. `docs/protocol.md`'s transport section covers TLS and mTLS.

## How it will be built

1. Write this record.
2. Serve over TLS: `TlsSettings` from `[server]` and flags; validation, key permissions, certificate dates; uvicorn serving `wss://`; hosts beyond loopback with TLS; `tls_messages`; README modes, server certificate steps and the error table with its test.
3. Connect over TLS: `client_ssl_context` in `orbit.client` from `--ca` or the system store with hostname checking; `--connect wss://…` with `--ca`; `orbit.client.messages`.
4. Require client certificates under mTLS: `client_ca`, the connect line's subject and fingerprint (`client_fingerprint`), `revoked` with 403; `--cert`/`--key`; README client certificates and revocation.
5. Name remotes: `RemoteSettings` from `[remotes.<name>]`; `orbit chat --remote`; README two-machine example.
6. Discover TLS servers on the same machine: the `run/` file's CA and hostname; `local_client_cert`/`local_client_key`.
