"""Which actions a session may take on its own. Pure functions, easy to test and to audit (but
for the branch a `git push` naming none goes to, which is asked of git).

These rules are guardrails checked in Chief of Staff's permission callback, not a sandbox: a
worker with a shell can still reach anything its user account can unless OS sandboxing is on
(see SECURITY.md).
"""

import bisect
import functools
import os
import re
import shlex
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
# How much of a line is read for pushes, at most: what is past it (a longer line, shells and
# evals nested deeper, bare pushes in more folders than are asked of git, more of git's own
# variables) is refused, saying why, under Ask first (where these rules apply).
MOST_CHARS = 400_000
MOST_WORDS = 40_000
MOST_NESTED = 6
MOST_FOLDERS = 8
MOST_GIT_ENV = 64
TOO_COMPLEX = "a command line too complex to check (split it into shorter commands)"
# Where a command substitution (`$(…)`) stood in a word: what it makes is not known here.
MADE_UP = "\x00"
# A refspec whose branch its words do not say: a variable, a glob, a brace expansion.
UNREADABLE = re.compile(r"[$`*?\[{]")
# A substitution that names the default branch (`$(echo main)`).
MADE_UP_DEFAULT = re.compile(r"\$\([^()]*\b(?:main|master)\b|`[^`]*\b(?:main|master)\b")


@dataclass
class Push:
    """One `git push`: whether it forces, the branches it updates for sure, and whether which
    it updates is not known from its words (a refspec in a variable or a glob, configuration
    that cannot be read here, an alias, another repository's bare push). A bare push (no
    refspec, `HEAD`, or one a substitution makes) updates where its folder's branch goes:
    asked of git, unless the line made a branch and switched to it first (`bare_new`, joined
    by `&&`); a checkout or switch before it may have gone elsewhere too (`bare_also`)."""

    forced: bool = False
    branches: set[str] | None = field(default_factory=set)  # None: every branch.
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
    """What a line did to a folder's branch before a push: a branch it made and switched to
    (and where in the line), branches it made, and where other checkouts may have gone."""

    new: str = ""
    new_at: int = 0
    made: dict[str, int] = field(default_factory=dict)
    also: set[str] = field(default_factory=set)
    unknown: bool = False


def pushed_branch(refspec: str) -> str:
    """The branch a refspec updates on the remote: `main`, `HEAD:main`, `+refs/heads/main`,
    `:main` (deleting it) and `src:refs/heads/main` all update `main`."""
    destination = refspec.lstrip("+").rsplit(":", 1)[-1]
    for prefix in ("refs/heads/", "heads/"):
        destination = destination.removeprefix(prefix)
    return destination


def expanded(folder: str, path: str) -> str:
    """`path` from `folder`, `~` expanded; `folder` itself when it cannot be (an unknown user's
    home: the shell's `cd` fails there, and the command goes on where it was)."""
    try:
        return str(Path(folder) / Path(path).expanduser())
    except RuntimeError:
        return folder


def without_comments(text: str) -> str:
    """A command line without its comments, as bash reads them: a word that starts with `#`,
    unquoted, to the end of its line (`fix#12` is a word, `'#'` a quoted one)."""
    kept, quote, start, index = [], "", True, 0
    while index < len(text):
        char = text[index]
        if quote:
            kept.append(char)
            if char == "\\" and quote == '"' and index + 1 < len(text):
                kept.append(text[index + 1])
                index += 1
            elif char == quote:
                quote = ""
        elif char == "\\" and index + 1 < len(text):
            kept += [char, text[index + 1]]
            index += 1
            start = False
        elif char in "'\"":
            quote, start = char, False
            kept.append(char)
        elif char == "#" and start:
            while index + 1 < len(text) and text[index + 1] != "\n":
                index += 1
        else:
            kept.append(char)
            start = char in " \t\r" or char in SEPARATORS
        index += 1
    return "".join(kept)


def shell_commands(text: str) -> list[tuple[str, list[str]]] | None:
    """A command line as the shell splits it (comments dropped, quotes and backslashes
    resolved, a backslash-newline joined): its simple commands, each with what joined it to
    the one before ("&&", ";", a newline… "" for the first) and its words, `$(` marked in the
    word it stood in (MADE_UP); None when it does not parse (an unclosed quote)."""
    lexer = shlex.shlex(
        without_comments(text).replace("\\\n", ""), posix=True, punctuation_chars=SEPARATORS
    )
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        words = list(lexer)
    except ValueError:
        return None
    commands: list[tuple[str, list[str]]] = []
    current: list[str] = []
    joiner = ""
    for word in words:
        if word and all(char in SEPARATORS for char in word):
            if current and word.startswith("(") and current[-1].endswith("$"):
                current[-1] = current[-1][:-1] + MADE_UP  # `$(`: what it makes, not known.
            if current:
                commands.append((joiner, current))
                current, joiner = [], ""
            joiner += word
        else:
            current.append(word)
    if current:
        commands.append((joiner, current))
    return commands


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


def git_command(words: list[str], env: dict[str, str], cwd: str) -> Git | None:
    """A `git …` invocation read from its words; None without a command."""
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
            folder = expanded(folder, value)
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
    switches to one it does not name (`-`, `@{-1}`); None switches nothing (files)."""
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
    if command == "checkout" and ("--" in rest or len(named) > 1):
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


def push_of(words: list[str], env: dict[str, str], cwd: str) -> Push | None:
    """The `git push` these words run (git's own options first), or None."""
    git = git_command(words, env, cwd)
    if git is None:
        return None
    if git.rest[0] != "push":
        alias = git.config.get(f"alias.{git.rest[0].lower()}", "")
        if alias is None or "push" in alias.lower() or "alias.?" in git.config:
            return Push(unknown=True)  # An alias that may push: what, not known.
        return None
    push, positional, valued, target = Push(), [], False, False
    deleting = dry = tags = False
    for word in git.rest[1:]:
        if valued or target:
            valued = target = False
            continue
        if (at := min((i for i in (word.find(">"), word.find("<")) if i >= 0), default=-1)) >= 0:
            # A redirection, its own word or glued to one (`main>/dev/null`): not a refspec.
            target = not word[at:].strip("<>&|")  # Its file is the next word.
            word = "" if word[:at].isdigit() else word[:at]
            if not word:
                continue
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            push.forced = push.forced or name in {"--force", "--force-with-lease"}
            deleting = deleting or name == "--delete"
            dry = dry or name == "--dry-run"
            tags = tags or name == "--tags"
            if name in PUSH_ALL:
                push.branches = None
            valued = name in PUSH_VALUED and "=" not in word
            continue
        if word.startswith("-") and len(word) > 1:
            letters = word[1:] if word[1:].isalpha() else ""
            push.forced = push.forced or "f" in letters
            deleting = deleting or "d" in letters
            dry = dry or "n" in letters
            valued = word == "-o"
            continue
        positional.append(word)
    refspecs, every, unknown = git_config_pushes(git.config)
    push.unknown = unknown
    if every:
        push.branches = None
    # Every word that is not an option counts as a refspec but the remote's (its name, or a
    # variable that holds it, names no branch of its own).
    for index, word in enumerate([*positional, *refspecs]):
        push.forced = push.forced or word.startswith("+")
        if MADE_UP in word or "$(" in word or "`" in word:
            push.made_up = True  # As `HEAD`: its folder's branch (asked of git).
        elif index and UNREADABLE.search(word):
            push.unknown = True  # A variable, a glob, a brace expansion: not known.
        elif push.branches is not None:
            push.branches.add(pushed_branch(word))
    if dry:
        return push  # It updates nothing.
    if deleting or refspecs:
        push.unknown = push.unknown or push.made_up  # Which one it deletes: not known.
        return push  # What it updates is all in its words.
    if (
        (len(positional) <= 1 and not tags)
        or push.made_up
        or any(pushed_branch(word) in {"HEAD", "@"} for word in positional[1:])
    ):
        push.bare_in = git.folder  # Where its folder's branch goes.
        push.unknown = push.unknown or git.elsewhere  # Another repository's: not known.
    return push


def loose_pushes(command: str) -> list[Push]:
    """The pushes of text the shell would not parse (an unclosed quote), read loosely: in each
    stretch up to a separator, the words after the first `push` that follows its first `git`."""
    found = []
    for stretch in FLOOR_ENDS.split(command):
        if (git := GIT_WORD.search(stretch)) and (push := PUSH_WORD.search(stretch, git.end())):
            words = ["git", "push", *(w.strip("'\"`()\\") for w in stretch[push.end() :].split())]
            if found_push := push_of(words, {}, ""):
                found_push.bare_in = None  # No folder is known of it: nothing is asked of git.
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


def read_line(command: str, cwd: str, depth: int, found: list[Push]) -> str:
    """Each `git push` in a command line, as the shell would run it, into `found`: wherever
    `git` stands in a simple command (after `if`, `do`, `{`, `timeout 60`, `env A=b`…), in
    `sh -c '…'` and `eval`, after `cd`, with configuration given on the line, and the branch
    a line made and switched to. Read in one pass: a simple command is read up to the first
    `git`, shell `-c` or `eval` in it. "" once read in full, else why not (TOO_COMPLEX)."""
    if depth > MOST_NESTED:
        return TOO_COMPLEX
    commands = shell_commands(command)
    if commands is None:  # Not a command line the shell runs: read loosely.
        found += loose_pushes(command)
        return ""
    if sum(len(words) for _, words in commands) > MOST_WORDS:
        return TOO_COMPLEX
    env: dict[str, str] = {}  # git's own variables (GIT_…) the line sets.
    folder = cwd
    switched: dict[str, Switched] = {}
    broken, marks = 0, []  # How many joins were not `&&`, up to each simple command.
    for index, (joiner, words) in enumerate(commands):
        broken += bool(index) and joiner.replace("\n", "") != "&&"
        marks.append(broken)
        first = next((i for i, word in enumerate(words) if word not in KEYWORDS), len(words))
        rest = words[first:]
        if rest and all(ASSIGNMENT.match(word) for word in rest):
            env.update(git_variables(rest))  # `NAME=value` alone: for what follows.
        elif rest and rest[0] == "export":
            env.update(git_variables(rest[1:]))
        elif len(rest) > 1 and rest[0] == "cd" and cwd:
            folder = expanded(folder, rest[1])
        elif why := read_command(words, env, folder, index, marks, switched, depth, found):
            return why
        if len(env) > MOST_GIT_ENV:
            return TOO_COMPLEX
    return ""


def git_variables(words: list[str]) -> dict[str, str]:
    """git's own variables these `NAME=value` words set (others say nothing of a push)."""
    found = {}
    for word in words:
        name, has, value = word.partition("=")
        if has and name.startswith("GIT_") and ASSIGNMENT.match(word):
            found[name] = value
    return found


def read_command(
    words: list[str],
    env: dict[str, str],
    folder: str,
    index: int,
    marks: list[int],
    switched: dict[str, "Switched"],
    depth: int,
    found: list[Push],
) -> str:
    """One simple command, read up to its first `git`, shell `-c` or `eval`."""
    local: dict[str, str] = {}
    for position, word in enumerate(words):
        if ASSIGNMENT.match(word):
            local.update(git_variables([word]))
            continue
        program = word.rsplit("/", 1)[-1]
        if program == "git":
            here = {**env, **local} if local else env
            if push := push_of(words[position:], here, folder):
                if push.bare_in is not None and (made := switched.get(push.bare_in)):
                    if made.new and marks[index] == marks[made.new_at]:
                        push.bare_new = made.new
                    push.bare_also = frozenset(made.also)
                    push.unknown = push.unknown or made.unknown
                found.append(push)
            elif git := git_command(words[position:], here, folder):
                record_switch(git, index, marks, switched)
            return ""
        if program in SHELLS and (script := shell_script(words, position)) is not None:
            return read_line(script, folder, depth + 1, found)
        if program == "eval":
            return read_line(" ".join(words[position + 1 :]), folder, depth + 1, found)
    return ""


def record_switch(git: Git, index: int, marks: list[int], switched: dict[str, Switched]):
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
    made = switched.setdefault(git.folder, Switched())
    if kind == "new":
        switched[git.folder] = Switched(new=name, new_at=index, made=made.made)
    elif kind == "made":
        made.made[name] = index
    elif kind == "to" and name in made.made and marks[index] == marks[made.made[name]]:
        switched[git.folder] = Switched(new=name, new_at=made.made[name], made=made.made)
    elif kind in {"to", "renamed"}:
        made.also.add(name)
    else:
        made.unknown = True


def squashed(command: str) -> str:
    """A line without quotes, backslashes and line continuations, in lower case: where a word
    of it says `push` (or an alias) however it is quoted."""
    return command.replace("\\\n", "").translate(str.maketrans("", "", "'\"\\")).lower()


@functools.lru_cache(maxsize=32)
def reading(command: str, cwd: str) -> tuple[tuple[Push, ...], str]:
    """Each push of a command line, once each (see read_line), and why it was not read in full
    ("" when it was). A line no word of which says `push` or an alias pushes nothing."""
    words = squashed(command)
    if "push" not in words and "alias" not in words:
        return (), ""
    if len(command) > MOST_CHARS:
        return (), TOO_COMPLEX
    found: list[Push] = []
    why = read_line(command, cwd, 0, found)
    unique = tuple({push.key(): push for push in found}.values())
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
# `rm` with a recursive flag among its flags, then home or root: which flag is the recursive
# one is looked for ahead, not tried flag by flag (v0.1.42's form did, a long line in seconds).
RECURSIVE_DELETE = re.compile(
    r"\brm\s+(?=(?:-[a-zA-Z]+\s+)*?-[a-zA-Z]*[rR])(?:-[a-zA-Z]+\s+)+(/|~|\$HOME)/?(\s|$)"
)
DOWNLOAD = re.compile(r"\b(curl|wget)\b")
DOWNLOAD_PIPED = re.compile(r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(sh|bash|zsh)\b")
PIPE_ENDS = re.compile(r"[|\n]")


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
        except (OSError, subprocess.SubprocessError):
            continue
        name = done.stdout.strip() if done.returncode == 0 else ""
        if name and name != "HEAD":
            found.add(name.split("/", 1)[1] if ref == "@{push}" and "/" in name else name)
    return found


def pushes_default(command: str, cwd: str = "") -> bool:
    """Whether a command line may push to the default branch, as far as its words say (and,
    for a push that names no branch, the branch its folder is on: asked of git once a
    folder); never less than v0.1.42's rule said."""
    if floor_default(command) or too_complex(command, cwd):
        return True  # (Too complex: refused for that, with nothing asked of git.)
    asked: dict[str, set[str]] = {}
    for push in pushes(command, cwd):
        if push.branches is None or push.unknown or push.branches & DEFAULT_BRANCHES:
            return True
        if push.made_up and MADE_UP_DEFAULT.search(command):
            return True
        if push.bare_in is not None:
            if push.bare_new:
                on = {push.bare_new}
            else:
                if push.bare_in not in asked:
                    asked[push.bare_in] = current_branches(push.bare_in)
                on = asked[push.bare_in]
            if (on | push.bare_also) & DEFAULT_BRANCHES:
                return True
    return False


def pushes_forced(command: str, cwd: str = "") -> bool:
    return floor_force(command) or any(push.forced for push in pushes(command, cwd))


def matches(pattern: str) -> Callable[[str, str], bool]:
    found = re.compile(pattern)
    return lambda command, cwd="": found.search(command) is not None


# Commands a worker may not run on its own; it must ask the owner through the Router. Each
# check is given the command line and the folder it runs in.
DANGEROUS: list[tuple[Callable[[str, str], bool], str]] = [
    (matches(r"(^|[\s;&|(])sudo\s"), "sudo is not allowed"),
    (
        lambda command, cwd="": RECURSIVE_DELETE.search(command) is not None,
        "recursive delete of home or root",
    ),
    (too_complex, TOO_COMPLEX),
    (pushes_forced, "force push"),
    (pushes_default, "push to the default branch"),
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
        # The first reason that holds is said: the checks after it are not run.
        first = next((reason for found, reason in DANGEROUS if found(command, cwd)), None)
        reasons = [first] if first else []
        if gh_api_writes(command):
            reasons.append(
                "writing to GitHub through gh api (with -f/-F fields gh sends a POST: to read, "
                "add -X GET)"
            )
        if reads_slack and getattr(request, "possible_urls", None):
            reasons.append("network access from a worker that can read Slack")
        if reasons:
            return (
                f"Denied: {reasons[0]}. If the task really needs it, ask the owner with "
                'report(status="question").'
            )
    return None
