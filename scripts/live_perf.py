"""Performance gate (more-agents.md section 7, decision 18): the same projects, a worker on
Codex and one on Copilot (the Router on Copilot), cold and warm caches, five runs each. Passes
when, per project and cache state, the Codex worker's median install or build time is within 10%
(or 15 s, if that is more) of the Copilot worker's.

Each cache state is one command a worker runs, as given: `python3 bench.py cold|warm`, which
installs or builds five times and times each run inside (the receipt of the command, as the
agent ran it, is kept as well: what it took beyond the runs is the agent's own per-command cost).
The caches are this run's own (an empty one for each cold run, one warmed beforehand for the warm
runs), so nothing of the owner's is cleared. Projects go one at a time, the two agents in turn
(their order alternating), so the network's mood is shared.

Needs node with npm (pnpm and yarn run through npx), uv, python3, and a JDK and Gradle (PERF_TOOLS:
a folder with jdk/Contents/Home and gradle-*/; Gradle is left out without them).

    TMPDIR=… uv run python scripts/live_perf.py [npm pnpm yarn uv pip gradle]
"""

import asyncio
import contextlib
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
import live_parity  # noqa: F401  (it sets its own port: this one is set after)
from fake_slack_mcp import Workspace, serve_in_thread
from live_e2e import SLACK_TOKEN, App, cleanup, git, log, wait_for
from live_parity import setup, write

PORT, SLACK_PORT = 19110, 19160
MODEL = os.environ.get("LIVE_AGENTS_MODEL", "claude-sonnet-5.5")
RUNS = 5
TOOLS = Path(os.environ.get("PERF_TOOLS", "")).expanduser()

BENCH = '''"""Installs or builds this project RUNS times (cold: an empty cache each time; warm: the
cache warmed beforehand) and prints how long each run took."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parent
setup = json.loads((root / "bench.json").read_text())
mode = sys.argv[1]
runs = int(sys.argv[2]) if len(sys.argv) > 2 else setup["runs"]
caches = Path(setup["caches"])
for number in range(1, runs + 1):
    for name in setup["clean"]:
        path = root / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    if mode == "warm":
        cache = caches / ("warm-" + setup["tool"])
    else:
        cache = caches / ("cold-%s-%d-%d" % (setup["tool"], os.getpid(), number))
    env = dict(os.environ)
    env.update({k: v.replace("{cache}", str(cache)) for k, v in setup.get("env", {}).items()})
    started = time.monotonic()
    for step in setup["steps"]:
        step = [part.replace("{cache}", str(cache)) for part in step]
        done = subprocess.run(step, cwd=str(root), env=env, capture_output=True, text=True)
        if done.returncode:
            sys.stderr.write(done.stdout[-2000:] + done.stderr[-2000:])
            sys.exit(done.returncode)
    print("TIME %s %s %d %.2f" % (setup["tool"], mode, number, time.monotonic() - started))
    sys.stdout.flush()
    if mode == "cold":
        shutil.rmtree(str(cache), ignore_errors=True)
'''

NODE_DEPS = {
    "express": "4.21.2", "lodash": "4.17.21", "axios": "1.7.9", "dayjs": "1.11.13",
    "commander": "12.1.0", "zod": "3.24.1", "date-fns": "4.1.0", "uuid": "11.0.3",
}  # fmt: skip
PYTHON_DEPS = ["requests==2.32.3", "rich==13.9.4", "pydantic==2.10.4", "httpx==0.28.1"]


def projects(root: Path) -> dict[str, dict]:
    """Each project's files and its bench settings (steps run in order; {cache}: the run's)."""
    gradle = next(iter(sorted(TOOLS.glob("gradle-*/bin/gradle"))), None) if TOOLS.name else None
    jdk = TOOLS / "jdk" / "Contents" / "Home" if TOOLS.name else None
    package = json.dumps({"name": "perf", "version": "1.0.0", "private": True,
                          "dependencies": NODE_DEPS}, indent=1)  # fmt: skip
    found = {
        "npm": {
            "files": {"package.json": package},
            "steps": [["npm", "install", "--no-audit", "--no-fund", "--cache", "{cache}"]],
            "clean": ["node_modules"],
        },
        "pnpm": {
            "files": {"package.json": package},
            "steps": [["npx", "--yes", "pnpm@9.15.0", "install", "--store-dir", "{cache}"]],
            "clean": ["node_modules"],
        },
        "yarn": {
            "files": {"package.json": package},
            "steps": [
                [
                    "npx",
                    "--yes",
                    "yarn@1.22.22",
                    "install",
                    "--cache-folder",
                    "{cache}",
                    "--non-interactive",
                    "--no-progress",
                ]
            ],
            "clean": ["node_modules"],
        },
        "uv": {
            "files": {
                "pyproject.toml": '[project]\nname = "perf"\nversion = "0.1.0"\n'
                'requires-python = ">=3.9"\ndependencies = ' + json.dumps(PYTHON_DEPS) + "\n"
            },
            "steps": [["uv", "sync", "--cache-dir", "{cache}", "--python", "3.12"]],
            "clean": [".venv"],
        },
        "pip": {
            "files": {"requirements.txt": "\n".join(PYTHON_DEPS) + "\n"},
            "steps": [
                ["python3", "-m", "venv", ".venv"],
                [
                    ".venv/bin/pip",
                    "install",
                    "-q",
                    "--cache-dir",
                    "{cache}",
                    "-r",
                    "requirements.txt",
                ],
            ],
            "clean": [".venv"],
        },
    }
    if gradle and jdk and jdk.is_dir():
        found["gradle"] = {
            "files": {
                "settings.gradle": "rootProject.name = 'perf'\n",
                "build.gradle": "plugins { id 'java' }\nrepositories { mavenCentral() }\n"
                "dependencies {\n  implementation 'com.google.guava:guava:33.3.1-jre'\n"
                "  testImplementation 'org.junit.jupiter:junit-jupiter:5.11.3'\n"
                "  testRuntimeOnly 'org.junit.platform:junit-platform-launcher'\n}\n"
                "test { useJUnitPlatform() }\n",
                "src/main/java/perf/App.java": "package perf;\n\nimport com.google.common.base."
                "Joiner;\n\npublic class App {\n  public static String join(String... parts) {\n"
                '    return Joiner.on(",").join(parts);\n  }\n}\n',
                "src/test/java/perf/AppTest.java": "package perf;\n\nimport static org.junit."
                "jupiter.api.Assertions.assertEquals;\n\nimport org.junit.jupiter.api.Test;\n\n"
                'class AppTest {\n  @Test\n  void joins() {\n    assertEquals("a,b", App.join("a", '
                '"b"));\n  }\n}\n',
            },
            "steps": [[str(gradle), "build", "--no-daemon", "-q", "-g", "{cache}"]],
            "env": {"JAVA_HOME": str(jdk)},
            "clean": ["build", ".gradle"],
        }
    for name, project in found.items():
        project["path"] = root / "projects" / name
    return found


def make(project: dict, name: str, caches: Path):
    """The project as a repository, its lock file made by a first install (warming the warm
    cache), so both agents install the same thing."""
    path = project["path"]
    path.mkdir(parents=True)
    for file, text in project["files"].items():
        write(path / file, text)
    settings = {"tool": name, "steps": project["steps"], "env": project.get("env", {}),
                "clean": project["clean"], "caches": str(caches), "runs": RUNS}  # fmt: skip
    write(path / "bench.json", json.dumps(settings, indent=1))
    write(path / "bench.py", BENCH)
    write(path / ".gitignore", "\n".join(project["clean"]) + "\n")
    git(path, "init", "-q", "-b", "main")
    done = subprocess.run([sys.executable, "bench.py", "warm", "1"], cwd=path, capture_output=True,
                          text=True)  # fmt: skip
    assert done.returncode == 0, f"{name}: the warm-up failed: {done.stderr[-1500:]}"
    for clean in project["clean"]:
        shutil.rmtree(path / clean, ignore_errors=True)
    git(path, "add", ".")
    git(path, "-c", "user.email=p@test", "-c", "user.name=p", "commit", "-qm", "init")
    log(f"{name}: made, warm cache ready")


def times(database: Path, worker: str, mode: str) -> tuple[list[float], float]:
    """The run times its `bench.py <mode>` command printed, and how long the command took."""
    with contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT output, started_at, ended_at FROM commands WHERE session_id=? AND command "
            "LIKE ? AND exit_code=0 ORDER BY id DESC",
            (worker, f"%bench.py {mode}%"),
        ).fetchall()
    for output, started, ended in rows:
        found = [float(m) for m in re.findall(rf"TIME \S+ {mode} \d+ ([\d.]+)", output or "")]
        if len(found) == RUNS and ended:
            from datetime import datetime

            took = (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
            return found, took
    return [], 0.0


async def bench(app: App, router: str, project: Path, agent: str, database: Path) -> dict:
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(
        f"/api/sessions/{router}/messages",
        {"text": f"Use {agent} for this, a new worker: in {project}, in your copy of it, run "
         "`python3 bench.py cold` and then `python3 bench.py warm`, exactly as written, each as "
         "its own command and in the foreground (each takes a few minutes; let it finish). "
         "Change nothing. Report the TIME lines they print."},
    )  # fmt: skip

    async def assigned():
        rows = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        started = [
            m["meta"]["session_id"]
            for m in rows
            if m["kind"] == "notice"
            and m["text"].startswith("Started")
            and m["meta"].get("session_id")
        ]
        return started[0] if started else None

    worker = await wait_for(f"{agent}: assigned", assigned, 300, app)
    view = await app.get(f"/api/sessions/{worker}")
    assert (view.get("agent") or "copilot") == agent, f"on {agent}: {view.get('agent')}"

    async def measured():
        cold, warm = times(database, worker, "cold"), times(database, worker, "warm")
        return (cold, warm) if cold[0] and warm[0] else None

    (cold, cold_took), (warm, warm_took) = await wait_for(
        f"{agent}: both benches ran", measured, 3600, app
    )
    with contextlib.suppress(Exception):
        await app.post(f"/api/sessions/{worker}/close")
    return {
        "cold": cold, "warm": warm,
        # What the agent's command took beyond the runs it timed (its own cost per command).
        "overhead": {"cold": cold_took - sum(cold), "warm": warm_took - sum(warm)},
    }  # fmt: skip


def within(codex: float, copilot: float) -> bool:
    return codex <= copilot + max(0.10 * copilot, 15.0)


async def main() -> int:
    live_e2e.PORT, live_e2e.SLACK_PORT = PORT, SLACK_PORT
    live_e2e.BASE = f"http://127.0.0.1:{PORT}"
    serve_in_thread(Workspace(), SLACK_TOKEN, SLACK_PORT)
    root = Path(tempfile.mkdtemp(prefix="cos-perf-")).resolve()
    home, _, _ = setup(root)
    caches = root / "caches"
    caches.mkdir()
    chosen = projects(root)
    if wanted := {name.lower() for name in sys.argv[1:]}:
        chosen = {name: project for name, project in chosen.items() if name in wanted}
    for name, project in chosen.items():
        make(project, name, caches)
    app = App(root / "data", root / "log", {"COPILOT_HOME": str(home)})
    await app.start()
    database = root / "data" / "chiefofstaff.db"
    results: dict[str, dict] = {}
    try:
        current = (await app.get("/api/state"))["settings"]
        await app.post("/api/settings", {
            **current, "projects": [str(p["path"]) for p in chosen.values()], "model": MODEL,
            "max_workers": 2, "mac_notifications": False, "digest_time": "", "worker_yolo": True,
            "agents": {"codex": {"enabled": True, "model": "", "limit": 0},
                       "new_workers": "copilot"},
        })  # fmt: skip
        router = (await app.get("/api/state"))["router_id"]
        for index, (name, project) in enumerate(chosen.items()):
            order = ["codex", "copilot"] if index % 2 == 0 else ["copilot", "codex"]
            results[name] = {}
            for agent in order:
                log(f"=== {name} on {agent}")
                results[name][agent] = await bench(app, router, project["path"], agent, database)
                log(f"{name} {agent}: {results[name][agent]}")
    finally:
        await app.stop()
        with contextlib.suppress(Exception):
            await cleanup(root / "data")
        (root.parent / f"cos-perf-{int(time.time())}.json").write_text(
            json.dumps(results, indent=1)
        )
    print("\n================ PERFORMANCE GATE (median of 5, seconds) ================")
    print(f"{'project':8} {'cache':5} {'Copilot':>8} {'Codex':>8} {'margin':>7}  verdict  "
          "(agent's own cost per command: Copilot / Codex)")  # fmt: skip
    failed = []
    for name, by_agent in results.items():
        for mode in ("cold", "warm"):
            copilot = statistics.median(by_agent["copilot"][mode])
            codex = statistics.median(by_agent["codex"][mode])
            margin = max(0.10 * copilot, 15.0)
            ok = within(codex, copilot)
            failed += [] if ok else [f"{name} {mode}"]
            print(f"{name:8} {mode:5} {copilot:8.1f} {codex:8.1f} {margin:7.1f}  "
                  f"{'pass' if ok else 'FAIL':7}  ({by_agent['copilot']['overhead'][mode]:.1f} / "
                  f"{by_agent['codex']['overhead'][mode]:.1f})")  # fmt: skip
    print("all within the margin" if not failed else f"OVER THE MARGIN: {', '.join(failed)}")
    shutil.rmtree(root, ignore_errors=True)
    return 0 if results and not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
