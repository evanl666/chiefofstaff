"""A new task for a busy worker goes to a second worker that starts at once with its context
(forks.py, docs/design/worker-fork.md); a free worker keeps it, as before."""

import asyncio

import pytest

from chiefofstaff.backend import fork_point
from chiefofstaff.scripted import Say, ScriptedBackend, Wait


@pytest.mark.parametrize(
    ("kinds", "cut"),
    [
        # Idle: nothing in progress, all of it is copied.
        (["user.message", "assistant.turn_end", "session.usage_checkpoint"], ""),
        # In a turn, with a message steered into it: cut where that turn began.
        (["user.message", "session.usage_checkpoint", "user.message", "assistant.turn_start",
          "user.message", "tool.execution_start"], "e2"),
        # Its first turn, still going.
        (["user.message", "assistant.turn_start"], "e0"),
        # A log that never shows a turn's end: its latest message.
        (["user.message", "assistant.turn_end", "user.message"], "e2"),
    ],
)  # fmt: skip
def test_a_fork_stops_before_the_turn_in_progress(kinds, cut):
    events = [{"type": kind, "id": f"e{n}", "data": {}} for n, kind in enumerate(kinds)]
    events.append({"type": "user.message", "id": "sub", "data": {"parentToolCallId": "c1"}})
    assert fork_point(events) == cut


async def test_the_scripted_backends_fork_has_what_came_before_the_turn_in_progress():
    async def brain(session, prompt):
        if "slow" in prompt:
            yield Wait(5)
        yield Say("ok")

    backend = ScriptedBackend(brain)
    session = await backend.create_session(session_id="s1", name="n")
    await session.send("remember BLUEBIRD")
    await session.turn
    await session.send("slow work")
    await asyncio.sleep(0.05)
    forked = await backend.fork_session("s1", "fork")
    assert backend.histories[forked] == [("user", "remember BLUEBIRD"), ("assistant", "ok")]
    await session.disconnect()
