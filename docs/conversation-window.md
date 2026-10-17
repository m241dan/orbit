# Conversation window requirements

The agreed requirements for improving orbit's conversation window, and what is deliberately left
for later. This is the source of truth for the work that follows.

## Goal

Make the conversation easier to read and to follow: markdown rendering, a clear signal when the
model is working and done, more useful tool results, better prompt input, and more room for the
conversation.

## Design decision: one timeline, not three windows

We considered separate windows for tool output, thinking-out-loud and final answers, and rejected
them:

- They lose causality. A turn reads "I'll check X → Read X → so change Y → Edit Y"; split across
  windows, the reasoning no longer sits next to the calls it explains.
- They cost screen space, multiplied by tabs.
- The final answer is only known when the turn ends, so nothing could go in its window until then.

Instead, there is one timeline with three levels of prominence: final answers bright, thinking dim
and filterable, and tool calls one line each, plus a detail pane for a call's full content.

## Markdown messages

- Assistant messages (the planner's and all agents') and the user's prompts render as markdown.
  Tool output, orbit's replies, notices and task headers stay plain.
- Headings are left-aligned and bold (h1 also underlined), never centred or boxed. Links show as
  `text (url)`. Code blocks are syntax highlighted with Rich's default theme, background included.
- Malformed markdown, empty messages, wide tables and long code lines render without error and stay
  within the gutter.
- The `▾`/`▸` marker hangs on the first row; the rows after it are indented 2 columns. A collapsed
  entry shows its first source line, plain, and `(+N lines)`. A one-line message has no marker.
- The agent's label (such as `agent-3`) is a muted row above a message, shown only when the speaker
  changes from the previous entry in that tab's log. The planner has no label.

## Thinking vs final

- Assistant messages arrive dim. When the turn ends, the messages after its last tool call are
  promoted to full brightness. A turn that ends on a tool call promotes nothing.
- `t` (in normal mode) hides and shows dim messages on all tabs; while they're hidden the title
  reads `Conversation · thinking hidden`. The choice isn't remembered across restarts.

## Working row and end rule

- A live row is pinned below the shown tab's timeline. It is not an entry and can't be selected.
  It has one line per running agent visible in that tab, such as
  `◜ agent-3 · Bash uv run pytest 0:12 · 3:40` (the current tool, the tool's time, the turn's
  time).
  - Parallel calls show as `(+N running)`.
  - When no tool runs, the line shows only the turn's time.
  - At most 3 lines, then `+N more`.
  - The All tab shows every running agent; a role's tab shows that role's agents.
  - The row is gone when nothing runs.
- Every turn ends with a muted rule: `── done · 1m12s · ↑1.2M ↓8k ──` (input tokens include the
  cache). Without usage, it shows only the time, measured from `TurnStarted`.
- Abnormal endings are named: `stopped` muted, `out of steps`, and `error` or `crashed` in red.
- For agents, the rule replaces the `■ agent-3 exited/stopped/crashed` line.

## Tool calls and orbit requests

- A call and its result are one entry (matched by `call_id`), updated in place:
  - while running, spinning: `◜ Read src/x.py`;
  - when done: `→ Read src/x.py ✓ 234 lines`;
  - on error, red with `✗`;
  - `⊘ no result` if the turn ends first.
- Tool entries no longer expand inline.
- Result summaries:
  - Read: `N lines`.
  - Edit/MultiEdit: `+A −R`.
  - Write: `+N`.
  - Bash: `✓`, or `✗ exit N` (N parsed from `Exit code N`; otherwise a plain `✗`).
  - Grep/Glob: `N matches`.
  - Others: `✓`/`✗` and `N lines`.
- An agent's request to orbit merges with orbit's reply (matched by `request_id`), such as
  `→ orbit complete_task ✓ accepted`.

## Detail pane

- `enter` on a tool call or request entry opens a pane under the conversation at 40% of its height
  (one setting, so resizing is easy to add later). The pane follows the cursor; on an entry without
  details it shows a dim "No details for this entry".
- It can be focused by window switching, and `J`/`K` scroll it from the conversation. `esc` first
  closes the pane, then leaves list mode. The pane closes when switching tabs and doesn't come
  back. A running call's content updates in place.
- Content: the arguments, then the result.
  - Edit/MultiEdit/Write: coloured diffs (red, green, dim), with best-effort line numbers found from
    Claude Code's numbered result snippet, omitted if they can't be found.
  - Read: highlighted by the file's extension.
  - Bash: `$ command` and its output, with ANSI colours.
  - Grep: the matches.
  - JSON: pretty-printed.
  - Orbit requests: the arguments and orbit's reply.
- The content is capped at 200 lines: the first 100, `… N lines omitted · y copies all …`, and the
  last 100. `y` copies the full output through OSC 52 and shows a "Copied N lines" toast (tmux
  needs `set -g set-clipboard on`).
- Below a minimum terminal height, the pane covers the whole conversation area until it is closed.

## Prompt input

- The prompt box wraps and grows up to 8 lines, then scrolls. Newlines can't be typed, but pasted
  ones are kept; `enter` sends.
- Typing is allowed while the planner is busy; `enter` then shows a "planner is busy" toast and
  keeps the text. The text clears only after a successful send.
- In the timeline, the user's prompt has a `❯` prefix and a faint background band, at normal
  weight, rendered as markdown, and starts collapsed when longer than 15 lines.

## Layout

- `ctrl+b` hides and shows the sidebar, so the conversation can take the full width. The sidebar
  never hides on its own, and the choice isn't remembered across restarts.

## Deferred (to do later)

- Notifications or a terminal bell when the planner finishes.
- An "open in editor" option for writing long prompts.
- Typed newlines in the prompt (`shift+enter` where the terminal reports it, `alt+enter`
  elsewhere).
- Queuing prompts while the planner is busy, and editing or cancelling queued ones.
- Resizing the detail pane with `+`/`-`.
- Remembering toggles (the thinking filter, the sidebar) across restarts.
- A key to jump the cursor to running tool calls.
- Streaming assistant text as it is generated.
- Markdown in task headers and proposals.
- Changes to the sidebar's contents, the tabs row or the footer; revisit the layout once the above
  lands.
- Viewing two tool outputs side by side (lost when tool entries stopped expanding inline).
