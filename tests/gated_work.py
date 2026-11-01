"""Work held mid-flight by a gate, noting its runs, for the tests of running work in the
background."""

import asyncio


class GatedWork:
    """Work that records its runs and, once started, waits for ``gate``."""

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    async def __call__(self, name: str) -> None:
        self.runs.append(name)
        self.started.set()
        await self.gate.wait()
