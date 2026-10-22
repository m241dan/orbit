"""The rules for the optional streams a connection watches."""

from collections.abc import Sequence

from orbit.agent.domain.refusals import unknown
from orbit.protocol import STREAMS


def check_streams(streams: Sequence[str]) -> None:
    """Refuse ``streams`` as ``unknown``, naming the streams there are, unless each is one."""
    for stream in streams:
        if stream not in STREAMS:
            listing = "the stream is" if len(STREAMS) == 1 else "the streams are"
            raise unknown(f"No stream {stream!r}", listing, STREAMS)
