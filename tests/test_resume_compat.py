"""Copilot's own runtime (FALLBACK_CLI) runs no extension: with the owner's Copilot in
experimental mode, every worker's create and resume on it failed ("Failed to initialize
standalone session extensions: extensionSdkPath is required when standalone extensions are
requested"). Its sessions now say they run none; the real runtime's side is in
test_backend_contract (CHIEFOFSTAFF_TEST_COPILOT=1)."""

import threading

import pytest
from conftest import FakeClient, FakeSession, Harness, call_tool, settle
from test_progress import spawn
from test_runtime import sdk_clients

from chiefofstaff import backend as backend_module
from chiefofstaff.backend import CopilotBackend, extensions_refused
from chiefofstaff.roles import ROUTER, WATCHDOG, WORKER
from chiefofstaff.runtime import CONTINUED_FRESH, RECONNECTING

REFUSED = (
    "JSON-RPC Error -32603: Request session.resume failed with message: Failed to initialize "
    "standalone session extensions: extensionSdkPath is required when standalone extensions "
    "are requested"
)


class Hostless(FakeClient):
    """Sessions as Copilot's own runtime opens them in the owner's experimental mode: one with
    config discovery that does not decline extensions is refused, created or resumed."""

    def __init__(self):
        super().__init__()
        self.asked: list[tuple[str, dict]] = []

    def check(self, how: str, options: dict):
        self.asked.append((how, options))
        if (
            options.get("enable_config_discovery")
            and options.get("request_extensions") is not False
        ):
            raise RuntimeError(REFUSED.replace("session.resume", f"session.{how}"))

    async def create_session(self, *, session_id, name, **options):
        self.check("create", options)
        return await super().create_session(session_id=session_id, name=name, **options)

    async def resume_session(self, session_id, **options):
        self.check("resume", options)
        return await super().resume_session(session_id, **options)


def test_only_a_refusal_for_extensions_is_one():
    assert extensions_refused(REFUSED)
    assert extensions_refused(
        RuntimeError(
            "Failed to initialize standalone session extensions: No extension launch "
            "provider is registered for standalone extensions"
        )
    )
    assert not extensions_refused("Session not found")
    assert not extensions_refused("Failed to initialize standalone session tools: boom")


async def test_every_session_on_copilots_own_runtime_declines_extensions(monkeypatch):
    """The owner's CLI 1.0.114 refused for its newer protocol, Copilot 1.0.112 runs: a worker
    (config discovery) is created and resumed at the first try, saying it runs no extension."""
    fake, setting_up = Hostless(), threading.Event()
    sdk_clients(monkeypatch, fake, setting_up)
    backend = backend_module.copilot_backend("git")
    setting_up.set()
    await backend.start()
    assert backend.fallback

    created = await backend.create_session(session_id="w1", name="x", enable_config_discovery=True)
    resumed = await backend.resume_session("w1", enable_config_discovery=True)
    router = await backend.create_session(session_id="r1", name="Router")

    assert created.options["request_extensions"] is False
    assert resumed.options["request_extensions"] is False
    assert router.options["request_extensions"] is False
    assert [how for how, _ in fake.asked] == ["create", "resume", "create"], "no refused try"


async def test_the_owners_cli_keeps_extensions_until_it_refuses_them():
    """On the owner's own CLI (which runs extensions) sessions are opened as before; one that
    could not (refused for them) opens without, at once, and so does every one after it."""
    fake = Hostless()
    backend = CopilotBackend(fake)  # type: ignore[arg-type]

    router = await backend.create_session(session_id="r1", name="Router")
    assert "request_extensions" not in router.options

    worker = await backend.create_session(session_id="w1", name="x", enable_config_discovery=True)
    assert worker.options["request_extensions"] is False
    assert [how for how, _ in fake.asked] == ["create", "create", "create"]
    assert fake.created == ["r1", "w1"]

    resumed = await backend.resume_session("w1", enable_config_discovery=True)
    assert resumed.options["request_extensions"] is False
    assert [how for how, _ in fake.asked][3:] == ["resume"], "declined at once after a refusal"


async def test_another_failure_is_not_tried_again():
    fake = Hostless()
    backend = CopilotBackend(fake)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="Session not found"):
        await backend.resume_session("gone")
    assert len(fake.asked) == 1 and not backend.no_extensions


def refuse(harness, *ids: str):
    """Copilot refusing to resume these sessions for their extensions however they are opened
    (the backend's declining did not help): the net under it."""
    resume = harness.client.resume_session

    async def resuming(session_id, **options):
        if session_id in ids:
            raise RuntimeError(REFUSED)
        return await resume(session_id, **options)

    harness.client.resume_session = resuming


@pytest.mark.parametrize("role", [ROUTER, WORKER, WATCHDOG])
async def test_a_session_that_cannot_resume_goes_on_fresh_with_what_it_was_not_given(
    tmp_path, role
):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    if role == ROUTER:
        session_id = harness.router_id
    else:
        session_id = f"{role}-1"
        harness.store.create_session(
            session_id, role, "Probe access", "idle", cwd=str(tmp_path), branch="probe-fix"
        )
        harness.client.sessions[session_id] = FakeSession(session_id, {})
        harness.store.set(f"created:{session_id}", "1")
    await runtime.detach(session_id)
    old = runtime.copilot_id(session_id)
    refuse(harness, old)
    before = harness.store.messages(session_id)
    runtime.add_message(session_id, "owner", "Try once more", {"prompt": "Try once more"})
    shown = []
    create = harness.client.create_session

    async def creating(**options):
        shown.append(harness.store.session(session_id)["activity"])
        return await create(**options)

    harness.client.create_session = creating

    fresh = await runtime.session_for(session_id)

    new = fresh.session_id
    assert new != old and harness.client.created.count(new) == 1
    assert runtime.copilot_id(session_id) == new
    assert runtime.row_id(old) == runtime.row_id(new) == session_id
    assert shown == [
        RECONNECTING if role == WORKER else harness.store.session(session_id)["activity"]
    ]
    assert harness.store.session(session_id)["activity"] != RECONNECTING
    assert harness.store.messages(session_id)[: len(before)] == before, "its timeline stays"
    notices = [m["text"] for m in harness.store.messages(session_id) if m["kind"] == "notice"]
    assert notices.count(CONTINUED_FRESH) == 1
    assert await runtime.delivery.flush(session_id, fresh)
    assert fresh.sent[-1].count("Try once more") == 1, "with its handover, once"
    assert "could not be resumed" in fresh.sent[-1]
    if role != ROUTER:
        assert "Probe access" in fresh.sent[-1] and str(tmp_path) in fresh.sent[-1]
    assert not await runtime.delivery.flush(session_id, fresh)
    assert await runtime.session_for(session_id) is fresh


async def test_a_fresh_session_failing_so_too_is_not_replaced_again(tmp_path):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Probe access")
    await settle(runtime)
    await runtime.detach(worker)
    refuse(harness, runtime.copilot_id(worker))
    fresh = await runtime.session_for(worker)
    await runtime.detach(worker)
    refuse(harness, fresh.session_id)
    created = list(harness.client.created)
    with pytest.raises(RuntimeError, match="extensionSdkPath"):
        await runtime.session_for(worker)
    assert harness.client.created == created and runtime.copilot_id(worker) == fresh.session_id


async def test_another_resume_failure_is_not_a_fresh_session(tmp_path):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    router = harness.router_id
    await runtime.detach(router)
    harness.store.delete(f"fresh:{runtime.copilot_id(router)}")
    created = list(harness.client.created)

    async def broken(session_id, **options):
        raise RuntimeError("permission denied")

    harness.client.resume_session = broken
    with pytest.raises(RuntimeError, match="permission denied"):
        await runtime.session_for(router)
    assert harness.client.created == created


async def test_the_owners_answer_reaches_the_fresh_worker_once_and_its_question_closes(tmp_path):
    """The owner answers a worker's question while its Copilot session can't be resumed (as
    0.1.48 showed it: "Could not deliver the message: …"): the answer goes to a fresh session
    of the worker, once, and the question is answered."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Retry the probe access")
    await call_tool(runtime, "report", worker, status="question", summary="Retry it now?")
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    assert runtime.asks.session_questions(worker)
    await runtime.detach(worker)  # As after a restart.
    old = runtime.copilot_id(worker)
    refuse(harness, old)

    await runtime.task_actions.answer_question(worker, "Yes, retry it")
    await settle(runtime)

    new = runtime.copilot_id(worker)
    assert new != old
    sent = harness.client.sessions[new].sent
    assert sum(text.count("Yes, retry it") for text in sent) == 1
    assert not any("Yes, retry it" in text for text in harness.client.sessions[old].sent)
    assert not runtime.asks.session_questions(worker)
    errors = [m for m in harness.store.messages(worker) if m["kind"] == "error"]
    assert not errors
