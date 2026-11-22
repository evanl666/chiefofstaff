"""Live: choosing and using another coding agent, smooth: Install and Sign in from the page, its
status seen live, the work it held going on by itself (once), in-place work to a worker of its
own. The Router on Copilot (real); Codex and Claude are the stand-in CLIs (tests/fake_codex.py,
tests/fake_claude.py) on a PATH this run controls, with a HOME, CODEX_HOME and CLAUDE_CONFIG_DIR of
its own: no real Codex or Claude runs, no account of theirs is touched, nothing of the owner's
home is read or written (the owner's `codex` and `claude` are taken off the PATH; their
~/.local/bin is not this run's). A stand-in `curl` answers the official installers' URLs with a
stand-in installer; any other URL goes to the real curl.

M1  Codex not installed: "use codex for …" is held, its card says the message is saved; Install
    runs the official command word for word (its output streamed), the installer puts codex in
    ~/.local/bin, not on the PATH: found there, its --version answers, the row says Sign-in
    needed by itself, and Sign in follows (the stand-in's `codex login`, never a real one)
M2  More work held for the sign-in; the sign-in shows its page's link (Codex prints it on
    stderr); the owner's browser says ok: Codex is Ready by itself (nothing asked), and each held
    task starts on Codex once, its card says how, a line under Handled for you each
M3  Once, never twice: Chief of Staff restarted, then Codex signed out in a terminal and signed
    in again from the page: nothing held starts again
M4  A turn that ran a command, then lost its sign-in: Sign-in needed at once, nothing held; signed
    in again, that turn is never run again (its command ran once)
M5  Claude, live: signed out in a terminal, then in, the page's next ask says so (and when its
    sign-in ends); signed in from the page with a pasted code, Ready by itself; ANTHROPIC_API_KEY
    and Codex's CODEX_ACCESS_TOKEN in the environment Chief of Staff starts in: said as what
    pays, by name, their values nowhere
M6  "work in place, directly in my folder (not a copy)" while a worker that read the project is
    idle: a new worker in place does it; the one that read it gets nothing
M7  Codex's plan limit used up (the stand-in says nothing of its windows until asked): a worker's
    turn pauses saying which limit and until when; the @ list says it; work named for Codex is
    held with its card; once the limit resets (its time, then Codex asked again), each goes on,
    once, under Handled for you

Signs in to Copilot with COPILOT_GITHUB_TOKEN, else the gh CLI's token (a Copilot home of its own;
~/.copilot is not used). Uses a few premium requests (the Router: LIVE_SMOOTH_MODEL, else
LIVE_COPILOT_MODEL, else claude-sonnet-5.5) and takes about 12 minutes. Ports: Chief of Staff on
LIVE_SMOOTH_PORT (21410), its stand-in Slack on the next one. Its folders are made by `tempfile`
(LIVE_AGENTS_ROOT to keep them elsewhere); kept for a look when a scenario fails.

    uv run python scripts/live_smooth_agents.py [M1 M2 …]
"""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(1, str(ROOT / "tests"))
import live_e2e  # noqa: E402

live_e2e.PORT = int(os.environ.get("LIVE_SMOOTH_PORT", "21410"))
live_e2e.SLACK_PORT = live_e2e.PORT + 1
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from agent_paths import without_agents  # noqa: E402
from fake_slack_mcp import Workspace, serve_in_thread  # noqa: E402
from live_e2e import SLACK_TOKEN, App, cleanup, git, log, transcript, wait_for  # noqa: E402

from chiefofstaff.backend import FALLBACK_CLI  # noqa: E402

MODEL = os.environ.get(
    "LIVE_SMOOTH_MODEL", os.environ.get("LIVE_COPILOT_MODEL", "claude-sonnet-5.5")
)
CODEX_INSTALL = "curl -fsSL https://chatgpt.com/codex/install.sh | sh"
PASTE = "smooth-code-2611"
FINISHED = {"review", "done"}
# Copilot's own runtime as the SDK keeps it (the owner's Copilot CLI may be newer than Chief of
# Staff supports): used where it is, not downloaded again into this run's home.
SDK_CACHE = Path.home() / "Library" / "Caches" / "github-copilot-sdk" / "cli"


class Ctx:
    def __init__(self, app: App, root: Path, project: Path):
        self.app, self.root, self.project = app, root, project
        self.home = root / "home"
        self.codex_state = root / "fake-codex-home"
        self.claude_config = root / "claude-config"
        self.held: dict[str, dict] = {}
        self.workers: list[str] = []

    async def state(self) -> dict:
        return await self.app.get("/api/state")

    async def status(self, name: str, ask: bool = False) -> dict:
        """An agent's status as the page has it (`ask`: asked again, as the page does on focus
        and as the @ list opens)."""
        if ask:
            found = (await self.app.post("/api/agents/status"))["agents_status"]
        else:
            found = (await self.state()).get("agents_status") or []
        return next((entry for entry in found if entry["name"] == name), {})

    async def setup(self, name: str) -> dict:
        return (await self.state()).get("agent_setups", {}).get(name) or {}

    async def router(self) -> str:
        return (await self.state())["router_id"]

    async def say(self, text: str, agent: str = "") -> int:
        """The owner's message to the Router (`agent`: picked from the @ list, as its chip)."""
        router = await self.router()
        mark = max((m["id"] for m in await self.app.get(f"/api/sessions/{router}/messages")),
                   default=0)  # fmt: skip
        body = {"text": text, **({"agent": agent} if agent else {})}
        await self.app.post(f"/api/sessions/{router}/messages", body)
        return mark

    async def router_messages(self, after: int = 0) -> list[dict]:
        return await self.app.get(f"/api/sessions/{await self.router()}/messages?after={after}")

    async def held_card(self, mark: int, timeout: float = 300) -> dict:
        async def card():
            return next(
                (m for m in await self.router_messages(mark) if m["meta"].get("agent_refused")),
                None,
            )

        return await wait_for("its card: the message saved", card, timeout, self.app)

    async def card(self, held: dict) -> dict:
        found = await self.router_messages(held["id"] - 1)
        return next(m for m in found if m["id"] == held["id"])

    async def workers_on(self, agent: str) -> list[dict]:
        rows = (await self.state())["sessions"]
        return [r for r in rows if r["role"] == "worker" and (r.get("agent") or "copilot") == agent]

    def approve(self, said: str = "ok"):
        """The owner finishing the stand-in Codex's sign-in in their browser."""
        (self.codex_state / "approve").write_text(said)

    def codex_signed_out(self):
        """Signed out of the stand-in Codex in a terminal (`codex logout`)."""
        (self.codex_state / "auth.json").unlink(missing_ok=True)
        (self.codex_state / "signed-out").write_text("")

    def processes(self) -> list[str]:
        """The command lines of what this Chief of Staff runs."""
        rows = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, errors="replace"
        ).stdout.splitlines()
        table = {}
        for line in rows:
            parts = line.split(None, 2)
            if len(parts) == 3:
                table[int(parts[0])] = (int(parts[1]), parts[2])
        assert self.app.process
        found, frontier = set(), {self.app.process.pid}
        while frontier:
            frontier = {pid for pid, (parent, _) in table.items() if parent in frontier} - found
            found |= frontier
        return [table[pid][1] for pid in found if pid in table]

    def no_real_agent(self):
        """Nothing this run started is the owner's own Codex or Claude."""
        real = [shutil.which("codex"), shutil.which("claude"), "@openai/codex",
                "/.local/share/claude/", "claude-code"]  # fmt: skip
        ran = [line for line in self.processes() if any(r and r in line for r in real)]
        assert not ran, f"a real agent ran: {ran}"


def write(path: Path, text: str, mode: int = 0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def stand_ins(root: Path) -> dict[str, str]:
    """This run's environment: the stand-in `claude` and `curl` on the PATH (the owner's own
    agents off it), the stand-in Codex only once installed; a home and agent folders of its own."""
    folder = root / "stand-ins"
    home = root / "home"
    for name in ("codex", "claude"):
        script = ROOT / "tests" / f"fake_{name}.py"
        write(root / "wrappers" / name, f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
              0o755)  # fmt: skip
    folder.mkdir()
    shutil.copy2(root / "wrappers" / "claude", folder / "claude")
    # The official installer, as a stand-in: slow enough to be seen running, it puts the stand-in
    # Codex where the official one puts codex (~/.local/bin, not on the PATH).
    write(
        root / "codex-install.sh",
        'echo "Downloading Codex (stand-in)…"\nsleep 6\nmkdir -p "$HOME/.local/bin"\n'
        f'cp "{root / "wrappers" / "codex"}" "$HOME/.local/bin/codex"\n'
        'chmod +x "$HOME/.local/bin/codex"\necho "Codex installed to $HOME/.local/bin/codex"\n',
    )
    real_curl = shutil.which("curl") or "/usr/bin/curl"
    write(
        folder / "curl",
        "#!/bin/sh\n# The official installers' URLs answered by this run's stand-ins.\n"
        'for a in "$@"; do case "$a" in\n'
        f'  https://chatgpt.com/codex/install.sh) cat "{root / "codex-install.sh"}"; exit 0;;\n'
        "  https://claude.ai/install.sh) echo 'echo \"Not in this run.\" >&2; exit 1'; exit 0;;\n"
        f'esac; done\nexec "{real_curl}" "$@"\n',
        0o755,
    )
    home.mkdir()
    (root / "copilot-home").mkdir()
    (root / "fake-codex-home").mkdir()
    (root / "fake-codex-home" / "signed-out").write_text("")
    (root / "codex-home").mkdir()
    write(root / "claude-config" / ".claude.json", "{}\n")
    (root / "tmp").mkdir()
    path = f"{folder}{os.pathsep}{without_agents(os.environ.get('PATH', ''), root / 'path')}"
    env = {
        "PATH": path,
        "HOME": str(home),
        "TMPDIR": str(root / "tmp"),
        "COPILOT_HOME": str(root / "copilot-home"),
        "CODEX_HOME": str(root / "codex-home"),
        "CLAUDE_CONFIG_DIR": str(root / "claude-config"),
        "FAKE_CODEX_HOME": str(root / "fake-codex-home"),
        "FAKE_CLAUDE_HOME": str(root / "fake-claude-home"),
        "FAKE_CLAUDE_PASTE": PASTE,
        "CHIEFOFSTAFF_MIN_FREE_MEMORY": "0",
    }
    if (runtime := SDK_CACHE / FALLBACK_CLI).is_dir():
        env["COPILOT_CLI_EXTRACT_DIR"] = str(runtime)
    # (Its installer answers through the stand-in curl, as Chief of Staff will run it.)
    said = subprocess.run(
        ["/bin/sh", "-c", CODEX_INSTALL.split(" | ")[0]], env={**os.environ, **env},
        capture_output=True, text=True, timeout=30,
    ).stdout  # fmt: skip
    assert said == (root / "codex-install.sh").read_text(), "the stand-in installer answers"
    assert not shutil.which("codex", path=path), "no codex on this run's PATH"
    return env


def project_at(root: Path) -> Path:
    project = root / "project"
    write(project / "data" / "sales.csv",
          "region,amount\nsouth,10\nnorth,20\nsouth,30\neast,5\nsouth,7\n")  # fmt: skip
    write(project / "textkit" / "money.py", "def cents(amount):\n    return round(amount * 100)\n")
    write(project / "README.md", "A small project.\n")
    git(project, "init", "-q", "-b", "main")
    git(project, "add", ".")
    git(project, "-c", "user.email=p@test", "-c", "user.name=p", "commit", "-qm", "init")
    return project


async def m1_install(ctx: Ctx):
    codex = await ctx.status("codex", ask=True)
    assert codex["status"] == "missing", codex
    assert codex["install"][0] == {"method": "script", "command": CODEX_INSTALL}, codex["install"]
    mark = await ctx.say(
        f"In {ctx.project}, count the rows of data/sales.csv whose region is south. Give the "
        "worker this line word for word: Report done: 3 south rows.",
        agent="codex",
    )
    held = await ctx.held_card(mark)
    assert held["text"].startswith("Codex isn't installed. Your message is saved:"), held["text"]
    assert held["meta"]["status"] == "missing", held["meta"]
    assert not await ctx.workers_on("codex"), "nothing started"
    ctx.held["install"] = held
    run = await ctx.app.post("/api/agents/codex/install", {"method": "script"})
    assert run["state"] == "running" and run["command"] == CODEX_INSTALL, run

    async def streamed():
        found = await ctx.setup("codex")
        return found.get("kind") == "install" and "Downloading Codex (stand-in)…" in found["tail"]

    await wait_for("the installer's output, as it runs", streamed, 30)

    async def follows():
        found = await ctx.setup("codex")
        return found if found.get("kind") == "signin" and found.get("url") else None

    signin = await wait_for("Sign in follows by itself", follows, 60)
    assert signin["url"].startswith("https://auth.openai.com/oauth/authorize?"), signin
    assert (ctx.home / ".local" / "bin" / "codex").is_file(), "where the installer put it"
    codex = await ctx.status("codex")
    assert codex["status"] == "signin", codex  # Found: no longer Not installed.
    login = [line for line in ctx.processes() if "fake_codex.py login" in line]
    assert login, "the stand-in's `codex login` runs"
    ctx.no_real_agent()
    assert not await ctx.workers_on("codex"), "held until signed in"
    log("M1 passed")


async def m2_sign_in(ctx: Ctx):
    mark = await ctx.say(
        f"In {ctx.project}, tell me what textkit/money.py does in one line. Give the worker this "
        "line word for word: Report done: it turns amounts into cents.",
        agent="codex",
    )
    held = await ctx.held_card(mark)
    assert held["text"].startswith("Codex needs you to sign in. Your message is saved:"), held
    ctx.held["signin"] = held
    first = await ctx.setup("codex")
    # Sign in on the card: the sign-in running already, not another.
    again = await ctx.app.post("/api/agents/codex/signin", {"method": "browser"})
    assert again["started"] == first["started"], (first, again)
    ctx.approve()  # The owner's browser: signed in.

    async def ready():
        found = await ctx.status("codex")  # (Nothing asked: told by its sign-in.)
        return found if found.get("status") == "ready" else None

    codex = await wait_for("Codex Ready by itself", ready, 60)
    assert codex["pays"] == "Uses your ChatGPT sign-in", codex
    assert (await ctx.setup("codex"))["state"] == "done"

    async def resumed():
        cards = [await ctx.card(found) for found in ctx.held.values()]
        return cards if all(card["meta"].get("resumed") for card in cards) else None

    cards = await wait_for("each held task resumes", resumed, 120, ctx.app)
    said = sorted(card["meta"]["resumed"] for card in cards)
    assert said == ["Resumed after sign-in", "Resumed once installed"], said

    async def receipts():
        found = [e for e in (await ctx.state()).get("handled", []) if e["kind"] == "resumed"]
        return found if len(found) >= 2 else None

    handled = await wait_for("a line each under Handled for you", receipts, 60)
    assert len(handled) == 2, handled
    workers = await ctx.workers_on("codex")
    assert len(workers) == 2, workers
    ctx.workers = [row["id"] for row in workers]
    assert "agent_refusals" not in await ctx.state()
    ctx.no_real_agent()
    log("M2 passed")


async def m3_once(ctx: Ctx):
    await ctx.app.stop()
    await ctx.app.start()
    await asyncio.sleep(10)
    assert sorted(r["id"] for r in await ctx.workers_on("codex")) == sorted(ctx.workers)
    ctx.codex_signed_out()

    async def signed_out():
        return (await ctx.status("codex", ask=True)).get("status") == "signin"

    await wait_for("Sign-in needed, the page's next ask", signed_out, 60)
    run = await ctx.app.post("/api/agents/codex/signin", {"method": "browser"})
    assert run["state"] == "running", run
    ctx.approve()

    async def ready():
        return (await ctx.status("codex")).get("status") == "ready"

    await wait_for("Ready again", ready, 60)
    await asyncio.sleep(12)
    assert sorted(r["id"] for r in await ctx.workers_on("codex")) == sorted(ctx.workers), "once"
    for found in ctx.held.values():
        assert (await ctx.card(found))["meta"]["resumed"], "as it was"
    log("M3 passed")


async def m4_no_replay(ctx: Ctx):
    worker = ctx.workers[0]
    marker = ctx.root / "ran-once.txt"
    mark = max((m["id"] for m in await ctx.router_messages()), default=0)
    await ctx.app.post(
        f"/api/sessions/{worker}/messages",
        {"text": f"Run the shell command `echo once >> {marker}` then Lose the sign-in"},
    )

    async def lost():
        if not marker.exists():
            return False
        return (await ctx.status("codex")).get("status") == "signin"

    await wait_for("its command ran, then Sign-in needed at once", lost, 120, ctx.app)
    assert marker.read_text() == "once\n"
    snap = await ctx.state()
    assert "agent_refusals" not in snap, "nothing held: it had run"
    assert not [m for m in await ctx.router_messages(mark) if m["meta"].get("agent_refused")]
    ctx.approve()
    await ctx.app.post("/api/agents/codex/signin", {"method": "browser"})

    async def ready():
        return (await ctx.status("codex")).get("status") == "ready"

    await wait_for("Ready again", ready, 60)
    await asyncio.sleep(15)
    assert marker.read_text() == "once\n", "that turn never ran again"
    log("M4 passed")


async def m5_claude_live(ctx: Ctx):
    config = ctx.claude_config
    (config / "fake-signed-out").write_text("")  # Signed out in a terminal.

    def is_(wanted: str):
        async def check():
            found = await ctx.status("claude", ask=True)
            return found if found.get("status") == wanted else None

        return check

    claude = await wait_for("Claude: Sign-in needed", is_("signin"), 30)
    assert [way["method"] for way in claude["ways"]] == ["browser", "console"], claude
    ends = time.time() + 3 * 86400
    write(config / "fake-auth.json",
          json.dumps({"authMethod": "claude.ai", "loginExpiresAt": int(ends * 1000)}))  # fmt: skip
    (config / "fake-signed-out").unlink()  # Signed in in a terminal.
    claude = await wait_for("Claude: Ready at the page's next ask", is_("ready"), 30)
    assert claude["pays"] == "Uses your Claude plan" and abs(claude["expires"] - ends) < 2, claude
    # Signed out again; signed in from the page, a code pasted.
    (config / "fake-auth.json").unlink()
    (config / "fake-signed-out").write_text("")
    # (A Ready one's look stands 30 seconds when the page asks.)
    await wait_for("Claude: Sign-in needed", is_("signin"), 60)
    run = await ctx.app.post("/api/agents/claude/signin", {"method": "browser"})
    assert run["state"] == "running", run

    async def asks():
        found = await ctx.setup("claude")
        return found if found.get("asks_code") else None

    run = await wait_for("it asks for the code", asks, 30)
    assert run["url"].startswith("https://claude.ai/oauth/authorize?"), run
    await ctx.app.post("/api/agents/claude/setup/input", {"text": PASTE})

    async def ready():
        found = await ctx.status("claude")  # (Nothing asked: told by its sign-in.)
        return found if found.get("status") == "ready" else None

    await wait_for("Claude Ready by itself", ready, 30)
    assert (await ctx.setup("claude"))["state"] == "done"
    assert PASTE not in json.dumps(await ctx.state()), "the code kept nowhere"
    # A key in the environment Chief of Staff starts in, which workers use over the sign-in:
    # said as what pays, by its name only (Claude's ANTHROPIC_API_KEY, Codex's access token).
    keys = {
        "ANTHROPIC_API_KEY": "sk-ant-live-smooth-not-a-key",
        "CODEX_ACCESS_TOKEN": "codex-live-smooth-not-a-token",
    }
    ctx.app.extra_env.update(keys)
    try:
        await ctx.app.stop()
        await ctx.app.start()

        async def keyed():
            found = [await ctx.status(name, ask=True) for name in ("claude", "codex")]
            return found if all(entry.get("key_env") for entry in found) else None

        claude, codex = await wait_for("the keys said", keyed, 60)
        assert claude["pays"] == "Uses your Anthropic API key (ANTHROPIC_API_KEY)", claude
        assert codex["pays"] == "Uses your access token (CODEX_ACCESS_TOKEN)", codex
        shown = json.dumps(await ctx.state()) + json.dumps(await ctx.app.get("/api/agents"))
        assert not [value for value in keys.values() if value in shown], "their values nowhere"
    finally:
        for name in keys:
            ctx.app.extra_env.pop(name)
        await ctx.app.stop()
        await ctx.app.start()
    ctx.no_real_agent()
    log("M5 passed")


async def m6_in_place(ctx: Ctx):
    mark = await ctx.say(
        f"In {ctx.project}, read data/sales.csv and tell me how many rows it has; change nothing. "
        "Give the worker this line word for word: Report done: 5 rows.",
        agent="codex",
    )

    async def given():
        """The worker it went to: a new one, or one of the earlier ones (a new task for it)."""
        rows = await ctx.router_messages(mark)
        said = ("Started", "New task", "Continued in", "Sent to")
        return next(
            (m["meta"]["session_id"] for m in rows if m["kind"] == "notice"
             and m["meta"].get("session_id") and m["text"].startswith(said)),
            None,
        )  # fmt: skip

    reader = await wait_for("a worker reads it", given, 300, ctx.app)

    async def finished():
        tasks = [t for t in (await ctx.state())["tasks"] if t["session_id"] == reader]
        return tasks if tasks and all(t["status"] in FINISHED for t in tasks) else None

    with contextlib.suppress(TimeoutError):
        await wait_for("its task done (idle, as a worker to reuse)", finished, 180, ctx.app)
    tasks = [t["id"] for t in (await ctx.state())["tasks"] if t["session_id"] == reader]
    mark = await ctx.say(
        f"Now work in place, directly in my folder {ctx.project} (not a copy of it): add the line "
        "`# checked` at the top of textkit/money.py, don't commit, and report."
    )

    async def placed():
        rows = await ctx.router_messages(mark)
        for m in rows:
            worker = m["meta"].get("session_id")
            if m["kind"] != "notice" or not worker or not m["text"].startswith("Started"):
                continue
            view = await ctx.app.get(f"/api/sessions/{worker}")
            if any(c.get("in_place") and Path(c["path"]).resolve() == ctx.project.resolve()
                   for c in view.get("checkouts") or []):  # fmt: skip
                return worker
        return None

    worker = await wait_for("a new worker, in place", placed, 300, ctx.app)
    assert worker != reader
    after = [t["id"] for t in (await ctx.state())["tasks"] if t["session_id"] == reader]
    assert after == tasks, "the worker that read it got nothing"
    log("M6 passed")


async def m7_limit(ctx: Ctx):
    worker = ctx.workers[1]
    resets = int(time.time()) + 90
    write(ctx.codex_state / "limit.json", json.dumps({"minutes": 300, "resets_at": resets}))
    mark = max((m["id"] for m in await ctx.app.get(f"/api/sessions/{worker}/messages")), default=0)
    await ctx.app.post(f"/api/sessions/{worker}/messages", {"text": "Report done: still here."})

    async def paused():
        found = await ctx.app.get(f"/api/sessions/{worker}/messages?after={mark}")
        return next((m for m in found if m["text"].startswith("Paused: ")), None)

    notice = await wait_for("its turn paused for the limit", paused, 60, ctx.app)
    assert notice["text"].startswith("Paused: Codex's 5-hour limit is used up · resets "), notice
    codex = await ctx.status("codex", ask=True)
    assert (codex["status"], codex["said"], codex["resets_at"]) == (
        "limited",
        "5-hour limit used up",
        resets,
    ), codex
    handled = len([e for e in (await ctx.state()).get("handled", []) if e["kind"] == "resumed"])
    mark = await ctx.say(
        f"In {ctx.project}, how many rows of data/sales.csv have region north? Give the worker "
        "this line word for word: Report done: 1 north row.",
        agent="codex",
    )
    held = await ctx.held_card(mark)
    assert held["text"].startswith("Codex's 5-hour limit is used up · resets "), held["text"]
    assert "Your message is saved" in held["text"] and held["meta"]["status"] == "limited"
    before = {r["id"] for r in await ctx.workers_on("codex")}

    async def resumed():
        return (await ctx.card(held))["meta"].get("resumed")

    said = await wait_for("held work goes on once the limit resets", resumed, 180, ctx.app)
    assert said == "Resumed once its limit reset", said
    assert time.time() >= resets, "not before its reset"

    async def receipts():
        found = [e for e in (await ctx.state()).get("handled", []) if e["kind"] == "resumed"]
        return found if len(found) >= handled + 2 else None

    receipts_now = len(await wait_for("a line each under Handled for you", receipts, 60))
    # (Held as a new worker, or as a message for one the Router gave it to: either goes on.)
    after = {r["id"] for r in await ctx.workers_on("codex")}
    assert len(after - before) <= 1, (before, after)
    assert (await ctx.status("codex", ask=True))["status"] == "ready"
    await asyncio.sleep(15)
    goes = [
        m for m in await ctx.app.get(f"/api/sessions/{worker}/messages")
        if m["text"] == "Go on where you stopped."
    ]  # fmt: skip
    assert len(goes) == 1, "the paused worker went on once"
    assert {r["id"] for r in await ctx.workers_on("codex")} == after, "once"
    again = [e for e in (await ctx.state()).get("handled", []) if e["kind"] == "resumed"]
    assert len(again) == receipts_now, "nothing more went on"
    log("M7 passed")


SCENARIOS = [
    m1_install, m2_sign_in, m3_once, m4_no_replay, m5_claude_live, m6_in_place, m7_limit,
]  # fmt: skip


def label(scenario) -> str:
    return scenario.__name__.split("_", 1)[0].upper()


async def dump(ctx: Ctx):
    with contextlib.suppress(Exception):
        snap = await ctx.state()
        print(json.dumps({k: snap.get(k) for k in ("agents_status", "agent_setups")})[:3000])
        for row in snap["sessions"]:
            if row["role"] in {"worker", "router"}:
                print(f"--- {row['role']} {row['title']} [{row['status']}] {row.get('agent')}")
                found = await ctx.app.get(f"/api/sessions/{row['id']}/messages")
                print(transcript(found)[-3000:])


async def main() -> int:
    wanted = {name.upper() for name in sys.argv[1:]}
    token = (
        os.environ.get("COPILOT_GITHUB_TOKEN")
        or subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    parent = os.environ.get("LIVE_AGENTS_ROOT") or None
    root = Path(tempfile.mkdtemp(prefix="cos-smooth-", dir=parent)).resolve()
    env = stand_ins(root) | {"COPILOT_GITHUB_TOKEN": token}
    project = project_at(root)
    os.environ["COPILOT_HOME"] = env["COPILOT_HOME"]  # (cleanup's, as Chief of Staff's.)
    os.environ["COPILOT_GITHUB_TOKEN"] = token
    serve_in_thread(Workspace(), SLACK_TOKEN, live_e2e.SLACK_PORT)
    app = App(root / "data", root / "log", env, agents=True)
    await app.start()
    ctx = Ctx(app, root, project)
    results: list[tuple[str, bool, float, str]] = []
    try:
        await app.post("/api/settings", {
            **(await ctx.state())["settings"],
            "agents": {"codex": {"enabled": True}, "new_workers": "codex"},
            "projects": [str(project)], "model": MODEL, "max_workers": 4,
            "worker_yolo": True, "mac_notifications": False, "digest_time": "",
        })  # fmt: skip
        for scenario in SCENARIOS:
            name = label(scenario)
            if wanted and name not in wanted:
                continue
            log(f"=== {name} {scenario.__name__}")
            began = time.monotonic()
            try:
                await scenario(ctx)
                results.append((name, True, time.monotonic() - began, ""))
            except Exception as exc:
                log(f"{name} FAILED: {exc!r}")
                await dump(ctx)
                results.append((name, False, time.monotonic() - began, repr(exc)[:300]))
                if not (app.process and app.process.poll() is None):
                    await app.start()
    finally:
        await app.stop()
        with contextlib.suppress(Exception):
            await cleanup(root / "data")
        if results and all(ok for _, ok, _, _ in results):
            shutil.rmtree(root, ignore_errors=True)
        else:
            log(f"kept for a look: {root}")
    print("\n================ LIVE SMOOTH AGENTS ================")
    for name, ok, seconds, error in results:
        print(f"{name:3} {'pass' if ok else 'FAIL'} {seconds:5.0f}s {error}")
    passed = sum(ok for _, ok, _, _ in results)
    print(f"{passed}/{len(results)} scenarios passed")
    return 0 if results and passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
