# Default user config requirements

This records what was agreed for giving each user a default `config.toml` in their config directory: `orbit serve` writes one when it's missing, and `orbit init --user` writes and refreshes it.

## Goal

The user config (`config.toml` in the config directory: `--config-dir`, else `$ORBIT_CONFIG_DIR`, else `$XDG_CONFIG_HOME/orbit`, else `~/.config/orbit`) is optional and nothing creates it, so a user has to read the README to learn what it can hold. Orbit should ship a user reference config, every user setting commented out at its default under what it does, write it when `orbit serve` starts without one, and let `orbit init --user` write, update and replace it.

## Behaviour

1. **Default contents:** a full user reference config, `user-config.toml`, shipped with orbit and generated from the models as the project reference is. Every setting is commented out at its default under a line saying what it does, and the file parses as TOML to an empty table. (Over a short stub: it would drift from the real settings, which nothing generates. Over live values: they'd freeze today's defaults for this user.)
2. **What it covers:** `[storage]`, `[server]` and `[telemetry]`, each key at its default, a setting that's unset by default shown as `# <key>: unset`; `[roles]` with `model`, `effort` and `runtime`, unset; and commented-out examples of `[roles.planner]` setting a model, a personal role, `[runtimes.claude.models]` adding an alias, a `[remotes.<name>]` table and an `[mcp.<name>]` override. (Follows from 1 and the user config's tables.)
3. **Who creates it:** `orbit serve` only, when there's no `config.toml` in the config directory it resolves; `orbit chat`, local or joining, never writes one. (Over chat too: chat would start writing files in the config directory. Over neither: an explicit command alone was not what was asked.)
4. **Explicit command:** `orbit init --user` writes it; `orbit init --user --update` adds the user settings the file doesn't mention, commented out; `orbit init --user --force` replaces it after keeping the old one as the next free `config.toml.bak.N`; all mirroring the project file's `orbit init` flags. (Over no command: the file could never be brought up to date with a later orbit's settings.)
5. **Serve says so:** after writing it, `orbit serve` prints `Wrote a default user config to <path>; every setting is commented out at its default, uncomment what you change.` before its banner. (Over silence: the user wouldn't know the file appeared.)
6. **Serve can't write it:** `orbit serve` warns `Couldn't write a default user config to <path>: <error>` on stderr and serves with the defaults. (Over stopping with status 2: the file is optional, so serving shouldn't depend on it.)

## Edge cases

| # | Given … | → outcome |
|---|---|---|
| 1 | `orbit serve`, no `config.toml` in the config dir (default or `--config-dir`) | Writes the user reference there, then prints the line in 5 before the banner. |
| 2 | `orbit serve`, the config dir itself doesn't exist | Creates it readable by its owner alone (700, as the token file requires), then as row 1. |
| 3 | `orbit serve`, `config.toml` exists, even empty or invalid | Left untouched; nothing extra is printed; an invalid file still fails as it does today. |
| 4 | `orbit serve`, the file or directory can't be written | The warning in 6, and it carries on with the defaults. |
| 5 | Two `orbit serve` starting at once on different state dirs with one config dir | The first write wins, the file whole or not at all; the second finds it there and says nothing. |
| 6 | `orbit serve` refused because one already runs on its state dir | Refused before anything is written. |
| 7 | `orbit chat`, local or joining, no user config | Nothing is written. |
| 8 | `orbit init --user`, no file | Writes the user reference and prints where, as `orbit init` does for the project file. |
| 9 | `orbit init --user`, a file exists | Status 2: `<path> already exists. Use orbit init --user --update to add the settings it doesn't mention, or --force to replace it (the old file is kept as a backup).` |
| 10 | `orbit init --user --update` | Adds the user settings the file doesn't mention, commented out, under the dated note the project `--update` uses; prints what it added, or `Nothing to add.`; writes the reference if there's no file. |
| 11 | `orbit init --user --force` | Keeps the old file as the next free `config.toml.bak.N` and replaces it. |
| 12 | `--user` with `--roles`, `--skill` or `-C` | Status 2: `--user writes your own config.toml, so it can't go with <option>.` |
| 13 | `orbit init --user --config-dir DIR` | Works on `DIR/config.toml`; without it, the config dir is resolved as `orbit serve` resolves it. |
| 14 | The user reference parsed as TOML | An empty table: writing it never changes a setting. |

## Decided for you

7. **The generator** is `src/orbit/agent/external/user_reference.py`, beside `reference_config.py`; run as `python -m orbit.agent.external.user_reference [path]` it writes `src/orbit/agent/external/reference/user-config.toml`, and a test holds the committed file equal to what it generates. (Over the CLI: Entry stays thin.)
8. **Descriptions live on the models:** each field of `StorageSettings`, `ServerSettings` (with `TlsSettings`), `RemoteSettings`, `TelemetrySettings` and `RoleFields` gets `Field(description=…)`, which the generator reads as the project generator reads its models'. (Over a table of text in the generator: it would drift from the fields.)
9. **One import-contract exception:** the generator imports `orbit.telemetry.settings`, which "Only the CLI uses telemetry" forbids; that contract gets one `ignore_imports` line for it, with a comment that `settings.py` is plain pydantic, without OpenTelemetry. (Over moving `TelemetrySettings`: it churns the telemetry package for a generator.)
10. **Reuse:** `--update` reuses `update_text` from `orbit.agent.domain.config_update` as it is; `--force` reuses the project file's backup; writes go through `create_whole`, so the first writer wins. The file gets the umask's mode, as the project config does.
11. **`--config-dir` without `--user`** on `orbit init` stops with status 2: `--config-dir names your own config.toml's directory, so it goes with --user.`

## How it will be built

1. Write this record.
2. Ship a user reference config and `orbit init --user`: the field descriptions; the generator `orbit.agent.external.user_reference` with `user_reference()` and `USER_REFERENCE_PATH`; the shipped `reference/user-config.toml`, read through `USER_REFERENCE` in `orbit.agent.external.user_config`; `init_user_config(config_dir, force) -> InitResult` in `orbit.agent.external.config_init`; `--user` (with `--force` and `--config-dir`) on `orbit init`; the import-contract line; the README.
3. Bring the user config up to date with `orbit init --user --update`: `update_user_config(config_dir, today) -> UpdateResult` in `config_init`, wired into `orbit init`; the README.
4. Have `orbit serve` write the default user config when it's missing: `create_user_config(config_dir) -> Path | None` in `config_init` (the path written, `None` when a file was there already; raises `OSError`), called by `orbit serve` after it takes the serve lock, printing the line in 5 or the warning in 6; the README.

## Out of scope

- `orbit chat` writing the user config.
- Updating an existing user config automatically when orbit is upgraded; `orbit init --user --update` does that on request.
