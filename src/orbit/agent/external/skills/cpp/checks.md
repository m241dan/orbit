# Checks

| Check | How |
|---|---|
| Warnings | `-Wall -Wextra -Wpedantic -Werror` on every build |
| clang-tidy | The checked-in `.clang-tidy`, with warnings as errors |
| Formatting | The checked-in `.clang-format`; run it before the first check run |
| Function limits | Lizard: complexity 6, 100 lines, 5 parameters |
| Layers | One CMake target per code type, and Orbit's include checker |
| Sanitizers | AddressSanitizer and UBSan on every test build in the gate; ThreadSanitizer in its own build for the concurrency tests, in a later set when slow |
| Coverage | gcovr with `--exclude-throw-branches --fail-under-line 100 --fail-under-branch 100` |
| Mutation testing | Mull, in a later set, over Domain and Application code |
| Fuzzing | libFuzzer targets for code that parses untrusted input; long runs in a later set |
| Property-based tests | RapidCheck, inside Catch2 |
| Benchmarks | Google Benchmark in the `release` build, compared with the machine's saved baseline by its `compare.py`; a slowdown past 10% is reported; a later set |

- Coverage leaves out the branches a `throw` adds. Any other line that can't be covered carries
  `GCOVR_EXCL_LINE`, where a reviewer sees it.
- A sanitizer report fails the check, like a failing assertion.
- A crash a fuzzer finds becomes a regression test with that input, and the seed corpus lives in
  the repository.
