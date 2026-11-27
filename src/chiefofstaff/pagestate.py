"""The page's state as the page reads it again and again (`GET /api/state`, after each burst of
events): what the page never shows left out, and what finished tasks said at length cut to what
their rows and chips read. Their whole words come once, on demand (`GET /api/task-texts`), and the
page keeps them for each version (state.js). Nothing the page shows changes.
"""

from __future__ import annotations

import hashlib
import re

# A finished task's result carried whole up to this many characters; past it, its start (whole
# lines, never inside a code block) and every later line that gives a link: its row's words and
# its chips' links are read from those.
RESULT_HEAD = 1000
# Fenced code, as the page's text.js leaves it out (to the end when never closed).
FENCE = re.compile(r"```[\s\S]*?(?:```|\Z)")
# A session's columns as stored, which its view has parsed (`checkouts`) or the page never reads:
# its latest report (shown, on hover, only for a closed worker), what it said last (only for an
# open one, at work).
UNREAD = ("repos", "files", "links", "said_at")
UNREAD_OPEN = ("report",)
UNREAD_CLOSED = ("said",)


def texts_key(result: str, earlier: list) -> str:
    """What a task's whole words are, in a few characters: the page asks for them again only
    once they differ. (Read as they are, not written out as JSON first: the state is read
    often.)"""
    digest = hashlib.blake2b(result.encode(), digest_size=6)
    for item in earlier:
        for key, value in sorted(item.items()) if isinstance(item, dict) else [("", item)]:
            digest.update(f"\0{key}\0{value}".encode())
        digest.update(b"\1")
    return digest.hexdigest()


def result_head(text: str) -> str:
    """A long result as a finished task's row and chips read it: its first RESULT_HEAD
    characters or so (whole lines, never cut inside fenced code), then each later line outside
    fenced code that gives a link, as it is (the page finds links, and their names, on one
    line). The text itself when it is not longer."""
    if len(text) <= RESULT_HEAD:
        return text
    fences = [match.span() for match in FENCE.finditer(text)]
    cut = text.rfind("\n", 0, RESULT_HEAD + 1)
    if cut <= 0:
        cut = text.find("\n")  # One long first line: whole.
        if cut < 0:
            return text
    for start, end in fences:
        if start < cut < end:
            cut = text.rfind("\n", 0, start)  # Before the fence: its line too.
            break
    if cut <= 0:
        return text
    links = []
    at = cut + 1
    for line in text[cut + 1 :].split("\n"):
        end = at + len(line)
        if "://" in line and not any(start < end and at < stop for start, stop in fences):
            links.append(line)
        at = end + 1
    head = text[:cut]
    shorter = f"{head}\n\n" + "\n".join(links) if links else head
    return shorter if len(shorter) < len(text) else text


def page_task(view: dict) -> dict:
    """A task's view for the page's state: its earlier reports by when only (how many its card
    says), and a finished one's long result cut (result_head); `texts_key` when anything was, for
    the page to ask for the rest (TaskActions.texts)."""
    earlier = view.get("earlier_results") or []
    kept = [item for item in earlier if isinstance(item, dict) and _words(item.get("text"))]
    result = view.get("result") or ""
    cut = result_head(result) if view.get("status") in {"done", "cancelled"} else result
    if not kept and cut == result:
        return view
    return {
        **view,
        "result": cut,
        "earlier_results": [{"at": item.get("at")} for item in kept],
        "texts_key": texts_key(result, earlier),
    }


def _words(text) -> bool:
    """Whether an earlier report says anything (the page shows those only)."""
    return isinstance(text, str) and bool(text.strip())


def page_session(view: dict, closed: bool) -> dict:
    """A session's view for the page's state, without what the page never reads of it."""
    dropped = (*UNREAD, *(UNREAD_CLOSED if closed else UNREAD_OPEN))
    return {key: value for key, value in view.items() if key not in dropped}


def page_state(snapshot: dict) -> dict:
    """Runtime.snapshot as the page reads it (see the module's doc)."""
    return {
        **snapshot,
        "tasks": [page_task(task) for task in snapshot.get("tasks", [])],
        "sessions": [page_session(row, False) for row in snapshot.get("sessions", [])],
        "closed": [page_session(row, True) for row in snapshot.get("closed", [])],
    }
