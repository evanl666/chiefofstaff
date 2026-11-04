"""Live test of routing: does the Router give work to the worker that already knows the subject,
and start a new one only when it should?

Builds up a realistic day in one Chief of Staff (two projects, several workers, a restart) and
checks, for each request, which worker took it:

  reuse:  a follow-up in loose words (Chinese too), acting on what a worker found, a feature
          named by its name, a closed worker (reopened), after a restart
  new:    unrelated work, "start a fresh worker", "in parallel" while the worker is busy
  none:   a question about earlier work is answered without starting anything

    uv run python scripts/live_routing.py
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
import live_parity  # noqa: F401  (it sets its own port: this one is set after)

live_e2e.PORT = 18770
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import App, cleanup, git, log, transcript, wait_for  # noqa: E402
from live_parity import setup, write  # noqa: E402

MODEL = os.environ.get("ROUTING_MODEL", "claude-sonnet-5.5")
ORDERS = '''"""Orders."""


def total(prices: list[float]) -> float:
    return sum(prices)
'''


def orders_project(root: Path) -> Path:
    repo = root / "orders"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo / "orders.py", ORDERS)
    write(repo / "tests" / "test_orders.py", "from orders import total\n\n\n"
          "def test_total():\n    assert total([1.0, 2.0]) == 3.0\n")  # fmt: skip
    write(repo / "pyproject.toml", '[project]\nname = "orders"\nversion = "0.1.0"\n'
          '\n[tool.pytest.ini_options]\npythonpath = ["."]\n')  # fmt: skip
    git(repo, "add", ".")
    git(repo, "-c", "user.email=r@test", "-c", "user.name=r", "commit", "-qm", "init")
    return repo


class Day:
    def __init__(self, app: App):
        self.app = app
        self.results: list[tuple[str, bool, str]] = []

    async def router(self) -> str:
        return (await self.app.get("/api/state"))["router_id"]

    async def ask(self, text: str, settle: bool = True) -> tuple[str, str]:
        """Send the owner's message; returns how the Router routed it and to which session."""
        router = await self.router()
        messages = await self.app.get(f"/api/sessions/{router}/messages")
        mark = max((m["id"] for m in messages), default=0)
        sent = datetime.now(UTC).isoformat()
        await self.app.post(f"/api/sessions/{router}/messages", {"text": text})

        async def routed():
            after = await self.app.get(f"/api/sessions/{router}/messages?after={mark}")
            for message in after:
                if message["kind"] != "notice":
                    continue
                for prefix, kind in (("Started", "new"), ("New task", "task"),
                                     ("Continued in", "continued")):  # fmt: skip
                    if message["text"].startswith(prefix):
                        return kind, message["meta"]["session_id"]
            row = await self.app.get(f"/api/sessions/{router}")
            replied = [
                m for m in after if m["kind"] == "assistant" and not m["meta"].get("internal")
            ]
            if replied and row["status"] != "working":
                return "answered", ""
            return None

        kind, worker = await wait_for(f"the Router routes: {text[:60]}", routed, 300)
        if worker and settle:
            await self.finished(worker, sent)
        return kind, worker

    async def finished(self, worker: str, since: str):
        async def done():
            tasks = [t for t in (await self.app.get("/api/state"))["tasks"]
                     if t["session_id"] == worker and t["updated_at"] > since]  # fmt: skip
            return next((t for t in tasks if t["status"] in {"review", "done", "waiting"}), None)

        return await wait_for("its work is reported", done, 900, self.app)

    def check(self, name: str, ok: bool, detail: str):
        self.results.append((name, ok, detail))
        log(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")


async def run_day(app: App, project: Path, orders: Path) -> list:
    day = Day(app)
    kind, analyst = await day.ask(
        f"In {project}: analyze data/sales.csv - revenue per region and any rows that look "
        "wrong. Do not change files."
    )
    day.check("analysis starts a worker", kind == "new", kind)

    kind, coder = await day.ask(
        f"In {project}, in an isolated worktree: add slugify(text) to textkit/__init__.py "
        "(lowercase, ASCII, hyphens), with tests, and commit."
    )
    day.check("unrelated coding: another worker", kind == "new" and coder != analyst, kind)

    kind, orders_worker = await day.ask(
        f"In {orders}, in an isolated worktree: make total() round to cents, add a test, commit."
    )
    day.check("another project: another worker", kind == "new"
              and orders_worker not in {analyst, coder}, kind)  # fmt: skip

    kind, who = await day.ask("刚才那个销售数据分析\uff0c再按月份算一下每个 region 的收入。")
    day.check("a loose follow-up in Chinese reuses the analyst", who == analyst, f"{kind}")

    kind, who = await day.ask(
        "Fix the bad rows you found in sales.csv (negative units to 5, the 1200 price to 12) "
        "and commit it in a worktree."
    )
    day.check("acting on what a worker found reuses it", who == analyst, kind)

    kind, who = await day.ask("slugify should also take a separator argument (default '-').")
    day.check("a feature named by its name reuses its worker", who == coder, kind)

    kind, who = await day.ask("What did the analysis find about April?")
    day.check("a question about earlier work starts nothing", kind in {"answered", "continued",
              "task"} and who in {"", analyst}, f"{kind} {who[:8]}")  # fmt: skip

    await app.post(f"/api/sessions/{analyst}/close")
    kind, who = await day.ask(
        "For the sales data you analysed earlier, also compute the median unit_price per region."
    )
    day.check("a closed worker is reopened, not replaced", who == analyst, kind)

    kind, who = await day.ask(
        "orders 里的 total() 再加一个税率参数 tax_rate\uff0c默认 0\uff0c加测试\uff0c提交。"
    )
    day.check("the other project's follow-up reuses its worker", who == orders_worker, kind)

    await app.stop()
    await app.start()
    kind, who = await day.ask("Make slugify's separator argument keyword-only.")
    day.check("after a restart the right worker is found", who == coder, kind)

    kind, who = await day.ask(
        f"Start a fresh worker for this, separate from the others: in {project}, list the "
        "functions in textkit/__init__.py with one line each. Do not change files."
    )
    day.check("'start a fresh worker' starts one", kind == "new"
              and who not in {analyst, coder, orders_worker}, kind)  # fmt: skip

    kind, busy = await day.ask(
        f"In {orders}: run `sleep 40 && uv run --with pytest python -m pytest -q` and report.",
        settle=False,
    )
    await asyncio.sleep(8)
    kind2, other = await day.ask(
        f"Meanwhile, in parallel, in {orders}: count the lines of orders.py. Do not change files.",
        settle=False,
    )
    day.check("'in parallel' while it is busy: not queued behind it",
              other != busy or kind2 == "new", f"{kind}/{kind2}")  # fmt: skip
    return day.results


async def main():
    runs = int(os.environ.get("ROUTING_RUNS", "1"))
    every = []
    for number in range(1, runs + 1):
        with tempfile.TemporaryDirectory(prefix="cos-routing-") as temporary:
            root = Path(temporary).resolve()
            home, project, _ = setup(root)
            orders = orders_project(root)
            app = App(root / "data", root / "log", {"COPILOT_HOME": str(home)})
            await app.start()
            try:
                await app.post("/api/settings", {
                    "projects": [str(project), str(orders)], "model": MODEL, "max_workers": 4,
                    "mac_notifications": False, "digest_time": ""})  # fmt: skip
                every += [(number, *result) for result in await run_day(app, project, orders)]
            except BaseException:
                router = (await app.get("/api/state"))["router_id"]
                print(transcript(await app.get(f"/api/sessions/{router}/messages")))
                raise
            finally:
                await app.stop()
                await cleanup(root / "data")
                subprocess.run(["git", "-C", str(project), "worktree", "prune"])
    print("\n================ ROUTING ================")
    for number, name, ok, detail in every:
        print(f"run {number} {'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    failed = [item for item in every if not item[2]]
    print(f"{len(every) - len(failed)}/{len(every)} routed as expected")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
