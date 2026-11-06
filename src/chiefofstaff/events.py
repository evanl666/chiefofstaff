"""What Copilot sessions report as they work: turns, tool calls, usage and errors.

Every event updates the session's state and timeline; shell commands become the
observed commands behind a task's receipts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

from . import prompts, receipts
from .delivery import digest
from .envelope import from_owner, wrap
from .policy import permission_denial, written_file
from .roles import ROUTER, SLACK, WORKER
from .store import now

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

# Copilot's own bookkeeping tools; they say nothing about the work.
PLUMBING_TOOLS = {"tool_search_tool", "report_intent"}


def describe_tool(name: str, arguments: Any) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    detail = ""
    if isinstance(arguments, dict):
        for key in (
            "command",
            "path",
            "file_path",
            "pattern",
            "query",
            "url",
            "channel_id",
            "description",
        ):
            if arguments.get(key):
                detail = " ".join(str(arguments[key]).split())
                break
    return f"{name} {detail}".strip()[:200]


# When the Router's context (prompt tokens of its last call) grows past this, a fresh Router
# session takes over with a summary.
# Failures on Copilot's side, gone when the turn is tried again.
TRANSIENT = re.compile(
    r"no response was returned|rate.?limit|too many requests|\b(429|500|502|503|504)\b|"
    r"overloaded|temporarily unavailable|service unavailable|internal server error|"
    r"timed? ?out|ECONNRESET|socket hang up|connection (reset|closed|error)|network error|"
    r"fetch failed|stream (ended|closed)",
    re.IGNORECASE,
)
RETRIES = 2
RETRY_WINDOW = 600
RETRY_DELAY = float(os.environ.get("CHIEFOFSTAFF_RETRY_DELAY", "8"))
# Copilot will not run the model asked for (a model or option the company does not allow).
MODEL_REFUSED = re.compile(
    r"requested model is not supported|model[^.\n]{0,60}not (?:supported|available|enabled)|"
    r"not allowed to use (?:the )?model|context tier[^.\n]{0,40}not supported",
    re.IGNORECASE,
)
ROUTER_CONTEXT = int(os.environ.get("CHIEFOFSTAFF_ROUTER_CONTEXT", "150000"))


class Events:
    """Turns session events into state, timeline entries, commands and usage."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.states = runtime.states
        self.tasks = runtime.tasks
        self.delivery = runtime.delivery
        self.workspaces = runtime.workspaces
        # The events Chief of Staff acts on; Copilot sends many more.
        self.handlers: dict[str, Callable[[dict, Any], None]] = {
            "user.message": self.user_message,
            "assistant.message": self.assistant_message,
            "assistant.turn_start": self.turn_start,
            "tool.execution_start": self.tool_start,
            "tool.execution_complete": self.tool_complete,
            "assistant.usage": self.usage,
            "session.mcp_server_status_changed": self.mcp_status,
            "session.mcp_server_tools_loaded": self.mcp_status,
            "session.idle": self.idle,
            "session.error": self.error,
            "system.notification": self.notification,
        }

    def record_command(self, session_id: str, call_id: str, arguments: dict):
        command = str(arguments.get("command") or "").strip()
        if not command:
            return
        # A worker may go on after reporting (a final push): that still belongs to its task.
        run = self.tasks.run(session_id) or self.tasks.latest_run(session_id)
        self.store.execute(
            """INSERT INTO commands(session_id, run_id, call_id, command, kind, started_at)
            VALUES (?,?,?,?,?,?)""",
            (
                session_id,
                run["id"] if run else "",
                call_id or "",
                command[:2000],
                receipts.kind(command),
                now(),
            ),
        )

    def finish_command(self, session_id: str, data: Any, output: str):
        call_id = getattr(data, "tool_call_id", "") or ""
        if not call_id:
            return
        ran = self.store.one(
            "SELECT * FROM commands WHERE session_id=? AND call_id=? AND ended_at IS NULL "
            "ORDER BY id DESC",
            (session_id, call_id),
        )
        if not ran:
            return
        error = getattr(data, "error", None)
        if getattr(data, "success", None) is False and error is not None and not output:
            # It never ran (refused, blocked, or stopped before it started): no failed run.
            message = str(getattr(error, "message", "") or error)
            self.store.execute(
                "UPDATE commands SET kind='', exit_code=NULL, output=?, ended_at=? WHERE id=?",
                (message[-600:], now(), ran["id"]),
            )
            self.command_changed(ran)
            return
        if shell := receipts.running_shell(output):
            # Still running past Copilot's wait, or in the background: its exit code comes
            # when the worker reads its shell or Copilot says it ended.
            self.store.execute(
                "UPDATE commands SET shell=?, output=? WHERE id=?",
                (shell, output[-600:], ran["id"]),
            )
            self.command_changed(ran)
            return
        execution = getattr(data, "shell_execution", None)
        code = getattr(execution, "exit_code", None)
        if code is None:
            code = receipts.exit_code(output, getattr(data, "success", None))
        self.settle_command(session_id, ran, code, output)

    def settle_command(self, session_id: str, ran: dict, code: int | None, output: str):
        """A command ended: its kind (a test run may show only by its output), and for test
        runs whether the tests passed rather than the command line."""
        kind = ran["kind"] or receipts.output_kind(ran["command"], output)
        if kind == "test":
            code = receipts.test_exit(ran["command"], output, code)
        if ran["ended_at"] and (kind, code) == (ran["kind"], ran["exit_code"]):
            return
        self.store.execute(
            "UPDATE commands SET kind=?, exit_code=?, output=?, ended_at=? WHERE id=?",
            (kind, code, output[-600:], ran["ended_at"] or now(), ran["id"]),
        )
        self.runtime.checks.on_command(session_id, ran["call_id"])
        self.command_changed({**ran, "kind": kind})

    def command_changed(self, ran: dict):
        """A test run, push or the like started or ended: its task's card shows it now, also
        when the task was reported already (work on its PR after the report)."""
        if not ran["kind"] or not ran["run_id"]:
            return
        run = self.store.one("SELECT task_id FROM runs WHERE id=?", (ran["run_id"],))
        task = self.tasks.get(run["task_id"]) if run else None
        if task:
            self.runtime.task_actions.publish(task)

    def shell_read(self, session_id: str, tool: str, shell: str, output: str):
        """The worker read, typed into or stopped a shell: a command Copilot left running there
        may have ended."""
        done = receipts.shell_done(output)
        shell = done[0] if done else shell
        if not shell:
            return
        ran = self.store.one(
            "SELECT * FROM commands WHERE session_id=? AND shell=? ORDER BY id DESC",
            (session_id, shell),
        )
        if not ran:
            return
        text = (ran["output"] + output) if ran["ended_at"] is None or done else ran["output"]
        if done:
            # Also after Copilot's notice settled it: what the worker reads now shows how the
            # tests went (`pytest | tail`), or that it ran tests at all.
            self.settle_command(session_id, ran, done[1], text)
        elif ran["ended_at"] is None and tool in receipts.SHELL_STOPPERS:
            self.store.execute(
                "UPDATE commands SET output=?, ended_at=? WHERE id=?",
                (text[-600:], now(), ran["id"]),
            )
            self.command_changed(ran)  # Stopped: no result.
        elif ran["ended_at"] is None:
            self.store.execute("UPDATE commands SET output=? WHERE id=?", (text[-600:], ran["id"]))

    def notification(self, row: dict, event: Any):
        """Copilot tells a session that a command it left running ended."""
        kind = getattr(event.data, "kind", None)
        if row["role"] != WORKER or getattr(kind, "type", "") != "shell_completed":
            return
        ran = self.store.one(
            "SELECT * FROM commands WHERE session_id=? AND shell=? AND ended_at IS NULL "
            "ORDER BY id DESC",
            (row["id"], getattr(kind, "shell_id", "") or ""),
        )
        if ran:
            self.settle_command(row["id"], ran, getattr(kind, "exit_code", None), ran["output"])

    def on_event(self, session_id: str, event: Any, source: Any = None):
        if source is not None and self.states.handle(session_id) is not source:
            # A Copilot session we let go of (replaced, refreshed or closed): its late events
            # belong to no live turn.
            logger.debug("Ignoring %s from a detached session.", getattr(event.type, "value", ""))
            return
        try:
            self.handle_event(session_id, event)
        except Exception:
            logger.exception("Failed to handle session event")

    def handle_event(self, session_id: str, event: Any):
        handler = self.handlers.get(getattr(event.type, "value", str(event.type)))
        row = self.store.find_session(session_id)
        if handler and row and not row["closed_at"]:
            handler(row, event)

    def user_message(self, row: dict, event: Any):
        state = self.states[row["id"]]
        state.echoes = True
        if state.unread:
            # It sees a message now: that one (or, changed on the way, the oldest).
            seen = digest(getattr(event.data, "content", "") or "")
            state.unread.pop(seen if seen in state.unread else next(iter(state.unread)))
        owner = from_owner(getattr(event.data, "content", "") or "") or state.owner_turn
        state.owner_turn = False
        state.event_turn = not owner
        state.owner_reply = bool(owner)
        state.turn_decisions, state.turn_tasks = [], []
        state.turn_tools = 0

    def assistant_message(self, row: dict, event: Any):
        data = event.data
        content = (getattr(data, "content", "") or "").strip()
        if row["role"] == ROUTER and content:
            self.store.delete(f"rotated:{row['id']}")  # The fresh session answers: it stays.
        if not content or getattr(data, "parent_tool_call_id", None):
            return
        # Said on the way to its next steps: narration, shown with the steps, not as an answer.
        narration = bool(getattr(data, "tool_requests", None))
        if row["role"] != WORKER and narration:
            return
        if row["role"] == WORKER:
            # What it said last: the latest word on its progress, shown at a glance.
            self.store.set(f"said:{row['id']}", json.dumps({"text": content[:1500], "at": now()}))
        if row["role"] == ROUTER and self.states[row["id"]].event_turn:
            # Results reach the owner through notify_owner; the closing remark is a footnote.
            self.runtime.add_message(row["id"], "assistant", content, {"internal": True})
        else:
            meta = {"narration": True} if narration else None
            self.runtime.add_message(
                row["id"], "assistant", content, meta, unread=row["role"] == ROUTER
            )

    def turn_start(self, row: dict, event: Any):
        if row["role"] == WORKER:
            self.states[row["id"]].busy = True
        if row["status"] == "idle":
            self.runtime.update_session(row["id"], status="working")

    def tool_start(self, row: dict, event: Any):
        data = event.data
        name = getattr(data, "tool_name", "") or "tool"
        self.states[row["id"]].turn_tools += 1
        if (
            name in self.runtime.tools
            or name in PLUMBING_TOOLS
            or getattr(data, "parent_tool_call_id", None)
        ):
            return  # Chief of Staff's own tools show as what they did; sub-agents as one step.
        arguments = getattr(data, "arguments", None)
        summary = describe_tool(name, arguments)
        self.runtime.add_message(row["id"], "tool", summary)
        call = getattr(data, "tool_call_id", "") or f"step-{time.monotonic()}"
        self.states[row["id"]].steps[call] = (time.time(), summary)
        self.runtime.update_session(row["id"], activity=summary)
        if row["role"] == WORKER and name in receipts.SHELL_TOOLS and isinstance(arguments, dict):
            self.record_command(row["id"], getattr(data, "tool_call_id", ""), arguments)
        elif row["role"] == WORKER and name in receipts.SHELL_READERS | receipts.SHELL_STOPPERS:
            shell = (
                str((arguments or {}).get("shellId") or "") if isinstance(arguments, dict) else ""
            )
            self.states[row["id"]].shell_calls[call] = (name, shell)

    def tool_complete(self, row: dict, event: Any):
        data = event.data
        if getattr(data, "parent_tool_call_id", None):
            return
        if self.states[row["id"]].steps.pop(getattr(data, "tool_call_id", "") or "", None):
            self.runtime.publish_session(self.store.session(row["id"]))  # Its "now" changed.
        if row["role"] != WORKER:
            return
        result = getattr(data, "result", None)
        output = (getattr(result, "content", "") or "") if result else ""
        # Kept for the watchdog: the tail of the latest output shows hangs and errors.
        if output:
            self.states[row["id"]].last_output = output[-1500:]
        shell_call = self.states[row["id"]].shell_calls.pop(getattr(data, "tool_call_id", ""), None)
        if shell_call:
            self.shell_read(row["id"], *shell_call, output)
        else:
            self.finish_command(row["id"], data, output)

    def usage(self, row: dict, event: Any):
        data = event.data
        session_id = row["id"]
        model = getattr(data, "model", "") or ""
        own = not getattr(event, "agent_id", None) and not getattr(
            data, "parent_tool_call_id", None
        )
        if own and row["role"] == ROUTER:
            context = sum(
                getattr(data, name, 0) or 0
                for name in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
            )
            self.store.set(f"context:{session_id}", str(context))
        if (
            model
            and own
            # The session's own conversation: not a sub-agent's or a background call's model
            # (a cheaper one, say), nor one that Auto picked for this turn only.
            and getattr(data, "interaction_type", None) in (None, "conversation-agent")
            and getattr(data, "initiator", None) not in ("sub-agent",)
            and not getattr(data, "is_auto", False)
            and not self.runtime.model_for(row)
            and not self.store.get(f"pinned:{session_id}")
        ):
            # Copilot resolves its default model on the first turn; remember which it is.
            # Sub-agents and sessions we pinned to a model do not show the default.
            self.store.set("copilot_default_model", model)
        run = self.tasks.run(session_id) if row["role"] == WORKER else None
        self.store.record_usage(
            session_id,
            row["role"],
            model,
            (getattr(getattr(data, "copilot_usage", None), "total_nano_aiu", 0) or 0) / 1e9,
            getattr(data, "cost", 0) or 0,
            getattr(data, "input_tokens", 0) or 0,
            getattr(data, "output_tokens", 0) or 0,
            run_id=run["id"] if run else "",
        )

    def mcp_status(self, row: dict, event: Any):
        self.runtime.asks.mcp_connected(row["id"], event.data)

    def idle(self, row: dict, event: Any):
        session_id = row["id"]
        state = self.states[session_id]
        state.busy = False
        state.steps.clear()
        state.retuned = False
        last = self.store.value("SELECT max(id) FROM messages WHERE session_id=?", (session_id,))
        self.store.set(f"idle:{session_id}", str(last or 0))
        status = row["status"]
        if status == "working":
            status = state.pop_resting("idle")
            task = self.tasks.current(session_id) if row["role"] == WORKER else None
            if task and task["status"] == "running":
                # Its task is open (given or reopened during the turn): it reports, or its
                # answer is filed as its report. Never "done" with the task left running.
                status = "idle"
        self.runtime.update_session(session_id, status=status, activity="")
        # Decided now: closing or reattaching (which brings its own message) clears these.
        settled = not (state.close_after_turn or state.stale or state.after_refresh)
        if state.close_after_turn:
            self.runtime.background(self.runtime.workers.close(session_id))
        elif state.stale:
            self.runtime.background(self.runtime.reattach(session_id))
        if row["role"] in {ROUTER, SLACK} and self.delivery.has_unsent(session_id):
            self.delivery.schedule_flush(session_id)  # Events that arrived during the turn.
        context = int(self.store.get(f"context:{session_id}") or 0)
        if row["role"] == ROUTER and context > self.runtime.router_context_limit(row):
            self.runtime.background(self.runtime.rotate_router())
        if row["role"] == WORKER:
            self.runtime.background(self.runtime.workers.pump())
            if settled and not state.retrying:
                self.runtime.workers.complete_report(session_id)
                self.runtime.background(self.runtime.workers.ask_for_report(session_id))
                self.news_unreported(row, state)
            # A turn may have changed code whose evidence was already collected.
            self.runtime.background(self.runtime.task_actions.check_fresh(session_id))

    def news_unreported(self, row: dict, state):
        """A followed PR or thread changed and the worker, its work finished, said nothing to
        the owner: asked once to tell them (and how it proposes to go on)."""
        task = self.tasks.current(row["id"])
        if not state.thread_turn or state.thread_nudged or (task and task["status"] == "running"):
            return
        state.thread_nudged = True
        self.runtime.background(
            self.delivery.deliver(
                row["id"],
                wrap("system", "news", prompts.THREAD_NEWS),
                kind="notice",
                display="Asked to tell you what changed on what it follows.",
            )
        )

    def error(self, row: dict, event: Any):
        session_id = row["id"]
        message = (getattr(event.data, "message", "") or "The session failed.")[:2000]
        if getattr(event.data, "error_type", "") == "hook":
            # A hook of one of the owner's Copilot plugins failed (in a sandboxed session it
            # cannot write outside the session's folders). Copilot goes on with the turn.
            self.hook_failed(row, message)
            return
        self.states[session_id].busy = False
        self.states[session_id].retuned = False
        self.states[session_id].steps.clear()
        if MODEL_REFUSED.search(message):
            # The company's policy, say: it recovers on another model instead of failing again
            # on every message.
            self.runtime.update_session(session_id, status="idle", activity="")
            self.runtime.background(self.refused(row, message))
            return
        if row["role"] == ROUTER and self.store.get(f"rotated:{session_id}"):
            self.runtime.update_session(session_id, status="idle", activity="")
            self.runtime.background(self.router_failed(row, message))
            return
        if TRANSIENT.search(message):
            if self.retry_turn(row, message):
                return
            message = f"Copilot failed on this turn {RETRIES + 1} times: {message}"
        self.runtime.add_message(session_id, "error", message)
        if row["role"] == WORKER:
            self.runtime.update_session(session_id, status="failed", activity="")
            self.runtime.task_actions.fail(session_id, message)
            task = self.tasks.current(session_id)
            self.runtime.background(self.runtime.tell_router(row, "failed", message, task))
            self.runtime.background(self.runtime.workers.pump())
        else:
            self.runtime.update_session(session_id, status="idle", activity="")
            if row["role"] in {ROUTER, SLACK} and self.delivery.has_unsent(session_id):
                self.delivery.schedule_flush(session_id)

    def retry_turn(self, row: dict, message: str) -> bool:
        """Copilot failed on its side (no answer, overloaded, a dropped connection): the turn is
        tried again, not left to the owner. As it was given when nothing was done yet; after
        some steps, a note to finish it (sending it again could do those steps twice). Twice
        within ten minutes; False after that, and the error is shown."""
        session_id = row["id"]
        state = self.states[session_id]
        recent = json.loads(self.store.get(f"retries:{session_id}") or "[]")
        recent = [at for at in recent if time.time() - at < RETRY_WINDOW]
        if len(recent) >= RETRIES:
            self.store.delete(f"retries:{session_id}")
            return False
        self.store.set(f"retries:{session_id}", json.dumps([*recent, time.time()]))
        acted = state.turn_tools > 0
        state.turn_tools = 0
        if not acted and (before := self.store.get(f"prevsent:{session_id}")) is not None:
            self.store.set(f"sent:{session_id}", before)  # What the failed turn was given.
        elif acted:
            note = (
                f"Your last turn stopped with an error on Copilot's side ({message.strip()[:200]}) "
                "after some steps. Check what is done, and finish what is left of it."
            )
            self.runtime.add_message(
                session_id, "notice", note, {"prompt": wrap("system", "retry", note)}
            )
        if row["role"] != WORKER:
            self.runtime.update_session(session_id, status="idle", activity="")
            self.delivery.schedule_flush(session_id, RETRY_DELAY * (len(recent) + 1))
        else:
            # Its task stays at work; its slot is free until the turn is sent again.
            state.retrying = True
            self.runtime.update_session(session_id, status="idle", activity="")
            self.runtime.background(self.resend_later(session_id, len(recent) + 1))
        logger.info("Retrying %s after %s", session_id, message[:200])
        return True

    async def resend_later(self, session_id: str, attempt: int):
        try:
            await asyncio.sleep(RETRY_DELAY * attempt)
            await self.delivery.resume(session_id)
        finally:
            self.states[session_id].retrying = False

    async def router_failed(self, row: dict, message: str):
        """A Router turn failed: if a fresh session had just taken over, the earlier one goes
        on; otherwise the error stands."""
        if not await self.runtime.undo_rotation(message):
            self.runtime.add_message(row["id"], "error", message)
            if self.delivery.has_unsent(row["id"]):
                self.delivery.schedule_flush(row["id"])

    async def refused(self, row: dict, message: str):
        if await self.runtime.recover_model(row, message):
            return
        # Nothing left to fall back to: an error as any other.
        self.runtime.add_message(row["id"], "error", message)
        if row["role"] == WORKER:
            self.runtime.update_session(row["id"], status="failed", activity="")
            self.runtime.task_actions.fail(row["id"], message)
            task = self.tasks.current(row["id"])
            await self.runtime.tell_router(row, "failed", message, task)

    def hook_failed(self, row: dict, message: str):
        """Said once per session and plugin, as a note: the work itself goes on."""
        found = re.search(r'hook from "([^"]+)"', message)
        source = (found.group(1) if found else message.split(" failed", 1)[0])[:200]
        if self.store.get(f"hookfail:{row['id']}:{source}"):
            return
        self.store.set(f"hookfail:{row['id']}:{source}", "1")
        sandboxed = "sandbox" in message
        hint = (
            " If that plugin needs to write somewhere, add the folder under Settings → Workers → "
            "Also writable in the sandbox."
            if sandboxed
            else ""
        )
        self.runtime.add_message(
            row["id"],
            "notice",
            f"A Copilot plugin hook failed; the session goes on. {message}{hint}",
        )

    async def on_permission(self, request: Any, invocation: dict):
        row = self.store.find_session(self.runtime.row_id(invocation.get("session_id", "")))
        if not row:
            return PermissionDecisionReject(feedback="Unknown session.")
        if not self.runtime.yolo(row) and (
            reason := permission_denial(row, request, self.workspaces.writable(row))
        ):
            self.runtime.add_message(row["id"], "notice", reason)
            return PermissionDecisionReject(feedback=reason)
        if row["role"] == WORKER and getattr(request, "kind", "") == "write":
            self.store.record(row["id"], "files", [written_file(row["cwd"], request)])
        return PermissionDecisionApproveOnce()
