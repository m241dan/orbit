# Tests

- **Catch2 v3.** A `TEST_CASE` is named by a sentence saying what happens under what condition:
  `TEST_CASE("deleting an open session is refused")`.
- **Application tests** are scenarios, written with `SCENARIO`, `GIVEN`, `WHEN` and `THEN` against
  fakes of the ports.
- **Contract tests** run the same tests over a fake and its real adapter:
  `TEMPLATE_TEST_CASE("store contract", "[contract]", fake_store, sqlite_store)`.
- **Fakes** are hand-written classes that implement the port and record what happened.
- **Domain tests** are small and synchronous; tables of cases use `GENERATE`.
- **Property-based tests** use RapidCheck's Catch2 integration (`rc::prop`).
- **Asynchronous code** runs on the test thread. The test makes its own `io_context`, starts the
  operation, and steps it with `run_one()` (one handler) or `poll_one()` (one ready handler,
  without blocking). Timers run on a fake clock, through `asio::basic_waitable_timer` with a
  custom clock and wait traits, so the test advances time by hand.
- **External tests** use real things locally: temporary directories, Unix domain sockets or
  loopback, local servers. They assert both the translated error and that nothing was left half
  done.
- **Stress tests** run under ThreadSanitizer with many threads and iterations, print the seed or
  schedule that replays a failure, and carry the `stress` label.
- **Lock-free code** is checked with a controlled scheduler or a model checker such as Relacy,
  since random interleavings only make a race likely.
- **Labels** (`small`, `medium`, `stress`, `fuzz`) match the check sets.
