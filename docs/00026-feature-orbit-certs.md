# orbit certs requirements

The decisions and plan for milestone 6 (certificates helper) of the control-plane design: `orbit certs` makes a private CA, server certificates and client certificates for orbit's TLS and mTLS, replacing the README's openssl steps as the main way to set them up.

## Goal

A self-hoster sets up TLS or mTLS, renews a server certificate and adds a laptop with a few commands, never handling openssl, never losing an old certificate, and never replacing the CA by accident.

## Commands and files

1. `orbit certs` has four subcommands: `init`, `server <name>…`, `client <name>` and `list`. Revoking stays a hand edit of `[server] revoked`, with `list` giving the fingerprint. Over a `revoke` command: a command editing your config; over `init` alone: no second laptop or renewal.
2. Files go in `<config dir>/certs/` (`--config-dir` applies): `ca.pem`, `ca-key.pem`, `server.pem`, `server-key.pem`, `clients/<name>.pem` and `clients/<name>-key.pem`; keys owner-only (0600), directories 0700. Over the current directory: keys land wherever you were.
3. Ed25519 keys by default, `--key-type p256` for a browser or managed load balancer in front; the CA lasts 10 years, server and client certificates 1 year, overridable with `--days`. Over P-256 by default: the modern choice was asked for; over Ed25519 only: no way out for proxies needing ECDSA.
4. `init` doesn't edit the config; it prints the `[server]` lines to add for mTLS (`tls = "mtls"`, `cert`, `key`, `client_ca`), noting that TLS without client certificates uses `tls = "tls"` without `client_ca`. Over writing the config: a surprise for hand-managed files.

## Behaviour

5. `init` refuses when `ca.pem` exists, saying a new CA invalidates every client certificate and pointing to `server` and `client`; `--force` replaces it after moving the old files to `certs/old-<timestamp>/`. `server` renews: the old server files move there and new ones are written, used by `orbit serve` after a restart. `client <name>` refuses an existing name; `--force` re-issues, moves the old one aside and prints its fingerprint with a reminder to add it to `revoked`. Over refusing everywhere without the move: renewing needs `--force` and old files are lost; over always overwriting: one stray `init` invalidates every client.
6. A server certificate holds the names given plus `localhost` and `127.0.0.1`, or the machine's hostname when none are given; IP addresses become IP SANs, names DNS SANs. Over only the names given: local discovery under TLS breaks; over the hostname only: remote names fail.
7. The CA key stays on the server by default; `server` and `client` take `--ca-key <path>` for a key moved elsewhere, and a missing CA key exits 2 naming `--ca-key`. README says how to move the key offline. Over never on the server: the server's account couldn't renew or add clients; over a passphrase: unattended renewals can't answer prompts.
8. Subjects: the CA `CN=orbit CA (<hostname>)`; a server certificate `CN=<first name>`; a client certificate `CN=<name>`, which `orbit serve`'s connect line shows. Certificates carry the right usages: the CA marked as a CA, server certificates for server authentication, client certificates for client authentication. Over fixed subjects: every connect line looks alike; over adding `O=orbit`: nothing reads it.
9. `list` prints each certificate in `certs/`: kind (CA, server, client), name, SANs for servers, expiry date with days left (marked `expired`, or `expires soon` within 14 days), SHA-256 fingerprint; client certificates whose fingerprint is in `[server] revoked` are marked `revoked`.

## Bundles

10. `client <name> --bundle <file>` also writes an owner-only tar holding `ca.pem`, `<name>.pem`, `<name>-key.pem` and a `remote.toml` snippet: `[remotes.<name>]` with `url` and `ca`, `cert`, `key` pointing to `~/.config/orbit/certs/<name>/…`. `url` comes from `--url`, else `wss://<first server name>:<[server] port>`. Over printing paths: three files to move by hand; over pairing: deferred in phase 3c; over a placeholder URL: always a manual edit; over requiring `--url`: a flag every time.

## Errors and docs

11. Errors exit with status 2 and say what's wrong and the fix.
12. README's certificate section uses `orbit certs` for setup, renewal, new clients, bundles, revocation with `list` and moving the CA key offline; the openssl steps stay as an alternative, for both key types.

## How it will be built

1. Write this record.
2. Create a CA and server certificate with `orbit certs init`: `orbit.agent.external.certificates` with `make_ca`, `issue_server`, `issue_client`, `CertsDir`.
3. Renew server and issue client certificates: `orbit certs server`, `orbit certs client`, `--ca-key`.
4. List certificates: `orbit certs list`.
5. Bundle a client's files: `--bundle`, `client_bundle`.
