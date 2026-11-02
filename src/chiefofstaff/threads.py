"""Thread keys name a conversation that sessions can follow.

slack:<channel>:<thread_ts>   a Slack thread (a top-level message is the root of its own thread)
slack:<D…>                    a Slack DM conversation
github:<owner/repo>#<number>  a pull request
"""

import re

SLACK_THREAD = re.compile(r"slack:([CDG][A-Z0-9]{2,}):(\d{10}\.\d{6})")
SLACK_CONVERSATION = re.compile(r"slack:(D[A-Z0-9]{2,})")
GITHUB_PR = re.compile(r"github:([a-z0-9_.-]+/[a-z0-9_.-]+)#(\d+)")
PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")
PERMALINK = re.compile(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})(?:\?thread_ts=(\d{10}\.\d{6}))?")


def normalize(key: str) -> str:
    key = key.strip()
    if key.startswith("github:"):
        key = key.lower()
    if any(p.fullmatch(key) for p in (SLACK_THREAD, SLACK_CONVERSATION, GITHUB_PR)):
        return key
    raise ValueError(
        f"Unknown thread key {key!r}. Use slack:<channel_id>:<thread_ts> for a Slack thread, "
        "slack:<D…> for a DM conversation, or github:<owner/repo>#<number> for a PR."
    )


def label(key: str) -> str:
    if match := SLACK_THREAD.fullmatch(key):
        return f"Slack thread in {match.group(1)}"
    if match := SLACK_CONVERSATION.fullmatch(key):
        return f"Slack DM {match.group(1)}"
    if match := GITHUB_PR.fullmatch(key):
        return f"{match.group(1)}#{match.group(2)}"
    return key


def pull_requests(text: str) -> list[str]:
    return list(dict.fromkeys(f"github:{repo.lower()}#{n}" for repo, n in PR_URL.findall(text)))


def slack_messages(text: str) -> dict[str, list[str]]:
    """ts -> the thread keys it belongs to, for every Slack permalink in a tool result."""
    found: dict[str, list[str]] = {}
    for channel, seconds, micros, root in PERMALINK.findall(text):
        stamp = f"{seconds}.{micros}"
        keys = [f"slack:{channel}:{root or stamp}"]
        if channel.startswith("D"):
            keys.append(f"slack:{channel}")
        found[stamp] = keys
    return found


def slack_follow_key(channel: str, thread_ts: str, sent: str) -> str | None:
    """What to follow after replying: the thread, the DM, or the thread under the reply."""
    links = slack_messages(sent)
    stamp, keys = next(iter(links.items()), ("", []))
    channel = keys[0].split(":")[1] if keys else channel
    if thread_ts:
        return f"slack:{channel}:{thread_ts}"
    if channel.startswith("D"):
        return f"slack:{channel}"
    return f"slack:{channel}:{stamp}" if stamp else None
