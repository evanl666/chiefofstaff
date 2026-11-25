"""A new task for a busy worker, given to a second worker that starts at once with its context.

The Router gives a worker a new task (message_session, task="new") because that worker knows the
subject. When it is busy (in a turn, with other tasks open or at work for long) and a worker slot
is free, the task would wait for the rest of its work: Chief of Staff forks its session instead
(up to its turn in progress, not that turn) and starts a second worker on the fork, in a
workspace of its own, with a brief made from what Chief of Staff knows of the first one's work
now (no model call). The first worker goes on undisturbed. Anything that does not fit (the
setting off, no free slot, the Router's same_worker) leaves the task with the busy worker, as
before. The Router can also start a new worker from a related one's conversation
(spawn_worker's fork_of). Where its conversation cannot be copied (its agent's sessions are not
forked, or the fork failed), the second worker gets a fuller brief of its work instead (its
tasks' results, folders and files, as context_from gives). See docs/design/worker-fork.md.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from . import agents, machine
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
# A brief without its conversation: the results of its latest finished tasks, at most.
EARLIER_TASKS = 3
DONE = {"review", "done"}
# Why no second worker starts now that keeps any new worker from starting too.
NO_SLOT = "every worker slot is taken"
SHORT = "this computer is short of memory or disk"
EVERY_WORKER_WAITS = frozenset({NO_SLOT, SHORT})


@dataclass
class Second:
    """A second worker about to be made: the worker it comes from, its session's id (the fork
    of that worker's session, or a new one), whether it has that conversation (`native`) or a
    brief of its work only, and whether it takes a new task of that worker's because that one
    was busy (`busy`; the sidebar's ⑂ says so)."""

    source: dict
    session_id: str
    native: bool
    busy: bool = False


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

    def in_turn(self, row: dict) -> bool:
        """At work on a turn now (it holds a slot), or starting one."""
        return (
            row["role"] == WORKER
            and not row["closed_at"]
            and self.states[row["id"]].busy
            and row["status"] in {"working", "starting"}
        )

    def turn_minutes(self, row: dict) -> int:
        """How long it has been at work without a break, in whole minutes (0 between turns)."""
        since = self.states[row["id"]].busy_since
        return int((time.monotonic() - since) // 60) if since and self.in_turn(row) else 0

    def open_work(self, row: dict) -> int:
        """Its tasks to do: running (at work, or given to it and waiting for its turn) or
        queued (not those waiting for the owner, nor finished)."""
        return sum(t["status"] in {"running", "queued"} for t in self.tasks.for_session(row["id"]))

    def busy(self, row: dict) -> str:
        """Why the worker is busy ("" when it is not): in a turn with BUSY_TASKS open tasks or
        more (the owner's "many tasks"), or at work for BUSY_MINUTES without a break (a new task
        would wait for the rest of it)."""
        if not self.in_turn(row) or self.runtime.special(row):
            return ""
        if (count := self.open_work(row)) >= BUSY_TASKS:
            return f"{count} open tasks"
        if (minutes := self.turn_minutes(row)) >= BUSY_MINUTES:
            return f"at work for {minutes} minutes"
        return ""

    def load(self, row: dict) -> dict:
        """How loaded a worker is, for the Router (QA E87): whether it is in a turn and for how
        long, its open tasks, what was sent to it that it has not taken up yet, and why it is
        busy when it is (its new task then goes to a second worker, a slot free)."""
        state = self.states[row["id"]]
        mark = int(self.store.get(f"sent:{row['id']}") or 0)
        waiting = sum(
            not m["meta"].get("deferred") for m in self.store.prompts(row["id"], after=mark)
        ) + len(state.unread)
        load: dict[str, Any] = {"in_turn": self.in_turn(row), "open_tasks": self.open_work(row)}
        if load["in_turn"]:
            load["turn_minutes"] = self.turn_minutes(row)
        if waiting:
            load["messages_waiting"] = waiting
        if busy := self.busy(row):
            load["busy"] = busy
        return load

    def reason(
        self, target: dict, earlier: str = "", same_worker: bool = False, parallel: bool = False
    ) -> str:
        """Why a new task for this worker goes to a second worker with its context ("" when it
        stays with it, as it always did): it is busy, or in a turn while the Router says the
        owner wants the task done in parallel (`parallel`), a slot is free and its session can
        fork. `same_worker`, and `earlier="superseded"` (the new task replaces its running one),
        are the Router's: the task is that worker's own."""
        settings = self.store.settings()
        if not settings.fork_busy or same_worker or earlier == "superseded":
            return ""
        if target["role"] != WORKER or self.runtime.special(target):
            return ""
        why = self.busy(target) or ("wanted in parallel" if self.parallel(target, parallel) else "")
        return why if why and not self.blocked(target) else ""

    def parallel(self, target: dict, parallel: bool) -> bool:
        """The Router wants this new task done in parallel with the turn the worker is in."""
        return parallel and self.store.settings().fork_busy and self.in_turn(target)

    def blocked(self, target: dict) -> str:
        """Why no second worker takes a new task of this one's now, which then waits for it as
        before ("" when one can)."""
        if self.in_place(target):
            return "it works in the owner's own folder"  # One worker writes there.
        if self.runtime.workers.running() >= self.store.settings().max_workers:
            return NO_SLOT  # It waits for this worker, as for a slot.
        if machine.short_of_memory() is not None or machine.short_of_disk() is not None:
            return SHORT
        return ""

    def in_place(self, target: dict) -> bool:
        return any(c.get("in_place") for c in self.runtime.workspaces.checkouts(target))

    def uncopied(self, source: dict) -> str:
        """Why this worker's conversation is not copied to a second worker, which then gets a
        brief of its work only ("" when it is)."""
        if self.backend(source) is None:
            return "its agent's sessions are not forked"
        copilot = self.runtime.copilot_id(source["id"])
        if not self.runtime.made(source["id"]) or self.store.get(f"fresh:{copilot}"):
            return "nothing of its conversation to copy yet"
        if self.runtime.is_elsewhere(source):
            return "it is open in a terminal"
        if self.in_place(source):
            return "it works in the owner's own folder"  # Its conversation edits that folder.
        return ""

    async def second(self, source: dict, title: str) -> Second | None:
        """A second worker of this one: on a fork of its session when that can be made (its
        conversation so far), else on a new session with a brief of its work; None when its
        agent cannot run one now."""
        if not (why := self.uncopied(source)):
            try:
                return Second(source, await self.make(source, title), True)
            except Exception:
                logger.warning("Could not fork %s: a brief instead.", source["id"], exc_info=True)
        else:
            logger.info("A second worker of %s with a brief: %s.", source["id"], why)
        agent = self.runtime.agent(source["id"])
        if agent != agents.COPILOT and not await self.runs(agent):
            return None
        return Second(source, str(uuid4()), False)

    async def runs(self, agent: str) -> bool:
        """Whether a new worker on this agent (not Copilot) can start now."""
        entry = self.runtime.agents.get(agent)
        return bool(entry and entry.enabled()) and not await self.runtime.worker_agent_down(agent)

    async def make(self, source: dict, title: str) -> str:
        """Fork the busy worker's session for the second worker: its id."""
        backend = self.backend(source)
        if backend is None:
            raise RuntimeError("Its agent cannot fork a session.")
        # Copilot refuses a session name with a double quote (as Runtime.open_live).
        name = ("Chief of Staff · " + clean_title(title).replace('"', "'"))[:100]
        forked = await backend.fork_session(self.runtime.copilot_id(source["id"]), name)
        # The fork is experimental in Copilot's SDK: one it does not know of is not run on.
        if not await backend.get_session_metadata(forked):
            await self.drop(source, forked)
            raise RuntimeError(f"Copilot does not know the fork it answered ({forked}).")
        return forked

    async def drop(self, source: dict, session_id: str):
        """A fork no worker was made of (setting it up failed): gone again."""
        if backend := self.backend(source):
            with suppress(Exception):
                await backend.delete_session(session_id)

    async def record(self, row: dict, second: Second):
        """The second worker's row exists: whose worker it came from (relations.py: a fork),
        and on a fork, that its session is made (it resumes the fork) and where its own part of
        its log begins (what it copied is never shown as what it did, nor counted as what it
        used)."""
        session_id, source = row["id"], second.source
        origin: dict[str, Any] = {"id": source["id"], "title": self.runtime.display_title(source)}
        if not second.native:
            origin["brief"] = True
        if second.busy:
            origin["busy"] = True
        self.store.set(f"forkof:{session_id}", json.dumps(origin, ensure_ascii=False))
        if not second.native:
            return
        self.store.set(f"created:{session_id}", "1")
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

    def brief(self, source: dict, row: dict, native: bool = True) -> str:
        """What the second worker is told with its task, from Chief of Staff's own records:
        with its conversation copied (`native`), or without it (what its finished tasks found
        too)."""
        name = self.runtime.display_title(source)
        lines = [
            (
                f"You are a second worker, started from the conversation of worker "
                f"{short(source['id'])} “{name}”: what came before this message is its "
                "conversation up to its last finished turn, copied to you."
                if native
                else f"You are a second worker of worker {short(source['id'])} “{name}”, "
                "started with what Chief of Staff knows of its work (its conversation is not "
                "copied to you): it is below."
            )
            + " That worker goes on with its own work in its own folder. Its tasks are not "
            "yours: do not continue, check on or report them, and do not tell the owner about "
            "them."
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
        finished = [t["id"] for t in self.tasks.for_session(source["id"]) if t["status"] in DONE]
        if finished and not native:
            lines.append(self.tasks.briefing(finished[-EARLIER_TASKS:], "What it did before"))
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
