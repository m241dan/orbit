"""The file of headers sent to a collector, which may hold secrets, so others mustn't read it."""

from pathlib import Path

from orbit.protocol.transport import too_open


class HeadersFileError(Exception):
    """The headers file can't be used; the message names it and says why."""


def parse_headers(text: str) -> dict[str, str]:
    """The headers in ``text``, one ``Name: value`` per line, ignoring blank lines and lines
    starting with ``#``.

    Raises :class:`ValueError` naming the first line that isn't a header.
    """
    headers: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, colon, value = stripped.partition(":")
        if not colon or not name.strip():
            raise ValueError(f"line {number} isn't a header, Name: value")
        headers[name.strip()] = value.strip()
    return headers


def read_headers(path: Path) -> dict[str, str]:
    """The headers in the file at ``path``.

    Raises :class:`HeadersFileError` if others can read it, or it can't be read or parsed.
    """
    try:
        if too_open(path.stat().st_mode):
            raise HeadersFileError(
                f"The telemetry headers file {path} can be read by others; run chmod 600 {path}."
            )
        return parse_headers(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise HeadersFileError(f"Can't read the telemetry headers file {path}: {exc}") from None
