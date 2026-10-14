import pytest

from orbit.agent.domain.roles import (
    BUILT_IN_ROLES,
    ORBIT_TOOLS,
    ConfigError,
    OrbitConfig,
    Role,
    load_config,
)


def test_built_in_roles_cover_coding_reviewing_and_planning() -> None:
    assert set(BUILT_IN_ROLES) == {"coder", "reviewer", "super-reviewer", "planner"}
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
    assert all(role.instructions for role in BUILT_IN_ROLES.values())
    assert "don't commit" in coder.instructions.lower()
    assert all(set(role.tools) <= set(ORBIT_TOOLS) for role in BUILT_IN_ROLES.values())


def test_defaults_without_a_file() -> None:
    config = load_config({})
    assert config == OrbitConfig()
    assert config.roles == BUILT_IN_ROLES
    assert (config.fix_attempts, config.review_rounds, config.super_review_every) == (5, 3, 10)
    assert (config.review_suggestions, config.super_review_suggestions) == (5, 10)


def test_a_file_overrides_settings_and_role_fields() -> None:
    config = load_config(
        {
            "fix_attempts": 2,
            "roles": {"coder": {"model": "sonnet", "timeout_minutes": 30}},
        }
    )
    assert config.fix_attempts == 2
    coder = config.roles["coder"]
    assert (coder.model, coder.timeout_minutes) == ("sonnet", 30)
    assert coder.instructions == BUILT_IN_ROLES["coder"].instructions  # Untouched fields kept.
    assert config.roles["reviewer"] == BUILT_IN_ROLES["reviewer"]


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
        }
    )
    writer = config.roles["docs-writer"]
    assert writer == Role(
        instructions="Write docs.", tools=("get_task", "complete_task"), verify=False
    )
    assert writer.tool_access == "restricted"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"fix_attempts": 0}, "fix_attempts"),
        ({"surprise": 1}, "surprise"),
        ({"roles": {"coder": {"tools": ["launch_rockets"]}}}, "launch_rockets"),
        ({"roles": {"coder": {"tool_access": "sometimes"}}}, "roles.coder.tool_access"),
        ({"roles": {"new": {"tools": ["get_task"]}}}, "roles.new.instructions"),
        ({"roles": {"coder": {"timeout_minutes": 0}}}, "timeout_minutes"),
    ],
)
def test_mistakes_are_reported_readably(data: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(data)
    assert message in str(raised.value)
    assert "\n" not in str(raised.value).split(":", 1)[0]


def test_a_new_role_must_be_able_to_fetch_its_task() -> None:
    with pytest.raises(ConfigError, match="get_task"):
        load_config({"roles": {"new": {"instructions": "x", "tools": ["complete_task"]}}})


def test_a_conversational_role_plans_instead_of_fetching_a_task() -> None:
    config = load_config({"roles": {"architect": {"instructions": "x", "tools": ["propose_task"]}}})
    assert config.roles["architect"].tools == ("propose_task",)
