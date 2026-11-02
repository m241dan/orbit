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
| Mutation testing | Behaviour no test checks: a small change to the code that no test fails | mutmut | `uv run mutmut run`, wrapped as below so surviving mutants fail it | set `mutation`, every 10 commits, reports to `tests-reviewer` |
| Property-based tests | Rules that fail for inputs no example covers | Hypothesis | run by pytest, with the tests | task checks, inside the tests |
| Benchmarks | Hot paths slower than the machine's saved baseline by more than 10% | pytest-benchmark, over `benchmarks/` | `uv run pytest benchmarks --benchmark-compare --benchmark-compare-fail=mean:10%` | set `bench`, every 10 commits, reports to `scaling-reviewer` |

## Reporting sets

A set that reports to a lens reaches it through a report, a `set_finished` trigger that queues
the lens's task when the set fails, carrying the failing step's output. So its command exits
non-zero when it finds something, and prints what it found.

- mutmut exits 0 even when mutants survive, and so does `mutmut results`. The mutation command
  wraps them: it shows each surviving mutant's diff and exits 1 when any survived, or 2 when
  mutmut itself failed:

  ```sh
  sh -c 'uv run mutmut run > /dev/null || exit 2; survivors=$(uv run mutmut results | cut -d: -f1); for m in $survivors; do uv run mutmut show "$m"; done; test -z "$survivors"'
  ```

- `--benchmark-compare-fail=mean:10%` exits 1, naming each benchmark, when one's mean is more
  than 10% slower than the saved baseline. Save the baseline with `--benchmark-autosave` on the
  machine that runs the set.

A report's prompt is inline, using `{set}`, `{status}`, `{commit}` and `{output}`. For mutation
testing, to `tests-reviewer`:

```text
The {set} set failed at {commit}: these mutants survived.

{output}

Report each surviving mutant as a finding: its file and line, the mutation, and the test that
should have caught it, or the test to write. Leave out a mutant that can't change behaviour.
```

For benchmarks, to `scaling-reviewer`:

```text
The {set} set failed at {commit}: these benchmarks slowed past the threshold.

{output}

Report each slowdown as a finding: the benchmark, how much slower it got, and the change that
slowed it among the commits since the baseline was saved.
```
