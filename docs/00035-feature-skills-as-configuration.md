# Skills as configuration requirements

The decisions and plan for milestone 11a of `scratch/control-plane-design.md`: orbit's skills
loaded from the package and the project, chosen by roles and by paths, and shown and copied by
orbit's commands. `scratch/skills-design.md` holds the settled design (its decisions 1–43); this
record settles what it left open for this phase.

## Goal

Orbit ships skills, folders of Markdown that teach agents how the user builds software, and a
project can override any file of them or add its own. Roles name the skills their agents get,
and the project maps paths to skills. `orbit config show` lists every skill file with its layer,
and `orbit init --skill` copies shipped skills for editing. Agents get nothing new until 11b
delivers the skills.

## Scope

1. Milestone 11 is planned a phase at a time (11a to 11e), each with its own record, as milestone
   7 was; this record is 11a's; over one plan for all of it (some thirty tasks settled before any
   code lands).
2. 11a moves the 18 shipped skill drafts from `docs/drafts/skills/` into the package as written,
   with the built-in roles' default skills; agents get nothing new until 11b delivers them; 11c
   keeps the role edits and removing `docs/drafts/`, and Orbit's `layers` and `orbit` go to 11d;
   over landing the skills in 11c (11a would show nothing real, and the defaults couldn't land).

## Data

3. The shipped skills live in `src/orbit/agent/external/skills/<name>/`, beside `instructions/`,
   inside orbit's wheel; over a top-level `src/orbit/skills/` (only the backend reads it) and
   `instructions/skills/` (mixes roles and skills).
4. Skills come from two layers: the package, and the project's `.orbit/skills/<name>/`. A project
   file replaces the shipped file at the same path, whole; a project folder with a new name is a
   new skill; over a user layer (skills would differ by machine). This follows the design's
   decision 5. A file directly in `.orbit/skills/`, outside any folder, is ignored, so a project
   can keep a note there; over refusing it (it rules out notes).
5. A skill is every file in its folder at any depth but hidden ones (starting with `.`), read as
   UTF-8; over top-level files only (Claude Code folders with subfolders wouldn't load whole) and
   `.md` only (a skill's scripts would be lost).
6. `SKILL.md` opens with front matter: a `---` block of `key: value` lines, read by a small domain
   parser; `name` and `description` are required and other keys are ignored; over PyYAML (a
   dependency for two fields) and refusing unknown keys (turns away Claude Code skill folders).
7. Skills are read with the configuration as a workspace opens, from the user's checkout with
   uncommitted edits, so an edit takes effect when the workspace opens again; over reading them at
   every launch (a broken skill would fail mid-run).

## Behaviour

8. Every role has `skills`, a list of skill names; a higher layer replaces the whole list, as for
   `tools` and `consults`; over adding to the built-in list (a project couldn't drop a default).
9. The defaults: planner `code-style`; coder `code-style`, `security`; reviewer `code-style`,
   `security`, `review-fit`, `review-bugs`, `review-tests`, `review-logic`, `review-security`,
   `review-performance`; super-reviewer `code-style`, `review-style`, `review-drift`,
   `review-scaling`; experimenter and configurator none; over `code-style` for the experimenter
   and configurator (text they rarely use) and the reviewer without `security` (its security lens
   measures against it).
10. `[[skills]]` entries have `paths` (a non-empty list of files or folders, relative to the
    project root) and `use` (a non-empty list of skill names), only in the project's
    `.orbit/config.toml`; the user config already refuses them; over allowing them in the user
    config (mappings would differ by machine), one `path` per entry (repeated entries for a group
    of paths) and folders only (a lone build script couldn't get its own skills).
11. An agent gets its role's skills plus every `[[skills]]` entry's; from 11b its instructions say
    which paths each entry's skills cover; over leaving the paths as notes for people (the agent
    couldn't tell which skill applies where).

## Validation

12. Validation checks that each skill folder has a `SKILL.md`, that its front matter's `name`
    matches the folder, Claude Code's rules (a name of at most 64 lowercase letters, digits and
    hyphens; a description of 1 to 1,024 characters), and that every skill a role or `[[skills]]`
    names exists. The "Read …" lines aren't checked; over checking they resolve (parsing their
    fixed form) and checking only that named skills exist (broken folders fail only once used).
13. Each `[[skills]]` path must be relative, stay inside the project (no `..` out of it), and
    exist in the checkout as a file or folder; over not checking existence (a typo would pass
    silently, though a mapping can't name a folder before it's made) and no checks at all
    (outside paths would reach agents as if in the project).
14. Errors are configuration errors opening with the file's path or the setting's key, the first
    problem raised on its own:
    - `.orbit/skills/foo: a skill needs a SKILL.md.`
    - `…/SKILL.md: needs front matter between --- lines, with name and description.`
    - `…/SKILL.md: its name is pyton but its folder is python.`
    - `…/SKILL.md: a skill's name is at most 64 lowercase letters, digits and hyphens.`
    - `…/SKILL.md: a skill's description is 1 to 1,024 characters.`
    - `…/tests.md: isn't UTF-8 text.`
    - `roles.reviewer.skills names pyton, which isn't a skill; the skills are application,
      code-style, …` (and `skills.2.use names …` for a mapping entry)
    - `skills.1.paths: paths are relative to the project root and stay inside it.`
    - `skills.1.paths names src/orbt, which isn't in the project.`

    Over listing every problem in one error (a collecting path the loader lacks).

## Wording and tools

15. `orbit config show` lists `[[skills]]` as numbered settings (`skills.1.paths`,
    `skills.1.use`), like the verification steps, and each skill file as
    `skill_files.<skill>.<file>`, which the key filter reaches; over a block outside the dotted
    keys (the filter couldn't reach it). A file's value is its number of lines, and its layer
    column carries the path, which only a project file has:

    ```
    skill_files.python.SKILL.md = (84 lines)   package
    skill_files.python.tests.md = (42 lines)   project .orbit/skills/python/tests.md
    ```

    `--full` shows the text in place of the count; each skill is a group of its own, as each
    role is; over the path in the value with a bare layer, as instructions show (long lines, a
    layer column that says less). In `--json`, `skill_files` nests by skill only and keeps each
    file's path whole as its key (`{"skill_files": {"python": {"tests.md": …}}}`), each with
    `value`, `layer` and `file` (the project file's path, `null` for the package's); over the
    generic split at every dot (file names split apart, and `tests` would collide with
    `tests.md`).
16. `orbit init --skill <name>`, repeatable, copies the named shipped skills' files into
    `.orbit/skills/<name>/`, keeping any file already there; it works with or without `--update`
    and is refused with `--force` with the existing message; over `--skills` copying all 18 (it
    freezes every skill against updates) and copying only each `SKILL.md` (the detail is in the
    resource files).
17. Its messages: `Copied python's files to .orbit/skills/python/.` · `Kept your
    .orbit/skills/python/tests.md.` · an unknown name exits with status 2: `No skill named pyton;
    orbit's skills are application, code-style, …`; over skipping unknown names with a warning (a
    typo looks like success).
18. The reference config: the role field reads `The skills its agents get, by name: orbit's own,
    or the project's in .orbit/skills/.`; a commented-out `[[skills]]` example after the roles
    reads `Skills for the code under some paths, given to every agent along with its role's.`
    (followed by ` For example:`, as the reference's other examples are), with `paths = ["src"]`
    described `Files or folders in the project, relative to its root; each must exist.` and
    `use = ["application", "python"]` described `The skills that apply there, by name.`; over no
    example (`[[skills]]` would go undocumented). A built-in role's list whose line would pass 100
    columns, as the reviewer's skills do, is written in the short form the reference already uses
    for instructions and unset fields, `# skills: 8 of orbit's own; orbit config show
    roles.reviewer.skills lists them`; over continuation lines (`orbit init --update`'s scanner,
    the reference test and orbit's own config would all need multi-line values) and exempting the
    line from the 100-column test (it weakens a check).
19. The README describes skills, their layers and overrides, `skills` and `[[skills]]`, what
    `config show` lists and `init --skill`, each with the task that brings it.

## How it will be built

1. Write this record.
2. Load the shipped skills: the drafts move into `src/orbit/agent/external/skills/`;
   `orbit.agent.domain.skills` holds `Skill`, `SkillFile`, `SkillLibrary`, `parse_front_matter`
   and the validation of decisions 12 and 14; `orbit.agent.external.skill_files` holds
   `SKILL_DIRECTORY` and `read_skill_library(root)`; the loader sets
   `OrbitConfig.skill_library`, which a file can't.
3. Let a project override and add skills: `.orbit/skills/` read over the package, whole file by
   whole file, each `SkillFile` with its `layer` (`PACKAGE`, or the project layer with the file's
   own path).
4. Give roles skills: `Role.skills`, the defaults, unknown names refused, the reference line,
   with long lists in the short form.
5. Map paths to skills: `[[skills]]` as `OrbitConfig.skills: tuple[SkillMapping, ...]` with
   `paths` and `use`, names and paths checked, the reference example.
6. List skills in `orbit config show`: `skills.N.paths`, `skills.N.use` and
   `skill_files.<skill>.<file>`.
7. Copy skills with `orbit init --skill`: `add_skills(root, names)`.

## Deferred

- Refining the planner's skills.
- `PutSkill` personal skills, and a user layer for skills.
