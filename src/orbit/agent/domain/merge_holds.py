"""Merge holds: a set that holds merges failing holds them, recording what failed, until a fix
lands and the set, run again, passes; a blocking finding a merge pass files holds them too, until
the review of its fix by the lens that raised it files no blocking task again; the fixes each
queues meanwhile, up to a limit, and what the user is told as a hold starts, lifts or needs
them."""

from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from functools import partial, reduce
from typing import Literal, assert_never

from orbit.agent.domain.commits import Commit
from orbit.agent.domain.verification import CheckSet, VerificationStep
from orbit.agent.domain.wording import counted
from orbit.protocol import MergeHold, TaskState
from orbit.wording import word_undone

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

    @property
    def key(self) -> str:
        """What tells the hold apart from the others: its set's name."""
        return self.name


@dataclass(frozen=True)
class FindingHold:
    """A blocking finding's hold on merges: the lens ``lens`` raised it in the review of
    ``commit``, and a merge pass filed it as the task titled ``title``. ``task`` is its latest
    fix, in ``batch``, a batch of its own, until a review checks it, none once no review can,
    and ``fixes`` fixes were queued for it since the hold started."""

    lens: str
    commit: str
    title: str
    task: str
    batch: str | None
    fixes: int = 1

    @property
    def key(self) -> str:
        """What tells the hold apart from the others, whichever fix it's on: its lens, commit
        and title."""
        return f"finding:{self.lens}:{self.commit}:{self.title}"


type AnyHold = Hold | FindingHold
"""A hold on merges: a named set's, or a blocking finding's."""


@dataclass(frozen=True)
class HoldDecision:
    """What a set's failure, or a blocking finding's filing or review, comes to: the ``holds``
    after it, the ``notes`` to tell the user, in order, and whether a ``fix`` is to be queued for
    a set."""

    holds: tuple[AnyHold, ...]
    notes: tuple[str, ...] = ()
    fix: bool = False


@dataclass(frozen=True)
class BlockingTask:
    """A task a merge pass filed as blocking, titled ``title``: the lens ``lens`` raised the
    first blocking finding it files, and the ``other_lenses`` the rest, in order, in the review
    of ``commit``, which reviewed the batch ``reviewed``, none for a review of no batch."""

    lens: str
    commit: str
    title: str
    reviewed: str | None
    other_lenses: tuple[str, ...] = ()

    @property
    def lenses(self) -> tuple[str, ...]:
        """The lenses that raised the blocking findings it files, each once, in order."""
        return tuple(dict.fromkeys((self.lens, *self.other_lenses)))


type FindingFiling = Literal["held", "fixed_again", "stepped_in"]
"""How a blocking task is filed: as the first fix of the hold it starts, as the next fix of the
hold whose latest fix its review checked, or, that hold's fixes having run out, as a normal
suggestion, the hold staying."""

BLOCKING_REASON = "blocking finding"
"""Why a blocking finding's hold holds merges, as frontends see it."""


def note_held(name: str, commit: str) -> str:
    """The notice that the set ``name`` failing on ``commit`` holds merges."""
    return f"Merges held: {name} failed on {commit[:SHORT_COMMIT]}. A fix is queued first."


def word_resumed(name: str) -> str:
    """The notice that the set ``name`` passed, lifting its hold."""
    return f"Merges resumed: {name} passed."


NOT_HELD = "Merges aren't held."
"""Why releasing the holds on merges by hand is refused while none is on."""


def word_released(names: Sequence[str], lenses: Sequence[str] = ()) -> str:
    """The notice that the user released the holds of the sets ``names`` and of the findings
    the ``lenses`` raised by hand, saying what that leaves undone as
    :func:`orbit.wording.word_undone` words it."""
    return f"Merges resumed by hand; {word_undone(names, lenses)}."


def note_finding_held(hold: FindingHold) -> str:
    """The notice that the blocking finding ``hold`` is about holds merges."""
    return (
        f"Merges held: a blocking finding from {hold.lens} in {hold.commit[:SHORT_COMMIT]}:"
        f" {hold.title}. Its fix is queued first."
    )


def word_finding_resumed(lens: str) -> str:
    """The notice that the finding the lens ``lens`` raised is fixed, lifting its hold."""
    return f"Merges resumed: the finding from {lens} is fixed."


def word_finding_step_in(lens: str, fixes: int) -> str:
    """The notice that the finding the lens ``lens`` raised still holds after ``fixes`` fixes,
    so the user must step in."""
    return (
        f"The finding from {lens} still holds after {counted(fixes, 'fix', 'fixes')}; merges stay"
        " held. Release with ctrl+f or fix it yourself."
    )


def word_unchecked(lens: str) -> str:
    """The notice that no review can check the fix of the finding the lens ``lens`` raised, so
    the user must step in."""
    return (
        f"The finding from {lens} can't be checked by a review; merges stay held. Release with"
        " ctrl+f or fix it yourself."
    )


def word_step_in(name: str, fixes: int) -> str:
    """The notice that the set ``name`` still fails after ``fixes`` fixes, so the user must step
    in."""
    return (
        f"{name} still fails after {counted(fixes, 'fix', 'fixes')}; merges stay held. Release"
        " with ctrl+f or fix it yourself."
    )


def find_hold(holds: Sequence[AnyHold], name: str) -> Hold | None:
    """The set ``name``'s hold among ``holds``; none if it holds none."""
    return next((hold for hold in filter_set_holds(holds) if hold.name == name), None)


def filter_set_holds(holds: Sequence[AnyHold]) -> tuple[Hold, ...]:
    """The named sets' holds among ``holds``, in order."""
    return tuple(hold for hold in holds if isinstance(hold, Hold))


def filter_finding_holds(holds: Sequence[AnyHold]) -> tuple[FindingHold, ...]:
    """The blocking findings' holds among ``holds``, in order."""
    return tuple(hold for hold in holds if isinstance(hold, FindingHold))


def decide_failure(
    holds: Sequence[AnyHold], failed: Hold, limit: int, fixing: bool
) -> HoldDecision:
    """What ``failed``, a run of a set that holds merges failing, comes to, given the ``holds``
    on and the ``limit`` of fixes a hold queues: a new hold is told of and queues the first
    fix; a set held already keeps its hold with the new failure, queuing another fix unless one
    is ``fixing`` it still or ``limit`` were queued, when the user is asked to step in."""
    held = find_hold(holds, failed.name)
    others = lift_hold(holds, failed.name)
    tried = 0 if held is None else held.fixes
    notes = () if held is not None else (note_held(failed.name, failed.commit),)
    if fixing:
        return HoldDecision((*others, replace(failed, fixes=tried)), notes)
    if tried >= limit:
        return HoldDecision(
            (*others, replace(failed, fixes=tried)), (*notes, word_step_in(failed.name, tried))
        )
    return HoldDecision((*others, replace(failed, fixes=tried + 1)), notes, fix=True)


def decide_unlanded_fix(
    holds: Sequence[AnyHold], name: str, limit: int, fixing: bool
) -> HoldDecision:
    """What a chain of fixes for the set ``name`` ending without its work merging comes to,
    given the ``holds`` on and the ``limit`` of fixes a hold queues: another failure of its
    hold, as the failure it recorded, as :func:`decide_failure` says; nothing while a fix is
    ``fixing`` it still, or if the set holds none."""
    held = find_hold(holds, name)
    if held is None or fixing:
        return HoldDecision(tuple(holds))
    return decide_failure(holds, held, limit, fixing=False)


def lift_hold(holds: Sequence[AnyHold], key: str) -> tuple[AnyHold, ...]:
    """``holds`` without the one whose key is ``key``, such as a set's name."""
    return tuple(hold for hold in holds if hold.key != key)


def find_refiled_hold(holds: Sequence[AnyHold], filed: BlockingTask) -> FindingHold | None:
    """The hold among ``holds`` whose latest fix the review that filed ``filed`` checked, if
    the hold's lens raised one of its blocking findings; none if ``filed`` starts a hold of its
    own."""
    return next(
        (
            hold
            for hold in filter_finding_holds(holds)
            if hold.lens in filed.lenses and hold.batch is not None and hold.batch == filed.reviewed
        ),
        None,
    )


def decide_filing(holds: Sequence[AnyHold], filed: BlockingTask, limit: int) -> FindingFiling:
    """How ``filed`` is filed, given the ``holds`` on and the ``limit`` of fixes a hold queues:
    as the next fix of the hold whose fix its review checked, as :func:`find_refiled_hold` finds it,
    unless that hold queued ``limit`` fixes already; else as the first fix of a hold of its
    own."""
    held = find_refiled_hold(holds, filed)
    if held is None:
        return "held"
    return "stepped_in" if held.fixes >= limit else "fixed_again"


def decide_blocking(
    holds: Sequence[AnyHold], filed: BlockingTask, task: str, batch: str, limit: int
) -> HoldDecision:
    """What ``filed``, filed as the task ``task`` in ``batch``, comes to, given the ``holds`` on
    and the ``limit`` of fixes a hold queues, as :func:`decide_filing` says: a new hold is told
    of; a hold fixed again takes the task as its latest fix; one whose fixes ran out stays,
    checked by no review, and the user is asked to step in."""
    held = find_refiled_hold(holds, filed)
    if held is None:
        new = FindingHold(filed.lens, filed.commit, filed.title, task, batch)
        return HoldDecision((*holds, new), (note_finding_held(new),))
    if held.fixes >= limit:
        note = word_finding_step_in(held.lens, held.fixes)
        return HoldDecision(merge_holds(holds, (replace(held, batch=None),)), (note,))
    again = replace(held, task=task, batch=batch, fixes=held.fixes + 1)
    return HoldDecision(merge_holds(holds, (again,)))


type MergePassEnding = Literal["completed", "incomplete"]
"""How a review's merge passes ended: one completed, deciding every finding, or none did, as when
it failed, gave up or was cancelled, or none ran."""


@dataclass(frozen=True)
class FixReview:
    """The end of the review of ``batch``: the roles whose tasks completed in it ``completed``,
    those whose tasks reported a blocking finding in it ``blocking``, and how its merge passes
    ended, ``merged``."""

    batch: str
    completed: frozenset[str]
    blocking: frozenset[str] = frozenset()
    merged: MergePassEnding = "incomplete"


def decide_fix_review(holds: Sequence[AnyHold], review: FixReview) -> HoldDecision:
    """What ``review``, the end of the review of a batch, given the ``holds`` on, comes to: each
    hold whose latest fix is that batch's lifts, told of, if its lens completed and either
    reported no blocking finding or a merge pass completed, deciding them; else it stays, told no
    review can check it."""
    checked = _fixed_in(holds, review.batch)
    fixed = tuple(hold for hold in checked if _is_cleared(hold, review))
    unchecked = tuple(hold for hold in checked if hold not in fixed)
    notes = tuple(word_finding_resumed(hold.lens) for hold in fixed)
    return _with_unchecked(HoldDecision(_without(holds, fixed), notes), unchecked)


def _fixed_in(holds: Sequence[AnyHold], batch: str) -> tuple[FindingHold, ...]:
    """The findings' holds among ``holds`` whose latest fix is ``batch``'s."""
    return tuple(hold for hold in filter_finding_holds(holds) if hold.batch == batch)


def _is_cleared(hold: FindingHold, review: FixReview) -> bool:
    """Whether ``review`` clears ``hold``: its lens completed in it, and reported no blocking
    finding or a merge pass completed, dismissing each or filing it as normal."""
    decided = hold.lens not in review.blocking or review.merged == "completed"
    return hold.lens in review.completed and decided


def _without(holds: Sequence[AnyHold], dropped: Collection[AnyHold]) -> tuple[AnyHold, ...]:
    """``holds`` without those ``dropped``."""
    return tuple(hold for hold in holds if hold not in dropped)


def decide_unreviewed(
    holds: Sequence[AnyHold], unreviewed: Collection[FindingHold]
) -> HoldDecision:
    """What the batches of the latest fixes of the ``unreviewed`` holds among ``holds`` closing
    with no review by their lenses comes to: each stays, checked by no review, telling the
    user."""
    return _with_unchecked(HoldDecision(tuple(holds)), unreviewed)


def _with_unchecked(decision: HoldDecision, unchecked: Collection[FindingHold]) -> HoldDecision:
    """``decision``, with the ``unchecked`` holds among its holds checked by no review, and
    notes telling the user so after its own."""
    holds = tuple(_unchecked(hold) if hold in unchecked else hold for hold in decision.holds)
    notes = (*decision.notes, *(word_unchecked(hold.lens) for hold in unchecked))
    return replace(decision, holds=holds, notes=notes)


def _unchecked(hold: AnyHold) -> AnyHold:
    """``hold``, a finding's, checked by no review."""
    return replace(hold, batch=None) if isinstance(hold, FindingHold) else hold


def holding_sets(sets: Iterable[CheckSet]) -> tuple[CheckSet, ...]:
    """The ``sets`` that hold merges when they fail."""
    return tuple(check_set for check_set in sets if check_set.effect == "holds_merges")


def held_step(sets: Iterable[CheckSet], hold: Hold) -> VerificationStep | None:
    """The step ``hold``'s set failed at, among ``sets``; none if no set of its name has a step
    of that name, as when the set changed since."""
    steps = (step for check_set in sets if check_set.name == hold.name for step in check_set.steps)
    return next((step for step in steps if step.name == hold.step), None)


def keep_holds(holds: Sequence[AnyHold], holding: Collection[str]) -> tuple[AnyHold, ...]:
    """``holds`` of the sets still ``holding`` merges, by name, and of every finding, as a saved
    hold is restored: those of sets removed, or that only report now, are dropped."""
    return tuple(hold for hold in holds if isinstance(hold, FindingHold) or hold.name in holding)


def merge_holds(earlier: Sequence[AnyHold], later: Sequence[AnyHold]) -> tuple[AnyHold, ...]:
    """The holds of ``earlier`` and ``later``, a hold in ``later`` replacing the one of its key
    in ``earlier``."""
    keys = {hold.key for hold in later}
    return (*(hold for hold in earlier if hold.key not in keys), *later)


type Lifted = frozenset[str] | Literal["every"]
"""The holds lifted since the holds kept were last read or saved, by key, a set's its name, or
``"every"`` once the user released every hold by hand."""

NONE_LIFTED: Lifted = frozenset()
"""That no hold was lifted since the holds kept were last read or saved."""


def note_lift(lifted: Lifted, key: str) -> Lifted:
    """``lifted``, with the hold whose key is ``key``, such as a set's name, among those
    lifted."""
    return lifted | {key} if isinstance(lifted, frozenset) else lifted


def _is_lifted(hold: AnyHold, lifted: Lifted) -> bool:
    """Whether ``hold`` is among those ``lifted``."""
    return lifted == "every" or hold.key in lifted


def unlifted_sets(holding: Collection[str], lifted: Lifted) -> frozenset[str]:
    """The sets still ``holding`` merges, by name, whose holds weren't ``lifted`` since the
    holds kept were last read or saved, so a hold kept for one of them still counts."""
    return frozenset(holding) - lifted if isinstance(lifted, frozenset) else frozenset()


def fold_holds(
    loaded: Sequence[AnyHold],
    holding: Collection[str],
    lifted: Lifted,
    started: Sequence[AnyHold],
) -> tuple[AnyHold, ...]:
    """The holds to take on from ``loaded``, those kept, read afresh: those not ``lifted``
    since they were last read or saved, of the sets still ``holding`` merges, by name, and of
    the findings, as :func:`keep_holds` says, with the holds ``started`` since then replacing
    those of their keys, as :func:`merge_holds` says."""
    unlifted = tuple(hold for hold in loaded if not _is_lifted(hold, lifted))
    return merge_holds(keep_holds(unlifted, holding), started)


def view_hold(hold: AnyHold) -> MergeHold:
    """``hold`` as frontends see it: a set's, the set, the commit it failed on and why; a
    finding's, the lens that raised it as the set, the commit reviewed, and the title of the task
    it was filed as."""
    match hold:
        case Hold():
            return MergeHold(set=hold.name, commit=hold.commit, reason=f"{hold.step} failed")
        case FindingHold():
            return MergeHold(
                set=hold.lens, commit=hold.commit, reason=BLOCKING_REASON, finding=hold.title
            )
        case _:
            assert_never(hold)


def title_fix(name: str) -> str:
    """The title of a task fixing the set ``name``."""
    return f"{FIX_TITLE}{name}"


def set_fix_prompt(hold: Hold, subjects: Sequence[str] | None, report: str) -> str:
    """A task prompt asking to fix why ``hold``'s set failed, listing the ``subjects`` of the
    commits since it last passed, none if that isn't known, and the failure's ``report``. Its
    first line is :func:`title_fix`."""
    if subjects is None:
        since = "Orbit hasn't seen it pass since it started, so it can't list the commits since."
    elif subjects:
        since = "The commits since it last passed:\n" + "".join(f"\n- {s}" for s in subjects)
    else:
        since = "No commit landed since it last passed."
    return (
        f"{title_fix(hold.name)}\n\n"
        f"The set {hold.name} failed on {hold.commit[:SHORT_COMMIT]} at its step {hold.step}."
        f" {since}\n\n{report}\n\n{_FIX_INSTRUCTIONS}"
    )


def find_fixed_set(prompt: str) -> str | None:
    """The set a task with ``prompt`` fixes, as its title names it; none if it fixes none."""
    title = prompt.partition("\n")[0]
    return title.removeprefix(FIX_TITLE) if title.startswith(FIX_TITLE) else None


def find_fix_root(state: TaskState, find: Callable[[str], TaskState | None]) -> TaskState:
    """The task ``state``'s chain of fixes started from: the task it fixes, the one that fixes,
    and so on, as ``find`` finds them by id, up to the first that fixes none or isn't found."""
    found = None if state.fixes is None else find(state.fixes)
    return state if found is None else find_fix_root(found, find)


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
