"""Worker scheduling: at most max_workers run a turn at once; the rest wait in a queue,
as do tasks that wait for other tasks. Starting, closing, reopening and interrupting workers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Collection
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import catalog
from .envelope import clean_title, owner_text, short, wrap
from .events import PLUMBING_TOOLS, describe_tool
from .roles import ROUTER, WORKER
from .sources.slack import describe
from .store import now
from .tasks import OPEN
from .watchdog import timestamp

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

# An adopted session shows at most this much of its earlier conversation.
HISTORY_SHOWN = 60
# How often sessions open in a terminal are mirrored, and looked for.
MIRROR_SECONDS = 3
ELSEWHERE_EVERY = 3  # mirror rounds
# A log that changed this recently means the session is busy (its sub-agents, say).
BUSY_SECONDS = 20
TURN_ENDS = {"assistant.turn_end", "session.idle", "session.error", "abort"}


def own_event(event: dict) -> bool:
    """An event of the session itself, not of one of its sub-agents."""
    return not event.get("agentId") and not (event.get("data") or {}).get("parentToolCallId")


# (kind, text, when): kind is owner, assistant (an answer), narration (said on the way to more
# steps) or tool (a step); when is the event's ISO time.
Entry = tuple[str, str, str]


def terminal_timeline(events: list[dict], ours: Collection[str] = ()) -> list[Entry]:
    """What a session said and did, from its log: its messages and steps, not its sub-agents'.

    `ours`: Chief of Staff's own tools. Only its sessions have them, so calls to them are from
    turns it ran (and shows as reports and cards), never from a terminal.
    """
    shown = []
    for event in filter(own_event, events):
        data = event.get("data") or {}
        content = str(data.get("content") or "").strip()
        at = str(event.get("timestamp") or "")
        if event.get("type") == "user.message" and (text := owner_text(content)):
            shown.append(("owner", text, at))
        elif event.get("type") == "assistant.message" and content:
            shown.append(("narration" if data.get("toolRequests") else "assistant", content, at))
        elif event.get("type") == "tool.execution_start":
            name = data.get("toolName") or "tool"
            if name not in PLUMBING_TOOLS and name not in ours:
                shown.append(("tool", describe_tool(name, data.get("arguments")), at))
    return shown


def moment(iso: str) -> float | None:
    try:
        return timestamp(iso)
    except ValueError:
        return None


def turn_open(events: list[dict], before: bool) -> bool:
    """Whether the session's own turn is still going after these events."""
    for event in filter(own_event, events):
        if event.get("type") == "assistant.turn_start":
            before = True
        elif event.get("type") in TURN_ENDS:
            before = False
    return before


class Workers:
    """Queues, starts, stops and finds workers."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.bus = runtime.bus
        self.states = runtime.states
        self.tasks = runtime.tasks
        self.delivery = runtime.delivery
        self.workspaces = runtime.workspaces
        # Taken to change which workers hold a slot (see state.py for the lock order).
        self.pump_lock = asyncio.Lock()
        self.external_cache: tuple[float, list[dict]] = (0.0, [])
        # Reading logs of sessions open in a terminal; held while their timelines catch up.
        self.mirror_lock = asyncio.Lock()
        # Other sessions' logs as last read: session id -> (offset, entries, turn open).
        self.histories: dict[str, tuple[int, list[Entry], bool]] = {}
        # How far catch_up has read each worker's log.
        self.read_to: dict[str, int] = {}

    def running(self) -> int:
        return sum(1 for row in self.store.open_sessions(WORKER) if self.states[row["id"]].busy)

    async def claim_slot(self, session_id: str) -> bool:
        """Reserve a worker slot for a new turn, or leave the message pending (queued)."""
        async with self.pump_lock:
            row = self.store.session(session_id)
            if row["status"] in {"queued", "starting"}:
                return False
            if self.states[session_id].busy:
                return True
            if self.running() >= self.store.settings().max_workers:
                self.runtime.update_session(session_id, status="queued", activity="")
                return False
            self.states[session_id].busy = True
            return True

    async def pump(self):
        """Start queued workers while fewer than max_workers are running a turn."""
        if not self.runtime.ready():
            return
        starting = []
        async with self.pump_lock:
            limit = self.store.settings().max_workers
            for row in self.store.open_sessions(WORKER):
                if row["status"] != "queued":
                    continue
                if not self.store.get(f"created:{row['id']}") and self.waiting_for(row):
                    continue
                if self.running() < limit:
                    self.states[row["id"]].busy = True
                    starting.append(self.runtime.update_session(row["id"], status="starting"))
        for row in starting:
            await self.start(row)

    def waiting_for(self, row: dict) -> list[dict]:
        """Tasks a worker's queued task still waits for; shown as the worker's activity."""
        task = self.tasks.current(row["id"])
        if not task or task["status"] != "queued" or self.store.get(f"released:{task['id']}"):
            return []
        waiting = self.tasks.unmet(task)
        names = ", ".join(
            f"your review of “{d['title']}”" if d["status"] == "review" else f"“{d['title']}”"
            for d in waiting
        )
        activity = f"waiting for {names}" if waiting else ""
        if row["activity"] != activity:
            self.runtime.update_session(row["id"], activity=activity)
        broken = self.tasks.broken(task)
        if broken and not self.store.get(f"depblocked:{task['id']}"):
            self.store.set(f"depblocked:{task['id']}", "1")
            names = ", ".join(f"“{d['title']}” ({d['id']}, {d['status']})" for d in broken)
            self.runtime.background(
                self.delivery.deliver(
                    self.runtime.router_id(),
                    wrap(
                        "system",
                        "blocked",
                        f"Task {task['id']} waits for {names}, which ended without finishing. "
                        "Message its worker to start anyway, or cancel_task it.",
                        worker=short(row["id"]),
                        task=task["id"],
                        title=task["title"],
                    ),
                    kind="inbound",
                    display=f"“{row['title']}” waits for {names}, which ended without finishing.",
                    meta={
                        "from": row["id"],
                        "title": task["title"],
                        "status": "blocked",
                        "task_id": task["id"],
                    },
                )
            )
        return waiting

    async def start(self, row: dict):
        session_id = row["id"]
        task = self.tasks.current(session_id)
        done = self.tasks.deps(task["id"]) if task else []
        if task and done and not self.store.get(f"handed:{task['id']}"):
            results = "\n".join(
                f"- Task {d['id']} “{d['title']}” ({d['status']}): {d['result'] or 'no report'}"
                for d in done
            )
            self.runtime.add_message(
                session_id,
                "router",
                f"Results of the tasks this one waited for:\n{results}",
                {
                    "prompt": wrap(
                        "router", "results", f"Results of the tasks this one waited for:\n{results}"
                    )
                },
            )
            self.store.set(f"handed:{task['id']}", "1")
        try:
            if self.store.get(f"created:{session_id}"):
                session = await self.runtime.session_for(session_id)
            else:
                async with self.states[session_id].lifecycle:
                    session = self.states.handle(session_id) or await self.runtime.open_live(
                        row, create=True
                    )
            self.runtime.update_session(session_id, status="working")
            if task and task["status"] == "queued":
                self.tasks.move(task["id"], "running")
            # Flushing now includes messages that arrived while the session was being created.
            if not await self.delivery.flush(session_id, session):
                self.states[session_id].busy = False
                self.runtime.update_session(session_id, status="idle")
        except Exception as exc:
            self.runtime.fail(row, f"Could not start: {describe(exc)}")

    async def close(self, session_id: str):
        row = self.store.session(session_id)
        if row["role"] == ROUTER:
            raise ValueError("The Router cannot be closed.")
        if row["closed_at"]:
            return
        async with self.states[session_id].lifecycle:
            if session := self.states.take_handle(session_id):
                if row["status"] == "working":
                    with suppress(Exception):
                        await session.abort()
                with suppress(Exception):
                    await session.disconnect()
        self.states[session_id].busy = False
        self.store.unfollow_session(session_id)
        self.runtime.decisions.drop_asker(session_id)
        self.states[session_id].last_output = ""
        self.states[session_id].close_after_turn = False
        self.runtime.asks.cancel_requests(session_id, "The session was closed.")
        await self.runtime.asks.cancel_questions(session_id)
        for task in self.tasks.for_session(session_id):
            if task["status"] in OPEN:
                self.tasks.move(task["id"], "cancelled", progress="Its worker was closed.")
        # Not "stopped without reporting": the Watchdog leaves a reopened worker alone until told.
        self.runtime.add_message(session_id, "notice", "Closed.", {"stopped": True})
        self.runtime.update_session(session_id, closed_at=now(), activity="")
        with suppress(RuntimeError):
            self.runtime.add_message(
                self.runtime.router_id(),
                "notice",
                f"Closed “{row['title']}”",
                {"session_id": session_id},
            )
        self.runtime.background(self.pump())
        if row["role"] == WORKER:
            self.runtime.background(self.release(session_id))

    async def release(self, session_id: str):
        """A closed worker's clean worktrees go; its branches stay (see Workspaces.release)."""
        async with self.states[session_id].checkouts:
            row = self.store.session(session_id)
            if row["closed_at"]:
                await self.workspaces.release(row)

    def recently_closed(self, limit: int) -> list[dict]:
        return self.store.all(
            """SELECT * FROM sessions WHERE role='worker' AND closed_at IS NOT NULL
            ORDER BY closed_at DESC LIMIT ?""",
            (limit,),
        )

    def card(self, row: dict) -> dict:
        """What the Router needs to recognize a worker's task."""
        return {
            "session_id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "closed": bool(row["closed_at"]),
            "task": row["task"][:300],
            "current_task": self.runtime.toolset.task_brief(self.tasks.current(row["id"])),
            "project": row["project"],
            "cwd": row["cwd"],
            "branch": row["branch"],
            "links": json.loads(row["links"]),
            "files": json.loads(row["files"])[-12:],
            "last_report": row["report"][:300],
            "updated_at": row["updated_at"],
        }

    def find(self, query: str, limit: int = 6) -> list[dict]:
        """Workers, open or closed, whose task matches the query, best first."""
        rows = {
            row["id"]: row
            for row in self.store.all(
                "SELECT * FROM sessions WHERE role='worker' ORDER BY updated_at DESC LIMIT 2000"
            )
        }
        named = []  # A session id, or its first 8+ characters, names the worker.
        for word in re.findall(r"[0-9a-f-]{8,}", query.lower()):
            prefixed = [row for key, row in rows.items() if key.startswith(word)]
            if len(prefixed) == 1 and prefixed[0] not in named:
                named.append(prefixed[0])
        said: dict[str, dict[str, list[str]]] = {
            key: {"reports": [], "messages": []} for key in rows
        }
        for message in self.store.all(
            """SELECT m.session_id, m.kind, m.text FROM messages m JOIN sessions s
            ON s.id=m.session_id WHERE s.role='worker' AND m.kind IN ('router','owner','report')
            ORDER BY m.id DESC"""
        ):
            if parts := said.get(message["session_id"]):
                parts["reports" if message["kind"] == "report" else "messages"].append(
                    message["text"][:2000]
                )
        tasks: dict[str, list[str]] = {}
        for task in self.store.all("SELECT session_id, title, goal FROM tasks ORDER BY created_at"):
            tasks.setdefault(task["session_id"], []).append(f"{task['title']}\n{task['goal']}")
        # Paths in texts share these prefixes; only what follows them tells tasks apart.
        roots = (self.workspaces.scratch_root(), self.runtime.work_root, Path.home())
        shared = re.compile("|".join(re.escape(str(path)) for path in roots))

        def local(text: str) -> str:
            return shared.sub(" ", text)

        docs = {
            key: {
                "title": row["title"],
                "task": local("\n".join(tasks.get(key, [row["task"]]))),
                # The directory's own name only: every worker shares the rest of the path.
                "paths": " ".join(
                    [
                        Path(row["cwd"]).name,
                        Path(row["project"]).name if row["project"] else "",
                        *(Path(c["path"]).name for c in json.loads(row["repos"] or "[]")),
                        row["branch"],
                        *json.loads(row["files"]),
                        *json.loads(row["links"]),
                    ]
                ),
                "reports": local("\n".join(said[key]["reports"][:20])),
                "messages": local("\n".join(said[key]["messages"][:20])),
            }
            for key, row in rows.items()
        }
        cards = [{**self.card(row), "matched": ["session id"]} for row in named]
        for key, matched in catalog.rank(query, docs):
            if rows[key] not in named:
                cards.append({**self.card(rows[key]), "matched": matched})
        return cards[:limit]

    async def reopen(self, session_id: str) -> dict:
        """Bring a closed worker back with its conversation, worktree and branch."""
        row = self.store.session(session_id)
        if row["role"] != WORKER:
            raise ValueError("Only workers can be reopened.")
        if not row["closed_at"]:
            return self.runtime.view(row)
        if not self.store.get(f"created:{session_id}"):
            raise ValueError("That worker never started; start a new one instead.")
        # Held until it is open again: a removal of its folders that was started when it closed
        # waits, then sees that it is open and leaves them.
        async with self.states[session_id].checkouts:
            with suppress(ValueError, OSError):
                await self.workspaces.restore(row)
            if row["cwd"] and not await asyncio.to_thread(os.path.isdir, row["cwd"]):
                raise ValueError("That worker's directory is gone; start a new one instead.")
            backend = self.runtime.require_ready()
            if not await backend.get_session_metadata(session_id):
                raise ValueError(
                    "Copilot no longer has that conversation; start a new one instead."
                )
            status = row["status"] if row["status"] in {"done", "failed", "waiting"} else "idle"
            row = self.runtime.update_session(
                session_id, closed_at=None, status=status, activity=""
            )
        with suppress(RuntimeError):
            self.runtime.add_message(
                self.runtime.router_id(),
                "notice",
                f"Reopened “{row['title']}”",
                {"session_id": session_id},
            )
        return self.runtime.view(row)

    async def interrupt(self, session_id: str, by: str = "you"):
        row = self.store.session(session_id)
        self.runtime.asks.cancel_requests(session_id, f"Stopped by {by}.")
        if session := self.states.handle(session_id):
            await session.abort()
        self.states[session_id].busy = False
        if row["status"] in {"working", "queued"}:
            self.runtime.update_session(session_id, status="idle", activity="")
            # A deliberate stop: the watchdog must not treat it as a silent stall.
            self.runtime.add_message(session_id, "notice", f"Stopped by {by}.", {"stopped": True})
            task = self.tasks.current(session_id)
            if task and task["status"] in {"queued", "running"}:
                self.tasks.move(task["id"], "waiting", progress=f"Stopped by {by}.")
        self.runtime.background(self.pump())

    async def external(self, refresh: bool = False) -> list[dict]:
        if not self.runtime.ready():
            return []
        cached_at, items = self.external_cache
        if not refresh and time.monotonic() - cached_at < 15:
            return items
        managed = {row["id"] for row in self.store.all("SELECT id FROM sessions")}
        managed |= {
            row["key"].removeprefix("alias:")
            for row in self.store.all("SELECT key FROM kv WHERE key LIKE 'alias:%'")
        }
        items = []
        backend = self.runtime.require_ready()
        listed = await backend.list_sessions()
        await self.refresh_elsewhere()
        elsewhere = self.runtime.elsewhere
        # Sessions open in a terminal right now first, then the most recent.
        for meta in sorted(
            listed, key=lambda m: (m.session_id in elsewhere, m.modified_time), reverse=True
        ):
            if meta.session_id in managed or meta.is_remote:
                continue
            context = meta.context
            items.append(
                {
                    "id": meta.session_id,
                    "summary": (meta.summary or "Untitled session")[:200],
                    "cwd": (context.working_directory if context else "") or "",
                    "repository": (context.repository if context else "") or "",
                    "branch": (context.branch if context else "") or "",
                    "modified": meta.modified_time.isoformat(),
                    "open": meta.session_id in elsewhere,
                }
            )
            if len(items) >= 40:
                break
        self.external_cache = (time.monotonic(), items)
        return items

    async def adopt(self, session_id: str) -> dict:
        backend = self.runtime.require_ready()
        if self.store.find_session(session_id):
            raise ValueError("Chief of Staff already manages that session.")
        meta = await backend.get_session_metadata(session_id)
        if not meta:
            raise LookupError("Session not found.")
        if pid := (await backend.open_elsewhere()).get(session_id):
            raise ValueError(
                f"That session is open in another Copilot window (process {pid}). Exit it there "
                "(/exit), then let the Router manage it."
            )
        cwd = meta.context.working_directory if meta.context else ""
        row = self.store.create_session(
            session_id,
            WORKER,
            clean_title(meta.summary or "Adopted session"),
            "idle",
            cwd=cwd or str(self.runtime.role_dir("scratch")),
        )
        try:
            await self.runtime.open_live(row)
        except Exception as exc:
            self.store.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self.bus.publish("state")
            raise RuntimeError(f"Could not adopt the session: {describe(exc)}") from exc
        self.store.set(f"created:{session_id}", "1")
        # Its work so far is not a task to review; the owner's next request starts one.
        task = self.tasks.create(session_id, row["title"], "(an existing Copilot session)")
        self.tasks.move(task["id"], "done", result="Adopted from Copilot.")
        self.external_cache = (0.0, [])
        self.runtime.add_message(
            self.runtime.router_id(),
            "notice",
            f"Adopted “{row['title']}”",
            {"session_id": session_id},
        )
        return self.store.session(session_id)

    async def import_history(self, row: dict, session: Any):
        """An adopted session's conversation so far, into its empty timeline.

        Imported messages carry no prompt: they are never sent again, and the Watchdog does not
        take them for instructions.
        """
        if self.store.one("SELECT id FROM messages WHERE session_id=? LIMIT 1", (row["id"],)):
            return
        try:
            events = await session.get_events()
        except Exception:
            logger.warning("Could not read the history of %s.", row["id"], exc_info=True)
            return
        said = []
        for event in events:
            kind = {"user.message": "owner", "assistant.message": "assistant"}.get(
                getattr(event.type, "value", str(event.type))
            )
            text = (getattr(event.data, "content", "") or "").strip()
            if kind == "owner":
                text = owner_text(text)
            if not kind or not text or getattr(event.data, "parent_tool_call_id", None):
                continue
            if kind == "assistant" and getattr(event.data, "tool_requests", None):
                kind = "narration"
            said.append((kind, text, ""))
        if not said:
            return
        shown = said[-HISTORY_SHOWN:]
        earlier = f"the last {len(shown)} of {len(said)} messages" if len(shown) < len(said) else ""
        self.runtime.add_message(
            row["id"],
            "notice",
            "Its conversation in Copilot before Chief of Staff managed it"
            + (f" ({earlier})" if earlier else ""),
            {"imported": True},
        )
        self.show(row, shown, {"imported": True})

    def show(self, row: dict, entries: list[Entry], meta: dict | None = None, note: str = ""):
        """Entries read from a session's history, into its timeline. They carry no prompt: they
        are never sent again, and the Watchdog does not take them for instructions."""
        meta = meta or {"terminal": True}
        if entries and note:
            self.runtime.add_message(row["id"], "notice", note, meta)
        for kind, text, _ in entries:
            extra = {"narration": True} if kind == "narration" else {}
            kind = "assistant" if kind == "narration" else kind
            self.runtime.add_message(row["id"], kind, text[:20000], {**meta, **extra})

    def missed(self, row: dict, entries: list[Entry]) -> list[Entry]:
        """The entries after the last thing its timeline shows it say or do: what it did
        elsewhere meanwhile (all of its latest, for a timeline without any)."""
        last = self.store.one(
            """SELECT created_at FROM messages WHERE session_id=?
            AND kind IN ('owner', 'assistant', 'tool') ORDER BY id DESC LIMIT 1""",
            (row["id"],),
        )
        if not last:
            return entries[-HISTORY_SHOWN:]
        since = timestamp(last["created_at"])
        recent = {
            message["text"]
            for message in self.store.all(
                "SELECT text FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 30",
                (row["id"],),
            )
        }
        fresh = [
            entry
            for entry in entries
            if (when := moment(entry[2])) is not None
            and when > since
            and (entry[0] == "tool" or entry[1][:20000] not in recent)  # Seen at the edge.
        ]
        return fresh[-HISTORY_SHOWN:]

    async def catch_up(self, row: dict):
        """Before a worker that is not attached gets a message: what it did in a terminal since
        its timeline last showed it, from the log Copilot writes, so it comes first."""
        backend = self.runtime.backend
        copilot = self.runtime.copilot_id(row["id"])
        if not backend or row["role"] != WORKER or copilot in self.runtime.elsewhere:
            return  # Open in a terminal: the mirror shows it.
        async with self.mirror_lock:
            key = f"mirror:{row['id']}"
            mirrored = self.store.get(key)
            if mirrored is None and not self.store.one(
                "SELECT id FROM messages WHERE session_id=? LIMIT 1", (row["id"],)
            ):
                return  # Adopted just now: import_history brings its conversation.
            start = int(mirrored) if mirrored is not None else self.read_to.get(row["id"], 0)
            try:
                events, after, _ = await backend.read_history(copilot, start)
            except Exception:
                logger.warning("Could not read the log of %s.", row["id"], exc_info=True)
                return
            entries = terminal_timeline(events, self.runtime.tools)
            if mirrored is not None:
                self.show(row, entries)  # The rest of a terminal visit the mirror followed.
            else:
                # Its own turns through Chief of Staff are in the log too: only what is newer.
                self.show(row, self.missed(row, entries), note="Meanwhile, in a terminal")
            self.store.delete(key)
            self.read_to[row["id"]] = after

    async def mirror_loop(self):
        rounds = 0
        while True:
            await asyncio.sleep(MIRROR_SECONDS)
            try:
                if rounds % ELSEWHERE_EVERY == 0:
                    await self.refresh_elsewhere()
                await self.mirror()
            except Exception:
                logger.exception("Mirroring sessions open in a terminal failed")
            rounds += 1

    async def mirror(self):
        """Workers open in a terminal: what they say and do there, read from their logs.

        Chief of Staff never attaches to such a session (two programs would interleave its
        history); it reads the log the CLI writes and shows it, read-only, in the timeline.
        """
        if not self.runtime.backend:
            return
        async with self.mirror_lock:
            for row in self.store.open_sessions(WORKER):
                await self.mirror_one(row)

    async def mirror_one(self, row: dict):
        backend = self.runtime.connected()
        copilot = self.runtime.copilot_id(row["id"])
        key = f"mirror:{row['id']}"
        offset = self.store.get(key)
        if copilot not in self.runtime.elsewhere:
            if offset is not None and not self.states.handle(row["id"]):
                # Exited there: the rest of what it did; its next turns come through us.
                events, _, _ = await backend.read_history(copilot, int(offset))
                self.show(row, terminal_timeline(events, self.runtime.tools))
            self.store.delete(key)
            if self.runtime.terminal.pop(row["id"], None) is not None:
                self.runtime.publish_session(row)
            return
        events, after, changed = await backend.read_history(copilot, int(offset or 0))
        entries = terminal_timeline(events, self.runtime.tools)
        if offset is None:
            # What it did there before we looked: since its timeline's last entry.
            self.show(
                row,
                self.missed(row, entries),
                note="Its conversation in a terminal, shown read-only while it is open there",
            )
        else:
            self.show(row, entries)
        self.store.set(key, str(after))
        before = self.runtime.terminal.get(row["id"]) or {}
        turn = turn_open(events, before.get("turn", False))
        steps = [text for kind, text, _ in entries if kind == "tool"]
        now = {
            "turn": turn,
            "working": turn or time.time() - changed < BUSY_SECONDS,
            "activity": steps[-1] if steps else before.get("activity", ""),
        }
        if now != before:
            self.runtime.terminal[row["id"]] = now
            self.runtime.publish_session(row)

    async def history(self, session_id: str) -> dict:
        """The latest of a session Chief of Staff does not manage, read-only from its log
        (read on from where the last look ended, while the owner watches it)."""
        backend = self.runtime.require_ready()
        offset, entries, turn = self.histories.pop(session_id, (0, [], False))
        events, after, changed = await backend.read_history(session_id, offset)
        if after < offset:  # The log was rewritten.
            entries, turn = [], False
        entries = (entries + terminal_timeline(events, self.runtime.tools))[-HISTORY_SHOWN * 2 :]
        turn = turn_open(events, turn)
        self.histories[session_id] = (after, entries, turn)
        while len(self.histories) > 20:
            self.histories.pop(next(iter(self.histories)))
        return {
            "entries": [{"kind": kind, "text": text[:20000]} for kind, text, _ in entries],
            "open": session_id in self.runtime.elsewhere,
            "working": turn or time.time() - changed < BUSY_SECONDS,
        }

    async def refresh_elsewhere(self):
        """Which sessions are open in a terminal now; the UI marks the managed ones."""
        if not self.runtime.backend:
            return
        before = self.runtime.elsewhere
        self.runtime.elsewhere = await self.runtime.connected().open_elsewhere()
        changed = set(before) ^ set(self.runtime.elsewhere)
        for row in self.store.open_sessions(WORKER):
            if self.runtime.copilot_id(row["id"]) in changed:
                self.runtime.publish_session(row)

    async def tidy(self, moment: float | None = None) -> list[str]:
        """Close finished workers that follow nothing and have been quiet for a day."""
        moment = time.time() if moment is None else moment
        idle = self.store.settings().worker_idle_hours * 3600
        closed = []
        for row in self.store.open_sessions(WORKER):
            if self.runtime.copilot_id(row["id"]) in self.runtime.elsewhere:
                continue  # Someone is using it in a terminal.
            quiet = moment - timestamp(row["updated_at"]) >= idle
            if row["status"] in {"done", "failed"} and quiet and not self.store.follows(row["id"]):
                await self.close(row["id"])
                closed.append(row["id"])
        return closed
