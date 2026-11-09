# Suggested checks

Suggestions for the project's verification: the configurator proposes these, and the project's
.orbit/config.toml decides what runs.

| Check | Catches | Tool | Command | Where it fits |
|---|---|---|---|---|
| Warnings | Code the compiler warns about | `-Wall -Wextra -Wpedantic -Werror` on every build | `cmake --workflow --preset asan`, the build itself | task checks |
| clang-tidy | Bug patterns, misused APIs, the naming `.clang-tidy` sets | clang-tidy, the checked-in `.clang-tidy`, warnings as errors | `run-clang-tidy -p build/asan -quiet` | task checks |
| Formatting | Code out of the checked-in `.clang-format` | clang-format | `clang-format --dry-run --Werror` over the sources | task checks |
| Function limits | Functions past complexity 6, 100 lines or 5 parameters | lizard | `lizard src apps tests -w -C 6 -L 100 -a 5` | task checks |
| Layers | Includes a directory's code type may not use, header-only libraries such as ASIO included | One CMake target per code type, and the include checker, which the configurator copies from `cpp`'s `scripts/check_includes.py` (read with `read_skill`) to `tools/check_includes.py`, with `include-rules.toml` beside it (see `build.md`) | `python3 tools/check_includes.py` | task checks |
| AddressSanitizer and UBSan | Memory errors and undefined behaviour in the tests | the `asan` preset | `cmake --workflow --preset asan` | task checks |
| ThreadSanitizer | Data races in the concurrency tests | the `tsan` preset, its own build | `cmake --workflow --preset tsan` | set `tsan`, every 10 commits, holds merges |
| Coverage | Lines and branches no test runs | gcovr over the `coverage` build | `gcovr --exclude-throw-branches --fail-under-line 100 --fail-under-branch 100`, after `cmake --workflow --preset coverage` | task checks |
| Mutation testing | Behaviour no test checks, over Domain and Application code | Mull | `mull-runner` over each test binary, which exits non-zero when a mutant survives | set `mutation`, every 10 commits, reports to `tests-reviewer` |
| Fuzzing | Crashes and sanitizer reports in code that parses untrusted input | libFuzzer targets in the `fuzz` preset | `ctest --preset fuzz -L fuzz` | set `fuzz`, nightly, reports |
| Property-based tests | Rules that fail for inputs no example covers | RapidCheck, inside Catch2 | run by ctest, with the tests | task checks, inside the tests |
| Benchmarks | Slowdowns past 10% against the machine's saved baseline | Google Benchmark in the `release` build, compared by its `compare.py` | `compare.py` against `bench-baseline.json`, wrapped as below so a slowdown fails it | set `bench`, every 10 commits, reports to `scaling-reviewer` |

- Run clang-format before the first check run.
- Every build and parallel run sets its parallelism to fit the machine the task describes,
  leaving headroom for the sanitizer builds, which take several times the memory of a plain
  one: `cmake --build` and `run-clang-tidy` take `-j N`, and `cmake --workflow`, which takes no
  `-j`, runs as `env CMAKE_BUILD_PARALLEL_LEVEL=N cmake --workflow --preset asan`.
- Coverage leaves out the branches a `throw` adds. Any other line that can't be covered carries
  `GCOVR_EXCL_LINE`, where a reviewer sees it.
- A sanitizer report fails the check, like a failing assertion.
- A crash a fuzzer finds becomes a regression test with that input, and the seed corpus lives in
  the repository.

## Reporting sets

A set that reports to a lens reaches it through a report, a `set_finished` trigger that queues
the lens's task when the set fails, carrying the failing step's output. So its command exits
non-zero when it finds something, and prints what it found.

- `mull-runner` fails when any mutant survives (its `--mutation-score-threshold` is 100 unless
  set), and lists the survivors. Leave out `--allow-surviving`, which would make it pass.
- Google Benchmark's `compare.py` prints the comparison but exits 0 whatever it finds. The
  benchmark command runs it on the `release` build's benchmark binary against the saved
  `bench-baseline.json`, dumps the comparison as JSON, and exits 1, naming each benchmark, when
  one's time rose by more than 10%:

  ```sh
  sh -c 'python3 tools/compare.py -d build/release/bench-diff.json benchmarks bench-baseline.json build/release/benchmarks && python3 - build/release/bench-diff.json <<"EOF"
  import json, sys
  diff = json.load(open(sys.argv[1]))
  slow = [b["name"] for b in diff if any(m["time"] > 0.10 for m in b["measurements"])]
  for name in slow:
      print(f"{name}: more than 10% slower than the baseline")
  sys.exit(1 if slow else 0)
  EOF'
  ```

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
