"""Moving a requirements record a task names to the next free number, when another record took
its number since the task was planned, such as by a plan made in another session."""

import asyncio
from pathlib import PurePath

from orbit.agent.application.ports import FileNames, Publish
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.records import moved_note, moved_records, record_directories
from orbit.protocol import Notice


async def move_records(
    tasks: Tasks, task_id: str, root: str, names: FileNames, publish: Publish
) -> None:
    """Move each record task ``task_id`` names that isn't written at ``root`` yet but whose
    number another record there has, listing the files with ``names``: it's renamed in every
    task that hasn't completed, and the user told with ``publish``."""
    prompt = tasks.board.get(task_id).task.prompt
    listings = {
        # A large directory, or a slow filesystem, would stall the event loop.
        directory: await asyncio.to_thread(names, str(PurePath(root, directory)))
        for directory in record_directories(prompt)
    }
    moves = moved_records(prompt, listings)
    if not moves:
        return
    await tasks.change(lambda board: board.move_records(moves))
    for old, new in moves.items():
        await publish(Notice(message=moved_note(old, new)))
