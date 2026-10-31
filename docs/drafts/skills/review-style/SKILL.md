---
name: review-style
description: Super-review lens across many commits — do names, docs, comments and function shape follow the project's conventions beyond what the checks enforce, and is the same thing done the same way everywhere? Use when reviewing a batch of commits for style and consistency.
---

# Style lens

**Your question:** across the commits since the last super review, do names, docs, comments and
function shape follow the conventions, beyond what the checks enforce?

## Look for

- Names that mislead, that describe what a thing used to mean, or that disagree with the words
  the project already uses for the idea.
- Wording a user can't follow word for word: messages, help text, advice.
- Functions not named as verbs, or with vague verbs (`get`, `handle`, `process`); variables,
  fields and properties not named as nouns.
- A unit in a name where a unit type exists; a bool where an enumeration would name the state.
- A local assigned twice, or a value built step by step where one expression would do.
- Docs that don't say what the function returns or does, comments that say what instead of why,
  and docs left stale.
- Functions doing several jobs, even within the complexity limit.
- Duplication.
- The same thing done several ways across files.
- A problem a tool could catch that keeps coming back: suggest a check (a lint rule, a contract)
  rather than another fix.

Look at the names and code the commits introduced or changed; older code waits for its own
tasks.

## Measure against

`code-style`, its `writing.md`, and the language skill.

## Leave to others

What the checks already enforce: formatting, types, the function limits. Code in the wrong type:
the Drift lens here, and the Logic lens on each commit.

## A finding

Name the convention, the places it is broken (several, when it's a pattern), and the fix. Group
findings by convention, and attach each to a task that already touches that code when there is
one. Style findings never block merges.

When the code follows its conventions, report no findings.
