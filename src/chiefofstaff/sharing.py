"""Share links, the pure part: a finished card as a snapshot, what looks private in it, and the
format a link carries (format 1). shares.py stores, publishes and opens links with it.

    link     https://evanl666.github.io/chiefofstaff/s/#1.<gist id>.<key>.<expiry hint>
    gist     one file, share.txt: base64url(0x01 · nonce[12] · AES-256-GCM ciphertext · tag[16])
    AAD      "chiefofstaff-share:1:<GitHub user id of the gist's owner>"
    payload  UTF-8 JSON, at most 64 KiB: {v, kind, created_at, expires_at, by, app, card}

The key is 32 random bytes, one per link; it lives only in the link's fragment, which browsers
never send. The viewer page (site/s/sharecrypto.js) is the same format in WebCrypto. The expiry
is the payload's own: past it, nothing opens it, whatever GitHub still holds.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

VERSION = 1
VIEWER = "https://evanl666.github.io/chiefofstaff/s/"
MAX_PLAIN = 64 * 1024
NONCE_BYTES, TAG_BYTES, KEY_BYTES = 12, 16, 32
# base64url of the largest gist: version, nonce, payload and tag.
MAX_BLOB = -(-((1 + NONCE_BYTES + MAX_PLAIN + TAG_BYTES) * 4) // 3)
FRAGMENT = re.compile(r"(\d+)\.([0-9a-f]{20,40})\.([A-Za-z0-9_-]{43})(?:\.([0-9a-z]{1,9}))?")
# A share link written out whole (Home's message box opens one pasted alone).
LINK = re.compile(
    re.escape(VIEWER) + r"#\d+\.[0-9a-f]{20,40}\.[A-Za-z0-9_-]{43}(?:\.[0-9a-z]{1,9})?"
)
B64U = re.compile(r"[A-Za-z0-9_-]*")
EXPIRY_DAYS = (1, 7, 30)
TITLE_CHARS = 300
RESULT_CHARS = 40_000
CUT_NOTE = "… (cut: a link holds at most 64 KB)"
REMOVED = "[removed]"
STATUS_WORDS = {"review": "Ready for review", "done": "Done"}
VERDICTS = frozenset({"verified", "partial", "unverified", "failed"})
CRITERION_STATES = frozenset({"verified", "claimed", "contradicted", "not_met", "not_checked"})
OUTCOMES = frozenset({"passed", "failed", "running", "none"})
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


class DamagedError(ValueError):
    """Changed, cut, or not this owner's: nothing of it is shown."""


class NewerError(ValueError):
    """A later format than this code reads."""


# The format.


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    if not isinstance(text, str) or not B64U.fullmatch(text) or len(text) % 4 == 1:
        raise DamagedError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def aad(owner_id: int) -> bytes:
    return f"chiefofstaff-share:{VERSION}:{int(owner_id)}".encode()


def encode(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def seal(payload: dict, owner_id: int) -> tuple[str, str]:
    """(the gist's text, the key for the link): a fresh key and nonce, used once."""
    return seal_with(payload, owner_id, os.urandom(KEY_BYTES), os.urandom(NONCE_BYTES))


def seal_with(payload: dict, owner_id: int, key: bytes, nonce: bytes) -> tuple[str, str]:
    """seal with a given key and nonce: only for the format's known-answer test."""
    plain = encode(payload)
    if len(plain) > MAX_PLAIN:
        raise ValueError(f"The card is {len(plain)} bytes; a link holds at most {MAX_PLAIN}.")
    if len(key) != KEY_BYTES or len(nonce) != NONCE_BYTES:
        raise ValueError("A key is 32 bytes and a nonce 12.")
    # Loaded when a link is made or opened, not with Chief of Staff.
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    sealed = AESGCM(key).encrypt(nonce, plain, aad(owner_id))
    return b64u(bytes([VERSION]) + nonce + sealed), b64u(key)


def open_payload(blob: str, key: str, owner_id: int) -> dict:
    """The payload, or DamagedError (anything changed, cut, or another owner's) / NewerError."""
    if not isinstance(blob, str) or len(blob) > MAX_BLOB:
        raise DamagedError("blob size")
    data = unb64u(blob.strip())
    if len(data) < 1 + NONCE_BYTES + TAG_BYTES:
        raise DamagedError("blob too short")
    if data[0] != VERSION:
        raise (NewerError if data[0] > VERSION else DamagedError)(f"format {data[0]}")
    raw = unb64u(key)
    if len(raw) != KEY_BYTES:
        raise DamagedError("key length")
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        plain = AESGCM(raw).decrypt(
            data[1 : 1 + NONCE_BYTES], data[1 + NONCE_BYTES :], aad(owner_id)
        )
    except InvalidTag as exc:
        raise DamagedError("authentication failed") from exc
    if len(plain) > MAX_PLAIN:
        raise DamagedError("payload size")
    try:
        payload = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DamagedError("not JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != VERSION
        or payload.get("kind") != "card"
    ):
        raise DamagedError("not a card")
    return payload


def base36(number: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    text = ""
    while number:
        number, rest = divmod(number, 36)
        text = digits[rest] + text
    return text or "0"


def make_link(gist_id: str, key: str, expires_at: str, base: str = VIEWER) -> str:
    """The link: the format, the gist, the key, and the expiry as a hint (not secret, not
    trusted: it only words a gone link as expired or revoked)."""
    hint = int(parse_time(expires_at).timestamp())
    return f"{base}#{VERSION}.{gist_id}.{key}.{base36(hint)}"


def parse_link(text: str) -> dict | None:
    """A link as pasted (the whole address, or its fragment): {gist_id, key, expires_hint},
    {"newer": True} for a later format, or None."""
    value = (text or "").strip()
    fragment = value.split("#", 1)[1] if "#" in value else value
    found = FRAGMENT.fullmatch(fragment)
    if not found:
        return None
    if int(found[1]) != VERSION:
        return {"newer": True} if int(found[1]) > VERSION else None
    hint = ""
    if found[4]:
        hint = datetime.fromtimestamp(int(found[4], 36), UTC).isoformat()
    return {"gist_id": found[2], "key": found[3], "expires_hint": hint}


def link_alone(text: str) -> str:
    """The share link a message is, when it is that and nothing else ("" otherwise)."""
    value = (text or "").strip()
    return value if LINK.fullmatch(value) else ""


def parse_time(value: object) -> datetime:
    """An ISO time with its zone; ValueError for none, a naive one or anything else."""
    if not isinstance(value, str):
        raise ValueError("no time")
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        raise ValueError("a time without its zone")
    return when


def zulu(value: str) -> str:
    """An ISO time as every browser reads it: to the second, in UTC ("" for none)."""
    try:
        return parse_time(value).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ""


def expired(payload: dict, now: datetime | None = None) -> bool:
    """Past its own expiry, or without one that parses (it fails closed)."""
    try:
        until = parse_time(payload.get("expires_at"))
    except ValueError:
        return True
    return (now or datetime.now(UTC)) >= until


# What a card holds once shared, and nothing else: checked field by field (type and length)
# before anything of a payload is shown or kept. Unknown fields are left out.


def _text(value: object, most: int, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > most:
        raise DamagedError(f"{what}: not a text of at most {most} characters")
    return value


def _count(value: object, what: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 10**9:
        raise DamagedError(f"{what}: not a count")
    return value


def _one_of(value: object, allowed: frozenset[str], what: str) -> str:
    value = "" if value is None else value
    if value != "" and value not in allowed:
        raise DamagedError(f"{what}: {value!r} is not one of {sorted(allowed)}")
    return str(value)


def _list(value: object, most: int, what: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > most:
        raise DamagedError(f"{what}: not a list of at most {most}")
    return value


def _object(value: object, what: str) -> dict:
    if not isinstance(value, dict):
        raise DamagedError(f"{what}: not an object")
    return value


def clean_card(card: object) -> dict:
    """The card's known fields, each checked; DamagedError for any that is not as it must be."""
    card = _object(card, "card")
    status = _one_of(card.get("status"), frozenset(STATUS_WORDS), "status")
    criteria = [
        {
            "text": _text(item.get("text"), 2000, "criterion"),
            "state": _one_of(item.get("state"), CRITERION_STATES, "criterion state"),
            "check": _text(item.get("check"), 2000, "criterion check"),
        }
        for item in (_object(x, "criterion") for x in _list(card.get("criteria"), 50, "criteria"))
    ]
    checks = [
        {
            "label": _one_of(item.get("label"), frozenset({"tests", "checks", "build"}), "check"),
            "command": _text(item.get("command"), 2000, "check command"),
            "outcome": _one_of(item.get("outcome"), OUTCOMES, "check outcome"),
            "failed_before": bool(item.get("failed_before")),
            "runs": _count(item.get("runs"), "runs"),
        }
        for item in (_object(x, "check") for x in _list(card.get("checks"), 3, "checks"))
    ]
    prs = []
    for item in _list(card.get("prs"), 10, "prs"):
        item = _object(item, "pr")
        url = _text(item.get("url"), 300, "pr url")
        if not PR_URL.fullmatch(url):
            raise DamagedError("pr url: not a PR on github.com")
        prs.append(
            {
                "label": _text(item.get("label"), 300, "pr label"),
                "url": url,
                "title": _text(item.get("title"), 300, "pr title"),
                "state": _one_of(item.get("state"), frozenset({"OPEN", "CLOSED", "MERGED"}), "pr"),
                "checks": _one_of(
                    item.get("checks"), frozenset({"passing", "failing", "pending", "none"}), "ci"
                ),
                "review": _one_of(
                    item.get("review"),
                    frozenset({"APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}),
                    "review",
                ),
            }
        )
    changes = _object(card.get("changes") or {}, "changes")
    at = _text(card.get("at"), 40, "at")
    if at and not re.fullmatch(r"[0-9a-f]{4,40}", at):
        raise DamagedError("at: not a commit")
    return {
        "title": _text(card.get("title"), TITLE_CHARS, "title") or "A shared card",
        "status": status,
        "status_words": _text(card.get("status_words"), 60, "status words"),
        "finished_at": _text(card.get("finished_at"), 40, "finished_at"),
        "result": _text(card.get("result"), RESULT_CHARS + len(CUT_NOTE) + 2, "result"),
        "verdict": _one_of(card.get("verdict"), VERDICTS, "verdict"),
        "criteria": criteria,
        "checks": checks,
        "changes": {
            key: _count(changes.get(key), key)
            for key in ("files", "insertions", "deletions", "commits")
        },
        "at": at,
        "prs": prs,
        "flags": [_text(flag, 2000, "flag") for flag in _list(card.get("flags"), 30, "flags")],
    }


def clean_payload(payload: dict) -> dict:
    """A payload's known fields, checked (its card too); DamagedError otherwise."""
    by = _object(payload.get("by") or {}, "by")
    for key in ("created_at", "expires_at"):
        try:
            parse_time(payload.get(key))
        except ValueError as exc:
            raise DamagedError(f"{key}: not a time") from exc
    return {
        "v": VERSION,
        "kind": "card",
        "created_at": payload["created_at"],
        "expires_at": payload["expires_at"],
        "by": {"login": _text(by.get("login"), 100, "login"), "id": _count(by.get("id"), "id")},
        "app": _text(payload.get("app"), 40, "app"),
        "card": clean_card(payload.get("card")),
    }


# The snapshot: a finished task's card, by allow-list (never its goal, conversation, paths,
# branches, changes, credits or anything else of the task).


def finished_words(view: dict) -> str:
    return STATUS_WORDS.get(view.get("status", ""), "")


def outcome(item: dict) -> str:
    if item.get("no_tests"):
        return "none"
    if item.get("passed") is None:
        return "running"
    return "passed" if item["passed"] else "failed"


def whole(text: str, at: int, environment: Sequence[str] = ()) -> int:
    """Where to cut `text` at `at` or before so as not to cut through a private value, as the
    scan finds them in the whole text before it is cut (U9): the start of the one `at` is
    inside, else `at`."""
    for hit in hits_in(text, environment):
        if hit["start"] < at < hit["end"]:
            return hit["start"]
    return at


def cut(text: str, most: int, environment: Sequence[str] = ()) -> str:
    """At most `most` characters, never cut through a private value."""
    if len(text) <= most:
        return text
    return text[: whole(text, most, environment)].rstrip()


def cut_result(
    text: str, most: int = RESULT_CHARS, environment: Sequence[str] = ()
) -> tuple[str, bool]:
    """At most `most` characters, cut at a paragraph (else a line), said at its end; never
    through a private value (a key's block spans lines)."""
    text = (text or "").strip()
    if len(text) <= most:
        return text, False
    head = text[:most]
    at = head.rfind("\n\n")
    if at < most // 2:
        at = head.rfind("\n")
    if at < most // 2:
        at = most
    at = whole(text, at, environment)
    return f"{head[:at].rstrip()}\n\n{CUT_NOTE}", True


def snapshot(view: dict, finished_at: str = "", environment: Sequence[str] = ()) -> dict:
    """The card of a finished task's view (TaskActions.view), as it would be shared: only the
    fields of share-links' allow-list. Paths are not shortened yet (see shorten). What it cuts
    short it never cuts through a private value (`environment`: values the scan looks for)."""
    found = view.get("receipts") or {}
    result, _ = cut_result(
        view.get("result") or view.get("progress") or "", RESULT_CHARS, environment
    )
    checks = []
    for label, key in (("tests", "tests"), ("checks", "checks"), ("build", "build")):
        item = found.get(key)
        if not item or (key == "tests" and item.get("own") is False):
            continue  # Tests it ran outside its own checkout are not this card's evidence.
        checks.append(
            {
                "label": label,
                "command": str(item.get("command") or ""),
                "outcome": outcome(item),
                "failed_before": bool(item.get("failed_before")),
                "runs": int(item.get("runs") or 0),
            }
        )
    changed = found.get("changed") or {}
    version = found.get("version") or []
    prs = [
        {
            "label": str(pr.get("label") or ""),
            "url": str(pr.get("url") or ""),
            "title": cut(str(pr.get("title") or ""), 200, environment),
            "state": pr.get("state") if pr.get("state") in {"OPEN", "CLOSED", "MERGED"} else "",
            "checks": pr.get("checks")
            if pr.get("checks") in {"passing", "failing", "pending", "none"}
            else "",
            "review": pr.get("review")
            if pr.get("review") in {"APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}
            else "",
        }
        for pr in found.get("prs") or []
        if PR_URL.fullmatch(str(pr.get("url") or ""))
    ]
    return {
        "title": cut(view.get("title") or "", TITLE_CHARS, environment),
        "status": view.get("status", ""),
        "status_words": finished_words(view),
        "finished_at": zulu(finished_at or view.get("ended_at") or view.get("updated_at") or ""),
        "result": result,
        "verdict": found.get("verdict") or "",
        "criteria": [
            {
                "text": str(item.get("criterion") or ""),
                "state": item.get("state") if item.get("state") in CRITERION_STATES else "",
                "check": str(item.get("command") or ""),
            }
            for item in found.get("criteria") or []
        ],
        "checks": checks,
        "changes": {
            key: int(changed.get(key) or 0)
            for key in ("files", "insertions", "deletions", "commits")
        },
        "at": str(version[0].get("commit") or "")[:7] if version else "",
        "prs": prs,
        "flags": [str(flag) for flag in found.get("flags") or []],
    }


def strings(card: dict) -> Iterator[tuple[str, str, Callable[[str], None]]]:
    """Every text a card holds: where it shows, the text, and how to set it."""

    def setter(holder: Any, key: Any) -> Callable[[str], None]:
        def put(value: str):
            holder[key] = value

        return put

    yield "Title", card.get("title") or "", setter(card, "title")
    yield "Result", card.get("result") or "", setter(card, "result")
    for item in card.get("criteria") or []:
        yield "Done when", item.get("text") or "", setter(item, "text")
        yield "Done when", item.get("check") or "", setter(item, "check")
    for item in card.get("checks") or []:
        yield "Checks", item.get("command") or "", setter(item, "command")
    for index, flag in enumerate(card.get("flags") or []):
        yield "Warnings", flag, setter(card["flags"], index)
    for pr in card.get("prs") or []:
        yield "PR", pr.get("title") or "", setter(pr, "title")


# Paths on this computer, shortened (a note in the preview, not a question): one inside a
# checkout becomes relative to it; any other under a home folder or ~ becomes …/<last part>.
LOCAL_PATH = re.compile(
    r"(?:/Users/|/home/)[^\s/]+/[^\s\"'`)\]]*|[A-Za-z]:\\Users\\[^\s\"'`)\]]+|~/[^\s\"'`)\]]*"
)


def last_part(path: str) -> str:
    parts = [part for part in re.split(r"[\\/]", path) if part]
    return parts[-1] if parts else ""


def shorten_text(text: str, roots: list[str], shortened: list[dict]) -> str:
    def note(path: str, short: str) -> str:
        if not any(item["from"] == path for item in shortened):
            shortened.append({"from": path, "to": short})
        return short

    for root in sorted({r.rstrip("/\\") for r in roots if len(r.rstrip("/\\")) > 1}, key=len)[::-1]:
        pattern = re.compile(re.escape(root) + r"(?:[/\\][^\s\"'`)\]]*)?(?![\w-]|\.\w)")

        def relative(match: re.Match, root: str = root) -> str:
            path = match[0].rstrip(".,;:!?")
            inside = path[len(root) + 1 :]
            return note(path, inside or last_part(root)) + match[0][len(path) :]

        text = pattern.sub(relative, text)

    def home(match: re.Match) -> str:
        path = match[0].rstrip(".,;:!?")
        rest = match[0][len(path) :]
        return note(path, f"…/{last_part(path)}") + rest

    return LOCAL_PATH.sub(home, text)


def shorten(card: dict, roots: list[str]) -> tuple[dict, list[dict]]:
    """The card with every path on this computer shortened, and those it shortened."""
    card = copy.deepcopy(card)
    shortened: list[dict] = []
    for _, text, put in strings(card):
        if text:
            put(shorten_text(text, roots, shortened))
    return card, shortened


# What looks private: each rule names what it found; the first rule to match a stretch of text
# names it (secrets before addresses). A hit is the whole secret, never just a marker beside it
# (a private key's whole block, an AWS key id with its secret, a link's password whatever its
# scheme, a credential's value however its name is spelled): Remove replaces exactly the hit, so
# what a hit leaves out stays in the link. The owner removes or keeps each hit before a link exists.
URL_TAIL = r"[^\s<>()\[\]{}\"'`]*"
PRIVATE_V4 = (
    r"(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168|127\.\d{1,3}|169\.254)\.\d{1,3}\.\d{1,3}"
)
INTERNAL_SUFFIX = r"(?:internal|corp|local|lan|intranet|home\.arpa|localhost|svc|cluster\.local)"
INTERNAL_HOST = rf"(?:[a-z0-9-]+\.)+{INTERNAL_SUFFIX}"
PRIVATE_V6 = r"\[?(?:f[cd][0-9a-f]{2}|fe80):[0-9a-f:]+\]?"
# Not after a letter or digit (`\b` fails after `_`: DB_PASSWORD=, AWS_SECRET_ACCESS_KEY=).
START = r"(?<![A-Za-z0-9])"
STOP = r"(?![A-Za-z0-9])"
# A line break, or one escaped in a JSON string ("private_key": "-----BEGIN …\n…").
BREAK = r"(?:\r?\n|\\r?\\n)"
KEY_BEGIN = r"-----BEGIN (?P<kind>[A-Z0-9 ]*?)PRIVATE KEY(?P<block>(?: BLOCK)?)-----"
KEY_END = r"-----END (?P=kind)PRIVATE KEY(?P=block)-----"
# A line of a key's body (base64) or of its headers (Proc-Type: …), when the END line is missing.
KEY_LINE = (
    rf"(?:{BREAK}[ \t]*)+(?:[A-Za-z0-9+/=]{{4,}}|[A-Za-z][A-Za-z-]*:[^\n\\]*)"
    r"(?=[ \t]*(?:\r?\n|\\[rn]|\"|$))"
)
# Where a text was cut short: its end, or the … (or ...) that marks a cut (envelope.clean_title,
# receipts.shown, cut_result's note). A value that runs up to one may be the start of a longer
# one cut there (U9: a title cut through a token): a known prefix and at least 4 of its value's
# characters up to a cut are flagged, however short of a whole one.
CUT = r"(?=\s*(?:\u2026|\.\.\.|\Z))"
AWS_ID = rf"{START}(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{{16}}{STOP}"
# A secret access key: 40 of base64, not a commit's 40 of hex.
AWS_KEY_BODY = (
    r"(?<![A-Za-z0-9/+=])(?![0-9A-Fa-f]{40}(?![A-Za-z0-9/+=]))[A-Za-z0-9/+=]{40}"
    r"(?![A-Za-z0-9/+=])"
)
# A name that says its value is a credential: DB_PASSWORD, "password", client_secret, x-api-key,
# SecretAccessKey, apiToken; not tokens, tokenizer, secrets (camelCase may follow the word).
CREDENTIAL_NAME = (
    r"[A-Za-z0-9_.-]{0,40}?(?:password|passwd|passphrase|pwd|secret|api[_ -]?key|access[_ -]?key"
    r"|private[_ -]?key|token|credentials?)(?:(?:[_.-]|(?-i:[A-Z]))[A-Za-z0-9_.-]{0,40})?"
)
CREDENTIAL_VALUE = (
    rf"(?:\"[^\"\n]+\"|'[^'\n]+'|[^\s\"'`]{{6,}}"
    # Cut short: a quoted one before its closing quote; a bare one only where … marks a cut
    # ("token: none" at a text's end is no value).
    rf"|\"[^\"\n\u2026]+{CUT}|'[^'\n\u2026]+{CUT}|[^\s\"'`\u2026]+(?=\s*(?:\u2026|\.\.\.)))"
)
RULES: tuple[tuple[str, re.Pattern], ...] = (
    (
        "a private key",
        re.compile(rf"{KEY_BEGIN}(?:[\s\S]{{0,20000}}?{KEY_END}|(?:{KEY_LINE})*)"),
    ),
    (
        "an AWS key and its secret",
        re.compile(
            rf"{AWS_ID}[\s\S]{{0,200}}?{AWS_KEY_BODY}|{AWS_KEY_BODY}[\s\S]{{0,200}}?{AWS_ID}"
        ),
    ),
    (
        "a link with a password",
        re.compile(
            rf"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://(?:[^\s/:@]*:[^\s/@]+@{URL_TAIL}"
            # Cut before its @: a user (no host's dots) and a password (not a port).
            rf"|[^\s/:@.]*:(?!\d+{CUT})[^\s/@\u2026]+{CUT})"
        ),
    ),
    (
        "a GitHub token",
        re.compile(
            rf"{START}(?:gh[pousr]_[A-Za-z0-9]{{36,255}}|github_pat_\w{{22,255}}"
            rf"|(?:gh[pousr]_[A-Za-z0-9]{{4,}}|github_pat_\w{{4,}}){CUT})"
        ),
    ),
    (
        "an AWS access key",
        re.compile(rf"{AWS_ID}|{START}(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{{4,15}}{CUT}"),
    ),
    (
        "a Google API key",
        re.compile(
            rf"{START}AIza(?:[0-9A-Za-z_-]{{35}}(?![0-9A-Za-z_-])|[0-9A-Za-z_-]{{4,}}{CUT})"
        ),
    ),
    (
        "a Slack token",
        re.compile(rf"{START}xox[abposre]-(?:[0-9A-Za-z-]{{10,}}|[0-9A-Za-z-]{{4,}}{CUT})"),
    ),
    (
        "a Stripe key",
        re.compile(rf"{START}(?:sk|rk)_live_(?:[0-9A-Za-z]{{16,}}|[0-9A-Za-z]{{4,}}{CUT})"),
    ),
    ("an npm token", re.compile(rf"{START}npm_(?:[A-Za-z0-9]{{36}}{STOP}|[A-Za-z0-9]{{4,}}{CUT})")),
    (
        "an Anthropic key",
        re.compile(rf"{START}sk-ant-(?:[A-Za-z0-9_-]{{32,}}|[A-Za-z0-9_-]{{4,}}{CUT})"),
    ),
    (
        "an OpenAI key",
        re.compile(rf"{START}sk-(?:proj-)?(?:[A-Za-z0-9_-]{{32,}}|[A-Za-z0-9_-]{{4,}}{CUT})"),
    ),
    # As drafts.SECRET has it.
    (
        "a JSON web token",
        re.compile(
            rf"{START}eyJ(?:[\w-]{{10,}}\.eyJ[\w-]{{10,}}\.[\w-]{{10,}}"
            rf"|[\w-]{{4,}}(?:\.[\w-]*){{0,2}}{CUT})"
        ),
    ),
    (
        "a bearer token",
        re.compile(rf"{START}Bearer\s+(?:[A-Za-z0-9._~+/-]{{20,}}=*|[A-Za-z0-9._~+/-]{{8,}}{CUT})"),
    ),
    (
        "a password or key",
        re.compile(rf"(?i){START}[\"']?{CREDENTIAL_NAME}[\"']?[ \t]*[:=][ \t]*{CREDENTIAL_VALUE}"),
    ),
    (
        "a signed link",
        re.compile(
            rf"(?i)\bhttps?://{URL_TAIL}[?&](?:X-Amz-Signature|sig|signature|token|access_token"
            rf"|code)={URL_TAIL}"
        ),
    ),
    ("a Slack link", re.compile(rf"\bhttps://[\w-]+\.slack\.com/{URL_TAIL}")),
    (
        "an internal address",
        re.compile(
            rf"(?i)\bhttps?://(?:{PRIVATE_V4}|{INTERNAL_HOST}|{PRIVATE_V6}|localhost"
            rf"|[a-z0-9-]+(?=[:/]))(?::\d+)?{URL_TAIL}"
        ),
    ),
    ("an internal address", re.compile(rf"\b{PRIVATE_V4}(?::\d+)?\b")),
    (
        "an internal address",
        re.compile(r"(?i)(?<![\w:])(?:f[cd][0-9a-f]{2}|fe80):[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){1,6}"),
    ),
    ("an internal host name", re.compile(rf"(?i)\b{INTERNAL_HOST}\b")),
    (
        "an email address",
        re.compile(
            r"\b[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
            rf"|[A-Za-z0-9.-]*{CUT})"
        ),
    ),
)
ENVIRONMENT = "a value from your environment"


def hits_in(text: str, environment: Sequence[str] = ()) -> list[dict]:
    """What looks private in a text: {label, start, end, value}, in order; overlaps go to the
    first rule (the more specific)."""
    rules = list(RULES)
    if environment:
        values = sorted({value for value in environment if value}, key=len, reverse=True)
        rules.insert(0, (ENVIRONMENT, re.compile("|".join(map(re.escape, values)))))
    taken: list[dict] = []
    for label, pattern in rules:
        for found in pattern.finditer(text):
            start = found.start()
            end = start + len(found[0].rstrip(".,;:!?"))
            if end <= start or any(start < hit["end"] and end > hit["start"] for hit in taken):
                continue
            taken.append({"label": label, "start": start, "end": end, "value": text[start:end]})
    return sorted(taken, key=lambda hit: hit["start"])


def scan(card: dict, environment: Sequence[str] = ()) -> list[dict]:
    """One hit per value across the card: {id, label, value, where, count}."""
    by_value: dict[str, dict] = {}
    for where, text, _ in strings(card):
        for hit in hits_in(text, environment):
            known = by_value.setdefault(
                hit["value"],
                {
                    "id": f"h{len(by_value) + 1}",
                    "label": hit["label"],
                    "value": hit["value"],
                    "where": [],
                    "count": 0,
                },
            )
            known["count"] += 1
            if where not in known["where"]:
                known["where"].append(where)
    return list(by_value.values())


def apply(card: dict, hits: list[dict], decisions: Mapping[str, str]) -> dict:
    """The card as it will be shared: each removed value replaced, everywhere it shows."""
    card = copy.deepcopy(card)
    removed = sorted(
        (hit["value"] for hit in hits if decisions.get(hit["id"]) == "remove"),
        key=len,
        reverse=True,
    )
    for _, text, put in strings(card):
        for value in removed:
            text = text.replace(value, REMOVED)
        put(text)
    return card


def digest(card: dict, hits: list[dict]) -> str:
    """What a preview showed: the card (paths shortened) and its hits, as one short hash."""
    shown = {"card": card, "hits": [(hit["id"], hit["label"], hit["value"]) for hit in hits]}
    return hashlib.sha256(json.dumps(shown, sort_keys=True).encode()).hexdigest()[:24]


def undecided(hits: list[dict], decisions: Mapping[str, str]) -> list[dict]:
    return [hit for hit in hits if decisions.get(hit["id"]) not in {"remove", "keep"}]


def fit(card: dict, payload: dict) -> tuple[dict, bool]:
    """The payload within 64 KiB: its result cut further while it is over (a result in a
    script of three bytes a letter, say). (payload, whether it was cut)."""
    cut = False
    while (over := len(encode({**payload, "card": card})) - MAX_PLAIN) > 0 and card.get("result"):
        raw = card["result"].removesuffix(CUT_NOTE).rstrip().encode()
        keep = max(0, len(raw) - over - len(CUT_NOTE.encode()) - 64)
        head = raw[:keep].decode("utf-8", "ignore").rstrip()
        card = {**card, "result": f"{head}\n\n{CUT_NOTE}" if head else ""}
        cut = True
    return {**payload, "card": card}, cut


def card_text(card: dict) -> str:
    """The card as plain Markdown: what Copy as text copies, and what a Router reads (quoted)."""
    marks = {"verified": "✓", "claimed": "~", "contradicted": "✗", "not_met": "✗"}
    unchecked = "\N{EN DASH}"
    lines = [f"## {card.get('title', '')}", "", card.get("status_words") or card.get("status", "")]
    if card.get("result"):
        lines += ["", card["result"].strip()]
    if card.get("criteria"):
        lines += ["", "**Done when**"]
        lines += [
            f"- {marks.get(item['state'], unchecked)} {item['text']}" for item in card["criteria"]
        ]
    seen = [
        f"- {item['label']}: `{item['command']}` {item['outcome']}"
        for item in card.get("checks", [])
    ]
    seen += [f"- {pr['label']}: {pr['url']}" for pr in card.get("prs", [])]
    if seen:
        lines += ["", "**Observed**", *seen]
    if card.get("flags"):
        lines += ["", "**Warnings**", *(f"- {flag}" for flag in card["flags"])]
    return "\n".join(lines).strip()
