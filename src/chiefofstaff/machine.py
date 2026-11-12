"""How much room this computer has left.

Copilot sessions themselves are light: an open, idle one takes about half a megabyte in the
Copilot CLI, and one that has worked about ten (measured on macOS). What weighs is what workers
run (test suites, builds, browsers) and the MCP servers each working session starts for itself.
So new worker turns wait while memory is low, and quiet workers let go of their Copilot session
(Workers.release_idle), which frees their MCP servers until their next message.

Disk space runs out more quietly: builds, dependencies and caches in workers' worktrees grow
by gigabytes, and a full disk fails every worker at once. So the disk is watched too (see
storage.py): the owner hears when it runs low, and new worker turns wait while it is nearly full.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import os
import shutil
import signal
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, fields
from pathlib import Path

# New worker turns wait while less than this share of memory (percent) is available.
MIN_FREE_PERCENT = float(os.environ.get("CHIEFOFSTAFF_MIN_FREE_MEMORY", "10"))
CACHE_SECONDS = 5.0
_cached: tuple[float, float | None] = (0.0, None)


def free_memory() -> float | None:
    """The share of memory still available (percent), as the system reckons it: on macOS the
    kernel's memory status level (Activity Monitor's memory pressure), on Linux MemAvailable.
    None where it cannot be read."""
    if sys.platform == "darwin":
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"))
            value, size = ctypes.c_uint32(0), ctypes.c_size_t(4)
            name = b"kern.memorystatus_level"
            if libc.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0) == 0:
                return float(value.value)
        except (OSError, AttributeError):
            return None
        return None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            fields = {
                line.split(":")[0]: int(line.split()[1])
                for line in meminfo.read_text().splitlines()
                if line.split()[1:2] and line.split()[1].isdigit()
            }
            return 100 * fields["MemAvailable"] / fields["MemTotal"]
        except (OSError, KeyError, ZeroDivisionError, ValueError):
            return None
    return None


def short_of_memory() -> float | None:
    """The share of memory available when it is too low to start more work, else None."""
    global _cached
    at, free = _cached
    if time.monotonic() - at > CACHE_SECONDS:
        free = free_memory()
        _cached = (time.monotonic(), free)
    return free if free is not None and free < MIN_FREE_PERCENT else None


GB = 1_000_000_000
# How long a reading of free disk space is used (it is read in a thread, at most this often).
DISK_CACHE_SECONDS = 60.0
# An older reading holds no worker back: whatever keeps it fresh has stopped.
DISK_STALE_SECONDS = 600.0
# A level clears once free space is this far above its threshold (a fifth of it, at least 2 GB):
# free space that hovers at a threshold must not alert again and again.
DISK_CLEAR_SHARE = 0.2
DISK_CLEAR_BYTES = 2 * GB
# What a queued worker's card says while the disk is nearly full.
DISK_WAIT = "waiting for disk space"
LEVELS = ("normal", "warning", "critical")
# When the disk was last read, and what was found then (with its level).
_disk: tuple[float, dict | None] = (float("-inf"), None)


@dataclass(frozen=True)
class DiskLimits:
    """Below how much free space the disk is low (warning) and nearly full (critical): in GB,
    or in percent of the disk, whichever is more."""

    warning_gb: float = 30
    warning_percent: float = 5
    critical_gb: float = 10
    critical_percent: float = 2

    @classmethod
    def of(cls, settings: object) -> DiskLimits:
        """The owner's limits (Settings disk_*), where no CHIEFOFSTAFF_DISK_* variable sets one."""
        values: dict[str, float] = {}
        for field in fields(cls):
            value = float(getattr(settings, f"disk_{field.name}", getattr(cls(), field.name)))
            with suppress(ValueError):
                value = float(os.environ.get(f"CHIEFOFSTAFF_DISK_{field.name.upper()}", value))
            values[field.name] = value
        return cls(**values)

    def thresholds(self, total: int) -> tuple[float, float]:
        """The warning and critical thresholds in bytes, for a disk of this size."""
        critical = max(self.critical_gb * GB, total * self.critical_percent / 100)
        warning = max(self.warning_gb * GB, total * self.warning_percent / 100)
        return max(warning, critical), critical


def cleared(threshold: float) -> float:
    """How much free space ends a level whose threshold this is."""
    return threshold + max(threshold * DISK_CLEAR_SHARE, DISK_CLEAR_BYTES)


def disk_level(free: int, total: int, previous: str, limits: DiskLimits) -> str:
    """normal, warning or critical; a level holds until free space is clearly above it."""
    warning, critical = limits.thresholds(total)
    if free < critical or (previous == "critical" and free < cleared(critical)):
        return "critical"
    if free < warning or (previous in {"warning", "critical"} and free < cleared(warning)):
        return "warning"
    return "normal"


def read_disk(paths: list[str]) -> dict | None:
    """Free and total bytes of the fullest disk these folders are on (a folder not made yet: the
    disk of its nearest existing parent). None when none can be read."""
    found: dict | None = None
    seen: set[int] = set()
    for raw in paths:
        path = Path(raw)
        while not path.exists() and path != path.parent:
            path = path.parent
        try:
            device = path.stat().st_dev
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        if device in seen:
            continue
        seen.add(device)
        if found is None or usage.free < found["free"]:
            found = {"free": usage.free, "total": usage.total, "path": str(path)}
    return found


async def disk_status(
    paths: list[str], previous: str = "normal", limits: DiskLimits | None = None
) -> dict | None:
    """Free space on the disk of these folders and its level (previous: the level it was at),
    read in a thread at most every DISK_CACHE_SECONDS. None when it cannot be read."""
    global _disk
    at, status = _disk
    if status is None or time.monotonic() - at >= DISK_CACHE_SECONDS:
        status = await asyncio.to_thread(read_disk, paths)
        at = time.monotonic()
    if status is None:
        _disk = (at, None)
        return None
    limits = limits or DiskLimits()
    warning, critical = limits.thresholds(status["total"])
    status = {
        **status,
        "used_percent": round(100 * (1 - status["free"] / status["total"]), 1)
        if status["total"]
        else 0.0,
        "level": disk_level(status["free"], status["total"], previous, limits),
        "warning": int(warning),
        "critical": int(critical),
    }
    _disk = (at, status)
    return status


def short_of_disk() -> int | None:
    """Free bytes when the disk is nearly full (as last read, lately), else None."""
    at, status = _disk
    if status and status["level"] == "critical" and time.monotonic() - at < DISK_STALE_SECONDS:
        return status["free"]
    return None


def size_text(size: float) -> str:
    """Bytes in words: 820 MB, 8.4 GB, 97 GB."""
    if size < 1_000_000:
        return f"{max(size, 0) / 1000:.0f} KB"
    if size < GB:
        return f"{size / 1_000_000:.0f} MB"
    return f"{size / GB:.1f} GB" if size < 10 * GB else f"{size / GB:.0f} GB"


def disk_note(free: int) -> str:
    return f"{DISK_WAIT}: {size_text(free)} free"


def low_priority() -> list[str]:
    """What runs a command at the lowest priority: it may read a whole disk's worth of folders,
    and must not slow down anything else."""
    found = []
    if sys.platform.startswith("linux") and shutil.which("ionice"):
        found += ["ionice", "-c", "3"]
    if shutil.which("nice"):
        found += ["nice", "-n", "19"]
    return found


def processes(*attrs: str) -> list[dict]:
    """Windows: every process this user can list, with its pid and these attributes (None
    where it may not read one: an administrator's, another user's, the system's)."""
    import psutil

    return [process.info for process in psutil.process_iter(["pid", *attrs])]


def kill_process_tree(pid: int) -> None:
    """Stop a process and everything it started, at once."""
    if sys.platform != "win32":
        with suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
        return
    import psutil

    with suppress(psutil.Error):
        parent = psutil.Process(pid)
        for process in [*parent.children(recursive=True), parent]:
            with suppress(psutil.Error):
                process.kill()


async def run_quietly(arguments: list[str], limit: float) -> tuple[int, str] | None:
    """A command's exit code and output, or None when it cannot run or takes longer than
    `limit` seconds (it is stopped then, with its children)."""
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        async with asyncio.timeout(limit):
            output, _ = await process.communicate()
    except TimeoutError:
        kill_process_tree(process.pid)
        await process.wait()
        return None
    except asyncio.CancelledError:
        # Chief of Staff is stopping: a du of a huge folder must not run on after it.
        kill_process_tree(process.pid)
        raise
    return process.returncode or 0, output.decode(errors="replace")


async def disk_used(paths: list[str], limit: float = 600, depth: int = 0) -> dict[str, int] | None:
    """How much each of these files and folders takes on disk (bytes, by path as given), as
    `du -k -x` counts it, one du at the lowest priority (`depth`: also the folders that deep in
    them, in the same pass). A path that is gone is left out. None when du could not run or took
    longer than `limit`."""
    if not paths:
        return {}
    summary = ["-d", str(depth)] if depth else ["-s"]
    ran = await run_quietly([*low_priority(), "du", "-k", "-x", *summary, "--", *paths], limit)
    if ran is None:
        return None
    sizes = {}
    for line in ran[1].splitlines():
        size, _, path = line.partition("\t")
        if size.isdigit() and path:
            sizes[path] = int(size) * 1024
    return sizes


async def process_cwds(limit: float = 30) -> set[str] | None:
    """The folders this computer's programs run in (their working directory), as one
    `lsof -d cwd -Fn` lists them (on Linux without lsof: /proc). None when they cannot be read:
    then every folder may be in use."""
    if sys.platform == "win32":
        return await asyncio.to_thread(windows_cwds)
    if shutil.which("lsof"):
        ran = await run_quietly(["lsof", "-w", "-d", "cwd", "-Fn"], limit)
        if ran and ran[0] == 0:
            return {line[1:] for line in ran[1].splitlines() if line.startswith("n/")}
    if sys.platform.startswith("linux"):
        return await asyncio.to_thread(proc_cwds)
    return None


def windows_cwds() -> set[str]:
    """The folders programs run in, as far as this user may read them: as lsof and /proc
    without root, other users', the system's and administrators' programs are left out."""
    return {p["cwd"] for p in processes("cwd") if p["cwd"]}


def proc_cwds() -> set[str]:
    found = set()
    with suppress(OSError):
        for entry in os.scandir("/proc"):
            if entry.name.isdigit():
                with suppress(OSError):
                    found.add(str(Path("/proc", entry.name, "cwd").readlink()))
    return found


def in_use(folder: str, cwds: set[str]) -> bool:
    """Whether a program runs in this folder or in one inside it (any case, on Windows)."""
    paths = {os.path.normcase(path).rstrip(os.sep) for path in (folder, os.path.realpath(folder))}
    return any(
        cwd == path or cwd.startswith(path + os.sep)
        for cwd in map(os.path.normcase, cwds)
        for path in paths
    )
