---
name: review-security
description: Review lens for one commit — what can untrusted input, a secret or a new dependency reach? Covers input reaching queries, commands, paths or templates before a parser checks it; secrets in code, logs or spans; secret file permissions; subprocesses; path confinement; transport that fails open; authentication and authorisation; new dependencies; personal data. Use when reviewing changes to boundaries, configuration or dependencies.
---

# Security lens

**Your question:** what can untrusted input, a secret or a new dependency reach?

## Look for

- Untrusted input reaching a query, a command, a path, a template or a deserialiser before a
  parser has checked it.
- Secrets in code, logs, spans, error messages, test fixtures or commits.
- A secret file or its directory open to others on some path that reads or writes it.
- Commands run through a shell, or built by joining strings.
- Paths used before they are resolved and confirmed to stay inside the directory they belong to,
  on every route in, and paths derived from them.
- Transport that fails open: a refused TLS handshake carried on past, an insecure scheme
  accepted.
- Authorisation missing on some path to an action, or checked after it.
- New dependencies: maintained, widely used, pinned, and needed.
- Personal data in logs or spans.

Look hardest at External and Entry, dependency manifests and configuration: untrusted input and
secrets arrive there.

## Measure against

The `security` skill, and the project's own security skill when it has one.

## A finding

Give the place, what an attacker controls, what they can reach, and how to close it.

A finding **blocks merges** when it is an exploitable hole (untrusted input reaching a query,
command, path or template unchecked, or a missing authorisation check) or a leaked secret. Say
why it blocks; the merge pass confirms it. Every other finding is a normal one.

Most changes open nothing. When this one opens nothing, report no findings.
