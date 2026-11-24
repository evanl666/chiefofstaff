"""Autopilot before a downgrade (`chiefofstaff --prepare-downgrade`, with Chief of Staff not
running): the run ends, with its `stopping` entry; every Autopilot prompt stored for a session
and not yet transmitted is withdrawn (its prompt removed, so no version replays it), and the
task it was for is left stopped by the owner, with Go on; items claimed and unfinished are
unknown. The record, its decisions and its kv stay as they are, for upgrading back; an older
version reads none of them. Upgrading back revives nothing."""

from __future__ import annotations

from ..store import Store, now
from .logbook import CLAIMED, ENDED, ON, STOPPING, UNKNOWN, WITHDRAWN, Logbook

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
    for key in store.keys("autopilot:origin:"):
        store.delete(key)
    if titles := leave_stopped(store, tasks):
        named = ", ".join(f"“{title}”" for title in titles)
        said.append(
            f"Withdrew Autopilot's answers not delivered yet: {len(titles)} task(s) wait for you "
            f"with Go on: {named}."
        )
    book.keep_head()
    return said
