"""QA round 12, GitHub: nothing hidden in what Chief of Staff posts (E37); news on a PR whose
task is not known goes to the Router (E46); PR roles read by older rules are read again (E45)."""

import asyncio
import json

from conftest import Harness, call_tool, settle
from test_round11_pr import pr_view, roles, someone_elses, spawn, url

from chiefofstaff.models import Settings
from chiefofstaff.sources.github import POSTED_MARK
from chiefofstaff.store import now
from chiefofstaff.task_actions import ROLE_REREADS, ROLE_RULES

ACCOUNTS = {"hosts": {"github.com": [{"login": "owner", "state": "success", "active": True}]}}


def review(number: int, review_id: int, body: str, comments: list[tuple[int, str]]) -> dict:
    url = f"https://github.com/acme/app/pull/{number}"
    return {
        "id": f"R{review_id}",
        "url": f"{url}#pullrequestreview-{review_id}",
        "body": body,
        "comments": {
            "nodes": [
                {"id": f"C{cid}", "url": f"{url}#discussion_r{cid}", "body": text, "path": "a.py"}
                for cid, text in comments
            ]
        },
    }


async def test_what_is_posted_carries_nothing_hidden_and_is_still_not_learned_as_yours(tmp_path):
    """E37: every review, line comment and reply ended with <!-- chief-of-staff -->, seen when
    it is edited, quoted or read through the API. Learning how the owner writes still leaves
    out what Chief of Staff posted: by its id, else (the id lost) by its words on that PR."""
    harness = Harness(tmp_path / "data", Settings(github_enabled=True, github_repos=["acme/app"]))
    runtime = await harness.connect()
    github = runtime.github
    harness.gh_replies |= {
        "auth status": json.dumps(ACCOUNTS),
        "auth token": "tok",
        "api repos/acme/app/pulls/7": json.dumps({"state": "open", "head": {"sha": "abc"}}),
        "api repos/acme/app/pulls/7/reviews": json.dumps(
            {"id": 41, "html_url": "https://github.com/acme/app/pull/7#pullrequestreview-41"}
        ),
        # Its line comments could not be listed: their ids are not known.
        "api repos/acme/app/pulls/7/reviews/41/comments": RuntimeError("HTTP 502"),
        "api repos/acme/app/pulls/7/comments/21/replies": json.dumps({"id": 52}),
        "api repos/acme/app/issues/7/comments": json.dumps({"id": 63}),
    }
    target = {"repo": "acme/app", "number": 7, "head_sha": "abc"}
    line = {"path": "a.py", "line": 3, "body": "Why 3 retries?"}
    await github.publish_review(target | {"comments": [line]}, "Two small things.")
    await github.publish_reply(target | {"in_reply_to": 21}, "Fixed in f61047f.")
    await github.publish_conversation_reply(target | {"reply_to_comment": 30}, "@bob Done.")
    payloads = [payload for _, payload in harness.gh_calls if payload]
    bodies = [p["body"] for p in payloads] + [
        c["body"] for p in payloads for c in p.get("comments", [])
    ]
    assert bodies == ["Two small things.", "Fixed in f61047f.", "@bob Done.", "Why 3 retries?"]

    reviews = [
        review(7, 41, "Two small things.", [(77, "Why 3 retries?")]),  # Posted above.
        review(7, 42, "", [(78, "Why  3 retries?\n")]),  # The same words, spaced otherwise.
        # Posted by an older version (on another install): its mark says so.
        review(8, 43, f"Looks good.\n\n{POSTED_MARK}", [(79, f"Rename this.\n\n{POSTED_MARK}")]),
        # The owner's own, on another PR: the same words are theirs there.
        review(8, 44, "Nice. Ship it.", [(80, "Why 3 retries?")]),
    ]
    pull = {"title": "Retry", "repository": {"nameWithOwner": "acme/app"}, "reviews": {}}
    pull["reviews"]["nodes"] = reviews
    harness.gh_replies["api graphql"] = json.dumps({"data": {"search": {"nodes": [pull]}}})
    assert await runtime.habits.collect_reviews() == 2
    learned = harness.store.all("SELECT ref, text FROM observations ORDER BY ref")
    assert [(row["ref"], row["text"]) for row in learned] == [
        ("github:C80", "Why 3 retries?"),
        ("github:R44", "Nice. Ship it."),
    ]


async def test_news_on_a_pr_no_task_made_or_follows_goes_to_the_router(harness):
    """E46: a followed PR that no task of its worker made or follows: its news went to the
    newest task that names it. Which task it is about is the Router's to say."""
    runtime = await harness.connect()
    started = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Fix the login flow",
        instructions="Fix the login. For context, see acme/app#9.", why_new="test",
    )  # fmt: skip
    worker = json.loads(started.text_result_for_llm)["session_id"]
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, task="new",
        why_new="test", earlier="keep", title="Update the login docs",
        text="Update the docs; acme/app#9 did the same for signup, for context.",
    )  # fmt: skip
    thread = "github:acme/app#9"
    harness.store.follow(thread, worker)  # Followed for none of its tasks.
    assert not harness.store.follow_tasks(worker, thread)
    router = harness.session(harness.router_id)
    before = (len(router.sent), len(harness.session(worker).sent))
    news = "New review comment on acme/app#9 by bob: please split this."
    assert await runtime.on_thread("github", thread, ["github:reviewcomment:5"], news)
    await settle(runtime)
    assert len(harness.session(worker).sent) == before[1], "not the newest task that names it"
    assert len(router.sent) == before[0] + 1 and "please split this" in router.sent[-1]
    assert 'type="event"' in router.sent[-1]
    # Followed for one of them, it is that task's.
    runtime.follow(worker, [thread], task_id=runtime.tasks.for_session(worker)[0]["id"])
    news = "New review comment on acme/app#9 by bob: and rename it."
    assert await runtime.on_thread("github", thread, ["github:reviewcomment:6"], news)
    await settle(runtime)
    first = runtime.tasks.for_session(worker)[0]["id"]
    assert "and rename it" in harness.session(worker).sent[-1]
    assert f'task="{first}"' in harness.session(worker).sent[-1]
    assert len(router.sent) == before[0] + 1


async def test_pr_roles_read_by_older_rules_are_read_again_once(harness):
    """E45: what a PR is to its task, as an older version read it, was never read again once
    the PR was merged or closed (or its task a few days old): one it opened stayed
    "referenced", one it never reviewed stayed "reviewed". Read again once, in the background,
    a few tasks a round."""
    runtime = await harness.connect()
    actions = runtime.task_actions
    _worker, task = await spawn(runtime, harness.router_id, "Fix the login redirect")
    runtime.tasks.move(task["id"], "done", result=f"Opened {url(9)}; see {url(10)}.")
    long_ago = "2026-01-01T00:00:00+00:00"
    harness.store.execute("UPDATE tasks SET created_at=?, ended_at=?", (long_ago, long_ago))
    actions.record_opened(task["id"], [url(9)])
    older = [
        {"label": "acme/app#9", "role": "referenced", "state": "MERGED", "url": url(9)},
        {"label": "acme/app#10", "role": "reviewed", "state": "CLOSED", "url": url(10)},
    ]
    older = [pr | {"at": long_ago} for pr in older]
    [run] = harness.store.all("SELECT id FROM runs WHERE task_id=?", (task["id"],))
    evidence = json.dumps({"checkouts": [], "prs": older, "at": "2026-01-02T00:00:00+00:00"})
    harness.store.execute("UPDATE runs SET evidence=? WHERE id=?", (evidence, run["id"]))
    # Receipts read since by today's rules (an earlier run's, read by older ones, is not what
    # its card shows), and those of a recent open PR, read again as it changes: not read again.
    _other, today = await spawn(runtime, harness.router_id, "Fix the signup redirect")
    [earlier] = harness.store.all("SELECT id FROM runs WHERE task_id=?", (today["id"],))
    harness.store.execute(
        "UPDATE runs SET evidence=?, started_at=? WHERE id=?", (evidence, long_ago, earlier["id"])
    )
    runtime.tasks.end_runs(today["id"], "done")
    runtime.tasks.start_run(today["id"], "test")
    actions.record_opened(today["id"], [url(11)])
    harness.gh_replies[f"pr view {url(11)}"] = pr_view(11)
    await actions.collect_evidence(today["id"])
    _third, recent = await spawn(runtime, harness.router_id, "Fix the reset redirect")
    open_pr = [{"label": "acme/app#12", "role": "reviewed", "state": "OPEN", "url": url(12)}]
    unread = json.dumps({"prs": [pr | {"at": long_ago} for pr in open_pr]})
    harness.store.execute("UPDATE runs SET evidence=? WHERE task_id=?", (unread, recent["id"]))
    actions.record_opened(recent["id"], [url(12)])
    harness.gh_replies[f"pr view {url(12)}"] = pr_view(12)
    harness.gh_replies[f"pr view {url(9)}"] = pr_view(9, state="MERGED")
    harness.gh_replies[f"pr view {url(10)}"] = someone_elses(10, "cache")
    await actions.refresh_open_prs()
    assert roles(runtime, task) == {"acme/app#9": "referenced", "acme/app#10": "reviewed"}
    assert roles(runtime, recent) == {"acme/app#12": "created"}
    harness.gh_calls.clear()

    await actions.reread_roles()
    assert roles(runtime, task) == {"acme/app#9": "created", "acme/app#10": "referenced"}
    read = [call for call, _ in harness.gh_calls if call[:2] == ("pr", "view")]
    assert sorted(call[2] for call in read) == sorted([url(9), url(10)]), "its two, no other"
    await settle(runtime)
    harness.gh_calls.clear()
    await actions.reread_roles()
    assert not harness.gh_calls and harness.store.get("role_rules") == str(ROLE_RULES)
    assert roles(runtime, task)["acme/app#9"] == "created"


async def test_roles_are_read_again_in_the_background(harness, monkeypatch):
    runtime = await harness.connect()
    called = asyncio.Event()

    async def reread_roles():
        called.set()

    monkeypatch.setattr(runtime.task_actions, "reread_roles", reread_roles)
    monkeypatch.setattr(runtime.watch, "interval", 0.01)
    loop = asyncio.create_task(runtime.watch_loop())
    try:
        await asyncio.wait_for(called.wait(), 10)
    finally:
        loop.cancel()


async def test_roles_are_read_again_a_few_tasks_a_round_and_once_each(harness):
    """E45: bounded; a task whose PRs cannot be read now keeps what it had, read once."""
    runtime = await harness.connect()
    harness.gh_replies["pr view"] = RuntimeError("HTTP 502: Bad Gateway")
    pulls = [
        {"label": "acme/app#9", "role": "reviewed", "state": "MERGED", "url": url(9), "at": now()}
    ]
    worker, first = await spawn(runtime, harness.router_id, "Measure acme/app#9")
    for index in range(ROLE_REREADS + 2):
        task = first if not index else runtime.tasks.create(worker, f"Measure {index}", "x")
        run = runtime.tasks.start_run(task["id"], "test") if index else None
        run_id = (
            run["id"]
            if run
            else harness.store.value("SELECT id FROM runs WHERE task_id=?", (task["id"],))
        )
        evidence = json.dumps({"prs": pulls, "at": now()})
        harness.store.execute("UPDATE runs SET evidence=? WHERE id=?", (evidence, run_id))
        runtime.task_actions.record_opened(task["id"], [url(9)])

    def reads() -> int:
        return len([call for call, _ in harness.gh_calls if call[:2] == ("pr", "view")])

    await runtime.task_actions.reread_roles()
    assert reads() == ROLE_REREADS
    await runtime.task_actions.reread_roles()
    assert reads() == ROLE_REREADS + 2
    await runtime.task_actions.reread_roles()
    assert reads() == ROLE_REREADS + 2, "once each"
    # Not read now, each PR is what today's rules make of it as it was last read.
    assert roles(runtime, first) == {"acme/app#9": "created"}
