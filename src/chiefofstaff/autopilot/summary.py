"""*While you were away* (autopilot.md §2.4): one card at the end of the Router's chat when a run
ends, stored with its message (the same in every tab), drawn by the daily summary's component.

What it did and left, in a few groups (P1: *Done*, *Decided for you*, *Waiting for you*), one line
each with one action, under each answer or acceptance one quiet *then:* line with what its
worker did next; the credits it spent; one line on how workers ran (asking first or as the owner
usually lets them). A daily summary or a Focus summary due during the run is folded into it:
never two cards for the same hours. Nothing happened and nothing waits: one quiet line."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from . import grammar

if TYPE_CHECKING:
    from .core import Autopilot

# Each group: its kinds, its name, and each row's action.
GROUPS = (
    ("Done", ("accepted", "released", "nudged")),
    ("Decided for you", ("answered", "decided")),
    ("Waiting for you", ("parked", "not-done")),
)
ACTION = {"accepted": "Undo", "decided": "Change", "parked": "Answer", "not-done": "Review"}
# Rows a group shows before *View all*.
SHOWN = 3


def window(started: str | None, ended: str | None) -> str:
    """2:10 to 6:00 PM, as "2:10 PM \u2013 6:00 PM" (with the day when they differ)."""

    def at(value: str | None) -> datetime | None:
        try:
            return datetime.fromisoformat(value).astimezone() if value else None
        except ValueError:
            return None

    start, end = at(started), at(ended) or datetime.now().astimezone()
    if start is None:
        return ""

    def shown(moment: datetime) -> str:
        half = "AM" if moment.hour < 12 else "PM"
        return f"{moment.hour % 12 or 12}:{moment.minute:02d} {half}"

    if start.date() == end.date():
        return f"{shown(start)} \u2013 {shown(end)}"
    return f"{start.strftime('%a')} {shown(start)} \u2013 {end.strftime('%a')} {shown(end)}"


def still_waits(autopilot: Autopilot, entry: dict) -> bool:
    """A parked item still waits for the owner (not answered or settled since)."""
    store = autopilot.store
    kind, _, ref = (entry.get("subject") or "").partition(":")
    if kind == "card":
        draft = store.one("SELECT session_id, kind, status FROM drafts WHERE id=?", (ref,))
        if not draft:
            return False
        if draft["kind"] == "decision":
            return draft["status"] == "pending"
        # A question card answered for its turn: it waits as a question under Needs you.
        return bool(store.get(f"ownerq:{draft['session_id']}:{entry.get('task_id') or ''}"))
    if kind == "question":
        return bool(store.get(f"ownerq:{ref}"))
    if kind == "task":
        found = store.one("SELECT status FROM tasks WHERE id=?", (ref,))
        return (found or {}).get("status") == "review"
    return True


def card(autopilot: Autopilot, run: dict) -> tuple[str, dict[str, Any]]:
    """The card's text (what the Router and notifications read) and its parts (the page's)."""
    book, store = autopilot.book, autopilot.store
    entries = book.entries(run_id=run.get("id", ""), limit=100000)
    since = run.get("summarized") or ""
    shown = [entry for entry in entries if not since or entry["at"] >= since]
    later = {}
    for entry in entries:
        if entry["kind"] == "after" and entry["of"]:
            later.setdefault(entry["of"], []).append(entry["title"].removeprefix("then: "))
    groups = []
    counts = {"done": 0, "left": 0}
    for name, kinds in GROUPS:
        rows = []
        for entry in shown:
            if entry["kind"] not in kinds:
                continue
            if name == "Waiting for you" and not still_waits(autopilot, entry):
                continue
            undone = bool(book.entries(of=entry["id"], kinds=("undone",), limit=1))
            rows.append({
                "entry": entry["id"], "text": entry["title"], "kind": entry["kind"],
                "action": "" if undone else ACTION.get(entry["kind"], "Open"),
                "then": "; ".join(later.get(entry["id"], [])[-2:]),
                "undone": undone, "task_id": entry["task_id"], "subject": entry["subject"],
            })  # fmt: skip
        if rows:
            groups.append({"name": name, "rows": rows, "more": max(0, len(rows) - SHOWN)})
            counts["left" if name == "Waiting for you" else "done"] += len(rows)
    rules = run.get("rules") or {}
    yolo = store.settings().worker_yolo
    usual = "act without asking" if yolo else "ask first"
    settings_line = (
        f"Workers asked first while Autopilot was on (your usual setting: {usual})."
        if rules.get("ask_first", True) and yolo
        else f"Workers kept your usual settings: {usual}."
    )
    folded = [line for line in (store.get(f"autopilot:folded:{run.get('id', '')}") or "").split(
        "\n\n") if line]  # fmt: skip
    spent = round(autopilot.spent_today(), 1)
    cap = store.settings().autopilot_credits
    moment = datetime.now().astimezone()
    ended = run.get("ended_at")
    when = window(run.get("started_at"), ended)
    if not counts["done"] and not counts["left"]:
        end = datetime.fromisoformat(ended).astimezone() if ended else moment
        text = f"Autopilot ended at {grammar.clock(end, moment, 'en')}: nothing came up."
    else:
        text = (
            f"Autopilot did {counts['done']} thing{'s' if counts['done'] != 1 else ''} and left "
            f"{counts['left']} for you. {spent:g} of {cap:g} credits."
        )
    lines = [f"While you were away · {when}", text]
    if counts["done"] or counts["left"]:
        lines.append(settings_line)
    for group in groups:
        lines.append(group["name"])
        lines += [f"{'•' if group['name'] == 'Waiting for you' else '✓'} {row['text']}"
                  for row in group["rows"][:SHOWN]]  # fmt: skip
    lines += folded
    parts = {
        "run": run.get("id"), "window": when, "started_at": run.get("started_at"),
        "ended_at": ended, "ended_by": run.get("ended_by"), "said": text,
        "settings": settings_line if counts["done"] or counts["left"] else "",
        "groups": groups, "folded": folded, "credits": {"spent": spent, "cap": cap},
        "quiet": not counts["done"] and not counts["left"],
    }  # fmt: skip
    return (text if parts["quiet"] and not folded else "\n".join(lines)), parts
