import pytest
from conftest import FakeSlackMcp, FakeTokens, slack_tool

from work_buddy.models import Settings
from work_buddy.sources.slack import SlackError, SlackSource, message_keys
from work_buddy.store import Store

RESULT = """Results:
1. From Alice in #deploys: can you check the rollout?
   https://acme.slack.com/archives/C111/p1712345678000100
2. DM from Bob: lunch?  ts: 1712345690.000200
"""


def test_message_keys_come_from_permalinks_and_timestamps():
    assert message_keys(RESULT) == ["slack:1712345678.000100", "slack:1712345690.000200"]
    assert message_keys("No results found.") == []


class FakeHost:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.thread_calls = []
        self.subscriptions = []

    async def on_trigger(self, name, keys, text, display=None):
        self.calls.append((name, keys, text, display))
        for key in keys:
            self.store.add_trigger(name, key, text)
        return True

    async def on_thread(self, name, thread, keys, text, display=None):
        self.thread_calls.append((thread, keys, text))
        for key in keys:
            self.store.add_trigger(name, key, text)
        return True

    def followed(self, prefix):
        return [sub for sub in self.subscriptions if sub["thread"].startswith(prefix)]

    def unfollow(self, thread):
        self.subscriptions = [sub for sub in self.subscriptions if sub["thread"] != thread]


@pytest.fixture
def source(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings(slack_enabled=True))
    host = FakeHost(store)
    slack = SlackSource(store, host, tokens=FakeTokens(), mcp=FakeSlackMcp())
    slack.calls = host.calls
    return slack


async def test_watcher_starts_from_now_then_reports_only_new_messages(source):
    await source.poll_once()
    assert source.calls == [] and source.store.get("slack_cursor")
    assert source.status["user_id"] == "U0OWNER1"
    assert source.read_tools == ["slack_read_thread", "slack_search_public_and_private"]

    source.mcp.results["to:me"] = RESULT
    await source.poll_once()
    assert len(source.calls) == 1
    name, keys, text, display = source.calls[0]
    assert name == "slack" and keys == ["slack:1712345678.000100", "slack:1712345690.000200"]
    assert "can you check the rollout" in text
    assert display == RESULT, "the owner sees the messages, not the watcher's preamble"
    searches = [
        args for tool, args in source.mcp.calls if tool == "slack_search_public_and_private"
    ]
    assert [args["query"] for args in searches] == ["to:me", "<@U0OWNER1>"]
    assert searches[0]["sort"] == "timestamp" and searches[0]["include_context"] is False

    await source.poll_once()
    assert len(source.calls) == 1, "already delivered messages must not wake the Router again"


async def test_cursor_does_not_advance_when_the_router_cannot_take_events(source):
    await source.poll_once()
    cursor = source.store.get("slack_cursor")

    async def failing(name, keys, text, display=None):
        raise RuntimeError("Copilot is not connected")

    source.host.on_trigger = failing
    source.mcp.results["to:me"] = RESULT
    with pytest.raises(RuntimeError):
        await source.poll_once()
    assert source.store.get("slack_cursor") == cursor


async def test_watcher_requires_search_scopes(source):
    source.mcp.tools = [slack_tool("slack_send_message", read_only=False)]
    with pytest.raises(SlackError, match="cannot search"):
        await source.poll_once()


async def test_disabled_or_tokenless_watcher_stays_idle(source):
    source.tokens.token = ""
    await source.poll_once()
    assert source.status["error"].startswith("Add your Slack user token")
    assert source.mcp.calls == []
    source.store.save_settings(Settings(slack_enabled=False))
    await source.poll_once()
    assert source.status["error"] == ""
    assert source.session_mcp_config() is None


async def test_watcher_pages_through_busy_windows(source):
    await source.poll_once()
    stamps = [f"17123456{i:02d}.000100" for i in range(25)]
    pages = {
        "0": "\n".join(f"DM ts: {stamp}" for stamp in stamps[:20]),
        stamps[19].split(".")[0]: "\n".join(f"DM ts: {stamp}" for stamp in stamps[19:]),
    }

    async def call(name, arguments):
        source.mcp.calls.append((name, arguments))
        if arguments["query"] != "to:me":
            return ""
        return pages.get(arguments["after"], pages["0"])

    source.store.set("slack_cursor", "300")
    source.mcp.call = call
    await source.poll_once()
    assert source.calls[0][1] == [f"slack:{stamp}" for stamp in stamps]


THREAD = "slack:C0DEPLOYS:1712345678.000100"
REPLIES = """Channel: <#C0DEPLOYS> | From: <@U0BOB001> | ts: 1712345678.000100
Text: Deploy failing again?
Permalink: https://acme.slack.com/archives/C0DEPLOYS/p1712345678000100
Channel: <#C0DEPLOYS> | From: <@U0OWNER1> | ts: 1712345690.000200
Text: Looking.
Permalink: https://acme.slack.com/archives/C0DEPLOYS/p1712345690000200?thread_ts=1712345678.000100
Channel: <#C0DEPLOYS> | From: <@U0BOB001> | ts: 1712345699.000300
Text: Any update?
Permalink: https://acme.slack.com/archives/C0DEPLOYS/p1712345699000300?thread_ts=1712345678.000100
"""


async def test_followed_threads_go_to_their_followers_not_the_router(source):
    await source.poll_once()
    source.host.subscriptions = [{"thread": THREAD, "since": 1712345680.0}]
    source.store.add_trigger("slack-sent", "slack:1712345690.000200", "our own reply")
    source.store.add_trigger("slack", "slack:1712345678.000100", "Bob's mention, routed earlier")
    source.mcp.reads["C0DEPLOYS"] = REPLIES
    source.mcp.results["<@U0OWNER1>"] = REPLIES
    source.mcp.results["to:me"] = (
        "DM <#D0ALICE01> ts: 1712345700.000400\n"
        "Permalink: https://acme.slack.com/archives/D0ALICE01/p1712345700000400"
    )
    await source.poll_once()

    assert source.host.thread_calls == [
        (THREAD, ["slack:1712345699.000300"], source.host.thread_calls[0][2])
    ]
    assert "Any update?" in source.host.thread_calls[0][2]
    reads = [args for tool, args in source.mcp.calls if tool == "slack_read_thread"]
    assert reads[-1] == {
        "channel_id": "C0DEPLOYS",
        "message_ts": "1712345678.000100",
        "oldest": "1712345680",
        "limit": 50,
    }
    [(name, keys, text, display)] = source.calls
    assert keys == ["slack:1712345700.000400"], "thread replies never reach the Router"
    assert "Thread keys: slack:D0ALICE01:1712345700.000400, slack:D0ALICE01" in text


async def test_followed_dm_is_read_as_a_conversation(source):
    await source.poll_once()
    source.host.subscriptions = [{"thread": "slack:D0ALICE01", "since": 1712345600.0}]
    source.mcp.reads["D0ALICE01"] = "ts: 1712345650.000100 Alice: thanks! one more thing"
    await source.poll_once()
    reads = [args for tool, args in source.mcp.calls if tool == "slack_read_channel"]
    assert reads[-1]["channel_id"] == "D0ALICE01"
    assert source.host.thread_calls[0][:2] == ("slack:D0ALICE01", ["slack:1712345650.000100"])


async def test_an_unreadable_followed_thread_does_not_block_dms(source):
    await source.poll_once()
    source.host.subscriptions = [{"thread": THREAD, "since": 1712345680.0}]
    calls = source.mcp.call

    async def call(name, arguments):
        if name == "slack_read_thread":
            raise RuntimeError("channel_not_found")
        return await calls(name, arguments)

    source.mcp.call = call
    source.mcp.results["to:me"] = RESULT
    for _ in range(8):
        await source.poll_once()
    assert source.calls and source.calls[0][1] == [
        "slack:1712345678.000100",
        "slack:1712345690.000200",
    ]
    assert source.store.get(f"slackfail:{THREAD}") == "8", "it backs off instead of dropping"


async def test_dm_thread_replies_found_by_search_reach_the_follower(source):
    await source.poll_once()
    source.host.subscriptions = [{"thread": "slack:D0ALICE01", "since": 1712345600.0}]
    source.mcp.results["to:me"] = (
        "Alice in a DM thread: one more thing\nPermalink: https://acme.slack.com/archives/"
        "D0ALICE01/p1712345700000400?thread_ts=1712345650.000100"
    )
    await source.poll_once()
    assert source.calls == []
    assert source.host.thread_calls[-1][:2] == ("slack:D0ALICE01", ["slack:1712345700.000400"])
