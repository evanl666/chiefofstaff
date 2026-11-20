"""E116: an MCP tool that says its account must be connected is one shared card (accounts.py),
not a request from each session it stops; the link it gives is untrusted."""

import asyncio
import json
import re
import time
from types import SimpleNamespace

import pytest
from conftest import Harness, call_tool, settle

from chiefofstaff.connector import GAP_TRIES, watch_key
from chiefofstaff.models import DraftDecision, Settings, Watch

ACME = {
    "acme-platform": {
        "type": "http",
        "url": "https://mcp.acme.test/api",
        "authorization_url": "https://auth.acme.test/oauth",
    },
    "paper-server": {"type": "http", "url": "https://paper.test/mcp"},
}
# As Copilot reports an MCP tool's error result (isError): a failed call, its text in both.
ERROR = (
    "MCP server 'acme-platform': Authorization required for Acme Search. Connect your Acme "
    "Search account in a browser at https://auth.acme.test/oauth?client=chief"
)
RETRY = "Retry your failed search call once"


async def start(tmp_path, **settings):
    harness = Harness(tmp_path / "data", Settings(mcp_servers=ACME, **settings))
    return harness, await harness.connect()


async def spawn(runtime, router, title):
    result = await call_tool(
        runtime, "spawn_worker", router, title=title, instructions=title, why_new="test"
    )
    return json.loads(result.text_result_for_llm)["session_id"]


def mcp_call(harness, session_id, call, *, server="acme-platform", tool="search", text=ERROR,
             success=False, mcp=True):  # fmt: skip
    session = harness.session(session_id)
    named = (
        {"mcp_config_server_name": server, "mcp_server_name": server, "mcp_tool_name": tool}
        if mcp
        else {}
    )
    session.emit("tool.execution_start", tool_call_id=call, tool_name=f"{server}-{tool}", **named)
    session.emit(
        "tool.execution_complete",
        tool_call_id=call,
        success=success,
        result=SimpleNamespace(content=text),
        error=None if success else SimpleNamespace(message=text, code="failure"),
    )


def cards(harness) -> list[dict]:
    return [draft for draft in harness.store.drafts() if draft["kind"] == "connection"]


def alerts(harness) -> list[str]:
    return [text for _, text in harness.notifications if "needs you to connect" in text]


def told(harness, session_id, words) -> int:
    return sum(words in sent for sent in harness.session(session_id).sent)


async def test_two_sessions_share_one_card_and_each_retries_once_when_done(tmp_path):
    harness, runtime = await start(tmp_path)
    first = await spawn(runtime, harness.router_id, "Search one")
    second = await spawn(runtime, harness.router_id, "Search two")
    mcp_call(harness, first, "a1")
    mcp_call(harness, second, "a2")
    await settle(runtime)

    [card] = cards(harness)
    assert card["text"] == "acme-platform needs you to connect Acme Search"
    target = card["target"]
    assert target["affected_sessions"] == 2
    assert target["last_error"] == ERROR
    assert target["url"] == "https://auth.acme.test/oauth?client=chief"
    assert target["known_origin"] is True and target["url_note"] == ""
    assert len(alerts(harness)) == 1, "one alert, not one per session"
    for worker in (first, second):
        assert told(harness, worker, "needs the owner to connect Acme Search") == 1
        assert told(harness, worker, "do not retry it now") == 1

    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer"))
    await settle(runtime)
    card = harness.store.draft(card["id"])
    assert card["status"] == "pending", "done is not connected: only a retry that works is"
    assert card["target"]["retrying"] is True
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer"))
    await settle(runtime)
    for worker in (first, second):
        assert told(harness, worker, RETRY) == 1, "each told once, however often it is clicked"

    mcp_call(harness, second, "a3", text=ERROR + "&attempt=2")
    await settle(runtime)
    card = harness.store.draft(card["id"])
    assert card["status"] == "pending" and card["target"]["retrying"] is False
    assert card["target"]["last_error"].endswith("&attempt=2"), "the card says the new failure"
    assert len(alerts(harness)) == 1, "a repeated failure updates the card, without an alert"
    assert told(harness, second, "needs the owner to connect") == 1, "and is not told again"

    mcp_call(harness, first, "a4", success=True, text='{"results": []}')
    await settle(runtime)
    assert harness.store.draft(card["id"])["status"] == "answered"
    assert harness.store.draft(card["id"])["outcome"] == "Connected: a retry worked."
    assert not cards(harness)
    assert told(harness, second, RETRY) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Authorization required for Acme Search. Connect in a browser at javascript:alert(1)",
        "Authorization required. Open data:text/html;base64,PHNjcmlwdD4=",
        "Authorization required. Open https://auth.acme.test@evil.test/oauth",
        "Authorization required. Open https://evil.test\\@auth.acme.test/oauth",
        "Authorization required. Open https://auth.acme.test/\u202eoauth",
        "Authorization required. Open acme-app://connect?then=https://auth.acme.test/",
        "Authorization required. Open https://login.evil.test/a or https://other.evil.test/b",
        "<b>Search failed.</b> Status at https://auth.acme.test/status",
        "Could not connect to the server: connection refused",
    ],
)
async def test_an_untrusted_or_ordinary_failure_makes_no_card(tmp_path, text):
    harness, runtime = await start(tmp_path)
    worker = await spawn(runtime, harness.router_id, "Search")
    mcp_call(harness, worker, "c1", text=text)
    await settle(runtime)
    assert not cards(harness) and not alerts(harness)
    assert told(harness, worker, "needs the owner to connect") == 0
    mcp_call(harness, worker, "c2")
    await settle(runtime)
    assert len(cards(harness)) == 1, "while a plain one with a known link makes one"


async def test_only_a_known_mcp_tools_failure_counts(tmp_path):
    harness, runtime = await start(tmp_path)
    worker = await spawn(runtime, harness.router_id, "Search")
    mcp_call(harness, worker, "plain", mcp=False)  # Any tool's text, no MCP server named.
    mcp_call(harness, worker, "worked", success=True)  # It said so, but the call worked.
    await settle(runtime)
    assert not cards(harness)
    mcp_call(harness, worker, "failed")
    await settle(runtime)
    assert len(cards(harness)) == 1


async def test_known_links_unknown_links_and_separate_servers_and_sources(tmp_path):
    harness, runtime = await start(tmp_path)
    worker = await spawn(runtime, harness.router_id, "Search")
    mcp_call(
        harness, worker, "acme",
        text="Authentication is required for Acme Search. Authorize the account in your "
        "browser at HTTPS://AUTH.ACME.TEST:443/oauth?flow=1.",
    )  # fmt: skip
    mcp_call(
        harness, worker, "drive", tool="files",
        text="Authorization required for Acme Drive. Open https://auth.acme.test/drive",
    )  # fmt: skip
    mcp_call(
        harness, worker, "paper", server="paper-server", tool="lookup",
        text="Your Paper Archive account is not connected. Connect your Paper Archive account "
        "at https://login.paper.test/connect",
    )  # fmt: skip
    await settle(runtime)
    by_text = {card["text"]: card["target"] for card in cards(harness)}
    assert sorted(by_text) == [
        "acme-platform needs you to connect Acme Drive",
        "acme-platform needs you to connect Acme Search",
        "paper-server needs you to connect Paper Archive",
    ]
    acme = by_text["acme-platform needs you to connect Acme Search"]
    assert acme["url"] == "https://auth.acme.test/oauth?flow=1", "normalized, shown as opened"
    assert acme["known_origin"] is True
    paper = by_text["paper-server needs you to connect Paper Archive"]
    assert paper["url"] == "https://login.paper.test/connect"
    assert paper["known_origin"] is False
    assert paper["url_note"] == "Link supplied by paper-server"
    assert len(alerts(harness)) == 3


def test_a_link_is_shown_as_it_opens():
    from chiefofstaff.accounts import web_link

    assert web_link("HTTP://Auth.Acme.Test:80/a?b=1#c") == (
        "http://auth.acme.test/a?b=1#c",
        "http://auth.acme.test",
    )
    assert web_link("https://\u0430cme.test:8443/x")[1] == "https://xn--cme-5cd.test:8443"
    for bad in ("https://user:pw@acme.test/", "ftp://acme.test/", "https://acme .test/"):
        with pytest.raises(ValueError, match="Not a web address"):
            web_link(bad)


async def test_not_now_quiets_it_and_a_closed_session_leaves_it(tmp_path):
    harness, runtime = await start(tmp_path)
    first = await spawn(runtime, harness.router_id, "Search one")
    mcp_call(harness, first, "n1")
    await settle(runtime)
    [card] = cards(harness)
    await runtime.drafts.resolve(card["id"], DraftDecision(action="reject"))
    await settle(runtime)
    assert told(harness, first, "chose not to connect Acme Search now") == 1
    second = await spawn(runtime, harness.router_id, "Search two")
    mcp_call(harness, second, "n2")
    await settle(runtime)
    assert not cards(harness) and len(alerts(harness)) == 1, "not asked again for a while"
    assert told(harness, second, "they chose not to for now") == 1

    third = await spawn(runtime, harness.router_id, "Search three")
    mcp_call(harness, third, "n3", server="paper-server", tool="lookup",
             text="Authorization required for Paper Archive.")  # fmt: skip
    await settle(runtime)
    [paper] = cards(harness)
    await runtime.workers.close(third)
    await settle(runtime)
    assert harness.store.draft(paper["id"])["status"] == "expired", "no session waits on it"


async def test_the_card_outlives_a_restart_and_can_be_answered_again(tmp_path):
    harness, runtime = await start(tmp_path)
    worker = await spawn(runtime, harness.router_id, "Search")
    mcp_call(harness, worker, "r1")
    await settle(runtime)
    [card] = cards(harness)
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer"))
    await settle(runtime)
    assert harness.store.draft(card["id"])["target"]["retrying"] is True
    runtime.asks.expire_requests()  # As at a restart: its retries ended with the last run.
    runtime.accounts.restore()
    card = harness.store.draft(card["id"])
    assert card["status"] == "pending" and card["target"]["retrying"] is False
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer"))
    await settle(runtime)
    assert told(harness, worker, RETRY) == 2, "answered again, it retries again"


async def test_a_tool_failure_during_copilots_sign_in_joins_that_card(tmp_path):
    harness, runtime = await start(tmp_path)
    first = await spawn(runtime, harness.router_id, "Search one")
    second = await spawn(runtime, harness.router_id, "Search two")
    harness.session(first).login_url = "https://auth.acme.test/oauth?state=1"
    request = {
        "requestId": "r1",
        "serverName": "acme-platform",
        "serverUrl": "https://mcp.acme.test/api",
    }
    signing = asyncio.create_task(runtime.asks.on_mcp_auth(request, {"sessionId": first}))
    await settle(runtime)
    mcp_call(harness, second, "s1")
    await settle(runtime)
    [signin] = harness.store.drafts()
    assert signin["kind"] == "signin", "one card: Copilot's sign-in"
    assert told(harness, second, "acme-platform needs sign-in") == 1

    harness.session(first).emit(
        "session.mcp_server_status_changed", server_name="acme-platform", status="connected"
    )
    assert await signing == {"kind": "cancelled"}
    await settle(runtime)
    assert harness.store.draft(signin["id"])["status"] == "answered"
    assert told(harness, second, RETRY) == 1


async def test_copilots_sign_in_takes_over_a_card_for_the_same_address(tmp_path):
    harness, runtime = await start(tmp_path)
    first = await spawn(runtime, harness.router_id, "Search one")
    second = await spawn(runtime, harness.router_id, "Search two")
    mcp_call(harness, second, "t1")
    await settle(runtime)
    [card] = cards(harness)
    harness.session(first).login_url = "https://auth.acme.test/oauth?state=2"
    request = {
        "requestId": "r2",
        "serverName": "acme-platform",
        "serverUrl": "https://mcp.acme.test/api",
    }
    signing = asyncio.create_task(runtime.asks.on_mcp_auth(request, {"sessionId": first}))
    await settle(runtime)
    assert harness.store.draft(card["id"])["status"] == "expired"
    [signin] = harness.store.drafts()
    assert signin["kind"] == "signin"
    await runtime.drafts.resolve(signin["id"], DraftDecision(action="answer"))
    assert await signing == {"kind": "cancelled"}
    await settle(runtime)
    assert told(harness, second, RETRY) == 1


async def scan(harness, runtime) -> tuple[str, str]:
    harness.store.set("watch_scanned_at", "0")
    await runtime.connector.scan()
    await settle(runtime)
    row = runtime.connector.session()
    return row["id"], harness.session(row["id"]).sent[-1]


def since(prompt: str) -> str:
    return re.search(r"new since (\S+)$", prompt, re.MULTILINE).group(1)


async def test_watch_keeps_its_window_and_scans_again_once_connected(tmp_path):
    watches = [Watch(what="Acme Search launch notes"), Watch(what="DMs to me")]
    harness, runtime = await start(tmp_path, watches=watches, watch_after_hours_minutes=0)
    keys = [watch_key(watch.what) for watch in watches]
    marks = [str(time.time() - 3600), str(time.time() - 1800)]
    for key, mark in zip(keys, marks, strict=True):
        harness.store.set(f"watchdone:{key}", mark)
    harness.store.set(f"watchpartial:{keys[0]}", json.dumps({"tries": GAP_TRIES - 1}))
    connector, first = await scan(harness, runtime)
    assert "Try every watch once in this scan" in first
    mcp_call(harness, connector, "w1")
    await settle(runtime)
    assert told(harness, connector, "list the watch that needs it in incomplete") == 1
    # It says nothing of the watch it could not search: still not covered.
    await call_tool(runtime, "report_found", connector, items=[])
    harness.session(connector).emit("session.idle")
    await settle(runtime)
    assert harness.store.get(f"watchdone:{keys[0]}") == marks[0], "its window stays open"
    assert harness.store.get(f"watchdone:{keys[1]}") != marks[1], "the other read on"
    assert not json.loads(harness.store.get("watch_gaps") or "[]"), "not given up meanwhile"
    view = runtime.connector.view()
    assert view["connection_needed"]["server"] == "acme-platform"
    assert view["connection_needed"]["source"] == "Acme Search"

    [card] = cards(harness)
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer"))
    await settle(runtime)
    again = harness.session(connector).sent[-1]
    assert again.startswith("<") and "The owner connected Acme Search" in again
    assert since(again) == since(first), "at once, from where it was last covered"

    mcp_call(harness, connector, "w2", success=True, text="[]")
    await settle(runtime)
    assert runtime.connector.view()["connection_needed"] is None
    assert harness.store.draft(card["id"])["status"] == "answered"


async def test_watch_done_while_a_scan_runs_scans_again_right_after(tmp_path):
    harness, runtime = await start(
        tmp_path, watches=[Watch(what="Acme Search launch notes")], watch_after_hours_minutes=0
    )
    connector, _ = await scan(harness, runtime)
    mcp_call(harness, connector, "b1")
    await settle(runtime)
    [card] = cards(harness)
    sent = len(harness.session(connector).sent)
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer"))
    await settle(runtime)
    assert len(harness.session(connector).sent) == sent, "the scan under way goes on"
    harness.session(connector).emit("session.idle")
    await settle(runtime)
    assert runtime.connector.due(), "due at once after it, whatever the schedule"
    await runtime.connector.tick()
    await settle(runtime)
    assert "The owner connected Acme Search" in harness.session(connector).sent[-1]
    assert not runtime.connector.due()
