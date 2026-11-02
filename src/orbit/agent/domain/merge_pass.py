"""What a merge pass decides of its review's findings, and what becomes of them after.

A *merge pass* is a task queued on ``reviews_finished``, booked into that review. It must decide
every finding its review's lenses reported: file it as a task with ``suggest_task``, the first a
suggestion names ``filed`` and the rest ``merged`` into that task, or dismiss it with a reason
with ``dismiss_finding``. Findings it leaves undecided as it ends are ``unanswered``. A filed
task landing marks its findings ``fixed``; one giving up marks them ``given_up``.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from orbit.agent.domain.merge_holds import BlockingTask
from orbit.agent.domain.reviews import (
    SEVERITIES,
    Finding,
    FindingOutcome,
    ReviewGroup,
    Severity,
    with_ended,
)
from orbit.agent.domain.suggestions import task_suggestion

NOT_A_MERGE_PASS = "Only a merge pass files findings; suggest the task without them."
"""The refusal of a suggestion naming findings from a review task that isn't a merge pass."""

NO_FINDING_NAMED = (
    "Name the findings the task covers: at least one of this review's undecided findings."
)
"""The refusal of a merge pass's suggestion naming no finding."""

NOT_BLOCKING = "File as blocking only when a blocking finding is among findings."
"""The refusal of a suggestion filed as blocking whose findings are all normal."""

NOT_MERGING = "Only a merge pass can dismiss findings."
"""The refusal of a dismissal from a task that isn't a merge pass."""

NO_REASON = "A dismissal needs a reason."
"""The refusal of a dismissal without a reason."""

TaskEnding = Literal["fixed", "given_up"]
"""What a filed task's end makes of its findings: ``fixed`` as it lands, ``given_up`` as it
gives up."""

COUNTED_OUTCOMES: tuple[FindingOutcome, ...] = ("filed", "merged", "dismissed", "unanswered")
"""The outcomes a merge pass's span counts as it ends."""


@dataclass(frozen=True)
class ReviewSuggestion:
    """A task a review task suggests with ``suggest_task``: its ``title``, on one line, and
    ``prompt``, the ``findings`` it files, by id, each once, in the order named (none from a
    review that isn't a merge pass), and its ``severity``."""

    title: str
    prompt: str
    findings: tuple[str, ...] = ()
    severity: Severity = "normal"


@dataclass(frozen=True)
class Dismissal:
    """A merge pass's dismissal of the finding ``finding``, by id, for ``reason``."""

    finding: str
    reason: str


def review_suggestion(arguments: Mapping[str, Any]) -> ReviewSuggestion | str:
    """The task a review task suggested with ``arguments``; or why it's refused."""
    suggestion = task_suggestion(arguments)
    if isinstance(suggestion, str):
        return suggestion
    findings = _finding_ids(arguments.get("findings"))
    if findings is None:
        return 'A task\'s findings are a list of finding ids, such as ["F1", "F3"].'
    title, prompt = suggestion
    severity = arguments.get("severity")
    if severity is None:
        return ReviewSuggestion(title, prompt, findings)
    if severity not in SEVERITIES:
        return f'A task\'s severity is "blocking" or "normal", not {severity!r}.'
    return ReviewSuggestion(title, prompt, findings, severity)


def _finding_ids(value: object) -> tuple[str, ...] | None:
    """The finding ids ``value`` lists, stripped, each once, in order: none for none; ``None``
    if it isn't a list of strings."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(dict.fromkeys(item.strip() for item in value))


def dismissal(arguments: Mapping[str, Any]) -> Dismissal | str:
    """The dismissal a merge pass made with ``arguments``; or why it's refused."""
    reason = str(arguments.get("reason", "")).strip()
    if not reason:
        return NO_REASON
    return Dismissal(str(arguments.get("finding", "")).strip(), reason)


def _finding(group: ReviewGroup, finding_id: str) -> Finding | None:
    """``group``'s finding ``finding_id``; none if it has no such finding."""
    return next((finding for finding in group.findings if finding.id == finding_id), None)


def decision_refusal(group: ReviewGroup, finding_id: str) -> str | None:
    """Why finding ``finding_id`` of ``group`` can't be decided: it isn't one, or it was decided
    already; ``None`` if it can."""
    finding = _finding(group, finding_id)
    if finding is None:
        return f"{finding_id} isn't an undecided finding of this review."
    if finding.outcome != "undecided":
        return f"{finding.id} is already {finding.outcome.replace('_', ' ')}."
    return None


def filing_refusal(
    group: ReviewGroup, finding_ids: Sequence[str], severity: Severity = "normal"
) -> str | None:
    """Why a merge pass of ``group`` can't file the findings ``finding_ids`` as one task at
    ``severity``: it names none, one of them can't be decided, or it's blocking with none of
    them blocking; ``None`` if it can."""
    if not finding_ids:
        return NO_FINDING_NAMED
    named = tuple(finding for finding in group.findings if finding.id in finding_ids)
    return _undecidable(group, finding_ids) or severity_refusal(named, severity)


def _undecidable(group: ReviewGroup, finding_ids: Sequence[str]) -> str | None:
    """Why the first of the findings ``finding_ids`` of ``group`` that can't be decided can't;
    ``None`` if each can."""
    refusals = (decision_refusal(group, finding_id) for finding_id in finding_ids)
    return next((refusal for refusal in refusals if refusal is not None), None)


def severity_refusal(findings: Sequence[Finding], severity: Severity) -> str | None:
    """Why a task can't be filed at ``severity`` from ``findings``: it's blocking, and none of
    them is; ``None`` if it can."""
    blocking = any(finding.severity == "blocking" for finding in findings)
    return NOT_BLOCKING if severity == "blocking" and not blocking else None


def blocking_task(group: ReviewGroup, suggestion: ReviewSuggestion) -> BlockingTask | None:
    """The blocking task ``suggestion``, which a merge pass of ``group`` may file, is: raised
    by the lens of the first finding it names, in the review of ``group``'s last commit; none
    unless it's blocking."""
    first = _finding(group, suggestion.findings[0]) if suggestion.findings else None
    if suggestion.severity != "blocking" or first is None:
        return None
    job = group.job
    return BlockingTask(first.role, job.commits[-1], suggestion.title, job.batch)


def with_filed(group: ReviewGroup, finding_ids: Sequence[str], task_id: str) -> ReviewGroup:
    """``group`` with the findings ``finding_ids`` filed as task ``task_id``: the first
    ``filed``, the rest ``merged`` into it."""
    findings = tuple(
        replace(finding, outcome=_filing(finding.id, finding_ids), task=task_id)
        if finding.id in finding_ids
        else finding
        for finding in group.findings
    )
    return replace(group, findings=findings)


def _filing(finding_id: str, finding_ids: Sequence[str]) -> FindingOutcome:
    """The outcome of finding ``finding_id``, filed with ``finding_ids`` as one task: ``filed``
    if it's the first, else ``merged``."""
    return "filed" if finding_id == finding_ids[0] else "merged"


def with_dismissed(group: ReviewGroup, dismissed: Dismissal) -> ReviewGroup:
    """``group`` with the finding ``dismissed`` names dismissed for its reason."""
    findings = tuple(
        replace(finding, outcome="dismissed", reason=dismissed.reason)
        if finding.id == dismissed.finding
        else finding
        for finding in group.findings
    )
    return replace(group, findings=findings)


def undecided(group: ReviewGroup) -> tuple[str, ...]:
    """The ids of ``group``'s findings that have no outcome yet, in order."""
    return tuple(finding.id for finding in group.findings if finding.outcome == "undecided")


def undecided_refusal(group: ReviewGroup) -> str | None:
    """Why a merge pass of ``group`` can't complete yet: some of its findings are undecided;
    ``None`` if every one is decided."""
    left = undecided(group)
    if not left:
        return None
    have = "has" if len(left) == 1 else "have"
    return (
        f"Decide every finding first: {', '.join(left)} {have} no outcome. File them with"
        " suggest_task or dismiss them with dismiss_finding."
    )


def ended_in(group: ReviewGroup, task_id: str) -> ReviewGroup:
    """``group`` with its task ``task_id`` ended, and, once its merge passes have all ended,
    the findings they left undecided ``unanswered``."""
    ended = with_ended(group, task_id)
    merged = task_id in ended.merges and all(task in ended.ended for task in ended.merges)
    return _with_unanswered(ended) if merged else ended


def _with_unanswered(group: ReviewGroup) -> ReviewGroup:
    """``group`` with its undecided findings ``unanswered``."""
    findings = tuple(
        replace(finding, outcome="unanswered") if finding.outcome == "undecided" else finding
        for finding in group.findings
    )
    return replace(group, findings=findings)


def filed_in(group: ReviewGroup, tasks: Collection[str]) -> tuple[Finding, ...]:
    """``group``'s findings filed, or merged, into one of ``tasks``, awaiting their end."""
    return tuple(
        finding
        for finding in group.findings
        if finding.task in tasks and finding.outcome in ("filed", "merged")
    )


def with_task_ended(group: ReviewGroup, tasks: Collection[str], ending: TaskEnding) -> ReviewGroup:
    """``group`` with its findings filed, or merged, into one of ``tasks`` marked as ``ending``
    says."""
    ending_ids = {finding.id for finding in filed_in(group, tasks)}
    findings = tuple(
        replace(finding, outcome=ending) if finding.id in ending_ids else finding
        for finding in group.findings
    )
    return replace(group, findings=findings)


def outcome_counts(group: ReviewGroup) -> dict[str, int]:
    """How many of ``group``'s findings stand at each of :data:`COUNTED_OUTCOMES`."""
    return {
        outcome: sum(finding.outcome == outcome for finding in group.findings)
        for outcome in COUNTED_OUTCOMES
    }
