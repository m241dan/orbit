import pytest

from orbit.agent.domain.checks import ChecksProposal, Setup, parse_proposal
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.verification import VerificationStep


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
