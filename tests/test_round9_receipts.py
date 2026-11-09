"""Bug bash 9 (0.1.33), receipts: a test run in which no test ran, one filtered to nothing, a
look at the test runner taken for a test run, and generated files a worker committed."""

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import Harness, call_tool, settle
from test_repos import receipts_of, run, spawn
from test_ui_text import STATIC, run_js

from chiefofstaff import receipts
from chiefofstaff.models import Settings

NONE_RAN = "uv run --with pytest pytest -q tests/test_textkit_extra.py 2>&1 | tail -3"
SHOWN = "uv run --with pytest pytest -q tests/test_textkit_extra.py"


def ran(command, code, kind="test"):
    return {"command": command, "kind": kind, "exit_code": code,
            "ended_at": "2026-11-08T10:00:00+00:00"}  # fmt: skip


def bash(session, call, command, output, code):
    session.emit("tool.execution_start", tool_name="bash", arguments={"command": command},
                 tool_call_id=call)  # fmt: skip
    done = SimpleNamespace(content=f"{output}\n<exited with exit code {code}>")
    session.emit("tool.execution_complete", tool_call_id=call, success=code == 0, result=done)


def test_a_run_in_which_no_test_ran_says_so_not_that_it_failed():
    """G7: "✗ tests" and "The last test run failed (exit 5)." for a run that printed "no tests
    ran" and exited 0: the 5 was made up (pytest's code for it)."""
    code = receipts.test_exit(NONE_RAN, "\nno tests ran in 0.01s\n<exited with exit code 0>", 0)
    found = receipts.summarize([ran(NONE_RAN, code)], None, "Added the tests.")
    tests = found["tests"]
    assert tests["no_tests"] and tests["passed"] is False and tests["exit_code"] is None, tests
    assert found["flags"] == [f"Its last test run ran no tests (`{SHOWN}`)."]
    said = receipts.summarize([ran(NONE_RAN, code)], None, "All tests pass.")
    assert said["flags"] == [f"Says tests pass, but its last test run ran no tests (`{SHOWN}`)."]

    # Cited as what shows a criterion met: shows nothing, and names no exit code.
    claims = [{"criterion": "The new tests pass", "result": "met", "evidence": f"`{SHOWN}`"}]
    found = receipts.summarize([ran(NONE_RAN, code)], None, "Done.", ["The new tests pass"],
                               claims)  # fmt: skip
    assert found["criteria"][0]["state"] == "claimed"
    assert found["criteria"][0]["exit_code"] is None
    assert not any("exit" in flag for flag in found["flags"]), found["flags"]

    # Before the run that passed, it is no failure: not "failed first, then passed".
    found = receipts.summarize([ran(NONE_RAN, code), ran("uv run pytest -q", 0)], None, "")
    assert found["tests"]["passed"] and not found["tests"]["failed_before"]


def test_no_test_ran_by_its_output_whatever_the_exit_code():
    none = receipts.NO_TESTS_RAN
    assert receipts.test_exit("uv run pytest -q -k nothing", "5 deselected in 0.01s", 5) == none
    assert receipts.test_exit("npx jest", "No tests found, exiting with code 1", 1) == none
    # Mocha exits with how many tests failed: its 5 is five failures, not "none ran".
    mocha = receipts.test_exit("npx mocha", "  3 passing (20ms)\n  5 failing\n", 5)
    assert mocha == 5 and mocha != none
    found = receipts.summarize([ran("npx mocha", mocha)], None, "")
    assert "no_tests" not in found["tests"]
    assert found["flags"] == ["The last test run failed (exit 5)."]


def test_the_tests_chip_says_no_tests_ran():
    """G7: the chip read "✗ tests" (failed): neither failed nor passed."""
    found = run_js(
        f"const k = await import({json.dumps((STATIC / 'tasks.js').as_uri())});\n"
        """
        document.createElement = (tag) => ({ tag, className: "", textContent: "", children: [],
          attrs: {}, addEventListener() {}, setAttribute(key, value) { this.attrs[key] = value; },
          append(...nodes) { this.children.push(...nodes); } });
        s.state.snapshot = { sessions: [], tasks: [], pr_titles: {} };
        const walk = (node) =>
          typeof node === "string" ? [] : [node, ...node.children.flatMap(walk)];
        const chips = (tests) => walk(k.chips({ id: "t1", session_id: "w1", status: "review",
          receipts: { tests: { command: "pytest -q x.py", runs: 1, failed_before: false,
            running: false, own: true, ...tests }, flags: [], changed: {}, prs: [] } }))
          .filter((node) => node.className.startsWith("receipt "))
          .map((node) => [node.className, node.textContent]);
        return [chips({ passed: false, exit_code: null, no_tests: true }),
          chips({ passed: false, exit_code: 1 })];
        """
    )
    assert found == [[["receipt warn", "no tests ran"]], [["receipt fail", "✗ tests"]]]


def test_a_test_run_filtered_to_nothing_has_no_result():
    """G8: `pytest -q 2>&1 | grep FAILED` printing nothing (every test passed) counted as a
    failed run: grep's 1 (no line matched) is not the tests' exit code."""
    assert receipts.test_exit("pytest -q 2>&1 | grep FAILED", "", 1) is None
    assert receipts.test_exit("go test ./... | grep FAIL", "<exited with exit code 1>", 1) is None
    # What it lets through still shows a failure.
    failed = "FAILED tests/test_a.py::test_x - assert 1 == 2\n<exited with exit code 0>"
    assert receipts.test_exit("pytest -q 2>&1 | grep FAILED", failed, 0) == 1
    assert receipts.test_exit("go test ./... | grep FAIL", "--- FAIL: TestX (0.00s)", 0) == 1
    assert receipts.test_exit("pytest -q", "<exited with exit code 1>", 1) == 1, "its own"


@pytest.mark.parametrize(
    "command",
    [
        "python3 -m pytest --version",
        "uv run --with pytest pytest --collect-only -q",
        "pytest --co tests/",
        "pytest --fixtures",
        "pytest --markers",
        "pytest -h",
        "go test -list . ./...",
        "npx jest --listTests",
        "npm test -- --help",
        "cargo test -- --list",
        "cargo test --no-run",
        "npx vitest list",
    ],
)
def test_asking_a_test_runner_about_itself_is_no_test_run(command):
    """D8: `python3 -m pytest --version` (pytest not installed, exit 1) became the task's test
    run: "✗ tests" while its own runner had passed."""
    assert receipts.kind(command) == "", command
    assert receipts.kind(f"{command} && git status") == ""


def test_options_that_look_like_those_still_run_tests():
    for command in [
        "uv run pytest --cov=src -q",
        "pytest -q --color=yes",
        "pytest -k help",
        "go test -run TestList ./...",
        "pytest --version; pytest -q",
    ]:
        assert receipts.kind(command) == "test", command


async def test_a_look_at_the_runner_or_a_filtered_run_is_not_the_tasks_test_run(harness):
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, title="Fix", instructions="Fix")
    session = harness.session(made["session_id"])
    bash(session, "c1", "uv run pytest -q", "....\n9 passed in 0.1s", 0)
    bash(session, "c2", "python3 -m pytest --version", "No module named pytest", 1)
    bash(session, "c3", "uv run pytest -q 2>&1 | grep FAILED", "", 1)
    await settle(runtime)
    found = runtime.task_actions.receipts(runtime.tasks.get(made["task_id"]))
    assert found["tests"]["passed"] is True, found["tests"]
    assert found["tests"]["command"] == "uv run pytest -q"
    assert found["flags"] == []


def test_generated_files_and_new_lockfiles_are_said():
    evidence = {"checkouts": [{"files": 6, "generated": ["a/__pycache__/", "b.pyc", "c/x.o",
                                                         "d.class"],
                               "lockfiles": ["uv.lock", "web/package-lock.json"]}]}  # fmt: skip
    assert receipts.summarize([], evidence, "")["flags"] == [
        "Commits generated files: a/__pycache__/, b.pyc, c/x.o and 1 more.",
        "Adds uv.lock and web/package-lock.json, which the project did not have.",
    ]


def git(tree: Path, *arguments: str):
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", *arguments, cwd=tree)


async def test_committed_generated_files_and_a_new_lockfile_are_flagged(tmp_path, git_repo):
    """D10: `__pycache__/*.pyc` and `uv.lock` committed by a worker counted with no word:
    "✓ Verified · +18 −2 in 6 files". Still counted (a PR carries them), now said."""
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    info = await spawn(runtime, harness.router_id, title="Textkit", instructions="x",
                       project=str(git_repo), isolate=True)  # fmt: skip
    tree = Path(info["cwd"])
    (tree / "textkit" / "__pycache__").mkdir(parents=True)
    (tree / "textkit" / "core.py").write_text("x = 1\n")
    for name in ("__init__", "core"):
        (tree / "textkit" / "__pycache__" / f"{name}.cpython-312.pyc").write_bytes(b"\0pyc")
    (tree / "uv.lock").write_text("version = 1\n")
    git(tree, "add", "-f", ".")
    git(tree, "commit", "-qm", "Add textkit")
    await call_tool(runtime, "report", info["session_id"], status="done", summary="Done.")
    found = await receipts_of(runtime, info["task_id"], lambda found: found["changed"]["files"])
    assert found["changed"]["files"] == 4, found["changed"]
    assert "Commits generated files: textkit/__pycache__/." in found["flags"], found["flags"]
    assert "Adds uv.lock, which the project did not have." in found["flags"], found["flags"]


async def test_a_lockfile_the_project_has_updated_is_not_flagged(tmp_path, git_repo):
    (git_repo / "uv.lock").write_text("version = 1\n")
    git(git_repo, "add", "-f", "uv.lock")
    git(git_repo, "commit", "-qm", "Lock")
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    info = await spawn(runtime, harness.router_id, title="Bump", instructions="x",
                       project=str(git_repo), isolate=True)  # fmt: skip
    tree = Path(info["cwd"])
    (tree / "uv.lock").write_text("version = 2\n")
    (tree / "app.py").write_text("x = 1\n")
    git(tree, "add", "-f", ".")
    git(tree, "commit", "-qm", "Bump")
    await call_tool(runtime, "report", info["session_id"], status="done", summary="Done.")
    found = await receipts_of(runtime, info["task_id"], lambda found: found["changed"]["files"])
    assert found["changed"]["files"] == 2 and found["flags"] == [], found


def test_failing_checks_on_its_own_pr_are_not_flagged_again():
    """H8: "CI failing" four times on one card: its marker, its line, the "✗ CI" chip and the
    flag "Checks are failing on …#95: docs.". Its own PR's chip (and line) say it."""
    prs = [
        {"label": "acme/app#95", "state": "OPEN", "checks": "failing", "failing": ["docs"],
         "own": True},
        {"label": "acme/app#96", "state": "OPEN", "checks": "failing", "reviewed": True},
        # One it is about, not its own nor reviewed: no chip in sight, so said here.
        {"label": "acme/lib#3", "state": "OPEN", "checks": "failing", "failing": ["build"]},
    ]  # fmt: skip
    found = receipts.summarize([], {"prs": prs}, "Opened the PR.")
    assert found["flags"] == ["Checks are failing on acme/lib#3: build."]


def test_each_command_is_split_once_per_summary_whatever_the_criteria(monkeypatch):
    """C6: a task's view took a second with 16,200 commands: every command was split into
    words again for every criterion."""
    calls = Counter()

    def counted(name):
        real = getattr(receipts, name)

        def counting(*arguments):
            calls[name] += 1
            return real(*arguments)

        monkeypatch.setattr(receipts, name, counting)

    counted("words")
    counted("simple_commands")
    # "tools" is in each line's letters, not its words: each line must be split to tell.
    commands = [ran(f"cat tools/part{index}.py", 0, kind="") for index in range(2000)]
    criteria = [f"Criterion {number}" for number in range(4)]
    claims = [{"criterion": name, "result": "met", "evidence": "`tools`"} for name in criteria]
    found = receipts.summarize(commands, None, "", criteria, claims)
    assert [item["state"] for item in found["criteria"]] == ["claimed"] * 4
    assert calls["simple_commands"] <= 2000 and calls["words"] <= 2000 + 4, calls
