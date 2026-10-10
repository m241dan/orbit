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
uv run orbit --help
uv run orbit hello Ada
```

## Development

```sh
uv run pytest --cov     # tests + coverage
uv run ruff check .     # lint
uv run ruff format .    # format
uv run mypy             # type-check
```
