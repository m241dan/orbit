---
name: review-drift
description: Super-review lens across many commits — what has crept into the structure? Covers Entry gaining logic, modules taking on second jobs, Execution code learning business rules, rules duplicated across code types, and the project's map of types falling out of date. Use when reviewing a batch of commits for structural drift.
---

# Drift lens

**Your question:** what has crept into the structure across the commits since the last super
review?

## Look for

- Entry gaining logic.
- A module taking on a second job.
- Execution code learning business names or rules.
- Adapters growing rules, or Domain growing effects, a little at a time.
- A fact or rule with several owners, where one should own it and the rest derive from it.
- The same steps appearing a second time without a shared helper.
- Two ways of doing the same thing appearing side by side.
- Code nothing uses, and re-exports that exist only for tests.
- The project's map of types falling out of date: new directories, or a type living where the
  map doesn't say.

## Measure against

`code-style`'s `types.md`, the project's `layers` skill, and the kind skill.

## Leave to others

A single commit's placement was the Logic lens's job on that commit; look for what only shows
across commits.

## A finding

Name the drift, the files and commits it spans, and the change that brings the code back, split
into tasks that each make sense alone. Drift findings never block merges.

When the structure holds, report no findings.
