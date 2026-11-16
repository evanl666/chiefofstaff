"""Prepared for you, from discovery (shepherd.py): the owner's other open PRs in the repositories
they work in, read on GitHub (no credits), with what each needs (CI failing, review comments to
answer). At most three rows on Home, the rest in *All found…*, with why not for those it can't
take yet (a fork, stacked, a draft, not theirs). *Hand to a worker* starts a task of theirs in
a worker, which checks the branch out in a worktree of its own; the PR then is that task's,
kept moving like any worker PR."""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING

from . import threads
from .workspaces import git

if TYPE_CHECKING:
    from .shepherd import Shepherd

logger = logging.getLogger(__name__)

DISCOVER_SECONDS = 900
MOST_READ = 25
SHOWN = 3
GITHUB_REMOTE = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", re.IGNORECASE)


async def scope(shepherd: Shepherd) -> set[str]:
    """The repositories it looks in: those in Settings, and those of the owner's projects (by
    their origin on GitHub)."""
    settings = shepherd.store.settings()
    found = {repo.lower() for repo in settings.github_repos}
    for project in shepherd.runtime.workspaces.projects()[:100]:
        with suppress(Exception):
            url = (await git("-C", project, "config", "--get", "remote.origin.url")).strip()
            if match := GITHUB_REMOTE.search(url):
                found.add(match.group(1).lower())
    return found


def worker_prs(shepherd: Shepherd) -> set[str]:
    """PRs a worker follows or a task holds: they show on their task's row, not here."""
    followed = {sub["thread"] for sub in shepherd.store.followed("github:")}
    held = {
        claim["target"]
        for claim in shepherd.control.claims()
        if claim["holder_kind"] == "task" and claim["state"] != "ended"
    }
    return followed | held


def when(iso: str) -> str:
    """ "since Tue", "since 3 Nov", "since 10:30" (today)."""
    if not iso:
        return ""
    try:
        moment = datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return ""
    current = datetime.now().astimezone()
    if moment.date() == current.date():
        return moment.strftime("since %H:%M")
    if (current - moment).days < 6:
        return moment.strftime("since %a")
    return f"since {moment.day} {moment.strftime('%b')}"


async def discover(shepherd: Shepherd, force: bool = False) -> dict:
    """Read the owner's open PRs in scope and keep what they need (kv `prepared:found`)."""
    found = json.loads(shepherd.store.get("prepared:found") or "null")
    if found and not force and time.time() - found.get("read", 0) < DISCOVER_SECONDS:
        return found
    github = shepherd.runtime.github
    repos = await scope(shepherd)
    mine = await github.logins()
    taken = worker_prs(shepherd)
    rows, others = [], []
    covered = 0
    listed = await github.search_mine()
    for pr in listed:
        if pr["repo"] not in repos:
            continue
        target = threads.normalize(f"github:{pr['repo']}#{pr['number']}")
        if target in taken:
            continue
        if covered >= MOST_READ:
            others.append({**base(pr, target), "why": "not looked at in this scan"})
            continue
        covered += 1
        try:
            details = await github.pr_details(pr["repo"], pr["number"])
        except Exception:
            others.append({**base(pr, target), "why": "could not be read"})
            continue
        item = base(pr, target) | {"title": details["title"] or pr["title"]}
        if why := await shepherd.eligible(details, mine):
            others.append({**item, "why": why})
            continue
        from .shepherd import failing, pending

        broken = failing(details)
        if broken and not pending(details):
            names = sorted({check["name"] for check in broken})
            what = f"CI failing {when(details['updated'])}".strip()
            rows.append(
                {**item, "kind": "fix-ci", "what": what, "checks": names[:5],
                 "updated": details["updated"]}
            )  # fmt: skip
            continue
        asks = await shepherd.review_asks(details)
        if asks:
            people = sorted({ask["author"] for ask in asks if ask["writer"]}) or ["a reviewer"]
            what = (
                f"Changes requested by {', '.join(people[:2])}"
                if details.get("review") == "CHANGES_REQUESTED"
                else f"{len(asks)} comment{'s' if len(asks) > 1 else ''} to answer"
            )
            rows.append({**item, "kind": "address-review", "what": what,
                         "updated": details["updated"]})  # fmt: skip
            continue
        others.append({**item, "why": "nothing to do now"})
    rows.sort(key=lambda row: row.get("updated") or "", reverse=True)
    found = {"read": time.time(), "rows": rows, "others": others, "scope": sorted(repos)}
    shepherd.store.set("prepared:found", json.dumps(found))
    shepherd.publish()
    return found


def base(pr: dict, target: str) -> dict:
    return {
        "target": target, "repo": pr["repo"], "number": pr["number"],
        "label": f"{pr['repo'].split('/', 1)[-1]}#{pr['number']}", "title": pr["title"],
        "url": pr["url"], "account": pr.get("account", ""),
    }  # fmt: skip


def rows_view(shepherd: Shepherd) -> dict:
    """Home's rows (at most three, the newest first) and everything found."""
    found = json.loads(shepherd.store.get("prepared:found") or "null") or {}
    taken = worker_prs(shepherd)
    rows = [row for row in found.get("rows") or [] if row["target"] not in taken]
    return {
        "rows": rows[:SHOWN],
        "more": max(0, len(rows) - SHOWN),
        "all": rows + [row for row in found.get("others") or [] if row["target"] not in taken],
        "read": found.get("read") or 0,
    }


async def hand(shepherd: Shepherd, target: str) -> dict:
    """Hand to a worker: a task of the owner's ("Keep acme/api#117 moving") in a worker that
    checks the branch out in a worktree of its own. The PR becomes its task's, and its first
    attempt counts as clicked (charged, not held back by the budget)."""
    if not shepherd.active():
        raise ValueError("Prepare work for me is off (Settings → General).")
    target = threads.normalize(target)
    if target in worker_prs(shepherd):
        raise ValueError("A worker has it already.")
    repo, number = target.removeprefix("github:").rsplit("#", 1)
    github = shepherd.runtime.github
    details = await github.pr_details(repo, int(number))
    if why := await shepherd.eligible(details, await github.logins()):
        raise ValueError(f"Not this PR: {why}.")
    project = await local_project(shepherd, repo)
    from .tools import SpawnWorker

    instructions = (
        f"Keep {repo}#{number} moving until it's merged ({details['url']}). Get ready for it: "
        + (
            f"in your worktree of {repo}, "
            if project
            else f"add_repo {repo} for a worktree of your own, then in it "
        )
        + f"check the PR's branch out (`gh pr checkout {number}`) and read what it changes. "
        "Chief of Staff prepares its fixes for the owner's approval as shepherd steps: don't "
        "change, commit or push anything now. Report done, in two lines: what the PR does and "
        "where it stands."
    )
    params = SpawnWorker(
        title=f"Keep {repo}#{number} moving",
        instructions=instructions,
        project=project,
        isolate=bool(project),
        follow=[target],
        why_new="Handed to a worker from Prepared for you",
    )
    router = shepherd.runtime.router_id()
    started = await shepherd.runtime.toolset.t_spawn_worker(params, router)
    task = shepherd.runtime.tasks.get(started["task_id"])
    claim = shepherd.control.give(target, task["id"], details["head"])
    shepherd.control.update_claim(target, details=details, read_at=time.time())
    shepherd.store.set(f"clickfirst:{target}", "1")
    shepherd.store.set(f"handed:{target}", task["id"])
    shepherd.runtime.task_actions.publish(task)
    shepherd.publish()
    return {"task_id": task["id"], "session_id": started["session_id"], "claim": claim}


async def local_project(shepherd: Shepherd, repo: str) -> str:
    """The owner's project folder whose origin is this repository ("" for none)."""
    for project in shepherd.runtime.workspaces.projects()[:100]:
        with suppress(Exception):
            url = (await git("-C", project, "config", "--get", "remote.origin.url")).strip()
            match = GITHUB_REMOTE.search(url)
            if match and match.group(1).lower() == repo.lower():
                return project
    return ""
