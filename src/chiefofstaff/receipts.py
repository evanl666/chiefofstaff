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


QUOTED = re.compile(r"`([^`\n]{4,})`")


def words(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def names(snippet: str, command: str) -> bool:
    """Whether a quoted command is the one that ran: the same text, or (from two words on) all
    of its words in it, as `git log --stat` is in `git --no-pager log --oneline --stat -2`."""
    if snippet in command or command in snippet:
        return True
    quoted = words(snippet)
    return len(quoted) >= 2 and set(quoted) <= set(words(command))


def backing(evidence: str, commands: list[dict]) -> dict | None:
    """The observed command a criterion's evidence names (in backticks, or verbatim), latest
    first: what makes a worker's "met" an observation rather than a claim."""
    quoted = QUOTED.findall(evidence or "")
    for command in reversed(commands):
        text = command["command"]
        if any(names(snippet, text) for snippet in quoted):
            return command
        if not quoted and len(text) >= 8 and text in (evidence or ""):
            return command
    return None


def failed_there(evidence: str, command: str) -> bool:
    """Whether a failed command line failed in the part the evidence names. In `a && b && c`
    an exit code is the last part's that ran: a part before the last may have passed."""
    parts = [part for part in simple_commands(command) if part]
    if len(parts) <= 1:
        return True
    last = " ".join(parts[-1])
    return any(names(snippet, last) for snippet in QUOTED.findall(evidence or ""))


def criteria_results(criteria: list[str], claims: list[dict], commands: list[dict]) -> list[dict]:
    """Each acceptance criterion with what the worker reported and what backs it.

    state: verified (met, and an observed command shows it), contradicted (met, but that command
    failed), claimed (met, with nothing observed to show it), not_met or not_checked.
    """

    def words(text: object) -> str:
        return " ".join(str(text).lower().split())

    by_text = {words(claim.get("criterion", "")): index for index, claim in enumerate(claims)}
    chosen = [by_text.get(words(criterion)) for criterion in criteria]
    for index, found in enumerate(chosen):
        if found is None and index < len(claims) and index not in chosen:
            chosen[index] = index  # Reported in order, in its own words.
    results = []
    for criterion, found in zip(criteria, chosen, strict=True):
        claim = claims[found] if found is not None else None
        result = (claim or {}).get("result") or "not_checked"
        proof = (claim or {}).get("evidence", "")
        observed = backing(proof, commands) if result == "met" else None
        state = result
        if result == "met":
            code = observed["exit_code"] if observed else None
            if code is None:
                state = "claimed"
            elif code == 0:
                state = "verified"
            else:
                failed = failed_there(proof, observed["command"]) if observed else False
                state = "contradicted" if failed else "claimed"
        results.append(
            {
                "criterion": criterion,
                "result": result,
                "evidence": proof[:500],
                "state": state,
                "command": observed["command"][:200] if observed else "",
                "exit_code": observed["exit_code"] if observed else None,
            }
        )
    return results


def verdict(results: list[dict]) -> str:
    """verified, partial, unverified or failed, over a task's acceptance criteria."""
    if not results:
        return ""
    states = [item["state"] for item in results]
    if any(state in {"not_met", "contradicted"} for state in states):
        return "failed"
    verified = states.count("verified")
    if verified == len(states):
        return "verified"
    return "partial" if verified else "unverified"


def summarize(
    commands: Iterable[dict],
    evidence: dict | None,
    report: str,
    criteria: list[str] | None = None,
    claims: list[dict] | None = None,
) -> dict:
    """The receipts for a task: observed commands, changes and PRs, the acceptance criteria and
    what backs them, and what disagrees."""
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
            # Failing first, then passing: what a regression test for a fix shows.
            "failed_before": final["exit_code"] == 0 and any(c["exit_code"] for c in runs[:-1]),
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
    results = criteria_results(criteria or [], claims or [], commands)
    for item in results:
        if item["state"] == "contradicted":
            flags.append(
                f"Says “{item['criterion']}” is met, but `{item['command']}` failed "
                f"(exit {item['exit_code']})."
            )
        elif item["state"] == "not_met":
            flags.append(f"Not met: {item['criterion']}")
    unreported = sum(item["result"] == "not_checked" for item in results)
    if results and unreported == len(results) and not claims:
        flags.append("Did not report on its acceptance criteria.")
    if stale := evidence.get("stale"):
        flags.insert(0, stale)
    return {
        "criteria": results,
        "verdict": verdict(results),
        "stale": stale or "",
        "version": [
            {
                "branch": c.get("branch", ""),
                "commit": c["commit"][:12],
                "edited": bool(c.get("edits")),
            }
            for c in checkouts
            if c.get("commit")
        ],
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
