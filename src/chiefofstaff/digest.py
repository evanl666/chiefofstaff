"""The daily digest: one short update instead of many, built from the record (no model call)."""

from datetime import UTC, datetime

from .store import Store


def iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).isoformat()


def names(rows: list[dict], limit: int = 4) -> str:
    titles = [f"“{row['title']}”" for row in rows[:limit]]
    more = f" (+{len(rows) - limit} more)" if len(rows) > limit else ""
    return ", ".join(titles) + more


def digest(store: Store, since: float, until: float) -> str:
    """Empty when nothing happened and nothing waits: a quiet day takes no attention."""
    window = (iso(since), iso(until))
    finished = store.all(
        """SELECT DISTINCT t.title FROM runs r JOIN tasks t ON t.id=r.task_id
        WHERE r.status='succeeded' AND r.ended_at>=? AND r.ended_at<? ORDER BY r.ended_at""",
        window,
    )
    review = store.one("SELECT count(*) AS n FROM tasks WHERE status='review'")["n"]
    waiting = store.all("SELECT title FROM tasks WHERE status='waiting' ORDER BY updated_at")
    failed = store.all("SELECT title FROM tasks WHERE status='failed' ORDER BY updated_at")
    drafts = store.one("SELECT count(*) AS n FROM drafts WHERE status='pending'")["n"]
    sent = store.one(
        "SELECT count(*) AS n FROM drafts WHERE status='sent' AND resolved_at>=? AND resolved_at<?",
        window,
    )["n"]
    follow_ups = store.one(
        """SELECT count(*) AS n FROM messages WHERE kind='inbound'
        AND json_extract(meta, '$.thread') IS NOT NULL AND created_at>=? AND created_at<?""",
        window,
    )["n"]
    if not (finished or review or waiting or failed or drafts or sent or follow_ups):
        return ""
    running = store.one(
        """SELECT count(*) AS n FROM sessions WHERE role='worker' AND closed_at IS NULL
        AND status IN ('working', 'queued', 'starting')"""
    )["n"]
    credits = store.one(
        "SELECT coalesce(sum(credits), 0) AS c FROM usage WHERE at>=? AND at<?", (since, until)
    )["c"]
    lines = ["Daily summary"]
    if finished:
        lines.append(f"• Finished: {names(finished)}")
    needs_you = []
    if review:
        needs_you.append(f"{review} result{'s' if review != 1 else ''} to review")
    if waiting:
        needs_you.append(f"{names(waiting)} asked a question")
    if drafts:
        needs_you.append(f"{drafts} draft{'s' if drafts != 1 else ''} to approve")
    if needs_you:
        lines.append("• Waiting for you: " + " · ".join(needs_you))
    if failed:
        lines.append(f"• Failed: {names(failed)}")
    if sent or follow_ups:
        lines.append(
            f"• Handled: {sent} repl{'ies' if sent != 1 else 'y'} sent, "
            f"{follow_ups} thread follow-up{'s' if follow_ups != 1 else ''} without the Router"
        )
    lines.append(f"• {running} worker{'s' if running != 1 else ''} running · {credits:.1f} credits")
    return "\n".join(lines)
