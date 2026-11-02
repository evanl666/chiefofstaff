import asyncio
import os

# Events are batched in the app; tests deliver them at once.
os.environ.setdefault("CHIEFOFSTAFF_EVENT_DEBOUNCE", "0")
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from copilot.tools import ToolInvocation

from chiefofstaff.backend import TESTED_CLI, Capabilities
from chiefofstaff.bus import Bus
from chiefofstaff.envelope import wrap as env  # noqa: F401  (tests import it)
from chiefofstaff.models import Settings
from chiefofstaff.runtime import Runtime
from chiefofstaff.sources.github import GitHubSource
from chiefofstaff.sources.slack import SlackSource
from chiefofstaff.store import Store
from chiefofstaff.watchdog import WatchConfig


class FakeSession:
    def __init__(self, session_id: str, options: dict):
        self.session_id = session_id
        self.options = options
        self.sent: list[str] = []
        self.handlers = []
        self.aborted = False
        self.disconnected = False
        self.models: list[str] = []
        self.tunings: list[dict] = []
        self.modes: list[str | None] = []
        # MCP sign-in: the URL to open, or "" when Copilot already has the credentials.
        self.login_url = "https://login.example.com/authorize?state=1"
        self.logins: list[str] = []

        async def login(request):
            self.logins.append(request.server_name)
            return SimpleNamespace(authorization_url=self.login_url)

        self.rpc = SimpleNamespace(mcp=SimpleNamespace(oauth=SimpleNamespace(login=login)))

    async def set_model(self, model, **tuning):
        self.models.append(model)
        self.tunings.append(tuning)

    def on(self, handler):
        self.handlers.append(handler)
        return lambda: None

    async def send(self, prompt, mode=None):
        self.sent.append(prompt)
        self.modes.append(mode)
        return f"m{len(self.sent)}"

    async def abort(self):
        self.aborted = True

    async def disconnect(self):
        self.disconnected = True

    def emit(self, kind: str, **data):
        event = SimpleNamespace(type=SimpleNamespace(value=kind), data=SimpleNamespace(**data))
        for handler in self.handlers:
            handler(event)


class FakeClient:
    capabilities = Capabilities(
        name="Fake",
        steering=True,
        resume=True,
        permission_callbacks=True,
        mcp_sign_in=True,
        sandbox=True,
    )

    def __init__(self):
        self.sessions: dict[str, FakeSession] = {}
        self.created: list[str] = []
        self.resumed: list[str] = []
        self.names: dict[str, str] = {}
        self.metas: list = []
        self.cli_version = TESTED_CLI

    async def start(self):
        pass

    async def stop(self):
        pass

    async def get_auth_status(self):
        return SimpleNamespace(isAuthenticated=True)

    async def version(self):
        return self.cli_version

    async def mcp_sign_in_url(self, session, server):
        login = await session.rpc.mcp.oauth.login(SimpleNamespace(server_name=server))
        return login.authorization_url

    async def create_session(self, *, session_id, name, **options):
        session = FakeSession(session_id, options)
        self.sessions[session_id] = session
        self.created.append(session_id)
        self.names[session_id] = name
        return session

    async def resume_session(self, session_id, **options):
        if session_id not in self.sessions:
            raise RuntimeError("Session not found")
        session = FakeSession(session_id, options)
        session.sent = self.sessions[session_id].sent
        self.sessions[session_id] = session
        self.resumed.append(session_id)
        return session

    async def list_sessions(self):
        return self.metas

    async def list_models(self):
        def model(name, efforts=(), long_context=0):
            prices = SimpleNamespace(
                max_prompt_tokens=200000,
                context_max=None,
                long_context=SimpleNamespace(max_prompt_tokens=long_context, context_max=None)
                if long_context
                else None,
            )
            return SimpleNamespace(
                id=name,
                name=name.title(),
                policy=None,
                billing=SimpleNamespace(token_prices=prices),
                capabilities=SimpleNamespace(limits=SimpleNamespace(max_prompt_tokens=200000)),
                supported_reasoning_efforts=list(efforts),
                default_reasoning_effort="medium" if efforts else None,
            )

        return [
            model("auto"),
            model("fast-model"),
            model("smart-model", ("low", "medium", "high"), 1000000),
        ]

    async def get_session_metadata(self, session_id):
        meta = next((meta for meta in self.metas if meta.session_id == session_id), None)
        if not meta and session_id in self.sessions:
            meta = SimpleNamespace(session_id=session_id, context=None)
        return meta


class FakeTokens:
    def __init__(self, token="xoxp-test"):
        self.token = token

    def get(self):
        return self.token

    def save(self, token):
        self.token = token

    def delete(self):
        self.token = ""


def slack_tool(name, read_only=True, description=""):
    return SimpleNamespace(
        name=name,
        description=description,
        annotations=SimpleNamespace(readOnlyHint=read_only),
    )


class FakeSlackMcp:
    url = "https://mcp.slack.test/mcp"

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.results: dict[str, str] = {}
        self.reads: dict[str, str] = {}
        self.sent_link = "https://acme.slack.com/archives/C1/p1712345678000200"
        self.fail_send = False
        self.tools = [
            slack_tool(
                "slack_search_public_and_private", description="Current user's user_id is U0OWNER1."
            ),
            slack_tool("slack_read_thread"),
            slack_tool("slack_send_message", read_only=False),
            slack_tool("slack_add_reaction", read_only=False),
        ]

    async def list_tools(self):
        return self.tools

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "slack_send_message":
            if self.fail_send:
                raise RuntimeError("network down")
            return f"Message sent: {self.sent_link}"
        if name in {"slack_read_thread", "slack_read_channel"}:
            return self.reads.get(arguments["channel_id"], "")
        return self.results.get(arguments.get("query", ""), "")


async def settle(runtime: Runtime):
    for _ in range(20):
        await asyncio.sleep(0)
        if runtime.tasks:
            await asyncio.gather(*list(runtime.tasks), return_exceptions=True)


async def call_tool(runtime: Runtime, name: str, caller: str, **arguments):
    result = await runtime.tools[name].handler(
        ToolInvocation(session_id=caller, tool_call_id="t1", tool_name=name, arguments=arguments)
    )
    return result


class Harness:
    def __init__(
        self,
        directory: Path,
        settings: Settings | None = None,
        client=None,
        watch: WatchConfig | None = None,
    ):
        self.store = Store(directory)
        if settings:
            self.store.save_settings(settings)
        self.client = client or FakeClient()
        self.notifications: list[tuple[str, str]] = []
        self.mcp = FakeSlackMcp()
        self.tokens = FakeTokens()
        self.gh_calls: list[tuple] = []
        self.gh_replies: dict[str, str] = {}

        async def notifier(title, text):
            self.notifications.append((title, text))

        async def gh(*arguments, payload=None):
            self.gh_calls.append((arguments, payload))
            for prefix, reply in self.gh_replies.items():
                if " ".join(arguments).startswith(prefix):
                    return reply
            raise RuntimeError(f"unexpected gh call {arguments}")

        self.runtime = Runtime(
            self.store,
            Bus(),
            backend_factory=lambda settings: self.client,
            notifier=notifier,
            watch=watch or WatchConfig(),
        )
        self.runtime.slack = SlackSource(self.store, self.runtime, tokens=self.tokens, mcp=self.mcp)
        self.runtime.github = GitHubSource(self.store, self.runtime, gh_command=gh)

    async def connect(self):
        self.runtime.loop = asyncio.get_running_loop()
        self.runtime.bus.bind(self.runtime.loop)
        await self.runtime.connect()
        await settle(self.runtime)
        return self.runtime

    @property
    def router_id(self):
        return self.store.get("router_id")

    def session(self, session_id) -> FakeSession:
        return self.client.sessions[session_id]

    def kinds(self, session_id):
        return [(m["kind"], m["text"]) for m in self.store.messages(session_id)]


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path / "data")


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ["git", "init", "-q", "-b", "main"],
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
    ):
        subprocess.run(command, cwd=repo, check=True)
    (repo / "pkg").mkdir()
    return repo
