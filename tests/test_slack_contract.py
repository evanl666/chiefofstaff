"""What Chief of Staff reads from the owner's real Slack, through Slack's MCP server: a search,
a thread and a channel's history, each read message by message (who, when, what).

Runs when CHIEFOFSTAFF_TEST_SLACK=1 with a Slack user token in CHIEFOFSTAFF_SLACK_USER_TOKEN
(never printed). Read-only: it calls only Slack's read tools (nothing is sent, reacted to or
marked read). Run it when Slack's MCP server changes how it answers.
"""

import os

import pytest

from chiefofstaff.sources.slack import (
    SEARCH_TOOL,
    SlackMcp,
    SlackTokens,
    known,
    parse,
    sectioned,
)

pytestmark = [
    pytest.mark.slack,
    pytest.mark.skipif(
        os.environ.get("CHIEFOFSTAFF_TEST_SLACK") != "1" or not os.environ.get(SlackTokens.ENV),
        reason="set CHIEFOFSTAFF_TEST_SLACK=1 and CHIEFOFSTAFF_SLACK_USER_TOKEN",
    ),
]
READS = {SEARCH_TOOL, "slack_read_thread", "slack_read_channel"}


class ReadOnly(SlackMcp):
    async def call(self, name: str, arguments: dict) -> str:
        assert name in READS, f"{name} is not one of the reads this test may make"
        return await super().call(name, arguments)


def read_whole(listed: str) -> list[dict]:
    """Its messages, each with who, when and what; none of it made up by its text."""
    assert known(listed), "Slack answered in a form Chief of Staff does not read"
    found, forged = parse(listed)
    assert not forged
    for entry in found:
        assert entry["ts"] and entry["user"], entry["head"]
        assert "@" not in entry["name"], "no email address in a name"
    return found


async def test_a_search_a_thread_and_a_channel_are_read_message_by_message():
    slack = ReadOnly(lambda: os.environ[SlackTokens.ENV])
    searched = await slack.call(
        SEARCH_TOOL,
        {
            "query": "is:thread",
            "sort": "timestamp",
            "sort_dir": "desc",
            "include_context": False,
            "content_types": "messages",
            "limit": 20,
        },
    )
    found = read_whole(searched)
    assert found, "the owner's Slack has no thread to read"
    assert sectioned(searched) is not None, "Slack's current (JSON) format"
    assert any(entry["text"] for entry in found)
    entry = found[0]
    root = entry["thread_ts"] or entry["ts"]
    thread = read_whole(
        await slack.call(
            "slack_read_thread", {"channel_id": entry["channel"], "message_ts": root, "limit": 50}
        )
    )
    assert thread[0]["ts"] == root and len(thread) >= 2, "its first message and a reply"
    assert all(reply["thread_ts"] == root for reply in thread[1:])
    assert any(message["ts"] == entry["ts"] for message in thread)
    history = read_whole(
        await slack.call("slack_read_channel", {"channel_id": entry["channel"], "limit": 5})
    )
    assert history and all(message["channel"] == entry["channel"] for message in history)
    stamps = [float(message["ts"]) for message in history]
    assert stamps == sorted(stamps, reverse=True), "newest first"
