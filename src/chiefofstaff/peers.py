"""Workers that work together pass information straight to each other.

Some tasks run at the same time and need each other while they work: two sides of one change
(an API and its client), a value one of them finds that the other uses, a finding that changes
how the other goes about its task. The Router says which (`works_with` on spawn_worker), and
each is told about the others. They tell each other what the other needs (`tell_session`) or
ask (`ask_session`) without a Router turn in between: the exact words, at once, at no cost to
the Router's context.

The Router still knows who talks to whom: its next turn gets a digest of it (it does not start
one; teams.py), never the words, and Home shows each message in a line, a run of them folded.
Information is not work: what a worker should do differently than its task says still goes
through the Router, which decides (and asks the owner when it is theirs). When one of them
finishes, the others still at it get its result.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from .envelope import sender, short, wrap
from .roles import WORKER

if TYPE_CHECKING:
    from .runtime import Runtime

# A worker tells one other worker at most this many things an hour: more is a conversation
# the Router should see and steer.
MAX_TELLS_PER_HOUR = 12
OPEN = {"queued", "running", "waiting"}
ROUTER_COPY_CHARS = 600
RESULT_CHARS = 3000


class TellSession(BaseModel):
    session_id: str = Field(
        description="The worker to tell: its short id (worker:<id> in a message's from, "
        "find_sessions, or the workers you were told you work with)"
    )
    text: str = Field(
        description="What it needs to know, whole and exact (values, names, paths, what "
        "changed and where): it reads only this"
    )


class Peers:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.tasks = runtime.tasks
        self.delivery = runtime.delivery

    def of(self, session_id: str) -> list[str]:
        """The workers it works with (ids), open or not."""
        return json.loads(self.store.get(f"peers:{session_id}") or "[]")

    def open(self, session_id: str) -> list[dict]:
        """The workers it works with that are open, each with its current task."""
        rows = [self.store.find_session(other) for other in self.of(session_id)]
        return [
            {**row, "task_row": self.tasks.current(row["id"])}
            for row in rows
            if row and not row["closed_at"]
        ]

    def link(self, session_id: str, others: list[str]):
        for other in others:
            if other == session_id:
                continue
            for one, two in ((session_id, other), (other, session_id)):
                known = self.of(one)
                if two not in known:
                    self.store.set(f"peers:{one}", json.dumps([*known, two]))

    def talked(self, one: str, other: str):
        """Two workers that told or asked each other something: their team's (teams.py), also
        when they were not started to work together. Nothing else follows from it (not their
        results, as for the ones they work with)."""
        for first, second in ((one, other), (other, one)):
            known = json.loads(self.store.get(f"talks:{first}") or "[]")
            if second not in known:
                self.store.set(f"talks:{first}", json.dumps([*known, second]))

    def resolve(self, references: list[str]) -> tuple[list[dict], list[str]]:
        """Workers named by a task id, a worker id or its short id; and the titles of the other
        references, for tasks started in the same turn that may not exist yet."""
        rows, titles = [], []
        for reference in references:
            reference = " ".join(reference.split()).removeprefix("worker:")
            if not reference:
                continue
            try:
                task = self.runtime.task_actions.task_for(reference)
            except ValueError:
                titles.append(reference)
                continue
            row = self.store.session(task["session_id"])
            if row["role"] != WORKER:
                raise ValueError(f"{reference} is not a worker's task.")
            rows.append(row)
        return list({row["id"]: row for row in rows}.values()), titles

    def candidates(
        self, title: str, goal: str, slack: bool, wanted: list[str], new: str = ""
    ) -> list[dict]:
        """Workers at work that a new task works with: those whose title it names in
        works_with, those that name its title so, and (said or not) those whose task names it
        by title or that it names. Not when both only carry the same message of the owner's.
        Run once the new task exists, the later of two tasks started at once finds the other."""
        found = []
        for row in self.store.open_sessions(WORKER):
            task = self.tasks.current(row["id"])
            if (
                row["id"] == new
                or self.runtime.special(row)
                or bool(row["slack"]) != slack
                or not task
                or task["status"] not in OPEN
            ):
                continue
            theirs = json.loads(self.store.get(f"wants:{row['id']}") or "[]")
            same_message = " ".join(task["goal"].split()) == " ".join(goal.split())
            if (
                any(same(name, task["title"]) for name in wanted)
                or any(same(name, title) for name in theirs)
                or (
                    not same_message
                    and (mentions(goal, task["title"]) or mentions(task["goal"], title))
                )
            ):
                found.append(row)
        return found

    def want(self, session_id: str, titles: list[str]):
        """Titles it works with that may start later in the same turn."""
        if titles:
            self.store.set(f"wants:{session_id}", json.dumps(titles))

    @staticmethod
    def line(row: dict, task: dict | None) -> str:
        what = f", on task {task['id']} “{task['title']}” ({task['status']})" if task else ""
        return f"- “{row['title']}” (worker:{short(row['id'])}){what}"

    def waits_on(self, session_id: str) -> set[str]:
        """The tasks its task waits on (after): those of a worker it follows, not works beside."""
        task = self.tasks.current(session_id)
        return {dependency["id"] for dependency in self.tasks.deps(task["id"])} if task else set()

    def introduction(self, session_id: str) -> str:
        """For a new worker's first message: who it works with, and how: beside it at the same
        time, or before it (its task waits on theirs: it starts once they are done)."""
        peers = self.open(session_id)
        waits = self.waits_on(session_id)
        before = [row for row in peers if row["task_row"] and row["task_row"]["id"] in waits]
        beside = [row for row in peers if row not in before]
        said = []
        if beside:
            lines = "\n".join(self.line(row, row["task_row"]) for row in beside)
            said.append(
                f"You work at the same time as these workers, on parts of the same request:\n"
                f"{lines}\nTell them what they need from your work as soon as you have it (a "
                "value, a name, a changed interface or file format, a finding that affects them) "
                "with tell_session; ask them with ask_session. What they tell you arrives as "
                'from="worker:…" type="note". When your task waits for something from one of '
                "them, end your turn without a report: their message starts your next turn."
            )
        if before:
            lines = "\n".join(self.line(row, row["task_row"]) for row in before)
            said.append(
                f"You start once these workers are done (your task waits on theirs), and their "
                f"results reach you then:\n{lines}\nIf you need more from one of them than its "
                "result, ask it with ask_session; what one tells you arrives as "
                'from="worker:…" type="note".'
            )
        return "\n\n".join(said)

    async def introduce(self, new: dict, others: list[dict]):
        """Each worker it works with hears of the new one: one that works beside it, or one
        that starts once it is done (its task waits on that worker's)."""
        task = self.tasks.current(new["id"])
        waits = self.waits_on(new["id"])
        for row in others:
            current = self.tasks.current(row["id"])
            if current and current["id"] in waits:
                text = (
                    f"“{new['title']}” (worker:{short(new['id'])}) starts once you're done with "
                    f"“{current['title']}”: its task"
                    + (f" ({task['title']})" if task else "")
                    + " waits on yours, and your result reaches it then. If it needs more from "
                    "your work than your result, tell it with tell_session."
                )
            else:
                text = (
                    f"“{new['title']}” (worker:{short(new['id'])}) now works at the same time as "
                    f"you, on another part of the same request"
                    + (f": {task['title']}" if task else "")
                    + ". Tell it what it needs from your work with tell_session (as soon as you "
                    "have it); it tells you what you need."
                )
            prompt = wrap("system", "peer", text, worker=short(new["id"]))
            meta = {"peer": new["id"], "title": new["title"], "session_id": new["id"]}
            if current and current["status"] in OPEN:
                await self.delivery.deliver(
                    row["id"], prompt, kind="notice", display=text, meta=meta, steer=True
                )
            else:
                self.delivery.hold(row["id"], prompt, kind="notice", display=text, meta=meta)

    def waiting_on(self, session_id: str, words: str) -> list[dict]:
        """The workers still at their task that it says it waits for, by title. One it does
        not work with yet becomes one it works with: its result then reaches this one."""
        row = self.store.find_session(session_id)
        if not row:
            return []
        named = [
            other
            for other in self.store.open_sessions(WORKER)
            if other["id"] != session_id
            and not self.runtime.special(other)
            and bool(other["slack"]) == bool(row["slack"])
            and (
                mentions(words, other["title"], by_name=True)
                or mentions(words, self.runtime.display_title(other), by_name=True)
                or re.search(rf"(?<![\w-]){other['id'][:8]}(?![\w-])", words or "") is not None
            )
            and (task := self.tasks.current(other["id"]))
            and task["status"] in OPEN
        ]
        self.link(session_id, [other["id"] for other in named])
        return named

    def waiting(self, session_id: str) -> list[dict]:
        """Workers it works with that are still at their task (it may wait for them)."""
        return [
            row
            for row in self.open(session_id)
            if row["task_row"] and row["task_row"]["status"] in OPEN
        ]

    def tells(self, caller: str, target: str) -> list[float]:
        hour = time.time() - 3600
        return [
            t for t in json.loads(self.store.get(f"told:{caller}:{target}") or "[]") if t > hour
        ]

    def copy_to_router(self, text: str, meta: dict):
        """A line in the Router's chat (Home folds a run of them). Not given to the Router: its
        next turn gets a digest of who told or asked whom instead, never the words (Teams.digest,
        E129)."""
        router = self.runtime.router_id()
        if router:
            self.runtime.add_message(router, "peer", text, meta)

    async def t_tell_session(self, params: TellSession, caller: str):
        teller = self.runtime.toolset.caller(caller, WORKER)
        try:
            target = self.store.session(params.session_id.removeprefix("worker:"))
        except LookupError:
            raise ValueError("No such worker; use find_sessions.") from None
        if target["id"] == caller:
            raise ValueError("That is you.")
        if target["role"] != WORKER:
            raise ValueError(
                "tell_session is for other workers; report to the Router, which tells the owner."
            )
        if bool(target["slack"]) != bool(teller["slack"]):
            raise ValueError("That worker's Slack access differs from yours; report to the Router.")
        if target["closed_at"]:
            raise ValueError(
                "That worker is closed. If its finished work must change, report it to the "
                "Router (status question)."
            )
        text = params.text.strip()[:6000]
        if not text:
            raise ValueError("The message is empty.")
        told = self.tells(caller, target["id"])
        if len(told) >= MAX_TELLS_PER_HOUR:
            raise ValueError(
                "You told that worker a lot this hour: report to the Router what needs "
                "coordinating, and it decides."
            )
        self.store.set(f"told:{caller}:{target['id']}", json.dumps([*told, time.time()]))
        # Which of their tasks this goes between (Teams' history), as they are now.
        stamped = self.runtime.teams_lite.exchange_tasks(caller, target["id"])
        task = self.tasks.current(caller)
        prompt = wrap(
            sender(teller), "note", text, title=teller["title"], task=task["id"] if task else ""
        )
        meta = {"told_by": caller, "title": teller["title"]}
        current = self.tasks.current(target["id"])
        if current and current["status"] in OPEN:
            await self.delivery.deliver(
                target["id"], prompt, kind="inbound", display=text, meta=meta, steer=True
            )
            self.runtime.forget_go_on_at(target["id"])  # Told something else: not by itself.
            outcome = f"Told “{target['title']}”."
        else:
            # Finished: it reads this with its next message, and does not start on its own.
            self.delivery.hold(target["id"], prompt, kind="inbound", display=text, meta=meta)
            outcome = (
                f"“{target['title']}” has finished its task; it reads this with its next "
                "message. If its finished work must change, report it to the Router (status "
                "question)."
            )
        row = self.runtime.add_message(
            caller, "tell", text, {"target": target["id"], "title": target["title"], **stamped}
        )
        self.runtime.teams_lite.exchanged(row["id"], stamped)
        self.talked(caller, target["id"])
        self.copy_to_router(
            f"“{self.runtime.display_title(teller)}” told "
            f"“{self.runtime.display_title(target)}”: {clip(text, ROUTER_COPY_CHARS)}",
            {"peer_from": caller, "peer_to": target["id"], "peer_kind": "told", **stamped},
        )
        return outcome

    async def finished(self, session_id: str, task: dict, summary: str):
        """A worker reported its task done: the workers it works with that are still at theirs
        get its result (unless they wait for it as a dependency, which hands it over)."""
        row = self.store.find_session(session_id)
        if not row:
            return
        for peer in self.waiting(session_id):
            if task["id"] in {dep["id"] for dep in self.tasks.deps(peer["task_row"]["id"])}:
                continue
            text = f"“{row['title']}” finished “{task['title']}”:\n{clip(summary, RESULT_CHARS)}"
            await self.delivery.deliver(
                peer["id"],
                wrap(sender(row), "peer-done", text, title=row["title"], task=task["id"]),
                kind="inbound",
                display=text,
                meta={"told_by": session_id, "title": row["title"], "finished": True},
                steer=True,
            )

    async def stopped(self, task: dict):
        """A worker's task failed or was cancelled: the workers it works with that are still
        at theirs hear that what they may wait for from it will not come."""
        row = self.store.find_session(task["session_id"])
        if not row:
            return
        how = "failed" if task["status"] == "failed" else "was cancelled"
        text = (
            f"“{row['title']}”'s task “{task['title']}” {how}: what you may wait for from it "
            'will not come. If your task needs it, report(status="question") to the Router.'
        )
        for peer in self.waiting(row["id"]):
            await self.delivery.deliver(
                peer["id"],
                wrap("system", "peer-stopped", text, worker=short(row["id"]), task=task["id"]),
                kind="notice",
                display=text,
                meta={"peer": row["id"], "session_id": row["id"]},
                steer=True,
            )

    async def back(self, task: dict):
        """A task they were told would not come is at work again (retried, reopened): each
        worker told so hears it may come after all; one that has not received that yet (held
        for it) never will."""
        row = self.store.find_session(task["session_id"])
        if not row:
            return
        text = (
            f"“{row['title']}”'s task “{task['title']}” is open again (retried or reopened): what "
            "you may wait for from it can come after all."
        )
        for peer in self.open(row["id"]):
            told = self.store.one(
                "SELECT id, meta FROM messages WHERE session_id=? AND kind='notice' "
                "AND json_extract(meta, '$.prompt') LIKE ? ORDER BY id DESC LIMIT 1",
                (peer["id"], f'%type="peer-stopped"%task="{task["id"]}"%'),
            )
            if not told:
                continue
            if told["id"] > int(self.store.get(f"sent:{peer['id']}") or 0):
                # Not received yet: withdrawn, not delivered late.
                meta = json.loads(told["meta"])
                kept = {key: value for key, value in meta.items() if key != "prompt"}
                self.store.update_message_meta(told["id"], {**kept, "dropped": True})
                self.delivery.publish_delivery(peer["id"], [self.store.message(told["id"])])
                continue
            if peer["task_row"] and peer["task_row"]["status"] in OPEN:
                await self.delivery.deliver(
                    peer["id"],
                    wrap("system", "peer-back", text, worker=short(row["id"]), task=task["id"]),
                    kind="notice",
                    display=text,
                    meta={"peer": row["id"], "session_id": row["id"]},
                    steer=True,
                )


def same(name: str, title: str) -> bool:
    return " ".join(name.split()).casefold() == " ".join(title.split()).casefold()


def mentions(text: str, title: str, by_name: bool = False) -> bool:
    """Whether a text names a task by its title (whole words; a title too short to tell a task
    by never counts). `by_name`: also by the name the title starts with ("Token Scout" of
    "Token Scout: generate a token"), as a worker says whom it waits for."""
    title = " ".join(title.split())
    head = re.split(r"\s*[:·|(]\s*|\s+[-\u2013\u2014]\s+", title, maxsplit=1)[0].strip()
    named = by_name and head != title and len(head.split()) >= 2 and len(head) >= 8
    names = [title, head] if named else [title]
    return any(
        len(name) >= 5
        and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text or "", re.IGNORECASE) is not None
        for name in names
    )


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
