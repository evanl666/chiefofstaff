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
from pathlib import Path
from types import SimpleNamespace

from .bus import Bus
from .models import Settings
from .runtime import Runtime
from .scripted import (
    AskOwner,
    CallTool,
    Say,
    ScriptedBackend,
    ScriptedSession,
    Shell,
    Wait,
    WriteFile,
)
from .sources.slack import SlackSource
from .store import Store
from .watchdog import WatchConfig

KICKOFF = (
    "Fix the flaky login test in acme-api, then add rate limiting to login attempts. "
    "Also compare a few feature-flag services for us."
)
MAYA_DM = "D0DEMOMAYA"
ENVELOPE = re.compile(r"<message ([^>]*)>\n(.*?)\n</message>", re.DOTALL)
ATTRIBUTE = re.compile(r'(\w+)="([^"]*)"')

LOGIN = '''"""Login with expiring tokens."""

import time

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
        subprocess.run(command, cwd=project, check=True, capture_output=True)
    return project


class DemoBrain:
    def __init__(self, project: Path):
        self.project = str(project)
        self.researchers: set[str] = set()  # Workers on the task that stalls on purpose.
        self.reported: set[str] = set()

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
                                },
                            )
                        )
                    )
                    yield CallTool(
                        "spawn_worker",
                        {
                            "title": "Rate-limit login attempts",
                            "instructions": "Limit login attempts. Build on the login fix.",
                            "project": self.project,
                            "isolate": True,
                            "after": [first["task_id"]],
                            "why_new": "A separate change with its own branch, after the fix.",
                        },
                    )
                    yield CallTool(
                        "spawn_worker",
                        {
                            "title": "Compare feature-flag services",
                            "instructions": "Compare three feature-flag services for a small "
                            "team: price, SDKs, self-hosting.",
                            "why_new": "Research, not about the login code.",
                        },
                    )
                    yield Say(
                        "Started three tasks: the flaky login test, rate limiting once the fix "
                        "is in, and a feature-flag comparison."
                    )
                    continue
                title = " ".join(body.split()[:7]).rstrip(".,!?") or "Task"
                code = re.search(r"\b(fix|bug|test|code|add|refactor|repo|api)\b", body, re.I)
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
                        },
                    )
                    yield Say(f"Gave it to “{related['title']}”, which already knows this code.")
                else:
                    yield Say(f"Started a worker on “{title}”.")
            elif sender.startswith("worker:") and kind == "report":
                status = attrs.get("status")
                if status == "done":
                    headline = body.strip().splitlines()[0]
                    yield CallTool(
                        "notify_owner", {"text": f"“{attrs.get('title')}” is done: {headline}"}
                    )
                elif status in {"question", "blocked", "failed"}:
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
                    )
                    yield Wait(2)
                    yield WriteFile("app/login.py", LOGIN_FIXED)
                    yield Shell("uv run pytest -q", "........\n8 passed in 0.29s", seconds=2)
                    yield Shell(
                        "git add -A && git commit -qm 'Fix flaky login test' "
                        "&& git log --oneline -1",
                        run=True,
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
                        },
                    )
                elif title == "Rate-limit login attempts":
                    answer = yield AskOwner(
                        "Should login attempts be limited per IP address or per account?",
                        ["Per IP address", "Per account", "Both"],
                    )
                    choice = (answer or {}).get("answer") or "Per IP address"
                    yield WriteFile("app/ratelimit.py", RATELIMIT)
                    yield Shell("uv run pytest -q", "...........\n11 passed in 0.33s", seconds=2)
                    yield Shell(
                        "git add -A && git commit -qm 'Rate-limit login attempts' "
                        "&& git log --oneline -1",
                        run=True,
                    )
                    yield CallTool(
                        "report",
                        {
                            "status": "done",
                            "task_id": attrs.get("task", ""),
                            "summary": f"Added rate limiting {choice[0].lower()}{choice[1:]}: "
                            "five attempts a "
                            "minute.\n- New `app/ratelimit.py`, used by login\n- Built on the "
                            "login fix (the clock is injected)\n- 11 tests pass",
                        },
                    )
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


FLAGS_REPORT = (
    "Compared three feature-flag services for a small team.\n"
    "- **Hosted A**: free up to 10 seats, SDKs for all our languages, no self-hosting\n"
    "- **Hosted B**: usage pricing, best experimentation tools\n"
    "- **Open source C**: self-hosted, free, needs a Postgres\n"
    "- Recommendation: start with A; move to C if we need data on our servers"
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
                stamp = f"{int(time.time())}000200"
                return (
                    "Results:\n1. DM from Maya Chen: hey! is the login fix going out today? "
                    "support keeps asking\n"
                    f"   https://demo.slack.com/archives/{MAYA_DM}/p{stamp}"
                )
            return "No results found."
        return "Maya Chen: hey! is the login fix going out today? support keeps asking"


class DemoTokens:
    def get(self) -> str:
        return "xoxp-demo"

    def save(self, token: str):
        pass

    def delete(self):
        pass


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
        brain = DemoBrain(project)
        super().__init__(
            store,
            bus,
            backend_factory=lambda settings: ScriptedBackend(brain),
            watch=watch or WatchConfig(interval=3, stall=12, long=600, cooldown=30),
        )
        self.slack = SlackSource(store, self, tokens=DemoTokens(), mcp=DemoSlackMcp(slack_delay))
        self.autoplay = autoplay

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


def factory(project: Path, autoplay: bool = True):
    def build(store: Store, bus: Bus) -> DemoRuntime:
        return DemoRuntime(store, bus, project, autoplay)

    return build
