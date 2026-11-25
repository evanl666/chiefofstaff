"""Share links, kept and published: a finished card sealed into a secret gist of the owner's
(through the bound-account primitive: the account's own token, its identity checked, never the
shared routing), its link, Revoke, and deletion at expiry. Nothing exists, runs or calls GitHub
until the owner's first link: the table is made then, and a timer waits only while a link is
live (or a gist waits to be deleted). sharing.py is the format, the snapshot and the scan.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from . import sharing
from .sources.github import GitHubError, answered
from .updates import installed

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

# Chief's own limit, per account, well inside GitHub's for content (80 a minute, 500 an hour)
# alongside Prepared for you's publishing.
LINKS_A_MINUTE = 10
LINKS_AN_HOUR = 60
# A gist that could not be deleted is tried again this often (seconds).
RETRY_SECONDS = 3600
# Ended links stay listed in Settings → You this long.
LISTED_DAYS = 7
# The longest a timer sleeps before looking again (a clock that jumps, a machine asleep).
LONGEST_WAIT = 6 * 3600
# A test-only expiry in seconds (live_share.py: a link deleted by the timer within minutes).
TEST_SECONDS = "CHIEFOFSTAFF_SHARE_TEST_SECONDS"
GIST_ID = re.compile(r"[0-9a-f]{20,40}")
SHARE_ID = re.compile(r"sh_[0-9a-f]{8}")
RATE_LIMITED = re.compile(r"rate limit|abuse|HTTP 429", re.IGNORECASE)

NO_GH = (
    "No GitHub account is signed in to gh on this computer. A link keeps the card in a secret "
    "gist of yours, so it needs one. Run `gh auth login` in a terminal and try again, or "
    "download the card as a file to send yourself."
)
SLOW_DOWN = "GitHub asked to wait a minute before the next link. Nothing was shared."
NO_ANSWER = "GitHub didn't answer, so nothing was shared."
CHANGED = "The card changed since this preview. Look at it again."
UNDECIDED = "Remove or keep each thing that looks private first."
FINISHED_ONLY = "Only a finished card can be shared."


class ChangedError(ValueError):
    """The card is not what the preview showed: the owner looks at it again."""


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def when(value: str) -> datetime:
    return sharing.parse_time(value)


def slug(title: str) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (words[:60].rstrip("-") or "shared-card") + ".html"


class Shares:
    TABLE = """
        CREATE TABLE IF NOT EXISTS shares (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, title TEXT NOT NULL,
            login TEXT NOT NULL, account_id INTEGER NOT NULL DEFAULT 0, gist_id TEXT UNIQUE,
            link TEXT NOT NULL DEFAULT '', digest TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL,
            ended_at TEXT, error TEXT NOT NULL DEFAULT '', tried_at TEXT
        );
        CREATE INDEX IF NOT EXISTS shares_by_task ON shares(task_id, created_at);
    """

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.timer: asyncio.Task | None = None
        self.made = False
        self.locks: dict[str, asyncio.Lock] = {}
        self.clock = lambda: datetime.now(UTC)

    # The table: made with the first link, never at upgrade.

    def exists(self, name: str = "shares") -> bool:
        if name == "shares" and self.made:
            return True
        found = self.store.one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        )
        if found and name == "shares":
            self.made = True
        return bool(found)

    def table(self):
        if not self.made:
            with self.store.lock:
                self.store.db.executescript(self.TABLE)
            self.made = True

    def row(self, share_id: str) -> dict:
        found = (
            self.store.one("SELECT * FROM shares WHERE id=?", (share_id,))
            if self.exists()
            else None
        )
        if not found:
            raise LookupError("No such link.")
        return found

    def update(self, share_id: str, **values: Any):
        sets = ", ".join(f"{key}=?" for key in values)
        self.store.execute(
            f"UPDATE shares SET {sets} WHERE id=?",  # noqa: S608  (keys are this module's)
            (*values.values(), share_id),
        )

    # Preview: the card as it would be shared, and what looks private in it.

    def finished(self, task_id: str) -> dict:
        task = self.runtime.tasks.get(task_id)
        if task["status"] not in sharing.STATUS_WORDS:
            raise ValueError(FINISHED_ONLY)
        return task

    def roots(self, task: dict) -> list[str]:
        """The folders whose paths become relative: the worker's checkouts and its folder."""
        row = self.store.find_session(task["session_id"]) or {}
        found = [row.get("cwd") or ""]
        if row:
            workspaces = self.runtime.workspaces
            for checkout in workspaces.checkouts(row):
                found += [checkout.get(key) or "" for key in ("path", "repo", "worktree")]
            with suppress(Exception):
                found.append(str(workspaces.workspace(row)))
        return [path for path in found if path]

    def prepared(self, task: dict) -> tuple[dict, list[dict], list[dict], str, bool]:
        """(card, the paths it shortened, its hits, the digest of what is shown, whether its
        result was cut)."""
        view = self.runtime.task_actions.view(task)
        ended = self.store.value("SELECT max(ended_at) FROM runs WHERE task_id=?", (task["id"],))
        raw = sharing.snapshot(view, ended or task.get("ended_at") or task["updated_at"])
        cut = raw["result"].endswith(sharing.CUT_NOTE)
        card, shortened = sharing.shorten(raw, self.roots(task))
        hits = sharing.scan(card, environment())
        return card, shortened, hits, sharing.digest(card, hits), cut

    async def preview(self, task_id: str) -> dict:
        task = self.finished(task_id)
        card, shortened, hits, digest, cut = self.prepared(task)
        accounts, private = await asyncio.gather(self.accounts(), self.private_repos(card))
        return {
            "card": card,
            "hits": hits,
            "digest": digest,
            "shortened": [item["to"] for item in shortened],
            "cut": cut,
            "private": private,
            "accounts": accounts,
            "account": self.default_account(accounts),
            "live": self.live_for(task_id),
            "days": list(sharing.EXPIRY_DAYS),
        }

    async def accounts(self) -> list[dict]:
        """The github.com accounts signed in to gh, gh's active one first."""
        try:
            found = await self.runtime.github.signed_in(fresh=True)
        except Exception:
            return []
        return sorted(found, key=lambda account: not account["active"])

    def default_account(self, accounts: list[dict]) -> str:
        last = (self.store.get("share:account") or "").lower()
        for account in accounts:
            if account["login"].lower() == last:
                return account["login"]
        return accounts[0]["login"] if accounts else ""

    async def private_repos(self, card: dict) -> list[str]:
        """The PRs' repositories that are private (people without access can't open their PRs),
        read once from GitHub as reads are; none when it doesn't answer quickly."""
        repos = list(dict.fromkeys(pr["label"].split("#")[0] for pr in card.get("prs", [])))

        async def private(repo: str) -> bool:
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
                return False
            try:
                async with asyncio.timeout(5):
                    said = await self.runtime.github.gh("api", f"repos/{repo}", "--jq", ".private")
            except Exception:
                return False
            return said.strip() == "true"

        found = await asyncio.gather(*(private(repo) for repo in repos))
        return [repo for repo, hidden in zip(repos, found, strict=True) if hidden]

    def checked(self, task_id: str, decisions: Mapping[str, str], digest: str):
        """The card with the owner's decisions applied, when it is still what the preview
        showed and every hit is decided. (task, card as shared, digest)."""
        task = self.finished(task_id)
        card, _, hits, now, _ = self.prepared(task)
        if now != digest:
            raise ChangedError(CHANGED)
        if sharing.undecided(hits, decisions):
            raise ValueError(UNDECIDED)
        return task, sharing.clean_card(sharing.apply(card, hits, decisions))

    def file(self, task_id: str, decisions: Mapping[str, str], digest: str) -> dict:
        """The card for Download HTML file: no gist, no gh, nothing to revoke."""
        task, card = self.checked(task_id, decisions, digest)
        return {"card": card, "name": slug(task["title"])}

    # Create a link.

    def lock(self, key: str) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())

    async def create(
        self, task_id: str, decisions: Mapping[str, str], digest: str, days: int, login: str
    ) -> dict:
        async with self.lock(task_id):
            task, card = self.checked(task_id, decisions, digest)
            if days not in sharing.EXPIRY_DAYS:
                raise ValueError("A link expires in 1, 7 or 30 days.")
            accounts = await self.accounts()
            if not accounts:
                raise ValueError(NO_GH)
            chosen = next((a["login"] for a in accounts if a["login"].lower() == login.lower()), "")
            if not chosen:
                raise ValueError(f"@{login} isn't signed in to gh on this computer.")
            self.table()
            self.limited(chosen)
            try:
                token, account_id = await self.runtime.github.bound_user(chosen)
            except RuntimeError as exc:
                raise ValueError(f"@{chosen}: {exc}") from exc
            now = self.clock().replace(microsecond=0)
            until = now + timedelta(days=days)
            if seconds := test_seconds():
                until = now + timedelta(seconds=seconds)
            payload, _ = sharing.fit(
                card,
                {
                    "v": sharing.VERSION,
                    "kind": "card",
                    "created_at": stamp(now),
                    "expires_at": stamp(until),
                    "by": {"login": chosen, "id": account_id},
                    "app": installed(),
                },
            )
            blob, key = sharing.seal(payload, account_id)
            share_id = f"sh_{secrets.token_hex(4)}"
            # Written before GitHub is asked: a stop mid-create is reconciled at the next start.
            self.store.execute(
                "INSERT INTO shares(id, task_id, title, login, account_id, digest, created_at, "
                "expires_at, state) VALUES(?,?,?,?,?,?,?,?,'creating')",
                (
                    share_id,
                    task_id,
                    task["title"][:300],
                    chosen,
                    account_id,
                    digest,
                    stamp(now),
                    stamp(until),
                ),
            )
            body = {
                "description": f"Chief of Staff share {share_id} · encrypted · expires "
                f"{until.date().isoformat()}",
                "public": False,
                "files": {"share.txt": {"content": blob}},
            }
            try:
                made = json.loads(
                    await self.runtime.github.raw(
                        "api",
                        "gists",
                        "--method",
                        "POST",
                        "--input",
                        "-",
                        payload=body,
                        token=token,
                    )
                )
                gist_id = str((made or {}).get("id") or "")
                if not GIST_ID.fullmatch(gist_id):
                    raise RuntimeError("GitHub did not say which gist it made.")
            except Exception as exc:
                raise await self.failed_create(share_id, chosen, token, exc) from exc
            link = sharing.make_link(gist_id, key, stamp(until))
            self.update(share_id, gist_id=gist_id, link=link, state="live")
            self.store.set("share:account", chosen)
        self.arm()
        return {"share": self.shown(self.row(share_id)), "link": link, "expires_at": stamp(until)}

    def limited(self, login: str):
        """Chief's own limit: at most LINKS_A_MINUTE a minute, LINKS_AN_HOUR an hour."""
        now = self.clock()
        for seconds, most in ((60, LINKS_A_MINUTE), (3600, LINKS_AN_HOUR)):
            made = self.store.value(
                "SELECT count(*) FROM shares WHERE lower(login)=lower(?) AND created_at>=? "
                "AND state!='failed'",
                (login, stamp(now - timedelta(seconds=seconds))),
            )
            if made >= most:
                raise ValueError(SLOW_DOWN)

    async def failed_create(
        self, share_id: str, login: str, token: str, exc: Exception
    ) -> ValueError:
        """The gist wasn't made, or may have been (no answer): found by its share id and
        deleted, so nothing is left that Chief doesn't know of. What to tell the owner."""
        text = str(exc)
        self.update(share_id, state="failed", ended_at=stamp(self.clock()), error=text[:300])
        if "timed out" in text or not isinstance(exc, GitHubError):
            with suppress(Exception):
                await self.reconcile(share_id, token)
            if "timed out" in text:
                return ValueError(NO_ANSWER)
        if isinstance(exc, GitHubError):
            said = f"{text} {answered(exc).get('message', '')}"
            if RATE_LIMITED.search(said):
                return ValueError(SLOW_DOWN)
            if re.search(r"HTTP 40[134]|HTTP 422", said):
                return ValueError(
                    f"@{login}'s sign-in can't create gists. Run `gh auth refresh -s gist` and "
                    "try again, or download the card as a file."
                )
        return ValueError(f"GitHub didn't make the link, so nothing was shared: {text[:200]}")

    async def reconcile(self, share_id: str, token: str) -> bool:
        """A create that may have gone through: the account's newest gists are read, and one
        whose description holds the share id is deleted. Whether one was."""
        listed = json.loads(
            await self.runtime.github.raw("api", "gists?per_page=30", token=token) or "[]"
        )
        for gist in listed if isinstance(listed, list) else []:
            if share_id in str(gist.get("description") or "") and GIST_ID.fullmatch(
                str(gist.get("id") or "")
            ):
                await self.runtime.github.raw(
                    "api", "--method", "DELETE", f"gists/{gist['id']}", token=token
                )
                return True
        return False

    # What the owner sees of their links.

    def shown(self, row: dict) -> dict:
        live = row["state"] == "live"
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "title": row["title"],
            "login": row["login"],
            "state": row["state"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "ended_at": row["ended_at"] or "",
            "error": row["error"],
            "link": row["link"] if live else "",
            "gist_url": f"https://gist.github.com/{row['login']}/{row['gist_id']}"
            if row["gist_id"]
            else "",
        }

    def listed(self) -> list[dict]:
        """Settings → You → Shared links: live links, and those that ended in the last week.
        Nothing (and no table) before the first link."""
        if not self.exists():
            return []
        since = stamp(self.clock() - timedelta(days=LISTED_DAYS))
        rows = self.store.all(
            "SELECT * FROM shares WHERE state='live' OR (state IN ('revoked','expired') "
            "AND ended_at>=?) ORDER BY created_at DESC",
            (since,),
        )
        return [self.shown(row) for row in rows]

    def live_for(self, task_id: str) -> dict | None:
        if not self.exists():
            return None
        row = self.store.one(
            "SELECT * FROM shares WHERE task_id=? AND state='live' ORDER BY created_at DESC "
            "LIMIT 1",
            (task_id,),
        )
        return self.shown(row) if row else None

    # Revoke, and the end of a link.

    async def delete_gist(self, login: str, gist_id: str) -> str:
        """The gist deleted as the account that made it: "" once it is gone (404: already),
        else why not, in the owner's words."""
        try:
            token, _ = await self.runtime.github.bound_user(login)
        except RuntimeError:
            return f"@{login} isn't signed in to gh on this computer"
        try:
            await self.runtime.github.raw(
                "api", "--method", "DELETE", f"gists/{gist_id}", token=token
            )
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return ""
            logger.info("Could not delete a shared card's gist.", exc_info=True)
            return "GitHub refused to delete it"
        return ""

    async def revoke(self, share_id: str) -> dict:
        row = self.row(share_id)
        async with self.lock(share_id):
            row = self.row(share_id)
            if row["state"] != "live":
                return self.shown(row)
            error = await self.delete_gist(row["login"], row["gist_id"])
            if error:
                self.update(share_id, error=error, tried_at=stamp(self.clock()))
                raise ValueError(
                    f"Couldn't revoke: {error}. Sign in with `gh auth login`, or delete it "
                    f"yourself on GitHub: gist.github.com/{row['login']}/{row['gist_id']}"
                    if "signed in" in error
                    else f"Couldn't revoke: {error}. Try again, or delete it yourself on GitHub: "
                    f"gist.github.com/{row['login']}/{row['gist_id']}"
                )
            self.update(share_id, state="revoked", ended_at=stamp(self.clock()), error="")
        self.arm()
        return self.shown(self.row(share_id))

    async def resume(self):
        """At start: what expired while Chief of Staff was off is deleted, a create a stop cut
        short is reconciled, and the timer is set while a link is live. Nothing without one."""
        if not self.exists():
            return
        for row in self.store.all("SELECT * FROM shares WHERE state='creating'"):
            with suppress(Exception):
                token, _ = await self.runtime.github.bound_user(row["login"])
                await self.reconcile(row["id"], token)
            self.update(row["id"], state="failed", ended_at=stamp(self.clock()))
        await self.sweep(retry_all=True)
        self.arm()

    async def sweep(self, retry_all: bool = False):
        """Links past their expiry: their gists deleted (expired). A gist that couldn't be
        deleted is tried again an hour later (at start, at once)."""
        now = self.clock()
        due = self.store.all(
            "SELECT * FROM shares WHERE state='live' AND expires_at<=?", (stamp(now),)
        )
        waiting = self.store.all("SELECT * FROM shares WHERE state='expired' AND error!=''")
        for row in due:
            error = await self.delete_gist(row["login"], row["gist_id"])
            self.update(
                row["id"],
                state="expired",
                ended_at=row["expires_at"],
                error=error,
                tried_at=stamp(now),
            )
        for row in waiting:
            tried = row["tried_at"] and when(row["tried_at"]) > now - timedelta(
                seconds=RETRY_SECONDS
            )
            if tried and not retry_all:
                continue
            error = await self.delete_gist(row["login"], row["gist_id"])
            self.update(row["id"], error=error, tried_at=stamp(now))
        if due or waiting:
            self.runtime.bus.publish("shares", {})

    def next_wake(self) -> datetime | None:
        if not self.exists():
            return None
        moments = []
        live = self.store.value("SELECT min(expires_at) FROM shares WHERE state='live'")
        if live:
            moments.append(when(live))
        retry = self.store.value(
            "SELECT min(coalesce(tried_at, ended_at)) FROM shares WHERE state='expired' "
            "AND error!=''"
        )
        if retry:
            moments.append(when(retry) + timedelta(seconds=RETRY_SECONDS))
        return min(moments) if moments else None

    def arm(self):
        """One timer, for the next expiry (or retry), only while one waits."""
        if self.timer and not self.timer.done():
            self.timer.cancel()
        self.timer = None
        wake = self.next_wake()
        if wake is None:
            return
        delay = max(0.0, min((wake - self.clock()).total_seconds(), LONGEST_WAIT))
        # Its own task, not the runtime's jobs: a timer is no work in progress (stop cancels it).
        self.timer = asyncio.get_running_loop().create_task(self.wait(delay))

    async def wait(self, delay: float):
        await asyncio.sleep(delay)
        self.timer = None
        try:
            await self.sweep()
        except Exception:
            logger.warning("Could not end the share links that expired.", exc_info=True)
        self.arm()

    def stop(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None


def environment() -> list[str]:
    """Values of Chief's environment that look like tokens (as a commit's check finds them)."""
    from .shepherd import token_values

    return token_values()


def test_seconds() -> int:
    """A test-only expiry, in seconds (at most an hour); 0 when not set."""
    try:
        seconds = int(os.environ.get(TEST_SECONDS, "0"))
    except ValueError:
        return 0
    return seconds if 0 < seconds <= 3600 else 0
