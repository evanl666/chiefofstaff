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
    result = await call_tool(
        runtime, "spawn_worker", router, title=title, instructions=title, why_new="test", **extra
    )
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
        runtime, router, "Checkout UI: the v2 pages",
        works_with=["checkout API:  the v2 endpoints", old["task_id"]],
    )  # fmt: skip
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
        runtime, "message_session", router, session_id=made["session_id"], earlier="keep",
        text="Also the import.", task="new", title="Import the report", why_new="more",
    )  # fmt: skip
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
            SpawnWorker(title="Try the cache", instructions="Try", why_new="t"), router,
            ["copilot", "copilot"], TURN_REQUEST.get(),
        )  # fmt: skip
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
        runtime.agent_refused_card("codex", "Codex is not signed in.", params.model_dump(),
                                   request=TURN_REQUEST.get())  # fmt: skip
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
        session_id=made["session_id"], text="Also the import.", task="new",
        title="Import the report", why_new="more", earlier="keep",
    ).model_dump()  # fmt: skip
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
    record = {"agent": "codex", "params": {"title": "Export"}, "request": first, "replay": replay,
              "worker": made["session_id"], "text": "Sort them."}  # fmt: skip
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
    mixed = await runtime.continue_on(two["session_id"], "copilot", also="Docs too.",
                                      also_request=other)  # fmt: skip
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
    await call_tool(runtime, "tell_session", scout["session_id"], session_id=writer["session_id"],
                    text="The token is in vault:api/v2.")  # fmt: skip
    asked = await call_tool(runtime, "ask_session", writer["session_id"],
                            session_id=scout["session_id"], question="Which scope?")  # fmt: skip
    question = re.search(r"\((q\w+)\)", asked.text_result_for_llm).group(1)
    tell = next(m for m in harness.store.messages(scout["session_id"]) if m["kind"] == "tell")
    ask = next(m for m in harness.store.messages(writer["session_id"]) if m["kind"] == "ask")
    assert (tell["meta"]["from_task"], tell["meta"]["to_task"]) == (
        scout["task_id"], writer["task_id"]
    )  # fmt: skip
    assert (ask["meta"]["from_task"], ask["meta"]["to_task"], ask["meta"]["question_id"]) == (
        writer["task_id"], scout["task_id"], question
    )  # fmt: skip
    listed = json.loads(harness.store.get(f"xchg:{scout['task_id']}"))
    assert listed == [tell["id"], ask["id"]]
    assert json.loads(harness.store.get(f"xchg:{writer['task_id']}")) == [tell["id"], ask["id"]]
    peers = [m["meta"] for m in harness.store.messages(router) if m["kind"] == "peer"]
    assert [(p["from_task"], p["to_task"]) for p in peers] == [
        (scout["task_id"], writer["task_id"]), (writer["task_id"], scout["task_id"])
    ]  # fmt: skip


async def test_an_ambiguous_recipient_gets_no_to_task_and_lists_keep_the_latest(harness):
    runtime = await harness.connect()
    router = harness.router_id
    await owner_turn(harness, runtime, "Export it.")
    one = await spawn(runtime, router, "Export the report")
    await owner_turn(harness, runtime, "Import it.")
    two = await spawn(runtime, router, "Import the report")
    await owner_turn(harness, runtime, "And sort it.")
    await call_tool(
        runtime, "message_session", router, session_id=two["session_id"],
        text="Also sort.", task="new", title="Sort the rows", why_new="more", earlier="keep",
    )  # fmt: skip
    await call_tool(runtime, "tell_session", one["session_id"], session_id=two["session_id"],
                    text="FYI.")  # fmt: skip
    tell = next(m for m in harness.store.messages(one["session_id"]) if m["kind"] == "tell")
    assert "to_task" not in tell["meta"], "two open tasks, neither of its request"
    harness.store.set(f"xchg:{one['task_id']}", json.dumps(list(range(500))))
    runtime.teams.exchanged(900, {"from_task": one["task_id"]})
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
