import asyncio
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import ConfigDict, Field

from .files import prepare_files
from .models import AgentReply, Assignment, MemoryCandidate, ProposedFile, StrictModel

if TYPE_CHECKING:
    from .engine import Engine

AGENT_LIMIT = 32
DONE = {"completed", "failed", "cancelled", "interrupted", "ignored"}
FileName = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}$")]


class Empty(StrictModel):
    pass


class AgentID(StrictModel):
    agent_id: str


class WaitAgents(StrictModel):
    agent_ids: list[str] = Field(min_length=1, max_length=32)


class Decision(StrictModel):
    question: str = Field(min_length=1, max_length=5000)
    options: list[str] = Field(min_length=2, max_length=4)


class FileProposal(StrictModel):
    files: list[ProposedFile] = Field(min_length=1, max_length=10)


class Artifact(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    name: FileName
    content: str = Field(max_length=200000)


class ReadArtifact(AgentID):
    name: FileName


class Channel(StrictModel):
    channel: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=15, ge=1, le=50)
    cursor: str = Field(default="", max_length=1000)


class Thread(Channel):
    thread_ts: str = Field(pattern=r"^\d+(\.\d+)?$")


class SlackReply(StrictModel):
    channel: str = Field(min_length=1, max_length=100)
    thread_ts: str = Field(pattern=r"^\d+(\.\d+)?$")
    text: str = Field(min_length=1, max_length=12000)


class Review(StrictModel):
    body: str = Field(min_length=1, max_length=20000)


class Ignore(StrictModel):
    reason: str = Field(min_length=1, max_length=3000)


TOOL_SPECS: dict[str, tuple[type[StrictModel], str]] = {
    "buddy_status": (
        Empty,
        "Read your task, authorized Slack channels, agents, and runtime limits.",
    ),
    "spawn_agent": (
        Assignment,
        "Create an independent CLI agent with a focused goal. Returns its ID "
        "immediately; the service queues it within the concurrency limit.",
    ),
    "get_agent": (AgentID, "Read one of your agents' status, result, errors, and artifact names."),
    "wait_for_agents": (
        WaitAgents,
        "Wait up to 15 seconds for your agents. Do not busy-poll. If still "
        "running, finish your turn; the service resumes you when they finish.",
    ),
    "cancel_agent": (AgentID, "Stop an agent that you created. Cannot cancel another Lead's work."),
    "retry_agent": (
        AgentID,
        "Retry one of your failed/interrupted agents. Does not replay uncertain actions.",
    ),
    "request_decision": (
        Decision,
        "Ask the owner a concrete question. Returns pending or a previously "
        "recorded answer. If pending, stop and wait for the owner's decision.",
    ),
    "propose_files": (
        FileProposal,
        "Queue exact full-file changes for owner approval. Read existing "
        "files first. This does NOT change files or execute code.",
    ),
    "memory_candidate": (
        MemoryCandidate,
        "Persist an evidence-backed profile/learning observation "
        "locally. Only owner-confirmed candidates affect future tasks.",
    ),
    "write_artifact": (
        Artifact,
        "Save a draft in this task's local artifacts, not the project. "
        "Use for code/document drafts or research too large for a final message.",
    ),
    "read_artifact": (
        ReadArtifact,
        "Read an artifact belonging to you or one of your child agents.",
    ),
    "slack_recent_messages": (
        Channel,
        "Read a bounded recent history from an authorized Slack channel. "
        "Returns pagination information; this is not global Slack search.",
    ),
    "slack_read_thread": (
        Thread,
        "Read an authorized Slack text conversation before replying. "
        "Returns actual message timestamps and any pagination limit.",
    ),
    "slack_reply": (
        SlackReply,
        "Queue an exact reply to a thread you read or were assigned. "
        "The application sends it only after owner approval. Never claim it is sent "
        "while pending; retries reuse the same approval.",
    ),
    "github_review": (
        Review,
        "Queue a COMMENT review for the PR assigned to this task. "
        "The application checks the head and publishes only after owner approval.",
    ),
    "ignore_task": (
        Ignore,
        "Record why a discovered inbox event needs no action. Not for daily goals.",
    ),
    "report_blocker": (
        Ignore,
        "Mark this task as blocked/interrupted with an explicit reason. "
        "Use when you cannot actually complete the work.",
    ),
}
WORKER_TOOLS = {
    "buddy_status",
    "write_artifact",
    "read_artifact",
    "slack_recent_messages",
    "slack_read_thread",
    "report_blocker",
}
PROFILE_TOOLS = {
    "buddy_status",
    "write_artifact",
    "read_artifact",
    "memory_candidate",
    "report_blocker",
}


def allowed_tools(task: dict) -> list[str]:
    if task["role"] == "worker":
        return sorted(WORKER_TOOLS)
    if task["kind"] in {"distill", "learn"}:
        return sorted(PROFILE_TOOLS)
    return list(TOOL_SPECS)


@dataclass
class ToolLease:
    task_id: str
    expires: float
    seen_agents: set[str] = field(default_factory=set)
    seen_threads: set[tuple[str, str]] = field(default_factory=set)
    failures: dict[str, str] = field(default_factory=dict)
    learned: int = 0
    calls: int = 0


class ToolBroker:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.store = engine.store
        self.endpoint: str | None = None
        self.leases: dict[str, ToolLease] = {}

    def grant(self, task_id: str) -> tuple[str, ToolLease]:
        task = self.store.task(task_id)
        lease = ToolLease(task_id, time.monotonic() + self.store.settings().timeout_seconds + 30)
        lease.seen_agents = {
            child["id"] for child in self.store.children(task_id) if child["status"] in DONE
        }
        root = self.store.task(task["parent_id"]) if task["parent_id"] else task
        source = root["source"]
        if source.get("channel") and source.get("thread_ts"):
            lease.seen_threads.add((source["channel"], source["thread_ts"]))
        token = secrets.token_urlsafe(32)
        self.leases[token] = lease
        return token, lease

    def revoke(self, token: str):
        self.leases.pop(token, None)

    def authorize(self, token: str) -> ToolLease:
        lease = self.leases.get(token)
        if not lease or lease.expires < time.monotonic():
            raise PermissionError("Agent capability expired or invalid.")
        if self.store.task(lease.task_id)["status"] in DONE:
            raise PermissionError("This agent is no longer active.")
        return lease

    async def call(self, token: str, name: str, arguments: dict) -> dict:
        lease = self.authorize(token)
        task = self.store.task(lease.task_id)
        try:
            lease.calls += 1
            if lease.calls > 100:
                raise ValueError("Per-invocation tool-call limit reached.")
            if name not in allowed_tools(task):
                raise PermissionError("This role cannot use that tool.")
            params = TOOL_SPECS[name][0].model_validate(arguments)
            if task["backend"] == "demo" and name in {
                "slack_recent_messages",
                "slack_read_thread",
                "slack_reply",
                "github_review",
                "propose_files",
            }:
                raise PermissionError("Demo agents cannot perform real operations.")
            result = await getattr(self, name)(lease, params)
            lease.failures.pop(name, None)
            self.store.event("tool", name, task["id"])
            return result
        except Exception as exc:
            lease.failures[name] = str(exc)
            self.store.event("error", f"{name}: {exc}", task["id"])
            raise

    def owned_agent(self, lease: ToolLease, agent_id: str) -> dict:
        task = self.store.task(agent_id)
        if task["id"] != lease.task_id and task["parent_id"] != lease.task_id:
            raise PermissionError("Agent belongs to another task.")
        return task

    def artifact_folder(self, task_id: str) -> Path:
        return self.store.directory / "artifacts" / task_id

    def artifact_names(self, task_id: str) -> list[str]:
        folder = self.artifact_folder(task_id)
        if folder.is_symlink():
            raise ValueError("Artifact symlinks are not allowed.")
        return (
            sorted(
                path.name for path in folder.iterdir() if path.is_file() and not path.is_symlink()
            )
            if folder.exists()
            else []
        )

    async def buddy_status(self, lease, params):
        task = self.store.task(lease.task_id)
        return {
            "task_id": task["id"],
            "role": task["role"],
            "kind": task["kind"],
            "slack_channels": self.store.settings().slack_channels,
            "agents": [
                {"id": child["id"], "title": child["title"], "status": child["status"]}
                for child in self.store.children(task["id"])
            ],
            "concurrent_worker_limit": self.store.settings().max_workers,
            "total_agent_limit": AGENT_LIMIT,
            "artifacts": self.artifact_names(task["id"]),
        }

    async def spawn_agent(self, lease, params):
        task = self.store.task(lease.task_id)
        children = self.store.children(task["id"])
        for child in children:
            if child["title"] == params.title and child["instructions"] == params.instructions:
                return {"agent_id": child["id"], "status": child["status"], "reused": True}
        if len(children) >= AGENT_LIMIT or self.store.backlog_count() >= 100:
            raise ValueError(
                "Agent/task budget reached. Finish existing work before creating more."
            )
        child_id = self.store.create_task(
            params.title,
            params.instructions,
            parent_id=task["id"],
            project_path=task["project_path"],
            backend=task["backend"],
            source={"read_project": task["source"].get("read_project", True)},
        )
        return {"agent_id": child_id, "status": "queued"}

    async def get_agent(self, lease, params):
        task = self.owned_agent(lease, params.agent_id)
        if task["status"] in DONE:
            lease.seen_agents.add(task["id"])
        return {
            "agent_id": task["id"],
            "status": task["status"],
            "result": task["result"],
            "error": task["error"],
            "artifacts": self.artifact_names(task["id"]),
        }

    async def wait_for_agents(self, lease, params):
        for agent_id in params.agent_ids:
            if agent_id == lease.task_id:
                raise ValueError("An agent cannot wait for itself.")
            self.owned_agent(lease, agent_id)
        try:
            async with asyncio.timeout(15):
                while True:
                    self.engine.progress.clear()
                    results = [
                        await self.get_agent(lease, AgentID(agent_id=agent_id))
                        for agent_id in params.agent_ids
                    ]
                    if all(result["status"] in DONE for result in results):
                        return {"agents": results}
                    await self.engine.progress.wait()
        except TimeoutError:
            return {
                "agents": results,
                "pending": True,
                "instruction": "Yield; you will be resumed.",
            }

    async def cancel_agent(self, lease, params):
        if params.agent_id == lease.task_id:
            raise ValueError("Use the owner's task controls to cancel the Lead.")
        self.owned_agent(lease, params.agent_id)
        await self.engine.cancel(params.agent_id)
        return {"agent_id": params.agent_id, "status": "cancelled"}

    async def retry_agent(self, lease, params):
        if params.agent_id == lease.task_id:
            raise ValueError("The owner must retry the Lead through the task controls.")
        self.owned_agent(lease, params.agent_id)
        self.engine.retry(params.agent_id)
        return {"agent_id": params.agent_id, "status": "queued"}

    async def approval(self, lease, kind, question, options, payload):
        # Reuse exact proposals across retries, rather than creating duplicate external actions.
        for row in self.store.all("SELECT id FROM approvals WHERE task_id=?", (lease.task_id,)):
            prior = self.store.approval(row["id"])
            if (prior["kind"], prior["question"], prior["options"], prior["payload"]) == (
                kind,
                question,
                options,
                payload,
            ):
                return {
                    "approval_id": prior["id"],
                    "status": prior["status"],
                    "choice": prior["choice"],
                    "reason": prior["reason"],
                }
        approval_id = self.store.create_approval(lease.task_id, kind, question, options, payload)
        if kind == "memory":
            lease.learned += 1
        else:
            await self.engine.notify_approval(approval_id)
        current = self.store.approval(approval_id)
        return {
            "approval_id": approval_id,
            "status": current["status"],
            "choice": current["choice"],
            "reason": current["reason"],
            "executed": current["status"] == "approved" and kind in {"files", "slack", "review"},
        }

    async def request_decision(self, lease, params):
        AgentReply(action="decision", message=params.question, options=params.options)
        return await self.approval(lease, "decision", params.question, params.options, {})

    async def propose_files(self, lease, params):
        task = self.store.task(lease.task_id)
        if task["source"].get("read_project") is False:
            raise PermissionError("Remote review tasks cannot modify a local project.")
        payload = prepare_files(task["project_path"], params.files)
        return await self.approval(
            lease,
            "files",
            "Apply these exact file changes?",
            ["Approve", "Reject"],
            payload,
        )

    async def memory_candidate(self, lease, params):
        task = self.store.task(lease.task_id)
        return await self.approval(
            lease,
            "memory",
            "Confirm this candidate memory?",
            ["Approve", "Reject"],
            {**params.model_dump(), "project_path": task["project_path"]},
        )

    async def write_artifact(self, lease, params):
        folder = self.artifact_folder(lease.task_id)
        folder.mkdir(exist_ok=True, parents=True, mode=0o700)
        if len(list(folder.iterdir())) >= 10 and not (folder / params.name).exists():
            raise ValueError("This agent has reached its ten-artifact limit.")
        path = folder / params.name
        if path.is_symlink() or folder.is_symlink():
            raise ValueError("Artifact symlinks are not allowed.")
        path.write_text(params.content, encoding="utf-8")
        path.chmod(0o600)
        return {"artifact": params.name, "saved_locally": True}

    async def read_artifact(self, lease, params):
        task = self.owned_agent(lease, params.agent_id)
        path = self.artifact_folder(task["id"]) / params.name
        if not path.is_file() or path.is_symlink() or path.parent.is_symlink():
            raise ValueError("Artifact not found.")
        return {"name": params.name, "content": path.read_text(encoding="utf-8")}

    def slack_client(self, lease, channel):
        task = self.store.task(lease.task_id)
        root = self.store.task(task["parent_id"]) if task["parent_id"] else task
        settings = self.store.settings()
        allowed = set(settings.slack_channels)
        if root["source"].get("channel"):
            allowed.add(root["source"]["channel"])
        if channel not in allowed:
            raise PermissionError("This Slack channel is not authorized for this task.")
        if not settings.slack_enabled or not self.engine.slack or not self.engine.slack.client:
            raise RuntimeError("Slack is disconnected. Connect it in Settings first.")
        return self.engine.slack.client

    @staticmethod
    def message_page(response):
        return {
            "messages": [
                {
                    "user": message.get("user"),
                    "text": message.get("text", "")[:12000],
                    "ts": message.get("ts"),
                    "thread_ts": message.get("thread_ts"),
                }
                for message in response.get("messages", [])
            ],
            "has_more": bool(response.get("has_more")),
            "next_cursor": response.get("response_metadata", {}).get("next_cursor", ""),
        }

    async def slack_recent_messages(self, lease, params):
        client = self.slack_client(lease, params.channel)
        response = await client.conversations_history(
            channel=params.channel,
            limit=params.limit,
            cursor=params.cursor,
        )
        return self.message_page(response)

    async def slack_read_thread(self, lease, params):
        client = self.slack_client(lease, params.channel)
        response = await client.conversations_replies(
            channel=params.channel,
            ts=params.thread_ts,
            limit=params.limit,
            cursor=params.cursor,
        )
        lease.seen_threads.add((params.channel, params.thread_ts))
        return self.message_page(response)

    async def slack_reply(self, lease, params):
        self.slack_client(lease, params.channel)
        if (params.channel, params.thread_ts) not in lease.seen_threads:
            raise ValueError("Read this thread before proposing a reply.")
        return await self.approval(
            lease,
            "slack",
            "Send this exact Slack reply?",
            ["Approve", "Reject"],
            params.model_dump(),
        )

    async def github_review(self, lease, params):
        source = self.store.task(lease.task_id)["source"]
        if source.get("type") != "github":
            raise PermissionError("No PR is assigned to this task.")
        return await self.approval(
            lease,
            "review",
            "Publish this exact PR review comment?",
            ["Approve", "Reject"],
            {key: source[key] for key in ("repo", "number", "head_sha")} | {"body": params.body},
        )

    async def ignore_task(self, lease, params):
        task = self.store.task(lease.task_id)
        if task["kind"] != "inbox":
            raise PermissionError("Only discovered inbox events can be ignored.")
        if self.store.children(task["id"]) or self.store.one(
            "SELECT id FROM approvals WHERE task_id=? AND status='pending'", (task["id"],)
        ):
            raise ValueError("Resolve outstanding work before ignoring this event.")
        self.store.update_task(task["id"], status="ignored", result=params.reason)
        return {"status": "ignored"}

    async def report_blocker(self, lease, params):
        self.store.update_task(lease.task_id, status="interrupted", error=params.reason)
        return {"status": "interrupted", "reason": params.reason}
