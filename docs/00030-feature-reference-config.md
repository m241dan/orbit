# Reference config requirements

The decisions and plan for phase 7d of milestone 7 (roles as data) of the control-plane design: a fully commented reference `config.toml` generated from orbit's models and shipped in the package, `orbit init` to write it, the configurator starting new files from it, and orbit's own file demonstrating it.

## Goal

Every setting a project can change is visible in one file with its default and what it does; it can never drift from the code; a project can start from it, catch up with it, and copy the built-in roles' instructions to edit, without orbit ever changing a line it wrote itself.

## The reference

1. It holds every key a project can set, each commented out with its default and a one-line explanation: the limits (`fix_attempts`, `max_checks`, `fallback_fixes`, `review_rounds`, `super_review_every`, `review_suggestions`, `super_review_suggestions`); `[[verification.steps]]` and `[setup]` as commented examples; a `[roles.<name>]` table per built-in role with every field at its shipped value; a commented example custom role. User-config sections aren't in it. Over limits, steps and one role: built-ins' fields looked up elsewhere; over project and user sections together: invites machine settings in projects.
2. `reference_config()` generates the text from the models (field names, defaults, descriptions from `Field(description=…)` added where missing) and the package's `roles.toml`; the shipped file is that text, committed, with a drift test naming the command that regenerates it (`uv run python -m orbit.agent.external.reference_config`). Over hand-written with a key check: defaults in comments drift; over no test: everything drifts.
3. Later milestones' sections appear commented out under `# Coming in a later version of orbit; leave commented out until then.`: `runtime` on roles, `[triggers.*]`, `[mcp.*]`, `[roles.<name>.tools]`, `[models.*]`. The loader refuses any of them uncommented, naming the section or key: "… isn't supported by this version of orbit yet; it's in the reference config for a later version. Leave it commented out." Over leaving them out: `--update` would add them later anyway; over ignoring them: settings that do nothing.
4. Layout: a short header (what the file is, that everything is commented out at its default, uncomment what you change); sections in the order limits, verification and setup, roles, later-version sections; each key's explanation on the comment line above it, then the commented key at its default; each role a commented table header followed by its fields; the example custom role `[roles.security-reviewer]` (snapshot, its own lane, no merge). Over inline explanations: long ones wrap badly and uncommenting leaves a trailing comment; over no example role: nothing shows how to add one.

## `orbit init`

5. `orbit init [-C path]` writes the reference to the workspace's `.orbit/config.toml`; an existing file is refused with exit 2, naming it and offering `--update` and `--force`. Over always merging: one stray command edits your file; over `--force` only: no catching up without losing edits.
6. `--update` adds, without changing or moving any existing line: missing keys inside the table they belong to, as commented lines after its last existing line; missing sections at the end, under `# Added by orbit init --update on <date>: settings this file didn't mention.`.
7. `--force` replaces the file after saving the old one as the next free `config.toml.bak.N` (`.bak.1`, `.bak.2`, …), keeping every backup. Over appending keys in one block: a role's new field far from its table; over one backup replaced each time: an older backup lost.
8. `--roles` copies each built-in's Markdown to `.orbit/roles/<name>.md` (skipping files that exist, saying which) and adds a commented `# instructions = "roles/<name>.md"` line under each role's table that lacks one (appending the role's table, commented, if the file has none), never changing an existing line; it works on an existing file by itself, and writes the reference first when there's none. Over copying only: you'd write the lines yourself; over switching roles at once: against phase 7a's rule; over requiring `--update` too: the common case needs a longer command.

## The configurator and orbit's own file

9. When the configurator creates `.orbit/config.toml`, it starts from the reference and writes the accepted steps and setup in place of the commented examples; with an existing file, only steps and setup change, as today. Over never using the reference: a new project's file is bare; over merging into existing files: edits without asking.
10. Orbit's own `.orbit/config.toml` is regenerated from the reference with its six steps and setup filled in, keeping its hand-written comments above them. Over dropping them: the reasons for the step order and limits lost; over leaving the file: the design asks it to demonstrate the reference.

## Docs

11. README's maximal example is replaced by a short section: `orbit init`, `--update`, `--force` with its numbered backups, `--roles`, a link to the reference file, and the later-version sections; orbit's own file serves as the worked example.

## How it will be built

1. Write this record.
2. Generate the reference config: `reference_config()`, the package's `reference/config.toml`, its drift test.
3. Refuse later-version settings while uncommented.
4. Add `orbit init`: `init_config`, `--force` with numbered backups.
5. Add `orbit init --update`: `update_config`.
6. Add `orbit init --roles`.
7. Start the configurator's new file from the reference.
8. Regenerate orbit's own config and point the README at the reference.
