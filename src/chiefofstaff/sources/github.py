"""GitHub review requests on explicitly connected repositories, through the gh CLI."""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from .. import threads
from ..receipts import APPROVAL
from ..store import Store, now
from . import SourceHost

# How long a PR closed (not merged) stays followed, from when it was closed (or, closed already
# when a session was asked to follow it, from then): to hear if it is reopened.
KEEP_CLOSED_DAYS = 14
# Reads of a followed PR that GitHub says does not exist before it is let go.
MISSING_TRIES = 3
# GitHub's own answers only (case and all): "GitHub CLI not found" (gh missing from PATH) is
# this computer's problem, not a PR that is gone.
NOT_FOUND = re.compile(
    r"HTTP 404\b|\bgh: Not Found\b|Could not resolve to a (?:Repository|PullRequest)\b"
)
FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
# A followed PR shows as unreadable on its task after this many failed reads in a row.
SHOWN_AFTER = 3
# How often followed PRs are read (a new follow wakes the loop sooner).
POLL_SECONDS = 120
# After failed reads a PR is read again one poll later, then twice as long each time (four
# times for no access: that seldom mends by itself), at most this long apart. GitHub's rate
# limits reset within the hour too.
MAX_BACKOFF_SECONDS = 3600
# A read due this soon is made now: the next poll is minutes away.
EARLY_SECONDS = 10
# Why a read failed, as the owner is told it. A rate limit first: GitHub answers HTTP 403 for
# it too, and it says nothing of what this account may read.
KINDS = ("permission", "not_found", "rate_limited", "transient")
RATE_LIMITED = re.compile(r"rate limit|HTTP 429\b|too many requests|abuse detection", re.IGNORECASE)
PERMISSION = re.compile(
    r"HTTP 40[13]\b|Resource not accessible|SAML|single sign-on|\bSSO\b|Bad credentials|"
    r"do not have permission|must have (?:admin|push|read|write) (?:rights|access)|"
    r"no longer signed in|requires authentication",
    re.IGNORECASE,
)
# What a rate limit's answer may say of when it ends: seconds to wait, or the reset (epoch).
RETRY_AFTER = re.compile(r"retry[- ]after:?\s*(\d+)", re.IGNORECASE)
RATE_RESET = re.compile(r"x-ratelimit-reset:?\s*(\d{9,})", re.IGNORECASE)
# GitHub not answering, in a few words for the owner.
NOT_ANSWERING = (
    (re.compile(r"timed? ?out|timeout", re.IGNORECASE), "timeout"),
    (re.compile(r"HTTP (5\d\d)"), r"HTTP \1"),
    (
        re.compile(r"connection|EOF|TLS|network|unexpected end|no such host", re.IGNORECASE),
        "network error",
    ),
    (re.compile(r"temporarily|try again|unavailable", re.IGNORECASE), "unavailable"),
)
# What reading a followed PR keeps between reads (its failures, its backoff, where its news got
# to, its head and closing): of no use once nobody follows it. Its news, the tasks' results and
# what they made stay.
READ_STATE = (
    "ghfail",
    "gh404",
    "ghskip",
    "ghwhy",
    "ghnext",
    "ghread",
    "ghsince",
    "ghhead",
    "ghclosed",
    "ghkeep",
)


def failure_kind(error: str) -> str:
    """What a failed read says: "rate_limited", "not_found", "permission", or "transient"
    (GitHub not answering: a 5xx, a timeout, the network; and anything else, so that it is
    tried again, never taken for a PR that is gone or out of reach)."""
    if RATE_LIMITED.search(error):
        return "rate_limited"
    if NOT_FOUND.search(error):
        return "not_found"
    return "permission" if PERMISSION.search(error) else "transient"


def backoff(kind: str, failures: int, step: str) -> float:
    """Seconds until a followed PR that failed `failures` reads in a row is read again. One not
    found is read again at the next poll: three in a row say it is gone."""
    if kind == "not_found" and step == "pull request":
        return POLL_SECONDS
    factor = 4 if kind == "permission" else 2
    return min(MAX_BACKOFF_SECONDS, POLL_SECONDS * factor ** min(max(failures, 1) - 1, 12))


def iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).isoformat()


def clock(moment: float) -> str:
    """A time of day as the owner reads it ("3:05 PM"), in this computer's time zone."""
    local = datetime.fromtimestamp(moment).astimezone()
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def why_words(kind: str, detail: str, retry_at: str | None, moment: float) -> str:
    """Why a followed PR could not be read and when it is tried again, in one short sentence
    for the owner (`detail`: GitHub's own words; `moment`: now)."""
    if kind == "rate_limited":
        said = "GitHub rate limit"
    elif kind == "not_found":
        said = "GitHub says there is no such pull request, or no account here can see it"
    elif kind == "permission" and re.search(r"SAML|single sign-on|\bSSO\b", detail, re.IGNORECASE):
        said = "GitHub says this account must be authorized for the organization's SSO to read it"
    elif kind == "permission" and re.search(
        r"Bad credentials|no longer signed in", detail, re.IGNORECASE
    ):
        said = "GitHub no longer accepts the signed-in account (gh auth login signs in again)"
    elif kind == "permission":
        said = "GitHub says this account cannot read it (permission)"
    elif "GitHub CLI not found" in detail:
        said = "The GitHub CLI (gh) is not installed here"
    else:
        cause = next(
            (
                found.expand(words)
                for pattern, words in NOT_ANSWERING
                if (found := pattern.search(detail))
            ),
            "",
        )
        said = (
            f"GitHub did not answer ({cause})" if cause else "Reading it failed (unexpected error)"
        )
    if not retry_at:
        return said
    at = datetime.fromisoformat(retry_at).timestamp()
    if kind == "rate_limited":
        return f"{said}; retrying at {clock(at)}"
    minutes = math.ceil((at - moment) / 60)
    return f"{said}; retrying now" if minutes <= 0 else f"{said}; retrying in {minutes} min"


# What Chief of Staff posted for the owner ended with before 0.1.43, unseen on GitHub but there
# when it is edited or quoted: text that has it is still known as drafted, not the owner's own
# words. What it posts now is known by its id (GitHubSource.posted), else by its words on that PR
# (posted_key).
POSTED_MARK = "<!-- chief-of-staff -->"


def posted_key(label: str, text: str) -> str:
    """A text Chief of Staff posts on a PR ("owner/repo#n"), by its words (spacing aside): known
    as its own also when GitHub's answer, with the id, was lost (a send cut off, comments that
    could not be listed). Not a text another PR got: "LGTM" posted once is no proof elsewhere."""
    words = " ".join(unmarked(text).split())
    found = hashlib.sha256(f"{label.lower()}\n{words}".encode()).hexdigest()[:32]
    return f"github:text:{found}"


def posted_text(store: Store, label: str, text: str) -> bool:
    """Whether Chief of Staff posted this text on that PR (or tried to: a send whose answer was
    lost may have gone out), or it carries the mark it once left."""
    if POSTED_MARK in (text or ""):
        return True
    found = "SELECT id FROM triggers WHERE key=? AND source='github-sent-text'"
    return bool(text.strip()) and bool(store.one(found, (posted_key(label, text),)))


# `git commit` output: "[branch abc1234] message".
COMMITTED = re.compile(r"\[[^\]\n]*? ([0-9a-f]{7,40})\]")


def worker_commits(store: Store) -> set[str]:
    """Commits Chief of Staff's workers made (their `git commit` output names them)."""
    found = set()
    for row in store.all(
        "SELECT output FROM commands WHERE kind='commit' AND exit_code=0 AND output!=''"
    ):
        found.update(COMMITTED.findall(row["output"]))
    return found


# A reflog entry of a commit made in that checkout ("commit: …", "commit (amend): …", a
# cherry-pick, a rebase's picks and the commit it goes on with after a conflict, `git pull
# --rebase`'s too, whatever its options: "pull -q --rebase origin main (pick): …", a merge
# commit), not one a fetch, pull, reset or checkout brought in. Not a rebase's "(finish)": it
# names the new head, someone else's commit when the rebase only fast-forwarded.
MADE_HERE = re.compile(
    r"^(?:commit|cherry-pick|revert|am)\b"
    r"|^(?:pull\b[^:(]*|rebase(?: -i)?) \((?:pick|reword|edit|squash|fixup|continue)\)"
    r"|^(?:merge|pull)\b.*: Merge made by"
)


def worker_folders(store: Store) -> list[str]:
    """The git repositories Chief of Staff's workers worked in, one folder each: their
    worktrees' (a closed worker's worktree may be gone; its branch's log is in the
    repository), the project folders they shared with the owner, and those cloned into their
    own folder."""
    folders = []
    workspaces = store.items("workspace:")
    for row in store.all("SELECT id, repos, cwd FROM sessions WHERE role='worker'"):
        for checkout in json.loads(row["repos"] or "[]"):
            folders += [checkout.get("repo") or "", checkout.get("path") or ""]
        for own in dict.fromkeys(filter(None, [row["cwd"], workspaces.get(row["id"])])):
            folders.append(own)
            with suppress(OSError):
                folders += [entry.path for entry in os.scandir(own) if entry.is_dir()]
    repositories: dict[str, str] = {}
    for folder in dict.fromkeys(filter(None, folders)):
        marker = Path(folder) / ".git"
        # os.path's checks answer False for a folder that cannot be read; Path's raise.
        if os.path.isdir(marker):  # noqa: PTH112
            repositories.setdefault(os.path.realpath(marker), folder)
        elif os.path.isfile(marker):  # noqa: PTH113
            # A worktree: "gitdir: <repository>/.git/worktrees/<x>".
            with suppress(OSError):
                linked = marker.read_text(encoding="utf-8").removeprefix("gitdir:").strip()
                common = str(Path(os.path.realpath(Path(folder) / linked)).parent.parent)
                repositories.setdefault(common, folder)
    return list(repositories.values())


async def made_locally(folders: list[str], shas: list[str]) -> set[str]:
    """Of these commits, those made in one of these folders (their reflogs say so)."""
    from ..workspaces import git

    wanted, found = set(shas), set()
    for folder in folders:
        try:
            # Every branch's log, and each worktree's HEAD's.
            log = await git("-C", folder, "reflog", "show", "--all", "--format=%H %gs", limit=20)
        except ValueError:
            continue
        for line in log.splitlines():
            sha, _, subject = line.partition(" ")
            if sha in wanted and MADE_HERE.search(subject):
                found.add(sha)
        if found == wanted:
            break
    return found


def unmarked(text: str | None) -> str:
    """What was posted, without the mark it once carried: to compare with the draft."""
    return (text or "").replace(POSTED_MARK, "").strip()


def github_time(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def body(item: dict) -> str:
    return " ".join(unmarked(item.get("body")).split())[:1500] or "(no text)"


logger = logging.getLogger(__name__)


async def gh(*arguments: str, payload: dict | None = None, token: str = "") -> str:
    """`token`: act as that signed-in account instead of gh's active one."""
    executable = shutil.which("gh")
    if not executable:
        raise RuntimeError(
            "GitHub CLI not found. Install gh and sign in before connecting reviews."
        )
    env = {key: value for key, value in os.environ.items() if not key.startswith("CHIEFOFSTAFF_")}
    env.update(GH_PROMPT_DISABLED="1", GH_PAGER="cat", NO_COLOR="1")
    if token:
        env["GH_TOKEN"] = token
    process = await asyncio.create_subprocess_exec(
        executable,
        *arguments,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(30):
            stdout, stderr = await process.communicate(
                json.dumps(payload).encode() if payload is not None else None
            )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RuntimeError("GitHub request timed out.") from exc
    if process.returncode:
        raise RuntimeError("GitHub request failed: " + stderr.decode(errors="replace")[:1500])
    return stdout.decode(errors="replace")


# What gh says when the account it used cannot see a repository (another may).
ACCESS_ERRORS = re.compile(
    r"HTTP 40[34]|Could not resolve to a Repository|SAML|do not have permission|"
    r"cannot be searched",
    re.IGNORECASE,
)
# How long a signed-in account's token is reused before gh is asked for it again.
TOKEN_SECONDS = 300
# A 403 that says nothing about this account's access: another account must not take over.
TRANSIENT = re.compile(r"rate limit|abuse detection|try again later", re.IGNORECASE)


# A review's verdict, as GitHub's review event.
REVIEW_EVENTS = {"approve": "APPROVE", "request_changes": "REQUEST_CHANGES", "comment": "COMMENT"}

# Why GitHub refused a merge (gh prints GraphQL's words), as the owner reads it.
HEAD_MOVED = re.compile(r"Head branch was modified|expected head (?:sha|oid)", re.IGNORECASE)
MERGE_REFUSALS = (
    (re.compile(r"Base branch was modified", re.IGNORECASE), "Its base branch moved: try again."),
    (
        re.compile(r"not mergeable|merge conflict", re.IGNORECASE),
        "GitHub cannot merge it as it is (a conflict, or a check or review it requires): see "
        "the PR.",
    ),
    (
        re.compile(r"required status check|status checks? .*(?:expected|pending)", re.IGNORECASE),
        "A check GitHub requires has not passed yet.",
    ),
    (
        re.compile(r"approving review|review is required", re.IGNORECASE),
        "GitHub requires an approving review first.",
    ),
)


def merge_refusal(error: str) -> tuple[str, bool]:
    """Why a merge failed, in words, and whether it is that new commits came (the head the
    card shows is not the PR's any more)."""
    if HEAD_MOVED.search(error):
        return "New commits were pushed since: check them, then merge.", True
    for pattern, reason in MERGE_REFUSALS:
        if pattern.search(error):
            return reason, False
    plain = re.sub(
        r"^(?:GitHub request failed:\s*)?(?:GraphQL:\s*)?|\s*\(mergePullRequest\)", "", error
    )
    return " ".join(plain.split())[:300] or "GitHub refused.", False


class NoAccess:
    """Whether a gh failure means this account cannot see the repository (another may)."""

    @staticmethod
    def search(text: str) -> bool:
        return bool(ACCESS_ERRORS.search(text)) and not TRANSIENT.search(text)


NO_ACCESS = NoAccess()
REPOSITORY_ARGUMENT = re.compile(
    r"^(?:repos/|--repo=?|https://github\.com/)([A-Za-z0-9_.-]+)/[A-Za-z0-9_.-]+"
)


def owner_of(arguments: tuple[str, ...]) -> str:
    """The repository owner a gh command is about ("" when none)."""
    for index, argument in enumerate(arguments):
        if argument == "--repo" and index + 1 < len(arguments):
            argument = "--repo=" + arguments[index + 1]
        if found := REPOSITORY_ARGUMENT.match(argument):
            return found.group(1).lower()
    return ""


def diff_lines(files: list[dict]) -> dict[str, dict[str, set[int]] | None]:
    """The lines of each changed file a review comment can be on, by side; None when GitHub
    gives no patch (a binary or very large file) to check against."""
    lines: dict[str, dict[str, set[int]] | None] = {}
    for changed in files:
        patch = changed.get("patch")
        if patch is None:
            # A binary file has no lines to comment on; a very large one is not checked.
            binary = not (changed.get("additions") or changed.get("deletions"))
            lines[changed["filename"]] = {"LEFT": set(), "RIGHT": set()} if binary else None
            continue
        sides: dict[str, set[int]] = {"LEFT": set(), "RIGHT": set()}
        old = new = 0
        for row in patch.split("\n"):
            if hunk := re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", row):
                old, new = int(hunk.group(1)), int(hunk.group(2))
            elif row.startswith("+"):
                sides["RIGHT"].add(new)
                new += 1
            elif row.startswith("-"):
                sides["LEFT"].add(old)
                old += 1
            elif not row.startswith("\\"):  # "\ No newline at end of file"
                sides["LEFT"].add(old)
                sides["RIGHT"].add(new)
                old += 1
                new += 1
        lines[changed["filename"]] = sides
    return lines


def snippet(files: list[dict], path: str, line: int, side: str = "RIGHT", around: int = 3) -> str:
    """The diff lines around a comment's line (as the PR shows them), for the owner to see what
    it is about; "" when GitHub gives no patch for the file."""
    changed = next((f for f in files if f.get("filename") == path), None)
    patch = (changed or {}).get("patch")
    if not patch:
        return ""
    rows: list[tuple[int | None, int | None, str]] = []
    old = new = 0
    for row in patch.split("\n"):
        if hunk := re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", row):
            old, new = int(hunk.group(1)), int(hunk.group(2))
            rows.append((None, None, "…"))
        elif row.startswith("+"):
            rows.append((None, new, row))
            new += 1
        elif row.startswith("-"):
            rows.append((old, None, row))
            old += 1
        elif not row.startswith("\\"):
            rows.append((old, new, row))
            old += 1
            new += 1
    key = 0 if side == "LEFT" else 1
    found = next((i for i, row in enumerate(rows) if row[key] == line), None)
    if found is None:
        return ""
    picked = [row[2] for row in rows[max(0, found - around) : found + 1] if row[2] != "…"]
    return "\n".join(text[:200] for text in picked)


def same_text(posted: str, drafted: str) -> bool:
    """Whether a posted text is a drafted one, copied and perhaps edited a little."""
    from difflib import SequenceMatcher

    def plain(text: str) -> str:
        return " ".join(re.sub(r"[^\w\s@]", " ", text).casefold().split())

    posted, drafted = plain(posted), plain(drafted)
    if not posted or not drafted:
        return False
    if min(len(posted), len(drafted)) < 40:
        return posted == drafted  # "LGTM" is no copy of "LGTM!" by itself.
    shorter, longer = sorted((posted, drafted), key=len)
    if shorter in longer and len(shorter) >= 0.6 * len(longer):
        return True
    return SequenceMatcher(None, posted, drafted, autojunk=False).ratio() >= 0.8


def pages(text: str) -> list:
    """The items of a paginated gh api answer: one array, or (older gh) one array per page
    printed back to back."""
    decoder, items, at = json.JSONDecoder(), [], 0
    text = text.strip()
    while at < len(text):
        page, at = decoder.raw_decode(text, at)
        items += page if isinstance(page, list) else [page]
        while at < len(text) and text[at].isspace():
            at += 1
    return items


class GitHubSource:
    name = "github"

    def __init__(self, store: Store, host: SourceHost, *, gh_command=gh):
        self.store = store
        self.host = host
        self.raw = gh_command
        # Every call goes through the account that can see the repository it is about.
        self.gh = self.routed
        # Never read yet (not 0: the monotonic clock starts at boot, so 0 can be "just now").
        self.accounts_cache: tuple[float, list[dict]] = (float("-inf"), [])
        self.tokens: dict[str, tuple[float, str]] = {}
        self.changed = asyncio.Event()
        self.job: asyncio.Task | None = None
        self.status = {"enabled": False, "connected": False, "error": "", "last_checked": None}
        # What a followed PR's poll is reading now: when it fails, what could not be read.
        self.reading: dict[str, str] = {}
        # One read of a PR at a time (the poll's, or one the owner asked for now).
        self.locks: dict[str, asyncio.Lock] = {}
        self.clock = time.time

    async def start(self):
        self.job = asyncio.create_task(self.loop())

    async def stop(self):
        if self.job:
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)

    def wake(self):
        self.changed.set()

    async def loop(self):
        while True:
            self.changed.clear()
            try:
                await self.poll_once()
            except Exception as exc:
                if self.status["error"] != str(exc):
                    logger.warning("GitHub watcher failed: %s", exc)
                self.status.update(connected=False, error=str(exc)[:500])
            with suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), timeout=POLL_SECONDS)

    async def signed_in(self, fresh: bool = False) -> list[dict]:
        """The github.com accounts signed in to gh ({login, active})."""
        cached_at, accounts = self.accounts_cache
        if not fresh and time.monotonic() - cached_at < 600:
            return accounts
        try:
            status = json.loads(await self.raw("auth", "status", "--json", "hosts"))
        except Exception:
            return accounts  # An older gh, or none signed in: nothing else to try.
        accounts = [
            {"login": account["login"], "active": bool(account.get("active"))}
            for account in status.get("hosts", {}).get("github.com", [])
            if account.get("state") == "success" and account.get("login")
        ]
        self.accounts_cache = (time.monotonic(), accounts)
        return accounts

    async def accounts(self) -> list[str]:
        """The other signed-in accounts, tried when gh's active one cannot see a repository.
        Read afresh: the owner may just have signed in with the one that can."""
        return [
            account["login"]
            for account in await self.signed_in(fresh=True)
            if not account["active"]
        ]

    async def acting_login(self, repo: str) -> str:
        """The account that reaches this repository (lowercase): the one remembered for its
        owner, else gh's active one. What it publishes there is published as it."""
        if remembered := self.store.get(f"ghaccount:{repo.split('/')[0].lower()}"):
            return remembered.lower()
        active = [a["login"] for a in await self.signed_in() if a["active"]]
        return active[0].lower() if active else ""

    async def logins(self) -> set[str]:
        """The owner's GitHub logins (lowercase)."""
        return {account["login"].lower() for account in await self.signed_in()}

    async def routed(self, *arguments: str, payload: dict | None = None) -> str:
        """gh as the account that can see the repository: the one remembered for its owner,
        else gh's active one, else another signed in (a company account, say)."""
        owner = owner_of(arguments)
        if not owner:
            return await self.raw(*arguments, payload=payload)
        remembered = self.store.get(f"ghaccount:{owner}") or ""
        failure: RuntimeError | None = None
        tried: set[str] = set()
        for login in [remembered, ""]:
            if login in tried:
                continue
            tried.add(login)
            try:
                return await self.as_account(owner, login, remembered, arguments, payload)
            except RuntimeError as exc:
                if not NO_ACCESS.search(str(exc)):
                    raise
                failure = failure or exc
        for login in await self.accounts():
            if login in tried:
                continue
            try:
                return await self.as_account(owner, login, remembered, arguments, payload)
            except RuntimeError as exc:
                if not NO_ACCESS.search(str(exc)):
                    raise
        # The first attempt above always runs, so `failure` is set by now.
        raise failure or RuntimeError("No GitHub account could be tried.")

    async def as_account(self, owner, login, remembered, arguments, payload) -> str:
        """One attempt as `login` ("": gh's active account); a 403/404 means try another, and
        the first that answers is remembered for the owner. A failed attempt wrote nothing, so
        trying again as another account cannot post twice."""
        token = ""
        if login:
            try:
                token = await self.token(login)
            except RuntimeError as exc:
                raise RuntimeError(f"HTTP 403: {login} is no longer signed in to gh") from exc
        extra = {"token": token} if token else {}
        try:
            reply = await self.raw(*arguments, payload=payload, **extra)
        except RuntimeError:
            if login:
                # Revoked, rotated or signed out since: gh is asked for it again next time.
                self.tokens.pop(login, None)
            raise
        if login != remembered:
            if login:
                self.store.set(f"ghaccount:{owner}", login)
            else:
                self.store.delete(f"ghaccount:{owner}")
        return reply

    async def token_for(self, repo: str) -> str:
        """The token of the account that can see this repository, for a command that runs gh
        itself (a clone); "" for gh's active account."""
        try:
            await self.gh("api", f"repos/{repo}", "--jq", ".full_name")
        except RuntimeError:
            return ""
        login = self.store.get(f"ghaccount:{repo.split('/')[0].lower()}")
        return await self.token(login) if login else ""

    async def token(self, login: str) -> str:
        cached_at, token = self.tokens.get(login, (float("-inf"), ""))
        if time.monotonic() - cached_at < TOKEN_SECONDS:
            return token
        token = (
            await self.raw("auth", "token", "--hostname", "github.com", "--user", login)
        ).strip()
        self.tokens[login] = (time.monotonic(), token)
        return token

    async def pull(self, repo: str, number: int) -> dict:
        return json.loads(await self.gh("api", f"repos/{repo}/pulls/{int(number)}"))

    def seen(self, key: str) -> bool:
        return bool(self.store.one("SELECT id FROM triggers WHERE key=?", (key,)))

    def sent_by_us(self, key: str) -> bool:
        """Chief of Staff posted it (as the owner). Not just read by a PR's follower."""
        found = "SELECT id FROM triggers WHERE key=? AND source='github-sent'"
        return bool(self.store.one(found, (key,)))

    def posting(self, target: dict, *texts: str):
        """Texts about to be posted on the target PR: known as Chief of Staff's by their words,
        whatever comes back (see posted_key)."""
        label = f"{target['repo']}#{int(target['number'])}"
        for text in texts:
            if text.strip():
                self.store.add_trigger("github-sent-text", posted_key(label, text), "")

    def posted(self, key: str, text: str, target: dict):
        """What Chief of Staff posted for a draft: never news to the PR's followers, and a
        reply to it is news to the task that drafted it (a reviewer that does not own the PR)."""
        self.store.add_trigger("github-sent", key, text)
        if target.get("task_id"):
            self.store.set(f"ghby:{key}", target["task_id"])

    async def poll_once(self):
        settings = self.store.settings()
        self.status["enabled"] = settings.github_enabled
        if settings.paused:
            return
        followed = self.host.followed("github:")
        self.forget_unfollowed({sub["thread"] for sub in followed})
        if not (settings.github_enabled or followed):
            self.status.update(connected=False, error="")
            return
        errors = []
        if settings.github_enabled:
            # Its failure (one owner's search, say) must not stop the followed PRs.
            errors = await self.poll_review_requests(settings, {sub["thread"] for sub in followed})
        for sub in followed:
            # One inaccessible PR must not stop the others.
            await self.read_followed(sub["thread"], sub["since"])
        self.status.update(connected=True, error="; ".join(errors)[:500], last_checked=now())

    async def read_followed(self, thread: str, since: float, at_once: bool = False) -> bool:
        """Read a followed PR, unless it is waiting out its backoff after failed reads
        (`at_once`: the owner asked to try now). True when it was read."""
        async with self.locks.setdefault(thread, asyncio.Lock()):
            moment = self.clock()
            # Failures in a row (ghfail) count real reads only: one backing off is not read
            # before its next read is due (ghnext).
            failures = int(self.store.get(f"ghfail:{thread}") or 0)
            due = self.store.get(f"ghnext:{thread}") or ""
            waiting = bool(failures and due and not at_once)
            if waiting and moment < datetime.fromisoformat(due).timestamp() - EARLY_SECONDS:
                return False
            try:
                followed = await self.poll_pull(thread, since)
            except Exception as exc:
                await self.read_failed(thread, failures + 1, exc)
                return False
            self.reading.pop(thread, None)
            if not followed:
                self.forget(thread)  # Let go as it ended: nothing of it is read again.
            else:
                self.read_ok(thread)
            return True

    async def read_failed(self, thread: str, failures: int, exc: Exception):
        """A followed PR could not be read: why, said on its task's card once it has failed a
        few times in a row; when it is read again; let go once GitHub said three times in a
        row that it does not exist."""
        logger.warning("Could not read followed %s (%s): %s", thread, failures, exc)
        before = json.loads(self.store.get(f"ghwhy:{thread}") or "{}")
        detail = " ".join(str(exc).removeprefix("GitHub request failed: ").split())[:300]
        kind, step, moment = failure_kind(detail), self.reading.pop(thread, ""), self.clock()
        retry = moment + backoff(kind, failures, step)
        reset = await self.rate_reset(thread, detail) if kind == "rate_limited" else None
        if reset and reset > moment:
            # When GitHub lifts it, sooner or later than backing off would (within the hour).
            retry = min(reset, moment + MAX_BACKOFF_SECONDS)
        found = {
            "kind": kind,
            "step": step,
            "error": why_words(kind, detail, iso(retry), moment),
            "detail": detail,
            "at": iso(moment),
            "retry_at": iso(retry),
        }
        self.store.set(f"ghfail:{thread}", str(failures))
        self.store.set(f"ghwhy:{thread}", json.dumps(found))
        self.store.set(f"ghnext:{thread}", iso(retry))
        if failures == SHOWN_AFTER or (
            failures > SHOWN_AFTER and (before.get("kind"), before.get("step")) != (kind, step)
        ):
            self.host.read_health_changed(thread)  # Its task's card says so now.
        missing = int(self.store.get(f"gh404:{thread}") or 0)
        if kind == "not_found" and step == "pull request":
            missing += 1
        elif kind == "permission" or step != "pull request":
            missing = 0  # GitHub refuses it, or the PR itself was read: it is there.
        # A rate limit or GitHub not answering says nothing of it either way: the count stays.
        if missing:
            self.store.set(f"gh404:{thread}", str(missing))
        else:
            self.store.delete(f"gh404:{thread}")
        if missing >= MISSING_TRIES:
            # No such PR (an example in a report, a typo), or no account sees it: following it
            # can never bring news.
            self.host.missing(
                thread, "GitHub has no such pull request, or none of your accounts can see it"
            )

    async def rate_reset(self, thread: str, detail: str) -> float | None:
        """When GitHub lifts the rate limit a read of this PR hit (epoch seconds), when it says:
        in its answer, else the reading account's REST limit (asking does not count against
        it). None for a secondary limit, which says no time."""
        if found := RETRY_AFTER.search(detail):
            return self.clock() + int(found.group(1))
        if found := RATE_RESET.search(detail):
            return float(found.group(1))
        owner = thread.removeprefix("github:").split("/")[0]
        login = self.store.get(f"ghaccount:{owner}") or ""
        try:
            extra = {"token": await self.token(login)} if login else {}
            core = json.loads(await self.raw("api", "rate_limit", **extra))["resources"]["core"]
        except Exception:
            return None
        return float(core["reset"]) if core.get("remaining") == 0 and core.get("reset") else None

    async def retry(self, thread: str) -> bool:
        """Read a followed PR now, whatever its backoff (the owner asked): True when it was
        read. Failed again, it is read next after the backoff that failure brings."""
        since = next(
            (sub["since"] for sub in self.host.followed("github:") if sub["thread"] == thread),
            None,
        )
        if since is None:
            raise ValueError(f"Nothing follows {threads.label(thread)} now.")
        return await self.read_followed(thread, since, at_once=True)

    def read_ok(self, thread: str, whole: bool = True):
        """A followed PR was read: what failed before is over, its backoff too, and its task's
        card says so at once. `whole`: all of it (comments, reviews, CI); else only the PR
        itself, which clears only a failure to read the PR itself."""
        why = json.loads(self.store.get(f"ghwhy:{thread}") or "{}")
        if not whole and why.get("step") not in {None, "", "pull request"}:
            return
        failed = int(self.store.get(f"ghfail:{thread}") or 0)
        for key in ("ghfail", "gh404", "ghskip", "ghwhy"):
            self.store.delete(f"{key}:{thread}")
        moment = self.clock()
        if whole:
            self.store.set(f"ghread:{thread}", iso(moment))
            self.store.set(f"ghnext:{thread}", iso(moment + POLL_SECONDS))
        elif failed:
            self.store.set(f"ghnext:{thread}", iso(moment))  # Read whole at the next poll.
        if failed:
            self.host.read_health_changed(thread)

    def forget(self, thread: str):
        """What reading a PR kept (see READ_STATE), once nobody follows it: a PR followed again
        later starts afresh."""
        for prefix in READ_STATE:
            self.store.delete(f"{prefix}:{thread}")
        self.reading.pop(thread, None)
        if (lock := self.locks.get(thread)) and not lock.locked():
            del self.locks[thread]

    def forget_unfollowed(self, followed: set[str]):
        """Read state left of PRs nobody follows now (its worker closed, the task it was
        followed for gone): nothing reads them till they are followed again. (Before, following
        a Slack thread wrote some under its key too.)"""
        left = {
            key.removeprefix(f"{prefix}:")
            for prefix in READ_STATE
            for key in self.store.keys(f"{prefix}:")
        }
        for thread in (left | set(self.locks)) - followed:
            self.forget(thread)

    def why(self, thread: str) -> dict:
        """Why a followed PR could not be read last, as the owner is told it: {kind (one of
        KINDS), error (in words, with when it is tried again), step, at, retry_at (ISO, or None),
        detail (GitHub's own words)}; {} when its last read worked."""
        why = json.loads(self.store.get(f"ghwhy:{thread}") or "{}")
        if not why:
            return {}
        # Before: GitHub's words in "error", and kinds "access" and "other".
        detail = why["detail"] if "detail" in why else why.get("error") or ""
        kind = why.get("kind") if why.get("kind") in KINDS else failure_kind(detail)
        retry_at = why.get("retry_at") or self.store.get(f"ghnext:{thread}") or None
        return {
            "kind": kind,
            "error": why_words(kind, detail, retry_at, self.clock()),
            "step": why.get("step") or "",
            "at": why.get("at") or "",
            "retry_at": retry_at,
            "detail": detail,
        }

    async def poll_review_requests(self, settings, followed: set[str]) -> list[str]:
        """Review requests in the owner's repositories become triggers. What failed, said (one
        owner's search or one PR failing does not stop the others)."""
        # One search per owner: each runs as the account that sees that owner's repositories
        # (@me is that account).
        owners: dict[str, list[str]] = {}
        for repo in settings.github_repos:
            owners.setdefault(repo.split("/")[0], []).append(repo)
        requests = []
        errors = []
        for owner, repos in owners.items():
            arguments = [
                "search",
                "prs",
                "--review-requested=@me",
                "--state=open",
                "--limit=50",
                "--json=number,title,repository,url,author,isDraft",
                *(f"--repo={repo}" for repo in repos),
            ]
            try:
                requests += json.loads(await self.gh(*arguments))
            except Exception as exc:
                logger.warning("Could not search review requests in %s: %s", owner, exc)
                errors.append(f"Review requests in {owner}: {str(exc)[:200]}")
        for request in requests:
            repo = request["repository"]["nameWithOwner"].lower()
            if repo not in settings.github_repos or request.get("isDraft"):
                continue
            try:
                pull = await self.pull(repo, request["number"])
            except Exception as exc:
                logger.warning("Could not read %s#%s: %s", repo, request["number"], exc)
                continue
            if pull["state"] != "open":
                continue
            sha = pull["head"]["sha"]
            key = f"github:{repo}#{request['number']}@{sha}"
            if self.store.one("SELECT id FROM triggers WHERE key=?", (key,)):
                continue
            author = (request.get("author") or {}).get("login", "unknown")
            thread = f"github:{repo}#{request['number']}"
            if thread in followed and await self.host.on_thread(
                "github",
                thread,
                [key],
                f"New commits on {repo}#{request['number']} (head {sha[:12]}); the owner's "
                "review is requested again.",
            ):
                continue  # Whoever follows the PR handles the re-review, not the Router.
            await self.host.on_trigger(
                "github",
                [key],
                f"Review requested from the owner on {repo}#{request['number']}: "
                f"{request['title']}\nAuthor: {author}\nURL: {request['url']}\nHead: {sha[:12]}\n"
                f"Thread key: github:{repo}#{request['number']}",
            )
        return errors

    async def poll_pull(self, thread: str, since: float) -> bool:
        """New comments, reviews, CI results and the end of a followed PR go to its followers.
        False when it was let go (it ended)."""
        match = threads.GITHUB_PR.fullmatch(thread)
        if not match:
            raise ValueError(f"Not a pull request thread: {thread}")
        repo, number = match.groups()
        started = github_time(datetime.now(UTC).timestamp() - 60)
        after = self.store.get(f"ghsince:{thread}") or github_time(since)
        self.reading[thread] = "pull request"
        pull = await self.pull(repo, int(number))
        if title := " ".join((pull.get("title") or "").split())[:200]:
            self.store.set(f"prtitle:{repo.lower()}#{number}", title)  # Shown with its number.
        base = f"repos/{repo}"
        items: list[tuple[str, str]] = []
        replies: dict[str, tuple[str, str]] = {}
        # The owner's own comments and reviews are not news to the sessions following the PR:
        # those of the account they act as on this repository (what Chief of Staff posts as).
        # Another account signed in on this machine (a teammate's, a bot's) may be someone else.
        self.reading[thread] = "account"
        mine = {await self.acting_login(repo)} - {""}

        def theirs(item: dict) -> bool:
            return ((item.get("user") or {}).get("login") or "").lower() not in mine

        # Every page: GitHub lists oldest first, so on a busy PR the new ones are on the last.
        self.reading[thread] = "comments"
        for comment in pages(
            await self.gh(
                "api", f"{base}/issues/{number}/comments?since={after}&per_page=100", "--paginate"
            )
        ):
            if not theirs(comment):
                continue
            items.append(
                (
                    f"github:issuecomment:{comment['id']}",
                    f"{comment['user']['login']} commented: {body(comment)} "
                    f"({comment['html_url']})",
                )
            )
        self.reading[thread] = "review comments"
        for comment in pages(
            await self.gh(
                "api", f"{base}/pulls/{number}/comments?since={after}&per_page=100", "--paginate"
            )
        ):
            if not theirs(comment):
                continue
            line = comment.get("line") or comment.get("original_line") or ""
            key = f"github:reviewcomment:{comment['id']}"
            said = (
                f"{comment['user']['login']} on {comment.get('path')}:{line}: "
                f"{body(comment)} ({comment['html_url']})"
            )
            items.append((key, said))
            root = comment.get("in_reply_to_id")
            for answered in (f"github:reviewcomment:{root}", f"github:reviewthread:{root}"):
                if root and self.sent_by_us(answered):
                    # It answers a comment Chief of Staff posted (or a conversation it replied
                    # in): news to the task that wrote it.
                    replies[key] = (self.store.get(f"ghby:{answered}") or "", said)
                    break
        self.reading[thread] = "reviews"
        for review in pages(
            await self.gh("api", f"{base}/pulls/{number}/reviews?per_page=100", "--paginate")
        ):
            if review.get("state") == "PENDING" or (review.get("submitted_at") or "") < after:
                continue
            if not theirs(review):
                continue
            if review.get("state") == "COMMENTED" and not (review.get("body") or "").strip():
                # It only holds line comments (a reply to one is such a review): listed above.
                continue
            state = review["state"].lower().replace("_", " ")
            items.append(
                (
                    f"github:review:{review['id']}",
                    f"{review['user']['login']} {state}: {body(review)} ({review['html_url']})",
                )
            )
        sha = pull["head"]["sha"]
        last = self.store.get(f"ghhead:{thread}")
        # New commits (a re-requested review already said so: not twice).
        if (
            last
            and last != sha
            and not self.seen(f"github:{repo}#{number}@{sha}")
            and (line := await self.pushed(repo, last, sha, (pull.get("base") or {}).get("ref")))
        ):
            items.append((f"github:head:{repo}#{number}@{sha}", line))
        self.reading[thread] = "CI checks"
        ci = await self.ci_result(base, sha)
        if ci and not last and ci["at"] and ci["at"] < github_time(since):
            # Its first look, at CI that finished before it was followed: where it stands, not
            # news (a PR named in passing would bring its old failures).
            self.store.add_trigger("github", f"github:ci:{repo}#{number}@{sha}:{ci['outcome']}", "")
        elif ci:
            # Keyed by the outcome too: a check that failed and passed when run again on the
            # same commit is news.
            items.append(
                (
                    f"github:ci:{repo}#{number}@{sha}:{ci['outcome']}",
                    f"CI on {sha[:7]}: {ci['text']}",
                )
            )
        state = "merged" if pull.get("merged") else pull["state"]
        if state == "open" and pull.get("mergeable_state") == "dirty":
            # Once per head and base: its author has to merge or rebase (bug bash 9: nobody
            # was told, and its chips said "approved").
            onto = (pull.get("base") or {}).get("ref") or "its base"
            items.append(
                (
                    f"github:conflicts:{thread}@{sha}:{(pull.get('base') or {}).get('sha', '')}",
                    f"It has merge conflicts with {onto}: merge or rebase it and push.",
                )
            )
        kept = False
        if state != "open":
            ended = pull.get("merged_at") or pull.get("closed_at")
            when = datetime.fromisoformat(ended).timestamp() if ended else time.time()
            before = bool(ended) and when < since
            asked = float(self.store.get(f"ghkeep:{thread}") or 0)
            if before and (state == "merged" or not asked):
                # Already finished when followed (a PR cited in passing): just stop
                # following it.
                self.host.unfollow(thread)
                return False
            if state == "merged":
                items.append((f"github:state:{thread}:merged", "The PR was merged."))
            else:
                # Closed, not merged (while followed, or followed on request once closed): it
                # stays followed for a while, so its reopening (or a comment) is news.
                if time.time() - (asked if before else when) > KEEP_CLOSED_DAYS * 86400:
                    self.host.missing(
                        thread,
                        f"it stayed closed for {KEEP_CLOSED_DAYS} days after it was "
                        f"{'followed' if before else 'closed'} (a reopening after that is not "
                        "noticed)",
                    )
                    return False
                kept = True
                self.store.set(f"ghclosed:{thread}", ended or now())
                if not before:
                    gone = await self.branch_gone(pull)
                    items.append(
                        (
                            f"github:state:{thread}:closed:{ended}",
                            f"The PR was closed, not merged, and its branch {gone} was deleted: "
                            "it can be reopened only once the branch is pushed again."
                            if gone
                            else f"The PR was closed, not merged. It stays followed for "
                            f"{KEEP_CLOSED_DAYS} days, so a reopening reaches you.",
                        )
                    )
        elif closed := self.store.get(f"ghclosed:{thread}"):
            items.append((f"github:state:{thread}:reopened:{closed}", "The PR was reopened."))
            self.store.delete(f"ghclosed:{thread}")
        fresh = [(key, line) for key, line in items if not self.seen(key)]
        if fresh:
            lines = "\n".join(f"- {line}" for _, line in fresh)
            self.reading[thread] = "delivery"
            await self.host.on_thread(
                "github",
                thread,
                [key for key, _ in fresh],
                f"New activity on {repo}#{number}, a PR you follow:\n{lines}",
                lines,
                replies={key: replies[key] for key, _ in fresh if key in replies},
            )
        self.store.set(f"ghsince:{thread}", started)
        self.store.set(f"ghhead:{thread}", sha)
        if state != "open" and not kept:
            self.host.unfollow(thread, finished=True, merged=state == "merged")
            return False
        return True

    async def branch_gone(self, pull: dict) -> str:
        """The name of a closed PR's head branch when it no longer exists (deleted, or its fork
        gone), else ""."""
        head = pull.get("head") or {}
        if "repo" in head and head["repo"] is None:
            return head.get("ref") or "its branch"  # GitHub's null: the fork is gone.
        ref, repo = head.get("ref") or "", (head.get("repo") or {}).get("full_name") or ""
        if not (ref and repo):
            return ""
        try:
            await self.gh("api", f"repos/{repo}/git/ref/heads/{ref}")
        except RuntimeError as exc:
            return ref if NOT_FOUND.search(str(exc)) else ""
        return ""

    async def pushed(self, repo: str, old: str, new: str, base: str | None = None) -> str:
        """What was pushed between two heads, by whom; "" when only the account Chief of Staff
        works as pushed (a worker's own commits are not news to it). Commits already on the PR's
        base branch are not the PR's: a rebase onto a base others moved, or a merge of it,
        brings theirs in."""
        rewritten = f"The branch was rewritten (force-pushed): the head is now {new[:7]}."
        try:
            compare = json.loads(await self.gh("api", f"repos/{repo}/compare/{old}...{new}"))
        except RuntimeError:
            return rewritten
        commits = compare.get("commits") or []
        if base and commits:
            with suppress(RuntimeError, ValueError):
                path = f"repos/{repo}/compare/{quote(base, safe='/')}...{new}"
                own = json.loads(await self.gh("api", path))
                listed = own.get("commits") or []
                if len(listed) >= (own.get("total_commits") or 0):  # Not cut at 250.
                    shas = {commit.get("sha") for commit in listed}
                    commits = [commit for commit in commits if commit.get("sha") in shas]

        def login(commit: dict) -> str:
            return ((commit.get("author") or {}).get("login") or "").lower()

        acting = await self.acting_login(repo)
        # Its workers' own commits: by the account it works as, or committed in one of their
        # checkouts (a git email GitHub does not know shows no login; `git commit -q` says
        # nothing, so their checkouts' logs are asked).
        made = worker_commits(self.store)

        def ours(commit: dict) -> bool:
            sha = commit.get("sha") or ""
            return login(commit) == acting or any(sha.startswith(short) for short in made)

        diverged = compare.get("status") == "diverged"
        unknown = [c["sha"] for c in commits if c.get("sha") and not ours(c)]
        if diverged and not any(new.startswith(short) for short in made):
            unknown.append(new)
        if unknown:
            made |= await made_locally(worker_folders(self.store), unknown)
        if commits and all(ours(commit) for commit in commits):
            return ""
        if diverged and any(new.startswith(short) for short in made):
            return ""  # Rewritten in a worker's checkout (rebased, amended): its own head.
        if not commits or diverged or compare.get("status") == "behind":
            return rewritten
        shown = "; ".join(
            f"{login(c) or c['commit']['author']['name']}: "
            f"{(c['commit']['message'].splitlines() or [''])[0][:120]}"
            for c in commits[-5:]
        )
        more = f" (the last 5 of {len(commits)})" if len(commits) > 5 else ""
        return f"{len(commits)} new commit(s), the head is now {new[:7]}{more}: {shown}"

    async def ci_result(self, base: str, sha: str) -> dict | None:
        """Once every check on the commit is finished (Actions check runs and commit statuses,
        each by its latest run): what they came to; None while any is still running."""
        runs = json.loads(
            await self.gh("api", f"{base}/commits/{sha}/check-runs?per_page=100")
        ).get("check_runs", [])
        try:
            statuses = json.loads(await self.gh("api", f"{base}/commits/{sha}/status")).get(
                "statuses", []
            )
        except RuntimeError:
            statuses = []
        latest: dict[str, tuple[str, str]] = {}  # Name -> (when, outcome).
        ended = ""
        waiting_for: list[str] = []
        for run in runs:
            if run["status"] != "completed" and APPROVAL.search(run["name"]):
                waiting_for.append(run["name"])  # A person's approval, not CI.
                continue
            if run["status"] != "completed":
                return None
            when = run.get("completed_at") or ""
            ended = max(ended, when)
            if when >= latest.get(run["name"], ("", ""))[0]:
                latest[run["name"]] = (when, run.get("conclusion") or "")
        for status in statuses:  # GitHub lists each context's latest first.
            if status["context"] not in latest:
                if status.get("state") == "pending" and APPROVAL.search(status["context"]):
                    waiting_for.append(status["context"])
                    continue
                if status.get("state") == "pending":
                    return None
                ended = max(ended, status.get("updated_at") or "")
                latest[status["context"]] = ("", status.get("state") or "")
        if not latest:
            return None
        failed = sorted(
            name for name, (_, outcome) in latest.items() if outcome in FAILED | {"error"}
        )
        if failed:
            return {
                "outcome": "failed-" + ",".join(failed),
                "text": f"failed: {', '.join(failed)}",
                "at": ended,
            }
        if waiting_for:
            names = ", ".join(sorted(set(waiting_for)))
            return {
                "outcome": "passed-awaiting",
                "text": f"all {len(latest)} CI checks passed; waiting for approval: {names}",
                "at": ended,
            }
        return {"outcome": "passed", "text": f"all {len(latest)} checks passed", "at": ended}

    async def files(self, repo: str, number: int) -> list[dict]:
        """The PR's changed files, each with its patch."""
        return pages(
            await self.gh(
                "api", f"repos/{repo}/pulls/{int(number)}/files?per_page=100", "--paginate"
            )
        )

    async def diff_lines(self, repo: str, number: int) -> dict:
        return diff_lines(await self.files(repo, number))

    async def heads(self, repo: str, number: int) -> set[str]:
        """The PR's head as GitHub has it, and its branch's head: the PR lags a push to its
        branch by a second or two, so a review checked against the PR alone could land on the
        commit before. The branch is not read when it cannot be (a deleted fork)."""
        pull = await self.pull(repo, number)
        if pull["state"] != "open":
            raise ValueError(f"{repo}#{number} is not open.")
        head = pull["head"]
        found = {head["sha"]}
        source = (head.get("repo") or {}).get("full_name") or ""
        if source and head.get("ref"):
            path = f"repos/{source}/git/ref/heads/{quote(head['ref'], safe='/')}"
            try:
                ref = json.loads(await self.gh("api", path))
            except RuntimeError as exc:
                if not NO_ACCESS.search(str(exc)):
                    raise
            else:
                found.add(((ref or {}).get("object") or {}).get("sha") or "")
        return found

    async def find_review(self, target: dict, body: str, since: str = "") -> str:
        """The URL of a published review with this body on the target PR, or "". One without a
        body (a bare approval, line comments only) is told by who submitted it, on which
        commit, with which verdict, and when (`since`, ISO: when it was drafted)."""
        reviews = pages(
            await self.gh(
                "api", f"repos/{target['repo']}/pulls/{int(target['number'])}/reviews", "--paginate"
            )
        )
        if body.strip():
            for review in reviews:
                if unmarked(review.get("body")) == body.strip():
                    return review.get("html_url") or "Review published."
            return ""
        login = await self.acting_login(target["repo"])
        event = REVIEW_EVENTS.get(target.get("verdict", ""), "COMMENT")
        state = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}.get(
            event, "COMMENTED"
        )
        for review in reviews:
            author = ((review.get("user") or {}).get("login") or "").lower()
            if (
                (not login or author == login)
                and review.get("state") == state
                and review.get("commit_id") == target.get("head_sha")
                and not (review.get("body") or "").strip()
                and (not since or (review.get("submitted_at") or "") >= since[:19])
            ):
                return review.get("html_url") or "Review published."
        return ""

    async def review_comment(self, repo: str, number: int, comment_id: int) -> dict:
        """A review comment on a PR, as the owner sees it next to a drafted reply."""
        comment = json.loads(await self.gh("api", f"repos/{repo}/pulls/comments/{int(comment_id)}"))
        if not str(comment.get("pull_request_url", "")).endswith(f"/pulls/{int(number)}"):
            raise ValueError(f"Comment {comment_id} is not on {repo}#{number}.")
        return {
            "author": (comment.get("user") or {}).get("login", ""),
            "body": body(comment)[:1500],
            "path": comment.get("path") or "",
            "line": comment.get("line") or comment.get("original_line") or "",
            "url": comment.get("html_url") or "",
        }

    async def conversation_comment(self, repo: str, number: int, comment_id: int) -> dict:
        """A comment in a PR's conversation (not on a line): a top-level comment, or a review's
        summary, as the owner sees it next to a drafted answer."""
        try:
            comment = json.loads(
                await self.gh("api", f"repos/{repo}/issues/comments/{int(comment_id)}")
            )
            if not str(comment.get("issue_url", "")).endswith(f"/issues/{int(number)}"):
                raise ValueError(f"Comment {comment_id} is not on {repo}#{number}.")
        except RuntimeError:
            # Not a top-level comment: a review's summary, by the review's id.
            comment = json.loads(
                await self.gh("api", f"repos/{repo}/pulls/{int(number)}/reviews/{int(comment_id)}")
            )
        return {
            "author": (comment.get("user") or {}).get("login", ""),
            "body": body(comment)[:1500],
            "url": comment.get("html_url") or "",
        }

    async def publish_conversation_reply(self, target: dict, text: str) -> str:
        repo, number = target["repo"], int(target["number"])
        self.posting(target, text)
        posted = json.loads(
            await self.gh(
                "api",
                f"repos/{repo}/issues/{number}/comments",
                "--method=POST",
                "--input=-",
                payload={"body": text},
            )
        )
        if posted.get("id"):
            # Our own reply must not come back as activity on the followed PR.
            self.posted(f"github:issuecomment:{posted['id']}", text, target)
        return posted.get("html_url", "Reply published.")

    async def find_conversation_reply(self, target: dict, text: str) -> str:
        """The URL of a published comment with this text on the target PR, or ""."""
        path = f"repos/{target['repo']}/issues/{int(target['number'])}/comments"
        for comment in pages(await self.gh("api", path, "--paginate")):
            if unmarked(comment.get("body")) == text.strip():
                return comment.get("html_url") or "Reply published."
        return ""

    async def graphql(self, repo: str, query: str, **variables: str) -> dict:
        """A GraphQL call as the account that reaches the repository (the one that posts
        there), its variables passed as strings."""
        owner = repo.split("/", 1)[0].lower()
        remembered = self.store.get(f"ghaccount:{owner}") or ""
        arguments = ["api", "graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            arguments += ["-f", f"{name}={value}"]
        return json.loads(await self.as_account(owner, remembered, remembered, arguments, None))

    async def merge(self, target: dict) -> str:
        """Merge a PR, its head as it was shown (GitHub refuses if it moved), the way the
        repository allows: squash, else a merge commit, else rebase."""
        repo = json.loads(await self.gh("api", f"repos/{target['repo']}"))
        method = next(
            (
                flag
                for flag, allowed in (
                    ("--squash", repo.get("allow_squash_merge", True)),
                    ("--merge", repo.get("allow_merge_commit", True)),
                    ("--rebase", repo.get("allow_rebase_merge", True)),
                )
                if allowed
            ),
            "--merge",
        )
        await self.gh(
            "pr", "merge", f"https://github.com/{target['repo']}/pull/{int(target['number'])}",
            method, "--match-head-commit", target["head"],
        )  # fmt: skip
        word = {
            "--squash": "squashed and merged",
            "--merge": "merged",
            "--rebase": "rebased and merged",
        }
        return f"{target['label']} {word[method]}."

    async def resolve_thread(self, target: dict) -> str:
        """Resolve the review thread the replied-to comment is in: "" when done, else why not."""
        owner, name = target["repo"].split("/", 1)
        query = (
            "query($owner:String!,$name:String!){repository(owner:$owner,name:$name)"
            f"{{pullRequest(number:{int(target['number'])}){{reviewThreads(first:100){{nodes"
            "{id isResolved comments(first:100){nodes{databaseId}}}}}}}"
        )
        found = await self.graphql(target["repo"], query, owner=owner, name=name)
        if found.get("errors"):
            return str(found["errors"][0].get("message", "GitHub refused"))[:200]
        pull = ((found.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
        wanted = int(target["in_reply_to"])
        for thread in (pull.get("reviewThreads") or {}).get("nodes") or []:
            ids = {c.get("databaseId") for c in (thread.get("comments") or {}).get("nodes") or []}
            if wanted not in ids:
                continue
            if thread.get("isResolved"):
                return ""
            mutation = (
                "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"
            )
            done = await self.graphql(target["repo"], mutation, id=thread["id"])
            if done.get("errors"):
                return str(done["errors"][0].get("message", "GitHub refused"))[:200]
            return ""
        return "its conversation was not found on the PR"

    async def owner_posted(self, target: dict, text: str, since: str = "") -> str:
        """The URL of what the owner posted themself on the target PR, since the draft was
        written, that is this text (copied from a draft, perhaps edited a little), or "". Not
        what Chief of Staff posted (also as the owner), nor a reply in another thread."""
        repo, number = target["repo"], int(target["number"])
        mine = await self.logins()
        candidates: list[tuple[str, dict]] = []
        in_reply_to = target.get("in_reply_to")
        if in_reply_to or target.get("anywhere"):
            path = f"repos/{repo}/pulls/{number}/comments"
            for item in pages(await self.gh("api", path, "--paginate")):
                if in_reply_to and item.get("in_reply_to_id") != in_reply_to:
                    continue
                candidates.append((f"github:reviewcomment:{item.get('id')}", item))
        if not in_reply_to:
            path = f"repos/{repo}/issues/{number}/comments"
            for item in pages(await self.gh("api", path, "--paginate")):
                candidates.append((f"github:issuecomment:{item.get('id')}", item))
            path = f"repos/{repo}/pulls/{number}/reviews"
            for item in pages(await self.gh("api", path, "--paginate")):
                candidates.append((f"github:review:{item.get('id')}", item))
        # GitHub's times are whole seconds: one posted the second the draft was written counts.
        start = datetime.fromisoformat(since).replace(microsecond=0) if since else None
        for key, item in candidates:
            if ((item.get("user") or {}).get("login") or "").lower() not in mine:
                continue
            when = item.get("created_at") or item.get("submitted_at") or ""
            if start and when and datetime.fromisoformat(when) < start:
                continue  # Before the draft was written: another reply.
            if self.sent_by_us(key):
                continue  # Chief of Staff posted it (as the owner): not theirs by hand.
            if same_text(item.get("body") or "", text):
                return item.get("html_url") or "Posted."
        return ""

    async def find_reply(self, target: dict, text: str) -> str:
        """The URL of a published reply with this text to the target comment, or ""."""
        path = f"repos/{target['repo']}/pulls/{int(target['number'])}/comments"
        comments = pages(await self.gh("api", path, "--paginate"))
        for comment in comments:
            if comment.get("in_reply_to_id") == target["in_reply_to"] and (
                unmarked(comment.get("body")) == text.strip()
            ):
                return comment.get("html_url") or "Reply published."
        return ""

    async def publish_reply(self, target: dict, text: str) -> str:
        repo, number = target["repo"], int(target["number"])
        self.posting(target, text)
        reply = json.loads(
            await self.gh(
                "api",
                f"repos/{repo}/pulls/{number}/comments/{int(target['in_reply_to'])}/replies",
                "--method=POST",
                "--input=-",
                payload={"body": text},
            )
        )
        if reply.get("id"):
            # Our own reply must not come back as activity on the followed PR.
            self.posted(f"github:reviewcomment:{reply['id']}", text, target)
            # Later replies in that conversation name its first comment (GitHub's threads
            # are flat): they answer this one too.
            root = reply.get("in_reply_to_id") or target["in_reply_to"]
            self.posted(f"github:reviewthread:{root}", text, target)
        return reply.get("html_url", "Reply published.")

    async def publish_review(self, target: dict, body: str) -> str:
        settings = self.store.settings()
        repo = target["repo"]
        if target.get("via", "connected") == "connected" and repo not in settings.github_repos:
            raise ValueError("This repository is no longer connected; nothing was published.")
        if target.get("in_reply_to"):
            return await self.publish_reply(target, body)
        if target.get("reply_to_comment"):
            return await self.publish_conversation_reply(target, body)
        if await self.heads(repo, target["number"]) != {target["head_sha"]}:
            raise ValueError("The PR changed after the draft was written; nothing was published.")
        event = REVIEW_EVENTS.get(target.get("verdict", ""), "COMMENT")
        payload = {"event": event, "commit_id": target["head_sha"], "body": body}
        if comments := target.get("comments"):
            payload["comments"] = [
                {
                    "path": comment["path"],
                    "line": comment["line"],
                    "side": comment.get("side", "RIGHT"),
                    "body": comment["body"],
                }
                | (
                    {
                        "start_line": comment["start_line"],
                        "start_side": comment.get("side", "RIGHT"),
                    }
                    if comment.get("start_line")
                    else {}
                )
                for comment in comments
            ]
        path = f"repos/{repo}/pulls/{int(target['number'])}/reviews"
        self.posting(target, body, *(comment["body"] for comment in comments or []))
        note = ""
        try:
            reply = await self.gh("api", path, "--method=POST", "--input=-", payload=payload)
        except RuntimeError as exc:
            # GitHub refuses (422, nothing published) to approve or request changes on one's
            # own pull request: a comment is what it allows.
            if event == "COMMENT" or "422" not in str(exc):
                raise
            payload["event"] = "COMMENT"
            reply = await self.gh("api", path, "--method=POST", "--input=-", payload=payload)
            note = " (posted as a comment: GitHub allows no approval on your own PR)"
        review = json.loads(reply)
        if review.get("id"):
            # Our own review, and its line comments, must not come back as activity on the
            # followed PR.
            self.posted(f"github:review:{review['id']}", body, target)
            if target.get("comments"):
                try:
                    posted = pages(
                        await self.gh(
                            "api",
                            f"repos/{repo}/pulls/{int(target['number'])}/reviews/"
                            f"{review['id']}/comments",
                            "--paginate",
                        )
                    )
                except Exception:
                    logger.info("Could not list the review's comments.", exc_info=True)
                    posted = []
                for comment in posted:
                    self.posted(
                        f"github:reviewcomment:{comment['id']}", comment.get("body", ""), target
                    )
        return review.get("html_url", "Review published.") + note
