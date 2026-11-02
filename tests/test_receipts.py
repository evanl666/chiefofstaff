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
