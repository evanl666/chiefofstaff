"""A world for the shepherd's tests: a GitHub stand-in that answers the `gh` calls Chief of Staff
makes (its pull request read back from a real bare repository, so a push shows), the owner's
clone, a worker with a worktree on the PR's branch, its delivered task following the PR."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from conftest import Harness, settle

from chiefofstaff.backend import TESTED_CLI
from chiefofstaff.models import Settings
from chiefofstaff.roles import WORKER

REPO = "acme/api"
NUMBER = 12
TARGET = f"github:{REPO}#{NUMBER}"
BRANCH = "fix-login"
PYTHON = sys.executable


def run(*command: str, cwd: Path | str | None = None) -> str:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(cwd or "."),
            "PATH": __import__("os").environ["PATH"],
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    ).stdout.strip()


LOGIN = """def timeout() -> int:
    return 31
"""
TEST = """from app import timeout


def test_ok():
    assert True


def test_timeout():
    assert timeout() == 30
"""


def ran_line(line: str) -> subprocess.CompletedProcess:
    """A shell line, run as Copilot's shell tool runs it."""
    return subprocess.run(line, shell=True, capture_output=True, text=True, check=False)  # noqa: S602


class FakeGitHub:
    """What `gh` answers, from a little state and the bare repository."""

    def __init__(self, remote: Path):
        self.remote = remote
        self.calls: list[tuple] = []
        self.writes: list[tuple] = []
        self.state = "OPEN"
        self.merged = False
        self.draft = False
        self.review = ""
        self.reviews: list[dict] = []
        self.threads: list[dict] = []
        self.labels: list[str] = []
        self.merge_state = "CLEAN"
        self.permission = "WRITE"
        self.checks: dict[str, list[dict]] = {}  # head -> checks
        self.default_checks = [
            {
                "name": "ci / test",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "required": True,
                "run": 501,
                "id": 9001,
            }
        ]
        self.protected: bool | None = False
        self.rules: list | None = []
        self.base_rules: list | None = []
        self.base_protected: bool | None = True
        self.stacked = 0
        self.log = "FAILED tests/test_app.py::test_timeout - assert 31 == 30\n"
        self.failed_runs = [
            {
                "id": 501,
                "name": "ci",
                "conclusion": "failure",
                "run_attempt": 1,
                "html_url": "https://github.com/acme/api/actions/runs/501",
                "event": "pull_request",
                "path": ".github/workflows/ci.yml",
            }
        ]
        self.posted: list[dict] = []
        self.rest_reviews: list[dict] = []
        self.mine: list[dict] = []
        self.author = "me"
        self.head_repo = REPO
        self.fail_push_reads = False
        self.signed_out = False

    def head(self) -> str:
        return run("git", "rev-parse", f"refs/heads/{BRANCH}", cwd=self.remote)

    def base(self) -> str:
        return run("git", "rev-parse", "refs/heads/main", cwd=self.remote)

    def details(self) -> dict:
        head = self.head()
        checks = self.checks.get(head, self.default_checks)
        nodes = []
        for check in checks:
            nodes.append(
                {
                    "__typename": "CheckRun",
                    "name": check["name"],
                    "status": check["status"],
                    "conclusion": check.get("conclusion"),
                    "detailsUrl": "https://x",
                    "databaseId": check.get("id"),
                    "isRequired": check.get("required", True),
                    "checkSuite": {"workflowRun": {"databaseId": check.get("run")}},
                }
            )
        return {
            "data": {
                "repository": {
                    "databaseId": 77,
                    "nameWithOwner": REPO,
                    "viewerPermission": self.permission,
                    "defaultBranchRef": {"name": "main"},
                    "pullRequest": {
                        "number": NUMBER,
                        "state": self.state,
                        "isDraft": self.draft,
                        "merged": self.merged,
                        "title": "Fix the login timeout",
                        "url": f"https://github.com/{REPO}/pull/{NUMBER}",
                        "updatedAt": "2026-11-16T08:00:00Z",
                        "createdAt": "2026-11-15T08:00:00Z",
                        "author": {"login": self.author},
                        "headRefName": BRANCH,
                        "headRefOid": head,
                        "baseRefName": "main",
                        "baseRefOid": self.base(),
                        "isCrossRepository": self.head_repo != REPO,
                        "mergeStateStatus": self.merge_state,
                        "reviewDecision": self.review,
                        "headRepository": {
                            "databaseId": 77 if self.head_repo == REPO else 78,
                            "nameWithOwner": self.head_repo,
                            "defaultBranchRef": {"name": "main"},
                        },
                        "labels": {"nodes": [{"name": label} for label in self.labels]},
                        "reviewRequests": {"nodes": []},
                        "latestOpinionatedReviews": {"nodes": self.reviews},
                        "reviewThreads": {"nodes": self.threads},
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "oid": head,
                                        "statusCheckRollup": {
                                            "state": "FAILURE",
                                            "contexts": {"nodes": nodes},
                                        },
                                    }
                                }
                            ]
                        },
                    },
                }
            }
        }

    async def __call__(self, *arguments: str, payload: dict | None = None, token: str = "") -> str:
        self.calls.append((arguments, payload, token))
        await asyncio.sleep(0)
        text = " ".join(arguments)
        if text.startswith("auth status"):
            return json.dumps(
                {"hosts": {"github.com": [{"login": "me", "active": True, "state": "success"}]}}
            )
        if text.startswith("auth token"):
            if self.signed_out:
                raise RuntimeError("no oauth token found for github.com account me")
            return "tok-me\n"
        if text.startswith("api user"):
            return json.dumps({"login": "me"})
        if text.startswith("api graphql"):
            if "resolveReviewThread" in text:
                self.writes.append(("resolve", arguments))
                for thread in self.threads:
                    if f"id={thread['id']}" in arguments:
                        thread["isResolved"] = True
                return json.dumps(
                    {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}
                )
            return json.dumps(self.details())
        if text.startswith(f"api repos/{REPO}/pulls?base="):
            return json.dumps([{}] * self.stacked)
        if text.startswith(f"api repos/{REPO}/branches/main"):
            if self.base_protected is None:
                raise RuntimeError("GitHub request failed: HTTP 403")
            return json.dumps({"protected": self.base_protected})
        if text.startswith(f"api repos/{REPO}/branches/"):
            if self.protected is None:
                raise RuntimeError("GitHub request failed: HTTP 403")
            return json.dumps({"protected": self.protected})
        if text.startswith(f"api repos/{REPO}/rules/branches/main"):
            if self.base_rules is None:
                raise RuntimeError("GitHub request failed: HTTP 403")
            return json.dumps(self.base_rules)
        if text.startswith(f"api repos/{REPO}/rules/branches/"):
            if self.rules is None:
                raise RuntimeError("GitHub request failed: HTTP 403")
            return json.dumps(self.rules)
        if text.startswith(f"api repos/{REPO}/git/ref/heads/"):
            if self.fail_push_reads:
                raise RuntimeError("GitHub request failed: HTTP 502")
            return json.dumps({"object": {"sha": self.head()}})
        if text.startswith(f"api repos/{REPO}/actions/runs?head_sha="):
            return json.dumps({"workflow_runs": self.failed_runs})
        if text.startswith(f"api repos/{REPO}/actions/runs/") and "/jobs" in text:
            return json.dumps(
                {
                    "jobs": [
                        {
                            "id": 1,
                            "name": "test",
                            "conclusion": "failure",
                            "steps": [{"name": "pytest", "conclusion": "failure"}],
                        }
                    ]
                }
            )
        if text.startswith(f"api repos/{REPO}/actions/runs/"):
            return json.dumps({"run_attempt": 2})
        if text.startswith("run view"):
            return "".join(f"test\tpytest\t{line}\n" for line in self.log.splitlines())
        if text.startswith("run rerun"):
            self.writes.append(("rerun", arguments, token))
            return ""
        if text.startswith(f"api repos/{REPO}/collaborators/"):
            login = arguments[1].rsplit("/", 2)[-2]
            return json.dumps(
                {
                    "permission": "write" if login == "maya" else "read",
                    "role_name": "write" if login == "maya" else "read",
                }
            )
        if text.startswith(f"api repos/{REPO}/pulls/{NUMBER}/comments/") and "replies" in text:
            body = (payload or {}).get("body", "")
            reply = {
                "id": 7000 + len(self.posted),
                "body": body,
                "in_reply_to_id": int(arguments[1].split("/")[-2]),
                "html_url": f"https://github.com/{REPO}/pull/{NUMBER}#r{7000 + len(self.posted)}",
                "user": {"login": "me"},
            }
            self.posted.append(reply)
            self.writes.append(("reply", arguments, token, body))
            return json.dumps(reply)
        if text.startswith(f"api repos/{REPO}/pulls/{NUMBER}/comments"):
            return json.dumps(self.posted)
        if text.startswith(f"api repos/{REPO}/pulls/{NUMBER}/requested_reviewers"):
            if "--method=POST" in arguments:
                self.writes.append(("request-review", arguments, token, payload))
            return json.dumps({"users": []})
        if text.startswith(f"api repos/{REPO}/pulls/{NUMBER}/reviews"):
            return json.dumps(self.rest_reviews)
        if text.startswith(f"api repos/{REPO}/issues/{NUMBER}/comments"):
            return json.dumps([])
        if text.startswith(f"api repos/{REPO}/commits/") and "/check-runs" in text:
            checks = self.checks.get(arguments[1].split("/")[4], self.default_checks)
            return json.dumps(
                {
                    "check_runs": [
                        {
                            "id": check.get("id"),
                            "name": check["name"],
                            "status": check["status"].lower(),
                            "conclusion": (check.get("conclusion") or "").lower() or None,
                            "completed_at": "2026-11-16T08:05:00Z",
                        }
                        for check in checks
                    ]
                }
            )
        if text.startswith(f"api repos/{REPO}/commits/") and text.endswith("/status"):
            return json.dumps({"statuses": []})
        if text == f"api repos/{REPO}/pulls/{NUMBER}":
            return json.dumps(
                {
                    "number": NUMBER,
                    "title": "Fix the login timeout",
                    "state": "open" if self.state == "OPEN" else "closed",
                    "merged": self.merged,
                    "merged_at": "2026-11-16T09:00:00Z" if self.merged else None,
                    "closed_at": None if self.state == "OPEN" else "2026-11-16T09:00:00Z",
                    "head": {"sha": self.head(), "ref": BRANCH},
                    "base": {"sha": self.base(), "ref": "main"},
                    "mergeable_state": self.merge_state.lower(),
                    "user": {"login": self.author},
                }
            )
        if text.startswith(f"api repos/{REPO}/compare/"):
            spec = arguments[1].rsplit("/", 1)[-1]
            old, new = spec.split("...")
            try:
                run("git", "cat-file", "-e", f"{old}^{{commit}}", cwd=self.remote)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"GitHub request failed: HTTP 404: No commit found for {old}"
                ) from exc
            ahead = run("git", "rev-list", "--reverse", f"{old}..{new}", cwd=self.remote).split()
            behind = run("git", "rev-list", f"{new}..{old}", cwd=self.remote).split()
            status = (
                "diverged" if ahead and behind else "behind" if behind else
                "ahead" if ahead else "identical"
            )  # fmt: skip
            return json.dumps({"status": status, "commits": [{"sha": sha} for sha in ahead]})
        if text.startswith(f"api repos/{REPO}"):
            return json.dumps({"allow_squash_merge": True})
        if text.startswith("pr merge"):
            self.writes.append(("merge", arguments, token))
            if arguments[-1] != self.head():
                raise RuntimeError("GitHub request failed: Head branch was modified")
            self.merged = True
            self.state = "MERGED"
            return ""
        if text.startswith("search prs"):
            return json.dumps(self.mine)
        raise RuntimeError(f"unexpected gh call {arguments}")


class World:
    """The owner's clone of acme/api, its GitHub stand-in, and a worker whose delivered task
    made the PR and follows it."""

    def __init__(self, harness: Harness | None, root: Path, runtime=None):
        """`harness`: a test's (connected by start); or `runtime`, one running already (a page's,
        scripts/ui_edges.py)."""
        self.harness = harness
        self.runtime = harness.runtime if harness else runtime
        self.root = root
        self.remote = root / "remote.git"
        self.clone = root / "code" / "api"
        self.github = FakeGitHub(self.remote)

    def build(self):
        seed = self.root / "seed"
        seed.mkdir(parents=True)
        (seed / "app.py").write_text(LOGIN)
        (seed / "tests").mkdir()
        (seed / "tests" / "test_app.py").write_text(TEST)
        run("git", "init", "-q", "-b", "main", cwd=seed)
        run("git", "add", "-A", cwd=seed)
        run("git", "commit", "-qm", "init", cwd=seed)
        run("git", "init", "-q", "--bare", "-b", "main", str(self.remote), cwd=self.root)
        run("git", "push", "-q", str(self.remote), "main", cwd=seed)
        run("git", "checkout", "-qb", BRANCH, cwd=seed)
        (seed / "app.py").write_text(LOGIN + "\n\ndef login():\n    return timeout()\n")
        run("git", "commit", "-qam", "Add login", cwd=seed)
        run("git", "push", "-q", str(self.remote), BRANCH, cwd=seed)
        self.clone.parent.mkdir(parents=True)
        run("git", "clone", "-q", str(self.remote), str(self.clone), cwd=self.root)
        run("git", "remote", "set-url", "origin", f"https://github.com/{REPO}.git", cwd=self.clone)
        self.h0 = self.github.head()

    async def start(
        self, *, budget: float = 0.0, levels: dict | None = None, worker: bool = True, **settings
    ):
        """On, with the probe passed; `worker`: the worker whose task made the PR (else the PR
        is one of the owner's others, for discovery)."""
        self.build()
        runtime = self.runtime
        runtime.github.raw = self.github
        runtime.github.gh = runtime.github.routed
        runtime.github.accounts_cache = (0.0, [])
        if self.harness:
            await self.harness.connect()
        else:
            # A running app's own reads of gh (this computer's, signed in as nobody) may still
            # be out: they must not say the owner isn't signed in.
            async def signed_in(fresh: bool = False) -> list[dict]:
                return [{"login": self.github.author, "active": True}]

            runtime.github.signed_in = signed_in
        runtime.shepherd.git_config = [
            (f"url.{self.remote.as_uri()}.insteadOf", f"https://github.com/{REPO}.git")
        ]
        runtime.store.set(f"stepprobe:{TESTED_CLI}", json.dumps({"passed": True}))
        if worker:
            self.worker = await self.make_worker()
        old = runtime.store.settings()
        new = old.model_copy(
            update={
                "proactive": True,
                "proactive_budget": budget,
                "proactive_levels": levels or {},
                "proactive_my_prs": False,
                "code_folder": str(self.clone.parent),
                **settings,
            }
        )
        runtime.store.save_settings(new)
        await runtime.settings_changed(old, new)
        await settle(runtime)
        return runtime

    async def make_worker(self) -> str:
        runtime = self.runtime
        store = runtime.store
        session_id = "11111111-2222-3333-4444-555555555555"
        workspace = self.root / "work" / "fix-login"
        tree = workspace / "api"
        workspace.mkdir(parents=True)
        run(
            "git",
            "worktree",
            "add",
            "-q",
            "-b",
            "chief/fix-login",
            str(tree),
            f"origin/{BRANCH}",
            cwd=self.clone,
        )
        admin = run("git", "rev-parse", "--absolute-git-dir", cwd=tree)
        checkout = {
            "project": str(self.clone),
            "repo": str(self.clone),
            "worktree": str(tree),
            "path": str(tree),
            "branch": "chief/fix-login",
            "base": "origin/main",
            "base_ref": self.h0,
            "git_dir": str(self.clone / ".git"),
            "admin": admin,
        }
        store.create_session(
            session_id,
            WORKER,
            "Fix the login timeout",
            "idle",
            cwd=str(tree),
            branch="chief/fix-login",
        )
        store.update_session(session_id, repos=json.dumps([checkout]), project=str(self.clone))
        store.set(f"workspace:{session_id}", str(workspace))
        await runtime.open_live(store.session(session_id), create=True)
        await runtime.detach(session_id)
        task = runtime.tasks.create(
            session_id, "Fix the login timeout", "Fix it.", status="running"
        )
        self.task = task["id"]
        runtime.task_actions.record_opened(task["id"], [f"https://github.com/{REPO}/pull/{NUMBER}"])
        evidence = {
            "checkouts": [{"path": str(tree), "branch": "chief/fix-login", "commit": self.h0}],
            "prs": [
                {
                    "label": f"{REPO}#{NUMBER}",
                    "url": f"https://github.com/{REPO}/pull/{NUMBER}",
                    "state": "OPEN",
                    "own": True,
                    "role": "created",
                    "about": True,
                    "author": "me",
                    "head": self.h0,
                    "branch": BRANCH,
                    "checks": "failing",
                    "failing": ["ci / test"],
                    "merge_state": "CLEAN",
                    "review": "",
                    "at": "2026-11-16T08:00:00+00:00",
                }
            ],
        }
        run_id = store.value("SELECT id FROM runs WHERE task_id=?", (task["id"],))
        store.execute("UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run_id))
        runtime.follow(session_id, [TARGET], task_id=task["id"], asked=[TARGET])
        runtime.tasks.move(task["id"], "review", result="Fixed it: PR acme/api#12.")
        return session_id

    def session(self):
        store, client = self.runtime.store, self.runtime.backend
        return client.sessions[store.get(f"copilot:{self.worker}") or self.worker]

    def commit_fix(self, tree: str) -> str:
        path = Path(tree) / "app.py"
        path.write_text(path.read_text().replace("return 31", "return 30"))
        run("git", "commit", "-qam", "Fix the timeout", cwd=tree)
        return run("git", "rev-parse", "HEAD", cwd=tree)

    async def ran(self, line: str, call: str):
        """The worker ran this command line (as Copilot's shell tool would), for real."""
        session = self.session()
        session.emit(
            "tool.execution_start", tool_name="bash", tool_call_id=call, arguments={"command": line}
        )
        done = await asyncio.to_thread(ran_line, line)
        session.emit(
            "tool.execution_complete",
            tool_call_id=call,
            success=done.returncode == 0,
            result=SimpleNamespace(content=done.stdout[-500:]),
            shell_execution=SimpleNamespace(exit_code=done.returncode),
        )
        await settle(self.runtime)
        return done.returncode


def harness_for(tmp_path: Path, **settings) -> Harness:
    return Harness(tmp_path / "data", Settings(**settings) if settings else None)


TEST_COMMAND = f"{PYTHON} -m pytest -q -p no:cacheprovider"


async def seed_page(runtime, root: Path) -> dict:
    """Prepared for you as the page shows it (scripts/ui_edges.py, ui_quality.py): a worker PR
    with a fix prepared for review (made the real way: an attempt in its worker, checked on
    frozen commits), one paused by a teammate's push, one its computer can't run steps for, and
    three of the owner's other PRs found, two more under All found."""
    from test_shepherd import fixed

    from chiefofstaff.control import PREPARED

    world = World(None, root, runtime=runtime)
    # Room for its attempt beside whatever the page's other workers are at.
    room = max(8, runtime.workers.running() + 4)
    await world.start(proactive_my_prs=True, max_workers=room)
    shepherd = runtime.shepherd
    await shepherd.tick()
    await settle(runtime)
    step, sha = await fixed(world, runtime)
    # Once pushed, its CI runs (as GitHub has it right after a push).
    world.github.checks[sha] = [
        {"name": "ci / test", "status": "IN_PROGRESS", "conclusion": "", "required": True}
    ]
    store = runtime.store
    extra = {}
    for title, label in (
        ("Add the export endpoint", "api#31"),
        ("Speed up the search index", "api#44"),
    ):
        session_id = f"22222222-2222-4222-8222-{len(extra) + 1:012d}"
        store.create_session(session_id, WORKER, title, "idle", cwd=str(world.clone))
        task = runtime.tasks.create(session_id, title, title, status="running")
        target = f"github:acme/api#{label.split('#')[1]}"
        runtime.follow(session_id, [target], task_id=task["id"], asked=[target])
        runtime.tasks.move(task["id"], "done", result=f"{title}: PR acme/{label}.")
        shepherd.control.give(target, task["id"], "a" * 40)
        extra[label] = (task["id"], target)
    paused_task, paused = extra["api#31"]
    shepherd.control.pause(paused, "new commits it didn't push (9f8e7d6)", "9f8e7d6" + "0" * 33)
    refused_task, refused = extra["api#44"]
    claim = shepherd.control.claim(refused)
    shepherd.control.add_step(
        refused, "fix-ci", "suggest", state="refused", holder=claim["holder"],
        generation=claim["generation"], h0="a" * 40, task_id=refused_task, started_at="",
        why="its worker runs on Codex, not Copilot",
    )  # fmt: skip

    def pr(repo: str, number: int, title: str, **more) -> dict:
        url = f"https://github.com/{repo}/pull/{number}"
        label = f"{repo.split('/')[1]}#{number}"
        return {"target": f"github:{repo}#{number}", "repo": repo, "number": number,
                "label": label, "title": title, "url": url, **more}  # fmt: skip

    found = [
        pr("acme/web", 117, "Show the export button to admins only", kind="fix-ci",
           what="CI failing since Tue", updated="2026-11-16T08:00:00Z"),
        pr("acme/api", 120, "Retry the payment webhook on 5xx", kind="address-review",
           what="Changes requested by maya", updated="2026-11-16T07:00:00Z"),
        pr("acme/web", 121, "Upgrade the date picker", kind="address-review",
           what="2 comments to answer", updated="2026-11-15T07:00:00Z"),
        pr("acme/web", 90, "Move the settings page to the new layout", kind="fix-ci",
           what="CI failing since 3 Nov", updated="2026-11-03T07:00:00Z"),
    ]  # fmt: skip
    others = [
        pr("acme/web", 88, "Translate the onboarding emails", why="from a fork: not yet"),
        pr("acme/api", 99, "Split the billing module, part 2", why="stacked: not yet"),
    ]
    seen = {"read": time.time(), "rows": found, "others": others, "scope": ["acme/api", "acme/web"]}
    store.set("prepared:found", json.dumps(seen))
    shepherd.publish()
    for task_id in (world.task, paused_task, refused_task):
        runtime.task_actions.publish(runtime.tasks.get(task_id))
    assert shepherd.control.step(step["id"])["state"] == PREPARED
    return {"world": world, "step": step, "paused": paused_task, "refused": refused_task}
