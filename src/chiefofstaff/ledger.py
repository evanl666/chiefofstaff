"""The asks ledger (QA10 E151, E152, E158, E159): every ask of the owner, whatever it came by,
is filed under a subject, the one choice it is about, so that one choice is asked once, answered
once, and known by every worker that waits on it.

- Channels: a decision card (`decision`, ask_decision), a question or blocked report
  (`question`, by its `ownerq:` id), a question card (`ask_user`, Copilot's ask_user), a note of
  the Router's that needs the owner (`notify`), and an ask at the end of a done report filed as
  a question (Tools.embedded_ask).
- Sameness is Chief of Staff's, not the asking model's: the topic words of two asks (numbers
  whole, Chinese as character pairs) are compared, with no model call (a tool call answers at
  once). The same within one owner request (a team is a request's tasks) or a worker's own tasks
  joins one subject; across requests it is only "possibly the same", for the owner to Merge.
- Any answer settles the subject: every other ask on it is answered in its own channel with
  the same words, each worker once (`settle`), and a choice is kept as the decision on it.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from . import catalog
from .envelope import wrap
from .models import DraftDecision
from .peers import clip
from .roles import ROUTER
from .store import hours_ago, now
from .tasks import OPEN

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

SAME = "same"
POSSIBLY = "possibly"
LATIN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")
# Words any question to the owner uses: they tell no choice from another.
STOP = catalog.STOP | frozenset(
    "about again all any ask asked asking choose choice decide decided decision get go going "
    "if instead into is keep kept may might more must need needed needs next no not now ok "
    "okay one option options pick prefer question shall should so some still than us use want "
    "wants yes".split()
)
CJK_STOP = frozenset(
    "我们 你们 是否 需要 还是 哪个 哪一 应该 选择 决定 一下 可以 这个 那个 什么 现在 继续 请你 "
    "请您 是不 不是 要不 不要 用哪 你的 我的 的话 一个".split()
)
# Which ask of a subject Needs you shows (the others fold into it): the richest first.
CHANNELS = ("decision", "ask_user", "question", "notify")
# Open asks compared for "possibly the same" on Home (pairs: a few dozen at most).
COMPARED = 40
# A joined ask of the Router's (notify_owner refused as asked already) waits this long at most.
ROUTER_ASK_HOURS = 72


def plain(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(text.split()).rstrip(".!?\u3002\uff01\uff1f")


def topic(text: str) -> frozenset[str]:
    """What a question is about: its telling words (numbers whole: "1.11", "v0.1.58"), and
    character pairs of Chinese and Japanese text."""
    text = unicodedata.normalize("NFKC", text).casefold()
    found = set()
    for word in LATIN.findall(text):
        if word in STOP:
            continue
        if (
            word.isalpha()
            and len(word) > 3
            and word.endswith("s")
            and not word.endswith(("ss", "us", "is"))
        ):
            word = word[:-1]
        found.add(word)
    for run in catalog.CJK.findall(text):
        pairs = [run] if len(run) == 1 else [run[i : i + 2] for i in range(len(run) - 1)]
        found.update(pair for pair in pairs if pair not in CJK_STOP)
    return frozenset(found)


def likeness(one: str, other: str) -> tuple[str, float]:
    """Whether two asks are about the same choice: SAME (the same words, or most of the shorter
    one's telling words in the other), POSSIBLY (several shared), or "" (not)."""
    if plain(one) and plain(one) == plain(other):
        return SAME, 1.0
    a, b = topic(one), topic(other)
    shared = len(a & b)
    if shared < 2:
        return "", 0.0
    small, large = sorted((len(a), len(b)))
    held = shared / small
    # Two words a long report happens to share with a short question are not its subject.
    if held >= 0.6 and (shared >= 3 or large <= 8):
        return SAME, held
    if held >= 0.35:
        return POSSIBLY, held
    return "", 0.0


def related(one: str, other: str) -> bool:
    """Whether a question is about what a decision of its own task settled: the same, or half
    the telling words of the shorter shared (R1: "Which tie rule, then?" of "Rule A or rule
    B?"). An unrelated one on that task is not (owner report 0.1.58: "already decided")."""
    if likeness(one, other)[0]:
        return True
    a, b = topic(one), topic(other)
    shared = len(a & b)
    return bool(shared) and shared / min(len(a), len(b)) >= 0.5


def subject_id() -> str:
    return "sub" + uuid4().hex[:6]


# A report's last words that ask the owner to choose (E152: "Want me to open a PR?"), or say
# it still waits on them (live D6: "I'm blocked: I asked you for three things").
ASKS_TO_CHOOSE = re.compile(
    r"\b(?:should|shall) (?:i|we)\b|\bdo you (?:want|prefer)\b|\bwould you (?:like|prefer)\b|"
    r"\bwant me to\b|\bwhich\b|\bprefer\b|\bor\b|\bok(?:ay)? (?:to|with)\b|\bconfirm\b|"
    r"要不要|是否|还是|哪个|哪一|哪种|选哪|你想|您想|需不需要|可以吗|行吗|好吗",
    re.IGNORECASE,
)
ASKS_PLAINLY = re.compile(
    r"\b(?:please|pls) (?:choose|decide|confirm|pick|let me know|tell me)\b|"
    r"\blet me know (?:which|whether|if)\b|\byour call\b|\bup to you\b|"
    r"请(?:你|您)?(?:选择|决定|确认|告诉|回复|定)",
    re.IGNORECASE,
)
STILL_WAITS = re.compile(
    r"\bblocked\b|\bwaiting (?:on|for) (?:you|your)\b|\bneeds? (?:from )?you\b|\bneed your\b|"
    r"\bfrom you\b|\buntil you\b|卡住|等你|等您|需要你|需要您|差你|差您",
    re.IGNORECASE,
)
SENTENCE = re.compile("(?<=[.!?\u3002\uff01\uff1f])\\s*|\n+")


def tail(text: str) -> str:
    """A report's last words: its last paragraph (or the last 400 characters of it)."""
    parts = [part.strip() for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]
    return parts[-1][-400:] if parts else ""


def embedded_ask(text: str) -> str:
    """The ask a report ends with, for the owner to choose ("" for none): a question to them
    among its last sentences, or a plain request to decide."""
    sentences = [s.strip() for s in SENTENCE.split(tail(text)) if s and s.strip()]
    for sentence in reversed(sentences[-3:]):
        asking = sentence.endswith(("?", "\uff1f")) and ASKS_TO_CHOOSE.search(sentence)
        if asking or ASKS_PLAINLY.search(sentence):
            return clip(sentence, 500)
    return ""


def still_waits(text: str) -> bool:
    """A report's last words say it still waits on the owner."""
    return bool(STILL_WAITS.search(tail(text)))


class Ledger:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store

    # Filing

    def anchor(self, session_id: str, task_id: str = "") -> str:
        """Where an ask joins others by itself: the owner request its task was made for (a team
        is a request's tasks), else its worker's own tasks; "" for the Router's (it joins any)."""
        row = self.store.find_session(session_id) if session_id else None
        if not row or row["role"] == ROUTER:
            return ""
        if task_id and (request := self.store.get(f"req:{task_id}")):
            return f"req:{request}"
        return f"worker:{session_id}"

    def candidates(self) -> list[dict]:
        """Subjects an ask may be about: those with an open ask, or a decision in force."""
        return self.store.all(
            """SELECT * FROM subjects WHERE merged_into='' AND (
            id IN (SELECT subject_id FROM asks WHERE status='open')
            OR decision_id IN (SELECT id FROM decisions WHERE status='active'))
            ORDER BY updated_at DESC LIMIT 300"""
        )

    def texts(self, subject: dict) -> list[str]:
        """What a subject was asked as: its first words, its open asks', and the question of
        the decision on it."""
        found = [subject["title"]]
        found += [
            row["text"]
            for row in self.store.all(
                "SELECT text FROM asks WHERE subject_id=? AND status='open'", (subject["id"],)
            )
        ]
        if decision := self.decided(subject):
            found.append(decision["text"].partition(" \u2192 ")[0])
        return list(dict.fromkeys(text for text in found if text))

    def anchors(self, subject: dict) -> set[str]:
        rows = self.store.all(
            "SELECT DISTINCT session_id, task_id FROM asks WHERE subject_id=?", (subject["id"],)
        )
        found = {self.anchor(row["session_id"], row["task_id"]) for row in rows}
        return (found | {subject["anchor"]}) - {""}

    def joins(self, subject: dict, anchor: str) -> bool:
        """Whether an ask from `anchor` joins a subject by itself: never across requests."""
        theirs = self.anchors(subject)
        return not anchor or not theirs or theirs == {anchor}

    def match(self, text: str, anchor: str, exclude: str = "") -> list[dict]:
        """The subjects an ask is about, best first: {subject, level, joins}."""
        found = []
        for subject in self.candidates():
            if subject["id"] == exclude:
                continue
            levels = [likeness(text, said) for said in self.texts(subject)]
            level, score = max(levels, key=lambda pair: (pair[0] == SAME, pair[1]))
            if level:
                found.append(
                    {
                        "subject": subject,
                        "level": level,
                        "score": score,
                        "joins": level == SAME and self.joins(subject, anchor),
                    }
                )
        found.sort(key=lambda item: (not item["joins"], item["level"] != SAME, -item["score"]))
        return found

    def same(self, text: str, anchor: str) -> dict | None:
        """The subject an ask from `anchor` joins by itself, if one."""
        found = self.match(text, anchor)
        return found[0]["subject"] if found and found[0]["joins"] else None

    def file(
        self,
        channel: str,
        ref: str,
        session_id: str,
        task_id: str,
        text: str,
        *,
        options: list[str] | None = None,
        subject: str = "",
        join: bool = True,
    ) -> dict:
        """An ask, filed under its subject: `subject` when it joined one, else the one it is
        the same as (`join`), else a new one. Asked again on the same card or question (still
        open), it is that ask, its words as they stand now. `options`: the choices it gives
        (None: not a choice, e.g. a question for a file or a token)."""
        text = clip(" ".join(text.split()), 2000)
        chosen = json.dumps(options) if options is not None else ""
        found = self.store.one(
            "SELECT * FROM asks WHERE channel=? AND ref=? AND session_id=? AND status='open'",
            (channel, ref, session_id),
        )
        if found:
            self.store.execute(
                "UPDATE asks SET text=?, options=? WHERE id=?", (text, chosen, found["id"])
            )
            return {**found, "text": text, "options": chosen}
        anchor = self.anchor(session_id, task_id)
        if not subject and join:
            subject = (self.same(text, anchor) or {}).get("id", "")
        stamp = now()
        if subject:
            self.store.execute(
                "UPDATE subjects SET updated_at=?, anchor=CASE WHEN anchor='' THEN ? ELSE anchor "
                "END WHERE id=?",
                (stamp, anchor, subject),
            )
        else:
            subject = subject_id()
            self.store.execute(
                """INSERT INTO subjects(id, anchor, title, created_at, updated_at)
                VALUES (?,?,?,?,?)""",
                (subject, anchor, clip(text, 300), stamp, stamp),
            )
        identifier = "ask" + uuid4().hex[:8]
        self.store.execute(
            """INSERT INTO asks(id, subject_id, channel, ref, session_id, task_id, text, options,
            created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (identifier, subject, channel, ref, session_id, task_id, text, chosen, stamp),
        )
        return self.store.row("SELECT * FROM asks WHERE id=?", (identifier,))

    def subject_of(self, channel: str, ref: str) -> dict | None:
        row = self.store.one(
            "SELECT subject_id FROM asks WHERE channel=? AND ref=? ORDER BY created_at DESC",
            (channel, ref),
        )
        return self.subject(row["subject_id"]) if row else None

    def subject(self, identifier: str) -> dict | None:
        found = self.store.one("SELECT * FROM subjects WHERE id=?", (identifier,))
        seen = set()
        while found and found["merged_into"] and found["id"] not in seen:
            seen.add(found["id"])
            found = self.store.one("SELECT * FROM subjects WHERE id=?", (found["merged_into"],))
        return found

    def decided(self, subject: dict | None) -> dict | None:
        """The decision in force on a subject (as it stands now), if one."""
        if not subject or not subject["decision_id"]:
            return None
        try:
            found = self.runtime.decisions.current(subject["decision_id"])
        except ValueError:
            return None
        return found if found["status"] == "active" else None

    def of_decision(self, decision: dict) -> dict | None:
        """The subject a decision (as it stands now) settles: its own, made for it if it had
        none (one from before the ledger)."""
        if found := self.store.one(
            "SELECT * FROM subjects WHERE decision_id=? AND merged_into=''", (decision["id"],)
        ):
            return found
        stamp, identifier = now(), subject_id()
        tasks = self.runtime.decisions.linked(decision["id"])
        task = self.runtime.tasks.find(tasks[0]) if tasks else None
        self.store.execute(
            """INSERT INTO subjects(id, anchor, title, decision_id, created_at, updated_at)
            VALUES (?,?,?,?,?,?)""",
            (
                identifier,
                self.anchor(task["session_id"], task["id"]) if task else "",
                clip(decision["text"].partition(" \u2192 ")[0], 300),
                decision["id"],
                stamp,
                stamp,
            ),
        )
        return self.subject(identifier)

    def recorded(self, decision: dict, tasks: list[str]):
        """A decision the Router recorded (replacing none): the subject it settles is the one
        asked the same (within its tasks' request), else one of its own. The asks open on it
        stay open (the Router says when its decision answers them: answers_question)."""
        if self.store.one("SELECT id FROM subjects WHERE decision_id=?", (decision["id"],)):
            return
        task = self.runtime.tasks.find(tasks[0]) if tasks else None
        anchor = self.anchor(task["session_id"], task["id"]) if task else ""
        question = decision["text"].partition(" \u2192 ")[0]
        found = [
            match
            for match in self.match(question, anchor)
            if match["joins"] and not self.decided(match["subject"])
        ]
        if found:
            self.decide(found[0]["subject"]["id"], decision["id"])
        else:
            self.of_decision(decision)

    def decide(self, subject: str, decision: str):
        self.store.execute(
            "UPDATE subjects SET decision_id=?, updated_at=? WHERE id=?", (decision, now(), subject)
        )

    def carry(self, old: list[str], new: str):
        """A decision replaced: the subjects it settled are settled by the new one."""
        for identifier in old:
            self.store.execute(
                "UPDATE subjects SET decision_id=? WHERE decision_id=?", (new, identifier)
            )

    # What is open

    def alive(self, ask: dict, updates: set[str] | None = None) -> bool:
        """Whether an ask still waits for the owner where it was asked (`updates`: the ids of
        the Router's notes under Needs you, when read already)."""
        channel, ref = ask["channel"], ask["ref"]
        if channel in {"decision", "ask_user"}:
            try:
                draft = self.store.draft(ref)
            except LookupError:
                return False
            if draft["status"] != "pending":
                return False
            if channel == "decision":
                askers = draft["target"].get("askers") or []
                return any(a["session_id"] == ask["session_id"] for a in askers)
            return True
        if channel == "question":
            return bool(self.store.get(f"ownerq:{ref}"))
        if channel == "notify":
            if not ref.isdigit():
                return ask["created_at"] > hours_ago(ROUTER_ASK_HOURS)
            if updates is None:
                updates = {str(update["id"]) for update in self.runtime.owner_updates()}
            return ref in updates
        return False

    def live(self, subject: str = "") -> list[dict]:
        """Open asks, oldest first (those no longer waiting where they were asked are closed)."""
        rows = self.store.all(
            "SELECT * FROM asks WHERE status='open' AND (?='' OR subject_id=?) "
            "ORDER BY created_at, id",
            (subject, subject),
        )
        found, gone = [], []
        updates = (
            {str(update["id"]) for update in self.runtime.owner_updates()}
            if any(row["channel"] == "notify" and row["ref"].isdigit() for row in rows)
            else set()
        )
        for row in rows:
            (found if self.alive(row, updates) else gone).append(row)
        for row in gone:
            self.store.execute(
                "UPDATE asks SET status='closed', settled_at=? WHERE id=? AND status='open'",
                (now(), row["id"]),
            )
        return found

    def drop(self, channel: str, ref: str, session_id: str = ""):
        """An ask withdrawn (its worker went on, its task ended): no longer open."""
        self.store.execute(
            "UPDATE asks SET status='withdrawn', settled_at=? WHERE channel=? AND ref=? "
            "AND status='open' AND (?='' OR session_id=?)",
            (now(), channel, ref, session_id, session_id),
        )

    def unjoin(self, session_id: str, text: str):
        """A note of the Router's joined to an ask, sent again as about something else: not
        that ask's."""
        self.store.execute(
            "UPDATE asks SET status='withdrawn', settled_at=? WHERE channel='notify' "
            "AND session_id=? AND text=? AND ref LIKE 'r:%' AND status='open'",
            (now(), session_id, clip(" ".join(text.split()), 2000)),
        )

    def choices(self, asks: list[dict]) -> list[str] | None:
        """The options asked on a subject (None: none of its asks is a choice)."""
        found: list[str] = []
        choice = False
        for ask in asks:
            if ask["options"]:
                choice = True
                found += json.loads(ask["options"])
        return list(dict.fromkeys(found)) if choice else None

    # Settling

    async def settle(
        self,
        channel: str,
        ref: str,
        answer: str,
        *,
        session_id: str = "",
        decision: dict | None = None,
        by: str = "owner",
        declined: bool = False,
        note: bool = False,
    ) -> list[dict]:
        """The owner answered an ask (or the Router did for them: `by`): every other open ask on
        its subject is answered with the same words where it was asked, and closes; each worker
        hears it once. A choice answered elsewhere than on a decision card is kept as the
        decision on the subject (`decision`: the one the answer made or kept; `note`: their words
        picked no option of a decision in force, which stays). `declined`: Not now. The asks
        answered so."""
        claimed = self.claim(channel, ref, answer, session_id)
        return await self.pass_on(
            claimed, answer, decision=decision, by=by, declined=declined, note=note
        )

    def claim(
        self, channel: str, ref: str, answer: str, session_id: str = ""
    ) -> tuple[list[dict], list[dict]]:
        """settle's first step, at once (before what was asked leaves where it was): the asks
        answered, and the others on their subjects, all settled now."""
        rows = self.store.all(
            "SELECT * FROM asks WHERE channel=? AND ref=? AND status='open' "
            "AND (?='' OR session_id=?)",
            (channel, ref, session_id, session_id),
        )
        if not rows:
            return [], []
        mine = {row["id"] for row in rows}
        others = [
            ask
            for subject in {row["subject_id"] for row in rows}
            for ask in self.live(subject)
            if ask["id"] not in mine
        ]
        stamp = now()
        for ask in [*rows, *others]:
            self.store.execute(
                "UPDATE asks SET status='settled', answer=?, settled_at=? WHERE id=?",
                (answer.strip()[:2000], stamp, ask["id"]),
            )
        return rows, others

    async def pass_on(
        self,
        claimed: tuple[list[dict], list[dict]],
        answer: str,
        *,
        decision: dict | None = None,
        by: str = "owner",
        declined: bool = False,
        note: bool = False,
    ) -> list[dict]:
        """settle's second step: the decision kept, the other asks answered."""
        rows, others = claimed
        if not rows:
            return []
        answer = answer.strip()
        channel, ref = rows[0]["channel"], rows[0]["ref"]
        subjects = {row["subject_id"] for row in rows}
        subject = self.subject(rows[0]["subject_id"])
        every = [*rows, *others]
        if (
            decision is None
            and by == "owner"
            and not declined
            and answer
            and channel != "decision"
            and (options := self.choices(every)) is not None
        ):
            decision, note = await self.keep_choice(subject, every, options, answer, channel, ref)
        if decision and not note and subject:
            for identifier in subjects:
                self.decide(identifier, decision["id"])
            # Every asker's task relies on it now.
            for ask in every:
                if ask["task_id"]:
                    self.store.execute(
                        "INSERT OR IGNORE INTO task_decisions(task_id, decision_id) VALUES (?,?)",
                        (ask["task_id"], decision["id"]),
                    )
        if not others:
            self.runtime.publish_state()
            return []
        told = {row["session_id"] for row in rows}
        if channel in {"question", "ask_user"} and by == "owner":
            # The Router heard this answer already (Asks.tell_router_answer).
            told.add(self.store.get("router_id") or "")
        asked_by = self.who(rows)
        for ask in others:
            try:
                await self.answer_ask(
                    ask, answer, decision, by, declined, note, told, asked_by, subject
                )
            except Exception:
                logger.warning("Could not pass the answer to ask %s on.", ask["id"], exc_info=True)
            told.add(ask["session_id"])
        self.runtime.publish_state()
        return others

    async def keep_choice(
        self,
        subject: dict | None,
        asks: list[dict],
        options: list[str],
        answer: str,
        channel: str,
        ref: str,
    ) -> tuple[dict | None, bool]:
        """The owner answered a choice outside a decision card: their pick, or their words on a
        first ask, is the decision on its subject (replacing the one in force); words that pick
        none of the options of a subject decided already are a note, the decision stays."""
        decisions = self.runtime.decisions
        kept = self.decided(subject)
        named = decisions.named({"options": [{"choice": o} for o in options]}, answer)
        if kept and not named:
            return kept, True
        question = clip(" ".join((subject or asks[0])["title" if subject else "text"].split()), 300)
        tasks = list(dict.fromkeys(ask["task_id"] for ask in asks if ask["task_id"]))
        try:
            recorded = await decisions.record(
                f"{question} \u2192 {named or answer}",
                tasks,
                [kept["id"]] if kept else [],
                f"ask:{channel}:{ref}",
            )
        except Exception:
            logger.warning("Could not keep the owner's answer as a decision.", exc_info=True)
            return None, False
        return recorded["decision"], False

    def who(self, asks: list[dict]) -> str:
        """Who asked, as the owner reads it: “Login API”, the Router."""
        names = []
        for ask in asks:
            row = self.store.find_session(ask["session_id"])
            if not row:
                continue
            name = "the Router" if row["role"] == ROUTER else f"“{self.runtime.display_title(row)}”"
            if name not in names:
                names.append(name)
        return " and ".join(names) or "another worker"

    async def answer_ask(
        self,
        ask: dict,
        answer: str,
        decision: dict | None,
        by: str,
        declined: bool,
        note: bool,
        told: set[str],
        asked_by: str,
        subject: dict | None,
    ):
        """One other ask on a settled subject, answered where it was asked."""
        runtime = self.runtime
        quiet = ask["session_id"] in told  # It heard the answer already: closed without a word.
        where = f"where {asked_by} asked the same"
        if declined:
            said = f"The owner did not decide this now ({where})" + (
                f": {answer}" if answer else "."
            )
        elif note and decision:
            said = (
                f"The owner picked none of the options ({where}): {answer}\nTheir decision stays "
                f"in force: {decision['text']}."
            )
        elif decision and by == "owner":
            said = f"The owner decided ({where}): {decision['text'].partition(' → ')[2] or answer}"
        elif by == "router":
            said = f"The Router answered for the owner ({where}): {answer}"
        else:
            said = f"The owner answered ({where}): {answer}"
        if ask["channel"] == "decision":
            card = self.store.draft(ask["ref"])
            if card["status"] != "pending":
                return
            short = answer if len(answer) <= 80 else answer[:79].rstrip() + "\u2026"
            status = "rejected" if declined else "answered"
            outcome = "Not decided" if declined else f"Answered {where}: {short}"
            card = runtime.decisions.close(card["id"], status, outcome)
            if not quiet:
                await runtime.decisions.tell_askers(
                    card,
                    said,
                    "Not decided" if declined else f"Decision: {short}",
                    decision["id"] if decision else "",
                    "Not decided by you" if declined else f"Decided: {short}",
                )
            return
        if ask["channel"] == "ask_user":
            draft = self.store.draft(ask["ref"])
            if draft["status"] != "pending":
                return
            action = (
                DraftDecision(action="reject", reason=answer)
                if declined
                else DraftDecision(action="answer", text=said if not quiet else answer)
            )
            with suppress(ValueError):  # No longer waited on: closed as such.
                runtime.asks.answer_request(draft, action, settle=False)
            return
        if ask["channel"] == "question":
            question = runtime.asks.owner_question(ask["ref"])
            if not question:
                return
            if declined or quiet or by == "router":
                runtime.asks.dismiss(ask["ref"], "answered")
                task = runtime.tasks.find(question.get("task_id") or "")
                if task and not quiet:
                    runtime.asks.go_on(task)
                if not quiet:
                    await runtime.delivery.deliver(
                        ask["session_id"],
                        wrap("router", "message", said, task=task["id"] if task else None),
                        kind="router",
                        display=clip(said, 200),
                        meta={"task_id": task["id"]} if task else None,
                        steer=True,
                    )
                return
            asked = clip(" ".join(question["text"].split()), 300)
            # The Router hears one answer once (not again for each question it closes).
            if (router := self.store.get("router_id") or "") in told:
                runtime.asks.router_heard.add(ask["ref"])
            told.add(router)
            runtime.delivery.hold(
                ask["session_id"],
                wrap(
                    "system",
                    "context",
                    f"The owner's next message answers your question “{asked}”: they answered "
                    f"it {where}.",
                ),
                kind="notice",
                display=f"Answered {where}",
            )
            await runtime.task_actions.answer_question(ask["ref"], answer, tell_router=False)
            return
        if ask["channel"] == "notify":
            if ask["ref"].isdigit():
                with suppress(LookupError, ValueError):
                    runtime.update_seen(int(ask["ref"]))
            if (router := self.store.get("router_id")) and subject and not quiet:
                runtime.delivery.hold(
                    router,
                    wrap(
                        "system",
                        "question-closed",
                        f"Your note to the owner about “{clip(subject['title'], 200)}” is "
                        f"answered: {said}",
                    ),
                    kind="notice",
                    display=f"Answered: {clip(subject['title'], 120)}",
                )

    # The owner's own word on sameness

    async def merge(self, identifier: str, into: str) -> dict:
        """The owner says two subjects are one (Merge on "possibly the same as"): the asks of
        one join the other's. Merged into one decided already, they are answered with that
        decision."""
        subject, other = self.subject(identifier), self.subject(into)
        if not subject or not other or subject["id"] == other["id"]:
            raise ValueError("Nothing to merge.")
        moved = self.live(subject["id"])
        self.store.execute(
            "UPDATE asks SET subject_id=? WHERE subject_id=?", (other["id"], subject["id"])
        )
        self.store.execute(
            "UPDATE subjects SET merged_into=?, updated_at=? WHERE id=?",
            (other["id"], now(), subject["id"]),
        )
        decision = self.decided(other)
        if decision and moved and not self.live_others(other["id"], moved):
            # The same as a choice decided already: that decision is its answer.
            value = decision["text"].partition(" \u2192 ")[2] or decision["text"]
            first = moved[0]
            await self.settle_merged(moved, value, decision)
            logger.info("Merged %s into decided %s (%s)", subject["id"], other["id"], first["id"])
        self.runtime.publish_state()
        return self.subject(other["id"]) or other

    def live_others(self, subject: str, moved: list[dict]) -> list[dict]:
        ids = {ask["id"] for ask in moved}
        return [ask for ask in self.live(subject) if ask["id"] not in ids]

    async def settle_merged(self, asks: list[dict], value: str, decision: dict):
        stamp = now()
        for ask in asks:
            self.store.execute(
                "UPDATE asks SET status='settled', answer=?, settled_at=? WHERE id=?",
                (value, stamp, ask["id"]),
            )
            if ask["task_id"]:
                self.store.execute(
                    "INSERT OR IGNORE INTO task_decisions(task_id, decision_id) VALUES (?,?)",
                    (ask["task_id"], decision["id"]),
                )
        told: set[str] = set()
        for ask in asks:
            try:
                await self.answer_ask(
                    ask, value, decision, "owner", False, False, told, "you decided before",
                    None,
                )  # fmt: skip
            except Exception:
                logger.warning("Could not answer ask %s.", ask["id"], exc_info=True)
            told.add(ask["session_id"])

    def split(self, ask_id: str) -> dict:
        """The owner says an ask is not the same as the others on its subject: a subject of its
        own."""
        ask = self.store.one("SELECT * FROM asks WHERE id=?", (ask_id,))
        if not ask:
            raise ValueError("No such ask.")
        stamp = now()
        identifier = subject_id()
        self.store.execute(
            """INSERT INTO subjects(id, anchor, title, created_at, updated_at)
            VALUES (?,?,?,?,?)""",
            (
                identifier,
                self.anchor(ask["session_id"], ask["task_id"]),
                clip(ask["text"], 300),
                stamp,
                stamp,
            ),
        )
        # Kept apart: never joined to the other one again by itself.
        self.store.execute(
            "UPDATE asks SET subject_id=? WHERE channel=? AND ref=? AND session_id=?",
            (identifier, ask["channel"], ask["ref"], ask["session_id"]),
        )
        self.store.set(f"apart:{identifier}", ask["subject_id"])
        self.runtime.publish_state()
        return self.subject(identifier) or {}

    # Views

    @staticmethod
    def keys(ask: dict) -> list[str]:
        """Its item under Home's Needs you, as one of these: a question its task's row shows,
        or by itself."""
        channel, ref = ask["channel"], ask["ref"]
        if channel in {"decision", "ask_user"}:
            return [f"draft:{ref}"]
        if channel == "question":
            return [f"task:{ask['task_id']}"] * bool(ask["task_id"]) + [f"question:{ref}"]
        if channel == "notify" and ref.isdigit():
            return [f"update:{ref}"]
        return []

    def folded(self, present: set[str]) -> dict[str, dict]:
        """One item for each subject asked more than once (E158): the items under Needs you
        (`present`, by key) folded into another's, by key, with the subject they are in; the
        one shown first: a decision card, a question card, a question, a note of the Router's,
        the oldest of a kind."""
        found: dict[str, dict] = {}
        for subject in self.view():
            items = []
            for ask in sorted(subject["asks"], key=lambda a: CHANNELS.index(a["channel"])):
                key = next((k for k in ask["keys"] if k in present), "")
                if key and key not in items:
                    items.append(key)
            for key in items[1:]:
                found[key] = {"subject": subject["id"], "into": items[0]}
        return found

    def ask_view(self, ask: dict) -> dict:
        row = self.store.find_session(ask["session_id"])
        task = self.runtime.tasks.find(ask["task_id"]) if ask["task_id"] else None
        router = bool(row and row["role"] == ROUTER)
        return {
            "id": ask["id"],
            "channel": ask["channel"],
            "keys": self.keys(ask),
            "router": router,
            "worker": "the Router" if router else (self.runtime.display_title(row) if row else ""),
            "session_id": ask["session_id"],
            "task_id": task["id"] if task else "",
            "task_title": task["title"] if task else "",
            "text": self.runtime.drafts.owner_text(ask["text"]),
            "at": ask["created_at"],
        }

    def asked_by(self, asks: list[dict]) -> str:
        """ "asked by 2 workers + the Router"."""
        workers = {a["session_id"] for a in asks if not a.get("router")}
        router = any(a.get("router") for a in asks)
        parts = []
        if workers:
            parts.append(f"{len(workers)} worker{'s' if len(workers) > 1 else ''}")
        if router:
            parts.append("the Router")
        return "asked by " + " + ".join(parts)

    def view(self) -> list[dict]:
        """Subjects with an open ask, for Home's Needs you: those asked more than once (one
        card), decided before, or possibly the same as another (Merge)."""
        live = self.live()
        if not live:
            return []
        grouped: dict[str, list[dict]] = {}
        for ask in live:
            subject = self.subject(ask["subject_id"])
            if subject:
                grouped.setdefault(subject["id"], []).append(ask)
        subjects = {key: self.subject(key) for key in grouped}
        decided = [
            subject
            for subject in self.store.all(
                """SELECT * FROM subjects WHERE merged_into='' AND decision_id IN
                (SELECT id FROM decisions WHERE status='active') ORDER BY updated_at DESC
                LIMIT ?""",
                (COMPARED,),
            )
            if subject["id"] not in grouped
        ]
        shown = []
        said: dict[str, list[str]] = {}  # Each subject's words, read once.
        for key, asks in list(grouped.items())[:COMPARED]:
            subject = subjects[key]
            if not subject:
                continue
            decision = self.decided(subject) or self.last_decision(subject)
            others = [subjects[k] for k in grouped if k != key]
            possibly = self.possibly(subject, asks, [*others, *decided], said)
            if len(asks) < 2 and not decision and not possibly:
                continue
            views = [self.ask_view(ask) for ask in asks]
            shown.append(
                {
                    "id": subject["id"],
                    "title": self.runtime.drafts.owner_text(subject["title"]),
                    "asks": views,
                    "asked_by": self.asked_by(views),
                    **(
                        {
                            "decided": {
                                "id": decision["id"],
                                "text": decision["text"],
                                "value": decision["text"].partition(" \u2192 ")[2]
                                or decision["text"],
                                "at": decision["created_at"],
                                "in_force": decision["status"] == "active",
                            }
                        }
                        if decision
                        else {}
                    ),
                    **({"possibly": possibly} if possibly else {}),
                }
            )
        return shown

    def last_decision(self, subject: dict) -> dict | None:
        if not subject["decision_id"]:
            return None
        try:
            return self.runtime.decisions.current(subject["decision_id"])
        except ValueError:
            return None

    def possibly(
        self,
        subject: dict,
        asks: list[dict],
        others: Sequence[dict | None],
        said: dict[str, list[str]] | None = None,
    ) -> list[dict]:
        """Other subjects this one is possibly the same as: the same words across requests, or
        some of them. Never one the owner kept apart."""
        found = []
        apart = {self.store.get(f"apart:{subject['id']}") or ""}
        texts = [subject["title"], *(ask["text"] for ask in asks)]
        for other in others:
            if (
                not other
                or other["id"] in apart
                or self.store.get(f"apart:{other['id']}") == (subject["id"])
            ):
                continue
            if said is None or other["id"] not in said:
                theirs = self.texts(other)
                if said is not None:
                    said[other["id"]] = theirs
            else:
                theirs = said[other["id"]]
            levels = [likeness(one, two)[0] for one in texts for two in theirs]
            if not any(levels):
                continue
            decision = self.decided(other)
            found.append(
                {
                    "id": other["id"],
                    "title": self.runtime.drafts.owner_text(other["title"]),
                    **(
                        {"decided": decision["text"].partition(" \u2192 ")[2] or decision["text"]}
                        if decision
                        else {}
                    ),
                }
            )
        return found[:3]

    def router_lines(self) -> list[str]:
        """Open asks by subject, for the Router: only those asked more than once."""
        lines = []
        for subject in self.view():
            if len(subject["asks"]) < 2:
                continue
            names = ", ".join(
                dict.fromkeys(
                    ask["worker"] + (f" ({ask['task_id']})" if ask["task_id"] else "")
                    for ask in subject["asks"]
                )
            )
            lines.append(
                f"- “{clip(subject['title'], 160)}”: {subject['asked_by']} ({names}); one "
                "answer reaches them all"
            )
        return lines

    # Workers keep up

    def in_force(self, session_id: str, text: str) -> tuple[str, list[str]]:
        """Decisions in force for a worker's open tasks that it was not given yet (nor is in
        `text`, the prompt it gets now): a short line, with their ids ("" when none)."""
        rows = self.store.all(
            """SELECT d.id, d.text, l.task_id FROM task_decisions l
            JOIN decisions d ON d.id=l.decision_id JOIN tasks t ON t.id=l.task_id
            WHERE t.session_id=? AND d.status='active' AND t.status IN ({})
            ORDER BY d.created_at""".format(",".join("?" * len(OPEN))),  # noqa: S608
            (session_id, *sorted(OPEN)),
        )
        if not rows:
            return "", []
        given = set(json.loads(self.store.get(f"dgiven:{session_id}") or "[]"))
        fresh: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["id"] in given:
                continue
            fresh.setdefault(row["id"], {"text": row["text"], "tasks": []})["tasks"].append(
                row["task_id"]
            )
        mentioned = [identifier for identifier in fresh if identifier in text]
        listed = [(k, v) for k, v in fresh.items() if k not in mentioned][-5:]
        if not listed:
            return "", mentioned
        lines = "\n".join(
            f"- {key} ({', '.join(value['tasks'])}): {clip(value['text'], 240)}"
            for key, value in listed
        )
        line = wrap(
            "system",
            "context",
            f"Decisions in force for your tasks (work to them; to question one, ask_decision "
            f"with replaces=its id):\n{lines}",
        )
        return line, [key for key, _ in listed] + mentioned

    def given(self, session_id: str, identifiers: list[str]):
        if not identifiers:
            return
        key = f"dgiven:{session_id}"
        seen = json.loads(self.store.get(key) or "[]")
        self.store.set(key, json.dumps(list(dict.fromkeys([*seen, *identifiers]))[-200:]))

    # Migration

    def migrate(self):
        """Once: every decision in force gets a subject, and every open decision card and
        question is filed under its own."""
        if self.store.get("migrated:ledger"):
            return
        stamp = now()
        for decision in self.store.all("SELECT * FROM decisions WHERE status='active'"):
            if self.store.one("SELECT id FROM subjects WHERE decision_id=?", (decision["id"],)):
                continue
            tasks = self.runtime.decisions.linked(decision["id"])
            task = self.runtime.tasks.find(tasks[0]) if tasks else None
            anchor = self.anchor(task["session_id"], task["id"]) if task else ""
            self.store.execute(
                """INSERT INTO subjects(id, anchor, title, decision_id, created_at, updated_at)
                VALUES (?,?,?,?,?,?)""",
                (
                    subject_id(),
                    anchor,
                    clip(decision["text"].partition(" \u2192 ")[0], 300),
                    decision["id"],
                    decision["created_at"],
                    stamp,
                ),
            )
        for card in self.store.drafts():
            if card["kind"] == "decision":
                target = card["target"]
                options = [o["choice"] for o in target.get("options") or []]
                for asker in target.get("askers") or []:
                    self.file(
                        "decision",
                        card["id"],
                        asker["session_id"],
                        asker.get("task_id") or "",
                        target["question"],
                        options=options,
                    )
        for key in self.store.keys("ownerq:"):
            question = json.loads(self.store.get(key) or "null")
            if not question:
                continue
            ref = key.removeprefix("ownerq:")
            self.file(
                "question",
                ref,
                ref.split(":", 1)[0],
                question.get("task_id") or "",
                question.get("text") or "",
                options=question.get("options") or None,
            )
        self.store.set("migrated:ledger", "1")
