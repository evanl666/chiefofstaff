"""Bug bash round 11: what a PR is to each task. A merge card is its task's, and only that
task's own reads of the PR decide it; a review is the task's that drafted it, not every task
of its worker; a follow from before follows were tied to tasks is one task's; a PR that cannot
be read stays what it was to its task; a push to a PR's branch is a change to it, wherever its
checkout was."""

import json

import pytest
from conftest import Harness, call_tool, settle
from test_round7_split import shell

from chiefofstaff.models import DraftDecision, Settings
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
    harness.store.save_settings(Settings(fork_busy=False))  # Each worker's tasks are its own.
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


# What GitHub annotates a job with when it refuses to start it (the account's billing).
BILLING = (
    "The job was not started because recent account payments have failed or your spending "
    "limit needs to be increased. Please check the 'Billing & plans' section in your settings"
)


def refused(number=15):
    """A PR whose CI GitHub never started: each job failed within seconds, annotated so."""
    job = "https://github.com/acme/app/actions/runs/45/job/"
    rollup = [
        {"__typename": "CheckRun", "name": "already-tested", "workflowName": "CI",
         "status": "COMPLETED", "conclusion": "SKIPPED", "detailsUrl": f"{job}116",
         "startedAt": "2999-01-01T00:00:18Z", "completedAt": "2999-01-01T00:00:17Z"},
        {"__typename": "CheckRun", "name": "rules-and-server", "workflowName": "CI",
         "status": "COMPLETED", "conclusion": "FAILURE", "detailsUrl": f"{job}211",
         "startedAt": "2999-01-01T00:00:17Z", "completedAt": "2999-01-01T00:00:19Z"},
        {"__typename": "CheckRun", "name": "e2e", "workflowName": "CI",
         "status": "COMPLETED", "conclusion": "FAILURE", "detailsUrl": f"{job}446",
         "startedAt": "2999-01-01T00:00:17Z", "completedAt": "2999-01-01T00:00:19Z"},
    ]  # fmt: skip
    return pr_view(number, statusCheckRollup=rollup, mergeStateStatus="UNSTABLE",
                   reviewDecision="", title="Phone app")  # fmt: skip


def billing_refused(harness, number=15):
    harness.gh_replies[f"pr view {url(number)}"] = refused(number)
    for job in (211, 446):
        harness.gh_replies[f"api repos/acme/app/check-runs/{job}/annotations"] = json.dumps(
            [{"path": ".github", "annotation_level": "failure", "message": BILLING}]
        )


def opens_pr(harness, worker, number=15):
    """Its worker opens a PR from a checkout outside Chief of Staff's folders, with a long PR
    body first (the command line kept is cut before `gh pr create`)."""
    body = "The phone app, and the server support it needs. " * 60
    command = (
        f"cd /src/app && cat > /src/pr.md <<'EOF'\n{body}\nEOF\ngh pr create --body-file /src/pr.md"
    )
    shell(harness, worker, "pr1", command, url(number))


def annotation_reads(harness) -> int:
    return sum("/annotations" in " ".join(call) for call, _ in harness.gh_calls)


async def test_a_follow_up_split_off_leaves_the_result_seen_and_ci_that_never_ran_is_no_failure(
    harness,
):
    """Owner report 0.1.58: an evaluation the owner had read and followed up on in its chat
    ("go build it") came back as New to accept once the Router split the build off, with the
    PR the build opened (from a checkout of its own) and "Checks are failing" on it, though
    GitHub had never started its CI (the account's Actions minutes used up)."""
    runtime = await harness.connect()
    harness.own_pull("acme/app", 15)
    worker, task = await spawn(runtime, harness.router_id, "Evaluate a phone app for Sly Town")
    await call_tool(runtime, "report", worker, status="done",
                    summary="Research only: no code written, nothing made.")  # fmt: skip
    await settle(runtime)
    # Read in its chat, not opened on Home: followed up on, it was seen.
    await runtime.task_actions.follow_up(task["id"], "Go build it, phones only.")
    shell(harness, worker, "b1", "make app", "app built")
    harness.session(worker).emit("assistant.message", content="Built. Opening the PR next.")
    opens_pr(harness, worker)
    await settle(runtime)
    billing_refused(harness)
    built = f"The phone app is in beta. The server part: {url(15)}"
    await call_tool(runtime, "report", worker, status="done", task_id=task["id"],
                    summary=built)  # fmt: skip
    await settle(runtime)
    split = await call_tool(runtime, "split_task", harness.router_id, task_id=task["id"],
                            title="Deliver the phone app to beta")  # fmt: skip
    assert split.result_type == "success", split.text_result_for_llm
    await settle(runtime)
    [made] = [t for t in runtime.tasks.for_session(worker) if t["id"] != task["id"]]

    kept = shown(runtime, task)
    assert kept["result"].startswith("Research only") and kept["read"], "seen: not New again"
    assert url(15) not in runtime.task_actions.pr_urls(kept), "the PR is the build's"
    assert not [pr for pr in kept["receipts"]["prs"] if pr["label"] == "acme/app#15"]
    assert not any("Checks are failing" in flag for flag in kept["receipts"]["flags"])
    assert not kept["receipts"]["build"] and not kept["links"], "the build's, not the evaluation's"

    build = shown(runtime, made)
    assert build["receipts"]["build"]["passed"]
    assert "acme/app#15" in runtime.task_actions.opened_prs(made["id"])
    [pr] = build["receipts"]["prs"]
    assert (pr["role"], pr["checks"], pr["failing"]) == ("created", "not_run", [])
    assert build["receipts"]["notes"] == [
        "CI didn't run on app#15: GitHub Actions minutes are used up"
    ]
    assert not any("failing" in flag.lower() for flag in build["receipts"]["flags"])
    assert runtime.task_actions.own_pr_standing(runtime.tasks.get(made["id"])) == ("", "")
    assert build["attention"] != "pr-failing"
    told = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "inbound"]
    assert told[-1]["meta"]["task_id"] == made["id"], "the Router's copy names the new task"

    # Read once per check run: the next refresh asks GitHub nothing more of them.
    reads = annotation_reads(harness)
    assert reads == 2
    await runtime.task_actions.refresh_prs(made["id"], first="acme/app#15")
    assert annotation_reads(harness) == reads


async def test_an_accepted_result_followed_up_and_split_off_stays_accepted(harness):
    """The follow-up of an accepted result split off as a task of its own: the result is
    accepted still, not back to accept; undoing the split makes the follow-up's its own again."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Evaluate a phone app for Sly Town")
    await call_tool(runtime, "report", worker, status="done", summary="Research only.")
    await settle(runtime)
    runtime.task_actions.mark_read(task["id"])
    runtime.task_actions.accept(task["id"])
    await runtime.task_actions.follow_up(task["id"], "Go build it, phones only.")
    await call_tool(runtime, "report", worker, status="done", task_id=task["id"],
                    summary="The phone app is in beta.")  # fmt: skip
    assert runtime.tasks.get(task["id"])["status"] == "review"
    made = runtime.task_actions.split(task["id"], "Deliver the phone app to beta")
    kept = shown(runtime, task)
    assert (kept["status"], kept["result"], kept["read"]) == ("done", "Research only.", True)
    assert made["status"] == "review" and not made["read"]
    runtime.task_actions.merge_back(made["id"])
    back = shown(runtime, task)
    assert (back["status"], back["read"]) == ("review", False)
    assert back["result"] == "The phone app is in beta."


async def test_work_reported_as_a_new_task_takes_the_pr_it_opened_with_it(harness):
    """new_task_title: the PR its worker opened from a checkout of its own in the owner's
    follow-up, and the links it reported meanwhile, are the new task's; the result followed
    up on is as the owner had it (read), and the Router hears of the new task."""
    runtime = await harness.connect()
    harness.own_pull("acme/app", 15)
    worker, task = await spawn(runtime, harness.router_id, "Evaluate a phone app for Sly Town")
    await call_tool(runtime, "report", worker, status="done", summary="Research only.")
    await settle(runtime)
    # Read in its chat, not opened on Home: followed up on, it was seen.
    await runtime.task_actions.follow_up(task["id"], "Go build it, phones only.")
    opens_pr(harness, worker)
    blocked = f"Opened {url(15)}; create the app at https://store.acme.dev/apps"
    await call_tool(runtime, "report", worker, status="blocked", task_id=task["id"],
                    summary=blocked)  # fmt: skip
    await runtime.task_actions.follow_up(task["id"], "Created it.")
    billing_refused(harness)
    await call_tool(runtime, "report", worker, status="done", task_id=task["id"],
                    new_task_title="Deliver the phone app to beta",
                    summary="The phone app is in beta.")  # fmt: skip
    await settle(runtime)
    [made] = [t for t in runtime.tasks.for_session(worker) if t["id"] != task["id"]]
    kept = shown(runtime, task)
    assert (kept["status"], kept["result"], kept["read"]) == ("review", "Research only.", True)
    assert url(15) not in runtime.task_actions.pr_urls(kept)
    assert not kept["links"]
    assert url(15) in runtime.task_actions.pr_urls(made)
    assert "https://store.acme.dev/apps" in shown(runtime, made)["links"]
    told = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "inbound"]
    assert told[-1]["meta"]["task_id"] == made["id"]


async def billing_refused_pr(harness, number=17, **changes):
    """Owner report 0.1.61 (sly-town#17): a PR the task opened whose only checks that did not
    pass GitHub never started (the account's Actions minutes used up), after its worker's own
    tests passed. The task, and the merge card it gets."""
    runtime = await harness.connect()
    harness.own_pull("acme/app", number)
    billing_refused(harness, number)
    view = json.loads(refused(number)) | {"headRefOid": "2540e83"} | changes
    harness.gh_replies[f"pr view {url(number)}"] = json.dumps(view)
    worker, task = await spawn(runtime, harness.router_id, "Leave the betting games out")
    shell(harness, worker, "t1", "npm test", "535 passed, 0 failed")
    await call_tool(runtime, "report", worker, status="done",
                    summary=f"Opened {url(number)}; npm test passes.")  # fmt: skip
    await settle(runtime)
    await runtime.task_actions.refresh_prs(task["id"])
    return runtime, task


async def test_a_pr_whose_ci_github_never_started_is_offered_to_merge_saying_so(harness):
    """Owner report 0.1.61: a PR whose CI GitHub refused to start for billing got no Merge
    card (its checks were not "passing", GitHub said UNSTABLE), and the Router told the owner
    to merge it on GitHub. It gets its card, which says CI didn't run and why, and what its
    worker checked itself; the decision is the owner's."""
    runtime, task = await billing_refused_pr(harness)
    [card] = cards(harness)
    target = card["target"]
    assert (target["task_id"], target["head"], target["checks"]) == (
        task["id"],
        "2540e83",
        "not_run",
    )
    assert target["not_run_why"] == "GitHub Actions minutes are used up"
    assert target["not_run"] == ["rules-and-server", "e2e"]
    assert target["verified"] == ["npm test passed"]
    listed = json.loads(
        (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    )["needs_owner"]
    [seen] = [item for item in listed["cards"] if item["id"] == card["id"]]
    assert seen["to"] == "acme/app#17", "the Router sees its Merge card"
    assert seen["ci"] == (
        "CI didn't run (GitHub Actions minutes are used up): the owner decides on its card"
    )
    # Away, its notification says so too.
    assert runtime.connector.describe({"draft": card})[1] == "Phone app (CI didn't run)"
    # Once per head: read again, the same card.
    await runtime.task_actions.refresh_prs(task["id"])
    assert [found["id"] for found in cards(harness)] == [card["id"]]


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"mergeStateStatus": "DIRTY"}, "merge conflicts"),
        ({"reviewDecision": "CHANGES_REQUESTED"}, "changes requested"),
        ({"isDraft": True}, "a draft"),
        ({"mergeStateStatus": "BLOCKED", "reviewDecision": "REVIEW_REQUIRED"}, "a review due"),
    ],
)  # fmt: skip
async def test_a_pr_whose_ci_never_ran_but_that_github_holds_back_gets_no_card(
    harness, changes, why
):
    await billing_refused_pr(harness, **changes)
    assert not cards(harness), why


async def test_a_check_that_ran_and_failed_still_gets_no_merge_card(harness):
    """Beside checks GitHub never started, one that ran for minutes and failed: no card."""
    view = json.loads(refused(17))
    ran = {"__typename": "CheckRun", "name": "lint", "workflowName": "CI", "status": "COMPLETED",
           "conclusion": "FAILURE", "detailsUrl": "https://github.com/acme/app/actions/runs/45/job/9",
           "startedAt": "2999-01-01T00:00:17Z", "completedAt": "2999-01-01T00:04:19Z"}  # fmt: skip
    runtime, task = await billing_refused_pr(
        harness, statusCheckRollup=[*view["statusCheckRollup"], ran]
    )
    assert not cards(harness)
    [pr] = shown(runtime, task)["receipts"]["prs"]
    assert (pr["checks"], pr["failing"]) == ("failing", ["lint"])


async def test_branch_rules_that_require_the_ci_that_never_ran_say_so_at_merge(harness):
    """Offered though GitHub blocks it (a check its branch requires never ran): Merge says
    GitHub's reason, in words, and the card stays for when the checks run."""
    runtime, _ = await billing_refused_pr(harness, mergeStateStatus="BLOCKED")
    [card] = cards(harness)
    harness.gh_replies["api repos/acme/app"] = json.dumps({"allow_squash_merge": True})
    harness.gh_replies["pr merge"] = RuntimeError(
        "GitHub request failed: X Pull request acme/app#17 is not mergeable: the base branch "
        "policy prohibits the merge."
    )
    with pytest.raises(RuntimeError, match="require checks that didn't run"):
        await runtime.drafts.resolve(card["id"], DraftDecision(action="send"))
    await settle(runtime)
    kept = harness.store.draft(card["id"])
    assert kept["status"] == "pending"
    assert kept["outcome"] == (
        "Not merged: GitHub's rules for its branch require checks that didn't run (GitHub "
        "Actions minutes are used up): it can merge once they run and pass."
    )
