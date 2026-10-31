# Errors

- **Expected outcomes are values**, returned as result variants. Failures that unwind are
  exceptions.
- **`X | None` means present or absent**, never success or failure: a search that finds nothing
  returns `None`. A function that can fail raises its port's exception, or returns a result
  variant naming the failure (`Written | WriteRefused`), where the success variant may carry no
  data, as `expected<void>` would. Only the few functions with side effects return nothing on
  success.
- **Rules**: a `check_*` function raises the project's refusal when its rule fails; a function
  that returns the refusal instead is named for finding it (`find_merge_refusal`).
- **Ports declare their exceptions** beside the port. External catches the OS and library errors
  it expects and raises the port's exception, naming the file, setting or flag that caused it.
- **Chain** with `raise NewError(...) from error`, which keeps the cause in the traceback. Use
  `from None` only when the original is noise the new message fully replaces.
- **Catch** the exceptions you expect, by type, close to where they happen, and let the rest reach
  the owner that reports them.
- A `pydantic.ValidationError` becomes one project error that lists each problem.
- Write `except A, B:` without parentheses when there's no `as`.
