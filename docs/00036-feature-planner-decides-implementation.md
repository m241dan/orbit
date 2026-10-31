# Planner decides implementation requirements

The decisions and plan for having the planner spend its questions on behaviour, edge cases and
outcomes, and decide the implementation itself.

## Goal

The user agrees with the planner's recommendations on implementation details almost every time,
and skills will carry the project's conventions. The planner asks about what users see or rely
on, decides the rest, and shows what it decided before anything is proposed.

## Behaviour

1. The planner asks about what users see or rely on: behaviour, edge cases, failure outcomes,
   interactions with existing features, stored data, and scope including new dependencies; it
   decides the implementation itself, following the project's guidance and skills, user-visible
   wording included; over asking about wording too (rounds the user answers with the
   recommendation) and asking only about behaviour and edge cases (a new dependency or a dropped
   feature could arrive unasked).
2. Implementation that's hard to undo is still asked, with a recommendation: a stored format or
   migration, a wire or protocol change, a public API, a new dependency; over deciding these too
   (a wrong call costs a migration rather than a refactor).
3. The planner's own choices appear in a **Decided for you** section after the requirements, each
   with its rejected option, and go into the record like any decision; over mixing them into the
   requirements (harder to skim) and not showing them before drafts (the surprise the user
   objected to before).
4. While writing prompts, the planner settles implementation details itself and names them in
   the line closing its proposals; a behavioural gap still pauses proposing for a question; over
   pausing for every choice (the round-trip this removes).
5. Once a behaviour is settled, its edge cases come as a numbered table of `Given … → outcome`
   rows, each with a proposed outcome, which the user accepts whole or overrides by row; a case
   with two plausible outcomes and no obvious one stays a full question; over a question per case
   (slow for obvious ones) and a table for every case (real forks get skimmed).
6. The requirements, **Decided for you** and the outline arrive in one message with one confirm,
   and a change brings the revised whole; over two gates (an extra turn on every plan) and a size
   threshold (a rule to remember).
7. Following from these: the lenses stay, Wording becoming the planner's to choose; walking each
   task before posting stays; rounds, recommendations and "yes to all" are unchanged.
8. Following from CLAUDE.md: the README's planner paragraph describes the new shape, and the
   planner's tests check the new passages and that the replaced ones are gone.

## Decided for you

9. The passages, copied exactly into `planner.md`, as task 2 lists them; over looser paraphrases
   (the tests check exact passages, as they do today).
10. The README's planner sentence describes the new shape, as task 2 gives it; over leaving it
    saying every choice is asked.
11. The change is one task after this record; over splitting the passages (a half-changed planner
    would contradict itself).

## How it will be built

1. Write this record.
2. Let the planner decide the implementation: the passages in
   `src/orbit/agent/external/instructions/planner.md`, `PLANNER_ASKS_BEFORE_PROPOSING` and the
   tests on it in `tests/test_built_in_roles.py`, and the README's planner sentence.

## Supersedes

Decisions of `docs/00019-bugfix-planner-asks-before-proposing.md` that put every choice to the
user: an implementation choice is now the planner's, shown under Decided for you.
