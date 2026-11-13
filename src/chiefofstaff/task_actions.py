"""What happens to tasks: reports, cancellation, review, retries and follow-ups, and the
receipts and shareable report of finished work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

from . import prompts, receipts, threads
from .backend import stop_background
from .envelope import wrap
from .policy import within
from .roles import WORKER
from .sources.github import MISSING_TRIES, NOT_FOUND
from .sources.slack import describe
from .store import hours_ago, now
from .tasks import ADOPTED, ENDED, OPEN
from .workspaces import (
    CommandRun,
    Scope,
    base_of,
    changes,
    checkout_git,
    checkout_top,
    diff,
    file_times,
    package_name,
    same_version,
    share_files,
    snapshot,
    tree_names,
    untracked_work,
    version,
    worth_committing,
)

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)
CHECK_BACK_RETRY_MINUTES = 5
# Reads of a followed thread failed in a row before it shows as failing.
FAILING_READS = 3

PROJECT_URL = "https://github.com/evanl666/chiefofstaff"
GITHUB_REMOTE = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


STATUS_WORDS = {
    "queued": "Queued",
    "running": "In progress",
    "waiting": "Waiting for an answer",
    "review": "Done, ready for review",
    "done": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
}


def moved(before: list[dict], now: list[dict]) -> str:
    """Why checkouts' code is not what it was (commit or edits), or ""."""
    current = {checkout["path"]: checkout for checkout in now}
    for recorded in before:
        found = current.get(recorded["path"])
        if not found or not recorded.get("commit") or not found.get("commit"):
            continue
        if found["commit"] != recorded["commit"]:
            return (
                f"Out of date: the code changed since it was checked "
                f"({recorded['commit'][:7]} → {found['commit'][:7]})."
            )
        if not same_version(recorded, found):
            return "Out of date: the code was edited since it was checked."
    return ""


# A recent task's open PRs are read again this often (seconds), and for this long (hours).
PR_REFRESH = 180
PR_HOURS = 72
# Those closed (not merged) this many times less often: one may be reopened.
CLOSED_PR_PACE = 5


# A report line that says its worker opened the PR it names (not that someone else did).
OPENED_PR = re.compile(
    r"\b(opened|created|raised|submitted|filed|put up|sent out|pushed and opened)\b"
    r"|\bPR\b[^.\n]{0,20}\b(is )?(up|open(ed)?|created)\b|开了|创建了?|提了|提交了|已开",
    re.IGNORECASE,
)
SOMEONE_ELSE = re.compile(
    r"\b(another|other|someone|their|his|her|by @?\w+)\b|另一个|别的|其他|由.{0,12}负责|他们",
    re.IGNORECASE,
)


def claim_lines(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|(?<=[\u3002\uff01\uff1f])\s*|\n", text or "")
    return [part for part in parts if part.strip()]


def claims_opening(line: str) -> bool:
    return bool(OPENED_PR.search(line)) and not SOMEONE_ELSE.search(line)


def not_before(when: str | None, start: str) -> bool:
    """Whether ISO time `when` (GitHub's "…Z" or ours) is at or after `start`; False when
    either cannot be read."""
    try:
        return datetime.fromisoformat(when or "") >= datetime.fromisoformat(start)
    except ValueError:
        return False


def stamp(value: str | None) -> float:
    """An ISO time as seconds since the epoch (0 when it cannot be read)."""
    with suppress(ValueError, TypeError):
        return datetime.fromisoformat(value or "").timestamp()
    return 0.0


async def elsewhere_now(checkout: dict, top: Path, ended: str, head: str, branch: str = "") -> bool:
    """Whether a checkout moved to another line of history since a result (a worker's next task
    on a branch of its own), while the result's commit is still on a branch: not a change of
    that result's files. So did one now on another branch built on top of it, while the
    result's own branch (`branch`, "refs/heads/…", when known) still ends at its commit."""
    if not ended or ended == head:
        return False
    try:
        await checkout_git(checkout, str(top), "merge-base", "--is-ancestor", ended, head)
    except ValueError:
        with suppress(ValueError):
            holding = await checkout_git(
                checkout, str(top), "for-each-ref", "--contains", ended, "--format=%(refname)",
                "refs/heads",
            )  # fmt: skip
            return bool(holding.strip())
        return False
    with suppress(ValueError):
        on = (await checkout_git(checkout, str(top), "symbolic-ref", "-q", "HEAD")).strip()
        ending = await checkout_git(
            checkout, str(top), "for-each-ref", "--points-at", ended, "--format=%(refname)",
            "refs/heads",
        )  # fmt: skip
        results = set(ending.split()) & ({branch} if branch else set(ending.split()))
        return bool(results) and on not in results
    return False


# A result this recent may have landed while the owner was cancelling the work.
JUST_FINISHED_SECONDS = 60


def just_now(when: str | None) -> bool:
    """Whether ISO time `when` (a result's) is within the last minute."""
    try:
        ended = datetime.fromisoformat(when or "")
    except ValueError:
        return False
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ended).total_seconds() < JUST_FINISHED_SECONDS


# What a PR's standing is: a change in one of these is news on the owner's list.
PR_STANDING = ("state", "draft", "checks", "review", "merge_state", "awaiting", "head")
# What a PR is to a task (see TaskActions.classify_prs): the roles that make it its own work.
OWN_ROLES = frozenset({"created", "modified"})
# What a follow of a PR from before follows were tied to tasks is bound to when it is no one
# task's (several only name it, see TaskActions.legacy_follower): no task. Bound, so it is not
# worked out again on every look.
NO_TASK = "-"
# A shell command that pushes a branch to GitHub (see TaskActions.pushed_branches), and what
# its output says it did: "To <remote>", then a line per branch it updated there
# ("   1a2b3c4..5d6e7f8  fix -> fix", " + 1a2b3c4...5d6e7f8 fix -> fix (forced update)",
# " * [new branch]      fix -> fix"). Not one it refused ("! [rejected]") or found up to date.
PUSHES = re.compile(r"\bpush\b|\bgh\s+pr\s+create\b")
PUSHED_TO = re.compile(r"To (\S+)$")
PUSHED_REF = re.compile(
    r"\s*(?:\+\s*)?(?:[0-9a-f]{7,}\.\.\.?[0-9a-f]{7,}|\*\s+\[new (?:branch|reference)\])"
    r"\s+\S+\s+->\s+(?:refs/heads/)?(\S+)"
)


def dry_run(command: str) -> bool:
    """Whether a push in a shell command line only shows what it would do (`git push -n`, `-nf`,
    `--dry-run`; `gh pr create --dry-run`): its output reads like a push's, but nothing was."""
    for words in receipts.simple_commands(command):
        found = receipts.program(words).split()
        if found[:2] == ["git", "push"] and any(
            word == "--dry-run" or re.fullmatch(r"-[A-Za-z]*n[A-Za-z]*", word) for word in found[2:]
        ):
            return True
        if found[:3] == ["gh", "pr", "create"] and "--dry-run" in found[3:]:
            return True
    return False


def pr_role(pr: dict) -> str:
    """A PR's role in a task's receipts; those read before roles were kept, by their flags."""
    if pr.get("role"):
        return pr["role"]
    if pr.get("own"):
        return "created"
    return "reviewed" if pr.get("reviewed") else "referenced"


def followed_up(pr: dict) -> bool:
    """Whether a task's PR is read again as it changes: one it made, changed or reviewed, or one
    it was asked about (its CI is what the task's tests are checked against). Not one it only
    mentions (its report brought it up)."""
    # Read before a PR's role was kept: "about" was what it was asked about (and the first
    # its report brought up).
    return pr_role(pr) != "referenced" or bool(pr.get("asked", pr.get("about")))


# What a PR waits for while it is followed, in the owner's words, by where it stands (see
# pr_status): what brings it on.
PR_WAITS_FOR = {
    "draft": "review",
    "changes requested": "changes",
    "merge conflicts": "conflicts resolved",
    "CI failing": "CI",
    "awaiting approval": "approval",
    "CI running": "CI",
    "ready to merge": "merge",
    "approved": "merge",
    "in review": "review",
}
# A status a worker gave a run that says it is over: nothing left to wait for there.
# A run its worker declared that is still going, by the status it gave ("running", "queued"):
# only such a run is something to monitor. Any other word (passed, deployed, an empty status)
# is no promise of news, and an accepted task must not stay open for ever on it.
RUN_GOING = re.compile(
    r"\b(running|queued|pending|in[ -]progress|started|starting|scheduled|waiting|submitted|"
    r"executing|deploying|building)\b",
    re.IGNORECASE,
)
# Kinds of files that are what a task worked with or on the way, never what it delivers:
# screenshots, mockup images, data dumps, logs.
NOT_DELIVERED = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|bmp|csv|tsv|log|txt|out|err|json|jsonl|parquet|zip|tar|gz)$",
    re.IGNORECASE,
)
# Under a run, for diagnosis: its logs, pods, task instances (as static/text.js linkKind).
UNDER_RUN = re.compile(
    r"/(logs?|pods?|nodes|taskinstance|task_instance|containers?)(/|$)|"
    r"[?&](task_id|try_number|pod)=",
    re.IGNORECASE,
)


def deliverable_kind(url: str, declared: bool = False) -> tuple[str, str]:
    """Whether a link is a deliverable, and what it is called by default: ("doc", "Google
    Doc"), ("flow", "Airflow · nightly"), or ("", "") for what is not one. A document is one
    page of a documents site (a Google Doc with its id, a Notion or Confluence page, a Quip
    doc), never the site's home page; a flow is one workflow run (an Airflow DAG run, a Flyte
    execution; a GitHub Actions run only when its worker `declared` it: one in a report is a
    PR's CI), never its logs or pods. Any other page its worker declared counts as a document
    when it is a page of its own, not a site's root nor a screenshot, a CSV or a log. Mirrors
    static/text.js linkKind."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "", ""
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return "", ""  # A file on this computer (a mockup, a CSV): its task's files.
    host = parts.hostname.removeprefix("www.").lower()
    path = parts.path.rstrip("/")
    last = path.rsplit("/", 1)[-1]
    if host == "github.com":
        found = re.match(r"/[^/]+/([^/]+)/actions/runs/\d+$", path)
        return ("flow", f"CI run · {found.group(1)}") if found and declared else ("", "")
    if host.endswith(".slack.com") or NOT_DELIVERED.search(path):
        return "", ""
    if host == "docs.google.com":
        if not (found := re.search(r"/d/(?:e/)?([\w-]{6,})", path)):
            return "", ""  # Google Docs' home, or a folder of them: no document.
        names = {"spreadsheets": "Sheet", "presentation": "Slides", "forms": "Form"}
        return "doc", names.get(path.split("/")[1], "Google Doc")
    if host.endswith(("notion.so", "notion.site")):
        return ("doc", "Notion page") if re.search(r"[0-9a-f]{32}$", last) else ("", "")
    if "atlassian.net" in host or host.startswith("confluence"):
        page = re.search(r"/pages/\d+|/display/[^/]+/[^/]+|/wiki/x/\w+", path)
        return ("doc", "Confluence page") if page else ("", "")
    if host.endswith("quip.com"):
        return ("doc", "Quip doc") if re.match(r"/\w{8,}", path) else ("", "")
    if host == "paper.dropbox.com" or host == "coda.io":
        return ("doc", "Doc") if re.match(r"/(?:doc|d)/[^/]+", path) else ("", "")
    if UNDER_RUN.search(parts.path + ("?" + parts.query if parts.query else "")):
        return "", ""
    query = dict(parse_qsl(parts.query))
    dag = query.get("dag_id") or (
        found.group(1) if (found := re.search(r"/dags/([^/?#]+)", path)) else ""
    )
    if "airflow" in host or dag:
        return ("flow", f"Airflow · {clip(dag, 28)}") if dag else ("", "")
    execution = re.search(r"/executions?/(?:[^/]+/[^/]+/)?([^/?#]+)$", path)
    if "flyte" in host or "/console/projects/" in path:
        return ("flow", f"Flyte · {clip(execution.group(1), 24)}") if execution else ("", "")
    if not declared or not path:
        return "", ""  # Only mentioned, or a site's root: no deliverable.
    if re.search(r"/(?:runs?|flow-runs?|dagruns?|executions?|workflows?|pipelines?)/[^/]+", path):
        return "flow", f"{host.split('.')[0]} · {clip(last, 28)}"
    return "doc", f"{clip(host, 28)} · {clip(last, 24)}"


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


SPLIT_UNDO_MINUTES = 10


# A title's last part when it only says how to write the work (its language, tone, length), not
# what it delivers: "Review app#7 (in English, human-sounding comments)". That belongs in the
# instructions. Bracketed at its end, or after its last separator.
TITLE_NOTE = re.compile(
    r"\s*(?:[(\uff08](?P<bracketed>[^()\uff08\uff09]+)[)\uff09]"
    r"|(?:\s[-\u2013\u2014\u00b7]\s|[,;:]\s|[\uff0c\uff1b\uff1a])(?P<after>[^,;:\uff0c\uff1b\uff1a]+))"
    r"\s*$"
)
WRITING_STYLE = re.compile(
    r"\b(?:(?:in|into)\s+(?:plain\s+|simple\s+|natural\s+|good\s+)?)?"
    r"(?:english|chinese|mandarin|japanese|korean|french|german|spanish)\b"
    r"|\bhuman[- ]?(?:sounding|like|written|readable|tone)\b|\bnot\s+robotic\b|\bnatural(?:ly)?\b"
    r"|\b(?:casual|formal|polite|friendly|concise|brief|terse|warm|professional)(?:ly)?\b"
    r"|\b(?:plain\s+language|no\s+jargon|short\s+and\s+sweet)\b"
    r"|用?(?:英文|中文|英语|汉语|日文|日语|韩文|韩语)|像人(?:写|说)?|口语化?|简洁|简短|自然|礼貌|正式|语气"
    r"|人性化|不要?(?:太)?(?:机器|生硬)"
)
# What else such a note says that is no deliverable: what is written, and joining words.
STYLE_FILLER = re.compile(
    r"\b(?:comments?|replies|reply|review|text|words|messages?|written|writing|write|sounding|"
    r"style|keep|it|be|make|use|with|and|or|the|a|an|in|please|very|more|less|bit)\b"
    r"|评论|回复|消息|写|说|的|地|得|用|要|请|和|并且?|一点|一些|风格|方式",
    re.IGNORECASE,
)
# A title of work that writes in a language: the language is what it delivers.
TRANSLATES = re.compile(r"translat|locali[sz]|翻译|译成|本地化", re.IGNORECASE)


def style_note(title: str) -> str:
    """The end of a title that only says how to write the work ("(in English, human-sounding
    comments)", "— 简洁一点"), or "": what to write goes in a title, how in the instructions.
    Not a note that names anything else ("(comments only)", "(security)"), nor one of work
    that translates."""
    found = TITLE_NOTE.search(title)
    if not found or TRANSLATES.search(title):
        return ""
    note = found.group("bracketed") or found.group("after") or ""
    if not WRITING_STYLE.search(note.lower()):
        return ""
    rest = STYLE_FILLER.sub(" ", WRITING_STYLE.sub(" ", note.lower()))
    return "" if re.search(r"\w", rest) else found.group(0).strip()


def deliverable_title(title: str) -> str:
    """A task's title, checked alike when a task is made, renamed or split: it must say what
    the task delivers. Not "111", "A" or "Fix issue": a word of what or where ("Lint", "Fix
    CI", in any language), a PR or issue with what to do with it ("Fix #12"), or two
    different Chinese, Japanese or Korean characters, which are already a word ("改日期")."""
    from .envelope import clean_title

    title = clean_title(title.strip())
    letters = sum(char.isalpha() for char in title)
    if len(set(CJK.findall(title))) >= 2:
        return title
    # Chinese, Japanese and Korean by their characters (above): "哈哈" is one, said twice.
    named = [
        word
        for word in TITLE_WORD.findall(title.lower())
        if word not in VAGUE and not CJK.search(word)
    ]
    target = any("#" in word for word in named) and letters >= 3
    if target or any(sum(char.isalpha() for char in word) >= 2 for word in named):
        return title
    if letters >= 3:
        # "Fix issue", "Check PR": no word of what or where.
        raise ValueError(
            f"“{title}” does not say what it is about. Name the action and its target, e.g. "
            "“Fix failing integration tests in the notification service”."
        )
    raise ValueError(
        "Give it a title saying what it delivers, in words, e.g. “Add a retry to the upload step”."
    )


# A title's words, in any script: "#12" and "owner/repo#12" (a target) count as one each.
TITLE_WORD = re.compile(r"[\w./-]*#\d+|[^\W_][\w'-]*")
# Words that say no more than "there is work": a title of only these (and numbers) names
# nothing. Not "tests", "docs", "login" and the like: those say where.
VAGUE = {
    "a", "an", "the", "this", "that", "it", "its", "my", "our", "some", "something", "stuff",
    "thing", "things", "misc", "item", "items", "todo", "task", "tasks", "work", "job",
    "fix", "fixes", "fixing", "issue", "issues", "bug", "bugs", "problem", "problems",
    "error", "errors", "check", "checks", "checking", "pr", "prs", "update", "updates",
    "change", "changes", "review", "help", "do", "make", "handle", "look", "into", "at",
    "on", "for", "of", "to", "in", "and", "or", "please", "new", "quick", "small", "minor",
    "follow", "up", "follow-up", "same", "again", "more", "other", "another",
}  # fmt: skip


# A character that is a word of its own (Han, kana, Hangul).
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def ran_in(command: str) -> str:
    """Where a command line runs, when it says so (`cd folder && …`); "" otherwise."""
    found = re.match(r"\s*cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*(?:&&|;)", command or "")
    return found.group(1).strip("\"'") if found else ""


# What a result says of its branch before its PR was opened: "not pushed", "nothing pushed",
# "no PR (opened)", "did not open a PR". Only those (not "pre-push", "pushed", "open").
# Options of `git clone` and `git worktree add` that take a value (`-b main`).
GIT_VALUED = {
    "-b", "-B", "--branch", "-o", "--origin", "--depth", "-c", "--config", "--reference",
    "--reference-if-able", "--separate-git-dir", "-u", "--upload-pack", "--template",
    "--filter", "-j", "--jobs", "--shallow-since", "--shallow-exclude", "--reason", "--orphan",
}  # fmt: skip


def works_in(command: str, path: str) -> bool:
    """Whether a shell command names this folder as where it works: its path (also as
    ~/…/name, ./name or ../name), or its name as a `cd`, `git -C`, `--git-dir` or `--work-tree`
    target or the start of a path under it ("name/src/x.py"). Generous: a task's own work there
    must not be taken for none."""
    path = path.rstrip("/")
    name = re.escape(Path(path).name)
    end = r"(?=[/\s'\";&|)]|$)"
    return bool(
        re.search(
            rf"{re.escape(path)}{end}|(?<![\w.-])(?:~|\.\.?)?/(?:[^\s'\";&|]*/)?{name}{end}|"
            rf"(?:\bcd|-C|--git-dir=?|--work-tree=?)\s*[\"']?{name}{end}|"
            rf"(?<![\w./~-]){name}/",
            command,
        )
    )


def made_folders(command: str) -> list[str]:
    """The folders a shell command's `git clone` or `git worktree add` makes (by name)."""
    found = []
    for part in re.split(r"&&|\|\||[;|\n]", command):
        try:
            words = shlex.split(part)
        except ValueError:
            continue
        while words[:1] and "=" in words[0]:  # GIT_DIR=… git …
            words = words[1:]
        if words[:1] != ["git"]:
            continue
        words = words[1:]
        while words[:1] and words[0].startswith("-"):  # git -C dir …
            words = words[2:] if words[0] in {"-C", "-c"} else words[1:]
        if words[:1] == ["clone"]:
            words, least = words[1:], 1
        elif words[:2] == ["worktree", "add"]:
            words, least = words[2:], 0
        else:
            continue
        plain, skip = [], False
        for word in words:
            if skip:
                skip = False
            elif word in GIT_VALUED:
                skip = True
            elif not word.startswith("-"):
                plain.append(word)
        if len(plain) > least:
            found.append(Path(plain[least]).name)
        elif plain:  # A clone into the folder its repository is named by.
            found.append(Path(plain[0].rstrip("/")).name.removesuffix(".git"))
    return found


NOT_PUSHED = re.compile(
    r"(?<![\w-])(?:"
    r"(?:not|never)\s+(?:yet\s+)?(?:been\s+)?pushed(?:\s+yet)?"
    r"|nothing\s+(?:was\s+|has\s+been\s+|is\s+)?pushed(?:\s+yet)?"
    r"|unpushed"
    r"|(?:(?:did|do|have|has)(?:n't|\s+not)|will\s+not|won't)\s+(?:yet\s+)?"
    r"(?:push(?:ed)?|open(?:ed)?)(?:\s+(?:it|anything|the\s+branch))?"
    r"(?:\s+(?:or|and|nor)\s+(?:push(?:ed)?|open(?:ed)?))?(?:\s+(?:a|any|the)\s+"
    r"(?:PR|pull\s+request))?"
    r"|no\s+(?:PR|pull\s+request)s?(?:\s+(?:opened|open|created|raised))?(?:\s+yet)?"
    r"(?=\s*(?:$|[.,;:)!\u2026]|(?:and|or|but)\b))"
    r"|(?:PR|pull\s+request)\s+not\s+(?:yet\s+)?(?:opened|created|raised)(?:\s+yet)?"
    r")(?![\w-])",
    re.IGNORECASE,
)
# What a result says of its PR before it was merged: "(not merged)", "awaiting review",
# "merging is yours"; with "open" in "It is open and not merged" (see status_only).
NOT_MERGED = re.compile(
    r"(?<![\w-])(?:not\s+(?:yet\s+)?merged(?:\s+yet)?|unmerged"
    r"|(?:awaiting|waiting\s+for|ready\s+(?:for|to))\s+(?:your\s+)?"
    r"(?:review|merge|merging|approval|be\s+merged)"
    r"|merg(?:e|ing)\s+is\s+(?:yours|up\s+to\s+you)|for\s+you\s+to\s+merge)(?![\w-])",
    re.IGNORECASE,
)
# What a result says of following its PR until it is merged ("still following", "watching it
# for CI"): over once it is merged.
FOLLOWING_PR = re.compile(
    r"(?<![\w-])(?:(?:I\s+am|I'm|I\s+will|I'll|will)\s+)?(?:keep(?:ing)?\s+)?"
    r"(?:following|watching|monitoring)(?:\s+(?:it|the\s+PR|its\s+CI|CI|the\s+checks|checks))?"
    r"(?:\s+(?:for|until)\s+(?:CI|checks|reviews?|comments|approval|the\s+merge|"
    r"it\s+(?:merges|is\s+merged)))?(?![\w-])",
    re.IGNORECASE,
)
STALE_ONCE_MERGED = re.compile(f"{NOT_MERGED.pattern}|{FOLLOWING_PR.pattern}", re.IGNORECASE)
# The words that may go with such a status ("It is", "the branch", "and"): nothing more.
STATUS_FILLER = re.compile(
    r"\b(?:and|or|but|nor|yet|so|far|still|also|it|its|this|that|the|branch|PR|pull|request|"
    r"is|was|are|has|been|i|we|now)\b|[^\w\s]",
    re.IGNORECASE,
)


def pr_names(label: str) -> re.Pattern:
    """How a result names one PR ("owner/repo#72"): its URL, "repo#72", "#72", "PR 72"."""
    repo, _, number = label.rpartition("#")
    name = re.escape(repo.rsplit("/", 1)[-1])
    return re.compile(
        rf"https?://github\.com/{re.escape(repo)}/pull/{number}\b"
        rf"|(?<![\w#])(?:{re.escape(repo)}|{name})?#{number}\b"
        rf"|\b(?:PR|pull\s+request)\s+{number}\b",
        re.IGNORECASE,
    )


def about_pushing(text: str) -> bool:
    """Whether a criterion is about its branch not being pushed or its PR not opened yet."""
    return bool(NOT_PUSHED.search(re.sub(r"`[^`]*`", "", text)))


def status_only(clause: str, phrases: re.Pattern, names: re.Pattern | None = None) -> bool:
    """Whether a clause says nothing but a status `phrases` finds ("not pushed", "It is open
    and not merged"), about the PR `names` finds or "it" (not code: `is_open`)."""
    text = re.sub(r"`[^`]*`", " code ", clause)
    rest = phrases.sub(" ", text)
    named = bool(names and names.search(rest))
    if rest == text and not named:
        return False
    if names:
        rest = re.sub(r"\bopen\b", " ", names.sub(" ", rest), flags=re.IGNORECASE)
    return not re.search(r"\w", STATUS_FILLER.sub(" ", rest))


def not_pushed(clause: str) -> bool:
    """Whether a clause says only that its branch is not pushed or its PR not opened."""
    return status_only(clause, NOT_PUSHED)


def capitalized(text: str) -> str:
    """A sentence whose start was dropped, starting with a capital: "the CI is green" (not
    `code`, nor "iOS")."""
    return text[0].upper() + text[1:] if re.match(r"[a-z]+\b", text) else text


def without_status(text: str, only: Callable[[str], bool]) -> str:
    """A result without the clauses, sentences and remarks in parentheses that say nothing but
    a status that no longer holds (`only`): "Committed abc, not pushed." keeps "Committed abc."
    Everything else stays as it is."""
    lines, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if fenced or line.lstrip().startswith(("```", "|")):
            lines.append(line)
            continue
        lead = re.match(r"\s*(?:[-*+]\s+|\d+[.)]\s+)?", line)
        start = lead.end() if lead else 0
        body = re.sub(
            r"\s*\(([^()]*)\)", lambda found: "" if only(found.group(1)) else found.group(0),
            line[start:],
        )  # fmt: skip
        kept: list[str] = []
        # The next sentence kept starts a sentence of its own, and what began it was dropped
        # ("Not merged; the CI is green." keeps "The CI is green.").
        opening, dropped = True, False
        for sentence in re.split(r"(?<=[.!?;])\s+", body):
            parts = re.split(r"(,\s+|\s+[\u2013\u2014]\s+)", sentence)
            clauses = parts[::2]
            if not any(only(clause) for clause in clauses):
                out = sentence
            else:
                out = ""
                for index, clause in enumerate(clauses):
                    if not only(clause):
                        out += (parts[2 * index - 1] if out else "") + clause
                if out:
                    end = re.search(r"[.!?;:\u2026]*$", sentence)
                    ending = end.group(0) if end else ""
                    out = out if re.search(r"[.!?;:]$", out) else out.rstrip(" ,") + ending
            if not out:
                if not sentence.rstrip().endswith(";"):
                    # It ended the sentence: so does what was kept of it ("Tests pass;
                    # not merged." keeps "Tests pass.").
                    if kept and kept[-1].endswith(";"):
                        tail = sentence.rstrip()[-1:]
                        kept[-1] = kept[-1][:-1] + (tail if tail in ".!?\u2026" else "")
                    opening = True
                dropped = True
                continue
            kept.append(capitalized(out) if opening and dropped else out)
            opening, dropped = bool(re.search(r"[.!?\u2026]$", out)), False
        if kept or not line.strip():
            lines.append(line[:start] + " ".join(kept) if kept else line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# What a follow-up's report says about the result before it ("This is unchanged from before.",
# "57c5e01 (new):"): it means nothing where that earlier result is not shown (a PR's text).
FOLLOW_UP_MARK = re.compile(
    r"\s+\((?:new|newly added|unchanged|updated|changed|as before|same as before|"
    r"(?:unchanged|carried over) from (?:before|the earlier result))\)",
    re.IGNORECASE,
)
FOLLOW_UP_SENTENCE = re.compile(
    r"(?:(?:this|that|it|these|those|the rest|everything else)\s+(?:is|are|was|were|stays?|"
    r"remains?)\s+)?(?:still\s+)?(?:unchanged|as before|the same as before)"
    r"(?:\s+from\s+(?:before|the earlier result|earlier|last time))?[.!]?",
    re.IGNORECASE,
)


def without_follow_up(text: str) -> str:
    """A result without what only the reader of its follow-up needs: what is new, or the same
    as in the result before ("(new)", "This is unchanged from before.")."""
    lines, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if fenced or line.lstrip().startswith("```"):
            lines.append(line)
            continue
        lead = re.match(r"\s*(?:[-*+]\s+|\d+[.)]\s+)?", line)
        start = lead.end() if lead else 0
        sentences = re.split(r"(?<=[.!?])\s+", FOLLOW_UP_MARK.sub("", line[start:]))
        body = " ".join(s for s in sentences if not FOLLOW_UP_SENTENCE.fullmatch(s.strip()))
        if body.strip() or not line[start:].strip():
            lines.append(line[:start] + body.strip() if body.strip() else line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def cited_command(command: str, evidence: str = "") -> str:
    """A check's command as a PR's description cites it, in one line: never a here-document's
    body (its blank lines end the code span on GitHub). Of a command on several lines, the run
    its evidence names, else its test run, else its first line (the cut marked)."""
    if "\n" not in command.strip():
        return command.strip()
    for snippet in receipts.QUOTED.findall(evidence or ""):
        if receipts.kind(snippet) or (
            len(receipts.words(snippet)) >= 2 and receipts.names(snippet, command)
        ):
            return snippet.strip()
    if runs := receipts.test_runs(command):
        return receipts.shown(runs[0])
    return command.strip().splitlines()[0].rstrip().removesuffix("\u2026") + " \u2026"


def code_span(text: str) -> str:
    """`text` as inline code, also when it holds a backtick."""
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def pr_title(subjects: list[str], limit: int = 100) -> str:
    """A PR's title from the subjects of its commits: the one, or as many as fit (and how many
    more there are)."""
    subjects = [
        subject
        for subject in dict.fromkeys(subject.strip() for subject in subjects)
        if subject and not re.match(r"(fixup|squash|amend)!", subject)
    ]
    if not subjects:
        return ""
    title = subjects[0]
    for count, subject in enumerate(subjects[1:], 1):
        rest = len(subjects) - count - 1
        if len(f"{title}; {subject}" + (f" (+{rest} more)" if rest else "")) > limit:
            return f"{title} (+{len(subjects) - count} more)"
        title = f"{title}; {subject}"
    return title


def pr_status(pr: dict) -> str:
    """Where a PR stands in a word or two: the same rule as the UI's prStatus (board.js), worst
    first (changes requested before failing CI: the author acts on the review either way)."""
    if pr.get("state") == "MERGED":
        return "merged"
    if pr.get("state") == "CLOSED":
        return "closed"
    if pr.get("draft"):
        return "draft"
    if pr.get("review") == "CHANGES_REQUESTED":
        return "changes requested"
    if pr.get("merge_state") == "DIRTY":
        return "merge conflicts"
    if pr.get("checks") == "failing":
        return "CI failing"
    if pr.get("awaiting"):
        return "awaiting approval"
    if pr.get("checks") == "pending":
        return "CI running"
    if pr.get("merge_state") in {"CLEAN", "HAS_HOOKS"}:
        return "ready to merge"
    return "approved" if pr.get("review") == "APPROVED" else "in review"


# What a delivered task's own PR asks for, most pressing first (board.js PR_MARKERS).
PR_MARKERS = {
    "changes requested": "pr-changes",
    "merge conflicts": "pr-conflicts",
    "CI failing": "pr-failing",
    "ready to merge": "mergeable",
}
# Its own PR to fix (board.js PR_TO_FIX): the owner asks its worker to, accepted or not.
PR_TO_FIX = frozenset({"pr-changes", "pr-conflicts", "pr-failing"})


class TaskActions:
    """Changes tasks on behalf of sessions and the owner."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.bus = runtime.bus
        self.states = runtime.states
        self.tasks = runtime.tasks
        self.delivery = runtime.delivery
        self.workspaces = runtime.workspaces
        self.waking: set[str] = set()  # Check-backs being delivered now.
        # Workers whose finished tasks are being checked for changes now (check_fresh), and
        # those changed again meanwhile (checked once more after).
        self.checking: set[str] = set()
        self.recheck: set[str] = set()
        # Tasks' starts being recorded (record_base): a result reported meanwhile waits for
        # its start, or it would have no share.
        self.basing: dict[str, asyncio.Future] = {}
        # An accepted task stays listed while something is followed up for it.
        self.tasks.monitored = lambda task: bool(self.monitoring(task))
        # The one task each follow tied to no task is for (legacy_follower), as worked out in
        # this pass: kept until the event loop runs anything else.
        self.legacy: dict[tuple[str, str], str] = {}
        self.bind_legacy_follows()

    def about(self, task: dict, detail: str) -> str:
        """What a piece of feedback was about, for learning: a Slack-reading worker's text may
        carry Slack content, so its task's title is all that is kept."""
        row = self.store.find_session(task["session_id"]) or {}
        if row.get("slack"):
            return task["title"]
        return f"{task['title']}: {detail[:600]}"

    def receipts(self, task: dict) -> dict | None:
        """Observed commands, changes and PRs for a task, beside its report."""
        commands = self.store.all(
            """SELECT c.command, c.kind, c.exit_code, c.ended_at FROM commands c
            JOIN runs r ON r.id=c.run_id WHERE r.task_id=? ORDER BY c.id""",
            (task["id"],),
        )
        # Claims are saved with the report; what was observed follows from git and gh.
        runs = self.store.all(
            """SELECT id, started_at, evidence, criteria FROM runs WHERE task_id=?
            AND (evidence!='' OR criteria!='') ORDER BY started_at DESC""",
            (task["id"],),
        )
        latest = runs[0] if runs else None
        evidence = json.loads(latest["evidence"]) if latest and latest["evidence"] else None
        if evidence and evidence.get("prs"):
            # Only the PRs it is about: one its report merely mentions (another worker's) is a
            # link in its report, not one of its chips or flags.
            evidence["prs"] = [pr for pr in evidence["prs"] if pr.get("about", True)]
        criteria = self.tasks.criteria(task)
        # A check learned from a mistake that still fails when the worker said done.
        failing = self.runtime.checks.flags(task["id"])
        if not commands and not evidence and not criteria and not failing:
            return None
        claims = json.loads(latest["criteria"]) if latest and latest["criteria"] else []
        earlier, since = self.earlier_claims(runs) if criteria else ([], "")
        said = self.store.get(f"saidchanged:{latest['id']}") if latest else None
        found = receipts.summarize(
            commands, evidence, task["result"] or task["progress"], criteria, claims,
            earlier=earlier, since=since, said_changed=None if said is None else said == "1",
        )  # fmt: skip
        found["flags"] += failing
        if found.get("tests"):
            # Tests of its own work: run where it changed code. A task with no checkout ran
            # someone else's tests (a clone it read): shown as where it ran them, not a pass.
            found["tests"]["own"] = bool((evidence or {}).get("checkouts"))
            found["tests"]["cwd"] = ran_in(found["tests"]["command"]) or (
                (self.store.find_session(task["session_id"]) or {}).get("cwd") or ""
            )
        return found

    @staticmethod
    def earlier_claims(runs: list[dict]) -> tuple[list[dict], str]:
        """What an earlier result reported on its criteria (the latest that did), for those a
        later report left out (a follow-up's), and since when what backs them must have run to
        count as verified: the latest run's start when the code changed since ("" if not)."""
        for run in runs[1:]:
            if not run["criteria"]:
                continue
            then = json.loads(run["evidence"] or "{}").get("checkouts", [])
            later = json.loads(runs[0]["evidence"] or "{}").get("checkouts", [])
            since = (runs[0]["started_at"] or "") if moved(then, later) else ""
            return json.loads(run["criteria"]), since
        return [], ""

    async def check_fresh(self, session_id: str, others: bool = True):
        """Mark the evidence of a worker's finished tasks out of date once its code changed:
        a "verified" must be about the code there is now. In a folder of the owner's, other
        workers' finished tasks there too (this one's work may have changed their files)."""
        row = self.store.find_session(session_id)
        if not row or row["role"] != WORKER:
            return
        if session_id in self.checking:
            self.recheck.add(session_id)  # One check at a time; once more when it ends.
            return
        self.checking.add(session_id)
        try:
            await self.check_own(row)
            while session_id in self.recheck:
                self.recheck.discard(session_id)
                await self.check_own(row)
        finally:
            self.checking.discard(session_id)
        if others:
            live = self.workspaces.checkouts(row)
            for path in {checkout["path"] for checkout in live if checkout.get("shared")}:
                for other in self.sharing(path):
                    if other != session_id:
                        await self.check_fresh(other, others=False)

    async def check_own(self, row: dict):
        """check_fresh for one worker's own finished tasks."""
        session_id = row["id"]
        live = {checkout["path"]: checkout for checkout in self.workspaces.checkouts(row)}
        async with self.states[session_id].checkouts:  # Not while its worktrees are removed.
            for task in self.tasks.for_session(session_id):
                if task["status"] not in {"review", "done"}:
                    continue
                run = self.store.one(
                    """SELECT id, evidence FROM runs WHERE task_id=? AND evidence!=''
                    ORDER BY started_at DESC""",
                    (task["id"],),
                )
                evidence = json.loads(run["evidence"]) if run else {}
                if not evidence or evidence.get("stale"):
                    continue
                ended = json.loads(self.store.get(f"taskend:{task['id']}") or "{}")
                if ended:
                    # Its result's own files, since its result: not a later task's work.
                    reason = await self.own_files_changed(task, live, ended)
                else:
                    reason = await self.changed_since(evidence, live)
                if reason:
                    self.stale(task["id"], reason)

    async def own_files_changed(self, task: dict, live: dict[str, dict], ended: dict) -> str:
        """Why a finished task's own files are not what its result left (or "")."""
        for path, end in ended.items():
            checkout = live.get(path)
            if not checkout or self.not_its(task, checkout):
                continue
            if not (scope := self.scope(task, checkout)):
                continue
            with suppress(Exception):
                now_ = await snapshot(checkout)
                if not (now_ and now_["tree"]) or now_["tree"] == end["tree"]:
                    continue
                top = await checkout_top(checkout)
                if await elsewhere_now(
                    checkout, top, end["commit"], now_["commit"], end.get("branch", "")
                ):
                    continue  # On another branch now: its result is on its own, unchanged.
                differ = await tree_names(checkout, top, end["tree"], now_["tree"])
                if hit := sorted(set(differ) & await share_files(checkout, scope)):
                    return (
                        "Out of date: its files changed since it was checked ("
                        + ", ".join(hit[:3])
                        + (f" and {len(hit) - 3} more" if len(hit) > 3 else "")
                        + ")."
                    )
        return ""

    @staticmethod
    async def changed_since(evidence: dict, live: dict[str, dict]) -> str:
        for recorded in evidence.get("checkouts", []):
            checkout = live.get(recorded["path"])
            if not checkout or not recorded.get("commit"):
                continue
            with suppress(Exception):
                current = await version(checkout)
                if current and (
                    reason := moved([recorded], [{**current, "path": recorded["path"]}])
                ):
                    return reason
        return ""

    def stale(self, task_id: str, reason: str):
        """Its latest evidence no longer holds (the code or a decision changed)."""
        run = self.store.one(
            """SELECT id, evidence FROM runs WHERE task_id=? AND evidence!=''
            ORDER BY started_at DESC""",
            (task_id,),
        )
        if not run:
            return
        evidence = json.loads(run["evidence"])
        if evidence.get("stale"):
            return
        evidence |= {"stale": reason, "stale_at": now()}
        self.store.execute(
            "UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run["id"])
        )
        self.publish(self.tasks.get(task_id))

    def unstale(self, task_id: str):
        """Its evidence holds again (the change it was out of date for changed nothing)."""
        run = self.store.one(
            """SELECT id, evidence FROM runs WHERE task_id=? AND evidence!=''
            ORDER BY started_at DESC""",
            (task_id,),
        )
        if run and (evidence := json.loads(run["evidence"])).pop("stale", None):
            evidence.pop("stale_at", None)
            self.store.execute(
                "UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run["id"])
            )

    def collect_evidence(
        self, task_id: str, keep_stale: bool = False, ended: bool = False
    ) -> Coroutine[Any, Any, None]:
        """After a task is reported done: its checkouts' changes and its PRs' state.
        `keep_stale`: not after a report (a PR opened), so what was checked did not change.
        `ended`: its result was just reported: what its checkouts hold now is what it made.
        Asked for now (it may run later, in the background): found out of date after this
        call (a decision changed), it stays out of date. What it finds is about the run that
        was its latest then (not one a follow-up started meanwhile)."""
        run_id = self.store.value(
            "SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (task_id,)
        )
        return self.collected(task_id, keep_stale, ended, now(), run_id or "")

    async def collected(
        self, task_id: str, keep_stale: bool, ended: bool, started: str, run_id: str = ""
    ):
        task = self.tasks.get(task_id)
        row = self.store.find_session(task["session_id"])
        if not row:
            return
        if ended:
            await self.record_end(task)  # Its result: its share ends here.
            row = self.store.find_session(task["session_id"]) or row  # Its own clones too.
        checkouts = []
        # Not while its worktrees are removed (a worker closed as it reported).
        async with self.states[row["id"]].checkouts:
            for checkout in self.workspaces.checkouts(row):
                if self.not_its(task, checkout):
                    continue
                with suppress(Exception):
                    if found := await changes(checkout, self.scope(task, checkout)):
                        checkouts.append(found)
        # Offered to merge only as read now: a PR GitHub could not answer for keeps its last
        # read (see read_prs), which may be of a head pushed over since.
        read = now()
        prs = await self.read_prs(task, checkouts)
        await self.offer_merge(task, prs, since=read)
        run = (
            self.store.one("SELECT id, evidence FROM runs WHERE id=?", (run_id,))
            if run_id
            else self.store.one(
                "SELECT id, evidence FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
                (task_id,),
            )
        )
        if run:
            evidence = {"checkouts": checkouts, "prs": prs, "at": now(), "prs_at": time.time()}
            before = json.loads(run["evidence"]) if keep_stale and run["evidence"] else {}
            if reason := before.get("stale") or moved(before.get("checkouts", []), checkouts):
                # The checks were about the code as it was then, not as it is now.
                evidence |= {"checkouts": before["checkouts"], "stale": reason}
            there = self.store.value("SELECT evidence FROM runs WHERE id=?", (run["id"],))
            latest = json.loads(there) if there else {}
            if latest.get("stale") and latest.get("stale_at", "") >= started:
                # Found out of date while this looked (a decision changed): that still holds.
                evidence |= {"stale": latest["stale"], "stale_at": latest["stale_at"]}
            self.store.execute(
                "UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run["id"])
            )
            self.publish(self.tasks.get(task_id))

    def scope(self, task: dict, checkout: dict) -> Scope | None:
        """This task's share of a checkout its worker used for several tasks (see Scope); None
        for a task from before its start was recorded: the checkout's work, as before."""
        found = self.store.get(f"taskbase:{task['id']}")
        if not found:
            return None
        base = json.loads(found)
        touched = set(json.loads(self.store.get(f"touched:{task['id']}") or "[]"))
        # Its result's snapshot: what it delivered (later tasks' work is theirs). Not while it
        # is open: then its share is up to now.
        ended = json.loads(self.store.get(f"taskend:{task['id']}") or "{}")
        end = ended.get(checkout["path"]) if task["status"] not in OPEN else None
        others: set[str] = set()
        commands = self.shell_runs(task) if checkout.get("shared") else []
        earlier: dict[str, list[float]] = {}
        other_runs: list[CommandRun] = []
        if checkout.get("shared"):
            for row in self.store.all(
                "SELECT key, value FROM kv WHERE key LIKE 'touched:%' AND key!=?",
                (f"touched:{task['id']}",),
            ):
                others.update(json.loads(row["value"] or "[]"))
            # Each earlier result's own write times (the last is `end`'s, when finished).
            results = json.loads(self.store.get(f"taskends:{task['id']}") or "[]")
            for result in results[:-1] if end else results:
                mtimes = (result["ended"].get(checkout["path"]) or {}).get("mtimes") or {}
                for name, when in mtimes.items():
                    if when is not None:
                        earlier.setdefault(name, []).append(when)
            other_runs = self.other_shell_runs(task, checkout["path"], base.get("at", 0))
        return Scope(
            base.get("commits", {}).get(checkout["path"], ""),
            base.get("at", 0),
            touched,
            start=base.get("trees", {}).get(checkout["path"], ""),
            end=end,
            others=others,
            head=base.get("heads", {}).get(checkout["path"], ""),
            large=set(base.get("large", {}).get(checkout["path"], [])),
            commands=commands,
            mtimes=(end or {}).get("mtimes"),
            earlier=earlier,
            other_runs=other_runs,
            gaps=[
                (
                    gap["end"][checkout["path"]],
                    gap["start"][checkout["path"]],
                    gap.get("end_commits", {}).get(checkout["path"], ""),
                    gap.get("start_commits", {}).get(checkout["path"], ""),
                    float(gap.get("at") or 0),
                )
                for gap in json.loads(self.store.get(f"taskgaps:{task['id']}") or "[]")
                if checkout["path"] in gap["end"] and checkout["path"] in gap["start"]
            ],
        )

    async def record_restart(self, task: dict, resumed: bool = False):
        """A finished task taken up again (a follow-up, the owner's chat): when other work
        changed its checkouts since its result, that gap is kept (see Scope.gaps). `resumed`:
        its worker came back to it from other work: where its checkouts are now is where what
        it changes for it from here starts (see changed_since_result)."""
        ended = json.loads(self.store.get(f"taskend:{task['id']}") or "{}")
        row = self.store.find_session(task["session_id"])
        if not ended or not row or (not resumed and self.own_change_pending(task)):
            return  # What changed since its result is its own: not a gap.
        at = time.time()  # Taken up again now: what it commits from here on is its own.
        result = json.dumps({p: e["tree"] for p, e in sorted(ended.items())})
        checked = json.loads(self.store.get(f"restarted:{task['id']}") or "{}")
        once = isinstance(checked, dict) and checked.get("result") == result
        if once and not resumed:
            return  # Checked once for this result: later messages are about its own work.
        gaps = json.loads(self.store.get(f"taskgaps:{task['id']}") or "[]")
        now_trees, now_commits = {}, {}
        for checkout in self.workspaces.checkouts(row):
            with suppress(Exception):
                if (found := await snapshot(checkout)) and found["tree"]:
                    now_trees[checkout["path"]] = found["tree"]
                    now_commits[checkout["path"]] = found["commit"]
        if resumed:
            trees = {p: {"tree": t, "commit": now_commits[p]} for p, t in now_trees.items()}
            self.store.set(f"resumed:{task['id']}", json.dumps({"at": now(), "trees": trees}))
        if once:
            return
        end_trees = {path: end["tree"] for path, end in ended.items()}
        end_commits = {path: end["commit"] for path, end in ended.items()}
        if any(now_trees.get(path, tree) != tree for path, tree in end_trees.items()):
            gap = {"end": end_trees, "start": now_trees, "end_commits": end_commits,
                   "start_commits": now_commits, "at": at}  # fmt: skip
            gaps = [*gaps, gap][-5:]
            self.store.set(f"taskgaps:{task['id']}", json.dumps(gaps))
        self.store.set(f"restarted:{task['id']}", json.dumps({"result": result, "at": at}))

    def shell_runs(self, task: dict, since: str = "") -> list[CommandRun]:
        """The shell commands its worker ran for this task (since a time), with when."""
        rows = self.store.all(
            "SELECT c.command, c.started_at, c.ended_at FROM commands c JOIN runs r "
            "ON r.id=c.run_id WHERE r.task_id=? AND c.started_at>=? ORDER BY c.id",
            (task["id"], since),
        )

        return [
            CommandRun(row["command"], stamp(row["started_at"]), stamp(row["ended_at"]))
            for row in rows
        ]

    def sharing(self, path: str, closed: bool = False) -> list[str]:
        """The open workers that work in this folder of the owner's (`closed`: those closed
        too)."""
        return [
            row["id"]
            for row in self.store.all(
                "SELECT id, repos FROM sessions WHERE role=? AND repos LIKE ? "
                "AND (? OR closed_at IS NULL)",
                (WORKER, f"%{json.dumps(path)[1:-1]}%", closed),
            )
            if any(
                checkout.get("path") == path and checkout.get("shared")
                for checkout in json.loads(row["repos"] or "[]")
            )
        ]

    def other_shell_runs(self, task: dict, path: str, since: float) -> list[CommandRun]:
        """Other tasks' shell commands in the same folder of the owner's, since this task was
        taken up (a little before: one that ran as it started)."""
        sessions = self.sharing(path, closed=True)  # A closed worker's commands ran too.
        if not sessions:
            return []
        start = datetime.fromtimestamp(max(since - 60, 0), UTC).isoformat()
        marks = ",".join("?" * len(sessions))  # Only placeholders go into the SQL text.
        rows = self.store.all(
            "SELECT c.command, c.started_at, c.ended_at FROM commands c JOIN runs r "  # noqa: S608
            f"ON r.id=c.run_id WHERE r.task_id!=? AND c.session_id IN ({marks}) "
            "AND c.started_at>=? ORDER BY c.id DESC LIMIT 2000",
            (task["id"], *sessions, start),
        )
        return [
            CommandRun(row["command"], stamp(row["started_at"]), stamp(row["ended_at"]))
            for row in rows
        ]

    def result_at(self, task: dict) -> str:
        """When its latest result's end was recorded ("" for none)."""
        results = json.loads(self.store.get(f"taskends:{task['id']}") or "[]")
        return results[-1]["at"] if results else ""

    def keep_result_base(self, task: dict):
        """It changed its own code since its result, and asks the owner about it: what it
        reports once answered is a new result, counted from where it was then (the answer does
        not record a new start past those changes; see record_chat_base)."""
        self.store.set(f"ownchange:{task['id']}", self.result_at(task) or "-")

    def own_change_pending(self, task: dict) -> bool:
        """Whether its own changes since its latest result wait for a new result."""
        return self.store.get(f"ownchange:{task['id']}") == (self.result_at(task) or "-")

    async def record_chat_base(self, task: dict):
        """Its checkouts when the owner asks about a finished task in chat: what changes from
        then on, by this task, is a new result (see changed_since_result). Not when it changed
        its code since its result already: those changes are part of that new result."""
        row = self.store.find_session(task["session_id"])
        if not row or self.own_change_pending(task):
            return
        chat = json.loads(self.store.get(f"chatbase:{task['id']}") or "{}")
        results = json.loads(self.store.get(f"taskends:{task['id']}") or "[]")
        if chat.get("at") and (not results or chat["at"] > results[-1]["at"]):
            # Asked again before a new result: what it changed since the first ask still
            # counts (it is not the base of the next one).
            await self.record_restart(task)
            return
        trees = {}
        for checkout in self.workspaces.checkouts(row):
            with suppress(Exception):
                if (found := await snapshot(checkout)) and found["tree"]:
                    trees[checkout["path"]] = found
        self.store.set(f"chatbase:{task['id']}", json.dumps({"at": now(), "trees": trees}))
        await self.record_restart(task)

    async def changed_since_result(self, task: dict) -> bool:
        """Whether this task changed its checkouts since the owner asked about it in chat or
        answered its question, or its worker came back to it from other work (else since its
        result): then what it reports is a new result. Not another task's changes since, nor
        the owner's, nor what running code left behind. A checkout it did not have then (a
        worktree added to change what it had only read) counts from where it started, with
        the files its edits wrote or its commands named."""
        ended = json.loads(self.store.get(f"taskend:{task['id']}") or "{}")
        results = json.loads(self.store.get(f"taskends:{task['id']}") or "[]")
        latest = results[-1]["at"] if results else ""
        # The latest base since its result (one from before it: that result is where it starts).
        bases = [
            found
            for key in ("chatbase", "resumed")
            if (found := json.loads(self.store.get(f"{key}:{task['id']}") or "{}")).get("at")
            and found["at"] > latest
        ]
        base = max(bases, key=lambda found: found["at"]) if bases else {}
        row = self.store.find_session(task["session_id"])
        if not row or not (base.get("at") or ended):
            return False
        trees = base.get("trees") or {}
        known = trees if base.get("at") else ended
        since = base.get("at") or latest
        touched = set(json.loads(self.store.get(f"touched:{task['id']}") or "[]"))
        for checkout in self.workspaces.checkouts(row):
            before = trees.get(checkout["path"]) or ended.get(checkout["path"])
            added = bool(not before and checkout["path"] not in known and checkout.get("base_ref"))
            with suppress(Exception):
                now_ = await snapshot(checkout)
                if added and now_ and now_["tree"]:
                    before = {"tree": checkout["base_ref"]}
                if not (before and now_ and before["tree"] and now_["tree"]):
                    continue
                if before["tree"] == now_["tree"]:
                    continue
                top = await checkout_top(checkout)
                differ = await tree_names(checkout, top, before["tree"], now_["tree"])
                # Not what running code left behind (new `__pycache__/`, a `.coverage`).
                names = [n for n, status in differ.items() if status != "A" or untracked_work([n])]
                if checkout.get("shared") or added:
                    # In a folder of the owner's, or one it had not had: only what its edits
                    # wrote or its commands named.
                    commands = self.shell_runs(task, since if added else base.get("at", ""))
                    scope = Scope(touched=touched, commands=commands)
                    names = [n for n in names if scope.wrote(top, n) or scope.named(top, n)]
                if names:
                    return True
        return False

    def not_its(self, task: dict, checkout: dict) -> bool:
        """Whether a checkout its worker has is another task's work only: split off this one
        (see split_away), or a clone or worktree another of its tasks made (`git clone` in a
        later task's turn) that this one neither ran commands in nor edited."""
        if self.split_away(task, checkout):
            return True
        made = checkout.get("base") in {"a worktree it made itself", "a clone it made itself"}
        if not made:
            return False
        path = checkout["path"].rstrip("/")
        name = Path(path).name
        makers = {
            found["task_id"]
            for found in self.store.all(
                "SELECT c.command, r.task_id FROM commands c JOIN runs r ON r.id=c.run_id "
                "WHERE c.session_id=?",
                (task["session_id"],),
            )
            if name in made_folders(found["command"])
        }
        if not makers or task["id"] in makers:
            return False
        ran = any(
            works_in(found["command"], path)
            for found in self.store.all(
                "SELECT c.command FROM commands c JOIN runs r ON r.id=c.run_id WHERE "
                "c.session_id=? AND r.task_id=?",
                (task["session_id"], task["id"]),
            )
        )
        edited = json.loads(self.store.get(f"touched:{task['id']}") or "[]")
        return not ran and not any(within(file, path) for file in edited)

    def split_away(self, task: dict, checkout: dict) -> bool:
        """Whether a clone or worktree its worker made is the work split off this task: that
        task's `git worktree add`/`git clone` made it, or its commands worked there (`cd
        …/whisper-wt && git commit`: made before the split) and this task's did not (beyond
        making it): counted there, not here too."""
        split = self.store.get(f"splitoff:{task['id']}")
        made = checkout.get("base") in {"a worktree it made itself", "a clone it made itself"}
        if not split or not made:
            return False
        path = checkout["path"].rstrip("/")
        name = Path(path).name

        def commands(task_id: str) -> list[str]:
            return [
                found["command"]
                for found in self.store.all(
                    "SELECT c.command FROM commands c JOIN runs r ON r.id=c.run_id WHERE "
                    "c.session_id=? AND r.task_id=?",
                    (task["session_id"], task_id),
                )
            ]

        theirs = commands(split)
        if any(name in made_folders(command) for command in theirs):
            return True
        mine = [c for c in commands(task["id"]) if name not in made_folders(c)]
        return any(works_in(c, path) for c in theirs) and not any(works_in(c, path) for c in mine)

    async def adopt_clones(self, row: dict) -> dict:
        """A worker's own clones (see Workspaces.own_clones) become its checkouts."""
        if row["role"] != WORKER:
            return row
        with suppress(Exception):
            if made := await self.workspaces.own_clones(row):
                checkouts = self.workspaces.checkouts(row)
                self.runtime.update_session(row["id"], repos=json.dumps([*checkouts, *made]))
                return self.store.session(row["id"])
        return row

    async def record_end(self, task: dict):
        """Its checkouts as its result left them: its share ends there (a later task on the
        same branch or folder does not add to it)."""
        if job := self.basing.pop(task["id"], None):
            with suppress(Exception):
                await asyncio.shield(job)
        row = self.store.find_session(task["session_id"])
        if not row or not self.store.get(f"taskbase:{task['id']}"):
            return
        row = await self.adopt_clones(row)
        ended = {}
        base = json.loads(self.store.get(f"taskbase:{task['id']}") or "{}")
        async with self.states[row["id"]].checkouts:  # Not while its worktrees are removed.
            for checkout in self.workspaces.checkouts(row):
                if self.not_its(task, checkout):
                    continue
                with suppress(Exception):
                    if (found := await snapshot(checkout)) and found["tree"]:
                        with suppress(ValueError):
                            # The branch it is on (see elsewhere_now).
                            on = await checkout_git(
                                checkout, checkout["path"], "symbolic-ref", "-q", "HEAD"
                            )
                            found["branch"] = on.strip()
                        if checkout.get("shared") and (
                            start := base.get("trees", {}).get(checkout["path"])
                        ):
                            # When each file it changed was last written, as of its result: a
                            # later task writing it again does not move it out of this one's
                            # time.
                            top = await checkout_top(checkout)
                            names = list(await tree_names(checkout, top, start, found["tree"]))
                            found["mtimes"] = await asyncio.to_thread(file_times, top, names[:2000])
                        ended[checkout["path"]] = found
        if ended:
            self.store.set(f"taskend:{task['id']}", json.dumps(ended))
            self.store.delete(f"restarted:{task['id']}")  # A new result: checked again.
            # Each result's end, kept: a split gives each result its own share.
            history = json.loads(self.store.get(f"taskends:{task['id']}") or "[]")
            history = [*history, {"at": now(), "ended": ended}][-6:]
            self.store.set(f"taskends:{task['id']}", json.dumps(history))

    def split_changes(self, task: dict, made: dict, latest: dict, first_at: str) -> dict:
        """A split's checkouts: the new task's share runs from the end of the result the task
        keeps to the end of the one that moves (with the commands run for it); the task's ends
        where its kept result did. What undoing it puts back."""
        history = json.loads(self.store.get(f"taskends:{task['id']}") or "[]")
        kept = [h for h in history if h["at"] < latest["created_at"]]
        moved_end = [h for h in history if h["at"] >= latest["created_at"]]
        before = json.loads(self.store.get(f"taskend:{task['id']}") or "null")
        undo = {"ended": before}
        if kept and (moved_end or before):
            start, end = kept[-1], (moved_end[-1]["ended"] if moved_end else before)
            at = datetime.fromisoformat(start["at"]).timestamp()
            ended = start["ended"]
            self.store.set(
                f"taskbase:{made['id']}",
                json.dumps(
                    {"at": at, "commits": {p: e["commit"] for p, e in ended.items()},
                     "trees": {p: e["tree"] for p, e in ended.items()},
                     "heads": {p: e["commit"] for p, e in ended.items()},
                     "large": {p: e.get("large", []) for p, e in ended.items()}}
                ),
            )  # fmt: skip
            self.store.set(f"taskend:{made['id']}", json.dumps(end))
            self.store.set(f"taskend:{task['id']}", json.dumps(ended))
        run = self.store.one(
            "SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (made["id"],)
        )
        if run:
            # Its tests and commands: those run since it was asked for.
            moved = self.store.all(
                "SELECT c.id FROM commands c JOIN runs r ON r.id=c.run_id WHERE r.task_id=? "
                "AND c.started_at>=?",
                (task["id"], first_at),
            )
            ids = [row["id"] for row in moved]
            for identifier in ids:
                self.store.execute(
                    "UPDATE commands SET run_id=? WHERE id=?", (run["id"], identifier)
                )
            undo |= {"commands": ids}
        touched = self.store.get(f"touched:{task['id']}")
        if touched:
            self.store.set(f"touched:{made['id']}", touched)
        news = json.loads(self.store.get(f"news:{task['id']}") or "null")
        if news and news.get("at", "") >= latest["created_at"]:
            # News since the result that moves is about that work: it moves too (the moved
            # report itself, filed as news, is the new task's result now).
            self.store.delete(f"news:{task['id']}")
            if news.get("text") != latest["text"].strip()[:2000]:
                self.store.set(f"news:{made['id']}", json.dumps(news))
        # Their ends moved: a follow-up on either is checked again for work in between.
        self.store.delete(f"restarted:{task['id']}")
        self.runtime.background(self.collect_evidence(made["id"]))
        self.runtime.background(self.collect_evidence(task["id"]))
        return undo

    async def record_base(self, session_id: str, task_ids: list[str], at: float):
        """Where its checkouts are when a worker takes a task up: that task's changes are what
        comes after (an earlier task's commits on the same branch are not its own)."""
        row = self.store.find_session(session_id)
        task_ids = [task_id for task_id in task_ids if not self.store.get(f"taskbase:{task_id}")]
        if not row or not task_ids:
            return
        row = await self.adopt_clones(row)
        commits, trees, heads, large = {}, {}, {}, {}
        first = self.tasks.for_session(session_id)[:1]
        # Its first task's work starts where its branch did (in a folder of the owner's, where
        # the folder is now); a later one's where it is now: its commit and all its files.
        later = not (first and all(task_id == first[0]["id"] for task_id in task_ids))
        for checkout in self.workspaces.checkouts(row):
            if not (later or checkout.get("shared")):
                continue
            with suppress(Exception):
                if found := await snapshot(checkout):
                    if found["tree"]:
                        trees[checkout["path"]] = found["tree"]
                    heads[checkout["path"]] = found["commit"]
                    if found.get("large"):
                        large[checkout["path"]] = found["large"]
                    if later:
                        commits[checkout["path"]] = found["commit"]
        for task_id in task_ids:
            if not self.store.get(f"taskbase:{task_id}"):
                self.store.set(
                    f"taskbase:{task_id}",
                    json.dumps(
                        {
                            "at": at,
                            "commits": commits,
                            "trees": trees,
                            "heads": heads,
                            "large": large,
                        }
                    ),
                )

    def needs_acceptance(self, task: dict) -> bool:
        """Whether a finished result waits for the owner to accept it: every one does, a report
        that only informs (research, an analysis, a review of someone's PR) as much as a code
        change. Reading it is not accepting it, and nothing accepts it for them (its worker
        going idle or on to other work, time passing, its PR merged): only Accept settles it,
        and Request changes sends it back to its worker."""
        return task["status"] == "review"

    def own_pr_standing(self, task: dict, prs: list[dict] | None = None) -> tuple[str, str]:
        """Where a delivered task's own open PRs that it follows stand, as Home places it
        (board.js ownPrStanding): ("pr-changes" | "pr-conflicts" | "pr-failing" | "mergeable",
        why), the most pressing first; ("", "") when none asks for anything. Unreadable ones
        count, as they stood when last read. `prs`: its receipts' PRs, when read already."""
        row = self.store.find_session(task["session_id"])
        if not row:
            return "", ""
        followed = {
            f"{found.group(1)}#{found.group(2)}".lower()
            for key in self.followed_for(task, self.store.follows(row["id"]))
            if (found := threads.GITHUB_PR.fullmatch(key))
        }
        if prs is None:
            prs = (self.receipts(task) or {}).get("prs") or []
        prs = [pr for pr in prs if pr.get("own") and pr["label"].lower() in followed]
        ranked = [(PR_MARKERS[word], pr) for pr in prs if (word := pr_status(pr)) in PR_MARKERS]
        if not ranked:
            return "", ""
        marker, pr = min(ranked, key=lambda item: list(PR_MARKERS.values()).index(item[0]))
        short = pr["label"].split("/", 1)[-1]
        failing = (
            f" ({', '.join(pr['failing'])})" if marker == "pr-failing" and pr.get("failing") else ""
        )
        return marker, {
            "pr-changes": f"Changes requested on {short}",
            "pr-conflicts": f"Merge conflicts on {short}",
            "pr-failing": f"CI failing on {short}{failing}",
            "mergeable": f"Ready to merge: {short}",
        }[marker]

    def status_words(self, task: dict) -> str:
        """A task's status for the Router: a finished one waits for the owner's Accept; one
        accepted says what it is still monitored for."""
        if task["status"] == "review":
            return "review, to accept"
        if task["status"] == "waiting" and self.store.get(f"stopped:{task['id']}"):
            return "waiting, stopped by the owner"
        if task["status"] == "done" and (watched := self.monitoring_words(task)):
            return f"done, monitoring {watched}"
        return task["status"]

    def resume_stopped(self, session_id: str):
        """Its worker is at work again on a task it was stopped on: one it was told to go on
        with since (in its chat, by the Router, a nudge: an instruction naming it, see
        `Runtime.took_up`) runs again. Work on anything else (another task, PR news) leaves
        a stopped task stopped."""
        for task in self.tasks.for_session(session_id):
            if (
                task["status"] == "waiting"
                and self.store.get(f"stopped:{task['id']}")
                and self.store.get(f"goingon:{task['id']}")
            ):
                self.publish(self.tasks.move(task["id"], "running", progress=""))

    def touched(self, task_id: str, paths: list[str]):
        """Files a worker's edits wrote for a task (in a folder of the owner's, only those are
        its changes)."""
        if not task_id or not paths:
            return
        known = json.loads(self.store.get(f"touched:{task_id}") or "[]")
        added = [os.path.realpath(path) for path in paths if path]
        merged = list(dict.fromkeys([*known, *added]))[-500:]
        if merged != known:
            self.store.set(f"touched:{task_id}", json.dumps(merged))

    async def read_prs(
        self, task: dict, checkouts: list[dict] | None = None, again: bool = False
    ) -> list[dict]:
        """The task's PRs as GitHub has them now: open or merged, checks, review, and what each
        is to the task (`role`, see pr_roles). `checkouts`: its changes as just found (whose
        commits are on which branch), else as its receipts last had them. `again`: a periodic
        refresh, which reads only what the task made, changed or reviewed: a PR it only
        mentions keeps how it stood when its result was read, and so does one that cannot be
        read now. A PR GitHub said several times does not exist (an example in a report, a
        typo) is not asked for again."""
        prs = []
        earlier = {pr["label"].lower(): pr for pr in self.previous_prs(task["id"])}
        opened = self.opened_prs(task["id"])
        for repo, number in threads.PR_URL.findall(" ".join(self.pr_urls(task)))[:5]:
            label = f"{repo}#{number}".lower()
            if int(self.store.get(f"prmissing:{label}") or 0) >= MISSING_TRIES:
                continue
            known = earlier.get(label)
            if again and known and not followed_up(known) and label not in opened:
                prs.append({key: value for key, value in known.items() if key != "role"})
                continue
            try:
                view = json.loads(
                    await self.runtime.github.gh(
                        "pr",
                        "view",
                        f"https://github.com/{repo}/pull/{number}",
                        "--json",
                        "number,url,state,statusCheckRollup,reviewDecision,title,isDraft,"
                        "mergeStateStatus,headRefOid,headRefName,createdAt,author",
                    )
                )
            except Exception as exc:
                logger.info("Could not read %s#%s for receipts.", repo, number, exc_info=True)
                if NOT_FOUND.search(str(exc)):
                    missing = int(self.store.get(f"prmissing:{label}") or 0) + 1
                    self.store.set(f"prmissing:{label}", str(missing))
                if known:
                    # Not read now: it stays as it was last read (what it is to the task, how
                    # it stood), not dropped from its receipts until GitHub answers again.
                    prs.append({key: value for key, value in known.items() if key != "role"})
                continue
            self.store.delete(f"prmissing:{label}")
            # Read: a follow that could not read the PR itself can again.
            self.runtime.github.read_ok(threads.normalize(f"github:{repo}#{number}"), whole=False)
            rollup = view.get("statusCheckRollup") or []
            prs.append(
                {
                    "label": f"{repo}#{number}",
                    "url": view.get("url") or f"https://github.com/{repo}/pull/{number}",
                    "state": view.get("state", ""),
                    "title": (view.get("title") or "")[:200],
                    "draft": bool(view.get("isDraft")),
                    "checks": receipts.checks_state(rollup),
                    "failing": receipts.failing_checks(rollup)[:5],
                    "awaiting": receipts.awaiting_approval(rollup)[:3],
                    "checks_started": receipts.checks_started(rollup),
                    "review": view.get("reviewDecision") or "",
                    "merge_state": view.get("mergeStateStatus") or "",
                    "head": view.get("headRefOid") or "",
                    "branch": view.get("headRefName") or "",
                    "created": view.get("createdAt") or "",
                    "author": ((view.get("author") or {}).get("login") or "").lower(),
                    "at": now(),
                }
            )
            if view.get("title"):
                self.runtime.remember_pr_title(f"{repo}#{number}", view["title"])
        roles, about = await self.classify_prs(task, prs, checkouts)
        reviews = self.review_states(task["session_id"], task["id"])
        # When each last changed (its state, checks, review, mergeability), not when it was
        # read: "2h ago" on the owner's list says how long it has stood like this.
        before = earlier
        for pr in prs:
            pr["role"] = roles.get(pr["label"].lower(), "referenced")
            pr["own"] = pr["role"] in OWN_ROLES
            pr["about"] = pr["label"].lower() in about
            # The PR it was asked about (named in its instructions, given to follow): what its
            # checks come to is part of its result, read again as they change.
            pr["asked"] = pr["role"] == "referenced" and self.asked_about(task, pr["label"])
            # Reviewed by its worker (others' work it was asked to look at): a main chip too.
            pr["reviewed"] = pr["role"] == "reviewed"
            # Its review went out, or waits on its card (the chip says which).
            pr["review_state"] = reviews.get(pr["label"].lower(), "") if pr["reviewed"] else ""
            old = before.get(pr["label"].lower()) or {}
            same = old and all(old.get(key) == pr.get(key) for key in PR_STANDING)
            pr["changed_at"] = (
                (old.get("changed_at") or old.get("at") or pr["at"]) if same else pr["at"]
            )
        return prs

    def previous_prs(self, task_id: str) -> list[dict]:
        found = self.store.one(
            "SELECT evidence FROM runs WHERE task_id=? AND evidence!='' ORDER BY started_at DESC",
            (task_id,),
        )
        evidence = json.loads(found["evidence"]) if found else {}
        return evidence.get("prs") or []

    def set_check_back(self, task: dict, at: str, what: str, url: str):
        """The worker is woken at `at` (ISO) to check `what`; one per task, the latest."""
        value = {"at": at, "what": what, "url": url, "session_id": task["session_id"]}
        self.store.set(f"checkback:{task['id']}", json.dumps(value))
        self.publish(self.tasks.get(task["id"]))

    def check_back(self, task: dict) -> dict | None:
        found = self.store.get(f"checkback:{task['id']}")
        return json.loads(found) if found and task["status"] in {"running", "waiting"} else None

    async def wake_check_backs(self, moment: datetime | None = None):
        """Workers whose check-back time came are woken with what they wanted to check. The
        check-back is kept until its worker has the message: one that could not be delivered
        (its worker could not be reopened, Copilot failed) is tried again a few minutes later,
        and says why on the task's card."""
        moment = moment or datetime.now(UTC)
        for task_id, raw in self.store.items("checkback:").items():
            found = json.loads(raw)
            if datetime.fromisoformat(found["at"]) > moment or task_id in self.waking:
                continue
            task = self.tasks.find(task_id)
            if not task or task["status"] not in {"running", "waiting"}:
                self.store.delete(f"checkback:{task_id}")
                continue
            if self.store.get(f"stopped:{task_id}"):
                continue  # Stopped by the owner: kept until they say Go on.
            row = self.store.find_session(task["session_id"])
            if not row:
                self.store.delete(f"checkback:{task_id}")
                continue
            self.waking.add(task_id)
            try:
                if row["closed_at"]:
                    await self.runtime.workers.reopen(row["id"])
                link = f" ({found['url']})" if found.get("url") else ""
                text = (
                    f"Time to check {found['what']}{link} for task {task_id} “{task['title']}”. "
                    "Check it now. Done: report. Not yet: check_back again, or report what "
                    "blocks it."
                )
                await self.delivery.deliver(
                    row["id"],
                    wrap("system", "check-back", text, task=task_id),
                    kind="router",
                    display=f"Check back: {found['what']}{link}",
                    meta={"task_id": task_id, "check_back": True},
                )
            except Exception as exc:
                logger.warning("Could not wake %s to check back.", task_id, exc_info=True)
                attempts = int(found.get("attempts", 0)) + 1
                retry = moment + timedelta(minutes=CHECK_BACK_RETRY_MINUTES)
                if self.store.get(f"checkback:{task_id}") == raw:  # Not set again meanwhile.
                    self.store.set(
                        f"checkback:{task_id}",
                        json.dumps(
                            found
                            | {
                                "at": retry.isoformat(),
                                "attempts": attempts,
                                "error": describe(exc)[:300],
                                "due": found.get("due") or found["at"],
                            }
                        ),
                    )
            else:
                self.store.set(f"checkfired:{task_id}", moment.isoformat())
                if self.store.get(f"checkback:{task_id}") == raw:  # Not set again meanwhile.
                    self.store.delete(f"checkback:{task_id}")
            finally:
                self.waking.discard(task_id)
            self.publish(self.tasks.get(task_id))

    async def learn_pr_titles(self, text: str):
        """The titles of the PRs a text names, read from GitHub once (a few at a time)."""
        learned = False
        for key in threads.pull_requests(text)[:5]:
            label = key.removeprefix("github:")
            if self.store.get(f"prtitle:{label}"):
                continue
            repo, number = label.rsplit("#", 1)
            try:
                pull = await self.runtime.github.pull(repo, int(number))
            except Exception:
                logger.info("Could not read the title of %s.", label, exc_info=True)
                continue
            if pull.get("title"):
                self.runtime.remember_pr_title(label, pull["title"])
                learned = True
        if learned:
            self.runtime.publish_state()

    def reviews(self, session_id: str) -> list[dict]:
        """A worker's reviews of PRs, the oldest first: {label ("owner/repo#n", lowercase),
        task_id, status}. Not its replies to comments: it answers comments on its own PRs too.
        Each is the review of the task it was drafted for, as the draft says. One that does
        not say (drafted before drafts did, or while the worker had several tasks open) is the
        task's whose run was under way when it was drafted, when only one was; else no task's.
        Another task of the worker that only names the PR did not review it."""
        runs: list[dict] | None = None
        found = []
        for row in self.store.all(
            "SELECT target, status, created_at FROM drafts WHERE session_id=? AND kind='github' "
            "ORDER BY created_at",
            (session_id,),
        ):
            target = json.loads(row["target"])
            if (
                not target.get("repo")
                or not target.get("number")
                or target.get("in_reply_to")
                or target.get("reply_to_comment")
            ):
                continue
            task_id = target.get("task_id") or ""
            if not task_id:
                if runs is None:
                    runs = self.store.all(
                        "SELECT task_id, started_at, ended_at FROM runs WHERE session_id=?",
                        (session_id,),
                    )
                at = stamp(row["created_at"])
                under = {
                    run["task_id"]
                    for run in runs
                    if stamp(run["started_at"]) <= at
                    and (not run["ended_at"] or at <= stamp(run["ended_at"]))
                }
                task_id = under.pop() if len(under) == 1 else ""
            label = f"{target['repo']}#{target['number']}".lower()
            found.append({"label": label, "task_id": task_id, "status": row["status"]})
        return found

    def reviewed_prs(self, session_id: str, task_id: str | None = None) -> set[str]:
        """The PRs a worker drafted a review of ("owner/repo#n"): others' work, not its own.
        `task_id`: only those it reviewed for that task (see reviews), not every one its worker
        did."""
        return {
            review["label"]
            for review in self.reviews(session_id)
            if task_id is None or review["task_id"] == task_id
        }

    def review_states(self, session_id: str, task_id: str | None = None) -> dict[str, str]:
        """Where a worker's reviews of PRs ("owner/repo#n") stand (`task_id`: those for that
        task): "sent" once one went out, else "drafted" while one waits for the owner, else ""
        (rejected, failed)."""
        found: dict[str, str] = {}
        for review in self.reviews(session_id):
            if task_id is not None and review["task_id"] != task_id:
                continue
            label = review["label"]
            state = {"sent": "sent", "pending": "drafted", "sending": "drafted"}.get(
                review["status"]
            )
            if state and found.get(label) != "sent":
                found[label] = state
        return found

    def first_reporter(self, label: str) -> str:
        """The task ("session_id|task_id") whose report named this PR ("owner/repo#n") first:
        the work that brought it up. Several workers push as the owner, so a PR another worker
        brought up is that one's. Read once from the reports, then kept; "" when none did."""
        key = f"prfirst:{label.lower()}"
        if found := self.store.get(key):
            return found
        repo, number = label.lower().rsplit("#", 1)
        named = re.compile(rf"github\.com/{re.escape(repo)}/pull/{number}(?!\d)", re.IGNORECASE)
        for row in self.store.all(
            "SELECT session_id, text, meta FROM messages WHERE kind='report' AND text LIKE ? "
            "ORDER BY id",
            (f"%/pull/{number}%",),
        ):
            if named.search(row["text"]):
                task_id = json.loads(row["meta"] or "{}").get("task_id") or ""
                found = f"{row['session_id']}|{task_id}"
                self.store.set(key, found)
                return found
        return ""

    def opener(self, label: str) -> str:
        """The task that opened this PR ("owner/repo#n") by record (Create PR, `gh pr create`,
        a create-PR tool), "" when none did."""
        label = label.lower()
        for task_id in self.store.items("prs:"):
            if label in self.opened_prs(task_id):
                return task_id
        return ""

    def claimed_by(self, label: str) -> str:
        """The task whose report first says it opened this PR ("Opened <url>", "PR 已开 <url>"),
        "" when none did. A report that only names it (to read it, about someone else's) is
        no claim. Read once from the reports, then kept."""
        key = f"prclaim:{label.lower()}"
        if (found := self.store.get(key)) is not None:
            return found
        repo, number = label.lower().rsplit("#", 1)
        named = re.compile(rf"github\.com/{re.escape(repo)}/pull/{number}(?!\d)", re.IGNORECASE)
        found = ""
        for row in self.store.all(
            "SELECT text, meta FROM messages WHERE kind='report' AND text LIKE ? ORDER BY id",
            (f"%/pull/{number}%",),
        ):
            task_id = json.loads(row["meta"] or "{}").get("task_id") or ""
            if task_id and any(
                named.search(line) and claims_opening(line) for line in claim_lines(row["text"])
            ):
                found = task_id
                break
        if found:
            self.store.set(key, found)
        return found

    def creator(self, label: str) -> str:
        """The task that made this PR: the one that opened it by record, else the first whose
        report says it opened it. A mention is no proof: the first to name a PR may only be
        reading it, or naming another worker's."""
        return self.opener(label) or self.claimed_by(label)

    async def pr_roles(self, task: dict, prs: list[dict]) -> tuple[set[str], set[str]]:
        """Which of these PRs ("owner/repo#n", lowercase) are the task's own work (it created or
        modified them, see classify_prs), and which it is about."""
        roles, about = await self.classify_prs(task, prs)
        return {label for label, role in roles.items() if role in OWN_ROLES}, about

    def committed_branches(self, task_id: str, checkouts: list[dict] | None = None) -> set[str]:
        """The branches its own commits are on, as its receipts have them (`checkouts`: as
        just found)."""
        found: list[dict] = checkouts or []
        if checkouts is None:
            run = self.store.one(
                "SELECT evidence FROM runs WHERE task_id=? AND evidence!='' "
                "ORDER BY started_at DESC",
                (task_id,),
            )
            found = (json.loads(run["evidence"]) if run else {}).get("checkouts") or []
        return {c["branch"] for c in found if c.get("branch") and c.get("commits")}

    def pushed_branches(self, task_id: str) -> set[tuple[str, str]]:
        """The GitHub branches its worker's `git push` (or `gh pr create`) updated for this task,
        as ("owner/repo" lowercase, branch), read from what its commands said: wherever the
        checkout was, and whichever branch it is on now. Not a dry run's (see dry_run)."""
        found = set()
        for row in self.store.all(
            "SELECT c.command, c.output FROM commands c JOIN runs r ON r.id=c.run_id "
            "WHERE r.task_id=? AND c.output LIKE '%->%' ORDER BY c.id",
            (task_id,),
        ):
            if not PUSHES.search(row["command"]) or dry_run(row["command"]):
                continue
            repo = ""
            for line in row["output"].splitlines():
                if to := PUSHED_TO.match(line.strip()):
                    remote = GITHUB_REMOTE.search(to.group(1))
                    repo = remote.group(1).lower() if remote else ""
                elif repo and (ref := PUSHED_REF.match(line)):
                    found.add((repo, ref.group(1)))
        return found

    async def classify_prs(
        self, task: dict, prs: list[dict], checkouts: list[dict] | None = None
    ) -> tuple[dict[str, str], set[str]]:
        """What each of these PRs ("owner/repo#n", lowercase) is to the task, by evidence of
        what it did to it: "created" (it opened it: by record, Create PR or `gh pr create`; or
        the owner's account opened it since the task began, and its report says it opened it,
        it is from its worker's branch or the task pushed its branch), "modified" (opened
        before the task began, from its worker's branch, or the task pushed to its branch:
        it changed a PR it did not open), "reviewed" (it drafted a review of it), else
        "referenced". One task owns a PR: not one when another task opened it or says it did
        (unless it pushed to it), nor one that only shares the branch with another task of its
        worker whose commits are on it. Which task named a PR first, or was updated last,
        makes no task its owner. And which it is about: those it owns or reviewed, those it
        was given to follow, those its title or instructions name, and those it brought up
        first. A PR its report only mentions after another task did is neither."""
        from .tools import names_pr

        opened = self.opened_prs(task["id"])
        row = self.store.find_session(task["session_id"])
        branches = {c.get("branch") or "" for c in self.workspaces.checkouts(row)} if row else set()
        branches = {branch for branch in branches | {(row or {}).get("branch") or ""} if branch}
        try:
            mine = await self.runtime.github.logins()
        except Exception:
            mine = set()
        reviewed = self.reviewed_prs(task["session_id"], task["id"])
        pushed = self.pushed_branches(task["id"])
        asked = f"{task.get('title') or ''}\n{task.get('goal') or ''}"
        others = [
            other["id"]
            for other in self.tasks.for_session(task["session_id"])
            if other["id"] != task["id"] and other["status"] != "cancelled"
        ]
        roles: dict[str, str] = {}
        about: set[str] = set()
        for pr in prs:
            label = pr["label"].lower()
            repo, number = label.rsplit("#", 1)
            first = self.first_reporter(label)
            # Several workers push as the owner: only the task that brought it up first.
            brought = first == f"{task['session_id']}|{task['id']}" or (
                first == f"{task['session_id']}|" and not task["id"]
            )
            maker = self.creator(label)
            new = pr.get("author") in mine and not_before(pr.get("created"), task["created_at"])
            branch = pr.get("branch") or ""
            # Its worker's branch, and its commits there (not another of its tasks' only).
            on_branch = bool(branch and branch in branches) and (
                branch in self.committed_branches(task["id"], checkouts)
                or not any(branch in self.committed_branches(other) for other in others)
            )
            # What it opened is its own, whatever else it drafted on it; so is a branch it
            # pushed to (by its commands' output, wherever its checkout was).
            if label in opened:
                roles[label] = "created"
            elif branch and (repo, branch) in pushed:
                made = new and maker in {"", task["id"]}
                roles[label] = "created" if made else "modified"
            elif label in reviewed:
                roles[label] = "reviewed"
            elif maker and maker != task["id"]:
                roles[label] = "referenced"
            elif new and (maker == task["id"] or on_branch):
                roles[label] = "created"
            elif on_branch:
                roles[label] = "modified"
            else:
                roles[label] = "referenced"
            if (
                roles[label] != "referenced"
                or self.store.get(f"given:{task['session_id']}:github:{label}")
                or names_pr(asked, repo, int(number))
                or (first and brought)
            ):
                about.add(label)
        return roles, about

    def asked_about(self, task: dict, label: str) -> bool:
        """Whether a task was asked about a PR ("owner/repo#n"): given it to follow, or its
        title or instructions name it."""
        from .tools import names_pr

        repo, number = label.lower().rsplit("#", 1)
        asked = f"{task.get('title') or ''}\n{task.get('goal') or ''}"
        return bool(
            self.store.get(f"given:{task['session_id']}:github:{repo}#{number}")
            or names_pr(asked, repo, int(number))
        )

    async def own_prs(self, task: dict, prs: list[dict]) -> set[str]:
        return (await self.pr_roles(task, prs))[0]

    async def offer_merge(self, task: dict, prs: list[dict], since: str = ""):
        """A PR the task delivered for the owner that can be merged now (open, not a draft, CI
        passing, nothing blocking on GitHub) gets a card: merge it, or not now. Once per head
        commit; the card goes when the PR changes, is merged or closed, and comes back once it
        is ready again. A card is its task's: only that task's own PRs, as it read them, make,
        keep or take it away. Another task's copy of the PR (one it only names, kept as it
        stood hours ago) never touches it, nor does a copy read before the card was offered.
        `since`: when these were read; one kept from before then (not read now) offers nothing."""
        try:
            mine = await self.runtime.github.logins()
        except Exception:
            return
        cards = [draft for draft in self.store.drafts() if draft["kind"] == "merge"]
        for pr in prs:
            label = pr["label"].lower()
            mergeable = (
                pr.get("state") == "OPEN"
                and not pr.get("draft")
                and pr.get("checks") in {"passing", "none"}
                and pr.get("merge_state") in {"CLEAN", "HAS_HOOKS"}
                and pr.get("review") != "CHANGES_REQUESTED"
                and pr.get("head")
            )
            for card in cards:
                target = card["target"]
                if target["label"].lower() != label or target.get("task_id") != task["id"]:
                    continue  # Another task's card: only what that task reads decides it.
                if not pr.get("own"):
                    # Offered on a task that only names it (another task made it).
                    self.store.delete(f"mergeoffer:{label}@{target['head']}")
                    closed = self.store.update_draft(
                        card["id"], status="rejected", outcome="Not this task's PR.",
                        resolved_at=now(),
                    )  # fmt: skip
                    self.bus.publish("draft", closed)
                    continue
                if stamp(pr.get("at")) < stamp(target.get("at")):
                    continue  # How it stood before the card was offered: no news of it.
                # GitHub works out whether it can merge (after its base moved, say): the card
                # stays; Merge is only ever of the head it shows.
                recomputing = (
                    pr.get("state") == "OPEN"
                    and pr.get("merge_state") == "UNKNOWN"
                    and target["head"] == pr.get("head")
                )
                if not recomputing and not (mergeable and target["head"] == pr["head"]):
                    gone = pr.get("state") in {"MERGED", "CLOSED"}
                    why = (
                        f"The PR is {pr['state'].lower()}."
                        if gone
                        else "It has merge conflicts with its base: offered again once they "
                        "are resolved."
                        if pr.get("merge_state") == "DIRTY"
                        else "The PR changed since: offered again once it is ready."
                    )
                    if pr.get("state") != "MERGED":
                        # Not ready for now (a check run again, changes asked for and then
                        # dismissed, the PR closed and maybe reopened later): offered again
                        # when it is, even at the same head.
                        self.store.delete(f"mergeoffer:{label}@{target['head']}")
                    closed = self.store.update_draft(
                        card["id"], status="rejected", outcome=why, resolved_at=now()
                    )
                    self.bus.publish("draft", closed)
            fresh = not since or stamp(pr.get("at")) >= stamp(since)
            ready = mergeable and pr.get("own") and fresh and pr.get("author") in mine
            key = f"mergeoffer:{label}@{pr.get('head')}"
            if not ready or self.store.get(key):
                continue
            self.store.set(key, "1")
            repo, number = pr["label"].rsplit("#", 1)
            target = {
                "label": pr["label"],
                "repo": repo.lower(),
                "number": int(number),
                "url": pr["url"],
                "title": pr.get("title", ""),
                "head": pr["head"],
                "task_id": task["id"],
                "review": pr.get("review", ""),
                "checks": pr.get("checks", ""),
                # When the PR was read as ready: a copy read before says nothing of it now.
                "at": pr.get("at") or now(),
            }
            card = self.store.create_draft(task["session_id"], "merge", target, pr.get("title", ""))
            self.bus.publish("draft", card)
            alerted = f"mergealert:{label}@{pr['head']}"
            if not self.store.get(alerted):  # Offered again at the same commit: said once.
                self.store.set(alerted, "1")
                self.runtime.alert(
                    f"{pr['label']} is ready to merge", session_id=task["session_id"],
                    needs_action=True, title="Ready to merge",
                )  # fmt: skip

    async def refresh_prs(self, task_id: str) -> bool:
        """The PR chips of a task show what is true now, not what was when it reported: a PR
        whose failing checks pass on a later run turns green. True when anything changed."""
        run = self.store.one(
            """SELECT id, evidence FROM runs WHERE task_id=? AND evidence!=''
            ORDER BY started_at DESC LIMIT 1""",
            (task_id,),
        )
        if not run:
            return False
        evidence = json.loads(run["evidence"])
        if not evidence.get("prs"):
            return False
        read = now()
        prs = await self.read_prs(self.tasks.get(task_id), again=True)
        if not prs:
            return False
        task = self.tasks.get(task_id)
        await self.offer_merge(task, prs, since=read)
        shown = [{k: v for k, v in pr.items() if k != "at"} for pr in evidence["prs"]]
        changed = shown != [{k: v for k, v in pr.items() if k != "at"} for pr in prs]
        evidence |= {"prs": prs, "prs_at": time.time()}
        # Saved first: whether its other PRs are still open is read from what is true now.
        self.store.execute(
            "UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run["id"])
        )
        # Its own PR merged: its card says so (not a PR its report only mentions). A result
        # still to accept stays so: merging is not accepting.
        merged = [pr["label"] for pr in prs if pr.get("state") == "MERGED" and pr.get("own")]
        if merged and task["status"] == "review":
            for label in merged:
                self.pr_merged(task["session_id"], f"github:{label.lower()}", task["id"])
        if changed:
            self.publish(self.tasks.get(task_id))
        return changed

    async def refresh_open_prs(self, pull: str = "", every: float = PR_REFRESH):
        """Every few minutes (and at once for `pull`, "owner/repo#n", when something happened
        on it): the PRs of recent tasks that are not merged yet. Those all closed (not merged)
        less often, and at once when something happened on one: it may have been reopened."""
        for task in self.tasks.listed(include_ended_since=hours_ago(PR_HOURS)):
            run = self.store.one(
                """SELECT evidence FROM runs WHERE task_id=? AND evidence!=''
                ORDER BY started_at DESC LIMIT 1""",
                (task["id"],),
            )
            evidence = json.loads(run["evidence"]) if run else {}
            # What it made, changed or reviewed, or is about (the PR its checks are on): a PR it
            # only mentions (an example, for context) is not followed up.
            prs = [pr for pr in evidence.get("prs") or [] if followed_up(pr)]
            states = {pr.get("state") for pr in prs}
            if not prs or states == {"MERGED"}:
                continue
            named = pull and any(pr.get("label", "").lower() == pull.lower() for pr in prs)
            pace = every * CLOSED_PR_PACE if states <= {"MERGED", "CLOSED"} else every
            if not named and time.time() - evidence.get("prs_at", 0) < pace:
                continue
            try:
                await self.refresh_prs(task["id"])
            except Exception:
                logger.info("Could not refresh the PRs of %s.", task["id"], exc_info=True)

    def record_opened(self, task_id: str, urls: list[str]):
        """PRs this task's worker opened (Create PR, or `gh pr create` in its shell): its own
        work, which a merge settles."""
        known = json.loads(self.store.get(f"prs:{task_id}") or "[]")
        self.store.set(f"prs:{task_id}", json.dumps(list(dict.fromkeys(known + urls))))

    def opened_prs(self, task_id: str) -> set[str]:
        """Those PRs, as "owner/repo#n" (lowercase)."""
        opened = json.loads(self.store.get(f"prs:{task_id}") or "[]")
        return {key.removeprefix("github:") for key in threads.pull_requests(" ".join(opened))}

    def owns_pr(self, task: dict, label: str) -> bool:
        """Whether a PR ("owner/repo#n") is this task's own work by what is known without
        GitHub: it opened it, or said it did and no other task opened it."""
        return self.creator(label) == task["id"]

    def pr_urls(self, task: dict) -> list[str]:
        """The task's PRs: those opened with Create PR, and those any of its reports linked to."""
        opened = json.loads(self.store.get(f"prs:{task['id']}") or "[]")
        cited = json.loads(self.store.get(f"cited:{task['id']}") or "[]") + [
            f"https://github.com/{repo}/pull/{number}"
            for repo, number in threads.PR_URL.findall(task["result"] or "")
        ]
        return list(dict.fromkeys(opened + cited))

    def remember_prs(self, task_id: str, report: str):
        """PRs a done report links to stay the task's when a later report does not repeat them
        (a merged PR then still settles it)."""
        cited = json.loads(self.store.get(f"cited:{task_id}") or "[]")
        found = [
            f"https://github.com/{repo}/pull/{n}" for repo, n in threads.PR_URL.findall(report)
        ]
        if found:
            self.store.set(f"cited:{task_id}", json.dumps(list(dict.fromkeys(cited + found))))

    def pr_merged(self, session_id: str, thread: str, task_id: str = ""):
        """A worker's PR was merged: the task that delivers it (`task_id`, when known already;
        not a task whose report only mentions it) says so on its card. Merging is not
        accepting: a result still to accept waits for the owner's Accept (or their Request
        changes), as one that was accepted stays done."""
        label = thread.removeprefix("github:")
        self.runtime.drafts.merged(session_id, thread, task_id)
        for task in self.tasks.for_session(session_id):
            if task["status"] not in {"review", "done"} or (task_id and task["id"] != task_id):
                continue
            if not task_id and not self.owns_pr(task, label):
                continue
            if thread in threads.pull_requests(" ".join(self.pr_urls(task))):
                self.note_merged(task, label)

    def note_merged(self, task: dict, label: str):
        """Its PR (`label`, "owner/repo#n") is merged, as of now (before its next read from
        GitHub): its card shows so (see shown_result)."""
        merged = json.loads(self.store.get(f"mergedprs:{task['id']}") or "[]")
        if label.lower() not in {known.lower() for known in merged}:
            self.store.set(f"mergedprs:{task['id']}", json.dumps([*merged, label]))
            self.publish(self.tasks.get(task["id"]))

    def shown_result(self, task: dict) -> str:
        """Its result as its card shows it (the stored one stays as reported): once its PR is
        open, without what it said of it before ("not pushed", "no PR opened"); once merged,
        without "(not merged)", "#72 is open" or "still following", and saying first that it
        is merged. Only those words: everything else it said stays."""
        result = task["result"] or ""
        own = [pr for pr in self.previous_prs(task["id"]) if pr.get("own")]
        noted = json.loads(self.store.get(f"mergedprs:{task['id']}") or "[]")
        if not result or not (own or noted or self.opened_prs(task["id"])):
            return result
        result = without_status(result, not_pushed) or result
        merged = {pr["label"].lower(): pr["label"] for pr in own if pr.get("state") == "MERGED"}
        for label in noted:
            merged.setdefault(label.lower(), label)
        for label in merged.values():
            names = pr_names(label)

            def unmerged(clause: str, names: re.Pattern = names) -> bool:
                return status_only(clause, STALE_ONCE_MERGED, names)

            result = without_status(result, unmerged) or result
            said = re.split(r"(?<=[.!?])\s+|\n", result)
            if not any(names.search(part) and re.search(r"\bmerged\b", part) for part in said):
                result = f"{label} is merged.\n\n{result}"
        return result

    async def diff(self, task_id: str) -> list[dict]:
        """What the task's worker changed, per checkout, as unified diffs."""
        task = self.tasks.get(task_id)
        row = self.store.session(task["session_id"])
        found = []
        async with self.states[row["id"]].checkouts:  # Not while its worktrees are removed.
            for checkout in self.workspaces.checkouts(row):
                try:
                    found.append(await diff(checkout, self.scope(task, checkout)))
                except ValueError as exc:
                    logger.info("No diff for %s: %s", checkout["path"], exc)
        return found

    async def create_pr(self, task_id: str) -> dict:
        """Push the task's branches and open a PR from each, or find the one already open."""
        task = self.tasks.get(task_id)
        if task["status"] not in {"review", "done"}:
            raise ValueError(f"The task is {task['status']}: open a PR once it is finished.")
        row = self.store.session(task["session_id"])
        gh = self.runtime.github.gh
        opened: list[str] = []
        async with self.states[row["id"]].checkouts:
            plans = []
            for checkout in self.workspaces.checkouts(row):
                if not (checkout.get("repo") and checkout.get("branch")):
                    continue  # A shared project folder has no branch of its own.
                if plan := await self.pr_plan(checkout):
                    plans.append((checkout, *plan))
            if not plans:
                raise ValueError("Nothing to open a PR from: the worker committed nothing.")
            title, body = self.pr_text(task, await self.commit_subjects(plans))
            for checkout, repo, slug, branch in plans:
                await checkout_git(
                    checkout, repo, "push", "--quiet", "origin",
                    f"refs/heads/{branch}:refs/heads/{branch}", limit=180,
                )  # fmt: skip
                listed = await gh(
                    "pr", "list", "--repo", slug, "--head", branch, "--state", "open",
                    "--json", "url", "--limit", "1",
                )  # fmt: skip
                if existing := json.loads(listed or "[]"):
                    opened.append(existing[0]["url"])
                    continue
                created = await gh(
                    "pr", "create", "--repo", slug, "--head", branch,
                    "--title", title, "--body", body,
                )  # fmt: skip
                if not (found := threads.PR_URL.search(created)):
                    raise ValueError(f"GitHub did not say which PR it opened: {created[:300]}")
                opened.append(found.group(0))
        self.record_opened(task_id, opened)
        if not row["closed_at"]:
            # Its reviews, comments and CI come to the worker; merged, the task is done.
            followed = self.runtime.follow(
                row["id"], threads.pull_requests(" ".join(opened)), task_id=task_id
            )
            note = (
                f"The owner opened {', '.join(opened)} from your branch. You follow it: its "
                "reviews, comments and CI results come to you."
            )
            self.runtime.add_message(
                row["id"],
                "notice",
                f"PR opened: {', '.join(opened)}",
                {"prompt": wrap("system", "context", note), "deferred": True} if followed else {},
            )
        await self.collect_evidence(task_id, keep_stale=True)
        return self.tasks.get(task_id)

    def pr_text(self, task: dict, subjects: list[str] | None = None) -> tuple[str, str]:
        """A PR's title and description for a task's work: what changed, as its commits say
        (`subjects`; else the task's title) and its worker reported it, and its checks. Not
        its instructions (written for the worker: "do not open a PR"), what it said about
        pushing or a PR (moot once it is open), what a follow-up said about the result before
        ("unchanged from before"), its credits or the project's name in its title."""
        title = pr_title(subjects or []) or (
            re.sub(r"\s*\u00b7\s*[\w.-]+$", "", task["title"]).strip() or task["title"]
        )
        found = self.view(task)["receipts"] or {}
        text = (task["result"] or task["progress"] or "").strip()
        lines = [without_follow_up(without_status(text, not_pushed)), ""]
        checks = []
        for label, key in (("Tests", "tests"), ("Checks", "checks"), ("Build", "build")):
            if (
                (item := found.get(key))
                and item.get("passed") is not None
                and item.get("own", True)
            ):
                outcome = (
                    "ran no tests"
                    if item.get("no_tests")
                    else "passed"
                    if item["passed"]
                    else f"failed (exit {item['exit_code']})"
                )
                checks.append(f"- {label}: {code_span(cited_command(item['command']))} {outcome}")
        for item in found.get("criteria") or []:
            if item["state"] == "verified" and not about_pushing(item["criterion"]):
                command = cited_command(item["command"], item.get("evidence", ""))
                checks.append(
                    f"- {item['criterion']}" + (f": {code_span(command)}" if command else "")
                )
        if checks:
            lines += ["**Checked**", "", *checks, ""]
        return title[:250], "\n".join(lines).strip()

    async def commit_subjects(self, plans: list[tuple[dict, str, str, str]]) -> list[str]:
        """The subjects of the commits the PRs bring, oldest first (no merges)."""
        found: list[str] = []
        for checkout, repo, _slug, branch in plans:
            with suppress(ValueError):
                listed = await checkout_git(
                    checkout, repo, "log", "--no-merges", "--reverse", "--format=%s",
                    f"{base_of(checkout)}..refs/heads/{branch}",
                )  # fmt: skip
                found += [line.strip() for line in listed.splitlines() if line.strip()]
        return found

    async def pr_plan(self, checkout: dict) -> tuple[str, str, str] | None:
        """(repository, GitHub owner/name, branch) to open a PR from, or None: no commits."""
        repo, branch = checkout["repo"], checkout["branch"]
        # As the owner knows it: its package's name ("textkit"), else its folder's.
        name = await asyncio.to_thread(package_name, Path(repo)) or Path(repo).name
        tree = checkout.get("worktree") or checkout["path"]
        live = await asyncio.to_thread(os.path.isdir, tree)
        status = await checkout_git(checkout, tree, "status", "--porcelain") if live else ""
        if worth_committing(status):
            raise ValueError(
                f"The {name} repository has uncommitted changes. Follow up and ask the worker to "
                "commit "
                "them, then create the PR."
            )
        if live:
            # The branch it is on now (one the owner named, say), not the one it was given.
            with suppress(ValueError):
                current = (
                    await checkout_git(checkout, tree, "rev-parse", "--abbrev-ref", "HEAD")
                ).strip()
                default = base_of(checkout).removeprefix("origin/")
                if current and current not in {"HEAD", default, "main", "master"}:
                    branch = current
        commits = await checkout_git(
            checkout, repo, "rev-list", "--count", f"{base_of(checkout)}..refs/heads/{branch}"
        )
        if not int(commits.strip() or 0):
            return None
        try:
            remote = (
                await checkout_git(checkout, repo, "config", "--get", "remote.origin.url")
            ).strip()
        except ValueError:
            remote = ""
        match = GITHUB_REMOTE.search(remote)
        if not match:
            raise ValueError(
                f"The {name} repository has no GitHub remote (origin) to open a PR on: push its "
                "branch somewhere first, or open the PR yourself."
            )
        return repo, match.group(1), branch

    def report_markdown(self, task_id: str) -> str:
        """A task as markdown to paste into a PR description or a message."""
        task = self.view(self.tasks.get(task_id))
        found = task["receipts"] or {}
        lines = [f"## {task['title']}", ""]
        facts: list[str] = [STATUS_WORDS.get(task["status"]) or task["status"]]
        if task["runs"] > 1:
            facts.append(f"{task['runs']} runs")
        if task["credits"] >= 0.05:
            facts.append(f"{task['credits']:.1f} Copilot credits")
        lines += [" · ".join(facts), ""]
        if task["goal"]:
            lines += ["**Goal**", "", task["goal"].strip(), ""]
        if text := task["result"] or task["progress"]:
            lines += ["**Result** (as reported)", "", text.strip(), ""]
        observed = []
        for label, key in (("Tests", "tests"), ("Checks", "checks"), ("Build", "build")):
            if item := found.get(key):
                mark = (
                    "ran no tests"
                    if item.get("no_tests")
                    else "running"
                    if item["passed"] is None
                    else "passed"
                    if item["passed"]
                    else f"failed (exit {item['exit_code']})"
                )
                observed.append(f"- {label}: `{item['command']}` {mark}")
        changed = found.get("changed") or {}
        if changed.get("files"):
            observed.append(
                f"- Changes: +{changed['insertions']} −{changed['deletions']} in "
                f"{changed['files']} file{'s' if changed['files'] != 1 else ''}"
                + (f", {changed['commits']} commit(s)" if changed.get("commits") else "")
            )
        for checkout in found.get("checkouts", []):
            if checkout.get("branch"):
                observed.append(f"- Branch `{checkout['branch']}`")
        for pr in found.get("prs", []):
            observed.append(f"- [{pr['label']}]({pr['url']}): checks {pr['checks']}")
        for item in found.get("version", []):
            edited = " with uncommitted edits" if item["edited"] else ""
            observed.append(f"- Checked at `{item['commit'][:7]}`{edited}")
        observed += [f"- ⚠ {flag}" for flag in found.get("flags", [])]
        if results := found.get("criteria"):
            marks = {"verified": "✅", "claimed": "☑️", "contradicted": "❌", "not_met": "❌"}
            words = {
                "verified": "verified",
                "claimed": "reported met, not observed",
                "contradicted": "reported met, but the check failed",
                "not_met": "not met",
                "not_checked": "not checked",
            }
            lines += ["**Done when**", ""]
            for item in results:
                how = f": `{item['command']}`" if item["command"] else ""
                lines.append(
                    f"- {marks.get(item['state'], '⬜')} {item['criterion']} "
                    f"({words[item['state']]}{how})"
                )
            lines.append("")
        if observed:
            lines += ["**Observed**", "", *observed, ""]
        if self.store.settings().report_footer:
            lines.append(f"<sub>Orchestrated with [Chief of Staff]({PROJECT_URL})</sub>")
        return "\n".join(lines).strip() + "\n"

    def fail(self, session_id: str, message: str):
        """Its session failed: none of its open tasks goes on."""
        for task in self.tasks.for_session(session_id):
            if task["status"] in OPEN:
                self.tasks.move(task["id"], "failed", progress=message[:500])
                self.runtime.background(self.record_end(task))  # See cancel.

    def report_task(self, caller: str, reference: str) -> dict | None:
        """The task a report is about: the one it names; else its only open task (not the one
        given last, which may be finished); else the task it is on."""
        if not reference:
            open_ = [task for task in self.tasks.for_session(caller) if task["status"] in OPEN]
            if len(open_) == 1:
                return open_[0]
            if len(open_) > 1:
                # Filed against whichever was given last, it would be another task's report.
                listed = "; ".join(f"{task['id']} “{task['title']}”" for task in open_)
                raise ValueError(
                    f"You have {len(open_)} open tasks ({listed}): call report again with the "
                    "task_id of the one this is about."
                )
            # Nothing open: what reached it since its last report is about which task (news on
            # a thread it follows for one, the owner's answer on one's card). Bug bash 9: the
            # newest news (another task's, queued) or the task given last took a report that
            # was about another, and overwrote its result.
            last = self.store.one(
                "SELECT id FROM messages WHERE session_id=? AND kind='report' "
                "ORDER BY id DESC LIMIT 1",
                (caller,),
            )
            named = [
                found
                for row in self.store.all(
                    "SELECT json_extract(meta, '$.task_id') AS task_id FROM messages "
                    "WHERE session_id=? AND id>? AND json_extract(meta, '$.task_id') IS NOT NULL "
                    "AND (kind IN ('owner', 'router') OR (kind='inbound' "
                    "AND json_extract(meta, '$.thread') IS NOT NULL)) ORDER BY id",
                    (caller, last["id"] if last else 0),
                )
                if (found := self.tasks.find(row["task_id"])) and found["session_id"] == caller
            ]
            about = list({task["id"]: task for task in named}.values())
            if len(about) > 1:
                listed = "; ".join(f"{task['id']} “{task['title']}”" for task in about)
                raise ValueError(
                    f"What came since your last report is about {len(about)} tasks ({listed}): "
                    "call report again with the task_id of the one this is about."
                )
            if about:
                return about[0]
            if (turn := self.tasks.find(self.runtime.turn_task(caller))) and turn[
                "session_id"
            ] == caller:
                return turn
            return self.tasks.current(caller)
        task = self.tasks.find(reference)
        if not task or task["session_id"] != caller:
            raise ValueError(f"You have no task {reference}.")
        return task

    def task_for(self, reference: str) -> dict:
        """A task by its id, or a worker's current task by the worker's id."""
        if task := self.tasks.find(reference):
            return task
        try:
            row = self.store.session(reference)
        except LookupError:
            raise ValueError(f"No task or worker {reference}; list_sessions lists them.") from None
        if row["role"] != WORKER or not (task := self.tasks.current(row["id"])):
            raise ValueError(f"{reference} is not a worker with a task.")
        return task

    async def cancel(self, task_id: str, reason: str = "", by: str = "you") -> dict:
        """End a task for good. Its worker stops if it is on this task, and stays open."""
        task = self.tasks.get(task_id)
        if task["status"] in ENDED:
            raise ValueError(f"Task {task_id} is {task['status']} already.")
        row = self.store.find_session(task["session_id"])
        # At work on it now: the task its turn is on (not merely the one it was given last).
        on_it = (
            bool(row)
            and task["status"] in OPEN
            and (self.runtime.working_on(row["id"]) or row["task_id"]) == task_id
        )
        why = f"Cancelled by {by}" + (f": {reason}" if reason else ".")
        if by == "you" and reason.strip():
            self.runtime.learning.record(
                "cancel",
                context=self.about(task, task["goal"]),
                owner=reason,
                scope=self.runtime.learning.scope_of(task["session_id"]),
            )
        was_stopped = bool(self.store.get(f"stopped:{task_id}"))
        if task["status"] == "review":
            reported = (
                self.store.value("SELECT max(ended_at) FROM runs WHERE task_id=?", (task_id,))
                or task["updated_at"]
            )
            if by == "you" and not self.store.get(f"cancelask:{task_id}") and just_now(reported):
                # Its result came as the owner cancelled (the form open as it landed): they
                # meant to stop the work, not to drop a result they have not seen.
                self.store.set(f"cancelask:{task_id}", "1")
                raise ValueError(
                    "It finished just now: its result waits for your review. Accept it, or "
                    "cancel again to set it aside."
                )
            # Finished already: nothing to stop, its result stands.
            self.store.delete(f"cancelask:{task_id}")
            return self.set_aside(task, why, reason, by)
        if was := self.store.get(f"reopened:{task_id}"):
            # Only the follow-up on finished work is cancelled: its result stands.
            self.store.delete(f"reopened:{task_id}")
            started = self.store.value(
                "SELECT max(started_at) FROM runs WHERE task_id=?", (task_id,)
            )
            self.withdraw_drafts(task, why, since=started or "")
            task = self.tasks.move(task_id, was, progress=f"Follow-up cancelled ({why})")
        else:
            self.withdraw_drafts(task, why)
            task = self.tasks.move(task_id, "cancelled", progress=why)
            # Where it stopped: retried later, what other tasks did meanwhile is not its own.
            self.runtime.background(self.record_end(task))
        others = [
            other
            for other in self.tasks.for_session(task["session_id"])
            if other["id"] != task_id and other["status"] in OPEN
        ]
        note = (
            f"The follow-up on task {task_id} “{task['title']}” was cancelled: stop that work. "
            "Its earlier result stands; no report."
            if was
            else f"Task {task_id} “{task['title']}” was cancelled. Stop work on it; no report."
        )
        if row and not row["closed_at"] and others:
            # Its other tasks go on: its turn is not stopped (it may be on another task), only
            # this task's instructions it has not received are dropped, and it hears of the
            # cancellation within its turn, if it knew of the task at all.
            received = self.delivery.drop_pending(row["id"], task_id)
            if row["task_id"] == task_id:
                self.store.execute(
                    "UPDATE sessions SET task_id=? WHERE id=?", (others[-1]["id"], row["id"])
                )
            if (
                on_it
                and self.states.is_live(row["id"])
                and (row["status"] == "working" or self.states[row["id"]].held)
            ):
                # At work on it (or its turn on it over, a command it left running): that work
                # stops now (the running command too), and it goes on with its other tasks, with
                # what it was sent for them and never saw.
                self.runtime.asks.cancel_requests(row["id"], why, keep_questions=task_id)
                self.states[row["id"]].after_abort = True  # Its aborted turn owes no report.
                if handle := self.states.handle(row["id"]):
                    with suppress(Exception):
                        await handle.abort()
                    await stop_background(handle)
                self.states[row["id"]].busy = False
                self.states[row["id"]].steps.clear()
                if self.delivery.unsee(row["id"]):
                    # What it never saw about this task is not sent now: only the others'.
                    self.delivery.drop_pending(row["id"], task_id)
                self.runtime.update_session(row["id"], status="idle", activity="")
                listed = "; ".join(f"{other['id']} “{other['title']}”" for other in others)
                await self.delivery.deliver(
                    row["id"],
                    wrap("system", "cancelled", f"{note} Go on with your other open task(s): "
                         f"{listed}.", task=task_id),
                    kind="notice",
                    display=why,
                    meta={"task_id": task_id},
                )  # fmt: skip
            elif (
                was_stopped
                and (going := [o for o in others if not self.store.get(f"stopped:{o['id']}")])
                and row["status"] != "working"
                and self.delivery.undefer(row["id"])
            ):
                # Stopped on this task, then it is cancelled: what it was given for its other
                # tasks and held back goes out now, so it goes on with them (not with another
                # task the owner stopped). After a restart too: delivering resumes its session.
                listed = "; ".join(f"{other['id']} “{other['title']}”" for other in going)
                text = (f"{note} " if received else "") + f"Go on with your open task(s): {listed}."
                await self.delivery.deliver(
                    row["id"],
                    wrap("system", "cancelled" if received else "go-on", text, task=task_id),
                    kind="notice",
                    display=why,
                    meta={"task_id": task_id},
                )
            elif received and row["status"] == "working" and self.states.is_live(row["id"]):
                # Not "stopped": its turn goes on, and it still owes a report on its other task.
                await self.delivery.deliver(
                    row["id"],
                    wrap("system", "cancelled", note, task=task_id),
                    kind="notice",
                    display=why,
                    meta={"task_id": task_id},
                    steer=True,
                )
            elif received:
                self.runtime.add_message(
                    row["id"],
                    "notice",
                    why,
                    {
                        "prompt": wrap("system", "cancelled", note, task=task_id),
                        "deferred": True,
                        "task_id": task_id,
                    },
                )
        elif row and not row["closed_at"]:
            if on_it:
                self.runtime.asks.cancel_requests(row["id"], why, keep_questions=task_id)
                self.states[row["id"]].after_abort = True
                if handle := self.states.handle(row["id"]):
                    with suppress(Exception):
                        await handle.abort()
                    await stop_background(handle)  # And what it left running in the background.
                self.states[row["id"]].busy = False
                self.states[row["id"]].after_refresh = ""  # Not "go on" once it is stopped.
                # What its aborted turn never saw was about the cancelled task: not sent again.
                self.states[row["id"]].echo_wait.clear()
                self.delivery.defer_pending(row["id"])  # Its task, if it never started.
                if row["status"] in {"working", "queued", "starting", "waiting"}:
                    self.runtime.update_session(row["id"], status="idle", activity="")
            # Pending, not sent: it reaches the worker with its next message, not as a new turn.
            self.runtime.add_message(
                row["id"],
                "notice",
                why,
                {
                    "prompt": wrap("system", "cancelled", note, task=task_id),
                    "stopped": True,
                    "deferred": True,
                },
            )
        with suppress(RuntimeError):
            display = f"Task “{task['title']}” cancelled"
            meta = {"session_id": task["session_id"], "task_id": task_id}
            if by.lower().removeprefix("the ") == "router":
                self.runtime.add_message(self.runtime.router_id(), "notice", display, meta)
            else:
                # The Router hears of it with its next message (not a turn of its own): it must
                # not start that work again by itself, here or in another worker. In a turn now
                # (the one that started it, say), it hears of it in that turn: what it tells the
                # owner must not say the task goes on.
                who = "The owner" if by == "you" else by[:1].upper() + by[1:]
                told = (
                    f"{who} cancelled the follow-up on task {task_id} “{task['title']}”; its "
                    "earlier result stands."
                    if was
                    else f"{who} cancelled task {task_id} “{task['title']}”."
                )
                told += f" Reason: {reason.strip()}" if reason.strip() else ""
                told += (
                    " Do not start that work again (in this worker or another) unless the owner "
                    "asks for it."
                )
                router = self.runtime.router_id()
                note = wrap("system", "cancelled", told, task=task_id, title=task["title"])
                working = self.store.session(router)["status"] == "working"
                if working and self.runtime.states.is_live(router):
                    try:
                        await self.delivery.deliver(
                            router, note, kind="notice", display=display, meta=meta, steer=True
                        )
                    except Exception:
                        logger.warning("Could not tell the Router of a cancel.", exc_info=True)
                else:
                    self.delivery.hold(router, note, kind="notice", display=display, meta=meta)
        self.runtime.background(
            self.runtime.workers.pump()
        )  # Tasks that depended on it are now blocked.
        return task

    def set_aside(self, task: dict, why: str, reason: str, by: str) -> dict:
        """Cancelling a task whose result was reported already: nothing is stopped, its result
        stands (done, not "Cancelled: <its report>"), and whoever cancelled it hears it had
        finished. Its drafts still waiting for the owner go."""
        withdrawn = self.withdraw_drafts(task, why)
        unaccepted = task["status"] == "review"
        # Not wanted any more: what it followed for it (a thread, a PR) it follows no longer.
        self.drop_follows(task)
        why = why if why.rstrip().endswith((".", "!", "?")) else f"{why.rstrip()}."
        task = self.tasks.move(
            task["id"], "done", progress=f"{why} It had finished: set aside, not accepted."
        )
        if unaccepted:
            # Done, but not accepted: Home shows it as set aside.
            self.store.set(f"setaside:{task['id']}", "1")
        self.publish(task)
        told = (
            f"Task {task['id']} “{task['title']}” had finished already (its result was "
            "reported): nothing was stopped; it is set aside, not accepted (its result is "
            "kept for reference, not to act on)."
        )
        if withdrawn:
            told += f" Its {len(withdrawn)} draft(s) waiting for the owner were withdrawn."
        with suppress(RuntimeError):
            router = self.runtime.router_id()
            display = f"“{task['title']}” had finished: set aside, not accepted"
            meta = {"session_id": task["session_id"], "task_id": task["id"]}
            if by.lower().removeprefix("the ") == "router":
                self.runtime.add_message(router, "notice", display, meta)
            else:
                who = "The owner" if by == "you" else by[:1].upper() + by[1:]
                note = f"{who} cancelled it. {told}"
                note += f" Reason: {reason.strip()}" if reason.strip() else ""
                self.delivery.hold(
                    router,
                    wrap("system", "cancelled", note, task=task["id"], title=task["title"]),
                    kind="notice",
                    display=display,
                    meta=meta,
                )
        self.runtime.background(self.runtime.workers.pump())
        return task

    def drop_follows(self, task: dict):
        """Its worker stops following what it followed for this task only (not a thread another
        of its tasks follows too)."""
        session_id = task["session_id"]
        for key in self.followed_for(task, self.store.follows(session_id)):
            bound = [
                t
                for t in self.store.follow_tasks(session_id, key)
                if t not in {task["id"], NO_TASK}
            ]
            if bound:
                self.store.set(f"followtask:{session_id}:{key}", json.dumps(bound))
            else:
                self.store.unfollow(key, session_id)
        if row := self.store.find_session(session_id):
            self.runtime.publish_session(row)

    def withdraw_drafts(self, task: dict, why: str, since: str = "") -> list[dict]:
        """Its replies and reviews still waiting for the owner go with it (`since`: only those
        drafted from then on, for a follow-up); not those of its worker's other tasks."""
        gone = []
        for draft in self.store.drafts():
            if draft["kind"] not in {"slack", "github", "reply"} or draft["created_at"] < since:
                continue
            found = self.runtime.drafts.task_of(draft)
            if draft["session_id"] != task["session_id"] or not found or found["id"] != task["id"]:
                continue
            if self.runtime.drafts.owner_edited(draft):
                continue  # The owner's own text: theirs to send or drop.
            draft = self.store.update_draft(
                draft["id"], status="rejected", outcome=f"Withdrawn: {why}", resolved_at=now()
            )
            self.bus.publish("draft", draft)
            gone.append(draft)
        return gone

    def rename(self, task_id: str, title: str, by: str = "you") -> dict:
        """A task named for what it delivers; its earlier names are kept (shown on hover)."""
        title = deliverable_title(title)
        task = self.tasks.get(task_id)
        if title == task["title"]:
            return self.view(task)
        names = json.loads(self.store.get(f"titles:{task_id}") or "[]")
        names.append({"title": task["title"], "at": now(), "by": by})
        self.store.set(f"titles:{task_id}", json.dumps(names[-20:]))
        before = task["title"]
        task = self.tasks.update(task_id, title=title)
        row = self.store.session(task["session_id"])
        self.runtime.publish_session(row)
        if row["role"] == WORKER and not row["closed_at"]:
            # Its worker knows it by the new name too (a retry names it; "that is not what
            # this task is" otherwise). Read with its next message, not a turn of its own.
            who = "The owner" if by == "you" else by[:1].upper() + by[1:]
            note = f"{who} renamed your task {task_id} “{before}” to “{title}”. Same task."
            self.delivery.hold(
                row["id"],
                wrap("system", "renamed", note, task=task_id),
                kind="notice",
                display=f"Renamed “{before}” to “{title}”",
            )
        return self.view(task)

    def split_parts(self, task: dict) -> tuple[dict, dict, list[dict]]:
        """What a split would do: (the result it keeps, the latest result that moves, the
        messages that move with it: from the instruction that asked for that result on).
        Raises when there is nothing to split off."""
        if task["status"] not in {"review", "done"}:
            raise ValueError(
                f"Task {task['id']} is {task['status']}: split it once it is finished."
            )
        messages = self.store.all(
            "SELECT id, kind, text, meta, created_at FROM messages WHERE session_id=? "
            "AND json_extract(meta, '$.task_id')=? ORDER BY id",
            (task["session_id"], task["id"]),
        )
        reports = [
            m for m in messages
            if m["kind"] == "report" and json.loads(m["meta"] or "{}").get("status") == "done"
        ]  # fmt: skip
        if len(reports) < 2:
            raise ValueError(
                "It has one result only: rename it instead, or give the other work a new task."
            )
        earlier, latest = reports[-2], reports[-1]
        # The instructions that asked for the latest result: those since the worker last
        # answered before it (a reply that asks nothing back, or a report on this task). An
        # exchange about the earlier result (a decision on it, a question answered) stays.
        between = self.store.all(
            "SELECT id, kind, text, meta FROM messages WHERE session_id=? AND id>? AND id<? "
            "ORDER BY id DESC",
            (task["session_id"], earlier["id"], latest["id"]),
        )
        asked: list[int] = []
        for message in between:
            meta = json.loads(message["meta"] or "{}")
            if message["kind"] in {"router", "owner"} and meta.get("task_id") == task["id"]:
                asked.append(message["id"])
            elif (
                asked
                and not meta.get("narration")
                and (
                    message["kind"] == "assistant"
                    or (message["kind"] == "report" and meta.get("task_id") == task["id"])
                )
                and not message["text"].rstrip().endswith(("?", "\uff1f"))
            ):
                break
        first = min(asked) if asked else latest["id"]
        moving = [m for m in messages if m["id"] >= first]
        return dict(earlier), dict(latest), [dict(m) for m in moving]

    def splittable(self, task: dict) -> bool:
        """Whether its latest result looks like other work than the one before (it names other
        PRs, files or links), so splitting it off is worth offering."""
        try:
            earlier, latest, _ = self.split_parts(task)
        except ValueError:
            return False

        def named(text: str) -> set[str]:
            found = {f"{repo}#{number}".lower() for repo, number in threads.PR_URL.findall(text)}
            found |= {threads.link_key(url) for url in threads.links(text)}
            found |= {code for code in re.findall(r"`([^`\n]{3,80})`", text) if "." in code}
            return found

        new = named(latest["text"])
        return bool(new) and not new <= named(earlier["text"])

    def split_preview(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        try:
            earlier, latest, moving = self.split_parts(task)
        except ValueError as exc:
            return {"allowed": False, "why": str(exc)}
        return {
            "allowed": True,
            "why": "" if self.splittable(task) else "Its results look like the same work.",
            "keeps": {"result": earlier["text"][:4000], "at": earlier["created_at"]},
            "moves": {
                "result": latest["text"][:4000],
                "at": latest["created_at"],
                "messages": [
                    {k: m[k] for k in ("id", "kind", "created_at")} | {"text": m["text"][:300]}
                    for m in moving
                ],
            },
        }

    def split(self, task_id: str, title: str, by: str = "you") -> dict:
        """A finished task that came to hold two deliverables: its latest result becomes a
        task of its own, named for it; the task keeps the result it had before. That result's
        report and the messages that asked for it move with it; the worker's name, the task's
        follows and the rest stay. Undone with merge_back within SPLIT_UNDO_MINUTES."""
        title = deliverable_title(title)
        task = self.tasks.get(task_id)
        earlier, latest, moving = self.split_parts(task)
        # Finished work split in two: what its worker is on now stays its current task.
        current = self.store.value("SELECT task_id FROM sessions WHERE id=?", (task["session_id"],))
        made = self.tasks.create(
            task["session_id"], title, f"Split off task {task_id}.", status=task["status"]
        )
        self.store.execute(
            "UPDATE sessions SET task_id=? WHERE id=?", (current, task["session_id"])
        )
        self.tasks.start_run(made["id"], "split")
        made = self.tasks.move(made["id"], task["status"], result=latest["text"])
        for row in moving:
            meta = json.loads(row["meta"] or "{}")
            self.store.update_message_meta(row["id"], {**meta, "task_id": made["id"]})
        undo = self.split_changes(task, made, latest, moving[0]["created_at"] if moving else now())
        names = json.loads(self.store.get(f"titles:{made['id']}") or "[]")
        names.append({"title": f"split from “{task['title']}”", "at": now(), "by": by})
        self.store.set(f"titles:{made['id']}", json.dumps(names))
        mark = self.store.value("SELECT max(id) FROM messages") or 0
        until = (datetime.now(UTC) + timedelta(minutes=SPLIT_UNDO_MINUTES)).isoformat()
        self.store.set(
            f"splitfrom:{made['id']}",
            json.dumps({
                "task_id": task_id, "until": until, "mark": mark, "status": made["status"],
                "moved": [m["id"] for m in moving], "result": task["result"], "undo": undo,
            }),
        )  # fmt: skip
        self.tasks.update(task_id, result=earlier["text"])
        # Its result again, not one of its earlier ones (the one split off went with it).
        kept = json.loads(self.store.get(f"results:{task_id}") or "[]")
        if kept and kept[0].get("text") == earlier["text"]:
            self.store.set(f"results:{task_id}", json.dumps(kept[1:]))
        self.publish(self.tasks.get(task_id))
        self.runtime.publish_session(self.store.session(task["session_id"]))
        return self.view(self.tasks.get(made["id"]))

    def last_message(self, session_id: str, task_id: str) -> int:
        """The id of the newest message about this task: when it last saw activity."""
        return (
            self.store.value(
                "SELECT max(id) FROM messages WHERE session_id=? "
                "AND json_extract(meta, '$.task_id')=?",
                (session_id, task_id),
            )
            or 0
        )

    def worked_on_since_split(self, task_id: str, found: dict) -> bool:
        """Whether the task split off saw work since (a follow-up, a report, a status of its
        own): undoing the split then would lose it."""
        task = self.tasks.find(task_id)
        if not task or task["status"] != found.get("status", task["status"]):
            return True
        return self.last_message(task["session_id"], task_id) > int(found.get("mark") or 0)

    def split_from(self, task_id: str) -> dict | None:
        found = json.loads(self.store.get(f"splitfrom:{task_id}") or "null")
        if found and self.worked_on_since_split(task_id, found):
            # Its Undo is gone: it is a task of its own now.
            return {"task_id": found["task_id"], "until": ""}
        return {"task_id": found["task_id"], "until": found["until"]} if found else None

    def merge_back(self, task_id: str) -> dict:
        """Undo a split, soon after: the result and messages go back to the task they came from."""
        found = json.loads(self.store.get(f"splitfrom:{task_id}") or "null")
        if not found:
            raise ValueError("That task was not split off.")
        if found["until"] < now():
            raise ValueError("Too late to undo the split: rename or cancel the task instead.")
        if self.worked_on_since_split(task_id, found):
            raise ValueError(
                "It was worked on since the split, so it stays a task of its own: rename or "
                "cancel it instead."
            )
        made = self.tasks.get(task_id)
        original = found["task_id"]
        for message_id in found["moved"]:
            row = self.store.one("SELECT meta FROM messages WHERE id=?", (message_id,))
            if row:
                meta = json.loads(row["meta"] or "{}")
                self.store.update_message_meta(message_id, {**meta, "task_id": original})
        self.store.delete(f"splitfrom:{task_id}")
        self.store.execute(
            "DELETE FROM task_deps WHERE task_id=? OR depends_on=?", (task_id, task_id)
        )
        undo = found.get("undo") or {}
        if undo.get("ended"):
            self.store.set(f"taskend:{original}", json.dumps(undo["ended"]))
        back = self.store.one(
            "SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (original,)
        )
        if back:
            for identifier in undo.get("commands") or []:
                self.store.execute(
                    "UPDATE commands SET run_id=? WHERE id=?", (back["id"], identifier)
                )
        for key in ("taskbase", "taskend", "taskends", "touched"):
            self.store.delete(f"{key}:{task_id}")
        self.store.execute("DELETE FROM runs WHERE task_id=?", (task_id,))
        self.store.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        restored = self.tasks.update(original, result=found.get("result") or made["result"])
        self.runtime.background(self.collect_evidence(original))
        self.publish(restored)
        self.runtime.publish_state()  # The split-off task is gone from every list.
        self.runtime.publish_session(self.store.session(made["session_id"]))
        return self.view(restored)

    def start_of_more(self, task: dict) -> tuple[str, dict]:
        """When and where (as a task's base, see record_base) work on a finished task started
        again: its checkouts when it was last taken up again after its result (the owner's
        chat, its worker back from other work), else when its result left them."""
        latest = self.result_at(task)
        bases = [
            found
            for key in ("chatbase", "resumed")
            if (found := json.loads(self.store.get(f"{key}:{task['id']}") or "{}")).get("at")
            and found["at"] > latest
        ]
        if bases:
            base = max(bases, key=lambda found: found["at"])
            at, trees = base["at"], base.get("trees") or {}
        else:
            at, trees = latest, json.loads(self.store.get(f"taskend:{task['id']}") or "{}")
        if not at or not trees:
            return at, {}
        return at, self.base_from(at, trees)

    @staticmethod
    def base_from(at: str, trees: dict) -> dict:
        """A task's base (see record_base) from snapshots of its checkouts taken at `at`."""
        commits = {path: found["commit"] for path, found in trees.items() if found.get("commit")}
        return {
            "at": datetime.fromisoformat(at).timestamp(),
            "commits": commits,
            "trees": {path: found["tree"] for path, found in trees.items() if found.get("tree")},
            "heads": commits,
            "large": {path: found.get("large", []) for path, found in trees.items()},
        }

    async def record_asked_base(self, task: dict):
        """Its checkouts when the owner writes in the chat of a task still at work: separate
        work that message asks for (see split_off) starts there, not where the task did, so
        the task keeps what it did before. One per message (the latest few)."""
        row = self.store.find_session(task["session_id"])
        if not row:
            return
        row = await self.adopt_clones(row)
        trees = {}
        for checkout in self.workspaces.checkouts(row):
            with suppress(Exception):
                if (taken := await snapshot(checkout)) and taken["tree"]:
                    trees[checkout["path"]] = taken
        found = json.loads(self.store.get(f"askedbase:{task['id']}") or "[]")
        found = [*found, {"at": now(), "trees": trees}][-6:]
        self.store.set(f"askedbase:{task['id']}", json.dumps(found))

    def asked_base(self, task: dict, since: str) -> dict:
        """Where separate work asked for at `since` in the chat of a task with no result yet
        starts: its checkouts as that message came (see record_asked_base); {} when they were
        not recorded since it was taken up."""
        started = self.store.value(
            "SELECT max(started_at) FROM runs WHERE task_id=?", (task["id"],)
        )
        found = [
            taken
            for taken in json.loads(self.store.get(f"askedbase:{task['id']}") or "[]")
            if (started or "") <= taken["at"] <= since
        ]
        return self.base_from(found[-1]["at"], found[-1]["trees"]) if found else {}

    def split_off(self, task: dict, title: str) -> dict:
        """A report on work apart from a task's result (its worker says it is another
        deliverable, or it is on another branch than the task's open PR): a new task of the
        same worker gets it, with what asked for it since the task was taken up again (the
        owner's or the Router's messages, the commands run). The task keeps its result and PR,
        as it was before (a follow-up that was this other work is over). A task with no result
        yet goes on as it was: the owner's chat message that asked for this work moves (see
        asked_apart), and the new task's share starts where its checkouts were then (see
        record_asked_base)."""
        title = deliverable_title(title)
        earlier = self.tasks.find(self.store.get(f"splitoff:{task['id']}") or "")
        if earlier and earlier["status"] == "running" and not earlier["result"]:
            return earlier  # Reported again (refused for something else the first time).
        if task["result"]:
            since, base = self.start_of_more(task)
            asked = (
                self.store.all(
                    "SELECT id, text, meta FROM messages WHERE session_id=? AND kind IN "
                    "('owner', 'router') AND json_extract(meta, '$.task_id')=? AND created_at>=? "
                    "ORDER BY id",
                    (task["session_id"], task["id"], since),
                )
                if since
                else []
            )
        else:
            # Only what asked for this work, and its checkouts then: what the task did before
            # (on what the owner wrote about it earlier) stays its own.
            asked = self.asked_apart(task)
            since = asked[0]["created_at"] if asked else ""
            base = self.asked_base(task, since) if since else {}
        made = self.tasks.create(
            task["session_id"], title, f"Split off task {task['id']}.", status="running"
        )
        self.store.set(f"splitoff:{task['id']}", made["id"])
        self.store.set(f"started:{made['id']}", "1")  # Taken up: it started where `base` says.
        if base:
            self.store.set(f"taskbase:{made['id']}", json.dumps(base))
        for message in asked:
            meta = json.loads(message["meta"] or "{}")
            self.store.update_message_meta(message["id"], {**meta, "task_id": made["id"]})
        if asked:
            goal = "\n\n".join(message["text"] for message in asked)[:4000]
            made = self.tasks.update(made["id"], goal=goal)
        if since:
            run = self.tasks.start_run(made["id"], "split")
            self.store.execute(
                "UPDATE commands SET run_id=? WHERE id IN (SELECT c.id FROM commands c JOIN runs "
                "r ON r.id=c.run_id WHERE r.task_id=? AND c.started_at>=?)",
                (run["id"], task["id"], since),
            )
        if touched := self.store.get(f"touched:{task['id']}"):
            self.store.set(f"touched:{made['id']}", touched)
        names = [{"title": f"split from \u201c{task['title']}\u201d", "at": now(), "by": "worker"}]
        self.store.set(f"titles:{made['id']}", json.dumps(names))
        # A task with no result yet stays as it is: its worker goes on with it (the task it is
        # on), and what it runs from here is that task's.
        if not task["result"]:
            self.store.execute(
                "UPDATE sessions SET task_id=? WHERE id=?", (task["id"], task["session_id"])
            )
        else:
            for key in ("followup", "morefor", "newresult", "ownchange"):
                self.store.delete(f"{key}:{task['id']}")
            if task["status"] in OPEN and (was := self.store.get(f"reopened:{task['id']}")):
                self.tasks.move(task["id"], was)
            self.store.set(f"turntask:{task['session_id']}", made["id"])
        if task["result"]:
            # Its receipts again, without what was this work (a worktree made for it, say).
            self.runtime.background(self.collect_evidence(task["id"]))
        self.publish(self.tasks.get(task["id"]))
        return made

    def asked_in_chat(self, task: dict) -> list[dict]:
        """What the owner wrote to a task's worker about it since it was taken up (its latest
        run), not their answers to its questions: with no result yet, all that may have asked
        for work apart from it."""
        started = self.store.value(
            "SELECT max(started_at) FROM runs WHERE task_id=?", (task["id"],)
        )
        return self.store.all(
            "SELECT id, text, meta, created_at FROM messages WHERE session_id=? AND kind='owner' "
            "AND json_extract(meta, '$.task_id')=? AND json_extract(meta, '$.answered') IS NULL "
            "AND created_at>=? ORDER BY id",
            (task["session_id"], task["id"], started or ""),
        )

    def asked_apart(self, task: dict) -> list[dict]:
        """The owner's chat message that asked for the separate work its worker reports now,
        on a task with no result yet: the latest one it has seen (one still on its way is not
        what it reports). Only that one: what the owner wrote before it the worker may have
        acted on for the task (unsure, fewer move)."""
        asked = self.asked_in_chat(task)
        if unread := self.states[task["session_id"]].unread:
            first = min(entry[2] for entry in unread.values())
            asked = [message for message in asked if message["id"] < first]
        return asked[-1:]

    async def off_branch(self, task: dict) -> tuple[str, str, str] | None:
        """(its PR, that PR's branch, the branch it is on now) when the worker did code work for
        this task, taken up again since its result, on another branch than that of the task's
        own open PR: that work is not that PR's. None when it is on it, did no such work (an
        answer in chat, PR news, read while it is on another task's branch), or the task has
        no open PR of its own."""
        prs = [
            pr
            for pr in self.previous_prs(task["id"])
            if pr.get("own") and pr.get("state") == "OPEN" and pr.get("branch")
        ]
        row = self.store.find_session(task["session_id"])
        if not prs or not row:
            return None
        latest = self.result_at(task)
        again = self.runtime.turn_task(row["id"]) == task["id"] or any(
            json.loads(self.store.get(f"{key}:{task['id']}") or "{}").get("at", "") > latest
            for key in ("chatbase", "resumed")
        )
        # Only work for it: since it was taken up again (another task's work in between is
        # not), as changed_since_result counts it.
        if not again or not await self.changed_since_result(task):
            return None
        found = None
        for checkout in self.workspaces.checkouts(row):
            tree = checkout.get("worktree") or checkout["path"]
            if not await asyncio.to_thread(os.path.isdir, tree):
                continue
            with suppress(ValueError):
                current = (
                    await checkout_git(checkout, tree, "rev-parse", "--abbrev-ref", "HEAD")
                ).strip()
                for pr in prs:
                    if current == pr["branch"]:
                        return None
                    if current in {"", "HEAD"} or found:
                        continue
                    with suppress(ValueError):
                        ref = f"refs/heads/{pr['branch']}"
                        await checkout_git(checkout, tree, "rev-parse", "--verify", "--quiet", ref)
                        found = (pr["label"], pr["branch"], current)
        return found

    def keep_earlier_result(self, task: dict, newer: str):
        """Bug E34: its result is replaced by a newer report on it: the one shown until now is
        kept, the newest first, for its card's Earlier reports (not lost, not shown as its
        result)."""
        if not task.get("result") or task["result"] in {newer, ADOPTED}:
            return
        kept = json.loads(self.store.get(f"results:{task['id']}") or "[]")
        if kept and kept[0].get("text") == task["result"]:
            return
        at = task.get("ended_at") or task["updated_at"]
        kept.insert(0, {"at": at, "text": task["result"]})
        self.store.set(f"results:{task['id']}", json.dumps(kept[:10]))

    def news(self, task_id: str, text: str):
        """What a worker reported about its finished work (its PR approved or merged, a reply
        handled): kept in sight on the board until the owner has seen it."""
        self.store.set(f"news:{task_id}", json.dumps({"text": text.strip()[:2000], "at": now()}))

    def news_seen(self, task_id: str) -> dict:
        self.store.delete(f"news:{task_id}")
        task = self.tasks.get(task_id)
        self.publish(task)
        return task

    def accept(self, task_id: str, as_is: bool = True) -> dict:
        """The owner accepts its result. `as_is=False`: settled by sending a draft the owner
        edited first (what they changed is learned from that draft, not as agreement)."""
        task = self.tasks.get(task_id)
        if task["status"] != "review":
            raise ValueError(f"Task {task_id} is {task['status']}, not ready for review.")
        self.store.delete(f"news:{task_id}")  # Seen with the result.
        self.store.delete(f"setaside:{task_id}")
        task = self.tasks.move(task_id, "done")
        self.runtime.background(self.runtime.workers.pump())  # Tasks waiting for its check.
        if as_is:
            scope = self.runtime.learning.scope_of(task["session_id"])
            self.runtime.learning.record("accept", context=task["title"], scope=scope, agreed=True)
        return task

    def settle_old_reports(self, shown_hours: float):
        """Once, on the first start of a version where every result waits for Accept: a
        result an earlier version counted as finished once read (a report that changed no code,
        opened no PR still open, had no criteria or flag to check) is accepted when the owner
        read it, its worker is closed, or it is older than Home showed: the owner settled those
        already. A recent one they never opened waits for Accept like any result now."""
        if self.store.get("migrated:accept_all"):
            return
        cutoff = hours_ago(shown_hours)
        for task in self.store.all("SELECT * FROM tasks WHERE status='review'"):
            row = self.store.find_session(task["session_id"])
            ended = task.get("ended_at") or task["updated_at"]
            if not (
                self.store.get(f"read:{task['id']}")
                or not row
                or row["closed_at"]
                or ended < cutoff
            ) or not self.informed_only(task):
                continue
            self.tasks.move(task["id"], "done")
            self.tasks.update(task["id"], ended_at=ended)  # Finished when it finished, not now.
        self.store.set("migrated:accept_all", now())

    def informed_only(self, task: dict) -> bool:
        """A result an earlier version did not ask the owner to accept (see
        settle_old_reports)."""
        shown = self.view(task)
        receipts = shown.get("receipts") or {}
        changed = receipts.get("changed") or {}
        return not (
            shown.get("review_required")
            or changed.get("files")
            or changed.get("commits")
            or changed.get("uncommitted")
            or any(
                pr.get("own") and pr.get("state") not in {"MERGED", "CLOSED"}
                for pr in receipts.get("prs") or []
            )
            or (receipts.get("criteria") and receipts.get("verdict") != "verified")
            or receipts.get("flags")
        )

    async def request_changes(self, task_id: str, text: str, by: str = "you") -> dict:
        """The owner does not accept a finished result (to accept, or accepted already): it
        goes back to its worker, running again, with what to change; its next report is a new
        result to accept. Kept as a follow-up (learned from as one)."""
        task = self.tasks.get(task_id)
        if task["status"] not in {"review", "done"} or self.store.get(f"setaside:{task_id}"):
            raise ValueError(
                f"Task {task_id} is {task['status']}: changes are asked of a finished result."
            )
        if not text.strip():
            raise ValueError("Say what to change.")
        return await self.follow_up(task_id, text.strip(), changes=by)

    async def answer_question(self, question_id: str, text: str) -> dict:
        """The owner answers a worker's question kept under Needs you (by its id, or by its
        worker's). Answering it, they want the work: a task cancelled or finished meanwhile
        goes on with the answer."""
        question = self.runtime.asks.owner_question(question_id)
        if not question:
            raise ValueError("That question was answered or dismissed already.")
        session_id = question.get("id", question_id).split(":", 1)[0]
        task = self.tasks.find(question["task_id"]) if question["task_id"] else None
        if not task:
            self.runtime.asks.dismiss(question["id"], "answered")  # That one: its others stay.
            if self.runtime.asks.session_questions(session_id):
                # It asked several: which one this answers rides along.
                said = " ".join(question["text"].split())[:1500]
                self.delivery.hold(
                    session_id,
                    wrap("system", "context", f"The owner's next message answers: {said}"),
                    kind="notice",
                    display=f"Answering: {said[:200]}",
                )
            await self.runtime.owner_send(session_id, text, clear_questions=False)
            return {"session_id": session_id}
        return await self.follow_up(task["id"], text, resume=True)

    async def follow_up(
        self, task_id: str, text: str, resume: bool = False, changes: str = ""
    ) -> dict:
        """The owner adds to a task: its worker continues it (a finished task reopens).
        `resume`: the owner answered its question after it was cancelled: it goes on.
        `changes`: who did not accept its result ("you": the owner) and asks for these
        changes: its worker hears so plainly."""
        task = self.tasks.get(task_id)
        # Go on (a stopped task's button, from two tabs say): sent once, while it is stopped.
        going_on = text.strip() == "Go on where you stopped."

        def gone_on(current: dict) -> bool:
            stopped = current["status"] == "waiting" and self.store.get(f"stopped:{task_id}")
            return going_on and not stopped and current["status"] != "cancelled"

        if gone_on(task):
            return task
        if task["status"] == "cancelled" and not resume:
            raise ValueError("That task was cancelled. Ask the Router for new work instead.")
        row = self.store.session(task["session_id"])
        if row["closed_at"]:
            await self.runtime.workers.reopen(row["id"])
            if gone_on(now := self.tasks.get(task_id)):
                return now
        self.store.delete(f"news:{task_id}")  # Answering it, the owner has seen its news.
        self.runtime.asks.answered(task["session_id"], task_id)
        before = task["status"]
        self.runtime.learning.record(
            "follow-up",
            context=self.about(task, task["goal"]),
            proposed=task["result"],
            owner=text,
            scope=self.runtime.learning.scope_of(task["session_id"]),
            agreed=False if before in {"review", "done"} else None,
            untrusted=bool(row["slack"]),
        )
        if before == "cancelled":
            # Answered after it was cancelled: the owner wants it after all.
            self.drop_cancelled_note(row["id"], task_id)
            with suppress(RuntimeError):
                self.drop_cancelled_note(self.runtime.router_id(), task_id)
        self.store.delete(f"setaside:{task_id}")  # Wanted after all.
        if before in {"review", "done", "failed", "cancelled"}:
            # Cancelling this follow-up gives the finished result back.
            self.store.set(f"reopened:{task_id}", before)
            task = self.tasks.move(task_id, "running", reason="follow-up")
        elif before == "waiting" and self.store.get(f"askedafter:{task_id}"):
            # Finished work it asked the owner about: answered, it is back where it was. What
            # its worker says next is news on it, unless it changes the task's code from here
            # (its checkouts now): then that is a new result.
            await self.record_chat_base(task)
            task = self.runtime.asks.go_on(task, told=True)
        elif before == "waiting":
            # The owner answered what it waited for: it goes on (and asks again if need be).
            task = self.tasks.move(task_id, "running", progress="")
        spoken = self.runtime.heard(text)
        said, shown = text, text
        if changes:
            who = "The owner" if changes == "you" else changes[:1].upper() + changes[1:]
            said = (
                f"{who} did not accept your result of task {task_id} \u201c{task['title']}\u201d: "
                f"change it as follows, then report it again (done) with the whole result.\n\n"
                f"{text}"
            )
            shown = f"Changes requested: {text}"
        prompt = wrap("owner", "message", said, task=task_id, reply_in=spoken)
        if switched := self.runtime.switched_language(row["id"], spoken):
            # A conversation in one language pulls the answer back into it.
            prompt += "\n\n" + wrap("system", "context", switched)
        if task["result"] and (
            before in {"review", "done"}
            or (before == "waiting" and task["status"] in {"review", "done"})
        ):
            # More on a result it reported (or its answer): its next report replaces that
            # result, its criteria too.
            restate = prompts.restate(self.tasks.criteria(task))
            prompt += "\n\n" + wrap("system", "context", restate)
            self.store.set(f"morefor:{task_id}", "1")  # Its next report is checked (Tools).
        if before == "cancelled":
            prompt += "\n\n" + wrap(
                "system",
                "context",
                f"Task {task_id} “{task['title']}” was cancelled before; with this answer the "
                "owner wants it after all, and it is running again. Do it now with their "
                "answer, and report.",
            )
        try:
            await self.delivery.deliver(
                row["id"],
                prompt,
                kind="owner",
                display=shown,
                meta={"task_id": task_id},
                steer=True,
            )
        except Exception:
            if task["status"] != before:
                self.store.delete(f"reopened:{task_id}")
                self.tasks.move(task_id, before)  # The follow-up never reached its worker.
            raise
        self.runtime.owner_told(row, shown)
        return task

    def drop_cancelled_note(self, session_id: str, task_id: str):
        """The note that a task was cancelled, not sent yet, is moot once it is wanted again."""
        mark = int(self.store.get(f"sent:{session_id}") or 0)
        for message in self.store.messages(session_id, after=mark):
            prompt = message["meta"].get("prompt", "")
            if 'type="cancelled"' in prompt and f'task="{task_id}"' in prompt:
                kept = {k: v for k, v in message["meta"].items() if k != "prompt"}
                self.store.update_message_meta(message["id"], {**kept, "dropped": True})

    async def retry(self, task_id: str, text: str = "") -> dict:
        """Run a failed or cancelled task again in its worker (reopened if closed)."""
        task = self.tasks.get(task_id)
        if task["status"] not in {"failed", "cancelled"}:
            raise ValueError(
                f"Task {task_id} is {task['status']}; only failed or cancelled tasks are retried."
            )
        row = self.store.session(task["session_id"])
        if row["closed_at"]:
            await self.runtime.workers.reopen(row["id"])
            task = self.tasks.get(task_id)
            if task["status"] not in {"failed", "cancelled"}:
                return task  # Retried meanwhile (a second click): once is enough.
        before = task
        if text.strip():
            self.runtime.learning.record(
                "retry",
                context=self.about(task, task["progress"]),
                owner=text,
                scope=self.runtime.learning.scope_of(task["session_id"]),
            )
        self.store.set(f"reopened:{task_id}", before["status"])
        if before["status"] == "cancelled":
            self.drop_cancelled_note(row["id"], task_id)  # Moot: it is wanted after all.
            with suppress(RuntimeError):
                self.drop_cancelled_note(self.runtime.router_id(), task_id)
        task = self.tasks.move(task_id, "running", reason="retry", progress="")
        body = f"The owner asks you to try task {task_id} “{task['title']}” again."
        if text.strip():
            body += f"\n\n{text.strip()}"
        spoken = self.runtime.heard(text) or self.store.get("owner_language") or ""
        prompt = wrap("owner", "message", body, task=task_id, reply_in=spoken)
        if text.strip() and (switched := self.runtime.switched_language(row["id"], spoken)):
            prompt += "\n\n" + wrap("system", "context", switched)
        shown = text.strip() or "Try again."
        try:
            await self.delivery.deliver(
                row["id"],
                prompt,
                kind="owner",
                display=shown,
                meta={"task_id": task_id},
            )
        except Exception:
            self.store.delete(f"reopened:{task_id}")
            self.tasks.move(task_id, before["status"], progress=before["progress"])
            raise
        # What the owner did, as their chat shows it: the task's id is for its worker only.
        self.runtime.owner_told(row, shown)
        return task

    def followed_for(self, task: dict, follows: list[str]) -> list[str]:
        """The threads and PRs its worker follows for this task: those it was given or started
        following for it; from before follows were tied to a task, a PR it is the one task for
        (see legacy_follower) and a Slack thread when the worker is on it. Not every follow of
        the worker: one tied to another task, or to none, is not this one's; nor a PR its
        title only names while another task (or several) names it too."""
        if task["status"] in {"cancelled", "failed"} or not follows:
            return []
        session_id = task["session_id"]
        # The tasks each is followed for (Store.follow_tasks), all in one query.
        bindings = self.store.items(f"followtask:{session_id}:")
        # The task it is on, for a Slack thread tied to no task.
        current = (
            self.tasks.current(session_id)
            if task["status"] in OPEN and not all(map(threads.GITHUB_PR.fullmatch, follows))
            else None
        )
        split: bool | None = None
        found = []
        for key in follows:
            bound = json.loads(bindings.get(key) or "[]")
            if bound:
                if task["id"] in bound:
                    found.append(key)
                continue
            pr = threads.GITHUB_PR.fullmatch(key)
            if pr and split is None:
                split = bool(self.store.get(f"splitfrom:{task['id']}"))
            if pr and split:
                continue  # Split off: the follows stay with the task it came from.
            if pr:
                if self.legacy_follower(session_id, key) == task["id"]:
                    found.append(key)
            elif current and current["id"] == task["id"]:
                found.append(key)  # A Slack thread: the task it was on.
        return found

    def legacy_follower(self, session_id: str, key: str) -> str:
        """The one task a worker's follow of a PR ("github:owner/repo#n") that is tied to no
        task is for (followed before follows were tied to tasks): the task that made it, else
        the one its receipts say created or modified it, else the one that reviewed it, else
        the one it was given to follow (the worker's newest task when it was given it), else
        the only one whose title or instructions name it. "" when none is, or several only
        name it: never more than one task, so one PR is one row on Home. Worked out once per
        pass (the state of every task asks it of each task several times)."""
        if (key, session_id) in self.legacy:
            return self.legacy[(key, session_id)]
        found = self.find_legacy_follower(session_id, key)
        with suppress(RuntimeError):  # Outside the event loop: worked out each time.
            loop = asyncio.get_running_loop()
            if not self.legacy:
                loop.call_soon(self.legacy.clear)
            self.legacy[(key, session_id)] = found
        return found

    def find_legacy_follower(self, session_id: str, key: str) -> str:
        from .tools import names_pr

        pr = threads.GITHUB_PR.fullmatch(key)
        if not pr:
            return ""
        repo, number = pr.group(1), int(pr.group(2))
        label = f"{repo}#{number}".lower()
        split = self.store.items("splitfrom:")
        mine = [
            task
            for task in self.tasks.for_session(session_id)
            if task["status"] not in {"cancelled", "failed"} and task["id"] not in split
        ]
        if not mine:
            return ""
        maker = self.creator(label)
        if maker in {task["id"] for task in mine}:
            return maker
        # Each task's latest receipts (as previous_prs), in one query: only those that name the
        # PR are read.
        latest: dict[str, str] = {}
        for row in self.store.all(
            "SELECT task_id, CASE WHEN evidence LIKE ? THEN evidence ELSE '' END AS named "
            "FROM runs WHERE session_id=? AND evidence!='' ORDER BY started_at DESC",
            (f"%{label}%", session_id),
        ):
            latest.setdefault(row["task_id"], row["named"])
        roles = {
            task["id"]: pr_role(found)
            for task in mine
            if latest.get(task["id"])
            for found in json.loads(latest[task["id"]]).get("prs") or []
            if found["label"].lower() == label
        }
        reviewed = {
            review["task_id"] for review in self.reviews(session_id) if review["label"] == label
        }
        for held in (
            [task for task in mine if roles.get(task["id"]) in OWN_ROLES],
            [task for task in mine if task["id"] in reviewed],
        ):
            if held:
                # Created over modified; else the first that did.
                return min(held, key=lambda task: roles.get(task["id"]) != "created")["id"]
        if self.store.get(f"given:{session_id}:{key}"):
            since = self.store.value(
                "SELECT since FROM subscriptions WHERE session_id=? AND thread=?",
                (session_id, key),
            )
            # Followed a minute after `since` (see Store.follow): the task it then had last.
            given = [task for task in mine if since and stamp(task["created_at"]) <= since + 60]
            if given:
                return given[-1]["id"]
        named = [
            task
            for task in mine
            if names_pr(f"{task['title']}\n{task['goal'] or ''}", repo, number)
        ]
        return named[0]["id"] if len(named) == 1 else ""

    def bind_legacy_follows(self):
        """Once: each follow of a PR that is tied to no task (from before follows were) is tied
        to the one task it is for (see legacy_follower), as a follow made since is; one that is
        no one task's to NO_TASK, so it is not worked out again on every look."""
        if self.store.get("migrated:legacy-follows"):
            return
        for row in self.store.all("SELECT thread, session_id FROM subscriptions"):
            key, session_id = row["thread"], row["session_id"]
            if not threads.GITHUB_PR.fullmatch(key) or self.store.follow_tasks(session_id, key):
                continue
            task_id = self.legacy_follower(session_id, key) or NO_TASK
            self.store.set(f"followtask:{session_id}:{key}", json.dumps([task_id]))
        self.store.set("migrated:legacy-follows", "1")

    def watch_standing(self, task: dict, follows: list[str]) -> dict:
        """What the PRs it follows are to it ("own": its delivery waiting on others;
        "reviewed": others' work it reviewed) and when anything it follows last changed: a
        PR's standing, news in a Slack conversation (or since it was followed there). All it
        follows for the task count, those that could not be read lately too, as they were
        last read: a read failing changes nothing of what a PR is to it."""
        keys = self.followed_for(task, follows)
        watched = {
            f"{found.group(1)}#{found.group(2)}".lower()
            for key in keys
            if (found := threads.GITHUB_PR.fullmatch(key))
        }
        prs = [pr for pr in self.previous_prs(task["id"]) if pr["label"].lower() in watched]
        reviewed = any(pr.get("reviewed") for pr in prs) or bool(
            watched and watched <= self.reviewed_prs(task["session_id"], task["id"])
        )
        kind = "own" if any(pr.get("own") for pr in prs) else "reviewed" if reviewed else ""
        changed = max((pr.get("changed_at") or pr.get("at") or "" for pr in prs), default="")
        if slack := [key for key in keys if key.startswith("slack:")]:
            # Only "?" placeholders go into the SQL text.
            touched = self.store.value(
                "SELECT max(touched) FROM subscriptions WHERE session_id=? AND thread IN "  # noqa: S608
                f"({','.join('?' * len(slack))})",
                (task["session_id"], *slack),
            )
            if touched:
                changed = max(changed, datetime.fromtimestamp(touched, UTC).isoformat())
        return {"watch_kind": kind, "watch_changed_at": changed}

    def watching(self, task: dict, follows: list[str]) -> list[str]:
        """The threads it waits on for this task that can be read: what may bring it news."""
        failing = self.unreadable(task, follows)
        return [key for key in self.followed_for(task, follows) if key not in failing]

    def unreadable(self, task: dict, follows: list[str]) -> list[str]:
        """Its threads for this task that could not be read several times in a row: not
        something it waits on."""
        row = self.store.find_session(task["session_id"])
        health = self.runtime.follow_health(row["id"]) if row else {}
        return [
            key for key in self.followed_for(task, follows) if health.get(key, 0) >= FAILING_READS
        ]

    def failing(self, task: dict, follows: list[str]) -> list[str]:
        """Those of them shown as unreadable on this task's card: on one task only, the one
        the PR is (the task that made it), else the newest that follows it; not on every task
        of its worker that follows it too."""
        found = self.unreadable(task, follows)
        if not found:
            return []
        others = [t for t in self.tasks.for_session(task["session_id"]) if t["id"] != task["id"]]
        shown = []
        for key in found:
            claims = [t for t in others if key in self.followed_for(t, [key])]
            pr = threads.GITHUB_PR.fullmatch(key)
            made = self.creator(f"{pr.group(1)}#{pr.group(2)}".lower()) if pr and claims else ""
            if made == task["id"] or (
                made not in {t["id"] for t in claims}
                and all(t["created_at"] <= task["created_at"] for t in claims)
            ):
                shown.append(key)
        return shown

    def failing_why(self, task: dict, keys: list[str]) -> dict[str, dict]:
        """Why each of its unreadable PRs could not be read (see Runtime.follow_why)."""
        return {key: why for key in keys if (why := self.runtime.follow_why(key))}

    def view(self, task: dict) -> dict:
        row = self.store.find_session(task["session_id"]) or {}
        follows = self.store.follows(row["id"]) if row else []
        shown_receipts = self.receipts(task)
        prs = (shown_receipts or {}).get("prs") or []
        for pr in prs:
            pr["role"] = pr_role(pr)
        given = self.given_links(task, row)
        attention, why = self.attention(task, prs)
        return {
            **task,
            # Its state, apart: where its run is (queued, running, waiting, finished,
            # cancelled, failed), what the owner has to do about it now (attention, with why;
            # "" for nothing), and what Chief of Staff still follows up for it (monitoring).
            "execution": self.execution(task),
            "attention": attention,
            "attention_why": why,
            "monitoring": self.monitoring(task, prs, follows),
            # What it delivered (its PRs, documents and runs): Artifacts and its card's chips.
            "deliverables": self.deliverables(task, prs, given),
            # Its question to the owner while it is open: why it asks, the choices, what next.
            "question": self.open_question(task),
            # As its card shows it: what it said of its PR before it was opened or merged
            # left out once that is past (the stored result stays as reported).
            "result": self.shown_result(task),
            "worker": self.runtime.display_title(row) if row else "",
            "worker_status": row.get("status", ""),
            "worker_closed": bool(row.get("closed_at")),
            "cwd": row.get("cwd", ""),
            "branch": row.get("branch", ""),
            "after": [dep["id"] for dep in self.tasks.deps(task["id"])],
            "runs": len(self.tasks.runs(task["id"])),
            "credits": self.tasks.credits(task["id"]),
            "blocked": task["status"] == "queued" and bool(self.tasks.broken(task)),
            "receipts": shown_receipts,
            # What it reported before its latest result (see keep_earlier_result).
            "earlier_results": json.loads(self.store.get(f"results:{task['id']}") or "[]"),
            "decisions": self.runtime.decisions.for_task(task["id"]),
            "news": json.loads(self.store.get(f"news:{task['id']}") or "null"),
            # What tells a running task's state apart: woken later to check something, the
            # threads it waits on, and when its worker last did anything.
            "check_back": self.check_back(task),
            "watching": self.watching(task, follows),
            **self.watch_standing(task, follows),
            # Followed but not read lately: still followed (Home places it by how its PR
            # stood when last read); its warning shows on one task (watch_failing).
            "watch_unreadable": self.unreadable(task, follows),
            "watch_failing": (failing := self.failing(task, follows)),
            "watch_failing_why": self.failing_why(task, failing),
            "earlier_titles": json.loads(self.store.get(f"titles:{task['id']}") or "[]"),
            # When its worker was last asked where this task stands (stalled).
            "nudged_at": self.store.get(f"stallnudge:{task['id']}") or "",
            "active_at": self.active_at(task, row) if row else task["updated_at"],
            # Its worker has taken it up (a task given while it is on another is not, until
            # that one's turn ends): one not taken up waits its turn; one taken up and left
            # is paused, not up next.
            "started": bool(task["progress"] or task["result"])
            or bool(self.store.get(f"started:{task['id']}")),
            # The owner asked about it after it finished: its worker at it is following up.
            "follow_up": bool(self.store.get(f"followup:{task['id']}")),
            # Cancelled after it reported, before the owner accepted it: done, not accepted.
            "set_aside": task["status"] == "done"
            and bool(self.store.get(f"setaside:{task['id']}")),
            # Waiting on a question about its own draft: that draft's card is where it is
            # decided (one card, not a question beside it).
            "question_draft": (
                (self.runtime.drafts.question_draft(task) or {}).get("id", "")
                if task["status"] == "waiting" and not self.store.get(f"stopped:{task['id']}")
                else ""
            ),
            # Stopped by the owner (or the Watchdog) mid-work: it waits to be told to go on.
            "stopped": self.store.get(f"stopped:{task['id']}") or "",
            # What it made or runs, as its worker last said: a short name and status each.
            "artifacts": json.loads(self.store.get(f"artifacts:{task['id']}") or "[]"),
            # Every link its reports gave, and those it was given (not its own work).
            "links": json.loads(self.store.get(f"links:{task['id']}") or "[]"),
            "given_links": given,
            # What its reports call those links ([the plan](…)), and the ones they say it made.
            "link_names": json.loads(self.store.get(f"linknames:{task['id']}") or "{}"),
            "made_links": json.loads(self.store.get(f"madelinks:{task['id']}") or "[]"),
            # Files it wrote in its own folder; whether the owner read its result; whether the
            # Router said the owner wants to see it (then it waits for their OK).
            "files": self.made_files(task, row),
            "read": bool(self.store.get(f"read:{task['id']}")),
            "review_required": bool(self.store.get(f"reviewreq:{task['id']}")),
            # Its latest result looks like other work (offered to split off), and, split off
            # from another task, until when that can be undone.
            "splittable": task["status"] in {"review", "done"} and self.splittable(task),
            "split_from": self.split_from(task["id"]),
        }

    def execution(self, task: dict) -> str:
        """Where its run is: "queued", "running", "waiting", "finished" (reported: to accept,
        or accepted), "cancelled" (a result set aside too: not accepted) or "failed"."""
        if task["status"] in {"review", "done"}:
            return "cancelled" if self.store.get(f"setaside:{task['id']}") else "finished"
        return task["status"]

    def attention(self, task: dict, prs: list[dict] | None = None) -> tuple[str, str]:
        """What the owner has to do about a task now, and why in their words: "blocked",
        "stopped", "question", "failed", "check-failed", "acceptance" (a result to accept),
        "update" (news on finished work), "pr-changes" / "pr-conflicts" / "pr-failing" (its own
        PR to fix, accepted or not), "merge" (its own PR ready to merge, on its Merge card); ""
        for nothing. As Home's Needs you (board.js taskPhase) and the owner's count
        (digest.needs) have it; not "No progress", which Home works out as time passes.
        `prs`: its receipts' PRs, when read already."""
        status, task_id = task["status"], task["id"]
        if status == "queued":
            broken = self.tasks.broken(task)
            return (
                ("blocked", "It waits for a task that ended without finishing")
                if broken
                else ("", "")
            )
        if status == "waiting":
            if who := self.store.get(f"stopped:{task_id}"):
                return "stopped", f"Stopped by {who}: tell it to go on, or cancel it"
            asked = self.open_question(task) or {}
            return "question", clip(" ".join((asked.get("text") or task["progress"]).split()), 300)
        if status == "failed":
            return "failed", clip(" ".join((task["progress"] or "").split()), 300)
        if status == "running":
            check = self.check_back(task) or {}
            if check.get("attempts", 0) >= 3:
                error = check.get("error") or "unknown error"
                return "check-failed", f"Its check-back could not wake its worker: {error}"
            return "", ""
        if status not in {"review", "done"} or self.store.get(f"setaside:{task_id}"):
            return "", ""
        row = self.store.find_session(task["session_id"])
        if (
            row
            and row["status"] in {"working", "starting"}
            and self.store.get(f"followup:{task_id}")
            and self.runtime.working_on(row["id"]) == task_id
        ):
            return "", ""  # Its worker follows up on the owner's message now: Running.
        marker, why = self.own_pr_standing(task, prs)
        news = json.loads(self.store.get(f"news:{task_id}") or "null")
        if status == "review":
            # A result to accept, always said so: its own PR to fix, or news on it since, is
            # why it needs a look (accepted or sent back, as the owner decides).
            if marker in PR_TO_FIX:
                return "acceptance", why
            if news:
                said = " ".join((news.get("text") or "").split())
                return "acceptance", clip(f"News: {said}", 300)
            # Nothing more to say: Home's status says it waits for Accept.
            return "acceptance", ""
        if news:
            return "update", clip(" ".join((news.get("text") or "").split()), 300)
        if marker in PR_TO_FIX:
            return marker, why
        return ("merge", why) if marker == "mergeable" else ("", "")

    def monitoring(
        self, task: dict, prs: list[dict] | None = None, follows: list[str] | None = None
    ) -> list[dict]:
        """What Chief of Staff still follows up for a task, each an obligation of its own: its
        own open PRs it follows (and one it was asked to follow), the Slack threads it follows
        for it, its check-back, and the runs it said are under way. Not a PR of someone else's
        it reviewed once its review is accepted (still followed, news there is an update),
        nor a PR merged, closed or only mentioned. Each with what it waits for, when it was
        last read and is read next (as the GitHub source keeps them), what brings it back,
        and when it last changed. Nothing ends it for being quiet: Home says how long it is
        ("No updates for 26h")."""
        if task["status"] in {"cancelled", "failed"} or self.store.get(f"setaside:{task['id']}"):
            return []
        row = self.store.find_session(task["session_id"])
        if not row:
            return []
        follows = self.store.follows(row["id"]) if follows is None else follows
        known = {pr["label"].lower(): pr for pr in self.previous_prs(task["id"])}
        known |= {pr["label"].lower(): pr for pr in prs or []}
        reviewed = self.reviewed_prs(row["id"], task["id"])
        found: list[dict] = []
        names: dict[str, str] | None = None
        for key in self.followed_for(task, follows):
            if found_pr := threads.GITHUB_PR.fullmatch(key):
                repo, number = found_pr.group(1), found_pr.group(2)
                label = f"{repo}#{number}"
                pr = known.get(label) or {}
                role = (
                    pr_role(pr)
                    if pr
                    else "created"
                    if self.creator(label) == task["id"]
                    else "reviewed"
                    if label in reviewed
                    else "referenced"
                )
                if pr.get("state") in {"MERGED", "CLOSED"}:
                    continue
                if role == "reviewed" and task["status"] == "done":
                    continue  # Its review was delivered and accepted: not the owner's to watch.
                if role == "referenced" and not self.store.get(f"given:{row['id']}:{key}"):
                    continue  # Only mentioned, not given to follow.
                title = pr.get("title") or self.store.get(f"prtitle:{label}") or ""
                short = f"{repo.split('/', 1)[-1]}#{number}"
                found.append(
                    {
                        "key": key,
                        "kind": "pr",
                        "label": f"{short}: {title}" if title else short,
                        "url": pr.get("url") or f"https://github.com/{repo}/pull/{number}",
                        "waiting_for": PR_WAITS_FOR.get(pr_status(pr), "review")
                        if pr
                        else "review",
                        "last_check": self.store.get(f"ghread:{key}") or None,
                        "next_check": self.store.get(f"ghnext:{key}") or None,
                        "trigger": "A review, CI result or merge on it brings it back",
                        "changed_at": pr.get("changed_at") or pr.get("at") or None,
                    }
                )
            elif key.startswith("slack:"):
                if names is None:
                    names = self.runtime.slack.display_names()
                touched = self.store.value(
                    "SELECT touched FROM subscriptions WHERE session_id=? AND thread=?",
                    (row["id"], key),
                )
                found.append(
                    {
                        "key": key,
                        "kind": "thread",
                        "label": threads.label(key, names),
                        "url": self.store.get(f"threadlink:{key}") or "",
                        "waiting_for": "a reply",
                        "last_check": None,
                        "next_check": None,
                        "trigger": "A reply there brings it back",
                        "changed_at": datetime.fromtimestamp(touched, UTC).isoformat()
                        if touched
                        else None,
                    }
                )
        check = self.check_back(task)
        if check:
            found.append(
                {
                    "key": f"checkback:{task['id']}",
                    "kind": "check_back",
                    "label": check.get("what") or "Check back",
                    "url": check.get("url") or "",
                    "waiting_for": check.get("what") or "",
                    "last_check": self.store.get(f"checkfired:{task['id']}") or None,
                    "next_check": check.get("at") or None,
                    "trigger": "Chief of Staff wakes its worker then",
                    "changed_at": None,
                }
            )
        if not row["closed_at"]:
            for item in json.loads(self.store.get(f"artifacts:{task['id']}") or "[]"):
                kind, called = deliverable_kind(item["url"], declared=True)
                if kind != "flow" or not RUN_GOING.search(item.get("status") or ""):
                    continue
                found.append(
                    {
                        "key": item["url"],
                        "kind": "flow",
                        "label": item.get("name") or called,
                        "url": item["url"],
                        "waiting_for": "the run",
                        "last_check": item.get("at") or None,
                        "next_check": check.get("at") if check else None,
                        "trigger": "Its worker's check-back on it"
                        if check
                        else "Its worker's next report on it",
                        "changed_at": item.get("at") or None,
                    }
                )
        return found

    def monitoring_words(self, task: dict) -> str:
        """What it is monitored for, in a few words for the Router ("" for nothing)."""
        return "; ".join(
            f"{item['label']} (waiting for {item['waiting_for']})" for item in self.monitoring(task)
        )[:300]

    def deliverables(self, task: dict, prs: list[dict], given: list[str]) -> list[dict]:
        """What the task delivered, the one list Home's Artifacts and its card's chips show:
        the PRs it created, modified or reviewed (as GitHub has them), the documents and the
        runs it declared or its reports say it made, each by its name (the one its worker or
        its report gave it), else by what it is. Not a PR it only mentions, a link it was
        given or only mentions, a file on this computer (a mockup, a screenshot, a CSV, a
        log), a site's home page, a run's logs, a Slack message: those stay in its report,
        files and links."""
        found: list[dict] = []
        labels = set()
        for pr in prs:
            role = pr_role(pr)
            if role == "referenced":
                continue
            short = pr["label"].split("/", 1)[-1]
            labels.add(pr["label"].lower())
            found.append(
                {
                    "kind": "pr",
                    "url": pr.get("url") or "",
                    "title": f"{short} · {pr['title']}" if pr.get("title") else short,
                    "role": role,
                    "status": pr_status(pr),
                }
            )
        for label in sorted(self.opened_prs(task["id"]) - labels):
            # Opened by it, not read from GitHub yet.
            repo, number = label.rsplit("#", 1)
            short = f"{repo.split('/', 1)[-1]}#{number}"
            title = self.store.get(f"prtitle:{label}") or ""
            found.append(
                {
                    "kind": "pr",
                    "url": f"https://github.com/{repo}/pull/{number}",
                    "title": f"{short} · {title}" if title else short,
                    "role": "created",
                    "status": "",
                }
            )
        names = {
            threads.link_key(url): name
            for url, name in json.loads(self.store.get(f"linknames:{task['id']}") or "{}").items()
        }
        seen: set[str] = set()
        declared = json.loads(self.store.get(f"artifacts:{task['id']}") or "[]")
        for item in reversed(declared):  # The newest word on each first.
            kind, called = deliverable_kind(item["url"], declared=True)
            key = threads.link_key(item["url"])
            if not kind or key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "kind": kind,
                    "url": item["url"],
                    "title": item.get("name") or names.get(key) or called,
                    "role": "",
                    "status": item.get("status") or "",
                }
            )
        given_keys = {threads.link_key(url) for url in given}
        for url in json.loads(self.store.get(f"madelinks:{task['id']}") or "[]"):
            kind, called = deliverable_kind(url)
            key = threads.link_key(url)
            if not kind or key in seen or key in given_keys:
                continue
            seen.add(key)
            found.append(
                {
                    "kind": kind,
                    "url": url,
                    "title": names.get(key) or called,
                    "role": "",
                    "status": "",
                }
            )
        return found

    def open_question(self, task: dict) -> dict | None:
        """Its question to the owner that is still open (under Needs you): what it asks, why
        it needs them, the choices it gives (none: their own words), and what it does next."""
        for question in reversed(self.runtime.asks.session_questions(task["session_id"])):
            if question.get("task_id") == task["id"]:
                return {
                    "id": question["id"],
                    "text": question.get("text") or "",
                    "kind": question.get("kind") or "question",
                    "why": question.get("why") or "",
                    "options": question.get("options") or [],
                    "next": question.get("next") or "",
                    "at": question.get("at") or "",
                }
        return None

    def mark_read(self, task_id: str) -> dict:
        """The owner read its result (opened its report)."""
        task = self.tasks.get(task_id)
        if not self.store.get(f"read:{task_id}"):
            self.store.set(f"read:{task_id}", now())
            self.publish(task)
        return self.view(task)

    def declare_artifacts(self, task_id: str, items: list[dict]):
        """Its worker says what the task made or runs and where each stands: kept by link (or
        by name: declared again under another link, it is the same thing moved on), the latest
        word on each; one said again moves to the top."""
        known = json.loads(self.store.get(f"artifacts:{task_id}") or "[]")
        stamp = now()
        row = self.store.find_session(self.tasks.get(task_id)["session_id"])
        for item in items:
            url = str(item.get("url") or "").strip()
            path = self.local_file(row, url)
            if path:
                url = Path(path).as_uri()  # A file it made (a mockup): opened from its folder.
            elif not re.match(r"https?://\S+$", url) or threads.PR_URL.match(url):
                continue  # Not a link; a PR's status comes from GitHub.
            entry = {
                **({"path": path} if path else {}),
                "url": url[:2000],
                "name": clip(" ".join(str(item.get("name") or "").split()), 60),
                "status": clip(" ".join(str(item.get("status") or "").split()), 40),
                "at": stamp,
            }
            known = [
                other
                for other in known
                if other["url"] != url and not (entry["name"] and other["name"] == entry["name"])
            ] + [entry]
        self.store.set(f"artifacts:{task_id}", json.dumps(known[-20:]))
        self.publish(self.tasks.get(task_id))

    def local_file(self, row: dict | None, given: str) -> str:
        """A file in one of the worker's folders, by its path or file:// link; "" otherwise."""
        if not row or not given:
            return ""
        path = given.removeprefix("file://") if given.startswith("file://") else given
        if not Path(path).is_absolute():
            return ""
        real = os.path.realpath(path)
        # Its folders: its workspace (its scratch files too), its checkouts.
        folders = self.workspaces.writable(row) if row.get("id") else [row.get("cwd") or ""]
        # os.path.isfile answers False for any path it cannot look at (too long, say): this one
        # is whatever the worker wrote, and Path.is_file would raise.
        if not os.path.isfile(real) or not any(  # noqa: PTH113
            folder and within(real, os.path.realpath(folder)) for folder in folders
        ):
            return ""
        return real

    def made_files(self, task: dict, row: dict) -> list[dict]:
        """Files its edits wrote in its own folder (not in a checkout, whose changes are its
        receipts) that are still there: a mockup, a plan, a CSV it delivered."""
        if not row or not row.get("cwd"):
            return []
        checkouts = [os.path.realpath(c["path"]) for c in self.workspaces.checkouts(row)]
        folders = [
            os.path.realpath(folder)
            for folder in self.workspaces.writable(row)
            if folder and os.path.realpath(folder) not in checkouts
        ]
        found = []
        for path in json.loads(self.store.get(f"touched:{task['id']}") or "[]"):
            if not any(within(path, folder) for folder in folders) or any(
                within(path, c) for c in checkouts
            ):
                continue
            with suppress(OSError):
                size = Path(path).stat().st_size
                found.append({"path": path, "name": Path(path).name, "size": size})
        return found[-20:]

    def reported_links(self, task_id: str, text: str):
        """Every link its reports gave, kept: a later report that does not repeat a doc it made
        does not take it off the task (nor off the owner's Artifacts). With when each was first
        reported: pasted back to it later, it is still its own, not one it was given."""
        if not (found := threads.links(text)):
            return
        if names := threads.named_links(text):
            kept = json.loads(self.store.get(f"linknames:{task_id}") or "{}")
            self.store.set(
                f"linknames:{task_id}", json.dumps(dict(list({**kept, **names}.items())[-60:]))
            )
        if made := threads.made_links(text):
            kept = json.loads(self.store.get(f"madelinks:{task_id}") or "[]")
            self.store.set(
                f"madelinks:{task_id}", json.dumps(list(dict.fromkeys([*kept, *made]))[-60:])
            )
        known = json.loads(self.store.get(f"links:{task_id}") or "[]")
        merged = list(dict.fromkeys([*known, *found]))[-60:]
        if merged != known:
            self.store.set(f"links:{task_id}", json.dumps(merged))
        first = json.loads(self.store.get(f"linksat:{task_id}") or "{}")
        stamp = now()
        new = {key: stamp for key in map(threads.link_key, found) if key not in first}
        if new:
            self.store.set(
                f"linksat:{task_id}", json.dumps(dict(list({**first, **new}.items())[-200:]))
            )

    def given_links(self, task: dict, row: dict) -> list[str]:
        """Links the task was given (its instructions, the owner's and the Router's messages
        about it): what it works from, not what it made. Not its own work, pasted back to it
        ("add a summary to <its doc>")."""
        given = threads.links(task["goal"] or "")
        if row:
            first = json.loads(self.store.get(f"linksat:{task['id']}") or "{}")
            for item in self.store.all(
                "SELECT text, created_at FROM messages WHERE session_id=? AND kind IN "
                "('router', 'owner') AND json_extract(meta, '$.task_id')=? ORDER BY id",
                (row["id"], task["id"]),
            ):
                for url in threads.links(item["text"]):
                    reported = first.get(threads.link_key(url))
                    if not (reported and reported <= item["created_at"]):
                        given.append(url)
        return list(dict.fromkeys(given))[:60]

    def active_at(self, task: dict, row: dict) -> str:
        """When anything last happened on this task: its own progress or messages, and its
        worker's steps while its turn was on it."""
        about = self.store.value(
            "SELECT max(created_at) FROM messages WHERE session_id=? "
            "AND json_extract(meta, '$.task_id')=?",
            (row["id"], task["id"]),
        )
        stamps = [task["updated_at"], about or ""]
        if self.runtime.working_on(row["id"]) == task["id"]:
            last = "SELECT max(created_at) FROM messages WHERE session_id=?"
            stamps.append(self.store.value(last, (row["id"],)) or "")
        return max(stamp for stamp in stamps if stamp is not None)

    def stalled(self, task: dict, now: datetime | None = None) -> float:
        """Hours a running task has been stalled (0 when not): its worker is not at work, it
        waits on nothing it follows and has no check-back, and nothing happened on it for
        stall_hours."""
        if task["status"] != "running":
            return 0
        row = self.store.find_session(task["session_id"])
        if not row or row["closed_at"] or row["status"] in {"working", "starting", "queued"}:
            return 0
        if self.check_back(task) or self.watching(task, self.store.follows(row["id"])):
            return 0
        now = now or datetime.now(UTC)
        idle = (now - datetime.fromisoformat(self.active_at(task, row))).total_seconds() / 3600
        return idle if idle >= self.store.settings().stall_hours else 0

    async def nudge(self, task_id: str, by: str = "you") -> dict:
        """Ask a stalled task's worker where it stands: it goes on, reports, or says what it
        waits on (check_back) or what blocks it."""
        task = self.tasks.get(task_id)
        if task["status"] not in {"running", "waiting"}:
            raise ValueError(f"Task {task_id} is {task['status']}: nothing to nudge.")
        row = self.store.session(task["session_id"])
        if row["closed_at"]:
            await self.runtime.workers.reopen(row["id"])
        asker = "The owner asks" if by == "you" else "Chief of Staff asks"
        text = (
            f"{asker} where task {task_id} “{task['title']}” stands: nothing has happened on it "
            f"for a while. Go on with it now; or report it (done, or a question); or, if it waits "
            "for something outside (a run, a review), check_back when to look again."
        )
        await self.delivery.deliver(
            row["id"],
            wrap("system", "nudge", text, task=task_id),
            kind="notice",
            display="Asked where it stands." if by == "you" else "Stalled: asked where it stands.",
            meta={"task_id": task_id},
            steer=True,
        )
        self.store.set(f"stallnudge:{task_id}", now())  # After it: the nudge is not activity.
        return self.view(self.tasks.get(task_id))

    async def nudge_stalled(self, moment: datetime | None = None):
        """Each stalled task's worker is asked once where it stands (again only after more
        activity and another stall)."""
        for task in self.store.all("SELECT * FROM tasks WHERE status='running'"):
            if not self.stalled(task, moment):
                continue
            nudged = self.store.get(f"stallnudge:{task['id']}") or ""
            row = self.store.find_session(task["session_id"])
            if not row or (nudged and nudged >= self.active_at(task, row)):
                continue
            try:
                await self.nudge(task["id"], by="watchdog")
            except Exception:
                logger.warning("Could not nudge stalled task %s.", task["id"], exc_info=True)
            self.publish(self.tasks.get(task["id"]))

    def publish(self, task: dict):
        self.bus.publish("task", self.view(task))
        if task["status"] in {"failed", "cancelled"} and not self.store.get(
            f"peerstop:{task['id']}"
        ):
            # Once: the workers it works with stop waiting for it.
            self.store.set(f"peerstop:{task['id']}", "1")
            self.runtime.background(self.runtime.peers.stopped(task))
        # Whether a task is blocked depends on the tasks it waits for.
        for dependent in self.tasks.dependents(task["id"]):
            if dependent["status"] in OPEN:
                self.bus.publish("task", self.view(dependent))
