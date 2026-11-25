"""A busy sidebar for the UI suites (ui_edges round_sidebar_flat): workers in every state, a
second worker started from a busy one's conversation, check-backs (within the hour and later),
one that waits on a peer, a team of three and a Compare, on a fake Copilot (the Compare's
other side on the stand-in Codex, which the caller turns on)."""

import json

from conftest import call_tool, settle
from teams_scene import owner_request

REQUEST = (
    "Run the reach experiment: build the model releases, train the lower-weight twins on them "
    "and raise unique reach against the baseline."
)


async def busy_sidebar(runtime, compare: bool = True) -> dict:
    """The scene's workers by role: idle (a list), build, train, reach (the team of three),
    thresholds (needs you), nightly (checks back within the hour), busy and fork (its second
    worker), and the Compare's two sides (`pair`, Copilot's first)."""
    from copilot.tools import ToolInvocation

    store = runtime.store
    router = store.get("router_id")
    old = store.settings()
    new = old.model_copy(update={"max_workers": 1000, "fork_busy": True})
    store.save_settings(new)
    await runtime.settings_changed(old, new)

    async def start(title, **extra):
        result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title,
                                 why_new="test", **extra)  # fmt: skip
        await settle(runtime)
        found = json.loads(result.text_result_for_llm)
        return found["session_id"], found["task_id"]

    def at_work(worker):
        session = runtime.states.handle(worker)
        session.emit("user.message", content=session.sent[-1])
        session.emit("assistant.turn_start")

    async def rest(worker):
        runtime.states.handle(worker).emit("session.idle")
        await settle(runtime)
        runtime.update_session(worker, status="idle")

    async def finish(worker, task, summary, accept):
        at_work(worker)
        await settle(runtime)
        await call_tool(runtime, "report", worker, status="done", summary=summary)
        await rest(worker)
        if accept:
            runtime.task_actions.accept(task)

    found: dict = {"idle": []}
    for title in ("Tidy the README", "Fix the flaky login test", "Rename the export flag"):
        worker, task = await start(title)
        await finish(worker, task, "Done; nothing else to do.", accept=True)
        found["idle"].append(worker)
    # A team of three (one request, its parts linked): one at work, one that starts once that
    # one is done (it waits on it), one checking back in a few hours.
    owner_request(runtime, REQUEST, minutes_ago=40)
    build, build_task = await start("Build model releases")
    train, _ = await start("Train lower-weight twins", after=[build_task])
    reach, _ = await start("Raise unique reach vs baseline", works_with=[build_task])
    runtime.states[router].turn_request = ""
    at_work(build)
    await settle(runtime)
    runtime.update_session(build, status="working")
    await call_tool(runtime, "check_back", reach, minutes=150,
                    what="whether the reach backfill finished")  # fmt: skip
    await rest(reach)
    found |= {"build": build, "train": train, "reach": reach}
    # A result to accept: it needs the owner.
    thresholds, task = await start("Add 1%-over-baseline thresholds")
    await finish(thresholds, task, "Thresholds added; 12 tests pass.", accept=False)
    found["thresholds"] = thresholds
    # Monitoring: it checks back within the hour.
    nightly, _ = await start("Watch the nightly export")
    await call_tool(runtime, "check_back", nightly, minutes=12,
                    what="whether the nightly export finished")  # fmt: skip
    await rest(nightly)
    found["nightly"] = nightly
    # Busy (in a turn, two tasks open): its new task goes to a second worker with its
    # conversation.
    busy, _ = await start("Answer a teammate: how revenue is counted")
    at_work(busy)
    await settle(runtime)
    runtime.update_session(busy, status="working")
    await call_tool(runtime, "message_session", router, session_id=busy, task="new",
                    why_new="test", earlier="keep", title="Check the revenue query",
                    text="Check the revenue query too.")  # fmt: skip
    told = await call_tool(runtime, "message_session", router, session_id=busy, task="new",
                           why_new="test", title="Count accounts using source X",
                           text="Count the accounts that use source X.")  # fmt: skip
    found |= {"busy": busy, "fork": json.loads(told.text_result_for_llm)["session_id"]}
    await settle(runtime)
    if compare:
        spawn = runtime.toolset.spawn_on(["codex"])
        made = await spawn.handler(
            ToolInvocation(session_id=router, tool_call_id="t", tool_name="spawn_worker",
                           arguments={"title": "Speed up the importer", "why_new": "t",
                                      "instructions": "Make the importer twice as fast.",
                                      "agent": "copilot", "compare_with": ["codex"]})
        )  # fmt: skip
        found["pair"] = [w["session_id"] for w in json.loads(made.text_result_for_llm)["workers"]]
        await settle(runtime)
    runtime.publish_state()
    return found
