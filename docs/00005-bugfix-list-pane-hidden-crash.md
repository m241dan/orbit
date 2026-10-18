# List pane hidden-crash requirements

This record holds what was agreed for fixing the crash when the sidebar is hidden while a task runs, and how the fix is built.

## Goal

Hiding the sidebar (ctrl+b) while a task runs crashes the TUI with `ValueError: range() arg 3 must not be zero`, raised from Rich's `chop_cells`. The cause: ctrl+b sets `display = False` on the sidebar, so the task list has a zero-size region, but its spinner timer keeps ticking and relabelling the running task's row. `ListPane.replace_prompt` (`src/orbit/tui/widgets.py`) finds that row's height cached and re-measures the new label at the pane's current width, which is 0. Textual's own `OptionList._update_lines` returns early when `scrollable_content_region` is empty; `replace_prompt` doesn't. The same path runs for the agents pane while the sidebar is hidden, and for conversation logs on hidden tabs (also `display = False`), whose running tool calls tick through `replace_prompt`; there the gutter and padding make the width negative, so they don't crash but cache wrong row heights. After the fix, nothing measures a list pane's rows while it has no size, and the rows are right once it's shown again.

## Behaviour

- The fix is a guard in `ListPane.replace_prompt`, covering the task list, the agents pane and hidden conversation tabs at once (over stopping tickers and relabels while hidden: event-driven relabels such as `TaskList.show` would still crash; over doing both: little gain once the guard is in).
- When the pane's `scrollable_content_region` is empty, `replace_prompt` stores the new prompt, drops that option's cached renders, forgets all of the pane's row measurements and refreshes, so the rows are measured afresh once the pane has a size, as Textual does on a resize (over keeping the old heights: the line map goes stale when a row's height changed while hidden; over measuring at a clamped width: it caches nonsense heights).

## Edge cases

- `TaskList._row_width` goes negative while the sidebar is hidden, so a failure reason line is cut to nothing. A test pins that hiding the sidebar, changing a task with a reason, then showing the sidebar again shows the reason cut to the list's real width; `TaskList` gets a fix only if that test fails (over skipping `_redraw` while the list has no width as a precaution: untested work; over leaving it untested: a likely second bug left unchecked).

## Data

- This record lives in `docs/` with the other numbered records (over a second numbering in `docs/bugs/`). The raw traceback in the untracked `docs/bugs/` folder is not committed (over committing it: some 340 lines of locals); the user deletes `docs/bugs/` once the fix has landed.

## How it will be built

1. Add this record.
2. Don't measure ListPane rows while the pane has no size: the guard in `ListPane.replace_prompt` in `src/orbit/tui/widgets.py`, with regression tests for the list pane itself, ctrl+b with a running task ticking, a hidden conversation tab with a running tool call, and the reason width after the sidebar is shown again.

## Out of scope

- Stopping spinner timers while their widget is hidden.
- Cleaning up `docs/bugs/`, which is untracked; the user does it by hand.
