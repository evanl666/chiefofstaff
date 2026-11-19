"""Prepared for you, end to end on a real repository (tests/shepherd_world.py): a worker's PR
with failing CI, kept moving by the shepherd: its attempt in the step profile, the fix checked on
frozen commits, the owner's approval, a leased push, durable acceptance and a head-matched
merge."""

import json
from pathlib import Path

import pytest
from conftest import settle
from shepherd_world import BRANCH, TARGET, TEST_COMMAND, World

from chiefofstaff.backend import StepSandboxPolicy
from chiefofstaff.publish import approve


@pytest.fixture
def world(harness, tmp_path):
    return World(harness, tmp_path)


async def claimed(world, **settings):
    runtime = await world.start(**settings)
    await runtime.shepherd.tick()
    await settle(runtime)
    claim = runtime.shepherd.control.claim(TARGET)
    assert claim and claim["holder"] == world.task
    return runtime


NUMBERED = {"1.", "2.", "3.", "4."}


async def step_result(runtime, caller, **arguments):
    """The worker calls shepherd_result (a step's own tool)."""
    from copilot.tools import ToolInvocation

    [tool, _] = runtime.toolset.step_tools()
    return await tool.handler(
        ToolInvocation(
            session_id=caller, tool_call_id="r1", tool_name=tool.name, arguments=arguments
        )
    )


def said(answer) -> str:
    """A tool's answer, as the session reads it."""
    return getattr(answer, "text_result_for_llm", answer)


async def fixed(world, runtime):
    """Fix it, the worker's commit, its frozen runs, its turn's end: a prepared plan."""
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    assert step["state"] == "running", step
    session = world.session()
    sent = session.sent[-1]
    assert f'step="{step["id"]}"' in sent and "Do not push" not in sent
    sha = world.commit_fix(step["tree"])
    answer = await step_result(
        runtime,
        world.worker,
        step=step["id"],
        outcome="fixed",
        summary="timeout() returned 31.",
        commit=sha,
        test_command=TEST_COMMAND,
        failing_tests=["tests/test_app.py::test_timeout"],
    )
    lines = [line.split(". ", 1)[1] for line in said(answer).splitlines() if line[:2] in NUMBERED]
    assert len(lines) == 4, answer
    for index, line in enumerate(lines):
        await world.ran(line, f"c{index}")
    await step_result(runtime, world.worker, step=step["id"], outcome="checked", summary="ran them")
    session.emit("session.idle")
    await settle(runtime)
    await settle(runtime)
    return runtime.shepherd.control.step(step["id"]), sha


async def test_a_failing_ci_fix_prepared_checked_approved_and_pushed(world):
    runtime = await claimed(world)
    step, sha = await fixed(world, runtime)
    assert step["state"] == "prepared", step
    plan = step["plan"]
    assert plan["kind"] == "fix" and plan["h1"] == sha and plan["h0"] == world.h0
    assert plan["change"] == "eligible"
    evidence = plan["evidence"]
    assert evidence["holds"] and evidence["checked"], evidence
    assert evidence["words"] == "`test_timeout` failed before and passes now; 2 tests ran on both"
    # The worker is let go of, unmarked, idle; its task's row offers the review.
    assert not runtime.shepherd.marked(world.worker)
    assert runtime.store.session(world.worker)["status"] == "idle"
    view = runtime.task_actions.view(runtime.tasks.get(world.task))
    assert view["shepherd"]["action"] == {
        "kind": "review",
        "label": "Review fix",
        "step": step["id"],
    }
    # Approved: exactly H1 is pushed, leased on H0, as the bound account, hooks off.
    await approve(runtime.shepherd, step["id"], {})
    await settle(runtime)
    await settle(runtime)
    assert world.github.head() == sha
    final = runtime.shepherd.control.step(step["id"])
    assert final["state"] == "published", runtime.shepherd.control.actions(step["id"])
    claim = runtime.shepherd.control.claim(TARGET)
    assert claim["published"] == sha and claim["approved"] == [sha]
    assert runtime.shepherd.control.explains(claim, sha)


async def test_the_step_profile_is_the_sessions_options_while_it_runs(world, monkeypatch):
    owners = [str(world.root / "owner.gitconfig")]
    monkeypatch.setattr("chiefofstaff.shepherd.git_config_files", lambda: owners)
    runtime = await claimed(world)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    options = world.session().options
    policy = options["sandbox_config"]
    assert isinstance(policy, StepSandboxPolicy)
    assert (policy.credentials, policy.keychain) == (False, False)
    assert (
        "github.com" in policy.blocked_hosts and "*.githubusercontent.com" in policy.blocked_hosts
    )
    assert step["tree"] in policy.readwrite and step["folder"] in policy.readwrite
    assert any(path.endswith(".ssh") for path in policy.denied)
    assert any(path.endswith(".config/gh") for path in policy.denied)
    assert not policy.local_network
    assert policy.allowed_hosts == () and policy.outbound is False  # No project hosts here.
    # The owner's git configuration is readable (git refuses to run without it); nothing else
    # of theirs is.
    assert step["evidence"]["git_config"] == owners and set(owners) <= set(policy.readonly)
    assert options["enable_config_discovery"] is False and options["enable_session_store"] is False
    assert "mcp_servers" not in options and "hook_directories" not in options
    assert "github-mcp-server" in options["disabled_mcp_servers"]
    assert set(options["available_tools"]) >= {"bash", "shepherd_result", "draft_github_review"}
    assert "tell_session" not in options["available_tools"]
    assert "report" not in options["available_tools"]
    assert [tool.name for tool in options["tools"]] == ["shepherd_result", "draft_github_review"]
    # Its permission checks apply, whatever its mode.
    row = runtime.store.session(world.worker)
    assert runtime.yolo_choice(row) and not runtime.yolo(row)
    # The system prompt is the worker's own, unchanged.
    runtime.shepherd.unmark(world.worker)
    plain = runtime.options(row)
    assert options["system_message"] == plain["system_message"]


async def test_a_step_has_none_of_the_workers_mcp_servers(world, monkeypatch, tmp_path):
    """Copilot's own (its mcp-config.json), Settings' and a worker's extra MCP servers: none is
    configured for a step, each is disabled by name (Copilot then never starts it), and the
    step's tools are only its own (one already running stays out of reach)."""
    from chiefofstaff.policy import GITHUB_MCP
    from chiefofstaff.shepherd import STEP_TOOLS

    local = {"type": "local", "command": "true", "args": [], "tools": ["*"]}
    copilot = tmp_path / "copilot-home"
    copilot.mkdir()
    (copilot / "mcp-config.json").write_text(json.dumps({"mcpServers": {"owner-tools": local}}))
    monkeypatch.setenv("COPILOT_HOME", str(copilot))
    monkeypatch.setenv("CHIEFOFSTAFF_WORKER_MCP", json.dumps({"probe-marker": local}))
    docs = {"type": "http", "url": "https://docs.example.com/mcp"}
    runtime = await claimed(world, mcp_servers={"acme-docs": docs})
    plain = runtime.options(runtime.store.session(world.worker))
    assert {"acme-docs", "probe-marker"} <= set(plain["mcp_servers"])
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    options = world.session().options
    assert "mcp_servers" not in options
    disabled = options["disabled_mcp_servers"]
    assert {GITHUB_MCP, "owner-tools", "acme-docs", "probe-marker", "slack"} <= set(disabled)
    assert len(disabled) == len(set(disabled))
    assert options["available_tools"] == list(STEP_TOOLS)


@pytest.mark.parametrize("slack", [False, True])
async def test_a_step_reaches_only_what_the_owner_allowed_and_a_slack_reader_nothing(
    world, monkeypatch, tmp_path, slack
):
    """The owner's extra hosts, the repository's own servers and the shared caches reach a
    step; a worker that reads Slack gets none of them: no network at all."""
    from shepherd_world import REPO

    from chiefofstaff.models import RepositoryChoices

    caches = tmp_path / "caches"
    caches.mkdir()
    monkeypatch.setattr("chiefofstaff.shepherd.shared_caches", lambda: [str(caches)])
    runtime = await claimed(
        world,
        step_hosts=["registry.acme.dev"],
        proactive_repos={REPO: RepositoryChoices(localhost=True)},
        shared_caches=True,
    )
    if slack:
        runtime.store.execute("UPDATE sessions SET slack=1 WHERE id=?", (world.worker,))
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    assert runtime.shepherd.control.open_step(TARGET)["state"] == "running"
    policy = world.session().options["sandbox_config"]
    reach = (policy.allowed_hosts, policy.outbound, policy.local_network)
    if slack:
        assert reach == ((), False, False)
        assert str(caches) not in policy.readwrite
    else:
        assert reach == (("registry.acme.dev",), True, True)
        assert str(caches) in policy.readwrite
    assert "github.com" in policy.blocked_hosts and policy.credentials is False


async def test_hooks_in_the_repository_never_run_when_it_pushes(world):
    """A hook the worker set up (husky's core.hooksPath, or the repository's hooks folder)
    would run for git there, and never runs for Chief of Staff's push (outside any sandbox)."""
    from shepherd_world import run

    runtime = await claimed(world)
    step, sha = await fixed(world, runtime)
    tree = step["tree"]
    marker = world.root / "hook-ran"
    husky = world.root / "husky"
    common = Path(
        run("git", "-C", tree, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    )
    for folder in (husky, common / "hooks"):
        folder.mkdir(exist_ok=True)
        for name in ("pre-push", "reference-transaction", "post-checkout", "post-merge"):
            hook = folder / name
            hook.write_text(f"#!/bin/sh\necho {name} >> '{marker}'\n")
            hook.chmod(0o755)
    run("git", "-C", tree, "config", "core.hooksPath", str(husky))
    run("git", "-C", tree, "hook", "run", "pre-push")
    assert marker.read_text() == "pre-push\n"  # It would run.
    marker.unlink()
    await approve(runtime.shepherd, step["id"], {})
    await settle(runtime)
    await settle(runtime)
    assert world.github.head() == sha
    assert runtime.shepherd.control.step(step["id"])["state"] == "published"
    assert not marker.exists()


async def test_a_tree_whose_git_file_the_step_rewrote_runs_nothing_of_it(world):
    """A step can write its tree's `.git` file: pointed at a repository of its own, whose
    config fetches a missing object through a program of its choosing, Chief of Staff's git
    (outside any sandbox) would run that program reading the step's commit. It reads the
    tree through its repository as recorded instead (0.1.46's rule), and pushes from there."""
    from shepherd_world import run

    runtime = await claimed(world)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    sha = world.commit_fix(step["tree"])
    marker = world.root / "ran-as-the-owner"
    planted = world.root / "planted"
    run("git", "init", "-q", str(planted), cwd=world.root)
    for key, value in (
        ("core.repositoryformatversion", "1"),
        ("extensions.partialclone", "origin"),
        ("remote.origin.url", world.remote.as_uri()),
        ("remote.origin.promisor", "true"),
        ("remote.origin.uploadpack", f"touch '{marker}'; git-upload-pack"),
    ):
        run("git", "--git-dir", str(planted / ".git"), "config", key, value)
    (planted / ".git" / "HEAD").write_text(sha + "\n")
    (Path(step["tree"]) / ".git").write_text(f"gitdir: {planted / '.git'}\n")
    answer = await step_result(
        runtime, world.worker, step=step["id"], outcome="fixed", summary="Fixed.", commit=sha
    )
    assert not marker.exists()
    assert "Recorded" in said(answer), said(answer)
    world.session().emit("session.idle")
    await settle(runtime)
    await settle(runtime)
    step = runtime.shepherd.control.step(step["id"])
    assert step["state"] == "prepared" and step["plan"]["h1"] == sha, step
    await approve(runtime.shepherd, step["id"], {})
    await settle(runtime)
    await settle(runtime)
    assert world.github.head() == sha
    assert not marker.exists()


@pytest.mark.parametrize("at_head", [True, False])
async def test_a_workers_own_clone_is_worked_in_only_at_the_head_never_made_into_a_worktree(
    world, at_head
):
    """A clone the worker made in its folder: its config is the worker's to write (a filter
    there would run, as the owner, for a worktree Chief of Staff made of it). The step runs in
    the clone itself while it is at the PR's head (read through Chief of Staff's own git folder
    for it); otherwise it does not run, and says why."""
    from shepherd_world import REPO, run

    runtime = await claimed(world)
    workspace = world.root / "work" / "fix-login"
    run("git", "worktree", "remove", "--force", str(workspace / "api"), cwd=world.clone)
    runtime.store.update_session(world.worker, repos="[]")
    clone = workspace / "own"
    run("git", "clone", "-q", "-b", BRANCH, str(world.remote), str(clone), cwd=world.root)
    run("git", "remote", "set-url", "origin", f"https://github.com/{REPO}.git", cwd=clone)
    marker = world.root / "ran-as-the-owner"
    run("git", "config", "filter.planted.smudge", f"touch '{marker}'; cat", cwd=clone)
    (clone / ".git" / "info" / "attributes").write_text("* filter=planted\n")
    if not at_head:
        run("git", "commit", "-q", "--allow-empty", "-m", "More", cwd=clone)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.steps(TARGET)[-1]
    assert not marker.exists()
    if not at_head:
        assert step["state"] == "failed", step
        assert "own clone of acme/api isn't at the PR's latest commit" in step["why"], step
        return
    assert step["state"] == "running" and step["tree"] == str(clone), step
    sha = world.commit_fix(str(clone))
    answer = await step_result(
        runtime, world.worker, step=step["id"], outcome="fixed", summary="Fixed.", commit=sha,
        test_command=TEST_COMMAND,
    )  # fmt: skip
    assert "not checked on frozen commits" in said(answer), said(answer)
    world.session().emit("session.idle")
    await settle(runtime)
    await settle(runtime)
    step = runtime.shepherd.control.step(step["id"])
    assert step["state"] == "prepared" and step["plan"]["h1"] == sha, step
    assert "return 30" in step["plan"]["diff"]
    assert not marker.exists()


async def test_off_has_no_tables_no_snapshot_and_no_hooks(harness, tmp_path):
    world = World(harness, tmp_path)
    world.build()
    runtime = harness.runtime
    runtime.github.raw = world.github
    runtime.github.gh = runtime.github.routed
    await harness.connect()
    session_id = await world.make_worker()
    assert not runtime.shepherd.control.exists()
    assert "shepherd" not in runtime.snapshot()
    assert "shepherd" not in runtime.task_actions.view(runtime.tasks.get(world.task))
    assert not await runtime.shepherd.signal(TARGET, ["github:ci:x:failed-a"], "x", None)
    await runtime.shepherd.tick()
    assert not runtime.shepherd.control.exists()
    assert "shepherd_step" not in runtime.view(runtime.store.session(session_id))
    router = runtime.store.session(runtime.router_id())
    options = runtime.options(router)
    assert "Prepared for you" not in options["system_message"]["content"]
    schemas = {tool.name: tool.parameters for tool in options["tools"]}
    assert "shepherd" not in schemas["spawn_worker"]["properties"]
    assert "shepherd" not in schemas["message_session"]["properties"]
    # Pausing and resuming tell the page nothing new; an Accept records nothing.
    sent = []
    publish = runtime.bus.publish
    runtime.bus.publish = lambda kind, data=None: (sent.append(kind), publish(kind, data))[1]
    for paused in (True, False):
        old = runtime.store.settings()
        new = old.model_copy(update={"paused": paused})
        runtime.store.save_settings(new)
        await runtime.settings_changed(old, new)
    assert runtime.shepherd.accepted(runtime.tasks.get(world.task)) == []
    assert "prepared" not in sent and not runtime.shepherd.control.exists()
    assert not runtime.store.items("acceptnote:") and not runtime.store.items("step:")


async def test_turned_on_the_router_gets_its_rule_and_field(world):
    runtime = await claimed(world)
    router = runtime.store.session(runtime.router_id())
    options = runtime.options(router)
    assert "Prepared for you is on" in options["system_message"]["content"]
    schemas = {tool.name: tool.parameters for tool in options["tools"]}
    assert "shepherd" in schemas["spawn_worker"]["properties"]
    assert "shepherd" in schemas["message_session"]["properties"]


async def test_a_secret_in_the_commit_is_refused_and_the_step_told_why(world):
    runtime = await claimed(world)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    tree = step["tree"]
    from shepherd_world import run

    (__import__("pathlib").Path(tree) / "config.py").write_text("TOKEN = 'ghp_" + "a" * 36 + "'\n")
    run("git", "add", "-A", cwd=tree)
    run("git", "commit", "-qm", "config", cwd=tree)
    sha = run("git", "rev-parse", "HEAD", cwd=tree)
    answer = await step_result(
        runtime,
        world.worker,
        step=step["id"],
        outcome="fixed",
        summary="x",
        commit=sha,
        test_command="pytest",
    )
    assert "a GitHub token" in said(answer) and "Refused" in said(answer)
    # Removed in a commit on top: still in the history a push would publish.
    (__import__("pathlib").Path(tree) / "config.py").write_text("TOKEN = ''\n")
    run("git", "commit", "-qam", "no token", cwd=tree)
    sha = run("git", "rev-parse", "HEAD", cwd=tree)
    answer = await step_result(
        runtime, world.worker, step=step["id"], outcome="fixed", summary="x", commit=sha,
        test_command="pytest",
    )  # fmt: skip
    assert "a GitHub token" in said(answer) and f"git reset --hard {world.h0[:12]}" in said(answer)
    assert not runtime.shepherd.control.step(step["id"])["h1"]


async def test_a_commit_not_on_the_head_is_refused(world):
    runtime = await claimed(world)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    answer = await step_result(
        runtime,
        world.worker,
        step=step["id"],
        outcome="fixed",
        summary="x",
        commit=world.h0,
        test_command="pytest",
    )
    assert "commit your change first" in said(answer)
    answer = await step_result(
        runtime, world.worker, step="s0000000000", outcome="fixed", summary="x", commit=world.h0
    )
    assert "No shepherd step of yours" in said(answer)


async def test_a_head_the_journal_does_not_explain_pauses_it(world):
    runtime = await claimed(world)
    claim = runtime.shepherd.control.claim(TARGET)
    runtime.shepherd.control.update_claim(TARGET, delivered=1)
    from shepherd_world import run

    seed = world.root / "seed"
    (seed / "notes.md").write_text("teammate")
    run("git", "add", "-A", cwd=seed)
    run("git", "commit", "-qm", "teammate", cwd=seed)
    run("git", "push", "-q", str(world.remote), BRANCH, cwd=seed)
    await runtime.shepherd.read(TARGET)
    claim = runtime.shepherd.control.claim(TARGET)
    head = world.github.head()
    assert claim["state"] == "paused" and head[:7] in claim["reason"]
    view = runtime.task_actions.view(runtime.tasks.get(world.task))["shepherd"]
    assert view["action"] == {"kind": "resume", "label": "Resume"}
    assert view["line"] == f"Someone else pushed {head[:7]}, so it's waiting for you."
    await runtime.shepherd.resume(TARGET)
    claim = runtime.shepherd.control.claim(TARGET)
    assert claim["state"] == "idle" and runtime.shepherd.control.explains(claim, head)


async def test_budget_zero_runs_nothing_unasked_and_fix_it_is_charged(world):
    runtime = await claimed(world)
    claim = runtime.shepherd.control.claim(TARGET)
    details = claim["details"]
    task = runtime.tasks.get(world.task)
    runtime.shepherd.control.update_claim(TARGET, delivered=1)
    [step] = await runtime.shepherd.signals(claim, task, details)
    await runtime.shepherd.pump()
    await settle(runtime)
    step = runtime.shepherd.control.step(step["id"])
    assert step["state"] == "queued" and step["why"] == "today's budget is used"
    view = runtime.task_actions.view(task)["shepherd"]
    assert view["action"]["label"] == "Fix it"
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.step(step["id"])
    assert step["state"] == "running" and step["clicked"] == 1 and step["level"] == "prepare"


async def test_your_message_stops_the_step_first(world):
    runtime = await claimed(world)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    stepped = world.session()
    await runtime.owner_send(world.worker, "What are you doing?")
    await settle(runtime)
    assert stepped.aborted
    assert runtime.shepherd.control.step(step["id"])["state"] == "interrupted"
    assert not runtime.shepherd.marked(world.worker)
    # Its message went to a session with its normal options.
    now = world.session()
    assert now is not stepped and "What are you doing?" in now.sent[-1]
    assert now.options.get("enable_config_discovery") is True


async def test_a_worker_left_in_a_step_is_not_continued_after_a_restart(world):
    runtime = await claimed(world)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    sent = len(world.session().sent)
    await runtime.resume_working()
    await settle(runtime)
    assert not runtime.shepherd.marked(world.worker)
    assert runtime.shepherd.control.step(step["id"])["state"] == "interrupted"
    assert len(world.session().sent) == sent  # No "continue where you left off".


async def test_closing_the_worker_gives_the_pr_to_you_and_reopening_gives_it_back_paused(world):
    runtime = await claimed(world)
    runtime.shepherd.control.accept(TARGET, world.h0, world.task)
    await runtime.workers.close(world.worker, by="you")
    await settle(runtime)
    claim = runtime.shepherd.control.claim(TARGET)
    assert (claim["holder_kind"], claim["state"], claim["reason"]) == (
        "owner",
        "paused",
        "its worker was closed",
    )
    assert claim["origin_task"] == world.task and claim["accepted"]["sha"] == world.h0
    await runtime.workers.reopen(world.worker, restore=True)
    await settle(runtime)
    claim = runtime.shepherd.control.claim(TARGET)
    assert (claim["holder"], claim["state"]) == (world.task, "paused")
    assert claim["accepted"]["sha"] == world.h0


@pytest.mark.parametrize("paused", [False, True])
async def test_accept_records_the_head_the_card_showed(world, paused):
    runtime = await claimed(world)
    if paused:
        old = runtime.store.settings()
        new = old.model_copy(update={"paused": True})
        runtime.store.save_settings(new)
        await runtime.settings_changed(old, new)
    runtime.task_actions.accept(world.task)
    claim = runtime.shepherd.control.claim(TARGET)
    assert claim["accepted"]["sha"] == world.h0 and claim["accepted"]["checks"] == ["ci / test"]


async def test_accept_of_a_head_that_is_not_what_was_reviewed_records_none(world):
    runtime = await claimed(world)
    run_id = runtime.store.value("SELECT id FROM runs WHERE task_id=?", (world.task,))
    evidence = json.loads(runtime.store.value("SELECT evidence FROM runs WHERE id=?", (run_id,)))
    evidence["checkouts"][0]["commit"] = "f" * 40
    runtime.store.execute("UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run_id))
    runtime.task_actions.accept(world.task)
    claim = runtime.shepherd.control.claim(TARGET)
    assert not claim["accepted"].get("sha")
    view = runtime.task_actions.view(runtime.tasks.get(world.task))["shepherd"]
    assert view["accept_note"] == (
        "Accepted, but the PR changed after what you reviewed: it won't merge by itself."
    )


def green(world, head):
    world.github.checks[head] = [
        {"name": "ci / test", "status": "COMPLETED", "conclusion": "SUCCESS", "required": True}
    ]
    world.github.reviews = [
        {
            "author": {"login": "maya"},
            "state": "APPROVED",
            "submittedAt": "2026-11-16T09:00:00Z",
            "commit": {"oid": head},
        }
    ]


async def test_merge_when_ready_merges_exactly_the_accepted_head(world):
    runtime = await claimed(world)
    runtime.task_actions.accept(world.task)
    world.github.base_rules = [{"type": "merge_queue"}]
    with pytest.raises(ValueError, match="merges through a queue"):
        await runtime.shepherd.merge_when_ready(TARGET, True)
    world.github.base_rules = []
    await runtime.shepherd.merge_when_ready(TARGET, True)
    await runtime.shepherd.read(TARGET)
    assert not world.github.merged  # CI failing, no approval.
    green(world, world.h0)
    await runtime.shepherd.read(TARGET)
    await settle(runtime)
    assert world.github.merged
    [merge] = [w for w in world.github.writes if w[0] == "merge"]
    assert merge[1][-2:] == ("--match-head-commit", world.h0)
    assert merge[2] == "tok-me"  # As the bound account.


@pytest.mark.parametrize(
    ("blocker", "words"),
    [
        ("draft", "waiting for it to be marked ready for review"),
        ("label", "waiting for its hold label to come off"),
        ("changes", "the requested changes to be addressed"),
        ("pending", "waiting for checks to finish"),
        ("not approved", "waiting for an approval of this commit"),
        ("state", "waiting for GitHub's merge rules to be met"),
        ("unreadable base", "its base branch's rules couldn't be read"),
    ],
)
async def test_merge_when_ready_waits_for_each_condition(world, blocker, words):
    """Not merged, and its row says what it waits for."""
    runtime = await claimed(world)
    runtime.task_actions.accept(world.task)
    await runtime.shepherd.merge_when_ready(TARGET, True)
    green(world, world.h0)
    github = world.github
    if blocker == "draft":
        github.draft = True
    elif blocker == "label":
        github.labels = ["do not merge"]
    elif blocker == "changes":
        github.review = "CHANGES_REQUESTED"
    elif blocker == "pending":
        github.checks[world.h0][0] |= {"status": "IN_PROGRESS", "conclusion": ""}
    elif blocker == "not approved":
        github.reviews = []
    elif blocker == "state":
        github.merge_state = "BLOCKED"
    else:
        github.base_protected = None
    await runtime.shepherd.read(TARGET)
    await settle(runtime)
    assert not github.merged
    line = runtime.task_actions.view(runtime.tasks.get(world.task))["shepherd"]["line"]
    assert line.startswith("Merges when ready: ") and words in line and line.endswith("."), line


async def test_a_teammates_push_after_accept_is_never_merged(world):
    runtime = await claimed(world)
    runtime.task_actions.accept(world.task)
    await runtime.shepherd.merge_when_ready(TARGET, True)
    from shepherd_world import run

    seed = world.root / "seed"
    (seed / "x.md").write_text("x")
    run("git", "add", "-A", cwd=seed)
    run("git", "commit", "-qm", "teammate", cwd=seed)
    run("git", "push", "-q", str(world.remote), BRANCH, cwd=seed)
    head = world.github.head()
    green(world, head)
    await runtime.shepherd.read(TARGET)
    await settle(runtime)
    assert not world.github.merged
    assert runtime.shepherd.control.claim(TARGET)["state"] == "paused"
    await runtime.shepherd.resume(TARGET)
    await runtime.shepherd.read(TARGET)
    await settle(runtime)
    assert not world.github.merged  # Resume is not an Accept.
    await runtime.shepherd.accept_head(TARGET, head)
    await runtime.shepherd.read(TARGET)
    await settle(runtime)
    assert world.github.merged


async def test_an_approved_fix_after_accept_merges_through_the_chain(world):
    runtime = await claimed(world)
    runtime.task_actions.accept(world.task)
    await runtime.shepherd.merge_when_ready(TARGET, True)
    step, sha = await fixed(world, runtime)
    await approve(runtime.shepherd, step["id"], {})
    await settle(runtime)
    await settle(runtime)
    assert world.github.head() == sha
    green(world, sha)
    await runtime.shepherd.read(TARGET)
    await settle(runtime)
    assert world.github.merged
    assert next(w for w in world.github.writes if w[0] == "merge")[1][-1] == sha


async def test_turned_off_mid_attempt_it_drains(world):
    runtime = await claimed(world)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    old = runtime.store.settings()
    new = old.model_copy(update={"proactive": False})
    runtime.store.save_settings(new)
    await runtime.settings_changed(old, new)
    await settle(runtime)
    assert not runtime.shepherd.marked(world.worker)
    assert runtime.shepherd.control.step(step["id"])["state"] == "withdrawn"
    assert "shepherd" not in runtime.task_actions.view(runtime.tasks.get(world.task))


def comments(world, *, author="maya", body="Please rename `timeout` to `login_timeout`."):
    world.github.review = "CHANGES_REQUESTED"
    world.github.threads = [
        {
            "id": "PRRT_1", "isResolved": False, "isOutdated": False, "path": "app.py", "line": 1,
            "comments": {"nodes": [{"databaseId": 501, "author": {"login": author}, "body": body,
                                    "createdAt": "2026-11-16T09:00:00Z", "url": "https://x/501"}]},
        },
        {
            "id": "PRRT_2", "isResolved": False, "isOutdated": False, "path": "app.py", "line": 2,
            "comments": {"nodes": [{"databaseId": 502, "author": {"login": "maya"},
                                    "body": "Why 30 and not 60?",
                                    "createdAt": "2026-11-16T09:01:00Z",
                                    "url": "https://x/502"}]},
        },
    ]  # fmt: skip
    world.github.reviews = [{"author": {"login": "maya"}, "state": "CHANGES_REQUESTED",
                             "submittedAt": "2026-11-16T09:02:00Z",
                             "commit": {"oid": world.h0}}]  # fmt: skip
    world.github.default_checks = [
        {"name": "ci / test", "status": "COMPLETED", "conclusion": "SUCCESS", "required": True}
    ]


async def addressed(world, runtime, stances):
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    assert step["kind"] == "address-review" and step["state"] == "running", step
    folder = __import__("pathlib").Path(step["folder"])
    asked = json.loads((folder / "threads.json").read_text())
    assert [(a["comment"], a["author"], a["writer"]) for a in asked] == [
        (501, "maya", True),
        (502, "maya", True),
    ]
    sha = world.commit_fix(step["tree"])
    await step_result(
        runtime, world.worker, step=step["id"], outcome="addressed", summary="Renamed it.",
        commit=sha, test_command="", threads=stances,
    )  # fmt: skip
    world.session().emit("session.idle")
    await settle(runtime)
    await settle(runtime)
    return runtime.shepherd.control.step(step["id"]), sha


STANCES = [
    {"comment": 501, "stance": "fix", "reply": "Done: renamed it."},
    {"comment": 502, "stance": "push-back", "reply": "30 matches the gateway's timeout."},
]


async def test_review_comments_one_plan_with_a_fix_replies_and_a_push_back(world):
    runtime = await world.start()
    comments(world)
    await runtime.shepherd.tick()
    step, sha = await addressed(world, runtime, STANCES)
    assert step["state"] == "prepared", step
    plan = step["plan"]
    assert plan["kind"] == "fix" and plan["h1"] == sha and plan["reviewers"] == ["maya"]
    assert [(t["comment"], t["stance"]) for t in plan["threads"]] == [
        (501, "fix"),
        (502, "push-back"),
    ]
    view = runtime.task_actions.view(runtime.tasks.get(world.task))["shepherd"]
    assert view["action"]["label"] == "Review & post"
    # Nothing posts until the owner decides the repository's disclosure line.
    with pytest.raises(ValueError, match="Answer first"):
        await approve(runtime.shepherd, step["id"], {})
    assert runtime.shepherd.control.step(step["id"])["state"] == "prepared"
    edited = "30 matches the gateway's timeout, so a longer one never helps."
    await approve(
        runtime.shepherd, step["id"],
        {"disclosure": "no", "replies": {"502": edited}, "resolve": ["501"]},
    )  # fmt: skip
    await settle(runtime)
    await settle(runtime)
    final = runtime.shepherd.control.step(step["id"])
    assert final["state"] == "published", runtime.shepherd.control.actions(step["id"])
    kinds = [action["kind"] for action in runtime.shepherd.control.actions(step["id"])]
    assert kinds == ["push", "reply", "reply", "resolve", "request-review"]
    writes = world.github.writes
    replies = [w[3] for w in writes if w[0] == "reply"]
    assert replies == ["Done: renamed it.", edited]  # The owner's edit, no disclosure line.
    assert all(w[2] == "tok-me" for w in writes if w[0] in {"reply", "request-review"})
    assert [t["isResolved"] for t in world.github.threads] == [True, False]
    assert runtime.store.settings().proactive_repos["acme/api"].disclosure == "no"
    learned = runtime.store.all("SELECT kind, owner FROM feedback ORDER BY id")
    assert ("draft-edited", edited) in [(row["kind"], row["owner"]) for row in learned]


async def test_disclosure_yes_adds_its_line(world):
    runtime = await world.start()
    comments(world)
    await runtime.shepherd.tick()
    step, _ = await addressed(world, runtime, STANCES[:1])
    await approve(runtime.shepherd, step["id"], {"disclosure": "yes"})
    await settle(runtime)
    await settle(runtime)
    [reply] = [w[3] for w in world.github.writes if w[0] == "reply"]
    assert reply.startswith("Done: renamed it.") and "AI assistant" in reply


async def test_do_it_their_way_prepares_a_new_plan(world):
    runtime = await world.start()
    comments(world)
    await runtime.shepherd.tick()
    step, _ = await addressed(world, runtime, STANCES)
    redone = await approve(runtime.shepherd, step["id"], {"choice": {"502": "their-way"}})
    await settle(runtime)
    assert runtime.shepherd.control.step(step["id"])["state"] == "dismissed"
    assert redone["signal"]["their_way"] == ["502"] and redone["clicked"] == 1
    assert not world.github.writes
    # Its worker is told which comments to do the reviewer's way.
    assert runtime.shepherd.control.step(redone["id"])["state"] == "running"
    sent = world.session().sent[-1]
    assert f'step="{redone["id"]}"' in sent
    assert "The owner chose the reviewer's way on comments 502" in sent


async def test_a_comment_by_someone_without_write_permission_is_listed_not_addressed(world):
    runtime = await world.start()
    comments(world, author="drive-by")
    world.github.review = ""
    world.github.reviews = []
    world.github.threads = world.github.threads[:1]
    await runtime.shepherd.tick()
    claim = runtime.shepherd.control.claim(TARGET)
    asks = await runtime.shepherd.review_asks(claim["details"])
    assert asks == []  # Neither a writer's thread nor changes requested: nothing to address.


async def test_a_question_only_comment_gets_an_answer_and_no_code(world):
    runtime = await world.start()
    comments(world)
    await runtime.shepherd.tick()
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    await step_result(
        runtime, world.worker, step=step["id"], outcome="addressed", summary="Answered.",
        threads=[{"comment": 502, "stance": "answer", "reply": "It matches the gateway."}],
    )  # fmt: skip
    world.session().emit("session.idle")
    await settle(runtime)
    await settle(runtime)
    plan = runtime.shepherd.control.step(step["id"])["plan"]
    assert plan["kind"] == "replies" and not plan["h1"]
    view = runtime.task_actions.view(runtime.tasks.get(world.task))["shepherd"]
    assert view["action"]["label"] == "Review & post"


async def test_an_infrastructure_failure_gets_a_rerun_plan_no_attempt(world):
    runtime = await world.start()
    world.github.log = "The runner has received a shutdown signal.\n"
    await runtime.shepherd.tick()
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    [step] = runtime.shepherd.control.steps(TARGET)
    assert step["state"] == "prepared" and step["plan"]["kind"] == "rerun"
    assert not world.session().sent or "shepherd-step" not in world.session().sent[-1]
    view = runtime.task_actions.view(runtime.tasks.get(world.task))["shepherd"]
    assert view["action"]["label"] == "Run it again"
    await approve(runtime.shepherd, step["id"], {})
    await settle(runtime)
    [rerun] = [w for w in world.github.writes if w[0] == "rerun"]
    assert rerun[1] == ("run", "rerun", "501", "--failed", "--repo", "acme/api")
    # Once per head: no second rerun plan on it.
    with pytest.raises(ValueError, match="tried already on this commit"):
        await runtime.shepherd.fix_it(TARGET)
