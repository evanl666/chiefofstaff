"""Which actions a session may take on its own. Pure functions, easy to test and to audit (but
for where a `git push` naming no branch goes, its folder's branch and configuration, what an
alias of git's runs, and whether a name a checkout goes to is a branch: asked of git).

These rules are guardrails checked in Chief of Staff's permission callback, not a sandbox: a
worker with a shell can still reach anything its user account can unless OS sandboxing is on
(see SECURITY.md).
"""

import bisect
import collections
import contextvars
import fnmatch
import functools
import hashlib
import itertools
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .roles import SLACK, WORKER

GITHUB_MCP = "github-mcp-server"

# The default branch's usual names (with the branch a bare push would update, asked of the
# repository: Push.branches).
DEFAULT_BRANCHES = frozenset({"main", "master"})
# `git push` options whose value is the next word; and those that push every branch.
PUSH_VALUED = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
PUSH_ALL = frozenset({"--all", "--mirror", "--branches"})
# A push's options that delete branches or hand the remote options of its own (Push.rewrites).
PUSH_REWRITES = frozenset({"--delete", "--prune", "--mirror", "--push-option"})
# The commands that push: an alias whose text names one may push.
PUSHING = ("push", "send-pack", "subtree")
# git's own commands (its builtins but the newest): git runs no alias of one of their names; a
# line's `git <name>` of another name may run an alias git's configuration has (Push.alias).
GIT_COMMANDS = frozenset(
    {"add", "am", "annotate", "apply", "archive", "bisect", "blame", "branch", "bugreport",
     "bundle", "cat-file", "check-attr", "check-ignore", "check-mailmap", "check-ref-format",
     "checkout", "checkout--worker", "checkout-index", "cherry", "cherry-pick", "clean", "clone",
     "column", "commit", "commit-graph", "commit-tree", "config", "count-objects", "credential",
     "credential-cache", "credential-cache--daemon", "credential-store", "describe", "diagnose",
     "diff", "diff-files", "diff-index", "diff-tree", "difftool", "fast-export", "fast-import",
     "fetch", "fetch-pack", "fmt-merge-msg", "for-each-ref", "for-each-repo", "format-patch",
     "fsck", "fsck-objects", "fsmonitor--daemon", "gc", "get-tar-commit-id", "grep",
     "hash-object", "help", "hook", "index-pack", "init", "init-db", "interpret-trailers", "log",
     "ls-files", "ls-remote", "ls-tree", "mailinfo", "mailsplit", "maintenance", "merge",
     "merge-base", "merge-file", "merge-index", "merge-ours", "merge-recursive",
     "merge-recursive-ours", "merge-recursive-theirs", "merge-subtree", "merge-tree", "mktag",
     "mktree", "multi-pack-index", "mv", "name-rev", "notes", "pack-objects", "pack-redundant",
     "pack-refs", "patch-id", "pickaxe", "prune", "prune-packed", "pull", "push", "range-diff",
     "read-tree", "rebase", "receive-pack", "reflog", "refs", "remote", "remote-ext", "remote-fd",
     "repack", "replace", "rerere", "reset", "restore", "rev-list", "rev-parse", "revert", "rm",
     "send-pack", "shortlog", "show", "show-branch", "show-index", "show-ref", "sparse-checkout",
     "stage", "stash", "status", "stripspace", "submodule--helper", "switch", "symbolic-ref",
     "tag", "unpack-file", "unpack-objects", "update-index", "update-ref", "update-server-info",
     "upload-archive", "upload-archive--writer", "upload-pack", "var", "verify-commit",
     "verify-pack", "verify-tag", "version", "whatchanged", "worktree", "write-tree"}
)  # fmt: skip
# A name an alias may have (a configuration variable's); one in an alias's text (lower case).
ALIAS_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
ALIAS_WORDS = re.compile(r"[a-z][a-z0-9-]*")
# Aliases followed, one naming the next: past those, what it runs is not known.
MOST_ALIASES = 16
# git's own options before its command whose value is the next word.
GIT_VALUED = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix"}
)
# Shells whose `-c` runs a command line of its own.
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Words that open or join the shell's own constructs (`if …; then`, `while …; do`, `{ …; }`).
KEYWORDS = frozenset(
    {"if", "then", "elif", "else", "fi", "do", "done", "while", "until", "for", "!", "{", "}"}
)
# Where one simple command ends (a backquote opens or closes a command of its own).
SEPARATORS = ";&|()`\n"
# The rule v0.1.42 shipped, kept as a floor: a push it saw forcing, or naming main or master,
# is refused whatever the reading below makes of the line (it never refuses less). Each is
# tried once for each stretch of a line up to a separator (floor_force, floor_default).
FLOOR_FORCE = re.compile(r"\bgit\s+push\b[^;&|\n]*(\s--force(-with-lease)?\b|\s-f\b|\s\+\S)")
FLOOR_DEFAULT = re.compile(r"\bgit\s+push\b[^;&|\n]*[\s:](main|master)(\s|$)")
# A push's words that delete or prune branches, mirror, or hand push options (Push.rewrites).
FLOOR_REWRITE = re.compile(
    r"\bgit\s+push\b[^;&|\n]*(\s--delete\b|\s-[a-z]*d[a-z]*\b|\s\+?:[^\s:]|\s--prune\b"
    r"|\s--mirror\b|\s-[a-z]*o|\s--push-option)"
)
GIT_PUSH_WORDS = re.compile(r"\bgit\s+push\b")
FLOOR_ENDS = re.compile(r"[;&|\n]")
# A loose `git … push`, for text the shell would not parse (an unclosed quote): its words.
GIT_WORD = re.compile(r"\bgit\b")
PUSH_WORD = re.compile(r"\b(?:push|send-pack)\b")
# How long a command line may be to be checked at all: a longer one is refused, saying so,
# before any rule reads it (under Ask first, where these rules apply). Every rule reads a line
# in time linear in its length (tests/test_policy.py), so this bounds the time a check takes.
MOST_LINE = 500_000
TOO_LONG = "a command line too long to check (split it into shorter commands)"
# How much of a line that may push is read, at most: past it (more words, shells and evals
# nested deeper, bare pushes in more folders than are asked of git, more of git's own
# variables) it is refused, saying why.
MOST_WORDS = 40_000
MOST_TOKENS = 2 * MOST_WORDS  # Words, separators and redirections, as first split.
MOST_NESTED = 6
MOST_FOLDERS = 8
MOST_GIT_ENV = 64
# And of all the lines read in it (`sh -c '…'`, `eval`, substitutions), at most: twice its
# own characters and so many more, so many moves to another folder (`cd`, `git -C`), each to
# a path so long.
MOST_READ = 100_000
MOST_MOVES = 256
MOST_PATH = 4096
# A line's variables put in its words: so many values a word may take, each so long (past
# them, the word is not read: not known).
MOST_VALUES = 16
MOST_VALUE = 1024
# The branches a line may have switched a folder to before a bare push, at most (past them,
# where it pushes is not known).
MOST_BRANCHES = 64
TOO_COMPLEX = "a command line too complex to check (split it into shorter commands)"
UNREADABLE_PUSH = "can't tell which branch this push updates (name the branch itself)"
NOT_CHECKED = "this command line could not be checked"
# Where a command substitution (`$(…)`) stood in a word: what it makes is not known here.
MADE_UP = "\x00"
# What a substitution that asks git for the checkout's branch makes, as `HEAD` names it (only
# those in current_query: `$(git branch --show-current)`…): the branch a push naming none
# updates. A variable set to one holds it with when (Line.epoch, captured): a `cd` or a switch
# after it may have made it another branch than the checkout's. (Its second character is in no
# line it is taken from: Tally.currents; else a substitution and text after it could make it.)
CURRENT = MADE_UP + "\x02"
# What asks for it: git's command, the options it must have, and whether `HEAD` follows them.
CURRENT_QUERIES = {
    "branch": (frozenset({"--show-current"}), False),
    "rev-parse": (frozenset({"--abbrev-ref"}), True),
    "symbolic-ref": (frozenset({"--short"}), True),
}
QUIET = frozenset({"-q", "--quiet"})
# A refspec whose branch its words do not say: a variable, a glob, a brace expansion.
UNREADABLE = re.compile(r"[$`*?\[{]")
# A substitution that names the default branch (`$(echo main)`).
MADE_UP_DEFAULT = re.compile(r"\$\([^()]*\b(?:main|master)\b|`[^`]*\b(?:main|master)\b")
# A value that is not plain text: a variable's, a substitution's (unless the whole of it).
UNSETTLED = re.compile(r"[$`\x00]")
# `$NAME` and `${NAME}`: a variable a line may have set (NAME=value) before its push.
VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
# After a variable, in zsh: a modifier (`:t`, `:s/x/y/`, `:&`) or a subscript (`[2]`).
ZSH_AFTER = re.compile(r":[A-Za-z&]|\[")
ZSH_MODIFIED = re.compile(r"\$(?:\{[^}]*\}|[A-Za-z_][A-Za-z0-9_]*)(?::[A-Za-z&]|\[)")
# The folder a command runs in, as a word says it (`$PWD`, `${PWD}`, `$(pwd)`), and the top of
# its repository (`$(git rev-parse --show-toplevel)`, read as TOP): see Tally.moved.
HERE = re.compile(r"\$(?:PWD|\{PWD\})(?=/|$)")
# What `$(pwd)` and `$(git rev-parse --show-toplevel)` say, put in their word as these marks (a
# `$` and a character no variable's name has: never a variable's value): `pwd` says the folder
# the shell is in, whatever the line set PWD to.
PWD_MARK, TOP_MARK = "$\x01PWD", "$\x01TOP"
HERE_MADE = re.compile(re.escape(PWD_MARK) + r"(?=/|$)")
TOP = re.compile(re.escape(TOP_MARK) + r"(?=/|$)")
MADE_FOLDERS = {"pwd": PWD_MARK, "git rev-parse --show-toplevel": TOP_MARK}
# A folder not known (a `cd` that may not have run, or where a line's text cannot say).
LOST = "/" + MADE_UP
# A heredoc's start: `<<WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"` (not `<<<`, a here-string).
HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([^\s;&|()<>'\"`]+))")

# A variable's name.
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Commands that may set a variable of the shell they run in, or run code in it (builtins and
# reserved words): a name in their words may be set there. Of those, the ones that run text
# as code (EVALUATORS): any variable may be set, any folder gone to, by what they run.
SETTING_COMMANDS = frozenset(
    {".", "source", "eval", "exec", "command", "builtin", "time", "coproc", "alias",
     "unalias", "bind", "complete", "compgen", "compopt", "enable", "fc", "history", "trap",
     "read", "mapfile", "readarray", "printf", "getopts", "wait", "declare", "typeset",
     "local", "export", "readonly", "unset", "let", "set", "shift", "shopt", "cd", "pushd",
     "popd", "dirs", "select", "for", "case", "function", "in", "do", "done", "then", "else",
     "elif", "fi", "if", "while", "until", "esac", "{", "}", "!", "[[", "]]", "caller", "hash",
     "emulate"}
)  # fmt: skip
EVALUATORS = frozenset({".", "source", "eval", "trap", "alias", "fc", "bind", "complete",
                        "compgen", "enable", "emulate"})  # fmt: skip
WRAPPERS = frozenset({"command", "builtin", "time", "exec"})
# A command's name that is plain text (no variable, substitution, glob or quote in it).
PLAIN_NAME = re.compile(r"[A-Za-z0-9_./+:@%,~-]+|\[\[?")


# Windows' endings of a program's name (`gh.exe`): program_of.
PROGRAM_ENDINGS = (".exe", ".cmd", ".bat", ".com")


def program_of(word: str) -> str:
    """The program a command's name runs, as the system finds it: past its folder, in lower
    case (macOS's file system ignores case: `GIT` runs git), without a Windows ending."""
    name = word.rsplit("/", 1)[-1]
    if "\\" in name:
        name = name.rsplit("\\", 1)[-1]
    name = name.lower()
    return name.rsplit(".", 1)[0] if name.endswith(PROGRAM_ENDINGS) else name


def command_name(words: list[str]) -> tuple[str, int]:
    """The name a simple command runs (past reserved words and assignments before it; past
    `command`, `builtin`, `exec`, and `time` first, and their options), and where its
    arguments start."""
    index = 0
    while index < len(words) and words[index] in KEYWORDS:
        index += 1
    first = index
    while index < len(words) and isinstance(words[index], Assignment):
        index += 1
    while index < len(words) and words[index] in WRAPPERS:
        if words[index] == "time" and index != first:
            break  # After an assignment, `time` is no reserved word: the program.
        index += 1
        while index < len(words) and words[index].startswith("-"):
            index += 1
    return (words[index], index + 1) if index < len(words) else ("", len(words))


def named(text: str) -> tuple[dict[str, int], dict[str, int]]:
    """How often each name stands in a text as a whole word, anywhere (in quotes, comments,
    heredocs too); and how often of those as a variable read (`$NAME`, `${NAME}`)."""
    every: dict[str, int] = {}
    read: dict[str, int] = {}
    for found in NAME.finditer(text):
        at, end = found.span()
        before = text[at - 1] if at else ""
        if before.isalnum() or before == "_":
            continue  # Within a word (`9B`).
        name = found.group()
        every[name] = every.get(name, 0) + 1
        if before == "$" or (
            before == "{" and text[at - 2 : at - 1] == "$" and text[end : end + 1] == "}"
        ):
            read[name] = read.get(name, 0) + 1
    return every, read


def names_made_up(name: str, words: list[str]) -> bool:
    """Whether a command may set a variable whose name the line makes up as it runs (`read
    "$N"`, `export "$N=x"`, `printf -v "$N"`), or one that stands for another (`declare -n`)."""
    if name == "printf":
        targets = [after for before, after in itertools.pairwise(words) if before == "-v"]
        words = targets + [word[2:] for word in words if word.startswith("-v") and word[2:]]
    elif name in {"export", "declare", "typeset", "local", "readonly"}:
        if any(word.startswith("-") and "n" in word for word in words):
            return True
        words = [word.partition("=")[0] for word in words]
    elif name not in {"read", "mapfile", "readarray", "getopts", "unset", "wait", "select"}:
        return False
    return any(UNSETTLED.search(word) for word in words)


def placed(
    text: str, parse: Callable[[str], Any] = lambda text: shell_commands(text)
) -> dict[str, int] | None:
    """How often each name stands, in a line, where it cannot be set: in the words given to a
    command that cannot set a variable of the shell running it (any command but
    SETTING_COMMANDS, by a plain name; `git branch`, `-m "Fix branch handling"`), in what its
    command substitutions run, and in heredocs that expand nothing. None when the line may
    set any variable without naming it (it runs EVALUATORS, or a command whose name it makes
    up), or is past what is read here."""
    counted: dict[str, int] = {}
    budget, pending = 2 * len(text) + MOST_READ, [text]
    while pending:
        text = pending.pop()
        budget -= len(text)
        parsed = parse(text) if budget >= 0 else None
        if parsed is None:
            return None
        commands, inside = parsed
        pending += inside
        for body, quoted in heredocs(text)[1]:
            every, read = named(body) if quoted else ({}, {})
            for name, times in every.items():  # (Not its `$NAME`s: counted as read, as text.)
                counted[name] = counted.get(name, 0) + times - read.get(name, 0)
        for _, words in commands:
            name, start = command_name(words)
            if not name:
                continue
            if (
                name in EVALUATORS
                or not PLAIN_NAME.fullmatch(name)
                or names_made_up(name, words[start:])
            ):
                return None
            if name in SETTING_COMMANDS and "/" not in name:
                continue
            for word in words[start:]:
                for found in NAME.finditer(word):
                    at = found.start()
                    before = word[at - 1] if at else ""
                    if (
                        before.isalnum()
                        or before in {"_", "$"}
                        or word[max(at - 2, 0) : at] == "${"
                    ):
                        continue
                    counted[found.group()] = counted.get(found.group(), 0) + 1
    return counted


class TooComplexError(Exception):
    """A line that takes more to read than is read here (MOST_TOKENS, MOST_READ, MOST_MOVES,
    MOST_PATH, Tally.expands)."""


@dataclass
class Push:
    """One `git push`: whether it forces, the branches it updates for sure, and whether which
    it updates cannot be read from its words (a refspec in a variable it did not set, a glob,
    a substitution's destination, configuration that cannot be read here, an alias, another
    repository's bare push). A bare push (no refspec, `HEAD`, or the checkout's branch as a
    substitution asks git for it) updates where its folder's branch goes: asked of git (with
    git's configuration, for one naming no refspec: `configured`), unless the line made a
    branch or switched to one first (`bare_new`, joined by `&&`; when a checkout's, only if git
    has that branch: `bare_check`, else as if it had not switched: `alt_*`); a checkout or
    switch before it may have gone elsewhere too (`bare_also`)."""

    forced: bool = False
    # It deletes a branch (`--delete`, `-d`, `:branch`), may delete others (`--prune`, `--mirror`)
    # or carries push options (`-o`): as a forced one, only a worker's own branches may be so.
    rewrites: bool = False
    branches: set[str] | None = field(default_factory=set)  # None: every branch.
    # (At most MOST_BRANCHES of them; past those, only main and master: what is judged.)
    bare_in: str | None = None  # The folder a bare push runs in.
    bare_new: str = ""
    bare_also: frozenset[str] = frozenset()
    unknown: bool = False
    made_up: bool = False  # A refspec a substitution makes as a whole.
    nested: bool = False  # In a line a command runs (`eval`, `sh -c`, a substitution).
    current: bool = False  # The checkout's branch, as git said it (CURRENT): as `HEAD`.
    configured: bool = False  # It names no refspec: where git's configuration says (Pushing).
    mapped: bool = False  # It names the checkout's branch (current), no `:`: as configured.
    mode: str | None = None  # push.default on its command line (`-c`), over the folder's.
    bare_check: bool = False
    alt_new: str = ""
    alt_also: frozenset[str] = frozenset()
    alt_unknown: bool = False
    upstreams_set: bool = False  # The line may set a branch's upstream (`branch -u`, `-t`…).
    # A command whose name a variable or a substitution holds, on a line that says push: maybe
    # a push (or a shell fed a script: refused for that, fed_script, when it is one).
    anonymous: bool = False
    # A git command of no name of git's (`git p origin main`, the alias's name in lower case),
    # with the git that ran it (`via`): the push its folder's configuration makes of it, as an
    # alias, if any (through_alias), placed as this one is when bare; or not known, whatever
    # the configuration says (`alias_unknown`: a line that may set it, another repository's).
    # "?": git's command not known (`git $C`); "$": a command not known, with these words.
    alias: str = ""
    via: "Git | None" = None
    alias_unknown: bool = False

    def key(self) -> tuple:
        branches = None if self.branches is None else frozenset(self.branches)
        return (self.forced, self.rewrites, branches, self.bare_in, self.bare_new, self.bare_also,
                self.unknown, self.made_up, self.nested, self.current, self.configured,
                self.mapped, self.mode, self.bare_check, self.alt_new, self.alt_also,
                self.alt_unknown, self.upstreams_set, self.anonymous, self.alias,
                self.via.key() if self.via else None, self.alias_unknown)  # fmt: skip


@dataclass
class Switched:
    """What a line did to a folder's branch before a push. A branch it made or switched to
    (`new`, where in the line; `checkout`: a `git checkout` of a name, a switch only if it is a
    branch), with what came before it (`prior_also`, `prior_unknown`: what counts when that
    switch may have failed); branches it made without switching (`made`); where checkouts after
    it may have gone (`also`), or somewhere not named (`unknown`)."""

    new: str = ""
    new_at: int = 0
    made: dict[str, int] = field(default_factory=dict)
    also: set[str] = field(default_factory=set)
    unknown: bool = False
    prior_also: set[str] = field(default_factory=set)
    prior_unknown: bool = False
    checkout: bool = False

    def switched_to(self, name: str, at: int, checkout: bool = False) -> "Switched":
        """It switched to `name` (made it, or a branch there is), at `at`: what came before is
        kept, for when that may have failed."""
        self.prior_also |= self.also | ({self.new} if self.new else set())
        self.prior_unknown = self.prior_unknown or self.unknown
        self.new, self.new_at, self.also, self.unknown = name, at, set(), False
        self.checkout = checkout
        return self.capped()

    def capped(self) -> "Switched":
        """Past MOST_BRANCHES names where it may be, where is not known."""
        if len(self.prior_also) > MOST_BRANCHES:
            self.prior_also, self.prior_unknown = set(), True
        if len(self.also) > MOST_BRANCHES:
            self.also, self.unknown = set(), True
        return self


def pushed_branch(refspec: str) -> str:
    """The branch a refspec updates on the remote: `main`, `HEAD:main`, `+refs/heads/main`,
    `:main` (deleting it) and `src:refs/heads/main` all update `main`."""
    destination = refspec.lstrip("+").rsplit(":", 1)[-1]
    for prefix in ("refs/heads/", "heads/"):
        destination = destination.removeprefix(prefix)
    return destination


def expanded(folder: str, path: str, physical: bool = False) -> str:
    """`path` from `folder`, `~` expanded; `folder` itself when it cannot be (an unknown user's
    home: the shell's `cd` fails there, and the command goes on where it was). Read as the
    shell's `cd` reads it (`..` takes off the name before it, a link's too); `physical`, as
    `cd -P` and `git -C` do (`..` of a link is its target's parent). One not known (a variable
    the line did not set, a substitution's, LOST) stays not known."""
    try:
        joined = str(Path(folder) / Path(path).expanduser())
    except (RuntimeError, ValueError):
        return folder
    if UNSETTLED.search(joined):
        return joined
    try:  # (Each link resolved as it comes, its `..` then of its target: as the system goes.)
        return os.path.realpath(joined) if physical else os.path.normpath(joined)
    except (OSError, ValueError):  # A name the system cannot take (`\ud800`): not known.
        return LOST


# The characters each scanner below stops at; what lies between is copied whole.
COMMENT_STOPS = re.compile(r"[\\'\"#]")
HEREDOC_STOPS = re.compile(r"[\\'\"#]|\(\(|\)\)|(?<!<)<<(?!<)")
BODY_STOPS = re.compile(r"[\\'\"#\n]|\(\(|\)\)|(?<!<)<<(?!<)")  # A heredoc's body to come.
SUBSTITUTION_STOPS = re.compile(r"[\\'\"$`]")
QUOTED_STOPS = re.compile(r'["\\]')
PARENTHESES = re.compile(r"[()]")


def quoted_end(text: str, index: int) -> int:
    """Where the quote opened at `index` ends, past its closing quote (the end of the text when
    it never closes); in double quotes a backslash escapes the character after it, and a
    command substitution is read whole (construct_end)."""
    if text[index] == "'":
        end = text.find("'", index + 1)
        return len(text) if end < 0 else end + 1
    return construct_end(text, index + 1, '"')[1]


def plain_run(text: str, index: int, stops: re.Pattern) -> tuple[int, bool | None]:
    """Where the run of characters from `index` that are none of `stops` ends, and whether its
    last character starts a word after it (None for an empty run)."""
    stop = stops.search(text, index)
    at = stop.start() if stop else len(text)
    if at == index:
        return at, None
    last = text[at - 1]
    return at, last in " \t\r" or last in SEPARATORS


def without_comments(text: str) -> str:
    """A command line without its comments, as bash reads them: a word that starts with `#`,
    unquoted, to the end of its line (`fix#12` is a word, `'#'` a quoted one)."""
    if "#" not in text:
        return text
    kept, start, index = [], True, 0
    while index < len(text):
        at, starts = plain_run(text, index, COMMENT_STOPS)
        if starts is not None:
            kept.append(text[index:at])
            start, index = starts, at
            continue
        char = text[index]
        if char == "\\":
            kept.append(text[index : index + 2])
            index += 2
            start = False
        elif char in "'\"":
            end = quoted_end(text, index)
            kept.append(text[index:end])
            index, start = end, False
        elif start:  # A comment: to its line's end.
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        else:
            kept.append(char)
            index += 1
            start = False
    return "".join(kept)


# line_starts of the texts read last, by the text itself (not by its value: comparing two
# long texts equal in value, at each heredoc, would take time growing with the line).
LINES: dict[int, tuple[str, tuple[dict[str, list[int]], dict[str, list[int]]]]] = {}


def line_starts(text: str) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """Where each line of a text starts, by what it says, and by what it says once its leading
    tabs are dropped (found once a text)."""
    known = LINES.get(id(text))
    if known is not None and known[0] is text:
        return known[1]
    if len(LINES) >= 8:
        LINES.clear()
    found = lines_of(text)
    LINES[id(text)] = (text, found)
    return found


def lines_of(text: str) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    said: dict[str, list[int]] = {}
    untabbed: dict[str, list[int]] = {}
    at = 0
    for line in text.split("\n"):
        if at >= len(text):
            break
        said.setdefault(line, []).append(at)
        untabbed.setdefault(line.lstrip("\t"), []).append(at)
        at += len(line) + 1
    return said, untabbed


def heredoc_at(text: str, index: int) -> tuple[str, bool, bool, int] | None:
    """The heredoc a `<<WORD` at `index` opens (not `<<<`, a here-string): its word, whether
    tabs are dropped from its lines (`<<-WORD`), whether the word is quoted (its body then
    expands nothing), and where `<<WORD` ends; None when there is none."""
    if not text.startswith("<<", index) or text.startswith("<<<", index):
        return None
    if (index and text[index - 1] == "<") or not (found := HEREDOC.match(text, index)):
        return None
    quoted = found.group(4) is None or found.group(0).find("\\") >= 0
    word = next(group for group in found.groups()[1:] if group is not None)
    return word, bool(found.group(1)), quoted, found.end()


def bodies_end(
    text: str, index: int, pending: list[tuple[str, bool, bool]]
) -> tuple[int, list[tuple[str, bool]]]:
    """Past the bodies of the heredocs `pending`, read from `index` (the line after the one
    they were opened on): each up to the first line after it that is its word; and the
    bodies, each with whether its word is quoted (if not, the shell runs its substitutions). A
    body with no line to end it is no heredoc: what is left is read from there as it is."""
    said, untabbed = line_starts(text)
    bodies = []
    for word, tabs, quoted in pending:
        starting = (untabbed if tabs else said).get(word, [])
        ending = bisect.bisect_left(starting, index)
        if ending == len(starting):  # Never ended: not taken for a heredoc.
            break
        end = starting[ending]
        stop = text.find("\n", end)
        bodies.append((text[index:end], quoted))
        index = len(text) if stop < 0 else stop + 1
    return index, bodies


def without_heredocs(text: str) -> tuple[str, list[str]]:
    """A command line without its heredocs' bodies (heredocs), and those whose word is not
    quoted, given apart: the shell runs their substitutions."""
    kept, bodies = heredocs(text)
    return kept, [body for body, quoted in bodies if not quoted]


def heredocs(text: str) -> tuple[str, list[tuple[str, bool]]]:
    """A command line without its heredocs' bodies, as bash reads them: after the line a
    `<<WORD` (unquoted, outside a comment, not in `((…))`) stands on, each line up to one that
    is WORD (its tabs first dropped for `<<-WORD`). What a body says is text, not commands (an
    apostrophe in it is no quote). The bodies, each with whether its word was quoted. A body
    with no line to end it is kept."""
    if "<<" not in text:
        return text, []
    kept, start, index, pending, bodies, arithmetic = [], True, 0, [], [], 0
    while index < len(text):
        at, starts = plain_run(text, index, BODY_STOPS if pending else HEREDOC_STOPS)
        if starts is not None:
            kept.append(text[index:at])
            start, index = starts, at
            continue
        char = text[index]
        if char == "\\":
            kept.append(text[index : index + 2])
            index += 2
            start = False
            continue
        if char in "'\"":
            end = quoted_end(text, index)
            kept.append(text[index:end])
            index, start = end, False
            continue
        if char == "#" and start:
            end = text.find("\n", index)
            end = len(text) if end < 0 else end
            kept.append(text[index:end])
            index = end
            continue
        if text.startswith("((", index):
            arithmetic += 1  # `$((1<<2))`, `((x<<=1))`: no heredoc in arithmetic.
        elif text.startswith("))", index) and arithmetic:
            arithmetic -= 1
        elif char == "<" and not arithmetic and (heredoc := heredoc_at(text, index)):
            pending.append(heredoc[:3])
            kept.append(text[index : heredoc[3]])
            index = heredoc[3]
            start = False
            continue
        elif char == "\n" and pending:
            kept.append(char)
            index, found = bodies_end(text, index + 1, pending)
            bodies += found
            pending = []
            start = True
            continue
        kept.append(char)
        start = char in " \t\r" or char in SEPARATORS
        index += 1
    return "".join(kept), bodies


INNER_STOPS = re.compile(r"[\\'\"()<\n]|(?<![\w$-])(?:case|esac)(?![\w-])")
DOUBLE_QUOTED_STOPS = re.compile(r'["\\$]')
BACKQUOTE_STOPS = re.compile(r"[\\`]")


# The reserved words a command may follow (`then case …`): a command starts after them.
LEADING = frozenset({"then", "do", "else", "elif", "if", "while", "until", "!", "time", "-p", "in"})
ENDING = frozenset(" \t\n;&|(){}")


def starts_command(text: str, at: int) -> bool:
    """Whether the word at `at` stands where a command starts: after a separator, or a
    reserved word a command follows (looked back over a few characters only)."""
    index = at
    while index and text[index - 1] in " \t":
        index -= 1
    if not index or text[index - 1] in ";\n&|(){":
        return True
    start = index
    while start and index - start < 6 and text[start - 1] not in ENDING:
        start -= 1
    return (not start or text[start - 1] in ENDING) and text[start:index] in LEADING


def construct_end(text: str, index: int, opened: str) -> tuple[int, int]:
    """Where a double-quoted string (`opened` '"') or a command substitution (`opened` "$(")
    whose text starts at `index` ends: the end of its text, and past what closes it (the
    text's end for both when nothing does). What is nested in it is read whole, as bash reads
    it, in one pass: in double quotes, a backslash's character and a substitution; in a
    substitution, quotes, parentheses (other substitutions), heredocs' bodies (text: an
    apostrophe in one is no quote; not in arithmetic, `$((…))`), and a `case` up to its
    `esac` (a pattern's `)` in it is no end, as bash 4 and later read it)."""
    stack, pending = [opened], []
    arithmetic = int(opened == "$(" and text.startswith("(", index))
    while stop := (DOUBLE_QUOTED_STOPS if stack[-1] == '"' else INNER_STOPS).search(text, index):
        at = stop.start()
        char, index = text[at], at + 1
        if char == "\\":
            index = at + 2
        elif stack[-1] == '"':
            if char == '"':
                stack.pop()
            elif text.startswith("$(", at):
                stack.append("$(")
                arithmetic += text.startswith("((", at + 1)
                index = at + 2
        elif char == "'":
            index = quoted_end(text, at)
        elif char == '"':
            stack.append('"')
        elif char in "ce":  # `case`, `esac`: in a case, a `)` may end a pattern.
            index = stop.end()
            if char == "c" and starts_command(text, at):
                stack.append("case")
            elif char == "e" and stack[-1] == "case" and starts_command(text, at):
                stack.pop()
        elif char == "(":
            arithmetic += text.startswith("((", at)
            stack.append("(")
        elif char == ")":  # (In a case, a pattern's `)` without its `(` ends nothing.)
            arithmetic -= bool(arithmetic and text.startswith("))", at))
            if stack[-1] != "case":
                stack.pop()
        elif char == "<" and not arithmetic and (heredoc := heredoc_at(text, at)):
            pending.append(heredoc[:3])
            index = heredoc[3]
        elif char == "\n" and pending:
            index = bodies_end(text, index, pending)[0]
            pending = []
        if not stack:
            return at, index
    return len(text), len(text)


def substitution_end(text: str, index: int) -> tuple[int, int]:
    """Where the `$(…)` whose text starts at `index` ends: the end of what it runs, and past its
    `)` (the text's end for both when it never closes); see construct_end."""
    return construct_end(text, index, "$(")


def backquote_end(text: str, index: int) -> tuple[int, int]:
    """Where the `` `…` `` whose text starts at `index` ends: the end of what it runs, and past
    its closing backquote (the text's end for both when it never closes)."""
    while stop := BACKQUOTE_STOPS.search(text, index):
        if text[stop.start()] == "`":
            return stop.start(), stop.end()
        index = stop.start() + 2
    return len(text), len(text)


def without_substitutions(text: str, quoted: bool = False) -> tuple[str, list[str]]:
    """A command line with each command substitution in it (`$(…)`, `` `…` ``, unquoted or in
    double quotes, not in single quotes: there text is only text) put as MADE_UP, in the word
    it stood in (a variable for one that says a folder: MADE_FOLDERS), and what each runs:
    lines of their own. `quoted`: all of the text is in double quotes (a heredoc's
    body)."""
    if "$(" not in text and "`" not in text:
        return text, []
    kept, bodies, quote, index = [], [], '"' if quoted else "", 0
    while index < len(text):
        if quote == "'":
            end = text.find("'", index)
            end = len(text) if end < 0 else end + 1
            kept.append(text[index:end])
            index, quote = end, ""
            continue
        at, _ = plain_run(text, index, SUBSTITUTION_STOPS)
        if at > index:
            kept.append(text[index:at])
            index = at
            continue
        char = text[index]
        if char == "\\":
            kept.append(text[index : index + 2])
            index += 2
            continue
        if text.startswith("$(", index) or char == "`":
            start = index + (2 if char == "$" else 1)
            ending = substitution_end if char == "$" else backquote_end
            end, index = ending(text, start)
            body = text[start:end]
            bodies.append(body)
            made = MADE_FOLDERS.get(" ".join(body.split())) if len(body) < 64 else None
            kept.append(made or (CURRENT if current_query(body) else MADE_UP))
            continue
        if char in "'\"" and not quoted:
            quote = "" if quote == char else (quote or char)
        kept.append(char)
        index += 1
    return "".join(kept), bodies


def redirection_free(words: list[str]) -> list[str]:
    """A simple command's words without its redirections (`> log`, `2>/dev/null`, `<<EOF`,
    `main>/dev/null`: the word before the operator stays; a quoted `>` is text: Redirecting):
    they name files, not branches."""
    kept, target = [], False
    for word in words:
        if target:
            target = False
            continue
        if not isinstance(word, Redirecting):
            kept.append(word)
            continue
        at = word.at
        rest = word[at:]
        target = not rest.endswith("&-") and not rest.strip("<>&|-")  # Its file: the next word.
        if word[:at] and not word[:at].isdigit():  # Not a file descriptor's number (`2>`).
            kept.append(word[:at])
    return kept


class Separator(str):
    """A word of a line that is a separator (`;`, `&&`, `|`, `(`…), not quoted: a quoted `";"`
    is a word like any other."""


class Assignment(str):
    """A word the shell takes for an assignment (`NAME=…`, `NAME+=…`, `NAME[…]=…`: its name
    and `=` unquoted); `"B"=x` and `B\\=x` are a command's name instead."""


class Redirecting(str):
    """A word with an unquoted `<` or `>` in it (a redirection), the first at `at`; a quoted
    one is text (`'a > b'`)."""

    at = 0


class Splitting(str):
    """A word with a variable or a substitution in it, unquoted: what it holds is split into
    words (`$B`, not `"$B"`); with a quoted part (`quoted`), it is a word even when empty."""

    quoted = False


def made_word(
    pieces: list[str], at: int, assignment: bool, splits: bool = False, quoted: bool = False
) -> str:
    text = "".join(pieces)
    if at >= 0:
        word = Redirecting(text)
        word.at = at
        return word
    if assignment:
        return Assignment(text)
    if splits:
        split = Splitting(text)
        split.quoted = quoted
        return split
    return text


ASSIGNING = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\s'\"\\]*\])?\+?=")


PLAIN_RUN = re.compile(r"[^\s'\"\\" + re.escape(SEPARATORS) + "]+")
REDIRECTS = re.compile(r"[<>]")


def shell_words(text: str) -> list[str]:
    """`text`'s words as `shlex.shlex(text, posix=True, punctuation_chars=SEPARATORS)` reads
    them with only " \\t\\r" for whitespace, whitespace_split and no commenters: quotes and
    backslashes resolved, a run of SEPARATORS one word. In linear time (shlex adds to a word a
    character at a time: a long word takes quadratic time). ValueError where shlex raises one
    (an unclosed quote, a backslash at the end); TooComplexError past MOST_TOKENS words."""
    words: list[str] = []
    index, size = 0, len(text)
    while index < size:
        if len(words) > MOST_TOKENS:
            raise TooComplexError
        char = text[index]
        if char in " \t\r":
            index += 1
            continue
        if char in SEPARATORS:
            end = index + 1
            while end < size and text[end] in SEPARATORS:
                end += 1
            words.append(Separator(text[index:end]))
            index = end
            continue
        word: list[str] = []
        at, assignment, splits = -1, bool(ASSIGNING.match(text, index)), False
        quoted = False
        while index < size:
            char = text[index]
            if char in " \t\r" or char in SEPARATORS:
                break
            quoted = quoted or char in "\\'\""
            if char == "\\":
                if index + 1 == size:
                    raise ValueError("No escaped character")
                word.append(text[index + 1])
                index += 2
            elif char == "'":
                end = text.find("'", index + 1)
                if end < 0:
                    raise ValueError("No closing quotation")
                word.append(text[index + 1 : end])
                index = end + 1
            elif char == '"':
                index += 1
                while True:
                    stop = QUOTED_STOPS.search(text, index)
                    if stop is None:
                        raise ValueError("No closing quotation")
                    end = stop.start()
                    word.append(text[index:end])
                    if text[end] == '"':
                        index = end + 1
                        break
                    if end + 1 == size:
                        raise ValueError("No escaped character")
                    escaped = text[end + 1]
                    word.append(escaped if escaped in '"\\' else "\\" + escaped)
                    index = end + 2
            elif found := PLAIN_RUN.match(text, index):
                piece = found.group()
                if at < 0 and (redirects := REDIRECTS.search(piece)):
                    at = sum(map(len, word)) + redirects.start()
                splits = splits or "$" in piece or MADE_UP in piece
                word.append(piece)
                index = found.end()
            else:  # Whitespace shlex does not split at (`\n` is a separator, `\f` a character).
                word.append(char)
                index += 1
        # (`B=(x)`: an array, no assignment of a word.)
        array = assignment and index < size and text[index] == "(" and text[index - 1] == "="
        words.append(made_word(word, at, assignment and not array, splits, quoted))
    return words


def marked(text: str) -> str:
    """A line with each `${…}` and `$'…'` outside quotes put as one piece of a word (`${\x02}`,
    `$\x02`: a variable's, as made_up reads it): what is in them (a `|`, a quote) splits
    nothing, as in bash (shell_words reads as shlex, which knows neither)."""
    if "${" not in text and "$'" not in text:
        return text
    out: list[str] = []
    at, size = 0, len(text)
    while at < size:
        char = text[at]
        if char in "'\"":
            end = at + 1
            while end < size and text[end] != char:
                end += 2 if char == '"' and text[end] == "\\" else 1
            out.append(text[at : end + 1])
            at = end + 1
        elif char == "\\":
            out.append(text[at : at + 2])
            at += 2
        elif char == "$" and text[at + 1 : at + 2] in {"{", "'"}:
            try:
                end = expansion_end(text, at)
            except ValueError:
                out.append(text[at:])
                break
            out.append("${\x02}" if text[at + 1] == "{" else "$\x02")
            at = end
        else:
            out.append(char)
            at += 1
    return "".join(out)


def expansion_end(text: str, start: int) -> int:
    """Where `${…}` (its braces nested, its quotes read) or `$'…'` (backslashes escaping, `\'`
    too) that starts at `start` ends. ValueError where it doesn't."""
    if text[start + 1] == "'":
        at = start + 2
        while at < len(text):
            if text[at] == "\\":
                at += 2
            elif text[at] == "'":
                return at + 1
            else:
                at += 1
        raise ValueError("No closing quotation")
    depth, at = 0, start + 1
    while at < len(text):
        char = text[at]
        if char == "\\":
            at += 2
            continue
        if char in "'\"":
            closing = text.find(char, at + 1)
            if closing < 0:
                raise ValueError("No closing quotation")
            at = closing + 1
            continue
        depth += {"{": 1, "}": -1}.get(char, 0)
        at += 1
        if depth == 0:
            return at
    raise ValueError("No closing brace")


def shell_commands(text: str) -> tuple[list[tuple[str, list[str]]], list[str]] | None:
    """A command line as the shell splits it (heredoc bodies and comments dropped, quotes and
    backslashes resolved, a backslash-newline joined, redirections dropped): its simple
    commands, each with what joined it to the one before ("&&", ";", a newline… "" for the
    first) and its words, a command substitution MADE_UP in the word it stood in; and what
    those substitutions run, and those in heredocs that expand. None when it does not parse
    (an unclosed quote)."""
    unheredoc, heredocs = without_heredocs(text)
    plain, inside = without_substitutions(without_comments(unheredoc).replace("\\\n", ""))
    for body in heredocs:
        inside += without_substitutions(body, quoted=True)[1]
    try:
        tokens = shell_words(plain)
    except ValueError:
        return None
    commands: list[tuple[str, list[str]]] = []
    current: list[str] = []
    joiner = ""
    index = 0
    while index < len(tokens):
        word = tokens[index]
        index += 1
        if not isinstance(word, Separator):
            current.append(word)
            continue
        following = tokens[index] if index < len(tokens) else Separator()
        is_word = not isinstance(following, Separator)
        last = current[-1] if current else ""
        if word == "&" and isinstance(last, Redirecting) and last[-1:] in {">", "<"} and is_word:
            current[-1] = made_word([last, "&", following], last.at, False)  # `2>&1`, `>&2`.
            index += 1
            continue
        if word == "&" and isinstance(following, Redirecting) and following.startswith(">"):
            current.append(made_word(["&", following], 0, False))  # `&>log`: a redirection.
            index += 1
            continue
        if current:
            commands.append((joiner, redirection_free(current)))
            current, joiner = [], ""
        joiner += word
    if current:
        commands.append((joiner, redirection_free(current)))
    return commands, inside


# What a worker that is only planning (Claude's plan mode) may run: programs that read and
# write nothing, called without an option of theirs that writes or runs another program.
READING_PROGRAMS = frozenset({
    "cd", "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "pwd", "echo", "which",
    "stat", "sort", "uniq", "cut", "diff", "basename", "dirname", "realpath", "readlink", "true",
    "false", "find", "git",
})  # fmt: skip
WRITING_OPTIONS = {
    "sort": ("--output", "--compress-program"),
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf",
             "-fls"),
    "rg": ("--pre", "--hostname-bin"),
}  # fmt: skip
GIT_READING = frozenset({
    "log", "status", "diff", "show", "rev-parse", "ls-files", "blame", "grep", "describe",
    "shortlog", "ls-tree", "cat-file", "merge-base", "rev-list",
})  # fmt: skip
GIT_READING_LISTS = {"branch": ("-a", "-r", "-v", "-vv", "--all", "--remotes", "--list",
                                "--show-current"), "remote": ("-v",)}  # fmt: skip
# git options that write or run another program: an output file, a pager command (git grep).
GIT_WRITING = ("--output", "-O", "--open-files-in-pager")


def reads_only(command: str) -> bool:
    """Whether a command line only reads: each of its commands one of READING_PROGRAMS (git's
    reading subcommands), with no option that writes (WRITING_OPTIONS; sort's -o however
    written; uniq's output file), nothing assigned or wrapped before it, no output redirected,
    no substitution and no heredoc. Anything else, or what does not parse: False."""
    unheredoc, heredocs = without_heredocs(command)
    if heredocs or ">(" in command or len(command) > MOST_LINE:
        return False
    parsed = shell_commands(command)
    if parsed is None or parsed[1]:
        return False
    try:
        words = shell_words(without_comments(unheredoc))
    except ValueError:
        return False
    if not discarded_output(words):
        return False
    for _, command_words in parsed[0]:
        name, start = command_name(command_words)
        arguments = command_words[start:]
        if start != 1 or name not in READING_PROGRAMS:
            return False
        if any(names_option(word, WRITING_OPTIONS.get(name, ())) for word in arguments):
            return False
        if name == "sort" and any("o" in word for word in short_options(arguments)):
            return False
        if name == "uniq" and len([word for word in arguments if not word.startswith("-")]) > 1:
            return False  # (Its second file is written; a number for -f, -s, -w is taken so too.)
        if name == "git" and not git_reads(arguments):
            return False
    return True


def git_reads(arguments: list[str]) -> bool:
    """git's arguments that only read: a reading subcommand without GIT_WRITING, or a listing
    of branches or remotes (none made, changed or removed); no option before it (-c, -C)."""
    if not arguments or arguments[0].startswith("-"):
        return False
    sub, rest = arguments[0], arguments[1:]
    if sub in GIT_READING:
        return not any(names_option(word, GIT_WRITING) for word in rest) and not any(
            "O" in word for word in short_options(rest)
        )
    if sub in GIT_READING_LISTS:
        return all(word in GIT_READING_LISTS[sub] for word in rest)
    return False


def discarded_output(words: list[str]) -> bool:
    """Whether every output redirected among these words goes to /dev/null or to another of the
    command's outputs (2>&1): nothing written."""
    for at, word in enumerate(words):
        if not isinstance(word, Redirecting) or ">" not in word:
            continue
        target = word.lstrip("0123456789").lstrip(">")
        after = words[at + 1 : at + 3]
        if target:
            if target != "/dev/null":
                return False
        elif after[:1] == ["/dev/null"] and not isinstance(after[0], Separator):
            continue
        elif not (
            len(after) == 2
            and isinstance(after[0], Separator)
            and after[0] == "&"
            and not isinstance(after[1], Separator | Redirecting)
            and after[1].isdigit()
        ):
            return False
    return True


def names_option(word: str, options: tuple[str, ...]) -> bool:
    """Whether a word is one of these options: a long one however shortened (getopt's and
    git's abbreviations: --outp=x for --output), a short one with its value attached or not
    (-Ox), one of find's whole (-exec, not -executable)."""
    name = word.split("=", 1)[0]
    return any(
        name.startswith("--") and len(name) > 2 and option.startswith(name)
        if option.startswith("--")
        else word.startswith(option)
        if len(option) == 2
        else word == option
        for option in options
    )


def short_options(words: list[str]) -> list[str]:
    """The words that are short options, together or not (-uo: -u -o)."""
    return [word for word in words if word.startswith("-") and not word.startswith("--")]


def one_substitution(word: str) -> str:
    """What a word is when it is one command substitution, whole: "current" when it asks git
    for the checkout's branch (CURRENT; see current_query), "other" for any other (MADE_UP);
    text that reads as one, though quoted (`'$(git branch --show-current)'`), taken so too. ""
    when it is not one."""
    if word in {MADE_UP, CURRENT}:
        return "current" if word == CURRENT else "other"
    if word.startswith("`") and word.endswith("`") and len(word) > 1:
        if "`" in word[1:-1]:
            return ""
        return "current" if current_query(word[1:-1]) else "other"
    if not (word.startswith("$(") and word.endswith(")")):
        return ""
    depth = 0
    for index, char in enumerate(word[1:], 1):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth == 0:
            if index != len(word) - 1:
                return ""
            return "current" if current_query(word[2:-1]) else "other"
    return ""


# What a query of the checkout's branch may be made of: words of letters and hyphens, apart.
QUERY_TEXT = re.compile(r"[ \t]*[A-Za-z-]+(?:[ \t]+[A-Za-z-]+)*[ \t]*")


def current_query(body: str) -> bool:
    """Whether what a substitution runs asks git for the checkout's branch, and nothing else:
    `git branch --show-current`, `git rev-parse --abbrev-ref HEAD` or `git symbolic-ref
    --short HEAD`, each with `-q` (`--quiet`) too, its options in any order."""
    if len(body) > 64 or not QUERY_TEXT.fullmatch(body):
        return False
    words = body.split()
    if len(words) < 3 or words[0] != "git" or words[1] not in CURRENT_QUERIES:
        return False
    options, head = CURRENT_QUERIES[words[1]]
    rest = words[2:]
    if head:
        if rest[-1] != "HEAD":
            return False
        rest = rest[:-1]
    quiet = [word for word in rest if word in QUIET]
    return len(quiet) <= 1 and len(set(rest)) == len(rest) and set(rest) - QUIET == options


def captured(epoch: int) -> str:
    """What a variable set to the checkout's branch (CURRENT) holds: when it was set."""
    return f"{CURRENT}\x00{epoch}"


CAPTURED = re.compile(re.escape(CURRENT) + r"\x00[0-9]+")
CURRENTS = re.compile(re.escape(CURRENT) + r"(?:\x00([0-9]+))?")


DESTINATION_STOPS = re.compile(r"\$\(|\$\{|[`)}:]")


def destination(refspec: str) -> str:
    """The part of a refspec after its last `:` outside a substitution or a variable's braces
    (`${B:-main}`: one word), all of it without one."""
    if "$(" not in refspec and "`" not in refspec and "${" not in refspec:
        return refspec.rpartition(":")[2]
    depth, braces, quoted, last = 0, 0, False, -1
    for found in DESTINATION_STOPS.finditer(refspec):
        char = found.group()
        if char == "`":
            quoted = not quoted
        elif char == "$(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif char == "${":
            braces += 1
        elif char == "}" and braces:
            braces -= 1
        elif char == ":" and not depth and not braces and not quoted:
            last = found.start()
    return refspec[last + 1 :]


def resolved(word: str, value_of: Callable[[str], str | None], zsh: bool = False) -> str:
    """A word with the variables the line set put in (`$NAME`, `${NAME}`); one it did not set
    stays (`$NAME`: not known), and so does the whole word when it would be longer than
    MOST_VALUE (no branch's name is). In zsh, one a modifier or a subscript follows (`$B:t`,
    `$B[2]`) stays too: what that makes of it is not read here."""
    if "$" not in word:
        return word
    pieces, at, size = [], 0, 0
    for found in VARIABLE.finditer(word):
        if zsh and ZSH_AFTER.match(word, found.end()):
            continue
        value = value_of(found.group(1) or found.group(2))
        if value is None:
            continue
        pieces += [word[at : found.start()], value]
        size += found.start() - at + len(value)
        if size > MOST_VALUE:
            return word
        at = found.end()
    if not pieces or size + len(word) - at > MOST_VALUE:
        return word
    return "".join(pieces) + word[at:]


def expansions(
    words: list[str], values: Callable[[str], tuple[str, ...]], tally: "Tally"
) -> list[list[str]]:
    """The words a simple command runs once the line's variables are put in: one list for each
    value a loop's variable takes (`for b in x y`), at most MOST_VALUES (past them, those
    stay unknown); a word a value went into split at its spaces, as the shell splits what it
    puts in unquoted (an empty one is no word). What they make is counted in `tally` as it is
    made (TooComplexError past its budget)."""
    if not any("$" in word for word in words):
        return [words]
    looped = {
        name
        for word in words
        if "$" in word
        for found in VARIABLE.finditer(word)
        if len(values(name := found.group(1) or found.group(2))) > 1
    }
    combinations: list[dict[str, str]] = [{}]
    for name in sorted(looped):
        combinations = [{**done, name: value} for done in combinations for value in values(name)]
        if len(combinations) > MOST_VALUES:
            combinations = [{}]
            break
    found = []
    for combination in combinations:

        def value_of(name: str, chosen: dict[str, str] = combination) -> str | None:
            said = values(name)
            return chosen.get(name, said[0] if len(said) == 1 else None)

        run = []
        for word in words:
            put = resolved(word, value_of, tally.modifiers)
            # Split where unquoted (`$B`), and gone when empty, unless part of it is quoted
            # (`""$B`); a quoted one (`"$B"`) is one word, even empty.
            made = [put] if put == word or not isinstance(word, Splitting) else put.split()
            if isinstance(word, Splitting) and put != word:  # (What is left unread splits still.)
                made = [Splitting(piece) if UNSETTLED.search(piece) else piece for piece in made]
            made = made or ([""] if getattr(word, "quoted", False) else [])
            tally.expands(len(put), len(made))
            run += made
        found.append(run)
    return found


# push.default's values, and git's words for true and false (a value of its own: `Pushing`).
MODES = frozenset({"nothing", "matching", "simple", "upstream", "tracking", "current"})
TRUE = frozenset({"true", "yes", "on", "1"})
FALSE = frozenset({"false", "no", "off", "0", ""})


def git_config_pushes(config: dict[str, str | None]) -> tuple[list[str], bool, bool, str | None]:
    """What configuration given on the command line says a push updates: refspecs
    (`remote.<name>.push`), every branch (`push.default=matching`, a mirror remote), or not
    known (a value that is not in the command line; a file it includes, an upstream it sets);
    and push.default, when given."""
    refspecs, every, unknown, mode = [], False, False, None
    for key, value in config.items():
        if key.startswith("remote.") and key.endswith(".push"):
            if value is None:
                unknown = True
            else:
                refspecs.append(value)
        elif key.startswith("remote.") and key.endswith(".mirror"):
            if value is None or value.lower() not in TRUE | FALSE:
                unknown = True
            every = every or (value or "").lower() in TRUE
        elif key == "push.default":
            mode = value.lower() if value is not None else None
            if value is None:
                unknown = True
            elif value.lower() == "matching":
                every = True
            elif value.lower() not in {"nothing", "current", "upstream", "simple", "tracking"}:
                unknown = True
        elif (
            key == "include.path"
            or (key.startswith("includeif.") and key.endswith(".path"))
            or (key.startswith("branch.") and key.endswith(".merge"))
        ):
            unknown = True
    return refspecs, every, unknown, mode


def environment_config(env: dict[str, str]) -> dict[str, str | None]:
    """git configuration given through the environment (GIT_CONFIG_COUNT and its keys and
    values; GIT_CONFIG_PARAMETERS, whose push and alias settings are not read here: not known;
    another file for the user's or the system's, GIT_CONFIG_GLOBAL and GIT_CONFIG_SYSTEM: not
    read here)."""
    found: dict[str, str | None] = {}
    parameters = env.get("GIT_CONFIG_PARAMETERS", "").lower()
    if "push" in parameters:
        found["remote.?.push"] = None
    if "alias." in parameters:
        found["alias.?"] = None
    if "GIT_CONFIG_GLOBAL" in env or "GIT_CONFIG_SYSTEM" in env:
        found["include.path"] = None
    for name, key in env.items():
        if name.startswith("GIT_CONFIG_KEY_"):
            found[key.lower()] = env.get("GIT_CONFIG_VALUE_" + name.removeprefix("GIT_CONFIG_KEY_"))
    return found


@dataclass
class Git:
    """A `git …` invocation: its folder (after `-C`), its configuration (`-c`, `--config-env`,
    the environment's; keys in lower case), its command with what follows, and whether it
    works on another repository than its folder's (`--git-dir`, `--work-tree`, GIT_DIR)."""

    folder: str
    config: dict[str, str | None]
    rest: list[str]
    elsewhere: bool
    moved: bool = False  # Its folder is another than the shell's (`-C`).
    zsh: bool = False  # zsh's modifiers may be in its words (`$B:t`: Tally.modifiers).
    epoch: int = -1  # Its line's Line.epoch: a variable set to CURRENT then is its branch.
    currents: bool = False  # Whether CURRENT in its words is what a substitution made.

    def key(self) -> tuple:
        rest = tuple((word, type(word).__name__) for word in self.rest)
        return (self.folder, tuple(sorted(self.config.items())), rest,
                self.elsewhere, self.moved, self.zsh, self.epoch, self.currents)  # fmt: skip


def git_command(
    words: list[str], env: dict[str, str], cwd: str, tally: "Tally | None" = None
) -> Git | None:
    """A `git …` invocation read from its words; None without a command. Its moves to another
    folder (`-C`) count in `tally`."""
    config: dict[str, str | None] = dict(environment_config(env))
    elsewhere = bool(env.get("GIT_DIR") or env.get("GIT_WORK_TREE"))
    folder, index = cwd, 1
    while index < len(words) and words[index].startswith("-"):
        word = words[index]
        name, _, value = word.partition("=")
        if name in GIT_VALUED and not value and index + 1 < len(words):
            index += 1
            value = words[index]
        if name == "-C" and value:  # (git goes there as the system does: a link's `..` too.)
            folder = tally.moved(folder, value, True) if tally else expanded(folder, value, True)
        elif name == "-c" and value:
            key, has, setting = value.partition("=")
            config[key.lower()] = setting if has else "true"
        elif name == "--config-env" and value:
            config[value.partition("=")[0].lower()] = None  # In a variable: not known.
        elif name in {"--git-dir", "--work-tree"}:
            elsewhere = True
        index += 1
    if index >= len(words):
        return None
    modifiers = bool(tally and tally.modifiers)
    return Git(folder, config, words[index:], elsewhere, folder != cwd, modifiers)


def switch_of(arguments: list[str]) -> tuple[str, str] | None:
    """What `git checkout`/`git switch` with these arguments does to the branch its checkout is
    on: ("new", name) makes that branch and switches to it; ("switched", name), `git switch`
    of one branch with options that change nothing of where, goes there (or fails); and
    ("branch?", name), `git checkout` so, goes there if git has that branch (else it restores
    a file of that name, or goes to that commit: Push.bare_check); ("to", name) may switch to
    `name` (a branch, or for `checkout` a file it restores instead: not known here);
    ("unknown", "") switches to one it does not name (`-`, `@{-1}`); None switches nothing
    (files after `--`)."""
    command, rest = arguments[0], arguments[1:]
    creating = (
        {"-b", "-B", "--orphan"} if command == "checkout" else {"-c", "-C", "--create", "--orphan"}
    )
    for index, word in enumerate(rest):
        if word == "--":
            break
        name, _, value = word.partition("=")
        if name in creating:
            branch = value or (rest[index + 1] if index + 1 < len(rest) else "")
            return ("new", branch) if branch else ("unknown", "")
    options = rest[: rest.index("--")] if "--" in rest else rest
    if "-" in options or any(word.startswith("@{-") for word in options):
        return ("unknown", "")
    named = [word for word in options if not word.startswith("-")]
    if command == "checkout" and "--" in rest:
        return None  # Files restored (from that commit): no switch.
    if not named:
        return None
    if {"-t", "--track"} & set(options) and "/" in named[0]:
        return ("to", named[0].split("/", 1)[1])  # A branch made after the remote's.
    flags = [word for word in options if word.startswith("-")]
    if (
        len(named) == 1
        and BRANCH_NAME.fullmatch(named[0])
        and all(flag in SWITCH_FLAGS or flag.startswith("--conflict=") for flag in flags)
    ):
        return ("switched" if command == "switch" else "branch?", named[0])
    return ("to", named[0])


# Options of `git switch` and `git checkout` that take no value and change nothing of where they
# go (not `-p`, `--detach`, `-t`; nor several letters at once: `-fd`).
SWITCH_FLAGS = frozenset(
    {"-f", "--force", "-m", "--merge", "-q", "--quiet", "--progress", "--no-progress",
     "--guess", "--no-guess", "--recurse-submodules", "--no-recurse-submodules",
     "--ignore-other-worktrees", "--discard-changes", "--overwrite-ignore",
     "--no-overwrite-ignore"}
)  # fmt: skip
# A branch's name as a switch is read for (plain: no variable, glob, `~`, `^`, `:` or `@{`).
BRANCH_NAME = re.compile(r"(?!-)[A-Za-z0-9._/+-]+")


def renamed_or_made(arguments: list[str]) -> tuple[str, str] | None:
    """What `git branch` with these arguments does: ("made", name) makes a branch, without
    switching to it; ("renamed", name) may rename the checkout's own branch to `name`."""
    rest = arguments[1:]
    named = [word for word in rest if not word.startswith("-")]
    if {"-m", "-M", "--move"} & set(rest):
        return ("renamed", named[-1]) if named else None
    listing = {"-d", "-D", "--delete", "-c", "-C", "--copy", "--list", "-l", "-a", "-r",
               "--all", "--remotes", "--show-current", "-v", "-vv", "--merged", "--no-merged",
               "--contains", "--no-contains", "--points-at", "-u", "--set-upstream-to",
               "--unset-upstream", "--edit-description", "--format", "--sort"}  # fmt: skip
    if listing & {word.partition("=")[0] for word in rest} or not named:
        return None
    return ("made", named[0])


def pointed(arguments: list[str]) -> tuple[str, str] | None:
    """What `git symbolic-ref` with these arguments does to the checkout's branch: ("to",
    name) points HEAD at that branch; ("unknown", "") at a ref not a branch, or changes
    another symbolic ref; None reads one (or deletes one: HEAD cannot be)."""
    named, valued = [], False
    for word in arguments[1:]:
        if valued:
            valued = False
        elif word == "-m":
            valued = True
        elif not word.startswith("-") or word == "-":
            named.append(word)
        elif word in {"-d", "--delete"}:
            return None
    if len(named) < 2:
        return None
    if named[0] == "HEAD" and named[1].startswith("refs/heads/") and len(named[1]) > 11:
        return ("to", named[1].removeprefix("refs/heads/"))
    return ("unknown", "")


def push_of(git: Git) -> Push | None:
    """The `git push` a git invocation runs (the line's variables put in its words), or
    None; `git subtree push` and `git send-pack` push too."""
    if UNSETTLED.search(git.rest[0]):
        return Push(unknown=True)  # A command in a variable or a substitution: maybe a push.
    if git.rest[0] == "subtree":
        return subtree_push(git)
    if git.rest[0] == "send-pack":
        return send_pack(git)
    if git.rest[0] != "push":
        alias = git.config.get(f"alias.{git.rest[0].lower()}", "")
        if (
            alias is None
            or any(word in alias.lower() for word in PUSHING)
            or "alias.?" in git.config
        ):
            return Push(unknown=True)  # An alias that may push: what, not known.
        return None
    push, positional, valued = Push(), [], False
    deleting = dry = tags = False
    for word in git.rest[1:]:
        if valued:
            valued = False
            continue
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            push.forced = push.forced or name in {"--force", "--force-with-lease"}
            deleting = deleting or name == "--delete"
            dry = (dry or name == "--dry-run") and name != "--no-dry-run"  # The last one wins.
            tags = tags or name == "--tags"
            if name in PUSH_ALL:
                push.branches = None
            if name in PUSH_REWRITES:
                push.rewrites = True
                if name in {"--prune", "--mirror"}:
                    push.branches = None  # Whatever the remote has that this side has not.
            valued = name in PUSH_VALUED and "=" not in word
            continue
        if word.startswith("-") and len(word) > 1:
            for at, letter in enumerate(word[1:], 1):
                if letter == "o":  # `-o <option>`, or `-o<option>`: the rest is its value.
                    push.rewrites = True
                    valued = at == len(word) - 1
                    break
                push.forced = push.forced or letter == "f"
                deleting = deleting or letter == "d"
                dry = dry or letter == "n"
            continue
        positional.append(word)
    push.rewrites = push.rewrites or deleting or "push.pushoption" in git.config
    refspecs, every, unknown, push.mode = git_config_pushes(git.config)
    push.unknown = unknown
    if positional and isinstance(positional[0], Splitting) and UNSETTLED.search(positional[0]):
        push.unknown = True  # A remote in a variable, unquoted: it may hold refspecs too.
    if every:
        push.branches = None
    # Every word that is not an option counts as a refspec but the remote's (its name, or a
    # variable that holds it, names no branch of its own).
    heads = read_refspecs(push, [*positional[1:], *refspecs], git)
    if dry:
        push.rewrites = False
        return push  # It updates nothing.
    configured = len(positional) <= 1 and not tags
    if deleting or refspecs:
        push.unknown = push.unknown or push.made_up  # Which one it deletes: not known.
        if refspecs and configured and not deleting:  # (The repository's are pushed too.)
            return bare(push, git, False, True)
        return push  # What it updates is all in its words.
    return bare(push, git, heads, configured)


def bare(push: Push, git: Git, heads: bool, configured: bool = False) -> Push:
    """A push of its folder's branch (`HEAD`, the checkout's branch as git said it, or no
    refspec at all: `configured`), as such."""
    if push.made_up and git.moved:
        push.unknown = True  # A substitution runs where the shell is, not in `-C`'s folder.
    if configured or push.current or heads:
        push.bare_in = git.folder  # Where its folder's branch goes.
        push.configured = configured
        push.unknown = push.unknown or git.elsewhere  # Another repository's: not known.
    return push


def read_refspecs(push: Push, words: list[str], git: Git) -> bool:
    """What these refspecs update, into `push`: a branch they name, every branch of the same
    name on both sides (`:`), the checkout's branch as a substitution asks git for it (current,
    also as a variable the line set then: Line.epoch), or not known (another substitution, a
    variable, a glob). Whether one is `HEAD` (the folder's branch)."""
    heads = False
    for word in words:
        push.forced = push.forced or word.startswith("+")
        spec = word.lstrip("+")
        if spec == ":":
            push.branches = None  # Every branch of the same name on both sides (as `--all`).
            continue
        if spec.startswith(":"):
            push.rewrites = True  # Nothing pushed to it: it is deleted.
        target = destination(spec)
        whole = one_substitution(spec)
        named = pushed_branch(target) if MADE_UP in target else ""
        if git.currents and (whole == "current" or named in {CURRENT, captured(git.epoch)}):
            push.made_up = push.current = True  # As `HEAD`: its folder's branch (asked of git).
            push.mapped = push.mapped or ":" not in spec
            continue
        if whole:
            push.made_up = push.unknown = True  # What another substitution makes: not known.
            continue
        unread = CURRENTS.sub("", word) if git.currents else word  # (A branch's name: no space.)
        if isinstance(word, Splitting) and UNSETTLED.search(unread):
            push.unknown = True  # Unquoted, what it holds may be several refspecs: not known.
            continue
        if git.zsh and ZSH_MODIFIED.search(word):
            push.unknown = True  # (`$B:t`, `$B[2]`: what zsh makes of B, not read here.)
            continue
        if MADE_UP in target or "$(" in target or "`" in target:
            push.unknown = True  # A part of a substitution: not known.
            continue
        if UNREADABLE.search(target):
            push.unknown = True  # A variable, a glob: not known.
            continue
        branch = pushed_branch(target)
        if branch in {"HEAD", "@"}:
            heads = True
        elif push.branches is not None and (
            branch in DEFAULT_BRANCHES or len(push.branches) < MOST_BRANCHES
        ):
            push.branches.add(branch)  # (Past MOST_BRANCHES, only the default's are kept.)
    return heads


# `git subtree`'s options that take a value; `git send-pack`'s.
SUBTREE_VALUED = frozenset({"-P", "--prefix", "-m", "--message", "-b", "--branch", "--onto",
                            "--annotate"})  # fmt: skip
SEND_PACK_VALUED = frozenset({"--receive-pack", "--exec", "--push-option"})


def subtree_push(git: Git) -> Push | None:
    """`git subtree push … <repository> <ref>`: it pushes to `<ref>` (`refs/heads/<ref>`, or
    `<commit>:<ref>`); None for its other commands."""
    positional, valued = [], False
    for word in git.rest[1:]:
        if valued:
            valued = False
        elif word.startswith("-") and len(word) > 1:
            valued = word.split("=", 1)[0] in SUBTREE_VALUED and "=" not in word
        else:
            positional.append(word)
    if positional[:1] != ["push"]:
        return None
    push = Push(unknown=len(positional) < 3)  # (Without its ref: not read here.)
    push.forced = any(word in {"-f", "--force"} for word in git.rest)
    repository = positional[1] if len(positional) > 1 else ""
    if isinstance(repository, Splitting) and UNSETTLED.search(repository):
        push.unknown = True  # A repository in a variable, unquoted: it may hold its ref too.
    return bare(push, git, read_refspecs(push, positional[2:], git))


def send_pack(git: Git) -> Push:
    """`git send-pack [<host>:]<directory> [<ref>…]`: as `git push` reads its refspecs; with
    none (or `--all`, `--mirror`), every branch of the same name on both sides; refspecs it
    reads from its input (`--stdin`): not known."""
    push, positional, valued, dry = Push(), [], False, False
    for word in git.rest[1:]:
        if valued:
            valued = False
            continue
        if word.startswith("-") and len(word) > 1:
            name = word.split("=", 1)[0]
            push.forced = push.forced or name.startswith("--force") or name == "-f"
            if name in {"--all", "--mirror"}:
                push.branches = None
            push.rewrites = push.rewrites or name in {"--mirror", "--push-option"}
            dry = dry or name in {"--dry-run", "-n"}
            push.unknown = push.unknown or name == "--stdin"
            valued = name in SEND_PACK_VALUED and "=" not in word
            continue
        positional.append(word)
    if positional and isinstance(positional[0], Splitting) and UNSETTLED.search(positional[0]):
        push.unknown = True  # A repository in a variable, unquoted: it may hold refs too.
    if len(positional) <= 1:
        push.branches = None
    heads = read_refspecs(push, positional[1:], git)
    return push if dry else bare(push, git, heads)


# In text read loosely: what may take a command to another folder, git to another repository,
# or the folder's checkout to another branch (bash runs the lines before one it cannot parse).
ELSEWHERE = re.compile(
    r"\b(?:cd|pushd|popd|eval|source|exec|function|checkout|switch|symbolic-ref|branch"
    r"|worktree|update-ref)\b"
    r"|(?:^|[\s;&|(`])\.\s|-C|GIT_DIR|GIT_WORK_TREE|--git-dir|--work-tree|\(\s*\)"
)
# And what may make a word a command that pushes (an alias), or set where pushes go (`git …
# config`): every push of such text is not known.
ALIASING = re.compile(r"\balias\b|\bBASH_ALIASES\b|\b[gs]?aliases\b")
CONFIG_WORD = re.compile(r"\bconfig\b")


def loose_pushes(command: str, cwd: str) -> list[Push]:
    """The pushes of text the shell would not parse (an unclosed quote), read loosely: in each
    stretch up to a separator, the words after the first `push` that follows its first `git`
    (with nothing between them: git's options, an alias, are not read here). A bare one is
    judged by the line's folder, unless the text may go elsewhere first (no `cd` or `-C` is
    read here: ELSEWHERE). Not known where it pushes: one whose words name a variable or run
    a command (no variable is read here), a `push` after a `git` of an earlier stretch
    (`git -C "$(cd x && pwd)" push`) or before this one's, or after a command a variable
    names (`$G push`), any in text that defines an alias, and every one in text that runs
    `git … config`."""
    found, elsewhere, gits = [], bool(ELSEWHERE.search(command)), False
    aliased = bool(ALIASING.search(command)) and "push" in squashed(command)
    configures = False
    for stretch in FLOOR_ENDS.split(command):
        git = GIT_WORD.search(stretch)
        configures = configures or bool(git and CONFIG_WORD.search(stretch, git.end()))
        if git and gits and PUSH_WORD.search(stretch, 0, git.start()):
            found.append(Push(unknown=True))  # (`g push … $(git …)`: g, an alias, not read.)
        push = PUSH_WORD.search(stretch, git.end()) if git else PUSH_WORD.search(stretch)
        if push and not git and UNSETTLED.search(stretch, 0, push.start()):
            found.append(Push(unknown=True))  # (`$G push`: a command a variable names.)
            continue
        if not push or not (git or gits):
            gits = gits or bool(git)
            continue
        gits, rest = True, stretch[push.end() :].split()
        runs_it = set(re.findall(r"[\w.+-]+", stretch[: git.start()] if git else ""))
        if (
            not git
            or stretch[git.end() : push.start()].strip()
            or UNSETTLED.search(" ".join(rest))
            or runs_it & (FEEDERS | {"env", "find", "sudo"})  # Words or a folder of their own.
        ):
            found.append(Push(unknown=True))
            continue
        given = filter(None, (word.strip("'\"`()\\") for word in rest))
        words = ["git", push.group(), *given]  # (`push`, or `send-pack`: as such.)
        git_push = git_command(words, {}, cwd)
        if git_push and (found_push := push_of(git_push)):
            if found_push.bare_in is not None and (not cwd or elsewhere):
                found_push.bare_in, found_push.unknown = None, True
            found.append(found_push)
    for push in found:
        push.unknown = push.unknown or configures
    return found + ([Push(unknown=True)] if aliased else [])


def shell_script(words: list[str], position: int) -> str | None:
    """The command line a shell's `-c` runs: the first word after that option that is no
    option of its own (nor the `--` that ends them)."""
    for index in range(position + 1, len(words)):
        word = words[index]
        if word.startswith("-") and not word.startswith("--") and "c" in word:
            return next((later for later in words[index + 1 :] if not later.startswith("-")), None)
    return None


@dataclass
class Tally:
    """What reading a line and the lines in it found (their pushes), and how much it read: the
    characters of each line, the moves to another folder, what its variables made."""

    text: str = ""
    start: str = ""
    shell: str = ""  # The shell running the line, when not bash or sh (zsh: Codex's).
    pushes: list[Push] = field(default_factory=list)
    read: int = 0
    moves: int = 0
    made: int = 0
    made_words: int = 0
    names: tuple[dict[str, int], dict[str, int]] | None = None
    places: dict[str, int] | None = None
    placing: bool = True
    # Branches checkouts and switches may have gone to: in lines a command runs, or in the
    # background (`aside`, for every push of the line); at the line's top (`top`, for the
    # pushes of the lines its commands run). Each with whether one went somewhere not named.
    parsed: dict[int, tuple[str, Any]] = field(default_factory=dict)
    knowing: dict[str, bool] = field(default_factory=dict)
    aside: set[str] = field(default_factory=set)
    aside_unknown: bool = False
    top: set[str] = field(default_factory=set)
    top_unknown: bool = False
    background: bool = False  # A job runs in the background (`&`): in no order with the rest.
    here: str | None = None  # Where code being read (read_code) started: `$PWD`, if there.
    # How cd reads `..` where the line being read starts (Line.physical, Line.physical_unsure).
    modes: tuple[bool, bool] = (False, False)
    # Whether a line zsh runs handed this one to another shell (Tally.modifiers).
    outer_zsh: bool = False
    saying: dict[str, bool] = field(default_factory=dict)
    currents: bool = False  # Whether CURRENT's second character is in no word of the line.
    # What the line may do to where pushes go, besides what it says of each: set git's push
    # configuration (`git config`), a branch's upstream (`git branch -u`, a branch made: Push.
    # upstreams_set), delete or rename a branch a checkout then names (Push.bare_check).
    config_set: bool = False
    upstreams_set: bool = False
    unbranched: bool = False
    # What the aliases git's configuration has that switch run, by folder and name (judged).
    aliases: dict[tuple[str, str], tuple | None] = field(default_factory=dict)

    def named(self) -> tuple[dict[str, int], dict[str, int]]:
        if self.names is None:
            self.names = named(self.text)
        return self.names

    def says(self, word: str) -> bool:
        """Whether a word of the line says `word` (in lower case), however it is quoted
        (squashed): asked of the line once a word, as each command may ask it."""
        if word not in self.saying:
            self.saying[word] = word in squashed(self.text)
        return self.saying[word]

    def parse(self, text: str) -> tuple[list[tuple[str, list[str]]], list[str]] | None:
        """shell_commands of a text, once each (by the text itself)."""
        known = self.parsed.get(id(text))
        if known is None or known[0] is not text:
            known = self.parsed[id(text)] = (text, shell_commands(text))
        return known[1]

    def known(self, name: str) -> bool:
        """Whether the line sets `name` in one place only, the one being read: every other
        word of the line that names it reads it (`$NAME`) or is given to a command that cannot
        set it (placed); and IFS, which splits what a variable holds, is not named."""
        if name in self.knowing:
            return self.knowing[name]
        every, read = self.named()
        if self.placing and self.places is None:
            self.places = placed(self.text, self.parse)
            self.placing = self.places is not None
        if self.places is None or every.get("IFS"):
            known = False
        else:
            known = every.get(name, 0) == read.get(name, 0) + self.places.get(name, 0) + 1
        self.knowing[name] = known
        return known

    def here_known(self) -> bool:
        """Whether `$PWD` is the folder a command runs in: the line names PWD only to read it."""
        every, read = self.named()
        return not every.get("IFS") and every.get("PWD", 0) == read.get("PWD", 0)

    def homed(self) -> bool:
        """Whether the user's git configuration may be another file than the one asked of git
        (config_entries): the line names HOME or XDG_CONFIG_HOME but to read them."""
        every, read = self.named()
        return any(every.get(name, 0) > read.get(name, 0) for name in ("HOME", "XDG_CONFIG_HOME"))

    def reads(self, text: str):
        self.read += len(text)
        if self.read > 2 * len(self.text) + MOST_READ:
            raise TooComplexError

    def expands(self, characters: int, words: int):
        """What putting the line's variables in a command's words made: in all, at most as
        many characters as are read (Tally.reads), and MOST_WORDS words."""
        self.made += characters
        self.made_words += words
        if self.made > 2 * len(self.text) + MOST_READ or self.made_words > MOST_WORDS:
            raise TooComplexError

    @property
    def zsh(self) -> bool:
        """Run by another shell than bash or sh (zsh, as Codex runs commands): what it reads
        otherwise (`$B:t`, `$B[2]`, `cdpath`, a pipe's last command in the shell itself) is
        taken as not known here."""
        return self.shell not in {"", "bash", "sh", "dash"}

    @property
    def modifiers(self) -> bool:
        """Whether a word may carry zsh's modifiers or subscripts (`$B:t`, `$B[2]`: not known
        here): read by zsh, or in a line zsh handed to another shell (`sh -c "… $B:t"`: zsh
        put those in, in its double quotes)."""
        return self.zsh or self.outer_zsh

    def moved(self, folder: str, path: str, physical: bool = False) -> str:
        """The folder `cd path` or `git -C path` moves to from `folder`, counted: `$(pwd)` is
        `folder`, and `$PWD` when the line does not set PWD (here_known), in code a command
        runs only where it started (`here`); the repository's top (a substitution's: TOP) is
        `folder` when that is the line's own and the top of one."""
        self.moves += 1
        if self.moves > MOST_MOVES or len(folder) + len(path) > MOST_PATH:
            raise TooComplexError
        if path.startswith("~") and self.named()[0].get("HOME"):
            return LOST  # A home the line may have set.
        here = HERE_MADE.match(path) or (HERE.match(path) if self.here_known() else None)
        if here and self.here in (None, folder):
            path = "." + path[here.end() :]
        elif top := TOP.match(path):
            if folder != self.start or not Path(folder, ".git").exists():
                return LOST
            path = "." + path[top.end() :]
        return expanded(folder, path, physical)


# Reserved words that open and close the shell's constructs: what is in one may not run (in a
# loop, may run again).
OPENERS = frozenset({"if", "while", "until", "for", "select", "{"})
CLOSERS = frozenset({"fi", "done", "}"})
LOOPS = frozenset({"while", "until", "for", "select"})


# In a joiner: the operators that join or end lists, and parentheses.
OPERATORS = re.compile(r"&&|\|\||;;&?|;&|[;&|\n()]")
# A device a line may name to feed a file it sources (`/dev/stdin`, `/dev/fd/0`, `/proc/…`).
DEVICES = re.compile(r"/dev/(?!null\b)|/proc/")
# A process substitution (`<(…)`, `>(…)`): what it runs, it runs alongside the line.
PROCESS = re.compile(r"[<>]\(")


def compounds(words: list[str]) -> str:
    """The compound commands a simple command's words start ("o": `if`, `while`, `{`, `case`…)
    and end ("c": `fi`, `done`, `}`, `esac`), in order."""
    first = next((i for i, word in enumerate(words) if word not in KEYWORDS), len(words))
    found = "".join("o" if w in OPENERS else "c" if w in CLOSERS else "" for w in words[:first])
    if words[first : first + 1] == ["case"] and not (first and words[first - 1] in LOOPS):
        found += "o"
    elif words[first : first + 1] == ["esac"]:
        found += "c"
    return found


def background(commands: list[tuple[str, list[str]]], joiners: list[str] | None) -> list[bool]:
    """Whether each command is in a list put in the background (`a && b &`, `(a; b) && c &`):
    run in a subshell of its own, in no order with what follows. `joiners`: the commands'
    joiners without case patterns' parentheses (subshells); None when the line's are not
    paired: then every command is, if the line puts any in the background."""
    if joiners is None:
        lone = any("&" in joiner.replace("&&", "") for joiner, _ in commands)
        return [lone] * len(commands)
    marks, starts = [0] * (len(commands) + 1), [0]  # Where the list read in each group started.
    for index, (_, words) in enumerate(commands):
        for part in [*OPERATORS.findall(joiners[index]), *compounds(words)]:
            if part in {"(", "o"}:  # A subshell or a compound command: its lists, its own.
                starts.append(index)
            elif part in {")", "c"}:
                if len(starts) > 1:
                    starts.pop()
            elif part == "&":
                marks[starts[-1]] += 1
                marks[index] -= 1
                starts[-1] = index
            elif part not in {"&&", "||", "|"}:
                starts[-1] = index
    return [running > 0 for running in itertools.accumulate(marks[:-1])]


def in_pipes(commands: list[tuple[str, list[str]]], joiners: list[str] | None) -> list[bool]:
    """Whether each command is in a subshell or a compound command that is part of a pipe
    (`( a; b ) | c`, `case … esac | c`, `c | { a; }`): run alongside the pipe's other
    commands, in no order with them. `joiners` as for background; None: every command is, if
    the line has a pipe."""
    if joiners is None:
        piping = any("|" in joiner.replace("||", "") for joiner, _ in commands)
        return [piping] * len(commands)
    marks, opened = [0] * (len(commands) + 1), []  # Each group open: where, and if piped.
    parts = [OPERATORS.findall(joiner) for joiner in joiners]

    def operator(found: list[str], last: bool) -> str:
        return next((part for part in (found[::-1] if last else found) if part not in "()"), "")

    for index, (_, words) in enumerate(commands):
        for at, part in enumerate(parts[index]):
            if part == "(":
                opened.append((index, operator(parts[index][:at], True) == "|"))
            elif part == ")" and opened:
                start, piped = opened.pop()
                if piped or operator(parts[index][at + 1 :], False) == "|":
                    marks[start] += 1
                    marks[index] -= 1
        for mark in compounds(words):
            if mark == "o":
                opened.append((index, operator(parts[index], True) == "|"))
            elif opened:
                start, piped = opened.pop()
                after = parts[index + 1] if index + 1 < len(commands) else []
                if piped or operator(after, False) == "|":
                    marks[start] += 1
                    marks[index + 1] -= 1
    for start, piped in opened:  # (Open to the line's end: its last `)` is not kept.)
        marks[start] += piped
    return [running > 0 for running in itertools.accumulate(marks[:-1])]


def loop_ends(commands: list[tuple[str, list[str]]]) -> dict[int, int]:
    """Where each loop a line opens ends: the command that opens it, to the one with its
    `done` (the line's last when there is none)."""
    ends, opened = {}, []
    for index, (_, words) in enumerate(commands):
        for word in words:
            if word in LOOPS:
                opened.append(index)
            elif word == "done" and opened:
                ends[opened.pop()] = index
            elif word not in KEYWORDS:
                break
    for index in opened:
        ends[index] = len(commands) - 1
    return ends


# In a joiner: a case's clause ends (`;;`, `;&`, `;;&`), parentheses, and the rest.
JOINER_PARTS = re.compile(r";;&?|;&|[()]|[^;()]+|;")


def subshells(
    commands: list[tuple[str, list[str]]],
) -> tuple[list[str], list[int], list[str]] | None:
    """What the parentheses before each command do, in order: a subshell starts (`(`) or ends
    (`)`); a case pattern's (`(x)`, `x|y)`) do neither. How many subshells each command runs
    in; and its joiner without a case pattern's parentheses. None when they do not pair as
    the shell's do, or a `case` is not read here."""
    events, depths, joiners, depth = [], [], [], 0
    cases: list[int] = []  # The depth of each case being read.
    expecting = worded = opened = False  # A pattern is next; its words, its `(`, seen.
    for joiner, words in commands:
        done, kept = "", ""
        for part in JOINER_PARTS.findall(joiner):
            if part.startswith(";;") or part == ";&":
                if not cases or depth != cases[-1] or expecting:
                    return None
                expecting, worded, opened = True, False, False
            elif part == "(" and expecting:
                if worded or opened:
                    return None
                opened = True
                continue
            elif part == "(":
                depth += 1
                done += "("
            elif part == ")" and expecting:
                if not worded:
                    return None
                expecting = False
                kept += ";"  # (What follows a pattern is a list of its own.)
                continue
            elif part == ")":
                if depth <= (cases[-1] if cases else 0):
                    return None
                depth -= 1
                done += ")"
            kept += part
        first = next((i for i, word in enumerate(words) if word not in KEYWORDS), len(words))
        rest = words[first:]
        if expecting and not worded and not opened and words[:1] == ["esac"]:
            expecting = False
            cases.pop()
        elif expecting:
            worded = worded or bool(words)
        elif rest[:1] == ["case"] and not (first and words[first - 1] in {"for", "select"}):
            if len(rest) < 3 or rest[2] != "in":
                return None
            cases.append(depth)
            expecting, worded, opened = True, len(rest) > 3, False
        elif rest[:1] == ["esac"] and (not cases or cases.pop() != depth):
            return None  # (An `esac` closing no `case` of this depth.)
        events.append(done)
        depths.append(depth)
        joiners.append(kept)
    if cases or expecting:  # (Subshells still open end the line: its last `)` is not kept.)
        return None
    return events, depths, joiners


@dataclass
class Line:
    """What reading a line keeps as it goes: git's own variables it set (`env`; `env_unsure`
    when one may not have been), the values of those it surely set in the one place it sets
    them (`values`: a loop's several, while in it), its folder (LOST when not known; `chain`:
    the mark of the `&&` chain a `cd` that may not have run moved in), what it did to each
    folder's branch, and how many joins were not `&&`, up to each simple command (`marks`).
    What a subshell sets ends with it: each change to `values` and `env` is noted (`undo`) to
    be put back where one ends (restore)."""

    folder: str
    depth: int
    tally: Tally
    env: dict[str, str] = field(default_factory=dict)
    env_unsure: bool = False
    values: dict[str, tuple[str, ...]] = field(default_factory=dict)
    loops: list[tuple[str, int]] = field(default_factory=list)
    switched: dict[str, Switched] = field(default_factory=dict)
    # As 0.1.45 read them: a checkout of a name only "may have gone there" (what counts when
    # git has no branch of that name: Push.alt_new).
    switched_old: dict[str, Switched] = field(default_factory=dict)
    # How many times the folder or a branch may have changed so far (a variable set to the
    # checkout's branch is that branch only while this stays: captured).
    epoch: int = 0
    text: str = ""  # The line itself (says_push).
    saying: bool | None = None

    marks: list[int] = field(default_factory=list)
    chained: list[bool] = field(default_factory=list)  # Only `&&` from its list's start to it.
    chain: int | None = None
    lost: bool = False
    piped: bool = False
    looping: int = 0  # Loops the command being read is in (run again: in no order).
    alongside: bool = False  # In a group that is part of a pipe: in no order with its others.
    undo: list[tuple[dict[str, Any], str, Any]] = field(default_factory=list)
    # Where `cd -` goes (the folder before the last move) and pushd's folders (popd goes to the
    # last): None when not known (before any move, the shell's own: not known).
    previous: str | None = None
    dirs: tuple[str, ...] | None = ()
    # cd as the system goes, for the rest of the line (`set -P`, `set -o physical`, zsh's
    # `setopt chase_links`); `physical_unsure`: whether may not be known (a cd through `..`:
    # then not known); `moded`: whether the line changed either.
    physical: bool = False
    physical_unsure: bool = False
    moded: bool = False

    def lose(self):
        """The folder, the one before it and pushd's: not known from here."""
        self.folder, self.chain, self.previous, self.dirs = LOST, None, None, None
        self.epoch += 1

    def says_push(self) -> bool:
        """Whether a word of this line (not of the line running it) may say push, however it
        is quoted: found once a line."""
        if self.saying is None:
            text = squashed(self.text)
            self.saying = "push" in text or "send-pack" in text
        return self.saying

    def put(self, store: dict[str, Any], name: str, value: Any = None):
        """Sets a value the line keeps (`values`, `env`), or drops it (None), noted in `undo`."""
        self.undo.append((store, name, store.get(name)))
        if value is None:
            store.pop(name, None)
        else:
            store[name] = value

    def restore(self, mark: int):
        """Puts back the values and git variables the line had when `undo` was `mark` long."""
        while len(self.undo) > mark:
            store, name, value = self.undo.pop()
            if value is None:
                store.pop(name, None)
            else:
                store[name] = value

    def said(self, name: str) -> tuple[str, ...]:
        """The values a variable the line surely set holds (none when not known: Tally.known,
        asked when one is read)."""
        said = self.values.get(name, ())
        return said if said and self.tally.known(name) else ()

    def value_of(self, name: str) -> str | None:
        """The value a variable holds, when one (a loop's holds several)."""
        said = self.said(name)
        return said[0] if len(said) == 1 else None

    def assign(self, name: str, said: list[str], certain: bool):
        """`name` set to one of these words (`NAME=value`, `for NAME in …`), for the words after
        it: known only when the line surely set it here (`certain`: not in a condition, a
        loop, a subshell or a pipe, nor after `&&` or `||`), at its top (not in a line a
        command runs: `eval`, `sh -c`, a substitution), in no other place (Tally.known, asked
        when it is read: said), and each value is plain text or one substitution's whole."""
        self.put(self.values, name)
        if not certain or self.depth or not NAME.fullmatch(name):
            return
        values = tuple(
            captured(self.epoch) if value == CURRENT else value
            for value in (resolved(value, self.value_of, self.tally.modifiers) for value in said)
        )
        if 0 < len(values) <= MOST_VALUES and all(
            value == MADE_UP or CAPTURED.fullmatch(value) or not UNSETTLED.search(value)
            for value in values
        ):
            self.put(self.values, name, values)

    def cd(self, name: str, arguments: list[str], certain: bool, mark: int):
        """Where `cd` goes (`cd -`: to the folder before; `pushd FOLDER`, and `popd` back to the
        folder pushd left; any other: not known), read as the shell reads it (`cd -P`: as the
        system does; a name that is no folder so, but is one as the system reads it: not
        known, bash goes there). When it may not have run (`certain` is false), only the
        commands of its `&&` chain after it surely run there."""
        rest, target, physical = list(arguments), None, self.physical
        unsure = self.physical_unsure
        while name == "cd" and rest and rest[0].startswith("-") and rest[0] != "-":
            option = rest.pop(0)
            if option == "--":
                break
            if not set(option[1:]) <= set("LPe@"):
                rest = ["-", "-"]  # (An option not read here, `cd -2` in zsh: not known.)
                break
            for letter in option[1:]:  # (The last of -L and -P counts.)
                physical = {"P": True, "L": False}.get(letter, physical)
                unsure = unsure and letter not in "LP"
        before, dirs = self.folder, self.dirs
        if name == "popd":
            if not rest and dirs:
                target, dirs = dirs[-1], dirs[:-1]
        elif name == "cd" and not rest:
            target = "~"
        elif name == "cd" and rest == ["-"]:
            target = self.previous
        elif (name == "cd" and len(rest) == 1 and rest[0] != "-") or (
            name == "pushd" and len(rest) == 1 and not rest[0].startswith(("-", "+"))
        ):
            target = resolved(rest[0], self.value_of, self.tally.modifiers)
            named = self.tally.named()[0]
            if (named.get("CDPATH") or (self.tally.zsh and named.get("cdpath"))) and (
                not target.startswith(("/", ".", "~"))
            ):
                target = None  # Searched in CDPATH's folders first: which, not known.
            if name == "pushd":
                dirs = None if dirs is None else (*dirs, before)
        if target is None or LOST in target or (unsure and ".." in target.split("/")):
            self.lose()  # (Through `..` when `set -P` may have run: as the system goes or not.)
            return
        folder = self.tally.moved(before, target, physical)
        if not physical and LOST not in folder and not Path(folder).is_dir():
            # Not there as the shell reads the name: bash then goes where the system resolves
            # it (a link's `..`: its target's parent). There, if that is a folder: not known.
            system = self.tally.moved(before, target, True)
            if LOST not in system and Path(system).is_dir():
                self.lose()
                return
        self.folder = folder
        self.previous, self.dirs = before, dirs
        self.chain = None if certain else mark
        self.epoch += 1
        if not certain and not self.chained[-1]:  # (`x || cd y && …`: it may not have run.)
            self.lose()

    def end_loops(self, nest: int):
        while self.loops and self.loops[-1][1] > nest:
            self.put(self.values, self.loops.pop()[0])

    def modes(self, name: str, arguments: list[str], certain: bool):
        """How `set` (`-P`, `+P`, `-o physical`; zsh's `-w`, `-o chase_links`), `shopt -s -o
        physical` or zsh's `setopt`/`unsetopt` (`chase_links`, `chase_dots`) has cd read `..`,
        for the rest of the line: as the system goes or as the shell does. One that may not
        have run, or that is not read here (a flag of setopt's, zsh's `set -P`, an option of
        another shell's, a name the line makes up): how is not known."""
        zsh, turned, unsure = self.tally.zsh, None, False

        def take(value: str, on: bool, own: bool):
            nonlocal turned, unsure
            if (found := chase(value, zsh)) is not None and found[1] and own:
                turned = on != found[0]
            elif found is not None or not PLAIN_NAME.fullmatch(value):
                unsure = True

        if name == "set":
            letter = "w" if zsh else "P"  # (zsh's CHASE_LINKS; bash's physical.)
            at = 0
            while at < len(arguments):
                word, at = arguments[at], at + 1
                if word in {"--", "-", "+"} or word[:1] not in {"-", "+"}:
                    break  # (What follows: the positional parameters.)
                on, letters = word[0] == "-", word[1:]
                if not letters.isalpha():
                    unsure = True  # (`--x`, `-$X`: not read here.)
                    continue
                if letter in letters:
                    turned = on
                unsure = unsure or (zsh and "P" in letters)
                if "o" in letters and at < len(arguments):
                    value, at = arguments[at], at + 1
                    take(value, on, True)
        elif name == "shopt":
            flags, names = "", list(arguments)
            while names and names[0].startswith("-") and len(names[0]) > 1 and names[0] != "--":
                flags += names.pop(0)[1:]
            names = names[1:] if names[:1] == ["--"] else names
            setting = ("s" in flags) != ("u" in flags) and set(flags) <= set("psuqo")
            if "o" in flags and (setting or set(flags) - set("pqo")):
                for value in names:
                    take(value, "s" in flags, setting and not zsh)
        else:
            for word in arguments:
                if word[:1] in {"-", "+"}:
                    unsure = True  # (Its own flags: not read here.)
                else:
                    take(word, name == "setopt", zsh)
        if turned is None and not unsure:
            return
        self.moded = True
        if unsure or not certain:
            self.physical_unsure = True
        elif turned is not None:
            self.physical = turned


def chase(option: str, zsh: bool) -> tuple[bool, bool] | None:
    """Whether an option's name is one that has cd go as the system does (bash's `physical`;
    zsh's `chase_links`, `CHASE_DOTS`, `nochaselinks`, case and `_` aside): (negated, whether
    this shell takes it); or None."""
    name = option.lower().replace("_", "")
    negated = name.startswith("no") and name[2:] in {"chaselinks", "chasedots", "physical"}
    base = name[2:] if negated else name
    if base not in {"physical", "chaselinks", "chasedots"}:
        return None
    if zsh:
        return negated, base != "physical"
    return False, option == "physical"


def shell_chases(words: list[str]) -> bool:
    """Whether a shell's options (its words before its script) may have its cd read `..` as
    the system goes: `bash -P`, `-o physical`, zsh's `-w`, `--chase-links`, or an option the
    line makes up."""
    for at, word in enumerate(words):
        if word.startswith(("--", "++")):
            if chase(word[2:].replace("-", ""), True) is not None or not PLAIN_NAME.fullmatch(word):
                return True
        elif word[:1] in {"-", "+"}:
            letters = word[1:]
            if not letters.isalpha() or "P" in letters or "w" in letters:
                return True
            if "o" in letters and at + 1 < len(words):
                value = words[at + 1]
                if chase(value, True) is not None or not PLAIN_NAME.fullmatch(value):
                    return True
    return False


# Where the shells keep aliases as variables (bash's BASH_ALIASES, zsh's aliases).
ALIAS_TABLES = ("BASH_ALIASES", "aliases", "galiases", "saliases")


def aliases_may_push(values: list[str], tally: Tally) -> bool:
    """Whether aliases with these texts may push: any, on a line that says push anywhere (an
    alias's words join the words after it: `alias g=git; g push origin main`); one that holds
    what the line makes up as it runs, or that says push itself."""
    if not values:
        return False
    return tally.says("push") or any(may_push(value) or UNSETTLED.search(value) for value in values)


def moves_folder(joiner: str, words: list[str], devices: bool = False) -> bool:
    """Whether a command may go to another folder (`cd`, an evaluator, a function's
    definition, a command whose name the line makes up). Not `. FILE` or `source FILE`: what
    a file runs is not read here (nor what `sh FILE` runs); unless what it reads may be what
    the line feeds it (`/dev/stdin`; a path in a variable, in a line naming a device:
    `devices`)."""
    first = next((i for i, word in enumerate(words) if word not in KEYWORDS), len(words))
    name, start = command_name(words[first:])
    file = words[first + start] if first + start < len(words) else ""
    sourced = (
        name in {".", "source"}
        and bool(file)
        and not file.startswith(("-", "/dev/", "/proc/"))
        and (bool(PLAIN_NAME.fullmatch(file)) or not devices)
    )
    return bool(
        "()" in joiner.replace(" ", "")
        or name in {"cd", "pushd", "popd", "function"}
        or (name in EVALUATORS and not sourced)
        or (name and not PLAIN_NAME.fullmatch(name))
    )


def read_line(command: str, cwd: str, depth: int, tally: Tally) -> str:
    """Each `git push` in a command line, as the shell would run it, into `tally`: wherever
    `git` stands in a simple command (after `if`, `do`, `{`, `timeout 60`, `env A=b`…), in
    `sh -c '…'`, `eval` and command substitutions, after `cd`, with configuration and variables
    set on the line, and the branch a line made and switched to. Read in one pass: a simple
    command is read up to the first `git`, shell `-c` or `eval` in it. What a command changes
    counts for those after it only where it surely ran: a subshell's changes end with it, a
    piped command's are its own, a conditional `cd` holds for its `&&` chain (after it, the
    folder is not known), and after an evaluator or a function's definition, too. "" once read
    in full, else why not (TOO_COMPLEX; or raises TooComplexError)."""
    if depth > MOST_NESTED:
        return TOO_COMPLEX
    tally.reads(command)
    parsed = tally.parse(command)
    if parsed is None:  # Not a command line the shell runs: read loosely (any alias may run).
        tally.pushes += loose_pushes(command, cwd)
        tally.pushes.append(Push(nested=bool(depth), alias="*", via=Git(cwd, {}, [], False)))
        return ""
    commands, inside = parsed
    if sum(len(words) for _, words in commands) > MOST_WORDS:
        return TOO_COMPLEX
    line = Line(cwd, depth, tally, text=command)
    start_modes = line.physical, line.physical_unsure = tally.modes
    broken, nest, ordered = 0, 0, True
    # Where each open subshell started: its folder, chain, undo mark, nest, and cd's.
    saved: list[tuple[Any, ...]] = []
    devices = bool(DEVICES.search(command))
    moving = [bool(cwd) and moves_folder(*command, devices) for command in commands]
    ends = loop_ends(commands)
    # The subshells (when their parentheses are not read: after one ends, the folder is not
    # known, and what is in one is not taken for sure).
    paired = subshells(commands)
    doubtful = paired is None
    raw = [re.sub(r"[^()]", "", joiner) for joiner, _ in commands] if doubtful else []
    events, depths, joiners = paired or (raw, [], None)
    jobs = background(commands, joiners)
    pipes = in_pipes(commands, joiners)
    tally.background = tally.background or bool(PROCESS.search(command))
    # Loops run again after a command in them went elsewhere (not in a subshell of their own).
    loops_moving = list(itertools.accumulate(moving, initial=0))  # How many, before each.
    lost_loops, budget = set(), 4 * len(commands) + 64
    for start, end in ends.items():
        if loops_moving[end + 1] == loops_moving[start]:
            continue
        budget -= end - start + 1
        if (
            doubtful
            or budget < 0
            or any(moving[at] and depths[at] <= depths[start] for at in range(start, end + 1))
        ):
            lost_loops.add(start)
    for index, (joiner, words) in enumerate(commands):
        # A pipe's commands are one item of its list (`a && b | c && d`: b | c runs if a did,
        # and d if c did): `&&` and `|` go on with the chain.
        going_on = joiner.replace("\n", "") in {"&&", "|"}
        broken += bool(index) and not going_on
        line.marks.append(broken)
        operators = joiner.replace("&&", "")
        listed = not index or bool(set(operators) & set(";\n&("))  # A list starts here.
        line.chained.append(listed or (going_on and line.chained[-1]))
        after = commands[index + 1][0] if index + 1 < len(commands) else ""
        for event in events[index]:  # A subshell's changes end with it.
            if event == "(":
                saved.append(
                    (line.folder, line.chain, len(line.undo), nest, line.previous, line.dirs,
                     line.physical, line.physical_unsure)
                )  # fmt: skip
            elif doubtful:
                line.lose()
                del saved[-1:]
            elif saved:
                (line.folder, line.chain, mark, _, line.previous, line.dirs, line.physical,
                 line.physical_unsure) = saved.pop()  # fmt: skip
                line.restore(mark)
                line.epoch += 1
        line.lost = line.lost or "()" in joiner.replace(" ", "")  # A function: called anywhere.
        if line.lost or (line.chain is not None and line.chain != broken):
            if cwd:
                line.lose()
            else:
                line.chain = None
        first = next((i for i, word in enumerate(words) if word not in KEYWORDS), len(words))
        for word in words[:first]:
            nest += (word in OPENERS) - (word in CLOSERS)
            line.looping += (word in LOOPS) - (word == "done")
            if word == "done":
                line.end_loops(nest)
        if index in lost_loops:
            line.lose()  # Run again after a `cd` in it: not known.
        rest = words[first:]
        name, start = command_name(rest)
        nest += (name == "case") - (name == "esac")
        ordered = ordered and nest >= 0
        before, following = joiner.rsplit("(", 1)[-1], after.split(")", 1)[0]
        # In a pipe: a subshell of its own (what it changes is its own). In the background:
        # in order with its list only (a `cd` there holds for its `&&` chain).
        piped = "|" in before.replace("||", "") or "|" in following.replace("||", "")
        tally.background = tally.background or "&" in joiner.replace("&&", "")
        line.piped = piped or jobs[index]
        line.alongside = pipes[index]
        # Surely run, in order, where it stands: in its subshell (from its start), if in one.
        opening = joiner.rsplit("(", 1)[-1] if "(" in events[index] else joiner
        certain = ordered and not line.piped and not set(opening) - set(";\n)")
        if doubtful:
            certain = certain and nest == 0 and not saved
        else:
            certain = certain and nest == (saved[-1][3] if saved else 0)
        # zsh's `command` runs no builtin (an external one, maybe none), and its `time` takes no
        # options (`time -p cd x` runs `-p`): what such a builtin would change may not be.
        wrapped = rest[: start - 1] if start else []
        unrun = tally.zsh and any(w == "command" or w.startswith("-") for w in wrapped)
        if rest and all(isinstance(word, Assignment) for word in rest):
            set_variables(line, rest, certain)  # `NAME=value` alone: for what follows.
        elif name == "export":
            set_variables(line, rest[start:], certain and not unrun)
        elif first and words[first - 1] in {"for", "select"} and len(rest) > 1 and rest[1] == "in":
            line.loops.append((rest[0], nest))
            line.assign(rest[0], rest[2:], certain=True)  # Each of those, in turn, in the loop.
        elif name in {"set", "shopt", "setopt", "unsetopt"}:
            line.modes(name, rest[start:], certain and not unrun)
        elif name in {"cd", "pushd", "popd"}:
            if cwd and unrun:
                line.lose()
            elif cwd and not piped:
                line.cd(name, rest[start:], certain, broken)
            elif cwd and tally.zsh:
                line.lose()  # zsh runs a pipe's last command in the shell itself.
        else:
            if why := read_command(words, index, line):
                return why
            if name == "alias" and aliases_may_push(
                [word.partition("=")[2] for word in rest[start:] if "=" in word], tally
            ):
                tally.pushes.append(Push(unknown=True))  # A name that may push from here on.
            if (not piped or tally.zsh) and (name == "function" or moving[index]):
                # (It may have run `set -P`; in zsh, a pipe's last command runs in the shell.)
                line.physical_unsure = line.moded = True
                if cwd:
                    line.lose()  # It may have gone elsewhere.
                    line.lost = line.lost or name == "function"
        if len(line.env) > MOST_GIT_ENV:
            return TOO_COMPLEX
    # What its command substitutions run: lines of their own, in the folder the line starts in
    # (not known when a command of it may go elsewhere first).
    inner = LOST if any(moving) else cwd
    for body in inside:
        tally.modes = (start_modes[0], start_modes[1] or line.moded)  # (Where it stands: as there.)
        if (may_push(body, switching=True) or may_alias(squashed(body))) and (
            why := read_line(body, inner, depth + 1, tally)
        ):
            return why
    return ""


def set_variables(line: Line, words: list[str], certain: bool):
    """The variables these assignments set, for what follows on the line: git's own (its
    configuration; when one may not have been set, git's configuration is not known), and any
    whose value is plain text (put in a later refspec: Line.assign)."""
    for word in words:
        if not isinstance(word, Assignment):
            continue
        name, _, value = word.partition("=")
        table = name.split("[", 1)[0].rstrip("+")
        if table in ALIAS_TABLES and aliases_may_push([value], line.tally):
            line.tally.pushes.append(Push(unknown=True))  # An alias that may push.
        if table == "options" and line.tally.zsh:  # (zsh's options, by name: `chase_links`…)
            line.physical_unsure = line.moded = True
        if name.startswith("GIT_"):
            if certain:
                line.put(line.env, name, value)
            else:
                line.env_unsure = True
        line.assign(name, [value], certain)


def git_variables(words: list[str]) -> dict[str, str]:
    """git's own variables these `NAME=value` words set (others say nothing of a push)."""
    found = {}
    for word in words:
        name, has, value = word.partition("=")
        if has and name.startswith("GIT_") and ASSIGNMENT.match(word):
            found[name] = value
    return found


def read_command(words: list[str], index: int, line: Line) -> str:
    """One simple command, read up to its first `git`, shell `-c` or `eval`; through the
    commands that run another (`env -C DIR`: there; `find … -execdir`: in folders not known;
    `xargs`, `parallel`, `fd -x`: with words they add to it, not known here)."""
    local: dict[str, str] = {}
    folder, feeds, found = line.folder, "", False
    first = words[0] if words else ""
    plain = PLAIN_NAME.fullmatch(first) and first not in FED_WRAPPERS and first not in KEYWORDS
    named = [] if plain else unwrapped(words)[0][:1]  # (The command, past `env`, `if`… only.)
    if named and UNSETTLED.search(named[0]) and line.says_push():
        line.tally.pushes.append(Push(unknown=True, anonymous=True))  # (Its name: not known.)
    elif (named and UNSETTLED.search(named[0])) or (
        first.rsplit("/", 1)[-1] in RUNNERS and any(UNSETTLED.search(word) for word in words)
    ):  # (A git it names may run an alias that pushes: `$G p`, `find … -exec $G p`.)
        via = Git(line.folder, {}, words, False)
        line.tally.pushes.append(Push(nested=bool(line.depth), alias="$", via=via))
    for position, word in enumerate(words):
        if ASSIGNMENT.match(word):
            local.update(git_variables([word]))
            continue
        if line.tally.zsh and word.startswith("=") and len(word) > 1:
            word = word[1:]  # (zsh's `=git`: git's path.)
        program = program_of(word)
        if program == "git":
            here = {**line.env, **local} if local else line.env
            if line.env_unsure:  # git's own variables, maybe set: what it does, not known.
                here = {**here, "GIT_CONFIG_PARAMETERS": "push alias.", "GIT_DIR": "?"}
            given = [word, *words[position + 1 :]]
            if found:  # (`find … -exec git … ;`: its words end at find's `;` or `+`.)
                given = list(itertools.takewhile(lambda word: word not in {";", "+"}, given))
                if any("{}" in word for word in given):  # A found file's name: not known.
                    folder, feeds = LOST, feeds or "{}"
            for run in expansions(given, line.said, line.tally):
                read_git(run, here, index, line, folder, feeds)
            return ""
        if program in SHELLS:  # Its words are its own: a script's (`-c`), or a file's.
            script = shell_script(words, position)
            if script is None:
                return ""
            # Read as that shell reads it; cd as the system goes if its options say so, or if
            # the line may hand it bash's options (SHELLOPTS).
            options = words[position + 1 :]
            chases = shell_chases(options) or line.tally.says("shellopts")
            return read_code(script, line, folder, program, (False, chases))
        if program == "eval":
            return read_code(" ".join(words[position + 1 :]), line, folder)
        if program == "emulate":  # (zsh's: `emulate sh -c '…'` runs code, as eval does.)
            after = words[position + 1 :]
            script = after[after.index("-c") + 1] if "-c" in after[:-1] else None
            return "" if script is None else read_code(script, line, folder)
        if program == "env":
            moved, script = env_options(words[position + 1 :])
            if moved == LOST:
                folder = LOST  # An option not read here.
            elif moved is not None:
                target = resolved(moved, line.value_of, line.tally.modifiers)
                folder = LOST if LOST in folder else line.tally.moved(folder, target, True)
            if script is not None:
                return read_code(script, line, folder)
        elif program == "find":
            found = True
            if {"-execdir", "-okdir"} & set(words[position + 1 :]):
                folder = LOST  # Each found file's folder.
        elif program in FEEDERS:
            feeds = feeds or placeholder(program, words[position + 1 :])
    if pushed_by_a_name(words) and line.says_push():
        line.tally.pushes.append(Push(unknown=True, anonymous=True))  # (`find … -exec $G push`.)
    return ""


def pushed_by_a_name(words: list[str]) -> bool:
    """Whether a command that runs no git it names may run one a variable or a substitution
    names, with `push` (or `send-pack`) after it: a command it runs (`find … -exec`, `watch`,
    `caffeinate`), whatever runs it."""
    named = False
    for word in words:
        named = named or bool(UNSETTLED.search(word))
        if named and word in {"push", "send-pack"}:
            return True
    return False


# Commands that run another with words of their own added (each line of their input, a found
# file's name), unless told where to put them (`xargs -I {}`); and others that run one.
FEEDERS = frozenset({"xargs", "parallel", "fd", "fdfind"})
RUNNERS = FEEDERS | {"find", "watch"}


def placeholder(program: str, words: list[str]) -> str:
    """What a feeder puts its words in place of (`xargs -I R`, `-J R`, `--replace[=R]`, `-i`),
    or "*" when it adds them at the end (or where cannot be told)."""
    if program == "xargs":
        for at, word in enumerate(words):
            name, has, value = word.partition("=")
            if name == "--replace":
                return value if has and value else "{}"
            if word in {"-I", "-J"} and at + 1 < len(words):
                return words[at + 1] or "*"
            if word.startswith(("-I", "-J")) and len(word) > 2:
                return word[2:]
            if word.startswith("-i"):
                return word[2:] or "{}"
            if not word.startswith("-"):
                break
    return "*"


# env's options, GNU's and BSD's: those that take no value, and those that take one (`-C DIR`,
# `-CDIR`, `-iC DIR`; `--chdir=DIR`, `--chdir DIR`); a long one with `=` only may take one.
ENV_FLAGS = frozenset("i0v")
ENV_VALUED = frozenset("CPSuLUa")
ENV_LONG_FLAGS = frozenset({"--ignore-environment", "--null", "--debug", "--list-signal-handling"})
ENV_LONG_VALUED = {"--chdir": "C", "--split-string": "S", "--unset": "u", "--argv0": "a"}
ENV_LONG_OPTIONAL = frozenset({"--block-signal", "--default-signal", "--ignore-signal"})


def env_options(words: list[str]) -> tuple[str | None, str | None]:
    """What `env` with these words does before the command it runs: the folder it goes to
    (`-C DIR`, also in a cluster: `-iC DIR`, `-iCDIR`), and a command line it is given as one
    word (`-S '…'`). An option not read here: the folder is not known (LOST)."""
    folder = script = None
    at = 0
    while at < len(words):
        word, at = words[at], at + 1
        if word == "--":
            break
        if word == "-":  # (As -i.)
            continue
        found: list[tuple[str, str]] = []  # (letter, value) of the options that take one.
        if word.startswith("--"):
            name, has, value = word.partition("=")
            if name in ENV_LONG_VALUED:
                if not has:
                    value, at = (words[at] if at < len(words) else ""), at + 1
                found.append((ENV_LONG_VALUED[name], value))
            elif name not in ENV_LONG_FLAGS | ENV_LONG_OPTIONAL or (has and name in ENV_LONG_FLAGS):
                return LOST, None
        elif word.startswith("-"):
            for index, letter in enumerate(word[1:], 1):
                if letter in ENV_FLAGS:
                    continue
                if letter not in ENV_VALUED:
                    return LOST, None
                value = word[index + 1 :]
                if not value:
                    value, at = (words[at] if at < len(words) else ""), at + 1
                found.append((letter, value))
                break
        else:
            break
        for letter, value in found:
            if letter == "C":
                folder = value
            elif letter == "S":
                return folder, " ".join([value, *words[at:]])
    return folder, script


def read_code(
    script: str,
    line: Line,
    folder: str | None = None,
    shell: str | None = None,
    modes: tuple[bool, bool] | None = None,
) -> str:
    """A line a command runs as code (`eval …`, `sh -c …`), from the folder it runs in (the
    line's, or `folder`: `env -C DIR sh -c …`); by the shell running the line, or another
    (`shell`: `zsh -c …` is read as zsh reads it), with cd reading `..` as in the line, or
    as `modes` says (Tally.modes). A `$PWD` in it may have been put in where it was written
    (in double quotes): it is the folder only where that is the folder it started in
    (Tally.here)."""
    folder = line.folder if folder is None else folder
    tally = line.tally
    before = (tally.here, tally.shell, tally.modes, tally.outer_zsh)
    tally.here = folder if before[0] in (None, folder) and folder == line.folder else LOST
    if shell is not None:
        tally.outer_zsh, tally.shell = tally.modifiers, shell
    tally.modes = (line.physical, line.physical_unsure) if modes is None else modes
    try:
        return read_line(handed(script, line, folder), folder, line.depth + 1, tally)
    finally:
        tally.here, tally.shell, tally.modes, tally.outer_zsh = before


def handed(script: str, line: Line, folder: str) -> str:
    """A line's text handed to a command that runs it (`eval`, `sh -c`), the checkout's branch
    as its substitutions made it in (CURRENT, or a variable that still holds it: captured) put
    in as what the line run holds from its start (captured(0)): its branch only until a `cd`
    or a switch there (Line.epoch); not known when it runs elsewhere (`env -C DIR sh -c`)."""
    if CURRENT not in script:
        return script

    def put(found: re.Match) -> str:
        now = found.group(1) is None or int(found.group(1)) == line.epoch
        return captured(0) if now and folder == line.folder else MADE_UP

    return CURRENTS.sub(put, script)


def read_git(
    words: list[str],
    env: dict[str, str],
    index: int,
    line: Line,
    folder: str | None = None,
    feeds: str = "",
):
    """One `git …` run (in the line's folder, or `folder`): a push, into the line's tally; a
    checkout, switch or branch, kept for the pushes after it. With words a command adds to it
    (`feeds`: see placeholder), which and where are not known."""
    git = git_command(words, env, line.folder if folder is None else folder, line.tally)
    if git is None:
        return
    if folder is not None and folder != line.folder:
        git.moved = True  # (`env -C DIR git push origin "$(…)"`: that runs in the shell's folder.)
    git = aliases_run(git, index, line)
    fed = feeds == "*" or bool(feeds and any(feeds in word for word in words[1:]))
    if fed and git.rest[0] in SWITCHING:
        record_switch(git, index, line, ("unknown", ""))
        return
    if git.rest[0] == "config":
        sets, alias, switches = config_change(git.rest)
        line.tally.config_set = line.tally.config_set or sets
        if alias and any(line.tally.says(word) for word in PUSHING):
            line.tally.pushes.append(Push(unknown=True))  # An alias that may push, from here on.
        if switches:  # An alias that switches, from here on: where to, not known.
            record_switch(git, index, line, ("unknown", ""))
    removes, upstream = branches_changed(git.rest)
    line.tally.unbranched = line.tally.unbranched or removes
    line.tally.upstreams_set = line.tally.upstreams_set or upstream
    git.epoch, git.currents = line.epoch, line.tally.currents
    name = git.rest[0]
    if name not in UNALIASED and f"alias.{name.lower()}" not in git.config:
        aliased = bool(ALIAS_NAME.fullmatch(name))
        if aliased or UNSETTLED.search(name):  # (An alias `-c` gives: push_of reads it.)
            via = Git(**{**vars(git), "rest": [*git.rest, *([FED] if fed else [])]})
            unknown = git.elsewhere or any(key in ALIAS_SOURCES or key.startswith("includeif.")
                                           for key in git.config)  # fmt: skip
            alias = name.lower() if aliased else "?"
            candidate = Push(nested=bool(line.depth), alias=alias, via=via, alias_unknown=unknown)
            candidate.bare_in = git.folder
            line.tally.pushes.append(placed_bare(candidate, index, line))
    if push := push_of(git):
        push.unknown = push.unknown or fed
        push.nested = bool(line.depth)
        line.tally.pushes.append(placed_bare(push, index, line))
    else:
        record_switch(git, index, line)


def aliases_run(git: Git, index: int, line: Line) -> Git:
    """A git command that runs an alias, read as the command it runs where that is known as the
    line is read: an alias `-c` gives (but one that may push: push_of reads it; one that runs
    another, of git's configuration: Push.alias), or one of git's configuration that switches
    branches (Tally.aliases: judged). One that may switch, not known: a switch to a branch not
    named."""
    for _ in range(MOST_ALIASES):
        name = git.rest[0]
        if name in UNALIASED or UNSETTLED.search(name):
            return git
        key = f"alias.{name.lower()}"
        if key in git.config:
            value = git.config[key]
            if (
                value is None
                or len(value) > MOST_VALUE
                or value.startswith("!")
                or any(word in value.lower() for word in PUSHING)
            ):
                text = " ".join([value or "", *git.rest[1:]]).lower()
                if value is None or (
                    value.startswith("!")
                    and (any(word in text for word in EXPANDED) or UNSETTLED.search(text))
                ):
                    record_switch(git, index, line, ("unknown", ""))  # (A shell's: which?)
                return git
            words = alias_words(value)
        elif (git.folder, name.lower()) in line.tally.aliases:
            words = line.tally.aliases[(git.folder, name.lower())]
        else:
            return git
        following = git_command(["git", *words, *git.rest[1:]], {}, git.folder) if words else None
        if following is None or following.folder != git.folder or following.elsewhere:
            record_switch(git, index, line, ("unknown", ""))
            return git
        following.config = {**git.config, **following.config}
        following.elsewhere, following.moved, following.zsh = git.elsewhere, git.moved, git.zsh
        git = following
    record_switch(git, index, line, ("unknown", ""))
    return git


# A word a feeder adds to a git command (`xargs git p`): not known (Push.via). The commands no
# alias is read for (git's own, and subtree: push_of reads it).
FED = Splitting(MADE_UP)
UNALIASED = GIT_COMMANDS | {"subtree"}
# What may give git aliases of other files than the folder's configuration (`-c`, the
# environment's: environment_config).
ALIAS_SOURCES = frozenset({"alias.?", "include.path"})


def placed_bare(push: Push, index: int, line: Line) -> Push:
    """A bare push (Push.bare_in), placed after the line's switches before it: the branch it
    switched to there, where others may have gone, whether that is not known."""
    if push.bare_in is None:
        return push
    for folder, other in line.switched.items():  # One in another folder: the same repository?
        if folder != push.bare_in:
            names = other.prior_also | other.also | ({other.new} if other.new else set())
            push.bare_also |= names
            push.unknown = push.unknown or other.unknown or other.prior_unknown
    if UNSETTLED.search(push.bare_in):
        push.unknown = True  # A folder not known (LOST): a switch "there" may be elsewhere.
    elif made := line.switched.get(push.bare_in):
        new, also, unknown = switched_reading(made, index, line)
        push.bare_new, push.bare_also = new, push.bare_also | also
        push.unknown = push.unknown or unknown
        old = line.switched_old.get(push.bare_in)
        if new and made.checkout and old is not None:  # Only if git has that branch.
            push.bare_check = True
            push.alt_new, alt_also, push.alt_unknown = switched_reading(old, index, line)
            push.alt_also = frozenset(alt_also)
    return push


def switched_reading(made: Switched, index: int, line: Line) -> tuple[str, set[str], bool]:
    """Where a folder's checkout is at the command `index`, as a line's switches before it say:
    the branch it switched to last, when every join from there is `&&` (with where checkouts
    after it may have gone); else where it was before too (that switch may have failed). And
    whether it may be somewhere not named."""
    if made.new and line.marks[index] == line.marks[made.new_at] and line.chained[made.new_at]:
        return made.new, made.also, made.unknown
    also = made.prior_also | made.also | ({made.new} if made.new else set())
    return "", also, made.unknown or made.prior_unknown


# The git commands that may put a checkout on another branch.
SWITCHING = frozenset({"checkout", "switch", "branch", "symbolic-ref"})


def record_switch(git: Git, index: int, line: Line, what: tuple[str, str] | None = None) -> None:
    """What a `git checkout`, `git switch`, `git branch` or `git symbolic-ref HEAD` does to its
    folder's branch (`what`, when told), for a bare push later in the line: a branch made and
    switched to, or one switched to (a checkout's, if git has it), counts only when every join
    from there is `&&` (it may have failed, and the line gone on: `;`, `||`, a new line); kept
    as 0.1.45 read it too (Line.switched_old). One in a line a command runs, or in the
    background, may have gone anywhere before any push: Tally.aside."""
    if what is not None:
        pass
    elif git.rest[0] in {"checkout", "switch"}:
        what = switch_of(git.rest)
    elif git.rest[0] == "branch":
        what = renamed_or_made(git.rest)
    elif git.rest[0] == "symbolic-ref":
        what = pointed(git.rest)
    else:
        return
    if what is None:
        return
    kind, name = what
    marks = line.marks
    machines = (line.switched, line.switched_old)
    for machine in machines:
        machine.setdefault(git.folder, Switched())
    tally = line.tally
    if kind != "made":
        line.epoch += 1
        aside = bool(line.depth or line.piped or line.looping or line.alongside)  # In no order.
        names = tally.aside if aside else tally.top
        if name and len(names) < MOST_BRANCHES:
            names.add(name)
        if kind == "unknown" or len(names) >= MOST_BRANCHES:
            tally.aside_unknown = tally.aside_unknown or aside
            tally.top_unknown = tally.top_unknown or not aside
        if aside:
            return
    for machine in machines:
        made = machine[git.folder]
        read = "to" if machine is line.switched_old and kind in {"switched", "branch?"} else kind
        going = read in {"to", "switched", "branch?"}
        if read == "new":
            made.switched_to(name, index)
        elif read == "made":
            made.made[name] = index
        elif going and name in made.made and marks[index] == marks[made.made[name]]:
            made.switched_to(name, made.made[name])
        elif read in {"switched", "branch?"}:
            made.switched_to(name, index, read == "branch?")
        elif read in {"to", "renamed"}:
            made.also.add(name)
            made.capped()
        else:
            made.unknown = True


# What may take a branch away from a later checkout of its name (`git branch -d`/`-m`,
# update-ref, a push to the repository itself), and set a branch's upstream (`-u`, `--track`,
# a branch made: branch.autoSetupMerge).
REMOVING = frozenset({"-d", "-D", "--delete", "-m", "-M", "--move"})
UPSTREAM = frozenset({"-u", "--set-upstream-to", "--set-upstream", "-t", "--track"})
MAKING = frozenset({"-b", "-B", "-c", "-C", "--create", "--force-create", "--orphan"})


def branches_changed(rest: list[str]) -> tuple[bool, bool]:
    """Whether a git command may delete or rename a branch (a checkout of its name then goes to
    no branch: Push.bare_check), and whether it may set a branch's upstream (where
    push.default=upstream sends a push naming none)."""
    command = rest[0]
    if command not in {"update-ref", "push", "worktree", "branch", "checkout", "switch"}:
        return False, False
    options = [word.split("=", 1)[0] for word in rest[1:] if word.startswith("-")]
    clusters = "".join(option[1:] for option in options if not option.startswith("--"))
    if command == "update-ref":
        return True, False
    if command == "push":
        return [word for word in rest[1:] if not word.startswith("-")][:1] == ["."], False
    if command == "worktree":
        return False, rest[1:2] == ["add"]
    if command == "branch":
        removes = bool(REMOVING & set(options) or set(clusters) & set("dDmM"))
        made = (renamed_or_made(rest) or ("", ""))[0] == "made"
        return removes, made or bool(UPSTREAM & set(options) or set(clusters) & set("ut"))
    if command in {"checkout", "switch"}:
        return False, bool((UPSTREAM | MAKING) & set(options) or set(clusters) & set("tbBcC"))
    return False, False


# `git config`'s commands (git 2.46 and later), its options that read, change, or take a value;
# the keys of where pushes go (in lower case), and their sections.
CONFIG_ACTIONS = frozenset(
    {"list", "get", "set", "unset", "rename-section", "remove-section", "edit"}
)
CONFIG_READS = frozenset({"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l",
                          "--get-color", "--get-colorbool"})  # fmt: skip
CONFIG_CHANGES = frozenset({"--add", "--replace-all", "--unset", "--unset-all", "-e", "--edit",
                            "--rename-section", "--remove-section"})  # fmt: skip
CONFIG_VALUED = frozenset({"-f", "--file", "--blob", "--type", "--default", "--comment",
                           "--value", "--url"})  # fmt: skip
PUSH_KEYS = re.compile(
    r"push\.default|remote\..*\.(?:push|mirror)|remote\.pushdefault"
    r"|branch\..*\.(?:pushremote|merge|remote)|alias\..*|include\.path|includeif\..*",
    re.DOTALL,
)
PUSH_SECTIONS = frozenset({"push", "remote", "branch", "alias", "include", "includeif"})


def config_change(rest: list[str]) -> tuple[bool, bool, bool]:
    """What `git config` with these words may change of where pushes go: whether it sets (or
    unsets, renames, removes) push.default, a remote's push refspecs or mirror, a branch's
    upstream or push remote, an alias that may push (alias_may_push; or unsets one) or a file
    to include, or what it can't say (a key in a variable, an editor); whether it may set such
    an alias; and whether it sets one that switches branches or sets configuration (EXPANDED).
    Reading changes nothing."""
    positional, options, valued = [], set(), False
    for word in rest[1:]:
        if valued:
            valued = False
        elif word.startswith("-") and len(word) > 1:
            name = word.split("=", 1)[0]
            options.add(name)
            valued = name in CONFIG_VALUED and "=" not in word
        else:
            positional.append(word)
    action = positional[0] if positional[:1] and positional[0] in CONFIG_ACTIONS else ""
    positional = positional[1:] if action else positional
    if action in {"list", "get"} or options & CONFIG_READS:
        return False, False, False
    if action == "edit" or options & {"-e", "--edit"}:
        return True, True, True
    sections = action.endswith("-section") or bool(
        options & {"--rename-section", "--remove-section"}
    )
    if not action and not options & CONFIG_CHANGES and len(positional) < 2:
        return False, False, False  # (`git config NAME`: it reads it.)
    sets = alias = switches = False
    unsets = action == "unset" or bool(options & {"--unset", "--unset-all"})
    for key in positional[: 2 if sections else 1]:
        if UNSETTLED.search(key):
            return True, True, True
        lower = key.lower()
        part = lower.split(".", 1)[0] if sections else lower
        if not sections and part.startswith("alias."):
            value = positional[1] if len(positional) > 1 and not unsets else None
            alias = alias or value is None or alias_may_push(value)
            sets = sets or alias
            switches = switches or (value is not None and alias_switches_to(value))
            continue
        sets = sets or (part in PUSH_SECTIONS if sections else bool(PUSH_KEYS.fullmatch(part)))
        alias = alias or part == "alias"
    return sets, alias, switches or alias


def alias_may_push(text: str) -> bool:
    """Whether an alias of this text may push: a shell's (`!…`), one that says push, send-pack
    or subtree, or runs no command of git's own (another alias), or can't be read."""
    if (
        len(text) > MOST_VALUE
        or text.startswith("!")
        or UNSETTLED.search(text)
        or any(word in text.lower() for word in PUSHING)
    ):
        return True
    words = alias_words(text)
    git = git_command(["git", *words], {}, "") if words else None
    return git is None or git.rest[0] not in GIT_COMMANDS


def alias_switches_to(text: str) -> bool:
    """Whether an alias of this text (that runs git's own command) switches branches or sets
    configuration (EXPANDED)."""
    words = alias_words(text) if len(text) <= MOST_VALUE else None
    git = git_command(["git", *words], {}, "") if words else None
    return git is not None and git.rest[0] in EXPANDED


def squashed(command: str) -> str:
    """A line without quotes, backslashes and line continuations, in lower case: where a word
    of it says `push` (or an alias) however it is quoted."""
    return command.replace("\\\n", "").translate(str.maketrans("", "", "'\"\\")).lower()


def may_alias(text: str) -> bool:
    """Whether a line (squashed) may run an alias of git's (Push.alias): a word of it that
    ends in `git` (`git`, `/usr/bin/git`, `G=git`, `$(echo git)`) followed, past git's own
    options, by a word that is not one of git's commands; or a command whose name a variable
    or a substitution holds (`$G p`; also one `find`, `xargs`… run: RUNNERS)."""
    words = text.split()
    for at, word in enumerate(words):
        if not word.strip(GIT_PUNCTUATION).endswith("git"):
            continue
        at += 1
        while at < len(words) and words[at].startswith("-"):
            at += 2 if words[at] in GIT_VALUED_LOWER else 1
        if at >= len(words) or words[at].strip(GIT_PUNCTUATION) not in GIT_COMMANDS:
            return True
    for stretch in COMMAND_STRETCHES.split(text):
        named = [word for word in stretch.split() if not COMMAND_PREFIX.fullmatch(word)]
        if named and ("$" in named[0] or (named[0] in RUNNERS and "$" in stretch)):
            return True
    return False


# Where a command may start in a line (squashed), and the words before its name (assignments,
# reserved words, commands that run the next, their options).
COMMAND_STRETCHES = re.compile(r"[;&|()\n`{}]")
COMMAND_PREFIX = re.compile(
    r"[a-z_][a-z0-9_]*\+?=\S*|-\S*|[0-9]+|if|then|elif|else|do|while|until|!|time|env|command"
    r"|builtin|exec|nice|nohup|timeout|sudo|stdbuf"
)


# What may stand around git's name or its command's in a word (`$(echo git)`, `git;`).
GIT_PUNCTUATION = "$(){}`;&|<>"
GIT_VALUED_LOWER = frozenset(option.lower() for option in GIT_VALUED)


def may_push(command: str, switching: bool = False) -> bool:
    """Whether a word of a line may say `push` or an alias, however it is quoted (a line that
    says neither pushes nothing); or, `switching`, go to another branch for a push after it,
    or set where it goes (`git config`)."""
    words = squashed(command)
    if switching and any(word in words for word in (*SWITCHING, "config", "update-ref")):
        return True
    return "push" in words or "alias" in words or "send-pack" in words


# What a line may write (the invariant review of 86f9f8d): git's configuration, in a file and
# not through `git config` (where its pushes go, which aliases there are: not known then), or a
# file git's configuration includes (Pushing.includes). Read in each simple command of the line
# and of what its substitutions run (as the shell splits them; text it can't parse, loosely, in
# stretches up to a separator): a redirection's target; a word of a command that may write what
# it names (not of one that reads only: `cat`, `grep`, `sed` but `-i`, `cp`'s sources).
WRITE_STRETCHES = re.compile(r"[;&|\n`()]")
WRITE_WORDS = re.compile(r"[\s,]+")
REDIRECTED = re.compile(r"(?<!>)>+[|&]?[ \t]*([^\s<>;&|()`,]+)")
REDIRECTION = re.compile(r"[0-9]*[<>]+[&|]?")
OPTION_VALUE = re.compile(r"[\w.-]*=")
SKIPPED = KEYWORDS | WRAPPERS | {"sudo", "env", "nice", "nohup", "xargs", "then", "do"}
READERS = frozenset(
    {"cat", "bat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "rg", "ag", "ack",
     "wc", "diff", "cmp", "comm", "stat", "ls", "file", "md5", "md5sum", "shasum", "sha1sum",
     "sha256sum", "cksum", "echo", "printf", "test", "[", "[[", "realpath", "readlink",
     "basename", "dirname", "git", "cut", "nl", "od", "xxd", "hexdump", "strings", "jq", "true",
     "false", ":", "cd", "pushd", "popd", "source", ".", "awk", "gawk", "sed", "perl"}
)  # fmt: skip
COPIERS = frozenset({"cp", "mv", "install", "ln", "rsync", "scp", "ditto"})
# Text that says a line may be in a repository's own folder (`.git`, `$GIT_DIR`): a file
# `config` it writes may be git's.
GIT_DIR_TEXT = re.compile(r"(?<![\w.-])\.git(?![\w.-])|git-dir|git-path|git-common-dir|git_dir|xdg")
MOST_WRITTEN = 4096


def git_config_file(word: str, here: bool) -> bool:
    """Whether a file a line names (in lower case) may be git's configuration: the user's or
    the system's (`~/.gitconfig`, `/etc/gitconfig`, `~/.config/git/config`), a repository's
    (`.git/config`, `config.worktree`; in `$GIT_DIR`, a module's, a worktree's, a bare one's),
    or a `config` where the line may be in a repository's own folder (`here`)."""
    base = word.rsplit("/", 1)[-1]
    if "gitconfig" in base:
        return True
    if base not in {"config", "config.worktree"}:
        return False
    folder = word[: -len(base)].rstrip("/")
    if folder.endswith((".git", "/git")) or folder == "git" or ".git/" in folder:
        return True
    return here or any(char in folder for char in "$)}`" + MADE_UP)


class Writes:
    """What a line may write, as `written` reads it."""

    def __init__(self, here: bool):
        self.here = here
        self.config = False
        self.names: set[str] = set()

    def note(self, word: str):
        if found := REDIRECTION.match(word):
            word = word[found.end() :]
        if found := OPTION_VALUE.match(word):  # (`of=FILE`, `--file=FILE`.)
            word = word[found.end() :]
        word = word.strip(")}`").lower()
        if word:
            self.config = self.config or git_config_file(word, self.here)
            self.names.add(word.rsplit("/", 1)[-1] if len(self.names) < MOST_WRITTEN else "*")

    def command(self, words: list[str]):
        """A simple command's words (lower case, its redirections' targets taken out)."""
        at = 0
        while at < len(words) and (
            words[at] in SKIPPED or ASSIGNMENT.match(words[at]) or words[at].startswith("-")
        ):
            at += 1
        name = words[at].rsplit("/", 1)[-1] if at < len(words) else ""
        rest = words[at + 1 :]
        if name in {"sed", "perl"}:  # In place: `sed -i`, `perl -pi`.
            if not any(word.startswith("-") and "i" in word[1:] for word in rest):
                return
        elif name in {"awk", "gawk"} and "inplace" in rest:
            pass
        elif name in READERS:
            return
        elif name in COPIERS:  # Where it writes: its last word, or after `-t`.
            positional = [word for word in rest if not word.startswith("-")]
            targeted = any(word == "-t" or word.startswith("--target") for word in rest)
            rest = positional if targeted else positional[-1:]
            if rest and rest[-1].endswith((".git", ".git/", "/git", "/git/")):
                self.config = True  # (A file put in a repository's own folder.)
        else:
            rest = words[at:]
        for word in rest:
            self.note(word)

    def loosely(self, text: str):
        """Text the shell can't parse (squashed): in stretches up to a separator."""
        for found in REDIRECTED.finditer(text):
            self.note(found.group(1))
        for stretch in WRITE_STRETCHES.split(text):
            words = [word for word in WRITE_WORDS.split(stretch) if word]
            kept, target = [], False
            for word in words:
                if target or REDIRECTION.fullmatch(word):
                    target = not target and bool(REDIRECTION.fullmatch(word))
                    continue
                if not REDIRECTION.match(word):
                    kept.append(word)
            self.command(kept)

    def line(self, text: str) -> list[str]:
        """A line's simple commands; its redirections' targets. The scripts its commands run,
        as lines of their own (`sh -c '…'`, `find … -exec sh -c '…'`, `env -S '…'`, `eval`)."""
        try:
            tokens = shell_words(text)
        except (ValueError, TooComplexError):
            self.loosely(squashed(text))
            return []
        words: list[str] = []
        given: list[str] = []  # (Its words as they are: inner_scripts reads `env -S`.)
        scripts: list[str] = []
        target = ""  # The next word: a file written (">"), or read ("<").
        for token in [*tokens, Separator(";")]:
            if isinstance(token, Separator):
                found, at = evaluated(given)
                if found:  # (Its words: the script, read as a line; not files it names.)
                    words = words[: at + 1]
                else:
                    found = inner_scripts(given)
                    run = set(found)
                    words = [
                        w for w, as_given in zip(words, given, strict=True) if as_given not in run
                    ]
                self.command(words)
                scripts += found
                words, given, target = [], [], ""
            elif target:
                if target == ">":
                    self.note(token)
                target = ""
            elif isinstance(token, Redirecting):
                operator = token[token.at :]
                found = REDIRECTION.match(operator)
                writes = ">" in operator[: found.end() if found else 0]
                file = operator[found.end() :] if found else ""
                if file and writes:
                    self.note(file)
                elif not file:
                    target = ">" if writes else "<"
                head = token[: token.at]
                if head and not head.isdigit():
                    words.append(head.lower())
                    given.append(head)
            else:
                words.append(token.lower())
                given.append(token)
        return scripts


def evaluated(words: list[str]) -> tuple[list[str], int]:
    """What a simple command's `eval` runs (past assignments and `command`, `builtin`…): its
    words, as one line; and where `eval` stands."""
    for at, word in enumerate(words):
        if ASSIGNMENT.match(word) or word in KEYWORDS or word in WRAPPERS or word[:1] == "-":
            continue
        return ([" ".join(words[at + 1 :])], at) if word.rsplit("/", 1)[-1] == "eval" else ([], 0)
    return [], 0


@functools.lru_cache(maxsize=32)
def written(command: str, cwd: str) -> tuple[bool, frozenset[str]]:
    """Whether a line may write a git configuration file (git_config_file), and the names of
    the files it may write (without their folders, in lower case; "*" past MOST_WRITTEN): in
    its commands, its substitutions' (a heredoc's body is text, but for those) and the scripts
    its commands run (`sh -c`, `eval`…), to MOST_NESTED deep (deeper, read loosely)."""
    here = bool(GIT_DIR_TEXT.search(squashed(command))) or ".git" in Path(cwd).parts
    here = here or cwd.endswith(".git")
    writes = Writes(here)
    texts = [(command, 0)]
    while texts:
        text, depth = texts.pop()
        if depth > MOST_NESTED:
            writes.loosely(squashed(text))
            continue
        kept, bodies = without_heredocs(text)
        plain, inside = without_substitutions(without_comments(kept).replace("\\\n", ""))
        for body in bodies:
            inside += without_substitutions(body, quoted=True)[1]
        texts += [(body, depth + 1) for body in inside]
        texts += [(script, depth + 1) for script in writes.line(plain)]
    return writes.config, frozenset(writes.names)


# The shell a check's command line runs in, when not bash or sh (Codex's `zsh -lc`): see
# Tally.zsh. Set for one check (shell_denial).
SHELL: contextvars.ContextVar[str] = contextvars.ContextVar("SHELL", default="")


def reading(command: str, cwd: str) -> tuple[tuple[Push, ...], str]:
    """Each push of a command line, once each (see read_line), and why it was not read in full
    ("" when it was), as the shell of the check reads it (SHELL)."""
    return read_pushes(command, cwd, SHELL.get())


@functools.lru_cache(maxsize=32)
def read_pushes(
    command: str, cwd: str, shell: str, aliases: frozenset = frozenset()
) -> tuple[tuple[Push, ...], str]:
    """See reading. A line no word of which says `push`, an alias or git pushes nothing; one
    that says git but no push may run an alias that pushes, only (Push.alias). With `aliases`
    (alias_switches), the git commands that run those are read as what they run."""
    says = may_push(command)
    if not says and not may_alias(squashed(command)):
        return (), ""
    if len(command) > MOST_LINE:
        return (), TOO_LONG
    tally = Tally(command, cwd, shell)
    tally.currents = CURRENT[1] not in command
    tally.aliases = dict(aliases)
    try:
        why = read_line(command, cwd, 0, tally)
    except TooComplexError:
        why = TOO_COMPLEX
    # (A file of git's configuration is named `…config…`, or put in a repository's own folder:
    # a line that says neither writes none, read so at once.)
    text = squashed(command)
    configured = (
        any(part in text for part in ("config", ".git", "/git")) and written(command, cwd)[0]
    )
    tally.config_set = tally.config_set or configured
    for push in tally.pushes:
        push.unknown = push.unknown or tally.config_set  # (Where it goes: what the line set.)
        if push.alias:  # (Which aliases there are: what the line may have set.)
            push.alias_unknown = push.alias_unknown or tally.config_set or tally.homed()
        if push.bare_in is not None:
            if push.bare_check and tally.unbranched:  # That branch may be gone by then.
                push.bare_new, push.bare_also = push.alt_new, push.bare_also | push.alt_also
                push.unknown = push.unknown or push.alt_unknown
                push.bare_check, push.alt_new, push.alt_also = False, "", frozenset()
            push.upstreams_set = tally.upstreams_set
            if (push.configured or push.mapped) and tally.homed():
                push.unknown = True  # Where its configuration sends it: in a file not asked.
            anywhere = push.nested or tally.background
            push.bare_also |= tally.aside | (tally.top if anywhere else set())
            push.unknown = push.unknown or tally.aside_unknown
            push.unknown = push.unknown or (anywhere and tally.top_unknown)
            if len(push.bare_also | push.alt_also) > MOST_BRANCHES:  # Too many to say.
                push.bare_also, push.alt_also, push.unknown = frozenset(), frozenset(), True
    unique = tuple({push.key(): push for push in tally.pushes}.values())
    asked = {push.bare_in for push in unique if push.bare_in is not None and asks(push)}
    asked |= {push.via.folder for push in unique if push.via is not None}
    if len(asked) > MOST_FOLDERS:
        why = why or TOO_COMPLEX
    if not says:  # Only an alias may push: any, where it can't be read.
        unique = tuple(push for push in unique if push.alias)
        if why:
            anywhere, unknown = Git(cwd, {}, [], False), tally.config_set or tally.homed()
            unique, why = (Push(alias="*", via=anywhere, alias_unknown=unknown),), ""
    return unique, why


reading.cache_clear = read_pushes.cache_clear  # type: ignore[attr-defined]


def pushes(command: str, cwd: str = "") -> list[Push]:
    """Each `git push` in a command line, as the shell would run it (see read_line). A rule
    of text, so best effort: what a command makes up as it runs is not known here. (With the
    git commands that may run an alias, Push.alias: judged.)"""
    return [push for push in reading(command, cwd)[0] if not push.alias]


def too_complex(command: str, cwd: str = "") -> bool:
    """A line that may push, too long or nested too deep to read here in time."""
    return bool(reading(command, cwd)[1])


def once_a_stretch(
    rule: re.Pattern, command: str, start: re.Pattern = GIT_PUSH_WORDS, ends=FLOOR_ENDS
) -> bool:
    """Whether a rule of the form `<start>[^<ends>]*…` matches: tried at the first `start` of
    each stretch up to one of `ends` (where a later one matches, so does the first), so in one
    pass, not once for every place it starts."""
    stops = [found.start() for found in ends.finditer(command)]
    tried = -1
    for started in start.finditer(command):
        stretch = bisect.bisect_left(stops, started.end())
        if stretch == tried:
            continue
        tried = stretch
        if rule.match(command, started.start()):
            return True
    return False


def floor_force(command: str) -> bool:
    return once_a_stretch(FLOOR_FORCE, command)


def floor_rewrite(command: str) -> bool:
    return once_a_stretch(FLOOR_REWRITE, squashed(command), GIT_PUSH_WORDS)


def floor_default(command: str) -> bool:
    return once_a_stretch(FLOOR_DEFAULT, command)


# v0.1.42's other rules, read in one pass: the same words refused (tests/test_policy.py).
# `rm`, then flags (`-` and letters, then spaces) one of which has an `r`, then home or root,
# exactly as v0.1.42's rule read it (an `rm` inside a flag, `-rm`, counts too).
RM_START = re.compile(r"\brm\s+")
FLAG = re.compile(r"-([a-zA-Z]+)\s+")
HOME_OR_ROOT = re.compile(r"(/|~|\$HOME)/?(\s|$)")
DOWNLOAD = re.compile(r"\b(curl|wget)\b")
DOWNLOAD_PIPED = re.compile(r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(sh|bash|zsh)\b")
PIPE_ENDS = re.compile(r"[|\n]")


def recursive_delete(command: str, cwd: str = "") -> bool:
    """v0.1.42's `rm` rule (`\\brm\\s+(-[a-zA-Z]+\\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\\s+
    (-[a-zA-Z]+\\s+)*(/|~|\\$HOME)/?(\\s|$)`), in time linear in the line: the flags after an
    `rm` are read once. An `rm` within flags read already (`-rm`) is followed by the rest of
    those same flags, so says nothing they did not: it is not read again."""
    read_to = -1
    for start in RM_START.finditer(command):
        if start.end() <= read_to:
            continue
        at, recursive = start.end(), False
        while flag := FLAG.match(command, at):
            recursive = recursive or "r" in flag.group(1).lower()
            at = flag.end()
        read_to = at
        if recursive and HOME_OR_ROOT.match(command, at):
            return True
    return False


def piped_download(command: str, cwd: str = "") -> bool:
    return once_a_stretch(DOWNLOAD_PIPED, command, DOWNLOAD, PIPE_ENDS)


# A script fed to a shell: what it runs is text the line makes or holds, not read here (Ask
# first). A line that feeds one, and says push (or may go to another branch: symbolic-ref,
# update-ref, a branch renamed to main or master), is refused, saying so.
FED_SCRIPT = "a script fed to a shell can't be checked; run its git commands directly"
FED_SAYS = ("push", "symbolic-ref", "update-ref", "send-pack")
FED_SHELLS = SHELLS | {"ash", "csh", "tcsh", "mksh", "yash", "posh", "rbash", "su"}
# Commands that run the command after them, with the letters of their options that take a
# value (`timeout` takes its duration too; `xargs`: the command may be given its script).
FED_WRAPPERS = {"env": "CPSuLUa", "command": "", "exec": "a", "nice": "n", "nohup": "",
                "time": "", "timeout": "sk", "stdbuf": "ioe", "busybox": "",
                "sudo": "ugCDhprtTU", "doas": "uC", "xargs": "IJLnPsEdaRS"}  # fmt: skip
FED_LONG_VALUED = frozenset({"--chdir", "--split-string", "--unset", "--argv0", "--signal",
                             "--kill-after", "--adjustment", "--user", "--group", "--input",
                             "--output", "--error", "--rcfile", "--init-file", "--emulate",
                             "--max-args", "--max-lines", "--max-procs", "--delimiter",
                             "--arg-file", "--eof", "--max-chars"})  # fmt: skip
# A file a command may read what the line feeds it from (`/dev/stdin`, `/dev/fd/0`, `/proc/…`).
FED_DEVICE = re.compile(r"/dev/(?!null$)|/proc/")
# A process substitution's file (`<(…)`, zsh's `=(…)`), as a word (none a line it is put in,
# for `eval` to run, reads otherwise: a zsh 200k run).
FED_PROCESS = "\x01PROCESS"
FED_WORD = re.compile(r"[^\s;&|()]+|[;&|()]+")
FED_SEGMENTS = re.compile(r"[;&|\n()`]+")


def fed_says(command: str) -> bool:
    """Whether a line says push, symbolic-ref or update-ref (however quoted), or renames a
    branch to main or master (`branch -M main`; the words after `branch`, a few)."""
    text = squashed(command)
    if any(word in text for word in FED_SAYS):
        return True
    words = FED_WORD.findall(text) if "branch" in text else []
    for at, word in enumerate(words):
        if word == "branch":
            after = list(itertools.takewhile(lambda w: w[0] not in ";&|()", words[at + 1 : at + 8]))
            moves = any(
                w == "--move" or (w[:1] == "-" and w[1:2] != "-" and "m" in w) for w in after
            )
            if moves and any(w in {"main", "master"} for w in after):
                return True
    return False


def unwrapped(words: list[str]) -> tuple[list[str], bool]:
    """A simple command's words from the command it runs: past reserved words, assignments,
    and the commands that run the one after them (`env`, `nice`, `timeout 60`, `command`,
    `xargs`…) with their options; and whether xargs runs it (with words of its input). Past
    MOST_NESTED of env's `-S` (each puts words of its own first): taken for a shell."""
    rest, xargs, splits = collections.deque(words), False, 0
    while rest and (rest[0] in KEYWORDS or isinstance(rest[0], Assignment)):
        rest.popleft()
    while rest and (name := rest[0].rsplit("/", 1)[-1]) in FED_WRAPPERS:
        valued = FED_WRAPPERS[name]
        rest.popleft()
        while rest and rest[0][:1] == "-" and rest[0] != "-":
            option, split = rest.popleft(), None
            if option == "--":
                break
            if option.startswith("--"):
                if option in FED_LONG_VALUED and rest:
                    value = rest.popleft()
                    split = value if option == "--split-string" else None
                elif option.startswith("--split-string="):
                    split = option.partition("=")[2]
            else:
                for index, letter in enumerate(option[1:], 1):
                    if letter in valued:
                        value = option[index + 1 :] or (rest.popleft() if rest else "")
                        split = value if name == "env" and letter == "S" else None
                        break
            if split is not None:
                splits += 1
                if splits > MOST_NESTED:
                    return ["sh"], xargs
                rest.extendleft(reversed(split.split()))
        while rest and (isinstance(rest[0], Assignment) or ASSIGNMENT.match(rest[0])):
            rest.popleft()  # (`env A=b cmd`, `sudo A=b cmd`.)
        if name == "timeout" and rest:
            rest.popleft()  # Its duration.
        xargs = xargs or name == "xargs"
    return list(rest), xargs


def code_of(words: list[str], fed: bool, xargs: bool) -> tuple[str, str | None]:
    """Where a simple command (unwrapped) takes the code it runs: "fed" (a file the line
    feeds: `bash <(…)`, `source /dev/stdin`), "stdin" (a shell given no script: its input;
    any run by xargs, which may give it its script), or "" (none, or a file); and the code
    eval or trap runs (a shell's `-c` script: inner_scripts). A command whose name the line
    makes up (`$SHELL`) is taken for a shell where its own input is fed (`fed`: a pipe, a
    heredoc)."""
    if not words:
        return "", None
    name = words[0].rsplit("/", 1)[-1]
    name = name[1:] if name.startswith("=") and len(name) > 1 else name  # (zsh's `=bash`.)
    if name in {".", "source"}:
        operand = next((word for word in words[1:] if word != "--"), "")
        return ("fed" if operand == FED_PROCESS or FED_DEVICE.match(operand) else ""), None
    if name == "eval":
        return "", " ".join(words[1:])
    if name == "trap":  # (`trap 'code' EXIT`: run by the shell, later.)
        given = words[2:] if words[1:2] == ["--"] else words[1:]
        return "", (given[0] if given and given[0][:1] != "-" else None)
    if name not in FED_SHELLS and not (fed and UNSETTLED.search(words[0])):
        return "", None
    stdin, at = xargs, 1
    while at < len(words):
        word, at = words[at], at + 1
        if word in {"--", "-"}:
            break
        if word.startswith("--"):
            if word.startswith("--command"):
                return ("stdin" if xargs else ""), None
            at += word in FED_LONG_VALUED
            continue
        if word[:1] in {"-", "+"} and len(word) > 1:
            letters = word[1:]
            if word[0] == "-" and "c" in letters:
                script = next((later for later in words[at:] if later[:1] not in {"-", "+"}), None)
                return ("stdin" if xargs or (name == "su" and script is None) else ""), None
            stdin = stdin or (word[0] == "-" and "s" in letters)
            at += sum(letter in "oO" for letter in letters)  # (`-o pipefail`: its value.)
            continue
        at -= 1
        break
    operand = words[at] if at < len(words) else None
    if operand is None or stdin or name == "su":  # (su's words: a user's name, not a file.)
        return "stdin", None
    if operand == FED_PROCESS or FED_DEVICE.match(operand):
        return "fed", None
    return "", None


def inner_scripts(words: list[str]) -> list[str]:
    """The scripts a simple command's words give a shell, wherever its name stands in them
    (`sh -c '…'`, `find … -exec bash -c '…'`, zsh's `emulate sh -c '…'`, `su -c '…'`), and
    what `env -S` is given: in one pass."""
    scripts: list[str] = []
    shell = env = script_next = skip = False
    for word in words:
        if skip:
            skip = False
        elif script_next:
            if word[:1] not in {"-", "+"} or env:
                scripts.append(word)
                script_next = env = shell = False
        elif env and word.startswith("--split-string"):
            value = word.partition("=")[2]
            scripts += [value] if value else []
            script_next = not value
        elif env and word[:1] == "-" and word[1:2] != "-" and "S" in word:
            value = word[word.index("S") + 1 :]
            scripts += [value] if value else []
            script_next = not value
        elif env and (word[:1] == "-" or ASSIGNMENT.match(word)):
            pass  # (env's own options and assignments.)
        elif shell and word[:1] in {"-", "+"} and not word.startswith(("--", "++")):
            script_next = word[0] == "-" and "c" in word[1:]
            skip = not script_next and ("o" in word[1:] or "O" in word[1:])  # (`-o pipefail`.)
        elif shell and word.startswith("--"):
            pass
        else:
            name = word.rsplit("/", 1)[-1].lstrip("=")
            shell, env = name in FED_SHELLS or name == "emulate", name == "env"
    return scripts


def feeds_shell(command: str, depth: int = 0, given: bool = False) -> bool:
    """Whether a line feeds a shell code it makes or holds: on its input (a pipe, a heredoc,
    a here-string, a process substitution; `given`: the input of the line that runs this
    one), or as a file (`bash <(…)`, `source /dev/stdin`); in the lines it runs too (`sh -c`,
    `eval`, substitutions). Where it does not parse: a shell or `source` named at all."""
    if depth > MOST_NESTED:
        return True
    unheredoc, bodies = heredocs(command)
    plain, inside = without_substitutions(without_comments(unheredoc).replace("\\\n", ""))
    for body, quoted in bodies:
        if not quoted:
            inside += without_substitutions(body, quoted=True)[1]
    try:
        tokens = shell_words(plain)
    except ValueError:
        return any(
            (words := segment.split())
            and (
                words[0] in {".", "source"}
                or any(w.rsplit("/", 1)[-1] in FED_SHELLS for w in words)
            )
            for segment in FED_SEGMENTS.split(plain)
        )
    except TooComplexError:
        return True
    given = given or any(
        (isinstance(token, Separator) and "|" in token.replace("||", ""))
        or (
            isinstance(token, Redirecting)
            and ("<<" in token[token.at :] or "<&" in token[token.at :])
        )
        for token in tokens
    )
    # Each simple command: its words, and whether its own input is fed (a pipe, a heredoc).
    commands: list[tuple[list[str], bool]] = []
    current: list[str] = []
    piped = False
    for token in [*tokens, Separator(";")]:
        if not isinstance(token, Separator):
            current.append(token)
            continue
        last = current[-1] if current else ""
        operator = last[last.at :] if isinstance(last, Redirecting) else ""
        inline = piped or any(
            isinstance(word, Redirecting) and "<<" in word[word.at :] for word in current
        )
        if token.startswith("(") and (operator == "<" or last == "="):  # `<(…)`, zsh's `=(…)`
            before = current[-2] if len(current) > 1 and last in {"<", "="} else ""
            if isinstance(before, Redirecting) and not before.strip("<>&0123456789"):
                given = given or "<" in before  # `< <(…)`: its input, what the line makes.
                commands.append((redirection_free(current[:-2]), True))
            else:
                prefix = last[: last.at] if isinstance(last, Redirecting) else ""
                words = [*redirection_free(current[:-1]), *([prefix] if prefix else [])]
                commands.append(([*words, FED_PROCESS], inline))
        elif current:
            commands.append((redirection_free(current), inline))
        current = []
        piped = "|" in token.replace("||", "")
    readers = False
    for words, inline in commands:
        rest, xargs = unwrapped(words)
        kind, script = code_of(rest, inline, xargs)
        if kind == "fed":
            return True
        readers = readers or kind == "stdin"
        for text in [*([] if script is None else [script]), *inner_scripts(words)]:
            if feeds_shell(text, depth + 1, given):
                return True
    return (readers and given) or any(feeds_shell(body, depth + 1, given) for body in inside)


def fed_script(command: str, cwd: str = "") -> bool:
    """A line that feeds a shell a script (feeds_shell) and says push or may go to another
    branch (fed_says): what the script does is not known here."""
    return fed_line(command)


@functools.lru_cache(maxsize=32)
def fed_line(command: str) -> bool:
    """fed_script, once a line (pushes_unknown may ask it first)."""
    return fed_says(command) and feeds_shell(command)


# What of a repository's config says where a push of its current branch goes (`@{push}`).
PUSH_SETTINGS_READ = re.compile(
    r"(?i)^(?:push\.default|remote\.pushdefault|branch\.(?P<branch>.+)\.(?:remote|merge|"
    r"pushremote)|remote\.(?P<remote>.+)\.(?:url|pushurl|push))$"
)


def git_folders(folder: str) -> tuple[Path, Path] | None:
    """The git folder of the checkout a folder is in, as git finds it (a `.git` folder, or a
    `gitdir:` file, above or at it), and its common folder (a linked worktree's `commondir`):
    read as files, no git run there. None when there is none, or it can't be read so."""
    from .settings_guard import read_regular  # (settings_guard imports this module.)

    here = Path(os.path.realpath(folder))
    for place in (here, *here.parents):
        dot = place / ".git"
        try:
            if dot.is_dir():
                own = Path(os.path.realpath(dot))
            elif dot.is_file():
                named = read_regular(dot, 4096, follow=True).decode(errors="replace").strip()
                if not named.startswith("gitdir:"):
                    return None
                own = Path(os.path.realpath(place / named.removeprefix("gitdir:").strip()))
            elif os.path.lexists(dot):
                return None
            else:
                continue
            try:
                common = read_regular(own / "commondir", 4096).decode(errors="replace").strip()
            except FileNotFoundError:
                return own, own
            return own, Path(os.path.realpath(own / common))
        except (OSError, ValueError):
            return None
    return None


def config_read(file: Path, depth: int = 0) -> list[tuple[str, str]] | None:
    """A git config file's settings, read as a file, with what it includes (`include.path`, as
    git finds it: relative to the file, or from home), each read so too; None when one can't be
    read so, or holds a conditional include (`includeIf`: not worked out here)."""
    from . import privategit

    entries = privategit.config_entries(file)
    if entries is None or depth > 8:
        return None
    found: list[tuple[str, str]] = []
    for key, value in entries:
        if key.lower().startswith("includeif."):
            return None
        if key.lower() != "include.path":
            found.append((key, value))
            continue
        named = Path(value).expanduser() if value.startswith("~") else file.parent / value
        if (more := config_read(named, depth + 1)) is None:
            return None
        found += more
    return found


def current_branches(folder: str) -> set[str]:
    """The branch a folder's checkout is on and the remote branch a bare push there updates
    (`@{push}`); none when that can't be told. Never by git in that folder, whose repository a
    worker may have written: its HEAD and config are read here as files (none at all for a
    config that includes another), and git works out `@{push}` from what of that config says it
    (with the owner's own config), in a folder of Chief of Staff's own. A config with a
    conditional include (`includeIf`) is not worked out: none."""
    from . import privategit  # (It imports settings_guard, which imports this module.)
    from .settings_guard import read_regular

    try:
        found = git_folders(folder)
    except (OSError, ValueError):  # (A folder with no name the file system takes.)
        return set()
    if not found:
        return set()
    own, common = found
    try:
        head = read_regular(own / "HEAD", 4096).decode(errors="replace").strip()
    except OSError:
        return set()
    if not head.startswith("ref: refs/heads/") or not privategit.ref_name(head[5:].strip()):
        return set()  # Detached (or a reftable's: HEAD says nothing).
    branch = head.removeprefix("ref: refs/heads/").strip()
    entries = config_read(common / "config")
    if entries is None:
        return set()
    if any(
        key.lower() == "extensions.worktreeconfig" and value.lower() in {"true", "yes", "on", "1"}
        for key, value in entries
    ):
        more = config_read(own / "config.worktree")
        if more is None:
            return set()
        entries = [*entries, *more]
    kept = []
    remotes = {"origin"}
    for key, value in entries:
        if not (found_key := PUSH_SETTINGS_READ.match(key)):
            continue
        named = found_key.group("branch")
        if named is not None and named != branch:
            continue
        kept.append((key, value))
        if key.lower().endswith((".remote", ".pushremote", "remote.pushdefault")):
            remotes.add(value)
    if any("/" in remote or not remote for remote in remotes):
        return set()
    kept += [(f"remote.{remote}.fetch", f"+refs/heads/*:refs/remotes/{remote}/*")
             for remote in sorted(remotes)]  # fmt: skip
    asked = privategit.root()
    asked.mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch = Path(tempfile.mkdtemp(prefix="push-", dir=asked))
    try:
        (scratch / "objects").mkdir()
        (scratch / "refs" / "heads").mkdir(parents=True)
        (scratch / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        (scratch / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = true\n")
        ref = scratch / "refs" / "heads" / branch
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text("1" * 40 + "\n")
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("GIT_", "CHIEFOFSTAFF_"))
                       or key in {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"}}  # fmt: skip
        environment |= {"GIT_DIR": str(scratch), "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_COUNT": str(len(kept)), "GIT_ALLOW_PROTOCOL": "",
                        "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1",
                        "GIT_TERMINAL_PROMPT": "0"}  # fmt: skip
        for at, (key, value) in enumerate(kept):
            environment[f"GIT_CONFIG_KEY_{at}"] = key
            environment[f"GIT_CONFIG_VALUE_{at}"] = value
        done = subprocess.run(  # noqa: S603  (git, in a folder of Chief of Staff's own)
            ["git", "-c", "core.fsmonitor=false", "for-each-ref",  # noqa: S607
             "--format=%(push)", f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=10, check=False, cwd=scratch,
            stdin=subprocess.DEVNULL, env=environment,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        return {branch}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    pushed = done.stdout.strip() if done.returncode == 0 else ""
    for remote in remotes:
        if pushed.startswith(f"refs/remotes/{remote}/"):
            return {branch, pushed.removeprefix(f"refs/remotes/{remote}/")}
    return {branch}


def git_output(arguments: list[str], most: int) -> tuple[int, bytes] | None:
    """What git says with these arguments (its exit status and what it writes), within 10
    seconds and `most` bytes; None when it can't be run or read, or takes or says more."""
    try:
        process = subprocess.Popen(  # noqa: S603  (git, with arguments of its own)
            ["git", *arguments],  # noqa: S607
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    chunks: list[bytes] = []
    ended: list[bool] = []  # Whether it was read to its end, within `most` bytes.
    stream = process.stdout

    def read():
        size = 0
        try:
            while stream and size <= most and (chunk := os.read(stream.fileno(), 65536)):
                chunks.append(chunk)
                size += len(chunk)
        except (OSError, ValueError):
            return
        ended.append(size <= most)

    reader = threading.Thread(target=read, daemon=True)
    try:
        reader.start()
        reader.join(10)
    except RuntimeError:  # (No thread to read it with.)
        pass
    if not ended:
        process.kill()
    try:
        code = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return None
    finally:
        if reader.is_alive():
            reader.join(1)
    if stream and not reader.is_alive():
        stream.close()
    return (code, b"".join(chunks)) if ended == [True] else None


# The keys of git's configuration that say where a push goes from a folder, and what a command
# there runs (an alias), and the files it includes (Pushing); at most so many bytes of them (past
# those: not known).
PUSH_SETTINGS = (
    r"^(push\.default|remote\..+\.(push|mirror)|branch\..+\.merge|alias\..+|include\.path"
    r"|includeif\..+\.path)$"
)
MOST_SETTINGS = 1 << 20


def config_entries(folder: str) -> list[tuple[str, str | None]] | None:
    """What git's configuration for a folder (the system's, the user's, the repository's, as
    git reads them) says of where pushes go, and of its aliases and the files it includes: its
    keys (in git's own case) and values (None for a key with none); None when git can't say."""
    found = git_output(
        ["-C", folder, "config", "--null", "--get-regexp", PUSH_SETTINGS], MOST_SETTINGS
    )
    if found is None or found[0] not in {0, 1} or (found[0] == 1 and found[1]):
        return None
    entries: list[tuple[str, str | None]] = []
    for entry in found[1].split(b"\0")[:-1]:
        key, has, value = entry.decode(errors="surrogateescape").partition("\n")
        entries.append((key, value if has else None))
    return entries


@dataclass
class Pushing:
    """What git's configuration says of where a push goes from a folder (config_entries):
    every branch of the same name on both sides for a push naming no refspec (`matching`, a
    mirror remote, a `:` push refspec); push.default; the branches remotes' push refspecs
    update; each branch's upstream (`branch.<name>.merge`, where `upstream` pushes it); an
    include that depends on the branch (`includeIf.onbranch:…`); or not known. Its aliases (by
    name, in lower case), those that may push (`pushers`: one whose text says push, send-pack
    or subtree, names another alias, or a shell's; `shell_pushers`: one whose text says so),
    those that switch branches or set configuration (`switchers`: EXPANDED), the names of the
    files it includes (without their folders, in lower case); and whether git could not be
    asked (`unread`)."""

    every: bool = False
    mode: str = "simple"
    refspecs: set[str] = field(default_factory=set)
    upstreams: dict[str, set[str]] = field(default_factory=dict)
    onbranch: bool = False
    unknown: bool = False
    aliases: dict[str, str | None] = field(default_factory=dict)
    pushers: set[str] = field(default_factory=set)
    shell_pushers: set[str] = field(default_factory=set)
    switchers: set[str] = field(default_factory=set)
    includes: set[str] = field(default_factory=set)
    unread: bool = False


def pushing_of(entries: list[tuple[str, str | None]] | None) -> Pushing:
    """Pushing, read from a folder's configuration (the last push.default counts)."""
    pushing = Pushing(unknown=entries is None, unread=entries is None)
    for key, value in entries or []:
        lower = key.lower()
        if lower.startswith("alias."):
            pushing.aliases[lower[6:]] = value
        elif lower == "include.path" or (
            lower.startswith("includeif.") and lower.endswith(".path")
        ):
            name = (value or "").lower().rstrip("/").rsplit("/", 1)[-1]
            pushing.includes.add(name or "*")
            pushing.onbranch = pushing.onbranch or lower.startswith("includeif.onbranch:")
        elif lower == "push.default":
            pushing.mode = value if value in MODES else "?"
        elif lower.startswith("remote.") and lower.endswith(".mirror"):
            truth = "true" if value is None else value.lower()
            pushing.every = pushing.every or truth in TRUE
            pushing.unknown = pushing.unknown or truth not in TRUE | FALSE
        elif lower.startswith("remote.") and lower.endswith(".push"):
            configured_refspec(pushing, value)
        elif lower.startswith("branch.") and lower.endswith(".merge"):
            pushing.upstreams.setdefault(key[7:-6], set()).add(pushed_branch(value or ""))
    for name, value in pushing.aliases.items():
        text = "" if value is None else value.lower()
        if any(word in text for word in PUSHING) or any(
            word in pushing.aliases and word != name for word in ALIAS_WORDS.findall(text)
        ):
            pushing.pushers.add(name)
            pushing.shell_pushers.add(name)
        if value is None or text.startswith("!"):
            pushing.pushers.add(name)
        shell = text.startswith("!")
        if any(word in text for word in EXPANDED) if shell else alias_switches_to(value or ""):
            pushing.switchers.add(name)
    pushing.unknown = (
        pushing.unknown or pushing.mode == "?" or len(pushing.refspecs) > MOST_BRANCHES
    )
    pushing.every = pushing.every or pushing.mode == "matching"
    return pushing


def configured_refspec(pushing: Pushing, value: str | None):
    """What a remote's push refspec (`remote.<name>.push`) updates, into `pushing`: its
    destination (or its source, without one), as a branch; every branch for `:` and a glob
    that may name the default branch; nothing for a tag, a ref not a branch's, or one it leaves
    out (`^…`); not known for one git can't take."""
    spec = (value or "").lstrip("+")
    if value is None or not spec or any(char.isspace() or ord(char) < 32 for char in spec):
        pushing.unknown = True
        return
    if spec.startswith("^"):
        return
    if spec == ":":
        pushing.every = True
        return
    target = spec.rpartition(":")[2] if ":" in spec else spec
    if not target:
        pushing.unknown = True
    elif "*" in target:
        pattern = pushed_branch(target)
        pushing.every = pushing.every or any(
            fnmatch.fnmatchcase(name, pattern) for name in DEFAULT_BRANCHES
        )
    elif not target.startswith("refs/") or target.startswith("refs/heads/"):
        pushing.refspecs.add(pushed_branch(target))


def local_branches(folder: str, names: list[str]) -> set[str] | None:
    """Which of these names (BRANCH_NAME's) are branches of a folder's repository, asked of
    git; None when it can't say."""
    most = sum(len(name) + 12 for name in names)
    found = git_output(
        ["-C", folder, "branch", "--list", "--format=%(refname)", "--", *names], most
    )
    if found is None or found[0] != 0:
        return None
    listed = found[1].decode(errors="surrogateescape").splitlines()
    return {line.removeprefix("refs/heads/") for line in listed} & set(names)


# What git said of each folder's branch, during one check (shell_denial): asked once a folder;
# and of its configuration (SETTINGS), and of the names checkouts there went to (CHECKED).
ANSWERS: contextvars.ContextVar[dict[str, set[str]] | None] = contextvars.ContextVar(
    "ANSWERS", default=None
)
SETTINGS: contextvars.ContextVar[dict[str, Pushing] | None] = contextvars.ContextVar(
    "SETTINGS", default=None
)
CHECKED: contextvars.ContextVar[dict[str, set[str]] | None] = contextvars.ContextVar(
    "CHECKED", default=None
)


def branches_of(folder: str) -> set[str]:
    """current_branches, asked once a folder during a check; none for a folder the line makes
    up (a variable's, a substitution's: not asked)."""
    if UNSETTLED.search(folder):
        return set()
    answers = ANSWERS.get()
    if answers is None:
        return current_branches(folder)
    if folder not in answers:
        answers[folder] = current_branches(folder)
    return answers[folder]


def settings_of(folder: str) -> Pushing:
    """Pushing of a folder's configuration, asked once a folder during a check."""
    settings = SETTINGS.get()
    if settings is None:
        return pushing_of(config_entries(folder))
    if folder not in settings:
        settings[folder] = pushing_of(config_entries(folder))
    return settings[folder]


def checked_branches(found: list[Push]) -> dict[str, set[str]]:
    """For each folder where a bare push follows a checkout of a name (Push.bare_check), which
    of those names are branches git has there: asked once a folder during a check, at most
    MOST_BRANCHES names (past those, and for one that is also a file or a folder there, a
    checkout may not have switched: not taken for one)."""
    checked = CHECKED.get()
    checked = {} if checked is None else checked
    wanted: dict[str, set[str]] = {}
    for push in found:
        if push.bare_check and push.bare_in is not None and push.bare_in not in checked:
            wanted.setdefault(push.bare_in, set()).add(push.bare_new)
    for folder, names in wanted.items():
        usable = sorted(
            name for name in names if BRANCH_NAME.fullmatch(name) and not lexists(folder, name)
        )[:MOST_BRANCHES]
        found_names = local_branches(folder, usable) if usable else set()
        checked[folder] = found_names or set()
    return checked


def lexists(folder: str, name: str) -> bool:
    """Whether a file or a folder (or a link) of that name is there; taken so when it can't be
    told."""
    try:
        return os.path.lexists(Path(folder, name))
    except (OSError, ValueError):
        return True


def alias_settings(folder: str) -> Pushing:
    """settings_of the folder a git that may run an alias runs in; of one not there (that the
    line makes), its nearest that is (git's configuration there, but a new repository's own)."""
    for _ in range(MOST_PATH):
        try:
            if not folder or Path(folder).is_dir():
                break
        except (OSError, ValueError):
            break
        parent = str(Path(folder).parent)
        if parent == folder:
            break
        folder = parent
    return settings_of(folder)


def includes_written(settings: Pushing, command: str, cwd: str) -> bool:
    """Whether a line may write a file git's configuration includes (by its name: written)."""
    if not settings.includes:
        return False
    names = written(command, cwd)[1]
    return "*" in names or "*" in settings.includes or bool(settings.includes & names)


def through_alias(candidate: Push, command: str, cwd: str) -> Push | None:
    """The push an alias makes (Push.alias), as git's configuration where it runs says (the
    line's own folder's, for one not known): followed from one alias to the next, git's own
    command at the end read as push_of reads it (a bare push placed as `candidate` is); a
    shell's (`!…`) not known when its text or its words say push, send-pack, subtree or an
    alias. None when it runs no push; not known when the configuration can't be read or the
    line may write it (written, includes_written)."""
    git = candidate.via
    if git is None:
        return None
    lost = bool(UNSETTLED.search(git.folder))
    settings = alias_settings(cwd if lost else git.folder)
    unknown = Push(unknown=True, nested=candidate.nested, anonymous=candidate.alias == "$")
    if candidate.alias_unknown or settings.unread or includes_written(settings, command, cwd):
        return unknown
    if candidate.alias in {"*", "?"}:  # Any alias: one that may push, there.
        return unknown if settings.pushers else None
    if candidate.alias == "$":  # A command not known: a git that runs an alias of the line's?
        given = git.rest[1:]
        if settings.pushers & said_words(command) or (
            settings.pushers and any(UNSETTLED.search(word) for word in given)
        ):
            return unknown
        return None
    kind, git = followed(git, settings)
    if kind == "none":
        return None
    if kind == "unknown":
        return unknown
    if kind == "shell":
        name = git.rest[0].lower()
        text = " ".join(git.rest[1:]).lower()
        named = ALIAS_WORDS.findall(text)
        if (
            name in settings.shell_pushers
            or f"alias.{name}" in git.config
            or UNSETTLED.search(text)
            or any(word in text for word in PUSHING)
            or any(word in settings.aliases or f"alias.{word}" in git.config for word in named)
        ):
            return unknown
        return None
    push = push_of(git)
    if push is None:
        return None
    push.nested = candidate.nested
    if push.bare_in is not None:
        push.bare_new, push.bare_also = candidate.bare_new, push.bare_also | candidate.bare_also
        push.bare_check, push.alt_new = candidate.bare_check, candidate.alt_new
        push.alt_also, push.alt_unknown = candidate.alt_also, candidate.alt_unknown
        push.upstreams_set = candidate.upstreams_set
        push.unknown = push.unknown or candidate.unknown
    return push


def followed(git: Git, settings: Pushing) -> tuple[str, Git]:
    """What a git command runs, followed from one alias to the next (`-c`'s, then those of the
    folder's configuration, `settings`): ("git", the git command of git's own at the end, its
    words the aliases' and then the line's), ("shell", the git command of a shell's alias:
    `!…`), ("none", …) for a name no alias has (a program of that name, or none), ("unknown",
    …) for one not read here."""
    for _ in range(MOST_ALIASES):
        name = git.rest[0]
        if name in UNALIASED or UNSETTLED.search(name):
            return "git", git
        key = f"alias.{name.lower()}"
        if key in git.config:
            value = git.config[key]
        elif name.lower() in settings.aliases:
            value = settings.aliases[name.lower()]
        else:
            return "none", git  # No alias: git runs a program of that name, or none.
        if value is None or len(value) > MOST_VALUE:
            return "unknown", git  # (Its text: not read here.)
        if value.startswith("!"):
            return "shell", git
        words = alias_words(value)
        if words is None:
            return "unknown", git
        following = git_command(["git", *words, *git.rest[1:]], {}, git.folder) if words else None
        if following is None:
            return "none", git
        if following.folder != git.folder or following.elsewhere:
            return "unknown", git  # (git runs no such alias; not followed here.)
        following.config = {**git.config, **following.config}
        following.elsewhere, following.moved, following.zsh = git.elsewhere, git.moved, git.zsh
        following.epoch, following.currents = git.epoch, git.currents
        git = following
    return "unknown", git


# The git commands an alias may run that change where a later push goes, or what it pushes
# (read as the line's own: alias_switches); a shell's alias that says one may.
EXPANDED = frozenset({"checkout", "switch", "branch", "symbolic-ref", "update-ref", "config",
                      "worktree"})  # fmt: skip


def alias_switches(found: tuple[Push, ...], cwd: str) -> dict[tuple[str, str], tuple | None]:
    """For each git command of a line that runs an alias of its folder's configuration (Push
    .alias) that switches branches or sets configuration (EXPANDED), by its folder and name:
    the words of git's own command it runs (the line's words after them), or None when it may
    and which is not known (a shell's alias that says one; one not read)."""
    switches: dict[tuple[str, str], tuple | None] = {}
    for candidate in found:
        git = candidate.via
        if git is None or not ALIAS_NAME.fullmatch(candidate.alias):
            continue
        lost = bool(UNSETTLED.search(git.folder))
        settings = alias_settings(cwd if lost else git.folder)
        if candidate.alias_unknown or settings.unread:
            words: tuple | None = None
        else:
            kind, final = followed(git, settings)
            if kind == "none" or (kind == "git" and final.rest[0] not in EXPANDED):
                continue
            if kind == "shell":
                name = final.rest[0].lower()
                value = final.config.get(f"alias.{name}") or settings.aliases.get(name) or ""
                text = " ".join([value, *final.rest[1:]]).lower()
                if not any(word in text for word in EXPANDED) and not UNSETTLED.search(text):
                    continue
            given = len(git.rest) - 1
            words = tuple(final.rest[: len(final.rest) - given]) if kind == "git" else None
        key = (git.folder, candidate.alias)
        switches[key] = words if switches.get(key, words) == words else None
    return switches


@functools.lru_cache(maxsize=32)
def said_words(command: str) -> frozenset[str]:
    """The words of a line that may name an alias (squashed)."""
    return frozenset(ALIAS_WORDS.findall(squashed(command)))


@functools.lru_cache(maxsize=256)
def alias_words(text: str) -> tuple[str, ...] | None:
    """An alias's words, as git splits them (quotes as the shell's); None when it can't."""
    try:
        return tuple(shlex.split(text))
    except ValueError:
        return None


def judged(command: str, cwd: str) -> list[Push]:
    """Each push of a line (pushes), an alias's as through_alias makes it: what is judged. A
    line that runs an alias that switches branches (or sets configuration) is read again,
    that alias read as what it runs (alias_switches)."""
    pushes_read = reading(command, cwd)[0]
    if switches := alias_switches(pushes_read, cwd):
        pushes_read, why = read_pushes(command, cwd, SHELL.get(), frozenset(switches.items()))
        if why:
            return [Push(unknown=True)]
    found = []
    for push in pushes_read:
        if not push.alias:
            found.append(push)
        elif (made := through_alias(push, command, cwd)) is not None:
            found.append(made)
    return found


def asks(push: Push) -> bool:
    """Whether judging a bare push asks git of its folder (its branch, its configuration, the
    branch a checkout went to)."""
    return not push.bare_new or push.bare_check or push.configured or push.mapped


def bare_reading(
    push: Push, checked: dict[str, set[str]], command: str = "", cwd: str = ""
) -> tuple[set[str] | None, bool, bool]:
    """Where a bare push may go: the branches it may update (None: every branch of the same name
    on both sides), whether that is not known (no branch checked out, no repository, a folder
    made up, configuration git can't read: git itself would push nothing there, or the line is
    not read right), and whether it may be several only because of a switch before it (then it
    is not known which). Its branch is the one the line switched to (a checkout's only if git
    has it; else as the line read before 0.1.46: alt_*), or its folder's (asked of git); and,
    pushing no refspec or the checkout's branch by name, where git's configuration sends that
    (not known when the line `command` may write a file it includes)."""
    if push.bare_in is None:
        return set(), False, False
    new, also, unknown = push.bare_new, push.bare_also, False
    if push.bare_check and new not in checked.get(push.bare_in, set()):
        new, also, unknown = push.alt_new, also | push.alt_also, push.alt_unknown
    base = {new} if new else branches_of(push.bare_in)
    if unknown or not base:
        return base | also, True, False
    if not (push.configured or push.mapped):
        return base | also, False, bool(also - base)
    settings = settings_of(push.bare_in)
    mode = push.mode or settings.mode
    upstream = mode in {"upstream", "tracking"}
    switched = bool(new or also)
    if settings.unknown or (settings.onbranch and switched) or (upstream and push.upstreams_set):
        return base | also, True, False
    if includes_written(settings, command, cwd):
        return base | also, True, False
    if push.configured and settings.every:
        return None, False, False

    def sent(names: set[str] | frozenset[str]) -> set[str]:
        found = set(names) | settings.refspecs
        for name in names if upstream else ():
            found |= settings.upstreams.get(name, set())
        return found

    definite = sent(base)
    extra = sent(also) - definite
    return definite | extra, False, bool(extra)


def pushes_default(command: str, cwd: str = "") -> bool:
    """Whether a command line may push to the default branch, as far as its words say (and,
    for a push that names no branch, the branch its folder is on and git's configuration
    there: asked of git once a folder); never less than v0.1.42's rule said."""
    if floor_default(command) or too_complex(command, cwd):
        return True  # (Too complex: refused for that, with nothing asked of git.)
    found = judged(command, cwd)
    checked = checked_branches(found)
    for push in found:
        if push.branches is None or push.branches & DEFAULT_BRANCHES:
            return True
        if push.made_up and MADE_UP_DEFAULT.search(command):
            return True
        if not push.unknown:
            branches, unknown, several = bare_reading(push, checked, command, cwd)
            if branches is None:
                return True
            if not unknown and not several and branches & DEFAULT_BRANCHES:
                return True
    return False


def pushes_unknown(command: str, cwd: str = "") -> bool:
    """Whether a push's branch cannot be read from the line (see Push.unknown), or a bare
    one's from git (see bare_reading), or may be the default branch only because of a switch
    before it; or the line runs a command whose name it makes up (Push.anonymous), unless it
    feeds that a script (fed_script says so then)."""
    if fed_alias(command, cwd):
        return True
    found = judged(command, cwd)
    checked = checked_branches(found)
    anonymous = False
    for push in found:
        anonymous = anonymous or push.anonymous
        if push.unknown and not push.anonymous:
            return True
        branches, unknown, several = bare_reading(push, checked, command, cwd)
        if unknown or (several and branches is not None and branches & DEFAULT_BRANCHES):
            return True
    return anonymous and not fed_script(command, cwd)


def fed_alias(command: str, cwd: str) -> bool:
    """Whether a line feeds a shell a script (feeds_shell) that may run an alias of git's
    configuration that pushes: one a word of the line names (the folder's configuration, read
    with where pushes go; one that can't be read, any where the line says git), or one that
    switches branches, on a line that says push. What the script pushes: not known."""
    words = said_words(command)
    if not words or not fed_shell(command):
        return False
    settings = alias_settings(cwd)
    if settings.unread:
        return "git" in words
    pushers = settings.shell_pushers | {name for name, value in settings.aliases.items()
                                        if value is None}  # fmt: skip
    return bool(words & pushers) or bool(words & settings.switchers and "push" in words)


@functools.lru_cache(maxsize=32)
def fed_shell(command: str) -> bool:
    """feeds_shell, once a line; at once for one that names no shell, `source`, `.`, `eval` or
    `trap`, and makes up no command's name (`$SHELL`) or substitution: none it feeds (and one
    too long or deep to read is refused for that)."""
    text = squashed(command)
    named = any(word.rsplit("/", 1)[-1].lstrip("=") in FED_NAMES for word in FED_SPLIT.split(text))
    return (named or "$" in text or "`" in text) and feeds_shell(command)


# The words that may feed a shell code (fed_shell), as a line splits them.
FED_NAMES = FED_SHELLS | {"source", ".", "eval", "trap", "emulate", "busybox"}
FED_SPLIT = re.compile(r"[\s;&|()<>{}=]+")


def pushes_forced(command: str, cwd: str = "") -> bool:
    return floor_force(command) or any(push.forced for push in judged(command, cwd))


def matches(pattern: str) -> Callable[[str, str], bool]:
    found = re.compile(pattern)
    return lambda command, cwd="": found.search(command) is not None


# Commands a worker may not run on its own; it must ask the owner through the Router. Each
# check is given the command line and the folder it runs in.
DANGEROUS: list[tuple[Callable[[str, str], bool], str]] = [
    (matches(r"(^|[\s;&|(])sudo\s"), "sudo is not allowed"),
    (recursive_delete, "recursive delete of home or root"),
    (too_complex, TOO_COMPLEX),
    (pushes_forced, "force push"),
    (pushes_default, "push to the default branch"),
    (pushes_unknown, UNREADABLE_PUSH),
    (fed_script, FED_SCRIPT),
    (
        matches(r"\bgh\s+pr\s+(merge|close|review|comment)\b"),
        "merging, closing or commenting on PRs as the owner",
    ),
    (matches(r"\bgh\s+issue\s+(close|comment|delete|edit)\b"), "changing issues as the owner"),
    (piped_download, "piping a download into a shell"),
    (matches(r"\b(mkfs|diskutil\s+erase|shutdown|reboot)\b"), "system-level destructive command"),
]


GH_API = re.compile(r"\bgh\s+api\b([^;&|\n]*)")


GH_METHOD = re.compile(r"(?:^|\s)(?:-X|--method)[\s=]*([A-Za-z]+)")


GH_FIELDS = re.compile(r"(?:^|\s)(?:-f|-F|--field|--raw-field|--input)(?:[\s=]|$)")


def gh_api_writes(command: str) -> bool:
    """gh api defaults to POST as soon as fields or an input body are given."""
    for match in GH_API.finditer(command):
        arguments = match.group(1)
        if method := GH_METHOD.search(arguments):
            if method.group(1).upper() != "GET":
                return True
        elif re.search(r"\bgraphql\b", arguments):
            if "mutation" in arguments.lower():
                return True
        elif GH_FIELDS.search(arguments):
            return True
    return False


def within(path: str, root: str) -> bool:
    try:
        return Path(path).expanduser().resolve().is_relative_to(Path(root).expanduser().resolve())
    except (OSError, ValueError):
        return False


# Chief of Staff's private files for other agents' workers (a Claude worker's MCP configuration,
# with its servers' headers and variables, and its system prompt: 0600). Never a worker's to read
# or change, whatever it may do otherwise (Runtime.private_folders).
PRIVATE_FILES = "Chief of Staff's private files are not a worker's to read or change"


def private_denial(request: Any, folders: list[str]) -> str | None:
    """Why a session may not do this: it reads or writes in Chief of Staff's private folders, or
    its command names one (by its path, its real path or its last parts); None otherwise."""
    if not folders:
        return None
    kind = getattr(request, "kind", "")
    paths: list[str] = []
    if kind in ("read", "write"):
        paths = [
            str(getattr(request, name, "") or "") for name in ("resolved_path", "path", "file_name")
        ]
    elif kind == "shell":
        text = str(getattr(request, "full_command_text", "") or "")
        if any(token in text for folder in folders for token in private_tokens(folder)):
            return PRIVATE_FILES
        paths = [str(path) for path in getattr(request, "possible_paths", None) or []]
        paths += [str(path) for path in (getattr(request, "resolved_paths", None) or {}).values()]
    if any(path and within(path, folder) for path in paths for folder in folders):
        return PRIVATE_FILES
    return None


def private_tokens(folder: str) -> set[str]:
    """How a command may name a private folder: its path, its real path, and its last three
    parts (agents/claude/private), which name it from its data folder or from anywhere above."""
    parts = Path(folder).parts[-3:]
    return {folder, os.path.realpath(folder), "/".join(parts)}


def written_file(cwd: str, request: Any) -> str:
    target = getattr(request, "resolved_path", None) or getattr(request, "file_name", "") or ""
    if cwd and target and not Path(target).is_absolute():
        target = str(Path(cwd) / target)
    if cwd and target and within(target, cwd):
        return os.path.relpath(target, cwd)
    return target


# A repository's own Copilot configuration, which Copilot loads only in a folder it trusts (a
# worktree of a trusted repository is trusted too), from the working directory up to the
# repository's top: hooks (one can approve what Chief of Staff would refuse), MCP and language
# servers (their commands run outside the worker's sandbox), plugins and extensions. Copilot also
# runs the hooks of Claude's settings. Checked on the Copilot CLI (scripts in its security
# review); the contract test reads the installed CLI's help for others (KNOWN_UNGATED there).
REPOSITORY_SETTINGS = (
    ".mcp.json",
    ".github/mcp.json",
    ".github/lsp.json",
    ".github/hooks",
    ".github/copilot",
    ".github/extensions",
    ".github/agents",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/agents",
)
# Of those, what Copilot loads by itself mid-session, with no tool call and no session start or
# resume: it watches its MCP servers' files and starts a server added there within seconds,
# outside the worker's sandbox (checked on the Copilot CLI 1.0.100 and 1.0.112). A sandboxed
# worker's commands may never write these in its own folder; the others load only as a session
# starts or resumes, which Chief of Staff's check precedes, or through a tool (hooks, plugins,
# extensions, language servers, custom agents), which its pre-tool check sees first.
SANDBOX_HELD = (".mcp.json", ".github/mcp.json")
# Codex's own (a trusted folder's rules, MCP servers, environment, model, hooks).
CODEX_SETTINGS = (".codex",)
# Claude Code's own, of those (2.1.322): what it loads from a folder as its process starts there
# (a worker's, resumed; the owner's own in a terminal): its settings (hooks, apiKeyHelper,
# permissions, MCP and plugin switches), MCP servers and subagents (with their hooks and
# servers). Its skills, commands and CLAUDE.md stay a worker's to write.
CLAUDE_SETTINGS = (
    ".mcp.json",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/agents",
)
# Every agent's: what a folder's snapshot holds (settings_guard).
AGENT_SETTINGS = REPOSITORY_SETTINGS + CODEX_SETTINGS
RUN_WRITE = (
    "The repository's agent settings run this file (a hook's script, a server's): a change to it "
    "would run outside Chief of Staff's checks, so workers don't write it. Leave that change to "
    "the owner."
)
SETTINGS_WRITE = (
    "A coding agent loads this file as the repository's own settings (hooks, MCP servers, "
    "agents), outside Chief of Staff's checks: workers don't write it. Leave that change to "
    "the owner."
)


def repository_settings(path: str) -> bool:
    """Whether a path is (or is in) one of the repository settings an agent loads in a trusted
    folder, by its path or where its links lead; names compared as a case-insensitive file
    system compares them (macOS's, Windows'), NFC or NFD."""
    for spelled in {path, os.path.realpath(path)}:
        parts = tuple(unicodedata.normalize("NFC", part).casefold() for part in Path(spelled).parts)
        for name in AGENT_SETTINGS:
            gated = tuple(name.casefold().split("/"))
            if any(parts[i : i + len(gated)] == gated for i in range(len(parts))):
                return True
    return False


def settings_write(row: dict, request: Any, ran: Iterable[str] = ()) -> str | None:
    """Why a worker may not write this file: it is a repository setting Copilot would load from
    its next turn or start, or a file such a setting runs (`ran`: a hook's script, held with
    them), which a hook would run at once (any worker, also one acting without permission
    checks)."""
    if row["role"] != WORKER or getattr(request, "kind", "") != "write":
        return None
    target = getattr(request, "resolved_path", None) or getattr(request, "file_name", "") or ""
    if target and not Path(target).is_absolute():
        target = str(Path(row["cwd"] or ".") / target)
    if not target:
        return None
    if repository_settings(target):
        return SETTINGS_WRITE
    real = os.path.realpath(target)
    if any(real == os.path.realpath(path) for path in ran):
        return RUN_WRITE
    return None


OWNERS_CHECKOUT = (
    "This folder is the owner's own checkout: nothing is written there. To change it or run "
    "what writes files (installs, builds, tests), add_repo it: you get your own worktree and "
    "branch, and work there."
)
OWNERS_CHECKOUT_ASK = (
    "This runs a program Chief of Staff can't read on files in the owner's own checkout, where "
    'nothing is written: if it only reads them, ask the owner with report(status="question"). '
    "To change them or run what writes files (installs, builds, tests), add_repo it: you get "
    "your own worktree and branch, and work there."
)
# What a worker hears instead of those, for a shell command, once it has its own copy of that
# checkout (Runtime.own_copy): one made then (OWN_COPY: `left_out`, what could not be copied
# in, as a sentence), or a worktree of that repository it had (OWN_WORKTREE).
OWN_COPY = (
    "The same command was not run in the owner's checkout; your own copy of it (same commit, "
    "with the owner's uncommitted changes) is ready at {path}; run it there.{left_out} That "
    "changes nothing of the owner's, so it is fine even for a task that says not to change any "
    "files."
)
OWN_WORKTREE = (
    "The same command was not run in the owner's checkout; your own worktree of it is at "
    "{path}; run it there. That changes nothing of the owner's, so it is fine even for a task "
    "that says not to change any files."
)
# What a worker hears of an edit in the owner's checkout its own worktree was made from.
OWNERS_CHECKOUT_TREE = (
    "This file is in the owner's own checkout: nothing is written there. Your own worktree of "
    "it is at {path}; change files there."
)
# What a shell command line does in a checkout of the owner's given only to read: see
# checkout_write.
CHECKOUT_WRITES, CHECKOUT_ASKS = "writes", "asks"
# Programs that write none of the files they name (only where their output is redirected).
CHECKOUT_READERS = (READERS - {"git", "sed", "perl", "awk", "gawk", "source", "."}) | frozenset(
    {"sleep", "pwd", "which", "type", "whereis", "date", "uname", "id", "whoami", "hostname",
     "printenv", "du", "df", "tree", "column", "fold", "fmt", "rev", "tac", "paste", "join",
     "expand", "unexpand", "look", "tr", "sum", "seq", "yes", "expr", "ps", "pgrep", "lsof",
     "kill", "pkill", "history", "jobs", "wait", "exit", "return", "break", "continue",
     "shift", "export", "unset", "set", "shopt", "local", "declare", "typeset", "readonly",
     "let", "read", "getopts", "hash", "alias", "unalias", "popd", "dirs", "nproc", "sysctl",
     "sw_vers", "locale", "tput", "clear", "uptime", "free", "vm_stat", "env", "diff3",
     "sdiff", "numfmt", "factor", "shuf", "tsort", "fd", "zcat", "zgrep", "zless", "bzcat",
     "xzcat", "lz4cat", "iconv", "gh", "[", "[[", "test", "true", "false", ":"}
)  # fmt: skip
# Programs whose every argument is a file they write, move or remove.
CHECKOUT_MAKERS = frozenset(
    {"rm", "rmdir", "unlink", "touch", "mkdir", "shred", "mkfifo", "mknod", "tee", "gzip",
     "gunzip", "bzip2", "bunzip2", "xz", "unxz", "zstd", "lz4", "truncate", "mktemp", "chflags",
     "setfacl", "xattr", "mv", "compress", "uncompress", "srm", "trash"}
)  # fmt: skip
# Their first argument is what they set (a mode, an owner), the rest the files.
CHECKOUT_MODES = frozenset({"chmod", "chown", "chgrp", "chattr"})
# Their last argument is where they write (or `-t DIR`).
CHECKOUT_COPIERS = frozenset({"cp", "install", "ln", "rsync", "scp", "ditto", "rclone"})
# Installs, builds, tests and what runs them: they write in the folder they run in.
CHECKOUT_BUILDERS = frozenset(
    {"npm", "npx", "pnpm", "pnpx", "yarn", "bun", "bunx", "deno", "pip", "pip3", "pipx", "uv",
     "uvx", "poetry", "pipenv", "pdm", "hatch", "rye", "pixi", "conda", "mamba", "micromamba",
     "cargo", "rustup", "go", "make", "gmake", "cmake", "ninja", "meson", "gradle", "gradlew",
     "mvn", "mvnw", "ant", "bazel", "bazelisk", "sbt", "dotnet", "swift", "xcodebuild", "pytest",
     "py.test", "tox", "nox", "pre-commit", "ruff", "black", "isort", "prettier", "eslint",
     "tsc", "jest", "vitest", "mypy", "pyright", "coverage", "bundle", "rake", "gem",
     "composer", "mix", "stack", "cabal", "flutter", "dart", "terraform", "tofu", "pod",
     "mise", "asdf", "volta", "corepack", "maturin", "twine", "lerna", "nx", "turbo",
     "webpack", "vite", "rollup", "esbuild", "babel", "jupyter", "sphinx-build", "mkdocs",
     "hugo", "jekyll", "rails", "django-admin", "alembic", "venv", "virtualenv", "build",
     "unittest", "nose2", "behave", "playwright", "cypress"}
)  # fmt: skip
# What a builder is asked that writes nothing where it runs: its version, its help, a list.
CHECKOUT_BUILDER_READS = frozenset(
    {"--version", "-V", "--help", "-h", "help", "version", "list", "ls", "show", "view", "info",
     "outdated", "freeze", "doc", "metadata", "search", "why", "explain"}
)  # fmt: skip
# Options of a builder whose value is the folder it runs in.
CHECKOUT_FOLDER_OPTIONS = frozenset(
    {"-C", "--directory", "--prefix", "--cwd", "--project", "--project-dir", "--root",
     "--manifest-path", "--dir", "--chdir", "--working-directory"}
)  # fmt: skip
# Programs that run another one (past their options): what that one does counts.
CHECKOUT_WRAPPERS = frozenset(
    {"env", "nice", "nohup", "stdbuf", "time", "command", "builtin", "exec", "caffeinate",
     "sudo", "doas", "ionice", "chrt", "taskset", "timeout", "xargs", "watch", "unbuffer",
     "flock", "setsid"}
)  # fmt: skip
# Their options that take a value (given apart).
CHECKOUT_WRAPPER_VALUED = frozenset(
    {"-n", "-u", "-o", "-e", "-i", "-I", "-L", "-P", "-d", "-E", "-s", "-k", "-c", "-t", "-g",
     "-a", "--signal", "--kill-after", "--max-args", "--max-procs", "--delimiter", "-S",
     "--user", "--group"}
)  # fmt: skip
# git's commands that change nothing of a checkout: its work tree, index or branches (fetch
# and push move remote branches only; where a push goes is checked on its own). With
# --output, what they write is read.
GIT_READS_ONLY = frozenset(
    {"status", "diff", "show", "log", "blame", "annotate", "grep", "ls-files", "ls-tree",
     "ls-remote", "cat-file", "rev-parse", "rev-list", "shortlog", "describe", "for-each-ref",
     "show-ref", "show-branch", "merge-base", "name-rev", "count-objects", "help", "version",
     "var", "check-ignore", "check-attr", "check-ref-format", "whatchanged", "range-diff",
     "fetch", "verify-commit", "verify-tag", "cherry", "diff-tree", "diff-files", "diff-index",
     "get-tar-commit-id", "fsck", "difftool", "push", "send-pack"}
)  # fmt: skip
# git's commands that only list or show when given one of these words, or (GIT_BARE_LISTS)
# none and only GIT_LIST_OPTIONS.
GIT_LISTING = {"branch": (), "tag": (), "stash": ("list", "show"), "worktree": ("list",),
               "remote": ("show", "get-url"), "reflog": ("show", "exists"),
               "notes": ("list", "show"), "submodule": ("status", "summary"),
               "sparse-checkout": ("list",), "lfs": ("ls-files", "status", "env", "version"),
               "bisect": ("log", "visualize", "view")}  # fmt: skip
GIT_BARE_LISTS = frozenset({"branch", "tag", "remote", "reflog", "notes", "submodule"})
GIT_LIST_OPTIONS = frozenset(
    {"-a", "--all", "-r", "--remotes", "-l", "--list", "-v", "-vv", "--verbose",
     "--show-current", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
     "--sort", "--format", "--column", "--no-column", "--color", "--no-color", "-n",
     "--ignore-case", "--abbrev", "-q", "--quiet", "--cached", "--recursive"}
)  # fmt: skip
GIT_CHANGES = frozenset(
    {"add", "am", "apply", "bisect", "branch", "checkout", "cherry-pick", "clean", "commit",
     "config", "gc", "merge", "mv", "notes", "prune", "pull", "read-tree", "rebase", "replace",
     "repack", "reset", "restore", "revert", "rm", "sparse-checkout", "stash", "submodule",
     "switch", "symbolic-ref", "tag", "update-index", "update-ref", "worktree", "checkout-index",
     "merge-file", "maintenance", "lfs", "filter-branch", "filter-repo", "commit-tree",
     "hash-object", "write-tree", "remote", "reflog", "pack-refs", "prune-packed",
     "commit-graph", "multi-pack-index", "rerere", "mergetool", "stage", "unpack-objects",
     "index-pack", "fast-import", "init", "clone", "format-patch"}
)  # fmt: skip
# Interpreters: a program not known, but `-m TOOL` runs that tool (a builder: pytest, pip);
# with a script, that script is the program they run.
CHECKOUT_INTERPRETERS = re.compile(r"python[\d.]*|py|pypy[\d.]*")
SCRIPT_RUNNERS = re.compile(
    r"python[\d.]*|py|pypy[\d.]*|node|ruby|php|Rscript|julia|lua|osascript|tclsh|"
    r"bash|sh|zsh|dash|ksh|fish"
)
GLOB = re.compile(r"[*?\[]")
# A sed script's `w FILE` (or `s///w FILE`): a file it writes.
SED_WRITES = re.compile(r"(?:^|[;{}\n])\s*[0-9,$/]*\s*[wW]\s*\S|/[gpiIme0-9]*w\s*\S")


def checkout_places(kept: tuple[str, ...]) -> tuple[str, ...]:
    """The read-only checkouts' folders as they may be named: as given, and as the system
    resolves them (macOS's /var is /private/var)."""
    places = set()
    for folder in kept:
        for form in (os.path.normpath(folder), os.path.realpath(folder)):
            places.add(form.rstrip("/") or "/")
    return tuple(sorted(places))


# Where a line may be at a point of it (CheckoutWrites.line): folders, None for one not known.
Places = frozenset[str | None]
NO_PLACE: Places = frozenset()
UNKNOWN_PLACE: Places = frozenset({None})
# The most folders a line is followed in at once: past them, where it is is not known.
MOST_PLACES = 8
# A separator's parts: a subshell's opening and closing, and the operators between.
SEPARATOR_PIECES = re.compile(r"[()]|[^()]+")


def capped(places: Places) -> Places:
    return UNKNOWN_PLACE if len(places) > MOST_PLACES else places


class Moves:
    """The moves a line makes (`cd`, `pushd`, `popd`, Set-Location…): the folders each `pushd`
    left, with where the line stayed if it failed, for `popd` to go back to."""

    def __init__(self):
        self.pushed: list[tuple[Places, Places, int]] = []
        self.count = 0

    def entered(
        self, pushing: bool, here: Places, moved: Places, failed: Places
    ) -> tuple[Places, Places]:
        """A move from `here`: where it leaves the line, and where it stays when it fails."""
        self.count += 1
        if pushing:
            self.pushed.append((here, failed, self.count))
        return capped(moved), failed

    def popped(self, here: Places) -> tuple[Places, Places]:
        """`popd`: back where its `pushd` was. Where that pushd failed, nothing of it was
        pushed: this popd goes back where the one before it was, or fails (and stays where
        that pushd left the line, if nothing moved it since; else wherever it may be)."""
        self.count += 1
        if not self.pushed:
            return UNKNOWN_PLACE, here
        before, failed, at = self.pushed.pop()
        if not failed:
            return before, NO_PLACE
        if self.pushed:
            return capped(before | self.pushed[-1][0]), NO_PLACE
        return before, failed if at == self.count - 1 else here


def joined(
    joiner: str, before: tuple[Places, Places], after: tuple[Places, Places]
) -> tuple[Places, Places]:
    """Where a list is when it succeeded and when it failed (`ok`, `failed`), once a command
    joined to it ends: after `&&` it ran only if the list succeeded, after `||` if it failed."""
    if joiner == "&&":
        return after[0], capped(before[1] | after[1])
    if joiner == "||":
        return capped(before[0] | after[0]), after[1]
    return after


class CheckoutWrites:
    """What a line does in a checkout of the owner's given only to read (`kept`), as
    checkout_write reads it: `found` is CHECKOUT_WRITES (it writes there), CHECKOUT_ASKS (a
    program not known runs there on its files, or what it writes could not be read in a line
    that runs or names one there), or "". `wrote`: it names what it writes there (a file, git's
    work tree, index or branches); `ran`: it runs a program there that may write there (an
    install, a build, a test run, a program not known), or what it does there can't be read.
    `works_there`: the worker works in them (one given to read), so a folder not known (`cd
    "$dir"`) may be one; not the checkouts its own worktrees were made from, where only a line
    run in one, or naming one, is about one."""

    works_there = True
    wrote = ran = False

    def __init__(self, text: str, kept: tuple[str, ...]):
        self.places = checkout_places(kept)
        self.home = str(Path("~").expanduser())  # (As HOME says now.)
        text = text.replace("\\\n", "")
        self.mentioned = any(place in text for place in self.places) or any(
            place.startswith(self.home + "/") and "~" + place[len(self.home) :] in text
            for place in self.places
        )
        self.found = ""
        self.links: dict[str, str] = {}  # Links the line makes (`ln -s`): where to, what.

    def inside(self, path: str, real: bool = False) -> bool:
        if any(path == place or path.startswith(place + "/") for place in self.places):
            return True
        if not real:
            return False
        resolved = os.path.realpath(path)  # (A link into it: where the system writes.)
        return any(resolved == place or resolved.startswith(place + "/") for place in self.places)

    def there(self, folder: str | None) -> bool:
        """Whether a folder is in a read-only checkout (one not known: when the line names
        one)."""
        return self.mentioned if folder is None else self.inside(folder, real=True)

    def near(self, folder: str | None) -> bool:
        """Whether what a line writes where it can't be read may be in a read-only checkout:
        it runs in one (or where is not known, of those it works in), or names one."""
        if folder is None:
            return self.mentioned or self.works_there
        return self.mentioned or self.inside(folder, real=True)

    def ask(self):
        self.found, self.ran = self.found or CHECKOUT_ASKS, True

    def write(self):
        self.found, self.wrote = CHECKOUT_WRITES, True

    def place(self, word: str, folder: str | None) -> str | None:
        """Where a word names a file, from `folder` (None: not known, as a variable's)."""
        if not word or "$" in word or "`" in word or MADE_UP in word:
            return None
        if word.startswith("~"):
            if not word.startswith(("~/", "~")) or (word != "~" and not word.startswith("~/")):
                return None  # (Another account's home.)
            word = self.home + word[1:]
        if match := GLOB.search(word):
            word = word[: match.start()].rpartition("/")[0] or ("/" if word[0] == "/" else ".")
        if not word.startswith("/"):
            if folder is None:
                return None
            word = f"{folder}/{word}"
        return os.path.normpath(word)

    def writes(self, word: str, folder: str | None):
        """A file the line writes (or moves, or removes); through a link it made, its target."""
        where = self.place(word, folder)
        for link, target in self.links.items():
            if where is not None and (where == link or where.startswith(link + "/")):
                where = target + where[len(link) :]
        if where is None:
            if self.near(folder):
                self.ask()
        elif self.inside(where, real=True):
            self.write()

    def runs_in(self, folder: str | None):
        """A program that writes in the folder it runs in (an install, a build, a test run)."""
        if folder is None:
            if self.mentioned:
                self.ask()
        elif self.inside(folder, real=True):
            self.found, self.ran = CHECKOUT_WRITES, True

    def unknown(self, name: str, arguments: list[str], folder: str | None):
        """A program not known: asked about when it is in a read-only checkout, or runs in one
        on files there."""
        if "/" in name and (program := self.place(name, folder)) and self.inside(program, True):
            self.ask()
            return
        if folder is None or not self.inside(folder, real=True):
            return
        for word in arguments:
            value = word.partition("=")[2] if word.startswith("-") else word
            where = self.place(value, folder) if value else None
            if where and self.inside(where) and ("/" in value or os.path.lexists(where)):
                self.ask()
                return

    def moved(self, folder: str | None, target: str) -> str | None:
        """The folder `cd target` goes to from `folder` (None: not known)."""
        if not target or target == "-" or GLOB.search(target):
            return None
        return self.place(target, folder)

    @staticmethod
    def enterable(folder: str | None) -> bool:
        """Whether a `cd` into `folder` can't fail: a folder there now that can be entered (one
        not known, or not there yet, may not be: then the line goes on where it was)."""
        return folder is not None and Path(folder).is_dir() and os.access(folder, os.X_OK)

    def location(
        self, base: str, rest: list[str], here: Places, moves: Moves
    ) -> tuple[Places, Places]:
        """Where `cd`, `pushd` or `popd` (its words but options, `rest`) leaves the line when it
        succeeds, and when it fails (where it was: a cd that can fail, see enterable)."""
        if base == "popd":
            return moves.popped(here)
        # (`cd a b`: bash refuses it, zsh replaces a part of the folder's path.)
        goes = {f: None if len(rest) > 1 else self.moved(f, rest[0] if rest else "~") for f in here}
        failed = frozenset(f for f, to in goes.items() if len(rest) > 1 or not self.enterable(to))
        return moves.entered(base == "pushd", here, frozenset(goes.values()), failed)

    def command(self, words: list[str], targets: list[str], folder: str | None) -> list[str]:
        """One simple command (its redirections' targets apart): what it writes. The lines it
        runs (`sh -c '…'`, `eval`)."""
        for target in targets:
            self.writes(target, folder)
        name, start = command_name(words)
        rest = list(words[start:])
        fed = False  # Its files come from its input (xargs).
        base = name.rsplit("/", 1)[-1]
        while base in CHECKOUT_WRAPPERS and "/" not in name:
            if base == "env":
                while rest and (rest[0].startswith("-") or ASSIGNMENT.match(rest[0])):
                    option = rest.pop(0)
                    if option in {"-C", "--chdir"} and rest:
                        folder = self.moved(folder, rest.pop(0))
                    elif option in {"-u", "--unset", "-S", "--split-string"} and rest:
                        rest.pop(0)
            else:
                fed = fed or base == "xargs"
                while rest and rest[0].startswith("-") and rest[0] != "-":
                    option = rest.pop(0)
                    if option in CHECKOUT_WRAPPER_VALUED and rest:
                        rest.pop(0)
                if base == "timeout" and rest:
                    rest.pop(0)  # (Its duration.)
            if not rest:
                return []
            name, rest = rest[0], rest[1:]
            base = name.rsplit("/", 1)[-1]
        if not name or (words[:1] and words[0] in {"for", "select", "case"}):
            return []  # (A loop's or a case's words: what it runs comes after `do`, `)`.)
        scripts = inner_scripts([name, *rest]) + evaluated([name, *rest])[0]
        if scripts:
            return scripts
        positional = [word for word in rest if not word.startswith("-") or word == "-"]
        if fed and base not in CHECKOUT_READERS:
            # Its files come from its input: wherever the line runs or names.
            if base in CHECKOUT_MAKERS | CHECKOUT_MODES | CHECKOUT_COPIERS | {"sed", "perl"}:
                if self.there(folder) or self.mentioned:
                    self.write()
            elif self.there(folder):
                self.ask()
        if base in CHECKOUT_READERS:
            if base == "gh" and rest[:2] in (["pr", "checkout"], ["repo", "clone"]):
                self.runs_in(folder)
            return []
        if base in CHECKOUT_MAKERS:
            for word in positional:
                self.writes(word, folder)
        elif base in CHECKOUT_MODES:
            reference = any(word.startswith("--reference") for word in rest)
            for word in positional if reference else positional[1:]:
                self.writes(word, folder)
        elif base in CHECKOUT_COPIERS:
            self.copies(base, rest, positional, folder)
        elif handler := CHECKOUT_SPECIAL.get(base):
            handler(self, rest, positional, folder)
        elif CHECKOUT_INTERPRETERS.fullmatch(base) and "-m" in rest:
            module = rest[rest.index("-m") + 1] if rest.index("-m") + 1 < len(rest) else ""
            if module.split(".")[0] in CHECKOUT_BUILDERS | {"pip"}:
                self.runs_in(folder)
            else:
                self.unknown(name, rest, folder)
        elif base in CHECKOUT_BUILDERS:
            self.builds(base, rest, folder)
        elif SCRIPT_RUNNERS.fullmatch(base) and positional and positional[0] != "-":
            self.unknown(positional[0], rest, folder)  # (Its script: the program it runs.)
        else:
            self.unknown(name, rest, folder)
        return []

    def copies(self, base: str, rest: list[str], positional: list[str], folder: str | None):
        """cp, ln, install, rsync…: where they write (`-t DIR`, or their last argument)."""
        targets = [b for a, b in itertools.pairwise(rest) if a in {"-t", "--target-directory"}]
        targets += [w.partition("=")[2] for w in rest if w.startswith("--target-directory=")]
        plain = [w for w in positional if not (base in {"rsync", "scp"} and ":" in w)]
        if not targets:
            if base == "ln" and len(plain) == 1:
                targets = [plain[0].rstrip("/").rsplit("/", 1)[-1]]  # (Made where it runs.)
            elif base == "install" and "-d" in rest:
                targets = plain
            else:
                targets = plain[-1:]
        for target in targets:
            self.writes(target, folder)
        if base == "ln" and plain:
            # What it writes through the links later (`chmod`, `>`): their targets.
            source = self.place(plain[0], folder)
            for target in targets:
                if (link := self.place(target, folder)) and source:
                    self.links[link] = source

    def builds(self, base: str, rest: list[str], folder: str | None):
        """An install, build or test tool: it writes in the folder it runs in (or the one an
        option names), unless all it is asked is its version, its help or a list."""
        plain = [word for word in rest if not word.startswith("-")]
        if (plain[:1] and plain[0] in CHECKOUT_BUILDER_READS) or (
            not plain and rest and set(rest) <= CHECKOUT_BUILDER_READS
        ):
            return
        where = folder
        for at, word in enumerate(rest):
            option, _, value = word.partition("=")
            if option in CHECKOUT_FOLDER_OPTIONS:
                value = value or (rest[at + 1] if at + 1 < len(rest) else "")
                if option == "--manifest-path":
                    value = value.rpartition("/")[0] or "."
                where = self.moved(where, value)
        self.runs_in(where)

    def line(self, text: str, folder: str | None) -> list[tuple[str, str | None]]:
        """A line's simple commands, each where it runs (`cd`, `pushd`, `popd`; a subshell's
        moves end with it): what each writes. The lines its commands run, and where."""
        if len(text) > MOST_LINE:
            raise TooComplexError
        unheredoc, bodies = heredocs(text)
        plain, inside = without_substitutions(without_comments(unheredoc).replace("\\\n", ""))
        for body, quoted in bodies:
            if not quoted:
                inside += without_substitutions(body, quoted=True)[1]
        runs = [(body, folder) for body in inside]
        tokens = shell_words(plain)
        words: list[str] = []
        targets: list[str] = []
        target = ""
        fed_shell = False
        # Where the line may be: `here`, where the next command runs; `ok` and `failed`, where
        # it is when what ran so far (the list up to here) succeeded, and when it failed, which
        # `&&` and `||` choose between; `ran`, where the last command ran (a pipe's next command
        # starts there too, and a background one's leaves it there). A subshell's moves end with
        # it; one in it that can fail (see location) leaves its commands where it was too.
        here = ok = failed = ran = frozenset({folder})
        joiner = ""  # What joined the next command to the list before it ("": its start).
        stack: list[tuple[Places, Places, Places, str]] = []
        moves = Moves()
        for index, token in enumerate([*tokens, Separator(";")]):
            if isinstance(token, Separator):
                following = tokens[index + 1] if index + 1 < len(tokens) else None
                ampersand = str(token) == "&"
                if ampersand and target == ">" and following is not None:
                    target = "copy"  # (`2>&1`, `>&2`: a descriptor's copy, not a file.)
                    continue
                if ampersand and isinstance(following, Redirecting):
                    continue  # (`&>log`: its file read with the redirection.)
                name, start = command_name(words)
                base = name.rsplit("/", 1)[-1]
                outcome: tuple[Places, Places] | None = None
                if base in {"cd", "pushd", "popd"}:
                    rest = [word for word in words[start:] if not word.startswith("-")]
                    outcome = self.location(base, rest, here, moves)
                elif base in {"source", "."}:
                    for where in here:
                        self.unknown(name, words[start:], where)
                        file = words[start] if start < len(words) else ""
                        if (found := self.place(file, where)) and self.inside(found, real=True):
                            self.ask()
                    outcome = UNKNOWN_PLACE, UNKNOWN_PLACE  # (Where it leaves the shell.)
                elif words or targets:
                    if base in FED_SHELLS and not any(w[:1] != "-" for w in words[start:]):
                        fed_shell = True  # (A shell running what its input says.)
                    for where in here:
                        runs += [(script, where) for script in self.command(words, targets, where)]
                    # (`exit`, `return`: nothing after them runs.)
                    outcome = (NO_PLACE, NO_PLACE) if base in {"exit", "return"} else (here, here)
                if outcome is not None:
                    if words[:start].count("!") % 2:
                        outcome = outcome[1], outcome[0]  # (`! cd x`: its status turned.)
                    ok, failed = joined(joiner, (ok, failed), outcome)
                    ran = here
                words, targets, target = [], [], ""
                for piece in SEPARATOR_PIECES.findall(token):
                    if piece == "(":
                        stack.append((ok, failed, here, joiner))
                        ok = failed = here
                        joiner = ""
                    elif piece == ")":
                        if stack:
                            before_ok, before_failed, here, opened = stack.pop()
                            ok, failed = joined(opened, (before_ok, before_failed), (here, here))
                            ran = here
                    else:
                        joiner = (
                            "||" if "||" in piece else "&&" if "&&" in piece
                            else "|" if "|" in piece else "&" if "&" in piece else ";"
                        )  # fmt: skip
                        here = capped(
                            ok if joiner == "&&" else failed if joiner == "||"
                            else ok | failed | (ran if joiner in {"|", "&"} else NO_PLACE)
                        )  # fmt: skip
                continue
            if target:
                if target == ">":
                    targets.append(token)
                target = ""
                continue
            if isinstance(token, Redirecting):
                operator = token[token.at :]
                found = REDIRECTION.match(operator)
                writes = ">" in operator[: found.end() if found else 0]
                file = operator[found.end() :] if found else ""
                if file and writes and not file.startswith("&"):
                    targets.append(file)
                elif not file:
                    target = ">" if writes else "<"
                head = token[: token.at]
                if head and not head.isdigit():
                    words.append(head)
                continue
            words.append(token)
        if fed_shell:
            runs += [(body, where) for body, _ in bodies for where in ok | failed]
        return runs


CheckoutHandler = Callable[[CheckoutWrites, list[str], list[str], "str | None"], None]
CHECKOUT_SPECIAL: dict[str, CheckoutHandler] = {}


def reads_for(*names: str) -> Callable[[CheckoutHandler], CheckoutHandler]:
    """How a program's words say what it writes (CheckoutWrites.command)."""

    def register(handler: CheckoutHandler) -> CheckoutHandler:
        for name in names:
            CHECKOUT_SPECIAL[name] = handler
        return handler

    return register


@reads_for("sed")
def checkout_sed(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    """sed changes its files in place with -i; a script's `w FILE` writes where that is not
    read here."""
    scripts = [b for a, b in itertools.pairwise(rest) if a in {"-e", "--expression", "-f"}]
    files = positional if scripts else positional[1:]
    if any(w.startswith("--in-place") or (w[:2] != "--" and w[:1] == "-" and "i" in w)
           for w in rest):  # fmt: skip
        for word in files:
            guard.writes(word, folder)
    elif SED_WRITES.search(" ".join(scripts or positional[:1])) and guard.near(folder):
        guard.ask()


@reads_for("perl")
def checkout_perl(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    """perl changes its files in place with -i; else it runs code: a program not known."""
    scripts = [b for a, b in itertools.pairwise(rest) if a in {"-e", "-E"}]
    if any(w[:2] != "--" and w[:1] == "-" and "i" in w for w in rest):
        files = [w for w in positional if w not in scripts]
        for word in files if scripts else files[1:]:
            guard.writes(word, folder)
    else:
        guard.unknown("perl", rest, folder)


@reads_for("awk", "gawk", "mawk", "nawk")
def checkout_awk(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    """awk changes its files in place with `-i inplace`; a program that prints into a file or
    runs a command writes where that is not read here."""
    if "inplace" in rest:
        for word in positional[1:]:
            guard.writes(word, folder)
    elif positional and re.search(r">|\bsystem\b|\|", positional[0]) and guard.near(folder):
        guard.ask()


@reads_for("sort")
def checkout_sort(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    for before, word in itertools.pairwise(["", *rest]):
        if before in {"-o", "--output"}:
            guard.writes(word, folder)
        elif word.startswith("--output="):
            guard.writes(word.partition("=")[2], folder)
        elif word.startswith("-o") and len(word) > 2:
            guard.writes(word[2:], folder)


@reads_for("uniq", "xxd")
def checkout_second(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    """uniq IN OUT, xxd [-r] IN OUT: their second file is written."""
    if len(positional) > 1:
        guard.writes(positional[1], folder)


@reads_for("dd")
def checkout_dd(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    for word in rest:
        if word.startswith("of="):
            guard.writes(word[3:], folder)


@reads_for("split", "csplit")
def checkout_split(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    prefix = [b for a, b in itertools.pairwise(rest) if a in {"-f", "--prefix"}]
    guard.writes(prefix[-1] if prefix else (positional[1] if len(positional) > 1 else "x"), folder)


@reads_for("patch")
def checkout_patch(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    """patch writes the files its diff names, where it runs (or -d DIR), or the one named."""
    where = folder
    for before, word in itertools.pairwise(["", *rest]):
        if before in {"-d", "--directory"}:
            where = guard.moved(where, word)
        elif before in {"-o", "--output"}:
            guard.writes(word, folder)
    for word in positional[:1]:
        guard.writes(word, where)
    guard.runs_in(where)


@reads_for("tar", "bsdtar", "gtar")
def checkout_tar(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    """tar: extracting writes where it runs (or -C DIR); creating writes its archive (-f)."""
    first = rest[0] if rest else ""
    letters = first.lstrip("-") if not first.startswith("--") else ""
    options = {word.split("=")[0] for word in rest if word.startswith("--")}
    extract = "x" in letters or bool(options & {"--extract", "--get"})
    create = bool(set(letters) & set("cru")) or bool(options & {"--create", "--append"})
    where, archive = folder, ""
    for at, word in enumerate(rest):
        value = rest[at + 1] if at + 1 < len(rest) else ""
        if word in {"-C", "--directory"}:
            where = guard.moved(where, value)
        elif word.startswith("--directory="):
            where = guard.moved(where, word.partition("=")[2])
        elif word in {"-f", "--file"} or (at == 0 and "f" in letters and word == first):
            archive = value
        elif word.startswith("--file="):
            archive = word.partition("=")[2]
    if extract:
        guard.runs_in(where)
    if create and archive and archive != "-":
        guard.writes(archive, folder)


@reads_for("unzip")
def checkout_unzip(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    if set(rest) & {"-l", "-t", "-v", "-Z", "-p", "-z", "-lv"}:
        return
    where = folder
    for before, word in itertools.pairwise(["", *rest]):
        if before == "-d":
            where = guard.moved(where, word)
    guard.runs_in(where)


@reads_for("zip")
def checkout_zip(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    for word in positional[:1]:
        guard.writes(word, folder)


@reads_for("curl")
def checkout_curl(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    where = folder
    for at, word in enumerate(rest):
        option, _, value = word.partition("=")
        value = value or (rest[at + 1] if at + 1 < len(rest) else "")
        if option in {"-o", "--output", "-D", "--dump-header", "-c", "--cookie-jar", "--trace",
                      "--trace-ascii", "--libcurl", "--etag-save"}:  # fmt: skip
            if value != "-":
                guard.writes(value, folder)
        elif option == "--output-dir":
            where = guard.moved(folder, value)
        elif option in {"--remote-name", "--remote-name-all", "--remote-header-name"} or (
            option[:1] == "-" and option[1:2] != "-" and ("O" in option or "J" in option)
        ):
            guard.runs_in(where)


@reads_for("wget")
def checkout_wget(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    where, named = folder, False
    for at, word in enumerate(rest):
        option, _, value = word.partition("=")
        value = value or (rest[at + 1] if at + 1 < len(rest) else "")
        if option in {"-O", "--output-document", "-o", "--output-file", "-a", "--append-output"}:
            named = named or option in {"-O", "--output-document"}
            if value != "-":
                guard.writes(value, folder)
        elif option in {"-P", "--directory-prefix"}:
            where = guard.moved(folder, value)
        elif option == "--spider":
            named = True
    if not named:
        guard.runs_in(where)


@reads_for("find")
def checkout_find(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    """find: what it deletes or writes (-delete, -fprint…) under the folders it searches, and
    the commands it runs on what it finds there (-exec…)."""
    roots: list[str] = []
    for word in rest:
        if word[:1] in {"-", "(", "!"}:
            break
        roots.append(word)
    places = [guard.place(root, folder) for root in roots] or [folder]
    if not any(guard.there(place) for place in places):
        return
    for at, word in enumerate(rest):
        if word in {"-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}:
            guard.write()
        elif word in {"-exec", "-execdir", "-ok", "-okdir"}:
            run = list(itertools.takewhile(lambda part: part not in {";", "+"}, rest[at + 1 :]))
            name = run[0].rsplit("/", 1)[-1] if run else ""
            in_place = name in {"sed", "perl"} and any(
                part[:1] == "-" and part[:2] != "--" and "i" in part for part in run[1:]
            )
            if name in CHECKOUT_MAKERS | CHECKOUT_MODES | CHECKOUT_COPIERS or in_place:
                guard.write()
            elif name not in CHECKOUT_READERS | {"sed"}:
                guard.ask()


@reads_for("git")
def checkout_git(guard: CheckoutWrites, rest: list[str], positional: list[str], folder):
    """git: a command that changes a read-only checkout's work tree, index or repository
    (commit, checkout, stash, branch -d, config…) writes there; one git does not have (an
    alias), asked about; one that reads, only what it writes out (--output)."""
    places: list[str | None] = [folder]
    at = 0
    while at < len(rest) and rest[at].startswith("-"):
        option, _, value = rest[at].partition("=")
        if option in GIT_VALUED and not value:
            at += 1
            value = rest[at] if at < len(rest) else ""
        if option == "-C":
            places[0] = guard.moved(places[0], value)
        elif option in {"--work-tree", "--git-dir"}:
            places.append(guard.moved(folder, value))
        at += 1
    command, words = (rest[at], rest[at + 1 :]) if at < len(rest) else ("", [])
    plain = [word for word in words if not word.startswith("-")]
    options = {word.split("=")[0] for word in words if word.startswith("-")}
    for word in words:
        if word.startswith("--output="):
            guard.writes(word.partition("=")[2], places[0])
    if command == "clone":
        made = plain[0].rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") if plain else ""
        guard.writes(plain[1] if len(plain) > 1 else made or ".", places[0])
        return
    if command == "init":
        guard.writes(plain[0] if plain else ".", places[0])
        return
    if command in {"archive", "bundle", "format-patch"}:
        for before, word in itertools.pairwise(["", *words]):
            if before in {"-o", "--output", "--output-directory"}:
                guard.writes(word, places[0])
        if command == "bundle" and plain[:1] == ["create"] and len(plain) > 1:
            guard.writes(plain[1], places[0])
        if command == "format-patch" and not options & {"-o", "--output-directory", "--stdout"}:
            guard.runs_in(places[0])
        return
    if not command or not any(guard.there(place) for place in places):
        return
    if command in GIT_READS_ONLY:
        return
    if command in GIT_LISTING:
        bare = command in GIT_BARE_LISTS and not plain and options <= GIT_LIST_OPTIONS
        if bare or (plain[:1] and plain[0] in GIT_LISTING[command]):
            return
        if command == "tag" and options & {"-l", "--list"}:
            return
    reads_config = (
        bool(options & CONFIG_READS)
        or plain[:1] in (["get"], ["list"])
        or (not options & CONFIG_CHANGES and len(plain) < 2 and plain[:1] != ["set"])
    )
    if command == "config" and reads_config and not config_change(["config", *words])[0]:
        return
    checks = options & {"--check", "--stat", "--numstat", "--summary"}
    applies = options & {"--index", "--cached", "--3way", "-3", "--apply"}
    if command == "apply" and checks and not applies:
        return
    if command == "clean" and options & {"-n", "--dry-run"}:
        return
    if command in GIT_CHANGES:
        guard.write()
    else:
        guard.ask()  # (An alias, or a command of its own: what it does is not read here.)


# On Windows a worker's shell is PowerShell (Copilot's; Codex wraps its commands in it): its lines
# are read as PowerShell and cmd read them (winshell.py).
WINDOWS_SHELLS = sys.platform == "win32"


def checkout_guard(text: str, kept: tuple[str, ...]) -> CheckoutWrites:
    """The reading of a line in the read-only checkouts `kept`: this system's shell's."""
    if WINDOWS_SHELLS:
        from .winshell import WindowsCheckout

        return WindowsCheckout(text, kept)
    return CheckoutWrites(text, kept)


def checkout_write(
    command: str, cwd: str, kept: tuple[str, ...], shell: str = "", works_there: bool = True
) -> str:
    """Whether a shell command line, run in `cwd`, writes in one of the owner's checkouts a
    worker was given only to read (`kept`): CHECKOUT_WRITES when it does, as far as can be
    read (a redirection or a program writing a file there; git changing its work tree, index
    or repository; an install, a build or a test run there); CHECKOUT_ASKS for a program not
    known run there on files there, or a write where it can't be read in a line that runs in
    one or names one; "" otherwise. Read as the shell reads it: `cd` in the line, subshells,
    `sh -c`, `eval`, substitutions, a shell's script on its input (MOST_NESTED deep). A line
    past what is read here, run in one or naming one: CHECKOUT_ASKS. On Windows, as PowerShell
    reads it (cmd, for one `shell` says cmd runs): winshell.py. `works_there`: see
    CheckoutWrites (False: the checkouts its own worktrees were made from)."""
    return checkout_reading(command, cwd, kept, shell, works_there).found


def checkout_reading(
    command: str, cwd: str, kept: tuple[str, ...], shell: str = "", works_there: bool = True
) -> CheckoutWrites:
    """checkout_write's reading of a line, to its end: `found`, `wrote` and `ran` (a test run
    there may come after a redirection there)."""
    if not kept:
        return CheckoutWrites("", ())
    if WINDOWS_SHELLS:
        from .winshell import windows_checkout_reading

        return windows_checkout_reading(command, cwd, kept, shell, works_there)
    guard = CheckoutWrites(command, kept)
    guard.works_there = works_there
    pending: list[tuple[str, str | None, int]] = [(command, cwd or None, 0)]
    try:
        while pending:
            text, folder, depth = pending.pop()
            if depth > MOST_NESTED:
                raise TooComplexError
            pending += [(line, where, depth + 1) for line, where in guard.line(text, folder)]
    except (ValueError, TooComplexError, IndexError):
        if guard.near(cwd or None):
            guard.ask()
    return guard


def owners_checkouts(row: dict, checkouts: list[dict]) -> tuple[list[str], dict[str, str]]:
    """The owner's git checkouts a worker never writes: those it was given only to read (a
    task that changes no code, see Workspaces.writes), and those its own worktrees were made
    from, each with that worktree (`behind`: live r77 F1, a worker in its own worktree, acting
    on its own, edited AGENTS.md in the owner's checkout it was made from). Not one it was
    given to change in place, nor one a folder of its own is in."""
    kept = [
        c["path"]
        for c in checkouts
        if c.get("shared") and c.get("git_dir") and not c.get("in_place")
    ]
    own = [row["cwd"], *(c.get("path") for c in checkouts)]
    own = tuple(folder for folder in own if folder and folder not in kept)
    behind: dict[str, str] = {}
    for checkout in checkouts:
        repo = checkout.get("repo") or ""
        if checkout.get("shared") or checkout.get("remote") or not repo or repo in behind:
            continue  # (A clone of its own, or one it made in its folder: its own.)
        if not holds_any(repo, own):
            behind[repo] = checkout.get("worktree") or checkout["path"]
    return kept, behind


@functools.lru_cache(maxsize=1024)
def holds_any(repo: str, folders: tuple[str, ...]) -> bool:
    """Whether one of these folders is in `repo`, as their real paths were when first asked
    (it is asked at each tool call; a link made later in one of them changes nothing)."""
    return any(within(folder, repo) for folder in folders)


def owners_checkout(row: dict, request: Any, checkouts: list[dict]) -> str | None:
    """Why a worker may not write this file, or run this command line: it is in (it writes in)
    a git checkout of the owner's it was given only to read (a task that changes no code, see
    Workspaces.writes), or one its own worktree was made from; a program not known, run there
    on its files, asks first. Also when it acts without permission checks (yolo), and with no
    system sandbox (Codex always; Copilot with the worker sandbox off), whichever agent it
    runs on: the owner's checkouts are never written (owners_checkouts)."""
    kind = getattr(request, "kind", "")
    if row["role"] != WORKER or kind not in {"write", "shell"}:
        return None
    kept, behind = owners_checkouts(row, checkouts)
    if not kept and not behind:
        return None
    if kind == "shell":
        command = getattr(request, "full_command_text", "") or ""
        folder = (
            getattr(request, "resolved_working_directory", None)
            or getattr(request, "cwd", None)
            or row["cwd"]
        )
        shell = str(getattr(request, "shell", "") or "")
        found = checkout_write(command, str(folder or ""), tuple(kept), shell)
        if found != CHECKOUT_WRITES and behind:
            beside = checkout_write(command, str(folder or ""), tuple(behind), shell, False)
            found = beside if beside == CHECKOUT_WRITES else found or beside
        if found == CHECKOUT_WRITES:
            return OWNERS_CHECKOUT
        return OWNERS_CHECKOUT_ASK if found == CHECKOUT_ASKS else None
    target = getattr(request, "resolved_path", None) or getattr(request, "file_name", "")
    if not Path(target).is_absolute():
        target = str(Path(row["cwd"]) / target)
    if sys.platform == "win32":
        from .winshell import plain_path

        target = plain_path(target)  # (`\\?\C:\…` is C:\…; `app.` is `app`.)
    if any(within(target, folder) for folder in kept):
        return OWNERS_CHECKOUT
    for repo, tree in behind.items():
        if within(target, repo):
            return OWNERS_CHECKOUT_TREE.format(path=tree)
    return None


def checkout_of(command: str, folder: str, kept: list[dict]) -> dict | None:
    """Which of the owner's checkouts given only to read (`kept`) a command line run in `folder`,
    refused by owners_checkout, is about: the one it names, when it names one only; else the one
    it runs in; else the first."""
    named = [c for c in kept if checkout_guard(command, (c["path"],)).mentioned]
    if len(named) == 1:
        return named[0]
    for checkout in named or kept:
        if folder and checkout_guard("", (checkout["path"],)).inside(folder, real=True):
            return checkout
    return (named or kept or [None])[0]


def permission_denial(row: dict, request: Any, writable: list[str] | None = None) -> str | None:
    """Why a session may not do this, or None when it is allowed."""
    kind = getattr(request, "kind", "")
    role = row["role"]
    reads_slack = role == SLACK or bool(row["slack"])
    if kind == "mcp":
        server = getattr(request, "server_name", "")
        read_only = getattr(request, "read_only", False)
        if server == "slack" and not read_only:
            return "Slack writes are not available. Use draft_slack_reply; the owner approves it."
        if server == GITHUB_MCP and not read_only:
            return "Publishing to GitHub as the owner goes through draft_github_review."
        if role != WORKER and server != "slack":
            return f"The {role} session may only use Slack read tools."
        return None
    if kind == "custom-tool":
        return None
    if kind == "memory":
        # Sessions that read untrusted text must not turn it into instructions for future ones.
        if role != WORKER or reads_slack:
            return "Only workers that do not read Slack may store memories."
        return None
    if role != WORKER:
        return f"The {role} session cannot use {kind} tools. Delegate work to a worker."
    if kind == "url" and reads_slack:
        return (
            "Workers with Slack access cannot fetch URLs, to protect Slack data. "
            'Ask with report(status="question") if you need the web.'
        )
    cwd = row["cwd"]
    if kind == "write":
        target = getattr(request, "resolved_path", None) or getattr(request, "file_name", "")
        if not Path(target).is_absolute():
            target = str(Path(cwd) / target)
        folders = [*(writable or [cwd]), tempfile.gettempdir()]
        if not any(within(target, folder) for folder in folders if folder):
            return (
                f"Writing outside your working directory ({cwd}) and checkouts needs the owner. "
                'Ask with report(status="question"), or add_repo for another repository.'
            )
    if kind == "shell":
        command = getattr(request, "full_command_text", "") or ""
        try:
            reason = shell_denial(command, cwd, reads_slack, request)
        except Exception as exc:  # Any failure to check refuses, saying so.
            reason = f"{NOT_CHECKED} ({type(exc).__name__})"
        if reason:
            return (
                f"Denied: {reason}. If the task really needs it, ask the owner with "
                'report(status="question").'
            )
    return None


def shell_denial(command: str, cwd: str, reads_slack: bool, request: Any) -> str | None:
    """Why a worker may not run this command line on its own: the first reason that holds (the
    checks after it are not run); a line too long to check is refused before any check reads
    it."""
    if len(command) > MOST_LINE:
        return TOO_LONG
    answers, settings, checked = ANSWERS.set({}), SETTINGS.set({}), CHECKED.set({})
    shell = SHELL.set(str(getattr(request, "shell", "") or ""))
    try:
        first = next((reason for found, reason in DANGEROUS if found(command, cwd)), None)
    finally:
        SHELL.reset(shell)
        CHECKED.reset(checked)
        SETTINGS.reset(settings)
        ANSWERS.reset(answers)
    if first:
        return first
    if gh_api_writes(command):
        return (
            "writing to GitHub through gh api (with -f/-F fields gh sends a POST: to read, add "
            "-X GET)"
        )
    if reads_slack and getattr(request, "possible_urls", None):
        return "network access from a worker that can read Slack"
    return None


# The floor (0.1.57): what no worker does as the owner on its own, with yolo on or off. Nothing
# goes out on GitHub in their name without their OK: no PR merged, reviewed, commented on or
# closed, no issue opened, changed, commented on or closed, no write through gh api or GitHub's
# MCP server, no force push to a branch that is not the worker's own, no push to the default
# branch. Its own branches it pushes to as it likes; it opens PRs and reads anything.
FLOOR_WHY = "Chief of Staff never does that as the owner unasked, whatever the worker's mode."
FLOOR_ASK = (
    f'{FLOOR_WHY} Ask the owner with report(status="question"), or hand them the text as a '
    "draft to approve (draft_github_review: a review, a reply or a comment)."
)
# Refused a write through GitHub's API or MCP server, or a push to the default branch, a
# worker is told what it may do itself: open a PR from a branch of its own (live, 0.1.57: told
# only to ask, a worker refused the API gave up on a PR it could have opened).
FLOOR_ROUTE = (
    f"{FLOOR_WHY} To change code and open a PR, do it yourself: in your checkout (or a clone "
    "of the repository in your own folder), commit on a new branch of your own, push it "
    "(`git push -u origin <branch>`), then open the PR with `gh pr create`. Only comments, "
    'reviews, merges and issue changes need the owner: ask them with report(status="question"), '
    "or hand them the text as a draft to approve (draft_github_review: a review, a reply or a "
    "comment)."
)
GH_API_WRITE = (
    "writing to GitHub through gh api (with -f/-F fields gh sends a POST: to read, add -X GET)"
)
DEFAULT_PUSH = "a push to the default branch"
REWRITE = (
    "deleting or rewriting a branch that is not one of yours (a deletion, `--prune`, `--mirror` "
    "or push options)"
)
GH_PR_SPEAKS = re.compile(r"\bgh\s+pr\s+(merge|review|comment|close)\b")
GH_ISSUE_CHANGES = re.compile(r"\bgh\s+issue\s+(create|new|edit|comment|close)\b")
# Only a line that names git or gh is read further: the rest costs one search.
FLOOR_WORDS = re.compile(r"\b(git|gh)\b")
GH_WORD = re.compile(r"\bgh\b")
GITHUB_NAMED = re.compile(r"github", re.IGNORECASE)


def github_server(name: str, config: Any) -> bool:
    """Whether an MCP server is GitHub's: by its name, its command and arguments, or its URL."""
    if GITHUB_NAMED.search(name):
        return True
    if not isinstance(config, dict):
        return False
    parts = [config.get("command"), config.get("url"), *(config.get("args") or [])]
    return any(isinstance(part, str) and GITHUB_NAMED.search(part) for part in parts)


# gh's commands a worker may run: those that only read (or work on this machine only: a clone,
# a checkout, a download), by command group; None: any of the group's. Any other is refused, a
# new command of gh's too, as are an extension's and an alias's (an allowlist, not a list of
# writes). `gh pr create` opens a PR from a branch of the worker's own (gh_pr_create); `gh api`
# reads only (gh_api_refusal).
GH_READS: dict[str, frozenset[str] | None] = {
    "search": None, "browse": None, "help": None, "completion": None, "status": frozenset(),
    "version": frozenset(),
    "pr": frozenset({"view", "list", "status", "checks", "diff", "checkout"}),
    "issue": frozenset({"view", "list", "status"}),
    "run": frozenset({"view", "list", "watch", "download"}),
    "workflow": frozenset({"view", "list"}),
    "repo": frozenset({"view", "list", "clone", "set-default"}),
    "release": frozenset({"view", "list", "download", "verify", "verify-asset"}),
    "secret": frozenset({"list"}), "variable": frozenset({"list", "get"}),
    "label": frozenset({"list"}), "ruleset": frozenset({"list", "view", "check"}),
    "gist": frozenset({"view", "list", "clone"}), "cache": frozenset({"list"}),
    "auth": frozenset({"status"}), "config": frozenset({"get", "list"}),
    "alias": frozenset({"list"}), "extension": frozenset({"list", "search", "browse"}),
    "codespace": frozenset({"list", "view"}), "org": frozenset({"list"}),
    "project": frozenset({"view", "list", "item-list", "field-list"}),
    "attestation": frozenset({"verify", "download", "trusted-root"}),
    "agent-task": frozenset({"list", "view"}), "gpg-key": frozenset({"list"}),
    "ssh-key": frozenset({"list"}),
}  # fmt: skip
GH_NESTED_READS = {
    ("repo", "deploy-key"): frozenset({"list"}),
    ("repo", "autolink"): frozenset({"list", "view"}),
    ("repo", "gitignore"): frozenset({"list", "view"}),
    ("repo", "license"): frozenset({"list", "view"}),
}
# gh's own command groups: a word `gh` followed by one, wherever it stands in a command (`uv run
# gh …`, `find … -exec gh …`), runs gh; one of the programs that only print or search text
# (`echo`, `grep`) does not.
GH_GROUPS = frozenset({*GH_READS, "api", "copilot", "preview", "accessibility"})
GH_TEXT = frozenset({
    "echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack", "cat", "head", "tail", "less",
    "more", "wc", "sort", "uniq", "cut", "diff", "ls", "stat", "which", "whereis", "type", "man",
    "tldr", "basename", "dirname", "realpath", "readlink", "brew", "apt", "apt-get", "jq", "yq",
    "git",
})  # fmt: skip
# gh's options that take no value: before its command they take none from the words after
# them (any other does, as cobra reads them).
GH_FLAGS = frozenset({"-h", "--help", "--version"})
GH_HELP = frozenset({"-h", "--help"})
GH_HEAD = frozenset({"-H", "--head"})
GH_FOREIGN_HEAD = "a pull request from a branch that is not one of yours"
GH_UNCHECKED = (
    "a line that runs gh in a way that can't be checked (run gh's command directly, any "
    "variable in it quoted)"
)
# `gh api`'s options that take a value (the rest take none).
GH_API_VALUED = frozenset({"-X", "--method", "-f", "--raw-field", "-F", "--field", "--input",
                           "-H", "--header", "-q", "--jq", "-t", "--template", "--hostname",
                           "-p", "--preview", "--cache"})  # fmt: skip
GH_API_FIELDS = frozenset({"-f", "--raw-field", "-F", "--field"})
GH_API_SWITCHES = "i"  # (`-i`, --include: its one switch of a letter.)
# A GraphQL query read plainly: it starts as one (a mutation in it would be another operation,
# which runs only as operationName names it); none of the shell's substitutions in it.
GH_QUERY_START = re.compile(r"\s*(?:query\b|\{)")
GH_QUERY_SUBSTITUTED = re.compile(r"\$[({]|[`\x00]")
# git's options whose value is text, not a command (a message; what log searches for): not
# read for gh, nor are `git grep`'s words. The rest are (what `-c`, `rebase -x`, `submodule
# foreach` or `bisect run` give git to run).
GIT_TEXT_LONG = frozenset(
    {"--message", "--grep", "--author", "--committer", "--format", "--pretty"}
)
GIT_TEXT_SHORT = "mSG"
# gh as code names it: past quotes, brackets and commas (GH_CODE_MARKS), up to where its
# command ends.
GH_CODE_MARKS = re.compile(r"[\"'\[\],{}]")
GH_IN_CODE = re.compile(r"(?<![\w.-])gh(?:\.exe)?\s+([^;&|()<>\n`]*)", re.IGNORECASE)
GH_NAMED = re.compile(r"\bgh\b", re.IGNORECASE)
GH_API_READS = frozenset({"GET", "HEAD"})


def gh_path(words: list[str]) -> tuple[list[str], list[str]]:
    """gh's command (its group, its command, and a nested group's command: GH_NESTED_READS), as
    cobra finds it past options (one met before the command is whole takes the word after it,
    unless it is one of GH_FLAGS or has `=`); and the words after it."""
    path: list[str] = []
    at = 0
    while at < len(words):
        word = words[at]
        at += 1
        if word == "--":
            break
        if word.startswith("-") and len(word) > 1:
            if "=" not in word and word not in GH_FLAGS:
                at += 1  # (`gh -R o/r pr view`, `gh pr -R o/r view`.)
            continue
        path.append(word)
        if path == ["api"] or len(path) == 3:  # (`gh api`'s endpoint: a word it is given.)
            break
        if len(path) == 2 and tuple(path) not in GH_NESTED_READS:
            break
    return path, words[at:]


def gh_command(words: list[str], own: frozenset[str]) -> str | None:
    """Why a worker may not run gh with these words (what follows `gh`), None when it may."""
    path, rest = gh_path(words)
    before = words[: len(words) - len(rest)]
    if any(UNSETTLED.search(word) for word in path) or any(
        isinstance(word, Splitting) for word in before
    ):
        return GH_UNCHECKED  # (Its command in a variable or a substitution, or maybe so.)
    if not path:
        return None  # gh's help, its version.
    group = path[0]
    if path[:2] in (["api"], ["pr", "create"]) and any(isinstance(w, Splitting) for w in rest):
        return GH_UNCHECKED  # (`gh api $A`: `-X DELETE` in it, maybe; `--head` in a PR's.)
    if group == "api":
        return gh_api_refusal(rest)
    if group not in GH_READS:
        return f"`gh {group}` is not one of gh's commands that only read"
    reads = GH_READS[group]
    if reads is None or len(path) == 1:
        return None  # (A group alone shows its help.)
    command = path[1]
    if group == "pr" and command == "create":
        return gh_pr_create(words, own)
    if (group, command) in GH_NESTED_READS:
        if len(path) == 2 or path[2] in GH_NESTED_READS[(group, command)]:
            return None
        command = f"{command} {path[2]}"
    elif command in reads:
        return None
    if rest and all(word in GH_HELP for word in rest):
        return None  # (Its help only.)
    return f"`gh {group} {command}` may write to GitHub as the owner (a worker's gh only reads)"


def gh_pr_create(words: list[str], own: frozenset[str]) -> str | None:
    """`gh pr create` opens a PR from the worker's current branch, or from the one `--head`
    names: only one of its own."""
    heads = []
    for at, word in enumerate(words):
        name, _, value = word.partition("=")
        if name in GH_HEAD and value:
            heads.append(value)
        elif word.startswith("-H") and len(word) > 2 and not word.startswith("--"):
            heads.append(word[2:])
        elif word in GH_HEAD and at + 1 < len(words):
            heads.append(words[at + 1])
    for head in heads:
        branch = head.rsplit(":", 1)[-1]
        if UNSETTLED.search(branch) or branch not in own:
            return GH_FOREIGN_HEAD
    return None


def gh_api_refusal(words: list[str]) -> str | None:
    """`gh api` reads only: a GET (or HEAD) it is told, or one it sends of itself (no field,
    no body); GraphQL, a query it is given in its words (no mutation, none read from a file or
    a variable). Options as gh reads them: `-XPOST`, `-fname=x`, `-if name=x`, `--field=x`."""
    method, endpoint, fields, body = "", "", [], False
    at = 0
    while at < len(words):
        word = words[at]
        at += 1
        if word == "--":
            endpoint = endpoint or (words[at] if at < len(words) else "")
            break
        if not word.startswith("-") or len(word) == 1:
            endpoint = endpoint or word
            continue
        if word.startswith("--"):
            name, equals, value = word.partition("=")
        else:
            letters = word[1:].lstrip(GH_API_SWITCHES)
            if not letters:
                continue
            name, value = f"-{letters[0]}", letters[1:].removeprefix("=")
            equals = "=" if letters[1:] else ""
        if name in GH_API_VALUED and not equals:
            value = words[at] if at < len(words) else ""
            at += 1
        if name in {"-X", "--method"}:
            method = value
        elif name in GH_API_FIELDS:
            fields.append(value)
        elif name == "--input":
            body = True
    if method:
        return None if method.upper() in GH_API_READS else GH_API_WRITE
    if endpoint.strip("/") == "graphql":
        named = any(field.partition("=")[0] == "operationName" for field in fields)
        for field in fields:  # (The query's variables can't make it a mutation.)
            key, given, value = field.partition("=")
            if not given or UNSETTLED.search(key):
                return GH_API_WRITE
            if key == "query" and (
                value.startswith("@")
                or "mutation" in value.lower()
                or (
                    UNSETTLED.search(value)  # ($name: a GraphQL variable, or the shell's.)
                    and (
                        named
                        or GH_QUERY_SUBSTITUTED.search(value)
                        or not GH_QUERY_START.match(value)
                    )
                )
            ):
                return GH_API_WRITE
        return GH_API_WRITE if body else None
    return GH_API_WRITE if fields or body else None


def gh_inside(words: list[str], own: frozenset[str]) -> str | None:
    """gh run by a command it follows in a simple command's words (`uv run gh …`, `find … -exec
    gh …`): a word `gh` followed by one of gh's groups; and gh in code a word gives a program
    (`python3 -c "os.system('gh …')"`, `node -e`, `perl -e`, awk's `system()`, a list of
    words: `["gh", "repo", …]`), as far as its text says it plainly."""
    for at, word in enumerate(words[1:-1], 1):
        runs = program_of(word) == "gh" and words[at + 1] in GH_GROUPS
        if runs and (reason := gh_command(words[at + 1 :], own)):
            return reason
    for word in words[1:]:
        if not GH_NAMED.search(word):
            continue
        for found in GH_IN_CODE.finditer(GH_CODE_MARKS.sub(" ", word)):
            said = found.group(1).split()
            path, _ = gh_path(said)
            if path and path[0] in GH_GROUPS and (reason := gh_command(said, own)):
                return reason
    return None


def git_code_words(words: list[str]) -> list[str]:
    """git's words that may hold a command it runs: all but its name, `git grep`'s, and the
    text of a message or of what log searches for (GIT_TEXT_LONG, GIT_TEXT_SHORT)."""
    kept: list[str] = []
    at, command = 1, None
    while at < len(words):
        word = words[at]
        at += 1
        if command is None and word in GIT_VALUED:
            kept += words[at : at + 1]  # (`-c core.pager=…`: its value.)
            at += 1
            continue
        if command is None and not word.startswith("-"):
            command = word
            if command == "grep":
                break
            continue
        if word.startswith("--"):
            name, equals, _ = word.partition("=")
            if name in GIT_TEXT_LONG:
                at += not equals
                continue
        elif word.startswith("-") and len(word) > 1:
            letters = word[1:]
            text = next((i for i, letter in enumerate(letters) if letter in GIT_TEXT_SHORT), None)
            if text is not None:
                at += text == len(letters) - 1  # (`-am <message>`; `-m<message>`.)
                continue
        kept.append(word)
    return kept


def gh_refusal(command: str, own: frozenset[str], depth: int = 0) -> str | None:
    """Why a line that names gh may not run (gh_command), read as the shell splits it: each
    simple command that runs gh (past `env`, `nice`, `xargs`…; one whose words run gh after
    them, as `uv run gh …`), and what it runs in turn (`sh -c`, `eval`, substitutions). A line
    that can't be read, or makes up the name of a command gh's words follow (`$G repo …`), or
    feeds a shell a script, is refused."""
    if depth > MOST_NESTED:
        return TOO_COMPLEX
    try:
        parsed = shell_commands(command)
    except TooComplexError:
        return TOO_COMPLEX
    if parsed is None:
        return GH_UNCHECKED
    commands, inside = parsed
    unquoted = squashed(command)
    for _, words in commands:
        rest, xargs = unwrapped(words)
        if not rest:
            continue
        name = program_of(rest[0])
        if name == "gh":
            if reason := gh_command(rest[1:], own):
                return reason
        elif UNSETTLED.search(rest[0]) and rest[1:2] and rest[1] in GH_GROUPS:
            return GH_UNCHECKED  # (`$G repo delete`, `$(echo gh) repo delete`.)
        elif name == "git" and (reason := gh_inside(["git", *git_code_words(rest)], own)):
            return reason  # (`git rebase -x 'gh …'`, `git -c core.pager='gh …' log`.)
        elif name not in GH_TEXT and (reason := gh_inside(rest, own)):
            return reason
        _, script = code_of(rest, False, xargs)
        for text in [*([] if script is None else [script]), *inner_scripts(words)]:
            if reason := gh_refusal(text, own, depth + 1):
                return reason
    for body in inside:
        if reason := gh_refusal(body, own, depth + 1):
            return reason
    if depth == 0 and (ALIASING.search(unquoted) or feeds_shell(command)):
        return GH_UNCHECKED  # (An alias it makes, a script it feeds a shell: not read here.)
    return None


# What the floor can't read (0.1.61), refused for every worker whatever the line names: a
# command whose name the shell makes as it runs (`g${X}it`, `$'\x67'it`, `{git,}`, a glob), gh or
# git copied, linked, or found under another name (by its file), a command on a PATH the line
# makes that can't be read, a shell fed code (a pipe, a heredoc, a process substitution), and
# git configuration that runs a command (hooks, a `!` alias, a pager, an ssh command…).
UNREAD_NAME = (
    "a command whose name the shell makes as it runs (an expansion, a quote or a glob in it): "
    "name the program plainly"
)
GH_COPIED = "copying or linking gh or git (run them by their own names)"
GH_RENAMED = "git under another name (run it as git)"
UNREAD_PATH = "a command on a PATH the line makes, which the floor can't read (run it by its path)"
FED_UNREAD = (
    "a shell fed code the floor can't read (from a pipe, a heredoc or a substitution): run the "
    "commands themselves, or a script file"
)
GIT_RUNS = "git configuration that runs a command ({key}): what it runs can't be read"
# What ends a case's branch: the next words are its next patterns.
CASE_ENDS = frozenset({";;", ";&", ";;&"})
# A command's name that is a variable's value only (`$PY`, `${PY}`).
VARIABLE_NAME = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
# A name's part the shell makes into another word: a brace expansion, a glob.
MADE_BRACES = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")
MADE_GLOB = re.compile(r"[*?]|\[.+\]")
# The word after one of these is a command a program runs (`uv run X`, `find … -exec X`).
RUN_WORDS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "run", "exec", "x", "dlx"})
RUN_FIRST = frozenset({"npx", "bunx", "pnpx"})
# Words after which a name made up is git's or gh's, wherever it stands (`… $G push`).
COMMAND_WORDS = GH_GROUPS | {"push", "send-pack", "subtree", "update-ref", "config", "-c"}
# Programs that only print or search text, and those that only show what they are piped.
TEXT_ONLY = frozenset({"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack"})
TEXT_SINKS = frozenset({
    "head", "tail", "less", "more", "wc", "sort", "uniq", "cut", "grep", "egrep", "fgrep", "rg",
    "cat", "jq", "column", "fold", "fmt", "nl",
})  # fmt: skip
# Programs that copy or link a file, and what looks up where gh or git is.
TOOL_COPIERS = frozenset({"cp", "ln", "install", "mv", "rsync", "ditto", "scp", "dd", "tar", "cpio",
                     "pax", "cat", "gcp", "gln", "ginstall", "gmv"})  # fmt: skip
GH_LOOKUP = re.compile(
    r"(?:\bcommand\s+-[vV]|\bwhich|\btype\s+-[a-zA-Z]+|\bwhereis|\bwhere|\brealpath|\breadlink"
    r"(?:\s+-\w+)?|\bbrew\s+--prefix)\s+(?:-\S+\s+)*(?:gh|git)\b"
)
# A shell's names, for a first look before feeds_shell reads a line.
FED_NAMED = re.compile(
    r"\b(?:" + "|".join(sorted(FED_SHELLS)) + r"|source)\b|<\(|=\(|/dev/(?:stdin|fd)"
)
# bash's builtins and keywords: never looked up on the PATH.
BUILTINS = frozenset({
    ".", ":", "[", "[[", "alias", "bg", "bind", "break", "builtin", "caller", "cd", "command",
    "compgen", "complete", "continue", "declare", "dirs", "disown", "echo", "enable", "eval",
    "exec", "exit", "export", "false", "fc", "fg", "getopts", "hash", "help", "history", "jobs",
    "kill", "let", "local", "logout", "popd", "printf", "pushd", "pwd", "read", "readonly",
    "return", "set", "shift", "shopt", "source", "suspend", "test", "times", "trap", "true",
    "type", "typeset", "ulimit", "umask", "unalias", "unset", "wait", *KEYWORDS,
})  # fmt: skip
# Where git runs a command, or code of a file, from its configuration or its variables.
GIT_RUN_KEYS = re.compile(
    r"(?:core\.(?:pager|editor|sshcommand|askpass|gitproxy|fsmonitor)|pager\..+|"
    r"sequence\.editor|diff\.external|diff\..+\.(?:command|textconv)|"
    r"(?:diff|merge)tool\..+\.(?:cmd|path)|merge\..+\.driver|filter\..+\.(?:clean|smudge|"
    r"process)|credential\.(?:.+\.)?helper|gpg\.(?:.+\.)?program|sendemail\.(?:tocmd|cccmd|"
    r"smtpserver)|browser\..+\.(?:cmd|path)|web\.browser|man\..+\.(?:cmd|path)|"
    r"remote\..+\.(?:uploadpack|receivepack)|uploadpack\.packobjectshook|alias\..+|"
    r"submodule\..+\.update)"
)
GIT_FILE_KEYS = re.compile(
    r"core\.hookspath|include\.path|includeif\..+\.path|init\.templatedir|protocol\.(?:ext\.)?allow"
)
GIT_RUN_VARIABLES = frozenset({"GIT_SSH", "GIT_SSH_COMMAND", "GIT_PAGER", "PAGER", "GIT_EDITOR",
                               "EDITOR", "VISUAL", "GIT_SEQUENCE_EDITOR", "GIT_EXTERNAL_DIFF",
                               "GIT_ASKPASS", "SSH_ASKPASS", "GIT_PROXY_COMMAND"})  # fmt: skip
GIT_FILE_VARIABLES = frozenset({"GIT_EXEC_PATH", "GIT_TEMPLATE_DIR", "GIT_CONFIG_GLOBAL",
                                "GIT_CONFIG_SYSTEM", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
                                "GIT_CONFIG_COUNT"})  # fmt: skip
# What git may run of its configuration: one of these programs, given plain words.
GIT_PLAIN_RUNS = frozenset({"cat", "less", "more", "most", "head", "tail", "true", "false", ":",
                            "vi", "vim", "nvim", "nano", "emacs", "code", "subl", "ssh", "delta",
                            "diff-so-fancy", "bat", "sed", "ed"})  # fmt: skip
GIT_HELPERS = frozenset({"", "store", "cache", "osxkeychain", "manager", "manager-core",
                         "libsecret", "wincred"})  # fmt: skip
GIT_CONFIG_VALUED = frozenset({"-f", "--file", "--blob", "--type", "-t", "--default",
                               "--comment", "--value"})  # fmt: skip
GIT_CONFIG_READS = frozenset({"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list",
                              "-l", "--unset", "--unset-all", "--rename-section",
                              "--remove-section", "--get-color", "--get-colorbool"})  # fmt: skip
GIT_CONFIG_COMMANDS = frozenset({"list", "get", "unset", "rename-section", "remove-section"})


def made_up(name: str) -> bool:
    """Whether the shell makes a command's name (past its folder: `$VENV/bin/python` is read)
    out of what it is given: a variable, a substitution, `$'…'`, a brace expansion, a glob."""
    base = name.rsplit("/", 1)[-1]
    if base in {"[", "[["}:
        return False
    return bool(UNSETTLED.search(base) or MADE_BRACES.search(base) or MADE_GLOB.search(base))


@functools.lru_cache(maxsize=64)
def line_commands(command: str) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Each simple command a line runs, in order: its words, and what it runs past `env`,
    `nice`, `xargs`… (unwrapped); and those it runs in turn (`sh -c`, `eval`, `$(…)`),
    MOST_NESTED deep (TooComplexError past that). A part that does not parse (an unclosed
    quote: the shell runs none of it) has none."""
    found: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def walk(text: str, depth: int) -> None:
        if depth > MOST_NESTED:
            raise TooComplexError
        parsed = shell_commands(marked(text))
        if parsed is None:
            return
        commands, inside = parsed
        patterns = False
        for joiner, words in commands:
            # (A case's patterns, `;; *)`, `a|b)`: what the commands after `)` run is read.)
            patterns = joiner in CASE_ENDS or (patterns and joiner == "|")
            if patterns:
                continue
            patterns = words[:1] == ["case"]
            rest, xargs = unwrapped(words)
            found.append((tuple(words), tuple(rest)))
            _, script = code_of(rest, False, xargs)
            for inner in [*([] if script is None else [script]), *inner_scripts(words)]:
                walk(inner, depth + 1)
        for body in inside:
            walk(body, depth + 1)

    walk(command, 0)
    return tuple(found)


def names_unread(commands: Iterable[tuple[tuple[str, ...], tuple[str, ...]]]) -> bool:
    """Whether a command's name, or a command a program runs (`uv run X`, `find -exec X`, a
    word git's or gh's command follows), is one the shell makes (made_up); but a variable the
    line set before to a plain name, not git's or gh's (`PY=.venv/bin/python; $PY -m x`)."""
    known: dict[str, str] = {}
    for words, rest in commands:
        if not rest or program_of(rest[0]) in {"export", "declare", "typeset", "readonly"}:
            known |= assigned(words if not rest else rest[1:])
            continue
        if made_up(rest[0]):
            named = VARIABLE_NAME.fullmatch(rest[0])
            value = known.get(named.group(1), "") if named else ""
            if not value or made_up(value) or program_of(value) in {"gh", "git"}:
                return True
        program = program_of(rest[0])
        if program in TEXT_ONLY or program in {"gh", "git"}:
            continue  # (gh's and git's own words are read as theirs.)
        for at, word in enumerate(rest[1:], 1):
            before = rest[at - 1]
            runs = before in RUN_WORDS or (at == 1 and program in RUN_FIRST)
            follows = at + 1 < len(rest) and rest[at + 1] in COMMAND_WORDS and before[:1] != "-"
            if (runs or follows) and made_up(word):
                return True
    return False


def path_folders(value: str, cwd: str) -> list[str] | None:
    """The folders a PATH a line gives holds, as the shell reads it there ($PATH, $HOME, $PWD
    and `~` put in); None when another variable or a substitution is in it."""
    current = os.environ.get("PATH", "")
    home = str(Path.home())
    for name, put in (("PATH", current), ("HOME", home), ("PWD", cwd)):
        value = value.replace("${" + name + "}", put).replace("$" + name, put)
    if UNSETTLED.search(value):
        return None
    folders = []
    for folder in value.split(os.pathsep):
        folder = home + folder[1:] if folder.startswith("~") else folder
        folders.append(str(Path(cwd) / folder) if folder else cwd)
    return folders


def assigned(words: Iterable[str]) -> dict[str, str]:
    """The variables assignments among these words set (`A=b`, `env A=b`, `export A=b`)."""
    found = {}
    for word in words:
        if isinstance(word, Assignment) or ASSIGNMENT.match(word):
            name, _, value = word.partition("=")
            found[name.rstrip("+")] = value
    return found


@functools.lru_cache(maxsize=4)
def installed_tools(path: str) -> tuple[tuple[str, str, tuple[int, int], int], ...]:
    """gh and git as a PATH finds them: each one's name, real file, file id and size. Not one
    another name in its folder runs too (macOS's /usr/bin/git: one stub of Xcode's tools for
    git, python3, make…, which runs the tool its name says: under another, not git)."""
    found = []
    for folder in path.split(os.pathsep):
        for tool in ("gh", "git"):
            place = Path(folder or ".") / tool
            try:
                status = place.stat()
            except OSError:
                continue
            if stat.S_ISREG(status.st_mode) and not shared_stub(place, tool, status):
                identity = (status.st_dev, status.st_ino)
                found.append((tool, os.path.realpath(place), identity, status.st_size))
    return tuple(found)


def shared_stub(place: Path, tool: str, status: os.stat_result) -> bool:
    """Whether another name in a tool's folder (not one of its own, `git-…`) is the same file,
    or a copy of it."""
    identity = (status.st_dev, status.st_ino)
    try:
        siblings = list(place.parent.iterdir())
    except OSError:
        return False
    for sibling in siblings:
        if sibling.name == tool or sibling.name.startswith(f"{tool}-"):
            continue
        try:
            other = sibling.stat()
        except OSError:
            continue
        if (other.st_dev, other.st_ino) == identity:
            return True
        if stat.S_ISREG(other.st_mode) and other.st_size == status.st_size:
            mine = file_digest(str(place), identity, status.st_size, status.st_mtime_ns)
            theirs = (other.st_dev, other.st_ino)
            if mine == file_digest(str(sibling), theirs, other.st_size, other.st_mtime_ns):
                return True
    return False


@functools.lru_cache(maxsize=16)
def file_digest(place: str, identity: tuple[int, int], size: int, changed: int) -> str:
    """A file's content's sha256 (once per file as it is: its id, size and change time)."""
    digest = hashlib.sha256()
    with Path(place).open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tool_of(place: str) -> str | None:
    """gh or git, when a file is the one a PATH finds (by its real file, its file id, or a copy
    of it); None otherwise."""
    try:
        status = Path(place).stat()
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode):
        return None
    real, identity = os.path.realpath(place), (status.st_dev, status.st_ino)
    for tool, tool_real, tool_identity, size in installed_tools(os.environ.get("PATH", "")):
        if real == tool_real or identity == tool_identity:
            return tool
        if size == status.st_size and size:
            changed = Path(tool_real).stat().st_mtime_ns
            mine = file_digest(real, identity, size, status.st_mtime_ns)
            if mine == file_digest(tool_real, tool_identity, size, changed):
                return tool
    return None


def found_as(name: str, cwd: str, folders: list[str] | None) -> str | None:
    """Where the shell finds a command's name: its path (from `cwd`), or a folder of the PATH
    (`folders`: the line's; else this process's)."""
    if "/" in name:
        return str(Path(cwd) / Path(name).expanduser())
    path = os.pathsep.join(folders) if folders is not None else os.environ.get("PATH", "")
    return shutil.which(name, path=path)


def gh_copied(commands: Iterable[tuple[tuple[str, ...], tuple[str, ...]]], line: str,
              cwd: str) -> bool:  # fmt: skip
    """Whether a line copies or links gh or git (a path that is theirs, or what a lookup of
    them makes: `cp "$(command -v gh)" ./g`)."""
    looked_up = bool(GH_LOOKUP.search(line))
    for _, rest in commands:
        if not rest or program_of(rest[0]) not in TOOL_COPIERS:
            continue
        for word in rest[1:]:
            if word.startswith("-") and len(word) > 1:
                continue
            word = word.partition("=")[2] if word[:3] in {"if=", "of="} else word
            if UNSETTLED.search(word):
                if looked_up or program_of(word) in {"gh", "git"}:
                    return True
                continue
            if program_of(word) in {"gh", "git"} and word.startswith(("/", "~")):
                return True
            if tool_of(str(Path(cwd) / Path(word).expanduser())):
                return True
    return False


def renamed_or_unfound(
    commands: Iterable[tuple[tuple[str, ...], tuple[str, ...]]], cwd: str, own: frozenset[str]
) -> str | None:
    """gh or git found under another name (a link, a copy: tool_of), read as such (gh's words
    by gh_command; git's refused); and a command on a PATH the line gives that the floor can't
    read (a variable in it), or that isn't found there in a folder of the line's own."""
    folders: list[str] | None = None
    for words, rest in commands:
        given = assigned(words[: len(words) - len(rest)])
        if not rest or program_of(rest[0]) in {"export", "declare", "typeset", "readonly"}:
            given |= assigned(rest[1:])
            if "PATH" in given:
                folders = path_folders(given["PATH"], cwd)
                if folders is None:
                    return UNREAD_PATH
            continue
        here = folders
        if "PATH" in given:
            here = path_folders(given["PATH"], cwd)
            if here is None:
                return UNREAD_PATH
        name = rest[0]
        if made_up(name) or (name in BUILTINS and "/" not in name):
            continue
        if program_of(name) in {"gh", "git"}:
            continue  # (Read as gh and git are.)
        place = found_as(name, cwd, here)
        if place is None:
            system = os.environ.get("PATH", "").split(os.pathsep)
            if here is not None and any(folder not in system for folder in here):
                return UNREAD_PATH
            continue
        tool = tool_of(place)
        if tool == "gh" and (reason := gh_command(list(rest[1:]), own)):
            return reason
        if tool == "git":
            return GH_RENAMED
    return None


def fed_scripts(line: str) -> list[str] | None:
    """What a line feeds a shell as code (feeds_shell): [] when nothing, or only a plain file
    (`cat setup.sh | sh`, as `sh setup.sh` is: what a file holds is not read here); a heredoc's
    body when that is all it is fed (`bash <<'EOF'`, `cat <<'EOF' | sh`: read as the lines the
    shell runs); None when anything else (a pipe from another program, a substitution…)."""
    if not FED_NAMED.search(squashed(line)) or not feeds_shell(line):
        return []
    parsed = shell_commands(line)
    _, bodies = heredocs(line)
    if parsed is None or parsed[1] or len(parsed[0]) > 2:
        return None
    first = parsed[0][0][1]
    joiner, second = parsed[0][1] if len(parsed[0]) == 2 else ("", [])
    piped = joiner == "|" and first[:1] == ["cat"]
    shell = second if piped else first
    if not shell or program_of(shell[0]) not in FED_SHELLS or (len(parsed[0]) == 2 and not piped):
        return None
    if not all(word[:1] == "-" and "c" not in word for word in shell[1:]):
        return None  # (Only its options, none `-c`: its code is what it is fed.)
    files = first[1:] if piped else []
    if not bodies and files and all(PLAIN_NAME.fullmatch(word) for word in files):
        return [] if not any(word[:1] == "-" or FED_DEVICE.match(word) for word in files) else None
    return [bodies[0][0]] if len(bodies) == 1 and not files else None


def git_setting(key: str, value: str | None) -> str | None:
    """Why a worker may not set this git configuration: one that runs a command (GIT_RUN_KEYS)
    but one of GIT_PLAIN_RUNS given plain words (harmless_run), a `!` alias, or code from a file
    (GIT_FILE_KEYS); None when it may. `value` None: not known (`--config-env`)."""
    name = key.lower()
    if UNSETTLED.search(name):
        return GIT_RUNS.format(key=key)
    if GIT_FILE_KEYS.fullmatch(name):
        if (value or "").strip() in {"/dev/null", "never"}:
            return None
        return GIT_RUNS.format(key=key)
    if not GIT_RUN_KEYS.fullmatch(name):
        return None
    if value is None:
        return GIT_RUNS.format(key=key)
    plain = value.strip()
    if name.startswith(("alias.", "submodule.")):
        return GIT_RUNS.format(key=key) if plain.startswith("!") else None
    if name.startswith("credential."):
        return None if plain.split(" ", 1)[0] in GIT_HELPERS else GIT_RUNS.format(key=key)
    if (name == "core.fsmonitor" or name.startswith("pager.")) and plain.lower() in TRUE | FALSE:
        return None
    return None if harmless_run(plain) else GIT_RUNS.format(key=key)


def harmless_run(value: str) -> bool:
    """A command git may run of its configuration: one of GIT_PLAIN_RUNS, its words plain (no
    variable, substitution, separator or redirection; naming neither git nor gh)."""
    if UNSETTLED.search(value) or any(mark in value for mark in ";&|<>\n"):
        return False
    try:
        words = shell_words(value)
    except (ValueError, TooComplexError):
        return False
    if not words or GH_NAMED.search(value) or re.search(r"\bgit\b", value, re.IGNORECASE):
        return False
    return program_of(words[0]) in GIT_PLAIN_RUNS


def git_runs(commands: Iterable[tuple[tuple[str, ...], tuple[str, ...]]]) -> str | None:
    """Git configuration a line sets that runs a command (git_setting): by `git config`, `-c`,
    `--config-env`, or git's variables (GIT_RUN_VARIABLES: but a harmless_run; GIT_FILE_VARIABLES:
    but /dev/null), set for git or exported before it."""
    exported: dict[str, str] = {}
    for words, rest in commands:
        given = assigned(words[: len(words) - len(rest)])
        if not rest or program_of(rest[0]) in {"export", "declare", "typeset", "readonly"}:
            exported |= given | (assigned(rest[1:]) if rest else {})
            continue
        if program_of(rest[0]) != "git":
            continue
        for name, value in (exported | given).items():
            if name in GIT_RUN_VARIABLES and not harmless_run(value):
                return GIT_RUNS.format(key=name)
            if name in GIT_FILE_VARIABLES and value.strip() != "/dev/null":
                return GIT_RUNS.format(key=name)
            if name.startswith("GIT_CONFIG_KEY_"):
                return GIT_RUNS.format(key=name)
        at, command = 1, None
        while at < len(rest):
            word = rest[at]
            at += 1
            if word == "-c" and at < len(rest):
                key, given_value, value = rest[at].partition("=")
                at += 1
                if reason := git_setting(key, value if given_value else "true"):
                    return reason
            elif word.startswith("--config-env"):
                setting = word.partition("=")[2] or (rest[at] if at < len(rest) else "")
                at += not word.partition("=")[2]
                if reason := git_setting(setting.partition("=")[0], None):
                    return reason
            elif word in GIT_VALUED:
                at += 1
            elif not word.startswith("-"):
                command = word
                break
        if command == "config" and (reason := git_config_sets(list(rest[at:]))):
            return reason
    return None


def git_config_sets(words: list[str]) -> str | None:
    """Why `git config <words>` may not run: it sets what git_setting refuses, or opens git's
    configuration in an editor."""
    positional, options, at = [], set(), 0
    while at < len(words):
        word = words[at]
        at += 1
        if word.startswith("-") and len(word) > 1 and not positional[1:]:
            name = word.partition("=")[0]
            options.add(name)
            at += name in GIT_CONFIG_VALUED and "=" not in word
            continue
        positional.append(word)
    if options & {"-e", "--edit"} or positional[:1] == ["edit"]:
        return GIT_RUNS.format(key="git config --edit")
    if positional[:1] == ["set"]:
        positional = positional[1:]
    elif (positional[:1] and positional[0] in GIT_CONFIG_COMMANDS) or options & GIT_CONFIG_READS:
        return None
    if len(positional) < 2:
        return None
    return git_setting(positional[0], positional[1])


def spoken(command: str) -> str:
    """A line's words but those of a command that only prints or searches text (TEXT_ONLY) whose
    output is shown, or piped to one that only shows it (TEXT_SINKS): for the floor's checks of
    a line's words (`grep "gh pr merge"` says nothing); written to a file or fed on, they count.
    The line itself where it does not parse."""
    unheredoc, bodies = heredocs(command)
    plain, inside = without_substitutions(without_comments(unheredoc).replace("\\\n", ""))
    try:
        tokens = shell_words(plain)
    except (ValueError, TooComplexError):
        return command
    segments: list[tuple[list[str], str]] = []
    current: list[str] = []
    for token in tokens:
        if isinstance(token, Separator):
            segments.append((current, token))
            current = []
        else:
            current.append(token)
    segments.append((current, ""))
    kept: list[str] = []
    for index, (words, after) in enumerate(segments):
        name = program_of(command_name(redirection_free(words))[0]) if words else ""
        piped = "|" in after.replace("||", "")
        sink = ""
        if (
            piped
            and index + 1 < len(segments)
            and (fed := redirection_free(segments[index + 1][0]))
        ):
            sink = program_of(command_name(fed)[0])
        if name in TEXT_ONLY and not writes_file(words) and (not piped or sink in TEXT_SINKS):
            continue
        kept += words
    return " ".join([*kept, *inside, *(body for body, _ in bodies)])


def writes_file(words: list[str]) -> bool:
    """Whether a simple command's redirections write a file (`> x.sh`; not `2>&1`, /dev/null)."""
    for at, word in enumerate(words):
        if not isinstance(word, Redirecting) or ">" not in word[word.at :]:
            continue
        operator = word[word.at :]
        target = operator.lstrip("&0123456789>|") or (words[at + 1] if at + 1 < len(words) else "")
        if operator.lstrip("0123456789").startswith(">&") or target.startswith("&"):
            continue
        if target not in {"/dev/null", "/dev/stderr", "/dev/stdout"}:
            return True
    return False


def floor_unread(command: str, cwd: str, own: frozenset[str]) -> str | None:
    """What the floor can't read in a line (the block above), whatever it names."""
    if len(command) > MOST_LINE:
        return TOO_LONG
    try:
        commands = line_commands(command)
    except TooComplexError:
        return TOO_COMPLEX
    if names_unread(commands):
        return UNREAD_NAME
    if fed_scripts(command) is None:
        return FED_UNREAD
    if gh_copied(commands, squashed(command), cwd):
        return GH_COPIED
    if reason := renamed_or_unfound(commands, cwd, own):
        return reason
    return git_runs(commands)


def floor_denial(
    row: dict, request: Any, own: Iterable[str] = (), servers: dict | None = None
) -> str | None:
    """Why no worker may do this as the owner, whatever its mode (the floor above); None when
    it may. `own`: the branches that are the worker's own (its checkouts'); `servers`: the MCP
    servers of Settings (a GitHub one under another name is GitHub's too)."""
    if row["role"] != WORKER:
        return None
    kind = getattr(request, "kind", "")
    if kind == "mcp":
        server = str(getattr(request, "server_name", "") or "")
        if github_server(server, (servers or {}).get(server)) and not getattr(
            request, "read_only", False
        ):
            tool = getattr(request, "tool_name", "") or "a tool"
            return f"Denied: writing to GitHub as the owner ({tool}). {FLOOR_ROUTE}"
        return None
    if kind != "shell":
        return None
    command = getattr(request, "full_command_text", "") or ""
    try:
        reason = floor_line(command, row["cwd"] or "", frozenset(own), request)
    except Exception as exc:  # Any failure to check refuses, saying so.
        reason = f"{NOT_CHECKED} ({type(exc).__name__})"
    if not reason:
        return None
    tail = FLOOR_ROUTE if reason in {GH_API_WRITE, DEFAULT_PUSH, GH_FOREIGN_HEAD} else FLOOR_ASK
    return f"Denied: {reason}. {tail}"


def floor_line(
    command: str, cwd: str, own: frozenset[str], request: Any, depth: int = 0
) -> str | None:
    """The floor's reading of a command line: what it can't read (floor_unread), what it says
    of git and gh (floor_shell), and the lines a heredoc feeds a shell, read as lines of their
    own (MOST_NESTED deep)."""
    if depth > MOST_NESTED:
        return TOO_COMPLEX
    if reason := floor_unread(command, cwd, own):
        return reason
    # (Read unquoted too: the shell runs `"g"it` and `g\h` as git and gh.)
    named = FLOOR_WORDS.search(command) or FLOOR_WORDS.search(squashed(command))
    if named and (reason := floor_shell(command, cwd, own, request)):
        return reason
    for script in fed_scripts(command) or []:
        if reason := floor_line(script, cwd, own, request, depth + 1):
            return reason
    return None


def floor_needs_git(request: Any) -> bool:
    """Whether the floor's check of this request may ask git (a push's branch, an alias): such a
    check runs aside, never holding up the rest (Events.floor)."""
    command = getattr(request, "full_command_text", "") or ""
    if getattr(request, "kind", "") != "shell" or not re.search(r"\bgit\b", squashed(command)):
        return False
    return may_push(command) or may_alias(squashed(command))


def floor_shell(command: str, cwd: str, own: frozenset[str], request: Any) -> str | None:
    """The floor's reading of a command line (floor_denial)."""
    if len(command) > MOST_LINE:
        return TOO_LONG
    unquoted = squashed(command)
    said = spoken(command)  # (What only prints or searches text says nothing here.)
    if GH_WORD.search(command) or GH_WORD.search(unquoted):
        if found := GH_PR_SPEAKS.search(said):
            return f"`gh pr {found.group(1)}` speaks as the owner on GitHub"
        if found := GH_ISSUE_CHANGES.search(said):
            return f"`gh issue {found.group(1)}` speaks as the owner on GitHub"
        if gh_api_writes(said):
            return GH_API_WRITE
        if reason := gh_refusal(command, own):
            return reason
    if not re.search(r"\bgit\b", command) and not re.search(r"\bgit\b", unquoted):
        return None
    answers, settings, checked = ANSWERS.set({}), SETTINGS.set({}), CHECKED.set({})
    shell = SHELL.set(str(getattr(request, "shell", "") or ""))
    try:
        if too_complex(command, cwd):
            return TOO_COMPLEX
        if fed_script(command, cwd):
            return FED_SCRIPT
        if pushes_default(command, cwd):
            return DEFAULT_PUSH
        found = judged(command, cwd)
        if forced_elsewhere(command, cwd, own, says=lambda _: floor_force(said), found=found):
            return "a force push to a branch that is not one of yours"
        rewrites, rewrite_said = (lambda push: push.rewrites), (lambda _: floor_rewrite(said))
        if forced_elsewhere(command, cwd, own, rewrites, rewrite_said, found):
            return REWRITE
        if pushes_unknown(command, cwd):
            return UNREADABLE_PUSH
    finally:
        SHELL.reset(shell)
        CHECKED.reset(checked)
        SETTINGS.reset(settings)
        ANSWERS.reset(answers)
    return None


def forced_elsewhere(
    command: str,
    cwd: str,
    own: frozenset[str],
    does: Callable[[Push], bool] = lambda push: push.forced,
    says: Callable[[str], bool] = lambda command: floor_force(command),
    found: list[Push] | None = None,
) -> bool:
    """Whether a line force-pushes (`--force`, `-f`, `--force-with-lease`, `+refspec`), or does
    what `does` says of a push (Push.rewrites: deletes, prunes, mirrors, hands push options), to
    a branch that is not one of `own`, or one it can't tell (as its words say, `says`:
    v0.1.42's rule, when no push read from the line does so). `found`: the line's pushes, when
    judged already."""
    found = judged(command, cwd) if found is None else found
    forced = [push for push in found if does(push)]
    if not forced:
        return says(command) and not found
    checked = checked_branches(found)
    for push in forced:
        if push.unknown or push.anonymous or push.branches is None:
            return True
        branches = set(push.branches)
        if push.bare_in is not None:
            bare, unknown, several = bare_reading(push, checked, command, cwd)
            if bare is None or unknown or several:
                return True
            branches |= bare
        if not branches or not branches <= own:
            return True
    return False


# A shepherd step (shepherd.py) runs in its worker under the step profile: an OS sandbox with no
# GitHub credentials and no route to GitHub, no MCP servers or plugins, a tool allowlist. These
# checks are its second line, applied while the step runs whatever the worker's mode: anything
# that could publish or reach GitHub is refused, however the line is written (quotes and
# backslashes are read through). Loose on purpose: a refusal costs the step one command.
STEP_GH = re.compile(r"(?:^|[\s;&|(`$/=!{])gh(?=$|[\s;&|)`}])")
STEP_SSH = re.compile(r"(?:^|[\s;&|(`$/=!{])(?:ssh|scp|sftp|ssh-add|ssh-agent)(?=$|[\s;&|)`}])")
STEP_GIT = re.compile(r"(?:^|[\s;&|(`$/=!{])git(?=$|[\s;&|)`}])")
STEP_GIT_WRITES = re.compile(
    r"\b(?:push|send-pack|remote|credential|insteadof|pushinsteadof|pushurl|subtree|"
    r"request-pull|http\.\S*extraheader|url\.)"
)
STEP_GITHUB = re.compile(
    r"(?:^|[^a-z0-9-])(?:[a-z0-9-]+\.)*(?:github\.com|githubusercontent\.com|github\.io|"
    r"githubassets\.com|ghcr\.io)(?![a-z0-9-])"
)
STEP_TOOLS = frozenset({"shepherd_result", "draft_github_review"})
# What a step never reads, besides what its sandbox denies: GitHub's and git's credentials.
STEP_SECRETS = (".config/gh", ".git-credentials", ".netrc", ".ssh", "Library/Keychains")


def step_denial(request: Any, folders: list[str], cwd: str = "") -> str | None:
    """Why a session in a shepherd step may not do this, or None. `folders`: where it may write
    (the worktree in use, the frozen worktrees, its step folder; temp is added). They and `cwd`,
    its worker's folder, are paths Chief of Staff named (after a task: "…-crash-after-push-…"):
    in a command line, what they say is no word of it."""
    kind = getattr(request, "kind", "")
    if kind == "shell":
        command = getattr(request, "full_command_text", "") or ""
        if len(command) > MOST_LINE:
            return f"Denied in a shepherd step: {TOO_LONG}."
        words = squashed(command)
        named = {squashed(path).rstrip("/") for path in (*folders, cwd) if path}
        for path in sorted(named, key=len, reverse=True):
            if path.count("/") >= 2:  # A full path, never a word git reads as a command.
                words = words.replace(path, "/folder")
        if STEP_GH.search(words):
            return "Denied in a shepherd step: gh is not available (Chief of Staff publishes)."
        if STEP_SSH.search(words):
            return "Denied in a shepherd step: ssh is not available."
        if STEP_GITHUB.search(words):
            return "Denied in a shepherd step: GitHub is not reachable from a step."
        if STEP_GIT.search(words) and (
            STEP_GIT_WRITES.search(words) or may_push(words) or may_alias(words)
        ):
            return (
                "Denied in a shepherd step: commit only; pushing, remotes and git's credentials "
                "are Chief of Staff's."
            )
        return None
    if kind == "write":
        target = getattr(request, "resolved_path", None) or getattr(request, "file_name", "") or ""
        allowed = [*folders, tempfile.gettempdir()]
        if not target or not any(within(target, folder) for folder in allowed if folder):
            return "Denied in a shepherd step: write only in the step's own folders."
        return None
    if kind == "read":
        target = getattr(request, "resolved_path", None) or getattr(request, "path", "") or ""
        home = str(Path.home())
        if any(within(target, str(Path(home) / secret)) for secret in STEP_SECRETS):
            return "Denied in a shepherd step: credentials are not readable."
        return None
    if kind == "custom-tool":
        if getattr(request, "tool_name", "") in STEP_TOOLS:
            return None
        return "Denied in a shepherd step: that tool is not available."
    # MCP servers, URLs, memories, hooks, extensions, workflows: none in a step.
    return f"Denied in a shepherd step: {kind or 'that'} is not available."


# Chief of Staff's own operations for the shepherd, checked before each is sent, in every mode:
# exactly the commands it means to run, never more. A push is a fast-forward of one ref to a
# commit by its full SHA, with an exact lease on the head it was planned on, to the head
# repository's https URL, hooks off; never a default or protected branch, never forced, never a
# deletion. A merge is head-matched and never `--admin`. GitHub writes go only to the endpoints
# a plan's operations use.
FULL_SHA = re.compile(r"[0-9a-f]{40}")
GITHUB_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
REF_NAME = re.compile(r"(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,250}(?<![./])")
MERGE_METHODS = frozenset({"--squash", "--merge", "--rebase"})
NEVER_FLAGS = frozenset(
    {"--admin", "--delete", "-d", "--force", "-f", "--mirror", "--all", "--tags", "--prune",
     "--delete-branch", "--auto", "--force-if-includes", "--set-upstream", "-u"}
)  # fmt: skip
API_WRITES = (
    re.compile(r"repos/[\w.-]+/[\w.-]+/pulls/\d+/comments/\d+/replies"),
    re.compile(r"repos/[\w.-]+/[\w.-]+/issues/\d+/comments"),
    re.compile(r"repos/[\w.-]+/[\w.-]+/pulls/\d+/requested_reviewers"),
    re.compile(r"repos/[\w.-]+/[\w.-]+/actions/runs/\d+/rerun-failed-jobs"),
)
RESOLVE_MUTATION = re.compile(
    r"mutation\(\$id:ID!\)\{resolveReviewThread\(input:\{threadId:\$id\}\)\{thread\{isResolved\}\}\}"
)


def operation_denial(operation: dict, argv: list[str], payload: dict | None = None) -> str | None:
    """Why Chief of Staff may not run this for a shepherd operation, or None. `operation`: what
    the plan bound (its kind; for a push the head repository, ref, planned head and commit, the
    repository's default branch and whether the branch is protected, as just read). `payload`:
    the body it sends (GitHub's asynchronous merge, for a PR in a stack)."""
    kind = operation.get("kind")
    words = [str(word) for word in argv]
    if kind in {"push", "merge"} and any(word in NEVER_FLAGS for word in words):
        return "refused: no deletion, force, --admin or auto-merge"
    if kind == "push":
        return push_denial(operation, words)
    if kind == "merge" and words[:2] == ["gh", "api"]:
        return merge_async_denial(operation, words, payload or {})
    if kind == "merge":
        if words[:3] != ["gh", "pr", "merge"] or len(words) != 7:
            return "refused: not a plain merge"
        url = f"https://github.com/{operation.get('repo')}/pull/{int(operation.get('number', 0))}"
        if words[3] != url or words[4] not in MERGE_METHODS or words[5] != "--match-head-commit":
            return "refused: a merge is of this PR, matched to its latest commit"
        if not FULL_SHA.fullmatch(words[6]) or words[6] != operation.get("head"):
            return "refused: the merge must be of the commit you accepted"
        return None
    if kind == "rerun":
        if (
            words[:3] == ["gh", "run", "rerun"]
            and len(words) == 7
            and words[3].isdigit()
            and words[4:6] == ["--failed", "--repo"]
            and GITHUB_NAME.fullmatch(words[6])
        ):
            return None
        return "refused: a rerun is of a run's failed jobs only"
    if kind in {"reply", "conversation-reply", "request-review", "resolve"}:
        if words[:2] != ["gh", "api"]:
            return "refused: not a GitHub API call"
        if kind == "resolve":
            if "graphql" in words and any(RESOLVE_MUTATION.fullmatch(w.removeprefix("query="))
                                          for w in words):  # fmt: skip
                return None
            return "refused: only a review thread is resolved"
        method = next((w.split("=", 1)[1] for w in words if w.startswith("--method=")), "GET")
        if method.upper() != "POST":
            return "refused: only new comments and review requests are written"
        paths = [w for w in words[2:] if w.startswith("repos/")]
        if len(paths) != 1 or not any(pattern.fullmatch(paths[0]) for pattern in API_WRITES):
            return "refused: not an endpoint a plan writes to"
        return None
    return f"refused: {kind or 'that'} is not an operation"


def merge_async_denial(operation: dict, words: list[str], payload: dict) -> str | None:
    """A PR in a stack merges only through GitHub's asynchronous merge: of this PR, directly
    (never a queue, never bypassing rules), at the commit accepted, in a way GitHub names."""
    path = f"repos/{operation.get('repo')}/pulls/{int(operation.get('number', 0))}/merge-async"
    if words != ["gh", "api", path, "--method=PUT", "--input=-"]:
        return "refused: a merge is of this PR, matched to its latest commit"
    if (
        set(payload) != {"sha", "merge_method", "merge_action"}
        or payload["merge_action"] != "direct_merge"
        or f"--{payload['merge_method']}" not in MERGE_METHODS
    ):
        return "refused: not a plain merge"
    if not FULL_SHA.fullmatch(str(payload["sha"])) or payload["sha"] != operation.get("head"):
        return "refused: the merge must be of the commit you accepted"
    return None


def push_denial(operation: dict, words: list[str]) -> str | None:
    repo, ref = str(operation.get("head_repo") or ""), str(operation.get("ref") or "")
    lease, commit = str(operation.get("lease") or ""), str(operation.get("commit") or "")
    if not (GITHUB_NAME.fullmatch(repo) and REF_NAME.fullmatch(ref)):
        return "refused: the PR's repository or branch is not known"
    if ref in DEFAULT_BRANCHES or ref == operation.get("default_branch"):
        return f"refused: {ref} is the repository's default branch"
    if operation.get("protected") is not False:
        return (
            "refused: the branch is protected"
            if operation.get("protected")
            else "refused: protection unknown (push it yourself)"
        )
    if not (FULL_SHA.fullmatch(lease) and FULL_SHA.fullmatch(commit)) or lease == commit:
        return "refused: a push needs the PR's planned commit and a new one, by their full SHAs"
    if "push" not in words:
        return "refused: not a push"
    at = words.index("push")
    before, after = words[:at], words[at + 1 :]
    if before[:1] != ["git"] or "core.hooksPath=/dev/null" not in before:
        return "refused: hooks must be off"
    expected = [
        "--no-verify",
        f"--force-with-lease=refs/heads/{ref}:{lease}",
        f"https://github.com/{repo}.git",
        f"{commit}:refs/heads/{ref}",
    ]
    if after != expected:
        return "refused: a push is one commit, leased, to the PR's own branch"
    return None
