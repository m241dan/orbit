import pytest

from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.reviews import ReviewLimits
from orbit.agent.domain.roles import (
    BUILT_IN_ROLE_NAMES,
    ORBIT_TOOLS,
    ChecksProposal,
    ConfigError,
    OrbitConfig,
    Role,
    Setup,
    built_in_roles,
    load_config,
    parse_proposal,
    role_task,
    suggested_task,
)
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import Settings, TaskSpec

INSTRUCTIONS = {name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES}
BUILT_IN_ROLES = built_in_roles(INSTRUCTIONS)


def test_built_in_roles_cover_coding_reviewing_and_planning() -> None:
    assert set(BUILT_IN_ROLES) == {
        "coder",
        "reviewer",
        "super-reviewer",
        "planner",
        "configurator",
    }
    coder = BUILT_IN_ROLES["coder"]
    assert coder.verify and coder.tool_access == "full"
    assert set(coder.tools) == {
        "get_task",
        "complete_task",
        "run_check",
        "report_progress",
        "give_up",
    }
    reviewer = BUILT_IN_ROLES["reviewer"]
    assert not reviewer.verify and reviewer.tool_access == "restricted"
    assert "run_check" not in reviewer.tools
    assert "suggest_task" in reviewer.tools
    assert "suggest_task" in BUILT_IN_ROLES["super-reviewer"].tools
    assert "suggest_task" not in coder.tools
    planner = BUILT_IN_ROLES["planner"]
    assert set(planner.tools) == {"propose_task", "report_progress"}
    assert BUILT_IN_ROLES["super-reviewer"].model == "opus"
    assert all(role.instructions == INSTRUCTIONS[name] for name, role in BUILT_IN_ROLES.items())
    assert all(set(role.tools) <= set(ORBIT_TOOLS) for role in BUILT_IN_ROLES.values())


@pytest.mark.parametrize("text", [None, "", "  \n"])
def test_every_built_in_role_needs_instructions(text: str | None) -> None:
    instructions = {name: "x" for name in BUILT_IN_ROLE_NAMES if name != "planner"}
    if text is not None:
        instructions["planner"] = text
    with pytest.raises(ConfigError, match="planner"):
        built_in_roles(instructions)


def test_the_configurator_proposes_checks_without_editing_or_being_verified() -> None:
    configurator = BUILT_IN_ROLES["configurator"]
    assert configurator.tool_access == "restricted" and not configurator.verify
    assert configurator.tools == ("get_task", "propose_checks", "report_progress", "give_up")
    assert "propose_checks" in ORBIT_TOOLS


def test_a_proposal_reads_like_the_config() -> None:
    proposal = parse_proposal(
        {
            "steps": [{"name": "Lint", "command": ["ruff", "check"]}],
            "setup": {"command": ["uv", "sync"], "watch": ["uv.lock"]},
        }
    )
    assert proposal == ChecksProposal(
        steps=(VerificationStep("Lint", ("ruff", "check")),),
        setup=Setup(command=("uv", "sync"), watch=("uv.lock",)),
    )
    assert parse_proposal({"steps": [{"name": "T", "command": ["t"]}], "setup": None}).setup is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "steps"),
        ({"steps": []}, "steps"),
        ({"steps": [{"name": "", "command": ["x"]}]}, "step 1 has no name"),
        ({"steps": [{"name": "a", "command": []}]}, "step 1 has no command"),
        ({"steps": [{"name": "a", "command": ["x"]}], "setup": {"command": []}}, "setup.command"),
        ({"steps": [{"name": "a", "command": ["x"]}], "extra": 1}, "extra"),
    ],
)
def test_invalid_proposals_are_reported_readably(data: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_proposal(data)


def test_defaults_without_a_file() -> None:
    config = load_config({}, BUILT_IN_ROLES)
    assert config == OrbitConfig(roles=BUILT_IN_ROLES)
    assert config.roles == BUILT_IN_ROLES
    assert (config.fix_attempts, config.review_rounds, config.super_review_every) == (5, 3, 10)
    assert (config.review_suggestions, config.super_review_suggestions) == (5, 10)
    assert config.fallback_fixes == 3
    assert config.verification.steps == ()


def test_the_configs_limits_default_to_the_domains() -> None:
    config = OrbitConfig(roles=BUILT_IN_ROLES)
    assert config.limits == Limits()
    assert config.review_limits == ReviewLimits()


def test_a_file_overrides_settings_and_role_fields() -> None:
    config = load_config(
        {
            "fix_attempts": 2,
            "fallback_fixes": 4,
            "roles": {"coder": {"model": "sonnet", "timeout_minutes": 30}},
        },
        BUILT_IN_ROLES,
    )
    assert (config.fix_attempts, config.fallback_fixes) == (2, 4)
    coder = config.roles["coder"]
    assert (coder.model, coder.timeout_minutes) == ("sonnet", 30)
    assert coder.instructions == BUILT_IN_ROLES["coder"].instructions  # Untouched fields kept.
    assert config.roles["reviewer"] == BUILT_IN_ROLES["reviewer"]


def test_the_built_in_roles_are_the_ones_given() -> None:
    reviewer = Role(instructions="Review.", tools=("get_task",))
    assert load_config({}, {"reviewer": reviewer}).roles == {"reviewer": reviewer}


def test_a_file_can_add_a_role() -> None:
    config = load_config(
        {
            "roles": {
                "docs-writer": {
                    "instructions": "Write docs.",
                    "tools": ["get_task", "complete_task"],
                    "verify": False,
                }
            }
        },
        BUILT_IN_ROLES,
    )
    writer = config.roles["docs-writer"]
    assert writer == Role(
        instructions="Write docs.", tools=("get_task", "complete_task"), verify=False
    )
    assert writer.tool_access == "restricted"


def test_a_file_lists_verification_steps_in_order() -> None:
    config = load_config(
        {
            "verification": {
                "steps": [
                    {"name": "Lint", "command": ["ruff", "check"]},
                    {"name": "Tests", "command": ["pytest"]},
                ]
            }
        },
        BUILT_IN_ROLES,
    )
    assert config.verification.steps == (
        VerificationStep(name="Lint", command=("ruff", "check")),
        VerificationStep(name="Tests", command=("pytest",)),
    )


def test_without_a_setup_table_there_is_no_setup() -> None:
    assert load_config({}, BUILT_IN_ROLES).setup is None


def test_setup_watches_nothing_unless_told() -> None:
    config = load_config({"setup": {"command": ["make", "deps"]}}, BUILT_IN_ROLES)
    assert config.setup == Setup(command=("make", "deps"), watch=())


def test_setup_lists_the_files_it_watches() -> None:
    config = load_config(
        {"setup": {"command": ["uv", "sync"], "watch": ["uv.lock", "a/b.txt"]}}, BUILT_IN_ROLES
    )
    assert config.setup == Setup(command=("uv", "sync"), watch=("uv.lock", "a/b.txt"))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"fix_attempts": 0}, "fix_attempts"),
        ({"max_checks": 0}, "max_checks"),
        ({"fallback_fixes": 0}, "fallback_fixes"),
        ({"surprise": 1}, "surprise"),
        ({"roles": "everyone"}, "roles"),
        ({"roles": {"coder": {"tools": ["launch_rockets"]}}}, "launch_rockets"),
        ({"roles": {"coder": {"tool_access": "sometimes"}}}, "roles.coder.tool_access"),
        ({"roles": {"new": {"tools": ["get_task"]}}}, "roles.new.instructions"),
        ({"roles": {"coder": {"timeout_minutes": 0}}}, "timeout_minutes"),
        ({"verification": {"steps": [{"name": "", "command": ["x"]}]}}, "step 1 has no name"),
        ({"verification": {"steps": [{"name": "a", "command": []}]}}, "step 1 has no command"),
        ({"verification": {"steps": [{"name": "  ", "command": ["x"]}]}}, "step 1 has no name"),
        ({"verification": {"steps": [{"name": "a", "command": [""]}]}}, "step 1 has no program"),
        (
            {"verification": {"steps": [{"name": "a", "command": ["  ", "x"]}]}},
            "step 1 has no program",
        ),
        ({"verification": {"steps": [{"name": "a"}]}}, "verification.steps.0.command"),
        (
            {
                "verification": {
                    "steps": [
                        {"name": "a", "command": ["x"]},
                        {"name": "b", "command": ["y"]},
                        {"name": "a", "command": ["z"]},
                    ]
                }
            },
            "step 3 has the same name as step 1",
        ),
        ({"verification": {"steps": [{"name": "a", "command": "x", "b": 1}]}}, "steps.0.b"),
        ({"verification": {"surprise": 1}}, "verification.surprise"),
        ({"setup": {}}, "setup.command"),
        ({"setup": {"command": []}}, "setup.command"),
        ({"setup": {"command": ["uv"], "watch": ["/etc/passwd"]}}, "relative"),
        ({"setup": {"command": ["uv"], "watch": ["../uv.lock"]}}, "relative"),
        ({"setup": {"command": ["uv"], "watch": [""]}}, "relative"),
        ({"setup": {"command": ["uv"], "surprise": 1}}, "setup.surprise"),
    ],
)
def test_mistakes_are_reported_readably(data: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(data, BUILT_IN_ROLES)
    assert message in str(raised.value)
    assert "\n" not in str(raised.value).split(":", 1)[0]


def test_a_new_role_must_be_able_to_fetch_its_task() -> None:
    with pytest.raises(ConfigError, match="get_task"):
        roles = {"new": {"instructions": "x", "tools": ["complete_task"]}}
        load_config({"roles": roles}, BUILT_IN_ROLES)


def test_a_conversational_role_plans_instead_of_fetching_a_task() -> None:
    roles = {"architect": {"instructions": "x", "tools": ["propose_task"]}}
    config = load_config({"roles": roles}, BUILT_IN_ROLES)
    assert config.roles["architect"].tools == ("propose_task",)


def test_a_roles_settings_are_its_model_and_tool_access() -> None:
    role = Role(instructions="Do it.", model="opus", tool_access="full")
    assert role.settings == Settings(model="opus", tool_access="full")
    assert BUILT_IN_ROLES["reviewer"].settings == Settings()


def test_a_role_task_runs_in_the_role_with_its_settings() -> None:
    reviewer = BUILT_IN_ROLES["super-reviewer"]
    assert role_task("super-reviewer", reviewer, "Review.") == TaskSpec(
        prompt="Review.", settings=Settings(model="opus"), role="super-reviewer"
    )


def test_a_suggested_task_is_a_coders_titled_prompt() -> None:
    coder = BUILT_IN_ROLES["coder"]
    assert suggested_task(coder, " Add the flag \n", "\nAdd --fast. ") == TaskSpec(
        prompt="Add the flag\n\nAdd --fast.", settings=Settings(tool_access="full")
    )
