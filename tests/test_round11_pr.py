"""Bug bash round 11: what a PR is to each task. A merge card is its task's, and only that
task's own reads of the PR decide it; a review is the task's that drafted it, not every task
of its worker; a follow from before follows were tied to tasks is one task's; a PR that cannot
be read stays what it was to its task; a push to a PR's branch is a change to it, wherever its
checkout was."""

import json

import pytest
from conftest import Harness, call_tool, settle
from test_round7_split import shell

from chiefofstaff.store import now
from chiefofstaff.task_actions import FAILING_READS, NO_TASK


async def spawn(runtime, router, title, **extra):
    extra.setdefault("instructions", title)
    extra.setdefault("why_new", "separate work")
    result = await call_tool(runtime, "spawn_worker", router, title=title, **extra)
    worker = json.loads(result.text_result_for_llm)["session_id"]
    return worker, runtime.tasks.current(worker)


async def next_task(runtime, router, worker, title):
    """Another task for the same worker (its earlier one, still running, goes on after it)."""
    await call_tool(runtime, "message_session", router, session_id=worker, text=title,
                    task="new", why_new="separate work", title=title, earlier="keep")  # fmt: skip
    [task] = [task for task in runtime.tasks.for_session(worker) if task["title"] == title]
    return task


def pr_view(number=7, **changes):
    """GitHub's view of a PR the owner's account opened: ready to merge unless changed."""
    view = {
        "number": number, "url": f"https://github.com/acme/app/pull/{number}", "state": "OPEN",
        "title": "Fix the login redirect", "isDraft": False, "mergeStateStatus": "CLEAN",
        "headRefOid": "abc1234", "author": {"login": "me"}, "reviewDecision": "APPROVED",
        "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"}],
        "headRefName": "fix-login", "createdAt": "2999-01-01T00:00:00Z",
    }  # fmt: skip
    return json.dumps(view | changes)


def someone_elses(number, branch):
    """A PR someone else opened long ago."""
    return pr_view(number, author={"login": "alice"}, createdAt="2020-01-01T00:00:00Z",
                   headRefName=branch, title=f"Change {number}")  # fmt: skip


def url(number):
    return f"https://github.com/acme/app/pull/{number}"


def approval(number, **extra):
    """What a review draft of the PR is about: approve it."""
    target = {"repo": "acme/app", "number": number, "url": url(number), "verdict": "approve"}
    return target | {"comments": []} | extra


def shown(runtime, task):
    return runtime.task_actions.view(runtime.tasks.get(task["id"]))


def roles(runtime, task):
    """What each PR its reports name is to it (those it only mentions too)."""
    return {pr["label"]: pr["role"] for pr in runtime.task_actions.previous_prs(task["id"])}


def cards(harness):
    return [draft for draft in harness.store.drafts() if draft["kind"] == "merge"]


async def maker_and_reader(harness):
    """A survey that read #7 while it was not ready (an older head) and only names it; then
    the task that opened it, ready to merge now, with its card."""
    runtime = await harness.connect()
    harness.own_pull("acme/app", 7)
    harness.gh_replies[f"pr view {url(7)}"] = pr_view(
        headRefOid="old1234", mergeStateStatus="BLOCKED", reviewDecision="REVIEW_REQUIRED"
    )
    reader, survey = await spawn(runtime, harness.router_id, "Survey the login flow")
    await call_tool(runtime, "report", reader, status="done",
                    summary=f"See {url(7)} for how the redirect works.")  # fmt: skip
    await settle(runtime)
    harness.gh_replies[f"pr view {url(7)}"] = pr_view()
    maker, fix = await spawn(runtime, harness.router_id, "Fix the login redirect")
    await call_tool(runtime, "report", maker, status="done", summary=f"Opened {url(7)}")
    await settle(runtime)
    return runtime, survey, fix


async def test_a_stale_copy_on_a_task_that_only_names_the_pr_leaves_its_merge_card(harness):
    """QA E29: a task that only names a PR kept how it stood hours ago; refreshing it closed
    the card of the task that made the PR, and the next refresh of that one offered it again,
    round and round."""
    runtime, survey, fix = await maker_and_reader(harness)
    [card] = cards(harness)
    assert card["target"]["task_id"] == fix["id"] and card["target"]["head"] == "abc1234"
    for _ in range(3):
        assert await runtime.task_actions.refresh_prs(survey["id"]) is False
        await runtime.task_actions.refresh_prs(fix["id"])
    assert [found["id"] for found in cards(harness)] == [card["id"]], "one card, the same"
    [stale] = runtime.task_actions.previous_prs(survey["id"])
    assert (stale["role"], stale["head"], stale["merge_state"]) == (
        "referenced", "old1234", "BLOCKED"
    )  # fmt: skip
    [own] = shown(runtime, fix)["receipts"]["prs"]
    assert own["role"] == "created"
    # Its own task's copy of the PR from before the card was offered says nothing of it now:
    # neither that it changed, nor that a new head is ready.
    before = "2000-01-01T00:00:00+00:00"
    older = own | {"review": "CHANGES_REQUESTED", "at": before}
    await runtime.task_actions.offer_merge(runtime.tasks.get(fix["id"]), [older])
    kept = own | {"head": "new5678", "at": before}
    await runtime.task_actions.offer_merge(runtime.tasks.get(fix["id"]), [kept], since=now())
    assert [found["id"] for found in cards(harness)] == [card["id"]]


@pytest.mark.parametrize(
    ("change", "outcome"),
    [
        ({"state": "MERGED"}, "The PR is merged."),
        ({"reviewDecision": "CHANGES_REQUESTED"}, "The PR changed since"),
        ({"headRefOid": "def5678", "statusCheckRollup": [{"name": "tests", "status": "QUEUED"}]},
         "The PR changed since"),
    ],
)  # fmt: skip
async def test_the_owning_tasks_own_read_takes_its_merge_card_away(harness, change, outcome):
    runtime, survey, fix = await maker_and_reader(harness)
    [card] = cards(harness)
    harness.gh_replies[f"pr view {url(7)}"] = pr_view(**change)
    await runtime.task_actions.refresh_prs(survey["id"])
    assert harness.store.draft(card["id"])["status"] == "pending", "not by a task that names it"
    await runtime.task_actions.refresh_prs(fix["id"])
    gone = harness.store.draft(card["id"])
    assert gone["status"] == "rejected" and gone["outcome"].startswith(outcome)
    for _ in range(2):
        await runtime.task_actions.refresh_prs(survey["id"])
        await runtime.task_actions.refresh_prs(fix["id"])
    assert not cards(harness)


async def test_a_review_is_its_tasks_not_every_task_of_its_worker(harness):
    """QA E30: a worker reviewed #5 for one task; its next task only mentions #5, and showed
    it as reviewed too."""
    runtime = await harness.connect()
    harness.own_pull("acme/app", 1)
    for number, branch in ((5, "cache"), (6, "limits"), (8, "docs")):
        harness.gh_replies[f"pr view {url(number)}"] = someone_elses(number, branch)
    worker, review = await spawn(runtime, harness.router_id, "Review the cache change")

    def review_of(number, **extra):
        harness.store.create_draft(worker, "github", approval(number, **extra), "LGTM")

    review_of(5)
    # Drafted before drafts said which task they were for: the task whose run was under way.
    review_of(6, task_id="")
    await call_tool(runtime, "report", worker, status="done",
                    summary=f"Reviewed {url(5)} and {url(6)}: both fine.")  # fmt: skip
    # Between its tasks (no run under way): no task's review.
    review_of(8, task_id="")
    docs = await next_task(runtime, harness.router_id, worker, "Document the cache settings")
    summary = f"Documented them; they came in with {url(5)} and {url(6)}."
    await call_tool(runtime, "report", worker, status="done", task_id=docs["id"], summary=summary)
    await settle(runtime)
    actions = runtime.task_actions
    assert actions.reviewed_prs(worker, review["id"]) == {"acme/app#5", "acme/app#6"}
    assert actions.reviewed_prs(worker, docs["id"]) == set()
    assert actions.reviewed_prs(worker) == {"acme/app#5", "acme/app#6", "acme/app#8"}
    assert roles(runtime, review) == {"acme/app#5": "reviewed", "acme/app#6": "reviewed"}
    assert roles(runtime, docs) == {"acme/app#5": "referenced", "acme/app#6": "referenced"}
    assert actions.review_states(worker, docs["id"]) == {}
    assert shown(runtime, review)["receipts"]["prs"][0]["review_state"] == "drafted"


async def test_a_follow_from_before_follows_had_tasks_is_one_tasks(harness):
    """QA E17: a worker followed a PR before follows were tied to tasks; every task of it whose
    title named the PR followed it too, and Home kept the one updated last, not its maker's."""
    runtime = await harness.connect()
    router = harness.router_id
    worker, made = await spawn(runtime, router, "Fix the login redirect")
    runtime.task_actions.record_opened(made["id"], [url(7)])
    checks = await next_task(runtime, router, worker, "Watch CI on acme/app#7")
    notes = await next_task(runtime, router, worker, "Write release notes for acme/app#7")
    other, reviewed = await spawn(runtime, router, "Review the limits change")
    harness.store.create_draft(other, "github", approval(9), "LGTM")
    named = await next_task(runtime, router, other, "Check how acme/app#9 handles limits")
    lone = await next_task(runtime, router, other, "Benchmark big exports with acme/app#11")
    both = [await next_task(runtime, router, other, f"Measure the {what} of acme/app#12")
            for what in ("speed", "memory use")]  # fmt: skip
    # As followed before follows were tied to tasks (or given to follow).
    for session_id, number in ((worker, 7), (other, 9), (other, 11), (other, 12)):
        key = f"github:acme/app#{number}"
        harness.store.delete(f"followtask:{session_id}:{key}")
        harness.store.delete(f"given:{session_id}:{key}")
        harness.store.follow(key, session_id)

    def followed(task):
        found = runtime.tasks.get(task["id"])
        return runtime.task_actions.followed_for(found, harness.store.follows(found["session_id"]))

    assert followed(made) == ["github:acme/app#7"], "its maker's"
    assert followed(checks) == followed(notes) == [], "they only name it"
    assert followed(reviewed) == ["github:acme/app#9"] and followed(named) == []
    assert followed(lone) == ["github:acme/app#11"], "the one task that names it"
    assert [followed(task) for task in both] == [[], []], "never several"
    # Once, at start: tied to that task, as a follow made since is.
    harness.store.delete("migrated:legacy-follows")
    restarted = Harness(harness.store.directory)
    assert restarted.store.follow_tasks(worker, "github:acme/app#7") == [made["id"]]
    assert restarted.store.follow_tasks(other, "github:acme/app#9") == [reviewed["id"]]
    assert restarted.store.follow_tasks(other, "github:acme/app#11") == [lone["id"]]
    assert restarted.store.follow_tasks(other, "github:acme/app#12") == [NO_TASK], "no task's"
    assert [followed(task) for task in both] == [[], []]
    assert restarted.store.get("migrated:legacy-follows")
    # Followed for one of them since: that task's, and let go with it (no task's alone is none).
    runtime.follow(other, ["github:acme/app#12"], task_id=both[0]["id"])
    assert [followed(task) for task in both] == [["github:acme/app#12"], []]
    runtime.task_actions.drop_follows(runtime.tasks.get(both[0]["id"]))
    assert "github:acme/app#12" not in harness.store.follows(other)


async def test_a_pr_followed_for_a_task_given_it_is_that_tasks(harness):
    """QA E17: a follow given to a worker before follows had tasks is the task it then had last,
    not every later task that names the PR."""
    runtime = await harness.connect()
    worker, first = await spawn(runtime, harness.router_id, "Keep an eye on the exports")
    key = "github:acme/app#15"
    harness.store.follow(key, worker)
    harness.store.set(f"given:{worker}:{key}", "1")
    later = await next_task(runtime, harness.router_id, worker, "Check acme/app#15 again later")
    harness.store.delete(f"followtask:{worker}:{key}")  # As before follows had tasks.
    actions = runtime.task_actions
    follows = harness.store.follows(worker)
    assert actions.followed_for(runtime.tasks.get(first["id"]), follows) == [key]
    assert actions.followed_for(runtime.tasks.get(later["id"]), follows) == []


async def test_a_reviewed_pr_that_cannot_be_read_stays_reviewed(harness):
    """QA E18: a PR its worker reviewed could not be read for a while: the task lost what the PR
    is to it (and its other PRs could pass for its deliverable)."""
    runtime = await harness.connect()
    harness.own_pull("acme/app", 1)
    harness.gh_replies[f"pr view {url(5)}"] = someone_elses(5, "cache")
    harness.gh_replies[f"pr view {url(6)}"] = someone_elses(6, "limits")
    worker, review = await spawn(runtime, harness.router_id, "Review the cache change",
                                 instructions=f"Review {url(5)}")  # fmt: skip
    harness.store.create_draft(worker, "github", approval(5), "LGTM")
    key = "github:acme/app#5"
    runtime.follow(worker, [key], task_id=review["id"])
    await call_tool(runtime, "report", worker, status="done",
                    summary=f"Reviewed {url(5)}; it builds on {url(6)}.")  # fmt: skip
    await settle(runtime)
    before = shown(runtime, review)
    assert (before["watching"], before["watch_kind"]) == ([key], "reviewed")
    harness.store.set(f"ghfail:{key}", str(FAILING_READS))
    harness.gh_replies[f"pr view {url(5)}"] = RuntimeError("HTTP 502: Bad Gateway")
    await runtime.task_actions.refresh_prs(review["id"])
    after = shown(runtime, review)
    assert (after["watching"], after["watch_unreadable"]) == ([], [key])
    assert after["watch_kind"] == "reviewed"
    assert after["monitoring"] == before["monitoring"] and after["monitoring"]
    assert roles(runtime, review) == {"acme/app#5": "reviewed", "acme/app#6": "referenced"}


async def test_a_push_to_a_prs_branch_makes_it_changed_wherever_the_checkout_was(harness):
    """QA E25: a worker pushed to a PR's branch from a clone that is not its checkout: the
    task only referenced the PR."""
    runtime = await harness.connect()
    harness.own_pull("acme/app", 1)
    harness.gh_replies[f"pr view {url(12)}"] = someone_elses(12, "cache-fix")
    harness.gh_replies[f"pr view {url(13)}"] = someone_elses(13, "limits")
    harness.gh_replies[f"pr view {url(14)}"] = pr_view(14, headRefName="new-export")
    worker, fix = await spawn(runtime, harness.router_id, "Address the cache review comments")
    pushed = ("remote: Resolving deltas: 100% (2/2), completed with 2 local objects.\n"
              "To github.com:acme/app.git\n   1a2b3c4..5d6e7f8  HEAD -> cache-fix")  # fmt: skip
    shell(harness, worker, "p1", "cd ~/elsewhere/app && git push origin HEAD:cache-fix", pushed)
    refused = ("To https://github.com/acme/app.git\n"
               " ! [rejected]        limits -> limits (fetch first)")  # fmt: skip
    shell(harness, worker, "p2", "git push origin limits", refused)
    made = ("To https://github.com/acme/app.git\n"
            " * [new branch]      new-export -> new-export")  # fmt: skip
    shell(harness, worker, "p3", "git push -u origin new-export", made)
    await settle(runtime)
    summary = f"Fixed the comments on {url(12)}; {url(13)} is next; see {url(14)}."
    await call_tool(runtime, "report", worker, status="done", summary=summary)
    other, survey = await spawn(runtime, harness.router_id, "Survey the cache layer")
    await call_tool(runtime, "report", other, status="done", summary=f"See {url(12)}.")
    await settle(runtime)
    assert roles(runtime, fix) == {
        "acme/app#12": "modified", "acme/app#13": "referenced", "acme/app#14": "created",
    }  # fmt: skip
    assert roles(runtime, survey) == {"acme/app#12": "referenced"}


def counted(store, monkeypatch) -> list[int]:
    """How many times the store is asked anything from now on."""
    calls = [0]
    for name in ("one", "all", "get", "value", "items", "keys", "row", "execute", "set"):
        method = getattr(store, name)

        def counting(*args, _method=method, **kwargs):
            calls[0] += 1
            return _method(*args, **kwargs)

        monkeypatch.setattr(store, name, counting)
    return calls


async def busy_worker(runtime, harness):
    """One worker with 20 tasks, each with ~20 KB of receipts; several name each of six PRs
    its worker follows (from before follows had tasks: tied to none)."""
    worker, first = await spawn(runtime, harness.router_id, "Measure the speed of acme/app#1")
    tasks = [first]
    for index in range(1, 20):
        title = f"Measure the {('speed', 'memory use')[index % 2]} of acme/app#{index % 6 + 1}"
        tasks.append(runtime.tasks.create(worker, title, title, status="done"))
    pulls = [{"label": f"acme/app#{n}", "role": "referenced", "state": "OPEN",
              "title": "x" * 3000, "url": url(n)} for n in range(1, 7)]  # fmt: skip
    for task in tasks:
        run = runtime.tasks.start_run(task["id"], "test")
        evidence = {"checkouts": [], "prs": pulls, "at": now()}
        harness.store.execute(
            "UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run["id"])
        )
    for number in range(1, 7):
        key = f"github:acme/app#{number}"
        harness.store.delete(f"followtask:{worker}:{key}")
        harness.store.delete(f"given:{worker}:{key}")
        harness.store.follow(key, worker)
    return worker, [runtime.tasks.get(task["id"]) for task in tasks]


async def test_follows_tied_to_no_task_cost_little_to_show(harness, monkeypatch):
    """Code review: working out the one task a follow tied to no task is for, on every look at
    every task, made the state of a busy worker's tasks twenty times slower to read. It is
    worked out once per pass, and once for all at start for those from before follows had
    tasks (one that is no single task's too)."""
    runtime = await harness.connect()
    worker, tasks = await busy_worker(runtime, harness)
    follows = harness.store.follows(worker)

    def looks(runtime):
        """Store calls to show every task once, and how often a follow's task was worked out."""
        actions = runtime.task_actions
        find = actions.find_legacy_follower
        worked = []
        monkeypatch.setattr(actions, "find_legacy_follower",
                            lambda *args: worked.append(args) or find(*args))  # fmt: skip
        calls = counted(runtime.store, monkeypatch)
        for task in tasks:
            actions.view(task)
        return calls[0], len(worked)

    harness.store.execute("DELETE FROM subscriptions WHERE session_id=?", (worker,))
    plain, _ = looks(runtime)
    for key in follows:
        harness.store.follow(key, worker)
    unbound, worked = looks(runtime)
    assert worked == len(follows) == 6, "once per follow"
    assert unbound < plain * 2
    harness.store.delete("migrated:legacy-follows")
    restarted = Harness(harness.store.directory)
    assert {restarted.store.follow_tasks(worker, key)[0] for key in follows} == {NO_TASK}
    bound, worked = looks(restarted.runtime)
    assert worked == 0 and bound < plain * 2


async def test_a_pr_not_read_at_its_report_is_not_offered_to_merge_as_it_was(harness):
    """Code review: a PR the task only named, then pushed to; GitHub did not answer when it
    reported: its copy from before the push (ready to merge at the old head) got a card and a
    "Ready to merge" alert."""
    runtime = await harness.connect()
    harness.own_pull("acme/app", 12)
    harness.gh_replies[f"pr view {url(12)}"] = pr_view(12, headRefName="cache-fix")
    worker, task = await spawn(runtime, harness.router_id, "Tune the cache sizes")
    await call_tool(runtime, "report", worker, status="done", summary=f"See {url(12)}.")
    await settle(runtime)
    assert roles(runtime, task) == {"acme/app#12": "referenced"}
    await runtime.task_actions.request_changes(task["id"], "Push the fix onto its branch.")
    pushed = "To github.com:acme/app.git\n   abc1234..def5678  HEAD -> cache-fix"
    shell(harness, worker, "p1", "git push origin HEAD:cache-fix", pushed)
    harness.gh_replies[f"pr view {url(12)}"] = RuntimeError("HTTP 502: Bad Gateway")
    await call_tool(runtime, "report", worker, status="done", summary=f"Pushed to {url(12)}.")
    await settle(runtime)
    [kept] = runtime.task_actions.previous_prs(task["id"])
    assert kept["role"] in {"created", "modified"} and kept["head"] == "abc1234"
    assert not cards(harness)
    assert not [note for note in harness.notifications if note[0] == "Ready to merge"]


async def test_a_dry_run_push_changes_no_pr(harness):
    """Code review: `git push --dry-run` (or -n) says what it would push as a push does."""
    runtime = await harness.connect()
    harness.own_pull("acme/app", 1)
    for number, branch in ((12, "cache-fix"), (13, "limits"), (14, "docs")):
        harness.gh_replies[f"pr view {url(number)}"] = someone_elses(number, branch)
    worker, task = await spawn(runtime, harness.router_id, "Tune the cache sizes")
    would = "To github.com:acme/app.git\n   1a2b3c4..5d6e7f8  HEAD -> cache-fix"
    shell(harness, worker, "d1", "git push --dry-run origin HEAD:cache-fix", would)
    forced = "To github.com:acme/app.git\n + 1a2b3c4...5d6e7f8 limits -> limits (forced update)"
    shell(harness, worker, "d2", "cd ../app && git push -nf origin limits", forced)
    real = "To github.com:acme/app.git\n   1a2b3c4..5d6e7f8  docs -> docs"
    shell(harness, worker, "p1", "git push --no-verify origin docs", real)
    await settle(runtime)
    summary = f"{url(12)} and {url(13)} would take it; pushed to {url(14)}."
    await call_tool(runtime, "report", worker, status="done", summary=summary)
    await settle(runtime)
    assert roles(runtime, task) == {
        "acme/app#12": "referenced", "acme/app#13": "referenced", "acme/app#14": "modified",
    }  # fmt: skip
