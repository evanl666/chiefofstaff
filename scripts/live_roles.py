"""Live: Chief of Staff's own sessions on another coding agent (agent-roles.md): the Router on
Codex and on Claude (real: the owner's own CLIs, their sign-ins), Watch for you, and the Router
on Copilot while its agent can't run it. Workers stay on Copilot (real). Each shard is its own
Chief of Staff; each scenario passes or fails on its own (a failure dumps the sessions and the
shard goes on); the summary lists each, with the agent's tokens and the Copilot credits spent.

Shard R, the Router on Codex (Settings → Coding agents → The Router runs on Codex):
R1  told something on Copilot, put on Codex: one line says so, and it still knows it
R2  routing: a question it answers itself, no worker started
R3  a task: a worker (on Copilot) does it; its report comes back through the Router
R4  the owner's standing decision, recorded (record_decision) as on Copilot
R5  back on Copilot: one line, and what it was told on Codex comes with it

Shard S, the same on Claude (Claude's cheapest model, LIVE_CLAUDE_MODEL, default "haiku"; the
owner's Claude files recorded before and checked after, and the run's one Claude budget, as
scripts/live_claude.py has them: LIVE_CLAUDE_BUDGET, default 3,000,000 tokens, shared through
LIVE_CLAUDE_SPENT by every suite of the run LIVE_CLAUDE_RUN names; a scenario stops as it runs
out, and the shard stops at a limit of the owner's plan):
S1-S5 as R1-R5

Shard W, Watch for you (Claude on):
W1  while what reads Slack doesn't run on Claude (agents.SLACK_READERS), Settings offers no
    choice for it and a scan runs on Copilot; once it does, put on Claude, a scan completes there

Shard F, Copilot while the agent can't run it, and back (a `codex` on the shard's PATH that is
the owner's own, run with a Codex folder of the shard's own, empty, while a flag file exists):
F1  Codex signed out: the Router stays on Copilot, says why in one line, and answers there
F2  signed in again (the flag gone): the Router is back on Codex by itself, and answers there

    env LIVE_AGENTS_ROOT=<tree>/.cos-live \
        uv run --extra claude --with playwright python scripts/live_roles.py R|S|W|F [R1 R3 …]

(no shard: R, S, W and F, one after another; once Claude stops one, no Claude shard runs). For
one budget across suites, give each the same LIVE_CLAUDE_SPENT file. Ports 20500-20513.
"""

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_agents as la
import live_claude as lc
import live_e2e
from fake_slack_mcp import Workspace, serve_in_thread
from live_e2e import SLACK_TOKEN, App, cleanup, git, log, wait_for
from live_parity import setup

SHARDS = {"R": (20500, 20510), "S": (20501, 20511), "W": (20502, 20512), "F": (20503, 20513)}
CLAUDE_SHARDS = {"S", "W"}
CODEX = shutil.which("codex") or "codex"
NOTICE_ON = "The {label} runs on {agent} now, with a summary of what it knew."


class Ctx(lc.Ctx):
    agent: str = "codex"

    def agent_tokens(self, session_id: str = "") -> int:
        query = "SELECT tokens FROM agent_usage WHERE agent=?"
        args: list = [self.agent]
        if session_id:
            query += " AND session_id=?"
            args.append(session_id)
        return sum(t for (t,) in self.rows(query, *args))

    def threads(self) -> list[str]:
        """The Codex threads the Router had (each an `alias:` of it that is not a uuid)."""
        router = self.rows("SELECT value FROM kv WHERE key='router_id'")
        if not router:
            return []
        return [
            key.split(":", 1)[1]
            for key, value in self.rows("SELECT key, value FROM kv WHERE key LIKE 'alias:%'")
            if value == router[0][0]
        ]


def label(agent: str) -> str:
    return {"codex": "Codex", "claude": "Claude", "copilot": "Copilot"}[agent]


async def router_view(ctx: Ctx) -> dict:
    router = (await la.state(ctx))["router_id"]
    return await la.session(ctx, router)


async def put(ctx: Ctx, role: str, agent: str):
    """Settings → Coding agents: where a role runs."""
    on = {ctx.agent: {"enabled": True, **({"model": lc.CLAUDE_MODEL} if ctx.agent == "claude"
                                          else {})}}  # fmt: skip
    await la.settings(ctx, agents={**on, "roles": {role: agent}})


async def ask(ctx: Ctx, text: str, timeout: float = 300) -> str:
    router, mark = await la.say(ctx, text)
    return await la.router_said(ctx, router, mark, timeout)


async def notice_since(ctx: Ctx, mark: int, text: str, timeout: float = 120) -> str:
    router = (await la.state(ctx))["router_id"]

    async def found():
        return next((n for n in await la.notices(ctx, router, mark) if n == text), None)

    return await wait_for(f"the line {text!r}", found, timeout, ctx.app)


async def router_mark(ctx: Ctx) -> int:
    return await la.mark_of(ctx, (await la.state(ctx))["router_id"])


# Shards R and S: the Router on an agent


async def told_then_moved(ctx: Ctx):
    said = await ask(ctx, "Please remember for later: my favourite fruit is mango. Just say ok.")
    log(f"{ctx.shard}1 on Copilot: {said[:120]!r}")
    assert not (await router_view(ctx)).get("agent"), "on Copilot first"
    mark = await router_mark(ctx)
    await put(ctx, "router", ctx.agent)
    await notice_since(ctx, mark, NOTICE_ON.format(label="Router", agent=label(ctx.agent)))
    view = await router_view(ctx)
    assert view.get("agent") == ctx.agent, view.get("agent")
    said = await ask(ctx, "What is my favourite fruit? One word.")
    log(f"{ctx.shard}1 on {label(ctx.agent)}: {said[:200]!r}")
    assert "mango" in said.lower(), said
    assert ctx.agent_tokens(view["id"]) > 0, "its turn ran on the agent"


async def routing(ctx: Ctx):
    workers = len([s for s in (await la.state(ctx))["sessions"] if s["role"] == "worker"])
    said = await ask(ctx, "What is 17 times 23? Answer it yourself in one line; start no worker.")
    log(f"{ctx.shard}2: {said[:200]!r}")
    assert "391" in said, said
    now = len([s for s in (await la.state(ctx))["sessions"] if s["role"] == "worker"])
    assert now == workers, "no worker for that"
    assert (await router_view(ctx)).get("agent") == ctx.agent


async def task_and_report(ctx: Ctx):
    router, mark = await la.say(
        ctx,
        f"In {ctx.project}: how many data rows (not counting the header) are in "
        "data/sales.csv? Don't change any files.",
    )
    worker = await la.assigned(ctx, router, mark)
    view = await la.session(ctx, worker)
    assert not view.get("agent"), "the worker is on Copilot, as Settings has new workers"
    task = await la.task_ended(ctx, worker, "the row count")
    log(f"{ctx.shard}3: {task['result'][:200]!r}")
    assert "540" in task["result"]

    async def heard():
        rows = await la.messages(ctx, router, prompts=True)
        got = [m for m in rows if m["id"] > mark and "540" in (m["meta"].get("prompt") or "")]
        return got and (await la.session(ctx, router))["status"] == "idle"

    await wait_for("the report reaches the Router", heard, 300, ctx.app)
    assert (await router_view(ctx)).get("agent") == ctx.agent


async def decision(ctx: Ctx):
    said = await ask(
        ctx,
        "A standing decision from now on: release notes are written in British English. "
        "Record it as a decision, then confirm in one line.",
    )
    log(f"{ctx.shard}4: {said[:200]!r}")

    def recorded():
        rows = ctx.rows("SELECT text FROM decisions WHERE status='active'")
        return [text for (text,) in rows if "british" in text.lower()]

    found = await la.until("the decision is recorded", recorded, 120)
    log(f"{ctx.shard}4 recorded: {found}")


async def back_to_copilot(ctx: Ctx):
    said = await ask(ctx, "Also remember: my favourite colour is teal. Just say ok.")
    log(f"{ctx.shard}5 on {label(ctx.agent)}: {said[:120]!r}")
    mark = await router_mark(ctx)
    await put(ctx, "router", "copilot")
    await notice_since(ctx, mark, NOTICE_ON.format(label="Router", agent="Copilot"))
    assert not (await router_view(ctx)).get("agent"), "on Copilot"
    said = await ask(ctx, "What are my favourite fruit and colour? Two words.")
    log(f"{ctx.shard}5 on Copilot: {said[:200]!r}")
    assert "mango" in said.lower() and "teal" in said.lower(), said


async def r1_told_then_moved(ctx: Ctx):
    await told_then_moved(ctx)


async def r2_routing(ctx: Ctx):
    await routing(ctx)


async def r3_task_and_report(ctx: Ctx):
    await task_and_report(ctx)


async def r4_decision(ctx: Ctx):
    await decision(ctx)


async def r5_back_to_copilot(ctx: Ctx):
    await back_to_copilot(ctx)


async def s1_told_then_moved(ctx: Ctx):
    await told_then_moved(ctx)


async def s2_routing(ctx: Ctx):
    await routing(ctx)


async def s3_task_and_report(ctx: Ctx):
    await task_and_report(ctx)


async def s4_decision(ctx: Ctx):
    await decision(ctx)


async def s5_back_to_copilot(ctx: Ctx):
    await back_to_copilot(ctx)


# Shard W: Watch for you


async def w1_watch_for_you(ctx: Ctx):
    from chiefofstaff import agents

    looked = await ctx.app.get("/api/agents")
    roles = {role["role"]: role for role in looked.get("roles") or []}
    held = "claude" not in agents.role_agents("connector")
    await la.settings(
        ctx,
        watches=[{"what": "Direct messages and mentions of me on Slack", "enabled": True}],
        watch_after_hours_minutes=5,
    )
    if held:
        assert "connector" not in roles and "slack" not in roles, sorted(roles)
    else:
        assert roles["connector"]["agents"] == ["claude"], roles["connector"]
        await put(ctx, "connector", "claude")
    await ctx.app.post("/api/watch/scan", {})

    async def scanned():
        # Its report in (Settings' "scanned at", whatever it covered), its run over.
        found = (await la.state(ctx))["connector"]
        if not found["scanned_at"] or found["scanning"] or not found["session_id"]:
            return None
        return await la.session(ctx, found["session_id"])

    view = await wait_for("the scan completes", scanned, 600, ctx.app)
    want = None if held else "claude"
    assert view.get("agent") == want, (view.get("agent"), want)
    log(f"W1 passed: scanned on {label(want or 'copilot')}")


# Shard F: Copilot while Codex can't run it, and back


def codex_wrapper(root: Path) -> tuple[Path, Path]:
    """A `codex` that is the owner's own, run with an empty Codex folder of the shard's own
    while the flag exists (signed out): its folder for the PATH, and the flag."""
    folder, flag, empty = root / "codex-path", root / "signed-out.flag", root / "codex-empty"
    folder.mkdir()
    empty.mkdir()
    script = folder / "codex"
    script.write_text(
        "#!/bin/sh\n"
        f'if [ -e "{flag}" ]; then CODEX_HOME="{empty}"; export CODEX_HOME; fi\n'
        f'exec "{CODEX}" "$@"\n'
    )
    script.chmod(0o755)
    flag.touch()
    return folder, flag


async def f1_signed_out(ctx: Ctx):
    mark = await router_mark(ctx)
    await put(ctx, "router", "codex")

    async def said_why():
        found = await la.notices(ctx, (await la.state(ctx))["router_id"], mark)
        return next((n for n in found if n.startswith("The Router runs on Copilot for now:")),
                    None)  # fmt: skip

    line = await wait_for("the line saying why", said_why, 120, ctx.app)
    log(f"F1: {line!r}")
    assert "codex" in line.lower(), line
    assert not (await router_view(ctx)).get("agent"), "on Copilot meanwhile"
    looked = await ctx.app.get("/api/agents")
    router = next(role for role in looked["roles"] if role["role"] == "router")
    assert router["chosen"] == "codex" and router["line"].startswith("On Copilot for now:")
    said = await ask(ctx, "What is 2 plus 3? One number.")
    assert "5" in said, said


async def f2_back_by_itself(ctx: Ctx):
    mark = await router_mark(ctx)
    ctx.notes["flag"].unlink()
    await notice_since(ctx, mark, NOTICE_ON.format(label="Router", agent="Codex"), 600)
    view = await router_view(ctx)
    assert view.get("agent") == "codex", view.get("agent")
    said = await ask(ctx, "What is 4 plus 5? One number.")
    assert "9" in said, said
    assert ctx.agent_tokens(view["id"]) > 0, "that turn ran on Codex"


SCENARIOS: dict[str, list] = {
    "R": [r1_told_then_moved, r2_routing, r3_task_and_report, r4_decision, r5_back_to_copilot],
    "S": [s1_told_then_moved, s2_routing, s3_task_and_report, s4_decision, s5_back_to_copilot],
    "W": [w1_watch_for_you],
    "F": [f1_signed_out, f2_back_by_itself],
}


async def archive(ctx: Ctx):
    """The Codex threads the run's Router had, archived in the owner's Codex."""
    from chiefofstaff.backend_codex import CodexBackend

    threads = [thread for thread in ctx.threads() if "-" in thread]
    if not threads:
        return
    codex = CodexBackend(CODEX)
    await codex.start()
    try:
        for thread in threads:
            with contextlib.suppress(Exception):
                await codex.archive(thread)
    finally:
        await codex.stop()


async def run_shard(
    shard: str, wanted: set[str], spent_before: int = 0
) -> tuple[list[tuple[str, bool, float, str]], int, str]:
    """Its results, the Claude tokens it spent and why it stopped early ("": it did not)."""
    port, slack_port = SHARDS[shard]
    live_e2e.PORT, live_e2e.SLACK_PORT = port, slack_port
    live_e2e.BASE = f"http://127.0.0.1:{port}"
    serve_in_thread(Workspace(), SLACK_TOKEN, slack_port)
    parent = os.environ.get("LIVE_AGENTS_ROOT") or None
    if parent:
        Path(parent).mkdir(parents=True, exist_ok=True)  # (Named, not made yet: made here.)
    root = Path(tempfile.mkdtemp(prefix=f"cos-roles-{shard}-", dir=parent)).resolve()
    home, project, truth = setup(root)
    agent = "claude" if shard in CLAUDE_SHARDS else "codex"
    record, allowed = root / "owner-files.json", ""
    if agent == "claude":
        allowed = await asyncio.to_thread(lc.calibrated, root, record)
    env = {
        "COPILOT_HOME": str(home),
        "CHIEFOFSTAFF_MIN_FREE_MEMORY": "0",
        # Back on the agent soon after it can run the Router again (F2).
        "CHIEFOFSTAFF_ROLE_RETRY": "20",
        "CHIEFOFSTAFF_ROLE_GLANCE": "10",
    }
    if shard == "F":
        folder, flag = codex_wrapper(root)
        env["PATH"] = f"{folder}:{os.environ.get('PATH', '')}"
    app = App(root / "data", root / "log", env, agents=True)
    await app.start()
    ctx = Ctx(shard, app, root, home, project, truth)
    ctx.agent, ctx.base_env = agent, env
    if shard == "F":
        ctx.notes["flag"] = flag
    results, stopped = [], ""
    try:
        on = {"enabled": True, **({"model": lc.CLAUDE_MODEL} if agent == "claude" else {})}
        await la.settings(
            ctx,
            agents={agent: on, "new_workers": "copilot"},
            projects=[str(project)], model=la.MODEL, max_workers=4, mac_notifications=False,
            digest_time="", worker_yolo=True,
        )  # fmt: skip
        for scenario in SCENARIOS[shard]:
            name = la.label(scenario)
            if wanted and name not in wanted:
                continue
            log(f"=== {name} {scenario.__name__}")
            started = time.monotonic()
            if agent == "claude":
                outcome = await lc.within_budget(ctx, scenario, spent_before)
                lc.save_spent(spent_before + ctx.claude_tokens())
            else:
                outcome = await outcome_of(scenario(ctx))
            if outcome is None:
                results.append((name, True, time.monotonic() - started, ""))
            else:
                log(f"{name} FAILED: {outcome!r}")
                await la.dump(ctx)
                results.append((name, False, time.monotonic() - started, repr(outcome)[:300]))
                if not (ctx.app.process and ctx.app.process.poll() is None):
                    await ctx.app.start()
            if agent == "claude" and ctx.plan_stopped():
                stopped = "Claude refused a turn for a limit of the plan: stopped here"
                break
            if agent == "claude" and spent_before + ctx.claude_tokens() >= lc.BUDGET:
                stopped = f"the budget ({lc.BUDGET:,} Claude tokens) is spent: stopped here"
                break
        ctx.notes["tokens"] = ctx.agent_tokens()
        ctx.notes["credits"] = ctx.copilot_credits()
    finally:
        await app.stop()
        log_copy = root.parent / f"cos-roles-{shard}-{int(time.time())}.log"
        with contextlib.suppress(OSError):
            log_copy.write_text((root / "log").read_text())
        if agent == "codex":
            with contextlib.suppress(Exception):
                await archive(ctx)
        with contextlib.suppress(Exception):
            await cleanup(root / "data")
        with contextlib.suppress(Exception):
            git(project, "worktree", "prune")
        if agent == "claude":
            try:
                log(
                    lc.owners_claude_files("check", record, "--live", str(root), "--allow", allowed)
                )
            except AssertionError as exc:
                results.append(("OWNER", False, 0.0, str(exc)[:300]))
        if results and all(ok for _, ok, _, _ in results):
            shutil.rmtree(root, ignore_errors=True)
        else:
            log(f"kept for a look: {root}; log {log_copy}")
    print(f"\n================ LIVE ROLES, shard {shard} ================")
    for name, ok, seconds, error in results:
        print(f"{name:5} {'pass' if ok else 'FAIL'} {seconds:5.0f}s {error}")
    if stopped:
        print(f"STOPPED: {stopped}")
    print(f"{label(agent)} tokens: {ctx.notes.get('tokens', 0):,}; Copilot credits: "
          f"{ctx.notes.get('credits', 0):.1f}")  # fmt: skip
    return results, ctx.notes.get("tokens", 0) if agent == "claude" else 0, stopped


async def outcome_of(running) -> Exception | None:
    try:
        await running
    except Exception as exc:
        return exc
    return None


async def main() -> int:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        print("Run with the claude extra: uv run --extra claude --with playwright python ...")
        return 2
    shards = [sys.argv[1].upper()] if len(sys.argv) > 1 else list(SCENARIOS)
    wanted = {name.upper() for name in sys.argv[2:]}
    results, stopped, spent = [], "", lc.run_spent()
    if CLAUDE_SHARDS & set(shards):
        log(f"run {lc.RUN!r}: {spent:,} of {lc.BUDGET:,} Claude tokens spent before ({lc.SPENT})")
    for shard in shards:
        # (Once Claude stopped one, no Claude shard runs; the others do.)
        if shard in CLAUDE_SHARDS and stopped:
            continue
        if shard in CLAUDE_SHARDS and spent >= lc.BUDGET:
            stopped = f"the run's budget ({lc.BUDGET:,} Claude tokens) is spent: {shard} not run"
            continue
        found, tokens, why = await run_shard(shard, wanted, spent)
        results += found
        if shard in CLAUDE_SHARDS:
            spent += tokens
            lc.save_spent(spent)
            stopped = why
    passed = sum(ok for _, ok, _, _ in results)
    print(f"{passed}/{len(results)} scenarios passed"
          + (f"; {spent:,} Claude tokens in run {lc.RUN!r}" if CLAUDE_SHARDS & set(shards) else "")
          + (f"; STOPPED: {stopped}" if stopped else ""))  # fmt: skip
    return 0 if results and passed == len(results) and not stopped else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
