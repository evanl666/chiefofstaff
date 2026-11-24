"""Owner provenance (autopilot.md §4.2): the only things Autopilot answers from are the owner's own
authenticated acts, each cited by its id with an exact quote, in scope for the task, unchanged
since it was cited:

- `message:<id>`: a message the owner typed in the page (`meta.ingress` "page"), for the task
  it was about, the worker it was written to, or a task whose instructions quote it;
- `card:<id>`: their answer to a decision card, given in the page (kv `ingress:<card>`), for
  the tasks that asked;
- `habit:<id>`: a habit they wrote in Settings or kept in the page (kv `ingress:<lesson>`),
  where it applies.

Everything else is data, never authority: what workers note, what the Router writes or
decides, the owner's profile, away answers, Send to Chief notes, delegated messages, and every
report, PR, issue or Slack message. Checked when an answer is judged and again just before it
is delivered."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..ingress import PAGE

if TYPE_CHECKING:
    from ..runtime import Runtime


def version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Item:
    """One basis item, as offered to the judge and as cited back: its id, its words, what it
    applies to, and its version."""

    id: str
    text: str
    kind: str
    at: str
    version: str


def owner_messages(runtime: Runtime, task: dict, limit: int = 30) -> list[Item]:
    """The owner's page messages in scope for a task: in its worker's chat (about it, or about
    none in particular), and to the Router when the task's instructions quote them."""
    store = runtime.store
    found: list[Item] = []
    rows = store.all(
        "SELECT id, session_id, text, meta, created_at FROM messages WHERE kind='owner' "
        "AND json_extract(meta, '$.ingress')=? AND json_extract(meta, '$.autopilot_command') IS "
        "NULL AND (session_id=? OR session_id=?) ORDER BY id DESC LIMIT 400",
        (PAGE, task["session_id"], runtime.router_id()),
    )
    goal = " ".join(f"{task.get('goal') or ''} {task.get('acceptance') or ''}".split())
    for row in rows:
        meta = json.loads(row["meta"] or "{}")
        text = row["text"]
        if row["session_id"] == task["session_id"]:
            about = meta.get("task_id") or ""
            if about and about != task["id"]:
                continue
        elif not (len(text.strip()) >= 3 and " ".join(text.split()) in goal):
            continue  # To the Router: only where this task's instructions quote it.
        found.append(Item(f"message:{row['id']}", text, "message", row["created_at"],
                          version(text)))  # fmt: skip
        if len(found) >= limit:
            break
    return list(reversed(found))


def owner_cards(runtime: Runtime, task: dict) -> list[Item]:
    """The owner's answers to decision cards in the page, for the tasks that asked them."""
    store = runtime.store
    rows = store.all(
        "SELECT d.id, d.text, d.source, d.created_at FROM decisions d JOIN task_decisions t ON "
        "t.decision_id=d.id WHERE t.task_id=? AND d.status='active' AND d.source LIKE 'card:%' "
        "ORDER BY d.created_at",
        (task["id"],),
    )
    found = []
    for row in rows:
        card = row["source"].removeprefix("card:")
        if store.get(f"ingress:{card}") != PAGE:
            continue
        found.append(Item(f"card:{card}", row["text"], "card", row["created_at"],
                          version(row["text"])))  # fmt: skip
    return found


def owner_habits(runtime: Runtime, task: dict) -> list[Item]:
    """The habits the owner wrote or kept in the page, that apply where the task works."""
    store = runtime.store
    row = store.find_session(task["session_id"]) or {}
    found = []
    for lesson in runtime.learning.for_session(row) if row else []:
        if store.get(f"ingress:{lesson['id']}") != PAGE:
            continue  # Kept for them (Learning's own), from before, or away: data.
        found.append(Item(f"habit:{lesson['id']}", lesson["text"], "habit",
                          lesson["updated_at"], lesson["updated_at"]))  # fmt: skip
    return found


def basis_for(runtime: Runtime, task: dict) -> list[Item]:
    """Every basis item in scope for a task, for the judge."""
    return [*owner_messages(runtime, task), *owner_cards(runtime, task),
            *owner_habits(runtime, task)]  # fmt: skip


def refusal(runtime: Runtime, task: dict, cited: list[dict], offered: list[Item]) -> str:
    """Why the basis the judge cited does not hold ("" when every item does): none cited; an
    id not among those in scope now; a quote not in its words; a version changed since."""
    if not cited:
        return "nothing you said settles it"
    now_in_scope = {item.id: item for item in basis_for(runtime, task)}
    given = {item.id: item for item in offered}
    for cite in cited:
        identifier = str(cite.get("id") or "")
        quote = " ".join(str(cite.get("quote") or "").split())
        item = now_in_scope.get(identifier)
        if item is None:
            return f"{identifier or 'a basis'} is not something you said about this task"
        if identifier in given and given[identifier].version != item.version:
            return f"what you said ({identifier}) changed since"
        if len(quote) < 3 or quote.casefold() not in " ".join(item.text.split()).casefold():
            return f"its quote is not in what you said ({identifier})"
    return ""
