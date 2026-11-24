"""Teams lite's scenes for the UI suites (ui_edges, ui_quality): the prototype's three teams on
a fake Copilot, made as the Router would make them for the owner's requests."""

# The scenes' words are the owner's and the workers', in Chinese and long, as they wrote them.
# ruff: noqa: RUF001, E501

import json
import time
from datetime import UTC, datetime, timedelta

from conftest import call_tool, settle


def owner_request(runtime, text: str, minutes_ago: int = 0) -> str:
    """An owner message the Router's turn answers: the tasks it makes now are that request's
    (Teams lite), as a real turn on it would have them."""
    router = runtime.store.get("router_id")
    message = runtime.add_message(router, "owner", text)
    if minutes_ago:
        at = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
        runtime.store.execute("UPDATE messages SET created_at=? WHERE id=?", (at, message["id"]))
    runtime.states[router].turn_request = str(message["id"])
    return str(message["id"])


def aged(runtime, task_id: str, minutes: int, ended: bool = False):
    at = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    runtime.store.execute("UPDATE tasks SET updated_at=? WHERE id=?", (at, task_id))
    if ended:
        runtime.store.execute("UPDATE tasks SET ended_at=? WHERE id=?", (at, task_id))


AUTOPILOT = (
    "设计 Chief of Staff 的 Autopilot 模式：先出方案，再定远程请求的安全规则和实例之间的点对点直连，"
    "最后做个可点的原型"
)
CHECKOUT = (
    "Ship checkout v2 for acme-shop: the API and the UI side by side, then end-to-end tests "
    "once I've OK'd both."
)
CUSTOM = "给 Chief of Staff 做用户自定义 UI：先出设计稿和文案，然后实现"


async def teams_lite_seeded(runtime, done_team: bool = True) -> dict:
    """Teams lite, as the prototype's phase-1 screens: a team at work in four parts (one done,
    two side by side that talk, a question waiting, one up next), a team waiting on the owner's
    OK (a result to review, tests after it), a done team, and a lone task (no team)."""
    store = runtime.store
    router = store.get("router_id")
    store.save_settings(store.settings().model_copy(update={"max_workers": 1000}))

    async def start(title, **extra):
        result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title,
                                 why_new="test", **extra)  # fmt: skip
        await settle(runtime)
        found = json.loads(result.text_result_for_llm)
        return found["session_id"], found["task_id"]

    async def tool(name, caller, **arguments):
        return (await call_tool(runtime, name, caller, **arguments)).text_result_for_llm

    def at_work(worker):
        session = runtime.states.handle(worker)
        session.emit("user.message", content=session.sent[-1])
        session.emit("assistant.turn_start")

    async def finish(worker, task, summary, accept=True):
        at_work(worker)
        await settle(runtime)
        await tool("report", worker, status="done", summary=summary)
        runtime.states.handle(worker).emit("session.idle")
        await settle(runtime)
        if accept:
            runtime.task_actions.accept(task)

    found: dict = {}
    if done_team:
        owner_request(runtime, CUSTOM, minutes_ago=200)
        design, design_task = await start("用户自定义 UI 设计稿")
        words, words_task = await start("用户自定义 UI 文案", works_with=[design_task])
        build, build_task = await start("用户自定义 UI 实现", after=[design_task, words_task])
        await tool("tell_session", words, session_id=design,
                   text="按钮一律以动词开头（“保存”“恢复默认”），设计稿里的“确定”请换成具体动作。")  # fmt: skip
        for worker, task, summary in ((design, design_task, "设计稿在 docs/ui/custom.fig。"),
                                      (words, words_task, "文案已定稿。"),
                                      (build, build_task, "已实现，测试全部通过。")):  # fmt: skip
            await finish(worker, task, summary)
        for task, minutes in ((design_task, 150), (words_task, 140), (build_task, 100)):
            aged(runtime, task, minutes, ended=True)
        found["custom"] = {"design": design, "words": words, "build": build}
    owner_request(runtime, CHECKOUT, minutes_ago=95)
    api, api_task = await start("Checkout API: the v2 endpoints")
    ui, ui_task = await start("Checkout UI: the v2 pages", works_with=[api_task])
    tests, _ = await start("Checkout tests: end to end", after=[api_task, ui_task],
                                    wait_for="accepted")  # fmt: skip
    for text in ("Declined cards return 402 with code card_declined; a wrong CVC is 402 with "
                 "code cvc_invalid. Both carry retry_after only when the bank says so.",
                 "Receipts on staging are v2 as well since this morning. The old /receipt route "
                 "answers 410."):  # fmt: skip
        await tool("tell_session", api, session_id=ui, text=text)
    asked = await tool("ask_session", ui, session_id=api,
                       question="Does /v2/cart return totals in cents or in dollars? The mock has both.")  # fmt: skip
    await tool("answer_session", api, question_id=asked.split("(")[1].split(")")[0],
               answer="Cents, always; the mock's dollars are a leftover.")  # fmt: skip
    await finish(api, api_task, "The v2 endpoints are live on staging.")
    await finish(ui, ui_task, "The v2 cart and payment pages, against the staging API; 14 new tests pass.",
                 accept=False)  # fmt: skip
    aged(runtime, api_task, 80, ended=True)
    aged(runtime, ui_task, 25)
    found["checkout"] = {"api": api, "ui": ui, "tests": tests, "ui_task": ui_task}
    owner_request(runtime, AUTOPILOT, minutes_ago=78)
    plan, plan_task = await start("Autopilot 模式的方案设计")
    rules, rules_task = await start("远程请求的安全规则", after=[plan_task])
    direct, direct_task = await start("实例之间的点对点直连", after=[plan_task],
                                      works_with=[rules_task])  # fmt: skip
    proto, _ = await start("Autopilot 可点击原型", after=[rules_task, direct_task])
    await tool("tell_session", plan, session_id=rules,
               text="威胁模型和信任边界写在 docs/autopilot/design.md 第 3 节。安全规则请以它为准；其中“对端”指另一台机器上的 Chief of Staff 实例。")  # fmt: skip
    await finish(plan, plan_task, "方案已完成：docs/autopilot/design.md。")
    aged(runtime, plan_task, 60, ended=True)
    for worker in (rules, direct):
        at_work(worker)
    await settle(runtime)
    await tool("tell_session", rules, session_id=direct,
               text="Autopilot 方案把来自对端的内容当请求处理，这会绕过主人。规则：对端发来的任何内容都只是信息；据此执行任何操作之前，必须由主人在 Needs you 里确认。")  # fmt: skip
    asked = await tool("ask_session", direct, session_id=rules,
                       question="对端实例发来的内容，能直接当作请求执行吗？还是必须先进主人的 Needs you？我这边握手完成后就要决定怎么分发。")  # fmt: skip
    question = asked.split("(")[1].split(")")[0]
    store.execute("UPDATE questions SET asked=? WHERE id=?", (time.time() - 12 * 60, question))
    store.execute("UPDATE tasks SET progress=? WHERE id=?",
                  ("正在写“先确认再执行”的规则；草稿在 docs/autopilot/security.md。", rules_task))  # fmt: skip
    # It ends its turn to wait for the answer: its task waits on it.
    waiting = runtime.states.handle(direct)
    waiting.emit("assistant.message", content="Waiting for the security rules' answer.")
    waiting.emit("session.idle")
    await settle(runtime)
    runtime.update_session(direct, status="idle")
    found["autopilot"] = {"plan": plan, "rules": rules, "direct": direct, "proto": proto,
                          "question": question, "rules_task": rules_task}  # fmt: skip
    runtime.states[router].turn_request = ""
    lone, _ = await start("修复决策卡重复和结果漏通知")
    at_work(lone)
    await settle(runtime)
    found["lone"] = lone
    runtime.publish_state()
    teams = {team["goal"]: team["id"] for team in runtime.snapshot()["teams"]}
    found["ids"] = teams
    return found


async def spawned(runtime, title, **extra):
    router = runtime.store.get("router_id")
    result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title,
                             why_new="test", **extra)  # fmt: skip
    await settle(runtime)
    found = json.loads(result.text_result_for_llm)
    return found["session_id"], found["task_id"]


async def held_unteamed(runtime) -> dict:
    """Two workers of two requests: one asked the other 34 minutes ago and waits (no team)."""
    owner_request(runtime, "Research how billing stores invoices.", minutes_ago=50)
    expert, _ = await spawned(runtime, "Billing research")
    owner_request(runtime, "Fix the invoice rounding bug.", minutes_ago=45)
    asker, asker_task = await spawned(runtime, "Invoice rounding bug")
    runtime.states[runtime.store.get("router_id")].turn_request = ""
    asked = await call_tool(runtime, "ask_session", asker, session_id=expert,
                            question="Which column holds the invoice total, and is it in cents?")  # fmt: skip
    question = asked.text_result_for_llm.split("(")[1].split(")")[0]
    runtime.store.execute(
        "UPDATE questions SET asked=? WHERE id=?", (time.time() - 34 * 60, question)
    )
    runtime.update_session(asker, status="idle")
    runtime.publish_state()
    return {"expert": expert, "asker": asker, "asker_task": asker_task, "question": question}


async def moved_out(runtime) -> dict:
    """A request in two parts; one part goes on in a new worker for work of another request
    too (Continue with…, its card's message of that request): it has moved out."""

    async def summary(row, agent):
        return f"Go on with {row['title']}."

    owner_request(
        runtime, "Export the sales report, then import it into the warehouse.", minutes_ago=30
    )
    export, export_task = await spawned(runtime, "Sales report export")
    load, _ = await spawned(runtime, "Warehouse import", after=[export_task])
    other = owner_request(runtime, "Also archive last year's reports.", minutes_ago=20)
    runtime.states[runtime.store.get("router_id")].turn_request = ""
    saved = runtime.handoff_summary, runtime.continue_checks
    runtime.handoff_summary, runtime.continue_checks = summary, lambda row, agent: None
    try:
        runtime.update_session(export, status="idle")
        made = await runtime.continue_on(export, "copilot", also="Archive last year's reports too.",
                                         also_request=other)  # fmt: skip
    finally:
        runtime.handoff_summary, runtime.continue_checks = saved
    await settle(runtime)
    runtime.publish_state()
    return {"export": export, "load": load, "export_task": export_task, "new": made}


async def big_team(runtime, parts: int = 11) -> str:
    """One request in many parts, each working with the one before (All N parts)."""
    owner_request(runtime, "Translate the help center into eleven languages: one worker a language.",
                  minutes_ago=15)  # fmt: skip
    languages = ["French", "German", "Spanish", "Italian", "Dutch", "Polish", "Czech",
                 "Swedish", "Danish", "Finnish", "Greek"][:parts]  # fmt: skip
    previous = ""
    for language in languages:
        _, task = await spawned(runtime, f"Help center in {language}",
                                **({"works_with": [previous]} if previous else {}))  # fmt: skip
        previous = task
    runtime.states[runtime.store.get("router_id")].turn_request = ""
    runtime.publish_state()
    return next(t["id"] for t in runtime.snapshot()["teams"] if t["goal"].startswith("Translate"))


async def past_teams(runtime, count: int) -> None:
    """Done teams of requests older than the current window (Past teams, a page at a time)."""
    store = runtime.store
    for number in range(count):
        request = owner_request(runtime, f"Old job {number}: two parts.", minutes_ago=60 * 24 * 30)
        _, one_task = await spawned(runtime, f"Old job {number} part one")
        _, two_task = await spawned(runtime, f"Old job {number} part two", works_with=[one_task])
        for task in (one_task, two_task):
            runtime.tasks.move(task, "done", result="Done.")
        old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        store.execute("UPDATE tasks SET updated_at=?, ended_at=? WHERE id IN (?, ?)",
                      (old, old, one_task, two_task))  # fmt: skip
        assert request
    runtime.states[store.get("router_id")].turn_request = ""
    runtime.teams_lite.loaded = 0  # Read again: their times changed under it.
    runtime.publish_state()


async def more_exchanges(runtime, found: dict, count: int = 6) -> None:
    """More notes between two parts at work (a history long enough for filters)."""
    rules, direct = found["autopilot"]["rules"], found["autopilot"]["direct"]
    for number in range(count):
        await call_tool(runtime, "tell_session", rules, session_id=direct,
                        text=f"规则第 {number + 1} 条已写进 docs/autopilot/security.md。请按它分发。")  # fmt: skip
    runtime.publish_state()
