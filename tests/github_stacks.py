"""GitHub's stacked PRs as gh shows them to Chief of Staff, recorded on 2026-11-23 from the live
test repository (a stack of three PRs merged with GitHub's asynchronous merge; names, numbers and
SHAs changed): what gh's merge says of a PR in a stack, a PR's `stack`, a stack's detail, and
the asynchronous merge's answers (202 taken, 409 taken already, 400 refused, its results, 404 for
one GitHub no longer has). `StackedGitHub` answers a merge as GitHub did; anything else goes to
the test's own gh."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from chiefofstaff.sources.github import GitHubError

# What `gh pr merge` printed for a PR in a stack, at its bottom too (exit 1).
REFUSED = (
    "GitHub request failed: GraphQL: This pull request is part of a stack and must be merged "
    "using the asynchronous merge REST API. For more information, see "
    "https://docs.github.com/rest/pulls/pulls#merge-a-pull-request-asynchronously "
    "(mergePullRequest)\n"
)
UUID = "4174d398-3e6c-4a28-bb8f-94b6c98e7b76"
MERGE_SHA = "550221abe95a533b50c46bd5879376748cff23dc"


def taken(head: str, message: str = "Merge request enqueued.", method: str = "squash") -> dict:
    """202, and 409 (message: "A merge request already exists for this pull request.")."""
    return {
        "status": "pending",
        "details": {
            "message": message, "uuid": UUID, "merge_method": method,
            "merge_action": "direct_merge", "expected_head_sha": head, "bypass_rules": False,
        },
    }  # fmt: skip


MERGED = {"status": "merged", "details": {"message": "Pull request was merged.", "sha": MERGE_SHA}}
HEAD_MOVED = {"status": "failed", "details": {"message": "Pull request head branch was modified."}}
CLOSED_BELOW = {
    "status": "failed",
    "details": {
        "message": "Invalid stack state: PR #10 is closed. Closed PRs are not allowed in a stack."
    },
}
NOT_FOUND = {
    "message": "Not Found",
    "documentation_url": "https://docs.github.com/rest/pulls/pulls#get-the-result-of-an-asynchronous-merge",
    "status": "404",
}


class StackedGitHub:
    """GitHub with acme/app's stack #526: `prs` bottom first ({number, head, state}), the PR the
    merge is of (`merging`), and what its asynchronous merge answers: `put` (the PUT's answer:
    a dict, or (dict, "HTTP 409") for an error answer), then `results` (each GET's, the last
    repeated)."""

    def __init__(self, fallback: Callable[..., Awaitable[str]], repo: str = "acme/app"):
        self.fallback = fallback
        self.repo = repo
        self.prs: list[dict] = []
        self.base = "main"
        self.put: dict | tuple[dict, str] = {}
        self.results: list[dict] = [MERGED]
        self.calls: list[tuple[tuple, dict | None, str]] = []
        self.rules: list[dict] = []

    def stacked(self, *numbers: int, heads: dict[int, str] | None = None) -> StackedGitHub:
        self.prs = [
            {"number": n, "head": (heads or {}).get(n, f"{n:040x}"), "state": "open"}
            for n in numbers
        ]
        return self

    def pr(self, number: int) -> dict:
        return next(pr for pr in self.prs if pr["number"] == number)

    def stack(self, number: int) -> dict:
        position = [pr["number"] for pr in self.prs].index(number) + 1
        base = {"ref": self.base, "sha": "4315bab927641ff879970c006fa735a88dc3edef"}
        return {"base": base, "id": 4006715, "number": 526, "position": position,
                "size": len(self.prs)}  # fmt: skip

    def detail(self) -> dict:
        return {
            "id": 4006715, "number": 526, "base": {"ref": self.base}, "open": True,
            "pull_requests": [
                {"number": pr["number"], "state": pr["state"], "draft": False,
                 "merged_at": "2026-11-23T18:20:52Z" if pr["state"] == "merged" else None,
                 "title": f"Part {pr['number']}",
                 "head": {"ref": f"part-{pr['number']}", "sha": pr["head"]}}
                for pr in self.prs
            ],
        }  # fmt: skip

    async def __call__(self, *arguments: str, payload: dict | None = None, token: str = ""):
        self.calls.append((arguments, payload, token))
        await asyncio.sleep(0)
        text = " ".join(arguments)
        repo = self.repo
        if text.startswith("pr merge"):
            raise GitHubError(REFUSED)
        if text.startswith(f"api repos/{repo}/stacks/526"):
            return json.dumps(self.detail())
        if text.startswith(f"api repos/{repo}/rules/branches/"):
            return json.dumps(self.rules)
        if text.startswith(f"api repos/{repo}/pulls/") and "/merge-async" in text:
            number = int(arguments[1].split("/")[4])
            if "--method=PUT" in arguments:
                answer, status = self.put if isinstance(self.put, tuple) else (self.put, "")
                if status:
                    raise GitHubError(f"GitHub request failed: gh: {status}\n", json.dumps(answer))
                return json.dumps(answer)
            if arguments[1].rsplit("/", 1)[-1] != UUID:
                raise GitHubError(
                    "GitHub request failed: gh: Not Found (HTTP 404)\n", json.dumps(NOT_FOUND)
                )
            found = self.results.pop(0) if len(self.results) > 1 else self.results[0]
            if found["status"] == "merged":
                # It and every open PR below it in the stack.
                for pr in self.prs[: [p["number"] for p in self.prs].index(number) + 1]:
                    pr["state"] = "merged"
            return json.dumps(found)
        if text.startswith(f"api repos/{repo}/pulls/") and arguments[1].count("/") == 4:
            number = int(arguments[1].rsplit("/", 1)[-1])
            if any(pr["number"] == number for pr in self.prs):
                pr = self.pr(number)
                return json.dumps({
                    "number": number, "state": "closed" if pr["state"] == "merged" else "open",
                    "merged": pr["state"] == "merged", "head": {"sha": pr["head"]},
                    "stack": self.stack(number),
                })  # fmt: skip
        return await self.fallback(*arguments, payload=payload, token=token)

    def merges(self) -> list[tuple[str, dict | None]]:
        """The asynchronous merges asked for: (path, body)."""
        return [
            (arguments[1], payload)
            for arguments, payload, _ in self.calls
            if "--method=PUT" in arguments and arguments[1].endswith("/merge-async")
        ]
