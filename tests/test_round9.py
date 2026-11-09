"""Bug bash round 9."""

from pathlib import Path

from conftest import Harness, call_tool, settle
from test_round7_split import based, commit, published, receipts, run, shell, spawn

from chiefofstaff.models import Settings


async def test_separate_work_in_a_worktree_made_before_the_tasks_result_is_not_its(tmp_path):
    """Bug bash 9: asked mid-task for separate work, the worker made its worktree in the task's
    turn, reported the task, then committed the separate work and reported it on its own: the
    task still counted the worktree ("+18 −2 in 4 files") and was flagged "Out of date"."""
    app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Shout", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    await based(runtime, task)
    commit(tree, "shout.py", "def shout(s):\n    return s.upper()\n")
    await runtime.owner_send(session_id, "Separately, in a worktree of its own: add whisper.")
    folder = runtime.workspaces.workspace(harness.store.session(session_id))
    whisper = folder / "whisper-wt"
    shell(harness, session_id, "c1", f"cd {tree} && git worktree add ../whisper-wt -b whisper")
    run("git", "worktree", "add", "-q", "-b", "whisper", str(whisper), cwd=tree)
    await settle(runtime)
    await call_tool(runtime, "report", session_id, status="done", changed_code=True,
                    summary="Added shout.", task_id=task)  # fmt: skip
    before = await receipts(runtime, task)
    shell(harness, session_id, "c2", f"cd {whisper} && git add -A && git commit -qm whisper")
    # Next to shout, in the file the task changed (its worktree started from the task's commit).
    commit(whisper, "shout.py", "def shout(s):\n    return s.upper()\n\n\ndef whisper(s):\n"
           "    return s.lower()\n")  # fmt: skip
    await settle(runtime)
    await call_tool(runtime, "report", session_id, status="done", changed_code=True,
                    summary="Added whisper.", new_task_title="Add whisper")  # fmt: skip
    [made] = [t for t in runtime.tasks.for_session(session_id) if t["id"] != task]
    split = await receipts(runtime, made["id"])
    assert "whisper" in [checkout["branch"] for checkout in split["checkouts"]]
    for _ in range(50):
        await settle(runtime)
        own = runtime.task_actions.view(runtime.tasks.get(task))["receipts"]
        if own["version"] != before["version"] or own["changed"] != before["changed"]:
            break
    assert "whisper" not in [checkout["branch"] for checkout in own["checkouts"]], own
    assert (own["changed"]["commits"], own["changed"]["files"]) == (1, 1), own["changed"]
    await runtime.task_actions.check_fresh(session_id)
    flags = runtime.task_actions.view(runtime.tasks.get(task))["receipts"]["flags"]
    assert not [flag for flag in flags if "Out of date" in flag], flags


async def test_a_retried_task_does_not_claim_what_other_tasks_did_meanwhile(tmp_path):
    """Bug bash 9: a cancelled read-only task, retried after its worker committed two other
    tasks' work in the same checkout, showed "+27 −2 in 5 files" (3 commits) as its own."""
    import asyncio

    app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Run the tests", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, first = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    await based(runtime, first)
    await runtime.task_actions.cancel(first, "not now")
    for _ in range(50):
        await settle(runtime)
        if harness.store.get(f"taskend:{first}"):
            break
        await asyncio.sleep(0.03)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", why_new="test", title="Add shout", earlier="keep",
                    text="Add shout.")  # fmt: skip
    shout = runtime.tasks.current(session_id)
    await based(runtime, shout["id"])
    commit(tree, "shout.py", "def shout(s):\n    return s.upper()\n")
    await call_tool(runtime, "report", session_id, status="done", changed_code=True,
                    summary="Added shout.", task_id=shout["id"])  # fmt: skip
    await receipts(runtime, shout["id"])
    await runtime.task_actions.retry(first)
    await settle(runtime)
    await call_tool(runtime, "report", session_id, status="done", changed_code=False,
                    summary="The tests pass.", task_id=first)  # fmt: skip
    own = await receipts(runtime, first)
    assert not own["changed"].get("commits") and not own["changed"].get("files"), own["changed"]


async def test_messages_past_a_crowded_second_are_said_even_when_the_rest_was_handled(tmp_path):
    """Bug bash 9: 20 of Bob's DMs (handled already) and 2 of Alice's in one second: the
    note about the ones past that page went out only with something new, so never."""
    from test_round8 import BOB, message
    from test_slack_source import FakeHost

    from chiefofstaff.sources.slack import SlackSource
    from chiefofstaff.store import Store

    store = Store(tmp_path / "data")
    store.save_settings(Settings(slack_enabled=True))
    store.set("slack_cursor", "1794182800")
    from conftest import FakeSlackMcp, FakeTokens

    slack = SlackSource(store, FakeHost(store), tokens=FakeTokens(), mcp=FakeSlackMcp())
    stamps = [f"1794182900.{n:06d}" for n in range(20)]
    for stamp in stamps:
        store.add_trigger("slack", f"slack:{stamp}", "")
    slack.mcp.results["to:me"] = "\n".join(message(BOB, stamp, "ping") for stamp in stamps)
    await slack.poll_once()
    [(_name, keys, text, display)] = slack.host.calls
    assert keys == ["slack-crowded:to:me:1794182900"]
    assert "More than 20 messages for `to:me`" in text and "D0BOB0001" in text
    assert "the Router looks for the rest" in display
    await slack.poll_once()
    assert len(slack.host.calls) == 1, "said once"


def test_a_slack_message_reads_as_written_with_names_in_parentheses():
    """Bug bash 9: "Alice Chen (she/her)" lost its ")"; the owner's line flattened a list and
    a code block into one paragraph and showed "<@U0OWNER1>"; the Router read "&gt;"."""
    from test_round8 import message

    from chiefofstaff.sources.slack import for_session, parse, readable

    said = "Steps for <@U0OWNER1>:\n1. pull\n2. run\n```\nmake  test\n```\nif x &gt; 1 &amp;&amp; y"
    alice = ("C0DEPLOYS", "U0ALICE1", "Alice Chen (she/her)")
    listed = message(alice, "1794182901.000100", said)
    [entry], forged = parse(listed)
    assert entry["name"] == "Alice Chen (she/her)" and not forged
    shown = readable(listed, {"U0OWNER1": "Evan", "C0DEPLOYS": "deploys"})
    assert shown.startswith("Alice Chen (she/her) in #deploys:\n> Steps for @Evan:\n> 1. pull\n")
    assert "> ```\n> make  test\n> ```\n> if x > 1 && y" in shown
    assert "> if x > 1 && y" in for_session(listed, {"1794182901.000100"})


def test_a_question_reads_without_draft_ids_or_slack_fields():
    """Bug bash 9: the Slack session's question card said "...channel_id=#deploys, thread_ts=
    1794189315.270463. (... draft d3c7ae145 still waits ...)", in Chinese."""
    from chiefofstaff.tools import owner_words

    comma, stop, opened, closed = "\uff0c", "\u3002", "\uff08", "\uff09"
    draft = "\u8349\u7a3f"  # "draft"
    asked = (
        f"Reply?channel_id=#deploys{comma}thread_ts=1794189315.270463{stop}"
        f"{opened}Bob {draft} d3c7ae145 waits{stop}{closed}"
    )
    assert (
        owner_words(asked, {"d3c7ae145"}) == f"Reply?{stop}{opened}Bob {draft} waits{stop}{closed}"
    )
    assert "d0000aaaa" in owner_words(f"{draft} d0000aaaa", {"d3c7ae145"}), "only drafts there are"


async def test_open_questions_on_home_read_as_the_owner_reads(harness):
    from test_round7_backend import spawn

    runtime = await harness.connect()
    worker = (await spawn(runtime, harness.router_id, "Answer Bob"))["session_id"]
    harness.store.execute(
        "INSERT INTO drafts(id, session_id, kind, target, text, status, created_at) "
        "VALUES('d3c7ae145', ?, 'slack', '{}', 'x', 'pending', '2026-01-01')",
        (worker,),
    )
    harness.store.set(
        f"ownerq:{worker}:-",
        '{"text": "Send it? thread_ts=1794189315.270463 (draft d3c7ae145)", "task_id": "", '
        '"at": "2026-01-01T00:00:00+00:00"}',
    )
    [question] = runtime.snapshot()["questions"]
    assert question["text"] == "Send it? (the draft)", question["text"]


async def test_a_report_goes_to_the_task_what_came_was_about(harness):
    """Bug bash 9 (P1): a worker with a review task and a PR task, both delivered: its reports
    on the review were filed under the PR task (given last, or whose news came last), whose
    result one of them overwrote."""
    from test_round7_backend import spawn

    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Review acme/app#92")
    worker, review = made["session_id"], made["task_id"]
    await call_tool(runtime, "report", worker, status="done", summary="Reviewed #92.")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    task="new", why_new="test", title="Add square", earlier="keep",
                    text="Add square in a PR.")  # fmt: skip
    square = runtime.tasks.current(worker)["id"]
    await call_tool(runtime, "report", worker, status="done", summary="Opened #93.",
                    task_id=square)  # fmt: skip
    runtime.follow(worker, ["github:acme/app#92"], task_id=review)
    runtime.follow(worker, ["github:acme/app#93"], task_id=square)
    await runtime.on_thread("github", "github:acme/app#92", ["github:c:1"], "bob: why mod()?")
    await settle(runtime)
    said = await call_tool(runtime, "report", worker, status="progress",
                           summary="Answered Bob on #92.")  # fmt: skip
    assert said.result_type == "success", said.text_result_for_llm
    assert "Answered Bob" in runtime.tasks.get(review)["progress"]
    assert runtime.tasks.get(square)["result"] == "Opened #93.", "not overwritten"
    await runtime.on_thread("github", "github:acme/app#93", ["github:c:2"], "carol: merged")
    await runtime.on_thread("github", "github:acme/app#92", ["github:c:3"], "bob: thanks")
    await settle(runtime)
    asked = await call_tool(runtime, "report", worker, status="progress", summary="Noted.")
    assert asked.result_type == "failure" and "task_id" in asked.text_result_for_llm


async def test_a_reply_on_a_pr_is_the_task_its_turn_is_on_when_that_names_it(harness):
    """Bug bash 9: on the review task, "draft that reply on the PR conversation" made a draft
    filed under the worker's newer task, whose instructions only said "not on PR #92's
    branch"."""
    from test_round7_backend import spawn

    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Review acme/app#92")
    worker, review = made["session_id"], made["task_id"]
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    task="new", why_new="test", title="Add square", earlier="keep",
                    text="Add square in a new PR, not on PR acme/app#92's branch.")  # fmt: skip
    harness.store.set(f"turntask:{worker}", review)
    target = {"repo": "acme/app", "number": 92}
    assert runtime.draft_task(worker, target) == review


async def test_a_clone_another_task_made_is_not_a_review_tasks_change(tmp_path):
    """Bug bash 9: a review task's card showed "+12 −1 in 2 files", "Out of date" and Create PR
    for the clone its worker's next task made in their shared folder."""
    app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Review acme/app#92",
                         instructions="Review https://github.com/acme/app/pull/92")  # fmt: skip
    session_id, review = worker["session_id"], worker["task_id"]
    await based(runtime, review)
    await call_tool(runtime, "report", session_id, status="done", summary="Reviewed #92.",
                    changed_code=False, task_id=review)  # fmt: skip
    await settle(runtime)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", why_new="test", title="Add square", earlier="keep",
                    text="Clone acme/app and add square.")  # fmt: skip
    square = runtime.tasks.current(session_id)["id"]
    await based(runtime, square)
    folder = runtime.workspaces.workspace(harness.store.session(session_id))
    shell(harness, session_id, "c1", f"git clone {app} {folder / 'app'}")
    run("git", "clone", "-q", str(app), str(folder / "app"))
    shell(harness, session_id, "c2", f"cd {folder / 'app'} && git commit -qam square")
    commit(folder / "app", "square.py", "def square(x):\n    return x * x\n")
    await settle(runtime)
    await call_tool(runtime, "report", session_id, status="done", summary="Added square.",
                    changed_code=True, task_id=square)  # fmt: skip
    made = await receipts(runtime, square)
    assert made["changed"]["commits"] == 1, made["changed"]
    await runtime.task_actions.collect_evidence(review)
    own = runtime.task_actions.view(runtime.tasks.get(review))["receipts"]
    assert not (own or {}).get("checkouts"), own
    # A later task that works in that clone has its work there.
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", why_new="test", title="Add cube", earlier="keep",
                    text="Add cube in that clone.")  # fmt: skip
    cube = runtime.tasks.current(session_id)["id"]
    await based(runtime, cube)
    shell(harness, session_id, "c3", f"cd {folder / 'app'} && git commit -qam cube")
    commit(folder / "app", "cube.py", "def cube(x):\n    return x**3\n")
    await settle(runtime)
    await call_tool(runtime, "report", session_id, status="done", summary="Added cube.",
                    changed_code=True, task_id=cube)  # fmt: skip
    later = await receipts(runtime, cube)
    assert later["changed"]["commits"] == 1, later["changed"]


def pr_gh(state: dict):
    """test_github_source's fake gh, with what a PR says of its base and branch."""
    import json

    from test_github_source import fake_gh

    plain = fake_gh(state)

    async def gh(*arguments, payload=None):
        path = arguments[1]
        if path == "repos/acme/app/pulls/7":
            pull = json.loads(await plain(*arguments, payload=payload))
            pull["mergeable_state"] = state.get("mergeable_state", "clean")
            pull["base"] = {"ref": "main", "sha": state.get("base", "b1")}
            pull["head"] |= {"ref": "fix-x", "repo": {"full_name": "acme/app"}}
            return json.dumps(pull)
        if path == "repos/acme/app/git/ref/heads/fix-x":
            if state.get("branch_gone"):
                raise RuntimeError("GitHub request failed: gh: Not Found (HTTP 404)")
            return "{}"
        return await plain(*arguments, payload=payload)

    return gh


async def test_merge_conflicts_and_a_deleted_branch_are_news(tmp_path):
    """Bug bash 9: a PR with conflicts with its base (#92 after #93 merged) reached nobody, and
    a PR closed by deleting its branch promised "a reopening reaches you"."""
    from test_github_source import Host

    from chiefofstaff.sources.github import GitHubSource
    from chiefofstaff.store import Store

    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    state = {"state": "open", "merged": False, "comments": [], "line_comments": []}
    host = Host(store)
    source = GitHubSource(store, host, gh_command=pr_gh(state))
    await source.poll_once()
    state["mergeable_state"] = "dirty"
    await source.poll_once()
    assert (
        "It has merge conflicts with main: merge or rebase it and push." in (host.deliveries[-1][2])
    )
    told = len(host.deliveries)
    await source.poll_once()
    assert len(host.deliveries) == told, "once per head and base"
    state |= {"state": "closed", "ended": "2999-01-01T00:00:00Z", "branch_gone": True}
    await source.poll_once()
    assert "its branch fix-x was deleted" in host.deliveries[-1][2]
    assert "a reopening reaches you" not in host.deliveries[-1][2]


def test_a_task_whose_pr_has_conflicts_needs_you():
    from chiefofstaff.task_actions import pr_status

    assert pr_status({"state": "OPEN", "merge_state": "DIRTY", "review": "APPROVED"}) == (
        "merge conflicts"
    )


async def test_a_worker_no_longer_waits_once_its_question_is_settled_otherwise(harness):
    """Bug bash 9: its question settled by sending the draft it asked about (its task done),
    the worker's box still said "waits for you"."""
    from test_round7_backend import spawn

    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Answer Alice")
    worker, task = made["session_id"], made["task_id"]
    await call_tool(runtime, "report", worker, status="question", summary="Send it as it is?")
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    assert harness.store.session(worker)["status"] == "waiting"
    runtime.tasks.move(task, "done", reason="its draft was sent")
    assert harness.store.session(worker)["status"] == "idle"


async def test_the_watchdogs_line_says_what_it_decided_in_words(harness):
    """Bug bash 9: "“Write sea haiku to sea.txt”: on track, none."."""
    from test_round7_backend import spawn

    runtime = await harness.connect()
    worker = (await spawn(runtime, harness.router_id, "Write sea haiku"))["session_id"]
    watchdog = await runtime.watchdog.session_id()
    await call_tool(runtime, "assess_worker", watchdog, session_id=worker, verdict="on_track",
                    action="none", reason="Writing it.")  # fmt: skip
    [line] = [m["text"] for m in harness.store.messages(watchdog) if m["kind"] == "notice"]
    assert line == "“Write sea haiku”: on track: no action. Writing it.", line


def test_a_ts_listed_after_where_a_message_is_leaves_the_parenthesis_whole():
    from chiefofstaff.tools import owner_words

    asked = "Alice Chen (DM D0ALICE01, ts 1794141072.949527) asked the owner"
    assert owner_words(asked, set()) == "Alice Chen (DM D0ALICE01) asked the owner"


async def test_a_run_in_which_no_test_ran_is_said_so_everywhere(tmp_path):
    """Bug bash 9: a run in which no test ran read "failed (exit None)" in the copied report and
    held the worker's report back as a failing test run."""
    from test_round7_split import published

    app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Run the tests", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    session_id, task = worker["session_id"], worker["task_id"]
    await based(runtime, task)
    shell(harness, session_id, "c1", "pytest -q tests/test_none.py 2>&1 | tail -3",
          "no tests ran in 0.01s")  # fmt: skip
    await settle(runtime)
    said = await call_tool(runtime, "report", session_id, status="done", changed_code=False,
                           summary="Ran the tests.", task_id=task)  # fmt: skip
    assert said.result_type == "success", said.text_result_for_llm
    for _ in range(50):
        await settle(runtime)
        if (runtime.task_actions.view(runtime.tasks.get(task))["receipts"] or {}).get("tests"):
            break
    assert "`pytest -q tests/test_none.py` ran no tests" in (
        runtime.task_actions.report_markdown(task)
    )


def real(path) -> Path:
    return Path(path).resolve()


async def test_a_worker_the_owner_asked_to_work_in_their_folder_can_write_there(
    tmp_path, monkeypatch
):
    """Live run 34: "work directly in my project folder (no worktree, no commit)" was refused:
    since bug bash 8 a worker given the owner's folder only reads it. Asked in so many words,
    the Router passes in_place, and that folder is the worker's to change."""
    from types import SimpleNamespace

    app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)], worker_sandbox=True))
    runtime = await harness.connect()
    refused = await call_tool(runtime, "spawn_worker", harness.router_id, title="Both",
                              instructions="x", project=str(app), isolate=True, in_place=True,
                              why_new="test")  # fmt: skip
    assert refused.result_type == "failure" and "exclude each other" in (
        refused.text_result_for_llm
    )
    info = await spawn(runtime, harness.router_id, title="Shout Fn", instructions="append shout",
                       project=str(app), in_place=True)  # fmt: skip
    row = harness.store.session(info["session_id"])
    policy = harness.session(info["session_id"]).options["sandbox_config"]
    assert {p for p in policy.readwrite if real(p) == real(app)}
    assert (
        "asked you to change in place"
        in (harness.session(info["session_id"]).options["system_message"]["content"])
    )
    monkeypatch.setattr("tempfile.gettempdir", lambda: "/no-temp-here")
    request = SimpleNamespace(kind="write", file_name="", resolved_path=str(app / "notes.txt"))
    decision = await runtime.events.on_permission(request, {"session_id": row["id"]})
    assert type(decision).__name__ == "PermissionDecisionApproveOnce"


def test_a_task_that_worked_in_a_clone_by_its_bare_name_keeps_it():
    """Code review: `cd foo && git commit -am fix` or `git -C foo …` (no ./ or full path) did
    not count as working in the clone `foo` another task made: the task lost its work."""
    from chiefofstaff.task_actions import works_in

    path = "/Users/evan/ws/fix-abc123/foo"
    for command in ("cd foo && git commit -am fix", "git -C foo commit -am fix",
                    "cd ~/ws/fix-abc123/foo", "python foo/x.py", f"cd {path}"):  # fmt: skip
        assert works_in(command, path), command
    for command in ("git commit -am fix", "cd foobar", "cd ../foo-docs", "gh pr diff 92"):
        assert not works_in(command, path), command


async def test_the_crowded_second_note_is_said_once_with_new_messages_too(tmp_path):
    """Code review: said with two new messages, the note came again on its own next poll."""
    from conftest import FakeSlackMcp, FakeTokens
    from test_round8 import BOB, message
    from test_slack_source import FakeHost

    from chiefofstaff.sources.slack import SlackSource
    from chiefofstaff.store import Store

    store = Store(tmp_path / "data")
    store.save_settings(Settings(slack_enabled=True))
    store.set("slack_cursor", "1794182800")
    slack = SlackSource(store, FakeHost(store), tokens=FakeTokens(), mcp=FakeSlackMcp())
    stamps = [f"1794182900.{n:06d}" for n in range(20)]
    for stamp in stamps[:18]:
        store.add_trigger("slack", f"slack:{stamp}", "")
    slack.mcp.results["to:me"] = "\n".join(message(BOB, stamp, "ping") for stamp in stamps)
    await slack.poll_once()
    [(_name, keys, text, _display)] = slack.host.calls
    assert len(keys) == 2 and "More than 20 messages" in text
    store.set("slack_cursor", "1794182800")  # Its look-back reaches that second again.
    await slack.poll_once()
    assert len(slack.host.calls) == 1, "not said again"


def test_a_ts_in_a_phrase_reads_as_the_message_once_and_keeps_its_parenthesis():
    """Live run 34: "(DM D0ALICE01, message ts 1794….) asks" became "message the message asks",
    its ")" gone."""
    from chiefofstaff.tools import owner_words

    said = "Alice Chen (DM D0ALICE01, message ts 1794141072.949527) asks: x"
    assert owner_words(said, set()) == "Alice Chen (DM D0ALICE01, the message) asks: x"
    assert owner_words("Bob (ts 1794141072.949527) asked", set()) == "Bob asked"
