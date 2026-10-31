---
name: review-performance
description: Review lens for one commit — will the new code stay fast enough at the sizes and rates it will see? Covers cost that grows faster than its input, work out of proportion to the change, repeated work, I/O in loops, blocking the event loop, large values copied whole, unbounded growth, and collections built where a lazy pipeline would do. Use when reviewing a change for performance.
---

# Performance lens

**Your question:** will this change stay fast enough at the sizes and rates it will see?

## Look for

- Cost that grows faster than its input: a loop over a collection inside a loop over the same
  one, a search inside a loop where a lookup by key would do.
- Work out of proportion to the change: rebuilding, rescanning or repainting everything when one
  item changed.
- Repeated work: the same query, file read or computation on every iteration or event, where
  once would do.
- I/O in a loop where one call could carry the batch; a round trip per item.
- Blocking work on the event loop: file, git or subprocess calls, or heavy computation, in async
  code.
- A large immutable value copied whole on every event, where splitting it among owners or keying
  it by id would replace only the changed part.
- Something that grows without a limit: a cache with no eviction, a queue with no backpressure, a
  list appended to on every event.
- A collection built in full where only part of it is used, and a lazy pipeline would do.

## Measure against

`code-style`'s `performance.md`, the sizes the code will see (from the task, the plan's record or
the code's own docs), and the benchmarks when the code is on a benchmarked path.

## Leave to others

Cost that builds up across commits: the Scaling lens, in the super review. Changes that don't
alter how cost grows: clear code stands until a measurement says otherwise.

## A finding

Name the code, the size or rate where it hurts, how the cost grows with it (per event, per item,
quadratic over a run), and what keeps it in proportion. Performance findings never block merges;
growth that ends in a crash or lost data is a Bugs finding.

Most changes don't alter how cost grows. When this one doesn't, report no findings.
