"""What sessions need from the owner, what they learned about the machine, and what they ask
each other.

- Requests: Copilot's ask_user tool, MCP forms and links (elicitation) and MCP sign-ins become
  cards the owner answers in the UI; the session waits meanwhile.
- Environment notes: facts about tools, integrations and sign-ins that one session found out,
  shared with the Router and later workers.
- Session questions: a worker asks another worker (or the Router) and gets the answer back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel, Field

from .envelope import sender, short, wrap
from .models import DraftDecision
from .peers import clip
from .store import now

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

REQUEST_KINDS = {"question", "form", "link", "signin"}
LABELS = {"question": "Question", "form": "Form", "link": "Link to open", "signin": "Sign-in"}
# How long a session waits for the owner before it is told to go on without an answer.
REQUEST_TIMEOUT = float(os.environ.get("CHIEFOFSTAFF_REQUEST_TIMEOUT", "3600"))
MAX_NOTES = 40
MAX_OPEN_QUESTIONS = 5
# A question between sessions that nobody answers in this long is given up.
QUESTION_TTL = float(os.environ.get("CHIEFOFSTAFF_QUESTION_TTL", "7200"))
# What a session gets when the owner declines (or the card ends without an answer).
DECLINED = {
    "question": {"answer": "(The owner chose not to answer.)", "wasFreeform": True},
    "form": {"action": "decline"},
    "link": {"action": "decline"},
    "signin": {"action": "cancel"},
}
# After the owner says "Not now" to a sign-in, other sessions do not ask again for a while.
SIGNIN_QUIET = 600
# A worker's question to the owner, besides what it asks: why it needs them, the choices it
# gives (none: their own words), what it does once answered, and whether it is blocked. Those
# kept before these were asked for read as empty.
QUESTION_FIELDS: dict[str, Any] = {"kind": "question", "why": "", "options": [], "next": ""}


class RecordSetup(BaseModel):
    topic: str = Field(
        description="Short name of a tool, integration or service, e.g. 'Google Docs'"
    )
    note: str = Field(
        description="What works on this machine: which MCP server, CLI or app, how sign-in was "
        "done, caveats. Empty removes the topic. Never include passwords or tokens."
    )


class AskSession(BaseModel):
    session_id: str = Field(
        description="The session to ask: a worker id from find_sessions, or the Router's id"
    )
    question: str = Field(description="One clear question, with the context it needs")


class AnswerSession(BaseModel):
    question_id: str = Field(description="The question attribute of the question message")
    answer: str


def http_url(url: str) -> str:
    if not url.startswith(("https://", "http://")):
        raise ValueError("Only http(s) links can be shown to the owner.")
    return url


def choices(spec: dict) -> list[str]:
    """Allowed values of a form field: `enum`, or the `const`s of `oneOf`/`anyOf`."""
    if spec.get("enum"):
        return [str(value) for value in spec["enum"]]
    options = spec.get("oneOf") or spec.get("anyOf") or []
    return [str(o["const"]) for o in options if isinstance(o, dict) and "const" in o]


def form_values(schema: dict, content: dict[str, Any]) -> dict[str, Any]:
    """The owner's answers to an MCP form, checked against its (flat) JSON schema."""
    properties = schema.get("properties") or {}
    unknown = set(content) - set(properties)
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}")
    values: dict[str, Any] = {}
    for name, spec in properties.items():
        value = content.get(name)
        if value in (None, "", []):
            if name in (schema.get("required") or []):
                raise ValueError(f"{spec.get('title') or name} is required.")
            continue
        kind = spec.get("type", "string")
        try:
            if kind == "boolean":
                value = value if isinstance(value, bool) else str(value).lower() == "true"
            elif kind == "integer":
                value = int(value)
            elif kind == "number":
                value = float(value)
            elif kind == "array":
                value = [str(item) for item in (value if isinstance(value, list) else [value])]
            else:
                value = str(value)
        except (TypeError, ValueError):
            raise ValueError(f"{spec.get('title') or name} must be a {kind}.") from None
        allowed = choices(spec) or choices(spec.get("items") or {})
        chosen = value if isinstance(value, list) else [value]
        if allowed and any(item not in allowed for item in chosen):
            raise ValueError(f"{spec.get('title') or name} must be one of: {', '.join(allowed)}")
        values[name] = value
    return values


class Asks:
    """Requests to the owner, environment notes and questions between sessions."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.bus = runtime.bus
        self.states = runtime.states
        self.delivery = runtime.delivery
        # Open requests to the owner (draft id -> answer) and MCP sign-ins being completed.
        self.requests: dict[str, asyncio.Future] = {}
        self.signins: dict[tuple[str, str], asyncio.Event] = {}
        self.signin_locks: dict[str, asyncio.Lock] = {}
        self.signin_pending: dict[str, asyncio.Event] = {}
        self.signin_declined: dict[str, float] = {}

    # A worker's question to the owner (report "question" or "blocked"): it stays under Needs
    # you until the owner answers or dismisses it, whatever becomes of its task meanwhile (its
    # PR merged, the task cancelled or given more work).

    # One per task (a worker with several tasks may have a question on each; a new one on a task
    # replaces its last): its id is "<session id>:<task id>". Each question without a task (the
    # Slack session's, about different conversations) is one of its own: "<session id>:-:<n>".

    def migrate_questions(self):
        """Questions kept by 0.1.23 and before, one per worker ("ownerq:<session>"): under the
        task they were asked on, so answering that task clears them."""
        for key, raw in self.store.items("ownerq:").items():
            if ":" in key:
                continue
            with suppress(ValueError, TypeError):
                question = json.loads(raw)
                task = question.get("task_id") or ""
                target = f"ownerq:{key}:{task or '-'}"
                if not self.store.get(target):
                    self.store.set(target, json.dumps({**question, "task_id": task}))
            self.store.delete(f"ownerq:{key}")

    def record_question(
        self,
        session_id: str,
        text: str,
        task_id: str = "",
        about_draft: bool = False,
        *,
        why: str = "",
        options: Sequence[str] = (),
        next_step: str = "",
        kind: str = "question",
    ):
        """`about_draft`: its session says it only asks the owner to decide its draft (see
        Drafts.asked_with). `why` the owner is needed, the `options` it gives (none: their own
        words), what it does `next_step` once answered, and its `kind` ("question", or
        "blocked": it cannot go on without them): kept with it until it is answered."""
        text = text.strip()[:6000]
        key = f"ownerq:{session_id}:{task_id}"
        if not task_id:
            # Not the one it asked before (that one stays), unless it asks the same again.
            same = [
                question["id"]
                for question in self.session_questions(session_id)
                if not question.get("task_id") and question.get("text") == text
            ]
            key = f"ownerq:{same[-1]}" if same else f"ownerq:{session_id}:-:{uuid4().hex[:8]}"
        question: dict[str, Any] = {
            "text": text,
            "task_id": task_id,
            "at": now(),
            "kind": kind,
            "why": " ".join(why.split())[:1000],
            "options": [clean for option in options if (clean := " ".join(option.split())[:200])][
                :8
            ],
            "next": " ".join(next_step.split())[:1000],
        }
        if about_draft:
            question["about_draft"] = True
        self.store.set(key, json.dumps(question))
        self.runtime.publish_state()

    def owner_question(self, question_id: str) -> dict | None:
        """A question by its id; by "<session id>:-", that session's question without a task
        when it has one only (of several, which one is meant is not known); by a worker's id,
        its only one (or its newest)."""
        if found := self.store.get(f"ownerq:{question_id}"):
            return {**QUESTION_FIELDS, **json.loads(found), "id": question_id}
        session_id, _, task = question_id.partition(":")
        mine = self.session_questions(session_id)
        if task == "-":
            loose = [question for question in mine if not question.get("task_id")]
            return loose[0] if len(loose) == 1 else None
        if task:
            return None
        return mine[-1] if mine else None

    def session_questions(self, session_id: str) -> list[dict]:
        found = []
        for key in self.store.keys(f"ownerq:{session_id}"):
            rest = key.removeprefix(f"ownerq:{session_id}")
            if rest and not rest.startswith(":"):
                continue  # Another session whose id starts the same.
            question = json.loads(self.store.get(key) or "null")
            if question:
                found.append({**QUESTION_FIELDS, **question, "id": key.removeprefix("ownerq:")})
        return sorted(found, key=lambda question: question["at"])

    def answered(self, session_id: str, task_id: str | None = None, by: str = "owner"):
        """The owner answered (or someone did for them), or the worker moved on: the question
        of that task ("": those it asked with no task), or (no task named) all of the
        worker's. `by`: "owner" (the Router hears it is no longer open), "router" or
        "worker"."""
        questions = [
            question
            for question in self.session_questions(session_id)
            if task_id is None or (question.get("task_id") or "") == task_id
        ]
        gone = []
        for question in questions:
            if self.store.get(f"ownerq:{question['id']}"):
                self.store.delete(f"ownerq:{question['id']}")
                gone.append(question)
        if gone:
            self.closed(session_id, gone, "answered" if by == "owner" else "")
            self.runtime.publish_state()

    def closed(self, session_id: str, questions: list[dict], how: str):
        """A session's questions are no longer under Needs you: the Router's notes about them
        go with them. `how` the owner settled them ("answered", "dismissed"; "" when the Router
        or the worker did): the Router hears it with its next message (not a turn of its own),
        so it never calls them open."""
        ids = {question["id"] for question in questions}
        router = self.store.get("router_id") or ""
        for message in self.store.all(
            "SELECT id, meta FROM messages WHERE session_id=? AND kind='update' "
            "AND json_extract(meta, '$.questions') IS NOT NULL",
            (router,),
        ):
            if ids & set(json.loads(message["meta"]).get("questions") or []):
                self.store.set(f"updateseen:{message['id']}", now())
        row = self.store.find_session(session_id)
        if not how or not router or not row:
            return
        name = self.runtime.display_title(row)
        # As the owner reads it: "the Slack session's question", "“Pick a database”'s question".
        whose = "the Slack session" if row["role"] == "slack" else f"\u201c{name}\u201d"
        for question in questions:
            task = self.runtime.tasks.find(question.get("task_id") or "")
            on = f" on task {task['id']} “{task['title']}”" if task else ""
            asked = " ".join(question["text"].split())[:300]
            self.delivery.hold(
                router,
                wrap(
                    "system",
                    "question-closed",
                    f"The owner {how} “{name}”'s question{on} (“{asked}”): it is no longer open "
                    "under Needs you. Do not ask it again or call it open.",
                    worker=short(session_id),
                    task=task["id"] if task else None,
                ),
                kind="notice",
                display=f"You {how} {whose}'s question"[:300],
                meta={"session_id": session_id, **({"task_id": task["id"]} if task else {})},
            )

    def go_on(self, task: dict, chat: bool = True, told: bool = False) -> dict:
        """The owner answered what a task waited for (its question, its decision): it goes on
        now, not only once its worker reports again. Finished work it asked about goes back
        where it was (`chat`: what its worker says next is news on it, its result stays, unless
        it changes the task's code from here: then a new result). Its worker is told to restate
        the whole result in its next report, which is checked for it (`morefor`). `told`: the
        caller recorded where its checkouts are and tells its worker so itself. A task stopped
        (by the owner or the Router) stays stopped: only Go on resumes it."""
        from .prompts import restate

        tasks = self.runtime.tasks
        task = tasks.find(task["id"]) or task
        if task["status"] != "waiting" or self.store.get(f"stopped:{task['id']}"):
            return task
        if was := self.store.get(f"askedafter:{task['id']}"):
            task = tasks.move(task["id"], was)
            if chat:
                self.store.set(f"followup:{task['id']}", "chat")
            if task["result"]:
                self.store.set(f"morefor:{task['id']}", "1")  # Its next report is checked (Tools).
            if task["result"] and not told:
                # Its checkouts now: what it changes from here is a new result.
                self.runtime.background(self.runtime.task_actions.record_chat_base(task))
                note = wrap(
                    "system",
                    "context",
                    f"About task {task['id']} “{task['title']}”, which the owner's answer is "
                    f"for: {restate(tasks.criteria(task))}",
                )
                shown = f"Its next report on “{task['title']}” restates the whole result."
                row = self.store.find_session(task["session_id"])
                if row and row["status"] == "working":
                    # The answer reached it already: this goes into the same turn.
                    self.runtime.background(
                        self.delivery.deliver(
                            row["id"],
                            note,
                            kind="notice",
                            display=shown,
                            meta={"task_id": task["id"]},
                            steer=True,
                        )
                    )
                else:
                    # With the answer, which is sent next.
                    self.delivery.hold(
                        task["session_id"], note, kind="notice", display=shown,
                        meta={"task_id": task["id"]},
                    )  # fmt: skip
        else:
            task = tasks.move(task["id"], "running", reason="answer", progress="")
        if row := self.store.find_session(task["session_id"]):
            self.delivery.rest_after(row)  # A turn on it now: it reports, not rests.
        return task

    def dismiss(self, question_id: str, how: str = "dismissed"):
        if found := self.store.get(f"ownerq:{question_id}"):
            self.store.delete(f"ownerq:{question_id}")
            question = {**json.loads(found), "id": question_id}
            self.closed(question_id.split(":", 1)[0], [question], how)
            self.runtime.publish_state()
        elif ":" not in question_id:
            for question in self.session_questions(question_id):
                self.dismiss(question["id"], how)

    def owner_questions(self) -> list[dict]:
        """Open questions of workers whose task does not show them already (a task waiting for
        the owner is its own card under Needs you)."""
        found = []
        for key in self.store.keys("ownerq:"):
            question_id = key.removeprefix("ownerq:")
            session_id = question_id.split(":", 1)[0]
            row = self.store.find_session(session_id)
            question = json.loads(self.store.get(key) or "null")
            if not row or not question:
                continue
            task = self.runtime.tasks.find(question["task_id"]) if question["task_id"] else None
            if task and task["status"] == "waiting":
                continue
            found.append(
                {
                    "session_id": session_id,
                    "worker": self.runtime.display_title(row),
                    "closed": bool(row["closed_at"]),
                    "task_id": task["id"] if task else "",
                    "task_title": task["title"] if task else "",
                    "task_status": task["status"] if task else "",
                    **QUESTION_FIELDS,
                    **question,
                    "id": question_id,
                }
            )
        return sorted(found, key=lambda item: item["at"])

    # Requests

    def expire_requests(self, reason: str = "Chief of Staff restarted before an answer."):
        # A watch the Router proposed (Asks.offer) waits for nothing: it would never hear that
        # its card went. It does, with its next message, and may propose it again.
        for draft in self.store.drafts():
            if draft["kind"] == "form" and (what := draft["target"].get("watch")):
                text = (
                    f"Your proposed watch card {draft['id']} ({what}) expired unanswered: "
                    "Chief of Staff restarted. Nothing was added; propose it again with "
                    "add_watch if it is still wanted."
                )
                self.delivery.hold(
                    draft["session_id"],
                    wrap("system", "draft-outcome", text),
                    kind="notice",
                    display=f"Watch not added (its card expired at a restart): {what}"[:300],
                )
        kinds = ",".join(f"'{kind}'" for kind in sorted(REQUEST_KINDS))  # Constant SQL.
        self.store.execute(
            f"UPDATE drafts SET status='expired', outcome=?, resolved_at=? "  # noqa: S608
            f"WHERE status='pending' AND kind IN ({kinds})",
            (reason, now()),
        )

    def close_request(self, draft_id: str, status: str, outcome: str) -> dict:
        draft = self.store.update_draft(draft_id, status=status, outcome=outcome, resolved_at=now())
        self.bus.publish("draft", draft)
        return draft

    async def ask_owner(
        self,
        session_id: str,
        kind: str,
        target: dict,
        text: str,
        *,
        done: asyncio.Event | None = None,
    ) -> dict | None:
        """Show the owner a card and wait for the answer (or for `done`); None on timeout.

        The session shows as waiting while a card is open (see `view`); its stored status is
        left alone, so turns and reports keep their own bookkeeping.
        """
        row = self.store.session(session_id)
        draft = self.store.create_draft(session_id, kind, target, text)
        self.runtime.add_message(
            session_id, "draft", text, {"draft_id": draft["id"], "label": LABELS[kind]}
        )
        self.bus.publish("draft", draft)
        who = "Chief of Staff" if row["role"] != "worker" else self.runtime.display_title(row)
        self.runtime.alert(
            text, session_id=session_id, needs_action=True, title=f"{LABELS[kind]} · {who}"
        )
        answer = asyncio.get_running_loop().create_future()
        self.requests[draft["id"]] = answer
        self.states[session_id].asking[draft["id"]] = text
        self.runtime.publish_session(self.store.session(session_id))
        finished = asyncio.ensure_future(done.wait()) if done else None
        try:
            waiting = {answer} | ({finished} if finished else set())
            ready, _ = await asyncio.wait(
                waiting, timeout=REQUEST_TIMEOUT, return_when=asyncio.FIRST_COMPLETED
            )
            if answer in ready:
                return answer.result()
            if finished in ready:
                self.close_request(draft["id"], "answered", "Done")
                return {"action": "done"}
            self.close_request(draft["id"], "expired", "No answer in time.")
            return None
        finally:
            self.requests.pop(draft["id"], None)
            if finished:
                finished.cancel()
            self.states[session_id].asking.pop(draft["id"], None)
            if row := self.store.find_session(session_id):
                self.runtime.publish_session(row)

    def offer(
        self,
        session_id: str,
        kind: str,
        target: dict,
        text: str,
        then: Callable[[dict], Awaitable[None]],
        title: str = "",
    ) -> dict:
        """Show the owner a card whose answer is applied when it comes (`then`, in the
        background); nothing waits for it meanwhile (a Router's turn goes on). Like every
        request, it does not survive a restart."""
        draft = self.store.create_draft(session_id, kind, target, text)
        self.runtime.add_message(
            session_id, "draft", text, {"draft_id": draft["id"], "label": title or LABELS[kind]}
        )
        self.bus.publish("draft", draft)
        self.runtime.alert(
            text, session_id=session_id, needs_action=True, title=title or LABELS[kind]
        )
        answer = asyncio.get_running_loop().create_future()
        self.requests[draft["id"]] = answer

        def answered(found: asyncio.Future):
            self.requests.pop(draft["id"], None)
            if not found.cancelled():
                self.runtime.background(then(found.result()))

        answer.add_done_callback(answered)
        return draft

    def cancel_requests(self, session_id: str, reason: str, keep_questions: str | None = None):
        """The session is closed or stopped: its open cards end and it stops waiting.
        `keep_questions` (a task id, or "" for none): its task was cancelled, but a question it
        asked the owner still waits for them, as a question kept under Needs you (the answer
        then reaches the worker as a message)."""
        for draft_id in list(self.states[session_id].asking):
            draft = self.store.draft(draft_id)
            if (
                keep_questions is not None
                and draft["kind"] == "question"
                and draft["status"] == "pending"
            ):
                choices = draft["target"].get("choices") or []
                listed = f"\n\nChoices: {', '.join(map(str, choices))}" if choices else ""
                self.record_question(session_id, draft["text"] + listed, keep_questions)
            if (answer := self.requests.get(draft_id)) and not answer.done():
                answer.set_result(
                    {"action": "closed"} if draft["kind"] == "signin" else DECLINED[draft["kind"]]
                )
            if draft["status"] == "pending":
                self.close_request(draft_id, "expired", reason)

    def answer_in_chat(self, session_id: str, text: str) -> dict | None:
        """The owner wrote to a worker that is waiting on its question card (Copilot's
        ask_user): what they wrote answers it (the worker reads nothing else until then). The
        card it answered, or None."""
        for draft_id in list(self.states[session_id].asking):
            draft = self.store.draft(draft_id)
            answer = self.requests.get(draft_id)
            pending = draft["kind"] == "question" and draft["status"] == "pending"
            if pending and answer and not answer.done():
                return self.answer_request(draft, DraftDecision(action="answer", text=text))
        return None

    def answer_request(self, draft: dict, decision: DraftDecision) -> dict:
        answer = self.requests.get(draft["id"])
        if not answer or answer.done():
            self.close_request(draft["id"], "expired", "No longer waiting.")
            raise ValueError("This request is no longer active.")
        kind = draft["kind"]
        if decision.action == "reject":
            result = DECLINED[kind]
            draft = self.close_request(draft["id"], "rejected", decision.reason or "Declined")
        elif decision.action == "answer":
            outcome = "Done"
            if kind == "question":
                text = decision.text.strip()
                if not text:
                    raise ValueError("Write an answer or pick a choice.")
                choices = draft["target"].get("choices") or []
                result = {"answer": text, "wasFreeform": text not in choices}
                outcome = text[:500]
            elif kind == "form":
                content = form_values(draft["target"].get("schema") or {}, decision.content)
                result = {"action": "accept", "content": content}
                outcome = "Submitted"  # Values may be private; they go to the tool only.
            elif kind == "link":
                result = {"action": "accept"}
            else:
                result = {"action": "done"}
            draft = self.close_request(draft["id"], "answered", outcome)
        else:
            raise ValueError("Answer or decline this request.")
        answer.set_result(result)
        return draft

    # Copilot callbacks (workers only, except the sign-in check)

    async def on_user_input(self, request: dict, context: dict) -> dict:
        session_id = self.runtime.row_id(context.get("sessionId") or context.get("session_id", ""))
        question = (request.get("question") or "").strip() or "The session has a question."
        target = {
            "question": question[:2000],
            "choices": [str(choice)[:200] for choice in (request.get("choices") or [])][:8],
            "allow_freeform": request.get("allowFreeform", True) is not False,
        }
        result = await self.ask_owner(session_id, "question", target, question[:2000])
        if result is None:
            return {
                "answer": "(No answer from the owner in time. Go on without it or ask later.)",
                "wasFreeform": True,
            }
        return result

    async def on_elicitation(self, context: dict) -> dict:
        session_id = self.runtime.row_id(context.get("session_id", ""))
        source = context.get("elicitationSourceDisplayName") or context.get(
            "elicitationSource", "A tool"
        )
        message = (context.get("message") or "").strip()[:2000]
        if context.get("mode") == "url":
            try:
                url = http_url(context.get("url") or "")
            except ValueError:
                return {"action": "decline"}
            target = {"url": url, "source": source}
            result = await self.ask_owner(session_id, "link", target, f"{source}: {message}")
        else:
            target = {"schema": context.get("requestedSchema") or {}, "source": source}
            result = await self.ask_owner(session_id, "form", target, f"{source}: {message}")
        return result or {"action": "cancel"}

    async def on_mcp_auth(self, request: dict, context: dict) -> dict:
        """An MCP server needs sign-in: one card per server; later sessions reuse the sign-in.
        Another agent's sign-in is the owner's to run (its `command`): its card says it."""
        cancelled = {"kind": "cancelled"}
        command = str(request.get("command") or "")
        session_id = self.runtime.row_id(context.get("sessionId", ""))
        row = self.store.find_session(session_id)
        server = request.get("serverName", "")
        key = command or server  # Another agent's sign-in is not Copilot's, nor serves it.
        if not row or row["role"] != "worker" or not server:
            return cancelled  # Only workers use MCP tools; others must not prompt the owner.
        for _ in range(3):
            if pending := self.signin_pending.get(key):
                await pending.wait()  # Another session's card: its sign-in serves this one too.
            if time.monotonic() - self.signin_declined.get(key, -SIGNIN_QUIET) < SIGNIN_QUIET:
                return cancelled
            async with self.signin_locks.setdefault(key, asyncio.Lock()):
                if key in self.signin_pending:
                    continue
                session = self.states.handle(session_id)
                if not session:
                    return cancelled
                try:
                    if command:
                        url = ""  # The owner runs it; the agent keeps the sign-in.
                    else:
                        backend = self.runtime.connected()  # Also while still connecting.
                        url = await backend.mcp_sign_in_url(session, server)
                        url = http_url(url) if url else ""
                except Exception:
                    logger.warning("Could not start sign-in to %s.", server, exc_info=True)
                    return cancelled
                if not url and not command:
                    return cancelled  # Signed in already: Copilot keeps the credentials.
                finished = asyncio.Event()
                self.signin_pending[key] = finished
            connected = asyncio.Event()
            self.signins[(session_id, server)] = connected
            await self.tell_session(
                session_id,
                f"{server} needs sign-in: the owner now has a sign-in card for it. Its tools fail "
                "until then. Go on with other steps or wait; do not ask the owner to sign in "
                "yourself. You will be told when it works.",
            )
            try:
                target = {"server": server, "url": url}
                text = f"{server} needs you to sign in"
                if command:
                    target["command"] = command
                    text += f": run `{command}` in a terminal."
                result = await self.ask_owner(session_id, "signin", target, text, done=connected)
            finally:
                self.signins.pop((session_id, server), None)
                self.signin_pending.pop(key, None)
                finished.set()
            action = (result or {}).get("action")
            if action == "closed":
                return cancelled  # The session closed; another session may still ask.
            if command and action == "done":
                # Its MCP servers start with its next connection: after this turn.
                message = f"You are signed in to {server}; its tools work from your next turn."
                if row := self.store.find_session(session_id):
                    await self.runtime.refresh_session(row)
            elif connected.is_set() or action == "done":
                message = f"You are signed in to {server}; its tools are available now."
            else:
                if action == "cancel":
                    self.signin_declined[key] = time.monotonic()  # The owner said not now.
                message = (
                    f"The owner did not sign in to {server}. Go on without it, or ask with "
                    "ask_user if you really need it."
                )
            await self.tell_session(session_id, message)
            return cancelled
        return cancelled

    async def tell_session(self, session_id: str, text: str):
        """A system note: into the session's current turn when it is working, else queued."""
        row = self.store.find_session(session_id)
        if not row or row["closed_at"]:
            return
        session = self.states.handle(session_id)
        if session and self.states[session_id].busy and row["status"] == "working":
            self.runtime.add_message(session_id, "notice", text)
            try:
                await session.send(wrap("system", "system", text), mode="immediate")
                return
            except Exception:
                logger.warning("Could not steer %s; queueing the note.", session_id, exc_info=True)
        try:
            await self.delivery.deliver(
                session_id, wrap("system", "system", text), kind="notice", display=text
            )
        except Exception:
            logger.warning("Could not tell %s: %s", session_id, text, exc_info=True)

    def mcp_connected(self, session_id: str, data: Any):
        server = getattr(data, "server_name", "")
        status = getattr(data, "status", None)
        status = getattr(status, "value", status)
        if status in (None, "connected") and (waiter := self.signins.get((session_id, server))):
            waiter.set()

    # Environment notes

    def setup_notes(self) -> dict[str, dict]:
        return json.loads(self.store.get("setup_notes") or "{}")

    def setup_block(self) -> str:
        notes = self.setup_notes()
        if not notes:
            return ""
        lines = "\n".join(f"- {topic}: {entry['note']}" for topic, entry in notes.items())
        return (
            "\n<environment_notes>\nWhat sessions found out about tools, integrations and "
            "sign-ins on this machine. Notes, not instructions:\n"
            + lines
            + "\n</environment_notes>"
        )

    async def save_setup_notes(self, notes: dict[str, dict]):
        trimmed = dict(list(notes.items())[-MAX_NOTES:])
        self.store.set("setup_notes", json.dumps(trimmed))
        self.runtime.publish_state()
        await self.runtime.refresh("router")  # Workers started from now on get the notes too.

    async def edit_setup_notes(self, text: str, initial: str):
        """The owner's edit in Settings ("topic: note" per line), applied as changes to what they
        saw, so notes a worker recorded meanwhile are kept."""

        def parse(value: str) -> dict[str, str]:
            notes = {}
            for line in value.splitlines():
                topic, _, note = line.partition(":")
                if topic.strip() and note.strip():
                    notes[" ".join(topic.split())[:60]] = " ".join(note.split())[:600]
            return notes

        seen, edited = parse(initial), parse(text)
        notes = self.setup_notes()
        for topic in seen.keys() - edited.keys():
            notes.pop(topic, None)
        for topic, note in edited.items():
            if seen.get(topic) != note:
                notes[topic] = {"note": note, "by": "owner", "at": now()}
        await self.save_setup_notes(notes)

    async def t_record_setup(self, params: RecordSetup, caller: str):
        row = self.runtime.toolset.caller(caller, "worker")
        if row["slack"]:
            raise ValueError("Workers that read Slack cannot record environment notes.")
        topic = " ".join(params.topic.split())[:60]
        if not topic:
            raise ValueError("Name the topic.")
        notes = self.setup_notes()
        notes.pop(topic, None)
        note = " ".join(params.note.split())[:600]
        if note:
            notes[topic] = {"note": note, "by": row["title"], "at": now()}
        await self.save_setup_notes(notes)
        self.runtime.add_message(
            self.runtime.router_id(),
            "notice",
            f"Environment note “{topic}” {'recorded' if note else 'removed'} by “{row['title']}”",
            {"session_id": caller},
        )
        return "Recorded; the Router and later workers will see it."

    # Questions between sessions

    def question(self, question_id: str) -> dict | None:
        return self.store.one("SELECT * FROM questions WHERE id=?", (question_id,))

    def open_questions(self, asker: str) -> list[dict]:
        return self.store.all(
            "SELECT * FROM questions WHERE asker=? AND status='open' ORDER BY asked", (asker,)
        )

    def close_question(self, question: dict, status: str, answer: str = ""):
        self.store.execute(
            "UPDATE questions SET status=?, answer=?, closed=? WHERE id=?",
            (status, answer, time.time(), question["id"]),
        )

    async def expire_questions(self, moment: float | None = None) -> list[str]:
        """Questions nobody answered in time: the asker is told to go on without an answer."""
        moment = time.time() if moment is None else moment
        expired = []
        for question in self.store.all(
            "SELECT * FROM questions WHERE status='open' AND asked<?", (moment - QUESTION_TTL,)
        ):
            self.close_question(question, "expired")
            target = self.store.find_session(question["target"])
            name = target["title"] if target else "that session"
            await self.tell_session(
                question["asker"],
                f"Your question {question['id']} to “{name}” got no answer in "
                f"{QUESTION_TTL // 3600:.0f} hours. Go on without it, or ask again.",
            )
            expired.append(question["id"])
        return expired

    async def cancel_questions(self, session_id: str):
        """A closed session neither answers nor waits: questions to it end, the askers are told."""
        for question in self.store.all(
            "SELECT * FROM questions WHERE status='open' AND (asker=? OR target=?)",
            (session_id, session_id),
        ):
            self.close_question(question, "cancelled")
            if question["target"] == session_id:
                await self.tell_session(
                    question["asker"],
                    f"Your question {question['id']} will not be answered: that session closed.",
                )

    async def t_ask_session(self, params: AskSession, caller: str):
        asker = self.runtime.toolset.caller(caller, "worker")
        try:
            target = self.store.session(params.session_id)
        except LookupError:
            raise ValueError("No such session; use find_sessions.") from None
        if target["id"] == caller:
            raise ValueError("That is you.")
        if target["role"] not in {"worker", "router"}:
            raise ValueError("You can ask workers or the Router.")
        if target["role"] == "worker" and bool(target["slack"]) != bool(asker["slack"]):
            raise ValueError("That session's Slack access differs from yours; ask the Router.")
        if not params.question.strip():
            raise ValueError("The question is empty.")
        if len(self.open_questions(caller)) >= MAX_OPEN_QUESTIONS:
            raise ValueError("You have unanswered questions; wait for their answers first.")
        question_id = "q" + uuid4().hex[:8]
        self.store.execute(
            "INSERT INTO questions(id, asker, target, text, asked) VALUES (?,?,?,?,?)",
            (question_id, caller, target["id"], params.question[:4000], time.time()),
        )
        title = "the Router" if target["role"] == "router" else f"“{target['title']}”"
        try:
            if target["closed_at"]:
                await self.runtime.workers.reopen(target["id"])
            await self.delivery.deliver(
                target["id"],
                wrap(
                    sender(asker),
                    "question",
                    params.question[:4000],
                    question=question_id,
                    title=asker["title"],
                ),
                kind="inbound",
                display=params.question[:4000],
                meta={"asked_by": caller, "title": asker["title"], "question_id": question_id},
            )
        except Exception:
            self.store.execute("DELETE FROM questions WHERE id=?", (question_id,))
            raise
        self.runtime.add_message(
            caller,
            "ask",
            params.question[:4000],
            {"target": target["id"], "title": title, "question_id": question_id},
        )
        if target["role"] == "worker":
            self.runtime.peers.copy_to_router(
                f"“{asker['title']}” asked {title}: {clip(params.question, 600)}",
                {"peer_from": caller, "peer_to": target["id"]},
            )
        return (
            f'Asked {title} ({question_id}). The answer arrives as a type="answer" message; go on '
            "with other work meanwhile."
        )

    async def t_answer_session(self, params: AnswerSession, caller: str):
        row = self.runtime.toolset.caller(caller, "worker", "router")
        question = self.question(params.question_id)
        if not question or question["target"] != caller:
            raise ValueError("No such question for you.")
        if question["status"] != "open":
            raise ValueError(f"That question is {question['status']} already.")
        self.close_question(question, "answered", params.answer[:4000])
        asker = self.store.find_session(question["asker"])
        if not asker or asker["closed_at"]:
            return "The asker has closed; the answer was kept but not delivered."
        title = "the Router" if row["role"] == "router" else f"“{row['title']}”"
        await self.delivery.deliver(
            asker["id"],
            wrap(
                sender(row),
                "answer",
                params.answer[:4000],
                question=params.question_id,
                title=row["title"],
            ),
            kind="inbound",
            display=params.answer[:4000],
            meta={"answered_by": caller, "title": title, "question_id": params.question_id},
        )
        if row["role"] == "worker" and asker["role"] == "worker":
            self.runtime.peers.copy_to_router(
                f"“{self.runtime.display_title(row)}” answered "
                f"“{self.runtime.display_title(asker)}”: {clip(params.answer, 600)}",
                {"peer_from": caller, "peer_to": asker["id"]},
            )
        return "Delivered."
