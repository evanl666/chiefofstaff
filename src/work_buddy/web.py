import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import keyring
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import Agent, CopilotAgent
from .engine import Engine
from .files import safe_path
from .models import (
    Distillation,
    Feedback,
    MemoryInput,
    NewTask,
    Resolution,
    Settings,
    SlackCredentials,
)
from .slack import SlackBridge
from .store import Store, identifier, now

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class Login(BaseModel):
    token: str


def create_app(
    directory: Path,
    token: str,
    port: int = 8765,
    agent: Agent | None = None,
) -> FastAPI:
    directory = directory.resolve()
    store = Store(directory)
    store.export_memories()
    engine = Engine(store, agent)
    bridge = SlackBridge(store, engine, f"http://127.0.0.1:{port}")
    engine.slack = bridge
    cookie_name = f"work_buddy_{port}"
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    @asynccontextmanager
    async def lifespan(app):
        await engine.start()
        await bridge.start()
        try:
            yield
        finally:
            await engine.stop()
            await bridge.stop()
            store.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def protect(request: Request, call_next):
        path = request.url.path
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") not in origins:
                return JSONResponse({"detail": "Untrusted request origin."}, status_code=403)
        if path.startswith("/api/") and path not in {"/api/auth", "/api/health"}:
            candidate = request.cookies.get(cookie_name, "")
            if not secrets.compare_digest(candidate, token):
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
        if not secrets.compare_digest(payload.token, token):
            raise HTTPException(401, "Invalid launch token.")
        response.set_cookie(
            cookie_name,
            token,
            httponly=True,
            samesite="strict",
            max_age=86400,
        )
        return {"ok": True}

    @app.get("/api/health")
    async def health():
        return {"status": "ready"}

    @app.get("/api/state")
    async def state():
        return {**engine.snapshot(), "data_directory": str(directory)}

    @app.post("/api/settings")
    async def save_settings(settings: Settings):
        if settings.project_path and directory.is_relative_to(Path(settings.project_path)):
            raise ValueError("The project must not contain Work Buddy's private data directory.")
        old = store.settings()
        store.save_settings(settings)
        store.event("settings", "Settings updated by owner.")
        if (old.slack_enabled, old.slack_owner) != (settings.slack_enabled, settings.slack_owner):
            await bridge.start()
        return {"ok": True, "slack": bridge.status}

    @app.post("/api/slack/credentials")
    async def save_credentials(credentials: SlackCredentials):
        try:
            await asyncio.to_thread(
                bridge.credentials.save,
                credentials.bot_token,
                credentials.app_token,
            )
        except keyring.errors.KeyringError as exc:
            raise RuntimeError(
                "OS keyring unavailable. Use the documented environment variables instead."
            ) from exc
        await bridge.start()
        return {"ok": True, "slack": bridge.status}

    @app.post("/api/slack/reconnect")
    async def reconnect():
        await bridge.start()
        return bridge.status

    @app.post("/api/agent/check")
    async def check_agent():
        settings = store.settings()
        if settings.backend == "demo":
            return {"version": "DEMO simulator selected. No real agent calls."}
        executable = CopilotAgent.command(settings)[0]
        process = await asyncio.create_subprocess_exec(
            executable,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise RuntimeError("CLI version check timed out.") from exc
        if process.returncode:
            raise RuntimeError("CLI version check failed: " + stderr.decode(errors="replace")[:500])
        return {
            "version": stdout.decode(errors="replace")[:500],
            "note": "Executable found. Authentication is verified by the first real task.",
        }

    def capacity():
        count = store.one(
            """SELECT count(*) AS n FROM tasks
            WHERE status NOT IN ('completed','failed','cancelled','interrupted')"""
        )["n"]
        if count >= 100:
            raise HTTPException(429, "Task backlog is full. Finish or cancel existing tasks.")

    @app.post("/api/tasks")
    async def new_task(task: NewTask):
        capacity()
        task_id = store.create_task(task.title, task.instructions)
        return {"id": task_id}

    @app.get("/api/tasks/{task_id}")
    async def task_detail(task_id: str):
        return {
            **store.task(task_id),
            "approvals": [
                store.approval(row["id"])
                for row in store.all(
                    "SELECT id FROM approvals WHERE task_id=? ORDER BY created_at", (task_id,)
                )
            ],
            "feedback": store.all(
                "SELECT text,created_at FROM feedback WHERE task_id=? ORDER BY created_at",
                (task_id,),
            ),
        }

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        async with engine.approval_lock:
            await engine.cancel(task_id)
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/retry")
    async def retry(task_id: str):
        engine.retry(task_id)
        return {"ok": True}

    @app.post("/api/approvals/{approval_id}")
    async def resolve(approval_id: str, resolution: Resolution):
        await engine.resolve(approval_id, resolution.choice, resolution.reason)
        return {"ok": True}

    @app.post("/api/distill")
    async def distill(payload: Distillation):
        capacity()
        settings = store.settings()
        for file in payload.source_files:
            if (
                not settings.project_path
                or not safe_path(Path(settings.project_path), file).is_file()
            ):
                raise ValueError("Each source file must exist inside the configured project.")
        task_id = store.create_task(
            "Distill initial work profile",
            "Extract an evidence-backed initial work profile. Material explicitly supplied "
            "by the owner:\n"
            + payload.material
            + "\nOnly these selected files should be read for distillation:\n"
            + "\n".join(payload.source_files),
            kind="distill",
        )
        return {"id": task_id}

    @app.post("/api/tasks/{task_id}/feedback")
    async def feedback(task_id: str, payload: Feedback):
        capacity()
        task = store.task(task_id)
        store.execute(
            "INSERT INTO feedback VALUES(?,?,?,?)",
            (identifier(), task_id, payload.text, now()),
        )
        if payload.remember:
            store.remember(
                payload.text,
                payload.scope,
                task["project_path"],
                task_id,
                evidence="Owner explicitly confirmed this feedback as memory.",
            )
        learning_id = engine.learn(task_id, payload.text)
        store.event("feedback", "Feedback recorded; Lead will propose lessons.", task_id)
        return {"learning_task_id": learning_id}

    @app.post("/api/memories")
    async def add_memory(payload: MemoryInput):
        store.remember(payload.text, payload.scope, store.settings().project_path)
        return {"ok": True}

    @app.delete("/api/memories/{memory_id}")
    async def forget(memory_id: str):
        if not store.one("SELECT id FROM memories WHERE id=?", (memory_id,)):
            raise LookupError("Memory not found.")
        store.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        store.export_memories()
        store.event("memory", "Owner removed a confirmed memory.")
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
