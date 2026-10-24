"""The container files in ``deploy/``, read as text: they match orbit and keep secrets out."""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from orbit.agent.domain.server import ServerSettings

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "deploy" / "Dockerfile").read_text()
COMPOSE = (ROOT / "deploy" / "compose.yaml").read_text()
DEPLOY_README = (ROOT / "deploy" / "README.md").read_text()
DOCKERIGNORE = (ROOT / ".dockerignore").read_text()

SHORT_MOUNT = re.compile(
    r"^\s+- (?P<source>[^\s:\"]+):(?P<target>/[^\s:]+)(?P<read_only>:ro)?$", re.M
)
LONG_MOUNT = re.compile(
    r"^(?P<indent> +)- type: (?P<type>\w+)\n(?P<body>(?:(?P=indent)  .+\n)+)", re.M
)
SECRET = re.compile(r"\.env|token|key|\.pem", re.I)


def published_ports(compose: str) -> list[tuple[int, int]]:
    """The host and container ports ``compose`` publishes, left out when commented."""
    found = re.findall(r'^\s+- "(\d+):(\d+)"$', compose, re.M)
    return [(int(host), int(container)) for host, container in found]


def healthcheck(compose: str) -> str:
    """The healthcheck's command in ``compose``, or nothing without one."""
    found = re.search(r"^\s+healthcheck:\n\s+test: (.+)$", compose, re.M)
    return found.group(1) if found else ""


@dataclass(frozen=True)
class Mount:
    """A mount's type, source, whether it's read-only and whether compose makes a missing source."""

    type: str
    source: str
    read_only: bool = False
    create_host_path: bool = True


def short_mount(source: str, read_only: bool) -> Mount:
    """The mount ``source:target`` gives: a bind for a path, else a named volume."""
    kind = "bind" if source.startswith((".", "/", "~")) else "volume"
    return Mount(kind, source, read_only)


def field(body: str, name: str) -> str | None:
    """The value of the key ``name`` in the long-syntax mount ``body``, if it has one."""
    found = re.search(rf"^\s+{name}: (\S+)$", body, re.M)
    return found.group(1) if found else None


def long_mount(kind: str, body: str) -> Mount:
    """The mount the long syntax ``body`` gives, of type ``kind``, with compose's defaults."""
    return Mount(
        kind,
        field(body, "source") or "",
        read_only=field(body, "read_only") == "true",
        create_host_path=field(body, "create_host_path") != "false",
    )


def mounts(compose: str) -> dict[str, Mount]:
    """Each mount in ``compose``, in the short or the long syntax, by its target."""
    found = {
        short["target"]: short_mount(short["source"], short["read_only"] is not None)
        for short in SHORT_MOUNT.finditer(compose)
    }
    for long in LONG_MOUNT.finditer(compose):
        found[field(long["body"], "target") or ""] = long_mount(long["type"], long["body"])
    return found


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
    assert mounts(COMPOSE)["/workspace"] == Mount("bind", "..")
    assert 'ENTRYPOINT ["orbit", "serve", "--add", "/workspace"]' in DOCKERFILE


@pytest.mark.parametrize(
    ("variable", "source"),
    [
        ("ORBIT_STATE_DIR", "orbit-state"),
        ("CLAUDE_CONFIG_DIR", "claude-login"),
    ],
)
def test_each_volume_the_image_sets_is_mounted_writable(variable: str, source: str) -> None:
    assert mounts(COMPOSE)[environment(DOCKERFILE, variable)] == Mount("volume", source)


def test_the_config_directory_is_a_writable_bind_compose_wont_create() -> None:
    target = environment(DOCKERFILE, "ORBIT_CONFIG_DIR")
    assert mounts(COMPOSE)[target] == Mount("bind", "./orbit-config", create_host_path=False)


def test_the_hosts_gitconfig_is_a_read_only_bind_compose_wont_create() -> None:
    target = f"{environment(DOCKERFILE, 'HOME')}/.gitconfig"
    expected = Mount("bind", "~/.gitconfig", read_only=True, create_host_path=False)
    assert mounts(COMPOSE)[target] == expected


@pytest.mark.parametrize(("variable", "under"), [("ORBIT_CONFIG_DIR", ""), ("HOME", "/.gitconfig")])
def test_a_bind_compose_would_create_is_caught(variable: str, under: str) -> None:
    edited = COMPOSE.replace("        bind:\n          create_host_path: false\n", "")
    assert mounts(edited)[environment(DOCKERFILE, variable) + under].create_host_path


def test_a_writable_gitconfig_is_caught() -> None:
    edited = COMPOSE.replace("        read_only: true\n", "")
    assert not mounts(edited)[f"{environment(DOCKERFILE, 'HOME')}/.gitconfig"].read_only


def test_a_removed_mount_is_caught() -> None:
    edited = COMPOSE.replace("      - orbit-state:/home/orbit/.local/state/orbit\n", "")
    assert environment(DOCKERFILE, "ORBIT_STATE_DIR") not in mounts(edited)


def test_an_unset_variable_is_reported() -> None:
    with pytest.raises(AssertionError, match="ORBIT_STATE_DIR"):
        environment("FROM python:3.14-slim\n", "ORBIT_STATE_DIR")


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


@pytest.mark.parametrize("pattern", ["**/.env", "**/orbit-config", "**/*.pem", ".git"])
def test_the_build_context_leaves_out_secrets(pattern: str) -> None:
    assert pattern in DOCKERIGNORE.splitlines()


def test_the_deploy_readme_points_at_the_main_readme_section() -> None:
    assert "../README.md#hosting-in-a-container" in DEPLOY_README
    assert "\n### Hosting in a container\n" in (ROOT / "README.md").read_text()
