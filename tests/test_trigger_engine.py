"""The trigger engine: triggers listening for a task's landing or end queue their roles' tasks,
rendered from their templates, with their names as origins, unless a guard holds them back, and
the review-rounds guard reviews the review the review lane finds due; landings count toward the
commits triggers, and those that fire, or one fired by hand, review the whole codebase; opening a
workspace fires the session_started triggers, those running the checks running them, and a step
of the startup check failing the startup_check_failed ones; one fired by hand on manual queues its
task, a configurator's first; a trigger naming a set runs it, on any event, counted or by hand;
a schedule come due fires its trigger alone; and a task queued while verification is missing
waits, with a notice naming its trigger, but the configurator's."""

import pytest

from orbit.agent.application.lanes import SessionGate
from orbit.agent.application.ports import TreeFailed
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.triggers import ReviewRound, TriggerEngine, TriggerHooks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.checks import Setup, configure_prompt, configured_checks
from orbit.agent.domain.reviews import (
    DIFF_HELD,
    ReviewJob,
    ReviewTemplate,
    commit_review,
    review_prompt,
    review_values,
    unshown_diff,
)
from orbit.agent.domain.roles import Role
from orbit.agent.domain.skills import SkillMapping
from orbit.agent.domain.triggers import Trigger, VerificationState, render_template
from orbit.agent.domain.verification import CheckSet, VerificationStep, startup_fix_prompt
from orbit.protocol import (
    BUGS_REVIEWER,
    CODER,
    CONFIGURATOR,
    CONFIGURE_CHECKS,
    DRIFT_REVIEWER,
    FIT_REVIEWER,
    LENS_REVIEWERS,
    REVIEWER,
    SCALING_REVIEWER,
    SECURITY_REVIEWER,
    STYLE_REVIEWER,
    SUPER_LENS_REVIEWERS,
    SUPER_REVIEW,
    TASK_CHECKS,
    TESTS_REVIEWER,
    ErrorOccurred,
    Event,
    Notice,
    TasksChanged,
    TaskSpec,
    TaskState,
    TaskStatus,
)
from tests.agent_fakes import ROLES, SHIPPED_TRIGGERS, TRIGGERS, FakeTree, desk_for
from tests.telemetry_helpers import Traced

COMMIT = "c1abcdef99"


class Engine:
    """A trigger engine firing ``triggers`` over a board of its own, in ``tree``, with
    ``roles`` (:data:`ROLES` unless given) and ``verification`` as its gate says it is (off
    unless given), recording what it publishes and the roles whose lanes it kicks."""

    def __init__(
        self,
        triggers: dict[str, Trigger],
        tree: FakeTree | None = None,
        roles: dict[str, Role] | None = None,
        verification: VerificationState = "off",
        skill_mappings: tuple[SkillMapping, ...] = (),
    ) -> None:
        self.events: list[Event] = []
        self.kicked: list[str] = []
        self.checks_run: list[str] = []  # The checks run, by what each trigger's checks named.
        self.tree = tree or FakeTree()
        desk = desk_for(
            self.publish,
            tree=self.tree,
            triggers=triggers,
            roles=roles or ROLES,
            skill_mappings=skill_mappings,
        )
        self.tasks = Tasks(TaskBoard(), self.publish)
        self.board = self.tasks.board
        gate = SessionGate(verification=lambda: verification)
        hooks = TriggerHooks(self.kicked.append, self.run_checks, gate)
        self.engine = TriggerEngine(desk, self.tasks, hooks)

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    def run_checks(self, checks: str, trigger: str) -> None:
        self.checks_run.append(checks)

    def shown(self) -> list[tuple[str, str, str, str]]:
        """Each task on the board: its role, origin, status and prompt."""
        return [(t.task.role, t.origin, t.status, t.task.prompt) for t in self.board.tasks]

    def errors(self) -> list[ErrorOccurred]:
        return [event for event in self.events if isinstance(event, ErrorOccurred)]

    def notices(self) -> list[str]:
        return [event.message for event in self.events if isinstance(event, Notice)]


def look(**fields: object) -> Trigger:
    """A trigger queuing a reviewer's look at each landed coder commit, with ``fields`` over
    that."""
    return Trigger.model_validate(
        {
            "on": "task_landed",
            "role": REVIEWER,
            "prompt": "Look at {commit}",
            "when": {"role": CODER},
            **fields,
        }
    )


def ended(
    task_id: str = "1",
    role: str = CODER,
    origin: str = "manual",
    status: TaskStatus = "completed",
    skills: tuple[str, ...] = (),
) -> TaskState:
    """A task as it ended: ``role``'s, queued by ``origin``, as ``status``, naming ``skills``."""
    task = TaskSpec(prompt=f"Task {task_id}", role=role, skills=skills)
    return TaskState(id=task_id, task=task, status=status, origin=origin)


def unbooked(task_id: str, template: ReviewTemplate) -> None:
    """Books no review task."""


def disabled(*names: str) -> dict[str, Role]:
    """The built-in roles, ``names`` disabled."""
    return {**ROLES, **{name: ROLES[name].model_copy(update={"enabled": False}) for name in names}}


# Firing on a landing


async def test_a_landing_queues_the_task_of_a_trigger_listening_from_its_template() -> None:
    engine = Engine({"look": look()})
    queued = await engine.engine.landed(ended(), COMMIT)
    assert engine.shown() == [(REVIEWER, "look", "pending", "Look at c1abcde")]
    assert queued == (engine.board.tasks[0].id,)
    assert isinstance(engine.events[-1], TasksChanged)
    assert engine.kicked == []  # Running is left to what raised the event.


async def test_a_triggers_task_has_its_roles_settings_and_no_skills_of_its_own() -> None:
    engine = Engine({"look": look()})
    await engine.engine.landed(ended(skills=("python",)), COMMIT)
    [state] = engine.board.tasks
    assert state.task.settings == ROLES[REVIEWER].settings
    assert state.task.skills == ()


async def test_a_landing_no_trigger_takes_queues_nothing_and_shows_no_diff() -> None:
    engine = Engine({"look": look()})
    engine.tree.failures["show"] = TreeFailed("would be shown")
    assert await engine.engine.landed(ended(role=REVIEWER), COMMIT) == ()
    assert engine.board.tasks == ()
    assert engine.events == []


async def test_a_trigger_whose_role_is_disabled_never_fires() -> None:
    engine = Engine({"look": look()}, roles=disabled(REVIEWER))
    assert await engine.engine.landed(ended(), COMMIT) == ()
    assert engine.board.tasks == ()


async def test_two_triggers_on_a_landing_each_fire_in_their_order() -> None:
    first = look(prompt="First at {commit}")
    engine = Engine({"second": look(prompt="Second {trigger}"), "first": first})
    await engine.engine.landed(ended(), COMMIT)
    assert [(origin, prompt) for _, origin, _, prompt in engine.shown()] == [
        ("second", "Second second"),
        ("first", "First at c1abcde"),
    ]


async def test_a_landing_gives_its_commit_task_skills_and_its_reviews_values() -> None:
    template = (
        "{title}\n\n{task}: {task_skills}\n\n{cap}\n\n{queued}\n\n{missing}{landed_since}\n\n{diff}"
    )
    engine = Engine({"look": look(prompt=template)})
    engine.board.add(TaskSpec(prompt="Add docs\n\nMore."))
    await engine.engine.landed(ended("3", skills=("python", "layers")), COMMIT)
    assert engine.board.tasks[-1].task.prompt == (
        "Review commit c1abcde\n\n3: python, layers\n\n5\n\n"
        "Tasks already queued (don't suggest these):\n- Add docs\n\n"
        f"diff of {COMMIT}"
    )


async def test_a_diff_git_cannot_show_at_landing_is_quoted_as_unshown_and_the_task_queued() -> None:
    engine = Engine({"look": look(prompt="Look at {commit}\n\n{diff}")})
    engine.tree.failures["show"] = TreeFailed("git show failed: bad object")
    [task_id] = await engine.engine.landed(ended(), COMMIT)
    unshown = unshown_diff("git show failed: bad object")
    assert engine.board.get(task_id).task.prompt == f"Look at c1abcde\n\n{unshown}"
    assert engine.errors() == []


def fixes(role: str) -> Trigger:
    """A trigger ``review-rounds`` guards queuing ``role``'s fix of each landed coder commit,
    quoting its diff."""
    return look(role=role, guards=["review-rounds"], prompt="Fix {commit}\n\n{diff}")


async def test_another_roles_task_booked_into_a_review_keeps_the_diff_in_its_prompt() -> None:
    booked: dict[str, ReviewTemplate] = {}
    engine = Engine({"fix": fixes(CODER)})
    review = ReviewRound(commit_review("1", COMMIT), booked.__setitem__)
    [task_id] = await engine.engine.landed(ended(), COMMIT, review)
    state = engine.board.get(task_id)
    assert (state.task.role, state.queued_as) == (CODER, "review")
    assert state.task.prompt == f"Fix c1abcde\n\ndiff of {COMMIT}"
    assert DIFF_HELD not in state.task.prompt
    assert "diff" not in booked[task_id].values


async def test_a_review_roles_task_booked_into_a_review_holds_its_diff_unread() -> None:
    booked: dict[str, ReviewTemplate] = {}
    engine = Engine({"fix": fixes(REVIEWER)})
    review = ReviewRound(commit_review("1", COMMIT), booked.__setitem__)
    [task_id] = await engine.engine.landed(ended(), COMMIT, review)
    assert engine.board.get(task_id).task.prompt == f"Fix c1abcde\n\n{DIFF_HELD}"
    assert "diff" not in booked[task_id].values
    assert engine.tree.asked_shown == []  # The review lane reads it as the review starts.


async def test_a_landing_whose_templates_quote_no_diff_asks_git_for_none() -> None:
    engine = Engine({"look": look()})
    await engine.engine.landed(ended(), COMMIT)
    assert engine.tree.asked_shown == []


async def test_a_landing_asks_git_for_the_diff_once_however_many_tasks_quote_it() -> None:
    engine = Engine({"look": look(prompt="{diff}"), "fix": fixes(CODER)})
    review = ReviewRound(commit_review("1", COMMIT), unbooked)
    await engine.engine.landed(ended(), COMMIT, review)
    assert len(engine.board.tasks) == 2
    assert engine.tree.asked_shown == [(COMMIT,)]


async def test_a_draft_trigger_adds_its_task_as_a_draft_from_it() -> None:
    engine = Engine({"look": look(draft=True)})
    await engine.engine.landed(ended(), COMMIT)
    [state] = engine.board.tasks
    assert (state.status, state.origin, state.proposed_by) == ("draft", "look", None)
    assert state.queued_as == "task"  # Booked as no review, a reviewer's though it is.


async def test_a_trigger_reviewing_nothing_queues_its_reviewers_task_as_a_task() -> None:
    engine = Engine({"look": look()})
    await engine.engine.landed(ended(), COMMIT)
    [state] = engine.board.tasks
    assert (state.task.role, state.origin, state.queued_as) == (REVIEWER, "look", "task")


# A list of roles


def looks(**fields: object) -> Trigger:
    """A trigger queuing a look by each of the super review's lenses at each landed coder
    commit, with ``fields`` over that."""
    return look(role=list(SUPER_LENS_REVIEWERS), **fields)


async def test_a_trigger_listing_roles_queues_one_task_per_role_from_one_rendering() -> None:
    engine = Engine({"look": looks()})
    queued = await engine.engine.landed(ended(), COMMIT)
    assert engine.shown() == [
        (role, "look", "pending", "Look at c1abcde") for role in SUPER_LENS_REVIEWERS
    ]
    assert queued == tuple(state.id for state in engine.board.tasks)


async def test_a_trigger_listing_roles_queues_none_for_a_disabled_one() -> None:
    engine = Engine({"look": looks()}, roles=disabled(DRIFT_REVIEWER))
    await engine.engine.landed(ended(), COMMIT)
    assert [role for role, _, _, _ in engine.shown()] == [STYLE_REVIEWER, SCALING_REVIEWER]


async def test_a_trigger_listing_only_disabled_roles_queues_nothing() -> None:
    engine = Engine({"look": looks()}, roles=disabled(*SUPER_LENS_REVIEWERS))
    assert await engine.engine.landed(ended(), COMMIT) == ()


async def test_not_queued_holds_a_list_back_while_a_task_of_any_of_its_roles_waits() -> None:
    engine = Engine({"look": looks(guards=["not-queued"])})
    waiting = engine.board.add(TaskSpec(prompt="Look again.", role=SCALING_REVIEWER))
    assert await engine.engine.landed(ended(), COMMIT) == ()
    engine.board.remove(waiting)
    assert len(await engine.engine.landed(ended(), COMMIT)) == 3


async def test_dedupe_holds_a_list_back_by_the_tasks_waiting_before_it_fired() -> None:
    engine = Engine({"look": looks(prompt="Look at the code", guards=["dedupe"])})
    assert len(await engine.engine.landed(ended("1"), COMMIT)) == 3
    assert await engine.engine.landed(ended("2"), "c2") == ()


async def test_a_list_reviewing_a_batch_queues_only_the_roles_that_raised_its_findings() -> None:
    booked: dict[str, ReviewTemplate] = {}
    guards = ["review-rounds", "raised-finding"]
    engine = Engine({"look": looks(when={}, guards=guards)})
    raised = frozenset({DRIFT_REVIEWER, CODER})
    queued = await engine.engine.reviewed(ReviewRound(BATCH, booked.__setitem__, raised))
    assert [role for role, _, _, _ in engine.shown()] == [DRIFT_REVIEWER]
    assert list(booked) == list(queued)


async def test_a_list_guarded_by_raised_finding_queues_every_role_on_a_landing() -> None:
    guards = ["review-rounds", "raised-finding"]
    engine = Engine({"look": looks(guards=guards)})
    review = ReviewRound(commit_review("1", COMMIT), unbooked)
    assert len(await engine.engine.landed(ended(), COMMIT, review)) == 3


# Guards


async def test_dedupe_skips_a_task_whose_first_line_a_waiting_task_has() -> None:
    engine = Engine({"look": look(prompt="Look at the code", guards=["dedupe"])})
    await engine.engine.landed(ended("1"), COMMIT)
    assert await engine.engine.landed(ended("2"), "c2") == ()
    assert len(engine.board.tasks) == 1
    engine.board.finish(engine.board.tasks[0].id, "completed")
    await engine.engine.landed(ended("3"), "c3")
    assert [status for _, _, status, _ in engine.shown()] == ["completed", "pending"]


async def test_not_queued_skips_while_a_task_of_its_role_waits() -> None:
    engine = Engine({"look": look(guards=["not-queued"])})
    engine.board.add(TaskSpec(prompt="Make it faster."))
    waiting = engine.board.add(TaskSpec(prompt="Review it.", role=REVIEWER))
    assert await engine.engine.landed(ended(), COMMIT) == ()
    engine.board.remove(waiting)
    assert len(await engine.engine.landed(ended(), COMMIT)) == 1


async def test_review_rounds_holds_its_trigger_back_without_a_review_due() -> None:
    engine = Engine({"review": TRIGGERS["review"], "look": look()})
    await engine.engine.landed(ended(), COMMIT)
    assert [origin for _, origin, _, _ in engine.shown()] == ["look"]


async def test_review_rounds_reviews_the_review_due_and_books_its_task() -> None:
    booked: dict[str, ReviewTemplate] = {}
    engine = Engine({"review": TRIGGERS["review"]})
    engine.board.add(TaskSpec(prompt="Make it faster.", skills=("python",)))
    job = commit_review("1", COMMIT)
    [task_id] = await engine.engine.landed(ended(), COMMIT, ReviewRound(job, booked.__setitem__))
    review = engine.board.get(task_id)
    assert (review.task.role, review.origin, review.queued_as) == (REVIEWER, "review", "review")
    assert review.task.prompt == review_prompt(job, DIFF_HELD, 5, ["Make it faster."])
    assert review.task.skills == ("python",)  # Those of the task it reviews.
    assert list(booked) == [task_id]
    assert "diff" not in booked[task_id].values


async def test_a_review_due_is_booked_with_the_template_and_values_its_prompt_renders_from() -> (
    None
):
    booked: dict[str, ReviewTemplate] = {}
    trigger = look(prompt="{trigger} at {commit}\n\n{landed_since}", guards=["review-rounds"])
    engine = Engine({"look": trigger})
    review = ReviewRound(commit_review("1", COMMIT), booked.__setitem__)
    [task_id] = await engine.engine.landed(ended(), COMMIT, review)
    kept = booked[task_id]
    assert kept.template == "{trigger} at {commit}\n\n{landed_since}"
    assert (kept.values["trigger"], kept.values["commit"]) == ("look", "c1abcde")
    assert render_template(kept.template, kept.values) == engine.board.get(task_id).task.prompt


async def test_a_review_due_whose_diff_git_cannot_show_at_landing_is_queued_all_the_same() -> None:
    engine = Engine({"review": TRIGGERS["review"]})
    engine.tree.failures["show"] = TreeFailed("git show failed: bad object")
    review = ReviewRound(commit_review("1", COMMIT), unbooked)
    [task_id] = await engine.engine.landed(ended(), COMMIT, review)
    assert engine.board.get(task_id).task.prompt.endswith(DIFF_HELD)
    assert engine.errors() == []


# Firing on a task's end


def tidy() -> Trigger:
    """A trigger queuing a coder's tidying up after each task that finishes."""
    return Trigger(on="task_finished", role=CODER, prompt="Tidy up after {trigger}")


async def test_a_task_finishing_queues_the_task_of_a_trigger_listening() -> None:
    engine = Engine({"tidy": tidy()})
    await engine.engine.finished(ended(status="gave_up"))
    assert engine.shown() == [(CODER, "tidy", "pending", "Tidy up after tidy")]


async def test_a_trigger_never_fires_on_the_end_of_a_task_it_queued() -> None:
    engine = Engine({"tidy": tidy()})
    assert await engine.engine.finished(ended(origin="tidy")) == ()
    assert engine.board.tasks == ()


async def test_a_trigger_takes_a_finished_task_by_its_status() -> None:
    engine = Engine({"tidy": tidy().model_copy(update={"when": {"status": ("gave_up",)}})})
    assert await engine.engine.finished(ended(status="completed")) == ()
    assert len(await engine.engine.finished(ended(status="gave_up"))) == 1


# Paths a landing changed


SECURITY_PATHS = ("src/**/external/**", "pyproject.toml")
"""The paths a project narrows the security review to."""


def security(**fields: object) -> Trigger:
    """A security-reviewer's look at each landed coder commit changing one of
    :data:`SECURITY_PATHS`, with ``fields`` over that."""
    return look(role=SECURITY_REVIEWER, paths=SECURITY_PATHS, **fields)


@pytest.mark.parametrize(
    ("changed", "fired"),
    [
        (["src/orbit/agent/external/git.py"], True),
        (["README.md", "pyproject.toml"], True),
        (["README.md"], False),
    ],
)
async def test_a_trigger_with_paths_fires_only_for_a_commit_changing_one(
    changed: list[str], fired: bool
) -> None:
    engine = Engine({"security-review": security()})
    engine.tree.changed[f"{COMMIT}^"] = changed
    queued = await engine.engine.landed(ended(), COMMIT)
    assert bool(queued) is fired
    assert engine.tree.asked_changed == [(f"{COMMIT}^", COMMIT)]


async def test_a_trigger_with_paths_fires_for_a_commit_moving_a_file_out_of_them() -> None:
    engine = Engine({"security-review": security()})
    engine.tree.move(f"{COMMIT}^", "src/x/external/auth.py", "src/x/auth.py")
    assert len(await engine.engine.landed(ended(), COMMIT)) == 1


async def test_a_trigger_with_paths_leaves_the_others_on_the_landing_firing() -> None:
    engine = Engine({"look": look(), "security-review": security()})
    engine.tree.changed[f"{COMMIT}^"] = ["README.md"]
    await engine.engine.landed(ended(), COMMIT)
    assert [origin for _, origin, _, _ in engine.shown()] == ["look"]


async def test_a_batchs_review_fires_a_trigger_with_paths_one_of_its_commits_matches() -> None:
    engine = Engine({"security-review": security(guards=("review-rounds",), when={})})
    engine.tree.changed |= {"c2^": ["README.md"], "c3^": ["pyproject.toml"]}
    queued = await engine.engine.reviewed(ReviewRound(BATCH, unbooked))
    assert [engine.board.get(task_id).origin for task_id in queued] == ["security-review"]
    assert engine.tree.asked_changed == [("c2^", "c2"), ("c3^", "c3")]


async def test_a_batchs_review_holds_back_a_trigger_with_paths_none_of_its_commits_match() -> None:
    engine = Engine({"security-review": security(guards=("review-rounds",), when={})})
    engine.tree.changed |= {"c2^": ["README.md"], "c3^": ["docs/plan.md"]}
    assert await engine.engine.reviewed(ReviewRound(BATCH, unbooked)) == ()


async def test_no_changed_paths_are_asked_for_while_no_trigger_listening_has_paths() -> None:
    engine = Engine({"look": look(), "security-review": security(when={"role": REVIEWER})})
    await engine.engine.landed(ended(), COMMIT)
    await engine.engine.reviewed(ReviewRound(BATCH, unbooked))
    assert engine.tree.asked_changed == []


async def test_changed_paths_git_cannot_list_fire_the_trigger_and_go_on_its_span() -> None:
    traced = Traced()
    engine = Engine({"look": look(), "security-review": security()})
    engine.tree.failures["changed_files"] = TreeFailed("git diff failed: bad revision")
    with traced.telemetry.span("landing"):
        queued = await engine.engine.landed(ended(), COMMIT)
    assert [engine.board.get(task_id).origin for task_id in queued] == ["look", "security-review"]
    [span] = traced.spans("landing")
    assert [(event.name, dict(event.attributes or {})) for event in span.events] == [
        (
            "orbit.trigger.paths_failed",
            {"orbit.trigger": "security-review", "error": "git diff failed: bad revision"},
        )
    ]


# Landings that change no code, which code-changed holds its triggers back on


def lens(role: str = BUGS_REVIEWER, **fields: object) -> Trigger:
    """``role``'s look at each landed coder commit, ``code-changed`` guarding it, with
    ``fields`` over that."""
    return look(**({"role": role, "guards": ("code-changed",)} | fields))


NO_CODE = 'Skipped the bugs reviews of "Task 1": it changed no code.'
"""The note of the bugs review held back on the landing of :func:`ended`'s task."""


async def test_a_landing_changing_no_code_holds_back_a_guarded_trigger_with_a_note() -> None:
    engine = Engine({"bugs-review": lens()})
    engine.tree.changed[f"{COMMIT}^"] = ["docs/x.md"]
    assert await engine.engine.landed(ended(), COMMIT) == ()
    assert engine.board.tasks == ()
    assert engine.notices() == [NO_CODE]


async def test_the_note_names_a_role_without_the_reviewer_suffix_whole() -> None:
    engine = Engine({"look": lens(REVIEWER)})
    engine.tree.changed[f"{COMMIT}^"] = ["docs/x.md"]
    await engine.engine.landed(ended(), COMMIT)
    assert engine.notices() == ['Skipped the reviewer reviews of "Task 1": it changed no code.']


async def test_two_guarded_triggers_held_back_share_one_note() -> None:
    engine = Engine({"bugs-review": lens(), "tests-review": lens(TESTS_REVIEWER), "look": look()})
    engine.tree.changed[f"{COMMIT}^"] = ["docs/x.md"]
    await engine.engine.landed(ended(), COMMIT)
    assert [origin for _, origin, _, _ in engine.shown()] == ["look"]
    assert engine.notices() == [
        'Skipped the bugs and tests reviews of "Task 1": it changed no code.'
    ]


@pytest.mark.parametrize("changed", [["src/x.py"], ["uv.lock"], ["src/x.md"]])
async def test_a_landing_changing_code_fires_a_guarded_trigger_without_a_note(
    changed: list[str],
) -> None:
    engine = Engine(
        {"bugs-review": lens()}, skill_mappings=(SkillMapping(paths=("src",), use=("python",)),)
    )
    engine.tree.changed[f"{COMMIT}^"] = changed
    assert len(await engine.engine.landed(ended(), COMMIT)) == 1
    assert engine.notices() == []


async def test_a_guarded_trigger_with_paths_is_decided_by_them_alone() -> None:
    engine = Engine({"docs-review": lens(paths=("docs/**",))})
    engine.tree.changed[f"{COMMIT}^"] = ["docs/x.md"]
    assert len(await engine.engine.landed(ended(), COMMIT)) == 1
    assert engine.notices() == []


async def test_changed_paths_git_cannot_list_fire_a_guarded_trigger_and_go_on_its_span() -> None:
    traced = Traced()
    engine = Engine({"bugs-review": lens()})
    engine.tree.failures["changed_files"] = TreeFailed("git diff failed: bad revision")
    with traced.telemetry.span("landing"):
        assert len(await engine.engine.landed(ended(), COMMIT)) == 1
    [span] = traced.spans("landing")
    assert [dict(event.attributes or {}) for event in span.events] == [
        {"orbit.trigger": "bugs-review", "error": "git diff failed: bad revision"}
    ]
    assert engine.notices() == []


def three_tasks(engine: Engine) -> ReviewJob:
    """A batch of three tasks added to ``engine``'s board, whose commits change only docs."""
    reviewed = tuple(
        engine.board.add(TaskSpec(prompt=prompt))
        for prompt in ("Fix the parser\n\nIt breaks.", "Two", "Three")
    )
    commits = ("c2", "c3", "c4")
    engine.tree.changed |= {f"{commit}^": ["docs/plan.md"] for commit in commits}
    return ReviewJob(commits=commits, round=1, batch="batch-1", reviewed=reviewed)


async def test_a_batchs_review_changing_no_code_notes_its_first_task_and_how_many_more() -> None:
    engine = Engine({"bugs-review": lens(guards=("review-rounds", "code-changed"), when={})})
    job = three_tasks(engine)
    assert await engine.engine.reviewed(ReviewRound(job, unbooked)) == ()
    assert engine.notices() == [
        'Skipped the bugs reviews of "Fix the parser" and 2 more: it changed no code.'
    ]


async def test_a_batchs_review_raised_quietly_notes_nothing_it_holds_back() -> None:
    engine = Engine({"bugs-review": lens(guards=("review-rounds", "code-changed"), when={})})
    await engine.engine.reviewed_quietly(ReviewRound(three_tasks(engine), unbooked))
    assert len(engine.board.tasks) == 3
    assert engine.events == []


async def shipped_landing(changed: list[str], triggers: dict[str, Trigger]) -> Engine:
    """An engine firing ``triggers`` after its task's commit, changing ``changed``, landed and
    came due for review."""
    engine = Engine(triggers)
    engine.tree.changed[f"{COMMIT}^"] = changed
    await engine.engine.landed(ended(), COMMIT, ReviewRound(commit_review("1", COMMIT), unbooked))
    return engine


async def test_the_shipped_triggers_review_a_docs_only_landing_with_the_fit_lens_alone() -> None:
    engine = await shipped_landing(["docs/x.md"], SHIPPED_TRIGGERS)
    assert [role for role, _, _, _ in engine.shown()] == [FIT_REVIEWER]
    assert engine.notices() == [
        'Skipped the bugs, tests, logic, security and performance reviews of "Task 1":'
        " it changed no code."
    ]


async def test_the_shipped_triggers_review_a_landing_changing_code_with_every_lens() -> None:
    engine = await shipped_landing(["src/x.py"], SHIPPED_TRIGGERS)
    assert [role for role, _, _, _ in engine.shown()] == list(LENS_REVIEWERS)
    assert engine.notices() == []


async def test_a_lens_whose_guards_drop_code_changed_reviews_a_docs_only_landing_too() -> None:
    bugs = SHIPPED_TRIGGERS["bugs-review"].model_copy(update={"guards": ("review-rounds",)})
    engine = await shipped_landing(["docs/x.md"], {**SHIPPED_TRIGGERS, "bugs-review": bugs})
    assert [role for role, _, _, _ in engine.shown()] == [FIT_REVIEWER, BUGS_REVIEWER]
    assert engine.notices() == [
        'Skipped the tests, logic, security and performance reviews of "Task 1":'
        " it changed no code."
    ]


# A batch's review


BATCH = ReviewJob(commits=("c2", "c3"), round=1, batch="batch-1", reviewed=("2", "3"))


async def test_a_batchs_review_fires_only_the_triggers_review_rounds_guards() -> None:
    booked: dict[str, ReviewTemplate] = {}
    triggers = {"review": TRIGGERS["review"], "look": look(when={})}
    engine = Engine(triggers)
    engine.board.add(TaskSpec(prompt="Add tests", skills=("python",)))
    [task_id] = await engine.engine.reviewed(ReviewRound(BATCH, booked.__setitem__))
    review = engine.board.get(task_id)
    assert review.origin == "review"
    assert review.task.prompt == review_prompt(BATCH, DIFF_HELD, 5, ["Add tests"])
    assert list(booked) == [task_id]
    assert isinstance(engine.events[-1], TasksChanged)


async def test_a_batchs_review_with_the_review_trigger_disabled_queues_nothing() -> None:
    off = TRIGGERS["review"].model_copy(update={"enabled": False})
    engine = Engine({"review": off})
    assert await engine.engine.reviewed(ReviewRound(BATCH, unbooked)) == ()
    assert engine.board.tasks == ()


async def test_a_batchs_review_queued_quietly_is_not_published() -> None:
    engine = Engine({"review": TRIGGERS["review"]})
    await engine.engine.reviewed_quietly(ReviewRound(BATCH, unbooked))
    assert [role for role, _, _, _ in engine.shown()] == [REVIEWER]
    assert engine.events == []


async def test_a_batchs_review_queued_quietly_with_none_listening_adds_nothing() -> None:
    engine = Engine({})
    await engine.engine.reviewed_quietly(ReviewRound(BATCH, unbooked))
    assert engine.board.tasks == ()


# Landings counted toward the commits triggers, and firing by hand


SUPER = TRIGGERS[SUPER_REVIEW]
WHOLE = ReviewJob(commits=(COMMIT,), round=0, batch=None, whole=True)


def super_every(every: int) -> dict[str, Trigger]:
    """The shipped super-review trigger, due every ``every`` landings."""
    return {SUPER_REVIEW: SUPER.model_copy(update={"every": every})}


async def test_a_landing_counts_toward_the_commits_triggers_taking_it() -> None:
    engine = Engine(super_every(2))
    first = engine.engine.count(ended("1", origin="planner"), {})
    assert (first.counts, first.fires) == ({SUPER_REVIEW: 1}, [])
    second = engine.engine.count(ended("2"), first.counts)
    assert (second.counts, second.fires) == (
        {SUPER_REVIEW: 0},
        [(SUPER_REVIEW, SUPER.model_copy(update={"every": 2}))],
    )
    assert engine.board.tasks == ()  # Queuing is left to fire_counted.


async def test_a_due_super_review_waits_while_a_review_on_the_board_waits() -> None:
    engine = Engine(super_every(1))
    review = TaskSpec(prompt="Review commit c0", role=REVIEWER)
    engine.board.add_triggered(review, "review", "review")
    counted = engine.engine.count(ended(), {})
    assert (counted.counts, counted.fires) == ({SUPER_REVIEW: 1}, [])


@pytest.mark.parametrize("role", [STYLE_REVIEWER, CODER])
async def test_a_due_super_review_waits_while_a_project_triggers_review_waits(role: str) -> None:
    engine = Engine(super_every(1))
    audit = TaskSpec(prompt="Audit the codebase at c0", role=role)
    engine.board.add_triggered(audit, "audit", "review")
    counted = engine.engine.count(ended(), {})
    assert (counted.counts, counted.fires) == ({SUPER_REVIEW: 1}, [])


async def test_a_due_super_review_does_not_wait_for_a_reviewers_task_booked_as_no_review() -> None:
    engine = Engine(super_every(1))
    engine.board.add_triggered(TaskSpec(prompt="Look around", role=REVIEWER), "look")
    counted = engine.engine.count(ended(), {})
    assert counted.counts == {SUPER_REVIEW: 0}
    assert [name for name, _ in counted.fires] == [SUPER_REVIEW]


async def test_a_due_super_review_waits_while_a_project_triggers_suggestion_runs() -> None:
    engine = Engine(super_every(1))
    suggestion = engine.board.add_suggestion(TaskSpec(prompt="Tidy"), "batch-1", 1, "audit")
    engine.board.set_status(suggestion, "running")
    counted = engine.engine.count(ended(), {})
    assert (counted.counts, counted.fires) == ({SUPER_REVIEW: 1}, [])


async def test_a_landing_counts_toward_no_trigger_whose_roles_are_all_disabled() -> None:
    engine = Engine(super_every(1), roles=disabled(*SUPER_LENS_REVIEWERS))
    assert engine.engine.count(ended(), {}).counts == {}


async def test_a_landing_counts_toward_a_trigger_with_one_of_its_roles_enabled() -> None:
    engine = Engine(super_every(1), roles=disabled(STYLE_REVIEWER, DRIFT_REVIEWER))
    assert [name for name, _ in engine.engine.count(ended(), {}).fires] == [SUPER_REVIEW]


async def test_a_trigger_fired_by_hand_is_found_by_its_name() -> None:
    engine = Engine({SUPER_REVIEW: SUPER})
    assert engine.engine.by_hand(SUPER_REVIEW) == [(SUPER_REVIEW, SUPER)]
    assert engine.engine.by_hand("nightly") == []


async def test_a_disabled_trigger_or_one_whose_role_is_disabled_does_not_fire_by_hand() -> None:
    off = Engine({SUPER_REVIEW: SUPER.model_copy(update={"enabled": False})})
    assert off.engine.by_hand(SUPER_REVIEW) == []
    unrolled = Engine({SUPER_REVIEW: SUPER}, roles=disabled(*SUPER_LENS_REVIEWERS))
    assert unrolled.engine.by_hand(SUPER_REVIEW) == []
    partly = Engine({SUPER_REVIEW: SUPER}, roles=disabled(DRIFT_REVIEWER))
    assert partly.engine.by_hand(SUPER_REVIEW) == [(SUPER_REVIEW, SUPER)]


async def test_counted_triggers_queue_their_tasks_reviewing_the_codebase_and_booked() -> None:
    booked: dict[str, ReviewTemplate] = {}
    engine = Engine({SUPER_REVIEW: SUPER})
    engine.board.add(TaskSpec(prompt="Make it faster.", skills=("python",)))
    values = review_values(WHOLE, 10, ["Make it faster."])
    fired = [(SUPER_REVIEW, SUPER)]
    queued = await engine.engine.fire_counted(fired, values, ReviewRound(WHOLE, booked.__setitem__))
    reviews = [engine.board.get(task_id) for task_id in queued]
    assert [(t.task.role, t.origin, t.queued_as) for t in reviews] == [
        (role, SUPER_REVIEW, "review") for role in SUPER_LENS_REVIEWERS
    ]
    uncapped = review_prompt(WHOLE, "", 10, ["Make it faster."]).replace(
        "\n\nYou may make at most 10 suggestions.", "", 1
    )
    assert {review.task.prompt for review in reviews} == {uncapped}
    assert {review.task.skills for review in reviews} == {()}  # They review no particular task.
    assert list(booked) == list(queued)
    first, *others = booked.values()
    assert others == [first, first]  # One rendering, booked with each.
    assert isinstance(engine.events[-1], TasksChanged)
    assert engine.kicked == []


@pytest.mark.parametrize("draft", [False, True])
async def test_a_counted_trigger_of_another_role_queues_its_task_as_a_review(draft: bool) -> None:
    booked: dict[str, ReviewTemplate] = {}
    audit = Trigger(on="commits", role=CODER, prompt="Audit at {commit}.", draft=draft)
    engine = Engine({"audit": audit})
    values = review_values(WHOLE, 10, [])
    review = ReviewRound(WHOLE, booked.__setitem__)
    [task_id] = await engine.engine.fire_counted([("audit", audit)], values, review)
    state = engine.board.get(task_id)
    assert (state.task.role, state.origin, state.queued_as) == (CODER, "audit", "review")
    assert state.status == ("draft" if draft else "pending")
    assert list(booked) == [task_id]


async def test_counted_triggers_are_not_held_back_again_as_they_are_queued() -> None:
    engine = Engine({SUPER_REVIEW: SUPER})
    review_task = TaskSpec(prompt="Review commit c0", role=REVIEWER)
    engine.board.add_triggered(review_task, "review", "review")
    values = review_values(WHOLE, 10, [])
    review = ReviewRound(WHOLE, unbooked)
    queued = await engine.engine.fire_counted([(SUPER_REVIEW, SUPER)], values, review)
    assert len(queued) == len(SUPER_LENS_REVIEWERS)


async def test_each_counted_trigger_renders_its_own_name_and_is_booked_apart() -> None:
    booked: dict[str, ReviewTemplate] = {}
    other = SUPER.model_copy(update={"prompt": "{trigger} at {commit}", "role": STYLE_REVIEWER})
    engine = Engine({SUPER_REVIEW: SUPER, "drift": other})
    fired = [(SUPER_REVIEW, SUPER), ("drift", other)]
    values = review_values(WHOLE, 10, [])
    queued = await engine.engine.fire_counted(fired, values, ReviewRound(WHOLE, booked.__setitem__))
    assert [(origin, prompt.split("\n", 1)[0]) for _, origin, _, prompt in engine.shown()] == [
        *[(SUPER_REVIEW, "Super review of the codebase at c1abcde")] * 3,
        ("drift", "drift at c1abcde"),
    ]
    assert list(booked) == list(queued)


# Firing as a workspace opens

LINT = VerificationStep("Lint", ("ruff", "check"))
SETUP = Setup(command=("uv", "sync"))
STARTUP_CHECK = Trigger(
    on="session_started", checks="task-checks", when={"verification": "configured"}
)


def hello(**fields: object) -> Trigger:
    """A trigger queuing a coder's hello as a workspace opens with steps, with ``fields`` over
    that."""
    return Trigger.model_validate(
        {
            "on": "session_started",
            "role": CODER,
            "prompt": "Say hello",
            "when": {"verification": "configured"},
            **fields,
        }
    )


async def test_opening_with_steps_runs_the_checks_of_a_trigger_running_them_queuing_nothing() -> (
    None
):
    engine = Engine({"startup-check": STARTUP_CHECK})
    assert await engine.engine.started((LINT,)) == ()
    assert engine.checks_run == [TASK_CHECKS]
    assert engine.board.tasks == ()
    assert engine.events == []


@pytest.mark.parametrize(
    ("steps", "state"), [((LINT,), "configured"), ((), "missing"), (None, "off")]
)
async def test_opening_fires_the_triggers_taking_its_verification(
    steps: tuple[VerificationStep, ...] | None, state: str
) -> None:
    states = ("configured", "missing", "off")
    triggers = {name: hello(when={"verification": name}) for name in states}
    engine = Engine(triggers)
    queued = await engine.engine.started(steps)
    assert engine.shown() == [(CODER, state, "pending", "Say hello")]
    assert queued == (engine.board.tasks[0].id,)
    assert isinstance(engine.events[-1], TasksChanged)
    assert engine.kicked == []


async def test_opening_gives_the_checks_configured_now() -> None:
    tree = FakeTree()
    tree.setup = SETUP
    engine = Engine({"hello": hello(prompt="Hello. {checks}")}, tree)
    await engine.engine.started((LINT,))
    assert engine.shown()[0][3] == f"Hello. {configured_checks((LINT,), SETUP)}"


async def test_opening_with_verification_missing_queues_todays_configurator_first() -> None:
    engine = Engine(TRIGGERS, verification="missing")
    engine.board.add(TaskSpec(prompt="Add a flag."))
    queued = await engine.engine.started(())
    configurator = engine.board.tasks[0]
    assert queued == (configurator.id,)
    assert (configurator.task.role, configurator.origin) == (CONFIGURATOR, "set-up-verification")
    assert configurator.task.prompt == configure_prompt((), None)
    assert engine.checks_run == []


async def test_opening_with_steps_queues_no_configurator() -> None:
    engine = Engine(TRIGGERS, verification="configured")
    assert await engine.engine.started((LINT,)) == ()
    assert engine.board.tasks == ()
    assert engine.checks_run == [TASK_CHECKS]


async def test_opening_with_verification_off_is_told_nothing_is_configured() -> None:
    engine = Engine({"hello": hello(prompt="{checks}", when={})})
    await engine.engine.started(None)
    assert engine.shown()[0][3] == "Nothing is configured yet."


async def test_the_triggers_on_opening_each_fire_in_their_order() -> None:
    engine = Engine({"hello": hello(), "startup-check": STARTUP_CHECK, "bye": hello(prompt="Bye")})
    await engine.engine.started((LINT,))
    assert [prompt for _, _, _, prompt in engine.shown()] == ["Say hello", "Bye"]
    assert engine.checks_run == [TASK_CHECKS]


async def test_a_guard_holds_back_a_trigger_running_the_checks() -> None:
    engine = Engine(
        {"startup-check": STARTUP_CHECK.model_copy(update={"guards": ("suggestions-pending",)})}
    )
    review = TaskSpec(prompt="Review commit c0", role=REVIEWER)
    engine.board.add_triggered(review, "review", "review")
    await engine.engine.started((LINT,))
    assert engine.checks_run == []


async def test_a_disabled_trigger_running_the_checks_runs_nothing() -> None:
    engine = Engine({"startup-check": STARTUP_CHECK.model_copy(update={"enabled": False})})
    await engine.engine.started((LINT,))
    assert engine.checks_run == []


async def test_a_failing_step_adds_todays_startup_fix_as_a_draft_from_startup_fix() -> None:
    engine = Engine(TRIGGERS)
    queued = await engine.engine.check_failed(LINT, "E501 line too long\n")
    [draft] = engine.board.tasks
    assert queued == (draft.id,)
    assert (draft.task.role, draft.origin, draft.status) == (CODER, "startup-fix", "draft")
    assert draft.task.prompt == startup_fix_prompt(LINT, "E501 line too long\n")
    assert draft.task.settings == ROLES[CODER].settings
    assert draft.proposed_by is None
    assert isinstance(engine.events[-1], TasksChanged)


async def test_a_failing_step_adds_no_fix_while_the_coder_is_disabled() -> None:
    engine = Engine(TRIGGERS, roles=disabled(CODER))
    assert await engine.engine.check_failed(LINT, "1 test failed") == ()
    assert engine.board.tasks == ()
    assert engine.events == []


async def test_a_failing_step_fires_the_triggers_taking_its_step() -> None:
    triggers = {
        name.lower(): Trigger(
            on="startup_check_failed", role=CODER, prompt=f"{{step}} {name}", when={"step": name}
        )
        for name in ("Lint", "Tests")
    }
    engine = Engine(triggers)
    await engine.engine.check_failed(LINT, "")
    assert [(origin, prompt) for _, origin, _, prompt in engine.shown()] == [("lint", "Lint Lint")]


# Running


async def test_running_kicks_the_lane_of_each_role_of_the_tasks_still_on_the_board() -> None:
    engine = Engine({})
    first = engine.board.add(TaskSpec(prompt="a", role=REVIEWER))
    second = engine.board.add(TaskSpec(prompt="b", role=REVIEWER))
    gone = engine.board.add(TaskSpec(prompt="c", role=CODER))
    engine.board.remove(gone)
    engine.engine.run((first, second, gone))
    assert engine.kicked == [REVIEWER]


# Firing by hand on manual


async def test_configure_checks_fired_by_hand_queues_todays_configurator_first() -> None:
    tree = FakeTree()
    tree.setup = SETUP
    engine = Engine(TRIGGERS, tree)
    engine.board.add(TaskSpec(prompt="Add a flag."))
    queued = await engine.engine.fire_by_hand(CONFIGURE_CHECKS, (LINT,))
    first = engine.board.tasks[0]
    assert queued == (first.id,)
    assert (first.task.role, first.origin, first.status) == (
        CONFIGURATOR,
        CONFIGURE_CHECKS,
        "pending",
    )
    assert first.task.prompt == configure_prompt((LINT,), SETUP)
    assert first.task.settings == ROLES[CONFIGURATOR].settings
    assert isinstance(engine.events[-1], TasksChanged)
    assert engine.kicked == []


async def test_configure_checks_fired_by_hand_tells_the_configurator_the_sets() -> None:
    slow = CheckSet("slow", (VerificationStep("mutation", ("mutmut", "run"), "slow"),))
    mutation = Trigger(on="commits", every=10, checks="slow")
    triggers = {**TRIGGERS, "mutation": mutation}
    engine = Engine(triggers)
    await engine.engine.fire_by_hand(CONFIGURE_CHECKS, (LINT,), (slow,))
    prompt = engine.board.tasks[0].task.prompt
    assert prompt == configure_prompt((LINT,), None, (slow,), triggers)
    assert "Set slow: every 10 commits, reports\n- mutation: mutmut run" in prompt


async def test_opening_tells_the_configurator_the_sets() -> None:
    slow = CheckSet("slow", (VerificationStep("mutation", ("mutmut", "run"), "slow"),))
    engine = Engine({"hello": hello(prompt="{checks}", when={})})
    await engine.engine.started((), (slow,))
    assert engine.shown()[0][3] == configured_checks((), None, (slow,), {})


async def test_a_disabled_trigger_fired_by_hand_queues_nothing() -> None:
    off = TRIGGERS[CONFIGURE_CHECKS].model_copy(update={"enabled": False})
    engine = Engine({CONFIGURE_CHECKS: off})
    assert await engine.engine.fire_by_hand(CONFIGURE_CHECKS, ()) == ()
    assert engine.board.tasks == ()
    assert engine.events == []


# Running named sets


def slow(**fields: object) -> Trigger:
    """A trigger running the set ``slow`` on each landed coder commit, with ``fields`` over
    that."""
    return Trigger.model_validate(
        {"on": "task_landed", "checks": "slow", "when": {"role": CODER}, **fields}
    )


async def test_a_landing_runs_the_set_a_trigger_names_queuing_nothing() -> None:
    engine = Engine({"slow": slow()})
    assert await engine.engine.landed(ended(), COMMIT) == ()
    assert engine.checks_run == ["slow"]
    assert engine.board.tasks == ()
    assert engine.events == []


async def test_a_trigger_naming_a_set_fires_on_any_event_its_when_takes() -> None:
    engine = Engine({"slow": slow(on="task_finished", when={"status": "completed"})})
    await engine.engine.finished(ended(status="unverified"))
    await engine.engine.finished(ended())
    await engine.engine.started((LINT,))
    assert engine.checks_run == ["slow"]


async def test_a_guard_holds_back_a_trigger_naming_a_set() -> None:
    engine = Engine({"slow": slow(guards=("suggestions-pending",))})
    review = TaskSpec(prompt="Review commit c0", role=REVIEWER)
    engine.board.add_triggered(review, "review", "review")
    await engine.engine.landed(ended(), COMMIT)
    assert engine.checks_run == []


async def test_a_landing_counts_toward_a_commits_trigger_naming_a_set() -> None:
    every_two = slow(on="commits", every=2)
    engine = Engine({"slow": every_two})
    first = engine.engine.count(ended("1"), {})
    second = engine.engine.count(ended("2"), first.counts)
    assert (first.fires, second.fires) == ([], [("slow", every_two)])


async def test_counted_triggers_naming_a_set_run_it_and_the_others_are_left_to_queue() -> None:
    every_two = slow(on="commits", every=2)
    engine = Engine({"slow": every_two, SUPER_REVIEW: SUPER})
    left = engine.engine.fire_checks([("slow", every_two), (SUPER_REVIEW, SUPER)])
    assert left == [(SUPER_REVIEW, SUPER)]
    assert engine.checks_run == ["slow"]
    assert engine.board.tasks == ()


async def test_a_trigger_naming_a_set_fired_by_hand_on_manual_runs_it() -> None:
    nightly = Trigger(on="manual", checks="slow", manual=True, label="Slow checks")
    engine = Engine({"nightly": nightly})
    assert await engine.engine.fire_by_hand("nightly", (LINT,)) == ()
    assert engine.checks_run == ["slow"]
    assert engine.events == []


async def test_a_trigger_running_the_task_checks_does_not_fire_by_hand() -> None:
    check = Trigger(on="manual", checks="task-checks", manual=True, label="Check")
    engine = Engine({"check": check})
    assert await engine.engine.fire_by_hand("check", (LINT,)) == ()
    assert engine.checks_run == []


# Setting up verification first


async def test_a_task_queued_while_verification_is_missing_waits_with_a_notice() -> None:
    engine = Engine({"look": look()}, verification="missing")
    await engine.engine.landed(ended(), COMMIT)
    assert engine.shown() == [(REVIEWER, "look", "pending", "Look at c1abcde")]
    assert isinstance(engine.events[-2], TasksChanged)
    assert engine.events[-1] == Notice(message="look's task waits: set up verification first.")


async def test_each_task_queued_while_verification_is_missing_has_its_notice() -> None:
    engine = Engine({"look": look(), "peek": look(prompt="Peek")}, verification="missing")
    await engine.engine.landed(ended(), COMMIT)
    notes = [event.message for event in engine.events if isinstance(event, Notice)]
    assert notes == [
        "look's task waits: set up verification first.",
        "peek's task waits: set up verification first.",
    ]


async def test_the_configurators_task_queued_while_verification_is_missing_has_no_notice() -> None:
    engine = Engine(TRIGGERS, verification="missing")
    await engine.engine.fire_by_hand(CONFIGURE_CHECKS, ())
    assert [role for role, _, _, _ in engine.shown()] == [CONFIGURATOR]
    assert not any(isinstance(event, Notice) for event in engine.events)


@pytest.mark.parametrize("verification", ["configured", "off"])
async def test_a_task_queued_with_verification_set_up_or_off_has_no_notice(
    verification: VerificationState,
) -> None:
    engine = Engine({"look": look()}, verification=verification)
    await engine.engine.landed(ended(), COMMIT)
    assert len(engine.board.tasks) == 1
    assert not any(isinstance(event, Notice) for event in engine.events)


def nightly(**fields: object) -> Trigger:
    """A trigger running the set ``slow`` at 3 every night, with ``fields`` over that."""
    return Trigger.model_validate(
        {"on": "schedule", "cron": "0 3 * * *", "checks": "slow", **fields}
    )


async def test_a_schedule_come_due_runs_the_set_its_trigger_names_queuing_nothing() -> None:
    engine = Engine({"nightly": nightly(), "weekly": nightly(checks="weekly")})
    assert await engine.engine.scheduled(("nightly",)) == ()
    assert engine.checks_run == ["slow"]
    assert engine.events == []


async def test_a_schedule_come_due_queues_its_roles_task_named_after_its_trigger() -> None:
    tidy = nightly(checks=None, role=CODER, prompt="Tidy up after {trigger}")
    engine = Engine(
        {"tidy": tidy, "slow": look(), "off": tidy.model_copy(update={"enabled": False})}
    )
    [task_id] = await engine.engine.scheduled(("tidy", "off", "slow"))
    assert engine.shown() == [(CODER, "tidy", "pending", "Tidy up after tidy")]
    assert engine.board.get(task_id).queued_as == "task"
    assert engine.events == [TasksChanged(tasks=engine.board.tasks)]


async def test_a_guard_holds_back_a_schedules_task_while_its_last_still_waits() -> None:
    tidy = nightly(checks=None, role=CODER, prompt="Tidy up", guards=("not-queued",))
    engine = Engine({"tidy": tidy})
    await engine.engine.scheduled(("tidy",))
    assert await engine.engine.scheduled(("tidy",)) == ()
    assert len(engine.board.tasks) == 1


async def test_a_schedules_task_waits_for_verification_to_be_set_up_with_a_notice() -> None:
    tidy = nightly(checks=None, role=CODER, prompt="Tidy up")
    engine = Engine({"tidy": tidy}, verification="missing")
    await engine.engine.scheduled(("tidy",))
    assert engine.events[-1] == Notice(message="tidy's task waits: set up verification first.")
