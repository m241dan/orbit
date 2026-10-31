# The five code types

## Placing code

Ask in order; the first yes decides:

1. Does it build, configure or start things? **Entry.**
2. Does it have effects: I/O, the clock, randomness, the environment, process-wide state?
   **External.**
3. Is it about how work runs, the same for any business? **Execution.**
4. Does it decide what happens next and sequence the work? **Application.**
5. Otherwise it decides or transforms: **Domain.**

A piece that answers yes to two of these is two pieces. Split it: the rule goes to Domain, the
I/O to External, the sequencing to Application.

## Imports

| Type | May import |
|---|---|
| Domain | Pure libraries |
| Execution | Execution-model libraries (asyncio, ASIO, `std::execution`) |
| Application | Domain, Execution, execution-model libraries |
| External | Application's ports, Domain's types, Execution, effectful libraries |
| Entry | Everything |

## Domain

- Its results depend only on its inputs. When a rule needs data from outside, such as a file's
  contents or the time, the caller reads it and passes it in.
- It takes and returns plain values: synchronous functions over immutable data.
- It holds the project's decisions, so Application asks it instead of deciding inline.
- Authorisation is a Domain rule: whether this caller may do this.
- It parses text formats (JSON, TOML, protocol bytes) into the project's types. Parsing has no
  effects, so the parser is a Domain function.

## Application

- It sequences work and holds session state.
- It reaches outside through ports it defines, which External implements and Entry hands in.
- It uses Execution's helpers to run work concurrently.
- Its decisions come from Domain; what stays in Application is the order of the steps.
- It sees no OS or library detail: each port declares its own errors.

## External

- One adapter per outside system, as thin as it can be.
- It turns library data into the project's own types at the boundary, so library types stay
  inside it, and translates OS and library errors into the errors its port declares.
- It reads untrusted input and hands it to a Domain parser, so code past the parser works with
  typed values.

## Execution

- It is generic: task groups, supervision, cancellation, timeouts, retries, schedulers.
- It knows nothing of the business, so its names are about work in general: task, job, deadline.
- Every task it starts is awaited or cancelled, and it cleans up when it is cancelled.

## Entry

- It builds, configures and starts the program, and reads like a list.
- Logic found here moves home: parsing to Domain, reading files to External, start and stop
  order to Execution, and choosing an adapter from config to a Domain function whose answer
  Entry acts on.
- A test suite is another Entry: it builds the same types with fakes. When a test needs a lot of
  wiring, logic has leaked into Entry.

## Kinds of project

The types are the same in every kind of project; their sizes differ. An application has all
five. A pure library is mostly Domain, and its caller's `main` is the Entry. In an execution
library, Execution is the product and the five types repeat inside it. The kind skill says more.
