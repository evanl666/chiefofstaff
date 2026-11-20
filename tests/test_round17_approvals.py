"""Round 17 (E123): smooth by default. What is safe is handled for the owner and listed under
Handled for you, with Undo where it means something: a report with no code or PR accepted once
read (or a day on), a changed decision nobody's work changes for closed, a lesson their own words
back often kept, the one project a task fits picked, a server the worker changed kept off without
a word until it is called, git files in Chief of Staff's own worktree folders moved aside. The
other approvals are one card, Your OK, a line each; what speaks as the owner, their decisions,
merges and sign-ins are never approved for them."""

import json
from datetime import UTC, datetime, timedelta

from conftest import call_tool, settle

from chiefofstaff.digest import needs, needs_you, waiting_parts
from chiefofstaff.models import DraftDecision
from chiefofstaff.watchdog import WatchConfig


async def spawn(runtime, router, title, **extra):
    extra.setdefault("instructions", title)
    extra.setdefault("why_new", "test")
    result = await call_tool(runtime, "spawn_worker", router, title=title, **extra)
    worker = json.loads(result.text_result_for_llm)["session_id"]
    return worker, runtime.tasks.current(worker)


async def reported(runtime, router, title, summary="Qdrant fits best."):
    worker, task = await spawn(runtime, router, title)
    await call_tool(runtime, "report", worker, status="done", summary=summary)
    await settle(runtime)
    return worker, runtime.tasks.get(task["id"])


def handled(runtime, kind):
    return [entry for entry in runtime.handled.view() if entry["kind"] == kind]


def later(**delta):
    return datetime.now(UTC) + timedelta(**delta)


# 1. Reports only


async def test_a_report_with_no_code_or_pr_is_accepted_once_read_with_undo(harness):
    runtime = await harness.connect()
    _, task = await reported(runtime, harness.router_id, "Compare vector databases")
    assert runtime.task_actions.view(task)["report_only"] is True
    assert runtime.task_actions.settle_reports() == [], "unread: it waits"
    runtime.task_actions.mark_read(task["id"])
    assert runtime.task_actions.settle_reports() == [], "not while the owner reads it"
    assert runtime.task_actions.settle_reports(later(minutes=11)) == [task["id"]]
    done = runtime.tasks.get(task["id"])
    assert done["status"] == "done" and done["ended_at"] == runtime.task_actions.finished_at(task)
    [entry] = handled(runtime, "accepted")
    assert entry["text"] == (
        "Accepted “Compare vector databases” after you read it: a report, with no code or PR "
        "to check"
    )
    assert entry["undo"] == "Undo" and entry["ref"] == task["id"]
    assert not runtime.learning.agreement()["now"]["results"][1], "not the owner's word"

    said = await runtime.handled.undo(entry["id"])
    assert said == "“Compare vector databases” waits for your Accept again."
    assert runtime.tasks.get(task["id"])["status"] == "review"
    assert runtime.task_actions.settle_reports(later(days=3)) == [], "theirs to accept now"
    assert runtime.handled.get(entry["id"])["status"] == "undone"
    assert needs(runtime).markers[task["id"]] == "review"


async def test_an_unread_report_is_accepted_a_day_after_it_finished(harness):
    runtime = await harness.connect()
    _, task = await reported(runtime, harness.router_id, "Summarize the outage")
    assert runtime.task_actions.settle_reports(later(hours=23)) == []
    assert runtime.task_actions.settle_reports(later(hours=25)) == [task["id"]]
    [entry] = handled(runtime, "accepted")
    assert "a day after it finished" in entry["text"]


async def test_code_a_pr_a_draft_or_a_wish_to_see_it_keeps_its_accept(harness):
    runtime = await harness.connect()
    _, coded = await reported(runtime, harness.router_id, "Fix the parser", "Fixed it.")
    run = harness.store.one("SELECT id, evidence FROM runs WHERE task_id=?", (coded["id"],))
    evidence = json.loads(run["evidence"]) | {"checkouts": [{"path": "/x", "files": 2}]}
    harness.store.execute(
        "UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run["id"])
    )
    _, wanted = await reported(runtime, harness.router_id, "Draft the plan")
    harness.store.set(f"reviewreq:{wanted['id']}", "1")
    drafting, replied = await reported(runtime, harness.router_id, "Answer Maya")
    harness.store.create_draft(drafting, "reply", {"to": "Maya", "task_id": replied["id"]}, "Hi")
    harness.own_pull("acme/app", 8)
    url = "https://github.com/acme/app/pull/8"
    harness.gh_replies[f"pr view {url}"] = json.dumps({
        "number": 8, "url": url, "state": "MERGED", "title": "Fix", "isDraft": False,
        "mergeStateStatus": "CLEAN", "headRefOid": "abc1234", "author": {"login": "me"},
        "reviewDecision": "APPROVED", "statusCheckRollup": [], "headRefName": "fix",
        "createdAt": "2999-01-01T00:00:00Z",
    })  # fmt: skip
    _, pr = await reported(runtime, harness.router_id, "Fix logout", f"Opened {url}")
    for task in (coded, wanted, replied, pr):
        assert runtime.task_actions.view(runtime.tasks.get(task["id"]))["report_only"] is False
    assert runtime.task_actions.settle_reports(later(days=2)) == []
    assert {runtime.tasks.get(t["id"])["status"] for t in (coded, wanted, replied, pr)} == {
        "review"
    }


async def test_reports_from_before_are_settled_by_the_same_rule(harness):
    """Migration: the rule stands for results an earlier version left waiting (read, or a day
    old)."""
    runtime = await harness.connect()
    _, old = await reported(runtime, harness.router_id, "Old research")
    harness.store.execute(
        "UPDATE runs SET ended_at='2020-01-01T00:00:00+00:00' WHERE task_id=?", (old["id"],)
    )
    harness.store.delete("migrated:accept_all")
    assert runtime.task_actions.settle_reports() == [old["id"]]
    assert harness.store.get("migrated:accept_all"), "an older version's one pass is done"


# 2. Decision changes


async def decided(runtime, harness, *titles):
    workers = [await spawn(runtime, harness.router_id, title) for title in titles]
    tasks = [task["id"] for _, task in workers]
    recorded = await call_tool(runtime, "record_decision", harness.router_id,
                               decision="Email only", tasks=tasks)  # fmt: skip
    first = recorded.text_result_for_llm.split()[1].rstrip(",.")
    await call_tool(runtime, "record_decision", harness.router_id,
                    decision="Email and SMS", replaces=[first])  # fmt: skip
    [new] = runtime.decisions.active()
    [card] = [d for d in harness.store.drafts() if d["kind"] == "change"]
    return workers, new, card


async def test_a_changed_decision_nobodys_work_changes_for_closes_by_itself(harness):
    runtime = await harness.connect()
    workers, new, card = await decided(runtime, harness, "Login API", "Login form")
    for worker, _ in workers:
        await call_tool(runtime, "ack_decision", worker, decision=new["id"],
                        plan="Nothing to change", changes=False)  # fmt: skip
    closed = harness.store.draft(card["id"])
    assert closed["status"] == "answered"
    assert closed["outcome"] == "Every affected worker said nothing changes."
    [entry] = handled(runtime, "change")
    assert entry["text"] == (
        "Closed “Decision changed: Email and SMS”: 2 workers said nothing changes for them"
    )
    assert entry["undo"] == ""


async def test_a_changed_decision_that_changes_work_keeps_its_card(harness):
    runtime = await harness.connect()
    workers, new, card = await decided(runtime, harness, "Login API", "Login form")
    (api, _), (form, _) = workers
    await call_tool(runtime, "ack_decision", api, decision=new["id"], plan="Nothing",
                    changes=False)  # fmt: skip
    await call_tool(runtime, "ack_decision", form, decision=new["id"], plan="Add a phone field")
    kept = harness.store.draft(card["id"])
    assert kept["status"] == "pending", "the owner sees who redoes what"
    assert [item["changes"] for item in kept["target"]["tasks"]] == [False, True]
    assert handled(runtime, "change") == []
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer"))
    assert harness.store.draft(card["id"])["status"] == "answered"


async def test_a_confirmation_from_before_keeps_the_card(harness):
    """Migration: a worker that confirmed before this version said not whether work changes."""
    runtime = await harness.connect()
    workers, new, card = await decided(runtime, harness, "Login API", "Login form")
    target = card["target"]
    target["tasks"][0]["ack"] = "Confirmed long ago"
    harness.store.update_draft_target(card["id"], target)
    await call_tool(runtime, "ack_decision", workers[1][0], decision=new["id"], plan="Nothing",
                    changes=False)  # fmt: skip
    assert harness.store.draft(card["id"])["status"] == "pending"


# 3. Lessons


async def learner(runtime, *said):
    for words in said:
        runtime.learning.record("follow-up", context="a task", owner=words)
    started = await runtime.learning.learn()
    await settle(runtime)
    return started["session_id"]


def feedback_ids(harness):
    return [row["id"] for row in harness.store.all("SELECT id FROM feedback ORDER BY id")]


async def test_a_lesson_backed_often_is_kept_with_undo_the_rest_wait_on_your_ok(harness):
    runtime = await harness.connect()
    session = await learner(runtime, "Add a rollback", "Rollbacks, always", "Where's the rollback?",
                            "Keep it short")  # fmt: skip
    ids = feedback_ids(harness)
    said = await call_tool(runtime, "propose_lessons", session, lessons=[
        {"lesson": "Give every migration a rollback plan", "feedback": ids[:3]},
        {"lesson": "Keep reports short", "feedback": ids[3:]},
        {"lesson": "Never force-push a shared branch", "feedback": ids[:3]},
    ])  # fmt: skip
    assert "Proposed 2 lesson(s). 1 more" in said.text_result_for_llm
    assert [lesson["text"] for lesson in runtime.learning.lessons()] == [
        "Give every migration a rollback plan"
    ]
    [entry] = handled(runtime, "lesson")
    assert entry["text"] == (
        "Kept the lesson “Give every migration a rollback plan”: your own words said so 3 times"
    )
    [card] = [d for d in harness.store.drafts() if d["kind"] == "lessons"]
    assert [(item["text"], item["sensitive"]) for item in card["target"]["lessons"]] == [
        ("Keep reports short", False),
        ("Never force-push a shared branch", True),
    ], "one backed once, one about what cannot be taken back: the owner's to keep"

    said = await runtime.handled.undo(entry["id"])
    assert said.startswith("Dropped the lesson “Give every migration a rollback plan”")
    assert runtime.learning.lessons() == []
    assert runtime.learning.lessons("rejected")[0]["text"].startswith("Give every migration")


async def test_the_same_words_count_once(harness):
    runtime = await harness.connect()
    session = await learner(runtime, "Add a rollback", "add a  rollback", "Add a rollback")
    await call_tool(runtime, "propose_lessons", session, lessons=[
        {"lesson": "Give every migration a rollback plan", "feedback": feedback_ids(harness)},
    ])  # fmt: skip
    assert runtime.learning.lessons() == [], "said once, in effect"


async def test_lessons_wait_again_once_the_owner_drops_too_many_of_those_kept(harness):
    runtime = await harness.connect()
    session = await learner(runtime, "One", "Two", "Three")
    ids = feedback_ids(harness)
    for index in range(5):
        harness.store.execute(
            """INSERT INTO lessons(id, text, scope, evidence, observed, status, created_at,
            updated_at) VALUES (?, ?, '', ?, '[]', ?, 'x', 'x')""",
            (f"l{index}", f"Old {index}", json.dumps(ids), "rejected" if index < 2 else "active"),
        )
    await call_tool(runtime, "propose_lessons", session, lessons=[
        {"lesson": "Name tests after behaviour", "feedback": ids},
    ])  # fmt: skip
    assert [d["kind"] for d in harness.store.drafts()] == ["lessons"], "3 of 5 kept: asked"


# 4. The folder of a new task


async def test_the_one_project_that_fits_is_picked_with_change(tmp_path, harness):
    api = tmp_path / "api"
    api.mkdir()
    settings = harness.store.settings().model_copy(update={"projects": [str(api)]})
    harness.store.save_settings(settings)
    runtime = await harness.connect()
    said = await call_tool(runtime, "ask_workspace", harness.router_id, title="Fix login",
                           request="Fix login", why="Not sure", options=[str(api)])  # fmt: skip
    assert said.text_result_for_llm.startswith(f"Only one configured project fits: {api}")
    assert [d for d in harness.store.drafts() if d["kind"] == "workspace"] == []
    [entry] = handled(runtime, "workspace")
    assert (entry["text"], entry["undo"]) == ("Picked api for “Fix login”: the only project "
                                              "that fits", "Change")  # fmt: skip
    notice = harness.store.messages(harness.router_id)[-1]
    assert notice["text"] == "“Fix login” works in api: the only project that fits."
    assert notice["meta"]["handled"] == {"id": entry["id"], "undo": "Change"}

    other = tmp_path / "web"
    other.mkdir()
    await runtime.handled.undo(entry["id"])
    [card] = [d for d in harness.store.drafts() if d["kind"] == "workspace"]
    assert card["target"]["picked"] == str(api)
    await runtime.drafts.resolve(card["id"], DraftDecision(action="choose", choice=str(other)))
    told = harness.session(harness.router_id).sent[-1]
    assert f"the owner chose {other}" in told
    assert f"If you started its worker in {api} already, cancel that task first." in told


async def test_several_folders_that_fit_are_asked_as_before(tmp_path, harness):
    api, web = tmp_path / "api", tmp_path / "web"
    api.mkdir()
    web.mkdir()
    settings = harness.store.settings().model_copy(update={"projects": [str(api), str(web)]})
    harness.store.save_settings(settings)
    runtime = await harness.connect()
    for options in ([str(api), str(web)], [str(tmp_path / "web")], [str(api), str(tmp_path)]):
        await call_tool(runtime, "ask_workspace", harness.router_id, title="Fix login",
                        request="Fix login", why="?", options=options)  # fmt: skip
    assert handled(runtime, "workspace")[0]["text"].startswith("Picked web"), "one configured"
    assert len([d for d in harness.store.drafts() if d["kind"] == "workspace"]) == 2


# 7. Your OK


async def test_your_ok_is_one_item_of_needs_you_and_approve_all_safe_takes_only_the_safe(
    harness,
):
    from chiefofstaff.models import OkItem

    runtime = await harness.connect()
    router = harness.router_id
    workers, _, change = await decided(runtime, harness, "Login API")
    session = await learner(runtime, "Keep it short")
    await call_tool(runtime, "propose_lessons", session, lessons=[
        {"lesson": "Keep reports short", "feedback": feedback_ids(harness)[-1:]},
        {"lesson": "Ask before you delete data", "feedback": feedback_ids(harness)[-1:]},
    ])  # fmt: skip
    [lessons] = [d for d in harness.store.drafts() if d["kind"] == "lessons"]
    merge = harness.store.create_draft(
        router, "merge", {"label": "acme/app#8", "url": "https://github.com/acme/app/pull/8"}, "x"
    )
    reply = harness.store.create_draft(workers[0][0], "reply", {"to": "Maya"}, "Hi Maya")
    found = needs(runtime)
    assert len(found.of("card")) == 4 and found.count() == 2 == needs_you(runtime)
    assert "Your OK on 3 things (changed decision to confirm, lessons to review, PR ready to " \
        "merge)" in waiting_parts(found)  # fmt: skip
    items = [OkItem(draft=change["id"]), OkItem(draft=merge["id"])] + [
        OkItem(draft=lessons["id"], lesson=item["id"]) for item in lessons["target"]["lessons"]
    ]
    done = await runtime.drafts.approve_safe(items)
    assert done == {"approved": 2, "left": 2}
    assert harness.store.draft(change["id"])["status"] == "answered"
    assert harness.store.draft(merge["id"])["status"] == "pending", "a merge: never with others"
    assert harness.store.draft(reply["id"])["status"] == "pending"
    assert [lesson["text"] for lesson in runtime.learning.lessons()] == ["Keep reports short"]
    [left] = [d for d in harness.store.drafts() if d["kind"] == "lessons"]
    assert [item["text"] for item in left["target"]["lessons"]] == ["Ask before you delete data"]


# Folder settings holds: their own row under Needs you, only while the worker is sandboxed.


async def test_a_settings_hold_is_under_needs_you_only_while_its_worker_is_sandboxed(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Fix login")
    runtime.settings_held.add(worker)  # (Left from before the sandbox was turned off.)
    runtime.settings_reasons[worker] = "Its folder's settings changed"
    shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert shown["attention"] != "settings" and task["id"] not in needs(runtime).markers
    runtime.settings_guarded = lambda row: True
    shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert shown["attention"] == "settings" and needs(runtime).markers[task["id"]] == "settings"
    assert not needs(runtime).of("card"), "its own row, not a line of Your OK"


# 9. The Watchdog


async def test_the_watchdog_leaves_a_worker_whose_hold_has_its_card(harness):
    """E119: a worker waiting for the owner's word on its folder's settings is not stuck."""
    harness.runtime.watch = WatchConfig(stall=0, cooldown=0)
    runtime = await harness.connect()
    worker, _ = await spawn(runtime, harness.router_id, "Fix login")
    runtime.update_session(worker, status="idle")
    runtime.settings_held.add(worker)
    assert await runtime.watchdog.check(later(hours=2).timestamp()) == []
    runtime.settings_held.discard(worker)
    assert await runtime.watchdog.check(later(hours=2).timestamp()) == [worker]
