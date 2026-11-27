"""Chief's own verification of a result (autopilot.md §4.3, the re-review's fix 3): before
Autopilot accepts a result, Chief of Staff runs its checks again itself, offline, on a private
copy of the exact commit, and records what it ran and what came of it.

1. **Committed**: the task's one checkout is clean at commit C (no uncommitted work).
2. **A private copy**: a repository of Chief's own (its own git metadata; the owner's objects
   shared read-only through alternates), checked out at C; its tracked tree is read before and
   after the run: it must be C's tree both times.
3. **A plan Chief validated**, not command text run as given: each criterion's command (each
   must be backed by a passing run the receipts observed) and the worker's last test command,
   every one a test, check or build the receipts recognize, run from the copy's root with
   paths relative to it. Rejected: a path outside the copy (absolute, `..`, `~`, variables), a
   change of directory or root (`cd`, `-C`, `--rootdir`, `--prefix`), anything chained but
   `&&`, pipes and redirections, and what masks a failure (`|| true`, `--exit-zero`).
4. **Offline**: run in a session of Chief's own, no model and no credits, in an OS sandbox with
   no network at all, no credentials (an empty environment but for the path, the home folder
   and the offline switches of package managers), no keychain, and nothing readable of the
   owner's code folder, projects or workers' folders but the copy; installs come only from the
   shared package caches. Where that can't be had (no sandbox here, its check not passed), or a
   step needs more, nothing is verified: *Couldn't verify offline: it waits for you*.
5. **Passed** when every step exits 0, at least one test ran, and C changes no test, CI or
   check configuration against its base (integrity.classify).

Research without code is never verified (so never accepted by Autopilot)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .. import integrity, receipts
from ..backend import StepSandboxPolicy
from ..shepherd import PROBING
from ..workspaces import base_of, checkout_git, worth_committing

if TYPE_CHECKING:
    from ..runtime import Runtime

OFFLINE = "Couldn't verify offline: it waits for you"
# Each step's time, and the whole run's.
STEP_SECONDS = 600
RUN_SECONDS = 1800
# How long a first verification waits for the safety check of this Copilot version (run once a
# version, Shepherd.probe_refusal) to say whether its sandbox holds.
PROBE_SECONDS = 300
# What a plan's command may not hold (see the docstring).
OUTSIDE = re.compile(
    r"(?:^|\s|=)(?:/|~)|(?:^|[\s=/])\.\.(?:/|\s|$)|\$|`|(?:^|\s)(?:cd|pushd|popd|chdir)(?:\s|$)"
    r"|(?:^|\s)-C(?:\s|$)|--(?:rootdir|prefix|cwd|cd|dir|directory|chdir|working-directory)\b"
    r"|--exit-zero\b|\bset\s+\+e\b|\bexit\s+0\b|\btrue\b|[;|<>&]"
)
# The offline switches of package managers, in the run's otherwise empty environment.
OFFLINE_ENV = {
    "npm_config_offline": "true", "YARN_ENABLE_OFFLINE_MODE": "1", "UV_OFFLINE": "1",
    "PIP_NO_INDEX": "1", "GOFLAGS": "-mod=mod", "GOPROXY": "off", "CARGO_NET_OFFLINE": "true",
    "CI": "1", "NO_COLOR": "1",
}  # fmt: skip
# Installs from the shared caches, by what the copy has (offline: nothing downloaded).
INSTALLS = (
    ("pnpm-lock.yaml", "pnpm install --offline --frozen-lockfile --ignore-scripts"),
    ("yarn.lock", "yarn install --offline --frozen-lockfile --ignore-scripts"),
    ("package-lock.json", "npm ci --offline --ignore-scripts --no-audit --no-fund"),
    ("uv.lock", "uv sync --offline --frozen"),
)
EXIT = re.compile(r"COS-EXIT-(-?\d+)\s*$", re.MULTILINE)


def plan(commands: list[str]) -> tuple[list[str], str]:
    """The steps to run (each `a && b` split into its parts), or why the plan is rejected."""
    steps: list[str] = []
    for command in commands:
        for part in (piece.strip() for piece in command.split("&&")):
            if not part:
                return [], "its checks have an empty step"
            if OUTSIDE.search(part):
                return [], f"its check “{receipts.shown(part, 80)}” reaches outside its copy"
            if receipts.kind(part) not in {"test", "check", "build"}:
                return [], f"“{receipts.shown(part, 80)}” isn't a test, check or build"
            if part not in steps:
                steps.append(part)
    if not steps:
        return [], "it has no checks to run again"
    return steps, ""


def commands_of(found: dict | None) -> tuple[list[str], str]:
    """From a task's receipts: each criterion's backing command and its last test command, or
    why not (a criterion nothing observed backs)."""
    if not found:
        return [], "it has no receipts"
    commands = []
    for item in found.get("criteria") or []:
        if item.get("state") != "verified" or not item.get("command"):
            return [], f"nothing it ran shows “{receipts.shown(item['criterion'], 80)}”"
        commands.append(item["command"])
    tests = found.get("tests") or {}
    if tests.get("passed") and tests.get("command"):
        commands.append(tests["command"])
    if not commands:
        return [], "it ran no tests"
    return commands, ""


def git(path: Path, *arguments: str, seconds: float = 120) -> str:
    """git in the private copy, as Chief of Staff (no configuration of the owner's run)."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(path), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "LANG": "C"}  # fmt: skip
    done = subprocess.run(  # noqa: S603
        ["git", "-c", "core.hooksPath=/dev/null", *arguments],  # noqa: S607
        cwd=path, env=env, capture_output=True, text=True, timeout=seconds, check=False,
    )  # fmt: skip
    if done.returncode != 0:
        raise ValueError(done.stderr.strip()[:500] or f"git {arguments[0]} failed")
    return done.stdout


def made_copy(folder: Path, objects: str, commit: str) -> str:
    """A repository of its own at `commit`, the owner's objects shared read-only: its tree."""
    folder.mkdir(parents=True)
    git(folder, "init", "-q")
    alternates = folder / ".git" / "objects" / "info" / "alternates"
    alternates.write_text(objects + "\n")
    git(folder, "checkout", "-q", "--detach", commit)
    return git(folder, "rev-parse", f"{commit}^{{tree}}").strip()


def tree_holds(folder: Path, tree: str) -> bool:
    """Its tracked files are the commit's tree, unchanged (build output aside)."""
    try:
        git(folder, "update-index", "-q", "--refresh")
        changed = git(folder, "diff-files", "--name-only").strip()
        return not changed and git(folder, "write-tree").strip() == tree
    except (ValueError, subprocess.SubprocessError):
        return False


def wrapped(step: str) -> str:
    """A step as the sandboxed shell runs it: in an empty environment but for its path, home
    and the offline switches, then its exit code."""
    keep = " ".join(f"{name}={shlex.quote(value)}" for name, value in OFFLINE_ENV.items())
    return (
        f'env -i PATH="$PATH" HOME="$HOME" LANG=en_US.UTF-8 TMPDIR="$PWD/.cos-tmp" {keep} '
        f"sh -c {shlex.quote(step)}; printf 'COS-%s-%s\\n' EXIT \"$?\""
    )


Runner = Callable[[Path, list[str]], Awaitable[list[dict]]]


async def run_offline(runtime: Runtime, copy: Path, steps: list[str]) -> list[dict]:
    """Each step in a session of Chief's own, sandboxed with no network: {command, exit, output,
    seconds, sandboxed}. Through the session's own tool pipeline: no model, no credits."""
    from copilot.generated.rpc import PermissionDecisionApproveOnce

    from ..policy import GITHUB_MCP
    from ..shepherd import BLOCKED_HOSTS, step_denied
    from ..step_probe import run_tool

    backend = runtime.connected()
    settings = runtime.store.settings()
    others = [settings.code_folder, *settings.projects, str(runtime.store.directory)]
    policy = StepSandboxPolicy(
        readwrite=(str(copy),),
        denied=tuple(dict.fromkeys([*step_denied(), *(p for p in others if p)])),
        credentials=False, outbound=False, allowed_hosts=(), blocked_hosts=BLOCKED_HOSTS,
        keychain=False,
    )  # fmt: skip

    async def approve(request: Any, invocation: dict):
        return PermissionDecisionApproveOnce()  # The OS sandbox holds, not this check.

    options = {
        "on_permission_request": approve, "working_directory": str(copy),
        "client_name": "chiefofstaff", "sandbox_config": backend.sandbox(policy),
        "enable_config_discovery": False, "enable_session_store": False,
        "available_tools": ["bash"], "tools": [], "disabled_mcp_servers": [GITHUB_MCP, "slack"],
    }  # fmt: skip
    session_id, session, results = str(uuid4()), None, []
    try:
        created = await backend.create_session(
            session_id=session_id, name="Chief of Staff · verification", **options
        )
        with contextlib.suppress(Exception):
            await created.disconnect()
        session = await backend.resume_session(session_id, **options)
        (copy / ".cos-tmp").mkdir(exist_ok=True)
        for step in steps:
            started = time.monotonic()
            ran, text, sandboxed = await run_tool(
                session, "bash", {"command": wrapped(step), "description": "Verify"}, STEP_SECONDS
            )
            found = EXIT.findall(text or "")
            results.append({
                "command": step, "exit": int(found[-1]) if ran and found else None,
                "output": EXIT.sub("", text or ""), "seconds": round(time.monotonic() - started, 1),
                "sandboxed": sandboxed,
            })  # fmt: skip
            if results[-1]["exit"] != 0:
                break
    finally:
        if session is not None:
            with contextlib.suppress(Exception):
                await session.disconnect()
        with contextlib.suppress(Exception):
            if (client := getattr(backend, "client", None)) is not None:
                await client.delete_session(session_id)
    return results


async def refusal(runtime: Runtime) -> str:
    """Why nothing can be verified offline here ("" when it can): an OS sandbox with no
    network, whose check passed for this Copilot version."""
    if not (runtime.sandbox_host and runtime.backend and runtime.backend.capabilities.sandbox):
        return OFFLINE
    why = await runtime.shepherd.probe_refusal()
    deadline = time.monotonic() + PROBE_SECONDS
    while why.endswith(PROBING) and time.monotonic() < deadline:
        # Never checked here (a fresh install, a new version): waited for, then what it found.
        await asyncio.sleep(1)
        if not getattr(runtime.shepherd, "probing", False):
            why = await runtime.shepherd.probe_refusal()
    return OFFLINE if why else ""


async def verify(runtime: Runtime, task: dict, runner: Runner | None = None) -> dict:
    """{passed, why, record}: the verification record is kept whole in Autopilot's record."""
    record: dict[str, Any] = {"task": task["id"], "steps": []}

    def failed(why: str) -> dict:
        return {"passed": False, "why": why, "record": record}

    row = runtime.store.find_session(task["session_id"])
    checkouts = [
        c for c in (runtime.workspaces.checkouts(row) if row else []) if not c.get("shared")
    ]
    if not checkouts:
        return failed("it changed no code of its own to verify")
    if len(checkouts) > 1:
        return failed("it worked in more than one repository")
    checkout = checkouts[0]
    path = checkout["path"]
    if why := await refusal(runtime):
        return failed(why)
    try:
        status = await checkout_git(checkout, path, "status", "--porcelain")
        if worth_committing(status):
            return failed("its result isn't committed")
        commit = (await checkout_git(checkout, path, "rev-parse", "HEAD")).strip()
        common = (await checkout_git(checkout, path, "rev-parse", "--git-common-dir")).strip()
        base = base_of(checkout)
        start = (await checkout_git(checkout, path, "merge-base", base, commit)).strip()
        diff = await checkout_git(checkout, path, "diff", "--no-color", "--find-renames", start,
                                  commit, limit=120)  # fmt: skip
    except ValueError as exc:
        return failed(f"its checkout couldn't be read ({str(exc)[:200]})")
    objects = str((Path(path) / common / "objects").resolve())
    record |= {"repository": checkout.get("repo") or path, "branch": checkout.get("branch", ""),
               "base": base, "base_commit": start, "commit": commit}  # fmt: skip
    findings = await asyncio.to_thread(integrity.classify, integrity.parse_diff(diff))
    record["integrity"] = integrity.verdict(findings)
    if record["integrity"] != integrity.ELIGIBLE:
        return failed("it changes tests, CI or checks, which only you may accept")
    commands, why = commands_of(runtime.task_actions.receipts(task))
    if why:
        return failed(why)
    steps, why = plan(commands)
    if why:
        return failed(why)
    folder = runtime.store.directory / "autopilot-verify" / uuid4().hex[:12]
    try:
        tree = await asyncio.to_thread(made_copy, folder, objects, commit)
        record["tree"] = tree
        installs = [line for name, line in INSTALLS if (folder / name).is_file()][:1]
        if not await asyncio.to_thread(tree_holds, folder, tree):
            return failed("its copy didn't hold the commit's tree")
        run = runner or (lambda copy, lines: run_offline(runtime, copy, lines))
        results = await asyncio.wait_for(run(folder, [*installs, *steps]), RUN_SECONDS)
        record["steps"] = results
        record["tree_after"] = await asyncio.to_thread(tree_holds, folder, tree)
    except (ValueError, OSError, subprocess.SubprocessError, TimeoutError) as exc:
        return failed(f"{OFFLINE} ({str(exc)[:200]})")
    finally:
        await asyncio.to_thread(shutil.rmtree, folder, True)
    if not record["tree_after"]:
        return failed("its checks changed its committed files")
    if len(results) < len(installs) + len(steps) or any(r["exit"] != 0 for r in results):
        bad = next((r for r in results if r["exit"] != 0), None)
        if bad is None or bad["exit"] is None or bad["command"] in installs:
            return failed(OFFLINE)
        return failed(f"“{receipts.shown(bad['command'], 80)}” failed in Chief's own run")
    if any(not r["sandboxed"] for r in results):
        return failed(OFFLINE)
    ran = [r for r in results if receipts.kind(r["command"]) == "test"]
    if not any(receipts.last_summary(r["output"]) == "passed" for r in ran):
        return failed("no test ran in Chief's own run")
    for result in ran:
        identities = integrity.test_identities(result["command"], result["output"])
        result["tests"] = sorted(identities.passed or ())[:2000] if identities else []
    return {"passed": True, "why": "", "record": record}


def short(sha: str) -> str:
    return sha[:7]


def files_of(diff: str) -> list[str]:
    return [str(PurePosixPath(item.path)) for item in integrity.parse_diff(diff)]
