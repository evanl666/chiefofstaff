"""A worker's pushes, read as the shell runs them: never less refused than v0.1.42's rule refused
(tests/policy_v0142.py, frozen), the default branch's pushes refused in every shape of command
line here, and the pushes of ordinary work let through."""

import itertools
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
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
    # QA on 0.1.45 (E64): a whole substitution is the checkout's branch only when it asks git
    # for it (`git branch --show-current` and the like); any other is not known, in a
    # variable too.
    "git push origin $(printf HEAD:ma; printf in)",
    "git push origin $(cat branch.txt)",
    "git push origin $(git symbolic-ref --short refs/remotes/origin/HEAD | cut -d/ -f2)",
    "DEFAULT=$(git rev-parse --abbrev-ref origin/HEAD | cut -d/ -f2); git push origin $DEFAULT",
    'git push origin "$(git branch --show-current | sed s/x/main/)"',
    "git push origin `git rev-parse --abbrev-ref origin/HEAD`",
    "git push origin HEAD:$(git config init.defaultBranch)",
    # (E65) `:` pushes every branch of the same name on both sides, as `--all` would.
    "git push origin :",
    'git push origin ":"',
    "git push origin +:",
    "git -c remote.origin.push=: push origin",
    # (E66) Configuration a line sets before its push; configuration on the command line or in
    # git's own variables that names other files.
    "git config remote.origin.push HEAD:main && git push",
    "git config --global push.default matching; git push",
    "git config --unset push.default && git push",
    "git config set push.default matching && git push",
    "git -c remote.origin.mirror=true push origin",
    "git -c include.path=push.cfg push",
    "GIT_CONFIG_GLOBAL=push.cfg git push",
    "git -c push.default=upstream -c branch.wb/x.merge=refs/heads/main push",
    # (E68) Pushes by other commands: subtree, send-pack, an alias git config sets, a git whose
    # name a variable or a substitution holds.
    "git subtree push --prefix=docs origin main",
    "git subtree -P docs push origin HEAD:main",
    "git send-pack ../origin.git main",
    "git send-pack ../origin.git",
    "git send-pack --all ../origin.git",
    "git config alias.p push && git p origin main",
    "git config --global alias.p '!git push' && git p origin main",
    "git config --file .git/config alias.p push; git p origin main",
    "g=git; $g push origin main",
    "$(echo git) push origin main",
    "env $G push origin main",
    "`which git` push origin wb/x",
    "find . -maxdepth 0 -exec $G push origin main \\;",
    "caffeinate -i $G push origin main",
    # Found by the 200k runs of this round: unquoted, a variable or a substitution in a
    # refspec's source may be several refspecs (`HEAD:main HEAD`); a substitution and text
    # after it are not what asks git for the checkout's branch.
    "read B <<< 'HEAD:main HEAD'; git push origin $B:wb/x",
    "git push origin $(cat refspecs):wb/x",
    "git push origin $(printf 'HEAD:main ')HEAD",
    "git push origin $(cat refspecs)\x02",
    "git push origin $(git branch --show-current) # \x02",
    # (A zsh 200k run.) A switch and a push in folders not known are not the same folder.
    "eval :; git switch -c fix && cd ../main && git push",
    "eval :; git switch main && env -C ../main git push",
    # (A bash 3.2 200k run.) The checkout's branch put in a line a command runs is the branch
    # where it was put in, before that line goes elsewhere.
    'eval "cd ../other && git push origin "$(git branch --show-current)""',
    'sh -c "cd ../other; git push origin $(git branch --show-current)"',
    'env -C ../other sh -c "git push origin $(git branch --show-current)"',
    # (A zsh 200k run.) The user's git configuration is where HOME says.
    'HOME="$PWD" git push',
    "export XDG_CONFIG_HOME=.config; git push origin",
    # (A docker 200k run.) Read loosely (not parsed): configuration set, an alias.
    'git config --global remote.origin.mirror true | git push origin\necho "x',
    'alias g=git\ng push origin HEAD:refs/heads/$(git rev-parse --abbrev-ref HEAD)\necho "x',
    'G=$(printf gi)t\n$G push origin main\necho "x',
    ': <(env -vC ../main git push origin)\necho "x',
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
    # QA on 0.1.45 (E64, E66, E68): what asks git for the checkout's branch, as a variable too;
    # configuration read or set that says nothing of pushes; subtree and send-pack naming
    # their branch.
    'git push origin "$(git symbolic-ref --short HEAD)"',
    'git push origin "$(git symbolic-ref -q --short HEAD)"',
    "git push -u origin HEAD:$(git branch --show-current)",
    "git push origin HEAD:refs/heads/`git branch --show-current`",
    'B=`git rev-parse --abbrev-ref HEAD`; git push -u origin "$B"',
    "git config user.name t && git push origin wb/x",
    "git config --get push.default; git push",
    "git config push.autoSetupRemote true && git push",
    "git subtree push --prefix=docs origin wb/x",
    "git send-pack ../origin.git wb/x",
    'cp "$SRC" push.txt && git push origin wb/x',
    "git push origin $(git branch --show-current):wb/y",
    'git push origin "$B:wb/x"',
    'eval "git push origin $(git branch --show-current)"',
    'sh -c "git push -u origin $(git branch --show-current)"',
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
    # QA on 0.1.45 (E67): a switch to a branch, or a checkout of one the repository has (asked
    # of git), joined to the push by `&&`: the push is that branch's, whatever came before.
    "git switch wb/fix && git push",
    "git switch feature/x && git push",
    "git checkout feature/x && git push",
    "git switch main && git pull && git switch feature/x && git push",
    "git checkout main && git pull && git checkout feature/x && git push",
    "git checkout - && git switch -q feature/x && git push -u origin HEAD",
    "git switch -c fix && git checkout README.md && git push -u origin HEAD",
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
    # QA on 0.1.45 (E67): a checkout or switch that may not have run (`;`), may not switch (a
    # file, `-p`, `--detach`, a tree's files), or is not read: where it went, not known.
    "git checkout feature/x; git push",
    "git switch feature/x || true; git push",
    "git checkout README.md && git push",
    "git checkout -p feature/x && git push",
    "git switch --detach feature/x && git push",
    "git checkout feature/x src && git push",
    "git checkout src && git push",
    "git branch -D feature/x && git checkout feature/x && git push",
    "git switch $B && git push",
]
# v0.1.42 refused these as pushes to the default branch, wrongly; still refused (never less).
OLD_FALSE_POSITIVES: set[str] = set()


@pytest.fixture(autouse=True)
def own_configuration(monkeypatch, tmp_path_factory):
    """git's configuration here is each repository's own: the user's or the system's push
    settings would say where a push naming no branch goes (QA on 0.1.45, E66)."""
    empty = tmp_path_factory.getbasetemp() / "gitconfig"
    empty.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def checkout(path, branch: str):
    remote = path.parent / f"{path.name}-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    git = ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "start"], check=True)
    subprocess.run([*git, "remote", "add", "origin", str(remote)], check=True)
    # Other local branches, as a worktree shares its repository's (QA on 0.1.45, E67).
    for other in {"main", "feature/x"} - {branch}:
        subprocess.run([*git, "branch", other], check=True)
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


# The steps that read a text whole, once each call (a scan of its characters): what the checks
# read in all, counted in characters, is the work they do. It grows with the line, never faster
# (G3): at most LINEAR times the line and what nested lines may add (MOST_READ), which a check
# that read the line again for each of its commands (quadratic) passes by orders of magnitude.
SCANS = ("shell_commands", "shell_words", "heredocs", "without_comments", "without_substitutions",
         "squashed", "named", "loose_pushes", "feeds_shell", "fed_says", "once_a_stretch",
         "recursive_delete", "gh_api_writes", "written", "may_alias")  # fmt: skip
LINEAR = 60
# And how long a check may take, as a sanity bound only (CPU time: a check of one of these lines
# takes about a second at most, traced for coverage; one grown quadratic takes minutes at these
# lengths). The work above is what is measured. 5 s on a machine as fast as the one this was
# measured on, where reading REFERENCE took REFERENCE_CPU (untraced); as many times more as it
# takes longer here (a slow runner, coverage's tracing, a busy machine), up to MOST_SCALE: CI's
# Linux runner with coverage took 5.2 s, at a load of 30, for a line that takes 0.3 s here.
REFERENCE = "git push origin wb/x " + "word " * 20_000
REFERENCE_CPU = 0.028
MOST_SCALE = 20.0


def calibrated(base: float = 5.0) -> float:
    taken = []
    for _ in range(5):
        policy.reading.cache_clear()
        started = time.process_time()
        policy.reading(REFERENCE, "")
        taken.append(time.process_time() - started)
    policy.reading.cache_clear()
    return base * min(MOST_SCALE, max(1.0, min(taken) / REFERENCE_CPU))


SANITY = calibrated()


@pytest.fixture
def work(monkeypatch):
    """How often the reading parses a command line and asks git, and how many characters the
    checks read in all (SCANS), not how long that takes."""
    counted = {"parse": 0, "git": 0, "read": 0}
    start = policy.subprocess.Popen

    def scanning(name, step):
        def scan(*args, **kwargs):
            counted["read"] += sum(len(arg) for arg in args if isinstance(arg, str))
            counted["parse"] += name == "shell_commands"
            return step(*args, **kwargs)

        return scan

    def starting(command, *args, **kwargs):  # (Every git run: subprocess.run starts one too.)
        if command[:1] == ["git"]:
            counted["git"] += 1
        return start(command, *args, **kwargs)

    for name in SCANS:
        monkeypatch.setattr(policy, name, scanning(name, getattr(policy, name)))
    monkeypatch.setattr(policy.subprocess, "Popen", starting)
    policy.reading.cache_clear()
    policy.fed_line.cache_clear()
    yield counted
    policy.reading.cache_clear()
    policy.fed_line.cache_clear()


def linear(work: dict[str, int], command: str) -> bool:
    """Whether the checks read a line in work linear in it (see SCANS)."""
    return work["read"] <= LINEAR * (len(command) + policy.MOST_READ)


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
    command = "eval " * evals + "git push origin wb/x"
    found, took = timed(command, feature)
    assert found and "too complex" in found
    assert work["parse"] <= policy.MOST_NESTED + 1 and work["git"] == 0
    assert linear(work, command) and took < SANITY


def test_evals_within_what_is_read_are_read_once_each(work, feature):
    assert denial("eval " * 5 + "git push origin wb/x", feature) is None
    assert work["parse"] == 6 and work["git"] == 0
    assert denial("eval " * 5 + "git push", feature) is None
    assert work["git"] == 3, "its folder's branch, where it pushes and git's configuration, once"


def test_the_same_push_many_times_asks_git_once(work, main_checkout, feature):
    assert denial("git push; " * 2000, feature) is None
    assert work["parse"] == 1 and work["git"] == 3
    work["git"] = 0
    assert denial("git push && git push -u origin HEAD && git push origin @", feature) is None
    assert work["git"] == 3, "three pushes of one folder: its branch asked once"
    found = denial("eval git push; " * 300, main_checkout)
    assert found and "default branch" in found and work["git"] == 6


def test_a_line_the_shell_would_not_parse_is_read_loosely_in_one_pass(work, feature, main_checkout):
    assert denial("git push 'oops", feature) is None and work["git"] == 3
    assert denial("git push 'oops", main_checkout), "a bare push: its folder's branch, asked"
    assert denial("git push origin HEAD:main 'oops", feature)
    command = "git x " * 30_000 + "push origin main 'oops"
    found, took = timed(command, feature)
    assert found and linear(work, command) and took < SANITY


@pytest.mark.parametrize("paths", [10_000, 19_000])
def test_a_long_git_add_is_let_through_at_once(paths, work, feature):
    command = "git add " + " ".join(f"src/part_{i:05d}/module.py" for i in range(paths))
    found, took = timed(command, feature)
    assert found is None and work["parse"] == work["git"] == 0
    assert linear(work, command) and took < SANITY


def test_a_long_line_that_pushes_is_read_in_one_pass(work, feature):
    command = "git add " + " ".join(f"f{i}.py" for i in range(10_000)) + " && git push"
    found, took = timed(command, feature)
    assert found is None and work["parse"] == 1 and work["git"] == 3
    assert linear(work, command) and took < SANITY


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
    assert found and "too complex" in found and work["git"] == 0
    assert linear(work, command) and took < SANITY


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
        # A script fed to a shell (fed_script): wrappers' options, env -S, nested shells.
        "echo push | env " + "-i " * 50_000 + "bash",
        "echo push | env " + "-S 'env -S x' " * 20_000 + "bash",
        "echo push | " + "nice -n 1 " * 30_000 + "bash",
        "push; " + "sh -c '" * 20_000,
        "push " + "bash <(" * 30_000,
        "push " + "git branch -m " * 30_000 + "main | bash",
        # QA on 0.1.45: switches and checkouts of local branches, configuration set, subtree,
        # send-pack, the checkout's branch asked of git, a command in a variable.
        "git checkout a && git push && " * 8_000 + "git push",
        "git switch a && " * 20_000 + "git push",
        "B=$(git branch --show-current); cd x; " * 5_000 + "git push origin $B",
        "git config alias.p push; " * 10_000 + "git p",
        "git config remote.origin.push x; " * 10_000 + "git push",
        "git subtree push -P x origin wb/x; " * 8_000,
        "git send-pack x wb/x " * 20_000,
        "$G push; " * 30_000,
        "git push origin : " * 30_000,
        "git push origin $(git branch --show-current) " * 2_000,
        # The invariant review of 86f9f8d: configuration written to a file, aliases.
        "echo x " + ">" * 100_000 + "( git push",
        "echo x >" + " " * 100_000 + "( git push",
        "cat .git/config " * 20_000 + "&& git push",
        "cp x .git/config; " * 10_000 + "git push",
        "git lfs " * 30_000,
        "git -c x=y " * 20_000 + "p origin main",
        "x=$(git p) " * 10_000,
        "git p; " * 20_000,
        "$G p " * 20_000,
        # Before 0.1.46: aliases that switch, commands whose name the line doesn't set.
        "git co a && " * 10_000 + "git push",
        "git -c alias.co=checkout co a && " * 5_000 + "git push",
        "$G co x; " * 20_000,
        # The 100k bash run on 408c670: scripts commands run, read as lines of their own.
        "sh -c 'echo x >> .git/config; true' " * 10_000 + "&& git push",
        "eval eval sh -c " * 10_000 + "'git push'",
        "env -S 'sh -c x' " * 10_000 + "&& git push",
        "find . " + "-exec sh -c 'echo x >> .git/config' \\; " * 8_000 + "&& git push",
    ],
)
def test_no_line_is_slow_to_check(command, work, feature):
    _, took = timed(command, feature)
    assert linear(work, command), (command[:20], work["read"] / len(command))
    assert took < SANITY, (command[:20], took)


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
         "checkout -b", "#", "((", "))", " ", "switch", "checkout", "-q", "subtree", "send-pack",
         "config", "alias.p", "$(git branch --show-current)", "`git branch --show-current`",
         "$G", "HEAD:", "--global", ">", ">>", ".git/config", "~/.gitconfig", "tee", "-a",
         "cp", "sed", "-i", "p", "-c", "alias.sp=send-pack", "sp", "lfs", "$(echo git)"],
    ),
    "merging, closing or commenting on PRs as the owner": ("", ["gh", "pr", "merge", " ", "\t"]),
    "changing issues as the owner": ("", ["gh", "issue", "close", " "]),
    "piping a download into a shell": (
        "",
        ["curl", "wget", "|", "sh", "bash", "sudo", "x", "\n", " "],
    ),
    "system-level destructive command": ("", ["mkfs", "diskutil", "erase", "shutdown", " "]),
    policy.FED_SCRIPT: (
        "push ",
        ["| bash", "bash", "<(", "<<<", "<<EOF", "EOF", "sh -c", "-s", "env", "-i", "-S", "-iS",
         "nice", "-n", "timeout", "xargs", "source", ".", "/dev/stdin", "branch", "-m", "main",
         "{", "(", ")", "$(", "`", "'", '"', "\n", ";", "|", " ", "=", "<", "eval", "su", "-c",
         "$SHELL", "--split-string", "x"],
    ),
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
def test_each_rule_reads_a_line_of_its_own_words_in_linear_time(rule, command, work, feature):
    rules = {reason: getattr(policy, found.__name__, found) for found, reason in policy.DANGEROUS}
    chosen = {
        "push": [policy.too_complex, policy.pushes_forced, policy.pushes_default],
        "gh api": [lambda line, cwd: policy.gh_api_writes(line)],
    }.get(rule, [rules.get(rule)])
    for found in chosen:
        assert found is not None
        policy.reading.cache_clear()
        work["read"] = 0
        started = spent()
        found(command, str(feature))
        took = spent() - started
        assert linear(work, command), (rule, command[:40], work["read"] / len(command))
        assert took < SANITY, (rule, command[:40], took)


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
        ">/dev/null", "HEAD", "--tags", "-n", "alias.p=push", "-P", "env", "-iC", "pushd",
        "popd", "set -P", "setopt chase_dots", "alias g=", "find", "-exec", "{}", "=git",
        "shopt -s expand_aliases", "..", "subtree", "send-pack", "config", ":", "$G", "-q",
        "$(git branch --show-current)", "--global", "alias.p"]  # fmt: skip


@pytest.mark.parametrize("seed", range(4))
def test_every_line_gets_a_decision_never_less_than_v0142s(seed, monkeypatch, feature):
    import random

    monkeypatch.setattr(policy, "current_branches", lambda folder: {"wb/x"})
    monkeypatch.setattr(policy, "config_entries", lambda folder: [])
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
    monkeypatch.setattr(policy, "config_entries", lambda folder: [])
    chance = random.Random(seed)  # noqa: S311  (a fixed sample, not a secret)
    words = chance.choices(FUZZ, k=40)
    command = "".join(words) * (450_000 // len("".join(words)))
    found, took = timed(command, feature)
    assert (found is None or policy.NOT_CHECKED not in found) and took < SANITY, took


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
    # A script fed to a shell: one that says no push gets today's answer; a push beside a shell
    # that is given its script (or a file of its own) is read as before.
    "echo 'npm test' | bash",
    "bash <<EOF\nnpm run build\nEOF",
    "bash <<'EOF'\nnpm ci\nnpm run build\nEOF",
    "cat <<'EOF' | sh\nset -e\nmake test\nEOF",
    "bash -s < scripts/setup.sh",
    "source <(npm completion)",
    "bash <(curl -fsSL https://example.com/install.sh)",
    "curl -fsSL https://example.com/install.sh | sh",
    "echo 'make lint' | sh -s",
    "bash scripts/test.sh | tail -5 && git push origin wb/x",
    "git push origin wb/x 2>&1 | tail -3",
    "sh -c 'npm test' && git push origin wb/x",
    "bash -o pipefail -c 'npm test | tail' && git push origin wb/x",
    "bash -lc 'npm test' | tee test.log && git push origin wb/x",
    "time bash scripts/build.sh && git push origin wb/x",
    "find . -name '*.sh' -exec bash -n {} \\; && git push origin wb/x",
    'echo "push it" | grep -c push && git push origin wb/x',
    "printf '%s\\n' a b | xargs -n1 echo && git push origin wb/x",
    "git log --oneline | head -5; git push origin wb/x",
    "cat <<'EOF' > notes.md\nWe push through PRs.\nEOF\ngit push origin wb/x",
    # QA on 0.1.45 (E67): back on a branch of its own after updating main (feature/x is a local
    # branch of both checkouts here), as v0.1.43 let it through.
    "git checkout main && git pull && git checkout feature/x && git push",
    "git switch main && git pull && git switch feature/x && git push",
    "git switch feature/x && git push",
    # (E64) What asks git for the checkout's branch pushes that branch (on main: as a push
    # naming none); what asks for anything else is not known.
    'git push origin "$(git symbolic-ref --short HEAD)"',
    "git push -u origin HEAD:$(git branch --show-current)",
    'B=`git branch --show-current` && git push -u origin "$B"',
    "git push origin $(git symbolic-ref --short refs/remotes/origin/HEAD | cut -d/ -f2)",
    "DEFAULT=$(git rev-parse --abbrev-ref origin/HEAD | cut -d/ -f2); git push origin $DEFAULT",
    # (E66, E68) Configuration read or set, and the other commands that push.
    "cat .git/config && git push origin wb/x",
    "grep -n url .git/config",
    "git lfs pull && git push origin wb/x",
    "git lg -5",
    "git config alias.co checkout && git co wb/x",
    '"$PYTHON" -m pytest -q',
    "$EDITOR README.md",
    "git config --get push.default && git push origin wb/x",
    "git config remote.origin.push HEAD:main && git push",
    "git subtree push --prefix=docs origin wb/x",
    "git subtree push --prefix=docs origin main",
    "git send-pack ../origin.git main",
    "git config alias.p push && git p origin main",
]

# Pushes v0.1.42 let through, unread, from any checkout, though they may update the default
# branch: bypasses refused since (QA on 0.1.45: E64, E66, E68).
INTENDED = {
    "git push origin $(git symbolic-ref --short refs/remotes/origin/HEAD | cut -d/ -f2)",
    "DEFAULT=$(git rev-parse --abbrev-ref origin/HEAD | cut -d/ -f2); git push origin $DEFAULT",
    "git config remote.origin.push HEAD:main && git push",
    "git subtree push --prefix=docs origin main",
    "git send-pack ../origin.git main",
    "git config alias.p push && git p origin main",
}

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
    'git push origin "$(git symbolic-ref --short HEAD)"',
    "git push -u origin HEAD:$(git branch --show-current)",
    'B=`git branch --show-current` && git push -u origin "$B"',
}  # fmt: skip


def test_the_normal_corpus_is_large():
    assert len(set(NORMAL)) >= 250 and set(NORMAL) >= INTENDED_ON_MAIN | INTENDED


@pytest.mark.parametrize("command", NORMAL)
def test_normal_work_gets_v0142s_decision(command, feature, main_checkout):
    old = bool(old_denial(command)) or command in INTENDED
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
    monkeypatch.setattr(policy, "config_entries", lambda folder: [])
    resolving, resolve = [], policy.resolved
    monkeypatch.setattr(policy, "resolved", lambda *a: resolving.append(1) or resolve(*a))
    found, took = timed(command, feature)
    assert found and "too complex" in found
    assert len(resolving) <= len(command.split()) and took < SANITY


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
    assert found is None and work["parse"] == 1 and linear(work, command)
    assert took < SANITY, (unit, took)


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


@pytest.mark.timeout(120, func_only=True)  # (Scaled to the runner: paced.)
@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_what_the_check_lets_through_pushes_nothing_to_main_in_real_bash(
    shell, tmp_path, capsys, paced
):
    """Review of 767e08f (C): command lines of a safe vocabulary (tests/policy_oracle.py),
    generated with a fixed seed, and every line of this file, run in real bash with git a
    shim that writes down what it pushes: none the check lets through pushed main or master.
    And in zsh, as Codex runs a worker's commands (the check told so), where zsh is. The
    rates are printed (pytest -s)."""
    import policy_oracle as oracle

    if shell == "zsh" and not oracle.ZSH:
        pytest.skip("no zsh here")
    began = time.monotonic()
    generator = oracle.Generator(0)
    cases = [generator.line() for _ in range(1000)]
    cases += [(line, start) for line in oracle.seeds() for start in ("main", "feat")]
    cases += oracle.FOUND + (oracle.FOUND_ZSH if shell == "zsh" else [])
    mark = f"ci-{shell}-{os.getpid()}-{time.monotonic_ns()}"
    outcomes = oracle.run(policy, cases, tmp_path, shell=shell, mark=mark)
    assert not oracle.marked(mark), "a line left something running"
    unsound = [(outcome.start, outcome.preset, outcome.line, sorted(outcome.targets))
               for outcome in outcomes if outcome.unsound]  # fmt: skip
    allowed = sum(outcome.allowed for outcome in outcomes)
    with capsys.disabled():
        refused, took = len(outcomes) - allowed, time.monotonic() - began
        sys.stdout.write(f"\noracle ({shell}): {len(outcomes)} lines, {allowed} allowed, {refused} "
                         f"refused, {len(unsound)} unsound ({took:.0f} s; runner pace {paced:.1f}: "
                         f"{120 * paced:.0f} s allowed)\n")  # fmt: skip
    assert not unsound, unsound[:10]


def test_the_tests_have_a_home_and_git_configuration_of_their_own():
    """A line the oracle ran (`unset HOME && echo … >> ~/.config/git/config`: `~` is then the
    account's own home) wrote the owner's git configuration. The tests run with a home, an
    XDG_CONFIG_HOME and git configuration of their own (conftest), never the account's."""
    import policy_oracle as oracle
    from own_home import MOST_PATH, owners_only

    home = Path(os.environ["HOME"])
    assert home.resolve() != oracle.REAL_HOME.resolve() and home == home.resolve()
    assert len(str(home)) <= MOST_PATH and owners_only(home)
    assert os.environ["XDG_CONFIG_HOME"] == f"{home}/.config"
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
    owners = {oracle.REAL_HOME / ".gitconfig", oracle.REAL_HOME / ".config" / "git" / "config"}
    assert Path(os.environ["GIT_CONFIG_GLOBAL"]) not in owners
    done = subprocess.run(["git", "config", "--global", "--list", "--show-origin"],
                          capture_output=True, text=True, check=False)  # fmt: skip
    assert not any(str(path) in done.stdout for path in owners)


def test_the_oracle_stops_when_the_owners_git_configuration_changes(tmp_path, monkeypatch):
    import policy_oracle as oracle

    states = iter([{"~/.gitconfig": None}, *[{"~/.gitconfig": (1,)}] * 4])
    monkeypatch.setattr(oracle, "owners_git_configuration", lambda: next(states))
    with pytest.raises(RuntimeError, match="owner's git configuration changed"):
        oracle.run(policy, [("git status", "feat")], tmp_path, workers=1)


def test_a_line_the_oracle_runs_changes_nothing_out_of_its_sandbox(tmp_path):
    """The shim works in its sandbox's repositories only; and, where macOS's sandbox-exec
    works, a line can write nothing in the home but in its sandbox."""
    import policy_oracle as oracle

    other = tmp_path / "other"
    oracle.make_repository(other, "main", ["push.default=simple"])
    box = oracle.Sandbox(tmp_path / "box")
    box.run(f"cd {other} && git checkout -b x; git config --global alias.p push", "feat")
    assert (other / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"
    assert (other / ".git" / "branches").read_text() == "main\nfeat\nmaster\nwb/x\n"
    home = tmp_path / "home"
    (home / "sandbox").mkdir(parents=True)
    prefix = oracle.confined(home / "sandbox", home)
    if not prefix:
        pytest.skip("no sandbox-exec here")
    line = f"echo x > {home}/outside; echo y > {home}/sandbox/inside"
    subprocess.run([*prefix, "/bin/sh", "-c", line], check=False, capture_output=True)
    assert not (home / "outside").exists() and (home / "sandbox" / "inside").exists()


def test_a_line_that_ends_as_the_oracle_stops_it_is_no_error(tmp_path, monkeypatch):
    """The 100k run on 84ef95b, on a machine loaded past 150: a line past its 10 seconds ended
    as it was stopped, and macOS said EPERM to killing its (ending) process group."""
    import policy_oracle as oracle

    class Late(subprocess.Popen):
        def wait(self, timeout=None):
            if timeout == 10:
                super().wait()
                raise subprocess.TimeoutExpired(self.args, timeout)
            return super().wait(timeout)

    def refused(*_):
        raise PermissionError(1, "Operation not permitted")

    box = oracle.Sandbox(tmp_path / "box")
    monkeypatch.setattr(oracle.subprocess, "Popen", Late)
    monkeypatch.setattr(oracle.os, "killpg", refused)
    assert box.run("git push origin feat", "feat") == [
        f"PUSH\t{box.folder('feat')}\tfeat\t\t\tpush\x1forigin\x1ffeat"
    ]


@pytest.mark.parametrize(
    ("line", "shell", "grouped"),
    [
        ("while :; do sleep 1; done &", "bash", True),
        ("( while :; do sleep 1; done & ); exit 0", "bash", True),
        ("while :; do sleep 1; done & disown", "bash", True),
        ("while :; do sleep 1; done &!", "zsh", True),
        ("until git -C repo push origin main; do sleep 1; done", "bash", True),
        (f"{sys.executable} -c 'import os, time; os.setsid(); time.sleep(600)' &", "bash", False),
    ],
)
def test_a_line_that_leaves_a_loop_running_leaves_nothing(line, shell, grouped, tmp_path):
    """Four `until git -C repo push origin main; do sleep 1; done` of the oracle's test lived
    18 hours: macOS answered EPERM for their process group (a process of it had ended and was
    not yet reaped), taken for "none left". What a line leaves in its process group ends with
    it (a job, a double fork, `disown`, past its 10 seconds); what left the group (`setsid`),
    by its mark."""
    import policy_oracle as oracle

    if shell == "zsh" and not oracle.ZSH:
        pytest.skip("no zsh here")
    box = oracle.Sandbox(tmp_path / "box")
    box.run(line, "feat", shell)
    assert bool(oracle.marked(box.last_mark)) != grouped
    oracle.end_marked(box.last_mark)
    assert not oracle.marked(box.last_mark)


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


@pytest.mark.parametrize(
    "command",
    ["git -C \ud800 push", "env -C \ud800 git push", "cd -P \ud800 && git push",
     "cd \ud800 && git push"],
)  # fmt: skip
def test_a_folder_the_system_cannot_name_is_refused_saying_why(command, feature):
    """The invariant review of a9aa9cc (G4): a name with a lone surrogate went to realpath, which
    raised: refused as "could not be checked". Its folder is not known, and so said."""
    found = denial(command, feature)
    assert found and policy.NOT_CHECKED not in found and "can't tell which branch" in found


@pytest.mark.parametrize(
    ("words", "read"),
    [
        (["-C", "../main", "git"], ("../main", None)),
        (["-iC", "../main", "git"], ("../main", None)),
        (["-iC../main", "git"], ("../main", None)),
        (["-vC", "x", "git"], ("x", None)),
        (["-0C", "x", "git"], ("x", None)),
        (["-P", "/usr/bin", "-C", "x", "git"], ("x", None)),
        (["-u", "C", "git"], (None, None)),
        (["--chdir", "x", "git"], ("x", None)),
        (["--chdir=x", "-S", "git push", "origin"], ("x", "git push origin")),
        (["-iS", "git push"], (None, "git push")),
        (["-X", "git"], (policy.LOST, None)),
        (["--frobnicate", "git"], (policy.LOST, None)),
        (["A=b", "-C", "x", "git"], (None, None)),
    ],
)
def test_envs_options_are_read_as_gnu_and_bsd_env_read_them(words, read):
    """The invariant review of a9aa9cc: `env -iC ../main git push` (a cluster), `-P DIR -C …`
    (an option that takes a value) pushed main. Every option is read; one not known: the
    folder is not known."""
    assert policy.env_options(words) == read


@pytest.mark.parametrize(
    "command",
    ["alias g='git push origin main'; g", "alias -g G='push origin main'; git G",
     "BASH_ALIASES[g]='git push origin main'; g", 'alias g="$X"; X=push; g',
     "alias g=git; g push origin main", "alias g='git '; alias p=push; g p origin main"],
)  # fmt: skip
def test_an_alias_that_may_push_is_refused(command, feature):
    """The invariant review of a9aa9cc: bash (told so) and zsh run the aliases a line sets
    (`shopt -s expand_aliases`): one whose text may push is refused, saying so."""
    found = denial(command, feature)
    assert found and "can't tell which branch" in found


@pytest.mark.parametrize(
    ("command", "shell", "goes"),
    [
        ("cd n && cd .. && git push", "", "logical"),
        ("set -P; cd n && cd .. && git push", "", "physical"),
        ("set -eP; cd n && cd .. && git push", "", "physical"),
        ("set -P; set +P; cd n && cd .. && git push", "", "logical"),
        ("set -o physical; cd n && cd .. && git push", "", "physical"),
        ("set -- -P; cd n && cd .. && git push", "", "logical"),
        ("set -o nophysical; cd n && cd .. && git push", "", "unknown"),
        ("shopt -so physical; cd n && cd .. && git push", "", "physical"),
        ("shopt -s -o physical; shopt -u -o physical; cd n && cd .. && git push", "", "logical"),
        ("shopt -o physical; cd n && cd .. && git push", "", "logical"),
        ("set -P; cd -L n && cd -L .. && git push", "", "logical"),
        ("set -P; x=$(cd n && cd .. && git push)", "", "unknown"),
        ("x=$(cd n && cd .. && git push)", "", "logical"),
        ("set -P; eval 'cd n && cd .. && git push'", "", "physical"),
        ("set -P; bash -c 'cd n && cd .. && git push'", "", "logical"),
        ("bash -P -c 'cd n && cd .. && git push'", "", "unknown"),
        ("bash -o physical -c 'cd n && cd .. && git push'", "", "unknown"),
        ("zsh -w -c 'cd n && cd .. && git push'", "", "unknown"),
        ("zsh -c 'set -w; cd n && cd .. && git push'", "", "physical"),
        ('eval "$X"; cd n && cd .. && git push', "", "unknown"),
        ('eval "$X"; cd {work} && git push', "", "logical"),
        ('eval "$X"; cd {work}/n/.. && git push', "", "unknown"),
        ('eval "$X"; cd -L {work}/n/.. && git push', "", "logical"),
        ("set -w; cd n && cd .. && git push", "zsh", "physical"),
        ("set -P; cd n && cd .. && git push", "zsh", "unknown"),
        ("setopt chase_dots; cd n && cd .. && git push", "zsh", "physical"),
        ("setopt NO_CHASE_LINKS; cd n && cd .. && git push", "zsh", "logical"),
        ("setopt chase_dots; unsetopt chase_dots; cd n && cd .. && git push", "zsh", "logical"),
        ("unsetopt nochaselinks; cd n && cd .. && git push", "zsh", "physical"),
        ("setopt -m 'chase*'; cd n && cd .. && git push", "zsh", "unknown"),
        ("options[chaselinks]=on; cd n && cd .. && git push", "zsh", "unknown"),
        ("emulate zsh -o chaselinks; cd n && cd .. && git push", "zsh", "unknown"),
        ("echo | setopt chase_dots; cd n && cd .. && git push", "zsh", "unknown"),
        ("setopt chase_dots; cd n && cd .. && git push", "", "unknown"),
        ("SHELLOPTS=physical bash -c 'cd n && cd .. && git push'", "zsh", "unknown"),
    ],
)
def test_cd_reads_dot_dot_as_the_lines_mode_says(command, shell, goes, tmp_path):
    """The invariant review of a9aa9cc: `set -P` (and `set -o physical`, `shopt -so physical`,
    zsh's `chase_links`/`chase_dots`, a shell's own options) has every cd after it read `..` as
    the system goes; `cd n && cd ..` through a link n into another checkout pushed from it."""
    work, other = tmp_path / "work", tmp_path / "other"
    (other / "sub").mkdir(parents=True)
    work.mkdir()
    (work / "n").symlink_to(other / "sub")
    token = policy.SHELL.set(shell)
    try:
        policy.reading.cache_clear()
        found = policy.pushes(command.format(work=work), str(work))
    finally:
        policy.SHELL.reset(token)
    assert len(found) == 1, found
    folder = found[0].bare_in
    expected = {"logical": str(work), "physical": str(other), "unknown": policy.LOST}[goes]
    assert (folder if folder is None or policy.LOST not in folder else policy.LOST) == expected


@pytest.mark.parametrize(
    ("command", "goes"),
    [
        ("cd m; cd ../main && git push", "unknown"),
        ("pushd m; cd ../main && git push", "unknown"),
        ("cd m && cd .. && git push", "work"),
        ("cd -P m && cd ../main && git push", "a/main"),
        ("cd build && cd .. && git push", "work"),
        ("cd sub && git push", "work/sub"),
    ],
)
def test_a_cd_to_no_folder_but_the_systems_leaves_the_folder_unknown(command, goes, tmp_path):
    """The owner's review of 2ab43e1: where a name is no folder as the shell reads it (`m/../main`
    with m a link: work/main) but is one as the system does (a/main), bash goes there; a name
    that is no folder either way (made later, or a typo) is read as before."""
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    (tmp_path / "a" / "inner").mkdir(parents=True)
    (tmp_path / "a" / "main").mkdir()
    (work / "m").symlink_to(tmp_path / "a" / "inner")
    policy.reading.cache_clear()
    found = policy.pushes(command, str(work))
    assert len(found) == 1, found
    folder = found[0].bare_in or ""
    assert (policy.LOST if policy.LOST in folder else folder) == (
        policy.LOST if goes == "unknown" else str(tmp_path / goes)
    )


@pytest.mark.parametrize(
    ("command", "shell", "unknown"),
    [
        ('sh -c "git push origin $B:t"', "zsh", True),
        ("eval \"bash -c 'git push origin $B:l'\"", "zsh", True),
        ("sh -c 'sh -c \"git push origin $B[1]\"'", "zsh", True),
        ("sh -c 'git push origin \"$B\":t'", "", False),
        # (Unquoted, in bash too, `$B` may be several refspecs: found by this round's 200k runs.)
        ('sh -c "git push origin $B:t"', "", True),
        ("zsh -c 'git push origin $B:t'", "", True),
    ],
)
def test_zshs_modifiers_stay_unknown_in_a_line_it_hands_to_another_shell(command, shell, unknown):
    """A 200k run on 2ab43e1: from zsh (Codex), `sh -c "git push origin $B:t"` pushed main as
    zsh put `$B:t` in (b391f06 read sh's line as sh does: `$B` then `:t`)."""
    token = policy.SHELL.set(shell)
    try:
        policy.reading.cache_clear()
        found = policy.pushes(command, "")
    finally:
        policy.SHELL.reset(token)
    assert any(push.unknown for push in found) == unknown, found


@pytest.mark.parametrize(
    ("option", "zsh", "read"),
    [("physical", False, (False, True)), ("PHYSICAL", False, (False, False)),
     ("nophysical", False, (False, False)), ("chase_links", False, (False, False)),
     ("chase_links", True, (False, True)), ("CHASE_DOTS", True, (False, True)),
     ("no_chase_links", True, (True, True)), ("physical", True, (False, False)),
     ("pipefail", False, None), ("notify", True, None)],
)  # fmt: skip
def test_an_options_name_is_read_as_its_shell_reads_it(option, zsh, read):
    """bash takes `physical` only, as written; zsh its own names, any case, `_` or `no` aside;
    a name like those that the shell does not take: (…, False), so not known."""
    assert policy.chase(option, zsh) == read


# A script fed to a shell, on a line that says push (or may go to another branch): refused as
# unreadable, from any checkout; v0.1.42 let these through (the owner's call on b391f06).
FED = [
    "echo 'git push origin main' | bash", "echo 'git push' | sh", "echo 'git push' | zsh",
    "echo 'git push' | dash", "echo 'git push' | ksh", "echo 'git push' | env bash",
    "echo 'git push' | /bin/bash", "echo 'git push' | timeout 60 bash",
    "echo 'git push' | nice -n 5 bash", "echo 'git push' | command bash",
    "echo 'git push' | bash -s -- x", "echo 'git push' | sh -s", "echo 'git push' | (bash)",
    "echo 'git push' | { bash; }", "echo 'git push' | $SHELL", "echo 'git push' | su",
    "echo 'git push' | xargs -0 sh -c", "echo 'git push' | env -S 'bash -s'",
    "bash <(echo 'git push origin main')", "bash<(echo 'git push')",
    "bash < <(echo 'git push')", "bash <<< 'git push origin main'",
    "bash <<EOF\ngit push\nEOF", "sh -s <<'EOF'\ngit push\nEOF",
    "cat <<'EOF' | bash\ngit push\nEOF", "cat <<'EOF' | bash\nnpm test\nEOF\ngit push origin wb/x",
    "source <(echo 'git push origin main')", ". <(echo 'git push')",
    ". /dev/stdin <<< 'git push'", "source /dev/fd/0 <<< 'git push'",
    "echo 'git symbolic-ref HEAD refs/heads/main' | bash", "echo 'git update-ref HEAD x' | sh",
    "echo 'git branch -M main' | bash", "echo 'git branch -m wb/x master' | bash",
    "sh -c \"echo 'git push' | bash\"", "x=$(echo 'git push' | bash)",
    "echo 'git push' | sh -c 'bash'", "f() { bash; }; echo 'git push' | f",
    "eval \"echo 'git push' | bash\"",
    "find . -maxdepth 0 -exec sh -c \"echo 'git push' | bash\" \\;",
    "trap \"echo 'git push' | bash\" EXIT", "env -S \"sh -c 'echo git push | bash'\"",
    "emulate sh -c \"bash <<< 'git push'\"", "echo 'git push' | env -i PATH=/bin bash",
    "echo 'git push' | bash -o errexit", "echo 'git push' | su - root",
    "echo 'git push' | bash\necho \"x", "env -S bash <(echo 'git push')",
    # Found by a zsh 200k run: the file of a process substitution `eval` is given, for a shell.
    "eval zsh -f <(echo 'git push origin main')", "eval 'x;' bash <(echo 'git push')",
]  # fmt: skip


@pytest.mark.parametrize("command", FED)
def test_a_script_fed_to_a_shell_on_a_line_that_says_push_is_refused(
    command, feature, main_checkout
):
    for folder in (feature, main_checkout):
        found = denial(command, folder) or ""
        assert found.startswith("Denied: "), (command, folder)
    assert policy.fed_script(command), command
    if "main" not in command and "master" not in command:
        assert not old_denial(command), command  # (An intended new refusal.)
        assert policy.FED_SCRIPT in (denial(command, feature) or ""), command


@pytest.mark.parametrize(
    ("command", "says"),
    [("git push", True), ("git pu'sh'", True), ("git symbolic-ref HEAD x", True),
     ("git update-ref HEAD x", True), ("git branch -M main", True),
     ("git branch -m old master", True), ("git branch --move x main", True),
     ("git branch -fm main", True), ("git branch -d main", False),
     ("git branch -m old new", False), ("npm test", False),
     ("git branch -a; echo main -m", False)],
)  # fmt: skip
def test_what_a_fed_script_must_say_to_be_refused(command, says):
    assert policy.fed_says(command) == says


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


# QA on 0.1.45 (E64 to E68).


@pytest.mark.parametrize(
    ("command", "cwd", "reason"),
    [
        # (E64) A whole substitution that asks anything but the checkout's branch.
        ("git push origin $(printf HEAD:ma; printf in)", "feature", "can't tell which branch"),
        ("git push origin $(cat branch.txt)", "feature", "can't tell which branch"),
        ("DEFAULT=$(git rev-parse --abbrev-ref origin/HEAD | cut -d/ -f2); "
         "git push origin $DEFAULT", "feature", "can't tell which branch"),
        ('git push origin "$(git symbolic-ref --short HEAD)"', "main",
         "push to the default branch"),
        # (E65) Every branch of the same name on both sides.
        ("git push origin :", "feature", "push to the default branch"),
        # (E67) Several branches only because of a switch before it: not known which.
        ("git checkout feature/x; git push", "main", "can't tell which branch"),
        ("git switch main; git push", "feature", "can't tell which branch"),
        ("git checkout main && git push", "feature", "push to the default branch"),
        ("git switch feature/x && git switch main && git push", "feature",
         "push to the default branch"),
        # (E66, E68) A line that sets push configuration or an alias; a command in a variable.
        ("git config remote.origin.push HEAD:main && git push", "feature",
         "can't tell which branch"),
        ("git config alias.p push && git p origin main", "feature", "can't tell which branch"),
        ("g=git; $g push origin main", "feature", "can't tell which branch"),
        ("git subtree push --prefix=docs origin main", "feature", "push to the default branch"),
        ("git send-pack ../origin.git", "feature", "push to the default branch"),
    ],
)  # fmt: skip
def test_qa_on_0145_a_refusal_says_why(command, cwd, reason, feature, main_checkout):
    found = denial(command, feature if cwd == "feature" else main_checkout) or ""
    assert reason in found, (command, found)
    assert reason == "push to the default branch" or "default branch" not in found


def test_a_branch_put_in_a_variable_is_the_one_it_was_then(tmp_path):
    """QA on 0.1.45 (E64): `$(git branch --show-current)` in a variable is the branch when it
    was set: after a `cd` or a switch, which branch that was is not known."""
    other = checkout(tmp_path / "main", "main")
    work = checkout(tmp_path / "work", "wb/x")
    assert denial('B=$(git branch --show-current); git push origin "$B"', work) is None
    for command in (
        f'cd {other}; B=$(git branch --show-current); cd {work} && git push origin "$B"',
        'git switch main; B=$(git branch --show-current); git switch wb/x && git push origin "$B"',
        'B=$(git branch --show-current); git switch main && git push origin "$B"',
        'git switch main; B=$(git branch --show-current); (git switch wb/x); git push origin "$B"',
    ):
        assert denial(command, work), command


def configured(path, settings: list[tuple[str, str]]):
    found = checkout(path, "wb/x")
    for key, value in settings:
        subprocess.run(["git", "-C", str(found), "config", "--add", key, value], check=True)
    return found


@pytest.mark.parametrize(
    ("settings", "command"),
    [
        ([("push.default", "matching")], "git push"),
        ([("push.default", "matching")], "git switch -c fix && git push"),
        ([("push.default", "matching")], "git push origin"),
        ([("remote.origin.push", "HEAD:main")], "git push"),
        ([("remote.origin.push", "HEAD:main")], "git switch feature/x && git push"),
        ([("remote.origin.push", "refs/heads/wb/x:refs/heads/main")],
         'git push origin "$(git branch --show-current)"'),
        ([("remote.origin.push", ":")], "git push"),
        ([("remote.origin.push", "+refs/heads/*:refs/heads/*")], "git push"),
        ([("remote.other.push", "HEAD:master")], "git push"),
        ([("remote.origin.mirror", "true")], "git push origin"),
        ([("push.default", "upstream"), ("branch.wb/x.merge", "refs/heads/main")], "git push"),
        ([("push.default", "tracking"), ("branch.wb/x.merge", "refs/heads/main")],
         "git push origin $(git branch --show-current)"),
        ([("push.default", "upstream"), ("branch.feature/x.merge", "refs/heads/main")],
         "git switch feature/x && git push"),
        ([("push.default", "upstream")], "git branch -u origin/main && git push"),
        ([("push.default", "upstream")], "git checkout -b fix origin/main && git push"),
        ([("push.default", "bogus")], "git push"),
        ([("includeIf.onbranch:feature/*.path", "push.cfg")], "git switch feature/x && git push"),
        ([("remote.origin.push", "HEAD:main")], "git -c remote.origin.push=HEAD:wb/x push"),
    ],
)  # fmt: skip
def test_where_gits_configuration_sends_a_bare_push_is_read(settings, command, tmp_path):
    """QA on 0.1.45 (E66): `push.default=matching`, a remote's push refspecs or mirror, the
    upstream a branch pushes to, in the repository's configuration, sent a push naming no
    branch to main; read from git now (once a folder), and not known where it can't be."""
    work = configured(tmp_path / "repo", settings)
    assert denial(command, work), command


@pytest.mark.parametrize(
    ("settings", "command"),
    [
        ([("remote.origin.push", "HEAD:main")], "git push origin HEAD"),
        ([("remote.origin.push", "HEAD:main")], "git push origin wb/x"),
        ([("remote.origin.push", "HEAD:wb/x")], "git push"),
        ([("push.default", "matching")], "git push origin wb/x"),
        ([("push.default", "matching")], "git push -u origin HEAD"),
        ([("push.default", "current")], "git push"),
        ([("push.default", "nothing")], "git push"),
        ([("push.default", "upstream"), ("branch.wb/x.merge", "refs/heads/wb/x")], "git push"),
        ([("push.default", "simple"), ("branch.wb/x.merge", "refs/heads/main")], "git push"),
        ([("remote.origin.mirror", "false")], "git push"),
        ([("remote.origin.push", "^refs/heads/main")], "git push"),
    ],
)  # fmt: skip
def test_configuration_that_pushes_the_checkouts_own_branch_lets_it_through(
    settings, command, tmp_path
):
    work = configured(tmp_path / "repo", settings)
    assert denial(command, work) is None, command


def test_global_configuration_is_read_and_unreadable_is_not_known(tmp_path, monkeypatch, feature):
    """QA on 0.1.45 (E66): the user's own configuration counts as the repository's; one git
    can't read leaves where a bare push goes not known."""
    settings = tmp_path / "gitconfig"
    settings.write_text("[push]\n\tdefault = matching\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(settings))
    assert "default branch" in (denial("git push", feature) or "")
    assert denial("git push origin wb/x", feature) is None
    settings.write_text("[push\n")
    assert "can't tell which branch" in (denial("git push", feature) or "")
    monkeypatch.delenv("GIT_CONFIG_GLOBAL")
    monkeypatch.setattr(policy, "config_entries", lambda folder: None)
    assert "can't tell which branch" in (denial("git push", feature) or "")


@pytest.mark.parametrize(
    "command",
    [
        "git config remote.origin.push HEAD:main && git push",
        "git config --global push.default matching\ngit push",
        "git config --local --add remote.origin.push HEAD:main; git push origin",
        "git config --unset-all remote.origin.push && git push",
        "git config --replace-all push.default matching && git push",
        "git config --remove-section remote.origin; git push",
        "git config --rename-section remote.x remote.origin; git push",
        "git config branch.wb/x.merge refs/heads/main && git push",
        "git config set --global push.default matching && git push",
        "git config unset push.default && git push",
        "git config --edit && git push",
        "git config $K $V && git push",
        "git config include.path ../push.cfg && git push",
        "git config --global alias.p push",
        "git config alias.p '!git' && git p push origin main",
        "git push && git config remote.origin.push HEAD:main",
        'echo "$(git config --global remote.origin.mirror true)"; git push',
    ],
)
def test_a_line_that_sets_push_configuration_cant_tell_where_it_pushes(command, feature):
    """QA on 0.1.45 (E66, E68): `git config` setting where pushes go (or an alias, on a line
    that says push) anywhere in the line: which branch a push updates is not known."""
    assert "can't tell which branch" in (denial(command, feature) or ""), command


@pytest.mark.parametrize(
    "command",
    [
        "git config user.email && git push",
        "git config --get remote.origin.push && git push origin wb/x",
        "git config --get-regexp '^push' && git push",
        "git config -l | grep push && git push",
        "git config list && git push",
        "git config get push.default && git push",
        "git config user.name 'Push Bot' && git push origin wb/x",
        "git config --global core.editor vim && git push",
        "git config push.autoSetupRemote true && git push",
        "git config alias.st status && git st",
    ],
)
def test_configuration_read_or_said_of_other_things_lets_a_push_through(command, feature):
    assert denial(command, feature) is None, command


def test_a_checkout_switches_only_to_a_branch_git_has(tmp_path):
    """QA on 0.1.45 (E67): `git checkout NAME` puts the checkout on NAME only when NAME is a
    local branch (asked of git); a file or a folder of that name, or a branch the line deletes
    first, leaves it where it was."""
    main = checkout(tmp_path / "repo", "main")
    git = ["git", "-C", str(main), "-c", "user.email=t@t", "-c", "user.name=t"]
    (main / "notes").write_text("x\n")
    subprocess.run([*git, "add", "notes"], check=True)
    subprocess.run([*git, "commit", "-qm", "notes"], check=True)
    subprocess.run([*git, "branch", "notes"], check=True)
    subprocess.run([*git, "branch", "src"], check=True)
    assert denial("git checkout feature/x && git push", main) is None
    for command in (
        "git checkout notes && git push",
        "git checkout src && git push",
        "git branch -D feature/x && git checkout feature/x && git push",
        "git branch -m feature/x old && git checkout feature/x && git push",
        "git update-ref -d refs/heads/feature/x && git checkout feature/x && git push",
        "git checkout nope && git push",
    ):
        found = denial(command, main) or ""
        assert "can't tell which branch" in found, command


def test_what_a_checkout_asks_of_git_is_asked_once_a_folder(work, main_checkout):
    command = " && ".join(f"git checkout a{i} && git push" for i in range(40))
    assert denial(command, main_checkout)
    assert work["git"] <= 4, work


@pytest.mark.parametrize(
    ("words", "read"),
    [
        (["checkout", "feature/x"], ("branch?", "feature/x")),
        (["checkout", "-q", "feature/x"], ("branch?", "feature/x")),
        (["switch", "feature/x"], ("switched", "feature/x")),
        (["switch", "--no-guess", "-f", "feature/x"], ("switched", "feature/x")),
        (["switch", "--detach", "feature/x"], ("to", "feature/x")),
        (["checkout", "-p", "feature/x"], ("to", "feature/x")),
        (["checkout", "feature/x", "src"], ("to", "feature/x")),
        (["switch", "-fd", "feature/x"], ("to", "feature/x")),
        (["switch", "$B"], ("to", "$B")),
        (["checkout", "-b", "fix"], ("new", "fix")),
        (["checkout", "--", "src"], None),
    ],
)
def test_a_switch_names_the_branch_it_goes_to(words, read):
    assert policy.switch_of(words) == read


def test_what_git_says_is_read_so_far_at_most(feature):
    """QA on 0.1.45 (E66, G3): git's configuration is read with one call a folder, at most so
    many bytes of it; more, or a git that can't be run, is not known."""
    code, said = policy.git_output(["-C", str(feature), "rev-parse", "--abbrev-ref", "HEAD"], 64)
    assert (code, said) == (0, b"wb/x\n")
    assert policy.git_output(["-C", str(feature), "rev-parse", "--abbrev-ref", "HEAD"], 3) is None
    assert policy.git_output(["-C", "a\x00b", "status"], 64) is None
    assert policy.config_entries(str(feature)) == []


@pytest.mark.parametrize(
    "command",
    [
        "printf '[push]\\n\\tdefault = matching\\n' >> .git/config && git push",
        "printf '[remote \"origin\"]\\n\\tpush = HEAD:main\\n' >> .git/config && git push",
        "printf '[push]\\n\\tdefault = matching\\n' >> ~/.gitconfig && git push",
        "printf '[push]\\n\\tdefault = matching\\n' | tee -a .git/config && git push",
        "cp ../push.cfg .git/config && git push",
        "mv push.cfg .git/config; git push",
        "install -m 644 push.cfg .git/config && git push",
        "cp -t .git config && git push",
        "sed -i.bak 's/simple/matching/' .git/config && git push",
        "perl -pi -e 's/simple/matching/' ~/.gitconfig && git push",
        "dd if=push.cfg of=.git/config && git push",
        'cat push.cfg >> "$GIT_DIR/config" && git push',
        "cat push.cfg >> .git/config.worktree && git push",
        "cat push.cfg >> ~/.config/git/config && git push",
        'cat push.cfg >> "$XDG_CONFIG_HOME/git/config" && git push',
        "cat push.cfg > $(git rev-parse --git-dir)/config && git push",
        "cd .git && cat ../push.cfg >> config && cd .. && git push",
        "printf '[alias]\\n\\tp = push\\n' >> .git/config && git p origin main",
        "cp ../aliases.cfg .git/config && git p origin main",
    ],
)
def test_a_line_that_writes_gits_configuration_cant_tell_where_it_pushes(command, feature):
    """The invariant review of 86f9f8d (E66): configuration written to git's own files on the
    line, not with `git config`, sent a push naming no branch to main (`push.default=matching`,
    a remote's push refspec, an alias): which branch it updates is not known."""
    assert "can't tell which branch" in (denial(command, feature) or ""), command


@pytest.mark.parametrize(
    "command",
    [
        "cat .git/config && git push",
        "grep -n url .git/config; git push origin wb/x",
        "cp .git/config config.bak && git push",
        "sed -n 1p ~/.gitconfig && git push",
        "head .git/config > notes.txt && git push",
        "echo done > build.log && git push",
        "cp app.cfg src/settings.cfg && git push",
    ],
)
def test_a_line_that_reads_gits_configuration_lets_a_push_through(command, feature):
    assert denial(command, feature) is None, command


def test_a_line_that_writes_a_file_gits_configuration_includes_cant_tell(tmp_path):
    """The invariant review of 86f9f8d (E66): a file git's configuration includes, written on
    the line, may say where a push goes, or what an alias runs."""
    work = configured(tmp_path / "repo", [("include.path", "../extra.cfg")])
    assert denial("echo x > notes.txt && git push", work) is None
    for command in (
        "printf '[push]\\n\\tdefault = matching\\n' >> ../extra.cfg && git push",
        "cp ../push.cfg extra.cfg && git push",
        "cp ../aliases.cfg extra.cfg && git p origin main",
    ):
        assert "can't tell which branch" in (denial(command, work) or ""), command


@pytest.mark.parametrize(
    "command",
    [
        "git -c alias.sp=send-pack sp ../origin.git main",
        "git -c alias.st=subtree st push -P docs origin main",
        "git -c 'alias.x=!git send-pack' x ../origin.git main",
    ],
)
def test_an_alias_given_on_the_line_that_sends_a_pack_is_refused(command, feature):
    """The invariant review of 86f9f8d (E68): an alias `-c` gives whose text runs send-pack or
    subtree pushes as one that says push does."""
    assert "can't tell which branch" in (denial(command, feature) or ""), command


ALIASES = [("alias.p", "push"), ("alias.pf", "push --force"), ("alias.sp", "send-pack"),
           ("alias.st", "subtree"), ("alias.q", "p"), ("alias.sh", "!git push"),
           ("alias.f", '!f() { git "$@"; }; f'), ("alias.lg", "log --oneline"),
           ("alias.pm", "-c push.default=matching push")]  # fmt: skip


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("git p origin main", "default branch"),
        ("git P origin main", "default branch"),
        ("git q origin main", "default branch"),
        ("git sp ../origin.git main", "default branch"),
        ("git st push -P docs origin main", "default branch"),
        ("git pm", "default branch"),
        ("git -C . p origin main", "default branch"),
        ("git checkout main && git p", "default branch"),
        ("x=$(git p origin main)", "default branch"),
        ("eval 'git p origin main'", "default branch"),
        ("git pf origin wb/x", "force push"),
        ("git sh origin wb/x", "can't tell"),
        ("git f push origin wb/x", "can't tell"),
        ("git f $X", "can't tell"),
        ("G=git; $G p origin main", "can't tell"),
        ("echo main | xargs git p origin", "can't tell"),
        ("git $C origin main", "can't tell"),
        ("git p origin main\necho 'x", "can't tell"),
        ("git config alias.lg p && git lg origin wb/x", "can't tell"),
    ],
)
def test_an_alias_in_gits_configuration_is_read_as_what_it_runs(command, reason, tmp_path):
    """The invariant review of 86f9f8d: an alias the repository's (or the user's) configuration
    has (`alias.p=push`, `alias.pf="push --force"`) pushed main, and forced, unread; read now
    from the same call that reads where pushes go, followed to what it runs."""
    work = configured(tmp_path / "repo", ALIASES)
    assert reason in (denial(command, work) or ""), command


@pytest.mark.parametrize(
    "command",
    ["git p origin wb/x", "git p", "git q origin wb/x", "git lg -5", "git f log", "git lfs pull",
     "git -c alias.p=log p origin main", "git status && git diff", "$(echo git) lg",
     "git config alias.co checkout && git co wb/x"],
)  # fmt: skip
def test_an_alias_that_pushes_no_default_branch_lets_it_through(command, tmp_path):
    work = configured(tmp_path / "repo", ALIASES)
    assert denial(command, work) is None, command


def test_the_users_aliases_count_and_are_asked_with_where_pushes_go(tmp_path, monkeypatch, work):
    """The user's aliases are read as the repository's, in the call that reads its push
    configuration; a line with no git command but git's own asks git nothing of them."""
    settings = tmp_path / "gitconfig"
    settings.write_text("[alias]\n\tp = push\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(settings))
    repo = checkout(tmp_path / "repo", "wb/x")
    work["git"] = 0
    assert "default branch" in (denial("git p origin main", repo) or "")
    assert work["git"] == 1
    assert denial("git status && git log -1 && git diff", repo) is None
    assert work["git"] == 1 and work["parse"] == 1
    assert denial("git p origin wb/x && git push", repo) is None
    assert work["git"] == 4, "the folder's branch, @{push}, its configuration: once each"
    settings.write_text("[alias\n")
    assert "can't tell which branch" in (denial("git lfs pull", repo) or "")


SWITCHING_ALIASES = [("alias.co", "checkout"), ("alias.sw", "switch"), ("alias.p", "push"),
                     ("alias.f", '!f() { git "$@"; }; f'), ("alias.via", "co")]  # fmt: skip


def aliased(path, branch: str):
    found = checkout(path, branch)
    for key, value in SWITCHING_ALIASES:
        subprocess.run(["git", "-C", str(found), "config", key, value], check=True)
    return found


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("git co main && git push", "default branch"),
        ("git sw main && git push", "default branch"),
        ("git via main && git push", "default branch"),
        ("git co main && git p", "default branch"),
        ("git -C . sw main && git push -u origin HEAD", "default branch"),
        ("git -c alias.go=checkout go main && git push", "default branch"),
        ("git -c alias.y=p y origin main", "default branch"),
        ("git co - && git push", "can't tell"),
        ("git co main; git co feature/x || true; git push", "can't tell"),
        ("git f checkout main && git push", "can't tell"),
        ("git config alias.go checkout && git go main && git push", "can't tell"),
        ("$G p origin main", "can't tell"),
        ("$G co main && $G push", "can't tell"),
        ("$G co main && git p", "can't tell"),
    ],
)
def test_an_alias_that_switches_branches_is_read_as_that_switch(command, reason, tmp_path):
    """Before 0.1.46: with `alias.co=checkout` (or `sw=switch`) in git's configuration, or `-c
    alias.co=checkout` on the line, `git co main && git push` pushed main, unread; it is read
    as the switch it runs (as E67 reads `git checkout main && git push`). And a command whose
    name the line doesn't set (`$G p origin main`), where `p` is an alias that pushes, is read
    as `$G push origin main` is: not known."""
    work = aliased(tmp_path / "repo", "wb/x")
    assert reason in (denial(command, work) or ""), command


@pytest.mark.parametrize(
    ("command", "branch"),
    [
        ("git co main && git pull && git co feature/x && git push", "wb/x"),
        ("git sw main && git pull && git sw feature/x && git push", "wb/x"),
        ("git sw feature/x && git push", "main"),
        ("git via feature/x && git push", "main"),
        ("git co main && git push origin wb/x", "wb/x"),
        ("git co nope && git push", "wb/x"),
        ("git -c alias.y=p y origin wb/x", "wb/x"),
        ("git config alias.go checkout && git go feature/x", "wb/x"),
        ('"$PYTHON" -m pytest -q', "wb/x"),
        ("$G status && $G log -1", "wb/x"),
    ],
)
def test_an_alias_that_switches_to_a_branch_of_its_own_lets_the_push_through(
    command, branch, tmp_path
):
    work = aliased(tmp_path / "repo", branch)
    assert denial(command, work) is None, command


def test_the_users_alias_that_switches_and_one_given_with_c_are_read(
    tmp_path, monkeypatch, feature
):
    settings = tmp_path / "gitconfig"
    settings.write_text("[alias]\n\tsw = switch\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(settings))
    repo = checkout(tmp_path / "repo", "wb/x")
    assert "default branch" in (denial("git sw main && git push", repo) or "")
    assert denial("git sw feature/x && git push", repo) is None
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "none"))
    assert "default branch" in (
        denial("git -c alias.co=checkout co main && git push", feature) or ""
    )


@pytest.mark.parametrize(
    "command",
    [
        "echo 'git p origin main' | sh",
        "echo 'git p' | bash",
        "(echo '$(echo git) p && <<<.. command -p '\\''re'\\''ad G' | xargs -0 sh -c)",
        "bash <<< 'git p origin feature/x'",
        "cat <<'EOF' | sh\ngit via main\ngit p\nEOF",
        "printf 'git co main\\n' | sh; git push origin wb/x",
    ],
)
def test_a_script_fed_to_a_shell_that_runs_an_alias_that_pushes_is_refused(command, tmp_path):
    """Found by the 100k bash run on 02b8513: a script fed to a shell runs aliases too; one of
    git's configuration that pushes (or that switches, on a line that says push) is named in
    the line: which branch the script pushes, not known."""
    work = aliased(tmp_path / "repo", "wb/x")
    assert "can't tell which branch" in (denial(command, work) or ""), command


@pytest.mark.parametrize(
    "command",
    ["echo 'git status' | sh", "echo 'git co feature/x' | sh", "echo 'git f log' | bash",
     "echo 'make test' | sh", "printf 'p\\n' | grep p"],
)  # fmt: skip
def test_a_script_fed_to_a_shell_that_names_no_alias_that_pushes_lets_it_through(command, tmp_path):
    work = aliased(tmp_path / "repo", "wb/x")
    assert denial(command, work) is None, command


@pytest.mark.parametrize(
    "command",
    [
        "sh -c 'echo remote.origin.push=HEAD:main >> .git/config; git push'",
        "bash -c 'cp .git/config .git/new; echo x >> .git/new; cp .git/new .git/config; true'; "
        "git push",
        "find . -maxdepth 0 -exec sh -c 'cp ../push.cfg .git/config; git push' \\;",
        "echo | xargs sh -c 'tee -a .git/config < ../push.cfg; git push'",
        "(eval 'echo push.default=matching >> .git/config; true') && git push",
        "(command eval 'cat ../push.cfg >> ~/.gitconfig; cd /; true') && git push",
        "env -S 'tee -a .git/config x' < ../push.cfg; git push",
        "zsh -lc 'sed -i.bak s/simple/matching/ .git/config && true'; git push",
        "sh -c \"sh -c 'echo x >> .git/config; true'\"; git push",
    ],
)
def test_a_script_a_command_runs_that_writes_gits_configuration_cant_tell(command, feature):
    """The 100k bash run on 408c670: git's configuration written by a script a command runs
    (`sh -c '…'`, `find … -exec sh -c '…'`, `xargs sh -c`, `env -S`, `eval`) was read as one
    word, and missed unless its file came last. It is read as a line of its own."""
    assert "can't tell which branch" in (denial(command, feature) or ""), command


@pytest.mark.parametrize(
    "command",
    [
        "sh -c 'cat .git/config; git push'",
        "bash -c 'git commit -m \"read .git/config\"; true' && git push",
        "sh -c 'echo done > build.log; git push origin wb/x'",
        "sh -c 'grep url .git/config' && git push",
    ],
)
def test_a_script_a_command_runs_that_only_reads_gits_configuration_lets_it_through(
    command, feature
):
    assert denial(command, feature) is None, command


def nested_shells(script: str, size: int) -> str:
    """A script given to `sh -c`, that line given to `sh -c` again, until it is `size` long."""
    while len(script) < size:
        script = "sh -c " + shlex.quote(script)
    return script


@pytest.mark.parametrize("size", [20_000, 100_000])
def test_scripts_given_to_shells_deep_in_quotes_are_read_in_linear_time(size, work, feature):
    """Each `sh -c` reads its script as a line, to MOST_NESTED deep (deeper: too complex, or
    loosely): the quotes of each level make the line grow, the reading of it does not grow
    faster."""
    command = nested_shells("echo x >> .git/config; git push", size) + "; git push"
    found, took = timed(command, feature)
    assert found and linear(work, command) and took < SANITY
    assert policy.written(command, str(feature))[0]


FORK = "+refs/heads/*:refs/remotes/fork/*"


@pytest.mark.parametrize(
    "settings",
    [
        [],
        [("push.default", "current")],
        [("push.default", "upstream"), ("branch.wb/x.remote", "origin"),
         ("branch.wb/x.merge", "refs/heads/main")],
        [("remote.origin.push", "refs/heads/wb/x:refs/heads/release")],
        [("remote.fork.url", "../fork.git"), ("remote.fork.fetch", FORK),
         ("branch.wb/x.pushRemote", "fork"), ("push.default", "current")],
        [("remote.pushDefault", "fork"), ("remote.fork.url", "../fork.git"),
         ("remote.fork.fetch", FORK), ("remote.fork.push", "HEAD:refs/heads/staging")],
    ],
)  # fmt: skip
def test_where_a_bare_push_goes_is_what_git_says_without_git_reading_the_repository(
    settings, tmp_path
):
    """The branch a folder is on and where a bare push there goes (`@{push}`), as git there
    says, read with no git run in the folder: its HEAD and config are read as files, and git
    works out the rest in a folder of Chief of Staff's own (as git there would, once fetched)."""
    work = configured(tmp_path / "repo", settings)
    for remote, branch in itertools.product(("origin", "fork"), ("main", "release", "staging")):
        subprocess.run(["git", "-C", str(work), "update-ref", f"refs/remotes/{remote}/{branch}",
                        "HEAD"], check=True)  # fmt: skip
    git = ["git", "-C", str(work), "rev-parse", "--abbrev-ref"]
    said = {subprocess.run([*git, "HEAD"], capture_output=True, text=True).stdout.strip()}
    pushed = subprocess.run([*git, "@{push}"], capture_output=True, text=True).stdout.strip()
    said |= {pushed.split("/", 1)[1]} if "/" in pushed else set()
    assert policy.current_branches(str(work)) == said
    assert policy.current_branches(str(work / "src")) == said


@pytest.mark.skipif(sys.platform == "win32", reason="a FIFO")
def test_a_repository_config_that_would_stall_git_does_not_stall_the_check(tmp_path):
    """A config including a FIFO (a worker's): git there would wait on it (ten seconds, twice,
    for each line checked); read as files, it is not opened, and where a push goes is not known
    (a push naming no branch is refused)."""
    work = configured(tmp_path / "repo", [("include.path", "../stall")])
    os.mkfifo(work / "stall")  # (Next to .git: as git finds what its config includes.)
    started = time.monotonic()
    assert policy.current_branches(str(work)) == set()
    assert time.monotonic() - started < 3


# A shell line run in the owner's checkout given to read (K) or in the worker's own folder (O),
# and what owners_checkout says of it: refused (W: it writes there), asked about (A: a program
# not known runs there on its files), or let through ("").
CHECKOUT_LINES = [
    ("echo y >> {K}/AGENTS.md", "O", "W"), ("echo y >> AGENTS.md", "K", "W"),
    ("cat AGENTS.md; sleep 1", "K", ""), ("grep -rn x src | head -5", "K", ""),
    ("sed -n '1,80p' src/app.py", "K", ""), ("sed -i 's/1/2/' src/app.py", "K", "W"),
    ("perl -pi -e 's/1/2/' {K}/src/app.py", "O", "W"), ("cat AGENTS.md | tee n.txt", "K", "W"),
    ("cat {K}/AGENTS.md | tee n.txt", "O", ""), ("cp {K}/src/app.py .", "O", ""),
    ("cp src/app.py src/b.py", "K", "W"), ("mv src/app.py src/c.py", "K", "W"),
    ("rm -rf {K}/src", "O", "W"), ("touch new.txt", "K", "W"), ("mkdir -p build", "K", "W"),
    ("chmod +x tool.py", "K", "W"), ("chmod 755 {O}/x", "K", ""), ("ln -s src/app.py l", "K", "W"),
    ("git status", "K", ""), ("git diff HEAD~1 -- src", "K", ""), ("git branch -a", "K", ""),
    ("git fetch origin", "K", ""), ("git stash list", "K", ""), ("git config user.name", "K", ""),
    ("git add -A", "K", "W"), ("git commit -m x", "K", "W"), ("git checkout -b f", "K", "W"),
    ("git stash", "K", "W"), ("git branch new", "K", "W"), ("git -C {K} reset --hard", "O", "W"),
    ("git config user.name x", "K", "W"), ("git worktree add ../wt", "K", "W"),
    ("git co main", "K", "A"), ("npm install", "K", "W"), ("npm --version", "K", ""),
    ("uv run pytest", "K", "W"), ("python3 -m pytest -q", "K", "W"), ("make -C {K}", "O", "W"),
    ("make", "O", ""), ("python3 tool.py", "K", "A"), ("python3 {K}/tool.py", "O", "A"),
    ("./tool.py", "K", "A"), ("mytool src/app.py", "K", "A"), ("mytool --version", "K", ""),
    ("python3 -c 'print(1)'", "K", ""), ("cd {K} && echo y > AGENTS.md", "O", "W"),
    ("(cd {K} && cat AGENTS.md); echo y > AGENTS.md", "O", ""),
    ("pushd {K}; touch z; popd; touch z", "O", "W"), ("cd {K}/src && touch ../a", "O", "W"),
    ("bash -c 'echo y >> {K}/AGENTS.md'", "O", "W"), ("eval 'echo y > {K}/a'", "O", "W"),
    ("cat > {K}/notes.md <<'EOF'\nhello\nEOF", "O", "W"),
    ("bash <<'EOF'\necho y > {K}/a\nEOF", "O", "W"), ("echo y > $OUT", "K", "A"),
    ("echo y > $OUT", "O", ""), ("find {K} -name '*.pyc' -delete", "O", "W"),
    ("find {K} -name '*.py' -exec grep -l x {{}} +", "O", ""),
    ("find {K} -name '*.pyc' | xargs rm", "O", "W"), ("tar -xzf {O}/a.tgz -C {K}", "O", "W"),
    ("tar -czf {O}/a.tgz -C {K} .", "O", ""), ("curl -sL https://x/y -o {K}/y", "O", "W"),
    ("wget https://x/y", "K", "W"), ("dd if=/dev/zero of={K}/z count=1", "O", "W"),
    ("sort {K}/AGENTS.md > {O}/s.txt", "O", ""), ("ls 2>/dev/null; echo x >&2", "K", ""),
    ("ls &> out.txt", "K", "W"), ("env A=1 timeout 5 rm -f a", "K", "W"),
    ("awk '{{print > \"out\"}}' AGENTS.md", "K", "A"), ("awk '{{print $1}}' AGENTS.md", "K", ""),
    ("touch {O}/../kept/a", "O", "W"), ("gh pr checkout 1", "K", "W"),
    ("for f in *.py; do echo $f; done", "K", ""),
    ("for f in *.py; do echo x >> $f; done", "K", "A"),
]  # fmt: skip


@pytest.mark.parametrize(("line", "where", "said"), CHECKOUT_LINES)
def test_a_command_that_writes_in_the_owners_checkout_given_to_read_is_refused(
    tmp_path, line, where, said
):
    """A worker given the owner's checkout to read, acting on its own (yolo) or with no system
    sandbox (Codex; Copilot with it off): its edits there were refused, its shell commands not
    (`echo y >> AGENTS.md` wrote there). What a command writes there is refused as the edits
    are; a program not known run there on files there asks the owner; reads go through."""
    kept, own = tmp_path / "kept", tmp_path / "own"
    (kept / "src").mkdir(parents=True)
    own.mkdir()
    subprocess.run(["git", "init", "-q", str(kept)], check=True)
    for name in ("AGENTS.md", "src/app.py", "tool.py"):
        (kept / name).write_text("x\n")
    command = line.format(K=kept, O=own)
    row = {"role": "worker", "cwd": str(kept if where == "K" else own)}
    checkouts = [{"path": str(kept), "shared": True, "git_dir": str(kept / ".git")},
                 {"path": str(own), "branch": "chief/x"}]  # fmt: skip
    request = SimpleNamespace(kind="shell", full_command_text=command)
    found = policy.owners_checkout(row, request, checkouts)
    expected = {"W": policy.OWNERS_CHECKOUT, "A": policy.OWNERS_CHECKOUT_ASK, "": None}[said]
    assert found == expected, command
    assert said != "W" or "owner's own checkout" in found


def test_a_refused_line_is_about_the_checkout_it_names_else_the_one_it_runs_in(tmp_path):
    """A worker with two checkouts of the owner's to read: the copy it gets for a line refused
    there (Runtime.own_copy) is of the one the line names, else of the one it runs in."""
    one, two, own = tmp_path / "one", tmp_path / "two", tmp_path / "own"
    for folder in (one / "src", two, own):
        folder.mkdir(parents=True)
    kept = [{"path": str(one)}, {"path": str(two)}]
    assert policy.checkout_of(f"echo y >> {two}/a", str(own), kept) == kept[1]
    assert policy.checkout_of(f"cp {one}/a {two}/b", str(two), kept) == kept[1]
    assert policy.checkout_of("make", str(one / "src"), kept) == kept[0]
    assert policy.checkout_of("make", str(two), kept) == kept[1]
    assert policy.checkout_of("make", str(own), kept) == kept[0]
    assert policy.checkout_of("make", "", kept[1:]) == kept[1]
    assert policy.checkout_of("make", str(own), []) is None


@pytest.mark.skipif(sys.platform == "win32", reason="real bash")
@pytest.mark.timeout(120, func_only=True)  # (Scaled to the runner: paced.)
def test_what_the_checkout_check_lets_through_writes_nothing_there_in_real_bash(
    tmp_path, capsys, paced
):
    """Lines of a safe vocabulary (tests/checkout_oracle.py), generated with a fixed seed, and
    each command of it on its own, run in real bash in a sandbox with the owner's checkout given
    to read: none the check lets through changed it (its files, index, HEAD, branches, stashes,
    config). The rates are printed (pytest -s)."""
    import checkout_oracle as oracle

    began = time.monotonic()
    generator = oracle.Generator(0)
    cases = [generator.line() for _ in range(300)] + oracle.seeds()
    outcomes = oracle.run(policy, cases, tmp_path)
    unsound = [(o.where, o.line) for o in outcomes if o.changed and not o.verdict]
    with capsys.disabled():
        took = time.monotonic() - began
        sys.stdout.write(
            f"\ncheckout oracle: {oracle.rates(outcomes)} ({took:.0f} s; runner pace "
            f"{paced:.1f}: {120 * paced:.0f} s allowed)\n"
        )
    assert not unsound, unsound
    assert sum(o.changed for o in outcomes) > 50, "the vocabulary writes there often enough"
