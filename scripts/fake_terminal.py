"""Stands in for a Copilot session the owner has open in a terminal: a separate Copilot CLI
process holds it open until this process is killed.

    python fake_terminal.py <working directory> <first message>
"""

import asyncio
import subprocess
import sys
import uuid

from copilot import CopilotClient, RuntimeConnection


async def main(cwd: str, text: str):
    path = subprocess.run(["which", "copilot"], capture_output=True, text=True).stdout.strip()
    client = CopilotClient(connection=RuntimeConnection.for_stdio(path=path))
    await client.start()
    session = await client.create_session(
        session_id=str(uuid.uuid4()),
        working_directory=cwd,
        on_permission_request=lambda *args, **kwargs: {"kind": "approved"},
    )
    done = asyncio.Event()
    session.on(lambda event: done.set() if event.type.value == "session.idle" else None)
    await session.send(text)
    await asyncio.wait_for(done.wait(), 180)
    print(f"READY {session.session_id}", flush=True)
    await asyncio.sleep(3600)  # Open, like a terminal the owner left running.


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
