"""The 0.1.33 bug bash, round 9 (scale and recovery): long chats page both ways and leave out
the prompts they were sent as (C1, C7); a turn of hundreds of steps is still followed up, and
nothing unsent is skipped (C2); Copilot stopping mid-send says so plainly and sends it once it
is back (C3); every closed worker is counted (C4); unsent composer text is kept (C5); the
settings are read once for many calls (C6)."""

import asyncio
import json
import shutil
import subprocess
import time
from typing import cast

import pytest
from conftest import FakeClient, FakeSlackMcp, FakeTokens, Harness, call_tool, settle
from fastapi.testclient import TestClient
from test_ui_text import STATIC, STUBS

from chiefofstaff import delivery as delivery_module
from chiefofstaff import runtime as runtime_module
from chiefofstaff.models import Settings
from chiefofstaff.runtime import Runtime
from chiefofstaff.sources.slack import SlackSource
from chiefofstaff.store import Store
from chiefofstaff.web import create_app

TOKEN = "round9-scale-token"
ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
def http(tmp_path):
    def factory(store, bus):
        async def notifier(title, text):
            pass

        runtime = Runtime(
            store, bus, backend_factory=lambda settings: FakeClient(), notifier=notifier
        )
        runtime.slack = SlackSource(store, runtime, tokens=FakeTokens(""), mcp=FakeSlackMcp())
        return runtime

    app = create_app(tmp_path / "data", TOKEN, 8765, runtime_factory=cast("type[Runtime]", factory))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.post("/api/auth", json={"token": TOKEN}, headers=ORIGIN).status_code == 200
        client.headers["X-Chief-Key"] = TOKEN
        yield client


async def spawn(runtime, title: str) -> str:
    result = await call_tool(
        runtime, "spawn_worker", runtime.router_id(), title=title, instructions=title,
        why_new="test",
    )  # fmt: skip
    await settle(runtime)
    return json.loads(result.text_result_for_llm)["session_id"]


async def until(check, seconds: float = 5.0):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


# C1: a chat of more than a page reads its earlier messages, and on after a reconnect, gapless.


def test_a_long_chat_pages_back_and_on_without_a_gap(http):
    store: Store = http.app.state.store
    router = http.get("/api/state").json()["router_id"]
    ids = [store.add_message(router, "tool", f"echo {i}")["id"] for i in range(450)]
    url = f"/api/sessions/{router}/messages"
    latest = http.get(url).json()
    assert [m["id"] for m in latest] == ids[-400:]
    count = http.get(f"{url}/count?before={latest[0]['id']}").json()
    assert count == {"count": 50}
    earlier = http.get(f"{url}?before={latest[0]['id']}&limit=30").json()
    assert [m["id"] for m in earlier] == ids[20:50], "the page right before, oldest first"
    # After a reconnect: the next page after the last one shown, then the next.
    first = http.get(f"{url}?after={ids[9]}&limit=100").json()
    assert [m["id"] for m in first] == ids[10:110]
    rest = http.get(f"{url}?after={first[-1]['id']}&limit=400").json()
    assert [m["id"] for m in rest] == ids[110:]
    assert http.get(f"{url}?limit=5000").status_code == 422


# C7: the chat's history does not carry each prompt a second time.


def test_the_chat_history_leaves_out_the_prompts_it_was_sent_as(http):
    router = http.get("/api/state").json()["router_id"]
    report = ("A long report. " * 1000).strip()
    sent = http.post(f"/api/sessions/{router}/messages", json={"text": report}, headers=ORIGIN)
    assert sent.status_code == 200 and "prompt" not in sent.json()["meta"]
    [shown] = http.get(f"/api/sessions/{router}/messages").json()
    assert shown["text"] == report and "prompt" not in shown["meta"]
    [full] = http.get(f"/api/sessions/{router}/messages?prompts=true").json()
    assert report in full["meta"]["prompt"]


# C2: a turn of more steps than the window still has its instruction found.


async def test_an_answer_after_hundreds_of_steps_is_filed_as_its_report(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, "Echo every number")
    for i in range(450):
        harness.store.add_message(worker, "tool", f"echo {i}")
    answer = (
        "Echoed all 450 numbers in ten batches of 45; the last batch (421-450) printed fine too."
    )
    session = harness.session(worker)
    session.emit("assistant.message", content=answer)
    session.emit("session.idle")
    await settle(runtime)
    reports = [text for kind, text in harness.kinds(worker) if kind == "report"]
    assert reports == [answer]
    assert runtime.tasks.current(worker)["status"] in {"review", "done"}


async def test_the_watchdog_sees_a_stop_after_hundreds_of_steps(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, "Echo every number")
    for i in range(450):
        harness.store.add_message(worker, "tool", f"echo {i}")
    runtime.update_session(worker, status="idle", activity="")
    reviewed = await runtime.watchdog.check(time.time() + 3600)
    assert reviewed == [worker]
    review = harness.store.messages(await runtime.watchdog.session_id())[-1]
    assert review["text"].startswith("stopped without reporting")


async def test_what_was_not_sent_is_never_skipped_however_much_came_after(harness):
    runtime = await harness.connect()
    router = harness.router_id
    runtime.delivery.hold(router, "Held: the worker's note", kind="peer", display="a note")
    for i in range(450):
        harness.store.add_message(router, "tool", f"step {i}")
    await runtime.owner_send(router, "What is new?")
    assert "Held: the worker's note" in harness.session(router).sent[-1]

    worker = await spawn(runtime, "Clean up the logs")
    task_id = runtime.tasks.current(worker)["id"]
    unsent = harness.store.add_message(
        worker, "router", "About it", {"prompt": "Also the old ones", "task_id": task_id}
    )
    for i in range(450):
        harness.store.add_message(worker, "tool", f"step {i}")
    runtime.delivery.drop_pending(worker, task_id)
    assert harness.store.message(unsent["id"])["meta"].get("dropped") is True


# C3: Copilot stopping during a send: a plain line, sent once it is back, and the line says so.


async def test_a_send_cut_short_by_copilot_stopping_goes_out_once_it_is_back(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, "PROCESS_CHECK", 0.01)
    monkeypatch.setattr(runtime_module, "RECONNECT_DELAY", 0.01)
    monkeypatch.setattr(delivery_module, "EVENT_DEBOUNCE", 0.01)
    monkeypatch.setattr(delivery_module, "RETRY_DELAY", 0.01)
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    router = runtime.router_id()
    stop = harness.client.stop

    async def restart():
        harness.client.crashed = False  # The next CLI runs.
        await stop()

    async def killed(prompt, mode=None, attachments=None):
        harness.client.crashed = True
        raise RuntimeError(
            "CLI process exited with code -9\nstderr: Copilot was terminated by signal SIGKILL. "
            "GitHub Copilot CLI: no platform package found. Reinstall with `npm install -g "
            "@github/copilot`."
        )

    await runtime.owner_send(router, "Hello")  # Copilot keeps a session that had a message.
    harness.session(router).emit("session.idle")
    await settle(runtime)
    harness.client.stop = restart
    harness.session(router).send = killed
    await runtime.on_trigger("slack", ["slack:9.9"], "Alice: the weekly report")
    await until(lambda: any("weekly report" in p for p in harness.session(router).sent))
    await until(lambda: runtime.ready() and not harness.store.get(f"undelivered:{router}"))
    shown = harness.store.messages(router)
    assert not [m for m in shown if m["kind"] == "error"], "not an error: it went out"
    assert not [m for m in shown if "SIGKILL" in m["text"] or "Reinstall" in m["text"]]
    [line] = [m for m in shown if m["meta"].get("resolved")]
    assert line["text"] == "Copilot stopped and reconnected; what was waiting went out."
    await runtime.stop()


async def test_your_message_as_copilot_stops_is_kept_and_sent_once_it_is_back(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runtime_module, "PROCESS_CHECK", 0.01)
    monkeypatch.setattr(runtime_module, "RECONNECT_DELAY", 0.01)
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    router = runtime.router_id()
    await runtime.owner_send(router, "Hello")  # Copilot keeps a session that had a message.
    harness.session(router).emit("session.idle")
    await settle(runtime)
    stop = harness.client.stop

    async def restart():
        harness.client.crashed = False
        await stop()

    async def killed(prompt, mode=None, attachments=None):
        harness.client.crashed = True
        raise ConnectionResetError("Connection lost")

    harness.client.stop = restart
    harness.session(router).send = killed
    await runtime.owner_send(router, "Ship the release notes")  # Not refused: it waits.
    [notice] = [m for m in harness.store.messages(router) if m["meta"].get("undelivered")]
    assert notice["text"] == "Copilot stopped; reconnecting. Your message goes out when it is back."
    await until(lambda: any("Ship the release notes" in p for p in harness.session(router).sent))
    await until(lambda: not harness.store.get(f"undelivered:{router}"))
    resolved = harness.store.message(notice["id"])
    assert resolved["text"] == "Copilot stopped and reconnected; your message went out."
    assert not [m for m in harness.store.messages(router) if m["kind"] == "error"]
    await runtime.stop()


async def test_a_failed_send_says_why_in_a_line_and_that_it_went_out_on_a_retry(
    harness, monkeypatch
):
    monkeypatch.setattr(delivery_module, "EVENT_DEBOUNCE", 0.01)
    monkeypatch.setattr(delivery_module, "RETRY_DELAY", 0.01)
    runtime = await harness.connect()
    router = harness.session(harness.router_id)
    working, failed = router.send, []

    async def flaky(prompt, mode=None, attachments=None):
        if not failed:
            failed.append(prompt)
            raise RuntimeError("socket closed.\nat line 3 of the CLI")
        return await working(prompt, mode)

    router.send = flaky
    await runtime.on_trigger("slack", ["slack:1.1"], "Alice: one")
    await until(lambda: len(router.sent) == 1)
    await settle(runtime)
    [line] = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "error"]
    assert line["text"] == "Delivery failed at first (socket closed); sent on a retry."
    assert line["meta"]["resolved"] is True


# C4: every closed worker of the owner's, counted and listed, not Chief of Staff's helpers.


def test_every_closed_worker_is_counted_and_listed(http):
    store: Store = http.app.state.store
    runtime: Runtime = http.app.state.runtime
    for i in range(25):
        store.create_session(f"w{i:02d}", "worker", f"Worker {i}", "idle")
        store.update_session(f"w{i:02d}", closed_at=f"2026-01-01T00:00:{i:02d}+00:00")
    store.set("learner:w24", "1")
    store.set("checker:w23", "1")
    assert runtime.workers.closed_count() == 23
    listed = http.get("/api/workers/closed").json()
    assert [row["id"] for row in listed] == [f"w{i:02d}" for i in range(22, -1, -1)]
    assert listed[0]["display_title"] == "Worker 22"


# C5: what you typed and did not send is back in that chat's composer, and gone once sent.


def test_unsent_composer_text_is_kept_per_chat_until_sent():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    page = """
    const box = () => ({ listeners: {}, style: {}, classList: { toggle() {} }, scrollHeight: 40,
      value: "", addEventListener(type, run) { this.listeners[type] = run; } });
    const nodes = { "#composer-input": box(), "#composer": box(), "#composer-count": box() };
    document.querySelector = (selector) => nodes[selector] ?? null;
    const input = nodes["#composer-input"];
    const first = await import(url);
    first.showDraft("home");
    input.value = "Half a question for the Router";
    input.listeners.input();
    first.showDraft("w1");
    const elsewhere = input.value;
    input.value = "For the worker";
    input.listeners.input();
    // The page again (a restart signed it out): a fresh module, the same storage.
    const again = await import(url + "?again");
    input.value = "";
    again.showDraft("home");
    const restored = input.value;
    nodes["#composer"].listeners.submit();
    input.value = "";
    await new Promise((done) => setTimeout(done, 10));
    return [elsewhere, restored, localStorage.getItem("cos.draft.home"),
            localStorage.getItem("cos.draft.w1")];
    """
    script = (
        STUBS
        + f"const url = {json.dumps((STATIC / 'session.js').as_uri())};\n"
        + f"const result = await (async () => {{ {page} }})();\n"
        + "console.log(JSON.stringify(result)); process.exit(0);\n"
    )
    done = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    elsewhere, restored, home, worker = json.loads(done.stdout.strip().splitlines()[-1])
    assert elsewhere == "", "another chat has its own"
    assert restored == "Half a question for the Router"
    assert home is None, "sent: gone"
    assert worker == "For the worker"


# C6: the settings are parsed once for the many reads of a snapshot, and again once changed.


def test_settings_are_read_once_and_again_after_any_change(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    store.save_settings(Settings(max_workers=3))
    parsed = []
    validate = Settings.model_validate_json.__func__

    def counted(cls, *args, **kwargs):
        parsed.append(1)
        return validate(cls, *args, **kwargs)

    monkeypatch.setattr(Settings, "model_validate_json", classmethod(counted))
    assert all(store.settings().max_workers == 3 for _ in range(200))
    assert len(parsed) == 1
    store.save_settings(Settings(max_workers=5))
    assert store.settings().max_workers == 5
    store.execute(
        "UPDATE settings SET value=? WHERE id=1", (Settings(max_workers=7).model_dump_json(),)
    )
    assert store.settings().max_workers == 7
    assert len(parsed) == 3
