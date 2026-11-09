"""Round 10: a task's lifecycle. Every finished result waits for the owner's Accept (or their
Request changes); what is still followed up for an accepted task is its monitoring; what it
delivered is one list; a worker's question keeps why it asks, its choices and what comes next."""

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import call_tool, settle

from chiefofstaff.digest import needs, needs_you
from chiefofstaff.store import Store, hours_ago


async def spawn(runtime, router, title, **extra):
    extra.setdefault("instructions", title)
    extra.setdefault("why_new", "test")
    result = await call_tool(runtime, "spawn_worker", router, title=title, **extra)
    worker = json.loads(result.text_result_for_llm)["session_id"]
    return worker, runtime.tasks.current(worker)


def pr_view(**changes):
    view = {
        "number": 7, "url": "https://github.com/acme/app/pull/7", "state": "OPEN",
        "title": "Fix login", "isDraft": False, "mergeStateStatus": "BLOCKED",
        "headRefOid": "abc1234", "author": {"login": "me"}, "reviewDecision": "REVIEW_REQUIRED",
        "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"},
                              {"name": "Owner Approval", "status": "QUEUED"}],
        "headRefName": "fix-login", "createdAt": "2999-01-01T00:00:00Z",
    }  # fmt: skip
    return json.dumps(view | changes)


async def delivered_pr(harness, runtime, title="Fix login", number=7):
    """A worker that opened its PR (the owner's account, just now) and reported it done."""
    harness.own_pull("acme/app", number)
    url = f"https://github.com/acme/app/pull/{number}"
    harness.gh_replies[f"pr view {url}"] = pr_view(number=number, url=url)
    worker, task = await spawn(runtime, harness.router_id, title)
    await call_tool(runtime, "report", worker, status="done", summary=f"Opened {url}")
    await settle(runtime)
    return worker, runtime.tasks.get(task["id"])


def view(runtime, task_id):
    return runtime.task_actions.view(runtime.tasks.get(task_id))


async def test_a_research_report_waits_for_accept_whatever_happens_meanwhile(harness):
    """E28: a report that changes no code (research) is a result to accept like any other:
    reading it, its worker going idle or on to other work, and days passing settle nothing."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Compare vector databases")
    summary = "Qdrant fits best: it filters by payload and runs on one box."
    await call_tool(runtime, "report", worker, status="done", summary=summary)
    await settle(runtime)
    shown = view(runtime, task["id"])
    assert (shown["status"], shown["execution"], shown["attention"]) == (
        "review", "finished", "acceptance"
    )  # fmt: skip
    assert needs(runtime).markers[task["id"]] == "review"
    counted = needs_you(runtime)
    assert runtime.task_actions.status_words(runtime.tasks.get(task["id"])) == "review, to accept"
    runtime.task_actions.mark_read(task["id"])
    assert not runtime.workers.finished(worker), "read is not accepted"
    runtime.update_session(worker, status="idle")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    task="new", why_new="test", title="Benchmark Qdrant",
                    text="Benchmark Qdrant on our data.")  # fmt: skip
    await call_tool(runtime, "report", worker, status="progress", summary="Loading the data.")
    later = time.time() + 30 * 86400
    await runtime.workers.tidy(later)
    assert runtime.tasks.get(task["id"])["status"] == "review"
    assert needs(runtime, datetime.fromtimestamp(later, UTC)).markers[task["id"]] == "review"
    assert needs_you(runtime) == counted
    assert task["id"] in {t["id"] for t in runtime.tasks.listed(include_ended_since=hours_ago(1))}


async def test_a_merged_pr_does_not_accept_its_task(harness):
    """Merge and Accept are apart: its own PR merged, the result still waits for Accept."""
    runtime = await harness.connect()
    worker, task = await delivered_pr(harness, runtime)
    assert view(runtime, task["id"])["receipts"]["prs"][0]["role"] == "created"
    harness.gh_replies["pr view https://github.com/acme/app/pull/7"] = pr_view(state="MERGED")
    await runtime.task_actions.refresh_prs(task["id"])
    runtime.unfollow("github:acme/app#7", finished=True, merged=True)
    await settle(runtime)
    shown = view(runtime, task["id"])
    assert (shown["status"], shown["attention"]) == ("review", "acceptance")
    assert "acme/app#7 is merged" in shown["result"]
    assert not harness.store.session(worker)["closed_at"], "its result still waits"
    assert runtime.task_actions.accept(task["id"])["status"] == "done"
    assert view(runtime, task["id"])["monitoring"] == [], "merged: nothing left to follow"


async def test_request_changes_sends_the_result_back_with_what_to_change(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Compare vector databases")
    session = harness.session(worker)
    await call_tool(runtime, "report", worker, status="done", summary="Qdrant fits best.")
    with pytest.raises(ValueError, match="what to change"):
        await runtime.task_actions.request_changes(task["id"], "  ")
    assert runtime.tasks.get(task["id"])["status"] == "review"
    moved = await runtime.task_actions.request_changes(task["id"], "Add a latency table.")
    assert moved["status"] == "running" and view(runtime, task["id"])["attention"] == ""
    assert "did not accept your result" in session.sent[-1]
    assert "Add a latency table." in session.sent[-1]
    [feedback] = harness.store.all("SELECT * FROM feedback WHERE kind='follow-up'")
    assert feedback["owner"] == "Add a latency table." and feedback["agreed"] == 0
    with pytest.raises(ValueError, match="finished result"):
        await runtime.task_actions.request_changes(task["id"], "More.")


async def test_accepted_with_its_own_pr_open_it_is_monitored_until_that_is_over(harness):
    runtime = await harness.connect()
    _worker, task = await delivered_pr(harness, runtime)
    key = "github:acme/app#7"
    read = datetime.now(UTC).isoformat()
    harness.store.set(f"ghread:{key}", read)
    runtime.task_actions.accept(task["id"])
    shown = view(runtime, task["id"])
    assert (shown["execution"], shown["attention"]) == ("finished", "")
    [watch] = shown["monitoring"]
    assert (watch["key"], watch["kind"], watch["waiting_for"]) == (key, "pr", "approval")
    assert watch["last_check"] == read and watch["next_check"] is None
    assert watch["label"] == "app#7: Fix login" and watch["changed_at"]
    assert "monitoring app#7: Fix login (waiting for approval)" in (
        runtime.task_actions.status_words(runtime.tasks.get(task["id"]))
    )
    harness.gh_replies["pr view https://github.com/acme/app/pull/7"] = pr_view(state="MERGED")
    await runtime.task_actions.refresh_prs(task["id"])
    assert key in harness.store.follows(_worker) and view(runtime, task["id"])["monitoring"] == []
    worker, plain = await spawn(runtime, harness.router_id, "Compare vector databases")
    await call_tool(runtime, "report", worker, status="done", summary="Qdrant fits best.")
    runtime.task_actions.accept(plain["id"])
    shown = view(runtime, plain["id"])
    assert (shown["execution"], shown["attention"], shown["monitoring"]) == ("finished", "", [])
    # Cancelled before it was accepted: set aside, over but not a success.
    worker, dropped = await spawn(runtime, harness.router_id, "Compare search engines")
    await call_tool(runtime, "report", worker, status="done", summary="Meilisearch fits.")
    with pytest.raises(ValueError, match="finished just now"):
        await runtime.task_actions.cancel(dropped["id"], "not needed")
    await runtime.task_actions.cancel(dropped["id"], "not needed")
    shown = view(runtime, dropped["id"])
    assert (shown["status"], shown["execution"], shown["attention"]) == ("done", "cancelled", "")


async def test_a_pr_it_reviewed_is_not_monitored_once_accepted_and_news_there_is_an_update(
    harness,
):
    runtime = await harness.connect()
    url = "https://github.com/acme/lib/pull/5"
    harness.gh_replies[f"pr view {url}"] = pr_view(
        number=5, url=url, author={"login": "alice"}, createdAt="2020-01-01T00:00:00Z"
    )
    worker, task = await spawn(runtime, harness.router_id, "Review the parser PR",
                               follow=["github:acme/lib#5"])  # fmt: skip
    draft = harness.store.create_draft(worker, "github", {"repo": "acme/lib", "number": 5}, "Ok")
    harness.store.update_draft(draft["id"], status="sent")
    await call_tool(runtime, "report", worker, status="done",
                    summary=f"Reviewed {url}: two comments on error handling.")  # fmt: skip
    await settle(runtime)
    [pr] = view(runtime, task["id"])["receipts"]["prs"]
    assert pr["role"] == "reviewed"
    runtime.task_actions.accept(task["id"])
    shown = view(runtime, task["id"])
    assert shown["monitoring"] == [] and shown["attention"] == ""
    assert "github:acme/lib#5" in harness.store.follows(worker), "followed still, silently"
    await call_tool(runtime, "report", worker, status="progress", task_id=task["id"],
                    summary="The author answered both comments.")  # fmt: skip
    shown = view(runtime, task["id"])
    assert shown["attention"] == "update" and "answered both" in shown["attention_why"]


async def test_what_a_pr_is_to_a_task_comes_from_what_it_did_to_it(harness):
    runtime = await harness.connect()
    actions = runtime.task_actions
    _maker, made = await delivered_pr(harness, runtime, "Fix login", 11)
    reader, read = await spawn(runtime, harness.router_id, "Write the release notes")
    url = "https://github.com/acme/app/pull/11"
    await call_tool(runtime, "report", reader, status="done", summary=f"Notes cover {url}.")
    await settle(runtime)
    pull = {"label": "acme/app#11", "author": "me", "created": "2999-01-01T00:00:00Z"}
    assert (await actions.classify_prs(made, [pull]))[0] == {"acme/app#11": "created"}
    assert (await actions.classify_prs(read, [pull]))[0] == {"acme/app#11": "referenced"}
    pr_of = {d["url"]: d["role"] for d in view(runtime, made["id"])["deliverables"]}
    assert pr_of == {url: "created"}
    assert view(runtime, read["id"])["deliverables"] == [], "only mentioned: not its own"
    runtime.follow(reader, ["github:acme/app#11"], given=False, task_id=read["id"])
    assert view(runtime, read["id"])["monitoring"] == [], "followed, but only mentioned"
    # Its worker on that PR's branch (to try it out): still the task that opened it's.
    harness.store.execute("UPDATE sessions SET branch='feature' WHERE id=?", (reader,))
    tried = pull | {"branch": "feature"}
    assert (await actions.classify_prs(read, [tried]))[0] == {"acme/app#11": "referenced"}

    pusher, pushed = await spawn(runtime, harness.router_id, "Fix the flaky test")
    harness.store.execute("UPDATE sessions SET branch='fix-flaky' WHERE id=?", (pusher,))
    theirs = {"label": "acme/app#12", "author": "bob", "created": "2020-01-01T00:00:00Z",
              "branch": "fix-flaky"}  # fmt: skip
    assert (await actions.classify_prs(pushed, [theirs]))[0] == {"acme/app#12": "modified"}
    # Another task of its worker whose commits are on that branch: that one changed it.
    await call_tool(runtime, "message_session", harness.router_id, session_id=pusher,
                    task="new", why_new="test", earlier="keep", title="Rename the fixture",
                    text="Rename the fixture.")  # fmt: skip
    other = runtime.tasks.current(pusher)
    harness.store.execute(
        "UPDATE runs SET evidence=? WHERE task_id=?",
        (json.dumps({"checkouts": [{"branch": "fix-flaky", "commits": 2}]}), pushed["id"]),
    )
    assert (await actions.classify_prs(other, [theirs]))[0] == {"acme/app#12": "referenced"}
    assert (await actions.classify_prs(pushed, [theirs]))[0] == {"acme/app#12": "modified"}

    reviewer, reviewing = await spawn(runtime, harness.router_id, "Review the parser")
    harness.store.create_draft(reviewer, "github", {"repo": "acme/lib", "number": 5}, "Ok")
    review = {"label": "acme/lib#5", "author": "alice", "created": "2020-01-01T00:00:00Z"}
    assert (await actions.classify_prs(reviewing, [review]))[0] == {"acme/lib#5": "reviewed"}


async def test_a_refresh_leaves_mentioned_prs_and_ones_github_does_not_have(harness):
    runtime = await harness.connect()
    reader, read = await spawn(runtime, harness.router_id, "Write the release notes")
    url = "https://github.com/acme/app/pull/30"
    harness.gh_replies[f"pr view {url}"] = pr_view(
        number=30, url=url, author={"login": "bob"}, createdAt="2020-01-01T00:00:00Z"
    )
    await call_tool(runtime, "report", reader, status="done", summary=f"Notes cover {url}.")
    await settle(runtime)
    assert [pr["role"] for pr in runtime.task_actions.previous_prs(read["id"])] == ["referenced"]
    harness.gh_calls.clear()
    before = harness.store.value("SELECT evidence FROM runs WHERE task_id=?", (read["id"],))
    await runtime.task_actions.refresh_open_prs(every=0)
    after = harness.store.value("SELECT evidence FROM runs WHERE task_id=?", (read["id"],))
    assert after == before, "not refreshed at all"
    await runtime.task_actions.refresh_prs(read["id"])
    assert not [call for call in harness.gh_calls if url in call[0]], "only mentioned"

    # One it was asked about (its tests are checked against that PR's CI): read again.
    checked = "https://github.com/acme/app/pull/31"
    harness.gh_replies[f"pr view {checked}"] = pr_view(
        number=31, url=checked, author={"login": "bob"}, createdAt="2020-01-01T00:00:00Z"
    )
    told = f"Run the tests; {checked} is the PR they are for."
    checker, check = await spawn(runtime, harness.router_id, "Check the tests", instructions=told)
    await call_tool(runtime, "report", checker, status="done", summary=f"Ran them for {checked}.")
    await settle(runtime)
    assert [pr["asked"] for pr in runtime.task_actions.previous_prs(check["id"])] == [True]
    harness.gh_calls.clear()
    await runtime.task_actions.refresh_open_prs(every=0)
    assert [call for call in harness.gh_calls if checked in call[0]], "its CI's newer word"

    gone = "https://github.com/acme/app/pull/404"
    harness.gh_replies[f"pr view {gone}"] = RuntimeError(
        "GraphQL: Could not resolve to a PullRequest with the number of 404."
    )
    _worker, task = await spawn(runtime, harness.router_id, "Fix the typo")
    harness.store.set(f"cited:{task['id']}", json.dumps([gone]))
    for _ in range(5):
        await runtime.task_actions.read_prs(runtime.tasks.get(task["id"]))
    assert len([call for call in harness.gh_calls if gone in call[0]]) == 3


def files_in(folder: Path) -> tuple[str, str]:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "mock.png").write_bytes(b"png")
    (folder / "rows.csv").write_text("a,b\n")
    return str(folder / "mock.png"), str(folder / "rows.csv")


async def test_deliverables_are_its_prs_documents_and_runs_not_files_or_home_pages(harness):
    runtime = await harness.connect()
    brief = "https://docs.google.com/document/d/1BriefGiven7/edit"
    worker, task = await spawn(runtime, harness.router_id, "Plan the launch",
                               instructions=f"Plan the launch; the brief is {brief}")  # fmt: skip
    mock, rows = files_in(Path(harness.store.session(worker)["cwd"]))
    plan = "https://docs.google.com/document/d/1PlanAbc123/edit"
    run = "https://airflow.example.com/dags/nightly_sync/grid?dag_run_id=manual_1"
    artifacts = [
        {"url": plan, "name": "Launch plan", "status": "draft"},
        {"url": "https://docs.google.com/", "name": "Google Docs", "status": "open"},
        {"url": "https://docs.google.com/document/", "name": "Docs", "status": "open"},
        {"url": "https://www.notion.so/", "name": "Notion", "status": "open"},
        {"url": mock, "name": "Home mockup", "status": "draft"},
        {"url": rows, "name": "Rows", "status": "final"},
        {"url": "https://example.com/export.csv", "name": "Export", "status": "final"},
        {"url": run, "name": "Nightly sync", "status": "running"},
        {"url": "https://airflow.example.com/dags/nightly_sync/logs/1", "name": "Logs",
         "status": "running"},
    ]  # fmt: skip
    summary = (
        "Created [Q3 numbers](https://docs.google.com/spreadsheets/d/1SheetXyz789/edit). "
        "The job ran on https://spark.example.com/history/application_1_2/ (logs: "
        "https://github.com/acme/app/actions/runs/123/job/456; CI: "
        "https://github.com/acme/app/actions/runs/999).\nSee also "
        "https://docs.google.com/document/d/1OtherDoc99/edit and https://www.notion.so/.\n"
        f"Added a timeline to [the brief]({brief})."
    )
    await call_tool(runtime, "report", worker, status="done", summary=summary,
                    artifacts=artifacts)  # fmt: skip
    await settle(runtime)
    shown = view(runtime, task["id"])
    assert [(d["kind"], d["title"], d["status"]) for d in shown["deliverables"]] == [
        ("flow", "Nightly sync", "running"),
        ("doc", "Launch plan", "draft"),
        ("doc", "Q3 numbers", ""),
    ]
    assert len(shown["artifacts"]) == 9, "its declared artifacts are all kept"
    assert "https://github.com/acme/app/actions/runs/123/job/456" in shown["links"]
    runtime.task_actions.accept(task["id"])
    [watch] = view(runtime, task["id"])["monitoring"]
    assert (watch["kind"], watch["label"], watch["url"]) == ("flow", "Nightly sync", run)
    assert not runtime.workers.finished(worker), "its run is still followed up"
    assert await runtime.workers.tidy(time.time() + 2 * 86400) == [], "however quiet"


async def test_a_run_declared_over_in_any_words_is_not_monitored(harness):
    """A CI run declared "passed" (or with no status) promises no news: its accepted task is
    not kept open for ever on it."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Ship it")
    runs = "https://github.com/acme/app/actions/runs/5"
    pipeline = "https://gitlab.com/acme/app/-/pipelines/123"
    await call_tool(runtime, "report", worker, status="done", summary="Shipped.",
                    artifacts=[{"url": runs, "name": "CI", "status": "passed"},
                               {"url": pipeline, "name": "Pipeline", "status": ""}])  # fmt: skip
    await settle(runtime)
    runtime.task_actions.accept(task["id"])
    assert view(runtime, task["id"])["monitoring"] == []
    assert runtime.workers.finished(worker)


async def test_a_question_keeps_why_its_choices_and_what_next_until_answered(harness):
    runtime = await harness.connect()
    worker, first = await spawn(runtime, harness.router_id, "Pick the database")
    session = harness.session(worker)
    await call_tool(runtime, "report", worker, status="question", summary="Postgres or MySQL?",
                    why="It decides how the migrations are written.",
                    options=["Postgres", "MySQL"],
                    next="Write the migrations for the one you pick.")  # fmt: skip
    asked = view(runtime, first["id"])["question"]
    assert (asked["why"], asked["options"], asked["next"]) == (
        "It decides how the migrations are written.",
        ["Postgres", "MySQL"],
        "Write the migrations for the one you pick.",
    )
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    task="new", why_new="test", title="Write the README",
                    text="Write the README.")  # fmt: skip
    second = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="progress", task_id=second["id"],
                    summary="Drafting the README.")  # fmt: skip
    shown = view(runtime, first["id"])
    assert shown["attention"] == "question" and shown["question"]["options"] == asked["options"]
    assert needs(runtime).markers[first["id"]] == "question"
    listed = json.loads(
        (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    )
    [entry] = listed["needs_owner"]["questions"]
    assert entry["options"] == ["Postgres", "MySQL"] and entry["next"].startswith("Write the")
    await runtime.task_actions.answer_question(asked["id"], "Postgres")
    shown = view(runtime, first["id"])
    assert shown["question"] is None and shown["status"] == "running"
    assert not runtime.asks.session_questions(worker) and "Postgres" in session.sent[-1]


async def test_every_question_a_worker_reports_is_a_pending_item(harness):
    runtime = await harness.connect()

    def shown(worker, task_id):
        """Its questions as Home lists them: on their own, or on the task waiting for them."""
        listed = [q for q in runtime.asks.owner_questions() if q["session_id"] == worker]
        open_ = view(runtime, task_id)["question"]
        return {q["text"] for q in listed} | ({open_["text"]} if open_ else set())

    worker, task = await spawn(runtime, harness.router_id, "Pick a style")
    await call_tool(runtime, "report", worker, status="blocked", summary="No access to the repo.",
                    why="Only you can grant it.")  # fmt: skip
    assert shown(worker, task["id"]) == {"No access to the repo."}
    assert view(runtime, task["id"])["question"]["kind"] == "blocked"

    worker, task = await spawn(runtime, harness.router_id, "Summarize the thread")
    await call_tool(runtime, "report", worker, status="done", summary="Three points.")
    await call_tool(runtime, "report", worker, status="question", summary="Post it to #team?")
    assert shown(worker, task["id"]) == {"Post it to #team?"}

    worker, task = await spawn(runtime, harness.router_id, "Tidy the backlog")
    await runtime.task_actions.cancel(task["id"], "not now")
    await call_tool(runtime, "report", worker, status="question", task_id=task["id"],
                    summary="Close the stale issues too?")  # fmt: skip
    assert shown(worker, task["id"]) == {"Close the stale issues too?"}

    # Kept by an earlier version, without why, choices or what next: read as empty.
    harness.store.set(f"ownerq:{worker}:{task['id']}", json.dumps(
        {"text": "Old one?", "task_id": task["id"], "at": "2020-01-01T00:00:00+00:00"}
    ))  # fmt: skip
    [old] = [q for q in runtime.asks.owner_questions() if q["text"] == "Old one?"]
    assert (old["why"], old["options"], old["next"]) == ("", [], "")


async def test_a_long_monitored_task_stays_listed_and_an_old_finished_one_goes(harness):
    runtime = await harness.connect()
    _worker, task = await delivered_pr(harness, runtime)
    runtime.task_actions.accept(task["id"])
    worker, plain = await spawn(runtime, harness.router_id, "Compare vector databases")
    doc = {"url": "https://docs.google.com/document/d/1Compare99/edit", "name": "Comparison",
           "status": "final"}  # fmt: skip
    await call_tool(runtime, "report", worker, status="done", summary="Qdrant fits best.",
                    artifacts=[doc])  # fmt: skip
    runtime.task_actions.accept(plain["id"])
    month = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    harness.store.execute("UPDATE tasks SET ended_at=?, updated_at=?", (month, month))
    listed = {t["id"] for t in runtime.tasks.listed(include_ended_since=hours_ago(72))}
    assert task["id"] in listed and plain["id"] not in listed
    assert task["id"] in {t["id"] for t in runtime.tasks.listed()}


def test_a_workers_slack_thread_is_not_dropped_for_being_quiet(tmp_path):
    store = Store(tmp_path / "data")
    store.create_session("w1", "worker", "Answer Bob", "idle")
    store.create_session("s1", "slack", "Slack", "idle")
    store.follow("slack:D0BOB0001", "w1")
    store.follow("slack:D0ALICE01", "s1")
    store.execute("UPDATE subscriptions SET touched=?", (time.time() - 5 * 86400,))
    assert [row["thread"] for row in store.followed("slack:")] == ["slack:D0BOB0001"]


async def test_a_check_back_is_monitored_with_when_it_is_due_and_last_fired(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Watch the nightly run")
    await call_tool(runtime, "check_back", worker, minutes=30, what="whether the run finished")
    [watch] = view(runtime, task["id"])["monitoring"]
    due = runtime.task_actions.check_back(runtime.tasks.get(task["id"]))["at"]
    assert (watch["kind"], watch["waiting_for"], watch["next_check"]) == (
        "check_back", "whether the run finished", due
    )  # fmt: skip
    assert watch["last_check"] is None
    fired = datetime.now(UTC) + timedelta(hours=1)
    await runtime.task_actions.wake_check_backs(fired)
    await call_tool(runtime, "check_back", worker, minutes=60, what="whether the run finished")
    [watch] = view(runtime, task["id"])["monitoring"]
    assert watch["last_check"] == fired.isoformat()


async def test_its_own_pr_to_fix_is_what_needs_the_owner_before_and_after_accept(harness):
    runtime = await harness.connect()
    harness.own_pull("acme/app", 8)
    url = "https://github.com/acme/app/pull/8"
    harness.gh_replies[f"pr view {url}"] = pr_view(
        number=8, url=url, reviewDecision="CHANGES_REQUESTED"
    )
    worker, task = await spawn(runtime, harness.router_id, "Fix logout")
    await call_tool(runtime, "report", worker, status="done", summary=f"Opened {url}")
    await settle(runtime)
    shown = view(runtime, task["id"])
    # Still a result to accept (or send back), and why it needs a look.
    assert (shown["attention"], shown["attention_why"]) == (
        "acceptance", "Changes requested on app#8"
    )  # fmt: skip
    assert needs(runtime).markers[task["id"]] == "review"
    runtime.task_actions.accept(task["id"])
    assert view(runtime, task["id"])["attention"] == "pr-changes"
    [watch] = view(runtime, task["id"])["monitoring"]
    assert watch["waiting_for"] == "changes"


async def test_reports_settled_before_every_result_waited_for_accept_are_accepted_once(harness):
    """Upgraded: a report an earlier version counted as finished once read is not suddenly
    under Needs you; one the owner never opened waits for Accept, as any result now."""
    runtime = await harness.connect()
    reports = {}
    for title in ("Read it", "Never opened", "Old one", "Wanted a look"):
        worker, task = await spawn(runtime, harness.router_id, title)
        await call_tool(runtime, "report", worker, status="done", summary=f"{title}: done.")
        reports[title] = runtime.tasks.get(task["id"])
    runtime.task_actions.mark_read(reports["Read it"]["id"])
    old = "2020-01-01T00:00:00+00:00"
    runtime.tasks.update(reports["Old one"]["id"], ended_at=old)
    harness.store.set(f"reviewreq:{reports['Wanted a look']['id']}", "1")
    runtime.task_actions.mark_read(reports["Wanted a look"]["id"])  # Read, still to accept.
    harness.store.delete("migrated:accept_all")  # As a database from before.
    runtime.task_actions.settle_old_reports(72)
    status = {title: runtime.tasks.get(task["id"])["status"] for title, task in reports.items()}
    assert status == {"Read it": "done", "Never opened": "review", "Old one": "done",
                      "Wanted a look": "review"}  # fmt: skip
    assert runtime.tasks.get(reports["Old one"]["id"])["ended_at"] == old
    harness.store.delete(f"read:{reports['Never opened']['id']}")
    runtime.tasks.update(reports["Never opened"]["id"], ended_at=old)
    runtime.task_actions.settle_old_reports(72)
    assert runtime.tasks.get(reports["Never opened"]["id"])["status"] == "review", "once only"


def test_a_pr_read_before_roles_were_kept_is_refreshed_as_it_was():
    """Receipts from before this version have no role nor "asked": the PR a task was asked
    about (then "about") is still read again as it changes."""
    from chiefofstaff.task_actions import followed_up

    assert followed_up({"label": "o/r#1", "own": False, "about": True})
    assert not followed_up({"label": "o/r#1", "own": False, "about": False})
    assert not followed_up({"label": "o/r#1", "role": "referenced", "about": True,
                            "asked": False})  # fmt: skip
