"""The Claude subscription's usage limits, fetched from Anthropic's OAuth usage endpoint."""

import json
import math
from pathlib import Path

import httpx

from orbit.agent.domain.usage import (
    UsageFetch,
    UsageRateLimited,
    UsageReport,
    UsageUnavailable,
    parse_usage,
)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
TIMEOUT_SECONDS = 10

_CREDENTIALS = Path("~/.claude/.credentials.json")


class UsageApi:
    """Fetches the usage limits with the token Claude Code keeps in ``credentials``, through
    ``transport`` if given."""

    def __init__(
        self,
        credentials: Path = _CREDENTIALS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.credentials = credentials.expanduser()
        self._transport = transport

    async def __call__(self) -> UsageFetch:
        """One fetch's outcome; it never raises for a failed fetch."""
        # Claude Code refreshes the token itself, so it's read afresh every time.
        token = _read_token(self.credentials)
        if token is None:
            return UsageUnavailable("no Claude login")
        try:
            response = await self._get(token)
        except httpx.HTTPError:
            return UsageUnavailable("couldn't reach api.anthropic.com")
        return _outcome(response)

    async def _get(self, token: str) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": BETA_HEADER,
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT_SECONDS) as client:
            return await client.get(USAGE_URL, headers=headers)


def _read_token(path: Path) -> str | None:
    """The OAuth access token in the credentials file at ``path``, or ``None`` without one."""
    try:
        content = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    oauth = content.get("claudeAiOauth") if isinstance(content, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    return token if isinstance(token, str) and token else None


def _outcome(response: httpx.Response) -> UsageFetch:
    """What the usage endpoint's ``response`` says."""
    status = response.status_code
    if status == 200:
        return _report(response)
    if status in (401, 403):
        return UsageUnavailable("login expired, run claude to refresh")
    if status == 429:
        return UsageRateLimited(_retry_after(response.headers.get("Retry-After")))
    return UsageUnavailable(f"HTTP {status}")


def _report(response: httpx.Response) -> UsageFetch:
    try:
        return UsageReport(parse_usage(response.json()))
    except ValueError:
        return UsageUnavailable("unexpected response")


def _retry_after(value: str | None) -> float | None:
    """The seconds a ``Retry-After`` header ``value`` asks for, or ``None`` if it isn't a
    finite, non-negative number (an HTTP date counts as not a number)."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None
