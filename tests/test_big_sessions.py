"""A worker session too large for Copilot to answer its whole history (0.1.59's hotfix).

Copilot 1.0.115 and 1.0.116 write each answer to their pipe at once. Asked for the whole history
of a session whose log is 808 MB (session.getMessages: what Chief of Staff read to check what a
session received, at each start for a worker at work), that write failed (EINVAL on macOS), and
from then on the CLI answered nothing while it ran on. Chief of Staff waited for ever: its start
never ended (the page never opened), and its stop hung until Copilot was killed.
"""

import asyncio
import json
import sys
import time
from uuid import uuid4

import pytest
from conftest import FakeClient, FakeSession, Harness, call_tool
from copilot.session import PermissionHandler
from test_runtime import fake_copilot

from chiefofstaff import backend as backend_module
from chiefofstaff import runtime as runtime_module
from chiefofstaff import web as web_module
from chiefofstaff.backend import running
from chiefofstaff.delivery import FreshStartError, digest
from chiefofstaff.runtime import Runtime


def write_log(client: FakeClient, copilot_id: str, prompts: list[str], padding: int = 8000):
    """A session's log as Copilot writes it: a long turn's events, then what it was sent."""
    log = client.home / "session-state" / copilot_id / "events.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "tool.execution_complete", "data": {"result": "x" * padding}}]
    lines += [{"type": "user.message", "data": {"content": prompt}} for prompt in prompts]
    log.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return log


async def spawn(runtime, router_id: str, title: str) -> str:
    result = await call_tool(runtime, "spawn_worker", router_id, title=title, instructions=title)
    return json.loads(result.text_result_for_llm)["session_id"]


def hold_history(monkeypatch, of: set[str]) -> list[str]:
    """Copilot's answer of these sessions' whole history never comes (as it never came for a
    session that size): who was asked."""
    asked = []
    whole = FakeSession.get_events

    async def history(self):
        asked.append(self.session_id)
        if self.session_id in of:
            await asyncio.Event().wait()
        return await whole(self)

    monkeypatch.setattr(FakeSession, "get_events", history)
    return asked


async def test_a_big_worker_session_is_not_asked_for_its_whole_history_after_a_restart(
    tmp_path, monkeypatch
):
    """At work when Chief of Staff stopped, with a message given: what it received is read from
    its log (a big one), not asked of Copilot; it continues, the message counted as received."""
    monkeypatch.setattr(backend_module, "BIG_LOG", 4096, raising=False)
    first = Harness(tmp_path / "data")
    first.client.home = tmp_path / "copilot"
    runtime = await first.connect()
    worker = await spawn(runtime, first.router_id, "Big")
    await runtime.delivery.deliver(worker, "Check the bottom row too.", kind="router")
    write_log(first.client, worker, first.session(worker).raw_sent)
    await runtime.stop()

    asked = hold_history(monkeypatch, {worker})
    second = Harness(tmp_path / "data", client=first.client)
    await asyncio.wait_for(second.connect(), 30)
    assert second.runtime.ready()
    assert worker not in asked
    note = second.session(worker).sent[-1]
    assert note.startswith('<message from="system" type="restart">\nChief of Staff restarted')
    assert "Check the bottom row too." in note
    assert "new to you" not in note, "what it received is not sent again"


async def test_a_big_worker_session_that_got_a_message_is_checked_from_its_log(
    tmp_path, monkeypatch
):
    """A send cut short by a stop: whether it arrived is read from its (big) log; and a message
    sent to it later is checked there too (E14's echo check), never asked of Copilot whole."""
    monkeypatch.setattr(backend_module, "BIG_LOG", 4096, raising=False)
    harness = Harness(tmp_path / "data")
    harness.client.home = tmp_path / "copilot"
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Big")
    harness.session(worker).emit("session.idle")
    await asyncio.sleep(0)
    message = runtime.add_message(worker, "router", "More", {"prompt": "More, please."})
    marker = {"digest": digest("More, please."), "upto": message["id"]}
    harness.store.set(f"sending:{worker}", json.dumps(marker))
    write_log(harness.client, worker, [*harness.session(worker).raw_sent, "More, please."])
    await runtime.detach(worker)
    asked = hold_history(monkeypatch, {worker})
    assert await asyncio.wait_for(runtime.delivery.reconcile(worker), 10) is True
    assert harness.store.get(f"sent:{worker}") == str(message["id"])
    session = await runtime.session_for(worker)
    assert await runtime.delivery.received(worker, [message]) == [message]
    events = await runtime.delivery.conversation(worker, session)
    assert [event.data.content for event in events][-1] == "More, please."
    assert worker not in asked


async def test_copilot_connects_while_a_session_it_resumes_never_answers(tmp_path, monkeypatch):
    """A worker at work whose resume never ends (Copilot hangs on it) holds nothing: Copilot is
    ready within RESUME_WAIT, the other worker continues, and that one is given up on after
    SESSION_RESUME; what is sent to it meanwhile waits for that."""
    monkeypatch.setattr(runtime_module, "RESUME_WAIT", 0.3, raising=False)
    monkeypatch.setattr(runtime_module, "SESSION_RESUME", 1.0, raising=False)
    first = Harness(tmp_path / "data")
    runtime = await first.connect()
    stuck = await spawn(runtime, first.router_id, "Stuck")
    other = await spawn(runtime, first.router_id, "Other")
    await runtime.stop()

    second = Harness(tmp_path / "data", client=first.client)
    resume = first.client.resume_session

    async def hanging(session_id, **options):
        if session_id == stuck:
            await asyncio.Event().wait()
        return await resume(session_id, **options)

    first.client.resume_session = hanging
    second.runtime.loop = asyncio.get_running_loop()
    second.runtime.bus.bind(second.runtime.loop)
    started = time.monotonic()
    await asyncio.wait_for(second.runtime.connect(), 10)
    assert second.runtime.ready()
    assert time.monotonic() - started < 5
    await asyncio.wait_for(asyncio.gather(*second.runtime.jobs, return_exceptions=True), 10)
    assert second.session(other).sent[-1].startswith('<message from="system" type="restart">')
    assert stuck not in first.client.resumed


async def test_the_page_is_served_while_the_runtime_still_starts(tmp_path, monkeypatch):
    """Copilot not answering as Chief of Staff starts: its page is served after START_WAIT all
    the same, and its stop ends that start."""
    monkeypatch.setattr(web_module, "START_WAIT", 0.2, raising=False)
    calls = []

    class Stuck(Runtime):
        async def start(self):
            calls.append("start")
            await asyncio.Event().wait()

        async def stop(self):
            calls.append("stop")

    app = web_module.create_app(tmp_path / "data", "token", 21301, runtime_factory=Stuck)
    context = app.router.lifespan_context(app)
    await asyncio.wait_for(context.__aenter__(), 5)
    await asyncio.wait_for(context.__aexit__(None, None, None), 5)
    assert calls == ["start", "stop"]


class Dying(FakeClient):
    """A Copilot whose connection dies after it resumes one of `kills` (alive() says False, as
    CopilotBackend's once its pings go unanswered), naming that session (culprit)."""

    def __init__(self):
        super().__init__()
        self.dead = ""
        self.culprit = ""
        self.kills: set[str] = set()

    def alive(self):
        return not self.dead and not self.crashed

    async def resume_session(self, session_id, **options):
        session = await super().resume_session(session_id, **options)
        if session_id in self.kills:
            self.kills.discard(session_id)
            self.dead, self.culprit = "Copilot stopped answering.", session_id
        return session


async def until(check, within: float = 10):
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("not in time")
        await asyncio.sleep(0.02)


async def test_copilot_connects_again_without_the_session_whose_resume_killed_it(
    tmp_path, monkeypatch
):
    """Copilot's connection died after it resumed a worker's big session: Chief of Staff
    connects to a new Copilot (no restart of its own), resumes the others, and never that one
    again: it rests with one plain line and Start fresh, which a fresh session goes on from."""
    monkeypatch.setattr(backend_module, "BIG_LOG", 4096, raising=False)
    monkeypatch.setattr(runtime_module, "PROCESS_CHECK", 0.02)
    client = Dying()
    client.home = tmp_path / "copilot"
    first = Harness(tmp_path / "data", client=client)
    runtime = await first.connect()
    big = await spawn(runtime, first.router_id, "Big")
    small = await spawn(runtime, first.router_id, "Small")
    write_log(client, big, client.sessions[big].raw_sent)
    await runtime.stop()

    client.kills = {big}
    second = Harness(tmp_path / "data", client=client)

    def revived(settings):
        client.dead, client.culprit = "", ""  # A new Copilot.
        return client

    second.runtime.backend_factory = revived
    runtime = await second.connect()
    await until(
        lambda: runtime.too_large(big) and runtime.ready() and client.resumed.count(small) == 2
    )
    await until(lambda: not runtime.jobs or all(job.done() for job in runtime.jobs))
    assert client.resumed.count(big) == 1, "never resumed again"
    row = second.store.session(big)
    assert row["status"] == "idle"
    assert row["activity"] == runtime_module.TOO_LARGE_WAIT
    line = [m for m in second.store.messages(big) if m["meta"].get("fresh")]
    assert [m["text"] for m in line] == [
        "This worker's Copilot session is too large for Copilot to reopen here; your message "
        "is kept for a fresh start."
    ]
    assert (
        client.sessions[small]
        .sent[-1]
        .startswith('<message from="system" type="restart">\nCopilot was restarted')
    )

    with pytest.raises(FreshStartError):
        await runtime.delivery.deliver(big, "Go on.", kind="router")
    assert client.resumed.count(big) == 1

    # A start afresh: kept as such (no loop) across a restart of Chief of Staff too.
    await runtime.stop()
    third = Harness(tmp_path / "data", client=client)
    runtime = await third.connect()
    assert client.resumed.count(big) == 1
    assert len([m for m in third.store.messages(big) if m["meta"].get("fresh")]) == 1

    said = await runtime.start_fresh(big)
    assert said == "Started a fresh worker with its context."
    fresh = third.store.get(f"copilot:{big}")
    assert fresh != big and fresh in client.created
    sent = client.sessions[fresh].sent[-1]
    assert "could not be resumed" in sent and "Continue the work of your earlier session" in sent
    row = third.store.session(big)
    assert row["activity"] != runtime_module.TOO_LARGE_WAIT and row["status"] == "working"


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in CLI is a shell script")
async def test_a_copilot_cli_that_stops_answering_is_found_dead(tmp_path, monkeypatch):
    """With the real SDK: a request Copilot never answers (it answers nothing more, its process
    runs on) fails once two pings go unanswered; the session it was about is named, nothing
    more is sent, and the backend is no longer alive (so Chief of Staff connects again)."""
    for name, value in (("PROBE_AFTER", 0.3), ("PROBE_TIMEOUT", 0.3), ("PROBE_EVERY", 0.1)):
        monkeypatch.setattr(backend_module, name, value, raising=False)
    monkeypatch.setenv("FAKE_COPILOT_DEAF", "session.getMessages")
    backend = backend_module.copilot_backend(fake_copilot(tmp_path / "copilot", "1.0.112", 3))
    await backend.start()
    try:
        session_id = str(uuid4())
        session = await backend.resume_session(
            session_id, on_permission_request=PermissionHandler.approve_all
        )
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(session.get_events(), 20)
        assert not backend.alive()
        assert backend.culprit == session_id
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(backend.version(), 2)
    finally:
        await asyncio.wait_for(backend.stop(), 30)


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in CLI is a shell script")
async def test_copilot_stops_in_bounded_time_when_it_answers_nothing(tmp_path, monkeypatch):
    """Copilot answering nothing more, with a session open, and ignoring SIGTERM: stopping it
    takes seconds (its session's release never answered, then its processes killed), never for
    ever."""
    monkeypatch.setattr(backend_module, "STOP_TIMEOUT", 1.0, raising=False)
    monkeypatch.setenv("FAKE_COPILOT_DEAF", "session.getMessages")
    monkeypatch.setenv("FAKE_COPILOT_STUBBORN", "1")
    backend = backend_module.copilot_backend(fake_copilot(tmp_path / "copilot", "1.0.112", 3))
    await backend.start()
    session = await backend.resume_session(
        str(uuid4()), on_permission_request=PermissionHandler.approve_all
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(session.get_events()), 0.5)
    pids = backend.processes()
    assert pids
    started = time.monotonic()
    await asyncio.wait_for(backend.stop(), 30)
    assert time.monotonic() - started < 15
    assert not [pid for pid in pids if running(pid)]


def test_a_log_read_for_what_a_session_received_goes_on_from_where_it_ended(tmp_path):
    """received_in_log: the user messages of a log, read from where the last read ended."""
    log = tmp_path / "events.jsonl"
    log.write_text(json.dumps({"type": "user.message", "data": {"content": "one"}}) + "\n")
    first = backend_module.received_in_log(log, (0, []))
    assert [event["data"]["content"] for event in first[1]] == ["one"]
    with log.open("a") as file:
        file.write(json.dumps({"type": "assistant.message", "data": {"content": "x"}}) + "\n")
        file.write(json.dumps({"type": "user.message", "data": {"content": "two"}}) + "\n")
        file.write('{"type": "user.message", "data": {"content": "half')
    second = backend_module.received_in_log(log, first)
    assert [event["data"]["content"] for event in second[1]] == ["one", "two"]
    assert second[0] < log.stat().st_size, "a line still being written is read next time"
    log.write_text(json.dumps({"type": "user.message", "data": {"content": "new"}}) + "\n")
    third = backend_module.received_in_log(log, (10_000, second[1]))
    assert [event["data"]["content"] for event in third[1]] == ["new"]
