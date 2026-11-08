"""Checks learned from mistakes: engineering memory that can be run, not only read.

When a worker's test run fails and a later run of that test passes after it changed the code,
both versions are kept as a failure case (git commits under a hidden ref; the worker's checkout
is left as it was). A checks worker reads the cases and proposes, for a mistake that can come
back, a shell command that fails when the mistake is there. Chief of Staff runs it on both
versions first: only a check that fails on the failing code and passes on the fixed code is
shown to the owner, with that evidence. Checks the owner keeps run whenever a worker reports work
done on files they apply to; one that fails sends the worker back once.

Checks run in a sandbox: no network, nothing written outside a scratch folder, nothing read in
the home folder but the repository. Everything stays on this machine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import signal
import sys
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel, Field

from . import prompts
from .models import NestedArguments
from .receipts import test_runs, words
from .roles import WORKER
from .store import now
from .workspaces import PLAIN_DIFF, base_of, command, git, locate

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

CHECK_SECONDS = 60
MAX_CASES = 6  # Per checks worker.
PROPOSE_EVERY = 3600
MAX_TRIES = 3  # Proposals per case that fail on the versions.
MAX_SENT_BACK = 1  # Done reports sent back because a check failed.
# A fix this big is new work rather than the fix of a mistake.
MAX_FIX_LINES = 400
MAX_CHANGED_ENTRIES = 2000
MAX_DIFF = 8000
OUTPUT_CHARS = 600
IDENTITY = {
    "GIT_AUTHOR_NAME": "Chief of Staff",
    "GIT_AUTHOR_EMAIL": "chiefofstaff@localhost",
    "GIT_COMMITTER_NAME": "Chief of Staff",
    "GIT_COMMITTER_EMAIL": "chiefofstaff@localhost",
}
REMOTE = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^/:]+)[:/](.+?)(?:\.git)?/?$", re.IGNORECASE)


class CheckProposal(BaseModel):
    case: str = Field(description="The case id (c…) the check comes from")
    name: str = Field(description="Short name: what mistake it catches")
    why: str = Field(description="One sentence: the mistake, and why it matters")
    applies_to: list[str] = Field(
        default_factory=list,
        description="Globs of the files where the mistake can be made (src/**/*.py); empty: any",
    )
    command: str = Field(
        description="A shell command run at the repository's top: exits non-zero when the "
        "mistake is there, 0 when it is not"
    )


class ProposeChecks(NestedArguments):
    checks: list[CheckProposal] = Field(
        default_factory=list,
        description="Checks for the cases that show a mistake that can come back; none for "
        "cases that show ordinary progress",
    )


def sandbox_kind() -> str:
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "macos"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bwrap"
    return ""


def quoted(path: Path | str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_profile(scratch: Path, readable: list[Path]) -> str:
    """Nothing but what a check needs: run programs, read the system and the repository (not the
    rest of the home folder), write to its scratch folder (and the shell's here-documents). No
    network, clipboard, other apps or system services."""
    home = Path.home().resolve()
    reads = " ".join(f"(subpath {quoted(path)})" for path in [scratch, *readable])
    return f"""(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read)
(allow file-read*)
(deny file-read-data (subpath {quoted(home)}))
(allow file-read-data (literal {quoted(home)}) {reads})
(allow file-write* (subpath {quoted(scratch)}))
(allow file-write* (regex #"^/private/(var/)?tmp/sh-thd-"))
(allow file-write-data (literal "/private/tmp") (literal "/private/var/tmp"))
(allow file-write-data (literal "/dev/null") (regex #"^/dev/fd/[0-9]+$") (literal "/dev/stdout")
  (literal "/dev/stderr"))
(allow file-ioctl (literal "/dev/null"))
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
(allow ipc-posix-sem)
"""


def wrapper(kind: str, scratch: Path, readable: list[Path], cwd: Path) -> list[str]:
    if kind == "macos":
        return ["sandbox-exec", "-p", macos_profile(scratch, readable)]
    home = str(Path.home().resolve())
    binds = []
    for path in [scratch, *readable]:
        binds += ["--ro-bind" if path != scratch else "--bind", str(path), str(path)]
    # No network, no other processes, and none of the sockets in /run (the session bus).
    return [
        "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
        "--tmpfs", "/tmp",  # noqa: S108  (a fresh, empty /tmp inside the sandbox)
        "--tmpfs", "/run", "--tmpfs", home, *binds, "--unshare-net", "--unshare-ipc",
        "--unshare-pid", "--new-session", "--die-with-parent", "--chdir", str(cwd),
    ]  # fmt: skip


def prepare(root: Path, cwd: Path, readable: list[Path]) -> tuple[Path, Path, list[Path]]:
    """A fresh scratch folder, and every path resolved (the sandbox sees real paths)."""
    scratch = (root / uuid4().hex[:8]).resolve()
    scratch.mkdir(parents=True, mode=0o700)
    return scratch, cwd.resolve(), [path.resolve() for path in readable]


def copy_index(index: str, to: Path):
    if os.path.exists(index):  # noqa: PTH110  (False, not an error, when it cannot look)
        shutil.copyfile(index, to)


async def run_sandboxed(
    text: str, cwd: Path, readable: list[Path], scratch_root: Path, limit: float = CHECK_SECONDS
) -> tuple[int | None, str]:
    """Run a check: its exit code (None when it timed out or could not run) and its output."""
    kind = sandbox_kind()
    if not kind:
        return None, "No sandbox on this machine."
    scratch, cwd, readable = await asyncio.to_thread(prepare, scratch_root, cwd, readable)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        process = await asyncio.create_subprocess_exec(
            *wrapper(kind, scratch, readable, cwd),
            "/bin/sh",
            "-c",
            text,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(limit):
                output, _ = await process.communicate()
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            return None, f"Timed out after {int(limit)} s."
        return process.returncode, output.decode(errors="replace")[-OUTPUT_CHARS:]
    except OSError as exc:
        return None, str(exc)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def related(first: str, second: str) -> bool:
    """The same test run, or one a part of the other (one test file, then the whole suite),
    whatever else the command lines do (`| tail`, `; git status`)."""
    for one in test_runs(first):
        for other in test_runs(second):
            a, b = set(words(one)), set(words(other))
            if a and b and (a <= b or b <= a):
                return True
    return False


def glob_pattern(glob: str) -> re.Pattern:
    """A path glob as git and editors read it: `**/` is any folders (none too), `*` and `?`
    stay within one folder; a glob without a slash matches a file name in any folder."""
    glob = glob.strip().lstrip("/")
    if "/" not in glob:
        glob = "**/" + glob
    parts, at = [], 0
    while at < len(glob):
        if glob.startswith("**/", at):
            parts.append("(?:.*/)?")
            at += 3
        elif glob.startswith("**", at):
            parts.append(".*")
            at += 2
        elif glob[at] == "*":
            parts.append("[^/]*")
            at += 1
        elif glob[at] == "?":
            parts.append("[^/]")
            at += 1
        else:
            parts.append(re.escape(glob[at]))
            at += 1
    return re.compile("".join(parts) + r"\Z")


def applies(globs: list[str], files: list[str]) -> bool:
    if not globs:
        return bool(files)
    patterns = [glob_pattern(glob) for glob in globs if glob.strip()]
    return any(pattern.match(name) for name in files for pattern in patterns)


def identity(remote: str, git_dir: str) -> str:
    """Which repository a checkout is of, whatever folder it is in: its origin, else its git
    folder (worktrees of one repository share it)."""
    if found := REMOTE.match(remote.strip()):
        return f"{found.group(1).lower()}/{found.group(2).lower()}"
    return str(Path(git_dir).resolve())


class Checks:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.locks: dict[str, asyncio.Lock] = {}
        self.propose_lock = asyncio.Lock()

    def scratch(self) -> Path:
        path = self.store.directory / "checks"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def enabled(self) -> bool:
        return self.store.settings().learn_checks and bool(sandbox_kind())

    # Failure cases

    def on_command(self, session_id: str, call_id: str):
        """A worker's command ended: a failing test run is kept, and a passing one after it
        makes a failure case."""
        if not self.enabled():
            return
        row = self.store.find_session(session_id)
        if not row or row["role"] != WORKER or row["slack"] or self.runtime.special(row):
            return
        ran = self.store.one(
            "SELECT * FROM commands WHERE session_id=? AND call_id=? ORDER BY id DESC",
            (session_id, call_id),
        )
        if ran and ran["kind"] == "test" and ran["exit_code"] is not None:
            self.runtime.background(self.observe(row, ran))

    async def where(self, row: dict, text: str) -> tuple[str, str, str] | None:
        """The checkout a command ran in: (top, its shared git folder, repository identity)."""
        paths = [c["path"] for c in self.runtime.workspaces.checkouts(row)] or [row["cwd"]]
        chosen = next(
            (path for path in paths if path and (path in text or f"cd {Path(path).name}" in text)),
            paths[0],
        )
        return await self.repository(chosen) if chosen else None

    @staticmethod
    async def repository(path: str) -> tuple[str, str, str] | None:
        try:
            top = (await git("-C", path, "rev-parse", "--show-toplevel")).strip()
            git_dir = (
                await git("-C", top, "rev-parse", "--path-format=absolute", "--git-common-dir")
            ).strip()
        except ValueError:
            return None
        remote = ""
        with suppress(ValueError):
            remote = await git("-C", top, "remote", "get-url", "origin")
        return top, git_dir, identity(remote, git_dir)

    async def snapshot(self, top: str) -> str:
        """A commit of the checkout as it is, uncommitted edits and new files included, made
        with a copy of its index: the checkout, its index and its branch are not touched."""
        status = await git("-C", top, "status", "--porcelain")
        if len(status.splitlines()) > MAX_CHANGED_ENTRIES:
            raise ValueError("Too many changed files to keep.")
        index = (
            await git("-C", top, "rev-parse", "--path-format=absolute", "--git-path", "index")
        ).strip()
        temp = self.scratch() / f"index-{uuid4().hex[:8]}"
        try:
            await asyncio.to_thread(copy_index, index, temp)
            env = {"GIT_INDEX_FILE": str(temp), **IDENTITY}
            await command("git", "-C", top, "add", "-A", env=env)
            tree = (await command("git", "-C", top, "write-tree", env=env)).strip()
            parent: list[str] = []
            with suppress(ValueError):
                parent = ["-p", (await git("-C", top, "rev-parse", "--verify", "HEAD")).strip()]
            message = ["-m", "Chief of Staff: a worker's checkout, kept for a check"]
            return (
                await command("git", "-C", top, "commit-tree", tree, *parent, *message, env=env)
            ).strip()
        finally:
            temp.unlink(missing_ok=True)

    async def observe(self, row: dict, ran: dict):
        lock = self.locks.setdefault(row["id"], asyncio.Lock())
        async with lock:
            try:
                await self.observe_locked(row, ran)
            except ValueError as exc:
                logger.info("Could not keep a failure case for %s: %s", row["id"], exc)

    async def observe_locked(self, row: dict, ran: dict):
        found = await self.where(row, ran["command"])
        if not found:
            return
        top, git_dir, repo = found
        key = f"failing:{row['id']}"
        if ran["exit_code"] != 0:
            # The latest failing version: what the fix that follows changes is the fix.
            failing = {
                "top": top,
                "sha": await self.snapshot(top),
                "command": ran["command"],
                "output": ran["output"],
            }
            self.store.set(key, json.dumps(failing))
            return
        failing = json.loads(self.store.get(key) or "null")
        if not failing or failing["top"] != top or not related(failing["command"], ran["command"]):
            return
        self.store.delete(key)
        fixed = await self.snapshot(top)
        stat = await git("-C", top, "diff", "--numstat", failing["sha"], fixed)
        lines = sum(
            int(part)
            for line in stat.splitlines()
            for part in line.split("\t")[:2]
            if part.isdigit()
        )
        if not lines or lines > MAX_FIX_LINES:
            return  # The same code passed (a flaky test), or new work rather than a fix.
        task = self.runtime.tasks.current(row["id"])
        case_id = "c" + uuid4().hex[:7]
        self.store.execute(
            """INSERT INTO failure_cases(id, session_id, task_id, repo, git_dir, top, command,
            output, failing, fixed, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (case_id, row["id"], task["id"] if task else "", repo, git_dir, top,
             failing["command"][:1000], failing["output"], failing["sha"], fixed, "new", now()),
        )  # fmt: skip
        for name, sha in (("failing", failing["sha"]), ("fixed", fixed)):
            # Kept from git's garbage collection while the case is used.
            await git(
                "--git-dir", git_dir, "update-ref", f"refs/chiefofstaff/{case_id}/{name}", sha
            )
        self.runtime.background(self.maybe_propose())

    async def forget(self, case: dict):
        for name in ("failing", "fixed"):
            with suppress(ValueError):
                ref = f"refs/chiefofstaff/{case['id']}/{name}"
                await git("--git-dir", case["git_dir"], "update-ref", "-d", ref)

    # The checks worker

    def proposers(self) -> list[dict]:
        return [row for row in self.store.open_sessions() if self.store.get(f"checker:{row['id']}")]

    async def maybe_propose(self):
        """Start a checks worker for new cases (at most hourly, one at a time)."""
        if not self.enabled() or not self.runtime.ready():
            return
        async with self.propose_lock:
            if any(row["status"] in {"queued", "starting", "working"} for row in self.proposers()):
                return
            last = float(self.store.get("checks_proposed_at") or 0)
            if time.time() - last < PROPOSE_EVERY:
                return
            cases = self.store.all(
                "SELECT * FROM failure_cases WHERE status='new' ORDER BY created_at LIMIT ?",
                (MAX_CASES,),
            )
            if not cases:
                return
            for row in self.proposers():  # One that stopped without proposing.
                await self.runtime.workers.close(row["id"])
            self.store.set("checks_proposed_at", str(time.time()))
            lines = [prompts.CHECKS]
            for case in cases:
                lines.append(await self.describe(case))
                self.store.execute(
                    "UPDATE failure_cases SET status='given' WHERE id=?", (case["id"],)
                )
            await self.runtime.toolset.start_special(
                "checker", "Learn checks from mistakes", "\n\n".join(lines)
            )

    async def describe(self, case: dict) -> str:
        task = self.store.one("SELECT title FROM tasks WHERE id=?", (case["task_id"],)) or {}
        try:
            change = await git(
                "--git-dir", case["git_dir"], *PLAIN_DIFF, case["failing"], case["fixed"]
            )
        except ValueError:
            change = "(not available)"
        existing = self.store.all(
            "SELECT name, command FROM checks WHERE repo=? AND status IN ('active','proposed')",
            (case["repo"],),
        )
        known = "\n".join(f"- {row['name']}: {row['command']}" for row in existing) or "none"
        return (
            f"<case id={case['id']} repository={json.dumps(case['repo'])} "
            f"task={json.dumps(task.get('title', ''))}>\n"
            f"Test command: {case['command']}\n"
            f"It failed, ending with:\n{case['output'][-OUTPUT_CHARS:]}\n"
            f"The fix (diff from the failing code to the fixed code):\n"
            f"{change[:MAX_DIFF]}{' […]' if len(change) > MAX_DIFF else ''}\n"
            f"Checks this repository has already:\n{known}\n</case>"
        )

    @asynccontextmanager
    async def checked_out(self, git_dir: str, *commits: str):
        """Commits checked out in scratch worktrees: what checks run on, as a clean checkout
        (new files included, nothing ignored or built), whatever the worker's folder holds."""
        root = self.scratch() / f"versions-{uuid4().hex[:8]}"
        made = []
        try:
            for index, commit in enumerate(commits):
                path = root / str(index)
                # Without the repository's hooks (a post-checkout hook would run outside the
                # sandbox) and without fetching large files.
                await command(
                    "git", "-c", "core.hooksPath=/dev/null", "--git-dir", git_dir, "worktree",
                    "add", "--detach", "--quiet", str(path), commit,
                    limit=120, env={"GIT_LFS_SKIP_SMUDGE": "1"},
                )  # fmt: skip
                made.append(path)
            yield made
        finally:
            for path in made:
                with suppress(ValueError):
                    await git("--git-dir", git_dir, "worktree", "remove", "--force", str(path))
            shutil.rmtree(root, ignore_errors=True)

    async def validate(self, case: dict, text: str) -> tuple[str, dict]:
        """Run a proposed check on both versions: "" and the evidence when it catches the
        mistake (fails on the failing code, passes on the fixed code), else what is wrong."""
        versions = self.checked_out(case["git_dir"], case["failing"], case["fixed"])
        async with versions as (failing_dir, fixed_dir):
            readable = [Path(case["git_dir"])]
            failing = await run_sandboxed(
                text, failing_dir, [failing_dir, *readable], self.scratch()
            )
            fixed = await run_sandboxed(text, fixed_dir, [fixed_dir, *readable], self.scratch())
        evidence = {
            "failing": {"exit": failing[0], "output": failing[1]},
            "fixed": {"exit": fixed[0], "output": fixed[1]},
        }
        if failing[0] is None or fixed[0] is None:
            return "It did not finish: " + (
                failing[1] if failing[0] is None else fixed[1]
            ), evidence
        if failing[0] in {126, 127}:
            return "A command in it was not found (exit 127).", evidence
        if failing[0] == 0:
            return "It passes on the failing code: it does not catch the mistake.", evidence
        if fixed[0] != 0:
            return f"It fails on the fixed code too (exit {fixed[0]}).", evidence
        return "", evidence

    async def t_propose_checks(self, params: ProposeChecks, caller: str):
        if not self.store.get(f"checker:{caller}"):
            raise ValueError("Only the checks worker proposes checks.")
        given = {
            case["id"]: case
            for case in self.store.all("SELECT * FROM failure_cases WHERE status='given'")
        }
        mentioned, accepted, problems = set(), [], []
        for item in params.checks[: MAX_CASES * 2]:
            case = given.get(item.case)
            if not case:
                problems.append(f"{item.case}: not a case you were given, or already settled.")
                continue
            mentioned.add(case["id"])
            text = item.command.strip()
            if not text or len(text) > 1000:
                problems.append(f"{case['id']}: the command must be 1-1000 characters.")
                continue
            try:
                problem, evidence = await self.validate(case, text)
            except ValueError as exc:  # The versions could not be checked out.
                await self.settle(case, "failed")
                problems.append(f"{case['id']}: could not be checked: {exc}")
                continue
            tries = case["tries"] + 1
            self.store.execute("UPDATE failure_cases SET tries=? WHERE id=?", (tries, case["id"]))
            case["tries"] = tries
            if problem:
                if tries >= MAX_TRIES:
                    await self.settle(case, "failed")
                    problem += " That was the last try for this case."
                problems.append(
                    f"{case['id']} ({item.name}): {problem}\nOn the failing code: exit "
                    f"{evidence['failing']['exit']}, output: {evidence['failing']['output'][-300:]}"
                    f"\nOn the fixed code: exit {evidence['fixed']['exit']}, output: "
                    f"{evidence['fixed']['output'][-300:]}"
                )
                continue
            accepted.append(self.add_check(case, item, text, evidence))
            self.store.execute(
                "UPDATE failure_cases SET status='checked' WHERE id=?", (case["id"],)
            )
        for case_id, case in given.items():
            if case_id not in mentioned:
                await self.settle(case, "no_check")  # Not a mistake that can come back.
        if accepted:
            self.runtime.alert(
                f"{len(accepted)} check(s) learned from workers' mistakes. Look before they "
                "are used.",
                session_id=self.runtime.router_id(),
                needs_action=True,
                title="Checks to review",
            )
        left = self.store.value("SELECT count(*) FROM failure_cases WHERE status='given'")
        if problems and left:
            return (
                f"{len(accepted)} check(s) caught their mistake and go to the owner. These did "
                "not; fix them and call propose_checks again with only those (or leave a case "
                "out when no check can catch it):\n" + "\n\n".join(problems)
            )
        for case in self.store.all("SELECT * FROM failure_cases WHERE status='given'"):
            await self.settle(case, "no_check")
        task = self.runtime.tasks.current(caller)
        if task and task["status"] not in {"done", "cancelled"}:
            self.runtime.tasks.move(
                task["id"], "done", result=f"Proposed {len(accepted)} check(s)."
            )
        self.runtime.states[caller].close_after_turn = True
        return f"{len(accepted)} check(s) go to the owner. You are done."

    async def stopped(self):
        """The checks worker ended without another proposal: its open cases get no check."""
        for case in self.store.all("SELECT * FROM failure_cases WHERE status='given'"):
            await self.settle(case, "no_check")

    async def settle(self, case: dict, status: str):
        self.store.execute("UPDATE failure_cases SET status=? WHERE id=?", (status, case["id"]))
        await self.forget(case)

    def add_check(self, case: dict, item: CheckProposal, text: str, evidence: dict) -> str:
        check_id = "k" + uuid4().hex[:7]
        globs = [glob.strip() for glob in item.applies_to if glob.strip()][:10]
        self.store.execute(
            """INSERT INTO checks(id, case_id, repo, name, why, applies_to, command, evidence,
            status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (check_id, case["id"], case["repo"], " ".join(item.name.split())[:120],
             " ".join(item.why.split())[:400], json.dumps(globs), text, json.dumps(evidence),
             "proposed", now(), now()),
        )  # fmt: skip
        task = self.store.one("SELECT title FROM tasks WHERE id=?", (case["task_id"],)) or {}
        card = self.store.create_draft(
            self.runtime.router_id(),
            "check",
            {
                "check": check_id,
                "name": " ".join(item.name.split())[:120],
                "why": " ".join(item.why.split())[:400],
                "command": text,
                "applies_to": globs,
                "repo": case["repo"].rsplit("/", 1)[-1],
                "task": task.get("title", ""),
                **evidence,
            },
            f"Check learned from a mistake: {item.name[:120]}",
        )
        self.runtime.bus.publish("draft", card)
        return check_id

    async def review(self, card: dict, decision) -> dict:
        """The owner keeps the check (it runs from now on) or drops it."""
        if decision.action not in {"answer", "reject"}:
            raise ValueError("Use the check, or drop it.")
        check = self.store.one("SELECT * FROM checks WHERE id=?", (card["target"]["check"],))
        status = "active" if decision.action == "answer" else "rejected"
        if check:
            self.store.execute(
                "UPDATE checks SET status=?, updated_at=? WHERE id=?", (status, now(), check["id"])
            )
            if status == "rejected":
                case = self.store.one("SELECT * FROM failure_cases WHERE id=?", (check["case_id"],))
                if case:
                    await self.forget(case)
        outcome = "In use." if status == "active" else "Dropped."
        card = self.store.update_draft(
            card["id"], status="answered", outcome=outcome, resolved_at=now()
        )
        self.runtime.bus.publish("draft", card)
        self.runtime.bus.publish("state")
        return card

    def turn(self, check_id: str, on: bool):
        found = self.store.one("SELECT status FROM checks WHERE id=?", (check_id,))
        if not found or found["status"] not in {"active", "disabled"}:
            raise LookupError("No such check in use.")
        self.store.execute(
            "UPDATE checks SET status=?, updated_at=? WHERE id=?",
            ("active" if on else "disabled", now(), check_id),
        )
        self.runtime.bus.publish("state")

    # Using them

    async def changed_files(self, checkout: dict, since: str = "") -> list[str]:
        """What a worker changed in a checkout: since where it started, uncommitted edits and
        new files, and (in a shared folder, where it started is unknown) what it committed
        since its task began."""
        path, _head, live = await asyncio.to_thread(locate, checkout)
        if not live:
            return []
        names = []
        for base in dict.fromkeys([base_of(checkout) or "HEAD", "HEAD"]):
            try:  # Where the worker started; a folder with no origin: its uncommitted edits.
                names += (await git("-C", path, "diff", "--name-only", base)).split("\n")
                break
            except ValueError:
                continue
        with suppress(ValueError):
            names += (await git("-C", path, "ls-files", "--others", "--exclude-standard")).split(
                "\n"
            )
        if since:
            # git reads whole seconds: a commit in the second the task began came before it.
            start = math.floor(datetime.fromisoformat(since).timestamp()) + 1
            with suppress(ValueError):
                log = await git("-C", path, "log", f"--since=@{start}", "--name-only", "--format=")
                names += log.split("\n")
        return [name for name in dict.fromkeys(names) if name.strip()]

    async def before_done(self, row: dict, task: dict) -> str:
        """Run the checks that apply to what a worker changed; what to tell it when one
        fails ("" when none does, or it was sent back already)."""
        if not self.enabled() or row["slack"] or self.runtime.special(row):
            return ""
        key = f"checked:{task['id']}"
        sent_back = int(self.store.get(key) or 0)
        failures = []
        for checkout in self.runtime.workspaces.checkouts(row):
            found = await self.repository(checkout["path"])
            if not found:
                continue
            top, git_dir, repo = found
            active = self.store.all(
                "SELECT * FROM checks WHERE repo=? AND status='active'", (repo,)
            )
            if not active:
                continue
            changed = await self.changed_files(checkout, task["created_at"])
            due = [check for check in active if applies(json.loads(check["applies_to"]), changed)]
            if not due:
                continue
            try:
                now_there = await self.snapshot(top)
                async with self.checked_out(git_dir, now_there) as (tree,):
                    readable = [tree, Path(git_dir)]
                    results = await asyncio.gather(
                        *(
                            run_sandboxed(check["command"], tree, readable, self.scratch())
                            for check in due
                        )
                    )
            except ValueError as exc:
                logger.info("Could not run checks for %s: %s", row["id"], exc)
                continue
            for check, (code, output) in zip(due, results, strict=True):
                caught = code is not None and code != 0
                self.store.execute(
                    "UPDATE checks SET runs=runs+1, caught=caught+? WHERE id=?",
                    (int(caught), check["id"]),
                )
                if caught:
                    failures.append(
                        f"- {check['name']} ({Path(top).name}): {check['why']}\n"
                        f"  `{check['command']}` exited {code}: {output[-300:].strip()}"
                    )
        if not failures:
            self.store.delete(f"checkflags:{task['id']}")
            return ""
        if sent_back >= MAX_SENT_BACK:
            # It answered once already: the owner sees the failing check on the review card.
            self.store.set(f"checkflags:{task['id']}", json.dumps(failures))
            return ""
        self.store.set(key, str(sent_back + 1))
        return (
            "Not reported yet: a check learned from an earlier mistake fails on your changes:\n"
            + "\n".join(failures)
            + "\nFix it and report again. If it does not apply here, report again and say why."
        )

    def flags(self, task_id: str) -> list[str]:
        failures = json.loads(self.store.get(f"checkflags:{task_id}") or "[]")
        return [
            f"A check learned from a mistake fails: {item.splitlines()[0][2:]}" for item in failures
        ]

    # The owner's view

    def view(self) -> dict:
        checks = self.store.all(
            "SELECT * FROM checks WHERE status IN ('active','disabled') ORDER BY created_at"
        )
        return {
            "enabled": self.store.settings().learn_checks,
            "sandbox": bool(sandbox_kind()),
            "cases": int(self.store.value("SELECT count(*) FROM failure_cases")),
            "checks": [
                {
                    "id": check["id"],
                    "name": check["name"],
                    "why": check["why"],
                    "command": check["command"],
                    "repo": check["repo"].rsplit("/", 1)[-1],
                    "applies_to": json.loads(check["applies_to"]),
                    "status": check["status"],
                    "runs": check["runs"],
                    "caught": check["caught"],
                }
                for check in checks
            ],
        }
