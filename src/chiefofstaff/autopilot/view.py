"""The Autopilot record as the page reads it (autopilot.md §10.4): every run, newest first, each
opening to its actions, one line each, unfolding to what, why, the evidence, exactly what, the
outcome and what its worker did next, with its one action. Opening it checks the chain."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .core import ACTIONS
from .logbook import PARKED, UNDONE
from .summary import ACTION, still_waits

if TYPE_CHECKING:
    from .core import Autopilot

# Shown as actions (one line each); the rest are states of them, or of the run.
SHOWN = ("answered", "decided", "accepted", "released", "nudged", PARKED, "not-done",
         "stopping", "handed-back")  # fmt: skip
STATES = {"withdrawn": "withdrawn", "undone": "undone", "unknown": "not known whether it went "
          "out", "done": "done", "failed": "failed"}  # fmt: skip


def runs(autopilot: Autopilot, limit: int = 50) -> dict[str, Any]:
    """Every run, newest first, with its counts; and whether the record holds."""
    book = autopilot.book
    chain = book.check()
    found = []
    for run in book.runs(limit):
        entries = book.entries(run_id=run["id"], limit=100000)
        found.append({
            "id": run["id"], "started_at": run["started_at"], "until": run["until"],
            "ended_at": run["ended_at"], "ended_by": run["ended_by"], "state": run["state"],
            "actions": sum(1 for e in entries if e["kind"] in ACTIONS),
            "left": sum(1 for e in entries if e["kind"] in {PARKED, "not-done"}
                        and still_waits(autopilot, e)),
        })  # fmt: skip
    return {
        "runs": found,
        "chain": {"ok": chain.ok, "after": chain.after, "seq": chain.seq},
        "ask_first": autopilot.store.settings().autopilot_ask_first,
    }


def actions(autopilot: Autopilot, run_id: str) -> list[dict]:
    """A run's actions, newest first, each with its later states and what came next."""
    book = autopilot.book
    entries = book.entries(run_id=run_id, limit=100000)
    chain = book.check()
    broken = chain.seq if not chain.ok else 0
    later: dict[str, list[dict]] = {}
    for entry in entries:
        if entry["of"]:
            later.setdefault(entry["of"], []).append(entry)
    shown = []
    for entry in reversed(entries):
        if entry["kind"] not in SHOWN:
            continue
        after = [e for e in later.get(entry["id"], []) if e["kind"] == "after"]
        states = [e for e in later.get(entry["id"], []) if e["kind"] in STATES]
        undone = any(e["kind"] == UNDONE for e in states)
        action = "" if undone else ACTION.get(entry["kind"], "")
        if entry["kind"] in {PARKED, "not-done"} and not still_waits(autopilot, entry):
            action = ""
        shown.append({
            "id": entry["id"], "at": entry["at"], "kind": entry["kind"], "text": entry["title"],
            "task_id": entry["task_id"], "subject": entry["subject"], "action": action,
            "state": STATES.get(states[-1]["kind"], "") if states else "",
            "then": [e["title"].removeprefix("then: ") for e in after[-3:]],
            "unverified": bool(broken and entry["seq"] >= broken),
        })  # fmt: skip
    return shown


def entry(autopilot: Autopilot, entry_id: str) -> dict:
    """One entry unfolded: why, evidence and exactly what, whole (its blobs read), and later
    entries about it."""
    book = autopilot.book
    found = book.entry(entry_id)
    if not found:
        raise LookupError("No such entry in the Autopilot record.")
    return {
        "id": found["id"], "run_id": found["run_id"], "at": found["at"], "kind": found["kind"],
        "text": found["title"], "task_id": found["task_id"], "subject": found["subject"],
        "why": found["why"],
        "evidence": book.whole(found["evidence"]), "payload": book.whole(found["payload"]),
        "later": [{"at": e["at"], "kind": e["kind"], "text": e["title"],
                   "evidence": book.whole(e["evidence"])}
                  for e in book.entries(of=entry_id, limit=200)],
    }  # fmt: skip


def markdown(autopilot: Autopilot, run_id: str) -> str:
    """*Copy as Markdown*: a run, each action with what it rested on."""
    run = autopilot.book.run(run_id)
    if not run:
        raise LookupError("No such run.")
    lines = [f"# Autopilot · {run['started_at']} to {run['ended_at'] or 'now'}", ""]
    for item in reversed(actions(autopilot, run_id)):
        full = entry(autopilot, item["id"])
        lines.append(f"- {item['at']} · {item['text']}")
        for cite in (full["why"] or {}).get("basis") or []:
            lines.append(f"  - because you said “{cite.get('quote', '')}” ({cite.get('id', '')})")
        if reason := (full["why"] or {}).get("reason"):
            lines.append(f"  - why: {reason}")
        lines += [f"  - then: {then}" for then in item["then"]]
    return "\n".join(lines) + "\n"
