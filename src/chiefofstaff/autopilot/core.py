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
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ..envelope import wrap
from ..models import AS_SAVED, Settings
from ..store import now
from . import grammar, summary
from .answers import Answers, Later, Withdrawn
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
from .results import Results

if TYPE_CHECKING:
    from ..runtime import Runtime

logger = logging.getLogger(__name__)

# The version of its rules, in each run's row and entries.
RULES = "p1"
# What counts as done for the owner (the header's *4 done*): its actions, not its parks.
ACTIONS = ("answered", "decided", "accepted", "released", "nudged", "picked-folder")
# Said of anything done while the line is unticked.
OFF_WORDS = "Autopilot is off: tick it in Settings → General first."


class AutopilotLog(BaseModel):
    since: str = Field(default="", description="ISO time or date to start from (default: a day)")
    until: str = Field(default="", description="ISO time or date to end at (default: now)")
    kind: str = Field(
        default="",
        description="Only these: answered, decided, accepted, released, nudged, parked, "
        "not-done, after, started, ended (comma separated)",
    )


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


class Autopilot(Answers, Results):
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
            "worker": self.deliver_worker,
            "accepted": self.deliver_accepted,
        }
        self.dispatch_lock = asyncio.Lock()
        self.run_lock = asyncio.Lock()
        # Cards held from the page while they are judged; the judge's one item at a time.
        self.deciding: set[str] = set()
        self.judge_lock = asyncio.Lock()
        self.proposal: asyncio.Future | None = None
        # The result being verified now ("" for none), and its job; how verification runs its
        # plan (tests run it without Copilot; None: in a sandboxed session of Chief's own).
        self.verifying = ""
        self.verify_job: asyncio.Task | None = None
        # Reported questions being judged now (one pass at a time, off the periodic loop).
        self.questioning = False
        # Workers being opened again asking first now (answers.reopen_asking), by id.
        self.reopening: dict[str, asyncio.Task] = {}
        self.runner: Any = None

    # The switch.

    def run(self) -> dict | None:
        """The run on now (None when none is, or the line is unticked)."""
        if not self.ticked:
            return None
        found = self.book.open_runs()
        return next((run for run in found if run["state"] == ON), None)

    def ticket(self) -> tuple[str, int] | None:
        """What work in flight acts for: the run on now and its generation (None: none on)."""
        run = self.run()
        return (run["id"], self.book.generation()) if run else None

    def holds(self, ticket: tuple[str, int] | None) -> bool:
        """Whether work that began under `ticket` may still be filed: that run is still on and
        no Stop came since (its generation). Read in the caller's transaction."""
        if not ticket or not self.ticked:
            return False
        run = self.book.run(ticket[0])
        return bool(run) and run["state"] == ON and self.book.generation() == ticket[1]

    def abandon(self):
        """Stop, or a run's end: what is in flight stops at once (the judge's wait ends with no
        proposal; a verification is cancelled). Anything judged before is filed nowhere
        (holds)."""
        if self.proposal is not None and not self.proposal.done():
            self.proposal.set_result(None)
        if self.verify_job is not None and not self.verify_job.done():
            self.verify_job.cancel()

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
            self.abandon()
            self.draw_returned(withdrawn)
            continued = await self.stop_continued(run, stopping)
            await self.end(run, by=by, at_its_time=False)
        parts = [grammar.said_off(True, language)]
        if continued:
            parts.append(
                f"{continued} task{'s' if continued != 1 else ''} it had continued "
                f"wait{'' if continued != 1 else 's'} with Go on"
            )
        if withdrawn:
            count = len(withdrawn)
            parts.append(f"{count} answer{'s' if count != 1 else ''} not yet delivered withdrawn")
        return {**self.state(), "said": " · ".join(parts)}

    async def stop_continued(self, run: dict, stopping: dict) -> int:
        """Turns it continued that are running now: stopped as Stop stops one (their tasks wait
        with Go on, and Ask first stays on them until they end); the Router's and the Slack
        session's turns that carry it too. Returns how many of the workers'."""
        ours = {entry["id"] for entry in self.book.entries(run_id=run["id"], limit=100000)}
        for session_id, carried in list(self.origins.turns.items()):
            row = self.store.find_session(session_id)
            if not row or row["closed_at"] or not ours & set(carried.get("entries") or []):
                continue
            if row["status"] in {"working", "starting"} or self.runtime.states[session_id].busy:
                await self.runtime.workers.interrupt(session_id, by="Autopilot's Stop")
        stopped = 0
        for session_id, carried in list(self.origins.sticky.items()):
            if not ours & set(carried.get("entries") or []):
                continue
            row = self.store.find_session(session_id)
            state = self.runtime.states[session_id]
            if not row or row["closed_at"]:
                continue
            if row["status"] in {"working", "starting"} or state.busy:
                await self.runtime.workers.interrupt(session_id, by="Autopilot's Stop")
                stopped += 1
        return stopped

    async def end(self, run: dict, *, by: str, at_its_time: bool):
        """The run's end: its `ended` entry, the head kept, the summary card, the Router told."""
        self.abandon()
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
        self.tidy()
        if actions:
            # One row per run under Handled for you, opening it in the record.
            self.runtime.handled.record(
                "autopilot",
                f"Autopilot: {actions} action{'s' if actions != 1 else ''}",
                ref=run["id"],
                data={"at": [run["started_at"], now()], "link": f"#autopilot-run/{run['id']}"},
            )
        self.runtime.background(self.dispatch())
        self.runtime.publish_state()

    def fold(self, text: str) -> bool:
        """A summary due during a run (Focus's): folded into the run's card, not a card of its
        own (never two for the same hours). Whether it was."""
        run = self.run()
        if run is None or not text:
            return False
        key = f"autopilot:folded:{run['id']}"
        folded = [part for part in (self.store.get(key) or "").split("\n\n") if part]
        self.store.set(key, "\n\n".join([*folded, text]))
        return True

    def fold_digest(self, text: str) -> str:
        """The daily summary, due during a run: folded into its card (a run of a day at most);
        a longer one, or one until turned off, gets its part in each daily summary (said to be
        on), and its card covers what came after."""
        run = self.run()
        if run is None:
            return text
        started = parsed(run["started_at"]) or datetime.now(UTC)
        until = parsed(run["until"])
        if until is not None and until - started <= timedelta(days=1):
            self.fold(text)
            return ""
        moment = local_now()
        since = run.get("summarized") or run["started_at"]
        entries = self.book.entries(run_id=run["id"], since=since, limit=100000)
        done = sum(1 for entry in entries if entry["kind"] in ACTIONS)
        left = sum(1 for entry in entries if entry["kind"] in {PARKED, "not-done"})
        end = f"until {grammar.clock(until, moment, 'en')}" if until else "until you turn it off"
        part = (
            f"• Autopilot is still on ({end}), since {grammar.clock(started, moment, 'en')}: "
            f"{done} done, {left} left for you"
        )
        self.book.move_run(run["id"], ON, summarized=now())
        return f"{text}\n{part}" if text else f"Daily summary\n{part}"

    def tidy(self):
        """What it kept about items that are gone (answered, closed): forgotten at a run's end."""
        pending = {draft["id"] for draft in self.store.drafts()}
        for key in self.store.keys("autopilot:setaside:"):
            item = key.removeprefix("autopilot:setaside:")
            if item not in pending and not self.store.get(f"ownerq:{item}"):
                self.store.delete(key)
        for key in self.store.keys("autopilot:judged:"):
            if not self.store.get(f"ownerq:{key.removeprefix('autopilot:judged:')}"):
                self.store.delete(key)

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
                except Later:
                    self.book.release(claimed["id"])  # Delivered later, once.
                    continue
                except Withdrawn as exc:
                    # Stop came since its claim (or what it answers was settled meanwhile).
                    if self.book.withdraw_claimed(claimed, str(exc)):
                        self.draw_returned([claimed])
                    continue
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
        self.runtime.alert(card["said"], session_id=router["id"], needs_action=False)
        return ""

    def summary(self, run: dict) -> tuple[str, dict]:
        """What the run did and left, in one card (its text, and its parts for the page)."""
        return summary.card(self, run)

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
        """Periodic: a run at its end ends; what is queued goes out; questions are judged,
        results verified, stalled work nudged. Never waits for a judgment, a verification or a
        delivery: each is a job of its own (one at a time), so no other periodic job waits."""
        if not self.ticked:
            return
        run = self.run()
        if run and (until := parsed(run["until"])) and until <= datetime.now(UTC):
            await self.end(run, by="time", at_its_time=True)
        if self.book.outbox(QUEUED) and not self.dispatch_lock.locked():
            self.runtime.background(self.dispatch())
        if self.run() is not None:
            if not self.questioning:
                self.questioning = True
                self.runtime.background(self.question_pass())
            await self.check_results()
            await self.check_stalled()

    async def question_pass(self):
        """One pass over the questions waiting for the owner (check_questions), off the
        periodic loop; the next tick starts none while it runs."""
        try:
            await self.check_questions()
        finally:
            self.questioning = False

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
        """One `after` entry for the turn, listing every entry that led to it: what it did,
        from its receipts (kept once, as a blob its hash names), and the settings it ran
        under; in one transaction."""
        if not self.book.exists():
            return
        done = receipts_since(self.store, row["id"], since, worker=row["role"] == "worker")
        mode = (
            "asked first"
            if carried.get("ask_first") or not self.runtime.yolo_choice(row)
            else "acted without asking, your usual setting"
        )
        task = self.runtime.tasks.current(row["id"]) if row["role"] == "worker" else None
        sources = [self.book.entry(entry_id) for entry_id in carried["entries"]]
        if row["role"] == "worker" and any(s and s["kind"] == "nudged" for s in sources):
            # A turn it started itself (a nudge): on its credits.
            moment = datetime.fromisoformat(since).timestamp() if since else 0
            self.spend(self.usage_since(row["id"], moment))
        found = [source for source in sources if source]
        if not found:
            return
        latest = found[-1]
        with self.store.transaction():
            receipts = self.book.stash(done)
            self.book.append(
                latest["run_id"],
                "after",
                "then: " + said_receipts(done),
                of=latest["id"],
                subject=f"session:{row['id']}",
                task_id=(task or {}).get("id") or latest.get("task_id") or "",
                why={"rule": RULES, "settings": mode},
                evidence={"receipts": receipts},
                payload={"of": [source["id"] for source in found]},
            )

    # The Router's tool.

    async def t_autopilot_log(self, params: AutopilotLog, caller: str) -> dict:
        if (self.store.find_session(caller) or {}).get("role") != "router":
            raise ValueError("autopilot_log is the Router's.")
        if not self.book.exists():
            return {"entries": [], "counts": {}, "note": "Autopilot has never been on."}
        since = params.since.strip() or (datetime.now(UTC) - timedelta(days=1)).isoformat()
        until = params.until.strip()
        kinds = tuple(k.strip() for k in params.kind.split(",") if k.strip())
        found = self.book.entries(since=since, until=until, kinds=kinds, limit=200)
        counts: dict[str, int] = {}
        for entry in found:
            counts[entry["kind"]] = counts.get(entry["kind"], 0) + 1
        return {
            "entries": [
                {
                    "at": entry["at"],
                    "kind": entry["kind"],
                    "what": entry["title"],
                    "task": entry["task_id"],
                    "why": entry["why"],
                    "link": f"#autopilot/{entry['id']}",
                }
                for entry in found
            ],
            "counts": counts,
            "runs": [
                {
                    "started": run["started_at"],
                    "until": run["until"],
                    "ended": run["ended_at"],
                    "ended_by": run["ended_by"],
                }
                for run in self.book.runs(10)
                if (run["ended_at"] or "9") >= since
            ],
        }

    # What the page shows.

    def spent_today(self) -> float:
        return float(self.store.get(f"autopilot:spent:{local_now().date().isoformat()}") or 0)

    def spend(self, used: float):
        """What it spent (its judge, the turns it started): on the day's cap."""
        if used <= 0:
            return
        key = f"autopilot:spent:{local_now().date().isoformat()}"
        with self.store.transaction():
            self.store.set(key, str(round(float(self.store.get(key) or 0) + used, 3)))
        self.runtime.publish_state()

    def paused(self) -> bool:
        """Today's credits are used: what would cost credits waits."""
        return self.spent_today() >= self.store.settings().autopilot_credits

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
            # Cards not drawn while it judges them (two minutes at most).
            "deciding": sorted(self.deciding),
            # What it set aside, and why (a line under its card in Needs you).
            "set_aside": {
                key: json.loads(value).get("why", "")
                for key, value in self.store.items("autopilot:setaside:").items()
            },
        }
