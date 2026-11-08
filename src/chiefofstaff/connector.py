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
import secrets
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
from .sources.slack import PERMALINK_CHANNEL, message_keys, messages
from .store import hours_ago, now

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

SPECIAL = "connector"
MESSENGER = "messenger"  # Sends the owner's away notifications, outside the connector's sandbox.
MESSENGER_TITLE = "Tell you when you're away"
TITLE = "Watch for you"
TICK = float(os.environ.get("CHIEFOFSTAFF_CONNECTOR_TICK", "60"))
LOOKBACK_HOURS = 24
# Scans overlap by this much: what arrived while the last one ran is read again (found items are
# never handed over twice).
OVERLAP_SECONDS = 600
# A fresh conversation once its context is this large: each scan carries what it needs.
FRESH_AFTER = 120_000
# Kinds of needs-you cards a notification lists, and how a reply settles each.
NOTIFIED_KINDS = {"slack", "github", "reply", "question", "decision", "workspace", "merge"}
CODE = re.compile(r"\b([A-Z2-7]{5})\b")
# A review's verdict as a notification says it.
VERDICTS = {"approve": "Approve", "request_changes": "Request changes", "comment": "Comment"}
# A PR's checks as a merge card has them (receipts.checks_state).
CHECK_WORDS = {
    "passing": "checks pass",
    "failing": "checks fail",
    "pending": "checks still running",
    "none": "no checks",
}
# A folder choice's answer for no project (the card's "No project").
NO_PROJECT_WORDS = {"none", "no project", "\u65e0"}
# Answer codes: random, issued only for what a notification told, single-use, for a week.
CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
CODE_DAYS = 7
# A notification lists at most this many items (the rest: "N more").
MAX_AWAY_ITEMS = 5
# After a failed notification, the next try waits this long; a run that never said how it went
# counts as failed after PENDING_SECONDS.
RETRY_SECONDS = 600
PENDING_SECONDS = 900
# The owner's replies are read for this long after the last notification.
READ_REPLIES_HOURS = 24
# Where a notification sent with send_owner_slack_dm went, as the owner reads it.
OWN_SLACK_DM = "your own Slack DM"


def yours(where: str) -> str:
    """Where a notification went, as the owner reads it: the messenger writes of them in the
    third person ("Owner's own Slack DM", "a DM to the owner")."""
    where = re.sub(r"\b(?:the\s+)?owner['\u2019]s\b", "your", where, flags=re.IGNORECASE)
    where = re.sub(r"\b(?:the\s+)?owner\b", "you", where, flags=re.IGNORECASE)
    where = re.sub(r"\bthemsel(?:f|ves)\b", "yourself", where, flags=re.IGNORECASE)
    return re.sub(r"\btheir\b", "your", where, flags=re.IGNORECASE)


# The messenger's way to the owner when nothing else reaches them: their own Slack DM, through
# Chief of Staff's own Slack connection (it gets no Slack tools of its own).
SLACK_DM_HINT = (
    "Chief of Staff's Slack connection reaches the owner's own Slack DM (a message to themself: "
    "they see it there, without a phone notification): send_owner_slack_dm sends version A or "
    "B there, and read_owner_slack_dm reads their replies there later. Use it when that is how "
    'they want to get it ("my own Slack DM"), or when nothing else here reaches them.'
)
SEND_WORDS = {"send", "ok", "okay", "yes", "y", "approve", "lgtm", "发", "发送", "好", "可以"}
REJECT_WORDS = {"reject", "no", "n", "skip", "don't", "dont", "不", "不要", "拒绝"}
GO_ON_WORDS = {"go on", "go", "continue", "resume", "继续"}
# What else a short send may say ("yes, send it", "go ahead and send", "好的 发吧").
SEND_FILLER = {
    "it", "pls", "please", "plz", "now", "go", "ahead", "and", "sure", "yep", "yeah", "yup",
    "thanks", "thx", "ty", "just", "this", "that", "one", "reply", "out", "send it",
}  # fmt: skip
SEND_PHRASES = {"发吧", "发送吧", "发出去", "发了吧", "好的", "可以发", "行", "没问题", "嗯"}
# Words that hold a send back ("do not send", "ok wait", "not yet, send later").
HOLD_WORDS = {
    "not", "no", "don't", "dont", "never", "nope", "wait", "hold", "later", "yet", "stop",
    "until", "tomorrow", "before", "after", "once", "when", "if", "unless",
}  # fmt: skip
# A short "don't send": nothing but a no and a send (dropped, as "no" is).
NO_SEND_WORDS = {"do", "not", "don't", "dont", "never", "no", "send", "it", "this", "please", "pls"}
# Chinese, written without spaces ("好的发吧", "可以发了"): read by its parts, not by words.
# "先别发", "一会再发", "待会发", "回头再发": not now.
CJK_HOLDS = (
    "先别", "先不", "别", "不", "等", "稍后", "晚点", "暂", "慢", "一会", "待会", "回头", "再",
)  # fmt: skip
CJK_NO = {
    "不要发送",
    "先不要发",
    "不要发了",
    "暂时别发",
    "别发",
    "不要发",
    "不发",
    "不用发",
    "别发了",
    "不用了",
    "不要了",
    "别",
    "不要",
    "不",
    "算了",
}
CJK_SENDS = ("没问题", "可以", "发送", "发出去", "发", "好", "行", "嗯", "对")
CJK_FILLER = (
    "吧",
    "了",
    "的",
    "啊",
    "呀",
    "哈",
    "就",
    "直接",
    "出去",
    "一下",
    "儿",
)
# A question ("ok?", "发吗", "可以吗"): the owner asks, they do not say send.
ASKS = re.compile(r"[?\uff1f]|[\u5417\u4e48\u5462\u561b]")
# What else holds a send back, or says no ("hold on", "never mind", "nah").
HOLD_PHRASES = re.compile(r"\b(hold on|hang on|one sec|not now|never ?mind|nevermind|nah)\b")
CJK = re.compile(r"[\u3400-\u9fff]")


def cjk_answer(text: str) -> str:
    """send_answer for an answer in Chinese."""
    plain = re.sub(r"[\W_]+", "", text)
    if not plain or len(plain) > 8:
        return ""
    if plain in CJK_NO:
        return "no"
    if any(hold in plain for hold in CJK_HOLDS):
        rest = plain
        for part in (*CJK_HOLDS, *CJK_SENDS, *CJK_FILLER):
            rest = rest.replace(part, "")
        # "先别发", "等等", "暂时不要", "好 但先等等": held; more words: what to change.
        return "" if len(rest) > 3 else "unclear"
    rest, sends = plain, False
    for part in CJK_SENDS:
        sends = sends or part in rest
        rest = rest.replace(part, "")
    for part in CJK_FILLER:
        rest = rest.replace(part, "")
    return "send" if sends and not rest else ""


def send_answer(word: str) -> str:
    """What a short answer to a draft says: "send", "no", "unclear" (a send held back:
    "ok wait", "not yet, send later", "先别发"), or "" (something else: what to change)."""
    word = word.replace("\u2019", "'")  # A phone's "don\u2019t".
    if word in REJECT_WORDS:
        return "no"
    if ASKS.search(word):
        # "ok?", "send now?", "发了吗": a question back, never a send (nor something to change
        # when it is only that).
        rest = ASKS.sub(" ", word)
        return "unclear" if not rest.strip() or send_answer(rest) in {"send", "no"} else ""
    if HOLD_PHRASES.search(word):
        rest = HOLD_PHRASES.sub(" ", word)
        return (
            "unclear"
            if send_answer(rest) in {"send", "", "unclear"} and len(rest.split()) <= 3
            else ""
        )
    if CJK.search(word) and not re.search(r"[A-Za-z]", word):
        return cjk_answer(word)
    said = re.findall(r"[\w']+", word)
    if not said or len(said) > 12:
        return ""
    tokens = set(said)
    plain = SEND_WORDS | SEND_PHRASES | SEND_FILLER | {"send"}
    if tokens & HOLD_WORDS:
        if (
            tokens <= NO_SEND_WORDS
            and "send" in tokens
            and not (said[0] == "no" and said[-1] in {"send", "it"} and "not" not in tokens)
        ):
            return "no"  # "do not send", "don't send it"; not "no, send it".
        # A send held back ("send it after lunch", "ok, send it once Bob confirms", "yes send.
        # actually wait"): not a send, and not something to redraft either.
        if tokens & (SEND_WORDS | SEND_PHRASES | {"send"}) or tokens <= plain | HOLD_WORDS:
            return "unclear"
        return ""
    if len(said) > 5:
        return ""
    if tokens & (SEND_WORDS | SEND_PHRASES) and tokens <= plain:
        return "send"
    return ""


CANCEL_WORDS = {"cancel", "取消"}
HID_IDLE = re.compile(r'"HIDIdleTime" = (\d+)')


def later(first: str, second: str) -> bool:
    """Whether ISO time `first` is after `second` (False when either cannot be read)."""
    try:
        # Times the model wrote: "Z" becomes "+00:00" first, which also reads a date-only
        # "2026-11-07Z" that fromisoformat alone refuses.
        first_time = datetime.fromisoformat(first.replace("Z", "+00:00"))  # noqa: FURB162
        return first_time > datetime.fromisoformat(second.replace("Z", "+00:00"))  # noqa: FURB162
    except (ValueError, TypeError):
        return False


def watch_key(what: str) -> str:
    """A watch by what it watches (renumbering or reordering watches does not reset it)."""
    return hashlib.sha256(" ".join(what.lower().split()).encode()).hexdigest()[:16]


def without_code(text: str, code: str) -> str:
    """An answer without its code (any case, with or without brackets): "URN2F drop them" and
    "[urn2f] drop them" are both "drop them"."""
    return re.sub(
        rf"\[?\b{re.escape(code)}\b\]?[\s:,.-]*", "", text, count=1, flags=re.IGNORECASE
    ).strip()


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


class Incomplete(BaseModel):
    watch: int = Field(description="The number of the watch")
    reason: str = Field(
        description="What it could not cover: a search that returns only some results (the "
        "newest few threads), a tool that failed, a source it cannot reach"
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
    incomplete: list[Incomplete] = Field(
        default_factory=list,
        description="Every watch whose search could not cover all that is new since its time, "
        "and why. A watch left out here counts as fully covered; its next scan starts later.",
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
    replies: str = Field(
        default="",
        description="How you can read the owner's replies in that same conversation later (the "
        "tool and its arguments), when you sent the version with answer codes; empty when you "
        "cannot (you sent the version without codes)",
    )
    error: str = Field(default="", description="Why it could not be sent")


class SendOwnerSlackDm(BaseModel):
    version: Literal["A", "B"] = Field(
        description="A: the version with answer codes (you then read the owner's replies with "
        "read_owner_slack_dm); B: the one without"
    )


class ReadOwnerSlackDm(BaseModel):
    pass


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
    return not after_hours(hours, moment)


def after_hours(hours: str, moment: datetime) -> str:
    """Why `moment` is after hours: "weekend" (work hours are Monday to Friday), or
    "outside_hours" on a workday; "" within work hours."""
    if moment.weekday() >= 5:
        return "weekend"
    start, end = (minutes_of(part) for part in hours.split("-"))
    current = moment.hour * 60 + moment.minute
    within = start <= current < end if start <= end else current >= start or current < end
    return "" if within else "outside_hours"


# A question to the owner: a sentence that ends with "?" (or its fullwidth form).
QUESTION_END = re.compile("[?\uff1f][\"')\\]*_\u201d\u2019\u300d\u300f]*(?=\\s|$)")


def without_link(link: re.Match) -> str:
    """A link taken out of a sentence, but not what ends the sentence after it."""
    url = link.group(0)
    return " " + url[len(url.rstrip(".,;:!?)]'\"")) :]


LIST_LINE = re.compile(r"^\s*(?:[-*+\u2022]|\d+[.)])\s+")
# A list line that says what someone wrote or asked ("- **Gina (#textkit):** …?"): theirs,
# not a question to the owner.
QUOTED_LINE = re.compile(
    r"^\s*(?:[-*+\u2022]|\d+[.)])\s+(?:\*\*|\*|__|_)?"
    r"(?P<who>(?:[A-Z][\w.'-]*|[\u4e00-\u9fff]+)(?:\s+(?:and|&|[A-Z][\w.'-]*))*"
    r"(?:\s*\([^)\n]*\))?)\s*(?:\*\*|\*|__|_)?\s*[:\uff1a]"
)
# Labels a list line may start with that name no one ("- **Decision:** merge now?").
LABELS = {
    "a", "action", "actions", "also", "approve", "ask", "asks", "blocker", "blockers", "both",
    "card", "cards", "choice", "confirm", "deadline", "decide", "decision", "decisions", "draft",
    "drafts", "due", "either", "how", "i", "issue", "me", "merge", "needs", "next", "note",
    "notes", "option", "options", "or", "plan", "pr", "proposal", "q", "question", "questions",
    "recommendation", "result", "results", "review", "risk", "send", "status", "summary",
    "to", "today", "todo", "update", "waiting", "what", "when", "which", "why", "you",
}  # fmt: skip


def quoted(line: str) -> bool:
    """Whether a list line quotes someone (see QUOTED_LINE)."""
    found = QUOTED_LINE.match(line)
    return bool(found) and found.group("who").split()[0].casefold() not in LABELS


def asks_owner(text: str, answer: bool = False) -> bool:
    """Whether a message asks the owner something: its last paragraph has a sentence ending
    with "?", outside code, tables and links (a "?" in a table cell or a "?q=" is no question),
    and outside what it quotes others asking ("- *Gina:* deprecate it in 0.5?", and the lines
    under it). `answer`: it answers the owner's own question, and its lists are that answer."""
    text = re.sub(r"```.*?(?:```|$)", "\n\n", text or "", flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", " ", text)
    text = re.sub(r"\[([^\]\n]*)\]\([^)\s]*\)", r"\1", text)
    # A link goes; what ends the sentence after it stays ("…see https://x/a?").
    text = threads.LINK.sub(without_link, text)
    lines, quoting = [], -1
    for line in text.splitlines():
        indent = len(line) - len(line.lstrip())
        if not line.strip() or (quoting >= 0 and indent > quoting):
            lines.append("" if not line.strip() else " ")  # Under a quoted line: left out.
            continue
        quoting = -1
        if line.count("|") >= 2:
            continue
        if quoted(line) or (answer and LIST_LINE.match(line)):
            quoting = indent
            lines.append(" ")
            continue
        lines.append(line)
    paragraphs = [part for part in re.split(r"\n\s*\n", "\n".join(lines)) if part.strip()]
    return bool(paragraphs) and bool(QUESTION_END.search(paragraphs[-1]))


def gist(text: str, limit: int = 300) -> str:
    """A text's first lines as Slack text (mrkdwn), short: one item of a notification. Its
    Markdown becomes Slack's (*bold*, <link|text>); &, < and > are escaped."""
    plain = re.sub(r"```.*?(```|$)", " ", text or "", flags=re.DOTALL)
    lines = []
    for line in plain.splitlines():
        line = re.sub(r"^\s*(#{1,6}|>+|[-*\u2022+]|\d+[.)])\s+", "", line).strip()
        if line and not re.fullmatch(r"[|:\- ]+", line):
            lines.append(line)
        if len(lines) == 2:
            break
    said = " ".join(lines)
    if len(said) > limit:
        said = said[: limit - 1].rsplit(" ", 1)[0] + "\u2026"
    said = mrkdwn(said)
    said = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", said)
    parts = re.split(r"(`[^`]*`)", said)
    for index in range(0, len(parts), 2):  # Outside `code`: Markdown's emphasis as Slack's.
        part = re.sub(r"\*\*(.+?)\*\*", r"*\1*", parts[index])
        # __bold__, not the underscores of a name or path ("textkit/__init__.py").
        part = re.sub(r"(?<![\w/.])__(?=\S)(.+?)(?<=\S)__(?![\w/.])", r"*\1*", part)
        parts[index] = re.sub(r"~~(.+?)~~", r"~\1~", part)
    return "".join(parts)


def mrkdwn(text: str) -> str:
    """Text for Slack: &, < and > escaped (Slack's own markup uses them)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
        if settings.away_notify:
            await self.read_replies()

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
        if not any(watch.enabled for watch in settings.watches):
            return 0
        if not self.after_hours_reason(moment):
            return settings.watch_minutes
        return settings.watch_after_hours_minutes

    def after_hours_reason(self, moment: datetime | None = None) -> str:
        """Why the watches run at their after-hours pace now: "weekend" (all of Saturday and
        Sunday), "outside_hours" (a workday outside the work hours); "" in work hours."""
        # Work hours are the owner's wall-clock time.
        moment = moment or datetime.now().astimezone()
        return after_hours(self.store.settings().watch_hours, moment)

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
            if not watches:
                raise ValueError("Add a watch first.")
            if self.busy():
                raise ValueError("A scan is running already.")
            self.store.set("watch_scanned_at", str(time.time()))  # When the next one is due.
            self.store.set("watch_started_at", str(time.time()))
            # Each watch from where it was last covered in full (a little before: what came in
            # while that scan ran), not from the last scan started: one that failed, or could
            # see only part of its source, leaves its window to the next.
            self.store.set("watch_scan", json.dumps([watch_key(w.what) for w in watches]))
            lines = ["Scan now. The owner's watches, each with the time to look from:"]
            for number, watch in enumerate(watches, 1):
                since = self.watch_since(watch.what)
                start = (
                    datetime.fromtimestamp(since).astimezone().isoformat(timespec="minutes")
                    if since
                    else f"the last {LOOKBACK_HOURS} hours"
                )
                lines.append(
                    f"{number}. {watch.what}"
                    + (f" (use: {watch.how})" if watch.how else "")
                    + f": new since {start}"
                )
            if methods := self.store.get("watch_methods"):
                lines.append(
                    f"How you ran them before (use it again if it still works):\n{methods}"
                )
            lines.append(prompts.CONNECTOR_SCAN)
            shown = "; ".join(w.what for w in watches)
            await self.send("scan", "\n\n".join(lines), "Scan: " + shown)
            if by_owner:
                self.runtime.publish_state()

    def watch_since(self, what: str) -> float:
        """Where a watch's next scan starts: a little before the last scan that covered it in
        full (at most LOOKBACK_HOURS back); 0 for a new one."""
        done = float(
            self.store.get(f"watchdone:{watch_key(what)}") or self.store.get("watch_done_at") or 0
        )
        return max(done - OVERLAP_SECONDS, time.time() - LOOKBACK_HOURS * 3600) if done else 0

    async def t_report_found(self, params: ReportFound, caller: str):
        self.check(caller)
        started = self.store.get("watch_started_at") or str(time.time())
        scanned = json.loads(self.store.get("watch_scan") or "[]")
        # A watch it could not run (problems) and said nothing more about: not covered.
        partial = {item.watch: item.reason.strip()[:300] for item in params.incomplete}
        if params.problems.strip() and not partial:
            partial = dict.fromkeys(range(1, len(scanned) + 1), params.problems.strip()[:300])
        incomplete = []
        for number, key in enumerate(scanned, 1):
            if number in partial:
                # Its window stays open: the next scan looks from where it was last covered.
                self.store.set(
                    f"watchpartial:{key}",
                    json.dumps({"reason": partial[number], "at": now()}),
                )
                incomplete.append({"watch": number, "reason": partial[number]})
            else:
                self.store.set(f"watchdone:{key}", started)
                self.store.delete(f"watchpartial:{key}")
        if not incomplete:
            self.store.set("watch_done_at", started)
        if params.methods.strip():
            self.store.set("watch_methods", params.methods.strip()[:2000])
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
            # A Slack message is known by its own key too: the Slack source (a followed thread,
            # a DM) does not hand it over again, nor this one what it handed over already.
            slack_keys = [f"slack:{stamp}" for stamp in threads.slack_messages(item.link)]
            if any(self.store.one("SELECT id FROM triggers WHERE key=?", (k,)) for k in slack_keys):
                self.store.add_trigger("watch", key, text[:4000])
                continue
            if await self.runtime.on_trigger("watch", [key, *slack_keys], text, display=display):
                new += 1
        self.store.set(
            "watch_last",
            json.dumps(
                {
                    "at": now(),
                    # What was new and handed over (Settings: "N found"), of all it saw.
                    "found": new,
                    "seen": len(params.items),
                    "problems": params.problems[:500],
                    "incomplete": incomplete,
                }
            ),
        )
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
        self.store.set(f"threadlink:{key}", link)  # A worker that follows it links to it.
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
        row = self.runtime.toolset.last_special(SPECIAL)
        finished = json.loads(self.store.get("watch_last") or "{}")
        return {
            "every": self.interval(),
            "after_hours_reason": self.after_hours_reason(),
            # When the last scan finished (its report), not when one started: "scanned just
            # now" is about what it found.
            "scanned_at": finished.get("at", ""),
            "last": finished,
            "scanning": self.busy(),
            "session_id": row["id"] if row else "",
            "away": {
                "enabled": settings.away_notify,
                "last": json.loads(self.store.get("away_last") or "{}"),
                "where": yours(self.store.get("away_where") or ""),
                "can": self.capabilities(),
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
        """What needs the owner now, each with a stable id (what a notification told of)."""
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
            items.append({"id": f"draft:{key}", "draft": draft})
        from .runtime import TASKS_SHOWN_HOURS

        # Also those that ended lately: a failed task waits for the owner (Retry), as Home
        # shows it under Needs you.
        tasks = self.runtime.tasks.listed(include_ended_since=hours_ago(TASKS_SHOWN_HOURS))
        about = self.runtime.drafts.question_draft
        for question in self.runtime.asks.owner_questions():
            asker = self.runtime.tasks.find(question["task_id"]) if question["task_id"] else None
            if asker and about(asker, question.get("text") or ""):
                continue  # Told as its draft: deciding that answers it (settle_question).
            items.append(
                {"id": f"question:{question['session_id']}:{question['at']}", "question": question}
            )
        if (ask := self.router_question()) and not self.relays(ask, tasks):
            items.append({"id": f"router:{ask['id']}", "ask": ask})
        for task in tasks:
            if task["status"] == "waiting" and (
                task["id"] in deciding or task["session_id"] in carded
            ):
                continue
            if (
                task["status"] == "waiting"
                and not self.store.get(f"stopped:{task['id']}")
                and about(task)
            ):
                continue  # It asks only about its own draft: told once, as that draft.
            if task["status"] in {"waiting", "failed"}:
                # Its question, by its words: a new question on the same task is a new item.
                asked = hashlib.sha256((task.get("progress") or "").encode()).hexdigest()[:10]
                items.append({"id": f"task:{task['id']}:{task['status']}:{asked}", "task": task})
        return items

    def relays(self, ask: dict, tasks: list[dict]) -> bool:
        """Whether the Router's question is a worker's, retold: a question of a waiting task (or
        the failure of a failed one) reached it after the owner's last message and before it
        asked, in much the same words. The owner is told it once, as the task's."""
        waiting = {task["id"]: task for task in tasks if task["status"] in {"waiting", "failed"}}
        asked = words_of(ask["text"])
        # A question a session asked the owner with no task (the Slack session's), retold.
        for question in self.runtime.asks.owner_questions():
            theirs = words_of(question.get("text") or "")
            if asked and theirs and len(asked & theirs) >= 0.4 * len(asked):
                return True
        if not waiting:
            return False
        latest = self.store.messages(self.runtime.router_id(), limit=40)
        owner = max((m["id"] for m in latest if m["kind"] == "owner"), default=0)
        for message in latest:
            task = waiting.get(message["meta"].get("task_id") or "")
            if not (owner < message["id"] < ask["id"] and message["kind"] == "inbound" and task):
                continue
            theirs = words_of(f"{message['text']} {task['progress']}")
            if asked and len(asked & theirs) >= 0.4 * len(asked):
                return True
        return False

    @staticmethod
    def router_asks(messages: list[dict]) -> list[dict]:
        """The Router's messages that ask the owner something or need them. Its answer to the
        owner's own question (a turn of theirs: its words there are not internal) asks nothing
        by its lists: they are the answer (what the team asked them, say)."""
        found, asked = [], False
        for m in sorted(messages, key=lambda m: m["id"]):
            if m["kind"] == "owner":
                asked = asks_owner(m["text"])
            if m["kind"] not in {"assistant", "update"} or m["meta"].get("internal"):
                continue
            answer = asked and m["kind"] == "assistant"
            if m["meta"].get("needs_action") or asks_owner(m["text"], answer=answer):
                found.append(m)
        return found

    def router_question(self) -> dict | None:
        """What the Router asked the owner in the chat and they have not answered yet: its
        latest message after theirs, when it asks something or needs them."""
        latest = self.store.messages(self.runtime.router_id(), limit=40)
        owner = max((m["id"] for m in latest if m["kind"] == "owner"), default=0)
        asks = [m for m in self.router_asks(latest) if m["id"] > owner]
        if not asks or self.settled(asks[-1]):
            return None
        return asks[-1]

    def settled(self, ask: dict) -> bool:
        """Whether what the Router said needs the owner was dealt with since, on Home: the
        drafts it names are no longer waiting; or, when it asks nothing (it points at a card:
        "resend the draft"), the owner decided a card after it."""
        named = set(re.findall(r"\bd[0-9a-f]{8}\b", ask["text"]))
        if named:
            pending = {draft["id"] for draft in self.store.drafts()}
            return not (named & pending)
        if asks_owner(ask["text"]):
            return False  # A question is answered by the owner, not by a card.
        return bool(
            self.store.one(
                "SELECT id FROM drafts WHERE resolved_at>? AND status IN ('sent', 'answered') "
                "LIMIT 1",
                (ask["created_at"],),
            )
        )

    def told_router_questions(self) -> list[dict]:
        """The Router's questions the owner was told of while away: each can be answered by its
        code (even when they wrote to the Router since, about something else) until the Router
        asks something newer."""
        router = self.runtime.router_id()
        told = [
            item.removeprefix("router:")
            for item in json.loads(self.store.get("away_sent") or "[]")
            if item.startswith("router:")
        ]
        if not told:
            return []
        recent = self.store.messages(router, limit=40)
        newest = max((m["id"] for m in self.router_asks(recent)), default=0)
        items = []
        for number in told:
            if not number.isdigit() or int(number) < newest:
                continue  # Superseded: the Router asked something newer.
            try:
                ask = self.store.message(int(number))
            except LookupError:
                continue
            if ask["session_id"] == router:
                items.append({"id": f"router:{number}", "ask": ask})
        return items

    def open_items(self) -> dict[str, dict]:
        """What a code the owner got can still answer, by item id: what waits for them now,
        and the Router's questions they were told of (see told_router_questions)."""
        items = {item["id"]: item for item in self.waiting_items()}
        for item in self.told_router_questions():
            items.setdefault(item["id"], item)
        return items

    # Answer codes: random, so a code cannot be worked out from what others see (a PR, a
    # message id); issued for an item when a notification lists it; used once.

    def code_for(self, item_id: str) -> str:
        """The code the owner answers this item by: the one it was given, else a new one."""
        if (code := self.store.get(f"awayitem:{item_id}")) and self.code_item(code) == item_id:
            return code
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
            if not self.store.get(f"awaycode:{code}"):
                break
        self.store.set(f"awaycode:{code}", json.dumps({"item": item_id, "at": time.time()}))
        self.store.set(f"awayitem:{item_id}", code)
        return code

    def code_item(self, code: str) -> str:
        """The item a code was issued for ("" when none, or it expired)."""
        found = json.loads(self.store.get(f"awaycode:{code.upper()}") or "null")
        if not found:
            return ""
        if time.time() - found["at"] > CODE_DAYS * 86400:
            self.store.delete(f"awaycode:{code.upper()}")
            return ""
        return found["item"]

    def open_codes(self) -> list[str]:
        """The codes of what the owner was told of and that still waits for them."""
        sent = set(json.loads(self.store.get("away_sent") or "[]"))
        codes = [
            code
            for item_id in self.open_items()
            if item_id in sent
            and (code := self.store.get(f"awayitem:{item_id}"))
            and self.code_item(code) == item_id
        ]
        if test := json.loads(self.store.get("away_test") or "null"):
            codes.append(test["code"])
        return codes

    def told_codes(self) -> list[str]:
        """Every code the owner was sent and has not used (nor expired), whether or not its item
        still waits: a reply with one that no longer does is told so, not dropped."""
        codes = []
        for item_id in json.loads(self.store.get("away_sent") or "[]"):
            code = self.store.get(f"awayitem:{item_id}")
            if code and self.code_item(code) == item_id and code not in codes:
                codes.append(code)
        return codes

    def describe(self, item: dict) -> tuple[str, str, str]:
        """An item as a notification line: (what, its gist, how to answer by code)."""
        if item.get("test"):
            return (
                "Test from Chief of Staff",
                "This is how you hear from it when you are away.",
                "ok",
            )
        if ask := item.get("ask"):
            return "Chief of Staff asks", self.gist(ask["text"]), "<your answer>"
        if draft := item.get("draft"):
            target = draft["target"]
            kind = draft["kind"]
            reply = target.get("in_reply_to") or target.get("reply_to_comment")
            if kind == "github" and not reply and not target.get("own"):
                # What posting it does, in words: "send" alone would approve a PR unsaid.
                verdict = str(target.get("verdict") or "comment")
                title = (target.get("pr") or {}).get("title") or ""
                pr = f"{str(target.get('repo', '')).split('/')[-1]}#{target.get('number', '')}"
                head = f"Review of {pr}" + (f" \u201c{title}\u201d" if title else "")
                head += f": {VERDICTS.get(verdict, 'Comment')}"
                word = "approve" if verdict == "approve" else "post"
                return head, self.gist(draft["text"]), f"{word} · no"
            if kind == "github":
                author = (target.get("comment") or {}).get("author") or ""
                pr = f"{str(target.get('repo', '')).split('/')[-1]}#{target.get('number', '')}"
                title = (target.get("pr") or {}).get("title") or ""
                head = f"Reply{f' to {author}' if author else ''} on {pr}"
                head += f" \u201c{title}\u201d" if title and not reply else ""
                return head, self.gist(draft["text"]), "send · no"
            if kind in {"slack", "reply"}:
                where = target.get("to") or target.get("where") or target.get("url") or ""
                head = f"Draft to approve{f' · {where}' if where else ''}"
                return head, self.gist(draft["text"]), "send · no"
            if kind == "decision":
                options = target.get("options") or []
                listed = " ".join(f"{n}) {o.get('choice', '')}" for n, o in enumerate(options, 1))
                text = f"{target.get('question', draft['text'])} {listed}".strip()
                return "Decision", self.gist(text), "<number or your answer>"
            if kind == "merge":
                head = f"Ready to merge · {target['label']}"
                checks = CHECK_WORDS.get(target.get("checks") or "", "")
                said = target.get("title", "") + (f" ({checks})" if checks else "")
                return head, gist(said), "merge · no"
            if kind == "workspace":
                options = target.get("options") or []
                # Not at the start of the line: gist would take "1)" for a list's marker.
                listed = "Folders: " + " ".join(
                    f"{n}) {o['path']}" for n, o in enumerate(options, 1)
                )
                listed = " · ".join(
                    x
                    for x in (listed if options else "", "none: no project, or a folder's path")
                    if x
                )
                hint = "<number, none or a path>" if options else "<none or a path>"
                return f"Which folder for “{target.get('title', '')}”", gist(listed), hint
            return "Question", self.gist(draft["text"]), "<your answer>"
        if question := item.get("question"):
            about = question.get("task_title") or question["worker"]
            return f"“{about}” asks", self.gist(question["text"]), "<your answer>"
        task = item["task"]
        said = self.gist(task.get("progress") or "")
        if task["status"] == "failed":
            return f"Failed · “{task['title']}”", said, "retry"
        if self.store.get(f"stopped:{task['id']}"):
            # Stopped mid-work: it asks nothing, it waits for Go on or Cancel.
            return f"Stopped · “{task['title']}”", said, "go on · cancel"
        return f"“{task['title']}” asks", said, "<your answer>"

    def gist(self, text: str) -> str:
        """gist, of a text sessions wrote: without draft ids or Slack timestamps."""
        return gist(self.runtime.drafts.owner_text(text))

    def render(self, items: list[dict], more: int, codes: bool) -> tuple[str, dict]:
        """A notification as Slack text (mrkdwn) and as Block Kit, with answer codes or
        without (then the owner answers in Chief of Staff)."""
        sections = []
        for item in items:
            what, said, hint = self.describe(item)
            code = item["code"]
            head = f"*[{code}]* {mrkdwn(what)}" if codes else f"*{mrkdwn(what)}*"
            lines = [head]
            if said:
                lines.append(said)
            if codes:
                lines.append(f"Reply: `{code} {hint}`")
            sections.append("\n".join(lines))
        title = f"Chief of Staff needs you ({len(items) + more})"
        footer = (
            "Reply in this conversation with the code and your answer."
            if codes
            else "Answer in Chief of Staff on your computer: replies here are not read."
        )
        if more:
            footer = f"{more} more waiting: open Chief of Staff. " + footer
        text = "\n\n".join([f"*{title}*", *sections, f"_{footer}_"])
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
            *(
                {"type": "section", "text": {"type": "mrkdwn", "text": section[:2900]}}
                for section in sections
            ),
            {"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]},
        ]
        return text, {"blocks": blocks, "text": title}

    def pending_send(self) -> dict | None:
        """The notification the messenger is sending now (until it reports how it went)."""
        pending = json.loads(self.store.get("away_pending") or "null")
        if pending and time.time() - pending["at"] > PENDING_SECONDS:
            # Its run ended without a word: as good as failed; tried again later.
            self.store.delete("away_pending")
            self.store.set("away_retry_at", str(time.time() + RETRY_SECONDS))
            return None
        return pending

    async def notify_away(self, test: bool = False):
        """Tell the owner, where they are, what needs them that they have not been told of.
        Items count as told once the messenger says the message arrived."""
        if self.messenger_busy() or self.pending_send():
            if test:
                raise ValueError("A notification is being sent now: try again in a minute.")
            return  # After its current run: one message at a time.
        if not test and time.time() < float(self.store.get("away_retry_at") or 0):
            return  # The last one failed: not again at every tick.
        sent = set(json.loads(self.store.get("away_sent") or "[]"))
        fresh = [item for item in self.waiting_items() if item["id"] not in sent]
        if test:
            fresh = [{"id": f"test:{int(time.time())}", "test": True}]
        if not fresh:
            return
        shown, more = fresh[:MAX_AWAY_ITEMS], max(0, len(fresh) - MAX_AWAY_ITEMS)
        for item in shown:
            item["code"] = self.code_for(item["id"])
        files = await asyncio.to_thread(self.write_notification, shown, more)
        self.store.set(
            "away_pending",
            json.dumps(
                {
                    "at": time.time(),
                    "items": [item["id"] for item in shown],
                    "test": test,
                    "files": files,
                }
            ),
        )
        settings = self.store.settings()
        how = settings.away_how.strip() or prompts.AWAY_DEFAULT
        lines = [
            "Send the owner one of these messages, as it is (only to the owner themself, nobody "
            "else). Version A has answer codes: send it when you can read the owner's replies in "
            "that same conversation later with your tools. Otherwise send version B. Each is "
            "written as Slack text and as Slack Block Kit (JSON with blocks and a fallback text): "
            "send the Block Kit one when your tool can, else the text.",
            f"A: {files['codes.txt']} or {files['codes.json']}",
            f"B: {files['plain.txt']} or {files['plain.json']}",
            f"How the owner wants to get it: {how}",
        ]
        if self.slack_dm():
            lines.append(SLACK_DM_HINT)
        if method := self.store.get("away_method"):
            lines.append(f"What you used before: {method}")
        if replies := self.store.get("away_replies"):
            lines.append(f"How you read their replies before: {replies}")
        lines.append(
            "Then call report_notified: whether it arrived, where, the exact method, and how you "
            "can read their replies there (empty if you sent B). Never post it anywhere else or "
            "as the owner to anyone else."
        )
        await self.runtime.toolset.start_special(MESSENGER, MESSENGER_TITLE, "\n\n".join(lines))

    def write_notification(self, items: list[dict], more: int) -> dict[str, str]:
        """The message in files of its own: with and without answer codes, as Slack text and
        as Block Kit (the last 20 notifications are kept)."""
        folder = self.runtime.role_dir("away")
        stamp = int(time.time() * 1000)
        files = {}
        for name, codes in (("codes", True), ("plain", False)):
            text, blocks = self.render(items, more, codes)
            path = folder / f"notification-{stamp}-{name}"
            path.with_suffix(".txt").write_text(text + "\n", encoding="utf-8")
            path.with_suffix(".json").write_text(json.dumps(blocks, ensure_ascii=False))
            files[f"{name}.txt"] = str(path.with_suffix(".txt"))
            files[f"{name}.json"] = str(path.with_suffix(".json"))
        for old in sorted(folder.glob("notification-*"))[:-80]:
            old.unlink(missing_ok=True)
        return files

    def messenger_busy(self) -> bool:
        row = self.runtime.toolset.last_special(MESSENGER)
        return bool(
            row and not row["closed_at"] and row["status"] in {"queued", "starting", "working"}
        )

    async def read_replies(self):
        """While the owner may still answer what they were told, the messenger reads their
        replies in the conversation it sent to (only it does: what others write never reaches
        report_owner_replies)."""
        settings = self.store.settings()
        how = self.store.get("away_replies") or ""
        last = json.loads(self.store.get("away_last") or "{}")
        if not how or not last.get("ok") or self.messenger_busy() or self.pending_send():
            return
        told = datetime.fromisoformat(last["at"]).timestamp()
        if time.time() - told > READ_REPLIES_HOURS * 3600:
            return
        if not (codes := self.open_codes()):
            return
        codes += [code for code in self.told_codes() if code not in codes]
        read_at = float(self.store.get("away_read_at") or 0)
        if time.time() - read_at < max(1, settings.watch_minutes) * 60:
            return
        self.store.set("away_read_at", str(time.time()))
        where = self.store.get("away_where") or "the conversation you sent Chief of Staff's "
        # Kept as the owner reads it ("your own Slack DM"); the messenger reads it as theirs.
        where = re.sub(r"\byour\b", "the owner's", where, flags=re.IGNORECASE)
        text = (
            f"Read the owner's replies to Chief of Staff's notifications in {where} with: {how}\n\n"
            f"Pass each reply that names one of these codes with report_owner_replies (the code "
            f"and the reply exactly): {', '.join(codes)}. Only what the owner themself wrote "
            "there; a reply you passed before is fine to pass again. If there is none, end "
            "without calling it. Send nothing."
        )
        await self.runtime.toolset.start_special(MESSENGER, MESSENGER_TITLE, text)

    def slack_dm(self) -> bool:
        """Whether Chief of Staff's own Slack connection can send to the owner's own DM."""
        slack = self.runtime.slack
        return slack.configured() and (slack.can_send or not slack.fingerprint)

    async def owner_slack_id(self) -> str:
        slack = self.runtime.slack
        if not self.slack_dm():
            raise ValueError(
                "Slack is not connected to Chief of Staff (or its token cannot send): use another "
                "way, or report_notified(ok=false) saying so."
            )
        if not slack.status.get("user_id"):
            await slack.discover()  # Who the owner is on Slack: read with its tools.
        if not (user := slack.status.get("user_id") or ""):
            raise ValueError("Slack did not say who the owner is: use another way.")
        return user

    async def t_send_owner_slack_dm(self, params: SendOwnerSlackDm, caller: str):
        """The notification being sent now, as it is, to the owner's own Slack DM: nothing
        else, to nobody else."""
        self.check(caller, (MESSENGER,))
        pending = json.loads(self.store.get("away_pending") or "null")
        if not pending or not pending.get("files"):
            raise ValueError("No notification is being sent now.")
        user = await self.owner_slack_id()
        name = "codes.txt" if params.version == "A" else "plain.txt"
        text = (await asyncio.to_thread(Path(pending["files"][name]).read_text, "utf-8")).strip()
        result = await self.runtime.slack.send({"channel_id": user}, text)
        keys = message_keys(result)
        for key in keys:
            # Not a message to the owner for the Slack watcher, nor a reply of theirs.
            self.store.add_trigger("slack-sent", key, text)
        sent = max((float(key.removeprefix("slack:")) for key in keys), default=time.time())
        before = json.loads(self.store.get("away_slack_dm") or "null") or {}
        recent = time.time() - before.get("after", 0) < READ_REPLIES_HOURS * 3600
        self.store.set(
            "away_slack_dm",
            json.dumps(
                {
                    # The DM itself (D…), as Slack linked the message; else the owner's id.
                    "channel": (PERMALINK_CHANNEL.findall(result) or [user])[0],
                    # Replies to every notification still answerable are read.
                    "after": min(before["after"], sent) if recent else sent,
                }
            ),
        )
        replies = "read_owner_slack_dm" if params.version == "A" else ""
        # Where it went, in the owner's words, whatever the messenger reports.
        self.store.set("away_pending", json.dumps({**pending, "slack_dm": True}))
        return (
            "Sent to the owner's own Slack DM. Now call report_notified(ok=true, where=\"your "
            f'own Slack DM", method="send_owner_slack_dm", replies="{replies}").'
        )

    async def t_read_owner_slack_dm(self, params: ReadOwnerSlackDm, caller: str):
        """What the owner wrote in their own Slack DM since a notification was sent there:
        their own messages only, not the notifications."""
        self.check(caller, (MESSENGER,))
        dm = json.loads(self.store.get("away_slack_dm") or "null")
        if not dm:
            raise ValueError("Nothing was sent to the owner's own Slack DM.")
        user = await self.owner_slack_id()
        listed = await self.runtime.slack.mcp.call(
            "slack_read_channel", {"channel_id": dm["channel"], "limit": 50}
        )
        found = [
            entry
            for entry in messages(listed)
            if entry["user"] == user
            and entry["ts"]
            and float(entry["ts"]) > dm["after"]
            and not self.store.one(
                "SELECT id FROM triggers WHERE key=? AND source='slack-sent'",
                (f"slack:{entry['ts']}",),
            )
        ]
        if not found:
            return "No reply from the owner there yet."
        return "The owner wrote there (oldest first):\n" + "\n".join(
            f"- {entry['text']}" for entry in sorted(found, key=lambda e: float(e["ts"]))
        )

    async def t_report_notified(self, params: ReportNotified, caller: str):
        # Only the messenger: what the connector writes (it reads others' messages) must never
        # become a command the messenger runs.
        self.check(caller, (MESSENGER,))
        pending = json.loads(self.store.get("away_pending") or "null")
        self.store.delete("away_pending")
        where = yours(params.where.strip())[:300]
        if params.ok and pending and pending.get("slack_dm"):
            where = OWN_SLACK_DM
        if params.ok:
            if params.method.strip():
                self.store.set("away_method", params.method.strip()[:1000])
            if where:
                if where != (self.store.get("away_where") or ""):
                    self.store.delete("away_reply_verified")  # Another way: not tried yet.
                self.store.set("away_where", where)
            replies = params.replies.strip()[:1000]
            if replies != (self.store.get("away_replies") or ""):
                self.store.delete("away_reply_verified")
            self.store.set("away_replies", replies)
            self.store.delete("away_retry_at")
            if pending and pending.get("test"):
                [item] = pending["items"]
                code = self.store.get(f"awayitem:{item}")
                self.store.set("away_test", json.dumps({"item": item, "code": code}))
            elif pending:
                sent = json.loads(self.store.get("away_sent") or "[]")
                self.store.set("away_sent", json.dumps((sent + pending["items"])[-200:]))
        else:
            self.store.set("away_retry_at", str(time.time() + RETRY_SECONDS))
        self.store.set(
            "away_last",
            json.dumps(
                {
                    "at": now(),
                    "ok": params.ok,
                    "where": where if params.ok else yours(params.where)[:300],
                    "error": params.error[:500],
                    "replies": bool(params.replies.strip()),
                }
            ),
        )
        failing = self.store.get("away_failing") == "1"
        test = bool(pending and pending.get("test"))
        if not (test and not params.ok):
            self.store.set("away_failing", "0" if params.ok else "1")
        if test and not params.ok:
            # The owner tried it themself just now: told each time, and not as a message that
            # missed them while they were away.
            self.runtime.add_message(
                self.runtime.router_id(),
                "notice",
                f"The test notification did not reach you: {params.error[:300]}. Change how in "
                "Settings → Connections, then send a test again.",
                {"settings": "connections"},
            )
        elif not params.ok and not failing:
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
        self.check(caller, (MESSENGER,))
        if not params.replies:
            raise ValueError(
                'No reply was given: pass replies=[{"code": "ABCDE", "text": "the owner\'s '
                'reply, exactly"}], one item per code.'
            )
        items = self.open_items()
        test = json.loads(self.store.get("away_test") or "null")
        done = []
        for reply in params.replies:
            code = reply.code.strip().upper()
            item_id = self.code_item(code) if CODE.fullmatch(code) else ""
            if test and code == test["code"]:
                self.store.delete("away_test")
                self.store.delete(f"awaycode:{code}")
                done.append(f"{code}: your answer came back")
                outcome = "your answers come back: replies here work"
                self.verified_replies()
            elif not (item := items.get(item_id)):
                outcome = self.what_became(item_id) or (
                    "nothing waits for that code (answered already?)"
                )
                done.append(f"{code}: {outcome}")
                if self.store.get(f"awayused:{code}") or self.store.get(f"awaynoted:{code}"):
                    continue  # Applied before (passed again), or said once already.
                self.store.set(f"awaynoted:{code}", str(time.time()))
            else:
                self.verified_replies()  # A code it sent came back: replies are read.
                try:
                    outcome = await self.apply(item, reply.text.strip(), code)
                    self.store.delete(f"awaycode:{code}")  # Used once.
                    self.store.set(f"awayused:{code}", str(time.time()))
                except (ValueError, LookupError, RuntimeError) as exc:
                    outcome = f"not done: {exc}"
                    done.append(f"{code}: {outcome}")
                    # The same reply read again (each time it looks) is said once, each reply.
                    said = hashlib.sha256(reply.text.strip().encode()).hexdigest()[:16]
                    if self.store.get(f"awayfailed:{code}:{said}"):
                        continue
                    self.store.set(f"awayfailed:{code}:{said}", str(time.time()))
                else:
                    done.append(f"{code}: {outcome}")
            self.runtime.add_message(
                self.runtime.router_id(),
                "notice",
                f"You answered from {yours(self.store.get('away_where') or 'elsewhere')}: "
                f"[{code}] {without_code(reply.text, code)[:200]} → {outcome}",
                {"away_reply": True},
            )
        self.runtime.publish_state()
        return "\n".join(done) or "Nothing to apply."

    def what_became(self, item_id: str) -> str:
        """What became of a draft a code was given for, once it no longer waits for the owner
        (replaced by a newer one, withdrawn, sent); "" when the item is not such a draft."""
        if not item_id.startswith("draft:") or item_id.startswith("draft:merge:"):
            return ""
        try:
            draft = self.store.draft(item_id.removeprefix("draft:"))
        except LookupError:
            return ""
        outcome = self.runtime.drafts.owner_text(draft["outcome"] or "").strip()
        if draft["status"] == "pending":
            return ""
        if draft["status"] in {"sent", "sending", "uncertain"}:
            return "sent already" if draft["status"] == "sent" else "being sent: see Home"
        if draft["status"] == "failed":
            return f"its sending failed: {outcome}" if outcome else "its sending failed"
        newer = self.store.get(f"replacedby:{draft['id']}") or ""
        if newer or outcome.startswith("Replaced by"):
            try:
                newest = self.store.draft(newer) if newer else None
            except LookupError:
                newest = None
            said = ""
            if newest:
                text = " ".join(newest["text"].split())
                said = f": \u201c{text if len(text) <= 160 else text[:159] + chr(0x2026)}\u201d"
            told = self.store.get(f"awayitem:draft:{newer}") if newer else ""
            if told and self.code_item(told) != f"draft:{newer}":
                told = ""
            then = ""
            if newest and newest["status"] == "pending":
                then = " It waits for you on Home."
                if told:
                    then = f" Answer that one with its code {told}, or on Home."
            return f"that draft was replaced by a newer one{said}; nothing was sent.{then}"
        if outcome.startswith("Withdrawn"):
            return "withdrawn: " + outcome.removeprefix("Withdrawn").lstrip(":. ")
        if outcome.startswith("Redo:"):
            return "you asked for it again: " + outcome.removeprefix("Redo:").strip()
        return f"dropped already ({outcome})" if outcome else "dropped already"

    def verified_replies(self):
        self.store.set(
            "away_reply_verified",
            json.dumps({"at": now(), "where": self.store.get("away_where") or ""}),
        )

    def capabilities(self) -> dict:
        """What the way the owner is reached can do, as tried, not as claimed: messages went
        out (the messenger said so), replies are read (it says how), a reply came back (a code
        returned), and new requests by reply (no: codes answer what was sent)."""
        last = json.loads(self.store.get("away_last") or "{}")
        verified = json.loads(self.store.get("away_reply_verified") or "null")
        return {
            "sends": bool(last.get("ok")),
            "sent_at": last.get("at", "") if last.get("ok") else "",
            "reads_replies": bool(self.store.get("away_replies")),
            "replies_verified_at": (verified or {}).get("at", ""),
            "new_requests": False,
        }

    async def apply(self, item: dict, text: str, code: str) -> str:
        """The owner's answer to one card, from elsewhere: what follows its code (any case)."""
        answer = without_code(text, code)
        answer = answer.strip(" :,-\t\n")
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
                target = draft["target"]
                if kind == "github" and not (
                    target.get("in_reply_to") or target.get("reply_to_comment") or target.get("own")
                ):
                    verdict = str(target.get("verdict") or "comment")
                    if word in {"post", "post it", "publish"}:
                        word = "send"
                    elif verdict != "approve" and "approve" in re.findall(r"\w+", word):
                        # It would post as it is, not as an approval.
                        raise ValueError(
                            f"not posted: this review is {VERDICTS.get(verdict, verdict)}, not an "
                            'approval: answer "post" to post it as it is, or "no"'
                        )
                # "yes, send it", "send pls", "ok 👍", "好的 发吧": a send in a few words; "do not
                # send" is a no; "ok wait", "not yet, send later" are not a send.
                meant = send_answer(word)
                if meant == "unclear":
                    raise ValueError('not sent: answer "send" when it should go, or "no"')
                word = {"send": "send", "no": "no"}.get(meant, word)
                if word in SEND_WORDS:
                    await drafts.resolve(draft["id"], DraftDecision(action="send"))
                    return "sent" if kind != "reply" else "marked as sent"
                if word in REJECT_WORDS:
                    await drafts.resolve(
                        draft["id"], DraftDecision(action="reject", reason="From away")
                    )
                    return "dropped"
                if not answer:
                    raise ValueError('answer "send" or "no", or say what to change')
                # Anything else is what to change: it is drafted again that way.
                await drafts.resolve(draft["id"], DraftDecision(action="revise", reason=answer))
                return "to be drafted again as you say"
            if kind == "decision":
                options = draft["target"].get("options") or []
                if word.isdigit() and 1 <= int(word) <= len(options):
                    answer = options[int(word) - 1]["choice"]
                await drafts.resolve(draft["id"], DraftDecision(action="answer", text=answer))
                return f"decided: {answer[:100]}"
            if kind == "workspace":
                # As its card: a folder it offers, no project, or another folder by its path.
                options = draft["target"].get("options") or []
                path = answer.strip("`'\" \u201c\u201d")
                if word.isdigit() and 1 <= int(word) <= len(options):
                    choice = options[int(word) - 1]["path"]
                elif word in NO_PROJECT_WORDS:
                    choice = ""
                elif path.startswith(("/", "~")):
                    choice = path
                else:
                    raise ValueError(
                        ("answer with the number of a folder, " if options else "answer ")
                        + '"none" for no project, or the path of a folder'
                    )
                await drafts.resolve(draft["id"], DraftDecision(action="choose", choice=choice))
                return (
                    f"chose {choice}" if choice else "no project: it starts in a folder of its own"
                )
            await drafts.resolve(draft["id"], DraftDecision(action="answer", text=answer))
            return "answered"
        if question := item.get("question"):
            if not answer:
                raise ValueError("write your answer after the code")
            await self.runtime.task_actions.answer_question(question["id"], answer)
            return "sent to the worker"
        task = item["task"]
        if task["status"] == "failed":
            if word != "retry":
                raise ValueError('answer "retry"')
            await self.runtime.task_actions.retry(task["id"])
            return "retrying"
        if task["status"] == "waiting" and self.store.get(f"stopped:{task['id']}"):
            # Stopped mid-work: Go on (with a note, if they wrote one) or Cancel, as on Home.
            if word in CANCEL_WORDS:
                await self.runtime.task_actions.cancel(task["id"])
                return "cancelled"
            if word in GO_ON_WORDS:
                answer = "Go on where you stopped."
            elif not answer or word in REJECT_WORDS:
                raise ValueError('answer "go on" or "cancel"')
            await self.runtime.task_actions.follow_up(task["id"], answer)
            return "going on"
        if not answer:
            raise ValueError("write your answer after the code")
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
                "Say whether the notification reached the owner, where, how, and how to read "
                "their replies there.",
                ReportNotified,
                self.t_report_notified,
            ),
            (
                "report_owner_replies",
                "Pass the owner's answers to Chief of Staff's notifications (by code).",
                ReportOwnerReplies,
                self.t_report_owner_replies,
            ),
            (
                "send_owner_slack_dm",
                "Send the notification to the owner's own Slack DM, through Chief of Staff's "
                "Slack connection.",
                SendOwnerSlackDm,
                self.t_send_owner_slack_dm,
            ),
            (
                "read_owner_slack_dm",
                "Read the owner's replies in their own Slack DM, where send_owner_slack_dm sent.",
                ReadOwnerSlackDm,
                self.t_read_owner_slack_dm,
            ),
        ]


TRUST = UNTRUSTED  # What the connector hands on is untrusted content.
