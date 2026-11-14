"""A soundness oracle for the push check (src/chiefofstaff/policy.py), against real bash.

Command lines made of a small, safe vocabulary (variables, `read`, `export` and the other
builtins that set them, `cd` between two fake repositories, substitutions, `eval`, loops,
conditions, groups, `git push` in many shapes) run in `bash --norc --noprofile -c`, in a
sandbox where `git` is a shim: it answers from fake repositories (one on main, one on a
feature branch) and, for `push`, writes down where it ran and what it was given instead of
pushing. From that, what bash pushed is known. The property: a line the check lets through
(under Ask first, with the same fake repositories for the branch it asks of git) pushed
nothing to main or master.

PATH holds the shim only (and `sh`/`bash`): any other program is "not found", so nothing
outside the sandbox is touched. `python tests/policy_oracle.py --cases N --seed S` runs N
generated lines and prints the counts.
"""

from __future__ import annotations

import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

DEFAULT = {"main", "master"}
BASH = shutil.which("bash") or "/bin/bash"
SH = shutil.which("sh") or "/bin/sh"  # (Each as found on PATH: another may be put first.)
ZSH = shutil.which("zsh")  # Codex runs commands in zsh (`zsh -lc`): the lines run in it too.
# Commands that run another, put in the sandbox's PATH (by their full path; timeout as a script
# that runs its command: GNU timeout puts it in a process group of its own, which a run would
# not wait for, and a push it made late would be read as the next line's).
WRAPPERS = ("env", "nice", "xargs", "find", "timeout", "cat")  # (cat: what it feeds a shell.)
TIMEOUT = r"""#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in -s|-k|--signal|--kill-after) shift 2 ;; -*) shift ;; *) break ;; esac
done
shift
exec "$@"
"""

# The git shim: global options (-C, as git goes there: a link's `..` is its target's parent; -c,
# --git-dir, --work-tree), then a few commands answered
# from `.git/HEAD` and `.git/branches` of the folder's fake repository; a push is written to
# $PUSHES: its folder's top, its branch, `-c` settings, git's own variables, and its words,
# separated by tabs (words by \x1f), each push ended by \x1e and a newline.
SHIM = r"""#!/bin/sh
config=""
other=""
while [ $# -gt 0 ]; do
  case "$1" in
    -C) cd -P "$2" 2>/dev/null || exit 128; shift 2 ;;
    -c) config="$config$2;"; shift 2 ;;
    --git-dir|--work-tree) other=1; shift 2 ;;
    --git-dir=*|--work-tree=*) other=1; shift ;;
    --config-env*|--namespace*|--super-prefix*) other=1; shift ;;
    -*) shift ;;
    *) break ;;
  esac
done
[ -n "$GIT_DIR$GIT_WORK_TREE$GIT_CONFIG_PARAMETERS" ] && other=1
i=0
while [ "$i" -lt "${GIT_CONFIG_COUNT:-0}" ] 2>/dev/null; do
  eval "key=\${GIT_CONFIG_KEY_$i}; value=\${GIT_CONFIG_VALUE_$i}"
  config="$config$key=$value;"
  i=$((i + 1))
done
top=$(pwd -P)
while [ ! -d "$top/.git" ] && [ "$top" != "/" ]; do top=$(dirname "$top"); done
[ -d "$top/.git" ] || { [ "$1" = push ] && printf 'NOREPO\036\n' >> "$PUSHES"; exit 128; }
head=$(sed -n 's|^ref: refs/heads/||p' "$top/.git/HEAD")
command="$1"
[ $# -gt 0 ] && shift
case "$config" in
  *"alias.$command="*)
    value=${config#*alias.$command=}
    case "${value%%;*}" in *push*) command=alias ;; esac ;;
esac
case "$command" in
  push|alias)
    record=$(printf 'PUSH\t%s\t%s\t%s\t%s\t%s' "$top" "$head" "$config" "$other" "$command")
    separator=$(printf '\037')
    for word in "$@"; do record="$record$separator$word"; done
    printf '%s\036\n' "$record" >> "$PUSHES" ;;
  rev-parse)
    case "$*" in
      *--show-toplevel*) echo "$top" ;;
      *"--abbrev-ref HEAD"*) echo "${head:-HEAD}" ;;
      *--git-dir*) echo "$top/.git" ;;
      *) exit 128 ;;
    esac ;;
  symbolic-ref)
    named=""; count=0
    while [ $# -gt 0 ]; do
      case "$1" in
        -m) shift ;;
        -*) ;;
        *) count=$((count + 1)); [ "$count" = 2 ] && named="$1" ;;
      esac
      shift
    done
    case "$named" in
      refs/heads/?*) echo "ref: $named" > "$top/.git/HEAD" ;;
      "") [ -n "$head" ] && echo "$head" || exit 128 ;;
    esac ;;
  branch)
    case "$1" in
      --show-current) echo "$head" ;;
      -*|"") ;;
      *) echo "$1" >> "$top/.git/branches" ;;
    esac ;;
  checkout|switch)
    new=""; target=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -b|-B|-c|-C|--create|--orphan) new="$2"; shift 2; continue ;;
        --create=*) new="${1#--create=}" ;;
        --) target=""; break ;;
        -) target="$(cat "$top/.git/previous" 2>/dev/null)" ;;
        -*) ;;
        *) [ -z "$target" ] && target="$1" ;;
      esac
      shift
    done
    if [ -n "$new" ]; then
      echo "$new" >> "$top/.git/branches"; target="$new"
    elif [ -n "$target" ] && ! grep -qx -- "$target" "$top/.git/branches"; then
      target=""
    fi
    if [ -n "$target" ]; then
      echo "$head" > "$top/.git/previous"
      echo "ref: refs/heads/$target" > "$top/.git/HEAD"
    fi ;;
  *) ;;
esac
exit 0
"""


# The shim's own tools, by their full path (the lines run with only the shim on PATH).
SHIM_TEXT = SHIM
for _tool in ("sed", "dirname", "grep", "cat"):
    _path = shutil.which(_tool) or f"/usr/bin/{_tool}"
    SHIM_TEXT = SHIM_TEXT.replace(f" {_tool} ", f" {_path} ").replace(f"$({_tool} ", f"$({_path} ")


def make_repository(folder: Path, branch: str):
    (folder / "sub").mkdir(parents=True, exist_ok=True)
    (folder / "a" / "b").mkdir(parents=True, exist_ok=True)
    (folder / ".git").mkdir(exist_ok=True)
    (folder / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    (folder / ".git" / "branches").write_text("main\nfeat\nmaster\nwb/x\n")
    (folder / ".git" / "previous").write_text("")


@dataclass
class Sandbox:
    """Two fake repositories (`main/` on main, `feat/` on feat, each with `sub/`), a folder in
    no repository (`plain/`), and the shim."""

    root: Path
    runs: int = 0

    def __post_init__(self):
        bin_folder = self.root / "bin"
        bin_folder.mkdir(parents=True, exist_ok=True)
        shim = bin_folder / "git"
        shim.write_text(SHIM_TEXT)
        shim.chmod(0o755)
        for shell, found in (("sh", SH), ("bash", BASH), ("zsh", ZSH)):
            link = bin_folder / shell
            if found and not link.exists():
                link.symlink_to(found)  # (zsh, where there is one: `zsh -c` from bash too.)
        for tool in WRAPPERS:
            link, found = bin_folder / tool, shutil.which(tool)
            if link.exists() or link.is_symlink():
                continue
            if tool == "timeout":
                link.write_text(TIMEOUT)
                link.chmod(0o755)
            elif found:
                link.symlink_to(found)
        (self.root / "plain").mkdir(exist_ok=True)
        self.reset()
        # Links: in each checkout, `l` to its own a/b (`cd l/..` reads as the checkout, `git -C
        # l/..` goes to a); `m` to the other checkout, `n` into it (`cd n/..`: this checkout,
        # `git -C n/..` and `cd -P n/..`: the other).
        for name, other in (("main", "feat"), ("feat", "main")):
            for link, target in (("l", "a/b"), ("m", f"../{other}"), ("n", f"../{other}/sub")):
                path = self.root / name / link
                if not path.is_symlink():
                    path.symlink_to(target)

    def reset(self):
        make_repository(self.root / "main", "main")
        make_repository(self.root / "feat", "feat")

    def folder(self, name: str) -> Path:
        return (self.root / name).resolve()

    def run(self, line: str, start: str, shell: str = "bash") -> list[str]:
        """Run a line in bash (or zsh, without its startup files: as Codex's `zsh -lc` but for
        the owner's profile) from the folder `start`: what the shim was asked to push."""
        self.reset()
        self.runs += 1  # Each run's own record (one a process left behind wrote is not read).
        pushes = self.root / f"pushes-{self.runs}"
        pushes.write_text("")
        tools = self.root / "bin"
        environment = {
            "PATH": str(tools),
            "HOME": str(self.root),
            "PUSHES": str(pushes),
            "LANG": "C",
        }
        run = (
            [ZSH, "-f", "-c", line]
            if shell == "zsh"
            else [BASH, "--norc", "--noprofile", "-c", line]
        )
        process = subprocess.Popen(  # In a group of its own: its jobs (`&`) are waited for.
            run,
            cwd=self.folder(start),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        for _ in range(400):
            try:
                os.killpg(process.pid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.005)
        records = [record for record in pushes.read_text().split("\x1e\n") if record]
        pushes.unlink()
        return records

    def branches(self, folder: str) -> set[str]:
        """What current_branches says of a folder here: its fake repository's branch."""
        path = Path(folder)
        if "\x00" in folder or not path.is_dir():
            return set()
        path = path.resolve()
        for top in (path, *path.parents):
            if (top / ".git").is_dir():
                head = (top / ".git" / "HEAD").read_text().strip()
                return {head.removeprefix("ref: refs/heads/")} if head.startswith("ref:") else set()
        return set()


def pushed(record: str) -> set[str]:
    """The branches one push the shim wrote down updates ("?" when it cannot say)."""
    if record == "NOREPO":
        return set()
    _, _top, head, config, other, words = record.split("\t", 5)
    command, *words_list = words.split("\x1f")
    if command == "alias":
        return {"?"}  # A push through an alias: what, not followed here.
    if other:
        head = "?"  # Another repository's (GIT_DIR): its branch, not known here.
    settings: dict[str, str] = {}
    for item in filter(None, config.split(";")):
        key, _, value = item.partition("=")
        settings[key] = value
    dry = tags = every = deleting = False
    positional: list[str] = []
    valued = False
    for word in words_list:
        if valued:
            valued = False
            continue
        if word == "--":
            continue
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            dry = (dry or name == "--dry-run") and name != "--no-dry-run"
            tags = tags or name == "--tags"
            every = every or name in {"--all", "--mirror", "--branches"}
            deleting = deleting or name == "--delete"
            valued = (
                name in {"--push-option", "--repo", "--receive-pack", "--exec"} and "=" not in word
            )
            continue
        if word.startswith("-") and len(word) > 1:
            for at, letter in enumerate(word[1:], 1):
                if letter == "o":
                    valued = at == len(word) - 1
                    break
                dry = dry or letter == "n"
                deleting = deleting or letter == "d"
            continue
        positional.append(word)
    if dry:
        return set()
    if every or settings.get("push.default", "").lower() == "matching":
        return {"main", "?"}
    remote = positional[0] if positional else "origin"
    refspecs = positional[1:]
    if not refspecs:
        configured = [value for key, value in settings.items()
                      if key.lower() == f"remote.{remote.lower()}.push"]  # fmt: skip
        if configured:
            refspecs = configured
        elif tags or deleting:
            return set()
        else:
            return {head} if head else set()
    found = set()
    for refspec in refspecs:
        refspec = refspec.lstrip("+")
        destination = refspec.rsplit(":", 1)[-1] if ":" in refspec else refspec
        if not destination and ":" in refspec:
            continue
        if destination in {"HEAD", "@"}:
            found |= {head} if head else set()
            continue
        if "*" in destination:
            found.add("?")
            continue
        if destination.startswith("refs/tags/"):
            continue
        for prefix in ("refs/heads/", "heads/"):
            destination = destination.removeprefix(prefix)
        if destination:
            found.add(destination)
    return found


# The generator: lines of this vocabulary only.
NAMES = ["B", "B", "B", "B", "PWD", "D", "D", "IFS", "CDPATH", "HOME"]
VALUES = ["main", "feat", "wb/x", "master", "", "'feat main'", "HEAD", "../main", "../feat",
          "sub", "'a b'", '"$PWD"', "x:main", "$(echo main)", '"$(git branch --show-current)"',
          "$(git rev-parse --abbrev-ref HEAD)", "/", "..", "'$(echo main)'", "$B",
          '"$PWD/../main"', "wb/main", "x/main", "xmain", "main.y", "MAIN"]  # fmt: skip
SPELLINGS = {
    "read": ["read", "r\\ead", "'re'ad", '"read"', "\\read"],
    "export": ["export", "ex\\port", "'export'"],
    "declare": ["declare", "'declare'", "typeset"],
    "unset": ["unset", "un\\set", "'unset'"],
    "readonly": ["readonly"],
    "local": ["local"],
}
PREFIXES = ["", "", "", "command ", "command -p ", "builtin ", "time -p "]
FOLDERS = ["../main", "../feat", "sub", "..", "../main/sub", "../feat/sub", "$PWD", '"$PWD"',
           "${PWD}", '"$(pwd)"', '"$(git rev-parse --show-toplevel)"',
           '"$(git rev-parse --show-toplevel)/.."', '"$(git rev-parse --show-toplevel)/../main"',
           "$D", '"$D"', "../plain", "nowhere", "-", "../main ../feat", "main", "feat", "~",
           "~/main", "-P ../main", "-- ../feat", "", '"$PWD/../main"', "`pwd`/../main",
           '"$(cd ../main && pwd)"', "$(echo ../main)", "l", "l/..", "l/../..", "l/../../main",
           "m", "m/..", "m/sub", "n/..", "-P n/..", "-P l/..", "-P l/../..", "-L l/..", "$PWD:h",
           "${PWD:h}", "+1", "-1", "../main sub"]  # fmt: skip
REFS = ["main", "feat", "wb/x", "master", "HEAD", "$B", '"$B"', "${B}", "HEAD:main",
        "HEAD:$B", "+feat", "refs/heads/main", "HEAD:feat", "$D", "@", "$B:feat", "$(echo main)",
        '"$(git branch --show-current)"', "feat:$B", "${B}x", "'$B'", "v1.0", "--tags",
        "HEAD:refs/heads/$B", "$B:t", "${B:t}", "$B[2,-1]", "$B:r", "$B:s/x//", "$B:l",
        "${B[1]}", "HEAD:$B:t"]  # fmt: skip
PUSH_ARGS = ["", "origin", "-u origin HEAD", "origin HEAD", "--dry-run origin main",
             "-f origin feat", "--set-upstream origin", "-n", "--all origin", "-o x origin",
             "origin -n main", "--no-dry-run -n origin", "-n --no-dry-run origin", "$B",
             "origin $B $B", "--delete origin main"]  # fmt: skip
SWITCHES = ["git checkout -b wb/new", "git switch -c fix", "git checkout main",
            "git checkout feat", "git checkout -", "git switch feat", "git branch wb/y",
            "git checkout wb/y", "git checkout -B main", "git switch -C feat", "git checkout -- x",
            "git checkout .", "git symbolic-ref HEAD refs/heads/main",
            "git symbolic-ref HEAD refs/heads/feat", "git symbolic-ref -q HEAD",
            "echo main | xargs git checkout"]  # fmt: skip
SEPARATORS = ["; ", "; ", " && ", " || ", " | ", "\n", " & "]


class Generator:
    def __init__(self, seed: int):
        self.chance = random.Random(seed)  # noqa: S311  (a fixed sample, not a secret)

    def pick(self, items):
        return self.chance.choice(items)

    def name(self) -> str:
        return self.pick(NAMES)

    def value(self) -> str:
        return self.pick(VALUES)

    def folder(self) -> str:
        return self.pick(FOLDERS)

    def setter(self) -> str:
        name, value = self.name(), self.value()
        kind = self.pick(["assign", "assign", "assign", "append", "quoted", "escaped",
                          "prefix", "read", "read", "export", "declare", "unset",
                          "readonly", "local", "printf", "for"])  # fmt: skip
        prefix = self.pick(PREFIXES)
        if kind == "assign":
            return f"{name}={value}"
        if kind == "append":
            return f"{name}+={value}"
        if kind == "quoted":
            return f'"{name}"={value}'
        if kind == "escaped":
            return self.pick([f"\\{name}={value}", f"{name}\\={value}"])
        if kind == "prefix":
            return f"{name}={value} {self.simple(depth=3)}"
        if kind == "read":
            spelled = self.pick(SPELLINGS["read"])
            return self.pick([
                f"{prefix}{spelled} {name} <<< {value or 'x'}",
                f"<<<{value or 'x'} {prefix}{spelled} {name}",
                f"0<<<{value or 'x'} {spelled} {name}",
                f"{prefix}{spelled} -r {name} <<< {value or 'x'}",
                f"{spelled} {name} <<EOF\n{value or 'x'}\nEOF",
            ])  # fmt: skip
        if kind == "printf":
            return f"{prefix}printf -v {name} %s {value or 'x'}"
        if kind == "for":
            return f"for {name} in {value or 'x'} {self.value() or 'y'}; do :; done"
        spelled = self.pick(SPELLINGS[kind])
        if kind == "unset":
            return f"{prefix}{spelled} {name}"
        return self.pick([f"{prefix}{spelled} {name}={value}", f"{prefix}{spelled} {name}"])

    def push(self) -> str:
        git = self.pick(["git", "git", "git", "command git", f"git -C {self.folder()}",
                         "\\git", "env git", f"env -C {self.folder()} git",
                         "env --chdir=../feat git", "timeout 5 git", "nice -n 5 git",
                         "nice git", f"env -vC {self.folder()} git",
                         f"env -iC {self.folder()} PATH=\"$PATH\" git",
                         f"env -P ~/bin -C {self.folder()} git", "env -i PATH=\"$PATH\" git",
                         "env -0 git", "=git"])  # fmt: skip
        arguments = self.pick(PUSH_ARGS + [f"origin {self.pick(REFS)}"] * 4)
        pushing = f"{git} push {arguments}".rstrip()
        roll = self.chance.random()
        if roll < 0.04:  # Its words, or more of them, from a command that adds them.
            value = self.value() or "main"
            return self.pick([
                f"echo {value} | xargs {pushing}",
                f"echo {value} | xargs -I{{}} git push origin {{}}",
                f"echo {value} | xargs -n1 {pushing}",
                f"echo | xargs {pushing}",
            ])  # fmt: skip
        if roll < 0.06:
            return self.pick([
                f"find {self.pick(['../main/sub', '../feat/sub', 'sub'])} -maxdepth 0 "
                f"-execdir {pushing} \\;",
                f"find {self.pick(['../main', '../feat', '.'])} -mindepth 1 -maxdepth 1 "
                f"-name sub -execdir {pushing} \\;",
                f"env -S {shell_quote(pushing)}",
                f"find {self.pick(['../main', '../feat', '.', '../main/sub', '../feat/sub'])} "
                f"-maxdepth 0 -exec git -C {self.pick(['{}', '{}/..', '../feat/{}/..'])} "
                f"push {arguments} \\;",
            ])  # fmt: skip
        if roll < 0.08:  # An alias for it (bash expands one only once told to; zsh always).
            alias, rest = self.pick([(pushing, ""), (git, f" push {arguments}".rstrip())])
            return f"shopt -s expand_aliases 2>/dev/null; alias g={shell_quote(alias)}\ng{rest}"
        return pushing

    def simple(self, depth: int = 0) -> str:
        roll = self.chance.random()
        if roll < 0.3:
            return self.setter()
        if roll < 0.48:
            mover = self.pick(["cd", "cd", "cd", "cd", "pushd", "popd"])
            if mover == "popd":
                return f"{self.pick(PREFIXES)}popd"
            return f"{self.pick(PREFIXES)}{mover} {self.folder()}".strip()
        if roll < 0.78 or depth > 2:
            return self.push()
        if roll < 0.88:
            return self.pick(SWITCHES)
        if roll < 0.92:  # How cd reads `..`, for the rest of the line.
            return self.pick(["set -P", "set +P", "set -o physical", "set +o physical",
                              "setopt chase_links", "setopt chase_dots", "unsetopt chase_dots",
                              "set -o chaselinks", "setopt CHASE_DOTS", "shopt -so physical",
                              "shopt -s -o physical", "shopt -uo physical", "set -- -P",
                              "set -w", "set +w", "set -eP", "set -o nophysical",
                              "setopt NO_CHASE_LINKS", "options[chaselinks]=on",
                              "emulate zsh -o chaselinks"])  # fmt: skip
        return self.pick(["true", "false", ":", "echo x", "echo read B", "git status"])

    def statement(self, depth: int = 0) -> str:
        if depth > 2 or self.chance.random() < 0.65:
            return self.simple(depth)
        inner = self.statements(depth + 1)
        kind = self.pick(["if", "else", "for", "case", "subshell", "group", "substitution",
                          "quoted", "eval", "eval1", "while", "case", "subshell", "process",
                          "source", "shell", "fed"])  # fmt: skip
        if kind == "if":
            return f"if {self.pick(['true', 'false'])}; then {inner}; fi"
        if kind == "else":
            return f"if {self.pick(['true', 'false'])}; then :; else {inner}; fi"
        if kind == "for":
            return f"for i in 1 2; do {inner}; done"
        if kind == "case":
            return self.pick([
                f"case x in x) {inner};; esac",
                f"case x in (x) {inner};; esac",
                f"case x in y|x) {inner};; esac",
                f"case x in (y) :;; (x) {inner};; esac",
                f"case x in y) :;; *) {inner};; esac",
                f"case x in\n  y) :\n  ;;\n  *) {inner}\nesac",
                f"case x in y) :;; x) {inner}; esac",
            ])  # fmt: skip
        if kind == "subshell":
            return self.pick([f"( {inner} )", f"({inner})"])
        if kind == "process":
            return f": <({inner})"
        if kind == "source":  # (A file it runs is not read: one that is not there.)
            return f"{self.pick(['.', 'source'])} ./nothing; {inner}"
        if kind == "group":
            return f"{{ {inner}; }}"
        if kind == "substitution":
            return f"x=$({inner})"
        if kind == "quoted":
            return f'echo "$({inner})"'
        if kind == "eval":
            return f"eval {shell_quote(inner)}"
        if kind == "eval1":
            return f'eval "{inner}"'
        if kind == "fed":  # A script fed to a shell: on its input, or as a file.
            shell = self.pick(["bash", "sh", "zsh -f", "bash -s", "env bash", "xargs -0 sh -c"])
            quoted = shell_quote(inner)
            return self.pick([
                f"echo {quoted} | {shell}", f"printf '%s\\n' {quoted} | {shell}",
                f"{shell} <<< {quoted}", f"{shell} <<'END'\n{inner}\nEND",
                f"cat <<'END' | {shell}\n{inner}\nEND", f"{shell} <(echo {quoted})",
                f"{shell} < <(echo {quoted})", f"source <(echo {quoted})",
                f". /dev/stdin <<< {quoted}",
            ])  # fmt: skip
        if kind == "shell":  # Another shell's line (zsh's from bash too); with options of cd's.
            shell = self.pick(["sh -c", "bash -c", "bash -P -c", "bash -o physical -c",
                               "zsh -f -c", "zsh -f -w -c", "emulate sh -c",
                               "find"])  # fmt: skip
            if shell == "find":
                return f"find . -maxdepth 0 -exec sh -c {shell_quote(inner)} \\;"
            return f"{shell} {shell_quote(inner)}"
        return f"while false; do {inner}; done"

    def statements(self, depth: int = 0) -> str:
        parts = [self.statement(depth)]
        for _ in range(self.chance.randint(0, 3 if depth == 0 else 1)):
            separator = self.pick(SEPARATORS if depth == 0 else ["; ", " && ", " || "])
            parts.append(separator + self.statement(depth))
        return "".join(parts)

    def line(self) -> tuple[str, str]:
        start = self.pick(["main", "feat", "main/sub", "feat/sub"])
        tail = '\necho "x' if self.chance.random() < 0.03 else ""  # Not parsed: run up to it.
        return self.statements() + tail, start


def shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


@dataclass
class Outcome:
    line: str
    start: str
    allowed: bool
    targets: set[str]

    @property
    def unsound(self) -> bool:
        return self.allowed and bool(self.targets & (DEFAULT | {"?"}))


# Lines left out of the seeds, though nothing but git and the shells is on PATH: programs by
# their full path, writes outside the sandbox, builtins that reach other processes.
UNSAFE_SEEDS = re.compile(r"/bin/|/usr/|/tmp|sudo|\bkill\b|>\s*[/~](?!dev/null)|\bexec\b|ulimit")


# Lines the oracle found let through though bash pushed main (each from its folder): kept.
FOUND = [
    # Found against 767e08f (seeds 1-12).
    ("eval ''\\''re'\\''ad B <<< x; git -C \"$D\" push origin'", "main"),
    ("while false; do cd ../feat && command cd \"$(git rev-parse --show-toplevel)/..\"; done\n"
     "B= && git push origin @ & for i in 1 2; do { cd ../plain; }; x=$(read D <<< main); done",
     "main"),
    ("case x in x) git checkout main;; esac && git -C ../feat/sub push -o x origin", "feat"),
    ("if true; then git checkout main; fi || git checkout .; x=$(git push origin @)", "feat"),
    ("for i in 1 2; do if true; then git push origin HEAD; cd ../main; fi; done", "feat"),
    ("x=$(git checkout -B main || command read IFS <<< sub) && command git push origin HEAD",
     "feat/sub"),
    ("B+='feat main'; git push $B", "feat/sub"),
    # Found by the 200k run (seed 100).
    ('if true; then :; else eval \'for B in master $(git rev-parse --abbrev-ref HEAD); do :; done || command -p export IFS="$PWD/../main"\'; fi || PWD=$B || git checkout -b wb/new && \\git push -o x origin', 'main'),  # noqa: E501
    ('git -C ../feat push origin "$(git branch --show-current)"', 'main'),
    ("case x in x) while false; do command readonly B; done;; esac; B='$(echo main)' \\git push -n | ( time -p cd ~/main; \\git push --set-upstream origin )", 'feat/sub'),  # noqa: E501
    ("git push origin & <<<master 're'ad D && builtin cd\necho x", 'main/sub'),
    ('for i in 1 2; do git push; done & git checkout -B main', 'feat'),
    ('\\git push origin & <<<../feat command read B && case x in x) false;; esac', 'main/sub'),
    ('git -C ../feat/sub push --set-upstream origin || git status & git checkout -B main\ncommand unset CDPATH', 'feat'),  # noqa: E501
    ('echo read B | git push -n\n( for i in 1 2; do { git -C ../feat push origin "$(git branch --show-current)" && unset HOME; }; done )', 'main'),  # noqa: E501
    ('HOME=x:main git push origin HEAD:feat || git switch -c fix && command git push', 'main/sub'),
    ('git status || git checkout -b wb/new && git push origin HEAD', 'main'),
    ('\\git push -o x origin & git checkout -B main', 'feat'),
    # Found by the second 200k run (seed 200).
    ('( { time -p cd ~/main && case x in x) \\git push -o x origin || git push --set-upstream origin;; esac; } )', 'feat/sub'),  # noqa: E501
    ('cd ../feat/sub && builtin cd & git push origin HEAD', 'main'),
    ('command -p r\\ead -r D <<< $(echo main); IFS=.. time -p cd ../feat/sub; x=$(0<<<$(echo main) \\read B)\n( git push origin HEAD || command declare CDPATH=wb/main )', 'main'),  # noqa: E501
    # Found by the third 200k run (seed 300): a quoted empty value is a word (`git -C ""`).
    ('B=\'feat main\'\nPWD=; git -C "$PWD" push -o x origin', 'main/sub'),
    # Found while checking the tests' mutants: a case pattern's parentheses taken for a
    # subshell's; what a subshell, a function, a `cd` that may not run, change.
    ('case x in (a) :;; (b) :;; esac; cd ../main; case y in a|b) :;; esac; git push', 'feat'),
    ('(cd ../feat; case x in (a) :;; (b) :;; esac); git push', 'main'),
    ('(B=feat); git push origin $B', 'main'),
    ('(cd ../feat); git push', 'main'),
    ('for d in ../feat; do (cd $d); git push; done', 'main'),
    ('true || cd ../feat && git push', 'main'),
    ('f() { cd ../main; }; cd ../feat; f; git push', 'feat'),
    ('. /dev/stdin <<< "cd ../main"; git push', 'feat'),
    ('D=; git -C "$D" push origin', 'main'),
    # Found by seed 26 (8000 cases): `$PWD` in code `eval` runs, put in where it was written.
    ('( eval ": <(command -p cd ../feat && git -C $PWD push)" ); command pushd "$(git rev-parse --show-toplevel)/../main"; echo x; . ./nothing; while false; do case x in y) :;; *) cd ../main ../feat;; esac; r\\ead D <<< master; done', 'main'),  # noqa: E501
    ('eval "cd ../feat && git -C $PWD push"', 'main'),
    ('cd ../main; eval "cd ../feat; eval \\"git -C $PWD push\\""', 'feat'),
    # Found while checking the tests' mutants (round 19): a function called after a `cd` to a
    # folder named in full; a subshell's end in a line bash stops reading at its next line.
    ('f() { cd ../main; }; cd ~/feat; f; git push', 'feat'),
    ('(cd ../feat) && git push\nesac', 'main'),
    # Found running the oracle in bash 5.2 (Debian's, as the CI's): a case in a substitution
    # (bash 3.2 cannot parse one); and lines bash runs before one it cannot parse.
    ('echo "$(cd ../main; case x in y) :;; x) \\git push --set-upstream origin; (git -C "$D" push -n || command git push origin HEAD); esac)"', 'main'),  # noqa: E501
    ('echo "$(case x in y) :;; x) if false; then :; else time -p typeset CDPATH && git push --delete origin main; fi && readonly D=master; esac && git -C ~/main push --set-upstream origin)"', 'feat/sub'),  # noqa: E501
    ('cd ../main\ngit push\necho "x', 'feat'),
    ('git -C ../main push\necho "x', 'feat'),
    ('git checkout main\ngit push\necho "x', 'feat'),
    # Found by the fourth 200k run (seed 400, on c9b55fb): a `cd` in a list put in the
    # background holds for the rest of that list (its own subshell).
    ('cd ~/main && command git push & time -p pushd ../feat', 'feat/sub'),
    ("pushd $(echo ../main) && \\git push -o x origin & x=$(builtin cd nowhere && (readonly PWD='feat main' && 0<<<sub read D)); git branch wb/y", 'feat'),  # noqa: E501
    ('command cd ../main/sub && CDPATH+=master && git push -u origin HEAD & echo x', 'feat'),
    ('git push --set-upstream origin & CDPATH="$(git branch --show-current)" pushd ~/main && git push -u origin HEAD & builtin cd "$(cd ../main && pwd)"', 'feat/sub'),  # noqa: E501
    ('cd ../main && git push & echo x', 'feat'),
    ('cd ../feat && (true) & git push', 'main'),
    # Found by seeds 33, 34, 36 (6000 cases each) once the generator ends some lines unparsed:
    # read loosely, a variable, an empty word, git's options, a push split from its git.
    ('( echo "$(command -p cd)" && git push $B ); echo read B; ( for B in \'a b\' \'a b\'; do :; done || : <(builtin \'export\' D=\'a b\'; builtin pushd ${PWD}) ) | case x in (x) x=$(eval "time -p cd $(echo ../main)"); { x=$(D="$PWD"); };; esac\necho "x', 'main'),  # noqa: E501
    ('git push -n | git push -n | popd; ( PWD= && command git push -o x origin )\necho "x', 'main'),
    ('x=$(source ./nothing; B=main && . ./nothing; command "read" -r D <<< ..) & git -C "$(cd ../main && pwd)" push --delete origin main | time -p pushd `pwd`/../main\necho "x', 'main/sub'),  # noqa: E501
    ('x=$(local B=..; true) || \\git push --no-dry-run -n origin && local PWD; ( git push --set-upstream origin )\necho "x', 'main'),  # noqa: E501
    ('B=main\ngit push origin $B\necho "x', 'feat'),
    ('git -c remote.origin.push=refs/heads/main push\necho "x', 'feat'),
    # Found while checking the tests' mutants (round 20): a remote in a variable, read
    # loosely; a `cd` put in the background in a line whose parentheses are not paired.
    ('R=\'origin main\'\ngit push $R\necho "x', 'feat'),
    ('cd ../feat & git push\nesac', 'main'),
    # Found by the fifth 200k run (seed 500, on 4ff270e): a switch in a subshell or compound
    # command that is part of a pipe runs alongside the pipe's pushes.
    ('case x in x) . ./nothing; git push; git push origin "$(git branch --show-current)"; echo "$(builtin cd "$(git rev-parse --show-toplevel)/.." || "B"=..)";; esac | ( git checkout -B main )', 'feat'),  # noqa: E501
    ('git push | ( git checkout -B main )', 'feat'),
    # Found by a 60k run in bash 5.2 (seed 800): a compound command's `;` ends no list around it.
    ('B=/ || if false; then time -p popd; fi & command git push origin $B $B', 'main'),
    # Found by the sixth 200k run (seed 700, on 6f3019b): a process substitution runs
    # alongside the line; a loop's `;` ends no list put in the background around it.
    ('source ./nothing; : <(git push -n --no-dry-run origin && case x in (x) git push origin HEAD;; esac) && case x in (y) :;; (x) popd;; esac; git checkout -B main', 'feat/sub'),  # noqa: E501
    ('command pushd ../feat/sub || for B in feat wb/main; do :; done & declare HOME=$B & git push --set-upstream origin', 'main'),  # noqa: E501
    (': <(git push) ; git checkout -B main', 'feat'),
    # The final review of 86c7d55: commands that run git (env -C, xargs, find -execdir), a
    # link's `..` as cd reads it, HEAD pointed at a branch (symbolic-ref).
    ('env -C ../main git push', 'feat'),
    ('echo main | xargs git push origin', 'feat'),
    ('echo main | xargs git push origin feat', 'feat'),
    ('echo main | xargs git checkout; git push', 'feat'),
    ('cd l && cd ../../main && git push', 'feat'),
    ('git symbolic-ref HEAD refs/heads/main && git push', 'feat'),
    ('find ../main/sub -maxdepth 0 -execdir git push \\;', 'feat'),
    ("env -C ../main -S 'git push'", 'feat'),
    ('find sub -maxdepth 0 -execdir nice git push origin \\; && PWD=sub; popd', 'main'),
    ('cd ../feat; true || cd ../main/sub && true; cd -; git push', 'main'),
    ('cd ../feat; (cd ../main); cd -; git push', 'main'),
    ('echo main | xargs -I R git push origin R', 'feat'),
    ('git -C n/.. push', 'feat'),
    ('cd -P n/.. && git push', 'feat'),
    ("X=/dev/stdin; . \"$X\" <<< 'cd ../main'; git push", 'feat'),
    ('git push origin ${B:t}', 'main'),
    ('env -C ../feat sh -c "git -C $PWD push"', 'main'),
    # Found while checking the tests' mutants (round 23).
    ('cd n/.. && git push', 'main'),
    ('cd n && cd .. && git push', 'main'),
    ('cd -q ../feat; git push', 'main'),
    ('find ../main -mindepth 1 -maxdepth 1 -name sub -execdir git push \\;', 'feat'),
    # Found by the 200k runs on 195971b (bash 5.2 in Debian, zsh): `pwd` says the folder
    # whatever PWD is set to; a substitution runs in the shell's folder, not env -C's;
    # xargs before git in a line read loosely.
    ("PWD='a b' && git -C `pwd`/../main push origin HEAD", 'main'),
    ('env -C ../feat/sub git push origin "$(git branch --show-current)"', 'main'),
    ('env -C m/sub git push origin "$(git branch --show-current)"', 'main'),
    ('echo "$(echo main | xargs -n1 git push origin v1.0)" | true\necho "x', 'main/sub'),
    ('CHIEFOFSTAFF_TOP=../feat; cd "$(git rev-parse --show-toplevel)"; git push', 'main'),
    # The invariant review of a9aa9cc: cd as the system goes for the rest of the line (`set -P`),
    # env's options in clusters and with values, find's `{}`, an alias that pushes.
    ('set -P; cd n && cd .. && git push', 'feat'),
    ('set -o physical; cd n && cd .. && git push', 'feat'),
    ('env -vC ../main git push', 'feat'),
    ('env -P ~/bin -C ../main git push', 'feat'),
    ('find ../main -maxdepth 0 -exec git -C {} push \\;', 'feat'),
    ('find ../main/sub -maxdepth 0 -exec git -C {}/.. push \\;', 'feat'),
    ('find ../main/sub -maxdepth 0 -exec git -C ../feat/{}/.. push \\;', 'feat'),
    # The owner's call on b391f06: a script fed to a shell, on a line that says push.
    ("echo 'git push origin main' | bash", 'feat'),
    ("echo 'git push' | sh", 'main'),
    ("bash <(echo 'git push origin main')", 'feat'),
    ("bash <<< 'git push origin main'", 'feat'),
    ("bash <<'END'\ngit push origin main\nEND", 'feat'),
    ("cat <<'END' | bash\ngit push\nEND", 'main'),
    ("source <(echo 'git push origin main')", 'feat'),
    (". /dev/stdin <<< 'git push origin main'", 'feat'),
    ("echo 'git push origin main' | xargs -0 sh -c", 'feat'),
    ("echo 'git push origin main' | env bash -s", 'feat'),
    ("echo 'git symbolic-ref HEAD refs/heads/main' | bash; git push", 'feat'),
    ("echo 'git push origin main' | zsh -f", 'feat'),
    ("env -S bash <(echo 'git push origin main')", 'feat'),
    ('find . -maxdepth 0 -exec sh -c "echo \'git push origin main\' | bash" \\;', 'feat'),
    ('trap "echo \'git push origin main\' | bash" EXIT', 'feat'),
    ('env -S "sh -c \'echo git push origin main | bash\'"', 'feat'),
    # Their variants: `shopt -o`, the line's mode in substitutions, eval and shells' options,
    # an alias that says no push itself, zsh's lines from bash.
    ('shopt -so physical; cd n && cd .. && git push', 'feat'),
    ('shopt -s expand_aliases\nalias g=git\ng push origin main', 'feat'),
    ('set -P; x=$(cd n && cd .. && git push)', 'feat'),
    ("set -P; eval 'cd n && cd .. && git push'", 'feat'),
    ("bash -P -c 'cd n && cd .. && git push'", 'feat'),
    ("bash -o physical -c 'cd n && cd .. && git push'", 'feat'),
    ("zsh -f -c 'set -w; cd n && cd .. && git push'", 'feat'),
    ("zsh -f --chase-links -c 'cd n && cd .. && git push'", 'feat'),
    ("zsh -f -c 'B=x/main; git push origin $B:t'", 'feat'),
    ("zsh -f -c '=git push origin HEAD'", 'main'),
    ("zsh -f -c 'echo | cd ../main; git push'", 'feat'),
    ("shopt -s expand_aliases\nalias g='git push origin main'\ng", 'feat'),
]  # fmt: skip


# Lines found in zsh (as Codex runs commands), run in zsh: a modifier or a subscript on a
# variable (`$B:t`, `$B[2,-1]`), a pipe's last command run in the shell itself.
FOUND_ZSH = [
    ('B=x/main; git push origin $B:t', 'feat'),
    ('B=x/main; git push origin ${B:t}', 'feat'),
    ('B=xmain; git push origin $B[2,-1]', 'feat'),
    ('B=main.y; git push origin $B:r', 'feat'),
    ('B=MAIN; git push origin $B:l', 'feat'),
    ('B=xmain; git push origin $B:s/x//', 'feat'),
    ('true | cd ../main; git push', 'feat'),
    ('git push origin $B:t', 'main'),
    # Found by the zsh 200k run on c3c270f: zsh's `command` runs no builtin (export).
    ('command -p export B=main.y; : <(timeout 5 git push origin $B $B)', 'main/sub'),
    ('command export B=feat; git push origin $B', 'main'),
    # The invariant review of a9aa9cc: zsh's chase_links and chase_dots, its `=git`.
    ('setopt chase_dots; cd n && cd .. && git push', 'feat'),
    ('setopt chase_links; cd n/.. && git push', 'feat'),
    ('set -o chaselinks; cd n && cd .. && git push', 'feat'),
    ('=git push -u origin HEAD', 'main'),
    ('B=main; =git push origin $B', 'feat'),
    ('set -w; cd n && cd .. && git push', 'feat'),
    ('options[chaselinks]=on; cd n && cd .. && git push', 'feat'),
    ("emulate csh -c 'B=main; git push origin $B'", 'feat'),
    ('emulate zsh -o chaselinks; cd n && cd .. && git push', 'feat'),
    ('setopt chase_dots; x=$(cd n && cd .. && git push)', 'feat'),
    ("setopt chase_dots; eval 'cd n && cd .. && git push'", 'feat'),
    ("echo | eval 'cd ../main'; git push", 'feat'),
    ("source =(echo 'git push origin main')", 'feat'),
    ("=bash <<< 'git push origin main'", 'feat'),
    ('git status\neval "env -i PATH="$PATH" git push -n" & emulate sh -c \'( : && bash < <(echo \'\\\'\'git -C ../main push origin HEAD; env -i PATH="$PATH" git push $B\'\\\'\') ) && false\' | for i in 1 2; do unset HOME || builtin popd; done', 'main'),  # noqa: E501
    ("emulate sh -c \"bash <<< 'git push origin main'\"", 'feat'),
    ('command pushd ../feat/sub || nice -n 5 git push -o x origin', 'main'),
    ('time -p cd -- ../feat; git push origin HEAD || while false; do env git push -o x origin; done\nwhile false; do command cd -P l/../..; done', 'main'),  # noqa: E501
]  # fmt: skip


def seeds() -> list[str]:
    """Every command line the tests refuse or let through (tests/test_policy.py), that keeps to
    what the sandbox can run safely (its PATH has only git and the shells)."""
    sys.path.insert(0, str(Path(__file__).parent))
    import test_policy as cases

    lines = [
        *cases.CORPUS,
        *(shape.format(push) for shape in cases.SHAPES for push in cases.REFUSED + cases.ALLOWED),
        *cases.NORMAL,
    ]
    return sorted({line for line in lines if not UNSAFE_SEEDS.search(line)})


def run(
    policy, cases: list[tuple[str, str]], root: Path, workers: int = 8, shell: str = "bash"
) -> list[Outcome]:
    """Check each (line, start) in its own sandbox, `workers` at a time, run in `shell` (bash,
    or zsh: as Codex runs a worker's commands, and as the check is told); with the check's
    current_branches answered from the sandbox."""
    local = threading.local()
    lock = threading.Lock()
    original = policy.current_branches
    counter = iter(range(10**9))

    def sandbox() -> Sandbox:
        if not hasattr(local, "sandbox"):
            local.sandbox = Sandbox(root / f"w{next(counter)}")
        return local.sandbox

    def branches(folder: str) -> set[str]:
        return local.sandbox.branches(folder)

    def one(case: tuple[str, str]) -> Outcome:
        box = sandbox()
        row = {"role": "worker", "slack": 0, "cwd": str(box.folder(case[1]))}
        request = SimpleNamespace(
            kind="shell", full_command_text=case[0], shell="" if shell == "bash" else shell
        )
        box.reset()
        with lock:  # The check (pure Python) one at a time; bash runs in parallel.
            policy.reading.cache_clear()
            policy.current_branches = branches
            home, os.environ["HOME"] = os.environ.get("HOME"), str(box.root)  # As bash's.
            try:
                allowed = policy.permission_denial(row, request) is None
            finally:
                policy.current_branches = original
                if home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = home
        targets: set[str] = set()
        for record in box.run(case[0], case[1], shell):
            targets |= pushed(record)
        return Outcome(case[0], case[1], allowed, targets)

    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(one, cases))


def main():
    import argparse

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from chiefofstaff import policy

    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--shell", choices=["bash", "zsh"], default="bash")
    arguments = parser.parse_args()
    generator = Generator(arguments.seed)
    cases = [generator.line() for _ in range(arguments.cases)]
    cases += [(line, start) for line in seeds() for start in ("main", "feat")] + FOUND
    cases += FOUND_ZSH if arguments.shell == "zsh" else []
    with tempfile.TemporaryDirectory(prefix="oracle-") as folder:
        outcomes = run(policy, cases, Path(folder), arguments.workers, arguments.shell)
    unsound = [outcome for outcome in outcomes if outcome.unsound]
    allowed = sum(outcome.allowed for outcome in outcomes)
    pushing = sum(bool(outcome.targets) for outcome in outcomes)
    say = sys.stdout.write
    say(f"cases {len(outcomes)}: allowed {allowed}, refused {len(outcomes) - allowed}; "
        f"lines that pushed {pushing}; unsound {len(unsound)}\n")  # fmt: skip
    for outcome in unsound[:40]:
        say(f"UNSOUND from {outcome.start}: {outcome.line!r} pushed {sorted(outcome.targets)}\n")
    sys.exit(1 if unsound else 0)


if __name__ == "__main__":
    main()
