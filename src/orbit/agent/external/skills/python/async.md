# Async

- **Every task has an owner** that holds its handle, stops it and reports its failure. Store every
  `create_task` result, and start background work through the project's Execution helpers.
- **Stop a task** by cancelling it and awaiting it. To stop several, cancel each, then
  `await asyncio.gather(*tasks, return_exceptions=True)`.
- **Cancellation**: swallow only the cancellation you caused, and re-raise when the caller itself
  is being cancelled. Put cleanup in `finally`, so it runs when cancelled, even a second time.
- **Work that must finish** even when its awaiter is cancelled runs shielded and is waited out,
  and the cancellation is re-raised after.
- **Close in order**: close every part even when one fails or the close is cancelled, then
  re-raise the cancellation, or else the first failure.
- **A coroutine you won't run** is closed, so it's never left unawaited.
- **Blocking I/O** runs off the loop: `asyncio.to_thread`, or a one-thread executor per resource.
- **After every `await`**, ask for state afresh, since anything may have changed. Hold the lock
  across the check and the act.
- **Every wait is bounded**, with `asyncio.timeout` or `wait_for`.
- **Async resources** open through `@classmethod async def open(cls, ...) -> Self`, which cleans
  up on failure. Context managers are `@contextmanager` or `@asynccontextmanager` functions.
