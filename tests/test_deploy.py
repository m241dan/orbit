"""The container files in ``deploy/``, read as text: they match orbit and keep secrets out."""

import re
from pathlib import Path

import pytest

from orbit.agent.domain.server import ServerSettings

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "deploy" / "Dockerfile").read_text()
COMPOSE = (ROOT / "deploy" / "compose.yaml").read_text()
DEPLOY_README = (ROOT / "deploy" / "README.md").read_text()
DOCKERIGNORE = (ROOT / ".dockerignore").read_text()

MOUNT = re.compile(r"^\s+- (?P<source>[^\s:\"]+):(?P<target>/[^\s:]+)(?P<read_only>:ro)?$", re.M)
SECRET = re.compile(r"\.env|token|key|\.pem", re.I)


def published_ports(compose: str) -> list[tuple[int, int]]:
    """The host and container ports ``compose`` publishes, left out when commented."""
    found = re.findall(r'^\s+- "(\d+):(\d+)"$', compose, re.M)
    return [(int(host), int(container)) for host, container in found]


def healthcheck(compose: str) -> str:
    """The healthcheck's command in ``compose``, or nothing without one."""
    found = re.search(r"^\s+healthcheck:\n\s+test: (.+)$", compose, re.M)
    return found.group(1) if found else ""


def mounts(compose: str) -> dict[str, tuple[str, bool]]:
    """The source of each mount in ``compose`` by its target, and whether it's read-only."""
    return {
        found["target"]: (found["source"], found["read_only"] is not None)
        for found in MOUNT.finditer(compose)
    }


def environment(dockerfile: str, name: str) -> str:
    """The value ``dockerfile`` sets the environment variable ``name`` to."""
    found = re.search(rf"^\s*(?:ENV\s+)?{name}=(\S+?)(?:\s+\\)?$", dockerfile, re.M)
    assert found, f"the Dockerfile doesn't set {name}"
    return found.group(1)


def copies_secret(line: str) -> bool:
    """Whether the Dockerfile ``line`` copies something named like a credential."""
    return bool(re.match(r"(COPY|ADD)\s", line) and SECRET.search(line))


def secret_lines(dockerfile: str) -> list[str]:
    """The lines of ``dockerfile`` that copy a credential into the image or set the API key."""
    lines = [line.strip() for line in dockerfile.splitlines()]
    return [line for line in lines if copies_secret(line) or "ANTHROPIC_API_KEY" in line]


def test_the_published_port_is_orbits_default() -> None:
    port = ServerSettings().port
    assert published_ports(COMPOSE) == [(port, port)]
    assert f"EXPOSE {port}" in DOCKERFILE


def test_a_different_published_port_is_caught() -> None:
    assert published_ports(COMPOSE.replace('"7433:7433"', '"8080:8080"')) == [(8080, 8080)]


def test_the_healthcheck_asks_readyz_on_the_default_port() -> None:
    assert f"https://127.0.0.1:{ServerSettings().port}/readyz" in healthcheck(COMPOSE)


def test_a_healthcheck_on_another_path_is_caught() -> None:
    assert "/readyz" not in healthcheck(COMPOSE.replace("/readyz", "/healthz"))


def test_without_a_healthcheck_there_is_no_command() -> None:
    assert healthcheck("services:\n  orbit:\n    image: orbit\n") == ""


def test_the_repository_is_mounted_at_the_workspace_the_entrypoint_serves() -> None:
    assert mounts(COMPOSE)["/workspace"] == ("..", False)
    assert 'ENTRYPOINT ["orbit", "serve", "--add", "/workspace"]' in DOCKERFILE


@pytest.mark.parametrize(
    ("variable", "source"),
    [
        ("ORBIT_CONFIG_DIR", "./orbit-config"),
        ("ORBIT_STATE_DIR", "orbit-state"),
        ("CLAUDE_CONFIG_DIR", "claude-login"),
    ],
)
def test_each_directory_the_image_sets_is_mounted_writable(variable: str, source: str) -> None:
    assert mounts(COMPOSE)[environment(DOCKERFILE, variable)] == (source, False)


def test_the_hosts_gitconfig_is_mounted_read_only_in_the_users_home() -> None:
    home = environment(DOCKERFILE, "HOME")
    assert mounts(COMPOSE)[f"{home}/.gitconfig"] == ("~/.gitconfig", True)


def test_a_removed_mount_is_caught() -> None:
    edited = COMPOSE.replace("      - orbit-state:/home/orbit/.local/state/orbit\n", "")
    assert environment(DOCKERFILE, "ORBIT_STATE_DIR") not in mounts(edited)


def test_an_unset_variable_is_reported() -> None:
    with pytest.raises(AssertionError, match="ORBIT_STATE_DIR"):
        environment("FROM python:3.13-slim\n", "ORBIT_STATE_DIR")


def test_the_container_runs_as_the_hosts_user() -> None:
    assert re.search(r'^\s+user: "\$\{UID:-1000\}:\$\{GID:-1000\}"$', COMPOSE, re.M)


def test_the_dockerfile_copies_no_credential() -> None:
    assert secret_lines(DOCKERFILE) == []


@pytest.mark.parametrize(
    "line",
    [
        "COPY deploy/.env /home/orbit/.env",
        "COPY orbit-config/token /home/orbit/.config/orbit/token",
        "ADD server-key.pem /etc/orbit/",
        "ENV ANTHROPIC_API_KEY=sk-ant-secret",
    ],
)
def test_a_copied_credential_is_caught(line: str) -> None:
    assert secret_lines(f"{DOCKERFILE}{line}\n") == [line]


@pytest.mark.parametrize("pattern", ["**/.env", "**/orbit-config", "**/*.pem", ".git/orbit"])
def test_the_build_context_leaves_out_secrets(pattern: str) -> None:
    assert pattern in DOCKERIGNORE.splitlines()


def test_the_deploy_readme_points_at_the_main_readme_section() -> None:
    assert "../README.md#hosting-in-a-container" in DEPLOY_README
    assert "\n### Hosting in a container\n" in (ROOT / "README.md").read_text()
