"""Owner provenance (autopilot.md §4.2): the only things Autopilot answers from are the owner's own
authenticated acts, each cited by its id with an exact quote, in scope for the task, unchanged
since it was cited:

- `message:<id>`: a message the owner typed in the page (`meta.ingress` "page"), for the task
  it was about, the worker it was written to, or a task the Router gave it to by its ref
  (`{owner:<ref>}`, kept as the task was given: kv `autopilot:refs:<task>`);
- `card:<id>`: their answer to a decision card, given in the page (kv `ingress:<card>`), for
  the tasks that asked: their answer's words only (the card's outcome), never its question,
  which is a worker's (given to the judge as data);
- `habit:<id>`: a habit they wrote in Settings or kept in the page (kv `ingress:<lesson>`),
  where it applies.

Everything else is data, never authority: what workers note, what the Router writes or
decides, the owner's profile, away answers, Send to Chief notes, delegated messages, and every
report, PR, issue or Slack message. Checked when an answer is judged and again just before it
is delivered.

A quote is a whole clause of the owner's words, word for word and word-bounded (or their whole
answer to a card), and it says the answer given: "yes" is not in "yesterday", and "use the
cache" is not what "never use the cache" says."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..ingress import PAGE

if TYPE_CHECKING:
    from ..runtime import Runtime


# The task's owner messages to the Router, given to it by their refs ({owner:<ref>}).
REFS = "autopilot:refs:"
# Where a clause starts or ends.
CLAUSE = set(".,;:!?()[]\"\u201c\u201d\u2018\u2019\u2014\u2013\u2026\n"
             "\u3002\uff0c\uff1b\uff1a\uff01\uff1f\u3001\uff08\uff09")  # fmt: skip
LATIN = re.compile(r"[A-Za-z0-9_]+")
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
# What says no (when in doubt, it does), anywhere in the sentence an answer is in, before it or
# after it: whole words, any "n't", and Chinese ones anywhere (不 covers 不要, 不用, 不能, 而不是).
NEGATORS = ("not", "never", "no", "none", "nor", "neither", "without", "avoid", "instead of",
            "rather than", "cannot", "dont", "except", "other than", "against")  # fmt: skip
CONTRACTED = re.compile(r"[a-z]n't\b")
NEGATORS_CJK = ("不", "别", "没", "勿", "避免", "禁止", "无需")
# Where a sentence ends (never a comma, a dash, a quotation mark or a bracket; a full stop only
# before a space or the end, not in "16.2"); and a curly apostrophe within a word (as in
# "don't": never a closing quote that ends a phrase).
SENTENCE_ENDS = set("!?;\n\u3002\uff01\uff1f\uff1b")
APOSTROPHE = re.compile(r"(?<=\w)[\u2019\u02bc](?=\w)")


def version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Item:
    """One basis item, as offered to the judge and as cited back: its id, its words (the owner's
    only), what it applies to, its version; and for a card, what it asked (a worker's words:
    data, never the owner's)."""

    id: str
    text: str
    kind: str
    at: str
    version: str
    about: str = ""


def plain(text: str) -> str:
    return APOSTROPHE.sub("'", " ".join(str(text).split())).casefold()


def joins(edge: str, neighbour: str) -> bool:
    """Whether two characters run on as one word (Latin letters and digits)."""
    return edge.isascii() and edge.isalnum() and neighbour.isascii() and neighbour.isalnum()


def bounded(text: str, part: str) -> list[int]:
    """Where `part` is in `text` as whole words (both plain): a part that starts or ends with a
    letter or digit is not a piece of a longer word ("yes" in "yesterday")."""
    if not part:
        return []
    found, start = [], 0
    while (at := text.find(part, start)) >= 0:
        before = text[at - 1] if at else ""
        after = text[at + len(part) : at + len(part) + 1]
        if not joins(part[0], before) and not joins(part[-1], after):
            found.append(at)
        start = at + 1
    return found


def clause(text: str, at: int, size: int) -> bool:
    """Whether text[at:at + size] starts and ends where a clause does."""
    before = text[:at].rstrip()
    after = text[at + size :].lstrip()
    return (not before or before[-1] in CLAUSE) and (not after or after[0] in CLAUSE)


def weight(text: str) -> float:
    """How much a quote says: its Latin words, and its CJK characters half each."""
    return len(LATIN.findall(text)) + len(CJK.findall(text)) / 2


def says_no(words: str) -> bool:
    if CONTRACTED.search(words) or any(bounded(words, word) for word in NEGATORS):
        return True
    return any(word in words for word in NEGATORS_CJK)


def sentence(text: str, start: int, end: int) -> str:
    """The whole sentence (or sentences) text[start:end] is in."""

    def ends(at: int) -> bool:
        mark = text[at]
        return mark in SENTENCE_ENDS or (mark == "." and text[at + 1 : at + 2] in {"", " "})

    while start > 0 and not ends(start - 1):
        start -= 1
    while end < len(text) and not ends(end):
        end += 1
    return text[start:end]


def negated(text: str, at: int, size: int) -> bool:
    """Whether anything in the sentence text[at:at + size] is in says no: before it or after it,
    whatever quotes or brackets it ("Redis is not an option"; "Do not use "Redis"")."""
    return says_no(sentence(text, at, at + size))


def quote_refusal(item: Item, quote: str, answer: str, options: Sequence[str] = ()) -> str:
    """Why a quote does not hold for an answer ("" when it does): it must be a whole phrase of
    the owner's that holds the answer (with options: the one chosen) in a sentence with nothing
    that says no (when in doubt, the question waits: "Use Postgres, not Redis" settles neither);
    and with options, any other it names must be in a sentence that says no."""
    text, said = plain(item.text), plain(quote)
    places = bounded(text, said)
    if not said or not places:
        return f"its quote is not in what you said ({item.id})"
    whole = said == text.rstrip("".join(CLAUSE) + " ")
    if not whole and (weight(said) < 2 or not any(clause(text, at, len(said)) for at in places)):
        return f"its quote is not a whole phrase of what you said ({item.id})"
    wanted = plain(answer)
    spots = bounded(said, wanted) if wanted else []
    if not spots:
        return f"what you said ({item.id}) doesn't say that answer"
    # Read where the quote is in what they said (each place it is: any that says no, holds).
    if any(negated(text, place + at, len(wanted)) for place in places for at in spots):
        return f"what you said ({item.id}) may say no to that answer"
    chosen = [(at, at + len(wanted)) for at in spots]
    for other in {plain(option) for option in options} - {wanted, ""}:
        for at in bounded(said, other):
            if any(start <= at and at + len(other) <= end for start, end in chosen):
                continue  # A part of the option chosen ("Postgres" in "Postgres 16").
            if not all(negated(text, place + at, len(other)) for place in places):
                return f"what you said ({item.id}) names another option too"
    return ""


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
    given = set(json.loads(store.get(f"{REFS}{task['id']}") or "[]"))
    for row in rows:
        meta = json.loads(row["meta"] or "{}")
        text = row["text"]
        if row["session_id"] == task["session_id"]:
            about = meta.get("task_id") or ""
            if about and about != task["id"]:
                continue
        elif row["id"] not in given:
            continue  # To the Router: only for a task it was given to by its ref.
        found.append(Item(f"message:{row['id']}", text, "message", row["created_at"],
                          version(text)))  # fmt: skip
        if len(found) >= limit:
            break
    return list(reversed(found))


def owner_cards(runtime: Runtime, task: dict) -> list[Item]:
    """The owner's answers to decision cards in the page, for the tasks that asked them: what
    they chose or wrote (the card's outcome), not the question a worker put to them."""
    store = runtime.store
    rows = store.all(
        "SELECT d.id, d.source, d.created_at FROM decisions d JOIN task_decisions t ON "
        "t.decision_id=d.id WHERE t.task_id=? AND d.status='active' AND d.source LIKE 'card:%' "
        "ORDER BY d.created_at",
        (task["id"],),
    )
    found = []
    for row in rows:
        card = row["source"].removeprefix("card:")
        if store.get(f"ingress:{card}") != PAGE:
            continue
        draft = store.draft(card)
        chosen = (draft or {}).get("outcome") or ""
        if not draft or draft["status"] != "answered" or not chosen.strip():
            continue
        asked = str((draft["target"] or {}).get("question") or draft["text"] or "")
        found.append(Item(f"card:{card}", chosen, "card", row["created_at"], version(chosen),
                          about=asked))  # fmt: skip
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


def refusal(
    runtime: Runtime,
    task: dict,
    cited: list[dict],
    offered: list[Item],
    answer: str,
    options: Sequence[str] = (),
) -> str:
    """Why the basis the judge cited does not hold ("" when every item does): none cited; an
    id not among those in scope now; a version changed since; a quote that is not a whole
    phrase of its words, or does not say the answer (quote_refusal)."""
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
        if why := quote_refusal(item, quote, answer, options):
            return why
    return ""
