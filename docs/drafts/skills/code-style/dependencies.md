# Placing dependencies

Ask two questions of every new dependency:

1. **Could a test that uses it be slow, flaky, or affected by the machine or by other tests?**
   Then it has effects.
2. **Would a test need to run a loop or wait for it to call back?** Then it takes control.

| | Takes no control | Takes control |
|---|---|---|
| **No effects** | **Pure**: pydantic, parsers, math. Any type may use it, and tests use it directly. | **Execution model**: asyncio, HPX futures, `std::execution`. Execution, Application and External use it; tests run it deterministically rather than mocking it. |
| **Effects** | **Effectful**: database drivers, HTTP clients, the file system. External uses it behind an adapter; Application's tests use a fake of the adapter's port. | **Both**: ASIO, Textual, Qt, uvicorn. Split it: its control parts follow the execution-model rule, its effectful parts stay in External, and Entry configures it. |

Then add the dependency's rule to the import contracts, so a checker enforces where it may be
used.

## Effects that hide in ordinary calls

The clock, randomness, UUIDs, environment variables and process-wide state are effects, even
though calling them looks pure. Domain receives them as parameters, so it stays deterministic.
Application and External may make ids and time spans directly, and take them as parameters
wherever a test needs to control the value. Give such a parameter a real default (a `now` or
`new_id` function), so callers pass only what a test controls.

## Choosing a dependency

Prefer the standard library, then maintained and widely used packages. Pin versions in the lock
file, and say in the change why the dependency is worth adding.
