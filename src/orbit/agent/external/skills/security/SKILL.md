---
name: security
description: Security practice at trust boundaries — parsing untrusted input, injection, secrets, subprocesses, file paths, transport, authentication and authorisation, new dependencies, and personal data in logs and spans. Use when writing or reviewing code that takes outside input, runs commands, touches files or credentials, or adds a dependency.
---

# Security

Untrusted input enters through External and Entry. Parse it as it arrives, so the rest of the
code works with values it can trust.

## Untrusted input

- Treat everything from outside the process as untrusted: requests, files, environment
  variables, command-line arguments, replies from other services.
- Parse it once, as it arrives: External reads the bytes and hands them to a Domain parser,
  which checks type, size, range and format and returns a typed value or a refusal. Code past
  the parser works with typed values.
- Bound every number later arithmetic depends on, NaN, infinity and sizes included, and reject
  unknown types at every nesting level.
- Give parsing errors a clear message that leaves out any secret parts of the input.

## Injection

- Build database queries with parameters.
- Run commands as argument lists, without a shell.
- Render templates with escaping on.
- Deserialise only formats that can't run code, into the expected types.
- Check that user-chosen names are free of path separators, and check them against the files
  derived from them.

## Secrets

- Read secrets at Entry, from files with owner-only permissions or from the environment.
- Check a secret file and its directory for others' access on every path that reads or writes
  it.
- Keep secrets out of code, logs, spans, error messages, test fixtures and commits.
- Pass each one inward only to the adapter that needs it.

## Files and paths

- Resolve every outside path, normalising `..` and symlinks the way the system will, and confirm
  it stays inside the directory it belongs to, on every route in. Confine paths derived from it
  the same way.
- Create files with the narrowest permissions that work.

## Transport

- Fail closed: when a TLS handshake is refused, stop and say why. Require secure schemes.

## Authentication and authorisation

- Check who is calling at the edge, in Entry or External, and pass the checked identity inward.
- Decide what that identity may do with a Domain rule, and check it before the action, on every
  path to it.

## Dependencies

- Choose maintained, widely used packages, pin them in the lock file, and say in the change why
  each new one is needed.

## Personal data

- Record ids in logs and spans, and keep personal data out of them.
