"""Signing in to a coding agent, installing and updating one, from the page (agent-smooth.md).

The owner clicks Sign in (or Install) where an agent says "Sign-in needed" (or "Not installed"),
Copilot included:
Chief of Staff runs that agent's own unmodified CLI for them, as workers run it (the owner's
environment: their CODEX_HOME, CLAUDE_CONFIG_DIR), and shows what it says: the page to open (Open
sign-in page, Copy link), a one-time code, a field for a code it asks to be pasted (sent to its
stdin), its output. The owner signs in on the agent's own page; Chief of Staff never signs in for
them and never reads an agent's credentials (Claude's tokens or keychain above all: Anthropic's
terms), only runs its binary and its status command. An API key goes to `codex login
--with-api-key` on its stdin: never on a command line, in the output kept, a log or the store.

While a sign-in runs, the agent's status is looked at every POLL_SECONDS (its glance: `codex
login status`, `claude auth status`); once it is signed in, its row turns Ready by itself and
the work it held goes on (Runtime.agent_back). Install runs the official installer the row
shows, word for word, then looks for the agent where installers put it and goes on to Sign in.
One run per agent at a time; Cancel ends its process group.

The same runs do what the page used to tell the owner to run in a terminal, each asked first
where it changes more than Chief of Staff (the command shown, word for word): Update, where an
agent is older than the versions tested (its own updater: `copilot update`, `codex update`,
`claude update`); Trust the sandbox's certificate (`copilot sandbox ca create`, then `trust`:
macOS asks for the owner's password itself), so that gh works inside Copilot's sandbox; and
Sign in on a card for an MCP server a Codex or Claude worker needs signed in (`codex mcp login
<server>`, `claude mcp login <server>`: the agent keeps the sign-in), its card then answered.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import agents
from .backend import OLDEST_CLI, cli_version, older
from .machine import kill_process_tree
from .models import DraftDecision

if TYPE_CHECKING:
    from collections.abc import Callable

    from .runtime import Runtime

# The agents set up from here: Copilot, and the others Chief of Staff runs.
SET_UP = (agents.COPILOT, *agents.NAMES)
COMMANDS = {agents.COPILOT: "copilot", **agents.COMMANDS}
# Each agent's ways to sign in: the CLI's arguments, and the words of each. The first is Sign in's
# (Copilot's: its one-time code, which says what to open and enter whatever runs it).
SIGN_IN = {
    "copilot": {
        "device": ("login", "--device-code"),
        "browser": ("login",),
    },
    "codex": {
        "browser": ("login",),
        "device": ("login", "--device-auth"),
        "key": ("login", "--with-api-key"),
    },
    "claude": {
        "browser": ("auth", "login", "--claudeai"),
        "console": ("auth", "login", "--console"),
    },
}
METHOD_WORDS = {
    "browser": "Sign in",
    "device": "Use a code instead (another device)",
    "key": "Use an API key",
    "console": "Use an Anthropic Console account",
}
WAY_WORDS = {
    "copilot": {"device": "Sign in with a code", "browser": "Sign in in the browser instead"},
}
# The official installers, as each agent's own page gives them: the first runs on Install (the
# exact command its row shows, as the owner's consent), the others under Other ways where their
# tool is here (`needs`). On Windows they are shown, not run.
INSTALLERS = {
    "copilot": (
        ("script", "curl -fsSL https://gh.io/copilot-install | bash", ""),
        ("npm", "npm install -g @github/copilot", "npm"),
        ("brew", "brew install --cask copilot-cli", "brew"),
    ),
    "codex": (
        ("script", "curl -fsSL https://chatgpt.com/codex/install.sh | sh", ""),
        ("npm", "npm install -g @openai/codex", "npm"),
        ("brew", "brew install --cask codex", "brew"),
    ),
    "claude": (
        ("script", "curl -fsSL https://claude.ai/install.sh | bash", ""),
        ("brew", "brew install --cask claude-code", "brew"),
    ),
}
WINDOWS_INSTALL = {
    "copilot": "winget install GitHub.Copilot",
    "codex": (
        'powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"'
    ),
    "claude": "irm https://claude.ai/install.ps1 | iex",
}
# How often a sign-in's agent is looked at while it runs, and how long one may take at most.
POLL_SECONDS = 1.5
RUN_SECONDS = 15 * 60
INSTALL_SECONDS = 10 * 60
# How long its process may take to end once it is signed in (or Cancel asked it to).
END_SECONDS = 10.0
# Lines of its output kept (shown under Show output), and of each line.
TAIL_LINES = 200
LINE_CHARS = 500
# How often what a run shows is sent to the page while it runs, at most.
SEND_SECONDS = 0.25
# How long an ended run stays in the page (its last words, done or failed).
KEPT_SECONDS = 10 * 60
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
URL = re.compile(r"https://[^\s<>\"'`]+")
# A one-time code as `codex login --device-auth` prints it (ABCD-12345).
DEVICE_CODE = re.compile(r"\b[A-Z0-9]{4,5}-[A-Z0-9]{4,6}\b")
PASTE = re.compile(r"paste (?:the |your )?code", re.IGNORECASE)
# How long Chief of Staff may take to start once an agent is signed in, installed or updated
# from here while it waited for one (recover), waited for at most.
START_SECONDS = 90.0
# An MCP server's name, as Codex and Claude Code name them (never an option's dash first).
SERVER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}")
# The run that trusts Copilot's sandbox certificate (its key: one at a time).
SANDBOX = "sandbox"
TRUST_COMMAND = "copilot sandbox ca create && copilot sandbox ca trust"
# What `copilot sandbox ca status` exits with: trusted; no OS trust store here; rotate first.
TRUSTED, NO_TRUST_STORE, ROTATE_FIRST = 0, 4, 5


class SignInAgainError(ValueError):
    """Signed in already: Sign in again is the owner's to confirm (`anyway`, its words)."""

    anyway = "Sign in again"


def stand_in_only(program: str):
    """While Chief of Staff's own tests run, only stand-ins (in their temporary folders) and
    their installers' shell: never the owner's own agent CLI, which a run could change."""
    if "pytest" not in sys.modules and "PYTEST_CURRENT_TEST" not in os.environ:
        return
    if program == "/bin/sh" or Path(program).resolve().is_relative_to(
        Path(tempfile.gettempdir()).resolve()
    ):
        return
    raise RuntimeError(f"not while Chief of Staff's own tests run: {program} is no stand-in")


def keychain() -> bool:
    """Whether Copilot's sandbox certificate is trusted here from the page: macOS (its login
    keychain; elsewhere gh inside the sandbox is not looked at: backend.gh_trusts_sandbox)."""
    return sys.platform == "darwin"


def install_methods(name: str) -> list[dict[str, str]]:
    """The installers of an agent the owner can run from here, Install's first: each one's
    method and its command, word for word; on Windows, the PowerShell command only, to copy."""
    if sys.platform == "win32":
        return [{"method": "windows", "command": WINDOWS_INSTALL[name], "shown": "1"}]
    return [
        {"method": method, "command": command}
        for method, command, needs in INSTALLERS.get(name, ())
        if not needs or shutil.which(needs)
    ]


def sign_in_methods(name: str) -> list[dict[str, str]]:
    words = METHOD_WORDS | WAY_WORDS.get(name, {})
    return [{"method": method, "label": words[method]} for method in SIGN_IN[name]]


def known_dirs() -> list[Path]:
    """Where installers put an agent's CLI, beside the PATH Chief of Staff started with (a new
    terminal's): the official installers' ~/.local/bin, npm's global bin, Homebrew's."""
    home = Path.home()
    found = [home / ".local" / "bin", home / ".npm-global" / "bin"]
    if prefix := os.environ.get("NPM_CONFIG_PREFIX"):
        found.append(Path(prefix) / "bin")
    found += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    return found


async def npm_bin() -> Path | None:
    """npm's global bin, where `npm install -g` put it (`npm prefix -g`)."""
    npm = shutil.which("npm")
    if not npm:
        return None
    with contextlib.suppress(Exception):
        process = await asyncio.create_subprocess_exec(
            npm,
            "prefix",
            "-g",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(process.communicate(), 20)
        if process.returncode == 0 and (prefix := out.decode().strip()):
            return Path(prefix) / "bin"
    return None


def on_path(folder: Path):
    """A folder an agent's CLI was found in, put on Chief of Staff's PATH (last: what the owner's
    PATH found first stays first, Homebrew's or npm's Python or git never in their place): its
    workers and its looks run that CLI from now on, as a new terminal would."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if str(folder) not in parts:
        os.environ["PATH"] = os.pathsep.join([*filter(None, parts), str(folder)])


async def locate(name: str) -> str:
    """An agent's CLI after an install: on the PATH, else where installers put it (put on the
    PATH then); "" when it is nowhere. Its `--version` must answer."""
    command = COMMANDS[name]
    path = shutil.which(command)
    if not path:
        places = known_dirs()
        if (npm := await npm_bin()) and npm not in places:
            places.append(npm)
        for folder in places:
            candidate = folder / command
            if candidate.is_file() and os.access(candidate, os.X_OK):
                path = str(candidate)
                break
    if not path:
        return ""
    try:
        stand_in_only(path)
        process = await asyncio.create_subprocess_exec(
            path,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        await asyncio.wait_for(process.wait(), 30)
    except (OSError, TimeoutError, RuntimeError):
        return ""
    if process.returncode != 0:
        return ""
    on_path(Path(path).parent)
    return path


def kept_paths(store) -> None:
    """Where an install from here put an agent's CLI (agentpath:<name>), on the PATH again as
    Chief of Staff starts: found as before, without a new terminal."""
    for name in SET_UP:
        folder = store.get(f"agentpath:{name}")
        if folder and (Path(folder) / COMMANDS[name]).is_file():
            on_path(Path(folder))


def environment(name: str) -> dict[str, str]:
    """A run's environment: the owner's, as each agent's workers get it (their CODEX_HOME or
    CLAUDE_CONFIG_DIR), without Chief of Staff's own variables; Claude prints its whole sign-in
    link (2.1.330 and later: one a script can read)."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("CHIEFOFSTAFF_")}
    if name == "claude":
        env["CLAUDE_CODE_FULL_SIGN_IN_LINK"] = "1"
    return env


class SetupRun:
    """One sign-in or install, as the page shows it: what the CLI said (its page's link, a
    one-time code, whether it asks for a pasted code), how it ended."""

    def __init__(self, agent: str, kind: str, method: str, command: str, key: str = ""):
        self.agent, self.kind, self.method, self.command = agent, kind, method, command
        self.key = key or agent  # Its runs' (one at a time): the agent's, the sandbox's, a card's.
        self.server = ""  # An MCP server's sign-in: its name.
        self.started = time.time()
        self.ended = 0.0
        self.state = "running"
        self.url = ""
        self.code = ""
        self.asks_code = False
        self.error = ""
        self.tail: deque[str] = deque(maxlen=TAIL_LINES)
        self.partial = {"stdout": "", "stderr": ""}
        self.process: asyncio.subprocess.Process | None = None
        self.readers: list[asyncio.Task] = []
        self.secret = ""
        self.follows = False  # An install whose Sign in follows (it is signed out).
        self.starting = False  # Its agent ready: Chief of Staff starts on it (Setups.ready_here).
        self.seq = 0  # Each change counted: the page keeps the newest it heard.
        self.changed: Callable[[], None] | None = None  # Called as what it shows changes.

    def view(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "key": self.key,
            "kind": self.kind,
            "method": self.method,
            **({"command": self.command} if self.kind != "signin" else {}),
            **({"server": self.server} if self.server else {}),
            "state": self.state,
            "started": self.started,
            "seq": self.seq,
            **({"ended": self.ended} if self.ended else {}),
            **({"url": self.url} if self.url else {}),
            **({"code": self.code} if self.code else {}),
            **({"asks_code": True} if self.asks_code else {}),
            **({"error": self.error} if self.error else {}),
            **({"follows": "signin"} if self.follows else {}),
            **({"starting": True} if self.starting else {}),
            "tail": list(self.tail)[-40:],
        }

    def hidden(self, text: str) -> str:
        return text.replace(self.secret, "•••") if self.secret else text

    def heard(self, stream: str, chunk: str):
        """What it printed, read as it comes: its lines kept (without colours), its link, code
        and paste prompt found (on a line not ended too: a prompt waits on its own line)."""
        text = self.partial[stream] + ANSI.sub("", chunk).replace("\r\n", "\n").replace("\r", "\n")
        *lines, self.partial[stream] = text.split("\n")
        if PASTE.search(self.partial[stream]):
            # A prompt waiting on its own line: a line of its own (what follows is not of it).
            lines.append(self.partial[stream])
            self.partial[stream] = ""
        for line in lines:
            if line.strip():
                self.tail.append(self.hidden(line.rstrip())[:LINE_CHARS])
            self.read(line)
        self.seq += 1
        if self.changed:
            self.changed()

    def read(self, line: str):
        if PASTE.search(line) and self.state == "running":
            self.asks_code = True
        if not self.url:
            for found in URL.findall(line):
                link = found.rstrip(".,;:)]}")
                if len(link) <= 4000 and "://localhost" not in link:
                    self.url = link
                    break
        if self.method == "device" and not self.code and (code := DEVICE_CODE.search(line)):
            self.code = code.group(0)

    async def start(self, arguments: list[str], cwd: Path, env: dict[str, str], secret: str = ""):
        stand_in_only(arguments[0])
        self.process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        for stream in ("stdout", "stderr"):
            self.readers.append(asyncio.ensure_future(self.reading(stream)))
        if secret:
            self.secret = secret
            await self.send(secret, close=True)

    async def reading(self, stream: str):
        source = getattr(self.process, stream)
        while chunk := await source.read(4096):
            self.heard(stream, chunk.decode(errors="replace"))
        if rest := self.partial[stream].strip():
            self.tail.append(self.hidden(rest)[:LINE_CHARS])
            self.partial[stream] = ""

    async def send(self, text: str, close: bool = False):
        """A line to its stdin (a code it asked for; an API key, its stdin closed after it)."""
        if not self.process or not self.process.stdin or self.state != "running":
            raise ValueError("That sign-in is over: start it again.")
        self.process.stdin.write(text.strip().encode() + b"\n")
        with contextlib.suppress(ConnectionError):
            await self.process.stdin.drain()
        if close:
            self.process.stdin.close()
        self.asks_code = False
        self.seq += 1

    def terminate(self):
        """Its process group asked to end (Windows: its process)."""
        if not self.process or self.process.returncode is not None:
            return
        if sys.platform == "win32":
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.process.pid, signal.SIGTERM)

    async def end(self):
        """Its process group ended: asked first, then killed, as it leaves nothing running."""
        if not self.process:
            return
        self.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), END_SECONDS / 2)
        except TimeoutError:
            kill_process_tree(self.process.pid)
            await self.process.wait()
        if sys.platform != "win32":
            # A child its shell forked as it ended (an installer's): the group again.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.process.pid, signal.SIGKILL)

    async def wait(self) -> int:
        if not self.process:
            return -1
        code = await self.process.wait()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.gather(*self.readers), 5)
        return code

    def finish(self, state: str, error: str = ""):
        if self.state != "running":
            return
        self.state, self.error, self.ended = state, error, time.time()
        self.asks_code = False
        self.seq += 1
        if self.changed:
            self.changed()

    def last_words(self) -> str:
        """Why it failed, as it said it: its last line (the rest under Show output)."""
        lines = [line.strip() for line in self.tail if line.strip()]
        return lines[-1][:300] if lines else ""


class Setups:
    """The sign-ins, installs and updates the owner started from the page, one per agent at a
    time (and one trust of the sandbox's certificate, one sign-in per MCP card)."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.runs: dict[str, SetupRun] = {}
        self.sent_at: dict[str, float] = {}
        self.later: set[str] = set()

    def view(self) -> dict[str, dict]:
        """Each agent's run, while it runs and a little after (its last words)."""
        now = time.time()
        return {
            name: run.view()
            for name, run in self.runs.items()
            if run.state == "running" or now - run.ended < KEPT_SECONDS
        }

    def running(self, name: str) -> SetupRun | None:
        run = self.runs.get(name)
        return run if run and run.state == "running" else None

    def changed(self, run: SetupRun):
        """What a run shows, sent to the page: at most every SEND_SECONDS while it runs (what
        came meanwhile, sent then), at once as it ends; never one another run replaced."""
        if self.runs.get(run.key) not in (run, None):
            return
        wait = SEND_SECONDS - (time.monotonic() - self.sent_at.get(run.key, 0.0))
        if run.state == "running" and wait > 0:
            if run.key not in self.later:
                self.later.add(run.key)
                asyncio.get_running_loop().call_later(wait, self.send_later, run)
            return
        self.sent_at[run.key] = time.monotonic()
        self.runtime.bus.publish("agent_setup", run.view())

    def send_later(self, run: SetupRun):
        self.later.discard(run.key)
        self.changed(run)

    def check(self, name: str):
        if name not in SET_UP:
            raise LookupError(f"Chief of Staff does not run {name!r}.")
        if getattr(self.runtime, "demo", False):
            raise ValueError("Not in the demo: it has no coding agents of its own.")

    def command(self, name: str) -> str:
        """An agent's command: Copilot's as Settings names it."""
        if name == agents.COPILOT:
            return self.runtime.store.settings().copilot_command or COMMANDS[name]
        return COMMANDS[name]

    async def begin(
        self,
        run: SetupRun,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        *,
        key: str = "",
        failed: str = "",
    ) -> dict:
        """A run started, as the page shows it, and watched to its end (watch)."""
        run.changed = lambda: self.changed(run)
        self.runs[run.key] = run
        try:
            await run.start(argv, cwd, env, secret=key)
        except OSError as exc:
            self.runs.pop(run.key, None)
            raise ValueError(f"{failed or 'It'} could not start: {exc.strerror}") from None
        self.runtime.background(self.watch(run))
        self.runtime.publish_state()
        return run.view()

    async def sign_in(
        self, name: str, method: str = "", confirm: bool = False, key: str = ""
    ) -> dict:
        """Sign in to an agent with its own CLI (SIGN_IN), as its workers run it: the run as the
        page shows it. One running already is that one. Signed in already, it asks first (`codex
        login` signs you out first). An API key goes to its stdin, kept nowhere."""
        self.check(name)
        if sys.platform == "win32":
            raise ValueError(f"Sign in to {agents.display(name)} in a terminal on Windows.")
        method = method or next(iter(SIGN_IN[name]))
        if method not in SIGN_IN[name]:
            raise ValueError(f"{agents.display(name)} has no sign-in like {method!r}.")
        if method == "key" and not key.strip():
            raise ValueError("Paste your API key first.")
        if run := self.running(name):
            if run.kind == "signin" and run.method == method:
                return run.view()
            await self.cancel(name)
        path = shutil.which(self.command(name))
        if not path:
            raise ValueError(f"{agents.display(name)} is not installed: install it first.")
        status = self.runtime.agent_status(name)
        if status.get("status") == agents.READY and not confirm:
            first = {
                "codex": "This signs you out of Codex first: its workers stop until you're "
                "signed in again.",
                "claude": "You're signed in to Claude already.",
            }.get(name, "You're signed in to Copilot already.")
            raise SignInAgainError(first)
        argv = [path, *SIGN_IN[name][method]]
        run = SetupRun(name, "signin", method, " ".join([self.command(name), *argv[1:]]))
        if name == agents.COPILOT:
            folder = Path.home()  # (Copilot keeps its sign-in for the owner's account.)
        else:
            folder = self.runtime.agent_folder(name)
            await asyncio.to_thread(folder.mkdir, mode=0o700, parents=True, exist_ok=True)
        return await self.begin(
            run, argv, folder, environment(name), key=key.strip(), failed=agents.display(name)
        )

    async def install(self, name: str, method: str = "script") -> dict:
        """Install an agent with the official installer its row shows (install_methods), word
        for word: the run as the page shows it. Then it is looked for where installers put it,
        and Sign in follows."""
        self.check(name)
        if sys.platform == "win32":
            raise ValueError("On Windows, copy the command and run it in PowerShell.")
        offered = {found["method"]: found["command"] for found in install_methods(name)}
        if method not in offered:
            raise ValueError(f"{method!r} is not a way to install {agents.display(name)} here.")
        if run := self.running(name):
            if run.kind == "install":
                return run.view()
            await self.cancel(name)
        run = SetupRun(name, "install", method, offered[method])
        argv = ["/bin/sh", "-c", offered[method]]
        return await self.begin(run, argv, Path.home(), environment(name), failed="The installer")

    async def update(self, name: str) -> dict:
        """Update an agent older than the versions tested with its own updater (`copilot
        update`, `codex update`, `claude update`), which the page showed, word for word, and the
        owner said yes to: it changes that agent for every use of it here. Then it is looked at
        again: its row says Ready, or why not."""
        self.check(name)
        command = self.command(name)
        if sys.platform == "win32":
            raise ValueError(f"On Windows, run {command} update in a terminal.")
        if run := self.running(name):
            if run.kind == "update":
                return run.view()
            raise ValueError(f"{agents.display(name)} is being set up: let that finish first.")
        path = shutil.which(command)
        if not path:
            raise ValueError(f"{agents.display(name)} is not installed: install it first.")
        run = SetupRun(name, "update", "update", f"{command} update")
        return await self.begin(
            run, [path, "update"], Path.home(), environment(name), failed=agents.display(name)
        )

    async def trust_sandbox(self) -> dict:
        """Trust Copilot's sandbox certificate (`copilot sandbox ca create`, then `trust`; one
        whose key must change first, `rotate`), as the owner said yes to: it goes into their
        login keychain (macOS asks for their password itself), and gh works inside the sandbox
        from then on. Claude Code's and Codex's sandboxes need none (gh_works_in_sandbox)."""
        if getattr(self.runtime, "demo", False):
            raise ValueError("Not in the demo: it has no coding agents of its own.")
        if not keychain():
            raise ValueError(f"Run {TRUST_COMMAND} in a terminal on this system.")
        if run := self.running(SANDBOX):
            return run.view()
        command = self.command(agents.COPILOT)
        path = shutil.which(command)
        if not path:
            raise ValueError("Copilot is not installed: install it first.")
        run = SetupRun(agents.COPILOT, "trust", "trust", TRUST_COMMAND, key=SANDBOX)
        run.changed = lambda: self.changed(run)
        self.runs[SANDBOX] = run
        self.runtime.background(self.trusting(run, path))
        self.runtime.publish_state()
        return run.view()

    async def step(self, run: SetupRun, argv: list[str], deadline: float) -> int:
        """One command of a run of several, to its end (said in its output first); -1 when
        the run ended meanwhile (Cancel) or it took too long."""
        if run.state != "running":
            return -1
        run.tail.append(f"$ {shlex.join([Path(argv[0]).name, *argv[1:]])}")
        await run.start(argv, Path.home(), environment(run.agent))
        try:
            return await asyncio.wait_for(run.wait(), max(1.0, deadline - time.monotonic()))
        except TimeoutError:
            run.finish("failed", "It took too long: try again.")
            await run.end()
            return -1

    async def trusting(self, run: SetupRun, path: str):
        """Its steps, one after another (its status first: trusted already, nothing more), then
        its status again: gh works inside the sandbox from now on (Settings says so)."""
        deadline = time.monotonic() + INSTALL_SECONDS
        try:
            found = await self.step(run, [path, "sandbox", "ca", "status"], deadline)
            if found == NO_TRUST_STORE:
                run.finish("failed", "This Mac has no trust store Copilot can add it to.")
            elif found not in (TRUSTED, -1):
                steps = ("rotate", "trust") if found == ROTATE_FIRST else ("create", "trust")
                for name in steps:
                    code = await self.step(run, [path, "sandbox", "ca", name], deadline)
                    if code != 0 and run.state == "running":
                        said = run.last_words()
                        run.finish("failed", said if not said.startswith("$ ") else (
                            f"copilot sandbox ca {name} did not finish."))  # fmt: skip
                    if run.state != "running":
                        break
                if run.state == "running":
                    found = await self.step(run, [path, "sandbox", "ca", "status"], deadline)
            if run.state == "running":
                self.runtime.sandbox_gh = found == TRUSTED
                if found == TRUSTED:
                    run.finish("done")
                else:
                    run.finish("failed", "Copilot still says its certificate is not trusted.")
        except Exception as exc:
            run.finish("failed", str(exc) or "It did not finish: try again.")
            await run.end()
        self.changed(run)
        self.runtime.publish_state()

    async def mcp_sign_in(self, draft_id: str) -> dict:
        """Sign in to an MCP server a Codex or Claude worker needs, from its card: that agent's
        own command (`codex mcp login <server>`, `claude mcp login <server>`), which keeps the
        sign-in, run in the worker's folder (where its project's servers are named); the card is
        answered once it is done, as I signed in did."""
        if getattr(self.runtime, "demo", False):
            raise ValueError("Not in the demo: it has no coding agents of its own.")
        draft = self.runtime.store.draft(draft_id)
        target = draft["target"]
        agent, server = str(target.get("agent") or ""), str(target.get("server") or "")
        if draft["kind"] != "signin" or agent not in agents.NAMES or not target.get("command"):
            raise ValueError("That sign-in is not one Chief of Staff can run.")
        if draft["status"] != "pending":
            raise ValueError("That sign-in is over.")
        if not SERVER_NAME.fullmatch(server):
            raise ValueError(f"Run {target['command']} in a terminal to sign in to it.")
        if sys.platform == "win32":
            raise ValueError(f"Run {target['command']} in a terminal on Windows.")
        key = f"mcp:{draft_id}"
        if run := self.running(key):
            return run.view()
        path = shutil.which(agents.command(agent))
        if not path:
            raise ValueError(f"{agents.display(agent)} is not installed here any more.")
        row = self.runtime.store.find_session(draft["session_id"]) or {}
        folder = Path(row.get("cwd") or Path.home())
        if not await asyncio.to_thread(folder.is_dir):
            folder = Path.home()
        command = f"{agents.command(agent)} mcp login {shlex.quote(server)}"
        run = SetupRun(agent, "mcp", "browser", command, key=key)
        run.server = server
        argv = [path, "mcp", "login", server]
        return await self.begin(run, argv, folder, environment(agent), failed=agents.display(agent))

    async def send(self, name: str, text: str) -> dict:
        """A code the agent's sign-in asked to be pasted, to its stdin."""
        self.check(name)
        run = self.running(name)
        if not run or run.kind != "signin":
            raise ValueError("That sign-in is over: start it again.")
        if not text.strip():
            raise ValueError("Paste the code the sign-in page showed.")
        await run.send(text)
        run.tail.append("(code sent)")
        run.seq += 1
        self.changed(run)
        return run.view()

    async def cancel(self, name: str) -> dict:
        """Cancel: its process group ends. (`codex login` signed you out as it began.)"""
        if name not in self.runs:
            self.check(name)
        run = self.runs.get(name)
        if not run:
            raise LookupError("Nothing is being set up for it.")
        if run.state == "running":
            run.finish("cancelled")
            await run.end()
            self.runtime.publish_state()
        return run.view()

    async def stop(self):
        """Chief of Staff stops: each run still going ends with it (its process group), as
        Cancel does; none is left waiting (a sign-in on its local port, say)."""
        for run in list(self.runs.values()):
            if run.state == "running":
                run.finish("cancelled")
                with contextlib.suppress(Exception):
                    await run.end()

    async def ready_now(self, name: str) -> bool:
        if name == agents.COPILOT:
            return await self.copilot_up()
        try:
            await self.runtime.glance_agent(name)
        except Exception:
            return False
        return True

    async def start_now(self):
        """recover's step now, not at its next one (minutes later, maybe): Chief of Staff
        started on what is ready now; waited for START_SECONDS at most (it goes on after)."""
        started = self.runtime.background(self.runtime.recover_now())
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(started), START_SECONDS)

    async def ready_here(self, run: SetupRun):
        """Codex or Claude Code made ready from here (signed in, installed, updated) while
        Chief of Staff waits for an agent (Home's first-run card): it starts now, the run saying
        so meanwhile, and is done once it runs; never "ready" while the card still says none
        is. Running already, nothing more (the work it held goes on: Runtime.agent_up)."""
        if self.runtime.ready() or getattr(self.runtime, "stopping", False):
            return
        run.starting = True
        run.seq += 1
        self.changed(run)
        await self.start_now()
        run.starting = False
        run.seq += 1

    async def copilot_up(self) -> bool:
        """Copilot signed in or installed from here: started now (start_now), as Chief of
        Staff would by itself later; whether it is ready (or, off, found)."""
        await self.start_now()
        return self.runtime.agent_status(agents.COPILOT).get("status") == agents.READY

    async def watch(self, run: SetupRun):
        """A run to its end: a sign-in is done once its agent says it is signed in (looked at
        every POLL_SECONDS, and as it ends); an install, once its CLI answers where installers
        put it, then Sign in follows if it is not signed in."""
        limit = RUN_SECONDS if run.kind in {"signin", "mcp"} else INSTALL_SECONDS
        ending = asyncio.ensure_future(run.wait())
        deadline = time.monotonic() + limit
        try:
            while not ending.done():
                await asyncio.wait({ending}, timeout=POLL_SECONDS)
                if ending.done() or run.state != "running":
                    break
                if time.monotonic() > deadline:
                    run.finish("failed", "It took too long: try again.")
                    break
                # (Copilot's sign-in, and an MCP server's, end by themselves once it is done.)
                polled = run.kind == "signin" and run.agent != agents.COPILOT
                if polled and await self.ready_now(run.agent):
                    await self.ready_here(run)
                    run.finish("done")  # Its own process ends by itself now.
                    break
            if not ending.done():
                try:
                    await asyncio.wait_for(asyncio.shield(ending), END_SECONDS)
                except TimeoutError:
                    await run.end()
            code = await ending
        except Exception as exc:
            run.finish("failed", str(exc))
            await run.end()
            code = -1
        if run.state == "cancelled":
            self.changed(run)
            self.runtime.publish_state()
            return
        follow = False
        if run.kind == "signin":
            await self.signed_in(run, code)
        elif run.kind == "update":
            await self.updated(run, code)
        elif run.kind == "mcp":
            await self.mcp_signed_in(run, code)
        else:
            follow = run.follows = await self.installed(run, code)
        self.changed(run)
        self.runtime.publish_state()
        if follow:
            # Installed and signed out: straight on to Sign in.
            with contextlib.suppress(Exception):
                await self.sign_in(run.agent)

    async def signed_in(self, run: SetupRun, code: int):
        """A sign-in's end: done once its agent says it is signed in (its row turns Ready and
        what it held goes on: Runtime.agent_up, from that look), else why not."""
        if run.state != "running":
            return
        if run.agent == agents.COPILOT and code == 0:
            # Signed in (its CLI says so as it ends): Copilot starts now.
            await self.copilot_up()
            run.finish("done")
        elif run.agent != agents.COPILOT and await self.ready_now(run.agent):
            await self.ready_here(run)
            run.finish("done")
        elif code == 0:
            run.finish("failed", f"{agents.display(run.agent)} still says it is signed out.")
        else:
            run.finish("failed", run.last_words() or "Sign-in did not finish: try again.")

    async def installed(self, run: SetupRun, code: int) -> bool:
        """An install's end: its CLI found and answering (where installers put it, kept for
        next time), its status looked at; whether Sign in follows (it is signed out)."""
        name = run.agent
        if run.state != "running":
            return False
        if code != 0:
            run.finish("failed", run.last_words() or "The installer failed: try again.")
            return False
        path = await locate(name)
        if not path:
            run.finish(
                "failed",
                f"Installed, but {agents.command(name)} was not found where installers put it: "
                "open a new terminal and run it once.",
            )
            return False
        self.runtime.store.set(f"agentpath:{name}", str(Path(path).parent))
        self.runtime.found_cache = (0.0, [])
        run.tail.append(f"Found {path}")
        if name == agents.COPILOT:
            run.finish("done")
            self.runtime.copilot_installed()
            await self.copilot_up()
            return self.runtime.agent_status(name).get("status") == agents.SIGN_IN
        ready = await self.ready_now(name)
        if ready:
            await self.ready_here(run)  # (Signed in already: it starts on it now.)
        run.finish("done")
        return not ready

    async def updated(self, run: SetupRun, code: int):
        """An update's end: done once the agent is no longer older than the versions tested
        (Codex and Claude Code looked at again, their rows saying so; Copilot's version asked:
        Chief of Staff runs the new one from Copilot's next start), else why not."""
        name = run.agent
        if run.state != "running":
            return
        if code != 0:
            run.finish("failed", run.last_words() or "The update did not finish: try again.")
            return
        if name == agents.COPILOT:
            version = await cli_version(shutil.which(self.command(name)) or self.command(name))
            if version and older(version, OLDEST_CLI):
                run.finish("failed", f"Copilot CLI is still {version} after its update.")
                return
            self.runtime.copilot_updated(version)
            run.finish("done")
            return
        ready = await self.ready_now(name)
        status = self.runtime.agent_status(name)
        if status.get("update"):
            run.finish("failed", f"Still not a version tested: {status.get('said', '')}".strip())
            return
        if ready:
            await self.ready_here(run)
        run.finish("done")

    async def mcp_signed_in(self, run: SetupRun, code: int):
        """An MCP server's sign-in's end: its card answered (the worker told, its session
        refreshed: Asks.on_mcp_auth), else why not."""
        if run.state != "running":
            return
        if code != 0:
            run.finish("failed", run.last_words() or "Sign-in did not finish: try again.")
            return
        run.finish("done")
        draft_id = run.key.removeprefix("mcp:")
        with contextlib.suppress(LookupError, ValueError):
            if self.runtime.store.draft(draft_id)["status"] == "pending":
                await self.runtime.drafts.resolve(draft_id, DraftDecision(action="answer"))
