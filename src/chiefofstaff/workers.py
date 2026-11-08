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

from . import catalog, machine
from .backend import stop_background
from .envelope import clean_title, owner_text, short, wrap
from .events import PLUMBING_TOOLS, describe_tool
from .roles import ROUTER, WORKER
from .sources.slack import describe
from .store import now
from .tasks import ADOPTED, OPEN
from .watchdog import instruction, since_last_instruction, timestamp

# A worker quiet this long (seconds) lets go of its Copilot session (see release_idle).
RELEASE_IDLE = float(os.environ.get("CHIEFOFSTAFF_RELEASE_IDLE_MINUTES", "10")) * 60
# What a queued worker's card says while the computer is low on memory.
MEMORY_WAIT = "waiting for memory"
# A final answer at least this long, left in a worker's chat without report(), is its report.
FILED_ANSWER_CHARS = 80
# An answer in its chat this long, and twice its "done" report or more, completes that report.
FULLER_ANSWER_CHARS = 200
# Words of a worker that stopped to wait, not to deliver.
WAITS = re.compile(
    r"\b(waiting|wait for|once (it|they|the|that|this|ci)\b"
    r"|when (it|they|ci) (finish|answer|complete)|will (continue|resume|go on|pick (it|this) up))",
    re.IGNORECASE,
)

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
        # Never read yet (not 0: the monotonic clock starts at boot, so 0 can be "just now").
        self.external_cache: tuple[float, list[dict]] = (float("-inf"), [])
        # Reading logs of sessions open in a terminal; held while their timelines catch up.
        self.mirror_lock = asyncio.Lock()
        # Other sessions' logs as last read: session id -> (offset, entries, turn open).
        self.histories: dict[str, tuple[int, list[Entry], bool]] = {}
        # How far catch_up has read each worker's log.
        self.read_to: dict[str, int] = {}
        self.usage_synced = False

    def running(self) -> int:
        """Workers in a turn, of the owner's limit: Chief of Staff's own helpers (learning,
        checks) do not take the owner's slots."""
        return sum(
            1
            for row in self.store.open_sessions(WORKER)
            if self.states[row["id"]].busy and not self.runtime.special(row)
        )

    async def claim_slot(self, session_id: str) -> bool:
        """Reserve a worker slot for a new turn, or leave the message pending (queued)."""
        async with self.pump_lock:
            row = self.store.session(session_id)
            if row["status"] in {"queued", "starting"}:
                return False
            if self.states[session_id].busy:
                return True
            limit = self.store.settings().max_workers
            if self.running() >= limit and not self.runtime.special(row):
                self.runtime.update_session(session_id, status="queued", activity="")
                return False
            if not self.runtime.special(row) and (free := machine.short_of_memory()) is not None:
                self.runtime.update_session(session_id, status="queued", activity=memory_note(free))
                return False
            self.states[session_id].busy = True
            return True

    def complete_report(self, session_id: str):
        """A worker that reported done in a line ("Analysis complete") and wrote the answer
        itself in its chat: the owner reads reports, so the report gets that answer."""
        recent = since_last_instruction(self.store.messages(session_id, limit=400))
        reports = [m for m in recent if m["kind"] == "report" and m["meta"].get("status") == "done"]
        if not reports or not self.states.handle(session_id):
            return
        if len({m["meta"].get("task_id") for m in reports}) > 1:
            return  # Several tasks reported in one turn: its answer is about them all.
        report = reports[-1]
        # The task that report was on (it may have moved on to another one since).
        task_id = report["meta"].get("task_id")
        task = self.tasks.find(task_id) if task_id else self.tasks.current(session_id)
        if not task or task["status"] not in {"review", "done"}:
            return
        answers = [
            m["text"].strip()
            for m in recent
            if m["kind"] == "assistant" and not m["meta"].get("narration")
        ]
        answer = max(answers, key=len, default="")
        short = report["text"].strip()
        if len(answer) < FULLER_ANSWER_CHARS or len(answer) < 2 * len(short) or answer in short:
            return
        from .tools import clip_report

        full = f"{short}\n\n{answer}"
        amended = {"amended": True}
        self.runtime.bus.publish(
            "message", self.store.update_message_text(report["id"], full) | amended
        )
        router = self.runtime.router_id()
        inbound = self.store.one(
            "SELECT id FROM messages WHERE session_id=? AND kind='inbound' "
            "AND json_extract(meta, '$.from')=? AND json_extract(meta, '$.task_id')=? "
            "ORDER BY id DESC LIMIT 1",
            (router, session_id, task["id"]),
        )
        if inbound:
            self.runtime.bus.publish(
                "message", self.store.update_message_text(inbound["id"], full) | amended
            )
        self.tasks.update(task["id"], result=clip_report(full))
        self.runtime.update_session(session_id, report=full[:4000])

    def answer_on_home(self, session_id: str):
        """The owner wrote to a worker on Home (@): its answer shows there too, once, as a line
        that opens the worker (its report, if it made one, is on the task's card)."""
        recent = since_last_instruction(self.store.messages(session_id, limit=400))
        if not recent or recent[0]["kind"] != "owner" or not recent[0]["meta"].get("home"):
            return
        asked = recent[0]
        if self.store.get(f"answered_home:{asked['id']}"):
            return
        said = [
            m
            for m in recent[1:]
            if m["kind"] == "report"
            or (m["kind"] == "assistant" and not m["meta"].get("narration"))
        ]
        if not said:
            return
        if any(m["kind"] == "report" and m["meta"].get("told") for m in recent[1:]):
            # Its result or question reached Home already (a line, a card): not again.
            self.store.set(f"answered_home:{asked['id']}", "1")
            return
        self.store.set(f"answered_home:{asked['id']}", "1")
        row = self.store.session(session_id)
        with suppress(RuntimeError):
            self.runtime.add_message(
                self.runtime.router_id(),
                "answer",
                said[-1]["text"][:4000],
                {
                    "session_id": session_id,
                    "task_id": asked["meta"].get("task_id") or "",
                    "from_title": self.runtime.display_title(row),
                },
                unread=True,
            )

    async def ask_for_report(self, session_id: str):
        """A turn that ended on its task without report(): the owner sees only reports. Its
        final answer, when it wrote one, becomes the report as written (at once, and nothing is
        lost in a retelling); otherwise it is asked once to report. Not after the owner's own
        chat with it (they read its answer there), nor after that request (the Watchdog
        follows it up)."""
        row = self.store.session(session_id)
        if row["status"] not in {"working", "starting", "queued"} and not row["closed_at"]:
            self.answer_on_home(session_id)  # Its turn is over: what it answered is all.
        if row["status"] not in {"working", "starting", "queued"} and not row["closed_at"]:
            await self.report_chat_change(session_id)
        running = [t for t in self.tasks.for_session(session_id) if t["status"] == "running"]
        current = self.tasks.current(session_id)
        if row["status"] != "idle" or row["closed_at"] or not running:
            return
        # The task its turn was about: the one it is on when that is running; else the one its
        # last instruction named; else its newest running one.
        task = current if current and current["status"] == "running" else running[-1]
        if self.runtime.special(row) in {"connector", "messenger"}:
            # It reports through its own tools: a run that ended is done.
            self.tasks.move(task["id"], "done", result="Run finished.")
            return
        if (special := self.runtime.special(row)) in {"learner", "checker"}:
            # A helper that stopped short of its last call has nothing more to propose.
            if special == "checker":
                await self.runtime.checks.stopped()
            self.tasks.move(task["id"], "done", result="Stopped without proposing more.")
            await self.close(session_id)
            return
        recent = since_last_instruction(self.store.messages(session_id, limit=400))
        if not recent or not instruction(recent[0]):
            return
        asked = recent[0]
        named = next((t for t in running if t["id"] == asked["meta"].get("task_id")), None)
        if named:
            task = named
        if self.store.get(f"checkback:{task['id']}"):
            return  # It said when it looks again: woken then, it reports.
        nudged = bool(asked["meta"].get("report_nudge"))
        chat = asked["meta"].get("chat") or not asked["meta"].get("task_id")
        if asked["kind"] == "owner" and chat:
            return  # The owner's own chat with it: they read its answer there.
        if any(m["meta"].get("stopped") for m in recent):
            return
        reports = [m for m in recent if m["kind"] == "report"]
        if reports:
            await self.ask_what_it_waits_for(session_id, task, asked, reports[-1])
            return
        if asked["meta"].get("wait_nudge"):
            return  # Asked what it waits for and silent: the stall check follows it up.
        if self.states[session_id].busy or not self.states.handle(session_id):
            return  # Working again, or being closed or reattached.
        answers = [m for m in recent if m["kind"] == "assistant" and not m["meta"].get("narration")]
        answer = answers[-1]["text"].strip() if answers else ""
        # Waiting for something (an answer it asked for, a card, a thread or PR it follows):
        # its last words say what it waits for, not its result. Then it reports itself.
        waiting = bool(
            WAITS.search(answer)
            or self.runtime.asks.open_questions(session_id)
            or self.store.follows(session_id)
            or any(d["session_id"] == session_id for d in self.store.drafts("pending"))
        )
        if WAITS.search(answer) and (peers := self.runtime.peers.waiting_on(session_id, answer)):
            # Waiting for a worker it works with: that worker's message starts its next turn.
            names = ", ".join(f"“{peer['title']}”" for peer in peers)
            self.runtime.update_session(session_id, activity=f"waiting for {names}")
            return
        if nudged:
            if waiting:
                return  # It says what it waits for; the Watchdog follows it up.
            # Asked once already and it still did not report: its last words are its report,
            # so the task leaves Running (the owner reviews what it said).
            from .tools import Report

            summary = answer or "It stopped without a report and said nothing more."
            # report() turns some reports back once each (a short answer, failing tests): the
            # owner judges it now, so it is filed all the same.
            for _ in range(4):
                try:
                    await self.runtime.toolset.t_report(
                        Report(status="done", summary=summary, task_id=task["id"]), session_id
                    )
                except ValueError:
                    continue
                except Exception:
                    logger.warning("Could not file %s's last words.", session_id, exc_info=True)
                    return
                self.runtime.add_message(
                    session_id, "notice", "It stopped again without a report: filed as one."
                )
                return
            return
        text = (
            "Your turn ended without report(). The owner sees only reports: call report now "
            "with your full result (status done), or question or blocked if you need something "
            "from the owner; if you wait for something outside (a run, a deploy, a reply), call "
            "check_back with when to look again. Do not redo the work."
        )
        if not waiting and len(answer) >= FILED_ANSWER_CHARS:
            from .tools import Report

            status = "question" if answer.endswith("?") else "done"
            # The task its last instruction was about, when it has several open.
            about = asked["meta"].get("task_id") or task["id"]
            try:
                await self.runtime.toolset.t_report(
                    Report(status=status, summary=answer, task_id=about), session_id
                )
            except ValueError as exc:
                # Not taken as it is (a check fails, an asked-for table is missing): the worker
                # hears why, as it would have from report.
                text = f"Your turn ended without report(), and your answer was not taken: {exc}"
            except Exception:
                logger.warning("Could not file %s's answer as its report.", session_id)
            else:
                self.runtime.add_message(
                    session_id, "notice", "It ended without a report: its answer is filed as one."
                )
                return
        with suppress(Exception):
            await self.delivery.deliver(
                session_id,
                wrap("system", "report-missing", text, task=task["id"]),
                kind="notice",
                display="It stopped without a report: asked for one.",
                meta={"report_nudge": True},
            )

    async def report_chat_change(self, session_id: str):
        """Asked in chat for a change to finished work, it changed its files and ended without
        report(): asked once to report it, so the owner reviews it as the task's new result."""
        for task in self.tasks.for_session(session_id):
            if task["status"] not in {"review", "done"}:
                continue
            if self.store.get(f"followup:{task['id']}") != "chat":
                continue
            chat = json.loads(self.store.get(f"chatbase:{task['id']}") or "{}")
            since = chat.get("at", "")
            if not since or self.store.get(f"chatnudged:{task['id']}") == since:
                continue
            reported = self.store.value(
                "SELECT count(*) FROM messages WHERE session_id=? AND kind='report' AND "
                "json_extract(meta, '$.task_id')=? AND created_at>?",
                (session_id, task["id"], since),
            )
            if reported or not await self.runtime.task_actions.changed_since_result(task):
                continue
            self.store.set(f"chatnudged:{task['id']}", since)
            text = (
                f"You changed files for task {task['id']} “{task['title']}” in this chat, after "
                f"its result. Report it now with report(status='done', task_id='{task['id']}'): "
                "it is its new result, for the owner to review."
            )
            with suppress(Exception):
                await self.delivery.deliver(
                    session_id,
                    wrap("system", "report-missing", text, task=task["id"]),
                    kind="notice",
                    display="It changed finished work without a report: asked for one.",
                    meta={"report_nudge": True, "task_id": task["id"]},
                )
            return

    async def ask_what_it_waits_for(
        self, session_id: str, task: dict, asked: dict, report: dict
    ) -> None:
        """Its turn ended after a progress report on a task that is still running, with
        nothing that will wake it (no check-back, no thread or PR followed for it, no card or
        question for the owner): it is asked once what it waits for, so the task does not sit
        under Running with nobody on it. Not after that question itself."""
        status = report["meta"].get("status")
        about = report["meta"].get("task_id") or task["id"]
        task = self.tasks.find(about) or task
        if status != "progress" or task["status"] != "running" or asked["meta"].get("wait_nudge"):
            return
        if self.states[session_id].busy or not self.states.handle(session_id):
            return
        follows = self.store.follows(session_id)
        if (
            self.runtime.task_actions.check_back(task)
            or self.runtime.task_actions.watching(task, follows)
            or self.runtime.asks.open_questions(session_id)
            or any(d["session_id"] == session_id for d in self.store.drafts("pending"))
            or self.runtime.peers.waiting_on(session_id, report["text"])
        ):
            return
        text = (
            f"Your turn ended after a progress report, and task {task['id']} “{task['title']}” "
            "is still running with nothing that will wake you: no check-back, and no thread or "
            "PR you follow for it. Say what happens next. If you need something from the owner "
            "(a key, access, a choice), report blocked or question with exactly what. If you "
            "wait for something outside (a run, a deploy, a reply), call check_back with when "
            "to look again. If it is finished, report done. Otherwise go on with it now."
        )
        with suppress(Exception):
            await self.delivery.deliver(
                session_id,
                wrap("system", "wait-missing", text, task=task["id"]),
                kind="notice",
                display="It stopped on a running task with nothing to wait for: asked what next.",
                meta={"wait_nudge": True, "task_id": task["id"]},
            )

    async def pump(self):
        """Start queued workers while fewer than max_workers are running a turn."""
        if not self.runtime.ready():
            return
        starting = []
        async with self.pump_lock:
            limit = self.store.settings().max_workers
            short = machine.short_of_memory()
            for row in self.store.open_sessions(WORKER):
                if row["status"] != "queued":
                    continue
                if not self.store.get(f"created:{row['id']}") and self.waiting_for(row):
                    continue
                if short is not None and not self.runtime.special(row):
                    # More work now could freeze the computer: it starts once memory frees up.
                    if row["activity"] != memory_note(short):
                        self.runtime.update_session(row["id"], activity=memory_note(short))
                    continue
                if self.running() < limit or self.runtime.special(row):
                    self.states[row["id"]].busy = True
                    starting.append(self.runtime.update_session(row["id"], status="starting"))
        for row in starting:
            await self.start(row)

    def waiting_for(self, row: dict) -> list[dict]:
        """Tasks a worker's queued tasks still wait for (any of them: what it has not received
        goes out together); shown as the worker's activity."""
        queued = [
            task
            for task in self.tasks.for_session(row["id"])
            if task["status"] == "queued" and not self.store.get(f"released:{task['id']}")
        ]
        waiting = list(
            {dep["id"]: dep for task in queued for dep in self.tasks.unmet(task)}.values()
        )
        names = ", ".join(
            f"your review of “{d['title']}”" if d["status"] == "review" else f"“{d['title']}”"
            for d in waiting
        )
        activity = f"waiting for {names}" if waiting else ""
        if row["activity"] != activity and not (
            not waiting and row["activity"].startswith(MEMORY_WAIT)
        ):
            self.runtime.update_session(row["id"], activity=activity)
        for task in queued:
            broken = self.tasks.broken(task)
            if not broken and self.store.get(f"depblocked:{task['id']}"):
                # Blocked no more (what it waits for was retried, or finished after all): what
                # the Router asked the owner about it is moot.
                self.store.delete(f"depblocked:{task['id']}")
                self.runtime.background(
                    self.delivery.deliver(
                        self.runtime.router_id(),
                        wrap(
                            "system",
                            "unblocked",
                            f"Task {task['id']} is no longer blocked: what it waits for is "
                            "running again or finished, and it starts after it. Nothing for the "
                            "owner to decide about it: if you asked them, say it is settled.",
                            worker=short(row["id"]),
                            task=task["id"],
                            title=task["title"],
                        ),
                        kind="inbound",
                        display=f"“{task['title']}” is no longer blocked.",
                        meta={"from": row["id"], "title": task["title"], "task_id": task["id"]},
                        batch=True,
                    )
                )
                continue
            if not broken or self.store.get(f"depblocked:{task['id']}"):
                continue
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
                    display=f"“{task['title']}” waits for {names}, which ended without finishing.",
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
        for task in self.tasks.for_session(session_id):
            done = self.tasks.deps(task["id"]) if task["status"] == "queued" else []
            if not done or self.store.get(f"handed:{task['id']}"):
                continue
            results = "\n".join(
                f"- Task {d['id']} “{d['title']}” ({d['status']}): {d['result'] or 'no report'}"
                for d in done
            )
            text = (
                f"Results of the tasks this one waited for (task {task['id']} “{task['title']}”):"
                f"\n{results}"
            )
            self.runtime.add_message(
                session_id,
                "router",
                text,
                {"prompt": wrap("router", "results", text, task=task["id"]), "task_id": task["id"]},
            )
            self.store.set(f"handed:{task['id']}", "1")
        connection = self.runtime.connections
        try:
            if self.store.get(f"created:{session_id}"):
                session = await self.runtime.session_for(session_id)
            else:
                async with self.states[session_id].lifecycle:
                    session = self.states.handle(session_id) or await self.runtime.open_live(
                        row, create=True
                    )
            current = self.store.session(session_id)
            if current["closed_at"] or current["status"] != "starting":
                # Cancelled, stopped or closed while its session was made: nothing to send now.
                self.states[session_id].busy = False
                self.runtime.background(self.pump())
                return
            self.runtime.update_session(session_id, status="working")
            current = self.tasks.current(session_id)
            for task in self.tasks.for_session(session_id):
                if task["status"] == "queued" and (not current or task["id"] != current["id"]):
                    self.tasks.move(task["id"], "running")  # Its instructions go out now too.
            if current and current["status"] == "queued":
                self.tasks.move(current["id"], "running")  # Last: the one it is on.
            elif current:
                self.store.execute(
                    "UPDATE sessions SET task_id=? WHERE id=?", (current["id"], session_id)
                )
            # Flushing now includes messages that arrived while the session was being created.
            if not await self.delivery.flush(session_id, session):
                self.states[session_id].busy = False
                self.runtime.update_session(session_id, status="idle")
                self.runtime.background(self.pump())
        except asyncio.CancelledError:
            # Stopped, reconnected or shut down meanwhile: it waits in the queue again.
            self.requeue(session_id)
            raise
        except Exception as exc:
            if not self.runtime.backend or self.runtime.connections != connection:
                self.requeue(session_id)  # Copilot disconnected meanwhile: not its failure.
                return
            self.runtime.fail(row, f"Could not start: {describe(exc)}")

    def requeue(self, session_id: str):
        """A start that did not happen: the worker waits in the queue again."""
        self.states[session_id].busy = False
        current = self.store.find_session(session_id)
        if current and not current["closed_at"] and current["status"] in {"starting", "working"}:
            self.runtime.update_session(session_id, status="queued", activity="")
        self.runtime.background(self.pump())  # Does nothing until Copilot is ready again.

    def finished(self, session_id: str) -> bool:
        """Nothing of it is open: no task at work, waiting or queued, no card for the owner,
        no question of it unanswered. Only then may it close by itself."""
        if any(task["status"] in OPEN for task in self.tasks.for_session(session_id)):
            return False
        if any(draft["session_id"] == session_id for draft in self.store.drafts()):
            return False
        return not self.runtime.asks.session_questions(session_id)

    async def close(self, session_id: str, by: str = ""):
        """`by`: who closed it ("you": the owner; "the Router"; "" for Chief of Staff, once
        nothing of it is open). The Router hears of open work closed with it."""
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
                    await stop_background(session)
                with suppress(Exception):
                    await session.disconnect()
        self.states[session_id].busy = False
        self.states[session_id].steps.clear()
        self.store.unfollow_session(session_id)
        self.runtime.decisions.drop_asker(session_id)
        self.states[session_id].last_output = ""
        self.states[session_id].close_after_turn = False
        # A question it asked the owner (Copilot's ask_user) still waits for them, under Needs
        # you: answered, it reopens (as one asked with report does).
        self.runtime.asks.cancel_requests(
            session_id,
            "The session was closed.",
            keep_questions=self.runtime.working_on(session_id) if row["role"] == WORKER else None,
        )
        await self.runtime.asks.cancel_questions(session_id)
        # What it had received of each open task: one it worked on comes back when it reopens.
        received = {
            task["id"]: self.delivery.drop_pending(session_id, task["id"])
            for task in self.tasks.for_session(session_id)
            if task["status"] in OPEN
        }
        self.delivery.drop_pending(session_id)  # A reopened worker does not get them.
        ended = []
        for task in self.tasks.for_session(session_id):
            if task["status"] not in OPEN:
                continue
            if was := self.store.get(f"reopened:{task['id']}"):
                # Only its follow-up ends: the result it had stands.
                self.store.delete(f"reopened:{task['id']}")
                self.tasks.move(task["id"], was, progress="Follow-up ended: its worker was closed.")
                ended.append(f"the follow-up on {task['id']} “{task['title']}” (its result stands)")
            else:
                if received.get(task["id"]) or self.store.get(f"started:{task['id']}"):
                    # Reopened by the owner, it is back where it was (a task at work: stopped).
                    self.store.set(
                        f"closedtask:{task['id']}",
                        json.dumps({
                            "by": by or "Chief of Staff", "status": task["status"],
                            "progress": task["progress"],
                            "stopped": self.store.get(f"stopped:{task['id']}") or "",
                        }),
                    )  # fmt: skip
                self.tasks.move(task["id"], "cancelled", progress="Its worker was closed.")
                ended.append(f"{task['id']} “{task['title']}”")
        # Not "stopped without reporting": the Watchdog leaves a reopened worker alone until told.
        self.runtime.add_message(session_id, "notice", "Closed.", {"stopped": True})
        self.runtime.update_session(session_id, closed_at=now(), activity="")
        if special := self.runtime.special(row):
            # Chief of Staff's own helper: what it did is told once, not its comings and goings.
            if special == "learner":
                self.runtime.habits.summarize(session_id)
        else:
            with suppress(RuntimeError):
                router = self.runtime.router_id()
                title = self.runtime.display_title(row)
                display, meta = f"Closed “{title}”", {"session_id": session_id}
                if ended and by.lower().removeprefix("the ") != "router":
                    # Held for the Router's next message: it must not start that work again.
                    who = "The owner" if by == "you" else "Chief of Staff"
                    text = (
                        f"{who} closed worker {short(session_id)} “{title}” with open work: "
                        f"{'; '.join(ended)}. Cancelled with it (if the owner reopens the worker, "
                        "what it had started comes back). Do not start that work again (in this "
                        "worker or another) unless the owner asks for it."
                    )
                    self.delivery.hold(
                        router,
                        wrap("system", "closed", text, worker=short(session_id), title=title),
                        kind="notice",
                        display=display,
                        meta=meta,
                    )
                else:
                    self.runtime.add_message(router, "notice", display, meta)
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
            "current_task": self.runtime.toolset.task_brief(
                self.tasks.current(row["id"]), router=True
            ),
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
        helpers = self.runtime.helper_ids()
        rows = {
            row["id"]: row
            for row in self.store.all(
                "SELECT * FROM sessions WHERE role='worker' ORDER BY updated_at DESC LIMIT 2000"
            )
            if row["id"] not in helpers
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

    async def reopen(self, session_id: str, restore: bool = False) -> dict:
        """Bring a closed worker back with its conversation, worktree and branch. `restore`:
        the owner reopens it, and the tasks closing it cancelled that it had started are back
        where they were: stopped, waiting for their Go on (or Cancel)."""
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
        restored = []
        for task in self.tasks.for_session(session_id):
            was = json.loads(self.store.get(f"closedtask:{task['id']}") or "null")
            self.store.delete(f"closedtask:{task['id']}")
            if not (restore and was and task["status"] == "cancelled"):
                continue
            if was["status"] == "waiting" and not was["stopped"]:
                # It waited for the owner's answer: it does again.
                restored.append(self.tasks.move(task["id"], "waiting", progress=was["progress"]))
                continue
            by = was["stopped"] or was["by"]
            self.store.set(f"stopped:{task['id']}", by)
            # Who closed it (not who had stopped it before).
            progress = f"Stopped when {was['by']} closed its worker: Go on, or Cancel."
            restored.append(self.tasks.move(task["id"], "waiting", progress=progress))
        if restored:
            self.store.execute(
                "UPDATE sessions SET task_id=? WHERE id=?", (restored[-1]["id"], session_id)
            )
            asking = any(not self.store.get(f"stopped:{t['id']}") for t in restored)
            row = self.runtime.update_session(session_id, status="waiting" if asking else "idle")
        with suppress(RuntimeError):
            display = f"Reopened “{self.runtime.display_title(row)}”"
            meta = {"session_id": session_id}
            if restored:
                listed = "; ".join(f"{t['id']} “{t['title']}”" for t in restored)
                text = (
                    f"The owner reopened worker {short(session_id)}: what closing it cancelled "
                    f"is back where it was, waiting for the owner (to go on, or an answer): "
                    f"{listed}."
                )
                self.delivery.hold(
                    self.runtime.router_id(),
                    wrap("system", "reopened", text, worker=short(session_id)),
                    kind="notice",
                    display=display,
                    meta=meta,
                )
            else:
                self.runtime.add_message(self.runtime.router_id(), "notice", display, meta)
        return self.runtime.view(row)

    async def interrupt(self, session_id: str, by: str = "you"):
        row = self.store.session(session_id)
        self.runtime.asks.cancel_requests(session_id, f"Stopped by {by}.")
        self.states[session_id].after_refresh = ""  # Not "go on" once it is stopped.
        if row["role"] == WORKER:
            self.states[session_id].after_abort = True
        if row["status"] in {"working", "queued", "starting"}:
            # Recorded before the abort: the turn's end (idle) can arrive before the abort
            # returns, and must find a deliberate stop, not a worker that ended without a report
            # (nor a silent stall for the Watchdog).
            self.runtime.update_session(session_id, status="idle", activity="")
            self.runtime.add_message(session_id, "notice", f"Stopped by {by}.", {"stopped": True})
            # The task it was on (not one given last that it has not started), or, before it
            # started, those it was given: stopped, waiting for the owner to say go on or cancel.
            on = self.runtime.working_on(session_id) if row["role"] == WORKER else ""
            stopped = [self.tasks.find(on)] if on else []
            if row["status"] in {"queued", "starting"} and row["role"] == WORKER:
                stopped += [
                    t for t in self.tasks.for_session(session_id) if t["status"] == "queued"
                ]
            for task in {t["id"]: t for t in stopped if t}.values():
                if task["status"] in {"queued", "running"}:
                    self.store.set(f"stopped:{task['id']}", by)
                    self.store.delete(f"goingon:{task['id']}")
                    self.tasks.move(task["id"], "waiting", progress=f"Stopped by {by}.")
                    self.runtime.task_actions.publish(self.tasks.get(task["id"]))
        if session := self.states.handle(session_id):
            await session.abort()
            # A command its turn left running would go on (and end in a turn of its own).
            await stop_background(session)
        self.states[session_id].busy = False
        self.states[session_id].steps.clear()
        if row["role"] == WORKER:
            # What it was sent during the turn and never saw (Copilot drops it with the turn),
            # and its task given and not received: for its next message, not a turn now. Events
            # held for the Router or the Slack session still go out when it is idle.
            self.delivery.unsee(session_id)
            self.delivery.defer_pending(session_id)
        if stuck := self.states[session_id].awaiting:
            # It never started on what it was sent (Copilot may have given up on its queue, on
            # settings it could not load): Go on reaches it on a new connection, with the
            # settings as they are then.
            if row["role"] != WORKER:
                mark = int(self.store.get(f"sent:{session_id}") or 0)
                self.store.set(f"sent:{session_id}", str(min(mark, stuck[1])))
            await self.runtime.detach(session_id)
            self.states[session_id].after_abort = False  # Nothing runs on a new connection.
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
        self.tasks.move(task["id"], "done", result=ADOPTED)
        self.external_cache = (float("-inf"), [])
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
            await self.sync_usage(row)

    async def sync_all_usage(self):
        """Once a start: what workers used outside Chief of Staff while it did not look."""
        for row in self.store.all("SELECT * FROM sessions WHERE role=?", (WORKER,)):
            await self.sync_usage(row)

    async def sync_usage(self, row: dict):
        """Usage in a worker's log that Chief of Staff did not see come by: the turns it took
        in a terminal since it was managed. Turns it drove are in the log too, and already
        counted: the same model and token counts are skipped."""
        backend = self.runtime.backend
        if not backend or self.states.handle(row["id"]):
            return  # Attached: its usage arrives as Copilot events.
        key = f"usage_log:{row['id']}"
        start = int(self.store.get(key) or 0)
        try:
            records, end = await backend.read_usage(self.runtime.copilot_id(row["id"]), start)
        except Exception:
            logger.warning("Could not read the usage of %s.", row["id"], exc_info=True)
            return
        since = timestamp(row["created_at"])  # Before it was managed it was not ours.
        for event in records:
            when = moment(str(event.get("timestamp") or "")) or time.time()
            if when < since:
                continue
            usage = (event.get("data") or {}).get("usage") or {}
            model = str(usage.get("model") or "")
            tokens = (int(usage.get("inputTokens") or 0), int(usage.get("outputTokens") or 0))
            if self.store.one(
                """SELECT id FROM usage WHERE session_id=? AND model=? AND input_tokens=?
                AND output_tokens=? LIMIT 1""",
                (row["id"], model, *tokens),
            ):
                continue
            nano = (usage.get("copilotUsage") or {}).get("totalNanoAiu") or 0
            self.store.record_usage(
                row["id"],
                WORKER,
                model,
                nano / 1e9,
                float(usage.get("cost") or 0),
                *tokens,
                at=when,
            )
        self.store.set(key, str(end))

    async def mirror_loop(self):
        rounds = 0
        while True:
            await asyncio.sleep(MIRROR_SECONDS)
            try:
                if not self.usage_synced and self.runtime.backend:
                    self.usage_synced = True
                    await self.sync_all_usage()
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
        await self.sync_usage(row)
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

    async def release_idle(self, moment: float | None = None) -> list[str]:
        """Workers quiet for a while let go of their Copilot session: the MCP servers it
        started and its memory in Copilot are freed. Their next message resumes it as it was.
        Not one in a turn (or waiting in one for the owner), nor one with a command still
        running in its shells (a server it started, say), nor one open in a terminal."""
        moment = time.time() if moment is None else moment
        released = []
        for row in self.store.open_sessions(WORKER):
            state = self.states[row["id"]]
            if (
                not self.states.is_live(row["id"])
                or state.busy
                or row["status"] in {"working", "starting", "queued"}
                or self.runtime.special(row)
                or self.runtime.copilot_id(row["id"]) in self.runtime.elsewhere
                or moment - timestamp(row["updated_at"]) < RELEASE_IDLE
                or self.store.one(
                    "SELECT id FROM commands WHERE session_id=? AND ended_at IS NULL LIMIT 1",
                    (row["id"],),
                )
            ):
                continue
            await self.runtime.detach(row["id"])
            released.append(row["id"])
        if released:
            logger.info("Let go of %d quiet workers' Copilot sessions.", len(released))
        return released

    async def tidy(self, moment: float | None = None) -> list[str]:
        """Close finished workers that follow nothing and have been quiet for a day; not one
        with something under the owner's Needs you (a result to review, news, a question, a
        failed task, a card): it stays in the sidebar and the @ list until that is settled."""
        from .digest import needs

        moment = time.time() if moment is None else moment
        idle = self.store.settings().worker_idle_hours * 3600
        closed = []
        waiting: set[str] | None = None
        for row in self.store.open_sessions(WORKER):
            if self.runtime.copilot_id(row["id"]) in self.runtime.elsewhere:
                continue  # Someone is using it in a terminal.
            quiet = moment - timestamp(row["updated_at"]) >= idle
            if row["status"] in {"done", "failed"} and quiet and not self.store.follows(row["id"]):
                if waiting is None:
                    waiting = needs(self.runtime).sessions()
                if row["id"] in waiting:
                    continue
                await self.close(row["id"])
                closed.append(row["id"])
        return closed


def memory_note(free: float) -> str:
    return f"{MEMORY_WAIT}: {free:.0f}% free on this computer"
