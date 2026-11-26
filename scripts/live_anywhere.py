"""Live: Chief of Staff with Codex alone (docs/design/agents-anywhere.md 9 and 12.11): the owner's
own Codex CLI and sign-in (real), no Copilot and no Claude Code on the PATH, a first start in a
fresh data folder; and the demo with no agent's CLI at all. Each scenario passes or fails on its
own (a failure dumps the sessions and the shard goes on); the summary lists each, with the Codex
tokens the shard spent, and the exit code says whether all passed.

Shard X, Codex alone (one Chief of Staff, first started here):
X1  a first start: ready on Codex (Copilot off, Codex on and the default, the Router on Codex,
    said once in the chat); the usage, models and Other sessions endpoints answer, Copilot's
    models none
X2  a question the Router answers itself, on Codex: no worker started
X3  a task: a worker on Codex does it; its result comes back through the Router
X4  the Watchdog on a quiet worker: its own session, on Codex, reviews it
X5  the owner profile distilled: its helper runs on Codex, the card comes to the inbox
X6  distilled again: the helper is reused (the same session), on Codex again
X7  away: the messenger, on Codex, tells the owner through a command-line tool (asking first:
    Chief of Staff's policy answers)
X8  Slack: a DM reaches the Slack session, on Codex, whose reply waits as a draft (its reads are
    Chief of Staff's own tools: Slack's token in no Codex request)
X9  Watch for you: a scan completes on Codex
X10 a mistake learned (a failure case there before a restart): the checker runs on Codex

Shard D, the demo with no agent's CLI on the PATH:
D1  `chiefofstaff --demo`: ready, the Router answers, a worker starts (all scripted)

    nice -n 15 uv run python scripts/live_anywhere.py [X|D] [X1 X3 …]

Codex's model is its default, or LIVE_CODEX_MODEL's. The owner's Codex files are recorded before
and checked after (scripts/owner_files.py). Ports 21010-21013 (LIVE_ANYWHERE_PORT moves them).
Its folders are made by `tempfile` (set TMPDIR, or LIVE_AGENTS_ROOT, to keep them elsewhere).
"""

import asyncio
import contextlib
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(1, str(Path(__file__).parent.parent / "tests"))
import live_agents as la
import live_e2e
from agent_paths import without_agents
from fake_slack_mcp import Workspace, serve_in_thread
from live_e2e import SLACK_TOKEN, App, git, log, wait_for
from live_parity import sales, write

BASE_PORT = int(os.environ.get("LIVE_ANYWHERE_PORT", "21010"))
SHARDS = {"X": (BASE_PORT, BASE_PORT + 1), "D": (BASE_PORT + 2, BASE_PORT + 3)}
CODEX_MODEL = os.environ.get("LIVE_CODEX_MODEL", "")
OWNER_FILES = Path(__file__).parent / "owner_files.py"
FIRST_RUN = re.compile(r"^Chief of Staff runs on Codex \(uses your .+\)\. Change it in Settings")
DASHBOARD = "https://deploy.acme.dev/dashboard"
HELPER = "Distill owner profile"


class Ctx(la.Ctx):
    workspace: Workspace


def project_of(root: Path) -> tuple[Path, dict]:
    """The owner's project: a git repository with data/sales.csv (its rows, known)."""
    project = root / "project"
    project.mkdir()
    git(project, "init", "-q", "-b", "main")
    truth = sales(project / "data" / "sales.csv")
    write(project / "README.md", "Sales numbers.\n")
    git(project, "add", ".")
    git(project, "-c", "user.email=p@test", "-c", "user.name=p", "commit", "-qm", "init")
    return project, truth


def codex_only_path(root: Path) -> str:
    """The PATH with neither Copilot nor Claude Code on it: Codex alone."""
    hidden = frozenset({"copilot", "claude"})
    return without_agents(os.environ.get("PATH", ""), root / "path", hidden)


def no_agent_path(root: Path) -> str:
    return without_agents(
        os.environ.get("PATH", ""), root / "path", frozenset({"copilot", "claude", "codex"})
    )


def owner_files(action: str, record: Path) -> str:
    done = subprocess.run(
        [sys.executable, str(OWNER_FILES), action, str(record)], capture_output=True, text=True
    )
    if done.returncode:
        raise AssertionError(f"the owner's files: {done.stdout.strip()} {done.stderr.strip()}")
    return done.stdout.strip()


async def router(ctx: Ctx) -> str:
    return (await la.state(ctx))["router_id"]


async def on_codex(ctx: Ctx, session_id: str, what: str):
    view = await la.session(ctx, session_id)
    assert view.get("agent") == "codex", f"{what} on {view.get('agent')!r}, not Codex"


def helpers(ctx: Ctx, title: str) -> list[str]:
    return [
        row[0] for row in ctx.rows("SELECT id FROM sessions WHERE role='worker' AND title=?", title)
    ]


# Shard X


async def x1_first_start(ctx: Ctx):
    state = await la.state(ctx)
    assert state["runtime"]["state"] == "ready", state["runtime"]
    work = state["work"]
    assert work["agents"] == ["codex"] and work["default"] == "codex", work
    copilot = next(s for s in state.get("agents_status") or [] if s["name"] == "copilot")
    assert not copilot["on"] and copilot["status"] == "missing", copilot
    chat = await router(ctx)
    await on_codex(ctx, chat, "the Router")
    said = await la.notices(ctx, chat)
    assert len([line for line in said if FIRST_RUN.match(line)]) == 1, said
    assert await ctx.app.get("/api/models") == [], "Copilot's models: none"
    listed = await ctx.app.get("/api/models?agent=codex")
    assert listed and all(model["id"] for model in listed), listed
    usage = await ctx.app.get(f"/api/usage?since={time.time() - 3600}")
    assert isinstance(usage, dict), usage
    others = await ctx.app.get("/api/external")
    assert isinstance(others, list) and all(item.get("agent") != "copilot" for item in others)
    await ctx.app.get("/api/others")
    log(f"X1: {said[0]!r}; {len(listed)} Codex models; {len(others)} other sessions")


async def x2_router_answers(ctx: Ctx):
    before = {s["id"] for s in (await la.state(ctx))["sessions"]}
    chat, mark = await la.say(ctx, "What is 17 times 23? Answer with just the number.")
    answer = await la.router_said(ctx, chat, mark)
    assert "391" in answer, answer
    workers = [s for s in (await la.state(ctx))["sessions"]
               if s["role"] == "worker" and s["id"] not in before]  # fmt: skip
    assert not workers, [w["title"] for w in workers]
    log(f"X2: {answer[:80]!r}")


async def x3_task(ctx: Ctx):
    worker = await la.delegate(
        ctx,
        f"In {ctx.project}: how many data rows (not counting the header) are in data/sales.csv? "
        "Don't change any files.",
    )
    await on_codex(ctx, worker, "the worker")
    task = await la.task_ended(ctx, worker, "the count", statuses=la.ENDED)
    assert task["status"] in la.FINISHED and "540" in (task["result"] or ""), task
    log(f"X3: {task['result'][:120]!r}")


async def x4_watchdog(ctx: Ctx):
    seconds = la.unique(150)
    worker = await la.delegate(
        ctx,
        f"No project, a new worker: run `sleep {seconds}` (quiet, about two and a half "
        "minutes; let it finish), then report 'waited'.",
        new=True,
    )
    await on_codex(ctx, worker, "the worker")

    async def reviewed():
        sessions = (await la.state(ctx))["sessions"]
        watchdog = next((s for s in sessions if s["role"] == "watchdog"), None)
        if not watchdog:
            return None
        rows = await la.messages(ctx, watchdog["id"])
        found = [m for m in rows if m["kind"] == "inbound" and m["meta"].get("review") == worker]
        return watchdog if found else None

    watchdog = await wait_for("the Watchdog reviews it", reviewed, 600, ctx.app)
    await on_codex(ctx, watchdog["id"], "the Watchdog")
    task = await la.task_ended(ctx, worker, "it finishes", statuses=la.ENDED)
    log(f"X4: task {task['status']}")


async def distilled(ctx: Ctx) -> dict:
    before = {d["id"] for d in (await la.state(ctx))["drafts"]}
    await ctx.app.post("/api/profile/distill")

    async def card():
        drafts = (await la.state(ctx))["drafts"]
        return next((d for d in drafts if d["kind"] == "profile" and d["id"] not in before), None)

    return await wait_for("the profile card", card, 900, ctx.app)


async def x5_distill(ctx: Ctx):
    card = await distilled(ctx)
    (helper,) = helpers(ctx, HELPER)
    await on_codex(ctx, helper, "the profile's helper")
    assert card["text"].strip(), card
    ctx.notes["distill"] = helper
    await ctx.app.post(f"/api/drafts/{card['id']}", {"action": "reject", "reason": "live run"})
    log(f"X5: a card of {len(card['text'])} chars")


async def x6_distill_again(ctx: Ctx):
    await distilled(ctx)
    found = helpers(ctx, HELPER)
    assert found == [ctx.notes.get("distill", found[0])], ("reused", found)
    await on_codex(ctx, found[0], "the profile's helper, again")
    log("X6: the same helper, on Codex")


async def x7_away(ctx: Ctx):
    sent = ctx.root / "phone.log"
    script = ctx.root / "notify-phone"
    script.write_text(f'#!/bin/sh\ncat "$1" >> "{sent}"\necho "delivered"\n')
    script.chmod(0o755)
    await la.settings(
        ctx, away_notify=True,
        away_how=f"Run {script} with the message file's path as its only argument: it sends it "
        "to my phone.",
    )  # fmt: skip
    await ctx.app.post("/api/away/test")

    async def arrived():
        return sent.read_text() if sent.exists() and sent.read_text().strip() else None

    text = await wait_for("the notification through the CLI", arrived, 600, ctx.app)
    assert "Test from Chief of Staff" in text, text

    async def reported():
        last = (await la.state(ctx))["connector"].get("away", {}).get("last") or {}
        return last if last.get("at") else None

    last = await wait_for("its report", reported, 300, ctx.app)
    assert last["ok"], last
    messenger = [
        key.split(":", 1)[1]
        for (key,) in ctx.rows("SELECT key FROM kv WHERE key LIKE 'messenger:%'")
    ]
    assert messenger, "a messenger ran"
    await on_codex(ctx, messenger[-1], "the messenger")
    await la.settings(ctx, away_notify=False)
    log(f"X7: {text[:120]!r}")


async def x8_slack(ctx: Ctx):
    await la.settings(ctx, slack_enabled=True)

    async def slack_ready():
        state = await la.state(ctx)
        sessions = [s for s in state["sessions"] if s["role"] == "slack"]
        return sessions[0] if sessions and state["slack"]["connected"] else None

    slack = await wait_for("the Slack session", slack_ready, 300, ctx.app)
    await on_codex(ctx, slack["id"], "the Slack session")
    ctx.workspace.post("C0DEPLOYS", "U0BOB001", f"Deploy dashboard lives here: {DASHBOARD}")
    await asyncio.sleep(2)
    ctx.workspace.post(
        "D0ALICE01", "U0ALICE1",
        "Hey! Could you send me the deploy dashboard link? Bob posted it in #deploys.",
    )  # fmt: skip

    async def drafted():
        drafts = (await la.state(ctx))["drafts"]
        return next((d for d in drafts if d["kind"] == "slack"), None)

    draft = await wait_for("a Slack reply drafted", drafted, 900, ctx.app)
    assert draft["target"]["channel_id"] == "D0ALICE01", draft
    assert not ctx.workspace.sent, "nothing sent before the owner's OK"
    reads = [name for name, _ in ctx.workspace.calls if name.startswith("slack_")]
    assert reads, "Slack read through Chief of Staff's tools"
    sessions = ctx.root / "codex-sessions"
    with contextlib.suppress(OSError):
        for path in sessions.rglob("*.jsonl"):
            assert SLACK_TOKEN not in path.read_text(errors="replace"), path
    await ctx.app.post(f"/api/drafts/{draft['id']}", {"action": "reject", "reason": "live run"})
    log(f"X8: {draft['text'][:120]!r}; {len(reads)} Slack reads")


async def x9_watch(ctx: Ctx):
    await la.settings(
        ctx,
        watches=[{"what": "Direct messages and mentions of me on Slack", "enabled": True}],
        watch_after_hours_minutes=5,
    )
    await ctx.app.post("/api/watch/scan", {})

    async def scanned():
        found = (await la.state(ctx))["connector"]
        if not found["scanned_at"] or found["scanning"] or not found["session_id"]:
            return None
        return found["session_id"]

    session_id = await wait_for("the scan completes", scanned, 900, ctx.app)
    await on_codex(ctx, session_id, "Watch for you")
    log("X9: scanned on Codex")


def seed_case(ctx: Ctx) -> None:
    """A mistake learned (failure_cases), as a worker's test run that failed and then passed
    leaves one, written while Chief of Staff is stopped."""
    repo = ctx.root / "mistake"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo / "calc.py", "def add(a, b):\n    return a - b\n")
    write(repo / "test_calc.py", "from calc import add\n\n\ndef test_add():\n"
          "    assert add(2, 2) == 4\n")  # fmt: skip
    git(repo, "add", ".")
    git(repo, "-c", "user.email=p@test", "-c", "user.name=p", "commit", "-qm", "add")
    failing = git(repo, "rev-parse", "HEAD").strip()
    write(repo / "calc.py", "def add(a, b):\n    return a + b\n")
    git(repo, "-c", "user.email=p@test", "-c", "user.name=p", "commit", "-qam", "fix add")
    fixed = git(repo, "rev-parse", "HEAD").strip()
    database = live_e2e.data_dir(ctx.root) / "chiefofstaff.db"
    with contextlib.closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO failure_cases (id, session_id, repo, git_dir, top, command, output, "
            "failing, fixed, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)",
            (str(uuid.uuid4()), "live", "acme/calc", str(repo / ".git"), str(repo),
             "python -m pytest -q", "1 failed: assert 0 == 4", failing, fixed,
             la.now()),
        )  # fmt: skip
        db.commit()


async def x10_checker(ctx: Ctx):
    await la.settings(ctx, learn_from_work=True)
    await ctx.app.stop()
    seed_case(ctx)
    await ctx.app.start()

    async def started():
        found = helpers(ctx, "Learn checks from mistakes")
        return found[0] if found else None

    # (Its first look comes two minutes after a start: habits.FIRST_COLLECT_DELAY.)
    checker = await wait_for("the checker starts", started, 600, ctx.app)
    await on_codex(ctx, checker, "the checker")

    async def ended():
        view = await la.session(ctx, checker)
        return view if view["status"] not in {"working", "queued", "starting"} else None

    view = await wait_for("the checker ends its run", ended, 900, ctx.app)
    assert view["status"] != "failed", view
    log(f"X10: the checker {view['status']}")


SCENARIOS = {
    "X": [x1_first_start, x2_router_answers, x3_task, x4_watchdog, x5_distill, x6_distill_again,
          x7_away, x8_slack, x9_watch, x10_checker]
}  # fmt: skip


async def outcome_of(running) -> Exception | None:
    try:
        await running
    except Exception as exc:
        return exc
    return None


async def run_codex_alone(wanted: set[str]) -> tuple[list, dict]:
    port, slack_port = SHARDS["X"]
    live_e2e.PORT, live_e2e.SLACK_PORT = port, slack_port
    live_e2e.BASE = f"http://127.0.0.1:{port}"
    workspace = Workspace()
    serve_in_thread(workspace, SLACK_TOKEN, slack_port)
    parent = os.environ.get("LIVE_AGENTS_ROOT") or None
    if parent:
        Path(parent).mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="cos-anywhere-X-", dir=parent)).resolve()
    project, truth = project_of(root)
    record = root / "owner-files.json"
    owner_files("record", record)
    env = {
        "PATH": codex_only_path(root),
        "CHIEFOFSTAFF_MIN_FREE_MEMORY": "0",
        "CHIEFOFSTAFF_ROLE_RETRY": "20",
    }
    app = App(live_e2e.data_dir(root), root / "log", env, agents=True)
    await app.start()
    ctx = Ctx("X", app, root, root / "home", project, truth)
    ctx.workspace = workspace
    results = []
    try:
        await la.settings(
            ctx, projects=[str(project)], max_workers=4, mac_notifications=False,
            digest_time="", worker_yolo=True,
            **({"agents": {"codex": {"model": CODEX_MODEL}}} if CODEX_MODEL else {}),
        )  # fmt: skip
        for scenario in SCENARIOS["X"]:
            name = la.label(scenario)
            if wanted and name not in wanted:
                continue
            log(f"=== {name} {scenario.__name__}")
            started = time.monotonic()
            outcome = await outcome_of(scenario(ctx))
            if outcome is None:
                results.append((name, True, time.monotonic() - started, ""))
            else:
                log(f"{name} FAILED: {outcome!r}")
                await la.dump(ctx)
                results.append((name, False, time.monotonic() - started, repr(outcome)[:300]))
                if not (ctx.app.process and ctx.app.process.poll() is None):
                    await ctx.app.start()
        notes = {"codex tokens": ctx.codex_tokens(), "copilot credits": ctx.copilot_credits()}
    finally:
        await app.stop()
        with contextlib.suppress(OSError):
            (root.parent / f"cos-anywhere-X-{int(time.time())}.log").write_text(
                (root / "log").read_text()
            )
    owner_files("check", record)
    log(f"the owner's files: unchanged ({root})")
    return results, notes


async def run_demo() -> list:
    port, _ = SHARDS["D"]
    root = Path(tempfile.mkdtemp(prefix="cos-anywhere-D-")).resolve()
    env = {**os.environ, "PATH": no_agent_path(root)}
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-m", "chiefofstaff", "--demo", "--no-browser", "--port", str(port),
         "--data-dir", str(root / "data")],
        stdout=subprocess.PIPE, stderr=(root / "log").open("a"), env=env, text=True,
    )  # fmt: skip
    live_e2e.PORT, live_e2e.BASE = port, f"http://127.0.0.1:{port}"
    try:
        token = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not token:
            line = await asyncio.to_thread(process.stdout.readline)
            if "#token=" in line:
                token = line.split("#token=", 1)[1].strip()
            elif not line and process.poll() is not None:
                break
        assert token, "the demo did not start"
        import httpx

        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, headers={"origin": base}, timeout=60) as http:
            assert (await http.post("/api/auth", json={"token": token})).status_code == 200
            http.headers["X-Chief-Key"] = token

            async def state() -> dict:
                return (await http.get("/api/state")).json()

            deadline = time.monotonic() + 60
            while (await state())["runtime"]["state"] != "ready":
                assert time.monotonic() < deadline, (await state())["runtime"]
                await asyncio.sleep(1)
            chat = (await state())["router_id"]
            await http.post(f"/api/sessions/{chat}/messages", json={"text": "Fix the flaky test"})
            deadline = time.monotonic() + 120
            while not [s for s in (await state())["sessions"] if s["role"] == "worker"]:
                assert time.monotonic() < deadline, "no worker started"
                await asyncio.sleep(1)
        log("D1: the demo runs with no agent's CLI on the PATH")
        return [("D1", True, time.monotonic() - started, "")]
    except Exception as exc:
        log(f"D1 FAILED: {exc!r}")
        return [("D1", False, time.monotonic() - started, repr(exc)[:300])]
    finally:
        if process.poll() is None:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(30)


async def main() -> int:
    shards = [sys.argv[1].upper()] if len(sys.argv) > 1 else ["X", "D"]
    wanted = {name.upper() for name in sys.argv[2:]}
    results, notes = [], {}
    if "X" in shards:
        found, notes = await run_codex_alone(wanted)
        results += found
    if "D" in shards and (not wanted or "D1" in wanted):
        results += await run_demo()
    print("\nSummary")
    for name, passed, seconds, why in results:
        print(f"  {name:4} {'PASS' if passed else 'FAIL'} {seconds:6.0f}s {why}")
    for key, value in notes.items():
        print(f"  {key}: {value}")
    return 0 if results and all(passed for _, passed, _, _ in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
