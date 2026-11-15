"""The owner's own agent CLI resuming a worker's conversation by hand (in a terminal), as its
command line shows it: what Chief of Staff does of it (Workers.refresh_elsewhere)."""

import asyncio
import time

from chiefofstaff import backend as backend_module


async def opened_by_hand(tmp_path, program: str, *arguments: str):
    """The owner's own CLI, resuming a conversation (as its command line shows it)."""
    command = tmp_path / "by-hand" / program
    command.parent.mkdir(exist_ok=True)
    command.write_text("#!/bin/sh\nsleep 60\nexit 0\n")
    command.chmod(0o755)
    return await asyncio.create_subprocess_exec(str(command), *arguments)


def no_longer_open(harness) -> int:
    return sum(
        "is no longer open in a terminal" in text for _, text in harness.kinds(harness.router_id)
    )


async def resumed_by_hand_is_let_go_and_said_closed_once(harness, runtime, worker, terminal):
    """While the owner's terminal has it: let go of, and nothing said of it closing (however
    often Chief of Staff looks); once closed there, said once."""
    deadline = time.monotonic() + 10
    while terminal.pid not in await asyncio.to_thread(backend_module.process_table):
        assert time.monotonic() < deadline, "its process never showed"
        await asyncio.sleep(0.05)
    harness.store.set(f"refusedopen:{worker}", "1")  # A message was refused meanwhile.
    for _ in range(3):
        await runtime.workers.refresh_elsewhere()
    assert runtime.is_elsewhere(harness.store.session(worker))
    assert not runtime.states.is_live(worker), "let go of: never two writing to it"
    assert no_longer_open(harness) == 0
    terminal.kill()
    await terminal.wait()
    for _ in range(3):
        await runtime.workers.refresh_elsewhere()
    assert not runtime.is_elsewhere(harness.store.session(worker))
    assert no_longer_open(harness) == 1
