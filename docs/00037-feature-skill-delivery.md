# Skill delivery requirements

The decisions and plan for milestones 11b to 11d of `scratch/control-plane-design.md`: skills
delivered to every agent, tasks carrying the skills they need, reviews bounded and given their
lenses, and Orbit's own skills moved into the project. `scratch/skills-design.md` holds the
settled design and `docs/00035-feature-skills-as-configuration.md` the skills' configuration.

## Goal

Every agent, on any runtime, works from the skills its role and its task call for, chosen
deterministically: roles name their lenses, the planner names each task's language and kind, and
orbit passes those lists on. Reviews know what landed since; super reviews read what changed and
a rotating sample of the rest. Orbit's own conventions live in its skills, with `CLAUDE.md` a
pointer.

## Delivery

1. `read_skill` reads only files of the skills the agent was given; anything else is refused:
   `You weren't given the skill pyton; your skills are code-style, python, …`; over reading any
   skill (agents wander into guidance meant for other roles).
2. The planner chooses each task's skills (its language and kind, and project skills such as
   `layers`) from a catalogue of names and descriptions in its instructions, and passes them to
   `propose_task`; tasks carry them (`TaskSpec.skills`, a wire change); over a skill-finding
   consultant (a consultation and a wait per plan to save some 2,000 tokens) and deriving them
   from paths (it can't tell kinds apart within a folder).
3. Orbit passes exactly a task's list: the agent gets its role's skills plus the task's; a task
   without a list gets every mapped skill; over the list replacing the role's skills (a task
   would drop `security`).
4. Every source is deterministic: a reviewer gets its role's lenses plus the reviewed task's list
   (for a batch, the union of its tasks' lists); a reviewer's suggestions and fix tasks for a
   failing check inherit their source task's list; the super reviewer gets its lenses plus every
   mapped skill; over choosing by changed paths and reviewers naming skills from a catalogue.
5. A task's row shows its skills, and the task editor has a **Skills** field, checked when
   saved; over showing without editing, and neither.
6. An unknown skill name in `propose_task` is refused: `Task skills names pyton, which isn't a
   skill; the skills are …`; over dropping it silently.
7. `orbit config show --skills` prints the project's resolved map: each skill, which agents get
   it and from where; over a static README section (it shows defaults and drifts).

## Reviews

8. Until orbit can hold merges, a blocking finding (Bugs, Security) is the most severe kind:
   suggested first, its prompt saying why it would block; over ignoring it until milestone 12
   (the signal is lost).
9. A per-commit review's task lists the subjects of commits landed since the reviewed one; none,
   no list; past 40, the first 40 then `… and N more`; over no list (already-fixed findings keep
   coming) and `git log` access (it breaks restricted access).
10. A super review covers the project's shape, the commits since the last super review (their
    subjects, and the files they changed, capped at 100 then `… and N more`), and a sample of
    other code files; over the whole codebase unbounded and commits only.
11. The sample size is a new limit, `super_review_sample`, default 10; 0 means no sample; over a
    fixed 10 and a share of the codebase.
12. Sampled files are tracked files with a built-in source extension, within the `[[skills]]`
    paths when any are mapped, leaving out files the commits since changed; over every tracked
    file, configurable globs, and extensions taken from language skills (it ties code to skill
    names).
13. The sample rotates: files never sampled first, then those sampled longest ago, ties at
    random; saved per repository beside the super-review count, removed files forgotten; over
    pure chance (files go unseen for long stretches).
14. The super reviewer uses only `review-style`, `review-drift` and `review-scaling`; over keeping
    latent bugs and tests until milestone 12 (the noisiest findings in the mined history).

## Orbit's own

15. `CLAUDE.md` becomes a pointer to the shipped skills and `.orbit/skills/`, telling a session to
    read `code-style`, `python`, `layers` and `orbit`; over `.orbit/skills/` only (interactive
    sessions lose two skills) and keeping the full file too (two sources drift apart).
16. When Orbit's own work brings in a language whose extension isn't on the list, the planner adds
    it in the same plan.

## Edge cases

17. - `read_skill` for a file the skill lacks: `python has no file tests2.md; its files are
      SKILL.md, async.md, …`; a path with `..` or an absolute one is refused the same way.
    - `read_skill` for `SKILL.md` returns it.
    - A skill edited while a session is open: agents get the version read when the workspace
      opened, until it opens again.
    - A role with no skills, and no mapping: instructions exactly as today, with no reading line.
    - A skill from both the role and the task or mapping: delivered once.
    - A very large project skill: delivered whole, with no limit.
    - A conversational role's skills changed between turns: the next turn carries them.
    - Nothing landed since the reviewed commit: no list. More than 40: the first 40 and
      `… and N more`.
    - A Claude agent in Orbit also loading the pointer `CLAUDE.md`: harmless.
    - The first super review in a repository, or ctrl+g with nothing landed since: no commit list.
    - Fewer eligible files than the sample size: all of them. More than 100 changed files: the
      first 100 and `… and N more`.
    - A sampled file removed since: forgotten from the rotation. A sample size of 0: no sample.

## Decided for you

18. Instructions are `AGENT_RULES`, the role's instructions, then `## Skills`: the reading line
    ("A line such as "Read `code-style`'s `testing.md`." means: call read_skill with skill
    `code-style` and file `testing.md`."), each skill's body under its name (the role's first,
    each once), and, when mapped skills were delivered, a line per mapping entry ("`python`,
    `application` apply under `src`, `tests`."). A role holding `propose_task` also gets
    `## Skills you can give tasks`, one `name: description` line per skill in the library. Over
    per-runtime wording (`AGENT_RULES` already names each runtime's tool prefix).
19. `read_skill(skill, file)` comes with every role; naming it in `tools` is refused; an agent with
    no skills is told `You weren't given any skills.`; over granting it per role (a role can't
    see the mapping).
20. Spans: `read_skill` calls carry `orbit.skill.name` and `orbit.skill.file`; the process span
    gets `orbit.agent.skills` and `orbit.agent.skill_files` (files read, sorted, once each); over
    a span per skill.
21. `TaskSpec.skills` is additive, empty by default, with no protocol version bump, as earlier
    additive fields were. The row shows ` · skills: python, application`; the editor's **Skills**
    field comes after **Role**, takes comma-separated names, and refuses unknown ones with the
    message of decision 6. `edit_draft` keeps a draft's skills.
22. The landed-since list comes from git in the review lane. The super review's start is the last
    super review's commit, saved per repository with its count; rotation saves the number of the
    super review that last sampled each file, so the domain needs no clock. A reviewed task no
    longer on the board contributes no list.
23. `CODE_EXTENSIONS` in the domain: `.py`, `.pyi`, `.c`, `.cc`, `.cpp`, `.cxx`, `.h`, `.hh`,
    `.hpp`, `.rs`, `.go`, `.java`, `.kt`, `.js`, `.jsx`, `.ts`, `.tsx`, `.cs`, `.swift`, `.rb`.
24. `reviewer.md`: the lens list becomes its `review-*` skills; it gains decision 8's rule and the
    habits from the mined history (check the queued and landed-since lists and drop what they
    cover; flag a finding a landed commit may have overtaken; confirm a framework's behaviour
    before demanding a test that must fail; write Done items that can all hold at once); Clarity
    leaves.
25. `super-reviewer.md`: its list becomes its three lenses, scoped to the shape, the commits since
    and the sample, with the same habits; `review-drift` and `review-scaling` ask "across the
    commits since the last super review and the sampled files"; `coder.md`'s test-isolation line
    defers to `code-style`.
26. Orbit's `[[skills]]`: `paths = ["src", "tests"]`, `use = ["application", "python", "layers",
    "orbit"]`; decision 16 lives in the `orbit` skill; `docs/drafts/` goes when Orbit's skills
    move out of it.
27. The five-parameter AST test covers `src` and `tests`, counting every named parameter but a
    method's `self` or `cls`; a CLI command or an Entry function (`orbit.cli`, `orbit.__main__`)
    may pass the limit with the marker the shipped `python` skill already names,
    `# #lizard forgives(parameter_count): <why>`; the Lizard step drops `-a 5`.
28. `--skills` prints a row per skill and a column per role, each cell `role`, `task`,
    `reviewed task`, `mapped` or `—`, with a line explaining each value.

## How it will be built

1. Write this record.
2. Let agents read skill files: `read_skill`, `READ_SKILL` through `Role.granted_tools`, its
   refusals and request-span attributes.
3. Deliver skills in agents' instructions: `skill_instructions(role_skills, task_skills,
   mappings, library)` in `orbit.agent.domain.skills`, used by `AgentDesk.launch`; the process
   span's attributes.
4. Give tasks skills: `TaskSpec.skills`, delivered as decision 3 says; `edit_draft` keeps them.
5. Let the planner choose tasks' skills: `propose_task`'s `skills` and its refusal, the catalogue
   for roles holding `propose_task`, and the planner's passage.
6. Show and edit a task's skills: the row and the editor's **Skills** field.
7. Inherit skills for reviews, suggestions and fix tasks.
8. Map skills in `orbit config show --skills`.
9. Tell reviewers what has landed since: `review_prompt`'s `landed_since`, read from git.
10. List the commits since the last super review: `last_super_commit` saved with the count.
11. Sample code files in super reviews: `super_review_sample`, `CODE_EXTENSIONS`,
    `sample_files(…)`, at random.
12. Rotate the sample: `super_reviews` and `sampled` saved with the count.
13. Give the reviewer its lens skills: `reviewer.md`.
14. Give the super reviewer its lens skills: `super-reviewer.md`, two lenses, `coder.md`.
15. Move Orbit's skills into the project: `.orbit/skills/`, `[[skills]]`, `docs/drafts/` removed.
16. Make `CLAUDE.md` a pointer.
17. Enforce the parameter limit with an AST test.

## Deferred

- The review rules of decisions 4, 9 and 10 becoming trigger data in milestone 12.
- A skill-finding consultant, should catalogues grow large.
- Refining the planner's own skills.
