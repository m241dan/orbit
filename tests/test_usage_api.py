import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from orbit.agent.domain.usage import UsageRateLimited, UsageReport, UsageUnavailable
from orbit.agent.external.usage_api import BETA_HEADER, TIMEOUT_SECONDS, USAGE_URL, UsageApi
from orbit.protocol import UsageLimit

TOKEN = "sk-ant-oat01-secret-token"

Handler = Callable[[httpx.Request], httpx.Response]

USAGE = {
    "five_hour": {"utilization": 42.0, "resets_at": "2026-10-16T12:00:00+00:00"},
    "seven_day": {"utilization": 7, "resets_at": None},
}


def credentials(tmp_path: Path, content: object) -> Path:
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps(content))
    return path


def logged_in(tmp_path: Path) -> Path:
    return credentials(tmp_path, {"claudeAiOauth": {"accessToken": TOKEN}})


def api(path: Path, handler: Handler) -> UsageApi:
    return UsageApi(credentials=path, transport=httpx.MockTransport(handler))


def responding(response: httpx.Response) -> Handler:
    return lambda request: response


def unreachable(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no request should be made")


async def test_a_successful_fetch_reports_the_limits(tmp_path: Path) -> None:
    fetch = api(logged_in(tmp_path), responding(httpx.Response(200, json=USAGE)))
    assert await fetch() == UsageReport(
        (
            UsageLimit(
                label="Session", percent=42.0, resets_at=datetime(2026, 10, 16, 12, tzinfo=UTC)
            ),
            UsageLimit(label="Week", percent=7.0, resets_at=None),
        )
    )


async def test_the_request_goes_to_the_usage_url_with_the_token(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    await api(logged_in(tmp_path), handler)()
    [request] = requests
    assert request.method == "GET"
    assert str(request.url) == USAGE_URL == "https://api.anthropic.com/api/oauth/usage"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["anthropic-beta"] == BETA_HEADER == "oauth-2025-04-20"
    assert request.headers["Accept"] == "application/json"


async def test_the_request_times_out_after_ten_seconds(tmp_path: Path) -> None:
    timeouts: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={})

    await api(logged_in(tmp_path), handler)()
    assert TIMEOUT_SECONDS == 10
    assert timeouts == [httpx.Timeout(10).as_dict()]


async def test_the_credentials_are_read_on_every_call(tmp_path: Path) -> None:
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["Authorization"])
        return httpx.Response(200, json={})

    path = logged_in(tmp_path)
    fetch = api(path, handler)
    await fetch()
    credentials(tmp_path, {"claudeAiOauth": {"accessToken": "refreshed"}})
    await fetch()
    assert tokens == [f"Bearer {TOKEN}", "Bearer refreshed"]


def test_the_credentials_default_to_claude_codes_file() -> None:
    assert UsageApi().credentials == Path.home() / ".claude" / ".credentials.json"


async def test_a_missing_credentials_file_is_no_login(tmp_path: Path) -> None:
    fetch = api(tmp_path / "missing.json", unreachable)
    assert await fetch() == UsageUnavailable("no Claude login")


async def test_an_unreadable_credentials_file_is_no_login(tmp_path: Path) -> None:
    fetch = api(tmp_path, unreachable)  # A directory can't be read as a file.
    assert await fetch() == UsageUnavailable("no Claude login")


async def test_credentials_that_arent_json_are_no_login(tmp_path: Path) -> None:
    path = tmp_path / ".credentials.json"
    path.write_text("{not json")
    assert await api(path, unreachable)() == UsageUnavailable("no Claude login")


async def test_credentials_that_arent_utf8_are_no_login(tmp_path: Path) -> None:
    path = tmp_path / ".credentials.json"
    path.write_bytes(b"\xff\xfe\xfa")
    assert await api(path, unreachable)() == UsageUnavailable("no Claude login")


@pytest.mark.parametrize(
    "content",
    [
        [],
        {},
        {"claudeAiOauth": "token"},
        {"claudeAiOauth": {}},
        {"claudeAiOauth": {"accessToken": ""}},
        {"claudeAiOauth": {"accessToken": 42}},
        {"claudeAiOauth": {"accessToken": "tökén"}},
        {"apiKey": "sk-ant-api"},
    ],
)
async def test_credentials_without_a_token_are_no_login(tmp_path: Path, content: object) -> None:
    fetch = api(credentials(tmp_path, content), unreachable)
    assert await fetch() == UsageUnavailable("no Claude login")


async def test_a_response_that_isnt_json_is_unexpected(tmp_path: Path) -> None:
    fetch = api(logged_in(tmp_path), responding(httpx.Response(200, text="<html>")))
    assert await fetch() == UsageUnavailable("unexpected response")


async def test_a_huge_utilization_is_reported_as_full(tmp_path: Path) -> None:
    body = '{"five_hour": {"utilization": 1' + "0" * 400 + "}}"
    fetch = api(logged_in(tmp_path), responding(httpx.Response(200, text=body)))
    assert await fetch() == UsageReport(
        (UsageLimit(label="Session", percent=100.0, resets_at=None),)
    )


async def test_a_malformed_usage_payload_is_unexpected(tmp_path: Path) -> None:
    fetch = api(logged_in(tmp_path), responding(httpx.Response(200, json=[1])))
    assert await fetch() == UsageUnavailable("unexpected response")


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_token_is_an_expired_login(tmp_path: Path, status: int) -> None:
    fetch = api(logged_in(tmp_path), responding(httpx.Response(status)))
    assert await fetch() == UsageUnavailable("login expired, run claude to refresh")


@pytest.mark.parametrize(
    ("retry_after", "seconds"),
    [
        ("120", 120.0),
        ("2.5", 2.5),
        ("0", 0.0),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("inf", None),
        ("nan", None),
        ("-5", None),
    ],
)
async def test_a_rate_limit_waits_as_long_as_it_asks(
    tmp_path: Path, retry_after: str, seconds: float | None
) -> None:
    response = httpx.Response(429, headers={"Retry-After": retry_after})
    fetch = api(logged_in(tmp_path), responding(response))
    assert await fetch() == UsageRateLimited(seconds)


async def test_a_rate_limit_without_retry_after_has_no_wait(tmp_path: Path) -> None:
    fetch = api(logged_in(tmp_path), responding(httpx.Response(429)))
    assert await fetch() == UsageRateLimited(None)


@pytest.mark.parametrize("status", [302, 500, 503])
async def test_any_other_status_is_named(tmp_path: Path, status: int) -> None:
    fetch = api(logged_in(tmp_path), responding(httpx.Response(status)))
    assert await fetch() == UsageUnavailable(f"HTTP {status}")


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("timed out")])
async def test_an_unreachable_api_is_unavailable(tmp_path: Path, error: httpx.HTTPError) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    fetch = api(logged_in(tmp_path), handler)
    assert await fetch() == UsageUnavailable("couldn't reach api.anthropic.com")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text=TOKEN),
        httpx.Response(200, json={"five_hour": TOKEN}),
        httpx.Response(401, text=TOKEN),
        httpx.Response(500, text=TOKEN),
    ],
)
async def test_a_reason_never_contains_the_token(tmp_path: Path, response: httpx.Response) -> None:
    outcome = await api(logged_in(tmp_path), responding(response))()
    assert isinstance(outcome, UsageUnavailable)
    assert TOKEN not in outcome.reason
