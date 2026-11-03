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
from .decisions import AckDecision, AskDecision, RecordDecision
from .envelope import clean_title, short, wrap
from .models import NestedArguments
from .policy import within
from .roles import ROUTER, SLACK, WATCHDOG, WORKER
from .workspaces import resolve_project

if TYPE_CHECKING:
    from .runtime import Runtime


class SpawnWorker(BaseModel):
    title: str = Field(description="Short task title shown to the owner (under 60 characters)")
    instructions: str = Field(
        description="The task in the owner's words, plus the context the worker needs"
    )
    project: str = Field(
        default="",
        description="Absolute project directory from list_sessions().projects; empty when the "
        "task has no codebase",
    )
    isolate: bool = Field(
        default=False,
        description="Create a dedicated git worktree and branch; use for code changes",
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
        "simple chores; empty for the default",
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
    text: str = Field(description="The message for that session")
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


class DraftGitHubReview(BaseModel):
    repo: str = Field(description="owner/name")
    number: int = Field(description="Pull request number")
    body: str = Field(description="The review comment (markdown)")


REPORT_STATUS = {"question": "waiting", "blocked": "waiting", "done": "done"}


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

    async def t_spawn_worker(self, params: SpawnWorker, caller: str, distill: bool = False):
        self.caller(caller, ROUTER)
        title = clean_title(params.title)
        follow = [threads.normalize(key) for key in params.follow]
        after = [self.runtime.task_actions.task_for(reference) for reference in params.after]
        context_from = [
            self.runtime.task_actions.task_for(reference)["id"] for reference in params.context_from
        ]
        await self.runtime.check_model(params.model)
        project = (
            str(resolve_project(params.project, self.workspaces.projects()))
            if (params.project)
            else ""
        )
        why_new = " ".join(params.why_new.split())[:200]
        if not distill and not why_new:
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
                    "context and files). Otherwise call spawn_worker again with why_new.\n"
                    + json.dumps(related, ensure_ascii=False)
                )
        session_id = str(uuid4())
        slack = params.slack and self.runtime.slack.configured()
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
        if effort and info and effort not in info["efforts"]:
            effort = ""  # Reported below; the model's default effort applies.
        if params.model or effort:
            self.store.update_session(session_id, model=params.model, effort=effort)
        criteria = clean_criteria(params.done_when)
        decisions = [self.runtime.decisions.get(identifier) for identifier in params.decisions]
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
        if distill:
            self.store.set(f"distill:{session_id}", "1")
        self.runtime.follow(session_id, follow)
        prompt = wrap(
            "router",
            "task",
            task_text(params.instructions, criteria, decisions),
            title=title,
            task=task["id"],
        )
        if context_from:
            prompt += "\n\n" + wrap("system", "context", self.tasks.briefing(context_from))
        self.runtime.add_message(
            session_id, "router", params.instructions, {"prompt": prompt, "task_id": task["id"]}
        )
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
        if params.effort and not effort:
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

    def link_decisions(self, task_id: str, decisions: list[dict]):
        for decision in decisions:
            self.store.execute(
                "INSERT OR IGNORE INTO task_decisions(task_id, decision_id) VALUES (?,?)",
                (task_id, decision["id"]),
            )

    async def t_message_session(self, params: MessageSession, caller: str):
        self.caller(caller, ROUTER)
        target = self.store.session(params.session_id)
        if target["role"] == ROUTER:
            raise ValueError("That is your own session.")
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
            decisions = [self.runtime.decisions.get(identifier) for identifier in params.decisions]
            task = self.tasks.create(
                target["id"], title, params.text[:4000], status="running", criteria=criteria
            )
            self.link_decisions(task["id"], decisions)
            prompt = wrap(
                "router",
                "task",
                task_text(params.text, criteria, decisions),
                title=title,
                task=task["id"],
            )
            notice = f"New task “{title}” for “{target['title']}”"
        else:
            if task and task["status"] == "cancelled":
                raise ValueError(
                    f"Its task {task['id']} was cancelled. For new work in this worker use "
                    "task='new' with a title."
                )
            if task and task["status"] in {"review", "done", "failed"}:
                task = self.tasks.move(task["id"], "running", reason="follow-up")
            prompt = wrap("router", "message", params.text, task=task["id"] if task else None)
            notice = f"Continued in “{target['title']}”"
        await self.delivery.deliver(
            target["id"],
            prompt,
            kind="router",
            display=params.text,
            meta={"task_id": task["id"]} if task else None,
        )
        self.runtime.add_message(caller, "notice", notice, {"session_id": target["id"]})
        return f"Delivered (task {task['id']})." if task else "Delivered."

    async def t_list_sessions(self, params: ListSessions, caller: str):
        settings = self.store.settings()
        try:
            models = [item["id"] for item in await self.runtime.available_models()]
        except Exception:
            models = []
        return {
            "projects": await self.workspaces.project_cards(),
            "models": models,
            "default_worker_model": settings.role_models.get(WORKER) or settings.model or "auto",
            "slack_connected": self.runtime.slack.configured(),
            "decisions": [
                {"id": d["id"], "decision": d["text"], "tasks": d["tasks"]}
                for d in self.runtime.decisions.active()
            ],
            "github_repos": settings.github_repos if settings.github_enabled else [],
            "sessions": [
                {
                    "session_id": row["id"],
                    "role": row["role"],
                    "title": row["title"],
                    "status": row["status"],
                    "cwd": row["cwd"],
                    "branch": row["branch"],
                    "slack_access": bool(row["slack"]),
                    "last_report": row["report"][:400],
                    "follows": self.store.follows(row["id"]),
                    "task": self.task_brief(self.tasks.current(row["id"])),
                    "model": self.runtime.model_for(row),
                }
                for row in self.store.open_sessions()
                if row["role"] != ROUTER
            ],
            "tasks": [
                {
                    **(self.task_brief(task) or {}),
                    "worker": short(task["session_id"]),
                    "after": [dep["id"] for dep in self.tasks.deps(task["id"])],
                }
                for task in self.tasks.listed()
            ],
            "recently_closed": [
                self.runtime.workers.card(row) for row in self.runtime.workers.recently_closed(8)
            ],
        }

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
        return [
            {
                "session_id": card["session_id"],
                "title": card["title"],
                "state": "closed" if card["closed"] else card["status"],
                "task": card["task"][:200],
                "last_report": card["last_report"][:200],
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
        meta = {"status": params.status, "task_id": task["id"] if task else None}
        self.runtime.add_message(caller, "report", params.summary, meta)
        if task and params.status == "done" and params.criteria:
            run = self.tasks.run(caller) or self.tasks.latest_run(caller)
            if run and run["task_id"] == task["id"]:
                claims = [item.model_dump() for item in params.criteria[:12]]
                self.store.execute(
                    "UPDATE runs SET criteria=? WHERE id=?", (json.dumps(claims), run["id"])
                )
        if task:
            if params.status == "done" and task["status"] == "done":
                # Accepted, or its PR merged: news about it does not reopen the review.
                task = self.tasks.update(task["id"], result=params.summary[:4000])
            elif params.status == "done":
                task = self.tasks.move(task["id"], "review", result=params.summary[:4000])
                self.runtime.background(self.runtime.task_actions.collect_evidence(task["id"]))
            elif params.status in {"question", "blocked"}:
                task = self.tasks.move(task["id"], "waiting", progress=params.summary[:1000])
            elif task["status"] != "running":
                task = self.tasks.move(task["id"], "running", progress=params.summary[:1000])
            else:
                task = self.tasks.update(task["id"], progress=params.summary[:1000])
        self.states[caller].resting = ""
        if row["role"] == WORKER and params.status in {"progress", "done"}:
            # A PR it delivers is its PR: reviews, comments and CI come back to it.
            self.runtime.follow(caller, threads.pull_requests(params.summary))
        if row["role"] == WORKER:
            self.runtime.watchdog.save(caller, nudges=0)
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
        draft = self.store.create_draft(
            caller,
            "slack",
            {"channel_id": params.channel_id, "thread_ts": params.thread_ts, "to": params.to},
            params.text,
            params.note,
        )
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

    async def t_draft_github_review(self, params: DraftGitHubReview, caller: str):
        self.caller(caller, WORKER)
        settings = self.store.settings()
        repo = params.repo.lower()
        if not (settings.github_enabled and repo in settings.github_repos):
            raise ValueError("Reviews can only be published on connected repositories.")
        if not params.body.strip():
            raise ValueError("The review is empty.")
        head = await self.runtime.github.head(repo, params.number)
        draft = self.store.create_draft(
            caller,
            "github",
            {
                "repo": repo,
                "number": params.number,
                "head_sha": head,
                "url": f"https://github.com/{repo}/pull/{params.number}",
            },
            params.body,
        )
        self.runtime.add_message(caller, "draft", params.body, {"draft_id": draft["id"]})
        self.bus.publish("draft", draft)
        self.runtime.alert(
            f"Review for {repo}#{params.number}",
            session_id=caller,
            needs_action=True,
            title="PR review to approve",
        )
        return f"Draft {draft['id']} is waiting for the owner's approval."

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
            distill=True,
        )

    async def t_propose_profile(self, params: ProposeProfile, caller: str):
        self.caller(caller, WORKER)
        if not self.store.get(f"distill:{caller}"):
            raise ValueError("Only a profile distillation worker can propose a profile.")
        text = params.markdown.strip()[:20000]
        if not text:
            raise ValueError("The profile is empty.")
        self.store.set("profile_draft", text)
        self.bus.publish("state")
        self.runtime.alert(
            "Your profile draft is ready to review in Settings.",
            session_id=self.runtime.router_id(),
            needs_action=True,
            title="Profile to review",
        )
        return "Proposed. The owner reviews it in Settings before it is used."

    async def save_profile(self, text: str):
        self.store.set("profile", text.strip())
        self.store.set("profile_draft", "")
        await self.runtime.refresh(ROUTER, SLACK)
        self.bus.publish("state")

    async def t_follow_thread(self, params: FollowThread, caller: str):
        self.caller(caller, WORKER, SLACK)
        return f"Following {', '.join(self.runtime.follow(caller, [params.thread]))}."

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
