"""Downgrade test (docs/design/more-agents.md, section 8): an earlier Chief of Staff, which runs
every worker on Copilot, opening a data directory where workers ran on Codex. With
`--case teams` (teams-v2.md, Teams lite; team-page-v4): a team this tree made, told something
from its page (a part not started yet keeping it for its start) and looked at, 0.1.56 working
in its folder, then this tree again.

    uv run python scripts/downgrade.py [--baseline v0.1.42]
    uv run python scripts/downgrade.py --case teams --baseline v0.1.56

1. This tree, with Codex on (the stand-in CLI, tests/fake_codex.py, as `codex` on the PATH) and
   one worker slot: a Codex worker that finished (idle), one at work with a command it left
   running (a stand-in dev server), one queued behind it, and a Copilot worker at work. Then
   Chief of Staff is killed (SIGKILL): within ten seconds no process of Codex may remain, the
   command left running included (the supervisor ends them).
2. The baseline (a git worktree of BASELINE, in its own `uv run` environment, on a scripted
   Copilot that records what it is asked) opens that data directory: it may never create a
   Copilot session for a Codex worker; the one at work and the queued one fail with their
   tasks; a message to the idle one fails in its chat; it never runs `codex`.

Prints each check; exits 1 when one fails. Its folders (browser-shots/downgrade-work) are
removed at the end, whatever happened.
"""

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = Path(__file__).resolve()
WORK = ROOT / "browser-shots" / "downgrade-work"
BASELINE = "v0.1.42"
SETTLE = 8.0  # Seconds the baseline gets to recover what it found, before it is looked at.


# The sides, each in its own tree's environment (--side).


class Tokens:
    """No Slack token (never the owner's keychain)."""

    def get(self):
        return ""

    def save(self, token):
        pass

    def delete(self):
        pass


async def quiet(title, text):
    pass


async def until(check, seconds: float = 30):
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("timed out")
        await asyncio.sleep(0.05)


def runtime_for(data: Path, backend):
    from chiefofstaff.bus import Bus
    from chiefofstaff.runtime import Runtime
    from chiefofstaff.sources.slack import SlackSource
    from chiefofstaff.store import Store

    store = Store(data)
    runtime = Runtime(store, Bus(), backend_factory=lambda settings: backend, notifier=quiet)
    runtime.slack = SlackSource(store, runtime, tokens=Tokens())
    return store, runtime


async def started(runtime):
    runtime.loop = asyncio.get_running_loop()
    runtime.bus.bind(runtime.loop)
    await runtime.connect()


async def new_side(run: Path):
    """This tree: Codex workers idle, at work (with a command left running) and queued."""
    from copilot.tools import ToolInvocation

    from chiefofstaff import agents
    from chiefofstaff.models import Settings
    from chiefofstaff.scripted import ScriptedBackend

    store, runtime = runtime_for(run / "data", ScriptedBackend())
    store.save_settings(Settings(max_workers=1, projects=[]))
    await started(runtime)
    before = agents.configured(store)
    agents.save(store, agents.planned(store, {"codex": {"enabled": True}, "new_workers": "codex"}))
    await runtime.agents_saved(before)
    router = store.get("router_id")

    async def spawn(title: str, text: str, agent: str = "") -> str:
        tool = runtime.toolset.spawn_on(["codex"])
        arguments = {"title": title, "instructions": text, "why_new": "t"}
        result = await tool.handler(
            ToolInvocation(
                session_id=router,
                tool_call_id="t",
                tool_name="spawn_worker",
                arguments=arguments | ({"agent": agent} if agent else {}),
            )
        )
        if result.result_type == "failure":
            raise RuntimeError(result.error)
        return json.loads(result.text_result_for_llm)["session_id"]

    idle = await spawn("Fix the parser", "Report done: Fixed it.")
    await until(lambda: store.session(idle)["status"] == "done")
    serving = await spawn("Serve the docs", "Leave the command `sleep 600` running. Wait 600 s")
    await until(
        lambda: (
            store.session(serving)["status"] == "working"
            and bool(store.all("SELECT 1 FROM commands WHERE session_id=?", (serving,)))
        )
    )
    queued = await spawn("Check the links", "Report done: Checked.")
    copilot = await spawn("Write the notes", "Write the notes.", agent="copilot")
    await asyncio.sleep(0.5)
    backend = runtime.agents["codex"].backend
    found = {
        "cos": os.getpid(),  # Chief of Staff itself (not `uv run`, which started it).
        "pids": backend.processes(),
        "rows": {"idle": idle, "serving": serving, "queued": queued, "copilot": copilot},
        "status": {key: store.session(key)["status"] for key in (idle, serving, queued)},
        "threads": {key: store.get(f"copilot:{key}") for key in (idle, serving, queued)},
    }
    print("downgrade: ready " + json.dumps(found), flush=True)
    await asyncio.Event().wait()  # Killed here.


async def old_side(run: Path, rows: dict):
    """The baseline: what it does with the data directory Codex workers left."""
    from chiefofstaff.scripted import ScriptedBackend

    asked = []

    class Recording(ScriptedBackend):
        async def create_session(self, *, session_id, name, **options):
            asked.append(["create", session_id])
            return await super().create_session(session_id=session_id, name=name, **options)

        async def resume_session(self, session_id, **options):
            asked.append(["resume", session_id])
            return await super().resume_session(session_id, **options)

    store, runtime = runtime_for(run / "data", Recording())
    await runtime.start()  # As the app starts it: recovering what was at work.
    await asyncio.sleep(SETTLE)
    try:
        await runtime.owner_send(rows["idle"], "Also check the tests.")
        refused = ""
    except Exception as exc:  # What the owner would be told.
        refused = str(exc)
    await asyncio.sleep(2)
    codex = [rows[key] for key in ("idle", "serving", "queued")]
    found = {
        "asked": asked,
        "refused": refused,
        "status": {key: store.session(key)["status"] for key in codex},
        "tasks": {
            key: [task["status"] for task in runtime.tasks.for_session(key)] for key in codex
        },
        "said": {key: [[m["kind"], m["text"]] for m in store.messages(key)[-4:]] for key in codex},
    }
    print("downgrade: result " + json.dumps(found), flush=True)
    await runtime.stop()


# Teams lite (--case teams): this tree makes a team for one of the owner's requests (its
# passive stamps: req:, reqtasks:, with:, xchg:, from_task/to_task), the owner tells it something
# from its page and looks (teamtells:, teamnote: for the part not started, teamlook:, the
# messages' team and to_parts); 0.1.56 opens the folder and
# works in it (reopens and accepts a part, continues one, tells, splits and merges back, a failed
# part done instead by a task of its own); this tree again: no error, the team as its tasks are now.


def remembering():
    """The scripted backend, keeping its sessions from one run to the next as Copilot does."""
    from chiefofstaff.scripted import ScriptedBackend

    class Remembering(ScriptedBackend):
        async def resume_session(self, session_id, **options):
            self.histories.setdefault(session_id, [])
            return await super().resume_session(session_id, **options)

    return Remembering()


async def tool(runtime, name: str, caller: str, **arguments):
    from copilot.tools import ToolInvocation

    result = await runtime.tools[name].handler(
        ToolInvocation(session_id=caller, tool_call_id="t", tool_name=name, arguments=arguments)
    )
    text = result if isinstance(result, str) else result.text_result_for_llm
    if not isinstance(result, str) and result.result_type == "failure":
        raise RuntimeError(text)
    return text


async def teams_new(run: Path):
    from chiefofstaff.models import Settings

    store, runtime = runtime_for(run / "data", remembering())
    store.save_settings(Settings(max_workers=10, projects=[]))
    await started(runtime)
    router = store.get("router_id")
    asked = runtime.add_message(router, "owner", "Ship checkout v2: the API, then the UI.")
    runtime.states[router].turn_request = str(asked["id"])

    async def spawn(title: str, **extra) -> dict:
        arguments = {"title": title, "instructions": title, "why_new": "t", **extra}
        return json.loads(await tool(runtime, "spawn_worker", router, **arguments))

    api = await spawn("Checkout API: the v2 endpoints")
    ui = await spawn("Checkout UI: the v2 pages", works_with=[api["task_id"]])
    tests = await spawn("Checkout tests: end to end", after=[api["task_id"], ui["task_id"]])
    await tool(runtime, "tell_session", api["session_id"], session_id=ui["session_id"],
               text="Totals are in cents.")  # fmt: skip
    [team] = runtime.snapshot()["teams"]
    runtime.teams_lite.seen(team["id"], "k0156")
    parts = [member["id"] for member in team["members"]]
    await runtime.teams_lite.tell(team["id"], "Keep totals in cents.", parts, "kv4tell")
    runtime.teams_lite.look(team["id"])
    found = {
        "team": team["id"],
        "parts": [member["name"] for member in team["members"]],
        "rows": {"api": api, "ui": ui, "tests": tests},
        "keys": sorted({key.split(":", 1)[0] for key in store.keys("")}),
    }
    await runtime.stop()
    print("downgrade: ready " + json.dumps(found), flush=True)


async def teams_old(run: Path, rows: dict):
    store, runtime = runtime_for(run / "data", remembering())
    await runtime.start()
    await asyncio.sleep(2)
    router = store.get("router_id")
    api, ui, tests = rows["api"], rows["ui"], rows["tests"]

    def idle(row: dict) -> bool:
        return store.session(row["session_id"])["status"] not in {"working", "queued", "starting"}

    def status(row: dict) -> str:
        return runtime.tasks.get(row["task_id"])["status"]

    async def reported(row: dict, summary: str):
        await until(lambda: idle(row) and status(row) in {"running", "review"})
        await tool(runtime, "report", row["session_id"], status="done", summary=summary,
                   task_id=row["task_id"])  # fmt: skip

    # (The tests wait, queued, for the API and the UI: `after`.)
    await until(lambda: idle(api) and idle(ui))
    done = []
    await reported(api, "The v2 endpoints are in.")
    runtime.task_actions.accept(api["task_id"])
    await runtime.task_actions.follow_up(api["task_id"], "Also return the currency.")
    await reported(api, "They return the currency too.")
    runtime.task_actions.accept(api["task_id"])
    done.append("reopened and accepted")
    await tool(
        runtime, "message_session", router, session_id=ui["session_id"], task="continue",
        task_id=ui["task_id"], text="Also show the currency.",
    )  # fmt: skip
    await until(lambda: runtime.tasks.get(ui["task_id"])["status"] == "review" and idle(ui))
    runtime.task_actions.accept(ui["task_id"])
    done.append("continued and accepted")
    await tool(runtime, "tell_session", ui["session_id"], session_id=tests["session_id"],
               text="The pages are on staging.")  # fmt: skip
    done.append("told")
    split = runtime.task_actions.split(api["task_id"], "Currency in the API")
    runtime.task_actions.merge_back(split["id"])
    done.append("split and merged back")
    runtime.tasks.move(tests["task_id"], "failed")
    instead = json.loads(
        await tool(runtime, "spawn_worker", router, title="Checkout tests on Linux",
                   instructions="Run them on Linux.", why_new="t", replaces=[tests["task_id"]])
    )  # fmt: skip
    done.append("replaced")
    found = {
        "done": done,
        "instead": instead,
        "status": {key: runtime.tasks.get(row["task_id"])["status"] for key, row in rows.items()},
    }
    await runtime.stop()
    print("downgrade: result " + json.dumps(found), flush=True)


async def teams_again(run: Path):
    store, runtime = runtime_for(run / "data", remembering())
    await runtime.start()
    await asyncio.sleep(2)
    teams = runtime.snapshot()["teams"]
    pages = [runtime.teams_lite.page(team["id"]) for team in teams]
    found = {
        "seen": [store.get(f"teamseen:{team['id']}") for team in teams],
        "looked": [bool(store.get(f"teamlook:{team['id']}")) for team in teams],
        "tells": [
            [
                (entry["text"], [(item["how"], item["delivery"]) for item in entry["to"]])
                for entry in page["tells"]
            ]
            for page in pages
        ],
        "teams": [
            {
                "id": team["id"],
                "members": [
                    {key: member[key] for key in ("name", "task", "status", "moved_out")}
                    for member in team["members"]
                ],
            }
            for team in teams
        ],
        "told": [
            [item["title"] for item in page["items"] if item["kind"] == "tell"] for page in pages
        ],
    }
    await runtime.stop()
    print("downgrade: result " + json.dumps(found), flush=True)


async def teams_downgrade(baseline: str) -> int:
    run, tree = WORK / "run", WORK / "baseline"
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    env = environment(run)
    checks = Checks()
    try:
        print(f"This tree: a team for one of the owner's requests ({ROOT})", flush=True)
        new, found = await side(ROOT, env, "--side", "teams-new", "--run", str(run))
        await new.wait()
        checks.check(found["parts"] == ["API", "UI", "Tests"], "a team of three", found)
        stamps = {"req", "reqtasks", "teamseen", "with", "xchg", "teamtells", "teamnote"}
        stamps |= {"teamlook"}
        checks.check(stamps <= set(found["keys"]), "its stamps, in kv", found["keys"])
        rows = found["rows"]
        print(f"The baseline ({baseline}) works in that data directory", flush=True)
        subprocess.run(
            ["git", "worktree", "add", "--detach", "-f", str(tree), baseline],
            cwd=ROOT, check=True, capture_output=True,
        )  # fmt: skip
        old, result = await side(
            tree, env, "--side", "teams-old", "--run", str(run), "--rows", json.dumps(rows)
        )
        await old.wait()
        checks.check(
            result["done"]
            == [
                "reopened and accepted",
                "continued and accepted",
                "told",
                "split and merged back",
                "replaced",
            ],
            "it reopens and accepts a part, continues one, tells, splits and merges back, and "
            "replaces a part, as ever",
            result,
        )
        print("This tree again on that data directory", flush=True)
        again, back = await side(ROOT, env, "--side", "teams-again", "--run", str(run))
        await again.wait()
        teams = back["teams"]
        checks.check(len(teams) == 1 and teams[0]["id"] == found["team"], "the same team", teams)
        members = teams[0]["members"] if teams else []
        checks.check(
            [m["status"] for m in members]
            == [result["status"][key] for key in ("api", "ui", "tests")],
            "its parts as their tasks are now",
            members,
        )
        moved = members[2]["moved_out"] if len(members) == 3 else None
        checks.check(
            bool(moved) and moved["task"] == result["instead"]["task_id"],
            "a part done instead by a task of the baseline's (no request) has moved out",
            members,
        )
        checks.check(
            [(mark or "").split("|")[0] for mark in back["seen"]] == ["k0156"],
            "the owner's look, kept",
            back["seen"],
        )
        checks.check(
            back["looked"] == [True],
            "Since you looked: the owner's last look at its page, kept",
            back["looked"],
        )
        tells = back["tells"][0] if back["tells"] else []
        checks.check(
            len(tells) == 1
            and tells[0][0] == "Keep totals in cents."
            and tells[0][1][2] == ["start", "dropped"]
            and all(
                state == "dropped" if how == "start" else state not in {"failed", "dropped"}
                for how, state in tells[0][1]
            ),
            "what the owner told it: given to the parts at work; the note for a part not started "
            "then (the baseline started it: it knows no notes) never given",
            tells,
        )
        # From 0.1.57 on a tell between parts is stamped with their tasks (Teams lite): the
        # baseline's own tell is in the history too; before, it has no tasks and is not.
        stamped = (tree / "src" / "chiefofstaff" / "teams_lite.py").exists()
        told = (
            ["The pages are on staging", "Totals are in cents"]
            if stamped
            else ["Totals are in cents"]
        )
        checks.check(
            back["told"] == [told],
            "its history: what was handed on (the baseline's tell too, once it stamps its tasks)",
            back["told"],
        )
    finally:
        if tree.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(tree)], cwd=ROOT, capture_output=True
            )
            subprocess.run(["git", "worktree", "prune"], cwd=ROOT, capture_output=True)
        shutil.rmtree(WORK, ignore_errors=True)
    print("downgrade: " + ("passed" if not checks.failed else f"{checks.failed} check(s) failed"))
    return 1 if checks.failed else 0


# The run: both sides, one after the other, on one data directory.


def environment(run: Path) -> dict:
    """A PATH with the stand-in `codex` (each run listed) and a `gh` that is not signed in; an
    empty Copilot home; nothing of Chief of Staff's own variables."""
    bin_ = run / "bin"
    bin_.mkdir(parents=True)
    codex = bin_ / "codex"
    codex.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "tests" / "fake_codex.py"}" "$@"\n'
    )
    gh = bin_ / "gh"
    gh.write_text('#!/bin/sh\necho "gh: not here" >&2\nexit 1\n')
    for tool in (codex, gh):
        tool.chmod(0o755)
    (run / "copilot-home").mkdir()
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("VIRTUAL_ENV", "CHIEFOFSTAFF_"))
    }
    return env | {
        "PATH": f"{bin_}{os.pathsep}{env.get('PATH', '')}",
        "COPILOT_HOME": str(run / "copilot-home"),
        "FAKE_CODEX_HOME": str(run / "codex-home"),
        "FAKE_CODEX_RUNS": str(run / "codex-runs"),
        "CHIEFOFSTAFF_EVENT_DEBOUNCE": "0",
    }


async def side(tree: Path, env: dict, *arguments: str) -> tuple[asyncio.subprocess.Process, dict]:
    """A side running in its tree's environment, and what it printed once ready."""
    process = await asyncio.create_subprocess_exec(
        "uv", "run", "--quiet", "python", str(SCRIPT), *arguments,
        cwd=tree, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    assert process.stdout
    tail = []
    while raw := await asyncio.wait_for(process.stdout.readline(), 300):
        line = raw.decode(errors="replace")
        tail = [*tail[-40:], line]
        if line.startswith(("downgrade: ready ", "downgrade: result ")):
            return process, json.loads(line.split(" ", 2)[2])
    raise RuntimeError("The side stopped:\n" + "".join(tail))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Checks:
    def __init__(self):
        self.failed = 0

    def check(self, ok: bool, what: str, detail: object = ""):
        print(f"  {'ok  ' if ok else 'FAIL'} {what}" + ("" if ok else f": {detail}"), flush=True)
        self.failed += not ok


async def downgrade(baseline: str) -> int:
    run, tree = WORK / "run", WORK / "baseline"
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    env = environment(run)
    checks = Checks()
    started_pids: list[int] = []
    try:
        print(f"This tree, with Codex on: three Codex workers ({ROOT})", flush=True)
        new, found = await side(ROOT, env, "--side", "new", "--run", str(run))
        started_pids = [int(pid) for pid in found["pids"]]
        checks.check(found["status"][found["rows"]["serving"]] == "working", "a worker at work")
        checks.check(found["status"][found["rows"]["queued"]] == "queued", "a worker queued")
        checks.check(
            any("sleep 600" in command for command in found["pids"].values()),
            "its command left running is one of Codex's processes",
            found["pids"],
        )
        os.kill(found["cos"], signal.SIGKILL)
        await new.wait()
        deadline = time.monotonic() + 10
        while any(map(alive, started_pids)) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        left = [pid for pid in started_pids if alive(pid)]
        checks.check(not left, "killed: no Codex process left within ten seconds", left)
        ran_before = (run / "codex-runs").read_text().count("\n")

        print(f"The baseline ({baseline}) on that data directory", flush=True)
        subprocess.run(
            ["git", "worktree", "add", "--detach", "-f", str(tree), baseline],
            cwd=ROOT, check=True, capture_output=True,
        )  # fmt: skip
        old, result = await side(
            tree, env, "--side", "old", "--run", str(run), "--rows", json.dumps(found["rows"])
        )
        await old.wait()
        rows, threads = found["rows"], found["threads"]
        codex = {rows["idle"], rows["serving"], rows["queued"], *threads.values()}
        created = [key for kind, key in result["asked"] if kind == "create" and key in codex]
        checks.check(not created, "no Copilot session made for a Codex worker", created)
        for key in ("serving", "queued"):
            tasks = result["tasks"][rows[key]]
            checks.check(
                tasks and tasks[-1] == "failed",
                f"the {key} worker's task fails",
                (tasks, result["status"][rows[key]], result["said"][rows[key]]),
            )
        said = result["said"][rows["idle"]]
        checks.check(
            bool(result["refused"]) or any(kind == "error" for kind, _ in said),
            "a message to the idle worker fails, said in its chat",
            (result["refused"], said),
        )
        ran_after = (run / "codex-runs").read_text().count("\n")
        checks.check(
            ran_after == ran_before, "the baseline never runs codex", ran_after - ran_before
        )
    finally:
        for pid in started_pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        if tree.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(tree)], cwd=ROOT, capture_output=True
            )
            subprocess.run(["git", "worktree", "prune"], cwd=ROOT, capture_output=True)
        shutil.rmtree(WORK, ignore_errors=True)
    print("downgrade: " + ("passed" if not checks.failed else f"{checks.failed} check(s) failed"))
    return 1 if checks.failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--baseline", default=BASELINE)
    parser.add_argument("--case", choices=("codex", "teams"), default="codex")
    parser.add_argument("--side", choices=("new", "old", "teams-new", "teams-old", "teams-again"))
    parser.add_argument("--run", type=Path)
    parser.add_argument("--rows", default="{}")
    args = parser.parse_args()
    if args.side == "new":
        asyncio.run(new_side(args.run))
    elif args.side == "old":
        asyncio.run(old_side(args.run, json.loads(args.rows)))
    elif args.side == "teams-new":
        asyncio.run(teams_new(args.run))
    elif args.side == "teams-old":
        asyncio.run(teams_old(args.run, json.loads(args.rows)))
    elif args.side == "teams-again":
        asyncio.run(teams_again(args.run))
    elif args.case == "teams":
        sys.exit(asyncio.run(teams_downgrade(args.baseline)))
    else:
        sys.exit(asyncio.run(downgrade(args.baseline)))


if __name__ == "__main__":
    main()
