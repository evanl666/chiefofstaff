"""Which actions a session may take on its own. Pure functions, easy to test and to audit (but
for where a `git push` naming no branch goes, its folder's branch and configuration, and
whether a name a checkout goes to is a branch: asked of git).

These rules are guardrails checked in Chief of Staff's permission callback, not a sandbox: a
worker with a shell can still reach anything its user account can unless OS sandboxing is on
(see SECURITY.md).
"""

import bisect
import collections
import contextvars
import fnmatch
import functools
import itertools
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Callable
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

    def key(self) -> tuple:
        branches = None if self.branches is None else frozenset(self.branches)
        return (self.forced, branches, self.bare_in, self.bare_new, self.bare_also,
                self.unknown, self.made_up, self.nested, self.current, self.configured,
                self.mapped, self.mode, self.bare_check, self.alt_new, self.alt_also,
                self.alt_unknown, self.upstreams_set, self.anonymous)  # fmt: skip


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
        if alias is None or "push" in alias.lower() or "alias.?" in git.config:
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
            valued = name in PUSH_VALUED and "=" not in word
            continue
        if word.startswith("-") and len(word) > 1:
            for at, letter in enumerate(word[1:], 1):
                if letter == "o":  # `-o <option>`, or `-o<option>`: the rest is its value.
                    valued = at == len(word) - 1
                    break
                push.forced = push.forced or letter == "f"
                deleting = deleting or letter == "d"
                dry = dry or letter == "n"
            continue
        positional.append(word)
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
        return push  # It updates nothing.
    if deleting or refspecs:
        push.unknown = push.unknown or push.made_up  # Which one it deletes: not known.
        return push  # What it updates is all in its words.
    configured = len(positional) <= 1 and not tags
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
    (`git -C "$(cd x && pwd)" push`) or before this one's, any in text that defines an alias,
    and every one in text that runs `git … config`."""
    found, elsewhere, gits = [], bool(ELSEWHERE.search(command)), False
    aliased = bool(ALIASING.search(command)) and "push" in squashed(command)
    configures = False
    for stretch in FLOOR_ENDS.split(command):
        git = GIT_WORD.search(stretch)
        configures = configures or bool(git and CONFIG_WORD.search(stretch, git.end()))
        if git and gits and PUSH_WORD.search(stretch, 0, git.start()):
            found.append(Push(unknown=True))  # (`g push … $(git …)`: g, an alias, not read.)
        push = PUSH_WORD.search(stretch, git.end()) if git else PUSH_WORD.search(stretch)
        if not push or not (git or gits):
            gits = gits or bool(git)
            continue
        gits, rest = True, stretch[push.end() :].split()
        runs_it = set(re.split(r"[\s/]+", stretch[: git.start()] if git else ""))
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
        elif rest[:1] == ["esac"]:
            if not cases or cases.pop() != depth:
                return None
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
    if parsed is None:  # Not a command line the shell runs: read loosely.
        tally.pushes += loose_pushes(command, cwd)
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
        if may_push(body, switching=True) and (why := read_line(body, inner, depth + 1, tally)):
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
    for position, word in enumerate(words):
        if ASSIGNMENT.match(word):
            local.update(git_variables([word]))
            continue
        if line.tally.zsh and word.startswith("=") and len(word) > 1:
            word = word[1:]  # (zsh's `=git`: git's path.)
        program = word.rsplit("/", 1)[-1]
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
# file's name), unless told where to put them (`xargs -I {}`).
FEEDERS = frozenset({"xargs", "parallel", "fd", "fdfind"})


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
    fed = feeds == "*" or bool(feeds and any(feeds in word for word in words[1:]))
    if fed and git.rest[0] in SWITCHING:
        record_switch(git, index, line, ("unknown", ""))
        return
    if git.rest[0] == "config":
        sets, alias = config_change(git.rest)
        line.tally.config_set = line.tally.config_set or sets
        if alias and (line.tally.says("push") or line.tally.says("send-pack")):
            line.tally.pushes.append(Push(unknown=True))  # An alias that may push, from here on.
    removes, upstream = branches_changed(git.rest)
    line.tally.unbranched = line.tally.unbranched or removes
    line.tally.upstreams_set = line.tally.upstreams_set or upstream
    git.epoch, git.currents = line.epoch, line.tally.currents
    if push := push_of(git):
        push.unknown = push.unknown or fed
        push.nested = bool(line.depth)
        if push.bare_in is not None:  # A switch in another folder may be in the same repository.
            for folder, other in line.switched.items():
                if folder != push.bare_in:
                    names = other.prior_also | other.also | ({other.new} if other.new else set())
                    push.bare_also |= names
                    push.unknown = push.unknown or other.unknown or other.prior_unknown
        if push.bare_in is not None and UNSETTLED.search(push.bare_in):
            push.unknown = True  # A folder not known (LOST): a switch "there" may be elsewhere.
        elif push.bare_in is not None and (made := line.switched.get(push.bare_in)):
            new, also, unknown = switched_reading(made, index, line)
            push.bare_new, push.bare_also = new, push.bare_also | also
            push.unknown = push.unknown or unknown
            old = line.switched_old.get(push.bare_in)
            if new and made.checkout and old is not None:  # Only if git has that branch.
                push.bare_check = True
                push.alt_new, alt_also, push.alt_unknown = switched_reading(old, index, line)
                push.alt_also = frozenset(alt_also)
        line.tally.pushes.append(push)
    else:
        record_switch(git, index, line)


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


def config_change(rest: list[str]) -> tuple[bool, bool]:
    """What `git config` with these words may change of where pushes go: whether it sets (or
    unsets, renames, removes) push.default, a remote's push refspecs or mirror, a branch's
    upstream or push remote, an alias or a file to include, or what it can't say (a key in a
    variable, an editor); and whether it may set an alias. Reading changes nothing."""
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
        return False, False
    if action == "edit" or options & {"-e", "--edit"}:
        return True, True
    sections = action.endswith("-section") or bool(
        options & {"--rename-section", "--remove-section"}
    )
    if not action and not options & CONFIG_CHANGES and len(positional) < 2:
        return False, False  # (`git config NAME`: it reads it.)
    sets = alias = False
    for key in positional[: 2 if sections else 1]:
        if UNSETTLED.search(key):
            return True, True
        lower = key.lower()
        part = lower.split(".", 1)[0] if sections else lower
        sets = sets or (part in PUSH_SECTIONS if sections else bool(PUSH_KEYS.fullmatch(part)))
        alias = alias or part == "alias" or part.startswith("alias.")
    return sets, alias


def squashed(command: str) -> str:
    """A line without quotes, backslashes and line continuations, in lower case: where a word
    of it says `push` (or an alias) however it is quoted."""
    return command.replace("\\\n", "").translate(str.maketrans("", "", "'\"\\")).lower()


def may_push(command: str, switching: bool = False) -> bool:
    """Whether a word of a line may say `push` or an alias, however it is quoted (a line that
    says neither pushes nothing); or, `switching`, go to another branch for a push after it."""
    words = squashed(command)
    if switching and any(word in words for word in SWITCHING):
        return True
    return "push" in words or "alias" in words or "send-pack" in words


# The shell a check's command line runs in, when not bash or sh (Codex's `zsh -lc`): see
# Tally.zsh. Set for one check (shell_denial).
SHELL: contextvars.ContextVar[str] = contextvars.ContextVar("SHELL", default="")


def reading(command: str, cwd: str) -> tuple[tuple[Push, ...], str]:
    """Each push of a command line, once each (see read_line), and why it was not read in full
    ("" when it was), as the shell of the check reads it (SHELL)."""
    return read_pushes(command, cwd, SHELL.get())


@functools.lru_cache(maxsize=32)
def read_pushes(command: str, cwd: str, shell: str) -> tuple[tuple[Push, ...], str]:
    """See reading. A line no word of which says `push` or an alias pushes nothing."""
    if not may_push(command):
        return (), ""
    if len(command) > MOST_LINE:
        return (), TOO_LONG
    tally = Tally(command, cwd, shell)
    tally.currents = CURRENT[1] not in command
    try:
        why = read_line(command, cwd, 0, tally)
    except TooComplexError:
        why = TOO_COMPLEX
    for push in tally.pushes:
        push.unknown = push.unknown or tally.config_set  # (Where it goes: what the line set.)
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
    if len(asked) > MOST_FOLDERS:
        why = why or TOO_COMPLEX
    return unique, why


reading.cache_clear = read_pushes.cache_clear  # type: ignore[attr-defined]


def pushes(command: str, cwd: str = "") -> list[Push]:
    """Each `git push` in a command line, as the shell would run it (see read_line). A rule
    of text, so best effort: what a command makes up as it runs is not known here."""
    return list(reading(command, cwd)[0])


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
FED_PROCESS = "\x01<("  # A process substitution's file (`<(…)`, zsh's `=(…)`), as a word.
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


def current_branches(folder: str) -> set[str]:
    """The branch a folder's checkout is on and the remote branch a bare push there updates
    (`@{push}`), asked of git; none when it cannot say."""
    found = set()
    for ref in ("HEAD", "@{push}"):
        try:
            done = subprocess.run(  # noqa: S603  (git, with its folder and a ref of its own)
                ["git", "-C", folder, "rev-parse", "--abbrev-ref", ref],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
            continue
        name = done.stdout.strip() if done.returncode == 0 else ""
        if name and name != "HEAD":
            found.add(name.split("/", 1)[1] if ref == "@{push}" and "/" in name else name)
    return found


def git_output(arguments: list[str], most: int) -> tuple[int, bytes] | None:
    """What git says with these arguments (its exit status and what it writes), within 10
    seconds and `most` bytes; None when it can't be run or read, or takes or says more."""
    try:
        process = subprocess.Popen(  # noqa: S603  (git, with arguments of its own)
            ["git", *arguments],  # noqa: S607
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
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


# The keys of git's configuration that say where a push goes from a folder (Pushing), at most
# so many bytes of them (past those: not known).
PUSH_SETTINGS = (
    r"^(push\.default|remote\..+\.(push|mirror)|branch\..+\.merge|includeif\.onbranch:.+\.path)$"
)
MOST_SETTINGS = 1 << 20


def config_entries(folder: str) -> list[tuple[str, str | None]] | None:
    """What git's configuration for a folder (the system's, the user's, the repository's, as
    git reads them) says of where pushes go: its keys (in git's own case) and values (None for
    a key with none); None when git can't say."""
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
    include that depends on the branch (`includeIf.onbranch:…`); or not known."""

    every: bool = False
    mode: str = "simple"
    refspecs: set[str] = field(default_factory=set)
    upstreams: dict[str, set[str]] = field(default_factory=dict)
    onbranch: bool = False
    unknown: bool = False


def pushing_of(entries: list[tuple[str, str | None]] | None) -> Pushing:
    """Pushing, read from a folder's configuration (the last push.default counts)."""
    pushing = Pushing(unknown=entries is None)
    for key, value in entries or []:
        lower = key.lower()
        if lower == "push.default":
            pushing.mode = value if value in MODES else "?"
        elif lower.startswith("remote.") and lower.endswith(".mirror"):
            truth = "true" if value is None else value.lower()
            pushing.every = pushing.every or truth in TRUE
            pushing.unknown = pushing.unknown or truth not in TRUE | FALSE
        elif lower.startswith("remote.") and lower.endswith(".push"):
            configured_refspec(pushing, value)
        elif lower.startswith("branch.") and lower.endswith(".merge"):
            pushing.upstreams.setdefault(key[7:-6], set()).add(pushed_branch(value or ""))
        elif lower.startswith("includeif.onbranch:"):
            pushing.onbranch = True
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


def asks(push: Push) -> bool:
    """Whether judging a bare push asks git of its folder (its branch, its configuration, the
    branch a checkout went to)."""
    return not push.bare_new or push.bare_check or push.configured or push.mapped


def bare_reading(push: Push, checked: dict[str, set[str]]) -> tuple[set[str] | None, bool, bool]:
    """Where a bare push may go: the branches it may update (None: every branch of the same name
    on both sides), whether that is not known (no branch checked out, no repository, a folder
    made up, configuration git can't read: git itself would push nothing there, or the line is
    not read right), and whether it may be several only because of a switch before it (then it
    is not known which). Its branch is the one the line switched to (a checkout's only if git
    has it; else as the line read before 0.1.46: alt_*), or its folder's (asked of git); and,
    pushing no refspec or the checkout's branch by name, where git's configuration sends that."""
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
    found = pushes(command, cwd)
    checked = checked_branches(found)
    for push in found:
        if push.branches is None or push.branches & DEFAULT_BRANCHES:
            return True
        if push.made_up and MADE_UP_DEFAULT.search(command):
            return True
        if not push.unknown:
            branches, unknown, several = bare_reading(push, checked)
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
    found = pushes(command, cwd)
    checked = checked_branches(found)
    anonymous = False
    for push in found:
        anonymous = anonymous or push.anonymous
        if push.unknown and not push.anonymous:
            return True
        branches, unknown, several = bare_reading(push, checked)
        if unknown or (several and branches is not None and branches & DEFAULT_BRANCHES):
            return True
    return anonymous and not fed_script(command, cwd)


def pushes_forced(command: str, cwd: str = "") -> bool:
    return floor_force(command) or any(push.forced for push in pushes(command, cwd))


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


def written_file(cwd: str, request: Any) -> str:
    target = getattr(request, "resolved_path", None) or getattr(request, "file_name", "") or ""
    if cwd and target and not Path(target).is_absolute():
        target = str(Path(cwd) / target)
    if cwd and target and within(target, cwd):
        return os.path.relpath(target, cwd)
    return target


def owners_checkout(row: dict, request: Any, checkouts: list[dict]) -> str | None:
    """Why a worker may not write this file: it is in a git checkout of the owner's it was given
    only to read (a task that changes no code, see Workspaces.writes). Also when it acts without
    permission checks (yolo): the owner's checkouts are never written."""
    if row["role"] != WORKER or getattr(request, "kind", "") != "write":
        return None
    target = getattr(request, "resolved_path", None) or getattr(request, "file_name", "")
    if not Path(target).is_absolute():
        target = str(Path(row["cwd"]) / target)
    kept = [
        c["path"]
        for c in checkouts
        if c.get("shared") and c.get("git_dir") and not c.get("in_place")
    ]
    if not any(within(target, folder) for folder in kept):
        return None
    return (
        "This folder is the owner's own checkout: nothing is written there. To change it or run "
        "what writes files (installs, builds, tests), add_repo it: you get your own worktree and "
        "branch, and work there."
    )


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
