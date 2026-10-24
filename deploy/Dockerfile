# Orbit's control plane, `orbit serve`, in a container: see "Hosting in a container" in README.md.
# Build it from the repository root, as deploy/compose.yaml does:
#   docker build -f deploy/Dockerfile -t orbit .
# The image holds no credential: Claude's login, the token and the certificates live in volumes.
FROM node:24-bookworm-slim AS node

FROM python:3.14-slim

# Claude Code's CLI, pinned so builds are reproducible; raise it with --build-arg.
ARG CLAUDE_CODE_VERSION=2.1.304
# The orbit user's ids, which should match the owner of the mounted repository.
ARG UID=1000
ARG GID=1000

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Node.js LTS, for the Claude Code CLI.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

# Orbit from this checkout, with the versions uv.lock pins.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/orbit/.venv \
    UV_COMPILE_BYTECODE=1
WORKDIR /opt/orbit
COPY . /opt/orbit
RUN uv sync --frozen --no-dev && rm -rf /root/.cache/uv
ENV PATH="/opt/orbit/.venv/bin:${PATH}"

# The repository is bind-mounted, owned by the host's user rather than the container's.
RUN git config --system --add safe.directory /workspace

RUN groupadd --gid "${GID}" orbit \
    && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /bin/bash orbit \
    && install -d -m 700 -o orbit -g orbit \
        /home/orbit/.config/orbit /home/orbit/.local/state/orbit /home/orbit/.claude

ENV HOME=/home/orbit \
    ORBIT_CONFIG_DIR=/home/orbit/.config/orbit \
    ORBIT_STATE_DIR=/home/orbit/.local/state/orbit \
    CLAUDE_CONFIG_DIR=/home/orbit/.claude

USER orbit
WORKDIR /workspace
EXPOSE 7433
ENTRYPOINT ["orbit", "serve", "--add", "/workspace"]
