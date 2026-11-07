"""Tasks and runs: what a worker is asked to do, and each attempt at doing it.

A worker session is a conversation with its own folder. Over time it may take several tasks
(follow-ups on the same subject reuse its context); each task has a goal, a status and a
result, and dependencies point at tasks, not at sessions. A run is one attempt at a task: it
starts when the worker starts on it and ends with a report, a failure or a cancellation, and
usage is counted per run.

Task status:
    queued    waiting for a worker slot or for the tasks it depends on
    running   the worker is on it
    waiting   the worker asked a question or is blocked
    review    the worker reported it done: ready for the owner to check
    done      the owner accepted it (or it was done before tasks existed)
    failed    the worker failed; it can be retried
    cancelled stopped for good
Tasks that depend on another start once it is in review or done, or (when they need its
accepted result) once it is done. A task's acceptance criteria ("done when") are checks the
worker reports on, each with its evidence, so the review card can tell verified from claimed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from .store import Store, now

STATUSES = ("queued", "running", "waiting", "review", "done", "failed", "cancelled")
OPEN = frozenset({"queued", "running", "waiting"})
FINISHED = frozenset({"review", "done"})  # Dependencies on these are met.
ENDED = frozenset({"done", "failed", "cancelled"})
# How a run ends when its task moves to this status.
RUN_ENDS = {
    "review": "succeeded",
    "done": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}
# The result of the task an adopted session gets so it has one: nothing the owner asked for.
ADOPTED = "Adopted from Copilot."
TASK_FIELDS = {"title", "goal", "acceptance", "status", "result", "progress", "why_new", "ended_at"}


def task_id() -> str:
    return "t" + uuid4().hex[:7]


# Chief of Staff's own helper workers (learning, checks, profile, connector, messenger): not the
# owner's tasks, never listed with them.
HELPER_KINDS = ("learner", "checker", "distill", "connector", "messenger")
HELPERS = " UNION ".join(
    f"SELECT substr(key, {len(kind) + 2}) FROM kv WHERE key >= '{kind}:' AND key < '{kind}:\uffff'"
    for kind in HELPER_KINDS
)


class TaskBook:
    def __init__(self, store: Store, on_change: Callable[[dict], None] = lambda task: None):
        self.store = store
        self.on_change = on_change  # Called with every created or changed task.

    # Reading

    def find(self, task_id: str) -> dict | None:
        if row := self.store.one("SELECT * FROM tasks WHERE id=?", (task_id,)):
            return row
        if len(task_id) >= 4:
            rows = self.store.all("SELECT * FROM tasks WHERE id LIKE ? LIMIT 2", (task_id + "%",))
            if len(rows) == 1:
                return rows[0]
        return None

    def get(self, task_id: str) -> dict:
        if row := self.find(task_id):
            return row
        raise LookupError(f"No task {task_id}.")

    def current(self, session_id: str) -> dict | None:
        """The task a worker is on now, or finished last."""
        row = self.store.one("SELECT task_id FROM sessions WHERE id=?", (session_id,))
        return self.find(row["task_id"]) if row and row["task_id"] else None

    def for_session(self, session_id: str) -> list[dict]:
        return self.store.all(
            "SELECT * FROM tasks WHERE session_id=? ORDER BY created_at", (session_id,)
        )

    def listed(self, *, include_ended_since: str = "") -> list[dict]:
        """Open and in-review tasks, those that ended since the given time, and finished ones
        with news the owner has not seen (not the placeholder task of an adopted session, until
        it is continued)."""
        return self.store.all(
            f"""SELECT * FROM tasks WHERE (status IN ('queued','running','waiting','review')
            OR (ended_at IS NOT NULL AND ended_at>=?)
            OR 'news:' || id IN (SELECT key FROM kv WHERE key LIKE 'news:%'))
            AND NOT (status='done' AND result=?) AND session_id NOT IN ({HELPERS})
            ORDER BY created_at""",
            (include_ended_since or now(), ADOPTED),
        )

    def run(self, session_id: str) -> dict | None:
        """The run in progress for a session."""
        return self.store.one(
            """SELECT * FROM runs WHERE session_id=? AND ended_at IS NULL
            ORDER BY started_at DESC LIMIT 1""",
            (session_id,),
        )

    def latest_run(self, session_id: str) -> dict | None:
        """The last run of the task a session is on, finished or not."""
        task = self.current(session_id)
        if not task:
            return None
        return self.store.one(
            "SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (task["id"],)
        )

    def runs(self, task_id: str) -> list[dict]:
        return self.store.all("SELECT * FROM runs WHERE task_id=? ORDER BY started_at", (task_id,))

    def deps(self, task_id: str) -> list[dict]:
        """The tasks it depends on, each with what it needs of them (`needs`)."""
        return self.store.all(
            """SELECT t.*, d.needs FROM task_deps d JOIN tasks t ON t.id=d.depends_on
            WHERE d.task_id=? ORDER BY t.created_at""",
            (task_id,),
        )

    @staticmethod
    def criteria(task: dict) -> list[str]:
        """Its acceptance criteria: what shows it is done."""
        return json.loads(task["acceptance"]) if task.get("acceptance") else []

    def dependents(self, task_id: str) -> list[dict]:
        """Tasks that wait for this one."""
        return self.store.all(
            """SELECT t.* FROM task_deps d JOIN tasks t ON t.id=d.task_id
            WHERE d.depends_on=? ORDER BY t.created_at""",
            (task_id,),
        )

    def unmet(self, task: dict) -> list[dict]:
        return [
            dep
            for dep in self.deps(task["id"])
            if dep["status"] not in FINISHED
            or (dep["needs"] == "accepted" and dep["status"] != "done")
        ]

    def broken(self, task: dict) -> list[dict]:
        """Dependencies that ended without finishing: this task cannot start on its own."""
        return [dep for dep in self.deps(task["id"]) if dep["status"] in {"failed", "cancelled"}]

    def credits(self, task_id: str) -> float:
        return self.store.value(
            """SELECT coalesce(sum(u.credits), 0) AS c FROM usage u JOIN runs r ON r.id=u.run_id
            WHERE r.task_id=?""",
            (task_id,),
        )

    # Writing

    def create(
        self,
        session_id: str,
        title: str,
        goal: str,
        *,
        status: str = "queued",
        why_new: str = "",
        after: tuple[str, ...] | list[str] = (),
        needs: str = "result",
        criteria: list[str] | None = None,
    ) -> dict:
        assert status in STATUSES and needs in {"result", "accepted"}
        identifier, stamp = task_id(), now()
        acceptance = json.dumps(criteria) if criteria else ""
        self.store.execute(
            """INSERT INTO tasks(id, session_id, title, goal, acceptance, status, why_new,
            created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (identifier, session_id, title, goal, acceptance, status, why_new, stamp, stamp),
        )
        for dependency in dict.fromkeys(after):
            self.store.execute(
                "INSERT OR IGNORE INTO task_deps(task_id, depends_on, needs) VALUES (?,?,?)",
                (identifier, dependency, needs),
            )
        self.store.execute("UPDATE sessions SET task_id=? WHERE id=?", (identifier, session_id))
        if status == "running":
            self.start_run(identifier, "start")
        task = self.get(identifier)
        self.on_change(task)
        return task

    def update(self, task_id: str, **values: Any) -> dict:
        if not values.keys() <= TASK_FIELDS:
            raise ValueError(f"Invalid task fields: {sorted(values.keys() - TASK_FIELDS)}")
        values["updated_at"] = now()
        self.store.execute(
            f"UPDATE tasks SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
            (*values.values(), task_id),
        )
        task = self.get(task_id)
        self.on_change(task)
        return task

    def start_run(self, task_id: str, reason: str) -> dict:
        task = self.get(task_id)
        if open_run := self.store.one(
            "SELECT * FROM runs WHERE task_id=? AND ended_at IS NULL", (task_id,)
        ):
            return open_run
        identifier = "r" + uuid4().hex[:7]
        self.store.execute(
            """INSERT INTO runs(id, task_id, session_id, status, reason, started_at)
            VALUES (?,?,?,?,?,?)""",
            (identifier, task_id, task["session_id"], "running", reason, now()),
        )
        return self.store.row("SELECT * FROM runs WHERE id=?", (identifier,))

    def end_runs(self, task_id: str, status: str):
        self.store.execute(
            "UPDATE runs SET status=?, ended_at=? WHERE task_id=? AND ended_at IS NULL",
            (status, now(), task_id),
        )

    def move(self, task_id: str, status: str, *, reason: str = "", **values: Any) -> dict:
        """Change a task's status, keeping its runs in step."""
        assert status in STATUSES
        task = self.get(task_id)
        if status in {"running", "waiting"}:
            self.start_run(
                task_id, reason or ("follow-up" if task["status"] != "queued" else "start")
            )
        elif status in RUN_ENDS:
            self.end_runs(task_id, RUN_ENDS[status])
        values["ended_at"] = now() if status in ENDED else None
        if status in {"done", "cancelled"}:
            # Settled (accepted, merged, cancelled): an earlier question to its worker is moot.
            self.store.delete(f"followup:{task_id}")
        if status in {"review", "done", "cancelled"}:
            self.store.delete(f"reopened:{task_id}")  # Its follow-up is over.
        if status == "running" and task["session_id"]:
            # The worker is on this task now.
            self.store.execute(
                "UPDATE sessions SET task_id=? WHERE id=?", (task_id, task["session_id"])
            )
        return self.update(task_id, status=status, **values)

    # Text for sessions

    def briefing(self, task_ids: list[str]) -> str:
        """What earlier tasks found and where their work is, for a worker that builds on them."""
        parts = []
        for identifier in task_ids:
            task = self.get(identifier)
            session = self.store.find_session(task["session_id"]) or {}
            lines = [f"Task {task['id']} “{task['title']}” ({task['status']})"]
            if task["goal"]:
                lines.append(f"Goal: {task['goal'][:600]}")
            if task["result"]:
                lines.append(f"Result: {task['result'][:1500]}")
            if session.get("cwd"):
                where = session["cwd"] + (
                    f" (branch {session['branch']})" if session["branch"] else ""
                )
                lines.append(f"Its folder: {where}")
            files = json.loads(session.get("files") or "[]")[-12:]
            if files:
                lines.append("Files it changed: " + ", ".join(files))
            links = json.loads(session.get("links") or "[]")[-8:]
            if links:
                lines.append("Links: " + ", ".join(links))
            parts.append("\n".join(lines))
        return "Earlier work this task builds on:\n\n" + "\n\n".join(parts)


SESSION_TO_TASK = {
    "queued": "queued",
    "starting": "running",
    "working": "running",
    "idle": "review",  # A finished turn; nothing runs until someone sends it more.
    "waiting": "waiting",
    "done": "review",
    "failed": "failed",
}


def settle_adopted(store: Store):
    """Sessions adopted before tasks existed became running tasks, though nothing runs there."""
    for task in store.all(
        """SELECT t.id FROM tasks t JOIN sessions s ON s.id=t.session_id
        WHERE t.status IN ('running', 'queued') AND s.status='idle' AND s.closed_at IS NULL
        AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id=s.id)"""
    ):
        stamp = now()
        store.execute(
            "UPDATE tasks SET status='done', result=?, ended_at=?, updated_at=? WHERE id=?",
            (ADOPTED, stamp, stamp, task["id"]),
        )
        store.execute(
            "UPDATE runs SET status='succeeded', ended_at=? WHERE task_id=? AND ended_at IS NULL",
            (stamp, task["id"]),
        )


def settle_followups(store: Store):
    """0.1.7-0.1.9 reopened a finished task when the owner messaged its worker directly; one
    that only answered stayed "running" with nothing running. Back to review: at startup no
    turn runs, and a worker that was mid-turn shows as working. Once: a follow-up that is
    legitimately between turns looks the same."""
    if store.get("settled_followups"):
        return
    store.set("settled_followups", "1")
    for task in store.all(
        """SELECT t.id FROM tasks t JOIN sessions s ON s.id=t.session_id
        WHERE t.status='running' AND s.status IN ('done', 'idle') AND s.closed_at IS NULL
        AND (SELECT reason FROM runs r WHERE r.task_id=t.id ORDER BY r.started_at DESC LIMIT 1)
            = 'follow-up'"""
    ):
        stamp = now()
        store.execute(
            "UPDATE tasks SET status='review', updated_at=? WHERE id=?", (stamp, task["id"])
        )
        store.execute(
            "UPDATE runs SET status='succeeded', ended_at=? WHERE task_id=? AND ended_at IS NULL",
            (stamp, task["id"]),
        )


def migrate(store: Store):
    """Give every worker from before tasks existed one task, its runs and its dependencies."""
    if store.one("SELECT id FROM tasks LIMIT 1"):
        return
    by_session: dict[str, str] = {}
    for row in store.all("SELECT * FROM sessions WHERE role='worker' ORDER BY created_at"):
        status = SESSION_TO_TASK.get(row["status"], "running")
        if row["closed_at"]:
            status = {"review": "done", "failed": "failed"}.get(status, "cancelled")
        identifier, stamp = task_id(), row["created_at"]
        ended = row["closed_at"] or row["updated_at"] if status in ENDED else None
        store.execute(
            """INSERT INTO tasks(id, session_id, title, goal, status, result, created_at,
            updated_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                row["id"],
                row["title"],
                row["task"],
                status,
                row["report"],
                stamp,
                row["updated_at"],
                ended,
            ),
        )
        store.execute("UPDATE sessions SET task_id=? WHERE id=?", (identifier, row["id"]))
        if status != "queued":
            run = "r" + uuid4().hex[:7]
            run_status = RUN_ENDS.get(status, "running")
            store.execute(
                """INSERT INTO runs(id, task_id, session_id, status, reason, started_at, ended_at)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    run,
                    identifier,
                    row["id"],
                    run_status,
                    "start",
                    stamp,
                    None if run_status == "running" else row["updated_at"],
                ),
            )
            store.execute("UPDATE usage SET run_id=? WHERE session_id=?", (run, row["id"]))
        by_session[row["id"]] = identifier
    for session_id, identifier in by_session.items():
        after = json.loads(store.get(f"after:{session_id}") or "[]")
        for dependency in after:
            if dependency in by_session:
                store.execute(
                    "INSERT OR IGNORE INTO task_deps(task_id, depends_on) VALUES (?,?)",
                    (identifier, by_session[dependency]),
                )
