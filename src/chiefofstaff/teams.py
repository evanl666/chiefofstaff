"""Teams (E129, E130): the workers that work together, as the owner and the Router see them.

A team is a connected group of workers linked as working together (Peers.link: `works_with`,
or a worker saying it waits for another) or that told or asked each other something
(Peers.talked). Nothing is stored for it: it is read from those links, named after its workers,
and its thread is what its workers told and asked each other (their `tell` messages and the
`questions` between them), a question paired with its answer.

The owner sees a team as one line on Home and one thread; the Router gets a short digest of who
told or asked whom with its next turn, never the words (it reads a thread when it must:
read_team_thread), so it never passes on a value one worker hands another.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from .envelope import short, wrap
from .peers import clip
from .roles import ROUTER, WORKER
from .tasks import OPEN

if TYPE_CHECKING:
    from .runtime import Runtime

LABEL_CHARS = 24
THREAD_ITEMS = 200
THREAD_CHARS = 2000
# A question between workers holding its asker's task up this long comes under Needs you, a line
# per team; Nudge tells the one asked, at most once this often.
BLOCKING_MINUTES = 30
NUDGE_EVERY = 30 * 60


class ReadTeamThread(BaseModel):
    team: str = Field(
        description="The team: its id (team-…, from a team-digest), or one of its workers "
        "(worker:<id>) or tasks (t…)"
    )
    last: int = Field(default=20, ge=1, le=60, description="How many of its latest exchanges")


def short_label(title: str) -> str:
    """A worker's short name from a title: the part before ":", "·", "(" or " - ", cut at a
    word to 24 characters (as static/team.js does)."""
    whole = " ".join(title.split())
    head = re.split(r"\s*[:·|(]\s*|\s+[-\u2013\u2014]\s+", whole, maxsplit=1)[0].strip() or whole
    if len(head) <= LABEL_CHARS:
        return head
    cut = head[: LABEL_CHARS + 1]
    at = cut.rfind(" ")
    return (cut[:at] if at >= 8 else head[:LABEL_CHARS]).rstrip(" ,;.")


def team_name(labels: list[str]) -> str:
    if len(labels) <= 2:
        return " & ".join(labels)
    return f"{labels[0]}, {labels[1]} +{len(labels) - 2}"


def iso(seconds: float | None) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat() if seconds else ""


def minutes_between(start: str, end: str = "") -> int:
    began = datetime.fromisoformat(start)
    ended = datetime.fromisoformat(end) if end else datetime.now(UTC)
    return max(0, round((ended - began).total_seconds() / 60))


class Teams:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.tasks = runtime.tasks
        self.cache: tuple[dict, list[list[str]]] = ({}, [])

    def components(self) -> list[list[str]]:
        """The teams' members (2 or more), each oldest first; the oldest team first. Read again
        only when the links changed."""
        links = {
            **{f"peers:{key}": value for key, value in self.store.items("peers:").items()},
            **{f"talks:{key}": value for key, value in self.store.items("talks:").items()},
        }
        if links == self.cache[0]:
            return self.cache[1]
        parent: dict[str, str] = {}

        def root(node: str) -> str:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for key, value in links.items():
            one = key.split(":", 1)[1]
            for other in json.loads(value or "[]"):
                parent[root(one)] = root(other)
        groups: dict[str, list[str]] = {}
        for node in parent:
            groups.setdefault(root(node), []).append(node)
        ids = list(parent)
        made = {
            row["id"]: row["created_at"]
            for row in self.store.all(
                f"SELECT id, created_at FROM sessions WHERE id IN ({','.join('?' * len(ids))})",  # noqa: S608  (placeholders only)
                tuple(ids),
            )
        } if ids else {}  # fmt: skip
        teams = [
            sorted((m for m in members if m in made), key=lambda m: (made[m], m))
            for members in groups.values()
        ]
        teams = sorted((t for t in teams if len(t) >= 2), key=lambda t: (made[t[0]], t[0]))
        self.cache = (links, teams)
        return teams

    @staticmethod
    def team_id(members: list[str]) -> str:
        return f"team-{short(members[0])}"

    def members_of(self, session_id: str) -> list[str]:
        return next((members for members in self.components() if session_id in members), [])

    def team_of(self, session_id: str) -> str:
        """Its team's id ("" when it works with nobody)."""
        members = self.members_of(session_id)
        return self.team_id(members) if members else ""

    def find(self, reference: str) -> list[str]:
        """A team's members, by its id, one of its workers (worker:<id>, a short id) or one of
        their tasks."""
        reference = " ".join(reference.split()).removeprefix("worker:")
        for members in self.components():
            if reference == self.team_id(members) or any(
                member == reference or (len(reference) >= 8 and member.startswith(reference))
                for member in members
            ):
                return members
        task = self.tasks.find(reference) if reference else None
        return self.members_of(task["session_id"]) if task else []

    def label(self, row: dict) -> str:
        """A worker's short name: the owner's, else from its first task's title."""
        if owned := self.store.get(f"label:{row['id']}"):
            return owned
        first = self.store.one(
            "SELECT title FROM tasks WHERE session_id=? ORDER BY created_at LIMIT 1", (row["id"],)
        )
        return short_label(first["title"] if first else row["title"]) or row["title"][:24]

    def set_label(self, session_id: str, text: str) -> str:
        """The owner's short name for a worker ("" gives it back its own)."""
        row = self.store.session(session_id)
        if row["role"] != WORKER:
            raise ValueError("Only a worker has a short name.")
        text = " ".join(text.split())[:LABEL_CHARS].strip()
        if text:
            self.store.set(f"label:{session_id}", text)
        else:
            self.store.delete(f"label:{session_id}")
        self.runtime.publish_state()
        return self.label(row)

    def labels(self, members: list[str]) -> dict[str, str]:
        found = {}
        for member in members:
            row = self.store.find_session(member)
            found[member] = self.label(row) if row else "a worker"
        return found

    def blocks(self, question: dict) -> bool:
        """Whether an open question holds its asker up: the asker is not at work and its task
        is open (it waits on the answer)."""
        asker = self.store.find_session(question["asker"])
        task = self.tasks.current(question["asker"]) if asker else None
        return bool(
            asker
            and not asker["closed_at"]
            and asker["status"] != "working"
            and task
            and task["status"] in {"running", "waiting"}
        )

    def waiting(self, members: list[str]) -> list[dict]:
        """Its open questions, oldest first: who waits on whom since when, and whether it holds
        the asker's task up."""
        return [
            {
                **question,
                "minutes": minutes_between(question["asked"]),
                "blocks": self.blocks(question),
                "nudged": iso(float(self.store.get(f"nudged:{question['id']}") or 0)),
            }
            for question in self.runtime.asks.peer_waiting()
            if question["asker"] in members and question["target"] in members
        ]

    def held_up(self) -> list[tuple[dict, dict]]:
        """Teams with a question holding its asker's task up BLOCKING_MINUTES or more, each with
        the one waiting longest (Needs you: a line per team)."""
        found = []
        for team in self.view():
            held = [
                question
                for question in team["waiting"]
                if question["blocks"] and question["minutes"] >= BLOCKING_MINUTES
            ]
            if held:
                found.append((team, max(held, key=lambda question: question["minutes"])))
        return found

    async def nudge(self, team_id: str, question_id: str) -> str:
        """The owner's Nudge: the worker asked is told its teammate waits for the answer."""
        members = self.find(team_id)
        question = self.runtime.asks.question(question_id)
        if not question or question["asker"] not in members or question["target"] not in members:
            raise LookupError("No such question in that team.")
        if question["status"] != "open":
            raise ValueError(f"That question is {question['status']} already.")
        last = float(self.store.get(f"nudged:{question_id}") or 0)
        if time.time() - last < NUDGE_EVERY:
            again = round((NUDGE_EVERY - (time.time() - last)) / 60)
            raise ValueError(f"Nudged already: again in {again} min if it still waits.")
        target = self.store.find_session(question["target"])
        if not target or target["closed_at"]:
            raise ValueError("That worker is closed: its questions end with it.")
        labels = self.labels(members)
        asker = labels[question["asker"]]
        waited = minutes_between(iso(question["asked"]))
        text = (
            f"“{asker}” (worker:{short(question['asker'])}) has waited {waited} min for your "
            f"answer to its question {question_id}: {clip(question['text'], 600)}\nAnswer it now "
            f'with answer_session(question_id="{question_id}"), in a line if that is enough, '
            "then go on with your task."
        )
        current = self.tasks.current(target["id"])
        await self.runtime.delivery.deliver(
            target["id"],
            wrap("system", "peer-nudge", text, question=question_id),
            kind="notice",
            display=f"Nudged by the owner: “{asker}” waits for your answer ({waited} min).",
            meta={"nudge": question_id, "session_id": question["asker"]},
            steer=bool(current and current["status"] in OPEN),
        )
        self.store.set(f"nudged:{question_id}", str(time.time()))
        self.runtime.publish_state()
        return f"Nudged “{labels[question['target']]}”: “{asker}” waits for its answer."

    def counts(self, members: list[str]) -> dict:
        marks = ",".join("?" * len(members))
        told = self.store.one(
            f"""SELECT count(*) AS n, max(created_at) AS last FROM messages WHERE kind='tell'
            AND session_id IN ({marks}) AND json_extract(meta, '$.target') IN ({marks})""",  # noqa: S608  (placeholders only)
            (*members, *members),
        )
        asked = self.store.one(
            f"""SELECT count(*) AS n, sum(status='answered') AS answered, max(asked) AS last
            FROM questions WHERE asker IN ({marks}) AND target IN ({marks})""",  # noqa: S608  (placeholders only)
            (*members, *members),
        )
        last = max(filter(None, [told["last"] if told else "", iso(asked["last"] if asked else 0)]),
                   default="")  # fmt: skip
        return {
            "told": told["n"] if told else 0,
            "asked": asked["n"] if asked else 0,
            "answered": (asked["answered"] or 0) if asked else 0,
            "last_at": last,
        }

    def view(self) -> list[dict]:
        """The teams with a worker still open, as the owner sees them; the latest active first."""
        found = []
        for members in self.components():
            rows = [self.store.find_session(member) for member in members]
            open_ = [row["id"] for row in rows if row and not row["closed_at"]]
            if not open_:
                continue
            labels = self.labels(members)
            found.append(
                {
                    "id": self.team_id(members),
                    "name": team_name([labels[member] for member in members]),
                    "members": members,
                    "open": open_,
                    "labels": labels,
                    "waiting": self.waiting(members),
                    **self.counts(members),
                }
            )
        return sorted(found, key=lambda team: team["last_at"], reverse=True)

    def thread(self, members: list[str], limit: int = THREAD_ITEMS) -> list[dict]:
        """What its workers told and asked each other, oldest first: a question with its answer
        (or how long it has waited), a tell on its own."""
        marks = ",".join("?" * len(members))
        items = []
        for message in self.store.all(
            f"""SELECT id, session_id, text, meta, created_at FROM messages WHERE kind='tell'
            AND session_id IN ({marks}) ORDER BY id DESC LIMIT ?""",  # noqa: S608  (placeholders only)
            (*members, limit),
        ):
            target = json.loads(message["meta"] or "{}").get("target", "")
            if target in members:
                items.append(
                    {
                        "kind": "tell",
                        "id": str(message["id"]),
                        "from": message["session_id"],
                        "to": target,
                        "text": message["text"],
                        "at": message["created_at"],
                    }
                )
        for question in self.store.all(
            f"""SELECT * FROM questions WHERE asker IN ({marks}) AND target IN ({marks})
            ORDER BY asked DESC LIMIT ?""",  # noqa: S608  (placeholders only)
            (*members, *members, limit),
        ):
            asked = iso(question["asked"])
            answered = iso(question["closed"]) if question["status"] == "answered" else ""
            items.append(
                {
                    "kind": "ask",
                    "id": question["id"],
                    "from": question["asker"],
                    "to": question["target"],
                    "text": question["text"],
                    "at": asked,
                    "status": question["status"],
                    "answer": question["answer"],
                    "answered_at": answered,
                    "minutes": minutes_between(asked, answered)
                    if question["status"] in {"open", "answered"}
                    else None,
                    **({"blocks": self.blocks(question)} if question["status"] == "open" else {}),
                }
            )
        items.sort(key=lambda item: item["at"])
        return items[-limit:]

    def thread_view(self, team_id: str) -> dict:
        members = self.find(team_id)
        if not members:
            raise LookupError("No such team.")
        team = next((t for t in self.view() if t["id"] == self.team_id(members)), None)
        if team is None:  # Every worker of it closed: its thread all the same.
            labels = self.labels(members)
            team = {
                "id": self.team_id(members),
                "name": team_name([labels[member] for member in members]),
                "members": members,
                "open": [],
                "labels": labels,
                "waiting": [],
                **self.counts(members),
            }
        return {"team": team, "items": self.thread(members)}

    def thread_text(self, members: list[str], last: int) -> str:
        """A team's latest exchanges in words, for the Router."""
        labels = self.labels(members)

        def who(session_id: str) -> str:
            return f"“{labels.get(session_id, 'a worker')}” (worker:{short(session_id)})"

        lines = []
        for item in self.thread(members, last):
            text = clip(item["text"], THREAD_CHARS)
            if item["kind"] == "tell":
                lines.append(
                    f"- {item['at'][:16]} {who(item['from'])} told {who(item['to'])}: {text}"
                )
                continue
            asked = f"{who(item['from'])} asked {who(item['to'])} ({item['id']})"
            line = f"- {item['at'][:16]} {asked}: {text}"
            if item["status"] == "answered":
                answer = clip(item["answer"], THREAD_CHARS)
                line += f"\n  Answered after {item['minutes']} min: {answer}"
            elif item["status"] == "open":
                line += f"\n  Not answered yet ({item['minutes']} min)."
            else:
                line += f"\n  {item['status'].capitalize()}, not answered."
            lines.append(line)
        return "\n".join(lines) or "Nothing told or asked between them yet."

    async def t_read_team_thread(self, params: ReadTeamThread, caller: str):
        self.runtime.toolset.caller(caller, ROUTER)
        members = self.find(params.team)
        if not members:
            raise ValueError(
                "No such team: pass a team id from a team-digest, or one of its workers or tasks."
            )
        labels = self.labels(members)
        names = ", ".join(f"“{labels[m]}” (worker:{short(m)})" for m in members)
        return (
            f"Team {self.team_id(members)}: {names}.\n{self.thread_text(members, params.last)}\n"
            "What one of them told another is theirs to use: never pass it on yourself."
        )

    def digest(self) -> tuple[str, int]:
        """What the Router's next turn gets instead of each message (E129): per team with news
        since its last digest, who told, asked or answered whom how often and what still waits
        (never the words); and the last Router row it covers."""
        router = self.runtime.router_id()
        mark = int(self.store.get("teamdigest:upto") or 0)
        rows = self.store.all(
            # (Rows of an earlier version ride along themselves: they carry their prompt.)
            "SELECT id, meta FROM messages WHERE session_id=? AND kind='peer' AND id>? "
            "AND json_type(meta, '$.prompt') IS NULL ORDER BY id",
            (router, mark),
        ) if router else []  # fmt: skip
        if not rows:
            return "", mark
        news: dict[str, dict[str, int]] = {}
        for row in rows:
            meta = json.loads(row["meta"] or "{}")
            members = self.members_of(meta.get("peer_from", ""))
            if members:
                kinds = news.setdefault(self.team_id(members), {})
                kind = meta.get("peer_kind") or "told"
                kinds[kind] = kinds.get(kind, 0) + 1
        lines = []
        for members in self.components():
            team = self.team_id(members)
            if team not in news:
                continue
            labels = self.labels(members)
            names = ", ".join(f"worker:{short(m)} “{labels[m]}”" for m in members)
            counts = ", ".join(f"{n} {kind}" for kind, n in sorted(news[team].items()))
            line = f"- {team} ({names}): {counts}."
            for question in self.waiting(members):
                line += (
                    f" Open: “{labels[question['asker']]}” waits on “{labels[question['target']]}”"
                    f" for {question['minutes']} min ({question['id']})"
                    + ("; its task waits on it." if question["blocks"] else ".")
                )
            lines.append(line)
        text = ""
        if lines:
            text = wrap(
                "system",
                "team-digest",
                "Workers that work together, since your last turn (who, not what; "
                "read_team_thread when the owner asks about it or a question there holds a task "
                "up):\n" + "\n".join(lines),
            )
        return text, rows[-1]["id"]
