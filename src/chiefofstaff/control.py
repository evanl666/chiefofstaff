"""The shepherd's control plane (shepherd.py): who holds each PR (a claim), the attempts at its
steps, the journal of what Chief of Staff publishes, the day's credit ledger and the owner's
verdicts. In the main database, so a claim, a dispatch and the consent generation are checked in
one transaction (the store runs in WAL mode, where a transaction across attached databases is not
atomic). Made when the feature is first turned on, never before: an install that never turns it
on has none of these tables.

Pure store work: no GitHub, no sessions. Every change that decides something (admitting an
attempt, approving a plan, dispatching an operation, handing a PR over) is a compare-and-set on
the claim's holder and generation, in the transaction that writes it."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .store import Store, now

TABLES = """
CREATE TABLE IF NOT EXISTS pr_claims (
    target TEXT PRIMARY KEY, holder TEXT NOT NULL DEFAULT '',
    holder_kind TEXT NOT NULL DEFAULT 'owner', origin_task TEXT NOT NULL DEFAULT '',
    accepted TEXT NOT NULL DEFAULT '{}', approved TEXT NOT NULL DEFAULT '[]',
    published TEXT NOT NULL DEFAULT '', resumed TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'idle',
    reason TEXT NOT NULL DEFAULT '', paused_head TEXT NOT NULL DEFAULT '',
    keep INTEGER NOT NULL DEFAULT 1, merge_when_ready INTEGER NOT NULL DEFAULT 0,
    head TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '{}',
    read_at REAL NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shepherd_steps (
    id TEXT PRIMARY KEY, target TEXT NOT NULL, kind TEXT NOT NULL, level TEXT NOT NULL,
    executor TEXT NOT NULL DEFAULT 'worker', session_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '', holder TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 0, consent INTEGER NOT NULL DEFAULT 0,
    h0 TEXT NOT NULL DEFAULT '', h1 TEXT NOT NULL DEFAULT '',
    signal TEXT NOT NULL DEFAULT '{}', fingerprint TEXT NOT NULL DEFAULT '',
    threads TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL,
    clicked INTEGER NOT NULL DEFAULT 0, plan TEXT NOT NULL DEFAULT '{}',
    evidence TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT '{}',
    outcome TEXT NOT NULL DEFAULT '', why TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '', day TEXT NOT NULL,
    reserved REAL NOT NULL DEFAULT 0, permitted REAL NOT NULL DEFAULT 0,
    settled REAL, folder TEXT NOT NULL DEFAULT '', tree TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS shepherd_steps_by_target ON shepherd_steps(target, created_at);
CREATE INDEX IF NOT EXISTS shepherd_steps_by_state ON shepherd_steps(state);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY, step_id TEXT NOT NULL, target TEXT NOT NULL, position INTEGER NOT NULL,
    kind TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '', link TEXT NOT NULL DEFAULT '',
    account TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS actions_by_step ON actions(step_id, position);
CREATE INDEX IF NOT EXISTS actions_by_state ON actions(state);
CREATE TABLE IF NOT EXISTS ledger_events (
    step_id TEXT NOT NULL, event_id TEXT NOT NULL, credits REAL NOT NULL,
    at REAL NOT NULL, PRIMARY KEY (step_id, event_id)
);
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY, step_id TEXT NOT NULL, kind TEXT NOT NULL, repo TEXT NOT NULL,
    verdict TEXT NOT NULL, at TEXT NOT NULL
);
"""

# An attempt's states. Waiting to run; running in its worker; prepared (a plan or a diagnosis
# waits for the owner); applying (approved, its operations go out); and its ends.
QUEUED, RUNNING, PREPARED, APPLYING = "queued", "running", "prepared", "applying"
ENDED = frozenset(
    {"published", "partly", "diagnosis", "dismissed", "withdrawn", "stale", "interrupted",
     "failed", "refused", "nothing"}
)  # fmt: skip
OPEN = frozenset({QUEUED, RUNNING, PREPARED, APPLYING})
# An operation's states: planned (in an approved plan, not sent), dispatching (being sent: its
# outcome is unknown until it is journaled), and its ends.
PLANNED, DISPATCHING = "planned", "dispatching"
# Why an operation approved too long ago is withdrawn (publish.APPROVAL_LASTS): what it left
# unsent is the owner's to review again (publish.review_again).
AGED = "you approved it over an hour ago"
OP_ENDED = frozenset({"sent", "failed", "uncertain", "withdrawn", "skipped"})
# A claim's states.
IDLE, STEPPING, HANDING, PAUSED, CLAIM_ENDED = "idle", "stepping", "handing-over", "paused", "ended"
# Attempt caps (every attempt counts, whatever its end): per kind and PR a day, per check
# fingerprint and head, per review thread.
PER_DAY = 3
PER_FINGERPRINT = 1
PER_THREAD = 2


def today(moment: float | None = None) -> str:
    """The day in the owner's time zone (this computer's)."""
    return (
        datetime.fromtimestamp(time.time() if moment is None else moment)
        .astimezone()
        .date()
        .isoformat()
    )


def loaded(row: dict | None, *fields: str) -> dict | None:
    """A row with its JSON columns read."""
    if row is None:
        return None
    for name in fields:
        if name in row and isinstance(row[name], str):
            try:
                row[name] = json.loads(row[name] or "null")
            except ValueError:
                row[name] = None
    return row


CLAIM_JSON = ("accepted", "approved", "details")
# What a plan keeps past its retention (control.prune_plans): what its journal shows.
PLAN_KEPT = ("kind", "h0", "h1", "summary", "change", "repo", "number")
STEP_JSON = ("signal", "threads", "plan", "evidence", "result")
ACTION_JSON = ("payload",)


class Control:
    """The tables, and every change to them that decides something."""

    def __init__(self, store: Store):
        self.store = store
        self.made = False

    # Made on first use while on.

    def exists(self) -> bool:
        if self.made:
            return True
        self.made = bool(
            self.store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='pr_claims'")
        )
        return self.made

    def ensure(self):
        if self.exists():
            return
        with self.store.lock:
            self.store.db.executescript(TABLES)
        self.made = True

    # The consent generation: up each time the feature is turned off (or paused), checked at
    # every admission, approval and dispatch.

    def generation(self) -> int:
        return int(self.store.get("proactive:generation") or 0)

    def bump(self) -> int:
        with self.store.lock, self.store.db:
            row = self.store.db.execute(
                "SELECT value FROM kv WHERE key='proactive:generation'"
            ).fetchone()
            value = int(row[0]) + 1 if row else 1
            self.store.db.execute(
                "INSERT OR REPLACE INTO kv VALUES ('proactive:generation', ?)", (str(value),)
            )
        return value

    # Claims: one row per PR, its holder a task or the owner.

    def claim(self, target: str) -> dict | None:
        if not self.exists():
            return None
        return loaded(
            self.store.one("SELECT * FROM pr_claims WHERE target=?", (target,)), *CLAIM_JSON
        )

    def claims(self, *states: str) -> list[dict]:
        if not self.exists():
            return []
        if states:
            marks = ",".join("?" for _ in states)
            rows = self.store.all(
                f"SELECT * FROM pr_claims WHERE state IN ({marks}) ORDER BY created_at",  # noqa: S608
                tuple(states),
            )
        else:
            rows = self.store.all("SELECT * FROM pr_claims ORDER BY created_at")
        return [loaded(row, *CLAIM_JSON) or {} for row in rows]

    def held_by(self, task_id: str) -> list[dict]:
        """The claims a task holds, or made (its origin): ended ones too."""
        if not self.exists() or not task_id:
            return []
        rows = self.store.all(
            "SELECT * FROM pr_claims WHERE holder=? OR origin_task=? ORDER BY created_at",
            (task_id, task_id),
        )
        return [loaded(row, *CLAIM_JSON) or {} for row in rows]

    def give(self, target: str, task_id: str, head: str, *, paused: str = "") -> dict:
        """The claim to a task: made for it (its origin then) when there is none, or handed to it
        from the owner (after draining, see Shepherd.hand_over). The head it holds now is the
        base it explains. `paused`: given back paused, until Resume (a reopened worker)."""
        stamp = now()
        with self.store.lock, self.store.db:
            row = self.store.db.execute(
                "SELECT holder, origin_task, state FROM pr_claims WHERE target=?", (target,)
            ).fetchone()
            if row is None:
                self.store.db.execute(
                    """INSERT INTO pr_claims(target, holder, holder_kind, origin_task, resumed,
                    head, state, reason, paused_head, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (target, task_id, "task", task_id, head, head,
                     PAUSED if paused else IDLE, paused, head if paused else "", stamp, stamp),
                )  # fmt: skip
            else:
                origin = row["origin_task"] or task_id
                state = PAUSED if paused else IDLE
                self.store.db.execute(
                    """UPDATE pr_claims SET holder=?, holder_kind='task', origin_task=?,
                    generation=generation+1, state=?, reason=?, keep=1,
                    resumed=CASE WHEN ?='' THEN ? ELSE resumed END,
                    paused_head=CASE WHEN ?='' THEN '' ELSE paused_head END, updated_at=?
                    WHERE target=?""",
                    (task_id, origin, state, paused, paused, head or "", paused, stamp, target),
                )  # fmt: skip
        return self.claim(target) or {}

    def to_owner(self, target: str, reason: str, head: str = "") -> dict | None:
        """The claim back to the owner, paused, saying why (its worker closed, a head nobody
        explains, a stack): its origin, acceptance and approved heads kept. Only Resume, or a
        task given it again, gives it back."""
        with self.store.lock, self.store.db:
            found = self.store.db.execute(
                "SELECT state FROM pr_claims WHERE target=?", (target,)
            ).fetchone()
            if found is None or found["state"] == CLAIM_ENDED:
                return None
            self.store.db.execute(
                """UPDATE pr_claims SET holder='', holder_kind='owner', state=?, reason=?,
                paused_head=?, generation=generation+1, updated_at=? WHERE target=?""",
                (PAUSED, reason, head, now(), target),
            )
        return self.claim(target)

    def pause(self, target: str, reason: str, head: str = "") -> dict | None:
        """Paused where it is (its holder kept): a head nobody explains, a PR that became
        stacked. Nothing is admitted until Resume."""
        with self.store.lock, self.store.db:
            cursor = self.store.db.execute(
                """UPDATE pr_claims SET state=?, reason=?, paused_head=?, generation=generation+1,
                updated_at=? WHERE target=? AND state NOT IN (?, ?)""",
                (PAUSED, reason, head, now(), target, CLAIM_ENDED, HANDING),
            )
        return self.claim(target) if cursor.rowcount else None

    def resume(self, target: str, head: str) -> dict | None:
        """The owner's Resume: the head as it is now is the base the journal explains. Held by a
        task again only if a task holds it (one given back to the owner stays theirs)."""
        with self.store.lock, self.store.db:
            cursor = self.store.db.execute(
                """UPDATE pr_claims SET state=?, reason='', paused_head='', resumed=?,
                generation=generation+1, updated_at=? WHERE target=? AND state=?""",
                (IDLE, head, now(), target, PAUSED),
            )
        return self.claim(target) if cursor.rowcount else None

    def end(self, target: str, reason: str) -> dict | None:
        """Merged, or closed for good: the row stays for history."""
        with self.store.lock, self.store.db:
            cursor = self.store.db.execute(
                """UPDATE pr_claims SET state=?, reason=?, generation=generation+1, updated_at=?
                WHERE target=? AND state!=?""",
                (CLAIM_ENDED, reason, now(), target, CLAIM_ENDED),
            )
        return self.claim(target) if cursor.rowcount else None

    def set_state(
        self, target: str, expected: tuple[str, int], state: str, reason: str = ""
    ) -> bool:
        """A compare-and-set on (holder, generation): stepping, handing-over, idle again."""
        holder, generation = expected
        with self.store.lock, self.store.db:
            cursor = self.store.db.execute(
                """UPDATE pr_claims SET state=?, reason=?, updated_at=?
                WHERE target=? AND holder=? AND generation=?""",
                (state, reason, now(), target, holder, generation),
            )
        return bool(cursor.rowcount)

    def update_claim(self, target: str, **values: Any):
        """Fields that decide nothing by themselves: the last read, Keep it moving, Merge when
        ready."""
        allowed = {"head", "details", "read_at", "keep", "merge_when_ready", "delivered"}
        if not values or set(values) - allowed:
            raise ValueError(f"Not a claim field to update: {sorted(set(values) - allowed)}")
        values = {k: json.dumps(v) if k == "details" else v for k, v in values.items()}
        columns = ", ".join(f"{key}=?" for key in values)
        self.store.execute(
            f"UPDATE pr_claims SET {columns}, updated_at=? WHERE target=?",  # noqa: S608
            (*values.values(), now(), target),
        )

    @staticmethod
    def explains(claim: dict, head: str) -> bool:
        """Whether the journal explains this head: the last one Chief of Staff published, or the
        head at the claim's last Resume (or when it was given). Nothing else: who wrote the
        commits decides only what is news, never ownership."""
        return bool(head) and head in {claim.get("published"), claim.get("resumed")}

    def accept(self, target: str, sha: str, task_id: str) -> dict | None:
        """The head the owner accepted (the one whose diff they reviewed): approved heads since
        an earlier acceptance no longer count."""
        accepted = json.dumps({"sha": sha, "at": now(), "task": task_id})
        with self.store.lock, self.store.db:
            cursor = self.store.db.execute(
                "UPDATE pr_claims SET accepted=?, approved='[]', updated_at=? WHERE target=?",
                (accepted, now(), target),
            )
        return self.claim(target) if cursor.rowcount else None

    def published(self, target: str, sha: str):
        """A head Chief of Staff pushed from a plan the owner approved: explained, and approved
        for merging after the accepted one."""
        with self.store.lock, self.store.db:
            row = self.store.db.execute(
                "SELECT approved FROM pr_claims WHERE target=?", (target,)
            ).fetchone()
            if row is None:
                return
            approved = json.loads(row["approved"] or "[]")
            if sha not in approved:
                approved.append(sha)
            self.store.db.execute(
                "UPDATE pr_claims SET published=?, approved=?, updated_at=? WHERE target=?",
                (sha, json.dumps(approved[-50:]), now(), target),
            )

    # Attempts.

    def add_step(self, target: str, kind: str, level: str, **values: Any) -> dict:
        fields = {"id": "s" + uuid4().hex[:10], "target": target, "kind": kind, "level": level,
                  "state": QUEUED, "day": today(), "created_at": now(), **values}  # fmt: skip
        fields = {k: json.dumps(v) if k in STEP_JSON else v for k, v in fields.items()}
        self.store.execute(
            f"INSERT INTO shepherd_steps({', '.join(fields)}) "  # noqa: S608
            f"VALUES ({', '.join('?' for _ in fields)})",
            tuple(fields.values()),
        )
        return self.step(fields["id"]) or {}

    def step(self, step_id: str) -> dict | None:
        if not self.exists():
            return None
        return loaded(
            self.store.one("SELECT * FROM shepherd_steps WHERE id=?", (step_id,)), *STEP_JSON
        )

    def steps(self, target: str = "", *states: str) -> list[dict]:
        if not self.exists():
            return []
        query, params = "SELECT * FROM shepherd_steps WHERE 1=1", []
        if target:
            query += " AND target=?"
            params.append(target)
        if states:
            query += f" AND state IN ({','.join('?' for _ in states)})"
            params += list(states)
        rows = self.store.all(query + " ORDER BY created_at, rowid", tuple(params))
        return [loaded(row, *STEP_JSON) or {} for row in rows]

    def open_step(self, target: str) -> dict | None:
        """The PR's attempt not ended yet (one at a time)."""
        found = self.steps(target, *OPEN)
        return found[-1] if found else None

    def session_step(self, session_id: str) -> dict | None:
        """The attempt running in this worker now (one per worker at a time)."""
        if not self.exists():
            return None
        return loaded(
            self.store.one(
                "SELECT * FROM shepherd_steps WHERE session_id=? AND state=? ORDER BY created_at "
                "DESC",
                (session_id, RUNNING),
            ),
            *STEP_JSON,
        )

    def move_step(self, step_id: str, expected: str | Iterable[str], **values: Any) -> dict | None:
        """A compare-and-set on the attempt's state: None when it was no longer `expected` (a
        second tab's approval, a withdrawal meanwhile)."""
        states = [expected] if isinstance(expected, str) else list(expected)
        values = {k: json.dumps(v) if k in STEP_JSON else v for k, v in values.items()}
        columns = ", ".join(f"{key}=?" for key in values)
        marks = ",".join("?" for _ in states)
        cursor = self.store.execute(
            f"UPDATE shepherd_steps SET {columns} WHERE id=? AND state IN ({marks})",  # noqa: S608
            (*values.values(), step_id, *states),
        )
        return self.step(step_id) if cursor.rowcount else None

    def admit(
        self,
        step_id: str,
        holder: str,
        generation: int,
        consent: int,
        budget: float,
        reserve: float,
        permitted: float,
    ) -> str:
        """Admit a queued attempt, in one transaction: the claim still held by `holder` at
        `generation` and not paused or changing hands, the consent generation unchanged, and
        (unless it was clicked) its reservation within the day's budget with every reservation
        still open and what was settled today. "" when admitted, else why not."""
        with self.store.lock, self.store.db:
            db = self.store.db
            step = db.execute("SELECT * FROM shepherd_steps WHERE id=?", (step_id,)).fetchone()
            if step is None or step["state"] != QUEUED:
                return "it is no longer waiting"
            if int(self.store_value(db, "proactive:generation") or 0) != consent:
                return "Prepared for you was turned off meanwhile"
            claim = db.execute(
                "SELECT holder, generation, state FROM pr_claims WHERE target=?", (step["target"],)
            ).fetchone()
            if claim is None or (claim["holder"], claim["generation"]) != (holder, generation):
                return "the PR changed hands"
            if claim["state"] != IDLE:
                return {PAUSED: "it is paused", HANDING: "it is changing hands"}.get(
                    claim["state"], f"it is {claim['state']}"
                )
            if not step["clicked"]:
                # What today's unclicked attempts hold: what each settled, else its reservation.
                held = db.execute(
                    """SELECT coalesce(sum(CASE WHEN settled IS NULL THEN reserved
                    ELSE settled END), 0) FROM shepherd_steps WHERE day=? AND clicked=0
                    AND started_at!='' AND id!=?""",
                    (step["day"], step_id),
                ).fetchone()[0]
                if held + reserve > budget + 1e-9:
                    return "today's budget is used"
            db.execute(
                "UPDATE shepherd_steps SET state=?, reserved=?, permitted=?, started_at=? "
                "WHERE id=?",
                (RUNNING, reserve, permitted, now(), step_id),
            )
            db.execute(
                "UPDATE pr_claims SET state=?, updated_at=? WHERE target=?",
                (STEPPING, now(), step["target"]),
            )
        return ""

    @staticmethod
    def store_value(db, key: str) -> str | None:
        row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def spent_today(self, day: str = "") -> dict[str, float]:
        """Credits of today's steps: settled, still reserved (admitted, not settled), and those
        clicked (charged, outside the budget)."""
        if not self.exists():
            return {"settled": 0.0, "reserved": 0.0, "clicked": 0.0}
        day = day or today()
        rows = self.store.all(
            "SELECT reserved, settled, clicked FROM shepherd_steps WHERE day=? AND started_at!=''",
            (day,),
        )
        settled = sum(row["settled"] or 0 for row in rows if not row["clicked"])
        reserved = sum(
            row["reserved"] for row in rows if row["settled"] is None and not row["clicked"]
        )
        clicked = sum(row["settled"] or 0 for row in rows if row["clicked"])
        return {"settled": settled, "reserved": reserved, "clicked": clicked}

    def settle(self, step_id: str, events: list[tuple[str, float]], final: bool) -> float:
        """What an attempt spent, from its worker's own log: each usage event once by its id,
        however often it is read (a restart reads it again). `final`: after a proven end of its
        turn: the reservation goes, the spend stays. Returns its spend so far."""
        with self.store.lock, self.store.db:
            db = self.store.db
            for event_id, spent in events:
                db.execute(
                    "INSERT OR IGNORE INTO ledger_events VALUES (?,?,?,?)",
                    (step_id, event_id, float(spent), time.time()),
                )
            total = db.execute(
                "SELECT coalesce(sum(credits), 0) FROM ledger_events WHERE step_id=?", (step_id,)
            ).fetchone()[0]
            if final:
                db.execute("UPDATE shepherd_steps SET settled=? WHERE id=?", (total, step_id))
        return float(total)

    def spend_of(self, step_id: str) -> float:
        return float(
            self.store.value(
                "SELECT coalesce(sum(credits), 0) FROM ledger_events WHERE step_id=?", (step_id,)
            )
        )

    def cap_reached(
        self, target: str, kind: str, fingerprint: str, h0: str, threads: Iterable[str] = (),
        clicked: bool = False, said: Iterable[str | int] = (),
    ) -> str:  # fmt: skip
        """Why another attempt may not start, "" when it may: three a kind and PR a day; one
        fix per check fingerprint and head unless the owner clicks for another (a look at why,
        at Suggest, is not a fix); two per review thread; none for review comments (`said`,
        each thread's last) that a plan the owner decided on had already, unless they click. An
        attempt stopped for the owner's work (`again` in its evidence) counts for the day only,
        and not at all if no model call of it was reported yet ("unstarted")."""
        tried = [step for step in self.steps(target) if step["started_at"] and step["kind"] == kind]
        # One stopped for the owner's work before its first model call never ran: no attempt.
        tried = [step for step in tried if (step["evidence"] or {}).get("again") != "unstarted"]
        if sum(1 for step in tried if step["day"] == today()) >= PER_DAY:
            return f"{PER_DAY} attempts today"
        # One stopped for the owner's work tried no fix and no round: the day's count only.
        tried = [step for step in tried if not (step["evidence"] or {}).get("again")]
        if (
            fingerprint
            and not clicked
            and sum(
                1 for step in tried
                if step["fingerprint"] == fingerprint and step["h0"] == h0
                and step["level"] == "prepare"
            )
            >= PER_FINGERPRINT
        ):  # fmt: skip
            return "tried already on this commit"
        wanted = set(threads)
        if wanted:
            counts: dict[str, int] = {}
            for step in tried:
                for thread in step["threads"] or []:
                    counts[str(thread)] = counts.get(str(thread), 0) + 1
            if all(counts.get(thread, 0) >= PER_THREAD for thread in wanted):
                return f"{PER_THREAD} rounds on each of these comments"
        asked = {str(comment) for comment in said}
        if asked and not clicked:
            # Published, partly or dismissed: the owner saw these comments' plan (one that left
            # a thread to them leaves it to them still, whatever moved the head since).
            decided = {
                str(ask.get("last"))
                for step in tried
                if step["state"] in {"published", "partly", "dismissed"}
                for ask in (step["signal"] or {}).get("threads") or []
            }
            if asked <= decided:
                return "you decided on these comments already"
        return ""

    # The journal: what Chief of Staff publishes, each operation with a stable id, written
    # before it is sent.

    def journal(
        self,
        step_id: str,
        holder: str,
        generation: int,
        consent: int,
        operations: list[dict],
        account: str,
    ) -> str:
        """Approve a prepared plan: one compare-and-set wins (prepared → applying), with the
        claim still held by `holder` at `generation` and the consent generation unchanged; its
        operations written, planned. "" when approved, else why not."""
        stamp = now()
        with self.store.lock, self.store.db:
            db = self.store.db
            step = db.execute("SELECT * FROM shepherd_steps WHERE id=?", (step_id,)).fetchone()
            if step is None or step["state"] != PREPARED:
                return "it was decided already"
            if int(self.store_value(db, "proactive:generation") or 0) != consent:
                return "Prepared for you was turned off"
            claim = db.execute(
                "SELECT holder, generation, state FROM pr_claims WHERE target=?", (step["target"],)
            ).fetchone()
            if claim is None or (claim["holder"], claim["generation"]) != (holder, generation):
                return "the PR changed hands since"
            if claim["state"] not in {IDLE, STEPPING}:
                return "it is paused" if claim["state"] == PAUSED else "it is changing hands"
            db.execute("UPDATE shepherd_steps SET state=? WHERE id=?", (APPLYING, step_id))
            for position, operation in enumerate(operations):
                db.execute(
                    """INSERT INTO actions(id, step_id, target, position, kind, payload, state,
                    account, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (f"{step_id}.{position}", step_id, step["target"], position,
                     operation["kind"], json.dumps(operation), PLANNED, account, stamp, stamp),
                )  # fmt: skip
        return ""

    def actions(self, step_id: str = "", *states: str) -> list[dict]:
        if not self.exists():
            return []
        query, params = "SELECT * FROM actions WHERE 1=1", []
        if step_id:
            query += " AND step_id=?"
            params.append(step_id)
        if states:
            query += f" AND state IN ({','.join('?' for _ in states)})"
            params += list(states)
        rows = self.store.all(query + " ORDER BY created_at, position", tuple(params))
        return [loaded(row, *ACTION_JSON) or {} for row in rows]

    def action(self, action_id: str) -> dict | None:
        return loaded(
            self.store.one("SELECT * FROM actions WHERE id=?", (action_id,)), *ACTION_JSON
        )

    def dispatching(
        self, action_id: str, holder: str, generation: int, consent: int, by: float = 0.0
    ) -> str:
        """The dispatch barrier, passed immediately before an operation is transmitted (after
        every wait: its write slot, its last reads, its account's token): it is journaled as
        dispatching, once, only while the claim is held as approved and not paused or changing
        hands, the feature was not turned off, and `by` (a time: how long its approval stands)
        has not passed. "" when it may go out."""
        with self.store.lock, self.store.db:
            db = self.store.db
            action = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if action is None or action["state"] != PLANNED:
                return "it is not waiting to go out"
            if why := self.consent_gone(db, action, holder, generation, consent, by):
                return why
            db.execute(
                "UPDATE actions SET state=?, updated_at=? WHERE id=?",
                (DISPATCHING, now(), action_id),
            )
        return ""

    def may_dispatch(
        self,
        action_id: str,
        holder: str,
        generation: int,
        consent: int,
        by: float = 0.0,
        state: str = PLANNED,
    ) -> str:
        """What the barrier would say now, changing nothing: before a wait (nothing waits for a
        write slot that could not go out), or (`state` dispatching) before a second
        transmission of one operation (a stacked PR's merge, gh's refused). "" when it may."""
        with self.store.lock:
            db = self.store.db
            action = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if action is None or action["state"] != state:
                return "it is not waiting to go out"
            return self.consent_gone(db, action, holder, generation, consent, by)

    def consent_gone(
        self, db: Any, action: Any, holder: str, generation: int, consent: int, by: float
    ) -> str:
        if int(self.store_value(db, "proactive:generation") or 0) != consent:
            return "Prepared for you was turned off"
        claim = db.execute(
            "SELECT holder, generation, state FROM pr_claims WHERE target=?", (action["target"],)
        ).fetchone()
        if claim is None or (claim["holder"], claim["generation"]) != (holder, generation):
            return "the PR changed hands"
        if claim["state"] == PAUSED:
            return "it was paused"
        if claim["state"] not in {IDLE, STEPPING}:
            return "the PR was merged or closed" if claim["state"] == CLAIM_ENDED else (
                "it is changing hands"
            )  # fmt: skip
        if by and time.time() > by:
            return AGED
        return ""

    def finish(self, action_id: str, state: str, outcome: str = "", link: str = ""):
        if state not in OP_ENDED and state != PLANNED:
            raise ValueError(f"Not an operation's end: {state}")
        self.store.execute(
            "UPDATE actions SET state=?, outcome=?, link=?, updated_at=? WHERE id=?",
            (state, outcome[:500], link[:500], now(), action_id),
        )

    def prune_plans(self, days: int):
        """Plans whose outcome is older than `days`: their diff, findings and replies go; what
        the journal shows of them (kind, heads, summary, evidence in words) stays."""
        if not self.exists():
            return
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        for row in self.store.all(
            "SELECT id, plan FROM shepherd_steps WHERE ended_at != '' AND ended_at < ? "
            "AND json_extract(plan, '$.diff') IS NOT NULL",
            (cutoff,),
        ):
            plan = json.loads(row["plan"] or "{}")
            kept = {key: plan[key] for key in PLAN_KEPT if key in plan}
            kept["evidence"] = {"words": (plan.get("evidence") or {}).get("words") or ""}
            self.store.execute(
                "UPDATE shepherd_steps SET plan=? WHERE id=?", (json.dumps(kept), row["id"])
            )

    def withdraw_planned(self, step_id: str, why: str):
        self.store.execute(
            "UPDATE actions SET state='withdrawn', outcome=?, updated_at=? WHERE step_id=? AND "
            "state=?",
            (why[:500], now(), step_id, PLANNED),
        )

    # The owner's verdicts, recorded from day one (a kind's numbers, later its ladder).

    def verdict(self, step: dict, verdict: str):
        repo = step["target"].removeprefix("github:").rsplit("#", 1)[0]
        self.store.execute(
            "INSERT INTO verdicts(step_id, kind, repo, verdict, at) VALUES (?,?,?,?,?)",
            (step["id"], step["kind"], repo, verdict, now()),
        )

    def verdicts(self, kind: str = "") -> list[dict]:
        if not self.exists():
            return []
        if kind:
            return self.store.all("SELECT * FROM verdicts WHERE kind=? ORDER BY id", (kind,))
        return self.store.all("SELECT * FROM verdicts ORDER BY id")
