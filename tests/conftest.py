import asyncio
import inspect
import json
import os
import re
import shutil
import sys

# Events are batched in the app; tests deliver them at once.
os.environ.setdefault("CHIEFOFSTAFF_EVENT_DEBOUNCE", "0")
os.environ.setdefault("CHIEFOFSTAFF_RETRY_DELAY", "0.01")
from own_home import home_for_tests

# The tests run with a home of their own (HOME, XDG_CONFIG_HOME) and git configuration of their
# own (GIT_CONFIG_GLOBAL, GIT_CONFIG_NOSYSTEM), never the owner's: git they run, and lines the
# push check's oracle runs, read and write those (owners_git_configuration_untouched checks).
os.environ["HOME"] = home_for_tests()
os.environ["XDG_CONFIG_HOME"] = os.environ["HOME"] + "/.config"
os.environ["GIT_CONFIG_GLOBAL"] = os.environ["HOME"] + "/.gitconfig"
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
import atexit
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import codex_schema
import pytest
from agent_paths import without_agents
from copilot.tools import ToolInvocation
from policy_oracle import owners_git_configuration

from chiefofstaff import machine
from chiefofstaff.backend import TESTED_CLI, Capabilities
from chiefofstaff.bus import Bus
from chiefofstaff.envelope import language
from chiefofstaff.envelope import wrap as env
from chiefofstaff.models import Settings
from chiefofstaff.runtime import Runtime
from chiefofstaff.sources.github import GitHubSource
from chiefofstaff.sources.slack import SlackSource
from chiefofstaff.store import Store
from chiefofstaff.watchdog import WatchConfig

TEST_HOME = os.environ["HOME"]
atexit.register(shutil.rmtree, TEST_HOME, True)


def said(text: str) -> str:
    """What a session receives when the owner writes this: tagged with its language."""
    return env("owner", "message", text, reply_in=language(text))


# The real-bash oracles (tests/policy_oracle.py, tests/checkout_oracle.py) run thousands of
# command lines, SPAWN_THREADS at a time, each in bash (on macOS, in sandbox-exec), and check each
# in Python: their time is how fast a runner starts processes that many at a time (its cores as
# much as each start), or runs Python, whichever is slower. On the machine their time limits were
# set on (a 10-core Mac, under its usual load; the median of 3 rounds): SPAWN_THREADS threads
# start SPAWNS `bash -c :` between them in SPAWN_REFERENCE seconds, and python_work() takes
# PYTHON_REFERENCE seconds.
SPAWN_THREADS = 8
SPAWNS = 120
SPAWN_REFERENCE = 0.14
PYTHON_REFERENCE = 0.07
# How much slower than that a runner may be given time for (past it: a hang, or a slowdown of
# the check itself, fails the test as before).
MOST_PACE = 8.0


def python_work() -> None:
    sum(len(str(number)) for number in range(1_500_000))


def runner_pace() -> float:
    """How many times slower than the reference machine this runner starts processes
    SPAWN_THREADS at a time, or runs Python, whichever is more (at least 1, at most MOST_PACE)."""

    def timed(work) -> float:
        rounds = []
        for _ in range(3):
            start = time.perf_counter()
            work()
            rounds.append(time.perf_counter() - start)
        return statistics.median(rounds)

    paces = [timed(python_work) / PYTHON_REFERENCE]
    if bash := shutil.which("bash"):
        with ThreadPoolExecutor(SPAWN_THREADS) as pool:

            def spawns() -> None:
                run = [bash, "--norc", "--noprofile", "-c", ":"]
                list(pool.map(lambda _: subprocess.run(run, check=False), range(SPAWNS)))

            paces.append(timed(spawns) / SPAWN_REFERENCE)
    return min(MOST_PACE, max(1.0, *paces))


@pytest.fixture
def paced(request) -> float:
    """A real-bash oracle's time limit (its `timeout` marker, func_only), scaled to how fast
    this runner is (runner_pace): on CI's macOS runners (3 cores) an oracle took over its 120 s,
    and the test then passed on a rerun only. Measured as the test starts; the pace is
    returned."""
    marker = request.node.get_closest_marker("timeout")
    base = float(marker.args[0]) if marker and marker.args else 120.0
    pace = runner_pace()
    request.node.add_marker(pytest.mark.timeout(base * pace, func_only=True), append=False)
    return pace


@pytest.fixture(autouse=True)
def private_git_folders(tmp_path_factory, monkeypatch):
    """Chief of Staff's own git folders (privategit) in a folder of each test's own, as made by
    it: never another test's, nor the temporary folder's."""
    from chiefofstaff import privategit

    monkeypatch.setattr(privategit, "ROOT", tmp_path_factory.mktemp("git"))
    monkeypatch.setattr(privategit, "MADE", {})
    monkeypatch.setattr(privategit, "LOCKS", {})


@pytest.fixture(autouse=True)
def no_pypi(monkeypatch):
    """No test reads PyPI for a newer release (updates.py): one that wants one says which."""
    from chiefofstaff import updates

    async def nothing() -> str:
        return ""

    monkeypatch.setattr(updates, "fetch_latest", nothing)


@pytest.fixture(autouse=True)
def no_copilot_downloads(monkeypatch):
    """No test sets up a Copilot runtime ahead (backend.prefetch) or finds one of this machine's
    (compat.installed_runtimes, compat.cli_protocol), nor shares one set up by another test:
    tests of those say what they find. The real contract tests see the machine's."""
    from chiefofstaff import backend, compat

    monkeypatch.setattr(backend, "PREFETCH", False, raising=False)
    monkeypatch.setattr(backend, "PROVISIONED", {}, raising=False)
    if os.environ.get("CHIEFOFSTAFF_TEST_COPILOT") != "1":
        monkeypatch.setattr(compat, "package_roots", list)
        monkeypatch.setattr(compat, "sdk_cache_root", lambda: None)


@pytest.fixture(autouse=True)
def terse_titles(request, monkeypatch):
    """Tests name tasks tersely ("Lint", "A"): a new task's title is checked as a rename's is
    (it says what it delivers, see deliverable_title) only in tests marked `titles`."""
    if request.node.get_closest_marker("titles") is None:
        from chiefofstaff import tools
        from chiefofstaff.envelope import clean_title

        monkeypatch.setattr(tools, "deliverable_title", clean_title)


class FakeSession:
    def __init__(self, session_id: str, options: dict):
        self.session_id = session_id
        self.options = options
        self.sent: list[str] = []
        self.raw_sent: list[str] = []
        self.handlers = []
        self.aborted = False
        self.disconnected = False
        self.models: list[str] = []
        self.tunings: list[dict] = []
        self.modes: list[str | None] = []
        self.attachments: list[list | None] = []
        # MCP sign-in: the URL to open, or "" when Copilot already has the credentials.
        self.login_url = "https://login.example.com/authorize?state=1"
        self.logins: list[str] = []

        async def login(request):
            self.logins.append(request.server_name)
            return SimpleNamespace(authorization_url=self.login_url)

        # Copilot's background tasks (a command a turn left running): id -> status.
        self.background: dict[str, str] = {}

        async def listed():
            return SimpleNamespace(
                tasks=[SimpleNamespace(id=k, status=v) for k, v in self.background.items()]
            )

        async def cancel(request):
            running = self.background.get(request.id) == "running"
            self.background[request.id] = "cancelled"
            return SimpleNamespace(cancelled=running)

        # What Copilot queued and has not started on: queue item id -> prompt.
        self.queued: dict[str, str] = {}

        async def pending_items():
            await asyncio.sleep(0)  # An RPC: others run meanwhile.
            return SimpleNamespace(
                items=[SimpleNamespace(id=k, display_text=v) for k, v in self.queued.items()]
            )

        async def remove_at(request):
            await asyncio.sleep(0)
            return SimpleNamespace(removed=self.queued.pop(request.id, None) is not None)

        # The shell init scripts Copilot was told to source (session.options.update), in turn.
        self.shell_scripts: list[list[str]] = []

        async def update_options(params):
            if params.shell is not None:
                self.shell_scripts.append([script.path for script in params.shell.init_scripts])

        self.rpc = SimpleNamespace(
            options=SimpleNamespace(update=update_options),
            mcp=SimpleNamespace(oauth=SimpleNamespace(login=login)),
            tasks=SimpleNamespace(list=listed, cancel=cancel),
            queue=SimpleNamespace(pending_items=pending_items, remove_at=remove_at),
        )

    async def set_model(self, model, **tuning):
        self.models.append(model)
        self.tunings.append(tuning)

    def on(self, handler):
        self.handlers.append(handler)
        return lambda: None

    async def send(self, prompt, mode=None, attachments=None):
        self.attachments.append(attachments)
        self.raw_sent.append(prompt)
        # Owner messages to the Router carry a random ref; tests compare the rest.
        self.sent.append(re.sub(r' ref="o[0-9a-f]{6}"', "", prompt))
        self.modes.append(mode)
        return f"m{len(self.sent)}"

    async def abort(self):
        self.aborted = True

    async def get_events(self):
        # What it was sent, as sent (an owner's ref and all).
        return [
            SimpleNamespace(
                type=SimpleNamespace(value="user.message"), data=SimpleNamespace(content=p)
            )
            for p in self.raw_sent
        ]

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
        echoes=False,  # Tests emit user.message themselves where it matters.
    )

    def __init__(self):
        self.sessions: dict[str, FakeSession] = {}
        self.created: list[str] = []
        self.resumed: list[str] = []
        self.names: dict[str, str] = {}
        self.metas: list = []
        self.in_use: dict[str, int] = {}  # Sessions open in a terminal: id -> pid.
        self.logs: dict[str, list[dict]] = {}  # Their logs (events as the CLI writes them).
        self.log_changed: dict[str, float] = {}
        self.cli_version = TESTED_CLI
        # A Copilot home of its own, where others.py reads sessions' logs (session_log).
        self.home: Path | None = None
        self.pids: set[int] = set()  # Its CLI's processes.
        self.crashed = False  # Its CLI exited by itself.

    async def start(self):
        pass

    def processes(self):
        return dict.fromkeys(self.pids, "copilot --headless")

    def alive(self):
        return not self.crashed

    async def stop(self):
        # Like Copilot: a session that never had a message is not kept.
        for session_id in [s for s, session in self.sessions.items() if not session.sent]:
            del self.sessions[session_id]

    async def get_auth_status(self):
        return SimpleNamespace(isAuthenticated=True)

    async def version(self):
        return self.cli_version

    def sandbox(self, policy):
        return policy

    async def gh_works_in_sandbox(self):
        return True

    async def open_elsewhere(self):
        return dict(self.in_use)

    async def read_history(self, session_id, offset):
        events = self.logs.get(session_id, [])
        return events[offset:], len(events), self.log_changed.get(session_id, 0.0)

    async def read_usage(self, session_id, offset):
        events = self.logs.get(session_id, [])
        found = [e for e in events[offset:] if e.get("type") == "session.usage_record"]
        return found, len(events)

    def session_log(self, session_id):
        return self.home / "session-state" / session_id / "events.jsonl" if self.home else None

    async def mcp_sign_in_url(self, session, server):
        login = await session.rpc.mcp.oauth.login(SimpleNamespace(server_name=server))
        return login.authorization_url

    async def create_session(self, *, session_id, name, **options):
        if '"' in (name or ""):  # As Copilot does.
            raise RuntimeError("session construction failed: Session name must not contain "
                               "double quotes.")  # fmt: skip
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
        session.raw_sent = self.sessions[session_id].raw_sent
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


def task_of(harness, session_id: str) -> str:
    """The id of a worker's current task."""
    return harness.store.one("SELECT task_id FROM sessions WHERE id=?", (session_id,))["task_id"]


async def settle(runtime: Runtime):
    for _ in range(20):
        await asyncio.sleep(0)
        if runtime.jobs:
            await asyncio.gather(*list(runtime.jobs), return_exceptions=True)


async def call_tool(runtime: Runtime, name: str, caller: str, **arguments):
    return await runtime.tools[name].handler(
        ToolInvocation(session_id=caller, tool_call_id="t1", tool_name=name, arguments=arguments)
    )


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
        self.gh_replies: dict[str, str | Exception] = {}

        async def notifier(title, text):
            self.notifications.append((title, text))

        async def gh(*arguments, payload=None, token=""):
            self.gh_calls.append((arguments, payload))
            # Longest prefix first: "api repos/a/b/pulls/1/files" before "api repos/a/b/pulls/1".
            for prefix, reply in sorted(self.gh_replies.items(), key=lambda item: -len(item[0])):
                if " ".join(arguments).startswith(prefix):
                    if isinstance(reply, Exception):
                        raise reply
                    return reply
            raise RuntimeError(f"unexpected gh call {arguments}")

        self.runtime = Runtime(
            self.store,
            Bus(),
            backend_factory=lambda settings: self.client,
            notifier=notifier,
            watch=watch or WatchConfig(),
        )
        self.runtime.sandbox_host = True  # Whatever machine the tests run on.
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
        """The Copilot session behind one of ours now (a rerun helper gets a fresh one)."""
        return self.client.sessions[self.store.get(f"copilot:{session_id}") or session_id]

    def kinds(self, session_id):
        return [(m["kind"], m["text"]) for m in self.store.messages(session_id)]

    def own_pull(self, repo: str, number: int, **extra):
        """A PR the owner's account opened just now (as a worker pushing as the owner does)."""
        self.gh_replies["auth status --json hosts"] = json.dumps(
            {"hosts": {"github.com": [{"login": "me", "active": True, "state": "success"}]}}
        )
        pull = {
            "number": number,
            "title": f"PR {number}",
            "state": "open",
            "user": {"login": "me"},
            "created_at": "2999-01-01T00:00:00Z",
            "head": {"ref": "feature", "sha": "abc"},
            "html_url": f"https://github.com/{repo}/pull/{number}",
        } | extra
        self.gh_replies[f"api repos/{repo}/pulls/{number}"] = json.dumps(pull)


@pytest.fixture
async def end_background_work():
    """What a test left running (a git command a runtime started) is cancelled before its loop
    closes, leaving asyncio's own pipe setup to finish: on Python 3.11 a process start whose pipe
    setup is cancelled with it never ends (the loop's close cancels both)."""
    yield
    me = asyncio.current_task()
    left = [
        task
        for task in asyncio.all_tasks()
        if task is not me
        and not getattr(task.get_coro(), "__qualname__", "").endswith("._connect_pipes")
    ]
    for task in left:
        task.cancel()
    if left:
        await asyncio.wait(left, timeout=10)


def pytest_collection_modifyitems(items):
    for item in items:
        if inspect.iscoroutinefunction(getattr(item, "function", None)):
            item.fixturenames.append("end_background_work")


@pytest.fixture(autouse=True)
def close_stores(monkeypatch):
    """Every store a test opens (its harness's, one it makes itself, one a restart makes) is
    closed after it, once what it left running has ended."""
    opened: list[Store] = []
    open_store = Store.__init__

    def tracked(self: Store, *args, **kwargs):
        open_store(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(Store, "__init__", tracked)
    yield
    for store in opened:
        store.close()


@pytest.fixture(autouse=True)
def plenty_of_memory(monkeypatch):
    """Tests do not depend on how much memory the machine running them has left."""
    monkeypatch.setattr(machine, "free_memory", lambda: None)
    monkeypatch.setattr(machine, "_cached", (0.0, None))


@pytest.fixture(autouse=True)
def own_code_unscanned(request, monkeypatch):
    """Chief of Staff's own code is not looked up in this machine's uv cache at each connect
    (tests of it pass `own_code` explicitly, or mark themselves `own_code`)."""
    if request.node.get_closest_marker("own_code") is None:
        from chiefofstaff import runtime

        monkeypatch.setattr(runtime, "own_code", list)


@pytest.fixture(autouse=True)
def no_cache_seeding(monkeypatch, tmp_path):
    """Workers' caches are never filled from the machine's own (caches.seed), nor its Go or
    Java looked at, unless a test does it itself."""
    from chiefofstaff import caches

    monkeypatch.setattr(caches, "seed", lambda *args, **kwargs: {})
    monkeypatch.setattr(caches, "default_tier", lambda: tmp_path / "worker-caches")
    monkeypatch.setattr(caches, "go_env", dict)
    monkeypatch.setattr(caches, "old_uv", lambda: "")
    monkeypatch.setattr(caches, "truststore", lambda *args: None)
    monkeypatch.setattr(caches, "uv_stamp", lambda: ("", 0))
    monkeypatch.setattr(caches, "authority_stamp", lambda copilot_home: 0)


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


def fake_codex(directory: Path, monkeypatch) -> str:
    """A stand-in Codex CLI (tests/fake_codex.py) in `directory`, its threads kept there too:
    the command a Codex backend runs."""
    directory.mkdir(parents=True, exist_ok=True)
    command = directory / "codex"
    script = Path(__file__).parent / "fake_codex.py"
    command.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    command.chmod(0o755)
    monkeypatch.setenv("FAKE_CODEX_HOME", str(directory / "home"))
    monkeypatch.setenv("FAKE_CODEX_WIRE", str(directory / "wire.jsonl"))
    WIRES.append(directory / "wire.jsonl")
    return str(command)


WIRES: list[Path] = []


def fake_claude(directory: Path, monkeypatch) -> str:
    """A stand-in Claude Code (tests/fake_claude.py) as `claude` in `directory`, its sessions
    kept there too, and a Claude configuration folder of the test's own: the command a Claude
    backend runs."""
    directory.mkdir(parents=True, exist_ok=True)
    command = directory / "claude"
    # The stand-in runs as a file named `claude`: its command line (`python …/claude --resume
    # <id>`) reads as Claude Code's does (resumed_elsewhere, claude_program).
    script = directory / "stand-in" / "claude"
    script.parent.mkdir(exist_ok=True)
    script.unlink(missing_ok=True)
    script.symlink_to(Path(__file__).parent / "fake_claude.py")
    command.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    command.chmod(0o755)
    monkeypatch.setenv("FAKE_CLAUDE_HOME", str(directory / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(directory / "config"))
    # Claude set up (its state file there), as on a computer where the owner signed in.
    (directory / "config").mkdir(exist_ok=True)
    (directory / "config" / ".claude.json").write_text("{}\n")
    return str(command)


# Modules a program of Chief of Staff's imports as it starts (and its own package): a repository
# with files of these names runs them as Chief of Staff if its folder is on the import path.
SHADOWED = ("typing", "argparse", "logging", "subprocess", "token", "copy", "inspect", "json")


def shadowing_repository(folder: Path, marks: Path) -> Path:
    """A repository whose `typing.py` (and others: SHADOWED) and `chiefofstaff/` would each leave
    a mark in `marks` if Python imported them."""
    (folder / "chiefofstaff").mkdir(parents=True)
    marks.mkdir()
    for name in [*SHADOWED, "chiefofstaff/__init__", "chiefofstaff/agentrun"]:
        mark = marks / name.replace("/", ".")
        (folder / f"{name}.py").write_text(f"open({str(mark)!r}, 'w').close()\n")
    return folder


def cwd_of(pid: int) -> str:
    """The folder a process runs in."""
    if sys.platform.startswith("linux"):
        return str(Path(f"/proc/{pid}/cwd").readlink())
    listed = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True
    ).stdout
    return next((line[1:] for line in listed.splitlines() if line.startswith("n")), "")


# The owner's own Claude Code and Codex, when this machine has them: the contract tests on the
# real CLIs run them by these paths. Every other test sees neither on the PATH, unless it puts a
# stand-in there (Chief of Staff looks for the agents installed: never the owner's in a test).
REAL_CLAUDE = shutil.which("claude")
CODEX_COMMAND = shutil.which("codex") or "codex"


@pytest.fixture(scope="session")
def path_without_claude(tmp_path_factory) -> str:
    """The PATH, each of its folders that has a `claude` or a `codex` mirrored without them."""
    return without_agents(os.environ.get("PATH", ""), tmp_path_factory.mktemp("path"))


@pytest.fixture(autouse=True)
def owners_claude_hidden(monkeypatch, path_without_claude):
    monkeypatch.setenv("PATH", path_without_claude)


@pytest.fixture
def codex_installed(tmp_path, monkeypatch, owners_claude_hidden):
    """The stand-in Codex CLI as `codex` on the PATH, a home of the test's own (where ~/.codex
    would be); each of its runs is listed (FAKE_CODEX_RUNS)."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    command = fake_codex(tmp_path / "fake", monkeypatch)
    monkeypatch.setenv("PATH", f"{tmp_path / 'fake'}:{os.environ['PATH']}")
    runs = tmp_path / "codex-runs"
    monkeypatch.setenv("FAKE_CODEX_RUNS", str(runs))
    return command, runs


@pytest.fixture
def claude_on_path(tmp_path, monkeypatch, owners_claude_hidden):
    """The stand-in Claude Code as `claude` on the PATH; each of its runs is listed."""
    command = fake_claude(tmp_path / "fake-claude", monkeypatch)
    monkeypatch.setenv("PATH", f"{tmp_path / 'fake-claude'}:{os.environ['PATH']}")
    runs = tmp_path / "claude-runs"
    monkeypatch.setenv("FAKE_CLAUDE_RUNS", str(runs))
    return command, runs


@pytest.fixture(autouse=True)
def codex_set_up(tmp_path_factory, monkeypatch):
    """Codex's folder (CODEX_HOME), as where it was set up: an empty one of the test's own, not
    the owner's ~/.codex (where there is none, nothing of Codex runs). Tests on the real Codex
    (CHIEFOFSTAFF_TEST_CODEX) use the owner's, for its sign-in."""
    if not os.environ.get("CHIEFOFSTAFF_TEST_CODEX"):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path_factory.mktemp("codex-home")))


@pytest.fixture(autouse=True)
def codex_wire_checked():
    """Each message either way between Chief of Staff and a fake Codex is as Codex's protocol
    has it (the tested version's, kept in tests/codex_protocol: tests/codex_schema.py)."""
    WIRES.clear()
    yield
    wires, WIRES[:] = list(WIRES), []
    if not wires:
        return
    unlisted = [problem for wire in dict.fromkeys(wires) for problem in codex_schema.unlisted(wire)]
    assert not unlisted, "What a version's check would not look for:\n" + "\n".join(unlisted)
    checked = codex_schema.protocol()
    problems = [problem for wire in dict.fromkeys(wires) for problem in checked.problems(wire)]
    assert not problems, "Off Codex's protocol:\n" + "\n".join(problems[:20])


# The repository's top folder: what a test run leaves there (and git does not ignore) fails it,
# so no artefact of a test can be committed with the code (tests write under tmp_path).
REPOSITORY = Path(__file__).resolve().parent.parent


def left_at_the_top() -> set[str]:
    """What is at the top of the repository and is neither tracked nor ignored by git."""
    done = subprocess.run(
        ["git", "-C", str(REPOSITORY), "ls-files", "--others", "--exclude-standard", "--directory"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        return set()  # Not a git checkout (an unpacked release): nothing to check.
    return {line.rstrip("/") for line in done.stdout.splitlines() if "/" not in line.rstrip("/")}


def pytest_sessionstart(session):
    session.config.stash[LEFT_BEFORE] = left_at_the_top()
    session.config.stash[OWNERS] = owners_git_configuration()


# The owner's git configuration as the last test left it: one that changes it fails.
OWNERS_NOW: list[dict] = [owners_git_configuration()]


@pytest.fixture(autouse=True)
def owners_git_configuration_untouched():
    """No test changes the owner's git configuration (`~/.gitconfig`, `~/.config/git/…` of the
    account's home, whatever HOME says): one that does fails, saying which files."""
    yield
    now = owners_git_configuration()
    before, OWNERS_NOW[0] = OWNERS_NOW[0], now
    if now != before:
        changed = sorted(path for path in {*now, *before} if now.get(path) != before.get(path))
        pytest.fail(f"this test changed the owner's git configuration: {', '.join(changed)}")


def pytest_sessionfinish(session, exitstatus):
    if (
        OWNERS in session.config.stash
        and owners_git_configuration() != session.config.stash[OWNERS]
    ):
        sys.stderr.write("\nThe tests changed the owner's git configuration (~/.gitconfig, "
                         "~/.config/git).\n")  # fmt: skip
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if LEFT_BEFORE not in session.config.stash:
        return
    left = sorted(left_at_the_top() - session.config.stash[LEFT_BEFORE])
    if left:
        sys.stderr.write(
            f"\nThe tests left {', '.join(left)} at the top of the repository: write under "
            "tmp_path instead.\n"
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


LEFT_BEFORE = pytest.StashKey[set]()
OWNERS = pytest.StashKey[dict]()
