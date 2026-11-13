"""A worker's pushes, read as the shell runs them: never less refused than v0.1.42's rule refused
(tests/policy_v0142.py, frozen), the default branch's pushes refused in every shape of command
line here, and the pushes of ordinary work let through."""

import subprocess
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
