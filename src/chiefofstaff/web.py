import asyncio
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import agents
from .bus import Bus
from .models import (
    AgentInstall,
    AgentOffer,
    AgentSettings,
    AgentSetupInput,
    AgentSignIn,
    ApproveSafe,
    ArtifactHide,
    CheckBackAction,
    ContinueOn,
    DraftDecision,
    FocusRequest,
    FollowRetry,
    GitIdentity,
    GitQuarantine,
    MergeSubjects,
    NewLesson,
    OpenFolder,
    OpenPr,
    OwnerMessage,
    ProfileText,
    ProjectPath,
    RefusalAnswer,
    RestartOn,
    RunsEnded,
    SessionModel,
    SessionYolo,
    Settings,
    SetupNotes,
    ShareCreate,
    SharedLink,
    ShareFile,
    SkillUpload,
    SlackToken,
    TaskNote,
    TeamNudge,
    TeamSeen,
    TeamTell,
    Unfollow,
    UpdateRequest,
)
from .pagestate import page_state
from .runtime import Runtime
from .shares import ChangedError
from .sharing import link_alone
from .storage import StorageDelete, StorageKeep, StorageSession
from .store import Store
from .updates import installed

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
# How long the server waits for the runtime to start before it serves the page all the same
# (seconds): the browser is opened once it answers (__main__.open_when_ready, about 10 s).
START_WAIT = float(os.environ.get("CHIEFOFSTAFF_START_WAIT", "8"))


def started_late(starting: asyncio.Future) -> None:
    """A start that went on after the page was served: its failure said in the log."""
    if not starting.cancelled() and (error := starting.exception()):
        logger.error("Chief of Staff did not start fully.", exc_info=error)


class ShepherdDecision(BaseModel):
    """The owner's decision on a prepared plan (shepherd.py)."""

    action: Literal["approve", "dismiss", "changes"]
    replies: dict[str, str] = {}
    resolve: list[str] = []
    choice: dict[str, Literal["post", "their-way", "leave"]] = {}
    disclosure: Literal["", "yes", "no"] = ""
    text: str = ""


class ShepherdAction(BaseModel):
    """The owner's action on a PR Prepared for you keeps moving, or on a row it found."""

    target: str
    action: Literal["fix", "resume", "keep", "merge-when-ready", "accept-head", "hand"]
    on: bool = True
    sha: str = ""


class Login(BaseModel):
    token: str


ATTACHMENT_FILE = re.compile(r"[0-9a-f]{32}\.(?:png|jpg|gif|webp)")
# What each list in Settings holds, for "At most 10 MCP servers." (not pydantic's "Dictionary
# should have at most 10 items after validation").
HOLDS = {
    "projects": "other project folders",
    "sandbox_writable": "folders writable in the sandbox",
    "github_repos": "GitHub repositories",
    "skill_folders": "skill folders",
    "plugins": "plugins",
    "mcp_servers": "MCP servers",
    "mcp_servers_off": "MCP servers",
    "watches": "watches",
}


def in_words(problem: Any) -> Any:
    """A Settings problem as the owner reads it: too many entries in a list, said in words."""
    loc = problem["loc"]
    if problem["type"] == "too_long" and len(loc) == 1 and loc[0] in HOLDS:
        limit = (problem.get("ctx") or {}).get("max_length")
        return {**problem, "type": "value_error", "msg": f"At most {limit} {HOLDS[loc[0]]}."}
    return problem


def settings_from(values: dict[str, Any], old: Settings) -> Settings:
    """Settings from what was saved (`old`) and what the page sent: one invalid is answered as
    FastAPI answers an invalid body (422, each problem with its field). A saved MCP server
    Copilot would refuse, sent back as it was, stays off (Settings.mcp_off), not refused; so
    does a saved project whose folder is gone (it shows as not found)."""
    context = {
        "mcp_saved": {**old.mcp_servers, **old.mcp_servers_off},
        "projects_saved": old.projects,
        "writable_saved": old.sandbox_writable,
        "scratch_saved": old.scratch_directory,
    }
    try:
        return Settings.model_validate(values, context=context)
    except ValidationError as error:
        problems = error.errors(include_url=False)
        raise RequestValidationError(
            [{**in_words(problem), "loc": ("body", *problem["loc"])} for problem in problems]
        ) from None


MODEL_FIELDS = {"router": "Router", "worker": "Workers", "slack": "Slack", "watchdog": "Watchdog"}


# The page's own id for one send (models.OwnerMessage.client_id).
CLIENT_ID = re.compile(r"[0-9a-f]{32}")

# A worker's start line (Started, Queued, Restarted): its chip says its agent (Copilot's too).
START_LINE = re.compile(r"^(Started|Queued|Restarted)\b")


def with_agents(store: Any, rows: list[dict]) -> list[dict]:
    """Messages as the page shows them, each start line of a worker with the agent it runs on
    (`meta.agent`; a line from before 0.1.59 had none: its worker's, none for Copilot's), so a
    closed worker's line says it too."""
    seen: dict[str, str] = {}
    found = []
    for row in rows:
        meta = row.get("meta")
        if not isinstance(meta, dict):
            found.append(row)
            continue
        worker = meta.get("session_id")
        if (
            row.get("kind") != "notice"
            or not isinstance(worker, str)
            or "agent" in meta
            or not START_LINE.match(str(row.get("text") or ""))
        ):
            found.append(row)
            continue
        if worker not in seen:
            seen[worker] = store.get(f"agent:{worker}") or agents.COPILOT
        found.append({**row, "meta": {**meta, "agent": seen[worker]}})
    return found


def shown(message: dict) -> dict:
    """A message as the page shows it: without the prompt it was sent as (as long again)."""
    meta = message.get("meta")
    if not isinstance(meta, dict) or "prompt" not in meta:
        return message
    return {**message, "meta": {key: value for key, value in meta.items() if key != "prompt"}}


async def check_models(runtime: Runtime, old: Settings, new: Settings):
    """A model newly named in Settings must be one Copilot offers ("" and auto: Copilot's
    choice); else every session given it would fail to start. Refused as an invalid field is
    (422), naming it. Unchecked while Copilot is not connected."""
    named = [(("model",), "Default model", new.model, old.model)] + [
        (
            ("role_models", role),
            f"{MODEL_FIELDS.get(role, role)} model",
            model,
            old.role_models.get(role),
        )
        for role, model in new.role_models.items()
    ]
    fresh = [item for item in named if item[2] and item[2].lower() != "auto" and item[2] != item[3]]
    if not fresh:
        return
    try:
        offered = [item["id"] for item in await runtime.available_models()]
    except Exception:
        return
    if problems := [
        {
            "type": "value_error",
            "loc": ("body", *loc),
            "msg": f"{field}: Copilot offers no model {model!r}. Available: "
            f"{', '.join(offered) or 'none'}.",
            "input": model,
        }
        for loc, field, model, _ in fresh
        if offered and model not in offered
    ]:
        raise RequestValidationError(problems)


def stopped_text(reasons: list[str]) -> str:
    """What a Settings save that stopped workers on other agents says (their tasks wait under
    Needs you, as after any stop): turned off, what brings them back."""
    many = len(reasons) > 1
    workers = f"{len(reasons)} worker{'s' if many else ''}"
    whys = list(dict.fromkeys(reasons))
    # Turned off, in either release's words: one reason.
    off = {found["name"] for why in whys if (found := TURNED_OFF.match(why))}
    if len(off) == 1 and all(TURNED_OFF.match(why) for why in whys):
        (name,) = off
        return (
            f"Stopped {workers} because {name} is off. Turn {name} on again, then press Go on "
            "under Needs you."
        )
    if len(whys) == 1 and (switched := SWITCHED.match(whys[0])):
        return (
            f"Stopped {workers} at work: {switched['name']} takes up Ask first only in a new "
            "process. To go on, asking first: Go on under Needs you, or send the message again."
        )
    return (
        f"Stopped {workers}: {' '.join(whys)} Tell {'them' if many else 'it'} to go on from "
        "Needs you once that changes."
    )


# Why a worker on another agent stopped when the owner turned that agent off (agent_block): in
# either release's words (0.1.57 and before said Coding agents; a stop may be in flight).
TURNED_OFF = re.compile(
    r"^(?P<name>\w+) is turned off in Settings → (?:Coding agents|Agents & models)\.$"
)
# Why one stopped when Ask first was turned on for it (agents.switched_to_ask_first).
SWITCHED = re.compile(r"^(?P<name>\w+) takes up Ask first only in a new process\.$")


def create_app(
    directory: Path,
    token: str,
    port: int = 8765,
    runtime_factory=Runtime,
) -> FastAPI:
    directory = directory.resolve()
    store = Store(directory)
    bus = Bus()
    runtime = runtime_factory(store, bus)
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    @asynccontextmanager
    async def lifespan(app):
        # Bounded: the page is served once the runtime has started, or after START_WAIT seconds
        # of a start that goes on (Copilot not answering): it says "Connecting…" meanwhile.
        starting = asyncio.ensure_future(runtime.start())
        await asyncio.wait({starting}, timeout=START_WAIT)
        if starting.done():
            starting.result()
        else:
            logger.warning("Chief of Staff is still starting; its page is served meanwhile.")
            starting.add_done_callback(started_late)
        try:
            yield
        finally:
            if not starting.done():
                starting.cancel()
                await asyncio.gather(starting, return_exceptions=True)
            await runtime.stop()
            store.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.state.store = store
    app.state.bus = bus
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def protect(request: Request, call_next):
        path = request.url.path
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("origin") not in origins
        ):
            return JSONResponse({"detail": "Untrusted request origin."}, status_code=403)
        if path.startswith("/api/") and path not in {"/api/auth", "/api/health"}:
            # The launch token, sent by the page in a header (or, for the event stream, where a
            # browser cannot send one, in the query: nowhere else, so no link or image address
            # carries it). Never a cookie: a browser sends cookies for 127.0.0.1 to every port on
            # it, to any other local server too.
            streamed = path == "/api/events" and request.method == "GET"
            candidate = request.headers.get("x-chief-key") or (
                request.query_params.get("key", "") if streamed else ""
            )
            if not secrets.compare_digest(candidate.encode(), token.encode()):
                return JSONResponse({"detail": "Open the private launch URL to sign in."}, 401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        # One the owner may do anyway (MayHoldWorkError): the words of that action, for its button.
        anyway = {"anyway": exc.anyway} if getattr(exc, "anyway", "") else {}
        return JSONResponse({"detail": str(exc), **anyway}, status_code=400)

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(RuntimeError)
    async def failed(request, exc):
        logger.warning("Request failed: %s", exc)
        # A session Copilot can't reopen (FreshStartError): which one, for its Start fresh button.
        fresh = {"fresh": exc.fresh} if getattr(exc, "fresh", "") else {}
        return JSONResponse({"detail": str(exc), **fresh}, status_code=409)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.post("/api/auth")
    async def login(payload: Login, response: Response):
        """Checks the launch token; the page keeps it (in this origin's storage) and sends it
        with every request."""
        if not secrets.compare_digest(payload.token.encode(), token.encode()):
            raise HTTPException(401, "Invalid launch token.")
        # An earlier version's cookie, which other local servers could read, is removed.
        response.delete_cookie(f"chiefofstaff_{port}")
        return {"ok": True}

    @app.post("/api/update")
    async def update(payload: UpdateRequest):
        """The owner's word only: no tool, Slack or Send to Chief reaches this."""
        return {"ok": True, "update": await runtime.update_now(cancel=payload.cancel)}

    @app.get("/api/health")
    async def health():
        return {"status": "ready"}

    @app.get("/api/state")
    async def state():
        """The page's state, as the page reads it (pagestate.py): finished tasks' long words
        cut, and asked for once each (/api/task-texts)."""
        found = {**page_state(runtime.snapshot()), "data_directory": str(directory)}
        found["home"] = str(Path.home())
        # Written out at once, as JSONResponse writes it: it is plain JSON already, and the page
        # reads it after each burst of events. (FastAPI's own conversion took longer than the
        # rest; it still writes anything plain JSON cannot hold.)
        try:
            return JSONResponse(found)
        except (TypeError, ValueError):
            return found

    @app.get("/api/task-texts")
    async def task_texts(ids: str = ""):
        """Tasks' whole words the page's state cut (their result, their earlier reports), by
        id ("t1,t2"), each with what they are (texts_key); one not found is left out."""
        return runtime.task_actions.texts([item for item in ids.split(",") if item][:200])

    @app.get("/api/events")
    async def events():
        async def stream():
            async with bus.subscribe() as queue:
                # Its version: a page of an earlier one loads again (it updated itself).
                yield f"data: {json.dumps({'type': 'hello', 'data': {'version': installed()}})}\n\n"
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if payload is None:
                        return
                    yield f"data: {payload}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/sessions/{session_id}")
    async def session(session_id: str):
        return runtime.view(store.session(session_id))

    @app.get("/api/sessions/{session_id}/messages")
    async def messages(
        session_id: str,
        after: int | None = None,
        before: int = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 400,
        prompts: bool = False,
    ):
        """A page of a session's messages, oldest first: its latest (before `before`, when
        given); with `after`, the next ones after it, to read on page by page. Without the
        prompts they were sent as (as long again; the chat shows what they say)."""
        session_id = store.session(session_id)["id"]
        rows = store.messages(session_id, after or 0, limit, before=before, first=after is not None)
        # Where each message it was sent stands: queued, sent, received, processed...
        found = runtime.delivery.delivery_for(session_id, rows)
        rows = [{**row, "delivery": delivery} for row, delivery in zip(rows, found, strict=True)]
        rows = with_agents(store, rows)
        return rows if prompts else [shown(row) for row in rows]

    @app.get("/api/sessions/{session_id}/messages/count")
    async def message_count(session_id: str, before: int = 0):
        """How many messages a session has before one (the earlier ones a chat can load)."""
        return {"count": store.count_messages(store.session(session_id)["id"], before)}

    @app.get("/api/workers/closed")
    async def closed_workers():
        """Every closed worker of the owner's, the latest first (the sidebar has the latest)."""
        return [
            {**row, "helper": "", "display_title": runtime.display_title(row)}
            for row in runtime.workers.recently_closed(None, helpers=False)
        ]

    def standing(message: dict) -> dict:
        """An owner's message as the page shows it, with where it stands now (sent, waiting to
        be sent…), as its chat says it."""
        if "delivery" not in message:
            found = runtime.delivery.delivery_for(message["session_id"], [message])
            message = {**message, "delivery": found[0]}
        return shown(message)

    @app.post("/api/sessions/{session_id}/messages")
    async def send(session_id: str, payload: OwnerMessage):
        async def sending() -> dict:
            if (
                session_id == runtime.router_id()
                and link_alone(payload.text)
                and not payload.attachments
            ):
                # A share link alone on Home is opened by Chief itself: never sent to a session.
                return shown((await runtime.shares.open_link(payload.text))["message"])
            if payload.to:
                return shown(
                    await runtime.owner_send_to(
                        session_id,
                        payload.to,
                        payload.text,
                        payload.attachments,
                        client_id=payload.client_id,
                    )
                )
            sent = await runtime.owner_send(
                session_id,
                payload.text,
                attachments=payload.attachments,
                agent=payload.agent,
                task_id=payload.task_id,
                compare=payload.agents,
                agent_model=payload.agent_model,
                agent_effort=payload.agent_effort,
                typed=True,
                settings_digest=payload.settings_digest,
                shared_card=payload.shared_card,
                client_id=payload.client_id,
            )
            return standing(sent)

        def again(found: dict) -> dict:
            # Stored already for this send (its page reloaded on the way): Home's copy is what
            # one sent to a worker from there answers with.
            message = standing(found)
            return {**message, "note": "", "copy": message} if payload.to else message

        return await runtime.owner_send_once(session_id, payload.client_id, sending, again)

    @app.get("/api/sessions/{session_id}/sends/{client_id}")
    async def sent_from_page(session_id: str, client_id: str):
        """Whether the page's send `client_id` to this session was stored (the page reloaded
        while it was on its way): {"message": the stored one, or null}."""
        if not CLIENT_ID.fullmatch(client_id):
            raise HTTPException(404)
        found = await runtime.client_sent(store.session(session_id)["id"], client_id)
        return {"message": standing(found) if found else None}

    @app.get("/api/attachments/{name}")
    async def attachment(name: str):
        if not ATTACHMENT_FILE.fullmatch(name):
            raise HTTPException(404)
        path = runtime.attachments_dir / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/sessions/{session_id}/read")
    async def mark_read(session_id: str):
        if store.session(session_id)["unread"]:
            runtime.update_session(session_id, unread=0)
        return {"ok": True}

    @app.post("/api/messages/{message_id}/seen")
    async def message_seen(message_id: int):
        runtime.update_seen(message_id)
        return {"ok": True}

    @app.post("/api/messages/{message_id}/settings-hold/cancel")
    async def cancel_settings_held_message(message_id: int, payload: AgentSettings):
        return shown(await runtime.cancel_held_owner_message(message_id, payload.digest))

    @app.post("/api/messages/{message_id}/settings-hold/new-worker")
    async def new_worker_for_settings_held_message(message_id: int, payload: AgentSettings):
        return await runtime.start_new_worker_for_held_message(message_id, payload.digest)

    @app.post("/api/sessions/{session_id}/model")
    async def session_model(session_id: str, payload: SessionModel):
        return await runtime.set_session_model(
            session_id, payload.model, payload.effort, payload.context
        )

    @app.get("/api/models")
    async def models(agent: str = ""):
        if agent:  # A worker's agent, not Copilot (its ⋯ menu).
            if agent not in agents.NAMES or not agents.enabled(store, agent):
                raise LookupError(f"No coding agent {agent!r} is on.")
            return await runtime.agent_models(agent, start=True)
        return await runtime.available_models()

    # Settings' Agents & models page, once the owner shows it: never before.
    @app.get("/api/agents")
    async def coding_agents():
        return await runtime.look_for_agents()

    @app.post("/api/agents/status")
    async def agents_status():
        """Each agent's status, glanced at again where it was not lately (the @ picker)."""
        return {"agents_status": await runtime.refresh_agent_status()}

    @app.post("/api/agents/{name}/signin")
    async def agent_sign_in(name: str, payload: AgentSignIn):
        """Sign in to an agent with its own CLI, as its workers run it (agentsetup.py): the run
        as the page shows it (its page's link, a code, its output), followed by agent_setup
        events until it ends."""
        return await runtime.setups.sign_in(name, payload.method, payload.confirm, payload.key)

    @app.post("/api/agents/{name}/install")
    async def agent_install(name: str, payload: AgentInstall):
        """Install an agent with the official installer its row shows, word for word."""
        return await runtime.setups.install(name, payload.method)

    @app.post("/api/agents/{name}/setup/input")
    async def agent_setup_input(name: str, payload: AgentSetupInput):
        """A code its sign-in asked to be pasted, to its stdin."""
        return await runtime.setups.send(name, payload.text)

    @app.post("/api/agents/{name}/setup/cancel")
    async def agent_setup_cancel(name: str):
        return await runtime.setups.cancel(name)

    @app.post("/api/agents/{name}/setup/retry")
    async def agent_setup_retry(name: str):
        """Claude's Retry (or Set up): Chief of Staff installs its Agent SDK again
        (claude_setup.py); its status as the page shows it."""
        return await runtime.retry_claude_setup(name)

    @app.post("/api/agents/{name}/offer")
    async def agent_offer(name: str, payload: AgentOffer):
        """The owner's answer on an agent's first-run card (Allow, or Not now)."""
        await runtime.answer_offer(name, payload.allow)
        return runtime.snapshot()

    @app.get("/api/usage")
    async def usage(since: float, until: float | None = None):
        return runtime.usage(since, time.time() + 1 if until is None else until)

    # Disk space (see storage.py): its own report, from what was measured; never in /api/state.
    @app.get("/api/storage")
    async def storage():
        return await runtime.storage.view()

    @app.post("/api/storage/measure")
    async def storage_measure():
        return runtime.storage.measure_now()

    @app.post("/api/storage/clean")
    async def storage_clean(payload: StorageSession):
        return await runtime.storage.clean_now(payload.session_id)

    @app.get("/api/storage/files")
    async def storage_files(session_id: Annotated[str, Query(min_length=1, max_length=64)]):
        return await runtime.storage.files(session_id)

    @app.post("/api/storage/delete-worktree")
    async def storage_delete(payload: StorageDelete):
        return await runtime.storage.delete_worktree(
            payload.session_id, payload.path, payload.confirm, payload.seen
        )

    @app.post("/api/storage/keep")
    async def storage_keep(payload: StorageKeep):
        return runtime.storage.keep(payload.session_id, payload.keep)

    @app.post("/api/storage/uv-measure")
    async def storage_uv():
        return await runtime.storage.uv_measure()

    @app.post("/api/storage/caches-measure")
    async def storage_caches_measure():
        return await runtime.storage.caches_measure()

    @app.post("/api/storage/caches-clear")
    async def storage_caches_clear():
        return await runtime.storage.caches_clear()

    @app.post("/api/profile")
    async def save_profile(payload: ProfileText):
        await runtime.toolset.save_profile(payload.text)
        return runtime.snapshot()

    @app.post("/api/setup-notes")
    async def save_setup_notes(payload: SetupNotes):
        await runtime.asks.edit_setup_notes(payload.text, payload.initial)
        return runtime.snapshot()

    @app.post("/api/profile/discard")
    async def discard_profile():
        runtime.toolset.discard_profile()
        return runtime.snapshot()

    @app.post("/api/profile/distill")
    async def distill_profile():
        # The owner asked for it: started at once, not left to the Router's judgment.
        return await runtime.toolset.distill_now()

    @app.post("/api/lessons/learn")
    async def learn_now():
        # What the owner wrote elsewhere since the last look, then everything not learned yet:
        # in the background, shown running in Settings.
        runtime.habits.start_learning()
        return runtime.snapshot()

    @app.post("/api/skills")
    async def add_skill(payload: SkillUpload):
        return await runtime.abilities.add_skill(payload.content)

    @app.post("/api/skills/{name}/remove")
    async def remove_skill(name: str):
        await runtime.abilities.remove_skill(name)
        return runtime.snapshot()

    @app.post("/api/plugins/retry")
    async def retry_plugins():
        for source, status in runtime.abilities.plugin_status().items():
            if status.get("state") == "failed":
                runtime.store.delete(f"plugin:{source}")
        runtime.background(runtime.abilities.sync())
        return runtime.snapshot()

    @app.post("/api/watch/scan")
    async def scan_now():
        await runtime.connector.scan(by_owner=True)
        return runtime.snapshot()

    @app.post("/api/away/test")
    async def away_test():
        await runtime.connector.notify_away(test=True)
        return runtime.snapshot()

    @app.post("/api/presence")
    async def presence():
        runtime.connector.last_seen = time.time()
        return {"ok": True}

    @app.post("/api/style/{section}/undo")
    async def undo_style(section: str):
        await runtime.habits.undo(section)
        return runtime.snapshot()

    @app.post("/api/checks/{check_id}/{action}")
    async def turn_check(check_id: str, action: Literal["enable", "disable"]):
        runtime.checks.turn(check_id, action == "enable")
        return runtime.snapshot()

    @app.post("/api/lessons")
    async def add_lesson(payload: NewLesson):
        await runtime.learning.add(payload.text, payload.scope)
        return runtime.snapshot()

    @app.post("/api/lessons/{lesson_id}/remove")
    async def remove_lesson(lesson_id: str):
        await runtime.learning.remove(lesson_id)
        return runtime.snapshot()

    @app.post("/api/focus")
    async def focus(payload: FocusRequest):
        return await runtime.focus.start(payload.minutes)

    @app.get("/api/file")
    async def read_file(path: str, session: str = ""):
        return await runtime.read_file(path, session)

    @app.get("/api/external/{session_id}/history")
    async def external_history(session_id: str):
        return await runtime.workers.history(session_id)

    @app.post("/api/projects")
    async def add_project(payload: ProjectPath):
        return await runtime.add_folder(payload.path)

    @app.get("/api/tasks/{task_id}")
    async def task(task_id: str):
        """A task as its card has it, with the commands its receipts count (the page's state
        leaves them out: its cards never show which)."""
        found = runtime.tasks.get(task_id)
        view = runtime.task_actions.view(found)
        if not view["receipts"]:
            return view
        ran = (runtime.task_actions.receipts(found) or {}).get("commands", [])
        return {**view, "receipts": {**view["receipts"], "commands": ran}}

    @app.get("/api/tasks/{task_id}/report")
    async def task_report(task_id: str):
        return {"markdown": runtime.task_actions.report_markdown(task_id)}

    # Share links (shares.py): a finished card's preview, its link, the HTML file; the links.
    @app.get("/api/tasks/{task_id}/share")
    async def share_preview(task_id: str, message: int | None = None):
        # `message`: Share… on a report in a chat (says when the card has a newer result).
        return await runtime.shares.preview(task_id, message)

    @app.post("/api/tasks/{task_id}/share")
    async def share_create(task_id: str, payload: ShareCreate):
        try:
            return await runtime.shares.create(
                task_id, payload.decisions, payload.digest, payload.days, payload.login
            )
        except ChangedError as exc:
            return JSONResponse({"detail": str(exc), "changed": True}, status_code=409)

    @app.post("/api/tasks/{task_id}/share-snapshot")
    async def share_file(task_id: str, payload: ShareFile):
        try:
            return runtime.shares.file(task_id, payload.decisions, payload.digest)
        except ChangedError as exc:
            return JSONResponse({"detail": str(exc), "changed": True}, status_code=409)

    @app.get("/api/shares")
    async def shares():
        return runtime.shares.listed()

    @app.post("/api/shares/{share_id}/revoke")
    async def share_revoke(share_id: str):
        return await runtime.shares.revoke(share_id)

    # A card someone shared, opened here from its link pasted alone into Home's message box.
    @app.post("/api/shared/open")
    async def shared_open(payload: SharedLink):
        opened = await runtime.shares.open_link(payload.link)
        return {**opened, "message": shown(opened["message"])}

    @app.delete("/api/shared/{card_id}")
    async def shared_remove(card_id: str):
        return runtime.shares.remove_copy(card_id)

    @app.get("/api/tasks/{task_id}/diff")
    async def task_diff(task_id: str):
        return await runtime.task_actions.diff(task_id)

    def fresh(task: dict) -> dict:
        """A task as it is once the action returns, not as the action left it: what changed
        since (its turn failed as a Retry was delivered, say) is not set back on the page by
        this reply (bug E90)."""
        return runtime.task_actions.view(runtime.tasks.get(task["id"]))

    @app.post("/api/tasks/{task_id}/pr")
    async def task_pr(task_id: str, payload: OpenPr | None = None):
        anyway = bool(payload and payload.anyway)
        return fresh(await runtime.task_actions.create_pr(task_id, anyway))

    @app.post("/api/tasks/{task_id}/accept")
    async def accept_task(task_id: str):
        return fresh(runtime.task_actions.accept(task_id))

    @app.post("/api/questions/{question_id}/answer")
    async def answer_question(question_id: str, payload: OwnerMessage):
        store.session(question_id.split(":", 1)[0])
        if not payload.text.strip():
            raise ValueError("Write your answer.")
        await runtime.task_actions.answer_question(question_id, payload.text)
        return runtime.snapshot()

    @app.post("/api/subjects/{subject_id}/merge")
    async def merge_subjects(subject_id: str, payload: MergeSubjects):
        # The owner says two asks are one choice ("possibly the same as …": Merge).
        await runtime.ledger.merge(subject_id, payload.into)
        return runtime.snapshot()

    @app.post("/api/asks/{ask_id}/apart")
    async def ask_apart(ask_id: str):
        # The owner says an ask folded into another is not the same choice.
        runtime.ledger.split(ask_id)
        return runtime.snapshot()

    @app.post("/api/questions/{question_id}/dismiss")
    async def dismiss_question(question_id: str):
        store.session(question_id.split(":", 1)[0])
        runtime.asks.dismiss(question_id)
        return runtime.snapshot()

    @app.post("/api/tasks/{task_id}/read")
    async def read_task(task_id: str):
        return runtime.task_actions.mark_read(task_id)

    @app.post("/api/tasks/{task_id}/news-seen")
    async def task_news_seen(task_id: str):
        return fresh(runtime.task_actions.news_seen(task_id))

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, payload: TaskNote):
        return fresh(await runtime.task_actions.cancel(task_id, payload.text))

    @app.post("/api/tasks/{task_id}/retry")
    async def retry_task(task_id: str, payload: TaskNote):
        return fresh(await runtime.task_actions.retry(task_id, payload.text))

    @app.post("/api/tasks/{task_id}/rename")
    async def rename_task(task_id: str, payload: TaskNote):
        return runtime.task_actions.rename(task_id, payload.text)

    @app.post("/api/tasks/{task_id}/split")
    async def split_task(task_id: str, payload: TaskNote):
        return runtime.task_actions.split(task_id, payload.text)

    @app.get("/api/tasks/{task_id}/split-preview")
    async def split_preview(task_id: str):
        return runtime.task_actions.split_preview(task_id)

    @app.post("/api/tasks/{task_id}/merge-back")
    async def merge_back(task_id: str):
        return runtime.task_actions.merge_back(task_id)

    @app.post("/api/tasks/{task_id}/runs")
    async def task_runs(task_id: str, payload: RunsEnded):
        """Runs its worker said go on: marked ended, or no longer followed."""
        return runtime.task_actions.end_runs(task_id, payload.urls, payload.how)

    @app.post("/api/tasks/{task_id}/check-back")
    async def task_check_back(task_id: str, payload: CheckBackAction):
        """A check-back that could not wake its worker: tried again now, or dropped."""
        if payload.how == "retry":
            return await runtime.task_actions.check_back_again(task_id)
        return runtime.task_actions.stop_check_back(task_id)

    @app.post("/api/artifacts/hide")
    async def hide_artifact(payload: ArtifactHide):
        """Off the owner's Artifacts list (or back on it): the task keeps it."""
        return {
            "hidden": runtime.hide_artifact(
                payload.key,
                payload.hidden,
                payload.tone,
                also=payload.also,
                together=[(item.key, item.tone) for item in payload.together],
            )
        }

    @app.post("/api/sessions/{session_id}/unfollow")
    async def unfollow_session_thread(session_id: str, payload: Unfollow):
        # From a task's card: for that task (its worker's other tasks keep following it).
        gone = runtime.unfollow_for(session_id, payload.text.strip(), payload.task_id.strip())
        return {"ok": True, "all": gone}

    @app.post("/api/sessions/{session_id}/follow-retry")
    async def follow_retry(session_id: str, payload: FollowRetry):
        """Read a PR it follows now, past any wait after failed reads."""
        return await runtime.retry_follow(session_id, payload.key.strip())

    @app.post("/api/tasks/{task_id}/nudge")
    async def nudge_task(task_id: str):
        return await runtime.task_actions.nudge(task_id)

    @app.post("/api/tasks/{task_id}/follow-up")
    async def follow_up_task(task_id: str, payload: OwnerMessage):
        if not payload.text.strip():
            raise ValueError("Write what the worker should do.")
        return fresh(await runtime.task_actions.follow_up(task_id, payload.text))

    @app.post("/api/tasks/{task_id}/go-on-at-reset")
    async def go_on_at_reset(task_id: str):
        """Paused for its agent's limit: it goes on by itself once that resets."""
        return runtime.task_actions.view(await runtime.task_actions.go_on_at(task_id))

    @app.post("/api/tasks/{task_id}/request-changes")
    async def request_changes(task_id: str, payload: OwnerMessage):
        """The owner does not accept the result: its worker changes it as they say."""
        return fresh(await runtime.task_actions.request_changes(task_id, payload.text))

    @app.post("/api/sessions/{session_id}/send-now")
    async def send_now(session_id: str):
        """What waits to be sent to a worker (a free slot, its next message) goes now."""
        return await runtime.send_now(session_id)

    @app.post("/api/sessions/{session_id}/fresh")
    async def start_fresh(session_id: str):
        """The owner's word only: a session Copilot can't reopen goes on in a fresh one."""
        return {"ok": True, "said": await runtime.start_fresh(session_id)}

    @app.post("/api/sessions/{session_id}/stop")
    async def stop(session_id: str):
        await runtime.workers.interrupt(session_id)
        return {"ok": True, **runtime.view(store.session(session_id))}

    @app.post("/api/sessions/{session_id}/reconnect")
    async def reconnect_session(session_id: str):
        """Retry, on a session reconnecting after a restart: its next try now."""
        try:
            runtime.retry_resume(session_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, **runtime.view(store.session(session_id))}

    @app.post("/api/sessions/{session_id}/reopen")
    async def reopen(session_id: str):
        return await runtime.workers.reopen(session_id, restore=True)

    @app.post("/api/sessions/{session_id}/yolo")
    async def session_yolo(session_id: str, payload: SessionYolo):
        return await runtime.set_session_yolo(session_id, payload.mode)

    @app.post("/api/sessions/{session_id}/label")
    async def session_label(session_id: str, payload: TaskNote):
        """A worker's short name, the owner's ("" gives it back its own)."""
        return {"label": runtime.teams.set_label(session_id, payload.text)}

    @app.get("/api/teams/past")
    async def past_teams(before: str = "", limit: int = 20):
        """Teams of older requests, newest first, a page at a time (Home's Past teams)."""
        return runtime.teams_lite.past(before, max(1, min(limit, 50)))

    @app.get("/api/teams/{team_id}")
    async def team_page(team_id: str, before: str = "", need: str = ""):
        """A team's page: its parts' task rows, what they handed each other (newest first, a
        page at a time), what was decided for it, what the owner told it, and the views of the
        parts' tasks Home does not list (`need`, their ids)."""
        wanted = [task for task in need.split(",") if task][:40]
        return runtime.teams_lite.page(team_id, before, wanted)

    @app.post("/api/teams/{team_id}/looked")
    async def team_looked(team_id: str):
        """The owner had a team's page in sight: Since you looked starts here, on every tab."""
        return runtime.teams_lite.look(team_id)

    @app.post("/api/teams/{team_id}/tell")
    async def team_tell(team_id: str, payload: TeamTell):
        """Tell the team: the owner's word to the parts they chose, each as its task stands."""
        return await runtime.teams_lite.tell(team_id, payload.text, payload.to, payload.key)

    @app.post("/api/teams/{team_id}/seen")
    async def team_seen(team_id: str, payload: TeamSeen):
        """The owner saw a done team (kept as the page gives it, the first look per key)."""
        return runtime.teams_lite.seen(team_id, payload.key)

    @app.post("/api/teams/{team_id}/dismiss")
    async def team_dismiss(team_id: str, payload: TeamSeen):
        """A done team dismissed: it goes to Past teams at once."""
        return runtime.teams_lite.seen(team_id, payload.key, dismissed=True)

    @app.post("/api/questions/{question_id}/nudge")
    async def question_nudge(question_id: str):
        """The owner's Nudge on a question that holds a worker up, in a team or not."""
        return {"said": await runtime.teams_lite.nudge(question_id)}

    @app.get("/api/teams/{team_id}/thread")
    async def team_thread(team_id: str):
        """What a team's workers told and asked each other, each question with its answer."""
        return runtime.teams.thread_view(team_id)

    @app.post("/api/teams/{team_id}/nudge")
    async def team_nudge(team_id: str, payload: TeamNudge):
        """The owner's Nudge: the worker asked hears its teammate waits for the answer."""
        return {"said": await runtime.teams.nudge(team_id, payload.question_id)}

    @app.post("/api/sessions/{session_id}/agent-settings")
    async def use_agent_settings(session_id: str, payload: AgentSettings):
        """The owner's word only: no tool, Slack or Send to Chief reaches this."""
        return {"ok": True, "said": await runtime.accept_settings(session_id, payload.digest)}

    @app.post("/api/sessions/{session_id}/agent-settings/undo")
    async def undo_agent_settings(session_id: str, payload: AgentSettings):
        return {"ok": True, "said": await runtime.undo_settings(session_id, payload.digest)}

    @app.post("/api/sessions/{session_id}/git-identity")
    async def use_git_identity(session_id: str, payload: GitIdentity):
        """The owner's word only, as for agent-settings."""
        return {"ok": True, "said": await runtime.accept_git(session_id, payload.digest)}

    @app.post("/api/sessions/{session_id}/git-quarantine")
    async def answer_git_quarantine(session_id: str, payload: GitQuarantine):
        """The owner's word only, as for agent-settings."""
        return {"ok": True, "said": await runtime.answer_git_quarantine(session_id, payload.use)}

    @app.post("/api/sessions/{session_id}/open")
    async def open_folder(session_id: str, payload: OpenFolder):
        return await runtime.open_folder(session_id, payload.app)

    @app.post("/api/sessions/{session_id}/terminal")
    async def terminal(session_id: str):
        try:
            return await runtime.terminal_command(session_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/agent-refusals/{key}")
    async def agent_refusal(key: str, payload: RefusalAnswer):
        """The owner's answer on a card for a worker its agent could not start."""
        try:
            await runtime.answer_refusal(key, payload.action)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return runtime.snapshot()

    @app.post("/api/role-waits/{role}")
    async def role_wait(role: str):
        """Use Copilot until then, on the card of a role waiting for its agent (Ask me)."""
        await runtime.answer_role_wait(role)
        return runtime.snapshot()

    @app.post("/api/sessions/{session_id}/restart")
    async def restart_on(session_id: str, payload: RestartOn):
        """Restart on <agent>: its task on another agent, before it did anything."""
        try:
            return await runtime.restart_on(
                session_id, payload.agent, payload.model, payload.effort
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/sessions/{session_id}/continue")
    async def continue_preview(session_id: str, agent: str):
        """What Continue with <agent>… sends, in one line, before the owner says go."""
        if agent not in {agents.COPILOT, *agents.NAMES}:
            raise HTTPException(400, f"Chief of Staff does not run {agent!r}.")
        try:
            return await runtime.continue_preview(session_id, agent)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/sessions/{session_id}/continue")
    async def continue_on(session_id: str, payload: ContinueOn):
        """Continue with <agent>…: a new worker on that agent goes on with this one's work."""
        try:
            return await runtime.continue_on(session_id, payload.agent)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/sessions/{session_id}/close")
    async def close(session_id: str):
        await runtime.workers.close(session_id, by="you")
        return {"ok": True, **runtime.view(store.session(session_id))}

    @app.get("/api/external")
    async def external(refresh: bool = False):
        return await runtime.workers.external(refresh, agents_too=True)

    @app.get("/api/others")
    async def others():
        """Where the owner's other Copilot sessions stand (see others.py)."""
        return runtime.others.view()

    @app.get("/api/others/{session_id}")
    async def other(session_id: str):
        return await runtime.others.find(session_id)

    @app.post("/api/external/{session_id}/adopt")
    async def adopt(session_id: str):
        return await runtime.workers.adopt(session_id)

    @app.post("/api/external/{session_id}/messages")
    async def continue_external(session_id: str, payload: OwnerMessage):
        """Messaging another session continues it here: the Router manages it from then on."""
        await runtime.workers.adopt(session_id)
        return await runtime.owner_send(session_id, payload.text, attachments=payload.attachments)

    @app.post("/api/drafts/{draft_id}")
    async def resolve(draft_id: str, decision: DraftDecision):
        return await runtime.drafts.resolve(draft_id, decision)

    @app.post("/api/your-ok/approve-safe")
    async def approve_safe(payload: ApproveSafe):
        """Your OK's Approve all safe (E123): only rows marked safe, each checked again now."""
        return await runtime.drafts.approve_safe(payload.items)

    @app.post("/api/handled/{entry_id}/undo")
    async def undo_handled(entry_id: str):
        """Undo of something handled for the owner (E123): their click only."""
        return {"ok": True, "said": await runtime.handled.undo(entry_id)}

    # One save at a time: each merges into what the one before saved.
    settings_lock = asyncio.Lock()

    @app.get("/api/shepherd/steps/{step_id}")
    async def shepherd_step(step_id: str):
        """A prepared plan, as the owner reviews it."""
        return runtime.shepherd.plan_view(step_id)

    @app.post("/api/shepherd/steps/{step_id}")
    async def shepherd_decide(step_id: str, decision: ShepherdDecision):
        from .publish import approve, dismiss, redo

        shepherd = runtime.shepherd
        if why := shepherd.refusal():
            raise ValueError(why)
        if decision.action == "approve":
            await approve(shepherd, step_id, decision.model_dump())
        elif decision.action == "dismiss":
            dismiss(shepherd, step_id, decision.text.strip()[:300])
        else:
            step = shepherd.control.step(step_id)
            if not step or not decision.text.strip():
                raise ValueError("Say what should change.")
            await redo(shepherd, step, decision.text.strip()[:2000])
        return shepherd.plan_view(step_id)

    @app.post("/api/shepherd/pr")
    async def shepherd_pr(payload: ShepherdAction):
        from .discovery import hand

        shepherd = runtime.shepherd
        if why := shepherd.refusal():
            raise ValueError(why)
        target = payload.target.strip().lower()
        if payload.action == "hand":
            return await hand(shepherd, target)
        if payload.action == "fix":
            await shepherd.fix_it(target)
        elif payload.action == "resume":
            await shepherd.resume(target)
        elif payload.action == "keep":
            await shepherd.keep(target, payload.on)
        elif payload.action == "merge-when-ready":
            await shepherd.merge_when_ready(target, payload.on)
        else:
            await shepherd.accept_head(target, payload.sha.strip().lower())
        return {"ok": True}

    @app.post("/api/shepherd/scan")
    async def shepherd_scan():
        """Look again now for what the owner's other PRs need."""
        from .discovery import discover

        if why := runtime.shepherd.refusal():
            raise ValueError(why)
        await discover(runtime.shepherd, force=True)
        return runtime.shepherd.snapshot()

    @app.post("/api/settings")
    async def save_settings(changes: Annotated[dict[str, Any], Body()]):
        """Saves the settings sent; those not sent keep their saved values. `base`: the settings
        as the page showed them when the owner began: one changed since (in another tab) stays
        as it is now unless the owner changed it here too, which is refused, not overwritten."""
        base = changes.pop("base", None)
        # The Agents & models group (agents.planned): not a setting.
        agents_change = changes.pop("agents", None)
        async with settings_lock:
            old = store.settings()
            saved = old.model_dump(mode="json")
            settings = settings_from({**saved, **changes}, old)
            if isinstance(base, dict):
                new = settings.model_dump(mode="json")
                moved = [key for key in changes if key in base and base[key] != saved.get(key)]
                if clashes := [key for key in moved if new[key] not in (base[key], saved[key])]:
                    names = ", ".join(key.replace("_", " ") for key in clashes)
                    raise HTTPException(
                        409,
                        f"Settings changed in another tab ({names}): reopen Settings to see "
                        "the change, then make yours.",
                    )
                if kept := {key: saved[key] for key in moved if new[key] == base[key]}:
                    settings = settings_from({**saved, **changes, **kept}, old)
            if any(directory.is_relative_to(Path(project)) for project in settings.projects):
                raise ValueError(
                    "A project must not contain Chief of Staff's private data directory."
                )
            # As when it is chosen on the first-run card.
            code = settings.code_folder
            if code and code != old.code_folder and directory.is_relative_to(Path(code)):
                raise ValueError(
                    "Code folder: choose a folder that does not contain Chief of Staff's own data."
                )
            for folder in map(Path, settings.sandbox_writable):
                if directory.is_relative_to(folder) or folder.is_relative_to(directory):
                    raise ValueError(
                        "Workers must not write Chief of Staff's private data directory."
                    )
            await asyncio.to_thread(runtime.workspaces.check_scratch, settings)
            await check_models(runtime, old, settings)
            plan = None if agents_change is None else agents.planned(store, agents_change)
            if runtime.agents or plan is not None:
                runtime.check_agents(plan, settings)
            if plan is not None:
                await runtime.check_agent_models(plan)
            models = agents.work_view(store, old)["models"]
            store.save_settings(settings)
            before, chose = agents.configured(store), agents.router_chooses(store)
            changed = plan is not None and agents.save(store, plan)
        stopped = await runtime.settings_changed(old, settings)
        if changed:
            stopped += await runtime.agents_saved(before, chose, models)
        snapshot = runtime.snapshot()
        if stopped:
            # One toast says how many it stopped, and why.
            snapshot["stopped"] = stopped_text(stopped)
        return snapshot

    @app.post("/api/slack/token")
    async def save_slack_token(payload: SlackToken):
        await asyncio.to_thread(runtime.slack.tokens.save, payload.token)
        runtime.slack.fingerprint = ""
        await runtime.refresh_slack()
        runtime.slack.wake()
        return runtime.snapshot()

    @app.delete("/api/slack/token")
    async def delete_slack_token():
        await asyncio.to_thread(runtime.slack.tokens.delete)
        runtime.slack.fingerprint = ""
        await runtime.refresh_slack()
        runtime.slack.wake()
        return runtime.snapshot()

    @app.post("/api/runtime/reconnect")
    async def reconnect():
        await runtime.reconnect()
        return runtime.snapshot()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
