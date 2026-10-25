# User layer and config show requirements

The decisions and plan for phase 7c of milestone 7 (roles as data) of the control-plane design: the control plane's user config sets machine-bound role fields and personal roles, between the package's defaults and the project's policy, and `orbit config show` prints the resolved configuration with each value's layer.

## Goal

A machine says what it can run (models, effort) without touching any project, carries its owner's own roles everywhere, and anyone can see exactly where every setting in effect comes from.

## The user layer

1. `~/.config/orbit/config.toml` (or `--config-dir`) may hold `[roles]` with `model` and `effort` for all roles, and `[roles.<name>]` with `model` and `effort` for a role it doesn't define (`runtime` joins in milestone 8). Any other field on such a role is a config error naming the field, the file, and that it belongs in the project's `.orbit/config.toml`. Over `model` only: effort is just as machine-bound; over any field: a machine could turn off a project's verification.
2. Fields resolve most specific first: the project's role value, the user's role value, the user's all-roles value, the package's. Over the user beating the project: a project's deliberate choice overridden everywhere.
3. The user layer may define whole personal roles (every field, instructions relative to the config directory), available in every workspace the control plane serves and following every validation rule; a project role with the same name replaces it entirely, with a startup notice naming it. Over no personal roles: the design's personal roles dropped; over personal roles projects can't override: a name clash couldn't be fixed.
4. `[roles]` stops being ignored in the user config. The layers are read when a workspace's backend starts and again on `ResetConversation`, as the project's are; `orbit serve` uses the server account's user config.

## `orbit config show`

5. `orbit config show [-C path] [key] [--json] [--full] [--config-dir]` resolves the configuration for that workspace from this machine's files (package, user, project): limits, roles field by field, verification steps, setup, `[storage]`, `[server]`, `[telemetry]`, each value with its layer, only a key's subtree when given (`roles.reviewer`); no running server needed. Over asking a server: a new message and nothing without one; over roles only: limits and steps are layered too.
6. Instructions show as `(<file>, N lines)` or `(inline, N lines)`; `--full` prints the text. Over always the full text: it buries the rest; over never: checking an override means opening files.
7. Layers are named `default` (built-in, in no file), `package`, `user <path>`, `project <path>`. In the TUI's Role picker a personal role is labelled `<label> · <lane> · personal`, from an additive `AgentEntry.personal: bool`. Over file paths only: long package paths, and "no source" reads like a gap; over not marking personal roles: they look like the project's.

## Docs

8. README covers the user layer (what it may set, precedence, personal roles) and `orbit config show`; the schema is regenerated for `AgentEntry.personal`.

## How it will be built

1. Write this record.
2. Take machine-bound role fields from the user config: `resolve_roles`, `Layer`.
3. Add personal roles: `AgentEntry.personal`, the Role picker's mark.
4. Add `orbit config show`: `config_report`.
