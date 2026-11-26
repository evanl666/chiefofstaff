"""Teams lite changes only what the owner sees (teams-v2.md §11.6, 11): a scripted day of an
owner's requests split into parts, run on this tree, gives the workers and the Router exactly
what 0.1.56 gave them: peer links, introductions, delivered results, waits, replacements, the
team digest and read_team_thread's answers, message by message.

The day as v0.1.56 ran it is tests/teams_day_v0156.json. To record it again, run this file in a
checkout of v0.1.56 with TEAMS_DAY_RECORD set to where to write it:

    git worktree add <scratch>/v0156 v0.1.56
    cp tests/test_teams_unchanged.py <scratch>/v0156/tests/ && cd <scratch>/v0156
    TEAMS_DAY_RECORD=<this tree>/tests/teams_day_v0156.json \\
        uv run --extra dev pytest -q tests/test_teams_unchanged.py
"""

import json
import os
import re
from pathlib import Path

from conftest import call_tool, settle

GOLDEN = Path(__file__).with_name("teams_day_v0156.json")
# What Teams lite adds to what is stored (declared, teams-v2.md §7): which task sent and which
# received an exchange; and (agents-anywhere.md 6) the agent a worker's start line names. No one
# is given them.
ADDED_META = {"from_task", "to_task", "agent"}
IDS = [
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
    re.compile(r"\bt[0-9a-f]{7}\b"),
    re.compile(r"\bq[0-9a-f]{6,12}\b"),
    re.compile(r"\bo[0-9a-f]{6}\b"),
    re.compile(r"\b(?:team|worker)-?:?[0-9a-f]{8}\b"),
    re.compile(r"\b[0-9a-f]{8}\b"),
    re.compile(r"(?<=/workspaces/)[\w-]+-[0-9a-f]{6}\b"),  # A worker's folder.
]
TIMES = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\+00:00|Z)?)?|\b\d{1,2}:\d{2}\b"
)


class Normal:
    """Ids by order of first appearance, times as <time>, folders as <dir>."""

    def __init__(self, folders: list[str]):
        self.seen: dict[str, str] = {}
        self.folders = folders

    def text(self, value: str) -> str:
        for folder in self.folders:
            value = value.replace(folder, "<dir>")
        value = TIMES.sub("<time>", value)
        for pattern in IDS:
            value = pattern.sub(self.id, value)
        return value

    def id(self, match: re.Match) -> str:
        return self.seen.setdefault(match.group(0), f"<id{len(self.seen) + 1}>")

    def value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self.value(v) for k, v in sorted(value.items())}
        if isinstance(value, list | tuple):
            return [self.value(v) for v in value]
        return value


async def owner_turn(harness, runtime, text: str):
    """The owner writes to the Router and its turn on that starts."""
    router = harness.session(harness.router_id)
    await runtime.owner_send(harness.router_id, text)
    router.emit("user.message", content=router.raw_sent[-1])
    await settle(runtime)


async def tool(runtime, name: str, caller: str, **arguments) -> str:
    result = await call_tool(runtime, name, caller, **arguments)
    await settle(runtime)
    return f"{result.result_type}: {result.text_result_for_llm}"


async def spawn(runtime, router, title: str, **extra) -> tuple[str, str, str]:
    said = await tool(runtime, "spawn_worker", router, title=title, instructions=title,
                      why_new="test", **extra)  # fmt: skip
    found = json.loads(said.split(": ", 1)[1])
    return found["session_id"], found["task_id"], said


async def day(harness) -> dict:
    """Two of the owner's requests, one in parts; their workers tell, ask, answer each other
    and across requests; a part waits on two others, one is done instead by another worker, one
    worker is reused; the Router's turns, its digest and what read_team_thread says."""
    runtime = await harness.connect()
    router = harness.router_id
    answers: list[str] = []
    await owner_turn(harness, runtime, "Ship checkout v2 for acme-shop: the API, the UI, tests.")
    api, api_task, said = await spawn(runtime, router, "Checkout API: the v2 endpoints")
    answers.append(said)
    ui, ui_task, said = await spawn(runtime, router, "Checkout UI: the v2 pages",
                                    works_with=["Checkout API: the v2 endpoints"])  # fmt: skip
    answers.append(said)
    tests, tests_task, said = await spawn(
        runtime, router, "Checkout tests: end to end", after=[api_task, ui_task]
    )
    answers.append(said)
    docs, _, said = await spawn(runtime, router, "Checkout docs", works_with=["Release notes"])
    answers.append(said)
    answers.append(await tool(runtime, "tell_session", api, session_id=ui,
                              text="Totals are in cents."))  # fmt: skip
    asked = await tool(runtime, "ask_session", ui, session_id=api, question="Rounding mode?")
    answers.append(asked)
    question = re.search(r"\((q[0-9a-f]+)\)", asked)
    if question:
        answers.append(await tool(runtime, "answer_session", api, question_id=question[1],
                                  answer="Half even."))  # fmt: skip
    answers.append(await tool(runtime, "report", api, status="done", task_id=api_task,
                              summary="The v2 endpoints are in acme/checkout#481."))  # fmt: skip
    await owner_turn(harness, runtime, "How is checkout going?")
    # Another request: its worker asks one of the first's.
    await owner_turn(harness, runtime, "Also fix the invoice rounding bug.")
    invoice, _, said = await spawn(runtime, router, "Invoice rounding bug")
    answers.append(said)
    answers.append(await tool(runtime, "ask_session", invoice, session_id=api,
                              question="Do totals round half even?"))  # fmt: skip
    answers.append(await tool(runtime, "tell_session", docs, session_id=ui,
                              text="The docs link to the pages."))  # fmt: skip
    # A part done instead by another worker; the reused one given more of the first request.
    linux, _, said = await spawn(runtime, router, "Checkout tests on Linux",
                                 replaces=[tests_task])  # fmt: skip
    answers.append(said)
    answers.append(await tool(runtime, "report", ui, status="done", task_id=ui_task,
                              summary="The v2 pages are in acme/checkout#482."))  # fmt: skip
    answers.append(await tool(runtime, "message_session", router, session_id=ui, task="new",
                              title="Checkout UI copy", text="Tighten the copy.",
                              why_new="Another deliverable: its words."))  # fmt: skip
    await owner_turn(harness, runtime, "And now?")
    for reference in (f"worker:{api[:8]}", f"worker:{invoice[:8]}", f"worker:{docs[:8]}"):
        answers.append(await tool(runtime, "read_team_thread", router, team=reference))
    sessions = [router, api, ui, tests, docs, invoice, linux]
    messages = {
        index: [
            {
                "kind": m["kind"],
                "text": m["text"],
                "meta": {k: v for k, v in (m["meta"] or {}).items() if k not in ADDED_META},
            }
            for m in harness.store.messages(session)
        ]
        for index, session in enumerate(sessions)
    }
    client = harness.client.sessions
    given = {
        index: list(client[key].sent) if (key := harness.store.get(f"copilot:{s}") or s) in client
        else None
        for index, s in enumerate(sessions)
    }  # fmt: skip
    tasks = [
        {key: task[key] for key in ("id", "title", "status", "session_id", "goal")}
        | {"after": [(dep["id"], dep["needs"]) for dep in runtime.tasks.deps(task["id"])]}
        for task in harness.store.all("SELECT * FROM tasks ORDER BY rowid")
    ]
    folders = [str(harness.store.directory)]
    return Normal(folders).value({
        "answers": answers,
        "messages": messages,
        "given": given,
        "peers": {index: runtime.peers.of(s) for index, s in enumerate(sessions)},
        "tasks": tasks,
        "teams": runtime.teams.view(),
    })  # fmt: skip


def recorded(found: dict) -> dict | None:
    """The day as v0.1.56 ran it; None while recording it."""
    if recording := os.environ.get("TEAMS_DAY_RECORD"):
        Path(recording).write_text(json.dumps(found, ensure_ascii=False, indent=1, sort_keys=True))
        return None
    return json.loads(GOLDEN.read_text())


async def test_a_day_of_teams_is_what_the_workers_and_the_router_had_in_0_1_56(harness):
    found = json.loads(json.dumps(await day(harness), ensure_ascii=False, sort_keys=True))
    expected = recorded(found)
    if expected is None:
        return
    for part in expected:
        assert found[part] == expected[part], part
    assert found == expected
    # What the owner sees of it: the first request's team (the part done instead by a task of
    # another request has moved out); the second's workers asking it make no team.
    tasks = {t["title"]: t["id"] for t in harness.store.all("SELECT id, title FROM tasks")}
    [team] = harness.runtime.snapshot()["teams"]
    assert [m["task"] for m in team["members"]] == [
        tasks["Checkout API: the v2 endpoints"],
        tasks["Checkout UI: the v2 pages"],
        tasks["Checkout tests: end to end"],
    ]
    assert team["members"][2]["moved_out"]["task"] == tasks["Checkout tests on Linux"]
