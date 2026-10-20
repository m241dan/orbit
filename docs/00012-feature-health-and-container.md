# Health checks and container hosting requirements

The decisions and plan for phase 3d of milestone 3 (control-plane server and remote client) of the control-plane design: `orbit serve` answers health checks, and the repository ships a sample container setup with the README section that explains it.

## Goal

`orbit serve` can be run and watched by a service manager or container orchestrator, and the recommended way to host it, in a container, works from the repository's own files with nothing secret baked in.

## Health

1. `/healthz` and `/readyz` answer on the main port without the token. Under mTLS, `orbit serve` asks for a client certificate at the TLS handshake without requiring one (`CERT_OPTIONAL`) and refuses a WebSocket upgrade that presents none, one not signed by `client_ca`, or a revoked one, at the route; every route but the two health paths refuses a connection without a valid certificate. This replaces the handshake-level requirement in `docs/00011-feature-tls-and-remotes.md` item 3. Over a separate loopback health port: a second listener to understand; over requiring credentials for health: orchestrators need secrets to probe liveness.
2. The status code is the contract; bodies follow the IETF health-check draft's minimal form, `application/health+json`. `/healthz`: 200 `{"status": "pass"}` while the event loop runs. `/readyz`: 200 `{"status": "pass"}` when the store is open, the workspace loaded, connections accepted and storage not halted; 200 `{"status": "warn", "output": "read_only"}` when another orbit owns the repository; 503 `{"status": "fail", "output": "starting" | "shutting_down" | "storage_halted"}` otherwise. No version, paths or checks are exposed. Over read-only as not ready: orchestrators restart in a loop; over plain-text bodies: no machine format; over Spring-style bodies: no warn state.

## The container

3. Files: `deploy/Dockerfile`, `deploy/compose.yaml` and `deploy/README.md` (a pointer to the main README section), built with the repository root as context.
4. The image: `python:3.13-slim`; git; Node.js LTS and the Claude Code CLI from npm pinned by a build argument; orbit installed from the repository with `uv sync --frozen --no-dev`; a non-root `orbit` user; the entrypoint `orbit serve`; `safe.directory = /workspace` in `/etc/gitconfig`. Over installing from a package index: orbit isn't published and the image wouldn't match the checkout; over an unpinned CLI: builds aren't reproducible.
5. The compose file uses TLS: it mounts the server certificate and key, sets `tls = "tls"`, binds 0.0.0.0 in the container and publishes 7433; a second, commented variant shows `network_mode: host` with plaintext on 127.0.0.1 for single-machine Linux use; its healthcheck calls `/readyz`. Over a plaintext-in-container flag: the insecure flag the design rules out; over only the host-network variant: Linux-only and can't serve another machine.
6. Claude Code's login: a named volume `claude-login`, after logging in once with `docker compose run orbit claude login`, or `ANTHROPIC_API_KEY` from an `.env` file. The image never contains a credential. Over the host's `~/.claude`: the container shares and alters the host's login; over the API key only: subscription users can't use it.
7. The container runs as `user: "${UID:-1000}:${GID:-1000}"`; the repository is bind-mounted at `/workspace`; the host's `~/.gitconfig` is mounted read-only for the commit identity; `orbit serve` exits with status 2 when git has no identity. Over the image's own user: files in the checkout owned by a stranger; over identity from `.env`: chosen against, accepting that the host config's aliases, hooks and credential helpers come along (orbit doesn't push, so helpers aren't called).
8. Volumes: `./orbit-config` bind-mounted read-write and owner-only at the config directory (`config.toml`, token, certificates); a named volume `orbit-state` for the state directory; a named volume `claude-login`. Over bind-mounting everything: permissions and WAL files on host mounts can surprise; over named volumes for everything: editing config needs `docker cp`.
9. A Python test reads the three files as text and checks that the published port matches orbit's default, the healthcheck path is `/readyz`, the config, state, repository, login and gitconfig mounts are present, and no credential appears in the image. Over no test: the files drift; over building the image when Docker exists: slow, and skips complicate coverage.

## Docs

10. README gains **Hosting in a container**: generating certificates first; building and starting with compose; logging Claude in or an API key; copying the token or client certificates to client machines and `--remote`; the healthcheck and `/readyz`'s answers; `orbit db backup` through `docker compose exec`; the host-network variant; a manual smoke test (build, up, `curl /readyz`, connect); and what the container doesn't protect: the control plane's own files from the agents inside it, and workspaces that trust each other differently. `docs/protocol.md` gains the health endpoints.

## How it will be built

1. Write this record.
2. Serve health checks: `/healthz` and `/readyz` on the main port, `readiness()`, the health+json bodies.
3. Check mTLS client certificates at the WebSocket route: `CERT_OPTIONAL` at the handshake, refusal at every route but health; update `docs/00011-feature-tls-and-remotes.md` item 3.
4. Refuse to serve without a git identity.
5. Add the container files: `deploy/Dockerfile`, `deploy/compose.yaml`, `deploy/README.md`, the text test, README's **Hosting in a container**.
