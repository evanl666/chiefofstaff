"""Event sources. Each one watches an external system and hands new events to its host.

New events go to the Router through on_trigger (kind "delegated" when the owner handed them over
explicitly, by a Slack reaction for example); activity in a thread that sessions follow goes
straight to those sessions through on_thread. Stable keys make sure the same external event is
delivered once. Teams can be added the same way.
"""

from typing import Protocol


class SourceHost(Protocol):
    async def on_trigger(
        self,
        source: str,
        keys: list[str],
        text: str,
        display: str | None = None,
        kind: str = "event",
    ) -> bool: ...

    async def on_thread(
        self, source: str, thread: str, keys: list[str], text: str, display: str | None = None
    ) -> bool: ...

    def followed(self, prefix: str) -> list[dict]: ...

    def unfollow(self, thread: str, finished: bool = False, merged: bool = False) -> None: ...

    def missing(self, thread: str, why: str) -> None: ...


class Source(Protocol):
    name: str
    status: dict

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def wake(self) -> None: ...
