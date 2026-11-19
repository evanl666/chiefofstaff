"""Live test of 0.1.47's hotfix: workers whose MCP servers have many tools, on the Copilot that
runs when the owner's CLI is newer than Chief of Staff supports (1.0.112 for a 1.0.114 CLI).

0.1.46 left tool search to Copilot's default, off in 1.0.112: every MCP tool's definition went
with each turn. With about 250 tools, a worker's turns were blocked on the default context, or
its tools were left out (Chief of Staff's own report and check_back among them), and the Router
started replacement workers that failed the same way. And a message sent while Chief of Staff
connected again went to the refused CLI ("Delivery failed: SDK protocol version mismatch").

  V1  A worker with the many-tools MCP server (scripts/many_tools_mcp.py, configured in
      Settings → MCP servers) finds its ping tool through tool search, sets a check-back with
      check_back, is woken by it, and reports with report. Its turns' tools all fit: nothing is
      left out, Chief of Staff's own were sent each time, tool search was on; no error, no
      replacement worker.
  V2  The owner writes to the Router while Chief of Staff connects to Copilot again: the message
      waits, then goes and is answered; no "Delivery failed".

Uses your signed-in Copilot CLI, consumes premium requests (LIVE_HOTFIX_MODEL, the workers'
model: claude-haiku-5.5 by default), and takes about 10 minutes (V1 waits for its 5-minute
check-back). Its port is LIVE_HOTFIX_PORT (20250). All Copilot sessions it creates are deleted
at the end.

    uv run python scripts/live_hotfix.py          # V1 and V2
    uv run python scripts/live_hotfix.py V2       # one of them
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e

live_e2e.PORT = int(os.environ.get("LIVE_HOTFIX_PORT", "20250"))
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import App, cleanup, log, transcript, wait_for  # noqa: E402

from chiefofstaff.backend import copilot_home  # noqa: E402
from chiefofstaff.events import tools_left_out  # noqa: E402

MODEL = os.environ.get("LIVE_HOTFIX_MODEL", "claude-haiku-5.5")
SERVER = str(Path(__file__).resolve().parent / "many_tools_mcp.py")
# What says a worker did not report with its tool (Workers.ask_for_report).
UNREPORTED = ("without a report", "filed as one")


def turns(session_id: str) -> list[dict]:
    """The tools of each model call of a session (its log's usage checkpoints), read only."""
    log_file = copilot_home() / "session-state" / session_id / "events.jsonl"
    found = []
    for line in log_file.read_text().splitlines() if log_file.exists() else []:
        event = json.loads(line)
        if event.get("type") != "session.usage_checkpoint":
            continue
        state = (event.get("data") or {}).get("promptCacheBreakState") or []
        for conversation in state:
            if conversation.get("conversation") != "main":
                continue
            model = conversation["models"][conversation["lastActiveModel"]]
            found.append(
                {
                    "tool_tokens": model.get("tool_tokens"),
                    "tools_truncated": model.get("tools_truncated"),
                    "sent": {tool["name"] for tool in model.get("tools") or []},
                    "left_out": tools_left_out([conversation]),
                }
            )
    return found


async def scenario_tools(app: App, calls: Path):
    """V1: a worker with about 250 MCP tools reaches them, and its own tools work."""
    state = await app.get("/api/state")
    router = state["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": "Start a worker for this: call the `ping` tool of the acme tools (an MCP "
            "server; search your tools for it) and note what it answers. Then set a check-back "
            "in 5 minutes to call ping again, and end your turn. When woken, call ping again "
            "and report done with both answers. Nothing else."
        },
    )

    async def started():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        found = [m["meta"]["session_id"] for m in messages
                 if m["kind"] == "notice" and m["text"].startswith("Started")]  # fmt: skip
        return found[0] if found else None

    worker = await wait_for("the Router starts a worker", started, 300, app)

    async def checking_back():
        tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] == worker]
        return next((t for t in tasks if t.get("check_back")), None)

    task = await wait_for("it pings, then sets a check-back (check_back)", checking_back, 600, app)
    pinged = calls.read_text().split() if calls.exists() else []
    assert "ping" in pinged, f"ping was not called before the check-back: {pinged}"
    log(f"check-back: {task['check_back']['what']!r} at {task['check_back']['at']}")

    async def reported():
        tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["id"] == task["id"]]
        return tasks[0] if tasks and tasks[0]["status"] in {"review", "done"} else None

    await wait_for("woken by it, it reports (report)", reported, 900, app)
    messages = await app.get(f"/api/sessions/{worker}/messages")
    reports = [m for m in messages if m["kind"] == "report"]
    assert reports and "pong" in reports[-1]["text"].lower(), transcript(messages)
    assert calls.read_text().split().count("ping") >= 2, "woken, it pinged again"
    unreported = [m["text"] for m in messages if any(word in m["text"] for word in UNREPORTED)]
    assert not unreported, f"it did not report with its tool: {unreported}"
    troubles = [m["text"] for m in messages if m["kind"] == "error" or "didn't fit" in m["text"]
                or "left out" in m["text"]]  # fmt: skip
    assert not troubles, troubles
    sessions = [s for s in (await app.get("/api/state"))["sessions"] if s["role"] == "worker"]
    workers = [s for s in sessions if not s.get("helper")]
    assert len(workers) == 1, f"one worker, no replacement: {[w['title'] for w in workers]}"
    seen = turns(worker)
    assert seen, "its log has no usage checkpoint"
    for number, turn in enumerate(seen, 1):
        assert not turn["left_out"] and not turn["tools_truncated"], f"call {number}: {turn}"
        assert "tool_search_tool" in turn["sent"], f"call {number}: tool search was off"
        assert {"report", "check_back"} <= turn["sent"], f"call {number}: its own tools left out"
    tokens = [turn["tool_tokens"] for turn in seen]
    log(f"V1: {len(seen)} model calls, tool tokens {min(tokens)}-{max(tokens)}, none left out")
    # Chief of Staff's own workers with the same MCP servers (Watch for you, say): nothing left
    # out of theirs either.
    for helper in [s for s in sessions if s.get("helper")]:
        for turn in turns(helper["id"]):
            assert not turn["left_out"], f"{helper['title']}: {turn}"


async def scenario_reconnect(app: App):
    """V2: a message sent while Chief of Staff connects to Copilot again waits, then goes."""
    router = (await app.get("/api/state"))["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    reconnecting = asyncio.ensure_future(app.post("/api/runtime/reconnect"))
    for _ in range(50):  # Until it is connecting.
        if (await app.get("/api/state"))["runtime"]["state"] == "starting":
            break
        await asyncio.sleep(0.1)
    sent = time.monotonic()
    await app.post(f"/api/sessions/{router}/messages", {"text": "Reply with just: here"})
    log(f"sent {time.monotonic() - sent:.1f}s into connecting")
    await reconnecting
    assert (await app.get("/api/state"))["runtime"]["state"] == "ready"

    async def answered():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        failed = [m["text"] for m in messages if m["kind"] == "error"]
        assert not failed, failed
        return any(m["kind"] == "assistant" and "here" in m["text"].lower() for m in messages)

    await wait_for("the Router answers it", answered, 300, app)
    log("V2: delivered after the reconnect, no error")


async def main():
    chosen = sys.argv[1:] or ["V1", "V2"]
    with tempfile.TemporaryDirectory(prefix="cos-live-hotfix-") as temporary:
        root = Path(temporary).resolve()
        data, calls = root / "data", root / "calls.txt"
        app = App(data, root / "chiefofstaff.log")
        failed = False
        try:
            await app.start()
            state = await app.get("/api/state")
            assert state["runtime"]["state"] == "ready", state["runtime"]
            warning = state["runtime"].get("warning") or ""
            log(f"connected: {warning or 'the owner CLI'}")
            server = {"type": "local", "command": sys.executable, "args": [SERVER],
                      "env": {"MANY_TOOLS_CALLS": str(calls)}, "tools": ["*"]}  # fmt: skip
            await app.post(
                "/api/settings",
                {
                    "mcp_servers": {"acme": server},
                    "role_models": {"worker": MODEL},
                    "max_workers": 1,
                    "mac_notifications": False,
                    "digest_time": "",
                },
            )
            if "V1" in chosen:
                await scenario_tools(app, calls)
            if "V2" in chosen:
                await scenario_reconnect(app)
            log("HOTFIX SCENARIOS PASSED")
        except Exception:
            failed = True
            raise
        finally:
            await app.stop()
            await cleanup(data)
            if failed:
                print((root / "chiefofstaff.log").read_text()[-6000:], file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
