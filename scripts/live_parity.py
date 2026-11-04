"""Live parity check: the same tasks in a standalone Copilot session and in a Chief of Staff
worker (reached through the Router, as the owner would), side by side.

It compares what each can do (plugins, MCP servers, skills, agents, the owner's tools), the
answers they give, and how long they take. Uses a temporary Copilot home holding a copy of
your sign-in plus a test plugin, MCP servers, skills and agents; nothing of yours is changed.

    uv run python scripts/live_parity.py
"""

import asyncio
import csv
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
from live_e2e import App, git, log, wait_for

# Its own port, so it can run next to the live end-to-end test.
live_e2e.PORT = 18767
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"

MODEL = os.environ.get("PARITY_MODEL", "claude-sonnet-5.5")
COPILOT = shutil.which("copilot") or "copilot"

PROBE_MCP = '''from mcp.server.fastmcp import FastMCP

server = FastMCP("probe")


@server.tool()
def probe_ping() -> str:
    """Answers with this server's name: proves the server is reachable."""
    return "pong from " + __import__("os").environ["PROBE_NAME"]


server.run()
'''

TEXTKIT = '''"""Small text helpers."""


def words(text: str) -> list[str]:
    return text.split()


def initials(name: str) -> str:
    return "".join(part[0].upper() for part in name.split() if part)


def chunk(items: list, size: int) -> list[list]:
    """Items in groups of `size`; the last group may be smaller."""
    return [items[start : start + size] for start in range(0, len(items) - size, size)]
'''

MONEY = '''"""Money formatting."""


def format_money(amount: float, currency: str = "$") -> str:
    """An amount like $1,234.50."""
    rounded = round(amount, 2)
    whole, cents = f"{rounded:.2f}".split(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return f"{currency}{','.join(groups)}.{cents}"
'''

HIDDEN_MONEY = """import pytest

from textkit.money import format_money


@pytest.mark.parametrize(
    "amount, expected",
    [
        (1.005, "$1.01"),
        (2.675, "$2.68"),
        (-1234.5, "-$1,234.50"),
        (-123456, "-$123,456.00"),
        (0, "$0.00"),
        (1234567.891, "$1,234,567.89"),
        (999.999, "$1,000.00"),
    ],
)
def test_format_money(amount, expected):
    assert format_money(amount) == expected


def test_other_currency():
    assert format_money(-5, "€") == "-€5.00"
"""

HIDDEN_TESTS = """import pytest

from textkit import slugify


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Hello World", "hello-world"),
        ("  Crème Brûlée!  ", "creme-brulee"),
        ("a--b__c", "a-b-c"),
        ("Ünïcödé façade", "unicode-facade"),
        ("---", ""),
        ("C'est l'été", "c-est-l-ete"),
        ("100% sure", "100-sure"),
    ],
)
def test_slugify(text, expected):
    assert slugify(text) == expected


def test_max_length_cuts_at_a_hyphen():
    assert slugify("the quick brown fox", max_length=12) == "the-quick"
    assert len(slugify("the quick brown fox", max_length=12)) <= 12


def test_max_length_without_a_hyphen_cuts_the_word():
    assert slugify("supercalifragilistic", max_length=5) == "super"


def test_max_length_none_keeps_everything():
    assert slugify("the quick brown fox") == "the-quick-brown-fox"
"""

CAPABILITIES = (
    "Do each of these and answer with one markdown table (item | result), nothing else. Do not "
    "change any files.\n"
    "1. Call the probe_ping tool of every MCP server whose name starts with 'probe' (one row "
    "per server: its name and its answer).\n"
    "2. List every skill you can use whose name starts with 'probe'.\n"
    "3. List every custom agent you can delegate to whose name starts with 'probe'.\n"
    "4. Run `gh api user --jq .login`.\n"
    "5. Run `docker info --format '{{.ServerVersion}}'`.\n"
    "6. Run `security find-generic-password -s gh:github.com >/dev/null 2>&1; echo $?`.\n"
    "7. Run `~/.local/bin/pre-commit --version`.\n"
    "8. Run `test -r ~/.gitconfig && echo readable`.\n"
    "9. Run `ls ~/workspace | head -3`.\n"
    "10. With the GitHub MCP server's get_file_contents, read the first line of README.md in "
    "evanl666/chiefofstaff."
)
CODING = (
    "In textkit/__init__.py add slugify(text, max_length=None): lowercase; turn accented "
    "letters into ASCII (é -> e); replace every run of other non-alphanumeric characters with "
    "one hyphen; no leading or trailing hyphens. With max_length, the result is at most that "
    "long, cut at a hyphen when there is one to cut at (and never ending in a hyphen). Add "
    "pytest tests for it, run the whole test suite (uv run --with pytest python -m pytest), "
    "and commit."
)
BUGFIX = (
    "Customers report two bugs in format_money (textkit/money.py): format_money(1.005) shows "
    "$1.00 but should round half up to $1.01, and negative amounts look wrong: "
    "format_money(-1234.5) should be -$1,234.50. Fix it, add regression tests, run the suite "
    "(uv run --with pytest python -m pytest) and commit."
)
REVIEW = (
    "Review the merged PR cli/cli#14801 (Replace msgpack with a minimal node ID encoder): what "
    "it changes, whether the new encoder produces exactly the bytes the msgpack code did for "
    "every input it can get, and concrete problems with file and line. Do not change any files."
)
ANALYSIS = (
    "Analyze data/sales.csv (columns: date, region, product, units, unit_price): total "
    "revenue (units * unit_price) per region, the month with the highest total revenue, and "
    "any rows that look wrong. Give the exact numbers. Do not change any files."
)
CHINESE = (  # As the owner writes: full-width punctuation included.
    "用 data/sales.csv 算一下每个 region 的总收入\uff08units * unit_price\uff09"
    "和每个月的环比增长\uff0c"
    "一步一步讲清楚你是怎么算的\uff0c最后用一个 markdown 表格总结。不要改任何文件。"
)
SKILLS = (
    "Use the probe-repo-skill skill, then delegate a one-line task to the probe-user custom "
    "agent asking it to say hi. Report what each one said, word for word."
)
WEB = (
    "Look up the latest released version of the Python package `requests` on PyPI (fetch "
    "https://pypi.org/pypi/requests/json) and tell me the version and its release date."
)
MCP_READ = (
    "Using the GitHub MCP server's tools (not the gh command), find the three most recent "
    "releases of cli/cli and list their tag names."
)
INVESTIGATE = (
    "Users say textkit.chunk() sometimes drops items at the end. Find out exactly why, with "
    "an example input that shows it, and say what the fix is. Do not change any files."
)
TASKS = {
    "capabilities": CAPABILITIES,
    "coding": CODING,
    "bugfix": BUGFIX,
    "analysis": ANALYSIS,
    "review": REVIEW,
    "chinese": CHINESE,
    "skills": SKILLS,
    "web": WEB,
    "mcp_read": MCP_READ,
    "investigate": INVESTIGATE,
}
if only := os.environ.get("PARITY_TASKS"):
    TASKS = {name: TASKS[name] for name in only.split(",")}
HIDDEN = {"coding": HIDDEN_TESTS, "bugfix": HIDDEN_MONEY}


def truths() -> dict:
    """Answers checked against the world, taken when the run starts."""
    import urllib.request

    found = {}
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/requests/json", timeout=20) as page:
            found["requests"] = json.load(page)["info"]["version"]
    except Exception:
        found["requests"] = ""
    tags = subprocess.run(
        ["gh", "api", "repos/cli/cli/releases?per_page=3", "--jq", ".[].tag_name"],
        capture_output=True, text=True,
    ).stdout.split()  # fmt: skip
    found["releases"] = tags
    return found


def score(name: str, answer: str, truth: dict, world: dict) -> str:
    """How much of what was asked the answer has (beyond the hidden tests and the analysis)."""
    plain = answer.replace(",", "")
    if name == "chinese":
        totals = [f"{t:.2f}".rstrip("0").rstrip(".") in plain for t in truth["totals"].values()]
        table = "|---" in answer.replace(" ", "") or "|:--" in answer.replace(" ", "")
        return f"totals {sum(totals)}/3, table {'ok' if table else 'MISSING'}, {len(answer)} chars"
    if name == "skills":
        skill = "probe-repo-skill" in answer
        agent = "probe-user" in answer
        return f"skill {'ok' if skill else 'MISSING'}, agent {'ok' if agent else 'MISSING'}"
    if name == "web":
        version = world.get("requests", "")
        return f"version {'ok' if version and version in answer else 'MISSING'} ({version})"
    if name == "mcp_read":
        tags = world.get("releases") or []
        return f"tags {sum(tag in answer for tag in tags)}/{len(tags)}"
    if name == "investigate":
        cause = "len(items) - size" in answer or "len(items)-size" in answer
        return f"cause {'ok' if cause else 'MISSING'}"
    return ""


def analysis_score(answer: str, truth: dict) -> str:
    plain = answer.replace(",", "")
    found = []
    for region, total in truth["totals"].items():
        shown = f"{total:.2f}".rstrip("0").rstrip(".")
        found.append(f"{region} {'ok' if shown in plain else 'MISSING'}")
    found.append("best month " + ("ok" if "2026-04" in answer or "April" in answer else "MISSING"))
    found.append("price row " + ("ok" if "1200" in plain else "MISSING"))
    found.append("negative row " + ("ok" if "-5" in plain else "MISSING"))
    return ", ".join(found)


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def sales(path: Path) -> dict:
    """A sales file with known answers: two bad rows (a negative quantity, a 100x price)."""
    rng = random.Random(7)
    rows, totals, months = [], {}, {}
    for day in range(1, 181):
        month = 1 + (day - 1) // 30
        for region in ("north", "south", "west"):
            units = rng.randint(1, 20)
            price = rng.choice([9.5, 12.0, 20.0])
            rows.append(
                [f"2026-{month:02d}-{(day - 1) % 28 + 1:02d}", region, "widget", units, price]
            )
    rows[40][3] = -5
    rows[300][4] = 1200.0
    for date, region, _, units, price in rows:
        totals[region] = totals.get(region, 0) + units * price
        months[date[:7]] = months.get(date[:7], 0) + units * price
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        out = csv.writer(handle)
        out.writerow(["date", "region", "product", "units", "unit_price"])
        out.writerows(rows)
    return {
        "totals": {region: round(total, 2) for region, total in totals.items()},
        "best_month": max(months, key=months.get),
        "bad_rows": [rows[40], rows[300]],
    }


def setup(root: Path) -> tuple[Path, Path, dict]:
    home = root / "copilot-home"
    project = root / "project"
    standalone = root / "standalone"
    standalone.mkdir()
    home.mkdir()
    text = (Path.home() / ".copilot" / "config.json").read_text()
    kept = [line for line in text.splitlines() if not line.lstrip().startswith("//")]
    config = json.loads("\n".join(kept))
    config["trustedFolders"] = [str(project), str(standalone)]  # The owner trusts their repo.
    write(home / "config.json", json.dumps(config))
    server = root / "probe_mcp.py"
    write(server, PROBE_MCP)

    def mcp(name):
        return {"type": "local", "command": sys.executable, "args": [str(server)],
                "env": {"PROBE_NAME": name}, "tools": ["*"]}  # fmt: skip

    write(home / "mcp-config.json", json.dumps({"mcpServers": {"probe-user": mcp("probe-user")}}))
    skill = "---\nname: {0}\ndescription: The {0} test skill.\n---\nSay hi.\n"
    agent = "---\nname: {0}\ndescription: The {0} test agent.\n---\nYou probe.\n"
    write(home / "skills" / "probe-user-skill" / "SKILL.md", skill.format("probe-user-skill"))
    write(home / "agents" / "probe-user.agent.md", agent.format("probe-user"))
    market = root / "market"
    plugin = market / "plugins" / "probe-plugin"
    write(plugin / "plugin.json", json.dumps({"name": "probe-plugin", "version": "1.0.0",
          "description": "Test plugin", "skills": "skills/", "agents": "agents/",
          "mcpServers": ".mcp.json"}))  # fmt: skip
    write(plugin / "skills" / "probe-plugin-skill" / "SKILL.md", skill.format("probe-plugin-skill"))
    write(plugin / "agents" / "probe-plugin.agent.md", agent.format("probe-plugin"))
    write(plugin / ".mcp.json", json.dumps({"mcpServers": {"probe-plugin": mcp("probe-plugin")}}))
    write(market / ".github" / "plugin" / "marketplace.json", json.dumps({"name": "probe-market",
          "owner": {"name": "probe"}, "plugins": [{"name": "probe-plugin", "version": "1.0.0",
          "description": "Test plugin", "source": "./plugins/probe-plugin"}]}))  # fmt: skip
    git(market, "init", "-q", "-b", "main")
    git(market, "add", ".")
    git(market, "-c", "user.email=p@test", "-c", "user.name=p", "commit", "-qm", "market")
    env = {**os.environ, "COPILOT_HOME": str(home)}
    for command in (["marketplace", "add", str(market)], ["install", "probe-plugin@probe-market"]):
        subprocess.run([COPILOT, "plugin", *command], env=env, check=True, capture_output=True)

    project.mkdir()
    git(project, "init", "-q", "-b", "main")
    write(project / "textkit" / "__init__.py", TEXTKIT)
    write(project / "textkit" / "money.py", MONEY)
    write(project / "tests" / "test_textkit.py", "from textkit import initials\n\n\n"
          "def test_initials():\n    assert initials('ada lovelace') == 'AL'\n")  # fmt: skip
    write(project / "pyproject.toml", '[project]\nname = "textkit"\nversion = "0.1.0"\n'
          '\n[tool.pytest.ini_options]\npythonpath = ["."]\n')  # fmt: skip
    truth = sales(project / "data" / "sales.csv")
    write(project / ".mcp.json", json.dumps({"mcpServers": {"probe-repo": mcp("probe-repo")}}))
    write(project / ".github" / "skills" / "probe-repo-skill" / "SKILL.md",
          skill.format("probe-repo-skill"))  # fmt: skip
    write(project / ".github" / "agents" / "probe-repo.agent.md", agent.format("probe-repo"))
    write(project / "AGENTS.md", "Python 3.11+. Run tests with uv run --with pytest.\n")
    git(project, "add", ".")
    git(project, "-c", "user.email=p@test", "-c", "user.name=p", "commit", "-qm", "init")
    return home, project, truth


async def standalone_run(name: str, task: str, home: Path, project: Path, root: Path) -> dict:
    checkout = root / "standalone" / name
    git(project, "worktree", "add", "-q", "-b", f"standalone-{name}", str(checkout))
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        COPILOT, "-p", task, "--model", MODEL, "--allow-all-tools", "-s",
        cwd=checkout, env={**os.environ, "COPILOT_HOME": str(home)},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    stdout, stderr = await asyncio.wait_for(process.communicate(), 1500)
    seconds = time.monotonic() - started
    log(f"standalone {name}: {seconds:.0f}s")
    return {
        "seconds": seconds,
        "answer": stdout.decode().strip() or stderr.decode().strip(),
        "cwd": checkout,
        "timeline": cli_timeline(home, checkout),
    }


def cli_timeline(home: Path, cwd: Path) -> list[str]:
    """What the terminal session did when, from Copilot's own log of it."""
    for log_file in (home / "session-state").glob("*/events.jsonl"):
        events = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
        start = next((e for e in events if e.get("type") == "session.start"), None)
        if not start or str(cwd) not in json.dumps(start.get("data", {}).get("context", {})):
            continue
        begun = datetime.fromisoformat(start["timestamp"].replace("Z", "+00:00")).timestamp()
        lines = []
        for event in events:
            kind = event.get("type")
            data = event.get("data") or {}
            if kind == "tool.execution_start":
                text = f"{data.get('toolName')} {json.dumps(data.get('arguments'))[:80]}"
            elif kind == "assistant.message" and data.get("content"):
                text = "said: " + data["content"][:80]
            elif kind in {"user.message", "session.idle", "assistant.turn_start"}:
                text = kind
            else:
                continue
            at = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
            lines.append(f"{at - begun:6.1f}s {text}".replace("\n", " "))
        return lines
    return ["(no log found)"]


async def worker_run(app: App, router: str, name: str, task: str, project: Path) -> dict:
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    known = {t["id"] for t in (await app.get("/api/state"))["tasks"]}
    started = time.monotonic()
    sent_at = time.time()
    await app.post(f"/api/sessions/{router}/messages", {"text": f"In {project}: {task}"})

    async def assigned():
        """A new worker, or a new task for one that has the context (the Router's choice)."""
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        return next(
            (m["meta"]["session_id"] for m in messages
             if m["kind"] == "notice" and m["text"].startswith(("Started", "New task"))),
            None,
        )  # fmt: skip

    worker = await wait_for(f"{name}: the Router assigns it", assigned, 300)
    spawned_after = time.monotonic() - started

    async def reported():
        tasks = [
            t
            for t in (await app.get("/api/state"))["tasks"]
            if t["session_id"] == worker and t["id"] not in known
        ]
        return next((t for t in tasks if t["status"] in {"review", "done", "waiting"}), None)

    found = await wait_for(f"{name}: the worker reports", reported, 1500, app)
    seconds = time.monotonic() - started
    report = await app.get(f"/api/tasks/{found['id']}/report")
    row = await app.get(f"/api/sessions/{worker}")
    messages = [
        m
        for m in await app.get(f"/api/sessions/{worker}/messages")
        if datetime.fromisoformat(m["created_at"]).timestamp() >= sent_at - 1
    ]
    begun = datetime.fromisoformat(messages[0]["created_at"]).timestamp()
    timeline = [
        f"{datetime.fromisoformat(m['created_at']).timestamp() - begun:6.1f}s {m['kind']:>9}: "
        + m["text"][:90].replace("\n", " ")
        for m in messages
    ]
    branched = [c["path"] for c in row.get("checkouts") or [] if c.get("branch")]
    changed_in = branched[-1] if branched else row["cwd"]
    log(f"worker {name}: {seconds:.0f}s (started after {spawned_after:.0f}s) [{found['status']}]")
    return {
        "seconds": seconds,
        "router_seconds": spawned_after,
        "answer": report.get("markdown") or report.get("text") or json.dumps(report)[:4000],
        # Where it changed code: its own branch (a worker in the project folder adds one).
        "cwd": Path(changed_in),
        "status": found["status"],
        "instructions": found.get("goal") or row.get("task", ""),
        "timeline": timeline,
    }


def hidden_tests(checkout: Path, tests: str) -> str:
    write(checkout / "tests" / "test_hidden_parity.py", tests)
    result = subprocess.run(
        ["uv", "run", "-q", "--with", "pytest", "python", "-m", "pytest", "-q",
         "tests/test_hidden_parity.py"],
        cwd=checkout, capture_output=True, text=True,
    )  # fmt: skip
    (checkout / "tests" / "test_hidden_parity.py").unlink()
    return (result.stdout.strip().splitlines() or ["no output"])[-1]


async def run(number: int) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="cos-parity-") as temporary:
        root = Path(temporary).resolve()
        home, project, truth = setup(root)
        world = truths()
        app = App(root / "data", root / "chiefofstaff.log", {"COPILOT_HOME": str(home)})
        await app.start()
        summary = []
        try:
            state = await app.get("/api/state")
            router = state["router_id"]
            await app.post(
                "/api/settings",
                {"projects": [str(project)], "model": MODEL, "max_workers": 5,
                 "mac_notifications": False, "digest_time": ""},
            )  # fmt: skip
            standalone = {
                name: asyncio.create_task(standalone_run(name, task, home, project, root))
                for name, task in TASKS.items()
            }
            workers = {}
            for name, task in TASKS.items():
                workers[name] = await worker_run(app, router, name, task, project)
            done = {name: await job for name, job in standalone.items()}
            print(f"\n================ PARITY REPORT, run {number} ================")
            print(f"model {MODEL}; truth for the analysis: {json.dumps(truth)}")
            for name in TASKS:
                one, other = done[name], workers[name]
                line = (f"{name}: standalone {one['seconds']:.0f}s | worker "
                        f"{other['seconds']:.0f}s (Router {other['router_seconds']:.0f}s) "
                        f"[{other['status']}]")  # fmt: skip
                print(f"\n### {line}")
                print(f"--- worker instructions from the Router:\n{other['instructions']}")
                print(f"--- standalone answer:\n{one['answer'][:5000]}")
                print(f"--- worker answer:\n{other['answer'][:5000]}")
                print("--- standalone timeline:\n" + "\n".join(one["timeline"]))
                print("--- worker timeline:\n" + "\n".join(other["timeline"]))
                for label, result in (("standalone", one), ("worker", other)):
                    if name in HIDDEN:
                        commits = git(result["cwd"], "log", "--oneline", "main..HEAD").strip()
                        verdict = hidden_tests(result["cwd"], HIDDEN[name])
                        line += (
                            f"\n    {label}: hidden tests {verdict}; commits: {commits or 'none'}"
                        )
                    if name == "analysis":
                        line += f"\n    {label}: {analysis_score(result['answer'], truth)}"
                    if extra := score(name, result["answer"], truth, world):
                        line += f"\n    {label}: {extra}"
                summary.append(line)
            changed = git(project, "status", "--porcelain", "--untracked-files=no").strip()
            commits = git(project, "rev-list", "--count", "main").strip()
            untouched = not changed and commits == "1"
            summary.append(f"the owner's checkout untouched: {'yes' if untouched else 'NO'}")
            assert untouched, f"the owner's checkout changed: {changed or commits + ' commits'}"
        finally:
            await app.stop()
            git(project, "worktree", "prune")
        return summary


async def main():
    summaries = []
    for number in range(1, int(os.environ.get("PARITY_RUNS", "1")) + 1):
        summaries += [f"run {number}: {line}" for line in await run(number)]
    print("\n================ SUMMARY ================")
    print("\n".join(summaries))


if __name__ == "__main__":
    asyncio.run(main())
