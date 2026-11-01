# Suggested checks

Suggestions for the project's verification: the configurator proposes these, and the project's
.orbit/config.toml decides what runs.

| Check | Catches | Tool | Command | Where it fits |
|---|---|---|---|---|
| Warnings | Code the compiler warns about | `-Wall -Wextra -Wpedantic -Werror` on every build | `cmake --workflow --preset asan`, the build itself | task checks |
| clang-tidy | Bug patterns, misused APIs, the naming `.clang-tidy` sets | clang-tidy, the checked-in `.clang-tidy`, warnings as errors | `run-clang-tidy -p build/asan -quiet` | task checks |
| Formatting | Code out of the checked-in `.clang-format` | clang-format | `clang-format --dry-run --Werror` over the sources | task checks |
| Function limits | Functions past complexity 6, 100 lines or 5 parameters | lizard | `lizard src apps tests -w -C 6 -L 100 -a 5` | task checks |
| Layers | Includes a directory's code type may not use, header-only libraries such as ASIO included | One CMake target per code type, and Orbit's include checker | `python3 tools/check_includes.py` | task checks |
| AddressSanitizer and UBSan | Memory errors and undefined behaviour in the tests | the `asan` preset | `cmake --workflow --preset asan` | task checks |
| ThreadSanitizer | Data races in the concurrency tests | the `tsan` preset, its own build | `cmake --workflow --preset tsan` | set `tsan`, every 10 commits, holds merges |
| Coverage | Lines and branches no test runs | gcovr over the `coverage` build | `gcovr --exclude-throw-branches --fail-under-line 100 --fail-under-branch 100`, after `cmake --workflow --preset coverage` | task checks |
| Mutation testing | Behaviour no test checks, over Domain and Application code | Mull | `mull-runner` over the test binaries | set `mutation`, every 10 commits, reports |
| Fuzzing | Crashes and sanitizer reports in code that parses untrusted input | libFuzzer targets in the `fuzz` preset | `ctest --preset fuzz -L fuzz` | set `fuzz`, nightly, reports |
| Property-based tests | Rules that fail for inputs no example covers | RapidCheck, inside Catch2 | run by ctest, with the tests | task checks, inside the tests |
| Benchmarks | Slowdowns past 10% against the machine's saved baseline | Google Benchmark in the `release` build, compared by its `compare.py` | `ctest --preset release -L bench` | set `bench`, every 10 commits, reports |

- Run clang-format before the first check run.
- Coverage leaves out the branches a `throw` adds. Any other line that can't be covered carries
  `GCOVR_EXCL_LINE`, where a reviewer sees it.
- A sanitizer report fails the check, like a failing assertion.
- A crash a fuzzer finds becomes a regression test with that input, and the seed corpus lives in
  the repository.
