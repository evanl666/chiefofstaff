"""The daily digest: one short update instead of many, built from the record (no model call);
what needs the owner, counted as Home counts it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .store import Store, hours_ago
from .tasks import HELPERS

if TYPE_CHECKING:
    from .runtime import Runtime


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
        AND t.session_id NOT IN ({HELPERS})
        AND t.session_id IN (SELECT id FROM sessions WHERE role='worker')
        GROUP BY t.id ORDER BY max(r.ended_at)""",
        (iso(since), iso(until)),
    )


def needs_you(runtime: Runtime) -> int:
    """How many things wait for the owner, as Home's Needs you counts them (inbox.js needsYou,
    board.js taskPhase): cards to approve or answer and sends to check; tasks waiting for an
    answer or Go on (not one a decision card asks for: the card counts), failed, blocked, or
    whose check-back failed; results to accept or with news (not one on a draft's card);
    workers' questions their task does not show; the Router's notes that need them."""
    from .runtime import TASKS_SHOWN_HOURS

    store = runtime.store
    drafts = store.drafts()
    deciding = {
        asker.get("task_id")
        for draft in drafts
        if draft["kind"] == "decision"
        for asker in draft["target"].get("askers") or []
    }
    on_cards = {
        task["id"]
        for draft in drafts
        if draft["kind"] not in {"decision", "merge"}
        and (task := runtime.drafts.task_of(draft))
        and task["status"] == "review"
    }
    shown = set()
    for task in runtime.tasks.listed(include_ended_since=hours_ago(TASKS_SHOWN_HOURS)):
        status = task["status"]
        if status == "waiting":
            needed = task["id"] not in deciding
        elif status == "failed":
            needed = True
        elif status == "queued":
            needed = bool(runtime.tasks.broken(task))
        elif status == "running":
            needed = (runtime.task_actions.check_back(task) or {}).get("attempts", 0) >= 3
        elif status in {"review", "done"} and task["id"] not in on_cards:
            needed = bool(store.get(f"news:{task['id']}")) or runtime.task_actions.needs_acceptance(
                task
            )
        else:
            needed = False
        if needed:
            shown.add(task["id"])
    questions = [q for q in runtime.asks.owner_questions() if q["task_id"] not in shown]
    return (
        len(drafts)
        + len(store.drafts("uncertain"))
        + len(shown)
        + len(questions)
        + len(runtime.owner_updates())
    )


def digest(runtime: Runtime, since: float, until: float) -> str:
    """Empty when nothing happened and nothing waits: a quiet day takes no attention."""
    from .runtime import TASKS_SHOWN_HOURS

    store = runtime.store
    window = (iso(since), iso(until))
    finished = finished_tasks(store, since, until)
    mine = f"session_id NOT IN ({HELPERS})"
    # Results that wait for the owner's Accept, as Home shows them (a report that only informs
    # is read, not accepted).
    review = sum(
        runtime.task_actions.needs_acceptance(task)
        for task in store.all(f"SELECT * FROM tasks WHERE status='review' AND {mine}")
    )
    waiting = store.all(
        f"SELECT id, title FROM tasks WHERE status='waiting' AND {mine} ORDER BY updated_at"
    )
    stopped = [task for task in waiting if store.get(f"stopped:{task['id']}")]
    asking = [task for task in waiting if task not in stopped]
    failed = store.all(
        f"SELECT title FROM tasks WHERE status='failed' AND ended_at>=? AND {mine} "
        "ORDER BY updated_at",
        (hours_ago(TASKS_SHOWN_HOURS),),
    )
    drafts = store.value("SELECT count(*) AS n FROM drafts WHERE status='pending'")
    sent = store.value(
        "SELECT count(*) AS n FROM drafts WHERE status='sent' AND resolved_at>=? AND resolved_at<?",
        window,
    )
    follow_ups = store.value(
        """SELECT count(*) AS n FROM messages WHERE kind='inbound'
        AND json_extract(meta, '$.thread') IS NOT NULL AND created_at>=? AND created_at<?""",
        window,
    )
    if not (finished or review or waiting or failed or drafts or sent or follow_ups):
        return ""
    running = store.value(f"SELECT count(*) AS n FROM tasks WHERE status='running' AND {mine}")
    credits = store.value(
        "SELECT coalesce(sum(credits), 0) AS c FROM usage WHERE at>=? AND at<?", (since, until)
    )
    lines = ["Daily summary"]
    if finished:
        lines.append(f"• Finished: {names(finished)}")
    waits = []
    if review:
        waits.append(f"{review} result{'s' if review != 1 else ''} to review")
    if asking:
        waits.append(f"{names(asking)} asked a question")
    if stopped:
        waits.append(f"{names(stopped)} stopped (Go on or Cancel)")
    if drafts:
        waits.append(f"{drafts} draft{'s' if drafts != 1 else ''} to approve")
    if waits:
        lines.append("• Waiting for you: " + " · ".join(waits))
    if failed:
        lines.append(f"• Failed: {names(failed)}")
    if sent or follow_ups:
        lines.append(
            f"• Handled: {sent} repl{'ies' if sent != 1 else 'y'} sent, "
            f"{follow_ups} thread follow-up{'s' if follow_ups != 1 else ''} without the Router"
        )
    lines.append(f"• {running} task{'s' if running != 1 else ''} running · {credits:.1f} credits")
    return "\n".join(lines)
