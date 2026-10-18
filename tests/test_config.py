import pytest

from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.checks import Setup
from orbit.agent.domain.config import OrbitConfig, load_config
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.reviews import ReviewLimits
from orbit.agent.domain.roles import Role, built_in_roles
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import BUILT_IN_ROLE_NAMES

INSTRUCTIONS = {name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES}
BUILT_IN_ROLES = built_in_roles(INSTRUCTIONS)


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
