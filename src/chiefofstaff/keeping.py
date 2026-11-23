"""Who holds each kept PR, and what the owner sees and does about it (shepherd.py): the claim's
transitions (a worker closed or reopened, a task cancelled, a PR no longer followed, the owner's
Resume, Look after this PR, Merge when ready, Accept this commit), durable acceptance, and each
task's `shepherd` field on its card (its words and its one action)."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from .control import (
    APPLYING,
    CLAIM_ENDED,
    DISPATCHING,
    HANDING,
    IDLE,
    PAUSED,
    PREPARED,
    QUEUED,
    RUNNING,
)
from .integrity import short_test

if TYPE_CHECKING:
    from .shepherd import Shepherd

logger = logging.getLogger(__name__)

# How long a dispatch already under way may take to end before a change of hands goes on.
DRAIN_SECONDS = 30


class Keeping:
    """The claims' part of the Shepherd (mixed into it)."""

    # Changing hands: drained first.

    async def drain_target(self: Shepherd, target: str, why: str):  # type: ignore[misc]
        """Before a claim changes holder: its attempt stopped, what is dispatching reconciled,
        its unpublished plans withdrawn."""
        await self.stop_target(target, why, "withdrawn")
        for _ in range(DRAIN_SECONDS * 10):
            if not [a for a in self.control.actions("", DISPATCHING) if a["target"] == target]:
                break
            await asyncio.sleep(0.1)
        from .publish import reconcile_all

        with suppress(Exception):
            await reconcile_all(self)
        self.stale_plans(target, why, state="withdrawn")

    async def to_owner(self: Shepherd, target: str, why: str):  # type: ignore[misc]
        claim = self.control.claim(target)
        if not claim or claim["state"] in {CLAIM_ENDED, HANDING}:
            return
        if not self.control.set_state(target, (claim["holder"], claim["generation"]), HANDING):
            return
        await self.drain_target(target, why)
        self.control.to_owner(target, why, claim.get("head") or "")
        self.publish_holder(claim)

    def publish_holder(self: Shepherd, claim: dict):  # type: ignore[misc]
        for task_id in {claim.get("holder") or "", claim.get("origin_task") or ""} - {""}:
            if task := self.runtime.tasks.find(task_id):
                self.runtime.task_actions.publish(task)

    async def worker_closed(self: Shepherd, session_id: str):  # type: ignore[misc]
        """Its worker closed: each PR its tasks held goes to the owner, paused; its origin, its
        acceptance and approved heads kept. Reopening the worker gives them back, paused."""
        if not self.control.exists():
            return
        if self.marked(session_id):
            await self.stop_step(session_id, "interrupted", "Its worker was closed.")
        tasks = {task["id"] for task in self.runtime.tasks.for_session(session_id)}
        for claim in self.control.claims(IDLE, PAUSED, "stepping"):
            if claim["holder"] in tasks:
                await self.to_owner(claim["target"], "its worker was closed")

    async def worker_reopened(self: Shepherd, session_id: str):  # type: ignore[misc]
        if not self.control.exists():
            return
        tasks = {task["id"] for task in self.runtime.tasks.for_session(session_id)}
        for claim in self.control.claims(PAUSED):
            if (
                claim["holder_kind"] == "owner"
                and claim["origin_task"] in tasks
                and claim["reason"] == "its worker was closed"
            ):
                self.control.give(claim["target"], claim["origin_task"], claim.get("head") or "",
                                  paused="its worker was reopened")  # fmt: skip
                self.publish_holder(claim)

    async def task_cancelled(self: Shepherd, task: dict):  # type: ignore[misc]
        if not self.control.exists():
            return
        for claim in self.control.claims(IDLE, PAUSED, "stepping"):
            if claim["holder"] == task["id"]:
                await self.to_owner(claim["target"], "its task was cancelled")

    async def unfollowed(self: Shepherd, session_id: str, thread: str):  # type: ignore[misc]
        if not self.control.exists():
            return
        claim = self.control.claim(thread)
        tasks = {task["id"] for task in self.runtime.tasks.for_session(session_id)}
        if claim and claim["holder"] in tasks and thread not in self.store.follows(session_id):
            await self.to_owner(thread, "its worker stopped following it")

    # The owner's actions on a kept PR.

    async def resume(self: Shepherd, target: str) -> dict:  # type: ignore[misc]
        """Resume: the PR's head as it is now is the one the journal explains; held by its task
        again (its origin, when its worker is open). Not an Accept: a head nobody explained is
        never merged until the owner accepts it."""
        claim = self.require(target)
        if claim["state"] != PAUSED:
            raise ValueError("It is not paused.")
        repo, number = target.removeprefix("github:").rsplit("#", 1)
        details = await self.runtime.github.pr_details(repo, int(number))
        self.control.update_claim(target, head=details["head"], details=details)
        holder = claim["holder"] or claim["origin_task"]
        task = self.runtime.tasks.find(holder) if holder else None
        row = self.store.find_session(task["session_id"]) if task else None
        if claim["holder_kind"] == "owner":
            if not task or task["status"] in {"cancelled", "failed"} or not row or row["closed_at"]:
                raise ValueError("Its worker is closed: hand it to a worker again.")
            self.control.give(target, task["id"], details["head"])
        else:
            self.control.resume(target, details["head"])
        self.store.delete(f"stopped:{target}")
        self.store.delete(f"capped:{target}")
        self.read_at[target] = 0
        self.publish_holder(claim)
        self.runtime.background(self.read(target))
        return self.control.claim(target) or {}

    async def keep(self: Shepherd, target: str, on: bool) -> dict:  # type: ignore[misc]
        """Look after this PR, on or off. Off: the owner's; its news comes as today."""
        claim = self.require(target)
        if not on:
            self.control.update_claim(target, keep=0, merge_when_ready=0)
            await self.to_owner(target, "you stopped looking after it")
        else:
            self.control.update_claim(target, keep=1)
            if claim["holder_kind"] == "owner":
                return await self.resume(target)
        return self.control.claim(target) or {}

    async def merge_when_ready(self: Shepherd, target: str, on: bool) -> dict:  # type: ignore[misc]
        """Opt one PR in to Merge when ready: eligible, on a base with no merge queue, one the
        owner's account may merge, with a head they accepted."""
        claim = self.require(target)
        if not on:
            self.control.update_claim(target, merge_when_ready=0)
            self.publish_holder(claim)
            return self.control.claim(target) or {}
        repo, number = target.removeprefix("github:").rsplit("#", 1)
        details = await self.runtime.github.pr_details(repo, int(number))
        mine = await self.runtime.github.logins()
        if why := await self.eligible(details, mine):
            raise ValueError(f"Not this PR: {why}.")
        if details.get("permission") not in {"ADMIN", "MAINTAIN", "WRITE"}:
            raise ValueError("Your account can't merge this PR.")
        rules = await self.runtime.github.branch_rules(repo, details["base_ref"])
        if rules is None:
            raise ValueError("Its base's rules could not be read.")
        if any(rule.get("type") == "merge_queue" for rule in rules):
            raise ValueError("Its base merges through a queue: not yet.")
        if not (claim.get("accepted") or {}).get("sha"):
            raise ValueError(
                "Accept it first: Accept on its task, or Accept this commit in its menu."
            )
        self.control.update_claim(target, merge_when_ready=1, details=details)
        self.publish_holder(claim)
        self.read_at[target] = 0
        self.runtime.background(self.read(target))
        return self.control.claim(target) or {}

    async def accept_head(self: Shepherd, target: str, sha: str) -> dict:  # type: ignore[misc]
        """Accept this head: the head the owner looked at (its diff), recorded if it is still
        the PR's."""
        claim = self.require(target)
        repo, number = target.removeprefix("github:").rsplit("#", 1)
        details = await self.runtime.github.pr_details(repo, int(number))
        if details["head"] != sha:
            raise ValueError("The PR has new commits since you looked: look at them first.")
        self.record_acceptance(target, sha, claim["holder"] or claim["origin_task"], details)
        self.publish_holder(claim)
        self.read_at[target] = 0
        return self.control.claim(target) or {}

    def record_acceptance(self: Shepherd, target: str, sha: str, task_id: str, details: dict):  # type: ignore[misc]
        self.control.accept(target, sha, task_id)
        claim = self.control.claim(target) or {}
        accepted = {**(claim.get("accepted") or {})}
        if details.get("head") == sha:
            accepted["checks"] = sorted({check["name"] for check in details.get("checks") or []})
        with self.store.lock, self.store.db:
            self.store.db.execute(
                "UPDATE pr_claims SET accepted=? WHERE target=?", (json.dumps(accepted), target)
            )

    def accepted(self: Shepherd, task: dict) -> list[str]:  # type: ignore[misc]
        """The owner accepted a task's result: for each of its kept PRs, the head its card
        showed is recorded as accepted when it is the commit its checkout holds (the diff they
        reviewed); otherwise the result is accepted but no head is (said on its card). Paused
        too: the owner's review stands (only what it does waits)."""
        if not (self.on() and self.control.exists()):
            return []
        evidence = {}
        run = self.store.one(
            "SELECT evidence FROM runs WHERE task_id=? AND evidence!='' ORDER BY started_at DESC",
            (task["id"],),
        )
        if run:
            evidence = json.loads(run["evidence"])
        commits = {checkout.get("commit") for checkout in evidence.get("checkouts") or []}
        notes = []
        for claim in self.control.held_by(task["id"]):
            if claim["state"] == CLAIM_ENDED:
                continue
            label = claim["target"].removeprefix("github:")
            pr = next((p for p in evidence.get("prs") or [] if p["label"].lower() == label), {})
            head = pr.get("head") or ""
            if head and head in commits:
                self.record_acceptance(
                    claim["target"], head, task["id"], claim.get("details") or {}
                )
                self.store.delete(f"acceptnote:{claim['target']}")
            else:
                # Not in its receipts at all (GitHub didn't answer for it then): nothing of it
                # was shown to review, rather than a head that moved since.
                note = (
                    "Accepted, but the PR changed after what you reviewed: it won't merge by "
                    "itself."
                    if pr
                    else "Accepted, but its PR wasn't in what you reviewed (GitHub didn't answer "
                    "for it then): it won't merge by itself; Accept this commit… in its ⋯ does."
                )
                self.store.set(f"acceptnote:{claim['target']}", note)
                notes.append(note)
        return notes

    def require(self: Shepherd, target: str) -> dict:  # type: ignore[misc]
        if why := self.refusal():
            raise ValueError(why)
        claim = self.control.claim(target)
        if not claim or claim["state"] == CLAIM_ENDED:
            raise ValueError("Chief of Staff doesn't keep this PR moving.")
        return claim

    async def fix_it(self: Shepherd, target: str) -> dict:  # type: ignore[misc]
        """Fix it: an attempt at Prepare now, the owner's click (charged, not held back by the
        budget)."""
        claim = self.require(target)
        if claim["state"] == PAUSED:
            raise ValueError("It is paused: Resume it first.")
        task = self.runtime.tasks.find(claim["holder"])
        if not task:
            raise ValueError("No task holds it.")
        repo, number = target.removeprefix("github:").rsplit("#", 1)
        details = await self.runtime.github.pr_details(repo, int(number))
        self.control.update_claim(target, head=details["head"], details=details)
        claim = self.control.claim(target) or claim
        if open_step := self.control.open_step(target):
            if (
                open_step["state"] == PREPARED
                and (open_step["plan"] or {}).get("kind") == "diagnosis"
            ):
                self.control.move_step(open_step["id"], PREPARED, state="diagnosis",
                                       ended_at=_now())  # fmt: skip
            elif open_step["state"] == QUEUED:
                self.control.move_step(open_step["id"], QUEUED, clicked=1, level="prepare", why="")
                await self.pump()
                return self.control.step(open_step["id"]) or {}
        self.store.delete(f"stopped:{target}")
        steps = await self.signals(claim, task, details, clicked=True)
        if not steps:
            raise ValueError("Nothing to fix on it now, or it was tried already on this commit.")
        await self.pump()
        return steps[0]

    async def take(self: Shepherd, pr: str, task_id: str) -> str:  # type: ignore[misc]
        """A PR the owner asked the Router to keep moving, given to this task (as Hand to a
        worker does): words for the Router, saying whether it is kept moving."""
        from . import threads

        if why := self.refusal():
            return f"Not kept moving: {why[0].lower()}{why[1:]} Tell the owner."
        keys = threads.pull_requests(pr) or (
            [threads.normalize(f"github:{pr.strip()}")]
            if threads.GITHUB_PR.fullmatch(threads.normalize(f"github:{pr.strip()}"))
            else []
        )
        if not keys:
            raise ValueError("Name the PR as owner/repo#n.")
        target = keys[0]
        task = self.runtime.tasks.get(task_id)
        claim = self.control.claim(target)
        if claim and claim["state"] != CLAIM_ENDED and claim["holder"] not in {"", task_id}:
            return "Another task keeps it moving already."
        if claim and claim["holder_kind"] == "owner" and claim["state"] != CLAIM_ENDED:
            await self.drain_target(target, "handed to another task")
        found = await self.give_task(target, task)
        if not found:
            return (
                "Not kept moving: it is not the owner's own open PR from the same repository (or "
                "is a draft, or stacked)."
            )
        self.store.set(f"handed:{target}", task_id)
        self.store.set(f"clickfirst:{target}", "1")
        self.runtime.follow(task["session_id"], [target], task_id=task_id, asked=[target])
        return (
            "Kept moving: its failing CI and review comments are prepared for the owner's approval."
        )

    def router_words(self: Shepherd, target: str) -> dict:  # type: ignore[misc]
        """What check_prs says of a PR Prepared for you keeps moving ({} for any other)."""
        if not self.on() or not self.control.exists():
            return {}
        claim = self.control.claim(target)
        if not claim or claim["state"] == CLAIM_ENDED:
            return {}
        task = self.runtime.tasks.find(claim["holder"]) if claim["holder"] else None
        view = self.claim_view(claim, task) if task else {"line": claim["reason"]}
        return {
            "kept_moving": view.get("line") or ("paused" if claim["state"] == PAUSED else "yes")
        }

    def step_reply(self: Shepherd, params: Any, caller: str) -> str:  # type: ignore[misc]
        """draft_github_review in a step: a reply to one of this PR's threads, kept for its plan
        (no card of its own); anything else is refused."""
        step = self.step_of(caller)
        if not step:
            raise ValueError("No shepherd step of yours is running.")
        target = f"github:{params.repo.lower().strip()}#{int(params.number)}"
        if target != step["target"]:
            raise ValueError("In a shepherd step, reply only on its own PR.")
        comment = params.in_reply_to or params.reply_to_comment
        asks = {int(ask["comment"]) for ask in (step["signal"] or {}).get("threads") or []}
        asks |= {int(ask["last"]) for ask in (step["signal"] or {}).get("threads") or []}
        if not comment or int(comment) not in asks:
            raise ValueError("In a shepherd step, reply only to the comments in threads.json.")
        if params.comments or params.verdict != "comment":
            raise ValueError("In a shepherd step, only replies: no review, no verdict.")
        result = {**(step["result"] or {})}
        drafted = [t for t in result.get("drafted") or [] if int(t["comment"]) != int(comment)]
        drafted.append({"comment": int(comment), "reply": params.body.strip()[:6000]})
        result["drafted"] = drafted
        self.control.move_step(step["id"], RUNNING, result=result)
        return "Kept for the plan: give its stance in shepherd_result's threads too."

    # What a task's card shows.

    def task_view(self: Shepherd, task: dict) -> dict | None:  # type: ignore[misc]
        """A task's kept PR (the most pressing, when it holds several): where the shepherd
        stands, in a line, and the row's one action. None for a task with none."""
        if not self.on() or not self.control.exists():
            return None
        claims = [c for c in self.control.held_by(task["id"]) if c["state"] != CLAIM_ENDED]
        if not claims:
            return None
        views = [self.claim_view(claim, task) for claim in claims]
        views.sort(key=lambda view: view["rank"])
        return views[0]

    def claim_view(self: Shepherd, claim: dict, task: dict) -> dict:  # type: ignore[misc]
        from .shepherd import KIND_WORDS, short_label

        target = claim["target"]
        label = short_label(target)
        details = claim.get("details") or {}
        step = self.control.open_step(target)
        last = (self.control.steps(target) or [None])[-1]
        view: dict[str, Any] = {
            "target": target, "label": label, "url": details.get("url") or "",
            "state": claim["state"], "reason": claim["reason"], "keep": bool(claim["keep"]),
            "merge_when_ready": bool(claim["merge_when_ready"]),
            "accepted": (claim.get("accepted") or {}).get("sha") or "",
            "accept_note": self.store.get(f"acceptnote:{target}") or "",
            "head": details.get("head") or claim.get("head") or "", "line": "", "action": None,
            "step": None, "rank": 3, "journal": self.journal_view(target),
        }  # fmt: skip
        mine = claim["holder"] == task["id"]
        if not mine:
            view["line"] = (
                "Its worker was closed" if claim["reason"] == "its worker was closed" else ""
            )
            view["rank"] = 4
            return view
        if claim["state"] == PAUSED:
            view["line"] = paused_words(claim["reason"], claim.get("paused_head") or "")
            view["action"] = {"kind": "resume", "label": "Resume"}
            view["rank"] = 1
            return view
        stopped = json.loads(self.store.get(f"stopped:{target}") or "null")
        if step:
            view["step"] = self.step_view(step)
            view.update(self.step_words(step, label))
        elif stopped:
            view["line"] = sentence(stopped["why"])  # Its status says Stopped.
            view["action"] = {"kind": "look", "label": "Look"}
            view["rank"] = 1
        elif last and last["state"] == "refused":
            view["line"] = f"Can't prepare fixes here: {last['why']}"
            view["refused"] = True
            view["rank"] = 2
        elif last and last["state"] in {"published", "partly"} and last["kind"] != "merge":
            view["line"] = self.published_words(last)
            view["rank"] = 3
            if last["state"] == "partly":
                view["action"] = {"kind": "look", "label": "Look"}
                view["rank"] = 1
        elif last and last["state"] == "failed":
            failed = [a for a in self.control.actions(last["id"]) if a["state"] != "sent"]
            if failed:
                # The owner approved it, and it did not go out: theirs to look at.
                view["line"] = f"Not published: {failed[0]['outcome'] or 'it failed'}"
                view["action"] = {"kind": "look", "label": "Look"}
                view["rank"] = 1
            elif last["why"]:
                view["line"] = last["why"]
                view["rank"] = 3
        if claim["merge_when_ready"] and not view["line"]:
            why = self.store.get(f"mergewhy:{target}") or ""
            view["line"] = f"Merges when ready{': ' + why if why else ''}"
        view["line"] = sentence(view["line"], capital=False)
        view["kinds"] = {kind: self.level(kind) for kind in KIND_WORDS}
        return view

    def step_words(self: Shepherd, step: dict, label: str) -> dict:  # type: ignore[misc]
        plan = step["plan"] or {}
        if step["state"] == QUEUED:
            if step["why"] == "today's budget is used" or (
                not step["clicked"] and self.store.settings().proactive_budget <= 0
            ):
                what = "CI failing" if step["kind"] == "fix-ci" else "Review comments"
                action = {"kind": "fix", "label": "Fix it", "step": step["id"]}
                return {"line": f"{what} on {label}", "rank": 1, "action": action}
            return {"line": "Waiting for its worker to be free", "rank": 2}
        if step["state"] == RUNNING:
            what = "a fix" if step["level"] == "prepare" else "a look at it"
            if step["kind"] == "address-review":
                what = "replies" if step["level"] == "prepare" else "a look at the comments"
            return {"line": f"Preparing {what}…", "rank": 2}
        if step["state"] == APPLYING:
            return {"line": "Publishing what you approved…", "rank": 2}
        if plan.get("kind") == "diagnosis":
            first = (plan.get("summary") or "").strip().split("\n", 1)[0][:200]
            return {"line": first or "It found the cause", "rank": 1,
                    "action": {"kind": "fix", "label": "Fix it", "step": step["id"]}}  # fmt: skip
        if plan.get("kind") == "rerun":
            action = {"kind": "review", "label": "Run it again", "step": step["id"]}
            return {"line": sentence(f"CI failed: {plan.get('why')}"), "rank": 1, "action": action}
        replies = any(
            t.get("reply") and t.get("stance") != "leave" for t in plan.get("threads") or []
        )
        words = "Review & post" if replies else "Review fix"
        evidence = plan.get("evidence") or {}
        fixed = evidence.get("fixed") or []
        if step["kind"] == "fix-ci":
            line = "A fix for the failing CI is ready"
        elif plan.get("h1"):
            line = "A fix and replies for the review are ready"
        else:
            line = "Replies to the review are ready"
        if plan.get("change") == "approval":
            line += ": it changes tests or checks"
        elif plan.get("h1") and not (evidence.get("holds") and not evidence.get("void")):
            line += ", not fully checked"
        elif len(fixed) == 1:
            line += f": {short_test(fixed[0])} passes now"
        elif fixed:
            line += f": {len(fixed)} failing tests pass now"
        line += "."
        return {"line": line, "rank": 0,
                "action": {"kind": "review", "label": words, "step": step["id"]}}  # fmt: skip

    def published_words(self: Shepherd, step: dict) -> str:  # type: ignore[misc]
        plan = step["plan"] or {}
        if plan.get("kind") == "rerun":
            return "CI is running again"
        if plan.get("h1"):
            part = " (partly published)" if step["state"] == "partly" else ""
            return f"Pushed {plan['h1'][:7]}{part}"
        return "Replies posted" + (" in part" if step["state"] == "partly" else "")

    def step_view(self: Shepherd, step: dict) -> dict:  # type: ignore[misc]
        plan = step["plan"] or {}
        return {
            "id": step["id"], "kind": step["kind"], "state": step["state"],
            "level": step["level"], "clicked": bool(step["clicked"]),
            "plan_kind": plan.get("kind") or "", "h0": step["h0"], "h1": plan.get("h1") or "",
            "why": step["why"],
        }  # fmt: skip

    def journal_view(self: Shepherd, target: str) -> list[dict]:  # type: ignore[misc]
        """Every attempt and operation on the PR, oldest first: the card's journal."""
        found = []
        for step in self.control.steps(target):
            if step["state"] == QUEUED and not step["started_at"]:
                continue
            plan = step["plan"] or {}
            found.append({
                "id": step["id"], "kind": step["kind"], "state": step["state"],
                "at": step["started_at"] or step["created_at"], "h0": step["h0"][:7],
                "h1": (plan.get("h1") or "")[:7], "summary": (plan.get("summary") or "")[:300],
                "evidence": (plan.get("evidence") or {}).get("words") or "",
                "change": plan.get("change") or "", "credits": step["settled"],
                "why": step["why"],
                "operations": [
                    {"kind": action["kind"], "state": action["state"], "link": action["link"],
                     "outcome": action["outcome"]}
                    for action in self.control.actions(step["id"])
                ],
            })  # fmt: skip
        return found[-30:]

    def plan_view(self: Shepherd, step_id: str) -> dict:  # type: ignore[misc]
        """A plan as the owner reviews it: what it does, its diff (hunks to approve first), its
        evidence, each reply and choice, and whether disclosure must be decided."""
        step = self.control.step(step_id)
        if not step:
            raise LookupError("Not found.")
        plan = step["plan"] or {}
        repo = plan.get("repo") or step["target"].removeprefix("github:").rsplit("#", 1)[0]
        choice = self.store.settings().proactive_repos.get(repo)
        replies = any(t.get("reply") for t in plan.get("threads") or [])
        return {
            "id": step["id"], "target": step["target"], "kind": step["kind"],
            "state": step["state"], "plan": plan, "h0": step["h0"],
            "disclosure": (choice.disclosure if choice else "") if replies else "n/a",
            "operations": self.control.actions(step_id),
            "account": "",
        }  # fmt: skip


def sentence(words: str, capital: bool = True) -> str:
    """Words as a sentence: a full stop last, and a capital first (`capital`: not for words that
    may start with code, `timeout()`)."""
    words = words.strip()
    if not words:
        return ""
    if capital:
        words = words[0].upper() + words[1:]
    return words if words.endswith((".", "!", "?", "…")) else f"{words}."


def paused_words(reason: str, head: str) -> str:
    """Why a PR waits for the owner's Resume, in a line (its status says Paused)."""
    if reason.startswith("new commits it didn't push"):
        pushed = head[:7] or reason.rsplit("(", 1)[-1].rstrip(")")
        return f"Someone else pushed {pushed}, so it's waiting for you."
    if reason.startswith("stacked"):
        return "Another PR now builds on this one, so it's waiting for you."
    if reason.startswith("from a fork"):
        return "It comes from a fork now, so it's waiting for you."
    if reason.startswith("a check vanished after the push: "):
        checks = reason.split(": ", 1)[1]
        return f"{checks} didn't run after its fix, so it's waiting for you."
    if not reason:
        return "It's waiting for you."
    return sentence(f"{reason}, so it's waiting for you")


def _now() -> str:
    from .store import now

    return now()


_unused = (APPLYING,)
