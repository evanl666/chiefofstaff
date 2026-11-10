import asyncio
import logging
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

from .bus import Bus
from .models import (
    ArtifactHide,
    DraftDecision,
    FocusRequest,
    FollowRetry,
    NewLesson,
    OpenFolder,
    OwnerMessage,
    ProfileText,
    ProjectPath,
    SessionModel,
    SessionYolo,
    Settings,
    SetupNotes,
    SkillUpload,
    SlackToken,
    TaskNote,
    Unfollow,
)
from .runtime import Runtime
from .storage import StorageDelete, StorageKeep, StorageSession
from .store import Store

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


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
        await runtime.start()
        try:
            yield
        finally:
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
            # The launch token, sent by the page in a header (or, where a browser cannot send
            # one: the event stream, an image, in the query). Never a cookie: a browser sends
            # cookies for 127.0.0.1 to every port on it, to any other local server too.
            candidate = request.headers.get("x-chief-key") or request.query_params.get("key", "")
            if not secrets.compare_digest(candidate.encode(), token.encode()):
                return JSONResponse({"detail": "Open the private launch URL to sign in."}, 401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(RuntimeError)
    async def failed(request, exc):
        logger.warning("Request failed: %s", exc)
        return JSONResponse({"detail": str(exc)}, status_code=409)

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

    @app.get("/api/health")
    async def health():
        return {"status": "ready"}

    @app.get("/api/state")
    async def state():
        return {**runtime.snapshot(), "data_directory": str(directory), "home": str(Path.home())}

    @app.get("/api/events")
    async def events():
        async def stream():
            async with bus.subscribe() as queue:
                yield 'data: {"type": "hello"}\n\n'
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
        marks = runtime.delivery.delivery_marks(session_id)
        rows = [{**row, "delivery": runtime.delivery.delivery_of(row, marks)} for row in rows]
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

    @app.post("/api/sessions/{session_id}/messages")
    async def send(session_id: str, payload: OwnerMessage):
        if payload.to:
            return shown(
                await runtime.owner_send_to(
                    session_id, payload.to, payload.text, payload.attachments
                )
            )
        return shown(
            await runtime.owner_send(session_id, payload.text, attachments=payload.attachments)
        )

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

    @app.post("/api/sessions/{session_id}/model")
    async def session_model(session_id: str, payload: SessionModel):
        return await runtime.set_session_model(
            session_id, payload.model, payload.effort, payload.context
        )

    @app.get("/api/models")
    async def models():
        return await runtime.available_models()

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
        return runtime.task_actions.view(runtime.tasks.get(task_id))

    @app.get("/api/tasks/{task_id}/report")
    async def task_report(task_id: str):
        return {"markdown": runtime.task_actions.report_markdown(task_id)}

    @app.get("/api/tasks/{task_id}/diff")
    async def task_diff(task_id: str):
        return await runtime.task_actions.diff(task_id)

    @app.post("/api/tasks/{task_id}/pr")
    async def task_pr(task_id: str):
        return runtime.task_actions.view(await runtime.task_actions.create_pr(task_id))

    @app.post("/api/tasks/{task_id}/accept")
    async def accept_task(task_id: str):
        return runtime.task_actions.view(runtime.task_actions.accept(task_id))

    @app.post("/api/questions/{question_id}/answer")
    async def answer_question(question_id: str, payload: OwnerMessage):
        store.session(question_id.split(":", 1)[0])
        if not payload.text.strip():
            raise ValueError("Write your answer.")
        await runtime.task_actions.answer_question(question_id, payload.text)
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
        return runtime.task_actions.view(runtime.task_actions.news_seen(task_id))

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, payload: TaskNote):
        return runtime.task_actions.view(await runtime.task_actions.cancel(task_id, payload.text))

    @app.post("/api/tasks/{task_id}/retry")
    async def retry_task(task_id: str, payload: TaskNote):
        return runtime.task_actions.view(await runtime.task_actions.retry(task_id, payload.text))

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

    @app.post("/api/artifacts/hide")
    async def hide_artifact(payload: ArtifactHide):
        """Off the owner's Artifacts list (or back on it): the task keeps it."""
        return {
            "hidden": runtime.hide_artifact(
                payload.key, payload.hidden, payload.tone, also=payload.also
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
        return runtime.task_actions.view(
            await runtime.task_actions.follow_up(task_id, payload.text)
        )

    @app.post("/api/tasks/{task_id}/request-changes")
    async def request_changes(task_id: str, payload: OwnerMessage):
        """The owner does not accept the result: its worker changes it as they say."""
        return runtime.task_actions.view(
            await runtime.task_actions.request_changes(task_id, payload.text)
        )

    @app.post("/api/sessions/{session_id}/stop")
    async def stop(session_id: str):
        await runtime.workers.interrupt(session_id)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/reopen")
    async def reopen(session_id: str):
        return await runtime.workers.reopen(session_id, restore=True)

    @app.post("/api/sessions/{session_id}/yolo")
    async def session_yolo(session_id: str, payload: SessionYolo):
        return await runtime.set_session_yolo(session_id, payload.mode)

    @app.post("/api/sessions/{session_id}/open")
    async def open_folder(session_id: str, payload: OpenFolder):
        return await runtime.open_folder(session_id, payload.app)

    @app.post("/api/sessions/{session_id}/close")
    async def close(session_id: str):
        await runtime.workers.close(session_id, by="you")
        return {"ok": True}

    @app.get("/api/external")
    async def external(refresh: bool = False):
        return await runtime.workers.external(refresh)

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

    # One save at a time: each merges into what the one before saved.
    settings_lock = asyncio.Lock()

    @app.post("/api/settings")
    async def save_settings(changes: Annotated[dict[str, Any], Body()]):
        """Saves the settings sent; those not sent keep their saved values. `base`: the settings
        as the page showed them when the owner began: one changed since (in another tab) stays
        as it is now unless the owner changed it here too, which is refused, not overwritten."""
        base = changes.pop("base", None)
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
            store.save_settings(settings)
        await runtime.settings_changed(old, settings)
        return runtime.snapshot()

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
