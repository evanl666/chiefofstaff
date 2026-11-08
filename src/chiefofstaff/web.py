import asyncio
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .bus import Bus
from .models import (
    ArtifactHide,
    DraftDecision,
    FocusRequest,
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
)
from .runtime import Runtime
from .store import Store

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class Login(BaseModel):
    token: str


ATTACHMENT_FILE = re.compile(r"[0-9a-f]{32}\.(?:png|jpg|gif|webp)")


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
    async def messages(session_id: str, after: int = 0):
        store.session(session_id)
        return store.messages(session_id, after)

    @app.post("/api/sessions/{session_id}/messages")
    async def send(session_id: str, payload: OwnerMessage):
        if payload.to:
            return await runtime.owner_send_to(
                session_id, payload.to, payload.text, payload.attachments
            )
        return await runtime.owner_send(session_id, payload.text, attachments=payload.attachments)

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
        return store.usage(since, time.time() + 1 if until is None else until)

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
    async def unfollow_session_thread(session_id: str, payload: TaskNote):
        runtime.store.unfollow(payload.text.strip(), session_id)
        runtime.publish_session(runtime.store.session(session_id))
        for task in runtime.tasks.for_session(session_id):
            runtime.task_actions.publish(task)
        return {"ok": True}

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

    @app.post("/api/sessions/{session_id}/stop")
    async def stop(session_id: str):
        await runtime.workers.interrupt(session_id)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/reopen")
    async def reopen(session_id: str):
        return await runtime.workers.reopen(session_id)

    @app.post("/api/sessions/{session_id}/yolo")
    async def session_yolo(session_id: str, payload: SessionYolo):
        return await runtime.set_session_yolo(session_id, payload.mode)

    @app.post("/api/sessions/{session_id}/open")
    async def open_folder(session_id: str, payload: OpenFolder):
        return await runtime.open_folder(session_id, payload.app)

    @app.post("/api/sessions/{session_id}/close")
    async def close(session_id: str):
        await runtime.workers.close(session_id)
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

    @app.post("/api/settings")
    async def save_settings(settings: Settings):
        if any(directory.is_relative_to(Path(project)) for project in settings.projects):
            raise ValueError("A project must not contain Chief of Staff's private data directory.")
        for folder in map(Path, settings.sandbox_writable):
            if directory.is_relative_to(folder) or folder.is_relative_to(directory):
                raise ValueError("Workers must not write Chief of Staff's private data directory.")
        await asyncio.to_thread(runtime.workspaces.check_scratch, settings)
        old = store.settings()
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
