import json
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, overload
from uuid import uuid4

from pydantic import ValidationError

from .models import Settings

DB_NAME = "chiefofstaff.db"
# Earlier names of the same database (the project was called Work Buddy).
RENAMED_DB_NAMES = ("work-buddy-sessions.db",)
# The pre-session database, whose settings are carried over once.
LEGACY_DB_NAME = "work-buddy.db"
SESSION_FIELDS = {
    "title",
    "status",
    "activity",
    "report",
    "unread",
    "closed_at",
    "cwd",
    "branch",
    "model",
    "task",
    "effort",
    "context",
    "project",
    "repos",
    "yolo",
}
# Lists kept on each worker's record so a later request can find the worker again.
RECORD_LISTS = {"files", "links"}
SHORT_ID = re.compile(r"[0-9a-f-]+")
DRAFT_FIELDS = {"status", "outcome", "final_text", "resolved_at"}
# Followed Slack conversations stop being followed after this long without activity.
CONVERSATION_TTL = 12 * 3600
THREAD_TTL = 3 * 86400


def now() -> str:
    return datetime.now(UTC).isoformat()


def hours_ago(hours: float) -> str:
    return datetime.fromtimestamp(time.time() - hours * 3600, UTC).isoformat()


def identifier() -> str:
    return uuid4().hex


def rename_database(directory: Path):
    """Give a database from before the rename its current name (with its WAL files)."""
    if (directory / DB_NAME).exists():
        return
    for name in RENAMED_DB_NAMES:
        if (directory / name).exists():
            for suffix in ("", "-wal", "-shm"):
                if (directory / (name + suffix)).exists():
                    (directory / (name + suffix)).rename(directory / (DB_NAME + suffix))
            return


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        rename_database(directory)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(directory / DB_NAME, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, role TEXT NOT NULL, title TEXT NOT NULL,
            status TEXT NOT NULL, cwd TEXT NOT NULL DEFAULT '',
            branch TEXT NOT NULL DEFAULT '', slack INTEGER NOT NULL DEFAULT 0,
            activity TEXT NOT NULL DEFAULT '', report TEXT NOT NULL DEFAULT '',
            unread INTEGER NOT NULL DEFAULT 0, closed_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
            text TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS messages_by_session ON messages(session_id, id);
        CREATE TABLE IF NOT EXISTS triggers (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, key TEXT NOT NULL UNIQUE,
            text TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
            target TEXT NOT NULL, text TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending', final_text TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
            model TEXT NOT NULL, credits REAL NOT NULL, premium REAL NOT NULL,
            input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS usage_by_time ON usage(at);
        CREATE INDEX IF NOT EXISTS usage_by_session ON usage(session_id);
        CREATE TABLE IF NOT EXISTS subscriptions (
            thread TEXT NOT NULL, session_id TEXT NOT NULL,
            since REAL NOT NULL, touched REAL NOT NULL,
            PRIMARY KEY (thread, session_id)
        );
        CREATE TABLE IF NOT EXISTS questions (
            id TEXT PRIMARY KEY, asker TEXT NOT NULL, target TEXT NOT NULL,
            text TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open', asked REAL NOT NULL, closed REAL
        );
        CREATE INDEX IF NOT EXISTS open_questions ON questions(status, asker);
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, title TEXT NOT NULL,
            goal TEXT NOT NULL DEFAULT '', acceptance TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '',
            progress TEXT NOT NULL DEFAULT '', why_new TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, ended_at TEXT
        );
        CREATE INDEX IF NOT EXISTS tasks_by_session ON tasks(session_id, created_at);
        CREATE INDEX IF NOT EXISTS tasks_by_status ON tasks(status);
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, session_id TEXT NOT NULL,
            status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL, ended_at TEXT
        );
        CREATE INDEX IF NOT EXISTS runs_by_task ON runs(task_id);
        CREATE INDEX IF NOT EXISTS runs_by_session ON runs(session_id, ended_at);
        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '',
            call_id TEXT NOT NULL, command TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '',
            exit_code INTEGER, output TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL, ended_at TEXT
        );
        CREATE INDEX IF NOT EXISTS commands_by_run ON commands(run_id, id);
        CREATE INDEX IF NOT EXISTS commands_by_call ON commands(session_id, call_id);
        CREATE TABLE IF NOT EXISTS task_deps (
            task_id TEXT NOT NULL, depends_on TEXT NOT NULL, PRIMARY KEY (task_id, depends_on)
        );
        """)
        for row in self.db.execute("SELECT key, value FROM kv WHERE key LIKE 'question:%'"):
            # Questions between sessions used to live in kv.
            old = json.loads(row["value"])
            answered = "answer" in old
            self.db.execute(
                "INSERT OR IGNORE INTO questions VALUES (?,?,?,?,?,?,?,?)",
                (
                    row["key"].removeprefix("question:"),
                    old["asker"],
                    old["target"],
                    old["text"],
                    old.get("answer", ""),
                    "answered" if answered else "open",
                    time.time(),
                    time.time() if answered else None,
                ),
            )
        self.db.execute("DELETE FROM kv WHERE key LIKE 'question:%' OR key LIKE 'asking:%'")
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(sessions)")}
        if "model" not in columns:
            self.db.execute("ALTER TABLE sessions ADD COLUMN model TEXT NOT NULL DEFAULT ''")
        if "repos" not in columns:
            self.db.executescript("""
            ALTER TABLE sessions ADD COLUMN repos TEXT NOT NULL DEFAULT '[]';
            ALTER TABLE sessions ADD COLUMN yolo TEXT NOT NULL DEFAULT '';
            """)
        if "task_id" not in columns:
            self.db.execute("ALTER TABLE sessions ADD COLUMN task_id TEXT NOT NULL DEFAULT ''")
        run_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(runs)")}
        if "evidence" not in run_columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN evidence TEXT NOT NULL DEFAULT ''")
        usage_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(usage)")}
        if "run_id" not in usage_columns:
            self.db.execute("ALTER TABLE usage ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")
        if "project" not in columns:
            self.db.execute("ALTER TABLE sessions ADD COLUMN project TEXT NOT NULL DEFAULT ''")
        if "effort" not in columns:
            self.db.executescript("""
            ALTER TABLE sessions ADD COLUMN effort TEXT NOT NULL DEFAULT '';
            ALTER TABLE sessions ADD COLUMN context TEXT NOT NULL DEFAULT '';
            """)
        if "task" not in columns:
            self.db.executescript("""
            ALTER TABLE sessions ADD COLUMN task TEXT NOT NULL DEFAULT '';
            ALTER TABLE sessions ADD COLUMN files TEXT NOT NULL DEFAULT '[]';
            ALTER TABLE sessions ADD COLUMN links TEXT NOT NULL DEFAULT '[]';
            UPDATE sessions SET task=coalesce((SELECT text FROM messages m
                WHERE m.session_id=sessions.id AND m.kind='router' ORDER BY m.id LIMIT 1), '')
            WHERE role='worker';
            """)
        self.db.commit()
        (directory / DB_NAME).chmod(0o600)
        if not self.one("SELECT id FROM settings WHERE id=1"):
            self.migrate_legacy_settings()

    def migrate_legacy_settings(self):
        """Carry over still-meaningful settings from the pre-session database."""
        legacy = self.directory / LEGACY_DB_NAME
        if not legacy.exists():
            return
        try:
            with sqlite3.connect(legacy) as old:
                row = old.execute("SELECT value FROM settings WHERE id=1").fetchone()
            values = json.loads(row[0]) if row else {}
            settings = Settings(
                copilot_command=values.get("copilot_command") or "copilot",
                model="" if values.get("model") in (None, "auto") else values["model"],
                projects=[values["project_path"]] if values.get("project_path") else [],
                github_enabled=bool(values.get("github_enabled")),
                github_repos=values.get("github_repos") or [],
            )
        except (sqlite3.Error, json.JSONDecodeError, ValidationError, KeyError, TypeError):
            return
        self.save_settings(settings)

    def close(self):
        with self.lock:
            self.db.close()

    def all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def row(self, sql: str, params: tuple = ()) -> dict[str, Any]:
        """The one row a query must find."""
        if (found := self.one(sql, params)) is None:
            raise LookupError(f"Not found: {sql.split()[3] if len(sql.split()) > 3 else sql}")
        return found

    def value(self, sql: str, params: tuple = ()) -> Any:
        """The single value of a query that always returns one row (count, max, sum)."""
        with self.lock:
            return self.db.execute(sql, params).fetchone()[0]

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock, self.db:
            return self.db.execute(sql, params)

    def settings(self) -> Settings:
        row = self.one("SELECT value FROM settings WHERE id=1")
        if not row:
            return Settings()
        try:
            return Settings.model_validate_json(row["value"])
        except ValidationError:
            # A project directory may have been deleted since it was saved.
            values = json.loads(row["value"])
            values["projects"] = [
                path for path in values.get("projects", []) if Path(path).is_dir()
            ]
            if not Path(values.get("code_folder") or "/nonexistent").is_dir():
                values["code_folder"] = ""
            return Settings.model_validate(values)

    def save_settings(self, settings: Settings):
        self.execute("INSERT OR REPLACE INTO settings VALUES (1, ?)", (settings.model_dump_json(),))

    @overload
    def get(self, key: str) -> str | None: ...
    @overload
    def get(self, key: str, default: str) -> str: ...
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def set(self, key: str, value: str):
        self.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))

    def delete(self, key: str):
        self.execute("DELETE FROM kv WHERE key=?", (key,))

    def keys(self, prefix: str) -> list[str]:
        rows = self.all(
            "SELECT key FROM kv WHERE key >= ? AND key < ?", (prefix, prefix + "\uffff")
        )
        return [row["key"] for row in rows]

    def create_session(
        self,
        session_id: str,
        role: str,
        title: str,
        status: str,
        *,
        cwd: str = "",
        branch: str = "",
        slack: bool = False,
    ) -> dict:
        timestamp = now()
        self.execute(
            """INSERT INTO sessions(id,role,title,status,cwd,branch,slack,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (session_id, role, title, status, cwd, branch, int(slack), timestamp, timestamp),
        )
        return self.session(session_id)

    def find_session(self, session_id: str) -> dict | None:
        return self.one("SELECT * FROM sessions WHERE id=?", (session_id,))

    def session(self, session_id: str) -> dict:
        """A session by its id, or by a unique prefix of it (sessions see 8-character ids)."""
        row = self.find_session(session_id)
        if not row and 6 <= len(session_id) < 36 and SHORT_ID.fullmatch(session_id):
            rows = self.all("SELECT * FROM sessions WHERE id LIKE ? LIMIT 2", (session_id + "%",))
            if len(rows) > 1:
                raise LookupError(f"Several sessions start with {session_id}; use the full id.")
            row = rows[0] if rows else None
        if not row:
            raise LookupError("Session not found.")
        return row

    def open_sessions(self, role: str | None = None) -> list[dict]:
        if role:
            return self.all(
                "SELECT * FROM sessions WHERE closed_at IS NULL AND role=? ORDER BY created_at",
                (role,),
            )
        return self.all("SELECT * FROM sessions WHERE closed_at IS NULL ORDER BY created_at")

    def update_session(self, session_id: str, **values) -> dict:
        if not values or not values.keys() <= SESSION_FIELDS:
            raise ValueError("Invalid session update.")
        values["updated_at"] = now()
        self.execute(
            f"UPDATE sessions SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
            (*values.values(), session_id),
        )
        return self.session(session_id)

    def record(self, session_id: str, column: str, items: list[str], keep: int = 40):
        """Add to a worker's record without counting as activity."""
        if column not in RECORD_LISTS:
            raise ValueError("Invalid record list.")
        row = self.one(f"SELECT {column} FROM sessions WHERE id=?", (session_id,))
        if not row:
            return
        current = json.loads(row[column])
        added = [item for item in dict.fromkeys(items) if item and item not in current]
        if added:
            self.execute(
                f"UPDATE sessions SET {column}=? WHERE id=?",
                (json.dumps((current + added)[-keep:]), session_id),
            )

    def add_message(self, session_id: str, kind: str, text: str, meta: dict | None = None) -> dict:
        cursor = self.execute(
            "INSERT INTO messages(session_id,kind,text,meta,created_at) VALUES(?,?,?,?,?)",
            (session_id, kind, text, json.dumps(meta or {}, ensure_ascii=False), now()),
        )
        return self.message(cursor.lastrowid or 0)

    def message(self, message_id: int) -> dict:
        row = self.one("SELECT * FROM messages WHERE id=?", (message_id,))
        if not row:
            raise LookupError("Message not found.")
        row["meta"] = json.loads(row["meta"])
        return row

    def update_message_meta(self, message_id: int, meta: dict):
        self.execute(
            "UPDATE messages SET meta=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False), message_id),
        )

    def messages(self, session_id: str, after: int = 0, limit: int = 400) -> list[dict]:
        rows = self.all(
            """SELECT * FROM (SELECT * FROM messages WHERE session_id=? AND id>?
            ORDER BY id DESC LIMIT ?) ORDER BY id""",
            (session_id, after, limit),
        )
        for row in rows:
            row["meta"] = json.loads(row["meta"])
        return rows

    def add_trigger(self, source: str, key: str, text: str) -> str | None:
        trigger_id = "t" + identifier()[:8]
        try:
            self.execute(
                "INSERT INTO triggers VALUES(?,?,?,?,?)", (trigger_id, source, key, text, now())
            )
        except sqlite3.IntegrityError:
            return None
        return trigger_id

    def create_draft(
        self, session_id: str, kind: str, target: dict, text: str, note: str = ""
    ) -> dict:
        draft_id = "d" + identifier()[:8]
        self.execute(
            """INSERT INTO drafts(id,session_id,kind,target,text,note,created_at)
            VALUES(?,?,?,?,?,?,?)""",
            (draft_id, session_id, kind, json.dumps(target), text, note, now()),
        )
        return self.draft(draft_id)

    def draft(self, draft_id: str) -> dict:
        row = self.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if not row:
            raise LookupError("Draft not found.")
        row["target"] = json.loads(row["target"])
        return row

    def drafts(self, status: str = "pending") -> list[dict]:
        return [
            self.draft(row["id"])
            for row in self.all(
                "SELECT id FROM drafts WHERE status=? ORDER BY created_at", (status,)
            )
        ]

    def update_draft(self, draft_id: str, **values) -> dict:
        if not values or not values.keys() <= DRAFT_FIELDS:
            raise ValueError("Invalid draft update.")
        self.execute(
            f"UPDATE drafts SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
            (*values.values(), draft_id),
        )
        return self.draft(draft_id)

    # Thread subscriptions: later activity in a followed Slack thread, DM or PR goes straight to
    # the following sessions instead of the Router.

    def follow(self, thread: str, session_id: str, since: float | None = None):
        moment = time.time()
        self.execute(
            """INSERT INTO subscriptions VALUES (?,?,?,?)
            ON CONFLICT(thread, session_id) DO UPDATE SET touched=excluded.touched""",
            (thread, session_id, moment - 60 if since is None else since, moment),
        )

    def unfollow(self, thread: str, session_id: str | None = None):
        if session_id:
            self.execute(
                "DELETE FROM subscriptions WHERE thread=? AND session_id=?", (thread, session_id)
            )
        else:
            self.execute("DELETE FROM subscriptions WHERE thread=?", (thread,))

    def unfollow_session(self, session_id: str):
        self.execute("DELETE FROM subscriptions WHERE session_id=?", (session_id,))

    def touch(self, thread: str):
        self.execute("UPDATE subscriptions SET touched=? WHERE thread=?", (time.time(), thread))

    def follows(self, session_id: str) -> list[str]:
        return [
            row["thread"]
            for row in self.all(
                "SELECT thread FROM subscriptions WHERE session_id=? ORDER BY since", (session_id,)
            )
        ]

    def followers(self, thread: str) -> list[str]:
        return [
            row["session_id"]
            for row in self.all(
                """SELECT s.session_id FROM subscriptions s JOIN sessions x ON x.id=s.session_id
                WHERE s.thread=? AND x.closed_at IS NULL ORDER BY s.since""",
                (thread,),
            )
        ]

    def followed(self, prefix: str) -> list[dict]:
        """Threads with at least one open follower; quiet Slack conversations expire."""
        moment = time.time()
        self.execute(
            """DELETE FROM subscriptions WHERE thread LIKE 'slack:%' AND touched < (
                CASE WHEN thread LIKE 'slack:%:%' THEN ? ELSE ? END)""",
            (moment - THREAD_TTL, moment - CONVERSATION_TTL),
        )
        return self.all(
            """SELECT s.thread, min(s.since) AS since FROM subscriptions s
            JOIN sessions x ON x.id=s.session_id
            WHERE x.closed_at IS NULL AND s.thread LIKE ? GROUP BY s.thread ORDER BY s.thread""",
            (prefix + "%",),
        )

    # Usage: one row per model call, for credits per session and over any time range.

    def record_usage(
        self,
        session_id: str,
        role: str,
        model: str,
        credits: float,
        premium: float,
        input_tokens: int,
        output_tokens: int,
        at: float | None = None,
        run_id: str = "",
    ):
        self.execute(
            "INSERT INTO usage(session_id,role,model,credits,premium,input_tokens,output_tokens,at,"
            "run_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                role,
                model,
                credits,
                premium,
                input_tokens,
                output_tokens,
                time.time() if at is None else at,
                run_id,
            ),
        )

    def usage(self, since: float, until: float) -> dict:
        totals = """sum(credits) AS credits, sum(premium) AS premium, count(*) AS calls,
            sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens"""
        window = (since, until)
        total = self.row(f"SELECT {totals} FROM usage WHERE at>=? AND at<?", window)
        return {
            "since": since,
            "until": until,
            "total": {key: value or 0 for key, value in total.items()},
            "by_role": self.all(
                f"SELECT role, {totals} FROM usage WHERE at>=? AND at<? GROUP BY role"
                " ORDER BY credits DESC",
                window,
            ),
            "by_model": self.all(
                f"SELECT model, {totals} FROM usage WHERE at>=? AND at<? GROUP BY model"
                " ORDER BY credits DESC",
                window,
            ),
            "by_session": self.all(
                f"""SELECT u.session_id, s.title, s.role, s.closed_at, {totals}
                FROM usage u LEFT JOIN sessions s ON s.id=u.session_id
                WHERE u.at>=? AND u.at<? GROUP BY u.session_id ORDER BY credits DESC LIMIT 50""",
                window,
            ),
        }

    def session_credits(self) -> dict[str, float]:
        return {
            row["session_id"]: row["credits"]
            for row in self.all("SELECT session_id, sum(credits) AS credits FROM usage GROUP BY 1")
        }
