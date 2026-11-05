"""The tools sessions call: what each role may do in Chief of Staff, and the argument
models the model sees.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from copilot import ToolResult, define_tool
from pydantic import BaseModel, Field

from . import prompts, threads
from .asks import AnswerSession, AskSession, RecordSetup
from .checks import ProposeChecks
from .decisions import AckDecision, AskDecision, RecordDecision
from .envelope import clean_title, language, short, wrap
from .habits import UpdateStyle
from .learning import ProposeLessons
from .models import MAX_DRAFT, DraftDecision, NestedArguments
from .policy import within
from .roles import ROUTER, SLACK, WATCHDOG, WORKER
from .sources.github import NO_ACCESS
from .store import ago_minutes, now
from .workspaces import resolve_project

if TYPE_CHECKING:
    from .runtime import Runtime


# In a task the Router hands on, one of the owner's messages as written (by its ref attribute):
# faster than having the Router copy it (every word it writes delays the worker), and nothing
# is lost in a retelling.
OWNER_WORDS = "{owner:<ref>}"
OWNER_REF = re.compile(r"\{owner:\s*([A-Za-z0-9]+)\}")  # Not {owner} (GitHub's path template).


# Thinking efforts, least first.
EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"]


class SpawnWorker(BaseModel):
    title: str = Field(description="Short task title shown to the owner (under 60 characters)")
    instructions: str = Field(
        description="The task in the owner's words, plus the context the worker needs. "
        f"{OWNER_WORDS} stands for the owner's message with that ref attribute, exactly as "
        "written: use it instead of copying the message (add context before or after it)"
    )
    project: str = Field(
        default="",
        description="Absolute project directory from list_sessions().projects; empty when the "
        "task has no codebase",
    )
    isolate: bool = Field(
        default=False,
        description="Create a dedicated git worktree and branch: for any change to files, and "
        "whenever the owner asks for a worktree or a branch",
    )
    more_projects: list[str] = Field(
        default_factory=list,
        description="Other configured project folders this task also changes; each gets its own "
        "checkout in the worker's workspace (worktrees on the same branch when isolate=true)",
    )
    slack: bool = Field(
        default=False, description="Give the worker Slack read tools and draft_slack_reply"
    )
    after: list[str] = Field(
        default_factory=list,
        description="Task ids (t…) whose results this task needs; it starts once they are done "
        "and receives their results. A worker id stands for that worker's current task",
    )
    wait_for: Literal["result", "accepted"] = Field(
        default="result",
        description="With after: start once they report done (result, the default), or only "
        "once the owner accepted them (accepted: when this builds on work that must be right "
        "first, like an API others will code against)",
    )
    done_when: list[str] = Field(
        default_factory=list,
        description="Acceptance criteria: 1-4 observable checks that show the task is done, "
        "each one a worker can run (a test passes, a command's output, a request and its "
        "response). For a bug fix include a regression test that fails before the fix and "
        "passes after. Empty for research and questions",
    )
    context_from: list[str] = Field(
        default_factory=list,
        description="Task ids (or worker ids) of earlier work this new worker should build on: "
        "it gets their goals, results, folders and files, without their whole conversation",
    )
    decisions: list[str] = Field(
        default_factory=list,
        description="Ids (dec…) of the owner's decisions this task relies on, from "
        "list_sessions().decisions: if one changes, its worker is told",
    )
    model: str = Field(default="", description="Model id; empty for the configured default")
    effort: Literal["", "low", "medium", "high", "xhigh", "max"] = Field(
        default="",
        description="Thinking effort: higher for hard debugging, design or research, lower for "
        "simple chores; empty for the default. Never below the effort the owner set for workers",
    )
    follow: list[str] = Field(
        default_factory=list,
        description="Thread keys from the event (slack:<channel>:<ts>, slack:<D…>, "
        "github:<owner/repo>#<n>); later messages there go straight to this session",
    )
    why_new: str = Field(
        default="",
        description="Only after spawn_worker listed related workers: why none of them should "
        "take this (different subject, an independent change that needs its own PR, or work "
        "that must run in parallel with theirs)",
    )


class AskWorkspace(BaseModel):
    title: str = Field(description="The task title, as you would pass to spawn_worker")
    request: str = Field(description="One line: what the task is")
    options: list[str] = Field(
        default_factory=list,
        description="Candidate folders, best first: configured projects or a folder the owner "
        "named. The owner can also pick no project or type another folder.",
    )
    why: str = Field(description="One short line: why you are not sure")


class AddRepo(BaseModel):
    repo: str = Field(
        description="A project folder (you get a worktree of it), or a git URL or GitHub "
        "owner/name: a copy already on this machine is used, else it is cloned (into the owner's "
        "code folder when there is one, to stay for later tasks)"
    )


class MessageSession(BaseModel):
    session_id: str = Field(
        description="Target session id: from list_sessions, or the short id in a message's "
        "from attribute (worker:<id>)"
    )
    text: str = Field(
        description=f"The message for that session. {OWNER_WORDS} stands for the owner's message "
        "with that ref attribute, exactly as written"
    )
    task: Literal["continue", "new"] = Field(
        default="continue",
        description="For a worker: continue its current task (a follow-up or more information; "
        "a finished task reopens), or start a new task in the same session, reusing its context "
        "(give a title)",
    )
    title: str = Field(default="", description="Title of the new task when task='new'")
    done_when: list[str] = Field(
        default_factory=list,
        description="For task='new': its acceptance criteria, as for spawn_worker",
    )
    decisions: list[str] = Field(
        default_factory=list,
        description="For task='new': ids (dec…) of decisions it relies on, as for spawn_worker",
    )
    follow: list[str] = Field(
        default_factory=list,
        description="Thread keys from the event (slack:<channel>:<ts>, slack:<D…>, "
        "github:<owner/repo>#<n>); later messages there go straight to this session",
    )


class ListSessions(BaseModel):
    pass


class ReadSession(BaseModel):
    session_id: str = Field(
        description="A session from list_sessions: a worker, or one of terminal_sessions"
    )
    items: int = Field(default=12, ge=1, le=40, description="How many of its latest entries")


class FindSessions(BaseModel):
    query: str = Field(
        description="What the request is about: topic, repo, file, branch, PR number, person, "
        "or a session id"
    )


class NotifyOwner(BaseModel):
    text: str = Field(description="The update for the owner, at most three short lines")
    needs_action: bool = Field(
        default=False, description="True when the owner must decide or answer something"
    )
    urgent: bool = Field(
        default=False,
        description="Interrupts an owner who is focusing: only for what cannot wait (an "
        "outage, someone blocked on them today)",
    )


class CloseWorker(BaseModel):
    session_id: str = Field(description="Worker session id (full or the short id)")


class CriterionResult(BaseModel):
    criterion: str = Field(description="The criterion, as given under Done when")
    result: Literal["met", "not_met", "not_checked"]
    evidence: str = Field(
        default="",
        description="What you observed: the exact command you ran in backticks and what it "
        "showed, a URL, a file. Not a claim",
    )


class Report(NestedArguments):
    status: Literal["progress", "question", "blocked", "done"]
    summary: str = Field(description="What happened, or the one concrete question")
    task_id: str = Field(
        default="",
        description="The task this report is about (the task attribute of its message); "
        "empty for your current task",
    )
    criteria: list[CriterionResult] = Field(
        default_factory=list,
        description="With status done, for a task with Done when criteria: each one's result "
        "and its evidence",
    )


def task_text(instructions: str, criteria: list[str], decisions: Sequence[dict] = ()) -> str:
    """A task as its worker reads it: the instructions, the owner's decisions it relies on, and
    what shows it is done."""
    text = instructions
    if decisions:
        listed = "\n".join(f"- {d['id']}: {d['text']}" for d in decisions)
        text += f"\n\nThe owner's decisions this relies on:\n{listed}"
    if criteria:
        listed = "\n".join(f"- {criterion}" for criterion in criteria)
        text += (
            "\n\nDone when (check each yourself, then give each one's result and evidence in "
            f"report(criteria=…)):\n{listed}"
        )
    return text


def clean_criteria(criteria: list[str]) -> list[str]:
    return [" ".join(text.split())[:300] for text in criteria if text.strip()][:8]


class CancelTask(BaseModel):
    task_id: str = Field(description="Task id (t…), or a worker id for its current task")
    reason: str = Field(default="", description="Why, in a few words (the owner sees it)")


class DraftSlackReply(BaseModel):
    channel_id: str = Field(description="Slack conversation id, e.g. C0123ABC or D0123ABC")
    text: str = Field(description="The exact reply in the owner's voice")
    thread_ts: str = Field(
        default="",
        description="ts of the thread parent (or of the message) to reply in its thread; "
        "empty posts to the conversation itself",
    )
    to: str = Field(
        default="", description="Where it goes, for the owner, e.g. 'Alice Chen (DM)' or '#deploys'"
    )
    note: str = Field(default="", description="One line for the owner: what this answers")


class DraftReply(BaseModel):
    text: str = Field(description="The exact reply in the owner's voice")
    to: str = Field(description="Who it answers, e.g. 'Alice Chen'")
    where: str = Field(
        description="Where the owner sends it, e.g. 'Slack DM', '#deploys thread', 'email'"
    )
    link: str = Field(default="", description="A link to the conversation it answers")
    note: str = Field(default="", description="One line for the owner: what this answers")


class DistillProfile(BaseModel):
    focus: str = Field(default="", description="Optional: what to focus on or update")


class ProposeProfile(BaseModel):
    markdown: str = Field(description="The proposed owner profile (markdown)")


class FollowThread(BaseModel):
    thread: str = Field(
        description="slack:<channel_id>:<thread_ts>, slack:<D…> (a DM) or github:<owner/repo>#<n>"
    )


class AssessWorker(BaseModel):
    session_id: str = Field(description="The reviewed worker: the review's worker attribute")
    verdict: Literal["on_track", "stuck", "looping", "off_track"]
    action: Literal["none", "nudge", "interrupt", "escalate"] = Field(
        description="nudge: message the worker; interrupt: stop its current step, then message "
        "it; escalate: hand the problem to the Router"
    )
    reason: str = Field(description="One or two sentences of evidence")
    message: str = Field(
        default="", description="For nudge/interrupt: what the worker should do now"
    )


class InlineComment(BaseModel):
    path: str = Field(description="The file's path in the PR")
    line: int = Field(
        description="The line commented on, as numbered in the PR's diff: in the new file for "
        "side RIGHT, in the old file for LEFT. It must be in the diff"
    )
    start_line: int = Field(default=0, description="For a range of lines: its first. 0: one line")
    side: Literal["RIGHT", "LEFT"] = "RIGHT"
    body: str = Field(description="The comment (markdown)")


class DraftGitHubReview(BaseModel):
    repo: str = Field(description="owner/name")
    number: int = Field(description="Pull request number")
    body: str = Field(description="The review, or your reply to a review comment (markdown)")
    comments: list[InlineComment] = Field(
        default_factory=list,
        max_length=50,
        description="Comments on specific lines of the diff, published with the review (not "
        "with in_reply_to). Put each finding about a line here rather than in the body.",
    )
    in_reply_to: int = Field(
        default=0,
        description="To reply to a review comment on the PR: that comment's id (from "
        "`gh api repos/<owner/name>/pulls/<number>/comments`). 0 for a review of the whole PR.",
    )
    verdict: Literal["comment", "approve", "request_changes"] = Field(
        default="comment",
        description="The review's verdict, published as the owner's: approve when the owner "
        "asked you to approve it, or nothing you found blocks merging and they asked for your "
        "recommendation; request_changes for problems that must be fixed before merging; "
        "comment otherwise. The owner sees it on the card and can change it. Not for replies.",
    )


# What a request asks for that a one-line report cannot be.
# A table to read, not a database's ("add the orders table").
ASKS_TABLE = re.compile(
    r"\bmarkdown tables?\b|\b(?:in|as|into) an? (?:\w+ )?table\b|\bsummary table\b|\btabular\b|"
    r"\btable (?:of|with|summari[sz]ing|comparing)\b|表格",
    re.IGNORECASE,
)
ASKS_EXPLANATION = re.compile(
    r"\bexplain|step[- ]by[- ]step|walk me through|\bhow you|\bin detail|讲清楚|解释|一步一步|"
    r"详细|怎么算",
    re.IGNORECASE,
)


# A report that leaves part of the answer out for its length ("too large to paste here").
CUT_FOR_LENGTH = re.compile(
    r"\btoo (?:large|long|big) to (?:paste|include|show|list|put|fit|reproduce)|"
    r"\b(?:omitted|truncated|abridged|shortened) (?:here |)for (?:brevity|length|space)|"
    r"\bfor brevity\b|太长|篇幅",
    re.IGNORECASE,
)


def short_of(goal: str, summary: str) -> str:
    """What the task asked for that this done report lacks ("" when nothing is missing)."""
    if ASKS_TABLE.search(goal) and "|" not in summary:
        return "a table"
    if ASKS_EXPLANATION.search(goal) and len(summary) < 400:
        return "an explanation"
    if CUT_FOR_LENGTH.search(summary) and len(summary) < MAX_RESULT // 2:
        return (
            f"the whole answer: a report holds up to {MAX_RESULT:,} characters, so nothing has "
            "to be left out for its length"
        )
    return ""


def names_pr(text: str, repo: str, number: int) -> bool:
    """The text names the PR by URL or key, or by its repository's name and number (as in
    "review tscp-targeting PR #1529")."""
    text = text.lower()
    thread = f"github:{repo}#{number}"
    if thread in threads.pull_requests(text) or thread in {
        f"github:{name}#{n}" for name, n in threads.GITHUB_PR.findall(text)
    }:
        return True
    owner, short_name = repo.split("/", 1)
    # ASCII boundaries: \w and \b would take "的" in "tscp-targeting的PR" for part of a word.
    word = r"A-Za-z0-9_.\-"
    name = rf"(?<![{word}/])(?:([{word}]+)/)?{re.escape(short_name)}(?![{word}])"
    pr = r"(?<![A-Za-z0-9_])(?:pr|prs|pull requests?)(?![A-Za-z0-9_])"
    # The number as a PR: "#1529", "PR 1529", "pull/1529" (not JIRA-1529 or 15290).
    marked = rf"(?:#\s*|{pr}\s*#?\s*|/pull/|/){number}(?!\d)"
    # After the name: spaces, punctuation, Chinese ("tscp-targeting 的 PR #1529", "里面的 #1529"),
    # the repository's other PR numbers ("PRs #1528, #1529"). Not a number that belongs to the
    # repository named right after it ("api#1600 and #1529 in web").
    after_name = (
        rf"(?:\s|[,:()&\uff0c\uff1a\uff08\uff09\u3001]|[\u3000-\u9fff]|#\d+|{pr}|"
        r"(?<![A-Za-z])and(?![A-Za-z]))"
    )
    elsewhere = (
        r"(?!\s*(?:in|on|of|for|from)\s+(?!(?:[A-Za-z0-9_.\-]+/)?"
        + re.escape(short_name)
        + r"(?![A-Za-z0-9_.\-])))"
    )
    # Before the name, only a PR's own marks: "PR #1529 (tscp-targeting)", "PR 1529 in
    # tscp-targeting"; not "web#1529 and api", whose number is web's.
    number_first = rf"(?:(?<![{word}/])#\s*|{pr}\s*#?\s*){number}(?!\d)"
    before_name = r"(?:\s|[:(\uff1a\uff08]|(?<![A-Za-z])(?:in|on|of|for|from)(?![A-Za-z]))"
    for found in (
        *re.finditer(
            rf"{name}(?:{after_name}{{0,12}}?{marked}{elsewhere}|\s{{1,2}}{number}(?!\d))", text
        ),
        *re.finditer(rf"{number_first}{before_name}{{0,6}}?{name}", text),
    ):
        if found.group(1) in (None, owner):
            return True
    return False


def misplaced(comments: list[InlineComment], lines: dict) -> list[str]:
    """Why GitHub would refuse each comment that is not on lines of the diff."""
    problems = []
    for comment in comments:
        where = f"{comment.path}:{comment.line}"
        if comment.path not in lines:
            problems.append(f"{where}: {comment.path} is not changed in this PR")
            continue
        if comment.start_line and comment.start_line >= comment.line:
            problems.append(f"{where}: start_line must be before line")
            continue
        sides = lines[comment.path]
        if sides is None:
            continue
        wanted = range(comment.start_line or comment.line, comment.line + 1)
        if any(number not in sides[comment.side] for number in wanted):
            problems.append(f"{where} ({comment.side}): not in the diff")
    return problems


REPORT_STATUS = {"question": "waiting", "blocked": "waiting", "done": "done"}
MAX_RESULT = 30000
# Copilot hands a tool output over 50 KB to the model as a file, which the Router cannot read.
LISTED_BYTES = 32000


def shortened(listed: dict) -> dict:
    """list_sessions small enough to be read whole, however many sessions there are."""

    def size() -> int:
        return len(json.dumps(listed, ensure_ascii=False).encode())

    if size() <= LISTED_BYTES:
        return listed
    listed["tasks"] = [
        task
        for task in listed["tasks"]
        if task["status"] in {"queued", "running", "waiting", "review"}
    ]
    listed["recently_closed"] = listed["recently_closed"][:3]
    for limit in (100, 0):
        if size() <= LISTED_BYTES:
            break
        for entry in listed["sessions"]:
            if limit:
                entry["last_report"] = entry.get("last_report", "")[:limit]
            else:
                entry.pop("last_report", None)
                entry.pop("now", None)
    if size() > LISTED_BYTES:
        listed["projects"] = [{"path": card["path"]} for card in listed["projects"]]
        listed["recently_closed"] = []
    if size() > LISTED_BYTES:
        # Each session already names its current task.
        listed["tasks"] = [task for task in listed["tasks"] if task["status"] != "review"]
    listed["note"] = "Shortened to fit: read_session and find_sessions give any session's details."
    return listed


def clip_report(text: str) -> str:
    """A task's result as the owner reads it: whole, unless it is very long."""
    if len(text) <= MAX_RESULT:
        return text
    cut = text.rfind("\n", 0, MAX_RESULT)
    return text[: cut if cut > MAX_RESULT // 2 else MAX_RESULT] + (
        "\n\n… (cut here; the whole report is in the worker's session)"
    )


SLACK_ID = re.compile(r"[CDGUW][A-Z0-9]{2,}")


SLACK_TS = re.compile(r"\d{10}\.\d{6}")


class Tools:
    """Builds the tool definitions and runs their handlers."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.bus = runtime.bus
        self.states = runtime.states
        self.tasks = runtime.tasks
        self.delivery = runtime.delivery
        self.workspaces = runtime.workspaces

    async def t_add_repo(self, params: AddRepo, caller: str):
        self.caller(caller, WORKER)
        async with self.states[caller].checkouts:
            return await self.runtime.add_repo(caller, params.repo.strip())

    async def t_ask_workspace(self, params: AskWorkspace, caller: str):
        self.caller(caller, ROUTER)
        projects = self.workspaces.projects()
        options = []
        for raw in params.options[:5]:
            path = Path(await asyncio.to_thread(os.path.expanduser, raw.strip()))
            if not path.is_absolute() or not await asyncio.to_thread(path.is_dir):
                raise ValueError(f"Not an existing folder: {raw}")
            path = await asyncio.to_thread(path.resolve)
            if path == Path.home() or path == Path(path.anchor):
                raise ValueError("Offer project folders, not the home or filesystem root.")
            configured = any(within(str(path), project) for project in projects)
            if str(path) not in {option["path"] for option in options}:
                options.append({"path": str(path), "configured": configured})
        title = clean_title(params.title)
        draft = self.store.create_draft(
            caller,
            "workspace",
            {"title": title, "options": options, "why": params.why[:300]},
            params.request[:500],
        )
        self.runtime.add_message(
            caller,
            "draft",
            f"Where should “{title}” work? {params.why[:300]}",
            {"draft_id": draft["id"], "label": "Folder question"},
        )
        self.bus.publish("draft", draft)
        self.runtime.alert(
            f"Where should “{title}” work?",
            session_id=caller,
            needs_action=True,
            title="Choose a folder",
        )
        return (
            f"Asked the owner ({draft['id']}). Their choice arrives as a [draft outcome] message; "
            "start the worker then."
        )

    def definitions(self) -> dict:
        definitions = [
            *self.runtime.connector.tools(),
            (
                "spawn_worker",
                "Start a new worker session for one task. Returns its session id.",
                SpawnWorker,
                self.t_spawn_worker,
            ),
            (
                "add_repo",
                "Add a repository to your workspace, as a worktree on your branch: of a project, "
                "or of a repository by URL or GitHub owner/name (cloned first when it is not on "
                "this machine).",
                AddRepo,
                self.t_add_repo,
            ),
            (
                "record_setup",
                "Record what you found out about a tool, integration or sign-in on this machine, "
                "so the Router and later workers know it.",
                RecordSetup,
                self.runtime.asks.t_record_setup,
            ),
            (
                "ask_decision",
                "Ask the owner to make a decision that is theirs (scope, a trade-off, a risky or "
                "costly change): with why it comes up, the options and what each means, and your "
                "recommendation. The answer arrives as a message; other workers can join it.",
                AskDecision,
                self.runtime.decisions.t_ask_decision,
            ),
            (
                "ack_decision",
                "Confirm that you now work to a changed decision of the owner's, and what you "
                "change because of it.",
                AckDecision,
                self.runtime.decisions.t_ack_decision,
            ),
            (
                "record_decision",
                "Record a decision the owner made that work depends on (a requirement, scope or "
                "design choice), linked to the tasks that rely on it. When the owner changes an "
                "earlier decision, pass replaces: every affected worker is told and confirms, "
                "and their checked results are marked out of date.",
                RecordDecision,
                self.runtime.decisions.t_record_decision,
            ),
            (
                "propose_lessons",
                "Propose the lessons learned from the owner's feedback, for the owner to review.",
                ProposeLessons,
                self.runtime.learning.t_propose_lessons,
            ),
            (
                "update_style",
                "Update one section of how the owner writes; it applies at once.",
                UpdateStyle,
                self.runtime.habits.t_update_style,
            ),
            (
                "propose_checks",
                "Propose checks learned from workers' mistakes; each is run on the failing and "
                "the fixed code before the owner sees it.",
                ProposeChecks,
                self.runtime.checks.t_propose_checks,
            ),
            (
                "ask_session",
                "Ask another worker (or the Router) a question; the answer comes back to you.",
                AskSession,
                self.runtime.asks.t_ask_session,
            ),
            (
                "answer_session",
                "Answer a [question …] another session asked you.",
                AnswerSession,
                self.runtime.asks.t_answer_session,
            ),
            (
                "ask_workspace",
                "Ask the owner which folder a new task should work in, when you are not "
                "confident: offer your best candidates; the owner can also pick no project or "
                "another folder, which then becomes a configured project.",
                AskWorkspace,
                self.t_ask_workspace,
            ),
            (
                "message_session",
                "Send a message to an existing worker or the Slack session.",
                MessageSession,
                self.t_message_session,
            ),
            (
                "read_session",
                "What a session is doing now, what it said last and its latest messages and "
                "steps, without interrupting it. Use it to answer the owner about progress.",
                ReadSession,
                self.t_read_session,
            ),
            (
                "list_sessions",
                "List configured projects, the open sessions with status and last report, and "
                "the most recently closed workers.",
                ListSessions,
                self.t_list_sessions,
            ),
            (
                "find_sessions",
                "Search every worker, open or closed, by what its task was about. Use it to find "
                "the worker that owns a follow-up before starting a new one. Each match lists "
                "the query words it matched; judge by its task and reports.",
                FindSessions,
                self.t_find_sessions,
            ),
            (
                "notify_owner",
                "Show the owner an update and send a desktop notification. Only for results, "
                "decisions and failures.",
                NotifyOwner,
                self.t_notify_owner,
            ),
            (
                "cancel_task",
                "Cancel a task: its worker stops working on it (the session stays open for other "
                "tasks). Tasks that depend on it are then blocked.",
                CancelTask,
                self.t_cancel_task,
            ),
            (
                "close_worker",
                "Close a worker whose task is finished or no longer needed.",
                CloseWorker,
                self.t_close_worker,
            ),
            (
                "report",
                "Report to the Router: progress (rare), question, blocked or done.",
                Report,
                self.t_report,
            ),
            (
                "draft_slack_reply",
                "Draft a Slack message in the owner's voice. The owner approves, edits or "
                "rejects it before it is sent.",
                DraftSlackReply,
                self.t_draft_slack_reply,
            ),
            (
                "draft_reply",
                "Draft a reply in the owner's voice that the owner sends themself (Slack when it "
                "is not connected, mail, a document comment): they approve, edit and copy it.",
                DraftReply,
                self.t_draft_reply,
            ),
            (
                "distill_profile",
                "Start a worker that distills what the owner's work says about them into a "
                "profile the owner reviews; later sessions use it.",
                DistillProfile,
                self.t_distill_profile,
            ),
            (
                "propose_profile",
                "Propose the distilled owner profile for the owner to review.",
                ProposeProfile,
                self.t_propose_profile,
            ),
            (
                "follow_thread",
                "Follow a Slack thread, DM or PR: its new messages come straight to you.",
                FollowThread,
                self.t_follow_thread,
            ),
            (
                "unfollow_thread",
                "Stop following a thread that is unrelated or finished.",
                FollowThread,
                self.t_unfollow_thread,
            ),
            (
                "assess_worker",
                "Record your verdict on a reviewed worker and what to do about it.",
                AssessWorker,
                self.t_assess_worker,
            ),
            (
                "draft_github_review",
                "Draft a PR review comment. The owner approves it before it is published.",
                DraftGitHubReview,
                self.t_draft_github_review,
            ),
        ]
        return {
            name: define_tool(
                name,
                description=description,
                params_type=params,
                handler=self.handler(handler),
                skip_permission=True,
            )
            for name, description, params, handler in definitions
        }

    def handler(self, handler):
        async def run(params, invocation):
            try:
                result = await handler(params, self.runtime.row_id(invocation.session_id))
            except (ValueError, LookupError, RuntimeError) as exc:
                return ToolResult(
                    text_result_for_llm=f"Error: {exc}", result_type="failure", error=str(exc)
                )
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

        return run

    def caller(self, session_id: str, *roles: str) -> dict:
        row = self.store.session(session_id)
        if row["role"] not in roles:
            raise ValueError(f"This tool is not available to the {row['role']} session.")
        return row

    async def start_special(
        self, special: str, title: str, instructions: str, slack: bool = False
    ) -> dict:
        """A worker Chief of Staff starts for itself (to learn from feedback, say), with the
        tools of its `special` kind; no project, no Slack. The same one each time (one entry,
        one history), on a fresh Copilot conversation: each run is given all it needs."""
        if previous := self.last_special(special):
            return await self.rerun_special(previous, special, instructions)
        return await self.t_spawn_worker(
            SpawnWorker(title=title, instructions=instructions),
            self.runtime.router_id(),
            special=special,
            untrusted=slack,
        )

    def last_special(self, special: str) -> dict | None:
        ids = [key.split(":", 1)[1] for key in self.store.keys(f"{special}:")]
        rows = [row for row in map(self.store.find_session, ids) if row and row["role"] == WORKER]
        return max(rows, key=lambda row: row["created_at"], default=None)

    async def rerun_special(self, row: dict, special: str, instructions: str) -> dict:
        session_id = row["id"]
        if row["status"] in {"queued", "starting", "working"} and not row["closed_at"]:
            raise ValueError(f"“{row['title']}” is at work already.")
        # Held as reopening does: a removal of its folder started when it closed waits.
        async with self.states[session_id].checkouts, self.states[session_id].lifecycle:
            await self.runtime.drop_handle(session_id)
            fresh = str(uuid4())
            self.store.set(f"alias:{fresh}", session_id)
            self.store.set(f"copilot:{session_id}", fresh)
            self.store.delete(f"created:{session_id}")
            for key in ("style_refused", "lessons_done", "learned_styles"):
                self.store.delete(f"{key}:{session_id}")
            workspace = self.store.get(f"workspace:{session_id}") or row["cwd"]
            await asyncio.to_thread(Path(workspace).mkdir, parents=True, exist_ok=True)
            self.states[session_id].close_after_turn = False
            self.runtime.update_session(
                session_id, closed_at=None, status="queued", activity="", task=instructions[:4000]
            )
        task = self.tasks.create(session_id, row["title"], instructions[:4000])
        prompt = wrap("router", "task", instructions, title=row["title"], task=task["id"])
        self.runtime.add_message(
            session_id, "router", instructions, {"prompt": prompt, "task_id": task["id"]}
        )
        await self.runtime.workers.pump()
        status = self.store.session(session_id)["status"]
        return {"task_id": task["id"], "session_id": session_id, "status": status}

    def owner_words(self, text: str) -> str:
        """The owner's messages in place of {owner:<ref>}."""

        def quoted(match: re.Match) -> str:
            found = self.store.one(
                "SELECT text FROM messages WHERE session_id=? AND kind='owner' "
                "AND json_extract(meta, '$.ref')=?",
                (self.runtime.router_id(), match.group(1)),
            )
            if not found:
                raise ValueError(
                    f"{match.group(0)} names no message of the owner: write {OWNER_WORDS} with "
                    "the ref attribute of the owner's message, or write the text out."
                )
            return found["text"]

        return OWNER_REF.sub(quoted, text)

    async def t_spawn_worker(
        self, params: SpawnWorker, caller: str, special: str = "", untrusted: bool = False
    ):
        """`untrusted`: a helper that reads untrusted content (the connector) runs as a worker
        that reads Slack does, whether or not Slack is connected."""
        self.caller(caller, ROUTER)
        params = params.model_copy(update={"instructions": self.owner_words(params.instructions)})
        title = clean_title(params.title)
        follow = [threads.normalize(key) for key in params.follow]
        after = [self.runtime.task_actions.task_for(reference) for reference in params.after]
        context_from = [
            self.runtime.task_actions.task_for(reference)["id"] for reference in params.context_from
        ]
        decisions = self.with_turn_decisions(caller, params.decisions)
        await self.runtime.check_model(params.model)
        project = (
            str(resolve_project(params.project, self.workspaces.projects()))
            if (params.project)
            else ""
        )
        why_new = " ".join(params.why_new.split())[:200]
        if not special and not why_new:
            related = self.related_workers(
                f"{params.title}\n{params.instructions}",
                project,
                exclude=[task["session_id"] for task in after],
            )
            if related:
                # Routing to the worker that already knows the subject matters: decide explicitly.
                raise ValueError(
                    "Related workers exist. If this is about the same subject as one of them, "
                    "send it there with message_session instead (a closed one reopens with its "
                    "context and files; one without a branch takes its own worktree with "
                    "add_repo to change code, so needing a worktree is no reason for a new "
                    "worker). A code change on another subject (another feature, function or "
                    "bug, even in the same project) is not theirs: on their branch it would ship "
                    "in the same PR. Then call spawn_worker again with why_new.\n"
                    + json.dumps(related, ensure_ascii=False)
                )
        session_id = str(uuid4())
        slack = (params.slack and self.runtime.slack.configured()) or untrusted
        self.runtime.check_follow({"role": WORKER, "slack": slack}, follow)
        workspace, checkouts = await self.workspaces.prepare_directory(
            session_id,
            title,
            [params.project, *params.more_projects],
            params.isolate,
            carry=not slack,  # Gitignored files are often secrets; Slack content is untrusted.
        )
        # It starts in its main checkout; the workspace around it is all its own.
        cwd = checkouts[0]["path"] if checkouts else str(workspace)
        branch = checkouts[0]["branch"] if checkouts else ""
        self.store.create_session(
            session_id, WORKER, title, "queued", cwd=cwd, branch=branch, slack=slack
        )
        self.store.set(f"workspace:{session_id}", str(workspace))
        self.store.update_session(
            session_id,
            task=params.instructions[:4000],
            project=project,
            repos=json.dumps(checkouts),
        )
        effort = params.effort
        info = self.runtime.model_info(
            self.runtime.effective_model({"model": params.model, "role": WORKER})
        )
        floor = self.store.settings().role_efforts.get(WORKER, "")
        lowered = bool(effort and floor and EFFORTS.index(effort) < EFFORTS.index(floor))
        if lowered:
            effort = ""  # The owner's choice for workers is the least they get.
        if effort and info and effort not in info["efforts"]:
            effort = ""  # Reported below; the model's default effort applies.
        if params.model or effort:
            self.store.update_session(session_id, model=params.model, effort=effort)
        criteria = clean_criteria(params.done_when)
        task = self.tasks.create(
            session_id,
            title,
            params.instructions[:4000],
            why_new=why_new,
            after=[dependency["id"] for dependency in after],
            needs=params.wait_for,
            criteria=criteria,
        )
        self.link_decisions(task["id"], decisions)
        if not special:
            self.states[caller].turn_tasks.append(task["id"])
        if special:  # Its own tools: see Runtime.tool_names.
            self.store.set(f"{special}:{session_id}", "1")
        self.runtime.follow(session_id, follow)
        prompt = wrap(
            "router",
            "task",
            task_text(params.instructions, criteria, decisions),
            title=title,
            task=task["id"],
            # The owner reads its report as written: in the language they last wrote in.
            reply_in=self.store.get("owner_language"),
        )
        if context_from:
            prompt += "\n\n" + wrap("system", "context", self.tasks.briefing(context_from))
        if note := self.report_language(params.instructions):
            prompt += "\n\n" + wrap("system", "context", note)
        meta: dict[str, Any] = {"prompt": prompt, "task_id": task["id"]}
        if images := self.runtime.attachments_in(params.instructions):
            meta["attachments"] = images  # The owner's images it names come with it.
        self.runtime.add_message(session_id, "router", params.instructions, meta)
        if not special:  # Chief of Staff's own helpers come and go quietly.
            notice = f"Started “{title}”" + (f" · new because {why_new}" if why_new else "")
            self.runtime.add_message(caller, "notice", notice, {"session_id": session_id})
        await self.runtime.workers.pump()
        row = self.store.session(session_id)
        result = {"task_id": task["id"], "session_id": session_id, "status": row["status"]}
        result["cwd"] = cwd
        if branch:
            result["branch"] = branch
        if len(checkouts) > 1:
            result["checkouts"] = [checkout["path"] for checkout in checkouts]
        if params.slack and not slack:
            result["warning"] = "Slack is not connected, so this worker has no Slack access."
        if lowered:
            result["effort"] = f"The owner set workers to {floor} effort: it was kept."
        elif params.effort and not effort:
            offered = ", ".join(info["efforts"]) if info and info["efforts"] else "none"
            result["warning"] = f"Effort {params.effort} is not offered by this model ({offered})."
        if params.after:
            result["note"] = (
                "Starts automatically once the owner accepted the tasks it depends on."
                if params.wait_for == "accepted"
                else "Starts automatically when the tasks it depends on are done."
            )
        elif row["status"] == "queued":
            result["note"] = "Queued until a worker slot frees up."
        return result

    def failed_tests(self, task: dict) -> str:
        """The task's last test run, when it failed: "`command` exited 1" ("" otherwise)."""
        found = (self.runtime.task_actions.receipts(task) or {}).get("tests") or {}
        if not found or found.get("passed") is not False:
            return ""
        command = " ".join(found.get("command", "").split())[:160]
        return f"`{command}` exited {found.get('exit_code')}"

    def report_language(self, instructions: str) -> str:
        """A note when the Router wrote a task in another language than the owner's: the owner
        reads the report as written."""
        spoken = self.store.get("owner_language") or ""
        written = language(instructions)
        if not spoken or not written or written == spoken:
            return ""
        return f"Write your report in {spoken}: the owner reads it, and wrote to us in {spoken}."

    def with_turn_decisions(self, caller: str, identifiers: list[str]) -> list[dict]:
        """The decisions a task relies on: those named, and those recorded in this turn (the
        owner decided them in the message that asked for the task)."""
        named = [self.runtime.decisions.get(identifier) for identifier in identifiers]
        recorded = [
            self.runtime.decisions.get(identifier)
            for identifier in self.states[caller].turn_decisions
            if identifier not in identifiers
        ]
        return named + [decision for decision in recorded if decision["status"] == "active"]

    def link_decisions(self, task_id: str, decisions: list[dict]):
        for decision in decisions:
            self.store.execute(
                "INSERT OR IGNORE INTO task_decisions(task_id, decision_id) VALUES (?,?)",
                (task_id, decision["id"]),
            )

    async def t_message_session(self, params: MessageSession, caller: str):
        self.caller(caller, ROUTER)
        params = params.model_copy(update={"text": self.owner_words(params.text)})
        try:
            target = self.store.session(params.session_id)
        except LookupError:
            # One of the owner's own Copilot sessions: the owner decides whether it continues
            # here (unless it is open in a terminal: two programs writing one session would
            # garble it).
            return await self.ask_to_continue(params)
        if target["role"] == ROUTER:
            raise ValueError("That is your own session.")
        if self.runtime.special(target):
            raise ValueError("That is Chief of Staff's own helper, not a worker to give work to.")
        self.runtime.check_follow(target, [threads.normalize(key) for key in params.follow])
        if target["closed_at"]:
            await self.runtime.workers.reopen(
                target["id"]
            )  # A follow-up on an earlier task: same worker.
            target = self.store.session(target["id"])
        task = self.tasks.current(target["id"]) if target["role"] == WORKER else None
        if task and task["status"] == "queued" and self.tasks.deps(task["id"]):
            self.store.set(f"released:{task['id']}", "1")  # The Router decides to go ahead.
            self.runtime.background(self.runtime.workers.pump())
        self.runtime.follow(target["id"], params.follow)
        if target["role"] != WORKER:
            await self.delivery.deliver(
                target["id"],
                wrap("router", "message", params.text),
                kind="router",
                display=params.text,
            )
            return "Delivered."
        if params.task == "new":
            title = clean_title(params.title or params.text.splitlines()[0][:60])
            criteria = clean_criteria(params.done_when)
            decisions = self.with_turn_decisions(caller, params.decisions)
            task = self.tasks.create(
                target["id"], title, params.text[:4000], status="running", criteria=criteria
            )
            self.link_decisions(task["id"], decisions)
            self.states[caller].turn_tasks.append(task["id"])
            prompt = wrap(
                "router",
                "task",
                task_text(params.text, criteria, decisions),
                title=title,
                task=task["id"],
                reply_in=self.store.get("owner_language"),
            )
            if note := self.report_language(params.text):
                prompt += "\n\n" + wrap("system", "context", note)
            notice = f"New task “{title}” for “{target['title']}”"
        else:
            if task and task["status"] == "cancelled":
                raise ValueError(
                    f"Its task {task['id']} was cancelled. For new work in this worker use "
                    "task='new' with a title."
                )
            if task and task["status"] in {"review", "done", "failed"}:
                task = self.tasks.move(task["id"], "running", reason="follow-up")
            prompt = wrap(
                "router",
                "message",
                params.text,
                task=task["id"] if task else None,
                reply_in=self.store.get("owner_language"),
            )
            notice = f"Continued in “{target['title']}”"
        meta: dict[str, Any] = {"task_id": task["id"]} if task else {}
        if images := self.runtime.attachments_in(params.text):
            meta["attachments"] = images  # The owner's images it names come with it.
        await self.delivery.deliver(
            target["id"],
            prompt,
            kind="router",
            display=params.text,
            meta=meta or None,
            # More on the task it is working on reaches it within its turn; a new task waits.
            steer=params.task == "continue",
        )
        self.runtime.add_message(caller, "notice", notice, {"session_id": target["id"]})
        return f"Delivered (task {task['id']})." if task else "Delivered."

    async def ask_to_continue(self, params: MessageSession) -> str:
        reference = params.session_id
        found = [
            item
            for item in await self.runtime.workers.external(refresh=True)
            if item["id"] == reference or (len(reference) >= 6 and item["id"].startswith(reference))
        ]
        if len(found) != 1:
            raise ValueError(f"No session {reference}; list_sessions lists them.")
        item = found[0]
        if item["open"]:
            raise ValueError(
                "That session is open in the owner's terminal, and Chief of Staff cannot type "
                "into it. Tell the owner to type it there, or to exit it there (then "
                "message_session asks them to continue it here)."
            )
        title = clean_title(params.title or params.text.splitlines()[0][:60])
        target = {
            "session_id": item["id"],
            "title": item["summary"],
            "cwd": item["cwd"],
            "text": params.text[:4000],
            "task_title": title,
        }
        asked = next(
            (
                card
                for card in self.store.drafts()
                if card["kind"] == "adopt" and card["target"]["session_id"] == item["id"]
            ),
            None,
        )
        if asked:
            # One card per session: the new one, so the owner approves only what they see.
            replaced = self.store.update_draft(
                asked["id"], status="rejected", outcome="Replaced by a newer request.",
                resolved_at=now(),
            )  # fmt: skip
            self.bus.publish("draft", replaced)
        card = self.store.create_draft(
            self.runtime.router_id(), "adopt", target, params.text[:4000], item["summary"]
        )
        self.bus.publish("draft", card)
        self.runtime.alert(
            f"Continue “{item['summary']}” here?",
            session_id=self.runtime.router_id(),
            needs_action=True,
            title="Continue a session here?",
        )
        return (
            "That is one of the owner's own sessions: they were asked whether it continues "
            'here. If they agree, it gets your message as a new task (a type="draft-outcome" '
            "message tells you)."
        )

    async def answer_adopt(self, card: dict, decision: DraftDecision) -> dict:
        """The owner lets one of their own sessions continue here, with the Router's message."""
        target = card["target"]
        if decision.action == "reject":
            card = self.store.update_draft(
                card["id"], status="rejected", outcome="Not now.", resolved_at=now()
            )
            self.bus.publish("draft", card)
            await self.runtime.drafts.tell_drafter(
                card, f"The owner did not continue “{target['title']}” here.", True
            )
            return card
        if decision.action != "answer":
            raise ValueError("Continue it here, or not now.")
        # Continued here meanwhile (from Other Copilot sessions, say): it just gets the message.
        row = self.store.find_session(target["session_id"]) or await self.runtime.workers.adopt(
            target["session_id"]
        )
        reply = await self.t_message_session(
            MessageSession(
                session_id=row["id"], text=target["text"], task="new", title=target["task_title"]
            ),
            self.runtime.router_id(),
        )
        card = self.store.update_draft(
            card["id"], status="answered", outcome="Continued here.", resolved_at=now()
        )
        self.bus.publish("draft", card)
        await self.runtime.drafts.tell_drafter(
            card,
            f"The owner continued “{target['title']}” here as worker {short(row['id'])}: {reply}",
            True,
        )
        return card

    async def t_read_session(self, params: ReadSession, caller: str):
        self.caller(caller, ROUTER)
        try:
            row = self.store.session(params.session_id)
        except LookupError:
            return await self.read_terminal_session(params)
        entries = []
        steps, last_step = 0, ""
        for message in self.store.messages(row["id"])[-200:]:
            if message["kind"] == "tool":
                steps, last_step = steps + 1, message["text"]
                continue
            if steps:
                entries.append(f"{steps} step(s), the last: {last_step[:200]}")
                steps = 0
            kind = message["kind"]
            if kind == "assistant":
                kind = "said" if message["meta"].get("narration") else "answered"
            when = ago_minutes(message["created_at"])
            entries.append(f"{when} min ago, {kind}: {message['text'][:600]}")
        if steps:
            entries.append(f"{steps} step(s), the last: {last_step[:200]}")
        return {
            "session_id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "task": self.task_brief(self.tasks.current(row["id"])),
            "now": self.runtime.progress_line(row) or row["status"],
            "open_in_terminal": self.runtime.copilot_id(row["id"]) in self.runtime.elsewhere,
            "latest": entries[-params.items :],
        }

    async def read_terminal_session(self, params: ReadSession) -> dict:
        found = [
            item
            for item in await self.runtime.workers.external()
            if item["id"] == params.session_id
            or (len(params.session_id) >= 6 and item["id"].startswith(params.session_id))
        ]
        if len(found) != 1:
            raise ValueError(f"No session {params.session_id}; list_sessions lists them.")
        history = await self.runtime.workers.history(found[0]["id"])
        entries = [f"{entry['kind']}: {entry['text'][:600]}" for entry in history["entries"]]
        return {
            "session_id": found[0]["id"],
            "title": found[0]["summary"],
            "cwd": found[0]["cwd"],
            "open_in_terminal": history["open"],
            "working": history["working"],
            "latest": entries[-params.items :],
        }

    async def terminal_sessions(self) -> list[dict]:
        """The owner's own Copilot sessions open in a terminal now."""
        try:
            listed = await self.runtime.workers.external()
        except Exception:
            return []
        return [
            {
                "session_id": item["id"],
                "title": item["summary"],
                "cwd": item["cwd"],
                "last_active": item["modified"],
            }
            for item in listed
            if item["open"]
        ][:10]

    async def t_list_sessions(self, params: ListSessions, caller: str):
        settings = self.store.settings()
        try:
            models = [item["id"] for item in await self.runtime.available_models()]
        except Exception:
            models = []
        helpers = self.runtime.helper_ids()  # Chief of Staff's own: never given work.
        sessions = [
            self.session_entry(row)
            for row in self.store.open_sessions()
            if row["role"] != ROUTER and row["id"] not in helpers
        ]
        listed = {
            # The owner's own sessions and standing decisions first: they are short.
            "terminal_sessions": await self.terminal_sessions(),
            "decisions": [
                {"id": d["id"], "decision": d["text"], "tasks": d["tasks"]}
                for d in self.runtime.decisions.active()
            ],
            "sessions": sessions,
            "tasks": [
                {
                    "task_id": task["id"],
                    "title": task["title"],
                    "status": task["status"],
                    "worker": short(task["session_id"]),
                    **(
                        {"after": after}
                        if (after := [d["id"] for d in self.tasks.deps(task["id"])])
                        else {}
                    ),
                }
                for task in self.tasks.listed()
            ],
            "projects": await self.workspaces.project_cards(),
            "models": models,
            "default_worker_model": settings.role_models.get(WORKER) or settings.model or "auto",
            "slack_connected": self.runtime.slack.configured(),
            "github_repos": settings.github_repos if settings.github_enabled else [],
            "recently_closed": [
                self.runtime.workers.card(row)
                for row in self.runtime.workers.recently_closed(8)
                if row["id"] not in helpers
            ],
        }
        return shortened(listed)

    def session_entry(self, row: dict) -> dict:
        """A session as list_sessions shows it: what tells it apart; read_session has the rest."""
        entry: dict[str, Any] = {
            "session_id": row["id"],
            "role": row["role"],
            "title": row["title"],
            "status": row["status"],
            "cwd": row["cwd"],
        }
        if row["branch"]:
            entry["branch"] = row["branch"]
        if row["slack"]:
            entry["slack_access"] = True
        if follows := self.store.follows(row["id"]):
            entry["follows"] = follows
        if model := self.runtime.model_for(row):
            entry["model"] = model
        if task := self.tasks.current(row["id"]):
            entry["task"] = {
                "task_id": task["id"],
                "title": task["title"],
                "status": task["status"],
            }
        if row["report"]:
            entry["last_report"] = " ".join(row["report"].split())[:200]
        working = row["role"] == WORKER and row["status"] == "working"
        if working and (now := self.runtime.progress_line(row, brief=True)):
            entry["now"] = now
        return entry

    @staticmethod
    def task_brief(task: dict | None) -> dict | None:
        if not task:
            return None
        brief = {"task_id": task["id"], "title": task["title"], "status": task["status"]}
        if task["status"] == "review":
            brief["result"] = task["result"][:300]
        elif task["progress"]:
            brief["progress"] = task["progress"][:300]
        return brief

    def related_workers(self, text: str, project: str, exclude: list[str]) -> list[dict]:
        """Workers that may already own this subject: the same project, or a similar task."""
        cards = [
            self.runtime.workers.card(row)
            for row in self.store.all(
                """SELECT * FROM sessions WHERE role='worker' AND project=? AND project!=''
                ORDER BY updated_at DESC LIMIT 3""",
                (project,),
            )
        ]
        for card in self.runtime.workers.find(text, limit=3):
            if card["session_id"] not in {known["session_id"] for known in cards}:
                cards.append(card)
        branches = {
            row["id"]: row["branch"]
            for row in self.store.all(
                "SELECT id, branch FROM sessions WHERE role='worker' AND branch!=''"
            )
        }
        return [
            {
                "session_id": card["session_id"],
                "title": card["title"],
                "state": "closed" if card["closed"] else card["status"],
                "task": card["task"][:200],
                "last_report": card["last_report"][:200],
                **(
                    {"branch": branches[card["session_id"]]}
                    if card["session_id"] in branches
                    else {}
                ),
            }
            for card in cards
            if card["session_id"] not in exclude
        ][:4]

    def related_hint(self, text: str) -> str:
        """Workers whose earlier work may be what this request is about, for the Router."""
        cards = self.runtime.workers.find(text, limit=4)
        if not cards:
            return ""

        def clip(value: str, size: int) -> str:
            value = " ".join(value.split())
            return value if len(value) <= size else value[: size - 1] + "…"

        lines = []
        for card in cards:
            state = "closed" if card["closed"] else card["status"]
            task = clip(card["task"], 200)
            line = f"- “{card['title']}” ({card['session_id']}, {state}). Task: {task}"
            if card["last_report"]:
                line += f" Result: {clip(card['last_report'], 160)}"
            lines.append(line)
        return (
            "Earlier workers that may be about the same subject (keyword match):\n"
            + "\n".join(lines)
            + "\nIf this request concerns the same project, repo or topic as one of them, send it "
            "to that worker with message_session (a closed one reopens with its context and "
            "files). Start a new worker only if none does."
        )

    async def t_find_sessions(self, params: FindSessions, caller: str):
        row = self.caller(caller, ROUTER, WORKER)
        matches = self.runtime.workers.find(params.query)
        if row["role"] == ROUTER:
            return {"matches": matches}
        # A worker sees only sessions it may ask: not itself, same Slack access.
        peers = [
            match
            for match in matches
            if match["session_id"] != caller
            and bool(self.store.session(match["session_id"])["slack"]) == bool(row["slack"])
        ]
        return {"matches": peers, "router_id": self.runtime.router_id()}

    async def t_notify_owner(self, params: NotifyOwner, caller: str):
        self.caller(caller, ROUTER)
        if self.states[caller].owner_reply and not (params.needs_action or params.urgent):
            # One message per answer: the owner reads the reply of this turn.
            return "Not sent: the owner wrote to you, so say this in your reply instead."
        meta = {"needs_action": params.needs_action, "urgent": params.urgent}
        self.runtime.add_message(caller, "update", params.text, meta, unread=True)
        self.runtime.alert(
            params.text,
            session_id=caller,
            needs_action=params.needs_action,
            urgent=params.urgent,
        )
        return "Shown to the owner."

    async def t_close_worker(self, params: CloseWorker, caller: str):
        self.caller(caller, ROUTER)
        row = self.store.session(params.session_id)
        if row["role"] != WORKER:
            raise ValueError("Only workers can be closed.")
        await self.runtime.workers.close(row["id"])
        return "Closed."

    async def t_report(self, params: Report, caller: str):
        row = self.caller(caller, WORKER, SLACK)
        task = (
            self.runtime.task_actions.report_task(caller, params.task_id)
            if row["role"] == WORKER
            else None
        )
        if (
            task
            and params.status == "done"
            # Its first answer: not a follow-up's news, nor a cancelled task's.
            and task["status"] == "running"
            and not task["result"]
            and not self.store.get(f"fuller:{task['id']}")
            and (missing := short_of(task["goal"] or "", params.summary))
        ):
            # Asked once: a short answer that is the whole answer is taken the second time.
            self.store.set(f"fuller:{task['id']}", "1")
            raise ValueError(
                f"Not reported: the owner asked for {missing}, and this report is all they "
                "read. Call report again with the whole answer: how you got there step by "
                "step, the numbers, and the table if one was asked for. If this already is the "
                "whole answer, call report again with it unchanged."
            )
        if (
            task
            and params.status == "done"
            and task["status"] == "running"
            and (unread := self.states[caller].unread)
            and not self.store.get(f"unread_report:{task['id']}")
        ):
            # Sent while it was finishing: a report now would be without it, and whoever sent
            # it would send it again. Once: it may be lost on the way.
            self.store.set(f"unread_report:{task['id']}", "1")
            newest = list(unread.values())[-1]
            raise ValueError(
                "Not reported yet: a newer message for this task arrived while you were "
                f"finishing (“{newest[:200]}”). It reaches you next: handle it, then report once, "
                "covering both. If you see it nowhere after this, report again as you were."
            )
        if (
            task
            and params.status == "done"
            and task["status"] == "running"
            and (failed := self.failed_tests(task))
            and not self.store.get(f"failing_report:{task['id']}")
        ):
            # Once: a failure it expected (the regression test before the fix) or one that is
            # not its own is reported as such.
            self.store.set(f"failing_report:{task['id']}", "1")
            raise ValueError(
                f"Not reported yet: your last test run failed ({failed}). Fix what fails and "
                "run it again; if the failure is expected or not caused by your change, call "
                "report again and say so."
            )
        if (
            task
            and params.status == "done"
            and task["status"] == "running"
            and (problem := await self.runtime.checks.before_done(row, task))
        ):
            raise ValueError(problem)
        meta = {"status": params.status, "task_id": task["id"] if task else None}
        self.runtime.add_message(caller, "report", params.summary, meta)
        if task and params.status == "done" and params.criteria:
            run = self.store.one(
                "SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
                (task["id"],),
            )
            if run:
                claims = [item.model_dump() for item in params.criteria[:12]]
                self.store.execute(
                    "UPDATE runs SET criteria=? WHERE id=?", (json.dumps(claims), run["id"])
                )
        settled = False
        if task:
            asked = bool(self.store.get(f"followup:{task['id']}"))
            if task["status"] == "cancelled" and not asked:
                # Cancelled (a report already on its way, say): it stays cancelled.
                settled = True
            elif task["status"] == "cancelled" and params.status != "done":
                # Answering the owner's question about it: news, not the task coming back.
                task = self.tasks.update(task["id"], progress=params.summary[:6000])
            elif task["status"] in {"review", "done"} and params.status == "progress":
                # News about finished work (PR feedback handled, a question answered): it stays
                # where it is. A blocker or a question needs the owner, and shows as such.
                task = self.tasks.update(task["id"], progress=params.summary[:6000])
            elif params.status == "done" and task["status"] == "done" and not asked:
                # Accepted, or its PR merged: news about it does not reopen the review.
                self.runtime.task_actions.remember_prs(task["id"], params.summary)
                task = self.tasks.update(task["id"], result=clip_report(params.summary))
            elif params.status == "done":
                self.store.delete(f"followup:{task['id']}")
                self.runtime.task_actions.remember_prs(task["id"], params.summary)
                task = self.tasks.move(task["id"], "review", result=clip_report(params.summary))
                if self.store.get(f"draft_sent:{task['id']}"):
                    # The owner already sent what it drafted: one approval was enough.
                    self.runtime.background(
                        self.runtime.drafts.settle_reported(task["id"], reported=True)
                    )
                else:
                    self.runtime.background(self.runtime.task_actions.collect_evidence(task["id"]))
                    # The one notification a finished task gets (the Router does not retell it).
                    self.runtime.alert(
                        f"“{task['title']}” is ready to review",
                        session_id=caller,
                        needs_action=False,
                        title="Ready to review",
                    )
            elif params.status in {"question", "blocked"}:
                task = self.tasks.move(task["id"], "waiting", progress=params.summary[:6000])
            elif task["status"] != "running":
                task = self.tasks.move(task["id"], "running", progress=params.summary[:6000])
            else:
                task = self.tasks.update(task["id"], progress=params.summary[:6000])
        self.states[caller].resting = ""
        if row["role"] == WORKER and params.status in {"progress", "done"}:
            # A PR it delivers is its PR: reviews, comments and CI come back to it.
            self.runtime.follow(caller, threads.pull_requests(params.summary), given=False)
        if row["role"] == WORKER:
            self.runtime.watchdog.save(caller, nudges=0)
        if settled and task:
            return f"Task {task['id']} was cancelled: this report stays in your session only."
        values: dict[str, Any] = {"report": params.summary[:4000]}
        if row["role"] == WORKER and params.status in REPORT_STATUS:
            values["status"] = REPORT_STATUS[params.status]
        self.runtime.update_session(caller, **values)
        if params.status == "done":
            self.runtime.background(
                self.runtime.workers.pump()
            )  # Tasks waiting for this one may start now.
        if params.status == "progress":
            return "Noted in your session. The Router is not interrupted for progress."
        await self.runtime.tell_router(row, params.status, params.summary, task)
        return "Reported to the Router."

    async def t_cancel_task(self, params: CancelTask, caller: str):
        self.caller(caller, ROUTER)
        task = await self.runtime.task_actions.cancel(
            self.runtime.task_actions.task_for(params.task_id)["id"], params.reason, "the Router"
        )
        return f"Cancelled task {task['id']}."

    async def t_draft_slack_reply(self, params: DraftSlackReply, caller: str):
        row = self.caller(caller, WORKER, SLACK)
        if not (row["role"] == SLACK or row["slack"]):
            raise ValueError("This session has no Slack access.")
        if not self.runtime.slack.configured():
            raise ValueError("Slack is not connected.")
        if not SLACK_ID.fullmatch(params.channel_id):
            raise ValueError("channel_id must be a Slack conversation id such as C0123ABC.")
        if params.thread_ts and not SLACK_TS.fullmatch(params.thread_ts):
            raise ValueError("thread_ts must look like 1712345678.123456.")
        if not params.text.strip():
            raise ValueError("The reply is empty.")
        if len(params.text) > MAX_DRAFT:
            raise ValueError(f"Too long: keep the reply under {MAX_DRAFT} characters.")
        target = {"channel_id": params.channel_id, "thread_ts": params.thread_ts, "to": params.to}
        target["latest"] = await self.runtime.slack.latest(params.channel_id, params.thread_ts)
        draft = self.store.create_draft(caller, "slack", target, params.text, params.note)
        self.runtime.add_message(caller, "draft", params.text, {"draft_id": draft["id"]})
        self.bus.publish("draft", draft)
        self.runtime.alert(
            params.note or params.text,
            session_id=caller,
            needs_action=True,
            title="Slack reply to approve",
        )
        return (
            f"Draft {draft['id']} is waiting for the owner's approval. You will be told if "
            "they edit or reject it. Do not send it any other way."
        )

    async def t_draft_reply(self, params: DraftReply, caller: str):
        """A reply Chief of Staff cannot send itself (Slack not connected, mail, a doc comment):
        the owner approves it and sends it themself (it is copied for them)."""
        self.caller(caller, WORKER, SLACK)
        text = params.text.strip()
        if not text:
            raise ValueError("The reply is empty.")
        if len(text) > MAX_DRAFT:
            raise ValueError(f"Too long: keep the reply under {MAX_DRAFT} characters.")
        link = params.link.strip()
        if link and not re.match(r"^https?://\S+$", link):
            raise ValueError("link must be an http(s) URL, or empty.")
        target = {"to": params.to[:200], "where": params.where[:200], "link": link}
        draft = self.store.create_draft(caller, "reply", target, text, params.note[:300])
        self.runtime.add_message(
            caller, "draft", text, {"draft_id": draft["id"], "label": f"Reply to {params.to}"}
        )
        self.bus.publish("draft", draft)
        self.runtime.alert(
            params.note or text, session_id=caller, needs_action=True, title="Reply to approve"
        )
        return (
            f"Draft {draft['id']} is waiting for the owner: they copy it and send it themself. "
            "You will be told what they did."
        )

    async def works_on_pr(self, caller: str, repo: str, number: int, pull: dict) -> bool:
        """A PR this worker was given (by the Router or the owner: to follow, opened with Create
        PR, named in its task), one from its own branch, or one GitHub asks the owner to review
        (or that the owner wrote). Not one it chose to follow itself: what it reads (Slack, PRs)
        must not widen where it can draft."""
        thread = f"github:{repo}#{number}"
        if self.store.get(f"given:{caller}:{thread}"):
            return True
        for task in self.tasks.for_session(caller):
            opened = json.loads(self.store.get(f"prs:{task['id']}") or "[]")
            if names_pr(f"{task['title']}\n{task['goal'] or ''}", repo, number) or (
                thread in threads.pull_requests(" ".join(opened))
            ):
                return True
        people = {(pull.get("user") or {}).get("login", "")} | {
            reviewer.get("login", "") for reviewer in pull.get("requested_reviewers") or []
        }
        if {login.lower() for login in people} & await self.runtime.github.logins():
            return True
        row = self.store.session(caller)
        branches = {c.get("branch") or "" for c in self.workspaces.checkouts(row)}
        branches = {branch for branch in branches | {row["branch"] or ""} if branch}
        return bool(branches) and (pull.get("head") or {}).get("ref") in branches

    async def t_draft_github_review(self, params: DraftGitHubReview, caller: str):
        self.caller(caller, WORKER)
        settings = self.store.settings()
        repo = params.repo.lower().strip()
        github = self.runtime.github
        if not params.body.strip():
            raise ValueError("The text is empty.")
        if len(params.body) + sum(len(comment.body) for comment in params.comments) > MAX_DRAFT:
            raise ValueError(f"Too long: keep the review under {MAX_DRAFT} characters.")
        if params.in_reply_to and params.comments:
            raise ValueError("A reply to a review comment has no inline comments of its own.")
        if params.in_reply_to and params.verdict != "comment":
            raise ValueError("A reply to a review comment has no verdict: leave verdict out.")
        try:
            pull = await github.pull(repo, params.number)
        except RuntimeError as exc:
            if not NO_ACCESS.search(str(exc)):
                raise
            try:
                await github.gh("api", f"repos/{repo}", "--jq", ".full_name")
            except RuntimeError:
                pass
            else:
                raise ValueError(f"{repo} has no pull request #{params.number}.") from exc
            logins = ", ".join(sorted(await github.logins())) or "none"
            if "saml" in str(exc).lower():
                fix = (
                    "the organization requires single sign-on: authorize gh's token for it "
                    "(the link in GitHub's message, or `gh auth refresh -h github.com`)"
                )
            else:
                fix = (
                    "signing in with the account that can (`gh auth login`) fixes it; Chief of "
                    "Staff then uses that account for the repository on its own"
                )
            raise ValueError(
                f"No GitHub account signed in to gh on the owner's machine (signed in: {logins}) "
                f"can see {repo}#{params.number}, so Chief of Staff cannot publish there. Tell "
                f'the owner with report(status="question"): {fix}. Keep the drafted text in '
                f"your report. GitHub said: {str(exc)[-400:]}"
            ) from exc
        connected = settings.github_enabled and repo in settings.github_repos
        if not (connected or await self.works_on_pr(caller, repo, params.number, pull)):
            raise ValueError(
                "Reviews and replies are drafted on connected repositories or on a PR your task "
                "is about (one you follow, opened or were given, or one the owner is asked to "
                "review)."
            )
        target: dict[str, Any] = {
            "repo": repo,
            "number": params.number,
            "url": f"https://github.com/{repo}/pull/{params.number}",
            "via": "connected" if connected else "task",
        }
        if params.in_reply_to:
            target["in_reply_to"] = params.in_reply_to
            target["comment"] = await github.review_comment(repo, params.number, params.in_reply_to)
            label = f"Reply on {repo}#{params.number}"
        else:
            if pull.get("state") != "open":
                raise ValueError(f"{repo}#{params.number} is not open.")
            target["head_sha"] = pull["head"]["sha"]
            target["verdict"] = params.verdict
            author = ((pull.get("user") or {}).get("login") or "").lower()
            if author and author == await github.acting_login(repo):
                # GitHub allows only comments on one's own pull request (one of the owner's
                # other accounts may approve it).
                target["own"] = True
                target["verdict"] = "comment"
            if params.comments:
                if problems := misplaced(
                    params.comments, await github.diff_lines(repo, params.number)
                ):
                    raise ValueError(
                        "GitHub only takes comments on lines of the PR's diff; fix these and "
                        "draft again (or move them into the body):\n- " + "\n- ".join(problems[:20])
                    )
                target["comments"] = [
                    comment.model_dump(exclude_defaults=True) | {"side": comment.side}
                    for comment in params.comments
                ]
            label = {"approve": "Approval", "request_changes": "Changes requested"}.get(
                target["verdict"], "Review"
            ) + f" of {repo}#{params.number}"
        draft = self.store.create_draft(caller, "github", target, params.body)
        shown = params.body
        if params.comments:
            shown += "\n\nInline comments:\n" + "\n".join(
                f"- {c.path}:{c.line}: {c.body}" for c in params.comments
            )
        self.runtime.add_message(caller, "draft", shown, {"draft_id": draft["id"], "label": label})
        self.bus.publish("draft", draft)
        self.runtime.alert(
            label, session_id=caller, needs_action=True, title="GitHub text to approve"
        )
        result = f"Draft {draft['id']} is waiting for the owner's approval."
        if target.get("own") and params.verdict != "comment":
            result += (
                " The PR is by the account that publishes the review, and GitHub does not let "
                "anyone approve or request changes on their own PR: it is drafted as a comment."
            )
        return result

    async def t_distill_profile(self, params: DistillProfile, caller: str):
        self.caller(caller, ROUTER)
        try:
            recent = await self.runtime.workers.external(refresh=True)
        except Exception:
            recent = []
        lines = [prompts.DISTILL]
        if params.focus:
            lines.append(f"Focus: {params.focus}")
        if current := self.store.get("profile"):
            lines.append(f"Current profile, to update rather than replace:\n{current}")
        lines.append(
            "Configured projects:\n" + "\n".join(f"- {p}" for p in self.workspaces.projects()[:40])
        )
        lines.append(
            "Recent Copilot sessions of the owner (summary · directory):\n"
            + "\n".join(f"- {item['summary']} · {item['cwd']}" for item in recent[:40])
        )
        examples = json.loads(self.store.get("style_examples") or "[]")
        if examples:
            lines.append(
                "Slack replies the owner rewrote, and reasons they rejected drafts (quoted data):\n"
                + "\n".join(
                    f"- {json.dumps(e.get('sent') or 'rejected: ' + e.get('reason', ''))}"
                    for e in examples[-20:]
                )
            )
        return await self.t_spawn_worker(
            SpawnWorker(
                title="Distill owner profile",
                instructions="\n\n".join(lines),
                slack=self.runtime.slack.configured(),
            ),
            caller,
            special="distill",
        )

    async def distill_now(self) -> dict:
        """Settings' Build from my work: a profile worker, unless one is at work already."""
        for row in self.store.open_sessions(WORKER):
            if self.store.get(f"distill:{row['id']}") and row["status"] in {
                "queued",
                "starting",
                "working",
            }:
                return {"session_id": row["id"], "status": row["status"]}
        return await self.t_distill_profile(DistillProfile(), self.runtime.router_id())

    async def t_propose_profile(self, params: ProposeProfile, caller: str):
        self.caller(caller, WORKER)
        if not self.store.get(f"distill:{caller}"):
            raise ValueError("Only a profile distillation worker can propose a profile.")
        text = params.markdown.strip()[:12000]
        if not text:
            raise ValueError("The profile is empty.")
        self.settle_profile_cards("Replaced by a newer draft.")
        self.store.set("profile_draft", text)
        card = self.store.create_draft(
            self.runtime.router_id(),
            "profile",
            {"worker": caller},
            text,
            "Distilled from your work",
        )
        self.bus.publish("draft", card)
        self.runtime.add_message(
            caller, "draft", text, {"draft_id": card["id"], "label": "Profile draft"}
        )
        self.bus.publish("state")
        self.runtime.alert(
            "Your profile draft is ready: check it in the inbox before sessions use it.",
            session_id=self.runtime.router_id(),
            needs_action=True,
            title="Profile to review",
        )
        task = self.tasks.current(caller)
        if task and task["status"] not in {"done", "cancelled"}:
            self.tasks.move(task["id"], "done", result="Proposed your profile; it is in the inbox.")
        self.states[caller].close_after_turn = True
        return "Proposed. The owner reviews it in the inbox. You are done: do not call report."

    async def save_profile(self, text: str):
        self.store.set("profile", text.strip())
        self.store.set("profile_draft", "")
        self.settle_profile_cards("Saved from Settings.", "answered")
        await self.runtime.refresh(ROUTER, SLACK)
        self.bus.publish("state")

    def discard_profile(self):
        self.store.set("profile_draft", "")
        self.settle_profile_cards("Discarded.")
        self.bus.publish("state")

    def ensure_profile_card(self):
        """A profile draft from before drafts were reviewed in the inbox gets its card."""
        text = self.store.get("profile_draft", "")
        if text and not any(card["kind"] == "profile" for card in self.store.drafts()):
            card = self.store.create_draft(
                self.runtime.router_id(), "profile", {}, text, "Distilled from your work"
            )
            self.bus.publish("draft", card)

    def settle_profile_cards(self, outcome: str, status: str = "rejected"):
        """Pending profile cards end when the draft is used or dropped some other way."""
        for card in self.store.drafts():
            if card["kind"] == "profile":
                card = self.store.update_draft(
                    card["id"], status=status, outcome=outcome, resolved_at=now()
                )
                self.bus.publish("draft", card)

    async def review_profile(self, card: dict, decision: DraftDecision) -> dict:
        """The owner uses the drafted profile (edited or not) or drops it."""
        if decision.action == "reject":
            self.discard_profile()
        elif decision.action == "answer" and decision.text.strip():
            await self.save_profile(decision.text)
        else:
            raise ValueError("Use the profile, or discard it.")
        return self.store.draft(card["id"])

    async def t_follow_thread(self, params: FollowThread, caller: str):
        self.caller(caller, WORKER, SLACK)
        followed = self.runtime.follow(caller, [params.thread], given=False)
        return f"Following {', '.join(followed)}."

    async def t_unfollow_thread(self, params: FollowThread, caller: str):
        self.caller(caller, WORKER, SLACK)
        self.store.unfollow(threads.normalize(params.thread), caller)
        self.runtime.publish_session(self.store.session(caller))
        return "Unfollowed. New messages there go to the Router again."

    async def t_assess_worker(self, params: AssessWorker, caller: str):
        self.caller(caller, WATCHDOG)
        row = self.store.session(params.session_id)
        if row["role"] != WORKER or row["closed_at"]:
            raise ValueError("That is not an open worker.")
        action = params.action
        nudges = self.runtime.watchdog.state(row["id"]).get("nudges", 0)
        if action in {"nudge", "interrupt"} and nudges >= self.runtime.watch.nudges:
            action = "escalate"  # Earlier nudges did not help; a person or the Router must decide.
        self.runtime.add_message(
            caller,
            "notice",
            f"“{row['title']}”: {params.verdict.replace('_', ' ')}, {action}. {params.reason}",
            {"session_id": row["id"]},
        )
        if action in {"nudge", "interrupt"}:
            if action == "interrupt":
                await self.runtime.workers.interrupt(row["id"], by="the watchdog")
            text = params.message or params.reason
            # Into the current turn: a worker stuck in a long turn would never see a queued one.
            await self.delivery.deliver(
                row["id"],
                wrap("watchdog", "nudge", text),
                kind="watchdog",
                display=text,
                # Shown with the nudge: why the Watchdog stepped in.
                meta={"verdict": params.verdict, "reason": params.reason[:500]},
                steer=True,
            )
            self.runtime.watchdog.save(row["id"], nudges=nudges + 1)
        elif action == "escalate":
            last = self.store.messages(row["id"])
            self.runtime.watchdog.save(row["id"], escalated_mark=last[-1]["id"] if last else 0)
            await self.delivery.deliver(
                self.runtime.router_id(),
                wrap(
                    "watchdog",
                    "escalation",
                    f"The worker looks {params.verdict.replace('_', ' ')}: {params.reason}",
                    worker=short(row["id"]),
                    title=row["title"],
                ),
                kind="inbound",
                display=params.reason,
                meta={
                    "from": row["id"],
                    "title": row["title"],
                    "status": f"watchdog: {params.verdict}",
                },
            )
        return f"Recorded: {action}."
