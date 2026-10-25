import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from .agent import Agent, CopilotAgent, DemoAgent
from .files import apply_files, prepare_files
from .models import AgentReply
from .store import Store, now

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class Engine:
    def __init__(self, store: Store, agent: Agent | None = None):
        self.store = store
        self.agent = agent or CopilotAgent(store.directory / "empty-workspace")
        self.demo = DemoAgent()
        self.active: dict[str, asyncio.Task] = {}
        self.loop_task: asyncio.Task | None = None
        self.slack = None
        self.approval_lock = asyncio.Lock()
        self.stopping = False

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
        for parent in self.store.all("SELECT id FROM tasks WHERE status='waiting_workers'"):
            children = self.store.children(parent["id"])
            if children and all(child["status"] in TERMINAL for child in children):
                if any(child["status"] != "completed" for child in children):
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
            "SELECT * FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 100"
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
            {"title": child["title"], "status": child["status"], "result": child["result"][:16000]}
            for child in self.store.children(task["id"])
        ]
        return {
            "profile": self.store.settings().profile,
            "memories": memories,
            "decisions": decisions,
            "children": children,
            "previous_actions": self.store.all(
                """SELECT kind,question,status,choice FROM approvals
                WHERE task_id=? AND kind!='decision' ORDER BY created_at""",
                (task["id"],),
            ),
            "feedback": self.store.all(
                "SELECT text FROM feedback WHERE task_id=? ORDER BY created_at", (task["id"],)
            ),
        }

    async def run_task(self, task_id: str):
        task = self.store.task(task_id)
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
            reply = await selected.run(
                task,
                context,
                self.store.settings(),
                lambda message: self.store.event("progress", message, task_id),
            )
            self.store.save_artifact(task_id, task["rounds"] + 1, reply.model_dump())
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

    async def handle_reply(self, task: dict, reply: AgentReply):
        task_id = task["id"]
        if task["kind"] in {"distill", "learn"} and (
            reply.action != "complete" or reply.files or reply.slack_reply
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
                )
            self.store.update_task(task_id, status="waiting_workers")
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
            file_payload = prepare_files(task["project_path"], reply.files) if reply.files else None
            if task["backend"] == "demo" and (reply.files or reply.slack_reply):
                raise ValueError("Demo tasks cannot propose real side effects.")
            if reply.slack_reply and (
                not task["source"].get("channel") or not task["source"].get("thread_ts")
            ):
                raise ValueError("This task has no authorized Slack reply target.")
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
            self.store.update_task(
                task_id,
                status="waiting_approval" if approvals else "completed",
            )
            for approval_id in approvals:
                await self.notify_approval(approval_id)
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
            if task["status"] != "waiting_approval":
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
            if approval["kind"] == "decision":
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
            if reason and approval["kind"] != "memory" and task["kind"] == "work":
                self.learn(task["id"], f"Decision: {choice}\nOwner's reason: {reason}")

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
                },
                ensure_ascii=False,
            ),
            project_path=task["project_path"],
            backend=task["backend"],
            kind="learn",
        )

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

    def retry(self, task_id: str):
        task = self.store.task(task_id)
        if task["status"] not in {"failed", "interrupted"} or task_id in self.active:
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
        if children and any(child["status"] != "completed" for child in children):
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
        }
