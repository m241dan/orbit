---
name: review-scaling
description: Super-review lens across many commits — what cost has built up? Covers work per event or tick that grew a little per commit, memory that grows with use, start-up time, the checks' own run time, and the slowdowns the benchmark set reports. Use when reviewing a batch of commits for performance that degrades as the code and its data grow.
---

# Scaling lens

**Your question:** across the commits since the last super review and the sampled files, what
cost has built up?

## Look for

- Work per event or tick that has grown a little per commit: more handlers, more rescans, more
  repaints on each update.
- Memory that grows with use: caches, histories, logs and collections that nothing trims.
- Start-up doing more each time: loading, scanning or migrating everything before the first
  response.
- The checks themselves getting slower: tests that wait for time, set up more than they need, or
  can't run in parallel.
- Slowdowns the benchmark set reports: find the commits and the cause.

## Measure against

`code-style`'s `performance.md`, the benchmark set's latest report, and the sizes the project
expects.

## Leave to others

The cost of one commit's new code was the Performance lens's job on that commit; look for what
only shows across commits.

## A finding

Name what has grown, the commits or files that grew it, how cost now grows with use, and the
change that brings it back in proportion, split into tasks that each make sense alone. Scaling
findings never block merges.

When nothing has built up, report no findings.
