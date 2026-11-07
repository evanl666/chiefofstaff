"""The connector: Chief of Staff's helper that reaches the owner's world with whatever tools this
machine's Copilot has (Slack, Glean or another company search, mail, docs). It runs the owner's
watches on a schedule and hands what needs them to the Router; it learns how the owner writes
from what they wrote there; and when the owner is away from this computer, it tells them what
needs them where they are, and brings back their answers.

It is one helper worker (special "connector"), reused across runs. It reads untrusted content,
so it runs like a worker that reads Slack: sandboxed, and nothing it reads reaches other
sessions' instructions. It sends nothing as the owner; the only message it sends is to the owner
themself, when they are away.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, model_validator

from . import prompts, threads
from .envelope import UNTRUSTED, wrap
from .models import DraftDecision, NestedArguments
from .roles import WORKER
from .store import now

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

SPECIAL = "connector"
MESSENGER = "messenger"  # Sends the owner's away notifications, outside the connector's sandbox.
TITLE = "Watch for you"
TICK = float(os.environ.get("CHIEFOFSTAFF_CONNECTOR_TICK", "60"))
LOOKBACK_HOURS = 24
# A fresh conversation once its context is this large: each scan carries what it needs.
FRESH_AFTER = 120_000
# Kinds of needs-you cards a notification lists, and how a reply settles each.
NOTIFIED_KINDS = {"slack", "github", "reply", "question", "decision", "workspace", "merge"}
CODE = re.compile(r"\b([A-Z2-7]{5})\b")
SEND_WORDS = {"send", "ok", "okay", "yes", "y", "approve", "lgtm", "发", "发送", "好", "可以"}
REJECT_WORDS = {"reject", "no", "n", "skip", "don't", "dont", "不", "不要", "拒绝"}
HID_IDLE = re.compile(r'"HIDIdleTime" = (\d+)')


def later(first: str, second: str) -> bool:
    """Whether ISO time `first` is after `second` (False when either cannot be read)."""
    try:
        return datetime.fromisoformat(first.replace("Z", "+00:00")) > datetime.fromisoformat(
            second.replace("Z", "+00:00")
        )
    except (ValueError, TypeError):
        return False


class FoundItem(BaseModel):
    key: str = Field(
        description="A stable id for it: its permalink, message id or URL (the same item "
        "always gets the same key)"
    )
    source: str = Field(description="Where it is: slack, email, doc, calendar, …")
    sender: str = Field(default="", description="Who wrote it")
    text: str = Field(description="What they wrote, quoted exactly (at most 2000 characters)")
    link: str = Field(default="", description="A link to open it")
    when: str = Field(default="", description="When it was written")
    context: str = Field(
        default="", description="What it answers or follows: the thread or document, briefly"
    )
    watch: int = Field(default=0, description="The number of the watch it is for")
    owner_replied: bool = Field(
        default=False,
        description="True when the owner already wrote in the same thread or DM after it (read "
        "the thread to know): then it needs nothing from them",
    )
    owner_replied_at: str = Field(
        default="", description="When the owner last wrote there, if they did (ISO time)"
    )


class ReportFound(NestedArguments):
    items: list[FoundItem] = Field(
        default_factory=list,
        max_length=30,
        description="Every new item that needs the owner (a question, a request, a mention "
        "expecting an answer). Empty when there is none.",
    )
    problems: str = Field(
        default="",
        description="A watch you could not run with the tools here, and what is missing",
    )
    methods: str = Field(
        default="",
        description="How you ran each watch (which tool and query), for the next scan",
    )


class OwnerWords(BaseModel):
    key: str = Field(description="A stable id: its permalink or message id")
    kind: Literal["slack_dm", "slack_channel", "pr_review"] = Field(
        description="slack_dm for a direct message, slack_channel for a channel or thread"
    )
    text: str = Field(description="What the owner wrote, exactly")


class ReportOwnerWords(NestedArguments):
    items: list[OwnerWords] = Field(default_factory=list, max_length=50)


class ReportNotified(BaseModel):
    ok: bool = Field(description="Whether the owner got the message")
    where: str = Field(default="", description="Where it went (a Slack DM from a bot, say)")
    method: str = Field(default="", description="The tool and arguments that worked, exactly")
    error: str = Field(default="", description="Why it could not be sent")


class OwnerReply(BaseModel):
    code: str = Field(description="The five-letter code the owner's reply names")
    text: str = Field(description="The owner's reply, exactly")


class ReportOwnerReplies(NestedArguments):
    replies: list[OwnerReply] = Field(default_factory=list, max_length=20)

    @model_validator(mode="before")
    @classmethod
    def one_reply(cls, data: Any) -> Any:
        """One reply passed on its own ({code, text}) is a list of one: never dropped."""
        if isinstance(data, dict) and "replies" not in data and {"code", "text"} <= data.keys():
            return {"replies": [{"code": data["code"], "text": data["text"]}]}
        return data


def words_of(text: str) -> set[str]:
    """The telling words of a text (four letters and up), for comparing two tellings."""
    return set(re.findall(r"\w{4,}", text.casefold()))


def minutes_of(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


def in_hours(hours: str, moment: datetime) -> bool:
    start, end = (minutes_of(part) for part in hours.split("-"))
    current = moment.hour * 60 + moment.minute
    workday = moment.weekday() < 5
    if start <= end:
        return workday and start <= current < end
    return workday and (current >= start or current < end)


def code_of(identifier: str) -> str:
    """A short code the owner types to answer a card from elsewhere."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    number = int(hashlib.sha256(identifier.encode()).hexdigest(), 16)
    return "".join(alphabet[(number >> (5 * index)) & 31] for index in range(5))


class Connector:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.job: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.last_seen = time.time()  # The owner's last activity in the app.

    async def start(self):
        self.job = asyncio.create_task(self.loop())

    async def stop(self):
        if self.job:
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)

    async def loop(self):
        while True:
            await asyncio.sleep(TICK)
            try:
                await self.tick()
            except Exception:
                logger.warning("The connector's round failed.", exc_info=True)

    async def tick(self):
        settings = self.store.settings()
        if not self.runtime.ready() or settings.paused:
            return
        if self.due():
            await self.scan()
        if settings.away_notify and await self.away():
            await self.notify_away()

    # The session

    def session(self) -> dict | None:
        row = self.runtime.toolset.last_special(SPECIAL)
        return row if row and not row["closed_at"] else None

    def busy(self) -> bool:
        row = self.session()
        return bool(row and row["status"] in {"queued", "starting", "working"})

    async def send(self, kind: str, text: str, display: str):
        """A job for the connector: in its open session, or a fresh one (a new run)."""
        row = self.session()
        context = int(self.store.get(f"context:{row['id']}") or 0) if row else 0
        if not row or context > FRESH_AFTER:
            await self.runtime.toolset.start_special(SPECIAL, TITLE, text, slack=True)
            return
        await self.runtime.delivery.deliver(
            row["id"], wrap("system", kind, text), kind="router", display=display
        )

    # Watches

    def interval(self, moment: datetime | None = None) -> int:
        """Minutes between scans now (0: none now)."""
        settings = self.store.settings()
        # Answers the owner may give to notifications sent while they were away: looked for at
        # the work-hours pace, at night and on weekends too (the notifications go out then).
        answers = settings.watch_minutes if settings.away_notify and self.open_codes() else 0
        if not any(watch.enabled for watch in settings.watches):
            return answers
        moment = moment or datetime.now()
        if in_hours(settings.watch_hours, moment):
            return settings.watch_minutes
        return min(
            (every for every in (settings.watch_after_hours_minutes, answers) if every), default=0
        )

    def due(self) -> bool:
        every = self.interval()
        if not every or self.busy():
            return False
        last = float(self.store.get("watch_scanned_at") or 0)
        return time.time() - last >= every * 60

    async def scan(self, by_owner: bool = False):
        """Run the owner's watches once."""
        async with self.lock:
            settings = self.store.settings()
            watches = [watch for watch in settings.watches if watch.enabled]
            codes = self.open_codes() if settings.away_notify else []
            if not watches and not codes:
                raise ValueError("Add a watch first.")
            if self.busy():
                raise ValueError("A scan is running already.")
            last = float(self.store.get("watch_scanned_at") or 0)
            since = max(last, time.time() - LOOKBACK_HOURS * 3600) if last else 0
            self.store.set("watch_scanned_at", str(time.time()))
            lines = ["Scan now. The owner's watches:" if watches else "Scan now: no watches."]
            for number, watch in enumerate(watches, 1):
                lines.append(
                    f"{number}. {watch.what}" + (f" (use: {watch.how})" if watch.how else "")
                )
            start = (
                datetime.fromtimestamp(since).astimezone().isoformat(timespec="minutes")
                if since
                else f"the last {LOOKBACK_HOURS} hours"
            )
            lines.append(f"Look at what is new since {start}.")
            if methods := self.store.get("watch_methods"):
                lines.append(
                    f"How you ran them before (use it again if it still works):\n{methods}"
                )
            if codes:
                lines.append(
                    "The owner may have answered Chief of Staff's notifications to them "
                    f"({self.store.get('away_where') or 'where they were sent'}): replies that "
                    f"name one of these codes: {', '.join(codes)}. Pass each with "
                    "report_owner_replies, only when the owner themself wrote it there. These "
                    "still wait: a reply naming one of them has not been passed on yet, even if "
                    "you saw it or think you passed it on before (one passed on is not listed "
                    "again)."
                )
            lines.append(prompts.CONNECTOR_SCAN)
            shown = "; ".join(w.what for w in watches) or "your answers to notifications"
            await self.send("scan", "\n\n".join(lines), "Scan: " + shown)
            if by_owner:
                self.runtime.publish_state()

    async def t_report_found(self, params: ReportFound, caller: str):
        self.check(caller)
        if params.methods.strip():
            self.store.set("watch_methods", params.methods.strip()[:2000])
        self.store.set(
            "watch_last",
            json.dumps(
                {"at": now(), "found": len(params.items), "problems": params.problems[:500]}
            ),
        )
        new = 0
        answered = 0
        for item in params.items:
            if item.owner_replied or later(item.owner_replied_at, item.when):
                answered += 1  # The owner answered it already: nothing for them to do.
                continue
            key = "watch:" + hashlib.sha256(item.key.strip().encode()).hexdigest()[:24]
            text = "\n".join(
                part
                for part in (
                    f"Source: {item.source}" + (f" (watch {item.watch})" if item.watch else ""),
                    f"From: {item.sender}" if item.sender else "",
                    f"When: {item.when}" if item.when else "",
                    f"Link: {item.link}" if item.link else "",
                    self.thread_line(item.link),
                    # The watcher's own reading, not checked: the thread is what counts.
                    f"Context (as the watcher read it): {item.context[:1000]}"
                    if item.context
                    else "",
                    f"Message:\n{item.text[:2000]}",
                )
                if part
            )
            display = f"{item.source} · {item.sender}: {' '.join(item.text.split())[:300]}"
            if await self.runtime.on_trigger("watch", [key], text, display=display):
                new += 1
        self.runtime.publish_state()
        skipped = f" {answered} the owner already answered were left out." if answered else ""
        return f"{new} new item(s) handed to the Router; the rest were seen before.{skipped}"

    def thread_line(self, link: str) -> str:
        """The Slack thread a found message is in, as a key a Slack-reading worker can follow;
        or why none can."""
        found = threads.slack_messages(link)
        if not found:
            return ""
        key = next(iter(found.values()))[0]
        if not self.runtime.slack.configured():
            return (
                f"Thread: {key} (Slack is not connected here, so no session can follow it: a "
                "worker reads it with the tools it has)"
            )
        return f"Thread: {key} (spawn_worker with slack=true and follow=[{key!r}] to follow it)"

    async def t_report_owner_words(self, params: ReportOwnerWords, caller: str):
        self.check(caller)
        added = sum(
            self.runtime.habits.add("watch", item.kind, item.key, item.text)
            for item in params.items
        )
        if params.items:
            self.store.set("words_empty", "0")
        return f"Kept {added} new example(s) of how the owner writes."

    async def collect_words(self, by_owner: bool = False) -> bool:
        """Ask the connector for what the owner wrote lately (Slack, mail…) with whatever
        tools work here: how they write is learned from it. Daily; weekly once two runs found
        nothing (no tool here reaches their messages)."""
        if self.busy():
            return False
        last = float(self.store.get("words_asked_at") or 0)
        empty = int(self.store.get("words_empty") or 0)
        wait = 86400 * (7 if empty >= 2 else 1)
        if not by_owner and time.time() - last < wait:
            return False
        self.store.set("words_asked_at", str(time.time()))
        self.store.set("words_empty", str(empty + 1))  # Back to 0 when words come.
        await self.send("learn", prompts.CONNECTOR_WORDS, "Read what you wrote lately")
        return True

    def check(self, caller: str, kinds: tuple[str, ...] = (SPECIAL,)):
        row = self.store.session(caller)
        if row["role"] != WORKER or not any(self.store.get(f"{kind}:{caller}") for kind in kinds):
            raise ValueError("Only Chief of Staff's connector calls this.")

    def view(self) -> dict:
        settings = self.store.settings()
        last = float(self.store.get("watch_scanned_at") or 0)
        row = self.runtime.toolset.last_special(SPECIAL)
        return {
            "every": self.interval(),
            "scanned_at": datetime.fromtimestamp(last).astimezone().isoformat() if last else "",
            "last": json.loads(self.store.get("watch_last") or "{}"),
            "scanning": self.busy(),
            "session_id": row["id"] if row else "",
            "away": {
                "enabled": settings.away_notify,
                "last": json.loads(self.store.get("away_last") or "{}"),
                "where": self.store.get("away_where") or "",
            },
        }

    # Away

    async def idle_seconds(self) -> float:
        """How long the owner has not used this computer (keyboard, mouse); on other systems,
        since they last used the app."""
        app = time.time() - self.last_seen
        if fake := os.environ.get("CHIEFOFSTAFF_IDLE_FILE"):
            # Tests: away (or not) whatever the machine's owner does.
            with suppress(OSError, ValueError):
                text = await asyncio.to_thread(Path(fake).read_text)
                return float(text.strip() or 0)
            return 0
        if sys.platform != "darwin":
            return app
        try:
            process = await asyncio.create_subprocess_exec(
                "ioreg", "-c", "IOHIDSystem", "-d", "4",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
            output, _ = await asyncio.wait_for(process.communicate(), 10)
        except Exception:
            return app
        found = HID_IDLE.search(output.decode(errors="replace"))
        return int(found.group(1)) / 1e9 if found else app

    async def away(self) -> bool:
        return await self.idle_seconds() >= self.store.settings().away_minutes * 60

    def waiting_items(self) -> list[dict]:
        """What needs the owner now, each with a code to answer it by."""
        items = []
        drafts = self.store.drafts()
        # A task waiting on a card that asks (a question, a folder to choose) is told as that card,
        # once; a draft of its worker's (a reply to approve) is not its question.
        carded = {d["session_id"] for d in drafts if d["kind"] in {"question", "workspace"}}
        deciding = {
            asker.get("task_id")
            for d in drafts
            if d["kind"] == "decision"
            for asker in d["target"].get("askers") or []
        }
        for draft in drafts:
            if draft["kind"] not in NOTIFIED_KINDS:
                continue
            key = draft["id"]
            if draft["kind"] == "merge":
                # A PR offered again at the same commit is the same item: told once, one code.
                key = f"merge:{draft['target']['label'].lower()}@{draft['target']['head']}"
            items.append({"id": f"draft:{key}", "code": code_of(key), "draft": draft})
        tasks = self.runtime.tasks.listed()
        for question in self.runtime.asks.owner_questions():
            key = f"question:{question['session_id']}:{question['at']}"
            items.append({"id": key, "code": code_of(key), "question": question})
        if (ask := self.router_question()) and not self.relays(ask, tasks):
            key = f"router:{ask['id']}"
            items.append({"id": key, "code": code_of(key), "ask": ask})
        for task in tasks:
            if task["status"] == "waiting" and (
                task["id"] in deciding or task["session_id"] in carded
            ):
                continue
            if task["status"] in {"waiting", "failed"}:
                items.append(
                    {
                        "id": f"task:{task['id']}:{task['status']}",
                        "code": code_of(task["id"]),
                        "task": task,
                    }
                )
        return items

    def relays(self, ask: dict, tasks: list[dict]) -> bool:
        """Whether the Router's question is a worker's, retold: a question of a waiting task
        reached it after the owner's last message and before it asked, in much the same words.
        The owner is told it once, as the task's."""
        waiting = {task["id"]: task for task in tasks if task["status"] == "waiting"}
        if not waiting:
            return False
        latest = self.store.messages(self.runtime.router_id(), limit=40)
        owner = max((m["id"] for m in latest if m["kind"] == "owner"), default=0)
        asked = words_of(ask["text"])
        for message in latest:
            task = waiting.get(message["meta"].get("task_id") or "")
            if not (owner < message["id"] < ask["id"] and message["kind"] == "inbound" and task):
                continue
            theirs = words_of(f"{message['text']} {task['progress']}")
            if asked and len(asked & theirs) >= 0.4 * len(asked):
                return True
        return False

    def router_question(self) -> dict | None:
        """What the Router asked the owner in the chat and they have not answered yet: its
        latest message after theirs, when it asks something or needs them."""
        latest = self.store.messages(self.runtime.router_id(), limit=40)
        owner = max((m["id"] for m in latest if m["kind"] == "owner"), default=0)
        asks = [
            m
            for m in latest
            if m["id"] > owner
            and m["kind"] in {"assistant", "update"}
            and not m["meta"].get("internal")
            and (m["meta"].get("needs_action") or re.search("[?\uff1f]", m["text"]))
        ]
        return asks[-1] if asks else None

    def open_codes(self) -> list[str]:
        sent = set(json.loads(self.store.get("away_sent") or "[]"))
        return [item["code"] for item in self.waiting_items() if item["id"] in sent]

    def describe(self, item: dict) -> str:
        code = item["code"]
        if ask := item.get("ask"):
            return (
                f'[{code}] Chief of Staff asks: {ask["text"][:1200]}\nReply "{code} <your answer>".'
            )
        if draft := item.get("draft"):
            target = draft["target"]
            kind = draft["kind"]
            if kind in {"slack", "github", "reply"}:
                where = target.get("to") or target.get("where") or target.get("url") or ""
                return (
                    f"[{code}] Draft to approve{f' ({where})' if where else ''}:\n"
                    f"{draft['text'][:1500]}\n"
                    f'Reply "{code} send" to send it, "{code} no" to drop it'
                    + (", or copy it and send it yourself." if kind == "reply" else ".")
                )
            if kind == "decision":
                options = target.get("options") or []
                listed = "; ".join(f"{n}) {o.get('choice', '')}" for n, o in enumerate(options, 1))
                return (
                    f"[{code}] Decision: {target.get('question', draft['text'])[:500]}"
                    + (f" Options: {listed}." if listed else "")
                    + (
                        f" Recommended: {target['recommendation']}."
                        if target.get("recommendation")
                        else ""
                    )
                    + f' Reply "{code} <number or your answer>".'
                )
            if kind == "merge":
                return (
                    f"[{code}] Ready to merge: {target['label']} “{target.get('title', '')}” "
                    f'(CI passes). Reply "{code} merge" to merge it, "{code} no" to leave it.'
                )
            if kind == "workspace":
                options = target.get("options") or []
                listed = "; ".join(f"{n}) {o['path']}" for n, o in enumerate(options, 1))
                title = target.get("title", "")
                return f'[{code}] Which folder for “{title}”? {listed}. Reply "{code} <number>".'
            return f'[{code}] Question: {draft["text"][:800]} Reply "{code} <your answer>".'
        if question := item.get("question"):
            return (
                f"[{code}] “{question['worker']}” asks: {question['text'][:800]} "
                f'Reply "{code} <your answer>".'
            )
        task = item["task"]
        if task["status"] == "failed":
            return f'[{code}] Failed: “{task["title"]}”. Reply "{code} retry" to run it again.'
        return (
            f"[{code}] “{task['title']}” asks: {(task['progress'] or '')[:800]} "
            f'Reply "{code} <your answer>".'
        )

    async def notify_away(self, test: bool = False):
        """Tell the owner, where they are, what needs them that they have not been told of."""
        sent = json.loads(self.store.get("away_sent") or "[]")
        fresh = [item for item in self.waiting_items() if item["id"] not in sent]
        if not fresh and not test:
            return
        if self.messenger_busy():
            if test:
                raise ValueError("A notification is being sent now: try again in a minute.")
            return  # After its current run: one message at a time.
        settings = self.store.settings()
        if test:
            body = (
                "Test from Chief of Staff: this is how you will hear from it when you are away. "
                f'Reply "{code_of("test")} ok" to check that your answers come back.'
            )
        else:
            body = "Chief of Staff needs you:\n\n" + "\n\n".join(
                self.describe(item) for item in fresh[:8]
            )
        how = settings.away_how.strip() or prompts.AWAY_DEFAULT
        # Sent by the messenger, outside the connector's sandbox (which reaches GitHub only, as
        # it reads others' messages): it is given Chief of Staff's own text, in a file.
        path = await asyncio.to_thread(self.write_notification, body)
        lines = [
            f"Send the owner the message in this file, as it is (only to the owner themself, "
            f"nobody else): {path}",
            f"How the owner wants to get it: {how}",
        ]
        if method := self.store.get("away_method"):
            lines.append(f"What worked before: {method}")
        lines.append(
            "Then call report_notified with whether it arrived, where, and the exact method. "
            "Never post it anywhere else or as the owner to anyone else."
        )
        if not test:
            self.store.set("away_sent", json.dumps((sent + [item["id"] for item in fresh])[-200:]))
        await self.runtime.toolset.start_special(
            MESSENGER, "Tell you when you're away", "\n\n".join(lines)
        )

    def write_notification(self, body: str) -> Path:
        """The message, in a file of its own (the last 20 are kept)."""
        folder = self.runtime.role_dir("away")
        path = folder / f"notification-{int(time.time() * 1000)}.txt"
        path.write_text(body + "\n", encoding="utf-8")
        for old in sorted(folder.glob("notification-*.txt"))[:-20]:
            old.unlink(missing_ok=True)
        return path

    def messenger_busy(self) -> bool:
        row = self.runtime.toolset.last_special(MESSENGER)
        return bool(
            row and not row["closed_at"] and row["status"] in {"queued", "starting", "working"}
        )

    async def t_report_notified(self, params: ReportNotified, caller: str):
        self.check(caller, (SPECIAL, MESSENGER))
        if params.ok:
            if params.method.strip():
                self.store.set("away_method", params.method.strip()[:1000])
            if params.where.strip():
                self.store.set("away_where", params.where.strip()[:300])
        self.store.set(
            "away_last",
            json.dumps(
                {
                    "at": now(),
                    "ok": params.ok,
                    "where": params.where[:300],
                    "error": params.error[:500],
                }
            ),
        )
        failing = self.store.get("away_failing") == "1"
        self.store.set("away_failing", "0" if params.ok else "1")
        if not params.ok and not failing:
            # Said once, not after every attempt: Settings shows the latest until it works.
            self.runtime.add_message(
                self.runtime.router_id(),
                "notice",
                f"Could not reach you while you were away: {params.error[:300]}. Change how in "
                "Settings → Connections (this is said once; Settings shows the latest attempt).",
                {"settings": "connections"},
            )
        self.runtime.publish_state()
        return "Recorded."

    async def t_report_owner_replies(self, params: ReportOwnerReplies, caller: str):
        self.check(caller)
        if not params.replies:
            raise ValueError(
                'No reply was given: pass replies=[{"code": "ABCDE", "text": "the owner\'s '
                'reply, exactly"}], one item per code.'
            )
        items = {item["code"]: item for item in self.waiting_items()}
        done = []
        for reply in params.replies:
            code = reply.code.strip().upper()
            item = items.get(code)
            if not item:
                done.append(f"{code}: nothing waits for that code (answered already?)")
                continue
            try:
                outcome = await self.apply(item, reply.text.strip())
            except (ValueError, LookupError, RuntimeError) as exc:
                outcome = f"not done: {exc}"
            done.append(f"{code}: {outcome}")
            self.runtime.add_message(
                self.runtime.router_id(),
                "notice",
                f"You answered from {self.store.get('away_where') or 'elsewhere'}: [{code}] "
                f"{reply.text[:200]} → {outcome}",
                {"away_reply": True},
            )
        self.runtime.publish_state()
        return "\n".join(done) or "Nothing to apply."

    async def apply(self, item: dict, text: str) -> str:
        """The owner's answer to one card, from elsewhere."""
        answer = CODE.sub("", text, count=1).strip(" :,-")
        word = answer.lower().strip(" .!")
        if item.get("ask"):
            if not answer:
                raise ValueError("write your answer after the code")
            await self.runtime.owner_send(self.runtime.router_id(), answer)
            return "sent to the Router"
        if draft := item.get("draft"):
            kind = draft["kind"]
            drafts = self.runtime.drafts
            if kind == "merge":
                if word in SEND_WORDS | {"merge", "合并"}:
                    merged = await drafts.resolve(draft["id"], DraftDecision(action="send"))
                    return merged.get("outcome") or "merged"
                if word in REJECT_WORDS:
                    await drafts.resolve(draft["id"], DraftDecision(action="reject"))
                    return "left as it is"
                raise ValueError('answer "merge" or "no"')
            if kind in {"slack", "github", "reply"}:
                if word in SEND_WORDS:
                    await drafts.resolve(draft["id"], DraftDecision(action="send"))
                    return "sent" if kind != "reply" else "marked as sent"
                if word in REJECT_WORDS:
                    await drafts.resolve(
                        draft["id"], DraftDecision(action="reject", reason="From away")
                    )
                    return "dropped"
                raise ValueError('answer "send" or "no"')
            if kind == "decision":
                options = draft["target"].get("options") or []
                if word.isdigit() and 1 <= int(word) <= len(options):
                    answer = options[int(word) - 1]["choice"]
                await drafts.resolve(draft["id"], DraftDecision(action="answer", text=answer))
                return f"decided: {answer[:100]}"
            if kind == "workspace":
                options = draft["target"].get("options") or []
                if not (word.isdigit() and 1 <= int(word) <= len(options)):
                    raise ValueError("answer with the number of a folder")
                choice = options[int(word) - 1]["path"]
                await drafts.resolve(draft["id"], DraftDecision(action="choose", choice=choice))
                return f"chose {choice}"
            await drafts.resolve(draft["id"], DraftDecision(action="answer", text=answer))
            return "answered"
        if question := item.get("question"):
            if not answer:
                raise ValueError("write your answer after the code")
            await self.runtime.task_actions.answer_question(question["session_id"], answer)
            return "sent to the worker"
        task = item["task"]
        if task["status"] == "failed":
            if word != "retry":
                raise ValueError('answer "retry"')
            await self.runtime.task_actions.retry(task["id"])
            return "retrying"
        await self.runtime.task_actions.follow_up(task["id"], answer)
        return "sent to the worker"

    def tools(self) -> list[tuple]:
        return [
            (
                "report_found",
                "Hand Chief of Staff what the scan found that needs the owner.",
                ReportFound,
                self.t_report_found,
            ),
            (
                "report_owner_words",
                "Pass messages the owner themself wrote, so Chief of Staff learns how they write.",
                ReportOwnerWords,
                self.t_report_owner_words,
            ),
            (
                "report_notified",
                "Say whether the notification reached the owner, where, and how.",
                ReportNotified,
                self.t_report_notified,
            ),
            (
                "report_owner_replies",
                "Pass the owner's answers to Chief of Staff's notifications (by code).",
                ReportOwnerReplies,
                self.t_report_owner_replies,
            ),
        ]


TRUST = UNTRUSTED  # What the connector hands on is untrusted content.
