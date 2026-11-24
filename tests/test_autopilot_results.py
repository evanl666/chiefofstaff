"""Autopilot accepts a result only after Chief's own offline run of its checks on a private copy
of the exact commit (autopilot/verify.py), starts what waited on it, and nudges a stalled task
once more (autopilot/results.py)."""

import asyncio
import subprocess
import sys
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
    assert not harness.store.all("SELECT id FROM feedback WHERE kind='accept'"), "nothing learned"
    assert not (harness.store.directory / "autopilot-verify").exists() or not any(
        (harness.store.directory / "autopilot-verify").iterdir()
    ), "its private copy is gone"
    # Undo: back to review, once.
    said = runtime.autopilot.undo(accepted["id"])
    assert "waits for your Accept again" in said
    assert runtime.tasks.get(info["task_id"])["status"] == "review"
    with pytest.raises(ValueError, match="undone already"):
        runtime.autopilot.undo(accepted["id"])
    await verified(runtime)
    assert runtime.tasks.get(info["task_id"])["status"] == "review", "not accepted again"


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
