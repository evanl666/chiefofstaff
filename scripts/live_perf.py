"""Performance gate (more-agents.md section 7, decision 18): the same projects, a worker on
Codex and one on Copilot (the Router on Copilot), cold and warm caches, five runs each. Passes
when, per project and cache state, the Codex worker's median install or build time is within 10%
(or 15 s, if that is more) of the Copilot worker's.

Each cache state is one command a worker runs, as given: `python3 bench.py cold|warm`, which
installs or builds five times and times each run inside (the receipt of the command, as the
agent ran it, is kept as well: what it took beyond the runs is the agent's own per-command cost).
The caches are this run's own (an empty one for each cold run, one warmed beforehand for the warm
runs), so nothing of the owner's is cleared. That the agent's commands would use the owner's own
caches, as Copilot's do, is checked apart: before its runs, `bench.py cold` prints where the
project's tool keeps its cache in the agent's environment (and HOME, TMPDIR and PATH), which must
be the same for both agents. Projects go one at a time, the two agents in turn
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
RUNS = int(os.environ.get("PERF_RUNS", "5"))
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
results = Path(setup["results"])
results.mkdir(parents=True, exist_ok=True)


def say(line):
    print(line)
    sys.stdout.flush()
    with open(str(results / (mode + ".txt")), "a") as out:
        out.write(line + "\\n")


if mode == "cold" and runs > 1:  # Where the owner's own cache would be, in this environment.
    for name in ("HOME", "TMPDIR", "PATH"):
        say("WHERE %s %s" % (name, os.environ.get(name, "")))
    for step in setup.get("where", []):
        done = subprocess.run(step, cwd=str(root), capture_output=True, text=True)
        say("WHERE cache %s" % (done.stdout.strip().splitlines() or ["?"])[-1])
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
    say("TIME %s %s %d %.2f" % (setup["tool"], mode, number, time.monotonic() - started))
    if mode == "cold":
        shutil.rmtree(str(cache), ignore_errors=True)
'''

# Projects of a real size (an install of tens of seconds when cold), so that the 10% of the margin
# is measured, not only its 15 seconds.
NODE_DEPS = {
    "express": "4.21.2", "lodash": "4.17.21", "axios": "1.7.9", "dayjs": "1.11.13",
    "commander": "12.1.0", "zod": "3.24.1", "date-fns": "4.1.0", "uuid": "11.0.3",
    "next": "15.1.3", "react": "19.0.0", "react-dom": "19.0.0", "typescript": "5.7.2",
    "eslint": "9.17.0", "jest": "29.7.0", "webpack": "5.97.1", "@babel/core": "7.26.0",
    "vite": "6.0.6", "@mui/material": "6.3.0", "@emotion/react": "11.14.0",
    "@emotion/styled": "11.14.0", "aws-sdk": "2.1692.0",
}  # fmt: skip
PYTHON_DEPS = [
    "requests==2.32.3", "rich==13.9.4", "pydantic==2.10.4", "httpx==0.28.1", "numpy==2.2.1",
    "pandas==2.2.3", "scipy==1.14.1", "scikit-learn==1.6.0", "matplotlib==3.10.0",
    "jupyterlab==4.3.4", "sqlalchemy==2.0.36", "fastapi==0.115.6", "boto3==1.35.90",
]  # fmt: skip
# One interpreter for both agents: which `python3` comes first differs between them (Codex runs
# commands in a login shell, which puts the system's folders first on macOS).
PYTHON = (
    subprocess.run(
        ["uv", "python", "find", "--system", "3.12"], capture_output=True, text=True
    ).stdout.strip()
    or "python3"
)


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
            "where": [["npm", "config", "get", "cache"]],
            "clean": ["node_modules"],
        },
        "pnpm": {
            "files": {"package.json": package},
            "steps": [["npx", "--yes", "pnpm@9.15.0", "install", "--store-dir", "{cache}"]],
            "where": [["npx", "--yes", "pnpm@9.15.0", "store", "path"]],
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
            "where": [["npx", "--yes", "yarn@1.22.22", "cache", "dir"]],
            "clean": ["node_modules"],
        },
        "uv": {
            "files": {
                "pyproject.toml": '[project]\nname = "perf"\nversion = "0.1.0"\n'
                'requires-python = ">=3.12"\ndependencies = ' + json.dumps(PYTHON_DEPS) + "\n"
            },
            "steps": [["uv", "sync", "--cache-dir", "{cache}", "--python", "3.12"]],
            "where": [["uv", "cache", "dir"]],
            "clean": [".venv"],
        },
        "pip": {
            "files": {"requirements.txt": "\n".join(PYTHON_DEPS) + "\n"},
            "steps": [
                [PYTHON, "-m", "venv", ".venv"],
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
            "where": [[PYTHON, "-m", "pip", "cache", "dir"]],
        },
    }
    if gradle and jdk and jdk.is_dir():
        found["gradle"] = {
            "files": {
                "settings.gradle": "rootProject.name = 'perf'\n",
                "build.gradle": "plugins { id 'java' }\nrepositories { mavenCentral() }\n"
                "dependencies {\n  implementation 'com.google.guava:guava:33.3.1-jre'\n"
                "  implementation 'org.springframework.boot:spring-boot-starter-web:3.4.1'\n"
                "  implementation 'org.springframework.boot:spring-boot-starter-data-jpa:3.4.1'\n"
                "  implementation 'com.fasterxml.jackson.core:jackson-databind:2.18.2'\n"
                "  implementation 'org.apache.commons:commons-lang3:3.17.0'\n"
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
            "where": [["sh", "-c", 'echo "${GRADLE_USER_HOME:-$HOME/.gradle}"']],
            "clean": ["build", ".gradle"],
        }
    for name, project in found.items():
        project["path"] = root / "projects" / name
    return found


def make(project: dict, name: str, caches: Path, results: Path):
    """The project as a repository, its lock file made by a first install (warming the warm
    cache), so both agents install the same thing."""
    path = project["path"]
    path.mkdir(parents=True)
    for file, text in project["files"].items():
        write(path / file, text)
    settings = {"tool": name, "steps": project["steps"], "env": project.get("env", {}),
                "clean": project["clean"], "caches": str(caches), "runs": RUNS,
                "where": project.get("where", []), "results": str(results / name)}  # fmt: skip
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


def times(results: Path, mode: str) -> list[float]:
    """The run times `bench.py <mode>` wrote (once all its runs are done)."""
    path = results / f"{mode}.txt"
    text = path.read_text() if path.exists() else ""
    found = [float(m) for m in re.findall(rf"^TIME \S+ {mode} \d+ ([\d.]+)$", text, re.MULTILINE)]
    return found if len(found) == RUNS else []


def where(results: Path) -> list[str]:
    path = results / "cold.txt"
    return [line for line in path.read_text().splitlines() if line.startswith("WHERE ")]


def took(database: Path, worker: str, mode: str) -> float | None:
    """How long the agent's `bench.py <mode>` command took, from its receipt (None: not known)."""
    from datetime import datetime

    with contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT started_at, ended_at FROM commands WHERE session_id=? AND command LIKE ? "
            "AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1",
            (worker, f"%bench.py {mode}%"),
        ).fetchall()
    for started, ended in rows:
        return (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
    return None


async def bench(
    app: App, router: str, project: Path, agent: str, database: Path, results: Path
) -> dict:
    shutil.rmtree(results, ignore_errors=True)
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
        cold, warm = times(results, "cold"), times(results, "warm")
        return (cold, warm) if cold and warm else None

    cold, warm = await wait_for(f"{agent}: both benches ran", measured, 3600, app)

    async def receipts():  # The receipts end a moment after the commands.
        found = {mode: took(database, worker, mode) for mode in ("cold", "warm")}
        return found if all(value is not None for value in found.values()) else None

    try:
        spent = await wait_for(f"{agent}: the receipts", receipts, 120, app)
    except Exception:
        spent = {mode: took(database, worker, mode) for mode in ("cold", "warm")}
    with contextlib.suppress(Exception):
        await app.post(f"/api/sessions/{worker}/close")
    timed = {"cold": cold, "warm": warm}
    return {
        **timed,
        "where": where(results),
        # What the agent's command took beyond the runs it timed (its own cost per command).
        "overhead": {
            mode: (None if spent[mode] is None else spent[mode] - sum(timed[mode]))
            for mode in timed
        },
    }


def within(codex: float, copilot: float) -> bool:
    return codex <= copilot + max(0.10 * copilot, 15.0)


async def main() -> int:
    live_e2e.PORT, live_e2e.SLACK_PORT = PORT, SLACK_PORT
    live_e2e.BASE = f"http://127.0.0.1:{PORT}"
    serve_in_thread(Workspace(), SLACK_TOKEN, SLACK_PORT)
    # Not in the temporary folder when it says so (LIVE_AGENTS_ROOT): workers may write there.
    parent = os.environ.get("LIVE_AGENTS_ROOT") or None
    root = Path(tempfile.mkdtemp(prefix="cos-perf-", dir=parent)).resolve()
    home, _, _ = setup(root)
    caches = root / "caches"
    caches.mkdir()
    results_root = root / "results"
    chosen = projects(root)
    if wanted := {name.lower() for name in sys.argv[1:]}:
        chosen = {name: project for name, project in chosen.items() if name in wanted}
    for name, project in chosen.items():
        make(project, name, caches, results_root)
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
                results[name][agent] = await bench(
                    app, router, project["path"], agent, database, results_root / name
                )
                log(f"{name} {agent}: {results[name][agent]}")
        with contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
            (tokens,) = db.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM agent_usage WHERE agent='codex'"
            ).fetchone()
            (spent,) = db.execute("SELECT COALESCE(SUM(credits), 0) FROM usage").fetchone()
        log(f"Codex tokens: {tokens:,}; Copilot credits: {spent:.1f}")
    finally:
        await app.stop()
        with contextlib.suppress(Exception):
            await cleanup(root / "data")
        (root.parent / f"cos-perf-{int(time.time())}.json").write_text(
            json.dumps(results, indent=1)
        )
    print("\n================ PERFORMANCE GATE (median of 5, seconds) ================")
    print(f"{'project':8} {'cache':5} {'Copilot':>8} {'Codex':>8} {'diff':>7} {'margin':>7}  "
          "verdict  (agent's own cost per command: Copilot / Codex)")  # fmt: skip
    failed = []

    def cost(value):
        return "?" if value is None else f"{value:.1f}"

    for name, by_agent in results.items():
        for mode in ("cold", "warm"):
            copilot = statistics.median(by_agent["copilot"][mode])
            codex = statistics.median(by_agent["codex"][mode])
            margin = max(0.10 * copilot, 15.0)
            ok = within(codex, copilot)
            failed += [] if ok else [f"{name} {mode}"]
            diff = f"{(codex - copilot) / copilot:+.0%}" if copilot else "-"
            print(f"{name:8} {mode:5} {copilot:8.1f} {codex:8.1f} {diff:>7} {margin:7.1f}  "
                  f"{'pass' if ok else 'FAIL':7}  ({cost(by_agent['copilot']['overhead'][mode])} / "
                  f"{cost(by_agent['codex']['overhead'][mode])})")  # fmt: skip
    print("all within the margin" if not failed else f"OVER THE MARGIN: {', '.join(failed)}")
    print("\n================ CACHES (where the owner's would be, per agent) ================")
    for name, by_agent in results.items():
        # PATH is shown, not compared: each agent may put its own helpers first.
        found = {
            agent: [line for line in by_agent[agent]["where"] if not line.startswith("WHERE PATH")]
            for agent in ("copilot", "codex")
        }
        same = found["copilot"] == found["codex"] and bool(found["copilot"])
        failed += [] if same else [f"{name} caches"]
        print(f"{name:8} {'same for both' if same else 'DIFFERENT'}")
        for agent in ("copilot", "codex") if not same else ("copilot",):
            print(f"  {agent if not same else 'both'}:")
            for line in found[agent]:
                print(f"    {line[:300]}")
        paths = {agent: by_agent[agent]["where"] for agent in ("copilot", "codex")}
        for agent, lines in paths.items():
            path = next((line[len("WHERE PATH ") :] for line in lines if "WHERE PATH" in line), "")
            others = next(
                (line[len("WHERE PATH ") :] for a, ls in paths.items() if a != agent
                 for line in ls if "WHERE PATH" in line), "",
            )  # fmt: skip
            extra = [part for part in path.split(":") if part and part not in others.split(":")]
            if extra:
                print(f"  PATH only on {agent}: {':'.join(extra)[:300]}")
    print("\nverdict: " + ("pass" if not failed else "FAIL: " + ", ".join(failed)))
    shutil.rmtree(root, ignore_errors=True)
    return 0 if results and not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
