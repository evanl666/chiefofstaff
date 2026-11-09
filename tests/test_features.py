import asyncio
import json
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import Harness, call_tool, env, said, settle, task_of

from chiefofstaff import catalog
from chiefofstaff.digest import digest
from chiefofstaff.events import describe_step, describe_tool
from chiefofstaff.models import DraftDecision, Settings


async def spawn(runtime, router, title, **extra):
    extra.setdefault("instructions", title)
    result = await call_tool(runtime, "spawn_worker", router, title=title, **extra)
    return json.loads(result.text_result_for_llm)


async def test_a_worker_closes_itself_when_its_pr_is_finished(harness):
    runtime = await harness.connect()
    worker_id = (await spawn(runtime, harness.router_id, "Fix"))["session_id"]
    harness.own_pull("acme/app", 7)
    await call_tool(
        runtime,
        "report",
        worker_id,
        status="done",
        summary="Opened https://github.com/acme/app/pull/7",
    )
    await settle(runtime)  # It follows the PR in the background.
    task = task_of(harness, worker_id)
    # Accepted (merging is not accepting, E28): it still follows its PR until that is over.
    runtime.task_actions.accept(task)
    await runtime.on_thread(
        "github", "github:acme/app#7", ["github:state:merged"], "The PR was merged."
    )
    runtime.unfollow("github:acme/app#7", finished=True, merged=True)
    assert runtime.tasks.get(task)["status"] == "done"
    assert not harness.store.session(worker_id)["closed_at"], "it handles the news first"
    await call_tool(runtime, "report", worker_id, status="done", summary="It was merged.")
    assert runtime.tasks.get(task)["status"] == "done", "and does not go back to review"
    harness.session(worker_id).emit("session.idle")
    await settle(runtime)
    assert harness.store.session(worker_id)["closed_at"]


async def test_quiet_finished_workers_are_tidied_up(harness):
    runtime = await harness.connect()
    done = (await spawn(runtime, harness.router_id, "Done"))["session_id"]
    following = (await spawn(runtime, harness.router_id, "Following"))["session_id"]
    asking = (await spawn(runtime, harness.router_id, "Asking"))["session_id"]
    harness.own_pull("a/b", 1)
    await call_tool(runtime, "report", done, status="done", summary="ok")
    await call_tool(
        runtime, "report", following, status="done", summary="Opened https://github.com/a/b/pull/1"
    )
    await call_tool(runtime, "report", asking, status="question", summary="which db?")
    await settle(runtime)
    assert await runtime.workers.tidy(time.time() + 3600) == []
    assert await runtime.workers.tidy(time.time() + 2 * 86400) == [], "results to accept"
    runtime.task_actions.accept(task_of(harness, done))
    assert await runtime.workers.tidy(time.time() + 2 * 86400) == [done]


async def test_dependent_tasks_start_with_the_results_they_need(harness):
    runtime = await harness.connect()
    first = (await spawn(runtime, harness.router_id, "Design schema"))["session_id"]
    second = await spawn(runtime, harness.router_id, "Write migration", after=[first])
    assert second["status"] == "queued" and "Starts automatically" in second["note"]
    second_id = second["session_id"]
    await runtime.workers.pump()
    assert harness.store.session(second_id)["activity"] == "waiting for “Design schema”"
    assert second_id not in harness.client.sessions

    await call_tool(runtime, "report", first, status="done", summary="Schema: users(id, email)")
    await settle(runtime)
    prompt = harness.session(second_id).sent[0]
    second_task = task_of(harness, second_id)
    assert second["task_id"] == second_task
    assert prompt.startswith(
        f'<message from="router" type="task" title="Write migration" task="{second_task}">'
    )
    design = task_of(harness, first)
    assert f"- Task {design} “Design schema” (review): Schema: users(id, email)" in prompt
    assert harness.store.one("SELECT status FROM tasks WHERE id=?", (second_task,))["status"] == (
        "running"
    )
    refused = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="X", instructions="x", after=["nope"]
    )
    assert refused.result_type == "failure"


async def test_a_task_can_wait_for_the_owners_check_of_another(harness):
    runtime = await harness.connect()
    api = await spawn(runtime, harness.router_id, "Design the API")
    client = await spawn(runtime, harness.router_id, "Write the client", after=[api["task_id"]],
                         wait_for="accepted")  # fmt: skip
    assert "once the owner accepted" in client["note"]
    await call_tool(runtime, "report", api["session_id"], status="done", summary="GET /orders")
    await settle(runtime)
    row = harness.store.session(client["session_id"])
    assert row["status"] == "queued", "a reported result is not enough"
    assert row["activity"] == "waiting for your review of “Design the API”"
    runtime.task_actions.accept(api["task_id"])
    await settle(runtime)
    assert harness.session(client["session_id"]).sent, "accepted: it starts"


async def test_models_per_role_task_and_session(tmp_path):
    settings = Settings(model="smart-model", role_models={"router": "fast-model"})
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    assert harness.session(harness.router_id).options["model"] == "fast-model"
    worker = await spawn(runtime, harness.router_id, "A")
    assert harness.session(worker["session_id"]).options["model"] == "smart-model"
    cheap = await spawn(runtime, harness.router_id, "B", model="fast-model")
    assert harness.session(cheap["session_id"]).options["model"] == "fast-model"
    unknown = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="C", instructions="c", model="gpt-0"
    )
    assert "Unknown model" in unknown.text_result_for_llm

    view = await runtime.set_session_model(worker["session_id"], "fast-model")
    assert view["model"] == "fast-model" and view["model_choice"] == "fast-model"
    assert harness.session(worker["session_id"]).models == ["fast-model"]
    listed = json.loads(
        (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    )
    assert listed["models"] == ["auto", "fast-model", "smart-model"]


async def test_thinking_effort_and_context_follow_what_the_model_supports(tmp_path):
    settings = Settings(model="smart-model", role_efforts={"worker": "high", "router": "max"})
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    router = harness.session(harness.router_id).options
    assert "reasoning_effort" not in router, "max is not offered: Copilot would refuse it"
    assert "context_tier" not in router
    worker_id = (await spawn(runtime, harness.router_id, "Hard bug"))["session_id"]
    assert harness.session(worker_id).options["reasoning_effort"] == "high"
    quick = (await spawn(runtime, harness.router_id, "Chore", effort="low"))["session_id"]
    assert harness.session(quick).options["reasoning_effort"] == "high", "the owner's is least"
    plain = (await spawn(runtime, harness.router_id, "Plain", model="fast-model"))["session_id"]
    options = harness.session(plain).options
    assert "reasoning_effort" not in options and "context_tier" not in options

    view = await runtime.set_session_model(worker_id, "", "low", "long_context")
    assert (view["effort"], view["effort_choice"], view["context"]) == (
        "low",
        "low",
        "long_context",
    )
    assert harness.session(worker_id).tunings[-1] == {
        "reasoning_effort": "low",
        "context_tier": "long_context",
    }
    for effort, context, error in [
        ("max", "", "efforts: low, medium, high"),
        ("", "long_context", "no long-context"),
    ]:
        with pytest.raises(ValueError, match=error):
            await runtime.set_session_model(plain, "fast-model" if context else "", effort, context)


async def test_sessions_on_the_copilot_default_model_get_its_options(harness):
    runtime = await harness.connect()
    router = harness.router_id
    assert "reasoning_effort" not in harness.session(router).options, "default not known yet"
    harness.session(router).emit("assistant.usage", model="fast-model", parent_tool_call_id="t")
    harness.session(router).emit("assistant.usage", model="smart-model", cost=1)
    await settle(runtime)
    view = runtime.view(harness.store.session(router))
    assert (view["model"], view["model_choice"], view["effort"]) == ("smart-model", "", "medium")
    plain = (await spawn(runtime, router, "Plain"))["session_id"]
    assert "model" not in harness.session(plain).options, "nothing chosen: Copilot's default"

    await runtime.set_session_model(router, "", "high")
    assert harness.session(router).models[-1] == "smart-model"
    assert harness.session(router).tunings[-1] == {
        "reasoning_effort": "high",
        "context_tier": "default",
    }
    await runtime.set_session_model(router, "", "")
    assert harness.session(router).tunings[-1]["reasoning_effort"] == "medium", "back to default"
    harness.session(router).emit("assistant.usage", model="smart-model-pinned", cost=1)
    await settle(runtime)
    assert harness.store.get("copilot_default_model") == "smart-model", "a pinned session"

    hard = (await spawn(runtime, router, "Hard", effort="high"))["session_id"]
    options = harness.session(hard).options
    assert options["model"] == "smart-model" and options["reasoning_effort"] == "high"
    result = await call_tool(
        runtime,
        "spawn_worker",
        router,
        title="Odd",
        instructions="o",
        effort="max",
        why_new="test",
    )
    assert (
        "Effort max is not offered by this model (low, medium, high)" in result.text_result_for_llm
    )


async def test_spawning_next_to_related_workers_needs_a_reason(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    harness = Harness(tmp_path / "data", Settings(projects=[str(project)]))
    runtime = await harness.connect()
    router = harness.router_id
    first = (await spawn(runtime, router, "Add login", project=str(project)))["session_id"]
    await call_tool(runtime, "report", first, status="done", summary="Login added.")
    await runtime.workers.close(first)

    refused = await call_tool(
        runtime,
        "spawn_worker",
        router,
        title="Explain README",
        instructions="x",
        project=str(project),
    )
    assert (
        refused.result_type == "failure" and "Related workers exist" in refused.text_result_for_llm
    )
    assert (
        first in refused.text_result_for_llm and '"state": "closed"' in refused.text_result_for_llm
    )
    assert len(harness.store.all("SELECT id FROM sessions WHERE role='worker'")) == 1

    dependent = await spawn(
        runtime, router, "Deploy", instructions="ship", project=str(project), after=[first]
    )
    assert dependent["session_id"], "a dependency is related on purpose"
    second = await spawn(
        runtime,
        router,
        "Logout",
        project=str(project),
        why_new="an independent change for its own PR",
    )
    notices = [m["text"] for m in harness.store.messages(router) if m["kind"] == "notice"]
    assert notices[-1] == "Started “Logout” · new because an independent change for its own PR"
    assert harness.store.session(second["session_id"])["project"] == str(project.resolve())

    research = (await spawn(runtime, router, "Research OpenHands agent"))["session_id"]
    refused = await call_tool(
        runtime, "spawn_worker", router, title="OpenHands history", instructions="its history"
    )
    assert research in refused.text_result_for_llm
    await call_tool(runtime, "message_session", router, session_id=research, text="its history")
    assert harness.store.messages(router)[-1]["text"] == "Continued in “Research OpenHands agent”"


async def test_the_router_hears_about_related_workers(harness):
    runtime = await harness.connect()
    router = harness.router_id
    worker_id = (await spawn(runtime, router, "研究 OpenHands 源码"))["session_id"]
    await call_tool(runtime, "report", worker_id, status="done", summary="源码解读已完成。")
    await runtime.workers.close(worker_id)
    await runtime.owner_send(router, "你去研究一下openhands的开发历史")
    sent = harness.session(router).sent[-1]
    assert sent.startswith(
        said("你去研究一下openhands的开发历史")
        + '\n\n<message from="system" type="context">\nEarlier workers'
    )
    card = f"“研究 OpenHands 源码” ({worker_id}, closed). Task: 研究 OpenHands 源码"
    assert f"{card} Result: 源码解读已完成。" in sent
    assert "send it to that worker with message_session" in sent
    assert harness.store.messages(router)[-1]["text"] == "你去研究一下openhands的开发历史"
    await runtime.owner_send(router, "book a flight to Tokyo")
    unrelated = harness.session(router).sent[-1]
    assert (
        unrelated.startswith(said("book a flight to Tokyo")) and "Earlier workers" not in unrelated
    )


async def test_usage_is_recorded_per_call_and_summed_by_range(harness):
    runtime = await harness.connect()
    router = harness.session(harness.router_id)
    router.emit(
        "assistant.usage",
        model="fast-model",
        cost=1.0,
        input_tokens=1000,
        output_tokens=50,
        copilot_usage=SimpleNamespace(total_nano_aiu=2.5e9),
    )
    await settle(runtime)
    now = time.time()
    usage = harness.store.usage(now - 60, now + 60)
    assert usage["total"]["credits"] == 2.5 and usage["total"]["premium"] == 1.0
    assert usage["by_role"][0]["role"] == "router"
    assert usage["by_session"][0]["title"] == "Router"
    assert harness.store.usage(now + 60, now + 120)["total"]["calls"] == 0
    assert runtime.view(harness.store.session(harness.router_id))["credits"] == 2.5
    # A worker by its work's name, as everywhere (bug bash: its first prompt's).
    worker = (await spawn(runtime, harness.router_id, "Benchmark words"))["session_id"]
    await call_tool(runtime, "report", worker, status="done", summary="Done.")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    task="new", title="Write the usage docs", text="Write them",
                    why_new="test")  # fmt: skip
    harness.session(worker).emit("assistant.usage", model="fast-model", cost=1.0,
                                 input_tokens=10, output_tokens=5,
                                 copilot_usage=SimpleNamespace(total_nano_aiu=1e9))  # fmt: skip
    await settle(runtime)
    rows = runtime.usage(now - 60, time.time() + 60)["by_session"]
    assert {row["title"] for row in rows} >= {"Router", "Write the usage docs"}


async def test_daily_digest_once_a_day_and_only_when_useful(harness):
    runtime = await harness.connect()
    now = time.time()
    assert digest(runtime, now - 86400, now) == ""
    worker = (await spawn(runtime, harness.router_id, "Ship notes"))["session_id"]
    await call_tool(runtime, "report", worker, status="done", summary="shipped")
    asking = (await spawn(runtime, harness.router_id, "Pick DB"))["session_id"]
    await call_tool(runtime, "report", asking, status="question", summary="postgres or mysql?")
    text = digest(runtime, now - 60, time.time() + 1)
    assert "• Finished: “Ship notes”" in text
    assert "• Waiting for you: 1 result to review · “Pick DB” asked a question" in text

    harness.store.save_settings(Settings(digest_time="00:00"))
    first = await runtime.digest_once(time.time() + 1)
    assert first and harness.kinds(harness.router_id)[-1] == ("update", first)
    assert await runtime.digest_once(time.time() + 2) is None, "once a day"


async def test_the_daily_digest_counts_what_home_shows(harness):
    """Bug bash 4 (D3): the digest listed Chief of Staff's helpers' and cancelled tasks as
    finished (and to review), and said "0 workers running" while tasks ran (it counted sessions
    in a turn). Every result of the owner's waits for Accept, a report that only informs too
    (E28)."""
    runtime = await harness.connect()
    now = time.time()
    workers = {}
    for title in ("Compare libraries", "Fix the bug", "Learn checks from mistakes", "Draft"):
        workers[title] = (await spawn(runtime, harness.router_id, title))["session_id"]
        if title == "Draft":  # Cancelled before it finished: not finished work.
            await runtime.task_actions.cancel(task_of(harness, workers[title]), "Not needed")
            continue
        await call_tool(runtime, "report", workers[title], status="done", summary="Done.")
    harness.store.set(f"reviewreq:{task_of(harness, workers['Fix the bug'])}", "1")  # To accept.
    harness.store.set(f"checker:{workers['Learn checks from mistakes']}", "1")  # A helper.
    await spawn(runtime, harness.router_id, "Benchmark")
    text = digest(runtime, now - 60, time.time() + 1)
    assert "• Finished: “Compare libraries”, “Fix the bug”\n" in text
    assert "• Waiting for you: 2 results to review\n" in text
    assert "• 1 task running · " in text


async def test_the_focus_summary_agrees_with_home(harness):
    """Bug bash 4 (D2, D9): "While you focused" said 1 needs you while Home showed 3 (it left
    out results to accept and questions without a task), counted a draft replaced since, and
    counted the Router's batch of three Slack messages as one. "Focusing until 04:25" beside
    the header's "4:25 AM": the line says how long instead."""
    runtime = await harness.connect()
    await runtime.focus.start(60)
    started = [text for kind, text in harness.kinds(harness.router_id) if kind == "notice"]
    assert started[-1] == "Focusing for 1h"
    await runtime.on_trigger("slack", ["slack:C1:1", "slack:C1:2", "slack:C1:3"], "Three")
    replaced = harness.store.create_draft(harness.router_id, "slack", {}, "Hi")
    harness.store.update_draft(replaced["id"], status="replaced")
    harness.store.create_draft(harness.router_id, "slack", {}, "Hi there")
    for title in ("Compare libraries", "Fix the bug"):
        worker = (await spawn(runtime, harness.router_id, title))["session_id"]
        await call_tool(runtime, "report", worker, status="done", summary="Done.")
    harness.store.set(f"reviewreq:{task_of(harness, worker)}", "1")  # A result to accept.
    runtime.asks.record_question(worker, "Which port?")  # On no task: its own card.
    await runtime.focus.check(time.time() + 3601)
    summary = harness.store.messages(harness.router_id)[-1]["text"]
    # Both results wait for Accept (E28), and the question.
    assert summary.endswith(
        "3 messages handled · 1 reply drafted for you · 2 tasks finished · 4 need you."
    ), summary


async def test_profile_is_distilled_reviewed_and_used(tmp_path):
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    result = await call_tool(runtime, "distill_profile", harness.router_id, focus="writing style")
    distiller = json.loads(result.text_result_for_llm)["session_id"]
    session = harness.session(distiller)
    assert "propose_profile" in {tool.name for tool in session.options["tools"]}
    assert "Focus: writing style" in session.sent[0] and "## Writing style" in session.sent[0]

    other = (await spawn(runtime, harness.router_id, "Other"))["session_id"]
    refused = await call_tool(runtime, "propose_profile", other, markdown="x")
    assert refused.result_type == "failure"
    await call_tool(runtime, "propose_profile", distiller, markdown="## Role and focus\n- ML infra")
    assert harness.store.get("profile_draft").startswith("## Role")
    await settle(runtime)
    assert harness.notifications[-1][0] == "Profile to review"
    # The whole draft is on a card in the inbox and in the worker's timeline; its task is done.
    [card] = [draft for draft in harness.store.drafts() if draft["kind"] == "profile"]
    assert card["text"] == "## Role and focus\n- ML infra"
    assert ("draft", "## Role and focus\n- ML infra") in harness.kinds(distiller)
    assert runtime.tasks.current(distiller)["status"] == "done"
    assert runtime.states[distiller].close_after_turn

    edited = "## Role and focus\n- ML infra and data pipelines"
    used = await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text=edited))
    assert used["status"] == "answered" and harness.store.get("profile") == edited
    assert harness.store.get("profile_draft") == ""
    await call_tool(runtime, "propose_profile", distiller, markdown="## Role\n- second")
    await runtime.toolset.save_profile("## Role and focus\n- ML infra")
    assert not [draft for draft in harness.store.drafts() if draft["kind"] == "profile"], (
        "using it from Settings settles the card"
    )
    worker = (await spawn(runtime, harness.router_id, "New"))["session_id"]
    prompt = harness.session(worker).options["system_message"]["content"]
    assert "<owner_profile>\n## Role and focus\n- ML infra\n</owner_profile>" in prompt


async def test_slack_edits_become_style_examples(tmp_path):
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    slack_id = harness.store.open_sessions("slack")[0]["id"]

    async def draft(text):
        await call_tool(runtime, "draft_slack_reply", slack_id, channel_id="C0DEPLOYS", text=text)
        return harness.store.drafts()[-1]

    harness.mcp.sent_link = "https://acme.slack.com/archives/C0DEPLOYS/p1712345680000100"
    await runtime.drafts.resolve(
        (await draft("Hello! Yes.")).get("id"), DraftDecision(action="send", text="yep")
    )
    await runtime.drafts.resolve(
        (await draft("Certainly, my friend!")).get("id"),
        DraftDecision(action="reject", reason="too formal"),
    )
    harness.session(slack_id).emit("session.idle")
    await settle(runtime)
    await runtime.ensure_slack_session()
    prompt = harness.session(slack_id).options["system_message"]["content"]
    assert 'The owner rewrote a draft and sent: "yep"' in prompt
    assert 'The owner rejected a draft because: "too formal"' in prompt
    assert "Hello! Yes." not in prompt, "model-written drafts are not kept"
    worker = (await spawn(runtime, harness.router_id, "W", slack=True))["session_id"]
    assert "<owner_edits>" not in harness.session(worker).options["system_message"]["content"]


async def test_a_broken_dependency_is_reported_and_the_router_can_release(harness):
    runtime = await harness.connect()
    first = (await spawn(runtime, harness.router_id, "A"))["session_id"]
    second = (await spawn(runtime, harness.router_id, "B", after=[first]))["session_id"]
    harness.session(first).emit("session.error", message="boom")
    await settle(runtime)
    await runtime.workers.pump()
    await settle(runtime)
    told = [p for p in harness.session(harness.router_id).sent if "ended without finishing" in p]
    assert len(told) == 1 and 'title="B"' in told[0]
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=second, text="Go ahead anyway."
    )
    await settle(runtime)
    assert harness.store.session(second)["status"] == "working"
    prompt = harness.session(second).sent[0]
    assert env("router", "message", "Go ahead anyway.", task=task_of(harness, second)) in prompt
    assert f"- Task {task_of(harness, first)} “A” (failed): no report" in prompt


async def test_a_finished_dependent_is_not_blocked_again(tmp_path):
    harness = Harness(tmp_path / "data", Settings(max_workers=1))
    runtime = await harness.connect()
    first = (await spawn(runtime, harness.router_id, "A"))["session_id"]
    second = (await spawn(runtime, harness.router_id, "B", after=[first]))["session_id"]
    await call_tool(runtime, "report", first, status="done", summary="a")
    harness.session(first).emit("session.idle")
    await settle(runtime)
    await call_tool(runtime, "report", second, status="done", summary="b")
    harness.session(second).emit("session.idle")
    await settle(runtime)
    await runtime.owner_send(first, "one more thing")
    await call_tool(runtime, "report", first, status="question", summary="which one?")
    await runtime.owner_send(second, "tweak it")
    assert harness.store.session(second)["status"] == "queued"
    harness.session(first).emit("session.idle")
    await settle(runtime)
    assert harness.session(second).sent[-1].startswith(said("tweak it"))


async def test_settings_models_reach_live_sessions(tmp_path):
    harness = Harness(tmp_path / "data", Settings(model="smart-model"))
    runtime = await harness.connect()
    old = harness.store.settings()
    new = Settings(model="smart-model", role_models={"router": "fast-model"})
    harness.store.save_settings(new)
    await runtime.settings_changed(old, new)
    assert harness.session(harness.router_id).models == ["fast-model"]


async def test_closed_workers_come_back_with_their_conversation(harness):
    runtime = await harness.connect()
    worker_id = (await spawn(runtime, harness.router_id, "Fix login"))["session_id"]
    await call_tool(runtime, "report", worker_id, status="done", summary="Fixed.")
    # Its result waits for Accept (E28): tidying leaves it; closed, it comes back all the same.
    assert await runtime.workers.tidy(time.time() + 2 * 86400) == []
    await runtime.workers.close(worker_id)
    listed = json.loads(
        (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    )
    assert [card["session_id"] for card in listed["recently_closed"]] == [worker_id]

    result = await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker_id,
        text="Also the logout", task_id=task_of(harness, worker_id),
    )  # fmt: skip
    assert result.result_type == "success"
    row = harness.store.session(worker_id)
    assert not row["closed_at"] and row["status"] == "working"
    assert (
        harness.session(worker_id)
        .sent[-1]
        .startswith(env("router", "message", "Also the logout", task=task_of(harness, worker_id)))
    )

    await runtime.workers.close(worker_id)
    await runtime.owner_send(worker_id, "One more thing")
    assert not harness.store.session(worker_id)["closed_at"]
    assert harness.session(worker_id).sent[-1].startswith(said("One more thing"))


async def test_a_reopened_worker_is_not_pushed_back_onto_abandoned_work(harness):
    runtime = await harness.connect()
    worker_id = (await spawn(runtime, harness.router_id, "Refactor auth"))["session_id"]
    harness.session(worker_id).emit("assistant.turn_start")
    harness.session(worker_id).emit("tool.execution_start", tool_name="bash", arguments={})
    await settle(runtime)
    await runtime.workers.close(worker_id)
    await runtime.workers.reopen(worker_id)
    assert harness.store.session(worker_id)["status"] == "idle"
    assert await runtime.watchdog.check(time.time() + 16 * 60) == []


async def test_a_worker_whose_conversation_is_gone_is_not_reopened(harness):
    runtime = await harness.connect()
    worker_id = (await spawn(runtime, harness.router_id, "Old"))["session_id"]
    await runtime.workers.close(worker_id)
    del harness.client.sessions[worker_id]
    result = await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker_id, text="again",
        task="new", why_new="test", title="Again",
    )  # fmt: skip
    assert "no longer has that conversation" in result.text_result_for_llm
    assert harness.store.session(worker_id)["closed_at"]
    # Its task was cancelled when it closed: refused before anything is reopened.
    cancelled = await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker_id, text="again"
    )
    assert "was cancelled" in cancelled.text_result_for_llm
    assert harness.store.session(worker_id)["closed_at"], "not reopened for nothing"


async def test_find_sessions_by_task_file_pr_and_id(harness):
    runtime = await harness.connect()
    router = harness.router_id
    login = (await spawn(runtime, router, "修复登录页面的样式问题"))["session_id"]
    config = (await spawn(runtime, router, "Tune retries"))["session_id"]
    review = (await spawn(runtime, router, "Review", follow=["github:acme/app#42"]))["session_id"]
    other = (await spawn(runtime, router, "Write release notes"))["session_id"]
    cwd = harness.store.session(config)["cwd"]
    request = SimpleNamespace(kind="write", file_name="src/http/config.py", resolved_path=None)
    await runtime.events.on_permission(request, {"session_id": config})
    summary = f"Notes for v2.3 are ready in {cwd}/NOTES.md."
    await call_tool(runtime, "report", other, status="done", summary=summary)
    await runtime.workers.close(login)

    def found(query):
        return [card["session_id"] for card in runtime.workers.find(query)]

    assert found("登录页面又出bug了")[0] == login, "closed workers are found too"
    assert found("the config.py change")[0] == config
    assert found("comments on PR #42")[0] == review
    assert found("v2.3 notes")[0] == other
    assert found(f"continue {review[:8]}")[0] == review
    assert found("something unrelated entirely") == []
    assert found("new data pipeline for customer support work") == [], "not the shared path"
    assert runtime.workers.find("PR 42")[0]["matched"] == ["42"]
    assert harness.store.session(config)["files"] == '["src/http/config.py"]'
    assert cwd


def test_words_most_workers_share_do_not_match():
    titles = ["Fix billing totals", "Bump the linter", "Flaky upload test", "Onboarding doc", "CI"]
    docs = {title: {"title": title, "task": f"{title}. Work in your worktree."} for title in titles}
    assert catalog.rank("more work in the worktree on payroll", docs) == []
    assert [doc for doc, _ in catalog.rank("billing, in the worktree", docs)] == [titles[0]]
    assert len(catalog.rank("the worktree", docs)) == 5, "unless the query has nothing else"


async def test_the_owner_picks_the_folder_when_the_router_is_unsure(tmp_path):
    app, notes = tmp_path / "app", tmp_path / "notes"
    app.mkdir()
    notes.mkdir()
    (app / "README.md").write_text("<!-- badge -->\n# Acme App\nThe storefront.\n")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    router = harness.router_id
    listed = json.loads((await call_tool(runtime, "list_sessions", router)).text_result_for_llm)
    assert listed["projects"] == [
        {"path": str(app.resolve()), "name": "app", "remote": "", "about": "Acme App"}
    ]
    refused = await call_tool(
        runtime, "spawn_worker", router, title="Notes", instructions="n", project=str(notes)
    )
    assert "not inside a configured project" in refused.text_result_for_llm
    assert "ask_workspace" in refused.text_result_for_llm

    async def ask(*options):
        result = await call_tool(
            runtime,
            "ask_workspace",
            router,
            title="Fix search",
            request="Fix the search bug",
            options=list(options),
            why="two repos have search",
        )
        return result.text_result_for_llm

    assert "Not an existing folder" in await ask(str(tmp_path / "missing"))
    assert "not the home" in await ask(str(Path.home()))
    await ask(str(app), str(notes))
    draft = harness.store.drafts()[0]
    assert draft["kind"] == "workspace"
    assert [option["configured"] for option in draft["target"]["options"]] == [True, False]
    assert harness.store.messages(router)[-1]["meta"]["label"] == "Folder question"

    await runtime.drafts.resolve(draft["id"], DraftDecision(action="choose", choice=str(notes)))
    assert harness.store.settings().projects == [str(app.resolve()), str(notes.resolve())]
    told = harness.session(router).sent[-1]
    assert (
        told.startswith(env("system", "draft-outcome", "For “Fix search” the owner chose")[:-30])
        and str(notes.resolve()) in told
    )
    started = await spawn(runtime, router, "Fix search", project=str(notes), why_new="t")
    assert started["cwd"] == str(notes.resolve())

    await ask(str(app))
    second = harness.store.drafts()[0]["id"]
    await runtime.drafts.resolve(second, DraftDecision(action="choose", choice=""))
    assert (
        harness.session(router)
        .sent[-1]
        .endswith("the owner chose no project: start the worker without one.\n</message>")
    )
    await ask(str(app))
    third = harness.store.drafts()[0]["id"]
    await runtime.drafts.resolve(third, DraftDecision(action="reject", reason="later"))
    assert "do not start it. Reason: later" in harness.session(router).sent[-1]
    with pytest.raises(ValueError, match="private data directory"):
        await runtime.add_project(str(tmp_path))  # The data directory must stay private.


async def test_scratch_folder_and_opening_a_workers_folder(tmp_path, monkeypatch):
    scratch = tmp_path / "WorkBuddy"
    harness = Harness(tmp_path / "data", Settings(scratch_directory=str(scratch)))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Research agents")
    assert Path(worker["cwd"]).parent == scratch.resolve()
    for bad in ("relative/dir", str(Path.home())):
        try:
            Settings(scratch_directory=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad} must be refused")

    calls = []

    async def fake_exec(*command, **options):
        calls.append(command)
        return SimpleNamespace(returncode=0, communicate=lambda: asyncio.sleep(0, ("", b"")))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    await runtime.open_folder(worker["session_id"], "vscode")
    assert calls == [("code", worker["cwd"])]


async def test_focus_holds_notifications_and_ends_with_one_summary(harness):
    runtime = await harness.connect()
    toasts = []
    runtime.bus.publish = lambda kind, data=None: toasts.append(data) if kind == "toast" else None
    state = await runtime.focus.start(60)
    assert state["until"] and runtime.focus.active()
    assert 'type="focus"' in harness.session(harness.router_id).sent[-1]

    await call_tool(runtime, "notify_owner", harness.router_id, text="PR #3 merged")
    worker = (await spawn(runtime, harness.router_id, "Docs"))["session_id"]
    await call_tool(runtime, "report", worker, status="done", summary="Docs written")
    assert toasts == [] and harness.notifications == [], "held back while focusing"
    await call_tool(
        runtime, "notify_owner", harness.router_id, text="Production is down", urgent=True
    )
    assert [t["text"] for t in toasts] == ["Production is down"], "urgent breaks through"

    await runtime.focus.check(time.time() + 3601)
    assert not runtime.focus.active()
    summary = harness.store.messages(harness.router_id)[-1]
    assert summary["meta"].get("focus_summary")
    assert summary["text"].startswith("While you focused (")
    assert "1 task finished" in summary["text"]


async def test_a_model_copilot_refuses_falls_back_and_the_message_is_sent_again(tmp_path):
    settings = Settings(model="smart-model", role_efforts={"worker": "high"})
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    worker = (await spawn(runtime, harness.router_id, "Hard bug"))["session_id"]
    session = harness.session(worker)
    task = session.sent[-1]
    refused = "Execution failed: 400 The requested model is not supported. (Request ID: x)"

    session.emit("session.error", message=refused)
    await settle(runtime)
    assert session.models[-1] == "smart-model" and session.tunings[-1] == {
        "reasoning_effort": "medium",
        "context_tier": "default",
    }, "first without the chosen effort and context size"
    assert session.sent[-1] == task, "the turn that failed is sent again"
    assert harness.store.session(worker)["status"] == "working", "not failed"

    session.emit("session.error", message=refused)
    await settle(runtime)
    assert session.models[-1] == "auto", "then on Copilot's default model"
    assert session.sent[-1] == task
    notes = [text for kind, text in harness.kinds(worker) if kind == "notice" and "refused" in text]
    assert len(notes) == 2 and "smart-model" in notes[1]
    other = (await spawn(runtime, harness.router_id, "Docs", why_new="other"))["session_id"]
    assert "model" not in harness.session(other).options, "a refused model is not asked again"

    session.emit("session.error", message=refused)
    await settle(runtime)
    assert runtime.tasks.current(worker)["status"] == "failed", "nothing left: it fails"


async def test_the_same_answer_twice_in_a_minute_shows_once(harness):
    runtime = await harness.connect()
    worker = (await spawn(runtime, harness.router_id, "Answer"))["session_id"]
    session = harness.session(worker)
    for _ in range(3):
        session.emit("assistant.message", content="The answer is 42.")
    await settle(runtime)
    said = [m for m in harness.store.messages(worker) if m["text"] == "The answer is 42."]
    assert len(said) == 1


async def test_the_owner_adds_a_habit_of_their_own(harness):
    runtime = await harness.connect()
    await runtime.learning.add("Run the linter before every commit", "everywhere")
    [lesson] = runtime.learning.lessons()
    assert lesson["text"] == "Run the linter before every commit" and lesson["scope"] == ""
    with pytest.raises(ValueError, match="Write the habit"):
        await runtime.learning.add("   ")


def test_a_step_reads_as_what_it_is_for():
    arguments = {"command": "cd /w && rm -f cos.db", "description": "Reset the test database"}
    assert describe_step("bash", arguments) == "Reset the test database"
    assert describe_tool("bash", arguments) == "bash cd /w && rm -f cos.db"
    assert describe_step("view", {"path": "/w/app.py"}) == "Reading app.py"
    assert describe_step("grep", {"pattern": "def main"}) == "Searching the code for “def main”"
    assert describe_step("read_bash", {"shellId": "3"}) == "Reading a command's output"
    assert describe_step("web_fetch", {"url": "https://docs.x.com/a"}) == "Reading docs.x.com"
    assert describe_step("bash", {"command": "make"}) == "bash make", "the command, as it runs"
