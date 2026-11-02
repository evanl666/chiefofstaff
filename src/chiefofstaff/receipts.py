"""Receipts: what a worker was seen doing, next to what it says it did.

A report is the worker's claim. Receipts are observations: the shell commands it ran and their
exit codes (from Copilot's tool events), the changes in its checkouts (git) and the state of
its pull requests (gh). The review card shows both, and flags where they disagree, so "the
model says it is done" and "tests ran and passed, here is the diff" look different.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Copilot's shell tools (bash on macOS and Linux, powershell on Windows).
SHELL_TOOLS = frozenset({"bash", "shell", "powershell", "run_in_terminal"})

KINDS = [
    (
        "test",
        re.compile(
            r"\b(pytest|py\.test|unittest|tox|nox|jest|vitest|mocha|rspec|phpunit|ctest|"
            r"go test|cargo test|cargo nextest|mvn( -\S+)* (test|verify)|gradlew? (test|check)|"
            r"(npm|pnpm|yarn|bun)( run)? test|make (test|check)|swift test|dotnet test|mix test)\b"
        ),
    ),
    (
        "check",
        re.compile(
            r"\b(ruff|flake8|pylint|mypy|pyright|eslint|tsc|prettier --check|golangci-lint|"
            r"go vet|cargo clippy|cargo fmt --check|rubocop|shellcheck|biome|(npm|pnpm|yarn)"
            r"( run)? (lint|typecheck))\b"
        ),
    ),
    (
        "build",
        re.compile(
            r"\b((npm|pnpm|yarn|bun)( run)? build|cargo build|go build|make\b(?! (test|check))|"
            r"gradlew? (build|assemble)|mvn( -\S+)* (package|install)|docker build|uv build|"
            r"python -m build|dotnet build|xcodebuild)\b"
        ),
    ),
    ("commit", re.compile(r"\bgit\b[^|;&]*\bcommit\b")),
    ("push", re.compile(r"\bgit\b[^|;&]*\bpush\b")),
    ("pr", re.compile(r"\bgh pr create\b")),
]
EXIT = re.compile(r"exit(?:ed with)? code (-?\d+)", re.IGNORECASE)
SAYS_TESTS_PASS = re.compile(
    r"\b(all )?(tests?|specs?|test suite|checks?)\b[^.\n]{0,40}"
    r"\b(pass(ed|ing|es)?|green|succeed(ed)?)|\b\d+ (tests? )?passed\b",
    re.IGNORECASE,
)
SAYS_CHANGED = re.compile(
    r"\b(added|fixed|implemented|changed|updated|refactored|created|removed|renamed|committed)\b",
    re.IGNORECASE,
)


def kind(command: str) -> str:
    """test, check, build, commit, push, pr or "" for a shell command."""
    for name, pattern in KINDS:
        if pattern.search(command):
            return name
    return ""


def exit_code(output: str, success: bool | None) -> int | None:
    """The exit code a shell tool reported at the end of its output, else from success."""
    found = EXIT.findall(output[-400:] if output else "")
    if found:
        return int(found[-1])
    if success is None:
        return None
    return 0 if success else 1


def summarize(commands: Iterable[dict], evidence: dict | None, report: str) -> dict:
    """The receipts for a task: observed commands, changes and PRs, and what disagrees."""
    commands = list(commands)
    evidence = evidence or {}
    by_kind: dict[str, list[dict]] = {}
    for command in commands:
        if command["kind"]:
            by_kind.setdefault(command["kind"], []).append(command)

    def last(name: str) -> dict | None:
        runs = [c for c in by_kind.get(name, []) if c["exit_code"] is not None]
        if not runs:
            return None
        final = runs[-1]
        return {
            "command": final["command"][:200],
            "exit_code": final["exit_code"],
            "passed": final["exit_code"] == 0,
            "runs": len(by_kind[name]),
        }

    checkouts = evidence.get("checkouts", [])
    changed = {
        "files": sum(c.get("files", 0) for c in checkouts),
        "insertions": sum(c.get("insertions", 0) for c in checkouts),
        "deletions": sum(c.get("deletions", 0) for c in checkouts),
        "commits": sum(c.get("commits", 0) for c in checkouts),
        "uncommitted": sum(c.get("uncommitted", 0) for c in checkouts),
    }
    flags = []
    tests = last("test")
    if SAYS_TESTS_PASS.search(report or ""):
        if not tests:
            flags.append("Says tests pass, but no test run was observed.")
        elif not tests["passed"]:
            code = tests["exit_code"]
            flags.append(f"Says tests pass, but the last test run failed (exit {code}).")
    elif tests and not tests["passed"]:
        flags.append(f"The last test run failed (exit {tests['exit_code']}).")
    if checkouts and SAYS_CHANGED.search(report or "") and not any(changed.values()):
        flags.append("Says it made changes, but its checkouts have none.")
    for pr in evidence.get("prs", []):
        if pr.get("checks") == "failing":
            flags.append(f"Checks are failing on {pr['label']}.")
    return {
        "tests": tests,
        "checks": last("check"),
        "build": last("build"),
        "commits": len(by_kind.get("commit", [])),
        "pushed": bool(by_kind.get("push")),
        "changed": changed,
        "checkouts": checkouts,
        "prs": evidence.get("prs", []),
        "commands": [
            {"command": c["command"][:200], "kind": c["kind"], "exit_code": c["exit_code"]}
            for c in commands[-12:]
        ],
        "observed": len(commands),
        "flags": flags,
    }


SHORTSTAT = re.compile(
    r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?"
)


def shortstat(text: str) -> dict:
    match = SHORTSTAT.search(text or "")
    if not match:
        return {"files": 0, "insertions": 0, "deletions": 0}
    files, insertions, deletions = match.groups()
    return {
        "files": int(files),
        "insertions": int(insertions or 0),
        "deletions": int(deletions or 0),
    }


def checks_state(rollup: list[dict]) -> str:
    """passing, failing, pending or none, from gh's statusCheckRollup."""
    if not rollup:
        return "none"
    states = {
        (item.get("conclusion") or item.get("state") or item.get("status") or "").upper()
        for item in rollup
    }
    if states & {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
        return "failing"
    if states & {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING", ""}:
        return "pending"
    return "passing"
