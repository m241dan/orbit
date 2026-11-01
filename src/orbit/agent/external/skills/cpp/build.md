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
- **Includes**: a target's include directories are private unless a dependent needs them. The
  include checker, `tools/check_includes.py` (a copy of `cpp`'s `scripts/check_includes.py`),
  reads `include-rules.toml` at the project root, whose `[rules]` map each directory, relative to
  the root, to the include prefixes it allows:

  ```toml
  [rules]
  "src/domain" = ["myproj/domain"]
  "src/execution" = ["myproj/execution"]
  "src/application" = ["myproj/application", "myproj/domain", "myproj/execution"]
  "src/external" = ["myproj", "asio", "nlohmann"]
  "include/myproj/domain" = ["myproj/domain"]
  "include/myproj/execution" = ["myproj/execution"]
  "include/myproj/application" = ["myproj/application", "myproj/domain", "myproj/execution"]
  "include/myproj/external" = ["myproj", "asio", "nlohmann"]
  "apps" = ["myproj", "asio"]
  "tests" = ["myproj", "catch2", "rapidcheck"]
  ```

  A library lists the public headers of each code type, `include/<project>/<type>`, with the same
  prefixes as the matching `src/<type>`, so they keep to their layers too.

  It checks each `#include "…"` and `#include <…>` in the C and C++ sources and headers under the
  root. A prefix allows the headers under it by whole path segments: `myproj/domain` allows
  `myproj/domain/rules.hpp`, not `myproj/domainx/a.hpp`. A directory not listed takes its
  nearest listed parent's rules, and a file under none fails. Standard headers are allowed unless
  a directory's rules list `std`, when only the headers its prefixes name are: `["std", "vector",
  "myproj/domain"]` allows `<vector>` and no other standard header. Hidden directories, symlinked
  ones and CMake build trees are skipped. It lists every include that isn't allowed and exits 1,
  or 2 when `include-rules.toml` is missing or invalid.
- **ctest labels** sort tests into check sets (`small`, `medium`, `stress`, `fuzz`, `bench`), so a
  set runs as `ctest -L stress`. Benchmarks build only in the `release` preset.
