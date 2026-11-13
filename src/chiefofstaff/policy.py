"""Which actions a session may take on its own. Pure functions, easy to test and to audit (but
for the branch a `git push` naming none goes to, which is asked of git).

These rules are guardrails checked in Chief of Staff's permission callback, not a sandbox: a
worker with a shell can still reach anything its user account can unless OS sandboxing is on
(see SECURITY.md).
"""

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
# Programs that run the command after them (their own options skipped), and shells whose `-c`
# runs a command line of its own.
WRAPPERS = frozenset({"command", "exec", "env", "nohup", "time", "nice", "builtin", "xargs"})
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
# A loose `git … push`, for text the shell would not parse (an unclosed quote).
GIT_PUSH = re.compile(r"\bgit\b[^;&|\n]*?\bpush\b([^;&|\n]*)")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass
class Push:
    """One `git push`: whether it forces, the branches it updates for sure, and whether which
    it updates is not known from its words (a bare push: where its folder's branch goes; one
    whose refspecs come from configuration that cannot be read here)."""

    forced: bool = False
    branches: set[str] | None = field(default_factory=set)  # None: every branch.
    bare_in: str | None = None  # The folder a refspec-less push runs in.
    unknown: bool = False


def pushed_branch(refspec: str) -> str:
    """The branch a refspec updates on the remote: `main`, `HEAD:main`, `+refs/heads/main`,
    `:main` (deleting it) and `src:refs/heads/main` all update `main`."""
    destination = refspec.lstrip("+").rsplit(":", 1)[-1]
    for prefix in ("refs/heads/", "heads/"):
        destination = destination.removeprefix(prefix)
    return destination


def shell_words(text: str) -> list[list[str]] | None:
    """A command line as the shell splits it (quotes and backslashes resolved, a
    backslash-newline joined): its simple commands, each a list of words; None when it does
    not parse (an unclosed quote)."""
    lexer = shlex.shlex(text.replace("\\\n", ""), posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return None
    commands, current = [], []
    for word in words:
        if word and all(char in ";&|()\n" for char in word):
            if current:
                commands.append(current)
            current = []
        else:
            current.append(word)
    if current:
        commands.append(current)
    return commands


def git_config_pushes(config: dict[str, str | None]) -> tuple[list[str], bool, bool]:
    """What configuration given on the command line says a push updates: refspecs
    (`remote.<name>.push`), every branch (`push.default=matching`), or not known (a value
    that is not in the command line)."""
    refspecs, every, unknown = [], False, False
    for key, value in config.items():
        key = key.lower()
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
    values; GIT_CONFIG_PARAMETERS, whose push settings are not read here: not known)."""
    found: dict[str, str | None] = {}
    if "push" in env.get("GIT_CONFIG_PARAMETERS", "").lower():
        found["remote.?.push"] = None
    for name, key in env.items():
        if name.startswith("GIT_CONFIG_KEY_"):
            found[key] = env.get("GIT_CONFIG_VALUE_" + name.removeprefix("GIT_CONFIG_KEY_"))
    return found


def push_of(words: list[str], env: dict[str, str], cwd: str) -> Push | None:
    """The `git push` these words run (git's own options first), or None."""
    config: dict[str, str | None] = dict(environment_config(env))
    folder, index = cwd, 1
    while index < len(words) and words[index].startswith("-"):
        word = words[index]
        name, _, value = word.partition("=")
        if name in GIT_VALUED and not value and index + 1 < len(words):
            index += 1
            value = words[index]
        if name == "-C" and value:
            folder = str(Path(folder) / Path(value).expanduser())
        elif name == "-c" and value:
            key, has, setting = value.partition("=")
            config[key] = setting if has else "true"
        elif name == "--config-env" and value:
            config[value.partition("=")[0]] = None  # Its value is in a variable: not known.
        index += 1
    if index >= len(words) or words[index] != "push":
        return None
    push, positional, valued, deleting = Push(), [], False, False
    for word in words[index + 1 :]:
        if valued:
            valued = False
            continue
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            push.forced = push.forced or name in {"--force", "--force-with-lease"}
            deleting = deleting or name == "--delete"
            if name in PUSH_ALL:
                push.branches = None
            valued = name in PUSH_VALUED and "=" not in word
            continue
        if word.startswith("-") and len(word) > 1:
            push.forced = push.forced or (word[1:].isalpha() and "f" in word[1:])
            deleting = deleting or (word[1:].isalpha() and "d" in word[1:])
            valued = word == "-o"
            continue
        positional.append(word)
    refspecs, every, unknown = git_config_pushes(config)
    push.unknown = unknown
    if every:
        push.branches = None
    # Every word that is not an option counts as a refspec (the remote's name too: it names no
    # branch of its own).
    for word in [*positional, *refspecs]:
        push.forced = push.forced or word.startswith("+")
        if push.branches is not None:
            push.branches.add(pushed_branch(word))
    if not refspecs and len(positional) <= 1 and not deleting:
        push.bare_in = folder  # Where its folder's branch goes.
    elif any(pushed_branch(word) in {"HEAD", "@"} for word in positional[1:]):
        push.bare_in = folder  # `HEAD`: the branch its folder is on.
    return push


def pushes(command: str, cwd: str = "") -> list[Push]:
    """Each `git push` in a command line, as the shell would run it (also in `sh -c '…'` and
    `eval`, after `cd`, with configuration given on the line). A rule of text, so best effort:
    what a command makes up as it runs (`$(…)`) is not known here."""
    found: list[Push] = []
    commands = shell_words(command)
    if commands is None:  # Not a command line the shell runs: read loosely.
        for match in GIT_PUSH.finditer(command):
            words = ["git", "push", *(w.strip("'\"`()\\") for w in match.group(1).split())]
            if push := push_of(words, {}, cwd):
                push.bare_in = None
                found.append(push)
        return found
    env: dict[str, str] = {}
    folder = cwd
    for words in commands:
        local = dict(env)
        while words and ASSIGNMENT.match(words[0]):
            name, _, value = words[0].partition("=")
            local[name] = value
            words = words[1:]
        if not words:
            env = local  # `NAME=value` alone: for what follows.
            continue
        if words[0] == "export":
            for word in words[1:]:
                if ASSIGNMENT.match(word):
                    name, _, value = word.partition("=")
                    env[name] = value
            continue
        if words[0] == "cd" and len(words) > 1 and cwd:
            folder = str(Path(folder) / Path(words[1]).expanduser())
            continue
        while words and (Path(words[0]).name in WRAPPERS or ASSIGNMENT.match(words[0])):
            if ASSIGNMENT.match(words[0]):
                name, _, value = words[0].partition("=")
                local[name] = value
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
        if not words:
            continue
        program = Path(words[0]).name
        if program in SHELLS:
            script = next(
                (
                    words[i + 1]
                    for i, word in enumerate(words[:-1])
                    if word.startswith("-") and not word.startswith("--") and "c" in word
                ),
                None,
            )
            if script is not None:
                found += pushes(script, folder)
            continue
        if program == "eval":
            found += pushes(" ".join(words[1:]), folder)
            continue
        if program == "git" and (push := push_of(words, local, folder)):
            found.append(push)
    return found


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
    for a push that names no branch, the branch its folder is on)."""
    for push in pushes(command, cwd):
        if push.branches is None or push.unknown or push.branches & DEFAULT_BRANCHES:
            return True
        if push.bare_in and current_branches(push.bare_in) & DEFAULT_BRANCHES:
            return True
    return False


def matches(pattern: str) -> Callable[[str, str], bool]:
    found = re.compile(pattern)
    return lambda command, cwd="": found.search(command) is not None


# Commands a worker may not run on its own; it must ask the owner through the Router. Each
# check is given the command line and the folder it runs in.
DANGEROUS: list[tuple[Callable[[str, str], bool], str]] = [
    (matches(r"(^|[\s;&|(])sudo\s"), "sudo is not allowed"),
    (
        matches(
            r"\brm\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(/|~|\$HOME)/?(\s|$)"
        ),
        "recursive delete of home or root",
    ),
    (lambda command, cwd: any(push.forced for push in pushes(command, cwd)), "force push"),
    (pushes_default, "push to the default branch"),
    (
        matches(r"\bgh\s+pr\s+(merge|close|review|comment)\b"),
        "merging, closing or commenting on PRs as the owner",
    ),
    (matches(r"\bgh\s+issue\s+(close|comment|delete|edit)\b"), "changing issues as the owner"),
    (
        matches(r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(sh|bash|zsh)\b"),
        "piping a download into a shell",
    ),
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
        reasons = [reason for found, reason in DANGEROUS if found(command, cwd)]
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
