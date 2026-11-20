"""Copilot's own runtime (FALLBACK_CLI) runs no extension: with the owner's Copilot in
experimental mode, every worker's create and resume on it failed ("Failed to initialize
standalone session extensions: extensionSdkPath is required when standalone extensions are
requested"). Its sessions now say they run none; the real runtime's side is in
test_backend_contract (CHIEFOFSTAFF_TEST_COPILOT=1)."""

import threading

import pytest
from conftest import FakeClient
from test_runtime import sdk_clients

from chiefofstaff import backend as backend_module
from chiefofstaff.backend import CopilotBackend, extensions_refused

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
