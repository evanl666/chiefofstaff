"""Disk space: warnings, held turns, and what closing and idle cleanup delete (and never do)."""

import asyncio
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import Harness, call_tool, settle
from fastapi.testclient import TestClient

from chiefofstaff import machine, storage
from chiefofstaff import workspaces as workspaces_module
from chiefofstaff.machine import GB, DiskLimits, disk_level
from chiefofstaff.models import Settings
from chiefofstaff.runtime import Runtime
from chiefofstaff.store import now
from chiefofstaff.web import create_app
from chiefofstaff.workspaces import TimedOutError, clean_tree, ignored_kind, sort_ignored

GIT = ("git", "-c", "user.email=t@t", "-c", "user.name=t")
REAL_PROCESS_CWDS = machine.process_cwds


@pytest.fixture(autouse=True)
def fresh_disk(monkeypatch):
    """No test sees another's (or this machine's) disk reading; no real program's folder counts
    as in use unless a test says so."""
    monkeypatch.setattr(machine, "_disk", (float("-inf"), None))

    async def none_in_use(limit: float = 30):
        return set()

    monkeypatch.setattr(machine, "process_cwds", none_in_use)


def run(*command, cwd=None):
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True).stdout


def write(path: Path, text: str = "x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def exists(path) -> bool:
    return Path(path).exists()


def resolved(path) -> Path:
    return Path(path).resolve()


def project(tmp_path: Path) -> Path:
    """A code folder with one project whose .gitignore covers build output, logs and a .env."""
    app = tmp_path / "code" / "app"
    app.mkdir(parents=True)
    run("git", "init", "-q", "-b", "main", cwd=app)
    write(
        app / ".gitignore",
        "build/\nnode_modules/\n__pycache__/\n.dart_tool/\ndist\n.env\nlogs/\n*.log\nout/*\n"
        "!out/keep.txt\n.cache/\n.pytest_cache/\n.vscode/\n",
    )
    write(app / "src" / "main.py", "print(1)\n")
    write(app / "build" / "tracked.txt", "forced in\n")
    run("git", "add", ".", cwd=app)
    run("git", "add", "-f", "build/tracked.txt", cwd=app)
    run(*GIT, "commit", "-qm", "init", cwd=app)
    return app


async def spawn(runtime, router, title, **extra):
    extra.setdefault("instructions", title)
    extra.setdefault("why_new", "test")
    result = await call_tool(runtime, "spawn_worker", router, title=title, **extra)
    return json.loads(result.text_result_for_llm)


async def harnessed(tmp_path: Path, **settings) -> Harness:
    (tmp_path / "code").mkdir(exist_ok=True)
    harness = Harness(tmp_path / "data", Settings(code_folder=str(tmp_path / "code"), **settings))
    await harness.connect()
    return harness


def disk(monkeypatch, free_gb: float, total_gb: float = 400):
    """The disk as read next: free and total, in GB."""
    reading = {"free": int(free_gb * GB), "total": int(total_gb * GB), "path": "/"}
    monkeypatch.setattr(machine, "read_disk", lambda paths: dict(reading))
    monkeypatch.setattr(machine, "_disk", (float("-inf"), None))


def idle_for(harness, session_id: str, days: float):
    """As if its last activity (and its tasks') was `days` ago, its tasks finished."""
    then = datetime.fromtimestamp(time.time() - days * 86400, UTC).isoformat()
    harness.store.execute("UPDATE sessions SET updated_at=?, status='idle' WHERE id=?",
                          (then, session_id))  # fmt: skip
    harness.store.execute(
        "UPDATE tasks SET status='review', updated_at=? WHERE session_id=?", (then, session_id)
    )
    harness.runtime.states[session_id].busy = False


# Disk levels


def test_levels_have_thresholds_in_gb_or_percent_and_clear_with_a_margin():
    limits = DiskLimits()
    total = int(400 * GB)
    assert disk_level(int(31 * GB), total, "normal", limits) == "normal"
    assert disk_level(int(29 * GB), total, "normal", limits) == "warning"
    assert disk_level(int(9 * GB), total, "normal", limits) == "critical"
    # Hysteresis: a level ends only once free space is clearly above it (1.2x, at least 2 GB).
    assert disk_level(int(35 * GB), total, "warning", limits) == "warning"
    assert disk_level(int(36.5 * GB), total, "warning", limits) == "normal"
    assert disk_level(int(11 * GB), total, "critical", limits) == "critical"
    assert disk_level(int(12.5 * GB), total, "critical", limits) == "warning"
    assert disk_level(int(36.5 * GB), total, "critical", limits) == "normal"
    # A big disk: 5% and 2% of it count when they are more than the GB.
    assert disk_level(int(40 * GB), int(926 * GB), "normal", limits) == "warning", "46 GB"
    big = int(4000 * GB)
    assert disk_level(int(150 * GB), big, "normal", limits) == "warning", "5% of 4 TB is 200 GB"
    assert disk_level(int(70 * GB), big, "normal", limits) == "critical", "2% of 4 TB is 80 GB"


def test_disk_limits_come_from_settings_and_the_environment(monkeypatch):
    settings = Settings(disk_warning_gb=50, disk_critical_gb=20)
    assert DiskLimits.of(settings).warning_gb == 50
    monkeypatch.setenv("CHIEFOFSTAFF_DISK_WARNING_GB", "80")
    assert DiskLimits.of(settings) == DiskLimits(80, 5, 20, 2)


async def test_the_owner_is_warned_once_per_level_and_the_note_goes_once_there_is_room(
    tmp_path, monkeypatch
):
    harness = await harnessed(tmp_path)
    runtime = harness.runtime

    async def at(free_gb: float):
        disk(monkeypatch, free_gb)
        await runtime.storage.check_disk()
        await settle(runtime)

    def notes():
        return [m for m in harness.store.messages(harness.router_id) if m["meta"].get("storage")]

    await at(100)
    assert not harness.notifications and not notes()
    await at(25)
    assert len(harness.notifications) == 1 and "Disk space is low: 25 GB free" in notes()[0]["text"]
    assert "Review & clean" in notes()[0]["text"]
    assert [u["id"] for u in runtime.owner_updates()] == [notes()[0]["id"]], "under Needs you"
    for free in (24, 31, 26, 35):  # Hovering around 30 GB: still the same low disk.
        await at(free)
    assert len(harness.notifications) == 1, "one alert while it stays low"
    await at(9)
    assert len(harness.notifications) == 2 and "nearly full" in notes()[-1]["text"]
    assert [u["id"] for u in runtime.owner_updates()] == [notes()[-1]["id"]], "the latest only"
    await at(11)
    assert len(harness.notifications) == 2, "not above 12 GB yet: still critical"
    assert machine.short_of_disk() == 11 * GB
    await at(13)
    assert machine.short_of_disk() is None and len(harness.notifications) == 2
    assert "turns go on" in notes()[-1]["text"], "told turns go on, without an alert"
    await at(40)
    assert not runtime.owner_updates(), "room again: the note leaves Needs you"
    await at(25)
    assert len(harness.notifications) == 3, "low again: a new alert"
    await at(40)
    disk(monkeypatch, 9)
    await asyncio.gather(runtime.storage.check_disk(), runtime.storage.check_disk())
    await settle(runtime)
    assert len(harness.notifications) == 4, "two readings at once: one alert"


async def test_a_nearly_full_disk_holds_new_worker_turns_until_there_is_room(tmp_path, monkeypatch):
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    earlier = await spawn(runtime, harness.router_id, "Started earlier")
    await settle(runtime)
    idle_for(harness, earlier["session_id"], 0)  # Its turn over.
    disk(monkeypatch, 8)
    await runtime.storage.check_disk()
    await runtime.owner_send(earlier["session_id"], "One more thing")
    await settle(runtime)
    row = harness.store.session(earlier["session_id"])
    assert row["status"] == "queued" and row["activity"].startswith("waiting for disk space")
    worker = await spawn(runtime, harness.router_id, "Build it")
    await settle(runtime)
    row = harness.store.session(worker["session_id"])
    assert row["status"] == "queued" and row["activity"] == "waiting for disk space: 8.0 GB free"
    assert not harness.client.sessions.get(worker["session_id"]), "not started"
    await runtime.workers.pump()
    assert harness.store.session(worker["session_id"])["activity"].startswith("waiting for disk")
    helper = await spawn(runtime, harness.router_id, "Learn")
    harness.store.set(f"learner:{helper['session_id']}", "1")  # Chief of Staff's own helper.
    await runtime.workers.pump()
    await settle(runtime)
    assert harness.store.session(helper["session_id"])["status"] != "queued", "helpers go on"
    disk(monkeypatch, 50)
    await runtime.storage.check_disk()
    await settle(runtime)
    assert harness.store.session(worker["session_id"])["status"] != "queued", "it starts"


# What closing a worker deletes


async def test_closing_removes_a_clean_worktree_and_cleans_build_output_from_one_that_stays(
    tmp_path,
):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    clean = await spawn(runtime, harness.router_id, "Clean", project=str(app), isolate=True)
    kept = await spawn(runtime, harness.router_id, "Kept", project=str(app), isolate=True)
    await settle(runtime)
    for info in (clean, kept):
        tree = Path(info["cwd"])
        write(tree / "build" / "classes" / "a.class", "x" * 100_000)
        write(tree / "node_modules" / "lib" / "x.js")
    tree = Path(kept["cwd"])
    write(tree / "logs" / "run.txt", "the owner may want this")
    write(tree / "debug.log", "and this")
    write(tree / "pkg" / "__pycache__" / "m.pyc")
    for info in (clean, kept):
        await runtime.workers.close(info["session_id"])
    await settle(runtime)

    assert not exists(clean["cwd"]), "build output alone keeps no worktree"
    assert exists(tree / "logs" / "run.txt") and exists(tree / "debug.log"), "the owner decides"
    assert not exists(tree / "build" / "classes") and not exists(tree / "node_modules")
    assert not exists(tree / "pkg" / "__pycache__")
    assert exists(tree / "build" / "tracked.txt"), "a tracked file is never deleted"
    assert exists(tree / "src" / "main.py")
    record = json.loads(harness.store.get(f"storage:kept:{kept['session_id']}"))
    assert record["trees"][0]["items"] == ["debug.log", "logs/"], "why it stays"
    history = json.loads(harness.store.get("storage:history"))
    assert {entry["what"] for entry in history} == {
        "Removed its worktree on close",
        "Cleaned build artifacts on close",
    }
    cleaned = next(e for e in history if e["session_id"] == kept["session_id"])
    assert cleaned["freed"] >= 100_000 and "app/build/classes/" in cleaned["items"]


async def test_a_worktree_in_use_or_with_cleanup_off_keeps_its_build_output_on_close(
    tmp_path, monkeypatch
):
    app = project(tmp_path)
    harness = await harnessed(tmp_path, auto_cleanup=False)
    runtime = harness.runtime
    off = await spawn(runtime, harness.router_id, "Off", project=str(app), isolate=True)
    used = await spawn(runtime, harness.router_id, "Used", project=str(app), isolate=True)
    await settle(runtime)
    for info in (off, used):
        write(Path(info["cwd"]) / "logs" / "x")
        write(Path(info["cwd"]) / "build" / "out.o")
    await runtime.workers.close(off["session_id"])
    await settle(runtime)
    assert exists(Path(off["cwd"]) / "build" / "out.o"), "automatic cleanup is off"

    harness.store.save_settings(harness.store.settings().model_copy(update={"auto_cleanup": True}))

    in_build = str(resolved(used["cwd"]) / "build")

    async def running_there(limit: float = 30):
        return {in_build}

    monkeypatch.setattr(machine, "process_cwds", running_there)
    await runtime.workers.close(used["session_id"])
    await settle(runtime)
    assert exists(Path(used["cwd"]) / "build" / "out.o"), "a program runs in it"
    again = {row["id"] for row in await runtime.storage.unreleased()}
    assert again == {off["session_id"], used["session_id"]}, "looked at again later"
    await runtime.storage.cleanup()
    await settle(runtime)
    assert not exists(Path(off["cwd"]) / "build" / "out.o"), "cleaned once cleanup is on"
    assert exists(Path(off["cwd"]) / "logs" / "x")
    assert exists(Path(used["cwd"]) / "build" / "out.o"), "still in use"


def test_ignored_files_are_sorted_by_whether_a_build_makes_them_again():
    assert ignored_kind("build/") == "build"
    assert ignored_kind("web/node_modules/") == "build"
    assert ignored_kind("app/cmake-build-debug/") == "build"
    assert ignored_kind("pkg.egg-info/") == "build"
    assert ignored_kind(".dart_tool/") == "build"
    assert ignored_kind("logs/") == "" and ignored_kind("server.log") == ""
    assert ignored_kind(".env") == "" and ignored_kind("data/models/") == ""
    assert ignored_kind(".idea/") == "editor"
    assert ignored_kind("generated/", extra=["generated"]) == "build"
    assert ignored_kind("build/", never=["build"]) == "never"
    assert ignored_kind("data/build/", never=["data/*"]) == "never"


async def test_cleaning_deletes_only_ignored_build_output(tmp_path):
    """Never a tracked file, a new file git does not ignore, an uncommitted change, a file it
    ignores that no build makes, or a repository inside."""
    app = project(tmp_path)
    tree = tmp_path / "tree"
    run("git", "worktree", "add", "-q", "-b", "chief/t", str(tree), cwd=app)
    write(tree / "build" / "a.o")
    write(tree / "build" / "sub" / "b.o")
    write(tree / "out" / "keep.txt", "not ignored: new work")
    write(tree / "out" / "o.bin")
    write(tree / "src" / "main.py", "print(2)\n")  # An uncommitted change.
    write(tree / "coverage" / "notes.md", "new, and not ignored: named like build output")
    write(tree / "logs" / "x")
    write(tree / "vendor" / "node_modules" / ".git" / "HEAD", "a repository of its own")
    outside = tmp_path / "elsewhere"
    write(outside / "precious.txt")
    (tree / "lib").mkdir()
    (tree / "lib" / "dist").symlink_to(outside)
    run("git", "-C", str(tree), "config", "core.excludesFile", "/dev/null")
    checkout = {"repo": str(app), "worktree": str(tree), "path": str(tree)}
    found = await sort_ignored(checkout, str(tree))
    assert found is not None
    cleanable, kept = found
    assert not {"out/keep.txt", "coverage/", "coverage/notes.md"} & {*cleanable, *kept}
    assert "logs/" in kept and "out/o.bin" in kept, "ignored, and no build makes them"
    removed, _ = await clean_tree(str(tree), cleanable, tmp_path / ".trash")
    assert not exists(tree / "build" / "a.o") and not exists(tree / "build" / "sub")
    assert exists(tree / "out" / "o.bin")
    assert exists(tree / "build" / "tracked.txt"), "tracked"
    assert exists(tree / "out" / "keep.txt") and exists(tree / "coverage" / "notes.md")
    assert (tree / "src" / "main.py").read_text() == "print(2)\n", "uncommitted change kept"
    assert exists(tree / "logs" / "x")
    assert exists(tree / "vendor" / "node_modules" / ".git" / "HEAD"), "a repository"
    assert exists(outside / "precious.txt"), "a link is removed, never followed"
    assert not (tree / "lib" / "dist").is_symlink() and "lib/dist" in removed


# Idle workers' build output


async def idle_worker(harness, app: Path, title: str, days: float = 4) -> dict:
    info = await spawn(harness.runtime, harness.router_id, title, project=str(app), isolate=True)
    await settle(harness.runtime)
    write(Path(info["cwd"]) / "build" / "out.o", "x" * 50_000)
    write(Path(info["cwd"]) / "logs" / "keep.txt")
    idle_for(harness, info["session_id"], days)
    await harness.runtime.storage.measure(harness.store.session(info["session_id"]))
    return info


def built(info: dict) -> bool:
    return exists(Path(info["cwd"]) / "build" / "out.o")


async def test_idle_cleanup_cleans_only_workers_idle_long_enough_with_nothing_running(
    tmp_path, monkeypatch
):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    idle = await idle_worker(harness, app, "Idle")
    recent = await idle_worker(harness, app, "Recent", days=2)
    kept = await idle_worker(harness, app, "Kept")
    open_task = await idle_worker(harness, app, "Open task")
    in_turn = await idle_worker(harness, app, "In a turn")
    command = await idle_worker(harness, app, "Command")
    terminal = await idle_worker(harness, app, "Terminal")
    used = await idle_worker(harness, app, "Used")
    held = await idle_worker(harness, app, "Held")
    helper = await idle_worker(harness, app, "Helper")
    runtime.states[held["session_id"]].held = True  # A dev server holds its session.
    harness.store.set(f"checker:{helper['session_id']}", "1")  # Chief of Staff's own.
    runtime.storage.keep(kept["session_id"], True)
    harness.store.execute(
        "UPDATE tasks SET status='waiting' WHERE session_id=?", (open_task["session_id"],)
    )
    runtime.states[in_turn["session_id"]].busy = True
    harness.store.execute(
        "INSERT INTO commands(session_id, call_id, command, started_at) VALUES (?, 'c1', "
        "'npm run dev', ?)",
        (command["session_id"], now()),
    )
    runtime.elsewhere = {runtime.copilot_id(terminal["session_id"]): 4242}

    in_web = str(resolved(used["cwd"]) / "web")

    async def dev_server(limit: float = 30):
        return {in_web}

    monkeypatch.setattr(machine, "process_cwds", dev_server)
    await runtime.storage.cleanup()
    await settle(runtime)

    assert not built(idle) and exists(Path(idle["cwd"]) / "logs" / "keep.txt")
    assert exists(idle["cwd"]), "the worktree stays: only its build output goes"
    for info in (recent, kept, open_task, in_turn, command, terminal, used, held, helper):
        assert built(info), info["cwd"]
    history = json.loads(harness.store.get("storage:history"))
    assert [(e["session_id"], e["by"]) for e in history] == [(idle["session_id"], "automatic")]

    harness.store.save_settings(
        harness.store.settings().model_copy(update={"cleanup_idle_days": 0})
    )
    idle_for(harness, recent["session_id"], 40)
    await runtime.storage.cleanup()
    assert built(recent), "0 days: never automatically"


async def test_programs_that_cannot_be_listed_keep_every_folder(tmp_path, monkeypatch):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    idle = await idle_worker(harness, app, "Idle")

    async def unknown(limit: float = 30):
        return None

    monkeypatch.setattr(machine, "process_cwds", unknown)
    await harness.runtime.storage.cleanup()
    assert built(idle)


async def test_a_worker_that_starts_a_turn_stops_its_cleanup(tmp_path, monkeypatch):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    idle = await idle_worker(harness, app, "Idle")
    write(Path(idle["cwd"]) / "node_modules" / "x.js")
    removed = []
    move = storage.clean_tree.__globals__["move_ignored"]

    def move_then_start(tree: str, entry: str, trash: Path):
        removed.append(entry)
        runtime.states[idle["session_id"]].busy = True  # A message started a turn meanwhile.
        return move(tree, entry, trash)

    monkeypatch.setitem(storage.clean_tree.__globals__, "move_ignored", move_then_start)
    await runtime.storage.cleanup()
    assert len(removed) == 1, "nothing more once it is in a turn"


async def test_two_tasks_in_one_worker_or_shared_folders_never_lead_to_unsafe_cleanup(
    tmp_path, monkeypatch
):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    worker = await idle_worker(harness, app, "Two tasks")
    # A second task for the same worker (and folder), still to do: its build output is in use.
    runtime.tasks.create(worker["session_id"], "Second", "more")
    await runtime.storage.cleanup()
    assert built(worker), "an open task in the same folder"
    harness.store.execute("UPDATE sessions SET status='queued' WHERE id=?",
                          (worker["session_id"],))  # fmt: skip
    with pytest.raises(ValueError, match="about to start"):
        await runtime.storage.clean_now(worker["session_id"])

    other = await idle_worker(harness, app, "Other")
    # Another open worker given this folder too (as an extra checkout): it is not idle.
    repos = json.loads(harness.store.session(other["session_id"])["repos"])
    sharer = await spawn(runtime, harness.router_id, "Sharer")
    await settle(runtime)
    harness.store.update_session(
        sharer["session_id"], repos=json.dumps([*repos, {"path": repos[0]["worktree"]}])
    )
    harness.store.execute("UPDATE sessions SET status='working' WHERE id=?",
                          (sharer["session_id"],))  # fmt: skip
    await runtime.storage.cleanup()
    assert built(other), "another worker uses that folder"
    assert "uses" in runtime.storage.notes[other["session_id"]]


async def test_a_program_running_in_a_worktree_protects_it(tmp_path):
    """The real check: a process whose working directory is in the folder (a dev server)."""
    folder = tmp_path / "tree" / "web"
    folder.mkdir(parents=True)
    process = await asyncio.create_subprocess_exec("sleep", "30", cwd=folder)
    try:
        cwds = await REAL_PROCESS_CWDS()
        if cwds is None:
            pytest.skip("this machine cannot list programs' folders")
        assert machine.in_use(str(tmp_path / "tree"), cwds)
        assert not machine.in_use(str(tmp_path / "elsewhere"), cwds)
    finally:
        process.kill()
        await process.wait()


async def test_closed_worktrees_an_earlier_version_kept_whole_are_cleaned_once(
    tmp_path, monkeypatch
):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    idle = await idle_worker(harness, app, "Idle")
    looked = []

    async def cwds(limit: float = 30):
        looked.append(1)
        return set()

    monkeypatch.setattr(machine, "process_cwds", cwds)
    old = await spawn(runtime, harness.router_id, "Old", project=str(app), isolate=True)
    logs = await spawn(runtime, harness.router_id, "Logs", project=str(app), isolate=True)
    await settle(runtime)
    for info in (old, logs):
        write(Path(info["cwd"]) / ".dart_tool" / "cache.bin")  # Unknown to earlier versions.
        write(Path(info["cwd"]) / "build" / "out.o")
        # Closed by an earlier version: nothing recorded, the worktree kept whole.
        harness.store.update_session(info["session_id"], closed_at=now())
    write(Path(logs["cwd"]) / "logs" / "x")
    await runtime.storage.cleanup()
    await settle(runtime)
    assert not exists(old["cwd"]), "nothing in it a build does not make again"
    assert exists(Path(logs["cwd"]) / "logs" / "x") and not built(logs)
    assert not built(idle)
    assert len(looked) == 1, "which folders programs run in: read once for the pass"
    assert not await runtime.storage.unreleased(), "once"


# Measuring, the report and the routes


async def test_the_periodic_job_returns_at_once_and_measures_only_what_was_active(
    tmp_path, monkeypatch
):
    harness = Harness(tmp_path / "data")
    runtime = harness.runtime
    root = runtime.workspaces.scratch_root()
    ids = []
    for n in range(50):
        session_id = f"{n:08d}-aaaa-bbbb-cccc-dddddddddddd"
        harness.store.create_session(session_id, "worker", f"Worker {n}", "idle")
        (root / f"w{n}").mkdir(parents=True)
        harness.store.set(f"workspace:{session_id}", str(root / f"w{n}"))
        record = {"at": now(), "size": n * GB, "regenerable": GB, "trees": []}
        harness.store.set(f"storage:size:{session_id}", json.dumps(record))
        ids.append(session_id)
    runs = []

    async def du(paths, limit=600, depth=0):
        runs.append(paths)
        return dict.fromkeys(paths, GB)

    monkeypatch.setattr(machine, "disk_used", du)
    disk(monkeypatch, 100)
    started = time.perf_counter()
    await runtime.storage.tick()
    took = time.perf_counter() - started
    await settle(runtime)
    assert took < 0.05, f"the periodic job took {took * 1000:.0f} ms"
    assert runs == [], "nothing was active: no du"
    started = time.perf_counter()
    report = runtime.storage.report()
    assert time.perf_counter() - started < 0.05 and len(report["workers"]) == 50
    assert report["workers"][0]["title"] == "Worker 49", "the largest first"
    assert report["recoverable"] == 50 * GB

    # One worked since it was measured, over half an hour ago: it alone is measured again.
    old = datetime.fromtimestamp(time.time() - 3600, UTC).isoformat()
    record = {"at": old, "size": 0, "regenerable": 0, "trees": []}
    harness.store.set(f"storage:size:{ids[0]}", json.dumps(record))
    runtime.storage.sizes = None
    harness.store.update_session(ids[0], status="idle")
    await runtime.storage.tick()
    await settle(runtime)
    assert runs == [[str((root / "w0").resolve())]]
    runs.clear()
    harness.store.update_session(ids[1], status="idle")
    await runtime.storage.tick()
    await settle(runtime)
    assert runs == [], "measured less than half an hour ago"
    runtime.storage.measure_now()
    await settle(runtime)
    assert len(runs) == 50, "the owner asked: every folder"


async def test_the_report_shows_what_stays_what_can_be_cleaned_and_what_was(tmp_path, monkeypatch):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    idle = await idle_worker(harness, app, "Idle")
    closed = await spawn(runtime, harness.router_id, "Closed", project=str(app), isolate=True)
    await settle(runtime)
    write(Path(closed["cwd"]) / "logs" / "big.txt", "y" * 300_000)
    await runtime.workers.close(closed["session_id"])
    await settle(runtime)
    await runtime.storage.measure(harness.store.session(closed["session_id"]))

    workers = {w["session_id"]: w for w in runtime.storage.report()["workers"]}
    assert workers[closed["session_id"]]["state"] == "closed"
    assert workers[closed["session_id"]]["kept"] == ["app: ignored files (logs/)"]
    assert workers[closed["session_id"]]["size"] >= 300_000
    files = await runtime.storage.files(closed["session_id"])
    assert files["trees"][0]["kept"][0]["name"] == "logs/"
    assert files["trees"][0]["kept"][0]["size"] >= 300_000, "measured in the background"
    before = workers[idle["session_id"]]
    assert before["state"] == "idle" and before["idle_days"] == 4
    assert before["regenerable"] >= 50_000
    assert runtime.storage.report()["recoverable"] == before["regenerable"]

    harness.store.save_settings(harness.store.settings().model_copy(update={"auto_cleanup": False}))
    disk(monkeypatch, 25)
    await runtime.storage.check_disk()
    await settle(runtime)
    note = harness.store.messages(harness.router_id)[-1]["text"]
    assert "“Closed”" in note and "can be cleaned safely" in note, note

    async with runtime.bus.subscribe() as events:
        await runtime.storage.clean_now(idle["session_id"])
        await settle(runtime)
        sent = []
        while not events.empty():
            sent.append(json.loads(events.get_nowait()))
    after = {w["session_id"]: w for w in runtime.storage.report()["workers"]}[idle["session_id"]]
    assert after["regenerable"] == 0 and after["size"] <= before["size"] - 50_000
    assert "freed" in after["note"] and not built(idle)
    history = json.loads(harness.store.get("storage:history"))[0]
    assert history["what"] == "Cleaned build artifacts" and history["by"] == "you"
    storage_events = [e for e in sent if e["type"] == "storage"]
    assert storage_events and storage_events[-1]["data"]["recoverable"] == 0
    async with runtime.bus.subscribe() as events:
        runtime.storage.publish()
        assert events.empty(), "nothing changed: nothing sent"


async def test_a_closed_workers_worktree_is_deleted_only_when_confirmed(tmp_path):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    closed = await spawn(runtime, harness.router_id, "Closed", project=str(app), isolate=True)
    running = await spawn(runtime, harness.router_id, "Open", project=str(app), isolate=True)
    await settle(runtime)
    tree = Path(closed["cwd"])
    write(tree / "logs" / "x")
    write(tree / "src" / "main.py", "changed\n")
    await runtime.workers.close(closed["session_id"])
    await settle(runtime)
    files = await runtime.storage.files(closed["session_id"])
    assert [c["name"] for c in files["trees"][0]["changes"]] == ["src/main.py"]
    delete = runtime.storage.delete_worktree
    with pytest.raises(ValueError, match="Confirm"):
        await delete(closed["session_id"], str(tree), False)
    with pytest.raises(ValueError, match="closed worker"):
        await delete(running["session_id"], running["cwd"], True)
    with pytest.raises(ValueError, match="Not one of its worktrees"):
        await delete(closed["session_id"], str(app), True)
    with pytest.raises(ValueError, match="Not one of its worktrees"):
        await delete(closed["session_id"], running["cwd"], True)
    assert exists(tree) and exists(running["cwd"]) and exists(app / "src" / "main.py")
    seen = files["trees"][0]["digest"]
    write(tree / "src" / "late.py", "written after the owner looked")
    with pytest.raises(ValueError, match="changed since you reviewed"):
        await delete(closed["session_id"], str(tree), True, seen)
    assert exists(tree / "src" / "late.py")

    seen = (await runtime.storage.files(closed["session_id"]))["trees"][0]["digest"]
    await delete(closed["session_id"], str(tree), True, seen)
    assert not exists(tree)
    assert closed["branch"] in run("git", "branch", "--format=%(refname:short)", cwd=app)
    assert json.loads(harness.store.get("storage:history"))[0]["what"] == "Deleted its worktree"
    await runtime.workers.reopen(closed["session_id"])
    await settle(runtime)
    assert exists(tree / "src" / "main.py"), "reopened from its branch"


def test_the_routes_serve_the_report_and_never_touch_the_uv_cache(tmp_path, monkeypatch):
    from conftest import FakeClient, FakeSlackMcp, FakeTokens

    from chiefofstaff.sources.slack import SlackSource

    cache = tmp_path / "uv-cache"
    write(cache / "archive-v0" / "pkg.whl", "z" * 20_000)
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    disk(monkeypatch, 100)

    def factory(store, bus):
        async def notifier(title, text):
            pass

        client = FakeClient()
        runtime = Runtime(store, bus, backend_factory=lambda settings: client, notifier=notifier)
        runtime.slack = SlackSource(store, runtime, tokens=FakeTokens(""), mcp=FakeSlackMcp())
        return runtime

    app = create_app(tmp_path / "data", "token", 8765, runtime_factory=factory)
    with TestClient(app, base_url="http://127.0.0.1:8765") as http:
        http.headers.update({"X-Chief-Key": "token", "origin": "http://127.0.0.1:8765"})
        report = http.get("/api/storage").json()
        assert report["disk"]["level"] == "normal" and report["disk"]["free"] == 100 * GB
        assert report["uv"]["path"] == str(cache) and report["uv"]["size"] is None
        assert "storage" not in http.get("/api/state").json(), "never in the page's state"
        router = http.get("/api/state").json()["router_id"]
        body = {"session_id": router, "path": str(cache), "confirm": True}
        refused = http.post("/api/storage/delete-worktree", json=body)
        assert refused.status_code == 400
        assert (
            http.post(
                "/api/storage/keep", json={"session_id": "nope0000", "keep": True}
            ).status_code
            == 404
        )
        assert http.post("/api/storage/clean", json={"session_id": router}).status_code == 400
        http.post("/api/storage/uv-measure")
        for _ in range(100):
            uv = http.get("/api/storage").json()["uv"]
            if uv["size"] is not None:
                break
            time.sleep(0.05)
        assert uv["size"] >= 20_000 and uv["path"] == str(cache)
    assert (cache / "archive-v0" / "pkg.whl").exists(), "measured, never deleted"


async def test_closing_leaves_a_worktree_alone_while_open_in_a_terminal_or_kept(tmp_path):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    terminal = await spawn(runtime, harness.router_id, "Terminal", project=str(app), isolate=True)
    kept = await spawn(runtime, harness.router_id, "Kept", project=str(app), isolate=True)
    await settle(runtime)
    for info in (terminal, kept):
        write(Path(info["cwd"]) / "build" / "out.o")
    runtime.elsewhere = {runtime.copilot_id(terminal["session_id"]): 4242}
    runtime.storage.keep(kept["session_id"], True)
    for info in (terminal, kept):
        await runtime.workers.close(info["session_id"])
    await settle(runtime)
    assert built(terminal) and built(kept), "nothing removed, nothing cleaned"
    workers = {w["session_id"]: w for w in runtime.storage.report()["workers"]}
    assert workers[terminal["session_id"]]["kept"] == ["app: it is open in a terminal"]
    assert [row["id"] for row in await runtime.storage.unreleased()] == [terminal["session_id"]]
    runtime.elsewhere = {}
    await runtime.storage.cleanup()
    await settle(runtime)
    assert not exists(terminal["cwd"]), "closed there: released as usual"
    assert built(kept)


def test_nothing_outside_a_worktree_or_chief_of_staffs_folders_is_ever_deleted(tmp_path):
    from chiefofstaff.workspaces import move_ignored

    tree, outside = tmp_path / "tree", tmp_path / "outside"
    write(outside / "build" / "precious.txt")
    tree.mkdir()
    (tree / "link").symlink_to(outside)
    trash = tmp_path / ".trash"
    assert not move_ignored(str(tree), "link/build/", trash), "through a link that leads out"
    assert not move_ignored(str(tree), "../outside/build/", trash)
    assert not move_ignored(str(tree), "", trash)
    assert exists(outside / "build" / "precious.txt") and exists(tree)

    harness = Harness(tmp_path / "data")
    workspaces = harness.runtime.workspaces
    root = workspaces.scratch_root()
    (root / "fix-login-abc123").mkdir(parents=True)
    harness.store.create_session("abc12345-0000", "worker", "Fix login", "idle")
    row = harness.store.session("abc12345-0000")
    harness.store.set("workspace:abc12345-0000", str(root / "fix-login-abc123"))
    assert workspaces.own_folder(row) == (root / "fix-login-abc123").resolve()
    for elsewhere in (outside, root, tmp_path / "data"):
        harness.store.set("workspace:abc12345-0000", str(elsewhere))
        assert workspaces.own_folder(row) is None, elsewhere


async def test_programs_folders_count_as_unknown_when_lsof_fails(monkeypatch):
    monkeypatch.setattr(machine.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(machine.sys, "platform", "darwin")

    async def failed(arguments, limit):
        return 1, "p1\nfcwd\nn/somewhere\n"

    monkeypatch.setattr(machine, "run_quietly", failed)
    assert await REAL_PROCESS_CWDS() is None, "then every folder may be in use"

    async def listed(arguments, limit):
        assert arguments == ["lsof", "-w", "-d", "cwd", "-Fn"]
        return 0, "p1\nfcwd\nn/work/tree\np2\nfcwd\nn/\n"

    monkeypatch.setattr(machine, "run_quietly", listed)
    assert await REAL_PROCESS_CWDS() == {"/work/tree", "/"}


async def test_closing_cleans_nothing_in_place_outside_chief_of_staffs_folders(tmp_path):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    info = await spawn(runtime, harness.router_id, "Moved", project=str(app), isolate=True)
    await settle(runtime)
    write(Path(info["cwd"]) / "logs" / "x")
    write(Path(info["cwd"]) / "build" / "out.o")
    # Its folder recorded somewhere else (an older layout): not one of Chief of Staff's.
    harness.store.set(f"workspace:{info['session_id']}", str(tmp_path / "elsewhere"))
    await runtime.workers.close(info["session_id"])
    await settle(runtime)
    assert built(info) and exists(Path(info["cwd"]) / "logs" / "x")


# The review's findings


async def test_never_clean_patterns_protect_what_is_inside_a_build_folder(tmp_path):
    from chiefofstaff.workspaces import holding_never

    app = project(tmp_path)
    harness = await harnessed(tmp_path, never_clean_patterns=["models"])
    runtime = harness.runtime
    idle = await idle_worker(harness, app, "Idle")
    weights = Path(idle["cwd"]) / ".cache" / "models" / "weights.bin"
    write(weights, "trained for a week")
    write(Path(idle["cwd"]) / ".cache" / "pip" / "wheel.whl")
    closed = await spawn(runtime, harness.router_id, "Closed", project=str(app), isolate=True)
    await settle(runtime)
    kept = Path(closed["cwd"]) / ".cache" / "models" / "w.bin"
    write(kept, "also precious")  # Its only ignored file: git lists just ".cache/".
    write(Path(closed["cwd"]) / "build" / "out.o")

    await runtime.storage.cleanup()
    await runtime.workers.close(closed["session_id"])
    await settle(runtime)
    assert weights.read_text() == "trained for a week", "never cleaned, though in .cache/"
    assert not built(idle), "the rest of its build output went"
    assert kept.exists(), "the worktree stays for it: git would delete it with the worktree"
    assert not built(closed)
    record = json.loads(harness.store.get(f"storage:kept:{closed['session_id']}"))
    assert record["trees"][0]["items"] == [".cache/"]
    # Too big to look through: kept whole.
    write(tmp_path / "big" / "a" / "b" / "c")
    assert holding_never(str(tmp_path), ["big/"], ["nothing"], limit=2) == {"big/"}
    assert holding_never(str(tmp_path), ["big/"], ["nothing"]) == set()


async def test_closing_never_removes_a_worktree_a_program_or_another_worker_uses(
    tmp_path, monkeypatch
):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    served = await spawn(runtime, harness.router_id, "Served", project=str(app), isolate=True)
    shared = await spawn(runtime, harness.router_id, "Shared", project=str(app), isolate=True)
    unknown = await spawn(runtime, harness.router_id, "Unknown", project=str(app), isolate=True)
    sharer = await spawn(runtime, harness.router_id, "Sharer")
    await settle(runtime)
    # Another open worker given the Shared worktree as a checkout of its own.
    harness.store.update_session(
        sharer["session_id"], repos=json.dumps([{"path": shared["cwd"], "shared": True}])
    )
    in_web = str(resolved(served["cwd"]) / "web")  # `npm run dev` there, after closing.
    readable = True

    async def cwds(limit: float = 30):
        return {in_web} if readable else None

    monkeypatch.setattr(machine, "process_cwds", cwds)
    for info in (served, shared):
        await runtime.workers.close(info["session_id"])
    await settle(runtime)
    readable = False
    await runtime.workers.close(unknown["session_id"])
    await settle(runtime)
    for info in (served, shared, unknown):
        assert exists(Path(info["cwd"]) / "src" / "main.py"), info["cwd"]
    workers = {w["session_id"]: w for w in runtime.storage.report()["workers"]}
    assert workers[served["session_id"]]["kept"] == ["app: a program runs in it"]
    assert workers[shared["session_id"]]["kept"] == ["app: “Sharer” uses it"]
    assert workers[unknown["session_id"]]["kept"] == [
        "app: which programs run in it could not be read"
    ]
    readable = True
    await runtime.storage.cleanup()  # The hourly pass: still running there.
    await settle(runtime)
    assert exists(served["cwd"]) and exists(shared["cwd"]) and not exists(unknown["cwd"])

    in_web = "/elsewhere"
    await runtime.workers.close(sharer["session_id"])
    await runtime.storage.cleanup()
    await settle(runtime)
    assert not exists(served["cwd"]) and not exists(shared["cwd"]), "once nothing uses them"
    await runtime.workers.reopen(served["session_id"])
    await settle(runtime)
    assert exists(Path(served["cwd"]) / "src" / "main.py"), "reopened from its branch"


async def test_a_folder_being_deleted_is_whole_or_gone_and_leftovers_are_counted(
    tmp_path, monkeypatch
):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    idle = await idle_worker(harness, app, "Idle")
    tree = resolved(idle["cwd"])
    write(tree / "node_modules" / "lib" / "index.js")
    write(tree / ".pytest_cache" / "v" / "lastfailed")
    trash = resolved(runtime.workspaces.scratch_root()) / ".chiefofstaff-trash"
    deleted, rmtree = [], workspaces_module.shutil.rmtree

    def slow_rmtree(path, *args, **kwargs):
        path = Path(path)
        # Deleted from trash only, never in place: the worktree has it whole, or not at all.
        assert path.parent == trash, path
        # All of them out of the worktree before any is deleted.
        assert not any((tree / name).exists() for name in ("node_modules", ".pytest_cache"))
        assert not (tree / "build" / "out.o").exists()
        deleted.append(path.name.partition("-")[2])
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(workspaces_module.shutil, "rmtree", slow_rmtree)
    await runtime.storage.cleanup()
    assert deleted == [".pytest_cache", "node_modules"], "folders: moved out, then deleted"
    status = run("git", "status", "--porcelain", "--ignored", cwd=tree)
    assert ".trash" not in status and not trash.is_relative_to(tree), "never in a worktree"
    monkeypatch.setattr(workspaces_module.shutil, "rmtree", rmtree)

    # Chief of Staff stopped while deleting: what is left goes on the next pass, counted.
    write(trash / "0123456789ab-node_modules" / "big.js", "z" * 40_000)
    await runtime.storage.cleanup()
    assert not (trash / "0123456789ab-node_modules").exists()
    entry = json.loads(harness.store.get("storage:history"))[0]
    assert entry["what"] == "Deleted what an earlier cleanup left" and entry["freed"] >= 40_000
    runtime.storage.measure_all = True
    await runtime.storage.measure_others()
    assert not runtime.storage.report()["others"], "trash is no worker's folder to list"


async def test_a_worktree_whose_changes_cannot_be_read_is_never_deleted(tmp_path, monkeypatch):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    first = await spawn(runtime, harness.router_id, "First", project=str(app), isolate=True)
    second = await spawn(runtime, harness.router_id, "Second", project=str(app), isolate=True)
    await settle(runtime)
    write(Path(first["cwd"]) / "logs" / "x")  # Kept on close, for the owner to look at.
    await runtime.workers.close(first["session_id"])
    await settle(runtime)
    real_git = workspaces_module.checkout_git

    async def broken_status(checkout, path, *arguments, **options):
        if "status" in arguments:
            raise ValueError("git failed: index.lock exists")
        return await real_git(checkout, path, *arguments, **options)

    monkeypatch.setattr(workspaces_module, "checkout_git", broken_status)
    files = await runtime.storage.files(first["session_id"])
    assert files["trees"][0]["changes_unread"] and files["trees"][0]["changes"] == []
    with pytest.raises(ValueError, match="could not read"):
        await runtime.storage.delete_worktree(first["session_id"], first["cwd"], True)
    assert exists(Path(first["cwd"]) / "src" / "main.py")

    async def broken_listing(checkout, path, *arguments, **options):
        if "ls-files" in arguments:
            raise ValueError("git failed: bad index")
        return await real_git(checkout, path, *arguments, **options)

    monkeypatch.setattr(workspaces_module, "checkout_git", broken_listing)
    assert (await runtime.storage.files(first["session_id"]))["trees"][0]["unlisted"]
    with pytest.raises(ValueError, match="could not read"):
        await runtime.storage.delete_worktree(first["session_id"], first["cwd"], True)
    assert exists(Path(first["cwd"]) / "logs" / "x")
    monkeypatch.setattr(workspaces_module, "checkout_git", broken_status)
    await runtime.workers.close(second["session_id"])
    await settle(runtime)
    assert exists(second["cwd"]), "not taken for clean"
    workers = {w["session_id"]: w for w in runtime.storage.report()["workers"]}
    assert "app: its changes could not be read" in workers[second["session_id"]]["kept"]


async def test_git_stopped_halfway_through_removing_a_worktree_is_finished(tmp_path, monkeypatch):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    closing = await spawn(runtime, harness.router_id, "Closing", project=str(app), isolate=True)
    deleting = await spawn(runtime, harness.router_id, "Deleting", project=str(app), isolate=True)
    await settle(runtime)
    write(Path(deleting["cwd"]) / "logs" / "x")
    write(Path(closing["cwd"]) / "node_modules" / "dep" / "index.js")
    await runtime.workers.close(deleting["session_id"])
    await settle(runtime)
    seen = (await runtime.storage.files(deleting["session_id"]))["trees"][0]["digest"]
    real_git = workspaces_module.checkout_git

    async def slow_removal(checkout, path, *arguments, **options):
        if "remove" in arguments:
            tree = Path(arguments[-1])
            # Its build output went first, without git's time limit.
            assert not (tree / "node_modules").exists()
            (tree / "src" / "main.py").unlink(missing_ok=True)  # Half done, then stopped.
            raise TimedOutError("git timed out.")
        return await real_git(checkout, path, *arguments, **options)

    monkeypatch.setattr(workspaces_module, "checkout_git", slow_removal)
    await runtime.workers.close(closing["session_id"])
    await settle(runtime)
    await runtime.storage.delete_worktree(deleting["session_id"], deleting["cwd"], True, seen)
    for info in (closing, deleting):
        assert not exists(info["cwd"]), "finished, not left half deleted"
        assert info["cwd"] not in run("git", "worktree", "list", cwd=app), "git forgot it"
        assert info["branch"] in run("git", "branch", "--format=%(refname:short)", cwd=app)
    monkeypatch.setattr(workspaces_module, "checkout_git", real_git)
    await runtime.workers.reopen(closing["session_id"])
    await settle(runtime)
    assert (Path(closing["cwd"]) / "src" / "main.py").read_text() == "print(1)\n", "whole"


async def test_a_worker_still_following_its_task_loses_only_tools_caches(tmp_path, monkeypatch):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    following = await idle_worker(harness, app, "Following")
    tree = Path(following["cwd"])
    write(tree / "node_modules" / "x.js")
    write(tree / ".pytest_cache" / "v" / "lastfailed")
    write(tree / "src" / "__pycache__" / "main.pyc")
    harness.store.execute("UPDATE tasks SET status='done' WHERE session_id=?",
                          (following["session_id"],))  # fmt: skip
    task = runtime.tasks.for_session(following["session_id"])[0]

    def monitoring(task_row, prs=None, follows=None):
        return [{"label": "acme/app#7"}] if task_row["id"] == task["id"] else []

    monkeypatch.setattr(runtime.task_actions, "monitoring", monitoring)
    await runtime.storage.cleanup()
    assert built(following) and exists(tree / "node_modules" / "x.js"), "it may need them"
    assert not exists(tree / ".pytest_cache") and not exists(tree / "src" / "__pycache__")
    workers = {w["session_id"]: w for w in runtime.storage.report()["workers"]}
    assert workers[following["session_id"]]["regenerable"] > 0, "what is left still counts"


def test_a_folder_on_another_disk_than_trash_is_left_whole(tmp_path, monkeypatch):
    """Moving it there would copy it piece by piece: a turn could find it half moved."""
    import os

    from chiefofstaff.workspaces import to_trash

    folder, trash = tmp_path / "tree" / "node_modules", tmp_path / ".trash"
    write(folder / "x.js")
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        found = real_stat(path, *args, **kwargs)
        if Path(path) == trash:
            fields = list(found)[:10]
            fields[2] += 1  # st_dev: another disk.
            return os.stat_result(fields)
        return found

    monkeypatch.setattr(os, "stat", stat)
    assert to_trash(folder, trash) is None and (folder / "x.js").exists()


async def test_new_files_hidden_by_git_config_are_never_deleted(tmp_path, monkeypatch):
    """status.showUntrackedFiles=no hides a worker's new, never committed source files from a
    plain `git status`, and from git's own check before it removes a worktree."""
    app = project(tmp_path)
    run("git", "config", "status.showUntrackedFiles", "no", cwd=app)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    closed = await spawn(runtime, harness.router_id, "Closed", project=str(app), isolate=True)
    raced = await spawn(runtime, harness.router_id, "Raced", project=str(app), isolate=True)
    await settle(runtime)
    for info in (closed, raced):
        write(Path(info["cwd"]) / "src" / "feature.py", "never committed\n")
    await runtime.workers.close(closed["session_id"])
    await settle(runtime)
    assert exists(Path(closed["cwd"]) / "src" / "feature.py"), "kept: a change of its own"
    files = await runtime.storage.files(closed["session_id"])
    assert [c["name"] for c in files["trees"][0]["changes"]] == ["src/feature.py"]

    # Found clean a moment before it appeared: git's own check sees it too.
    async def clean(checkout, tree):
        return []

    monkeypatch.setattr(workspaces_module, "uncommitted", clean)
    await runtime.workers.close(raced["session_id"])
    await settle(runtime)
    assert exists(Path(raced["cwd"]) / "src" / "feature.py"), "git refused to remove it"


async def test_a_half_removed_worktree_with_a_new_file_is_not_forced(tmp_path, monkeypatch):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    info = await spawn(runtime, harness.router_id, "Closing", project=str(app), isolate=True)
    await settle(runtime)
    real_git = workspaces_module.checkout_git

    async def stopped(checkout, path, *arguments, **options):
        if "remove" in arguments:
            tree = Path(arguments[-1])
            write(tree / "src" / "appeared.py", "written while git was removing")
            raise TimedOutError("git timed out.")
        return await real_git(checkout, path, *arguments, **options)

    monkeypatch.setattr(workspaces_module, "checkout_git", stopped)
    await runtime.workers.close(info["session_id"])
    await settle(runtime)
    assert exists(Path(info["cwd"]) / "src" / "appeared.py"), "not finished with --force"


async def test_a_submodules_own_commits_show_whatever_git_config_ignores(tmp_path):
    app = project(tmp_path)
    lib = tmp_path / "lib"
    run("git", "init", "-q", "-b", "main", str(lib))
    write(lib / "a.txt")
    run("git", "add", ".", cwd=lib)
    run(*GIT, "commit", "-qm", "a", cwd=lib)
    allow = ("-c", "protocol.file.allow=always")
    run("git", *allow, "submodule", "add", "-q", str(lib), "lib", cwd=app)
    run(*GIT, "commit", "-qm", "lib", cwd=app)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    info = await spawn(runtime, harness.router_id, "Sub", project=str(app), isolate=True)
    await settle(runtime)
    tree = Path(info["cwd"])
    run("git", *allow, "submodule", "update", "--init", "-q", cwd=tree)
    write(tree / "lib" / "b.txt", "a commit only in this submodule")
    run("git", "add", ".", cwd=tree / "lib")
    run(*GIT, "commit", "-qm", "b", cwd=tree / "lib")
    run("git", "config", "diff.ignoreSubmodules", "all", cwd=app)
    await runtime.workers.close(info["session_id"])
    await settle(runtime)
    files = await runtime.storage.files(info["session_id"])
    assert [c["name"] for c in files["trees"][0]["changes"]] == ["lib"]


async def test_what_cannot_be_deleted_is_counted_once_and_shown(tmp_path, monkeypatch):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    idle = await idle_worker(harness, app, "Idle")
    tree = resolved(idle["cwd"])
    write(tree / "node_modules" / "mod" / "x.go", "z" * 30_000)
    (tree / "node_modules" / "mod").chmod(0o555)  # Read-only, as Go's module cache is.
    write(tree / ".pytest_cache" / "v" / "cache", "y" * 20_000)  # Say a container wrote it.
    before = await machine.disk_used(
        [str(tree / name) for name in ("build/out.o", "node_modules", ".pytest_cache")]
    )
    real_rmtree = workspaces_module.shutil.rmtree
    tries = []

    def rmtree(path, *args, **kwargs):
        if not Path(path).name.endswith("-.pytest_cache"):  # Else another user's: it stays.
            real_rmtree(path, *args, **kwargs)
        else:
            tries.append(path)

    monkeypatch.setattr(workspaces_module.shutil, "rmtree", rmtree)
    await runtime.storage.cleanup()
    assert not (tree / "node_modules").exists() and not (tree / ".pytest_cache").exists()
    trash = resolved(runtime.workspaces.scratch_root()) / ".chiefofstaff-trash"
    assert [item.name.partition("-")[2] for item in trash.iterdir()] == [".pytest_cache"]
    cleaned = json.loads(harness.store.get("storage:history"))[0]
    assert (
        cleaned["freed"] == before[str(tree / "build/out.o")] + before[str(tree / "node_modules")]
    ), "only what went"
    for _ in range(3):  # Hourly passes.
        runtime.storage.cleaned_at = float("-inf")
        await runtime.storage.cleanup()
    history = json.loads(harness.store.get("storage:history"))
    assert history == [cleaned], "not deleted again and again"
    assert len(tries) == 2, "tried when moved, then once a day, not every hour"
    stuck = runtime.storage.report()["stuck"]
    assert [(Path(s["path"]).name.partition("-")[2], s["size"]) for s in stuck] == [
        (".pytest_cache", before[str(tree / ".pytest_cache")])
    ]
    assert runtime.storage.report()["total"] >= stuck[0]["size"], "still counted"

    # The owner fixed its owner; a day later it goes, counted then.
    monkeypatch.setattr(workspaces_module.shutil, "rmtree", real_rmtree)
    record = json.loads(harness.store.get("storage:stuck"))
    harness.store.set(
        "storage:stuck",
        json.dumps(
            {path: {**value, "tried": value["tried"] - 86400} for path, value in record.items()}
        ),
    )
    await runtime.storage.cleanup()
    assert not list(trash.iterdir()) and not runtime.storage.report()["stuck"]
    entry = json.loads(harness.store.get("storage:history"))[0]
    assert entry["what"] == "Deleted what an earlier cleanup left"
    assert entry["freed"] == stuck[0]["size"]


async def test_only_chief_of_staffs_own_trash_is_emptied_and_never_twice(tmp_path):
    from chiefofstaff.workspaces import delete_moved, leftovers, to_trash

    harness = await harnessed(tmp_path, auto_cleanup=False)
    runtime = harness.runtime
    root = resolved(runtime.workspaces.scratch_root())
    # The owner's own .trash in a folder of theirs, beside one an earlier version made there.
    write(root / ".trash" / "photos" / "a.jpg", "the owner's")
    write(root / ".trash" / "notes.txt", "the owner's")
    write(root / ".trash" / "0123456789ab-node_modules" / "x.js", "z" * 10_000)
    trash = root / ".chiefofstaff-trash"
    write(trash / "README", "not one to_trash made")
    folder = root / "worker" / "app" / "node_modules"
    write(folder / "y.js", "w" * 30_000)
    moved = to_trash(folder, trash)  # A cleanup running now: about to delete it.
    assert moved and leftovers(trash) == []
    await runtime.storage.cleanup()
    assert moved.exists(), "its own cleanup deletes it, and counts it"
    assert exists(root / ".trash" / "photos" / "a.jpg") and exists(root / ".trash" / "notes.txt")
    assert exists(trash / "README")
    assert not exists(root / ".trash" / "0123456789ab-node_modules"), "an earlier version's"
    entry = json.loads(harness.store.get("storage:history"))[0]
    assert entry["items"] == ["node_modules"] and 10_000 <= entry["freed"] < 30_000
    assert delete_moved(moved) and not moved.exists()
    await runtime.storage.cleanup()
    assert len(json.loads(harness.store.get("storage:history"))) == 1, "counted once"


async def test_a_cleanup_stopped_midway_leaves_what_it_moved_to_the_next_pass(
    tmp_path, monkeypatch
):
    from chiefofstaff.workspaces import leftovers

    tree, trash = tmp_path / "tree", tmp_path / ".chiefofstaff-trash"
    write(tree / "node_modules" / "x.js")

    def stopping(item):
        raise RuntimeError("Chief of Staff is stopping")

    monkeypatch.setitem(clean_tree.__globals__, "delete_moved", stopping)
    with pytest.raises(RuntimeError):
        await clean_tree(str(tree), ["node_modules/"], trash)
    assert [item.name.partition("-")[2] for item in leftovers(trash)] == ["node_modules"]


async def test_delete_goes_ahead_whatever_patterns_sort_what_was_reviewed(tmp_path):
    app = project(tmp_path)
    write(app / ".env", "KEY=1\n")  # Copied into new worktrees, unchanged there.
    harness = await harnessed(tmp_path, never_clean_patterns=[".env", ".vscode"])
    runtime = harness.runtime
    closed = await spawn(runtime, harness.router_id, "Closed", project=str(app), isolate=True)
    await settle(runtime)
    tree = Path(closed["cwd"])
    write(tree / ".vscode" / "settings.json", "{}")
    await runtime.workers.close(closed["session_id"])
    await settle(runtime)
    files = (await runtime.storage.files(closed["session_id"]))["trees"][0]
    assert [item["name"] for item in files["kept"]] == [".env", ".vscode/"], "never clean"
    write(tree / "src" / "late.py", "a real change after the review")
    with pytest.raises(ValueError, match="changed since you reviewed"):
        await runtime.storage.delete_worktree(
            closed["session_id"], str(tree), True, files["digest"]
        )
    files = (await runtime.storage.files(closed["session_id"]))["trees"][0]
    await runtime.storage.delete_worktree(closed["session_id"], str(tree), True, files["digest"])
    assert not exists(tree)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads anything")
async def test_an_unreadable_folder_of_the_owners_is_deleted_without_stopping(tmp_path):
    app = project(tmp_path)
    harness = await harnessed(tmp_path)
    runtime = harness.runtime
    idle = await idle_worker(harness, app, "Idle")
    locked = Path(idle["cwd"]) / "node_modules" / "locked"
    write(locked / "deep" / "x.js", "z" * 10_000)
    locked.chmod(0)  # Unreadable: rmtree cannot even open it.
    await runtime.storage.cleanup()
    trash = resolved(runtime.workspaces.scratch_root()) / ".chiefofstaff-trash"
    assert not exists(Path(idle["cwd"]) / "node_modules") and not list(trash.iterdir())
    assert not built(idle), "the rest of its cleanup went on"
    assert not runtime.storage.report()["stuck"]


def test_the_retry_handler_never_stops_a_whole_delete(tmp_path, monkeypatch):
    from chiefofstaff.workspaces import writable

    folder = tmp_path / "locked"
    folder.mkdir()

    def broken(path):
        raise TypeError("open() missing required argument 'flags'")

    monkeypatch.setitem(writable.__globals__, "rmtree_writable", broken)
    writable(os.open, str(folder), None)  # Logged; the rest of the delete goes on.
