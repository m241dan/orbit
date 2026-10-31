# Performance

## Know the sizes

- For each collection on a hot path, know how large it gets and how often it is touched. Write it
  down where it matters, in the plan's record or a docstring.
- Choose the approach for those sizes: what is fine for ten items may not be for ten thousand.

## Keep work in proportion to the change

- When one item changes, update that item: relabel the changed row, recheck the changed record,
  rather than rebuilding everything.
- Look things up by key rather than scanning: a map by id, a set for membership.
- Do repeated work once: move it out of the loop, or keep a cache that names its source (see
  `state.md`).

## Keep immutable values small

- Immutable state replaced whole is cheap while the value is small. Split large state among
  owners, keep collections keyed by id, and replace only the part that changed, so a change costs
  the size of its part rather than the whole.
- Use a lazy pipeline (a generator, a view) when only part of the result is used; build a
  collection once when all of it is.

## I/O

- Batch it: one call that carries many items, rather than a call per item.
- Keep it off the event loop (see the language skill).

## Bound everything that grows

- A cache has a size or age limit, a queue has backpressure, and a log or history has retention.

## Measure

- Write clear code first, and optimise where a measurement shows the cost: a profile, a
  benchmark, a timing.
- A change made for performance cites its measurement, before and after.
- Hot paths keep benchmarks in the benchmark check set: every library's, and the ones an
  application names. The set runs every 10 commits, compares each run with the last baseline
  recorded on the same machine, and reports a slowdown past its threshold (10% by default) to the
  Scaling lens.
