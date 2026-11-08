"""Thread keys name a conversation that sessions can follow.

slack:<channel>:<thread_ts>   a Slack thread (a top-level message is the root of its own thread)
slack:<D…>                    a Slack DM conversation
github:<owner/repo>#<number>  a pull request
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

SLACK_THREAD = re.compile(r"slack:([CDG][A-Z0-9]{2,}):(\d{10}\.\d{6})")
SLACK_CONVERSATION = re.compile(r"slack:(D[A-Z0-9]{2,})")
GITHUB_PR = re.compile(r"github:([a-z0-9_.-]+/[a-z0-9_.-]+)#(\d+)")
PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")
# A link in a report: up to whitespace, brackets, quotes, markup or punctuation (Western or CJK),
# as the page reads them (static/text.js URL_CHARS).
LINK = re.compile(
    "https?://[^\\s<>\\[\\]{}`'\"*|\uff0c\u3002\u3001\uff1b\uff1a\uff01\uff1f\uff08\uff09\u300c\u300d\u300e\u300f\u3010\u3011\u201c\u201d\u2018\u2019\u300a\u300b\u3008\u3009\u2026]+"
)
LINK_TRAIL = re.compile(r"(?:[.,;:!?'\"\]]|&quot;|&#39;)$")
FENCE = re.compile(r"```[\s\S]*?(?:```|$)")


CODE_SPAN = re.compile(r"`[^`\n]*`")
# A link cut short for display ("https://docs.google.com/…"): no link at all.
CUT = re.compile("(?:\u2026|\\.\\.\\.)")


def links(text: str) -> list[str]:
    """The links a text gives (not those in its code, nor ones cut short with "…"), each once,
    in order."""
    found = []
    plain = CODE_SPAN.sub(" ", FENCE.sub(" ", text or ""))
    for match in LINK.finditer(plain):
        if CUT.match(plain, match.end()):
            continue
        url = match.group(0).replace("&amp;", "&")
        while True:
            if mark := LINK_TRAIL.search(url):
                url = url[: mark.start()]
            elif url.endswith(")") and url.count(")") > url.count("("):
                url = url[:-1]
            else:
                break
        if re.match(r"https?://[^/\s]+\.[^/\s]", url) and url not in found:
            found.append(url)
    return found


# [the plan](https://…): what the report calls a link.
NAMED_LINK = re.compile(r"\[([^\]\n]{1,120})\]\((https?://[^\s)]+)\)")
# A line saying it made what it links: created, wrote, opened, published, started…
MADE = re.compile(
    r"\b(?:creat|wr[io]te|written|made|mak|open|publish|start|launch|draft|add|upload|generat|"
    r"produc|deliver|built|build|kick|submit|shar|new)\w*|"
    "\u521b\u5efa|\u65b0\u5efa|\u5199\u4e86|\u5199\u597d|\u4ea7\u51fa|\u751f\u6210|"
    "\u542f\u52a8|\u53d1\u5e03|\u63d0\u4ea4|\u8d77\u8349",
    re.IGNORECASE,
)


def named_links(text: str) -> dict[str, str]:
    """The text a report gives its links ([text](url)), by link."""
    plain = CODE_SPAN.sub(" ", FENCE.sub(" ", text or ""))
    return {url: " ".join(name.split()) for name, url in NAMED_LINK.findall(plain)}


def made_links(text: str) -> list[str]:
    """Links a report says it made: on a line that says so ("Created [the plan](…)", "the run:
    …" after "Started a run"). Not those it only mentions or was given."""
    found: list[str] = []
    for line in FENCE.sub(" ", text or "").splitlines():
        if MADE.search(CODE_SPAN.sub(" ", NAMED_LINK.sub(r"\1", line))):
            found += [url for url in links(line) if url not in found]
    return found


PERMALINK = re.compile(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})(?:\?thread_ts=(\d{10}\.\d{6}))?")


GOOGLE_DOC = re.compile(r"^https://docs\.google\.com/.*?/d/(?:e/)?([\w-]{6,})")
SHARING = re.compile(r"^(usp|utm_\w+|ref|ref_src|fbclid|gclid|si|authuser|ouid|rtpof|sd)$", re.I)


def link_key(url: str) -> str:
    """One key for one thing linked in different ways (as the UI's linkKey): a Google document
    by its id; else the address without what only says how it was shared ("?usp=sharing") or a
    place in the page ("#L10"). What tells runs apart stays ("?dag_id=a", "#job/2/run/3")."""
    if match := GOOGLE_DOC.match(url):
        return f"gdoc:{match.group(1)}"
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = sorted(
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not SHARING.match(name)
    )
    host = (parts.hostname or "").removeprefix("www.")
    route = f"#{parts.fragment}" if "/" in parts.fragment else ""
    search = f"?{urlencode(query)}" if query else ""
    return f"{host}{parts.path.rstrip('/')}{search}{route}"


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


def slack_entries(text: str, stamps: set[str]) -> str:
    """Only the entries of a Slack search result whose permalink is one of these messages
    ("1712345700.000400"): each entry ends with its permalink line."""
    entries: list[list[str]] = [[]]
    for line in text.splitlines():
        entries[-1].append(line)
        if PERMALINK.search(line):
            entries.append([])
    kept = []
    for entry in entries:
        found = {
            f"{seconds}.{micros}" for _, seconds, micros, _ in PERMALINK.findall("\n".join(entry))
        }
        if found & stamps:
            kept.append("\n".join(entry).strip())
    return "\n".join(kept)


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
