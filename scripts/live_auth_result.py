"""Live E116: an MCP tool that says its account must be connected, on real Copilot.

The tool is a local fake Acme Search MCP server (acme_auth_result_mcp.py): until a local
"browser step" has been followed, its tool fails with "Authorization required for Acme Search.
Connect your Acme Search account in a browser at <link>"; after, it works. No outside account
is used.

A. Two workers both meet it: one shared card under Needs you (not a question from each), its
   link labelled as the server's; the owner follows the link and clicks I've done it: each
   worker is told once to retry, a retry works, and the card closes.
B. Watch for you meets it in a scan: a card for it too, and Watch says "connection needed",
   not "0 found"; after I've done it, it scans again at once and the card closes.

    uv run --with playwright python scripts/live_auth_result.py [A|B ...]

Ports: LIVE_AUTH_RESULT_PORT (20740) for Chief of Staff, LIVE_AUTH_RESULT_LINK_PORT (20741) for
the browser step.
"""

import asyncio
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e

live_e2e.PORT = int(os.environ.get("LIVE_AUTH_RESULT_PORT", "20740"))
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import App, cleanup, log, transcript, wait_for  # noqa: E402

LINK_PORT = int(os.environ.get("LIVE_AUTH_RESULT_LINK_PORT", "20741"))
LINK = f"http://127.0.0.1:{LINK_PORT}/connect"
SERVER = str(Path(__file__).resolve().parent / "acme_auth_result_mcp.py")
RETRY = "Retry your failed"
ASKS = re.compile(r"connect|authori[sz]", re.IGNORECASE)


def browser_step(marker: Path) -> ThreadingHTTPServer:
    """The page the card's link opens: following it connects the account."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?", 1)[0] != "/connect":
                self.send_error(404)
                return
            marker.write_text("connected\n")
            body = b"Acme Search connected. Go back to Chief of Staff."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", LINK_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def outcomes(calls: Path) -> list[str]:
    return calls.read_text().splitlines() if calls.exists() else []


async def connection_cards(app: App) -> list[dict]:
    return [d for d in (await app.get("/api/state"))["drafts"] if d["kind"] == "connection"]


async def connect(app: App, card: dict, marker: Path):
    """The owner opens the link (only on a click, so here: by hand) and says it is done."""
    assert card["target"]["url"] == LINK, card["target"]
    assert card["target"]["url_note"] == "Link supplied by acme-platform", card["target"]
    async with httpx.AsyncClient(timeout=10) as client:
        (await client.get(card["target"]["url"])).raise_for_status()
    assert marker.exists()
    answered = await app.post(f"/api/drafts/{card['id']}", {"action": "answer"})
    assert answered["status"] == "pending", "done starts retries; only one that works closes it"


async def closed(app: App, card: dict) -> bool:
    return not any(d["id"] == card["id"] for d in (await app.get("/api/state"))["drafts"])


async def scenario_a(app: App, marker: Path, calls: Path):
    """Two workers, one card; each retries once after I've done it; a retry closes it."""
    state = await app.get("/api/state")
    router = state["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": "Start two separate workers now, one titled 'Acme launch' and one titled "
            "'Acme plan'. Each calls the search_acme tool of the acme-platform MCP server once, "
            "with its title as the query, and reports what it found."
        },
    )

    async def shared():
        cards = await connection_cards(app)
        if len(cards) > 1:
            raise AssertionError(f"more than one card: {cards}")
        return cards[0] if cards and cards[0]["target"]["affected_sessions"] == 2 else None

    card = await wait_for("one card for both workers", shared, 600, app)
    assert card["text"] == "acme-platform needs you to connect Acme Search", card
    assert set(outcomes(calls)) == {"blocked"}, outcomes(calls)
    drafts = (await app.get("/api/state"))["drafts"]
    asked = [d for d in drafts if d["kind"] != "connection" and ASKS.search(d["text"])]
    assert not asked, f"no worker asks the owner to connect it itself: {asked}"
    await connect(app, card, marker)
    await wait_for("a retry works and the card closes", lambda: closed(app, card), 600, app)
    workers = [s for s in (await app.get("/api/state"))["sessions"] if s["role"] == "worker"]
    workers = [s for s in workers if s["title"] and "acme" in s["title"].lower()]
    for worker in workers:
        said = await app.get(f"/api/sessions/{worker['id']}/messages")
        retries = [m for m in said if RETRY in m["text"]]
        assert len(retries) <= 1, f"told to retry at most once: {transcript(said)}"
    assert "ok" in outcomes(calls), outcomes(calls)
    messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
    log(f"A: calls {outcomes(calls)}; router said:\n{transcript(messages[-4:])}")
    log("A passed: one card for two workers; a retry after I've done it closed it")


async def scenario_b(app: App, marker: Path, calls: Path):
    """Watch for you: a card and "connection needed"; after I've done it, a scan at once."""
    marker.unlink(missing_ok=True)
    calls.unlink(missing_ok=True)
    await app.post(
        "/api/settings",
        {
            "watches": [
                {
                    "what": "Acme Search launch notes that ask me something",
                    "how": "the search_acme tool of the acme-platform MCP server",
                }
            ],
            "watch_hours": "00:00-23:59",
            "watch_minutes": 240,
            "watch_after_hours_minutes": 0,
        },
    )
    await app.post("/api/watch/scan")

    async def needed():
        snap = await app.get("/api/state")
        connector = snap["connector"]
        cards = [d for d in snap["drafts"] if d["kind"] == "connection"]
        if connector["scanning"] or not cards or not connector["connection_needed"]:
            return None
        return cards[0], connector

    card, connector = await wait_for("Watch says a connection is needed", needed, 900, app)
    assert connector["connection_needed"]["source"] == "Acme Search", connector
    assert set(outcomes(calls)) == {"blocked"}, outcomes(calls)
    before = connector["last"].get("at", "")
    await connect(app, card, marker)

    async def rescanned():
        snap = await app.get("/api/state")
        connector = snap["connector"]
        done = connector["last"].get("at", "") != before and not connector["scanning"]
        return connector if done and await closed(app, card) else None

    connector = await wait_for("a scan at once, and the card closes", rescanned, 900, app)
    assert connector["connection_needed"] is None, connector
    assert "ok" in outcomes(calls), outcomes(calls)
    log(f"B: calls {outcomes(calls)}; last scan {connector['last']}")
    log("B passed: Watch said connection needed, scanned again at once, and the card closed")


async def main():
    wanted = set(sys.argv[1:]) or {"A", "B"}
    with tempfile.TemporaryDirectory(prefix="cos-live-auth-") as temporary:
        root = Path(temporary).resolve()
        project = root / "project"
        project.mkdir()
        git = ["git", "-C", str(project)]
        subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
        (project / "README.md").write_text("# Launch notes\n")
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run(
            [*git, "-c", "user.email=live@test", "-c", "user.name=live", "commit", "-qm", "init"],
            check=True,
        )
        data, marker, calls = root / "data", root / "connected", root / "calls.txt"
        app = App(data, root / "chiefofstaff.log")
        link = browser_step(marker)
        failed = False
        try:
            await app.start()
            server = {
                "type": "local",
                "command": sys.executable,
                "args": [SERVER],
                "env": {
                    "ACME_AUTH_MARKER": str(marker),
                    "ACME_AUTH_CALLS": str(calls),
                    "ACME_AUTH_URL": LINK,
                },
                "tools": ["*"],
            }
            await app.post(
                "/api/settings",
                {
                    "projects": [str(project)],
                    "mcp_servers": {"acme-platform": server},
                    "max_workers": 3,
                    "mac_notifications": False,
                    "digest_time": "",
                },
            )
            if "A" in wanted:
                await scenario_a(app, marker, calls)
            if "B" in wanted:
                await scenario_b(app, marker, calls)
            log("AUTH RESULT SCENARIOS PASSED")
        except BaseException:
            failed = True
            raise
        finally:
            await app.stop()
            link.shutdown()
            link.server_close()
            await cleanup(data)
            if failed and (root / "chiefofstaff.log").exists():
                print((root / "chiefofstaff.log").read_text()[-6000:], file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
