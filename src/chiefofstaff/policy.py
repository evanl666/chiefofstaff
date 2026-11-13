"""Which actions a session may take on its own. Pure functions: easy to test and to audit.

These rules are guardrails checked in Chief of Staff's permission callback, not a sandbox: a
worker with a shell can still reach anything its user account can unless OS sandboxing is on
(see SECURITY.md).
"""

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .roles import SLACK, WORKER

GITHUB_MCP = "github-mcp-server"

# `git [its own options] push …`, also inside `sh -c "…"` or `$(…)`: what it pushes, up to the
# end of that command.
GIT_PUSH = re.compile(
    r"\bgit\b(?:\s+(?:-[Cc]\s*\S+|--(?:git-dir|work-tree|namespace|config-env|exec-path|"
    r"super-prefix)(?:[=\s]\S+)?|-[-\w]+(?:=\S+)?))*\s+push\b([^;&|\n]*)"
)
# The default branch's usual names (a rule of text: it does not ask the repository).
DEFAULT_BRANCHES = frozenset({"main", "master"})
# `git push` options whose value is the next word; and those that push every branch.
PUSH_VALUED = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
PUSH_ALL = frozenset({"--all", "--mirror", "--branches"})


def pushed_branch(refspec: str) -> str:
    """The branch a refspec updates on the remote: `main`, `HEAD:main`, `+refs/heads/main`,
    `:main` (deleting it) and `src:refs/heads/main` all update `main`."""
    destination = refspec.lstrip("+").rsplit(":", 1)[-1]
    for prefix in ("refs/heads/", "heads/"):
        destination = destination.removeprefix(prefix)
    return destination


def pushes(command: str) -> list[tuple[bool, set[str] | None]]:
    """Each `git push` in a command line: whether it forces, and the remote branches it may
    update (None: every branch). Every word that is not an option counts as a refspec (the
    remote's name too: it names no branch of its own), quoted or not."""
    found: list[tuple[bool, set[str] | None]] = []
    for match in GIT_PUSH.finditer(command):
        forced, branches, valued = False, set(), False
        for word in match.group(1).split():
            word = word.strip("'\"`()\\")
            if valued or not word:
                valued = False
                continue
            if word.startswith("--"):
                name = word.split("=", 1)[0]
                forced = forced or name in {"--force", "--force-with-lease"}
                if name in PUSH_ALL:
                    branches = None
                valued = name in PUSH_VALUED and "=" not in word
                continue
            if word.startswith("-") and len(word) > 1:
                forced = forced or (word[1:].isalpha() and "f" in word[1:])
                valued = word == "-o"
                continue
            forced = forced or word.startswith("+")
            if branches is not None:
                branches.add(pushed_branch(word))
        found.append((forced, branches))
    return found


# Commands a worker may not run on its own; it must ask the owner through the Router.
DANGEROUS = [
    (re.compile(r"(^|[\s;&|(])sudo\s").search, "sudo is not allowed"),
    (
        re.compile(
            r"\brm\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(/|~|\$HOME)/?(\s|$)"
        ).search,
        "recursive delete of home or root",
    ),
    (lambda command: any(forced for forced, _ in pushes(command)), "force push"),
    (
        lambda command: any(to is None or to & DEFAULT_BRANCHES for _, to in pushes(command)),
        "push to the default branch",
    ),
    (
        re.compile(r"\bgh\s+pr\s+(merge|close|review|comment)\b").search,
        "merging, closing or commenting on PRs as the owner",
    ),
    (
        re.compile(r"\bgh\s+issue\s+(close|comment|delete|edit)\b").search,
        "changing issues as the owner",
    ),
    (
        re.compile(r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(sh|bash|zsh)\b").search,
        "piping a download into a shell",
    ),
    (
        re.compile(r"\b(mkfs|diskutil\s+erase|shutdown|reboot)\b").search,
        "system-level destructive command",
    ),
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
        reasons = [reason for found, reason in DANGEROUS if found(command)]
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
