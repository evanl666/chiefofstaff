"""Autopilot's runtime part: the switch, its runs, Stop, the outbox's deliveries, and the hooks
today's paths call (each returns at once while its Settings line is unticked: an in-memory
flag, `ticked`).

- **The line** (Settings → General) makes its tables, Home's button, and the Router's tool and
  rule; it starts nothing. A command to the Router ticks it (the command is the owner's
  consent).
- **A run** is on from Home's button or a command until its time (or until turned off): one row
  in the record's runs, its `started` entry, and a note to the Router, in one transaction (the
  note goes out from the outbox once it commits).
- **Stop** (Home, `autopilot off`, unticking the line) is a brake: in one transaction the run's
  `stopping` entry and the generation's bump, so nothing it queued goes out (each a `withdrawn`
  entry); then the run ends, with its summary.
- **A restart** resumes a timed run silently and an open-ended one with a line in the Router's
  chat; a run whose time passed while Chief of Staff was not running ends at start."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..envelope import wrap
from ..models import AS_SAVED, Settings
from ..store import now
from . import grammar
from .logbook import (
    CLAIMED,
    DONE,
    ENDED,
    FAILED,
    ON,
    PARKED,
    QUEUED,
    STOPPING,
    UNKNOWN,
    Logbook,
)
from .origin import Origins

if TYPE_CHECKING:
    from ..runtime import Runtime

logger = logging.getLogger(__name__)

# The version of its rules, in each run's row and entries.
RULES = "p1"
# What counts as done for the owner (the header's *4 done*): its actions, not its parks.
ACTIONS = ("answered", "decided", "accepted", "released", "nudged", "picked-folder")
# Said of anything done while the line is unticked.
OFF_WORDS = "Autopilot is off: tick it in Settings → General first."


def iso(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment else None


def parsed(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def local_now() -> datetime:
    return datetime.now().astimezone()


COMMIT = re.compile(r"\bgit\b[^\n;&|]*\bcommit\b")
PUSH = re.compile(r"\bgit\b[^\n;&|]*\bpush\b(?P<rest>[^\n;&|]*)")
PULL_REQUEST = re.compile(r"\bgh\s+pr\s+(?P<verb>create|merge|comment|review|close|edit|ready)\b")


def receipts_since(store, session_id: str, since: str, *, worker: bool = True) -> dict:
    """What a session did since a moment, from its receipts: the commands it ran (tests and
    their result, commits, pushes and their branches, PRs touched), the drafts it made, the
    permissions it was refused, the tasks it started (the Router's)."""
    commands = store.all(
        "SELECT command, kind, exit_code, started_at FROM commands WHERE session_id=? "
        "AND started_at>=? ORDER BY id",
        (session_id, since),
    )
    tests = [row for row in commands if row["kind"] == "test"]
    pushes = []
    for row in commands:
        if found := PUSH.search(row["command"]):
            words = [w for w in found["rest"].split() if not w.startswith("-")]
            branch = words[-1] if len(words) >= 2 else ""
            pushes.append({"branch": branch, "exit": row["exit_code"]})
    drafts = store.all(
        "SELECT id, kind, status FROM drafts WHERE session_id=? AND created_at>=? ORDER BY rowid",
        (session_id, since),
    )
    refused = store.all(
        "SELECT text FROM messages WHERE session_id=? AND kind='notice' AND created_at>=? "
        "AND json_extract(meta, '$.refused') IS NOT NULL ORDER BY id",
        (session_id, since),
    )
    # The Router's: the tasks it started or messaged with the origin (their workers' messages
    # carry it).
    started = (
        []
        if worker
        else store.all(
            "SELECT id, title FROM tasks WHERE created_at>=? AND session_id IN (SELECT "
            "session_id FROM messages WHERE created_at>=? AND session_id!=? AND "
            "json_extract(meta, '$.autopilot_origin') IS NOT NULL) ORDER BY created_at",
            (since, since, session_id),
        )
    )
    return {
        "commands": [
            {"command": row["command"][:2000], "kind": row["kind"], "exit": row["exit_code"]}
            for row in commands
        ],
        "tests": {
            "runs": len(tests),
            "passed": sum(1 for row in tests if row["exit_code"] == 0),
            "failed": sum(1 for row in tests if row["exit_code"] not in (0, None)),
        },
        "commits": sum(1 for row in commands if COMMIT.search(row["command"])),
        "pushes": pushes,
        "pull_requests": [
            found["verb"] for row in commands if (found := PULL_REQUEST.search(row["command"]))
        ],
        "drafts": [
            {"id": row["id"], "kind": row["kind"], "status": row["status"]} for row in drafts
        ],
        "refused": [row["text"][:300] for row in refused],
        "started": [{"id": row["id"], "title": row["title"]} for row in started],
    }


def said_receipts(done: dict) -> str:
    """The *then:* line: committed 2, ran 14 tests (passed), pushed chief/x."""
    parts = []
    if done["commits"]:
        parts.append(f"committed {done['commits']} time{'s' if done['commits'] != 1 else ''}")
    tests = done["tests"]
    if runs := tests["runs"]:
        result = "passed" if not tests["failed"] else f"{tests['failed']} failed"
        parts.append(f"ran tests {runs} time{'s' if runs != 1 else ''} ({result})")
    if pushed := [p["branch"] or "a branch" for p in done["pushes"] if p["exit"] == 0]:
        parts.append("pushed " + ", ".join(dict.fromkeys(pushed)))
    if done["pull_requests"]:
        parts.append("PR " + ", ".join(dict.fromkeys(done["pull_requests"])))
    if done["drafts"]:
        parts.append(f"drafted {len(done['drafts'])} for you")
    if done["refused"]:
        parts.append(f"{len(done['refused'])} refused (asked first)")
    if done["started"]:
        parts.append("started " + ", ".join(f"“{t['title']}”" for t in done["started"][:3]))
    if not parts and (count := len(done["commands"])):
        parts.append(f"ran {count} command{'s' if count != 1 else ''}")
    return ", ".join(parts) or "nothing more"


class Autopilot:
    """The switch and its runs, the outbox, and the hooks (inert while the line is unticked)."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.book = Logbook(runtime.store)
        # Which turns carry its origin (origin.py): inert until it first acts.
        self.origins = Origins(runtime.store)
        # The line, as last saved: every hook returns at once while False.
        self.ticked = self.store.settings().autopilot
        # Outbox deliveries by kind (each its own part: the note to the Router here; answers,
        # nudges and dependents with theirs).
        self.deliveries: dict[str, Callable[[dict], Awaitable[str]]] = {
            "router-note": self.deliver_router_note,
            "summary": self.deliver_summary,
        }
        self.dispatch_lock = asyncio.Lock()
        self.run_lock = asyncio.Lock()

    # The switch.

    def run(self) -> dict | None:
        """The run on now (None when none is, or the line is unticked)."""
        if not self.ticked:
            return None
        found = self.book.open_runs()
        return next((run for run in found if run["state"] == ON), None)

    def morning_time(self) -> str:
        return self.store.settings().digest_time or grammar.MORNING

    def until_of(self, choice: str, moment: datetime | None = None) -> datetime | None:
        """Home's durations: for 2 hours, until the next morning time, until turned off."""
        moment = moment or local_now()
        if choice == "2h":
            return moment + timedelta(hours=2)
        if choice == "morning":
            return grammar.morning(moment, self.morning_time())
        if choice == "off":
            return None
        raise ValueError("Choose for 2 hours, until the morning, or until you turn it off.")

    async def tick_line(self, how: str):
        """The line ticked by a command (`how`: its ingress): saved as Settings saves it."""
        old = self.store.settings()
        if old.autopilot:
            return
        new = Settings.model_validate({**old.model_dump(), "autopilot": True}, context=AS_SAVED)
        self.store.save_settings(new)
        await self.runtime.settings_changed(old, new)

    async def start(
        self,
        until: datetime | None,
        *,
        by: str,
        language: str = "en",
        moment: datetime | None = None,
    ) -> dict:
        """On until `until` (None: until turned off); a run on already gets the new end. `by`:
        how it was turned on (home, chat). Returns the header's state."""
        if not self.ticked:
            raise ValueError(OFF_WORDS)
        moment = moment or local_now()
        self.book.ensure()
        said = grammar.said_on(until, moment, language)
        async with self.run_lock:
            settings = self.store.settings()
            rules = {
                "version": RULES,
                "credits": settings.autopilot_credits,
                "repos": settings.autopilot_repos,
                "ask_first": settings.autopilot_ask_first,
            }
            with self.store.transaction():
                current = self.run()
                if current:
                    self.book.move_run(current["id"], ON, until=iso(until))
                    entry = self.book.append(
                        current["id"],
                        "renewed",
                        f"Now on until {grammar.clock(until, moment, 'en')}"
                        if until
                        else "Now on until you turn it off",
                        subject=f"run:{current['id']}",
                        why={"rule": RULES, "by": by, "ingress": "page"},
                        payload={"until": iso(until)},
                    )
                    run = self.book.run(current["id"]) or {}
                else:
                    run = self.book.new_run(until=iso(until), started_by=by, rules=rules)
                    entry = self.book.append(
                        run["id"],
                        "started",
                        f"Turned on until {grammar.clock(until, moment, 'en')}"
                        if until
                        else "Turned on until you turn it off",
                        subject=f"run:{run['id']}",
                        why={"rule": RULES, "by": by, "ingress": "page"},
                        payload={"until": iso(until), **rules},
                    )
                self.book.queue(
                    run["id"],
                    entry["id"],
                    "router-note",
                    {"note": self.start_note(until, moment), "display": said,
                     "language": language},
                    self.book.generation(),
                )  # fmt: skip
        self.runtime.background(self.dispatch())
        self.runtime.publish_state()
        return self.state()

    def start_note(self, until: datetime | None, moment: datetime) -> str:
        end = f"until {grammar.clock(until, moment, 'en')}" if until else "until they turn it off"
        return (
            f"Autopilot is on {end}: start what the owner's requests, watches and review requests "
            "ask for without asking them; ask them only what is theirs. Answer what Autopilot did "
            "only from autopilot_log."
        )

    async def stop(self, *, by: str, why: str, language: str = "en") -> dict:
        """The brake: nothing it queued goes out; the run ends, with its summary. Returns the
        header's state and what the toast says."""
        run = self.run()
        if run is None:
            return {**self.state(), "said": grammar.said_off(False, language)}
        async with self.run_lock:
            with self.store.transaction():
                if not self.book.move_run(run["id"], ON, state=STOPPING):
                    return {**self.state(), "said": grammar.said_off(False, language)}
                stopping = self.book.append(
                    run["id"],
                    STOPPING,
                    f"Stopped: {why}",
                    subject=f"run:{run['id']}",
                    why={"rule": RULES, "by": by},
                )
                self.book.bump()
                withdrawn = self.book.withdraw(why, run["id"])
            continued = await self.stop_continued(run, stopping)
            await self.end(run, by=by, at_its_time=False)
        parts = [grammar.said_off(True, language)]
        if continued:
            parts.append(
                f"{continued} task{'s' if continued != 1 else ''} it had continued wait with Go on"
            )
        if withdrawn:
            parts.append(
                f"{withdrawn} answer{'s' if withdrawn != 1 else ''} not yet delivered withdrawn"
            )
        return {**self.state(), "said": " · ".join(parts)}

    async def stop_continued(self, run: dict, stopping: dict) -> int:
        """Turns it continued that are running now: stopped as Stop stops one (their tasks wait
        with Go on). None in a run that continued nothing."""
        return 0

    async def end(self, run: dict, *, by: str, at_its_time: bool):
        """The run's end: its `ended` entry, the head kept, the summary card, the Router told."""
        with self.store.transaction():
            if not self.book.move_run(
                run["id"], (ON, STOPPING), state=ENDED, ended_at=now(), ended_by=by
            ):
                return
            actions = len(self.book.entries(run_id=run["id"], kinds=ACTIONS, limit=100000))
            entry = self.book.append(
                run["id"],
                "ended",
                "Ended at its time" if at_its_time else "Ended",
                subject=f"run:{run['id']}",
                why={"rule": RULES, "by": by},
                payload={"actions": actions},
            )
            self.book.queue(run["id"], entry["id"], "summary", {}, self.book.generation())
        self.book.keep_head()
        self.runtime.background(self.dispatch())
        self.runtime.publish_state()

    # Chat commands.

    async def typed(self, row: dict, text: str) -> dict | None:
        """A message the owner wrote to the Router in the page that is exactly an Autopilot
        command: answered here, without the Router's model (the Router hears the note). None
        for anything else."""
        moment = local_now()
        command = grammar.parse(text, moment, self.store.settings().digest_time)
        if command is None:
            return None
        self.runtime.add_message(
            row["id"], "owner", text, {"autopilot_command": True, "ingress": "page"}
        )
        if command.action == "off":
            if self.run() is None:
                return self.runtime.add_message(
                    row["id"], "notice", grammar.said_off(False, command.language)
                )
            result = await self.stop(
                by="you", why="you wrote autopilot off", language=command.language
            )
            return self.runtime.add_message(
                row["id"], "notice", result["said"], {"autopilot": {"stopped": True}}
            )
        if not self.ticked:
            await self.tick_line("page")
        await self.start(command.until, by="chat", language=command.language, moment=moment)
        await self.dispatch()  # Its line now: the answer to what they wrote.
        run = self.run() or {}
        found = self.store.one(
            "SELECT id FROM messages WHERE session_id=? AND json_extract(meta, '$.autopilot.run')=?"
            " ORDER BY id DESC LIMIT 1",
            (row["id"], run.get("id", "")),
        )
        return self.store.message(found["id"]) if found else {}

    # The outbox: what was committed, delivered after, once.

    async def dispatch(self):
        """Every queued item, oldest first, each claimed once."""
        if not self.book.exists():
            return
        async with self.dispatch_lock:
            for item in self.book.outbox(QUEUED):
                claimed = self.book.claim(item["id"])
                if claimed is None:
                    continue
                handler = self.deliveries.get(claimed["kind"])
                try:
                    outcome = await handler(claimed) if handler else "no handler"
                except Exception as exc:
                    logger.warning("An Autopilot delivery failed.", exc_info=True)
                    self.book.finish(claimed["id"], FAILED, str(exc)[:500])
                    continue
                self.book.finish(claimed["id"], DONE if handler else FAILED, outcome)

    async def deliver_router_note(self, item: dict) -> str:
        """The Router's note at a run's start and end: a line in its chat when there is
        something to show."""
        payload = item["payload"]
        message = await self.runtime.delivery.deliver(
            self.runtime.router_id(),
            wrap("system", "autopilot", payload["note"]),
            kind="notice",
            display=payload["display"],
            meta={"autopilot": {"run": item["run_id"], "entry": item["entry"]},
                  "outbox": item["id"]},
            batch=True,
        )  # fmt: skip
        self.book.stored(item["id"], message["id"])
        return ""

    async def deliver_summary(self, item: dict) -> str:
        """The run's summary card at the end of the Router's chat; the Router hears that
        Autopilot is off with its next message (the card carries the note)."""
        run = self.book.run(item["run_id"]) or {}
        text, card = self.summary(run)
        message = self.runtime.delivery.hold(
            self.runtime.router_id(),
            wrap("system", "autopilot-ended", "Autopilot is off: ask the owner as usual again."),
            kind="update",
            display=text,
            meta={"autopilot_summary": card, "outbox": item["id"]},
        )
        self.book.stored(item["id"], message["id"])
        router = self.store.session(self.runtime.router_id())
        self.runtime.update_session(router["id"], unread=router["unread"] + 1)
        self.runtime.alert(text.splitlines()[0], session_id=router["id"], needs_action=False)
        return ""

    def summary(self, run: dict) -> tuple[str, dict]:
        """What the run did and left, in one card (its text, and its parts for the page)."""
        entries = self.book.entries(run_id=run.get("id", ""), limit=100000)
        done = [entry for entry in entries if entry["kind"] in ACTIONS]
        parked = [entry for entry in entries if entry["kind"] == PARKED]
        moment = local_now()
        ended = parsed(run.get("ended_at")) or moment
        if not done and not parked:
            text = f"Autopilot ended at {grammar.clock(ended, moment, 'en')}: nothing came up."
        else:
            text = (
                f"Autopilot did {len(done)} thing{'s' if len(done) != 1 else ''} and left "
                f"{len(parked)} for you."
            )
        card = {
            "run": run.get("id"),
            "started_at": run.get("started_at"),
            "ended_at": run.get("ended_at"),
            "done": [entry["id"] for entry in done],
            "parked": [entry["id"] for entry in parked],
        }
        return text, card

    def reconcile(self):
        """At start: items claimed and not finished. One that became a message is delivered as
        any stored message is (Delivery.resume); one that never did went nowhere: unknown."""
        for item in self.book.outbox(CLAIMED):
            if item.get("message_id"):
                self.book.finish(item["id"], DONE, "stored before the restart")
            else:
                self.book.finish(item["id"], UNKNOWN, "Chief of Staff stopped while delivering it")

    # Start, periodic, settings.

    async def started(self):
        """After a restart, before any session resumes: the outbox reconciled; a run whose time
        passed ended; an open-ended one said in the Router's chat."""
        self.ticked = self.store.settings().autopilot
        if not self.book.exists():
            return
        self.reconcile()
        for run in self.book.open_runs():
            if not self.ticked or run["state"] == STOPPING:
                await self.end(run, by="settings" if not self.ticked else "you",
                               at_its_time=False)  # fmt: skip
                continue
            until = parsed(run["until"])
            if until and until <= datetime.now(UTC):
                await self.end(run, by="time", at_its_time=True)
            elif until is None:
                since = grammar.clock(parsed(run["started_at"]) or local_now(), local_now(), "en")
                self.runtime.add_message(
                    self.runtime.router_id(),
                    "notice",
                    f"Autopilot is still on (until you turn it off), since {since}",
                    {"autopilot": {"run": run["id"], "still_on": True}},
                )
        self.runtime.background(self.dispatch())

    async def tick(self):
        """Periodic: a run at its end ends; what is queued goes out."""
        if not self.ticked:
            return
        run = self.run()
        if run and (until := parsed(run["until"])) and until <= datetime.now(UTC):
            await self.end(run, by="time", at_its_time=True)
        if self.book.outbox(QUEUED):
            await self.dispatch()

    async def settings_changed(self, old: Settings, new: Settings):
        """Ticked: its tables (once). Unticked: a run on stops as Stop stops it."""
        if old.autopilot == new.autopilot:
            if self.ticked:
                self.runtime.publish_state()
            return
        if new.autopilot:
            self.book.ensure()
            self.ticked = True
            return
        if self.run():
            await self.stop(by="settings", why="you unticked Autopilot in Settings")
        self.ticked = False

    # Its origin: the turns it continued, and what came of them.

    def handed_back(self, row: dict):
        """The owner wrote to this worker in the page: its turns are theirs again (no more
        Ask-first checks for Autopilot's sake, nothing it does is Autopilot's)."""
        carried = self.origins.handed_back(row["id"])
        if not carried or not self.book.exists():
            return
        first = self.book.entry(carried["entries"][0]) or {}
        self.book.append(
            first.get("run_id") or "",
            "handed-back",
            f"You wrote to “{row['title']}”: its work is yours again",
            of=first.get("id") or "",
            subject=f"session:{row['id']}",
            task_id=first.get("task_id") or "",
            why={"rule": RULES, "by": "you", "ingress": "page"},
            payload={"entries": carried["entries"]},
        )

    def turn_ended(self, row: dict):
        """A turn that carried its origin ended: the Router's or the Slack session's stops
        carrying it; a worker's keeps it, and the record says what the turn did (`after`)."""
        if row["role"] != "worker":
            if carried := self.origins.turn_ended(row["id"]):
                self.after(row, carried, carried.get("since") or now())
            return
        if carried := self.origins.sticky.get(row["id"]):
            moment = now()
            self.after(row, carried, carried.get("seen") or carried.get("since") or moment)
            self.origins.seen(row["id"], moment)

    def after(self, row: dict, carried: dict, since: str):
        """One `after` entry per entry that led to this turn: what it did, from its receipts,
        and the settings it ran under."""
        if not self.book.exists():
            return
        done = receipts_since(self.store, row["id"], since, worker=row["role"] == "worker")
        mode = (
            "asked first"
            if carried.get("ask_first") or not self.runtime.yolo_choice(row)
            else "acted without asking, your usual setting"
        )
        task = self.runtime.tasks.current(row["id"]) if row["role"] == "worker" else None
        for entry_id in carried["entries"]:
            source = self.book.entry(entry_id)
            if not source:
                continue
            self.book.append(
                source["run_id"],
                "after",
                "then: " + said_receipts(done),
                of=entry_id,
                subject=f"session:{row['id']}",
                task_id=(task or {}).get("id") or source.get("task_id") or "",
                why={"rule": RULES, "settings": mode},
                evidence=done,
            )

    # What the page shows.

    def spent_today(self) -> float:
        return float(self.store.get(f"autopilot:spent:{local_now().date().isoformat()}") or 0)

    def state(self) -> dict[str, Any]:
        """Home's header line: until when, what it did, whether it is paused."""
        run = self.run()
        if run is None:
            return {"run": None}
        settings = self.store.settings()
        done = len(self.book.entries(run_id=run["id"], kinds=ACTIONS, limit=100000))
        parked = len(self.book.entries(run_id=run["id"], kinds=(PARKED,), limit=100000))
        spent = self.spent_today()
        return {
            "run": run["id"],
            "until": run["until"],
            "started_at": run["started_at"],
            "done": done,
            "parked": parked,
            "paused": "credits" if spent >= settings.autopilot_credits else "",
            "credits": settings.autopilot_credits,
        }

    def snapshot(self) -> dict | None:
        """The page's part, only once the line is ticked."""
        if not self.ticked:
            return None
        return {
            **self.state(),
            "spent": round(self.spent_today(), 1),
            "record": bool(self.book.runs(1)),
            "morning": self.morning_time(),
            "ask_first": self.store.settings().autopilot_ask_first,
        }
