"""Merge holds: a set that holds merges failing holds them, recording what failed, until a fix
lands and the set, run again, passes; the fixes it queues meanwhile, up to a limit, and what the
user is told as a hold starts, lifts or needs them."""

from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from functools import partial, reduce
from typing import Literal

from orbit.agent.domain.commits import Commit
from orbit.agent.domain.verification import CheckSet, VerificationStep
from orbit.agent.domain.wording import counted
from orbit.protocol import MergeHold, TaskState

FIX_TITLE = "Fix the failing set: "
"""What the title of a task fixing a set that holds merges starts with, before the set's name."""

SHORT_COMMIT = 7
"""How many characters of a commit's id a notice shows."""

_FIX_INSTRUCTIONS = (
    "Find and fix the cause so the set passes again, leaving its checks as strong as they were:"
    " the same thresholds, the same tests, the same code under coverage. Merges into the user's"
    " branch are held until it passes; your work lands as soon as it's verified."
)


@dataclass(frozen=True)
class Hold:
    """The set ``name``'s hold on merges: it failed on ``commit`` at the step named ``step``
    with ``output``, and ``fixes`` fixes were queued for it since the hold started."""

    name: str
    commit: str
    step: str
    output: str
    fixes: int = 0


@dataclass(frozen=True)
class HoldDecision:
    """What a set's failure comes to: the ``holds`` after it, the ``notes`` to tell the user,
    in order, and whether a ``fix`` is to be queued for it."""

    holds: tuple[Hold, ...]
    notes: tuple[str, ...] = ()
    fix: bool = False


def held_note(name: str, commit: str) -> str:
    """The notice that the set ``name`` failing on ``commit`` holds merges."""
    return f"Merges held: {name} failed on {commit[:SHORT_COMMIT]}. A fix is queued first."


def resumed_note(name: str) -> str:
    """The notice that the set ``name`` passed, lifting its hold."""
    return f"Merges resumed: {name} passed."


NOT_HELD = "Merges aren't held."
"""Why releasing the holds on merges by hand is refused while none is on."""


def unpassed(names: Sequence[str]) -> str:
    """That the sets ``names`` haven't passed: ``stress hasn't passed``, ``stress, soak haven't
    passed``."""
    verb = "hasn't" if len(names) == 1 else "haven't"
    return f"{', '.join(names)} {verb} passed"


def released_note(names: Sequence[str]) -> str:
    """The notice that the user released the holds of the sets ``names`` by hand."""
    return f"Merges resumed by hand; {unpassed(names)}."


def step_in_note(name: str, fixes: int) -> str:
    """The notice that the set ``name`` still fails after ``fixes`` fixes, so the user must step
    in."""
    return (
        f"{name} still fails after {counted(fixes, 'fix', 'fixes')}; merges stay held. Release"
        " with ctrl+f or fix it yourself."
    )


def find_hold(holds: Sequence[Hold], name: str) -> Hold | None:
    """The set ``name``'s hold among ``holds``; none if it holds none."""
    return next((hold for hold in holds if hold.name == name), None)


def decide_failure(holds: Sequence[Hold], failed: Hold, limit: int, fixing: bool) -> HoldDecision:
    """What ``failed``, a run of a set that holds merges failing, comes to, given the ``holds``
    on and the ``limit`` of fixes a hold queues: a new hold is told of and queues the first
    fix; a set held already keeps its hold with the new failure, queuing another fix unless one
    is ``fixing`` it still or ``limit`` were queued, when the user is asked to step in."""
    held = find_hold(holds, failed.name)
    others = lift_hold(holds, failed.name)
    tried = 0 if held is None else held.fixes
    notes = () if held is not None else (held_note(failed.name, failed.commit),)
    if fixing:
        return HoldDecision((*others, replace(failed, fixes=tried)), notes)
    if tried >= limit:
        return HoldDecision(
            (*others, replace(failed, fixes=tried)), (*notes, step_in_note(failed.name, tried))
        )
    return HoldDecision((*others, replace(failed, fixes=tried + 1)), notes, fix=True)


def decide_unlanded_fix(holds: Sequence[Hold], name: str, limit: int, fixing: bool) -> HoldDecision:
    """What a chain of fixes for the set ``name`` ending without its work merging comes to,
    given the ``holds`` on and the ``limit`` of fixes a hold queues: another failure of its
    hold, as the failure it recorded, as :func:`decide_failure` says; nothing while a fix is
    ``fixing`` it still, or if the set holds none."""
    held = find_hold(holds, name)
    if held is None or fixing:
        return HoldDecision(tuple(holds))
    return decide_failure(holds, held, limit, fixing=False)


def lift_hold(holds: Sequence[Hold], name: str) -> tuple[Hold, ...]:
    """``holds`` without the set ``name``'s."""
    return tuple(hold for hold in holds if hold.name != name)


def holding_sets(sets: Iterable[CheckSet]) -> tuple[CheckSet, ...]:
    """The ``sets`` that hold merges when they fail."""
    return tuple(check_set for check_set in sets if check_set.effect == "holds_merges")


def held_step(sets: Iterable[CheckSet], hold: Hold) -> VerificationStep | None:
    """The step ``hold``'s set failed at, among ``sets``; none if no set of its name has a step
    of that name, as when the set changed since."""
    steps = (step for check_set in sets if check_set.name == hold.name for step in check_set.steps)
    return next((step for step in steps if step.name == hold.step), None)


def kept_holds(holds: Sequence[Hold], holding: Collection[str]) -> tuple[Hold, ...]:
    """``holds`` of the sets still ``holding`` merges, by name, as a saved hold is restored:
    those of sets removed, or that only report now, are dropped."""
    return tuple(hold for hold in holds if hold.name in holding)


def merge_holds(earlier: Sequence[Hold], later: Sequence[Hold]) -> tuple[Hold, ...]:
    """The holds of ``earlier`` and ``later``, a set's hold in ``later`` replacing its own in
    ``earlier``."""
    named = {hold.name for hold in later}
    return (*(hold for hold in earlier if hold.name not in named), *later)


type Lifted = frozenset[str] | Literal["every"]
"""The sets whose holds were lifted since the holds kept were last read or saved, by name, or
``"every"`` once the user released every hold by hand."""

NONE_LIFTED: Lifted = frozenset()
"""That no hold was lifted since the holds kept were last read or saved."""


def note_lift(lifted: Lifted, name: str) -> Lifted:
    """``lifted``, with the set ``name``'s hold among those lifted."""
    return lifted | {name} if isinstance(lifted, frozenset) else lifted


def unlifted_sets(holding: Collection[str], lifted: Lifted) -> frozenset[str]:
    """The sets still ``holding`` merges, by name, whose holds weren't ``lifted`` since the
    holds kept were last read or saved, so a hold kept for one of them still counts."""
    return frozenset(holding) - lifted if isinstance(lifted, frozenset) else frozenset()


def fold_holds(
    loaded: Sequence[Hold], holding: Collection[str], lifted: Lifted, started: Sequence[Hold]
) -> tuple[Hold, ...]:
    """The holds to take on from ``loaded``, those kept, read afresh: those of the sets still
    ``holding`` merges, by name, and not ``lifted`` since they were last read or saved, as
    :func:`unlifted_sets` and :func:`kept_holds` say, with the holds ``started`` since then
    replacing their sets', as :func:`merge_holds` says."""
    return merge_holds(kept_holds(loaded, unlifted_sets(holding, lifted)), started)


def hold_view(hold: Hold) -> MergeHold:
    """``hold`` as frontends see it: the set, the commit it failed on and why."""
    return MergeHold(set=hold.name, commit=hold.commit, reason=f"{hold.step} failed")


def fix_title(name: str) -> str:
    """The title of a task fixing the set ``name``."""
    return f"{FIX_TITLE}{name}"


def set_fix_prompt(hold: Hold, subjects: Sequence[str] | None, report: str) -> str:
    """A task prompt asking to fix why ``hold``'s set failed, listing the ``subjects`` of the
    commits since it last passed, none if that isn't known, and the failure's ``report``. Its
    first line is :func:`fix_title`."""
    if subjects is None:
        since = "Orbit hasn't seen it pass since it started, so it can't list the commits since."
    elif subjects:
        since = "The commits since it last passed:\n" + "".join(f"\n- {s}" for s in subjects)
    else:
        since = "No commit landed since it last passed."
    return (
        f"{fix_title(hold.name)}\n\n"
        f"The set {hold.name} failed on {hold.commit[:SHORT_COMMIT]} at its step {hold.step}."
        f" {since}\n\n{report}\n\n{_FIX_INSTRUCTIONS}"
    )


def fixed_set(prompt: str) -> str | None:
    """The set a task with ``prompt`` fixes, as its title names it; none if it fixes none."""
    title = prompt.partition("\n")[0]
    return title.removeprefix(FIX_TITLE) if title.startswith(FIX_TITLE) else None


def fix_root(state: TaskState, find: Callable[[str], TaskState | None]) -> TaskState:
    """The task ``state``'s chain of fixes started from: the task it fixes, the one that fixes,
    and so on, as ``find`` finds them by id, up to the first that fixes none or isn't found."""
    found = None if state.fixes is None else find(state.fixes)
    return state if found is None else fix_root(found, find)


def stacked_commits(
    commits: Sequence[Commit], pieces: Sequence[Sequence[str]]
) -> tuple[str | None, ...]:
    """The last commit of each of the ``pieces`` of work stacked among ``commits``, oldest
    first, in that order, each piece given by its commits' subjects, oldest first.

    Each piece is found by its subjects, the last piece first, back from the end: its commits
    are the run ending at the last commit with one of its subjects, before the pieces after it,
    reaching back as far as the commits are its own, in order. Commits of no piece, as those of
    the base moving beneath it or of work that didn't wait, are passed over. A piece none of
    whose subjects is found, as a rebase drops the commits it leaves empty, has none; one whose
    subjects aren't known, the last of ``commits``. Subjects can repeat, so a piece whose
    subjects another commit before it shares, and none of whose own is left, can be given that
    commit."""
    no_runs: tuple[range, ...] = ()
    runs = reduce(partial(_run_beneath, commits), reversed(pieces), no_runs)
    return tuple(
        _last_of(commits, subjects, run) for subjects, run in zip(pieces, runs, strict=True)
    )


def _run_beneath(
    commits: Sequence[Commit], found: tuple[range, ...], subjects: Sequence[str]
) -> tuple[range, ...]:
    """The runs of ``commits`` ``found`` for the pieces after the one with ``subjects``, with
    its own before them: empty, where they start, if it's not found."""
    end = found[0].start if found else len(commits)
    last = next((i for i in reversed(range(end)) if commits[i].subject in subjects), None)
    run = range(end, end) if last is None else range(_run_start(commits, subjects, last), last + 1)
    return (run, *found)


def _run_start(commits: Sequence[Commit], subjects: Sequence[str], index: int) -> int:
    """Where the run of ``commits`` ending at ``index``, one of the ``subjects``, starts:
    reaching back as far as the commits before it have the subjects before its own, in
    order."""
    subject = commits[index].subject
    before = subjects[: len(subjects) - 1 - subjects[::-1].index(subject)]
    if index == 0 or commits[index - 1].subject not in before:
        return index
    return _run_start(commits, before, index - 1)


def _last_of(commits: Sequence[Commit], subjects: Sequence[str], run: range) -> str | None:
    """The last commit of the piece with ``subjects`` whose commits are the ``run`` of
    ``commits``: the last of all if its subjects aren't known; none if its run is empty."""
    if not subjects:
        return commits[-1].id if commits else None
    return commits[run[-1]].id if run else None
