"""Task relationships (E130): how the tasks of workers that work together are related.

One model, read when asked from what Chief of Staff keeps anyway: a dependency (`task_deps`),
a task done instead by another (`replacedby:`, `supersededby:`), the earlier work a new worker
was given (`contextfrom:`), work split off a task (`splitfrom:`, `splitoff:`), the parts of one
request started side by side (`parts:`), a worker's work handed to a new one (`continues:`), a
second worker forked from a busy one (`forkof:`), the same task on several agents (`compare:`).
Nothing of its own is stored. Each is an edge from the earlier task to the one that came of it
(left to right in a lineage), with its `kind` (the record) apart from its `verb` (what the page
says: waits, continues, branched, parts, vs), so the words can change without touching data.

The workers these join are one team (teams.py), as those that work together or talk are. A PR
or project two tasks share is an anchor on the team, never an edge: a team never grows into a
web. A task's row carries one relation (Teams: the most telling), and its lineage when asked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from . import threads
from .tasks import ADOPTED

if TYPE_CHECKING:
    from .runtime import Runtime

VERBS = {
    "after": "waits",
    "replaced": "continues",
    "superseded": "continues",
    "context": "continues",
    "handoff": "continues",
    "split": "branched",
    "fork": "branched",
    "part": "parts",
    "compare": "vs",
}
# The relations that make the workers at either end one team (parts are peers already).
TEAM_KINDS = frozenset({"after", "replaced", "superseded", "context", "handoff", "fork", "compare"})
# What one task does instead of, or on from, another.
CONTINUING = ("replaced", "superseded", "context", "handoff")
# Of a group, not a chain: drawn as one fan in a lineage.
GROUP_KINDS = frozenset({"part", "compare"})
KEYS = (
    "replacedby:",
    "supersededby:",
    "contextfrom:",
    "splitfrom:",
    "splitoff:",
    "parts:",
    "continues:",
    "forkof:",
    "compare:",
)
LANDED = frozenset({"review", "done"})
# A wait cleared this recently keeps its task's chip ("Ada delivered · unblocked").
JUST_CLEARED = 90
# A lineage: at most this many tasks, two steps each way.
LINEAGE_NODES = 5
# A team's rows: its latest tasks.
TASK_ROWS = 60
FOLLOW_UPS = ("follow-up", "redo")


def loads(text: str | None, default: Any) -> Any:
    try:
        found = json.loads(text or "")
    except ValueError:
        return default
    return found if isinstance(found, type(default)) else default


def seconds_since(stamp: str, now: datetime | None = None) -> float:
    if not stamp:
        return float("inf")
    return ((now or datetime.now(UTC)) - datetime.fromisoformat(stamp)).total_seconds()


def landed_at(task: dict) -> str:
    """When a task's result came: its last run's end (a reported one is in review)."""
    return task.get("ran_to") or task.get("ended_at") or task.get("updated_at") or ""


@dataclass
class Graph:
    edges: list[dict] = field(default_factory=list)
    tasks: dict[str, dict] = field(default_factory=dict)
    groups: list[dict] = field(default_factory=list)
    # The workers its relations join, as teams.py reads them (once per graph).
    joined: dict[str, list[str]] | None = None


TASK_COLUMNS = """t.id, t.session_id, t.title, t.status, t.created_at, t.updated_at, t.ended_at,
    (SELECT max(r.ended_at) FROM runs r WHERE r.task_id = t.id) AS ran_to"""


class Relations:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        # Read once for many calls (a snapshot asks per worker): again once anything changed.
        self.memo: tuple[int, Graph] | None = None

    def graph(self) -> Graph:
        changes = self.store.db.total_changes
        if self.memo and self.memo[0] == changes:
            return self.memo[1]
        graph = self.read()
        self.memo = (changes, graph)
        return graph

    def read(self) -> Graph:
        """Every relation, from what is stored: a fixed number of queries however many."""
        store = self.store
        deps = store.all("SELECT task_id, depends_on, needs FROM task_deps")
        ranges = " OR ".join("(key >= ? AND key < ?)" for _ in KEYS)
        bounds = tuple(bound for prefix in KEYS for bound in (prefix, prefix + "\uffff"))
        found: dict[str, dict[str, str]] = {prefix: {} for prefix in KEYS}
        for row in store.all(f"SELECT key, value FROM kv WHERE {ranges}", bounds):  # noqa: S608  (placeholders only)
            prefix, rest = row["key"].split(":", 1)
            found[prefix + ":"][rest] = row["value"]
        forks = {new: loads(value, {}).get("id", "") for new, value in found["forkof:"].items()}
        handed = found["continues:"]
        workers = {*handed, *handed.values(), *forks, *forks.values(), *found["compare:"]}
        named = {
            *(dep["task_id"] for dep in deps),
            *(dep["depends_on"] for dep in deps),
            *(key for prefix in KEYS[:6] for key in found[prefix]),
            *found["replacedby:"].values(),
            *found["supersededby:"].values(),
            *found["splitoff:"].values(),
            *(loads(value, {}).get("task_id", "") for value in found["splitfrom:"].values()),
            *(one for value in found["contextfrom:"].values() for one in loads(value, [])),
            *(one for value in found["parts:"].values() for one in loads(value, [])),
        }  # fmt: skip
        if not named and not workers:
            return Graph()
        tasks = {
            row["id"]: row
            for row in store.all(
                f"""SELECT {TASK_COLUMNS} FROM tasks t WHERE t.id IN (SELECT value FROM
                json_each(?)) OR t.session_id IN (SELECT value FROM json_each(?))""",  # noqa: S608  (constant columns)
                (json.dumps(sorted(filter(None, named))), json.dumps(sorted(workers))),
            )
        }
        made = (
            {
                row["id"]: row["created_at"]
                for row in store.all(
                    "SELECT id, created_at FROM sessions WHERE id IN (SELECT value FROM "
                    "json_each(?))",
                    (json.dumps(sorted(workers)),),
                )
            }
            if workers
            else {}
        )
        graph = Graph(tasks=tasks)

        def add(kind: str, source: str, target: str, by: str, state: str = "resolved", **more):
            if not source or not target or source == target:
                return
            if source not in tasks or target not in tasks:
                return
            graph.edges.append(
                {
                    "id": f"{kind}:{source}:{target}",
                    "kind": kind,
                    "verb": VERBS[kind],
                    "from_task": source,
                    "to_task": target,
                    "from_session": tasks[source]["session_id"],
                    "to_session": tasks[target]["session_id"],
                    "created_by": by,
                    "at": more.pop("at", "") or tasks[target]["created_at"],
                    "state": state,
                    "resolved_at": more.pop("resolved_at", ""),
                    **more,
                }
            )

        for dep in deps:
            needed, waiting = tasks.get(dep["depends_on"]), tasks.get(dep["task_id"])
            if not needed or not waiting:
                continue
            met = needed["status"] in LANDED and (
                dep["needs"] != "accepted" or needed["status"] == "done"
            )
            # A wait its task no longer has (cancelled, failed before it started) is moot.
            moot = not met and waiting["status"] in {"cancelled", "failed"}
            state = "resolved" if met else "superseded" if moot else "pending"
            add("after", needed["id"], waiting["id"], "router", state,
                resolved_at=landed_at(needed) if met else "", needs=dep["needs"])  # fmt: skip
        later: dict[str, str] = {}
        for kind, prefix in (("replaced", "replacedby:"), ("superseded", "supersededby:")):
            for old, new in found[prefix].items():
                if old in tasks and new in tasks:
                    later[old] = new
                    add(kind, old, new, "router")
        instead = {(edge["from_task"], edge["to_task"]) for edge in graph.edges}
        for new, value in found["contextfrom:"].items():
            for earlier in loads(value, []):
                if (earlier, new) not in instead:
                    add("context", earlier, new, "router")
        splits = {}
        for made_id, value in found["splitfrom:"].items():
            splits[made_id] = loads(value, {}).get("task_id", "")
        owners = self.split_by_owner(list(splits)) if splits else set()
        for made_id, original in splits.items():
            add("split", original, made_id, "owner" if made_id in owners else "router")
        for original, made_id in found["splitoff:"].items():
            if splits.get(made_id) != original:
                add("split", original, made_id, "worker")
        of_worker: dict[str, list[dict]] = {}
        for task in sorted(tasks.values(), key=lambda task: (task["created_at"], task["id"])):
            of_worker.setdefault(task["session_id"], []).append(task)

        def first(worker: str) -> str:
            return of_worker[worker][0]["id"] if of_worker.get(worker) else ""

        def then(worker: str, moment: str) -> str:
            """The task a worker was on at a moment: its latest given by then."""
            given = [task for task in of_worker.get(worker, []) if task["created_at"] <= moment]
            return given[-1]["id"] if given else first(worker)

        for new, old in handed.items():
            if new in made:
                add("handoff", then(old, made[new]), first(new), "owner", at=made[new])
        for new, source in forks.items():
            if new in made:
                add("fork", then(source, made[new]), first(new), "router", at=made[new])

        def latest(task_id: str) -> str:
            """A task done instead by another is that one (a part, a compare's side)."""
            seen = set()
            while task_id in later and task_id not in seen:
                seen.add(task_id)
                task_id = later[task_id]
            return task_id

        parent: dict[str, str] = {}

        def root(node: str) -> str:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        # A task of the request that waits on another of it is a chain, not a part: it starts
        # once that one is done (as an earlier version kept it too).
        waits = {(e["from_task"], e["to_task"]) for e in graph.edges if e["kind"] == "after"}
        for new, value in found["parts:"].items():
            for other in loads(value, []):
                one, two = latest(new), latest(other)
                if (one, two) in waits or (two, one) in waits:
                    continue
                if one in tasks and two in tasks and one != two:
                    parent[root(one)] = root(two)
        fans: dict[str, list[str]] = {}
        for node in parent:
            fans.setdefault(root(node), []).append(node)
        for members in fans.values():
            fan = set(members)
            following = {after for before, after in waits if before in fan and after in fan}
            self.group(graph, add, "parts", [m for m in members if m not in following], "")
        compared: dict[str, list[str]] = {}
        for worker, value in found["compare:"].items():
            if group := loads(value, {}).get("id", ""):
                compared.setdefault(group, []).append(worker)
        for group, members in compared.items():
            # One that went on in a new worker (Continue with…) is that worker's side.
            gone = {handed[worker] for worker in members if worker in handed}
            sides = [latest(first(worker)) for worker in members if worker not in gone]
            self.group(graph, add, "compare", [side for side in sides if side], group)
        graph.groups.sort(key=lambda group: group["started_at"])
        return graph

    @staticmethod
    def group(graph: Graph, add, kind: str, members: list[str], name: str):
        tasks = graph.tasks
        members = sorted(set(members), key=lambda task: (tasks[task]["created_at"], task))
        if len(members) < 2:
            return
        anchor = members[0]
        landed = [task for task in members if tasks[task]["status"] in LANDED]
        if kind == "parts":
            state = "resolved" if len(landed) == len(members) else "pending"
            picked = ""
        else:
            accepted = sorted(
                (task for task in members if tasks[task]["status"] == "done"),
                key=lambda task: tasks[task]["ended_at"] or "",
            )
            picked = accepted[0] if accepted else ""
            state = "resolved" if picked else "pending"
        identifier = f"{kind}-{name or anchor}"
        graph.groups.append(
            {
                "id": identifier,
                "kind": kind,
                "tasks": members,
                "landed": len(landed),
                "failed": sum(tasks[task]["status"] in {"failed", "cancelled"} for task in members),
                "total": len(members),
                "state": state,
                "picked": picked,
                "started_at": tasks[anchor]["created_at"],
                "landed_at": max(landed_at(tasks[task]) for task in landed)
                if state == "resolved" and landed
                else "",
            }
        )
        for task in members[1:]:
            add("part" if kind == "parts" else "compare", anchor, task, "router", state,
                group=identifier)  # fmt: skip

    def split_by_owner(self, made: list[str]) -> set[str]:
        """Tasks split off by the owner (their earlier title says by whom)."""
        rows = self.store.all(
            "SELECT key, value FROM kv WHERE key IN (SELECT 'titles:' || value FROM json_each(?))",
            (json.dumps(made),),
        )
        return {
            row["key"].removeprefix("titles:")
            for row in rows
            if any(
                entry.get("by") == "you" and str(entry.get("title", "")).startswith("split from")
                for entry in loads(row["value"], [])
                if isinstance(entry, dict)
            )
        }

    # What teams.py reads

    def links(self) -> dict[str, list[str]]:
        """The workers a relation joins (one team): of each, the others."""
        graph = self.graph()
        if graph.joined is None:
            joined: dict[str, list[str]] = {}
            for edge in graph.edges:
                one, other = edge["from_session"], edge["to_session"]
                if edge["kind"] in TEAM_KINDS and one != other:
                    for first, second in ((one, other), (other, one)):
                        if second not in joined.setdefault(first, []):
                            joined[first].append(second)
            graph.joined = joined
        return graph.joined

    def cue(self, members: list[str]) -> dict:
        """What Home's line of a team says of its tasks: the parts of a request still landing
        (how many of how many), and how many tasks wait on another's."""
        graph = self.graph()
        mine = set(members)
        landing = [
            group
            for group in graph.groups
            if group["kind"] == "parts"
            and group["state"] == "pending"
            and graph.tasks[group["tasks"][0]]["session_id"] in mine
        ]
        waiting = sum(
            edge["kind"] == "after" and edge["state"] == "pending" and edge["to_session"] in mine
            for edge in graph.edges
        )
        latest = landing[-1] if landing else None
        return {
            "parts": [latest["landed"], latest["total"]] if latest else None,
            "waiting": waiting,
        }

    def latest(self, members: list[str]) -> str:
        """When its workers' tasks last became related, or a wait between them cleared."""
        mine = set(members)
        return max(
            (
                stamp
                for edge in self.graph().edges
                if edge["to_session"] in mine
                for stamp in (edge["at"], edge["resolved_at"])
            ),
            default="",
        )

    def team(self, members: list[str], waiting: list[dict]) -> dict:
        """A team's tasks (its latest, a row each with its one relation and how many more) and
        how they are related: the edges, the groups (parts, compares), the PRs or project they
        share, and counts for the view's summary. `waiting`: its open questions (Teams)."""
        graph = self.graph()
        rows = self.store.all(
            f"""SELECT {TASK_COLUMNS}, t.result FROM tasks t WHERE t.session_id IN (SELECT value
            FROM json_each(?)) AND NOT (t.status='done' AND t.result=?)
            ORDER BY t.created_at, t.id""",  # noqa: S608  (constant columns)
            (json.dumps(members), ADOPTED),
        )
        ids = {row["id"] for row in rows}
        edges = [
            edge for edge in graph.edges if edge["from_task"] in ids and edge["to_task"] in ids
        ]
        groups = [group for group in graph.groups if set(group["tasks"]) <= ids]
        followups = {
            row["task_id"]: row["n"]
            for row in self.store.all(
                "SELECT task_id, count(*) AS n FROM runs WHERE task_id IN (SELECT value FROM "
                "json_each(?)) AND reason IN (?, ?) GROUP BY task_id",
                (json.dumps(sorted(ids)), *FOLLOW_UPS),
            )
        }
        # A teammate's question its asker's task waits on: that task's wait (one chip).
        asked: dict[str, dict] = {}
        for question in waiting:
            task = self.runtime.tasks.current(question["asker"]) if question["blocks"] else None
            if task and task["id"] in ids and task["id"] not in asked:
                asked[task["id"]] = question
        now = datetime.now(UTC)
        shown = rows[-TASK_ROWS:]
        listed = []
        for row in shown:
            relation, more = chip_of(row, edges, groups, asked.get(row["id"]), followups, now)
            listed.append(
                {
                    **{key: row[key] for key in ("id", "session_id", "title", "status")},
                    **{key: row[key] for key in ("created_at", "updated_at", "ended_at")},
                    "landed_at": landed_at(row) if row["status"] in LANDED else "",
                    # Its one relation on its row (the most telling), and how many more it has.
                    "relation": relation,
                    "more": more,
                    "followups": followups.get(row["id"], 0),
                    "lineage": lineage(row["id"], edges, groups),
                }
            )
        return {
            "tasks": listed,
            "earlier": len(rows) - len(shown),
            "relations": edges,
            "groups": groups,
            "anchors": self.anchors(rows, members),
            "summary": summary(edges, groups, asked),
        }

    def anchors(self, rows: list[dict], members: list[str]) -> list[dict]:
        """What its tasks share: their PRs (a chip each, the one most of them have first), else
        a project two or more of its workers work in."""
        actions = self.runtime.task_actions
        prs: dict[str, dict] = {}
        for row in rows:
            for url in actions.pr_urls(row):
                for repo, number in threads.PR_URL.findall(url):
                    key = f"{repo.lower()}#{number}"
                    found = prs.setdefault(
                        key,
                        {"kind": "pr", "label": f"#{number}", "title": f"{repo}#{number}",
                         "url": f"https://github.com/{repo}/pull/{number}", "tasks": []},
                    )  # fmt: skip
                    if row["id"] not in found["tasks"]:
                        found["tasks"].append(row["id"])
        if prs:
            return sorted(prs.values(), key=lambda pr: -len(pr["tasks"]))[:3]
        projects: dict[str, list[str]] = {}
        for row in self.store.all(
            "SELECT id, project FROM sessions WHERE id IN (SELECT value FROM json_each(?)) AND "
            "project != ''",
            (json.dumps(members),),
        ):
            projects.setdefault(row["project"], []).append(row["id"])
        return [
            {
                "kind": "repo",
                "label": path.rstrip("/").rsplit("/", 1)[-1],
                "title": path,
                "workers": workers,
            }
            for path, workers in projects.items()
            if len(workers) >= 2
        ][:1]


# A row's one relation, most telling first: what it waits on (what the owner may act on now),
# a task done instead by another (its row is over), a compare to pick, its part of a request,
# where it branched from, then what it continues.
WAIT, REPLACED, VERSUS, PART, BRANCHED, CONTINUES, MORE = range(7)


def chip_of(
    task: dict,
    edges: list[dict],
    groups: list[dict],
    question: dict | None,
    followups: dict[str, int],
    now: datetime,
) -> tuple[dict | None, int]:
    """A task's one relation on its row, and how many more it has."""
    task_id = task["id"]
    found: list[tuple[int, str, dict]] = []
    for edge in edges:
        if edge["kind"] in GROUP_KINDS or task_id not in (edge["from_task"], edge["to_task"]):
            continue
        side = "to" if edge["to_task"] == task_id else "from"
        other = edge["from_task"] if side == "to" else edge["to_task"]
        chip = {"kind": edge["kind"], "side": side, "other": other, "edge": edge["id"]}
        chip |= {key: edge[key] for key in ("state", "at", "resolved_at")}
        kind = edge["kind"]
        if kind == "after" and side == "to":
            if edge["state"] == "pending":
                rank = WAIT
                chip["minutes"] = round(seconds_since(edge["at"], now) / 60)
            elif (
                edge["state"] == "resolved"
                and seconds_since(edge["resolved_at"], now) < JUST_CLEARED
            ):
                rank, chip["just"] = WAIT, True
            else:
                rank = CONTINUES
        elif kind == "after":
            rank = BRANCHED if edge["state"] == "pending" else CONTINUES
        elif kind in {"replaced", "superseded", "handoff"} and side == "from":
            rank = REPLACED
        elif kind in {"split", "fork"} and side == "to":
            rank = BRANCHED
        else:
            rank = CONTINUES
        found.append((rank, edge["at"], chip))
    for group in groups:
        if task_id not in group["tasks"]:
            continue
        chip = {"kind": group["kind"], "group": group["id"], "state": group["state"],
                "landed": group["landed"], "total": group["total"], "failed": group["failed"],
                "picked": group["picked"], "at": group["started_at"],
                "others": [other for other in group["tasks"] if other != task_id]}  # fmt: skip
        if group["kind"] == "parts":
            rank = PART
        else:
            rank = VERSUS if group["state"] == "pending" else BRANCHED
        found.append((rank, group["started_at"], chip))
    relations = len(found)
    if question:
        chip = {"kind": "question", "other_session": question["target"],
                "question": question["id"], "minutes": question["minutes"],
                "state": "pending", "at": question["asked"]}  # fmt: skip
        found.append((WAIT, question["asked"], chip))
    if followups.get(task_id):
        found.append((MORE, "", {"kind": "followups", "count": followups[task_id]}))
    if not found:
        return None, 0
    # The most telling; of two as telling, the latest (of two waits, the longer).
    found.sort(key=lambda item: item[1], reverse=True)
    found.sort(key=lambda item: (item[0], -item[2].get("minutes", 0)))
    chip = found[0][2]
    counted = chip["kind"] not in {"question", "followups"}
    return chip, relations - (1 if counted else 0)


def lineage(task_id: str, edges: list[dict], groups: list[dict]) -> dict | None:
    """Where a task came from and what came of it, as a strip: columns left to right (a step
    each, two at most each way), the task's own holding it first and its parts' or compare's
    others under it, at most LINEAGE_NODES tasks; how many more there are each way. Each task
    joins the one at the top of the next column, so no two lines cross. None for a task with
    no relation."""
    chained = [edge for edge in edges if edge["kind"] not in GROUP_KINDS]
    before: dict[str, list[dict]] = {}
    after: dict[str, list[dict]] = {}
    for edge in chained:
        before.setdefault(edge["to_task"], []).append(edge)
        after.setdefault(edge["from_task"], []).append(edge)
    group = next((g for g in groups if task_id in g["tasks"] and g["kind"] == "parts"), None)
    group = group or next((g for g in groups if task_id in g["tasks"]), None)
    if not group and task_id not in before and task_id not in after:
        return None

    def ordered(found: list[dict], end: str) -> list[str]:
        """The tasks one step away, the nearest in time first."""
        steps = sorted(found, key=lambda edge: edge["at"], reverse=end == "from_task")
        return list(dict.fromkeys(edge[end] for edge in steps))

    def reach(start: str, links: dict[str, list[dict]], end: str) -> set[str]:
        seen: set[str] = set()
        todo = [start]
        while todo and len(seen) < 200:
            for edge in links.get(todo.pop(), []):
                if edge[end] not in seen and edge[end] != task_id:
                    seen.add(edge[end])
                    todo.append(edge[end])
        return seen

    siblings = [other for other in group["tasks"] if other != task_id] if group else []
    left1 = ordered(before.get(task_id, []), "from_task")
    right1 = ordered(after.get(task_id, []), "to_task")
    columns: dict[str, list[str]] = {"left2": [], "left1": [], "own": [task_id], "right1": [],
                                     "right2": []}  # fmt: skip
    room = LINEAGE_NODES - 1
    shown = {task_id}

    def take(column: str, candidates: list[str], most: int):
        nonlocal room
        for candidate in candidates:
            if room <= 0 or len(columns[column]) >= most:
                return
            if candidate not in shown:
                columns[column].append(candidate)
                shown.add(candidate)
                room -= 1

    # Its own fan first (one sibling, then its nearest steps), so each side is seen.
    take("own", siblings, 2)
    take("left1", left1, 1)
    take("right1", right1, 1)
    take("own", siblings, 3)
    take("left1", left1, 2)
    take("right1", right1, 2)
    if columns["left1"]:
        take("left2", ordered(before.get(columns["left1"][0], []), "from_task"), 1)
    if columns["right1"]:
        take("right2", ordered(after.get(columns["right1"][0], []), "to_task"), 1)
    earlier = reach(task_id, before, "from_task") - shown
    later = reach(task_id, after, "to_task") - shown
    order = [name for name in ("left2", "left1", "own", "right1", "right2") if columns[name]]
    return {
        "columns": [columns[name] for name in order],
        "at": order.index("own"),
        "earlier": len(earlier),
        "later": len(later),
        "group": group["id"] if group else "",
        "siblings": max(0, len(siblings) - (len(columns["own"]) - 1)),
    }


def summary(edges: list[dict], groups: list[dict], asked: dict[str, dict]) -> dict:
    """A team view's chips: counts only."""
    kinds: dict[str, int] = {}
    for edge in edges:
        kinds[edge["kind"]] = kinds.get(edge["kind"], 0) + 1
    pending = [edge for edge in edges if edge["kind"] == "after" and edge["state"] == "pending"]
    return {
        "parts": [
            {
                "group": group["id"],
                "landed": group["landed"],
                "total": group["total"],
                "failed": group["failed"],
                "state": group["state"],
            }
            for group in groups
            if group["kind"] == "parts"
        ][-2:],
        "waiting": len({edge["to_task"] for edge in pending}),
        "asked": len(asked),
        "split": kinds.get("split", 0),
        "fork": kinds.get("fork", 0),
        "continues": sum(kinds.get(kind, 0) for kind in CONTINUING),
        "compare": sum(group["kind"] == "compare" for group in groups),
    }
