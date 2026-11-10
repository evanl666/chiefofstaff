"""Disk space: what Chief of Staff's workers take on disk, and keeping it in check.

Workers build, install and test in their own worktrees, and what that leaves (build output,
dependencies, caches) grows by gigabytes a day. The disk's free space is read every minute (in a
thread); folders are measured with `du` at the lowest priority, one at a time, in the
background: a worker's folder at most every half hour, and only when it was active since it was
last measured (or when the owner asks, or the disk runs low). Nothing here walks a folder on the
event loop, and the state the page loads does not wait for any of it: the Storage dialog reads
its own report, from what was measured.

What a build or an install makes again (only what git ignores: see workspaces.ignored_kind) is
deleted from the worktrees of workers idle for cleanup_idle_days, and from a closed worker's
worktree that stays (Workspaces.release); never while the worker is in a turn, runs a command,
is open in a terminal, or a program runs in the folder. The owner is told once when the disk
runs low and once more when it is nearly full: then new worker turns wait (Workers.claim_slot),
those running go on. The shared uv cache is measured on request, never deleted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field

from . import machine
from .models import StrictModel
from .roles import WORKER
from .store import now
from .tasks import FINISHED, OPEN
from .workspaces import (
    OLD_TRASH,
    TRASH,
    clean_tree,
    delete_moved,
    folder_of,
    ignored_kind,
    ignored_listing,
    is_cache,
    leftovers,
    overlapping,
    own_trees,
    sort_ignored,
    status_lines,
    status_name,
    trash_of,
    uncommitted,
)

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

# A worker's folder is measured at most this often (and only when it was active since).
MEASURE_SECONDS = 30 * 60
# Idle workers are looked at for cleanup this often.
CLEAN_SECONDS = 60 * 60
# How long one du may take (a huge folder on a slow disk), and the shared uv cache's.
DU_SECONDS = 900
UV_SECONDS = 1800
# At most this many ignored entries of a worktree are measured one by one.
MAX_MEASURED = 2000
MAX_LISTED = 200
HISTORY = 50
TITLE_SECONDS = 300
TITLES_AT_ONCE = 40
# Store keys: a worker's measured folder, why a closed worker's worktrees stay, the owner's "keep
# its files", cleanups done, the disk's level and the note about it, uv's cache, other folders.
SIZE, KEPT, KEEP = "storage:size:", "storage:kept:", "storage:keep:"
HISTORY_KEY, LEVEL, NOTE = "storage:history", "storage:level", "storage:note"
UV_KEY, OTHERS = "storage:uv", "storage:others"
# What a cleanup could not delete from trash, by path: its size, since when, last tried.
STUCK = "storage:stuck"
STUCK_RETRY_SECONDS = 86400
DAY = 86400


class StorageSession(StrictModel):
    session_id: str = Field(min_length=1, max_length=64)


class StorageKeep(StorageSession):
    keep: bool


class StorageDelete(StorageSession):
    path: str = Field(min_length=1, max_length=4000)
    confirm: bool = False
    # What the owner reviewed (`digest` of its listing in Review files).
    seen: str = Field(default="", max_length=64)


def listing_digest(ignored: list[str], status: list[str]) -> str:
    """One word for all that is in a worktree beyond its commits: every ignored entry git lists
    (as git lists them: no setting changes which) and every line of its status. Delete goes
    ahead only if it is still what the owner saw."""
    text = "\0".join(sorted(ignored)) + "\1" + "\0".join(sorted(status))
    return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()


def timestamp(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def default_uv_cache() -> str:
    """Where uv keeps its cache when nothing says otherwise."""
    if found := os.environ.get("UV_CACHE_DIR"):
        return found
    if sys.platform == "win32":
        return str(Path(os.environ.get("LOCALAPPDATA", "~")).expanduser() / "uv" / "cache")
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return str(Path(base) / "uv")


class Storage:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.workspaces = runtime.workspaces
        self.workspaces.on_release = self.released
        self.workspaces.holds = self.holds
        self.disk: dict | None = None
        self.reading_disk = False
        # A background pass (measuring, cleaning) is running; what it is doing.
        self.working = ""
        # The owner asked to measure every folder (or the disk ran low).
        self.measure_all = False
        self.cleaned_at = float("-inf")
        self.cleaning: set[str] = set()
        self.uv_measuring = False
        # What came of the owner's last action on a worker (a cleanup, why not): shown by it.
        self.notes: dict[str, str] = {}
        self.sizes: dict[str, dict] | None = None
        self.published = ""
        self.titles: dict[str, tuple[float, str]] = {}
        self.titles_left = TITLES_AT_ONCE
        # Workers still following a task whose caches were cleaned: when they last did anything.
        self.caches_cleaned: dict[str, float] = {}
        # One du at a time: measuring reads every folder in what it measures.
        self.du_lock = asyncio.Lock()
        self.disk_lock = asyncio.Lock()

    # Periodic job

    async def tick(self):
        """Every minute (Runtime.watch_loop): what is due starts in the background, and this
        returns at once (a few SQL reads)."""
        if getattr(self.runtime, "demo", False):
            return
        if not self.reading_disk:
            self.reading_disk = True
            self.runtime.background(self.check_disk())
        if self.working:
            return
        due = self.due()
        clean = time.monotonic() - self.cleaned_at >= CLEAN_SECONDS
        if due or clean or self.measure_all:
            self.start_pass(due, clean)

    def start_pass(self, due: list[str], clean: bool):
        self.working = "measuring"
        if clean:
            self.cleaned_at = time.monotonic()
        self.runtime.background(self.run_pass(due, clean))

    def due(self) -> list[str]:
        """Workers whose folders to measure: those active since they were last measured, at
        most every MEASURE_SECONDS (all of them when the owner asked)."""
        sizes = self.cached_sizes()
        recent = datetime.fromtimestamp(time.time() - MEASURE_SECONDS, UTC).isoformat()
        found = []
        for row in self.store.all("SELECT id, updated_at FROM sessions WHERE role='worker'"):
            record = sizes.get(row["id"])
            at = record.get("at", "") if record else ""
            if not record or self.measure_all or (at < row["updated_at"] and at < recent):
                found.append(row["id"])
        return found

    async def run_pass(self, due: list[str], clean: bool):
        try:
            everything = self.measure_all
            for session_id in due:
                if row := self.store.find_session(session_id):
                    await self.measure(row)
            if everything:
                await self.measure_others()
                self.measure_all = False
            if clean:
                self.working = "cleaning"
                self.publish()
                await self.cleanup()
        finally:
            self.working = ""
            self.publish()

    # Disk space

    def disk_paths(self) -> list[str]:
        return [str(self.store.directory), str(self.workspaces.scratch_root())]

    async def check_disk(self):
        """Free space now, and the owner told once when it runs low (and once when nearly
        full). One reading at a time: two must not both see the level change."""
        try:
            async with self.disk_lock:
                previous = self.store.get(LEVEL, "normal")
                limits = machine.DiskLimits.of(self.store.settings())
                status = await machine.disk_status(self.disk_paths(), previous, limits)
                if not status:
                    return
                self.disk = status
                if status["level"] != previous:
                    self.store.set(LEVEL, status["level"])
                    self.level_changed(previous, status)
            self.publish()
        finally:
            self.reading_disk = False

    def level_changed(self, previous: str, status: dict):
        level = status["level"]
        if machine.LEVELS.index(level) > machine.LEVELS.index(previous):
            self.warn(status)
            # Every folder measured again, for the Storage dialog; what is due is cleaned now.
            self.measure_all = True
            if not self.working:
                self.start_pass(self.due(), clean=self.store.settings().auto_cleanup)
        else:
            self.settle_note()
            if level == "warning":
                self.note(
                    f"Disk space: {machine.size_text(status['free'])} free now, so new worker "
                    "turns go on. It is still low: review & clean in Storage (the disk icon at "
                    "the bottom of the sidebar).",
                    level,
                )
        if previous == "critical":
            self.runtime.background(self.runtime.workers.pump())  # Queued turns can start.

    def warn(self, status: dict):
        level = status["level"]
        free, total = machine.size_text(status["free"]), machine.size_text(status["total"])
        if level == "critical":
            text = (
                f"The disk is nearly full: {free} free of {total}. New worker turns wait until "
                "there is room; those running go on."
            )
        else:
            text = f"Disk space is low: {free} free of {total}."
        report = self.report()
        biggest = [w for w in report["workers"] if w["size"]][:3]
        if biggest:
            named = ", ".join(
                f"“{w['title']}” {machine.size_text(w['size'])}"
                + (" (closed)" if w["state"] == "closed" else "")
                for w in biggest
            )
            text += f" Biggest workers' folders: {named}."
        if report["recoverable"]:
            text += (
                f" About {machine.size_text(report['recoverable'])} can be cleaned safely "
                "(build output, dependencies and caches a build makes again)."
            )
        text += " Review & clean: open Storage (the disk icon at the bottom of the sidebar)."
        self.settle_note()
        self.note(text, level)
        router = self.store.get("router_id") or ""
        critical = level == "critical"
        self.runtime.alert(
            text, session_id=router, needs_action=critical, title="Disk space", urgent=critical
        )

    def note(self, text: str, level: str):
        """Under Needs you on Home until the disk has room again (or the owner saw it)."""
        if not self.store.get("router_id"):
            return
        meta = {"needs_action": True, "storage": level}
        message = self.runtime.add_message(
            self.runtime.router_id(), "update", text, meta, unread=True
        )
        self.store.set(NOTE, str(message["id"]))

    def settle_note(self):
        if found := self.store.get(NOTE):
            self.store.delete(NOTE)
            with suppress(LookupError, ValueError):
                self.runtime.update_seen(int(found))

    # Measuring

    def cached_sizes(self) -> dict[str, dict]:
        if self.sizes is None:
            self.sizes = {key: json.loads(value) for key, value in self.store.items(SIZE).items()}
        return self.sizes

    def save_size(self, session_id: str, record: dict):
        self.cached_sizes()[session_id] = record
        self.store.set(SIZE + session_id, json.dumps(record))

    async def folder(self, row: dict) -> Path | None:
        """A worker's own folder, in Chief of Staff's workspaces folder (see folder_of)."""
        workspace, roots = self.workspaces.workspace(row), self.workspaces.roots()
        return await asyncio.to_thread(folder_of, workspace, roots)

    async def trees(self, row: dict) -> list[tuple[dict, str]]:
        """A worker's worktrees and clones in its own folder: what may be measured and
        cleaned. Never a project folder of the owner's it works in."""
        folder = await self.folder(row)
        return await asyncio.to_thread(own_trees, folder, self.workspaces.checkouts(row))

    async def measure(self, row: dict) -> dict:
        """A worker's folder, measured: its size, and in each worktree what a build makes again
        and what else git ignores."""
        async with self.du_lock:
            folder = await self.folder(row)
            record: dict = {"at": now(), "size": 0, "regenerable": 0, "trees": []}
            if not folder or not await asyncio.to_thread(folder.is_dir):
                record["gone"] = True
                self.save_size(row["id"], record)
                return record
            sizes = await machine.disk_used([str(folder)], DU_SECONDS, depth=1)
            if sizes is None:
                logger.warning("Measuring %s took too long.", folder)
                old = self.cached_sizes().get(row["id"]) or record
                self.save_size(row["id"], {**old, "at": now(), "error": "took too long"})
                return old
            record["size"] = sizes.get(str(folder), 0)
            settings = self.store.settings()
            for checkout, tree in await self.trees(row):
                found = await sort_ignored(
                    checkout, tree, settings.regenerable_patterns, settings.never_clean_patterns
                )
                cleanable, kept = found or ([], [])
                changes = await uncommitted(checkout, tree)
                paths = {e: str(Path(tree, e.rstrip("/"))) for e in [*cleanable, *kept]}
                parts = await machine.disk_used(list(paths.values())[:MAX_MEASURED], DU_SECONDS)
                parts = parts or {}
                regenerable = sum(parts.get(paths[entry], 0) for entry in cleanable)
                record["regenerable"] += regenerable
                record["trees"].append({
                    "path": tree,
                    "branch": checkout.get("branch") or "",
                    "size": sizes.get(str(folder / Path(tree).name), 0),
                    "regenerable": regenerable,
                    "build": len(cleanable),
                    "kept": [
                        {"name": entry, "size": parts.get(paths[entry])}
                        for entry in kept[:MAX_LISTED]
                    ],
                    "changes": None if changes is None else len(changes),
                    # Build folders holding something the owner never wants cleaned.
                    "protected": [
                        entry
                        for entry in kept
                        if ignored_kind(entry, settings.regenerable_patterns) == "build"
                    ][:MAX_LISTED],
                    "unlisted": found is None,
                })  # fmt: skip
            self.save_size(row["id"], record)
            return record

    async def measure_others(self):
        """Folders in the workspaces folder that are no worker's (left from earlier)."""
        stored = [path for path in self.store.items("workspace:").values() if path]
        roots = self.workspaces.roots()

        def listed() -> list[str]:
            known = {os.path.realpath(path) for path in stored}
            found = []
            for root in roots:
                if root.is_dir():
                    found += [
                        entry.path
                        for entry in os.scandir(root)
                        if entry.is_dir(follow_symlinks=False)
                        and entry.name != TRASH
                        and os.path.realpath(entry.path) not in known
                    ]
            return found[:MAX_LISTED]

        folders = await asyncio.to_thread(listed)
        others = {}
        for folder in folders:
            async with self.du_lock:
                sizes = await machine.disk_used([folder], DU_SECONDS)
            if sizes:
                others[folder] = {"size": sizes.get(folder, 0), "at": now()}
        self.store.set(OTHERS, json.dumps(others))

    # Cleaning

    def busy_why(self, row: dict) -> str:
        """Why its folders must not be touched now ("" when they may)."""
        state = self.runtime.states[row["id"]]
        if state.busy or (not row["closed_at"] and row["status"] in {"working", "starting"}):
            return "it is working"
        if not row["closed_at"] and row["status"] == "queued":
            return "it is about to start"
        if state.held:
            return "a command it left running still runs"
        if self.store.one(
            "SELECT 1 FROM commands WHERE session_id=? AND ended_at IS NULL LIMIT 1", (row["id"],)
        ):
            return "a command it started still runs"
        if self.runtime.copilot_id(row["id"]) in self.runtime.elsewhere:
            return "it is open in a terminal"
        return ""

    def holds(self, row: dict) -> str:
        """Why a closed worker's worktrees stay as they are for now (Workspaces.release): the
        owner keeps its files, or works in it in a terminal. Closing it stopped its turn and
        its commands; a program still running in a folder is checked there."""
        if self.store.get(KEEP + row["id"]):
            return "Keep its files is on"
        if self.runtime.copilot_id(row["id"]) in self.runtime.elsewhere:
            return "it is open in a terminal"
        return ""

    def last_active(self, row: dict) -> float:
        task = self.store.value(
            "SELECT max(updated_at) FROM tasks WHERE session_id=?", (row["id"],)
        )
        return max(timestamp(row["updated_at"]), timestamp(task or ""))

    def has_open_task(self, session_id: str) -> bool:
        marks = ",".join("?" * len(OPEN))
        return bool(
            self.store.one(
                f"SELECT 1 FROM tasks WHERE session_id=? AND status IN ({marks}) LIMIT 1",  # noqa: S608
                (session_id, *sorted(OPEN)),
            )
        )

    def idle_why(self, row: dict, days: int) -> str:
        """Why an open worker's build output is not cleaned automatically ("" when it is)."""
        if self.runtime.special(row):
            return "Chief of Staff's own helper"
        if self.store.get(KEEP + row["id"]):
            return "Keep its files is on"
        if self.has_open_task(row["id"]):
            return "it has open work"
        idle = (time.time() - self.last_active(row)) / DAY
        if idle < days:
            return f"active {idle:.1f} days ago"
        return self.busy_why(row)

    def monitored(self, row: dict) -> bool:
        """Whether it still follows up a finished task (its PR's checks, a run under way): it
        may need what it built, so only tools' caches are cleaned (see workspaces.CACHES)."""
        actions = self.runtime.task_actions
        return any(
            task["status"] in FINISHED and actions.monitoring(task)
            for task in self.runtime.tasks.for_session(row["id"])
        )

    async def shared_with(self, row: dict, tree: str) -> str:
        """Another open worker that works in this folder: its title."""
        return await asyncio.to_thread(overlapping, tree, self.workspaces.others(row))

    async def cleanup(self):
        """What is due: closed workers' worktrees an earlier version kept whole (or that were in
        use then), and the build output of workers idle for cleanup_idle_days. Which folders
        programs run in is read once for it all."""
        settings = self.store.settings()
        await self.empty_leftovers()
        if not settings.auto_cleanup:
            return
        closed = await self.unreleased()
        sizes = self.cached_sizes()
        rows = []
        for at, row in enumerate(
            self.store.open_sessions(WORKER) if settings.cleanup_idle_days else []
        ):
            if at % 25 == 24:
                await asyncio.sleep(0)  # Many workers: others' turns go on meanwhile.
            if (sizes.get(row["id"]) or {}).get("regenerable") and not self.idle_why(
                row, settings.cleanup_idle_days
            ):
                rows.append(row)
        if not closed and not rows:
            return
        cwds = await machine.process_cwds()
        if cwds is None:
            logger.info("Not cleaning workers' folders: cannot tell which ones programs run in.")
            return
        self.workspaces.known_cwds = cwds
        try:
            for row in closed:
                await self.runtime.workers.release(row["id"])
        finally:
            self.workspaces.known_cwds = None
        for row in rows:
            # Looked at again: it may have started a turn since.
            fresh = self.store.session(row["id"])
            if fresh["closed_at"] or self.idle_why(fresh, settings.cleanup_idle_days):
                continue
            caches = self.monitored(fresh)
            if caches and self.caches_cleaned.get(fresh["id"]) == self.last_active(fresh):
                continue  # Its caches went, and it has not run anything since.
            with suppress(ValueError):  # Busy after all: next time.
                await self.clean(fresh, cwds, by="automatic", caches_only=caches)
                if caches:
                    self.caches_cleaned[fresh["id"]] = self.last_active(fresh)

    async def empty_leftovers(self):
        """What a cleanup left in trash: what Chief of Staff stopped deleting (it was stopped
        itself) goes now and is counted; what it cannot delete (owned by another user: a
        container's build) is shown in the Storage view as such, tried again once a day and
        counted only once it goes."""
        stuck = json.loads(self.store.get(STUCK) or "{}")
        found, old = [], []
        for root in self.workspaces.roots():
            found += await asyncio.to_thread(leftovers, root / TRASH)
            if earlier := await asyncio.to_thread(leftovers, root / OLD_TRASH):
                found += earlier
                old.append(root / OLD_TRASH)
        present = {str(item) for item in found}
        kept = {path: value for path, value in stuck.items() if path in present}
        due = [
            item
            for item in found
            if time.time() - (kept.get(str(item)) or {}).get("tried", 0) >= STUCK_RETRY_SECONDS
        ]
        if not due:
            if kept != stuck:
                self.store.set(STUCK, json.dumps(kept))
            return
        new = [str(item) for item in due if str(item) not in kept]
        async with self.du_lock:
            before = (await machine.disk_used(new, DU_SECONDS) or {}) if new else {}
        before |= {path: value["size"] for path, value in kept.items()}
        gone, left = [], []
        for item in due:
            (gone if await asyncio.to_thread(delete_moved, item) else left).append(str(item))
        async with self.du_lock:
            after = (await machine.disk_used(left, DU_SECONDS) or {}) if left else {}
        freed = sum(before.get(path, 0) for path in gone) + sum(
            max(0, before.get(path, 0) - after.get(path, before.get(path, 0))) for path in left
        )
        for path in gone:
            kept.pop(path, None)
        for path in left:
            kept[path] = {
                "size": after.get(path, before.get(path, 0)),
                "at": (kept.get(path) or {}).get("at") or now(),
                "tried": time.time(),
            }
        self.store.set(STUCK, json.dumps(kept))
        for folder in old:
            with suppress(OSError):
                await asyncio.to_thread(folder.rmdir)  # An earlier version's, once empty.
        if freed > 0:
            self.record(
                {"id": "", "title": "Earlier cleanup", "role": ""},
                "Deleted what an earlier cleanup left",
                [Path(path).name.partition("-")[2] for path in gone + left],
                freed,
                "automatic",
            )

    async def unreleased(self) -> list[dict]:
        """Closed workers whose worktrees are still there, never released by this version (or
        that a program or a terminal was using then, or kept whole while automatic cleanup was
        off); not those whose files the owner keeps."""
        found = []
        rows = self.store.all(
            "SELECT id, role, title, cwd, branch, project, repos FROM sessions "
            "WHERE role='worker' AND closed_at IS NOT NULL"
        )
        kept, keep = self.store.items(KEPT), self.store.items(KEEP)
        for at, row in enumerate(rows):
            if at % 25 == 24:
                await asyncio.sleep(0)
            record = kept.get(row["id"])
            if row["id"] in keep or (
                record
                and not any(
                    tree.get("in_use") or tree.get("pending") or tree.get("held")
                    for tree in json.loads(record).get("trees", [])
                )
            ):
                continue
            if any(checkout.get("worktree") for checkout, _ in await self.trees(row)):
                found.append(row)
            elif not record:
                self.store.set(KEPT + row["id"], json.dumps({"at": now(), "trees": []}))
        return found

    async def clean(
        self, row: dict, cwds: set[str], by: str = "you", caches_only: bool = False
    ) -> dict:
        """Delete what a build makes again from a worker's worktrees (only what git ignores;
        `caches_only`: only tools' caches), one entry at a time; stops if the worker starts a
        turn."""
        session_id = row["id"]
        if why := self.busy_why(row):
            raise ValueError(f"Not cleaned: {why}.")
        settings = self.store.settings()
        trash = trash_of(await self.folder(row), self.workspaces.roots())
        if not trash:
            raise ValueError("Not cleaned: its folder is not in Chief of Staff's workspaces.")
        freed, removed, skipped = 0, [], []
        # Each worktree cleaned: the bytes freed, and whether all its build output went.
        trees: dict[str, tuple[int, bool]] = {}
        self.cleaning.add(session_id)
        self.publish()
        try:
            async with self.runtime.states[session_id].checkouts:
                for checkout, tree in await self.trees(row):
                    if machine.in_use(tree, cwds):
                        skipped.append(f"a program runs in {Path(tree).name}")
                        continue
                    if other := await self.shared_with(row, tree):
                        skipped.append(f"“{other}” uses {Path(tree).name}")
                        continue
                    found = await sort_ignored(
                        checkout, tree, settings.regenerable_patterns, settings.never_clean_patterns
                    )
                    entries = [e for e in (found or ([], []))[0] if not caches_only or is_cache(e)]
                    if not found or not entries:
                        continue
                    done, size = await clean_tree(
                        tree,
                        entries,
                        trash,
                        stop=lambda: self.busy_why(self.store.session(session_id)),
                    )
                    removed += [str(Path(Path(tree).name, entry)) for entry in done]
                    freed += size
                    trees[tree] = (size, len(done) == len(found[0]))
        finally:
            self.cleaning.discard(session_id)
        if removed:
            self.record(row, "Cleaned build artifacts", removed, freed, by)
            if record := self.cached_sizes().get(session_id):
                left = []
                for tree in record["trees"]:
                    size, whole = trees.get(tree["path"], (0, False))
                    regenerable = 0 if whole else max(0, tree.get("regenerable", 0) - size)
                    left.append({**tree, "regenerable": regenerable})
                record = {
                    **record,
                    "size": max(0, record.get("size", 0) - freed),
                    "regenerable": sum(tree.get("regenerable", 0) for tree in left),
                    "trees": left,
                }
                self.save_size(session_id, record)
        note = (
            f"Cleaned {len(removed)} build and cache folders: {machine.size_text(freed)} freed."
            if removed
            else "Nothing to clean."
        )
        if skipped:
            note += f" Skipped: {'; '.join(skipped)}."
        self.notes[session_id] = note
        self.publish()
        return {"cleaned": removed, "freed": freed, "skipped": skipped}

    def record(self, row: dict, what: str, items: list[str], freed: int, by: str):
        """A cleanup, for the history in the Storage dialog and the log."""
        title = self.title(row, limited=False)
        entry = {
            "at": now(),
            "session_id": row["id"],
            "title": title,
            "what": what,
            "items": items[:10],
            "count": len(items),
            "freed": freed,
            "by": by,
        }
        history = json.loads(self.store.get(HISTORY_KEY) or "[]")
        self.store.set(HISTORY_KEY, json.dumps([entry, *history][:HISTORY]))
        logger.info("%s in “%s” (%s): %s freed.", what, title, by, machine.size_text(freed))

    def released(self, row: dict, removed: list[str], kept: list[dict]):
        """What Workspaces.release did with a closed worker's worktrees, recorded: why each
        that stays stays, and the space it freed."""
        self.store.set(KEPT + row["id"], json.dumps({"at": now(), "trees": kept}))
        record = self.cached_sizes().get(row["id"])
        trees = {tree["path"]: tree for tree in (record or {}).get("trees", [])}
        gone = sum((trees.get(path) or {}).get("size", 0) for path in removed)
        if removed:
            self.record(row, "Removed its worktree on close", removed, gone, "automatic")
        cleaned = [f"{Path(tree['path']).name}/{e}" for tree in kept for e in tree["cleaned"]]
        freed = sum(tree["freed"] for tree in kept)
        if cleaned:
            self.record(row, "Cleaned build artifacts on close", cleaned, freed, "automatic")
        if record:
            self.save_size(
                row["id"],
                {
                    **record,
                    "size": max(0, record.get("size", 0) - gone - freed),
                    "regenerable": max(
                        0,
                        sum(
                            tree.get("regenerable", 0)
                            for path, tree in trees.items()
                            if path not in removed
                        )
                        - freed,
                    ),
                    "trees": [tree for path, tree in trees.items() if path not in removed],
                },
            )
        self.publish()

    # The owner's actions (routes)

    async def view(self) -> dict:
        if self.disk is None and not getattr(self.runtime, "demo", False):
            await self.check_disk()
        return self.report()

    def measure_now(self) -> dict:
        self.measure_all = True
        if not self.working:
            self.start_pass(self.due(), clean=False)
        return self.report()

    async def clean_now(self, session_id: str) -> dict:
        row = self.store.session(session_id)
        if row["role"] != WORKER:
            raise ValueError("Only a worker's folders are cleaned.")
        if session_id in self.cleaning:
            raise ValueError("It is being cleaned already.")
        if why := self.busy_why(row):
            raise ValueError(f"Not now: {why}. Try again once it is idle.")
        if not await self.trees(row):
            raise ValueError("It has no worktree of its own to clean.")
        self.cleaning.add(session_id)
        self.runtime.background(self.clean_later(row))
        return self.report()

    async def clean_later(self, row: dict):
        try:
            cwds = await machine.process_cwds()
            if cwds is None:
                self.notes[row["id"]] = "Not cleaned: cannot tell which folders programs run in."
                return
            self.cleaning.discard(row["id"])
            await self.clean(self.store.session(row["id"]), cwds)
        except ValueError as exc:
            self.notes[row["id"]] = str(exc)
        finally:
            self.cleaning.discard(row["id"])
            self.publish()

    def keep(self, session_id: str, keep: bool) -> dict:
        row = self.store.session(session_id)
        if keep:
            self.store.set(KEEP + row["id"], now())
        else:
            self.store.delete(KEEP + row["id"])
        self.publish()
        return self.report()

    async def files(self, session_id: str) -> dict:
        """What would be lost with a worker's worktrees: ignored files no build makes again and
        uncommitted changes (with sizes where they are cheap: files, and folders measured), and
        what a build makes again."""
        row = self.store.session(session_id)
        settings = self.store.settings()
        record = self.cached_sizes().get(row["id"]) or {}
        measured = {
            (tree["path"], item["name"]): item.get("size")
            for tree in record.get("trees", [])
            for item in tree.get("kept", [])
        }
        # Build folders holding something the owner never wants cleaned, as measured (looking
        # through them is for the background, not for this request).
        protected = {
            (tree["path"], name)
            for tree in record.get("trees", [])
            for name in tree.get("protected", [])
        }
        found = []
        for checkout, tree in await self.trees(row):
            # What the owner reviews, before any of it is sorted (by patterns that may change).
            raw = await ignored_listing(checkout, tree)
            lines = await status_lines(checkout, tree)
            digest = listing_digest(raw, lines) if raw is not None and lines is not None else ""
            listed = await sort_ignored(
                checkout,
                tree,
                settings.regenerable_patterns,
                settings.never_clean_patterns,
                look_inside=False,
                listing=raw,
            )
            cleanable, kept = listed or ([], [])
            held = [entry for entry in cleanable if (tree, entry) in protected]
            cleanable = [entry for entry in cleanable if entry not in held]
            kept = sorted([*kept, *held])
            changes = None if lines is None else [status_name(line) for line in lines]

            def sized(names: list[str], tree: str = tree) -> list[dict]:
                items = []
                for name in names[:MAX_LISTED]:
                    size = measured.get((tree, name))
                    path = Path(tree, name.rstrip("/"))
                    if size is None and not name.endswith("/"):
                        try:
                            size = path.lstat().st_size
                        except OSError:
                            size = None
                    items.append({"name": name, "size": size})
                return items

            found.append({
                "path": tree,
                "branch": checkout.get("branch") or "",
                "worktree": bool(checkout.get("worktree")),
                "kept": await asyncio.to_thread(sized, kept),
                "changes": await asyncio.to_thread(sized, changes or []),
                "changes_unread": changes is None,
                "build": cleanable[:MAX_LISTED],
                "build_count": len(cleanable),
                "unlisted": listed is None,
                "digest": digest,
            })  # fmt: skip
        return {"session_id": row["id"], "closed": bool(row["closed_at"]), "trees": found}

    async def delete_worktree(
        self, session_id: str, path: str, confirm: bool, seen: str = ""
    ) -> dict:
        """A closed worker's worktree, deleted with what it holds (`git worktree remove
        --force`), once the owner confirmed. Its branch stays in the repository: reopening the
        worker brings the worktree back from it, without what was only in the folder."""
        if not confirm:
            raise ValueError("Confirm first: what is listed in the worktree is lost.")
        row = self.store.session(session_id)
        if row["role"] != WORKER or not row["closed_at"]:
            raise ValueError("Only a closed worker's worktree can be deleted. Close it first.")

        trees = await self.trees(row)

        def find() -> dict | None:
            real = os.path.realpath(path)
            return next(
                (c for c, tree in trees if c.get("worktree") and os.path.realpath(tree) == real),
                None,
            )

        checkout = await asyncio.to_thread(find)
        if not checkout:
            raise ValueError(
                "Not one of its worktrees in Chief of Staff's workspaces folder: not deleted."
            )
        if why := self.busy_why(row):
            raise ValueError(f"Not now: {why}.")
        tree = checkout["worktree"]
        # What would be lost must be known, and be what the owner saw (read again now: a file
        # that appeared since is never deleted unseen).
        raw = await ignored_listing(checkout, tree)
        lines = await status_lines(checkout, tree)
        if raw is None or lines is None:
            raise ValueError(
                f"Not deleted: git could not read what is in {tree} (its changes or ignored "
                "files). Look at it yourself."
            )
        if listing_digest(raw, lines) != seen:
            raise ValueError(
                "Not deleted: what is in it changed since you reviewed it. Look again."
            )
        cwds = await machine.process_cwds()
        if cwds is None or machine.in_use(tree, cwds):
            raise ValueError("A program runs in it (or this cannot be checked): not deleted.")
        if user := await self.shared_with(row, tree):
            raise ValueError(f"Not deleted: “{user}” works in it.")
        trash = trash_of(await self.folder(row), self.workspaces.roots())
        if not trash:
            raise ValueError("Not in Chief of Staff's workspaces folder: not deleted.")
        record = self.cached_sizes().get(row["id"]) or {}
        size = next((t.get("size", 0) for t in record.get("trees", []) if t["path"] == tree), 0)
        async with self.runtime.states[row["id"]].checkouts:
            if not await self.workspaces.force_remove(checkout, tree, trash):
                raise ValueError("git could not remove it all.")
        with suppress(OSError):
            # Only when nothing else is in it, like a worker's notes.
            await asyncio.to_thread(self.workspaces.workspace(row).rmdir)
        self.record(row, "Deleted its worktree", [checkout["worktree"]], size, "you")
        kept = json.loads(self.store.get(KEPT + row["id"]) or '{"trees": []}')
        kept["trees"] = [t for t in kept["trees"] if t.get("path") != checkout["worktree"]]
        self.store.set(KEPT + row["id"], json.dumps(kept))
        if record:
            trees_left = [t for t in record["trees"] if t["path"] != checkout["worktree"]]
            self.save_size(
                row["id"],
                {
                    **record,
                    "size": max(0, record.get("size", 0) - size),
                    "regenerable": sum(t.get("regenerable", 0) for t in trees_left),
                    "trees": trees_left,
                },
            )
        self.notes[row["id"]] = f"Deleted {Path(checkout['worktree']).name}; its branch stays."
        self.publish()
        return self.report()

    async def uv_measure(self) -> dict:
        """How much uv's shared package cache takes: measured only when the owner asks (it can
        hold a hundred gigabytes), never cleaned here (`uv cache clean` while Chief of Staff
        runs can delete the environment it runs from)."""
        if not self.uv_measuring:
            self.uv_measuring = True
            self.runtime.background(self.measure_uv())
        return self.report()

    async def measure_uv(self):
        try:
            path = default_uv_cache()
            if shutil.which("uv"):
                ran = await machine.run_quietly(["uv", "cache", "dir"], 20)
                if ran and ran[0] == 0 and ran[1].strip():
                    path = ran[1].strip().splitlines()[-1]
            found: dict = {"path": path, "at": now(), "size": None}
            if await asyncio.to_thread(os.path.isdir, path):
                async with self.du_lock:
                    sizes = await machine.disk_used([path], UV_SECONDS)
                found["size"] = (sizes or {}).get(path)
                found["error"] = "" if sizes is not None else "Measuring took too long."
            else:
                found["error"] = "No uv cache there."
            self.store.set(UV_KEY, json.dumps(found))
        finally:
            self.uv_measuring = False
            self.publish()

    # The report

    def report(self) -> dict:
        """What the Storage dialog shows, from what was measured (no folder is read here)."""
        self.titles_left = TITLES_AT_ONCE
        settings = self.store.settings()
        sizes = self.cached_sizes()
        kept = self.store.items(KEPT)
        keep = self.store.items(KEEP)
        marks = ",".join("?" * len(OPEN))
        busy_tasks = {
            row["session_id"]
            for row in self.store.all(
                f"SELECT DISTINCT session_id FROM tasks WHERE status IN ({marks})",  # noqa: S608
                tuple(sorted(OPEN)),
            )
        }
        workers = []
        rows = self.store.all(
            "SELECT id, role, title, status, closed_at, updated_at FROM sessions "
            "WHERE role='worker'"
        )
        for row in rows:
            record = sizes.get(row["id"]) or {}
            closed = bool(row["closed_at"])
            if closed and not record.get("size") and row["id"] not in kept:
                continue
            stays = json.loads(kept.get(row["id"]) or "{}").get("trees", []) if closed else []
            if record.get("gone") or (closed and not record.get("size") and not stays):
                continue  # Nothing of it on disk (or not in Chief of Staff's folders).
            workers.append(self.worker_view(row, record, stays, row["id"] in busy_tasks, keep))
        workers.sort(key=lambda w: (-(w["size"] or 0), w["title"]))
        others = json.loads(self.store.get(OTHERS) or "{}")
        stuck = json.loads(self.store.get(STUCK) or "{}")
        disk = self.disk or {}
        total = (
            sum(w["size"] or 0 for w in workers)
            + sum(o["size"] for o in others.values())
            + sum(value["size"] for value in stuck.values())
        )
        return {
            "disk": {
                key: disk.get(key)
                for key in ("free", "total", "used_percent", "level", "warning", "critical", "path")
            },
            "root": str(self.workspaces.scratch_root()),
            "total": total,
            "recoverable": sum(w["regenerable"] or 0 for w in workers),
            "workers": workers,
            "others": [{"path": path, **value} for path, value in others.items()],
            "stuck": [
                {"path": path, "size": value["size"], "at": value["at"]}
                for path, value in stuck.items()
            ],
            "history": json.loads(self.store.get(HISTORY_KEY) or "[]"),
            "uv": {
                "path": default_uv_cache(),
                "size": None,
                "at": "",
                **json.loads(self.store.get(UV_KEY) or "{}"),
                "measuring": self.uv_measuring,
            },
            "working": self.working,
            "measuring_all": self.measure_all,
            "settings": {
                key: getattr(settings, key)
                for key in (
                    "auto_cleanup",
                    "cleanup_idle_days",
                    "regenerable_patterns",
                    "never_clean_patterns",
                    "disk_warning_gb",
                    "disk_warning_percent",
                    "disk_critical_gb",
                    "disk_critical_percent",
                )
            },
        }

    def worker_view(
        self, row: dict, record: dict, stays: list[dict], open_work: bool, keep: dict
    ) -> dict:
        state = self.runtime.states[row["id"]]
        idle_days = 0
        if row["closed_at"]:
            shown = "closed"
        elif state.busy or row["status"] in {"working", "starting"}:
            shown = "running"
        elif open_work or row["status"] in {"waiting", "queued"}:
            shown = "waiting"
        else:
            idle_days = int((time.time() - timestamp(row["updated_at"])) // DAY)
            shown = "idle"
        reasons = []
        for tree in stays:
            name = Path(tree["path"]).name
            if tree.get("changes"):
                reasons.append(f"{name}: {len(tree['changes'])} uncommitted changes")
            if tree.get("items"):
                reasons.append(f"{name}: ignored files ({', '.join(tree['items'][:3])})")
            if tree.get("unlisted"):
                reasons.append(f"{name}: its ignored files could not be listed")
            if tree.get("changes_unread"):
                reasons.append(f"{name}: its changes could not be read")
            if tree.get("held"):
                reasons.append(f"{name}: {tree['held']}")
        return {
            "session_id": row["id"],
            "title": self.title(row),
            "state": shown,
            "idle_days": idle_days,
            "size": record.get("size"),
            "regenerable": record.get("regenerable"),
            "measured_at": record.get("at", ""),
            "error": record.get("error", ""),
            "trees": [
                {key: tree.get(key) for key in ("path", "branch", "size", "regenerable", "changes")}
                | {"kept": len(tree.get("kept") or [])}
                for tree in record.get("trees", [])
            ],
            "kept": reasons,
            "keep": row["id"] in keep,
            "cleaning": row["id"] in self.cleaning,
            "note": self.notes.get(row["id"], ""),
        }

    def title(self, row: dict, limited: bool = True) -> str:
        """Its name as everywhere else, looked up at most every few minutes (it reads tasks), and
        for at most TITLES_AT_ONCE workers per report: the others have the name they were
        started with until a later report (hundreds of workers must not hold up the others)."""
        at, title = self.titles.get(row["id"], (float("-inf"), ""))
        if time.monotonic() - at > TITLE_SECONDS:
            if limited and self.titles_left <= 0:
                return title or row["title"]
            self.titles_left -= 1
            title = self.runtime.display_title(row)
            self.titles[row["id"]] = (time.monotonic(), title)
        return title

    def publish(self):
        """The report to the page, when it changed (free space by the 100 MB)."""
        report = self.report()
        free = (report["disk"]["free"] or 0) // 10**8
        shown = {
            **report,
            "disk": {**report["disk"], "free": free},
        }
        digest = hashlib.sha1(
            json.dumps(shown, sort_keys=True, default=str).encode(), usedforsecurity=False
        ).hexdigest()
        if digest != self.published:
            self.published = digest
            self.runtime.bus.publish("storage", report)
