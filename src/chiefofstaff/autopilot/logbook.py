"""Autopilot's record: every action it takes, written as it happens, in tables of its own in the
main database, made when its Settings line is first ticked (never at upgrade: an install that
never ticks it has none of them).

- **Runs**: one row each time it is turned on, with its end and who ended it.
- **Entries** (`autopilot_log`): one row per action, and one more per change of its state,
  pointing at it (`of`); nothing is rewritten. Each entry's hash is SHA-256 of the previous
  entry's hash and its own canonical JSON; the head is copied to the kv store at each run's
  end. A row changed outside Chief of Staff breaks the chain, which the record says when it is
  opened. Triggers refuse any UPDATE or DELETE of an entry or a blob, from any code path, this
  version's or an older one's (evidence of a change, not protection from someone who controls
  this computer).
- **Blobs**: payloads are kept whole; a large part is a content-addressed blob (SHA-256 of its
  bytes), zlib-compressed, in parts of 256 KB, which its entry's hash covers by that hash.
- **Outbox**: what to deliver once an action is committed (a message to a worker, a card), in
  the same transaction as the change and its entry; delivered after the commit by whoever
  claims the row, once.
- **Decisions** Autopilot made, never in the shared `decisions` table (no version's
  precedents can take one for the owner's), and the owner's own **acts** it may cite.

Pure store work: no sessions, no turns."""

from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ..store import Store, now

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS autopilot_runs (
    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, until TEXT, started_by TEXT NOT NULL,
    generation INTEGER NOT NULL, rules TEXT NOT NULL, state TEXT NOT NULL,
    ended_at TEXT, ended_by TEXT NOT NULL DEFAULT '', summarized TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS autopilot_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL,
    of TEXT NOT NULL DEFAULT '', at TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
    subject TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,
    why TEXT NOT NULL, evidence TEXT NOT NULL, payload TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '', link TEXT NOT NULL DEFAULT '',
    prev TEXT NOT NULL, hash TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS autopilot_blobs (
    hash TEXT NOT NULL, part INTEGER NOT NULL, parts INTEGER NOT NULL, size INTEGER NOT NULL,
    data BLOB NOT NULL, PRIMARY KEY (hash, part))""",
    """CREATE TABLE IF NOT EXISTS autopilot_outbox (
    id TEXT PRIMARY KEY, entry TEXT NOT NULL, run_id TEXT NOT NULL, kind TEXT NOT NULL,
    payload TEXT NOT NULL, state TEXT NOT NULL, claimed_at TEXT, generation INTEGER NOT NULL,
    outcome TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, ended_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS autopilot_decisions (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, entry TEXT NOT NULL, card_id TEXT NOT NULL,
    question TEXT NOT NULL, options TEXT NOT NULL, choice TEXT NOT NULL, basis TEXT NOT NULL,
    askers TEXT NOT NULL, status TEXT NOT NULL, changed_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, changed_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS autopilot_provenance (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL, channel TEXT NOT NULL,
    text TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '{}', at TEXT NOT NULL,
    UNIQUE (kind, ref))""",
    "CREATE INDEX IF NOT EXISTS autopilot_log_run ON autopilot_log(run_id, seq)",
    "CREATE INDEX IF NOT EXISTS autopilot_log_task ON autopilot_log(task_id, seq)",
    "CREATE INDEX IF NOT EXISTS autopilot_log_of ON autopilot_log(of)",
    "CREATE INDEX IF NOT EXISTS autopilot_outbox_state ON autopilot_outbox(state, created_at)",
    "CREATE INDEX IF NOT EXISTS autopilot_decisions_card ON autopilot_decisions(card_id)",
    *(
        f"""CREATE TRIGGER IF NOT EXISTS {table}_kept_{when} BEFORE {when.upper()} ON {table}
        BEGIN SELECT RAISE(ABORT, 'The Autopilot record is kept as it was written.'); END"""
        for table in ("autopilot_log", "autopilot_blobs")
        for when in ("update", "delete")
    ),
)
TABLES = (
    "autopilot_runs", "autopilot_log", "autopilot_blobs", "autopilot_outbox",
    "autopilot_decisions", "autopilot_provenance",
)  # fmt: skip

# A run's states.
ON, STOPPING, ENDED = "on", "stopping", "ended"
# An entry's state: what became of its action as of that entry.
DOING, DONE, FAILED, UNKNOWN = "doing", "done", "failed", "unknown"
PARKED, WITHDRAWN, UNDONE = "parked", "withdrawn", "undone"
# An outbox item's states: queued (committed, not delivered), claimed (being delivered: its
# outcome is unknown until it is recorded), and its ends.
QUEUED, CLAIMED = "queued", "claimed"
ITEM_ENDED = frozenset({DONE, FAILED, WITHDRAWN, UNKNOWN})
# An Autopilot decision's states: in force, or changed by the owner (Change).
ACTIVE, CHANGED = "active", "changed"

# The hash before the first entry.
GENESIS = "0" * 64
# A blob's parts, compressed; a payload's strings longer than this are kept as blobs.
PART = 256 * 1024
INLINE = 8192
# What an entry keeps of a blob's text, to show folded.
HEAD = 400
BLOB = "$blob"
# What each entry's hash covers, in this order (its why, evidence and payload as stored).
HASHED = (
    "id", "run_id", "of", "at", "kind", "state", "subject", "task_id", "title", "why",
    "evidence", "payload", "outcome", "link",
)  # fmt: skip
ENTRY_JSON = ("why", "evidence", "payload")
HEAD_KEY = "autopilot:head"
GENERATION_KEY = "autopilot:generation"


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def chained(prev: str, fields: dict) -> str:
    """An entry's hash: the previous entry's hash and the entry's own fields."""
    body = canonical({name: fields[name] for name in HASHED})
    return hashlib.sha256(f"{prev}\n{body}".encode()).hexdigest()


def loaded(row: dict | None, *fields: str) -> dict | None:
    if row is None:
        return None
    for name in fields:
        if isinstance(row.get(name), str):
            try:
                row[name] = json.loads(row[name] or "null")
            except ValueError:
                row[name] = None
    return row


@dataclass(frozen=True)
class Chain:
    """What opening the record found: whether every entry holds; if not, the first that does
    not (`seq`) and the time of the last that does (`after`, "" when none does)."""

    ok: bool
    entries: int
    seq: int = 0
    after: str = ""


class Logbook:
    """The record's tables, and every change to them."""

    def __init__(self, store: Store):
        self.store = store
        self.made = False

    # Made on first use (the Settings line ticked).

    def exists(self) -> bool:
        if self.made:
            return True
        found = bool(
            self.store.one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='autopilot_log'"
            )
        )
        # Inside a transaction it could still be rolled back: known for good only outside.
        self.made = found and not self.store.db.depth
        return found

    def ensure(self):
        """Its tables, once. Statement by statement (never executescript, which commits): it
        may run inside a transaction."""
        if self.exists():
            return
        with self.store.transaction():
            for statement in STATEMENTS:
                self.store.execute(statement)
        self.made = not self.store.db.depth

    # The generation: up at each Stop, so nothing queued before it is delivered after.

    def generation(self) -> int:
        return int(self.store.get(GENERATION_KEY) or 0)

    def bump(self) -> int:
        with self.store.transaction():
            value = self.generation() + 1
            self.store.set(GENERATION_KEY, str(value))
        return value

    # Runs.

    def new_run(self, *, until: str | None, started_by: str, rules: dict) -> dict:
        """A run, on now (its `started` entry is the caller's, in the same transaction)."""
        run_id = "r" + uuid4().hex[:10]
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO autopilot_runs(id, started_at, until, started_by, generation,
                rules, state) VALUES (?,?,?,?,?,?,?)""",
                (run_id, now(), until, started_by, self.generation(), canonical(rules), ON),
            )
        return self.run(run_id) or {}

    def run(self, run_id: str) -> dict | None:
        if not run_id or not self.exists():
            return None
        return loaded(self.store.one("SELECT * FROM autopilot_runs WHERE id=?", (run_id,)), "rules")

    def runs(self, limit: int = 50) -> list[dict]:
        """Newest first."""
        if not self.exists():
            return []
        rows = self.store.all(
            "SELECT * FROM autopilot_runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
        )
        return [loaded(row, "rules") or {} for row in rows]

    def open_runs(self) -> list[dict]:
        """Runs not ended (one at most, but for a crash between two writes)."""
        if not self.exists():
            return []
        rows = self.store.all(
            "SELECT * FROM autopilot_runs WHERE state!=? ORDER BY started_at", (ENDED,)
        )
        return [loaded(row, "rules") or {} for row in rows]

    def move_run(self, run_id: str, expected: str | tuple[str, ...], **values: Any) -> bool:
        """A compare-and-set on the run's state: its until, its stopping, its end."""
        allowed = {"state", "until", "ended_at", "ended_by", "summarized"}
        if not values or set(values) - allowed:
            raise ValueError(f"Not a run field to change: {sorted(set(values) - allowed)}")
        states = (expected,) if isinstance(expected, str) else expected
        columns = ", ".join(f"{key}=?" for key in values)
        marks = ",".join("?" for _ in states)
        cursor = self.store.execute(
            f"UPDATE autopilot_runs SET {columns} WHERE id=? AND state IN ({marks})",  # noqa: S608
            (*values.values(), run_id, *states),
        )
        return bool(cursor.rowcount)

    # Entries.

    def append(
        self,
        run_id: str,
        kind: str,
        title: str,
        *,
        state: str = DONE,
        of: str = "",
        subject: str = "",
        task_id: str = "",
        why: Any = None,
        evidence: Any = None,
        payload: Any = None,
        outcome: str = "",
        link: str = "",
    ) -> dict:
        """One entry, chained to the last (in the caller's transaction, if there is one)."""
        with self.store.transaction():
            fields = {
                "id": "e" + uuid4().hex[:12],
                "run_id": run_id,
                "of": of,
                "at": now(),
                "kind": kind,
                "state": state,
                "subject": subject,
                "task_id": task_id,
                "title": title,
                "why": canonical(why if why is not None else {}),
                "evidence": canonical(self.kept(evidence if evidence is not None else {})),
                "payload": canonical(self.kept(payload if payload is not None else {})),
                "outcome": outcome,
                "link": link,
            }
            last = self.store.one("SELECT hash FROM autopilot_log ORDER BY seq DESC LIMIT 1")
            prev = last["hash"] if last else GENESIS
            fields["prev"] = prev
            fields["hash"] = chained(prev, fields)
            self.store.execute(
                f"INSERT INTO autopilot_log({', '.join(fields)}) "  # noqa: S608
                f"VALUES ({', '.join('?' for _ in fields)})",
                tuple(fields.values()),
            )
        return self.entry(fields["id"]) or {}

    def entry(self, entry_id: str) -> dict | None:
        if not entry_id or not self.exists():
            return None
        row = self.store.one("SELECT * FROM autopilot_log WHERE id=?", (entry_id,))
        return loaded(row, *ENTRY_JSON)

    def entries(
        self,
        *,
        run_id: str = "",
        task_id: str = "",
        of: str = "",
        kinds: tuple[str, ...] = (),
        since: str = "",
        until: str = "",
        limit: int = 1000,
    ) -> list[dict]:
        """Oldest first, as written."""
        if not self.exists():
            return []
        where, params = [], []
        for column, value in (("run_id", run_id), ("task_id", task_id), ("of", of)):
            if value:
                where.append(f"{column}=?")
                params.append(value)
        if kinds:
            where.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params += kinds
        if since:
            where.append("at>=?")
            params.append(since)
        if until:
            where.append("at<?")
            params.append(until)
        condition = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.store.all(
            f"SELECT * FROM (SELECT * FROM autopilot_log {condition} "  # noqa: S608
            "ORDER BY seq DESC LIMIT ?) ORDER BY seq",
            (*params, limit),
        )
        return [loaded(row, *ENTRY_JSON) or {} for row in rows]

    def latest(self, entry_id: str) -> dict | None:
        """An action's latest state: its own entry, or the last entry about it."""
        later = self.store.one(
            "SELECT * FROM autopilot_log WHERE of=? ORDER BY seq DESC LIMIT 1", (entry_id,)
        )
        return loaded(later, *ENTRY_JSON) if later else self.entry(entry_id)

    # Blobs: large parts of payloads, kept whole.

    def put(self, raw: bytes) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        packed = zlib.compress(raw, 6)
        parts = [packed[start : start + PART] for start in range(0, len(packed), PART)]
        with self.store.transaction():
            for index, data in enumerate(parts):
                self.store.execute(
                    "INSERT OR IGNORE INTO autopilot_blobs VALUES (?,?,?,?,?)",
                    (digest, index, len(parts), len(raw), data),
                )
        return digest

    def blob(self, digest: str) -> bytes | None:
        """A blob's bytes, or None when it is missing or was changed."""
        rows = self.store.all(
            "SELECT part, parts, size, data FROM autopilot_blobs WHERE hash=? ORDER BY part",
            (digest,),
        )
        if not rows or [row["part"] for row in rows] != list(range(rows[0]["parts"])):
            return None
        try:
            raw = zlib.decompress(b"".join(bytes(row["data"]) for row in rows))
        except zlib.error:
            return None
        if len(raw) != rows[0]["size"] or hashlib.sha256(raw).hexdigest() != digest:
            return None
        return raw

    def kept(self, value: Any) -> Any:
        """A payload as stored: each string longer than INLINE as a blob, its start kept."""
        if isinstance(value, str) and len(value) > INLINE:
            raw = value.encode()
            return {BLOB: self.put(raw), "size": len(raw), "head": value[:HEAD]}
        if isinstance(value, dict):
            return {key: self.kept(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.kept(item) for item in value]
        return value

    def whole(self, value: Any) -> Any:
        """A stored payload with its blobs read back; one missing or changed stays a reference,
        marked `changed`."""
        if isinstance(value, dict):
            if isinstance(value.get(BLOB), str) and set(value) <= {BLOB, "size", "head"}:
                raw = self.blob(value[BLOB])
                return (
                    raw.decode(errors="replace") if raw is not None else {**value, "changed": True}
                )
            return {key: self.whole(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.whole(item) for item in value]
        return value

    # The chain.

    def check(self) -> Chain:
        """Every entry against its hash and the one before it, and the head kept at the last
        run's end against the entry it names."""
        if not self.exists():
            return Chain(True, 0)
        rows = self.store.all("SELECT * FROM autopilot_log ORDER BY seq")
        prev, after = GENESIS, ""
        for row in rows:
            if row["prev"] != prev or chained(prev, row) != row["hash"]:
                return Chain(False, len(rows), row["seq"], after)
            prev, after = row["hash"], row["at"]
        kept = (self.store.get(HEAD_KEY) or "").split(":", 1)
        if len(kept) == 2 and kept[0].isdigit():
            seq, digest = int(kept[0]), kept[1]
            found = next((row for row in rows if row["seq"] == seq), None)
            if found is None or found["hash"] != digest:
                # Rewritten from there on, or entries gone from the end.
                before = [row["at"] for row in rows if row["seq"] < seq]
                return Chain(False, len(rows), seq, before[-1] if before else "")
        return Chain(True, len(rows))

    def keep_head(self):
        """The last entry's hash, copied to the kv store (at each run's end)."""
        last = self.store.one("SELECT seq, hash FROM autopilot_log ORDER BY seq DESC LIMIT 1")
        if last:
            self.store.set(HEAD_KEY, f"{last['seq']}:{last['hash']}")

    def size(self) -> int:
        """Bytes the record takes (its entries and blobs, compressed as kept)."""
        if not self.exists():
            return 0
        return int(
            self.store.value(
                """SELECT coalesce((SELECT sum(length(data)) FROM autopilot_blobs), 0)
                + coalesce((SELECT sum(length(title) + length(why) + length(evidence)
                    + length(payload) + length(outcome) + 200) FROM autopilot_log), 0)"""
            )
        )

    # The outbox.

    def queue(self, run_id: str, entry_id: str, kind: str, payload: dict, generation: int) -> str:
        """What to deliver once the transaction it is written in commits."""
        item_id = "o" + uuid4().hex[:12]
        self.store.execute(
            """INSERT INTO autopilot_outbox(id, entry, run_id, kind, payload, state, generation,
            created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (item_id, entry_id, run_id, kind, canonical(payload), QUEUED, generation, now()),
        )
        return item_id

    def item(self, item_id: str) -> dict | None:
        if not self.exists():
            return None
        row = self.store.one("SELECT * FROM autopilot_outbox WHERE id=?", (item_id,))
        return loaded(row, "payload")

    def outbox(self, *states: str) -> list[dict]:
        """Oldest first."""
        if not self.exists():
            return []
        states = states or (QUEUED,)
        rows = self.store.all(
            f"SELECT * FROM autopilot_outbox WHERE state IN ({','.join('?' for _ in states)}) "  # noqa: S608
            "ORDER BY created_at, rowid",
            states,
        )
        return [loaded(row, "payload") or {} for row in rows]

    def claim(self, item_id: str) -> dict | None:
        """The item to deliver now, once: None when it was claimed, withdrawn or delivered
        already, or queued before the last Stop (then withdrawn, with its entry)."""
        with self.store.transaction():
            row = self.item(item_id)
            if not row or row["state"] != QUEUED:
                return None
            if row["generation"] != self.generation():
                self.withdraw_item(row, "Autopilot was stopped before it went out.")
                return None
            self.store.execute(
                "UPDATE autopilot_outbox SET state=?, claimed_at=? WHERE id=? AND state=?",
                (CLAIMED, now(), item_id, QUEUED),
            )
        return self.item(item_id)

    def finish(self, item_id: str, state: str, outcome: str = "") -> bool:
        """A claimed item's outcome."""
        if state not in ITEM_ENDED:
            raise ValueError(f"Not an outbox outcome: {state}")
        cursor = self.store.execute(
            "UPDATE autopilot_outbox SET state=?, outcome=?, ended_at=? WHERE id=? AND state=?",
            (state, outcome[:2000], now(), item_id, CLAIMED),
        )
        return bool(cursor.rowcount)

    def withdraw_item(self, row: dict, why: str) -> bool:
        """A queued item that will not go out, and its `withdrawn` entry (one transaction)."""
        with self.store.transaction():
            cursor = self.store.execute(
                "UPDATE autopilot_outbox SET state=?, outcome=?, ended_at=? WHERE id=? AND state=?",
                (WITHDRAWN, why, now(), row["id"], QUEUED),
            )
            if not cursor.rowcount:
                return False
            action = self.entry(row["entry"]) or {}
            self.append(
                row["run_id"],
                WITHDRAWN,
                f"Withdrawn: {action.get('title') or row['kind']}",
                state=WITHDRAWN,
                of=row["entry"],
                subject=action.get("subject") or "",
                task_id=action.get("task_id") or "",
                why={"rule": "stopped", "reason": why},
                payload={"item": row["id"], "kind": row["kind"]},
            )
        return True

    def withdraw(self, why: str, run_id: str = "") -> int:
        """Every queued item (of one run, or all): none of them goes out."""
        with self.store.transaction():
            rows = [row for row in self.outbox(QUEUED) if not run_id or row["run_id"] == run_id]
            return sum(self.withdraw_item(row, why) for row in rows)

    # Autopilot's decisions: never in `decisions` or `task_decisions`.

    def decide(
        self,
        *,
        run_id: str,
        entry_id: str,
        card_id: str,
        question: str,
        options: list,
        choice: str,
        basis: list,
        askers: list,
    ) -> str:
        decision_id = "a" + uuid4().hex[:10]
        self.store.execute(
            """INSERT INTO autopilot_decisions(id, run_id, entry, card_id, question, options,
            choice, basis, askers, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (decision_id, run_id, entry_id, card_id, question, canonical(options), choice,
             canonical(basis), canonical(askers), ACTIVE, now()),
        )  # fmt: skip
        return decision_id

    def decision(self, decision_id: str) -> dict | None:
        if not self.exists():
            return None
        row = self.store.one("SELECT * FROM autopilot_decisions WHERE id=?", (decision_id,))
        return loaded(row, "options", "basis", "askers")

    def decisions(self, *, card_id: str = "", task_id: str = "", status: str = "") -> list[dict]:
        if not self.exists():
            return []
        where, params = [], []
        if card_id:
            where.append("card_id=?")
            params.append(card_id)
        if task_id:
            where.append(
                "EXISTS (SELECT 1 FROM json_each(askers) WHERE json_extract(value, '$.task_id')=?)"
            )
            params.append(task_id)
        if status:
            where.append("status=?")
            params.append(status)
        condition = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.store.all(
            f"SELECT * FROM autopilot_decisions {condition} ORDER BY created_at, rowid",  # noqa: S608
            tuple(params),
        )
        return [loaded(row, "options", "basis", "askers") or {} for row in rows]

    def change_decision(self, decision_id: str, by: str) -> bool:
        """The owner changed it: no longer in force (compare-and-set: once)."""
        cursor = self.store.execute(
            """UPDATE autopilot_decisions SET status=?, changed_by=?, changed_at=?
            WHERE id=? AND status=?""",
            (CHANGED, by, now(), decision_id, ACTIVE),
        )
        return bool(cursor.rowcount)

    # The owner's own acts it may cite (a card answered in the page, say), by kind and id.

    def owner_act(
        self, kind: str, ref: str, text: str, *, channel: str, scope: dict | None = None
    ) -> dict:
        self.store.execute(
            """INSERT OR IGNORE INTO autopilot_provenance(id, kind, ref, channel, text, scope, at)
            VALUES (?,?,?,?,?,?,?)""",
            ("p" + uuid4().hex[:10], kind, ref, channel, text, canonical(scope or {}), now()),
        )
        return self.act(kind, ref) or {}

    def act(self, kind: str, ref: str) -> dict | None:
        if not self.exists():
            return None
        row = self.store.one(
            "SELECT * FROM autopilot_provenance WHERE kind=? AND ref=?", (kind, ref)
        )
        return loaded(row, "scope")
