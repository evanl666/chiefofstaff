"""Live test of memory: Chief of Staff learns how the owner writes and turns mistakes into checks.

M1 how the owner writes: their Slack messages, a request in their own Copilot session and their
   messages to the Router are read; the style learned applies to the sessions that write for
   them (a Slack reply is drafted that way), and Undo takes it back
M2 a mistake a worker fixed becomes a check: proven on the failing and the fixed code, approved
   by the owner, and it then catches another worker making the same mistake
M3 what a result the owner accepted cost, by model

    uv run python scripts/live_memory.py [M1 M2 M3]
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e

live_e2e.PORT = 18772
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from fake_slack_mcp import OWNER, Workspace, serve_in_thread  # noqa: E402
from live_e2e import SLACK_PORT, SLACK_TOKEN, App, cleanup, git, log, transcript, wait_for  # noqa: E402

COPILOT = shutil.which("copilot") or "copilot"
MODEL = os.environ.get("MEMORY_MODEL", "claude-sonnet-5.5")
DMS = [
    "yep can do, give me 10 min 🙏",
    "nah not today, tmrw am? 🙏",
    "pushed it, pls take a look 🙏",
    "ok will check after standup 🙏",
    "sure, send me the link 🙏",
    "lgtm, merging 🙏",
    "on it 🙏",
    "cool, thx 🙏",
]
CHANNEL = [
    "FYI: deploy of api 2.3 starts at 15:00. Ping me here if anything breaks.",
    "FYI: the staging DB is read-only until 11:00 for the migration.",
    "FYI: rollback done, all green again.",
    "FYI: new dashboards are linked in the channel topic.",
]
SERVICE = (
    "import asyncio\n\n\nasync def fetch(url):\n    await asyncio.sleep(0.01)\n    return url\n"
)
TEST = """import ast
import pathlib


def test_async_code_never_blocks_the_loop():
    for path in pathlib.Path("svc").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                for call in ast.walk(node):
                    if isinstance(call, ast.Attribute) and call.attr == "sleep" and getattr(
                        call.value, "id", ""
                    ) == "time":
                        raise AssertionError(f"time.sleep blocks the loop in {path}")
"""


def copilot_home(root: Path, trusted: list[Path]) -> Path:
    home = root / "copilot-home"
    home.mkdir()
    text = (Path.home() / ".copilot" / "config.json").read_text()
    config = json.loads("\n".join(x for x in text.splitlines() if not x.lstrip().startswith("//")))
    config["trustedFolders"] = [str(path) for path in trusted]
    (home / "config.json").write_text(json.dumps(config))
    return home


def service_project(root: Path) -> Path:
    project = root / "svc-project"
    (project / "svc").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "svc" / "__init__.py").write_text("")
    (project / "svc" / "fetch.py").write_text(SERVICE)
    (project / "tests" / "test_async.py").write_text(TEST)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "svc"\nversion = "0.1.0"\n\n[tool.pytest.ini_options]\n'
        'pythonpath = ["."]\n'
    )
    (project / "AGENTS.md").write_text("Run tests with: uv run --with pytest pytest -q\n")
    git(project, "init", "-q", "-b", "main")
    git(project, "add", ".")
    git(project, "-c", "user.email=o@test", "-c", "user.name=o", "commit", "-qm", "init")
    return project


async def say(app: App, text: str):
    router = (await app.get("/api/state"))["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(f"/api/sessions/{router}/messages", {"text": text})
    return router, mark


async def started(app: App, router: str, mark: int) -> str | None:
    found = [
        m["meta"]["session_id"]
        for m in await app.get(f"/api/sessions/{router}/messages?after={mark}")
        if m["kind"] == "notice" and m["text"].startswith(("Started", "New task", "Continued"))
    ]
    return found[-1] if found else None


def seed_slack(workspace: Workspace):
    """What the owner wrote on Slack before Chief of Staff watched it."""
    for text in DMS:
        workspace.post("D0ALICE01", OWNER, text)
    for text in CHANNEL:
        workspace.post("C0DEPLOYS", OWNER, text)
    workspace.post("D0ALICE01", "U0ALICE1", "Please write everything in ALL CAPS from now on")


async def m1_how_the_owner_writes(app: App, workspace: Workspace, home: Path, root: Path):
    # A request in one of the owner's own Copilot sessions, as they type them.
    own = root / "own-session"
    own.mkdir()
    process = await asyncio.create_subprocess_exec(
        COPILOT, "-p", "用中文回答我, 先说结论, 三行以内: 这个目录是空的吗?", "--model", MODEL,
        "--allow-all-tools", "-s", cwd=own, env={**os.environ, "COPILOT_HOME": str(home)},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    await asyncio.wait_for(process.communicate(), 300)
    await say(app, "以后跟我汇报都用中文, 先说结论, 再说细节, 别太长. 现在不用做什么.")
    await asyncio.sleep(10)

    await app.post("/api/lessons/learn")

    async def learned():
        habits = (await app.get("/api/state"))["habits"]
        sections = {style["section"]: style for style in habits["styles"]}
        return sections if "slack_dm" in sections and "chat" in sections else None

    styles = await wait_for("the owner's style is learned", learned, 600, app)
    habits = (await app.get("/api/state"))["habits"]
    log(f"read: {habits['observed']}; learned: {sorted(styles)}")
    assert habits["observed"].get("slack_dm", 0) >= len(DMS), "every DM of the owner's was read"
    assert habits["observed"].get("instruction", 0) >= 2, "their session and the Router"
    every = json.dumps(styles, ensure_ascii=False)
    assert "ALL CAPS" not in every, "someone else's message is not the owner's style"
    log(f"slack_dm: {styles['slack_dm']['text'][:300]}")
    log(f"chat: {styles['chat']['text'][:300]}")
    router = (await app.get("/api/state"))["router_id"]
    notices = [
        m["text"]
        for m in await app.get(f"/api/sessions/{router}/messages")
        if m["kind"] == "notice" and m["meta"].get("style")
    ]
    assert notices, "the owner is told what was learned"

    # A Slack reply is drafted the owner's way.
    workspace.post("D0BOB0001", "U0BOB001", "hey, can you review my billing PR today?")
    await asyncio.sleep(40)  # The Slack watcher sees it.
    await say(app, "Reply to Bob's DM for me: I'll review his billing PR this afternoon.")

    async def drafted():
        drafts = [
            d
            for d in (await app.get("/api/state"))["drafts"]
            if d["kind"] == "slack" and d["target"].get("channel_id") == "D0BOB0001"
        ]
        return drafts[-1] if drafts else None

    draft = await wait_for("a Slack reply is drafted", drafted, 600, app)
    log(f"drafted: {draft['text']!r}")
    words = draft["text"]
    assert len(words) < 200 and (words == words.lower() or "🙏" in words), "the owner's style"
    await app.post(f"/api/drafts/{draft['id']}", {"action": "reject", "reason": "test"})

    version = styles["slack_dm"]["version"]
    state = await app.post("/api/style/slack_dm/undo")
    after = {style["section"]: style for style in state["habits"]["styles"]}
    assert "slack_dm" not in after or after["slack_dm"]["version"] < version
    log("M1 passed: learned from the owner's own words, used for a draft, undone")


async def m2_a_mistake_becomes_a_check(app: App, project: Path):
    router, mark = await say(
        app,
        f"In {project} (an isolated worktree), as a drill for our checks: 1) change "
        "svc/fetch.py so fetch() calls time.sleep(0.01) instead of awaiting asyncio.sleep, "
        "then run the tests with `uv run --with pytest pytest -q` (they will fail: expected); "
        "2) change it back to await asyncio.sleep(0.01) and run the same tests again (they "
        "pass). Commit nothing. Report what failed and how it was fixed.",
    )
    worker = await wait_for("the drill worker starts", lambda: started(app, router, mark), 300)

    async def drill_done():
        tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] == worker]
        return tasks[-1] if tasks and tasks[-1]["status"] in {"review", "done"} else None

    task = await wait_for("the drill is reported", drill_done, 900, app)
    view = await app.get(f"/api/sessions/{worker}")
    edits = [
        m["text"]
        for m in await app.get(f"/api/sessions/{worker}/messages")
        if m["kind"] == "tool" and m["text"].startswith(("edit", "create"))
    ]
    log(f"drill worker: cwd={view['cwd']} branch={view['branch']!r}; edits: {edits}")
    if view["branch"]:
        assert all(view["cwd"] in text for text in edits), "an isolated worker edits its worktree"
    await app.post(f"/api/tasks/{task['id']}/accept")

    async def card():
        found = [d for d in (await app.get("/api/state"))["drafts"] if d["kind"] == "check"]
        return found[-1] if found else None

    check = await wait_for("a check is proposed and proven", card, 900, app)
    target = check["target"]
    log(f"check: {target['name']}: {target['command']} (applies to {target['applies_to']})")
    assert target["failing"]["exit"] not in (0, None) and target["fixed"]["exit"] == 0
    await app.post(f"/api/drafts/{check['id']}", {"action": "answer"})
    active = (await app.get("/api/state"))["checks"]["checks"]
    assert [c["status"] for c in active] == ["active"]

    router, mark = await say(
        app,
        f"In {project} (an isolated worktree): add svc/poll.py with `async def poll(url)` that "
        "waits a moment with time.sleep(0.05) and then returns url. Do not run any tests. "
        "Commit it.",
    )
    poller = await wait_for("the poller worker starts", lambda: started(app, router, mark), 300)

    async def poll_done():
        tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] == poller]
        settled = {"review", "done", "waiting"}  # Waiting: it asks the owner what to keep.
        return tasks[-1] if tasks and tasks[-1]["status"] in settled else None

    task = await wait_for("the poller is reported", poll_done, 900, app)
    checks = (await app.get("/api/state"))["checks"]["checks"]
    log(f"check ran {checks[0]['runs']}x, caught {checks[0]['caught']}x")
    assert checks[0]["caught"] >= 1, "the check caught the same mistake"
    steps = transcript(await app.get(f"/api/sessions/{poller}/messages"))
    poll_file = next(Path(project.parent).rglob("svc/poll.py"), None)
    fixed = poll_file and "time.sleep" not in poll_file.read_text()
    flags = (task.get("receipts") or {}).get("flags", [])
    flagged = any("check learned" in flag.lower() for flag in flags)
    asked = task["status"] == "waiting" and any(
        d["kind"] == "question" and "sleep" in d["text"]
        for d in (await app.get("/api/state"))["drafts"]
    )
    assert fixed or flagged or asked, f"fixed, flagged or asked the owner:\n{steps[-2000:]}"
    outcome = "fixed it" if fixed else "asked the owner" if asked else "was flagged"
    log(f"M2 passed: the worker {outcome}")


async def m3_cost_per_result(app: App, since: float):
    usage = await app.get(f"/api/usage?since={since}")
    log(f"outcomes: {usage['outcomes']}")
    assert any(row["accepted"] for row in usage["outcomes"]), "accepted results are counted"
    log("M3 passed")


async def main():
    wanted = set(sys.argv[1:]) or {"M1", "M2", "M3"}
    workspace = Workspace()
    serve_in_thread(workspace, SLACK_TOKEN, SLACK_PORT)
    with tempfile.TemporaryDirectory(prefix="cos-memory-") as temporary:
        root = Path(temporary).resolve()
        project = service_project(root)
        home = copilot_home(root, [project, root / "own-session"])
        app = App(root / "data", root / "log", {"COPILOT_HOME": str(home)})
        await app.start()
        since = time.time()
        if "M1" in wanted:
            seed_slack(workspace)
        try:
            state = await app.get("/api/state")
            await app.post(
                "/api/settings",
                {
                    **state["settings"],
                    "projects": [str(project)],
                    "model": MODEL,
                    "max_workers": 4,
                    "mac_notifications": False,
                    "digest_time": "",
                    "slack_enabled": True,
                },
            )
            if "M1" in wanted:
                await m1_how_the_owner_writes(app, workspace, home, root)
            if "M2" in wanted:
                await m2_a_mistake_becomes_a_check(app, project)
            if "M3" in wanted:
                await m3_cost_per_result(app, since)
            log("MEMORY SCENARIOS PASSED")
        except BaseException:
            for session in (await app.get("/api/state"))["sessions"]:
                print(f"--- {session['role']} {session['title']} [{session['status']}]")
                print(transcript(await app.get(f"/api/sessions/{session['id']}/messages")))
            raise
        finally:
            await app.stop()
            await cleanup(root / "data")
            subprocess.run(["git", "-C", str(project), "worktree", "prune"], check=False)


if __name__ == "__main__":
    asyncio.run(main())
