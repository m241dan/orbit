# Clones, re-attaching and purging requirements

The decisions and plan for phase 4b of milestone 4 (workspaces) of the control-plane design: a moved or re-cloned repository re-attaches to its sessions, a server can clone a repository from a URL and serve it, and a workspace's records can be purged.

## Goal

A workspace keeps its history wherever its repository goes; a remote server can be given a repository by URL without anyone touching its filesystem; and a workspace that's no longer wanted can be removed without a trace, never losing work that exists nowhere else.

## Re-attaching

1. Registering a path that isn't known looks for a registered workspace with a matching fingerprint whose recorded path no longer exists; if exactly one matches, it takes over that workspace (id, sessions, events) and updates the path, with a Notice: "Re-attached the sessions of <old path>, which moved to <new path>." If the old path still exists, the new path is a separate clone with a new workspace; if several match, none is taken and the Notice names them. Over any match re-attaching: two live checkouts would share one history; over asking each time: unattended servers would need decisions.
2. Fingerprints match when the root commits are the same and either the normalised `origin` URLs are equal or one side has no `origin`. Normalising makes `git@host:a/b.git`, `ssh://git@host/a/b` and `https://host/a/b` equal. Outside git or before the first commit, nothing matches. Over the exact stored string: switching a remote's protocol breaks it; over root commits only: forks would claim each other's sessions.

## Cloning

3. `AddWorkspace` gains `url: str | None` beside `path`; exactly one must be given, else `invalid`.
4. A URL is accepted at once and cloned in the background: `WorkspaceCloning(url, phase, percent)` control-plane events report git's progress to every connection; on success the workspace is registered, flagged served and started like any added workspace, a new Catalog follows, and a last `WorkspaceCloning` names the `workspace` it's served as; on failure a control-plane `ErrorOccurred` (`git_failed`, or the refusal's code if serving it fails) gives git's message, its `url` naming the clone, so frontends drop a clone's row by URL rather than by label or wording. A URL already cloning is refused with `busy`. Over answering when the clone finishes: a long clone stalls every frontend; over a separate `CloneWorkspace`: two commands for one idea.
5. Clones go to `[server] clone_dir`, default `<state dir>/workspaces`, in a directory named after the URL's last part without `.git`, with `-2`, `-3`… when taken; the registry marks these workspaces `managed`. Over a fixed location: the location should be choosable; over frontend-chosen paths: remote users would pick directories they can't see.
6. Accepted sources: `https://…`, `ssh://…`, scp-style `user@host:path`, and a local path or `file://` URL (cloned, not served in place). Git runs as the server's account with its credential helpers and SSH agent, never prompting (`GIT_TERMINAL_PROMPT=0`, SSH `BatchMode=yes`); a clone that lacks credentials fails with git's message and a hint naming SSH keys or credential helpers. Over refusing local sources: copies should be possible; over frontends sending credentials: secrets in commands and the log.
7. A clone in progress at shutdown is stopped and its partial directory removed. `orbit serve --add <url>` clones before serving.

## Purging

8. `RemoveWorkspace(workspace, purge: bool = False)`, under the same refusals as removing, also deletes all of the workspace's records (sessions, events, logs, review count, session locks, fallback files, registry row) and a managed clone's directory, only when it has no uncommitted changes, no stashes and no commits missing from its remote; otherwise the purge is refused with `invalid`, listing what would be lost. A directory orbit didn't create is never deleted. Over records only: clones pile up; over deleting whatever the clone holds: one keypress destroys unpushed work.
9. `orbit db forget <path>` purges a workspace registered but not served (for example, used only by local `orbit chat`) with the same rules, refusing while any orbit holds its workspace lock. Over the protocol only: local-only workspaces could never be cleaned up; over the command line only: remote users couldn't purge.

## TUI and docs

10. In the TUI picker, **x** asks "Stop serving <name>" with "Keep its sessions" (preselected) or "Delete its sessions too", the latter naming a managed clone's directory when it would go.
11. "Add a path…" becomes "Add a path or URL…": URL-looking text (`https://`, `ssh://`, `file://`, `user@host:path`) is sent as `url`, anything else as `path`; a clone in progress shows as a row ("Cloning <name>… 42%"); a failure shows git's message in the field's hint. Over two entries: two fields that look the same; over URLs on the command line only: remote users couldn't clone.
12. The schema is regenerated; `docs/protocol.md` covers `AddWorkspace(url)`, `WorkspaceCloning`, `RemoveWorkspace(purge)` and re-attaching; README covers re-attaching moved repositories, cloning, `clone_dir`, credentials on the server, purging and `orbit db forget`.

## How it will be built

1. Write this record.
2. Re-attach moved repositories by fingerprint: `fingerprints_match` and `normalise_origin` in the domain; `WorkspaceRegistry.find_or_register` taking over a workspace whose old path is gone; the Notice.
3. Clone workspaces from a URL: `AddWorkspace(url)`, `WorkspaceCloning`, `Cloner`, `[server] clone_dir`, `managed` (migration 6), `orbit serve --add <url>`.
4. Purge a workspace's records: `RemoveWorkspace(purge=True)`, `purge_workspace`, `clone_loss`, `orbit db forget <path>`.
5. Clone and purge from the workspace picker.
