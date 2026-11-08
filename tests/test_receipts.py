import json
from types import SimpleNamespace

import pytest
from conftest import call_tool, settle

from chiefofstaff import receipts


def command(text, code, kind=None):
    return {
        "command": text,
        "kind": receipts.kind(text) if kind is None else kind,
        "exit_code": code,
    }


def test_commands_are_recognized_by_what_they_do():
    assert receipts.kind("cd app && uv run pytest -q tests/") == "test"
    assert receipts.kind("npm run test -- --watch=false") == "test"
    assert receipts.kind("go test ./...") == "test"
    assert receipts.kind("uv run ruff check .") == "check"
    assert receipts.kind("cargo build --release") == "build"
    assert receipts.kind("git -c user.name=a commit -qm 'Fix'") == "commit"
    assert receipts.kind("git push origin HEAD") == "push"
    assert receipts.kind("gh pr create --fill") == "pr"
    assert receipts.kind("ls -la") == ""
    assert receipts.kind("python3 -m pytest -x && git commit -am 'Fix'") == "test"
    assert receipts.kind("FOO=1 npx --yes jest --ci") == "test"
    assert receipts.kind("./gradlew --offline test") == "test"
    assert receipts.kind("uv run --extra dev pytest -q") == "test"


def test_mentioning_a_tool_is_not_running_it():
    for text in [
        "pip install pytest",
        "npm install --save-dev jest",
        "grep -rn pytest .",
        "cat jest.config.js",
        "echo 'run pytest later'",
    ]:
        assert receipts.kind(text) == "", text
    assert receipts.kind('git commit -m "fix pytest flake"') == "commit"
    assert receipts.kind("git log --grep push | head") == ""


def test_exit_codes_come_from_copilots_output():
    assert receipts.exit_code("ok\n<shellId: 3 completed with exit code 2>", True) == 2
    assert receipts.exit_code("done\n<exited with exit code 0>", None) == 0
    assert receipts.exit_code("", False) == 1
    assert receipts.exit_code("", None) is None


def test_claims_are_checked_against_what_was_observed():
    clean = receipts.summarize(
        [command("uv run pytest -q", 1), command("uv run pytest -q", 0)],
        {"checkouts": [{"files": 3, "insertions": 40, "deletions": 2, "commits": 1}]},
        "Fixed the bug. All tests pass.",
    )
    assert clean["tests"] == {
        "command": "uv run pytest -q",
        "exit_code": 0,
        "passed": True,
        "runs": 2,
        "failed_before": True,
        "running": False,
    }
    assert clean["changed"]["insertions"] == 40 and clean["flags"] == []

    unverified = receipts.summarize(
        [command("ls", 0)], {"checkouts": [{}]}, "Tests pass; fixed it."
    )
    assert unverified["flags"] == [
        "Says tests pass, but no test run was observed.",
        "Says it made changes, but its checkouts have none.",
    ]
    analysis = receipts.summarize(
        [command("python3 analyze.py", 0)],
        {"checkouts": [{}]},
        "Analyzed data/sales.csv. No files were changed. Line 302 adds 3,600.00 to north.",
    )
    assert analysis["flags"] == [], "an analysis that changed nothing says so: not a claim"
    assert not receipts.says_changed("I did not change anything, nothing was committed.")
    assert receipts.says_changed("Nothing failed. I added a regression test.")
    for claim in (
        "The endpoint did not validate input, so I fixed it and added a regression test.",
        "The login page didn't redirect after sign-in, so I updated the handler.",
        "Without changing the public API, I refactored the parser.",
        "Tests were not running on CI because the workflow had no trigger, so I fixed it.",
    ):
        assert receipts.says_changed(claim), claim
    assert not receipts.says_changed("None of the files were changed.")
    for nothing in ("The failure is expected; I changed nothing.", "I updated no files."):
        assert not receipts.says_changed(nothing), nothing
    assert receipts.says_changed("I added no-cache headers to the responses.")
    failing = receipts.summarize([command("npm test", 1)], None, "Done.")
    assert failing["flags"] == ["The last test run failed (exit 1)."]
    red = receipts.summarize(
        [], {"prs": [{"label": "acme/app#7", "checks": "failing"}]}, "Opened the PR."
    )
    assert red["flags"] == ["Checks are failing on acme/app#7."]


def test_acceptance_criteria_count_only_what_was_seen():
    criteria = [
        "pytest tests/test_orders.py passes",
        "Submitting twice creates one order",
        "ruff passes",
        "Docs updated",
    ]
    claims = [
        {"criterion": "pytest tests/test_orders.py passes", "result": "met",
         "evidence": "`uv run pytest tests/test_orders.py`: 6 passed"},
        {"criterion": "Submitting twice creates one order", "result": "met",
         "evidence": "I reasoned through the handler"},
        {"criterion": "ruff passes", "result": "met", "evidence": "`ruff check .` is clean"},
    ]  # fmt: skip
    commands = [
        command("uv run pytest tests/test_orders.py", 1),
        command("uv run pytest tests/test_orders.py", 0),
        command("ruff check .", 1),
    ]
    found = receipts.summarize(commands, {}, "Done.", criteria, claims)
    assert [item["state"] for item in found["criteria"]] == [
        "verified",  # The command it named was seen passing.
        "claimed",  # Nothing observed backs it.
        "contradicted",  # The command it named failed.
        "not_checked",  # Not reported.
    ]
    assert found["verdict"] == "failed"
    assert "Says “ruff passes” is met, but `ruff check .` failed (exit 1)." in found["flags"]
    assert found["tests"]["failed_before"], "red, then green"

    # Bug bash: `cat fin.txt g.txt` failed on g.txt (not there yet); fin.txt was fine.
    part = [
        {"criterion": "fin.txt exists", "result": "met", "evidence": "`cat fin.txt` showed fin"}
    ]
    found = receipts.summarize([command("cat fin.txt g.txt", 1)], {}, "Done.", ["fin.txt exists"],
                               part)  # fmt: skip
    assert found["criteria"][0]["state"] == "claimed" and not found["flags"]

    in_order = [{"criterion": "the test passes", "result": "met",
                 "evidence": "`uv run pytest tests/test_orders.py`"}]  # fmt: skip
    found = receipts.summarize(commands, {}, "Done.", criteria[:1], in_order)
    assert found["verdict"] == "verified", "matched in order when worded differently"
    # As real workers write it: the check they ran, cited with fewer options than they used.
    ran = [command("git --no-pager log --oneline -3 --all --decorate && cat hello.txt", 0)]
    cited = [{"criterion": "commit", "result": "met",
              "evidence": "`git log --oneline --all --decorate`: 6460933 Add hello"}]  # fmt: skip
    assert receipts.summarize(ran, {}, "", ["commit"], cited)["verdict"] == "verified"
    chain = [command("git log --oneline -2 && git diff --quiet main", 1)]
    logged = [{"criterion": "commit", "result": "met", "evidence": "`git log --oneline -2`"}]
    assert receipts.summarize(chain, {}, "", ["commit"], logged)["criteria"][0]["state"] == (
        "claimed"
    ), "a chain that failed later says nothing against its first part"
    diffed = [{"criterion": "commit", "result": "met", "evidence": "`git diff --quiet main`"}]
    assert receipts.summarize(chain, {}, "", ["commit"], diffed)["verdict"] == "failed"
    later = [command("pytest tests/test_tools.py", 1), command("ls", 0), command("pwd", 0)]
    tools = [{"criterion": "t", "result": "met", "evidence": "`pytest tests/test_tools.py` ok"}]
    assert receipts.summarize(later, {}, "", ["t"], tools)["verdict"] == "failed", (
        "ls and pwd are not in test_tools.py: only whole words name a command"
    )
    other = [{"criterion": "commit", "result": "met", "evidence": "`git status --short`"}]
    assert receipts.summarize(ran, {}, "", ["commit"], other)["verdict"] == "unverified", (
        "a command it did not run backs nothing"
    )
    stale = receipts.summarize([], {"stale": "Out of date: the code changed."}, "", [], [])
    assert stale["flags"][0] == "Out of date: the code changed." and stale["verdict"] == ""


def test_git_and_gh_output_is_read():
    assert receipts.shortstat(" 3 files changed, 12 insertions(+), 1 deletion(-)") == {
        "files": 3,
        "insertions": 12,
        "deletions": 1,
    }
    assert receipts.shortstat(" 1 file changed, 2 deletions(-)")["deletions"] == 2
    assert receipts.checks_state([{"conclusion": "SUCCESS"}, {"state": "SUCCESS"}]) == "passing"
    assert (
        receipts.checks_state([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]) == "failing"
    )
    assert receipts.checks_state([{"status": "IN_PROGRESS", "conclusion": ""}]) == "pending"
    assert receipts.checks_state([]) == "none"
    # An approval gate waits for a person: CI itself has passed.
    gated = [
        {"name": "build", "conclusion": "SUCCESS"},
        {"name": "Owner Approval", "status": "IN_PROGRESS", "conclusion": ""},
    ]
    assert receipts.checks_state(gated) == "passing"
    assert receipts.awaiting_approval(gated) == ["Owner Approval"]


def test_only_the_workers_own_word_counts_as_saying_tests_pass():
    assert receipts.says_tests_pass("Fixed it. All tests pass.")
    for report in (
        "Megha said all 33 checks passed.",
        "> all tests passed",
        "The log reads “tests pass” but it hangs.",
        "CI is green: all 33 checks passed.",
    ):
        assert not receipts.says_tests_pass(report), report


async def test_a_failing_test_run_piped_into_tail_is_seen_failing(harness):
    runtime = await harness.connect()
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Fix", instructions="Fix", why_new="t"
    )
    worker = json.loads(result.text_result_for_llm)["session_id"]
    session = harness.session(worker)
    for call, output in [
        ("c1", "F.\n1 failed, 1 passed in 0.1s\n<exited with exit code 0>"),
        ("c2", "..\n2 passed in 0.1s\n<exited with exit code 0>"),
    ]:
        command = "uv run --with pytest pytest -q 2>&1 | tail -15"
        session.emit("tool.execution_start", tool_name="bash", arguments={"command": command},
                     tool_call_id=call)  # fmt: skip
        session.emit("tool.execution_complete", tool_call_id=call, success=True,
                     result=SimpleNamespace(content=output))  # fmt: skip
    await settle(runtime)
    codes = [row["exit_code"] for row in harness.store.all("SELECT exit_code FROM commands")]
    assert codes == [1, 0], "tail's exit code is not the tests'"


@pytest.mark.parametrize(
    ("output", "failed"),
    [
        ("F.\n1 failed, 2 passed in 0.10s", True),
        ("======= 2 passed, 1 error in 0.31s =======", True),
        ("retried 3 errors\nflaky 2 failed attempts recovered\n5 passed in 1.2s", False),
        ("4 passed, 1 xfailed in 0.2s", False),
        ("--- FAIL: TestX (0.00s)\nFAIL\tpkg\t0.1s", True),
        ("test result: FAILED. 1 passed; 1 failed", True),
        ("Tests:       0 failed, 4 passed, 4 total", False),
        (" Tests  1 failed | 3 passed (4)", True),
        ("  3 passing (20ms)\n  1 failing", True),
        ("FAILED tests/test_x.py::test_y - assert 1 == 2", True),
        ("FAILED (failures=1)", True),
        ("BUILD FAILED in 3s", True),
        ("ERROR Retry - giving up\nBUILD SUCCESSFUL in 2s", False),
        ("  1 failing upload\n    ✓ retries\n  5 passing", False),
        ("2 errors in 0.5s while retrying\n7 passed in 1.1s", False),
        ("test_x[1 failed] PASSED\n3 passed in 0.2s", False),
        # The new test failing before the fix, then every test passing after it, in one command.
        ("F\n1 failed in 0.05s\n......\n6 passed in 0.08s", False),
        ("......\n6 passed in 0.08s\nF\n1 failed, 5 passed in 0.1s", True),
    ],
)
def test_a_piped_test_run_is_read_by_its_summary(output, failed):
    assert receipts.masked_failure("run tests 2>&1 | tail -5", output) == failed
    assert not receipts.masked_failure("run tests", output), "unpiped: its exit code says"


async def test_a_done_report_right_after_failing_tests_is_asked_to_look_again(harness):
    runtime = await harness.connect()
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Fix", instructions="Fix", why_new="t"
    )
    worker = json.loads(result.text_result_for_llm)["session_id"]
    session = harness.session(worker)
    command = "uv run --with pytest pytest -q"
    session.emit("tool.execution_start", tool_name="bash", arguments={"command": command},
                 tool_call_id="c1")  # fmt: skip
    output = SimpleNamespace(content="F\n1 failed in 0.1s\n<exited with exit code 1>")
    session.emit("tool.execution_complete", tool_call_id="c1", success=True, result=output)
    await settle(runtime)
    early = await call_tool(runtime, "report", worker, status="done", summary="Fixed.")
    assert early.result_type == "failure" and "last test run failed" in early.text_result_for_llm
    again = await call_tool(
        runtime,
        "report",
        worker,
        status="done",
        summary="Fixed; the failure is the old flaky test.",
    )
    assert again.result_type == "success", "once: an expected failure is reported as such"


def test_a_check_that_failed_and_then_passed_has_passed():
    rerun = [
        {"name": "validate", "conclusion": "FAILURE", "completedAt": "2026-11-05T09:00:00Z"},
        {"name": "validate", "conclusion": "SUCCESS", "completedAt": "2026-11-05T09:20:00Z"},
        {"context": "ci/lint", "state": "SUCCESS", "startedAt": "2026-11-05T09:01:00Z"},
    ]
    assert receipts.checks_state(rerun) == "passing" and receipts.failing_checks(rerun) == []
    again = [
        *rerun,
        {"name": "validate", "conclusion": "FAILURE", "completedAt": "2026-11-05T10:00:00Z"},
    ]
    assert receipts.checks_state(again) == "failing"
    assert receipts.failing_checks(again) == ["validate"]


async def test_a_task_card_shows_its_prs_as_they_are_now(harness):
    runtime = await harness.connect()
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Fix", instructions="Fix", why_new="t"
    )
    worker = json.loads(result.text_result_for_llm)["session_id"]
    url = "https://github.com/acme/app/pull/7"

    def pr(conclusion):
        rollup = [
            {"name": "validate", "conclusion": conclusion, "completedAt": "2026-11-05T09:00Z"}
        ]
        return json.dumps({"url": url, "state": "OPEN", "statusCheckRollup": rollup})

    harness.gh_replies[f"pr view {url}"] = pr("FAILURE")
    await call_tool(runtime, "report", worker, status="done", summary=f"Opened {url}")
    await settle(runtime)
    task = runtime.tasks.current(worker)
    found = runtime.task_actions.receipts(task)
    assert found["prs"][0]["checks"] == "failing" and found["prs"][0]["failing"] == ["validate"]
    assert "Checks are failing on acme/app#7: validate." in found["flags"]

    harness.gh_replies[f"pr view {url}"] = pr("SUCCESS")
    await runtime.task_actions.refresh_open_prs(every=3600)
    assert runtime.task_actions.receipts(task)["prs"][0]["checks"] == "failing", "not yet due"
    await runtime.task_actions.refresh_open_prs("acme/app#7", every=3600)
    fresh = runtime.task_actions.receipts(task)
    assert fresh["prs"][0]["checks"] == "passing" and not fresh["flags"], "something happened on it"

    harness.gh_replies[f"pr view {url}"] = pr("FAILURE")
    read = json.loads(
        (
            await call_tool(runtime, "read_session", harness.router_id, session_id=worker)
        ).text_result_for_llm
    )
    assert read["task"]["prs_now"][0]["checks"] == "failing", "the Router reads it as it is now"


async def test_the_router_reads_pr_status_from_github_now(harness):
    runtime = await harness.connect()
    rollup = [
        {"name": "validate", "conclusion": "FAILURE", "completedAt": "2026-11-05T09:00Z"},
        {"name": "validate", "conclusion": "SUCCESS", "completedAt": "2026-11-05T09:30Z"},
    ]
    harness.gh_replies["pr view https://github.com/acme/app/pull/7"] = json.dumps(
        {"title": "Fix", "url": "u", "state": "OPEN", "statusCheckRollup": rollup,
         "reviewDecision": "APPROVED", "mergeable": "MERGEABLE"}
    )  # fmt: skip
    result = await call_tool(
        runtime, "check_prs", harness.router_id,
        prs=["https://github.com/acme/app/pull/7", "github:acme/app#7"],
    )  # fmt: skip
    [pr] = json.loads(result.text_result_for_llm)["prs"]
    assert pr["checks"] == "passing" and pr["failing"] == [] and pr["review"] == "APPROVED"
    worker = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="W", instructions="w"
    )
    refused = await call_tool(
        runtime, "check_prs", json.loads(worker.text_result_for_llm)["session_id"], prs=["x"]
    )
    assert refused.result_type == "failure", "the Router's tool"


def run(session, call, command, output, success=True, **extra):
    session.emit("tool.execution_start", tool_name="bash", arguments={"command": command},
                 tool_call_id=call)  # fmt: skip
    session.emit("tool.execution_complete", tool_call_id=call, success=success,
                 result=SimpleNamespace(content=output), **extra)  # fmt: skip


def read(session, call, shell, output, tool="read_bash"):
    session.emit("tool.execution_start", tool_name=tool, arguments={"shellId": shell},
                 tool_call_id=call)  # fmt: skip
    session.emit("tool.execution_complete", tool_call_id=call, success=True,
                 result=SimpleNamespace(content=output))  # fmt: skip


async def worker_with_task(harness):
    runtime = await harness.connect()
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Fix", instructions="Fix", why_new="t"
    )
    worker = json.loads(result.text_result_for_llm)["session_id"]
    return runtime, worker, harness.session(worker)


def observed_tests(runtime, worker):
    return runtime.task_actions.receipts(runtime.tasks.current(worker))["tests"]


async def test_a_long_test_run_gets_its_result_when_it_ends(harness):
    runtime, worker, session = await worker_with_task(harness)
    published = []
    publish = runtime.task_actions.publish
    runtime.task_actions.publish = lambda task: (published.append(task["id"]), publish(task))
    still = "collected 900 items\n....\n<command with shellId: 4 is still running after 30 " \
        "seconds. The command is still running. Use stop_bash to stop it.>"  # fmt: skip
    run(session, "c1", "uv run pytest tests/", still)
    await settle(runtime)
    found = observed_tests(runtime, worker)
    assert found["running"] and found["passed"] is None, "running: no result yet, not a pass"
    assert published, "the card shows it running"
    read(session, "r1", "4", "....F\nFAILED tests/a.py::t\n1 failed, 899 passed in 80.1s\n"
         "<shellId: 4 completed with exit code 1>")  # fmt: skip
    await settle(runtime)
    assert observed_tests(runtime, worker)["passed"] is False

    run(session, "c2", "uv run pytest tests/ 2>&1 | tail -3",
        "<command started in background with shellId: 5>")  # fmt: skip
    ended = SimpleNamespace(type="shell_completed", shell_id="5", exit_code=0)
    event = SimpleNamespace(type=SimpleNamespace(value="system.notification"),
                            data=SimpleNamespace(content="ended", kind=ended))  # fmt: skip
    for handler in session.handlers:
        handler(event)
    await settle(runtime)
    assert observed_tests(runtime, worker)["passed"] is True, "Copilot's notice that it ended"
    read(session, "r2", "5", "F\n1 failed, 899 passed in 80.1s\n<shellId: 5 completed with "
         "exit code 0>")  # fmt: skip
    await settle(runtime)
    assert observed_tests(runtime, worker)["passed"] is False, "tail's 0; its output says it failed"

    run(session, "c3", "uv run pytest tests/", "<command with shellId: 6 is still running "
        "after 30 seconds.>")  # fmt: skip
    read(session, "s1", "6", "<shellId: 6 stopped>", tool="stop_bash")
    await settle(runtime)
    found = observed_tests(runtime, worker)
    assert not found["running"] and found["passed"] is False, "stopped: no result of its own"


async def test_a_command_that_never_ran_is_no_failed_test_run(harness):
    runtime, worker, session = await worker_with_task(harness)
    run(session, "c1", "uv run pytest -q", "\n2 passed in 0.1s\n<shellId: 1 completed with "
        "exit code 0>")  # fmt: skip
    blocked = SimpleNamespace(message="Command not executed. The 'pkill' command is not allowed.")
    run(session, "c2", "pkill -f server; uv run pytest -q", "", success=False, error=blocked)
    await settle(runtime)
    assert observed_tests(runtime, worker)["passed"] is True
    assert "test run failed" not in json.dumps(
        runtime.task_actions.receipts(runtime.tasks.current(worker))["flags"]
    )


async def test_the_tests_pass_when_a_later_part_of_the_line_fails(harness):
    runtime, worker, session = await worker_with_task(harness)
    run(session, "c1", "uv run pytest -q && git push", "......\n6 passed in 0.2s\n! [rejected] "
        "main -> main (fetch first)\n<shellId: 1 completed with exit code 1>")  # fmt: skip
    await settle(runtime)
    assert observed_tests(runtime, worker)["passed"] is True
    run(session, "c2", "uv run pytest -q && git push", "F.\n1 failed, 1 passed in 0.2s\n"
        "<shellId: 2 completed with exit code 1>")  # fmt: skip
    await settle(runtime)
    assert observed_tests(runtime, worker)["passed"] is False


async def test_tests_that_fail_before_other_commands_on_the_line_failed(harness):
    runtime, worker, session = await worker_with_task(harness)
    run(session, "c1", "uv run pytest -q local_fail; rm -rf .venv uv.lock; git status --short",
        "F\n1 failed in 0.05s\n?? notes.txt\n<shellId: 1 completed with exit code 0>")  # fmt: skip
    await settle(runtime)
    assert observed_tests(runtime, worker)["passed"] is False


async def test_a_test_run_is_known_by_its_runners_summary(harness):
    runtime, worker, session = await worker_with_task(harness)
    run(session, "c1", "cd model && ./scripts/run_unit_tests.sh", "....\n2 failed, 40 passed in "
        "9.1s\n<shellId: 1 completed with exit code 1>")  # fmt: skip
    run(session, "c2", "cat /tmp/old-run.log", "....\n42 passed in 9.1s\n<shellId: 2 "
        "completed with exit code 0>")  # fmt: skip
    await settle(runtime)
    assert observed_tests(runtime, worker)["passed"] is False, "an old log is not a run"
    run(session, "c3", "conda run -n torch python -m unittest discover", "......\nRan 6 tests "
        "in 0.4s\n\nOK\n<shellId: 3 completed with exit code 0>")  # fmt: skip
    await settle(runtime)
    assert observed_tests(runtime, worker) | {"runs": 0} == observed_tests(runtime, worker) | {
        "runs": 0, "passed": True, "command": "conda run -n torch python -m unittest discover"
    }  # fmt: skip
    assert receipts.kind("bash -lc 'cd svc && pytest -q'") == "test"
    assert receipts.kind("ligradle :ads-model:test") == "test"
    assert receipts.kind("pixi run pytest") == "test"


async def test_the_prs_checks_passing_after_a_local_failure_say_so(harness):
    runtime, worker, session = await worker_with_task(harness)
    run(session, "c1", "uv run pytest -q", "F\n1 failed in 0.1s\n<shellId: 1 completed with "
        "exit code 1>")  # fmt: skip
    await settle(runtime)
    url = "https://github.com/acme/app/pull/7"
    rollup = [{"name": "tests", "conclusion": "SUCCESS", "completedAt": "2099-01-01T00:00:00Z"}]
    harness.gh_replies[f"pr view {url}"] = json.dumps(
        {"url": url, "state": "OPEN", "statusCheckRollup": rollup}
    )
    for _ in range(2):  # Asked once to look at the failed run first.
        await call_tool(runtime, "report", worker, status="done", summary=f"Opened {url}.")
    await settle(runtime)
    found = runtime.task_actions.receipts(runtime.tasks.current(worker))
    assert found["tests"]["ci_passing"] == ["acme/app#7"]
    assert found["flags"] == [
        "The last local test run failed (exit 1), but the checks on acme/app#7 pass now."
    ]


def test_ci_that_started_before_the_local_failure_is_not_newer():
    failed = [{"command": "pytest -q", "kind": "test", "exit_code": 1,
               "ended_at": "2026-11-05T10:02:00+00:00"}]  # fmt: skip
    rollup = [{"name": "ci", "conclusion": "SUCCESS", "startedAt": "2026-11-05T10:00:00Z",
               "completedAt": "2026-11-05T10:06:00Z"}]  # fmt: skip
    pr = {"label": "acme/app#7", "checks": "passing", "state": "OPEN",
          "checks_started": receipts.checks_started(rollup)}  # fmt: skip
    found = receipts.summarize(failed, {"prs": [pr]}, "Done.")
    assert found["tests"]["ci_passing"] == [], "CI ran the code pushed before the failure"
    pr["checks_started"] = "2026-11-05T10:05:00Z"
    assert receipts.summarize(failed, {"prs": [pr]}, "Done.")["tests"]["ci_passing"] == [
        "acme/app#7"
    ]


async def test_a_reconnect_ends_the_commands_left_running(harness):
    runtime, worker, session = await worker_with_task(harness)
    run(session, "c1", "uv run pytest tests/", "<command with shellId: 4 is still running "
        "after 30 seconds.>")  # fmt: skip
    await settle(runtime)
    assert observed_tests(runtime, worker)["running"]
    await runtime.reconnect()
    await settle(runtime)
    assert observed_tests(runtime, worker) is None, "no result, and not running"


@pytest.mark.parametrize(
    "command, expected",
    [
        ("# run the suite\npytest -q", "test"),
        ("cd svc  # the service\nuv run pytest -q", "test"),
        ("pytest \\\n  -q tests/", "test"),
        ("cat > notes.md <<'EOF'\npytest -q\nEOF", ""),
        ("python - <<EOF\nimport os\nEOF\nruff check .", "check"),
        ("make -C services/api test", "test"),
        ("mvn -B -pl core clean test", "test"),
        ("./gradlew -p app clean :core:test", "test"),
        ("pnpm -r test", "test"),
        ("yarn workspace web test", "test"),
        ("npm -w pkg run test:unit", "test"),
        ("npm install test-utils", ""),
    ],
)
def test_command_kinds_across_lines_and_runners(command, expected):
    assert receipts.kind(command) == expected


@pytest.mark.parametrize(
    "output, expected",
    [
        ("TAP version 13\nok 1 - adds\nnot ok 2 - subtracts\n1..2\n# pass 1\n# fail 1", "failed"),
        ("TAP version 13\nok 1 - adds\nok 2 - subtracts\n1..2\n# pass 2\n# fail 0", "passed"),
        ("ok  \tgithub.com/acme/app\t0.012s", "passed"),
        ("Tests:       1 skipped, 5 passed, 6 total", "passed"),
        ("Tests:       1 skipped, 1 failed, 4 passed, 6 total", "failed"),
        ("  5 passing (20ms)", "passed"),
        ("  5 passing (20ms)\n  1 failing", "failed"),
        ("[INFO] Tests run: 12, Failures: 0, Errors: 0, Skipped: 1", "passed"),
        ("[ERROR] Tests run: 12, Failures: 2, Errors: 0, Skipped: 0", "failed"),
        ("[ERROR] Tests run: 12, Failures: 0, Errors: 1, Skipped: 0", "failed"),
    ],
)
def test_runner_summaries(output, expected):
    assert receipts.last_summary(output) == expected


@pytest.mark.parametrize(
    "report, expected",
    [
        ("All tests pass.", True),
        ("Tests do not pass yet: two fail on the new schema.", False),
        ("The tests never passed on this branch.", False),
        ("Tests pass locally; CI is still running.", True),
        ("Tests pass on CI.", False),
        ("CI checks are green.", False),
    ],
)
def test_what_a_report_says_about_tests(report, expected):
    assert receipts.says_tests_pass(report) is expected


@pytest.mark.parametrize(
    "report, expected",
    [
        ("I fixed the null check.", True),
        ("Nothing to change: it was already fixed upstream.", False),
        ("The crash was fixed by Bob in #12, so no change here.", False),
        ("No files were changed.", False),
    ],
)
def test_what_a_report_says_about_changes(report, expected):
    assert receipts.says_changed(report) is expected


def test_a_check_not_finished_yet_is_not_the_oldest_run():
    rollup = [
        {"name": "tests", "conclusion": "FAILURE", "startedAt": "2026-01-01T00:00:00Z",
         "completedAt": "2026-01-01T00:05:00Z"},
        {"name": "tests", "status": "IN_PROGRESS", "startedAt": "2026-01-01T01:00:00Z",
         "completedAt": "0001-01-01T00:00:00Z"},
    ]  # fmt: skip
    assert receipts.checks_state(rollup) == "pending", "the rerun is the latest"
    done = [{**rollup[1], "conclusion": "SUCCESS", "completedAt": "0001-01-01T00:00:00Z"}]
    assert receipts.checks_started(done) == "2026-01-01T01:00:00Z"
