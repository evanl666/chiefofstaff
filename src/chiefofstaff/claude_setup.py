"""Claude workers' Agent SDK (Chief of Staff's Claude extra), set up by Chief of Staff itself: the
owner runs no command.

While Claude is in use (on for workers, or a role put on it) and the SDK is missing or one this
release was not tested with (backend_claude.sdk_refused), Chief of Staff installs the tested one
in the background: at its start, as the owner turns Claude on, and on Retry; one install at a
time. Its row says "Setting up Claude…" meanwhile, then Ready, without a restart (the SDK is
imported only as a worker starts, and its version is read anew each time).

What is installed is the extra's own requirement, from Chief of Staff's metadata (else the range
backend_claude was tested with): never anything the owner or a model typed. It runs as an
argument list, never through a shell, for SETUP_SECONDS at most; its output goes to the log.

Where:
- Into Chief of Staff's own environment, where that one stays and can be written (a `uv tool`'s,
  pipx's, any virtual environment's): `uv pip install --python <its Python>`, else that Python's
  `-m pip install` (a `uv tool` environment has no pip).
- Otherwise (started with uvx: its environment is in uv's cache, uv's alone to change; or a
  Python that is not a virtual environment's) into a folder of Chief of Staff's own in its data
  directory (SIDE), put last on sys.path: Chief of Staff's own packages always come first, and
  the SDK's requirements are held to the very versions installed there (a constraints file), so
  that folder only adds what was missing and replaces nothing.

On its own it tries once per Chief of Staff version, reason and place (kv TRIED: an upgrade or a
reinstall that dropped the SDK is set up again at the next start, a failure is not tried again
and again); Retry tries again. A failure is said in a few words; the command to run instead is under
Details, as a last resort.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import platform
import re
import shlex
import shutil
import sys
import sysconfig
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import agents, updates
from .machine import kill_process_tree
from .send import throwaway

if TYPE_CHECKING:
    from .store import Store

logger = logging.getLogger(__name__)

SDK = "claude-agent-sdk"
# How long an install may take, and how long a Claude worker's start waits for one under way.
SETUP_SECONDS = 180.0
WAIT_SECONDS = 30.0
# How long what sdk_refused said stands (it reads the installed packages' metadata).
CHECK_SECONDS = 2.0
# Each automatic try (kv, by Chief of Staff's version, why and where): {"ok": true, false, or
# null while it runs (cut short: tried once more), "reason", "tries", "at"}.
TRIED = "claudesetup:"
# Lines of an install's output kept for Details (redacted), and of each line.
TAIL_LINES = 20
LINE_CHARS = 300

SETTING_UP = "Setting up Claude…"
BEING_SET_UP = "Claude is being set up"
FAILED = "Couldn't set up Claude"
NEEDED = "Claude isn't set up yet"
NOT_SET_UP = "Not set up yet"
NO_INSTALLER = "neither uv nor pip is here to install it"
TOO_LONG = "it took too long"
STILL_REFUSED = "it still can't be used after installing"

# The extra's requirement as Chief of Staff's metadata says it ("claude-agent-sdk<0.3,>=0.2.182;
# extra == 'claude'"): this one package, and version clauses only.
EXTRA_MARKER = re.compile(r"""\A\s*extra\s*==\s*(['"])claude\1\s*\Z""")
CLAUSE = r"(?:===|==|!=|~=|<=|>=|<|>)\s*[0-9][0-9A-Za-z.*+!-]*"
REQUIREMENT = re.compile(
    rf"\A\s*claude[-_.]agent[-_.]sdk\s*\(?\s*({CLAUSE}(?:\s*,\s*{CLAUSE})*)\s*\)?\s*\Z",
    re.IGNORECASE,
)
# Why an install failed, in a few words, from what it printed.
REASONS = (
    (
        re.compile(
            r"failed to fetch|failed to download|could not connect|connection (?:refused|reset|"
            r"error|aborted)|network is unreachable|no route to host|name or service not known|"
            r"nodename nor servname|temporary failure in name resolution|dns error|failed to "
            r"resolve|could not resolve host|max retries exceeded|newconnectionerror|"
            r"connecttimeout|timed out|request failed after|offline|network (?:was|is) disabled|"
            r"network connectivity is disabled",
            re.IGNORECASE,
        ),
        "no network",
    ),
    (re.compile(r"no space left on device", re.IGNORECASE), "the disk is full"),
    (
        re.compile(
            r"permission denied|read-only file system|operation not permitted|errno 13",
            re.IGNORECASE,
        ),
        "its folder can't be written",
    ),
    (
        re.compile(
            r"no solution found|no matching distribution|could not find a version|"
            r"resolutionimpossible|unsatisfiable",
            re.IGNORECASE,
        ),
        "no tested version can be installed here",
    ),
)
# A password in an address an installer printed (a private index's): never kept or logged.
CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/@]+@", re.IGNORECASE)

Runner = Callable[[list[str], float], Awaitable[tuple[int | None, str]]]


def requirement(requires: list[str] | None = None) -> str:
    """What to install: the Claude extra's requirement as Chief of Staff's metadata says it, else
    the range backend_claude was tested with (fallback)."""
    if requires is None:
        try:
            requires = metadata.requires("chiefofstaff") or []
        except metadata.PackageNotFoundError:
            requires = []
    for entry in requires:
        spec, _, marker = entry.partition(";")
        if not EXTRA_MARKER.match(marker):
            continue
        if found := REQUIREMENT.match(spec):
            return SDK + re.sub(r"\s+", "", found.group(1))
    return fallback()


def fallback() -> str:
    from .backend_claude import SDK_BEFORE, SDK_OLDEST

    oldest, before = ".".join(map(str, SDK_OLDEST)), ".".join(map(str, SDK_BEFORE))
    return f"{SDK}>={oldest},<{before}"


def find_uv(environ: Mapping[str, str] | None = None, home: Path | None = None) -> str:
    """uv, where it is: the one that started Chief of Staff (UV), on the PATH, or where its
    installers put it (~/.local/bin, ~/.cargo/bin, Homebrew's); "" when it is nowhere."""
    found = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    places = [
        found.get("UV", ""),
        shutil.which("uv", path=found.get("PATH")) or "",
        str(home / ".local" / "bin" / "uv"),
        str(home / ".cargo" / "bin" / "uv"),
        "/opt/homebrew/bin/uv",
        "/usr/local/bin/uv",
    ]
    for place in places:
        if (
            place
            and Path(place).name == "uv"
            and Path(place).is_file()
            and os.access(place, os.X_OK)
        ):
            return place
    return ""


def has_pip() -> bool:
    """Whether Chief of Staff's own Python has pip (a `uv tool` environment has none)."""
    return importlib.util.find_spec("pip") is not None


def own_environment(prefix: str = sys.prefix, base: str = sys.base_prefix) -> bool:
    """Whether Chief of Staff's own environment is one to install into: a virtual environment
    that stays (not uvx's, in uv's cache) and can be written."""
    if throwaway(prefix):
        return False
    with contextlib.suppress(OSError):
        if Path(prefix).resolve() == Path(base).resolve():
            return False
    return os.access(sysconfig.get_paths()["purelib"], os.W_OK)


def made() -> str:
    """Which install of Chief of Staff this is (its METADATA file's inode and time): installed
    again (`uv tool install --reinstall`, an upgrade), it is another, set up again by itself."""
    try:
        found = metadata.distribution("chiefofstaff")
        files = [f for f in found.files or [] if f.name == "METADATA" and f.parent.suffix]
        info = Path(str(files[0].locate())).stat()
    except (metadata.PackageNotFoundError, IndexError, OSError):
        return ""
    return f"{info.st_ino}-{info.st_mtime_ns}"


def side_folder(directory: Path) -> Path:
    """Chief of Staff's own folder for the SDK (where its own environment is not one to install
    into), in its data directory: one per Python and machine."""
    machine = platform.machine().lower() or "any"
    return (
        directory / "extras" / f"claude-sdk-{sys.implementation.cache_tag}-{sys.platform}-{machine}"
    )


@dataclass(frozen=True)
class Plan:
    """How the SDK is installed: `where` ("own": Chief of Staff's own environment, "side": its
    own folder), with what (`tool`: "uv", "pip", "" for nothing found), the command, and for
    "side" the folder it is installed into first (`staging`) and the constraints it is held
    to."""

    where: str
    tool: str
    argv: tuple[str, ...]
    staging: Path | None = None
    constraints: Path | None = None


def plan(wanted: str, side: Path, *, own: bool, uv: str, pip: bool) -> Plan:
    python = sys.executable
    quiet = ("--disable-pip-version-check", "--no-input")
    if own:
        if uv:
            return Plan("own", "uv", (uv, "pip", "install", "--python", python, wanted))
        if pip:
            return Plan("own", "pip", (python, "-m", "pip", "install", *quiet, wanted))
        return Plan("own", "", ())
    staging = side.with_name(side.name + ".new")
    constraints = side.with_name(side.name + ".constraints.txt")
    held = ("--target", str(staging), "--constraint", str(constraints), wanted)
    if uv:
        argv: tuple[str, ...] = (uv, "pip", "install", "--python", python, *held)
        return Plan("side", "uv", argv, staging, constraints)
    if pip:
        return Plan(
            "side", "pip", (python, "-m", "pip", "install", *quiet, *held), staging, constraints
        )
    return Plan("side", "", (), staging, constraints)


def own_packages(side: Path) -> list[str]:
    """Chief of Staff's own packages, `name==version` (its folder for the SDK left out): what the
    SDK installed beside them is held to."""
    paths = [entry for entry in sys.path if Path(entry or ".").resolve() != side.resolve()]
    found: dict[str, str] = {}
    for dist in metadata.distributions(path=paths):
        name = re.sub(r"[-_.]+", "-", str(dist.metadata["Name"] or "")).lower()
        if name and name != SDK and re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            found.setdefault(name, dist.version)
    return [f"{name}=={version}" for name, version in sorted(found.items())]


def prepare(planned: Plan, side: Path):
    """Its own folder's install, made ready: a fresh folder to install into, and the
    constraints that hold the SDK's requirements to Chief of Staff's own packages."""
    if planned.staging is None or planned.constraints is None:
        return
    planned.staging.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.rmtree(planned.staging, ignore_errors=True)
    planned.constraints.write_text("\n".join(own_packages(side)) + "\n")


def tidy(planned: Plan):
    """What its own folder's install left beside it: gone (a failed one's folder too)."""
    if planned.staging:
        shutil.rmtree(planned.staging, ignore_errors=True)
    if planned.constraints:
        planned.constraints.unlink(missing_ok=True)


def swap(staging: Path, side: Path):
    """The new install in its own folder's place (the one before removed)."""
    old = side.with_name(side.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if side.exists():
        side.rename(old)
    staging.rename(side)
    shutil.rmtree(old, ignore_errors=True)


def use_folder(side: Path) -> bool:
    """Its own folder, last on sys.path (once): what it holds is found from now on, after
    Chief of Staff's own packages."""
    if not side.is_dir():
        return False
    if str(side) not in sys.path:
        sys.path.append(str(side))
    importlib.invalidate_caches()
    return True


def manual_command(how: str = "") -> str:
    """The command that installs the extra by hand, as Chief of Staff was installed: under
    Details, a last resort."""
    how = how or updates.launch_now().how
    return {
        "tool": "uv tool install 'chiefofstaff[claude]'",
        "uvx": "uvx 'chiefofstaff[claude]'",
        "pipx": "pipx install --force 'chiefofstaff[claude]'",
        "source": "uv sync --extra claude",
    }.get(how, f"{shlex.quote(sys.executable)} -m pip install 'chiefofstaff[claude]'")


def redacted(line: str) -> str:
    return CREDENTIALS.sub(r"\1•••@", line)[:LINE_CHARS]


def reason_of(output: str) -> str:
    """Why an install failed, in a few words (its last line where nothing known says it)."""
    for pattern, words in REASONS:
        if pattern.search(output):
            return words
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return (
        f"the installer failed ({redacted(lines[-1])[:120]})" if lines else "the installer failed"
    )


async def run_command(argv: list[str], seconds: float) -> tuple[int | None, str]:
    """An install's command, run (its exit code, None once it took too long, and its output).
    Never while Chief of Staff's own tests run: they give a stand-in of their own."""
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        raise RuntimeError("not while Chief of Staff's own tests run")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("CHIEFOFSTAFF_")}
    environment["NO_COLOR"] = "1"
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=environment,
        start_new_session=sys.platform != "win32",
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), seconds)
    except TimeoutError:
        kill_process_tree(process.pid)
        await process.wait()
        return None, ""
    except asyncio.CancelledError:
        kill_process_tree(process.pid)
        raise
    return process.returncode, output.decode(errors="replace")


def kept_through_upgrade() -> str:
    """What a `uv tool upgrade` of Chief of Staff must put back (it removes what its tool's
    own record does not name): the extra's requirement, where the SDK is in its own
    environment now; "" where it is not."""
    with contextlib.suppress(metadata.PackageNotFoundError):
        found = metadata.distribution(SDK)
        place = Path(str(found.locate_file(""))).resolve()
        if place.is_relative_to(Path(sys.prefix).resolve()):
            return requirement()
    return ""


class ClaudeSetup:
    """Claude's Agent SDK set up by Chief of Staff, one install at a time: what Claude's status
    says of it (status), what a Claude worker's start waits for (wait), and when it runs
    (at_start, turned_on, retry)."""

    def __init__(self, store: Store, in_use: Callable[[], bool]):
        self.store = store
        self.in_use = in_use
        # Told as an install starts (None) and ends (whether it set Claude up).
        self.changed: Callable[[bool | None], None] | None = None
        self.runner: Runner = run_command
        self.task: asyncio.Task | None = None
        # How this process's last install ended ({"ok", "reason"}), and the end of its output.
        self.outcome: dict[str, Any] = {}
        self.tail: list[str] = []
        # What sdk_refused said last, when, and the automatic try it stands for (key).
        self.checked: tuple[float, str, str] = (float("-inf"), "", "")

    @property
    def side(self) -> Path:
        return side_folder(self.store.directory)

    def refused(self, fresh: bool = False) -> str:
        """Why the SDK installed can't run Claude workers ("" when it can), as read lately."""
        at, why, _ = self.checked
        if fresh or time.monotonic() - at > CHECK_SECONDS:
            from .backend_claude import sdk_refused

            why = sdk_refused()
            self.checked = (time.monotonic(), why, self.key() if why else "")
        return why

    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def key(self) -> str:
        """What an automatic try is kept by: this version, why (missing, or which version is
        there), and where it installs (Chief of Staff's own environment, as this install of it:
        installed again without the SDK, it is another)."""
        from .backend_claude import sdk_version

        found = sdk_version()
        why = f"untested {found}" if found else "missing"
        place = f"{sys.prefix}|{made()}" if own_environment() else str(self.side)
        said = f"{updates.installed() or 'dev'}|{why}|{place}"
        return TRIED + hashlib.sha256(said.encode()).hexdigest()[:16]

    def record(self) -> dict[str, Any]:
        """The automatic try for the SDK as it is now (none while it can run Claude)."""
        if not self.refused():
            return {}
        try:
            found = json.loads(self.store.get(self.checked[2]) or "{}")
        except ValueError:
            return {}
        return found if isinstance(found, dict) else {}

    def load(self):
        """At start: the SDK set up before in Chief of Staff's own folder, found as then."""
        if sys.platform != "win32" and not own_environment():
            use_folder(self.side)

    def at_start(self) -> bool:
        """At start, while Claude is in use and the SDK can't run it: set up, once per version,
        why and place (one cut short is tried once more). Whether it runs."""
        if sys.platform == "win32" or not self.in_use():
            return False
        if self.running():
            return True
        if not self.refused(fresh=True):
            return False
        record = self.record()
        if record and not (record.get("ok") is None and int(record.get("tries") or 0) < 2):
            return False
        self.start(int(record.get("tries") or 0))
        return True

    def turned_on(self) -> bool:
        """The owner turned Claude on: set up now if the SDK can't run it."""
        if sys.platform == "win32" or self.running() or not self.refused(fresh=True):
            return self.running()
        self.start(int(self.record().get("tries") or 0))
        return True

    def retry(self) -> bool:
        """Retry (or Set up): again, whatever was tried before."""
        if sys.platform == "win32":
            raise ValueError("Claude workers don't run on Windows yet.")
        return self.turned_on()

    def start(self, tries: int = 0):
        key = self.key()
        record = {"ok": None, "tries": tries + 1, "at": time.time()}
        self.store.set(key, json.dumps(record))
        self.tail, self.outcome = [], {}
        self.task = asyncio.ensure_future(self.run(key, record))
        if self.changed:
            self.changed(None)

    async def run(self, key: str, record: dict):
        ok, reason, said = False, "", []
        planned: Plan | None = None
        try:
            wanted = requirement()
            own = own_environment()
            planned = plan(wanted, self.side, own=own, uv=find_uv(), pip=has_pip())
            if not planned.argv:
                reason = NO_INSTALLER
            else:
                if planned.staging:
                    await asyncio.to_thread(prepare, planned, self.side)
                logger.info("Setting up Claude: %s", shlex.join(planned.argv))
                code, output = await self.runner(list(planned.argv), SETUP_SECONDS)
                said = [redacted(line) for line in output.splitlines() if line.strip()][-80:]
                self.tail = said[-TAIL_LINES:]
                if code is None:
                    reason = TOO_LONG
                elif code:
                    reason = reason_of(output)
                else:
                    if planned.staging:
                        await asyncio.to_thread(swap, planned.staging, self.side)
                        use_folder(self.side)
                    importlib.invalidate_caches()
                    if why := self.refused(fresh=True):
                        logger.warning("Claude's Agent SDK, installed, still can't run: %s", why)
                        reason = STILL_REFUSED
                    else:
                        ok = True
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            reason = f"the installer could not start ({exc.strerror or exc})"
        except Exception as exc:
            logger.warning("Setting up Claude failed.", exc_info=True)
            reason = str(exc) or type(exc).__name__
        if planned and planned.staging:
            await asyncio.to_thread(tidy, planned)
        self.outcome = {"ok": ok, "reason": reason}
        self.store.set(key, json.dumps(record | self.outcome))
        self.refused(fresh=True)
        # What the install printed, in the log: on a failure where it is seen (a warning).
        if ok:
            logger.info("Claude is set up. The installer said:\n%s", "\n".join(said))
        else:
            logger.warning("Could not set up Claude: %s. The installer said:\n%s", reason,
                           "\n".join(said) or "(nothing)")  # fmt: skip
        if self.changed:
            self.changed(ok)

    async def stop(self):
        if self.running() and self.task:
            self.task.cancel()
            with contextlib.suppress(BaseException):
                await self.task

    async def wait(self, seconds: float = WAIT_SECONDS) -> str:
        """Before a Claude worker's process starts: a setup under way waited for, `seconds` at
        most. Why Claude can't run yet for it ("" when it can, or when the SDK's own words say
        it: backend_claude.sdk_refused)."""
        if sys.platform == "win32":
            return ""
        if self.running() and self.task:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self.task), seconds)
            if self.running():
                return BEING_SET_UP
        if not self.refused(fresh=True):
            return ""
        ended = self.outcome or self.record()
        if ended.get("ok") is False:
            return f"{FAILED}: {ended.get('reason') or 'the installer failed'}"
        return ""

    def status(self) -> dict[str, Any] | None:
        """Claude's status while the SDK is what holds it (None when it is not): being set up;
        its setup failed (why, Retry; the command under Details); not set up while in use
        (Set up), or while off (it is set up once turned on)."""
        if sys.platform == "win32":
            return None
        if self.running():
            return {"status": agents.SETTING_UP, "said": SETTING_UP, "setup": {"state": "running"}}
        if not self.refused():
            return None
        ended = self.outcome or self.record()
        if ended.get("ok") is False:
            reason = ended.get("reason") or "the installer failed"
            return {
                "status": agents.PROBLEM,
                "said": f"{FAILED}: {reason}",
                "setup": {
                    "state": "failed",
                    "reason": reason,
                    "command": manual_command(),
                    **({"output": list(self.tail)} if self.tail else {}),
                },
            }
        if self.in_use():
            return {
                "status": agents.PROBLEM,
                "said": NEEDED,
                "setup": {"state": "needed", "command": manual_command()},
            }
        return {"status": agents.NOT_SET_UP, "said": NOT_SET_UP, "setup": {"state": "off"}}
