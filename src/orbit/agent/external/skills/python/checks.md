# Suggested checks

Suggestions for the project's verification: the configurator proposes these, and the project's
.orbit/config.toml decides what runs.

| Check | Catches | Tool | Command | Where it fits |
|---|---|---|---|---|
| Lint | Likely bugs, unused imports and names, imports out of order | ruff | `uv run ruff check` | task checks |
| Format | Code out of the project's format, long lines | ruff | `uv run ruff format --check` | task checks |
| Types | Type errors, optionals used unnarrowed, untyped code, in `src` and `tests` | mypy, strict | `uv run mypy` | task checks |
| Import contracts | Imports between code types that the contracts forbid | import-linter | `uv run lint-imports` | task checks |
| Function limits | Functions past complexity 6 or 100 lines, and past 5 parameters | lizard, and a test that counts parameters | `uv run lizard src tests -w -C 6 -L 100` | task checks, with the test that counts parameters |
| Tests with 100% branch coverage | Broken behaviour, and lines and branches no test runs | pytest with pytest-cov | `uv run pytest --cov --cov-branch --cov-fail-under=100` | task checks |
| Mutation testing | Behaviour no test checks: a small change to the code that no test fails | mutmut | `uv run mutmut run` | set `mutation`, every 10 commits, reports |
| Property-based tests | Rules that fail for inputs no example covers | Hypothesis | run by pytest, with the tests | task checks, inside the tests |
| Benchmarks | Hot paths slower than the machine's saved baseline by more than 10% | pytest-benchmark, over `benchmarks/` | `uv run pytest benchmarks --benchmark-compare --benchmark-compare-fail=mean:10%` | set `bench`, every 10 commits, reports |
