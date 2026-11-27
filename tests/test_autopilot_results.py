"""Autopilot accepts a result only after Chief's own offline run of its checks on a private copy
of the exact commit (autopilot/verify.py), starts what waited on it, and nudges a stalled task
once more (autopilot/results.py)."""

import asyncio
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import Harness, call_tool, settle
from test_repos import receipts_of, spawn
from test_round9_receipts import bash

from chiefofstaff.autopilot import verify
from chiefofstaff.models import Settings

TEST_LINE = "pytest -q -p no:cacheprovider tests"


def git(tree: Path, *arguments: str) -> str:
    done = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *arguments],
                          cwd=tree, capture_output=True, text=True, check=True)  # fmt: skip
    return done.stdout.strip()


async def local(copy: Path, steps: list[str]) -> list[dict]:
    """The plan run as the sandboxed session runs it, here (tests run no Copilot)."""
    results = []
    for step in steps:
        line = step.replace("pytest", str(Path(sys.executable).parent / "pytest"), 1)
        done = await asyncio.to_thread(subprocess.run, ["sh", "-c", line], cwd=copy,
                                       capture_output=True, text=True)  # fmt: skip
        results.append({"command": step, "exit": done.returncode, "sandboxed": True,
                        "output": done.stdout + done.stderr, "seconds": 0.1})  # fmt: skip
        if done.returncode:
            break
    return results


@pytest.fixture
def offline(monkeypatch):
    """This computer can verify offline (its sandbox's check passed)."""

    async def none(runtime):
        return ""

    monkeypatch.setattr(verify, "refusal", none)


async def project(tmp_path, git_repo):
    (git_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    (git_repo / "conftest.py").write_text("")
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-qm", "calc")
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    old = runtime.store.settings()
    new = old.model_copy(update={"autopilot": True})
    runtime.store.save_settings(new)
    await runtime.settings_changed(old, new)
    await runtime.autopilot.start(None, by="home")
    runtime.autopilot.runner = local
    return harness, runtime


async def finished(harness, runtime, git_repo, *, change=None, command=TEST_LINE, after=None):
    """A worker's committed result, its test run observed, reported done."""
    extra = {"after": after, "wait_for": "accepted"} if after else {}
    info = await spawn(runtime, harness.router_id, title="Subtract", instructions="x",
                       project=str(git_repo), isolate=True, **extra)  # fmt: skip
    tree = Path(info["cwd"])
    (tree / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
    )
    (tree / "tests" / "test_sub.py").write_text(
        "from calc import sub\n\n\ndef test_sub():\n    assert sub(3, 2) == 1\n"
    )
    if change:
        change(tree)
    git(tree, "add", ".")
    git(tree, "commit", "-qm", "sub")
    bash(harness.session(info["session_id"]), "c1", command, "2 passed in 0.01s", 0)
    await settle(runtime)
    await call_tool(runtime, "report", info["session_id"], status="done", summary="Done.")
    await receipts_of(runtime, info["task_id"], lambda found: bool(found.get("tests")))
    await settle(runtime)
    return info, tree


async def verified(runtime):
    await runtime.autopilot.check_results()
    for _ in range(100):
        await settle(runtime)
        if not runtime.autopilot.verifying:
            return
    raise AssertionError("still verifying")


def entries(runtime, *kinds):
    return runtime.autopilot.book.entries(kinds=kinds)


async def test_a_committed_result_whose_checks_pass_offline_is_accepted(
    tmp_path, git_repo, offline
):
    harness, runtime = await project(tmp_path, git_repo)
    original, copies = runtime.autopilot.runner, []

    async def runner(copy, lines):
        copies.append(copy)
        return await original(copy, lines)

    runtime.autopilot.runner = runner
    info, tree = await finished(harness, runtime, git_repo)
    await verified(runtime)
    task = runtime.tasks.get(info["task_id"])
    assert task["status"] == "done"
    [accepted] = entries(runtime, "accepted")
    record = runtime.autopilot.book.whole(accepted["evidence"])
    assert record["commit"] == git(tree, "rev-parse", "HEAD")
    assert record["tree"] == git(tree, "rev-parse", "HEAD^{tree}") and record["tree_after"]
    assert [s["command"] for s in record["steps"]] == [TEST_LINE]
    assert record["steps"][0]["exit"] == 0 and "2 passed" in record["steps"][0]["output"]
    assert (
        sorted(record["steps"][0]["tests"])
        == [
            "tests/test_calc.py::test_add",
            "tests/test_sub.py::test_sub",
        ]
        or record["steps"][0]["tests"] == []
    )
    assert harness.store.get(f"autopilot:accepted:{info['task_id']}") == accepted["id"]
    # Its task says who accepted it (the page: "Accepted by Autopilot").
    assert runtime.task_actions.view(task)["accepted_by"] == "autopilot"
    assert not harness.store.all("SELECT id FROM feedback WHERE kind='accept'"), "nothing learned"
    [copy] = copies
    assert not copy.exists(), "its private copy is gone"
    # Undo: back to review, once.
    said = runtime.autopilot.undo(accepted["id"])
    assert "waits for your Accept again" in said
    assert runtime.tasks.get(info["task_id"])["status"] == "review"
    assert "accepted_by" not in runtime.task_actions.view(runtime.tasks.get(info["task_id"]))
    with pytest.raises(ValueError, match="undone already"):
        runtime.autopilot.undo(accepted["id"])
    await verified(runtime)
    assert runtime.tasks.get(info["task_id"])["status"] == "review", "not accepted again"


async def test_what_autopilot_accepted_says_so_wherever_it_is_said(tmp_path, git_repo, offline):
    """Never "accepted by the owner" when Autopilot accepted it: its task (with its entry, for
    the record's link), its worker's chat, the Router's words for it, its rule; the owner's own
    Accept is said as before."""
    from chiefofstaff import prompts
    from chiefofstaff.digest import needs, task_words

    harness, runtime = await project(tmp_path, git_repo)
    router = harness.router_id
    info, _ = await finished(harness, runtime, git_repo)
    later = json.loads((await call_tool(
        runtime, "spawn_worker", router, title="Write the client", instructions="Client",
        after=[info["task_id"]], wait_for="accepted", why_new="test",
    )).text_result_for_llm)  # fmt: skip
    assert later["note"] == (
        "Starts automatically once the tasks it depends on are accepted (by the owner, or by "
        "Autopilot while it is on)."
    )
    await verified(runtime)
    await settle(runtime)
    [accepted] = entries(runtime, "accepted")
    task = runtime.tasks.get(info["task_id"])
    view = runtime.task_actions.view(task)
    assert (view["accepted_by"], view["accepted_entry"]) == ("autopilot", accepted["id"])
    notice = [m for m in harness.store.messages(info["session_id"]) if m["kind"] == "notice"][-1]
    assert notice["text"] == (
        f"Autopilot accepted it: its checks pass at {accepted['payload']['commit'][:7]}."
    )
    assert notice["meta"]["autopilot"]["entry"] == accepted["id"]
    assert task_words(task, needs(runtime)) == "done, accepted by Autopilot"
    said = await call_tool(runtime, "message_session", router, session_id=info["session_id"],
                           text="More", task="continue", task_id=info["task_id"])  # fmt: skip
    assert "was accepted by Autopilot and is done" in said.text_result_for_llm
    said = await call_tool(runtime, "message_session", router, session_id=info["session_id"],
                           text="More", task="continue")  # fmt: skip
    assert "was accepted by Autopilot and is done" in said.text_result_for_llm
    assert "by the owner" not in said.text_result_for_llm
    assert "accepted by Autopilot" in prompts.AUTOPILOT_RULE
    assert harness.store.get(f"acceptedby:{info['task_id']}") == "autopilot", "E141's key"
    # Undone, then the owner's own Accept: said as theirs.
    runtime.autopilot.undo(accepted["id"])
    assert harness.store.get(f"acceptedby:{info['task_id']}") is None
    runtime.task_actions.accept(info["task_id"])
    task = runtime.tasks.get(info["task_id"])
    assert "accepted_by" not in runtime.task_actions.view(task)
    assert task_words(task, needs(runtime)) == "done"
    said = await call_tool(runtime, "message_session", router, session_id=info["session_id"],
                           text="More", task="continue", task_id=info["task_id"])  # fmt: skip
    assert "was accepted by the owner and is done" in said.text_result_for_llm


async def test_stop_cancels_a_verification_in_flight_and_its_result_stays_for_you(
    tmp_path, git_repo, offline
):
    """Review 2: Stop while Chief's own run of the checks is under way: cancelled; nothing is
    accepted or recorded for it; its result waits for the owner, verified again in a later run."""
    harness, runtime = await project(tmp_path, git_repo)
    info, _ = await finished(harness, runtime, git_repo)
    started, gate = asyncio.Event(), asyncio.Event()

    async def slow(copy, steps):
        started.set()
        await gate.wait()
        return await local(copy, steps)

    runtime.autopilot.runner = slow
    await runtime.autopilot.check_results()
    await asyncio.wait_for(started.wait(), 10)
    job = runtime.autopilot.verify_job
    await runtime.autopilot.stop(by="you", why="you pressed Stop")
    gate.set()
    await settle(runtime)
    assert job is not None and job.cancelled()
    assert runtime.tasks.get(info["task_id"])["status"] == "review"
    assert entries(runtime, "accepted", "not-done") == []
    assert not harness.store.get(f"autopilot:tried:{info['task_id']}"), "tried again later"
    assert not runtime.autopilot.verifying


async def test_a_result_verified_across_a_stop_is_not_accepted(tmp_path, git_repo, offline):
    """Review 2: its checks passed, but Stop came while they ran (its generation): left for the
    owner, never filed under that run or the next."""
    harness, runtime = await project(tmp_path, git_repo)
    info, _ = await finished(harness, runtime, git_repo)

    async def braked(copy, steps):
        runtime.autopilot.book.bump()  # Stop's brake, while the checks run.
        return await local(copy, steps)

    runtime.autopilot.runner = braked
    await verified(runtime)
    assert runtime.tasks.get(info["task_id"])["status"] == "review"
    assert entries(runtime, "accepted") == []


async def test_what_waited_on_it_starts_carrying_its_origin(tmp_path, git_repo, offline):
    harness, runtime = await project(tmp_path, git_repo)
    info, _ = await finished(harness, runtime, git_repo)
    later = await spawn(runtime, harness.router_id, title="Client", instructions="Client",
                        after=[info["task_id"]], wait_for="accepted")  # fmt: skip
    await settle(runtime)
    assert runtime.tasks.get(later["task_id"])["status"] == "queued"
    await verified(runtime)
    await settle(runtime)
    [released] = entries(runtime, "released")
    assert released["task_id"] == later["task_id"]
    assert runtime.autopilot.origins.of(later["session_id"])["entries"] == [released["id"]]
    assert runtime.tasks.get(later["task_id"])["status"] != "queued", "it started"


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        ("uncommitted", "its result isn't committed"),
        ("weakened", "changes tests, CI or checks"),
        ("outside", "reaches outside its copy"),
        ("failing", "failed in Chief's own run"),
        ("moved", "its branch moved on since"),
        # Live (B2): a test that needs the network fails offline, which isn't its code failing.
        ("network", "Couldn't verify offline: it waits for you"),
    ],
)
async def test_a_result_that_does_not_verify_waits_for_the_owner(
    tmp_path, git_repo, offline, setup, why
):
    harness, runtime = await project(tmp_path, git_repo)
    change, command = None, TEST_LINE
    if setup == "weakened":

        def change(tree):  # An existing test changed to pass.
            (tree / "tests" / "test_calc.py").write_text(
                "from calc import add\n\n\ndef test_add():\n    assert True\n"
            )
    elif setup == "outside":
        command = f"cd ../other && {TEST_LINE}"
    elif setup == "failing":

        def change(tree):
            (tree / "tests" / "test_sub.py").write_text("def test_sub():\n    assert 1 == 2\n")

    elif setup == "network":

        def change(tree):
            (tree / "tests" / "test_page.py").write_text(
                "import urllib.request\n\n\ndef test_page():\n"
                "    raise OSError('<urlopen error [Errno 8] nodename nor servname provided, or "
                "not known>')\n"
            )

    info, tree = await finished(harness, runtime, git_repo, change=change, command=command)
    if setup == "uncommitted":
        (tree / "calc.py").write_text("broken = True\n")
    if setup == "moved":
        original = runtime.autopilot.runner

        async def then_moved(copy, steps):
            found = await original(copy, steps)
            (tree / "more.py").write_text("x = 1\n")
            git(tree, "add", ".")
            git(tree, "commit", "-qm", "more")
            return found

        runtime.autopilot.runner = then_moved
    await verified(runtime)
    assert runtime.tasks.get(info["task_id"])["status"] == "review"
    [left] = entries(runtime, "not-done")
    assert why in left["why"]["reason"], left["why"]
    assert why in runtime.snapshot()["autopilot"]["set_aside"][info["task_id"]]


async def test_without_an_offline_sandbox_nothing_is_verified(tmp_path, git_repo):
    harness, runtime = await project(tmp_path, git_repo)
    runtime.sandbox_host = False
    info, _ = await finished(harness, runtime, git_repo)
    await verified(runtime)
    assert runtime.tasks.get(info["task_id"])["status"] == "review"
    [left] = entries(runtime, "not-done")
    assert left["why"]["reason"] == "Couldn't verify offline: it waits for you"


async def test_the_first_check_of_a_copilot_version_is_waited_for_not_a_reason(
    harness, monkeypatch
):
    """Live (B1, a fresh install): the safety check of this Copilot version, never run here,
    was a reason to leave the first result for the owner; it is waited for (bounded), then
    what it found holds: passed, verified; failed, it waits for the owner."""
    from chiefofstaff.backend import TESTED_CLI

    runtime = await harness.connect()
    runtime.sandbox_host = True
    found = {"passed": True}

    async def probe(runtime):
        await asyncio.sleep(0.3)
        return {**found, "why": "" if found["passed"] else "its shell reached GitHub",
                "at": "now", "checked": time.time()}  # fmt: skip

    monkeypatch.setattr("chiefofstaff.step_probe.probe", probe)
    runtime.store.delete(f"stepprobe:{TESTED_CLI}")
    assert await asyncio.wait_for(verify.refusal(runtime), 10) == ""
    found["passed"] = False
    runtime.store.delete(f"stepprobe:{TESTED_CLI}")
    assert await asyncio.wait_for(verify.refusal(runtime), 10) == verify.OFFLINE


async def test_its_private_copy_is_in_no_folder_its_sandbox_denies(tmp_path, git_repo, offline):
    """Live (B1, Copilot 1.0.116): the copy was in Chief's data folder, which the sandbox
    denies, and the CLI refused to start a command there ("a sandbox denied path"). It is a
    private folder of its own, in none of the denied ones (the data folder, the code folder,
    the projects)."""
    harness, runtime = await project(tmp_path, git_repo)
    seen = []

    async def runner(copy, lines):
        seen.append(copy)
        return [{"command": line, "exit": 0, "output": "1 passed", "seconds": 0.1,
                 "sandboxed": True} for line in lines]  # fmt: skip

    runtime.autopilot.runner = runner
    await finished(harness, runtime, git_repo)
    await verified(runtime)
    settings = harness.store.settings()
    denied = [harness.store.directory, *(Path(p) for p in settings.projects)]
    [copy] = seen
    assert not any(copy.resolve().is_relative_to(d.resolve()) for d in denied), copy
    # Beside the workers' caches, where no worker writes (not the system's temporary folder,
    # which Copilot's sandbox lets every worker write).
    assert copy.is_relative_to(runtime.cache_tier.parent / "temp"), copy
    assert not copy.exists()


async def test_its_offline_run_uses_the_workers_shared_caches(tmp_path, git_repo):
    """Live (B1): uv, given no cache it could write (the owner's, read-only to the sandbox),
    failed ("failed to open file ~/.cache/uv/…: Operation not permitted"), and Python, its
    toolchain unreadable to the sandbox, could not start ("file system sandbox blocked open()").
    Its steps run as a sandboxed worker's: its toolchains and the owner's caches read-only, the
    caches workers share (the tier) writable, nothing else of the owner's."""
    harness, runtime = await project(tmp_path, git_repo)
    info = await spawn(runtime, harness.router_id, title="Subtract", instructions="x",
                       project=str(git_repo), isolate=True)  # fmt: skip
    row = harness.store.session(info["session_id"])
    backend, policies = runtime.connected(), []
    original = backend.sandbox

    def sandbox(policy):
        policies.append(policy)
        return original(policy)

    backend.sandbox = sandbox
    resume, sessions = harness.client.resume_session, []

    async def resumed(session_id, **options):
        sessions.append(await resume(session_id, **options))
        return sessions[-1]

    harness.client.resume_session = resumed
    copy = tmp_path / "copy"
    copy.mkdir()
    [step] = await verify.run_offline(runtime, copy, ["echo ok"], row)
    tier = runtime.cache_tier
    [policy] = policies
    assert set(policy.readwrite) == {str(copy), str(tier)}
    worker = runtime.cache_plan(row)
    # Its init script (in the data folder, denied here) aside.
    data = harness.store.directory
    read = {path for path in worker.readonly if not Path(path).is_relative_to(data)}
    assert read and read == set(policy.readonly)
    [command] = sessions[-1].executed
    for name, folder in (("UV_CACHE_DIR", "uv"), ("npm_config_cache", "npm")):
        assert f"{name}={shlex.quote(str(tier / folder))}" in command, command
    assert step["exit"] == 0


async def test_its_offline_run_builds_the_sessions_tools_before_it_runs_a_step(harness, tmp_path):
    """Live (B1, Copilot 1.0.116): a session resumed for the verification has no tools until
    they are built ("Tool 'bash' does not exist"), so no step ran and every result was left
    for the owner. They are built first, as the step check's session does."""
    from types import SimpleNamespace

    await harness.connect()
    runtime, client = harness.runtime, harness.client
    original, built = client.resume_session, []

    async def resume(session_id, **options):
        session = await original(session_id, **options)

        async def execute(request, **options):
            if not built:
                return {"textResultForLlm": "Tool 'bash' does not exist. Available tools that "
                        "can be called are .", "resultType": "failure"}  # fmt: skip
            return {"textResultForLlm": "ok\nCOS-EXIT-0\n", "resultType": "success",
                    "toolTelemetry": {"properties": {"sandboxApplied": "true"}}}  # fmt: skip

        async def validate(**options):
            built.append(True)

        session.rpc.tools = SimpleNamespace(execute=execute, initialize_and_validate=validate)
        return session

    client.resume_session = resume
    copy = tmp_path / "copy"
    copy.mkdir()
    [step] = await verify.run_offline(runtime, copy, ["echo ok"])
    assert built and (step["exit"], step["sandboxed"]) == (0, True), step


@pytest.mark.parametrize(
    "command",
    [
        "cd /tmp && pytest", "pytest /etc", "pytest ~/x", "pytest $HOME", "pytest `x`",
        "pytest ../x", "pytest || true", "pytest; echo", "pytest | tail", "pytest > out",
        "git -C x status", "pytest --rootdir=/x", "ruff check --exit-zero", "curl x",
        "pytest & sleep 1",
    ],
)  # fmt: skip
def test_a_plan_that_reaches_outside_or_masks_a_failure_is_rejected(command):
    steps, why = verify.plan([command])
    assert steps == [] and why


def test_a_plan_of_tests_checks_and_builds_is_run_step_by_step():
    steps, why = verify.plan(["ruff check . && pytest -q tests/test_x.py", "pytest -q tests"])
    assert why == "" and steps == ["ruff check .", "pytest -q tests/test_x.py", "pytest -q tests"]


def test_a_last_run_that_only_lists_tests_is_left_out_not_a_reason():
    """Live (B1): the worker's last test command only listed its tests (`--collect-only`), and
    the whole plan was refused ("isn't a test, check or build"): that run is left out; what
    its criteria rest on is still run, and a last run reaching outside is still refused."""
    found = {
        "criteria": [{"criterion": "tests pass", "state": "verified",
                      "command": "uv run --with pytest pytest -q"}],
        "tests": {"passed": True, "command": "uv run --with pytest pytest -q --collect-only"},
    }  # fmt: skip
    commands, why = verify.commands_of(found)
    assert why == "" and verify.plan(commands) == (["uv run --with pytest pytest -q"], "")
    found["tests"]["command"] = "cd ../other && pytest -q"
    assert "reaches outside" in verify.plan(verify.commands_of(found)[0])[1]
