"""A worker's pushes, read as the shell runs them: never less refused than v0.1.42's rule refused
(tests/policy_v0142.py, frozen), the default branch's pushes refused in every shape of command
line here, and the pushes of ordinary work let through."""

import itertools
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from policy_v0142 import old_denial

from chiefofstaff import policy
from chiefofstaff.policy import permission_denial

# Pushes refused wherever they stand in a command line: to main or master, forced.
REFUSED = [
    "git push origin main",
    "git push origin master",
    "git push -f origin feat",
    "git push --force origin feat",
    "git push --force-with-lease origin wb/x",
    "git push origin +wb/x",
    "git push origin HEAD:refs/heads/master",
    "git push origin HEAD:main",
    "git push origin :main",
    "git -C repo push origin main",
    "git -c remote.origin.push=HEAD:main push origin",
    "git push --all origin",
]
# Pushes let through from a checkout on a feature branch.
ALLOWED = [
    "git push origin wb/x",
    "git push -u origin HEAD",
    "git push",
    "git push origin fix-main",
    "git push origin mainline",
    "git push origin HEAD:refs/heads/fix-main",
    "git push --tags",
    "git push origin v1.2.3",
]
# Where a push can stand: `{}` is the push.
SHAPES = [
    "{}",
    "for i in 1 2 3; do {} && break; sleep 2; done",
    "{{ {}; }} 2>&1",
    "timeout 120 {}",
    "if true; then {}; fi",
    "if false; then echo no; else {}; fi",
    "while ! {}; do sleep 1; done",
    "until {}; do sleep 1; done",
    "git commit -am fix#12 && {}",
    "cd . # the repo\n{}",
    "git rebase origin/main # update\n{}",
    "( {} )",
    "{} | tee push.log",
    "{} 2>&1 | grep -v hint",
    "nice -n 5 {}",
    "stdbuf -oL {}",
    "caffeinate -i {}",
    "env FOO=bar {}",
    "FOO=bar {}",
    "command {}",
    "exec {}",
    "time {}",
    "nohup {} &",
    "sh -c '{}'",
    "bash -lc '{}'",
    "eval '{}'",
    "{} \\\n  && echo done",
    "echo start; {}; echo end",
    "set -e\n{}",
    "cat <<EOF\nnotes\nEOF\n{}",
    "{} || true",
    "! {}",
]
# From the reviews: bypasses of earlier rules, each refused.
BYPASSES = [
    "git push origin ma''in",
    "git push origin m\\ain",
    'git push origin HEAD:refs/heads/ma""in',
    "git push origin \\\nmain",
    "git -c push.default=matching push origin",
    "git --config-env=remote.origin.push=SPEC push origin",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0=HEAD:main "
    "git push origin",
    "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push "
    "GIT_CONFIG_VALUE_0=HEAD:master; git push",
    "GIT_CONFIG_PARAMETERS=\"'remote.origin.push'='HEAD:main'\" git push origin",
    "git rebase origin/main # update\ngit push -f origin feat",
    "for i in 1 2 3; do git push origin main && break; sleep 2; done",
    "{ git push -f origin feat; } 2>&1",
    "timeout 120 git push origin main",
    "if true; then git push origin main; fi",
    "while ! git push origin main; do sleep 1; done",
    "git commit -am fix#12 && git push origin main",
    "cd repo # the repo\ngit push origin main",
    "echo '#'; git push origin main",
    "git push origin 'HEAD:refs/heads/main'",
    'git push origin "main"',
    "git push --delete origin main",
    "git push --mirror origin",
    "git -C ../repo push origin HEAD:main",
    "git push -o ci.skip origin HEAD:main",
    "git push origin refs/heads/main:refs/heads/main",
    "git push --dry-run origin HEAD:main",
    "git -C ~nosuchuser push origin main",
    "echo `git push origin main`",
    "out=`git push -f origin feat`",
    "timeout 60 git -c remote.origin.push=HEAD:main push origin",
    "if true; then git -c push.default=matching push; fi",
    # Review of 00b1a30 (Q4): what pushes main though no word of it says so plainly.
    "git push origin 'refs/heads/*:refs/heads/*'",
    "git push origin '*:*'",
    "git push --prune origin 'refs/heads/*:refs/heads/*'",
    "B=main; git push origin $B",
    'git push origin "$BRANCH"',
    "git push origin ${B}",
    "git push origin ma{i,}n",
    "git push origin main>/dev/null",
    "git push origin HEAD:main>>push.log",
    "bash -c -- 'git push origin main'",
    "sh -c -e 'git push origin main'",
    "git -c alias.p=push p origin main",
    "git -c alias.p='!git push' p origin main",
    "git --config-env=alias.p=ALIAS p origin main",
    "GIT_DIR=/x/.git git push",
    "git --git-dir /x/.git push",
    "git --git-dir=/x/.git push -u origin HEAD",
    "git --work-tree /x push",
    "export GIT_DIR=/x/.git; git push",
    'git push origin "$(echo main)"',
    "git checkout - && git push",
    # Review of 9d8e631 (V2): a switch to a new branch that may have failed (`;`, `||`, a new
    # line) leaves where it was before, and the branch it named, counting.
    "git checkout -B main origin/main; git push -u origin HEAD",
    "git checkout -b master origin/master\ngit push",
    "git checkout main; git switch -c fix || true; git push",
    "git checkout main && git switch -c fix; git push",
    "git checkout - && git switch -c fix; git push",
    # (V3) What a substitution runs, in double quotes or a heredoc that expands; the part of a
    # refspec after a substitution.
    'git push origin "$(git branch --show-current):main"',
    'git push origin "$(echo HEAD):refs/heads/main"',
    'out="$(git push origin HEAD:main)"',
    'echo "$(git push --all)"',
    'echo "`git push origin main`"',
    "cat <<EOF\n$(git push origin HEAD:main)\nEOF",
    # (V4) Redirections of a checkout; an apostrophe in a heredoc's body.
    "git checkout main 2>&1 && git push",
    "git checkout main > /dev/null && git push",
    "git checkout main 2>/dev/null && git push -u origin HEAD",
    "git checkout main\ncat > notes.md <<'EOF'\nit's done\nEOF\ngit push",
    # Found while fixing those: a substitution is a word's part; a variable's value is split
    # at its spaces, set where the line may set it again, or holds options and commands.
    "git push origin `echo main`",
    "git push origin $(echo ma)in",
    "git push origin `echo ma`in",
    "B=`echo main`; git push origin $B",
    'B="feat main"; git push origin $B',
    'R="origin main"; git push $R',
    "B=feat; read B <<< main; git push origin $B",
    "B=feat; for i in 1 2; do git push origin $B; B=main; done",
    'for b in wb/a main; do git push origin "$b"; done',
    "OPTS=-f; git push $OPTS origin wb/x",
    "X=push; git $X origin main",
    "B=feat; f() { git push origin $B; }; B=main; f",
    'git push -o ";" origin main',
    "git $(echo push) origin main",
    "cd $D && git push",
    'cd "$(git rev-parse --show-toplevel)/../other" && git push',
    # Review of b73e0a2 (2): every place a line may set a variable counts, a name made up or
    # one that stands for another makes any variable not known.
    "B=wb/x; IFS= read -r B <<< main; git push origin $B",
    "B=wb/x; builtin read B <<< main; git push origin $B",
    'B=wb/x; eval "B=main"; git push origin $B',
    'B=wb/x; N=B; eval "x; $N=main"; git push origin $B',
    "B=wb/x; declare -n R=B; R=main; git push origin $B",
    "B=wb/x; printf -v B main; git push origin $B",
    "B=(main); git push origin $B",
    "B=wb/; B+=x; git push origin $B",
    "B[0]=main; git push origin $B",
    "B=wb/x; trap 'B=main' DEBUG; git push origin $B",
    "B=wb/x; f() { B=main; }; f; git push origin $B",
    "B=wb/x; : ${B:=main}; git push origin $B",
    "B=wb/x; . ./env.sh; git push origin $B",
    'B=wb/x; read "$N" <<< main; git push origin $B',
    "B=wb/x; for B in main; do :; done; git push origin $B",
    # Review of 767e08f (B): a setter however it is spelled or put; $PWD a line sets; the top
    # of a repository after a `cd`.
    "B=feat; <<<main read B; git push origin $B",
    "B=feat; 0<<<main read B; git push origin $B",
    "B=feat; r\\ead B <<< main; git push origin $B",
    "B=feat; 're'ad B <<< main; git push origin $B",
    "B=feat; command -p read B <<< main; git push origin $B",
    "B=feat; time -p read B <<< main; git push origin $B",
    'read PWD <<< ../other; git -C "$PWD" push',
    'PWD=/x; PWD=/other; git -C "$PWD" push',
    'cd src && git -C "$(git rev-parse --show-toplevel)/../other" push',
]
# What a worker does every day, on its own branch: let through.
EVERYDAY = [
    "git fetch origin main && git rebase origin/main",
    "git commit -m 'push to main' && git push origin wb/x",
    "git push origin $(git branch --show-current)",
    "git log --oneline -1 && git push origin wb/x 2>&1",
    "git stash push -m main",
    "git status && git push -u origin HEAD",
    "uv run pytest -q && git push",
    "npm test && git push origin wb/x",
    "git push -o ci.skip origin wb/x",
    "cd ~nosuchuser && git push origin wb/x",
    "git -c push.autoSetupRemote=true push",
    "echo '# not a comment' && git push origin wb/x",
    'git push origin "$(git branch --show-current)"',
    "git push -u origin `git branch --show-current`",
    "gh pr create --base main --title 'Fix the push'",
    "git commit -m 'Never push origin main' && git push origin wb/x",
    "cat > notes.md <<'EOF'\nRemember to push\nEOF\ngit push origin wb/x",
    "git push origin wb/x > push.log 2>&1",
    "git push origin wb/x 2>/dev/null",
    "git push origin wb/x>/dev/null",
    "git -c alias.st=status st && git push origin wb/x",
    "GIT_DIR=/x/.git git push origin wb/x",
    "git push origin wb/x --tags",
    "git push origin 'refs/tags/v1.2.3'",
    'git push origin wb/x > "$LOG" 2>&1',
    'git push "$REMOTE" wb/x',
    # Review of 9d8e631 (V5): a branch the line put in a variable itself.
    "B=feat; git push origin $B",
    'BRANCH=feat && git push -u origin "$BRANCH"',
    'export B=wb/x; git push origin "$B"',
    'BRANCH=$(git branch --show-current); git push origin "$BRANCH"',
    'BRANCH="$(git branch --show-current)" && git push -u origin "$BRANCH"',
    'for b in wb/a wb/b; do git push origin "$b"; done',
    # (V4) Redirections, heredocs.
    "git push origin wb/x 2>&1 | tail -5",
    "git commit -F - <<'EOF'\nDon't push origin main yet\nEOF\ngit push origin wb/x",
    "cat > notes.md <<'EOF'\nit's done\nEOF\ngit push",
    'git commit -m "$(cat msg.txt)" && git push origin wb/x',
    "cat <<'EOF'\n$(git push)\nEOF\ngit push origin wb/x",
    "git push -o ci.skip -u origin HEAD",
    'D=src; cd "$D" && git push',
    'git push origin "$SOURCE:wb/x"',
]
# On a checkout of main itself: pushes that update no branch, or one the line made.
ON_MAIN_ALLOWED = [
    "git push --tags",
    "git push origin --tags",
    "git tag v1.2.3 && git push origin --tags",
    "git push --dry-run origin",
    "git push -n origin",
    "git switch -c fix/typo && git push -u origin HEAD",
    "git checkout -b fix/typo && git commit -am 'Fix typo' && git push -u origin HEAD",
    "git checkout -B fix/typo && git push",
    "git switch --create=fix/typo && git push origin HEAD",
    "git push origin v1.2.3",
    "git branch fix/typo && git checkout fix/typo && git push -u origin HEAD",
    "git switch -c fix && git commit -am 'Fix it' && git push -u origin HEAD",
    "git checkout -b fix &&\n  git push -u origin HEAD",
    "git switch -c fix && git checkout README.md && git push -u origin HEAD",
    # Review of 9d8e631 (V4): `2>&1` is one redirection, not a break in the `&&` chain; the last
    # of `-n`/`--no-dry-run` wins. (V2) The branch made last counts, every join `&&`.
    "git switch -c fix 2>&1 && git push -u origin HEAD",
    "git checkout main && git switch -c fix && git push -u origin HEAD",
    "git push --no-dry-run -n origin",
    "git push origin --tags 2>&1 | tail -3",
]
ON_MAIN_REFUSED = [
    "git push",
    "git push origin",
    "git push -u origin HEAD",
    "git push origin @",
    "git switch -c fix/typo && git switch main && git push",
    "git checkout -b fix/typo && git checkout - && git push",
    "git push --follow-tags",
    "git commit -am fix#12 && git push",
    "cd . # the repo\ngit push",
    "for i in 1 2; do git push && break; sleep 1; done",
    "timeout 60 git push",
    "if true; then git push; fi",
    "{ git push; } 2>&1",
    "nice -n 5 git push origin",
    "env FOO=bar git push -u origin HEAD",
    # Review of 00b1a30 (Q3): a checkout or switch that may not have switched, or not to a new
    # branch, says nothing for sure of where a bare push goes.
    "git checkout README.md && git push",
    "git checkout . && git push",
    "git switch -c wb/fix; git push -u origin HEAD",
    "git switch -c wb/fix\ngit commit -am 'Fix it'\ngit push -u origin HEAD",
    "git checkout -b wb/fix || true; git push -u origin HEAD",
    "git switch wb/fix && git push",
    "git push origin $(git branch --show-current)",
    'git push origin "$(git branch --show-current)"',
    "git push origin `git branch --show-current`",
    # Review of 9d8e631 (V4): an apostrophe in a heredoc's body is no quote; `-on` is `-o n`
    # (a push option), not a dry run; the last of `-n`/`--no-dry-run` wins.
    "git commit -F - <<'EOF'\nDon't break the build\nEOF\ngit push",
    "git push -on origin",
    "git push -n --no-dry-run origin",
    "git switch -c fix 2>&1; git push -u origin HEAD",
    # An empty variable is no word: a bare push. A redirection's file descriptor is no refspec.
    "B=; git push origin $B",
    "git push origin 2>/dev/null",
    "git push origin 2>&1",
    # Review of 767e08f (B): `unset` escaped; and a quoted `>` (found by the oracle) is no
    # redirection: what follows it in the script is read.
    "B=feat; un\\set B; git push origin $B",
    "sh -c 'echo x > log; git push'",
    "eval 'echo x > log; git push'",
]
# v0.1.42 refused these as pushes to the default branch, wrongly; still refused (never less).
OLD_FALSE_POSITIVES: set[str] = set()


def checkout(path, branch: str):
    remote = path.parent / f"{path.name}-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    git = ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "start"], check=True)
    subprocess.run([*git, "remote", "add", "origin", str(remote)], check=True)
    for folder in ("src", "docs"):
        (path / folder).mkdir()
    return path


@pytest.fixture(scope="module")
def feature(tmp_path_factory):
    return checkout(tmp_path_factory.mktemp("feature") / "repo", "wb/x")


@pytest.fixture(scope="module")
def main_checkout(tmp_path_factory):
    return checkout(tmp_path_factory.mktemp("main") / "repo", "main")


def denial(command: str, cwd) -> str | None:
    row = {"role": "worker", "slack": 0, "cwd": str(cwd)}
    request = SimpleNamespace(kind="shell", full_command_text=command)
    return permission_denial(row, request)


CORPUS = [
    *(shape.format(push) for shape in SHAPES for push in [*REFUSED, *ALLOWED]),
    *BYPASSES,
    *EVERYDAY,
    *ON_MAIN_ALLOWED,
    *ON_MAIN_REFUSED,
]


def test_the_corpus_is_large():
    assert len(set(CORPUS)) >= 150


@pytest.mark.parametrize("command", sorted(set(CORPUS)))
def test_what_v0142_refused_is_refused_still(command, feature, main_checkout):
    """The invariant: never less refused than the rule shipped in v0.1.42, from any checkout."""
    if not old_denial(command) or command in OLD_FALSE_POSITIVES:
        return
    for cwd in (feature, main_checkout):
        assert denial(command, cwd), (command, old_denial(command))


@pytest.mark.parametrize(("shape", "push"), [(shape, push) for shape in SHAPES for push in REFUSED])
def test_a_refused_push_is_refused_in_every_shape(shape, push, feature):
    command = shape.format(push)
    assert denial(command, feature), command


@pytest.mark.parametrize(("shape", "push"), [(shape, push) for shape in SHAPES for push in ALLOWED])
def test_an_allowed_push_is_allowed_in_every_shape(shape, push, feature):
    command = shape.format(push)
    assert denial(command, feature) is None, command


@pytest.mark.parametrize("command", BYPASSES)
def test_each_bypass_of_an_earlier_rule_is_refused(command, feature):
    assert denial(command, feature), command


@pytest.mark.parametrize("command", EVERYDAY)
def test_everyday_work_on_its_own_branch_is_let_through(command, feature):
    assert denial(command, feature) is None, command


@pytest.mark.parametrize("command", ON_MAIN_ALLOWED)
def test_on_main_a_push_of_tags_a_dry_run_or_a_new_branch_is_let_through(command, main_checkout):
    """Review: judged by the checkout's branch at approval time, these were refused on a
    checkout of main, though they update no branch, or one the line made first."""
    assert denial(command, main_checkout) is None, command


@pytest.mark.parametrize("command", ON_MAIN_REFUSED)
def test_on_main_a_push_naming_no_branch_is_refused(command, main_checkout):
    assert denial(command, main_checkout), command


def test_an_unknown_users_home_is_no_error(feature):
    """Review: `~nosuchuser` raised out of the check: the worker got no reason."""
    assert denial("cd ~nosuchuser && git push origin wb/x", feature) is None
    found = denial("git -C ~nosuchuser push origin main", feature)
    assert found and "default branch" in found


def test_v0142s_rule_stays_a_floor_under_the_reading(monkeypatch, feature):
    """Whatever the reading of a line misses, what v0.1.42 refused (a push it saw forcing or
    naming main) is refused still."""
    monkeypatch.setattr(policy, "pushes", lambda command, cwd="": [])
    assert "default branch" in (denial("git push origin main", feature) or "")
    assert "force push" in (denial("git push -f origin wb/x", feature) or "")


# Review of 00b1a30 (Q1): the reading's work grows with the line, never faster; what it asks git
# is asked once a folder; past what it reads it refuses, saying why.


@pytest.fixture
def work(monkeypatch):
    """How often the reading parses a command line and asks git (not how long it takes)."""
    counted = {"parse": 0, "git": 0}
    parse, run = policy.shell_commands, policy.subprocess.run

    def parsing(text):
        counted["parse"] += 1
        return parse(text)

    def running(command, *args, **kwargs):
        if command[:1] == ["git"]:
            counted["git"] += 1
        return run(command, *args, **kwargs)

    monkeypatch.setattr(policy, "shell_commands", parsing)
    monkeypatch.setattr(policy.subprocess, "run", running)
    policy.reading.cache_clear()
    yield counted
    policy.reading.cache_clear()


def spent() -> float:
    """The CPU time of this process and of the commands it ran and waited for (git): what a
    check costs, which a busy machine does not stretch as it does the time on the clock (30
    suites at once, each running the bash oracle, stretched 0.1 s checks past 1 s)."""
    times = os.times()
    return times.user + times.system + times.children_user + times.children_system


def timed(command: str, cwd) -> tuple[str | None, float]:
    started = spent()
    found = denial(command, cwd)
    return found, spent() - started


@pytest.mark.parametrize("evals", [16, 18, 20])
def test_evals_nested_past_what_is_read_are_refused_at_once(evals, work, feature):
    found, took = timed("eval " * evals + "git push origin wb/x", feature)
    assert found and "too complex" in found
    assert work["parse"] <= policy.MOST_NESTED + 1 and work["git"] == 0
    assert took < 0.5


def test_evals_within_what_is_read_are_read_once_each(work, feature):
    assert denial("eval " * 5 + "git push origin wb/x", feature) is None
    assert work["parse"] == 6 and work["git"] == 0
    assert denial("eval " * 5 + "git push", feature) is None
    assert work["git"] == 2, "its folder's branch and where it pushes, once"


def test_the_same_push_many_times_asks_git_once(work, main_checkout, feature):
    assert denial("git push; " * 2000, feature) is None
    assert work["parse"] == 1 and work["git"] == 2
    work["git"] = 0
    assert denial("git push && git push -u origin HEAD && git push origin @", feature) is None
    assert work["git"] == 2, "three pushes of one folder: its branch asked once"
    found = denial("eval git push; " * 300, main_checkout)
    assert found and "default branch" in found and work["git"] == 4


def test_a_line_the_shell_would_not_parse_is_read_loosely_in_one_pass(work, feature, main_checkout):
    assert denial("git push 'oops", feature) is None and work["git"] == 2
    assert denial("git push 'oops", main_checkout), "a bare push: its folder's branch, asked"
    assert denial("git push origin HEAD:main 'oops", feature)
    found, took = timed("git x " * 30_000 + "push origin main 'oops", feature)
    assert found and took < 0.5


@pytest.mark.parametrize("paths", [10_000, 19_000])
def test_a_long_git_add_is_let_through_at_once(paths, work, feature):
    command = "git add " + " ".join(f"src/part_{i:05d}/module.py" for i in range(paths))
    found, took = timed(command, feature)
    assert found is None and work["parse"] == work["git"] == 0 and took < 0.5


def test_a_long_line_that_pushes_is_read_in_one_pass(work, feature):
    command = "git add " + " ".join(f"f{i}.py" for i in range(10_000)) + " && git push"
    found, took = timed(command, feature)
    assert found is None and work["parse"] == 1 and work["git"] == 2 and took < 0.5


@pytest.mark.parametrize(
    "command",
    [
        "git push origin wb/x " + "x " * policy.MOST_WORDS,
        "git push origin wb/x" + " ;" * policy.MOST_TOKENS,
        "bash -c 'eval \"eval git push origin wb/x " + "y" * 100_000 + "\"'",
        "cd d; " * (policy.MOST_MOVES + 1) + "git push",
        "git -C " + "d/" * (policy.MOST_PATH // 2 + 1) + " push",
        "".join(f"export GIT_A{i}=1; " for i in range(policy.MOST_GIT_ENV + 1)) + "git push",
        "; ".join(f"git -C d{i} push" for i in range(policy.MOST_FOLDERS + 1)),
    ],
)
def test_a_line_too_much_to_read_that_pushes_is_refused_saying_why(command, work, feature):
    found, took = timed(command, feature)
    assert found and "too complex" in found and took < 0.5 and work["git"] == 0


@pytest.mark.parametrize(
    "command",
    [
        "rm " + "-r " * 30_000,
        "rm -r " * 20_000,
        "curl " * 40_000,
        "git push " * 15_000,
        "git " * 40_000 + "'",
        "gh pr " * 20_000,
        "sudo " * 40_000,
        "$(" * 40_000,
        "`" * 40_000,
        # Review of 9d8e631 (V1): `\brm\s+` matched in each `-rm`: quadratic, under the GIL.
        "rm " + "-rm " * 20_000,
        "rm " + "-arm " * 20_000 + "/",
        # Found while fixing it: a long word, many `-C`s, heredocs that never end, a variable
        # put in itself, many variables, many switches, shells that run nothing.
        "git push " + "-o=" * 30_000,
        "B=" + "x" * 1000 + "; git push origin " + "$B" * 50_000,
        "B0=x; " + "".join(f"B{i + 1}=$B{i}$B{i}; " for i in range(40)) + "git push origin $B40",
        "git -C x" * 12_000 + " push",
        "\n<<'EOF'\t" * 10_000 + "git push",
        "".join(f"A{i}=x; " for i in range(12_000)) + "git push",
        "".join(f"git checkout a{i}; git checkout -b b{i}; " for i in range(2_500)) + "git push",
        "/\\bash -cmain" * 8_000 + "\ngit push origin main",
        '"$(' * 30_000 + " git push",
        "$((" * 30_000 + " git push",
        '""' * 50_000 + " git push",
        ")[} " * 25_000 + " git push",
        "".join(f"git checkout a{i}; git push; " for i in range(5_000)),
        # Review of b73e0a2: evals whose words run to the line's end, heredocs in substitutions.
        "B=wb/x; " + "eval '" * 16_000 + " git push origin $B",
        "B=wb/x; " + "eval '" * 16_000 + " git push origin wb/x",
        "B=wb/x; " + "eval " * 20_000 + " git push origin $B",
        "<<E $(\n" * 14_000 + " git push",
        "\"$(cat <<'E'\nit's\n" * 7_000 + " git push",
    ],
)
def test_no_line_is_slow_to_check(command, feature):
    _, took = timed(command, feature)
    assert took < 0.5, (command[:20], took)


# The rules of v0.1.42 the check keeps, now read in one pass: what each says is what it said.
ALPHABET = ["git", "push", "origin", "main", "master", "-f", "--force", "+x", ":main", "rm",
            "-r", "-rf", "-x", "/", "~", "$HOME", "curl", "wget", "|", "sh", "sudo", ";", "&",
            "\n", " ", "  ", "x", "'", "--force-with-lease", "-rm", "-arm", "-R", "-Rf",
            "-fr"]  # fmt: skip


@pytest.mark.parametrize("seed", range(4))
def test_the_rules_kept_from_v0142_say_what_they_said(seed):
    import random

    from policy_v0142 import DANGEROUS as OLD

    chance = random.Random(seed)  # noqa: S311  (a fixed sample, not a secret)
    old = {reason: rule for rule, reason in OLD}
    new = {reason: found for found, reason in policy.DANGEROUS}
    for _ in range(3000):
        words = chance.choices(ALPHABET, k=chance.randint(1, 12))
        command = " ".join(words).replace("\\n", "\n")
        for reason in (
            "recursive delete of home or root",
            "piping a download into a shell",
        ):
            assert bool(old[reason].search(command)) == new[reason](command, ""), command
        assert bool(old["force push"].search(command)) == policy.floor_force(command), command
        assert bool(old["push to the default branch"].search(command)) == policy.floor_default(
            command
        ), command


# Review of 9d8e631 (V1, G3): each rule reads a line in time linear in it, whatever words it is
# made of: tried here on lines of its own words, repeated to about 100 KB.
RULE_WORDS = {
    "sudo is not allowed": ("", ["sudo", " ", "\t", ";", "(", "&", "x"]),
    "recursive delete of home or root": (
        "rm ",
        ["rm", "-rm", "-r", "-arm", "-R", "/", "~", "$HOME", "-", " "],
    ),
    "push": (
        "git push ",
        ["git", "push", "origin", "main", "-f", "+", ":", "$(", "`", '"', "'", "\\", "\n", ";",
         "&&", "|", "<<EOF", "EOF", "eval", "sh -c", "cd", "-C", "B=x", "$B", "for b in", "do",
         "checkout -b", "#", "((", "))", " "],
    ),
    "merging, closing or commenting on PRs as the owner": ("", ["gh", "pr", "merge", " ", "\t"]),
    "changing issues as the owner": ("", ["gh", "issue", "close", " "]),
    "piping a download into a shell": (
        "",
        ["curl", "wget", "|", "sh", "bash", "sudo", "x", "\n", " "],
    ),
    "system-level destructive command": ("", ["mkfs", "diskutil", "erase", "shutdown", " "]),
    "gh api": (
        "gh api ",
        ["gh", "api", "-X", "--method", "=", "-f", "graphql", "mutation", "GET", " ", "\t"],
    ),
}  # fmt: skip


def adversarial_lines():
    for name, (start, words) in RULE_WORDS.items():
        repeated = [
            *words,
            " ".join(words),
            "".join(words),
            *map("".join, itertools.pairwise(words)),
        ]
        for word in repeated:
            yield name, start + word * (100_000 // len(word))


@pytest.mark.parametrize(("rule", "command"), list(adversarial_lines()))
def test_each_rule_reads_a_line_of_its_own_words_in_linear_time(rule, command, feature):
    rules = {reason: found for found, reason in policy.DANGEROUS}
    chosen = {
        "push": [policy.too_complex, policy.pushes_forced, policy.pushes_default],
        "gh api": [lambda line, cwd: policy.gh_api_writes(line)],
    }.get(rule, [rules.get(rule)])
    for found in chosen:
        assert found is not None
        policy.reading.cache_clear()
        started = spent()
        found(command, str(feature))
        took = spent() - started
        assert took < 0.5, (rule, command[:40], took)


def test_a_line_past_the_cap_is_refused_before_any_rule_reads_it(monkeypatch, feature):
    """Review of 9d8e631 (V1): one cap, on every command line, before any rule."""
    read = []
    rules = [(lambda line, cwd, r=reason: read.append(r), reason) for _, reason in policy.DANGEROUS]
    monkeypatch.setattr(policy, "DANGEROUS", rules)
    monkeypatch.setattr(policy, "gh_api_writes", lambda line: read.append("gh api"))
    for command in ("rm " + "-rm " * 200_000, "ls " + "x" * policy.MOST_LINE):
        found = denial(command, feature)
        assert found and "too long to check" in found and not read
    assert denial("ls " + "x" * (policy.MOST_LINE - 3), feature) is None and len(read) > 1


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ('git push origin "$BRANCH"', "can't tell which branch"),
        ("git push origin $(echo ma)in", "can't tell which branch"),
        ("cd $D && git push", "can't tell which branch"),
        ("git push origin 'refs/heads/*:refs/heads/*'", "can't tell which branch"),
        ("B=main; git push origin $B", "push to the default branch"),
        ("B=`echo main`; git push origin $B", "push to the default branch"),
        ("OPTS=-f; git push $OPTS origin wb/x", "force push"),
    ],
)
def test_a_refusal_says_why(command, reason, feature):
    """Review of 9d8e631 (V5): a push whose branch the line does not say is refused for that,
    never as a push to the default branch."""
    found = denial(command, feature) or ""
    assert reason in found
    assert reason == "push to the default branch" or "default branch" not in found


@pytest.mark.parametrize(
    "command",
    [
        "git -C a\x00b push",
        "cd a\x00b && git push",
        "git -C \udcff push",
        "cd \ud800 && git push",
        "git -C 'a\x00b' push origin main",
    ],
)
def test_a_folder_git_cannot_be_asked_of_is_a_reason_not_an_error(command, feature):
    """Review of 9d8e631 (V6): a NUL or an undecodable path escaped the check as an error."""
    found = denial(command, feature)
    assert found and found.startswith("Denied: ") and policy.NOT_CHECKED not in found


def test_any_failure_to_check_refuses_saying_so(monkeypatch, feature):
    def broken(folder):
        raise RuntimeError(folder)

    monkeypatch.setattr(policy, "current_branches", broken)
    policy.reading.cache_clear()
    found = denial("git push", feature)
    assert found and f"{policy.NOT_CHECKED} (RuntimeError)" in found


# G1 and G4: on any line, what v0.1.42 refused is refused still; and every line gets a decision
# (a reason when refused), never an error, in time.
FUZZ = ["git", "push", "origin", "main", "wb/x", "-f", "+x", ":main", " ", " ", "\n", "\t", "'",
        '"', "`", "$(", ")", "(", "\\", "\x00", "\ud800", "\udcff", ";", "&&", "||", "|", "&",
        "<<EOF", "<<'E'", "EOF", "E", "#", "eval", "sh -c", "cd", "-C", "$B", "B=", "for b in",
        "do", "done", "{", "}", "$((", "))", "rm", "-rf", "/", "~", "curl", "gh api", "-X",
        "-f", "sudo", "\r", "é", "${B}", "*", "checkout", "-b", "switch", "-c", "2>&1",
        ">/dev/null", "HEAD", "--tags", "-n", "alias.p=push"]  # fmt: skip


@pytest.mark.parametrize("seed", range(4))
def test_every_line_gets_a_decision_never_less_than_v0142s(seed, monkeypatch, feature):
    import random

    monkeypatch.setattr(policy, "current_branches", lambda folder: {"wb/x"})
    chance = random.Random(seed)  # noqa: S311  (a fixed sample, not a secret)
    for _ in range(1500):
        command = "".join(chance.choices(FUZZ, k=chance.randint(0, 40)))
        policy.reading.cache_clear()
        found = denial(command, feature)
        assert found is None or (found.startswith("Denied: ") and policy.NOT_CHECKED not in found)
        assert found or not old_denial(command), command


@pytest.mark.parametrize("seed", range(3))
def test_a_giant_line_gets_a_decision_in_time(seed, monkeypatch, feature):
    import random

    monkeypatch.setattr(policy, "current_branches", lambda folder: {"wb/x"})
    chance = random.Random(seed)  # noqa: S311  (a fixed sample, not a secret)
    words = chance.choices(FUZZ, k=40)
    command = "".join(words) * (450_000 // len("".join(words)))
    found, took = timed(command, feature)
    assert (found is None or policy.NOT_CHECKED not in found) and took < 1.5, took


@pytest.mark.parametrize("seed", range(2))
def test_the_words_of_a_line_are_shlexs(seed):
    """shell_words reads as shlex.shlex(…, posix=True, punctuation_chars=SEPARATORS) with only
    " \t\r" for whitespace (what the check used before), in linear time."""
    import random
    import shlex

    def shlexs(text):
        lexer = shlex.shlex(text, posix=True, punctuation_chars=policy.SEPARATORS)
        lexer.whitespace, lexer.whitespace_split, lexer.commenters = " \t\r", True, ""
        try:
            return list(lexer)
        except ValueError as error:
            return str(error)

    def ours(text):
        try:
            return policy.shell_words(text)
        except ValueError as error:
            return str(error)

    chance = random.Random(seed)  # noqa: S311  (a fixed sample, not a secret)
    letters = [*"a b\t\r\n'\"\\;&|()`$#<>=-é\f\v\x00", "git", "  ", "\\\n", "\u2028", "\xa0"]
    for _ in range(20_000):
        text = "".join(chance.choices(letters, k=chance.randint(0, 20)))
        assert ours(text) == shlexs(text), text


def test_a_quoted_separator_is_a_word():
    assert [type(word).__name__ for word in policy.shell_words("a ';' b; c")] == [
        "str", "str", "str", "Separator", "str",
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("text", "made", "runs"),
    [
        ("echo $(git push)", "echo \x00", ["git push"]),
        ("echo `git push`", "echo \x00", ["git push"]),
        ("echo '$(git push)'", "echo '$(git push)'", []),
        ('echo "$(git push)"', 'echo "\x00"', ["git push"]),
        ("echo \\$(git push)", "echo \\$(git push)", []),
        ('echo $(echo ")")x', "echo \x00x", ['echo ")"']),
        ("a$(b $(c))d", "a\x00d", ["b $(c)"]),
        ("echo `a \\` b`", "echo \x00", ["a \\` b"]),
        ("echo $(never closed", "echo \x00", ["never closed"]),
        ('echo "it\'s $(x)"', 'echo "it\'s \x00"', ["x"]),
    ],
)
def test_a_command_substitution_is_a_made_up_part_of_its_word(text, made, runs):
    assert policy.without_substitutions(text) == (made, runs)


# Review of 9d8e631 (G2): what workers run every day gets exactly v0.1.42's decision, from a
# checkout of a feature branch and of main, but for the intended new refusals listed below.
NORMAL = [
    # git, no push
    "git status",
    "git status --short",
    "git diff",
    "git diff --stat origin/main...HEAD",
    "git log --oneline -20",
    "git log --oneline main..HEAD",
    "git fetch origin",
    "git fetch origin main",
    "git pull --rebase origin main",
    "git rebase origin/main",
    "git merge origin/main",
    "git merge --no-ff origin/main -m 'Merge main'",
    "git add -A",
    "git add .",
    "git add src tests",
    "git commit -m 'Fix the parser'",
    "git commit -am 'Update docs'",
    "git commit --amend --no-edit",
    "git stash",
    "git stash pop",
    "git stash push -m 'wip on main'",
    "git branch",
    "git branch -a",
    "git branch -d wb/old",
    "git branch -D wb/old",
    "git checkout -- README.md",
    "git restore README.md",
    "git restore --staged .",
    "git reset --hard origin/wb/x",
    "git reset HEAD~1",
    "git cherry-pick abc123",
    "git tag v1.2.3",
    "git tag -a v1.2.3 -m 'Release 1.2.3'",
    "git show HEAD",
    "git show HEAD:src/app.py",
    "git blame src/app.py",
    "git grep -n 'push origin main'",
    "git worktree list",
    "git remote -v",
    "git rev-parse HEAD",
    "git describe --tags",
    "git clean -fdx",
    "git submodule update --init --recursive",
    "git config user.email",
    "git log -1 --format=%H",
    "git diff main",
    "git diff origin/main -- src",
    "git merge-base HEAD origin/main",
    "git switch wb/x",
    "git checkout wb/x",
    "git checkout -b wb/fix-typo",
    "git switch -c wb/fix-typo",
    "git log --grep='push'",
    "git rebase --continue",
    "git rebase --abort",
    "git am < fix.patch",
    "git format-patch -1",
    "git apply fix.patch",
    "git bisect start",
    "git reflog -10",
    "git shortlog -sn",
    "git log --oneline | head -5",
    "git diff | head -100",
    "git ls-files | wc -l",
    "git stash list",
    "git fetch --all --prune",
    "git pull",
    "git pull origin main",
    # git push, in the shapes workers write
    "git push origin wb/x",
    "git push -u origin wb/x",
    "git push --set-upstream origin wb/x",
    "git push -u origin HEAD",
    "git push",
    "git push origin HEAD",
    "git push origin HEAD:wb/x",
    "git push origin HEAD:refs/heads/wb/x",
    "git push -o ci.skip origin wb/x",
    "git push --push-option=ci.skip origin wb/x",
    "git push origin wb/x 2>&1",
    "git push origin wb/x 2>&1 | tail -20",
    "git push origin wb/x > push.log 2>&1",
    "git push origin wb/x --tags",
    "git push --tags",
    "git push origin v1.2.3",
    "git push origin --tags",
    "git push --follow-tags origin wb/x",
    "git push origin --delete wb/old",
    "git push origin :wb/old",
    "git push -d origin wb/old",
    "git push --dry-run origin wb/x",
    "git push -n",
    "git push -q origin wb/x",
    "git push --verbose origin wb/x",
    "git push --no-verify origin wb/x",
    "git push origin wb/x && gh pr create --fill",
    "git push -u origin HEAD && gh pr create --title 'Fix' --body 'Fixes it'",
    "git add -A && git commit -m 'Fix' && git push",
    "git add -A && git commit -m 'Fix' && git push origin wb/x",
    "git commit -m 'push to main later' && git push origin wb/x",
    "git commit -m 'Refuse pushes to main' && git push -u origin HEAD",
    "for i in 1 2 3; do git push && break; sleep 5; done",
    "for i in 1 2 3; do git push origin wb/x && break; sleep 5; done",
    "until git push; do sleep 2; done",
    "git push || (git pull --rebase && git push)",
    "git pull --rebase origin wb/x && git push origin wb/x",
    'BRANCH=wb/x; git push origin "$BRANCH"',
    'B=$(git branch --show-current); git push -u origin "$B"',
    'git push origin "$(git branch --show-current)"',
    "git push origin `git rev-parse --abbrev-ref HEAD`",
    'git push -u origin "$(git rev-parse --abbrev-ref HEAD)"',
    "git push --force-with-lease origin wb/x",
    "git push -f origin wb/x",
    "git push origin +wb/x",
    "git push --force",
    "cd src && git push",
    "cd src && git push origin wb/x",
    "cd docs; git push origin wb/x",
    "git -C src push",
    "git -C docs push origin wb/x",
    "REMOTE=origin; git push $REMOTE wb/x",
    "export GIT_TERMINAL_PROMPT=0; git push origin wb/x",
    "GIT_TRACE=1 git push origin wb/x",
    "git -c http.postBuffer=524288000 push origin wb/x",
    "git -c core.sshCommand='ssh -i key' push origin wb/x",
    "git push origin wb/x 2>&1 | grep -v hint",
    "git push origin wb/x || echo 'push failed'",
    "timeout 120 git push origin wb/x",
    "git push origin wb/x; echo done",
    "git push origin wb/x && echo 'Pushed to wb/x'",
    "(git fetch && git rebase origin/main && git push --force-with-lease)",
    "git commit -F - <<'EOF'\nFix push to main checks\n\nDetails here.\nEOF\ngit push origin wb/x",
    "git commit -F - <<'EOF'\nDon't push straight to main\nEOF\ngit push",
    "cat > notes.md <<'EOF'\nWe push to main only through PRs.\nEOF\n"
    "git add notes.md && git commit -m notes && git push",
    "cat <<EOF > release.md\nRelease $(date +%F)\nEOF\ngit push origin wb/x",
    "git push origin wb/x\ngh pr create --fill",
    "git push origin HEAD:wb/x -o merge_request.create",
    # gh
    "gh pr create --fill",
    "gh pr create --base main --title 'Push fix' --body 'Pushes to main are refused'",
    "gh pr create --draft --title 'WIP'",
    "gh pr view 12",
    "gh pr view --web",
    "gh pr list",
    "gh pr checks",
    "gh pr checks --watch",
    "gh pr diff",
    "gh pr merge --auto --squash",
    "gh pr merge 12 --squash",
    "gh pr comment 12 --body 'Done'",
    "gh pr review --approve",
    "gh pr edit 12 --add-label bug",
    "gh pr ready",
    "gh run list",
    "gh run watch",
    "gh run view 123 --log",
    "gh issue view 7",
    "gh issue list",
    "gh issue create --title 'Bug' --body 'x'",
    "gh issue comment 7 --body 'Fixed in #12'",
    "gh api repos/o/r/pulls",
    "gh api -X GET search/issues -f q=x",
    "gh api graphql -f query='query { viewer { login } }'",
    "gh api repos/o/r/issues/1/comments -f body=x",
    "gh api -X POST repos/o/r/dispatches",
    "gh repo view",
    "gh auth status",
    "gh release list",
    # package managers, builds, tests
    "npm install",
    "npm ci",
    "npm test",
    "npm run build",
    "npm run lint -- --fix",
    "pnpm install --frozen-lockfile",
    "pnpm -r build",
    "yarn install",
    "yarn test --watch=false",
    "npx prettier --write .",
    "npm install --save-dev typescript@5",
    "npm publish --dry-run",
    "npm run test -- --runInBand",
    "npm audit fix",
    "pip install -r requirements.txt",
    "pip install -e '.[dev]'",
    "uv sync",
    "uv run pytest -q",
    "uv run pytest tests/test_app.py -k 'push and not main'",
    "python -m pytest -x",
    "python3 -c 'print(1)'",
    "uv pip install ruff",
    "ruff check . --fix",
    "mypy src",
    "pyright",
    "make",
    "make test",
    "make -j8 build",
    "cargo build --release",
    "cargo test",
    "go test ./...",
    "go build ./...",
    "gradle build",
    "./gradlew test",
    "mvn -q test",
    "dotnet test",
    "bazel test //...",
    "cmake -S . -B build && cmake --build build",
    "docker build -t app .",
    "docker compose up -d",
    "docker run --rm app pytest",
    "tox -e py311",
    "pre-commit run --all-files",
    "uv run ruff format --check .",
    # deletes
    "rm -rf node_modules",
    "rm -rf build dist",
    "rm -rf .venv",
    "rm -rf ./build",
    "rm -f *.log",
    "rm -rf node_modules && npm ci",
    "rm -r __pycache__",
    "rm -rf target/",
    "rm -rf ~/.cache/pip",
    "rm -rf /tmp/build",
    "rm -rf $HOME/.cache/x",
    "rm -rf ./dist/*",
    "rm -rf coverage .pytest_cache",
    "rm notes.md",
    "rm -i old.txt",
    "rm -rf -- build",
    "rm -fr out",
    "rm -Rf out",
    "rm -rf ../wb-old-worktree",
    "find . -name '*.pyc' -delete",
    "find build -type f -delete",
    "find . -name node_modules -prune -exec rm -rf {} +",
    "git clean -xdf && rm -rf node_modules",
    'rm -rf "$TMPDIR/cos-test"',
    "rm -rf ${BUILD_DIR:-build}",
    # downloads
    "curl -fsSL https://example.com/install.sh -o install.sh",
    "curl -s https://api.github.com/repos/o/r | jq .",
    "curl -sL https://example.com/x.tgz | tar xz",
    "wget -q https://example.com/file.tar.gz",
    "curl -fsSL https://bun.sh/install | bash",
    "curl -s localhost:3000/health",
    "curl -X POST -d '{}' localhost:3000/api",
    "wget -qO- https://example.com | head",
    # everything else
    "ls -la",
    "cat README.md",
    "sed -i '' 's/main/master/' README.md",
    "grep -rn 'git push origin main' docs",
    "echo 'git push origin main' >> notes.md",
    'echo "Run git push origin main to release"',
    "printf '%s\\n' a b",
    "jq '.version' package.json",
    "tar czf dist.tgz dist",
    "mkdir -p build && cd build && cmake ..",
    "export PATH=$HOME/.local/bin:$PATH",
    "source .venv/bin/activate",
    "chmod +x scripts/run.sh",
    "./scripts/run.sh",
    "sleep 5",
    "kill 1234",
    "ps aux | grep node",
    "lsof -i :3000",
    "which python",
    "env | sort",
    "date",
    "pwd",
    "head -50 log.txt",
    "tail -n 100 server.log",
    "wc -l src/*.py",
    "du -sh node_modules",
    "df -h",
    "xargs -n1 echo < list.txt",
    "time npm test",
    "timeout 60 npm test",
    "nohup npm run dev > dev.log 2>&1 &",
    "npm run dev &",
    'cat > config.json <<\'EOF\'\n{"branch": "main"}\nEOF',
    "cat <<EOF > notes.md\nPushed to $BRANCH\nEOF",
    "python - <<'PY'\nprint('push origin main')\nPY",
    'for f in src/*.py; do python -m py_compile "$f"; done',
    'while read -r line; do echo "$line"; done < files.txt',
    "for i in $(seq 1 3); do npm test && break; done",
    "(cd src && npm test)",
    "( git fetch && git rebase origin/main )",
    "diff -u a.txt b.txt",
    "patch -p1 < fix.patch",
    "open http://localhost:3000",
    "code .",
    "sqlite3 db.sqlite '.tables'",
    "redis-cli ping",
    "brew install jq",
    "apt-get install -y jq",
    "node -e 'console.log(1)'",
    "deno test",
    "bun test",
    "ruby -v",
    "bundle exec rspec",
    "php artisan test",
    "swift test",
    "xcodebuild test",
    "kubectl get pods",
    "terraform plan",
    "aws s3 ls",
    "ssh-keygen -t ed25519 -f key -N ''",
    "openssl rand -hex 16",
    "shasum -a 256 dist.tgz",
    "zip -r out.zip dist",
    "unzip out.zip",
    "awk '{print $1}' log.txt",
    "sort -u list.txt",
    "cut -d, -f1 data.csv",
    "tr a-z A-Z < x",
    "echo $((1 + 2))",
    "test -f x && echo yes",
    "[ -d build ] || mkdir build",
    "if [ -f package.json ]; then npm ci; fi",
    "case $1 in a) echo a;; esac",
    "set -euo pipefail",
    "trap 'rm -f tmp' EXIT",
    "alias ll='ls -la'",
    "export NODE_ENV=test",
    "unset NODE_ENV",
    "ln -s ../shared shared",
    "cp -r src dist",
    "mv old.txt new.txt",
    "touch .keep",
    "git log --format='%an pushed %s' | head",
    # Review of b73e0a2 (2): a variable set once is read, whatever else the line says of its name.
    'branch=$(git branch --show-current); git push -u origin "$branch"',
    'tag=v1.2.3; git tag "$tag" && git push origin "$tag"',
    'branch=wb/x; git commit -m "Fix branch handling" && git push origin "$branch"',
    'branch=wb/x; git commit -m "Add tests for branch names" && git push origin "$branch"',
    'B=wb/x; export PATH="$HOME/bin:$PATH"; git push origin "$B"',
    'B=wb/x; printf "%s\\n" "$B"; git push origin "$B"',
    # (3) The commit and PR messages agents write: a heredoc in a substitution, in double quotes.
    "git commit -m \"$(cat <<'EOF'\nFix the parser's edge case\nEOF\n)\" && "
    'git push -u origin "$(git branch --show-current)"',
    'B=wb/x; git commit -m "$(cat <<\'EOF\'\nIt\'s done.\nEOF\n)" && git push origin "$B"',
    'gh pr create --title "Make git push safer" --body "$(cat <<\'EOF\'\nIt\'s safer now.\nEOF\n)"',
    "git commit -m \"$(cat <<'EOF'\nIt's the parser's fix\nEOF\n)\" && git push origin wb/x",
    'git commit -m "$(cat <<\'EOF\'\nDon\'t say "main" here\nEOF\n)" && git push origin wb/x',
    "git commit -m \"$(cat <<-'EOF'\n\tIt's tabbed.\n\tEOF\n)\" && git push origin wb/x",
    'git commit -m "$(cat <<EOF\nIt\'s $(date +%F)\nEOF\n)" && git push origin wb/x',
    'git commit -m "$(cat <<"EOF"\nIt\'s quoted\nEOF\n)" && git push origin wb/x',
    "git commit -m \"$(cat <<'A'\nIt's one\nA\n)\" -m \"$(cat <<'B'\nIt's two\nB\n)\" && "
    "git push origin wb/x",
    "gh pr create --fill --body \"$(cat <<'EOF'\n## Summary\nIt's done; it's tested.\nEOF\n)\"",
    # (4) The folder a command runs in, said as a variable or a substitution.
    'cd "$(git rev-parse --show-toplevel)" && git push',
    'git -C "$PWD" push',
    'git -C "${PWD}" push',
    'cd "$(pwd)" && git push',
    'git -C "$(git rev-parse --show-toplevel)" push origin wb/x',
    # Final round: a subshell's folder (what is in one runs in order; it ends with it), a
    # file a line sources (not read, as `sh FILE` is not), `pushd`, a quoted heredoc's `$B`.
    "(cd src; git push)",
    "(cd src && git push -u origin HEAD)",
    "(cd src; npm test) && git push origin wb/x",
    'for d in src; do (cd "$d" && git push); done',
    'for d in src docs; do git -C "$d" push origin wb/x; done',
    "pushd src && git push && popd",
    "cd src || exit 1; git push",
    "source .venv/bin/activate && git push -u origin HEAD",
    ". .venv/bin/activate; git push",
    "B=wb/x; cat <<'EOF'\n$B is pushed\nEOF\ngit push origin \"$B\"",
    'case "$1" in (x) git push origin wb/x;; esac',
    "if [ -d src ]; then (cd src; git push); fi",
    "(export GIT_DIR=elsewhere; git status); git push -u origin HEAD",
    "npm run dev > /dev/null & sleep 5 && git push -u origin HEAD",
    "(cd src && git push -u origin HEAD) & wait",
    # The final review of 86c7d55: back where pushd or `cd -` left; the nvm snippet (`\.` is
    # `source`); a pipe in an `&&` chain (`a && b | c && d` is `a && (b | c) && d`).
    "pushd frontend && npm test && popd && git push",
    "cd frontend; npm test; cd -; git push",
    'export NVM_DIR="$HOME/.nvm"; [ -s "$NVM_DIR/nvm.sh" ] && \\. "$NVM_DIR/nvm.sh"; '
    "npm test && git push -u origin HEAD",
    "git switch -c fix && npm test | tail && git push -u origin HEAD",
    "npm test 2>&1 | tail -20 && git push origin wb/x",
]

# On a checkout of main, a push of the checkout's own branch (naming none, HEAD, or what
# `git branch --show-current` says) pushes main: v0.1.42 let these through, unread.
INTENDED_ON_MAIN = {
    "git push", "git push -u origin HEAD", "git push origin HEAD",
    "git push -u origin HEAD && gh pr create --title 'Fix' --body 'Fixes it'",
    "git add -A && git commit -m 'Fix' && git push",
    "git commit -m 'Refuse pushes to main' && git push -u origin HEAD",
    "for i in 1 2 3; do git push && break; sleep 5; done", "until git push; do sleep 2; done",
    "git push || (git pull --rebase && git push)",
    'B=$(git branch --show-current); git push -u origin "$B"',
    'git push origin "$(git branch --show-current)"',
    "git push origin `git rev-parse --abbrev-ref HEAD`",
    'git push -u origin "$(git rev-parse --abbrev-ref HEAD)"', "cd src && git push",
    "git -C src push", "git commit -F - <<'EOF'\nDon't push straight to main\nEOF\ngit push",
    "cat > notes.md <<'EOF'\nWe push to main only through PRs.\nEOF\n"
    "git add notes.md && git commit -m notes && git push",
    'branch=$(git branch --show-current); git push -u origin "$branch"',
    "git commit -m \"$(cat <<'EOF'\nFix the parser's edge case\nEOF\n)\" && "
    'git push -u origin "$(git branch --show-current)"',
    'cd "$(git rev-parse --show-toplevel)" && git push', 'git -C "$PWD" push',
    'git -C "${PWD}" push', 'cd "$(pwd)" && git push',
    "(cd src; git push)", "(cd src && git push -u origin HEAD)",
    'for d in src; do (cd "$d" && git push); done', "pushd src && git push && popd",
    "cd src || exit 1; git push", "source .venv/bin/activate && git push -u origin HEAD",
    ". .venv/bin/activate; git push", "if [ -d src ]; then (cd src; git push); fi",
    "(export GIT_DIR=elsewhere; git status); git push -u origin HEAD",
    "npm run dev > /dev/null & sleep 5 && git push -u origin HEAD",
    "(cd src && git push -u origin HEAD) & wait",
    "pushd frontend && npm test && popd && git push", "cd frontend; npm test; cd -; git push",
    'export NVM_DIR="$HOME/.nvm"; [ -s "$NVM_DIR/nvm.sh" ] && \\. "$NVM_DIR/nvm.sh"; '
    "npm test && git push -u origin HEAD",
}  # fmt: skip


def test_the_normal_corpus_is_large():
    assert len(set(NORMAL)) >= 250 and set(NORMAL) >= INTENDED_ON_MAIN


@pytest.mark.parametrize("command", NORMAL)
def test_normal_work_gets_v0142s_decision(command, feature, main_checkout):
    old = bool(old_denial(command))
    assert bool(denial(command, feature)) == old, command
    assert bool(denial(command, main_checkout)) == (old or command in INTENDED_ON_MAIN), command


def test_a_value_too_long_for_a_branch_is_not_put_in():
    """A variable put in itself doubles at each turn: past MOST_VALUE its word stays unread."""
    assert policy.resolved("$B:$B", lambda name: "x" * 600) == "$B:$B"
    assert policy.resolved("$B:$B", lambda name: "wb/x") == "wb/x:wb/x"


@pytest.mark.parametrize("seed", range(4))
def test_the_rm_rule_reads_as_v0142s(seed):
    """Review of 9d8e631 (V1): the `rm` rule in one pass refuses exactly what v0.1.42's
    regular expression did, flags glued or apart, an `rm` inside a flag (`-rm`) too."""
    import random

    from policy_v0142 import DANGEROUS as OLD

    rule = {reason: found for found, reason in OLD}["recursive delete of home or root"]
    words = ["rm", "-rm", "-r", "-R", "-f", "-arm", "-x", "/", "~", "$HOME", "/tmp", "~/x", "-",
             "--", "x", "rm -rf", ";"]  # fmt: skip
    gaps = [" ", "  ", "\t", "\n", ""]
    chance = random.Random(seed)  # noqa: S311  (a fixed sample, not a secret)
    for _ in range(5000):
        command = "".join(
            chance.choice(words) + chance.choice(gaps) for _ in range(chance.randint(1, 10))
        )
        assert bool(rule.search(command)) == policy.recursive_delete(command), command


@pytest.mark.parametrize(
    ("text", "kept", "bodies"),
    [
        ("cat <<EOF\nit's\nEOF\ngit push", "cat <<EOF\ngit push", ["it's\n"]),
        ("cat <<'EOF'\n$(x)\nEOF\ngit push", "cat <<'EOF'\ngit push", []),
        ("cat <<-EOF\n\tx\n\tEOF\ngit push", "cat <<-EOF\ngit push", ["\tx\n"]),
        ("cat <<EOF\ngit push", "cat <<EOF\ngit push", []),
        (
            "cat <<EOF\na\nEOF\ncat <<EOF\nb\nEOF\ngit push",
            "cat <<EOF\ncat <<EOF\ngit push",
            ["a\n", "b\n"],
        ),
        ("cat <<A <<B\na\nA\nb\nB\ngit push", "cat <<A <<B\ngit push", ["a\n", "b\n"]),
        ("echo $((1<<2))\ngit push", "echo $((1<<2))\ngit push", []),
        ("cat <<<EOF\nEOF\n", "cat <<<EOF\nEOF\n", []),
        ("echo '<<EOF'\nEOF\n", "echo '<<EOF'\nEOF\n", []),
    ],
)
def test_a_heredocs_body_is_text_up_to_its_word(text, kept, bodies):
    """A body ends at the first line after it that is its word (the same word may end two);
    one with no such line is kept, read as commands (never less refused)."""
    assert policy.without_heredocs(text) == (kept, bodies)


@pytest.mark.parametrize(
    "command",
    [
        "B=" + "x" * 1000 + "; git push origin " + "$B " * 39_000,
        "for B in "
        + " ".join(chr(97 + i) * 1000 for i in range(16))
        + "; do git push origin "
        + "$B " * 39_000
        + "; done",
        "for B in "
        + " ".join("'" + " ".join([chr(97 + i)] * 500) + "'" for i in range(16))
        + "; do git push origin "
        + "$B " * 1_000
        + "; done",
        'B="' + "a " * 500 + '"; git push origin ' + "$B " * 90,
    ],
)
def test_what_variables_make_counts_in_one_budget(command, monkeypatch, feature):
    """Review of b73e0a2 (1): a value put in many words, a loop's values each in all of them,
    values split into many words: what they make is counted, and past a budget the line is
    refused as too complex, the work bounded by the line's own words."""
    monkeypatch.setattr(policy, "current_branches", lambda folder: {"wb/x"})
    resolving, resolve = [], policy.resolved
    monkeypatch.setattr(policy, "resolved", lambda *a: resolving.append(1) or resolve(*a))
    found, took = timed(command, feature)
    assert found and "too complex" in found
    assert len(resolving) <= len(command.split()) and took < 0.5


def test_a_push_keeps_so_many_branches_at_most_but_the_default(feature):
    """Review of b73e0a2 (1): what a cached reading keeps of a push is bounded."""
    many = " ".join(f"wb/b{i}" for i in range(5_000))
    (push,) = policy.pushes(f"git push origin {many}", str(feature))
    assert push.branches is not None and len(push.branches) <= policy.MOST_BRANCHES
    (push,) = policy.pushes(f"git push origin {many} main", str(feature))
    assert push.branches is not None and "main" in push.branches
    assert "default branch" in (denial(f"git push origin {many} main", feature) or "")


@pytest.mark.parametrize(
    ("text", "name", "known"),
    [
        ('branch=$(git branch --show-current); git push -u origin "$branch"', "branch", True),
        ('branch=wb/x; git commit -m "Add tests for branch names"', "branch", True),
        ("B=x; echo read B; git branch", "B", True),
        ("B=x; cat <<'EOF'\nB\nEOF", "B", True),
        ("B=x; cat <<EOF\nB\nEOF", "B", False),
        ("B=x; IFS= read -r B", "B", False),
        ("B=x; <<<main read B", "B", False),
        ("B=x; r\\ead B", "B", False),
        ("B=x; 're'ad B", "B", False),
        ("B=x; command -p read B", "B", False),
        ("B=x; time -p read B", "B", False),
        ("B=x; un\\set B", "B", False),
        ('B=x; eval "$N=x"', "B", False),
        ("B=x; declare -n R=B", "B", False),
        ("B=x; source env.sh", "B", False),
        ('B=x; read "$N"', "B", False),
        ("B=x; IFS=/", "B", False),
        ("B=x; $CMD B", "B", False),
    ],
)
def test_a_variable_is_known_where_the_line_sets_it_once(text, name, known):
    """Review of 767e08f (B): every other word naming it reads it (`$B`) or is given to a
    command that cannot set it; anything else makes it not known."""
    assert policy.Tally(text, "/").known(name) is known


@pytest.mark.parametrize(
    ("text", "end"),
    [
        ("$(cat <<'EOF'\nit's\nEOF\n) && git push", "$(cat <<'EOF'\nit's\nEOF\n)"),
        ("$(cat <<EOF\n)\nEOF\n) x", "$(cat <<EOF\n)\nEOF\n)"),
        ("$(cat <<-E\n\tit's\n\tE\n)", "$(cat <<-E\n\tit's\n\tE\n)"),
        ("$(a <<'A' <<'B'\n'\nA\n'\nB\n)", "$(a <<'A' <<'B'\n'\nA\n'\nB\n)"),
        ("$(echo $((1<<2)))\n2\n)", "$(echo $((1<<2)))"),
        ("$((1<<2\n+1))\n2\n) x", "$((1<<2\n+1))"),
        ("$(echo \"$(echo ')')\")x", "$(echo \"$(echo ')')\")"),
        # Found running the oracle in bash 5.2: a case pattern's `)` ends no substitution.
        ("$(case x in y) :;; x) git push;; esac) z", "$(case x in y) :;; x) git push;; esac)"),
        ("$(case x in a) echo esac;; (b) (cd c);; esac) z",
         "$(case x in a) echo esac;; (b) (cd c);; esac)"),
        ("$(if true; then case x in a) :;; esac; fi) z",
         "$(if true; then case x in a) :;; esac; fi)"),
        ("$(echo case) z", "$(echo case)"),
        ("$(x=esac) z", "$(x=esac)"),
    ],
)  # fmt: skip
def test_a_substitution_ends_where_bash_ends_it(text, end):
    """Review of b73e0a2 (3): a heredoc's body in a substitution is text (an apostrophe in it
    is no quote, a `)` no end), as without_heredocs reads one outside."""
    assert policy.substitution_end(text, 2)[1] == len(end)
    quoted = f'"{end}"{text[len(end) :]}'
    assert policy.quoted_end(quoted, 0) == len(end) + 2


@pytest.mark.parametrize("unit", [":.", ":read", "+source", ":eval", ",for", "=read", "%unset"])
def test_a_line_naming_setters_is_read_in_one_pass(unit, work, feature):
    """Review of 767e08f (A): words glued to setters' names made each of them read back over
    the same growing word: quadratic. The names of a line are counted in one pass, over its
    one parse."""
    head = "B=x; git push origin $B; echo "
    command = head + unit * ((480_000 - len(head)) // len(unit))
    found, took = timed(command, feature)
    assert found is None and work["parse"] == 1 and took < 0.5, (unit, took)


@pytest.mark.parametrize(
    ("text", "kinds"),
    [
        ("B=x \"B\"=y B\\=z B=(a) B+=c", ["Assignment", "str", "str", "str", "Separator",
                                          "str", "Separator", "Assignment"]),
        ("echo 'a > b' c>d \\>e", ["str", "str", "Redirecting", "str"]),
        ('git push $R "$R" ${R}x \\$R', ["str", "str", "Splitting", "str", "Splitting", "str"]),
    ],
)  # fmt: skip
def test_the_words_say_what_the_shell_takes_them_for(text, kinds):
    """An assignment only with its name and `=` unquoted; a redirection only unquoted (a
    quoted `>` in a script was taken for one, and what followed it was lost); a variable
    unquoted is split into words."""
    assert [type(word).__name__ for word in policy.shell_words(text)] == kinds


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_what_the_check_lets_through_pushes_nothing_to_main_in_real_bash(shell, tmp_path, capsys):
    """Review of 767e08f (C): command lines of a safe vocabulary (tests/policy_oracle.py),
    generated with a fixed seed, and every line of this file, run in real bash with git a
    shim that writes down what it pushes: none the check lets through pushed main or master.
    And in zsh, as Codex runs a worker's commands (the check told so), where zsh is. The
    rates are printed (pytest -s)."""
    import policy_oracle as oracle

    if shell == "zsh" and not oracle.ZSH:
        pytest.skip("no zsh here")
    generator = oracle.Generator(0)
    cases = [generator.line() for _ in range(1000)]
    cases += [(line, start) for line in oracle.seeds() for start in ("main", "feat")]
    cases += oracle.FOUND + (oracle.FOUND_ZSH if shell == "zsh" else [])
    outcomes = oracle.run(policy, cases, tmp_path, shell=shell)
    unsound = [(outcome.start, outcome.line, sorted(outcome.targets)) for outcome in outcomes
               if outcome.unsound]  # fmt: skip
    allowed = sum(outcome.allowed for outcome in outcomes)
    with capsys.disabled():
        refused = len(outcomes) - allowed
        sys.stdout.write(f"\noracle ({shell}): {len(outcomes)} lines, {allowed} allowed, {refused} "
                         f"refused, {len(unsound)} unsound\n")  # fmt: skip
    assert not unsound, unsound[:10]


def commands_of(text: str) -> list[tuple[str, list[str]]]:
    parsed = policy.shell_commands(text)
    assert parsed is not None
    return parsed[0]


@pytest.mark.parametrize(
    ("text", "events"),
    [
        ("(cd a; b) && c", ["(", "", ")"]),
        ("( (cd a); b )", ["((", ")"]),
        ("f() { cd a; }; f", ["", "()", "", ""]),
        ("case x in (a) :;; (b) :;; esac; (c)", ["", "", "", "", "", "", "("]),
        ("case x in a|b) (c);; esac", ["", "", "(", ")"]),
        ("case x in\n a) :\n ;;\n *) (c)\nesac", ["", "", "", "", "(", ")"]),
        ("case x in a) case y in (b) :;; esac;; esac", ["", "", "", "", "", ""]),
        ("for case in a; do (b); done", ["", "", "(", ")"]),
        ("a;; b", None),
        ("case x in a) b", None),
        ("esac", None),
        ("a) b", None),
        ("case x in a) (b;; esac)", None),
        ("time case x in a) b;; esac", None),
    ],
)
def test_a_case_patterns_parentheses_are_no_subshells(text, events):
    """Found while checking the tests' mutants: a case pattern's `(` (`(a)`, `;; (b)`) was taken
    for a subshell's, its `x|y)` for one's end; the folder a subshell had came back after the
    case. Where the parentheses do not pair as bash's, None: the folder is then not known
    after a `)`."""
    found = policy.subshells(commands_of(text))
    assert (found and found[0]) == events


@pytest.mark.parametrize(
    ("text", "jobs"),
    [
        ("a && b & c", [True, True, False]),
        ("a; b & c", [False, True, False]),
        ("(a; b) && c & d", [True, True, True, False]),
        ("x && (a; b) & c", [True, True, True, False]),
        ("(a && b) & c; d", [True, True, False, False]),
        ("case x in (a) b & c;; esac; d", [False, False, True, False, False, False]),
        ("a & b & c", [True, True, False]),
        ("B=/ || if false; then a; fi & b", [True, True, True, True, False]),
        ("for case in a; do b & done; c", [False, True, False, False]),
        ("x && for case in a; do b; done & c", [True, True, True, True, False]),
        ("{ a; b; } & c", [True, True, True, False]),
    ],
)
def test_a_list_put_in_the_background_is_read_whole(text, jobs):
    """Found by the fourth 200k run: in `cd ../main && git push & …` the `cd` was taken for a
    piped command's (its own), and the push read where the line started; a list with a
    subshell in it (`x && (a; b) &`) was not seen to be in the background at all."""
    commands = commands_of(text)
    paired = policy.subshells(commands)
    assert policy.background(commands, paired and paired[2]) == jobs


@pytest.mark.parametrize(
    ("text", "alongside"),
    [
        ("case x in x) a;; esac | ( b )", [True, True, True, True]),
        ("( a; b ) | c", [True, True, False]),
        ("c | { a; b; }", [False, True, True, True]),
        ("x | while read l; do a; done; b", [False, True, True, True, False]),
        ("( a ) ; b | c", [False, False, False]),
        ("{ a; } && b | c", [False, False, False, False]),
    ],
)
def test_a_group_in_a_pipe_runs_alongside_it(text, alongside):
    """Found by the fifth 200k run: `… | ( git checkout -B main )` switched while the pushes
    piped into it ran; the switch was read as one after them."""
    commands = commands_of(text)
    paired = policy.subshells(commands)
    assert policy.in_pipes(commands, paired and paired[2]) == alongside


def test_a_cd_bash_may_refuse_leaves_the_folder_unknown(feature, main_checkout):
    """`cd A B`: bash 5 refuses it (too many arguments), bash 3.2 goes to A."""
    assert denial(f"cd {feature}; git push", main_checkout) is None
    assert denial(f"cd {feature} extra; git push", main_checkout)


@pytest.mark.parametrize(
    ("value", "made"),
    [("", ["", "", "x"]), ("a b", ["a b", "a", "b", "a", "b", "x"])],
)
def test_a_quoted_variable_is_one_word_even_empty(value, made):
    """Found by the third 200k run: `git -C "$D" push` with D empty was read as `git -C push`
    (the empty word dropped). Only what is put in unquoted is split; a word with a quoted
    part (`""$D`) stays a word when empty."""
    words = policy.shell_words('"$D" ""$D $D x')
    assert policy.expansions(words, lambda name: (value,), policy.Tally("x", "/")) == [made]


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        # Intended, conservative: what the line runs as code, or reads from a file, may set
        # any variable; a push of a variable the line set is then refused, saying why.
        ('B=wb/x; source env.sh; git push origin "$B"', False),
        ('B=wb/x; . ./env.sh; git push origin "$B"', False),
        ('B=wb/x; eval "$(ssh-agent -s)"; git push origin "$B"', False),
        ('B=wb/x; eval "$X"; git push origin "$B"', False),
        # Text that names it, given to a command that cannot set it, is only text.
        ("B=wb/x; git commit -m 'B=main later'; git push origin \"$B\"", True),
        ('B=wb/x; echo "for B in main"; git push origin "$B"', True),
        # Intended, conservative: a folder the line may or may not have gone to (code it runs,
        # a `cd` in a group or a condition, a loop's several), a remote that may split into
        # refspecs. A push naming its branch is let through (`git push origin wb/x`).
        ('eval "$(ssh-agent -s)" && git push -u origin HEAD', False),
        ('eval "$(ssh-agent -s)" && git push -u origin wb/x', True),
        ("{ cd src; git push; }", False),
        ("if [ -d src ]; then cd src; fi; git push", False),
        ('for d in src docs; do (cd "$d" && git push); done', False),
        ('for d in src docs; do git -C "$d" push; done', True),
        ("git push $REMOTE HEAD", False),
        ('git push "$REMOTE" HEAD', True),
    ],
)
def test_code_a_line_runs_makes_its_variables_unknown_text_does_not(command, allowed, feature):
    """Review of 767e08f: the intended conservative differences with v0.1.42 (which let these
    through, unread): only where the line may run code it does not show, or go to a folder
    that cannot be told from its text."""
    found = denial(command, feature)
    assert (found is None) is allowed, found
    assert allowed or "can't tell which branch" in (found or "")
