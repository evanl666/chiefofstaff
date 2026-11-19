"""Prepared for you, S1's gate (tests/shepherd_world.py): the news that starts an attempt, what
it may spend, what stops it, where it can't run, what pauses a PR, a push the lease refuses,
what a crash leaves, the Router's handing over, and the owner's other PRs found and handed."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import call_tool, settle
from shepherd_world import BRANCH, REPO, TARGET, World, run
from test_shepherd import claimed, fixed, said, step_result

from chiefofstaff import publish, workspaces
from chiefofstaff.publish import approve, reconcile_all
from chiefofstaff.shepherd import MARGIN, PERMITTED, PROBE_RETRY_SECONDS

# What an unasked attempt reserves of the day's budget: what it may spend, and the margin.
RESERVE = PERMITTED["fix-ci"] + MARGIN


@pytest.fixture
def world(harness, tmp_path):
    return World(harness, tmp_path)


def teammate_pushes(world) -> str:
    seed = world.root / "seed"
    (seed / "notes.md").write_text("teammate")
    run("git", "add", "-A", cwd=seed)
    run("git", "commit", "-qm", "teammate", cwd=seed)
    run("git", "push", "-q", str(world.remote), BRANCH, cwd=seed)
    return world.github.head()


# The news that starts an attempt.


async def test_ci_news_read_by_the_poller_starts_an_attempt_and_rides_along(world):
    """A followed PR's failing CI, read as today: held from the worker for the attempt (no turn
    of its own), carried in the attempt's message, and the Router told in a line."""
    runtime = await claimed(world, budget=RESERVE, levels={"fix-ci": "prepare"})
    runtime.shepherd.control.update_claim(TARGET, delivered=1)
    assert await runtime.github.read_followed(TARGET, 0)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    assert step and step["kind"] == "fix-ci" and step["state"] == "running", step
    assert step["level"] == "prepare" and not step["clicked"]
    assert "CI on" in step["signal"]["news"]["text"]
    sent = world.session().sent[-1]
    assert f'step="{step["id"]}"' in sent and "failed: ci / test" in sent
    held = [
        message
        for message in runtime.store.messages(world.worker)
        if message["kind"] == "inbound" and message["meta"].get("source") == "github"
    ]
    assert len(held) == 1 and held[0]["meta"]["deferred"]
    notices = [m["text"] for m in runtime.store.messages(runtime.router_id())]
    assert any("(a fix is being prepared)" in text for text in notices), notices


async def test_news_of_a_failure_its_row_offers_fix_it_for_is_held_too(world):
    """Seen live (SU2): the round's read had queued the failure's attempt (its row offering Fix
    it); the poller's news of it then went to the worker as a turn of its own, which fixed the
    PR and pushed, and the PR paused on its commit. That news is the attempt's: held too."""
    runtime = await claimed(world, levels={"fix-ci": "suggest"})
    runtime.shepherd.control.update_claim(TARGET, delivered=1)
    await runtime.shepherd.read(TARGET)
    step = runtime.shepherd.control.open_step(TARGET)
    assert step and step["state"] == "queued", step  # Fix it, on its row.
    turns = len(world.session().sent)
    assert await runtime.github.read_followed(TARGET, 0)
    await settle(runtime)
    held = [
        message
        for message in runtime.store.messages(world.worker)
        if message["kind"] == "inbound" and message["meta"].get("source") == "github"
    ]
    assert len(held) == 1 and held[0]["meta"]["deferred"], held
    assert len(world.session().sent) == turns, "no turn of the worker's for it"


async def test_off_the_same_news_reaches_the_worker_as_today(harness, tmp_path):
    world = World(harness, tmp_path)
    world.build()
    runtime = harness.runtime
    runtime.github.raw = world.github
    runtime.github.gh = runtime.github.routed
    await harness.connect()
    world.worker = await world.make_worker()
    runtime.store.update_session(world.worker, status="idle")
    assert await runtime.github.read_followed(TARGET, 0)
    await settle(runtime)
    assert not runtime.shepherd.control.exists()
    inbound = [m for m in runtime.store.messages(world.worker) if m["kind"] == "inbound"]
    assert len(inbound) == 1 and not inbound[0]["meta"].get("deferred")
    assert "failed: ci / test" in world.session().sent[-1]


async def test_kind_off_and_keep_off_deliver_news_as_today(world):
    runtime = await claimed(world, budget=RESERVE, levels={"fix-ci": "off"})
    runtime.shepherd.control.update_claim(TARGET, delivered=1)
    key = f"github:ci:{REPO}#12@{world.h0}:failed-ci / test"
    assert not await runtime.shepherd.signal(TARGET, [key], "CI failed", None)
    await runtime.shepherd.keep(TARGET, False)
    old = runtime.store.settings()
    new = old.model_copy(update={"proactive_levels": {"fix-ci": "prepare"}})
    runtime.store.save_settings(new)
    assert not await runtime.shepherd.signal(TARGET, [key], "CI failed", None)
    assert runtime.shepherd.control.open_step(TARGET) is None


# What an attempt may spend, and what it spent.


async def test_an_attempt_is_interrupted_at_its_permitted_spend(world):
    """As measured live (claude-sonnet-5.5): resuming the worker's session in the step profile
    costs a prompt-cache write over its context first (4 to 6 credits), then about one credit a
    call; a fix takes 15 to 20 calls, and runs to its end. Past what it may spend, it stops."""
    runtime = await claimed(world)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    assert step["state"] == "running" and step["permitted"] == PERMITTED["fix-ci"]
    session = world.session()
    for spent in [6.0, *[1.0] * 18]:
        usage = SimpleNamespace(total_nano_aiu=spent * 1e9)
        session.emit("assistant.usage", model="m", copilot_usage=usage)
    await settle(runtime)
    assert runtime.shepherd.control.step(step["id"])["state"] == "running"
    usage = SimpleNamespace(total_nano_aiu=(PERMITTED["fix-ci"] - 24.0) * 1e9)
    session.emit("assistant.usage", model="m", copilot_usage=usage)
    await settle(runtime)
    ended = runtime.shepherd.control.step(step["id"])
    assert ended["state"] == "interrupted" and "credits it may spend" in ended["why"]
    assert session.aborted and not runtime.shepherd.marked(world.worker)


async def test_what_it_spent_is_settled_from_its_log_each_record_once(world):
    runtime = await claimed(world)
    step, _ = await fixed(world, runtime)
    copilot = runtime.copilot_id(world.worker)
    record = {
        "type": "session.usage_record",
        "id": "u1",
        "timestamp": step["started_at"],
        "data": {"usage": {"copilotUsage": {"totalNanoAiu": 3_500_000_000}}},
    }
    old = {**record, "id": "u0", "timestamp": "2020-01-01T00:00:00+00:00"}
    harness_client = runtime.backend
    harness_client.logs[copilot] = [old, record, record]
    await runtime.shepherd.settle(step["id"])
    await runtime.shepherd.settle(step["id"])
    assert runtime.shepherd.control.step(step["id"])["settled"] == pytest.approx(3.5)
    assert runtime.shepherd.control.spent_today()["clicked"] == pytest.approx(3.5)


async def test_spend_past_the_limit_and_margin_raises_the_margin(world):
    runtime = await claimed(world)
    step, _ = await fixed(world, runtime)
    record = {
        "type": "session.usage_record",
        "id": "u1",
        "timestamp": step["started_at"],
        "data": {"usage": {"copilotUsage": {"totalNanoAiu": (PERMITTED["fix-ci"] + 5) * 1e9}}},
    }
    runtime.backend.logs[runtime.copilot_id(world.worker)] = [record]
    await runtime.shepherd.settle(step["id"])
    assert runtime.store.get("proactive:margin") == "5.00"
    assert runtime.shepherd.margin() == 5.0


@pytest.mark.parametrize(("budget", "state"), [(RESERVE - 1, "queued"), (RESERVE, "running")])
async def test_an_unclicked_attempt_runs_only_with_room_for_its_reserve(world, budget, state):
    """Its reserve is what it may spend and the margin: a budget just short of it leaves it
    waiting for a click (its row offers Fix it), one with room for it runs it."""
    runtime = await claimed(world, budget=budget, levels={"fix-ci": "prepare"})
    control = runtime.shepherd.control
    claim = control.claim(TARGET)
    task = runtime.tasks.get(world.task)
    control.update_claim(TARGET, delivered=1)
    [step] = await runtime.shepherd.signals(claim, task, claim["details"])
    await runtime.shepherd.pump()
    await settle(runtime)
    step = control.step(step["id"])
    assert step["state"] == state
    if state == "queued":
        assert step["why"] == "today's budget is used"
        view = runtime.task_actions.view(task)["shepherd"]
        assert view["action"]["label"] == "Fix it"
        assert not runtime.shepherd.marked(world.worker)
    else:
        assert step["reserved"] == RESERVE and control.spent_today()["reserved"] == RESERVE


# What stops an attempt.


async def test_the_owners_stop_ends_the_attempt_and_its_turn(world):
    runtime = await claimed(world)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    assert step["state"] == "running"
    await runtime.workers.interrupt(world.worker, by="you")
    await settle(runtime)
    ended = runtime.shepherd.control.step(step["id"])
    assert ended["state"] == "interrupted" and ended["why"] == "Stopped by you."
    assert world.session().aborted and not runtime.shepherd.marked(world.worker)
    assert runtime.store.session(world.worker)["status"] == "idle"


async def test_the_owners_queued_work_preempts_an_attempt(world):
    runtime = await claimed(world, max_workers=1)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    assert step["state"] == "running"
    await call_tool(
        runtime, "spawn_worker", runtime.router_id(), title="Another", instructions="Do it."
    )
    await settle(runtime)
    ended = runtime.shepherd.control.step(step["id"])
    assert ended["state"] == "interrupted", ended
    assert not runtime.shepherd.stepping


async def test_work_waiting_for_another_task_does_not_hold_an_attempt_back(world):
    """Queued behind another task still at work, it waits for that, not for a slot: an attempt
    may run meanwhile, and is not stopped for it."""
    from chiefofstaff.roles import WORKER

    runtime = await claimed(world, max_workers=2)
    router = runtime.router_id()
    first = json.loads(said(await call_tool(
        runtime, "spawn_worker", router, title="First", instructions="Do this first.",
    )))  # fmt: skip
    await call_tool(
        runtime, "spawn_worker", router, title="After it", instructions="Then this.",
        after=[first["task_id"]],
    )  # fmt: skip
    await settle(runtime)
    queued = [row for row in runtime.store.open_sessions(WORKER) if row["status"] == "queued"]
    assert len(queued) == 1 and runtime.workers.waiting_for(queued[0])
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    assert step["state"] == "running"
    await runtime.workers.pump()
    await settle(runtime)
    assert runtime.shepherd.control.step(step["id"])["state"] == "running"


async def test_an_attempt_waits_for_a_busy_worker(world):
    runtime = await claimed(world)
    runtime.store.update_session(world.worker, status="working")
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    assert step["state"] == "queued" and not runtime.shepherd.marked(world.worker)
    runtime.store.update_session(world.worker, status="idle")
    await runtime.shepherd.pump()
    await settle(runtime)
    assert runtime.shepherd.control.step(step["id"])["state"] == "running"


# Where it can't run.


async def test_on_a_codex_worker_it_is_refused_in_the_rows_words(world, monkeypatch):
    runtime = await claimed(world)
    monkeypatch.setattr(runtime, "agent", lambda session_id: "codex")
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    [step] = runtime.shepherd.control.steps(TARGET)
    assert step["state"] == "refused"
    view = runtime.task_actions.view(runtime.tasks.get(world.task))["shepherd"]
    assert view["line"] == "Can't prepare fixes here: its worker runs on Codex, not Copilot."
    assert view["refused"] and view["action"] is None


async def test_without_a_sandbox_it_is_refused(world):
    runtime = await claimed(world)
    runtime.sandbox_host = None
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    [step] = runtime.shepherd.control.steps(TARGET)
    assert step["state"] == "refused" and step["why"] == "this computer has no sandbox for it"
    assert "no sandbox" in runtime.shepherd.snapshot()["steps_here"]


async def test_until_its_safety_check_passes_an_attempt_waits(world, monkeypatch):
    from chiefofstaff.backend import TESTED_CLI

    runtime = await claimed(world)
    runtime.store.delete(f"stepprobe:{TESTED_CLI}")
    probed = []

    async def probe(runtime):
        probed.append(True)
        passed = len(probed) > 1
        return {"passed": passed, "why": "" if passed else "its shell reached GitHub",
                "at": "now", "checked": time.time()}  # fmt: skip

    monkeypatch.setattr("chiefofstaff.step_probe.probe", probe)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    assert probed and step is not None
    await runtime.shepherd.pump()
    await settle(runtime)
    [step] = runtime.shepherd.control.steps(TARGET)
    assert step["state"] == "refused"
    assert step["why"] == f"Copilot {TESTED_CLI} failed the safety check: its shell reached GitHub"
    # Checked again only once its failure is PROBE_RETRY_SECONDS old: then it passes.
    assert await runtime.shepherd.probe_refusal() == step["why"] and len(probed) == 1
    key = f"stepprobe:{TESTED_CLI}"
    old = json.loads(runtime.store.get(key))
    runtime.store.set(key, json.dumps({**old, "checked": time.time() - PROBE_RETRY_SECONDS}))
    assert await runtime.shepherd.probe_refusal() == f"Copilot {TESTED_CLI} is being checked first"
    await settle(runtime)
    assert len(probed) == 2 and await runtime.shepherd.probe_refusal() == ""


# What pauses a PR, and what stops trying.


async def test_a_pr_that_became_stacked_or_a_fork_pauses(world):
    runtime = await claimed(world)
    runtime.shepherd.control.update_claim(TARGET, delivered=1)
    world.github.stacked = 1
    await runtime.shepherd.read(TARGET)
    claim = runtime.shepherd.control.claim(TARGET)
    assert claim["state"] == "paused" and claim["reason"].startswith("stacked")


async def test_a_check_gone_after_its_push_pauses(world):
    runtime = await claimed(world)
    step, sha = await fixed(world, runtime)
    world.github.checks[sha] = [
        {"name": "lint", "status": "IN_PROGRESS", "conclusion": "", "required": False}
    ]
    await approve(runtime.shepherd, step["id"], {})
    await settle(runtime)
    await settle(runtime)
    assert world.github.head() == sha
    assert runtime.shepherd.control.claim(TARGET)["state"] == "idle"  # Read while CI runs.
    world.github.checks[sha] = [
        {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS", "required": False}
    ]
    await runtime.shepherd.read(TARGET)
    claim = runtime.shepherd.control.claim(TARGET)
    assert claim["state"] == "paused"
    assert claim["reason"] == "a check vanished after the push: ci / test"


async def test_the_same_failure_after_its_fix_stops_trying(world):
    runtime = await claimed(world)
    step, _ = await fixed(world, runtime)
    await approve(runtime.shepherd, step["id"], {})
    await settle(runtime)
    await settle(runtime)
    # CI fails again on the pushed head, the same way.
    claim = runtime.shepherd.control.claim(TARGET)
    task = runtime.tasks.get(world.task)
    details = await runtime.github.pr_details(REPO, 12)
    queued = await runtime.shepherd.signals(claim, task, details)
    assert queued == []
    view = runtime.task_actions.view(task)["shepherd"]
    assert view["line"] == "The same failure came back after its fix."
    assert view["action"] == {"kind": "look", "label": "Look"}


# A push the lease refuses; what a crash leaves.


async def test_a_teammates_push_between_the_check_and_the_push_is_kept(world, monkeypatch):
    """The head check passed, then a teammate pushed: the leased push is refused, their commit
    stays, and the PR pauses at its next read."""
    runtime = await claimed(world)
    step, sha = await fixed(world, runtime)
    checked = publish.head_checked

    async def raced(shepherd, operation):
        details = await checked(shepherd, operation)
        teammate_pushes(world)
        return details

    monkeypatch.setattr(publish, "head_checked", raced)
    await approve(runtime.shepherd, step["id"], {})
    await settle(runtime)
    await settle(runtime)
    theirs = world.github.head()
    assert theirs != sha
    [push] = runtime.shepherd.control.actions(step["id"])
    assert push["state"] == "failed", push
    assert runtime.shepherd.control.step(step["id"])["state"] == "failed"
    await runtime.shepherd.read(TARGET)
    claim = runtime.shepherd.control.claim(TARGET)
    assert claim["state"] == "paused" and theirs[:7] in claim["reason"]


async def test_the_push_carries_no_credentials_of_the_owners_and_skips_hooks(world, monkeypatch):
    runtime = await claimed(world)
    step, sha = await fixed(world, runtime)
    seen = {}
    sent = workspaces.command

    async def command(*argv, **options):
        if "push" in argv:
            seen.update(argv=argv, env=options.get("env") or {})
        return await sent(*argv, **options)

    monkeypatch.setattr(workspaces, "command", command)
    await approve(runtime.shepherd, step["id"], {})
    await settle(runtime)
    await settle(runtime)
    argv, env = seen["argv"], seen["env"]
    assert "--no-verify" in argv
    assert f"--force-with-lease=refs/heads/{BRANCH}:{world.h0}" in argv
    assert argv[-1] == f"{sha}:refs/heads/{BRANCH}"
    pairs = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert pairs["credential.helper"] == ""
    assert "tok-me" not in " ".join(argv)
    # From the repository as recorded, not wherever the tree's .git points now.
    assert env["GIT_COMMON_DIR"] == str(world.clone / ".git")


@pytest.mark.parametrize("went_out", ["yes", "no", "under", "unreadable"])
async def test_after_a_crash_a_push_is_read_on_github_never_sent_again(
    world, monkeypatch, went_out
):
    """Journaled as dispatching, then the app stopped: at the next start, the branch read on
    GitHub tells whether it went out (published) or not (failed, never pushed blindly); with a
    teammate's commit on top of it, it went out; when GitHub can't say, it stays uncertain."""
    runtime = await claimed(world)
    step, sha = await fixed(world, runtime)
    control = runtime.shepherd.control

    async def stopped(shepherd, step_id):
        pass

    monkeypatch.setattr(publish, "dispatch", stopped)
    await approve(runtime.shepherd, step["id"], {})
    [push] = control.actions(step["id"])
    claim = control.claim(TARGET)
    assert control.dispatching(push["id"], claim["holder"], claim["generation"], 0) == ""
    if went_out != "no":
        run("git", "push", "-q", str(world.remote), f"{sha}:refs/heads/{BRANCH}", cwd=world.clone)
    if went_out in {"under", "unreadable"}:
        run("git", "pull", "-q", str(world.remote), BRANCH, cwd=world.root / "seed")
        teammate_pushes(world)
    if went_out == "unreadable":
        answer = world.github.__call__

        async def flaky(*arguments, **options):
            if "/compare/" in " ".join(arguments):
                raise RuntimeError("GitHub request failed: HTTP 502")
            return await answer(*arguments, **options)

        runtime.github.raw = flaky
    head = world.github.head()
    before = len(world.github.calls)
    await reconcile_all(runtime.shepherd)
    [push] = control.actions(step["id"])
    state = {"yes": "sent", "no": "failed", "under": "sent", "unreadable": "uncertain"}[went_out]
    assert push["state"] == state
    assert control.step(step["id"])["state"] == ("published" if state == "sent" else "failed")
    assert world.github.head() == (world.h0 if went_out == "no" else head)
    assert not any("push" in call[0] for call in world.github.calls[before:])
    if state == "sent":
        assert control.explains(control.claim(TARGET), sha)


# The Router hands a PR over; the owner's other PRs found and handed.


async def test_the_router_hands_a_pr_over_with_spawn_workers_field(world):
    from copilot.tools import ToolInvocation

    runtime = await world.start(worker=False)
    router = runtime.router_id()
    tools = {tool.name: tool for tool in runtime.options(runtime.store.session(router))["tools"]}
    answer = await tools["spawn_worker"].handler(
        ToolInvocation(
            session_id=router,
            tool_call_id="t1",
            tool_name="spawn_worker",
            arguments={
                "title": "Keep api#12 moving",
                "instructions": "Keep acme/api#12 moving.",
                "shepherd": "acme/api#12",
            },
        )
    )
    text = said(answer)
    assert "Kept moving" in text, text
    claim = runtime.shepherd.control.claim(TARGET)
    task = runtime.tasks.get(claim["holder"])
    assert task["title"] == "Keep api#12 moving" and claim["holder_kind"] == "task"
    assert runtime.store.get(f"clickfirst:{TARGET}") == "1"
    assert TARGET in runtime.store.follows(task["session_id"])


async def test_a_pr_already_kept_by_another_task_is_not_taken(world):
    runtime = await claimed(world)
    other = runtime.tasks.create(world.worker, "Something else", "Do it.")
    assert await runtime.shepherd.take("acme/api#12", other["id"]) == (
        "Another task keeps it moving already."
    )
    assert runtime.shepherd.control.claim(TARGET)["holder"] == world.task


async def test_the_owners_other_prs_are_found_and_one_handed_to_a_worker(world):
    from chiefofstaff import discovery

    runtime = await world.start(worker=False, proactive_my_prs=True, github_repos=[REPO])
    world.github.mine = [
        {
            "repository": {"nameWithOwner": REPO},
            "number": 12,
            "title": "Fix the login timeout",
            "url": f"https://github.com/{REPO}/pull/12",
            "isDraft": False,
            "updatedAt": "2026-11-16T08:00:00Z",
            "createdAt": "2026-11-15T08:00:00Z",
        }
    ]
    await discovery.discover(runtime.shepherd, force=True)
    view = discovery.rows_view(runtime.shepherd)
    [row] = view["rows"]
    assert row["target"] == TARGET and row["kind"] == "fix-ci"
    assert row["what"].startswith("CI failing since") and row["label"] == "api#12"
    assert runtime.snapshot()["shepherd"]["prepared"]["rows"] == [row]
    started = await discovery.hand(runtime.shepherd, TARGET)
    task = runtime.tasks.get(started["task_id"])
    assert task["title"] == f"Keep {REPO}#12 moving"
    assert "gh pr checkout 12" in task["goal"] and "don't change" in task["goal"]
    claim = runtime.shepherd.control.claim(TARGET)
    assert claim["holder"] == task["id"] and runtime.store.get(f"clickfirst:{TARGET}") == "1"
    assert discovery.rows_view(runtime.shepherd)["rows"] == []
    with pytest.raises(ValueError, match="A worker has it already"):
        await discovery.hand(runtime.shepherd, TARGET)


async def test_a_fork_or_draft_is_listed_with_why_not(world):
    from chiefofstaff import discovery

    runtime = await world.start(worker=False, proactive_my_prs=True, github_repos=[REPO])
    world.github.draft = True
    world.github.mine = [
        {"repository": {"nameWithOwner": REPO}, "number": 12, "title": "WIP",
         "url": f"https://github.com/{REPO}/pull/12", "isDraft": True,
         "updatedAt": "2026-11-16T08:00:00Z", "createdAt": "2026-11-15T08:00:00Z"}
    ]  # fmt: skip
    await discovery.discover(runtime.shepherd, force=True)
    view = discovery.rows_view(runtime.shepherd)
    assert view["rows"] == [] and [item["why"] for item in view["all"]] == ["a draft: not yet"]
    with pytest.raises(ValueError, match="Not this PR: a draft"):
        await discovery.hand(runtime.shepherd, TARGET)


# Its routes, as the page calls them.


@pytest.fixture
def served(tmp_path):
    from conftest import FakeClient, FakeSlackMcp, FakeTokens
    from fastapi.testclient import TestClient
    from test_web import TOKEN

    from chiefofstaff.runtime import Runtime
    from chiefofstaff.sources.slack import SlackSource
    from chiefofstaff.web import create_app

    client = FakeClient()

    def factory(store, bus):
        async def notifier(title, text):
            pass

        async def signed_in(fresh: bool = False) -> list[dict]:
            return []  # Not this computer's gh (its run would outlive the app's loop).

        runtime = Runtime(store, bus, backend_factory=lambda settings: client, notifier=notifier)
        runtime.slack = SlackSource(store, runtime, tokens=FakeTokens(""), mcp=FakeSlackMcp())
        runtime.github.signed_in = signed_in
        return runtime

    app = create_app(tmp_path / "data", TOKEN, 8765, runtime_factory=factory)
    with TestClient(app, base_url="http://127.0.0.1:8765") as http:
        http.runtime = app.state.runtime
        yield http


def test_off_its_routes_refuse_in_plain_words(served):
    from test_web import ORIGIN, sign_in

    from chiefofstaff.shepherd import OFF_WORDS

    sign_in(served)
    assert served.get("/api/shepherd/steps/s1").status_code == 404
    for path, body in (
        ("/api/shepherd/pr", {"target": TARGET, "action": "fix"}),
        ("/api/shepherd/pr", {"target": TARGET, "action": "hand"}),
        ("/api/shepherd/steps/s1", {"action": "approve"}),
        ("/api/shepherd/scan", {}),
    ):
        answer = served.post(path, json=body, headers=ORIGIN)
        assert answer.status_code == 400 and answer.json()["detail"] == OFF_WORDS, path
    wrong = served.post(
        "/api/shepherd/pr", json={"target": TARGET, "action": "push"}, headers=ORIGIN
    )
    assert wrong.status_code == 422
    assert "shepherd" not in served.get("/api/state").json()
    assert not served.runtime.shepherd.control.exists()


def test_a_fix_reviewed_and_approved_through_its_routes(served, tmp_path):
    from test_web import ORIGIN, sign_in

    sign_in(served)
    runtime = served.runtime
    runtime.sandbox_host = True  # Whatever machine the tests run on (a container has none).
    world = World(None, tmp_path / "world", runtime=runtime)
    (tmp_path / "world").mkdir()
    served.portal.call(world.start)
    served.portal.call(runtime.shepherd.tick)
    served.portal.call(settle, runtime)
    state = served.get("/api/state").json()
    assert state["shepherd"]["on"] is True
    step, sha = served.portal.call(fixed, world, runtime)
    task = next(t for t in served.get("/api/state").json()["tasks"] if t["id"] == world.task)
    assert task["shepherd"]["action"]["kind"] == "review"
    view = served.get(f"/api/shepherd/steps/{step['id']}").json()
    assert view["plan"]["kind"] == "fix" and view["plan"]["h1"] == sha
    assert "return 30" in view["plan"]["diff"]
    approved = served.post(
        f"/api/shepherd/steps/{step['id']}", json={"action": "approve"}, headers=ORIGIN
    )
    assert approved.status_code == 200, approved.text
    for _ in range(3):
        served.portal.call(settle, runtime)
    assert world.github.head() == sha
    again = served.post(
        f"/api/shepherd/steps/{step['id']}", json={"action": "approve"}, headers=ORIGIN
    )
    assert again.status_code == 400 and again.json()["detail"] == "This was decided already."
    kept = served.post(
        "/api/shepherd/pr", json={"target": TARGET, "action": "keep", "on": False}, headers=ORIGIN
    )
    assert kept.status_code == 200
    assert not runtime.shepherd.control.claim(TARGET)["keep"]


# The step profile's probe, on a stand-in for the sandbox.


@pytest.mark.parametrize(
    ("through", "why"),
    [
        (set(), ""),
        ({"git push over HTTPS"}, "git push over HTTPS got through"),
        ({"tell_session"}, "calling tell_session got through"),
        ({"no install"}, "an install from pypi.org did not work"),
        ({"no commit"}, "a commit in its worktree did not work"),
        ({"mcp tool"}, "the tools probe-user-ping got through"),
        ({"no listing"}, "listing its tools (no such method) got through"),
        ({"a socket to github.com's address (140.82.112.3):443"},
         "a socket to github.com's address (140.82.112.3):443 got through"),
        (
            {"unsandboxed"},
            "git push over HTTPS (unsandboxed), git push over SSH (unsandboxed), gh api -X POST "
            "(unsandboxed), curl -X POST to api.github.com (unsandboxed), a socket to "
            "github.com:443 (unsandboxed), a socket to github.com:22 (unsandboxed), reading gh's "
            "hosts.yml (unsandboxed), a socket to github.com's address (140.82.112.3):443 "
            "(unsandboxed) got through",
        ),
        # A command that could not start (its interpreter blocked) shows nothing either way,
        # even with the shell printing its whole line: tried again, never a false "got through".
        ({"crash: a socket to github.com:443"}, "a socket to github.com:443 could not be tried"),
    ],
)  # fmt: skip
async def test_the_probe_passes_only_when_every_forbidden_action_fails(
    harness, monkeypatch, through, why
):
    from chiefofstaff import step_probe
    from chiefofstaff.shepherd import BLOCKED_HOSTS, STEP_TOOLS

    monkeypatch.setattr(step_probe, "github_address", lambda: "140.82.112.3")
    await harness.connect()
    runtime = harness.runtime
    client = harness.client
    resumed = {}
    original = client.resume_session

    async def resume(session_id, **options):
        session = await original(session_id, **options)
        resumed.update(options)

        async def execute(request, **options):
            # As Copilot answers: a mapping, the sandbox said in its telemetry.
            what = request.arguments.get("description", request.name)
            if request.name == "tell_session":
                ok = "tell_session" in through
                return {
                    "textResultForLlm": "" if ok else "Tool 'tell_session' does not exist.",
                    "resultType": "success" if ok else "failure",
                }
            command = request.arguments.get("command", "")
            commands.append(command)
            allowed = (what == "an install from pypi.org" and "no install" not in through) or (
                what == "a commit in its worktree" and "no commit" not in through
            )
            reached = what in through or allowed
            applied = "unsandboxed" not in through
            if f"crash: {what}" in through:
                # As bash says when what it runs is killed: the command line, whole.
                text = f"/bin/bash: line 1: 78173 Abort trap: 6           {command}"
            else:
                text = f"{step_probe.RAN}\n" + (
                    step_probe.REACHED if reached else "Operation not permitted"
                )
            return {
                "textResultForLlm": text,
                "resultType": "success",
                "toolTelemetry": {"properties": {"sandboxApplied": str(applied).lower()}},
            }

        async def validate(**options):
            pass

        async def metadata(**options):
            if "no listing" in through:
                raise RuntimeError("no such method")
            names = [*STEP_TOOLS, *(["probe-user-ping"] if "mcp tool" in through else [])]
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names])

        session.rpc.tools = SimpleNamespace(
            execute=execute, initialize_and_validate=validate, get_current_metadata=metadata
        )
        return session

    commands: list[str] = []
    client.resume_session = resume
    result = await step_probe.probe(runtime)
    assert result["passed"] == (not why) and result["why"] == why
    # Neither marker is ever in a command's own text.
    assert commands and not any(
        step_probe.REACHED in command or step_probe.RAN in command for command in commands
    )
    policy = resumed["sandbox_config"]
    assert policy.blocked_hosts == BLOCKED_HOSTS and not policy.credentials
    assert resumed["available_tools"] == list(STEP_TOOLS)
    assert [tool.name for tool in resumed["tools"]] == ["shepherd_result", "draft_github_review"]
    assert resumed["enable_config_discovery"] is False


# Who holds a PR when its worker, its task or its follow ends.


async def test_a_cancelled_task_gives_its_pr_to_the_owner_and_attempts_stop(world):
    runtime = await claimed(world)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    assert step["state"] == "running"
    # Its result came just now: the first cancel only asks; the PR stays its task's.
    with pytest.raises(ValueError, match="cancel again to set it aside"):
        await runtime.task_actions.cancel(world.task)
    assert runtime.shepherd.control.claim(TARGET)["holder_kind"] == "task"
    assert runtime.shepherd.control.step(step["id"])["state"] == "running"
    await runtime.task_actions.cancel(world.task)
    await settle(runtime)
    claim = runtime.shepherd.control.claim(TARGET)
    assert claim["holder_kind"] == "owner" and claim["reason"] == "its task was cancelled"
    ended = runtime.shepherd.control.step(step["id"])
    assert ended["state"] == "withdrawn" and ended["why"] == "its task was cancelled"
    assert not runtime.shepherd.marked(world.worker)
    key = f"github:ci:{REPO}#12@{world.h0}:failed-x"
    assert not await runtime.shepherd.signal(TARGET, [key], "CI failed", None)


async def test_a_worker_that_stops_following_its_pr_gives_it_to_the_owner(world):
    runtime = await claimed(world)
    assert runtime.unfollow_for(world.worker, TARGET)
    await settle(runtime)
    claim = runtime.shepherd.control.claim(TARGET)
    assert claim["holder_kind"] == "owner"
    assert claim["reason"] == "its worker stopped following it"


# What a step may draft: replies to its own PR's threads, kept for its plan.


async def test_a_steps_drafted_reply_is_kept_for_its_plan_and_nothing_else_is(world):
    from test_shepherd import comments

    runtime = await world.start()
    comments(world)
    await runtime.shepherd.tick()
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    assert step["kind"] == "address-review" and step["state"] == "running"
    worker = world.worker

    async def draft(**arguments):
        return said(await call_tool(runtime, "draft_github_review", worker, **arguments))

    kept = await draft(repo=REPO, number=12, in_reply_to=502, body="30 matches the gateway.")
    assert kept.startswith("Kept for the plan")
    for arguments, why in (
        ({"repo": "acme/web", "number": 12, "in_reply_to": 502}, "only on its own PR"),
        ({"repo": REPO, "number": 12, "in_reply_to": 999}, "only to the comments"),
        ({"repo": REPO, "number": 12, "in_reply_to": 502, "verdict": "approve"}, "no verdict"),
    ):
        answer = await draft(body="x", **arguments)
        assert why in answer, answer
    sha = world.commit_fix(step["tree"])
    await step_result(
        runtime, worker, step=step["id"], outcome="addressed", summary="Renamed it.",
        commit=sha, test_command="", threads=[
            {"comment": 501, "stance": "fix", "reply": "Done."},
            {"comment": 502, "stance": "push-back"},
        ],
    )  # fmt: skip
    world.session().emit("session.idle")
    await settle(runtime)
    await settle(runtime)
    plan = runtime.shepherd.control.step(step["id"])["plan"]
    assert [t["reply"] for t in plan["threads"]] == ["Done.", "30 matches the gateway."]
    assert not [w for w in world.github.writes if w[0] == "reply"]


# More of S1's scenarios, offline.


async def test_a_fix_that_skips_a_test_is_for_approval_its_hunks_first(world):
    """SU7: a skip marker in the fix: approval class, said on the row, its hunks first."""
    runtime = await claimed(world)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    tree = __import__("pathlib").Path(step["tree"])
    test = tree / "tests" / "test_app.py"
    test.write_text(
        test.read_text().replace(
            "def test_timeout():", "import pytest\n\n\n@pytest.mark.skip\ndef test_timeout():"
        )
    )
    (tree / "app.py").write_text((tree / "app.py").read_text() + "\n# tidy\n")
    run("git", "commit", "-qam", "Skip it", cwd=tree)
    sha = run("git", "rev-parse", "HEAD", cwd=tree)
    await step_result(
        runtime, world.worker, step=step["id"], outcome="fixed", summary="Skipped it.",
        commit=sha, test_command="",
    )  # fmt: skip
    world.session().emit("session.idle")
    await settle(runtime)
    await settle(runtime)
    plan = runtime.shepherd.control.step(step["id"])["plan"]
    assert plan["change"] == "approval"
    assert any(item["path"] == "tests/test_app.py" for item in plan["findings"])
    assert plan["diff"].index("tests/test_app.py") < plan["diff"].index("app.py\n")
    view = runtime.task_actions.view(runtime.tasks.get(world.task))["shepherd"]
    assert view["line"] == "A fix for the failing CI is ready: it changes tests or checks."


async def test_a_branch_that_requires_signed_commits_is_refused_with_why(world):
    """SY9: found at the push: nothing pushed; the next attempt is refused, saying why."""
    runtime = await claimed(world)
    step, _ = await fixed(world, runtime)
    world.github.rules = [{"type": "required_signatures"}]
    await approve(runtime.shepherd, step["id"], {})
    await settle(runtime)
    await settle(runtime)
    assert world.github.head() == world.h0
    [push] = runtime.shepherd.control.actions(step["id"])
    assert push["state"] == "failed" and push["outcome"] == "the branch requires signed commits", (
        push
    )
    view = runtime.task_actions.view(runtime.tasks.get(world.task))["shepherd"]
    assert view["line"] == "Not published: the branch requires signed commits."
    assert view["action"] == {"kind": "look", "label": "Look"}
    row = runtime.store.session(world.worker)
    assert await runtime.shepherd.executor_refusal(row, step) == (
        "the branch requires signed commits"
    )


async def test_writes_go_out_as_the_bound_account_or_not_at_all(world):
    """SY11: its account signed out of gh: nothing is pushed, and the journal says so."""
    runtime = await claimed(world)
    step, _ = await fixed(world, runtime)
    world.github.signed_out = True
    await approve(runtime.shepherd, step["id"], {})
    await settle(runtime)
    await settle(runtime)
    assert world.github.head() == world.h0
    [push] = runtime.shepherd.control.actions(step["id"])
    assert push["state"] == "failed" and "no oauth token" in push["outcome"]


async def test_a_reply_the_owner_posted_themselves_is_not_posted_again(world):
    """SW9 and SY3: a reply with the plan's text is on GitHub already (the owner posted it, or
    a send whose answer was lost): settled, never posted twice."""
    from test_shepherd import STANCES, addressed, comments

    runtime = await world.start()
    comments(world)
    await runtime.shepherd.tick()
    step, _ = await addressed(world, runtime, STANCES)
    world.github.posted.append(
        {"id": 6999, "body": "Done: renamed it.", "in_reply_to_id": 501,
         "html_url": "https://x/6999", "user": {"login": "me"}}
    )  # fmt: skip
    await approve(runtime.shepherd, step["id"], {"disclosure": "no"})
    await settle(runtime)
    await settle(runtime)
    replies = [w[3] for w in world.github.writes if w[0] == "reply"]
    assert replies == ["30 matches the gateway's timeout."]
    sent = {a["payload"]["text"]: a for a in runtime.shepherd.control.actions(step["id"])
            if a["kind"] == "reply"}  # fmt: skip
    assert sent["Done: renamed it."]["state"] == "sent"
    assert sent["Done: renamed it."]["link"] == "https://x/6999"


async def test_a_check_that_ran_on_the_accepted_head_must_run_on_the_one_merged(world):
    """SX6: after an approved fix, a check the accepted head ran is missing: not merged."""
    from test_shepherd import green

    runtime = await claimed(world)
    green(world, world.h0)
    await runtime.shepherd.read(TARGET)
    await runtime.shepherd.accept_head(TARGET, world.h0)
    assert runtime.shepherd.control.claim(TARGET)["accepted"]["checks"] == ["ci / test"]
    world.github.checks.pop(world.h0)
    step, sha = await fixed(world, runtime)
    await runtime.shepherd.merge_when_ready(TARGET, True)
    await approve(runtime.shepherd, step["id"], {})
    await settle(runtime)
    await settle(runtime)
    green(world, sha)
    world.github.checks[sha] = [
        {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS", "required": True}
    ]
    await runtime.shepherd.read(TARGET)
    await settle(runtime)
    assert not world.github.merged


async def test_acceptance_survives_closing_and_reopening_the_worker(world):
    """SX10: closed between Accept and green, reopened: its acceptance kept, merged after
    Resume."""
    from test_shepherd import green

    runtime = await claimed(world)
    runtime.task_actions.accept(world.task)
    await runtime.shepherd.merge_when_ready(TARGET, True)
    await runtime.workers.close(world.worker)
    await settle(runtime)
    claim = runtime.shepherd.control.claim(TARGET)
    assert claim["holder_kind"] == "owner" and claim["accepted"]["sha"] == world.h0
    await runtime.workers.reopen(world.worker)
    await settle(runtime)
    claim = runtime.shepherd.control.claim(TARGET)
    assert claim["holder"] == world.task and claim["state"] == "paused"
    green(world, world.h0)
    await runtime.shepherd.read(TARGET)
    await settle(runtime)
    assert not world.github.merged
    await runtime.shepherd.resume(TARGET)
    await runtime.shepherd.read(TARGET)
    await settle(runtime)
    assert world.github.merged


async def test_after_the_merge_the_claim_ends_and_accept_stays_the_owners(world):
    """SX11."""
    from test_shepherd import green

    runtime = await claimed(world)
    await runtime.shepherd.accept_head(TARGET, world.h0)
    await runtime.shepherd.merge_when_ready(TARGET, True)
    green(world, world.h0)
    await runtime.shepherd.read(TARGET)
    await settle(runtime)
    assert world.github.merged
    await runtime.shepherd.read(TARGET)
    await settle(runtime)
    assert runtime.shepherd.control.claim(TARGET)["state"] == "ended"
    task = runtime.tasks.get(world.task)
    assert task["status"] == "review"  # Its result still waits for the owner's Accept.


# What is kept, and for how long; a downgrade.


async def test_a_plans_diff_and_replies_go_after_30_days_its_journal_stays(world):
    runtime = await claimed(world)
    step, _ = await fixed(world, runtime)
    control = runtime.shepherd.control
    control.prune_plans(30)
    assert control.step(step["id"])["plan"]["diff"]  # Its outcome is today's.
    runtime.store.execute(
        "UPDATE shepherd_steps SET ended_at='2020-01-01T00:00:00+00:00' WHERE id=?", (step["id"],)
    )
    control.prune_plans(30)
    plan = control.step(step["id"])["plan"]
    assert "diff" not in plan and "threads" not in plan and "findings" not in plan
    assert plan["kind"] == "fix" and plan["h1"] and plan["summary"] == "timeout() returned 31."
    assert plan["evidence"] == {
        "words": "`test_timeout` failed before and passes now; 2 tests ran on both"
    }
    [entry] = runtime.shepherd.journal_view(TARGET)
    assert entry["summary"] == "timeout() returned 31." and entry["evidence"]


async def test_what_a_pr_kept_beside_its_claim_goes_when_it_merges(world):
    from test_shepherd import green

    runtime = await claimed(world)
    for prefix in ("acceptnote", "clickfirst", "mergewhy", "signedbranch"):
        runtime.store.set(f"{prefix}:{TARGET}", "1")
    runtime.store.set(f"stopped:{TARGET}", json.dumps({"kind": "fix-ci", "why": "x", "at": ""}))
    await runtime.shepherd.accept_head(TARGET, world.h0)
    runtime.store.delete(f"signedbranch:{TARGET}")
    await runtime.shepherd.merge_when_ready(TARGET, True)
    green(world, world.h0)
    await runtime.shepherd.read(TARGET)
    await settle(runtime)
    assert world.github.merged
    await runtime.shepherd.read(TARGET)
    assert runtime.shepherd.control.claim(TARGET)["state"] == "ended"
    for prefix in ("acceptnote", "clickfirst", "mergewhy", "stopped"):
        assert runtime.store.get(f"{prefix}:{TARGET}") is None, prefix


async def test_a_downgrade_lets_go_of_a_worker_left_in_a_step(world, capsys, monkeypatch):
    """SY13: an earlier version would continue its turn in its normal profile: prepared, it is
    idle, unmarked, its attempt interrupted."""
    from chiefofstaff import __main__ as entry

    runtime = await claimed(world)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    assert step["state"] == "running" and runtime.store.get(f"step:{world.worker}")
    monkeypatch.setattr(entry, "lock_data_file", lambda handle: None)
    code = entry.prepare_downgrade(None, runtime.store.directory)
    assert code == 0
    out = capsys.readouterr().out
    assert out.strip() == "Stopped preparing fixes in 1 worker(s): “Fix the login timeout”."
    assert runtime.store.get(f"step:{world.worker}") is None
    assert runtime.store.session(world.worker)["status"] == "idle"
    ended = runtime.shepherd.control.step(step["id"])
    assert ended["state"] == "interrupted" and ended["why"] == "Stopped before a downgrade"


async def test_unpushed_work_in_the_worker_is_left_as_it_is(world):
    """Decision 19: its checkout has a commit it hasn't pushed: the fix is made in a fresh copy
    detached at the PR's head; the worker's branch and its commit stay; only the fix is pushed."""
    runtime = await claimed(world)
    checkout = world.root / "work" / "fix-login" / "api"
    (checkout / "notes.md").write_text("mine, not pushed")
    run("git", "add", "-A", cwd=checkout)
    run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "wip", cwd=checkout)
    mine = run("git", "rev-parse", "HEAD", cwd=checkout)
    step, sha = await fixed(world, runtime)
    assert step["tree"] != str(checkout) and step["plan"]["fresh"] is True
    assert run("git", "rev-parse", "HEAD", cwd=checkout) == mine
    # Its own worktree's .git file is the step's to read, never to rewrite (as a worker's).
    policy = world.session().options["sandbox_config"]
    assert str(Path(step["tree"]) / ".git") in policy.readonly
    await approve(runtime.shepherd, step["id"], {})
    await settle(runtime)
    await settle(runtime)
    assert world.github.head() == sha
    assert run("git", "rev-parse", f"{sha}^", cwd=world.remote) == world.h0
    assert run("git", "rev-parse", "HEAD", cwd=checkout) == mine


# The owner's other answers to a plan: Request changes…, Dismiss.


async def test_request_changes_sends_the_owners_words_to_a_new_attempt(world):
    from chiefofstaff.publish import redo

    runtime = await claimed(world)
    step, _ = await fixed(world, runtime)
    new = await redo(runtime.shepherd, step, "Keep 31 and fix the test instead.")
    await settle(runtime)
    control = runtime.shepherd.control
    old = control.step(step["id"])
    assert old["state"] == "dismissed" and old["why"] == "You asked for changes."
    assert control.step(new["id"])["state"] == "running" and new["clicked"] == 1
    sent = world.session().sent[-1]
    assert '<message from="owner" type="changes">\nKeep 31 and fix the test instead.' in sent
    with pytest.raises(ValueError, match="decided already"):
        await redo(runtime.shepherd, old, "Again.")
    verdicts = runtime.store.all("SELECT verdict FROM verdicts ORDER BY id")
    assert [row["verdict"] for row in verdicts] == ["request-changes"]


async def test_a_dismissed_plan_is_not_prepared_again_on_that_head(world):
    from chiefofstaff.publish import dismiss

    runtime = await claimed(world)
    step, _ = await fixed(world, runtime)
    dismiss(runtime.shepherd, step["id"], "Not now")
    control = runtime.shepherd.control
    assert control.step(step["id"])["state"] == "dismissed"
    with pytest.raises(ValueError, match="decided already"):
        dismiss(runtime.shepherd, step["id"])
    with pytest.raises(ValueError, match="decided already"):
        await approve(runtime.shepherd, step["id"], {})
    view = runtime.task_actions.view(runtime.tasks.get(world.task))["shepherd"]
    assert view["action"] is None
    # Nothing tries it again by itself on that head (the owner's own click still could).
    claim = control.claim(TARGET)
    task = runtime.tasks.get(world.task)
    assert await runtime.shepherd.signals(claim, task, claim["details"]) == []
    assert not world.github.writes and world.github.head() == world.h0
    verdicts = runtime.store.all("SELECT verdict FROM verdicts ORDER BY id")
    assert [row["verdict"] for row in verdicts] == ["dismissed:Not now"]


# Reconciliation of each kind of operation, read on GitHub.


@pytest.mark.parametrize(
    ("kind", "payload", "github", "state"),
    [
        ("reply", {"in_reply_to": 501, "text": "Done."}, {"posted": "Done."}, "sent"),
        ("reply", {"in_reply_to": 501, "text": "Done."}, {}, "failed"),
        ("resolve", {"thread": "PRRT_1"}, {"resolved": True}, "sent"),
        ("resolve", {"thread": "PRRT_1"}, {}, "failed"),
        ("request-review", {"reviewers": ["maya"]}, {"requested": ["maya"]}, "sent"),
        ("request-review", {"reviewers": ["maya"]}, {}, "failed"),
        ("rerun", {"run": 501, "attempt": 1}, {}, "sent"),
        ("rerun", {"run": 501, "attempt": 2}, {}, "failed"),
        ("merge", {}, {"merged": True}, "sent"),
        ("merge", {}, {}, "failed"),
        ("merge", {}, {"broken": True}, "uncertain"),
    ],
)
async def test_each_operation_is_read_back_on_github(world, kind, payload, github, state):
    from chiefofstaff.publish import reconcile

    runtime = await claimed(world)
    fake = world.github
    if "posted" in github:
        fake.posted.append({"id": 7001, "body": github["posted"], "in_reply_to_id": 501,
                            "html_url": "https://x/7001", "user": {"login": "me"}})  # fmt: skip
    if "resolved" in github:
        fake.threads = [{"id": "PRRT_1", "isResolved": True, "isOutdated": False, "path": "a",
                         "line": 1, "comments": {"nodes": []}}]  # fmt: skip
    if github.get("merged"):
        fake.merged, fake.state = True, "MERGED"
    if github.get("broken"):
        fake.raw_broken = True
    requested = github.get("requested", [])
    answer = fake.__call__

    async def gh(*arguments, **options):
        text = " ".join(arguments)
        if fake.__dict__.get("raw_broken"):
            raise RuntimeError("GitHub request failed: HTTP 502")
        if "requested_reviewers" in text:
            return json.dumps({"users": [{"login": login} for login in requested]})
        return await answer(*arguments, **options)

    runtime.github.raw = gh
    operation = {"kind": kind, "repo": REPO, "number": 12, **payload}
    found, _ = await reconcile(runtime.shepherd, {"id": "a1", "payload": operation})
    assert found == state


MINE = [
    {"repository": {"nameWithOwner": REPO}, "number": 12, "title": "Fix the login timeout",
     "url": f"https://github.com/{REPO}/pull/12", "isDraft": False,
     "updatedAt": "2026-11-16T08:00:00Z", "createdAt": "2026-11-15T08:00:00Z"},
]  # fmt: skip


@pytest.mark.parametrize("found", ["changes", "comments", "nothing", "unreadable", "out of scope"])
async def test_what_discovery_says_of_each_pr(world, found):
    from test_shepherd import comments

    from chiefofstaff import discovery

    repos = ["acme/other"] if found == "out of scope" else [REPO]
    runtime = await world.start(worker=False, proactive_my_prs=True, github_repos=repos)
    world.github.mine = MINE
    run("git", "remote", "set-url", "origin", "https://github.com/acme/elsewhere.git",
        cwd=world.clone)  # fmt: skip
    if found in {"changes", "comments"}:
        comments(world)
        if found == "comments":
            world.github.review = ""
    elif found == "nothing":
        world.github.default_checks = [
            {"name": "ci / test", "status": "COMPLETED", "conclusion": "SUCCESS", "required": True}
        ]
    elif found == "unreadable":
        answer = world.github.__call__

        async def gh(*arguments, **options):
            if arguments[:2] == ("api", "graphql"):
                raise RuntimeError("GitHub request failed: HTTP 502")
            return await answer(*arguments, **options)

        runtime.github.raw = gh
    await discovery.discover(runtime.shepherd, force=True)
    view = discovery.rows_view(runtime.shepherd)
    rows, others = view["rows"], [item for item in view["all"] if item.get("why")]
    if found == "changes":
        assert [row["what"] for row in rows] == ["Changes requested by maya"]
    elif found == "comments":
        assert [row["what"] for row in rows] == ["2 comments to answer"]
    elif found == "nothing":
        assert rows == [] and [item["why"] for item in others] == ["nothing to do now"]
    elif found == "unreadable":
        assert rows == [] and [item["why"] for item in others] == ["could not be read"]
    else:
        assert rows == [] and others == []


async def test_a_found_pr_is_read_again_only_after_a_while(world):
    from chiefofstaff import discovery

    runtime = await world.start(worker=False, proactive_my_prs=True, github_repos=[REPO])
    world.github.mine = MINE
    first = await discovery.discover(runtime.shepherd, force=True)
    calls = len(world.github.calls)
    again = await discovery.discover(runtime.shepherd)
    assert again == first and len(world.github.calls) == calls


# The owner's actions on a kept PR, refused when they can't apply.


async def test_owner_actions_say_why_they_cant_apply(world):
    runtime = await claimed(world)
    shepherd = runtime.shepherd
    with pytest.raises(ValueError, match="It is not paused"):
        await shepherd.resume(TARGET)
    with pytest.raises(ValueError, match="doesn't keep this PR moving"):
        await shepherd.fix_it("github:acme/api#99")
    with pytest.raises(
        ValueError, match="Accept it first: Accept on its task, or Accept this commit in its menu"
    ):
        await shepherd.merge_when_ready(TARGET, True)
    with pytest.raises(ValueError, match="The PR has new commits since you looked"):
        await shepherd.accept_head(TARGET, "f" * 40)
    world.github.permission = "READ"
    await shepherd.accept_head(TARGET, world.h0)
    with pytest.raises(ValueError, match="Your account can't merge this PR"):
        await shepherd.merge_when_ready(TARGET, True)
    world.github.permission = "WRITE"
    world.github.base_rules = None
    with pytest.raises(ValueError, match="rules could not be read"):
        await shepherd.merge_when_ready(TARGET, True)
    world.github.base_rules = []
    world.github.draft = True
    with pytest.raises(ValueError, match="Not this PR: a draft"):
        await shepherd.merge_when_ready(TARGET, True)
    world.github.draft = False
    await shepherd.merge_when_ready(TARGET, True)
    assert shepherd.control.claim(TARGET)["merge_when_ready"]
    await shepherd.merge_when_ready(TARGET, False)
    assert not shepherd.control.claim(TARGET)["merge_when_ready"]
    # Kept off: the owner's again; on: its task's, paused until Resume.
    await shepherd.keep(TARGET, False)
    claim = shepherd.control.claim(TARGET)
    assert claim["holder_kind"] == "owner" and claim["reason"] == "you stopped looking after it"
    await shepherd.keep(TARGET, True)
    claim = shepherd.control.claim(TARGET)
    assert claim["holder"] == world.task and claim["keep"]
    old = runtime.store.settings()
    runtime.store.save_settings(old.model_copy(update={"paused": True}))
    with pytest.raises(ValueError, match="Chief of Staff is paused"):
        await shepherd.fix_it(TARGET)


# Suggest: a look at why, no change; a fix that does not hold is not offered.


async def test_at_suggest_it_looks_at_why_and_offers_fix_it(world):
    runtime = await claimed(world, budget=RESERVE, levels={"fix-ci": "suggest"})
    control = runtime.shepherd.control
    claim = control.claim(TARGET)
    control.update_claim(TARGET, delivered=1)
    [step] = await runtime.shepherd.signals(claim, runtime.tasks.get(world.task), claim["details"])
    await runtime.shepherd.pump()
    await settle(runtime)
    step = control.step(step["id"])
    assert step["state"] == "running" and step["level"] == "suggest"
    sent = world.session().sent[-1]
    assert "Find the cause; change no file." in sent and "Never weaken" not in sent
    sha = world.commit_fix(step["tree"])
    refused = await step_result(
        runtime, world.worker, step=step["id"], outcome="fixed", summary="x", commit=sha
    )
    assert "This step is a diagnosis" in said(refused)
    await step_result(
        runtime, world.worker, step=step["id"], outcome="diagnosis",
        summary="timeout() returns 31; the test expects 30.\nThe gateway allows 30.",
    )  # fmt: skip
    world.session().emit("session.idle")
    await settle(runtime)
    await settle(runtime)
    plan = control.step(step["id"])["plan"]
    assert plan["kind"] == "diagnosis"
    view = runtime.task_actions.view(runtime.tasks.get(world.task))["shepherd"]
    assert view["line"] == "timeout() returns 31; the test expects 30."
    assert view["action"]["label"] == "Fix it"
    with pytest.raises(ValueError, match="A diagnosis has nothing to publish"):
        await approve(runtime.shepherd, step["id"], {})
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    again = control.open_step(TARGET)
    assert again["id"] != step["id"] and again["level"] == "prepare" and again["clicked"]


async def test_a_fix_that_does_not_hold_on_its_own_commit_is_not_offered(world):
    runtime = await claimed(world)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    tree = __import__("pathlib").Path(step["tree"])
    (tree / "notes.md").write_text("not a fix")
    run("git", "add", "-A", cwd=tree)
    run("git", "commit", "-qm", "Not a fix", cwd=tree)
    sha = run("git", "rev-parse", "HEAD", cwd=tree)
    await step_result(
        runtime, world.worker, step=step["id"], outcome="fixed", summary="Fixed it.",
        commit=sha, test_command="python -m pytest -q -p no:cacheprovider",
    )  # fmt: skip
    world.session().emit("session.idle")
    await settle(runtime)
    await settle(runtime)
    plan = runtime.shepherd.control.step(step["id"])["plan"]
    assert plan["kind"] == "diagnosis"
    assert "Not offered: it did not pass on its own commit" in plan["summary"]
    view = runtime.task_actions.view(runtime.tasks.get(world.task))["shepherd"]
    assert view["action"]["label"] == "Fix it"
    # The row's Fix it works: another attempt, the owner's.
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    again = runtime.shepherd.control.open_step(TARGET)
    assert again["id"] != step["id"] and again["state"] == "running" and again["clicked"]


def test_a_steps_git_reads_the_owners_git_configuration_and_its_includes(tmp_path, monkeypatch):
    """Without Copilot's credential proxy, its sandbox keeps the owner's git configuration out
    of reach, and git then refuses to run (seen on Copilot 1.0.111): the files git reads for
    it, includes and its XDG folder too, are readable in the step."""
    from chiefofstaff.shepherd import git_config_files

    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    extra = home / "work.gitconfig"
    extra.write_text("[user]\n\temail = owner@acme.dev\n")
    (home / ".gitconfig").write_text(f"[user]\n\tname = Owner\n[include]\n\tpath = {extra}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    found = git_config_files()
    assert found == [str(home / ".gitconfig"), str(extra), str(home / ".config" / "git")]
    (home / ".gitconfig").unlink()
    assert git_config_files() == [str(home / ".config" / "git")]
