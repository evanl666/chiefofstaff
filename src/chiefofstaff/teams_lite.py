"""Teams lite (docs teams-v2.md v3.1): what the owner sees of the work several workers do for one
of their requests. Nothing here changes what workers or the Router receive: their links,
delivery, introductions, waits, hand-overs, Compare, the Router's team digest and
read_team_thread are teams.py's, as before.

A few passive stamps are written as things happen: `req:<task>` and `reqtasks:<message>` (the
owner request a task was made for), `with:<task>` (the tasks of its request it was told to work
with), `from_task`/`to_task` in an exchange's message and `xchg:<task>` (the exchanges of a
task), `teamseen:<team>` (the owner's look at a done team). A team is computed from them on
every read: the tasks of one request joined by a link the Router made on purpose (works_with by
name, after, a replacement), on two or more workers. The server returns candidates; the page
says where each stands from Home's own board (static/team.js), the one lifecycle authority.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .peers import same as same_words
from .tasks import OPEN
from .team_names import avatars, goal_name, message_title, part_names
from .teams import BLOCKING_MINUTES, iso, minutes_between

if TYPE_CHECKING:
    from .runtime import Runtime

# An owner message to the Router, as its turn receives it: its ref says which row it is.
OWNER_REF = re.compile(r'^<message from="owner" type="message" ref="(o[0-9a-f]{6})"', re.MULTILINE)
# A turn's owner messages are among the Router's latest ones: this many are looked at.
RECENT_OWNER = 50
# Exchanges kept per task (xchg:<task>): its latest.
EXCHANGES_KEPT = 500
# A team is current (the snapshot's) while a task of its request is open, listed on Home, or
# changed this recently; older ones come a page at a time (Past teams).
WINDOW_DAYS = 7
PAST_PAGE = 20
PAST_READ = 200
HISTORY_PAGE = 50
# Every request's teams are read again this often (their tasks' changes invalidate them).
RELOAD_SECONDS = 3600
CHUNK = 400
# Part colors by the order parts joined: neighbours apart, red last.
PALETTE = ("c0", "c3", "c5", "c2", "c6", "c7", "c1", "c4")
TEAM_ID = re.compile(r"^team-(\d+)-(t[0-9a-f]{7})$")
SEEN_KEY = re.compile(r"^[0-9a-z]{1,32}$")
LINK_KEYS = ("with:", "replacedby:", "supersededby:", "contextfrom:")


def loads(text: str | None, default: Any) -> Any:
    try:
        found = json.loads(text or "")
    except ValueError:
        return default
    return found if isinstance(found, type(default)) else default


@dataclass
class Request:
    """One owner request's tasks and the teams they make, as read last."""

    id: str
    tasks: list[str]
    teams: list[dict] = field(default_factory=list)
    latest: str = ""
    open: bool = False


class TeamsLite:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.tasks = runtime.tasks
        # Questions asked since the start: the tasks they went between (else read from their
        # ask message, through the asker's tasks' exchanges).
        self.asked: dict[str, dict[str, str]] = {}
        # Every request's teams by its id, read once and again only once a task of it changed.
        self.requests: dict[str, Request] = {}
        self.dirty: set[str] = set()
        self.loaded = 0.0
        self.memo: tuple[tuple, tuple[list[dict], int]] | None = None

    # Stamps, as things happen (passive: never read back to change what is done).

    def turn_request(self, router: str, content: str) -> str:
        """The owner request a Router turn answers: the first of the owner's messages it
        received (their row id), by the ref each carries; "" when it received none of theirs
        (an event, a turn resumed after a restart: their words are only quoted then)."""
        refs = set(OWNER_REF.findall(content))
        if not refs:
            return ""
        rows = self.store.all(
            "SELECT id, meta FROM messages WHERE session_id=? AND kind='owner' "
            "ORDER BY id DESC LIMIT ?",
            (router, RECENT_OWNER),
        )
        found = [row["id"] for row in rows if loads(row["meta"], {}).get("ref") in refs]
        return str(min(found)) if found else ""

    def record_with(self, task_id: str, request: str, references: list[str]):
        """`with:<task>`: the tasks of its own request a new task was told to work with, by id,
        by its worker or by exact title, as they are now (never one made later, nor a worker's
        `wants:` or candidates: those link workers as before, and make no team)."""
        mine = [
            task for task in loads(self.store.get(f"reqtasks:{request}"), []) if task != task_id
        ]
        if not mine:
            return
        rows = self.rows("SELECT id, title FROM tasks WHERE id IN ({})", mine)
        found = []
        for reference in references:
            reference = " ".join(reference.split()).removeprefix("worker:")
            if not reference:
                continue
            try:
                named = self.runtime.task_actions.task_for(reference)
            except ValueError:
                named = None
            if named:
                found += [named["id"]] if named["id"] in mine else []
                continue
            found += [row["id"] for row in rows if same_words(reference, row["title"])]
        if found := list(dict.fromkeys(found)):
            self.store.set(f"with:{task_id}", json.dumps(found))
            self.dirty.add(request)

    def recipient_task(self, target: str, from_task: str) -> str:
        """The task an exchange reaches when that is plain: the one its recipient is at work
        on when that is of the sender's request, else its only open task, else none."""
        request = self.store.get(f"req:{from_task}") if from_task else ""
        at = self.runtime.at_work(target)
        if at and request and self.store.get(f"req:{at['id']}") == request:
            return at["id"]
        open_ = [task for task in self.tasks.for_session(target) if task["status"] in OPEN]
        return open_[0]["id"] if len(open_) == 1 else ""

    def exchange_tasks(self, sender: str, target: str) -> dict[str, str]:
        """`from_task`/`to_task` for an exchange sent now (each only when known)."""
        at = self.runtime.at_work(sender)
        from_task = at["id"] if at else ""
        to_task = self.recipient_task(target, from_task)
        return {
            key: value for key, value in (("from_task", from_task), ("to_task", to_task)) if value
        }

    def exchanged(self, message_id: int, stamped: dict[str, str], question: str = ""):
        """An exchange's message, listed for its sender's and its recipient's tasks
        (`xchg:<task>`, their latest EXCHANGES_KEPT): a team's history reads these by key."""
        if question:
            self.asked[question] = stamped
        for task in dict.fromkeys(stamped.values()):
            known = loads(self.store.get(f"xchg:{task}"), [])
            self.store.set(f"xchg:{task}", json.dumps([*known, message_id][-EXCHANGES_KEPT:]))

    def task_changed(self, task: dict):
        """A task was made or changed: its request's teams are read again."""
        if request := self.store.get(f"req:{task['id']}"):
            self.dirty.add(request)

    # Reading, by key or by an index only.

    def rows(self, sql: str, ids: list, extra: tuple = ()) -> list[dict]:
        found: list[dict] = []
        ids = list(dict.fromkeys(ids))
        for start in range(0, len(ids), CHUNK):
            part = ids[start : start + CHUNK]
            found += self.store.all(sql.format(",".join("?" * len(part))), (*part, *extra))
        return found

    def kv(self, keys: list[str]) -> dict[str, str]:
        return {
            row["key"]: row["value"]
            for row in self.rows("SELECT key, value FROM kv WHERE key IN ({})", keys)
        }

    def read(self, made: dict[str, list[str]]) -> dict[str, Request]:
        """Requests' teams from their tasks (`reqtasks:`): a fixed number of reads by key."""
        ids = [task for tasks in made.values() for task in tasks]
        tasks = {
            row["id"]: row
            for row in self.rows(
                "SELECT id, session_id, title, status, created_at, updated_at, ended_at "
                "FROM tasks WHERE id IN ({})",
                ids,
            )
        }
        found = {}
        linked = []
        for request, listed in made.items():
            mine = [task for task in listed if task in tasks]
            latest = max(
                (tasks[t]["ended_at"] or tasks[t]["updated_at"] or "" for t in mine), default=""
            )
            busy = any(tasks[t]["status"] in {*OPEN, "review"} for t in mine)
            found[request] = Request(request, mine, latest=latest, open=busy)
            if len(mine) >= 2:
                linked += mine
        if not linked:
            return found
        links = self.kv([prefix + task for task in linked for prefix in LINK_KEYS])
        deps = self.rows(
            "SELECT task_id, depends_on, needs FROM task_deps WHERE task_id IN ({})", linked
        )
        # Where a part went on outside its request: the task it continued in.
        targets = [
            target
            for task in linked
            if (target := links.get(f"replacedby:{task}") or links.get(f"supersededby:{task}"))
            and target not in tasks
        ]
        if targets:
            for row in self.rows(
                "SELECT id, session_id, title, status, created_at, updated_at, ended_at "
                "FROM tasks WHERE id IN ({})",
                targets,
            ):
                tasks[row["id"]] = row
        messages = {
            str(row["id"]): row
            for row in self.rows(
                "SELECT id, text, created_at FROM messages WHERE id IN ({})",
                [int(request) for request, entry in found.items() if len(entry.tasks) >= 2],
            )
        }
        by_task: dict[str, list[dict]] = {}
        for dep in deps:
            by_task.setdefault(dep["task_id"], []).append(dep)
        for entry in found.values():
            if len(entry.tasks) >= 2:
                entry.teams = self.teams_of(entry, tasks, links, by_task, messages.get(entry.id))
        return found

    def teams_of(
        self,
        entry: Request,
        tasks: dict[str, dict],
        links: dict[str, str],
        deps: dict[str, list[dict]],
        message: dict | None,
    ) -> list[dict]:
        inside = set(entry.tasks)
        parent = {task: task for task in entry.tasks}

        def root(task: str) -> str:
            while parent[task] != task:
                parent[task] = parent[parent[task]]
                task = parent[task]
            return task

        def join(one: str, other: str):
            parent[root(one)] = root(other)

        after: dict[str, str] = {}  # A task -> the one of its request that replaced it.
        for task in entry.tasks:
            for other in loads(links.get(f"with:{task}"), []):
                if other in inside:
                    join(task, other)
            new = links.get(f"replacedby:{task}") or links.get(f"supersededby:{task}") or ""
            if new in inside:
                join(task, new)
                after[task] = new
            for dep in deps.get(task, []):
                if dep["depends_on"] in inside:
                    join(task, dep["depends_on"])
        groups: dict[str, list[str]] = {}
        for task in entry.tasks:
            groups.setdefault(root(task), []).append(task)
        teams = []
        for group in groups.values():
            team = self.team(entry, group, tasks, links, deps, after, message)
            if team:
                teams.append(team)
        return teams

    def team(
        self,
        entry: Request,
        group: list[str],
        tasks: dict[str, dict],
        links: dict[str, str],
        deps: dict[str, list[dict]],
        after: dict[str, str],
        message: dict | None,
    ) -> dict | None:
        # A part: the tasks one replaced another in (oldest first), by the task it is now.
        def latest(task: str) -> str:
            seen = {task}
            while task in after and after[task] not in seen:
                task = after[task]
                seen.add(task)
            return task

        made = sorted(group, key=lambda task: (tasks[task]["created_at"], task))
        chains: dict[str, list[str]] = {}
        for task in made:
            chains.setdefault(latest(task), []).append(task)
        if len(chains) < 2 or len({tasks[task]["session_id"] for task in group}) < 2:
            return None  # One part, or all in one worker: no team.
        parts = sorted(chains.items(), key=lambda item: (tasks[item[1][0]]["created_at"], item[0]))
        titles = [tasks[chain[0]]["title"] for _, chain in parts]
        request_text = message["text"] if message else ""
        goal = goal_name(request_text, titles)
        names = part_names(titles, goal)
        marks = avatars(names)
        part_of = {task: chain[0] for _, chain in parts for task in chain}
        members = []
        for index, (now_on, chain) in enumerate(parts):
            out = links.get(f"replacedby:{now_on}") or links.get(f"supersededby:{now_on}") or ""
            moved = tasks.get(out) if out and out not in part_of else None
            members.append(
                {
                    "id": chain[0],
                    "task": now_on,
                    "tasks": chain,
                    "session": tasks[now_on]["session_id"],
                    "title": tasks[now_on]["title"],
                    "name": names[index],
                    "avatar": marks[index],
                    "color": PALETTE[index % len(PALETTE)],
                    "moved_out": {
                        "task": moved["id"],
                        "title": moved["title"],
                        "session": moved["session_id"],
                    }
                    if moved
                    else None,
                }
            )
        edges: dict[tuple[str, str], dict] = {}
        for task in group:
            for dep in deps.get(task, []):
                source = dep["depends_on"]
                if source not in part_of or part_of[source] == part_of[task]:
                    continue
                state = dep_state(tasks[source]["status"], dep["needs"])
                key = (part_of[source], part_of[task])
                known = edges.get(key)
                if not known or STATE_RANK[state] < STATE_RANK[known["state"]]:
                    edges[key] = {"kind": "after", "from": key[0], "to": key[1], "state": state}
            for source in loads(links.get(f"contextfrom:{task}"), []):
                if source in part_of and part_of[source] != part_of[task]:
                    key = (part_of[source], part_of[task])
                    edges.setdefault(
                        key, {"kind": "context", "from": key[0], "to": key[1], "state": "met"}
                    )
        return {
            "id": f"team-{entry.id}-{made[0]}",
            "request": {
                "id": entry.id,
                "at": message["created_at"] if message else tasks[made[0]]["created_at"],
                "text": request_text[:600],
            },
            "goal": goal,
            "members": members,
            "edges": list(edges.values()),
        }

    def load(self):
        """Every request's teams: all of them now and then (once an hour), else those whose
        tasks changed since."""
        if not self.loaded or time.time() - self.loaded > RELOAD_SECONDS:
            made = {key: loads(value, []) for key, value in self.store.items("reqtasks:").items()}
            self.requests = self.read(made)
            self.loaded, self.dirty = time.time(), set()
            return
        if self.dirty:
            again, self.dirty = self.dirty, set()
            made = {
                key.split(":", 1)[1]: loads(value, [])
                for key, value in self.kv([f"reqtasks:{r}" for r in again]).items()
            }
            self.requests.update(self.read(made))

    def current(self, listed: list[dict]) -> tuple[list[dict], int]:
        """Candidate teams (§4.1): those of a request with a task open, listed on Home, or
        changed in the last WINDOW_DAYS; and how many older ones there are (Past teams). Read
        again once anything changed; the page says where each stands."""
        key = (self.store.db.total_changes, tuple(task["id"] for task in listed), self.bucket())
        if self.memo and self.memo[0] == key:
            return self.memo[1]
        self.load()
        window = self.window(listed)
        # Read anew on every change: what a hook may have missed never stays.
        self.requests.update(self.read({r: self.requests[r].tasks for r in window}))
        teams = [team for r in window for team in self.requests[r].teams]
        past = sum(len(entry.teams) for r, entry in self.requests.items() if r not in window)
        seen = self.kv([f"teamseen:{team['id']}" for team in teams])
        for team in teams:
            team["seen"] = seen_mark(seen.get(f"teamseen:{team['id']}"))
        teams.sort(key=lambda team: int(team["request"]["id"]), reverse=True)
        self.memo = (key, (teams, past))
        return teams, past

    @staticmethod
    def bucket() -> int:
        return int(time.time() // 60)

    def window(self, listed: list[dict]) -> set[str]:
        since = (datetime.now(UTC) - timedelta(days=WINDOW_DAYS)).isoformat()
        stamps = self.kv([f"req:{task['id']}" for task in listed])
        found = {value for value in stamps.values() if value in self.requests}
        found |= {r for r, entry in self.requests.items() if entry.open or entry.latest >= since}
        return {r for r in found if self.requests[r].teams or len(self.requests[r].tasks) >= 2}

    def snapshot(self, listed: list[dict]) -> dict:
        """The state's `teams` (candidates, with the questions held between their parts) and
        `past_teams`; `held_questions` while one holds its asker up half an hour."""
        teams, past = self.current(listed)
        waiting = self.waiting(teams)
        shown = [{**team, "waiting": waiting.get(team["id"], [])} for team in teams]
        held = [question for found in waiting.values() for question in found if question["held"]]
        return {
            "teams": shown,
            "past_teams": past,
            **({"held_questions": held} if held else {}),
        }

    # Questions between workers, by the tasks they went between.

    def question_tasks(self, question: dict) -> dict[str, str]:
        if (known := self.asked.get(question["id"])) is not None:
            return known
        found: dict[str, str] = {}
        mine = [task["id"] for task in self.tasks.for_session(question["asker"])][-6:]
        ids = [i for task in mine for i in loads(self.store.get(f"xchg:{task}"), [])[-20:]]
        for row in self.rows("SELECT meta FROM messages WHERE id IN ({})", ids):
            meta = loads(row["meta"], {})
            if meta.get("question_id") == question["id"]:
                found = {k: meta[k] for k in ("from_task", "to_task") if meta.get(k)}
                break
        self.asked[question["id"]] = found
        return found

    def holds(self, question: dict, from_task: str) -> bool:
        """Its asker waits on it: not at work, and the task it asked from still open."""
        asker = self.store.find_session(question["asker"])
        task = self.tasks.find(from_task) if from_task else None
        if not from_task:
            return self.runtime.teams.blocks(question)
        return bool(
            asker
            and not asker["closed_at"]
            and asker["status"] != "working"
            and task
            and task["status"] in {"running", "waiting"}
        )

    def waiting(self, teams: list[dict]) -> dict[str, list[dict]]:
        """Open questions between workers, by the team both their tasks are parts of ("" for
        none): who waits on whom since when, whether it holds its asker up, and when nudged."""
        parts = {
            task: (team["id"], member["id"])
            for team in teams
            for member in team["members"]
            for task in member["tasks"]
        }
        found: dict[str, list[dict]] = {}
        for question in self.runtime.asks.peer_waiting():
            stamped = self.question_tasks(question)
            one = parts.get(stamped.get("from_task", ""))
            other = parts.get(stamped.get("to_task", ""))
            team = one[0] if one and other and one[0] == other[0] else ""
            minutes = minutes_between(question["asked"])
            holds = self.holds(question, stamped.get("from_task", ""))
            found.setdefault(team, []).append(
                {
                    **question,
                    **stamped,
                    "team": team,
                    "from": one[1] if team and one else "",
                    "to": other[1] if team and other else "",
                    "minutes": minutes,
                    "blocks": holds,
                    "held": holds and minutes >= BLOCKING_MINUTES,
                    "nudged": iso(float(self.store.get(f"nudged:{question['id']}") or 0)),
                }
            )
        return found

    def held(self) -> list[dict]:
        """Questions holding their asker up half an hour or more, one each (Needs you)."""
        if not any(
            minutes_between(question["asked"]) >= BLOCKING_MINUTES
            for question in self.runtime.asks.peer_waiting()
        ):
            return []
        return self.snapshot(self.tasks.listed()).get("held_questions", [])

    async def nudge(self, question_id: str) -> str:
        """The owner's Nudge on a question, in a team or not: the worker asked hears its asker
        waits for the answer, in the words it always has (Teams.nudge)."""
        question = self.runtime.asks.question(question_id)
        if not question:
            raise LookupError("No such question.")
        # Two workers that asked each other are one of the Router's teams (talks:).
        team = self.runtime.teams.team_of(question["asker"])
        if not team:
            raise LookupError("No such question between workers.")
        return await self.runtime.teams.nudge(team, question_id)

    # A team's page, past teams, seen marks.

    def find(self, team_id: str) -> dict:
        found = TEAM_ID.match(team_id or "")
        if not found:
            raise LookupError("No such team.")
        request, task = found.groups()
        made = {request: loads(self.store.get(f"reqtasks:{request}"), [])}
        entry = self.read(made)[request]
        self.requests[request] = entry
        team = next((t for t in entry.teams if t["id"] == team_id), None)
        team = team or next(
            (t for t in entry.teams if any(task in m["tasks"] for m in t["members"])), None
        )
        if team is None:
            raise LookupError("No such team.")
        seen = self.store.get(f"teamseen:{team['id']}")
        return {**team, "seen": seen_mark(seen)}

    def page(self, team_id: str, before: str = "") -> dict:
        """A team with its members' task rows, the questions held between its parts, what
        they handed each other (newest first, a page at a time) and what each pair exchanged."""
        team = self.find(team_id)
        waiting = self.waiting([team]).get(team["id"], [])
        tasks = [task for member in team["members"] for task in member["tasks"]]
        rows = self.rows("SELECT * FROM tasks WHERE id IN ({})", tasks)
        views = [self.runtime.task_actions.view(row) for row in rows]
        items, pairs = self.history(team)
        items += self.moments(team, {row["id"]: row for row in rows})
        items.sort(key=lambda item: (item["at"], item["key"]), reverse=True)
        if before:
            at, _, key = before.partition("|")
            items = [item for item in items if (item["at"], item["key"]) < (at, key)]
        page = items[:HISTORY_PAGE]
        more = len(items) > HISTORY_PAGE
        return {
            "team": {**team, "waiting": waiting},
            "tasks": views,
            "pairs": pairs,
            "items": page,
            "total": len(items) if not before else None,
            "next": f"{page[-1]['at']}|{page[-1]['key']}" if more and page else "",
        }

    def history(self, team: dict) -> tuple[list[dict], list[dict]]:
        """Its parts' exchanges (from their tasks' `xchg:` lists, by key), kept when both
        stamped tasks are its parts'; and each pair's counts for the map."""
        part_of = {task: member["id"] for member in team["members"] for task in member["tasks"]}
        lists = self.kv([f"xchg:{task}" for task in part_of])
        ids = sorted({i for value in lists.values() for i in loads(value, [])})
        messages = [
            (row, meta)
            for row in self.rows(
                "SELECT id, session_id, kind, text, meta, created_at FROM messages "
                "WHERE id IN ({})",
                ids,
            )
            if (meta := loads(row["meta"], {})).get("from_task") in part_of
            and meta.get("to_task") in part_of
            and part_of[meta["from_task"]] != part_of[meta["to_task"]]
        ]
        questions = {
            row["id"]: row
            for row in self.rows(
                "SELECT * FROM questions WHERE id IN ({})",
                [meta["question_id"] for _, meta in messages if meta.get("question_id")],
            )
        }
        items, pairs = [], {}
        for row, meta in messages:
            one, other = part_of[meta["from_task"]], part_of[meta["to_task"]]
            pair = pairs.setdefault(
                tuple(sorted((one, other))),
                {"parts": sorted((one, other)), "told": [], "asked": [], "answered": 0, "open": []},
            )
            item = {
                "key": f"m{row['id']:012d}",
                "id": str(row["id"]),
                "from": one,
                "to": other,
                "at": row["created_at"],
                "text": row["text"],
            }
            if row["kind"] == "tell":
                title, rest = message_title(row["text"])
                items.append({**item, "kind": "tell", "title": title, "rest": rest})
                pair["told"].append(one)
                continue
            question = questions.get(meta.get("question_id", ""))
            if not question:
                continue
            title, rest = message_title(question["text"], question=True)
            asked = iso(question["asked"])
            answered = iso(question["closed"]) if question["status"] == "answered" else ""
            waited = (
                minutes_between(asked, answered)
                if question["status"] in {"open", "answered"}
                else None
            )
            items.append(
                {
                    **item,
                    "kind": "ask",
                    "question": question["id"],
                    "title": title,
                    "rest": rest,
                    "status": question["status"],
                    "answer": question["answer"],
                    "answered_at": answered,
                    "minutes": waited,
                    **(
                        {"blocks": self.holds(question, meta["from_task"])}
                        if question["status"] == "open"
                        else {}
                    ),
                }
            )
            pair["asked"].append(one)
            if question["status"] == "answered":
                pair["answered"] += 1
            if question["status"] == "open":
                pair["open"].append({"from": one, "to": other, "minutes": waited})
        return items, [
            {
                "parts": pair["parts"],
                "told": [{"from": part, "n": pair["told"].count(part)} for part in pair["parts"]],
                "asked": len(pair["asked"]),
                "answered": pair["answered"],
                "open": pair["open"],
                "from": sorted(set(pair["told"]) | set(pair["asked"])),
            }
            for pair in pairs.values()
        ]

    def moments(self, team: dict, rows: dict[str, dict]) -> list[dict]:
        """Quiet lines for what moved without words: the request in parts, a result accepted
        or waiting for the owner, a part that went on outside the team."""
        found = [
            {
                "key": "r",
                "kind": "request",
                "at": team["request"]["at"],
                "parts": [member["id"] for member in team["members"]],
            }
        ]
        for member in team["members"]:
            row = rows.get(member["task"])
            if not row:
                continue
            at = row["ended_at"] or row["updated_at"]
            if member["moved_out"]:
                found.append({"key": f"o{member['id']}", "kind": "moved", "part": member["id"],
                              "at": at, "to": member["moved_out"]})  # fmt: skip
            elif row["status"] == "review":
                found.append({"key": f"v{member['id']}", "kind": "review", "part": member["id"],
                              "at": row["updated_at"]})  # fmt: skip
            elif row["status"] in {"done", "failed", "cancelled"} and at:
                found.append({"key": f"e{member['id']}", "kind": row["status"],
                              "part": member["id"], "at": at})  # fmt: skip
        return found

    def past(self, before: str = "", limit: int = PAST_PAGE) -> dict:
        """Teams of older requests, newest first, a page at a time by a cursor over the
        Router's owner messages (at most PAST_READ of them read a page)."""
        router = self.runtime.router_id()
        listed = self.tasks.listed()
        self.current(listed)
        window = self.window(listed)
        bound = int(before) if before.isdigit() else 2**62
        rows = self.store.all(
            "SELECT id FROM messages WHERE session_id=? AND kind='owner' AND id<? "
            "ORDER BY id DESC LIMIT ?",
            (router, bound, PAST_READ),
        )
        found, last = [], ""
        for row in rows:
            request = str(row["id"])
            last = request
            entry = self.requests.get(request)
            if entry is None:
                made = loads(self.store.get(f"reqtasks:{request}"), [])
                if len(made) < 2:
                    continue
                entry = self.read({request: made})[request]
                self.requests[request] = entry
            if request in window:
                continue
            found += entry.teams
            if len(found) >= limit:
                break
        seen = self.kv([f"teamseen:{team['id']}" for team in found])
        more = len(rows) == PAST_READ or len(found) >= limit
        return {
            "teams": [
                {**team, "seen": seen_mark(seen.get(f"teamseen:{team['id']}"))} for team in found
            ],
            "next": last if more and last else "",
        }

    def seen(self, team_id: str, key: str, dismissed: bool = False) -> dict:
        """The owner saw a done team (`key`, the page's, as it is now), or dismissed it: kept as
        given, the first look per key; never read here for what it means."""
        if not TEAM_ID.match(team_id or "") or not SEEN_KEY.match(key or ""):
            raise ValueError("No such team or look.")
        known = seen_mark(self.store.get(f"teamseen:{team_id}"))
        if known and known["key"] == key and (known["dismissed"] or not dismissed):
            return known
        at = known["at"] if known and known["key"] == key else datetime.now(UTC).isoformat()
        self.store.set(f"teamseen:{team_id}", "|".join([key, at, *(["dismissed"] * dismissed)]))
        self.memo = None
        self.runtime.publish_state()
        return seen_mark(self.store.get(f"teamseen:{team_id}")) or {}


STATE_RANK = {"failed": 0, "owner": 1, "pending": 2, "met": 3}


def dep_state(status: str, needs: str) -> str:
    """What a wait on another part comes to now: its result came (met), it waits for the
    owner's OK, it won't come, or it is still to come."""
    if status == "done" or (status == "review" and needs != "accepted"):
        return "met"
    if status == "review":
        return "owner"
    if status in {"failed", "cancelled"}:
        return "failed"
    return "pending"


def seen_mark(raw: str | None) -> dict | None:
    if not raw:
        return None
    key, _, rest = raw.partition("|")
    at, _, flag = rest.partition("|")
    return {"key": key, "at": at, "dismissed": flag == "dismissed"}
