"""Team formation, as a person would judge it (team quality): realistic owner requests, in
Chinese and English, through the Router's real tool paths (spawn_worker, message_session new and
continue, a busy worker's second worker, Compare, Continue with…, a refused start replayed, an
event turn), each with the teams it must make, or none. When unsure: no team."""

# ruff: noqa: RUF001, E501  (The owner's words, in Chinese, as they wrote them: on one line each.)

import json

from conftest import call_tool, settle
from test_fork import at_work, new_task
from test_teams_lite import _summary, event_turn, owner_turn, spawn, teams_of

from chiefofstaff.tools import TURN_REQUEST, SpawnWorker


def formation(runtime) -> list[list[str]]:
    """Every current team: its parts' titles (as they are now), in order. (Its name is
    test_team_names's.)"""
    rows = {task["id"]: task for task in runtime.tasks.listed(include_ended_since="2000")}
    found = []
    for team in teams_of(runtime):
        titles = []
        for member in team["members"]:
            task = rows.get(member["task"]) or runtime.tasks.get(member["task"])
            titles.append(task["title"] + (" (moved out)" if member["moved_out"] else ""))
        found.append(titles)
    return sorted(found)


def idle(runtime, *made):
    for item in made:
        runtime.update_session(item["session_id"], status="idle")


async def started(harness, runtime, text: str, parts: list[tuple[str, dict]]) -> list[dict]:
    """The owner asks; the Router's turn on it starts each part as given (title, extra)."""
    await owner_turn(harness, runtime, text)
    made = []
    for title, extra in parts:
        extra = {key: [made[i]["task_id"] for i in value] if key in {"after", "works_with"}
                 else value for key, value in extra.items()}  # fmt: skip
        made.append(await spawn(runtime, harness.router_id, title, **extra))
    return made


REFUND = "给 acme-shop 加上按单退款：后端出退款 API，订单页加退款按钮和确认弹窗，退款成功发邮件通知，最后补端到端测试。"
REFUND_PARTS = [
    ("退款 API：POST /v2/orders/{id}/refunds", {}),
    ("订单页：退款按钮和确认弹窗", {"after": [0]}),
    ("邮件通知：退款成功后发邮件", {"after": [0]}),
    ("端到端测试：退款的全流程", {"after": [1, 2]}),
]
CHECKOUT = "Ship checkout v2 for acme-shop: the API and the UI side by side, then end-to-end tests."
CHECKOUT_PARTS = [
    ("Checkout API: the v2 endpoints", {}),
    ("Checkout UI: the v2 pages", {"works_with": [0]}),
    ("Checkout tests: end to end", {"after": [0, 1]}),
]


async def test_f01_a_request_in_parts_in_chinese_is_one_team(harness):
    runtime = await harness.connect()
    await started(harness, runtime, REFUND, REFUND_PARTS)
    assert formation(runtime) == [[title for title, _ in REFUND_PARTS]]


async def test_f02_a_request_in_parts_in_english_is_one_team(harness):
    runtime = await harness.connect()
    await started(harness, runtime, CHECKOUT, CHECKOUT_PARTS)
    assert formation(runtime) == [[t for t, _ in CHECKOUT_PARTS]]


async def test_f03_do_x_then_y_is_a_team(harness):
    runtime = await harness.connect()
    await started(harness, runtime, "Write the migration for the invoices table, then switch "
                  "the billing API to it.", [("Invoices table migration", {}),
                                             ("Billing API on the invoices table", {"after": [0]})])  # fmt: skip
    assert formation(runtime) == [
        ["Invoices table migration", "Billing API on the invoices table"]
    ]  # fmt: skip


async def test_f04_do_x_and_y_separately_is_no_team(harness):
    runtime = await harness.connect()
    await started(harness, runtime, "Two unrelated things, separately: update the CLI README, "
                  "and bump CI to Python 3.13.", [("CLI README update", {}),
                                                  ("CI on Python 3.13", {})])  # fmt: skip
    assert formation(runtime) == []


async def test_f05_two_fixes_asked_together_in_chinese_are_no_team(harness):
    runtime = await harness.connect()
    await started(harness, runtime, "分别修两个问题：导出 CSV 时日期格式错了；设置页的保存按钮点了没反应。",
                  [("修复导出 CSV 的日期格式", {}), ("修复设置页保存按钮无响应", {})])  # fmt: skip
    assert formation(runtime) == []


async def test_f06_a_single_worker_request_is_no_team(harness):
    runtime = await harness.connect()
    await started(harness, runtime, "Rename the userId column to user_id in the reports "
                  "service.", [("Rename userId to user_id in reports", {})])  # fmt: skip
    assert formation(runtime) == []


async def test_f07_parts_all_on_one_worker_are_no_team(harness):
    """Two tasks of one request, the second given to the first one's worker: one worker."""
    runtime = await harness.connect()
    [first] = await started(harness, runtime, "Add CSV export, then a JSON one too.",
                            [("CSV export", {})])  # fmt: skip
    idle(runtime, first)
    await call_tool(runtime, "message_session", harness.router_id,
                    session_id=first["session_id"], task="new", title="JSON export",
                    text="Then add a JSON export.", why_new="test", earlier="keep")  # fmt: skip
    assert formation(runtime) == []


async def test_f08_an_unrelated_request_while_a_team_runs_stays_out(harness):
    runtime = await harness.connect()
    await started(harness, runtime, CHECKOUT, CHECKOUT_PARTS)
    await started(harness, runtime, "Separately: why is the staging disk full?",
                  [("Why the staging disk is full", {})])  # fmt: skip
    assert formation(runtime) == [[t for t, _ in CHECKOUT_PARTS]]


async def test_f09_a_request_that_builds_on_another_teams_work_makes_no_new_team(harness):
    """“When the refund API is done, use it in the admin panel too”: a request of its own,
    one part; the refund team is as it was (unsure: no team)."""
    runtime = await harness.connect()
    made = await started(harness, runtime, REFUND, REFUND_PARTS)
    await started(harness, runtime, "退款 API 好了以后，后台管理页也接上退款。",
                  [("后台管理页接入退款", {})])  # fmt: skip
    await spawn(runtime, harness.router_id, "后台退款的权限检查", after=[made[0]["task_id"]])
    assert formation(runtime) == [[t for t, _ in REFUND_PARTS]]


async def test_f10_a_follow_up_to_one_part_continues_it_in_its_team(harness):
    runtime = await harness.connect()
    made = await started(harness, runtime, REFUND, REFUND_PARTS)
    await owner_turn(harness, runtime, "订单页的确认弹窗再加一个金额输入框。")
    await call_tool(runtime, "message_session", harness.router_id,
                    session_id=made[1]["session_id"], task="continue",
                    text="确认弹窗再加一个金额输入框。")  # fmt: skip
    assert formation(runtime) == [[t for t, _ in REFUND_PARTS]]


async def test_f11_new_work_for_a_parts_worker_in_a_later_request_is_not_the_team(harness):
    """More work asked of a part's worker, as a task of its own, in a later message: a request
    of its own (one part), not a part of the earlier team (unsure: no team)."""
    runtime = await harness.connect()
    made = await started(harness, runtime, CHECKOUT, CHECKOUT_PARTS)
    idle(runtime, *made)
    await owner_turn(harness, runtime, "Also have the API worker write its changelog entry.")
    await call_tool(runtime, "message_session", harness.router_id,
                    session_id=made[0]["session_id"], task="new", title="Checkout API changelog",
                    text="Write the changelog entry.", why_new="test", earlier="keep")  # fmt: skip
    assert formation(runtime) == [[t for t, _ in CHECKOUT_PARTS]]


async def test_f12_a_re_plan_that_replaces_a_part_continues_it_in_its_team(harness):
    """“Skip the email, send an SMS instead”: the Router replaces the email part with an SMS
    part in its turn on that message. The SMS part is the team's (it does that part instead),
    not a part “moved out”."""
    runtime = await harness.connect()
    made = await started(harness, runtime, REFUND, REFUND_PARTS)
    await owner_turn(harness, runtime, "邮件通知不要了，改成退款成功发短信。")
    await spawn(runtime, harness.router_id, "短信通知：退款成功后发短信",
                replaces=[made[2]["task_id"]])  # fmt: skip
    titles = [t for t, _ in REFUND_PARTS]
    titles[2] = "短信通知：退款成功后发短信"
    assert formation(runtime) == [titles]


async def test_f13_a_busy_parts_second_worker_for_more_of_its_request_is_no_new_part(harness):
    """A part's worker is busy; the Router gives more of the same request to it, so a second
    worker takes it with its context: the team's parts stay as they were."""
    runtime = await harness.connect()
    store = runtime.store
    store.save_settings(store.settings().model_copy(update={"same_worker": True}))
    made = await started(harness, runtime, CHECKOUT, CHECKOUT_PARTS[:2])
    await at_work(runtime, harness, made[0]["session_id"])
    await new_task(runtime, harness, made[0]["session_id"], "Checkout API rate limits",
                   "Add rate limits too.", earlier="keep")  # fmt: skip
    await new_task(runtime, harness, made[0]["session_id"], "Checkout API docs",
                   "And its docs.", earlier="keep")  # fmt: skip
    assert formation(runtime) == [[t for t, _ in CHECKOUT_PARTS[:2]]]


async def test_f14_a_compare_is_two_tries_of_one_task_not_a_team(harness):
    runtime = await harness.connect()
    await owner_turn(harness, runtime, "Try the flaky login test fix on Copilot and Codex.")
    found = await call_tool(runtime, "spawn_worker", harness.router_id, title="Fix the flaky login test",
                            instructions="Fix it", why_new="test", agent="copilot",
                            compare_with=["codex"])  # fmt: skip
    assert found.result_type != "failure" or "Codex" in found.text_result_for_llm
    assert formation(runtime) == []


async def test_f15_continue_with_keeps_a_part_in_its_team(harness, monkeypatch):
    runtime = await harness.connect()
    monkeypatch.setattr(runtime, "handoff_summary", _summary)
    monkeypatch.setattr(runtime, "continue_checks", lambda row, agent: None)
    made = await started(harness, runtime, CHECKOUT, CHECKOUT_PARTS)
    idle(runtime, *made)
    await runtime.continue_on(made[1]["session_id"], "copilot")
    assert formation(runtime) == [[t for t, _ in CHECKOUT_PARTS]]


async def test_f16_continue_with_other_work_too_moves_a_part_out(harness, monkeypatch):
    runtime = await harness.connect()
    monkeypatch.setattr(runtime, "handoff_summary", _summary)
    monkeypatch.setattr(runtime, "continue_checks", lambda row, agent: None)
    made = await started(harness, runtime, CHECKOUT, CHECKOUT_PARTS)
    other = await owner_turn(harness, runtime, "Unrelated: the docs site.")
    idle(runtime, *made)
    await runtime.continue_on(made[1]["session_id"], "copilot", also="Docs.", also_request=other)
    titles = [t for t, _ in CHECKOUT_PARTS]
    titles[1] += " (moved out)"
    assert formation(runtime) == [titles]


async def test_f17_a_refused_start_replayed_later_joins_its_own_team(harness, monkeypatch):
    runtime = await harness.connect()
    made = await started(harness, runtime, CHECKOUT, CHECKOUT_PARTS[:1])
    first = runtime.store.get(f"req:{made[0]['task_id']}")
    monkeypatch.setattr(runtime, "unready", lambda agent, why: {"status": "", "said": why})
    params = SpawnWorker(title="Checkout UI: the v2 pages", instructions="UI",
                         why_new="test", works_with=[made[0]["task_id"]])  # fmt: skip
    token = TURN_REQUEST.set(first)
    try:
        runtime.agent_refused_card("codex", "Codex is not signed in.", params.model_dump(),
                                   request=TURN_REQUEST.get())  # fmt: skip
    finally:
        TURN_REQUEST.reset(token)
    await owner_turn(harness, runtime, "Something else entirely.")
    [(key, raw)] = harness.store.items("agentrefusal:").items()
    found = json.loads(raw) | {"agent": "copilot"}
    await runtime.resume_refused(key, found, after="started")
    assert formation(runtime) == [
        ["Checkout API: the v2 endpoints", "Checkout UI: the v2 pages"]
    ]  # fmt: skip


async def test_f18_a_later_part_started_on_an_event_joins_its_request(harness):
    """“Draft the spec; when it's done, build the prototype”: the Router starts the spec now and
    the prototype in its turn on the spec's report (an event, no owner message), after the
    spec. A part of the same request: the team has both."""
    runtime = await harness.connect()
    [spec] = await started(harness, runtime, "先写 Autopilot 的方案，方案好了再做个可点的原型。",
                           [("Autopilot 方案", {})])  # fmt: skip
    await event_turn(harness, runtime, "“Autopilot 方案” finished.")
    await spawn(runtime, harness.router_id, "Autopilot 可点击原型", after=[spec["task_id"]])
    assert formation(runtime) == [["Autopilot 方案", "Autopilot 可点击原型"]]


async def test_f19_an_event_turns_task_that_names_no_requests_task_is_no_team(harness):
    runtime = await harness.connect()
    await started(harness, runtime, CHECKOUT, CHECKOUT_PARTS[:2])
    await event_turn(harness, runtime, "CI failed on acme/infra#9.")
    await spawn(runtime, harness.router_id, "Fix CI on acme/infra#9")
    assert formation(runtime) == [[t for t, _ in CHECKOUT_PARTS[:2]]]


async def test_f20_a_part_named_before_it_is_made_joins_when_made(harness):
    runtime = await harness.connect()
    await owner_turn(harness, runtime, "Generate a token and write it into the config.")
    await spawn(runtime, harness.router_id, "Token writer: put the token in config",
                works_with=["Token scout"])  # fmt: skip
    await spawn(runtime, harness.router_id, "Token scout: generate a token")
    assert formation(runtime) == [
        ["Token writer: put the token in config", "Token scout: generate a token"]
    ]  # fmt: skip


async def test_f21_only_the_linked_parts_of_a_request_are_its_team(harness):
    runtime = await harness.connect()
    await started(harness, runtime, "这周的三件事：先修登录页，再给登录页补截图测试；另外整理一下 README。",
                  [("修复登录页", {}), ("登录页截图测试", {"after": [0]}), ("整理 README", {})])  # fmt: skip
    assert formation(runtime) == [["修复登录页", "登录页截图测试"]]


async def test_f22_workers_of_two_requests_that_talk_are_no_team(harness):
    runtime = await harness.connect()
    [one] = await started(harness, runtime, "Research how billing stores invoices.",
                          [("Billing research", {})])  # fmt: skip
    [two] = await started(harness, runtime, "Fix the invoice rounding bug.",
                          [("Invoice rounding bug", {})])  # fmt: skip
    await call_tool(runtime, "ask_session", two["session_id"], session_id=one["session_id"],
                    question="Which column holds the total?")  # fmt: skip
    assert formation(runtime) == []


async def test_f23_help_me_x_then_y_in_chinese_is_a_team(harness):
    runtime = await harness.connect()
    await started(harness, runtime, "帮我把登录页改成新的设计稿，然后把注册页也改了，最后补上截图测试",
                  [("登录页换新设计", {}), ("注册页换新设计", {"works_with": [0]}),
                   ("登录和注册页截图测试", {"after": [0, 1]})])  # fmt: skip
    [titles] = formation(runtime)
    assert titles == ["登录页换新设计", "注册页换新设计", "登录和注册页截图测试"]


async def test_f24_a_failed_part_replaced_in_its_own_request_continues_in_its_team(harness):
    runtime = await harness.connect()
    made = await started(harness, runtime, CHECKOUT, CHECKOUT_PARTS)
    runtime.tasks.move(made[1]["task_id"], "failed")
    await spawn(runtime, harness.router_id, "Checkout UI: the v2 pages, again",
                replaces=[made[1]["task_id"]])  # fmt: skip
    titles = [t for t, _ in CHECKOUT_PARTS]
    titles[1] = "Checkout UI: the v2 pages, again"
    assert formation(runtime) == [titles]
    await settle(runtime)
