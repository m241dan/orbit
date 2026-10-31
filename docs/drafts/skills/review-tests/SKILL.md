---
name: review-tests
description: Review lens for one commit — would the tests fail if the behaviour broke? Covers untested behaviour and error paths, lines coverage runs but nothing asserts, fixes without their test, tests that pass whatever the code does or by luck, fakes that drift from their adapters, and surviving mutants. Use when reviewing the tests of a change.
---

# Tests lens

**Your question:** would the tests fail if the behaviour broke?

## Look for

- Behaviour the change adds or alters that no test checks, error and cancellation paths
  included.
- A line that coverage runs but no assertion checks.
- A fix without the test that would have caught it, in the same commit.
- Tests that pass whatever the code does: asserting nothing, asserting a mock's calls instead of
  the result, or asserting values the test itself set.
- Tests that pass by luck: the real clock, real randomness, fixed-time sleeps, waits with no
  bound, test order, state shared between tests.
- Fakes that don't pass the contract tests of the adapter they stand in for.
- Tests of the wrong size: a Domain rule tested through External, a slow test for a fast rule.
- Surviving mutants from the latest mutation run, when you have them: each one names a behaviour
  no test checks.

## Measure against

`code-style`'s `testing.md` and the kind skill's testing section.

## Leave to the other lenses

Defects in the code itself: Bugs. Behaviour the task asked for: Fit.

## A finding

Name the behaviour and where it lives, the test that seems to cover it and why it wouldn't catch
a break, and the test to add. When the test you ask for depends on a framework's behaviour,
confirm the behaviour first, so the test can fail without the fix. Tests findings never block
merges.

Most changes arrive with the tests they need. When this one did, report no findings.
