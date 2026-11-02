---
name: python
description: Python conventions and tools — uv, ruff, strict mypy, import contracts, lizard limits, pytest with full branch coverage, mutmut and Hypothesis; naming, docstrings, pydantic versus frozen dataclasses, errors, typing, asyncio ownership and cancellation, and test style. Use when writing or reviewing Python code.
---

# Python

Adds Python's tools and idioms to `code-style`.

## Tools

- Run everything through uv: `uv run pytest`, `uv run mypy`.
- Run `ruff format` and `ruff check --fix` before the first check run; long lines and import
  order are the most common failures.
- Type strictly, for mypy's strict mode over `src` and `tests`. Narrow an optional before using
  it, in tests too, and import each name from the module that defines it.
- Keep the code types apart with import contracts (import-linter); add a contract line with each
  new dependency.
- Keep functions to complexity 6, 100 lines and 5 parameters. A CLI command or an Entry function
  past five parameters carries `# #lizard forgives(parameter_count): <why>`.
- Cover every branch. Coverage exclusions live only in `pyproject.toml`: `TYPE_CHECKING` blocks,
  `assert_never(` and `__main__`.
- Property-based tests use Hypothesis, and benchmarks use pytest-benchmark in `benchmarks/`, apart
  from `tests/`.

## Suggested checks

Suggestions for the project's verification: the configurator proposes these, and the project's
.orbit/config.toml decides what runs. Lint, format, types, import contracts, function limits and
the tests with full branch coverage fit every task's checks; mutation testing and benchmarks fit
named sets, each reporting its failures to a lens: mutation testing to `tests-reviewer`, and
benchmarks to `scaling-reviewer`.

Read `python`'s `checks.md`.

## Current Python

Write the project's Python version as it is: `X | Y` unions, ABCs from `collections.abc`, PEP 695
generics (`def first[T](items: Sequence[T]) -> T`), `match` over closed unions, and annotations
without `from __future__ import annotations`.

Read `python`'s `typing.md`.

## Functional style

- Assign each local once, and give a new value a new name; the Style lens checks it.
- Build values with comprehensions, generator expressions and conditional expressions, and change
  a frozen value with `dataclasses.replace`.
- Where a bool would go, use a `Literal` alias or an `Enum`.

Read `python`'s `functional.md`.

## Names and docs

- Functions are verbs (`parse_config`, `find_task`) and variables, fields and properties nouns,
  as `code-style`'s `naming.md` says. Classes and type aliases are CapWords.
- A docstring opens "The …" for a value, "Whether …" for a bool, and with an imperative for an
  action, with parameters in ``double backticks``. The class docstring documents the
  constructor's parameters.
- Every module opens with a one-line docstring, tests included.

Read `python`'s `naming.md` and `docs.md`.

## Models and errors

- Pydantic only where data crosses a boundary as text: wire messages, config files, saved JSON,
  JSON output. Every other value is a frozen dataclass; a small stateful helper is a plain class.
- Expected outcomes are values: result variants as frozen dataclasses joined by a union alias.
  Failures that unwind are exceptions, declared beside their port and raised by External.
- `X | None` means absent, never failed. A function that can fail raises, or returns a variant
  naming the failure.

Read `python`'s `models.md` and `errors.md`.

## Async

- Give every task an owner that holds its handle, stops it and reports its failure.
- Ask for state afresh after every `await`, and bound every wait.
- Re-raise a cancellation you didn't cause, and clean up in `finally`.
- Run blocking I/O off the loop, with `asyncio.to_thread` or a one-thread executor per resource.

Read `python`'s `async.md`.

## Tests

- A test is a plain function named by a sentence saying what happens under what condition.
- Fakes are hand-written, record what happened, and live in helper modules.
- Hold an operation mid-flight with a gate, and wait for quiet, with a bound on every wait.

Read `python`'s `tests.md`.
