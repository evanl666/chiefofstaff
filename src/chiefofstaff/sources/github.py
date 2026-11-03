"""GitHub review requests on explicitly connected repositories, through the gh CLI."""

import asyncio
import json
import logging
import os
import shutil
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


async def gh(*arguments: str, payload: dict | None = None) -> str:
    executable = shutil.which("gh")
    if not executable:
        raise RuntimeError(
            "GitHub CLI not found. Install gh and sign in before connecting reviews."
        )
    env = {key: value for key, value in os.environ.items() if not key.startswith("CHIEFOFSTAFF_")}
    env.update(GH_PROMPT_DISABLED="1", GH_PAGER="cat", NO_COLOR="1")
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


class GitHubSource:
    name = "github"

    def __init__(self, store: Store, host: SourceHost, *, gh_command=gh):
        self.store = store
        self.host = host
        self.gh = gh_command
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
        arguments = [
            "search",
            "prs",
            "--review-requested=@me",
            "--state=open",
            "--limit=50",
            "--json=number,title,repository,url,author,isDraft",
        ]
        arguments += [f"--repo={repo}" for repo in settings.github_repos]
        for request in json.loads(await self.gh(*arguments)):
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

    async def head(self, repo: str, number: int) -> str:
        pull = await self.pull(repo, number)
        if pull["state"] != "open":
            raise ValueError(f"{repo}#{number} is not open.")
        return pull["head"]["sha"]

    async def find_review(self, target: dict, body: str) -> str:
        """The URL of a published review with this body on the target PR, or ""."""
        reviews = json.loads(
            await self.gh(
                "api", f"repos/{target['repo']}/pulls/{int(target['number'])}/reviews", "--paginate"
            )
            or "[]"
        )
        for review in reviews:
            if (review.get("body") or "").strip() == body.strip():
                return review.get("html_url") or "Review published."
        return ""

    async def publish_review(self, target: dict, body: str) -> str:
        settings = self.store.settings()
        repo = target["repo"]
        if repo not in settings.github_repos:
            raise ValueError("This repository is no longer connected; nothing was published.")
        if await self.head(repo, target["number"]) != target["head_sha"]:
            raise ValueError("The PR changed after the draft was written; nothing was published.")
        reply = await self.gh(
            "api",
            f"repos/{repo}/pulls/{int(target['number'])}/reviews",
            "--method=POST",
            "--input=-",
            payload={"event": "COMMENT", "commit_id": target["head_sha"], "body": body},
        )
        review = json.loads(reply)
        if review.get("id"):
            # Our own review must not come back as activity on the followed PR.
            self.store.add_trigger("github-sent", f"github:review:{review['id']}", body)
        return review.get("html_url", "Review published.")
