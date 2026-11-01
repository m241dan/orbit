"""The triggers shipped with orbit, read from the triggers.toml and templates beside it, whose
templates rendered from today's inputs are today's prompts."""

from importlib.resources import files
from pathlib import Path

import pytest

from orbit.agent.domain.checks import Setup, configure_prompt, configured_checks
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.reviews import LandedSince, ReviewJob, review_prompt, review_values
from orbit.agent.domain.triggers import Trigger, render_template
from orbit.agent.domain.verification import VerificationStep, failure_report, startup_fix_prompt
from orbit.agent.external.built_in_triggers import (
    TRIGGER_DIRECTORY,
    read_built_in_triggers,
    read_template_files,
)

FOLDER = files("orbit.agent.external") / "triggers"
README = Path(__file__).parent.parent / "README.md"
SHIPPED = read_built_in_triggers()
TEMPLATES = read_template_files()


def template(name: str) -> str:
    """The text of the shipped trigger ``name``'s template."""
    prompt = SHIPPED[name].prompt
    assert prompt is not None
    return prompt


def directory(tmp_path: Path, triggers: str, **templates: str) -> Path:
    """A directory holding ``triggers`` as its ``triggers.toml`` and each of ``templates``, by
    its file's name without ``.md``."""
    (tmp_path / "triggers.toml").write_text(triggers, encoding="utf-8")
    for name, text in templates.items():
        (tmp_path / f"{name}.md").write_text(text, encoding="utf-8")
    return tmp_path


# The shipped triggers


def test_the_shipped_triggers_are_the_records_in_order() -> None:
    fields = {
        name: trigger.model_copy(update={"prompt": None}) for name, trigger in SHIPPED.items()
    }
    assert fields == {
        "review": Trigger(on="task_landed", role="reviewer", guards=("review-rounds",)),
        "super-review": Trigger(
            on="commits",
            role="super-reviewer",
            when={"origin": ("manual", "planner")},
            guards=("suggestions-pending",),
            every=10,
            manual=True,
            label="Super review",
        ),
        "configure-checks": Trigger(
            on="manual", role="configurator", manual=True, label="Set up checks"
        ),
        "set-up-verification": Trigger(
            on="session_started", role="configurator", when={"verification": "missing"}
        ),
        "startup-check": Trigger(
            on="session_started", checks="gate", when={"verification": "configured"}
        ),
        "startup-fix": Trigger(on="startup_check_failed", role="coder", draft=True),
    }


def test_each_prompt_is_the_text_of_the_template_beside_triggers_toml_it_names() -> None:
    assert TEMPLATES == {
        "review": "review.md",
        "super-review": "super-review.md",
        "configure-checks": "configure-checks.md",
        "set-up-verification": "configure-checks.md",
        "startup-fix": "startup-fix.md",
    }
    for name, file in TEMPLATES.items():
        assert template(name) == (FOLDER / file).read_text(encoding="utf-8").strip()
    assert SHIPPED["startup-check"].prompt is None


def test_every_template_beside_triggers_toml_is_one_a_trigger_names() -> None:
    names = {entry.name for entry in FOLDER.iterdir() if entry.name.endswith(".md")}
    assert names == set(TEMPLATES.values())


def test_the_readme_lists_every_shipped_trigger() -> None:
    readme = README.read_text(encoding="utf-8")
    assert all(f"| `{name}` |" in readme for name in SHIPPED)


def test_the_package_directory_is_the_one_read_by_default() -> None:
    assert read_built_in_triggers(TRIGGER_DIRECTORY) == SHIPPED


# Reading a directory of triggers


def test_an_inline_prompt_is_the_text_itself(tmp_path: Path) -> None:
    folder = directory(tmp_path, '[triggers.x]\non = "manual"\nrole = "coder"\nprompt = "Go"\n')
    assert read_built_in_triggers(folder)["x"].prompt == "Go"
    assert read_template_files(folder) == {}


def test_a_missing_template_is_refused_naming_it(tmp_path: Path) -> None:
    folder = directory(tmp_path, '[triggers.x]\non = "manual"\nrole = "coder"\nprompt = "x.md"\n')
    with pytest.raises(ConfigError, match=f"^{tmp_path / 'x.md'}: "):
        read_built_in_triggers(folder)


def test_an_invalid_trigger_is_refused_naming_the_file(tmp_path: Path) -> None:
    folder = directory(tmp_path, '[triggers.x]\non = "someday"\n')
    with pytest.raises(ConfigError, match=f"^{tmp_path / 'triggers.toml'}: triggers.x.on: "):
        read_built_in_triggers(folder)
    with pytest.raises(ConfigError, match=f"^{tmp_path / 'triggers.toml'}: triggers.x.on: "):
        read_template_files(folder)


def test_a_template_using_a_variable_its_event_lacks_is_refused_naming_the_file(
    tmp_path: Path,
) -> None:
    triggers = '[triggers.x]\non = "task_finished"\nrole = "coder"\nprompt = "x.md"\n'
    folder = directory(tmp_path, triggers, x="Look at {diff}")
    with pytest.raises(ConfigError) as raised:
        read_built_in_triggers(folder)
    assert str(raised.value) == (
        f"{tmp_path / 'triggers.toml'}: triggers.x.prompt: {{diff}} isn't a variable of"
        " task_finished; its variables are {trigger}."
    )


def test_a_trigger_for_a_role_orbit_doesnt_ship_is_refused(tmp_path: Path) -> None:
    folder = directory(tmp_path, '[triggers.x]\non = "manual"\nrole = "tidier"\nprompt = "Go"\n')
    with pytest.raises(ConfigError, match=r"triggers\.x\.role: tidier isn't a role; the roles are"):
        read_built_in_triggers(folder)


# Rendering the shipped templates from today's inputs


COMMIT = "0123456789abcdef"
DIFF = "Add the thing\n\ndiff --git a/x.py b/x.py\n+x = 1\n\n\n+y = 2\n"
PENDING = ("Tidy the parser", "Speed up the tests")
LANDED = LandedSince(subjects=("Fix the lexer", "Name the tokens"))
REVIEWS = {
    "a commit": (ReviewJob(commits=(COMMIT,), round=0, batch=None), (), LandedSince()),
    "a commit with queued tasks and landings since": (
        ReviewJob(commits=(COMMIT,), round=0, batch=None),
        PENDING,
        LANDED,
    ),
    "a batch with missing tasks": (
        ReviewJob(commits=("aaaaaaaaa", COMMIT), round=1, batch="batch-2", missing=("4", "7")),
        PENDING,
        LandedSince(),
    ),
    "a batch with a missing task and landings since": (
        ReviewJob(commits=(COMMIT,), round=2, batch="batch-3", missing=("9",)),
        (),
        LANDED,
    ),
}
"""Reviews of commits by what they're told: each job, the titles queued and what landed since."""


@pytest.mark.parametrize("case", list(REVIEWS))
def test_the_review_template_renders_todays_review_prompt(case: str) -> None:
    job, pending, landed = REVIEWS[case]
    values = review_values(job, DIFF, 5, pending, landed) | {
        "trigger": "review",
        "task": "3",
        "task_skills": "python",
    }
    assert render_template(template("review"), values) == review_prompt(
        job, DIFF, 5, pending, landed
    )


SUPER = ReviewJob(commits=(COMMIT,), round=0, batch=None, whole=True)
SUPER_REVIEWS = {
    "the first": ((), LandedSince()),
    "commits, changed files and a sample": (
        PENDING,
        LandedSince(
            subjects=("Fix the lexer", "Name the tokens"),
            files=("src/b.py", "src/a.py"),
            sampled=("src/c.py", "src/d.py"),
        ),
    ),
    "commits that changed no files, without a sample": ((), LandedSince(subjects=("Empty",))),
    "only a sample": (PENDING, LandedSince(sampled=("src/c.py",))),
}
"""Super reviews by what they're told: the titles queued and what landed since."""


@pytest.mark.parametrize("case", list(SUPER_REVIEWS))
def test_the_super_review_template_renders_todays_super_review_prompt(case: str) -> None:
    pending, landed = SUPER_REVIEWS[case]
    values = review_values(SUPER, "", 10, pending, landed) | {"trigger": "super-review"}
    assert render_template(template("super-review"), values) == review_prompt(
        SUPER, "", 10, pending, landed
    )


LINT = VerificationStep("Lint", ("ruff", "check"))
TESTS = VerificationStep("Tests", ("uv", "run", "pytest"))
CHECKS = {
    "nothing": ((), None),
    "steps": ((LINT, TESTS), None),
    "steps and a setup": ((LINT,), Setup(command=("uv", "sync"), watch=("uv.lock",))),
    "only a setup": ((), Setup(command=("make", "deps"))),
}
"""What's configured when the configurator is asked for: steps and a setup."""


@pytest.mark.parametrize("name", ["configure-checks", "set-up-verification"])
@pytest.mark.parametrize("case", list(CHECKS))
def test_the_configurators_template_renders_todays_configure_prompt(name: str, case: str) -> None:
    steps, setup = CHECKS[case]
    values = {"trigger": name, "checks": configured_checks(steps, setup)}
    assert render_template(template(name), values) == configure_prompt(steps, setup)


@pytest.mark.parametrize("output", ["FAILED test_x\n\nAssertionError\n", "", "x" * 20000])
def test_the_startup_fix_template_renders_todays_startup_fix_prompt(output: str) -> None:
    values = {"trigger": "startup-fix", "step": TESTS.name, "output": failure_report(TESTS, output)}
    assert render_template(template("startup-fix"), values) == startup_fix_prompt(TESTS, output)
