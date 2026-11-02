"""Receipts: what a worker was seen doing, next to what it says it did.

A report is the worker's claim. Receipts are observations: the shell commands it ran and their
exit codes (from Copilot's tool events), the changes in its checkouts (git) and the state of
its pull requests (gh). The review card shows both, and flags where they disagree, so "the
model says it is done" and "tests ran and passed, here is the diff" look different.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable

# Copilot's shell tools (bash on macOS and Linux, powershell on Windows).
SHELL_TOOLS = frozenset({"bash", "shell", "powershell", "run_in_terminal"})

# Matched against each simple command, from the program on: `grep pytest`, `pip install jest`
# or a commit message that mentions tests are not test runs.
KINDS = [
    (
        "test",
        re.compile(
            r"^(pytest|py\.test|unittest|tox|nox|jest|vitest|mocha|rspec|phpunit|ctest|"
            r"go test|cargo (test|nextest)|mvnw?( -\S+)* (test|verify)|gradlew?( -\S+)* "
            r"(test|check)|(npm|pnpm|yarn|bun)( run)? test|make( -\S+)* (test|check)|swift test|"
            r"dotnet test|mix test|bazel test|deno test|playwright test)\b"
        ),
    ),
    (
        "check",
        re.compile(
            r"^(ruff|flake8|pylint|mypy|pyright|eslint|tsc|prettier( -\S+)* --check|black --check|"
            r"golangci-lint|go vet|cargo (clippy|fmt --check)|rubocop|shellcheck|biome|"
            r"(npm|pnpm|yarn|bun)( run)? (lint|typecheck))\b"
        ),
    ),
    (
        "build",
        re.compile(
            r"^((npm|pnpm|yarn|bun)( run)? build|cargo build|go build|make\b(?!( -\S+)* "
            r"(test|check))|gradlew?( -\S+)* (build|assemble)|mvnw?( -\S+)* (package|install)|"
            r"docker build|uv build|build\b|dotnet build|xcodebuild)"
        ),
    ),
    ("pr", re.compile(r"^gh pr create\b")),
    ("push", re.compile(r"^git push\b")),
    ("commit", re.compile(r"^git commit\b")),
]
SEPARATORS = {"&&", "||", ";", "|", "&", "(", ")"}
# Programs that run another one: `uv run pytest`, `npx jest`, `python -m pytest`, `env X=1 make`.
PREFIXES = {"env", "time", "sudo", "nice", "nohup", "command", "exec", "xvfb-run"}
# `npm run test` runs a script, which KINDS knows by name; `npm exec jest` runs a program.
RUNNERS = {
    **{key: {"run"} for key in ("uv", "poetry", "pipenv", "hatch", "pdm", "rye")},
    **{key: {"exec", "dlx"} for key in ("bundle", "npm", "pnpm", "yarn")},
}
LAUNCHERS = {"npx", "bunx", "pnpx"}
VALUE_OPTIONS = {
    "--directory", "--env-file", "--extra", "--from", "--group", "--index", "--only-group",
    "--package", "--project", "--python", "--with", "-C", "-c", "-p",
}  # fmt: skip
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


def simple_commands(command: str) -> list[list[str]]:
    """The simple commands in a shell command line, as words (quotes respected)."""
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:  # Unbalanced quotes: a rough split is better than nothing.
        words = re.sub(r"(&&|\|\||[;|&()])", r" \1 ", command).split()
    found, current = [], []
    for word in words:
        if word in SEPARATORS:
            found.append(current)
            current = []
        else:
            current.append(word)
    found.append(current)
    return [words for words in found if words]


def skip_options(words: list[str]) -> list[str]:
    while words and words[0].startswith("-"):
        option = words.pop(0)
        if option == "--":
            break
        if option in VALUE_OPTIONS and words:
            words.pop(0)
    return words


def program(words: list[str]) -> str:
    """A simple command from the program it runs on, without the wrappers that run it."""
    words = list(words)
    while words:
        first = words[0].rsplit("/", 1)[-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]) or first in PREFIXES:
            words = skip_options(words[1:])
        elif first == "timeout":
            words = skip_options(words[1:])[1:]
        elif len(words) > 1 and words[1] in RUNNERS.get(first, ()):
            words = skip_options(words[2:])
        elif first in LAUNCHERS:
            words = skip_options(words[1:])
        elif re.fullmatch(r"python[\d.]*|py", first) and "-m" in words[1:3]:
            words = words[words.index("-m") + 1 :]
        elif first == "git":
            # git's own options come before its subcommand: git -C dir -c a=b commit.
            words = ["git", *skip_options(words[1:])]
            break
        else:
            break
    if not words:
        return ""
    return " ".join([words[0].rsplit("/", 1)[-1], *words[1:]])


def kind(command: str) -> str:
    """test, check, build, pr, push, commit or "" for a shell command line."""
    programs = [program(words) for words in simple_commands(command)]
    for name, pattern in KINDS:
        if any(pattern.search(text) for text in programs):
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
