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
    }
    assert clean["changed"]["insertions"] == 40 and clean["flags"] == []

    unverified = receipts.summarize(
        [command("ls", 0)], {"checkouts": [{}]}, "Tests pass; fixed it."
    )
    assert unverified["flags"] == [
        "Says tests pass, but no test run was observed.",
        "Says it made changes, but its checkouts have none.",
    ]
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
