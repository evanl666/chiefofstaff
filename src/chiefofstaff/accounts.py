"""Accounts an MCP tool needs connected: one shared card per server and source.

An MCP server can be reachable and its tool still fail, saying the owner must connect or
authorize an account in a browser. Copilot's own sign-in request (Asks.on_mcp_auth) does not
cover that, so each session met it alone: each asked the owner, one "done" reached one of them,
and Watch for you stopped trying. Here every such failure joins one record per server and
source, shared by the sessions it stops: one card under Needs you; "I've done it" has each of
them retry once (Watch for you scans again); only a call that works closes it.

A tool's result is untrusted (it may carry a phishing link). A card is made only for a failed
call of an MCP tool, its server named by the backend's tool metadata (never by what the result
says), reading as a narrow authorization-required error; a link only as a normalized http(s)
address, shown as it is opened and labelled unless it is one of the server's known addresses.
Anything less certain stays an ordinary error. Nothing opens a link but the owner.

A server may keep each connection for the app that made it (E137): Copilot's workers reach it as
Copilot CLI, a Claude Code or Codex worker as that app. Such a worker's failures are a record and
card of their own, naming its app ("… for Claude Code"); Copilot's sign-in to the server, or a
Copilot session's call that works, is never its connection.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
import unicodedata
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from .models import DraftDecision
from .store import now

if TYPE_CHECKING:
    from .runtime import Runtime

PREFIX = "connection:"
# After "Not now", the same account is not asked for again for this long (as for sign-ins).
QUIET = 600
# "I've done it" again this soon while its sessions retry starts no second round of retries.
RETRY_GAP = 120
# The app each other agent's workers reach MCP servers as (Copilot's: none named).
APPS = {"claude": "Claude Code", "codex": "Codex"}
MAX_TEXT = 6000
# An error that says an account must be connected or authorized; not any failure that mentions
# signing in or accounts.
AUTH_NEEDED = re.compile(
    r"\b(?:authori[sz]ation|authentication|re-?authentication|oauth|sign[- ]?in|log[- ]?in)"
    r"\s+(?:is\s+)?(?:required|needed)\b"
    r"|\b(?:connect|reconnect|authori[sz]e|link|re-?authenticate)\s+(?:your|the|an?)\s+"
    r"(?:[\w.-]+\s+){0,4}?account\b"
    r"|\baccount\s+(?:is\s+|has\s+)?(?:not|never)\s+(?:been\s+)?(?:yet\s+)?"
    r"(?:connected|authori[sz]ed|linked)\b"
    # "the user has not connected their glean account" (an agent platform's words: E138).
    r"|\b(?:has|have)(?:\s+not|n[\u2019']t)\s+(?:yet\s+)?(?:connected|authori[sz]ed|linked)\s+"
    r"(?:their|your|the|an?)\s+(?:[\w.-]+\s+){0,4}?account\b",
    re.IGNORECASE,
)
# Up to four capitalized words ("Acme Search", "Acme.io Docs"): a name, not the sentence on.
NAME = r"[A-Z0-9][\w-]*(?:\.[\w-]+)*(?:[ ][A-Z0-9][\w-]*(?:\.[\w-]+)*){0,3}"
# The account it names, when it names one: "Authorization required for Acme Search", "Connect
# your Acme Search account" (capitalized words: a name, not the rest of the sentence).
SOURCE = re.compile(
    r"(?i:\b(?:authori[sz]ation|authentication|sign[- ]?in|log[- ]?in)\s+(?:is\s+)?"
    r"(?:required|needed)\s+(?:for|to\s+(?:use|access))\s+)"
    rf"(?P<named>{NAME})"
    r"|(?i:\b(?:connect|reconnect|authori[sz]e|link)\s+(?:your|the)\s+)"
    rf"(?P<owned>{NAME})[ ](?i:account)\b"
    # "has not connected their glean account": the words before "account", whatever their case.
    r"|(?i:\b(?:has|have)(?:\s+not|n[\u2019']t)\s+(?:yet\s+)?(?:connected|authori[sz]ed|linked)\s+"
    r"(?:their|your|the|an?)\s+)(?P<theirs>[\w-]+(?:\.[\w-]+)*(?:[ ][\w-]+(?:\.[\w-]+)*){0,2})"
    r"[ ](?i:account)\b"
)
LINK = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
# Any other kind of address (javascript:, data:, a custom scheme): not a result to trust.
OTHER_LINK = re.compile(
    r"\b(?!https?://)[a-z][a-z0-9+.-]{1,30}://"
    r"|\b(?:javascript|vbscript|data|file|blob|filesystem|intent|about|chrome|view-source):(?=\S)",
    re.IGNORECASE,
)
HOST = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
# Where an MCP server's settings may name its own addresses.
ADDRESS_KEYS = ("url", "serverUrl", "authorization_url", "authorizationUrl", "auth_url", "authUrl")


def web_link(raw: str) -> tuple[str, str]:
    """An http(s) address as it is shown and opened (normalized: lower-case scheme and host,
    punycode, no default port), and its origin. ValueError for anything else, or one that
    could show other than it goes (credentials, backslashes, control or invisible characters)."""
    raw = raw.rstrip(".,;:!?)]}")
    if len(raw) > 2000 or "\\" in raw or any(unicodedata.category(c)[0] in "CZ" for c in raw):
        raise ValueError("Not a web address.")
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname or "@" in parts.netloc:
        raise ValueError("Not a web address.")
    try:
        port = parts.port
        host = parts.hostname.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise ValueError("Not a web address.") from None
    if not HOST.fullmatch(host):
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError("Not a web address.") from None
        host = f"[{host}]" if address.version == 6 else host
    default = {"http": 80, "https": 443}[scheme]
    netloc = host + (f":{port}" if port is not None and port != default else "")
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment)), (
        f"{scheme}://{netloc}"
    )


def needs_connection(text: str) -> tuple[str, list[tuple[str, str]]] | None:
    """A tool's failure that asks for an account to be connected: the account it names ("" for
    none) and its links (normalized, with their origins). None when it reads otherwise, or
    carries any address that is not plainly http(s)."""
    text = text[:MAX_TEXT]
    if not AUTH_NEEDED.search(text) or OTHER_LINK.search(text):
        return None
    links: list[tuple[str, str]] = []
    for raw in LINK.findall(text):
        try:
            link = web_link(raw)
        except ValueError:
            return None
        if link not in links:
            links.append(link)
    source = ""
    if found := SOURCE.search(text):
        source = found.group("named") or found.group("owned") or ""
        if theirs := found.group("theirs"):
            # Written as a name ("glean" -> "Glean"), as it is in its other words.
            source = " ".join(word[:1].upper() + word[1:] for word in theirs.split())
    return source[:60], links


def record_key(server: str, identity: str, app: str = "") -> str:
    named = f"{server}\0{identity.casefold()}" + (f"\0{app}" if app else "")
    digest = hashlib.sha256(named.encode()).hexdigest()[:24]
    return PREFIX + digest


class Accounts:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.bus = runtime.bus
        # Sessions whose tool failed while Copilot's own sign-in card for that server was open:
        # they retry once it ends. By server: {session id: tool}.
        self.joined: dict[str, dict[str, str]] = {}
        self.servers_: set[str] | None = None

    # Records

    def records(self, server: str = "") -> list[dict]:
        found = []
        for raw in self.store.items(PREFIX).values():
            with suppress(ValueError, TypeError):
                record = json.loads(raw)
                if isinstance(record, dict) and (not server or record.get("server") == server):
                    found.append(record)
        return found

    def servers(self) -> set[str]:
        """Servers with an account to connect: a call of any other is not looked at again
        (every MCP call that works asks this)."""
        if self.servers_ is None:
            self.servers_ = {record["server"] for record in self.records()}
        return self.servers_

    def save(self, record: dict):
        self.store.set(record["key"], json.dumps(record))
        self.servers_ = None

    def drop(self, record: dict):
        self.store.delete(record["key"])
        self.servers_ = None

    def app(self, session_id: str) -> str:
        """The app a session reaches MCP servers as, when it is not Copilot CLI."""
        return APPS.get(self.runtime.agent(session_id), "")

    def find(self, server: str, source: str, origin: str, app: str = "") -> dict | None:
        """Its record: by app, server and source (or the link's origin when it names none). One
        that names no source is the same as one of that server's on the same origin."""
        key = record_key(server, source or origin, app)
        records = [record for record in self.records(server) if record.get("app", "") == app]
        if found := next((record for record in records if record["key"] == key), None):
            return found
        if origin:
            return next(
                (
                    record
                    for record in records
                    if record.get("origin") == origin and not (source and record.get("source"))
                ),
                None,
            )
        return records[0] if not source and len(records) == 1 else None

    def known_origins(self, server: str) -> set[str]:
        """Addresses the server's own settings name, and where Copilot's sign-in to it led."""
        config = self.store.settings().mcp_servers.get(server) or {}
        values = [config.get(key) for key in ADDRESS_KEYS]
        if isinstance(oauth := config.get("oauth"), dict):
            values += [oauth.get(key) for key in ("issuer", *ADDRESS_KEYS)]
        origins = set(json.loads(self.store.get(f"signinorigin:{server}") or "[]"))
        for value in values:
            if isinstance(value, str):
                with suppress(ValueError):
                    origins.add(web_link(value)[1])
        return origins

    def watching(self, session_id: str) -> bool:
        row = self.store.find_session(session_id)
        return bool(row) and self.runtime.special(row) == "connector"

    def open_sessions(self, record: dict) -> list[dict]:
        rows = (self.store.find_session(session_id) for session_id in record["sessions"])
        return [row for row in rows if row and not row["closed_at"]]

    def target(self, record: dict) -> dict:
        rows = self.open_sessions(record)
        count = len(rows)
        watch = any(entry.get("watch") for entry in record["sessions"].values())
        if watch and not any(self.runtime.special(row) == "connector" for row in rows):
            count += 1  # Watch for you, in the session it scans in next.
        url = record.get("url", "")
        known = bool(url) and record.get("origin") in self.known_origins(record["server"])
        return {
            "connection_key": record["key"],
            "server": record["server"],
            "source": record["source"],
            **({"app": app} if (app := record.get("app")) else {}),
            "url": url,
            "known_origin": known,
            "url_note": "" if known or not url else f"Link supplied by {record['server']}",
            "last_error": record.get("last_error", ""),
            "last_failed_at": record.get("last_failed_at", ""),
            "affected_sessions": count,
            "retrying": record.get("status") == "retrying",
            # Its sessions retried after the owner's "I've done it" and failed the same way
            # (E138): connected, but not for Chief of Staff. Said so, not asked for again.
            "still_failing": bool(record.get("after_done")),
        }

    def card(self, record: dict) -> dict | None:
        with suppress(LookupError):
            draft = self.store.draft(record.get("draft_id", ""))
            if draft["status"] == "pending":
                return draft
        return None

    def update_card(self, record: dict):
        if draft := self.card(record):
            self.bus.publish(
                "draft", self.store.update_draft_target(draft["id"], self.target(record))
            )

    def close_card(self, record: dict, status: str, outcome: str):
        if draft := self.card(record):
            draft = self.store.update_draft(
                draft["id"], status=status, outcome=outcome, resolved_at=now()
            )
            self.bus.publish("draft", draft)

    # A tool's result

    def tool_result(self, session_id: str, server: str, tool: str, success: bool, text: str):
        """Every MCP call's end in a worker; `server` and `tool` as the backend named them."""
        if success:
            if server in self.servers():
                self.worked(session_id, server, tool)
            return
        found = needs_connection(text)
        if not found:
            return
        source, links = found
        known = self.known_origins(server)
        url, origin = next(((u, o) for u, o in links if o in known), ("", ""))
        if links and not url:
            if len({o for _, o in links}) > 1:
                return  # Links to several unknown places: not clear which is the server's.
            url, origin = links[0]
        app = self.app(session_id)
        if not app and self.join_signin(session_id, server, tool, origin):
            return  # (Copilot's sign-in: not another app's connection.)
        error = " ".join(text.split())[:1000]
        self.need(session_id, server, tool, source, url, origin, error, app)

    def need(
        self,
        session_id: str,
        server: str,
        tool: str,
        source: str,
        url: str,
        origin: str,
        error: str,
        app: str = "",
    ):
        record = self.find(server, source, origin, app)
        known = self.known_origins(server)
        if record is None:
            record = {
                "key": record_key(server, source or origin, app),
                "server": server,
                "source": source,
                **({"app": app} if app else {}),
                "origin": origin,
                "url": url,
                "tools": [],
                "sessions": {},
                "generation": 0,
                "status": "needed",
            }
        else:
            if url and (not record["url"] or origin in known or record["origin"] not in known):
                record["url"], record["origin"] = url, origin  # Never a known link for another.
            record["source"] = record["source"] or source
        if tool and tool not in record["tools"]:
            record["tools"].append(tool)
        record["last_error"], record["last_failed_at"] = error, now()
        quiet = record["status"] == "not_now" and time.time() < record.get("quiet_until", 0)
        if record["status"] == "retrying":
            record["after_done"] = int(record.get("after_done") or 0) + 1
        if not quiet:
            record["status"] = "needed"  # A retry that failed: the card says so again.
        entry = record["sessions"].setdefault(session_id, {})
        told = entry.get("told")
        entry.update(tool=tool, at=time.time(), watch=self.watching(session_id), told=True)
        self.save(record)
        if self.card(record):
            self.update_card(record)  # Updated, not alerted again.
        elif not quiet:
            self.open_card(session_id, record)
        if not told:
            self.tell(session_id, record, quiet)
        self.runtime.publish_state()

    def open_card(self, session_id: str, record: dict):
        text = f"{record['server']} needs you to connect {record['source'] or 'your account'}"
        if app := record.get("app"):
            text += f" for {app}"
        draft = self.store.create_draft(session_id, "connection", {}, text)
        record["draft_id"] = draft["id"]
        record.pop("quiet_until", None)
        self.save(record)
        draft = self.store.update_draft_target(draft["id"], self.target(record))
        self.runtime.add_message(
            session_id, "draft", text, {"draft_id": draft["id"], "label": "Connection needed"}
        )
        self.bus.publish("draft", draft)
        self.runtime.alert(
            text, session_id=session_id, needs_action=True, title="Connection needed"
        )

    def tell(self, session_id: str, record: dict, quiet: bool):
        what = f"{record['server']} needs the owner to connect {record['source'] or 'an account'}"
        if app := record.get("app"):
            what += f" for {app}"
        if quiet:
            note = f"{what}; they chose not to for now. Go on without it; do not ask them."
        elif self.watching(session_id):
            note = (
                f"{what}: they have a card for it. Do not ask them yourself, and do not try it "
                "again in this scan: list the watch that needs it in incomplete and go on with "
                "the others. Each later scan tries it once again."
            )
        else:
            note = (
                f"{what}: they have one card for it, shared with any other session it stops. Do "
                "not ask them yourself, and do not retry it now: go on with other steps, or "
                "wait. You will be told when to retry."
            )
        self.runtime.background(self.runtime.asks.tell_session(session_id, note))

    def worked(self, session_id: str, server: str, tool: str):
        """A call that works, of a tool that failed for want of the account in this session:
        connected."""
        found = [
            record
            for record in self.records(server)
            if session_id in record["sessions"] and tool in record["tools"]
        ]
        if len(found) == 1:
            self.connected(found[0], session_id)

    def connected(self, record: dict, by: str):
        self.close_card(record, "answered", "Connected: a retry worked.")
        self.drop(record)
        what = record["source"] or record["server"]
        self.retry(
            [
                (session_id, entry)
                for session_id, entry in record["sessions"].items()
                # Not one that just worked, or was told to retry already.
                if session_id != by and entry.get("retried") != record["generation"]
            ],
            lambda entry: (
                f"{what} works again (another session's call to it just did). Retry your failed "
                f"{entry.get('tool') or 'tool'} call once now."
            ),
        )
        self.runtime.publish_state()

    def retry(self, sessions: list[tuple[str, dict]], note: Callable[[dict], str]):
        """Each session retries its failed call once (told so); Watch for you scans again,
        once however many of its sessions there are."""
        watch = ""
        for session_id, entry in sessions:
            if entry.get("watch"):
                watch = watch or note(entry)
                continue
            row = self.store.find_session(session_id)
            if row and not row["closed_at"]:
                self.runtime.background(self.refresh_and_tell(row, note(entry)))
        if watch:
            self.runtime.background(self.runtime.connector.rescan_connection(watch))

    async def refresh_and_tell(self, row: dict, note: str):
        # On a new connection from its next turn: an MCP server may hold its sign-in state
        # per connection.
        await self.runtime.refresh_session(row)
        await self.runtime.asks.tell_session(row["id"], note)

    # The owner's answer

    async def answer(self, draft: dict, decision: DraftDecision) -> dict:
        key = draft["target"].get("connection_key", "")
        raw = self.store.get(key) if key.startswith(PREFIX) else None
        if not raw:
            draft = self.store.update_draft(
                draft["id"], status="expired", outcome="No longer needed.", resolved_at=now()
            )
            self.bus.publish("draft", draft)
            raise ValueError("This connection is no longer needed.")
        record = json.loads(raw)
        what = record["source"] or record["server"]
        if decision.action == "reject":
            record["status"], record["quiet_until"] = "not_now", time.time() + QUIET
            self.save(record)
            draft = self.store.update_draft(
                draft["id"],
                status="rejected",
                outcome=decision.reason or "Not now",
                resolved_at=now(),
            )
            self.bus.publish("draft", draft)
            for row in self.open_sessions(record):
                if self.runtime.special(row) != "connector":
                    self.runtime.background(
                        self.runtime.asks.tell_session(
                            row["id"],
                            f"The owner chose not to connect {what} now. Go on without it; do "
                            "not ask them again.",
                        )
                    )
            self.runtime.publish_state()
            return draft
        if decision.action != "answer":
            raise ValueError("Say you've done it, or choose Not now.")
        if record["status"] == "retrying" and time.time() - record["confirmed"] < RETRY_GAP:
            return draft  # Its sessions retry already: no second round.
        record["generation"] += 1
        record["status"], record["confirmed"] = "retrying", time.time()
        for entry in record["sessions"].values():
            entry["retried"] = record["generation"]
        self.save(record)
        self.retry(
            list(record["sessions"].items()),
            lambda entry: (
                f"The owner connected {what} (for {record['server']}). Retry your failed "
                f"{entry.get('tool') or 'tool'} call once now. If it still says the account "
                "needs connecting, do not retry again or ask them: the same card stays open."
            ),
        )
        draft = self.store.update_draft_target(draft["id"], self.target(record))
        self.bus.publish("draft", draft)
        self.runtime.publish_state()
        return draft

    # Sessions, restarts and Watch for you

    def closed(self, session_id: str):
        """A closed session no longer waits on any account; a card no session waits on goes."""
        for record in self.records():
            entry = record["sessions"].get(session_id)
            if entry is None or entry.get("watch"):
                continue
            del record["sessions"][session_id]
            if record["sessions"]:
                self.save(record)
                self.update_card(record)
            else:
                self.close_card(record, "expired", "No session needs it now.")
                self.drop(record)

    def restore(self):
        """At a start: a card whose sessions were retrying can be answered again (their
        retries ended with the last run); a record whose card went is let go."""
        for record in self.records():
            if record.get("status") == "retrying":
                record["status"] = "needed"
            if not self.card(record) and record.get("status") != "not_now":
                self.drop(record)
                continue
            self.save(record)
            self.update_card(record)

    def watch_record(self) -> dict | None:
        found = [
            record
            for record in self.records()
            if any(entry.get("watch") for entry in record["sessions"].values())
        ]
        return max(found, key=lambda record: record.get("last_failed_at", ""), default=None)

    def for_watch(self) -> dict | None:
        """The account Watch for you waits on (the latest), as Settings and the sidebar say it."""
        record = self.watch_record()
        if not record:
            return None
        return {
            "server": record["server"],
            "source": record["source"],
            "last_error": record.get("last_error", ""),
            "at": record.get("last_failed_at", ""),
            "retrying": record.get("status") == "retrying",
        }

    def watch_failed_since(self, moment: float) -> dict | None:
        """The account a scan begun at `moment` found it could not use, if any."""
        for record in self.records():
            for entry in record["sessions"].values():
                if entry.get("watch") and entry.get("at", 0) >= moment:
                    return record
        return None

    # Copilot's own sign-in card (Asks.on_mcp_auth), for the same server

    def signin_started(self, server: str, url: str):
        """Copilot asks the owner to sign in to `server` at `url` (its own address: known from
        now on). A card of this server's for an account on that address, or none, becomes that
        sign-in: its sessions retry once it ends."""
        try:
            origin = web_link(url)[1]
        except ValueError:
            return
        known = set(json.loads(self.store.get(f"signinorigin:{server}") or "[]"))
        if origin not in known:
            self.store.set(f"signinorigin:{server}", json.dumps(sorted(known | {origin})))
        for record in self.records(server):
            if record.get("origin") in {origin, ""} and not record.get("app"):
                self.close_card(record, "expired", f"Now {server}'s sign-in card.")
                self.drop(record)
                joined = self.joined.setdefault(server, {})
                for session_id, entry in record["sessions"].items():
                    joined[session_id] = entry.get("tool", "")

    def join_signin(self, session_id: str, server: str, tool: str, origin: str) -> bool:
        """A tool of `server` failed while its sign-in card is open: the same card, when the
        failure points where that sign-in does (or nowhere)."""
        if server not in self.runtime.asks.signin_pending:
            return False
        if origin and origin not in json.loads(self.store.get(f"signinorigin:{server}") or "[]"):
            return False
        joined = self.joined.setdefault(server, {})
        if session_id not in joined:
            self.runtime.background(
                self.runtime.asks.tell_session(
                    session_id,
                    f"{server} needs sign-in: the owner has a sign-in card for it. Do not ask "
                    "them yourself. You will be told when to retry.",
                )
            )
        joined[session_id] = tool
        return True

    def signin_ended(self, server: str, signed_in: bool):
        sessions = [
            (session_id, {"tool": tool, "watch": self.watching(session_id)})
            for session_id, tool in self.joined.pop(server, {}).items()
        ]
        if signed_in:
            self.retry(
                sessions,
                lambda entry: (
                    f"You are signed in to {server}. Retry your failed "
                    f"{entry['tool'] or 'tool'} call once now."
                ),
            )
            return
        for session_id, entry in sessions:
            if not entry["watch"]:
                self.runtime.background(
                    self.runtime.asks.tell_session(
                        session_id, f"The owner did not sign in to {server}. Go on without it."
                    )
                )
