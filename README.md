# orbit

Orbit command-line tool.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pre-commit install
```

## Usage

```sh
uv run orbit chat       # interactive agent session
```

## Development

Write tests first. Coverage is held at 100% (`fail_under` in `pyproject.toml`); only
untestable boilerplate is excluded (`__main__.py`, `assert_never` arms, `TYPE_CHECKING`).

```sh
uv run pytest --cov     # tests; fails below 100% coverage
uv run ruff check .     # lint
uv run ruff format .    # format
uv run mypy             # type-check
uv run lint-imports     # enforce frontend/backend separation
```

## Architecture

```
orbit.tui  ──Command──▶  orbit.protocol.Session  ──▶  orbit.agent
(Textual)  ◀──Event────                          ◀──  (agentic loop)
```

- `orbit.protocol`: pydantic models (`Command`s in, `Event`s out) and the `Session`
  interface. It imports no other orbit package.
- `orbit.agent`: backend. Runs the model/tool loop and implements `Session`. Has no UI imports.
- `orbit.tui`: frontend. Sends commands and renders events. Imports only `orbit.protocol`.
- `orbit.cli`: the only place the two sides are wired together.

These rules are enforced by `lint-imports` (see `[tool.importlinter]` in `pyproject.toml`).
