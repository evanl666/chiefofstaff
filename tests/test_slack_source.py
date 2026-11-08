import pytest
from conftest import FakeSlackMcp, FakeTokens, Harness, call_tool, slack_tool

from chiefofstaff.models import DraftDecision, Settings
from chiefofstaff.sources.slack import SlackError, SlackSource, message_keys, newest_message
from chiefofstaff.store import Store

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
        self.delegated = []
        self.subscriptions = []

    async def on_trigger(self, name, keys, text, display=None, kind="event"):
        if kind == "event":
            self.calls.append((name, keys, text, display))
        else:
            self.delegated.append((keys, text))
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
    assert [args["query"] for args in searches] == [
        "hasmy::robot_face:",  # The first check only notes what was there before.
        "to:me",
        "<@U0OWNER1>",
        "hasmy::robot_face:",
    ]
    assert searches[1]["sort"] == "timestamp" and searches[1]["include_context"] is False

    await source.poll_once()
    assert len(source.calls) == 1, "already delivered messages must not wake the Router again"


REACTED = """1. From Alice in #support: exports time out for big accounts
   https://acme.slack.com/archives/C0SUPPORT/p1712345600000100
"""
REACTED_NOW = (
    """1. From Dan in #deploys: can someone bump the API timeout?
   https://acme.slack.com/archives/C0DEPLOYS/p1712345700000300?thread_ts=1712345650.000200
"""
    + REACTED
)


async def test_reacting_with_the_delegate_emoji_hands_a_message_over(source):
    # Reactions from before the watcher started are history, not requests.
    source.mcp.results["hasmy::robot_face:"] = REACTED
    await source.poll_once()
    await source.poll_once()
    assert source.host.delegated == []

    source.mcp.results["hasmy::robot_face:"] = REACTED_NOW
    await source.poll_once()
    [(keys, text)] = source.host.delegated
    assert keys == ["slack-delegated:1712345700.000300"]
    assert "reacted with :robot_face:" in text and "new ts: 1712345700.000300" in text
    assert "Thread keys: slack:C0DEPLOYS:1712345650.000200" in text
    assert source.calls == [], "a delegation is not an ordinary event"
    await source.poll_once()
    assert len(source.host.delegated) == 1

    # Another emoji starts from its current reactions; an empty one stops searching.
    source.store.save_settings(Settings(slack_enabled=True, slack_delegate_emoji=":Eyes:"))
    source.mcp.results["hasmy::eyes:"] = REACTED
    await source.poll_once()
    assert len(source.host.delegated) == 1
    source.store.save_settings(Settings(slack_enabled=True, slack_delegate_emoji=""))
    source.mcp.calls.clear()
    await source.poll_once()
    assert all("hasmy" not in args["query"] for _, args in source.mcp.calls)


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


async def test_more_messages_than_one_poll_reads_are_not_lost(source):
    await source.poll_once()
    stamps = [f"{1712340000 + i}.000100" for i in range(300)]

    async def call(name, arguments):
        if arguments.get("query") != "to:me":
            return ""
        after = float(arguments["after"])
        page = [stamp for stamp in stamps if float(stamp) > after][:20]
        return "\n".join(f"DM ts: {stamp}" for stamp in page)

    source.store.set("slack_cursor", "1712339000")
    source.mcp.call = call
    for _ in range(3):
        await source.poll_once()
    delivered = {key for _, keys, *rest in source.calls for key in keys}
    assert delivered == {f"slack:{stamp}" for stamp in stamps}, "all 300, over several polls"


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
    [(_name, keys, text, _display)] = source.calls
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


async def test_a_followed_dm_gets_only_its_own_entries_from_a_search_page(source):
    await source.poll_once()
    source.host.subscriptions = [{"thread": "slack:D0ALICE01", "since": 1712345600.0}]
    source.mcp.reads["D0ALICE01"] = ""
    source.mcp.results["to:me"] = (
        "DM <#D0ALICE01> ts: 1712345700.000400\nText: from Alice\n"
        "Permalink: https://acme.slack.com/archives/D0ALICE01/p1712345700000400\n"
        "DM <#D0BOB0001> ts: 1712345710.000500\nText: private from Bob\n"
        "Permalink: https://acme.slack.com/archives/D0BOB0001/p1712345710000500"
    )
    await source.poll_once()
    [(thread, keys, text)] = source.host.thread_calls
    assert thread == "slack:D0ALICE01" and keys == ["slack:1712345700.000400"]
    assert "from Alice" in text and "Bob" not in text
    [(_name, routed, _text, _display)] = source.calls
    assert routed == ["slack:1712345710.000500"]


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
    assert source.store.get(f"slackfail:{THREAD}") == "5", "read 5 times, then backs off"
    assert source.store.get(f"slackskip:{THREAD}") == "3", "passed over since, not failed"


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


def test_the_newest_message_is_quoted_whole_not_cut_mid_sentence():
    """Bug bash #13: the thread quote kept the listing's last 400 characters, starting mid-word
    of an older message and cutting a long one without saying so."""
    listed = "\n\n".join(
        f"Channel: <#C1> (x) | From: <@U{n}> ({who}) | ts: 17000000{n}.0\nText: {text}\n"
        f"Permalink: https://acme.slack.com/archives/C1/p17000000{n}0"
        for n, (who, text) in enumerate(
            [("Bob", "older " * 80), ("Alice", "Ship it &lt;today&gt; &amp; tell\n  me.")]
        )
    )
    assert newest_message(listed, last=True) == "Alice: Ship it <today> & tell me."
    assert newest_message(listed, last=False).startswith("Bob: older older")
    long = newest_message(listed.replace("Ship it", "word " * 200), last=True)
    assert len(long) == 400 and long.endswith("\u2026") and long.startswith("Alice: word")
    assert newest_message("plain " * 100, last=True) == "", "not a message: no quote"


def entry(channel, user, name, ts, text, root=""):
    """One message as Slack's MCP server lists it."""
    thread = f" | thread_ts: {root}" if root else ""
    link = f"https://acme.slack.com/archives/{channel}/p{ts.replace('.', '')}"
    link += f"?thread_ts={root}&cid={channel}" if root else ""
    return (
        f"Channel: <#{channel}> | From: <@{user}> ({name}) | ts: {ts}{thread}\n"
        f"Text: {text}\nPermalink: {link}"
    )


async def test_a_read_error_is_not_quoted_as_the_latest_message(source):
    """Bug bash: "Thread not found." showed on a card as Bob's message."""
    source.mcp.reads["C0DEPLOYS"] = "Thread not found."
    found = await source.latest_message("C0DEPLOYS", "1712345678.000100")
    assert "quote" not in found and "Thread not found." in found["error"]
    assert await source.latest("C0DEPLOYS", "1712345678.000100") == ""
    place = await source.reply_place("C0DEPLOYS", "1712345678.000100")
    assert place["thread_ts"] == "1712345678.000100" and "Thread not found." in place["error"]


async def test_slack_news_reads_by_name_and_the_names_are_kept(source):
    await source.poll_once()
    source.mcp.results["to:me"] = (
        "Channel: <#C0DEPLOYS> (deploys) | From: <@U0BOB001> (Bob Li) | ts: 1712345678.000100\n"
        "Text: can you check the rollout? &lt;3\n"
        "Permalink: https://acme.slack.com/archives/C0DEPLOYS/p1712345678000100\n\n"
        "Channel: <#D0ALICE01> (DM with Alice) | From: <@U0ALICE1> (Alice Chen) | ts: "
        "1712345690.000200 | thread_ts: 1712345600.000100\nText: lunch?\n"
        "Permalink: https://acme.slack.com/archives/D0ALICE01/p1712345690000200"
        "?thread_ts=1712345600.000100&cid=D0ALICE01"
    )
    await source.poll_once()
    [(_name, keys, text, display)] = source.calls
    assert len(keys) == 2 and "| ts: 1712345678.000100" in text, "the Router keeps the ids"
    assert display == (
        "Bob Li in #deploys: can you check the rollout? <3\n"
        "https://acme.slack.com/archives/C0DEPLOYS/p1712345678000100\n\n"
        "Alice Chen in a DM, in a thread: lunch?\n"
        "https://acme.slack.com/archives/D0ALICE01/p1712345690000200"
        "?thread_ts=1712345600.000100&cid=D0ALICE01"
    )
    names = {
        "C0DEPLOYS": "deploys",
        "U0BOB001": "Bob Li",
        "D0ALICE01": "DM with Alice",
        "U0ALICE1": "Alice Chen",
    }
    assert source.display_names() == names
    later = SlackSource(source.store, source.host, tokens=FakeTokens(), mcp=FakeSlackMcp())
    assert later.display_names() == names, "kept for the page, across restarts"


async def test_the_owners_own_mention_is_shown_by_name(tmp_path):
    """Bug bash 3 (U14): "<@U0OWNER1>" in a Slack message showed as the raw id: Slack lists
    the owner's own id without a name."""
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    await runtime.slack.discover()
    assert runtime.snapshot()["slack_names"]["U0OWNER1"] == "you"
    runtime.slack.learn_names("From: <@U0OWNER1> (Evan Li) | Text: hi")
    assert runtime.snapshot()["slack_names"]["U0OWNER1"] == "Evan Li", "its name, once read"


async def test_a_dm_message_in_no_thread_is_answered_in_the_dm(source):
    """Bug bash: a reply to a top-level DM was drafted "in a thread" under it."""
    asked = entry("D0ALICE01", "U0ALICE1", "Alice Chen", "1712345600.000100", "approve it?")
    source.mcp.reads["D0ALICE01"] = asked
    place = await source.reply_place("D0ALICE01", "1712345600.000100")
    assert place["thread_ts"] == "" and place["quote"] == "Alice Chen: approve it?"
    assert place["ts"] == "1712345600.000100" and place["user"] == "U0ALICE1"
    asked_for = await source.reply_place("D0ALICE01", "1712345600.000100", in_thread=True)
    assert asked_for["thread_ts"] == "1712345600.000100", "a thread when the session asks"
    reply = entry("D0ALICE01", "U0OWNER1", "Owner", "1712345650.000200", "on it",
                  root="1712345600.000100")  # fmt: skip
    source.mcp.reads["D0ALICE01"] = f"{asked}\n\n{reply}"
    place = await source.reply_place("D0ALICE01", "1712345600.000100")
    assert place["thread_ts"] == "1712345600.000100", "a DM thread stays its thread"
    assert place["quote"] == "Owner: on it"
    source.mcp.reads["C0DEPLOYS"] = reply.replace("D0ALICE01", "C0DEPLOYS")
    place = await source.reply_place("C0DEPLOYS", "1712345650.000200")
    assert place["thread_ts"] == "1712345600.000100", "a reply goes into its thread"


async def test_a_reply_is_not_sent_into_a_conversation_that_moved_on(tmp_path):
    """Bug bash: Alice asked for an approval and "Approved" was drafted; she then wrote "nvm, DO
    NOT approve"; the session could not withdraw its draft, both cards stayed, and Send posted
    the approval."""
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    await runtime.slack.discover()
    slack = harness.store.open_sessions("slack")[0]["id"]
    dm = "D0ALICE01"
    harness.mcp.reads[dm] = entry(dm, "U0ALICE1", "Alice Chen", "1712345600.000100", "approve?")

    async def draft(text):
        result = await call_tool(runtime, "draft_slack_reply", slack, channel_id=dm,
                                 thread_ts="1712345600.000100", text=text)  # fmt: skip
        return harness.store.drafts()[-1], result.text_result_for_llm

    def sends():
        return [args for tool, args in harness.mcp.calls if tool == "slack_send_message"]

    first, said = await draft("Approved \u2705")
    assert first["target"]["thread_ts"] == "" and "into the DM itself" in said
    assert first["target"]["latest"] == "Alice Chen: approve?"
    nvm = entry(dm, "U0ALICE1", "Alice Chen", "1712345700.000100", "nvm \u2014 DO NOT approve")
    harness.mcp.reads[dm] = nvm
    with pytest.raises(ValueError, match="Alice Chen wrote since this draft"):
        await runtime.drafts.resolve(first["id"], DraftDecision(action="send"))
    card = harness.store.draft(first["id"])
    assert card["status"] == "pending" and sends() == []
    assert card["target"]["latest"] == "Alice Chen: nvm \u2014 DO NOT approve"
    assert card["target"]["moved_on"]["who"] == "Alice Chen"
    assert card["outcome"].endswith("Not sent: send anyway?")
    # The newer draft replaces the older one; a draft can be withdrawn.
    second, said = await draft("OK, holding off.")
    assert f"replaces your earlier draft {first['id']}" in said
    replaced = harness.store.draft(first["id"])
    assert replaced["status"] == "rejected" and replaced["outcome"].startswith("Replaced by")
    assert [d["id"] for d in harness.store.drafts()] == [second["id"]]
    assert not harness.store.all("SELECT id FROM feedback"), "not the owner's rejection"
    not_mine = await call_tool(runtime, "withdraw_draft", harness.router_id,
                               draft_id=second["id"], reason="x")  # fmt: skip
    assert not_mine.result_type == "failure"
    withdrawn = await call_tool(runtime, "withdraw_draft", slack, draft_id=second["id"],
                                reason="Alice sorted it out")  # fmt: skip
    assert "withdrawn" in withdrawn.text_result_for_llm
    gone = harness.store.draft(second["id"])
    assert gone["status"] == "rejected" and gone["outcome"] == "Withdrawn: Alice sorted it out"
    # Sent again once the card shows what was written since; or at once with send_anyway.
    third, _ = await draft("Holding off, then.")
    harness.mcp.reads[dm] = entry(dm, "U0ALICE1", "Alice Chen", "1712345800.000100", "hello?")
    with pytest.raises(ValueError, match="wrote since"):
        await runtime.drafts.resolve(third["id"], DraftDecision(action="send"))
    sent = await runtime.drafts.resolve(third["id"], DraftDecision(action="send"))
    assert sent["status"] == "sent" and sends()[-1]["message"] == "Holding off, then."
    # An edit sent as it was held stays the card's text: Send anyway sends the edit.
    edited, _ = await draft("Holding.")
    harness.mcp.reads[dm] = entry(dm, "U0ALICE1", "Alice Chen", "1712345850.000100", "ok?")
    with pytest.raises(ValueError, match="wrote since"):
        await runtime.drafts.resolve(
            edited["id"], DraftDecision(action="send", text="Held, sorry.")
        )
    assert harness.store.draft(edited["id"])["final_text"] == "Held, sorry."
    anyway_edit = DraftDecision(action="send", text="Held, sorry.", content={"send_anyway": True})
    await runtime.drafts.resolve(edited["id"], anyway_edit)
    assert sends()[-1]["message"] == "Held, sorry."
    fourth, _ = await draft("Done.")
    harness.mcp.reads[dm] = entry(dm, "U0ALICE1", "Alice Chen", "1712345900.000100", "and?")
    anyway = DraftDecision(action="send", content={"send_anyway": True})
    assert (await runtime.drafts.resolve(fourth["id"], anyway))["status"] == "sent"
    # What the owner wrote there themself is not someone moving the conversation on.
    fifth, _ = await draft("One more thing.")
    harness.mcp.reads[dm] = entry(dm, "U0OWNER1", "Owner", "1712346000.000100", "brb")
    assert (await runtime.drafts.resolve(fifth["id"], DraftDecision(action="send")))[
        "status"
    ] == "sent"
