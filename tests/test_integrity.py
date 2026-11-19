"""What a shepherd fix changes (integrity.py): change classes on a corpus per ecosystem, secrets,
test identities per runner, the frozen runs' comparison and validity, all linear in time."""

import time

import pytest

from chiefofstaff import integrity
from chiefofstaff.integrity import APPROVAL, ELIGIBLE, REFUSED, Identities, Ran


def diff(path: str, removed=(), added=(), status="modified", context="", old_path="") -> str:
    head = f"diff --git a/{old_path or path} b/{path}\n"
    if status == "added":
        head += "new file mode 100644\n--- /dev/null\n"
    elif status == "deleted":
        head += "deleted file mode 100644\n"
    if status == "renamed":
        head += f"similarity index 90%\nrename from {old_path}\nrename to {path}\n"
    if status != "added":
        head += f"--- a/{old_path or path}\n"
    head += f"+++ b/{path}\n" if status != "deleted" else "+++ /dev/null\n"
    body = f"@@ -10,{len(removed) + 1} +10,{len(added) + 1} @@ {context}\n line\n"
    body += "".join(f"-{line}\n" for line in removed)
    body += "".join(f"+{line}\n" for line in added)
    return head + body


def classes(text: str, **readers):
    return integrity.classify(integrity.parse_diff(text), **readers)


def test_new_code_and_new_tests_are_eligible():
    text = diff("src/login.py", ["    return a"], ["    return a + 1"]) + diff(
        "tests/test_login.py", added=["def test_new():", "    assert login() == 2"], status="added"
    )
    found = classes(text)
    assert {item.change for item in found} == {ELIGIBLE}
    assert integrity.verdict(found) == ELIGIBLE


def test_tests_appended_to_an_existing_file_are_eligible():
    found = classes(diff("tests/test_login.py", added=["def test_more():", "    assert x"]))
    assert integrity.verdict(found) == ELIGIBLE


@pytest.mark.parametrize(
    ("text", "hint"),
    [
        # Python: a skip, an `if False:` wrapper, removed assertions, an edited test.
        (
            diff(
                "tests/test_login.py",
                ["    assert timeout() == 30"],
                ["    pytest.skip('flaky')", "    assert timeout() == 30"],
                context="def test_timeout():",
            ),
            "adds a skip",
        ),
        (
            diff(
                "tests/test_login.py",
                ["    assert timeout() == 30"],
                ["    if False:", "        assert timeout() == 30"],
                context="def test_timeout():",
            ),
            "wraps code in a condition that never holds",
        ),
        (
            diff(
                "tests/test_login.py",
                ["    assert a", "    assert b", "    assert c"],
                [],
                context="def test_timeout():",
            ),
            "removes 3 assertions",
        ),
        # JavaScript and Go and Rust: their skips.
        (
            diff(
                "src/login.test.ts",
                ["  it('times out', () => {"],
                ["  it.skip('times out', () => {"],
            ),
            "adds a skip",
        ),
        (
            diff("login_test.go", ["\trequire.Equal(t, 30, got)"], ['\tt.Skip("flaky")']),
            "adds a skip",
        ),
        (
            diff(
                "src/lib.rs",
                ["    fn times_out() {"],
                ["    #[ignore]", "    fn times_out() {"],
                context="mod tests {",
            ),
            "adds a skip",
        ),
    ],
)
def test_weakened_tests_need_approval_with_a_hint(text, hint):
    found = classes(text)
    assert integrity.verdict(found) == APPROVAL
    assert any(hint in item.hints for item in found)
    assert all(item.hunk.startswith("@@") for item in found if item.change == APPROVAL)


def test_a_test_function_outside_test_files():
    text = diff(
        "src/lib.rs",
        ["        assert_eq!(f(), 1);"],
        ["        assert_eq!(f(), 2);"],
        context="mod tests {",
    )
    assert integrity.verdict(classes(text)) == APPROVAL


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/actions/setup/action.yml",
        ".gitlab-ci.yml",
        ".circleci/config.yml",
        "Jenkinsfile",
        "pytest.ini",
        "tox.ini",
        "noxfile.py",
        "setup.cfg",
        ".coveragerc",
        "mypy.ini",
        ".flake8",
        "ruff.toml",
        "pyrightconfig.json",
        "jest.config.js",
        "vitest.config.ts",
        ".eslintrc.json",
        "eslint.config.mjs",
        "tsconfig.json",
        ".golangci.yml",
        "clippy.toml",
        ".pre-commit-config.yaml",
        "web/tests/__snapshots__/app.test.ts.snap",
        "testdata/out.golden",
    ],
)
def test_ci_test_and_lint_configuration_and_snapshots_need_approval(path):
    assert integrity.verdict(classes(diff(path, ["a"], ["b"]))) == APPROVAL


def test_pyproject_by_its_table():
    text = diff("pyproject.toml", ["addopts = '-q'"], ["addopts = '-q --deselect tests/x'"])
    old = "[project]\nname = 'x'\n" + "\n" * 7 + "[tool.pytest.ini_options]\naddopts = '-q'\n"
    new = old.replace("-q'", "-q --deselect tests/x'")
    found = classes(text, read_old=lambda p: old, read_new=lambda p: new)
    assert integrity.verdict(found) == APPROVAL
    dependencies = "[project]\n" + "\n" * 8 + "dependencies = ['a']\n"
    found = classes(
        diff("pyproject.toml", ["dependencies = ['a']"], ["dependencies = ['a', 'b']"]),
        read_old=lambda p: dependencies,
        read_new=lambda p: dependencies,
    )
    assert integrity.verdict(found) == ELIGIBLE


def test_package_json_test_scripts_and_make_targets():
    text = diff(
        "package.json", ['    "test": "vitest run"'], ['    "test": "vitest run --passWithNoTests"']
    )
    assert integrity.verdict(classes(text)) == APPROVAL
    text = diff("package.json", ['    "build": "tsc"'], ['    "build": "tsc -b"'])
    assert integrity.verdict(classes(text)) == ELIGIBLE
    makefile = "build:\n\tgo build\n" + "\n" * 5 + "test:\n\tgo test ./...\n" + "\n" * 5
    text = diff("Makefile", ["\tgo test ./..."], ["\tgo test ./... || true"])
    found = classes(text, read_old=lambda p: makefile, read_new=lambda p: makefile)
    assert integrity.verdict(found) == APPROVAL


@pytest.mark.parametrize(
    "line",
    [
        "x = 1  # noqa: E501",
        "y = f()  # type: ignore",
        "// eslint-disable-next-line",
        "// @ts-expect-error",
        "#[allow(dead_code)]",
        "x := 1 //nolint",
        '@SuppressWarnings("x")',
    ],
)
def test_suppressions_anywhere_need_approval(line):
    found = classes(diff("src/app.py", ["x = 1"], [line]))
    assert integrity.verdict(found) == APPROVAL
    assert "adds a lint or type suppression" in found[0].hints


@pytest.mark.parametrize(
    "line",
    [
        "token = 'ghp_" + "a" * 36 + "'",
        "pat = 'github_pat_" + "b" * 30 + "'",
        "key = 'AKIA" + "A" * 16 + "'",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "g = 'AIza" + "c" * 35 + "'",
        "s = 'xoxb-1234567890-abc'",
    ],
)
def test_secrets_are_refused(line):
    found = classes(diff("src/config.py", [], [line]))
    assert integrity.verdict(found) == REFUSED


def test_a_value_of_the_sessions_environment_is_refused():
    value = "s3cr3tTokenValue1234567890"
    found = integrity.classify(
        integrity.parse_diff(diff("a.py", [], [f"x = '{value}'"])),
        environment=[value, "short", "plainwordswithoutdigits"],
    )
    assert integrity.verdict(found) == REFUSED
    assert (
        integrity.secrets_in(["x = 'plainwordswithoutdigits'"], ["plainwordswithoutdigits"]) == ""
    )


def test_deleting_or_renaming_tests_needs_approval():
    assert (
        integrity.verdict(classes(diff("tests/test_a.py", ["x"], [], status="deleted"))) == APPROVAL
    )
    renamed = (
        "diff --git a/tests/test_a.py b/tests/test_b.py\nsimilarity index 100%\n"
        "rename from tests/test_a.py\nrename to tests/test_b.py\n"
    )
    assert integrity.verdict(classes(renamed)) == APPROVAL


def test_parse_diff_reads_files_hunks_and_line_numbers():
    text = diff("src/a.py", ["old"], ["new", "more"])
    [found] = integrity.parse_diff(text)
    assert (found.path, found.status) == ("src/a.py", "modified")
    [hunk] = found.hunks
    assert hunk.removed == ["old"] and hunk.added == ["new", "more"]
    assert hunk.old_lines == [11] and hunk.new_lines == [11, 12]


def test_linear_in_time():
    lines = [f"    assert value_{n} == {n}" for n in range(4000)]
    small = diff("tests/test_big.py", lines[:400], lines[:400])
    big = diff("tests/test_big.py", lines, lines)
    started = time.perf_counter()
    classes(small)
    first = time.perf_counter() - started
    started = time.perf_counter()
    classes(big)
    second = time.perf_counter() - started
    assert second < max(first, 0.002) * 40  # 10 times the input, far from 100 times the time
    hostile = "diff --git a/x b/x\n+++ b/x\n@@ -1 +1 @@\n+" + ("a" * 200_000) + "\n"
    started = time.perf_counter()
    classes(hostile)
    assert time.perf_counter() - started < 2


PYTEST_COLLECT = """tests/test_login.py::test_ok
tests/test_login.py::test_timeout
tests/test_login.py::TestA::test_b[1-x]

3 tests collected in 0.01s
"""
PYTEST_RUN = """....F
=================================== FAILURES ===================================
FAILED tests/test_login.py::test_timeout - AssertionError: assert 31 == 30
========================= 1 failed, 2 passed in 0.12s =========================
"""
PYTEST_PASS = (
    "...\n============================== 3 passed in 0.10s ===============================\n"
)


def test_pytest_identities():
    listed = integrity.test_identities("uv run pytest -q", PYTEST_COLLECT)
    assert listed.collected == {
        "tests/test_login.py::test_ok",
        "tests/test_login.py::test_timeout",
        "tests/test_login.py::TestA::test_b[1-x]",
    }
    run = integrity.test_identities("python -m pytest", PYTEST_RUN)
    assert run.failed == {"tests/test_login.py::test_timeout"}
    assert integrity.test_identities("pytest", PYTEST_PASS).failed == set()
    assert (
        integrity.collect_command("uv run pytest -q tests")
        == "uv run pytest tests --collect-only -q"
    )
    assert integrity.collect_command("pytest -qq -x") == "pytest -x --collect-only -q"


def test_go_cargo_and_vitest_identities():
    go = integrity.test_identities("go test ./...", "TestA\nTestB\nok  \texample.com/x\t0.01s\n")
    assert go.collected == {"example.com/x.TestA", "example.com/x.TestB"}
    go = integrity.test_identities(
        "go test ./...", "--- FAIL: TestA (0.00s)\nFAIL\texample.com/x\t0.1s\n"
    )
    assert go.failed == {"TestA"}
    assert integrity.collect_command("go test ./...") == "go test -list '.*' ./..."
    cargo = integrity.test_identities(
        "cargo test", "tests::a: test\ntests::b: test\n\n2 tests, 0 benchmarks\n"
    )
    assert cargo.collected == {"tests::a", "tests::b"}
    cargo = integrity.test_identities(
        "cargo test", "test tests::a ... ok\ntest tests::b ... FAILED\ntest result: FAILED.\n"
    )
    assert cargo.failed == {"tests::b"} and cargo.passed == {"tests::a"}
    vitest = integrity.test_identities("npx vitest list", "src/a.test.ts > login > times out\n")
    assert vitest.collected == {"src/a.test.ts > login > times out"}
    run = "\u00d7 src/a.test.ts > login > times out 12ms\n Test Files  1 failed (1)\n"
    assert integrity.test_identities("npx vitest run", run).failed == {
        "src/a.test.ts > login > times out"
    }


def test_other_runners_are_not_checked():
    assert integrity.test_identities("npm test", "ok") is None
    assert integrity.collect_command("make test") == ""
    compared = integrity.compare(None, None, None, None, 1, 0)
    assert compared.holds and not compared.checked
    assert compared.words == "its tests failed before and pass now; which tests ran was not checked"


def test_compare_failing_to_passing_and_a_superset():
    run0 = Identities("pytest", failed={"t::a"})
    run1 = Identities("pytest", failed=set())
    list0 = Identities("pytest", collected={"t::a", "t::b"})
    list1 = Identities("pytest", collected={"t::a", "t::b", "t::c"})
    compared = integrity.compare(run0, run1, list0, list1, 1, 0)
    assert compared.holds and compared.checked
    assert compared.words == "`a` failed before and passes now; 2 tests before, 3 with the fix"


def test_compare_finds_what_still_fails_broke_or_vanished():
    run0 = Identities("pytest", failed={"t::a"})
    still = integrity.compare(run0, Identities("pytest", failed={"t::a"}), None, None, 1, 1)
    assert not still.holds and still.still_failing == ["t::a"]
    broke = integrity.compare(run0, Identities("pytest", failed={"t::b"}), None, None, 1, 1)
    assert not broke.holds and broke.broke == ["t::b"]
    assert still.words == "`a` still fails; which tests ran was not checked"
    assert broke.words.startswith("`a` failed before and passes now; `b` fails now;")
    gone = integrity.compare(
        run0,
        Identities("pytest", failed=set()),
        Identities("pytest", collected={"t::a", "t::b"}),
        Identities("pytest", collected={"t::a"}),
        1,
        0,
    )
    assert gone.holds and gone.missing == ["t::b"]
    assert (
        gone.words
        == "`a` failed before and passes now; 1 test no longer run; 2 tests before, 1 with the fix"
    )
    many = Identities("pytest", failed={f"t::f{n}" for n in range(5)})
    fixed = integrity.compare(many, Identities("pytest", failed=set()), None, None, 1, 0)
    assert fixed.words.startswith("`f0`, `f1`, `f2` and 2 more failed before and pass now")


PLANNED = ["cd /f0 && pytest -q > /s/h0-run.txt 2>&1", "cd /f1 && pytest -q > /s/h1-run.txt 2>&1"]
TREES = {"/f0": ("h0", "h0", "", ""), "/f1": ("h1", "h1", "", "")}


def test_frozen_evidence_valid_only_on_exactly_what_was_planned():
    ran = [Ran("bash", PLANNED[0]), Ran("read_bash"), Ran("bash", PLANNED[1])]
    assert integrity.frozen_validity(PLANNED, ran, TREES) == ""
    extra = [*ran, Ran("bash", "rm -rf tests")]
    assert "something else" in integrity.frozen_validity(PLANNED, extra, TREES)
    edited = [Ran("bash", PLANNED[0]), Ran("edit"), Ran("bash", PLANNED[1])]
    assert "edit" in integrity.frozen_validity(PLANNED, edited, TREES)
    assert integrity.frozen_validity(PLANNED, ran[:1], TREES) == "not every command ran (1 of 2)"
    dirty = {**TREES, "/f1": ("h1", "h1", "", " M tests/test_a.py")}
    assert "changed files" in integrity.frozen_validity(PLANNED, ran, dirty)
    moved = {**TREES, "/f0": ("h0", "zz", "", "")}
    assert "another commit" in integrity.frozen_validity(PLANNED, ran, moved)


def test_frozen_evidence_holds_through_its_own_result_and_reading_and_in_any_order():
    """Seen live: the step's own `shepherd_result(outcome="checked")`, which it is told to call
    once it ran them, voided its frozen runs ("it also used shepherd_result in between"); so
    would saying what it is at, or reading a file. The lines run in another order, or one run
    again, are the same evidence. What types into a shell is not."""
    ran = [
        Ran("report_intent"), Ran("bash", PLANNED[1]), Ran("view"), Ran("bash", PLANNED[0]),
        Ran("bash", PLANNED[0]), Ran("shepherd_result"),
    ]  # fmt: skip
    assert integrity.frozen_validity(PLANNED, ran, TREES) == ""
    typed = [*ran, Ran("write_bash")]
    assert "write_bash" in integrity.frozen_validity(PLANNED, typed, TREES)
