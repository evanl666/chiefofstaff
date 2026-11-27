"""Demo mode: the whole product, with scripted sessions instead of a model.

`chiefofstaff --demo` needs no Copilot or Slack account. It makes a small git project in a
temporary folder and plays a scenario through the real runtime: the Router splits a request
into tasks, one task waits for another and builds on its result, a worker asks the owner a
question, a Slack message gets a reply drafted for approval, the Watchdog unsticks a stalled
worker, and the finished work waits for review with real commits and test runs. Anything the
owner types gets a simple scripted run too.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx

from . import agents
from .bus import Bus
from .models import Settings
from .runtime import AgentConnection, Runtime, described
from .scripted import (
    AgentLikeBackend,
    CallTool,
    Profile,
    Say,
    ScriptedBackend,
    ScriptedSession,
    Shell,
    Wait,
    WriteFile,
    agent_capabilities,
)
from .shares import Shares
from .sources.github import GitHubError, GitHubSource
from .sources.slack import SlackSource
from .store import Store
from .watchdog import WatchConfig

KICKOFF = (
    "Fix the flaky login test in acme-api, then have Codex add rate limiting to login attempts. "
    "Also ask Claude to compare a few feature-flag services for us."
)
MAYA_DM = "D0DEMOMAYA"
# What the demo's Codex and Claude use, as Settings says it: nothing.
SCRIPTED = "Scripted for the demo: nothing to sign in to, nothing used"
# Codex and Claude, scripted as well and ready as if installed and signed in, so the demo has a
# worker on each of the three agents. As their adapters show them (backend_codex.CODEX,
# backend_claude.CLAUDE): both steer and give exit codes; Codex chooses its threads' ids.
DEMO_AGENTS = {
    "codex": Profile(agent_capabilities("Codex", steering=True), exit_codes=True, assigns_ids=True),
    "claude": Profile(
        agent_capabilities("Claude", steering=True), exit_codes=True, assigns_ids=False
    ),
}
# The agent the kickoff names for its tasks (the other one runs where new workers start).
NAMED = {"Rate-limit login attempts": "codex", "Compare feature-flag services": "claude"}
ENVELOPE = re.compile(r"<message ([^>]*)>\n(.*?)\n</message>", re.DOTALL)
ATTRIBUTE = re.compile(r'(\w+)="([^"]*)"')

LOGIN = '''"""Login with expiring tokens."""

import time
from datetime import UTC, datetime

TOKEN_SECONDS = 60


def issue(user: str, now=time.time) -> dict:
    return {"user": user, "expires": now() + TOKEN_SECONDS}


def valid(token: dict, now=time.time) -> bool:
    return now() < token["expires"]
'''
LOGIN_FIXED = LOGIN.replace(
    '"""Login with expiring tokens."""',
    '"""Login with expiring tokens. The clock is passed in, so tests do not race it."""',
)
LOGIN_TEST = """from app.login import issue, valid


def test_a_fresh_token_is_valid():
    assert valid(issue("ann"))
"""
RATELIMIT = '''"""At most five login attempts a minute, per key (an IP address or an account)."""

import time
from datetime import UTC, datetime
from collections import defaultdict, deque

LIMIT, WINDOW = 5, 60.0
_attempts: dict[str, deque] = defaultdict(deque)


def allow(key: str, now=time.time) -> bool:
    moment, recent = now(), _attempts[key]
    while recent and moment - recent[0] > WINDOW:
        recent.popleft()
    if len(recent) >= LIMIT:
        return False
    recent.append(moment)
    return True
'''


def envelopes(prompt: str) -> list[tuple[dict, str]]:
    return [
        (dict(ATTRIBUTE.findall(head)), body.replace("&lt;", "<"))
        for head, body in ENVELOPE.findall(prompt)
    ]


def role(session: ScriptedSession) -> str:
    prompt = session.system_prompt
    if "Router inside Chief of Staff" in prompt:
        return "router"
    if "Slack session inside Chief of Staff" in prompt:
        return "slack"
    if "Chief of Staff's Watchdog" in prompt:
        return "watchdog"
    return "worker"


def make_project(root: Path) -> Path:
    """A small git repository for the demo's workers to change."""
    project = root / "acme-api"
    if project.exists():
        return project
    (project / "app").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "README.md").write_text(
        "# acme-api\n\nA small login service (Chief of Staff demo).\n"
    )
    (project / "app" / "__init__.py").write_text("")
    (project / "app" / "login.py").write_text(LOGIN)
    (project / "tests" / "test_login.py").write_text(LOGIN_TEST)
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.name", "Demo Owner"],
        ["git", "config", "user.email", "owner@example.com"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "Initial commit"],
    ):
        subprocess.run(command, cwd=project, check=True, capture_output=True)  # noqa: S603  (fixed)
    return project


class DemoBrain:
    def __init__(self, project: Path, store: Store | None = None):
        self.project = str(project)
        self.store = store
        self.researchers: set[str] = set()  # Workers on the task that stalls on purpose.
        self.reported: set[str] = set()

    def on_agent(self, title: str) -> dict:
        """The agent the owner named for a kickoff task, while the demo's agents are on."""
        if title not in NAMED or not self.store or NAMED[title] not in agents.turned_on(self.store):
            return {}
        return {"agent": NAMED[title]}

    def __call__(self, session: ScriptedSession, prompt: str):
        script = {
            "router": self.router,
            "slack": self.slack,
            "watchdog": self.watchdog,
        }.get(role(session), self.worker)
        return script(session, prompt)

    async def router(self, session, prompt):
        said = False
        for attrs, body in envelopes(prompt):
            sender, kind = attrs.get("from", ""), attrs.get("type", "")
            if sender == "owner" and kind == "message":
                said = True
                if "flaky login test" in body:
                    first = json.loads(
                        (
                            yield CallTool(
                                "spawn_worker",
                                {
                                    "title": "Fix the flaky login test",
                                    "instructions": "The login tests fail now and then. Find out "
                                    "why and fix it.",
                                    "project": self.project,
                                    "isolate": True,
                                    "done_when": [
                                        "`uv run pytest -q` passes",
                                        "The login tests pass 20 runs in a row",
                                    ],
                                },
                            )
                        )
                    )
                    yield CallTool(
                        "spawn_worker",
                        {
                            "title": "Rate-limit login attempts",
                            **self.on_agent("Rate-limit login attempts"),
                            "instructions": "Limit login attempts. Build on the login fix.",
                            "project": self.project,
                            "isolate": True,
                            "after": [first["task_id"]],
                            "why_new": "A separate change with its own branch, after the fix.",
                            "done_when": [
                                "`uv run pytest -q` passes",
                                "The sixth attempt within a minute is refused",
                            ],
                        },
                    )
                    yield CallTool(
                        "spawn_worker",
                        {
                            "title": "Compare feature-flag services",
                            **self.on_agent("Compare feature-flag services"),
                            "instructions": "Compare three feature-flag services for a small "
                            "team: price, SDKs, self-hosting.",
                            "why_new": "Research, not about the login code.",
                        },
                    )
                    yield Say(
                        "Started three tasks: the flaky login test, rate limiting on Codex once "
                        "the fix is in, and a feature-flag comparison on Claude."
                    )
                    continue
                if "change of plan" in body.lower():
                    listed = json.loads((yield CallTool("list_sessions", {})))
                    earlier = next(
                        (d for d in listed["decisions"] if "login attempts" in d["decision"]), None
                    )
                    if earlier:
                        yield CallTool(
                            "record_decision",
                            {
                                "decision": "Limit login attempts per IP address only",
                                "replaces": [earlier["id"]],
                            },
                        )
                        yield Say(
                            "Changed the decision to per IP address only. The rate-limiting "
                            "result was out of date, so its worker is redoing it."
                        )
                        continue
                title = " ".join(body.split()[:7]).rstrip(".,!?") or "Task"
                code = re.search(
                    r"\b(fix|bug|test|code|add|refactor|repo|api)\b", body, re.IGNORECASE
                )
                arguments = {"title": title[:60], "instructions": body}
                if code:
                    arguments |= {"project": self.project, "isolate": True}
                result = yield CallTool("spawn_worker", arguments)
                if result.startswith("Error: Related workers exist"):
                    related = json.loads(result.split("\n", 1)[1])[0]
                    yield CallTool(
                        "message_session",
                        {
                            "session_id": related["session_id"],
                            "text": body,
                            "task": "new",
                            "title": title[:60],
                            "why_new": "Another request about the code it knows.",
                        },
                    )
                    yield Say(f"Gave it to “{related['title']}”, which already knows this code.")
                else:
                    yield Say(f"Started a worker on “{title}”.")
            elif sender.startswith("worker:") and kind == "report":
                # A finished task reaches the owner by itself (its report, its card on Home):
                # nothing to repeat. What needs the owner is said once more, to be sure.
                status = attrs.get("status")
                if status in {"question", "blocked", "failed"}:
                    yield CallTool(
                        "notify_owner",
                        {"text": f"“{attrs.get('title')}”: {body.strip()}", "needs_action": True},
                    )
            elif sender == "slack" and kind == "event":
                sessions = json.loads((yield CallTool("list_sessions", {})))["sessions"]
                slack = next((s for s in sessions if s["role"] == "slack"), None)
                if slack:
                    yield CallTool(
                        "message_session", {"session_id": slack["session_id"], "text": body}
                    )
        if not said:
            return  # Events need no closing words; results reached the owner as updates.

    async def slack(self, session, prompt):
        for _attrs, body in envelopes(prompt):
            if "Maya" in body:
                yield CallTool(
                    "draft_slack_reply",
                    {
                        "channel_id": MAYA_DM,
                        "thread_ts": "",
                        "to": "Maya Chen",
                        "text": "Yes! The flaky login test is fixed and waiting for review, and "
                        "rate limiting lands right after. I'll ping you when it ships.",
                        "note": "Maya asks whether the login fix goes out today.",
                    },
                )
                yield Say("Drafted a reply for Maya.")
                return
        yield Say("Nothing to reply to.")

    async def watchdog(self, session, prompt):
        for attrs, _body in envelopes(prompt):
            if attrs.get("type") == "review":
                yield CallTool(
                    "assess_worker",
                    {
                        "session_id": attrs.get("worker", ""),
                        "verdict": "stuck",
                        "action": "nudge",
                        "reason": "No activity for a while after reading the docs; it is "
                        "waiting on nothing.",
                        "message": "You have enough material. Write up the comparison now and "
                        "report it.",
                    },
                )
        yield Say("Reviewed.")

    async def worker(self, session, prompt):
        for attrs, body in envelopes(prompt):
            sender, kind = attrs.get("from", ""), attrs.get("type", "")
            title = attrs.get("title", "")
            if sender == "router" and kind == "task":
                if title == "Fix the flaky login test":
                    yield Shell(
                        "uv run pytest -q tests/test_login.py",
                        "F\n1 failed in 0.21s (token expired: clock moved during the test)",
                        1,
                        seconds=2,
                        note="Reproducing the flaky test first.",
                    )
                    yield Wait(2)
                    yield WriteFile("app/login.py", LOGIN_FIXED)
                    yield Shell(
                        "uv run pytest -q",
                        "........\n8 passed in 0.29s",
                        seconds=2,
                        note="The token expiry reads the real clock. Passing the clock in, then "
                        "running every test:",
                    )
                    yield Shell(
                        "uv run pytest -q --count 20 tests/test_login.py",
                        "....................\n20 passed in 0.41s",
                        seconds=1,
                    )
                    yield Shell(
                        "git add -A && git commit -qm 'Fix flaky login test' "
                        "&& git log --oneline -1",
                        run=True,
                    )
                    # What the rate limiting, which builds on this fix, needs to know.
                    found = json.loads((yield CallTool("find_sessions", {"query": "Rate-limit"})))
                    for match in found.get("matches", []):
                        if match.get("title") == "Rate-limit login attempts":
                            yield CallTool(
                                "tell_session",
                                {"session_id": match["session_id"], "text": TOLD_RATE_LIMITING},
                            )
                    yield CallTool(
                        "report",
                        {
                            "status": "done",
                            "task_id": attrs.get("task", ""),
                            "summary": "Fixed the flaky login test; all tests pass.\n"
                            "- Cause: token expiry read the real clock, so slow runs failed\n"
                            "- Fix: `app/login.py` takes the clock as a parameter\n"
                            "- `uv run pytest -q`: 8 passed\n- Committed on my branch",
                            "criteria": [
                                {
                                    "criterion": "`uv run pytest -q` passes",
                                    "result": "met",
                                    "evidence": "`uv run pytest -q`: 8 passed",
                                },
                                {
                                    "criterion": "The login tests pass 20 runs in a row",
                                    "result": "met",
                                    "evidence": "`uv run pytest -q --count 20 "
                                    "tests/test_login.py`: 20 passed",
                                },
                            ],
                        },
                    )
                elif title == "Learn how you work":
                    yield CallTool("propose_lessons", {"lessons": demo_lessons(body)})
                elif title == "Rate-limit login attempts":
                    yield CallTool(
                        "ask_decision",
                        {
                            "question": "Limit login attempts per IP address or per account?",
                            "why": "Per IP misses attackers who rotate addresses; per account "
                            "lets anyone lock a user out.",
                            "options": [
                                {"choice": "Per IP address", "effect": "Simple; offices share IPs"},
                                {"choice": "Per account", "effect": "Stops guessing one password"},
                                {"choice": "Both", "effect": "Safest; one more counter"},
                            ],
                            "recommendation": "Per account: it protects each user directly",
                        },
                    )
                    yield Say("Asked the owner how to limit login attempts.")
                elif title == "Compare feature-flag services":
                    self.researchers.add(session.session_id)
                    yield Shell(
                        "curl -s https://example.com/feature-flags/pricing | head -40",
                        "(pricing tables for three services)",
                        seconds=1,
                    )
                    yield Wait(60)  # Stalls here until the Watchdog steps in.
                    if session.session_id not in self.reported:
                        yield research_report(attrs.get("task", ""))
                else:
                    yield Shell("ls -la", "README.md  app  tests", seconds=1)
                    yield Wait(3)
                    yield Shell("grep -rn TODO . | head", "", seconds=1)
                    yield CallTool(
                        "report",
                        {
                            "status": "done",
                            "task_id": attrs.get("task", ""),
                            "summary": f"Looked into it: {body.strip()[:200]}\n- (Demo: a "
                            "scripted worker; with Copilot a real session does the work.)",
                        },
                    )
                return
            if sender == "watchdog" and kind == "nudge":
                if session.session_id in self.researchers:
                    self.reported.add(session.session_id)
                    yield Say("Right, writing it up now.")
                    yield CallTool("report", {"status": "done", "summary": FLAGS_REPORT})
                else:
                    yield Say("Still on it; this step takes a moment.")
                return
            if sender == "owner" and kind == "decision":
                choice = body.rsplit("The owner decided:", 1)[-1].strip() or "Per IP address"
                for step in rate_limiting(attrs.get("task", ""), choice):
                    yield step
                return
            if kind == "decision-changed":
                yield CallTool(
                    "ack_decision",
                    {
                        "decision": attrs.get("decision", ""),
                        "plan": "Key the limiter on the IP address only.",
                    },
                )
                for step in rate_limiting(attrs.get("task", ""), "Per IP address only"):
                    yield step
                return
            if sender == "owner" or (sender == "router" and kind == "message"):
                yield Wait(2)
                yield CallTool(
                    "report",
                    {
                        "status": "done",
                        "task_id": attrs.get("task", ""),
                        "summary": f"Done: {body.strip()[:200]}",
                    },
                )
                return
        yield Say("OK.")


TOLD_RATE_LIMITING = (
    "Heads-up: `app/login.py` now takes the clock as a parameter (`now=time.time`). Give the "
    "rate limiter the same parameter, so its tests don't race the real clock."
)
FLAGS_REPORT = (
    "Compared three feature-flag services for a small team.\n"
    "- **Hosted A**: free up to 10 seats, SDKs for all our languages, no self-hosting\n"
    "- **Hosted B**: usage pricing, best experimentation tools\n"
    "- **Open source C**: self-hosted, free, needs a Postgres\n"
    "- Recommendation: start with A; move to C if we need data on our servers"
)


def demo_lessons(prompt: str) -> list[dict]:
    """What the demo learns from the owner's feedback, citing the feedback it rests on."""
    feedback = [json.loads(line) for line in prompt.splitlines() if line.startswith('{"id"')]
    feedback = [item for item in feedback if "owner" in item]  # Not the owner's other words.

    def cited(*words: str) -> list[int]:
        return [item["id"] for item in feedback if any(w in item["owner"] for w in words)]

    lessons = [
        {
            "lesson": "In vendor comparisons, include pricing for a team of 50",
            "scope": "everywhere",
            "feedback": cited("pricing"),
        },
        {
            "lesson": "Rate-limit by IP address unless told otherwise",
            "scope": "everywhere",
            "feedback": cited("IP address"),
        },
    ]
    return [lesson for lesson in lessons if lesson["feedback"]]


def rate_limiting(task_id: str, choice: str) -> list:
    """The rate-limiting work, once the owner decided how (or changed it)."""
    return [
        WriteFile("app/ratelimit.py", RATELIMIT),
        Shell("uv run pytest -q", "...........\n11 passed in 0.33s", seconds=2),
        Shell(
            "git add -A && git commit -qm 'Rate-limit login attempts' && git log --oneline -1",
            run=True,
        ),
        CallTool(
            "report",
            {
                "status": "done",
                "task_id": task_id,
                "summary": f"Added rate limiting {choice[0].lower()}{choice[1:]}: five attempts "
                "a minute.\n- New `app/ratelimit.py`, used by login\n- Built on the login fix "
                "(the clock is injected)\n- 11 tests pass",
                "criteria": [
                    {
                        "criterion": "`uv run pytest -q` passes",
                        "result": "met",
                        "evidence": "`uv run pytest -q`: 11 passed",
                    },
                    {
                        "criterion": "The sixth attempt within a minute is refused",
                        "result": "met",
                        "evidence": "The limiter allows five per window, so the sixth is refused.",
                    },
                ],
            },
        ),
    ]


DEMO_SCALA = (
    "services/matching/src/main/scala/com/acme/openconnect/pysparkjobs/company_matching/"
    "normalizers/CompanyNameNormalizer.scala"
)
DEMO_REVIEW = {
    "repo": "acme/matching",
    "number": 42,
    "url": "https://github.com/acme/matching/pull/42",
    "via": "task",
    "head_sha": "c0ffee",
    "verdict": "request_changes",
    "pr": {
        "title": "Match companies by normalized name",
        "author": "maya",
        "about": "Adds a normalizer for company names (case, punctuation, legal suffixes such as "
        "Inc. and GmbH) and uses it in the nightly matching job, so the same company spelled "
        "two ways is matched once.",
        "additions": 214,
        "deletions": 37,
        "files": 6,
    },
    "comments": [
        {
            "path": DEMO_SCALA,
            "line": 48,
            "side": "RIGHT",
            "body": "This drops every non-Latin character, so 株式会社トヨタ normalizes to an "
            "empty string and matches every other empty name. Keep letters from all scripts.",
            "context": '     val cleaned = name.toLowerCase\n-    .replaceAll("[^a-z ]", "")\n'
            '+    .replaceAll("[^a-z0-9 ]", "").trim  // only ASCII letters and digits are kept',
        },
        {
            "path": "services/matching/jobs/nightly_match.py",
            "line": 112,
            "side": "RIGHT",
            "body": "Joining on the domain alone fans out when two companies share one "
            "(gmail.com): join on the normalized name too.",
            "context": '+    matches = companies.join(accounts, on="domain")',
        },
        {
            "path": "services/matching/jobs/nightly_match.py",
            "start_line": 140,
            "line": 146,
            "side": "LEFT",
            "body": "Was removing the dedupe step on purpose?",
            "context": '-    matches = matches.dropDuplicates(["company_id"])',
        },
    ],
}
DEMO_REVIEW_BODY = (
    "Good direction. Two things before this merges: the normalizer drops non-Latin names, and "
    "the join on domain alone can fan out."
)


def research_report(task_id: str) -> CallTool:
    """The research result (reported after a nudge, or anyway when no nudge came)."""
    return CallTool("report", {"status": "done", "task_id": task_id, "summary": FLAGS_REPORT})


class DemoSlackMcp:
    """Slack without Slack: one DM from Maya arrives a little after the demo starts."""

    url = "https://slack.demo.invalid/mcp"

    def __init__(self, delay: float = 18):
        self.ready_at = time.time() + delay
        self.delivered = False
        self.sent: list[dict] = []
        # Maya's message's ts, as Slack stamps one (seconds and six digits): read back with it.
        self.stamp = f"{int(self.ready_at)}.000200"

    async def list_tools(self):
        def tool(name, read_only=True, description=""):
            return SimpleNamespace(
                name=name,
                description=description,
                annotations=SimpleNamespace(readOnlyHint=read_only),
            )

        return [
            tool(
                "slack_search_public_and_private",
                description="Current user's user_id is U0DEMOYOU.",
            ),
            tool("slack_read_thread"),
            tool("slack_read_channel"),
            tool("slack_send_message", read_only=False),
        ]

    async def call(self, name: str, arguments: dict) -> str:
        if name == "slack_send_message":
            self.sent.append(arguments)
            stamp = f"{int(time.time())}000100"
            return (
                f"Message sent: https://demo.slack.com/archives/{arguments['channel_id']}/p{stamp}"
            )
        if name == "slack_search_public_and_private":
            if (
                arguments.get("query") == "to:me"
                and not self.delivered
                and time.time() >= self.ready_at
            ):
                self.delivered = True
                self.stamp = f"{int(time.time())}.000200"
                return (
                    "Results:\n1. DM from Maya Chen: hey! is the login fix going out today? "
                    "support keeps asking\n"
                    f"   https://demo.slack.com/archives/{MAYA_DM}/p{self.stamp.replace('.', '')}"
                )
            return "No results found."
        return (
            f"Channel: <#{MAYA_DM}> | From: <@U0MAYA01> (Maya Chen) | ts: {self.stamp}\n"
            "Text: hey! is the login fix going out today? support keeps asking"
        )


class DemoTokens:
    def get(self) -> str:
        return "xoxp-demo"

    def save(self, token: str):
        pass

    def delete(self):
        pass


# The demo's GitHub account, and what its share dialogs and links say (QA U6).
DEMO_LOGIN = "you"
DEMO_ONLY = (
    "In the demo, only links made in this demo open: nothing leaves this machine, so a link "
    "from anywhere else isn't fetched."
)


class DemoGitHub:
    """GitHub as the demo has it, in place of `gh` (GitHubSource's gh_command): one account
    signed in, no repository private, nobody posted anything, and gists kept here (a share link's
    card, sealed as for real). What else is asked of it fails, saying so: nothing runs `gh` or
    leaves this machine (QA U4)."""

    def __init__(self):
        # Its gists: a link's sealed card, by gist id ("" once deleted: the link was revoked).
        self.gists: dict[str, str] = {}

    async def __call__(self, *arguments: str, payload: dict | None = None, token: str = "") -> str:
        said = [word for word in arguments if word not in {"--method", "--paginate", "--input"}]
        if said[:2] == ["auth", "status"]:
            found = [{"login": DEMO_LOGIN, "active": True, "state": "success"}]
            return json.dumps({"hosts": {"github.com": found}})
        if said[:2] == ["auth", "token"]:
            return "demo"
        if said[:2] == ["api", "user"]:
            return json.dumps({"login": DEMO_LOGIN, "id": 1})
        if said[:3] == ["api", "gists", "POST"] and payload:
            gist = uuid4().hex
            self.gists[gist] = payload["files"]["share.txt"]["content"]
            return json.dumps({"id": gist})
        if said[:2] == ["api", "DELETE"] and said[2].startswith("gists/"):
            if (gist := said[2].removeprefix("gists/")) in self.gists:
                self.gists[gist] = ""
            return ""
        if said[:2] == ["api", "gists?per_page=30"]:
            return "[]"
        if len(said) > 1 and said[0] == "api" and said[1].startswith("repos/"):
            # Its repositories are public; nobody posted on its PRs.
            return "false" if "--jq" in said else "[]"
        raise GitHubError("The demo has no GitHub of its own: nothing leaves this machine.")


class DemoShares(Shares):
    """Share links in the demo: sealed as for real, kept in the demo's GitHub, so a link opens
    here (pasted on Home) until it is revoked or expires; the dialogs say it is the demo's."""

    @property
    def gists(self) -> dict[str, str]:
        return self.runtime.github.raw.gists  # type: ignore[attr-defined]

    async def read_gist(self, gist_id: str) -> tuple[int, httpx.Headers, bytes]:
        if gist_id not in self.gists:
            raise ValueError(DEMO_ONLY)
        if not self.gists[gist_id]:
            return 404, httpx.Headers(), b""
        gist = {
            "files": {"share.txt": {"content": self.gists[gist_id], "truncated": False}},
            "owner": {"login": DEMO_LOGIN, "id": 1},
        }
        return 200, httpx.Headers(), json.dumps(gist).encode()

    async def preview(self, task_id: str, message_id: int | None = None) -> dict:
        return {**await super().preview(task_id, message_id), "demo": True}

    async def create(self, *arguments, **options) -> dict:
        return {**await super().create(*arguments, **options), "demo": True}

    def shown(self, row: dict) -> dict:
        return {**super().shown(row), "gist_url": "", "demo": True}


class DemoRuntime(Runtime):
    """The real runtime, with scripted sessions, in-memory Slack and a quick Watchdog."""

    demo = True

    def __init__(
        self,
        store: Store,
        bus: Bus,
        project: Path,
        autoplay: bool = True,
        *,
        watch: WatchConfig | None = None,
        slack_delay: float = 18,
    ):
        if not store.one("SELECT id FROM settings WHERE id=1"):
            store.save_settings(
                Settings(
                    projects=[str(project)],
                    slack_enabled=True,
                    slack_poll_seconds=20,
                    mac_notifications=False,
                    digest_time="",
                    max_workers=3,
                )
            )
            on = {name: {"enabled": True} for name in DEMO_AGENTS}
            agents.save(store, agents.planned(store, on))
        brain = self.brain = DemoBrain(project, store)
        super().__init__(
            store,
            bus,
            # The owner's own sessions write their logs in a Copilot home of the demo's own,
            # where the sidebar reads where each stands.
            backend_factory=lambda settings: ScriptedBackend(
                brain, others=terminal_sessions(project), home=store.directory / "copilot-home"
            ),
            watch=watch or WatchConfig(interval=3, stall=12, long=600, cooldown=30),
            # The demo's own GitHub, never the owner's `gh` (QA U4).
            github=GitHubSource(store, self, gh_command=DemoGitHub()),
        )
        self.slack = SlackSource(store, self, tokens=DemoTokens(), mcp=DemoSlackMcp(slack_delay))
        self.autoplay = autoplay

        async def published(target: dict, body: str) -> str:
            # Nothing goes to GitHub in the demo: the review is "posted" here.
            return f"{target['url']}#pullrequestreview-demo"

        self.github.publish_review = published
        self.shares = DemoShares(self)
        # Scripted, they need no Agent SDK or sign-in of their own.
        self.claude_setup.refused = lambda fresh=False: ""

    def agent_connection(self, name: str) -> AgentConnection:
        if name not in DEMO_AGENTS:
            return super().agent_connection(name)
        return AgentConnection(
            lambda: AgentLikeBackend(self.brain, DEMO_AGENTS[name]),
            enabled=lambda: agents.enabled(self.store, name),
        )

    def agents_found(self) -> list[str]:
        return list(DEMO_AGENTS)

    async def glance_agent(self, name: str) -> dict:
        facts = {"version": "demo", "account": ""}
        self.seen_ready(name, facts)
        return facts

    def agent_status(self, name: str) -> dict:
        if name not in DEMO_AGENTS:
            return super().agent_status(name)
        on = name in agents.turned_on(self.store)
        return {"name": name, "label": agents.display(name), "on": on, "status": agents.READY,
                "said": agents.STATUS_WORDS[agents.READY], "pays": SCRIPTED}  # fmt: skip

    async def looked_at_agent(self, name: str) -> dict:
        """Settings → Agents & models: the demo's Codex and Claude as they are, scripted and
        ready, never looked for on this computer (QA U7: "Not installed", or the owner's own
        run and "Can't run here")."""
        if name not in DEMO_AGENTS:
            return await super().looked_at_agent(name)
        chosen = agents.configured(self.store).get(name, {})
        models = [described(model) for model in await ScriptedBackend().list_models()]
        self.agent_models_cache[name] = (time.monotonic(), models)
        return {
            "name": name,
            "label": agents.display(name),
            "enabled": bool(chosen.get("enabled")),
            "model": chosen.get("model") or "",
            "effort": chosen.get("effort") or "",
            "limit": agents.limit(self.store, name),
            "found": True,
            "state": "",
            "problem": "",
            "models": models,
        }

    async def cache_facts_now(self):
        """Workers' caches need nothing of the owner's tools: the demo's run no commands of
        theirs (`uv --version`, `go env` are never run)."""

    async def ready_caches(self, row: dict):
        return

    async def start(self):
        await super().start()
        if self.autoplay and not self.store.get("demo_played"):
            self.store.set("demo_played", "1")
            self.background(self.play())

    async def play(self):
        await asyncio.sleep(2)
        await self.owner_send(self.router_id(), KICKOFF)
        await asyncio.sleep(20)
        self.slack.wake()
        await asyncio.sleep(6)
        self.review_request()

    def review_request(self):
        """A teammate's PR to review, with a review drafted for the owner to check: the PR in a
        line, and each line comment with the code it is about."""
        draft = self.store.create_draft(self.router_id(), "github", DEMO_REVIEW, DEMO_REVIEW_BODY)
        self.bus.publish("draft", draft)


def terminal_sessions(project: Path) -> list[dict]:
    """The owner's own Copilot sessions: one open in a terminal that asks the owner something,
    shown read-only, and one closed after it finished, which a message continues in Chief of
    Staff. Their logs are as the Copilot CLI writes them, each event at its time."""
    now = datetime.now(UTC)

    def session(session_id: str, summary: str, events: list, minutes: float) -> dict:
        context = SimpleNamespace(working_directory=str(project), repository="", branch="main")
        meta = SimpleNamespace(
            session_id=session_id,
            summary=summary,
            is_remote=False,
            modified_time=now - timedelta(minutes=minutes),
            context=context,
        )
        # Made in a terminal (the Copilot CLI), as Copilot's list says it.
        vars(meta).update(client_name="github/cli", last_client_name="", clients_listed=True)
        start = {
            "sessionId": session_id,
            "version": 1,
            "producer": "copilot-agent",
            "copilotVersion": "1.0.109",
            "context": {"cwd": str(project), "branch": "main"},
        }
        log, parent = [], None
        for kind, ago, data in [("session.start", events[0][1] + 1, start), *events]:
            at = (now - timedelta(minutes=ago)).isoformat(timespec="milliseconds")
            event = {"type": kind, "data": data, "id": str(uuid4()), "parentId": parent,
                     "timestamp": at.replace("+00:00", "Z")}  # fmt: skip
            log.append(event)
            parent = event["id"]
        return {"meta": meta, "log": log}

    def step(call: str, tool: str, ago: float, words: str = "", **arguments) -> list:
        request = {"toolCallId": call, "name": tool, "arguments": arguments, "type": "function"}
        return [
            ("assistant.message", ago, {"content": words, "toolRequests": [request]}),
            ("tool.execution_start", ago,
             {"toolCallId": call, "toolName": tool, "arguments": arguments}),
        ]  # fmt: skip

    profiling = session("0d3e8c7a-5b1f-4c2e-9a6d-2f8b1c4e7a90", "Profile the slow orders page", [
        ("user.message", 19, {"content": "Why is the orders page slow for big accounts?"}),
        ("assistant.turn_start", 19, {"turnId": "0"}),
        *step("call-1", "bash", 18, command="uv run python -m cProfile app/orders.py"),
        ("tool.execution_complete", 15, {"toolCallId": "call-1", "success": True}),
        ("assistant.turn_end", 15, {"turnId": "0"}),
        ("assistant.turn_start", 15, {"turnId": "0"}),
        *step("call-2", "ask_user", 14,
              "Most of the time goes to one query per order line. Batching them into a single "
              "query should make it about ten times faster.",
              message="Batch the order-line queries in app/orders.py now?"),
    ], 14)  # fmt: skip
    changelog = session("7b2e4f1a-9c3d-4e8b-a5f6-1d0c9e8b7a64", "Write the API changelog", [
        ("user.message", 190, {"content": "Draft a changelog entry for the login API."}),
        ("assistant.turn_start", 190, {"turnId": "0"}),
        *step("call-3", "bash", 189, "Reading the recent commits first.",
              command="git log --oneline -10"),
        ("tool.execution_complete", 188, {"toolCallId": "call-3", "success": True}),
        ("assistant.turn_end", 188, {"turnId": "0"}),
        ("assistant.turn_start", 188, {"turnId": "0"}),
        ("assistant.message", 186, {"content": "Drafted: “Login tokens expire on the server's "
                                    "clock.” It is in CHANGELOG.md, not committed yet."}),
        ("assistant.turn_end", 186, {"turnId": "0"}),
        ("session.shutdown", 180, {"shutdownType": "routine"}),
    ], 180)  # fmt: skip
    return [{**profiling, "open": True}, changelog]


def factory(project: Path, autoplay: bool = True):
    def build(store: Store, bus: Bus) -> DemoRuntime:
        return DemoRuntime(store, bus, project, autoplay)

    return build
