import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import Settings


def now() -> str:
    return datetime.now(UTC).isoformat()


def today() -> str:
    return datetime.now().astimezone().date().isoformat()


def identifier() -> str:
    return uuid4().hex


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        self.db = sqlite3.connect(directory / "work-buddy.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, parent_id TEXT REFERENCES tasks(id),
            title TEXT NOT NULL, instructions TEXT NOT NULL, role TEXT NOT NULL,
            status TEXT NOT NULL, project_path TEXT NOT NULL, source TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'work',
            result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            rounds INTEGER NOT NULL DEFAULT 0, backend TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
            kind TEXT NOT NULL, question TEXT NOT NULL, options TEXT NOT NULL,
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            choice TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, text TEXT NOT NULL, scope TEXT NOT NULL,
            project_path TEXT NOT NULL, task_id TEXT, created_at TEXT NOT NULL
            ,category TEXT NOT NULL DEFAULT 'preference', evidence TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
            text TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inbox (
            event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_briefs (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id),
            day TEXT NOT NULL, text TEXT NOT NULL, project_path TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)
        self.db.commit()
        (directory / "work-buddy.db").chmod(0o600)

    def close(self):
        self.db.close()

    def all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def execute(self, sql: str, params: tuple = ()):
        with self.db:
            return self.db.execute(sql, params)

    def settings(self) -> Settings:
        row = self.one("SELECT value FROM settings WHERE id=1")
        return Settings.model_validate_json(row["value"]) if row else Settings()

    def save_settings(self, settings: Settings):
        self.execute("INSERT OR REPLACE INTO settings VALUES (1, ?)", (settings.model_dump_json(),))

    def create_daily_brief(self, text: str) -> str:
        day = today()
        task_id = self.create_task(
            f"Your priorities for {day}",
            text,
            kind="daily",
            source={"type": "daily", "day": day},
        )
        self.execute(
            "INSERT INTO daily_briefs VALUES(?,?,?,?,?)",
            (task_id, day, text, self.task(task_id)["project_path"], now()),
        )
        return task_id

    def daily_briefs(self, project_path: str) -> list[dict]:
        return self.all(
            """SELECT b.*,t.status FROM daily_briefs b JOIN tasks t ON t.id=b.task_id
            WHERE b.day=? AND b.project_path=? AND t.status!='cancelled'
            ORDER BY b.created_at DESC LIMIT 10""",
            (today(), project_path),
        )

    def backlog_count(self) -> int:
        return self.one(
            """SELECT count(*) AS n FROM tasks
            WHERE status NOT IN ('completed','failed','cancelled','interrupted','ignored')"""
        )["n"]

    def event(self, kind: str, message: str, task_id: str | None = None):
        self.execute(
            "INSERT INTO events(task_id, kind, message, created_at) VALUES(?,?,?,?)",
            (task_id, kind, message, now()),
        )

    def create_task(
        self,
        title: str,
        instructions: str,
        *,
        parent_id: str | None = None,
        source: dict | None = None,
        project_path: str | None = None,
        backend: str | None = None,
        kind: str = "work",
    ) -> str:
        settings = self.settings()
        task_id = identifier()
        timestamp = now()
        self.execute(
            """INSERT INTO tasks(
                id,parent_id,title,instructions,role,status,project_path,source,
                backend,created_at,updated_at,kind
            ) VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?)""",
            (
                task_id,
                parent_id,
                title,
                instructions,
                "worker" if parent_id else "lead",
                settings.project_path if project_path is None else project_path,
                json.dumps(source or {}),
                backend or settings.backend,
                timestamp,
                timestamp,
                kind,
            ),
        )
        self.event("queued", title, task_id)
        return task_id

    def task(self, task_id: str) -> dict[str, Any]:
        task = self.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise LookupError("Task not found.")
        task["source"] = json.loads(task["source"])
        return task

    def update_task(self, task_id: str, **values):
        allowed = {"status", "result", "error", "rounds"}
        if not values or not values.keys() <= allowed:
            raise ValueError("Invalid task update.")
        values["updated_at"] = now()
        self.execute(
            f"UPDATE tasks SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
            (*values.values(), task_id),
        )

    def children(self, task_id: str) -> list[dict]:
        return self.all("SELECT * FROM tasks WHERE parent_id=? ORDER BY created_at", (task_id,))

    def create_approval(
        self,
        task_id: str,
        kind: str,
        question: str,
        options: list[str],
        payload: dict,
    ) -> str:
        approval_id = identifier()
        self.execute(
            """INSERT INTO approvals(
                id,task_id,kind,question,options,payload,created_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                approval_id,
                task_id,
                kind,
                question,
                json.dumps(options),
                json.dumps(payload),
                now(),
            ),
        )
        self.event("approval", question, task_id)
        return approval_id

    def approval(self, approval_id: str) -> dict:
        item = self.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not item:
            raise LookupError("Approval not found.")
        item["options"] = json.loads(item["options"])
        item["payload"] = json.loads(item["payload"])
        return item

    def remember(
        self,
        text: str,
        scope: str,
        project_path: str,
        task_id: str | None = None,
        category: str = "preference",
        evidence: str = "Explicitly entered by the owner.",
    ):
        self.execute(
            "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?)",
            (identifier(), text, scope, project_path, task_id, now(), category, evidence),
        )
        self.export_memories()

    def export_memories(self):
        folder = self.directory / "memory"
        folder.mkdir(exist_ok=True, mode=0o700)
        payload = {
            "format": 1,
            "exported_at": now(),
            "note": "Confirmed memory export. Edit or remove memories through the local UI.",
            "memories": self.all("SELECT * FROM memories ORDER BY created_at"),
        }
        fd, temporary = tempfile.mkstemp(dir=folder, prefix=".confirmed-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2)
            os.replace(temporary, folder / "confirmed.json")
        finally:
            Path(temporary).unlink(missing_ok=True)

    def save_artifact(self, task_id: str, invocation: int, result: dict):
        folder = self.directory / "artifacts"
        folder.mkdir(exist_ok=True, mode=0o700)
        path = folder / f"{task_id}-{invocation}.json"
        with path.open("w", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
        path.chmod(0o600)

    def recover(self):
        with self.db:
            self.db.execute(
                """UPDATE tasks SET status='interrupted', error=?
                WHERE status IN ('running','receiving')""",
                ("Application stopped during execution. Explicitly retry to resume.",),
            )
            self.db.execute("UPDATE approvals SET status='uncertain' WHERE status='executing'")
            self.db.execute(
                """UPDATE tasks SET status='interrupted', error=?
                WHERE id IN (SELECT task_id FROM approvals WHERE status='uncertain')""",
                ("Action interrupted; verify external state before retrying. Not replayed.",),
            )
