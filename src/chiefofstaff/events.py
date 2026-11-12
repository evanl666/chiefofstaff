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
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

from . import prompts, receipts, threads
from .backend import running_background, stop_background
from .delivery import digest
from .envelope import from_owner, news_tasks, tasks_named, wrap
from .policy import owners_checkout, permission_denial, written_file
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


# Copilot's own tools, as what they do: "Reading app.py", not "view /w/app.py".
STEP_WORDS = {
    "view": "Reading {path}", "read": "Reading {path}", "read_file": "Reading {path}",
    "edit": "Editing {path}", "str_replace": "Editing {path}", "apply_patch": "Editing files",
    "str_replace_editor": "Editing {path}", "create": "Writing {path}", "write": "Writing {path}",
    "grep": "Searching the code for “{pattern}”", "rg": "Searching the code for “{pattern}”",
    "glob": "Finding files ({pattern})", "read_bash": "Reading a command's output",
    "read_powershell": "Reading a command's output", "write_bash": "Typing into a command",
    "write_powershell": "Typing into a command", "stop_bash": "Stopping a command",
    "stop_powershell": "Stopping a command", "list_bash": "Checking its commands",
    "web_fetch": "Reading {host}", "web_search": "Searching the web for “{query}”",
    "task": "Running a helper agent", "read_agent": "Reading a helper agent's result",
    "skill": "Loading a skill", "sql": "Updating its notes", "store_memory": "Saving a note",
    "fetch_copilot_cli_documentation": "Reading Copilot's docs",
}  # fmt: skip


def describe_step(name: str, arguments: Any) -> str:
    """A step as the owner reads it on a card: what it is for when the call says so (a shell
    command's description), else what the tool does in words (the command line for a shell
    command without a description)."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    arguments = arguments if isinstance(arguments, dict) else {}
    described = arguments.get("description")
    if isinstance(described, str) and described.strip():
        return " ".join(described.split())[:200]
    if words := STEP_WORDS.get(name):
        path = str(arguments.get("path") or arguments.get("file_path") or "")
        url = str(arguments.get("url") or "")
        found = {
            "path": path.rstrip("/").rpartition("/")[2] or "a file",
            "pattern": " ".join(str(arguments.get("pattern") or "").split())[:60],
            "query": " ".join(str(arguments.get("query") or "").split())[:60],
            "host": re.sub(r"^https?://([^/]+).*", r"\1", url) or "a web page",
        }
        return words.format(**found).replace(" ()", "").replace("“”", "…")[:200]
    if name.startswith("slack_"):
        return "Searching Slack" if "search" in name else "Reading Slack"
    if name.startswith(("github-", "github_")) or "github" in name:
        return "Reading GitHub"
    return describe_tool(name, arguments)


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
# Seconds after its loop went idle (assistant.idle) or a turn ended (assistant.turn_end, for a
# Copilot that does not say assistant.idle) with no session.idle: a command left running in the
# background holds it (see Events.held_idle).
HELD_AFTER_IDLE = float(os.environ.get("CHIEFOFSTAFF_HELD_AFTER_IDLE", "2"))
HELD_AFTER_TURN = float(os.environ.get("CHIEFOFSTAFF_HELD_AFTER_TURN", "8"))
# A shell command that opens a PR (its URL is in the output).
OPENS_PR = re.compile(r"\bgh\s+pr\s+create\b|\bhub\s+pull-request\b")
# A tool that opens one (a GitHub MCP server's).
OPENS_PR_TOOL = re.compile(r"create_pull_request|create_pr\b|pull_request_create", re.IGNORECASE)
RETRY_WINDOW = 600
RETRY_DELAY = float(os.environ.get("CHIEFOFSTAFF_RETRY_DELAY", "8"))
# Copilot will not run the model asked for (a model or option the company does not allow).
MODEL_REFUSED = re.compile(
    r"requested model is not supported|model[^.\n]{0,60}not (?:supported|available|enabled)|"
    r"model \"[\w.:/-]{1,80}\" is not (?:supported|available|enabled)|"
    r"not allowed to use (?:the )?model|context tier[^.\n]{0,40}not supported",
    re.IGNORECASE,
)
# Copilot kept a thinking effort the session's model takes none of ("Reasoning effort 'low' is
# not supported for model …"): chosen for the model the session had before.
EFFORT_REFUSED = re.compile(r"effort\b[^.\n]{0,40}\bnot supported", re.IGNORECASE)
ROUTER_CONTEXT = int(os.environ.get("CHIEFOFSTAFF_ROUTER_CONTEXT", "150000"))
# The Router's closing words in a turn without the owner that only say it has nothing to say
# ("(quiet)", as its prompt asks, or "Nothing more to route: …"): not shown to the owner.
NOTHING_TO_SAY = re.compile(
    r"^\W*(?:quiet|nothing (?:more |else |further |new )?(?:to (?:route|do|add|report|say|tell|"
    r"act on)|needed)|no (?:further |other )?(?:action|reply) (?:is )?(?:needed|required))\b",
    re.IGNORECASE,
)


# Copilot's tools that write files, by the argument naming the file.
EDIT_TOOLS = {"edit", "str_replace", "str_replace_editor", "create", "write", "write_file"}


def edited_paths(name: str, arguments: Any) -> list[str]:
    """The files a tool call writes (an edit, a new file, a patch)."""
    if not isinstance(arguments, dict):
        return []
    if name in EDIT_TOOLS and (path := arguments.get("path") or arguments.get("file_path")):
        return [str(path)]
    if name == "apply_patch":
        patch = str(arguments.get("input") or arguments.get("patch") or "")
        return re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE)
    return []


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
            # Another agent's usage, in its own ledger (never Copilot's), and its turns' end.
            "agent.usage": lambda row, event: self.runtime.record_agent_usage(row, event.data),
            "agent.settled": lambda row, event: self.runtime.agent_settled(row),
            "session.mcp_server_status_changed": self.mcp_status,
            "session.mcp_server_tools_loaded": self.mcp_status,
            "session.idle": self.idle,
            "assistant.idle": self.loop_idle,
            "assistant.turn_end": self.turn_end,
            "session.error": self.error,
            "system.notification": self.notification,
        }

    def record_command(self, session_id: str, call_id: str, arguments: dict):
        command = str(arguments.get("command") or "").strip()
        if not command:
            return
        # The run of the task its turn is on (a task given meanwhile is not what it runs); a
        # worker may go on after reporting (a final push): that still belongs to its task.
        turn = self.runtime.turn_task(session_id)
        run = (
            self.store.one(
                "SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (turn,)
            )
            if turn
            else None
        )
        run = run or self.tasks.run(session_id) or self.tasks.latest_run(session_id)
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
        if code is None and self.runtime.on_copilot(session_id):
            # From what Copilot's shell tool printed, else from its success. Another agent's
            # command without one has none: unknown stays unknown.
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
        if code == 0 and ran["run_id"] and OPENS_PR.search(ran["command"]):
            # The PR it opened is its task's own work (not one its report only mentions).
            run = self.store.one("SELECT task_id FROM runs WHERE id=?", (ran["run_id"],))
            if run and (urls := threads.PR_URL.findall(output)):
                self.runtime.task_actions.record_opened(
                    run["task_id"], [f"https://github.com/{r}/pull/{n}" for r, n in urls[:3]]
                )
        self.command_changed({**ran, "kind": kind})

    def opened_pr(self, session_id: str, output: str):
        """A PR a worker's tool opened: its turn's task's own work."""
        task_id = self.runtime.turn_task(session_id) or (
            (self.tasks.current(session_id) or {}).get("id") or ""
        )
        if task_id and (urls := threads.PR_URL.findall(output or "")):
            self.runtime.task_actions.record_opened(
                task_id, [f"https://github.com/{r}/pull/{n}" for r, n in urls[:1]]
            )

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
        if row["role"] == WORKER and not self.states[row["id"]].busy:
            self.states[row["id"]].woken = True
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

    def stopped_work(self, row: dict) -> bool:
        """Whether what it was on was called off (stopped, cancelled) and nothing else of it is
        open: a turn it starts on its own has nothing to do."""
        tasks = self.tasks.for_session(row["id"])
        if any(
            t["status"] in {"queued", "running"}
            or (t["status"] == "waiting" and not self.store.get(f"stopped:{t['id']}"))
            for t in tasks
        ):
            return False
        last = self.tasks.current(row["id"]) or (tasks[-1] if tasks else None)
        return bool(
            last and (last["status"] == "cancelled" or self.store.get(f"stopped:{last['id']}"))
        )

    async def stop_waking(self, session_id: str):
        if not (handle := self.states.handle(session_id)):
            return
        with suppress(Exception):
            await handle.abort()
        await stop_background(handle)
        self.states[session_id].busy = False
        self.runtime.update_session(session_id, status="idle", activity="")
        self.runtime.add_message(
            session_id,
            "notice",
            "It started again on its own when a command it had left running ended; what it was "
            "on is stopped or cancelled, so it was stopped again.",
        )

    def user_message(self, row: dict, event: Any):
        state = self.states[row["id"]]
        state.woken = False  # A turn on a message.
        state.echoes = True
        state.awaiting = None  # It took up what it was sent.
        self.delivery.echoed(row["id"], getattr(event.data, "content", "") or "")
        if row["role"] == WORKER and (named := tasks_named(getattr(event.data, "content", ""))):
            # What its turn is about now; steered into a turn on another task, it stays on that.
            delivery = getattr(getattr(event.data, "delivery", None), "value", "")
            self.runtime.took_up(row["id"], named, switch=delivery != "steering")
        elif row["role"] == WORKER and (named := news_tasks(getattr(event.data, "content", ""))):
            # News on a thread it follows for a task: its turn is on that task.
            delivery = getattr(getattr(event.data, "delivery", None), "value", "")
            self.runtime.took_up(row["id"], named, switch=delivery != "steering", news=True)
        seen = digest(getattr(event.data, "content", "") or "")
        if state.unread:
            # It sees a message now: that one (or, changed on the way, the oldest).
            state.unread.pop(seen if seen in state.unread else next(iter(state.unread)))
        if state.unseen:
            state.unseen.pop(seen if seen in state.unseen else next(iter(state.unseen)))
        state.after_abort = False  # A turn on what it was sent now: its steps are work again.
        owner = from_owner(getattr(event.data, "content", "") or "") or state.owner_turn
        state.owner_turn = False
        state.event_turn = not owner
        state.owner_reply = bool(owner)
        state.turn_decisions, state.turn_tasks = [], []
        state.turn_check_backs = set()
        state.turn_runs = {}
        state.turn_reported = set()
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
            if NOTHING_TO_SAY.match(content):
                return  # Nothing for the owner: nothing shown.
            # Results reach the owner through notify_owner; the closing remark is a footnote.
            self.runtime.add_message(row["id"], "assistant", content, {"internal": True})
        else:
            meta = {"narration": True} if narration else None
            self.runtime.add_message(
                row["id"], "assistant", content, meta, unread=row["role"] == ROUTER
            )

    def turn_start(self, row: dict, event: Any):
        state = self.states[row["id"]]
        woken, state.woken = state.woken and not state.busy, False
        if woken and row["role"] == WORKER and self.stopped_work(row):
            # Bug E13: stopped (its task stopped or cancelled), it started again on its own when
            # a command it had left running ended, and worked on what was called off.
            self.runtime.background(self.stop_waking(row["id"]))
            return
        self.states[row["id"]].awaiting = None  # It started.
        self.states[row["id"]].held = False
        self.states[row["id"]].turn_mark += 1
        if row["role"] == WORKER:
            self.states[row["id"]].busy = True
        if row["status"] == "idle":
            self.runtime.update_session(row["id"], status="working")

    def tool_start(self, row: dict, event: Any):
        data = event.data
        name = getattr(data, "tool_name", "") or "tool"
        self.states[row["id"]].turn_tools += 1
        arguments = getattr(data, "arguments", None)
        call = getattr(data, "tool_call_id", "") or f"step-{time.monotonic()}"
        if getattr(data, "parent_tool_call_id", None):
            # A sub-agent's steps show as its one step; the commands it runs (a test suite, a
            # flow, often run again there) are receipts all the same.
            self.observe_shell(row, name, call, arguments)
            return
        if row["role"] == WORKER and not self.states[row["id"]].after_abort:
            # Working again after a stop (told to go on in its chat, by the Router, nudged): the
            # task it was stopped on runs again.
            self.runtime.task_actions.resume_stopped(row["id"])
        if name in self.runtime.tools or name in PLUMBING_TOOLS:
            return  # Chief of Staff's own tools show as what they did.
        if row["role"] == WORKER and OPENS_PR_TOOL.search(name):
            self.states[row["id"]].pr_calls.add(call)
        if row["role"] == WORKER and (paths := edited_paths(name, arguments)):
            task = self.runtime.turn_task(row["id"]) or (
                (self.tasks.current(row["id"]) or {}).get("id") or ""
            )
            self.runtime.task_actions.touched(task, paths)
        summary = describe_tool(name, arguments)
        self.runtime.add_message(row["id"], "tool", summary)
        step = describe_step(name, arguments)
        self.states[row["id"]].steps[call] = (time.time(), step, summary)
        self.runtime.update_session(row["id"], activity=step)
        self.observe_shell(row, name, call, arguments)

    def observe_shell(self, row: dict, name: str, call: str, arguments: Any):
        """A worker's shell command is recorded for receipts; reading, typing into or stopping
        a shell may settle one Copilot left running there."""
        if row["role"] != WORKER:
            return
        if name in receipts.SHELL_TOOLS and isinstance(arguments, dict):
            self.record_command(row["id"], call, arguments)
        elif name in receipts.SHELL_READERS | receipts.SHELL_STOPPERS:
            shell = (
                str((arguments or {}).get("shellId") or "") if isinstance(arguments, dict) else ""
            )
            self.states[row["id"]].shell_calls[call] = (name, shell)

    def tool_complete(self, row: dict, event: Any):
        data = event.data
        sub = bool(getattr(data, "parent_tool_call_id", None))
        if not sub and self.states[row["id"]].steps.pop(
            getattr(data, "tool_call_id", "") or "", None
        ):
            self.runtime.publish_session(self.store.session(row["id"]))  # Its "now" changed.
        if row["role"] != WORKER:
            return
        result = getattr(data, "result", None)
        output = (getattr(result, "content", "") or "") if result else ""
        # Kept for the watchdog: the tail of the latest output shows hangs and errors.
        if output and not sub:
            self.states[row["id"]].last_output = output[-1500:]
        call = getattr(data, "tool_call_id", "") or ""
        if call in self.states[row["id"]].pr_calls:
            self.states[row["id"]].pr_calls.discard(call)
            if getattr(data, "success", True) is not False:
                self.opened_pr(row["id"], output)
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
            # How much the conversation holds: Copilot's input count includes what came from
            # the cache (19,334 with 7,694 read and 11,636 written); a count without it (the
            # model's own API) is added to it.
            given = getattr(data, "input_tokens", 0) or 0
            cached = sum(
                getattr(data, name, 0) or 0 for name in ("cache_read_tokens", "cache_write_tokens")
            )
            context = given if given >= cached else given + cached
            self.store.set(f"context:{session_id}", str(context))
        if (
            model
            and own
            # The session's own conversation: not a sub-agent's or a background call's model
            # (a cheaper one, say), nor one that Auto picked for this turn only.
            and getattr(data, "interaction_type", None) in (None, "conversation-agent")
            and getattr(data, "initiator", None) != "sub-agent"
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

    def loop_idle(self, row: dict, event: Any):
        """Its turn is over (assistant.idle); session.idle follows unless a command it left
        running in the background holds it."""
        if not getattr(event.data, "parent_tool_call_id", None):
            self.check_held(row, HELD_AFTER_IDLE)

    def turn_end(self, row: dict, event: Any):
        """A step of its turn ended (assistant.turn_end): the end of its turn when no step
        follows. Copilot says no assistant.idle while a message it queued waits for a command
        running in the background. A sub-agent's steps are steps of a call in its turn."""
        if not getattr(event.data, "parent_tool_call_id", None):
            self.check_held(row, HELD_AFTER_TURN)

    def check_held(self, row: dict, delay: float):
        if row["role"] == WORKER:
            mark = self.states[row["id"]].turn_mark
            self.runtime.background(self.held_idle(row["id"], mark, delay))

    async def held_idle(self, session_id: str, mark: int, delay: float):
        """Bug E13: a worker that left a command running in the background (a dev server
        started attached) ended its turn, but Copilot defers session.idle until that command
        ends, and it was taken to work for ever ("Thinking…"): what was sent to it then went
        into a turn that was over, and was lost. Its turn's end is taken now (as idle does),
        and what is sent to it goes in at once (see Delivery.flush)."""
        await asyncio.sleep(delay)
        state = self.states[session_id]
        if state.turn_mark != mark or not state.busy or state.held:
            return  # Another turn began, or it went idle as usual.
        if not (handle := self.states.handle(session_id)) or not await running_background(handle):
            return
        row = self.store.find_session(session_id)
        if state.turn_mark != mark or not state.busy or not row or row["closed_at"]:
            return
        if await self.delivery.take_back(session_id, handle):
            # What Copilot had queued for it goes in now: its turn goes on with that.
            return
        if state.turn_mark != mark or not state.busy:
            return
        logger.info("%s ended its turn with a command running in the background.", session_id)
        self.idle(row, None, held=True)

    def idle(self, row: dict, event: Any, held: bool = False):
        """Its turn is over (session.idle; or `held`: its loop is idle with a command it left
        running holding session.idle, see held_idle)."""
        session_id = row["id"]
        state = self.states[session_id]
        state.turn_mark += 1
        if state.held and not state.busy:
            # What it left running in the background ended, and it did not go on (Copilot
            # wakes it when a command ends; this one did not, or it was stopped): its turn's end
            # was taken already, what waited for its session to be free is due now: a
            # reconnect, or a report it owes.
            state.held = False
            if state.stale:
                self.runtime.background(self.runtime.reattach(session_id))
            elif row["role"] == WORKER and not state.after_abort:
                self.owes_report(row, state)
            return
        state.held = held
        state.busy = False
        self.delivery.handled(session_id)
        # Its session is free again: messages queue as usual. A turn that ended because it was
        # aborted (stopped, its task cancelled) owes no report.
        aborted, state.after_abort = state.after_abort, False
        state.steps.clear()
        state.retuned = False
        last = self.store.value("SELECT max(id) FROM messages WHERE session_id=?", (session_id,))
        self.store.set(f"idle:{session_id}", str(last or 0))
        status = row["status"]
        if status == "working":
            status = state.pop_resting("idle")
        if (
            status == "done"
            and row["role"] == WORKER
            and any(task["status"] == "running" for task in self.tasks.for_session(session_id))
        ):
            # One of its tasks is open (given or reopened during the turn, or one it did not
            # report on): it reports, or its answer is filed as its report. Never "done" with a
            # task left running.
            status = "idle"
        self.runtime.update_session(session_id, status=status, activity="")
        if (
            state.close_after_turn
            and row["role"] == WORKER
            and not self.runtime.special(row)
            and not self.runtime.workers.finished(session_id)
        ):
            # Its PR is merged or closed, but it still has work open (a question it just
            # asked, another task): it stays, and nothing of it is cancelled.
            state.close_after_turn = False
        # Decided now: closing or reattaching (which brings its own message) clears these.
        settled = not (state.close_after_turn or state.stale or state.after_refresh)
        if state.close_after_turn:
            self.runtime.background(self.runtime.workers.close(session_id))
        elif state.stale and not held:
            # Reconnected once its session is free: a held one's command would end with none
            # to wake (see the start of idle, for its session.idle).
            self.runtime.background(self.runtime.reattach(session_id))
        if row["role"] in {ROUTER, SLACK} and self.delivery.has_unsent(session_id):
            self.delivery.schedule_flush(session_id)  # Events that arrived during the turn.
        context = int(self.store.get(f"context:{session_id}") or 0)
        if row["role"] == ROUTER and context > self.runtime.router_context_limit(row):
            self.runtime.background(self.runtime.rotate_router())
        if row["role"] == WORKER:
            self.runtime.background(self.runtime.workers.pump())
            # Held: it waits for a command it left running, whose end wakes it to go on (a
            # report now would be asked of work in the middle); asked once it is free.
            if settled and not state.retrying and not aborted and not held:
                self.owes_report(row, state)
            # A turn may have changed code whose evidence was already collected.
            self.runtime.background(self.runtime.task_actions.check_fresh(session_id))

    def owes_report(self, row: dict, state):
        """Its turn is over: what it said is its report, or it is asked for one."""
        self.runtime.workers.complete_report(row["id"])
        self.runtime.background(self.runtime.workers.ask_for_report(row["id"]))
        self.news_unreported(row, state)

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
        self.states[session_id].awaiting = None  # Copilot said what went wrong.
        self.states[session_id].steps.clear()
        # Copilot's model and effort fallbacks: another agent's models are not Copilot's.
        copilot = self.runtime.on_copilot(session_id)
        if copilot and EFFORT_REFUSED.search(message) and self.runtime.effort_clearable(row):
            self.runtime.update_session(session_id, status="idle", activity="")
            self.runtime.background(self.runtime.clear_effort(row))
            return
        if copilot and MODEL_REFUSED.search(message):
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
        """Said once a day per plugin, as a note: the work itself goes on. The same hook fails
        in every session alike (a plugin's problem, or this machine's), so once is enough."""
        found = re.search(r'hook from "([^"]+)"', message)
        source = (found.group(1) if found else message.split(" failed", 1)[0])[:200]
        key = f"hookfail:{source}"
        if time.time() - float(self.store.get(key) or 0) < 86400:
            return
        self.store.set(key, str(time.time()))
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
        if (reason := owners_checkout(row, request, self.workspaces.checkouts(row))) or (
            not self.runtime.yolo(row)
            and (reason := permission_denial(row, request, self.workspaces.writes(row)))
        ):
            self.runtime.add_message(row["id"], "notice", reason)
            return PermissionDecisionReject(feedback=reason)
        if row["role"] == WORKER and getattr(request, "kind", "") == "write":
            self.store.record(row["id"], "files", [written_file(row["cwd"], request)])
        return PermissionDecisionApproveOnce()
