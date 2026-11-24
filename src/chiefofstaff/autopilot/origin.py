"""Autopilot's origin: which turns it continued, and everything they lead to (autopilot.md §4.1).

A turn is Autopilot's when one of its deliveries starts or steers it (an answer, a nudge, a task
it released), or when it takes in a message that carries Autopilot's origin. The origin is the
list of record entries that led there; a turn can carry several.

- **A worker keeps it** (`autopilot:origin:<session>`) across every continuation of its own:
  check-backs, queued messages, restarts. Only the owner's handoff clears it: the owner writing
  to that worker in the page.
- **The Router and the Slack session carry it per turn** (`autopilot:turn:<session>`): a turn
  that takes in any message carrying it (a report, a finished-work notice, batched inputs
  too) carries it until it ends; one whose inputs are all the owner's carries none.
- **It travels with what those turns cause**: each message stored for a session while code acts
  for one (a tool it called, an event of its turn) is tagged with the actor's origin
  (`meta.autopilot_origin`), so a task the Router spawns, a message it sends, a worker's note
  to another and its finished-work notices all carry it.
- **What it changes**: Chief's handlers refuse to publish from a turn that carries it (a delegate
  reply waits as a card; `tell_session` is held, never steered); with *workers ask first* on,
  such a worker's turns get the Ask-first checks whatever the owner's yolo setting; the record
  follows the chain (`after` entries).

Inert until Autopilot first acts: every hook looks at two dicts in memory, empty until then."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from ..store import Store, now

STICKY = "autopilot:origin:"
TURN = "autopilot:turn:"
# The origin the code running now acts with (a tool call's, an event's): None for none.
CARRIED: ContextVar[dict | None] = ContextVar("autopilot_origin", default=None)
# A message's tag: the entries that led to it, and whether those turns ask first.
TAG = "autopilot_origin"


def merged(first: dict | None, second: dict | None) -> dict | None:
    """Two origins as one: every entry of both; asking first if either does."""
    if not first or not second:
        return first or second
    entries = list(dict.fromkeys([*first["entries"], *second["entries"]]))
    asks = bool(first.get("ask_first") or second.get("ask_first"))
    return {"entries": entries, "ask_first": asks}


class Origins:
    """Who carries Autopilot's origin now, kept in the kv store and mirrored in memory."""

    def __init__(self, store: Store):
        self.store = store
        self.sticky: dict[str, dict] = self.loaded(STICKY)
        self.turns: dict[str, dict] = self.loaded(TURN)

    def loaded(self, prefix: str) -> dict[str, dict]:
        found = {}
        for key, value in self.store.items(prefix).items():
            try:
                origin = json.loads(value)
            except ValueError:
                continue
            if isinstance(origin, dict) and origin.get("entries"):
                found[key] = origin
        return found

    def any(self) -> bool:
        return bool(self.sticky or self.turns)

    def of(self, session_id: str) -> dict | None:
        """The origin a session's turn carries now (a worker's, kept; the Router's, this
        turn's)."""
        if not (self.sticky or self.turns):
            return None
        return self.sticky.get(session_id) or self.turns.get(session_id)

    def asks_first(self, session_id: str) -> bool:
        """Whether a worker's turns get the Ask-first checks because Autopilot continued it."""
        return bool(self.sticky) and bool((self.sticky.get(session_id) or {}).get("ask_first"))

    @contextmanager
    def acting(self, session_id: str) -> Iterator[None]:
        """Code that acts for this session (a tool it called, an event of its turn): what it
        stores for another session carries the session's origin."""
        origin = self.of(session_id) if (self.sticky or self.turns) else None
        if origin is None:
            yield
            return
        token = CARRIED.set(merged(CARRIED.get(), origin))
        try:
            yield
        finally:
            CARRIED.reset(token)

    @contextmanager
    def carrying(self, origin: dict) -> Iterator[None]:
        """Autopilot's own deliveries: what they store carries this origin."""
        token = CARRIED.set(merged(CARRIED.get(), origin))
        try:
            yield
        finally:
            CARRIED.reset(token)

    def tag(self, row: dict, meta: dict, *, keep: bool = True) -> dict:
        """A message about to be stored for a session (`row`) to deliver: tagged with the origin
        the code storing it acts with. A worker keeps it from now on (`keep`), or once a turn
        takes the message in (`taken`)."""
        origin = CARRIED.get()
        if origin is None:
            return meta
        tagged = merged(meta.get(TAG), origin) or origin
        if row["role"] == "worker" and keep:
            self.keep(row["id"], tagged)
        return {**meta, TAG: {"entries": tagged["entries"], "ask_first": tagged["ask_first"]}}

    def keep(self, session_id: str, origin: dict):
        """A worker carries this origin from now on (with what it carried before)."""
        before = self.sticky.get(session_id) or {}
        kept = merged(before or None, origin) or origin
        if (
            before
            and kept["entries"] == before["entries"]
            and kept["ask_first"] == bool(before.get("ask_first"))
        ):
            return
        kept = {**kept, "since": before.get("since") or now(), "seen": before.get("seen") or now()}
        self.sticky[session_id] = kept
        self.store.set(f"{STICKY}{session_id}", json.dumps(kept))

    def seen(self, session_id: str, moment: str):
        """A worker's turn in the chain ended at `moment`: the next one's receipts start there."""
        if kept := self.sticky.get(session_id):
            kept["seen"] = moment
            self.store.set(f"{STICKY}{session_id}", json.dumps(kept))

    def taken(self, row: dict, messages: Iterable[dict]):
        """A turn takes these messages in (sent to it now): the Router's or the Slack session's
        turn carries the origin of any of them; a worker keeps it."""
        origin = None
        for message in messages:
            origin = merged(origin, (message.get("meta") or {}).get(TAG))
        if origin is None:
            return
        if row["role"] == "worker":
            self.keep(row["id"], origin)
            return
        before = self.turns.get(row["id"]) or {}
        turn = merged(before or None, origin) or origin
        if not before or turn["entries"] != before["entries"]:
            self.turns[row["id"]] = {**turn, "since": before.get("since") or now()}
            self.store.set(f"{TURN}{row['id']}", json.dumps(self.turns[row["id"]]))

    def turn_ended(self, session_id: str) -> dict | None:
        """The Router's or the Slack session's turn ended: its next carries only what it takes
        in. Returns what this one carried."""
        found = self.turns.pop(session_id, None)
        if found is not None:
            self.store.delete(f"{TURN}{session_id}")
        return found

    def handed_back(self, session_id: str) -> dict | None:
        """The owner wrote to this worker in the page: it is theirs again. Returns what it
        carried (None when nothing)."""
        found = self.sticky.pop(session_id, None)
        if found is not None:
            self.store.delete(f"{STICKY}{session_id}")
        return found

    def forget(self, session_id: str):
        """A closed session's."""
        self.handed_back(session_id)
        self.turn_ended(session_id)

    def entries(self) -> dict[str, Any]:
        """Every session carrying it, with its origin (for the record and the downgrade)."""
        return {**self.turns, **self.sticky}
