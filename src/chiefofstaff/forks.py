"""A new task for a busy worker, given to a second worker that starts at once with its context.

The Router gives a worker a new task (message_session, task="new") because that worker knows the
subject. When it is busy (in a turn, with other tasks open or at work for long) and a worker slot
is free, the task would wait for the rest of its work: Chief of Staff forks its session instead
(up to its turn in progress, not that turn) and starts a second worker on the fork, in a
workspace of its own, with a brief made from what Chief of Staff knows of the first one's work
now (no model call). The first worker goes on undisturbed. Anything that does not fit (the
setting off, no free slot, an agent that cannot fork, the Router's same_worker) leaves the task
with the busy worker, as before. See docs/design/worker-fork.md.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import machine
from .envelope import clean_title, short
from .policy import within
from .roles import WORKER

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

# A worker in a turn is busy with this many open tasks (running or queued: the owner's "many
# tasks"), or once it has been at work this many minutes without a break.
BUSY_TASKS = 2
BUSY_MINUTES = float(os.environ.get("CHIEFOFSTAFF_FORK_BUSY_MINUTES", "10"))
# What the brief says of each of the first worker's open tasks, at most.
GOAL_CHARS = 400
PROGRESS_CHARS = 300


class Forks:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.tasks = runtime.tasks
        self.states = runtime.states

    def backend(self, row: dict) -> Any | None:
        """The backend that can fork this worker's session; None when its agent cannot."""
        try:
            backend = self.runtime.backend_for(row)
        except RuntimeError:
            return None
        capabilities = getattr(backend, "capabilities", None)
        return backend if getattr(capabilities, "fork", False) else None

    def reason(self, target: dict, earlier: str = "", same_worker: bool = False) -> str:
        """Why a new task for this worker goes to a second worker with its context ("" when it
        stays with it, as it always did): it is busy, a slot is free and its session can fork.
        `earlier` (finish, superseded: the new task follows or replaces its running one) and
        `same_worker` are the Router's: the task is that worker's own."""
        settings = self.store.settings()
        if not settings.fork_busy or same_worker or earlier not in {"", "keep"}:
            return ""
        if target["role"] != WORKER or target["closed_at"] or self.runtime.special(target):
            return ""
        state = self.states[target["id"]]
        if not state.busy or target["status"] not in {"working", "starting"}:
            return ""
        copilot = self.runtime.copilot_id(target["id"])
        if (
            not self.runtime.made(target["id"])
            or self.store.get(f"fresh:{copilot}")
            or copilot in self.runtime.elsewhere
        ):
            return ""  # Nothing of it to fork yet, or open in a terminal.
        if any(c.get("in_place") for c in self.runtime.workspaces.checkouts(target)):
            return ""  # The owner's own folder: one worker writes there.
        if self.backend(target) is None:
            return ""
        if self.runtime.workers.running() >= settings.max_workers:
            return ""  # No free slot: it waits for this worker, as it would for a slot.
        if machine.short_of_memory() is not None or machine.short_of_disk() is not None:
            return ""
        open_ = [
            t for t in self.tasks.for_session(target["id"]) if t["status"] in {"running", "queued"}
        ]
        if len(open_) >= BUSY_TASKS:
            return f"{len(open_)} open tasks"
        if state.busy_since and time.monotonic() - state.busy_since >= BUSY_MINUTES * 60:
            return f"at work for {int((time.monotonic() - state.busy_since) // 60)} minutes"
        return ""

    async def make(self, source: dict, title: str) -> str:
        """Fork the busy worker's session for the second worker: its id."""
        backend = self.backend(source)
        if backend is None:
            raise RuntimeError("Its agent cannot fork a session.")
        # Copilot refuses a session name with a double quote (as Runtime.open_live).
        name = ("Chief of Staff · " + clean_title(title).replace('"', "'"))[:100]
        return await backend.fork_session(self.runtime.copilot_id(source["id"]), name)

    async def drop(self, source: dict, session_id: str):
        """A fork no worker was made of (setting it up failed): gone again."""
        if backend := self.backend(source):
            with suppress(Exception):
                await backend.delete_session(session_id)

    async def record(self, row: dict, source: dict):
        """The second worker's row exists: its session is made (it resumes the fork), whose
        worker it came from, and where its own part of its log begins (what it copied is never
        shown as what it did, nor counted as what it used)."""
        session_id = row["id"]
        self.store.set(f"created:{session_id}", "1")
        origin = {"id": source["id"], "title": self.runtime.display_title(source)}
        self.store.set(f"forkof:{session_id}", json.dumps(origin, ensure_ascii=False))
        end = 0
        if backend := self.backend(row):
            with suppress(Exception):
                _, end, _ = await backend.read_history(session_id, 0)
        self.store.set(f"forklog:{session_id}", str(end))
        self.store.set(f"usage_log:{session_id}", str(end))
        self.runtime.workers.read_to[session_id] = end

    def origin(self, session_id: str) -> dict | None:
        """The worker a second worker came from: its id and name (as it is now, while it is
        known; as it was then, otherwise)."""
        stored = self.store.get(f"forkof:{session_id}")
        if not stored:
            return None
        origin = json.loads(stored)
        if row := self.store.find_session(origin["id"]):
            origin["title"] = self.runtime.display_title(row)
        return origin

    def refused_write(self, row: dict, request: Any) -> str | None:
        """Why a second worker may not write this file: it is in a folder of the worker it came
        from, which works there (its conversation is full of that folder's paths). Also when it
        acts without permission checks (yolo), as the owner's checkouts are never written."""
        if getattr(request, "kind", "") != "write" or not (origin := self.origin(row["id"])):
            return None
        source = self.store.find_session(origin["id"])
        if not source or source["closed_at"]:
            return None
        target = getattr(request, "resolved_path", None) or getattr(request, "file_name", "")
        if not Path(target).is_absolute():
            target = str(Path(row["cwd"]) / target)
        own = self.runtime.workspaces.writable(row)
        theirs = self.runtime.workspaces.writable(source)
        if any(within(target, place) for place in own) or not any(
            within(target, place) for place in theirs
        ):
            return None
        return (
            f"Not written: that is the folder of the worker whose conversation you have "
            f"(“{origin['title']}”), and it is working there. Write in your own folder: "
            f"{row['cwd']}."
        )

    def log_start(self, session_id: str) -> int:
        """Where a worker's own part of its log begins: after what a fork copied (0 for any
        other)."""
        return int(self.store.get(f"forklog:{session_id}") or 0)

    def brief(self, source: dict, row: dict) -> str:
        """What the second worker is told with its task, from Chief of Staff's own records."""
        name = self.runtime.display_title(source)
        lines = [
            f"You are a second worker, started from the conversation of worker "
            f"{short(source['id'])} “{name}” because it is busy: what came before this message "
            "is its conversation up to its last finished turn, copied to you. That worker goes "
            "on with its own work in its own folder. Its tasks are not yours: do not continue, "
            "check on or report them, and do not tell the owner about them."
        ]
        open_ = [
            t
            for t in self.tasks.for_session(source["id"])
            if t["status"] in {"running", "queued", "waiting"}
        ]
        if open_:
            lines.append("What it is at now:")
            for task in open_:
                said = f"- Task {task['id']} “{task['title']}” ({task['status']})"
                if goal := " ".join((task["goal"] or "").split()):
                    said += f": {goal[:GOAL_CHARS]}"
                if progress := " ".join((task["progress"] or "").split()):
                    said += f" Latest: {progress[:PROGRESS_CHARS]}"
                lines.append(said)
        files = json.loads(source.get("files") or "[]")[-12:]
        if files:
            lines.append("Files it changed so far: " + ", ".join(files))
        theirs = self.runtime.workspaces.checkouts(source)
        where = source["cwd"] + (f" (branch {source['branch']})" if source["branch"] else "")
        lines.append(
            f"Its folder: {where}. Read it if you need its work in progress (its commits are in "
            "the same repository), but never change anything there: it is working in it."
        )
        others = [c for c in theirs if not c.get("project") and c.get("remote")]
        if others:
            named = ", ".join(c["remote"] for c in others)
            lines.append(
                f"It also has its own clone of {named}: add_repo one for yourself if you need it."
            )
        mine = row["cwd"] + (
            f" (branch {row['branch']}, new from the project's base)" if row["branch"] else ""
        )
        lines.append(
            f"Your folder: {mine}. Your changes go there only. If this task can only be done on "
            "its branch or in its pull request, do not start another: report a question saying so."
        )
        return "\n".join(lines)
