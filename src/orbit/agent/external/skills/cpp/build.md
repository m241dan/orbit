# Build

- **CMake presets** in `CMakePresets.json` name every build the checks need:
  - `asan`: AddressSanitizer and UBSan (`-fsanitize=address,undefined`), the build the per-task
    gate tests;
  - `tsan`: ThreadSanitizer, for the concurrency tests, since it can't share a build with
    AddressSanitizer;
  - `coverage`: built with `--coverage`, and read by gcovr;
  - `fuzz`: Clang with `-fsanitize=fuzzer,address,undefined`;
  - `release`: optimised, for benchmarks and packaging.

  Workflow presets let a check configure, build and test in one command:
  `cmake --workflow --preset asan`.
- **vcpkg** runs in manifest mode: `vcpkg.json` lists the dependencies, and its
  `builtin-baseline` pins their versions.
- **Targets**: one library target per code type (`<project>_domain`, `<project>_application`,
  `<project>_external`, `<project>_execution`), each linking only the ones below it. Entry points
  in `apps/` link what they wire, and each test links the target it covers.
- **Includes**: a target's include directories are private unless a dependent needs them. Orbit's
  include checker reads each directory's allowed include prefixes from its config, and fails the
  check on any other include.
- **ctest labels** sort tests into check sets (`small`, `medium`, `stress`, `fuzz`, `bench`), so a
  set runs as `ctest -L stress`. Benchmarks build only in the `release` preset.
