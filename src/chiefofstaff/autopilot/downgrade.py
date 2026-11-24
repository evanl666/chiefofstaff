"""Autopilot before a downgrade (`chiefofstaff --prepare-downgrade`, with Chief of Staff not
running): an older version knows nothing of Autopilot's origin, so nothing Autopilot continued
may go on there.

- The run ends, with its `stopping` entry; items claimed and unfinished are unknown.
- Every Autopilot prompt stored and not yet transmitted is withdrawn (its prompt removed, so no
  version replays it), and the task it was for is left stopped by the owner, with Go on.
- **Every session that carries Autopilot's origin** (a worker it continued, transmitted
  answers too; the Router in a turn that carried it) is drained: at work, it is left idle; its
  open tasks stopped by the owner (Go on); what it was sent and not yet received waits for its
  next message from the owner (deferred: no restart replays it); its check-backs and its own
  schedules (Claude's) wake nothing. Processes of other agents end as `agents.prepare_downgrade`
  ends them; a Copilot worker's ends with Chief of Staff.

The record, its decisions and its kv stay as they are, for upgrading back; an older version
reads none of them. Upgrading back revives nothing."""

from __future__ import annotations

import json

from ..store import Store, now
from .logbook import CLAIMED, ENDED, ON, STOPPING, UNKNOWN, WITHDRAWN, Logbook
from .origin import STICKY, TURN

WHY = "before a downgrade"


def withdraw_unsent(store: Store, book: Logbook, why: str) -> list[str]:
    """Autopilot's prompts stored and not yet transmitted (after their session's `sent:` mark):
    their prompts removed, each with a `withdrawn` entry. Returns the tasks they were for."""
    tasks: list[str] = []
    rows = store.all(
        "SELECT id, entry, run_id, message_id FROM autopilot_outbox WHERE message_id IS NOT NULL "
        "AND state!=? ORDER BY created_at, rowid",
        (WITHDRAWN,),
    )
    for item in rows:
        message = store.one("SELECT * FROM messages WHERE id=?", (item["message_id"],))
        if not message:
            continue
        meta = store.message(message["id"])["meta"]
        sent = int(store.get(f"sent:{message['session_id']}") or 0)
        if "prompt" not in meta or message["id"] <= sent:
            continue
        with store.transaction():
            store.update_message_meta(
                message["id"],
                {**{k: v for k, v in meta.items() if k != "prompt"}, "withdrawn": True},
            )
            store.execute(
                "UPDATE autopilot_outbox SET state=?, outcome=?, ended_at=? WHERE id=?",
                (WITHDRAWN, f"Withdrawn {why}.", now(), item["id"]),
            )
            action = book.entry(item["entry"]) or {}
            book.append(
                item["run_id"],
                WITHDRAWN,
                f"Withdrawn {why}: {action.get('title') or 'a message'}",
                state=WITHDRAWN,
                of=item["entry"],
                subject=action.get("subject") or "",
                task_id=action.get("task_id") or "",
                why={"rule": "stopped", "reason": f"Not delivered yet, {why}."},
                payload={"item": item["id"], "message": message["id"]},
            )
        if task_id := (meta.get("task_id") or action.get("task_id") or ""):
            tasks.append(task_id)
    return list(dict.fromkeys(tasks))


def leave_stopped(store: Store, task_ids: list[str]) -> list[str]:
    """Tasks left as the owner's Stop leaves them (waiting, with Go on), not idle: an older
    version starts no turn for them. Returns their titles."""
    titles = []
    for task_id in task_ids:
        task = store.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task or task["status"] not in {"queued", "running", "waiting"}:
            continue
        said = "Stopped by you."
        with store.transaction():
            store.set(f"stopped:{task_id}", "you")
            store.delete(f"goingon:{task_id}")
            store.execute(
                "UPDATE tasks SET status='waiting', progress=?, updated_at=? WHERE id=?",
                (said, now(), task_id),
            )
            row = store.find_session(task["session_id"])
            if row and row["status"] in {"working", "queued", "starting"}:
                store.update_session(row["id"], status="idle", activity="")
            if row:
                store.add_message(
                    row["id"],
                    "notice",
                    f"{said} Autopilot's answer was withdrawn {WHY}.",
                    {"stopped": True, "task_id": task_id},
                )
        titles.append(task["title"])
    return titles


def drain(store: Store, book: Logbook) -> list[str]:
    """Every session carrying Autopilot's origin left as the owner's Stop leaves it, nothing of
    it to wake or replay. Returns their titles."""
    carried: dict[str, dict] = {}
    for prefix in (STICKY, TURN):
        for session_id, value in store.items(prefix).items():
            try:
                carried[session_id] = json.loads(value)
            except ValueError:
                carried[session_id] = {}
    titles = []
    for session_id, origin in carried.items():
        row = store.find_session(session_id)
        if not row or row["closed_at"]:
            continue
        open_tasks = store.all(
            "SELECT id, status FROM tasks WHERE session_id=? AND status IN "
            "('queued', 'running', 'waiting')",
            (session_id,),
        )
        at_work = row["status"] in {"working", "queued", "starting"}
        with store.transaction():
            if at_work:
                store.update_session(session_id, status="idle", activity="")
            mark = int(store.get(f"sent:{session_id}") or 0)
            for message in store.prompts(session_id, after=mark):
                if not message["meta"].get("deferred"):
                    store.update_message_meta(message["id"], {**message["meta"], "deferred": True})
            for task in store.all("SELECT id FROM tasks WHERE session_id=?", (session_id,)):
                store.delete(f"checkback:{task['id']}")
            if store.get(f"agentschedules:{session_id}"):
                store.set(f"scheduleshold:{session_id}", "1")
            first = book.entry((origin.get("entries") or [""])[0]) or {}
            book.append(
                first.get("run_id") or "", STOPPING, f"Stopped “{row['title']}” {WHY}",
                of=first.get("id") or "", subject=f"session:{session_id}",
                why={"rule": "stopped", "by": "downgrade", "at_work": at_work},
                payload={"entries": origin.get("entries") or []},
            )  # fmt: skip
        if row["role"] == "worker":
            leave_stopped(store, [task["id"] for task in open_tasks])
            if at_work and not open_tasks:
                store.add_message(session_id, "notice", f"Stopped by you, {WHY}.",
                                  {"stopped": True})  # fmt: skip
        titles.append(row["title"])
    for prefix in (STICKY, TURN):
        for key in store.keys(prefix):
            store.delete(key)
    return titles


def prepare_downgrade(store: Store) -> list[str]:
    """What it did, one line each (nothing when Autopilot was never used)."""
    book = Logbook(store)
    if not book.exists():
        return []
    said = []
    for run in book.open_runs():
        with store.transaction():
            book.move_run(run["id"], (ON, STOPPING), state=STOPPING)
            book.append(
                run["id"],
                STOPPING,
                f"Stopped: {WHY}",
                subject=f"run:{run['id']}",
                why={"rule": "stopped", "by": "downgrade"},
            )
            book.bump()
            book.withdraw(f"Stopped {WHY}.", run["id"])
            book.move_run(run["id"], STOPPING, state=ENDED, ended_at=now(), ended_by="downgrade")
            book.append(
                run["id"], "ended", "Ended", subject=f"run:{run['id']}",
                why={"rule": "stopped", "by": "downgrade"},
            )  # fmt: skip
        said.append("Stopped Autopilot.")
    tasks = withdraw_unsent(store, book, WHY)
    for item in book.outbox(CLAIMED):
        book.finish(item["id"], UNKNOWN, f"Chief of Staff stopped while delivering it, {WHY}.")
    if titles := leave_stopped(store, tasks):
        named = ", ".join(f"“{title}”" for title in titles)
        said.append(
            f"Withdrew Autopilot's answers not delivered yet: {len(titles)} task(s) wait for you "
            f"with Go on: {named}."
        )
    if drained := [title for title in drain(store, book) if title not in titles]:
        named = ", ".join(f"“{title}”" for title in drained)
        said.append(f"Stopped what Autopilot had continued: {named}; each waits for you.")
    book.keep_head()
    return said
