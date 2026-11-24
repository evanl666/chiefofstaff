"""Teams lite (teams-v2.md v3.1): passive stamps on what the Router makes for one owner request,
and what the owner sees of a team from them. Workers, the Router and delivery are as before."""

import json
import re
from pathlib import Path

from conftest import call_tool, settle
from test_fork import busy, new_task

from chiefofstaff.tools import TURN_REQUEST, MessageSession, SpawnWorker


async def owner_turn(harness, runtime, text: str) -> str:
    """The owner writes to the Router and its turn on that starts: the request's id."""
    router = harness.session(harness.router_id)
    message = await runtime.owner_send(harness.router_id, text)
    router.emit("user.message", content=router.raw_sent[-1])
    await settle(runtime)
    return str(message["id"])


async def event_turn(harness, runtime, text: str = "CI finished on api#7."):
    router = harness.session(harness.router_id)
    router.emit("user.message", content=f'<message from="system" type="event">\n{text}\n</message>')
    await settle(runtime)


async def spawn(runtime, router, title, **extra) -> dict:
    extra.setdefault("instructions", title)
    result = await call_tool(runtime, "spawn_worker", router, title=title, why_new="test", **extra)
    assert result.result_type != "failure", result
    return json.loads(result.text_result_for_llm)


def stamp(harness, task_id: str) -> str | None:
    return harness.store.get(f"req:{task_id}")


def with_of(harness, task_id: str) -> list[str]:
    return json.loads(harness.store.get(f"with:{task_id}") or "[]")


async def test_a_task_made_for_an_owner_request_has_it_and_one_made_on_an_event_none(harness):
    runtime = await harness.connect()
    router = harness.router_id
    request = await owner_turn(harness, runtime, "Ship checkout v2 for acme-shop: API and UI.")
    api = await spawn(runtime, router, "Checkout API: the v2 endpoints")
    ui = await spawn(runtime, router, "Checkout UI: the v2 pages", works_with=[api["task_id"]])
    assert stamp(harness, api["task_id"]) == stamp(harness, ui["task_id"]) == request
    assert json.loads(harness.store.get(f"reqtasks:{request}")) == [api["task_id"], ui["task_id"]]
    await event_turn(harness, runtime)
    later = await spawn(runtime, router, "Rerun the flaky check · api#7")
    assert stamp(harness, later["task_id"]) is None, "an event's turn: no request"
    # Outside any Router tool call (a click, a timer): never the turn that runs.
    await owner_turn(harness, runtime, "Something else entirely.")
    direct = await runtime.toolset.t_spawn_worker(
        SpawnWorker(title="Tidy the docs", instructions="Tidy", why_new="test"), router
    )
    assert stamp(harness, direct["task_id"]) is None


async def test_with_names_only_tasks_of_the_same_request_made_before_it(harness):
    runtime = await harness.connect()
    router = harness.router_id
    first = await owner_turn(harness, runtime, "Fix the token flow.")
    old = await spawn(runtime, router, "Rotate the API token")
    await owner_turn(harness, runtime, "Ship checkout v2: the API, then the UI and tests.")
    api = await spawn(runtime, router, "Checkout API: the v2 endpoints", works_with=["Tests later"])
    ui = await spawn(
        runtime,
        router,
        "Checkout UI: the v2 pages",
        works_with=["checkout API:  the v2 endpoints", old["task_id"]],
    )
    tests = await spawn(runtime, router, "Tests later", works_with=[f"worker:{ui['session_id']}"])
    assert stamp(harness, old["task_id"]) == first
    assert with_of(harness, ui["task_id"]) == [api["task_id"]], "by title; never another request"
    assert with_of(harness, tests["task_id"]) == [ui["task_id"]], "by its worker, as it is now"
    assert with_of(harness, api["task_id"]) == [], "a task not made yet is never named"
    # wants: keeps linking workers as before (peers.candidates): the API worker works with Tests.
    assert tests["session_id"] in runtime.peers.of(api["session_id"])


async def test_message_session_new_its_fork_and_compare_sides_have_the_request(harness):
    runtime = await harness.connect()
    router = harness.router_id
    await owner_turn(harness, runtime, "Make the export.")
    made = await spawn(runtime, router, "Export the report")
    request = await owner_turn(harness, runtime, "Now the import too, and docs.")
    reply = await call_tool(
        runtime,
        "message_session",
        router,
        session_id=made["session_id"],
        earlier="keep",
        text="Also the import.",
        task="new",
        title="Import the report",
        why_new="more",
    )
    assert reply.result_type != "failure", reply
    task = runtime.tasks.current(made["session_id"])
    assert task["title"] == "Import the report" and stamp(harness, task["id"]) == request
    # A busy worker's new task goes to a second worker (its fork): the same request.
    worker = await busy(runtime, harness)
    forked = await owner_turn(harness, runtime, "And a CSV export.")
    second = await new_task(runtime, harness, worker, "Export CSV", "Add a CSV export.")
    assert second["session_id"] != worker
    assert stamp(harness, runtime.tasks.current(second["session_id"])["id"]) == forked
    # A Compare: a task per side, each of the request.
    token = TURN_REQUEST.set(forked)
    try:
        compared = await runtime.toolset.spawn_compare(
            SpawnWorker(title="Try the cache", instructions="Try", why_new="t"),
            router,
            ["copilot", "copilot"],
            TURN_REQUEST.get(),
        )
    finally:
        TURN_REQUEST.reset(token)
    sides = [side["task_id"] for side in compared["workers"]]
    assert len(sides) == 2 and {stamp(harness, side) for side in sides} == {forked}


async def test_a_replayed_start_keeps_the_request_it_was_asked_in(harness, monkeypatch):
    """A start refused for its agent and replayed later, while the Router answers another
    message: the request of its record, never the turn running then; none in it, none."""
    runtime = await harness.connect()
    first = await owner_turn(harness, runtime, "Fix the login test.")
    monkeypatch.setattr(runtime, "unready", lambda agent, why: {"status": "", "said": why})
    params = SpawnWorker(title="Fix the login test", instructions="Fix it", why_new="test")
    token = TURN_REQUEST.set(first)
    try:
        runtime.agent_refused_card(
            "codex", "Codex is not signed in.", params.model_dump(), request=TURN_REQUEST.get()
        )
    finally:
        TURN_REQUEST.reset(token)
    [(key, raw)] = harness.store.items("agentrefusal:").items()
    found = json.loads(raw)
    assert found["request"] == first
    await owner_turn(harness, runtime, "Something else entirely.")
    found["agent"] = "copilot"
    await runtime.resume_refused(key, found, after="started")
    [task] = [t for t in runtime.tasks.listed() if t["title"] == "Fix the login test"]
    assert stamp(harness, task["id"]) == first
    # A record from before (no request): no stamp, never the turn running now.
    old = {"agent": "copilot", "params": params.model_dump() | {"title": "Fix the sign-up test"}}
    harness.store.set("agentrefusal:k2", json.dumps(old))
    await runtime.resume_refused("k2", old, after="started")
    [task] = [t for t in runtime.tasks.listed() if t["title"] == "Fix the sign-up test"]
    assert stamp(harness, task["id"]) is None


async def test_every_replay_branch_passes_the_stored_request(harness, monkeypatch):
    """message_session's replay, the instead start and refusal_to_continuation (task="new"):
    each its record's request."""
    runtime = await harness.connect()
    router = harness.router_id
    first = await owner_turn(harness, runtime, "Export, then import.")
    made = await spawn(runtime, router, "Export the report")
    await owner_turn(harness, runtime, "Unrelated: the docs.")
    replay = MessageSession(
        session_id=made["session_id"],
        text="Also the import.",
        task="new",
        title="Import the report",
        why_new="more",
        earlier="keep",
    ).model_dump()
    record = {
        "agent": "copilot",
        "params": {"title": "Export the report"},
        "request": first,
        "worker": made["session_id"],
        "text": "Also the import.",
        "replay": replay,
    }
    harness.store.set("agentrefusal:k1", json.dumps(record))
    await runtime.resume_refused("k1", record, after="started")
    task = runtime.tasks.current(made["session_id"])
    assert task["title"] == "Import the report" and stamp(harness, task["id"]) == first
    params = SpawnWorker(title="Count the rows", instructions="Count", why_new="t").model_dump()
    held = {"agent": "codex", "params": params, "instead": "copilot", "request": first}
    harness.store.set("agentrefusal:k2", json.dumps(held))
    await runtime.answer_refusal("k2", "copilot")
    [task] = [t for t in runtime.tasks.listed() if t["title"] == "Count the rows"]
    assert stamp(harness, task["id"]) == first
    replay["title"] = "Sort the rows"
    record = {
        "agent": "codex",
        "params": {"title": "Export"},
        "request": first,
        "replay": replay,
        "worker": made["session_id"],
        "text": "Sort them.",
    }
    await runtime.refusal_to_continuation("k3", record, {"session_id": made["session_id"]})
    [task] = [t for t in runtime.tasks.listed() if t["title"] == "Sort the rows"]
    assert stamp(harness, task["id"]) == first


async def test_continue_with_stamps_only_what_every_piece_agrees_on(harness, monkeypatch):
    runtime = await harness.connect()
    router = harness.router_id
    monkeypatch.setattr(runtime, "handoff_summary", _summary)
    # Moved to another agent as Continue with… does; which one is beside the point here.
    monkeypatch.setattr(runtime, "continue_checks", lambda row, agent: None)
    first = await owner_turn(harness, runtime, "Export, then import.")
    one = await spawn(runtime, router, "Export the report")
    two = await spawn(runtime, router, "Import the report", works_with=[one["task_id"]])
    for made in (one, two):
        runtime.update_session(made["session_id"], status="idle")
    moved = await runtime.continue_on(one["session_id"], "copilot")
    assert stamp(harness, moved["task_id"]) == first, "the one task it replaces: its request"
    other = await owner_turn(harness, runtime, "Unrelated: the docs.")
    mixed = await runtime.continue_on(
        two["session_id"], "copilot", also="Docs too.", also_request=other
    )
    assert stamp(harness, mixed["task_id"]) is None, "an also of another request: none"
    third = await spawn(runtime, router, "Write the docs")
    runtime.update_session(third["session_id"], status="idle")
    old = await runtime.continue_on(third["session_id"], "copilot", also="And the FAQ.")
    assert stamp(harness, old["task_id"]) is None, "an also from a record without one: none"


async def _summary(row, agent):
    return f"Go on with {row['title']}."


async def test_an_exchange_is_stamped_with_its_tasks_and_listed_for_each(harness):
    runtime = await harness.connect()
    router = harness.router_id
    await owner_turn(harness, runtime, "Rotate the token and use it.")
    scout = await spawn(runtime, router, "Rotate the API token")
    writer = await spawn(runtime, router, "Use the new token", works_with=[scout["task_id"]])
    await call_tool(
        runtime,
        "tell_session",
        scout["session_id"],
        session_id=writer["session_id"],
        text="The token is in vault:api/v2.",
    )
    asked = await call_tool(
        runtime,
        "ask_session",
        writer["session_id"],
        session_id=scout["session_id"],
        question="Which scope?",
    )
    question = re.search(r"\((q\w+)\)", asked.text_result_for_llm).group(1)
    tell = next(m for m in harness.store.messages(scout["session_id"]) if m["kind"] == "tell")
    ask = next(m for m in harness.store.messages(writer["session_id"]) if m["kind"] == "ask")
    assert (tell["meta"]["from_task"], tell["meta"]["to_task"]) == (
        scout["task_id"],
        writer["task_id"],
    )
    assert (ask["meta"]["from_task"], ask["meta"]["to_task"], ask["meta"]["question_id"]) == (
        writer["task_id"],
        scout["task_id"],
        question,
    )
    listed = json.loads(harness.store.get(f"xchg:{scout['task_id']}"))
    assert listed == [tell["id"], ask["id"]]
    assert json.loads(harness.store.get(f"xchg:{writer['task_id']}")) == [tell["id"], ask["id"]]
    peers = [m["meta"] for m in harness.store.messages(router) if m["kind"] == "peer"]
    assert [(p["from_task"], p["to_task"]) for p in peers] == [
        (scout["task_id"], writer["task_id"]),
        (writer["task_id"], scout["task_id"]),
    ]


async def test_an_ambiguous_recipient_gets_no_to_task_and_lists_keep_the_latest(harness):
    runtime = await harness.connect()
    router = harness.router_id
    await owner_turn(harness, runtime, "Export it.")
    one = await spawn(runtime, router, "Export the report")
    await owner_turn(harness, runtime, "Import it.")
    two = await spawn(runtime, router, "Import the report")
    await owner_turn(harness, runtime, "And sort it.")
    await call_tool(
        runtime,
        "message_session",
        router,
        session_id=two["session_id"],
        text="Also sort.",
        task="new",
        title="Sort the rows",
        why_new="more",
        earlier="keep",
    )
    await call_tool(
        runtime, "tell_session", one["session_id"], session_id=two["session_id"], text="FYI."
    )
    tell = next(m for m in harness.store.messages(one["session_id"]) if m["kind"] == "tell")
    assert "to_task" not in tell["meta"], "two open tasks, neither of its request"
    harness.store.set(f"xchg:{one['task_id']}", json.dumps(list(range(500))))
    runtime.teams_lite.exchanged(900, {"from_task": one["task_id"]})
    assert json.loads(harness.store.get(f"xchg:{one['task_id']}")) == [*range(1, 500), 900]


def test_every_task_creation_says_its_request_or_why_none():
    """A new tasks.create( path must stamp its request or say why it has none."""
    source = Path(__file__).parents[1] / "src" / "chiefofstaff"
    missing = []
    for path in source.glob("*.py"):
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "self.tasks.create(" not in line:
                continue
            call = "\n".join(lines[index : index + 12])
            call = call[: call.find(")\n") + 1] if ")\n" in call else call
            before = "\n".join(lines[max(0, index - 3) : index])
            if "request=" not in call and "No request:" not in before:
                missing.append(f"{path.name}:{index + 1}")
    assert not missing, missing


def teams_of(runtime) -> list[dict]:
    return runtime.snapshot()["teams"]


def parts(team) -> list[tuple[str, str]]:
    return [(member["name"], member["avatar"]) for member in team["members"]]


async def test_a_request_split_into_parts_is_one_team_named_from_its_words(harness):
    runtime = await harness.connect()
    router = harness.router_id
    request = await owner_turn(
        harness,
        runtime,
        "Ship checkout v2 for acme-shop: the API and the UI side by side, then end-to-end tests.",
    )
    api = await spawn(runtime, router, "Checkout API: the v2 endpoints")
    ui = await spawn(
        runtime, router, "Checkout UI: the v2 pages", works_with=["Checkout API: the v2 endpoints"]
    )
    tests = await spawn(
        runtime, router, "Checkout tests: end to end", after=[api["task_id"], ui["task_id"]]
    )
    [team] = teams_of(runtime)
    assert team["id"] == f"team-{request}-{api['task_id']}"
    assert team["request"]["id"] == request and team["request"]["text"].startswith("Ship checkout")
    assert team["goal"] == "Ship checkout v2 for acme-shop"
    assert parts(team) == [("API", "A"), ("UI", "U"), ("Tests", "T")]
    assert [m["task"] for m in team["members"]] == [api["task_id"], ui["task_id"], tests["task_id"]]
    assert [m["color"] for m in team["members"]] == ["c0", "c3", "c5"]
    assert sorted((e["kind"], e["from"], e["to"], e["state"]) for e in team["edges"]) == sorted(
        [
            ("after", api["task_id"], tests["task_id"], "pending"),
            ("after", ui["task_id"], tests["task_id"], "pending"),
        ]
    )
    assert team["seen"] is None and team["waiting"] == []
    assert runtime.snapshot()["past_teams"] == 0


async def bridge(harness, runtime, order: str) -> list[dict]:
    router = harness.router_id
    await owner_turn(harness, runtime, "Two pairs, then one piece joining them.")
    one = await spawn(runtime, router, "Alpha one")
    two = await spawn(runtime, router, "Alpha two", works_with=[one["task_id"]])
    if order == "bridge-last":
        three = await spawn(runtime, router, "Beta one")
        four = await spawn(runtime, router, "Beta two", after=[three["task_id"]])
        await spawn(runtime, router, "Gamma bridge", works_with=["Alpha two", "Beta two"])
    else:
        bridge = await spawn(runtime, router, "Gamma bridge", works_with=["Alpha two"])
        three = await spawn(runtime, router, "Beta one")
        four = await spawn(
            runtime, router, "Beta two", after=[three["task_id"]], works_with=[bridge["task_id"]]
        )
    assert two and four
    return teams_of(runtime)


async def test_a_bridge_task_makes_one_team_whichever_order(harness, tmp_path):
    from conftest import Harness

    runtime = await harness.connect()
    [team] = await bridge(harness, runtime, "bridge-last")
    assert len(team["members"]) == 5
    other = Harness(tmp_path / "other")
    runtime = await other.connect()
    [team] = await bridge(other, runtime, "bridge-first")
    assert len(team["members"]) == 5


async def test_what_is_not_a_deliberate_link_of_one_request_makes_no_team(harness):
    runtime = await harness.connect()
    router = harness.router_id
    await owner_turn(harness, runtime, "Export and import, separately.")
    await spawn(runtime, router, "Export the report")
    await spawn(runtime, router, "Import the report")
    assert teams_of(runtime) == [], "two tasks of one request, no link"
    await owner_turn(harness, runtime, "Fix the token flow.")
    await spawn(runtime, router, "Rotate the API token", works_with=["Use the new token"])
    await owner_turn(harness, runtime, "Use the new token in the client.")
    # Its worker's wants: names this title (stale, another request): linked as before, no team.
    later = await spawn(
        runtime, router, "Use the new token", instructions="Use what Rotate the API token made."
    )
    assert runtime.peers.of(later["session_id"]), "workers linked as in 0.1.56"
    assert teams_of(runtime) == [], "a stale wants: or a title mentioned: no team"
    # All of a request's tasks in one worker: no team.
    await owner_turn(harness, runtime, "Plan then write.")
    plan = await spawn(runtime, router, "Plan the rollout")
    await call_tool(
        runtime,
        "message_session",
        router,
        session_id=plan["session_id"],
        task="new",
        text="Then write it.",
        title="Write the rollout",
        why_new="next",
        earlier="keep",
    )
    assert teams_of(runtime) == []
    # A worker's discovered wait links workers (as before), never a team.
    runtime.peers.waiting_on(later["session_id"], "I wait for Rotate the API token.")
    assert teams_of(runtime) == []


async def test_workers_of_two_requests_that_talk_are_no_team_and_a_held_question_comes_once(
    harness,
):
    runtime = await harness.connect()
    router = harness.router_id
    await owner_turn(harness, runtime, "Research billing.")
    expert = await spawn(runtime, router, "Billing research")
    await owner_turn(harness, runtime, "Fix the invoice bug.")
    asker = await spawn(runtime, router, "Invoice bug")
    await call_tool(
        runtime,
        "tell_session",
        expert["session_id"],
        session_id=asker["session_id"],
        text="Invoices live in billing.inv.",
    )
    asked = await call_tool(
        runtime,
        "ask_session",
        asker["session_id"],
        session_id=expert["session_id"],
        question="Which column?",
    )
    question = re.search(r"\((q\w+)\)", asked.text_result_for_llm).group(1)
    assert teams_of(runtime) == []
    runtime.update_session(asker["session_id"], status="idle")  # Its turn ended, to wait.
    harness.store.execute("UPDATE questions SET asked=asked-45*60 WHERE id=?", (question,))
    [held] = runtime.snapshot()["held_questions"]
    assert (held["id"], held["team"], held["minutes"], held["blocks"]) == (question, "", 45, True)
    said = await runtime.teams_lite.nudge(question)
    assert said == "Nudged “Billing research”: “Invoice bug” waits for its answer."
    assert runtime.snapshot()["held_questions"][0]["nudged"]


async def test_a_part_continued_outside_its_request_has_moved_out(harness, monkeypatch):
    runtime = await harness.connect()
    router = harness.router_id
    monkeypatch.setattr(runtime, "handoff_summary", _summary)
    monkeypatch.setattr(runtime, "continue_checks", lambda row, agent: None)
    await owner_turn(harness, runtime, "Export, then import.")
    one = await spawn(runtime, router, "Export the report")
    two = await spawn(runtime, router, "Import the report", after=[one["task_id"]])
    for made in (one, two):
        runtime.update_session(made["session_id"], status="idle")
    inside = await runtime.continue_on(one["session_id"], "copilot")
    [team] = teams_of(runtime)
    first = team["members"][0]
    assert (first["id"], first["task"], first["tasks"]) == (
        one["task_id"],
        inside["task_id"],
        [one["task_id"], inside["task_id"]],
    )
    assert first["name"] == "Report" and first["moved_out"] is None
    other = await owner_turn(harness, runtime, "Unrelated: docs.")
    runtime.update_session(two["session_id"], status="idle")
    outside = await runtime.continue_on(
        two["session_id"], "copilot", also="Docs.", also_request=other
    )
    [team] = teams_of(runtime)
    moved = team["members"][1]
    assert moved["task"] == two["task_id"]
    assert moved["moved_out"] == {
        "task": outside["task_id"],
        "title": "Import the report",
        "session": outside["session_id"],
    }
    assert stamp(harness, outside["task_id"]) is None


async def test_a_moved_out_part_keeps_its_team_with_what_waited_on_it(harness, monkeypatch):
    """Continue with… moves what waited on a part onto its continuation (outside the request):
    still a link to the part it was, so the team stays, the part moved out."""
    runtime = await harness.connect()
    router = harness.router_id
    monkeypatch.setattr(runtime, "handoff_summary", _summary)
    monkeypatch.setattr(runtime, "continue_checks", lambda row, agent: None)
    await owner_turn(harness, runtime, "Export, then import.")
    one = await spawn(runtime, router, "Sales report export")
    two = await spawn(runtime, router, "Warehouse import", after=[one["task_id"]])
    other = await owner_turn(harness, runtime, "Also archive.")
    runtime.update_session(one["session_id"], status="idle")
    made = await runtime.continue_on(
        one["session_id"], "copilot", also="Archive.", also_request=other
    )
    assert [d["id"] for d in runtime.tasks.deps(two["task_id"])] == [made["task_id"]]
    [team] = teams_of(runtime)
    assert [m["moved_out"]["task"] if m["moved_out"] else "" for m in team["members"]] == [
        made["task_id"],
        "",
    ]
    assert [(e["from"], e["to"], e["state"]) for e in team["edges"]] == [
        (one["task_id"], two["task_id"], "pending")
    ]


async def test_a_seen_mark_is_kept_as_given_first_look_per_key(harness):
    runtime = await harness.connect()
    team = "team-12-tabcdef0"
    first = runtime.teams_lite.seen(team, "k1")
    assert runtime.teams_lite.seen(team, "k1") == first, "another tab: the first look stands"
    dismissed = runtime.teams_lite.seen(team, "k1", dismissed=True)
    assert dismissed == {**first, "dismissed": True}
    again = runtime.teams_lite.seen(team, "k2")
    assert again["key"] == "k2" and not again["dismissed"], "a new key: a new look"
    for bad in [("team-x", "k1"), (team, "K 1"), (team, "")]:
        try:
            runtime.teams_lite.seen(*bad)
        except ValueError:
            continue
        raise AssertionError(bad)


async def test_past_teams_come_a_page_at_a_time_by_request(harness, monkeypatch):
    from chiefofstaff import teams_lite

    runtime = await harness.connect()
    router = harness.router_id
    made = []
    for n in range(3):
        await owner_turn(harness, runtime, f"Job {n}: two parts.")
        one = await spawn(runtime, router, f"Job {n} part one")
        two = await spawn(runtime, router, f"Job {n} part two", works_with=[one["task_id"]])
        made.append((one, two))
    assert len(teams_of(runtime)) == 3
    monkeypatch.setattr(teams_lite, "WINDOW_DAYS", 0)
    for one, two in made:
        for task in (one, two):
            runtime.tasks.move(task["task_id"], "done", result="Done.")
    harness.store.execute("UPDATE tasks SET updated_at='2020-01-01', ended_at='2020-01-01'")
    runtime.teams_lite.loaded = 0
    snapshot = runtime.snapshot()
    assert snapshot["teams"] == [] and snapshot["past_teams"] == 3
    page = runtime.teams_lite.past(limit=2)
    assert [t["goal"] for t in page["teams"]] == ["Job 2", "Job 1"] and page["next"]
    rest = runtime.teams_lite.past(page["next"], limit=2)
    assert [t["goal"] for t in rest["teams"]] == ["Job 0"]


async def test_a_teams_history_is_only_what_its_parts_handed_each_other(harness):
    runtime = await harness.connect()
    router = harness.router_id
    await owner_turn(harness, runtime, "Rotate the token and use it.")
    scout = await spawn(runtime, router, "Rotate the API token")
    writer = await spawn(runtime, router, "Use the new token", works_with=[scout["task_id"]])
    await owner_turn(harness, runtime, "Unrelated: billing.")
    other = await spawn(runtime, router, "Billing research")
    for n in range(3):  # A reused worker's exchanges about other work.
        await call_tool(
            runtime,
            "tell_session",
            other["session_id"],
            session_id=scout["session_id"],
            text=f"Billing note {n}.",
        )
    await call_tool(
        runtime,
        "tell_session",
        scout["session_id"],
        session_id=writer["session_id"],
        text="Handshake checks the signature; a bad one disconnects.",
    )
    asked = await call_tool(
        runtime,
        "ask_session",
        writer["session_id"],
        session_id=scout["session_id"],
        question="Thanks. Does /v2/cart return cents? The mock has both.",
    )
    question = re.search(r"\((q\w+)\)", asked.text_result_for_llm).group(1)
    [team] = teams_of(runtime)
    page = runtime.teams_lite.page(team["id"])
    talk = [i for i in page["items"] if i["kind"] in {"tell", "ask"}]
    assert [(i["kind"], i["from"], i["to"], i["title"], i["rest"]) for i in talk] == [
        (
            "ask",
            writer["task_id"],
            scout["task_id"],
            "Does /v2/cart return cents?",
            "The mock has both.",
        ),
        (
            "tell",
            scout["task_id"],
            writer["task_id"],
            "Handshake checks the signature",
            "A bad one disconnects.",
        ),
    ]
    assert talk[0]["question"] == question and talk[0]["status"] == "open"
    assert [i["kind"] for i in page["items"]][-1] == "request", "the request, oldest"
    [pair] = page["pairs"]
    assert pair["asked"] == 1 and pair["open"] and sum(t["n"] for t in pair["told"]) == 1
    assert {view["id"] for view in page["tasks"]} == {scout["task_id"], writer["task_id"]}
