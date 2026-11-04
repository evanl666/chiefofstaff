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
from ..store import Store, now
from . import SourceHost

MAX_FOLLOW_FAILURES = 5
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

    async def poll_once(self):
        settings = self.store.settings()
        self.status["enabled"] = settings.github_enabled
        if settings.paused:
            return
        followed = self.host.followed("github:")
        if not (settings.github_enabled or followed):
            self.status.update(connected=False, error="")
            return
        if settings.github_enabled:
            await self.poll_review_requests(settings, {sub["thread"] for sub in followed})
        for sub in followed:
            thread = sub["thread"]
            failures = int(self.store.get(f"ghfail:{thread}") or 0)
            # After repeated failures retry only every tenth poll; never drop or close anything.
            if failures >= MAX_FOLLOW_FAILURES and failures % 10:
                self.store.set(f"ghfail:{thread}", str(failures + 1))
                continue
            try:
                await self.poll_pull(thread, sub["since"])
            except Exception as exc:
                # One inaccessible PR must not stop the others.
                self.store.set(f"ghfail:{thread}", str(failures + 1))
                logger.warning("Could not read followed %s (%s): %s", thread, failures + 1, exc)
                continue
            self.store.set(f"ghfail:{thread}", "0")
        self.status.update(connected=True, error="", last_checked=now())

    async def poll_review_requests(self, settings, followed: set[str]):
        # One search per owner: each runs as the account that sees that owner's repositories
        # (@me is that account).
        owners: dict[str, list[str]] = {}
        for repo in settings.github_repos:
            owners.setdefault(repo.split("/")[0], []).append(repo)
        requests = []
        for repos in owners.values():
            arguments = [
                "search",
                "prs",
                "--review-requested=@me",
                "--state=open",
                "--limit=50",
                "--json=number,title,repository,url,author,isDraft",
                *(f"--repo={repo}" for repo in repos),
            ]
            requests += json.loads(await self.gh(*arguments))
        for request in requests:
            repo = request["repository"]["nameWithOwner"].lower()
            if repo not in settings.github_repos or request.get("isDraft"):
                continue
            pull = await self.pull(repo, request["number"])
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

    async def poll_pull(self, thread: str, since: float):
        """New comments, reviews, CI results and the end of a followed PR go to its followers."""
        match = threads.GITHUB_PR.fullmatch(thread)
        if not match:
            raise ValueError(f"Not a pull request thread: {thread}")
        repo, number = match.groups()
        started = github_time(datetime.now(UTC).timestamp() - 60)
        after = self.store.get(f"ghsince:{thread}") or github_time(since)
        pull = await self.pull(repo, int(number))
        base = f"repos/{repo}"
        items: list[tuple[str, str]] = []
        for comment in json.loads(
            await self.gh("api", f"{base}/issues/{number}/comments?since={after}&per_page=50")
        ):
            items.append(
                (
                    f"github:issuecomment:{comment['id']}",
                    f"{comment['user']['login']} commented: {body(comment)} "
                    f"({comment['html_url']})",
                )
            )
        for comment in json.loads(
            await self.gh("api", f"{base}/pulls/{number}/comments?since={after}&per_page=50")
        ):
            line = comment.get("line") or comment.get("original_line") or ""
            items.append(
                (
                    f"github:reviewcomment:{comment['id']}",
                    f"{comment['user']['login']} on {comment.get('path')}:{line}: "
                    f"{body(comment)} ({comment['html_url']})",
                )
            )
        for review in json.loads(
            await self.gh("api", f"{base}/pulls/{number}/reviews?per_page=100")
        ):
            if review.get("state") == "PENDING" or (review.get("submitted_at") or "") < after:
                continue
            state = review["state"].lower().replace("_", " ")
            items.append(
                (
                    f"github:review:{review['id']}",
                    f"{review['user']['login']} {state}: {body(review)} ({review['html_url']})",
                )
            )
        sha = pull["head"]["sha"]
        runs = json.loads(
            await self.gh("api", f"{base}/commits/{sha}/check-runs?per_page=100")
        ).get("check_runs", [])
        if runs and all(run["status"] == "completed" for run in runs):
            failed = [run["name"] for run in runs if run.get("conclusion") in FAILED]
            result = f"failed: {', '.join(failed)}" if failed else f"all {len(runs)} checks passed"
            items.append((f"github:ci:{repo}#{number}@{sha}", f"CI on {sha[:7]}: {result}"))
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
        if state != "open":
            self.host.unfollow(thread, finished=True, merged=state == "merged")

    async def diff_lines(self, repo: str, number: int) -> dict:
        files = await self.gh(
            "api", f"repos/{repo}/pulls/{int(number)}/files?per_page=100", "--paginate"
        )
        return diff_lines(pages(files))

    async def head(self, repo: str, number: int) -> str:
        pull = await self.pull(repo, number)
        if pull["state"] != "open":
            raise ValueError(f"{repo}#{number} is not open.")
        return pull["head"]["sha"]

    async def find_review(self, target: dict, body: str) -> str:
        """The URL of a published review with this body on the target PR, or ""."""
        reviews = pages(
            await self.gh(
                "api", f"repos/{target['repo']}/pulls/{int(target['number'])}/reviews", "--paginate"
            )
        )
        for review in reviews:
            if (review.get("body") or "").strip() == body.strip():
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
        if await self.head(repo, target["number"]) != target["head_sha"]:
            raise ValueError("The PR changed after the draft was written; nothing was published.")
        payload = {"event": "COMMENT", "commit_id": target["head_sha"], "body": body}
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
        reply = await self.gh(
            "api",
            f"repos/{repo}/pulls/{int(target['number'])}/reviews",
            "--method=POST",
            "--input=-",
            payload=payload,
        )
        review = json.loads(reply)
        if review.get("id"):
            # Our own review must not come back as activity on the followed PR.
            self.store.add_trigger("github-sent", f"github:review:{review['id']}", body)
        return review.get("html_url", "Review published.")
