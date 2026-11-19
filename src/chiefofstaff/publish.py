"""Publishing what the owner approved (shepherd.py): a plan's operations, journaled with stable
ids before each goes out (control.py), sent by Chief of Staff as the account bound to the plan,
each checked just before it goes (and by policy.operation_denial), and reconciled on GitHub
before anything is tried again: a push by its ref's SHA, a reply found by its text. And the
merge of *Merge when ready*: matched to a head the owner accepted (or one Chief of Staff pushed
from a plan they approved), once every condition holds, read fresh."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING

from .control import (
    APPLYING,
    DISPATCHING,
    IDLE,
    PLANNED,
    PREPARED,
)
from .policy import DEFAULT_BRANCHES, operation_denial
from .store import now
from .workspaces import SAFE_GIT, checkout_git

if TYPE_CHECKING:
    from .shepherd import Shepherd

logger = logging.getLogger(__name__)

RESOLVE = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"
# Content writes per account (GitHub's limits are 80 a minute and 500 an hour).
WRITES_A_MINUTE = 60
WRITES_AN_HOUR = 400
CONTENT = frozenset({"reply", "conversation-reply", "request-review"})
# Labels that hold a merge.
HOLD = re.compile(
    r"do[ -]?not[ -]?merge|\bdnm\b|\bwip\b|work in progress|\bhold\b|\bblocked\b", re.IGNORECASE
)
# A line saying a reply was drafted with an AI assistant, where the owner wants one.
DISCLOSURE = "\n\n<sub>Drafted with an AI assistant; reviewed and posted by me.</sub>"


class RefusedError(Exception):
    """An operation not sent: why, in the owner's words."""


# Approving a plan.


async def approve(shepherd: Shepherd, step_id: str, choices: dict) -> dict:
    """The owner approves a prepared plan (with their edits): one approval wins, its operations
    are journaled, then sent in order. `choices`: `replies` (comment id -> text as edited),
    `resolve` (comment ids to resolve once the fix is on the PR), `choice` (comment id -> post,
    their-way or leave), `disclosure` (yes or no, for the repository, when not decided yet)."""
    control = shepherd.control
    step = control.step(step_id)
    if not step or step["state"] != PREPARED:
        raise ValueError("This was decided already.")
    plan = step["plan"] or {}
    if plan.get("kind") == "diagnosis":
        raise ValueError("A diagnosis has nothing to publish: Fix it prepares a fix.")
    claim = control.claim(step["target"])
    if not claim:
        raise ValueError("The PR is no longer kept moving.")
    repo = plan.get("repo") or step["target"].removeprefix("github:").rsplit("#", 1)[0]
    choice = {str(key): value for key, value in (choices.get("choice") or {}).items()}
    their_way = [key for key, value in choice.items() if value == "their-way"]
    if their_way:
        return await redo(shepherd, step, "Do it the way the reviewer asked on these comments: "
                          + ", ".join(their_way), their_way)  # fmt: skip
    replies = {str(key): str(value) for key, value in (choices.get("replies") or {}).items()}
    edited = False
    threads_kept = []
    for thread in plan.get("threads") or []:
        key = str(thread["comment"])
        thread = {**thread, "choice": choice.get(key, "post")}
        if key in replies and replies[key].strip() != thread.get("reply", "").strip():
            thread["reply"], edited = replies[key].strip(), True
        thread["resolve"] = str(thread["comment"]) in {str(c) for c in choices.get("resolve") or []}
        threads_kept.append(thread)
    posting = [
        t for t in threads_kept if t["choice"] == "post" and t["stance"] != "leave" and t["reply"]
    ]
    settings = shepherd.store.settings()
    disclosure = settings.proactive_repos.get(repo) or None
    decided = disclosure.disclosure if disclosure else ""
    if posting and not decided:
        answer = choices.get("disclosure") or ""
        if answer not in {"yes", "no"}:
            raise ValueError("Answer first: add a line saying replies are drafted with an AI "
                             "assistant, in this repository?")  # fmt: skip
        await save_disclosure(shepherd, repo, answer)
        decided = answer
    operations = operations_of(plan, threads_kept, decided == "yes", step)
    if not operations:
        raise ValueError("Nothing in it would be published.")
    login = await shepherd.runtime.github.acting_login(repo)
    if not login:
        raise ValueError("No GitHub account is signed in to publish it.")
    why = control.journal(
        step_id, claim["holder"], claim["generation"], shepherd.control.generation(), operations,
        login,
    )  # fmt: skip
    if why:
        raise ValueError(f"Not published: {why}.")
    control.move_step(step_id, APPLYING, plan={**plan, "threads": threads_kept})
    control.verdict(step, "approved-edited" if edited else "approved")
    learn_edits(shepherd, step, plan, threads_kept)
    shepherd.runtime.background(dispatch(shepherd, step_id))
    shepherd.publish_task(step)
    return control.step(step_id) or {}


async def save_disclosure(shepherd: Shepherd, repo: str, answer: str):
    from .models import RepositoryChoices

    store = shepherd.store
    settings = store.settings()
    repos = dict(settings.proactive_repos)
    repos[repo] = (repos.get(repo) or RepositoryChoices()).model_copy(update={"disclosure": answer})
    store.save_settings(settings.model_copy(update={"proactive_repos": repos}))


def learn_edits(shepherd: Shepherd, step: dict, plan: dict, threads: list[dict]):
    """Replies posted are learned from as drafts are: as written, or as the owner edited them."""
    before = {str(t["comment"]): t.get("reply", "") for t in plan.get("threads") or []}
    for thread in threads:
        if thread["choice"] != "post" or thread["stance"] == "leave" or not thread["reply"]:
            continue
        drafted = before.get(str(thread["comment"]), "")
        edited = drafted.strip() != thread["reply"].strip()
        shepherd.runtime.learning.record(
            "draft-edited" if edited else "draft-sent",
            context=f"A reply on {plan.get('repo')}#{plan.get('number')} to "
            f"{thread.get('author') or 'a reviewer'}: {thread.get('said', '')[:600]}",
            proposed=drafted,
            owner=thread["reply"] if edited else "",
            agreed=not edited,
            untrusted=True,
        )


def operations_of(plan: dict, threads: list[dict], disclose: bool, step: dict) -> list[dict]:
    """The plan's operations, in order: the push, the replies, resolves (only once the fix is on
    the PR, never for an answer or a push-back), the re-request of review (once per round)."""
    repo, number = plan.get("repo"), plan.get("number")
    if plan.get("kind") == "rerun":
        return [{"kind": "rerun", "repo": step["target"].removeprefix("github:").rsplit("#", 1)[0],
                 "run": run["id"], "attempt": run.get("attempt") or 1, "head": step["h0"]}
                for run in plan.get("runs") or []]  # fmt: skip
    operations: list[dict] = []
    if plan.get("h1"):
        operations.append({
            "kind": "push", "repo": repo, "number": number, "head_repo": plan["head_repo"],
            "head_repo_id": plan["head_repo_id"], "ref": plan["ref"], "lease": plan["h0"],
            "commit": plan["h1"], "base_ref": plan["base_ref"],
        })  # fmt: skip
    for thread in threads:
        if thread["choice"] != "post" or thread["stance"] == "leave" or not thread["reply"]:
            continue
        text = thread["reply"] + (DISCLOSURE if disclose else "")
        operations.append({
            "kind": "reply", "repo": repo, "number": number, "in_reply_to": thread["last"],
            "thread": thread["thread"], "text": text,
        })  # fmt: skip
    for thread in threads:
        if thread.get("resolve") and thread["stance"] == "fix" and plan.get("h1"):
            operations.append({
                "kind": "resolve", "repo": repo, "number": number, "thread": thread["thread"],
                "after": plan["h1"],
            })  # fmt: skip
    reviewers = [login for login in plan.get("reviewers") or [] if login]
    if reviewers and operations:
        operations.append({"kind": "request-review", "repo": repo, "number": number,
                           "reviewers": reviewers})  # fmt: skip
    return operations


async def redo(shepherd: Shepherd, step: dict, note: str, threads: list[str] | None = None) -> dict:
    """The owner wants it done otherwise (Request changes, Do it their way): this plan is set
    aside and a new attempt prepares another, with their note."""
    control = shepherd.control
    moved = control.move_step(step["id"], PREPARED, state="dismissed", ended_at=now(),
                              why="You asked for changes.")  # fmt: skip
    if not moved:
        raise ValueError("This was decided already.")
    control.verdict(step, "request-changes")
    claim = control.claim(step["target"])
    task = shepherd.runtime.tasks.find(step["task_id"])
    if not claim or not task:
        raise ValueError("The PR is no longer kept moving.")
    signal = {**(step["signal"] or {}), "owner_note": note[:2000]}
    if threads:
        signal["their_way"] = threads
    new = control.add_step(
        step["target"], step["kind"], "prepare", session_id=step["session_id"],
        task_id=step["task_id"], holder=claim["holder"], generation=claim["generation"],
        consent=control.generation(), h0=step["h0"], signal=signal,
        fingerprint=step["fingerprint"], threads=step["threads"], clicked=1,
        evidence={k: v for k, v in (step["evidence"] or {}).items() if k in {"runs", "log"}},
    )  # fmt: skip
    shepherd.publish_task(step)
    await shepherd.pump()
    return new


def dismiss(shepherd: Shepherd, step_id: str, why: str = "") -> dict:
    control = shepherd.control
    step = control.step(step_id)
    if not step:
        raise ValueError("Not found.")
    moved = control.move_step(step_id, PREPARED, state="dismissed", ended_at=now(),
                              why=why or "You dismissed it.")  # fmt: skip
    if not moved:
        raise ValueError("This was decided already.")
    control.verdict(step, f"dismissed{':' + why if why else ''}")
    shepherd.publish_task(step)
    return moved


# Sending.


async def dispatch(shepherd: Shepherd, step_id: str):
    """Send a plan's operations in order, each journaled as dispatching first (the barrier:
    turning the feature off or a change of hands stops what has not gone out). A push that does
    not go out withdraws what depends on it."""
    control = shepherd.control
    step = control.step(step_id)
    if not step:
        return
    pushed = True
    for action in control.actions(step_id, PLANNED):
        operation = action["payload"]
        if not pushed and operation["kind"] in {"reply", "resolve", "request-review"}:
            control.finish(action["id"], "withdrawn", "The fix was not pushed.")
            continue
        claim = control.claim(step["target"])
        if not claim:
            control.finish(action["id"], "withdrawn", "The PR is no longer kept moving.")
            continue
        async with shepherd.lock(step["target"]):
            why = control.dispatching(action["id"], claim["holder"], claim["generation"],
                                      step["consent"])  # fmt: skip
            if why:
                control.finish(action["id"], "withdrawn", why[:300])
                pushed = pushed and operation["kind"] != "push"
                continue
            try:
                if operation["kind"] in CONTENT:
                    await write_slot(shepherd, action["account"])
                link = await SEND[operation["kind"]](shepherd, operation, action["account"], step)
            except RefusedError as exc:
                control.finish(action["id"], "failed", str(exc))
                pushed = pushed and operation["kind"] != "push"
                continue
            except Exception as exc:
                logger.info("Operation %s failed.", action["id"], exc_info=True)
                state, link = await reconcile(shepherd, {**action, "payload": operation})
                control.finish(action["id"], state, f"{exc}"[:300], link)
                pushed = pushed and not (operation["kind"] == "push" and state != "sent")
                continue
            control.finish(action["id"], "sent", "", link)
    shepherd.finish_applying(step_id)


def finish_applying(shepherd: Shepherd, step_id: str):
    control = shepherd.control
    actions = control.actions(step_id)
    if any(action["state"] in {PLANNED, DISPATCHING} for action in actions):
        return
    sent = [action for action in actions if action["state"] == "sent"]
    state = "published" if len(sent) == len(actions) else "partly" if sent else "failed"
    if any(action["state"] == "uncertain" for action in actions):
        state = "partly" if sent else "failed"
    step = control.move_step(step_id, APPLYING, state=state, ended_at=now())
    if step:
        claim = control.claim(step["target"])
        if claim and claim["state"] == "stepping":
            control.set_state(step["target"], (claim["holder"], claim["generation"]), IDLE)
        # Read again at once: its row shows the PR as GitHub has it now (its new head).
        shepherd.read_at[step["target"]] = 0
        shepherd.runtime.background(shepherd.read(step["target"]))
        shepherd.publish_task(step)


async def write_slot(shepherd: Shepherd, login: str):
    """One content write at a time per account, within 60 a minute and 400 an hour."""
    async with shepherd.write_locks.setdefault(login, asyncio.Lock()):
        while True:
            moment = time.time()
            recent = [at for at in shepherd.writes.get(login, []) if moment - at < 3600]
            shepherd.writes[login] = recent
            minute = [at for at in recent if moment - at < 60]
            if len(minute) < WRITES_A_MINUTE and len(recent) < WRITES_AN_HOUR:
                recent.append(moment)
                return
            wait = 60 - (moment - minute[0]) if len(minute) >= WRITES_A_MINUTE else 3600 - (
                moment - recent[0])  # fmt: skip
            await asyncio.sleep(max(1.0, wait))


async def head_checked(shepherd: Shepherd, operation: dict) -> dict:
    """Just before a push: the PR read again, and refused unless its head repository (by id),
    branch and head are the plan's, its base unchanged, the branch neither its repository's
    default nor protected, and the branch's rules readable with none a fast-forward push might
    not meet. Unknown protection refuses."""
    github = shepherd.runtime.github
    details = await github.pr_details(operation["repo"], int(operation["number"]))
    if details["head_repo_id"] != operation["head_repo_id"] or details["fork"]:
        raise RefusedError("the PR now comes from another repository: push it yourself")
    if details["head_ref"] != operation["ref"]:
        raise RefusedError("the PR's branch changed")
    if details["head"] != operation["lease"]:
        raise RefusedError(f"the PR moved to {details['head'][:7]} since the plan was made")
    if details["base_ref"] != operation["base_ref"]:
        raise RefusedError("the PR's base changed")
    if operation["ref"] in DEFAULT_BRANCHES or operation["ref"] == details["head_default"]:
        raise RefusedError(f"{operation['ref']} is its repository's default branch")
    protected = await github.branch_protected(operation["head_repo"], operation["ref"])
    rules = await github.branch_rules(operation["head_repo"], operation["ref"])
    if protected is None or rules is None:
        raise RefusedError("protection unknown: push it yourself")
    if protected:
        raise RefusedError("the branch is protected: push it yourself")
    if any(rule.get("type") == "required_signatures" for rule in rules):
        # Its attempts are refused for a day, then the rules are read again at the next push.
        target = f"github:{operation['repo']}#{operation['number']}"
        shepherd.store.set(f"signedbranch:{target}", str(time.time()))
        raise RefusedError("the branch requires signed commits")
    if other := sorted({str(rule.get("type") or "") for rule in rules} - PUSH_RULES - {""}):
        raise RefusedError(f"the branch has rules a push may not meet ({', '.join(other)})")
    return details


PUSH_RULES = frozenset({"non_fast_forward", "deletion", "creation"})


async def send_push(shepherd: Shepherd, operation: dict, login: str, step: dict) -> str:
    from .shepherd import git_env

    details = await head_checked(shepherd, operation)
    # Through the repository as recorded, never what the tree's .git says now (the step could
    # write it): its config, hooks and remotes are not the worker's to choose.
    tree, checkout = step["tree"], shepherd.tree_of(step)
    try:
        await checkout_git(
            checkout, tree, "merge-base", "--is-ancestor", operation["lease"], operation["commit"]
        )
    except ValueError as exc:
        raise RefusedError("the fix doesn't build on the PR's latest commit") from exc
    facts = {**operation, "protected": False, "default_branch": details["head_default"]}
    words = [
        "push", "--no-verify",
        f"--force-with-lease=refs/heads/{operation['ref']}:{operation['lease']}",
        f"https://github.com/{operation['head_repo']}.git",
        f"{operation['commit']}:refs/heads/{operation['ref']}",
    ]  # fmt: skip
    if why := operation_denial(facts, ["git", *SAFE_GIT, "-C", tree, *words]):
        raise RefusedError(why)
    token = await shepherd.runtime.github.bound_token(login)
    target = f"github:{operation['repo']}#{operation['number']}"
    names = [check["name"] for check in details.get("checks") or []]
    shepherd.store.set(f"checksbefore:{target}",
                       json.dumps({"head": operation["commit"], "names": names}))  # fmt: skip
    # Explained before GitHub tells anyone: a read meanwhile must not take it for another's.
    shepherd.control.published(target, operation["commit"])
    try:
        await checkout_git(checkout, tree, *words, limit=300,
                           env=git_env(token, shepherd.git_config))  # fmt: skip
    except ValueError:
        if (
            await shepherd.runtime.github.ref_head(operation["head_repo"], operation["ref"])
            != (operation["commit"])
        ):
            raise
    found = await shepherd.runtime.github.ref_head(operation["head_repo"], operation["ref"])
    if found != operation["commit"]:
        raise RuntimeError(f"the branch is at {str(found)[:7]} after the push")
    return f"https://github.com/{operation['repo']}/pull/{operation['number']}/commits/{found}"


async def send_reply(shepherd: Shepherd, operation: dict, login: str, step: dict) -> str:
    github = shepherd.runtime.github
    details = await github.pr_details(operation["repo"], int(operation["number"]))
    thread = next((t for t in details["threads"] if t["id"] == operation["thread"]), None)
    if thread is None:
        raise RefusedError("its conversation is gone")
    if thread["resolved"]:
        raise RefusedError("its conversation was resolved meanwhile")
    target = {"repo": operation["repo"], "number": operation["number"],
              "in_reply_to": operation["in_reply_to"], "task_id": step["task_id"]}  # fmt: skip
    if found := await github.find_reply(target, operation["text"]):
        return found  # Posted already (by a send whose answer was lost, or the owner).
    path = (f"repos/{operation['repo']}/pulls/{int(operation['number'])}/comments/"
            f"{int(operation['in_reply_to'])}/replies")  # fmt: skip
    argv = ["gh", "api", path, "--method=POST", "--input=-"]
    if why := operation_denial(operation, argv):
        raise RefusedError(why)
    github.posting(target, operation["text"])
    reply = json.loads(await github.bound(login, *argv[1:], payload={"body": operation["text"]}))
    if reply.get("id"):
        github.posted(f"github:reviewcomment:{reply['id']}", operation["text"], target)
        root = reply.get("in_reply_to_id") or operation["in_reply_to"]
        github.posted(f"github:reviewthread:{root}", operation["text"], target)
    return reply.get("html_url") or "Reply posted."


async def send_resolve(shepherd: Shepherd, operation: dict, login: str, step: dict) -> str:
    github = shepherd.runtime.github
    details = await github.pr_details(operation["repo"], int(operation["number"]))
    if details["head"] != operation["after"]:
        raise RefusedError("the fix isn't the PR's latest commit")
    thread = next((t for t in details["threads"] if t["id"] == operation["thread"]), None)
    if thread is None:
        raise RefusedError("its conversation is gone")
    if thread["resolved"]:
        return "Resolved already."
    argv = ["gh", "api", "graphql", "-f", f"query={RESOLVE}", "-f", f"id={operation['thread']}"]
    if why := operation_denial(operation, argv):
        raise RefusedError(why)
    found = json.loads(await github.bound(login, *argv[1:]))
    if found.get("errors"):
        raise RefusedError(str(found["errors"][0].get("message", "GitHub refused"))[:200])
    return "Resolved."


async def send_request_review(shepherd: Shepherd, operation: dict, login: str, step: dict) -> str:
    github = shepherd.runtime.github
    details = await github.pr_details(operation["repo"], int(operation["number"]))
    reviewers = [r for r in operation["reviewers"] if r and r != details["author"]]
    if not reviewers:
        raise RefusedError("no reviewer to ask again")
    path = f"repos/{operation['repo']}/pulls/{int(operation['number'])}/requested_reviewers"
    argv = ["gh", "api", path, "--method=POST", "--input=-"]
    if why := operation_denial(operation, argv):
        raise RefusedError(why)
    await github.bound(login, *argv[1:], payload={"reviewers": reviewers})
    return f"Asked {', '.join(reviewers)} to review again."


async def send_rerun(shepherd: Shepherd, operation: dict, login: str, step: dict) -> str:
    github = shepherd.runtime.github
    details = await github.pr_details(operation["repo"], int(step["target"].rsplit("#", 1)[1]))
    if details["head"] != operation["head"]:
        raise RefusedError("the PR moved since: CI runs again on its new commit")
    argv = ["gh", "run", "rerun", str(int(operation["run"])), "--failed", "--repo",
            operation["repo"]]  # fmt: skip
    if why := operation_denial(operation, argv):
        raise RefusedError(why)
    await github.bound(login, *argv[1:])
    shepherd.store.set(
        f"rerun:{operation['repo']}:{operation['run']}",
        json.dumps({"head": operation["head"], "checks": (step["signal"] or {}).get("checks"),
                    "fingerprint": step["fingerprint"]}),
    )  # fmt: skip
    return f"https://github.com/{operation['repo']}/actions/runs/{int(operation['run'])}"


async def send_merge(shepherd: Shepherd, operation: dict, login: str, step: dict) -> str:
    github = shepherd.runtime.github
    repo = json.loads(await github.gh("api", f"repos/{operation['repo']}"))
    method = next(
        (flag for flag, allowed in (("--squash", repo.get("allow_squash_merge", True)),
                                    ("--merge", repo.get("allow_merge_commit", True)),
                                    ("--rebase", repo.get("allow_rebase_merge", True)))
         if allowed), "--merge",
    )  # fmt: skip
    url = f"https://github.com/{operation['repo']}/pull/{int(operation['number'])}"
    argv = ["gh", "pr", "merge", url, method, "--match-head-commit", operation["head"]]
    if why := operation_denial(operation, argv):
        raise RefusedError(why)
    await github.bound(login, *argv[1:])
    return url


SEND = {
    "push": send_push,
    "reply": send_reply,
    "resolve": send_resolve,
    "request-review": send_request_review,
    "rerun": send_rerun,
    "merge": send_merge,
}


# Reconciling: what became of an operation whose outcome is not known.


async def reconcile(shepherd: Shepherd, action: dict) -> tuple[str, str]:
    """Whether an operation went out, read on GitHub: (sent | failed | uncertain, link)."""
    operation, github = action["payload"], shepherd.runtime.github
    try:
        if operation["kind"] == "push":
            found = await github.ref_head(operation["head_repo"], operation["ref"])
            if found == operation["commit"]:
                return "sent", ""
            if found == operation["lease"] or not found:
                return "failed", ""
            return await pushed_under(shepherd, operation, found), ""
        if operation["kind"] == "reply":
            target = {"repo": operation["repo"], "number": operation["number"],
                      "in_reply_to": operation["in_reply_to"]}  # fmt: skip
            found = await github.find_reply(target, operation["text"])
            return ("sent", found) if found else ("failed", "")
        if operation["kind"] == "resolve":
            details = await github.pr_details(operation["repo"], int(operation["number"]))
            thread = next((t for t in details["threads"] if t["id"] == operation["thread"]), None)
            return ("sent", "") if thread and thread["resolved"] else ("failed", "")
        if operation["kind"] == "request-review":
            asked = await github.requested_reviewers(operation["repo"], int(operation["number"]))
            return ("sent", "") if set(operation["reviewers"]) <= set(asked) else ("failed", "")
        if operation["kind"] == "rerun":
            attempt = await github.run_attempt(operation["repo"], int(operation["run"]))
            if attempt is None:
                return "uncertain", ""
            return ("sent", "") if attempt > int(operation.get("attempt") or 1) else ("failed", "")
        if operation["kind"] == "merge":
            details = await github.pr_details(operation["repo"], int(operation["number"]))
            return ("sent", "") if details["merged"] else ("failed", "")
    except Exception:
        logger.info("Could not reconcile %s.", action["id"], exc_info=True)
    return "uncertain", ""


async def pushed_under(shepherd: Shepherd, operation: dict, found: str) -> str:
    """The branch moved on to another commit: whether the push went out under it (the commit is
    in its history), read on GitHub. A commit GitHub doesn't know never went out."""
    from .sources.github import NOT_FOUND

    path = f"repos/{operation['head_repo']}/compare/{operation['commit']}...{found}"
    try:
        status = json.loads(await shepherd.runtime.github.gh("api", path)).get("status")
    except RuntimeError as exc:
        return "failed" if NOT_FOUND.search(str(exc)) else "uncertain"
    if status in {"ahead", "identical"}:
        return "sent"
    return "failed" if status in {"behind", "diverged"} else "uncertain"


async def reconcile_all(shepherd: Shepherd):
    """Operations left dispatching (a crash, turning off mid-send): read on GitHub, never sent
    again blindly."""
    for action in shepherd.control.actions("", DISPATCHING):
        state, link = await reconcile(shepherd, action)
        shepherd.control.finish(action["id"], state, "Reconciled after a stop.", link)
        if state == "sent" and action["payload"]["kind"] == "push":
            payload = action["payload"]
            shepherd.control.published(action["target"], payload["commit"])
        shepherd.finish_applying(action["step_id"])


# Merge when ready.


# What GitHub's merge state says it waits for (after "waiting for").
MERGE_STATES = {
    "BEHIND": "its branch to catch up", "DIRTY": "its conflicts to be resolved",
    "BLOCKED": "GitHub's merge rules to be met", "UNSTABLE": "checks to pass",
    "DRAFT": "it to be marked ready for review",
}  # fmt: skip


async def merge_blockers(shepherd: Shepherd, claim: dict, details: dict) -> list[str]:
    """What a PR enrolled in Merge when ready still waits for, each to follow "waiting for" in
    the row's line ([] when it merges now). Every condition read fresh, each on its own."""
    found = []
    head = details.get("head") or ""
    accepted = (claim.get("accepted") or {}).get("sha") or ""
    if not accepted:
        return ["your Accept"]
    if head != accepted:
        chain = await approved_chain(shepherd, claim, details, accepted, head)
        if not chain:
            found.append("you to accept this commit (it isn't the one you accepted)")
    if claim.get("state") == "paused":
        found.append("you to resume it")
    elif claim.get("holder_kind") != "task":
        found.append("you to look after this PR again")
    elif claim.get("state") != IDLE:
        found.append("what it's preparing")
    if details.get("draft"):
        found.append("it to be marked ready for review")
    if any(HOLD.search(label) for label in details.get("labels") or []):
        found.append("its hold label to come off")
    checks = details.get("checks") or []
    if any(check["status"] != "COMPLETED" for check in checks):
        found.append("checks to finish")
    if any(check.get("required") and check["conclusion"] not in {"SUCCESS", "NEUTRAL", "SKIPPED"}
           for check in checks):  # fmt: skip
        found.append("required checks to pass")
    ran = set((claim.get("accepted") or {}).get("checks") or [])
    if missing := ran - {check["name"] for check in checks}:
        found.append(f"{', '.join(sorted(missing)[:2])} to run on this commit")
    reviews = details.get("reviews") or []
    if not any(r["state"] == "APPROVED" and r["commit"] == head and r["author"] != details["author"]
               for r in reviews):  # fmt: skip
        found.append("an approval of this commit")
    if details.get("review") == "CHANGES_REQUESTED" or any(
        r["state"] == "CHANGES_REQUESTED" for r in reviews
    ):
        found.append("the requested changes to be addressed")
    if (state := details.get("merge_state") or "") not in {"CLEAN", "HAS_HOOKS"}:
        found.append(MERGE_STATES.get(state, "GitHub to say it can merge"))
    return list(dict.fromkeys(found))


async def approved_chain(shepherd: Shepherd, claim: dict, details: dict, accepted: str,
                         head: str) -> bool:  # fmt: skip
    """Whether every commit from the accepted head to this one is a head Chief of Staff pushed
    from a plan the owner approved."""
    approved = set(claim.get("approved") or [])
    if head not in approved:
        return False
    try:
        compare = json.loads(
            await shepherd.runtime.github.gh(
                "api", f"repos/{details['repo']}/compare/{accepted}...{head}"
            )
        )
    except Exception:
        return False
    if compare.get("status") != "ahead":
        return False
    # Each approved push is one commit on top of the last: every commit in between is one of
    # them, or an ancestor of one (a fix's own commits), and the last is the head.
    shas = [commit.get("sha") for commit in compare.get("commits") or []]
    marks = [index for index, sha in enumerate(shas) if sha in approved]
    return bool(shas) and shas[-1] == head and bool(marks)


async def try_merge(shepherd: Shepherd, target: str):
    """Merge an enrolled PR once nothing blocks it: the base's protection readable (and no merge
    queue), then a head-matched merge, journaled once per head. GitHub refuses if the head
    moved."""
    control = shepherd.control

    def waits(claim: dict, words: str):
        """Its row says why it hasn't merged ("Merges when ready: …")."""
        if shepherd.store.get(f"mergewhy:{target}") != words:
            shepherd.store.set(f"mergewhy:{target}", words)
            shepherd.publish_holder(claim)

    async with shepherd.lock(target):
        claim = control.claim(target)
        if not claim or not claim["merge_when_ready"] or claim["state"] != IDLE:
            return
        details = claim.get("details") or {}
        if blockers := await merge_blockers(shepherd, claim, details):
            waits(claim, f"waiting for {' and '.join(blockers[:2])}")  # The first two.
            return
        if any(
            step["kind"] == "merge" and step["h0"] == details["head"]
            and step["state"] in {PREPARED, APPLYING, "published"}
            for step in control.steps(target)
        ):  # fmt: skip
            return
        github = shepherd.runtime.github
        protected = await github.branch_protected(details["repo"], details["base_ref"])
        rules = await github.branch_rules(details["repo"], details["base_ref"])
        if protected is None or rules is None:
            waits(claim, "its base branch's rules couldn't be read")
            return
        if any(rule.get("type") == "merge_queue" for rule in rules):
            waits(claim, "its base branch merges through a queue, not supported yet")
            return
        if details.get("permission") not in {"ADMIN", "MAINTAIN", "WRITE"}:
            waits(claim, "your account can't merge it")
            return
        shepherd.store.delete(f"mergewhy:{target}")
        task = shepherd.runtime.tasks.find(claim["holder"])
        if not task:
            return
        step = control.add_step(
            target, "merge", "do", state=PREPARED, session_id=task["session_id"],
            task_id=task["id"], holder=claim["holder"], generation=claim["generation"],
            consent=control.generation(), h0=details["head"],
            plan={"kind": "merge", "head": details["head"]}, started_at=now(),
        )  # fmt: skip
        login = await github.acting_login(details["repo"])
        operation = {"kind": "merge", "repo": details["repo"], "number": details["number"],
                     "head": details["head"]}  # fmt: skip
        if control.journal(step["id"], claim["holder"], claim["generation"], control.generation(),
                           [operation], login):  # fmt: skip
            control.move_step(step["id"], PREPARED, state="withdrawn", ended_at=now())
            return
    await dispatch(shepherd, step["id"])
