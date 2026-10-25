import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from .agent import Agent, CopilotAgent, DemoAgent
from .files import apply_files, prepare_files
from .models import AgentReply, AgentText, Feedback
from .store import Store, identifier, now, today
from .tools import ToolBroker, ToolLease

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled", "interrupted", "ignored"}


class Engine:
    def __init__(self, store: Store, agent: Agent | None = None):
        self.store = store
        self.agent = agent or CopilotAgent(store.directory / "empty-workspace")
        self.demo = DemoAgent()
        self.active: dict[str, asyncio.Task] = {}
        self.loop_task: asyncio.Task | None = None
        self.slack = None
        self.github = None
        self.approval_lock = asyncio.Lock()
        self.stopping = False
        self.progress = asyncio.Event()
        self.tools = ToolBroker(self)

    async def start(self):
        self.store.recover()
        self.loop_task = asyncio.create_task(self.loop())

    async def stop(self):
        self.stopping = True
        if self.loop_task:
            self.loop_task.cancel()
            await asyncio.gather(self.loop_task, return_exceptions=True)
        for job in self.active.values():
            job.cancel()
        await asyncio.gather(*self.active.values(), return_exceptions=True)
        self.active.clear()

    async def loop(self):
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Scheduler failed")
                self.store.event("error", "Scheduler failed; see terminal logs.")
            await asyncio.sleep(0.25)

    async def tick(self):
        self.active = {key: value for key, value in self.active.items() if not value.done()}
        for parent in self.store.all("SELECT * FROM tasks WHERE status='waiting_workers'"):
            children = self.store.children(parent["id"])
            if children and all(child["status"] in TERMINAL for child in children):
                if not parent["tool_driven"] and any(
                    child["status"] != "completed" for child in children
                ):
                    self.store.update_task(
                        parent["id"],
                        status="failed",
                        error="A worker did not complete. Retry failed workers before synthesis.",
                    )
                else:
                    self.store.update_task(parent["id"], status="queued")
        if self.store.settings().paused:
            return
        queued = self.store.all(
            """SELECT * FROM tasks WHERE status='queued'
            ORDER BY CASE WHEN kind='learn' THEN 1 ELSE 0 END,created_at LIMIT 100"""
        )
        active_tasks = [self.store.task(task_id) for task_id in self.active]
        lead_busy = any(task["role"] == "lead" for task in active_tasks)
        worker_count = sum(task["role"] == "worker" for task in active_tasks)
        for task in queued:
            if task["id"] in self.active:
                continue
            if task["role"] == "lead":
                if lead_busy:
                    continue
                lead_busy = True
            else:
                if worker_count >= self.store.settings().max_workers:
                    continue
                worker_count += 1
            self.store.update_task(task["id"], status="running")
            self.active[task["id"]] = asyncio.create_task(self.run_task(task["id"]))

    def context(self, task: dict) -> dict:
        memories = self.store.all(
            """SELECT m.text,m.scope,m.category,m.evidence FROM memories m
            LEFT JOIN tasks origin ON origin.id=m.task_id
            WHERE (m.scope='general' OR m.project_path=?)
            AND (?='demo' OR origin.backend IS NULL OR origin.backend!='demo')
            ORDER BY m.created_at DESC LIMIT 40""",
            (task["project_path"], task["backend"]),
        )
        decisions = self.store.all(
            """SELECT question,choice,reason FROM approvals
            WHERE task_id=? AND kind='decision' AND status='approved' ORDER BY created_at""",
            (task["id"],),
        )
        children = [
            {
                "id": child["id"],
                "title": child["title"],
                "status": child["status"],
                "result": child["result"][:4000],
                "error": child["error"],
            }
            for child in self.store.children(task["id"])
        ]
        actions = []
        for row in self.store.all(
            """SELECT id FROM approvals WHERE task_id=? AND kind!='decision'
            ORDER BY created_at DESC LIMIT 20""",
            (task["id"],),
        ):
            item = self.store.approval(row["id"])
            payload = item["payload"]
            if item["kind"] == "files":
                payload = {
                    "project_path": payload["project_path"],
                    "files": [file["path"] for file in payload["files"]],
                }
            actions.append(
                {key: item[key] for key in ("id", "kind", "question", "status", "choice", "reason")}
                | {"payload": payload}
            )
        return {
            "profile": self.store.settings().profile,
            "memories": memories,
            "decisions": decisions,
            "children": children,
            "today": today(),
            "daily_briefs": self.store.daily_briefs(task["project_path"]),
            "ongoing_work": self.store.all(
                """SELECT id,title,kind,status FROM tasks WHERE project_path=? AND id!=?
                AND parent_id IS NULL AND kind NOT IN ('distill','learn')
                AND status IN ('running','queued','waiting_workers','waiting_approval')
                ORDER BY created_at DESC LIMIT 20""",
                (task["project_path"], task["id"]),
            ),
            "previous_actions": list(reversed(actions)),
            "feedback": self.store.all(
                "SELECT text FROM feedback WHERE task_id=? ORDER BY created_at", (task["id"],)
            ),
        }

    async def run_task(self, task_id: str):
        task = self.store.task(task_id)
        token = None
        lease = None
        try:
            if task["rounds"] >= 8:
                raise ValueError(
                    "Task reached its eight-invocation limit; create a scoped follow-up."
                )
            self.store.update_task(task_id, rounds=task["rounds"] + 1, error="")
            context = self.context(task)
            if task["parent_id"]:
                context["parent_goal"] = self.store.task(task["parent_id"])["instructions"]
            selected = self.demo if task["backend"] == "demo" else self.agent

            def report(message: str):
                self.store.event("progress", message, task_id)

            if isinstance(selected, CopilotAgent):
                if not self.tools.endpoint:
                    raise RuntimeError("Start Work Buddy's local service to connect agent tools.")
                self.store.update_task(task_id, tool_driven=1, needs_resume=0)
                token, lease = self.tools.grant(task_id)
                reply = await selected.run(
                    task,
                    context,
                    self.store.settings(),
                    report,
                    tool_endpoint=self.tools.endpoint,
                    tool_token=token,
                )
            else:
                reply = await selected.run(task, context, self.store.settings(), report)
            self.store.save_artifact(task_id, task["rounds"] + 1, reply.model_dump())
            if isinstance(reply, AgentText):
                await self.finish_tool_run(task_id, reply, lease)
            else:
                await self.handle_reply(task, reply)
        except asyncio.CancelledError:
            if self.store.task(task_id)["status"] == "running":
                self.store.update_task(
                    task_id, status="interrupted", error="Execution stopped; retry explicitly."
                )
            raise
        except Exception as exc:
            logger.exception("Task %s failed", task_id)
            self.store.update_task(task_id, status="failed", error=str(exc)[:3000])
            self.store.event("error", str(exc)[:3000], task_id)
        finally:
            if token:
                self.tools.revoke(token)
            self.progress.set()

    async def finish_tool_run(self, task_id: str, reply: AgentText, lease: ToolLease | None):
        self.store.update_task(task_id, result=reply.message)
        task = self.store.task(task_id)
        if task["status"] in TERMINAL:
            return
        pending = self.store.one(
            "SELECT id FROM approvals WHERE task_id=? AND status IN ('pending','executing')",
            (task_id,),
        )
        children = self.store.children(task_id)
        seen = lease.seen_agents if lease else set()
        if pending:
            status = "waiting_approval"
        elif lease and lease.failures:
            raise RuntimeError(
                "Unresolved agent tool errors: " + "; ".join(lease.failures.values())
            )
        elif task["needs_resume"]:
            status = "queued"
        elif any(child["status"] not in TERMINAL for child in children):
            status = "waiting_workers"
        elif any(child["id"] not in seen for child in children):
            status = "queued"
        else:
            status = "completed"
        self.store.update_task(task_id, status=status)
        if lease and lease.learned and self.slack:
            await self.slack.notify_profile(task_id, lease.learned)
        if status == "completed":
            self.store.event("completed", reply.message[:1000], task_id)
            if self.slack and task["role"] == "lead":
                await self.slack.notify_result(task_id)

    async def handle_reply(self, task: dict, reply: AgentReply):
        """Structured simulator/test adapter; real CLI agents act through MCP tools."""
        task_id = task["id"]
        if task["kind"] in {"distill", "learn"} and (
            reply.action != "complete" or reply.files or reply.slack_reply or reply.github_review
        ):
            raise ValueError(
                "Profile learning can only propose memories, not work or side effects."
            )
        self.store.update_task(task_id, result=reply.message)
        if reply.action == "delegate":
            if task["role"] != "lead" or self.store.children(task_id):
                raise ValueError("Only a lead can delegate, and only once per task.")
            if self.store.settings().paused:
                self.store.event("info", "Delegated tasks will wait while paused.", task_id)
            for assignment in reply.assignments:
                self.store.create_task(
                    assignment.title,
                    assignment.instructions,
                    parent_id=task_id,
                    project_path=task["project_path"],
                    backend=task["backend"],
                    source={"read_project": task["source"].get("read_project", True)},
                )
            self.store.update_task(task_id, status="waiting_workers")
        elif reply.action == "ignore":
            if task["kind"] != "inbox":
                raise ValueError("Only discovered inbox work can be ignored by Lead.")
            self.store.update_task(task_id, status="ignored")
            self.store.event("triage", reply.message, task_id)
        elif reply.action == "decision":
            approval_id = self.store.create_approval(
                task_id,
                "decision",
                reply.message,
                reply.options,
                {},
            )
            self.store.update_task(task_id, status="waiting_approval")
            await self.notify_approval(approval_id)
        else:
            approvals = []
            if reply.files and task["source"].get("read_project") is False:
                raise ValueError("Remote review tasks cannot propose changes to the local project.")
            file_payload = prepare_files(task["project_path"], reply.files) if reply.files else None
            if task["backend"] == "demo" and (
                reply.files or reply.slack_reply or reply.github_review
            ):
                raise ValueError("Demo tasks cannot propose real side effects.")
            if reply.slack_reply and (
                not task["source"].get("channel") or not task["source"].get("thread_ts")
            ):
                raise ValueError("This task has no authorized Slack reply target.")
            if reply.github_review and (
                task["source"].get("type") != "github"
                or not all(task["source"].get(key) for key in ("repo", "number", "head_sha"))
            ):
                raise ValueError("This task has no authorized GitHub review target.")
            for memory in reply.memories:
                approvals.append(
                    self.store.create_approval(
                        task_id,
                        "memory",
                        "Confirm this candidate memory?",
                        ["Approve", "Reject"],
                        {**memory.model_dump(), "project_path": task["project_path"]},
                    )
                )
            if reply.files:
                approvals.append(
                    self.store.create_approval(
                        task_id,
                        "files",
                        "Apply these exact file changes?",
                        ["Approve", "Reject"],
                        file_payload,
                    )
                )
            if reply.slack_reply:
                source = task["source"]
                approvals.append(
                    self.store.create_approval(
                        task_id,
                        "slack",
                        "Send this exact reply to the source Slack thread?",
                        ["Approve", "Reject"],
                        {
                            "channel": source["channel"],
                            "thread_ts": source["thread_ts"],
                            "text": reply.slack_reply,
                        },
                    )
                )
            if reply.github_review:
                source = task["source"]
                approvals.append(
                    self.store.create_approval(
                        task_id,
                        "review",
                        "Publish this exact PR review comment?",
                        ["Approve", "Reject"],
                        {
                            "repo": source["repo"],
                            "number": source["number"],
                            "head_sha": source["head_sha"],
                            "body": reply.github_review,
                        },
                    )
                )
            self.store.update_task(
                task_id,
                status="waiting_approval" if approvals else "completed",
            )
            for approval_id in approvals:
                if self.store.approval(approval_id)["kind"] != "memory":
                    await self.notify_approval(approval_id)
            if reply.memories and self.slack:
                await self.slack.notify_profile(task_id, len(reply.memories))
            if not approvals:
                self.store.event("completed", reply.message[:1000], task_id)
                if self.slack and task["role"] == "lead":
                    await self.slack.notify_result(task_id)

    async def notify_approval(self, approval_id: str):
        if self.slack:
            await self.slack.notify_approval(approval_id)

    async def resolve(self, approval_id: str, choice: str, reason: str):
        async with self.approval_lock:
            approval = self.store.approval(approval_id)
            if approval["status"] != "pending":
                raise ValueError("This approval is no longer pending.")
            task = self.store.task(approval["task_id"])
            if task["status"] != "waiting_approval" and not (
                task["tool_driven"] and task["status"] in {"running", "waiting_workers", "queued"}
            ):
                raise ValueError("The task is not waiting for this approval.")
            if choice not in approval["options"]:
                raise ValueError("Choose one of the displayed options.")
            created = datetime.fromisoformat(approval["created_at"])
            if datetime.now(UTC) - created > timedelta(hours=24):
                self.store.execute(
                    "UPDATE approvals SET status='expired' WHERE id=?", (approval_id,)
                )
                self.store.update_task(
                    task["id"],
                    status="interrupted",
                    error="Approval expired. Retry for a new preview.",
                )
                self.store.execute(
                    "UPDATE approvals SET status='cancelled' WHERE task_id=? AND status='pending'",
                    (task["id"],),
                )
                raise ValueError("Approval expired after 24 hours; nothing was executed.")
            rejecting = approval["kind"] != "decision" and choice == "Reject"
            self.store.execute(
                "UPDATE approvals SET status=?,choice=?,reason=? WHERE id=?",
                ("rejected" if rejecting else "executing", choice, reason, approval_id),
            )
            try:
                if not rejecting:
                    if approval["kind"] == "files":
                        apply_files(approval["payload"])
                    elif approval["kind"] == "slack":
                        if not self.slack:
                            raise RuntimeError("Slack is not connected; no message was sent.")
                        await self.slack.send_reply(approval["payload"], approval_id)
                    elif approval["kind"] == "review":
                        if not self.github:
                            raise RuntimeError("GitHub review integration is unavailable.")
                        await self.github.publish_review(approval["payload"])
                    elif approval["kind"] == "memory":
                        payload = approval["payload"]
                        self.store.remember(
                            payload["text"],
                            payload["scope"],
                            payload["project_path"],
                            task["id"],
                            payload["category"],
                            payload["evidence"],
                        )
                    self.store.execute(
                        "UPDATE approvals SET status='approved' WHERE id=?", (approval_id,)
                    )
            except Exception as exc:
                # Network timeouts / partial file batches may have taken effect. Never replay.
                self.store.execute(
                    "UPDATE approvals SET status='uncertain' WHERE id=?", (approval_id,)
                )
                self.store.update_task(task["id"], status="interrupted", error=str(exc))
                self.store.execute(
                    "UPDATE approvals SET status='cancelled' WHERE task_id=? AND status='pending'",
                    (task["id"],),
                )
                self.store.event(
                    "error", f"Action outcome requires manual verification: {exc}", task["id"]
                )
                raise
            self.store.event(
                "decision", json.dumps({"choice": choice, "reason": reason}), task["id"]
            )
            if task["tool_driven"]:
                if approval["kind"] != "memory":
                    self.store.update_task(task["id"], needs_resume=1)
                remaining = self.store.one(
                    "SELECT id FROM approvals WHERE task_id=? AND status='pending'", (task["id"],)
                )
                job = self.active.get(task["id"])
                if not job or job.done():
                    current = self.store.task(task["id"])
                    children = self.store.children(task["id"])
                    status = (
                        "waiting_approval"
                        if remaining
                        else "queued"
                        if current["needs_resume"]
                        else "waiting_workers"
                        if any(child["status"] not in TERMINAL for child in children)
                        else "completed"
                    )
                    self.store.update_task(task["id"], status=status)
            elif approval["kind"] == "decision":
                self.store.update_task(task["id"], status="queued")
            elif not self.store.one(
                "SELECT id FROM approvals WHERE task_id=? AND status='pending'", (task["id"],)
            ):
                self.store.update_task(task["id"], status="completed")
                self.store.event(
                    "completed", "Deliverable reviewed; approval outcomes recorded.", task["id"]
                )
                if self.slack and task["role"] == "lead":
                    await self.slack.notify_result(task["id"])
            if approval["kind"] != "memory" and task["kind"] in {"work", "daily", "inbox"}:
                self.learn(
                    task["id"],
                    f"Decision about: {approval['question']}\nChoice: {choice}\n"
                    f"Owner's reason: {reason or '(not provided; do not invent one)'}",
                )

    def learn(self, task_id: str, feedback: str) -> str:
        task = self.store.task(task_id)
        return self.store.create_task(
            "Learn from feedback: " + task["title"][:150],
            "Analyze this actual feedback. Propose only well-supported, reusable lessons. "
            "Task-specific choices are not universal preferences. Output no memories if none "
            "are justified.\n"
            + json.dumps(
                {
                    "original_task": task["instructions"],
                    "agent_result": task["result"][:20000],
                    "human_feedback": feedback,
                    "reviewed_actions": self.context(task)["previous_actions"],
                },
                ensure_ascii=False,
            ),
            project_path=task["project_path"],
            backend=task["backend"],
            kind="learn",
        )

    def record_feedback(
        self,
        task_id: str,
        text: str,
        *,
        remember: bool = False,
        scope: str = "project",
    ) -> str:
        feedback = Feedback(text=text, remember=remember, scope=scope)
        task = self.store.task(task_id)
        self.store.execute(
            "INSERT INTO feedback VALUES(?,?,?,?)",
            (identifier(), task_id, feedback.text, now()),
        )
        if remember:
            self.store.remember(
                feedback.text,
                scope,
                task["project_path"],
                task_id,
                evidence="Owner explicitly confirmed this feedback as memory.",
            )
        learning_id = self.learn(task_id, feedback.text)
        self.store.event("feedback", "Owner feedback recorded for Lead learning.", task_id)
        return learning_id

    async def cancel(self, task_id: str):
        self.store.task(task_id)
        for child in self.store.children(task_id):
            if child["status"] not in TERMINAL:
                await self.cancel(child["id"])
        self.store.update_task(task_id, status="cancelled")
        self.store.execute(
            "UPDATE approvals SET status='cancelled' WHERE task_id=? AND status='pending'",
            (task_id,),
        )
        if job := self.active.get(task_id):
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self.store.event("cancelled", "Cancelled by owner.", task_id)
        self.progress.set()

    def retry(self, task_id: str):
        task = self.store.task(task_id)
        active = self.active.get(task_id)
        if task["status"] not in {"failed", "interrupted"} or (active and not active.done()):
            raise ValueError("Only stopped failed/interrupted tasks may be retried.")
        if self.store.one(
            "SELECT id FROM approvals WHERE task_id=? AND status='uncertain'", (task_id,)
        ):
            raise ValueError(
                "An action has an uncertain outcome. Verify it manually and create a new task."
            )
        if task["rounds"] >= 8:
            raise ValueError("Invocation limit reached. Create a new, narrower task.")
        self.store.execute(
            "UPDATE approvals SET status='cancelled' WHERE task_id=? AND status='pending'",
            (task_id,),
        )
        children = self.store.children(task_id)
        if (
            not task["tool_driven"]
            and children
            and any(child["status"] != "completed" for child in children)
        ):
            raise ValueError("Retry or resolve failed workers first.")
        self.store.update_task(task_id, status="queued", error="")
        if task["parent_id"]:
            parent = self.store.task(task["parent_id"])
            if parent["status"] != "cancelled":
                self.store.update_task(parent["id"], status="waiting_workers", error="")
        self.store.event("retry", "Owner explicitly retried task.", task_id)

    def snapshot(self) -> dict:
        return {
            "settings": self.store.settings().model_dump(),
            "tasks": self.store.all(
                """SELECT id,parent_id,title,role,kind,status,project_path,backend,error,created_at,
                updated_at FROM tasks ORDER BY created_at DESC LIMIT 200"""
            ),
            "approvals": [
                self.store.approval(row["id"])
                for row in self.store.all(
                    "SELECT id FROM approvals WHERE status='pending' ORDER BY created_at"
                )
            ],
            "memories": self.store.all("SELECT * FROM memories ORDER BY created_at DESC LIMIT 200"),
            "events": self.store.all("SELECT * FROM events ORDER BY id DESC LIMIT 100"),
            "slack": self.slack.status if self.slack else {"connected": False, "error": ""},
            "active_count": len(self.active),
            "timestamp": now(),
            "today": today(),
            "daily_briefs": self.store.daily_briefs(self.store.settings().project_path),
            "updates": self.store.all(
                """SELECT id,title,kind,status,error,created_at,updated_at,
                substr(result,1,1800) AS preview FROM tasks
                WHERE parent_id IS NULL AND kind IN ('daily','work','inbox')
                AND status!='ignored' ORDER BY created_at DESC LIMIT 50"""
            ),
            "initial_profile": self.store.one(
                """SELECT id,status,result,error FROM tasks
                WHERE kind='distill' AND project_path=? AND backend=?
                ORDER BY created_at DESC LIMIT 1""",
                (self.store.settings().project_path, self.store.settings().backend),
            ),
            "profile_tasks": self.store.all(
                """SELECT id,kind,status,result,error,created_at FROM tasks
                WHERE kind IN ('distill','learn') AND project_path=? AND backend=?
                ORDER BY created_at DESC LIMIT 20""",
                (self.store.settings().project_path, self.store.settings().backend),
            ),
            "github": self.github.status if self.github else {"connected": False, "error": ""},
        }
