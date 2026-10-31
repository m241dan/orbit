# Tests

- **Layout**: tests sit flat in `tests/`. A module is named for the module it covers
  (`test_<name>.py`) or for a behaviour. Helpers live in helper modules (`*_fakes.py`,
  `stand_ins.py`, `waiting.py`), and test modules never import each other.
- **Names**: a test is a plain function named by a sentence saying what happens under what
  condition: `test_deleting_a_session_open_in_another_orbit_is_refused`. Tests have no classes and
  no docstrings; the module's one-line docstring names its subject.
- **Scenarios**: behaviour tests read as scenarios through their names and helpers (`start`,
  `settle`), rather than through Given/When/Then syntax.
- **Fakes** are hand-written and record what happened. Monkeypatch has two jobs: replacing what
  Entry wires, and injecting OS faults in External tests (`os.replace` raising `ENOSPC`).
- **Contract tests** run over a fake and its real adapter alike:
  `@pytest.mark.parametrize("store", ["fake", "sqlite"])`.
- **Fixtures** set up the environment and shared state: isolated state and config directories,
  stand-in executables on `PATH`. Everything else is plain helper functions and module constants,
  and tables use `parametrize`.
- **Domain tests** are small and synchronous, with no I/O.
- **Application tests** go through the public surface: commands in, events out, a fake for every
  port. A test that must look inside uses the accessors in the shared helper module.
- **External tests** use real things locally: throwaway git repositories, SQLite files in
  `tmp_path`, stand-in executables driven by scenarios, loopback servers. They assert both the
  translated error and that nothing was left half done.
- **UI**: view logic is tested with fake views, synchronously; widgets with the framework's
  pilot.
- **Async tests** run under pytest-asyncio's auto mode. Every wait is bounded
  (`asyncio.wait_for(..., 5)`); an operation is held mid-flight with a gate, and a test waits for
  quiet rather than for a fixed time.
- **Asserting**: plain `assert`. Error messages are pinned with `pytest.raises(X, match=...)`,
  given an escaped or raw pattern.
- **Benchmarks** use pytest-benchmark and live in `benchmarks/`, so the gate never collects them.
  The benchmark set compares each run with the baseline saved on the same machine, and reports a
  mean slowdown past 10%.
- **Property-based tests** use Hypothesis. **Mutation testing** uses mutmut, with
  `# pragma: no mutate` on a mutant that can't change behaviour.
