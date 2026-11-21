"""Handled for you (E123): what Chief of Staff settled on the owner's behalf instead of asking.

A report with no code or PR accepted once read (or a day on), a changed decision every affected
worker said changes nothing for, a lesson the owner's own words back often, the one project a
task fits, a server kept off for the worker whose folder changed it, git files moved out of Chief
of Staff's own worktree folders. Each is listed for a week, in one quiet line under Needs you,
with Undo where undoing it still means something; Undo looks again at how things stand now and
refuses what moved on since. Nothing that speaks as the owner, merges, spends or signs in is ever
handled here, but for one thing the owner asked for: a reply in the thread of a Slack message they
reacted to with their delegate emoji goes out at once, and is listed here with its link.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

from .store import hours_ago, now

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .runtime import Runtime

SHOWN_DAYS = 7
SHOWN_MAX = 50


class Handled:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.bus = runtime.bus

    def record(
        self,
        kind: str,
        text: str,
        *,
        session_id: str = "",
        ref: str = "",
        undo: str = "",
        data: dict | None = None,
    ) -> dict:
        """One thing handled: `kind` says how to undo it, `undo` the button's word ("" for
        none), `ref` what it is about (a task, a lesson, a card), `data` what undoing needs."""
        entry_id = "h" + uuid4().hex[:8]
        self.store.execute(
            """INSERT INTO handled(id, kind, text, session_id, ref, undo, data, created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (entry_id, kind, " ".join(text.split())[:300], session_id, ref, undo,
             json.dumps(data or {}), now()),
        )  # fmt: skip
        self.bus.publish("state")
        return self.get(entry_id)

    def get(self, entry_id: str) -> dict:
        row = self.store.one("SELECT * FROM handled WHERE id=?", (entry_id,))
        if not row:
            raise LookupError("Nothing like that was handled for you.")
        row["data"] = json.loads(row["data"] or "{}")
        return row

    def latest(self, kind: str, ref: str) -> dict | None:
        """The newest entry of a kind about one thing that can still be undone."""
        row = self.store.one(
            "SELECT id FROM handled WHERE kind=? AND ref=? AND status='done' "
            "ORDER BY created_at DESC LIMIT 1",
            (kind, ref),
        )
        return self.get(row["id"]) if row else None

    def close(self, entry_id: str, status: str = "undone"):
        """Undone, or past undoing (what it was about moved on): no Undo any more."""
        self.store.execute(
            "UPDATE handled SET status=?, undone_at=? WHERE id=? AND status='done'",
            (status, now(), entry_id),
        )
        self.bus.publish("state")

    def view(self) -> list[dict]:
        """The last week's, the newest first, as Home lists them; `at`: when what an entry is
        about was written ([first, last]), where that is not when it was handled (E125)."""
        rows = self.store.all(
            """SELECT id, kind, text, session_id, ref, undo, status, created_at, undone_at,
            json_extract(data, '$.at') AS at, json_extract(data, '$.link') AS link
            FROM handled WHERE created_at>=? ORDER BY created_at DESC LIMIT ?""",
            (hours_ago(SHOWN_DAYS * 24), SHOWN_MAX),
        )
        for row in rows:
            row["at"] = json.loads(row["at"]) if row["at"] else None
            if not row["link"]:
                del row["link"]  # Only a reply sent for the owner has one (in Slack).
        return rows

    async def undo(self, entry_id: str) -> str:
        """The owner's Undo: what was done is reversed if nothing moved on since; what it says."""
        entry = self.get(entry_id)
        if entry["status"] != "done" or not entry["undo"]:
            raise ValueError("That can't be undone any more.")
        if entry["created_at"] < hours_ago(SHOWN_DAYS * 24):
            raise ValueError("That was over a week ago: it can't be undone from here.")
        undoers: dict[str, Callable[[dict], Awaitable[str]]] = {
            "accepted": self.runtime.task_actions.unaccept,
            "lesson": self.runtime.learning.unkeep,
            "workspace": self.runtime.drafts.change_workspace,
            "servers": self.runtime.servers_back,
            "git": self.runtime.git_put_back,
        }
        if entry["kind"] not in undoers:
            raise ValueError("That can't be undone.")
        try:
            said = await undoers[entry["kind"]](entry)
        except ValueError:
            self.close(entry_id, "moved")
            raise
        self.close(entry_id)
        return said
