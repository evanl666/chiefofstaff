"""The daily digest: one short update instead of many, built from the record (no model call);
what needs the owner, counted as Home counts it."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from .drafts import CARD_WORDS
from .store import Store, hours_ago
from .tasks import HELPERS

if TYPE_CHECKING:
    from .runtime import Runtime

# Why a task is under Needs you, in Home's words (board.js MARKERS).
NEEDS_WORDS = {
    "blocked": "blocked",
    "stopped": "stopped: waits for Go on or Cancel",
    "question": "question",
    "failed": "failed",
    "review": "to review",
    "update": "update",
    "stalled": "no progress",
    "check-failed": "check-back failed",
    "pr-failing": "CI failing on its PR",
    "pr-changes": "changes requested on its PR",
    "pr-conflicts": "merge conflicts on its PR",
}
# A delivered task's own PR to fix (board.js PR_TO_FIX): it goes under Needs you.
PR_TO_FIX = {"pr-changes", "pr-conflicts", "pr-failing"}
# Cards whose text goes out once the owner approves it.
APPROVALS = {"slack", "github", "reply"}


def iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).isoformat()


def names(rows: list[dict], limit: int = 4) -> str:
    titles = [f"“{row['title']}”" for row in rows[:limit]]
    more = f" (+{len(rows) - limit} more)" if len(rows) > limit else ""
    return ", ".join(titles) + more


def finished_tasks(store: Store, since: float, until: float) -> list[dict]:
    """The owner's tasks a run finished in that time: not those of Chief of Staff's own helpers
    (learning, checks, the connector, the messenger), nor those cancelled since."""
    return store.all(
        f"""SELECT t.id, t.title FROM tasks t JOIN runs r ON r.task_id=t.id
        WHERE r.status='succeeded' AND r.ended_at>=? AND r.ended_at<? AND t.status!='cancelled'
        AND t.id NOT IN (SELECT substr(key, 10) FROM kv WHERE key LIKE 'setaside:%')
        AND t.session_id NOT IN ({HELPERS})
        AND t.session_id IN (SELECT id FROM sessions WHERE role='worker')
        GROUP BY t.id ORDER BY max(r.ended_at)""",  # noqa: S608  (HELPERS: constant SQL)
        (iso(since), iso(until)),
    )


@dataclass
class Needs:
    """What waits for the owner, item by item as Home's Needs you lists it. Each item has a
    `kind` ("card", "send" to check, "task", "question", "note" of the Router's) and the
    session it is from; a task's `marker` says why (NEEDS_WORDS). `markers` holds every
    listed task's: those under Needs you, "card" for a result reviewed on its draft's card
    (counted once, with that card) and "decision" for one a decision card asks for."""

    items: list[dict] = field(default_factory=list)
    markers: dict[str, str] = field(default_factory=dict)
    # What an accepted task is still monitored for, in a few words ("" for nothing).
    monitored: Callable[[dict], str] = lambda task: ""

    def of(self, kind: str) -> list[dict]:
        return [item for item in self.items if item["kind"] == kind]

    def tasks(self, *markers: str) -> list[dict]:
        return [item["task"] for item in self.of("task") if item["marker"] in markers]

    def sessions(self) -> set[str]:
        return {item["session_id"] for item in self.items}


def stalled(runtime: Runtime, task: dict, moment: datetime) -> bool:
    """Home's "No progress" (board.js taskPhase): a running task quiet for stall_hours with
    nothing it waits on, whose worker was asked where it stands an hour ago or more and has
    done nothing on it since."""
    actions = runtime.task_actions
    if not actions.stalled(task, moment):
        return False
    store = runtime.store
    row = store.find_session(task["session_id"])
    if not row or runtime.copilot_id(row["id"]) in runtime.elsewhere:
        return False
    if (row["activity"] or "").lower().startswith("waiting for "):
        return False
    if row["status"] == "waiting" and runtime.working_on(row["id"]) == task["id"]:
        return False  # It waits for the owner on a card.
    started = task["progress"] or task["result"] or store.get(f"started:{task['id']}")
    if not started and any(
        other["status"] == "waiting" and store.get(f"stopped:{other['id']}")
        for other in runtime.tasks.for_session(row["id"])
    ):
        return False  # Up next, once the owner says Go on to the one stopped.
    nudged = store.get(f"stallnudge:{task['id']}") or ""
    if not nudged:
        return False
    asked = datetime.fromisoformat(nudged)
    active = datetime.fromisoformat(actions.active_at(task, row))
    return asked >= active - timedelta(seconds=1) and moment - asked >= timedelta(hours=1)


def needs_marker(runtime: Runtime, task: dict, moment: datetime) -> str:
    """Why Home puts a task under Needs you (board.js taskPhase), "" when it does not: not one
    on a draft's card (see needs). What the task's view says the owner has to do about it
    (TaskActions.attention: every result to accept, its own PR to fix, news on finished
    work, a question…), and a running task that made no progress. Its own PR ready to merge
    is not here: its Merge card is."""
    attention, _why = runtime.task_actions.attention(task)
    if task["status"] == "running" and not attention:
        return "stalled" if stalled(runtime, task, moment) else ""
    return {"acceptance": "review", "merge": ""}.get(attention, attention)


def needs(runtime: Runtime, moment: datetime | None = None) -> Needs:
    """What waits for the owner, as Home's Needs you lists it (inbox.js needsYou, board.js
    taskPhase), so its count, the Router's answer, the focus and daily summaries and tidying
    agree: every card (a result its worker drafted something for is reviewed on that card,
    counted once with it) and send to check; tasks waiting for an answer or Go on (not one a
    decision card asks for: the card counts), failed, blocked, stalled, or whose check-back
    failed; results to accept or with news; workers' questions their task does not show; the
    Router's notes that need the owner, until seen."""
    from .runtime import TASKS_SHOWN_HOURS

    moment = moment or datetime.now(UTC)
    store = runtime.store
    found = Needs(monitored=runtime.task_actions.monitoring_words)
    drafts = store.drafts()
    deciding = {
        asker.get("task_id")
        for draft in drafts
        if draft["kind"] == "decision"
        for asker in draft["target"].get("askers") or []
    }
    for draft in drafts:
        item = {"kind": "card", "draft": draft, "session_id": draft["session_id"]}
        if (
            draft["kind"] not in {"decision", "merge"}
            and (task := runtime.drafts.task_of(draft))
            # A result to review, or finished work with news (what it drafted is the news).
            and (
                task["status"] == "review"
                or (task["status"] == "done" and store.get(f"news:{task['id']}"))
            )
            and task["id"] not in found.markers
        ):
            found.markers[task["id"]] = "card"
            item["task"] = task
        found.items.append(item)
    for draft in store.drafts("uncertain"):
        found.items.append({"kind": "send", "draft": draft, "session_id": draft["session_id"]})
    # A PR two delivered tasks of one worker follow is one row on Home (board.js boardOf):
    # counted once, for the newest of them.
    pr_rows: list[tuple[dict, str]] = []
    for task in runtime.tasks.listed(include_ended_since=hours_ago(TASKS_SHOWN_HOURS)):
        if task["id"] in found.markers:
            continue
        marker = needs_marker(runtime, task, moment)
        if marker in PR_TO_FIX:
            pr_rows.append((task, marker))
        elif marker == "question" and task["id"] in deciding:
            found.markers[task["id"]] = "decision"
        elif marker == "question" and runtime.drafts.question_draft(task):
            found.markers[task["id"]] = "card"  # It asks only about its draft: that card counts.
        elif marker:
            found.markers[task["id"]] = marker
            found.items.append(
                {"kind": "task", "marker": marker, "task": task, "session_id": task["session_id"]}
            )
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for task, marker in sorted(pr_rows, key=lambda row: row[0]["updated_at"], reverse=True):
        if task["status"] == "review":
            # A result still to accept stays a row of its own, whichever task has the PR's.
            found.markers[task["id"]] = marker
            found.items.append(
                {"kind": "task", "marker": marker, "task": task, "session_id": task["session_id"]}
            )
            continue
        follows = store.follows(task["session_id"])
        key = (task["session_id"], tuple(sorted(runtime.task_actions.followed_for(task, follows))))
        if key in seen:
            continue
        seen.add(key)
        found.markers[task["id"]] = marker
        found.items.append(
            {"kind": "task", "marker": marker, "task": task, "session_id": task["session_id"]}
        )
    for question in runtime.asks.owner_questions():
        if found.markers.get(question["task_id"]) not in NEEDS_WORDS:
            found.items.append(
                {"kind": "question", "question": question, "session_id": question["session_id"]}
            )
    router = store.get("router_id") or ""
    for note in runtime.owner_updates():
        found.items.append({"kind": "note", "note": note, "session_id": router})
    return found


def task_words(task: dict, found: Needs) -> str:
    """Its status with where Home shows it, in its words: why it is under Needs you, or a
    finished one's standing (every result waits for the owner's Accept; one accepted may
    still be monitored, its PR waiting for a review, say)."""
    marker = found.markers.get(task["id"], "")
    if marker in NEEDS_WORDS:
        words = f"{NEEDS_WORDS[marker]} (needs you)"
    elif marker == "card" and task["status"] == "waiting":
        words = "its question is its draft's card (needs you)"
    elif marker == "card":
        words = "to review on its draft's card (needs you)"
    elif marker == "decision":
        words = "on the owner's decision card (needs you)"
    elif task["status"] == "review":
        words = "to accept once its worker has followed up on the owner's message"
    elif task["status"] == "done" and (watched := found.monitored(task)):
        words = f"accepted, monitoring {watched}"
    else:
        return task["status"]
    return words if words.startswith(task["status"]) else f"{task['status']}, {words}"


def needs_you(runtime: Runtime) -> int:
    """How many things wait for the owner, as Home's Needs you counts them."""
    return len(needs(runtime).items)


def plural(count: int, word: str, many: str = "") -> str:
    return f"{count} {word if count == 1 else many or word + 's'}"


def waiting_parts(found: Needs) -> list[str]:
    """What waits for the owner, by Home's kinds (each item once; failed tasks apart)."""
    asking = found.tasks("question") + [
        {"title": item["question"]["task_title"] or item["question"]["worker"]}
        for item in found.of("question")
    ]
    cards: list[str] = [item["draft"]["kind"] for item in found.of("card")]
    approvals = sum(kind in APPROVALS for kind in cards)
    others = Counter(CARD_WORDS.get(kind) or kind for kind in cards if kind not in APPROVALS)
    review, update = found.tasks("review"), found.tasks("update")
    stopped, blocked = found.tasks("stopped"), found.tasks("blocked")
    stalled_, unchecked = found.tasks("stalled"), found.tasks("check-failed")
    sends, notes = found.of("send"), found.of("note")
    parts = [
        (review, plural(len(review), "result") + " to review"),
        (update, plural(len(update), "update") + " on finished work"),
        (asking, f"{names(asking)} asked a question"),
        (stopped, f"{names(stopped)} stopped (Go on or Cancel)"),
        (blocked, f"{names(blocked)} blocked (a task it needs did not finish)"),
        (stalled_, f"{names(stalled_)} made no progress"),
        (unchecked, f"{names(unchecked)} could not check back"),
        (approvals, plural(approvals, "draft") + " to approve"),
        (others, plural(others.total(), "card") + f" to answer ({', '.join(sorted(others))})"),
        (sends, plural(len(sends), "send") + " to check"),
        (notes, plural(len(notes), "note") + " of mine to act on"),
    ]
    return [text for found_, text in parts if found_]


def digest(runtime: Runtime, since: float, until: float) -> str:
    """Empty when nothing happened and nothing waits: a quiet day takes no attention. What
    waits is what Home's Needs you lists."""
    store = runtime.store
    window = (iso(since), iso(until))
    finished = finished_tasks(store, since, until)
    mine = f"session_id NOT IN ({HELPERS})"  # Constant SQL.
    found = needs(runtime)
    failed = found.tasks("failed")
    sent = store.value(
        "SELECT count(*) AS n FROM drafts WHERE status='sent' AND resolved_at>=? AND resolved_at<? "
        "AND kind IN ('slack', 'github', 'reply')",  # Replies and reviews, not a merge.
        window,
    )
    follow_ups = store.value(
        """SELECT count(*) AS n FROM messages WHERE kind='inbound'
        AND json_extract(meta, '$.thread') IS NOT NULL AND created_at>=? AND created_at<?""",
        window,
    )
    if not (finished or found.items or sent or follow_ups):
        return ""
    running = store.value(
        f"SELECT count(*) AS n FROM tasks WHERE status='running' AND {mine}"  # noqa: S608
    )
    credits_used = store.value(
        "SELECT coalesce(sum(credits), 0) AS c FROM usage WHERE at>=? AND at<?", (since, until)
    )
    lines = ["Daily summary"]
    if finished:
        lines.append(f"• Finished: {names(finished)}")
    if waits := waiting_parts(found):
        lines.append("• Waiting for you: " + " · ".join(waits))
    if failed:
        lines.append(f"• Failed: {names(failed)}")
    if sent or follow_ups:
        lines.append(
            f"• Handled: {sent} repl{'ies' if sent != 1 else 'y'} sent, "
            f"{follow_ups} thread follow-up{'s' if follow_ups != 1 else ''} without the Router"
        )
    lines.append(
        f"• {running} task{'s' if running != 1 else ''} running · {credits_used:.1f} credits"
    )
    return "\n".join(lines)
