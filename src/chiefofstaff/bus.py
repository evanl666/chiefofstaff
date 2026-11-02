import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class Bus:
    """Fan-out of UI events. publish() is safe to call from SDK callback threads."""

    def __init__(self):
        self.queues: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.closed = False

    def bind(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    def publish(self, kind: str, data: Any = None):
        payload = json.dumps({"type": kind, "data": data}, ensure_ascii=False, default=str)
        if not self.loop:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self._fan_out(payload)
        elif not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._fan_out, payload)

    def _fan_out(self, payload: str):
        for queue in list(self.queues):
            if queue.qsize() > 1000:
                # A stalled tab must not grow memory without bound. Ending its stream makes the
                # browser reconnect and resync.
                self.queues.discard(queue)
                queue.put_nowait(None)
                continue
            queue.put_nowait(payload)

    def close(self):
        """End every event stream (None ends one), so shutting down does not wait for tabs."""
        self.closed = True
        for queue in list(self.queues):
            queue.put_nowait(None)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue()
        if self.closed:
            queue.put_nowait(None)
        self.queues.add(queue)
        try:
            yield queue
        finally:
            self.queues.discard(queue)
