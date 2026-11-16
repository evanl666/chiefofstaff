"""The shepherd's two checks in policy.py: what a session in a step may do (the second line
behind its sandbox), and what Chief of Staff's own operations may be."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from chiefofstaff.policy import operation_denial, step_denial

FOLDERS = ["/work/w/.shepherd/s1/tree", "/work/w/.shepherd/s1"]
H0, H1 = "a" * 40, "b" * 40


def shell(command: str):
    return SimpleNamespace(kind="shell", full_command_text=command)


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin HEAD",
        "cd tree && git commit -am fix && git push",
        "git -c x=y push",
        'g"i"t push',
        "git remote add up https://example.com/x.git",
        "git remote set-url origin https://example.com/x.git",
        "git send-pack x",
        "git config credential.helper store",
        "git config url.https://x.insteadOf https://github.com/",
        "git p",  # An alias: git's commands are known, this is not one.
        "gh pr comment 1 -b hi",
        "gh api user",
        "/usr/local/bin/gh auth token",
        "G=gh; $G api user",
        'g"h" api user',
        "ssh git@example.com",
        "scp file host:",
        "curl https://api.github.com/repos/acme/api",
        "wget https://raw.githubusercontent.com/acme/api/main/x",
        "curl -X POST https://uploads.github.com/x",
        "docker pull ghcr.io/acme/image",
        "python -c \"import socket; socket.create_connection(('github.com', 443))\"",
    ],
)
def test_a_step_never_publishes_or_reaches_github(command):
    assert step_denial(shell(command), FOLDERS)


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q tests/test_push.py",
        "cd /work/w/.shepherd/s1/tree && uv run pytest -q",
        "git status --porcelain",
        "git diff HEAD~1",
        "git add -A && git commit -m 'Fix the login timeout'",
        "git log --oneline -5",
        "npm test",
        "curl -sS https://registry.npmjs.org/left-pad",
        "go test ./...",
        "cargo test -- --list",
    ],
)
def test_a_step_works_and_commits(command):
    assert step_denial(shell(command), FOLDERS) is None


def test_writes_only_in_the_steps_folders(tmp_path):
    inside = SimpleNamespace(kind="write", resolved_path=f"{FOLDERS[0]}/src/a.py")
    outside = SimpleNamespace(kind="write", resolved_path="/work/w/repo/src/a.py")
    temp = SimpleNamespace(
        kind="write", resolved_path=str(Path(__import__("tempfile").gettempdir()) / "x")
    )
    assert step_denial(inside, FOLDERS) is None
    assert step_denial(temp, FOLDERS) is None
    assert step_denial(outside, FOLDERS)
    assert step_denial(SimpleNamespace(kind="write", resolved_path=""), FOLDERS)


def test_credentials_are_not_readable():
    home = Path.home()
    for secret in (".config/gh/hosts.yml", ".git-credentials", ".netrc", ".ssh/id_ed25519"):
        assert step_denial(SimpleNamespace(kind="read", path=str(home / secret)), FOLDERS)
    assert (
        step_denial(SimpleNamespace(kind="read", path=f"{FOLDERS[0]}/README.md"), FOLDERS) is None
    )


@pytest.mark.parametrize("kind", ["mcp", "url", "memory", "hook", "extension-management", ""])
def test_nothing_else_is_available(kind):
    assert step_denial(SimpleNamespace(kind=kind), FOLDERS)


def test_only_its_own_tools():
    assert (
        step_denial(SimpleNamespace(kind="custom-tool", tool_name="shepherd_result"), FOLDERS)
        is None
    )
    assert (
        step_denial(SimpleNamespace(kind="custom-tool", tool_name="draft_github_review"), FOLDERS)
        is None
    )
    for name in ("tell_session", "report", "follow_thread", "add_repo", "draft_slack_reply"):
        assert step_denial(SimpleNamespace(kind="custom-tool", tool_name=name), FOLDERS)


def push_operation(**changes):
    return {
        "kind": "push",
        "head_repo": "acme/api",
        "ref": "fix-login",
        "lease": H0,
        "commit": H1,
        "protected": False,
        "default_branch": "main",
    } | changes


def push_argv(ref="fix-login", lease=H0, commit=H1, repo="acme/api", hooks=True, extra=()):
    head = ["git", "-c", "core.fsmonitor=false"]
    if hooks:
        head += ["-c", "core.hooksPath=/dev/null"]
    return [
        *head,
        "-C",
        "/tree",
        "push",
        *extra,
        "--no-verify",
        f"--force-with-lease=refs/heads/{ref}:{lease}",
        f"https://github.com/{repo}.git",
        f"{commit}:refs/heads/{ref}",
    ]


def test_a_leased_push_of_one_commit_to_the_head_branch():
    assert operation_denial(push_operation(), push_argv()) is None


@pytest.mark.parametrize(
    ("operation", "argv", "words"),
    [
        (push_operation(ref="main"), push_argv(ref="main"), "default branch"),
        (push_operation(ref="trunk", default_branch="trunk"), push_argv(ref="trunk"), "default"),
        (push_operation(protected=True), push_argv(), "protected"),
        (push_operation(protected=None), push_argv(), "protection unknown"),
        (push_operation(), push_argv(hooks=False), "hooks"),
        (push_operation(), push_argv(extra=("--force",)), "force"),
        (push_operation(), push_argv(extra=("-f",)), "force"),
        (push_operation(), push_argv(extra=("--delete",)), "deletion"),
        (push_operation(), push_argv(extra=("--all",)), "force"),
        (push_operation(), push_argv(repo="evil/api"), "leased"),
        (push_operation(), push_argv(lease="c" * 40), "leased"),
        (push_operation(), push_argv(commit="c" * 40), "leased"),
        (push_operation(lease="abc"), push_argv(lease="abc"), "full SHAs"),
        (push_operation(commit=H0), push_argv(commit=H0), "full SHAs"),
        (push_operation(head_repo="not a repo"), push_argv(), "not known"),
        (push_operation(ref="-x"), push_argv(ref="-x"), "not known"),
        (push_operation(), ["git", "-c", "core.hooksPath=/dev/null", "fetch"], "not a push"),
    ],
)
def test_pushes_refused(operation, argv, words):
    found = operation_denial(operation, argv)
    assert found and words in found


def test_a_head_matched_merge_only():
    operation = {"kind": "merge", "repo": "acme/api", "number": 12, "head": H1}
    url = "https://github.com/acme/api/pull/12"
    assert (
        operation_denial(
            operation, ["gh", "pr", "merge", url, "--squash", "--match-head-commit", H1]
        )
        is None
    )
    for argv in (
        ["gh", "pr", "merge", url, "--squash", "--match-head-commit", H0],
        ["gh", "pr", "merge", url, "--squash"],
        ["gh", "pr", "merge", url, "--admin", "--match-head-commit", H1],
        ["gh", "pr", "merge", url, "--squash", "--match-head-commit", H1, "--admin"],
        ["gh", "pr", "merge", url, "--auto", "--match-head-commit", H1],
        ["gh", "pr", "merge", url, "--squash", "--match-head-commit", H1, "--delete-branch"],
        [
            "gh",
            "pr",
            "merge",
            "https://github.com/acme/api/pull/13",
            "--squash",
            "--match-head-commit",
            H1,
        ],
    ):
        assert operation_denial(operation, argv)


def test_github_writes_go_only_to_a_plans_endpoints():
    reply = {"kind": "reply"}
    ok = ["gh", "api", "repos/acme/api/pulls/12/comments/99/replies", "--method=POST", "--input=-"]
    assert operation_denial(reply, ok) is None
    assert (
        operation_denial(
            {"kind": "request-review"},
            [
                "gh",
                "api",
                "repos/acme/api/pulls/12/requested_reviewers",
                "--method=POST",
                "--input=-",
            ],
        )
        is None
    )
    for argv in (
        ["gh", "api", "repos/acme/api/pulls/12/comments/99/replies", "--method=DELETE"],
        ["gh", "api", "repos/acme/api/pulls/12/comments/99/replies"],
        ["gh", "api", "repos/acme/api/git/refs/heads/main", "--method=POST", "--input=-"],
        ["gh", "api", "repos/acme/api/pulls/12/merge", "--method=POST"],
        ["gh", "pr", "comment", "12"],
    ):
        assert operation_denial(reply, argv)


def test_resolve_and_rerun():
    from chiefofstaff.publish import RESOLVE

    resolve = ["gh", "api", "graphql", "-f", f"query={RESOLVE}", "-f", "id=PRRT_1"]
    assert operation_denial({"kind": "resolve"}, resolve) is None
    other = ["gh", "api", "graphql", "-f", "query=mutation{deleteRef(input:{}){clientMutationId}}"]
    assert operation_denial({"kind": "resolve"}, other)
    rerun = ["gh", "run", "rerun", "123", "--failed", "--repo", "acme/api"]
    assert operation_denial({"kind": "rerun"}, rerun) is None
    assert operation_denial({"kind": "rerun"}, ["gh", "run", "rerun", "123", "--repo", "acme/api"])
    assert operation_denial(
        {"kind": "rerun"}, ["gh", "run", "cancel", "123", "--failed", "--repo", "acme/api"]
    )
    assert operation_denial({"kind": "delete"}, ["gh", "repo", "delete"])
