# Workspace explorer requirements

The decisions and plan for phase 4d of milestone 4 (workspaces) of the control-plane design: the workspace picker gains a vim-like file explorer over the server host's filesystem, its listings served through the protocol, with a field that takes a typed path or URL.

## Goal

A frontend on any machine can find a repository on the server host and add it by browsing, without knowing its path in advance, while what can be browsed is the same as what can be added.

## Placement and keys

1. The list picker stays (served workspaces, "Add <this repository>"); its "Add a path or URL…" entry opens the explorer: a full-height modal, the path or URL field at the top, the directory listing below. Over replacing the list: opening a served workspace would mean browsing to it; over tabs: new to the TUI.
2. Keys: `j`/`k` move; `l` or `enter` goes into the highlighted directory, `h` to its parent; `a` adds the highlighted directory (`AddWorkspace(path)`); `enter` on a served workspace opens it; `/` focuses the field; `.` toggles hidden directories; `esc` returns from the field, then from the explorer to the list picker. Over `enter` adding repositories: you couldn't look inside one without adding it; over `space`: `a` already means accept on the task list.

## Listings

3. `ListDirectory(path: str | None)`, a control-plane command, answered only to the asker with `DirectoryListing(path, parent, entries, truncated, total)` (in `CONNECTION_EVENTS`, never logged): directories only, each `DirectoryEntry` with `name`, `is_repository` (has `.git`), `workspace` (the id when served), `hidden`, and `target` (a symlink's target, if any); repositories first, then alphabetical; at most 2,000 entries, with `truncated` and the total when there are more. Over files too: noise, and files can't be picked; over no repository mark: finding them means descending into each; over no cap: huge messages; over paging: protocol and keys for a rare case.
4. `ListDirectory(None)` lists the starting directory: the parent of the served workspace with the latest `added_at`, else `browse_root` if set, else the server account's home. Migration 8 adds `added_at` to `workspaces`, set whenever a workspace is flagged served (`AddWorkspace`, `--add`, a clone); existing served workspaces get the migration's time. Over always home: navigating from the top every time; over the frontend's last directory: no TUI config, and paths are per server; over most recently active or row order: not "added".
5. `[server] browse_root` (default unset) confines `ListDirectory` and `AddWorkspace(path)` to one tree, both refused with `invalid` outside it; unset, the server account sees whatever it can read. Over home only: `/srv` and `/opt` repositories couldn't be browsed; over limiting browsing only: a fenced host would still take any typed path.
6. Errors are refused: a missing path with `unknown`, an unreadable directory with `invalid` ("Permission denied: <path>"), outside the root with `invalid`; the explorer shows the message under the listing and stays where it was. A symlink to a directory is listed as one, marked `→ <target>`, and entering it follows it; the root check uses the resolved path, for `AddWorkspace` too. Over not following symlinks: repositories reached by symlink couldn't be browsed; over unresolved checks: a link could leave the root.
7. Hidden entries come in every listing, flagged; the explorer hides them by default and `.` toggles them with no new request, while the explorer is open. Over a request per toggle: round trips; over always showing them: dot-directory clutter.
8. The explorer shows only the listing it last asked for, ignoring late answers to earlier requests.
9. A local `orbit chat` refuses `ListDirectory` with `invalid`: "A local orbit works on one repository; run orbit serve to browse for others." Over allowing it: nothing uses it, and it's inconsistent with `AddWorkspace`.

## The field and adding

10. The field only adds: `enter` sends `AddWorkspace`, routed by phase 4b's rule (URL-looking text as `url`, else `path`). `tab` completes the last path component: from the current listing when the text's parent is the listing's directory, otherwise from a `ListDirectory` of that parent, without moving the listing; several matches complete their common prefix and show the candidates under the field. Over `enter` navigating: one key, two meanings; over completing from the current listing only: typed paths elsewhere wouldn't complete.
11. Adding a directory (`a`, or a path in the field) closes the explorer and the picker once the workspace is accepted and listed, and opens its session picker, as phase 4a's add does; adding a URL returns to the list picker, where phase 4b's cloning row shows progress. A refusal shows its message under the listing, or in the field's hint when typed there. Over always returning to the list: an extra step; over staying in the explorer: you'd have to leave it to use what you added.

## Docs

12. The schema is regenerated; `docs/protocol.md` covers `ListDirectory`, `DirectoryListing`, `browse_root` and `added_at`'s role; README covers the explorer, its keys, `browse_root`, and that listings come from the server.

## How it will be built

1. Write this record.
2. List directories on the server: `ListDirectory`, `DirectoryListing`, `DirectoryEntry`, `DirectoryLister`, `[server] browse_root` (also for `AddWorkspace`), refused on a local orbit.
3. Start listings beside the latest added workspace: `added_at` (migration 8) and `ListDirectory(None)`.
4. Browse directories in the workspace picker: `ExplorerScreen`, its keys and marks.
5. Type and complete paths in the explorer: the field, adding by path or URL, tab completion.
