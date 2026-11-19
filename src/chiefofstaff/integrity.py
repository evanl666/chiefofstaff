"""What a shepherd fix changes, read before it is offered (shepherd.py): pure functions over the
diff from the head a step started from (H0) to the commit it made (H1), and over what the tests
said when run on those two commits as they are (frozen).

- **Change classes.** Edited or deleted lines of an existing test, test or CI configuration,
  snapshots and golden files, and lint or type suppressions anywhere need the owner's approval
  of those hunks, shown first, at every level; anything that looks like a secret is refused;
  everything else (new tests included) is eligible for its level. Hints say what was seen ("adds
  a skip"); they explain, they never decide.
- **Test identities.** Which tests a runner collected and which failed, read from its output for
  pytest, Go, cargo and vitest; any other runner's are not checked, and the plan says so.
- **Frozen-SHA evidence** is valid only if exactly the planned commands ran in the planned
  folders, with nothing else in between, and neither tree changed.

Every function here reads its input once, line by line: linear in its size."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

APPROVAL, REFUSED, ELIGIBLE = "approval", "refused", "eligible"

# Where tests live, by path.
TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec|specs|testing|test_?data|e2e|integration_tests?)/"
    r"|(?:^|/)test_[^/]*\.py$|_test\.py$|(?:^|/)conftest\.py$"
    r"|\.(?:test|spec)\.[cm]?[jt]sx?$|_test\.go$|(?:^|/)src/test/"
    r"|(?:Tests?|Spec)\.(?:java|kt|cs|swift|scala)$|_spec\.rb$",
)
# Test and CI configuration, by path: any change is the owner's to approve.
CI_PATH = re.compile(
    r"^\.github/(?:workflows|actions)/|^\.gitlab-ci\.ya?ml$|^\.circleci/|(?:^|/)Jenkinsfile$"
    r"|^azure-pipelines\.ya?ml$|^\.travis\.ya?ml$|^\.buildkite/|^bitbucket-pipelines\.yml$"
    r"|^\.pre-commit-config\.ya?ml$|^\.drone\.ya?ml$|^appveyor\.ya?ml$",
)
CONFIG_PATH = re.compile(
    r"(?:^|/)(?:pytest\.ini|tox\.ini|noxfile\.py|setup\.cfg|\.coveragerc|mypy\.ini|\.mypy\.ini"
    r"|\.flake8|\.pylintrc|pylintrc|ruff\.toml|\.ruff\.toml|pyrightconfig\.json|\.isort\.cfg"
    r"|\.golangci\.ya?ml|\.golangci\.toml|clippy\.toml|\.clippy\.toml|rustfmt\.toml|\.rustfmt\.toml"
    r"|\.rubocop\.ya?ml|codecov\.ya?ml|\.codecov\.ya?ml|\.nycrc(?:\.json)?|\.mocharc\.[a-z]+"
    r"|\.eslintrc(?:\.[a-z]+)?|\.eslintignore|\.prettierrc(?:\.[a-z]+)?|\.stylelintrc(?:\.[a-z]+)?"
    r"|tsconfig(?:\.[\w-]+)?\.json|jsconfig\.json|\.editorconfig|\.flowconfig"
    r"|(?:jest|vitest|vite|karma|playwright|cypress|eslint|ava|babel)\.config\.[a-z]+"
    r"|\.babelrc|phpunit\.xml(?:\.dist)?|\.swiftlint\.ya?ml|detekt\.ya?ml|\.scalafmt\.conf)$",
)
SNAPSHOT_PATH = re.compile(
    r"(?:^|/)__snapshots__/|\.snap$|\.snapshot$|\.golden$|(?:^|/)golden/|(?:^|/)goldens/"
    r"|(?:^|/)snapshots?/|(?:^|/)__fixtures__/|(?:^|/)testdata/",
)
# pyproject.toml and Cargo.toml sections that configure tests, coverage, lint and types.
TOOL_SECTION = re.compile(
    r"^\[+\s*(?:tool\.(?:pytest|coverage|ruff|mypy|pyright|black|isort|pylint|flake8|tox|nox|"
    r"hatch\.envs\.(?:test|lint|default)[^\]]*|basedpyright|pyre|bandit|pydocstyle|codespell)"
    r"|lints|workspace\.lints|profile\.test)\b"
)
TEST_SCRIPT = re.compile(r'^\s*"([^"]*)"\s*:', re.MULTILINE)
SCRIPT_NAME = re.compile(
    r"test|lint|check|type|coverage|cov\b|ci\b|e2e|spec|format|fmt", re.IGNORECASE
)
MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.%/-]+)\s*:(?!=)")
# Test functions (a hunk's context, or a line that opens one).
TEST_FUNCTION = re.compile(
    r"\bdef\s+test\w*|\bclass\s+Test\w*|\bfn\s+test\w*|#\[(?:tokio::)?test\]|\bmod\s+tests\b"
    r"|\bfunc\s+(?:Test|Benchmark|Fuzz|Example)\w*|\b(?:it|test|describe|context)(?:\.\w+)?\s*\("
    r"|@Test\b|\[Fact\]|\[Test\]|\[TestMethod\]",
)
# What weakens a test or a check, in added lines.
SKIP = re.compile(
    r"@pytest\.mark\.(?:skip|skipif|xfail)\b|\bpytest\.(?:skip|xfail)\(|@unittest\.skip"
    r"|\.skip\(|\b(?:xit|xdescribe|xtest|fit|fdescribe)\(|\bt\.Skip(?:Now|f)?\(|#\[ignore\]"
    r"|@(?:Ignore|Disabled)\b|\bskip\s*\(|\bpending\(",
)
SUPPRESSION = re.compile(
    r"#\s*noqa\b|#\s*type:\s*ignore|#\s*pyright:\s*ignore|#\s*pylint:\s*disable|#\s*mypy:"
    r"|eslint-disable|@ts-ignore|@ts-expect-error|@ts-nocheck|#!?\[allow\(|//\s*nolint"
    r"|NOLINT|@SuppressWarnings|rubocop:disable|#\s*nosec\b|#\s*pragma:\s*no\s*cover"
    r"|istanbul\s+ignore|c8\s+ignore|//\s*@flow\s+weak|\$FlowFixMe|\$FlowIgnore",
)
NEVER_RUN = re.compile(
    r"^\s*if\s+(?:False|0)\s*:|^\s*if\s*\(\s*(?:false|0)\s*\)|^\s*if\s+false\s*\{"
)
ASSERTION = re.compile(
    r"\bassert\w*\b|\bexpect\s*\(|\bshould\b|\.to(?:Be|Equal|Match|Throw|Contain|Have)\w*\("
    r"|\bassert_(?:eq|ne)!|\bt\.(?:Error|Fatal|Fail)\w*\(|\brequire\.\w+\(|\bself\.assert\w+\(",
)
# Things that look like secrets, in added lines.
SECRETS = (
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_\w{22,255})")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----")),
    ("a Slack token", re.compile(r"\bxox[abposr]-[0-9A-Za-z-]{10,}")),
    ("a Stripe key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{16,}")),
    ("an npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("an OpenAI key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("an Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{32,}")),
)
# A value of the session's environment worth looking for: long, and mixed like a token.
TOKEN_LIKE = re.compile(r"(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_\-+/=.]{20,}")


@dataclass
class Hunk:
    header: str
    context: str
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    old_start: int = 0
    new_start: int = 0
    old_lines: list[int] = field(default_factory=list)
    new_lines: list[int] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    old_path: str
    status: str  # added, deleted, modified, renamed
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False


HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$")


def unquote(path: str) -> str:
    path = path.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        path = path.encode("latin-1", "replace").decode("utf-8", "replace")
    return path


def parse_diff(text: str) -> list[FileDiff]:
    """`git diff --no-color --find-renames H0 H1`, file by file."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    old_line = new_line = 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = FileDiff(path="", old_path="", status="modified")
            files.append(current)
            hunk = None
            parts = line[len("diff --git ") :]
            if " b/" in parts:
                old, new = parts.split(" b/", 1)
                current.old_path, current.path = unquote(old.removeprefix("a/")), unquote(new)
            continue
        if current is None:
            continue
        if hunk is None:
            if line.startswith("new file mode"):
                current.status = "added"
            elif line.startswith("deleted file mode"):
                current.status = "deleted"
            elif line.startswith("rename from "):
                current.status, current.old_path = "renamed", unquote(line[len("rename from ") :])
            elif line.startswith("rename to "):
                current.path = unquote(line[len("rename to ") :])
            elif line.startswith("--- ") and line[4:] != "/dev/null":
                current.old_path = unquote(line[4:]).removeprefix("a/")
            elif line.startswith("+++ ") and line[4:] != "/dev/null":
                current.path = unquote(line[4:]).removeprefix("b/")
            elif line.startswith("Binary files "):
                current.binary = True
        if found := HUNK.match(line):
            old_line, new_line = int(found.group(1)), int(found.group(2))
            hunk = Hunk(header=line, context=found.group(3), old_start=old_line, new_start=new_line)
            current.hunks.append(hunk)
            continue
        if hunk is None:
            continue
        if line.startswith("-"):
            hunk.removed.append(line[1:])
            hunk.old_lines.append(old_line)
            old_line += 1
        elif line.startswith("+"):
            hunk.added.append(line[1:])
            hunk.new_lines.append(new_line)
            new_line += 1
        elif line.startswith(" "):
            old_line += 1
            new_line += 1
    for item in files:
        if not item.path:
            item.path = item.old_path
        if not item.old_path:
            item.old_path = item.path
    return files


@dataclass
class Finding:
    """One hunk (or file) and what it is."""

    path: str
    change: str  # approval | refused | eligible
    why: str
    hunk: str = ""  # its header, to show it first
    hints: list[str] = field(default_factory=list)


def section_at(text: str | None, lines: list[int], opener: re.Pattern) -> set[str]:
    """The sections (a TOML table, a make target) these line numbers (1-based) are in."""
    if not text or not lines:
        return set()
    wanted = set(lines)
    found: set[str] = set()
    current = ""
    for number, line in enumerate(text.splitlines(), 1):
        if match := opener.match(line):
            current = match.group(0)
        if number in wanted:
            found.add(current)
    return found


TOML_TABLE = re.compile(r"^\s*\[+[^\]]*\]+")


# The files whose content (not only their hunk) says whether a change configures tests or checks.
MAKEFILES = frozenset({"Makefile", "makefile", "GNUmakefile", "justfile", "Justfile",
                       "Taskfile.yml"})  # fmt: skip
TOOL_CONFIGS = frozenset({"pyproject.toml", "Cargo.toml", "package.json", *MAKEFILES})


def tool_config(path: str, hunk: Hunk, old: str | None, new: str | None) -> bool:
    """A pyproject.toml or Cargo.toml hunk in a table that configures tests, coverage, lint or
    types; a package.json hunk in its test or lint scripts; a Makefile hunk in a test target."""
    name = PurePosixPath(path).name
    if name in {"pyproject.toml", "Cargo.toml"}:
        tables = section_at(old, hunk.old_lines, TOML_TABLE) | section_at(
            new, hunk.new_lines, TOML_TABLE
        )
        return any(TOOL_SECTION.match(table) for table in tables)
    if name == "package.json":
        named = any(
            SCRIPT_NAME.search(found.group(1))
            for line in [*hunk.removed, *hunk.added]
            if (found := TEST_SCRIPT.match(line))
        )
        return (
            named
            or json_key_in(old, hunk.old_lines, "scripts")
            or json_key_in(new, hunk.new_lines, "scripts")
        )
    if name in MAKEFILES:
        targets = section_at(old, hunk.old_lines, MAKE_TARGET) | section_at(
            new, hunk.new_lines, MAKE_TARGET
        )
        return any(SCRIPT_NAME.search(target) for target in targets) or any(
            SCRIPT_NAME.search(line)
            for line in [*hunk.removed, *hunk.added]
            if MAKE_TARGET.match(line)
        )
    return False


def json_key_in(text: str | None, lines: list[int], key: str) -> bool:
    """Whether these lines of a package.json are inside its top-level `key` object (read by
    indentation and braces, line by line), on a test or lint script."""
    if not text or not lines:
        return False
    wanted = set(lines)
    inside = False
    depth = 0
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not inside and stripped.startswith(f'"{key}"') and stripped.endswith("{"):
            inside, depth = True, 1
            continue
        if inside:
            depth += stripped.count("{") - stripped.count("}")
            if depth <= 0:
                inside = False
                continue
            found = TEST_SCRIPT.match(line) if number in wanted else None
            if found and SCRIPT_NAME.search(found.group(1)):
                return True
    return False


def hints_of(hunk: Hunk) -> list[str]:
    """What a hunk does to tests, in a few words."""
    hints = []
    if any(SKIP.search(line) for line in hunk.added) and not any(
        SKIP.search(line) for line in hunk.removed
    ):
        hints.append("adds a skip")
    if any(NEVER_RUN.match(line) for line in hunk.added):
        hints.append("wraps code in a condition that never holds")
    removed = sum(1 for line in hunk.removed if ASSERTION.search(line))
    added = sum(1 for line in hunk.added if ASSERTION.search(line))
    if removed > added:
        gone = removed - added
        hints.append(f"removes {gone} assertion{'s' if gone > 1 else ''}")
    if any(SUPPRESSION.search(line) for line in hunk.added) and not any(
        SUPPRESSION.search(line) for line in hunk.removed
    ):
        hints.append("adds a lint or type suppression")
    return hints


def secrets_in(lines: Iterable[str], environment: Iterable[str] = ()) -> str:
    """What looks like a secret among these lines ("" when nothing): a known token's form, or a
    value of the session's environment that looks like a token."""
    values = [value for value in environment if TOKEN_LIKE.fullmatch(value)]
    for line in lines:
        for name, pattern in SECRETS:
            if pattern.search(line):
                return name
        for value in values:
            if value in line:
                return "a value of its environment that looks like a token"
    return ""


def classify(
    files: list[FileDiff],
    read_old: Callable[[str], str | None] = lambda path: None,
    read_new: Callable[[str], str | None] = lambda path: None,
    environment: Iterable[str] = (),
) -> list[Finding]:
    """Each changed file's hunks by class: refused (a secret), approval (with hints), eligible."""
    environment = list(environment)
    findings: list[Finding] = []
    for diff in files:
        path, old_path = diff.path, diff.old_path
        added_lines = [line for hunk in diff.hunks for line in hunk.added]
        if secret := secrets_in([path, *added_lines], environment):
            findings.append(Finding(path, REFUSED, f"looks like {secret}"))
            continue
        if diff.binary:
            kind = APPROVAL if SNAPSHOT_PATH.search(path) or TEST_PATH.search(path) else ELIGIBLE
            findings.append(Finding(path, kind, "a binary file" + (
                " among tests or snapshots" if kind == APPROVAL else "")))  # fmt: skip
            continue
        ci = CI_PATH.search(path) or CI_PATH.search(old_path)
        config = CONFIG_PATH.search(path) or CONFIG_PATH.search(old_path)
        snapshot = SNAPSHOT_PATH.search(path) or SNAPSHOT_PATH.search(old_path)
        tests = TEST_PATH.search(path) or TEST_PATH.search(old_path)
        if diff.status == "deleted" and (tests or ci or config or snapshot):
            findings.append(Finding(path, APPROVAL, "deletes a test or check file"))
            continue
        if diff.status == "renamed" and tests and not diff.hunks:
            findings.append(Finding(path, APPROVAL, f"renames a test file (from {old_path})"))
            continue
        old_text = new_text = None
        for hunk in diff.hunks:
            hints = hints_of(hunk)
            why = ""
            if ci:
                why = "changes CI configuration"
            elif config:
                why = "changes test, lint or type configuration"
            elif snapshot:
                why = "changes a snapshot or golden file"
            elif (
                hunk.removed
                and diff.status != "added"
                and (
                    tests
                    or TEST_FUNCTION.search(hunk.context)
                    or any(TEST_FUNCTION.search(line) for line in hunk.removed)
                )
            ):
                why = "changes or deletes lines of an existing test"
            elif any(SUPPRESSION.search(line) for line in hunk.added) and not any(
                SUPPRESSION.search(line) for line in hunk.removed
            ):
                why = "adds a lint or type suppression"
            elif tests and (
                any(SKIP.search(line) for line in hunk.added)
                or any(NEVER_RUN.match(line) for line in hunk.added)
            ):
                why = "skips or disables a test"
            else:
                if old_text is None and new_text is None:
                    old_text, new_text = read_old(old_path), read_new(path)
                if tool_config(path, hunk, old_text, new_text):
                    why = "changes test, lint or type configuration"
            if why:
                findings.append(Finding(path, APPROVAL, why, hunk.header, hints))
            else:
                findings.append(Finding(path, ELIGIBLE, "", hunk.header, hints))
        if not diff.hunks and diff.status in {"added", "renamed", "modified"}:
            kind = APPROVAL if (ci or config or snapshot) else ELIGIBLE
            findings.append(
                Finding(path, kind, "changes CI or test configuration" if kind == APPROVAL else "")
            )
    return findings


def verdict(findings: list[Finding]) -> str:
    """The plan's class: refused if anything is, else approval if any hunk is, else eligible."""
    if any(item.change == REFUSED for item in findings):
        return REFUSED
    if any(item.change == APPROVAL for item in findings):
        return APPROVAL
    return ELIGIBLE


# Test identities, from a runner's output.

PYTEST_ID = re.compile(r"^((?:[\w.\-/]+/)?[\w.\-]+\.py::[^\s]+)\s*$")
PYTEST_RESULT = re.compile(
    r"^(?:(FAILED|ERROR|PASSED|XFAIL|XPASS|SKIPPED)\s+([\w.\-/]+\.py::\S+)"
    r"|([\w.\-/]+\.py::\S+)\s+(FAILED|ERROR|PASSED|XFAIL|XPASS|SKIPPED))"
)
PYTEST_COLLECTED = re.compile(r"\b(\d+) (?:tests? collected|selected)\b|collected (\d+) items?")
PYTEST_SUMMARY = re.compile(r"=+ .*\b(?:passed|failed|error|no tests ran)\b.* =+$")
GO_LIST = re.compile(r"^((?:Test|Example|Fuzz|Benchmark)\w*)$")
GO_PACKAGE = re.compile(r"^(ok|FAIL|\?)\s+(\S+)")
GO_RESULT = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
CARGO_LIST = re.compile(r"^(\S+): test$")
CARGO_RESULT = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b")
CARGO_RUNNING = re.compile(r"^\s*Running (?:unittests )?(\S+)")
VITEST_LIST = re.compile(r"^(?:\[[^\]]+\]\s+)?(\S+\.(?:test|spec)\.[cm]?[jt]sx?)\s+>\s+(.+)$")
VITEST_FAIL = re.compile(
    r"^\s*(?:FAIL|\u00d7|\u2717|\u276f)\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)\s+>\s+(.+?)(?:\s+\d+ms)?$"
)
VITEST_PASS = re.compile(
    r"^\s*(?:\u2713|\u221a)\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)\s+>\s+(.+?)(?:\s+\d+ms)?$"
)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass
class Identities:
    """What one run said: the runner, the tests it collected (None when it did not list them),
    those that failed and those that passed by name (None when unknown)."""

    runner: str
    collected: set[str] | None = None
    failed: set[str] | None = None
    passed: set[str] | None = None


def runner_of(command: str) -> str:
    """Which runner a test command runs: pytest, go, cargo, vitest, or "" (not checked)."""
    words = re.split(r"[\s;&|()]+", command)
    names = {PurePosixPath(word).name for word in words if word}
    if names & {"pytest", "py.test"} or "pytest" in command.split():
        return "pytest"
    if re.search(r"(?:^|[\s;&|(])-m\s+pytest\b", command):
        return "pytest"
    if re.search(r"(?:^|[\s;&|(/])go\s+test\b", command):
        return "go"
    if re.search(r"(?:^|[\s;&|(/])cargo\s+(?:\+\S+\s+)?(?:test|nextest\s+run)\b", command):
        return "cargo"
    if "vitest" in names or re.search(r"\bvitest\b", command):
        return "vitest"
    return ""


def collect_command(command: str) -> str:
    """The command that lists the tests `command` runs, without running them ("" when its runner
    has no parser)."""
    runner = runner_of(command)
    if runner == "pytest":
        # One -q lists each test's id (two print counts per file only).
        quiet = re.sub(r"(?<!\S)(?:-q+|--quiet)(?!\S)", "", command)
        return f"{' '.join(quiet.split())} --collect-only -q"
    if runner == "go":
        return re.sub(r"\bgo\s+test\b", "go test -list '.*'", command, count=1)
    if runner == "cargo":
        return f"{command} -- --list" if " -- " not in command else f"{command} --list"
    if runner == "vitest":
        return re.sub(r"\bvitest(?:\s+run)?\b", "vitest list", command, count=1)
    return ""


def test_identities(command: str, output: str) -> Identities | None:
    """What a run of a test command said, by runner: None for a runner with no parser."""
    runner = runner_of(command)
    if not runner:
        return None
    text = ANSI.sub("", output)
    parse = {"pytest": pytest_ids, "go": go_ids, "cargo": cargo_ids, "vitest": vitest_ids}[runner]
    return parse(text)


def pytest_ids(text: str) -> Identities:
    found = Identities("pytest")
    collected: set[str] = set()
    failed: set[str] = set()
    passed: set[str] = set()
    counted = summary = False
    for raw in text.splitlines():
        line = raw.strip()
        if result := PYTEST_RESULT.match(line):
            word = result.group(1) or result.group(4)
            name = result.group(2) or result.group(3)
            (failed if word in {"FAILED", "ERROR"} else passed).add(name)
            continue
        if ident := PYTEST_ID.match(line):
            collected.add(ident.group(1))
        if PYTEST_COLLECTED.search(line):
            counted = True
        if PYTEST_SUMMARY.match(line) or re.match(r"^\d+ (?:passed|failed)", line):
            summary = True
    if collected and counted:
        found.collected = collected
    if summary:
        found.failed = failed
        found.passed = passed or None
    return found


def go_ids(text: str) -> Identities:
    found = Identities("go")
    pending: list[str] = []
    collected: set[str] = set()
    failed: set[str] = set()
    passed: set[str] = set()
    listed = results = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if name := GO_LIST.match(line):
            pending.append(name.group(1))
            continue
        if package := GO_PACKAGE.match(line):
            where = package.group(2)
            collected.update(f"{where}.{name}" for name in pending)
            listed = listed or bool(pending)
            pending = []
            results = True
            continue
        if result := GO_RESULT.match(line):
            results = True
            (failed if result.group(1) == "FAIL" else passed).add(result.group(2))
    if listed:
        found.collected = collected
    if results:
        found.failed = failed
        found.passed = passed or None
    return found


def cargo_ids(text: str) -> Identities:
    found = Identities("cargo")
    collected: set[str] = set()
    failed: set[str] = set()
    passed: set[str] = set()
    results = False
    for raw in text.splitlines():
        line = raw.strip()
        if listed := CARGO_LIST.match(line):
            collected.add(listed.group(1))
        elif result := CARGO_RESULT.match(line):
            results = True
            if result.group(2) == "FAILED":
                failed.add(result.group(1))
            elif result.group(2) == "ok":
                passed.add(result.group(1))
        elif line.startswith("test result:"):
            results = True
    if collected:
        found.collected = collected
    if results:
        found.failed = failed
        found.passed = passed or None
    return found


def vitest_ids(text: str) -> Identities:
    found = Identities("vitest")
    collected: set[str] = set()
    failed: set[str] = set()
    passed: set[str] = set()
    results = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if fail := VITEST_FAIL.match(line):
            results = True
            failed.add(f"{fail.group(1)} > {fail.group(2).strip()}")
        elif ok := VITEST_PASS.match(line):
            results = True
            passed.add(f"{ok.group(1)} > {ok.group(2).strip()}")
        elif listed := VITEST_LIST.match(line.strip()):
            collected.add(f"{listed.group(1)} > {listed.group(2).strip()}")
        elif re.match(r"^\s*Tests?\s+\d+", line) or re.match(r"^\s*Test Files\s+\d+", line):
            results = True
    if collected:
        found.collected = collected
    if results:
        found.failed = failed
        found.passed = passed or None
    return found


@dataclass
class Comparison:
    """What the frozen runs show: whether the fix holds, and the evidence in words."""

    holds: bool
    checked: bool  # identities compared (a parser, and collections that ran)
    words: str
    failing_fixed: list[str] = field(default_factory=list)
    still_failing: list[str] = field(default_factory=list)
    broke: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    before: int = 0
    after: int = 0


def compare(
    run0: Identities | None,
    run1: Identities | None,
    list0: Identities | None,
    list1: Identities | None,
    exit0: int | None,
    exit1: int | None,
) -> Comparison:
    """The same command on H0 and H1 (and its collection on each): the tests failing at H0 pass
    at H1, none that passed at H0 fails at H1, and every test collected at H0 is collected at H1
    (missing or renamed ones are the owner's to approve, never refused)."""
    if run0 is None or run1 is None:
        holds = exit1 == 0 and exit0 not in (None, 0)
        words = (
            "its tests failed before and pass now; which tests ran was not checked"
            if holds
            else "its tests still fail with the fix"
            if exit1 not in (None, 0)
            else "which tests ran was not checked"
        )
        return Comparison(holds=exit1 == 0, checked=False, words=words)
    failed0 = run0.failed or set()
    failed1 = run1.failed or set()
    fixed = sorted(failed0 - failed1)
    still = sorted(failed0 & failed1)
    broke = sorted(failed1 - failed0)
    collected0 = (list0.collected if list0 else None) or run0.collected
    collected1 = (list1.collected if list1 else None) or run1.collected
    checked = collected0 is not None and collected1 is not None and run1.failed is not None
    missing = sorted((collected0 or set()) - (collected1 or set())) if checked else []
    added = sorted((collected1 or set()) - (collected0 or set())) if checked else []
    holds = exit1 == 0 and not still and not broke and run1.failed is not None
    parts = []
    if fixed:
        parts.append(
            f"{named(fixed)} failed before and {'passes' if len(fixed) == 1 else 'pass'} now"
        )
    if still:
        parts.append(f"{named(still)} still {'fails' if len(still) == 1 else 'fail'}")
    if broke:
        parts.append(f"{named(broke)} {'fails' if len(broke) == 1 else 'fail'} now")
    if missing:
        parts.append(f"{len(missing)} {'test' if len(missing) == 1 else 'tests'} no longer run")
    if checked:
        count0, count1 = len(collected0 or ()), len(collected1 or ())
        parts.append(f"{count0} tests ran on both" if count0 == count1 and not missing
                     else f"{count0} tests before, {count1} with the fix")  # fmt: skip
    else:
        parts.append("which tests ran was not checked")
    return Comparison(
        holds=holds, checked=checked, words="; ".join(parts) or "no failing tests named",
        failing_fixed=fixed, still_failing=still, broke=broke, missing=missing, added=added,
        before=len(collected0 or ()), after=len(collected1 or ()),
    )  # fmt: skip


def named(names: list[str]) -> str:
    """Up to three tests by name (in backticks), and how many more."""
    shown = ", ".join(f"`{short_test(name)}`" for name in names[:3])
    return shown + (f" and {len(names) - 3} more" if len(names) > 3 else "")


def short_test(name: str) -> str:
    """A test's name without its file and parameters' noise: `test_login_timeout`."""
    tail = re.split(r"::| > |\.(?=Test)", name)[-1]
    return tail[:80]


# Frozen-SHA evidence: valid only on exactly what was planned.


@dataclass
class Ran:
    """A step's command or edit, as its receipts have it, in order."""

    tool: str  # bash, edit, create…
    command: str = ""
    exit_code: int | None = None
    output: str = ""


def frozen_validity(
    planned: list[str], ran: list[Ran], trees: dict[str, tuple[str, str, str, str]]
) -> str:
    """Why the frozen-SHA evidence is void ("" when valid): `planned`, the command lines asked
    for, in order; `ran`, every command and edit the session made since it was asked; `trees`,
    per frozen folder, (HEAD before, HEAD after, tracked changes before, after) as Chief of
    Staff read them."""
    commands = [item for item in ran if item.tool in {"bash", "shell", "zsh"}]
    others = [
        item for item in ran if item.tool not in {"bash", "shell", "zsh", "read_bash", "stop_bash"}
    ]
    if others:
        return f"it also used {others[0].tool} in between"
    if [item.command.strip() for item in commands] != [line.strip() for line in planned]:
        extra = next(
            (
                item.command
                for item in commands
                if item.command.strip() not in {p.strip() for p in planned}
            ),
            "",
        )
        return f"it ran something else too: {extra[:120]}" if extra else "not every command ran"
    for folder, (head0, head1, status0, status1) in trees.items():
        if head0 != head1:
            return f"{folder} moved to another commit"
        if status0 or status1:
            return f"{folder} has changed files"
    return ""
