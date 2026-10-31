---
name: review-bugs
description: Review lens for one commit — can the change give wrong results or fail? Covers cases sibling paths handle, unchecked modes, state not rechecked after a wait, partial failures, lost errors, edge cases, leaked resources, cancellation, regressions and stale docs. Use when reviewing a change for defects.
---

# Bugs lens

**Your question:** can this change give wrong results or fail?

## Look for, most common first

- A case a sibling path already handles, missed on the new path; a fix made on one path but not
  on the others with the same pattern.
- A mode that can forbid the action, left unchecked: halted, read-only, paused, closing,
  disabled. And what the mode held, not restarted when it lifts.
- State read before an `await` or a lock release and used after it without a recheck; a second
  call racing an operation already under way.
- A multi-step change that a failure partway leaves in a state a retry can't finish.
- Errors lost or misreported: swallowed, retried forever, a failed read that looks like an empty
  result, the wrong file or setting named as the cause, an original error hidden by a failing
  cleanup, an optional standing in for a failure.
- Wrong results: off-by-one, wrong units, wrong order, wrong defaults.
- Edge cases: empty, missing or malformed input; boundaries; NaN, infinity and very large input.
- States the code doesn't expect: called twice, after close, or while another call runs.
- Resources left behind on some path: a file written partly instead of whole, a child process
  outliving its group, what a failed step created.
- Tasks with no owner to await or cancel them and report their failure; a cancellation
  swallowed; cleanup that doesn't run when the work is cancelled.
- A feature grown from one case to many, with places still assuming one.
- Behaviour the task said to keep, now broken.
- Behaviour that contradicts the commit message, the docs or the README, including docs the
  change left stale.

## Measure against

The kind skill's typical mistakes, and `code-style`'s rules for the type of code you are
reading.

## Leave to the other lenses

Behaviour the task asked for and didn't get: Fit. Missing or weak tests: Tests. Code in the
wrong type: Logic. Holes an attacker could use: Security.

## A finding

Give the place (file and line), the input or state that triggers it, what goes wrong, and the
test that would show it. When that test depends on a framework's behaviour, confirm the
behaviour first.

A finding **blocks merges** when it means lost data, wrong results on a common path, a crash, or
a broken contract other code relies on (a public API, a protocol, a stored format). Say why it
blocks; the merge pass confirms it. Every other finding is a normal one.

Most changes have no bugs this lens can find. When this one has none, report no findings.
