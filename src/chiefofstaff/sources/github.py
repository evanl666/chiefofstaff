"""GitHub review requests on explicitly connected repositories, through the gh CLI."""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from contextlib import suppress
from datetime import UTC, datetime

from .. import threads
from ..receipts import APPROVAL
from ..store import Store, now
from . import SourceHost

MAX_FOLLOW_FAILURES = 5
# Reads of a followed PR that GitHub says does not exist before it is let go.
MISSING_TRIES = 3
# GitHub's own answers only (case and all): "GitHub CLI not found" (gh missing from PATH) is
# this computer's problem, not a PR that is gone.
NOT_FOUND = re.compile(
    r"HTTP 404\b|\bgh: Not Found\b|Could not resolve to a (?:Repository|PullRequest)\b"
)
FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}


def github_time(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def body(item: dict) -> str:
    return " ".join((item.get("body") or "").split())[:1500] or "(no text)"


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
                await asyncio.wait_for(self.changed.wait(), timeout=120)

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
        assert failure
        raise failure

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

    async def poll_once(self):
        settings = self.store.settings()
        self.status["enabled"] = settings.github_enabled
        if settings.paused:
            return
        followed = self.host.followed("github:")
        if not (settings.github_enabled or followed):
            self.status.update(connected=False, error="")
            return
        errors = []
        if settings.github_enabled:
            # Its failure (one owner's search, say) must not stop the followed PRs.
            errors = await self.poll_review_requests(settings, {sub["thread"] for sub in followed})
        for sub in followed:
            thread = sub["thread"]
            # Failures in a row (ghfail) count real reads only; after a few, it is read only
            # every tenth poll (ghskip counts those passed over).
            failures = int(self.store.get(f"ghfail:{thread}") or 0)
            if failures >= MAX_FOLLOW_FAILURES:
                skipped = int(self.store.get(f"ghskip:{thread}") or 0) + 1
                if skipped < 10:
                    self.store.set(f"ghskip:{thread}", str(skipped))
                    continue
                self.store.set(f"ghskip:{thread}", "0")
            try:
                await self.poll_pull(thread, sub["since"])
            except Exception as exc:
                # One inaccessible PR must not stop the others.
                self.store.set(f"ghfail:{thread}", str(failures + 1))
                logger.warning("Could not read followed %s (%s): %s", thread, failures + 1, exc)
                missing = (
                    int(self.store.get(f"gh404:{thread}") or 0) + 1
                    if NOT_FOUND.search(str(exc))
                    else 0
                )
                self.store.set(f"gh404:{thread}", str(missing))
                if missing >= MISSING_TRIES:
                    # No such PR (an example in a report, a typo), or no account sees it:
                    # following it can never bring news.
                    self.host.missing(
                        thread,
                        "GitHub has no such pull request, or none of your accounts can see it",
                    )
                continue
            for key in ("ghfail", "gh404", "ghskip"):
                self.store.set(f"{key}:{thread}", "0")
        self.status.update(connected=True, error="; ".join(errors)[:500], last_checked=now())

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

    async def poll_pull(self, thread: str, since: float):
        """New comments, reviews, CI results and the end of a followed PR go to its followers."""
        match = threads.GITHUB_PR.fullmatch(thread)
        if not match:
            raise ValueError(f"Not a pull request thread: {thread}")
        repo, number = match.groups()
        started = github_time(datetime.now(UTC).timestamp() - 60)
        after = self.store.get(f"ghsince:{thread}") or github_time(since)
        pull = await self.pull(repo, int(number))
        if title := " ".join((pull.get("title") or "").split())[:200]:
            self.store.set(f"prtitle:{repo.lower()}#{number}", title)  # Shown with its number.
        base = f"repos/{repo}"
        items: list[tuple[str, str]] = []
        # Every page: GitHub lists oldest first, so on a busy PR the new ones are on the last.
        for comment in pages(
            await self.gh(
                "api", f"{base}/issues/{number}/comments?since={after}&per_page=100", "--paginate"
            )
        ):
            items.append(
                (
                    f"github:issuecomment:{comment['id']}",
                    f"{comment['user']['login']} commented: {body(comment)} "
                    f"({comment['html_url']})",
                )
            )
        for comment in pages(
            await self.gh(
                "api", f"{base}/pulls/{number}/comments?since={after}&per_page=100", "--paginate"
            )
        ):
            line = comment.get("line") or comment.get("original_line") or ""
            items.append(
                (
                    f"github:reviewcomment:{comment['id']}",
                    f"{comment['user']['login']} on {comment.get('path')}:{line}: "
                    f"{body(comment)} ({comment['html_url']})",
                )
            )
        for review in pages(
            await self.gh("api", f"{base}/pulls/{number}/reviews?per_page=100", "--paginate")
        ):
            if review.get("state") == "PENDING" or (review.get("submitted_at") or "") < after:
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
            and (line := await self.pushed(repo, last, sha))
        ):
            items.append((f"github:head:{repo}#{number}@{sha}", line))
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
        if state != "open":
            ended = pull.get("merged_at") or pull.get("closed_at")
            if ended and datetime.fromisoformat(ended.replace("Z", "+00:00")).timestamp() < since:
                # Already finished when followed (a PR cited in passing): just stop following it.
                self.host.unfollow(thread)
                return
            items.append((f"github:state:{thread}:{state}", f"The PR was {state}."))
        fresh = [(key, line) for key, line in items if not self.seen(key)]
        if fresh:
            lines = "\n".join(f"- {line}" for _, line in fresh)
            await self.host.on_thread(
                "github",
                thread,
                [key for key, _ in fresh],
                f"New activity on {repo}#{number}, a PR you follow:\n{lines}",
                lines,
            )
        self.store.set(f"ghsince:{thread}", started)
        self.store.set(f"ghhead:{thread}", sha)
        if state != "open":
            self.host.unfollow(thread, finished=True, merged=state == "merged")

    async def pushed(self, repo: str, old: str, new: str) -> str:
        """What was pushed between two heads, by whom; "" when only the account Chief of Staff
        works as pushed (a worker's own commits are not news to it)."""
        try:
            compare = json.loads(await self.gh("api", f"repos/{repo}/compare/{old}...{new}"))
        except RuntimeError:
            return f"The branch was rewritten (force-pushed): the head is now {new[:7]}."
        commits = compare.get("commits") or []

        def login(commit: dict) -> str:
            return ((commit.get("author") or {}).get("login") or "").lower()

        acting = await self.acting_login(repo)
        if commits and all(login(commit) == acting for commit in commits):
            return ""
        if not commits or compare.get("status") in {"diverged", "behind"}:
            return f"The branch was rewritten (force-pushed): the head is now {new[:7]}."
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

    async def head(self, repo: str, number: int) -> str:
        pull = await self.pull(repo, number)
        if pull["state"] != "open":
            raise ValueError(f"{repo}#{number} is not open.")
        return pull["head"]["sha"]

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
                if (review.get("body") or "").strip() == body.strip():
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
            self.store.add_trigger("github-sent", f"github:issuecomment:{posted['id']}", text)
        return posted.get("html_url", "Reply published.")

    async def find_conversation_reply(self, target: dict, text: str) -> str:
        """The URL of a published comment with this text on the target PR, or ""."""
        path = f"repos/{target['repo']}/issues/{int(target['number'])}/comments"
        for comment in pages(await self.gh("api", path, "--paginate")):
            if (comment.get("body") or "").strip() == text.strip():
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
        start = datetime.fromisoformat(since) if since else None
        for key, item in candidates:
            if ((item.get("user") or {}).get("login") or "").lower() not in mine:
                continue
            when = item.get("created_at") or item.get("submitted_at") or ""
            if start and when and datetime.fromisoformat(when.replace("Z", "+00:00")) < start:
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
                (comment.get("body") or "").strip() == text.strip()
            ):
                return comment.get("html_url") or "Reply published."
        return ""

    async def publish_reply(self, target: dict, text: str) -> str:
        repo, number = target["repo"], int(target["number"])
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
            self.store.add_trigger("github-sent", f"github:reviewcomment:{reply['id']}", text)
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
        if await self.head(repo, target["number"]) != target["head_sha"]:
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
            self.store.add_trigger("github-sent", f"github:review:{review['id']}", body)
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
                    self.store.add_trigger(
                        "github-sent",
                        f"github:reviewcomment:{comment['id']}",
                        comment.get("body", ""),
                    )
        return review.get("html_url", "Review published.") + note
