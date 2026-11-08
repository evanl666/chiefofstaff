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
from datetime import datetime

# Copilot's shell tools (bash on macOS and Linux, powershell on Windows).
SHELL_TOOLS = frozenset({"bash", "shell", "powershell", "run_in_terminal"})
# The tools that read, type into or stop a command still running in one of their shells.
SHELL_READERS = frozenset({"read_bash", "write_bash", "read_powershell", "write_powershell"})
SHELL_STOPPERS = frozenset({"stop_bash", "stop_powershell"})
# A command Copilot left running (past its wait, or started in the background): its result comes
# later, from reading its shell or from the notice that it ended.
STILL_RUNNING = re.compile(
    r"with shellId: ([^\s>]+) is still running|started in background with shellId: ([^\s>]+)"
)
SHELL_DONE = re.compile(r"<shellId: ([^\s>]+) completed with exit code (-?\d+)>")

# Matched against each simple command, from the program on: `grep pytest`, `pip install jest`
# or a commit message that mentions tests are not test runs.
KINDS = [
    (
        "test",
        re.compile(
            r"^(pytest|py\.test|unittest|tox|nox|jest|vitest|mocha|rspec|phpunit|ctest|"
            r"go test|cargo (test|nextest)|mvnw?( \S+)*? (test|verify)|(li)?gradlew?( \S+)*? "
            r"(\S*:)?(test|check)\b|(npm|pnpm|yarn|bun)( \S+)*? (run )?test(:\S+)?(?=\s|$)|"
            r"make( \S+)*? (test|check)|"
            r"swift test|dotnet test|mix test|bazel test|deno test|playwright test)\b"
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
    **{key: {"run"} for key in ("uv", "poetry", "pipenv", "hatch", "pdm", "rye", "pixi")},
    **{key: {"run"} for key in ("conda", "mamba", "micromamba")},
    **{key: {"exec", "dlx"} for key in ("bundle", "npm", "pnpm", "yarn")},
}
LAUNCHERS = {"npx", "bunx", "pnpx"}
# `bash -c "pytest -q"`: the shell runs the commands in its script.
SHELLS = {"bash", "sh", "zsh", "dash"}
VALUE_OPTIONS = {
    "--directory", "--env-file", "--extra", "--from", "--group", "--index", "--only-group",
    "--package", "--project", "--python", "--with", "-C", "-c", "-p", "-n", "--name",
    "--prefix", "--environment", "--manifest-path",
}  # fmt: skip
EXIT = re.compile(r"exit(?:ed with)? code (-?\d+)", re.IGNORECASE)
SAYS_TESTS_PASS = re.compile(
    r"\b(all )?(tests?|specs?|test suite|checks?)\b[^.\n]{0,40}"
    r"\b(pass(ed|ing|es)?|green|succeed(ed)?)|\b\d+ (tests? )?passed\b",
    re.IGNORECASE,
)
# Words that are not the worker's own claim: quoted, or what someone else said or reported.
CITED = re.compile(r"“[^”\n]*”|\"[^\"\n]*\"|「[^」\n]*」|`[^`\n]*`|^\s*>.*$", re.MULTILINE)
HEARSAY = re.compile(
    r"\b(said|says|reports?|reported|according to|claims?|claimed|wrote|mentioned)\b|说|表示|提到",
    re.IGNORECASE,
)
# A claim about CI (a PR's checks), which the PR's own state shows, not about a local run.
ABOUT_CI = re.compile(r"\b(CI|checks?|workflows?|actions|pipelines?|builds? on)\b", re.IGNORECASE)


# "Tests do not pass yet", "tests never passed", "tests failed to pass".
NOT_PASSING = re.compile(
    r"\b(not|never|no longer|didn't|don't|doesn't|won't|aren't|isn't|fail(ed|s)? to|yet to)\b",
    re.IGNORECASE,
)
# The claim is about CI when CI is its subject or where it passed ("pass on CI").
CI_CLAIM = re.compile(
    r"\b(CI|checks?|workflows?|actions|pipelines?)\b|\b(on|in) (the )?(CI|GitHub|PR)\b",
    re.IGNORECASE,
)


def says_tests_pass(report: str) -> bool:
    """Whether the worker itself says its tests pass: not in a quote, not someone else's word,
    not negated, not about a PR's CI checks."""
    text = CITED.sub(" ", report or "")
    for sentence in re.split(r"(?<=[.!?;\u3002\uff01\uff1f\uff1b])\s+|\n", text):
        for match in SAYS_TESTS_PASS.finditer(sentence):
            claim = sentence[match.start() : match.end() + 16]
            if (
                not HEARSAY.search(sentence)
                and not NOT_PASSING.search(match.group(0))
                and not CI_CLAIM.search(claim)
            ):
                return True
    return False


SAYS_CHANGED = re.compile(
    r"\b(added|fixed|implemented|changed|updated|refactored|created|removed|renamed|committed)\b",
    re.IGNORECASE,
)
# A negation that governs the verb right after it: "No files were changed", "nothing was
# committed", "were not updated"; not "it did not work, so I fixed it".
NEGATED = re.compile(r"(\b(no|not|nothing|never|none|neither|nor)\b|n't)(\W+\w+){0,4}\W*$", re.I)
# Or right after it: "I changed nothing", "modified no files" (not "added no-cache headers").
NOTHING_AFTER = re.compile(r"\s+(nothing|none|no\s)", re.I)


# Changed by someone else, or before: "it was already fixed upstream", "fixed by Bob in #12".
NOT_ITS_OWN = re.compile(r"\balready\b|\bupstream\b|\bby (someone|another|other|@?[A-Z]\w+)\b")


def says_changed(report: str) -> bool:
    """Whether a report claims changes: "I added a test", not "No files were changed" or
    "it was already fixed upstream"."""
    for match in SAYS_CHANGED.finditer(report):
        clause = re.split(r"[.!?;:,\n]", report[max(0, match.start() - 80) : match.start()])[-1]
        rest = re.split(r"[.!?;:,\n]", report[match.end() : match.end() + 60])[0]
        if NEGATED.search(clause) or NOTHING_AFTER.match(report, match.end()):
            continue
        if NOT_ITS_OWN.search(clause[-20:]) or NOT_ITS_OWN.match(rest.strip()):
            continue
        if re.match(r"\s+(by|in) ", rest) and NOT_ITS_OWN.search(rest):
            continue
        return True
    return False


def drop_heredocs(command: str) -> str:
    """A command line without the bodies of its here-documents (text, not commands)."""
    lines, kept, until = command.split("\n"), [], None
    for line in lines:
        if until is not None:
            if line.strip() == until:
                until = None
            continue
        kept.append(line)
        if found := re.search(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1", line):
            until = found.group(2)
    return "\n".join(kept)


def simple_commands(command: str, depth: int = 0) -> list[list[str]]:
    """The simple commands in a shell command line, as words (quotes respected); those in a
    `bash -c "…"` script count as its own."""
    # A comment ends at its line: each line is its own command (not swallowed by one).
    lines = drop_heredocs(command.replace("\\\n", " ")).replace("\n", " ;\n; ")
    lexer = shlex.shlex(lines, posix=True, punctuation_chars=True)
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
    expanded = []
    for words in found:
        script = shell_script(words) if depth < 3 else None
        expanded += simple_commands(script, depth + 1) if script else [words] if words else []
    return expanded


def shell_script(words: list[str]) -> str | None:
    """The script of `bash -c "…"` (or `sh -lc`, `zsh -ec`): what that shell runs."""
    if len(words) < 3 or words[0].rsplit("/", 1)[-1] not in SHELLS:
        return None
    for index, word in enumerate(words[1:-1], start=1):
        if not word.startswith("-"):
            return None
        if not word.startswith("--") and "c" in word[1:]:
            return words[index + 1]
    return None


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


def test_runs(command: str) -> list[str]:
    """The test runs in a command line, from the runner on: `pytest -q` in
    `cd svc && uv run pytest -q 2>&1 | tail -5; git status`."""
    test = dict(KINDS)["test"]
    return [text for words in simple_commands(command) if test.search(text := program(words))]


def kind(command: str) -> str:
    """test, check, build, pr, push, commit or "" for a shell command line."""
    programs = [program(words) for words in simple_commands(command)]
    for name, pattern in KINDS:
        if any(pattern.search(text) for text in programs):
            return name
    return ""


# The summary lines test runners print when tests fail, as each prints them: pytest
# "1 failed, 2 passed in 0.3s" and "FAILED tests/x.py::test_y", unittest "FAILED (failures=1)",
# jest "Tests: 1 failed", vitest "Tests  1 failed | 2 passed", mocha "2 failing", go "--- FAIL:"
# and "FAIL", cargo "test result: FAILED", Gradle "BUILD FAILED", Maven "BUILD FAILURE".
TESTS_FAILED = re.compile(
    r"^=* ?(\d+ \w+, )*[1-9]\d* (failed|errors?)\b.* in [\d.]+s( \([\d:]+\))?( =+)?$"
    r"|^(FAILED|ERROR) \S+(::|\.py\b)"
    r"|^FAILED \("
    r"|^\s*Tests:?\s+(\d+ \w+, )*[1-9]\d* failed\b"
    r"|^\s*[1-9]\d* failing$"
    r"|^(--- FAIL: |FAIL(\s|$))"
    r"|^test result: FAILED"
    r"|^(\[\w+\] )?BUILD FAIL(ED|URE)\b"
    r"|^not ok \d+"
    r"|^# fail\s+[1-9]"
    r"|^(\[\w+\] )?Tests run: \d+, Failures: (\d+, Errors: [1-9]|[1-9])",
    re.MULTILINE,
)


# The summary lines of a run in which every test passed: pytest "6 passed in 0.1s" (with skipped
# and the like), unittest "OK", jest and vitest "Tests: 6 passed", go "ok  pkg", cargo
# "test result: ok", Gradle and Maven "BUILD SUCCESS(FUL)".
TESTS_PASSED = re.compile(
    r"^=* ?[1-9]\d* passed(, \d+ (skipped|deselected|xfailed|xpassed|warnings?))* in [\d.]+s"
    r"|^OK( \(|$)"
    r"|^\s*Tests:?\s+(\d+ (skipped|todo|pending), )*[1-9]\d* passed\b(?!.*failed)"
    r"|^\s*[1-9]\d* passing \("
    r"|^ok\s+\S+\s+(\(cached\)|[\d.]+s)"
    r"|^# pass\s+[1-9]\d*\s*$(?![\s\S]*^# fail\s+[1-9])"
    r"|^test result: ok\b"
    r"|^(\[\w+\] )?BUILD SUCCESS(FUL)?\b"
    r"|^(\[\w+\] )?Tests run: [1-9]\d*, Failures: 0, Errors: 0\b",
    re.MULTILINE,
)


def masked_failure(command: str, output: str) -> bool:
    """A test run piped into another command (`pytest | tail`), or followed by others
    (`pytest; git status`), exits with the last command's code: its own summary says whether
    the tests failed. The last summary counts: a command that runs the tests twice (the new test
    failing before the fix, then passing) passed."""
    if not output or ("|" not in command and len(simple_commands(command)) < 2):
        return False
    return last_summary(output) == "failed"


def last_summary(output: str) -> str:
    """ "passed" or "failed": what the last test summary in the output says ("" if none)."""
    tail = (output or "")[-4000:]
    failed = [match.start() for match in TESTS_FAILED.finditer(tail)]
    passed = [match.start() for match in TESTS_PASSED.finditer(tail)]
    if not failed and not passed:
        return ""
    return "failed" if failed and (not passed or failed[-1] > passed[-1]) else "passed"


# A test runner's own summary line: what shows a command ran tests, whatever started them
# (`./run_tests.sh`, `python tests/run.py`, a Makefile target with another name).
TEST_SUMMARY = re.compile(
    r"^=* ?(\d+ \w+, )*\d+ (passed|failed|errors?)\b.* in [\d.]+s"
    r"|^Ran \d+ tests? in [\d.]+s"
    r"|^\s*Tests:?\s+\d+ (passed|failed)\b"
    r"|^\s*\d+ passing \("
    r"|^(ok|FAIL)\s+\S+\t[\d.]+s"
    r"|^test result: (ok|FAILED)\."
    r"|^(\[\w+\] )?Tests run: \d+, Failures: \d+"
    r"|^1\.\.\d+$",
    re.MULTILINE,
)
# Programs that show or search text: a test summary in what they print is an old run's.
READERS = {
    "cat", "tail", "head", "less", "more", "grep", "egrep", "rg", "ag", "sed", "awk", "echo",
    "printf", "ls", "find", "wc", "jq", "sort", "uniq", "cut", "tr", "tee", "git", "gh", "diff",
    "bat", "nl", "column", "cd", "export", "source", ".", "set", "unset", "pushd", "popd",
    "true", "false", "sleep", "test", "[", "mkdir", "rm", "cp", "mv", "touch", "pwd", "which",
    "type", "date", "clear", "read",
}  # fmt: skip


def output_kind(command: str, output: str) -> str:
    """ "test" when a command no pattern knows printed a test runner's summary of its own."""
    runs = [program(words).split(" ", 1)[0] for words in simple_commands(command)]
    if not any(name and name not in READERS for name in runs):
        return ""
    return "test" if TEST_SUMMARY.search((output or "")[-4000:]) else ""


def test_exit(command: str, output: str, code: int | None) -> int | None:
    """Whether the tests in a command line passed (0) or failed, rather than the line: piped
    into `tail` they exit 0 even when they fail; in `pytest && git push` a rejected push exits
    1 after every test passed."""
    if code is None:
        return None
    summary = last_summary(output)
    if code == 0 and summary == "failed" and masked_failure(command, output):
        return 1
    if code != 0 and summary == "passed":
        parts = simple_commands(command)
        test = dict(KINDS)["test"]
        ran = [index for index, words in enumerate(parts) if test.search(program(words))]
        if ran and ran[-1] < len(parts) - 1:
            return 0  # A part after the tests failed, not the tests.
    return code


def running_shell(output: str) -> str:
    """The shell of a command Copilot left running, from its output ("" once it ended)."""
    found = STILL_RUNNING.search((output or "")[-600:])
    return (found.group(1) or found.group(2)) if found else ""


def shell_done(output: str) -> tuple[str, int] | None:
    """(shell, exit code) when reading a shell shows its command ended."""
    found = SHELL_DONE.findall((output or "")[-400:])
    return (found[-1][0], int(found[-1][1])) if found else None


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


def within_words(part: list[str], whole: list[str]) -> bool:
    """Whether `part` is a run of consecutive words in `whole`."""
    size = len(part)
    return bool(part) and any(whole[i : i + size] == part for i in range(len(whole) - size + 1))


def names(snippet: str, command: str) -> bool:
    """Whether a quoted command is the one that ran, word for word: it is part of it, or (from
    two words on) all of its words are in it, as `git log --stat` is in
    `git --no-pager log --oneline --stat -2`. Never by letters: `ls` is not in `tools`."""
    quoted, ran = words(snippet), words(command)
    if within_words(quoted, ran):
        return True
    return len(quoted) >= 2 and set(quoted) <= set(ran)


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
    """Whether a failed command line failed in what the evidence names. In `a && b && c` an
    exit code is the last part's that ran: a part before the last may have passed. And the
    evidence must name that part whole: `cat fin.txt` is not `cat fin.txt g.txt`, which may
    have failed on g.txt alone."""
    parts = [part for part in simple_commands(command) if part]
    if not parts:
        return True
    last = parts[-1]
    quoted = QUOTED.findall(evidence or "")
    if not quoted:
        return len(parts) <= 1
    return any(words(snippet) == last for snippet in quoted)


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


def running_now(command: dict) -> bool:
    """A command that has not ended (Copilot left it running and has not said it ended)."""
    return "ended_at" in command and command["ended_at"] is None


def after(later: str, earlier: str) -> bool:
    """Whether the time `later` (ISO) is after `earlier`; a run without a time (an old record)
    is earlier than anything."""
    if not earlier:
        return bool(later)
    try:
        return datetime.fromisoformat(later) > datetime.fromisoformat(earlier)
    except (ValueError, TypeError):
        return False


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

    ended: dict[str, str] = {}

    def last(name: str) -> dict | None:
        every = by_kind.get(name, [])
        runs = [c for c in every if c["exit_code"] is not None]
        # Copilot left it running (a long test run): its result is not in yet.
        running = bool(every) and every[-1]["exit_code"] is None and running_now(every[-1])
        if not runs:
            if not running:
                return None
            return {"command": every[-1]["command"][:200], "exit_code": None, "passed": None,
                    "runs": len(every), "failed_before": False, "running": True}  # fmt: skip
        final = runs[-1]
        ended[name] = final.get("ended_at") or ""
        return {
            "command": final["command"][:200],
            "exit_code": final["exit_code"],
            "passed": final["exit_code"] == 0,
            "runs": len(every),
            # Failing first, then passing: what a regression test for a fix shows.
            "failed_before": final["exit_code"] == 0 and any(c["exit_code"] for c in runs[:-1]),
            "running": running,
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
    prs = evidence.get("prs", [])
    if tests and tests["passed"] is False:
        # The PR's checks started after it ended, so they tested newer code: what CI says now is
        # the newer word on the tests.
        green = [
            pr["label"]
            for pr in prs
            if pr.get("checks") == "passing"
            and pr.get("state") != "CLOSED"
            and after(pr.get("checks_started", ""), ended.get("test", ""))
        ]
        tests["ci_passing"] = green
    if tests and tests["passed"] is False and tests.get("ci_passing"):
        code, where = tests["exit_code"], ", ".join(tests["ci_passing"])
        flags.append(f"The last local test run failed (exit {code}), but the checks on {where} "
                     "pass now.")  # fmt: skip
    elif tests and tests["running"] and tests["passed"] is not True:
        pass  # Its tests are running again: their result is not in yet.
    elif says_tests_pass(report or ""):
        if not tests:
            flags.append("Says tests pass, but no test run was observed.")
        elif tests["passed"] is False:
            code = tests["exit_code"]
            flags.append(f"Says tests pass, but the last test run failed (exit {code}).")
    elif tests and tests["passed"] is False:
        flags.append(f"The last test run failed (exit {tests['exit_code']}).")
    if checkouts and says_changed(report or "") and not any(changed.values()):
        flags.append("Says it made changes, but its checkouts have none.")
    for pr in prs:
        # A closed or merged PR's checks are history: nothing to fix there now.
        if pr.get("checks") == "failing" and pr.get("state") not in {"CLOSED", "MERGED"}:
            names = ", ".join(pr.get("failing") or [])
            flags.append(f"Checks are failing on {pr['label']}" + (f": {names}." if names else "."))
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


def when(item: dict, *keys: str) -> str:
    """The first of these times a check has; gh gives "0001-01-01T00:00:00Z" for none yet."""
    for key in keys:
        if (value := item.get(key) or "") and not value.startswith("0001-"):
            return value
    return ""


def latest_checks(rollup: list[dict]) -> list[dict]:
    """Each check's latest run: one that failed and passed when run again has passed."""
    latest: dict[tuple, dict] = {}
    for item in rollup:
        key = (item.get("workflowName") or "", item.get("name") or item.get("context") or "")
        stamp = when(item, "completedAt", "startedAt", "createdAt")
        seen = latest.get(key)
        if not seen or stamp >= when(seen, "completedAt", "startedAt"):
            latest[key] = item
    return list(latest.values())


def outcome(item: dict) -> str:
    return (item.get("conclusion") or item.get("state") or item.get("status") or "").upper()


FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
PENDING = {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING", ""}


# Checks that wait for a person, not for CI: "Owner Approval", "Code owner review", sign-offs.
APPROVAL = re.compile(r"approv|code.?owner|owner.?review|sign.?off|review.?required", re.I)


def check_name(item: dict) -> str:
    return item.get("name") or item.get("context") or "a check"


def awaiting_approval(rollup: list[dict]) -> list[str]:
    """The checks still waiting for someone's approval (by each check's latest run)."""
    return [
        check_name(item)
        for item in latest_checks(rollup)
        if outcome(item) in PENDING and APPROVAL.search(check_name(item))
    ]


def failing_checks(rollup: list[dict]) -> list[str]:
    """The names of the checks whose latest run failed."""
    return [
        item.get("name") or item.get("context") or "a check"
        for item in latest_checks(rollup)
        if outcome(item) in FAILED
    ]


def checks_state(rollup: list[dict]) -> str:
    """passing, failing, pending or none, from gh's statusCheckRollup, by each check's latest
    run. A check waiting for a person's approval is not CI still running (awaiting_approval
    names it)."""
    if not rollup:
        return "none"
    states = {
        outcome(item)
        for item in latest_checks(rollup)
        if not (outcome(item) in PENDING and APPROVAL.search(check_name(item)))
    }
    if not states:
        return "passing"
    if states & FAILED:
        return "failing"
    if states & PENDING:
        return "pending"
    return "passing"


def checks_started(rollup: list[dict]) -> str:
    """When the latest run of the PR's checks started (ISO, the earliest of them; "" while one
    is still running): what they tested was pushed before then."""
    latest = [
        item
        for item in latest_checks(rollup)
        if not (outcome(item) in PENDING and APPROVAL.search(check_name(item)))
    ]
    if not latest or any(outcome(item) in PENDING for item in latest):
        return ""
    return min(when(item, "startedAt", "createdAt", "completedAt") for item in latest)
