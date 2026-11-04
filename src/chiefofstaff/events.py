"""What Copilot sessions report as they work: turns, tool calls, usage and errors.

Every event updates the session's state and timeline; shell commands become the
observed commands behind a task's receipts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

from . import receipts
from .envelope import from_owner
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
        code = receipts.exit_code(output, getattr(data, "success", None))
        self.store.execute(
            """UPDATE commands SET exit_code=?, output=?, ended_at=?
            WHERE session_id=? AND call_id=? AND ended_at IS NULL""",
            (code, output[-600:], now(), session_id, call_id),
        )

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
        owner = from_owner(getattr(event.data, "content", "") or "") or state.owner_turn
        state.owner_turn = False
        state.event_turn = not owner
        state.owner_reply = bool(owner)

    def assistant_message(self, row: dict, event: Any):
        data = event.data
        content = (getattr(data, "content", "") or "").strip()
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
        if row["role"] == ROUTER and context > ROUTER_CONTEXT:
            self.runtime.background(self.runtime.rotate_router())
        if row["role"] == WORKER:
            self.runtime.background(self.runtime.workers.pump())
            if settled:
                self.runtime.workers.complete_report(session_id)
                self.runtime.background(self.runtime.workers.ask_for_report(session_id))
            # A turn may have changed code whose evidence was already collected.
            self.runtime.background(self.runtime.task_actions.check_fresh(session_id))

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
