"""Signing in to another coding agent, and installing one, from the page (design/agent-smooth.md).

The owner clicks Sign in (or Install) where an agent says "Sign-in needed" (or "Not installed"):
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
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import sys
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import agents
from .machine import kill_process_tree

if TYPE_CHECKING:
    from collections.abc import Callable

    from .runtime import Runtime

# Each agent's ways to sign in: the CLI's arguments, and the words of each. The first is Sign in's.
SIGN_IN = {
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
# The official installers, as each agent's own page gives them: the first runs on Install (the
# exact command its row shows, as the owner's consent), the others under Other ways where their
# tool is here (`needs`). On Windows they are shown, not run.
INSTALLERS = {
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


class SignInAgainError(ValueError):
    """Signed in already: Sign in again is the owner's to confirm (`anyway`, its words)."""

    anyway = "Sign in again"


def install_methods(name: str) -> list[dict[str, str]]:
    """The installers of an agent the owner can run from here, Install's first: each one's
    method and its command, word for word; on Windows, the PowerShell command only, to copy."""
    if sys.platform == "win32":
        return [{"method": "windows", "command": WINDOWS_INSTALL[name], "shown": "1"}]
    return [
        {"method": method, "command": command}
        for method, command, needs in INSTALLERS[name]
        if not needs or shutil.which(needs)
    ]


def sign_in_methods(name: str) -> list[dict[str, str]]:
    return [{"method": method, "label": METHOD_WORDS[method]} for method in SIGN_IN[name]]


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
    """A folder an agent's CLI was found in, put on Chief of Staff's PATH (first): its workers
    and its looks run that CLI from now on, as a new terminal would."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if str(folder) not in parts:
        os.environ["PATH"] = os.pathsep.join([str(folder), *filter(None, parts)])


async def locate(name: str) -> str:
    """An agent's CLI after an install: on the PATH, else where installers put it (put on the
    PATH then); "" when it is nowhere. Its `--version` must answer."""
    command = agents.COMMANDS[name]
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
        process = await asyncio.create_subprocess_exec(
            path,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        await asyncio.wait_for(process.wait(), 30)
    except (OSError, TimeoutError):
        return ""
    if process.returncode != 0:
        return ""
    on_path(Path(path).parent)
    return path


def kept_paths(store) -> None:
    """Where an install from here put an agent's CLI (agentpath:<name>), on the PATH again as
    Chief of Staff starts: found as before, without a new terminal."""
    for name in agents.NAMES:
        folder = store.get(f"agentpath:{name}")
        if folder and (Path(folder) / agents.COMMANDS[name]).is_file():
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

    def __init__(self, agent: str, kind: str, method: str, command: str):
        self.agent, self.kind, self.method, self.command = agent, kind, method, command
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
        self.seq = 0  # Each change counted: the page keeps the newest it heard.
        self.changed: Callable[[], None] | None = None  # Called as what it shows changes.

    def view(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "kind": self.kind,
            "method": self.method,
            **({"command": self.command} if self.kind == "install" else {}),
            "state": self.state,
            "started": self.started,
            "seq": self.seq,
            **({"ended": self.ended} if self.ended else {}),
            **({"url": self.url} if self.url else {}),
            **({"code": self.code} if self.code else {}),
            **({"asks_code": True} if self.asks_code else {}),
            **({"error": self.error} if self.error else {}),
            **({"follows": "signin"} if self.follows else {}),
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
    """The sign-ins and installs the owner started from the page, one per agent at a time."""

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
        if self.runs.get(run.agent) not in (run, None):
            return
        wait = SEND_SECONDS - (time.monotonic() - self.sent_at.get(run.agent, 0.0))
        if run.state == "running" and wait > 0:
            if run.agent not in self.later:
                self.later.add(run.agent)
                asyncio.get_running_loop().call_later(wait, self.send_later, run)
            return
        self.sent_at[run.agent] = time.monotonic()
        self.runtime.bus.publish("agent_setup", run.view())

    def send_later(self, run: SetupRun):
        self.later.discard(run.agent)
        self.changed(run)

    def check(self, name: str):
        if name not in agents.NAMES:
            raise LookupError(f"Chief of Staff does not run {name!r}.")
        if getattr(self.runtime, "demo", False):
            raise ValueError("Not in the demo: it has no coding agents of its own.")

    async def sign_in(
        self, name: str, method: str = "browser", confirm: bool = False, key: str = ""
    ) -> dict:
        """Sign in to an agent with its own CLI (SIGN_IN), as its workers run it: the run as the
        page shows it. One running already is that one. Signed in already, it asks first (`codex
        login` signs you out first). An API key goes to its stdin, kept nowhere."""
        self.check(name)
        if sys.platform == "win32":
            raise ValueError(f"Sign in to {agents.display(name)} in a terminal on Windows.")
        if method not in SIGN_IN[name]:
            raise ValueError(f"{agents.display(name)} has no sign-in like {method!r}.")
        if method == "key" and not key.strip():
            raise ValueError("Paste your API key first.")
        if run := self.running(name):
            if run.kind == "signin" and run.method == method:
                return run.view()
            await self.cancel(name)
        path = shutil.which(agents.command(name))
        if not path:
            raise ValueError(f"{agents.display(name)} is not installed: install it first.")
        status = self.runtime.agent_status(name)
        if status.get("status") == agents.READY and not confirm:
            first = (
                "This signs you out of Codex first: its workers stop until you're signed in again."
                if name == "codex"
                else "You're signed in to Claude already."
            )
            raise SignInAgainError(first)
        argv = [path, *SIGN_IN[name][method]]
        run = SetupRun(name, "signin", method, " ".join([agents.command(name), *argv[1:]]))
        run.changed = lambda: self.changed(run)
        folder = self.runtime.agent_folder(name)
        await asyncio.to_thread(folder.mkdir, mode=0o700, parents=True, exist_ok=True)
        self.runs[name] = run
        try:
            await run.start(argv, folder, environment(name), secret=key.strip())
        except OSError as exc:
            self.runs.pop(name, None)
            raise ValueError(f"{agents.display(name)} could not start: {exc.strerror}") from None
        self.runtime.background(self.watch(run))
        self.runtime.publish_state()
        return run.view()

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
        run.changed = lambda: self.changed(run)
        home = Path.home()
        self.runs[name] = run
        try:
            await run.start(["/bin/sh", "-c", offered[method]], home, environment(name))
        except OSError as exc:
            self.runs.pop(name, None)
            raise ValueError(f"The installer could not start: {exc.strerror}") from None
        self.runtime.background(self.watch(run))
        self.runtime.publish_state()
        return run.view()

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
        self.check(name)
        run = self.runs.get(name)
        if not run:
            raise LookupError("Nothing is being set up for it.")
        if run.state == "running":
            run.finish("cancelled")
            await run.end()
            self.runtime.publish_state()
        return run.view()

    async def ready_now(self, name: str) -> bool:
        try:
            await self.runtime.glance_agent(name)
        except Exception:
            return False
        return True

    async def watch(self, run: SetupRun):
        """A run to its end: a sign-in is done once its agent says it is signed in (looked at
        every POLL_SECONDS, and as it ends); an install, once its CLI answers where installers
        put it, then Sign in follows if it is not signed in."""
        limit = RUN_SECONDS if run.kind == "signin" else INSTALL_SECONDS
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
                if run.kind == "signin" and await self.ready_now(run.agent):
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
        if await self.ready_now(run.agent):
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
        run.finish("done")
        return not await self.ready_now(name)
