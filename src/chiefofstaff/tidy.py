"""`chiefofstaff tidy-sessions`: the Copilot sessions a worker or a script made (provenance.py),
moved out of the owner's Copilot home when the owner says so.

Without --apply it only says what it would do: how many, how large, a few of each, grouped by
who made them, and what was learned from them; nothing is changed. With --apply, run by the owner
in their own terminal (refused in a Copilot session's shell, where a worker's commands run), it
moves each such session's folder whole into <Copilot home>/session-state-moved-by-chiefofstaff/
<when>/ with a manifest.json saying what each was: moving a folder back into session-state
brings it back. Nothing is deleted. A session open now or changed in the last hour stays where
it is, as do all others (the owner's, Chief of Staff's own, a program's). What was read from the
sessions it finds as the owner's words and not learned from yet is dropped, and who made each is
kept as Chief of Staff keeps it (provenance.py); what was learned from them stays, and is named:
the owner decides in Settings → You.

It lists the sessions as Chief of Staff does (it starts the Copilot CLI to ask it) and reads
Chief of Staff's data directory to know its workers; Chief of Staff may be running meanwhile.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import stat
import sys
import time
import unicodedata
from collections.abc import Iterable
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .backend import SESSION_ID, copilot_backend, copilot_home, running
from .habits import learned_from
from .provenance import BATCH, KV, MADE_BY_WORKER, Provenance, identity, known
from .send import default_data_dir
from .store import DB_NAME, Store
from .workspaces import Workspaces

# A session changed this recently stays: it may still be at work.
RECENT = 3600.0
MOVED = "session-state-moved-by-chiefofstaff"
EXAMPLES = 3
# Shown of a session's summary in the list.
SUMMARY = 72
# Set in the shell of a Copilot session (the CLI's, and so a worker's): not the owner's terminal.
AGENT_SHELL = ("COPILOT_CLI", "COPILOT_AGENT_SESSION_ID")
USAGE = """\
Moves the Copilot sessions a Chief of Staff worker or a script made (one-shot `copilot -p`
sessions) out of your Copilot home. Without --apply it only shows what it would move.\
"""


@dataclass
class Found:
    """A session a worker or a script made, and whether it can be moved now."""

    session_id: str
    made: str  # worker or batch
    record: dict  # what provenance decided (by, why, group, at)
    summary: str
    cwd: str
    folder: Path
    size: int = 0
    changed: float = 0.0  # its folder's latest change
    stays: str = ""  # why it is not moved ("" when it is)
    log: tuple | None = None  # its log as it was looked at


@dataclass
class Survey:
    home: Path
    found: list[Found] = field(default_factory=list)
    unlearned: int = 0
    learned: list[dict] = field(default_factory=list)
    workers: dict[str, str] = field(default_factory=dict)  # session id -> its title

    @property
    def moving(self) -> list[Found]:
        return [item for item in self.found if not item.stays]


def shown(text: str, limit: int = SUMMARY) -> str:
    """A session's words, safe to print: on one line, without control characters (a prompt can
    hold a terminal's escape codes), cut to `limit`."""
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text or "")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def place(path: str) -> str:
    """A folder as the owner reads it: ~ for their home, its end when it is long."""
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        path = "~" + path[len(home) :]
    path = shown(path, 4096)
    return (path if len(path) <= 60 else "…" + path[-59:]) or "?"


def size_text(size: float) -> str:
    for unit in ("bytes", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover


def when(seconds: float) -> str:
    """A time as the owner reads it: in their time zone."""
    if not seconds:
        return "?"
    return datetime.fromtimestamp(seconds, UTC).astimezone().strftime("%Y-%m-%d %H:%M")


def plural(count: int, one: str, many: str = "") -> str:
    return f"{count} {one if count == 1 else many or one + 's'}"


def folder_facts(folder: Path) -> tuple[int, float] | None:
    """A session folder's size and latest change (its own and of everything in it), never
    following a link; None when it is not a folder."""
    try:
        info = os.lstat(folder)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    size, changed = 0, info.st_mtime
    for root, folders, files in os.walk(folder):
        for name in folders + files:
            with suppress(OSError):
                entry = (Path(root) / name).lstat()
                size += entry.st_size if stat.S_ISREG(entry.st_mode) else 0
                changed = max(changed, entry.st_mtime)
    return size, changed


def locked(folder: Path) -> bool:
    """Whether a running program holds the session (the CLI's inuse.<pid>.lock)."""
    for lock in folder.glob("inuse.*.lock"):
        pid = lock.name.split(".")[1]
        if not pid.isdigit() or running(int(pid)):
            return True
    return False


def stays(item: Found, now: float) -> str:
    """Why a session cannot be moved now ("" when it can)."""
    facts = folder_facts(item.folder)
    if facts is None:
        return "not a folder"
    item.size, item.changed = facts
    if locked(item.folder):
        return "open now"
    if now - item.changed < RECENT:
        return "changed in the last hour"
    return ""


async def survey(store: Store, backend: Any, home: Path, now: float) -> Survey:
    """The sessions a worker or a script made, as Chief of Staff tells them apart."""
    listing = list(await backend.list_sessions())
    if not all(getattr(meta, "clients_listed", False) for meta in listing):
        raise RuntimeError(
            "This Copilot CLI does not say which app made each session, so they cannot be told "
            "apart. Nothing was changed."
        )
    in_use = set(await backend.open_elsewhere())
    state = home / "session-state"

    def log_of(session_id: str) -> Path | None:
        return state / session_id / "events.jsonl" if SESSION_ID.fullmatch(session_id) else None

    workspaces = Workspaces(store, store.directory / "work")
    provenance = Provenance(store, log_of, workspaces.own_folder, keep=False)
    made = await provenance.made(listing, in_use)
    records = known(store) | provenance.decided
    result = Survey(home)
    for meta in listing:
        if meta.session_id not in made:
            continue
        context = getattr(meta, "context", None)
        item = Found(
            meta.session_id,
            made[meta.session_id],
            records.get(meta.session_id, {}),
            getattr(meta, "summary", "") or "",
            (getattr(context, "working_directory", "") or "") if context else "",
            state / meta.session_id,
        )
        log = log_of(item.session_id)
        item.log = identity(log) if log else None
        item.stays = stays(item, now)
        result.found.append(item)
    result.found.sort(key=lambda item: float(item.record.get("at") or 0))
    ids = [item.session_id for item in result.found]
    if learns(store):
        result.unlearned = unlearned(store, ids)
        # What was learned from them, and from those moved before (decided then, kept since).
        result.learned = learned_from(store, {*ids, *known(store)})
    for item in result.found:
        if (by := item.record.get("by")) and by not in result.workers:
            row = store.find_session(by)
            result.workers[by] = row["title"] if row else "a worker no longer there"
    return result


def learns(store: Any) -> bool:
    """Whether the data is from a Chief of Staff that learns from the owner's words (one from
    before that has nothing to drop or name, and is not updated here)."""
    tables = {row["name"] for row in store.all("SELECT name FROM sqlite_master WHERE type='table'")}
    return {"observations", "style_versions", "lessons"} <= tables


def unlearned(store: Any, ids: Iterable[str]) -> int:
    count = 0
    for session_id in ids:
        prefix = f"copilot:{session_id}:"
        count += int(
            store.value(
                "SELECT count(*) FROM observations WHERE learned=0 AND source='copilot' "
                "AND substr(ref, 1, ?)=?",
                (len(prefix), prefix),
            )
        )
    return count


def report(result: Survey) -> list[str]:
    """What it found, grouped by who made them."""
    state = result.home / "session-state"
    if not result.found:
        lines = [f"No Copilot sessions a worker or a script made in {state}."]
        return lines + learned_lines(result.learned)
    lines = [f"Copilot sessions a worker or a script made, in {state}:", ""]
    groups: dict[tuple[str, str], list[Found]] = {}
    for item in result.found:
        key = (
            (MADE_BY_WORKER, item.record.get("by", ""))
            if item.made == MADE_BY_WORKER
            else (BATCH, item.record.get("group", ""))
        )
        groups.setdefault(key, []).append(item)
    for (made, key), items in groups.items():
        size = sum(item.size for item in items)
        count = plural(len(items), "session")
        if made == MADE_BY_WORKER:
            title = shown(result.workers.get(key, ""), 60)
            lines.append(f"  {count} a Chief of Staff worker made (“{title}”), {size_text(size)}")
        elif items[0].record.get("named"):
            lines.append(f"  {count} named as a batch's, {size_text(size)}")
        else:
            lines.append(f"  {count} of a script's batch (one-shot, alike), {size_text(size)}")
        span = when(float(items[0].record.get("at") or 0))
        if len(items) > 1:
            last = when(float(items[-1].record.get("at") or 0))
            span += f" to {last[11:] if last[:10] == span[:10] else last}"
        folders = list(dict.fromkeys(place(item.cwd) for item in items))
        more = f" and {plural(len(folders) - 2, 'other')}" if len(folders) > 2 else ""
        lines.append(f"    {span}, in {', '.join(folders[:2])}{more}")
        # Alike, a batch's start the same: each different one once.
        for summary in list(dict.fromkeys(shown(item.summary) for item in items))[:EXAMPLES]:
            lines.append(f"    e.g. “{summary}”")
    staying: dict[str, int] = {}
    for item in result.found:
        if item.stays:
            staying[item.stays] = staying.get(item.stays, 0) + 1
    if staying:
        said = ", ".join(f"{plural(count, 'session')} {why}" for why, count in staying.items())
        lines += ["", f"Left where they are: {said}."]
    if result.unlearned:
        lines += [
            "",
            f"{plural(result.unlearned, 'prompt')} of theirs, read as yours and not learned from "
            "yet, would be dropped.",
        ]
    return lines + learned_lines(result.learned)


def learned_lines(learned: list[dict]) -> list[str]:
    """What was learned from such sessions before (see habits.learned_from): named, kept."""
    if not learned:
        return []
    lines = ["", "Learned from them before (kept: check them in Settings → You):"]
    for item in learned:
        part = f"{item['from_them']} of its {plural(item['examples'], 'example')}"
        if item["kind"] == "style":
            name = f"{item['name']} (version {item['version']})"
            lines.append(f"  - How you write, {name}: {part}")
        else:
            waits = " (waiting for your OK)" if item["status"] == "proposed" else ""
            lines.append(f"  - Habit “{shown(item['name'], 80)}”{waits}: {part}")
    lines.append(
        "  Undo goes back to a style's previous version; a habit can be removed. Nothing "
        "learned is removed for you."
    )
    return lines


def agent_shell() -> str:
    return next((name for name in AGENT_SHELL if os.environ.get(name)), "")


def move(result: Survey, now: float) -> tuple[Path, list[Found], list[str]]:
    """Moves what can be moved, each checked again just before; a manifest says what each was
    and where it came from (written whatever happens). Returns the folder, those moved, and what
    failed."""
    stamp = datetime.fromtimestamp(now, UTC).strftime("%Y%m%d-%H%M%S")
    target = result.home / MOVED / stamp
    target.mkdir(parents=True, mode=0o700)
    moved: list[Found] = []
    failed: list[str] = []
    try:
        for item in result.moving:
            log = item.folder / "events.jsonl"
            why = stays(item, time.time()) or (
                "changed since it was looked at" if identity(log) != item.log else ""
            )
            if why:
                item.stays = why
                continue
            try:
                item.folder.rename(target / item.session_id)
            except OSError as exc:
                failed.append(f"{item.session_id}: {exc.strerror or exc}")
                continue
            moved.append(item)
    finally:
        manifest = {
            "moved_at": datetime.fromtimestamp(now, UTC).isoformat(),
            "from": str(result.home / "session-state"),
            "restore": "Move a session's folder back into the folder it came from to have it "
            "again (copilot --resume <id>).",
            "sessions": [
                {
                    "id": item.session_id,
                    "made_by": item.made,
                    "worker": result.workers.get(item.record.get("by", ""), ""),
                    "worker_session": item.record.get("by", ""),
                    "why": item.record.get("why", ""),
                    "summary": item.summary,
                    "cwd": item.cwd,
                    "began": datetime.fromtimestamp(
                        float(item.record.get("at") or 0), UTC
                    ).isoformat(),
                    "bytes": item.size,
                }
                for item in moved
            ],
        }
        if moved:
            (target / "manifest.json").write_text(
                json.dumps(manifest, indent=1, ensure_ascii=False)
            )
        else:
            target.rmdir()  # Nothing in it.
    return target, moved, failed


def settle(directory: Path, found: list[Found]) -> int:
    """In Chief of Staff's store, in one transaction (it may be running): what was read from
    these sessions as the owner's words and not learned from yet, dropped; and who made each,
    kept as Chief of Staff keeps it (provenance.py), so that what was learned from them stays
    named, and one moved back stays left out. Returns how much was dropped."""
    if not found:
        return 0
    with closing(sqlite3.connect(directory / DB_NAME, timeout=30)) as db, db:
        learning = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
        ).fetchone()
        dropped = 0
        for item in found:
            prefix = f"copilot:{item.session_id}:"
            if learning:
                dropped += db.execute(
                    "DELETE FROM observations WHERE learned=0 AND source='copilot' "
                    "AND substr(ref, 1, ?)=?",
                    (len(prefix), prefix),
                ).rowcount
            if item.record.get("as"):
                db.execute(
                    "INSERT OR IGNORE INTO kv VALUES (?, ?)",
                    (KV + item.session_id, json.dumps(item.record, sort_keys=True)),
                )
        return dropped


async def run(directory: Path, apply: bool, backend: Any = None) -> int:
    home = copilot_home()
    if not (directory / DB_NAME).is_file():
        print(
            f"No Chief of Staff data in {directory}: pass the one it uses with --data-dir.",
            file=sys.stderr,
        )
        return 2
    store = Store.reading(directory)
    own_backend = backend is None
    if backend is None:
        command = store.settings().copilot_command
        if not shutil.which(command):
            print(f"The Copilot CLI ({command}) was not found.", file=sys.stderr)
            return 1
        backend = copilot_backend(command)
        await backend.start()
    try:
        result = await survey(store, backend, home, time.time())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if own_backend:
            with suppress(Exception):
                await backend.stop()
        store.close()
    for line in report(result):
        print(line)
    moving = result.moving
    if not apply:
        line = "\nNothing was changed."
        if moving:
            size = size_text(sum(item.size for item in moving))
            line += (
                f" To move {plural(len(moving), 'session')} ({size}) to {home / MOVED}/<now>/ "
                "(move them back any time), run:\n  chiefofstaff tidy-sessions --apply"
            )
        print(line)
        return 0
    if not moving:
        return 0
    target, moved, failed = move(result, time.time())
    dropped = settle(directory, result.found)
    size = size_text(sum(item.size for item in moved))
    print(f"\nMoved {plural(len(moved), 'session')} ({size}) to {target}")
    print(
        f"Its manifest.json says what each was. To have one again, move its folder back into "
        f"{home / 'session-state'}."
    )
    if left := [item for item in moving if item.stays]:
        print(f"Left where they are (they changed meanwhile): {plural(len(left), 'session')}.")
    for problem in failed:
        print(f"Could not move {problem}", file=sys.stderr)
    if dropped:
        print(f"Dropped {plural(dropped, 'prompt')} of theirs not learned from yet.")
    return 1 if failed else 0


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="chiefofstaff tidy-sessions", description=USAGE)
    command.add_argument(
        "--apply",
        action="store_true",
        help="move them (run it in your own terminal; nothing is deleted)",
    )
    command.add_argument(
        "--data-dir", type=Path, default=None, help="Chief of Staff's, if not the default one"
    )
    return command


def main(arguments: list[str] | None = None) -> int:
    options = parser().parse_args(arguments)
    if options.apply and (name := agent_shell()):
        print(
            f"Not run: this shell is a Copilot session's ({name} is set). Run it in your own "
            "terminal: it moves your sessions.",
            file=sys.stderr,
        )
        return 2
    directory = (options.data_dir or default_data_dir()).expanduser().resolve()
    return asyncio.run(run(directory, options.apply))
