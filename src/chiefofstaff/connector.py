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
from datetime import UTC, datetime
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
# A watch is read from where it was last covered in full (its watermark), however long ago, back
# to this many days: older than that, what was never covered (scans that kept coming back partial,
# or Chief of Staff was not running) is a coverage gap, told to the owner.
RECOVER_DAYS = 7
# A watch's window that this many scans in a row covered only in part is given up on: a coverage
# gap, told; the next scans read on from its end.
GAP_TRIES = 4
# A gap told to the owner that goes on growing is told again once it is this many times as long
# as when it was told, or once it passes one of these lengths (seconds: an hour, a day).
GAP_GROWN = 2
GAP_MARKS = (3600, 86400)
# Coverage gaps kept for Settings, the newest.
MAX_GAPS = 10
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
# The owner's replies are read for this long after the last notification: every few minutes
# in its first hour, then every half hour.
READ_REPLIES_HOURS = 24
READ_OFTEN_SECONDS = 3600
READ_LATER_SECONDS = 1800
# Where a notification sent with send_owner_slack_dm went, as the owner reads it.
OWN_SLACK_DM = "your own Slack DM"
# The threads of the notifications sent there are read too (the newest this many).
MAX_THREADS = 10
# The owner's own DM is read this many messages at a time: a full page whose oldest message is
# newer than what was read before leaves the rest unread (coverage limited).
DM_PAGE = 50
# New commands from where the owner is: none, by design. A reply answers what a notification
# asked, by its code; one that names no code is placed with a code that waits (or the owner is
# asked which), never taken as new work.
NO_NEW_COMMANDS = (
    "by design: a reply answers what a notification asked, by its code; new work starts in "
    "Chief of Staff"
)
# What the owner reads away (fixed words: a notification's, what became of a reply) in Chinese
# (simplified) for an owner who writes it; English otherwise. Written with ASCII punctuation:
# Chinese gets its own (FULLWIDTH). Reply keywords ("send", "no") stay, with what they mean.
FULLWIDTH = str.maketrans(
    {":": "\uff1a", ",": "\uff0c", "(": "\uff08", ")": "\uff09", "?": "\uff1f"}
)
CHINESE = {
    english: chinese.translate(FULLWIDTH)
    for english, chinese in {
        "Chief of Staff needs you ({count})": "Chief of Staff 需要你({count})",
        "Reply: `{code} {hint}`": "回复:`{code} {hint}`",
        "Reply in this conversation with the code and your answer.": (
            "在这个对话里回复代码和你的答复。"
        ),
        "Answer in Chief of Staff on your computer: replies here are not read.": (
            "请在电脑上的 Chief of Staff 里答复:这里的回复不会被读取。"
        ),
        "Replies here cannot be read now: answer in Chief of Staff on your computer.": (
            "现在读不到这里的回复:请在电脑上的 Chief of Staff 里答复。"
        ),
        "{more} more waiting: open Chief of Staff. ": "还有 {more} 项在等你:打开 Chief of Staff。",
        "Not merged yet": "还没合并",
        "Not sent yet": "还没发送",
        "Not answered yet": "还没答复",
        "Test from Chief of Staff": "Chief of Staff 的测试",
        "This is how you hear from it when you are away.": "你不在电脑前时,会这样收到它的消息。",
        "Chief of Staff asks": "Chief of Staff 问你",
        "Urgent · Chief of Staff": "紧急 · Chief of Staff",
        "Review of {pr}": "{pr} 的评审",
        "Approve": "批准",
        "Request changes": "要求修改",
        "Comment": "评论",
        "Reply on {pr}": "在 {pr} 上回复",
        "Reply to {author} on {pr}": "在 {pr} 上回复 {author}",
        "Draft to approve": "待批准的草稿",
        "Decision": "决定",
        "Ready to merge · {label}": "可以合并 · {label}",
        "checks pass": "检查通过",
        "checks fail": "检查失败",
        "checks still running": "检查还在运行",
        "no checks": "没有检查",
        "Which folder for “{title}”": "“{title}”用哪个文件夹",
        "Folders: ": "文件夹:",
        "none: no project, or a folder's path": "none:不用项目,或写文件夹路径",
        "Question": "问题",
        "Needs your input": "需要你的意见",
        "Failed": "失败",
        "Stopped": "已停止",
        "Task: {title}": "任务:{title}",
        "About: {about}": "关于:{about}",
        "Action: Open {place}": "操作:打开 {place}",
        "Action: Open Chief of Staff → {place}": "操作:打开 Chief of Staff → {place}",
        "the chat with the Router": "与 Router 的对话",
        "a note from the Router": "Router 的提醒",
        "a question from the Router": "Router 的问题",
        "a question from {who}": "{who} 的问题",
        "your reply to {to}": "你给 {to} 的回复",
        "your reply": "你的回复",
        "your review of {pr}": "你对 {pr} 的评审",
        "your reply on {pr}": "你在 {pr} 上的回复",
        "<your answer>": "<你的答复>",
        "<number or your answer>": "<编号或你的答复>",
        "<number, none or a path>": "<编号、none 或文件夹路径>",
        "<none or a path>": "<none 或文件夹路径>",
        "<words for the blanks> · no": "<空白处要填的话> · no",
        "Which one? Reply with its code: {codes}": "是哪一项?请带上它的代码回复:{codes}",
        "{code} was already sent at {clock}: this reply changes nothing.": (
            "{code} 已在 {clock} 发出:这条回复不会改变什么。"
        ),
        "{code} was answered already at {clock}: this reply changes nothing.": (
            "{code} 已在 {clock} 答复过:这条回复不会改变什么。"
        ),
        "{code} was answered already at {clock} ({became}): this reply changes nothing.": (
            "{code} 已在 {clock} 答复过({became}):这条回复不会改变什么。"
        ),
        "not done: {reason}": "未完成:{reason}",
        "not done: you answered this code already": "未完成:你已经答复过这个代码",
        "not done: you answered this code already ({became})": (
            "未完成:你已经答复过这个代码({became})"
        ),
        "nothing waits for that code (answered already?)": "这个代码没有在等的事(已经答复过了?)",
        "your answers come back: replies here work": "你的答复能传回来:在这里回复有效",
        "write your answer after the code": "请在代码后写你的答复",
        'not merged: answer "merge" when it should be merged, or "no"': (
            '未合并:要合并时回复 "merge",不合并回复 "no"'
        ),
        'not merged: “{held}” sounds like not now: answer "merge" when it should be merged, '
        'or "no"': '未合并:“{held}”听起来是现在先不合并:要合并时回复 "merge",不合并回复 "no"',
        'not posted: this review is {verdict}, not an approval: answer "post" to post it as it '
        'is, or "no"': '未发布:这条评审是“{verdict}”,不是批准:回复 "post" 按原样发布,或回复 "no"',
        'not sent: “{held}” sounds like not now, or not as it is: answer "send" when it should '
        'go, or "no"': (
            '未发送:“{held}”听起来是现在先不发,或不按原样发:要发送时回复 "send",不发回复 "no"'
        ),
        "not sent: the draft still has a blank to fill (“{blank}”): say the words to use "
        "instead, or edit it on Home": (
            "未发送:草稿里还有要填的空白(“{blank}”):说出要填的话,或在 Home 上编辑"
        ),
        'not sent: say the words for its blanks ({blanks}), or "no"': (
            '未发送:说出空白处要填的话({blanks}),或回复 "no"'
        ),
        "not sent: “{held}” sounds like not now, or not as it is: say the words for its blanks "
        '({blanks}), or "no"': (
            "未发送:“{held}”听起来是现在先不发,或不按原样发:"
            '说出空白处要填的话({blanks}),或回复 "no"'
        ),
        'not sent: answer "send" when it should go, or "no"': (
            '未发送:要发送时回复 "send",不发回复 "no"'
        ),
        'answer with the number of a folder, "none" for no project, or the path of a folder': (
            '回复文件夹的编号,不用项目回复 "none",或写文件夹的路径'
        ),
        'answer "none" for no project, or the path of a folder': (
            '不用项目回复 "none",或写文件夹的路径'
        ),
        'answer "retry"': '回复 "retry"(重试)',
        'answer "go on" or "cancel"': '回复 "go on"(继续)或 "cancel"(取消)',
        "sent to the Router": "已转给 Router",
        "sent to the worker": "已转给 worker",
        "merged": "已合并",
        "left as it is": "保持原样",
        "sent": "已发送",
        "marked as sent": "已标记为已发送",
        "dropped": "已放弃",
        "to be drafted again as you say": "会按你说的重新起草",
        "decided: {answer}": "已决定:{answer}",
        "chose {choice}": "已选择 {choice}",
        "no project: it starts in a folder of its own": "不用项目:它在自己的文件夹里开始",
        "answered": "已答复",
        "retrying": "正在重试",
        "cancelled": "已取消",
        "going on": "继续进行",
        "sent already": "已经发出",
        "being sent: see Home": "正在发送:见 Home",
        "its sending failed": "发送失败",
        "its sending failed: {outcome}": "发送失败:{outcome}",
        "withdrawn: {reason}": "已撤回:{reason}",
        "you asked for it again: {reason}": "你要求重写:{reason}",
        "dropped already": "已经放弃",
        "dropped already ({outcome})": "已经放弃({outcome})",
    }.items()
}
# What a reply keyword means, shown after it for an owner who writes Chinese.
GLOSS = {
    "send · no": "发送 · 不发",
    "post · no": "发布 · 不发",
    "approve · no": "批准 · 不发",
    "merge · no": "合并 · 不合并",
    "<words for the blanks> · no": "no 即不发",
    "retry": "重试",
    "go on · cancel": "继续 · 取消",
    "ok": "好",
}
# Words a notification's reason starts with that its "Not sent yet" heading says already.
NOT_DONE = re.compile(r"^(?:not (?:sent|merged|posted)|未发送|未合并|未发布)\s*[:\uff1a]\s*")


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
REJECT_WORDS = {"reject", "no", "n", "skip", "don't", "dont", "不", "不要", "拒绝"}
GO_ON_WORDS = {"go on", "go", "continue", "resume", "继续"}
# What a reply to a draft means is the messenger's judgment (report_owner_replies' intent), not
# a guess from its words. These only ever stop a send: a reply the messenger read as "send"
# that asks something back, holds it back, makes it conditional or says no is not sent (the
# owner is asked to answer "send" when it should go). Matching too much only asks once more.
HOLDS_SEND = re.compile(
    r"[?\uff1f]"
    r"|\b(?:wait|hold|later|yet|until|till|tomorrow|tmrw|tmr|tonight|eod|eow|before|after|"
    r"once|when|if|unless|not|don'?t|do not|never|no|nope|nah|stop|first|instead|but|except|"
    r"hang on|one sec|never ?mind|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"next|this week|morning|afternoon|evening|noon|night|"
    r"in (?:an?|\d+) (?:hour|hours|hr|hrs|min|mins|minutes|days?)|"
    r"at \d{1,2}(?::\d\d)?|\d{1,2} ?(?:am|pm)|jk|kidding|joking|joke|sarcasm|/s|lol|lmao)\b"
    r"|n't\b"
    r"|[\u5417\u4e48\u5462\u561b\u4e0d\u522b\u7b49\u6682\u6162\u518d\u4f46]"
    r"|\u5148\u522b|\u5148\u4e0d|\u5148\u7b49|\u5148\u653e|\u7a0d\u540e|\u665a\u70b9|\u4e00\u4f1a|"
    r"\u5f85\u4f1a|\u56de\u5934|\u4e4b\u540e|\u4ee5\u540e|\u660e\u5929|\u540e\u5929|\u4eca\u665a|"
    r"\u665a\u4e0a|\u4e0a\u5348|\u4e0b\u5348|\u4e2d\u5348|\u65e9\u4e0a|\u5468[\u4e00\u4e8c\u4e09"
    r"\u56db\u4e94\u516d\u65e5\u5929]|\u661f\u671f|\u793c\u62dc|\d+\s*\u70b9|\u5f00\u73a9\u7b11|"
    r"\u9017\u4f60|\u54c8\u54c8|\u8fc7\u4f1a|\u4e0b\u73ed|\u5f00\u5b8c\u4f1a|\u4f1a\u540e|"
    r"\u660e\u65e9|\u665a\u4e9b"
    r"|[\U0001f644\U0001f60f\U0001f612\U0001f928]",
    re.IGNORECASE,
)
# Approval in words that hold a "no" or a "not" ("no changes needed", "not bad", and their
# Chinese counterparts): not a hold.
NOT_HOLDS = re.compile(
    r"\b(?:no (?:changes?|edits?|notes?|problem|problems|issues?|comments?|worries)"
    r"(?: (?:needed|required|from me))?|nothing (?:to|else to) (?:change|add|fix)|not bad)\b"
    r"|\u4e0d\u9519|\u6ca1\u95ee\u9898|\u6ca1\u4ec0\u4e48\u95ee\u9898|\u4e0d\u7528\u6539|"
    r"\u65e0\u9700\u4fee\u6539|\u4e0d\u9700\u8981\u6539",
    re.IGNORECASE,
)
# A draft that still has blanks to fill ("[yes / no]", "___"): never sent as it is.
BLANKS = re.compile(r"_{3,}|\[[^\[\]\n]{0,80}/[^\[\]\n]{0,80}\](?!\()")
# Any code in a reply (several in one message: "UELNW ship it, B4XJT no").
ANY_CODE = re.compile(r"\[?\b([A-Za-z2-7]{5})\b\]?")


# What the messenger may say a reply to a draft (or a merge card) means.
DRAFT_INTENTS = {"send", "no", "change", "unclear"}
# A merge card's outcome once its PR was closed (TaskActions.offer_merge): reopened at the same
# commit, the PR's next card is a new item, told again.
PR_CLOSED = "The PR is closed."
# A reply judged when its code no longer answered anything (see t_report_owner_replies).
GONE = "nothing waited for it"
# A "no" that says nothing more: its reason is that it came from away.
BARE_NO = REJECT_WORDS | {"nope", "nah", "drop", "drop it", "no thanks", "not now"}


def blanks_of(text: str) -> str:
    """A draft's blanks to fill, as it has them (“[yes / no]”, “___”); "" when none."""
    found = list(dict.fromkeys(BLANKS.findall(text or "")))[:3]
    return ", ".join(f"\u201c{blank}\u201d" for blank in found)


# A link in a reply as Slack writes it (<url|its words>, <url>): its words count, not its address.
SLACK_LINK = re.compile(r"<(?:https?://|mailto:)[^<>|\s]*(?:\|([^<>]*))?>")
# Separators between a code and its answer ("ABCDE: x", "ABCDE - x", "x; ABCDE"), not the
# answer's own punctuation (".env", "Pasta please.").
AFTER_CODE = re.compile(r"^(?:[:,;.\-\u2013\u2014]+|\s+[:,;.\-\u2013\u2014]+(?=\s|$))?\s*")
BEFORE_CODE = re.compile(r"[\s:,;\-\u2013\u2014]+$")


def holds_send(text: str) -> str:
    """What in a reply stops a send ("" when nothing does): see HOLDS_SEND. Links count by their
    words only: "?tab=rows" or "/next/" in an address hold nothing."""
    text = SLACK_LINK.sub(lambda link: f" {link.group(1) or ''} ", text)
    text = re.sub(r"\[([^\]\n]*)\]\([^)\s]*\)", r"\1", text)
    text = threads.LINK.sub(without_link, text)
    found = HOLDS_SEND.search(NOT_HOLDS.sub(" ", text.replace("\u2019", "'")))
    return found.group(0) if found else ""


def own_part(text: str, code: str, codes: set[str]) -> str:
    """Of a message naming several codes, the part about `code`: from it to the next code
    ("UELNW ship it, B4XJT no": "ship it, " for UELNW). Naming `code` alone, the message
    without it, wherever it is ("Teal, please. 4IF37"). Without its code (the messenger left it
    out), the message up to the first other code."""
    found = [
        (match.start(), match.end(), match.group(1).upper())
        for match in ANY_CODE.finditer(text)
        if match.group(1).upper() in codes
    ]
    mine = [at for at in found if at[2] == code]
    if not mine:
        others = [at for at in found if at[2] != code]
        return text[: others[0][0]] if others else text
    if len(found) == 1:
        return without_code(text, code)
    after = [at for at in found if at[0] >= mine[0][1] and at[2] != code]
    return AFTER_CODE.sub("", text[mine[0][1] : after[0][0] if after else len(text)], count=1)


def answer_part(text: str, code: str, codes: set[str]) -> str:
    """own_part, without what separates it from the next code ("ship it, " is "ship it")."""
    return BEFORE_CODE.sub("", own_part(text, code, codes).strip())


def reply_key(code: str, text: str) -> str:
    """A reply as judged: the same words for the same code are judged once, with or without
    its code in them (the messenger passes what it reads each time it looks)."""
    words = " ".join(without_code(text, code).casefold().split()).strip(" .!,;:-")
    return hashlib.sha256(f"{code}\n{words}".encode()).hexdigest()[:16]


CANCEL_WORDS = {"cancel", "取消"}
RETRY_WORDS = {"retry", "重试"}
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


def epoch(when: str) -> float:
    """An ISO time in seconds (one without its zone is local); 0 when it cannot be read."""
    try:
        moment = datetime.fromisoformat(when.strip().replace("Z", "+00:00"))  # noqa: FURB162
    except (ValueError, TypeError, AttributeError):
        return 0
    return moment.timestamp()


def loose_words(text: str) -> str:
    """Words as compared: case, spacing and the punctuation around them aside."""
    return " ".join(text.lower().split()).strip(" :;,.!?-\u2013\u2014\u00b7|")


def watch_key(what: str) -> str:
    """A watch by what it watches (renumbering or reordering watches does not reset it)."""
    return hashlib.sha256(" ".join(what.lower().split()).encode()).hexdigest()[:16]


def clock_of(moment: float) -> str:
    """A time as the owner reads it: "3:00 PM", with its day when that is not today ("Nov 8,
    3:00 PM")."""
    at = datetime.fromtimestamp(moment).astimezone()
    hour = f"{at.hour % 12 or 12}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"
    if at.date() == datetime.now().astimezone().date():
        return hour
    return f"{at.strftime('%b')} {at.day}, {hour}"


def gap_text(gap: dict, still: bool = False) -> str:
    """A watch's coverage gap, for the owner: what could not be checked, when, and why
    (`still`: told before, it grew since)."""
    reason = str(gap.get("reason") or "").strip().rstrip(".")
    return (
        f"{'Still could' if still else 'Could'} not check \u201c{gap['watch']}\u201d between "
        f"{clock_of(gap['from'])} and {clock_of(gap['to'])}" + (f" ({reason})" if reason else "")
    )


def without_code(text: str, code: str) -> str:
    """An answer without its code (any case, with or without brackets) and the separators next
    to it: "URN2F drop them", "[urn2f]: drop them" and "drop them - URN2F" are all "drop them";
    "URN2F .env" is ".env"."""
    found = re.search(rf"\[?\b{re.escape(code)}\b\]?", text, flags=re.IGNORECASE)
    if not found:
        return text.strip()
    head = text[: found.start()]
    before = BEFORE_CODE.sub("", head)
    rest = text[found.end() :]
    after = AFTER_CODE.sub("", rest, count=1)
    # Words on both sides keep what separated them ("no 52YVY, too long": "no, too long").
    mark = rest[: len(rest) - len(after)].strip() or head[len(before) :].strip()
    if before.strip() and after and mark:
        return f"{before}{mark} {after}" if mark[0] in ",;:." else f"{before} {mark} {after}"
    return f"{before} {after}".strip()


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
    reaches_back_to: str = Field(
        default="",
        description="Only when the source cannot search as far back as the watch's time at all "
        "(it keeps or searches only its last days): the oldest time it can search (ISO). What "
        "is before it can never be checked. Empty when a later search could still cover it.",
    )


class ReportFound(NestedArguments):
    items: list[FoundItem] = Field(
        default_factory=list,
        max_length=30,
        description="Every new item that needs the owner (a question, a request, a mention "
        "expecting an answer). Empty when there is none. At most 30 a call: with more, call "
        "report_found again with the rest (each call adds to the same scan).",
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
    code: str = Field(
        description="The five-letter code the owner's reply names, or the one it answers when "
        'it names none (the job says which); "" when the job says so'
    )
    text: str = Field(description="The owner's reply, exactly")
    at: str = Field(
        default="",
        description="When the owner wrote it, as the conversation shows it (its timestamp)",
    )
    intent: Literal["send", "no", "change", "unclear", "answer"] = Field(
        default="answer",
        description="What the reply means. For a code that is a draft or a PR to merge: "
        "'send' (send it, post it, merge it now, as it is: 'ok', 'ship it', '发吧'), 'no' "
        "(drop it, leave it), 'change' (it says what to change in it, or gives the words for a "
        "draft's blanks: 'attending yes, size M'), or 'unclear' (anything else: a question "
        "back, a send held back or with a condition: 'after lunch', 'once Bob confirms', "
        "'wait'; or when you are not sure). For any other code: 'answer'",
    )


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


# An item of a notification says this much of what it is about: its first sentences that say
# something, whole (a first one longer than this is cut at a word, with an ellipsis).
GIST_CHARS = 200
# Where a sentence ends: after its stop (and a closing quote or bracket), at a space; a Chinese
# stop ends one at once.
SENTENCE_END = re.compile(
    "([.!?\u2026][\"')\\]\u201d\u2019]*)\\s+|([\u3002\uff01\uff1f][\"')\\]\u201d\u2019\u300d\u300f]*)"
)
# Lines that only label what follows them say nothing on their own: a gist that took the first
# lines read "Issue:", "There are two reasons:" or a heading, without what they introduce.
HEADING_LINE = re.compile(r"^\s*#{1,6}\s")
# A line that ends what it says (a stop, a colon or a semicolon); one that does not is wrapped,
# its sentence going on on the next line (unless a new paragraph or list item comes next).
STOPPED = re.compile(
    "[.!?:;\u2026\u3002\uff01\uff1f\uff1a\uff1b][\"')\\]\u201d\u2019\u300d\u300f]*$"
)
BOLD_LABEL = re.compile(r"(?:\*\*|__|\*|_)[^*_\n]{1,80}(?:\*\*|__|\*|_)\s*[:\uff1a]?")
LINE_MARK = re.compile(r"^\s*(?:>+|[-*\u2022+]|\d+[.)])\s+")
# A line ending with a colon this short (in words; two Chinese characters count as one) is a
# label ("There are two reasons:"); a longer one leads into what follows it, and a gist never
# ends on it.
LABEL_WORDS = 6


def label_only(line: str) -> bool:
    """Whether a line (its list marker taken off) only labels what follows it: a short one
    ending with a colon, or a few words in bold with no stop ("**Options**")."""
    if not re.search(r"\w", line):
        return True
    bold = BOLD_LABEL.fullmatch(line)
    if bold:
        line = re.sub(r"[*_]", "", line).strip()
    size = len(line.split()) + len(re.findall(r"[\u3400-\u9fff]", line)) // 2
    if line.endswith((":", "\uff1a")):
        return size <= LABEL_WORDS
    return bool(bold) and size <= LABEL_WORDS and not line.endswith(tuple(".!?\u3002\uff01\uff1f"))


# A quote or bracket that opens (ASCII, curly, CJK), and what closes it. Straight quotes close
# themselves.
PAIRS = {
    "\u201c": "\u201d", "\u2018": "\u2019", "\u300c": "\u300d", "\u300e": "\u300f",
    "(": ")", "\uff08": "\uff09", "[": "]", "\u3010": "\u3011",
}  # fmt: skip
CLOSERS = set(PAIRS.values())


def unclosed(text: str) -> str:
    """What would close the quotes and brackets `text` leaves open, innermost first ("" when
    none is): a sentence that ends inside a quote is not where the quote ends. A straight
    quote opens at a word's start and closes at its end; one inside a word ("don't", 5") or a
    closing mark with nothing open (a list's "1)") is no quote."""
    stack: list[str] = []
    for index, char in enumerate(text):
        before = text[index - 1] if index else " "
        after = text[index + 1] if index + 1 < len(text) else " "
        if char in PAIRS:
            stack.append(PAIRS[char])
        elif char in "\"'\u2019" and before.isalnum() and after.isalnum():
            continue  # An apostrophe: "don't", "it\u2019s".
        elif char in stack and (char in CLOSERS or not after.isalnum()):
            del stack[len(stack) - 1 - stack[::-1].index(char) :]
        elif char in "\"'" and not before.isalnum() and after.strip():
            stack.append(char)
    return "".join(reversed(stack))


def shortened(said: str, ends: list[int], limit: int) -> str:
    """`said` in at most `limit` characters: up to the last sentence end (`ends`) that fits,
    not on a colon nor inside a quote or brackets; a first sentence longer than that is cut at
    a word, with an ellipsis (and the quotes and brackets it leaves open closed)."""
    if len(said) <= limit:
        return said
    fits = [
        end
        for end in ends
        if end <= limit
        and not said[:end].rstrip().endswith((":", "\uff1a"))
        and not unclosed(said[:end])
    ]
    # Not a few words when most of what fits is the next sentence: that one, cut.
    if fits and max(fits) >= limit // 2:
        return said[: max(fits)].rstrip(" ;")
    room = limit - 1
    while True:
        cut = said[:room]
        if " " in cut.strip():
            cut = cut.rsplit(" ", 1)[0]
        cut = cut.rstrip(" ;,:\uff1a\uff0c")
        closing = unclosed(cut)
        if len(cut) + 1 + len(closing) <= limit or room <= 1:
            return cut + "\u2026" + closing
        room -= len(cut) + 1 + len(closing) - limit


def gist(text: str, limit: int = GIST_CHARS, ask: bool = False) -> str:
    """What a text says, in short, as Slack text (mrkdwn): one item of a notification. Its first
    sentences that say something (labels and headings left out), whole, up to `limit`. `ask`:
    it asks the owner something, and the question it ends with is kept even when the sentences
    before it are long. Its Markdown becomes Slack's (*bold*, <link|text>); &, < and > are
    escaped."""
    plain = re.sub(r"```.*?(```|$)", " ", text or "", flags=re.DOTALL)
    said, spans = "", []  # The sentences, by where each starts and ends in `said`.
    loose = False  # A new paragraph, list item or section: not the line before, wrapped.
    opened = None  # Where a sentence the last line did not end starts.
    for raw in plain.splitlines():
        if not raw.strip() or HEADING_LINE.match(raw):
            loose = True
            continue
        listed = bool(LINE_MARK.match(raw))
        line = LINE_MARK.sub("", raw).strip()
        if re.fullmatch(r"[|:\- ]+", line) or label_only(line):
            loose = True
            continue
        joiner = ""
        if said:
            ended = said.endswith((".", "!", "?", "\u2026", ":", ";"))
            joiner = "; " if (loose or listed) and not ended else " "
            if said.endswith(tuple("\u3002\uff01\uff1f\uff1a\uff1b\uff0c")):
                joiner = ""  # Chinese punctuation has its own space.
        if opened is not None and (loose or listed):
            spans.append((opened, len(said)))  # What the last line left open ends with it.
            opened = None
        line_start = len(said) + len(joiner)
        start = line_start if opened is None else opened
        said += joiner + line
        for found in SENTENCE_END.finditer(line):
            spans.append((start, line_start + found.end(found.lastindex or 0)))
            start = line_start + found.end()
        opened = None
        if start < len(said):
            if STOPPED.search(line):
                spans.append((start, len(said)))
            else:
                opened = start  # Wrapped: its sentence goes on on the next line.
        loose = False
    if opened is not None:
        spans.append((opened, len(said)))
    if not said:
        # Nothing but labels: the first of them, rather than nothing.
        first = next((line for line in plain.splitlines() if line.strip()), "")
        said = re.sub(r"^\s*#{1,6}\s+", "", LINE_MARK.sub("", first)).strip()
        spans = [(0, len(said))]
    ends = [end for _start, end in spans]
    short = shortened(said, ends, limit)
    if ask and len(short) < len(said):
        # The question it asks, when the sentences that fit end before it.
        question = next(
            (
                said[start:end]
                for start, end in reversed(spans)
                if end > len(short) and QUESTION_END.search(said[start:end])
            ),
            "",
        )
        if question:
            room = limit - len(question) - 3
            head = shortened(said, ends, room) if room >= 40 else ""
            short = f"{head} \u2026 {question}" if head else shortened(question, [], limit)
    said = mrkdwn(short)
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
        # What the owner wrote in their own Slack DM, as Chief of Staff last read it itself (not
        # as the messenger copied it): which code a reply names (see place_codeless).
        self.own_dm_read: list[dict] = []

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
            self.retell_gaps()
            self.store.set("watch_scanned_at", str(time.time()))  # When the next one is due.
            self.store.set("watch_started_at", str(time.time()))
            # Each watch from where it was last covered in full (a little before: what came in
            # while that scan ran), not from the last scan started: one that failed, or could
            # see only part of its source, leaves its window to the next, however long ago.
            keys = [watch_key(w.what) for w in watches]
            for watch in watches:
                key = watch_key(watch.what)
                started = self.store.get(f"watchdone:{key}") or self.store.get(f"watchfrom:{key}")
                if not started:
                    # A watch never covered on its own (new, or its words edited): from a day
                    # before its first scan, or the last scan of all watches if later; not from
                    # one long ago that other, partly covered watches hold back. One an earlier
                    # version scanned goes on from where that version would have (what is
                    # older than scans reach back is a gap, see reach_back).
                    since = self.kept_before(key) or max(
                        float(self.store.get("watch_done_at") or 0),
                        time.time() - LOOKBACK_HOURS * 3600,
                    )
                    self.store.set(f"watchfrom:{key}", str(since))
                self.reach_back(watch.what)
            self.store.set("watch_scan", json.dumps(keys))
            # Where each was covered to before it: a later report of this scan that finds one
            # partial puts it back there, even after an earlier report took it for covered. And
            # how many scans in a row had covered each only in part: this scan counts once, from
            # there, whichever of its reports (one may leave a watch out, another list it).
            self.store.set(
                "watch_scan_before",
                json.dumps(
                    {
                        "done_at": self.store.get("watch_done_at"),
                        "done": {key: self.store.get(f"watchdone:{key}") for key in keys},
                        "what": {watch_key(w.what): w.what for w in watches},
                        "tries": {
                            key: json.loads(self.store.get(f"watchpartial:{key}") or "{}").get(
                                "tries", 0
                            )
                            for key in keys
                        },
                    }
                ),
            )
            lines = ["Scan now. The owner's watches, each with the time to look from:"]
            oldest = time.time()
            for number, watch in enumerate(watches, 1):
                since = self.watch_since(watch.what)
                oldest = min(oldest, since or time.time() - LOOKBACK_HOURS * 3600)
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
                partly = json.loads(self.store.get(f"watchpartial:{keys[number - 1]}") or "{}")
                if tries := partly.get("tries"):
                    lines[-1] += (
                        f"\n   Not covered in full since then ({tries} scan{'s' * (tries > 1)} "
                        f"in a row: {partly.get('reason', '')}): search all of it again, from "
                        "that time."
                    )
            day = datetime.fromtimestamp(oldest - 86400).astimezone().date().isoformat()
            lines.append(
                "Cover each watch's whole window, from its time: search with the source's own "
                f"date filter (in a Slack search, after:{day} is the day before the oldest time "
                "above), not only its latest results, and page through all of them (the next "
                "page, cursor or offset) until there are no more. When a tool gives only the "
                "newest few conversations or results and cannot page further, or cannot search "
                "back that far, that watch is incomplete: list it in incomplete, with what "
                "limited it (and reaches_back_to when the source cannot search back to its time "
                "at all). report_found takes 30 items a call: with more, call it again with the "
                "rest (every call adds to this scan)."
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

    def restore(self, key: str, value: str | None):
        """A cursor back where it was (`value`; none when that is empty)."""
        if value:
            self.store.set(key, value)
        else:
            self.store.delete(key)

    def watermark(self, key: str) -> float:
        """Where a watch (by watch_key) was covered to: all before it was covered in full, or is
        a coverage gap the owner was told of; where its first scan started from, until one
        covers it; 0 before its first scan."""
        return float(
            self.store.get(f"watchdone:{key}")
            or self.store.get(f"watchfrom:{key}")
            or self.store.get("watch_done_at")
            or 0
        )

    def kept_before(self, key: str) -> float:
        """Where a version before 0.1.36 left a watch it scanned and never covered in full
        (one in its last scan, or covered in part, with no place of its own; this version
        gives each watch its place as a scan starts): its last scan that covered every watch
        (watch_done_at), what that version read such a watch on from. 0 for one it never
        scanned, or when it kept no such scan."""
        known = key in json.loads(self.store.get("watch_scan") or "[]") or bool(
            self.store.get(f"watchpartial:{key}")
        )
        return float(self.store.get("watch_done_at") or 0) if known else 0.0

    def watch_since(self, what: str) -> float:
        """Where a watch's next scan starts: a little before its watermark, however long ago
        that is (a window that scans kept covering only in part is searched again, not left
        behind), back to RECOVER_DAYS (see reach_back); 0 for a new one."""
        done = self.watermark(watch_key(what))
        return max(done - OVERLAP_SECONDS, time.time() - RECOVER_DAYS * 86400) if done else 0

    def reach_back(self, what: str):
        """A watch not covered in full for longer than scans reach back (RECOVER_DAYS): what is
        older than that is a coverage gap, told; scans read on from there."""
        key = watch_key(what)
        done, horizon = self.watermark(key), time.time() - RECOVER_DAYS * 86400
        if done and done < horizon:
            partly = json.loads(self.store.get(f"watchpartial:{key}") or "{}")
            why = partly.get("reason") or "no scan covered it in full"
            self.gap(what, done, horizon, f"{why}; scans reach back only {RECOVER_DAYS} days")
            self.store.set(f"watchdone:{key}", str(horizon))

    def gap(self, what: str, start: float, end: float, reason: str):
        """A stretch of time a watch could not check, and no scan will: kept (Settings shows
        it) and told to the owner. One that goes on growing (a source that keeps covering only
        part of it) is told again once it grew a lot since it was told (see GAP_GROWN), with
        all of it, never for each scan that adds a little."""
        if end - start < 60:
            return
        key = watch_key(what)
        gaps = json.loads(self.store.get("watch_gaps") or "[]")
        last = next((gap for gap in reversed(gaps) if gap["key"] == key), None)
        if last and last["from"] <= start <= last["to"] + OVERLAP_SECONDS:
            # Kept by an older version, which told it once when it began, however long it grew
            # since (bug E50): told none of it as far as anyone knows (see retell_gaps).
            told = last.setdefault("told", 0)
            last.update({"to": max(last["to"], end), "reason": reason, "at": now()})
            span = last["to"] - last["from"]
            grown = span >= told * GAP_GROWN or any(told < mark <= span for mark in GAP_MARKS)
            if grown:
                last["told"] = span
            self.store.set("watch_gaps", json.dumps(gaps))
            if grown:
                self.tell_gap(last, still=True)
            return
        found = {"key": key, "watch": what, "from": start, "to": end, "reason": reason}
        found["told"] = end - start
        self.store.set("watch_gaps", json.dumps([*gaps, {**found, "at": now()}][-MAX_GAPS:]))
        self.tell_gap(found)

    def tell_gap(self, gap: dict, still: bool = False, *more: dict):
        said = "; ".join(gap_text(each) for each in more)
        self.runtime.add_message(
            self.runtime.router_id(),
            "notice",
            f"{gap_text(gap, still)}{f'; {said}' if said else ''}: anything that came in then "
            "may have been missed, so look there yourself. Settings → Connections → Watches lists "
            "what could not be checked.",
            {"settings": "connections"},
        )

    def retell_gaps(self):
        """Gaps an older version kept (bug E50): it told each once, when it began, and not as
        it grew (one told as 15 minutes was days long by the update). Told again once, all of
        each, in one notice; from then on as GAP_GROWN has it."""
        gaps = json.loads(self.store.get("watch_gaps") or "[]")
        older = [gap for gap in gaps if "told" not in gap]
        if not older:
            return
        for gap in older:
            gap["told"] = gap["to"] - gap["from"]
        self.store.set("watch_gaps", json.dumps(gaps))
        self.tell_gap(older[0], False, *older[1:])

    async def t_report_found(self, params: ReportFound, caller: str):
        self.check(caller)
        started = self.store.get("watch_started_at") or str(time.time())
        scanned = json.loads(self.store.get("watch_scan") or "[]")
        # A watch it could not run (problems) and said nothing more about: not covered.
        partial = {item.watch: item.reason.strip()[:300] for item in params.incomplete}
        if params.problems.strip() and not partial:
            partial = dict.fromkeys(range(1, len(scanned) + 1), params.problems.strip()[:300])
        reaches = {item.watch: epoch(item.reaches_back_to) for item in params.incomplete}
        before = json.loads(self.store.get("watch_scan_before") or "{}")
        incomplete = []
        for number, key in enumerate(scanned, 1):
            # Partial in an earlier report of this same scan (items come in several reports):
            # still partial, whatever a later one leaves out.
            record = json.loads(self.store.get(f"watchpartial:{key}") or "null") or {}
            earlier = record if record.get("scan") == started else {}
            if number in partial or earlier:
                reason = partial.get(number) or earlier["reason"]
                # Its window stays open: the next scan looks from where it was last covered.
                if key in (before.get("done") or {}):
                    self.restore(f"watchdone:{key}", before["done"][key])
                # Scans in a row that covered it only in part, this one counted once however
                # many reports it makes (from before it: an earlier report that left it out took
                # it for covered).
                counted = before.get("tries")
                if counted is not None:
                    tries = counted.get(key, 0) + 1
                else:
                    tries = earlier.get("tries", 1) if earlier else record.get("tries", 0) + 1
                tries = self.give_up(key, before, reason, reaches.get(number, 0), tries, started)
                self.store.set(
                    f"watchpartial:{key}",
                    json.dumps({"reason": reason, "at": now(), "scan": started, "tries": tries}),
                )
                incomplete.append({"watch": number, "reason": reason})
            else:
                self.store.set(f"watchdone:{key}", started)
                self.store.delete(f"watchpartial:{key}")
        if not incomplete:
            self.store.set("watch_done_at", started)
        elif "done_at" in before:
            self.restore("watch_done_at", before["done_at"])
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

    def give_up(
        self, key: str, before: dict, reason: str, reach: float, tries: int, started: str
    ) -> int:
        """What of a watch's window a scan covered only in part that no later scan can cover:
        what is older than its source can search (`reach`); all of it once GAP_TRIES scans in
        a row (`tries`) covered it only in part. That is a coverage gap (see gap), and the
        watch's watermark moves past it, for this scan's later reports too. The scans in a row
        that covered what is left only in part."""
        what = (before.get("what") or {}).get(key) or next(
            (w.what for w in self.store.settings().watches if watch_key(w.what) == key), "a watch"
        )
        end = float(started)
        done = self.watermark(key) or end - LOOKBACK_HOURS * 3600
        moved = 0.0
        if reach and done < reach:
            moved = min(reach, end)
            self.gap(what, done, moved, f"{reason}; the source searches back only to then")
        if tries >= GAP_TRIES:
            self.gap(
                what,
                moved or done,
                end,
                f"{reason}; {tries} scans in a row covered it only in part",
            )
            moved, tries = end, 0
        if moved:
            self.store.set(f"watchdone:{key}", str(moved))
            before.setdefault("done", {})[key] = str(moved)
            self.store.set("watch_scan_before", json.dumps(before))
        return tries

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
            # What watches could not check, and no scan will: the newest first, for a week after
            # each was told (or grew), however long before that it was.
            "gaps": [
                {
                    "watch": gap["watch"],
                    "from": datetime.fromtimestamp(gap["from"], UTC).isoformat(),
                    "to": datetime.fromtimestamp(gap["to"], UTC).isoformat(),
                    "reason": gap.get("reason", ""),
                    "text": gap_text(gap),
                }
                for gap in reversed(json.loads(self.store.get("watch_gaps") or "[]"))
                if time.time() - epoch(gap.get("at") or "") < RECOVER_DAYS * 86400
            ],
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
                # A PR offered again at the same commit is the same item: told once, one code
                # (unless it was closed and reopened since: merge_key).
                key = self.merge_key(draft)
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

    def merge_key(self, draft: dict) -> str:
        """A merge card as an item: its PR at its commit, and how many times the PR was closed
        at that commit before it (reopened, its card is told again, with a code of its own)."""
        label, head = draft["target"]["label"].lower(), draft["target"]["head"]
        closed = self.store.one(
            "SELECT COUNT(*) AS closed FROM drafts WHERE kind='merge' AND outcome=? "
            "AND created_at<? AND lower(json_extract(target, '$.label'))=? "
            "AND json_extract(target, '$.head')=?",
            (PR_CLOSED, draft["created_at"], label, head),
        )
        count = (closed or {}).get("closed") or 0
        return f"merge:{label}@{head}" + (f"+{count}" if count else "")

    def merge_became(self, key: str) -> str:
        """What became of a merge card a code was given for (by its merge_key), once it no
        longer waits: merged, or not and why; and the PR's newer card, if one waits."""
        label = key.removeprefix("merge:").rsplit("@", 1)[0]
        cards = [
            self.store.draft(row["id"])
            for row in self.store.all(
                "SELECT id FROM drafts WHERE kind='merge' "
                "AND lower(json_extract(target, '$.label'))=? ORDER BY created_at",
                (label,),
            )
        ]
        mine = [card for card in cards if self.merge_key(card) == key]
        if not mine or mine[-1]["status"] == "pending":
            return ""
        card = mine[-1]
        outcome = self.runtime.drafts.owner_text(card["outcome"] or "").strip().rstrip(".")
        if card["status"] == "sent":
            became = f"merged already: {outcome}" if outcome else "merged already"
        elif outcome == "Not now":
            became = "not merged: you chose not now"
        else:
            became = "not merged" + (f": {outcome[:1].lower()}{outcome[1:]}" if outcome else "")
        newer = [card for card in cards if card["status"] == "pending"]
        if not newer:
            return f"{became}."
        item = f"draft:{self.merge_key(newer[-1])}"
        told = self.store.get(f"awayitem:{item}") or ""
        then = "it waits for you on Home"
        if told and self.code_item(told) == item:
            then = f"answer it with its code {told}, or on Home"
        return f"{became}. It is ready to merge again: {then}."

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
        """The Router's messages that ask the owner something or need them (an urgent note
        too). Its answer to the owner's own question (a turn of theirs: its words there are not
        internal) asks nothing by its lists: they are the answer (what the team asked them)."""
        found, asked = [], False
        for m in sorted(messages, key=lambda m: m["id"]):
            if m["kind"] == "owner":
                asked = asks_owner(m["text"])
            if m["kind"] not in {"assistant", "update"} or m["meta"].get("internal"):
                continue
            answer = asked and m["kind"] == "assistant"
            if (
                m["meta"].get("needs_action")
                or m["meta"].get("urgent")
                or asks_owner(m["text"], answer=answer)
            ):
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
        if self.store.get(f"updateseen:{ask['id']}"):
            return True  # The owner said they saw it (it left Needs you).
        if asks_owner(ask["text"]) or (ask.get("meta") or {}).get("urgent"):
            # A question is answered by the owner, not by a card; an urgent note stays under
            # Needs you until they saw it.
            return False
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

    def used_codes(self) -> list[str]:
        """Codes the owner answered with, while their replies are still read."""
        found = []
        for code, value in self.store.items("awayused:").items():
            with suppress(ValueError, TypeError, KeyError):
                used = json.loads(value)
                at = used["at"] if isinstance(used, dict) else float(used)
                if time.time() - at < READ_REPLIES_HOURS * 3600:
                    found.append(code)
        return found

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
                self.say("Test from Chief of Staff"),
                self.say("This is how you hear from it when you are away."),
                "ok",
            )
        if ask := item.get("ask"):
            # An urgent note asks nothing: an answer to it still goes to the Router.
            urgent = (ask.get("meta") or {}).get("urgent") and not asks_owner(ask["text"])
            head = "Urgent · Chief of Staff" if urgent else "Chief of Staff asks"
            return self.say(head), self.gist(ask["text"], ask=not urgent), "<your answer>"
        if draft := item.get("draft"):
            target = draft["target"]
            kind = draft["kind"]
            reply = target.get("in_reply_to") or target.get("reply_to_comment")
            # A draft with blanks is answered with the words for them (sent as it is, never).
            fill = "<words for the blanks> · no" if blanks_of(draft["text"]) else ""
            if kind == "github" and not reply and not target.get("own"):
                # What posting it does, in words: "send" alone would approve a PR unsaid.
                verdict = str(target.get("verdict") or "comment")
                title = (target.get("pr") or {}).get("title") or ""
                pr = f"{str(target.get('repo', '')).split('/')[-1]}#{target.get('number', '')}"
                head = self.say("Review of {pr}", pr=pr) + (
                    f" \u201c{title}\u201d" if title else ""
                )
                head += f": {self.say(VERDICTS.get(verdict, 'Comment'))}"
                word = "approve" if verdict == "approve" else "post"
                return head, self.gist(draft["text"]), fill or f"{word} · no"
            if kind == "github":
                author = (target.get("comment") or {}).get("author") or ""
                pr = f"{str(target.get('repo', '')).split('/')[-1]}#{target.get('number', '')}"
                title = (target.get("pr") or {}).get("title") or ""
                head = (
                    self.say("Reply to {author} on {pr}", author=author, pr=pr)
                    if author
                    else self.say("Reply on {pr}", pr=pr)
                )
                head += f" \u201c{title}\u201d" if title and not reply else ""
                return head, self.gist(draft["text"]), fill or "send · no"
            if kind in {"slack", "reply"}:
                where = target.get("to") or target.get("where") or target.get("url") or ""
                head = self.say("Draft to approve") + (f" · {where}" if where else "")
                return head, self.gist(draft["text"]), fill or "send · no"
            if kind == "decision":
                options = target.get("options") or []
                # Its options whole (the owner answers with one's number), after its question.
                listed = " ".join(
                    f"{n}) {shortened(str(o.get('choice', '')), [], 80)}"
                    for n, o in enumerate(options, 1)
                )
                question = self.gist(target.get("question", draft["text"]), ask=True)
                text = f"{question} {mrkdwn(listed)}".strip()
                return self.say("Decision"), text, "<number or your answer>"
            if kind == "merge":
                head = self.say("Ready to merge · {label}", label=target["label"])
                checks = self.say(CHECK_WORDS.get(target.get("checks") or "", ""))
                said = target.get("title", "") + (f" ({checks})" if checks else "")
                return head, gist(said), "merge · no"
            if kind == "workspace":
                options = target.get("options") or []
                # Not at the start of the line: gist would take "1)" for a list's marker.
                listed = self.say("Folders: ") + " ".join(
                    f"{n}) {o['path']}" for n, o in enumerate(options, 1)
                )
                listed = " · ".join(
                    x
                    for x in (
                        listed if options else "",
                        self.say("none: no project, or a folder's path"),
                    )
                    if x
                )
                hint = "<number, none or a path>" if options else "<none or a path>"
                title = target.get("title", "")
                # Every folder it offers, whole: the owner answers with one's number.
                return (
                    self.say("Which folder for “{title}”", title=title),
                    gist(listed, limit=600),
                    hint,
                )
            return self.say("Question"), self.gist(draft["text"], ask=True), "<your answer>"
        if question := item.get("question"):
            return (
                self.say("Needs your input"),
                self.gist(question["text"], ask=True),
                "<your answer>",
            )
        task = item["task"]
        if task["status"] == "failed":
            return self.say("Failed"), self.gist(task.get("progress") or ""), "retry"
        if self.store.get(f"stopped:{task['id']}"):
            # Stopped mid-work: it asks nothing, it waits for Go on or Cancel.
            return self.say("Stopped"), self.gist(task.get("progress") or ""), "go on · cancel"
        said = self.gist(task.get("progress") or "", ask=True)
        return self.say("Needs your input"), said, "<your answer>"

    def subject(self, item: dict) -> tuple[str, str]:
        """Which task an item is (“Task: …”), or, when it is no task's, what it is about
        (“About: …”); and where it waits in Chief of Staff, to open there."""
        if item.get("test"):
            return "", ""
        title = self.task_title(item)
        name, about, place = title, "", "Needs you → {name}"
        if ask := item.get("ask"):
            if ask.get("kind") == "update":
                # A note of the Router's waits under Needs you, on its card ("The Router").
                about, name = self.say("a note from the Router"), "The Router"
            else:
                about, place = self.say("a question from the Router"), "the chat with the Router"
        elif draft := item.get("draft"):
            target = draft["target"]
            pr = f"{str(target.get('repo', '')).split('/')[-1]}#{target.get('number', '')}"
            to = target.get("to") or target.get("where") or ""
            if draft["kind"] in {"slack", "reply"}:
                about = self.say("your reply to {to}", to=to) if to else self.say("your reply")
            elif draft["kind"] == "github":
                review = not (
                    target.get("in_reply_to") or target.get("reply_to_comment") or target.get("own")
                )
                about = self.say("your review of {pr}" if review else "your reply on {pr}", pr=pr)
            elif draft["kind"] == "merge":
                pr_title = target.get("title") or ""
                about = target.get("label", "") + (f" \u201c{pr_title}\u201d" if pr_title else "")
            else:
                who = self.asker(draft.get("session_id") or "")
                about = self.say("a question from {who}", who=who)
        elif question := item.get("question"):
            about = self.say("a question from {who}", who=question["worker"])
        if title:
            line = self.say("Task: {title}", title=title)
        else:
            line = self.say("About: {about}", about=about) if about else ""
        return line, self.say(place, name=name or about)

    def task_title(self, item: dict) -> str:
        """The title of the task an item is (or the tasks, of a decision several wait on); ""
        when it is none's."""
        if task := item.get("task"):
            return task["title"]
        if question := item.get("question"):
            return question.get("task_title") or ""
        tasks = self.runtime.tasks
        if ask := item.get("ask"):
            found = tasks.find(str((ask.get("meta") or {}).get("task_id") or ""))
            return found["title"] if found else ""
        draft = item.get("draft") or {}
        target = draft.get("target") or {}
        if draft.get("kind") == "decision":
            titles = [asker.get("title") or "" for asker in target.get("askers") or []]
            return ", ".join(dict.fromkeys(title for title in titles if title))
        if draft.get("kind") == "workspace":
            return target.get("title") or ""
        found = tasks.find(str(target.get("task_id") or ""))
        if not found and draft.get("kind") == "question":
            found = tasks.current(draft.get("session_id") or "")
        return found["title"] if found else ""

    def asker(self, session_id: str) -> str:
        """Who asks, by its session: a worker by its name, else Chief of Staff."""
        row = self.store.find_session(session_id)
        if row and row["role"] == WORKER:
            return self.runtime.display_title(row)
        return "Chief of Staff"

    def gist(self, text: str, ask: bool = False) -> str:
        """gist, of a text sessions wrote: without draft ids or Slack timestamps."""
        return gist(self.runtime.drafts.owner_text(text), ask=ask)

    def chinese(self) -> bool:
        return self.store.get("owner_language") == "Chinese"

    def say(self, english: str, **fields: Any) -> str:
        """Fixed words the owner reads away, in their language (see CHINESE)."""
        text = CHINESE.get(english, english) if self.chinese() else english
        return text.format(**fields) if fields else text

    def reply_line(self, code: str, hint: str) -> str:
        """How to answer an item, in the owner's language: its reply keywords stay, with what
        they mean after them for an owner who writes Chinese (see GLOSS)."""
        line = self.say("Reply: `{code} {hint}`", code=code, hint=self.say(hint))
        gloss = GLOSS.get(hint, "") if self.chinese() else ""
        return line + (f"\uff08{gloss}\uff09" if gloss else "")

    def render(
        self, items: list[dict], more: int, codes: bool, notes: list[str] | None = None
    ) -> tuple[str, dict]:
        """A notification as Slack text (mrkdwn) and as Block Kit, with answer codes or
        without (then the owner answers in Chief of Staff). `notes`: lines for the owner that
        are no items (see tell_away)."""
        notes = notes or []
        if not codes:
            # Without codes nothing asks for a reply there: not a note asking which code a
            # reply answers.
            asking = set(self.asking_notes())
            notes = [note for note in notes if note not in asking]
        sections = []
        for item in items:
            what, said, hint = self.describe(item)
            if reason := self.store.get(f"awayreask:{item['id']}"):
                kind = (item.get("draft") or {}).get("kind")
                again = (
                    "Not merged yet"
                    if kind == "merge"
                    else "Not sent yet"
                    if self.decides_draft(item)
                    else "Not answered yet"
                )
                # Its heading says "not sent" already; the draft goes on its own line. Without
                # codes, not why (it says what to answer there).
                reason = NOT_DONE.sub("", reason.strip()).rstrip(".\u3002") if codes else ""
                reason = reason[:1].upper() + reason[1:]
                end = "\u3002" if self.chinese() else "."
                what = f"{self.say(again)} \u00b7 {what}"
                said = "\n".join(part for part in (reason + end if reason else "", said) if part)
            code = item.get("code", "")
            head = f"*[{code}]* {mrkdwn(what)}" if codes else f"*{mrkdwn(what)}*"
            # Which task it is, what it says, and where to answer it: in a notification that
            # reads no replies, there only (Chief of Staff, on the owner's computer).
            about, place = self.subject(item)
            action = ""
            if place:
                action = (
                    self.say("Action: Open {place}", place=place)
                    if codes
                    else self.say("Action: Open Chief of Staff → {place}", place=place)
                )
            lines = [head, mrkdwn(about), said, mrkdwn(action)]
            if codes:
                lines.append(self.reply_line(code, hint))
            sections.append("\n".join(line for line in lines if line))
        # Lines for the owner that are no items to answer (a reply answered already, say).
        sections = [mrkdwn(note) for note in notes] + sections
        title = (
            self.say("Chief of Staff needs you ({count})", count=len(items) + more)
            if items or more
            else "Chief of Staff"
        )
        footer = self.say(
            "Reply in this conversation with the code and your answer."
            if codes
            else "Answer in Chief of Staff on your computer: replies here are not read."
        )
        if more:
            footer = self.say("{more} more waiting: open Chief of Staff. ", more=more) + footer
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
            # Its run ended without a word: as good as failed; tried again later. Its codes stay
            # (it may have gone out with them: the owner's answers by them still count), and
            # the next try gives the same ones.
            self.store.delete("away_pending")
            self.store.set("away_retry_at", str(time.time() + RETRY_SECONDS))
            return None
        return pending

    def drop_issued(self, pending: dict):
        """The answer codes made for a notification that did not go out with them (it failed,
        or went as the version without codes, where replies are not read): none of them can
        come back, so none is kept. Codes the owner got before stay."""
        for code in pending.get("issued") or []:
            item_id = self.code_item(code)
            self.store.delete(f"awaycode:{code}")
            if item_id and self.store.get(f"awayitem:{item_id}") == code:
                self.store.delete(f"awayitem:{item_id}")

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
        fresh = [
            item
            for item in self.waiting_items()
            if item["id"] not in sent or self.store.get(f"awayreask:{item['id']}")
        ]
        if test:
            fresh = [{"id": f"test:{int(time.time())}", "test": True}]
        notes = [] if test else self.away_notes()
        if not fresh and not notes:
            return
        shown, more = fresh[:MAX_AWAY_ITEMS], max(0, len(fresh) - MAX_AWAY_ITEMS)
        # Replies where it goes could not be read when last tried (Send only, or Read replies
        # no): it asks for none and points back to Chief of Staff, and no answer code is made
        # for it (none could ever come back). A test tries reading them again.
        plain = not test and self.capabilities()["replies"]["state"] == "no"
        issued = []  # Codes made for this one: dropped if it goes out without them.
        for item in [] if plain else shown:
            known = self.store.get(f"awayitem:{item['id']}")
            item["code"] = self.code_for(item["id"])
            if item["code"] != known:
                issued.append(item["code"])
        files = await asyncio.to_thread(self.write_notification, shown, more, notes, not plain)
        self.store.set(
            "away_pending",
            json.dumps(
                {
                    "at": time.time(),
                    "items": [item["id"] for item in shown],
                    "test": test,
                    "files": files,
                    "notes": notes,
                    "plain": plain,
                    "issued": issued,
                }
            ),
        )
        settings = self.store.settings()
        how = settings.away_how.strip() or prompts.AWAY_DEFAULT
        lines = (
            [
                "Send the owner this message, as it is (only to the owner themself, nobody "
                "else). Their replies there could not be read when last tried, so it asks for "
                "none: send this version only (with send_owner_slack_dm, version B). It is "
                "written as Slack text and as Slack Block Kit (JSON with blocks and a fallback "
                "text): send the Block Kit one when your tool can, else the text.",
                f"B: {files['plain.txt']} or {files['plain.json']}",
            ]
            if plain
            else [
                "Send the owner one of these messages, as it is (only to the owner themself, "
                "nobody else). Version A has answer codes: send it when you can read the owner's "
                "replies in that same conversation later with your tools. Otherwise send version "
                "B. Each is written as Slack text and as Slack Block Kit (JSON with blocks and a "
                "fallback text): send the Block Kit one when your tool can, else the text.",
                f"A: {files['codes.txt']} or {files['codes.json']}",
                f"B: {files['plain.txt']} or {files['plain.json']}",
            ]
        )
        lines.append(f"How the owner wants to get it: {how}")
        if self.slack_dm():
            lines.append(SLACK_DM_HINT)
        if method := self.store.get("away_method"):
            lines.append(f"What you used before: {method}")
        if not plain and (replies := self.store.get("away_replies")):
            lines.append(f"How you read their replies before: {replies}")
        lines.append(
            "Then call report_notified: whether it arrived, where, and the exact method (replies "
            "empty: they are not read). Never post it anywhere else or as the owner to anyone "
            "else."
            if plain
            else "Then call report_notified: whether it arrived, where, the exact method, and how "
            "you can read their replies there (empty if you sent B). Never post it anywhere else "
            "or as the owner to anyone else."
        )
        await self.runtime.toolset.start_special(MESSENGER, MESSENGER_TITLE, "\n\n".join(lines))

    def write_notification(
        self, items: list[dict], more: int, notes: list[str] | None = None, codes: bool = True
    ) -> dict[str, str]:
        """The message in files of its own: with answer codes (unless `codes` is false: replies
        are not read where it goes) and without, as Slack text and as Block Kit (the last 20
        notifications are kept)."""
        folder = self.runtime.role_dir("away")
        stamp = int(time.time() * 1000)
        files = {}
        for name, coded in (("codes", True), ("plain", False)):
            if coded and not codes:
                continue
            text, blocks = self.render(items, more, coded, notes)
            path = folder / f"notification-{stamp}-{name}"
            path.with_suffix(".txt").write_text(text + "\n", encoding="utf-8")
            path.with_suffix(".json").write_text(json.dumps(blocks, ensure_ascii=False))
            files[f"{name}.txt"] = str(path.with_suffix(".txt"))
            files[f"{name}.json"] = str(path.with_suffix(".json"))
        for old in sorted(folder.glob("notification-*"))[:-80]:
            old.unlink(missing_ok=True)
        return files

    def reply_codes(self) -> list[str]:
        """The codes a reply of the owner's may name: open, told, and answered lately (a new
        reply to one is told what became of it: "wait, don't send that yet")."""
        codes = self.open_codes()
        codes += [code for code in self.told_codes() if code not in codes]
        return codes + [code for code in self.used_codes() if code not in codes]

    def followed_codes(self, written: float = 0) -> list[str]:
        """The codes a reply that names no code may answer: every code still open that was
        issued before it was written (`written`; all open ones when that is unknown). Not only
        the latest notification's: it lists new items only, and an earlier one's may be what
        the reply answers (code review: "yes, send it" to the first sent the second)."""
        found = []
        for code in self.open_codes():
            issued = json.loads(self.store.get(f"awaycode:{code}") or "null")
            if not (written and issued and written < issued["at"]):
                found.append(code)
        return found

    def away_notes(self) -> list[str]:
        """Lines for the owner waiting for the next notification (see tell_away)."""
        notes = json.loads(self.store.get("away_notes") or "[]")
        return [
            note["text"] for note in notes if time.time() - note["at"] < READ_REPLIES_HOURS * 3600
        ]

    def asking_notes(self) -> list[str]:
        """Those of the notes that ask the owner to reply there (which code a reply answers)."""
        notes = json.loads(self.store.get("away_notes") or "[]")
        return [note["text"] for note in notes if note.get("asks")]

    async def tell_away(self, text: str, asks: bool = False):
        """A line for the owner where they are, about a reply of theirs there (no item to
        answer): at once in their own Slack DM, else with the next notification. `asks`: it
        asks them to reply there (left out of a notification that reads no replies)."""
        if self.store.get("away_where") == OWN_SLACK_DM and self.slack_dm():
            try:
                user = await self.owner_slack_id()
                result = await self.runtime.slack.send({"channel_id": user}, text)
            except Exception as exc:
                logger.warning("Could not write to the owner's own Slack DM.", exc_info=True)
                self.tried("send", False, str(exc), OWN_SLACK_DM)
            else:
                self.tried("send", True, "", OWN_SLACK_DM)
                for key in message_keys(result):
                    self.store.add_trigger("slack-sent", key, text)  # Not a reply of theirs.
                return
        notes = json.loads(self.store.get("away_notes") or "[]")
        notes = [note for note in notes if note["text"] != text]
        note = {"text": text, "at": time.time(), **({"asks": True} if asks else {})}
        self.store.set("away_notes", json.dumps([*notes, note][-10:]))

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
        codes = self.reply_codes()
        if not codes:
            return
        read_at = float(self.store.get("away_read_at") or 0)
        # Each read by the messenger is a run (a fresh Copilot conversation): every few minutes
        # in the hour after a notification, then every half hour. The owner's own Slack DM is
        # read here first, without a run, every few minutes all day: a run once they wrote.
        every = max(1, settings.watch_minutes) * 60
        runs_every = every
        if time.time() - told > READ_OFTEN_SECONDS:
            runs_every = max(every, READ_LATER_SECONDS)
        own = "read_owner_slack_dm" in how
        if time.time() - read_at < (every if own else runs_every):
            return
        self.store.set("away_read_at", str(time.time()))
        if own:
            news = await self.owner_dm_news()
            if news is False:
                return  # Nothing new from them there: no run.
            ran = float(self.store.get("away_ran_at") or 0)
            if news is None and time.time() - ran < runs_every:
                return  # It could not be read here: the messenger looks, at its own pace.
        self.store.set("away_ran_at", str(time.time()))
        self.own_dm_read = []  # What this run reads there, not an earlier run.
        where = self.store.get("away_where") or "the conversation you sent Chief of Staff's "
        # Kept as the owner reads it ("your own Slack DM"); the messenger reads it as theirs.
        where = re.sub(r"\byour\b", "the owner's", where, flags=re.IGNORECASE)
        items = self.open_items()
        listed = []
        for code in codes:
            item = items.get(self.code_item(code)) or {}
            kind = (item.get("draft") or {}).get("kind")
            blanks = blanks_of(item["draft"]["text"]) if self.decides_draft(item) else ""
            listed.append(
                f"{code} (a PR to merge)"
                if kind == "merge"
                else f"{code} (a draft to send, with blanks to fill: {blanks}; words for them "
                "are intent 'change')"
                if blanks
                else f"{code} (a draft to send)"
                if self.decides_draft(item)
                else code
            )
        latest = self.followed_codes()
        if len(latest) == 1:
            codeless = (
                " A reply that names no code answers the only code that waits: pass one written "
                f"after it was sent with {latest[0]}."
            )
        elif latest:
            codeless = (
                " A reply that names no code: pass it with code \"\" and intent 'unclear' (codes "
                f"{', '.join(latest)} wait: the owner is asked which one it answers)."
            )
        else:
            codeless = " Leave out replies that name no code."
        text = (
            f"Read the owner's replies to Chief of Staff's notifications in {where} with: {how}\n\n"
            f"Pass each reply that names one of these codes with report_owner_replies (the code, "
            f"the reply exactly, when it was written, and what it means as intent): "
            f"{', '.join(listed)}.{codeless} Only what the owner themself wrote there (replies "
            "in a notification's thread too). Each reply is judged once, and a code's newest "
            "reply is the one that counts, so passing one again is harmless. If there is none, "
            "end without calling it. Send nothing."
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
        # With answer codes only while replies there can be read: not when the notification
        # asks for none (Send only), nor when reading them failed when last tried.
        replies = self.capabilities_of(OWN_SLACK_DM).get("replies") or {}
        codes = (
            params.version == "A"
            and not pending.get("plain")
            and "codes.txt" in pending["files"]
            and replies.get("ok") is not False
        )
        name = "codes.txt" if codes else "plain.txt"
        text = (await asyncio.to_thread(Path(pending["files"][name]).read_text, "utf-8")).strip()
        try:
            result = await self.runtime.slack.send({"channel_id": user}, text)
        except Exception as exc:
            self.tried("send", False, str(exc), OWN_SLACK_DM)
            raise
        self.tried("send", True, "", OWN_SLACK_DM)
        keys = message_keys(result)
        for key in keys:
            # Not a message to the owner for the Slack watcher, nor a reply of theirs.
            self.store.add_trigger("slack-sent", key, text)
        sent = max((float(key.removeprefix("slack:")) for key in keys), default=time.time())
        before = json.loads(self.store.get("away_slack_dm") or "null") or {}
        recent = time.time() - before.get("after", 0) < READ_REPLIES_HOURS * 3600
        # Each notification's own ts: replies in its thread are read too.
        stamps = [key.removeprefix("slack:") for key in keys]
        notified = [
            stamp
            for stamp in (before.get("threads") or [] if recent else [])
            if time.time() - float(stamp) < READ_REPLIES_HOURS * 3600
        ]
        if stamps:
            notified.append(max(stamps, key=float))
        # The DM itself (D…), as Slack linked the message; else the owner's id.
        channel = (PERMALINK_CHANNEL.findall(result) or [user])[0]
        self.store.set(
            "away_slack_dm",
            json.dumps(
                {
                    "channel": channel,
                    # Replies to every notification still answerable are read.
                    "after": min(before["after"], sent) if recent else sent,
                    "threads": notified[-MAX_THREADS:],
                }
            ),
        )
        readable = await self.read_back(channel, max(stamps, key=float) if stamps else "")
        if codes and not readable:
            # It asks for replies that cannot be read: said at once, where it went.
            note = self.say(
                "Replies here cannot be read now: answer in Chief of Staff on your computer."
            )
            with suppress(Exception):
                for key in message_keys(await self.runtime.slack.send({"channel_id": user}, note)):
                    self.store.add_trigger("slack-sent", key, note)
            codes = False
        replies = "read_owner_slack_dm" if codes else ""
        # Where it went, in the owner's words, whatever the messenger reports; and whether the
        # owner got its codes (kept then, even when their replies cannot be read now).
        coded = name == "codes.txt"
        self.store.set("away_pending", json.dumps({**pending, "slack_dm": True, "coded": coded}))
        return (
            "Sent to the owner's own Slack DM. Now call report_notified(ok=true, where=\"your "
            f'own Slack DM", method="send_owner_slack_dm", replies="{replies}").'
        )

    async def read_back(self, channel: str, stamp: str) -> bool:
        """Right after a notification went to the owner's own Slack DM, read there as their
        replies are read: whether it is there (Delivery), and whether the DM and its thread can
        be read (Read replies). False when replies there cannot be read."""
        mcp = self.runtime.slack.mcp
        try:
            listed = await mcp.call("slack_read_channel", {"channel_id": channel, "limit": DM_PAGE})
        except Exception as exc:
            logger.info("Could not read the owner's own Slack DM back: %s", exc)
            self.tried("delivery", False, f"could not read the DM back: {exc}", OWN_SLACK_DM)
            self.tried("replies", False, f"could not read the DM: {exc}", OWN_SLACK_DM)
            return False
        if not stamp:
            self.tried("delivery", False, "Slack gave no message id to look for", OWN_SLACK_DM)
            self.tried(
                "replies", False, "Slack gave no message id: its thread cannot be read",
                OWN_SLACK_DM,
            )  # fmt: skip
            return False
        there = stamp in {entry["ts"] for entry in messages(listed)}
        self.tried(
            "delivery", there, "" if there else "not found in the DM when read back", OWN_SLACK_DM
        )
        try:
            await mcp.call("slack_read_thread", {"channel_id": channel, "message_ts": stamp})
        except Exception as exc:
            logger.info("Could not read the notification's thread back: %s", exc)
            self.tried("replies", False, f"could not read its thread: {exc}", OWN_SLACK_DM)
            return False
        self.tried("replies", True, "", OWN_SLACK_DM)
        return True

    async def owner_dm_news(self) -> bool | None:
        """Whether the owner wrote in their own Slack DM since it was last looked at (read
        through Chief of Staff's own Slack connection: no messenger run); None when it cannot
        be read here, or only in part (the messenger looks, at its own pace)."""
        try:
            found, complete = await self.owner_dm_messages()
        except Exception:
            logger.warning("Could not read the owner's own Slack DM.", exc_info=True)
            return None
        newest = max((float(entry["ts"]) for entry in found), default=0.0)
        # Seen once the messenger read it (see t_read_owner_slack_dm): a run that failed
        # first leaves it for the next.
        seen = float(self.store.get("away_dm_seen") or 0)
        if complete:
            return newest > seen
        # Part of it could not be read: nothing counts as seen. At once only for what the
        # messenger has not looked at yet.
        looked = float(self.store.get("away_dm_looked") or 0)
        return True if newest > max(seen, looked) else None

    async def owner_dm_messages(self) -> tuple[list[dict], bool]:
        """The owner's own messages in their own Slack DM since a notification was sent
        there, and in the threads of the notifications, oldest first; and whether all of it
        was read (a thread that could not be read, a listing cut short: see away_coverage)."""
        dm = json.loads(self.store.get("away_slack_dm") or "null")
        if not dm:
            raise ValueError("Nothing was sent to the owner's own Slack DM.")
        user = await self.owner_slack_id()
        mcp = self.runtime.slack.mcp
        try:
            listed = await mcp.call(
                "slack_read_channel", {"channel_id": dm["channel"], "limit": DM_PAGE}
            )
        except Exception as exc:
            self.tried("replies", False, f"could not read the DM: {exc}", OWN_SLACK_DM)
            raise
        stamps = dm.get("threads") or []
        replies = await asyncio.gather(
            *(
                mcp.call("slack_read_thread", {"channel_id": dm["channel"], "message_ts": stamp})
                for stamp in stamps
            ),
            return_exceptions=True,
        )
        entries = messages(listed)
        page = [float(entry["ts"]) for entry in entries if entry["ts"]]
        gone, failed = [], 0
        for stamp, thread in zip(stamps, replies, strict=True):
            if isinstance(thread, BaseException):
                # One that cannot be read (deleted, a passing error) does not stop the rest;
                # one Slack says is not there is not read again.
                logger.info("Could not read the notification thread %s: %s", stamp, thread)
                if re.search(r"thread_not_found|not found", str(thread), re.IGNORECASE):
                    gone.append(stamp)
                else:
                    failed += 1
                continue
            # Its replies, not the notification itself.
            entries += [e for e in messages(thread) if e["ts"] and float(e["ts"]) > float(stamp)]
        if gone:
            kept = [stamp for stamp in stamps if stamp not in gone]
            self.store.set("away_slack_dm", json.dumps({**dm, "threads": kept}))
        gaps = []
        if failed:
            gaps.append(f"could not read {failed} of {len(stamps)} notification threads")
        # A full page that does not reach back to what was read before: the rest is unread.
        reached = max(dm["after"], float(self.store.get("away_dm_seen") or 0))
        if len(page) >= DM_PAGE and min(page) > reached:
            gaps.append(f"the DM listing stopped at {DM_PAGE} messages: earlier ones were not read")
        self.covered(gaps)
        if not gaps:
            self.tried("replies", True, "", OWN_SLACK_DM)
        if stamps and float(stamps[-1]) in page:
            self.tried("delivery", True, "", OWN_SLACK_DM)  # The latest one is there.
        found = {
            entry["ts"]: entry
            for entry in entries
            if entry["user"] == user
            and entry["ts"]
            and float(entry["ts"]) > dm["after"]
            and not self.store.one(
                "SELECT id FROM triggers WHERE key=? AND source='slack-sent'",
                (f"slack:{entry['ts']}",),
            )
        }
        return sorted(found.values(), key=lambda entry: float(entry["ts"])), not gaps

    def covered(self, gaps: list[str]):
        """How much of the owner's own DM the last read covered: "limited" (and why) when part
        of it could not be read; a full read clears it."""
        before = json.loads(self.store.get("away_coverage") or "{}")
        detail = "; ".join(gaps)
        self.store.set(
            "away_coverage", json.dumps({"limited": bool(gaps), "at": now(), "detail": detail})
        )
        if bool(before.get("limited")) != bool(gaps) or before.get("detail", "") != detail:
            self.runtime.publish_state()

    async def t_read_owner_slack_dm(self, params: ReadOwnerSlackDm, caller: str):
        """What the owner wrote in their own Slack DM since a notification was sent there:
        their own messages only, not the notifications; each with when (for its `at`)."""
        self.check(caller, (MESSENGER,))
        found, complete = await self.owner_dm_messages()
        limited = (
            ""
            if complete
            else "\n(Coverage limited: "
            + json.loads(self.store.get("away_coverage") or "{}").get("detail", "")
            + ". What could not be read is read again later.)"
        )
        if not complete:
            # Part of it could not be read: what is reported now is not all there is, so
            # nothing counts as seen (see owner_dm_news); what was looked at is not run for
            # again at once.
            self.store.delete("away_dm_reading")
            if found:
                self.store.set("away_dm_looked", str(max(float(e["ts"]) for e in found)))
        self.own_dm_read = [{"ts": entry["ts"], "text": entry["text"]} for entry in found]
        if not found:
            return "No reply from the owner there yet." + limited
        newest = str(max(float(entry["ts"]) for entry in found))
        seen = float(self.store.get("away_dm_seen") or 0)
        codes = {code.lower() for code in self.reply_codes()}
        fresh = [entry for entry in found if float(entry["ts"]) > seen]
        coded = [
            entry
            for entry in fresh
            if any(code in codes for code in re.findall(r"[a-z2-7]{5}", entry["text"].lower()))
        ]
        # One that names no code answers the notification before it while that waits (see
        # followed_codes): read again once if it was not reported, then seen (no endless runs).
        codeless = any(
            self.followed_codes(float(entry["ts"])) for entry in fresh if entry not in coded
        )
        if complete and (coded or (codeless and self.store.get("away_dm_reading") != newest)):
            # Seen once it reported them: a run that fails before that leaves them for the next.
            self.store.set("away_dm_reading", newest)
        elif complete:
            self.store.set("away_dm_seen", newest)  # Nothing in them to report.
        return (
            "The owner wrote there (oldest first):\n"
            + "\n".join(
                f"- ({datetime.fromtimestamp(float(entry['ts']), UTC).isoformat()}) {entry['text']}"
                for entry in found
            )
            + limited
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
        if pending:
            # Its codes are kept only when the owner got them: it went out as the version with
            # codes (as send_owner_slack_dm sent it; another way, the messenger sent version A
            # when it says how it reads the replies).
            coded = pending.get("coded") if pending.get("slack_dm") else params.replies.strip()
            if not (params.ok and coded):
                self.drop_issued(pending)
        if params.ok:
            # A message went out (the send tool said so, for the owner's own Slack DM).
            self.tried("send", True, "", where)
            if not params.replies.strip() and not (pending and pending.get("slack_dm")):
                # Its tools found no way to read replies there (the own Slack DM's are tried
                # by reading them: read_back).
                self.tried("replies", False, "no way to read them there was found", where)
            if params.method.strip():
                self.store.set("away_method", params.method.strip()[:1000])
            if where:
                self.store.set("away_where", where)
            replies = params.replies.strip()[:1000]
            if where != OWN_SLACK_DM and replies not in {"", self.store.get("away_replies")}:
                self.untried("replies")  # Read another way now: not tried yet.
            self.store.set("away_replies", replies)
            self.store.delete("away_retry_at")
            if pending and pending.get("test"):
                [item] = pending["items"]
                if code := self.store.get(f"awayitem:{item}"):
                    self.store.set("away_test", json.dumps({"item": item, "code": code}))
            elif pending:
                sent = json.loads(self.store.get("away_sent") or "[]")
                self.store.set("away_sent", json.dumps((sent + pending["items"])[-200:]))
                for item_id in pending["items"]:
                    self.store.delete(f"awayreask:{item_id}")  # Asked again now.
                if pending["items"]:
                    # What a reply naming no code answers (followed_codes).
                    notified = [
                        notified
                        for notified in json.loads(self.store.get("away_notified") or "[]")
                        if time.time() - notified["at"] < READ_REPLIES_HOURS * 3600
                    ]
                    notified.append({"at": pending["at"], "items": pending["items"]})
                    self.store.set("away_notified", json.dumps(notified[-50:]))
                told = set(pending.get("notes") or [])
                notes = json.loads(self.store.get("away_notes") or "[]")
                self.store.set(
                    "away_notes", json.dumps([note for note in notes if note["text"] not in told])
                )
        else:
            self.tried("send", False, params.error, yours(params.where.strip())[:300])
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
        known = {*self.open_codes(), *self.told_codes(), *self.used_codes()}
        if test:
            known.add(test["code"])
        replies, unsure = self.place_codeless(params.replies, known)
        # A reply to a draft or a merge card says what it means: the messenger reads it.
        unsaid = [
            reply.code.strip().upper()
            for reply in replies
            if (item := items.get(self.code_item(reply.code.strip().upper()) or ""))
            and self.decides_draft(item)
            and reply.intent not in DRAFT_INTENTS
        ]
        if unsaid:
            raise ValueError(
                f"{', '.join(unsaid)}: a draft or a PR to merge: pass each reply again with "
                "intent='send', 'no', 'change' or 'unclear' (what the owner means)."
            )
        if reading := self.store.get("away_dm_reading"):
            # What it read in the owner's own Slack DM reached here: seen.
            self.store.set("away_dm_seen", reading)
            self.store.delete("away_dm_reading")
        done = [said for reply, codes in unsure if (said := await self.ask_which(reply, codes))]
        # A code's newest reply is what it says (an earlier one may be a joke taken back);
        # the others are judged as superseded, never applied later.
        ordered = list(replies)
        if all(reply.at for reply in ordered):
            ordered.sort(key=lambda reply: reply.at)
        newest = {reply.code.strip().upper(): reply for reply in ordered}
        for reply in ordered:
            code = reply.code.strip().upper()
            if reply is not newest[code]:
                part = answer_part(reply.text, code, known)
                key = f"awayjudged:{reply_key(code, part)}"
                if not self.store.get(key):
                    self.store.set(key, "superseded by a later reply")
        for code, reply in newest.items():
            part = answer_part(reply.text, code, known)
            key = f"awayjudged:{reply_key(code, part)}"
            item_id = self.code_item(code) if CODE.fullmatch(code) else ""
            used = json.loads(self.store.get(f"awayused:{code}") or "null")
            item = items.get(item_id)
            gone = not item and not used and not (test and code == test["code"])
            judged = self.store.get(key)
            if judged and not (gone and judged == GONE):
                continue  # Judged before: never again (a second reading could differ).
            outcome = ""
            if gone:
                outcome = self.what_became(item_id) or self.say(
                    "nothing waits for that code (answered already?)"
                )
                # Nothing to apply: said again only once what became of it changes (the PR
                # closed, then reopened). Should it wait again, these words never apply to it.
                self.store.set(key, GONE)
                key += ":" + hashlib.sha256(outcome.encode()).hexdigest()[:8]
                if self.store.get(key):
                    continue
            if test and code == test["code"]:
                self.store.delete("away_test")
                self.store.delete(f"awaycode:{code}")
                done.append(f"{code}: your answer came back")
                outcome = self.say("your answers come back: replies here work")
                self.verified_replies()
            elif used:
                # Answered already: this reply changes nothing; said once, here and where the
                # owner is (they may think it held something back).
                became = self.what_became(used["item"]) if isinstance(used, dict) else ""
                outcome = (
                    self.say("not done: you answered this code already ({became})", became=became)
                    if became
                    else self.say("not done: you answered this code already")
                )
                done.append(f"{code}: {outcome}")
                await self.tell_away(self.answered_already(code, used, became))
            elif not item:
                done.append(f"{code}: {outcome}")
                if self.store.get(f"awaynoted:{code}") == outcome:
                    self.store.set(key, outcome)
                    continue  # Said once already.
                self.store.set(f"awaynoted:{code}", outcome)
            else:
                self.verified_replies()  # A code it sent came back: replies are read.
                try:
                    outcome = await self.apply(item, f"{code} {part}", code, reply.intent)
                    self.store.delete(f"awaycode:{code}")  # Used once.
                    self.store.set(
                        f"awayused:{code}",
                        json.dumps({"at": time.time(), "item": item_id, "outcome": outcome}),
                    )
                    self.store.delete(f"awayreask:{item_id}")
                except (ValueError, LookupError, RuntimeError) as exc:
                    outcome = self.say("not done: {reason}", reason=exc)
                    # Asked again where the owner is, not only here (see notify_away).
                    self.store.set(f"awayreask:{item_id}", str(exc)[:200])
                done.append(f"{code}: {outcome}")
            self.store.set(key, outcome)
            self.runtime.add_message(
                self.runtime.router_id(),
                "notice",
                f"You answered from {yours(self.store.get('away_where') or 'elsewhere')}: "
                f"[{code}] {part[:200]} \u2192 {outcome}",
                {"away_reply": True},
            )
        self.runtime.publish_state()
        return "\n".join(done) or "Nothing to apply."

    def place_codeless(
        self, replies: list[OwnerReply], known: set[str]
    ) -> tuple[list[OwnerReply], list[tuple[OwnerReply, list[str]]]]:
        """Replies with the code each answers. One that names no code answers the only code
        that waited when it was written (see followed_codes), whatever code the messenger gave
        it; one that could answer several is asked about (the second list: with those codes);
        one that answers nothing that waits is left out."""
        placed, unsure = [], []
        for reply in replies:
            code = reply.code.strip().upper()
            named = {found.upper() for found in ANY_CODE.findall(reply.text)}
            if (code and code in named) or named & known:
                placed.append(reply)
                continue
            # The messenger may leave the code out of the owner's words: their own message, as
            # Chief of Staff read it, says which; never the messenger's code for it.
            own = self.own_dm_codes(reply, known)
            named = set().union(*own)
            if code and code in named:
                placed.append(reply)
                continue
            waiting = named - set(self.used_codes())
            if own and all(own) and len(named) == 1 and waiting:
                placed.append(reply.model_copy(update={"code": waiting.pop()}))
                continue
            # It names no code: whatever code the messenger gave it, it answers the only one open
            # then; with several, the owner is asked which.
            codes = self.followed_codes(epoch(reply.at))
            if len(codes) == 1:
                placed.append(reply.model_copy(update={"code": codes[0]}))
            elif codes:
                unsure.append((reply, codes))
        return placed, unsure

    def own_dm_codes(self, reply: OwnerReply, known: set[str]) -> list[set[str]]:
        """The codes of each message the owner wrote in their own Slack DM that is this reply,
        whole, with its codes left out ("no, I'll answer Bob myself" is "S443M no, I'll answer Bob
        myself"; "no" is not "AAAAA no, not now"), from what Chief of Staff read there itself in
        this run; of several, the one written when the reply was, if one is. Nothing for a way
        only the messenger reads: there its copy is all there is."""
        said = loose_words(reply.text)
        if not said:
            return []
        found = []
        for entry in self.own_dm_read:
            codes = {name.upper() for name in ANY_CODE.findall(entry["text"])} & known
            rest = ANY_CODE.sub(
                lambda name, codes=codes: "" if name.group(1).upper() in codes else name.group(0),
                entry["text"],
            )
            if loose_words(rest) == said:
                found.append((float(entry["ts"]), codes))
        at = epoch(reply.at) if reply.at else 0
        # Its time as the messenger was shown it (to the microsecond); one rounded narrows nothing.
        if len(found) > 1 and (then := [one for one in found if abs(one[0] - at) < 0.001]):
            found = then
        return [codes for _, codes in found]

    async def ask_which(self, reply: OwnerReply, codes: list[str]) -> str:
        """A reply that names no code after a notification with several: the owner is asked
        which code it answers, once per reply."""
        key = hashlib.sha256(f"{reply.at}\n{reply.text.strip()}".encode()).hexdigest()[:16]
        if self.store.get(f"awayasked:{key}"):
            return ""
        self.store.set(f"awayasked:{key}", now())
        listed = ", ".join(codes)
        await self.tell_away(
            self.say("Which one? Reply with its code: {codes}", codes=listed), asks=True
        )
        said = " ".join(reply.text.split())[:200]
        self.runtime.add_message(
            self.runtime.router_id(),
            "notice",
            f"You answered from {yours(self.store.get('away_where') or 'elsewhere')}: {said} "
            f"\u2192 it names no code: you were asked which one ({listed})",
            {"away_reply": True},
        )
        return f"(no code) {said[:80]}: the owner is asked which code it answers ({listed})"

    def answered_already(self, code: str, used: Any, became: str) -> str:
        """For the owner where they are: what became of a code they answered before, when a
        reply names it again ("wait, don't send that yet")."""
        at = used.get("at") if isinstance(used, dict) else used
        clock = datetime.fromtimestamp(float(at or time.time())).astimezone().strftime("%H:%M")
        outcome = used.get("outcome", "") if isinstance(used, dict) else ""
        if became == self.say("sent already") or outcome in {
            self.say("sent"),
            self.say("marked as sent"),
        }:
            return self.say(
                "{code} was already sent at {clock}: this reply changes nothing.",
                code=code,
                clock=clock,
            )
        was = became or outcome
        if not was:
            return self.say(
                "{code} was answered already at {clock}: this reply changes nothing.",
                code=code,
                clock=clock,
            )
        return self.say(
            "{code} was answered already at {clock} ({became}): this reply changes nothing.",
            code=code,
            clock=clock,
            became=was,
        )

    def what_became(self, item_id: str) -> str:
        """What became of a draft (or a merge card) a code was given for, once it no longer
        waits for the owner (replaced by a newer one, withdrawn, sent, merged, its PR closed);
        "" when the item is not such a draft."""
        if item_id.startswith("draft:merge:"):
            return self.merge_became(item_id.removeprefix("draft:"))
        if not item_id.startswith("draft:"):
            return ""
        try:
            draft = self.store.draft(item_id.removeprefix("draft:"))
        except LookupError:
            return ""
        outcome = self.runtime.drafts.owner_text(draft["outcome"] or "").strip()
        if draft["status"] == "pending":
            return ""
        if draft["status"] in {"sent", "sending", "uncertain"}:
            return self.say("sent already" if draft["status"] == "sent" else "being sent: see Home")
        if draft["status"] == "failed":
            return (
                self.say("its sending failed: {outcome}", outcome=outcome)
                if outcome
                else self.say("its sending failed")
            )
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
            reason = outcome.removeprefix("Withdrawn").lstrip(":. ")
            return self.say("withdrawn: {reason}", reason=reason)
        if outcome.startswith("Redo:"):
            reason = outcome.removeprefix("Redo:").strip()
            return self.say("you asked for it again: {reason}", reason=reason)
        if outcome:
            return self.say("dropped already ({outcome})", outcome=outcome)
        return self.say("dropped already")

    def verified_replies(self):
        """A code a notification gave came back: it arrived, and replies there are read."""
        self.tried("delivery", True, "you answered it")
        self.tried("replies", True, "a reply came back")

    def tried(self, what: str, ok: bool, detail: str = "", where: str | None = None):
        """What the way the owner is reached away did when it was just used: "send",
        "delivery" or "replies" worked, or failed (see capabilities). `where`: the way (where
        notifications went); a message that went out another way starts afresh, as what the
        last way could do says nothing of it."""
        where = (self.store.get("away_where") or "") if where is None else where
        tested: dict[str, Any] = json.loads(self.store.get("away_tested") or "{}")
        if what == "send" and ok and where != tested.get("way"):
            tested = {"way": where}
        tested[what] = {
            "ok": ok,
            "at": now(),
            "where": where,
            "detail": " ".join(detail.split())[:300],
        }
        self.store.set("away_tested", json.dumps(tested))

    def untried(self, what: str):
        tested = json.loads(self.store.get("away_tested") or "{}")
        if tested.pop(what, None) is not None:
            self.store.set("away_tested", json.dumps(tested))

    def capabilities_of(self, where: str) -> dict:
        """What was tried of one way to the owner: each capability's last result there."""
        tested = json.loads(self.store.get("away_tested") or "{}")
        return {
            what: entry
            for what, entry in tested.items()
            if isinstance(entry, dict) and entry.get("where") == where
        }

    def capabilities(self) -> dict:
        """What the way the owner is reached away can do, as tried when it was used (by the
        Away test, and by notifications and reads as they happen), never as a tool claims:
        Send (a message went out), Delivery (it was found where it went, or the owner answered
        it), Read replies (their replies there were read: in the DM and in the threads of the
        notifications), each yes, no or untested, with when; New commands: no, by design (see
        NO_NEW_COMMANDS). Send only: messages go out, replies there are not read. Coverage: how
        much of the owner's own DM the last read could read."""
        tested = json.loads(self.store.get("away_tested") or "{}")
        found: dict[str, Any] = {}
        for what in ("send", "delivery", "replies"):
            entry = tested.get(what)
            found[what] = (
                {
                    "state": "yes" if entry["ok"] else "no",
                    "at": entry["at"],
                    "detail": entry.get("detail", ""),
                }
                if isinstance(entry, dict)
                else {"state": "untested", "at": "", "detail": ""}
            )
        found["commands"] = {"state": "no", "at": "", "detail": NO_NEW_COMMANDS}
        found["send_only"] = found["send"]["state"] == "yes" and found["replies"]["state"] == "no"
        coverage = json.loads(self.store.get("away_coverage") or "{}")
        found["coverage"] = {
            "limited": bool(coverage.get("limited")),
            "at": coverage.get("at", ""),
            "detail": coverage.get("detail", ""),
        }
        return found

    @staticmethod
    def decides_draft(item: dict) -> bool:
        """An item answered with send / no / change: a reply to send, a review to post, a PR
        to merge."""
        draft = item.get("draft")
        return bool(draft) and draft["kind"] in {"merge", "slack", "github", "reply"}

    async def apply(self, item: dict, text: str, code: str, intent: str = "answer") -> str:
        """The owner's answer to one card, from elsewhere: what follows its code (any case).
        `intent`: what the messenger read it to mean, for a draft or a merge card (see
        OwnerReply); a send it holds back is not sent (holds_send)."""
        answer = without_code(text, code)
        word = answer.lower().strip(" .!")
        say = self.say
        if item.get("ask"):
            if not answer:
                raise ValueError(say("write your answer after the code"))
            await self.runtime.owner_send(self.runtime.router_id(), answer)
            return say("sent to the Router")
        if draft := item.get("draft"):
            kind = draft["kind"]
            drafts = self.runtime.drafts
            if kind == "merge":
                held = holds_send(answer)
                if intent == "send" and not held:
                    merged = await drafts.resolve(draft["id"], DraftDecision(action="send"))
                    return merged.get("outcome") or say("merged")
                if intent == "no":
                    await drafts.resolve(draft["id"], DraftDecision(action="reject"))
                    return say("left as it is")
                # Said why when its words say it ("after CI", "tomorrow").
                if held:
                    raise ValueError(
                        say(
                            'not merged: \u201c{held}\u201d sounds like not now: answer "merge" '
                            'when it should be merged, or "no"',
                            held=held,
                        )
                    )
                raise ValueError(
                    say('not merged: answer "merge" when it should be merged, or "no"')
                )
            if kind in {"slack", "github", "reply"}:
                target = draft["target"]
                if kind == "github" and not (
                    target.get("in_reply_to") or target.get("reply_to_comment") or target.get("own")
                ):
                    verdict = str(target.get("verdict") or "comment")
                    if verdict != "approve" and "approve" in re.findall(r"\w+", word):
                        # It would post as it is, not as an approval.
                        raise ValueError(
                            say(
                                "not posted: this review is {verdict}, not an approval: answer "
                                '"post" to post it as it is, or "no"',
                                verdict=say(VERDICTS.get(verdict, verdict)),
                            )
                        )
                # What it means is the messenger's reading; a send it holds back ("ok wait",
                # "send it after lunch", "发吗") is not one, nor a draft with blanks.
                held = holds_send(answer)
                if intent == "send" and held:
                    raise ValueError(
                        say(
                            "not sent: \u201c{held}\u201d sounds like not now, or not as it is: "
                            'answer "send" when it should go, or "no"',
                            held=held,
                        )
                    )
                if intent == "send" and (blank := BLANKS.search(draft["text"])):
                    raise ValueError(
                        say(
                            "not sent: the draft still has a blank to fill (\u201c{blank}\u201d): "
                            "say the words to use instead, or edit it on Home",
                            blank=blank.group(0),
                        )
                    )
                if intent == "send":
                    await drafts.resolve(draft["id"], DraftDecision(action="send"))
                    return say("sent" if kind != "reply" else "marked as sent")
                if intent == "no":
                    # Their words are the reason (on its card, and for how they write).
                    reason = "From away" if word in BARE_NO else answer
                    await drafts.resolve(draft["id"], DraftDecision(action="reject", reason=reason))
                    return say("dropped")
                blanks = blanks_of(draft["text"])
                if intent != "change" or not answer:
                    # Said why when its words say it ("after lunch"), as for a send held back.
                    if blanks and held:
                        raise ValueError(
                            say(
                                "not sent: \u201c{held}\u201d sounds like not now, or not as it "
                                'is: say the words for its blanks ({blanks}), or "no"',
                                held=held,
                                blanks=blanks,
                            )
                        )
                    if blanks:
                        raise ValueError(
                            say(
                                'not sent: say the words for its blanks ({blanks}), or "no"',
                                blanks=blanks,
                            )
                        )
                    if held:
                        raise ValueError(
                            say(
                                "not sent: \u201c{held}\u201d sounds like not now, or not as it "
                                'is: answer "send" when it should go, or "no"',
                                held=held,
                            )
                        )
                    raise ValueError(say('not sent: answer "send" when it should go, or "no"'))
                # Anything else is what to change (or the words for its blanks): it is drafted
                # again that way.
                reason = f"Fill in its blanks: {answer}" if blanks else answer
                await drafts.resolve(draft["id"], DraftDecision(action="revise", reason=reason))
                return say("to be drafted again as you say")
            if kind == "decision":
                options = draft["target"].get("options") or []
                if word.isdigit() and 1 <= int(word) <= len(options):
                    answer = options[int(word) - 1]["choice"]
                await drafts.resolve(draft["id"], DraftDecision(action="answer", text=answer))
                return say("decided: {answer}", answer=answer[:100])
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
                        say(
                            'answer with the number of a folder, "none" for no project, or the '
                            "path of a folder"
                            if options
                            else 'answer "none" for no project, or the path of a folder'
                        )
                    )
                await drafts.resolve(draft["id"], DraftDecision(action="choose", choice=choice))
                return (
                    say("chose {choice}", choice=choice)
                    if choice
                    else say("no project: it starts in a folder of its own")
                )
            await drafts.resolve(draft["id"], DraftDecision(action="answer", text=answer))
            return say("answered")
        if question := item.get("question"):
            if not answer:
                raise ValueError(say("write your answer after the code"))
            await self.runtime.task_actions.answer_question(question["id"], answer)
            return say("sent to the worker")
        task = item["task"]
        if task["status"] == "failed":
            if word not in RETRY_WORDS:
                raise ValueError(say('answer "retry"'))
            await self.runtime.task_actions.retry(task["id"])
            return say("retrying")
        if task["status"] == "waiting" and self.store.get(f"stopped:{task['id']}"):
            # Stopped mid-work: Go on (with a note, if they wrote one) or Cancel, as on Home.
            if word in CANCEL_WORDS:
                await self.runtime.task_actions.cancel(task["id"])
                return say("cancelled")
            if word in GO_ON_WORDS:
                answer = "Go on where you stopped."
            elif not answer or word in REJECT_WORDS:
                raise ValueError(say('answer "go on" or "cancel"'))
            await self.runtime.task_actions.follow_up(task["id"], answer)
            return say("going on")
        if not answer:
            raise ValueError(say("write your answer after the code"))
        await self.runtime.task_actions.follow_up(task["id"], answer)
        return say("sent to the worker")

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
