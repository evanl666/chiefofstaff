"""Bug bash round 7: a PR closed and reopened (G2, G3, G7), a question asked as its draft was
made (G6), the text left after a merge (G11), Retry in the Router's chat (D1), a result on a
branch its worker built on (D3), and a task at work after its decision (D8)."""

import asyncio
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from conftest import Harness, call_tool, settle
from test_github_source import THREAD, Host, fake_gh

from chiefofstaff.models import DraftDecision, Settings
from chiefofstaff.sources.github import GitHubSource
from chiefofstaff.store import Store

URL = "https://github.com/acme/app/pull/7"


async def spawn(runtime, router, title, **extra):
    result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title,
                             why_new="test", **extra)  # fmt: skip
    return json.loads(result.text_result_for_llm)


def pr_view(**changes):
    view = {
        "number": 7, "url": URL, "state": "OPEN", "title": "Fix login", "isDraft": False,
        "mergeStateStatus": "CLEAN", "headRefOid": "abc1234", "author": {"login": "me"},
        "reviewDecision": "APPROVED", "statusCheckRollup": [{"name": "tests",
        "conclusion": "SUCCESS"}], "headRefName": "fix-login", "createdAt": "2999-01-01T00:00:00Z",
    }  # fmt: skip
    return json.dumps(view | changes)


async def opened_pr(harness, runtime, **view):
    """A worker whose task opened PR #7 (the owner's account), as GitHub shows it (`view`)."""
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "me", "state": "success", "active": True}]}}
    )
    harness.gh_replies[f"pr view {URL}"] = pr_view(**view)
    worker = await spawn(runtime, harness.router_id, "Fix login")
    await call_tool(runtime, "report", worker["session_id"], status="done", summary=f"Opened {URL}")
    await settle(runtime)
    return worker["session_id"], worker["task_id"]


def merge_cards(harness):
    return [draft for draft in harness.store.drafts() if draft["kind"] == "merge"]


def pr_state(runtime, task_id):
    [pr] = runtime.task_actions.receipts(runtime.tasks.get(task_id))["prs"]
    return pr["state"]


async def test_a_reopened_pr_is_read_again_at_once_and_a_closed_one_now_and_then(harness):
    """G2: reopened, the PR stayed "closed" on its task for 10+ minutes, CI news on it or not:
    a task whose PRs were all closed was never read again."""
    runtime = await harness.connect()
    _worker, task = await opened_pr(harness, runtime, state="CLOSED")
    assert pr_state(runtime, task) == "CLOSED"
    harness.gh_replies[f"pr view {URL}"] = pr_view()
    await runtime.task_actions.refresh_open_prs("acme/app#7", every=3600)
    assert pr_state(runtime, task) == "OPEN", "news on it: read at once"

    def read_ago(seconds):
        run = runtime.store.one(
            "SELECT id, evidence FROM runs WHERE task_id=? AND evidence!='' "
            "ORDER BY started_at DESC LIMIT 1",
            (task,),
        )
        evidence = json.loads(run["evidence"]) | {"prs_at": time.time() - seconds}
        runtime.store.execute(
            "UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run["id"])
        )

    harness.gh_replies[f"pr view {URL}"] = pr_view(state="CLOSED")
    read_ago(120)
    await runtime.task_actions.refresh_open_prs(every=60)
    assert pr_state(runtime, task) == "CLOSED"
    harness.gh_replies[f"pr view {URL}"] = pr_view()
    read_ago(120)
    await runtime.task_actions.refresh_open_prs(every=60)
    assert pr_state(runtime, task) == "CLOSED", "closed: read again less often"
    read_ago(600)
    await runtime.task_actions.refresh_open_prs(every=60)
    assert pr_state(runtime, task) == "OPEN", "but read again"


async def test_a_merged_pr_is_not_read_again(harness):
    runtime = await harness.connect()
    _worker, task = await opened_pr(harness, runtime, state="MERGED")
    harness.gh_calls.clear()
    await runtime.task_actions.refresh_open_prs("acme/app#7", every=0)
    assert not [call for call, _ in harness.gh_calls if call[:2] == ("pr", "view")]
    assert pr_state(runtime, task) == "MERGED"


async def test_a_pr_reopened_at_the_same_commit_is_offered_to_merge_again(harness):
    """G3: its card went when the PR was closed, and was never offered again once it was
    reopened, ready, at the same commit."""
    runtime = await harness.connect()
    _worker, task = await opened_pr(harness, runtime)
    [first] = merge_cards(harness)
    harness.gh_replies[f"pr view {URL}"] = pr_view(state="CLOSED")
    await runtime.task_actions.refresh_prs(task)
    assert harness.store.draft(first["id"])["outcome"] == "The PR is closed."
    assert not merge_cards(harness)
    harness.gh_replies[f"pr view {URL}"] = pr_view()
    await runtime.task_actions.refresh_prs(task)
    [again] = merge_cards(harness)
    assert again["id"] != first["id"] and again["target"]["head"] == "abc1234"


async def test_a_pr_closed_while_followed_stays_followed_for_its_reopening(tmp_path):
    """G7: closed (not merged), its worker heard only "The PR was closed." and the PR was let go,
    though the worker had promised to say when it is reopened."""
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    host = Host(store)
    host.subscriptions = [{"thread": THREAD, "since": time.time() - 86400}]

    def ago(seconds):
        return datetime.fromtimestamp(time.time() - seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    state = {"state": "open", "merged": False, "comments": [], "line_comments": []}
    source = GitHubSource(store, host, gh_command=fake_gh(state))
    await source.poll_once()
    state.update(state="closed", ended=ago(3600))
    await source.poll_once()
    assert host.unfollowed == [] and [s["thread"] for s in host.subscriptions] == [THREAD]
    said = host.deliveries[-1][2]
    assert "The PR was closed, not merged. It stays followed for 14 days" in said, said
    seen = len(host.deliveries)
    await source.poll_once()
    assert len(host.deliveries) == seen and host.unfollowed == [], "still followed, nothing new"
    state["state"] = "open"
    await source.poll_once()
    assert "The PR was reopened." in host.deliveries[-1][2]
    # Closed again and left so for longer: let go, and said why.
    host.subscriptions = [{"thread": THREAD, "since": time.time() - 40 * 86400}]
    state.update(state="closed", ended=ago(20 * 86400))
    await source.poll_once()
    assert host.unfollowed[-1][:2] == (THREAD, "missing")
    assert "14 days after it was closed" in host.unfollowed[-1][2]


async def test_a_question_asked_as_its_draft_was_made_is_one_card_the_draft_settles(harness):
    """G6: a review worker asked "I drafted an approval for you to send" a moment before making
    the draft: two cards; once the draft was sent its task still waited on the question, and
    its worker heard of it only with unrelated news."""
    runtime = await harness.connect()
    posted = []

    async def publish_review(target, text):
        posted.append(text)
        return "Posted."

    runtime.github.publish_review = publish_review
    made = await spawn(runtime, harness.router_id, "Review median() PR")
    worker, task = made["session_id"], made["task_id"]
    await call_tool(runtime, "report", worker, status="question", about_draft=True,
                    summary="I drafted an approval for you to send.")  # fmt: skip
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    target = {"repo": "acme/app", "number": 68, "url": "https://github.com/acme/app/pull/68",
              "verdict": "approve", "head_sha": "c0ffee", "task_id": task}  # fmt: skip
    draft = harness.store.create_draft(worker, "github", target, "LGTM.")
    assert runtime.task_actions.view(runtime.tasks.get(task))["question_draft"] == draft["id"]
    sent = len(harness.session(worker).sent)
    await runtime.drafts.resolve(draft["id"], DraftDecision(action="send"))
    await settle(runtime)
    assert posted == ["LGTM."]
    assert runtime.asks.owner_question(f"{worker}:{task}") is None, "answered by sending it"
    assert runtime.tasks.get(task)["status"] == "running"
    assert len(harness.session(worker).sent) == sent + 1, "its worker hears it now"
    assert "it was sent" in harness.session(worker).sent[-1]


async def test_a_worker_waiting_on_another_question_hears_its_draft_was_sent(harness):
    """G6: a task waiting for the owner's answer to something else: its worker hears at once
    what became of its draft (not only with its next message); the question stays open."""
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Answer Bob")
    worker, task = made["session_id"], made["task_id"]
    await call_tool(runtime, "report", worker, status="question", summary="Friday or Monday?")
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    key = f"ownerq:{worker}:{task}"
    asked = json.loads(harness.store.get(key))
    harness.store.set(key, json.dumps(asked | {"at": "2026-01-01T00:00:00+00:00"}))
    await call_tool(runtime, "draft_reply", worker, to="Bob", where="mail", text="On it.")
    [draft] = harness.store.drafts()
    sent = len(harness.session(worker).sent)
    await runtime.drafts.resolve(draft["id"], DraftDecision(action="send"))
    await settle(runtime)
    assert runtime.asks.owner_question(f"{worker}:{task}"), "another question: still open"
    assert runtime.tasks.get(task)["status"] == "waiting"
    assert len(harness.session(worker).sent) == sent + 1, "woken, not held"
    assert "still waits for the owner's answer" in harness.session(worker).sent[-1]


async def test_after_a_merge_the_result_says_nothing_of_following_it(harness):
    """G11: "…before merging. still following." was left of "…before merging. Not merged;
    still following."."""
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Merge note")
    worker, task = made["session_id"], made["task_id"]
    url = "https://github.com/acme/app/pull/72"
    summary = (
        f"Opened {url}. Bob asked for a test before merging. Not merged; still following.\n\n"
        "- Tests pass; not merged.\n- Not merged; the parser is next."
    )
    await call_tool(runtime, "report", worker, status="done", summary=summary)
    await settle(runtime)
    runtime.task_actions.pr_merged(worker, "github:acme/app#72", task)
    shown = runtime.task_actions.view(runtime.tasks.get(task))["result"]
    assert shown == (
        f"acme/app#72 is merged.\n\nOpened {url}. Bob asked for a test before merging.\n\n"
        "- Tests pass.\n- The parser is next."
    ), shown


async def test_retry_says_in_the_routers_chat_what_the_owner_did_not_the_task_id(harness):
    """D1: "The owner asks you to try task t14003d6 “…” again." showed as the owner's message."""
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Fix the parser")
    worker, task = made["session_id"], made["task_id"]
    for note in ("", "Use the fixture from main."):
        runtime.tasks.move(task, "failed", progress="Crashed.")
        await runtime.task_actions.retry(task, note)
        told = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "owner"][-1]
        assert told["text"] == (note or "Try again."), told["text"]
        assert task not in told["meta"]["prompt"]
        assert f"try task {task}" in harness.session(worker).sent[-1], "its worker is told which"


def git(*command, cwd):
    return subprocess.run(["git", *command], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout  # fmt: skip


def commit(tree: Path, name: str, text: str):
    (tree / name).write_text(text)
    git("add", "-A", cwd=tree)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", f"edit {name}", cwd=tree)


async def test_a_result_is_not_out_of_date_because_its_worker_built_on_it_elsewhere(tmp_path):
    """D3: its worker's next task, on a new branch made from the result's commit, changed the
    result's file: "Out of date", though the result's branch still ends at its commit."""
    from test_repos import published, receipts_of

    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Slugify", project=str(app), isolate=True)
    tree, worker, task = Path(made["cwd"]), made["session_id"], made["task_id"]
    for _ in range(50):
        if runtime.store.get(f"taskbase:{task}"):
            break
        await asyncio.sleep(0.05)
    commit(tree, "slug.py", "def slugify(): ...\n")
    await call_tool(runtime, "report", worker, status="done", summary="Slugify.")
    await receipts_of(runtime, task)
    git("checkout", "-q", "-b", "chief/kebab", cwd=tree)
    commit(tree, "slug.py", "def slugify(): ...\n\n\ndef kebab(): ...\n")
    await runtime.task_actions.check_fresh(worker)
    assert not (await receipts_of(runtime, task))["stale"]
    # Its own branch moved on (its file changed there): out of date, though another branch
    # still ends where its result did.
    git("checkout", "-q", made["branch"], cwd=tree)
    git("branch", "kept", cwd=tree)
    commit(tree, "slug.py", "def slugify(): return 1\n")
    await runtime.task_actions.check_fresh(worker)
    found = await receipts_of(runtime, task, lambda found: bool(found["stale"]))
    assert "slug.py" in found["stale"]


async def test_a_task_at_work_after_its_decision_says_what_was_decided(harness):
    """D8: answered on its card, the task at work still showed "Needs your decision: …"."""
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Login API")
    await call_tool(runtime, "ask_decision", made["session_id"],
                    question="Do we support SMS login in v1?",
                    options=[{"choice": "Email only", "effect": "On time"},
                             {"choice": "Email and SMS", "effect": "Two more days"}])  # fmt: skip
    task = runtime.tasks.get(made["task_id"])
    assert task["progress"].startswith("Needs your decision")
    [card] = harness.store.drafts()
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Email only"))
    task = runtime.tasks.get(made["task_id"])
    assert (task["status"], task["progress"]) == ("running", "Decided: Email only")


async def test_a_question_asked_next_to_a_draft_is_its_own_unless_its_worker_says(harness):
    """Round 7 (review of G6): "64 or 128 MB?" asked the moment before an unrelated Slack
    draft was hidden on that draft's card, and sending the draft would have answered it. Only
    a question its worker says is about its draft (about_draft) goes with it."""
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Pick the cache size")
    worker, task = made["session_id"], made["task_id"]
    await call_tool(runtime, "report", worker, status="question", summary="64 or 128 MB?")
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    await call_tool(runtime, "draft_reply", worker, to="Bob", where="mail", text="Sure, Friday.")
    assert not runtime.task_actions.view(runtime.tasks.get(task))["question_draft"]
    [draft] = harness.store.drafts()
    await runtime.drafts.resolve(draft["id"], DraftDecision(action="send"))
    await settle(runtime)
    assert runtime.asks.owner_question(f"{worker}:{task}"), "its question still waits"
