"""Which actions a session may take on its own. Pure functions, easy to test and to audit (but
for the branch a `git push` naming none goes to, which is asked of git).

These rules are guardrails checked in Chief of Staff's permission callback, not a sandbox: a
worker with a shell can still reach anything its user account can unless OS sandboxing is on
(see SECURITY.md).
"""

import bisect
import contextvars
import functools
import itertools
import os
import re
import subprocess
import tempfile
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
PUSH_WORD = re.compile(r"\bpush\b")
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
# A refspec whose branch its words do not say: a variable, a glob, a brace expansion.
UNREADABLE = re.compile(r"[$`*?\[{]")
# A substitution that names the default branch (`$(echo main)`).
MADE_UP_DEFAULT = re.compile(r"\$\([^()]*\b(?:main|master)\b|`[^`]*\b(?:main|master)\b")
# A value that is not plain text: a variable's, a substitution's (unless the whole of it).
UNSETTLED = re.compile(r"[$`\x00]")
# `$NAME` and `${NAME}`: a variable a line may have set (NAME=value) before its push.
VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
# The folder a command runs in, as a word says it (`$PWD`, `${PWD}`), and the substitutions
# that make it, or its repository's top (the same repository: its branch is asked of git).
HERE = re.compile(r"\$(?:PWD|\{PWD\})(?=/|$)")
HERE_MADE = frozenset({"pwd", "git rev-parse --show-toplevel"})
# A heredoc's start: `<<WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"` (not `<<<`, a here-string).
HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([^\s;&|()<>'\"`]+))")

# A variable's name.
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Where a line may set a variable (mentions): `${NAME=…}` and `${NAME:=…}`; `NAME=`, `NAME+=`
# and `NAME[…]=` starting a word (also in quotes: eval, a shell or a trap may run them); and
# the commands that set the variables they name (SETTERS), where a command may start.
SETTERS = ("for", "select", "read", "mapfile", "readarray", "getopts", "export", "local",
           "declare", "typeset", "readonly", "unset", "printf", "eval", "source", ".")  # fmt: skip
SETTING = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*):?="
    r"|(?<![^\s;&|()`'\"{!])([A-Za-z_][A-Za-z0-9_]*)(\[[^\]\s;&|()`'\"{!]*\])?(\+?=)(\(?)"
    r"|(?<![A-Za-z0-9_./-])(" + "|".join(map(re.escape, SETTERS)) + r")(?![A-Za-z0-9_./-])"
)
NAME_AFTER = re.compile(r"[ \t]+([A-Za-z_][A-Za-z0-9_]*)")
WORD_NAME = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\[[^\]]*\])?(\+?=)?(\()?")
ARGUMENTS_STOPS = re.compile(r"[;&|\n()`'\"\\]")
LINE_STOPS = re.compile(r"[\n'\"\\]")
# Words after which a word may be a command's name.
COMMAND_WORDS = frozenset(
    {"do", "then", "else", "elif", "if", "while", "until", "time", "builtin", "command", "exec",
     "eval", "!", "{"}
)  # fmt: skip


def command_position(text: str, at: int) -> bool:
    """Whether the word at `at` may be a command's name: first, or after a separator, a quote
    (a string eval or a shell may run), a reserved word, `builtin`, `command`, `exec`, `eval`,
    or an assignment (`IFS= read`); not a word in a sentence (`Add tests for branch names`)."""
    if at and text[at - 1] == "\\":
        at -= 1
    end = at
    while end and text[end - 1] in " \t":
        end -= 1
    if not end or text[end - 1] in ";&|(){}`'\"\n!":
        return True
    start = end
    while start and text[start - 1] not in " \t;&|(){}`'\"\n!":
        start -= 1
    word = text[start:end]
    return word in COMMAND_WORDS or bool(ASSIGNMENT.match(word))


def arguments_end(text: str, index: int, stops: re.Pattern = ARGUMENTS_STOPS) -> int:
    """Where the words of a command from `index` end: at a separator (`stops`), quotes read
    whole."""
    while stop := stops.search(text, index):
        at = stop.start()
        if text[at] in "'\"":
            index = quoted_end(text, at)
        elif text[at] == "\\":
            index = at + 2
        else:
            return at
    return len(text)


def mentions(text: str) -> dict[str, int] | None:
    """How often a line may set each variable (SETTING): an assignment that is plain (`B=x`)
    counts once, any other (`B+=x`, `B[0]=x`, `B=(x)`) twice; a command's words (`git branch`)
    and text (`-m "Fix branch handling"`) do not count. None when it may set any variable,
    which it does not name (`eval "$N=x"`, `read "$N"`, `declare -n`, `source`)."""
    counted: dict[str, int] = {}

    def add(name: str, times: int = 1):
        counted[name] = counted.get(name, 0) + times

    index, settled = 0, 0  # (No `$` nor backquote in the text from an `eval` up to `settled`.)
    while found := SETTING.search(text, index):
        index = found.end()
        if found.group(1):
            add(found.group(1))
            continue
        if found.group(2):
            plain = found.group(3) is None and found.group(4) == "=" and not found.group(5)
            add(found.group(2), 1 if plain else 2)
            continue
        setter = found.group(6)
        if not command_position(text, found.start()):
            continue
        if setter in {"for", "select"}:
            if name := NAME_AFTER.match(text, index):
                add(name.group(1))
                index = name.end()
            continue
        if setter in {"source", "."}:
            return None
        if setter == "eval":  # What it runs is the line's own text: read on.
            if index >= settled:
                settled = arguments_end(text, index, LINE_STOPS)
                if UNSETTLED.search(text, index, settled):
                    return None  # A line to run, made up as it runs (to its line's end).
            continue
        end = arguments_end(text, index)
        words, index = text[index:end].split(), end
        if setter == "printf":
            targets = [after for before, after in itertools.pairwise(words) if before == "-v"]
            words = targets + [word[2:] for word in words if word.startswith("-v") and word[2:]]
        elif setter in {"declare", "local", "typeset"}:
            if any(word.startswith("-") and "n" in word for word in words):
                return None  # A name that stands for another (`declare -n`).
        for word in words:
            if word.startswith("-"):
                continue
            if UNSETTLED.search(word.partition("=")[0]):
                return None  # A name made up as it runs (`read "$N"`, `export "$N=x"`).
            if not (named := WORD_NAME.match(word.lstrip("'\"\\"))):
                continue
            assigning = bool(named.group(3))
            if setter in {"export", "readonly"} and not assigning:
                continue  # Its value as it was.
            plain = not named.group(2) and named.group(3) in {None, "="} and not named.group(4)
            add(named.group(1), 1 if plain else 2)
    return counted


class TooComplexError(Exception):
    """A line that takes more to read than is read here (MOST_TOKENS, MOST_READ, MOST_MOVES,
    MOST_PATH, Tally.expands)."""


@dataclass
class Push:
    """One `git push`: whether it forces, the branches it updates for sure, and whether which
    it updates cannot be read from its words (a refspec in a variable it did not set, a glob,
    a substitution's destination, configuration that cannot be read here, an alias, another
    repository's bare push). A bare push (no refspec, `HEAD`, or one a substitution makes as a
    whole) updates where its folder's branch goes: asked of git, unless the line made a branch
    and switched to it first (`bare_new`, joined by `&&`); a checkout or switch before it may
    have gone elsewhere too (`bare_also`)."""

    forced: bool = False
    branches: set[str] | None = field(default_factory=set)  # None: every branch.
    # (At most MOST_BRANCHES of them; past those, only main and master: what is judged.)
    bare_in: str | None = None  # The folder a bare push runs in.
    bare_new: str = ""
    bare_also: frozenset[str] = frozenset()
    unknown: bool = False
    made_up: bool = False  # A refspec a substitution makes (taken as its folder's branch).

    def key(self) -> tuple:
        branches = None if self.branches is None else frozenset(self.branches)
        return (self.forced, branches, self.bare_in, self.bare_new, self.bare_also,
                self.unknown, self.made_up)  # fmt: skip


@dataclass
class Switched:
    """What a line did to a folder's branch before a push. A branch it made and switched to
    (`new`, where in the line), with what came before it (`prior_also`, `prior_unknown`: what
    counts when that switch may have failed); branches it made without switching (`made`);
    where checkouts after it may have gone (`also`), or somewhere not named (`unknown`)."""

    new: str = ""
    new_at: int = 0
    made: dict[str, int] = field(default_factory=dict)
    also: set[str] = field(default_factory=set)
    unknown: bool = False
    prior_also: set[str] = field(default_factory=set)
    prior_unknown: bool = False

    def switched_to(self, name: str, at: int) -> "Switched":
        """It made `name` and switched to it, at `at`: what came before is kept, for when that
        may have failed."""
        self.prior_also |= self.also | ({self.new} if self.new else set())
        self.prior_unknown = self.prior_unknown or self.unknown
        self.new, self.new_at, self.also, self.unknown = name, at, set(), False
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


def expanded(folder: str, path: str) -> str:
    """`path` from `folder`, `~` expanded, and `$PWD` (`${PWD}`, `$(pwd)`, the top of its
    repository: see without_substitutions) as `folder` itself; `folder` itself when it cannot
    be (an unknown user's home: the shell's `cd` fails there, and the command goes on where it
    was)."""
    if here := HERE.match(path):
        path = "." + path[here.end() :]
    try:
        return str(Path(folder) / Path(path).expanduser())
    except (RuntimeError, ValueError):
        return folder


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
) -> tuple[int, list[str]]:
    """Past the bodies of the heredocs `pending`, read from `index` (the line after the one
    they were opened on): each up to the first line after it that is its word; and the bodies
    whose word is not quoted (the shell runs their substitutions). A body with no line to end
    it is no heredoc: what is left is read from there as it is."""
    said, untabbed = line_starts(text)
    bodies = []
    for word, tabs, quoted in pending:
        starting = (untabbed if tabs else said).get(word, [])
        ending = bisect.bisect_left(starting, index)
        if ending == len(starting):  # Never ended: not taken for a heredoc.
            break
        end = starting[ending]
        stop = text.find("\n", end)
        if not quoted:
            bodies.append(text[index:end])
        index = len(text) if stop < 0 else stop + 1
    return index, bodies


def without_heredocs(text: str) -> tuple[str, list[str]]:
    """A command line without its heredocs' bodies, as bash reads them: after the line a
    `<<WORD` (unquoted, outside a comment, not in `((…))`) stands on, each line up to one that
    is WORD (its tabs first dropped for `<<-WORD`). What a body says is text, not commands (an
    apostrophe in it is no quote); but the shell runs the substitutions in one whose WORD is
    not quoted: those bodies are given apart. A body with no line to end it is kept."""
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


INNER_STOPS = re.compile(r"[\\'\"()<\n]")
DOUBLE_QUOTED_STOPS = re.compile(r'["\\$]')
BACKQUOTE_STOPS = re.compile(r"[\\`]")


def construct_end(text: str, index: int, opened: str) -> tuple[int, int]:
    """Where a double-quoted string (`opened` '"') or a command substitution (`opened` "$(")
    whose text starts at `index` ends: the end of its text, and past what closes it (the
    text's end for both when nothing does). What is nested in it is read whole, as bash reads
    it, in one pass: in double quotes, a backslash's character and a substitution; in a
    substitution, quotes, parentheses (other substitutions), and heredocs' bodies (text: an
    apostrophe in one is no quote; not in arithmetic, `$((…))`)."""
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
        elif char == "(":
            arithmetic += text.startswith("((", at)
            stack.append("(")
        elif char == ")":
            arithmetic -= bool(arithmetic and text.startswith("))", at))
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
    it stood in (`$PWD` for one that says the folder it runs in: HERE_MADE), and what each
    runs: lines of their own. `quoted`: all of the text is in double quotes (a heredoc's
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
            here = len(body) < 64 and " ".join(body.split()) in HERE_MADE
            kept.append("$PWD" if here else MADE_UP)
            continue
        if char in "'\"" and not quoted:
            quote = "" if quote == char else (quote or char)
        kept.append(char)
        index += 1
    return "".join(kept), bodies


def redirection_free(words: list[str]) -> list[str]:
    """A simple command's words without its redirections (`> log`, `2>/dev/null`, `<<EOF`,
    `main>/dev/null`: the word before the operator stays): they name files, not branches."""
    kept, target = [], False
    for word in words:
        if target:
            target = False
            continue
        if ">" not in word and "<" not in word:
            kept.append(word)
            continue
        at = min(i for i in (word.find(">"), word.find("<")) if i >= 0)
        rest = word[at:]
        target = not rest.endswith("&-") and not rest.strip("<>&|-")  # Its file: the next word.
        if word[:at] and not word[:at].isdigit():  # Not a file descriptor's number (`2>`).
            kept.append(word[:at])
    return kept


class Separator(str):
    """A word of a line that is a separator (`;`, `&&`, `|`, `(`…), not quoted: a quoted `";"`
    is a word like any other."""


PLAIN_RUN = re.compile(r"[^\s'\"\\" + re.escape(SEPARATORS) + "]+")


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
        while index < size:
            char = text[index]
            if char in " \t\r" or char in SEPARATORS:
                break
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
                word.append(found.group())
                index = found.end()
            else:  # Whitespace shlex does not split at (`\n` is a separator, `\f` a character).
                word.append(char)
                index += 1
        words.append("".join(word))
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
        if word == "&" and current and current[-1][-1:] in {">", "<"} and is_word:
            current[-1] += "&" + following  # `2>&1`, `>&2`: one redirection, split by `&`.
            index += 1
            continue
        if word == "&" and following.startswith(">"):
            current.append("&" + following)  # `&>log`: a redirection, not `&` and a command.
            index += 1
            continue
        if current:
            commands.append((joiner, redirection_free(current)))
            current, joiner = [], ""
        joiner += word
    if current:
        commands.append((joiner, redirection_free(current)))
    return commands, inside


def one_substitution(word: str) -> bool:
    """Whether a word is one command substitution, whole: MADE_UP (see without_substitutions);
    or text that reads as one, though quoted (`'$(git branch --show-current)'`), taken so too."""
    if word == MADE_UP:
        return True
    if word.startswith("`") and word.endswith("`") and len(word) > 1:
        return "`" not in word[1:-1]
    if not (word.startswith("$(") and word.endswith(")")):
        return False
    depth = 0
    for index, char in enumerate(word[1:], 1):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth == 0:
            return index == len(word) - 1
    return False


DESTINATION_STOPS = re.compile(r"\$\(|[`):]")


def destination(refspec: str) -> str:
    """The part of a refspec after its last `:` outside a substitution (all of it without
    one)."""
    if "$(" not in refspec and "`" not in refspec:
        return refspec.rpartition(":")[2]
    depth, quoted, last = 0, False, -1
    for found in DESTINATION_STOPS.finditer(refspec):
        char = found.group()
        if char == "`":
            quoted = not quoted
        elif char == "$(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif char == ":" and not depth and not quoted:
            last = found.start()
    return refspec[last + 1 :]


def resolved(word: str, value_of: Callable[[str], str | None]) -> str:
    """A word with the variables the line set put in (`$NAME`, `${NAME}`); one it did not set
    stays (`$NAME`: not known), and so does the whole word when it would be longer than
    MOST_VALUE (no branch's name is)."""
    if "$" not in word:
        return word
    pieces, at, size = [], 0, 0
    for found in VARIABLE.finditer(word):
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
    words: list[str], values: dict[str, tuple[str, ...]], tally: "Tally"
) -> list[list[str]]:
    """The words a simple command runs once the line's variables are put in: one list for each
    value a loop's variable takes (`for b in x y`), at most MOST_VALUES (past them, those
    stay unknown); a word a value went into split at its spaces, as the shell splits what it
    puts in unquoted (an empty one is no word). What they make is counted in `tally` as it is
    made (TooComplexError past its budget)."""
    if not values or not any("$" in word for word in words):
        return [words]
    looped = {
        name
        for word in words
        if "$" in word
        for found in VARIABLE.finditer(word)
        if len(values.get(name := found.group(1) or found.group(2), ())) > 1
    }
    combinations: list[dict[str, str]] = [{}]
    for name in sorted(looped):
        combinations = [{**done, name: value} for done in combinations for value in values[name]]
        if len(combinations) > MOST_VALUES:
            combinations = [{}]
            break
    found = []
    for combination in combinations:

        def value_of(name: str, chosen: dict[str, str] = combination) -> str | None:
            said = values.get(name, ())
            return chosen.get(name, said[0] if len(said) == 1 else None)

        run = []
        for word in words:
            put = resolved(word, value_of)
            made = [put] if put == word else put.split()
            tally.expands(len(put), len(made))
            run += made
        found.append(run)
    return found


def git_config_pushes(config: dict[str, str | None]) -> tuple[list[str], bool, bool]:
    """What configuration given on the command line says a push updates: refspecs
    (`remote.<name>.push`), every branch (`push.default=matching`), or not known (a value
    that is not in the command line)."""
    refspecs, every, unknown = [], False, False
    for key, value in config.items():
        if key.startswith("remote.") and key.endswith(".push"):
            if value is None:
                unknown = True
            else:
                refspecs.append(value)
        elif key == "push.default":
            if value is None:
                unknown = True
            elif value.lower() == "matching":
                every = True
            elif value.lower() not in {"nothing", "current", "upstream", "simple", "tracking"}:
                unknown = True
    return refspecs, every, unknown


def environment_config(env: dict[str, str]) -> dict[str, str | None]:
    """git configuration given through the environment (GIT_CONFIG_COUNT and its keys and
    values; GIT_CONFIG_PARAMETERS, whose push and alias settings are not read here: not
    known)."""
    found: dict[str, str | None] = {}
    parameters = env.get("GIT_CONFIG_PARAMETERS", "").lower()
    if "push" in parameters:
        found["remote.?.push"] = None
    if "alias." in parameters:
        found["alias.?"] = None
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
        if name == "-C" and value:
            folder = tally.moved(folder, value) if tally else expanded(folder, value)
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
    return Git(folder, config, words[index:], elsewhere)


def switch_of(arguments: list[str]) -> tuple[str, str] | None:
    """What `git checkout`/`git switch` with these arguments does to the branch its checkout is
    on: ("new", name) makes that branch and switches to it; ("to", name) may switch to `name`
    (a branch, or for `checkout` a file it restores instead: not known here); ("unknown", "")
    switches to one it does not name (`-`, `@{-1}`); None switches nothing (files after
    `--`)."""
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
    return ("to", named[0])


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


def push_of(git: Git) -> Push | None:
    """The `git push` a git invocation runs (the line's variables put in its words), or
    None."""
    if UNSETTLED.search(git.rest[0]):
        return Push(unknown=True)  # A command in a variable or a substitution: maybe a push.
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
    refspecs, every, unknown = git_config_pushes(git.config)
    push.unknown = unknown
    if every:
        push.branches = None
    heads = False  # A refspec that pushes the folder's branch (`HEAD`, a substitution's whole).
    # Every word that is not an option counts as a refspec but the remote's (its name, or a
    # variable that holds it, names no branch of its own).
    for word in [*positional[1:], *refspecs]:
        push.forced = push.forced or word.startswith("+")
        if one_substitution(word):
            push.made_up = True  # As `HEAD`: its folder's branch (asked of git).
            continue
        target = destination(word.lstrip("+"))
        if MADE_UP in target or "$(" in target or "`" in target or UNREADABLE.search(target):
            push.unknown = True  # A substitution, a variable, a glob: not known.
            continue
        branch = pushed_branch(target)
        if branch in {"HEAD", "@"}:
            heads = True
        elif push.branches is not None and (
            branch in DEFAULT_BRANCHES or len(push.branches) < MOST_BRANCHES
        ):
            push.branches.add(branch)  # (Past MOST_BRANCHES, only the default's are kept.)
    if dry:
        return push  # It updates nothing.
    if deleting or refspecs:
        push.unknown = push.unknown or push.made_up  # Which one it deletes: not known.
        return push  # What it updates is all in its words.
    if (len(positional) <= 1 and not tags) or push.made_up or heads:
        push.bare_in = git.folder  # Where its folder's branch goes.
        push.unknown = push.unknown or git.elsewhere  # Another repository's: not known.
    return push


def loose_pushes(command: str, cwd: str) -> list[Push]:
    """The pushes of text the shell would not parse (an unclosed quote), read loosely: in each
    stretch up to a separator, the words after the first `push` that follows its first `git`.
    A bare one is judged by the line's folder (no `cd` or `-C` is read here)."""
    found = []
    for stretch in FLOOR_ENDS.split(command):
        if (git := GIT_WORD.search(stretch)) and (push := PUSH_WORD.search(stretch, git.end())):
            words = ["git", "push", *(w.strip("'\"`()\\") for w in stretch[push.end() :].split())]
            git_push = git_command(words, {}, cwd)
            if git_push and (found_push := push_of(git_push)):
                if found_push.bare_in is not None and not cwd:
                    found_push.bare_in, found_push.unknown = None, True
                found.append(found_push)
    return found


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
    pushes: list[Push] = field(default_factory=list)
    read: int = 0
    moves: int = 0
    made: int = 0
    made_words: int = 0
    names: dict[str, int] | None = None
    namable: bool = True

    def mentioned(self, name: str) -> int:
        """How often the line may set `name` (mentions; twice when it may set any)."""
        if self.namable and self.names is None:
            self.names = mentions(self.text)
            self.namable = self.names is not None
        return self.names.get(name, 0) if self.names is not None else 2

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

    def moved(self, folder: str, path: str) -> str:
        """The folder `cd path` or `git -C path` moves to from `folder`, counted."""
        self.moves += 1
        if self.moves > MOST_MOVES or len(folder) + len(path) > MOST_PATH:
            raise TooComplexError
        return expanded(folder, path)


@dataclass
class Line:
    """What reading a line keeps as it goes: git's own variables it set (`env`), the values of
    those it set in the one place it may set them (`values`: a loop's several), its folder,
    what it did to each folder's branch, and how many joins were not `&&`, up to each simple
    command (`marks`)."""

    folder: str
    depth: int
    tally: Tally
    env: dict[str, str] = field(default_factory=dict)
    values: dict[str, tuple[str, ...]] = field(default_factory=dict)
    switched: dict[str, Switched] = field(default_factory=dict)
    marks: list[int] = field(default_factory=list)

    def value_of(self, name: str) -> str | None:
        """The value a variable holds, when one (a loop's holds several)."""
        said = self.values.get(name, ())
        return said[0] if len(said) == 1 else None

    def assign(self, name: str, said: list[str]):
        """`name` set to one of these words (`NAME=value`, `for NAME in …`), for the words after
        it: known only when this is the one place the line may set it (a loop, `read`, a
        second assignment would make it something else, maybe before a later word is read)
        and each value is plain text or one substitution's whole."""
        self.values.pop(name, None)
        values = tuple(resolved(value, self.value_of) for value in said)
        if (
            0 < len(values) <= MOST_VALUES
            and self.tally.mentioned(name) == 1
            and all(value == MADE_UP or not UNSETTLED.search(value) for value in values)
        ):
            self.values[name] = values


def read_line(command: str, cwd: str, depth: int, tally: Tally) -> str:
    """Each `git push` in a command line, as the shell would run it, into `tally`: wherever
    `git` stands in a simple command (after `if`, `do`, `{`, `timeout 60`, `env A=b`…), in
    `sh -c '…'`, `eval` and command substitutions, after `cd`, with configuration and variables
    set on the line, and the branch a line made and switched to. Read in one pass: a simple
    command is read up to the first `git`, shell `-c` or `eval` in it. "" once read in full,
    else why not (TOO_COMPLEX; or raises TooComplexError)."""
    if depth > MOST_NESTED:
        return TOO_COMPLEX
    tally.reads(command)
    parsed = shell_commands(command)
    if parsed is None:  # Not a command line the shell runs: read loosely.
        tally.pushes += loose_pushes(command, cwd)
        return ""
    commands, inside = parsed
    if sum(len(words) for _, words in commands) > MOST_WORDS:
        return TOO_COMPLEX
    for body in inside:  # What its command substitutions run: lines of their own.
        if may_push(body) and (why := read_line(body, cwd, depth + 1, tally)):
            return why
    line = Line(cwd, depth, tally)
    broken = 0
    for index, (joiner, words) in enumerate(commands):
        broken += bool(index) and joiner.replace("\n", "") != "&&"
        line.marks.append(broken)
        first = next((i for i, word in enumerate(words) if word not in KEYWORDS), len(words))
        rest = words[first:]
        if rest and all(ASSIGNMENT.match(word) for word in rest):
            set_variables(line, rest)  # `NAME=value` alone: for what follows.
        elif rest and rest[0] == "export":
            set_variables(line, rest[1:])
        elif first and words[first - 1] == "for" and len(rest) > 1 and rest[1] == "in":
            if NAME.fullmatch(rest[0]):
                line.assign(rest[0], rest[2:])  # `for NAME in …`: each of those, in turn.
        elif rest and rest[0] == "cd" and cwd:
            to = resolved(rest[1], line.value_of) if len(rest) > 1 else "~"
            line.folder = tally.moved(line.folder, to)
        elif why := read_command(words, index, line):
            return why
        if len(line.env) > MOST_GIT_ENV:
            return TOO_COMPLEX
    return ""


def set_variables(line: Line, words: list[str]):
    """The variables these `NAME=value` words set, for what follows on the line: git's own
    (its configuration), and any whose value is plain text (put in a later refspec)."""
    for word in words:
        if not ASSIGNMENT.match(word):
            continue
        name, _, value = word.partition("=")
        if name.startswith("GIT_"):
            line.env[name] = value
        line.assign(name, [value])


def git_variables(words: list[str]) -> dict[str, str]:
    """git's own variables these `NAME=value` words set (others say nothing of a push)."""
    found = {}
    for word in words:
        name, has, value = word.partition("=")
        if has and name.startswith("GIT_") and ASSIGNMENT.match(word):
            found[name] = value
    return found


def read_command(words: list[str], index: int, line: Line) -> str:
    """One simple command, read up to its first `git`, shell `-c` or `eval`."""
    local: dict[str, str] = {}
    for position, word in enumerate(words):
        if ASSIGNMENT.match(word):
            local.update(git_variables([word]))
            continue
        program = word.rsplit("/", 1)[-1]
        if program == "git":
            here = {**line.env, **local} if local else line.env
            for run in expansions(words[position:], line.values, line.tally):
                read_git(run, here, index, line)
            return ""
        if program in SHELLS:  # Its words are its own: a script's (`-c`), or a file's.
            script = shell_script(words, position)
            return (
                "" if script is None else read_line(script, line.folder, line.depth + 1, line.tally)
            )
        if program == "eval":
            return read_line(
                " ".join(words[position + 1 :]), line.folder, line.depth + 1, line.tally
            )
    return ""


def read_git(words: list[str], env: dict[str, str], index: int, line: Line):
    """One `git …` run: a push, into the line's tally; a checkout, switch or branch, kept for
    the pushes after it."""
    git = git_command(words, env, line.folder, line.tally)
    if git is None:
        return
    if push := push_of(git):
        if push.bare_in is not None and (made := line.switched.get(push.bare_in)):
            trusted = made.new and line.marks[index] == line.marks[made.new_at]
            if trusted:
                push.bare_new = made.new
                push.bare_also = frozenset(made.also)
                push.unknown = push.unknown or made.unknown
            else:  # That switch may have failed: where it was before counts too.
                also = made.prior_also | made.also | ({made.new} if made.new else set())
                push.bare_also = frozenset(also)
                push.unknown = push.unknown or made.unknown or made.prior_unknown
        line.tally.pushes.append(push)
    else:
        record_switch(git, index, line)


def record_switch(git: Git, index: int, line: Line):
    """What a `git checkout`, `git switch` or `git branch` does to its folder's branch, for a
    bare push later in the line: a branch made and switched to counts only when every join
    from there is `&&` (it may have failed, and the line gone on: `;`, `||`, a new line)."""
    if git.rest[0] in {"checkout", "switch"}:
        what = switch_of(git.rest)
    elif git.rest[0] == "branch":
        what = renamed_or_made(git.rest)
    else:
        return
    if what is None:
        return
    kind, name = what
    marks = line.marks
    made = line.switched.setdefault(git.folder, Switched())
    if kind == "new":
        line.switched[git.folder] = made.switched_to(name, index)
    elif kind == "made":
        made.made[name] = index
    elif kind == "to" and name in made.made and marks[index] == marks[made.made[name]]:
        line.switched[git.folder] = made.switched_to(name, made.made[name])
    elif kind in {"to", "renamed"}:
        made.also.add(name)
        made.capped()
    else:
        made.unknown = True


def squashed(command: str) -> str:
    """A line without quotes, backslashes and line continuations, in lower case: where a word
    of it says `push` (or an alias) however it is quoted."""
    return command.replace("\\\n", "").translate(str.maketrans("", "", "'\"\\")).lower()


def may_push(command: str) -> bool:
    """Whether a word of a line may say `push` or an alias, however it is quoted (a line that
    says neither pushes nothing)."""
    words = squashed(command)
    return "push" in words or "alias" in words


@functools.lru_cache(maxsize=32)
def reading(command: str, cwd: str) -> tuple[tuple[Push, ...], str]:
    """Each push of a command line, once each (see read_line), and why it was not read in full
    ("" when it was). A line no word of which says `push` or an alias pushes nothing."""
    if not may_push(command):
        return (), ""
    if len(command) > MOST_LINE:
        return (), TOO_LONG
    tally = Tally(command)
    try:
        why = read_line(command, cwd, 0, tally)
    except TooComplexError:
        why = TOO_COMPLEX
    unique = tuple({push.key(): push for push in tally.pushes}.values())
    asked = {push.bare_in for push in unique if push.bare_in is not None and not push.bare_new}
    if len(asked) > MOST_FOLDERS:
        why = why or TOO_COMPLEX
    return unique, why


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


# What git said of each folder's branch, during one check (shell_denial): asked once a folder.
ANSWERS: contextvars.ContextVar[dict[str, set[str]] | None] = contextvars.ContextVar(
    "ANSWERS", default=None
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


def bare_branches(push: Push) -> tuple[set[str], bool]:
    """The branches a bare push may update (the one the line made and switched to, or its
    folder's, asked of git; and where a checkout before it may have gone), and whether its
    folder's is not known (git says none: no branch checked out, no repository, a folder made
    up; git itself would push nothing there, or the line is not read right)."""
    if push.bare_in is None:
        return set(), False  # Not a bare push.
    if push.bare_new:
        return {push.bare_new} | push.bare_also, False
    on = branches_of(push.bare_in)
    return on | push.bare_also, not on


def pushes_default(command: str, cwd: str = "") -> bool:
    """Whether a command line may push to the default branch, as far as its words say (and,
    for a push that names no branch, the branch its folder is on: asked of git once a
    folder); never less than v0.1.42's rule said."""
    if floor_default(command) or too_complex(command, cwd):
        return True  # (Too complex: refused for that, with nothing asked of git.)
    for push in pushes(command, cwd):
        if push.branches is None or push.branches & DEFAULT_BRANCHES:
            return True
        if push.made_up and MADE_UP_DEFAULT.search(command):
            return True
        if not push.unknown and bare_branches(push)[0] & DEFAULT_BRANCHES:
            return True
    return False


def pushes_unknown(command: str, cwd: str = "") -> bool:
    """Whether a push's branch cannot be read from the line (see Push.unknown), or a bare
    one's from git (see bare_branches)."""
    return any(push.unknown or bare_branches(push)[1] for push in pushes(command, cwd))


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
    answers = ANSWERS.set({})
    try:
        first = next((reason for found, reason in DANGEROUS if found(command, cwd)), None)
    finally:
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
