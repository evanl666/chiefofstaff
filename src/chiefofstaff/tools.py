"""The tools sessions call: what each role may do in Chief of Staff, and the argument
models the model sees.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Iterable, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from copilot import ToolResult, define_tool
from pydantic import BaseModel, Field

from . import prompts, receipts, threads
from .asks import AnswerSession, AskSession, RecordSetup
from .checks import ProposeChecks
from .decisions import AckDecision, AskDecision, RecordDecision
from .digest import NEEDS_WORDS, Needs, needs, task_words, waiting_parts
from .envelope import clean_title, language, short, wrap
from .habits import UpdateStyle
from .learning import ProposeLessons
from .models import (
    MAX_DRAFT,
    DraftDecision,
    NestedArguments,
    Watch,
    WithdrawDraft,
    leak_proof,
)
from .peers import TellSession
from .policy import within
from .roles import ROUTER, SLACK, WATCHDOG, WORKER
from .sources.github import NO_ACCESS, diff_lines, snippet
from .store import ago_minutes, now
from .task_actions import GITHUB_RUN, deliverable_title, style_note
from .tasks import ADOPTED, OPEN
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

REPLACES = (
    "Ids (t…) of failed tasks (or tasks still open) that this new task does instead: they "
    "are cancelled as replaced by it, so the owner is not also offered to retry them. To try "
    "a failed task again in its own worker, message_session it with task='continue' and its "
    "task_id instead"
)
WHY_NEW_WORDS = (
    "The owner reads it after “new because”, so a short phrase that goes on from there, said "
    "to them (“a different repository”, “you asked for a separate PR”), never your reasoning "
    "(not “separate decision task, no code change”). In the language the owner writes in: to "
    "an owner who writes Chinese, in Chinese (“你要单独的 PR”), or it is not shown"
)


logger = logging.getLogger(__name__)


class SpawnWorker(BaseModel):
    title: str = Field(
        description="What the task delivers, under 60 characters: a verb, the object and where "
        "(repo#PR, flow), e.g. 'Fix the Spark 3.5 dependency · ranking-data#594'"
    )
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
    in_place: bool = Field(
        default=False,
        description="Only when the owner asks in so many words for the work to be done directly "
        "in their own project folder (no worktree): the worker may change files there. "
        "Otherwise a worker given the folder only reads it",
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
    review: bool = Field(
        default=False,
        description="True when the owner asked to see what it makes before it counts (a "
        "mockup, a plan, a doc or a proposal). Every finished result waits for their Accept "
        "either way; this marks it as one they asked to look at",
    )
    context_from: list[str] = Field(
        default_factory=list,
        description="Task ids (or worker ids) of earlier work this new worker should build on: "
        "it gets their goals, results, folders and files, without their whole conversation",
    )
    works_with: list[str] = Field(
        default_factory=list,
        description="The tasks running at the same time that this one needs while it works, "
        "or that need it (two sides of one change, a value one finds that the other uses): "
        "their titles (also of tasks you start in this same turn, in parallel calls) or task "
        "ids. Each is told about the other, they pass information straight to each other, and "
        "you get a copy. Not for a task that needs another's finished result before it can "
        "start: that is after",
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
        "github:<owner/repo>#<n>); later messages there go straight to this session. A PR the "
        "instructions ask to act on (review, fix, merge…) is followed too; one they only "
        "mention for context is not",
    )
    why_new: str = Field(
        default="",
        description="Only after spawn_worker listed related workers: why none of them should "
        "take this (different subject, an independent change that needs its own PR, or work "
        "that must run in parallel with theirs). " + WHY_NEW_WORDS,
    )
    replaces: list[str] = Field(
        default_factory=list,
        description=REPLACES,
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


ACK_WORDS = {
    "thanks", "thank", "thx", "ty", "received", "noted", "ok", "okay", "great", "perfect",
    "cool", "nice", "got", "acknowledged", "lgtm", "awesome", "appreciated", "cheers",
}  # fmt: skip
ACK_FILLER = {
    "you", "it", "this", "that", "a", "lot", "so", "much", "very", "for", "the", "update",
    "report", "work", "looks", "good", "all", "and", "job", "well", "done", "nice", "will",
    "take", "look", "i", "we", "have", "has", "been", "is",
}  # fmt: skip
ACK_CJK = re.compile(
    r"谢谢|感谢|多谢|收到|好的|明白了?|了解|辛苦了?|知道了|没问题|很好|不错|嗯|好|啦|了"
)
# Chinese, Japanese or Korean writing.
CJK_TEXT = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def acknowledgement(text: str) -> bool:
    """Whether a message only acknowledges (“Thanks, received.”, “收到 谢谢”): nothing to do."""
    text = text.strip().lower()
    if not text or len(text) > 80 or "?" in text or "\uff1f" in text:
        return False
    words = re.findall(r"[a-z']+", text)
    cjk = re.findall(r"[\u4e00-\u9fff]+", text)
    if any(ACK_CJK.sub("", run) for run in cjk):
        return False
    if re.search(r"\d", text):
        return False
    acked = any(word in ACK_WORDS for word in words) or bool(cjk)
    return acked and all(word in ACK_WORDS | ACK_FILLER for word in words)


class MessageSession(BaseModel):
    session_id: str = Field(
        description="Target session id: from list_sessions, or the short id in a message's "
        "from attribute (worker:<id>)"
    )
    text: str = Field(
        description=f"The message for that session. {OWNER_WORDS} stands for the owner's message "
        "with that ref attribute, exactly as written"
    )
    task: Literal["continue", "new", "note"] = Field(
        default="continue",
        description="For a worker: continue its current task (more on the same subject: a "
        "correction, more information, a follow-up question; a task in review reopens), start "
        "a new task in the same session, reusing its context (give a title; also for another "
        "subject, and for more work on a task the owner accepted), or note: tell it something "
        "without changing any task (an answer to its question, an acknowledgement, an FYI)",
    )
    task_id: str = Field(
        default="",
        description="For continue or note: the id (t…) of the worker's task this is about. "
        "Needed when it has several open tasks; default its current task",
    )
    title: str = Field(
        default="",
        description="Required when task='new': what the new task delivers (a verb, the object "
        "and where: repo#PR, flow), under 60 characters",
    )
    done_when: list[str] = Field(
        default_factory=list,
        description="For task='new': its acceptance criteria, as for spawn_worker",
    )
    review: bool = Field(
        default=False, description="For task='new': as for spawn_worker (the owner wants to see it)"
    )
    decisions: list[str] = Field(
        default_factory=list,
        description="For task='new': ids (dec…) of decisions it relies on, as for spawn_worker",
    )
    earlier: Literal["", "finish", "keep", "superseded"] = Field(
        default="",
        description="For task='new' when the worker still has a running task: what becomes of "
        "it. finish: what it delivers is delivered, or it waits on the owner; its worker "
        "reports it (done, blocked or question) before starting the new one. keep: it goes on "
        "after the new one. superseded: the new task replaces it, and it is cancelled",
    )
    replaces: list[str] = Field(
        default_factory=list, description=f"For task='new': {REPLACES[0].lower()}{REPLACES[1:]}"
    )
    why_new: str = Field(
        default="",
        description="For task='new' when the worker was given a task in the last half hour: "
        "why this is another deliverable, not more of that task (a correction, one more thing "
        "is task='continue'). " + WHY_NEW_WORDS,
    )
    follow: list[str] = Field(
        default_factory=list,
        description="Thread keys from the event (slack:<channel>:<ts>, slack:<D…>, "
        "github:<owner/repo>#<n>); later messages there go straight to this session",
    )


class ListSessions(BaseModel):
    pass


class CheckPrs(BaseModel):
    prs: list[str] = Field(
        min_length=1,
        max_length=10,
        description="PR URLs or github:<owner/repo>#<n> keys (from reports and list_sessions)",
    )


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


class ArtifactState(BaseModel):
    url: str = Field(
        description="Its link, or the full path of a file it made in your folder (a mockup, a "
        "CSV): the owner opens it from the list"
    )
    name: str = Field(
        description="A short name the owner knows it by, under 40 characters: 'Interviewer "
        "spec', 'ICP flow, full run', 'BG embeddings DAG'"
    )
    status: str = Field(
        description="Where it stands now, in a word or two: a doc or sheet 'draft', 'in "
        "review', 'final'; a flow, DAG run or job 'queued', 'running', 'succeeded', 'failed'; "
        "a dashboard or table 'live', 'updated'"
    )


class Report(NestedArguments):
    status: Literal["progress", "question", "blocked", "done", "failed"]
    summary: str = Field(
        description="What happened, or the one concrete question. With status done: the whole "
        "answer the owner reads (tables, lists, every row asked for), up to 30,000 characters; "
        "never only a pointer to a file. With status failed: why it cannot be done, and what "
        "you tried"
    )
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
    artifacts: list[ArtifactState] = Field(
        default_factory=list,
        max_length=12,
        description="What this task made or runs that the owner follows (a doc, a sheet, a "
        "Flyte or Airflow run, a job, a dashboard, a table), each with a short name and its "
        "status now; whenever one is made or its status changes. Not PRs: their status comes "
        "from GitHub",
    )
    why: str = Field(
        default="",
        max_length=1000,
        description="With status question or blocked: why you need the owner for it (what only "
        "they can decide or give), in a sentence",
    )
    options: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="With status question or blocked: the choices they can pick from, each in "
        "a few words, your recommendation first; empty when only their own words answer it",
    )
    next: str = Field(
        default="",
        max_length=1000,
        description="With status question or blocked: what you will do once they answer",
    )
    about_draft: bool = Field(
        default=False,
        description="With status question: true when the question only asks the owner to "
        "decide a draft you made, or make in this same turn (send it, post it, approve it): "
        "its card asks it, once. False for any other question, even one asked next to a draft",
    )
    new_task_title: str = Field(
        default="",
        description="With status done, when this is work apart from the task (the owner asked "
        "you straight for separate work with a result of its own: another branch, PR, "
        "document, run or flow, a change), finished or not: a title saying what it delivers. "
        "It becomes a task of its own with this result; the task keeps its result and PR (one "
        "still at work goes on, reported by itself). Never for the task's own result, nor for "
        "an answer to a question (answer that in your reply)",
    )
    replaces_result: bool = Field(
        default=False,
        description="With status done on a task already finished, after the owner wrote to you "
        "about it: true when this report is the task's whole result again, redone or "
        "corrected (it replaces the one they have; the earlier one is kept); false when it "
        "only answers a question about the finished work (that is news on it, and its result "
        "stays)",
    )
    changed_code: bool | None = Field(
        default=None,
        description="With status done: whether you changed files in a checkout for this task "
        "(true or false)",
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


# Words a reason starts with as a sentence does (“The task is busy”, “Different subject”): not
# capitalized after "new because". Any other capitalized word may start a name or a title
# (“Long Count is busy”, “Alice asked for it”), which keeps its capital.
SENTENCE_STARTS = frozenset(
    "a an the this that these those it its it's another other others different separate "
    "independent unrelated new same each both either neither no not one two your you you're "
    "they their there we our for from in on at by to with without after before since because "
    "as so but and or while unlike only also still just already needs needed requested asked "
    "own parallel research review work".split()
)


def mid_sentence(text: str, names: Iterable[str] = ()) -> str:
    """A reason as it reads after "new because": no period to end it, and no capital to start
    it when it starts as a sentence does (“The task is busy”), unless that is the start of a
    name in `names` (a task's title: “The Long Count is busy”). A name, a title or an acronym
    keeps its capital (“Long Count is busy”, “Alice asked”, “PR of its own”)."""
    if text.endswith("\u3002") or (text.endswith(".") and not text.endswith("..")):
        text = text[:-1].rstrip()
    words = text.split(maxsplit=2)
    first = words[0].replace("\u2019", "'").rstrip(",:;") if words else ""
    second = words[1] if len(words) > 1 else ""
    titled = second[:1].isupper() and not second.isupper()  # “The Long Count is busy”
    named = any(name and text.startswith(name) for name in names)
    if first[:1].isupper() and first.lower() in SENTENCE_STARTS and not titled and not named:
        text = text[0].lower() + text[1:]
    return text


class CancelTask(BaseModel):
    task_id: str = Field(description="Task id (t…), or a worker id for its current task")
    reason: str = Field(default="", description="Why, in a few words (the owner sees it)")


class StopWorker(BaseModel):
    session_id: str = Field(
        description="The worker to stop (its id or short id), or a task id (t…) for its worker"
    )


class StopFollowing(BaseModel):
    session_id: str = Field(
        description="The worker that follows it (its id or short id), or a task id (t…)"
    )
    thread: str = Field(
        default="",
        description="slack:<channel_id>:<thread_ts>, slack:<D…> (a DM) or github:<owner/repo>#<n>; "
        "empty: every thread and PR it follows for that task (or, given a worker, all of them)",
    )


class AddWatch(BaseModel):
    what: str = Field(
        description="What to look out for, in the owner's words, with where: 'New messages in "
        "#deploys about the payments service', 'Mail from Acme legal', 'Comments on my design "
        "doc'"
    )
    how: str = Field(
        default="",
        description="Which tool or source to use when the owner said ('the Slack MCP', 'Glean "
        "search'); empty otherwise",
    )


class RenameTask(BaseModel):
    task_id: str = Field(description="Task id (t…)")
    title: str = Field(
        description="What it delivers: a verb, the object and where (repo#PR, flow), under 60 "
        "characters"
    )


class SplitTask(BaseModel):
    task_id: str = Field(description="The finished task (t…) that holds two deliverables")
    title: str = Field(description="Title of the new task for its latest result")


# What a draft's card says it answers: the owner reads it, not the person the draft is for.
OWNER_NOTE = (
    "One line for the owner: what this answers, in the owner's language (Chinese for an owner "
    "who writes Chinese, even when the reply itself is in English)"
)


class DraftSlackReply(BaseModel):
    channel_id: str = Field(description="Slack conversation id, e.g. C0123ABC or D0123ABC")
    text: str = Field(description="The exact reply in the owner's voice")
    thread_ts: str = Field(
        default="",
        description="To reply in a thread: the ts of its first message (or of the message you "
        "answer). Empty posts to the conversation itself. A DM message that is in no thread is "
        "answered in the DM, not in a thread under it, unless in_thread",
    )
    in_thread: bool = Field(
        default=False,
        description="Only to start a thread under a DM message that has none (rarely wanted)",
    )
    to: str = Field(
        default="", description="Where it goes, for the owner, e.g. 'Alice Chen (DM)' or '#deploys'"
    )
    note: str = Field(default="", description=OWNER_NOTE)


class DraftReply(BaseModel):
    text: str = Field(description="The exact reply in the owner's voice")
    to: str = Field(description="Who it answers, e.g. 'Alice Chen'")
    where: str = Field(
        description="Where the owner sends it, e.g. 'Slack DM', '#deploys thread', 'email'"
    )
    link: str = Field(default="", description="A link to the conversation it answers")
    note: str = Field(default="", description=OWNER_NOTE)


class DistillProfile(BaseModel):
    focus: str = Field(default="", description="Optional: what to focus on or update")


class ProposeProfile(BaseModel):
    markdown: str = Field(description="The proposed owner profile (markdown)")


class FollowThread(BaseModel):
    thread: str = Field(
        description="slack:<channel_id>:<thread_ts>, slack:<D…> (a DM) or github:<owner/repo>#<n>"
    )


class CheckBack(BaseModel):
    minutes: int = Field(
        ge=5,
        le=7 * 24 * 60,
        description="When to be woken, in minutes from now (5 minutes to 7 days)",
    )
    what: str = Field(
        description="What to check then, in a line the owner reads on the task's card, e.g. "
        "'whether Flyte execution f3a9 finished'"
    )
    url: str = Field(default="", description="A link to what you will check (a run, a page)")
    task_id: str = Field(default="", description="The task it is for; empty for your current task")


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
    body: str = Field(
        description="The review's summary, or your reply to a review comment (markdown). Empty "
        "for a review of line comments only, when the owner asked for that."
    )
    comments: list[InlineComment] = Field(
        default_factory=list,
        max_length=50,
        description="Comments on specific lines of the diff, published with the review (not "
        "with in_reply_to). Put each finding about a line here rather than in the body.",
    )
    in_reply_to: int = Field(
        default=0,
        description="To reply to a review comment on a line of the PR: that comment's id (from "
        "`gh api repos/<owner/name>/pulls/<number>/comments`). 0 for a review of the whole PR.",
    )
    reply_to_comment: int = Field(
        default=0,
        description="To answer a comment in the PR's conversation that is not on a line (a "
        "top-level comment, or the summary of someone's review): its id (an issue comment from "
        "`gh api repos/<owner/name>/issues/<number>/comments`, or a review from "
        "`gh api repos/<owner/name>/pulls/<number>/reviews`). GitHub has no replies to those: "
        "it is posted in the conversation, starting with @ and its author's login.",
    )
    resolve: bool = Field(
        default=False,
        description="With in_reply_to: resolve the conversation once the reply is posted, "
        "when your reply says the point is fixed or settled (not when you ask something or "
        "leave it open). The owner can change it on the card.",
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
    r"\bmarkdown tables?\b|\bsummary table\b|\b(?:in|as) (?:a )?tabular (?:form|format)\b|"
    r"\b(?:show|give|present|format|put|summari[sz]e|list|compare|answer|reply|report|lay out|"
    r"tabulate|organi[sz]e)\b[^.\n]{0,40}\b(?:in|as|into) an? (?:\w+ )?table\b|"
    r"\b(?:give|show|make|send|want|need)\b[^.\n]{0,20}\ban? (?:\w+ )?table (?:of|summari[sz]ing|"
    r"comparing)\b|(?:用|做|列|整理|给我|以|画)[^。\uff0c,\n]{0,12}表格|表格(?:形式|展示|列出|汇总)",
    re.IGNORECASE,
)
ASKS_EXPLANATION = re.compile(
    r"\bexplain\b(?! (?:analy[sz]e|plan|query|output|select)\b)(?!ed)|step[- ]by[- ]step|"
    r"walk me through|\bhow you\b|\bexplain in detail\b|讲清楚|解释一下|解释清楚|一步一步|"
    r"详细(?:说明|解释|讲|介绍|分析)|怎么算",
    re.IGNORECASE,
)


# A report that leaves part of the answer out for its length ("too large to paste here").
CUT_FOR_LENGTH = re.compile(
    r"\btoo (?:large|long|big) to (?:paste|include|show|list|put|fit|reproduce)|"
    r"\b(?:omitted|truncated|abridged|shortened|left out|not shown|not included|cut) "
    r"(?:here )?(?:for|due to) (?:brevity|length|space)|"
    r"\b(?:would )?exceeds? (?:the |a )?(?:report(?:'s)?|message(?:'s)?|reply(?:'s)?) "
    r"(?:size|length|limit)|\bwould exceed (?:the |a )?(?:report|message)\b|\bfor brevity\b|"
    r"太长[^。\n]{0,6}(?:没|不|无法|只|仅)(?:贴|放|列|展示|给)|篇幅(?:有限|所限|原因)|"
    r"超出(?:报告|长度|篇幅)",
    re.IGNORECASE,
)
# "The full table is saved at …", "is in the file below": the answer itself left in a file.
POINTS_TO_FILE = re.compile(
    r"\b(?:full|whole|complete|entire)\b[^.\n]{0,100}?\b(?:saved|written|stored)\s+(?:at|to|in)\b|"
    r"\b(?:full|whole|complete|entire)\b[^.\n]{0,100}?\b(?:is|are)\s+in\s+(?:the\s+|a\s+)?file\b|"
    r"\b(?:saved|wrote|written|stored)\s+(?:the\s+)?(?:full|whole|complete|entire)\b|"
    r"完整[^。\n]{0,40}(?:保存|写入|存|在文件)",
    re.IGNORECASE,
)
ASKS_ALL = re.compile(
    r"\b(?:every|all)\b[^.\n]{0,40}\b(?:rows?|lines?|items?|entries|records)\b|所有|全部|每一",
    re.IGNORECASE,
)
# The owner asked for a file: one that holds the answer is what they wanted.
ASKS_FILE = re.compile(
    r"\b(?:save|write|export|dump)\b[^.\n]{0,80}\b(?:to|into)\b|保存到|写入|导出", re.IGNORECASE
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
    if (
        (ASKS_TABLE.search(goal) or ASKS_ALL.search(goal))
        and not ASKS_FILE.search(goal)
        and POINTS_TO_FILE.search(summary)
        and len(summary) < MAX_RESULT // 2
    ):
        return (
            f"the whole answer in the report itself, not only in a file (a report holds up to "
            f"{MAX_RESULT:,} characters)"
        )
    return ""


def left_out(earlier: str, report: str) -> list[str]:
    """What an earlier result is made of that a new report on the same task leaves out, when
    that is most of it: code, links, versions and names (RapidFuzz, MIT), which survive a
    translation. [] when the report keeps most of it, or there is too little to tell."""
    found: set[str] = set()
    found |= {item.lower() for item in re.findall(r"`([^`\n]{2,60})`", earlier)}
    found |= {item.lower().rstrip(").,") for item in re.findall(r"https?://\S+", earlier)}
    found |= set(re.findall(r"(?<![\w.])\d+(?:\.\d+){1,3}(?![\w.])", earlier))
    found |= {
        item.lower()
        for item in re.findall(r"\b(?:[A-Z][a-z]+[A-Z]\w*|[A-Z]{2,}\w*|[a-z]+[A-Z]\w*)\b", earlier)
    }
    said = report.lower()
    missing = sorted(item for item in found if item not in said)
    return missing if len(found) >= 6 and len(missing) * 2 > len(found) else []


def names_pr(text: str, repo: str, number: int) -> bool:
    """The text names the PR by URL or key, or by its repository's name and number (as in
    "review checkout-service PR #1529")."""
    text = text.lower()
    thread = f"github:{repo}#{number}"
    if thread in threads.pull_requests(text) or thread in {
        f"github:{name}#{n}" for name, n in threads.GITHUB_PR.findall(text)
    }:
        return True
    owner, short_name = repo.split("/", 1)
    # ASCII boundaries: \w and \b would take "的" in "checkout-service的PR" for part of a word.
    word = r"A-Za-z0-9_.\-"
    name = rf"(?<![{word}/])(?:([{word}]+)/)?{re.escape(short_name)}(?![{word}])"
    pr = r"(?<![A-Za-z0-9_])(?:pr|prs|pull requests?)(?![A-Za-z0-9_])"
    # The number as a PR: "#1529", "PR 1529", "pull/1529" (not JIRA-1529 or 15290).
    marked = rf"(?:#\s*|{pr}\s*#?\s*|/pull/|/){number}(?!\d)"
    # After the name: spaces, punctuation, Chinese ("checkout-service 的 PR #1529", "里面的 #1529"),
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
    # Before the name, only a PR's own marks: "PR #1529 (checkout-service)", "PR 1529 in
    # checkout-service"; not "web#1529 and api", whose number is web's.
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


REPORT_STATUS = {"question": "waiting", "blocked": "waiting", "done": "done", "failed": "failed"}
MAX_RESULT = 30000
# Copilot hands a tool output over 50 KB to the model as a file, which the Router cannot read.
LISTED_BYTES = 14000
# The most watches Settings holds.
MAX_WATCHES = 10


def shortened(listed: dict) -> dict:
    """list_sessions small enough to be read whole, however many sessions there are."""

    def size() -> int:
        return len(json.dumps(listed, ensure_ascii=False).encode())

    if size() <= LISTED_BYTES:
        return listed
    listed["tasks"] = [
        task
        for task in listed["tasks"]
        if task["status"] not in {"done", "cancelled"} and not task["status"].startswith("failed")
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
        listed["projects"] = [
            {key: card[key] for key in ("path", "missing") if key in card}
            for card in listed["projects"]
        ]
        listed["recently_closed"] = []
    if size() > LISTED_BYTES:
        # Each session already names its current task; needs_owner lists results to review.
        listed["tasks"] = [
            task for task in listed["tasks"] if not task["status"].startswith(("review", "done"))
        ]
    if size() > LISTED_BYTES and "needs_owner" in listed:
        for card in listed["needs_owner"]["cards"]:
            card.pop("text", None)  # What it is, to whom and what it answers stay.
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
# A Slack conversation (channel, DM or group) named in a text, also inside a thread key
# (slack:C0DEPLOYS:<ts>): its id has digits, unlike a shouted word (DEPLOYMENT, CHANGELOG).
SLACK_CONVERSATION = re.compile(r"\b[CDG](?=[A-Z0-9]*\d)[A-Z0-9]{7,}\b")
# The Router passes on the owner's answer ("the owner answered: yes", "the owner says…").
OWNER_ANSWER = re.compile(
    r"\bowner(?:'s|\u2019s)?\s+(?:\w+\s+){0,3}?(?:answer\w*|said|says|repl\w*|confirm\w*|"
    r"decid\w*|chose|choose\w*|approv\w*|agree\w*|declin\w*|accept\w*|refus\w*)\b",
    re.IGNORECASE,
)
# What the Router must not ask a worker to do on the owner's behalf: merge, delete, force-push,
# or skip the owner's card.
RISKY = re.compile(
    r"\bgh\s+pr\s+merge\b|\bgh\s+repo\s+delete\b|\bgit\s+push\s+(-f|--force)(?![\w-])"
    r"|\bno (merge )?card (is )?needed\b|\b(skip|without) the (owner's )?(merge )?card\b",
    re.IGNORECASE,
)
# "do not run gh pr merge", "never force-push": said so the worker does not.
FORBIDS = re.compile(
    r"\b(do not|don't|dont|never|not to|must not|no)\W+(\w+\W+){0,2}$", re.IGNORECASE
)


def risky(text: str) -> re.Match | None:
    """A request that a worker merge, delete or force-push, or skip the owner's card: not one
    that tells it not to."""
    for found in RISKY.finditer(text):
        if not FORBIDS.search(text[max(0, found.start() - 40) : found.start()]):
            return found
    return None


# A new task for a worker given one this recently is asked about once (see recent_task).
ADD_MINUTES = 30
# Internal ids a worker may write in its report: a draft's ("d4d31ea2f"), a Slack message's
# timestamp ("ts 1794157630.819867").
DRAFT_ID = r"d[0-9a-f]{8}"
# "(d4d31ea2f)", "(d4d31ea2f, d939522b7)": ids alone in brackets; "(d4d31ea2f, sent)"; one of
# a list after a comma; "the draft d4d31ea2f" (the id after "draft").
IDS_IN_BRACKETS = re.compile(rf"\s*\(\s*({DRAFT_ID}(?:\s*,\s*{DRAFT_ID})*)\s*\)")
ID_FIRST = re.compile(rf"\(\s*({DRAFT_ID})\s*,\s*")
ID_LISTED = re.compile(rf"\s*,\s*({DRAFT_ID})\b")
DRAFT_NAMED = re.compile(rf"\b(?:(\w+)\s+)?draft\s+({DRAFT_ID})\b", re.IGNORECASE)
# "草稿 d3c7ae145": a draft named in Chinese.
DRAFT_NAMED_ZH = re.compile(rf"(\u8349\u7a3f)\s*({DRAFT_ID})\b")
# Where a Slack message is, as a session wrote it down for a tool: "channel_id=#deploys",
# "thread_ts=1794189315.270463".
SLACK_FIELDS = re.compile(
    r"[ \t]*[,\uff0c;\uff1b]?[ \t]*\b(?:channel_id[ \t]*[=:][ \t]*#?[\w-]+|"
    r"(?:thread_ts|message_ts)[ \t]*[=:][ \t]*\d{10}\.\d{6})\b"
    # Listed after where it is: "(DM D0ALICE01, ts 1794141072.949527)".
    r"|[ \t]*[,\uff0c][ \t]*\bts[ \t]*[=:]?[ \t]*\d{10}\.\d{6}\b"
)
# A Slack timestamp named in text: on its own in parentheses ("(ts 1712345678.000100)", left
# out), else in a phrase ("message ts …", "the message"). A ")" closes it only with its "(".
TS_MENTION = re.compile(
    r"\s*\(\s*(?:ts|thread_ts|thread)\s*[=:]?\s*\d{10}\.\d{6}\s*\)|"
    r"\s*\b(?:message\s+)?(?:ts|thread_ts|thread)\s*[=:]?\s*\d{10}\.\d{6}\b"
)
NAMING = {"the", "a", "this", "that", "your", "my", "its", "slack", "review", "reply"}


def owner_words(text: str, drafts: set[str]) -> str:
    """A report as the owner reads it: without the ids of drafts (`drafts`, those that exist:
    not a commit hash that looks like one) and Slack timestamps, which mean nothing to them.
    Only where an id is: nothing else of the text changes (`main()` stays)."""

    def ours(*found: str) -> bool:
        return all(identifier in drafts for identifier in found)

    def brackets(match: re.Match) -> str:
        found = re.findall(DRAFT_ID, match.group(1))
        return "" if ours(*found) else match.group(0)

    def named(match: re.Match) -> str:
        word, found = match.group(1) or "", match.group(2)
        if not ours(found):
            return match.group(0)
        if word.lower() in NAMING:
            return f"{word} draft"  # "the draft d4d31ea2f" -> "the draft"
        return f"{word} the draft".lstrip() if word else "the draft"

    text = IDS_IN_BRACKETS.sub(brackets, text)
    text = ID_FIRST.sub(lambda m: "(" if ours(m.group(1)) else m.group(0), text)
    text = ID_LISTED.sub(lambda m: "" if ours(m.group(1)) else m.group(0), text)
    text = DRAFT_NAMED.sub(named, text)
    text = DRAFT_NAMED_ZH.sub(lambda m: m.group(1) if ours(m.group(2)) else m.group(0), text)
    text = SLACK_FIELDS.sub("", text)
    return TS_MENTION.sub(
        lambda m: "" if m.group(0).lstrip().startswith("(") else " the message", text
    )


SLACK_TS = re.compile(r"\d{10}\.\d{6}")


def same_conversation(asked: str, text: str) -> bool:
    """Whether `text` names the Slack conversation a question is about: the same channel or DM
    and, in a channel, the same thread or message when both name one."""
    shared = set(SLACK_CONVERSATION.findall(asked)) & set(SLACK_CONVERSATION.findall(text))
    if not shared:
        return False
    if all(conversation.startswith("D") for conversation in shared):
        return True  # A DM is one conversation.
    stamps, named = set(SLACK_TS.findall(asked)), set(SLACK_TS.findall(text))
    return not stamps or not named or bool(stamps & named)


def named_pulls(text: str) -> list[str]:
    """The PRs a text names, as thread keys: by URL or as github:<owner/repo>#<n>."""
    keys = threads.pull_requests(text or "")
    keys += [f"github:{repo.lower()}#{n}" for repo, n in GITHUB_KEY.findall(text or "")]
    return list(dict.fromkeys(keys))


GITHUB_KEY = re.compile(r"\bgithub:([\w.-]+/[\w.-]+)#(\d+)")

# What makes a PR a task names the task's to act on (review it, fix it, merge it, its CI…), not
# one it only mentions (notes that live in it, an example to follow).
ACTS_ON = re.compile(
    r"\b(?:review|fix|merg|land|address|rebas|resolv|approv|follow|watch|monitor|check|"
    r"look\s+(?:at|into|over)|investigat|debug|updat|finish|respond|repl|answer|comment|test|"
    r"ship|unblock|babysit|shepherd|triag|close|reopen|push|continu|take\s+over|handl|"
    r"summari[sz]|go\s+through|keep\s+an\s+eye|fail|green|conflict|feedback|notif|alert|ping|"
    r"mov(?:e|es|ing)\b|tell\s+(?:me|us)\s+(?:if|when|once)|let\s+(?:me|us)\s+know|"
    r"keep\s+(?:me|us)\s+posted)\w*"
    r"|\b(?:ci|checks?)\b|审|修|合并|看|检查|跟进|处理|回复|评论|关注|盯",
    re.IGNORECASE,
)
NOT_ACTING = re.compile(
    r"\b(?:not|don't|dont|never|without|no)\b|不要|别|不用|勿|无需", re.IGNORECASE
)
# Right before a PR: an example or a source, not what to act on ("see #60", "like #60").
ONLY_MENTIONED = re.compile(
    r"(?:\b(?:like|see|cf|per|unlike|from|similar\s+to|as\s+in)\W*|参考|参见|类似)$", re.IGNORECASE
)
# A word of the next sentence that points back at the PR ("… #5 from Bob. Review it.").
POINTS_BACK = re.compile(
    r"\b(?:it|its|this|that|them|these|those|the\s+prs?)\b|它|这个|该", re.IGNORECASE
)
QUOTED = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”|「[^」\n]*」")
SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+|\s+but\s+|[\u3002\uff01\uff1f\uff1b]")
MENTION = re.compile(rf"{threads.PR_URL.pattern}|{GITHUB_KEY.pattern}")
# Names, paths and links: "fix-tool", "cos-approve-test" say nothing about what to do.
IDENTIFIER = re.compile(
    r"https?://[!-~]+|[\w./#-]*[A-Za-z0-9][/#_.-][A-Za-z0-9][\w./#-]*", re.ASCII
)


def acting(clause: str) -> bool:
    """Whether a sentence asks to act (not "do not touch it")."""
    clause = IDENTIFIER.sub(" ", clause)
    for match in ACTS_ON.finditer(clause):
        before = re.split(r"[,:;()]", clause[: match.start()])[-1]
        if not NOT_ACTING.search(" ".join(before.split()[-3:])):
            return True
    return False


def acted_pulls(text: str, title: str = "") -> list[str]:
    """The PRs a task's text names that the task is to act on (review, fix, merge, follow, its
    CI…), as thread keys; not one it only mentions for context (a link to write into a file, an
    example: "see #60"). What a quoted text says is its own sentence. Named in its title, a PR is
    what the task is about."""
    found = named_pulls(title)
    quoted = [match.group(0)[1:-1] for match in QUOTED.finditer(text or "")]
    sentences = [
        sentence
        for part in [QUOTED.sub(" ", text or ""), *quoted]
        for sentence in SENTENCE.split(part)
        if sentence.strip()
    ]
    for index, sentence in enumerate(sentences):
        mentions = list(MENTION.finditer(sentence))
        if not mentions:
            continue
        after = sentences[index + 1] if index + 1 < len(sentences) else ""
        if not acting(sentence) and not (POINTS_BACK.search(after) and acting(after)):
            continue
        found += [
            key
            for match in mentions
            if not ONLY_MENTIONED.search(sentence[: match.start()])
            for key in named_pulls(match.group(0))
        ]
    return list(dict.fromkeys(found))


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
        # What the Router last saw of each task (list_sessions, read_session): its status and
        # result, and how it was put.
        self.looked: dict[str, tuple[tuple[str, int], str]] = {}
        # The Router's notes refused as asking a worker's open question again (shown when sent
        # again unchanged).
        self.refused_notes: set[str] = set()

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
                "tell_session",
                "Tell another worker something it needs for its work now: a value it waits for, "
                "an interface, name or file format you changed, a finding that affects its task. "
                "It gets it within its current turn; no answer is expected (ask_session asks). "
                "Information only: what it should do differently than its task says goes to the "
                "Router (report status question).",
                TellSession,
                self.runtime.peers.t_tell_session,
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
                "check_prs",
                "The state of pull requests on GitHub right now: open, merged or closed, each "
                "check by its latest run (and which fail), the review decision, mergeable. Use "
                "it to answer about PRs instead of asking a worker or going by earlier reports.",
                CheckPrs,
                self.t_check_prs,
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
                "stop_worker",
                "Stop a worker now, as the owner's Stop button does: its turn ends (the command "
                "it runs too) and the task it is on waits, stopped, for the owner's Go on or "
                "Cancel. Nothing is cancelled. For “stop”, “pause”, “hold on”.",
                StopWorker,
                self.t_stop_worker,
            ),
            (
                "stop_following",
                "Stop a worker following a Slack thread, DM or PR (the owner's “stop following "
                "…”, “ignore that thread”): new messages in a Slack thread come to you again; a "
                "PR nobody else follows is no longer read, so its news reaches no one. The "
                "worker, its tasks, results and drafts stay; closing the worker is not how to do "
                "this.",
                StopFollowing,
                self.t_stop_following,
            ),
            (
                "add_watch",
                "Propose a watch for the owner to confirm: something Chief of Staff looks out "
                "for on their behalf every few minutes in work hours (a channel, a topic, mail, "
                "comments), with the tools this machine has; what needs them comes to you as "
                'from="watch" events. Watches are listed in Settings → Connections → Watches.',
                AddWatch,
                self.t_add_watch,
            ),
            (
                "rename_task",
                "Rename a task whose title does not say what it delivers (its earlier names are "
                "kept).",
                RenameTask,
                self.t_rename_task,
            ),
            (
                "split_task",
                "A finished task whose latest result is another deliverable than its title: that "
                "result becomes a task of its own with this title; the task keeps its earlier "
                "result.",
                SplitTask,
                self.t_split_task,
            ),
            (
                "close_worker",
                "Close a worker whose task is finished or no longer needed.",
                CloseWorker,
                self.t_close_worker,
            ),
            (
                "report",
                "Report to the Router: progress (rare), question, blocked, failed or done.",
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
                "withdraw_draft",
                "Withdraw a draft of yours that still waits for the owner (the conversation "
                "moved on, it answers the wrong thing): its card leaves, with your reason. A new "
                "draft_slack_reply answering the same message replaces your earlier one by "
                "itself; one answering another message does not: withdraw the earlier one when "
                "the new reply covers it.",
                WithdrawDraft,
                self.t_withdraw_draft,
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
                "Follow a Slack thread, DM or PR: its new messages come straight to you (a PR "
                "merged or closed already: its reopening, for 14 days).",
                FollowThread,
                self.t_follow_thread,
            ),
            (
                "check_back",
                "Be woken later to check on something with no thread to follow (a run, a deploy, "
                "a scheduled job); then end your turn at once (do not do the check now, nor "
                "report it done): you are woken at that time to check it.",
                CheckBack,
                self.t_check_back,
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
                params_type=leak_proof(params),
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

    def check_title_style(self, title: str, refused: str):
        """A new task's title that ends with how to write the work (its language, tone) is
        refused once, with why: that goes in the instructions. The same title again stands."""
        note = style_note(title)
        flag = "styletitle:" + hashlib.sha256(title.encode()).hexdigest()[:16]
        if not note or self.store.get(flag):
            return
        self.store.set(flag, "1")
        bare = title[: title.rindex(note)].rstrip(" -\u2013\u2014\u00b7,;:\uff0c\uff1b\uff1a")
        raise ValueError(
            f"{refused}: the title's \u201c{note}\u201d says how to write the work, not what it "
            "delivers. Put how to write it in the instructions, and give the title without "
            f"it (\u201c{bare}\u201d). If it is part of what the task delivers, call again with "
            "the same title."
        )

    async def t_spawn_worker(
        self, params: SpawnWorker, caller: str, special: str = "", untrusted: bool = False
    ):
        """`untrusted`: a helper that reads untrusted content (the connector) runs as a worker
        that reads Slack does, whether or not Slack is connected."""
        self.caller(caller, ROUTER)
        params = params.model_copy(update={"instructions": self.owner_words(params.instructions)})
        if not special and (asked := risky(params.instructions)):
            raise ValueError(
                f"Not started: workers never do this for the owner ({asked.group(0)!r}). A merge "
                "is the owner's click on the PR's Merge card on Home: tell them where it is. For "
                "something destructive, give the owner the exact command to run themselves."
            )
        # Checked as a rename or split is: it says what the task delivers.
        title = deliverable_title(params.title)
        if not special:
            self.check_title_style(title, "Not started")
        follow = [threads.normalize(key) for key in params.follow]
        after = [self.runtime.task_actions.task_for(reference) for reference in params.after]
        replaced = self.replaced_tasks(params.replaces)
        context_from = [
            self.runtime.task_actions.task_for(reference)["id"] for reference in params.context_from
        ]
        peers, wanted = self.runtime.peers.resolve(params.works_with)
        if not special:
            # Parts of one request that work together (named or not), not "related" workers.
            known = {row["id"] for row in peers}
            peers += [
                row
                for row in self.runtime.peers.candidates(
                    title, params.instructions, bool(params.slack), wanted
                )
                if row["id"] not in known
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
                # The workers it works with are the other parts of its request, not
                # candidates for this one.
                exclude=[task["session_id"] for task in after] + [row["id"] for row in peers],
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
        if params.slack and not slack and any(key.startswith("slack:") for key in follow):
            raise ValueError(
                "Slack is not connected (Settings → Connections → Slack), so no session can "
                f"follow {', '.join(k for k in follow if k.startswith('slack:'))}. Start the "
                "worker without those threads: it reads the conversation with the tools it "
                "has (a company search such as Glean), and drafts with draft_reply."
            )
        self.runtime.check_follow({"role": WORKER, "slack": slack}, follow)
        if params.in_place and params.isolate:
            raise ValueError(
                "in_place (directly in the owner's folder) and isolate (a worktree of its own) "
                "exclude each other: pass one."
            )
        workspace, checkouts = await self.workspaces.prepare_directory(
            session_id,
            title,
            [params.project, *params.more_projects],
            params.isolate,
            carry=not slack,  # Gitignored files are often secrets; Slack content is untrusted.
        )
        if params.in_place:
            # The owner asked for it: their folder is its to change (see Workspaces.writes).
            checkouts = [{**c, "in_place": True} if c.get("shared") else c for c in checkouts]
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
        if params.review:
            self.store.set(f"reviewreq:{task['id']}", "1")
        self.link_decisions(task["id"], decisions)
        await self.replace(replaced, task)
        if not special:
            # Again now that it exists: another task started at the same time may exist now.
            self.runtime.peers.want(session_id, wanted)
            known = {row["id"] for row in peers}
            peers += [
                row
                for row in self.runtime.peers.candidates(
                    title, params.instructions, slack, wanted, new=session_id
                )
                if row["id"] not in known
            ]
        self.runtime.peers.link(session_id, [row["id"] for row in peers])
        if not special:
            self.states[caller].turn_tasks.append(task["id"])
        if special:  # Its own tools: see Runtime.tool_names.
            self.store.set(f"{special}:{session_id}", "1")
        # A PR the task is to act on is followed: its reviews, replies and CI come to the worker
        # (not one its instructions only mention: news there is not this task's).
        self.runtime.follow(
            session_id,
            list(dict.fromkeys(follow + acted_pulls(params.instructions, title))),
            task_id=task["id"],
            asked=follow,
        )
        spoken, note = self.report_language(caller, params.instructions)
        prompt = wrap(
            "router",
            "task",
            task_text(params.instructions, criteria, decisions),
            title=title,
            task=task["id"],
            # The owner reads its report as written: in the language of the request it serves.
            reply_in=spoken,
        )
        if context_from:
            prompt += "\n\n" + wrap("system", "context", self.tasks.briefing(context_from))
        if introduction := self.runtime.peers.introduction(session_id):
            prompt += "\n\n" + wrap("system", "peers", introduction)
        if note:
            prompt += "\n\n" + wrap("system", "context", note)
        meta: dict[str, Any] = {"prompt": prompt, "task_id": task["id"]}
        if images := self.runtime.attachments_in(params.instructions):
            meta["attachments"] = images  # The owner's images it names come with it.
        self.runtime.add_message(session_id, "router", params.instructions, meta)
        if not special:  # Chief of Staff's own helpers come and go quietly.
            # Not "Started" for work that waits: for the tasks it comes after, or for a slot.
            waits = [dep["title"] for dep in self.tasks.unmet(task)]
            limit = self.store.settings().max_workers
            if waits:
                said = f"Queued “{title}” · after " + ", ".join(f"“{t}”" for t in waits[:2])
            elif self.runtime.workers.running() >= limit:
                said = f"Queued “{title}” · waits for a worker slot ({limit} busy)"
            else:
                said = f"Started “{title}”"
            notice = said + self.new_because(why_new)
            self.runtime.add_message(
                caller, "notice", notice, {"session_id": session_id, "task_id": task["id"]}
            )
        if peers:
            try:
                await self.runtime.peers.introduce(self.store.session(session_id), peers)
            except Exception:
                logger.warning("Could not tell %s's peers about it.", session_id, exc_info=True)
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
        elif row["status"] == "queued" and row["activity"].startswith("waiting for memory"):
            result["note"] = (
                f"Queued: this computer is low on memory ({row['activity'].split(': ', 1)[-1]});"
                " it starts once memory frees up."
            )
        elif row["status"] == "queued":
            result["note"] = "Queued until a worker slot frees up."
        return result

    async def follow_own_pulls(self, caller: str, text: str, task: dict | None):
        """Follow the PRs a report names that are this worker's own (see TaskActions.pr_roles):
        on its branch, or opened by the owner's account since its task began and first brought
        up by it. Not another worker's PR it mentions."""
        followed = set(self.store.follows(caller))
        row = self.store.session(caller)
        about = task or {
            "id": "", "session_id": caller, "created_at": row["created_at"], "title": "", "goal": ""
        }  # fmt: skip
        own = []
        for key in threads.pull_requests(text):
            if key in followed:
                continue
            repo, number = key.removeprefix("github:").rsplit("#", 1)
            try:
                pull = await self.runtime.github.pull(repo, int(number))
            except Exception:
                # GitHub can't say whose it is: only one it opened itself.
                if task and key.removeprefix("github:") in self.runtime.task_actions.opened_prs(
                    task["id"]
                ):
                    own.append(key)
                continue
            label = f"{repo}#{number}"
            if pull.get("title"):
                self.runtime.remember_pr_title(label, pull["title"])
            seen = {
                "label": label,
                "author": ((pull.get("user") or {}).get("login") or "").lower(),
                "created": pull.get("created_at") or "",
                "branch": (pull.get("head") or {}).get("ref") or "",
            }
            if (await self.runtime.task_actions.pr_roles(about, [seen]))[0]:
                own.append(key)
        if own:
            own = self.runtime.follow(
                caller, own, given=False, task_id=task["id"] if task else None
            )
        followed |= set(own)
        delivered = [key for key in threads.pull_requests(text) if key in followed]
        task = self.tasks.find(task["id"]) if task else None
        if task and task["status"] == "running" and delivered:
            # Its PR while it is still at it: on its card with GitHub's state, refreshed every
            # few minutes, and offered to merge once it is ready.
            self.runtime.task_actions.remember_prs(
                task["id"],
                " ".join(
                    "https://github.com/" + key.removeprefix("github:").replace("#", "/pull/")
                    for key in delivered
                ),
            )
            await self.runtime.task_actions.collect_evidence(task["id"])

    def failed_tests(self, task: dict) -> str:
        """The task's last test run, when it failed: "`command` exited 1" ("" otherwise)."""
        found = (self.runtime.task_actions.receipts(task) or {}).get("tests") or {}
        if found.get("passed") is not False or found.get("running") or found.get("ci_passing"):
            return ""  # Passed, running again, or the PR's checks passed after it.
        if found.get("no_tests"):
            return ""  # No failure to fix: its card says no test ran.
        command = " ".join(found.get("command", "").split())[:160]
        return f"`{command}` exited {found.get('exit_code')}"

    def told_line(self, done: str, target: dict, task: dict | None) -> str:
        """'Told “Fix login”', or, for a task the worker is not on now, 'Told the worker of
        “Add tests” about “Fix login”' (its name is that of its current work)."""
        name = self.runtime.display_title(target)
        if task and task["title"] != name:
            if done == "Told":
                return f"Told the worker of “{name}” about “{task['title']}”"
            return f"Continued “{task['title']}” in the worker of “{name}”"
        return f"{done} “{name}”"

    def new_because(self, why_new: str) -> str:
        """ " · new because …" for a notice ("" without a reason). Not an English reason in the
        middle of the line of an owner who writes Chinese, Japanese or Korean: one in their
        language was asked for."""
        if not why_new or (
            self.store.get("owner_language") in {"Chinese", "Japanese", "Korean"}
            and not CJK_TEXT.search(why_new)
        ):
            return ""
        # A reason may start with a task's title (“The Long Count is busy”): it keeps its capital.
        names = [
            row["title"]
            for row in self.store.all("SELECT title FROM tasks ORDER BY updated_at DESC LIMIT 200")
        ]
        return f" · new because {mid_sentence(why_new, names)}"

    def report_language(self, caller: str, instructions: str) -> tuple[str, str]:
        """The language a new task reports in, and a note when its instructions are in another:
        that of the request it serves. The owner's (as they write to the Router) in a turn with
        their message; in a turn without one (a Slack message, a PR's news), that of its
        instructions, which hand that request on; the owner's when they say nothing."""
        spoken = self.store.get("owner_language") or ""
        written = language(instructions)
        if written and self.states[caller].event_turn:
            return written, ""
        if not spoken or not written or written == spoken:
            return spoken, ""
        return spoken, (
            f"Write your report in {spoken}: the owner reads it, and wrote to us in {spoken}."
        )

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

    def answer_slack_question(self, session_id: str, text: str, quoted: bool):
        """The Router passes the owner's answer on to the Slack session (it says so, or
        `quoted` the owner's message): its questions about the conversation the message names
        are no longer open. Not by any other message: the Router forwards it Slack events too,
        which answer nothing."""
        if not (quoted or OWNER_ANSWER.search(text)):
            return
        for question in self.runtime.asks.session_questions(session_id):
            if same_conversation(question["text"], text):
                self.runtime.asks.dismiss(question["id"], how="")

    async def t_message_session(self, params: MessageSession, caller: str):
        self.caller(caller, ROUTER)
        quoted = bool(OWNER_REF.search(params.text))  # The owner's own words, passed on.
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
        task = self.tasks.current(target["id"]) if target["role"] == WORKER else None
        if params.task_id and params.task != "new":
            named = self.tasks.find(params.task_id.strip())
            if not named or named["session_id"] != target["id"]:
                raise ValueError(f"That worker has no task {params.task_id}.")
            task = named
        elif target["role"] == WORKER and params.task == "continue":
            open_ = [t for t in self.tasks.for_session(target["id"]) if t["status"] in OPEN]
            if len(open_) > 1:
                listed = "; ".join(f"{t['id']} “{t['title']}” ({t['status']})" for t in open_)
                raise ValueError(
                    f"That worker has {len(open_)} open tasks ({listed}): give task_id, or "
                    "task='new' for another subject."
                )
        if task and params.task == "continue" and acknowledgement(params.text):
            # “Thanks, received.”: nothing more to do on it, so nothing reopens.
            params = params.model_copy(update={"task": "note"})
        if (
            task
            and params.task == "continue"
            and (stale := self.changed_since_looked(caller, task))
        ):
            raise ValueError(stale)
        # Checked before anything changes (a closed worker is not reopened for nothing).
        if task and params.task == "continue" and task["status"] == "cancelled":
            raise ValueError(
                f"Its task {task['id']} was cancelled. For new work in this worker use "
                "task='new' with a title; to only tell it something, task='note'."
            )
        if task and params.task == "continue" and task["status"] == "done":
            raise ValueError(
                f"Its task {task['id']} “{task['title']}” was accepted by the owner and is done. "
                "More work, or another subject: task='new' with a title. Only telling it "
                "something (an answer, an acknowledgement): task='note'."
            )
        if (
            task
            and params.task == "continue"
            and task["status"] in {"review", "failed"}
            and params.task_id.strip() != task["id"]
        ):
            # Reopening finished work is a choice: the same deliverable, named by its id.
            raise ValueError(
                f"Its task {task['id']} “{task['title']}” is finished ({task['status']}). More "
                f"of the same deliverable (a correction, a missing piece): call again with "
                f"task_id='{task['id']}'. Another deliverable or subject (another PR, question "
                "or flow): task='new' with a title saying what it delivers. Only telling it "
                "something: task='note'."
            )
        if params.task == "new" and not params.title.strip():
            raise ValueError(
                "A new task needs a title saying what it delivers: a verb, the object and where "
                "(repo#PR, flow), e.g. 'Fix the Spark 3.5 dependency · repo#594'."
            )
        if params.replaces and params.task != "new":
            raise ValueError(
                "replaces goes with task='new'. To try a failed task again in its own worker: "
                "task='continue' with its task_id."
            )
        if target["role"] == WORKER and (asked := risky(params.text)):
            raise ValueError(
                f"Not sent: workers never do this for the owner ({asked.group(0)!r}). A merge is "
                "the owner's click on the PR's Merge card on Home (it checks nothing changed "
                "since they looked): tell them where it is. For something destructive, give the "
                "owner the exact command to run themselves."
            )
        why_new = " ".join(params.why_new.split())[:200]
        if (
            params.task == "new"
            and target["role"] == WORKER
            and not why_new
            and (recent := self.recent_task(target["id"]))
        ):
            # "Oh wait, one more thing: accents too" is more of the task just given, not a new
            # one (two results to accept, two replies to the asker): the Router, which read it,
            # says why it is new. Asked once.
            self.store.set(f"newasked:{recent['id']}", "1")
            raise ValueError(
                f"That worker was given {recent['id']} “{recent['title']}” minutes ago. If this "
                "adds to it (a correction, one more thing), call again with task='continue' and "
                f"task_id='{recent['id']}'. If it truly is another deliverable, call again with "
                "task='new' and why_new saying why (the owner sees it)."
            )
        replaced = self.replaced_tasks(params.replaces)
        # A running task left behind by a new one would show as waiting its turn though it was
        # delivered or waits on the owner: the Router says what becomes of it.
        earlier = (
            [t for t in self.tasks.for_session(target["id"]) if t["status"] == "running"]
            if target["role"] == WORKER and params.task == "new"
            else []
        )
        if earlier and not params.earlier:
            listed = "; ".join(f"{t['id']} “{t['title']}”" for t in earlier)
            raise ValueError(
                f"That worker still has a running task ({listed}). Say what becomes of it with "
                "earlier=: 'finish' when what it delivers is delivered or it waits on the owner "
                "(its worker reports it first: done, or blocked/question), 'keep' when it goes "
                "on after the new one, 'superseded' when the new task replaces it (it is "
                "cancelled)."
            )
        # Checked before anything changes: a closed worker is not reopened for a refused title.
        title = deliverable_title(params.title) if params.task == "new" else ""
        if title:
            self.check_title_style(title, "Not sent")
        if target["closed_at"]:
            await self.runtime.workers.reopen(
                target["id"]
            )  # A follow-up on an earlier task: same worker.
            target = self.store.session(target["id"])
        if target["role"] == WORKER and params.task in {"continue", "note"}:
            # The Router answers or redirects the question of the task it writes about (a new
            # task does not answer it; another task's question stays).
            self.runtime.asks.answered(target["id"], task["id"] if task else "", by="router")
            self.runtime.asks.answered(target["id"], "", by="router")
        elif target["role"] == SLACK:
            self.answer_slack_question(target["id"], params.text, quoted)
        if (
            task
            and params.task == "continue"
            and task["status"] == "queued"
            and self.tasks.deps(task["id"])
        ):
            self.store.set(f"released:{task['id']}", "1")  # The Router decides to go ahead.
            self.runtime.background(self.runtime.workers.pump())
        pulls = acted_pulls(params.text, params.title) if target["role"] == WORKER else []
        to_follow = list(dict.fromkeys(params.follow + pulls))
        if target["role"] != WORKER:
            self.runtime.follow(target["id"], to_follow, asked=params.follow)
            await self.delivery.deliver(
                target["id"],
                wrap("router", "message", params.text),
                kind="router",
                display=params.text,
            )
            return "Delivered."
        # The worker as the owner knows it now, before a new task renames it.
        worker_name = self.runtime.display_title(target)
        if params.task == "new":
            criteria = clean_criteria(params.done_when)
            decisions = self.with_turn_decisions(caller, params.decisions)
            task = self.tasks.create(
                target["id"],
                title,
                params.text[:4000],
                status="running",
                why_new=why_new,
                criteria=criteria,
            )
            if params.review:
                self.store.set(f"reviewreq:{task['id']}", "1")
            self.link_decisions(task["id"], decisions)
            await self.replace(replaced, task)
            self.states[caller].turn_tasks.append(task["id"])
            spoken, note = self.report_language(caller, params.text)
            prompt = wrap(
                "router",
                "task",
                task_text(params.text, criteria, decisions),
                title=title,
                task=task["id"],
                reply_in=spoken,
            )
            if note:
                prompt += "\n\n" + wrap("system", "context", note)
            prompt += "".join(
                "\n\n" + wrap("system", "context", text)
                for text in await self.settle_earlier(earlier, params.earlier, task)
            )
            notice = f"New task “{title}” for the worker of “{worker_name}”" + self.new_because(
                why_new
            )
        elif params.task == "note":
            prompt = wrap(
                "router",
                "note",
                params.text,
                task=task["id"] if task else None,
                reply_in=self.store.get("owner_language"),
            )
            notice = self.told_line("Told", target, task)
        else:
            # More on a result it reported: its next report replaces that result.
            restate = bool(task and task["status"] in {"review", "done"} and task["result"])
            if task and task["status"] in {"review", "failed"}:
                # Reopened for a follow-up: cancelling that follow-up gives the finished result
                # back, rather than losing it.
                self.store.set(f"reopened:{task['id']}", task["status"])
                task = self.tasks.move(task["id"], "running", reason="follow-up")
            prompt = wrap(
                "router",
                "message",
                params.text,
                task=task["id"] if task else None,
                reply_in=self.store.get("owner_language"),
            )
            if restate and task:
                text = prompts.restate(self.tasks.criteria(task))
                prompt += "\n\n" + wrap("system", "context", text)
                self.store.set(f"morefor:{task['id']}", "1")  # Its next report is checked.
            notice = self.told_line("Continued in", target, task)
        # Followed for the task this is about (a new one, or the one it continues).
        self.runtime.follow(
            target["id"], to_follow, task_id=task["id"] if task else "", asked=params.follow
        )
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
            steer=params.task in {"continue", "note"},
        )
        self.runtime.add_message(
            caller,
            "notice",
            notice,
            {"session_id": target["id"], **({"task_id": task["id"]} if task else {})},
        )
        return f"Delivered (task {task['id']})." if task else "Delivered."

    async def settle_earlier(self, earlier: list[dict], how: str, new: dict) -> list[str]:
        """What a worker is told about its running tasks when it gets a new one: report them
        first, go back to them after, or nothing (they are cancelled: superseded)."""
        notes = []
        for task in earlier:
            named = f"task {task['id']} “{task['title']}”"
            if how == "superseded":
                with suppress(ValueError):
                    await self.runtime.task_actions.cancel(
                        task["id"], f"Superseded by “{new['title']}”.", by="router"
                    )
            elif how == "finish":
                notes.append(
                    f"Before you start this, report {named}: done if what it delivers is "
                    "delivered; blocked or question with exactly what you need if it waits on "
                    "the owner (a key, access, a decision); check_back if it waits on something "
                    "outside. Then start this task."
                )
            else:
                notes.append(
                    f"{named[0].upper()}{named[1:]} stays open: when this one is reported, go on "
                    "with it (or report it)."
                )
        return notes

    def replaced_tasks(self, references: list[str]) -> list[dict]:
        """The tasks a new one does instead (checked before anything changes)."""
        found = []
        for reference in references:
            task = self.tasks.find(reference.strip())
            if not task:
                raise ValueError(f"No task {reference} to replace; list_sessions lists them.")
            if task["status"] not in {"failed", *OPEN}:
                raise ValueError(
                    f"Task {task['id']} is {task['status']}: replaces is for a failed task (or "
                    "one still open) that the new task does instead."
                )
            found.append(task)
        return found

    async def replace(self, old: list[dict], new: dict):
        """Tasks a new one does instead are over: a failed one is not offered for a retry."""
        why = f"Replaced by {new['id']} “{new['title']}”."
        for task in old:
            task = self.tasks.get(task["id"])
            if task["status"] in OPEN:
                with suppress(ValueError):
                    await self.runtime.task_actions.cancel(task["id"], why, by="the Router")
                continue
            if task["status"] != "failed":
                continue
            self.tasks.move(task["id"], "cancelled", progress=why)
            row = self.store.find_session(task["session_id"])
            if (
                row
                and row["status"] == "failed"
                and not any(
                    t["status"] in {"failed", *OPEN} for t in self.tasks.for_session(row["id"])
                )
            ):
                self.runtime.update_session(row["id"], status="done")  # Nothing of it is left.

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
        task = self.tasks.current(row["id"])
        brief = self.task_brief(task, router=True)
        if task and brief:
            self.looked_at(task, brief.get("stands") or task["status"])
            # Its PRs as GitHub has them now, not as its report put them.
            with suppress(Exception):
                await asyncio.wait_for(self.runtime.task_actions.refresh_prs(task["id"]), 30)
            if prs := (self.runtime.task_actions.receipts(task) or {}).get("prs"):
                brief["prs_now"] = [
                    {k: pr.get(k) for k in ("label", "state", "checks", "failing", "review")}
                    for pr in prs
                ]
        return {
            "session_id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "task": brief,
            "now": self.runtime.progress_line(row) or row["status"],
            "open_in_terminal": self.runtime.copilot_id(row["id"]) in self.runtime.elsewhere,
            "latest": entries[-params.items :],
        }

    async def t_check_prs(self, params: CheckPrs, caller: str):
        self.caller(caller, ROUTER)
        found = []
        for key in dict.fromkeys(named_pulls(" ".join(params.prs))):
            repo, number = key.removeprefix("github:").rsplit("#", 1)
            try:
                view = json.loads(
                    await self.runtime.github.gh(
                        "pr", "view", f"https://github.com/{repo}/pull/{number}", "--json",
                        "title,url,state,isDraft,statusCheckRollup,reviewDecision,mergeable,"
                        "updatedAt",
                    )
                )  # fmt: skip
            except Exception as exc:
                found.append({"pr": f"{repo}#{number}", "error": str(exc)[-300:]})
                continue
            rollup = view.get("statusCheckRollup") or []
            found.append(
                {
                    "pr": f"{repo}#{number}",
                    "title": view.get("title", ""),
                    "url": view.get("url", ""),
                    "state": "DRAFT" if view.get("isDraft") else view.get("state", ""),
                    "checks": receipts.checks_state(rollup),
                    "failing": receipts.failing_checks(rollup),
                    "review": view.get("reviewDecision") or "none yet",
                    "mergeable": view.get("mergeable", ""),
                    "updated": view.get("updatedAt", ""),
                }
            )
            # Task cards with this PR show it as it is now too.
            self.runtime.background(self.runtime.task_actions.refresh_open_prs(f"{repo}#{number}"))
        if not found:
            raise ValueError("Name each PR by its URL or as github:<owner/repo>#<n>.")
        return {"checked_at": now(), "prs": found}

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
        found = needs(self.runtime)
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
                    # As Home shows it: "review, to review (needs you)", "review, report…"
                    "status": self.looked_at(task, task_words(task, found)),
                    "worker": short(task["session_id"]),
                    **({"prs": prs} if (prs := self.runtime.task_actions.pr_urls(task)) else {}),
                    **(
                        {"after": after}
                        if (after := [d["id"] for d in self.tasks.deps(task["id"])])
                        else {}
                    ),
                }
                for task in self.tasks.listed()
            ],
            # What waits for the owner now, item by item as Home's Needs you lists it: their
            # cards (what each is, to whom, what it answers, for which task), results to
            # review, the questions workers asked them, your notes they have not seen.
            "needs_owner": self.needs_owner(found),
            "projects": await self.workspaces.project_cards(),
            "models": models,
            "default_worker_model": settings.role_models.get(WORKER) or settings.model or "auto",
            "slack_connected": self.runtime.slack.configured(),
            "github_repos": settings.github_repos if settings.github_enabled else [],
            # What Chief of Staff looks out for on the owner's behalf (add_watch proposes more).
            "watches": [watch.what for watch in settings.watches if watch.enabled],
            "recently_closed": [
                self.runtime.workers.card(row)
                for row in self.runtime.workers.recently_closed(8)
                if row["id"] not in helpers
            ],
        }
        return shortened(listed)

    def needs_owner(self, found: Needs | None = None) -> dict:
        """What waits for the owner, item by item as Home's Needs you lists it (digest.needs),
        for the Router: `count` is Home's count and each item is in it once (a result reviewed
        on its draft's card is that card)."""
        found = found or needs(self.runtime)

        def named(task: dict) -> str:
            return f"{task['id']} “{task['title']}”"

        def worker(session_id: str) -> str:
            row = self.store.find_session(session_id)
            return self.runtime.display_title(row) if row else ""

        cards = []
        for item in found.of("card"):
            card = self.runtime.drafts.brief(item["draft"])
            if task := item.get("task"):
                card["result_to_review"] = f"{named(task)}: its result is reviewed on this card"
            cards.append(card)

        def asked(question: dict | None) -> dict:
            """Why it asks, the choices it gives, what it does next: as the owner sees them."""
            question = question or {}
            return {key: question[key] for key in ("why", "options", "next") if question.get(key)}

        questions = [
            {
                "worker": question["worker"],
                "question": " ".join(question["text"].split())[:300],
                **(
                    {"task": f"{question['task_id']} “{question['task_title']}”"}
                    if question["task_id"]
                    else {}
                ),
                **asked(question),
            }
            for question in (item["question"] for item in found.of("question"))
        ]
        tasks = []
        for item in found.of("task"):
            task = item["task"]
            if item["marker"] in {"question", "stopped"}:
                questions.append(
                    {
                        "worker": worker(task["session_id"]),
                        "question": "Stopped: waits for the owner's Go on (or Cancel)."
                        if item["marker"] == "stopped"
                        else " ".join((task["progress"] or "").split())[:300],
                        "task": named(task),
                        **(
                            asked(self.runtime.task_actions.open_question(task))
                            if item["marker"] == "question"
                            else {}
                        ),
                    }
                )
                continue
            entry = {
                "task": named(task),
                "worker": worker(task["session_id"]),
                "needs": NEEDS_WORDS[item["marker"]],
            }
            if item["marker"] in {"failed", "blocked", "update"} and task["progress"]:
                entry["why"] = " ".join(task["progress"].split())[:200]
            tasks.append(entry)
        failed = found.tasks("failed")
        summary = waiting_parts(found) + ([f"{len(failed)} failed"] if failed else [])
        return {
            "count": len(found.items),
            "summary": " · ".join(summary) or "nothing",
            "cards": cards,
            "sends_to_check": [
                {**self.runtime.drafts.brief(item["draft"]), "outcome": item["draft"]["outcome"]}
                for item in found.of("send")
            ],
            "tasks": tasks,
            "questions": questions,
            "your_notes": [
                {"id": item["note"]["id"], "text": item["note"]["text"][:300]}
                for item in found.of("note")
            ],
        }

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
        if peers := self.runtime.peers.of(row["id"]):
            entry["works_with"] = [short(peer) for peer in peers]
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
            if since := self.drafts_since_report(row):
                # Its report may say a draft "awaits approval" that was sent since.
                entry["drafts_since_report"] = since
        working = row["role"] == WORKER and row["status"] == "working"
        if working and (now := self.runtime.progress_line(row, brief=True)):
            entry["now"] = now
        return entry

    def drafts_since_report(self, row: dict) -> list[str]:
        """What became of its replies and reviews since its last report: "sent", "rejected"…"""
        reported = self.store.value(
            "SELECT max(created_at) FROM messages WHERE session_id=? AND kind='report'",
            (row["id"],),
        )
        if not reported:
            return []
        found = []
        for draft in self.store.all(
            "SELECT * FROM drafts WHERE session_id=? AND kind IN ('slack', 'github', 'reply') "
            "AND resolved_at>=? ORDER BY resolved_at DESC LIMIT 5",
            (row["id"], reported),
        ):
            draft = self.store.draft(draft["id"])
            outcome = {"sent": "sent", "rejected": "not sent"}.get(draft["status"], draft["status"])
            words = (draft.get("outcome") or "").split(":", 1)[0][:60]
            found.append(
                f"{self.runtime.drafts.line(draft)}: {outcome}" + (f" ({words})" if words else "")
            )
        return found

    def looked_at(self, task: dict, words: str) -> str:
        """The Router sees a task as it is now (`words`, as it was put to it)."""
        self.looked[task["id"]] = ((task["status"], hash(task["result"] or "")), words)
        return words

    def changed_since_looked(self, caller: str, task: dict) -> str:
        """Why acting on a task now would be acting on old news: its status or result changed
        since the Router last looked, by what it has not heard of yet (the owner retried it, its
        worker reported). "" when it did not, or the Router changed it itself. Said once: the
        Router knows now."""
        seen = self.looked.get(task["id"])
        if not seen or seen[0] == (task["status"], hash(task["result"] or "")):
            return ""
        mark = int(self.store.get(f"sent:{caller}") or 0)
        news = []
        for message in self.store.messages(caller, after=mark):
            meta = message["meta"]
            if "prompt" not in meta or meta.get("task_id") not in {None, "", task["id"]}:
                continue
            if task["session_id"] not in {meta.get("to"), meta.get("from"), meta.get("session_id")}:
                continue
            text = " ".join(message["text"].split())[:200]
            if message["kind"] == "owner":
                news.append(f"the owner told its worker: “{text}”")
            elif message["kind"] == "inbound" and meta.get("status"):
                news.append(f"its worker reported ({meta['status']}): “{text}”")
            else:
                news.append(text)
        if not news:
            return ""
        now_words = self.runtime.task_actions.status_words(task)
        self.looked_at(task, now_words)
        return (
            f"Not done: task {task['id']} “{task['title']}” changed since you looked (it was "
            f"{seen[1]}; it is {now_words} now). Since then, not in your messages yet: "
            + "; ".join(news[-3:])
            + ". Look again (read_session) before you act on it; if it still needs this, call "
            "again."
        )

    def task_brief(self, task: dict | None, router: bool = False) -> dict | None:
        if not task:
            return None
        brief = {"task_id": task["id"], "title": task["title"], "status": task["status"]}
        if router and (words := self.runtime.task_actions.status_words(task)) != task["status"]:
            brief["stands"] = words  # A finished one: to accept, or a report only.
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

    def asked_already(self, caller: str, text: str) -> list[dict]:
        """The workers' questions under the owner's Needs you that a note of the Router's is
        about: those it names (by their task's or worker's id or title); naming none, nor any
        other task or worker, those whose reports were all this turn brought."""
        pending, theirs = [], set()
        for key in self.store.keys("ownerq:"):
            question = json.loads(self.store.get(key) or "null")
            row = self.store.find_session(key.removeprefix("ownerq:").split(":", 1)[0])
            if not question or not row:
                continue
            task = self.tasks.find(question.get("task_id") or "")
            if task and task["status"] == "waiting" and self.store.get(f"stopped:{task['id']}"):
                continue
            pending.append(
                {
                    "id": key.removeprefix("ownerq:"),
                    "session_id": row["id"],
                    "task_id": task["id"] if task else "",
                    "worker": self.runtime.display_title(row),
                    "task": f"{task['id']} “{task['title']}”" if task else "",
                    "text": question.get("text") or "",
                    "names": [short(row["id"]), self.runtime.display_title(row), row["title"]]
                    + ([task["id"], task["title"]] if task else []),
                }
            )
            theirs |= {row["id"], task["id"] if task else ""}
        if not pending:
            return []
        said = text.lower()

        def named(names: list[str]) -> bool:
            # Short names ("Fix") would be found in any text.
            return any(len(name) >= 6 and name.lower() in said for name in names)

        if found := [question for question in pending if named(question["names"])]:
            return found
        others = [
            [task["id"], task["title"]]
            for task in self.tasks.listed()
            if task["id"] not in theirs and task["session_id"] not in theirs
        ] + [
            [short(row["id"]), self.runtime.display_title(row)]
            for row in self.store.open_sessions(WORKER)
            if row["id"] not in theirs
        ]
        if any(named(names) for names in others):
            return []
        sent = int(self.store.get(f"sent:{caller}") or 0)
        batch = [
            message["meta"]
            for message in self.store.messages(
                caller, after=int(self.store.get(f"prevsent:{caller}") or 0)
            )
            if message["id"] <= sent and message["kind"] in {"inbound", "owner"}
        ]
        if any(meta.get("status") not in {"question", "blocked"} for meta in batch):
            return []  # The note may be about anything else this turn brought.
        reported = {(meta.get("from"), meta.get("task_id") or "") for meta in batch}
        return [q for q in pending if (q["session_id"], q["task_id"]) in reported]

    async def t_notify_owner(self, params: NotifyOwner, caller: str):
        self.caller(caller, ROUTER)
        if self.states[caller].owner_reply and not params.urgent:
            # One message per answer: the owner reads the reply of this turn (a decision they
            # must make too: said there, it is not said twice).
            return "Not sent: the owner wrote to you, so say this in your reply instead."
        asked = self.asked_already(caller, params.text) if params.needs_action else []
        if asked and params.text in self.refused_notes:
            asked = []  # Sent again unchanged: about something else.
        self.refused_notes.discard(params.text)
        if asked and not params.urgent:
            # Asked once: a note about something else is shown when it is sent again.
            self.refused_notes.add(params.text)
            listed = "; ".join(
                f"“{question['worker']}”"
                + (f" on task {question['task']}" if question["task"] else "")
                + f": “{' '.join(question['text'].split())[:160]}”"
                for question in asked
            )
            return (
                "Not shown: the owner has this under Needs you already, as the worker's own "
                f"question ({listed}). Do not ask it again: you hear when they answer it. If you "
                "can answer it from what the owner said, message_session(task='note'). If your "
                "note is about something else, call notify_owner again with it unchanged."
            )
        meta: dict[str, Any] = {"needs_action": params.needs_action, "urgent": params.urgent}
        if asked:
            meta["questions"] = [question["id"] for question in asked]  # Gone once answered.
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
        await self.runtime.workers.close(row["id"], by="the Router")
        return "Closed."

    async def t_report(self, params: Report, caller: str):
        row = self.caller(caller, WORKER, SLACK)
        task = (
            self.runtime.task_actions.report_task(caller, params.task_id)
            if row["role"] == WORKER
            else None
        )
        split_note = ""
        if task and params.status == "done" and task["status"] != "cancelled":
            # Never the result of a task it is not: work apart from it (the worker says so, or
            # it is on another branch than the task's open PR) is a task of its own, also while
            # the task is still at work (asked for in its chat).
            title = params.new_task_title.strip()
            off = (
                await self.runtime.task_actions.off_branch(task)
                if task["result"] and not title
                else None
            )
            if off and not self.store.get(f"offbranch:{task['id']}"):
                self.store.set(f"offbranch:{task['id']}", "1")
                label, branch, current = off
                raise ValueError(
                    f"Not reported: task {task['id']}'s PR {label} is on branch {branch}, and "
                    f"this work is on {current}: another deliverable, so it becomes a task of "
                    f"its own and {task['id']} keeps its result. Call report again with "
                    "new_task_title (what this work delivers). Work for "
                    f"{label} belongs on {branch}."
                )
            if title or off:
                made = self.runtime.task_actions.split_off(
                    task, title or f"Work on branch {off[2] if off else ''}"
                )
                kept = (
                    "keeps its result"
                    if task["result"]
                    else "goes on: report it by itself when it is done"
                )
                split_note = (
                    f" Filed as task {made['id']} \u201c{made['title']}\u201d, a task of its "
                    f"own; {task['id']} {kept}."
                )
                task = made
        if task and params.artifacts:
            # What they are and where they stand is a fact, whatever becomes of the report.
            self.runtime.task_actions.declare_artifacts(
                task["id"], [item.model_dump() for item in params.artifacts]
            )
        if task:
            self.runtime.task_actions.reported_links(task["id"], params.summary)
        if (
            task
            and params.status == "done"
            and task["id"] in self.states[caller].turn_check_backs
            and not self.store.get(f"checkback_done:{task['id']}")
        ):
            # It said it looks again later, and reports done in the same turn: what it waits
            # for cannot have been checked yet. Asked once.
            self.store.set(f"checkback_done:{task['id']}", "1")
            found = self.runtime.task_actions.check_back(task) or {}
            raise ValueError(
                f"Not reported: you set a check-back on this task in this turn ("
                f"{found.get('what', 'what you wait for')}), so it has not been checked yet. End "
                "your turn now: you are woken then, and report what you find. If it really is "
                "finished already, call report again."
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
        finished = bool(
            task
            and (
                task["status"] in {"review", "done"}
                or (task["status"] == "waiting" and self.store.get(f"askedafter:{task['id']}"))
            )
        )
        chat = bool(task and self.store.get(f"followup:{task['id']}") == "chat")
        own = [
            pr
            for pr in (self.runtime.task_actions.previous_prs(task["id"]) if task else [])
            if pr.get("own")
        ]
        # Its PRs merged or closed: what it does in its checkout now (a pull) is not more work.
        landed = bool(own) and all(pr.get("state") in {"MERGED", "CLOSED"} for pr in own)
        # Finished work taken up again (the owner's chat or answer, its PR's review comments):
        # what it changed for it since is a new result to review, not news on the old one (a
        # question about it too: once answered, its next report is that new result).
        changed = bool(
            task
            and finished
            and params.status in {"progress", "done", "question", "blocked"}
            and (chat or (self.runtime.turn_task(caller) == task["id"] and not landed))
            and await self.runtime.task_actions.changed_since_result(task)
        )
        if (
            task
            and changed
            and params.status == "progress"
            and not self.store.get(f"newresult:{task['id']}")
        ):
            # Asked once: it may still be at it.
            self.store.set(f"newresult:{task['id']}", "1")
            raise ValueError(
                "Not reported yet: you changed this task's code since its result, so this is a "
                "new result for the owner to review, not news on the old one. Once it is "
                'finished, call report(status="done") with the whole result, updated (what still '
                "holds from before, what changed, what is new). If you are still at it, call "
                "report again unchanged."
            )
        more = self.store.get(f"morefor:{task['id']}") if task else None
        if (
            task
            and params.status == "done"
            and task["result"]
            and (more or changed)
            and (changed or not (chat and finished))  # News on it: its result stays.
        ):
            # This report replaces the result it reported (more was asked of it, or it changed
            # its code since). Asked once, when it leaves out most of what the earlier one said
            # (only the addition).
            self.store.delete(f"morefor:{task['id']}")
            if more != "asked" and (dropped := left_out(task["result"], params.summary)):
                self.store.set(f"morefor:{task['id']}", "asked")
                raise ValueError(
                    "Not reported yet: this report replaces the task's result, and it leaves out "
                    f"most of the earlier one ({', '.join(dropped[:6])}…). The owner reads only "
                    "the latest: call report again with the whole result, updated (what still "
                    "holds from before, what changed, what is new). If the earlier result no "
                    "longer holds, call report again unchanged."
                )
        unread = (
            [
                text
                for text, about, _ in self.states[caller].unread.values()
                if task["id"] in about or "" in about
            ]
            if task
            else []
        )
        if (
            task
            and params.status == "done"
            and task["status"] == "running"
            and unread
            and not self.store.get(f"unread_report:{task['id']}")
        ):
            # Sent while it was finishing, about this task: a report now would be without it,
            # and whoever sent it would send it again. Once: it may be lost on the way. A new
            # task, or more on another, does not hold this one's report back.
            self.store.set(f"unread_report:{task['id']}", "1")
            newest = unread[-1]
            first = min(entry[2] for entry in self.states[caller].unread.values())
            owner_chat = self.store.value(
                "SELECT count(*) FROM messages WHERE session_id=? AND id>=? AND kind='owner' "
                "AND json_extract(meta, '$.task_id')=?",
                (caller, first, task["id"]),
            )
            if owner_chat:
                # The owner's chat may ask for separate work (its worker decides, see
                # Runtime.apart_note): each deliverable is reported on its own, never with
                # this task's result.
                raise ValueError(
                    "Not reported yet: a newer message from the owner about this task arrived "
                    f"while you were finishing (“{newest[:200]}”). It reaches you next. Report "
                    "each deliverable on its own, never two in one report: if it asks for "
                    "separate work, do that and report it with report(new_task_title=…), then "
                    f"report this task's own work with task_id='{task['id']}'; if it adds to "
                    "this task, handle it, then report this task once, with it; a question, "
                    "answer in your reply, then report this task as you were. If you see it "
                    "nowhere after this, report again as you were."
                )
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
        # Reported done with a run it says still goes on, and nothing will look at it again (one
        # on GitHub Chief of Staff reads itself): told once how the owner hears its end.
        run_note = ""
        unwatched = [
            item
            for item in (self.runtime.task_actions.runs_going(task["id"]) if task else [])
            if not GITHUB_RUN.match(item["url"])
        ]
        if (
            task
            and params.status == "done"
            and row["role"] == WORKER
            and unwatched
            and task["status"] != "cancelled"
            and not self.store.get(f"setaside:{task['id']}")
            and task["id"] not in self.states[caller].turn_check_backs
            and not self.store.get(f"runnudge:{task['id']}")
        ):
            self.store.set(f"runnudge:{task['id']}", "1")
            names = ", ".join(f"\u201c{item['name'] or item['url']}\u201d" for item in unwatched)
            run_note = (
                f" By what you said, {names} still goes on, and nothing will look at it again: "
                "for the owner to hear how it ends, call check_back for it now (task_id="
                f"'{task['id']}') and end your turn; if it has ended, report again with its end "
                "state in artifacts."
            )
        meta = {"status": params.status, "task_id": task["id"] if task else None}
        drafts = {
            d["id"] for d in self.store.all("SELECT id FROM drafts WHERE session_id=?", (caller,))
        }
        shown = owner_words(params.summary, drafts)
        reported = self.runtime.add_message(caller, "report", shown, meta)
        if (
            task
            and params.status == "done"
            and (params.criteria or params.changed_code is not None)
        ):
            run = self.store.one(
                "SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
                (task["id"],),
            )
            if run and params.criteria:
                claims = [item.model_dump() for item in params.criteria[:12]]
                self.store.execute(
                    "UPDATE runs SET criteria=? WHERE id=?", (json.dumps(claims), run["id"])
                )
            if run and params.changed_code is not None:
                # What it says it changed, held against its checkouts (see receipts.summarize).
                self.store.set(f"saidchanged:{run['id']}", "1" if params.changed_code else "0")
        settled = chat_news = False
        if task:
            # It reports on it: whatever it says next (a question, say), it is not "stopped".
            self.store.delete(f"stopped:{task['id']}")
            self.store.delete(f"goingon:{task['id']}")
            asked = bool(self.store.get(f"followup:{task['id']}"))
            if task["status"] == "cancelled" and not asked:
                # Cancelled (a report already on its way, say): it stays cancelled.
                settled = True
            elif task["status"] == "cancelled" and params.status != "done":
                # Answering the owner's question about it: news, not the task coming back.
                task = self.tasks.update(task["id"], progress=params.summary[:6000])
            elif (
                task["status"] == "waiting"
                and (was := self.store.get(f"askedafter:{task['id']}"))
                and params.status in {"progress", "done"}
                and not asked
                and not (changed and params.status == "done")
            ):
                # Finished work it asked the owner about (news on its PR): answered, it goes
                # back where it was, and what it says now is news; its result stays.
                self.store.delete(f"askedafter:{task['id']}")
                self.runtime.task_actions.news(task["id"], params.summary)
                task = self.tasks.move(task["id"], was, progress=params.summary[:6000])
            elif task["status"] in {"review", "done"} and params.status == "progress":
                # News about finished work (PR feedback handled, a question answered): it stays
                # where it is, and stays in sight until the owner has seen it. A blocker or a
                # question needs the owner, and shows as such.
                self.runtime.task_actions.news(task["id"], params.summary)
                task = self.tasks.update(task["id"], progress=params.summary[:6000])
            elif (
                params.status == "done"
                and task["status"] == "review"
                and task["id"] in self.states[caller].turn_reported
                and not asked
            ):
                # Reported again in the same turn (with its criteria, say): the report is
                # amended in place; the owner and the Router heard of it once.
                self.runtime.task_actions.remember_prs(task["id"], params.summary)
                task = self.tasks.update(task["id"], result=clip_report(shown))
                self.runtime.task_actions.publish(task)
                self.runtime.update_session(caller, report=params.summary[:4000])
                return (
                    f"Task {task['id']}'s report is updated. It was reported already: the "
                    "Router is not told again." + run_note
                )
            elif (
                params.status == "done"
                and asked
                and task["status"] in {"review", "done"}
                and not changed
            ):
                # The owner's chat about finished work: an answer is news on that task, and the
                # result the owner reviews (or accepted) stays. Its work redone or corrected is
                # its result now (bug E34: that stayed news, the old result in sight), the
                # earlier one kept with it; accepted, it is news too: the owner sees it came.
                self.store.delete(f"followup:{task['id']}")
                self.store.delete(f"morefor:{task['id']}")
                self.runtime.task_actions.remember_prs(task["id"], params.summary)
                if params.replaces_result:
                    self.runtime.task_actions.keep_earlier_result(task, clip_report(shown))
                    if task["status"] != "done":
                        self.store.delete(f"read:{task['id']}")  # A new result, not read yet.
                    task = self.tasks.update(task["id"], result=clip_report(shown))
                if task["status"] == "done" or not params.replaces_result:
                    self.runtime.task_actions.news(task["id"], params.summary)
                self.runtime.task_actions.publish(task)
                others = [
                    other
                    for other in self.tasks.for_session(caller)
                    if other["status"] in OPEN and other["id"] != task["id"]
                ]
                self.runtime.update_session(
                    caller, report=params.summary[:4000], status="idle" if others else "done"
                )
                chat_news = True
            elif params.status == "done" and task["status"] == "done" and not asked and not changed:
                # Accepted, or its PR merged: news about it does not reopen the review.
                self.runtime.task_actions.remember_prs(task["id"], params.summary)
                self.runtime.task_actions.news(task["id"], params.summary)
                self.runtime.task_actions.keep_earlier_result(task, clip_report(shown))
                task = self.tasks.update(task["id"], result=clip_report(shown))
            elif params.status == "done":
                self.store.delete(f"followup:{task['id']}")
                self.store.delete(f"newresult:{task['id']}")
                self.store.delete(f"ownchange:{task['id']}")
                self.store.delete(f"read:{task['id']}")  # A new result, not read yet.
                self.states[caller].turn_reported.add(task["id"])
                self.runtime.task_actions.remember_prs(task["id"], params.summary)
                self.runtime.task_actions.keep_earlier_result(task, clip_report(shown))
                task = self.tasks.move(task["id"], "review", result=clip_report(shown))
                # The workers it works with that are still at it get its result.
                self.runtime.background(self.runtime.peers.finished(caller, task, params.summary))
                if self.store.get(f"draft_sent:{task['id']}"):
                    # The owner already sent what it drafted: one approval was enough.
                    self.runtime.background(
                        self.runtime.drafts.settle_reported(task["id"], reported=True)
                    )
                else:
                    self.runtime.background(
                        self.runtime.task_actions.collect_evidence(task["id"], ended=True)
                    )
                    # The one notification a finished task gets (the Router does not retell it).
                    # Whether it is to accept (it changed code…) shows on Home once its
                    # receipts are in; the notification says it is finished.
                    self.runtime.alert(
                        f"“{task['title']}” finished",
                        session_id=caller,
                        needs_action=False,
                        title="Finished",
                    )
            elif params.status in {"question", "blocked"}:
                if task["status"] in {"review", "done"} and not asked:
                    self.store.set(f"askedafter:{task['id']}", task["status"])
                if changed:
                    # It changed its code since its result (fixes for its PR's review, say):
                    # the owner's answer does not move where its next result starts past that.
                    self.runtime.task_actions.keep_result_base(task)
                task = self.tasks.move(task["id"], "waiting", progress=params.summary[:6000])
            elif params.status == "failed" and (
                (
                    was := self.store.get(f"reopened:{task['id']}")
                    or self.store.get(f"askedafter:{task['id']}")
                    or ""
                )
                in {"review", "done"}
                or task["status"] in {"review", "done"}
            ):
                # More asked of finished work that it cannot do: what it had stands, and why is
                # news on it.
                self.runtime.task_actions.news(task["id"], params.summary)
                task = (
                    self.tasks.move(task["id"], was, progress=params.summary[:6000])
                    if was
                    else self.tasks.update(task["id"], progress=params.summary[:6000])
                )
            elif params.status == "failed":
                # It cannot be done: under Needs you with why, for the owner to retry or drop.
                task = self.tasks.move(task["id"], "failed", progress=params.summary[:6000])
                # Where it stopped: retried later, what other tasks did meanwhile is not its own.
                self.runtime.background(self.runtime.task_actions.record_end(task))
                self.runtime.alert(
                    f"“{task['title']}” failed: {params.summary[:160]}",
                    session_id=caller,
                    needs_action=True,
                    title="Failed",
                )
            elif task["status"] != "running":
                task = self.tasks.move(task["id"], "running", progress=params.summary[:6000])
            else:
                task = self.tasks.update(task["id"], progress=params.summary[:6000])
        self.states[caller].resting = ""
        if params.status in {"question", "blocked"}:
            # Under Needs you until answered, whatever becomes of the task (the Slack
            # session's too: it has no task).
            self.runtime.asks.record_question(
                caller,
                params.summary,
                task["id"] if task else "",
                about_draft=params.about_draft,
                why=params.why,
                options=params.options,
                next_step=params.next,
                kind=params.status,
            )
        else:
            # It went on with this task: its question is behind it (another task's stays).
            self.runtime.asks.answered(caller, task["id"] if task else "", by="worker")
        if row["role"] == WORKER and params.status in {"progress", "done"}:
            # A PR it delivers is its PR: reviews, comments and CI come back to it. Not every
            # PR it mentions (stale CI news of others' PRs would follow).
            self.runtime.background(self.follow_own_pulls(caller, params.summary, task))
        if row["role"] == WORKER:
            # The PRs it names are shown with their titles.
            self.runtime.background(self.runtime.task_actions.learn_pr_titles(params.summary))
        if row["role"] == WORKER:
            self.runtime.watchdog.save(caller, nudges=0)
        if chat_news and task:
            # A PR it opened as asked shows on the card (what was checked stays as it was).
            self.runtime.background(
                self.runtime.task_actions.collect_evidence(task["id"], keep_stale=True)
            )
            if params.replaces_result:
                return (
                    f"Task {task['id']}'s result is this report now (the owner asked in chat); "
                    "the earlier one is kept with it." + run_note
                )
            return (
                f"Noted as an update on task {task['id']} (the owner asked in chat; its "
                "result stays as it is)." + run_note
            )
        if settled and task and params.status in {"question", "blocked"}:
            # A cancelled task's worker still needs the owner: the question shows under Needs
            # you, and the Router hears of it.
            await self.runtime.tell_router(row, params.status, params.summary, task)
            return (
                f"Task {task['id']} was cancelled, but your question reaches the owner: it is "
                "under Needs you."
            )
        if settled and task:
            return f"Task {task['id']} was cancelled: this report stays in your session only."
        values: dict[str, Any] = {"report": params.summary[:4000]}
        if row["role"] == WORKER and params.status in REPORT_STATUS:
            values["status"] = REPORT_STATUS[params.status]
            if params.status == "failed" and task and task["status"] != "failed":
                values["status"] = "done"  # Its finished work stands.
            if values["status"] in {"done", "failed"} and any(
                other["status"] in OPEN for other in self.tasks.for_session(caller)
            ):
                values["status"] = "idle"  # Another of its tasks is still open: not done.
        self.runtime.update_session(caller, **values)
        if params.status in {"done", "failed"}:
            self.runtime.background(
                self.runtime.workers.pump()
            )  # Tasks waiting for this one may start now (or are blocked).
        news = self.states[caller].thread_turn and (not task or task["status"] != "running")
        self.states[caller].thread_turn = False  # It told the owner.
        if params.status == "progress" and not news:
            return "Noted in your session. The Router is not interrupted for progress."
        await self.runtime.tell_router(row, params.status, params.summary, task)
        # On Home now (a line in the Router's chat, a card): an answer to the owner's @ there
        # is not shown again.
        self.store.update_message_meta(reported["id"], {**meta, "told": True})
        if news and params.status == "progress":
            # News on a PR it follows, for work already finished: the owner reads it.
            self.runtime.alert(
                f"Update on “{task['title'] if task else self.runtime.display_title(row)}”",
                session_id=caller,
                needs_action=False,
                title="PR update",
            )  # fmt: skip
        return "Reported to the Router." + split_note + run_note

    async def t_cancel_task(self, params: CancelTask, caller: str):
        self.caller(caller, ROUTER)
        found = self.runtime.task_actions.task_for(params.task_id)
        if stale := self.changed_since_looked(caller, found):
            raise ValueError(stale)
        task = await self.runtime.task_actions.cancel(found["id"], params.reason, "the Router")
        if task["status"] == "done":
            return (
                f"Task {task['id']} “{task['title']}” had finished already: its result was "
                "reported to the owner, so nothing was stopped: it is set aside, not accepted "
                "(its result is kept for reference; drafts of it still waiting for the owner were "
                "withdrawn). If the owner asked to stop it, tell them it had finished."
            )
        return f"Cancelled task {task['id']}."

    def recent_task(self, session_id: str) -> dict | None:
        """The worker's task given in the last half hour (not cancelled) that a new one may
        only add to, when the Router was not asked about it yet. Not an adopted session's work
        so far: no task was given there."""
        since = (datetime.now(UTC) - timedelta(minutes=ADD_MINUTES)).isoformat()
        for task in reversed(self.tasks.for_session(session_id)):
            if task["created_at"] < since:
                break
            if (
                task["status"] != "cancelled"
                and task["result"] != ADOPTED
                and not self.store.get(f"newasked:{task['id']}")
            ):
                return task
        return None

    async def t_stop_following(self, params: StopFollowing, caller: str):
        self.caller(caller, ROUTER)
        reference = params.session_id.strip()
        task = self.tasks.find(reference) if re.fullmatch(r"t[0-9a-f]{4,7}", reference) else None
        try:
            row = self.store.session(task["session_id"] if task else reference)
        except LookupError:
            raise ValueError(f"No worker or task {reference}; list_sessions lists them.") from None
        follows = self.store.follows(row["id"])
        if params.thread.strip():
            wanted = [threads.normalize(params.thread)]
        elif task:
            wanted = self.runtime.task_actions.followed_for(task, follows)
        else:
            wanted = list(follows)
        gone = [key for key in wanted if key in follows]
        if not gone:
            listed = ", ".join(follows) or "nothing"
            raise ValueError(
                f"“{self.runtime.display_title(row)}” does not follow that ({listed})."
            )
        # Given a task: for that task only (its other tasks that follow it go on).
        about = task["id"] if task else ""
        kept = [
            key
            for key in gone
            if not self.runtime.unfollow_for(row["id"], key, about, "The Router")
        ]
        names = self.runtime.slack.display_names()
        said = ", ".join(threads.label(key, names) for key in gone)
        # The owner reads the notice: the task by its title (not its id, nor the worker's).
        for_task = f" for \u201c{task['title']}\u201d" if task else ""
        self.runtime.add_message(
            row["id"], "notice", f"No longer following {said}{for_task} (the Router stopped it).",
            {"threads": gone},
        )  # fmt: skip
        gone_all = [key for key in gone if key not in kept]
        lines = []
        # Others that still follow it get its news; else a Slack thread's comes to the Router
        # (Slack is searched for what reaches the owner), a PR's to no one (only followed PRs
        # are read).
        held = {
            key: [
                self.runtime.display_title(self.store.session(other))
                for other in self.store.followers(key)
                if other != row["id"]
            ]
            for key in gone_all
        }
        slack = [key for key in gone_all if not held[key] and not threads.GITHUB_PR.fullmatch(key)]
        prs = [key for key in gone_all if not held[key] and threads.GITHUB_PR.fullmatch(key)]
        if slack:
            listed = ", ".join(threads.label(key, names) for key in slack)
            lines.append(
                f"Its worker no longer follows {listed}{for_task}: new messages there come to you."
            )
        if prs:
            listed = ", ".join(threads.label(key, names) for key in prs)
            it, its = ("them", "their") if len(prs) > 1 else ("it", "its")
            lines.append(
                f"Its worker no longer follows {listed}{for_task}. Nobody follows {it} now, so "
                f"{its} CI, reviews and comments reach no one, you included, until a worker "
                f"follows {it} again (hand {it} on with follow=). A review requested from the "
                "owner still comes to you."
            )
        for key in gone_all:
            if held[key]:
                many = len(held[key]) > 1
                followers = ", ".join(f"\u201c{name}\u201d" for name in held[key])
                lines.append(
                    f"Its worker no longer follows {threads.label(key, names)}{for_task}; the "
                    f"worker{'s' if many else ''} of {followers} still follow{'' if many else 's'}"
                    f" it, so news there goes to {'them' if many else 'that worker'}."
                )
        if kept:
            listed = ", ".join(threads.label(key, names) for key in kept)
            lines.append(
                f"Task {task['id'] if task else ''}{for_task} no longer follows {listed}; its "
                "worker still follows it for its other tasks, so news there still goes to it."
            )
        return " ".join(lines) + " Its tasks, results and drafts are as they were."

    async def t_stop_worker(self, params: StopWorker, caller: str):
        self.caller(caller, ROUTER)
        reference = params.session_id.strip()
        task = self.tasks.find(reference) if re.fullmatch(r"t[0-9a-f]{4,7}", reference) else None
        try:
            row = self.store.session(task["session_id"] if task else reference)
        except LookupError:
            raise ValueError(f"No worker or task {reference}; list_sessions lists them.") from None
        if row["role"] != WORKER or row["closed_at"] or self.runtime.special(row):
            raise ValueError(f"{reference} is not an open worker of the owner's.")
        name = self.runtime.display_title(row)
        if row["status"] not in {"working", "queued", "starting"}:
            return f"“{name}” is not at work ({row['status']}): nothing to stop."
        await self.runtime.workers.interrupt(row["id"], by="the Router")
        stopped = [
            f"{t['id']} “{t['title']}”"
            for t in self.tasks.for_session(row["id"])
            if t["status"] == "waiting" and self.store.get(f"stopped:{t['id']}")
        ]
        return f"Stopped “{name}”" + (
            f": {', '.join(stopped)} waits for the owner's Go on (or Cancel)." if stopped else "."
        )

    async def t_add_watch(self, params: AddWatch, caller: str):
        self.caller(caller, ROUTER)
        what, how = " ".join(params.what.split())[:500], " ".join(params.how.split())[:300]
        if len(what) < 3:
            raise ValueError("Say what to look out for, and where.")
        watches = self.store.settings().watches
        if any(watch.what.lower() == what.lower() for watch in watches):
            return "The owner has this watch already (Settings → Connections → Watches)."
        if proposed := next(
            (
                card
                for card in self.store.drafts()
                if card["kind"] == "form"
                and str(card["target"].get("watch") or "").lower() == what.lower()
            ),
            None,
        ):
            return (
                f"You proposed this watch already: card {proposed['id']} waits for the owner's "
                "OK under Needs you."
            )
        if len(watches) >= MAX_WATCHES:
            raise ValueError(
                f"The owner has {MAX_WATCHES} watches, the most there can be: ask them to remove "
                "one in Settings → Connections → Watches first."
            )
        schema = {
            "type": "object",
            "properties": {
                "what": {"type": "string", "title": "Look out for", "default": what},
                "how": {
                    "type": "string",
                    "title": "Using",
                    "description": "A tool or source, if it matters (the Slack MCP, Glean…)",
                    "default": how,
                },
            },
            "required": ["what"],
        }
        draft = self.runtime.asks.offer(
            caller,
            "form",
            # `watch`: the card is a watch (its expiry after a restart is told, see Asks).
            {"schema": schema, "source": "Chief of Staff", "watch": what},
            f"Add a watch? Chief of Staff looks out for this every few minutes in work hours "
            f"and tells you what needs you (Settings → Connections → Watches): {what}",
            lambda result: self.watch_answered(caller, result),
            title="New watch",
        )
        return (
            f"Asked the owner to confirm the watch (card {draft['id']} under Needs you); what "
            "they chose reaches you with your next message. Tell them it waits for their OK."
        )

    async def watch_answered(self, router: str, result: dict):
        """The owner's answer to a watch the Router proposed."""
        content = result.get("content") or {}
        what = " ".join(str(content.get("what") or "").split())[:500]
        how = " ".join(str(content.get("how") or "").split())[:300]
        if result.get("action") != "accept" or not what:
            text = "The owner did not add the watch you proposed."
        else:
            old = self.store.settings()
            try:
                if any(
                    " ".join(watch.what.split()).casefold() == what.casefold()
                    for watch in old.watches
                ):
                    raise ValueError(f"the owner has this watch already ({what})")
                if len(old.watches) >= MAX_WATCHES:
                    raise ValueError(f"there are {MAX_WATCHES} watches already")
                new = old.model_copy(update={"watches": [*old.watches, Watch(what=what, how=how)]})
            except ValueError as exc:
                text = f"The watch could not be added: {exc}"
            else:
                self.store.save_settings(new)
                await self.runtime.settings_changed(old, new)
                text = (
                    f"The owner added the watch: {what}. What it finds comes to you as "
                    'from="watch" events.'
                )
        with suppress(LookupError):
            self.delivery.hold(
                router, wrap("system", "draft-outcome", text), kind="notice", display=text
            )

    async def t_rename_task(self, params: RenameTask, caller: str):
        self.caller(caller, ROUTER)
        task = self.runtime.task_actions.task_for(params.task_id)
        renamed = self.runtime.task_actions.rename(task["id"], params.title, "the Router")
        return f"Renamed task {renamed['id']} to “{renamed['title']}”."

    async def t_split_task(self, params: SplitTask, caller: str):
        self.caller(caller, ROUTER)
        task = self.runtime.task_actions.task_for(params.task_id)
        made = self.runtime.task_actions.split(task["id"], params.title, "the Router")
        return f"Split off task {made['id']} “{made['title']}” ({made['status']})."

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
        # Where it really goes, and the newest message there (the card quotes it; sending
        # checks nobody wrote since).
        place = await self.runtime.slack.reply_place(
            params.channel_id, params.thread_ts, params.in_thread
        )
        target = {
            "channel_id": params.channel_id,
            "thread_ts": place["thread_ts"],
            "to": params.to.strip() or self.runtime.drafts.recipient(params.channel_id, place),
            "latest": place.get("quote", ""),
            "latest_ts": place.get("ts", ""),
        }
        if place.get("error"):
            target["latest_error"] = place["error"]
        if found := self.runtime.draft_task(caller, target):
            target["task_id"] = found  # The task it answers for, not merely its newest.
        draft = self.store.create_draft(caller, "slack", target, params.text, params.note)
        replaced = await self.runtime.drafts.supersede(draft)
        self.runtime.add_message(caller, "draft", params.text, {"draft_id": draft["id"]})
        self.bus.publish("draft", draft)
        self.runtime.alert(
            params.note or params.text,
            session_id=caller,
            needs_action=True,
            title="Slack reply to approve",
        )
        result = (
            f"Draft {draft['id']} is waiting for the owner's approval. You will be told if "
            "they edit or reject it. Do not send it any other way."
        )
        if place["thread_ts"] != params.thread_ts:
            result += (
                f" It goes in the thread of {place['thread_ts']} (that message is a reply in it)."
                if place["thread_ts"]
                else " It goes into the DM itself: that message is in no thread (in_thread=true "
                "starts one)."
            )
        if replaced:
            result += f" It replaces your earlier draft {', '.join(replaced)} there."
        result += self.runtime.drafts.beside_note(draft)
        if place.get("error"):
            result += (
                f" Slack showed no message there ({place['error']}): check channel_id and "
                "thread_ts, and withdraw_draft it if they are wrong."
            )
        return result

    async def t_withdraw_draft(self, params: WithdrawDraft, caller: str):
        self.caller(caller, WORKER, SLACK)
        draft = await self.runtime.drafts.withdraw(caller, params.draft_id, params.reason)
        return f"Draft {draft['id']} is withdrawn: the owner no longer sees it."

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
        if pulls := threads.PR_URL.findall(f"{link} {params.where}"):
            # On a PR: what the owner posts there themself settles it.
            repo, number = pulls[0]
            target |= {"repo": repo.lower(), "number": int(number), "anywhere": True}
        if found := self.runtime.draft_task(caller, target):
            target["task_id"] = found
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
        if not params.body.strip() and (
            params.in_reply_to or params.reply_to_comment or not params.comments
        ):
            raise ValueError("The text is empty.")
        if len(params.body) + sum(len(comment.body) for comment in params.comments) > MAX_DRAFT:
            raise ValueError(f"Too long: keep the review under {MAX_DRAFT} characters.")
        reply = params.in_reply_to or params.reply_to_comment
        if params.in_reply_to and params.reply_to_comment:
            raise ValueError("Reply to a line comment (in_reply_to) or to a conversation comment "
                             "(reply_to_comment), not both.")  # fmt: skip
        if reply and params.comments:
            raise ValueError("A reply to a comment has no inline comments of its own.")
        if reply and params.verdict != "comment":
            raise ValueError("A reply to a comment has no verdict: leave verdict out.")
        if params.resolve and not params.in_reply_to:
            raise ValueError("Only a conversation on a line (in_reply_to) can be resolved.")
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
        body = params.body
        if params.in_reply_to:
            target["in_reply_to"] = params.in_reply_to
            target["comment"] = await github.review_comment(repo, params.number, params.in_reply_to)
            if params.resolve:
                target["resolve"] = True
            label = f"Reply on {repo}#{params.number}"
        elif params.reply_to_comment:
            target["reply_to_comment"] = params.reply_to_comment
            target["comment"] = await github.conversation_comment(
                repo, params.number, params.reply_to_comment
            )
            # Who it answers is named: a conversation comment has no thread to reply in.
            body = mention(body, target["comment"]["author"])
            label = f"Reply on {repo}#{params.number}"
        else:
            if pull.get("state") != "open":
                raise ValueError(f"{repo}#{params.number} is not open.")
            target["head_sha"] = pull["head"]["sha"]
            target["verdict"] = params.verdict
            # What the PR is, in a line, for the owner to review the review.
            target["pr"] = {
                "title": (pull.get("title") or "")[:200],
                "author": (pull.get("user") or {}).get("login", ""),
                "about": " ".join((pull.get("body") or "").split())[:300],
                "additions": pull.get("additions", 0),
                "deletions": pull.get("deletions", 0),
                "files": pull.get("changed_files", 0),
            }
            author = ((pull.get("user") or {}).get("login") or "").lower()
            if author and author == await github.acting_login(repo):
                # GitHub allows only comments on one's own pull request (one of the owner's
                # other accounts may approve it).
                target["own"] = True
                target["verdict"] = "comment"
            if params.comments:
                files = await github.files(repo, params.number)
                if problems := misplaced(params.comments, diff_lines(files)):
                    raise ValueError(
                        "GitHub only takes comments on lines of the PR's diff; fix these and "
                        "draft again (or move them into the body):\n- " + "\n- ".join(problems[:20])
                    )
                target["comments"] = [
                    comment.model_dump(exclude_defaults=True)
                    | {
                        "side": comment.side,
                        # The lines it is about, as the PR shows them.
                        "context": snippet(files, comment.path, comment.line, comment.side),
                    }
                    for comment in params.comments
                ]
            label = {"approve": "Approval", "request_changes": "Changes requested"}.get(
                target["verdict"], "Review"
            ) + f" of {repo}#{params.number}"
        if found := self.runtime.draft_task(caller, target):
            target["task_id"] = found
        draft = self.store.create_draft(caller, "github", target, body)
        replaced = await self.runtime.drafts.supersede_review(draft)
        if task := self.runtime.tasks.find(draft["target"].get("task_id") or "") or (
            self.runtime.tasks.current(caller)
        ):
            # Its card shows the PR it is about, as GitHub has it.
            self.runtime.task_actions.remember_prs(task["id"], target["url"])
        shown = body
        if params.comments:
            shown += "\n\nInline comments:\n" + "\n".join(
                f"- {c.path}:{c.line}: {c.body}" for c in params.comments
            )
        self.runtime.add_message(caller, "draft", shown, {"draft_id": draft["id"], "label": label})
        self.bus.publish("draft", draft)
        self.runtime.drafts.link_reviews()  # A Slack reply that may speak of it waits for it.
        self.runtime.alert(
            label, session_id=caller, needs_action=True, title="GitHub text to approve"
        )
        result = f"Draft {draft['id']} is waiting for the owner's approval."
        if replaced:
            result += f" It replaces your earlier review {', '.join(replaced)} of this PR."
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
        followed = self.runtime.follow(caller, [params.thread], given=False, asked=[params.thread])
        if not followed:
            raise ValueError(
                f"You were stopped following {threads.normalize(params.thread)}; only the "
                "Router or the owner can give it back."
            )
        return f"Following {', '.join(followed)}."

    async def t_check_back(self, params: CheckBack, caller: str):
        self.caller(caller, WORKER)
        task = self.runtime.task_actions.report_task(caller, params.task_id)
        # Reported done with a run it said still goes on: checked on when it ends.
        run_going = bool(
            task
            and task["status"] in {"review", "done"}
            and not self.store.get(f"setaside:{task['id']}")
            and self.runtime.task_actions.runs_going(task["id"])
        )
        if not task or (task["status"] not in {"running", "waiting"} and not run_going):
            raise ValueError(
                "check_back is for a task you are still on, or a run of a finished one that "
                "still goes on."
            )
        what = " ".join(params.what.split())[:300]
        if not what:
            raise ValueError("Say what you will check.")
        url = params.url.strip()
        if url and not re.match(r"^https?://\S+$", url):
            raise ValueError("url must be an http(s) link, or empty.")
        at = datetime.now(UTC) + timedelta(minutes=params.minutes)
        self.runtime.task_actions.set_check_back(task, at.isoformat(), what, url, run=run_going)
        self.states[caller].turn_check_backs.add(task["id"])
        clock = at.astimezone().strftime("%H:%M")
        return (
            f"You are woken at {clock} ({params.minutes} minutes) to check {what}. End your turn "
            "now; do not wait in a shell."
        )

    async def t_unfollow_thread(self, params: FollowThread, caller: str):
        self.caller(caller, WORKER, SLACK)
        key = threads.normalize(params.thread)
        self.store.unfollow(key, caller)
        self.runtime.publish_session(self.store.session(caller))
        if threads.GITHUB_PR.fullmatch(key) and not self.store.followers(key):
            return "Unfollowed. Nobody follows it now: its news reaches no one."
        return "Unfollowed. New messages there go to the Router again."

    def moved_on_since_review(self, row: dict) -> str:
        """Why a nudge decided on the review is moot now ("" if it is not): the worker reported,
        or has nothing open to go on with."""
        mark = self.runtime.watchdog.state(row["id"]).get("reviewed_mark", 0)
        later = self.store.messages(row["id"], after=mark) if mark else []
        if any(m["kind"] == "report" for m in later):
            return "it reported after it was flagged."
        if not any(t["status"] == "running" for t in self.tasks.for_session(row["id"])):
            return "it has no running task now."
        return ""

    async def t_assess_worker(self, params: AssessWorker, caller: str):
        self.caller(caller, WATCHDOG)
        row = self.store.session(params.session_id)
        if row["role"] != WORKER or row["closed_at"]:
            raise ValueError("That is not an open worker.")
        action = params.action
        nudges = self.runtime.watchdog.state(row["id"]).get("nudges", 0)
        if action in {"nudge", "interrupt"} and nudges >= self.runtime.watch.nudges:
            action = "escalate"  # Earlier nudges did not help; a person or the Router must decide.
        # Its decision in words (bug bash 9: "on track, none."); "Not sent: …" may follow.
        decided = {
            "none": "no action",
            "nudge": "nudge it",
            "interrupt": "stop and nudge it",
            "escalate": "tell the Router",
        }[action]
        self.runtime.add_message(
            caller,
            "notice",
            f"“{row['title']}”: {params.verdict.replace('_', ' ')}: {decided}. {params.reason}",
            {"session_id": row["id"]},
        )
        if action in {"nudge", "interrupt"} and (moved := self.moved_on_since_review(row)):
            # Out of date by the time it came: what it was flagged for is behind it.
            self.runtime.add_message(
                caller, "notice", f"Not sent: {moved}", {"session_id": row["id"]}
            )
            return f"Not sent: {moved}"
        if action in {"nudge", "interrupt"}:
            if action == "interrupt":
                await self.runtime.workers.interrupt(row["id"], by="the watchdog")
                # Stopped to be told what to do instead (the nudge below): it goes on at once,
                # not stopped for the owner.
                for task in self.tasks.for_session(row["id"]):
                    if self.store.get(f"stopped:{task['id']}") == "the watchdog":
                        self.runtime.task_actions.publish(self.tasks.move(task["id"], "running"))
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


def mention(body: str, login: str) -> str:
    """A reply that names whom it answers: "@login " first, unless it mentions them already."""
    if not login or re.search(rf"(?<![\w-])@{re.escape(login)}(?![\w-])", body, re.IGNORECASE):
        return body
    return f"@{login} {body.lstrip()}"
